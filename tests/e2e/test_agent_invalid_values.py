"""End-to-end tests of the installed `ka-telemetry agents` command naming the values it could not
read as numbers (CP-624–CP-632, CP-636, CP-637); its totals-check assertions follow `check`
naming the counts (CP-639–CP-653).

The command is driven from the scratch virtualenv the `installed` fixture builds, under a `HOME` of
its own, on real temporary streams written as `logs.jsonl` lines. Each request is built from a
valid base; a test names the envelope one field arrives in, or leaves it absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import PRODUCT_ROOT, run

if TYPE_CHECKING:
    from conftest import InstalledProduct

ABSENT = object()

FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
)
# The valid value each field carries in a base request, and how the text report names its figure.
BASE = {
    "input_tokens": {"intValue": 3},
    "output_tokens": {"intValue": 4},
    "cache_read_tokens": {"intValue": 5},
    "cache_creation_tokens": {"intValue": 6},
    "cost_usd": {"doubleValue": 0.5},
    "duration_ms": {"intValue": 7},
}
TEXT_NAME = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_creation_tokens": "cache_creation",
    "cost_usd": "cost_usd",
    "duration_ms": "duration_ms",
}
BASE_FIGURE = {
    "input_tokens": 3,
    "output_tokens": 4,
    "cache_read_tokens": 5,
    "cache_creation_tokens": 6,
    "cost_usd": 0.5,
    "duration_ms": 7,
}
NOT_A_NUMBER = {
    "string": {"stringValue": "abc"},
    "true": {"boolValue": True},
    "false": {"boolValue": False},
}


def _request(session: str = "s1", **fields) -> dict:
    """One `api_request` as a log record: the base figures, each replaced by the envelope given
    (`ABSENT` leaves the field out of the record altogether)."""
    figures = {**BASE, **fields}
    attributes = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "api_request"}},
        {"key": "model", "value": {"stringValue": "claude-model"}},
        {"key": "query_source", "value": {"stringValue": "sdk"}},
    ]
    attributes.extend(
        {"key": name, "value": value} for name, value in figures.items() if value is not ABSENT
    )
    return {"attributes": attributes}


def _stream(directory: Path, requests: list[dict], *, resource: dict | None = None) -> Path:
    """`directory` holding a `logs.jsonl` with one export carrying `requests`."""
    directory.mkdir(exist_ok=True)
    resource_attributes = [{"key": k, "value": v} for k, v in (resource or {}).items()]
    body = {
        "resourceLogs": [
            {
                "resource": {"attributes": resource_attributes},
                "scopeLogs": [{"logRecords": requests}],
            }
        ]
    }
    (directory / "logs.jsonl").write_text(json.dumps({"signal": "logs", "body": body}) + "\n")
    return directory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _agents(installed: InstalledProduct, home: Path, stream: Path, *arguments: str):
    return run(installed, "agents", "--dir", str(stream), *arguments, home=home)


def _both(installed: InstalledProduct, home: Path, stream: Path):
    """The text result and the parsed JSON document of `agents` on `stream`; both exit 0 with an
    empty stderr."""
    text = _agents(installed, home, stream)
    as_json = _agents(installed, home, stream, "--json")
    for result in (text, as_json):
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    return text.stdout, json.loads(as_json.stdout)


def _row_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines() if line.startswith("  session=") and " requests=" in line
    ]


def _figure_text(field: str, value) -> str:
    shown = f"{value:.6f}" if field == "cost_usd" else f"{value}"
    return f"{TEXT_NAME[field]}={shown}"


@pytest.mark.parametrize("kind", NOT_A_NUMBER)
@pytest.mark.parametrize("field", FIELDS)
def test_each_summed_field_with_a_value_that_is_not_a_number_shows_count_1(
    installed: InstalledProduct, home: Path, tmp_path: Path, field: str, kind: str
):
    stream = _stream(
        tmp_path / "s", [_request(), _request(**{field: NOT_A_NUMBER[kind]})]
    )

    text, document = _both(installed, home, stream)

    # The figure is the valid request's value; every other field sums both requests.
    for name in FIELDS:
        figure = BASE_FIGURE[name] if name == field else 2 * BASE_FIGURE[name]
        marked = f"{_figure_text(name, figure)} (invalid: 1)"
        assert (marked in text) is (name == field), (name, text)
    # The cost count is also on the total line and, with the session's request costs beside it, on
    # the totals check's session line.
    expected_marks = 3 if field == "cost_usd" else 1
    assert text.count("(invalid:") == expected_marks
    (row,) = document["rows"]
    assert row["invalid_values"] == {field: 1}
    assert type(row["invalid_values"][field]) is int
    assert row[field] == (BASE_FIGURE[field])
    assert row["request_count"] == 2


@pytest.mark.parametrize("field", FIELDS)
def test_an_absent_field_gives_the_other_records_sum_and_no_count(
    installed: InstalledProduct, home: Path, tmp_path: Path, field: str
):
    stream = _stream(tmp_path / "s", [_request(), _request(**{field: ABSENT})])

    text, document = _both(installed, home, stream)

    (row,) = document["rows"]
    assert row[field] == BASE_FIGURE[field]
    assert "(invalid:" not in text
    assert "invalid_values" not in row
    assert "invalid_values" not in document


def _probe(directory: Path) -> Path:
    """The five-record probe of the spec: `input_tokens` 10 each, `output_tokens` an `intValue`
    string holding no number in all five, and `cost_usd` arriving as 0.5, `"abc"`, `true`, absent
    and `"0.25"`."""
    costs = [
        {"doubleValue": 0.5},
        {"stringValue": "abc"},
        {"boolValue": True},
        ABSENT,
        {"stringValue": "0.25"},
    ]
    requests = [
        _request(
            input_tokens={"intValue": 10},
            output_tokens={"intValue": "oops"},
            cache_read_tokens={"intValue": 1},
            cache_creation_tokens={"intValue": 1},
            cost_usd=cost,
            duration_ms={"intValue": 100},
        )
        for cost in costs
    ]
    return _stream(directory, requests)


def test_the_mixed_probe_prints_the_spec_lines(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    text, _ = _both(installed, home, _probe(tmp_path / "s"))

    lines = text.splitlines()
    assert lines[1].endswith("requests=5  models=claude-model  cost_usd=0.750000 (invalid: 2)")
    assert lines[2] == (
        "    input=50 output=0 (invalid: 5) cache_read=5 cache_creation=5 duration_ms=500"
    )
    assert "Total cost_usd: 0.750000 (invalid: 2)" in lines


def test_the_mixed_probe_json_members_and_positions(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    _, document = _both(installed, home, _probe(tmp_path / "s"))

    (row,) = document["rows"]
    assert list(row)[-3:] == ["cost_usd", "duration_ms", "invalid_values"]
    assert row["cost_usd"] == 0.75
    assert row["duration_ms"] == 500
    assert row["invalid_values"] == {"output_tokens": 5, "cost_usd": 2}
    assert list(row["invalid_values"]) == ["output_tokens", "cost_usd"]
    keys = list(document)
    assert document["total_cost_usd"] == 0.75
    assert keys[keys.index("total_cost_usd") + 1] == "invalid_values"
    assert document["invalid_values"] == {"total_cost_usd": 2}
    assert row["input_tokens"] == 50 and row["output_tokens"] == 0 and row["request_count"] == 5


def test_only_the_invalid_field_is_annotated(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [_request(duration_ms={"intValue": 100}), _request(duration_ms={"stringValue": "slow"})],
    )

    text, document = _both(installed, home, stream)

    assert "duration_ms=100 (invalid: 1)" in text
    assert "cost_usd=1.000000\n" in text
    assert "Total cost_usd: 1.000000\n" in text
    assert document["rows"][0]["invalid_values"] == {"duration_ms": 1}
    assert "invalid_values" not in document


def test_two_rows_each_with_an_invalid_cost(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [
            _request("s1", cost_usd={"stringValue": "abc"}),
            _request("s2", cost_usd={"boolValue": True}),
        ],
    )

    text, document = _both(installed, home, stream)

    assert [line.endswith("cost_usd=0.000000 (invalid: 1)") for line in _row_lines(text)] == [
        True,
        True,
    ]
    assert "Total cost_usd: 0.000000 (invalid: 2)" in text
    assert document["invalid_values"] == {"total_cost_usd": 2}
    assert [row["invalid_values"] for row in document["rows"]] == [{"cost_usd": 1}] * 2


def test_valid_only_and_invalid_rows_keep_order_and_isolation(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [
            _request("tie-a", cost_usd={"doubleValue": 0.5}),
            _request("tie-b", cost_usd={"doubleValue": 0.5}, duration_ms={"stringValue": "x"}),
            _request("pricier", cost_usd={"doubleValue": 2.0}),
            _request("broken", cost_usd={"stringValue": "abc"}),
        ],
    )

    _, document = _both(installed, home, stream)

    rows = document["rows"]
    assert [row["session_id"] for row in rows] == ["pricier", "tie-a", "tie-b", "broken"]
    assert ["invalid_values" in row for row in rows] == [False, False, True, True]
    assert rows[2]["invalid_values"] == {"duration_ms": 1}
    assert rows[3]["invalid_values"] == {"cost_usd": 1}


def test_text_report_adds_no_line_for_a_count(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    clean = _stream(tmp_path / "clean", [_request(), _request()])
    counted = _stream(
        tmp_path / "counted", [_request(), _request(input_tokens={"stringValue": "abc"})]
    )

    clean_text, _ = _both(installed, home, clean)
    counted_text, _ = _both(installed, home, counted)

    assert len(counted_text.splitlines()) == len(clean_text.splitlines())
    assert "(invalid:" in counted_text


def test_absent_duration_ms_gives_the_other_records_sum_and_no_count(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(tmp_path / "s", [_request(), _request(duration_ms=ABSENT)])

    text, document = _both(installed, home, stream)

    assert "duration_ms=7\n" in text
    assert "(invalid:" not in text
    assert document["rows"][0]["duration_ms"] == 7
    assert "invalid_values" not in document["rows"][0]
    assert "invalid_values" not in document


def test_empty_stream_and_zero_selected_rows_have_zero_total_and_no_count(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "logs.jsonl").write_text("")
    selected_nothing = _stream(tmp_path / "s", [_request(cost_usd={"stringValue": "abc"})])

    for stream, extra in ((empty, ()), (selected_nothing, ("--where", "session.id=nobody"))):
        text = _agents(installed, home, stream, *extra)
        as_json = _agents(installed, home, stream, *extra, "--json")
        assert text.returncode == 0 and as_json.returncode == 0
        assert "Total cost_usd: 0.000000\n" in text.stdout
        assert "(invalid:" not in text.stdout
        document = json.loads(as_json.stdout)
        assert document["total_cost_usd"] == 0
        assert "invalid_values" not in document
        assert document["rows"] == []


def test_a_missing_or_unreadable_stream_prints_no_count(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    missing = tmp_path / "nothing-here"
    missing.mkdir()
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "logs.jsonl").mkdir()

    for stream in (missing, unreadable):
        text = _agents(installed, home, stream)
        as_json = _agents(installed, home, stream, "--json")
        assert text.returncode == 0 and as_json.returncode == 0
        assert "invalid" not in text.stdout
        assert "invalid_values" not in as_json.stdout
    assert f"No stream at {missing} — no logs.jsonl there." in _agents(
        installed, home, missing
    ).stdout


def test_a_value_only_in_the_resource_attributes_is_absent_in_the_report(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [_request(cost_usd=ABSENT)],
        resource={"cost_usd": {"stringValue": "abc"}},
    )

    text, document = _both(installed, home, stream)

    assert "(invalid:" not in text
    assert "invalid_values" not in document["rows"][0]


# ── --prefix invalid_values is refused by agents, before any directory is read ─────────────────

AGENTS_NEXT_STEP = "Run `ka-telemetry agents --help` to see what it accepts."
INVALID_VALUES_REFUSAL = (
    "ka-telemetry agents: error: --prefix 'invalid_values' is also the name of a field the "
    "--json output adds to a row that left out an invalid value, so labels under it cannot be "
    "shown; use --where to select requests by those attributes instead"
)


@pytest.mark.parametrize("as_json", [False, True])
def test_prefix_invalid_values_is_refused_with_exit_2_and_empty_stdout(
    installed: InstalledProduct, home: Path, tmp_path: Path, as_json: bool
):
    stream = _stream(tmp_path / "s", [_request()])

    result = _agents(
        installed, home, stream, "--prefix", "invalid_values", *(["--json"] if as_json else [])
    )

    assert result.returncode == 2
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0].startswith("usage: ka-telemetry agents")
    assert lines[-2] == INVALID_VALUES_REFUSAL
    assert lines[-1] == AGENTS_NEXT_STEP


_AUDITED_REFUSAL = """
import json, os, sys
directory = sys.argv[1]
events = []

def hook(event, args):
    for argument in args:
        if isinstance(argument, (str, bytes, os.PathLike)):
            is_bytes = isinstance(argument, bytes)
            text = argument.decode(errors="replace") if is_bytes else str(argument)
            if text.startswith(directory):
                events.append(event)
                return

sys.addaudithook(hook)
from ka_telemetry import stream_usage
try:
    stream_usage.main(["--dir", directory, "--prefix", "invalid_values"])
except SystemExit as stop:
    code = stop.code
sys.stdout.write(json.dumps({"code": code, "events": events}))
"""


@pytest.mark.parametrize("state", ["nonexistent", "unreadable"])
def test_refusal_happens_before_the_directory_is_looked_at(
    installed: InstalledProduct, home: Path, tmp_path: Path, state: str
):
    import subprocess

    from e2e.support import child_env

    if state == "unreadable" and os.geteuid() == 0:
        pytest.skip("root lists a directory whatever its mode, so it cannot be made unreadable")
    directory = tmp_path / "supplied"
    if state == "unreadable":
        directory.mkdir()
        directory.chmod(0o000)
    try:
        done = subprocess.run(
            [str(installed.venv / "bin" / "python"), "-c", _AUDITED_REFUSAL, str(directory)],
            capture_output=True,
            text=True,
            check=False,
            env=child_env(home),
        )
    finally:
        if state == "unreadable":
            directory.chmod(0o700)

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report == {"code": 2, "events": []}
    assert done.stderr.splitlines()[-2] == INVALID_VALUES_REFUSAL


# ── What this change leaves alone ──────────────────────────────────────────────────────────────


def _unchanged_sections_stream(directory: Path) -> Path:
    """A stream whose requests, completed agents and cost metric each carry a value that is not a
    number: an invalid request cost (a row's), an invalid completed-agent duration and tool count,
    and an invalid cost-metric point. Its expected outputs below were taken once from the product
    at 6de1734, where nothing is counted."""
    directory.mkdir(exist_ok=True)

    def completed(**fields) -> dict:
        attributes = [
            {"key": "session.id", "value": {"stringValue": "s1"}},
            {"key": "event.name", "value": {"stringValue": "subagent_completed"}},
            {"key": "agent_type", "value": {"stringValue": "Explore"}},
        ]
        attributes.extend({"key": key, "value": value} for key, value in fields.items())
        return {"attributes": attributes}

    records = [
        _request("s1", cost_usd={"doubleValue": 0.4}),
        _request("s1", cost_usd={"stringValue": "abc"}),
        _request("s2", cost_usd={"doubleValue": 0.2}),
        completed(duration_ms={"stringValue": "slow"}, total_tool_uses={"intValue": 3}),
        completed(duration_ms={"intValue": 100}, total_tool_uses={"stringValue": "many"}),
    ]
    # `s2` is a different query source, so `--compare query_source sdk other` has two groups.
    records[2]["attributes"] = [
        {"key": "query_source", "value": {"stringValue": "other"}}
        if attribute["key"] == "query_source"
        else attribute
        for attribute in records[2]["attributes"]
    ]
    _stream(directory, records)

    def point(session: str, value: dict) -> dict:
        return {
            "attributes": [
                {"key": "session.id", "value": {"stringValue": session}},
                {"key": "query_source", "value": {"stringValue": "main"}},
            ],
            "startTimeUnixNano": "1788989273064000000",
            "timeUnixNano": "1788989273225000000",
            **value,
        }

    metrics = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.cost.usage",
                                "unit": "USD",
                                "sum": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": [
                                        point("s1", {"asDouble": 0.4}),
                                        point("s2", {"asDouble": "abc"}),
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
        ]
    }
    line = json.dumps({"signal": "metrics", "body": metrics})
    (directory / "metrics.jsonl").write_text(line + "\n")
    return directory


# Taken once from the product at 6de1734, on the stream above. Written in as literals, so no
# commit is read when the suite runs.
BASELINE_AGENTS_TEXT = (
    '2 agent row(s), 0 unreadable line(s) skipped.\n'
    '  session=s1 workflow_run_id=None query_source=sdk agent=None  requests=2  '
    'models=claude-model  cost_usd=0.400000\n'
    '    input=6 output=8 cache_read=10 cache_creation=12 duration_ms=14\n'
    '  session=s2 workflow_run_id=None query_source=other agent=None  requests=1  '
    'models=claude-model  cost_usd=0.200000\n'
    '    input=3 output=4 cache_read=5 cache_creation=6 duration_ms=7\n'
    'Total cost_usd: 0.600000\n'
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill.\n"
    '\n'
    'subagent_completed counts Agent-tool agents only — a Workflow step\'s own requests '
    '(query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so this '
    'count does not cover them.\n'
    '  session=s1 agent_type=Explore count=2 duration_ms=100 total_tool_uses=3\n'
    '\n'
    'Totals check (claude_code.cost.usage vs summed cost_usd):\n'
    '  session=s1  log_cost_usd=0.400000  metric_cost_usd=0.400000\n'
    "  session=s2  log_cost_usd=0.200000  metric_cost_usd=0.000000  MISMATCH >1% — possible "
    "causes, which ka-telemetry cannot tell apart: the session may still be running, or may have "
    "ended before Claude Code exported all of it, so one of its two totals may not have caught up "
    "with the other; the receiver (ka-telemetry receive) may not have been running for part of the "
    "session, so some of what Claude Code sent may never have arrived; Claude Code's request costs "
    "and its cost metric may really disagree\n"
)
BASELINE_AGENTS_JSON = (
    '{\n'
    '  "unreadable_metric_lines": 0,\n'
    '  "metrics_file": {\n'
    '    "state": "read"\n'
    '  },\n'
    '  "skipped_lines": 0,\n'
    '  "total_cost_usd": 0.6000000000000001,\n'
    '  "rows": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "workflow_run_id": null,\n'
    '      "query_source": "sdk",\n'
    '      "agent_name": null,\n'
    '      "models": [\n'
    '        "claude-model"\n'
    '      ],\n'
    '      "request_count": 2,\n'
    '      "input_tokens": 6,\n'
    '      "output_tokens": 8,\n'
    '      "cache_read_tokens": 10,\n'
    '      "cache_creation_tokens": 12,\n'
    '      "cost_usd": 0.4,\n'
    '      "duration_ms": 14\n'
    '    },\n'
    '    {\n'
    '      "session_id": "s2",\n'
    '      "workflow_run_id": null,\n'
    '      "query_source": "other",\n'
    '      "agent_name": null,\n'
    '      "models": [\n'
    '        "claude-model"\n'
    '      ],\n'
    '      "request_count": 1,\n'
    '      "input_tokens": 3,\n'
    '      "output_tokens": 4,\n'
    '      "cache_read_tokens": 5,\n'
    '      "cache_creation_tokens": 6,\n'
    '      "cost_usd": 0.2,\n'
    '      "duration_ms": 7\n'
    '    }\n'
    '  ],\n'
    '  "subagent_coverage_note": "subagent_completed counts Agent-tool agents only \\u2014 a '
    'Workflow step\'s own requests (query_source \\"agent:builtin:workflow-subagent\\") emit no '
    'subagent_completed record, so this count does not cover them.",\n'
    '  "subagent_rows": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "agent_type": "Explore",\n'
    '      "count": 2,\n'
    '      "duration_ms": 100,\n'
    '      "total_tool_uses": 3\n'
    '    }\n'
    '  ],\n'
    '  "metrics_check": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "log_cost_usd": 0.4,\n'
    '      "metric_cost_usd": 0.4,\n'
    '      "within_tolerance": true,\n'
    '      "possible_causes": []\n'
    '    },\n'
    '    {\n'
    '      "session_id": "s2",\n'
    '      "log_cost_usd": 0.2,\n'
    '      "metric_cost_usd": 0.0,\n'
    '      "within_tolerance": false,\n'
    '      "possible_causes": [\n'
    '        "the session may still be running, or may have ended before Claude Code exported all '
    'of it, so one of its two totals may not have caught up with the other",\n'
    '        "the receiver (ka-telemetry receive) may not have been running for part of the '
    'session, so some of what Claude Code sent may never have arrived",\n'
    '        "Claude Code\'s request costs and its cost metric may really disagree"\n'
    '      ]\n'
    '    }\n'
    '  ],\n'
    '  "cost_estimate_note": "Every cost here is an estimate: Claude Code\'s own, in US dollars as '
    'Claude Code reported it, or a total, mean or difference of those estimates. None is a bill."\n'
    '}\n'
)
BASELINE_COMPARE_TEXT = (
    '2 agent row(s), 0 unreadable line(s) skipped.\n'
    '  session=s1 workflow_run_id=None query_source=sdk agent=None  requests=2  '
    'models=claude-model  cost_usd=0.400000\n'
    '    input=6 output=8 cache_read=10 cache_creation=12 duration_ms=14\n'
    '  session=s2 workflow_run_id=None query_source=other agent=None  requests=1  '
    'models=claude-model  cost_usd=0.200000\n'
    '    input=3 output=4 cache_read=5 cache_creation=6 duration_ms=7\n'
    'Total cost_usd: 0.600000\n'
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill.\n"
    '\n'
    'subagent_completed counts Agent-tool agents only — a Workflow step\'s own requests '
    '(query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so this '
    'count does not cover them.\n'
    '  session=s1 agent_type=Explore count=2 duration_ms=100 total_tool_uses=3\n'
    '\n'
    'Totals check (claude_code.cost.usage vs summed cost_usd):\n'
    '  session=s1  log_cost_usd=0.400000  metric_cost_usd=0.400000\n'
    "  session=s2  log_cost_usd=0.200000  metric_cost_usd=0.000000  MISMATCH >1% — possible "
    "causes, which ka-telemetry cannot tell apart: the session may still be running, or may have "
    "ended before Claude Code exported all of it, so one of its two totals may not have caught up "
    "with the other; the receiver (ka-telemetry receive) may not have been running for part of the "
    "session, so some of what Claude Code sent may never have arrived; Claude Code's request costs "
    "and its cost metric may really disagree\n"
    '\n'
    '--compare query_source sdk other:\n'
    '  sdk: runs=1  mean_cost_per_run=0.400000  total_cost_usd=0.400000\n'
    '    totals:  input=6 output=8 cache_read=10 cache_creation=12 cost_usd=0.400000 '
    'duration_ms=14\n'
    '    per run: input=6.00 output=8.00 cache_read=10.00 cache_creation=12.00 cost_usd=0.400000 '
    'duration_ms=14.00\n'
    '  other: runs=1  mean_cost_per_run=0.200000  total_cost_usd=0.200000\n'
    '    totals:  input=3 output=4 cache_read=5 cache_creation=6 cost_usd=0.200000 duration_ms=7\n'
    '    per run: input=3.00 output=4.00 cache_read=5.00 cache_creation=6.00 cost_usd=0.200000 '
    'duration_ms=7.00\n'
    '  other - sdk = -0.200000\n'
    '  other / sdk = 0.500000\n'
)
BASELINE_COMPARE_JSON = (
    '{\n'
    '  "unreadable_metric_lines": 0,\n'
    '  "metrics_file": {\n'
    '    "state": "read"\n'
    '  },\n'
    '  "skipped_lines": 0,\n'
    '  "total_cost_usd": 0.6000000000000001,\n'
    '  "rows": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "workflow_run_id": null,\n'
    '      "query_source": "sdk",\n'
    '      "agent_name": null,\n'
    '      "models": [\n'
    '        "claude-model"\n'
    '      ],\n'
    '      "request_count": 2,\n'
    '      "input_tokens": 6,\n'
    '      "output_tokens": 8,\n'
    '      "cache_read_tokens": 10,\n'
    '      "cache_creation_tokens": 12,\n'
    '      "cost_usd": 0.4,\n'
    '      "duration_ms": 14\n'
    '    },\n'
    '    {\n'
    '      "session_id": "s2",\n'
    '      "workflow_run_id": null,\n'
    '      "query_source": "other",\n'
    '      "agent_name": null,\n'
    '      "models": [\n'
    '        "claude-model"\n'
    '      ],\n'
    '      "request_count": 1,\n'
    '      "input_tokens": 3,\n'
    '      "output_tokens": 4,\n'
    '      "cache_read_tokens": 5,\n'
    '      "cache_creation_tokens": 6,\n'
    '      "cost_usd": 0.2,\n'
    '      "duration_ms": 7\n'
    '    }\n'
    '  ],\n'
    '  "subagent_coverage_note": "subagent_completed counts Agent-tool agents only \\u2014 a '
    'Workflow step\'s own requests (query_source \\"agent:builtin:workflow-subagent\\") emit no '
    'subagent_completed record, so this count does not cover them.",\n'
    '  "subagent_rows": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "agent_type": "Explore",\n'
    '      "count": 2,\n'
    '      "duration_ms": 100,\n'
    '      "total_tool_uses": 3\n'
    '    }\n'
    '  ],\n'
    '  "metrics_check": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "log_cost_usd": 0.4,\n'
    '      "metric_cost_usd": 0.4,\n'
    '      "within_tolerance": true,\n'
    '      "possible_causes": []\n'
    '    },\n'
    '    {\n'
    '      "session_id": "s2",\n'
    '      "log_cost_usd": 0.2,\n'
    '      "metric_cost_usd": 0.0,\n'
    '      "within_tolerance": false,\n'
    '      "possible_causes": [\n'
    '        "the session may still be running, or may have ended before Claude Code exported all '
    'of it, so one of its two totals may not have caught up with the other",\n'
    '        "the receiver (ka-telemetry receive) may not have been running for part of the '
    'session, so some of what Claude Code sent may never have arrived",\n'
    '        "Claude Code\'s request costs and its cost metric may really disagree"\n'
    '      ]\n'
    '    }\n'
    '  ],\n'
    '  "compare": {\n'
    '    "attr": "query_source",\n'
    '    "a_value": "sdk",\n'
    '    "b_value": "other",\n'
    '    "a": {\n'
    '      "run_count": 1,\n'
    '      "mean_cost_per_run": 0.4,\n'
    '      "total_cost_usd": 0.4,\n'
    '      "totals": {\n'
    '        "input_tokens": 6,\n'
    '        "output_tokens": 8,\n'
    '        "cache_read_tokens": 10,\n'
    '        "cache_creation_tokens": 12,\n'
    '        "cost_usd": 0.4,\n'
    '        "duration_ms": 14\n'
    '      },\n'
    '      "mean_per_run": {\n'
    '        "input_tokens": 6.0,\n'
    '        "output_tokens": 8.0,\n'
    '        "cache_read_tokens": 10.0,\n'
    '        "cache_creation_tokens": 12.0,\n'
    '        "cost_usd": 0.4,\n'
    '        "duration_ms": 14.0\n'
    '      }\n'
    '    },\n'
    '    "b": {\n'
    '      "run_count": 1,\n'
    '      "mean_cost_per_run": 0.2,\n'
    '      "total_cost_usd": 0.2,\n'
    '      "totals": {\n'
    '        "input_tokens": 3,\n'
    '        "output_tokens": 4,\n'
    '        "cache_read_tokens": 5,\n'
    '        "cache_creation_tokens": 6,\n'
    '        "cost_usd": 0.2,\n'
    '        "duration_ms": 7\n'
    '      },\n'
    '      "mean_per_run": {\n'
    '        "input_tokens": 3.0,\n'
    '        "output_tokens": 4.0,\n'
    '        "cache_read_tokens": 5.0,\n'
    '        "cache_creation_tokens": 6.0,\n'
    '        "cost_usd": 0.2,\n'
    '        "duration_ms": 7.0\n'
    '      }\n'
    '    },\n'
    '    "diff": -0.2,\n'
    '    "ratio": 0.5\n'
    '  },\n'
    '  "cost_estimate_note": "Every cost here is an estimate: Claude Code\'s own, in US dollars as '
    'Claude Code reported it, or a total, mean or difference of those estimates. None is a bill."\n'
    '}\n'
)
BASELINE_CHECK_TEXT = (
    'Totals check (claude_code.cost.usage vs summed cost_usd):\n'
    '  session=s1  log_cost_usd=0.400000  metric_cost_usd=0.400000\n'
    "  session=s2  log_cost_usd=0.200000  metric_cost_usd=0.000000  MISMATCH >1% — possible "
    "causes, which ka-telemetry cannot tell apart: the session may still be running, or may have "
    "ended before Claude Code exported all of it, so one of its two totals may not have caught up "
    "with the other; the receiver (ka-telemetry receive) may not have been running for part of the "
    "session, so some of what Claude Code sent may never have arrived; Claude Code's request costs "
    "and its cost metric may really disagree\n"
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill.\n"
)
BASELINE_CHECK_JSON = (
    '{\n'
    '  "unreadable_metric_lines": 0,\n'
    '  "metrics_file": {\n'
    '    "state": "read"\n'
    '  },\n'
    '  "skipped_lines": 0,\n'
    '  "metrics_check": [\n'
    '    {\n'
    '      "session_id": "s1",\n'
    '      "log_cost_usd": 0.4,\n'
    '      "metric_cost_usd": 0.4,\n'
    '      "within_tolerance": true,\n'
    '      "possible_causes": []\n'
    '    },\n'
    '    {\n'
    '      "session_id": "s2",\n'
    '      "log_cost_usd": 0.2,\n'
    '      "metric_cost_usd": 0.0,\n'
    '      "within_tolerance": false,\n'
    '      "possible_causes": [\n'
    '        "the session may still be running, or may have ended before Claude Code exported all '
    'of it, so one of its two totals may not have caught up with the other",\n'
    '        "the receiver (ka-telemetry receive) may not have been running for part of the '
    'session, so some of what Claude Code sent may never have arrived",\n'
    '        "Claude Code\'s request costs and its cost metric may really disagree"\n'
    '      ]\n'
    '    }\n'
    '  ],\n'
    '  "cost_estimate_note": "Every cost here is an estimate: Claude Code\'s own, in US dollars as '
    'Claude Code reported it, or a total, mean or difference of those estimates. None is a bill."\n'
    '}\n'
)


def _sections(text: str) -> list[str]:
    """The blank-line separated blocks of an `agents` report: the rows with the total, the
    Agent-tool agents, the totals check and, when asked for, the `--compare` block."""
    return text.rstrip("\n").split("\n\n")


def _with_totals_check_counts(text: str) -> str:
    """The stored totals check as it reads now: `s1`'s one invalid request cost marks its log
    figure, and `s2`'s one invalid cost point marks its metric figure. Nothing else moves. Each
    stored figure is found exactly once, so neither mark can drop out of the expectation."""
    return _each_replaced_once(
        text,
        (
            "session=s1  log_cost_usd=0.400000  metric",
            "session=s1  log_cost_usd=0.400000 (invalid: 1)  metric",
        ),
        (
            "metric_cost_usd=0.000000  MISMATCH",
            "metric_cost_usd=0.000000 (invalid: 1)  MISMATCH",
        ),
    )


def _each_replaced_once(text: str, *replacements: tuple[str, str]) -> str:
    """`text` with each `(old, new)` replaced, after checking `old` occurs in it exactly once."""
    for old, new in replacements:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def _with_agent_row_counts(block: str) -> str:
    """The stored Agent-tool agents block as it reads now: the one row's two figures are marked."""
    return _each_replaced_once(
        block,
        (
            "count=2 duration_ms=100 total_tool_uses=3",
            "count=2 duration_ms=100 (invalid: 1) total_tool_uses=3 (invalid: 1)",
        ),
    )


def _with_compare_counts(block: str) -> str:
    """The stored `--compare` block as it reads now: side `sdk` holds the one invalid request
    cost, so its first line and both figure lines mark `cost_usd`. Side `other` and the two lines
    under the sides do not move."""
    return _each_replaced_once(
        block,
        (
            "  sdk: runs=1  mean_cost_per_run=0.400000  total_cost_usd=0.400000",
            "  sdk: runs=1  mean_cost_per_run=0.400000 (invalid: 1)  "
            "total_cost_usd=0.400000 (invalid: 1)",
        ),
        (
            "totals:  input=6 output=8 cache_read=10 cache_creation=12 cost_usd=0.400000 "
            "duration_ms=14",
            "totals:  input=6 output=8 cache_read=10 cache_creation=12 cost_usd=0.400000 "
            "(invalid: 1) duration_ms=14",
        ),
        (
            "per run: input=6.00 output=8.00 cache_read=10.00 cache_creation=12.00 "
            "cost_usd=0.400000 duration_ms=14.00",
            "per run: input=6.00 output=8.00 cache_read=10.00 cache_creation=12.00 "
            "cost_usd=0.400000 (invalid: 1) duration_ms=14.00",
        ),
    )


def _with_compare_members(compare: dict | None) -> dict | None:
    """The stored `compare` member with the one `invalid_values` member side `a` (`sdk`) gains."""
    if compare is None:
        return None
    return {**compare, "a": {**compare["a"], "invalid_values": {"cost_usd": 1}}}


def _with_agent_row_members(rows: list[dict]) -> list[dict]:
    """The stored `subagent_rows` entries with the one `invalid_values` member they gain."""
    return [{**row, "invalid_values": {"duration_ms": 1, "total_tool_uses": 1}} for row in rows]


def _with_totals_check_members(checks: list[dict]) -> list[dict]:
    """The stored `metrics_check` entries with the two `invalid_values` members they gain."""
    members = {"s1": {"log_cost_usd": 1}, "s2": {"metric_cost_usd": 1}}
    return [{**entry, "invalid_values": members[entry["session_id"]]} for entry in checks]


@pytest.fixture
def unchanged_sections_stream(tmp_path: Path) -> Path:
    return _unchanged_sections_stream(tmp_path / "stream")


def test_unaffected_agents_sections_carry_no_count(
    installed: InstalledProduct, home: Path, unchanged_sections_stream: Path
):
    plain = _agents(installed, home, unchanged_sections_stream)
    compared = _agents(
        installed, home, unchanged_sections_stream, "--compare", "query_source", "sdk", "other"
    )
    plain_json = _agents(installed, home, unchanged_sections_stream, "--json")
    compared_json = _agents(
        installed, home, unchanged_sections_stream, "--compare", "query_source", "sdk", "other",
        "--json",
    )
    for result in (plain, compared, plain_json, compared_json):
        assert result.returncode == 0 and result.stderr == ""

    # Only the row with the invalid request cost gains a count in the rows; the Agent-tool agents
    # row gains its two marks; the totals check gains its two marks; the rest equals the baseline.
    now, then = _sections(plain.stdout), _sections(BASELINE_AGENTS_TEXT)
    assert now[0].splitlines()[0] == then[0].splitlines()[0]
    assert now[1] == _with_agent_row_counts(then[1])
    assert now[2:-1] == then[2:-1]
    assert now[-1] == _with_totals_check_counts(then[-1])
    now, then = _sections(compared.stdout), _sections(BASELINE_COMPARE_TEXT)
    totals = next(i for i, block in enumerate(then) if block.startswith("Totals check"))
    assert now[1] == _with_agent_row_counts(then[1])
    assert now[2:totals] == then[2:totals]
    assert now[totals] == _with_totals_check_counts(then[totals])
    assert now[totals + 1] == _with_compare_counts(then[totals + 1])
    assert now[totals + 2 :] == then[totals + 2 :]
    assert now[totals + 1].count("(invalid:") == 4
    for index, block in enumerate(now[1:], start=1):
        if index not in (totals, totals + 1, 1):
            assert "(invalid:" not in block

    pairs = ((plain_json, BASELINE_AGENTS_JSON), (compared_json, BASELINE_COMPARE_JSON))
    for result, baseline in pairs:
        document, before = json.loads(result.stdout), json.loads(baseline)
        assert document.get("skipped_lines") == before.get("skipped_lines")
        assert document.get("compare") == _with_compare_members(before.get("compare"))
        if "compare" in document:
            assert list(document["compare"]["a"])[-1] == "invalid_values"
            assert json.dumps(document["compare"]).count("invalid_values") == 1
        assert document["subagent_rows"] == _with_agent_row_members(before["subagent_rows"])
        assert document["metrics_check"] == _with_totals_check_members(before["metrics_check"])
        assert "invalid_values" not in json.dumps(
            {
                k: v
                for k, v in document.items()
                if k not in ("rows", "invalid_values", "metrics_check", "subagent_rows", "compare")
            }
        )


def test_invalid_metric_and_completed_agent_values_still_read_as_zero_with_no_count(
    installed: InstalledProduct, home: Path, unchanged_sections_stream: Path
):
    document = json.loads(_agents(installed, home, unchanged_sections_stream, "--json").stdout)

    # An invalid completed-agent duration and tool count add zero and are counted on their row.
    assert document["subagent_rows"] == [
        {
            "session_id": "s1",
            "agent_type": "Explore",
            "count": 2,
            "duration_ms": 100,
            "total_tool_uses": 3,
            "invalid_values": {"duration_ms": 1, "total_tool_uses": 1},
        }
    ]
    # An invalid cost-metric point reads as zero and is not counted.
    by_session = {check["session_id"]: check for check in document["metrics_check"]}
    assert by_session["s2"]["metric_cost_usd"] == 0
    # The cost metric's and the request costs' own values are counted in the totals check.
    assert by_session["s1"]["invalid_values"] == {"log_cost_usd": 1}
    assert by_session["s2"]["invalid_values"] == {"metric_cost_usd": 1}
    # The one invalid request cost is the only count the report carries.
    assert document["invalid_values"] == {"total_cost_usd": 1}


def test_check_output_is_unchanged_in_text_and_json(
    installed: InstalledProduct, home: Path, unchanged_sections_stream: Path
):
    text = run(installed, "check", "--dir", str(unchanged_sections_stream), home=home)
    as_json = run(installed, "check", "--dir", str(unchanged_sections_stream), "--json", home=home)

    assert text.returncode == 0 and as_json.returncode == 0
    # The stored output gains the two marks and the two members, and no byte more.
    assert text.stdout == _with_totals_check_counts(BASELINE_CHECK_TEXT)
    log_member = '      "invalid_values": {\n        "log_cost_usd": 1\n      }\n'
    metric_member = '      "invalid_values": {\n        "metric_cost_usd": 1\n      }\n'
    assert as_json.stdout == _each_replaced_once(
        BASELINE_CHECK_JSON,
        (
            '      "possible_causes": []\n    },',
            '      "possible_causes": [],\n' + log_member + "    },",
        ),
        (
            'cost metric may really disagree"\n      ]\n    }',
            'cost metric may really disagree"\n      ],\n' + metric_member + "    }",
        ),
    )


# ── The Agent-tool agents rows count the values they left out ───────────────

AGENTS_NOTE = "subagent_completed counts Agent-tool agents only"
COMPLETED_FIELDS = ("duration_ms", "total_tool_uses")
BAD_ENVELOPES = {
    "string": {"stringValue": "slow"},
    "true": {"boolValue": True},
}


def _completed(session: str = "s1", agent_type: str = "Explore", **fields) -> dict:
    """One `subagent_completed` record, each field in the envelope given (`ABSENT` omits it)."""
    attributes = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "subagent_completed"}},
        {"key": "agent_type", "value": {"stringValue": agent_type}},
    ]
    attributes.extend(
        {"key": key, "value": value} for key, value in fields.items() if value is not ABSENT
    )
    return {"attributes": attributes}


def _number(value: int) -> dict:
    return {"intValue": value}


def _agent_lines(text: str) -> list[str]:
    """The Agent-tool agents rows of a text report: the lines under the coverage note."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(AGENTS_NOTE)) + 1
    end = lines.index("", start)
    return lines[start:end]


def _agent_probe(directory: Path) -> Path:
    """The spec's probe (change spec § Examples), record for record: two valid requests, then
    `s1`/`Explore` with five records — (100, 3), (`"slow"`, `true`), (`false`, `"many"`), (neither
    field), (`intValue` `"oops"`, `"2"`) — `s1`/`general-purpose` with (50, 1), and `s2`/`Explore`
    with (an empty `kvlistValue`, 4). The record with neither field counts nothing, and `"2"` is a
    number."""
    records = [
        _request("s1"),
        _request("s2"),
        _completed(duration_ms=_number(100), total_tool_uses=_number(3)),
        _completed(duration_ms={"stringValue": "slow"}, total_tool_uses={"boolValue": True}),
        _completed(duration_ms={"boolValue": False}, total_tool_uses={"stringValue": "many"}),
        _completed(),
        _completed(duration_ms={"intValue": "oops"}, total_tool_uses={"stringValue": "2"}),
        _completed(
            agent_type="general-purpose", duration_ms=_number(50), total_tool_uses=_number(1)
        ),
        _completed("s2", duration_ms={"kvlistValue": {}}, total_tool_uses=_number(4)),
    ]
    return _stream(directory, records)


@pytest.mark.parametrize("kind", BAD_ENVELOPES)
@pytest.mark.parametrize("field", COMPLETED_FIELDS)
def test_an_invalid_completed_agent_value_is_counted_beside_its_figure_in_text_and_json(
    installed: InstalledProduct, home: Path, tmp_path: Path, field: str, kind: str
):
    other = next(name for name in COMPLETED_FIELDS if name != field)
    stream = _stream(
        tmp_path / "stream",
        [
            _completed(**{field: _number(30), other: _number(2)}),
            _completed(**{field: BAD_ENVELOPES[kind], other: _number(3)}),
        ],
    )

    text, document = _both(installed, home, stream)

    figures = {field: 30, other: 5}
    expected = (
        f"  session=s1 agent_type=Explore count=2 duration_ms={figures['duration_ms']}"
        f"{' (invalid: 1)' if field == 'duration_ms' else ''}"
        f" total_tool_uses={figures['total_tool_uses']}"
        f"{' (invalid: 1)' if field == 'total_tool_uses' else ''}"
    )
    assert _agent_lines(text) == [expected]
    (entry,) = document["subagent_rows"]
    assert entry[field] == 30 and entry[other] == 5
    assert entry["invalid_values"] == {field: 1}


def test_the_probe_text_section_lines_are_exact(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    text, _ = _both(installed, home, _agent_probe(tmp_path / "stream"))

    assert _agent_lines(text) == [
        "  session=s1 agent_type=Explore count=5 duration_ms=100 (invalid: 3) "
        "total_tool_uses=5 (invalid: 2)",
        "  session=s1 agent_type=general-purpose count=1 duration_ms=50 total_tool_uses=1",
        "  session=s2 agent_type=Explore count=1 duration_ms=0 (invalid: 1) total_tool_uses=4",
    ]


def test_the_probe_json_entries_carry_invalid_values_last_in_field_order(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    _, document = _both(installed, home, _agent_probe(tmp_path / "stream"))

    explore, general, other_session = document["subagent_rows"]
    five_members = ["session_id", "agent_type", "count", "duration_ms", "total_tool_uses"]
    assert explore == {
        "session_id": "s1",
        "agent_type": "Explore",
        "count": 5,
        "duration_ms": 100,
        "total_tool_uses": 5,
        "invalid_values": {"duration_ms": 3, "total_tool_uses": 2},
    }
    assert list(explore) == [*five_members, "invalid_values"]
    assert list(explore["invalid_values"]) == ["duration_ms", "total_tool_uses"]
    assert general == {
        "session_id": "s1",
        "agent_type": "general-purpose",
        "count": 1,
        "duration_ms": 50,
        "total_tool_uses": 1,
    }
    assert list(general) == five_members
    assert other_session == {
        "session_id": "s2",
        "agent_type": "Explore",
        "count": 1,
        "duration_ms": 0,
        "total_tool_uses": 4,
        "invalid_values": {"duration_ms": 1},
    }
    assert list(other_session) == [*five_members, "invalid_values"]


def test_json_entry_without_invalid_value_has_exactly_the_five_members(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "stream",
        [
            _completed("clean", duration_ms=_number(5), total_tool_uses=_number(1)),
            _completed("zero", duration_ms=_number(0), total_tool_uses=_number(0)),
            _completed("none"),
        ],
    )

    text, document = _both(installed, home, stream)

    assert [list(entry) for entry in document["subagent_rows"]] == [
        ["session_id", "agent_type", "count", "duration_ms", "total_tool_uses"]
    ] * 3
    assert "(invalid:" not in "\n".join(_agent_lines(text))


@pytest.mark.parametrize("field", COMPLETED_FIELDS)
def test_absent_field_shows_no_mark_and_no_member(
    installed: InstalledProduct, home: Path, tmp_path: Path, field: str
):
    other = next(name for name in COMPLETED_FIELDS if name != field)
    stream = _stream(
        tmp_path / "stream",
        [
            _completed(**{field: _number(100), other: _number(3)}),
            _completed(**{field: ABSENT, other: _number(2)}),
            _completed(**{field: _number(40), other: ABSENT}),
        ],
    )

    text, document = _both(installed, home, stream)

    # The figure is the other records' sum, 100 + 40, and nothing marks it (CP-665, Example 9).
    figures = {field: 140, other: 5}
    assert _agent_lines(text) == [
        f"  session=s1 agent_type=Explore count=3 duration_ms={figures['duration_ms']} "
        f"total_tool_uses={figures['total_tool_uses']}"
    ]
    (entry,) = document["subagent_rows"]
    assert entry == {
        "session_id": "s1",
        "agent_type": "Explore",
        "count": 3,
        "duration_ms": figures["duration_ms"],
        "total_tool_uses": figures["total_tool_uses"],
    }


def test_where_keeps_whole_row_counts(installed: InstalledProduct, home: Path, tmp_path: Path):
    stream = _agent_probe(tmp_path / "stream")
    whole_text, whole = _both(installed, home, stream)

    def narrowed(selection: str) -> tuple[list[str], list[dict]]:
        text = _agents(installed, home, stream, "--where", selection)
        as_json = _agents(installed, home, stream, "--where", selection, "--json")
        for result in (text, as_json):
            assert result.returncode == 0 and result.stderr == ""
        return _agent_lines(text.stdout), json.loads(as_json.stdout)["subagent_rows"]

    # A shown row carries the counts it carries unfiltered, in text and JSON (CP-657).
    only_s2_lines, only_s2_rows = narrowed("session.id=s2")
    assert only_s2_lines == [
        "  session=s2 agent_type=Explore count=1 duration_ms=0 (invalid: 1) total_tool_uses=4"
    ]
    assert only_s2_rows == whole["subagent_rows"][2:]
    only_s1_lines, only_s1_rows = narrowed("session.id=s1")
    assert only_s1_lines == _agent_lines(whole_text)[:2]
    assert only_s1_rows == whole["subagent_rows"][:2]
    # A selection that shows no session keeps today's filtered-empty line and no mark (CP-662).
    nothing_lines, nothing_rows = narrowed("session.id=nothing")
    assert nothing_lines == [
        "subagent_completed records were read, but none is in a session the rows show. "
        "Run without --where to list them."
    ]
    assert nothing_rows == []


def test_a_request_only_filter_inside_a_shown_session_does_not_re_filter_completed_records(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "stream",
        [
            _request("s1", query_source={"stringValue": "main"}),
            _request("s1", query_source={"stringValue": "other"}),
            _completed(duration_ms={"stringValue": "slow"}, total_tool_uses=_number(2)),
        ],
    )

    narrowed = _agents(installed, home, stream, "--where", "query_source=main")

    assert narrowed.returncode == 0
    assert _agent_lines(narrowed.stdout) == [
        "  session=s1 agent_type=Explore count=1 duration_ms=0 (invalid: 1) total_tool_uses=2"
    ]


# `check` on the spec's probe, as the product printed it at ab60557, before this change; the text's
# first line names the directory read.
PROBE_CAUSES = (
    "the session may have ended before Claude Code exported a cost metric for it",
    "the session may still be running",
    "the receiver (ka-telemetry receive) may not have been running when Claude Code sent the "
    "cost metric",
)
PROBE_ESTIMATE_NOTE = (
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill."
)
PROBE_CHECK_TEXT_LINES = (
    "No metrics.jsonl was found in {directory}, so no session has a cost metric to check against.",
    "Totals check (claude_code.cost.usage vs summed cost_usd):",
    *(
        f"  session={session}  log_cost_usd=0.500000  no metric export — possible causes, which "
        f"ka-telemetry cannot tell apart: {'; '.join(PROBE_CAUSES)}"
        for session in ("s1", "s2")
    ),
    PROBE_ESTIMATE_NOTE,
)
PROBE_CHECK_JSON = {
    "unreadable_metric_lines": 0,
    "metrics_file": {"state": "nothing_received"},
    "skipped_lines": 0,
    "metrics_check": [
        {
            "session_id": session,
            "log_cost_usd": 0.5,
            "metric_cost_usd": None,
            "within_tolerance": None,
            "possible_causes": list(PROBE_CAUSES),
        }
        for session in ("s1", "s2")
    ],
    "cost_estimate_note": PROBE_ESTIMATE_NOTE,
}


def test_check_and_prefix_and_empty_section_are_unchanged(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    probe = _agent_probe(tmp_path / "probe")
    empty = _stream(tmp_path / "empty", [_request()])

    check_text = run(installed, "check", "--dir", str(probe), home=home)
    check_json = run(installed, "check", "--dir", str(probe), "--json", home=home)
    refused = _agents(installed, home, probe, "--prefix", "invalid_values")
    nothing = _agents(installed, home, empty)

    # `check` on the probe prints, byte for byte, what it printed before this change (CP-662).
    assert check_text.returncode == 0 and check_json.returncode == 0
    expected_text = "\n".join(PROBE_CHECK_TEXT_LINES).format(directory=probe) + "\n"
    assert check_text.stdout == expected_text
    assert check_json.stdout == json.dumps(PROBE_CHECK_JSON, indent=2) + "\n"
    # `--prefix invalid_values` is refused with today's status and words.
    assert refused.returncode == 2 and refused.stdout == ""
    assert refused.stderr.splitlines()[-2:] == [INVALID_VALUES_REFUSAL, AGENTS_NEXT_STEP]
    # A stream with no completed record keeps today's line, and nothing else, in the section.
    assert _agent_lines(nothing.stdout) == ["No subagent_completed records read."]


def test_stream_parity_reference_output_is_byte_identical(installed: InstalledProduct, home: Path):
    """CP-661: `agents --dir tests/stream_parity`, text and `--json`, run from the product's top
    directory, print the bytes they printed at ab60557 — the spec's recorded sha1 of each — and
    show no count in the Agent-tool agents section."""
    text = run(installed, "agents", "--dir", "tests/stream_parity", home=home, cwd=PRODUCT_ROOT)
    as_json = run(
        installed, "agents", "--dir", "tests/stream_parity", "--json", home=home, cwd=PRODUCT_ROOT
    )

    for result in (text, as_json):
        assert result.returncode == 0 and result.stderr == ""
    assert hashlib.sha1(text.stdout.encode()).hexdigest() == (
        "d3ebebb90a37acd78f6b7b5c6ae46ca436ec5763"
    )
    assert hashlib.sha1(as_json.stdout.encode()).hexdigest() == (
        "47e644dfd6b3a71a857469a56b842b4b8b703bae"
    )
    assert len(_agent_lines(text.stdout)) == 3
    assert "(invalid:" not in "\n".join(_agent_lines(text.stdout))
    assert all("invalid_values" not in row for row in json.loads(as_json.stdout)["subagent_rows"])
