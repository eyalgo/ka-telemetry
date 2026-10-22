"""End-to-end tests of the installed `ka-telemetry` command's three stream jobs.

`receive`, `agents` and `check` are driven as a person drives them: the command from the scratch
virtualenv, a real receiver on a real port, and a child-process `HOME` of its own, so the default
directory is the one under that home and never the machine's.
"""

from __future__ import annotations

import difflib
import json
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import (
    DATA_SET,
    PRODUCT_ROOT,
    Receiver,
    api_request_body,
    free_port,
    needs_history,
    neutral,
    run,
)

if TYPE_CHECKING:
    from conftest import InstalledProduct

COPY_COMMIT = "91d580f"
JOBS = ("runs", "steps", "context", "receive", "agents", "check")
NETWORK_PROMISE = (
    "This tool sends no telemetry and makes no network call except the ones the job you asked "
    "for requires."
)
TOP_DESCRIPTION = (
    "Reads the transcripts Claude Code keeps on this machine and reports the tokens they "
    "record: per background run, per workflow step, and the current context size of one "
    "transcript. Those three reports show no cost, because ka-telemetry counts tokens and "
    "does not price them. It also receives the telemetry Claude Code can send as OpenTelemetry "
    "over HTTP in JSON, writes it to files on this machine, and reads those files for a "
    "per-agent report of tokens, cost and duration, and for a totals check that holds each "
    "session's request costs against Claude Code's cost metric. Every cost in those two comes "
    "from what Claude Code reported; ka-telemetry never computes a cost from tokens."
)
JOB_LINES = (
    "receive the telemetry Claude Code sends and write it to files on this machine",
    "tokens, cost and duration per agent, read from the received telemetry",
    "each session's request costs checked against Claude Code's cost metric",
)
RESERVED_PREFIXES = (
    "session_id", "workflow_run_id", "query_source", "agent_name", "models", "request_count",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd",
    "duration_ms",
)


def _words(text: str) -> str:
    return " ".join(text.split())


def _next_step(command: str) -> str:
    return f"Run `ka-telemetry {command} --help` to see what it accepts."


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), *args], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _changed_lines(path: str) -> list[str]:
    """The lines added (`+`) and removed (`-`) in `path` since COPY_COMMIT, that commit's bytes
    first given their neutral names."""
    before = neutral(_git("show", f"{COPY_COMMIT}:{path}") + "\n").splitlines()
    after = (PRODUCT_ROOT / path).read_text().splitlines()
    return [
        line
        for line in difflib.unified_diff(before, after, n=0, lineterm="")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


# ── The package and the help ────────────────────────────────────────────────


def test_help_lists_six_jobs_and_exact_final_network_sentence(installed, home):
    result = run(installed, "--help", home=home)

    assert result.returncode == 0, result.stderr
    text = _words(result.stdout)
    assert "{runs,steps,context,receive,agents,check,turn-on}" in text
    assert _words(TOP_DESCRIPTION) in text
    for line in JOB_LINES:
        assert _words(line) in text
    # The three existing jobs keep their own lines, in the order the placeholder gives.
    assert text.index("tokens used by each background run") < text.index(JOB_LINES[0])
    assert text.endswith(NETWORK_PROMISE)


def test_empty_venv_install_adds_only_the_distribution(installed, home):
    assert installed.installed - installed.baseline == {"ka-telemetry"}
    # Nothing else was installed, and each stream job still starts in that empty virtualenv.
    for job in ("receive", "agents", "check"):
        result = run(installed, job, "--help", home=home)

        assert result.returncode == 0, (job, result.stderr)
        assert result.stdout.startswith(f"usage: ka-telemetry {job}")


# ── Usage errors ────────────────────────────────────────────────────────────

BAD_USAGE = [
    ("receive", []),
    ("receive", ["--dir", "x"]),
    ("receive", ["--port", "0", "--bogus"]),
    ("receive", ["--po", "0"]),
    ("receive", ["--port", "0", "--pub", "x"]),
    ("receive", ["--port"]),
    ("receive", ["--port", "not-a-number"]),
    ("receive", ["--port", "0", "--host"]),
    ("receive", ["--port", "0", "stray"]),
    ("agents", ["--bogus"]),
    ("agents", ["--di", "x"]),
    ("agents", ["--pre", "x"]),
    ("agents", ["--dir"]),
    ("agents", ["--compare", "attr", "a"]),
    ("agents", ["--where"]),
    ("agents", ["stray"]),
    ("check", ["--bogus"]),
    ("check", ["--di", "x"]),
    ("check", ["--dir"]),
    ("check", ["--prefix", "team"]),
    ("check", ["--where", "a=b"]),
    ("check", ["--compare", "attr", "a", "b"]),
    ("check", ["stray"]),
]


@pytest.mark.parametrize(("command", "arguments"), BAD_USAGE)
def test_usage_errors_across_new_commands(installed, home, command, arguments):
    result = run(installed, command, *arguments, home=home)

    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0].startswith(f"usage: ka-telemetry {command}")
    assert f"ka-telemetry {command}: error:" in result.stderr
    assert lines[-1] == _next_step(command)


# ── The default directory ───────────────────────────────────────────────────


def test_default_directory_end_to_end_under_isolated_home(installed, home, tmp_path):
    default = home / ".ka-telemetry"
    assert not default.exists()

    with Receiver(installed, home, scratch=tmp_path) as receiver:
        assert receiver.post("logs", api_request_body("session-default", 0.25)) == 200
    assert (default / "logs.jsonl").is_file()

    agents = run(installed, "agents", home=home)
    check = run(installed, "check", home=home)

    assert agents.returncode == 0, agents.stderr
    assert agents.stdout.startswith("1 agent row(s), 0 unreadable line(s) skipped.")
    assert "session=session-default" in agents.stdout
    assert "cost_usd=0.250000" in agents.stdout
    assert check.returncode == 0
    # CP-358, CP-364: the directory the absence line names is the expanded default one.
    assert check.stdout.splitlines()[0] == (
        f"No metrics.jsonl was found in {default}, "
        "so no session has a cost metric to check against."
    )
    assert "session=session-default" in check.stdout


@pytest.mark.parametrize("spelling", ["~/chosen", "~/nested/chosen"])
def test_an_explicit_directory_with_a_leading_tilde_overrides_the_default(
    installed, home, tmp_path, spelling
):
    chosen = home / spelling.removeprefix("~/")

    with Receiver(installed, home, "--dir", spelling, scratch=tmp_path) as receiver:
        assert receiver.post("logs", api_request_body("session-chosen", 0.5)) == 200

    assert (chosen / "logs.jsonl").is_file()
    assert not (home / ".ka-telemetry").exists()
    for job in ("agents", "check"):
        result = run(installed, job, "--dir", spelling, home=home)
        assert result.returncode == 0, result.stderr
    assert "session=session-chosen" in run(installed, "agents", "--dir", spelling, home=home).stdout
    # Without --dir the same records are not found: the default directory holds none.
    assert run(installed, "agents", home=home).stdout.splitlines()[0] == (
        f"No stream at {home / '.ka-telemetry'} — no logs.jsonl there."
    )


def test_an_explicit_directory_overrides_the_default_when_both_hold_records(
    installed, home, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    with Receiver(installed, home, scratch=tmp_path) as receiver:
        receiver.post("logs", api_request_body("session-default", 1.0))
    with Receiver(installed, home, "--dir", str(elsewhere), scratch=tmp_path) as receiver:
        receiver.post("logs", api_request_body("session-elsewhere", 2.0))

    assert "session-default" in run(installed, "agents", home=home).stdout
    other = run(installed, "agents", "--dir", str(elsewhere), home=home).stdout
    assert "session-elsewhere" in other
    assert "session-default" not in other


# ── The receiver's flags ────────────────────────────────────────────────────


def test_receiver_flags_through_installed_command(installed, home, tmp_path):
    with Receiver(installed, home, "--dir", str(tmp_path / "out"), scratch=tmp_path) as receiver:
        assert receiver.port > 0
        assert receiver.endpoint_file.read_text() == f"http://localhost:{receiver.port}\n"
        # The URL it published is the port it is listening on.
        assert receiver.post("logs", api_request_body("session-flags", 0.1)) == 200


def test_receiver_without_publish_endpoint_writes_no_file_and_prints_only_the_start_line(
    installed, home, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    port = free_port()
    receiver = Receiver(
        installed, home, "--dir", str(tmp_path / "out"),
        scratch=work, publish=False, port=port,
    )

    with receiver:
        assert receiver.post("logs", api_request_body("session-quiet", 0.1)) == 200
        assert list(work.iterdir()) == []
    assert receiver.stdout.endswith("\n") and len(receiver.stdout.splitlines()) == 1


def _address_other_than_loopback() -> str | None:
    try:
        address = socket.gethostbyname(socket.gethostname())
    except OSError:
        return None
    return None if address.startswith("127.") else address


def test_receiver_listens_on_loopback_only_unless_a_host_is_given(installed, home, tmp_path):
    other = _address_other_than_loopback()
    if other is None:
        pytest.skip("this machine has no address other than loopback to test against")
    port = free_port()
    with Receiver(installed, home, scratch=tmp_path, publish=False, port=port):
        with pytest.raises(OSError):
            socket.create_connection((other, port), timeout=2).close()

    with Receiver(installed, home, "--host", "0.0.0.0", scratch=tmp_path, publish=False, port=port):
        socket.create_connection((other, port), timeout=5).close()


# ── Prefix validation, missing logs, a malformed filter ─────────────────────

PREFIX_REFUSALS = [
    (
        "",
        "ka-telemetry agents: error: --prefix is empty; give the text your label keys start "
        "with, without the dot after it — for labels such as team.run_id, use --prefix team",
    ),
    (
        "team.",
        "ka-telemetry agents: error: --prefix 'team.' ends with a dot; give it without the dot "
        "— for labels such as team.run_id, use --prefix team",
    ),
    *[
        (
            name,
            f"ka-telemetry agents: error: --prefix {name!r} is also the name of a field in each "
            "row of the --json output, so labels under it cannot be shown; use --where to "
            "select requests by those attributes instead",
        )
        for name in RESERVED_PREFIXES
    ],
]


@pytest.mark.parametrize(("prefix", "message"), PREFIX_REFUSALS)
def test_cli_prefix_validation_exit_2_with_no_stdout(installed, home, prefix, message):
    result = run(installed, "agents", "--dir", str(DATA_SET), "--prefix", prefix, home=home)

    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[-2] == message
    assert lines[-1] == _next_step("agents")


def test_a_valid_unrelated_prefix_is_accepted(installed, home):
    result = run(installed, "agents", "--dir", str(DATA_SET), "--prefix", "team", home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("16 agent row(s), 1 unreadable line(s) skipped.")


@pytest.mark.parametrize("command", ["agents", "check"])
@pytest.mark.parametrize("as_json", [False, True])
def test_missing_logs_say_which_directory_was_read(installed, home, tmp_path, command, as_json):
    """CP-325, CP-326: every form names the directory it read; the text carries the no-stream line
    first, and the JSON carries the directory as its own member."""
    flags = ["--json"] if as_json else []
    empty = tmp_path / "no-logs-here"
    empty.mkdir()
    named = {
        "absent": (["--dir", str(tmp_path / "absent")], tmp_path / "absent"),
        "present": (["--dir", str(empty)], empty),
        "tilde": (["--dir", "~/no-such-directory"], home / "no-such-directory"),
        "default": ([], home / ".ka-telemetry"),
    }

    for arguments, directory in named.values():
        done = run(installed, command, *arguments, *flags, home=home)
        assert done.returncode == 0
        if as_json:
            assert json.loads(done.stdout)["directory"] == str(directory)
        else:
            assert done.stdout.splitlines()[0] == (
                f"No stream at {directory} — no logs.jsonl there."
            )


def test_an_empty_logs_file_is_a_different_outcome_from_a_missing_one(installed, home, tmp_path):
    (tmp_path / "logs.jsonl").write_text("")

    result = run(installed, "agents", "--dir", str(tmp_path), home=home)

    assert result.returncode == 0
    assert result.stdout.startswith("0 agent row(s), 0 unreadable line(s) skipped.")
    assert "No stream at" not in result.stdout


@pytest.mark.parametrize("where_directory", ["data-set", "empty", "absent"])
def test_malformed_where_exits_1_with_the_quoted_argument(
    installed, home, tmp_path, where_directory
):
    (tmp_path / "empty").mkdir()
    directory = {
        "data-set": DATA_SET, "empty": tmp_path / "empty", "absent": tmp_path / "absent",
    }[where_directory]

    result = run(installed, "agents", "--dir", str(directory), "--where", "bad", home=home)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: --where expects KEY=VALUE, got 'bad'\n"


def test_a_where_with_an_empty_value_or_an_equals_inside_is_accepted(installed, home):
    for pair in ("model=", "a=b=c"):
        result = run(installed, "agents", "--dir", str(DATA_SET), "--where", pair, home=home)

        assert result.returncode == 0, (pair, result.stderr)


def test_mismatch_totals_still_exit_0(installed, home):
    check = run(installed, "check", "--dir", str(DATA_SET), home=home)
    agents = run(installed, "agents", "--dir", str(DATA_SET), "--prefix", "kensai", home=home)

    assert (check.returncode, agents.returncode) == (0, 0)
    assert "MISMATCH >1%" in check.stdout
    assert "no metric export" in check.stdout


# ── The transcript jobs are untouched ───────────────────────────────────────


def test_existing_transcript_commands_unchanged():
    """`tests/parity/` is byte-identical to the commit this one was copied from, so every assertion
    it holds still stands as written. Three things are left out. The stored text outputs of `runs`
    and `steps` gained their one no-cost line, and `tests/test_parity.py` runs the commands under
    an empty home and carries that line through its expectations; neither's figures change, and the
    stored JSON outputs stay inside the comparison. `tests/test_cli.py` is left out too: the
    transcript reports' own change (CP-436) retires its whole-object expectation of `context
    --json`, which now always carries `unreadable_lines`, so that one test is edited on purpose."""
    needs_history()
    paths = (
        "tests/parity",
        ":(exclude)tests/parity/expected/runs.txt",
        ":(exclude)tests/parity/expected/runs.json",
        ":(exclude)tests/parity/expected/steps.txt",
        ":(exclude)tests/parity/expected/steps.json",
    )

    # Against the working tree, so an edit that is not committed yet is seen too. The data set's
    # neutral names are the one change allowed: each file of the commit, renamed, is here unchanged.
    left_out = {path.removeprefix(":(exclude)") for path in paths[1:]}
    held = _git("ls-files", "--", *paths).splitlines()
    recorded = [
        path
        for path in _git("ls-tree", "-r", "--name-only", COPY_COMMIT, "--", paths[0]).splitlines()
        if path not in left_out
    ]
    assert sorted(held) == sorted(neutral(path) for path in recorded)
    for path in recorded:
        assert (PRODUCT_ROOT / neutral(path)).read_bytes() == neutral(
            _git("show", f"{COPY_COMMIT}:{path}") + "\n"
        ).encode(), path
    # The stored `steps --json` stays inside the comparison but for the one member the step span
    # adds (`steps says how long each step took`) and the one statement of what the time covers:
    # nothing is removed or changed, and every line added is one of those.
    added = _changed_lines("tests/parity/expected/runs.json")
    assert len(added) == 1 and added[0].startswith('+  "span": ')
    changed = _changed_lines("tests/parity/expected/steps.json")
    assert changed and all(
        line.startswith(('+          "span_seconds": ', '+  "span": ')) for line in changed
    )
    assert sum(line.startswith('+  "span": ') for line in changed) == 1
