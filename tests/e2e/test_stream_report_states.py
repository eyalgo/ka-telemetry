"""End-to-end tests of the states the stream reports name: the installed `ka-telemetry agents`
and `check` run over real files, and what a person reads is asserted.

Each group is a rule of the change that made the reports say what they hold: cost marked as Claude
Code's estimate (CP-287, CP-288, CP-289, CP-290), nothing received (CP-291), a session with no cost
metric (CP-292, CP-293), a `custom` agent (CP-294), an unreadable metrics line (CP-295 to CP-298),
and what did not move (CP-299 to CP-303). The exact words are `change_text.md`'s; the tests here
assert the properties the rules fix, and pin a string only where the rule fixes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import DATA_SET, api_request_body, diff, run
from e2e.test_stream_parity import (
    COST_MARKING,
    COST_NOTE_MEMBER,
    MISMATCH_CAUSES,
    MISMATCH_REASON,
    LOGS_COVERAGE_LINE as CHECK_LOGS_COVERAGE_TEMPLATE,
    MISMATCH_STORED_LINE,
    NO_METRIC_CAUSES,
    NO_METRIC_REASON,
    NO_METRIC_SESSION,
    NO_METRIC_STORED_LINE,
    SKIPPED_LINES_MEMBER,
    UNREADABLE_COUNT_MEMBER,
    _cut_declared_member,
    _totals_block,
    agents_text_without_declared_lines,
    check_text_without_declared_lines,
    current_heading_in,
    json_without_declared_members,
)

if TYPE_CHECKING:
    pass

EXPECTED = DATA_SET / "expected"
PREFIX = "kensai"
COMPARISON = ("kensai.arm", "two-arms-inline", "two-arms-subagent")
NO_METRICS_LINE = "No metrics.jsonl found — totals check skipped."
METRICS_FILE_MEMBER = "metrics_file"
CHECK_HEADING = "Totals check (claude_code.cost.usage vs summed cost_usd):"


def _absent_line(directory: Path) -> str:
    """CP-358's absence line, written out from `change_text.md`."""
    return (
        f"No metrics.jsonl was found in {directory}, "
        "so no session has a cost metric to check against."
    )


def _unopenable_metrics_statement(directory: Path, reason: str) -> str:
    """CP-351's statement, written out from `change_text.md`."""
    return (
        f"There is a metrics.jsonl at {directory}, but ka-telemetry could not read it: {reason}. "
        "No session was checked in the totals check. "
        "Run this command again once metrics.jsonl can be read."
    )


def _undetermined_metrics_statement(directory: Path, reason: str) -> str:
    """CP-352's statement, written out from `change_text.md`."""
    return (
        f"In {directory}, ka-telemetry could not read metrics.jsonl: {reason}. "
        "It could not tell whether metrics.jsonl is there either. "
        "No session was checked in the totals check. Run this command again once "
        "ka-telemetry can read metrics.jsonl or tell that it is not there."
    )
# `check`'s `logs.jsonl` count line, word for word from `change_text.md`, with `{n}` for the count.
CHECK_LOGS_COVERAGE = CHECK_LOGS_COVERAGE_TEMPLATE.replace("{count}", "{n}")
NO_SESSIONS_LINE = "  No sessions to check."


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _stored(name: str) -> str:
    return (EXPECTED / name).read_text()


def _agents(installed, home, directory: Path, *arguments: str):
    return run(installed, "agents", "--dir", str(directory), *arguments, home=home)


def _check(installed, home, directory: Path, *arguments: str):
    return run(installed, "check", "--dir", str(directory), *arguments, home=home)


def _request_line(session_id: str, cost: float = 0.25) -> str:
    return json.dumps({"signal": "logs", "body": api_request_body(session_id, cost)})


def _cost_export_line(session_id: str, value: float) -> str:
    point = {
        "attributes": [{"key": "session.id", "value": {"stringValue": session_id}}],
        "asDouble": value,
    }
    metric = {
        "name": "claude_code.cost.usage",
        "sum": {"aggregationTemporality": 1, "dataPoints": [point]},
    }
    body = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [metric]}]}]}
    return json.dumps({"signal": "metrics", "body": body})


NO_COST_EXPORT = json.dumps(
    {"signal": "metrics", "body": {"resourceMetrics": [{"scopeMetrics": [{"metrics": []}]}]}}
)


def _stream(
    directory: Path, logs: list[str] | None, metrics: list[str] | None = None
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if logs is not None:
        (directory / "logs.jsonl").write_text("".join(f"{line}\n" for line in logs))
    if metrics is not None:
        (directory / "metrics.jsonl").write_text("".join(f"{line}\n" for line in metrics))
    return directory


def _estimate_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "estimate" in line]


def _marking_follows_total(text: str) -> None:
    lines = text.splitlines()
    totals = [i for i, line in enumerate(lines) if line.startswith("Total cost_usd: ")]
    assert len(totals) == 1, text
    assert len(_estimate_lines(text)) == 1, f"CP-287: one marking line, no more: {text}"
    assert "estimate" in lines[totals[0] + 1], f"CP-287: marking right after the total: {text}"


# ── A — cost is shown as Claude Code's estimate (CP-287 to CP-290) ───────────


def test_agents_text_prints_marking_once_after_total(installed, home):
    done = _agents(installed, home, DATA_SET, "--prefix", PREFIX)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "16 agent row(s), 1 unreadable line(s) skipped."
    assert "Total cost_usd: 1.313953" in done.stdout.splitlines()
    _marking_follows_total(done.stdout)


def test_agents_text_with_zero_rows_still_prints_marking(installed, home):
    done = _agents(installed, home, DATA_SET, "--where", "session.id=nothing")

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[:2] == [
        "0 agent row(s), 1 unreadable line(s) skipped.",
        "Total cost_usd: 0.000000",
    ]
    _marking_follows_total(done.stdout)


def test_compare_text_has_one_marking_and_unchanged_block(installed, home):
    done = _agents(installed, home, DATA_SET, "--prefix", PREFIX, "--compare", *COMPARISON)

    assert done.returncode == 0, done.stderr
    _marking_follows_total(done.stdout)
    stored = _stored("compare.txt")
    block = stored[stored.index("--compare ") :]
    assert done.stdout[done.stdout.index("--compare ") :] == block


@pytest.mark.parametrize(
    "logs, metrics",
    [
        pytest.param(["not json", "[1, 2]"], None, id="logs-hold-only-unreadable-lines"),
        pytest.param([_request_line("s")], None, id="metrics-missing"),
        pytest.param([_request_line("s")], [], id="metrics-empty"),
        pytest.param([_request_line("s")], [NO_COST_EXPORT], id="metrics-without-cost-points"),
        pytest.param([], None, id="no-request-records"),
    ],
)
def test_agents_marks_with_unreadable_logs_and_missing_empty_or_costless_metrics(
    installed, home, tmp_path, logs, metrics
):
    directory = _stream(tmp_path / "stream", logs, metrics)

    done = _agents(installed, home, directory)

    assert done.returncode == 0, done.stderr
    _marking_follows_total(done.stdout)


def test_check_text_marking_is_last_line_exactly_once(installed, home):
    done = _check(installed, home, DATA_SET)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert "estimate" in lines[-1], f"CP-288: the marking is the last line of check: {lines[-1]!r}"
    assert len(_estimate_lines(done.stdout)) == 1, "CP-288: the marking is printed once"
    assert lines[0] == CHECK_LOGS_COVERAGE.format(n=1), "CP-356: the logs.jsonl count stands first"
    assert lines[1].startswith("Totals check")


def test_check_without_metrics_or_without_sessions_prints_no_marking(installed, home, tmp_path):
    without_metrics = _stream(tmp_path / "no-metrics", [_request_line("s")])
    without_sessions = _stream(tmp_path / "no-sessions", [], [NO_COST_EXPORT])

    missing = _check(installed, home, without_metrics)
    empty = _check(installed, home, without_sessions)

    assert missing.returncode == 0 and empty.returncode == 0
    # CP-358: an absent file no longer skips the check, so the session it names gets the marking.
    assert missing.stdout.splitlines()[0] == _absent_line(without_metrics)
    assert missing.stdout.splitlines()[-1] == COST_MARKING
    assert NO_SESSIONS_LINE in empty.stdout.splitlines()
    assert "estimate" not in empty.stdout


def test_marking_text_is_identical_in_agents_and_check(installed, home):
    agents = _agents(installed, home, DATA_SET, "--prefix", PREFIX)
    check = _check(installed, home, DATA_SET)

    (in_agents,) = _estimate_lines(agents.stdout)
    in_check = check.stdout.splitlines()[-1]
    assert in_agents == in_check
    assert "estimate" in in_agents and "US dollars" in in_agents and "None is a bill" in in_agents
    assert in_agents == COST_MARKING


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([], id="plain"),
        pytest.param(["--compare", *COMPARISON], id="with-compare"),
        pytest.param(["--where", "session.id=nothing"], id="zero-rows"),
        pytest.param(
            ["--where", "session.id=nothing", "--compare", *COMPARISON],
            id="zero-rows-with-compare",
        ),
    ],
)
def test_agents_json_carries_cost_estimate_note_last(installed, home, arguments):
    done = _agents(installed, home, DATA_SET, "--prefix", PREFIX, "--json", *arguments)

    assert done.returncode == 0, done.stderr
    document = json.loads(done.stdout)
    assert list(document)[-1] == COST_NOTE_MEMBER, "CP-289: the cost note is the last member"
    assert isinstance(document[COST_NOTE_MEMBER], str)
    assert document[COST_NOTE_MEMBER] == COST_MARKING, "CP-289: the note is the marking"
    assert "estimate" in document[COST_NOTE_MEMBER]


def test_agents_json_note_with_the_compare_member_stands_after_it(installed, home):
    done = _agents(
        installed, home, DATA_SET, "--prefix", PREFIX, "--json", "--compare", *COMPARISON
    )

    keys = list(json.loads(done.stdout))
    assert keys[-2:] == ["compare", COST_NOTE_MEMBER]


@pytest.mark.parametrize("alteration", ["indentation", "numeric-spelling"])
def test_json_parity_rejects_format_changes_even_when_values_match(alteration):
    document = {UNREADABLE_COUNT_MEMBER: 0, **json.loads(_stored("agents.json"))}
    document[COST_NOTE_MEMBER] = COST_MARKING
    printed = json.dumps(document, indent=2) + "\n"
    if alteration == "indentation":
        changed = json.dumps(document, indent=4) + "\n"
    else:
        number = re.search(r"(?<=: )\d+\.\d+(?=,?\n)", printed)
        assert number is not None
        changed = printed[:number.end()] + "0" + printed[number.end():]
    assert changed != printed and json.loads(changed) == document

    with pytest.raises(AssertionError) as failure:
        json_without_declared_members(changed)

    assert "---" in str(failure.value) and "+++" in str(failure.value)


@pytest.mark.parametrize("metrics", [None, []], ids=["metrics-missing", "metrics-empty"])
def test_agents_json_carries_the_note_without_metrics(installed, home, tmp_path, metrics):
    directory = _stream(tmp_path / "stream", [_request_line("s")], metrics)

    document = json.loads(_agents(installed, home, directory, "--json").stdout)

    assert document[COST_NOTE_MEMBER] == COST_MARKING


def test_check_json_carries_the_same_member_only_for_non_empty_metrics_check(
    installed, home, tmp_path
):
    with_sessions = json.loads(_check(installed, home, DATA_SET, "--json").stdout)
    no_metrics = _stream(tmp_path / "no-metrics", [_request_line("s")])
    no_sessions = _stream(tmp_path / "no-sessions", [], [NO_COST_EXPORT])

    missing = _check(installed, home, no_metrics, "--json")
    empty = _check(installed, home, no_sessions, "--json")

    assert list(with_sessions) == [
        UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER, SKIPPED_LINES_MEMBER, "metrics_check",
        COST_NOTE_MEMBER,
    ]
    assert with_sessions[COST_NOTE_MEMBER] == COST_MARKING
    absent = {"state": "nothing_received"}
    missing_document = json.loads(missing.stdout)
    # CP-359: an absent file lists its sessions, so the member and the marking stand beside it.
    assert list(missing_document) == list(with_sessions)
    assert missing_document[METRICS_FILE_MEMBER] == absent
    assert [entry["session_id"] for entry in missing_document["metrics_check"]] == ["s"]
    counted_empty = {
        UNREADABLE_COUNT_MEMBER: 0, METRICS_FILE_MEMBER: {"state": "read"},
        SKIPPED_LINES_MEMBER: 0, "metrics_check": [],
    }
    assert empty.stdout == json.dumps(counted_empty, indent=2) + "\n"


def test_json_carries_no_explanation_or_reason_and_the_count_alone(installed, home):
    stored = json.loads(_stored("agents.json"))

    agents = json.loads(_agents(installed, home, DATA_SET, "--prefix", PREFIX, "--json").stdout)
    check = json.loads(_check(installed, home, DATA_SET, "--json").stdout)

    assert list(agents) == [UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER, *stored, COST_NOTE_MEMBER]
    assert list(check) == [
        UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER, SKIPPED_LINES_MEMBER, "metrics_check",
        COST_NOTE_MEMBER,
    ]


def test_every_figure_is_unchanged(installed, home):
    stored = _stored("compare.txt")
    figures = [
        line
        for line in stored.splitlines()
        if "cost_usd" in line or "mean_cost_per_run" in line or " - " in line or " / " in line
    ]
    assert figures, "the stored output carries cost figures"

    printed = _agents(installed, home, DATA_SET, "--prefix", PREFIX, "--compare", *COMPARISON)
    document = json.loads(
        _agents(
            installed, home, DATA_SET, "--prefix", PREFIX, "--json", "--compare", *COMPARISON
        ).stdout
    )
    stored_document = json.loads(_stored("compare.json"))

    # The declared additions (the marking, the no-metric reason) come out first; every stored
    # figure line is then still printed exactly as stored.
    printed_lines = agents_text_without_declared_lines(printed.stdout).splitlines()
    assert all(line in printed_lines for line in figures)
    assert document[METRICS_FILE_MEMBER] == {"state": "read"}
    declared = {COST_NOTE_MEMBER, UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER}
    for entry in document["metrics_check"]:
        _cut_declared_member(entry)
    assert {k: v for k, v in document.items() if k not in declared} == stored_document


def test_a_one_millionth_cost_change_still_fails_the_parity_comparison_naming_the_line():
    stored = _stored("agents.txt")
    lines = current_heading_in(stored).splitlines(keepends=True)
    total = next(i for i, line in enumerate(lines) if line.startswith("Total cost_usd: "))
    printed = "".join([*lines[: total + 1], COST_MARKING + "\n", *lines[total + 1 :]])
    printed = printed.replace(
        NO_METRIC_STORED_LINE + "\n", NO_METRIC_STORED_LINE + NO_METRIC_REASON + "\n"
    ).replace(MISMATCH_STORED_LINE + "\n", MISMATCH_STORED_LINE + MISMATCH_REASON + "\n")
    changed = printed.replace("cost_usd=0.950151", "cost_usd=0.950152", 1)
    assert changed != printed

    remaining = agents_text_without_declared_lines(changed)
    shown = diff(stored, remaining)

    assert remaining != stored
    assert "-  session=ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593" in shown
    assert "cost_usd=0.950151" in shown and "cost_usd=0.950152" in shown
    assert "estimate" not in shown


# ── C — a session with no cost metric to check against (CP-292, CP-293) ──────


def _no_metric_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith(f"  session={NO_METRIC_SESSION}  log_cost_usd=")
    ]


def test_no_metric_session_line_keeps_prefix_and_names_three_causes(installed, home):
    done = _check(installed, home, DATA_SET)

    assert done.returncode == 0, done.stderr
    (line,) = _no_metric_lines(done.stdout)
    assert line.startswith(NO_METRIC_STORED_LINE), "CP-338: the stored line is the prefix"
    assert line == NO_METRIC_STORED_LINE + NO_METRIC_REASON, "CP-338: then the causes"
    assert all(cause in line for cause in NO_METRIC_CAUSES), "CP-338: all three causes"


def test_reason_is_identical_in_agents_and_check(installed, home):
    agents = _agents(installed, home, DATA_SET, "--prefix", PREFIX)
    check = _check(installed, home, DATA_SET)

    assert _no_metric_lines(agents.stdout) == _no_metric_lines(check.stdout)
    assert len(_no_metric_lines(check.stdout)) == 1


def test_no_metric_session_is_not_a_mismatch(installed, home):
    text = _check(installed, home, DATA_SET).stdout
    as_json = json.loads(_check(installed, home, DATA_SET, "--json").stdout)

    (line,) = _no_metric_lines(text)
    assert "MISMATCH" not in line
    mismatched = [row for row in text.splitlines() if "MISMATCH >1%" in row]
    assert len(mismatched) == 1 and "ee5d2a51" in mismatched[0]
    (entry,) = [e for e in as_json["metrics_check"] if e["session_id"] == NO_METRIC_SESSION]
    assert entry == {
        "session_id": NO_METRIC_SESSION,
        "log_cost_usd": 0.050332699999999994,
        "metric_cost_usd": None,
        "within_tolerance": None,
        "possible_causes": NO_METRIC_CAUSES,
    }


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param([_cost_export_line("another", 0.5)], id="only-another-session"),
        pytest.param([NO_COST_EXPORT], id="no-cost-point"),
        pytest.param([], id="empty"),
    ],
)
def test_a_session_without_a_cost_point_gets_the_three_causes(installed, home, tmp_path, metrics):
    directory = _stream(tmp_path / "stream", [_request_line("s", 0.5)], metrics)

    line = _check(installed, home, directory).stdout.splitlines()[1]

    assert line == f"  session=s  log_cost_usd=0.500000  no metric export{NO_METRIC_REASON}"


def test_a_zero_valued_metric_is_a_metric_not_an_absence(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "stream", [_request_line("s", 0.0)], [_cost_export_line("s", 0.0)]
    )

    line = _check(installed, home, directory).stdout.splitlines()[1]

    assert line == "  session=s  log_cost_usd=0.000000  metric_cost_usd=0.000000"


# ── D — a custom agent shows as `custom` (CP-294) ────────────────────────────

CUSTOM_EXPLANATION = (
    "Claude Code reports an agent you defined yourself as custom, and names it only when Claude "
    "Code's detailed tool logging is on. To tell such agents apart, label your runs with "
    "ka-telemetry turn-on --prefix PREFIX --label KEY=VALUE and read this report with --prefix "
    "PREFIX: rows from runs you labelled differently are shown apart."
)


def _records_line(records: list[dict]) -> str:
    """One logs export holding one OTLP log record per mapping of attribute name to value."""

    def attribute(key: str, value: object) -> dict:
        kind = "intValue" if isinstance(value, int) else "stringValue"
        return {"key": key, "value": {kind: value}}

    log_records = [{"attributes": [attribute(k, v) for k, v in r.items()]} for r in records]
    body = {"resourceLogs": [{"scopeLogs": [{"logRecords": log_records}]}]}
    return json.dumps({"signal": "logs", "body": body})


def _agent_request(session: str, agent: str | None) -> dict:
    record: dict = {"event.name": "api_request", "session.id": session, "query_source": "sdk"}
    if agent is not None:
        record["agent.name"] = agent
    return record


def _agent_tool(session: str, agent_type: str) -> dict:
    return {"event.name": "subagent_completed", "session.id": session, "agent_type": agent_type}


def _explanations(text: str) -> list[str]:
    return [line for line in text.splitlines() if "custom" in line.lower() and "label" in line]


def test_custom_agent_row_prints_one_explanation_after_the_marking_line(installed, home, tmp_path):
    directory = _stream(tmp_path / "s", [_records_line([_agent_request("s1", "custom")])])

    done = _agents(installed, home, directory)

    assert _explanations(done.stdout) == [CUSTOM_EXPLANATION], "CP-294: one explanation is printed"
    lines = done.stdout.splitlines()
    marking = lines.index(COST_MARKING)
    assert lines[marking + 1] == CUSTOM_EXPLANATION, "CP-294: the explanation follows the marking"
    assert lines[marking + 2] == ""
    assert _explanations(done.stdout) == [CUSTOM_EXPLANATION]


def test_custom_only_in_an_agent_tool_row_prints_one_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s1", "named"), _agent_tool("s1", "custom")])],
    )

    done = _agents(installed, home, directory)

    assert _explanations(done.stdout) == [CUSTOM_EXPLANATION]
    assert done.stdout.splitlines().index(CUSTOM_EXPLANATION) > 0


def test_custom_agent_tool_without_requests_still_gets_explained(installed, home, tmp_path):
    directory = _stream(tmp_path / "s", [_records_line([_agent_tool("s1", "custom")])])

    done = _agents(installed, home, directory)

    assert (done.returncode, done.stderr) == (0, "")
    lines = done.stdout.splitlines()
    assert lines[0] == "0 agent row(s), 0 unreadable line(s) skipped."
    assert "agent_type=custom" in done.stdout
    assert _explanations(done.stdout) == [CUSTOM_EXPLANATION], "CP-294: printed tool rows count"
    assert lines[lines.index(COST_MARKING) + 1] == CUSTOM_EXPLANATION


def test_custom_agent_tool_left_out_by_where_is_neither_printed_nor_explained(
    installed, home, tmp_path
):
    directory = _stream(tmp_path / "s", [_records_line([_agent_tool("s1", "custom")])])

    done = _agents(installed, home, directory, "--where", "session.id=other")

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines()[0] == "0 agent row(s), 0 unreadable line(s) skipped."
    assert "agent_type=custom" not in done.stdout
    assert _explanations(done.stdout) == [], "CP-320: the explanation follows the rows printed"


def test_custom_in_both_prints_one_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s1", "custom"), _agent_tool("s1", "custom")])],
    )

    assert _explanations(_agents(installed, home, directory).stdout) == [CUSTOM_EXPLANATION]


def test_explanation_states_all_three_things_and_never_says_to_enable_logging(
    installed, home, tmp_path
):
    directory = _stream(tmp_path / "s", [_records_line([_agent_request("s1", "custom")])])

    (line,) = _explanations(_agents(installed, home, directory).stdout)

    assert "agent you defined yourself" in line and "custom" in line
    assert "names it only when" in line and "detailed tool logging is on" in line
    assert "label your runs" in line and "ka-telemetry turn-on" in line
    for instruction in ("enable", "turn on tool logging", "turn the logging on", "turn it on"):
        assert instruction not in line.lower()


def test_filtering_away_the_custom_agent_row_removes_the_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s1", "custom"), _agent_request("s2", "named")])],
    )

    done = _agents(installed, home, directory, "--where", "session.id=s2")

    assert _explanations(done.stdout) == []
    assert "estimate" in done.stdout


def test_excluded_custom_agent_tool_row_brings_no_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s2", "named"), _agent_tool("s1", "custom")])],
    )

    done = _agents(installed, home, directory, "--where", "session.id=s2")

    assert _explanations(done.stdout) == []


def test_custom_agent_tool_row_in_a_shown_session_brings_one_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s2", "named"), _agent_tool("s2", "custom")])],
    )

    done = _agents(installed, home, directory, "--where", "session.id=s2")

    assert _explanations(done.stdout) == [CUSTOM_EXPLANATION]


@pytest.mark.parametrize(
    "records",
    [
        pytest.param([_agent_request("s", "Custom")], id="capitalised"),
        pytest.param([_agent_request("s", None)], id="no-agent-name"),
        pytest.param([_agent_request("s", "customer")], id="longer-name"),
        pytest.param([_agent_tool("s", "Custom")], id="capitalised-agent-tool"),
    ],
)
def test_capitalised_and_absent_names_print_no_explanation(installed, home, tmp_path, records):
    directory = _stream(tmp_path / "s", [_records_line(records)])

    assert _explanations(_agents(installed, home, directory).stdout) == []


def test_clean_input_prints_no_explanation(installed, home):
    done = _agents(installed, home, DATA_SET, "--prefix", PREFIX)

    assert _explanations(done.stdout) == []
    assert CUSTOM_EXPLANATION not in done.stdout


def test_json_carries_no_explanation(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "s",
        [_records_line([_agent_request("s1", "custom"), _agent_tool("s1", "custom")])],
    )

    plain = _agents(installed, home, directory, "--json").stdout
    compared = _agents(
        installed, home, directory, "--json", "--compare", "session.id", "s1", "x"
    ).stdout

    assert "custom" not in json.loads(plain)[COST_NOTE_MEMBER]
    for printed in (plain, compared):
        assert CUSTOM_EXPLANATION not in printed
        assert "label your runs" not in printed


# ── E — a metrics record ka-telemetry cannot read (CP-295 to CP-298) ─────────

AGENTS_COVERAGE = (
    "Unreadable lines in metrics.jsonl, also skipped and left out of the totals check: {n}."
)
CHECK_COVERAGE = (
    "Unreadable lines in metrics.jsonl, skipped and left out of the totals check: {n}."
)


def _copy_with_unreadable_metrics(directory: Path, *extra: str) -> Path:
    """`<M>`: a copy of the stored data set with unreadable lines appended to `metrics.jsonl`."""
    directory.mkdir(parents=True)
    (directory / "logs.jsonl").write_bytes((DATA_SET / "logs.jsonl").read_bytes())
    stored = (DATA_SET / "metrics.jsonl").read_text()
    appended = "".join(f"{line}\n" for line in (extra or ("not json {",)))
    (directory / "metrics.jsonl").write_text(stored + appended)
    return directory


def test_agents_first_line_extends_only_after_the_unchanged_logs_count(installed, home, tmp_path):
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    done = _agents(installed, home, copy, "--prefix", PREFIX)

    assert done.returncode == 0, done.stderr
    first = done.stdout.splitlines()[0]
    assert first.startswith("16 agent row(s), 1 unreadable line(s) skipped.")
    assert "metrics.jsonl" in first
    assert first == "16 agent row(s), 1 unreadable line(s) skipped. " + AGENTS_COVERAGE.format(
        n=1
    ), "CP-296: the first line goes on to state the metrics.jsonl count"


def test_agents_first_line_is_unchanged_for_clean_missing_or_zero_count(installed, home, tmp_path):
    blank_only = _stream(tmp_path / "blank", [_request_line("s")], ["", "   "])
    missing = _stream(tmp_path / "missing", [_request_line("s")])

    clean = _agents(installed, home, DATA_SET, "--prefix", PREFIX).stdout.splitlines()[0]

    assert clean == "16 agent row(s), 1 unreadable line(s) skipped."
    for directory in (blank_only, missing):
        first = _agents(installed, home, directory).stdout.splitlines()[0]
        assert first == "1 agent row(s), 0 unreadable line(s) skipped."


def test_check_prepends_a_coverage_line_stating_the_count(installed, home, tmp_path):
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    done = _check(installed, home, copy)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == CHECK_COVERAGE.format(n=1), "CP-297: the coverage line stands first"
    assert lines[1] == CHECK_LOGS_COVERAGE.format(n=1), "CP-356: the logs.jsonl line comes next"
    assert lines[2].startswith("Totals check")
    assert lines[-1] == COST_MARKING


def test_check_without_unreadable_lines_has_no_coverage_line(installed, home):
    done = _check(installed, home, DATA_SET)

    assert done.stdout.splitlines()[0] == CHECK_LOGS_COVERAGE.format(n=1)
    assert done.stdout.splitlines()[1].startswith("Totals check")
    assert "Unreadable lines in metrics.jsonl" not in done.stdout


def _copy_without_second_logs_line(directory: Path) -> Path:
    """`<Z>`: a copy of the stored data set without the one `logs.jsonl` line `agents` skips."""
    directory.mkdir(parents=True)
    lines = (DATA_SET / "logs.jsonl").read_text().splitlines(keepends=True)
    del lines[1]
    (directory / "logs.jsonl").write_text("".join(lines))
    (directory / "metrics.jsonl").write_bytes((DATA_SET / "metrics.jsonl").read_bytes())
    return directory


def test_cp356_zero_count_prints_no_logs_line_in_the_installed_check(installed, home, tmp_path):
    copy = _copy_without_second_logs_line(tmp_path / "z")

    done = _check(installed, home, copy)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0].startswith("Totals check")
    assert "Unreadable lines in logs.jsonl" not in done.stdout


def test_cp357_installed_check_json_skipped_lines_equals_agents_and_is_0_when_none(
    installed, home, tmp_path
):
    copy = _copy_without_second_logs_line(tmp_path / "z")

    for directory, expected in ((DATA_SET, 1), (copy, 0)):
        check = json.loads(_check(installed, home, directory, "--json").stdout)
        agents = json.loads(_agents(installed, home, directory, "--json").stdout)
        assert check[SKIPPED_LINES_MEMBER] == agents[SKIPPED_LINES_MEMBER] == expected
        assert list(check)[:3] == [
            UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER, SKIPPED_LINES_MEMBER,
        ]


def test_multiple_unreadable_lines_state_the_count(installed, home, tmp_path):
    copy = _copy_with_unreadable_metrics(tmp_path / "m", "not json {", "[1, 2]", '{"body": {}}', "")

    agents = _agents(installed, home, copy, "--prefix", PREFIX).stdout.splitlines()[0]
    check = _check(installed, home, copy).stdout.splitlines()[0]

    assert agents.endswith(AGENTS_COVERAGE.format(n=3))
    assert check == CHECK_COVERAGE.format(n=3)


def test_unreadable_line_changes_no_row_session_line_or_figure(installed, home, tmp_path):
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    clean_agents = _agents(installed, home, DATA_SET, "--prefix", PREFIX)
    agents = _agents(installed, home, copy, "--prefix", PREFIX)
    clean_check = _check(installed, home, DATA_SET)
    check = _check(installed, home, copy)

    assert agents.returncode == 0 and check.returncode == 0
    assert agents.stdout.splitlines()[1:] == clean_agents.stdout.splitlines()[1:]
    assert check.stdout.splitlines()[1:] == clean_check.stdout.splitlines()
    clean_json = json.loads(_check(installed, home, DATA_SET, "--json").stdout)
    copy_json = json.loads(_check(installed, home, copy, "--json").stdout)
    # CP-330, CP-331: the two differ by the count alone — 0 against 1 — and by nothing else.
    assert (clean_json.pop(UNREADABLE_COUNT_MEMBER), copy_json.pop(UNREADABLE_COUNT_MEMBER)) == (
        0,
        1,
    )
    assert copy_json == clean_json


def test_selection_that_matches_no_rows_still_reports_the_whole_file_count(
    installed, home, tmp_path
):
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    done = _agents(installed, home, copy, "--where", "session.id=nothing")

    assert done.stdout.splitlines()[0] == (
        "0 agent row(s), 1 unreadable line(s) skipped. " + AGENTS_COVERAGE.format(n=1)
    )


def test_empty_logs_with_malformed_metrics_prints_coverage_and_no_session_block_without_marking(
    installed, home, tmp_path
):
    directory = _stream(tmp_path / "s", [], ["not json {"])

    done = _check(installed, home, directory)

    assert done.stdout.splitlines() == [
        CHECK_COVERAGE.format(n=1),
        "Totals check (claude_code.cost.usage vs summed cost_usd):",
        NO_SESSIONS_LINE,
    ]


def test_agents_json_gains_only_the_marking_and_count_members(installed, home, tmp_path):
    """CP-330, CP-331: clean and malformed differ by the count, 0 against 1, and by nothing else."""
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    clean = json.loads(_agents(installed, home, DATA_SET, "--prefix", PREFIX, "--json").stdout)
    printed = json.loads(_agents(installed, home, copy, "--prefix", PREFIX, "--json").stdout)

    assert (clean.pop(UNREADABLE_COUNT_MEMBER), printed.pop(UNREADABLE_COUNT_MEMBER)) == (0, 1)
    assert printed == clean
    assert list(printed)[-1] == COST_NOTE_MEMBER


def _count(done) -> int:
    assert (done.returncode, done.stderr) == (0, "")
    document = json.loads(done.stdout)
    assert list(document)[0] == UNREADABLE_COUNT_MEMBER, "CP-330: the count stands first"
    assert type(document[UNREADABLE_COUNT_MEMBER]) is int, "CP-330: an integer, never null"
    return document[UNREADABLE_COUNT_MEMBER]


def test_json_count_is_zero_on_the_parity_data_set(installed, home):
    """CP-330: both commands over the stored data set, where no metrics line is unreadable."""
    agents = _agents(installed, home, DATA_SET, "--prefix", PREFIX, "--json")
    check = _check(installed, home, DATA_SET, "--json")

    assert (_count(agents), _count(check)) == (0, 0)


def test_json_count_is_one_with_a_not_json_line_appended(installed, home, tmp_path):
    """CP-330: the same number the text form states, and `skipped_lines` still the logs' count."""
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    agents = _agents(installed, home, copy, "--prefix", PREFIX, "--json")
    check = _check(installed, home, copy, "--json")

    assert (_count(agents), _count(check)) == (1, 1)
    assert json.loads(agents.stdout)["skipped_lines"] == 1
    text = _agents(installed, home, copy, "--prefix", PREFIX).stdout.splitlines()[0]
    assert text.endswith(AGENTS_COVERAGE.format(n=1))


def test_json_count_is_zero_with_no_metrics_file(installed, home, tmp_path):
    """CP-330: `check --json` over a `logs.jsonl` alone, and `agents --json` the same."""
    directory = _stream(tmp_path / "s", [_request_line("s")])

    check = _check(installed, home, directory, "--json")
    agents = _agents(installed, home, directory, "--json")

    assert _count(check) == 0 and _count(agents) == 0
    for done in (check, agents):
        assert json.loads(done.stdout)[METRICS_FILE_MEMBER] == {"state": "nothing_received"}
    assert [e["session_id"] for e in json.loads(check.stdout)["metrics_check"]] == ["s"]


@pytest.mark.parametrize(
    "extra, expected",
    [
        pytest.param([], 0, id="empty-metrics-file"),
        pytest.param(["", "   "], 0, id="blank-lines-only"),
        pytest.param(["[1, 2]"], 1, id="outer-value-not-an-object"),
        pytest.param(['{"signal": "metrics"}'], 1, id="body-missing"),
        pytest.param(['{"body": 3}'], 1, id="body-not-an-object"),
        pytest.param(['{"body": {}}'], 1, id="body-empty-object"),
        pytest.param(["not json {", "[1]", '{"body": {}}', ""], 3, id="several-bad-lines"),
    ],
)
def test_json_count_counts_each_unreadable_line_category(
    installed, home, tmp_path, extra, expected
):
    """CP-330: the parser's categories, in both commands; blank lines are not counted."""
    directory = _stream(tmp_path / "s", [_request_line("s")], extra)

    assert _count(_agents(installed, home, directory, "--json")) == expected
    assert _count(_check(installed, home, directory, "--json")) == expected


def test_json_count_with_a_readable_export_with_no_cost_metric(installed, home, tmp_path):
    """CP-330, CP-331: a readable export holding no cost point is counted as readable, and the
    cost note stands where it did."""
    directory = _stream(tmp_path / "s", [_request_line("s")], [NO_COST_EXPORT])

    agents = _agents(installed, home, directory, "--json")
    check = _check(installed, home, directory, "--json")

    assert _count(agents) == 0 and _count(check) == 0
    assert list(json.loads(agents.stdout))[-1] == COST_NOTE_MEMBER
    assert list(json.loads(check.stdout))[-1] == COST_NOTE_MEMBER


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--where", "session.id=nothing"], id="where-selecting-none"),
        pytest.param(["--where", "kensai.arm=two-arms-inline"], id="where-selecting-some"),
        pytest.param(["--prefix", PREFIX], id="prefix"),
        pytest.param(["--compare", *COMPARISON], id="compare"),
        pytest.param(
            ["--where", "kensai.arm=two-arms-inline", "--compare", *COMPARISON],
            id="compare-and-where",
        ),
    ],
)
def test_agents_json_count_is_whole_file_under_where_prefix_and_compare(
    installed, home, tmp_path, arguments
):
    """CP-330, CP-336 group C: selecting rows leaves the metrics file's count where it was."""
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    assert _count(_agents(installed, home, copy, "--json", *arguments)) == 1


def test_json_where_selecting_none_still_carries_the_count_with_no_rows(installed, home, tmp_path):
    """CP-330: `rows` is `[]` and the count is still 1."""
    copy = _copy_with_unreadable_metrics(tmp_path / "m")

    done = _agents(installed, home, copy, "--json", "--where", "session.id=x")
    document = json.loads(done.stdout)

    assert document["rows"] == [] and document[UNREADABLE_COUNT_MEMBER] == 1


def test_empty_readable_logs_with_malformed_metrics_reports_the_count(installed, home, tmp_path):
    """CP-330: an existing empty `logs.jsonl` gives an empty check and the count, and no `state`."""
    directory = _stream(tmp_path / "s", [], ["not json {"])

    check = _check(installed, home, directory, "--json")
    agents = _agents(installed, home, directory, "--json")

    assert json.loads(check.stdout) == {
        UNREADABLE_COUNT_MEMBER: 1, METRICS_FILE_MEMBER: {"state": "read"},
        SKIPPED_LINES_MEMBER: 0, "metrics_check": [],
    }
    assert _count(agents) == 1 and "state" not in json.loads(agents.stdout)


@pytest.mark.parametrize(
    "arguments",
    [[], ["--prefix", PREFIX], ["--compare", *COMPARISON], ["--where", "session.id=nothing"]],
    ids=["plain", "prefix", "compare", "zero-rows"],
)
def test_removing_the_count_gives_the_old_json_and_the_text_is_unchanged(
    installed, home, tmp_path, arguments
):
    """CP-331, CP-333: with the member out, every old member keeps its name, value and order, the
    cost note included; the text names the count as it always did."""
    copy = _copy_with_unreadable_metrics(tmp_path / "m")
    clean = _agents(installed, home, DATA_SET, "--json", *arguments)
    printed = _agents(installed, home, copy, "--json", *arguments)

    clean_document, document = json.loads(clean.stdout), json.loads(printed.stdout)
    assert (clean_document.pop(UNREADABLE_COUNT_MEMBER), document.pop(UNREADABLE_COUNT_MEMBER)) == (
        0,
        1,
    )
    assert list(document) == list(clean_document) and document == clean_document
    text = _agents(installed, home, copy, *arguments).stdout
    assert UNREADABLE_COUNT_MEMBER not in text


# ── B — nothing received yet (CP-291) ────────────────────────────────────────


def _no_stream_line(directory: Path) -> str:
    return f"No stream at {directory} — no logs.jsonl there."


def _guidance(directory: Path) -> list[str]:
    return [
        "This directory holds no requests received from Claude Code yet.",
        "Check that Claude Code's telemetry is turned on: ka-telemetry turn-on writes the "
        "settings that turn it on.",
        f"Check that the receiver is running and writing to {directory}: ka-telemetry receive "
        "runs it, and its --dir option sets the directory it writes to.",
    ]


def test_empty_directory_prints_first_line_then_guidance_and_no_table(installed, home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    done = _agents(installed, home, empty)

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == [_no_stream_line(empty), *_guidance(empty)], (
        "CP-291: the no-stream line, then what to check"
    )
    assert "ka-telemetry turn-on" in done.stdout and "ka-telemetry receive" in done.stdout
    for absent in ("agent row(s)", "Total cost_usd:", "estimate"):
        assert absent not in done.stdout


def test_absent_directory_leading_tilde_and_absent_default_directory_print_guidance(
    installed, home, tmp_path
):
    absent = tmp_path / "absent"

    explicit = run(installed, "agents", "--dir", str(absent), home=home)
    tilde = run(installed, "agents", "--dir", "~/no-such-directory", home=home)
    default = run(installed, "agents", home=home)

    for done, directory in (
        (explicit, absent),
        (tilde, home / "no-such-directory"),
        (default, home / ".ka-telemetry"),
    ):
        assert (done.returncode, done.stderr) == (0, "")
        assert done.stdout.splitlines() == [_no_stream_line(directory), *_guidance(directory)]


def test_guidance_holds_with_any_valid_prefix_where_and_compare(installed, home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    done = _agents(
        installed, home, empty, "--prefix", "kensai", "--where", "session.id=x",
        "--compare", "kensai.arm", "a", "b",
    )

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == [_no_stream_line(empty), *_guidance(empty)]


def _nothing_received_document(directory: Path) -> dict:
    _, statement, first_check, second_check = [_no_stream_line(directory), *_guidance(directory)]
    return {
        "state": "nothing_received",
        "directory": str(directory),
        "message": statement,
        "checks": [first_check, second_check],
    }


def _unopenable_statement(directory: Path, reason: str) -> str:
    return (
        f"There is a logs.jsonl at {directory}, but ka-telemetry could not read it: {reason}. "
        "Run this command again once logs.jsonl can be read."
    )


def _unopenable_document(directory: Path, reason: str) -> dict:
    return {
        "state": "unopenable",
        "directory": str(directory),
        "message": _unopenable_statement(directory, reason),
        "reason": reason,
    }


def _nothing_received_directories(tmp_path: Path) -> dict[str, Path]:
    (tmp_path / "a-file").write_text("")
    dangling = tmp_path / "dangling"
    dangling.mkdir()
    (dangling / "logs.jsonl").symlink_to(tmp_path / "no-such-target")
    empty = tmp_path / "empty"
    empty.mkdir()
    return {
        "empty-directory": empty,
        "absent-directory": tmp_path / "absent",
        "path-through-a-file": tmp_path / "a-file" / "child",
        "dangling-symlink": dangling,
    }


VARIANTS = ["empty-directory", "absent-directory", "path-through-a-file", "dangling-symlink"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_check_text_nothing_received_prints_the_four_lines(installed, home, tmp_path, variant):
    """CP-325, CP-336 group A: `check` prints what `agents` prints, however nothing is there."""
    directory = _nothing_received_directories(tmp_path)[variant]

    from_check = _check(installed, home, directory)
    from_agents = _agents(installed, home, directory)

    assert (from_check.returncode, from_check.stderr) == (0, "")
    assert from_check.stdout.splitlines() == [_no_stream_line(directory), *_guidance(directory)]
    assert from_check.stdout == from_agents.stdout
    for absent in ("Totals check", "No metrics.jsonl found", "estimate"):
        assert absent not in from_check.stdout


@pytest.mark.parametrize("variant", VARIANTS)
def test_json_nothing_received_is_one_object_the_same_from_both_commands(
    installed, home, tmp_path, variant
):
    """CP-326, CP-329, CP-336 group A: one JSON object, the same bytes from `agents` and `check`."""
    directory = _nothing_received_directories(tmp_path)[variant]

    from_check = _check(installed, home, directory, "--json")
    from_agents = _agents(installed, home, directory, "--json")
    with_options = _agents(
        installed, home, directory, "--json", "--prefix", "kensai", "--where", "session.id=x",
        "--compare", "kensai.arm", "a", "b",
    )

    for done in (from_check, from_agents, with_options):
        assert (done.returncode, done.stderr) == (0, "")
        document = json.loads(done.stdout)
        assert list(document) == ["state", "directory", "message", "checks"]
        assert document == _nothing_received_document(directory)
    assert from_check.stdout == from_agents.stdout == with_options.stdout
    assert from_check.stdout == json.dumps(_nothing_received_document(directory), indent=2) + "\n"


def test_nothing_received_lines_expand_the_default_directory_and_a_leading_tilde(
    installed, home
):
    """CP-325: the isolated child environment's `HOME` decides the directory named."""
    default = run(installed, "check", home=home)
    tilde = run(installed, "check", "--dir", "~/no-such-directory", "--json", home=home)

    assert default.stdout.splitlines() == [
        _no_stream_line(home / ".ka-telemetry"), *_guidance(home / ".ka-telemetry")
    ]
    assert json.loads(tilde.stdout) == _nothing_received_document(home / "no-such-directory")


@pytest.fixture
def refused_logs(tmp_path: Path):
    directory = tmp_path / "refused"
    directory.mkdir()
    logs = directory / "logs.jsonl"
    logs.write_text("secret\n")
    logs.chmod(0)
    yield directory
    logs.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="mode 000 does not stop a read at effective uid 0")
def test_unopenable_mode_000_logs_say_it_is_there_and_why_in_all_four_forms(
    installed, home, refused_logs
):
    """CP-327, CP-328, CP-336 group B: `agents` and `check`, text and `--json`."""
    statement = _unopenable_statement(refused_logs, "Permission denied")

    text = [_agents(installed, home, refused_logs), _check(installed, home, refused_logs)]
    as_json = [
        _agents(installed, home, refused_logs, "--json"),
        _check(installed, home, refused_logs, "--json"),
    ]

    for done in text:
        assert (done.returncode, done.stderr, done.stdout) == (0, "", statement + "\n")
    for done in as_json:
        assert (done.returncode, done.stderr) == (0, "")
        assert json.loads(done.stdout) == _unopenable_document(refused_logs, "Permission denied")
        assert list(json.loads(done.stdout)) == ["state", "directory", "message", "reason"]
    assert as_json[0].stdout == as_json[1].stdout
    for done in (*text, *as_json):
        for absent in ("no logs.jsonl there", "Traceback", "holds no requests"):
            assert absent not in done.stdout + done.stderr


def test_unopenable_logs_jsonl_directory_says_it_is_there_and_why(installed, home, tmp_path):
    """CP-327, CP-328: a directory named `logs.jsonl` — held at any user, root included."""
    directory = tmp_path / "d"
    (directory / "logs.jsonl").mkdir(parents=True)

    for done in (_agents(installed, home, directory), _check(installed, home, directory)):
        assert (done.returncode, done.stderr) == (0, "")
        assert done.stdout == _unopenable_statement(directory, "Is a directory") + "\n"
    for done in (
        _agents(installed, home, directory, "--json"),
        _check(installed, home, directory, "--json"),
    ):
        assert (done.returncode, done.stderr) == (0, "")
        assert json.loads(done.stdout) == _unopenable_document(directory, "Is a directory")


def _undetermined_logs_statement(directory: Path, reason: str) -> str:
    """CP-354's statement, written out from `change_text.md`, not read from the product."""
    return (
        f"In {directory}, ka-telemetry could not read logs.jsonl: {reason}. "
        "It could not tell whether logs.jsonl is there either. "
        "Run this command again once ka-telemetry can read logs.jsonl or tell that it is not there."
    )


def _undetermined_logs_document(directory: Path, reason: str) -> dict:
    return {
        "state": "uninspectable",
        "directory": str(directory),
        "message": _undetermined_logs_statement(directory, reason),
        "reason": reason,
    }


def _assert_undetermined_logs_in_all_four_forms(installed, home, directory: Path) -> None:
    statement = _undetermined_logs_statement(directory, "Permission denied")
    for done in (_agents(installed, home, directory), _check(installed, home, directory)):
        assert (done.returncode, done.stderr, done.stdout) == (0, "", statement + "\n")
    for done in (
        _agents(installed, home, directory, "--json"),
        _check(installed, home, directory, "--json"),
    ):
        assert (done.returncode, done.stderr) == (0, "")
        document = json.loads(done.stdout)
        assert list(document) == ["state", "directory", "message", "reason"]
        assert document == _undetermined_logs_document(directory, "Permission denied")
    for done in (
        _agents(installed, home, directory), _check(installed, home, directory),
        _agents(installed, home, directory, "--json"), _check(installed, home, directory, "--json"),
    ):
        for absent in ("No stream at", "no logs.jsonl there", "Traceback"):
            assert absent not in done.stdout + done.stderr


def test_cp354_cp355_mode_000_directory_is_named_in_all_four_forms(
    installed, home, tmp_path, seal
):
    """CP-354, CP-355: a directory the user may not look into gives one statement, or one JSON
    document, from `agents` and `check` alike."""
    directory = _stream(tmp_path / "sealed", ["{}"])
    seal(directory)

    _assert_undetermined_logs_in_all_four_forms(installed, home, directory)


def test_cp354_logs_link_into_mode_000_directory_is_named_in_all_four_forms(
    installed, home, tmp_path, seal
):
    """CP-354: the directory opens and the link cannot be followed, so the statement claims
    neither that the directory could not be opened nor that `logs.jsonl` is absent or present."""
    inner = _stream(tmp_path / "inner", ["{}"])
    seal(inner)
    stream = tmp_path / "stream"
    stream.mkdir()
    (stream / "logs.jsonl").symlink_to(inner / "logs.jsonl")

    _assert_undetermined_logs_in_all_four_forms(installed, home, stream)
    done = _check(installed, home, stream)
    for claim in ("There is a logs.jsonl", "could not be opened"):
        assert claim not in done.stdout


def test_json_outcomes_are_told_apart_by_the_state_member(installed, home, tmp_path):
    """CP-329: missing and unopenable carry two different states; a report, an existing empty
    `logs.jsonl` included, carries none."""
    unopenable = tmp_path / "d"
    (unopenable / "logs.jsonl").mkdir(parents=True)
    empty_logs = _stream(tmp_path / "empty-logs", [])

    for command in (_agents, _check):
        states = [
            json.loads(command(installed, home, directory, "--json").stdout).get("state")
            for directory in (tmp_path / "missing", unopenable, empty_logs, DATA_SET)
        ]
        assert states == ["nothing_received", "unopenable", None, None]


def test_check_and_json_missing_stream_outputs_say_nothing_received(installed, home, tmp_path):
    """CP-325, CP-326: replaces the old one-line outputs; the directory is still named."""
    empty = tmp_path / "empty"
    empty.mkdir()

    from_check = _check(installed, home, empty)
    assert (from_check.returncode, from_check.stderr) == (0, "")
    assert from_check.stdout.splitlines() == [_no_stream_line(empty), *_guidance(empty)]
    for done in (
        _agents(installed, home, empty, "--json"),
        _check(installed, home, empty, "--json"),
    ):
        assert (done.returncode, done.stderr) == (0, "")
        assert json.loads(done.stdout)["directory"] == str(empty)


def test_invalid_option_failures_are_unchanged(installed, home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    bad_prefix = _agents(installed, home, empty, "--prefix", "")
    bad_where = _agents(installed, home, empty, "--where", "nokv")

    assert bad_prefix.returncode == 2 and bad_prefix.stdout == ""
    assert "--prefix is empty" in bad_prefix.stderr
    assert bad_where.returncode == 1 and bad_where.stdout == ""
    assert "error: --where expects KEY=VALUE, got 'nokv'" in bad_where.stderr
    assert "ka-telemetry turn-on" not in bad_prefix.stderr + bad_where.stderr


# ── F — nothing else moved (CP-299 to CP-303) ────────────────────────────────


def _options(help_text: str) -> set[str]:
    return set(re.findall(r"^  (?:-\w, )?(-{1,2}[\w-]+)", help_text, flags=re.MULTILINE))


def test_agents_and_check_option_sets_are_unchanged(installed, home):
    agents = run(installed, "agents", "--help", home=home).stdout
    check = run(installed, "check", "--help", home=home).stdout

    # `-h, --help` is read as the one option it is, listed by its long name.
    assert _options(agents) == {"--help", "--dir", "--prefix", "--where", "--compare", "--json"}, (
        "CP-302"
    )
    assert _options(check) == {"--help", "--dir", "--json"}, "CP-302"
    assert "-h, --help" in agents and "-h, --help" in check




# ── Hardening — the causes in every state the installed commands reach (CP-338 to CP-342) ──


def _entries(done) -> dict[str, dict]:
    assert done.returncode == 0, done.stderr
    return {e["session_id"]: e for e in json.loads(done.stdout)["metrics_check"]}


def _entries_in_both_commands(installed, home, directory: Path, *arguments: str):
    return (
        _entries(_check(installed, home, directory, "--json", *arguments)),
        _entries(_agents(installed, home, directory, "--json", *arguments)),
    )


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param([], id="empty"),
        pytest.param(["not json"], id="malformed"),
        pytest.param([_cost_export_line("another", 0.5)], id="only-another-session"),
        pytest.param([NO_COST_EXPORT], id="only-another-metric"),
    ],
)
def test_installed_commands_over_real_temporary_streams_name_causes_by_state(
    installed, home, tmp_path, metrics
):
    directory = _stream(tmp_path / "stream", [_request_line("s", 0.5)], metrics)

    for entries in _entries_in_both_commands(installed, home, directory):
        assert entries["s"]["possible_causes"] == NO_METRIC_CAUSES
    for done in (_check(installed, home, directory), _agents(installed, home, directory)):
        assert f"no metric export{NO_METRIC_REASON}" in done.stdout


def test_installed_commands_name_a_missing_metrics_file_and_still_name_its_sessions(
    installed, home, tmp_path
):
    directory = _stream(tmp_path / "stream", [_request_line("s", 0.5)])

    lines = _check(installed, home, directory).stdout.splitlines()

    assert lines[:2] == [_absent_line(directory), CHECK_HEADING]
    assert lines[2] == f"  session=s  log_cost_usd=0.500000  no metric export{NO_METRIC_REASON}"
    assert NO_METRICS_LINE not in lines
    for command in (_check, _agents):
        document = json.loads(command(installed, home, directory, "--json").stdout)
        assert document[METRICS_FILE_MEMBER] == {"state": "nothing_received"}
        (entry,) = document["metrics_check"]
        assert entry["possible_causes"] == NO_METRIC_CAUSES


def test_installed_commands_give_an_empty_list_where_no_session_is_checked(
    installed, home, tmp_path
):
    directory = _stream(tmp_path / "stream", [], [_cost_export_line("ghost", 5.0)])

    text = _check(installed, home, directory).stdout.splitlines()
    assert text == [
        "Totals check (claude_code.cost.usage vs summed cost_usd):",
        NO_SESSIONS_LINE,
    ]
    for done in (_check(installed, home, directory, "--json"),
                 _agents(installed, home, directory, "--json")):
        assert json.loads(done.stdout)["metrics_check"] == []
    # A session only the metrics know is not named.
    assert "ghost" not in "".join(text)


def test_installed_commands_check_only_the_selected_sessions_whole_log_cost_under_where(
    installed, home, tmp_path
):
    directory = _stream(
        tmp_path / "stream",
        [_request_line("a", 0.5), _request_line("b", 1.0), _request_line("b", 1.0)],
        [_cost_export_line("b", 4.0)],
    )

    none = _entries(_agents(installed, home, directory, "--json", "--where", "session.id=none"))
    only_b = _entries(_agents(installed, home, directory, "--json", "--where", "session.id=b"))

    assert none == {}, "a selection that matches no row names no session"
    assert list(only_b) == ["b"]
    assert only_b["b"]["log_cost_usd"] == 2.0, "the session's whole log cost, as today"
    assert only_b["b"]["possible_causes"] == MISMATCH_CAUSES


def test_installed_commands_name_causes_for_the_zero_total_states(installed, home, tmp_path):
    directory = _stream(
        tmp_path / "stream",
        [_request_line("zero-log", 0.0), _request_line("zero-both", 0.0),
         _request_line("zero-metric", 1.0)],
        [_cost_export_line("zero-log", 0.5), _cost_export_line("zero-both", 0.0),
         _cost_export_line("zero-metric", 0.0)],
    )

    for entries in _entries_in_both_commands(installed, home, directory):
        assert entries["zero-log"]["possible_causes"] == MISMATCH_CAUSES
        assert entries["zero-both"]["possible_causes"] == []
        assert entries["zero-metric"]["possible_causes"] == MISMATCH_CAUSES


# ── Hardening — the oracle helpers do not let a defect pass as a declared departure ──


def _stored_printed(command: str) -> str:
    """What `command` would print over the stored data set, with every declared departure in, the
    heading as the package now prints it (CP-383)."""
    text = _stored("agents.txt")
    for stored_line, reason in (
        (NO_METRIC_STORED_LINE, NO_METRIC_REASON),
        (MISMATCH_STORED_LINE, MISMATCH_REASON),
    ):
        text = text.replace(stored_line + "\n", stored_line + reason + "\n")
    if command == "check":
        counted = CHECK_LOGS_COVERAGE.format(n=1) + "\n"
        return counted + current_heading_in(_totals_block(text)) + COST_MARKING + "\n"
    lines = current_heading_in(text).splitlines(keepends=True)
    total = next(i for i, line in enumerate(lines) if line.startswith("Total cost_usd: "))
    return "".join([*lines[: total + 1], COST_MARKING + "\n", *lines[total + 1 :]])


@pytest.mark.parametrize("command", ["agents", "check"])
@pytest.mark.parametrize("session", ["no-metric", "mismatch"])
@pytest.mark.parametrize("damage", ["missing", "wrong", "reordered", "extra-text"])
def test_normalisation_rejects_a_missing_or_wrong_cause(command, session, damage):
    printed = _stored_printed(command)
    stored_line, reason = (
        (NO_METRIC_STORED_LINE, NO_METRIC_REASON)
        if session == "no-metric"
        else (MISMATCH_STORED_LINE, MISMATCH_REASON)
    )
    causes = reason.split(": ", 1)[1].split("; ")
    broken = {
        "missing": reason.split(": ")[0],
        "wrong": reason.replace("may", "did", 1),
        "reordered": reason.split(": ", 1)[0] + ": " + "; ".join(reversed(causes)),
        "extra-text": reason + ".",
    }[damage]
    damaged = printed.replace(stored_line + reason, stored_line + broken)
    assert damaged != printed
    cut = (
        agents_text_without_declared_lines if command == "agents"
        else check_text_without_declared_lines
    )

    with pytest.raises(AssertionError):
        cut(damaged)


@pytest.mark.parametrize("damage", ["missing", "not-last", "wrong-value", "null", "extra-member"])
def test_normalisation_rejects_a_missing_reordered_or_wrong_possible_causes_member(damage):
    document = json.loads(_stored("agents.json"))
    for entry in document["metrics_check"]:
        _cut_declared_member_back(entry)
    document = {UNREADABLE_COUNT_MEMBER: 0, **document, COST_NOTE_MEMBER: COST_MARKING}
    entry = next(e for e in document["metrics_check"] if e["session_id"] == NO_METRIC_SESSION)
    if damage == "missing":
        del entry["possible_causes"]
    elif damage == "not-last":
        entry["possible_causes"] = entry.pop("possible_causes")
        entry["within_tolerance"] = entry.pop("within_tolerance")
    elif damage == "wrong-value":
        entry["possible_causes"] = list(reversed(NO_METRIC_CAUSES))
    elif damage == "null":
        entry["possible_causes"] = None
    else:
        entry["unrelated"] = 1

    with pytest.raises(AssertionError):
        json_without_declared_members(json.dumps(document, indent=2) + "\n")


def test_a_mutated_cost_still_survives_cause_removal():
    printed = _stored_printed("check").replace("log_cost_usd=0.950151", "log_cost_usd=0.950152", 1)

    remaining = check_text_without_declared_lines(printed)

    assert "log_cost_usd=0.950152" in remaining
    assert remaining != check_text_without_declared_lines(_stored_printed("check"))
    assert "MISMATCH >1%\n" in remaining


def _cut_declared_member_back(entry: dict) -> None:
    """The stored entry with the member the departure adds put back, as the package prints it."""
    if entry["metric_cost_usd"] is None:
        entry["possible_causes"] = list(NO_METRIC_CAUSES)
    elif entry["within_tolerance"] is False:
        entry["possible_causes"] = list(MISMATCH_CAUSES)
    else:
        entry["possible_causes"] = []


# ── The metrics file's state, through the installed commands (CP-351 to CP-359, CP-365) ────────


def _stored_copy(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "logs.jsonl").write_text((DATA_SET / "logs.jsonl").read_text())
    return directory


@pytest.mark.skipif(os.geteuid() == 0, reason="mode 000 does not stop a read at effective uid 0")
def test_cp351_installed_mode_000_metrics_is_named_by_both_commands_in_both_forms(
    installed, home, tmp_path, seal
):
    directory = _stored_copy(tmp_path / "refused")
    (directory / "metrics.jsonl").write_text("secret\n")
    seal(directory / "metrics.jsonl")
    statement = _unopenable_metrics_statement(directory, "Permission denied")

    check = _check(installed, home, directory)
    agents = _agents(installed, home, directory)
    check_json = json.loads(_check(installed, home, directory, "--json").stdout)
    agents_json = json.loads(_agents(installed, home, directory, "--json").stdout)

    assert check.stdout == CHECK_LOGS_COVERAGE.format(n=1) + "\n" + statement + "\n"
    assert statement in agents.stdout.splitlines() and COST_MARKING in agents.stdout
    for done in (check, agents):
        assert done.returncode == 0 and done.stderr == ""
        assert "Traceback" not in done.stdout and NO_METRICS_LINE not in done.stdout
    for document in (check_json, agents_json):
        assert document[METRICS_FILE_MEMBER] == {
            "state": "unopenable", "reason": "Permission denied", "message": statement,
        }
        assert "metrics_check" not in document and document[UNREADABLE_COUNT_MEMBER] == 0
    assert list(check_json) == [UNREADABLE_COUNT_MEMBER, METRICS_FILE_MEMBER, SKIPPED_LINES_MEMBER]


def test_cp351_installed_metrics_that_is_a_directory_says_so_and_runs_as_root(
    installed, home, tmp_path
):
    directory = _stored_copy(tmp_path / "dir-metrics")
    (directory / "metrics.jsonl").mkdir()

    check = _check(installed, home, directory)
    document = json.loads(_check(installed, home, directory, "--json").stdout)

    statement = _unopenable_metrics_statement(directory, "Is a directory")
    assert statement in check.stdout.splitlines()
    assert document[METRICS_FILE_MEMBER]["reason"] == "Is a directory"
    assert document[METRICS_FILE_MEMBER]["state"] == "unopenable"


@pytest.mark.skipif(os.geteuid() == 0, reason="mode 000 does not stop a read at effective uid 0")
def test_cp352_installed_metrics_link_into_mode_000_directory_claims_neither_presence_nor_absence(
    installed, home, tmp_path, seal
):
    directory = _stored_copy(tmp_path / "linked")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "metrics.jsonl").write_text("secret\n")
    seal(inner)
    (directory / "metrics.jsonl").symlink_to(inner / "metrics.jsonl")
    statement = _undetermined_metrics_statement(directory, "Permission denied")

    for command in (_check, _agents):
        text = command(installed, home, directory)
        document = json.loads(command(installed, home, directory, "--json").stdout)
        assert statement in text.stdout.splitlines() and text.stderr == ""
        assert document[METRICS_FILE_MEMBER] == {
            "state": "uninspectable", "reason": "Permission denied", "message": statement,
        }
        assert "There is a metrics.jsonl" not in text.stdout
        assert "No metrics.jsonl was found" not in text.stdout


def test_cp358_installed_absent_metrics_names_the_absence_then_the_eight_sessions(
    installed, home, tmp_path
):
    directory = _stored_copy(tmp_path / "absent")

    for command in (_check, _agents):
        done = command(installed, home, directory)
        lines = done.stdout.splitlines()
        at = lines.index(_absent_line(directory))
        assert lines[at + 1] == CHECK_HEADING
        sessions = lines[at + 2 : at + 10]
        assert all(f"no metric export{NO_METRIC_REASON}" in line for line in sessions)
        assert any(
            NO_METRIC_SESSION in line and "log_cost_usd=0.050333" in line for line in sessions
        )
        assert "MISMATCH" not in done.stdout and NO_METRICS_LINE not in done.stdout
    check = _check(installed, home, directory).stdout.splitlines()
    assert check[-1] == COST_MARKING
    document = json.loads(_check(installed, home, directory, "--json").stdout)
    assert len(document["metrics_check"]) == 8
    assert document[METRICS_FILE_MEMBER] == {"state": "nothing_received"}


# ── The one name of the totals check (CP-383 to CP-385, CP-393) ────────────────────────────────


def _the_two_data_sets(tmp_path: Path) -> dict[str, Path]:
    """`<R>`, a copy of the stored data set, and `<M>`, a directory holding only its logs.jsonl."""
    reader = tmp_path / "R"
    shutil.copytree(DATA_SET, reader)
    return {"R": reader, "M": _stored_copy(tmp_path / "M")}


def test_no_text_report_says_cross_check(installed, home, tmp_path):
    """CP-384, CP-393: neither command's text form prints a line that names the check by its old
    name, over a directory holding both files and over one holding only logs.jsonl. Both `check`
    outputs open the totals block with the heading. Against product commit eaf839c this fails:
    the four outputs held 2, 1, 2 and 1 such lines."""
    for label, directory in _the_two_data_sets(tmp_path).items():
        for command in (_check, _agents):
            done = command(installed, home, directory)
            assert done.returncode == 0 and done.stderr == ""
            old_name = re.findall(r"(?im)^.*cross.check.*$", done.stdout)
            assert old_name == [], f"{label}: {old_name}"
        check_lines = _check(installed, home, directory).stdout.splitlines()
        assert [line for line in check_lines if line.startswith("Totals check")] == [CHECK_HEADING]


def test_json_forms_hold_no_cross_and_keep_their_members_over_r_and_m(installed, home, tmp_path):
    """CP-385: only the failed-read messages carry a renamed line, so over `<R>` and `<M>` the
    JSON documents say nothing of the old name and `check`'s five members stay in order."""
    for label, directory in _the_two_data_sets(tmp_path).items():
        for command in (_check, _agents):
            printed = command(installed, home, directory, "--json").stdout
            assert "cross" not in printed.lower(), label
        document = json.loads(_check(installed, home, directory, "--json").stdout)
        assert list(document) == [
            UNREADABLE_COUNT_MEMBER,
            METRICS_FILE_MEMBER,
            SKIPPED_LINES_MEMBER,
            "metrics_check",
            COST_NOTE_MEMBER,
        ], label


# CP-390: `agents` and `check` over `<M>` and over `<U>`, each with and without `--json`, print what
# product commit eaf839c printed with CP-383's substitution applied, and nothing else differs. Over
# `<R>` the parity tests hold the same runs against the stored outputs. No stored output exists for
# `<M>` or `<U>`, and `tests/stream_parity/` may not gain one (CP-391), so each run is held by a
# digest: the sha256 of eaf839c's stdout for that run, with the run's directory written as `<DIR>`
# and `Metrics cross-check` -> `Totals check`, `metrics cross-check` -> `totals check` applied.
# Measured in a clone at eaf839c by the code-integrity review of 2026-10-13.
CP390_BASELINE_DIGESTS = {
    ("M", "agents", "text"): "c6d17742fc796b81aa6a2fa9ff47f97321295f25309ee58eb2ee172d301f815c",
    ("M", "agents", "json"): "4d2eaadec0db56f0fc661a22629d5f1b3b261bf0f47b767f9db73039a49dbc7d",
    ("M", "check", "text"): "36eab1c95af81fe33c40bebd015c83b64020ac234fa3da2db6f7746d7b7b1541",
    ("M", "check", "json"): "3e51e36159d2bb6afc4f0ac19a427caa30b796639a23cdfded454034ca78b0e4",
    ("U", "agents", "text"): "47d97439e16dc62f8bb607334f467e3667aa58e45e7edb7c914a1591551f2521",
    ("U", "agents", "json"): "3320bae0e1834a99170633513f0f86af186fb33341a80325eedd835d579edf8e",
    ("U", "check", "text"): "33c3f96460e2729c844a4ba88b246de81a6ba8578ccbb3569e076d0472df842b",
    ("U", "check", "json"): "86d92cefeb3df48bc1eec3a62f58e597c0308db33a1d1a5e666678d06001824e",
}


def _cp390_directory(tmp_path: Path, label: str) -> Path:
    """`<M>` holds only the stored logs.jsonl; `<U>` adds a directory named metrics.jsonl."""
    directory = _stored_copy(tmp_path / label)
    if label == "U":
        (directory / "metrics.jsonl").mkdir()
    return directory


@pytest.mark.parametrize(("label", "command", "form"), sorted(CP390_BASELINE_DIGESTS))
def test_all_cp390_runs_equal_baseline_with_only_the_wording_departure(
    installed, home, tmp_path, label, command, form
):
    directory = _cp390_directory(tmp_path, label)
    reader = _agents if command == "agents" else _check

    done = reader(installed, home, directory, *(["--json"] if form == "json" else []))

    assert (done.returncode, done.stderr) == (0, "")
    printed = done.stdout.replace(str(directory), "<DIR>")
    assert hashlib.sha256(printed.encode()).hexdigest() == CP390_BASELINE_DIGESTS[
        label, command, form
    ], (
        f"CP-390: {command} over <{label}> ({form}) differs from eaf839c's output beyond "
        f"CP-383's substitution; it printed:\n{printed}"
    )
