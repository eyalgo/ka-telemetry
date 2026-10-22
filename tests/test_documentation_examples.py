"""Unit tests of the document reader that holds the README and the how-to to their examples.

The reader (`tests/e2e/documentation_examples.py`) is test support, not product code. These tests
give it text and explicit command results and read what it returns or refuses. A failing case is
driven through the reader's real validation, never through a second copy of its rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from e2e.documentation_examples import (
    DocumentError,
    assert_example_result,
    capture_vocabulary,
    command_vocabulary,
    parse_document,
    validate_document_set,
    validate_invocations,
)

REPO_ROOT = Path("/repo")
DECLARATION = (
    "Run this example from the top directory of a copy of this repository, "
    "the directory that holds the README.\n\n"
)
FENCE = "```"
TILDES = "~~~"


def block(body: str, label: str = "sh", fence: str = FENCE) -> str:
    return f"{fence}{label}\n{body}\n{fence}\n"


def command_with_output(command: str, output: str) -> str:
    return block(command) + "\n" + block(output, label="text")


class RecordingResolver:
    """Stands in for the real fixture resolver: records each call and answers from a table, or
    resolves a relative path against the repository root, `..` removed; nothing need exist."""

    def __init__(self, outcomes: dict[str, Path | None] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, repo_root: Path, value: str) -> Path | None:
        self.calls.append((repo_root, value))
        if value in self.outcomes:
            return self.outcomes[value]
        return (repo_root / value).resolve()


README_PAIR = command_with_output("ka-telemetry agents --dir tests/stream_parity", "2 rows\n")
HOW_TO_COMMANDS = {
    "agents": "ka-telemetry agents --dir tests/stream_parity",
    "check": "ka-telemetry check --dir tests/stream_parity",
    "runs": (
        "ka-telemetry runs --agents-json tests/parity/agents.json "
        "--projects-root tests/parity/projects"
    ),
    "steps": "ka-telemetry steps --projects-root tests/parity/projects",
}


def readme_text(*pairs: str) -> str:
    return "# ka-telemetry\n\n" + DECLARATION + "\n".join(pairs)


def how_to_text(without: str | None = None) -> str:
    pairs = [
        command_with_output(command, f"{report} output\n")
        for report, command in HOW_TO_COMMANDS.items()
        if report != without
    ]
    return "# How to\n\n" + DECLARATION + "\n".join(pairs)


def parsed(text: str, source: str = "README.md"):
    return parse_document(text, source)


def invocation_argvs(text: str) -> list[tuple[str, ...]]:
    return [invocation.argv for invocation in parsed(text).invocations]


# --- pairing and exact stdout -------------------------------------------------------------


def test_paired_example_carries_command_argv_cwd_and_exact_expected_stdout():
    text = (
        DECLARATION
        + block("ka-telemetry agents --dir tests/stream_parity")
        + "\n"
        + block("  first row\n\nsecond\nlast", label="text")
    )

    document = parsed(text)

    assert len(document.examples) == 1
    example = document.examples[0]
    assert example.source == "README.md"
    assert example.line == 3
    assert example.command == "ka-telemetry agents --dir tests/stream_parity"
    assert example.argv == ("ka-telemetry", "agents", "--dir", "tests/stream_parity")
    assert example.cwd == "."
    assert example.expected_stdout == "  first row\n\nsecond\nlast\n"


def test_output_block_keeps_a_trailing_blank_line_and_an_empty_output_is_empty():
    with_blank = DECLARATION + command_with_output("ka-telemetry agents", "row\n")
    empty = DECLARATION + block("ka-telemetry agents") + "\n" + "```text\n```\n"

    assert parsed(with_blank).examples[0].expected_stdout == "row\n\n"
    assert parsed(empty).examples[0].expected_stdout == ""


def test_declared_working_directory_is_read_for_each_example():
    text = (
        DECLARATION
        + command_with_output("ka-telemetry agents --dir tests/a", "one\n")
        + "\n"
        + command_with_output("ka-telemetry check --dir tests/a", "two\n")
    )

    document = parsed(text)

    assert [example.cwd for example in document.examples] == [".", "."]
    assert document.cwd == "."


def test_missing_working_directory_declaration_is_a_failure_with_source_line():
    text = "# Title\n\nSome prose.\n\n" + command_with_output("ka-telemetry agents", "row\n")

    with pytest.raises(DocumentError) as raised:
        parsed(text, "docs/how-to.md")

    assert raised.value.source == "docs/how-to.md"
    assert raised.value.line == 5
    assert "docs/how-to.md:5" in str(raised.value)


def test_a_declaration_after_an_example_does_not_cover_it():
    text = command_with_output("ka-telemetry agents", "row\n") + "\n" + DECLARATION

    with pytest.raises(DocumentError) as raised:
        parsed(text)

    assert raised.value.line == 1


def test_unclosed_backtick_fence_is_a_failure():
    text = "Intro.\n\n```sh\nka-telemetry agents\n"

    with pytest.raises(DocumentError) as raised:
        parsed(text)

    assert raised.value.line == 3
    assert "never closed" in str(raised.value)


def test_unclosed_tilde_fence_is_a_failure():
    text = "Intro.\n\n~~~~~\n# ka-telemetry\n~~~\n"

    with pytest.raises(DocumentError) as raised:
        parsed(text)

    assert raised.value.line == 3


def test_a_fence_is_closed_only_by_a_fence_as_long_and_of_its_own_kind():
    shorter = "````sh\nka-telemetry agents\n```\n"
    other_kind = "```sh\nka-telemetry agents\n~~~\n"

    for text in (shorter, other_kind):
        with pytest.raises(DocumentError):
            parsed(text)


def test_a_fence_left_open_before_the_next_block_opens_is_a_failure():
    """CP-538: a block whose closing fence is missing is never closed, even when a later bare fence
    would end it. The next block's opening fence, label and all, is where that shows."""
    text = (
        "Intro.\n\n"
        "```ini\n[Service]\nExecStart=/path/to/ka-telemetry receive --port 4318\n\n"
        "Enable it:\n\n"
        "```sh\nsystemctl --user daemon-reload\n```\n"
    )

    with pytest.raises(DocumentError) as raised:
        parsed(text, "docs/how-to.md")

    assert raised.value.source == "docs/how-to.md"
    assert raised.value.line == 3
    assert "never closed" in raised.value.message


def test_a_longer_fence_or_a_fence_of_the_other_kind_may_show_a_fence_line():
    """The control for the case above: a fence line inside a block opened by a longer fence, or by
    a fence of the other kind, is the block's content. (The label is one the reader does not scan
    as shell, which would refuse the backticks on their own.)"""
    longer = "````ini\n```sh\nRestart=on-failure\n```\n````\n"
    other_kind = "~~~ini\n```sh\nRestart=on-failure\n```\n~~~\n"

    for text in (longer, other_kind):
        document = parsed(text)
        assert len(document.blocks) == 1
        assert document.blocks[0].body == ("```sh", "Restart=on-failure", "```")


def test_output_block_with_no_command_is_a_failure():
    alone = DECLARATION + block("some output", label="text")
    after_prose = (
        DECLARATION
        + block("ka-telemetry agents --dir tests/a")
        + "\nA sentence between them.\n\n"
        + block("some output", label="text")
    )

    for text in (alone, after_prose):
        with pytest.raises(DocumentError):
            parsed(text)


def test_command_block_with_no_output_is_not_a_paired_example():
    text = (
        DECLARATION + block("ka-telemetry receive --port 4318") + "\nStart it, then read the log.\n"
    )

    document = parsed(text)

    assert document.examples == ()
    assert [invocation.argv for invocation in document.invocations] == [
        ("ka-telemetry", "receive", "--port", "4318")
    ]


def test_a_command_followed_directly_by_another_command_block_is_not_paired():
    text = (
        DECLARATION
        + block("ka-telemetry agents --dir tests/a")
        + "\n"
        + block("ka-telemetry check --dir tests/a")
    )

    assert parsed(text).examples == ()


def test_a_block_with_more_than_one_command_is_not_a_command_example():
    text = (
        DECLARATION
        + block("ka-telemetry agents --dir tests/a\nka-telemetry check --dir tests/a")
        + "\n"
        + block("output", label="text")
    )

    with pytest.raises(DocumentError):
        parsed(text)


# --- empty discovery ----------------------------------------------------------------------


def test_empty_document_set_cannot_pass():
    with pytest.raises(DocumentError):
        validate_document_set(
            parsed(""), parsed("", "docs/how-to.md"), REPO_ROOT, RecordingResolver()
        )


def test_a_document_with_text_and_no_examples_cannot_pass():
    readme = parsed("# ka-telemetry\n\nA sentence.\n")

    with pytest.raises(DocumentError):
        validate_document_set(
            readme, parsed("# How to\n", "docs/how-to.md"), REPO_ROOT, RecordingResolver()
        )


# --- scanning every block -----------------------------------------------------------------


@pytest.mark.parametrize("label", ["sh", "bash", "", "python", "console", "anything-else"])
def test_blocks_in_any_fence_language_are_scanned(label):
    text = block("ka-telemetry receive --prot 4318", label=label)

    assert invocation_argvs(text) == [("ka-telemetry", "receive", "--prot", "4318")]


def test_invocations_inventoried_in_multi_command_blocks_inline_spans_and_command_substitution():
    text = (
        block(
            "cd somewhere\n"
            "ka-telemetry agents --dir tests/a && ka-telemetry check --dir tests/a\n"
            'claude --settings "$(ka-telemetry turn-on --print)"\n'
            "ka-telemetry runs; ka-telemetry steps | head"
        )
        + "\nRun `ka-telemetry context transcript.jsonl` for one file, or\n"
        + "**`ka-telemetry agents` and `ka-telemetry check`** together.\n"
    )

    document = parsed(text)

    assert [invocation.argv for invocation in document.invocations] == [
        ("ka-telemetry", "agents", "--dir", "tests/a"),
        ("ka-telemetry", "check", "--dir", "tests/a"),
        ("ka-telemetry", "turn-on", "--print"),
        ("ka-telemetry", "runs"),
        ("ka-telemetry", "steps"),
        ("ka-telemetry", "context", "transcript.jsonl"),
        ("ka-telemetry", "agents"),
        ("ka-telemetry", "check"),
    ]
    assert [invocation.line for invocation in document.invocations] == [3, 3, 4, 5, 5, 8, 9, 9]
    contexts = {invocation.context for invocation in document.invocations}
    assert contexts == {"shell block", "inline code span"}


def test_invocation_in_displayed_output_is_inventoried():
    output = (
        "Totals check:\n"
        "  receiver (ka-telemetry receive) may not have been running; "
        "run `ka-telemetry agents --json`\n"
        "ka-telemetry cannot tell these apart, and ka-telemetry never computes a cost.\n"
    )
    text = DECLARATION + command_with_output("ka-telemetry check --dir tests/a", output)

    document = parsed(text)

    output_sites = [
        invocation for invocation in document.invocations if invocation.context == "output text"
    ]
    assert [site.argv for site in output_sites] == [
        ("ka-telemetry", "receive"),
        ("ka-telemetry", "agents", "--json"),
    ]
    assert [site.line for site in output_sites] == [9, 9]


def test_line_continuations_and_quoted_paths_stay_one_invocation():
    text = block(
        "ka-telemetry agents \\\n  --dir 'tests/my dir' \\\n  --where \"session.id=abc\" --json"
    )

    document = parsed(text)

    assert [invocation.argv for invocation in document.invocations] == [
        (
            "ka-telemetry",
            "agents",
            "--dir",
            "tests/my dir",
            "--where",
            "session.id=abc",
            "--json",
        )
    ]
    assert document.invocations[0].line == 2


def test_launchd_program_arguments_and_systemd_command_values_are_decoded_as_their_own_formats():
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "  <key>Label</key>\n  <string>ka-telemetry.receiver</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        "    <string>/path/to/ka-telemetry</string>\n    <string>receive</string>\n"
        "    <string>--port</string>\n    <string>4318</string>\n"
        "  </array>\n</dict>\n</plist>"
    )
    unit = (
        "[Unit]\nDescription=ka-telemetry receiver\n\n[Service]\n"
        "ExecStart=/path/to/ka-telemetry receive --port 4318 "
        "--publish-endpoint %h/.claude/tools/otel-receiver/endpoint\n"
        "Restart=on-failure\n"
    )

    document = parsed(block(plist, label="xml") + "\n" + block(unit, label="ini"))

    sites = {invocation.context: invocation for invocation in document.invocations}
    assert sites["launchd ProgramArguments"].argv == (
        "/path/to/ka-telemetry",
        "receive",
        "--port",
        "4318",
    )
    assert sites["systemd command"].argv == (
        "/path/to/ka-telemetry",
        "receive",
        "--port",
        "4318",
        "--publish-endpoint",
        "%h/.claude/tools/otel-receiver/endpoint",
    )
    assert len(document.invocations) == 2


def test_a_launchd_array_for_another_program_is_not_an_invocation():
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
        "  <key>ProgramArguments</key>\n  <array>\n    <string>/bin/echo</string>\n"
        "  </array>\n</dict>\n</plist>"
    )

    assert parsed(block(plist, label="xml")).invocations == ()


def test_malformed_service_file_formats_fail_with_a_location():
    broken_plist = block("<plist><dict>", label="xml")
    broken_unit = block("ExecStart=/path/to/ka-telemetry receive 'unterminated", label="ini")

    for text in (broken_plist, broken_unit):
        with pytest.raises(DocumentError) as raised:
            parsed(text)
        assert raised.value.line >= 1


@pytest.mark.parametrize(
    "unsupported",
    [
        "time ka-telemetry agents --dir tests/a",
        "xargs ka-telemetry agents",
        "echo `ka-telemetry agents`",
        "ka-telemetry agents <<EOF\nEOF",
        "echo $(ka-telemetry agents",
        "diff <(ka-telemetry agents) other",
        "echo $((1 + 1))",
        "(ka-telemetry agents)",
        "ka-telemetry agents 'unterminated",
    ],
)
def test_unsupported_invocation_form_fails_with_a_location(unsupported):
    text = "Intro.\n\n" + block(unsupported)

    with pytest.raises(DocumentError) as raised:
        parsed(text, "docs/how-to.md")

    assert raised.value.source == "docs/how-to.md"
    assert raised.value.line >= 4


def test_product_name_mention_without_invocation_is_not_a_command():
    text = (
        "ka-telemetry cannot tell these apart, and `ka-telemetry` is the name.\n"
        "The directory `~/.ka-telemetry` and the label `ka-telemetry.receiver`.\n\n"
        + block('command -v ka-telemetry\necho "$HOME"')
        + "\n"
        + block("systemctl --user status ka-telemetry-receiver.service")
    )

    assert parsed(text).invocations == ()


def test_the_install_command_is_inventoried_as_an_install_and_not_as_an_invocation():
    document = parsed(block("python3 -m pip install ka-telemetry"))

    assert document.invocations == ()
    assert [install.argv for install in document.installs] == [
        ("python3", "-m", "pip", "install", "ka-telemetry")
    ]
    assert document.installs[0].line == 2


def test_pipx_install_is_inventoried_as_an_install():
    document = parsed("Intro.\n\n" + block("pipx install ka-telemetry"))

    assert document.invocations == ()
    assert [(install.argv, install.line) for install in document.installs] == [
        (("pipx", "install", "ka-telemetry"), 4)
    ]


def test_pipx_install_with_python_option_is_an_install():
    document = parsed(block("pipx install --python python3.12 ka-telemetry"))

    assert document.invocations == ()
    assert [install.argv for install in document.installs] == [
        ("pipx", "install", "--python", "python3.12", "ka-telemetry")
    ]


def test_pipx_install_in_an_inline_span_is_an_install():
    text = (
        "Run `pipx install ka-telemetry` first, or, on an older Python,\n"
        "`pipx install --python python3.12 ka-telemetry`.\n"
    )

    document = parsed(text)

    assert document.invocations == ()
    assert [(install.argv, install.line) for install in document.installs] == [
        (("pipx", "install", "ka-telemetry"), 1),
        (("pipx", "install", "--python", "python3.12", "ka-telemetry"), 2),
    ]


def test_pip_install_stays_an_install():
    text = block("python3 -m pip install ka-telemetry") + "\nUse `pip3 install ka-telemetry`.\n"

    document = parsed(text)

    assert [install.argv for install in document.installs] == [
        ("python3", "-m", "pip", "install", "ka-telemetry"),
        ("pip3", "install", "ka-telemetry"),
    ]


def test_brew_apt_dnf_and_ensurepath_are_neither_installs_nor_invocations():
    text = block(
        "brew install pipx\nsudo apt update\nsudo apt install pipx\nsudo dnf install pipx\n"
        "pipx ensurepath"
    ) + "\nTry `brew install pipx`, then `pipx ensurepath`.\n"

    document = parsed(text)

    assert document.installs == ()
    assert document.invocations == ()


@pytest.mark.parametrize(
    "unread",
    [
        "uv tool install ka-telemetry",
        "pipx run ka-telemetry agents",
        "pipx upgrade ka-telemetry",
        "pipx reinstall ka-telemetry",
    ],
)
def test_any_other_unread_form_of_running_ka_telemetry_still_fails(unread):
    with pytest.raises(DocumentError) as raised:
        parsed("Intro.\n\n" + block(unread))

    assert raised.value.line == 4


# --- required counts and coverage ---------------------------------------------------------


def valid_set():
    return parsed(readme_text(README_PAIR)), parsed(how_to_text(), "docs/how-to.md")


def test_a_valid_document_set_passes():
    readme, how_to = valid_set()

    validate_document_set(readme, how_to, REPO_ROOT, RecordingResolver())


def test_required_counts_exactly_one_readme_example_using_agents():
    how_to = parsed(how_to_text(), "docs/how-to.md")
    two = parsed(readme_text(README_PAIR, README_PAIR))
    other_report = parsed(
        readme_text(command_with_output("ka-telemetry check --dir tests/stream_parity", "x\n"))
    )
    none = parsed(readme_text())

    for readme in (two, other_report, none):
        with pytest.raises(DocumentError) as raised:
            validate_document_set(readme, how_to, REPO_ROOT, RecordingResolver())
        assert raised.value.source == "README.md"


@pytest.mark.parametrize("missing", ["agents", "check", "runs", "steps"])
def test_how_to_must_cover_all_four_reports(missing):
    readme = parsed(readme_text(README_PAIR))
    how_to = parsed(how_to_text(without=missing), "docs/how-to.md")

    with pytest.raises(DocumentError) as raised:
        validate_document_set(readme, how_to, REPO_ROOT, RecordingResolver())

    assert raised.value.source == "docs/how-to.md"
    assert missing in str(raised.value)


# --- fixture confinement ------------------------------------------------------------------


def readme_for(command: str):
    return parsed(readme_text(command_with_output(command, "row\n")))


def test_fixture_path_outside_tests_is_rejected():
    how_to = parsed(how_to_text(), "docs/how-to.md")
    absolute = readme_for("ka-telemetry agents --dir /tmp/live-data")
    escape = readme_for("ka-telemetry agents --dir tests/../elsewhere")
    symlink = readme_for("ka-telemetry agents --dir tests/link")
    resolver = RecordingResolver({"tests/link": Path("/somewhere/else/data")})

    for readme in (absolute, escape, symlink):
        with pytest.raises(DocumentError) as raised:
            validate_document_set(readme, how_to, REPO_ROOT, resolver)
        assert "tests" in str(raised.value)


def test_an_absolute_fixture_path_is_refused_before_the_resolver_is_asked():
    resolver = RecordingResolver()

    with pytest.raises(DocumentError):
        validate_document_set(
            readme_for("ka-telemetry agents --dir=/tmp/live-data"),
            parsed(how_to_text(), "docs/how-to.md"),
            REPO_ROOT,
            resolver,
        )

    assert resolver.calls == []


def test_nonexistent_fixture_path_is_rejected():
    resolver = RecordingResolver({"tests/missing": None})

    with pytest.raises(DocumentError) as raised:
        validate_document_set(
            readme_for("ka-telemetry agents --dir tests/missing"),
            parsed(how_to_text(), "docs/how-to.md"),
            REPO_ROOT,
            resolver,
        )

    assert "tests/missing" in str(raised.value)


def test_each_fixture_path_an_example_names_is_resolved_from_the_repository_root():
    readme, how_to = valid_set()
    resolver = RecordingResolver()

    validate_document_set(readme, how_to, REPO_ROOT, resolver)

    named = {value for _, value in resolver.calls}
    assert named == {
        "tests/stream_parity",
        "tests/parity/agents.json",
        "tests/parity/projects",
    }
    assert {root for root, _ in resolver.calls} == {REPO_ROOT}


def test_an_example_that_names_no_fixture_would_read_live_data_and_is_rejected():
    for command in (
        "ka-telemetry agents",
        "ka-telemetry runs --agents-json tests/parity/agents.json",
        "ka-telemetry steps",
    ):
        with pytest.raises(DocumentError):
            validate_document_set(
                readme_for(command),
                parsed(how_to_text(), "docs/how-to.md"),
                REPO_ROOT,
                RecordingResolver(),
            )


# --- the vocabulary -----------------------------------------------------------------------

TOP_HELP = """\
usage: ka-telemetry [-h] {receive,turn-on,context} ...

Reads the transcripts Claude Code keeps. Pass --settings to claude, not here.

options:
  -h, --help            show this help message and exit

commands:
  {receive,turn-on,context}
    receive             receive the telemetry Claude Code sends
    turn-on             write the keys that make Claude Code send it
    context             the current context size of one transcript
"""
RECEIVE_HELP = """\
usage: ka-telemetry receive [-h] --port PORT [--dir DIR] [--host HOST]
                            [--publish-endpoint PATH]

Receives the telemetry. Use claude --settings with turn-on, not --verbose here.

options:
  -h, --help            show this help message and exit
  --port PORT           the TCP port to listen on (required); 0 lets the
                        system choose a free port
  --dir DIR             the directory to write in
  --publish-endpoint PATH
                        write http://localhost:PORT to the file PATH
"""
TURN_ON_HELP = """\
usage: ka-telemetry turn-on [-h] [--print]

prints on stdout the JSON that claude --settings accepts, and its notices on
stderr.

options:
  -h, --help            show this help message and exit
  --print               print the five keys for one run as the JSON that
                        claude --settings accepts, instead of writing a file
"""
CONTEXT_HELP = """\
usage: ka-telemetry context [-h] [--json] TRANSCRIPT

positional arguments:
  TRANSCRIPT  path to a transcript file

options:
  -h, --help  show this help message and exit
  --json      print the result as JSON
"""
HELP_BY_COMMAND = {
    "ka-telemetry": TOP_HELP,
    "receive": RECEIVE_HELP,
    "turn-on": TURN_ON_HELP,
    "context": CONTEXT_HELP,
}


def test_vocabulary_is_taken_from_option_declarations():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    assert vocabulary.commands == frozenset({"receive", "turn-on", "context"})
    assert vocabulary.options["receive"] == frozenset(
        {"-h", "--help", "--port", "--dir", "--publish-endpoint"}
    )
    assert vocabulary.options["context"] == frozenset({"-h", "--help", "--json"})
    assert vocabulary.top_level_options == frozenset({"-h", "--help"})


def test_option_names_come_only_from_declarations_not_help_prose():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    assert "--settings" not in vocabulary.options["turn-on"]
    assert vocabulary.options["turn-on"] == frozenset({"-h", "--help", "--print"})
    assert "--verbose" not in vocabulary.options["receive"]


def test_vocabulary_is_captured_by_asking_for_the_top_level_help_then_each_command():
    asked: list[tuple[str, ...]] = []

    def read_help(arguments: tuple[str, ...]) -> str:
        asked.append(arguments)
        return HELP_BY_COMMAND[arguments[0] if arguments else "ka-telemetry"]

    vocabulary = capture_vocabulary(read_help)

    assert asked[0] == ()
    assert sorted(asked[1:]) == [("context",), ("receive",), ("turn-on",)]
    assert vocabulary == command_vocabulary(HELP_BY_COMMAND)


@pytest.mark.parametrize(
    "broken",
    [
        {"ka-telemetry": "not help at all"},
        {**HELP_BY_COMMAND, "ka-telemetry": TOP_HELP.replace("{receive,turn-on,context}", "")},
        {**HELP_BY_COMMAND, "receive": "usage: ka-telemetry receive\n\nno options section\n"},
        {key: value for key, value in HELP_BY_COMMAND.items() if key != "context"},
    ],
)
def test_unreadable_help_structure_is_an_error_not_an_empty_vocabulary(broken):
    with pytest.raises(DocumentError):
        command_vocabulary(broken)


def invocations_of(command: str):
    return parsed(block(command), "docs/how-to.md").invocations


def test_known_commands_and_options_are_accepted():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)
    commands = [
        "ka-telemetry receive --port 4318 --publish-endpoint x",
        "ka-telemetry turn-on --print",
        "ka-telemetry context --json transcript.jsonl",
        "ka-telemetry --help",
        "ka-telemetry",
        "/path/to/ka-telemetry receive --port=4318",
    ]

    for command in commands:
        validate_invocations(invocations_of(command), vocabulary)


def test_unknown_command_is_rejected_with_source_line_and_token():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)
    text = "Intro.\n\n" + block("ka-telemetry reciev --port 4318")

    with pytest.raises(DocumentError) as raised:
        validate_invocations(parsed(text, "docs/how-to.md").invocations, vocabulary)

    assert raised.value.source == "docs/how-to.md"
    assert raised.value.line == 4
    assert raised.value.token == "reciev"


def test_unknown_option_is_rejected():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    with pytest.raises(DocumentError) as raised:
        validate_invocations(invocations_of("ka-telemetry receive --prot 4318"), vocabulary)

    assert raised.value.token == "--prot"


def test_an_option_of_another_command_is_not_accepted():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    with pytest.raises(DocumentError) as raised:
        validate_invocations(invocations_of("ka-telemetry turn-on --port 4318"), vocabulary)

    assert raised.value.token == "--port"


def test_equals_form_option_is_checked():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    validate_invocations(invocations_of("ka-telemetry receive --port=4318"), vocabulary)
    with pytest.raises(DocumentError) as raised:
        validate_invocations(invocations_of("ka-telemetry receive --prot=4318"), vocabulary)

    assert raised.value.token == "--prot"


def test_an_unknown_top_level_option_is_rejected_and_values_are_not_options():
    vocabulary = command_vocabulary(HELP_BY_COMMAND)

    with pytest.raises(DocumentError):
        validate_invocations(invocations_of("ka-telemetry --bogus receive"), vocabulary)
    validate_invocations(
        invocations_of("ka-telemetry receive --dir ~/.ka-telemetry --port 4318"), vocabulary
    )


# --- comparing a result -------------------------------------------------------------------


def one_example(expected: str = "row one\n  row two\n"):
    text = DECLARATION + command_with_output(
        "ka-telemetry agents --dir tests/a", expected.rstrip("\n")
    )
    return parsed(text).examples[0]


def test_matching_stdout_with_exit_0_and_empty_stderr_passes():
    assert_example_result(one_example(), "row one\n  row two\n", "", 0)


def test_matching_stdout_with_exit_1_fails():
    with pytest.raises(DocumentError) as raised:
        assert_example_result(one_example(), "row one\n  row two\n", "", 1)

    assert raised.value.source == "README.md"
    assert raised.value.line == 3


def test_matching_stdout_with_nonempty_stderr_fails():
    with pytest.raises(DocumentError):
        assert_example_result(one_example(), "row one\n  row two\n", "warning\n", 0)


def test_one_character_difference_in_stdout_fails():
    with pytest.raises(DocumentError) as raised:
        assert_example_result(one_example(), "row one\n  row twp\n", "", 0)

    assert "row tw" in str(raised.value)


def test_trailing_newline_difference_fails():
    with pytest.raises(DocumentError):
        assert_example_result(one_example(), "row one\n  row two", "", 0)
    with pytest.raises(DocumentError):
        assert_example_result(one_example(), "row one\n  row two\n\n", "", 0)


def test_leading_space_and_blank_line_differences_fail():
    with pytest.raises(DocumentError):
        assert_example_result(one_example(), "row one\nrow two\n", "", 0)
    with pytest.raises(DocumentError):
        assert_example_result(one_example(), "row one\n\n  row two\n", "", 0)
