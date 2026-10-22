"""End-to-end checks that the README and the how-to say what the installed command does.

The documents are the real ones, read from the checkout. Every command example in them is run
through the installed `ka-telemetry` (the scratch virtualenv's `bin` first on `PATH`, so the
documented spelling runs unchanged) from the directory the documents declare, and its output is
compared with the block shown, character for character. Every other place the documents show the
command being run is checked against the commands and options the installed help declares. The
documented setup runs in scratch homes, against a receiver this test starts and a recording
stand-in for `claude`; nothing here touches real settings, starts a model or installs a service.

Parsing and comparing are the reader's (`tests/e2e/documentation_examples.py`). A failing path is
driven through the same validation, on text copies; the product documents are never edited.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.documentation_examples import (
    LAUNCHD_ARGUMENTS,
    PRODUCT_NAME,
    REPORTS,
    SYSTEMD_COMMAND,
    DocumentError,
    Example,
    ParsedDocument,
    Vocabulary,
    assert_example_result,
    capture_vocabulary,
    parse_document,
    validate_document_set,
    validate_invocations,
)
from e2e.support import (
    PRODUCT_ROOT,
    README_BASE_COMMIT,
    BaselineMissing,
    Receiver,
    base_readme,
    child_env,
)

if TYPE_CHECKING:
    from conftest import InstalledProduct

README = PRODUCT_ROOT / "README.md"
HOW_TO = PRODUCT_ROOT / "docs" / "how-to.md"
FORBIDDEN_LOCATIONS = ("ka_telemetry", "tests", "docs/trail")
FIVE_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
EVERY_RUN_HEADING = "## Turn telemetry on for every Claude Code run"
ENDPOINT_PATH = "~/.claude/tools/otel-receiver/endpoint"
SHELL = "sh"
RECORD_VARIABLE = "KA_RECORDED_ARGUMENTS"
RECORDING_CLAUDE = f'#!/bin/sh\nprintf \'%s\\0\' "$@" > "${RECORD_VARIABLE}"\n'
NOTHING_WRITTEN = "nothing written"


def read_documents() -> tuple[str, str]:
    return README.read_text(encoding="utf-8"), HOW_TO.read_text(encoding="utf-8")


def resolve_fixture(base: Path, value: str) -> Path | None:
    path = base / value
    return path.resolve() if path.exists() else None


def parsed_pair() -> tuple[ParsedDocument, ParsedDocument]:
    readme_text, how_to_text = read_documents()
    return parse_document(readme_text, README), parse_document(how_to_text, HOW_TO)


def validate_set(readme_text: str, how_to_text: str) -> None:
    validate_document_set(
        parse_document(readme_text, README),
        parse_document(how_to_text, HOW_TO),
        PRODUCT_ROOT,
        resolve_fixture,
    )


def environment(installed: InstalledProduct, home: Path, *extra_path: Path) -> dict[str, str]:
    """A child's environment: its own `HOME`, and the installed command found first on `PATH`."""
    env = child_env(home)
    directories = [*map(str, extra_path), str(installed.venv / "bin"), env.get("PATH", "")]
    env["PATH"] = os.pathsep.join(directories)
    return env


def run_example(installed: InstalledProduct, example: Example, home: Path) -> Example:
    """Runs an example's argv unchanged from its declared directory, and checks it against the
    block shown. Raises the reader's error when the two differ."""
    done = subprocess.run(
        list(example.argv),
        capture_output=True,
        text=True,
        check=False,
        env=environment(installed, home),
        cwd=PRODUCT_ROOT / example.cwd,
    )
    assert_example_result(example, done.stdout, done.stderr, done.returncode)
    return example


def run_shell(text: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Documented shell text, run by `sh` exactly as a reader would paste it."""
    return subprocess.run(
        [SHELL, "-c", text], capture_output=True, text=True, check=False, env=env, cwd=cwd
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A scratch home that holds Claude's settings directory and nothing else."""
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


@pytest.fixture(scope="module")
def vocabulary(installed: InstalledProduct, tmp_path_factory: pytest.TempPathFactory) -> Vocabulary:
    scratch = tmp_path_factory.mktemp("help-home")

    def read_help(words: tuple[str, ...]) -> str:
        done = subprocess.run(
            [str(installed.command), *words, "--help"],
            capture_output=True,
            text=True,
            check=True,
            env=child_env(scratch),
        )
        return done.stdout

    return capture_vocabulary(read_help)


# --- the examples -------------------------------------------------------------------------------


def test_every_paired_example_in_both_documents_prints_exactly_its_shown_output(
    installed: InstalledProduct, home: Path
) -> None:
    readme, how_to = parsed_pair()
    examples = [*readme.examples, *how_to.examples]
    assert len(examples) == 5, "one README example and four how-to examples"
    for example in examples:
        run_example(installed, example, home)


def test_readme_holds_exactly_one_pair_and_it_is_agents() -> None:
    readme, how_to = parsed_pair()
    assert [example.argv[1] for example in readme.examples] == ["agents"]
    validate_document_set(readme, how_to, PRODUCT_ROOT, resolve_fixture)


def test_how_to_holds_a_pair_for_each_of_the_four_reports() -> None:
    _, how_to = parsed_pair()
    assert sorted(example.argv[1] for example in how_to.examples) == sorted(REPORTS)


def test_every_other_invocation_names_known_commands_and_options(vocabulary: Vocabulary) -> None:
    readme, how_to = parsed_pair()
    sites = (*readme.invocations, *how_to.invocations)
    validate_invocations(sites, vocabulary)
    contexts = {site.context for site in sites}
    assert {"inline code span", LAUNCHD_ARGUMENTS, SYSTEMD_COMMAND} <= contexts
    assert any(site.argv[1:3] == ("turn-on", "--print") for site in sites), "nested substitution"


def test_each_child_runs_in_an_isolated_scratch_home(
    installed: InstalledProduct, home: Path
) -> None:
    env = environment(installed, home)
    assert env["HOME"] == str(home)
    assert Path(env["HOME"]) != Path.home()
    # `steps` with no option looks under the home's own `.claude/projects`: with this home it
    # finds nothing, so the owner's workflow runs cannot appear in any documented output.
    done = subprocess.run(
        [str(installed.command), "steps"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=PRODUCT_ROOT,
    )
    assert "No workflow runs read." in done.stdout


# --- what the README says -----------------------------------------------------------------------


INSTALL_HEADING = "## Install"
RELEASE_HEADING = "## Check a release"
STATUS_HEADING = "## Status"
OLDER_PYTHON_PASSAGE = "If the Python pipx installs with is older than 3.11"
# The paragraph as the change's published text gives it. It fences wording no check below reviews;
# changing it does not get past `passage_problems`, which holds every claim against the captures.
OLDER_PYTHON_TEXT = (
    "If the Python pipx installs with is older than 3.11, the install does one of two things. "
    "It stops with an error and installs nothing, or it installs ka-telemetry with a Python 3.11 "
    "or later that pipx chose instead, and says which Python it used. Either way, what pipx "
    "prints names ka-telemetry's Python requirement, `>=3.11`, and the rest of its wording "
    "differs from one pipx setup to another. If the install stopped, install Python 3.11 or "
    "later, then give it to pipx with `--python`, putting the name or the path of the Python you "
    "installed in place of `python3.12`:"
)
LIBRARY_HEADING = "## Use the library in your own code"
TRY_IT_HEADING = "## Try it"
BUILT_BY_HEADING = "## Built by KensaiApp"
SUPPORT_HEADING = "## Support"
VERSIONS_HEADING = "## Versions and the public API"
CANONICAL = ("pipx", "install", PRODUCT_NAME)
RECOVERY = ("pipx", "install", "--python", "python3.12", PRODUCT_NAME)
LIBRARY_PIP = ("python3", "-m", "pip", "install", PRODUCT_NAME)


def sections(text: str) -> dict[str, str]:
    """A document's level-two sections by heading line, each with its heading; the text before the
    first heading is under the empty key. A heading inside a fence is not a heading."""
    found: dict[str, list[str]] = {"": []}
    heading, fenced = "", False
    for line in text.splitlines(keepends=True):
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            heading = line.rstrip("\n")
            found[heading] = []
        found[heading].append(line)
    return {name: "".join(body) for name, body in found.items()}


def readme_section(heading: str) -> str:
    return sections(README.read_text(encoding="utf-8"))[heading]


# --- the older-Python passage, held against the recorded pipx runs -----------------------------

CAPTURES = Path(__file__).with_name("pipx_install_captures.json")
OLDEST_SUPPORTED = (3, 11)
WRITE_LOCATIONS = (
    "PIPX_HOME",
    "PIPX_BIN_DIR",
    "PIPX_MAN_DIR",
    "PIPX_SHARED_LIBS",
    "PIPX_COMPLETION_DIR",
    "HOME",
    "TMPDIR",
)
OTHER_INDEXES = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")
STOPPED, INSTALLED = "stopped", "installed"
WHICH_PYTHON = "installed using Python"
# Each outcome claim the passage may make. A sentence about the outcome or the output that matches
# none of them is refused, so a new claim has to be given its check here before it can be printed.
ONE_OF_TWO = re.compile(r"the install does one of two things\.")
BOTH_OUTCOMES = re.compile(
    r"It stops with an error and installs nothing, or it installs ka-telemetry with a Python "
    r"3\.11 or later that pipx chose instead, and says which Python it used\."
)
REQUIREMENT_PRINTED = re.compile(
    r"what pipx prints names ka-telemetry's Python requirement, `>=3\.11`"
)
ALWAYS_STOPS = re.compile(r"the install stops with an error and installs nothing\.")
ERROR_NAMES_REQUIREMENT = re.compile(
    r"The error names ka-telemetry's Python requirement, `>=3\.11`"
)
SCOPED_OUTCOME = re.compile(
    r"With pipx on Python (\d+\.\d+), the install "
    r"(stops with an error and installs nothing|installs ka-telemetry)\."
)
RECOVERY_SENTENCE = re.compile(r"^If the install stopped, install Python 3\.11 or later, ")
OUTCOME_WORDS = re.compile(
    r"(?i)\b(stops?|stopped|installs|installed|chose|used|prints?|printed|errors?)\b"
)
QUOTED = re.compile(r"\u201c([^\u201d]+)\u201d|\"([^\"]+)\"")


def load_captures() -> dict:
    return json.loads(CAPTURES.read_text(encoding="utf-8"))


def version_of(text: str) -> tuple[int, ...]:
    found = re.search(r"\d+(?:\.\d+)+", text or "")
    return tuple(int(part) for part in found.group().split(".")) if found else ()


def streams(run: dict) -> str:
    return run["stdout"] + "\n" + run["stderr"]


def outcome_of(run: dict) -> str | None:
    """`stopped` when the install failed and left ka-telemetry out; `installed` when it succeeded
    and left ka-telemetry working on a Python 3.11 or later; otherwise neither."""
    state = run["installed_state"]
    if run["exit_status"] != 0 and not state["present"]:
        return STOPPED
    installed_python = version_of(state.get("python") or "")
    if run["exit_status"] == 0 and state["present"] and installed_python >= OLDEST_SUPPORTED:
        return INSTALLED
    return None


def capture_problems(data: dict) -> set[str]:
    """What is wrong with the recorded runs, by name; empty when they hold the two CP-620 cases."""
    runs = [run for run in data.get("runs", []) if not run.get("preparation_failure")]
    if not runs:
        return {"no-runs"}
    problems: set[str] = set()
    pipx_on = [version_of(run.get("pipx_python", {}).get("version", "")) for run in runs]
    older_default = [
        version_of(run.get("default_install_python", {}).get("version", "")) < OLDEST_SUPPORTED
        for run in runs
    ]
    if not any(version[:2] == (3, 9) for version in pipx_on):
        problems.add("missing-case-pipx-on-3.9")
    if not any(
        version >= OLDEST_SUPPORTED and older for version, older in zip(pipx_on, older_default)
    ):
        problems.add("missing-case-newer-pipx-older-default")
    for run in runs:
        problems |= run_problems(run)
    return problems


def field_of(run: dict, path: tuple[str, ...]) -> object | None:
    node: object = run
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def run_problems(run: dict) -> set[str]:
    problems: set[str] = set()
    for path in (
        ("pipx", "version"),
        ("pipx_python", "version"),
        ("default_install_python", "version"),
        ("exit_status",),
        ("stdout",),
        ("stderr",),
        ("installed_state", "present"),
        ("scratch_root",),
        ("settings",),
    ):
        value = field_of(run, path)
        # An empty stream is a real answer; an empty version or path is not.
        if value is None or (value == "" and path[0] not in ("stdout", "stderr")):
            problems.add("missing-field:" + ".".join(path))
    if problems:
        return problems
    state = run["installed_state"]
    if (run["exit_status"] == 0 and not state["present"]) or (
        run["exit_status"] != 0 and state["present"] and state.get("working")
    ):
        problems.add("install-observation-contradicts-exit-status")
    scratch = Path(run["scratch_root"])
    settings = run["settings"]
    locations = [settings.get(name) for name in WRITE_LOCATIONS]
    if any(value is None or not Path(value).is_relative_to(scratch) for value in locations):
        problems.add("write-location-outside-scratch-root")
    wheels = settings.get("PIP_FIND_LINKS")
    if (
        settings.get("PIP_NO_INDEX") != "1"
        or any(name in settings for name in OTHER_INDEXES)
        or wheels is None
        or not Path(wheels).is_relative_to(scratch)
    ):
        problems.add("index-setting-is-not-the-local-wheel-directory")
    return problems


def passage_problems(passage: str, data: dict) -> set[str]:
    """What the older-Python passage says about the outcome or the output that the recorded runs
    contradict, by claim; empty when every claim is true of the runs it covers."""
    runs = [run for run in data["runs"] if not run.get("preparation_failure")]
    outcomes = [outcome_of(run) for run in runs]
    problems: set[str] = set()
    for sentence in re.split(r"(?<=[.:])\s+", passage.strip()):
        if ONE_OF_TWO.search(sentence) and None in outcomes:
            problems.add("a-run-is-neither-outcome")
        if BOTH_OUTCOMES.search(sentence) and not {STOPPED, INSTALLED} <= set(outcomes):
            problems.add("an-outcome-no-run-shows")
        if BOTH_OUTCOMES.search(sentence) and any(
            WHICH_PYTHON not in streams(run)
            for run, outcome in zip(runs, outcomes)
            if outcome == INSTALLED
        ):
            problems.add("an-installing-run-does-not-say-which-python")
        if REQUIREMENT_PRINTED.search(sentence) and any(
            ">=3.11" not in streams(run) for run in runs
        ):
            problems.add("requirement-not-printed-by-every-run")
        scoped = SCOPED_OUTCOME.search(sentence)
        if (
            ALWAYS_STOPS.search(sentence)
            and not scoped
            and any(outcome != STOPPED for outcome in outcomes)
        ):
            problems.add("not-every-run-stopped-and-installed-nothing")
        if ERROR_NAMES_REQUIREMENT.search(sentence) and any(
            ">=3.11" not in streams(run)
            for run, outcome in zip(runs, outcomes)
            if outcome != INSTALLED
        ):
            problems.add("a-failed-run-does-not-name-the-requirement")
        if scoped:
            wanted = STOPPED if scoped.group(2).startswith("stops") else INSTALLED
            covered = [
                outcome
                for run, outcome in zip(runs, outcomes)
                if version_of(run["pipx_python"]["version"])[:2] == version_of(scoped.group(1))
            ]
            if not covered or any(outcome != wanted for outcome in covered):
                problems.add("a-setup-claim-its-runs-contradict")
        known = any(
            pattern.search(sentence)
            for pattern in (
                ONE_OF_TWO,
                BOTH_OUTCOMES,
                REQUIREMENT_PRINTED,
                ALWAYS_STOPS,
                ERROR_NAMES_REQUIREMENT,
                SCOPED_OUTCOME,
                RECOVERY_SENTENCE,
            )
        )
        if not known and OUTCOME_WORDS.search(sentence):
            problems.add("unrecognised-outcome-claim")
    everything = "".join(streams(run) for run in runs)
    for quote in QUOTED.findall(passage):
        if (quote[0] or quote[1]) not in everything:
            problems.add("quoted-message-no-run-printed")
    return problems


def older_python_passage() -> str:
    install = readme_section(INSTALL_HEADING)
    start = install.index(OLDER_PYTHON_PASSAGE)
    return install[start : install.index("\n\n", start)]


def test_readme_install_section_first_block_is_exactly_the_canonical_line() -> None:
    section = readme_section(INSTALL_HEADING)
    [first, *_] = re.findall(r"```sh\n(.*?)```", section, re.S)
    assert first == "pipx install ka-telemetry\n"
    assert parse_document(section, README).blocks[0].body == ("pipx install ka-telemetry",)


def test_only_the_recovery_form_is_a_second_install_inside_install() -> None:
    inside = parse_document(readme_section(INSTALL_HEADING), README)
    assert [site.argv for site in inside.installs] == [CANONICAL, RECOVERY]
    # Status is the `readme-status` run's text and quotes the canonical command in prose; it
    # installs nothing, so the inventory is counted over the rest of the README.
    text = README.read_text(encoding="utf-8")
    outside_status = "".join(
        body for name, body in sections(text).items() if name != STATUS_HEADING
    )
    whole = parse_document(outside_status, README)
    assert [site.argv for site in whole.installs] == [CANONICAL, RECOVERY, LIBRARY_PIP]


def test_install_section_gives_homebrew_a_package_command_ensurepath_and_a_new_terminal() -> None:
    section = readme_section(INSTALL_HEADING)
    assert "brew install pipx" in section and "Homebrew" in section
    assert "Ubuntu 23.04 or later" in section and "sudo apt install pipx" in section
    assert "distribution's own package" in section, "the Linux command is the distribution's own"
    assert "dnf" not in section and "yum" not in section, "no command for a distribution not named"
    assert "pipx ensurepath" in section and "new terminal" in section
    commands = [site.argv for site in parse_document(section, README).installs]
    assert ("brew", "install", "pipx") not in commands, "getting pipx is not an install here"


def test_install_section_states_3_11_the_older_python_outcome_and_the_recovery() -> None:
    section = readme_section(INSTALL_HEADING)
    assert "Python 3.11 or later" in section
    assert "older than 3.11" in section
    assert passage_problems(older_python_passage(), load_captures()) == set()
    assert "--python" in section
    assert "```text" not in section, "an output block must be paired with a command"
    assert "supports macOS and Linux" in section and "Python Package Index" in section
    assert "nothing outside Python's standard library" in section


# Test inputs below are labelled as such: synthetic contradictions of the recorded runs, built in
# memory by `altered` and never written into the capture file.


def altered(data: dict, index: int, change) -> dict:
    """A synthetic test input: the recorded runs with `change` applied to a copy of run `index`."""
    result = copy.deepcopy(data)
    change(result["runs"][index])
    return result


def run_index(data: dict, outcome: str) -> int:
    return next(i for i, run in enumerate(data["runs"]) if outcome_of(run) == outcome)


def case_i(data: dict) -> int:
    """The run of pipx itself on Python 3.9."""
    return next(
        i
        for i, run in enumerate(data["runs"])
        if version_of(run["pipx_python"]["version"])[:2] == (3, 9)
    )


def case_ii(data: dict) -> int:
    """The run of pipx on Python 3.11 or later, installing with an older default Python."""
    return next(
        i
        for i, run in enumerate(data["runs"])
        if version_of(run["pipx_python"]["version"]) >= OLDEST_SUPPORTED
        and version_of(run["default_install_python"]["version"]) < OLDEST_SUPPORTED
    )


def test_both_cp_620_cases_are_recorded_and_complete() -> None:
    data = load_captures()
    assert capture_problems(data) == set()
    case_i(data), case_ii(data)  # each raises StopIteration when its case is not recorded
    for run in data["runs"]:
        assert "\u2026" not in run["stdout"] + run["stderr"], "a stream is whole, not trimmed"
        assert run["argv"] == ["pipx", "install", "ka-telemetry"]


def test_every_outcome_claim_in_the_passage_holds_for_the_runs_it_covers() -> None:
    data = load_captures()
    passage = older_python_passage()
    assert passage_problems(passage, data) == set()
    stopped = ".".join(data["runs"][case_i(data)]["pipx_python"]["version"].split(".")[:2])
    installed = ".".join(data["runs"][case_ii(data)]["pipx_python"]["version"].split(".")[:2])
    scoped = (
        f" With pipx on Python {stopped}, the install stops with an error and installs nothing."
        f" With pipx on Python {installed}, the install installs ka-telemetry."
    )
    assert passage_problems(passage + scoped, data) == set()
    every_run_stopped = {"runs": [data["runs"][run_index(data, STOPPED)]]}
    universal = (
        "If the Python pipx installs with is older than 3.11, "
        "the install stops with an error and installs nothing."
    )
    assert passage_problems(universal, every_run_stopped) == set()


def test_a_quoted_message_must_appear_in_a_recorded_stream() -> None:
    data = load_captures()
    printed = " The last line reads \u201cError installing ka-telemetry.\u201d"
    invented = " The last line reads \u201cThis Python is too old for ka-telemetry.\u201d"
    passage = older_python_passage()
    assert "quoted-message-no-run-printed" not in passage_problems(passage + printed, data)
    assert "quoted-message-no-run-printed" in passage_problems(passage + invented, data)
    plain = passage + ' It says "no such thing".'
    assert "quoted-message-no-run-printed" in passage_problems(plain, data)


def test_a_claim_that_the_install_installs_nothing_fails_when_a_run_installed_it() -> None:
    data = load_captures()
    installed = data["runs"][run_index(data, INSTALLED)]["installed_state"]
    assert installed["present"], "the test input is a run that installed ka-telemetry"
    claim = (
        "If the Python pipx installs with is older than 3.11, "
        "the install stops with an error and installs nothing."
    )
    assert "not-every-run-stopped-and-installed-nothing" in passage_problems(claim, data)
    only_stopped = {"runs": [data["runs"][run_index(data, STOPPED)]]}
    assert passage_problems(claim, only_stopped) == set()


def test_a_universal_requirement_error_claim_fails_when_a_failed_run_lacks_it() -> None:
    data = load_captures()
    claim = (
        "If the Python pipx installs with is older than 3.11, the install stops with an error. "
        "The error names ka-telemetry's Python requirement, `>=3.11`, and nothing else differs."
    )
    assert "a-failed-run-does-not-name-the-requirement" not in passage_problems(claim, data)

    def drop_requirement(run: dict) -> None:
        run["stderr"] = run["stderr"].replace(">=3.11", "a newer Python")

    without = altered(data, run_index(data, STOPPED), drop_requirement)
    assert "a-failed-run-does-not-name-the-requirement" in passage_problems(claim, without)


def test_a_setup_specific_claim_fails_when_that_setups_run_contradicts_it() -> None:
    data = load_captures()
    installed_on = ".".join(data["runs"][case_ii(data)]["pipx_python"]["version"].split(".")[:2])
    wrong = (
        f"With pipx on Python {installed_on}, the install stops with an error and installs nothing."
    )
    assert "a-setup-claim-its-runs-contradict" in passage_problems(wrong, data)
    nobody = "With pipx on Python 3.8, the install stops with an error and installs nothing."
    assert "a-setup-claim-its-runs-contradict" in passage_problems(nobody, data)


def test_an_outcome_sentence_no_check_knows_is_refused() -> None:
    data = load_captures()
    passage = older_python_passage()
    assert "unrecognised-outcome-claim" not in passage_problems(passage, data)
    for sentence in (
        " On Python 3.9, pipx installs ka-telemetry anyway.",
        " pipx always chose Python 3.14.",
        " ka-telemetry is installed with the Python pipx used.",
    ):
        flagged = passage_problems(passage + sentence, data)
        assert "unrecognised-outcome-claim" in flagged, sentence


def test_older_python_passage_is_the_published_text() -> None:
    assert older_python_passage() == OLDER_PYTHON_TEXT


def test_passage_still_gives_3_11_the_older_than_condition_and_the_python_recovery() -> None:
    passage = older_python_passage()
    assert passage.startswith("If the Python pipx installs with is older than 3.11, ")
    assert "Python 3.11 or later" in passage and "`--python`" in passage
    assert "3.10" not in passage and "3.9" not in passage, "3.11 is the lowest, stated once"
    install = readme_section(INSTALL_HEADING)
    assert install.index(passage) < install.index("pipx install --python python3.12 ka-telemetry")
    blocks = re.findall(r"```sh\n(.*?)```", install, re.S)
    assert blocks[0] == "pipx install ka-telemetry\n"
    assert blocks[-1] == "pipx install --python python3.12 ka-telemetry\n"


def test_empty_runs_fail() -> None:
    assert capture_problems({"runs": []}) == {"no-runs"}
    assert capture_problems({}) == {"no-runs"}


def test_missing_a_cp_620_case_fails() -> None:
    data = load_captures()
    only_i = {"runs": [data["runs"][case_i(data)]]}
    only_ii = {"runs": [data["runs"][case_ii(data)]]}
    assert capture_problems(only_i) == {"missing-case-newer-pipx-older-default"}
    assert capture_problems(only_ii) == {"missing-case-pipx-on-3.9"}


@pytest.mark.parametrize(
    "path",
    [
        ("pipx", "version"),
        ("pipx_python", "version"),
        ("default_install_python", "version"),
        ("exit_status",),
        ("stdout",),
        ("stderr",),
        ("installed_state", "present"),
    ],
)
def test_missing_version_default_interpreter_status_or_stream_field_fails(path) -> None:
    data = load_captures()

    def remove(run: dict) -> None:
        node = run
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    flagged = capture_problems(altered(data, case_i(data), remove))
    assert "missing-field:" + ".".join(path) in flagged


def test_an_empty_stream_is_valid() -> None:
    data = load_captures()
    emptied = altered(data, run_index(data, STOPPED), lambda run: run.update(stdout=""))
    assert capture_problems(emptied) == set()


def test_inconsistent_install_observation_fails() -> None:
    data = load_captures()
    stopped, installed = run_index(data, STOPPED), run_index(data, INSTALLED)
    zero_but_absent = altered(data, stopped, lambda run: run.update(exit_status=0))
    failed_but_working = altered(data, installed, lambda run: run.update(exit_status=1))
    assert "install-observation-contradicts-exit-status" in capture_problems(zero_but_absent)
    assert "install-observation-contradicts-exit-status" in capture_problems(failed_but_working)


def test_recorded_writes_stay_under_the_scratch_root_and_index_points_only_at_the_wheel_directory(
) -> None:
    data = load_captures()
    index = "index-setting-is-not-the-local-wheel-directory"

    def with_settings(**settings: str) -> set[str]:
        return capture_problems(altered(data, 0, lambda run: run["settings"].update(settings)))

    assert "write-location-outside-scratch-root" in with_settings(PIPX_HOME="/Users/x/.local/pipx")
    assert "write-location-outside-scratch-root" in with_settings(TMPDIR="/tmp")
    assert index in with_settings(PIP_INDEX_URL="https://pypi.org/simple")
    assert index in with_settings(PIP_EXTRA_INDEX_URL="https://pypi.org/simple")
    assert index in with_settings(PIP_FIND_LINKS="/tmp/elsewhere")
    assert index in with_settings(PIP_NO_INDEX="0")


def test_a_preparation_failure_is_not_counted_as_a_capture() -> None:
    data = load_captures()

    def failed_preparation(run: dict) -> None:
        run["preparation_failure"] = True

    without_i = altered(data, case_i(data), failed_preparation)
    without_ii = altered(data, case_ii(data), failed_preparation)
    assert "missing-case-pipx-on-3.9" in capture_problems(without_i)
    assert "missing-case-newer-pipx-older-default" in capture_problems(without_ii)
    every = copy.deepcopy(data)
    for run in every["runs"]:
        run["preparation_failure"] = True
    assert capture_problems(every) == {"no-runs"}


def test_the_library_note_is_its_own_section_and_the_only_pip_install_line() -> None:
    text = README.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    start = headings.index(INSTALL_HEADING)
    assert headings[start : start + 3] == [INSTALL_HEADING, LIBRARY_HEADING, TRY_IT_HEADING]
    holders = [name for name, body in sections(text).items() if "pip install ka-telemetry" in body]
    assert holders == [LIBRARY_HEADING]
    note = readme_section(LIBRARY_HEADING)
    assert "virtual environment" in note and "active" in note
    assert "run the `ka-telemetry` command" in note and "pipx" in note, "pip is not a way to run it"
    assert sum(line.count("pip install ka-telemetry") for line in text.splitlines()) == 1


def test_the_readme_keeps_its_alternate_route_prohibitions() -> None:
    text = README.read_text(encoding="utf-8")
    for alternate in ("git+", "pip install .", "python -m ka_telemetry", "python3 -m ka_telemetry"):
        assert alternate not in text, alternate


def around_older_python_passage(install: str) -> tuple[str, str]:
    """The text of the Install section before and after its older-Python paragraph."""
    start = install.index(OLDER_PYTHON_PASSAGE)
    return install[:start], install[install.index("\n\n", start) + 2 :]


def test_every_readme_section_outside_the_two_passages_is_identical_to_f2b7a6e() -> None:
    before, after = sections(base_readme()), sections(README.read_text(encoding="utf-8"))
    # Built by KensaiApp is a section of its own, added after the base, before Install. The text
    # before the first heading lost its pointer to the requirements document, which the public
    # repository leaves out.
    after.pop(BUILT_BY_HEADING)
    prd_pointer = r"What it is and why is \[[^\]]+\]\([^)]+\)\. "
    assert after.pop("") == re.sub(prd_pointer, "", before.pop(""), count=1)
    assert list(after) == list(before), "the headings and their order are the base's"
    for name, body in before.items():
        # Check a release changes in one passage and one paragraph; what stays is held byte for byte
        # by `test_protected_text_of_the_section_matches_f2b7a6e` in test_release_check_readme.py.
        # Try it gained how to get the copy its example runs in; its example is held by the
        # paired-example tests. Support gained the security-report channel and Versions the fact that
        # kensaiapp runs on ka-telemetry; Status moved its list of what is built to the release notes.
        if name not in (
            INSTALL_HEADING, RELEASE_HEADING, TRY_IT_HEADING,
            SUPPORT_HEADING, VERSIONS_HEADING, STATUS_HEADING,
        ):
            assert after[name] == body, name
    prefix, suffix = around_older_python_passage(before[INSTALL_HEADING])
    install = after[INSTALL_HEADING]
    assert install.startswith(prefix) and install.endswith(suffix)
    assert len(install) > len(prefix) + len(suffix), "the older-Python passage is still there"


def test_preservation_baseline_missing_is_a_failure() -> None:
    with pytest.raises(BaselineMissing):
        base_readme("0" * 40)
    assert base_readme(README_BASE_COMMIT).startswith("# ")


def test_a_copy_with_no_history_skips_the_comparison_by_name(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    copy.mkdir()
    git = [
        "git", "-C", str(copy),
        "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null",
    ]
    subprocess.run([*git, "init", "--quiet"], check=True)
    with pytest.raises(pytest.skip.Exception, match="no history"):
        base_readme(repo=copy)
    (copy / "README.md").write_text("# copy\n", encoding="utf-8")
    subprocess.run([*git, "add", "--all"], check=True)
    subprocess.run([*git, "commit", "--quiet", "--message", "copy"], check=True)
    with pytest.raises(pytest.skip.Exception, match="no history"):
        base_readme(repo=copy)


def test_the_how_to_still_finds_the_command_with_command_v_in_the_pipx_command_directory(
    tmp_path: Path,
) -> None:
    _, how_to = parsed_pair()
    assert how_to.installs == ()
    lines = [
        line for line in HOW_TO.read_text(encoding="utf-8").splitlines() if "command -v" in line
    ]
    assert lines == ["command -v ka-telemetry"] * 2
    pipx_bin = tmp_path / "pipx-bin"
    pipx_bin.mkdir()
    command = pipx_bin / PRODUCT_NAME
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": f"{pipx_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    done = run_shell(lines[0], env, home)
    assert done.stdout.strip() == str(command) and done.returncode == 0
    assert list(home.iterdir()) == [], "no shell profile was touched"


def test_how_to_names_no_install_instruction_and_no_alternate_route() -> None:
    """CP-519: neither document shows another way to install or run ka-telemetry. The README holds
    the one install path; the how-to points at it and shows none of its own."""
    _, how_to = parsed_pair()
    text = HOW_TO.read_text(encoding="utf-8")
    assert how_to.installs == ()
    for alternate in ("git+", "pip install .", "python -m ka_telemetry", "python3 -m ka_telemetry"):
        assert alternate not in text, alternate


def test_readme_how_to_link_resolves_to_a_tracked_file_outside_forbidden_locations() -> None:
    targets = re.findall(r"\]\((docs/how-to\.md)\)", README.read_text(encoding="utf-8"))
    assert targets, "the README links to docs/how-to.md"
    for target in targets:
        resolved = (README.parent / target).resolve()
        relative = resolved.relative_to(PRODUCT_ROOT).as_posix()
        assert resolved.is_file()
        assert not any(
            relative == place or relative.startswith(f"{place}/") for place in FORBIDDEN_LOCATIONS
        )
        tracked = subprocess.run(
            ["git", "-C", str(PRODUCT_ROOT), "ls-files", "--error-unmatch", relative],
            capture_output=True,
            text=True,
            check=False,
        )
        assert tracked.returncode == 0, tracked.stderr


def test_readme_names_codex_gemini_and_local_model() -> None:
    text = README.read_text(encoding="utf-8").lower()
    for name in ("codex", "gemini", "local model"):
        assert name in text, name


def test_readme_effects_inventory_has_a_row_per_dispatched_command(vocabulary: Vocabulary) -> None:
    headings = [
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("**") and PRODUCT_NAME in line
    ]
    assert vocabulary.commands, "the installed help declares commands"
    for command in vocabulary.commands:
        assert any(f"{PRODUCT_NAME} {command}`" in heading for heading in headings), command


def test_readme_states_api_support_identity_and_host_clauses() -> None:
    text = README.read_text(encoding="utf-8")
    for heading in ("## Support", "## Versions and the public API", "## What the files hold"):
        assert heading in text, heading
    lowered = text.lower()
    for phrase in ("best effort", "semantic versioning", "user.email", "--host"):
        assert phrase in lowered, phrase


# --- the documented setup -----------------------------------------------------------------------


def every_run_section(how_to_text: str) -> str:
    start = how_to_text.index(EVERY_RUN_HEADING)
    following = how_to_text.find("\n## ", start + len(EVERY_RUN_HEADING))
    return how_to_text[start : following if following != -1 else len(how_to_text)]


def shell_blocks(section: str) -> list[str]:
    parsed = parse_document(section, HOW_TO)
    return ["\n".join(block.body) for block in parsed.blocks]


def documented_turn_on(section: str) -> str:
    return next(text for text in shell_blocks(section) if text.startswith("ka-telemetry turn-on"))


def documented_mkdir(section: str) -> str:
    return next(text for text in shell_blocks(section) if text.startswith("mkdir"))


def documented_receive(section: str) -> list[str]:
    parsed = parse_document(section, HOW_TO)
    return list(next(site.argv for site in parsed.invocations if site.argv[1] == "receive"))


def settings_keys(settings_file: Path) -> dict[str, str]:
    return json.loads(settings_file.read_text(encoding="utf-8"))["env"]


def start_documented_receiver(installed: InstalledProduct, home: Path, tmp_path: Path) -> Receiver:
    """The documented receive command, on a free port: the options come from the how-to, and the
    publish path is the one the how-to gives, which `turn-on` reads by default."""
    argv = documented_receive(every_run_section(read_documents()[1]))
    assert argv[argv.index("--publish-endpoint") + 1] == ENDPOINT_PATH
    receiver = Receiver(installed, home, scratch=tmp_path)
    receiver.endpoint_file = home / ".claude" / "tools" / "otel-receiver" / "endpoint"
    return receiver


def test_every_run_setup_publishes_endpoint_before_turn_on_writes_settings(
    installed: InstalledProduct, home: Path, tmp_path: Path
) -> None:
    section = every_run_section(read_documents()[1])
    env = environment(installed, home)
    settings = home / ".claude" / "settings.json"

    assert run_shell(documented_mkdir(section), env, PRODUCT_ROOT).returncode == 0
    early = run_shell(documented_turn_on(section), env, PRODUCT_ROOT)
    assert NOTHING_WRITTEN in early.stdout, "turn-on before the receiver published writes nothing"
    assert not settings.exists()

    with start_documented_receiver(installed, home, tmp_path) as receiver:
        done = run_shell(documented_turn_on(section), env, PRODUCT_ROOT)
        assert done.returncode == 0, done.stderr
        written = settings_keys(settings)
    assert sorted(written) == sorted(FIVE_KEYS)
    assert written["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://localhost:{receiver.port}"
    assert "wrote " + ", ".join(FIVE_KEYS) in done.stdout


def one_run_command(how_to_text: str) -> str:
    parsed = parse_document(how_to_text, HOW_TO)
    return next(
        "\n".join(block.body)
        for block in parsed.blocks
        if "claude --settings" in "".join(block.body)
    )


@pytest.fixture
def recording_claude(tmp_path: Path) -> tuple[Path, Path]:
    """A `claude` stand-in on a directory of its own: it records its arguments and launches no
    model. Returns the directory for `PATH` and the file the arguments are recorded in."""
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    program = directory / "claude"
    program.write_text(RECORDING_CLAUDE, encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return directory, tmp_path / "recorded"


@pytest.mark.parametrize("existing", [None, b'{"theme": "dark"}\n'])
def test_one_run_setup_passes_the_five_keys_to_a_recording_claude_and_changes_no_settings(
    installed: InstalledProduct,
    home: Path,
    tmp_path: Path,
    recording_claude: tuple[Path, Path],
    existing: bytes | None,
) -> None:
    directory, record = recording_claude
    settings = home / ".claude" / "settings.json"
    if existing is not None:
        settings.write_bytes(existing)
    env = {**environment(installed, home, directory), RECORD_VARIABLE: str(record)}
    (home / ".claude" / "tools" / "otel-receiver").mkdir(parents=True)

    with start_documented_receiver(installed, home, tmp_path) as receiver:
        done = run_shell(one_run_command(read_documents()[1]), env, PRODUCT_ROOT)
    assert done.returncode == 0, done.stderr

    arguments = record.read_text(encoding="utf-8").split("\0")[:-1]
    assert len(arguments) == 2
    assert arguments[0] == "--settings"
    keys = json.loads(arguments[1])["env"]
    assert sorted(keys) == sorted(FIVE_KEYS)
    assert keys["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://localhost:{receiver.port}"
    assert "Telemetry:" in done.stderr and "Telemetry:" not in arguments[1]
    if existing is None:
        assert not settings.exists()
    else:
        assert settings.read_bytes() == existing


def test_walkthrough_fails_without_a_published_endpoint(
    installed: InstalledProduct, home: Path
) -> None:
    section = every_run_section(read_documents()[1])
    env = environment(installed, home)
    run_shell(documented_mkdir(section), env, PRODUCT_ROOT)

    done = run_shell(documented_turn_on(section), env, PRODUCT_ROOT)

    assert "no published telemetry endpoint" in done.stdout
    assert NOTHING_WRITTEN in done.stdout
    assert not (home / ".claude" / "settings.json").exists()
    assert "no published telemetry endpoint" in read_documents()[1], "the how-to says so too"


def test_walkthrough_fails_when_endpoint_parent_is_absent(
    installed: InstalledProduct, home: Path, tmp_path: Path
) -> None:
    how_to_text = read_documents()[1]
    assert how_to_text.index("mkdir -p ~/.claude/tools/otel-receiver") < how_to_text.index(
        "ka-telemetry receive --port"
    ), "the how-to makes the parent before it starts the receiver"
    receiver = start_documented_receiver(installed, home, tmp_path)

    with pytest.raises(AssertionError, match="exited at start"), receiver:
        pass
    assert not receiver.endpoint_file.exists()


def test_malformed_invocation_and_invalid_option_in_setup_text_fail(
    vocabulary: Vocabulary,
) -> None:
    how_to_text = read_documents()[1]
    turn_on = "ka-telemetry turn-on --settings-file ~/.claude/settings.json"
    assert turn_on in how_to_text

    malformed = how_to_text.replace(turn_on, turn_on.replace("~", '"~', 1))
    with pytest.raises(DocumentError) as unclosed:
        parse_document(malformed, HOW_TO)
    assert unclosed.value.line > 0

    misspelled = parse_document(how_to_text.replace("--settings-file", "--settngs-file"), HOW_TO)
    with pytest.raises(DocumentError) as unknown:
        validate_invocations(misspelled.invocations, vocabulary)
    assert unknown.value.token == "--settngs-file"


# --- the same validation, on text copies --------------------------------------------------------


def pair_pattern(command_prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"```sh\n{re.escape(command_prefix)}[^\n]*\n```\n\n```text\n.*?\n```\n", re.DOTALL
    )


def test_mutating_one_output_character_fails(installed: InstalledProduct, home: Path) -> None:
    readme_text = read_documents()[0]
    first_line = "2 agent row(s), 1 unreadable line(s) skipped."
    assert first_line in readme_text
    mutated = parse_document(
        readme_text.replace(first_line, first_line.replace("2", "3", 1)), README
    )

    with pytest.raises(DocumentError) as difference:
        run_example(installed, mutated.examples[0], home)
    assert "other than the block shown" in difference.value.message


def test_removing_the_readme_pair_fails() -> None:
    readme_text, how_to_text = read_documents()
    pattern = pair_pattern(f"{PRODUCT_NAME} agents")
    assert pattern.search(readme_text)

    with pytest.raises(DocumentError) as missing:
        validate_set(pattern.sub("", readme_text, count=1), how_to_text)
    assert missing.value.source == str(README)


@pytest.mark.parametrize("report", REPORTS)
def test_removing_each_required_how_to_report_pair_fails(report: str) -> None:
    readme_text, how_to_text = read_documents()
    pattern = pair_pattern(f"{PRODUCT_NAME} {report} ")
    assert pattern.search(how_to_text)

    with pytest.raises(DocumentError) as missing:
        validate_set(readme_text, pattern.sub("", how_to_text, count=1))
    assert report in missing.value.message


@pytest.mark.parametrize("document", ["README", "how-to"])
def test_leaving_a_fence_open_in_either_file_fails(document: str) -> None:
    texts = dict(zip(("README", "how-to"), read_documents(), strict=True))
    opened = texts[document] + "\n```sh\nka-telemetry agents\n"

    with pytest.raises(DocumentError):
        parse_document(opened, document)


@pytest.mark.parametrize("document", [README, HOW_TO], ids=["README", "how-to"])
def test_leaving_any_existing_fence_open_in_either_file_fails(document: Path) -> None:
    """CP-538: remove each block's closing fence in turn. The block is reported as never closed, at
    its own opening line, whichever block follows it."""
    text = document.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocks = parse_document(text, document).blocks
    assert blocks, "the document holds code blocks"

    for found in blocks:
        opened = "\n".join(lines[: found.end_line - 1] + lines[found.end_line :])
        with pytest.raises(DocumentError) as unclosed:
            parse_document(opened, document)
        assert (unclosed.value.line, unclosed.value.message) == (
            found.line,
            "the code block opened here is never closed",
        ), f"closing fence of the block at line {found.line} removed"


Mutation = tuple[str, str, str]

SPELLING_MUTATIONS: list[Mutation] = [
    (
        "unpaired block",
        "receive --port 4318 --publish-endpoint ~/.claude",
        "receive --prot 4318 --publish-endpoint ~/.claude",
    ),
    ("inline text", "`ka-telemetry agents`", "`ka-telemetry agents --prot`"),
    ("nested substitution", "$(ka-telemetry turn-on --print)", "$(ka-telemetry turn-on --prnt)"),
    ("launchd arguments", "<string>--port</string>", "<string>--prot</string>"),
    (
        "systemd command",
        "receive --port 4318 --publish-endpoint %h",
        "receive --prot 4318 --publish-endpoint %h",
    ),
]
UNKNOWN_COMMAND_MUTATIONS: list[Mutation] = [
    ("unpaired block", "ka-telemetry receive --port", "ka-telemetry recieve --port"),
    ("inline text", "`ka-telemetry agents`", "`ka-telemetry agentz`"),
    ("nested substitution", "$(ka-telemetry turn-on --print)", "$(ka-telemetry turn-onn --print)"),
    ("launchd arguments", "<string>receive</string>", "<string>recieve</string>"),
    (
        "systemd command",
        "ka-telemetry receive --port 4318 --publish",
        "ka-telemetry recieve --port 4318 --publish",
    ),
]


def mutate_and_validate(old: str, new: str, vocabulary: Vocabulary) -> DocumentError:
    how_to_text = read_documents()[1]
    assert old in how_to_text, old
    mutated = parse_document(how_to_text.replace(old, new), HOW_TO)
    with pytest.raises(DocumentError) as refused:
        validate_invocations(mutated.invocations, vocabulary)
    return refused.value


@pytest.mark.parametrize(("place", "old", "new"), SPELLING_MUTATIONS)
def test_mutating_receive_port_to_prot_in_unpaired_block_fails(
    place: str, old: str, new: str, vocabulary: Vocabulary
) -> None:
    """The mutated option is refused in each of the places an invocation can sit."""
    assert mutate_and_validate(old, new, vocabulary).token in ("--prot", "--prnt")


@pytest.mark.parametrize(("place", "old", "new"), UNKNOWN_COMMAND_MUTATIONS)
def test_mutating_a_command_name_fails_in_each_place_an_invocation_can_sit(
    place: str, old: str, new: str, vocabulary: Vocabulary
) -> None:
    assert mutate_and_validate(old, new, vocabulary).token in (
        "recieve",
        "agentz",
        "turn-onn",
    )


def test_the_unmutated_documents_pass_the_checks_the_mutations_break(
    vocabulary: Vocabulary,
) -> None:
    """The control for the mutations above: the unmutated documents pass the same checks."""
    readme_text, how_to_text = read_documents()
    validate_set(readme_text, how_to_text)
    validate_invocations(parse_document(how_to_text, HOW_TO).invocations, vocabulary)
