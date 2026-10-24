"""End-to-end tests of the installed `ka-telemetry check` and the totals check of `ka-telemetry
agents` naming the values they could not read as numbers (CP-639–CP-653).

Both commands are driven from the scratch virtualenv the `installed` fixture builds, under a `HOME`
of its own, on real temporary streams written as `logs.jsonl` and `metrics.jsonl` lines. Every
expected line and member is written out here; none is taken from the product's own output.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import DATA_SET, run

if TYPE_CHECKING:
    from conftest import InstalledProduct

ABSENT = object()
COMMANDS = ("check", "agents")
MISMATCH_CAUSES_SUFFIX = (
    " — possible causes, which ka-telemetry cannot tell apart: the session may still be running, "
    "or may have ended before Claude Code exported all of it, so one of its two totals may not "
    "have caught up with the other; the receiver (ka-telemetry receive) may not have been running "
    "for part of the session, so some of what Claude Code sent may never have arrived; Claude "
    "Code's request costs and its cost metric may really disagree"
)
NO_EXPORT_CAUSES_PREFIX = " — possible causes, which ka-telemetry cannot tell apart: "


def _request(session: str = "s1", cost=ABSENT, *, query_source: str = "sdk", **extra) -> dict:
    """One `api_request` log record; `cost` is the `cost_usd` envelope, `ABSENT` leaves it out."""
    attributes = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "api_request"}},
        {"key": "model", "value": {"stringValue": "claude-model"}},
        {"key": "query_source", "value": {"stringValue": query_source}},
        {"key": "input_tokens", "value": {"intValue": 1}},
    ]
    if cost is not ABSENT:
        attributes.append({"key": "cost_usd", "value": cost})
    attributes.extend({"key": key, "value": value} for key, value in extra.items())
    return {"attributes": attributes}


def _point(session: str | None, **members) -> dict:
    """One cost datapoint; `members` are its `asDouble` / `asInt` members as given."""
    attributes = [{"key": "query_source", "value": {"stringValue": "main"}}]
    if session is not None:
        attributes.append({"key": "session.id", "value": {"stringValue": session}})
    return {
        "attributes": attributes,
        "startTimeUnixNano": "1788989273064000000",
        "timeUnixNano": "1788989273225000000",
        **members,
    }


def _logs_line(requests: list[dict]) -> str:
    body = {
        "resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [{"logRecords": requests}]}]
    }
    return json.dumps({"signal": "logs", "body": body})


def _metrics_line(points: list[dict]) -> str:
    metric = {
        "name": "claude_code.cost.usage",
        "unit": "USD",
        "sum": {"aggregationTemporality": 1, "dataPoints": points},
    }
    body = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [metric]}]}]}
    return json.dumps({"signal": "metrics", "body": body})


def _stream(
    directory: Path,
    requests: list[dict],
    points: list[dict] | None = None,
    *,
    extra_log_lines: tuple[str, ...] = (),
    extra_metric_lines: tuple[str, ...] = (),
) -> Path:
    """`directory` holding `logs.jsonl` with one export of `requests`, and, unless `points` is
    `None`, a `metrics.jsonl` with one export of `points`."""
    directory.mkdir(exist_ok=True)
    lines = [_logs_line(requests), *extra_log_lines]
    (directory / "logs.jsonl").write_text("\n".join(lines) + "\n")
    if points is not None:
        lines = [_metrics_line(points), *extra_metric_lines]
        (directory / "metrics.jsonl").write_text("\n".join(lines) + "\n")
    return directory


def _probe(directory: Path) -> Path:
    """The spec's two-session probe."""
    requests = [
        _request("s1", {"doubleValue": 0.4}),
        _request("s1", {"stringValue": "abc"}),
        _request("s1", {"boolValue": True}),
        _request("s1"),
        _request("s1", {"stringValue": "0.1"}),
        _request("s2", {"doubleValue": 0.2}),
    ]
    points = [
        _point("s1", asDouble=0.3),
        _point("s1", asDouble="abc"),
        _point("s1", asDouble=True),
        _point("s1"),
        _point("s1", asDouble=None),
        _point("s1", asInt="0.2"),
        _point("s2", asDouble="xyz"),
    ]
    return _stream(directory, requests, points)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _text_and_json(installed: InstalledProduct, home: Path, command: str, stream: Path, *more: str):
    """The text output and the parsed JSON document of `command` on `stream`; both exit 0 with an
    empty stderr."""
    outputs = []
    for flags in ((), ("--json",)):
        result = run(installed, command, "--dir", str(stream), *more, *flags, home=home)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        outputs.append(result.stdout)
    return outputs[0], json.loads(outputs[1])


def _session_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("  session=") and "  log_cost_usd=" in line
    ]


def _entry(document: dict, session: str) -> dict:
    return next(e for e in document["metrics_check"] if e["session_id"] == session)


# ── The probe ──────────────────────────────────────────────────────────────────────────────────


def test_check_probe_text_and_json(installed: InstalledProduct, home: Path, tmp_path: Path):
    stream = _probe(tmp_path / "probe")

    text, document = _text_and_json(installed, home, "check", stream)

    assert _session_lines(text) == [
        "  session=s1  log_cost_usd=0.500000 (invalid: 2)  metric_cost_usd=0.500000 (invalid: 2)",
        "  session=s2  log_cost_usd=0.200000  metric_cost_usd=0.000000 (invalid: 1)  MISMATCH >1%"
        + MISMATCH_CAUSES_SUFFIX,
    ]
    assert text.count("\n") == 4  # heading, two sessions, the cost note: no line for a count
    s1, s2 = _entry(document, "s1"), _entry(document, "s2")
    assert list(s1) == [
        "session_id",
        "log_cost_usd",
        "metric_cost_usd",
        "within_tolerance",
        "possible_causes",
        "invalid_values",
    ]
    assert (s1["log_cost_usd"], s1["metric_cost_usd"], s1["within_tolerance"]) == (0.5, 0.5, True)
    assert s1["possible_causes"] == []
    assert s1["invalid_values"] == {"log_cost_usd": 2, "metric_cost_usd": 2}
    assert list(s1["invalid_values"]) == ["log_cost_usd", "metric_cost_usd"]
    assert (s2["log_cost_usd"], s2["metric_cost_usd"], s2["within_tolerance"]) == (0.2, 0.0, False)
    assert len(s2["possible_causes"]) == 3
    assert list(s2)[-1] == "invalid_values"
    assert s2["invalid_values"] == {"metric_cost_usd": 1}
    for entry in (s1, s2):
        assert all(type(n) is int for n in entry["invalid_values"].values())


def test_agents_probe_totals_check_matches_check(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _probe(tmp_path / "probe")
    checked_text, checked_json = _text_and_json(installed, home, "check", stream)
    agents_text, agents_json = _text_and_json(installed, home, "agents", stream)

    assert _session_lines(agents_text) == _session_lines(checked_text)
    assert _session_lines(agents_text)[0] == (
        "  session=s1  log_cost_usd=0.500000 (invalid: 2)  metric_cost_usd=0.500000 (invalid: 2)"
    )
    assert _session_lines(agents_text)[1].startswith(
        "  session=s2  log_cost_usd=0.200000  metric_cost_usd=0.000000 (invalid: 1)  MISMATCH >1%"
    )
    assert _entry(agents_json, "s1")["invalid_values"] == {"log_cost_usd": 2, "metric_cost_usd": 2}
    assert agents_json["metrics_check"] == checked_json["metrics_check"]
    assert [list(e) for e in agents_json["metrics_check"]] == [
        list(e) for e in checked_json["metrics_check"]
    ]
    assert "Total cost_usd: 0.700000 (invalid: 2)" in agents_text.splitlines()
    rows = [
        line
        for line in agents_text.splitlines()
        if line.startswith("  session=") and " requests=" in line
    ]
    assert "cost_usd=0.500000 (invalid: 2)" in rows[0]
    assert "(invalid:" not in rows[1]
    assert agents_json["invalid_values"] == {"total_cost_usd": 2}


# ── The figures that stay, the counts that appear ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("requests", "points", "line_end"),
    [
        pytest.param(
            [_request("s1", {"doubleValue": 0.0})],
            [_point("s1", asDouble="abc")],
            "log_cost_usd=0.000000  metric_cost_usd=0.000000 (invalid: 1)",
            id="zero-log-and-only-invalid-points",
        ),
        pytest.param(
            [_request("s1", {"doubleValue": 0.0})],
            [_point("s1")],
            "log_cost_usd=0.000000  metric_cost_usd=0.000000",
            id="zero-log-and-only-valueless-points",
        ),
        pytest.param(
            [_request("s1", {"doubleValue": 0.5})],
            [_point("s1", asDouble=True)],
            "log_cost_usd=0.500000  metric_cost_usd=0.000000 (invalid: 1)  MISMATCH >1%"
            + MISMATCH_CAUSES_SUFFIX,
            id="positive-log-and-only-invalid-points",
        ),
        pytest.param(
            [_request("s1", {"doubleValue": 0.5})],
            [_point("s1", asDouble=None)],
            "log_cost_usd=0.500000  metric_cost_usd=0.000000  MISMATCH >1%"
            + MISMATCH_CAUSES_SUFFIX,
            id="positive-log-and-only-valueless-points",
        ),
        pytest.param(
            [_request("s1", {"doubleValue": 0.0})],
            [_point("s1", asDouble=0.3), _point("s1", asDouble="abc")],
            "log_cost_usd=0.000000  metric_cost_usd=0.300000 (invalid: 1)  MISMATCH >1%"
            + MISMATCH_CAUSES_SUFFIX,
            id="zero-log-and-positive-metric",
        ),
    ],
)
@pytest.mark.parametrize("command", COMMANDS)
def test_only_invalid_metric_points_keep_a_zero_metric_figure_and_count(
    installed: InstalledProduct, home: Path, tmp_path: Path, command, requests, points, line_end
):
    stream = _stream(tmp_path / "s", requests, points)

    text, document = _text_and_json(installed, home, command, stream)

    assert _session_lines(text) == ["  session=s1  " + line_end]
    counted = "(invalid: 1)" in line_end
    entry = _entry(document, "s1")
    assert ("invalid_values" in entry) is counted
    if counted:
        assert entry["invalid_values"] == {"metric_cost_usd": 1}


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("metrics", ["absent", "empty"])
def test_valid_plus_invalid_request_cost_with_no_metric_session_marks_only_the_log_cost(
    installed: InstalledProduct, home: Path, tmp_path: Path, command, metrics
):
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.4}), _request("s1", {"stringValue": "abc"})],
        None,
    )
    if metrics == "empty":
        (stream / "metrics.jsonl").write_text("")

    text, document = _text_and_json(installed, home, command, stream)

    (line,) = _session_lines(text)
    assert line.startswith(
        "  session=s1  log_cost_usd=0.400000 (invalid: 1)  no metric export"
        + NO_EXPORT_CAUSES_PREFIX
    )
    entry = _entry(document, "s1")
    assert list(entry)[-2:] == ["possible_causes", "invalid_values"]
    assert entry["metric_cost_usd"] is None and entry["within_tolerance"] is None
    assert len(entry["possible_causes"]) == 3
    assert entry["invalid_values"] == {"log_cost_usd": 1}
    assert "metric_cost_usd (invalid" not in text


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    ("points", "line", "members"),
    [
        pytest.param(
            [_point("s1", asDouble=0.2)],
            "  session=s1  log_cost_usd=0.500000 (invalid: 1)  metric_cost_usd=0.200000"
            "  MISMATCH >1%" + MISMATCH_CAUSES_SUFFIX,
            {"log_cost_usd": 1},
            id="log-mark-only",
        ),
        pytest.param(
            [_point("s1", asDouble=0.2), _point("s1", asDouble="xyz")],
            "  session=s1  log_cost_usd=0.500000 (invalid: 1)  "
            "metric_cost_usd=0.200000 (invalid: 1)  MISMATCH >1%" + MISMATCH_CAUSES_SUFFIX,
            {"log_cost_usd": 1, "metric_cost_usd": 1},
            id="both-marks",
        ),
    ],
)
def test_log_cost_mark_on_a_mismatch_line(
    installed: InstalledProduct, home: Path, tmp_path: Path, command, points, line, members
):
    """CP-643: the request-cost mark sits beside `log_cost_usd` in the `MISMATCH >1%` state too,
    alone and beside the metric mark, and the verdict and its causes stay where they are."""
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.5}), _request("s1", {"stringValue": "abc"})],
        points,
    )

    text, document = _text_and_json(installed, home, command, stream)

    assert _session_lines(text) == [line]
    entry = _entry(document, "s1")
    assert (entry["log_cost_usd"], entry["metric_cost_usd"]) == (0.5, 0.2)
    assert entry["within_tolerance"] is False
    assert len(entry["possible_causes"]) == 3
    assert list(entry)[-2:] == ["possible_causes", "invalid_values"]
    assert entry["invalid_values"] == members
    assert list(entry["invalid_values"]) == list(members)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("invalid", ["abc", True], ids=["string", "bool"])
def test_invalid_cost_point_string_and_bool_leave_metric_figure_without_it(
    installed: InstalledProduct, home: Path, tmp_path: Path, command, invalid
):
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.4})],
        [_point("s1", asDouble=0.4), _point("s1", asDouble=invalid)],
    )

    text, document = _text_and_json(installed, home, command, stream)

    assert _session_lines(text) == [
        "  session=s1  log_cost_usd=0.400000  metric_cost_usd=0.400000 (invalid: 1)"
    ]
    assert _entry(document, "s1")["invalid_values"] == {"metric_cost_usd": 1}


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "invalid",
    [{"stringValue": "abc"}, {"boolValue": True}, {"boolValue": False}],
    ids=["string", "true", "false"],
)
def test_invalid_request_cost_string_true_and_false_leave_log_figure_without_it(
    installed: InstalledProduct, home: Path, tmp_path: Path, command, invalid
):
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.4}), _request("s1", invalid)],
        [_point("s1", asDouble=0.4)],
    )

    text, document = _text_and_json(installed, home, command, stream)

    assert _session_lines(text) == [
        "  session=s1  log_cost_usd=0.400000 (invalid: 1)  metric_cost_usd=0.400000"
    ]
    assert _entry(document, "s1")["invalid_values"] == {"log_cost_usd": 1}


@pytest.mark.parametrize("command", COMMANDS)
def test_absent_request_cost_and_valueless_point_add_no_count(
    installed: InstalledProduct, home: Path, tmp_path: Path, command
):
    """Continuity: this may already pass before the change."""
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.4}), _request("s1")],
        [_point("s1", asDouble=0.4), _point("s1")],
    )

    text, document = _text_and_json(installed, home, command, stream)

    assert _session_lines(text) == ["  session=s1  log_cost_usd=0.400000  metric_cost_usd=0.400000"]
    assert "(invalid:" not in "\n".join(_session_lines(text))
    assert list(_entry(document, "s1")) == [
        "session_id",
        "log_cost_usd",
        "metric_cost_usd",
        "within_tolerance",
        "possible_causes",
    ]


# ── Which requests the count covers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", [False, True])
def test_agents_where_filter_keeps_the_session_log_count(
    installed: InstalledProduct, home: Path, tmp_path: Path, prefix: bool
):
    requests = [
        _request("s1", {"doubleValue": 0.4}, query_source="sdk"),
        _request("s1", {"doubleValue": 0.1}, query_source="other"),
        _request("s1", {"stringValue": "abc"}, query_source="other"),
        _request(
            "s1", {"doubleValue": 0.0}, query_source="sdk", **{"team.run_id": {"stringValue": "r2"}}
        ),
    ]
    stream = _stream(tmp_path / "s", requests, [_point("s1", asDouble=0.5)])
    flags = ("--where", "query_source=sdk") + (("--prefix", "team") if prefix else ())

    text, document = _text_and_json(installed, home, "agents", stream, *flags)

    rows = [
        line for line in text.splitlines() if line.startswith("  session=") and " requests=" in line
    ]
    assert rows and all("(invalid:" not in row for row in rows)
    assert "(invalid:" not in next(l for l in text.splitlines() if l.startswith("Total cost_usd"))
    assert _session_lines(text) == [
        "  session=s1  log_cost_usd=0.500000 (invalid: 1)  metric_cost_usd=0.500000"
    ]
    assert _entry(document, "s1")["invalid_values"] == {"log_cost_usd": 1}


# ── What does not move ──────────────────────────────────────────────────────────────────────────

# The sha1 of each output of the commit before this change, from `change_spec.md` Verified Claims.
STREAM_PARITY_SHA1 = {
    ("check", ()): "6da99781a79965399edc4c9b7c9797aea6197120",
    ("check", ("--json",)): "ec0276e31c1bb4e78c923c6e7fae887f75756d4b",
    ("agents", ()): "d3ebebb90a37acd78f6b7b5c6ae46ca436ec5763",
    ("agents", ("--json",)): "47e644dfd6b3a71a857469a56b842b4b8b703bae",
}


@pytest.mark.parametrize(("command", "flags"), list(STREAM_PARITY_SHA1))
def test_stream_parity_outputs_are_byte_identical_with_no_count(
    installed: InstalledProduct, home: Path, command, flags
):
    """Continuity: the reference stream holds no invalid value, so nothing is counted."""
    result = run(installed, command, "--dir", str(DATA_SET), *flags, home=home)

    assert result.returncode == 0 and result.stderr == ""
    assert "invalid" not in result.stdout
    assert hashlib.sha1(result.stdout.encode()).hexdigest() == STREAM_PARITY_SHA1[(command, flags)]


@pytest.mark.parametrize("command", COMMANDS)
def test_unreadable_lines_statement_unchanged_and_not_counted(
    installed: InstalledProduct, home: Path, tmp_path: Path, command
):
    stream = _stream(
        tmp_path / "s",
        [_request("s1", {"doubleValue": 0.4}), _request("s1", {"stringValue": "abc"})],
        [_point("s1", asDouble=0.4), _point("s1", asDouble="abc")],
        extra_log_lines=("not json", "[1]"),
        extra_metric_lines=("{broken",),
    )

    text, document = _text_and_json(installed, home, command, stream)

    if command == "check":
        assert (
            "Unreadable lines in logs.jsonl, skipped and left out of the totals check: 2." in text
        )
        assert (
            "Unreadable lines in metrics.jsonl, skipped and left out of the totals check: 1."
            in text
        )
    else:
        assert "1 agent row(s), 2 unreadable line(s) skipped." in text
        assert (
            "Unreadable lines in metrics.jsonl, also skipped and left out of the totals check: 1."
            in text
        )
    assert _session_lines(text) == [
        "  session=s1  log_cost_usd=0.400000 (invalid: 1)  metric_cost_usd=0.400000 (invalid: 1)"
    ]
    assert _entry(document, "s1")["invalid_values"] == {"log_cost_usd": 1, "metric_cost_usd": 1}


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_or_unreadable_metrics_file_keeps_today_statement(
    installed: InstalledProduct, home: Path, tmp_path: Path, command
):
    requests = [_request("s1", {"doubleValue": 0.4}), _request("s1", {"stringValue": "abc"})]

    missing = _stream(tmp_path / "missing", requests, None)
    text, document = _text_and_json(installed, home, command, missing)
    assert (
        f"No metrics.jsonl was found in {missing}, "
        "so no session has a cost metric to check against." in text
    )
    (line,) = _session_lines(text)
    assert line.startswith("  session=s1  log_cost_usd=0.400000 (invalid: 1)  no metric export")
    assert _entry(document, "s1")["invalid_values"] == {"log_cost_usd": 1}

    if os.geteuid() == 0:
        pytest.skip("mode 000 does not stop a read at effective uid 0")
    unreadable = _stream(tmp_path / "unreadable", requests, [_point("s1", asDouble="abc")])
    metrics = unreadable / "metrics.jsonl"
    metrics.chmod(0)
    try:
        text, document = _text_and_json(installed, home, command, unreadable)
    finally:
        metrics.chmod(0o600)
    assert "metrics.jsonl" in text
    assert _session_lines(text) == []
    assert not document.get("metrics_check")
