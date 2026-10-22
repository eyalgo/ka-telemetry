"""Tests for `ka_telemetry.cli` — the one `ka-telemetry` command and its three subcommands.

The grammar is exercised in-process through `cli.main(argv)`: a usage error is a `SystemExit`
with code 2 and the parser's text on stderr. The installed command repeats help and one misuse in
`tests/test_package.py`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ka_telemetry import cli, flow_usage, run_usage

TOP_NEXT_STEP = "Run `ka-telemetry --help` to see the commands and what each one accepts."
SUBCOMMANDS = ("runs", "steps", "context")


def _next_step(command: str) -> str:
    return f"Run `ka-telemetry {command} --help` to see what it accepts."


def _argv(command: str, *rest: str) -> list[str]:
    """`command` with its required arguments, plus `rest` (context needs a transcript path)."""
    required = ["transcript.jsonl"] if command == "context" else []
    return [command, *required, *rest]


def _response(message_id: str, **classes: int) -> str:
    usage = {
        "input_tokens": classes.get("input_tokens", 0),
        "cache_read_input_tokens": classes.get("cache_read", 0),
        "cache_creation_input_tokens": classes.get("cache_creation", 0),
        "output_tokens": 999,
    }
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage}})


def _transcript(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                _response("m1", input_tokens=10, cache_read=100, cache_creation=1000),
                _response("m2", input_tokens=20, cache_read=200, cache_creation=2000),
                _response("m3", input_tokens=30, cache_read=300, cache_creation=3000),
            ]
        )
        + "\n"
    )
    return path


def _usage_error(argv: list[str], capsys) -> str:
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


def test_help_names_runs_steps_and_context(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert text.startswith("usage: ka-telemetry")
    for words in (
        "Reads the transcripts Claude Code keeps on this machine",
        "tokens used by each background run, and by each run you name by session id",
        "tokens used by each step of a workflow run",
        "the current context size of one transcript",
    ):
        assert words in text
    # The promise is the last thing on the screen, in the instance's fixed words.
    assert text.endswith(
        "This tool sends no telemetry and makes no network call except the ones the job you "
        "asked for requires."
    )


def test_missing_subcommand_exits_2_with_usage_on_stderr(capsys):
    err = _usage_error([], capsys)

    assert err.startswith("usage: ka-telemetry")
    assert "ka-telemetry: error:" in err
    assert err.splitlines()[-1] == TOP_NEXT_STEP


def test_unknown_subcommand_exits_2(capsys):
    err = _usage_error(["frobnicate"], capsys)

    assert err.startswith("usage: ka-telemetry")
    assert err.splitlines()[-1] == TOP_NEXT_STEP


def test_unknown_flag_exits_2_on_each_subcommand(capsys):
    assert _next_step("runs") != TOP_NEXT_STEP
    for command in SUBCOMMANDS:
        err = _usage_error(_argv(command, "--bogus"), capsys)

        assert err.startswith(f"usage: ka-telemetry {command}")
        assert err.splitlines()[-1] == _next_step(command)
    assert _usage_error(["--bogus"], capsys).splitlines()[-1] == TOP_NEXT_STEP


def test_removed_run_logs_flag_exits_2(capsys):
    err = _usage_error(["runs", "--run-logs", "logs/"], capsys)

    assert "--run-logs" in err
    assert err.splitlines()[-1] == _next_step("runs")


@pytest.mark.parametrize(
    "argv",
    [
        ["runs", "--agents", "x.json"],
        ["runs", "--projects", "x"],
        ["runs", "--sess", "sid"],
        ["runs", "--js"],
        ["steps", "--projects", "x"],
        ["steps", "--w", "wf_x"],
        ["steps", "--js"],
        ["context", "transcript.jsonl", "--js"],
    ],
)
def test_abbreviated_flag_is_rejected_on_each_subcommand(argv, capsys):
    err = _usage_error(argv, capsys)

    assert "unrecognized arguments" in err


def test_abbreviated_help_flag_is_not_help(capsys):
    # `--he` is a prefix of `--help`: accepted, it would print the help screen and exit 0.
    err = _usage_error(["--he"], capsys)

    assert err.splitlines()[-1] == TOP_NEXT_STEP


def test_context_requires_a_transcript_path(capsys):
    for argv in (["context"], ["context", "--json"]):
        err = _usage_error(argv, capsys)

        assert "TRANSCRIPT" in err
        assert err.splitlines()[-1] == _next_step("context")


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["runs", "--wf", "wf_x"], "runs"),
        (["runs", "--projects-root", "x", "--wf", "wf_x"], "runs"),
        (["steps", "--session", "sid"], "steps"),
        (["steps", "--agents-json", "x.json"], "steps"),
        (["context", "t.jsonl", "--projects-root", "x"], "context"),
        (["context", "t.jsonl", "--session", "sid"], "context"),
    ],
)
def test_runs_and_steps_flags_stay_separate(argv, command, capsys):
    err = _usage_error(argv, capsys)

    assert err.splitlines()[-1] == _next_step(command)


def test_runs_and_steps_flags_each_work_on_their_own_command(tmp_path, capsys):
    listing = tmp_path / "agents.json"
    listing.write_text("[]")

    assert cli.main(["runs", "--agents-json", str(listing), "--projects-root", str(tmp_path)]) == 0
    assert "No runs read." in capsys.readouterr().out
    assert cli.main(["steps", "--projects-root", str(tmp_path), "--wf", "wf_none"]) == 1
    assert "no workflow run found for --wf wf_none" in capsys.readouterr().err


def test_context_table_and_unknown_line(tmp_path, capsys):
    transcript = _transcript(tmp_path / "t.jsonl")

    assert cli.main(["context", str(transcript)]) == 0
    assert capsys.readouterr().out == "Context size: 3330 tokens\n"

    assert cli.main(["context", "absent.jsonl"]) == 0
    assert capsys.readouterr().out == (
        "Context size: unknown — no assistant usage read from absent.jsonl\n"
    )


def test_context_json_echoes_the_path_as_given_and_uses_null_for_unknown(tmp_path, capsys):
    transcript = _transcript(tmp_path / "t.jsonl")

    assert cli.main(["context", str(transcript), "--json"]) == 0
    assert capsys.readouterr().out == (
        '{\n  "transcript": ' + json.dumps(str(transcript)) + ',\n  "context_size": 3330,\n'
        '  "unreadable_lines": 0\n}\n'
    )

    assert cli.main(["context", "./sub/../absent.jsonl", "--json"]) == 0
    assert capsys.readouterr().out == (
        '{\n  "transcript": "./sub/../absent.jsonl",\n  "context_size": null,\n'
        '  "unreadable_lines": 0\n}\n'
    )


def test_context_json_equals_library_value(tmp_path, capsys):
    from ka_telemetry import context_gauge

    transcript = _transcript(tmp_path / "t.jsonl")
    cli.main(["context", str(transcript), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["context_size"] == context_gauge.current_context(transcript, run_usage.Files())


def test_human_and_json_outputs_share_one_reader_result(tmp_path, capsys):
    transcript = _transcript(tmp_path / "t.jsonl")
    cli.main(["context", str(transcript)])
    table = capsys.readouterr().out
    cli.main(["context", str(transcript), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert table == f"Context size: {payload['context_size']} tokens\n"

    run_dir = tmp_path / "slug" / "sid-1" / "subagents" / "workflows" / "wf_one"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1"})
        + "\n"
        + json.dumps({"type": "result", "agentId": "a1", "result": "no step"})
        + "\n"
    )
    (run_dir / "agent-a1.jsonl").write_text(_response("m1", input_tokens=9) + "\n")
    cli.main(["steps", "--projects-root", str(tmp_path)])
    steps_table = capsys.readouterr().out
    cli.main(["steps", "--projects-root", str(tmp_path), "--json"])
    steps_json = json.loads(capsys.readouterr().out)
    spend = steps_json["runs"][0]["rows"][0]["spend"]
    assert f"input={spend[next(iter(spend))]['input']} " in steps_table


def test_runs_and_steps_output_is_the_readers_own(tmp_path, capsys):
    listing = tmp_path / "agents.json"
    listing.write_text("[]")
    runs_argv = ["--agents-json", str(listing), "--projects-root", str(tmp_path), "--json"]
    steps_argv = ["--projects-root", str(tmp_path)]

    run_usage.main(runs_argv)
    expected_runs = capsys.readouterr().out
    flow_usage.main(steps_argv)
    expected_steps = capsys.readouterr().out

    assert cli.main(["runs", *runs_argv]) == 0
    assert capsys.readouterr().out == expected_runs
    assert cli.main(["steps", *steps_argv]) == 0
    assert capsys.readouterr().out == expected_steps


def test_cli_adds_no_second_counting_or_grouping_path():
    tree = ast.parse(Path(cli.__file__).read_text())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    assert not defined & {"usage_records", "spend_by_model", "merge_spend", "aggregate", "ledger"}
    assert {"run_usage.main", "flow_usage.main", "context_gauge.current_context"} <= called


# ── `context` states its size beside what it could not read ──

BAD_JSON = "not json {"
LINES_SKIPPED_ONE = " — 1 unreadable line skipped in this transcript"
LINES_SKIPPED_MANY = " — {count} unreadable lines skipped in this transcript"


def _context_run(argv: list[str], capsys) -> tuple[int, str, str]:
    status = cli.main(["context", *argv])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _with_bad_lines(path: Path, count: int) -> Path:
    path.write_text(path.read_text() + "".join(f"{BAD_JSON}\n" for _ in range(count)))
    return path


@pytest.mark.parametrize("count", [0, 1, 3])
def test_context_text_and_json_with_zero_one_and_many_unreadable_lines(tmp_path, capsys, count):
    known = _with_bad_lines(_transcript(tmp_path / "known.jsonl"), count)
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(f"{BAD_JSON}\n" * count)
    clause = {0: "", 1: LINES_SKIPPED_ONE}.get(count, LINES_SKIPPED_MANY.format(count=count))

    status, out, err = _context_run([str(known)], capsys)
    assert (status, err) == (0, "")
    assert out == f"Context size: 3330 tokens{clause}\n"

    status, out, err = _context_run([str(unknown)], capsys)
    assert (status, err) == (0, "")
    assert out == f"Context size: unknown — no assistant usage read from {unknown}{clause}\n"

    for transcript, size in ((known, 3330), (unknown, None)):
        status, out, err = _context_run([str(transcript), "--json"], capsys)
        payload = json.loads(out)
        assert (status, err) == (0, "")
        assert list(payload) == ["transcript", "context_size", "unreadable_lines"]
        assert (payload["context_size"], payload["unreadable_lines"]) == (size, count)


def test_context_directory_has_its_own_text_and_count_one(tmp_path, capsys):
    directory = tmp_path / "t.jsonl"
    directory.mkdir()

    status, out, err = _context_run([str(directory)], capsys)

    assert (status, err) == (0, "")
    assert out == (
        f"Context size: unknown — the transcript {directory} could not be opened. "
        "Run this command again once it can be read.\n"
    )
    assert "no assistant usage read" not in out
    assert "unreadable line" not in out

    status, out, err = _context_run([str(directory), "--json"], capsys)

    assert (status, err) == (0, "")
    assert json.loads(out) == {
        "transcript": str(directory),
        "context_size": None,
        "unreadable_lines": 0,
        "unreadable_transcripts": 1,
    }


def test_context_echoes_a_non_normalized_path_as_given_in_the_new_cases(tmp_path, capsys):
    # CP-435: "`{path}` is the `TRANSCRIPT` argument as given" — in the unreadable-line case and
    # the could-not-open case too, not only the old ones. `./` and `..` are kept as typed; a
    # path rebuilt through `Path` would drop the `./`.
    (tmp_path / "sub").mkdir()
    only_bad = tmp_path / "only-bad.jsonl"
    only_bad.write_text(f"{BAD_JSON}\n")
    (tmp_path / "dir.jsonl").mkdir()
    given_bad = f"{tmp_path}/./sub/../only-bad.jsonl"
    given_dir = f"{tmp_path}/./sub/../dir.jsonl"

    status, out, err = _context_run([given_bad], capsys)
    assert (status, err) == (0, "")
    assert out == (
        f"Context size: unknown — no assistant usage read from {given_bad}{LINES_SKIPPED_ONE}\n"
    )

    status, out, err = _context_run([given_dir], capsys)
    assert (status, err) == (0, "")
    assert out == (
        f"Context size: unknown — the transcript {given_dir} could not be opened. "
        "Run this command again once it can be read.\n"
    )

    for given in (given_bad, given_dir):
        status, out, err = _context_run([given, "--json"], capsys)
        assert (status, err) == (0, "")
        assert json.loads(out)["transcript"] == given


def test_context_absent_and_not_a_directory_keep_the_old_text(tmp_path, capsys):
    regular = tmp_path / "regular"
    regular.write_text("x")
    for given in ("absent.jsonl", f"{regular}/x.jsonl"):
        status, out, err = _context_run([given], capsys)
        assert (status, err) == (0, "")
        assert out == f"Context size: unknown — no assistant usage read from {given}\n"

        status, out, err = _context_run([given, "--json"], capsys)
        assert (status, err) == (0, "")
        assert json.loads(out) == {"transcript": given, "context_size": None, "unreadable_lines": 0}


def test_context_wiring_passes_current_context_as_the_size_reader():
    class ReadLinesOnly:
        def read_lines(self, path):
            return [_response("m1", input_tokens=2, cache_read=3, cache_creation=4)]

    assert cli.context_size_reader(Path("t.jsonl"), ReadLinesOnly()) == 9

    tree = ast.parse(Path(cli.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    scalar_calls = [c for c in calls if ast.unparse(c.func) == "context_gauge.current_context"]
    (rich_call,) = [c for c in calls if ast.unparse(c.func) == "context_gauge.read_context"]
    assert len(scalar_calls) == 1  # the one call is the helper's: nothing calls it to be seen
    assert {kw.arg: ast.unparse(kw.value) for kw in rich_call.keywords} == {
        "size_reader": "context_size_reader"
    }
