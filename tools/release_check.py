"""Check a release: `python3 tools/release_check.py`.

Measures the PRD's data bars 1, 2, 3, 5, 7 and 8 on a disposable copy of the checkout this
script lives in, prints one line per bar ending `holds` or `fails`, writes what did not hold to
standard error, and exits 0 only when every line holds. It measures the files as they are,
uncommitted edits and deletions included, and writes nothing in the checkout: everything it makes
is under one temporary directory that it removes before it exits.

Only the standard library is imported here, and bytecode is not written, because this runs in the
original checkout. Every measurement imports and runs in a child process of its own, over the
copy (`tools/release_metrics.py`).
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Callable, Iterable, Mapping  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

CHECKOUT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_metrics  # noqa: E402

DESCRIPTIONS: dict[int, str] = {
    1: "Every figure ka-telemetry reports on the reference set equals its recorded expected figure",
    2: (
        "Cost totals agree within 1% for every session recorded as agreeing, and every session "
        "outside 1% or with no cost metric is named with a possible cause"
    ),
    3: (
        "No response on the reference set is counted more than once by the runs report or the "
        "steps report"
    ),
    5: (
        "The README's install command, run as written on a Python that refuses pip installs as "
        "externally managed, installs the ka-telemetry built from this checkout and no other "
        "package, setting pip, setuptools and wheel aside"
    ),
    7: (
        "No ka-telemetry command that the release check runs tries to reach beyond this machine, "
        "and the receiver started with no --host listens only on this machine"
    ),
    8: "Every example in the README and the how-to runs as written and prints the output shown",
}
"""The registry: a bar's number, and the words it is printed with, the same whichever verdict
follows. A bar is added by adding its number here and its measurement to `release_metrics`."""

KIND_GIT_MISSING = "git_missing"
KIND_NOT_PREPARED = "not_prepared"

NOT_COPIED = frozenset(
    {
        ".git",
        ".worktrees",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        ".venv",
        "venv",
        "build",
        "dist",
    }
)
NOT_COPIED_SUFFIX = ".egg-info"
"""What the working-file overlay leaves out: Git metadata, nested worktrees and tool or build
caches."""

SCRATCH_PLACES = {
    "HOME": "home",
    "TMPDIR": "tmp",
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "data",
    "PIP_CACHE_DIR": "pip-cache",
    "PYTEST_DEBUG_TEMPROOT": "tmp",
}
DROPPED_PREFIXES = (
    "PYTHON",
    "PYTEST_",
    "PIP_",
    "XDG_",
    "GIT_",
    "UV_",
    "PIPX_",
    "VIRTUAL_ENV",
    "TMP",
    "TEMP",
    "HOME",
)
CLONE = "checkout"
SCRATCH_PREFIX = "release-check-"

TERMINAL_SEQUENCES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # a control sequence
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # an operating-system command
    r"|\x1b[@-Z\\-_]"  # any other escape
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # a control character that is not a tab or a line end
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def plain(text: str) -> str:
    """`text` without terminal control sequences."""
    return TERMINAL_SEQUENCES.sub("", text)


# ── the scratch copy and the children that run in it ──


class ScratchNotPrepared(Exception):
    """The temporary copy could not be made: Git is missing, or the system gave a reason."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def child_environment(scratch: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    """The environment of every child: the caller's, without any Python, pytest, pip, Git or
    cache setting that could reach outside the scratch directory, with every place a tool writes
    set to one inside it and bytecode and pytest's plugin autoload off."""
    env = {
        key: value
        for key, value in inherited.items()
        if not key.startswith(DROPPED_PREFIXES)
    }
    for key, place in SCRATCH_PLACES.items():
        env[key] = str(scratch / place)
    env.update(
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return env


def _is_left_out(name: str) -> bool:
    return name in NOT_COPIED or name.endswith(NOT_COPIED_SUFFIX)


def _copy_working_files(checkout: Path, copy: Path) -> None:
    """Make `copy` hold what `checkout` holds now: edits and additions copied over it, and what
    `checkout` no longer has taken out, so a deleted input is not restored from the clone. A link
    is copied only when it points inside the checkout by a relative path; nothing is followed."""
    for directory, names, files in os.walk(checkout):
        names[:] = [name for name in names if not _is_left_out(name)]
        here = Path(directory)
        target = copy / here.relative_to(checkout)
        target.mkdir(parents=True, exist_ok=True)
        for name in [*names, *files]:
            source = here / name
            if source.is_symlink():
                _copy_link(checkout, source, target / name)
                if name in names:
                    names.remove(name)
            elif name in files:
                shutil.copy2(source, target / name)
    for directory, names, files in os.walk(copy, topdown=True):
        here = Path(directory)
        names[:] = [name for name in names if not _is_left_out(name)]
        original = checkout / here.relative_to(copy)
        for name in [*names, *files]:
            if not os.path.lexists(original / name):
                path = here / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                    names.remove(name)
                else:
                    path.unlink()


def _copy_link(checkout: Path, source: Path, destination: Path) -> None:
    link = os.readlink(source)
    if os.path.isabs(link) or not (source.parent / link).resolve().is_relative_to(
        checkout.resolve()
    ):
        return
    if os.path.lexists(destination):
        destination.unlink()
    os.symlink(link, destination)


def prepare_scratch(checkout: Path, scratch: Path, *, run: Run, environment: dict) -> Path:
    """Make the copy that is measured: a private local clone, with Git metadata of its own and no
    hardlinks into the checkout, overlaid with the checkout's working files."""
    for place in set(SCRATCH_PLACES.values()):
        (scratch / place).mkdir(parents=True, exist_ok=True)
    copy = scratch / CLONE
    try:
        done = run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(checkout), str(copy)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            cwd=scratch,
        )
    except FileNotFoundError as missing:
        raise ScratchNotPrepared(KIND_GIT_MISSING, str(missing)) from missing
    except OSError as failure:
        raise ScratchNotPrepared(KIND_NOT_PREPARED, failure.strerror or str(failure)) from failure
    if done.returncode != 0:
        raise ScratchNotPrepared(KIND_NOT_PREPARED, (done.stderr or done.stdout).strip())
    try:
        _copy_working_files(checkout, copy)
    except OSError as failure:
        raise ScratchNotPrepared(KIND_NOT_PREPARED, failure.strerror or str(failure)) from failure
    return copy


def _failed_everywhere(ids: Iterable[int], error: ScratchNotPrepared):
    for metric in ids:
        diagnostic = release_metrics.Diagnostic(error.kind, "the temporary copy", error.reason)
        yield metric, release_metrics.WorkerResult(metric, False, (diagnostic,))


def measure(
    checkout: Path,
    scratch: Path,
    ids: Iterable[int],
    *,
    run: Run = subprocess.run,
    inherited: Mapping[str, str] | None = None,
) -> dict[int, release_metrics.WorkerResult]:
    """One result for every id in `ids`: each measured in a child process over the scratch copy,
    or all failed together when the copy could not be made."""
    ids = sorted(ids)
    environment = child_environment(scratch, os.environ if inherited is None else inherited)
    try:
        copy = prepare_scratch(checkout, scratch, run=run, environment=environment)
    except ScratchNotPrepared as error:
        return dict(_failed_everywhere(ids, error))
    results = {}
    for metric in ids:
        command = [
            sys.executable,
            str(copy / "tools" / "release_metrics.py"),
            "worker",
            "--metric",
            str(metric),
            "--checkout",
            str(copy),
        ]
        try:
            done = run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                cwd=scratch,
            )
        except OSError as failure:
            results[metric] = release_metrics.read_result(
                metric, None, returncode=127, stderr=failure.strerror or str(failure)
            )
            continue
        results[metric] = release_metrics.read_result(
            metric, done.stdout, returncode=done.returncode, stderr=done.stderr.strip()
        )
    return results


# ── what is printed ──

EXAMPLES_FILE = release_metrics.EXAMPLES_FILE
EXAMPLES_RUN = f"python3 -m pytest {EXAMPLES_FILE}"
NOT_MEASURED = "This bar was not measured:"

CLOSING = {
    1: (
        "Nothing under tests/parity or tests/stream_parity is edited to make this bar hold, so "
        "the fix goes in ka-telemetry. To see whether anything there has changes that are not "
        "committed, run git status -- tests/parity tests/stream_parity."
    ),
    2: (
        "Nothing under tests/stream_parity is edited to make this bar hold, so the fix goes in "
        "ka-telemetry. To see the check's line for every session, run python3 -m ka_telemetry "
        "check --dir tests/stream_parity from the top directory of this checkout. To see whether "
        "anything under tests/stream_parity has changes that are not committed, run git status "
        "-- tests/stream_parity."
    ),
    3: (
        "Nothing under tests/parity is edited to make this bar hold, so the fix goes in "
        "ka-telemetry. To see whether anything there has changes that are not committed, run git "
        "status -- tests/parity."
    ),
    7: (
        "This bar counts only these as this machine: an address from 127.0.0.0 to "
        "127.255.255.255, the same address written in IPv6 form, such as ::ffff:127.0.0.1, the "
        "address ::1, and the host name localhost. No ka-telemetry command is allowed to try to "
        "reach anything else, so the fix goes in ka-telemetry: change each command named above "
        "so that it no longer makes the attempt, then run the release check again."
    ),
}
"""The step printed once, after the last message of a bar that names what differs."""

CLOSED_BY = {
    1: {release_metrics.KIND_DIFFERENCE},
    2: {
        release_metrics.KIND_SESSION_APART,
        release_metrics.KIND_SESSION_NO_METRIC,
        release_metrics.KIND_SESSION_MISSING,
        release_metrics.KIND_APART_WITHOUT_CAUSE,
        release_metrics.KIND_NO_METRIC_WITHOUT_CAUSE,
    },
    3: {release_metrics.KIND_COUNTED_TWICE},
    7: {release_metrics.KIND_NETWORK_ATTEMPT},
}

SESSION_RECORDED = "the reference set records its cost totals as agreeing within 1%, and"
SESSION_MESSAGES = {
    release_metrics.KIND_SESSION_APART: (
        f"{SESSION_RECORDED} ka-telemetry check now reports them more than 1% apart."
    ),
    release_metrics.KIND_SESSION_NO_METRIC: (
        f"{SESSION_RECORDED} ka-telemetry check now reports no cost metric for it."
    ),
    release_metrics.KIND_SESSION_MISSING: (
        f"{SESSION_RECORDED} ka-telemetry check does not report the session at all."
    ),
    release_metrics.KIND_APART_WITHOUT_CAUSE: (
        "ka-telemetry check reports its cost totals more than 1% apart and names no possible "
        "cause."
    ),
    release_metrics.KIND_NO_METRIC_WITHOUT_CAUSE: (
        "ka-telemetry check reports no cost metric for it and names no possible cause."
    ),
}


def _names(names: Iterable[str]) -> str:
    """Each name once, in the order given, with a comma and a space between names."""
    return ", ".join(dict.fromkeys(names))


PACKAGES_ASKED_FOR = (
    "What was built from this checkout asks for packages other than ka-telemetry when it is "
    "installed: {names}. This bar asks that installing ka-telemetry installs nothing else, so "
    "the fix goes in ka-telemetry: take each of them out of the dependencies list in "
    "pyproject.toml, then run the release check again."
)
PACKAGES_INSTALLED = (
    "After the README's install command ran, the environment pipx made for ka-telemetry holds "
    "packages other than ka-telemetry, pip, setuptools and wheel: {names}. This bar asks that "
    "installing ka-telemetry installs nothing else, so the fix goes in ka-telemetry. The "
    "packages ka-telemetry asks to have installed with it are in the dependencies list in "
    "pyproject.toml. Take each of these out of that list, then run the release check again."
)
WHERE_BAR_5_LOOKS = (
    "This bar runs the one command line in the first shell code block of the Install section of "
    "README.md, a block fenced as sh, shell or bash."
)
README_NOT_READ = (
    f"{NOT_MEASURED} the README's install command could not be read, because"
)
README_READINGS = {
    release_metrics.READING_FILE_UNREADABLE: (
        f"{NOT_MEASURED} the release check could not read README.md in this checkout, so it "
        "could not read the README's install command and ran nothing for this bar. The system "
        "reported: {reason}. Run the release check again once README.md can be read."
    ),
    release_metrics.READING_NO_INSTALL_SECTION: (
        f"{README_NOT_READ} README.md has no section headed Install, so the release check ran "
        f"nothing for this bar. {WHERE_BAR_5_LOOKS} Add an Install section to README.md with the "
        "install command in its first shell code block, then run the release check again."
    ),
    release_metrics.READING_NO_SHELL_BLOCK: (
        f"{README_NOT_READ} the Install section of README.md holds no shell code block, so the "
        f"release check ran nothing for this bar. {WHERE_BAR_5_LOOKS} Put the install command in "
        "the first shell code block of that section, then run the release check again."
    ),
    release_metrics.READING_BLOCK_UNCLOSED: (
        f"{README_NOT_READ} the first shell code block in the Install section of README.md, "
        "opened at line {line}, is never closed, so the release check ran nothing for this bar. "
        "Close that block with the same fence it opens with, then run the release check again."
    ),
    release_metrics.READING_NOT_ONE_LINE: (
        f"{README_NOT_READ} the first shell code block in the Install section of README.md, "
        "opened at line {line}, does not hold exactly one line, so the release check could not "
        f"tell which line is the install command and ran nothing for this bar. "
        f"{WHERE_BAR_5_LOOKS} Leave the install command alone in that block, with no blank line "
        "or comment beside it, and put any other command in a later block. Then run the release "
        "check again."
    ),
    release_metrics.READING_WORDS_UNREADABLE: (
        f"{README_NOT_READ} the command on line {{line}} of README.md, {{command}}, could not be "
        "split into words: {reason}. The release check ran nothing for this bar. Correct the "
        "quoting in that line, then run the release check again."
    ),
    release_metrics.READING_TARGET_NOT_NAMED: (
        f"{README_NOT_READ} the first shell code block in the Install section of README.md, "
        "opened at line {line}, holds {command}, which does not name ka-telemetry. The release "
        f"check did not run it, and ran nothing for this bar. {WHERE_BAR_5_LOOKS} Put the "
        "command that installs ka-telemetry in that first block, and move a command that "
        "prepares for it, such as one that installs pipx, to a later block. Then run the "
        "release check again."
    ),
}
COMMAND_REFUSED = (
    f"{NOT_MEASURED} the README's install command, {{command}}, on line {{line}} of README.md, "
    "is not one the release check runs: {reason}. The release check runs an install command "
    "only in a form it can run without writing anything outside its temporary directory, and "
    "it ran nothing for this bar. Make the first shell code block in the Install section of "
    "README.md read pipx install ka-telemetry, then run the release check again."
)
NO_PYTHON_OPENING = (
    f"{NOT_MEASURED} the release check found no Python 3.11 or later that refuses pip installs "
    "as externally managed, and could not make one in its temporary directory, so it did not "
    "run the README's install command. This bar is measured on such a Python only. Each Python "
    "it tried, and what it found:"
)
NO_PYTHON_CLOSING = (
    "To measure this bar, run the release check with a Python 3.11 or later that refuses pip "
    "installs as externally managed, such as Homebrew's Python on macOS."
)
CANDIDATE_MESSAGES = {
    release_metrics.CANDIDATE_TOO_OLD: "{python}: it is Python {version}, older than 3.11.",
    release_metrics.CANDIDATE_IN_VIRTUAL_ENVIRONMENT: (
        "{python}: it runs inside a virtual environment."
    ),
    release_metrics.CANDIDATE_NOT_MARKED: "{python}: it is not marked as externally managed.",
    release_metrics.CANDIDATE_NO_PIP: (
        "{python}: it has no pip the release check could run, so whether it refuses pip "
        "installs could not be confirmed."
    ),
    release_metrics.CANDIDATE_NOT_REFUSED: (
        "{python}: pip did not refuse a trial install as externally managed. pip reported: "
        "{reason}"
    ),
    release_metrics.CANDIDATE_COPY_FAILED: (
        "A copy of {python} in the temporary directory could not be made to refuse pip installs "
        "as externally managed. It reported: {reason}"
    ),
}
PYTHON_MISMATCH = (
    f"{NOT_MEASURED} the README's install command, {{command}}, made the environment for "
    "ka-telemetry from {found}, not from {expected}, the Python the release check had "
    "confirmed refuses pip installs as externally managed. This bar is measured on that Python "
    "only. Run the release check again."
)
PIPX_INSTALL_DID_NOT_SUCCEED = (
    f"{NOT_MEASURED} pipx was not found on this machine's PATH, and the release check could "
    "not install its own pipx in its temporary directory, so it did not run the README's "
    "install command. Here is what the release check's install of pipx did."
)
PIPX_INSTALL_CLOSING = (
    "Install pipx as the Install section of README.md shows, so that it is on your PATH, then "
    "run the release check again."
)
PIPX_INSTALL_NOT_STARTED = (
    f"{NOT_MEASURED} pipx was not found on this machine's PATH, and the release check could "
    "not start its own install of pipx in its temporary directory, so it did not run the "
    "README's install command. The system reported: {reason}. " + PIPX_INSTALL_CLOSING
)
PIPX_NOT_PREPARED = (
    f"{NOT_MEASURED} the release check could not prepare {{pipx}} to install from what it "
    "built from this checkout and nothing else, so it did not run the README's install "
    "command. It reported: {reason}. Run the release check again once what it reported is "
    "resolved."
)
INSTALLED_NOTHING = (
    "The README's install command, {command}, installed nothing: when it finished, the release "
    "check found no ka-telemetry that it had installed."
)
INSTALLED_NOTHING_CLOSING = (
    "This bar asks that the README's install command installs ka-telemetry. Check that the "
    "first shell code block in the Install section of README.md reads pipx install "
    "ka-telemetry, and that the name set in pyproject.toml is ka-telemetry, then run the "
    "release check again."
)
COMMAND_DID_NOT_SUCCEED = (
    "The README's install command, {command}, did not succeed, so this bar does not hold. The "
    "release check gave it only what it built from this checkout to install, and nothing could "
    "be fetched while it ran."
)
COMMAND_DID_NOT_SUCCEED_CLOSING = (
    "Run the release check again once what the command printed is resolved."
)
COMMAND_NOT_STARTED = (
    f"{NOT_MEASURED} the README's install command, {{command}}, could not be started. The "
    "system reported: {reason}. Run the release check again once that is resolved."
)
LISTING_UNREADABLE = (
    f"{NOT_MEASURED} the README's install command, {{command}}, finished, and the release "
    "check could not read the list of packages in the environment pipx made for ka-telemetry. "
    "Reading the list reported: {reason}. Run the release check again."
)
LISTING_GAVE_NOTHING = (
    f"{NOT_MEASURED} the README's install command, {{command}}, finished, and reading the list "
    "of packages in the environment pipx made for ka-telemetry gave back nothing. Run the "
    "release check again."
)
NOT_THIS_BUILD = (
    "The README's install command, {command}, installed a ka-telemetry that is not the one the "
    "release check built from this checkout: {reason}. This bar measures what the README's "
    "command installs when what this checkout builds stands in for the package on the Python "
    "Package Index. Run the release check again."
)
EXIT_STATUS = "It exited with status {returncode}."
OUTPUT_SHOWN = {
    "stdout": ("It printed this on standard output:", "It printed nothing on standard output."),
    "stderr": ("It printed this on standard error:", "It printed nothing on standard error."),
}
OUTPUT_INDENT = "  "
INSTALL_STEP_MESSAGES = {
    "build": (
        f"{NOT_MEASURED} ka-telemetry could not be built from this checkout to install it. The "
        "build reported: {reason}. The build fetches its build backend with pip over the "
        "network. Run the release check again once the build can complete."
    ),
}
ATTEMPT_MESSAGES = {
    "open a connection": (
        "{command} tried to open a connection to {address}, port {port}. The release check "
        "refused it."
    ),
    "send a datagram": (
        "{command} tried to send a datagram to {address}, port {port}. The release check "
        "refused it."
    ),
    "look up the host name": (
        "{command} tried to look up the host name {address}. The release check refused the "
        "lookup."
    ),
    "look up the name of the address": (
        "{command} tried to look up the name of the address {address}. The release check "
        "refused the lookup."
    ),
}
ATTEMPT_KINDS = {
    "connect": "open a connection",
    "connect_ex": "open a connection",
    "sendto": "send a datagram",
    "sendmsg": "send a datagram",
    "getaddrinfo": "look up the host name",
    "gethostbyname": "look up the host name",
    "gethostbyaddr": "look up the name of the address",
    "getnameinfo": "look up the name of the address",
}
"""What a socket operation the watch recorded is called to a person: the four things CP-563 lists.
An operation not listed here is worded as a connection, the commonest."""
DEFAULT_ADDRESS_MESSAGE = (
    "The receiver, started by the release check with no --host, listened on the address "
    "{address}, port {port}. With no --host, this bar allows the receiver to listen only on an "
    "address on this machine, such as 127.0.0.1 or ::1, so the fix goes in ka-telemetry: make "
    "the receiver listen on 127.0.0.1 when it is given no --host, then run the release check "
    "again."
)
HELP_COMMAND_MESSAGES = {
    "help_incomplete": (
        f"{NOT_MEASURED} {{command}}, which the release check runs to find the commands it has "
        "to run, did not complete. It reported: {reason}. Run the release check again once that "
        "is resolved."
    ),
    "no_command_list": (
        f"{NOT_MEASURED} {{command}} did not print a list of commands that the release check "
        "could read, so it could not tell which commands to run. To see what it prints, run "
        "{command} from the top directory of this checkout."
    ),
}
COMMAND_MESSAGES = {
    "no_run": (
        "ka-telemetry --help lists {name}, but the release check has no run of {name} set up, "
        "so it cannot tell whether {name} tries to reach beyond this machine. Add a run of "
        "{name} to the release check, then run the release check again."
    ),
    "did_not_complete": (
        "{command} did not complete, so the release check could not watch all of what it does. "
        "It reported: {reason}. Run the release check again once that is resolved."
    ),
    "not_started": (
        "{command} could not be started, so the release check could not watch what it does. "
        "The system reported: {reason}. Run the release check again once that is resolved."
    ),
    "timed_out": (
        "{command} had not finished {seconds} seconds after it started, so the release check "
        "stopped it and could not watch all of what it does. Run the release check again."
    ),
    "record_unreadable": (
        "The release check could not read a complete record of what {command} tried to reach, "
        "so it cannot tell whether that command tried to reach beyond this machine. Run the "
        "release check again."
    ),
    "receiver_not_listening": (
        "The release check started the receiver with {command} and did not see it start "
        "listening, so it could not watch the receiver at work. The receiver printed: {reason}. "
        "Run the release check again once that is resolved."
    ),
    "receiver_request_not_accepted": (
        "The receiver started with {command} did not accept the request the release check sent "
        "to {path} from this machine, so the release check could not watch the receiver at "
        "work. The release check got back: {reason}. Run the release check again once that is "
        "resolved."
    ),
    "receiver_stopped_early": (
        "The receiver started with {command} stopped before the release check had sent it both "
        "of its requests, so the release check could not watch the receiver at work. The "
        "receiver printed: {reason}. Run the release check again once that is resolved."
    ),
}
COMMAND_MESSAGES.update(HELP_COMMAND_MESSAGES)


def _install_messages(diagnostic: release_metrics.Diagnostic) -> list[str]:
    """Packages asked for and packages installed are two whole messages, either or both."""
    data = diagnostic.data
    messages = []
    if data.get("requested"):
        messages.append(PACKAGES_ASKED_FOR.format(names=_names(data["requested"])))
    if data.get("installed"):
        messages.append(PACKAGES_INSTALLED.format(names=_names(data["installed"])))
    return messages


def _printed(data: Mapping) -> str:
    """What a program did, in the order the published words give it: its exit status, then what it
    printed on standard output, then on standard error, each line of an output on a line of its
    own and indented."""
    lines = [EXIT_STATUS.format(returncode=data.get("returncode"))]
    for channel, (printed, nothing) in OUTPUT_SHOWN.items():
        text = str(data.get(channel) or "").rstrip()
        if text.strip():
            lines.append(printed)
            lines.extend(OUTPUT_INDENT + line for line in text.splitlines())
        else:
            lines.append(nothing)
    return "\n".join(lines)


def _no_python_message(diagnostic: release_metrics.Diagnostic) -> str:
    lines = [NO_PYTHON_OPENING]
    for candidate in diagnostic.data.get("candidates", []):
        template = CANDIDATE_MESSAGES.get(candidate.get("outcome"))
        if template is not None:
            lines.append(OUTPUT_INDENT + template.format_map(_Blank(candidate)))
    lines.append(NO_PYTHON_CLOSING)
    return "\n".join(lines)


class _Blank(dict):
    """A mapping that answers an empty string for a figure it was not given."""

    def __missing__(self, key: str) -> str:
        return ""


def _bar_5_cause_message(diagnostic: release_metrics.Diagnostic) -> str | None:
    """The published words for a bar-5 diagnostic that carries a closed cause, or None."""
    data = diagnostic.data
    cause = data.get("cause")
    figures = _Blank(
        reason=diagnostic.reason, command=data.get("command"), line=data.get("line"),
        found=data.get("found"), expected=data.get("expected"), pipx=data.get("pipx"),
    )
    if cause == release_metrics.CAUSE_README_UNREADABLE:
        template = README_READINGS.get(data.get("reading"))
        return None if template is None else template.format_map(figures)
    if cause == release_metrics.CAUSE_COMMAND_REFUSED:
        return COMMAND_REFUSED.format_map(figures)
    if cause == release_metrics.CAUSE_PYTHON_UNAVAILABLE:
        return _no_python_message(diagnostic)
    if cause == release_metrics.CAUSE_PYTHON_MISMATCH:
        return PYTHON_MISMATCH.format_map(figures)
    if cause == release_metrics.CAUSE_PIPX_UNAVAILABLE:
        if "returncode" not in data:
            return PIPX_INSTALL_NOT_STARTED.format_map(figures)
        return "\n".join([PIPX_INSTALL_DID_NOT_SUCCEED, _printed(data), PIPX_INSTALL_CLOSING])
    if cause == release_metrics.CAUSE_PIPX_NOT_PREPARED:
        return PIPX_NOT_PREPARED.format_map(figures)
    if cause == release_metrics.CAUSE_TARGET_ABSENT:
        return "\n".join(
            [INSTALLED_NOTHING.format_map(figures), _printed(data), INSTALLED_NOTHING_CLOSING]
        )
    if cause == release_metrics.CAUSE_COMMAND_FAILED:
        return "\n".join(
            [
                COMMAND_DID_NOT_SUCCEED.format_map(figures),
                _printed(data),
                COMMAND_DID_NOT_SUCCEED_CLOSING,
            ]
        )
    simple = {
        release_metrics.CAUSE_COMMAND_NOT_STARTED: COMMAND_NOT_STARTED,
        release_metrics.CAUSE_LIST_UNREADABLE: LISTING_UNREADABLE,
        release_metrics.CAUSE_LIST_EMPTY: LISTING_GAVE_NOTHING,
        release_metrics.CAUSE_NOT_THIS_BUILD: NOT_THIS_BUILD,
    }.get(cause)
    return None if simple is None else simple.format_map(figures)


def _install_step_message(diagnostic: release_metrics.Diagnostic) -> str | None:
    said = _bar_5_cause_message(diagnostic)
    if said is not None:
        return said
    template = INSTALL_STEP_MESSAGES.get(diagnostic.data.get("step"))
    return None if template is None else template.format(reason=diagnostic.reason)


def _attempt_message(diagnostic: release_metrics.Diagnostic) -> str:
    data = diagnostic.data
    kind = ATTEMPT_KINDS.get(data.get("operation"), "open a connection")
    return ATTEMPT_MESSAGES[kind].format(
        command=diagnostic.input_or_operation,
        address=data.get("destination"),
        port=data.get("port"),
    )


def _command_message(diagnostic: release_metrics.Diagnostic) -> str | None:
    template = COMMAND_MESSAGES.get(diagnostic.data.get("cause"))
    if template is None:
        return None
    data = diagnostic.data
    return template.format(
        command=diagnostic.input_or_operation,
        reason=diagnostic.reason,
        name=data.get("name"),
        seconds=data.get("seconds"),
        path=data.get("path"),
    )


def _new_bar_messages(diagnostic: release_metrics.Diagnostic) -> list[str] | None:
    """The published words for the diagnostics of bars 5 and 7, or None for any other kind."""
    kind = diagnostic.kind
    if kind == release_metrics.KIND_INSTALL_PACKAGES:
        return _install_messages(diagnostic)
    if kind == release_metrics.KIND_INSTALL_TARGET_MISSING:
        said = _bar_5_cause_message(diagnostic)
        return None if said is None else [said]
    if kind == release_metrics.KIND_INSTALL_STEP_FAILED:
        said = _install_step_message(diagnostic)
        return None if said is None else [said]
    if kind == release_metrics.KIND_NETWORK_ATTEMPT:
        return [_attempt_message(diagnostic)]
    if kind == release_metrics.KIND_NETWORK_DEFAULT_ADDRESS:
        data = diagnostic.data
        return [DEFAULT_ADDRESS_MESSAGE.format(address=data.get("address"), port=data.get("port"))]
    if kind == release_metrics.KIND_NETWORK_UNMEASURED:
        said = _command_message(diagnostic)
        return None if said is None else [said]
    return None


def _message(diagnostic) -> str:
    """The published words for one diagnostic, without the bar's number in front."""
    kind, operation, reason, data = (
        diagnostic.kind,
        diagnostic.input_or_operation,
        diagnostic.reason,
        diagnostic.data,
    )
    if kind == KIND_NOT_PREPARED:
        return (
            f"{NOT_MEASURED} the release check could not prepare the temporary copy of this "
            f"checkout that it measures in. The system reported: {reason}. Run the release check "
            "again once that is resolved."
        )
    if kind == KIND_GIT_MISSING:
        return (
            f"{NOT_MEASURED} the release check needs Git to make its temporary copy of this "
            "checkout, and Git was not found. Install Git and run the release check again."
        )
    if kind == release_metrics.KIND_UNREADABLE_INPUT:
        return (
            f"{NOT_MEASURED} {operation} could not be read. The system reported: {reason}. Run "
            f"the release check again once {operation} can be read."
        )
    if kind == release_metrics.KIND_COMMAND_FAILED:
        return (
            f"{NOT_MEASURED} {operation} did not complete. It reported: {reason}. Run the "
            "release check again once that is resolved."
        )
    if kind == release_metrics.KIND_DIFFERENCE:
        return (
            f"{operation} does not match what ka-telemetry reports on the same data. The "
            f"difference: {reason}"
        )
    if kind in SESSION_MESSAGES:
        prefix = "Session " + data.get("session_id", operation) + ": "
        return prefix + SESSION_MESSAGES[kind]
    if kind == release_metrics.KIND_COUNTED_TWICE:
        return (
            f"The {data.get('report')} report counts response {data.get('response_id')} more "
            f"than once. It counted it from these transcripts: "
            f"{', '.join(data.get('transcripts', []))}."
        )
    if kind == release_metrics.KIND_COUNT_UNFINISHED:
        return (
            f"{NOT_MEASURED} counting the responses the {data.get('report')} report counts did "
            f"not finish. It reported: {reason}. Run the release check again once that is "
            "resolved."
        )
    if kind == release_metrics.KIND_EXAMPLE_FAILED:
        return (
            f"The test of the documented examples, {EXAMPLES_FILE}, failed on an example in "
            f"{data.get('document')}. Its report: {data.get('detail')}. To see the whole "
            f"failure, run {EXAMPLES_RUN} from the top directory of this checkout. Then change "
            "the example or ka-telemetry so that the example prints what is shown beside it."
        )
    if kind == release_metrics.KIND_EXAMPLE_FAILED_NO_DOCUMENT:
        return (
            f"The test of the documented examples, {EXAMPLES_FILE}, failed. Its report: "
            f"{data.get('detail')}. To see the whole failure, run {EXAMPLES_RUN} from the top "
            "directory of this checkout."
        )
    if kind == release_metrics.KIND_BUILD_FAILED:
        return (
            f"{NOT_MEASURED} ka-telemetry could not be built for the test of the documented "
            f"examples. The build reported: {reason}. The build fetches its build backend with "
            "pip over the network. Run the release check again once the build can complete."
        )
    if kind == release_metrics.KIND_PYTEST_MISSING:
        python = data.get("python")
        return (
            f"{NOT_MEASURED} pytest is not installed for {python}, the Python running the "
            f"release check. Install it with {python} -m pip install pytest and run the release "
            "check again."
        )
    said = reason.strip() if kind == release_metrics.KIND_STOPPED else f"{operation}: {reason}"
    if not said.strip(": "):
        return (
            f"{NOT_MEASURED} its measurement stopped without giving a result or a reason. Run "
            "the release check again."
        )
    return (
        f"{NOT_MEASURED} its measurement stopped without giving a result. It reported: "
        f"{said}. Run the release check again."
    )


def _lines(metric: int, text: str) -> list[str]:
    """One stderr line per line of `text`, each starting with the bar's number and a space."""
    return [f"{metric} {line}" for line in plain(text).splitlines() or [""]]


def explain(metric: int, result: release_metrics.WorkerResult) -> list[str]:
    """What standard error says about one bar that did not hold."""
    lines: list[str] = []
    last_closing = -1
    for diagnostic in result.diagnostics:
        for message in _new_bar_messages(diagnostic) or [_message(diagnostic)]:
            lines.extend(_lines(metric, message))
        if diagnostic.kind in CLOSED_BY.get(metric, ()):
            last_closing = len(lines)
    if last_closing >= 0:
        lines[last_closing:last_closing] = _lines(metric, CLOSING[metric])
    return lines


@dataclass(frozen=True)
class Report:
    """What the command prints and how it ends."""

    stdout_lines: list[str]
    stderr_lines: list[str]
    exit_status: int


def _unmeasured(metric: int) -> release_metrics.WorkerResult:
    return release_metrics.WorkerResult(
        metric,
        False,
        (release_metrics.Diagnostic(release_metrics.KIND_STOPPED, f"metric {metric}", ""),),
    )


def render_report(
    descriptions: Mapping[int, str], results: Mapping[int, release_metrics.WorkerResult]
) -> Report:
    """One stdout line for every registered bar, sorted by number, whatever its worker did, and
    the explanation of each bar that did not hold. Exit 0 only when every bar holds."""
    stdout, stderr, status = [], [], 0
    for metric in sorted(descriptions):
        result = results.get(metric) or _unmeasured(metric)
        stdout.append(f"{metric} {descriptions[metric]}: {'holds' if result.holds else 'fails'}")
        if not result.holds:
            status = 1
            stderr.extend(explain(metric, result))
    return Report(stdout, stderr, status)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        results = measure(CHECKOUT, Path(scratch), DESCRIPTIONS)
    report = render_report(DESCRIPTIONS, results)
    if report.stderr_lines:
        print("\n".join(report.stderr_lines), file=sys.stderr)
    print("\n".join(report.stdout_lines))
    return report.exit_status


if __name__ == "__main__":
    sys.exit(main())
