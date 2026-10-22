"""Test support: reads the README and the how-to the way the examples test needs them read.

This module is not part of the installed package and is not exported from it. It turns Markdown
text into data, and it decides, from data it is handed, whether the documents hold their promises
(CP-536 to CP-538 in the change spec):

- a *command example* is a code block holding one `ka-telemetry` command alone, with a code block
  of that command's output directly after it. The output is kept character for character, because
  a trimmed comparison would let a changed space through;
- every other place a document shows `ka-telemetry` being run — in a block of any fence language,
  in an inline code span, inside another command line, in displayed output, in a launchd argument
  array, in a systemd command — is an *invocation* that must name only commands and options the
  installed command accepts;
- parsing has no effect on the world. Anything that touches the filesystem or a process (resolving
  a fixture path, running a command, capturing help) is handed in by the caller.

WHY A SCANNER OF OUR OWN: an examples test that finds nothing passes. So the scanner fails closed:
a form of invocation it cannot read, a code block never closed, an output with no command and an
example with no declared working directory are each refused with the document and the line,
instead of being skipped. Option names are read from the option declarations in the captured help
(an indented line that begins with a dash), never from every dash-word in it, because the help
mentions other programs' options in prose (`claude --settings`).
"""

from __future__ import annotations

import difflib
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

PRODUCT_NAME = "ka-telemetry"
TOP_LEVEL_HELP = PRODUCT_NAME  # the key under which the top-level help is handed to the vocabulary
REPOSITORY_ROOT = "."
WORKING_DIRECTORY_DECLARATION = "from the top directory of a copy of this repository"
FIXTURE_ROOT = "tests"
REPORTS = ("agents", "check", "runs", "steps")
# The options through which a report reads data. An example that leaves one out would read the
# machine's own files (or start `claude`), so it must name every one for its command.
DATA_OPTIONS = {
    "agents": ("--dir",),
    "check": ("--dir",),
    "runs": ("--agents-json", "--projects-root"),
    "steps": ("--projects-root",),
}

OUTPUT_LABELS = frozenset({"text"})
LAUNCHD_LABELS = frozenset({"xml", "plist"})
SYSTEMD_LABELS = frozenset({"ini", "systemd", "service"})

SHELL_BLOCK = "shell block"
INLINE_SPAN = "inline code span"
OUTPUT_TEXT = "output text"
LAUNCHD_ARGUMENTS = "launchd ProgramArguments"
SYSTEMD_COMMAND = "systemd command"

SUBSTITUTION_MARK = "$(…)"

_OPENING_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$")
_CLOSING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_INLINE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_OUTPUT_PARENTHESES = re.compile(r"\((" + PRODUCT_NAME + r"\s[^()\n]*)\)")
_OUTPUT_BACKTICKS = re.compile(r"`(" + PRODUCT_NAME + r"\s[^`\n]*)`")
_ENVIRONMENT_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SERVICE_EXEC_LINE = re.compile(r"^\s*(Exec\w*)\s*=\s*(.*)$")
_SERVICE_MODIFIERS = "@-:+!"
_COMMAND_SET_LINE = re.compile(r"^\s+\{([^{}\s]*)\}\s*$", re.MULTILINE)
_COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]*")
_LOOKUP_PROGRAMS = frozenset({"which", "type"})
_NEVER_CLOSED = "the code block opened here is never closed"


class DocumentError(Exception):
    """A document, or the help describing the command, breaks a rule. Names the place."""

    def __init__(self, source: str, line: int, message: str, token: str | None = None) -> None:
        super().__init__(f"{source}:{line}: {message}")
        self.source = source
        self.line = line
        self.message = message
        self.token = token


@dataclass(frozen=True)
class Block:
    """A fenced code block: where its opening fence is, its label and its lines, as written."""

    line: int
    end_line: int
    label: str
    body: tuple[str, ...]


@dataclass(frozen=True)
class Example:
    """A command and the output block directly after it."""

    source: str
    line: int
    command: str
    argv: tuple[str, ...]
    cwd: str
    expected_stdout: str


@dataclass(frozen=True)
class Invocation:
    """One place a document shows `ka-telemetry` being run, and the words it is run with."""

    source: str
    line: int
    argv: tuple[str, ...]
    context: str


@dataclass(frozen=True)
class Install:
    """One place a document shows the package being installed."""

    source: str
    line: int
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ParsedDocument:
    source: str
    cwd: str | None
    blocks: tuple[Block, ...]
    examples: tuple[Example, ...]
    invocations: tuple[Invocation, ...]
    installs: tuple[Install, ...]


@dataclass(frozen=True)
class Vocabulary:
    """What the installed command accepts, as its own help declares it."""

    commands: frozenset[str]
    options: Mapping[str, frozenset[str]]
    top_level_options: frozenset[str]


FixtureResolver = Callable[[Path, str], "Path | None"]


# --- reading a document ---------------------------------------------------------------------


def parse_document(text: str, source: str | Path) -> ParsedDocument:
    """Everything the examples test needs from one document, with nothing run."""
    source = str(source)
    lines = text.splitlines()
    blocks, prose = _split_blocks(lines, source)
    declaration_lines = _declaration_lines(prose)

    invocations: list[Invocation] = []
    installs: list[Install] = []
    alone_commands: dict[int, ShellCommand] = {}
    for index, block in enumerate(blocks):
        label = block.label.lower()
        if label in OUTPUT_LABELS:
            invocations.extend(_output_invocations(block, source))
        elif label in LAUNCHD_LABELS:
            invocations.extend(_launchd_invocations(block, source))
        elif label in SYSTEMD_LABELS:
            invocations.extend(_systemd_invocations(block, source))
        else:
            commands = scan_shell("\n".join(block.body), block.line + 1, source)
            found, installed = _classify(commands, source, SHELL_BLOCK, within_span=False)
            invocations.extend(found)
            installs.extend(installed)
            if len(commands) == 1 and PurePosixPath(commands[0].argv[0]).name == PRODUCT_NAME:
                alone_commands[index] = commands[0]
    for line_number, line in prose:
        found, installed = _span_sites(line_number, line, source)
        invocations.extend(found)
        installs.extend(installed)

    examples = _pair_examples(blocks, alone_commands, lines, declaration_lines, source)
    invocations.sort(key=lambda site: site.line)
    return ParsedDocument(
        source=source,
        cwd=REPOSITORY_ROOT if declaration_lines else None,
        blocks=tuple(blocks),
        examples=tuple(examples),
        invocations=tuple(invocations),
        installs=tuple(sorted(installs, key=lambda site: site.line)),
    )


def _split_blocks(lines: list[str], source: str) -> tuple[list[Block], list[tuple[int, str]]]:
    """The fenced blocks, and the other lines with their line numbers. A fence is closed only by a
    fence of its own kind that is at least as long.

    A block is never closed when the document ends inside it, and also when a labelled fence that
    could have closed it appears inside it: that line opens the next block, so this block's own
    closing fence is missing. Read as Markdown reads it, the next bare fence would end the block
    and the missing fence would never be named."""
    blocks: list[Block] = []
    prose: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        opening = _OPENING_FENCE.match(lines[index])
        if opening is None:
            prose.append((index + 1, lines[index]))
            index += 1
            continue
        indent, fence, label = len(opening.group(1)), opening.group(2), opening.group(3)
        body: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            closing = _CLOSING_FENCE.match(lines[cursor])
            if closing and _could_close(closing.group(1), fence):
                break
            reopening = _OPENING_FENCE.match(lines[cursor])
            if reopening and reopening.group(3) and _could_close(reopening.group(2), fence):
                raise DocumentError(source, index + 1, _NEVER_CLOSED)
            body.append(_without_indent(lines[cursor], indent))
            cursor += 1
        else:
            raise DocumentError(source, index + 1, _NEVER_CLOSED)
        blocks.append(Block(index + 1, cursor + 1, label, tuple(body)))
        index = cursor + 1
    return blocks, prose


def _could_close(candidate: str, fence: str) -> bool:
    """Whether a fence of these characters is of the open fence's kind and at least as long."""
    return candidate[0] == fence[0] and len(candidate) >= len(fence)


def _without_indent(line: str, indent: int) -> str:
    stripped = 0
    while stripped < indent and stripped < len(line) and line[stripped] == " ":
        stripped += 1
    return line[stripped:]


def _declaration_lines(prose: list[tuple[int, str]]) -> list[int]:
    """The line each paragraph that says where to run the examples begins on."""
    declarations: list[int] = []
    paragraph: list[tuple[int, str]] = []
    for entry in [*prose, (0, "")]:
        if entry[1].strip():
            paragraph.append(entry)
            continue
        joined = " ".join(text.strip() for _, text in paragraph)
        if WORKING_DIRECTORY_DECLARATION in joined:
            declarations.append(paragraph[0][0])
        paragraph = []
    return declarations


def _pair_examples(
    blocks: list[Block],
    alone_commands: dict[int, ShellCommand],
    lines: list[str],
    declaration_lines: list[int],
    source: str,
) -> list[Example]:
    examples: list[Example] = []
    paired_outputs: set[int] = set()
    for index, command in alone_commands.items():
        output = blocks[index + 1] if index + 1 < len(blocks) else None
        if (
            output is None
            or output.label.lower() not in OUTPUT_LABELS
            or not _only_blank_between(lines, blocks[index], output)
        ):
            continue
        block = blocks[index]
        if not any(line < block.line for line in declaration_lines):
            raise DocumentError(
                source,
                block.line,
                "no working directory is declared before this example; say where to run it "
                f"({WORKING_DIRECTORY_DECLARATION!r})",
            )
        paired_outputs.add(index + 1)
        examples.append(
            Example(
                source=source,
                line=block.line,
                command="\n".join(block.body).strip(),
                argv=command.argv,
                cwd=REPOSITORY_ROOT,
                expected_stdout="".join(f"{line}\n" for line in output.body),
            )
        )
    for index, block in enumerate(blocks):
        if block.label.lower() in OUTPUT_LABELS and index not in paired_outputs:
            raise DocumentError(
                source,
                block.line,
                "this output block has no command example directly before it",
            )
    return examples


def _only_blank_between(lines: list[str], first: Block, second: Block) -> bool:
    return all(not text.strip() for text in lines[first.end_line : second.line - 1])


# --- the shell, read just far enough --------------------------------------------------------


@dataclass(frozen=True)
class ShellCommand:
    argv: tuple[str, ...]
    line: int


def scan_shell(text: str, first_line: int, source: str) -> list[ShellCommand]:
    """The commands a shell text runs, including the ones inside `$(…)`. Syntax this does not read
    (backticks, here-documents, subshells, arithmetic, an unclosed quote) is refused, never
    skipped: an invocation inside it would escape the check."""
    return _ShellScanner(text, first_line, source).scan()


class _ShellScanner:
    def __init__(self, text: str, first_line: int, source: str) -> None:
        self._text = text
        self._position = 0
        self._line = first_line
        self._source = source
        self._commands: list[ShellCommand] = []

    def scan(self) -> list[ShellCommand]:
        self._sequence(nested=False)
        return self._commands

    def _fail(self, message: str) -> None:
        raise DocumentError(self._source, self._line, f"shell syntax not read here: {message}")

    def _record(self, words: list[str], line: int) -> None:
        arguments = list(words)
        while arguments and _ENVIRONMENT_ASSIGNMENT.match(arguments[0]):
            arguments.pop(0)
        if arguments:
            self._commands.append(ShellCommand(tuple(arguments), line))

    def _sequence(self, nested: bool) -> None:
        text = self._text
        words: list[str] = []
        word: list[str] | None = None
        command_line = self._line

        def start_word() -> list[str]:
            nonlocal word, command_line
            if word is None:
                word = []
                if not words:
                    command_line = self._line
            return word

        def end_word() -> None:
            nonlocal word
            if word is not None:
                words.append("".join(word))
                word = None

        def end_command() -> None:
            end_word()
            self._record(words, command_line)
            words.clear()

        while self._position < len(text):
            char = text[self._position]
            following = text[self._position + 1 : self._position + 2]
            if char == "\\":
                if following == "\n":
                    self._position += 2
                    self._line += 1
                elif following == "":
                    self._position += 1
                else:
                    start_word().append(following)
                    self._position += 2
            elif char in " \t\r":
                end_word()
                self._position += 1
            elif char == "\n":
                end_command()
                self._position += 1
                self._line += 1
            elif char == "#" and word is None:
                while self._position < len(text) and text[self._position] != "\n":
                    self._position += 1
            elif char == "&" and self._position and text[self._position - 1] in "<>":
                start_word().append(char)
                self._position += 1
            elif char in ";&|":
                end_command()
                self._position += 1
            elif char == "(":
                self._fail("a subshell is not read")
            elif char == ")":
                if not nested:
                    self._fail("a closing parenthesis with nothing open")
                end_command()
                self._position += 1
                return
            elif char == "`":
                self._fail("backtick command substitution is not read")
            elif char in "<>" and following in ("(", "<") and (following == "(" or char == "<"):
                self._fail("a here-document or process substitution is not read")
            elif char == "'":
                end = text.find("'", self._position + 1)
                if end == -1:
                    self._fail("a single quote is never closed")
                quoted = text[self._position + 1 : end]
                start_word().append(quoted)
                self._line += quoted.count("\n")
                self._position = end + 1
            elif char == '"':
                self._position += 1
                self._double_quoted(start_word())
            elif char == "$" and text.startswith("$(", self._position):
                self._substitution(start_word())
            else:
                start_word().append(char)
                self._position += 1
        if nested:
            self._fail("a command substitution is never closed")
        end_command()

    def _substitution(self, word: list[str]) -> None:
        if self._text.startswith("$((", self._position):
            self._fail("arithmetic expansion is not read")
        self._position += 2
        self._sequence(nested=True)
        word.append(SUBSTITUTION_MARK)

    def _double_quoted(self, word: list[str]) -> None:
        text = self._text
        while self._position < len(text):
            char = text[self._position]
            following = text[self._position + 1 : self._position + 2]
            if char == '"':
                self._position += 1
                return
            if char == "\\":
                if following == "\n":
                    self._position += 2
                    self._line += 1
                elif following in ("$", "`", '"', "\\"):
                    word.append(following)
                    self._position += 2
                else:
                    word.append(char)
                    self._position += 1
            elif char == "`":
                self._fail("backtick command substitution is not read")
            elif char == "$" and text.startswith("$(", self._position):
                self._substitution(word)
            else:
                if char == "\n":
                    self._line += 1
                word.append(char)
                self._position += 1
        self._fail("a double quote is never closed")


# --- which commands are invocations ---------------------------------------------------------


def _classify(
    commands: list[ShellCommand], source: str, context: str, within_span: bool
) -> tuple[list[Invocation], list[Install]]:
    invocations: list[Invocation] = []
    installs: list[Install] = []
    for command in commands:
        argv = command.argv
        # `nohup` runs the command after it, so that command is the invocation read and checked.
        if PurePosixPath(argv[0]).name == "nohup" and len(argv) > 1:
            argv = argv[1:]
        program = PurePosixPath(argv[0]).name
        if program == PRODUCT_NAME:
            if len(argv) > 1 or not within_span:
                invocations.append(Invocation(source, command.line, argv, context))
        elif _is_install(argv):
            installs.append(Install(source, command.line, argv))
        elif _only_looks_the_name_up(argv):
            continue
        elif PRODUCT_NAME in argv[1:]:
            raise DocumentError(
                source,
                command.line,
                f"{PRODUCT_NAME} is run here in a form this reader does not read: {' '.join(argv)}",
            )
    return invocations, installs


def _is_install(argv: tuple[str, ...]) -> bool:
    return _is_pip_install(argv) or _is_pipx_install(argv)


def _is_pip_install(argv: tuple[str, ...]) -> bool:
    return "install" in argv and any(word in ("pip", "pip3") for word in argv)


def _is_pipx_install(argv: tuple[str, ...]) -> bool:
    """`pipx install ka-telemetry`, with or without options between: the words are kept as typed."""
    return argv[0] == "pipx" and argv[1:2] == ("install",) and PRODUCT_NAME in argv[2:]


def _only_looks_the_name_up(argv: tuple[str, ...]) -> bool:
    return argv[0] in _LOOKUP_PROGRAMS or (argv[0] == "command" and argv[1:2] in (("-v",), ("-V",)))


def _span_sites(line_number: int, line: str, source: str) -> tuple[list[Invocation], list[Install]]:
    invocations: list[Invocation] = []
    installs: list[Install] = []
    for span in _INLINE_SPAN.finditer(line):
        inner = span.group(2).strip()
        if PRODUCT_NAME not in inner or inner == PRODUCT_NAME:
            continue
        commands = scan_shell(inner, line_number, source)
        found, installed = _classify(commands, source, INLINE_SPAN, within_span=True)
        invocations.extend(found)
        installs.extend(installed)
    return invocations, installs


def _output_invocations(block: Block, source: str) -> list[Invocation]:
    """A displayed output that recommends a command names it in parentheses or in backticks. A bare
    mention of the product's name in a sentence is not an invocation."""
    sites: list[Invocation] = []
    for offset, text in enumerate(block.body):
        line_number = block.line + 1 + offset
        spans = [*_OUTPUT_PARENTHESES.finditer(text), *_OUTPUT_BACKTICKS.finditer(text)]
        for match in sorted(spans, key=lambda found: found.start()):
            for command in scan_shell(match.group(1), line_number, source):
                if PurePosixPath(command.argv[0]).name == PRODUCT_NAME:
                    sites.append(Invocation(source, line_number, command.argv, OUTPUT_TEXT))
    return sites


def _launchd_invocations(block: Block, source: str) -> list[Invocation]:
    try:
        root = ElementTree.fromstring("\n".join(block.body).encode("utf-8"))
    except ElementTree.ParseError as error:
        raise DocumentError(
            source, block.line, f"this launchd file is not well-formed XML: {error}"
        )
    marker_lines = [
        offset for offset, text in enumerate(block.body) if "<key>ProgramArguments</key>" in text
    ]
    sites: list[Invocation] = []
    arrays = []
    for dictionary in root.iter("dict"):
        children = list(dictionary)
        for index, child in enumerate(children[:-1]):
            following = children[index + 1]
            if child.tag == "key" and child.text == "ProgramArguments" and following.tag == "array":
                arrays.append(tuple(item.text or "" for item in following if item.tag == "string"))
    for position, argv in enumerate(arrays):
        offset = marker_lines[position] if position < len(marker_lines) else 0
        if argv and PurePosixPath(argv[0]).name == PRODUCT_NAME:
            sites.append(Invocation(source, block.line + 1 + offset, argv, LAUNCHD_ARGUMENTS))
    return sites


def _systemd_invocations(block: Block, source: str) -> list[Invocation]:
    sites: list[Invocation] = []
    logical_lines: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for offset, text in enumerate(block.body):
        if not pending:
            start = offset
        if text.endswith("\\"):
            pending.append(text[:-1])
            continue
        pending.append(text)
        logical_lines.append((start, " ".join(pending)))
        pending = []
    for offset, text in logical_lines:
        found = _SERVICE_EXEC_LINE.match(text)
        if found is None:
            continue
        line_number = block.line + 1 + offset
        try:
            argv = tuple(shlex.split(found.group(2).lstrip(_SERVICE_MODIFIERS)))
        except ValueError as error:
            raise DocumentError(
                source, line_number, f"this service command is not readable: {error}"
            )
        if argv and PurePosixPath(argv[0]).name == PRODUCT_NAME:
            sites.append(Invocation(source, line_number, argv, SYSTEMD_COMMAND))
    return sites


# --- the document set -----------------------------------------------------------------------


def validate_document_set(
    readme: ParsedDocument,
    how_to: ParsedDocument,
    repo_root: Path,
    resolver: FixtureResolver,
) -> None:
    """Refuses a document set that could pass without proving anything: no examples at all, a
    README without exactly one `agents` example, a how-to missing one of the four reports, or an
    example that reads data from outside `tests/`. `resolver(repo_root, value)` answers the real
    path a fixture value names, or None when nothing is there."""
    if not readme.examples and not how_to.examples:
        raise DocumentError(
            readme.source,
            1,
            "no command example found in either document; finding none is not a pass",
        )
    if len(readme.examples) != 1:
        raise DocumentError(
            readme.source,
            1,
            "the README must hold exactly one command example, "
            f"and it holds {len(readme.examples)}",
        )
    only = readme.examples[0]
    if _report_of(only) != "agents":
        raise DocumentError(
            readme.source, only.line, "the README's one command example must be the agents report"
        )
    shown = {_report_of(example) for example in how_to.examples}
    for report in REPORTS:
        if report not in shown:
            raise DocumentError(
                how_to.source, 1, f"the how-to holds no command example for the {report} report"
            )
    for example in [*readme.examples, *how_to.examples]:
        _check_fixtures(example, repo_root, resolver)


def _report_of(example: Example) -> str | None:
    return example.argv[1] if len(example.argv) > 1 else None


def _check_fixtures(example: Example, repo_root: Path, resolver: FixtureResolver) -> None:
    fixtures = repo_root / FIXTURE_ROOT
    for option in DATA_OPTIONS.get(_report_of(example) or "", ()):
        value = _option_value(example.argv, option)
        if value is None:
            raise DocumentError(
                example.source,
                example.line,
                f"this example does not name {option}, "
                f"so it would read data outside {FIXTURE_ROOT}/",
                token=option,
            )
        if PurePosixPath(value).is_absolute() or value.startswith("~"):
            raise DocumentError(
                example.source,
                example.line,
                f"{option} {value} names data outside {FIXTURE_ROOT}/",
                token=value,
            )
        resolved = resolver(repo_root / example.cwd, value)
        if resolved is None:
            raise DocumentError(
                example.source,
                example.line,
                f"{option} {value} names nothing that exists",
                token=value,
            )
        if resolved != fixtures and fixtures not in resolved.parents:
            raise DocumentError(
                example.source,
                example.line,
                f"{option} {value} resolves outside {FIXTURE_ROOT}/ ({resolved})",
                token=value,
            )


def _option_value(argv: tuple[str, ...], option: str) -> str | None:
    for index, word in enumerate(argv):
        if word == option and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith(f"{option}="):
            return word.split("=", 1)[1]
    return None


# --- the command's vocabulary ---------------------------------------------------------------


def command_vocabulary(help_by_command: Mapping[str, str]) -> Vocabulary:
    """The commands and, for each, the options the installed command declares in its help.
    `help_by_command` maps `TOP_LEVEL_HELP` to the top-level help and each command's name to that
    command's help."""
    top_help = help_by_command.get(TOP_LEVEL_HELP)
    if top_help is None:
        raise DocumentError("help", 1, "the top-level help was not captured")
    names = _declared_commands(top_help)
    options: dict[str, frozenset[str]] = {}
    for name in names:
        if name not in help_by_command:
            raise DocumentError("help", 1, f"the help for the {name} command was not captured")
        options[name] = _declared_options(help_by_command[name], f"help for {name}")
    return Vocabulary(
        commands=frozenset(names),
        options=options,
        top_level_options=_declared_options(top_help, "top-level help"),
    )


def capture_vocabulary(read_help: Callable[[tuple[str, ...]], str]) -> Vocabulary:
    """The vocabulary of a command that can print its own help. `read_help(())` answers the
    top-level help and `read_help((name,))` a command's; the caller owns how they are fetched."""
    top_help = read_help(())
    helps = {TOP_LEVEL_HELP: top_help}
    for name in _declared_commands(top_help):
        helps[name] = read_help((name,))
    return command_vocabulary(helps)


def _declared_commands(top_help: str) -> list[str]:
    found = _COMMAND_SET_LINE.search(top_help)
    names = found.group(1).split(",") if found else []
    if not names or not all(_COMMAND_NAME.fullmatch(name) for name in names):
        raise DocumentError("help", 1, "the top-level help declares no readable set of commands")
    return names


def _declared_options(help_text: str, origin: str) -> frozenset[str]:
    """Option names from the `options:` section, one per declaration line (an indented line that
    begins with a dash). Description lines are indented deeper and never begin with one."""
    declared: set[str] = set()
    in_options = False
    for text in help_text.splitlines():
        if text and not text[0].isspace():
            in_options = text.strip() == "options:"
            continue
        if in_options and text.startswith("  -"):
            declaration = re.split(r"\s{2,}", text.strip(), maxsplit=1)[0]
            declared.update(part.split()[0] for part in declaration.split(", "))
    if not declared:
        raise DocumentError("help", 1, f"the {origin} declares no options we can read")
    return frozenset(declared)


def validate_invocations(invocations: tuple[Invocation, ...], vocabulary: Vocabulary) -> None:
    """Every invocation names a command and options the vocabulary holds."""
    for invocation in invocations:
        arguments = list(invocation.argv[1:])
        position = 0
        while position < len(arguments) and arguments[position].startswith("-"):
            _require_option(invocation, arguments[position], vocabulary.top_level_options, None)
            position += 1
        if position == len(arguments):
            continue
        command = arguments[position]
        if command not in vocabulary.commands:
            raise DocumentError(
                invocation.source,
                invocation.line,
                f"{command!r} is not a {PRODUCT_NAME} command",
                token=command,
            )
        for word in arguments[position + 1 :]:
            if word == "--":
                break
            if word.startswith("-") and word != "-":
                _require_option(invocation, word, vocabulary.options[command], command)


def _require_option(
    invocation: Invocation, word: str, accepted: frozenset[str], command: str | None
) -> None:
    name = word.split("=", 1)[0] if word.startswith("--") else word
    if name not in accepted:
        where = f"{PRODUCT_NAME} {command}" if command else PRODUCT_NAME
        raise DocumentError(
            invocation.source, invocation.line, f"{name!r} is not an option of {where}", token=name
        )


# --- comparing a result ---------------------------------------------------------------------


def assert_example_result(example: Example, stdout: str, stderr: str, status: int) -> None:
    """The three assertions of CP-536: exit 0, nothing on standard error, and standard output equal
    to the block shown, character for character."""
    if status != 0:
        raise DocumentError(
            example.source, example.line, f"{example.command!r} exited {status}, not 0"
        )
    if stderr:
        raise DocumentError(
            example.source, example.line, f"{example.command!r} wrote to standard error: {stderr!r}"
        )
    if stdout != example.expected_stdout:
        difference = "\n".join(
            difflib.unified_diff(
                [repr(line) for line in example.expected_stdout.splitlines(keepends=True)],
                [repr(line) for line in stdout.splitlines(keepends=True)],
                fromfile="shown",
                tofile="printed",
                lineterm="",
                n=0,
            )
        )
        raise DocumentError(
            example.source,
            example.line,
            f"{example.command!r} prints something other than the block shown:\n{difference}",
        )
