"""End-to-end test that the installed package is a library as well as a command.

The reader is imported from the installed distribution in a child interpreter run from outside the
checkout (`-I`, no current directory on the path), asked for the report in every mode, and its
figures and identities are held against what the installed command prints with `--json` for the
same inputs. The two are separate entry points over one reader, and they must not part.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from e2e.support import DATA_SET, run
from e2e.test_stream_parity import (
    MISMATCH_CAUSES,
    MISMATCH_SESSION,
    NO_METRIC_CAUSES,
    NO_METRIC_SESSION,
)

if TYPE_CHECKING:
    from conftest import InstalledProduct

PROBE = """
import json
import sys
from dataclasses import asdict

from ka_telemetry import stream_usage

spec = json.loads(sys.argv[1])
reader = stream_usage.build_reader(spec["directory"])
report = stream_usage.read_report(
    reader,
    prefix=spec["prefix"],
    where=tuple(tuple(pair) for pair in spec["where"]),
    comparison=tuple(spec["comparison"]) if spec["comparison"] else None,
)
if report is None:
    print(json.dumps(None))
else:
    print(json.dumps({
        "directory": str(reader.directory),
        "rows": [asdict(row) for row in report.rows],
        "subagent_rows": [asdict(row) for row in report.subagent_rows],
        "metrics_check": (
            None if report.metrics_check is None
            else [asdict(check) for check in report.metrics_check]
        ),
        "skipped_lines": report.skipped_lines,
        "skipped_metric_lines": report.skipped_metric_lines,
        "compare": None if report.compare is None else asdict(report.compare),
        "subagents_filtered_out": report.subagents_filtered_out,
        "metrics_state": report.metrics_state.value,
        "metrics_reason": report.metrics_reason,
    }))
"""

COMPARISON = ["kensai.arm", "two-arms-inline", "two-arms-subagent"]
ROW_IDENTITY = ("session_id", "workflow_run_id", "query_source", "agent_name")
ROW_FIGURES = (
    "models", "request_count", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "cost_usd", "duration_ms",
)


def _library(
    installed: InstalledProduct,
    tmp_path: Path,
    directory: Path,
    *,
    prefix: str | None = None,
    where: list[list[str]] | None = None,
    comparison: list[str] | None = None,
) -> Any:
    outside = tmp_path / "outside-the-checkout"
    outside.mkdir(exist_ok=True)
    spec = {
        "directory": str(directory), "prefix": prefix, "where": where or [],
        "comparison": comparison,
    }
    done = subprocess.run(
        [str(installed.python), "-I", "-c", PROBE, json.dumps(spec)],
        capture_output=True, text=True, check=False, cwd=outside,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _cli_json(installed, home, *arguments: str) -> dict:
    done = run(installed, *arguments, "--json", home=home)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _row_as_the_command_prints_it(row: dict, prefix: str | None) -> dict:
    printed = {key: row[key] for key in ROW_IDENTITY}
    if prefix is not None:
        printed[prefix] = row["labels"]
    printed.update({key: row[key] for key in ROW_FIGURES})
    return printed


def _group_as_the_command_prints_it(group: dict | None) -> dict | None:
    if group is None:
        return None
    return {
        "run_count": group["run_count"],
        "mean_cost_per_run": group["mean_cost_per_run"],
        "total_cost_usd": group["totals"]["cost_usd"],
        "totals": group["totals"],
        "mean_per_run": group["mean_per_run"],
    }


def _compare_as_the_command_prints_it(result: dict) -> dict:
    return {
        "attr": result["attr"],
        "a_value": result["a_value"],
        "b_value": result["b_value"],
        "a": _group_as_the_command_prints_it(result["a"]),
        "b": _group_as_the_command_prints_it(result["b"]),
        "diff": result["diff"],
        "ratio": result["ratio"],
    }


MODES = {
    "chosen prefix": dict(prefix="kensai"),
    "no prefix": dict(prefix=None),
    "prefix and a filter": dict(prefix="kensai", where=[["kensai.arm", "two-arms-inline"]]),
    "a filter on a request attribute": dict(prefix="kensai", where=[["query_source", "sdk"]]),
    "a comparison": dict(prefix="kensai", comparison=COMPARISON),
    "a comparison and a filter": dict(
        prefix="kensai", where=[["query_source", "sdk"]], comparison=COMPARISON
    ),
    "a comparison with no prefix": dict(prefix=None, comparison=COMPARISON),
}


def _command_arguments(mode: dict) -> list[str]:
    arguments: list[str] = []
    if mode.get("prefix") is not None:
        arguments += ["--prefix", mode["prefix"]]
    for key, value in mode.get("where", []):
        arguments += ["--where", f"{key}={value}"]
    if mode.get("comparison"):
        arguments += ["--compare", *mode["comparison"]]
    return arguments


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_library_matches_installed_cli_json_in_all_modes(installed, tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir()

    library = _library(installed, tmp_path, DATA_SET, **mode)
    command = _cli_json(
        installed, home, "agents", "--dir", str(DATA_SET), *_command_arguments(mode)
    )

    prefix = mode.get("prefix")
    assert library["rows"], "the mode selected no row, so it proves nothing"
    assert [_row_as_the_command_prints_it(r, prefix) for r in library["rows"]] == command["rows"]
    assert library["subagent_rows"] == command["subagent_rows"]
    assert library["metrics_check"] == command["metrics_check"]
    assert library["skipped_lines"] == command["skipped_lines"]
    # CP-330: the command carries the library's whole-file count under its own name, first.
    assert library["skipped_metric_lines"] == command["unreadable_metric_lines"]
    assert list(command)[0] == "unreadable_metric_lines"
    if mode.get("comparison"):
        assert _compare_as_the_command_prints_it(library["compare"]) == command["compare"]
    else:
        assert library["compare"] is None and "compare" not in command
    # The command adds costs with `math.fsum`, so the same total comes out on every Python.
    assert math.fsum(r["cost_usd"] for r in library["rows"]) == command["total_cost_usd"]


def test_the_library_labels_and_the_command_labels_name_the_same_attributes(
    installed, tmp_path
):
    home = tmp_path / "home"
    home.mkdir()

    library = _library(installed, tmp_path, DATA_SET, prefix="kensai")
    command = _cli_json(installed, home, "agents", "--dir", str(DATA_SET), "--prefix", "kensai")

    labelled = [row["labels"] for row in library["rows"] if row["labels"]]
    assert labelled and all(key.startswith("kensai.") for row in labelled for key in row)
    assert [row["kensai"] for row in command["rows"] if row["kensai"]] == labelled
    no_prefix = _library(installed, tmp_path, DATA_SET)
    assert all(row["labels"] == {} for row in no_prefix["rows"])


def _delivered_labels_stream(directory: Path, completed: tuple[str, ...] = ()) -> Path:
    """Three requests in three sessions, each carrying `team.note` as Claude Code delivers it: a
    NUL and four hex digits for each character the turn-on encodes."""
    directory.mkdir()
    notes = {
        "s-encoded": "two\u00000020words",
        "s-plain": "plain",
        "s-punct": 'a\u0000002Cb\u0000003Bc\u00000022d\u0000003De',
    }
    lines = []
    for session, note in notes.items():
        attributes = [
            {"key": "session.id", "value": {"stringValue": session}},
            {"key": "event.name", "value": {"stringValue": "api_request"}},
            {"key": "cost_usd", "value": {"doubleValue": 0.25}},
        ]
        resource = [{"key": "team.note", "value": {"stringValue": note}}]
        body = {
            "resourceLogs": [
                {
                    "resource": {"attributes": resource},
                    "scopeLogs": [{"logRecords": [{"attributes": attributes}]}],
                }
            ]
        }
        lines.append(json.dumps({"signal": "logs", "body": body}))
    for session in completed:
        attributes = [
            {"key": "session.id", "value": {"stringValue": session}},
            {"key": "event.name", "value": {"stringValue": "subagent_completed"}},
            {"key": "agent_type", "value": {"stringValue": "Explore"}},
        ]
        body = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [{"logRecords": [{"attributes": attributes}]}],
                }
            ]
        }
        lines.append(json.dumps({"signal": "logs", "body": body}))
    (directory / "logs.jsonl").write_text("".join(f"{line}\n" for line in lines))
    return directory


def test_the_library_reads_labels_as_typed_and_as_the_command_does(installed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stream = _delivered_labels_stream(tmp_path / "stream")
    mode = dict(
        prefix="team",
        where=[["team.note", "two words"]],
        comparison=["team.note", "two words", "plain"],
    )

    library = _library(installed, tmp_path, stream, **mode)
    command = _cli_json(installed, home, "agents", "--dir", str(stream), *_command_arguments(mode))

    assert [row["session_id"] for row in library["rows"]] == ["s-encoded"]
    assert library["rows"][0]["labels"] == {"team.note": "two words"}
    assert [row["team"] for row in command["rows"]] == [{"team.note": "two words"}]
    assert library["compare"]["a"]["run_count"] == 1
    assert library["compare"]["b"] is None
    assert _compare_as_the_command_prints_it(library["compare"]) == command["compare"]


def test_the_library_narrows_completed_agents_as_the_command_does(installed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stream = _delivered_labels_stream(tmp_path / "stream", completed=("s-encoded", "s-plain"))
    narrowed = dict(prefix="team", where=[["session.id", "s-plain"]])
    emptied = dict(prefix="team", where=[["session.id", "s-punct"]])

    library = _library(installed, tmp_path, stream, **narrowed)
    command = _cli_json(
        installed, home, "agents", "--dir", str(stream), *_command_arguments(narrowed)
    )
    nothing_selected = _library(installed, tmp_path, stream, **emptied)
    nothing_selected_command = _cli_json(
        installed, home, "agents", "--dir", str(stream), *_command_arguments(emptied)
    )
    unfiltered = _library(installed, tmp_path, stream, prefix="team")

    assert [row["session_id"] for row in library["subagent_rows"]] == ["s-plain"]
    assert library["subagent_rows"] == command["subagent_rows"]
    assert library["subagents_filtered_out"] is False
    assert nothing_selected["subagent_rows"] == nothing_selected_command["subagent_rows"] == []
    assert nothing_selected["subagents_filtered_out"] is True
    assert [row["session_id"] for row in unfiltered["subagent_rows"]] == ["s-encoded", "s-plain"]
    assert unfiltered["subagents_filtered_out"] is False
    assert "subagents_filtered_out" not in command


def test_library_checks_equal_the_installed_check_json(installed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    library = _library(installed, tmp_path, DATA_SET)
    command = _cli_json(installed, home, "check", "--dir", str(DATA_SET))

    # CP-289: the command's `check --json` carries the cost marking beside `metrics_check`; the
    # library's checks stay exactly what they were.
    assert command["metrics_check"] == library["metrics_check"]
    assert list(command) == [
        "unreadable_metric_lines", "metrics_file", "skipped_lines", "metrics_check",
        "cost_estimate_note",
    ]
    assert command["metrics_file"] == {"state": "read"}
    assert library["metrics_state"] == "read" and library["metrics_reason"] is None
    assert command["unreadable_metric_lines"] == library["skipped_metric_lines"] == 0
    assert command["skipped_lines"] == library["skipped_lines"] == 1
    assert "estimate" in command["cost_estimate_note"]
    assert library["metrics_check"], "the data set holds sessions to check"


def test_library_checks_carry_the_same_causes_as_check_json(installed, tmp_path):
    """CP-344: the library's checks carry `possible_causes`, a list once it is through JSON, and
    it equals the matching `check --json` entry for the session in every one of the three states."""
    home = tmp_path / "home"
    home.mkdir()

    library = _library(installed, tmp_path, DATA_SET)["metrics_check"]
    command = _cli_json(installed, home, "check", "--dir", str(DATA_SET))["metrics_check"]

    assert library == command
    causes = {entry["session_id"]: entry["possible_causes"] for entry in library}
    assert causes[NO_METRIC_SESSION] == NO_METRIC_CAUSES
    assert causes[MISMATCH_SESSION] == MISMATCH_CAUSES
    named = (NO_METRIC_SESSION, MISMATCH_SESSION)
    within = [found for session, found in causes.items() if session not in named]
    assert len(within) == 6 and all(c == [] for c in within)


def test_the_library_reader_expands_a_leading_tilde_and_the_default_directory(
    installed, tmp_path
):
    probe = (
        "import os; from ka_telemetry import stream_usage as s; "
        "print(s.build_reader('~/chosen').directory); print(s.build_reader().directory)"
    )
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        [str(installed.python), "-I", "-c", probe],
        capture_output=True, text=True, check=False, cwd=tmp_path, env={"HOME": str(home)},
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [str(home / "chosen"), str(home / ".ka-telemetry")]


def test_missing_logs_is_distinct_from_missing_metrics(installed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    no_logs = tmp_path / "no-logs"
    no_logs.mkdir()
    no_metrics = tmp_path / "no-metrics"
    no_metrics.mkdir()
    shutil.copy(DATA_SET / "logs.jsonl", no_metrics / "logs.jsonl")
    nothing_to_check = tmp_path / "empty-metrics"
    nothing_to_check.mkdir()
    (nothing_to_check / "logs.jsonl").write_text("")
    (nothing_to_check / "metrics.jsonl").write_text("")

    assert _library(installed, tmp_path, no_logs) is None
    assert _library(installed, tmp_path, tmp_path / "absent") is None
    without_metrics = _library(installed, tmp_path, no_metrics)
    with_empty_metrics = _library(installed, tmp_path, nothing_to_check)

    # CP-360: absent is a state of its own, with a check per session, and empty is another.
    assert without_metrics["rows"] and without_metrics["metrics_state"] == "nothing_received"
    assert without_metrics["metrics_check"] and without_metrics["metrics_reason"] is None
    assert with_empty_metrics["rows"] == [] and with_empty_metrics["metrics_check"] == []
    assert with_empty_metrics["metrics_state"] == "read"
    # The command tells the three apart too, and its entries are the library's.
    absent_document = _cli_json(installed, home, "check", "--dir", str(no_metrics))
    assert absent_document["metrics_file"] == {"state": "nothing_received"}
    assert absent_document["metrics_check"] == without_metrics["metrics_check"]
    assert absent_document["skipped_lines"] == 1
    assert _cli_json(installed, home, "check", "--dir", str(nothing_to_check)) == {
        "unreadable_metric_lines": 0,
        "metrics_file": {"state": "read"},
        "skipped_lines": 0,
        "metrics_check": [],
    }
    missing = run(installed, "check", "--dir", str(no_logs), "--json", home=home)
    document = json.loads(missing.stdout)
    assert (document["state"], document["directory"]) == ("nothing_received", str(no_logs))
    assert "rows" not in document and "metrics_check" not in document


COUNT_PROBE = """
import json
import sys
from dataclasses import asdict

from ka_telemetry import stream_usage

report = stream_usage.read_report(stream_usage.build_reader(sys.argv[1]))
print(json.dumps([
    {
        "figures": asdict(row),
        "invalid_duration_ms_count": row.invalid_duration_ms_count,
        "invalid_total_tool_uses_count": row.invalid_total_tool_uses_count,
    }
    for row in report.subagent_rows
]))
"""


def _completed_record(
    session: str, agent_type: str, duration: dict | None = None, tools: dict | None = None
) -> dict:
    """One `subagent_completed` record; a field passed as `None` is left out of it."""
    attributes = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "subagent_completed"}},
        {"key": "agent_type", "value": {"stringValue": agent_type}},
    ]
    for key, value in (("duration_ms", duration), ("total_tool_uses", tools)):
        if value is not None:
            attributes.append({"key": key, "value": value})
    return {"attributes": attributes}


def test_read_report_rows_carry_the_command_counts(installed, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stream = tmp_path / "stream"
    stream.mkdir()
    # The change spec's probe (§ Examples), completed records only: the record with neither field
    # counts nothing, and `"2"` is a number.
    records = [
        _completed_record("s1", "Explore", {"intValue": 100}, {"intValue": 3}),
        _completed_record("s1", "Explore", {"stringValue": "slow"}, {"boolValue": True}),
        _completed_record("s1", "Explore", {"boolValue": False}, {"stringValue": "many"}),
        _completed_record("s1", "Explore"),
        _completed_record("s1", "Explore", {"intValue": "oops"}, {"stringValue": "2"}),
        _completed_record("s1", "general-purpose", {"intValue": 50}, {"intValue": 1}),
        _completed_record("s2", "Explore", {"kvlistValue": {}}, {"intValue": 4}),
    ]
    scope = {"logRecords": records}
    body = {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [scope]}]}
    (stream / "logs.jsonl").write_text(json.dumps({"signal": "logs", "body": body}) + "\n")
    outside = tmp_path / "outside-the-checkout"
    outside.mkdir()

    done = subprocess.run(
        [str(installed.python), "-I", "-c", COUNT_PROBE, str(stream)],
        capture_output=True, text=True, check=False, cwd=outside,
    )
    assert done.returncode == 0, done.stderr
    library = json.loads(done.stdout)
    command = _cli_json(installed, home, "agents", "--dir", str(stream))["subagent_rows"]

    assert [
        (r["invalid_duration_ms_count"], r["invalid_total_tool_uses_count"]) for r in library
    ] == [(3, 2), (0, 0), (1, 0)]
    # The figures are the spec's: each is today's number, and the counts sit beside them.
    assert [
        (f["session_id"], f["agent_type"], f["count"], f["duration_ms"], f["total_tool_uses"])
        for f in (r["figures"] for r in library)
    ] == [
        ("s1", "Explore", 5, 100, 5),
        ("s1", "general-purpose", 1, 50, 1),
        ("s2", "Explore", 1, 0, 4),
    ]
    # The original five fields are compared on their own: the counts are not among them.
    assert [r["figures"] for r in library] == [
        {k: v for k, v in entry.items() if k != "invalid_values"} for entry in command
    ]
    # The printed counts are the library's counts, for every row.
    for row, entry in zip(library, command, strict=True):
        printed = entry.get("invalid_values", {})
        assert printed.get("duration_ms", 0) == row["invalid_duration_ms_count"]
        assert printed.get("total_tool_uses", 0) == row["invalid_total_tool_uses_count"]


def test_library_comparison_equals_the_installed_json_over_a_dirty_stream(installed, tmp_path):
    """The library's `compare` carries the counts the command prints, over a stream where one side
    left values out, one side is clean and a third value matches no request."""
    home = tmp_path / "home"
    home.mkdir()
    stream = tmp_path / "dirty"
    stream.mkdir()

    def request(arm: str, session: str, cost: dict, tokens: dict) -> dict:
        names = ("session.id", "event.name", "model", "query_source", "arm")
        values = (session, "api_request", "claude-model", "sdk", arm)
        attributes = [
            {"key": k, "value": {"stringValue": v}} for k, v in zip(names, values, strict=True)
        ]
        attributes.append({"key": "cost_usd", "value": cost})
        attributes.append({"key": "input_tokens", "value": tokens})
        return {"attributes": attributes}

    records = [
        request("A", "s1", {"doubleValue": 0.25}, {"intValue": 4}),
        request("A", "s2", {"stringValue": "abc"}, {"boolValue": True}),
        request("A", "s2", {"stringValue": "x"}, {"intValue": 6}),
        request("B", "s3", {"doubleValue": 0.5}, {"intValue": 2}),
    ]
    scope = {"logRecords": records}
    body = {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [scope]}]}
    (stream / "logs.jsonl").write_text(json.dumps({"signal": "logs", "body": body}) + "\n")
    comparison = ["arm", "A", "B"]

    library = _library(installed, tmp_path, stream, comparison=comparison)["compare"]
    command = _cli_json(installed, home, "agents", "--dir", str(stream), "--compare", *comparison)

    assert library["a"]["invalid_values"] == {"input_tokens": 1, "cost_usd": 2}
    assert library["b"]["invalid_values"] == {}
    assert command["compare"]["a"]["invalid_values"] == library["a"]["invalid_values"]
    assert "invalid_values" not in command["compare"]["b"]
    for side in ("a", "b"):
        assert _group_as_the_command_prints_it(library[side]) == {
            key: value
            for key, value in command["compare"][side].items()
            if key != "invalid_values"
        }

    empty = ["arm", "A", "nothing"]
    library_empty = _library(installed, tmp_path, stream, comparison=empty)["compare"]
    command_empty = _cli_json(installed, home, "agents", "--dir", str(stream), "--compare", *empty)
    assert library_empty["b"] is None and command_empty["compare"]["b"] is None
    assert library_empty["a"]["invalid_values"] == command_empty["compare"]["a"]["invalid_values"]
