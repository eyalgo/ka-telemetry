"""Tests for `stream_usage.py` — one row per agent, off the OTLP telemetry stream.

**The captured fixture below is a real payload, not invented.** `CAPTURED_LOGS_LINE` is cut from
`~/.kensaiapp/telemetry/logs.jsonl` on this machine, read 2026-09-11:

- Its `api_request` and `subagent_completed` records are line 398, counted from 1 (`received_at`
  `2026-09-10T16:03:12.507333+00:00`), session `41a62ae0-6d62-47b0-a0b5-3614fe3411d6` — one export
  that happened to carry both kinds for the same agent. The line's other records
  (`user_prompt`, `hook_execution_start`, `hook_execution_complete`, `assistant_response`) are
  dropped; neither carries spend or agent identity, and `assistant_response` and `user_prompt`
  carry redacted prompt/response text this station never keeps.
- Its one `hook_registered` record is spliced in from line 12 (`received_at`
  `2026-09-10T07:30:04.379949+00:00`), session `4d893290-19b6-4f47-b6d0-67bd73f315d3` — chosen
  because line 398 carries no `hook_registered` of its own, and this one carries `kensai.arm` /
  `kensai.run_id` at the record level, real values for the same attributes Task 2 reads. The plan
  asks for one payload line; none holds all three kinds (0 of 2,020 lines, counted 2026-09-11), so
  two real lines are the nearest a real capture can come.
- `user.*` and `organization.id` are stripped off all three records, per the guardrail. Every
  other attribute, and its exact OTLP envelope (`stringValue` / `intValue` / `doubleValue` /
  `boolValue`), is what the receiver actually stored — including `safe_mode`, which arrived as
  the *string* `"false"`, not a bool.

`_attr(key, value)` below is a compaction, not an invention: it picks the OTLP envelope key by
the Python type of `value`, and every value it is called with is the real one the payload
carried — the helper only saves re-typing `{"key": ..., "value": {"stringValue": ...}}` by hand.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import math
import os
import re
from pathlib import Path

import pytest
from conftest import sealed_paths

from ka_telemetry import stream_usage


# ── The real capture (see module docstring) ────────────────────────────────


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        kind = "boolValue"
    elif isinstance(value, int):
        kind = "intValue"
    elif isinstance(value, float):
        kind = "doubleValue"
    else:
        kind = "stringValue"
    return {"key": key, "value": {kind: value}}


_API_REQUEST_RECORD = {
    "timeUnixNano": "1789056188302000000",
    "observedTimeUnixNano": "1789056188302000000",
    "body": {"stringValue": "claude_code.api_request"},
    "attributes": [
        _attr("session.id", "41a62ae0-6d62-47b0-a0b5-3614fe3411d6"),
        _attr("terminal.type", "Apple_Terminal"),
        _attr("event.name", "api_request"),
        _attr("event.timestamp", "2026-09-10T16:03:08.302Z"),
        _attr("event.sequence", 547),
        _attr("prompt.id", "012a824f-5de0-4363-a5a6-07e69167cc54"),
        _attr("model", "claude-opus-5"),
        _attr("input_tokens", 32),
        _attr("output_tokens", 2315),
        _attr("cache_read_tokens", 295809),
        _attr("cache_creation_tokens", 1684),
        _attr("cost_usd", 0.2164645),
        _attr("cost_usd_micros", 216465),
        _attr("duration_ms", 25645),
        _attr("request_id", "req_011Cev3Jqf1hwhuxPxZ7DJUU"),
        _attr("client_request_id", "1b531883-efae-4e87-ba97-6b30b19fdc41"),
        _attr("speed", "normal"),
        _attr("query_source", "agent:custom"),
        _attr("effort", "high"),
        _attr("agent.name", "custom"),
    ],
    "droppedAttributesCount": 0,
}

_SUBAGENT_COMPLETED_RECORD = {
    "timeUnixNano": "1789056188304000000",
    "observedTimeUnixNano": "1789056188304000000",
    "body": {"stringValue": "claude_code.subagent_completed"},
    "attributes": [
        _attr("session.id", "41a62ae0-6d62-47b0-a0b5-3614fe3411d6"),
        _attr("terminal.type", "Apple_Terminal"),
        _attr("event.name", "subagent_completed"),
        _attr("event.timestamp", "2026-09-10T16:03:08.304Z"),
        _attr("event.sequence", 549),
        _attr("prompt.id", "012a824f-5de0-4363-a5a6-07e69167cc54"),
        _attr("agent_type", "custom"),
        _attr("agent.source", "projectSettings"),
        _attr("is_built_in", False),
        _attr("is_async", True),
        _attr("total_tokens", 299840),
        _attr("total_tool_uses", 77),
        _attr("duration_ms", 278394),
        _attr("model", "claude-opus-5[1m]"),
        _attr("final_model", "claude-opus-5[1m]"),
        _attr("model_swapped", False),
    ],
    "droppedAttributesCount": 0,
}

_HOOK_REGISTERED_RECORD = {
    "timeUnixNano": "1789025401255000000",
    "observedTimeUnixNano": "1789025401255000000",
    "body": {"stringValue": "claude_code.hook_registered"},
    "attributes": [
        _attr("kensai.arm", "fg-control-093000"),
        _attr("kensai.run_id", "ab3b5267-3f21-404b-9015-3fa7ddcf74f1"),
        _attr("session.id", "4d893290-19b6-4f47-b6d0-67bd73f315d3"),
        _attr("terminal.type", "Apple_Terminal"),
        _attr("event.name", "hook_registered"),
        _attr("event.timestamp", "2026-09-10T07:30:01.255Z"),
        _attr("event.sequence", 0),
        _attr("hook_event", "PreToolUse"),
        _attr("hook_type", "command"),
        _attr("hook_source", "userSettings"),
        _attr("safe_mode", "false"),  # arrived as a string, not a bool — kept faithfully
    ],
    "droppedAttributesCount": 0,
}

CAPTURED_LOGS_LINE = {
    "signal": "logs",
    "received_at": "2026-09-10T16:03:12.507333+00:00",
    "body": {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        _attr("host.arch", "arm64"),
                        _attr("os.type", "darwin"),
                        _attr("os.version", "25.6.0"),
                        _attr("service.name", "claude-code"),
                        _attr("service.version", "2.1.267"),
                    ],
                    "droppedAttributesCount": 0,
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "com.anthropic.claude_code.events",
                            "version": "2.1.267",
                        },
                        "logRecords": [
                            _API_REQUEST_RECORD,
                            _SUBAGENT_COMPLETED_RECORD,
                            _HOOK_REGISTERED_RECORD,
                        ],
                    }
                ],
            }
        ]
    },
}


# ── Task 1 — a value is read whatever envelope it arrives in ───────────────


class TestDecodeValue:
    def test_reads_a_string_value(self):
        assert stream_usage.decode_value({"stringValue": "agent:custom"}) == "agent:custom"

    def test_reads_an_int_value_arriving_as_a_json_number(self):
        assert stream_usage.decode_value({"intValue": 43}) == 43

    def test_reads_an_int_value_arriving_as_a_json_string_the_same_as_a_number(self):
        assert stream_usage.decode_value({"intValue": "43"}) == stream_usage.decode_value(
            {"intValue": 43}
        )

    def test_reads_a_double_value(self):
        assert stream_usage.decode_value({"doubleValue": 0.0503237}) == 0.0503237

    def test_reads_a_bool_value(self):
        assert stream_usage.decode_value({"boolValue": True}) is True

    def test_an_envelope_with_none_of_the_four_keys_decodes_to_none(self):
        assert stream_usage.decode_value({}) is None

    def test_a_non_dict_value_decodes_to_none_rather_than_raising(self):
        assert stream_usage.decode_value(None) is None


class TestAsNumber:
    def test_an_int_passes_through(self):
        assert stream_usage.as_number(43) == 43

    def test_a_float_passes_through(self):
        assert stream_usage.as_number(0.5) == 0.5

    def test_a_numeric_string_value_reads_as_that_number_when_summed(self):
        assert stream_usage.as_number("1") == 1
        assert stream_usage.as_number("2.5") == 2.5

    def test_a_non_numeric_string_reads_as_zero_rather_than_raising(self):
        assert stream_usage.as_number("normal") == 0

    def test_none_reads_as_zero(self):
        assert stream_usage.as_number(None) == 0


class TestReadLogLines:
    def test_reads_the_real_capture_into_three_records(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        assert len(result.records) == 3
        assert result.skipped == 0

    def test_the_api_request_record_decodes_its_typed_fields_correctly(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        api_request = next(
            r for r in result.records if r.record_attrs.get("event.name") == "api_request"
        )
        assert api_request.record_attrs["cost_usd"] == 0.2164645
        assert api_request.record_attrs["input_tokens"] == 32
        assert api_request.record_attrs["session.id"] == "41a62ae0-6d62-47b0-a0b5-3614fe3411d6"

    def test_the_resource_attributes_are_carried_on_every_record_from_that_body(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        for record in result.records:
            assert record.resource_attrs["service.name"] == "claude-code"

    def test_a_line_that_is_not_json_is_skipped_and_counted(self):
        result = stream_usage.read_log_lines(["not json at all {"])
        assert result.records == ()
        assert result.skipped == 1

    def test_a_line_whose_outer_object_has_no_body_is_skipped_and_counted(self):
        result = stream_usage.read_log_lines([json.dumps({"signal": "logs"})])
        assert result.records == ()
        assert result.skipped == 1

    def test_a_line_whose_body_is_an_empty_object_is_skipped_and_counted(self):
        result = stream_usage.read_log_lines([json.dumps({"signal": "logs", "body": {}})])
        assert result.records == ()
        assert result.skipped == 1

    def test_a_blank_line_is_neither_a_record_nor_a_skip(self):
        result = stream_usage.read_log_lines(["", "   "])
        assert result.records == ()
        assert result.skipped == 0

    def test_one_bad_line_never_stops_the_read_of_the_rest(self):
        result = stream_usage.read_log_lines(["garbled", json.dumps(CAPTURED_LOGS_LINE)])
        assert len(result.records) == 3
        assert result.skipped == 1

    def test_an_empty_list_of_lines_reads_as_no_records_and_no_skips(self):
        result = stream_usage.read_log_lines([])
        assert result.records == ()
        assert result.skipped == 0


# ── Task 2 — one row per agent ──────────────────────────────────────────────


def _record(record_attrs: dict, resource_attrs: dict | None = None) -> stream_usage.LogRecord:
    return stream_usage.LogRecord(record_attrs=record_attrs, resource_attrs=resource_attrs or {})


def _api_request(**overrides) -> stream_usage.LogRecord:
    attrs = {
        "event.name": "api_request",
        "session.id": "sid-1",
        "query_source": "agent:custom",
        "agent.name": "custom",
        "model": "claude-opus-5",
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.1,
        "duration_ms": 100,
    }
    attrs.update(overrides)
    return _record(attrs)


class TestAgentRows:
    def test_only_api_request_records_become_rows(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        rows = stream_usage.agent_rows(result.records)
        assert len(rows) == 1
        assert rows[0].session_id == "41a62ae0-6d62-47b0-a0b5-3614fe3411d6"

    def test_the_fixtures_hook_registered_record_changes_no_figure(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        rows = stream_usage.agent_rows(result.records)
        assert rows[0].cost_usd == pytest.approx(0.2164645)
        assert rows[0].request_count == 1

    def test_records_sharing_the_key_fold_into_one_row_summing_and_listing_models(self):
        records = (
            _api_request(model="claude-opus-5", input_tokens=10, cost_usd=0.1, duration_ms=100),
            _api_request(model="claude-haiku-4-5", input_tokens=5, cost_usd=0.05, duration_ms=50),
        )
        rows = stream_usage.agent_rows(records)
        assert len(rows) == 1
        row = rows[0]
        assert row.request_count == 2
        assert row.input_tokens == 15
        assert row.cost_usd == pytest.approx(0.15)
        assert row.duration_ms == 150
        assert row.models == ("claude-haiku-4-5", "claude-opus-5")

    def test_kensai_attrs_are_read_off_the_record_first_and_the_resource_second(self):
        record = _record(
            {
                "event.name": "api_request",
                "session.id": "sid-1",
                "query_source": "sdk",
                "kensai.arm": "record-arm",
                "cost_usd": 0.1,
            },
            resource_attrs={"kensai.arm": "resource-arm", "kensai.run_id": "rid-1"},
        )
        rows = stream_usage.agent_rows((record,), prefix="kensai")
        assert rows[0].labels == {"kensai.arm": "record-arm", "kensai.run_id": "rid-1"}

    def test_a_row_whose_records_disagree_on_a_kensai_value_is_split_never_merged(self):
        records = (
            _api_request(**{"kensai.arm": "arm-a"}),
            _api_request(**{"kensai.arm": "arm-b"}),
        )
        # both share session.id/query_source/agent.name via _api_request's defaults
        rows = stream_usage.agent_rows(records, prefix="kensai")
        assert len(rows) == 2
        arms = {row.labels.get("kensai.arm") for row in rows}
        assert arms == {"arm-a", "arm-b"}
        for row in rows:
            assert row.request_count == 1

    def test_where_filters_on_any_attribute_including_session_id(self):
        records = (
            _api_request(**{"session.id": "sid-1"}),
            _api_request(**{"session.id": "sid-2"}),
        )
        rows = stream_usage.agent_rows(records, where=(("session.id", "sid-1"),))
        assert len(rows) == 1
        assert rows[0].session_id == "sid-1"

    def test_where_filters_on_a_kensai_attribute(self):
        records = (
            _record(
                {"event.name": "api_request", "session.id": "sid-1", "kensai.arm": "keep"},
            ),
            _record(
                {"event.name": "api_request", "session.id": "sid-2", "kensai.arm": "drop"},
            ),
        )
        rows = stream_usage.agent_rows(records, where=(("kensai.arm", "keep"),))
        assert len(rows) == 1
        assert rows[0].session_id == "sid-1"

    def test_an_unseen_query_source_value_gets_a_row_like_any_other(self):
        records = (_api_request(query_source="a-brand-new-source"),)
        rows = stream_usage.agent_rows(records)
        assert len(rows) == 1
        assert rows[0].query_source == "a-brand-new-source"


# ── Task 3 — the metrics check ──────────────────────────────────────────────
#
# `_cost_datapoint` mirrors the real shape captured from `~/.kensaiapp/telemetry/metrics.jsonl`
# on 2026-09-11 — `claude_code.cost.usage`'s `sum.dataPoints[].asDouble`, `aggregationTemporality:
# 1` (delta) — e.g. session `cf05030e-112f-4665-89cb-3e15ec7dcf67` reported `asDouble: 0.0503237`
# for `query_source: "main"`. The values below are synthesized in that same real shape to drive
# the specific arithmetic this task's behaviours name (two exports summing, a session with no
# export, a >1% mismatch) — Task 1's "cut from one payload line" rule binds `logs.jsonl` only.


def _cost_metric(data_points: list[dict]) -> dict:
    return {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.cost.usage",
                                "unit": "USD",
                                "sum": {"aggregationTemporality": 1, "dataPoints": data_points},
                            }
                        ]
                    }
                ]
            }
        ]
    }


def _cost_datapoint(session_id: str, value: float) -> dict:
    return {
        "attributes": [_attr("session.id", session_id), _attr("query_source", "main")],
        "startTimeUnixNano": "1788989273064000000",
        "timeUnixNano": "1788989273225000000",
        "asDouble": value,
    }


def _metrics_line(body: dict) -> str:
    return json.dumps(
        {"signal": "metrics", "received_at": "2026-09-11T00:00:00+00:00", "body": body}
    )


class TestReadMetricLines:
    def test_reads_cost_usage_datapoints_carrying_session_id(self):
        line = _metrics_line(_cost_metric([_cost_datapoint("sid-1", 0.0503237)]))
        points = stream_usage.read_metric_lines([line])
        assert len(points) == 1
        assert points[0].attrs["session.id"] == "sid-1"
        assert points[0].value == pytest.approx(0.0503237)

    def test_datapoints_of_any_other_metric_name_are_ignored(self):
        other_metric = {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "unit": "tokens",
                                    "sum": {
                                        "aggregationTemporality": 1,
                                        "dataPoints": [_cost_datapoint("sid-1", 10)],
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        points = stream_usage.read_metric_lines([_metrics_line(other_metric)])
        assert points == ()

    def test_a_missing_metrics_file_reads_as_no_points(self):
        assert stream_usage.read_metric_lines(None) == ()


def _skipped_metric_lines(tmp_path: Path, metric_lines: list[str]) -> int:
    """The unreadable-metrics-line count `read_report` carries for `metric_lines` on disk."""
    request = _logs_line([{"event.name": "api_request", "session.id": "s"}])
    _write_stream(tmp_path, [request], metric_lines)
    report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
    assert report is not None
    return report.skipped_metric_lines


_NO_COST_EXPORT = _metrics_line({"resourceMetrics": [{"scopeMetrics": [{"metrics": []}]}]})


class TestMetricsLineCoverage:
    """CP-295, CP-296, CP-298: a non-blank metrics line that is not JSON, not an object, or an
    object whose `body` is missing, not an object, or `{}` is counted — a readable export with no
    cost point is not."""

    def test_blank_and_whitespace_metrics_lines_are_not_counted(self, tmp_path):
        assert _skipped_metric_lines(tmp_path, ["", "   ", "\t"]) == 0

    def test_invalid_json_metrics_line_counts_one(self, tmp_path):
        assert _skipped_metric_lines(tmp_path, ["not json"]) == 1

    @pytest.mark.parametrize("line", ["[1, 2]", "7", "null", '""', "true"])
    def test_scalar_or_array_metrics_line_counts_one(self, tmp_path, line):
        assert _skipped_metric_lines(tmp_path, [line]) == 1

    @pytest.mark.parametrize(
        "line",
        ['{"signal": "metrics"}', '{"body": null}', '{"body": []}', '{"body": 3}', '{"body": {}}'],
    )
    def test_metrics_object_without_usable_body_counts_one(self, tmp_path, line):
        assert _skipped_metric_lines(tmp_path, [line]) == 1

    def test_readable_export_with_no_cost_point_is_not_counted(self, tmp_path):
        assert _skipped_metric_lines(tmp_path, [_NO_COST_EXPORT]) == 0

    def test_example_file_counts_four(self, tmp_path):
        lines = ["", "not json", "[1, 2]", '{"body": {}}', '{"body": []}', _NO_COST_EXPORT]
        assert _skipped_metric_lines(tmp_path, lines) == 4

    def test_count_covers_the_whole_file_whatever_the_selection(self, tmp_path):
        _write_stream(
            tmp_path,
            [_logs_line([{"event.name": "api_request", "session.id": "s"}])],
            ["not json"],
        )
        reader = stream_usage.build_reader(tmp_path)
        everything = stream_usage.read_report(reader)
        nothing = stream_usage.read_report(reader, where=(("session.id", "other"),))
        assert everything is not None and nothing is not None
        assert nothing.rows == ()
        assert everything.skipped_metric_lines == nothing.skipped_metric_lines == 1

    def test_public_read_metric_lines_returns_the_same_points_in_order(self):
        lines = [
            _metrics_line(_cost_metric([_cost_datapoint("a", 0.1), _cost_datapoint("b", 0.2)])),
            "not json",
            _metrics_line(_cost_metric([_cost_datapoint("c", 0.3)])),
        ]
        points = stream_usage.read_metric_lines(lines)
        assert [p.attrs["session.id"] for p in points] == ["a", "b", "c"]
        assert stream_usage.read_metric_lines(None) == ()

    def test_stream_report_constructs_with_the_old_five_arguments(self):
        report = stream_usage.StreamReport((), (), None, 0, None)
        assert report.skipped_metric_lines == 0
        names = [f.name for f in dataclasses.fields(stream_usage.StreamReport)]
        # CP-360 appends `metrics_state` and `metrics_reason`; the seven old fields keep their
        # order, and `TestLibraryCarriesMetricsState` pins the two that follow.
        assert names[:7] == [
            "rows", "subagent_rows", "metrics_check", "skipped_lines", "compare",
            "skipped_metric_lines", "subagents_filtered_out",
        ]

    def test_read_report_carries_zero_without_metrics(self, tmp_path):
        _write_stream(tmp_path, [_logs_line([{"event.name": "api_request", "session.id": "s"}])])
        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
        assert report is not None and report.skipped_metric_lines == 0

    def test_read_report_carries_zero_for_an_unreadable_metrics_file(self, tmp_path):
        _write_stream(tmp_path, [_logs_line([{"event.name": "api_request", "session.id": "s"}])])
        (tmp_path / "metrics.jsonl").mkdir()
        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
        assert report is not None and report.skipped_metric_lines == 0


class TestMetricsCheck:
    def _row(self, session_id: str, cost_usd: float) -> stream_usage.AgentRow:
        return stream_usage.AgentRow(
            session_id=session_id,
            workflow_run_id=None,
            query_source="main",
            agent_name=None,
            labels={},
            models=("claude-opus-5",),
            request_count=1,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost_usd,
            duration_ms=0,
        )

    def test_two_delta_exports_for_one_session_are_summed_never_reading_only_the_last(self):
        rows = (self._row("sid-1", 0.07),)
        points = stream_usage.read_metric_lines(
            [
                _metrics_line(_cost_metric([_cost_datapoint("sid-1", 0.05)])),
                _metrics_line(_cost_metric([_cost_datapoint("sid-1", 0.02)])),
            ]
        )
        checks = stream_usage.metrics_check(rows, points)
        assert len(checks) == 1
        assert checks[0].metric_cost_usd == pytest.approx(0.07)
        assert checks[0].within_tolerance is True

    def test_a_session_with_requests_and_no_metric_export_is_named_as_such(self):
        rows = (self._row("sid-no-export", 0.05),)
        checks = stream_usage.metrics_check(rows, ())
        assert len(checks) == 1
        assert checks[0].session_id == "sid-no-export"
        assert checks[0].metric_cost_usd is None
        assert checks[0].within_tolerance is None

    def test_a_greater_than_one_percent_difference_is_flagged(self):
        rows = (self._row("sid-1", 1.00),)
        points = stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_cost_datapoint("sid-1", 1.02)]))]
        )
        checks = stream_usage.metrics_check(rows, points)
        assert checks[0].within_tolerance is False

    def test_a_one_percent_or_smaller_difference_is_not_flagged(self):
        rows = (self._row("sid-1", 1.00),)
        points = stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_cost_datapoint("sid-1", 1.005)]))]
        )
        checks = stream_usage.metrics_check(rows, points)
        assert checks[0].within_tolerance is True


# ── Task 4 — the Agent-tool agent count ─────────────────────────────────────


def _subagent_completed(**overrides) -> stream_usage.LogRecord:
    attrs = {
        "event.name": "subagent_completed",
        "session.id": "sid-1",
        "agent_type": "custom",
        "duration_ms": 100,
        "total_tool_uses": 5,
        "total_tokens": 999,
    }
    attrs.update(overrides)
    return _record(attrs)


class TestSubagentRows:
    def test_the_real_captures_subagent_completed_record_becomes_one_row(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        rows = stream_usage.subagent_rows(result.records)
        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == "41a62ae0-6d62-47b0-a0b5-3614fe3411d6"
        assert row.agent_type == "custom"
        assert row.count == 1
        assert row.duration_ms == 278394
        assert row.total_tool_uses == 77

    def test_api_request_and_hook_registered_records_produce_no_subagent_row(self):
        result = stream_usage.read_log_lines([json.dumps(CAPTURED_LOGS_LINE)])
        rows = stream_usage.subagent_rows(result.records)
        assert all(row.session_id == "41a62ae0-6d62-47b0-a0b5-3614fe3411d6" for row in rows)

    def test_records_sharing_session_id_and_agent_type_fold_into_one_row(self):
        records = (
            _subagent_completed(duration_ms=100, total_tool_uses=5),
            _subagent_completed(duration_ms=50, total_tool_uses=2),
        )
        rows = stream_usage.subagent_rows(records)
        assert len(rows) == 1
        assert rows[0].count == 2
        assert rows[0].duration_ms == 150
        assert rows[0].total_tool_uses == 7

    def test_different_agent_types_in_one_session_stay_separate_rows(self):
        records = (
            _subagent_completed(agent_type="custom"),
            _subagent_completed(agent_type="general-purpose"),
        )
        rows = stream_usage.subagent_rows(records)
        assert len(rows) == 2

    def test_total_tokens_is_not_a_field_on_the_row_the_lumped_shape_is_forbidden(self):
        field_names = {f.name for f in dataclasses.fields(stream_usage.SubagentRow)}
        assert "total_tokens" not in field_names

    def test_the_coverage_note_states_a_workflow_step_emits_none(self):
        assert "workflow" in stream_usage.SUBAGENT_COVERAGE_NOTE.lower()
        assert "agent-tool" in stream_usage.SUBAGENT_COVERAGE_NOTE.lower()


# ── Task 5 — `--compare ATTR A B` ────────────────────────────────────────────


class TestCompare:
    def test_rows_are_grouped_by_the_attribute_and_only_a_and_b_are_kept(self):
        records = (
            _api_request(**{"kensai.arm": "launch-skill-evals"}, cost_usd=0.10),
            _api_request(**{"kensai.arm": "product-sdd"}, cost_usd=0.20),
            _api_request(**{"kensai.arm": "unrelated-arm"}, cost_usd=99.0),
        )
        result = stream_usage.compare(records, "kensai.arm", "launch-skill-evals", "product-sdd")
        assert result.a.value == "launch-skill-evals"
        assert result.b.value == "product-sdd"
        assert result.a.totals.cost_usd == pytest.approx(0.10)
        assert result.b.totals.cost_usd == pytest.approx(0.20)

    def test_a_run_is_one_kensai_run_id_when_a_record_carries_one(self):
        records = (
            _api_request(**{"kensai.arm": "A", "kensai.run_id": "run-1"}, cost_usd=0.10),
            _api_request(**{"kensai.arm": "A", "kensai.run_id": "run-1"}, cost_usd=0.05),
            _api_request(**{"kensai.arm": "A", "kensai.run_id": "run-2"}, cost_usd=0.10),
        )
        result = stream_usage.compare(records, "kensai.arm", "A", "B", prefix="kensai")
        assert result.a.run_count == 2
        assert result.a.totals.cost_usd == pytest.approx(0.25)
        assert result.a.mean_cost_per_run == pytest.approx(0.125)

    def test_a_run_falls_back_to_session_id_when_a_record_carries_no_run_id(self):
        records = (
            _api_request(**{"kensai.arm": "A", "session.id": "sid-1"}, cost_usd=0.10),
            _api_request(**{"kensai.arm": "A", "session.id": "sid-2"}, cost_usd=0.10),
        )
        result = stream_usage.compare(records, "kensai.arm", "A", "B")
        assert result.a.run_count == 2

    def test_it_reports_b_minus_a_and_b_over_a_for_the_per_run_mean_cost(self):
        records = (
            _api_request(**{"kensai.arm": "A", "session.id": "sid-1"}, cost_usd=0.10),
            _api_request(**{"kensai.arm": "B", "session.id": "sid-2"}, cost_usd=0.30),
        )
        result = stream_usage.compare(records, "kensai.arm", "A", "B")
        assert result.diff == pytest.approx(0.20)
        assert result.ratio == pytest.approx(3.0)

    def test_a_group_with_no_runs_is_named_empty_and_prints_no_ratio(self):
        records = (_api_request(**{"kensai.arm": "A", "session.id": "sid-1"}, cost_usd=0.10),)
        result = stream_usage.compare(records, "kensai.arm", "A", "no-such-value")
        assert result.a is not None
        assert result.b is None
        assert result.diff is None
        assert result.ratio is None

    def test_where_applies_before_the_grouping(self):
        records = (
            _api_request(
                **{"kensai.arm": "A", "session.id": "sid-1", "speed": "normal"}, cost_usd=0.10
            ),
            _api_request(
                **{"kensai.arm": "A", "session.id": "sid-2", "speed": "fast"}, cost_usd=0.90
            ),
        )
        result = stream_usage.compare(
            records, "kensai.arm", "A", "B", where=(("speed", "normal"),)
        )
        assert result.a.totals.cost_usd == pytest.approx(0.10)


# ── Task 6 — the command ─────────────────────────────────────────────────────


class TestFiles:
    def test_reads_a_real_files_lines(self, tmp_path):
        path = tmp_path / "logs.jsonl"
        path.write_text("a\nb\n")
        files = stream_usage.Files()
        assert files.read_lines(path) == ["a", "b"]

    def test_returns_none_for_a_missing_file(self, tmp_path):
        files = stream_usage.Files()
        assert files.read_lines(tmp_path / "does-not-exist") is None


class TestFilesIsMissing:
    """CP-291: absence is told from every other reason a path cannot be read — only established
    absence is `True`."""

    def test_true_for_an_absent_path_and_for_a_path_beneath_a_file(self, tmp_path):
        files = stream_usage.Files()
        (tmp_path / "plain").write_text("")
        assert files.is_missing(tmp_path / "absent") is True
        assert files.is_missing(tmp_path / "plain" / "child") is True

    def test_false_for_an_existing_file_or_directory(self, tmp_path):
        files = stream_usage.Files()
        (tmp_path / "plain").write_text("")
        assert files.is_missing(tmp_path / "plain") is False
        assert files.is_missing(tmp_path) is False

    def test_false_when_inspection_fails_for_another_reason(self):
        class Unreadable:
            def stat(self):
                raise PermissionError("denied")

        assert stream_usage.Files().is_missing(Unreadable()) is False

    def test_read_lines_still_answers_none_for_any_unreadable_path(self, tmp_path):
        (tmp_path / "logs.jsonl").mkdir()
        files = stream_usage.Files()
        assert files.read_lines(tmp_path / "logs.jsonl") is None
        assert files.read_lines(tmp_path / "absent") is None


_AS_ROOT = os.geteuid() == 0
_needs_a_non_root_user = pytest.mark.skipif(
    _AS_ROOT, reason="mode 000 does not stop a read at effective uid 0"
)


class TestReadOutcome:
    """The filesystem port's one answer for a read: the lines it read, or the reason the read failed
    and what looking at the path established (CP-325, CP-327, CP-329 sit on it)."""

    def test_read_outcome_of_readable_logs_carries_the_lines(self, tmp_path):
        path = tmp_path / "logs.jsonl"
        path.write_text("a\nb\n")
        outcome = stream_usage.Files().read_outcome(path)
        assert outcome.state is stream_usage.ReadState.READ
        assert outcome.lines == ("a", "b")
        assert outcome.reason is None

    def test_read_outcome_of_empty_logs_is_read_not_nothing_received(self, tmp_path):
        path = tmp_path / "logs.jsonl"
        path.write_text("")
        outcome = stream_usage.Files().read_outcome(path)
        assert outcome.state is stream_usage.ReadState.READ
        assert outcome.lines == ()

    def test_read_outcome_absent_logs_is_nothing_received(self, tmp_path):
        files = stream_usage.Files()
        (tmp_path / "plain").write_text("")
        (tmp_path / "dangling").symlink_to(tmp_path / "no-such-target")
        (tmp_path / "empty-dir").mkdir()
        absent = [
            tmp_path / "absent-dir" / "logs.jsonl",
            tmp_path / "empty-dir" / "logs.jsonl",
            tmp_path / "plain" / "logs.jsonl",
            tmp_path / "dangling",
        ]
        for path in absent:
            outcome = files.read_outcome(path)
            assert outcome.state is stream_usage.ReadState.NOTHING_RECEIVED, path
            assert outcome.lines == ()
            assert outcome.reason is not None, path

    @_needs_a_non_root_user
    def test_read_outcome_mode_000_logs_is_unopenable_with_permission_denied_reason(
        self, tmp_path
    ):
        path = tmp_path / "logs.jsonl"
        path.write_text("a\n")
        path.chmod(0)
        try:
            outcome = stream_usage.Files().read_outcome(path)
        finally:
            path.chmod(0o600)
        assert outcome.state is stream_usage.ReadState.UNOPENABLE
        assert outcome.reason == "Permission denied"
        assert outcome.lines == ()

    def test_read_outcome_directory_named_logs_is_unopenable_with_is_a_directory_reason(
        self, tmp_path
    ):
        (tmp_path / "logs.jsonl").mkdir()
        outcome = stream_usage.Files().read_outcome(tmp_path / "logs.jsonl")
        assert outcome.state is stream_usage.ReadState.UNOPENABLE
        assert outcome.reason == "Is a directory"

    def test_read_outcome_reason_comes_from_the_one_read_attempt(self):
        class RefusedOnce:
            """A path whose read fails with a chosen error and whose inspection finds it there."""

            def __init__(self) -> None:
                self.reads = 0

            def read_text(self, **kwargs):
                self.reads += 1
                raise OSError(errno.EIO, "Input/output error")

            def stat(self):
                return None

        path = RefusedOnce()
        outcome = stream_usage.Files().read_outcome(path)
        assert outcome.state is stream_usage.ReadState.UNOPENABLE
        assert outcome.reason == "Input/output error"
        assert path.reads == 1

    @_needs_a_non_root_user
    def test_read_outcome_uninspectable_directory_is_neither_state(self, tmp_path):
        directory = tmp_path / "sealed"
        directory.mkdir()
        (directory / "logs.jsonl").write_text("a\n")
        directory.chmod(0)
        try:
            outcome = stream_usage.Files().read_outcome(directory / "logs.jsonl")
        finally:
            directory.chmod(0o700)
        assert outcome.state is stream_usage.ReadState.UNINSPECTABLE
        assert outcome.state not in (
            stream_usage.ReadState.NOTHING_RECEIVED,
            stream_usage.ReadState.UNOPENABLE,
        )

    def test_read_outcome_is_immutable(self, tmp_path):
        outcome = stream_usage.Files().read_outcome(tmp_path / "absent")
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.state = stream_usage.ReadState.READ

    def test_read_lines_still_projects_the_outcome_to_a_list_or_none(self, tmp_path):
        files = stream_usage.Files()
        (tmp_path / "logs.jsonl").write_text("a\nb\n")
        assert files.read_lines(tmp_path / "logs.jsonl") == ["a", "b"]
        assert files.read_lines(tmp_path / "absent") is None

    def test_read_report_still_returns_none_for_failed_logs(self, tmp_path):
        nothing, unopenable = tmp_path / "nothing", tmp_path / "unopenable"
        nothing.mkdir()
        (unopenable / "logs.jsonl").mkdir(parents=True)
        for directory in (nothing, unopenable):
            assert stream_usage.read_report(stream_usage.build_reader(directory)) is None

    @_needs_a_non_root_user
    def test_read_report_returns_none_for_an_uninspectable_directory(self, tmp_path):
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        (sealed / "logs.jsonl").write_text("")
        sealed.chmod(0)
        try:
            assert stream_usage.read_report(stream_usage.build_reader(sealed)) is None
        finally:
            sealed.chmod(0o700)

    def test_report_result_carries_the_state_and_reason_of_the_one_logs_read(self, tmp_path):
        reader = stream_usage.build_reader(tmp_path)
        assert (
            stream_usage.read_report_result(reader).state is stream_usage.ReadState.NOTHING_RECEIVED
        )

        (tmp_path / "logs.jsonl").mkdir()
        refused = stream_usage.read_report_result(reader)
        assert refused.state is stream_usage.ReadState.UNOPENABLE
        assert (refused.report, refused.reason) == (None, "Is a directory")

    def test_report_result_of_an_empty_logs_file_is_a_report(self, tmp_path):
        (tmp_path / "logs.jsonl").write_text("")
        result = stream_usage.read_report_result(stream_usage.build_reader(tmp_path))
        assert result.state is stream_usage.ReadState.READ
        assert result.report is not None and result.report.rows == ()
        assert result.reason is None

    def test_read_report_figures_for_readable_logs_are_unchanged(self, tmp_path):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [{"event.name": "api_request", "session.id": "s1", "cost_usd": 0.25}]
                    + [{"event.name": "subagent_completed", "session.id": "s1"}]
                ),
                "not json",
            ],
            [_metrics_line(_cost_metric([_cost_datapoint("s1", 0.25)])), "bad"],
        )
        reader = stream_usage.build_reader(tmp_path)
        report = stream_usage.read_report(reader)
        assert [row.cost_usd for row in report.rows] == [0.25]
        assert report.skipped_lines == 1
        assert report.skipped_metric_lines == 1
        assert [check.session_id for check in report.metrics_check] == ["s1"]
        assert len(report.subagent_rows) == 1
        assert stream_usage.read_report_result(reader).report == report


class TestCompositionRoot:
    def test_no_module_level_name_holds_the_expanded_default_directory(self):
        default_dir = Path.home() / ".ka-telemetry"
        holders = [
            name
            for name, value in vars(stream_usage).items()
            if value == default_dir or value == str(default_dir)
        ]
        assert holders == []

    def test_build_reader_resolves_the_default_directory(self):
        reader = stream_usage.build_reader()
        assert reader.directory == Path.home() / ".ka-telemetry"
        assert isinstance(reader.files, stream_usage.Files)

    def test_build_reader_honors_an_explicit_directory(self, tmp_path):
        reader = stream_usage.build_reader(tmp_path)
        assert reader.directory == tmp_path

    def test_only_build_reader_constructs_a_files(self):
        import ast as _ast

        source = Path(stream_usage.__file__).read_text()
        tree = _ast.parse(source)
        constructing_functions = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                for call in _ast.walk(node):
                    if isinstance(call, _ast.Call) and isinstance(call.func, _ast.Name):
                        if call.func.id == "Files":
                            constructing_functions.add(node.name)
        assert constructing_functions == {"build_reader"}

    def test_main_obtains_ports_from_build_reader_and_constructs_none_itself(self):
        import inspect as _inspect

        source = _inspect.getsource(stream_usage.main)
        assert "build_reader(" in source
        assert "Files(" not in source


def _write_stream(
    tmp_path: Path, logs_lines: list[str] | None, metrics_lines: list[str] | None = None
) -> Path:
    if logs_lines is not None:
        (tmp_path / "logs.jsonl").write_text("\n".join(logs_lines) + "\n")
    if metrics_lines is not None:
        (tmp_path / "metrics.jsonl").write_text("\n".join(metrics_lines) + "\n")
    return tmp_path


class TestMain:
    def test_a_directory_holding_no_logs_jsonl_exits_0_and_names_the_path(self, tmp_path, capsys):
        exit_code = stream_usage.main(["--dir", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert str(tmp_path) in out

    def test_a_missing_metrics_jsonl_skips_the_check_and_says_so(self, tmp_path, capsys):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        exit_code = stream_usage.main(["--dir", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "metrics.jsonl" in out
        assert "skipped" in out.lower()

    def test_rows_are_led_by_cost_usd_highest_first_and_the_total_is_stated(self, tmp_path, capsys):
        cheap = _api_request(**{"session.id": "sid-cheap"}, cost_usd=0.01)
        expensive = _api_request(**{"session.id": "sid-expensive"}, cost_usd=9.0)
        lines = [
            json.dumps(
                {
                    "signal": "logs",
                    "received_at": "2026-09-11T00:00:00+00:00",
                    "body": {
                        "resourceLogs": [
                            {
                                "resource": {"attributes": []},
                                "scopeLogs": [
                                    {
                                        "scope": {},
                                        "logRecords": [
                                            {
                                                "attributes": [
                                                    _attr(k, v) for k, v in r.record_attrs.items()
                                                ]
                                            }
                                            for r in (cheap, expensive)
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        ]
        _write_stream(tmp_path, lines)
        stream_usage.main(["--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert out.index("sid-expensive") < out.index("sid-cheap")
        assert "Total cost_usd: 9.010000" in out

    def test_json_reports_the_same_total_as_the_table(self, tmp_path, capsys):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        stream_usage.main(["--dir", str(tmp_path)])
        table_out = capsys.readouterr().out
        assert "0.216465" in table_out or "0.2164645" in table_out

        stream_usage.main(["--dir", str(tmp_path), "--json"])
        json_out = capsys.readouterr().out
        payload = json.loads(json_out)
        assert payload["rows"][0]["cost_usd"] == pytest.approx(0.2164645)

    def test_where_flag_filters_the_agent_rows(self, tmp_path, capsys):
        # --where is Task 2's own rule (and Task 5's --compare shares it) — it is never claimed
        # of Task 4's subagent count, so this checks the "0 agent row(s)" line, not the whole
        # output.
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        stream_usage.main(["--dir", str(tmp_path), "--where", "session.id=no-such-session"])
        out = capsys.readouterr().out
        assert "0 agent row(s)" in out

    def test_a_where_value_carrying_no_equals_sign_is_a_clear_error_not_a_crash(self, tmp_path):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        with pytest.raises(SystemExit, match="KEY=VALUE"):
            stream_usage.main(["--dir", str(tmp_path), "--where", "no-equals-sign-here"])

    def test_compare_flag_is_threaded_through_main(self, tmp_path, capsys):
        a = _api_request(**{"kensai.arm": "A", "session.id": "sid-1"}, cost_usd=0.10)
        b = _api_request(**{"kensai.arm": "B", "session.id": "sid-2"}, cost_usd=0.30)
        lines = [
            json.dumps(
                {
                    "signal": "logs",
                    "received_at": "2026-09-11T00:00:00+00:00",
                    "body": {
                        "resourceLogs": [
                            {
                                "resource": {"attributes": []},
                                "scopeLogs": [
                                    {
                                        "scope": {},
                                        "logRecords": [
                                            {
                                                "attributes": [
                                                    _attr(k, v) for k, v in r.record_attrs.items()
                                                ]
                                            }
                                            for r in (a, b)
                                        ],
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        ]
        _write_stream(tmp_path, lines)
        exit_code = stream_usage.main(["--dir", str(tmp_path), "--compare", "kensai.arm", "A", "B"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "A" in out and "B" in out
        assert "0.200000" in out  # B - A


# ── Integrity review — tests derived from the station plan, not the code ─────
#
# Added by `development-code-integrity` on 2026-09-11. Each test states a behaviour the plan at
# `system/projects/tools/journey/shukuba/2026-09-11_agent-usage-reads-a-run.md` names, at the level
# a person running the command sees it, where the suite above stopped at the unit.


def _logs_line(record_attr_maps: list[dict], resource_attrs: dict | None = None) -> str:
    return json.dumps(
        {
            "signal": "logs",
            "received_at": "2026-09-11T00:00:00+00:00",
            "body": {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [_attr(k, v) for k, v in (resource_attrs or {}).items()]
                        },
                        "scopeLogs": [
                            {
                                "scope": {},
                                "logRecords": [
                                    {"attributes": [_attr(k, v) for k, v in attrs.items()]}
                                    for attrs in record_attr_maps
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    )


class TestIntegrityTask1:
    def test_numeric_fields_arriving_as_strings_are_summed_as_numbers_from_file_to_row(self):
        def envelope(key: str, value: dict) -> dict:
            return {"key": key, "value": value}

        base = [
            envelope("event.name", {"stringValue": "api_request"}),
            envelope("session.id", {"stringValue": "sid-1"}),
        ]
        records = [
            {
                "attributes": base
                + [
                    envelope("cost_usd", {"stringValue": "0.5"}),
                    envelope("duration_ms", {"stringValue": "1200"}),
                    envelope("input_tokens", {"intValue": "7"}),
                ]
            },
            {
                "attributes": base
                + [
                    envelope("cost_usd", {"doubleValue": 0.25}),
                    envelope("duration_ms", {"intValue": 300}),
                    envelope("input_tokens", {"intValue": 3}),
                ]
            },
        ]
        line = json.dumps(
            {"signal": "logs", "body": {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}}
        )
        rows = stream_usage.agent_rows(stream_usage.read_log_lines([line]).records)
        assert len(rows) == 1
        assert rows[0].cost_usd == pytest.approx(0.75)
        assert rows[0].duration_ms == 1500
        assert rows[0].input_tokens == 10

    def test_the_skipped_line_count_is_reported_by_the_command(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            ["not json {", json.dumps({"signal": "logs"}), json.dumps(CAPTURED_LOGS_LINE)],
        )
        stream_usage.main(["--dir", str(tmp_path)])
        text = capsys.readouterr().out
        assert any("skipped" in line and "2" in line for line in text.splitlines())

        stream_usage.main(["--dir", str(tmp_path), "--json"])
        assert json.loads(capsys.readouterr().out)["skipped_lines"] == 2


class TestIntegrityTask2:
    def test_the_fold_sums_every_token_class_not_only_input(self):
        records = (
            _api_request(output_tokens=20, cache_read_tokens=300, cache_creation_tokens=40),
            _api_request(output_tokens=5, cache_read_tokens=100, cache_creation_tokens=2),
        )
        row = stream_usage.agent_rows(records)[0]
        assert (row.output_tokens, row.cache_read_tokens, row.cache_creation_tokens) == (
            25, 400, 42
        )

    @pytest.mark.parametrize(
        "key, first, second",
        [
            ("session.id", "sid-1", "sid-2"),
            ("workflow.run_id", "wf-1", "wf-2"),
            ("query_source", "agent:custom", "repl_main_thread"),
            ("agent.name", "custom", "general-purpose"),
        ],
    )
    def test_records_differing_on_any_one_part_of_the_key_land_on_separate_rows(
        self, key, first, second
    ):
        rows = stream_usage.agent_rows(
            (_api_request(**{key: first}), _api_request(**{key: second}))
        )
        assert len(rows) == 2

    def test_repeated_where_keeps_only_rows_matching_every_pair(self):
        records = (
            _api_request(**{"session.id": "sid-1", "kensai.arm": "x"}),
            _api_request(**{"session.id": "sid-1", "kensai.arm": "y"}),
            _api_request(**{"session.id": "sid-2", "kensai.arm": "x"}),
        )
        rows = stream_usage.agent_rows(
            records, where=(("session.id", "sid-1"), ("kensai.arm", "x")), prefix="kensai"
        )
        assert [(r.session_id, r.labels["kensai.arm"]) for r in rows] == [("sid-1", "x")]

    def test_where_reads_a_kensai_attribute_carried_only_on_the_resource(self):
        keep = _record(
            {"event.name": "api_request", "session.id": "sid-1"},
            resource_attrs={"kensai.arm": "keep"},
        )
        drop = _record(
            {"event.name": "api_request", "session.id": "sid-2"},
            resource_attrs={"kensai.arm": "drop"},
        )
        rows = stream_usage.agent_rows((keep, drop), where=(("kensai.arm", "keep"),))
        assert [r.session_id for r in rows] == ["sid-1"]

    def test_where_is_repeatable_on_the_command_line(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "sid-1", "kensai.arm": "x",
                            "cost_usd": 0.1},
                        {"event.name": "api_request", "session.id": "sid-1", "kensai.arm": "y",
                            "cost_usd": 0.2},
                        {"event.name": "api_request", "session.id": "sid-2", "kensai.arm": "x",
                            "cost_usd": 0.4},
                    ]
                )
            ],
        )
        stream_usage.main(
            [
                "--dir", str(tmp_path), "--json", "--where", "session.id=sid-1",
                "--where", "kensai.arm=x",
            ]
        )
        rows = json.loads(capsys.readouterr().out)["rows"]
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == pytest.approx(0.1)


class TestIntegrityTask3:
    def test_the_command_reports_both_figures_names_a_silent_session_and_flags_a_gap(
        self, tmp_path, capsys
    ):
        logs = [
            _logs_line(
                [
                    {"event.name": "api_request", "session.id": "sid-silent", "cost_usd": 0.05},
                    {"event.name": "api_request", "session.id": "sid-off", "cost_usd": 1.0},
                    {"event.name": "api_request", "session.id": "sid-ok", "cost_usd": 1.0},
                ]
            )
        ]
        metrics = [
            _metrics_line(
                _cost_metric([_cost_datapoint("sid-off", 1.5), _cost_datapoint("sid-ok", 0.6)])
            ),
            _metrics_line(_cost_metric([_cost_datapoint("sid-ok", 0.4)])),
        ]
        _write_stream(tmp_path, logs, metrics)

        stream_usage.main(["--dir", str(tmp_path)])
        text = capsys.readouterr().out
        silent_lines = [
            line for line in text.splitlines() if "sid-silent" in line and "log_cost" in line
        ]
        assert silent_lines and "no metric" in silent_lines[0]

        stream_usage.main(["--dir", str(tmp_path), "--json"])
        checks = {c["session_id"]: c for c in json.loads(capsys.readouterr().out)["metrics_check"]}
        assert checks["sid-silent"]["metric_cost_usd"] is None
        assert checks["sid-off"]["log_cost_usd"] == pytest.approx(1.0)
        assert checks["sid-off"]["metric_cost_usd"] == pytest.approx(1.5)
        assert checks["sid-off"]["within_tolerance"] is False
        assert checks["sid-ok"]["metric_cost_usd"] == pytest.approx(1.0)
        assert checks["sid-ok"]["within_tolerance"] is True


class TestIntegrityTask4:
    def test_the_coverage_note_is_output_and_the_lumped_total_tokens_never_is(
        self, tmp_path, capsys
    ):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        stream_usage.main(["--dir", str(tmp_path)])
        text = capsys.readouterr().out
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        json_text = capsys.readouterr().out

        assert stream_usage.SUBAGENT_COVERAGE_NOTE in text
        assert stream_usage.SUBAGENT_COVERAGE_NOTE in json.loads(json_text).values()
        for out in (text, json_text):
            assert "total_tokens" not in out
            assert "299840" not in out  # the real capture's lumped total_tokens value


class TestIntegrityTask5:
    FIGURES = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "duration_ms",
    )

    def _stream(self, tmp_path: Path) -> Path:
        records = [
            {"event.name": "api_request", "session.id": "sid-1", "kensai.arm": "A",
                "kensai.run_id": "r1",
             "input_tokens": 1001, "output_tokens": 1100, "cache_read_tokens": 50000,
             "cache_creation_tokens": 4100, "cost_usd": 0.10, "duration_ms": 60000},
            {"event.name": "api_request", "session.id": "sid-2", "kensai.arm": "A",
                "kensai.run_id": "r2",
             "input_tokens": 2002, "output_tokens": 2200, "cache_read_tokens": 70000,
             "cache_creation_tokens": 4200, "cost_usd": 0.30, "duration_ms": 80000},
            {"event.name": "api_request", "session.id": "sid-3", "kensai.arm": "B",
                "kensai.run_id": "r3",
             "input_tokens": 7, "output_tokens": 8, "cache_read_tokens": 9,
             "cache_creation_tokens": 11, "cost_usd": 0.50, "duration_ms": 13},
        ]
        return _write_stream(tmp_path, [_logs_line(records)])

    def test_json_emits_each_groups_totals_and_mean_per_run_over_every_figure(
        self, tmp_path, capsys
    ):
        self._stream(tmp_path)
        stream_usage.main(
            [
                "--dir", str(tmp_path), "--json", "--prefix", "kensai",
                "--compare", "kensai.arm", "A", "B",
            ]
        )
        compared = json.loads(capsys.readouterr().out)["compare"]
        a = compared["a"]
        assert a["run_count"] == 2
        expected_totals = dict(zip(self.FIGURES, (3003, 3300, 120000, 8300, 0.40, 140000)))
        for figure, total in expected_totals.items():
            assert a["totals"][figure] == pytest.approx(total), figure
            assert a["mean_per_run"][figure] == pytest.approx(total / 2), figure
        assert compared["diff"] == pytest.approx(0.50 - 0.20)
        assert compared["ratio"] == pytest.approx(0.50 / 0.20)

    def test_the_table_prints_each_groups_token_and_duration_totals(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.main(["--dir", str(tmp_path), "--compare", "kensai.arm", "A", "B"])
        out = capsys.readouterr().out
        block = out[out.index("--compare"):]
        for total in ("3003", "3300", "120000", "8300", "140000"):
            assert total in block, total

    def test_an_empty_group_is_named_and_no_ratio_is_printed(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.main(["--dir", str(tmp_path), "--compare", "kensai.arm", "A", "no-such-value"])
        out = capsys.readouterr().out
        block = out[out.index("--compare"):]
        assert any(
            "no-such-value" in line and "--compare" not in line for line in block.splitlines()
        )
        assert " / " not in block

        stream_usage.main(
            ["--dir", str(tmp_path), "--json", "--compare", "kensai.arm", "A", "no-such-value"]
        )
        compared = json.loads(capsys.readouterr().out)["compare"]
        assert compared["b"] is None
        assert compared["ratio"] is None


class TestIntegrityTask6:
    def test_a_path_nothing_occupies_exits_0_with_one_line_naming_it_and_no_row(
        self, tmp_path, capsys
    ):
        missing = tmp_path / "nothing" / "here"
        assert stream_usage.main(["--dir", str(missing)]) == 0
        out = capsys.readouterr().out
        # CP-291: the no-stream line stays the one line that names the path, first; the lines after
        # it say what to check. The test keeps its identity: this suite refuses a deleted id.
        assert out.splitlines()[0] == f"No stream at {missing} — no logs.jsonl there."
        assert "agent row(s)" not in out and "Total cost_usd" not in out

    def test_a_directory_with_no_logs_jsonl_prints_exactly_one_line(
        self, tmp_path, capsys
    ):
        assert stream_usage.main(["--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == f"No stream at {tmp_path} — no logs.jsonl there."
        assert "agent row(s)" not in out and "Total cost_usd" not in out


class TestSurfacedByIntegrity:
    """The three items `code-integrity-report.md` § Surfaced, not fixed raised, fixed by hand:
    a non-numeric `intValue` must not stop the read,
    a missing key must not match `--where KEY=None`,
    and a `--where` narrower than a session must not report a false metrics gap."""

    def test_a_non_numeric_int_value_string_decodes_to_none_rather_than_stopping_the_read(self):
        assert stream_usage.decode_value({"intValue": "abc"}) is None
        payload = json.loads(
            _logs_line([{"event.name": "api_request", "session.id": "sid-1", "cost_usd": 0.2}])
        )
        record = payload["body"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        record["attributes"].append({"key": "event.sequence", "value": {"intValue": "abc"}})
        rows = stream_usage.agent_rows(stream_usage.read_log_lines([json.dumps(payload)]).records)
        assert sum(r.cost_usd for r in rows) == pytest.approx(0.2)

    def test_where_key_equals_none_does_not_match_a_record_lacking_the_key(self):
        rows = stream_usage.agent_rows((_api_request(),), (("workflow.run_id", "None"),))
        assert rows == ()

    def test_a_where_narrower_than_a_session_is_held_against_the_whole_session(
        self, tmp_path, capsys
    ):
        logs = [
            _logs_line(
                [
                    {"event.name": "api_request", "session.id": "sid-1",
                        "query_source": "agent:custom", "cost_usd": 0.3},
                    {"event.name": "api_request", "session.id": "sid-1",
                        "query_source": "repl_main_thread", "cost_usd": 0.7},
                ]
            )
        ]
        metrics = [_metrics_line(_cost_metric([_cost_datapoint("sid-1", 1.0)]))]
        _write_stream(tmp_path, logs, metrics)

        stream_usage.main(
            ["--dir", str(tmp_path), "--where", "query_source=agent:custom", "--json"]
        )
        checks = {c["session_id"]: c for c in json.loads(capsys.readouterr().out)["metrics_check"]}
        assert checks["sid-1"]["log_cost_usd"] == pytest.approx(1.0)
        assert checks["sid-1"]["within_tolerance"] is True


# ── The stream change — a chosen prefix, one report, one totals check ────────
#
# Added by `2026-10-12_stream-read-from-the-package`. The library module no longer knows any
# fixed label name: a prefix is chosen by the caller or there are no labels at all.

RESERVED_ROW_KEYS = (
    "session_id",
    "workflow_run_id",
    "query_source",
    "agent_name",
    "models",
    "request_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
)
INVALID_PREFIXES = (*RESERVED_ROW_KEYS, "", "team.", ".")
AGENTS_NEXT_STEP = "Run `ka-telemetry agents --help` to see what it accepts."
FIGURE_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
)


def _totals_block(table: str) -> str:
    """The totals-check block of an `agents` table: from its first line to the line before the
    next empty one, plus one newline."""
    lines = table.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith(
            ("Totals check", "No metrics.jsonl found", "No metrics.jsonl was")
        )
    )
    end = start
    while end < len(lines) and lines[end] != "":
        end += 1
    return "\n".join(lines[start:end]) + "\n"


class TestChosenPrefix:
    def test_chosen_prefix_splits_labels_and_no_prefix_merges(self):
        records = (
            _api_request(**{"kensai.arm": "a"}),
            _api_request(**{"kensai.arm": "b"}),
        )
        split = stream_usage.agent_rows(records, prefix="kensai")
        assert sorted(row.labels["kensai.arm"] for row in split) == ["a", "b"]

        merged = stream_usage.agent_rows(records)
        assert len(merged) == 1
        assert merged[0].labels == {}
        assert merged[0].request_count == 2

    def test_a_label_is_read_off_the_record_first_over_the_union_of_both_sides_keys(self):
        record = _record(
            {"event.name": "api_request", "session.id": "s", "team.a": "from-record"},
            resource_attrs={"team.a": "from-resource", "team.b": "only-resource"},
        )
        row = stream_usage.agent_rows((record,), prefix="team")[0]
        assert row.labels == {"team.a": "from-record", "team.b": "only-resource"}

    def test_the_prefix_boundary_is_exact_in_case_and_dot(self):
        record = _api_request(
            **{"x.run_id": "1", "xmore.run_id": "2", "X.run_id": "3", "x": "4", "xrun_id": "5"}
        )
        assert stream_usage.agent_rows((record,), prefix="x")[0].labels == {"x.run_id": "1"}
        assert stream_usage.agent_rows((record,), prefix="X")[0].labels == {"X.run_id": "3"}
        assert stream_usage.agent_rows((record,), prefix="xmore")[0].labels == {
            "xmore.run_id": "2"
        }

    def test_a_row_with_no_label_carries_an_empty_map_when_a_prefix_is_chosen(self):
        row = stream_usage.agent_rows((_api_request(),), prefix="kensai")[0]
        assert row.labels == {}

    def test_an_unrelated_prefix_is_ignored_as_labels_but_still_selects_by_where(self):
        records = (
            _api_request(**{"other.k": "v", "session.id": "s1"}),
            _api_request(**{"other.k": "w", "session.id": "s2"}),
        )
        rows = stream_usage.agent_rows(records, where=(("other.k", "v"),), prefix="kensai")
        assert [(row.session_id, row.labels) for row in rows] == [("s1", {})]

    def test_label_keys_are_written_in_full_in_sorted_order(self):
        record = _api_request(**{"team.z": "1", "team.a": "2"})
        text = stream_usage.render(
            stream_usage.agent_rows((record,), prefix="team"), (), None, 0, None, prefix="team"
        )
        assert "    team: team.a=2, team.z=1\n" in text


class TestReservedRowKeyPrefixes:
    @pytest.mark.parametrize("prefix", INVALID_PREFIXES)
    def test_reserved_row_key_prefixes_empty_and_trailing_dot_rejected(self, prefix, tmp_path):
        reader = stream_usage.build_reader(tmp_path)
        calls = (
            lambda: stream_usage.agent_rows((), prefix=prefix),
            lambda: stream_usage.compare((), "attr", "A", "B", prefix=prefix),
            lambda: stream_usage.read_report(reader, prefix=prefix),
            lambda: stream_usage.render((), (), None, 0, None, prefix=prefix),
            lambda: stream_usage.render_json((), (), None, 0, None, prefix=prefix),
        )
        for call in calls:
            with pytest.raises(ValueError):
                call()

    @pytest.mark.parametrize("prefix", INVALID_PREFIXES)
    def test_the_command_refuses_each_with_exit_2_and_nothing_on_stdout(
        self, prefix, tmp_path, capsys
    ):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        with pytest.raises(SystemExit) as raised:
            stream_usage.main(["--dir", str(tmp_path), "--prefix", prefix])
        captured = capsys.readouterr()
        assert raised.value.code == 2
        assert captured.out == ""
        assert "--prefix" in captured.err
        assert captured.err.endswith(AGENTS_NEXT_STEP + "\n")

    @pytest.mark.parametrize("prefix", ["team", "kensai", "x", "Session_id", "cost"])
    def test_valid_prefixes_are_accepted(self, prefix, tmp_path):
        assert stream_usage.agent_rows((_api_request(),), prefix=prefix)
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        assert stream_usage.main(["--dir", str(tmp_path), "--prefix", prefix]) == 0

    @pytest.mark.parametrize(
        "prefix, message",
        [
            (
                "",
                "--prefix is empty; give the text your label keys start with, without the dot "
                "after it — for labels such as team.run_id, use --prefix team",
            ),
            (
                "team.",
                "--prefix 'team.' ends with a dot; give it without the dot — for labels such "
                "as team.run_id, use --prefix team",
            ),
            (
                "cost_usd",
                "--prefix 'cost_usd' is also the name of a field in each row of the --json "
                "output, so labels under it cannot be shown; use --where to select requests by "
                "those attributes instead",
            ),
        ],
    )
    def test_each_refusal_says_what_to_do_in_the_published_words(
        self, prefix, message, tmp_path, capsys
    ):
        with pytest.raises(SystemExit):
            stream_usage.main(["--dir", str(tmp_path), "--prefix", prefix])
        err = capsys.readouterr().err
        assert err.endswith(f"ka-telemetry agents: error: {message}\n{AGENTS_NEXT_STEP}\n")


class TestCompareRunIdentity:
    def test_compare_run_identity_uses_chosen_prefix(self):
        records = (
            _api_request(**{"team.arm": "A", "team.run_id": "r1", "session.id": "s1"}),
            _api_request(**{"team.arm": "A", "team.run_id": "r1", "session.id": "s2"}),
            _api_request(**{"team.arm": "A", "kensai.run_id": "zz", "session.id": "s3"}),
        )
        chosen = stream_usage.compare(records, "team.arm", "A", "B", prefix="team")
        assert chosen.a.run_count == 2  # r1, then s3 by the session fallback
        assert stream_usage.compare(records, "team.arm", "A", "B").a.run_count == 3

    def test_a_run_id_carried_only_on_the_resource_identifies_the_run(self):
        records = tuple(
            _record(
                {"event.name": "api_request", "session.id": session, "team.arm": "A"},
                resource_attrs={"team.run_id": "R"},
            )
            for session in ("s1", "s2")
        )
        result = stream_usage.compare(records, "team.arm", "A", "B", prefix="team")
        assert result.a.run_count == 1

    def test_the_filter_is_applied_before_the_comparison_and_repeated_filters_all_hold(self):
        records = (
            _api_request(**{"team.arm": "A", "session.id": "s1", "speed": "normal"}, cost_usd=0.1),
            _api_request(**{"team.arm": "A", "session.id": "s2", "speed": "fast"}, cost_usd=0.9),
            _api_request(**{"team.arm": "A", "session.id": "s1", "speed": "fast"}, cost_usd=0.8),
        )
        result = stream_usage.compare(
            records, "team.arm", "A", "B", where=(("speed", "normal"), ("session.id", "s1"))
        )
        assert result.a.run_count == 1
        assert result.a.totals.cost_usd == pytest.approx(0.1)

    def test_a_record_without_the_attribute_is_in_the_none_group_but_never_matches_where_none(
        self,
    ):
        records = (
            _api_request(**{"team.arm": "A", "session.id": "s1"}, cost_usd=0.1),
            _api_request(**{"session.id": "s2"}, cost_usd=0.3),
        )
        result = stream_usage.compare(records, "team.arm", "None", "A")
        assert result.a.run_count == 1
        assert result.a.totals.cost_usd == pytest.approx(0.3)
        assert stream_usage.agent_rows(records, where=(("team.arm", "None"),)) == ()

    def test_either_side_with_no_runs_leaves_the_difference_and_ratio_undefined(self):
        records = (_api_request(**{"team.arm": "A", "session.id": "s1"}, cost_usd=0.1),)
        neither = stream_usage.compare(records, "team.arm", "x", "y")
        assert (neither.a, neither.b, neither.diff, neither.ratio) == (None, None, None, None)
        only_a = stream_usage.compare(records, "team.arm", "A", "y")
        assert (only_a.b, only_a.diff, only_a.ratio) == (None, None, None)
        only_b = stream_usage.compare(records, "team.arm", "x", "A")
        assert (only_b.a, only_b.diff, only_b.ratio) == (None, None, None)

    def test_a_zero_mean_on_the_a_side_leaves_the_ratio_undefined_but_not_the_difference(self):
        records = (
            _api_request(**{"team.arm": "A", "session.id": "s1"}, cost_usd=0.0),
            _api_request(**{"team.arm": "B", "session.id": "s2"}, cost_usd=0.5),
        )
        result = stream_usage.compare(records, "team.arm", "A", "B")
        assert result.diff == pytest.approx(0.5)
        assert result.ratio is None
        text = stream_usage.render((), (), None, 0, result)
        assert "  B / A: ratio undefined (zero mean)\n" in text

    def test_the_same_value_on_both_sides_compares_a_group_with_itself(self):
        records = (_api_request(**{"team.arm": "A", "session.id": "s1"}, cost_usd=0.25),)
        result = stream_usage.compare(records, "team.arm", "A", "A")
        assert result.a == result.b
        assert (result.diff, result.ratio) == (0.0, 1.0)

    def test_counts_print_as_integers_and_every_other_figure_with_two_decimals(self):
        records = (
            _api_request(
                **{"team.arm": "A", "session.id": "s1"},
                input_tokens=1,
                output_tokens=2,
                cost_usd=0.5,
                duration_ms=10,
            ),
            _api_request(
                **{"team.arm": "A", "session.id": "s2"},
                input_tokens=2,
                output_tokens=2,
                cost_usd=0.25,
                duration_ms=11,
            ),
            _api_request(**{"team.arm": "B", "session.id": "s3"}, cost_usd=0.75),
        )
        result = stream_usage.compare(records, "team.arm", "A", "B")
        text = stream_usage.render((), (), None, 0, result)
        assert text[text.index("--compare") :] == (
            "--compare team.arm A B:\n"
            "  A: runs=2  mean_cost_per_run=0.375000  total_cost_usd=0.750000\n"
            "    totals:  input=3 output=4 cache_read=0 cache_creation=0 cost_usd=0.750000 "
            "duration_ms=21\n"
            "    per run: input=1.50 output=2.00 cache_read=0.00 cache_creation=0.00 "
            "cost_usd=0.375000 duration_ms=10.50\n"
            "  B: runs=1  mean_cost_per_run=0.750000  total_cost_usd=0.750000\n"
            "    totals:  input=10 output=20 cache_read=0 cache_creation=0 cost_usd=0.750000 "
            "duration_ms=100\n"
            "    per run: input=10.00 output=20.00 cache_read=0.00 cache_creation=0.00 "
            "cost_usd=0.750000 duration_ms=100.00\n"
            "  B - A = 0.375000\n"
            "  B / A = 2.000000\n"
        )


class TestReportShapes:
    def _records(self) -> list[dict]:
        return [
            {"event.name": "api_request", "session.id": "s-a", "model": "m2", "cost_usd": 0.25},
            {
                "event.name": "api_request",
                "session.id": "s-b",
                "model": "m1",
                "cost_usd": 0.25,
                "team.run_id": "t1",
            },
            {"event.name": "api_request", "session.id": "s-c", "cost_usd": 0.5,
             "input_tokens": 1.5, "duration_ms": 100.5},
            {"event.name": "api_request", "session.id": "s-c", "cost_usd": 0.0,
             "input_tokens": 1, "duration_ms": 20},
        ]

    def _written(self, tmp_path: Path) -> Path:
        return _write_stream(tmp_path, [_logs_line(self._records()), "not json at all"])

    def test_report_text_and_json_shapes_and_ordering(self, tmp_path, capsys):
        self._written(tmp_path)
        stream_usage.main(["--dir", str(tmp_path), "--prefix", "team"])
        text = capsys.readouterr().out
        assert text.splitlines()[:9] == [
            "3 agent row(s), 1 unreadable line(s) skipped.",
            "  session=s-c workflow_run_id=None query_source=None agent=None  requests=2  "
            "models=(none)  cost_usd=0.500000",
            "    input=2.5 output=0 cache_read=0 cache_creation=0 duration_ms=120.5",
            "  session=s-a workflow_run_id=None query_source=None agent=None  requests=1  "
            "models=m2  cost_usd=0.250000",
            "    input=0 output=0 cache_read=0 cache_creation=0 duration_ms=0",
            "  session=s-b workflow_run_id=None query_source=None agent=None  requests=1  "
            "models=m1  cost_usd=0.250000",
            "    input=0 output=0 cache_read=0 cache_creation=0 duration_ms=0",
            "    team: team.run_id=t1",
            "Total cost_usd: 1.000000",
        ]

        stream_usage.main(["--dir", str(tmp_path), "--prefix", "team", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert list(payload) == [
            "unreadable_metric_lines",
            "metrics_file",
            "skipped_lines",
            "total_cost_usd",
            "rows",
            "subagent_coverage_note",
            "subagent_rows",
            "metrics_check",
            "cost_estimate_note",
        ]
        assert payload["unreadable_metric_lines"] == 0, "CP-330: counted, and none is unreadable"
        assert [row["session_id"] for row in payload["rows"]] == ["s-c", "s-a", "s-b"]
        assert list(payload["rows"][2]) == [
            "session_id",
            "workflow_run_id",
            "query_source",
            "agent_name",
            "team",
            "models",
            "request_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_usd",
            "duration_ms",
        ]
        assert payload["rows"][2]["team"] == {"team.run_id": "t1"}
        assert payload["rows"][0]["team"] == {}
        assert payload["rows"][0]["workflow_run_id"] is None
        assert payload["rows"][2]["models"] == ["m1"]

    def test_without_a_prefix_no_label_line_or_label_key_appears(self, tmp_path, capsys):
        self._written(tmp_path)
        stream_usage.main(["--dir", str(tmp_path)])
        assert "team.run_id" not in capsys.readouterr().out
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        rows = json.loads(capsys.readouterr().out)["rows"]
        assert all("team" not in row and "labels" not in row for row in rows)

    def test_a_dynamic_label_key_never_appears_as_a_literal_labels_key(self, tmp_path, capsys):
        self._written(tmp_path)
        stream_usage.main(["--dir", str(tmp_path), "--prefix", "team", "--json"])
        assert all("labels" not in row for row in json.loads(capsys.readouterr().out)["rows"])

    def test_models_are_sorted_and_the_row_with_none_reads_none(self):
        records = (
            _api_request(model="b"),
            _api_request(model="a"),
            _record({"event.name": "api_request", "session.id": "other"}),
        )
        rows = {row.session_id: row for row in stream_usage.agent_rows(records)}
        assert rows["sid-1"].models == ("a", "b")
        assert rows["other"].models == ()

    def test_no_agent_tool_agent_prints_the_coverage_sentence_and_the_none_line(
        self, tmp_path, capsys
    ):
        _write_stream(tmp_path, [_logs_line([{"event.name": "api_request", "cost_usd": 0.1}])])
        stream_usage.main(["--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert (
            f"{stream_usage.SUBAGENT_COVERAGE_NOTE}\nNo subagent_completed records read.\n\n"
        ) in out
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["subagent_rows"] == []
        assert payload["subagent_coverage_note"] == stream_usage.SUBAGENT_COVERAGE_NOTE

    def test_agent_tool_lines_are_sorted_by_session_and_then_type(self, tmp_path, capsys):
        def completed(session: str, kind: str) -> dict:
            return {
                "event.name": "subagent_completed",
                "session.id": session,
                "agent_type": kind,
                "duration_ms": 5,
                "total_tool_uses": 2,
            }

        _write_stream(
            tmp_path,
            [_logs_line([completed("s2", "b"), completed("s1", "z"), completed("s1", "a")])],
        )
        stream_usage.main(["--dir", str(tmp_path)])
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.startswith("  session=")]
        assert lines == [
            "  session=s1 agent_type=a count=1 duration_ms=5 total_tool_uses=2",
            "  session=s1 agent_type=z count=1 duration_ms=5 total_tool_uses=2",
            "  session=s2 agent_type=b count=1 duration_ms=5 total_tool_uses=2",
        ]

    def test_zero_api_rows_still_prints_the_total(self, tmp_path, capsys):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        stream_usage.main(["--dir", str(tmp_path), "--where", "session.id=nothing"])
        assert capsys.readouterr().out.startswith(
            "0 agent row(s), 0 unreadable line(s) skipped.\nTotal cost_usd: 0.000000\n"
        )

    def test_unreadable_and_blank_lines_are_counted_apart(self, tmp_path):
        _write_stream(
            tmp_path,
            ["", "garbage", "[1, 2]", json.dumps({"body": {}}), json.dumps(CAPTURED_LOGS_LINE)],
        )
        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
        assert report.skipped_lines == 3
        assert len(report.rows) == 1

    def test_malformed_numeric_fields_read_as_their_number_or_zero(self, tmp_path):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s", "cost_usd": "0.5"},
                        {"event.name": "api_request", "session.id": "s", "cost_usd": "n/a"},
                    ]
                )
            ],
        )
        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
        assert report.rows[0].cost_usd == 0.5

    def test_tied_cost_rows_keep_the_order_their_first_record_appears(self, tmp_path):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": f"s-{name}", "cost_usd": 0.5}
                        for name in ("z", "m", "a")
                    ]
                )
            ],
        )
        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))
        assert [row.session_id for row in report.rows] == ["s-z", "s-m", "s-a"]


_COST_MARKING = (
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill."
)


# The causes a session's check carries, word for word (`change_text.md`), in the order it gives.
_NO_METRIC_CAUSES = [
    "the session may have ended before Claude Code exported a cost metric for it",
    "the session may still be running",
    "the receiver (ka-telemetry receive) may not have been running when Claude Code sent the "
    "cost metric",
]
_MISMATCH_CAUSES = [
    "the session may still be running, or may have ended before Claude Code exported all of it, "
    "so one of its two totals may not have caught up with the other",
    "the receiver (ka-telemetry receive) may not have been running for part of the session, so "
    "some of what Claude Code sent may never have arrived",
    "Claude Code's request costs and its cost metric may really disagree",
]
# What a text line carries after `no metric export` / `MISMATCH >1%`: the lead-in, then the causes.
_NO_METRIC_REASON = (
    "possible causes, which ka-telemetry cannot tell apart: the session may have ended before "
    "Claude Code exported a cost metric for it; the session may still be running; the receiver "
    "(ka-telemetry receive) may not have been running when Claude Code sent the cost metric"
)
_MISMATCH_REASON = (
    "possible causes, which ka-telemetry cannot tell apart: the session may still be running, or "
    "may have ended before Claude Code exported all of it, so one of its two totals may not have "
    "caught up with the other; the receiver (ka-telemetry receive) may not have been running for "
    "part of the session, so some of what Claude Code sent may never have arrived; Claude Code's "
    "request costs and its cost metric may really disagree"
)


class TestCostMarking:
    """CP-287, CP-288, CP-289: the one marking line, and its JSON member, in every output that
    shows a cost — and in none that shows no cost."""

    def _stream(self, tmp_path: Path) -> Path:
        return _write_stream(
            tmp_path,
            [_logs_line([{"event.name": "api_request", "session.id": "s", "cost_usd": 0.5}])],
            [_metrics_line(_cost_metric([_cost_datapoint("s", 0.5)]))],
        )

    def test_agents_text_marks_after_the_total_with_one_or_no_rows(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.main(["--dir", str(tmp_path)])
        text = capsys.readouterr().out
        assert text.splitlines()[3:5] == ["Total cost_usd: 0.500000", _COST_MARKING]

        stream_usage.main(["--dir", str(tmp_path), "--where", "session.id=nothing"])
        assert capsys.readouterr().out.splitlines()[:3] == [
            "0 agent row(s), 0 unreadable line(s) skipped.",
            "Total cost_usd: 0.000000",
            _COST_MARKING,
        ]

    def test_check_text_ends_with_the_marking_only_when_it_shows_a_session(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path)])
        assert capsys.readouterr().out.splitlines()[-1] == _COST_MARKING

        empty = tmp_path / "empty"
        empty.mkdir()
        _write_stream(empty, [], [])
        stream_usage.check_main(["--dir", str(empty)])
        assert "estimate" not in capsys.readouterr().out

        no_metrics = tmp_path / "no-metrics"
        no_metrics.mkdir()
        _write_stream(no_metrics, [_logs_line([{"event.name": "api_request", "session.id": "s"}])])
        stream_usage.check_main(["--dir", str(no_metrics)])
        # CP-358: an absent file no longer skips the check, so a shown session carries the marking.
        assert capsys.readouterr().out.splitlines()[-1] == _COST_MARKING
        (no_metrics / "logs.jsonl").write_text("")
        stream_usage.check_main(["--dir", str(no_metrics)])
        assert "estimate" not in capsys.readouterr().out

    def test_json_member_is_appended_after_every_existing_member(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        plain = json.loads(capsys.readouterr().out)
        stream_usage.main(["--dir", str(tmp_path), "--json", "--compare", "session.id", "s", "t"])
        compared = json.loads(capsys.readouterr().out)
        assert list(plain)[-2:] == ["metrics_check", "cost_estimate_note"]
        assert list(compared)[-2:] == ["compare", "cost_estimate_note"]
        assert plain["cost_estimate_note"] == compared["cost_estimate_note"] == _COST_MARKING

    def test_check_json_has_the_member_only_for_a_non_empty_metrics_check(self, tmp_path, capsys):
        self._stream(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        assert json.loads(capsys.readouterr().out)["cost_estimate_note"] == _COST_MARKING

        assert stream_usage.render_checks_json(None) == '{\n  "metrics_check": null\n}'
        assert stream_usage.render_checks_json(()) == '{\n  "metrics_check": []\n}'


class TestNoMetricReason:
    """CP-292, CP-293, CP-301: a session with requests and no cost metric keeps its line up to
    `no metric export`, then says why — in the one block `check` and `agents` share."""

    def _logs(self, directory: Path, sessions: dict[str, float]) -> None:
        records = [
            {"event.name": "api_request", "session.id": session, "cost_usd": cost}
            for session, cost in sessions.items()
        ]
        _write_stream(directory, [_logs_line(records)])

    def _blocks(self, directory: Path, capsys) -> tuple[list[str], list[str]]:
        stream_usage.main(["--dir", str(directory)])
        agents = _totals_block(capsys.readouterr().out).splitlines()
        stream_usage.check_main(["--dir", str(directory)])
        check = capsys.readouterr().out.splitlines()
        return agents, check

    def test_no_metric_session_line_keeps_prefix_and_names_three_causes(self):
        check = stream_usage.SessionMetricsCheck("s", 0.5, None, None)
        _, line = stream_usage.render_checks((check,)).splitlines()[:2]
        assert line.startswith("  session=s  log_cost_usd=0.500000  no metric export")
        assert line == f"  session=s  log_cost_usd=0.500000  no metric export — {_NO_METRIC_REASON}"

    def test_reason_is_identical_in_agents_and_check(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 0.5})
        (tmp_path / "metrics.jsonl").write_text("")
        agents, check = self._blocks(tmp_path, capsys)
        expected = f"  session=s1  log_cost_usd=0.500000  no metric export — {_NO_METRIC_REASON}"
        assert expected in agents and expected in check

    def test_no_metric_session_is_not_a_mismatch(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 0.5})
        (tmp_path / "metrics.jsonl").write_text("")
        _, check = self._blocks(tmp_path, capsys)
        assert not any("MISMATCH" in line for line in check)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        (entry,) = json.loads(capsys.readouterr().out)["metrics_check"]
        assert entry == {
            "session_id": "s1",
            "log_cost_usd": 0.5,
            "metric_cost_usd": None,
            "within_tolerance": None,
            "possible_causes": _NO_METRIC_CAUSES,
        }

    @pytest.mark.parametrize(
        "metric",
        [
            _metrics_line(_cost_metric([_cost_datapoint("other", 0.5)])),
            _metrics_line(
                {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "claude_code.token.usage",
                                            "sum": {"dataPoints": [_cost_datapoint("s1", 9)]},
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ),
        ],
        ids=["only-another-session", "only-another-metric"],
    )
    def test_session_absent_from_metrics_that_hold_only_another_session_or_another_metric(
        self, tmp_path, capsys, metric
    ):
        self._logs(tmp_path, {"s1": 0.5})
        (tmp_path / "metrics.jsonl").write_text(metric + "\n")
        _, check = self._blocks(tmp_path, capsys)
        assert check[1] == (
            f"  session=s1  log_cost_usd=0.500000  no metric export — {_NO_METRIC_REASON}"
        )

    def test_a_zero_valued_metric_is_a_metric_not_an_absence(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 0.0})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("s1", 0.0)])) + "\n"
        )
        _, check = self._blocks(tmp_path, capsys)
        assert check[1] == "  session=s1  log_cost_usd=0.000000  metric_cost_usd=0.000000"

    def test_genuine_mismatch_and_agreement_lines_remain_exact(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0, "s2": 1.0})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("s1", 1.0), _cost_datapoint("s2", 2.0)]))
            + "\n"
        )
        _, check = self._blocks(tmp_path, capsys)
        assert check[1:3] == [
            "  session=s1  log_cost_usd=1.000000  metric_cost_usd=1.000000",
            f"  session=s2  log_cost_usd=1.000000  metric_cost_usd=2.000000  MISMATCH >1% — "
            f"{_MISMATCH_REASON}",
        ]

    @pytest.mark.parametrize(
        "metrics",
        [
            pytest.param(_metrics_line(_cost_metric([_cost_datapoint("s1", 0.5)])), id="clean"),
            pytest.param("not json", id="malformed"),
            pytest.param(None, id="missing"),
            pytest.param("", id="empty"),
            pytest.param(
                _metrics_line(_cost_metric([_cost_datapoint("s2", 0.5)])), id="no-metric-session"
            ),
        ],
    )
    def test_check_text_equals_the_totals_block_of_agents_once_framing_is_removed(
        self, tmp_path, capsys, metrics
    ):
        self._logs(tmp_path, {"s1": 0.5})
        if metrics is not None:
            (tmp_path / "metrics.jsonl").write_text(metrics + "\n")
        agents, check = self._blocks(tmp_path, capsys)
        if check and check[-1] == _COST_MARKING:
            check = check[:-1]
        if check and check[0].startswith("Unreadable lines in metrics.jsonl"):
            check = check[1:]
        assert check == agents


_CUSTOM_EXPLANATION = (
    "Claude Code reports an agent you defined yourself as custom, and names it only when Claude "
    "Code's detailed tool logging is on. To tell such agents apart, label your runs with "
    "ka-telemetry turn-on --prefix PREFIX --label KEY=VALUE and read this report with --prefix "
    "PREFIX: rows from runs you labelled differently are shown apart."
)


class TestCustomAgentExplanation:
    """CP-294: one explanation after the marking line when a printed row shows `custom` — an agent
    row after `--where`, or an Agent-tool row as printed — and none otherwise."""

    def _agents(
        self, tmp_path: Path, capsys, *records: dict, where: list[str] | None = None
    ) -> str:
        _write_stream(tmp_path, [_logs_line(list(records))])
        arguments = ["--dir", str(tmp_path)]
        for pair in where or []:
            arguments += ["--where", pair]
        stream_usage.main(arguments)
        return capsys.readouterr().out

    def _request(self, session: str, agent: str) -> dict:
        return {"event.name": "api_request", "session.id": session, "agent.name": agent}

    def _agent_tool(self, session: str, agent_type: str) -> dict:
        return {"event.name": "subagent_completed", "session.id": session, "agent_type": agent_type}

    def test_an_agent_row_named_custom_prints_it_once_after_the_marking(self, tmp_path, capsys):
        out = self._agents(tmp_path, capsys, self._request("s", "custom"))
        lines = out.splitlines()
        marking = lines.index(_COST_MARKING)
        assert lines[marking + 1 : marking + 3] == [_CUSTOM_EXPLANATION, ""]
        assert out.count(_CUSTOM_EXPLANATION) == 1

    def test_an_agent_tool_row_of_type_custom_prints_it_once(self, tmp_path, capsys):
        out = self._agents(
            tmp_path, capsys, self._request("s", "named"), self._agent_tool("s", "custom")
        )
        assert out.count(_CUSTOM_EXPLANATION) == 1

    def test_custom_in_both_prints_it_once(self, tmp_path, capsys):
        out = self._agents(
            tmp_path, capsys, self._request("s", "custom"), self._agent_tool("s", "custom")
        )
        assert out.count(_CUSTOM_EXPLANATION) == 1

    def test_the_rows_as_printed_decide_not_the_raw_input(self, tmp_path, capsys):
        out = self._agents(
            tmp_path,
            capsys,
            self._request("s1", "custom"),
            self._request("s2", "named"),
            where=["session.id=s2"],
        )
        assert _CUSTOM_EXPLANATION not in out

    def test_excluded_custom_row_brings_no_explanation(self, tmp_path, capsys):
        out = self._agents(
            tmp_path,
            capsys,
            self._request("s2", "named"),
            self._agent_tool("s1", "custom"),
            where=["session.id=s2"],
        )
        assert _CUSTOM_EXPLANATION not in out

    def test_a_custom_row_in_a_shown_session_brings_exactly_one_explanation(
        self, tmp_path, capsys
    ):
        out = self._agents(
            tmp_path,
            capsys,
            self._request("s2", "named"),
            self._agent_tool("s2", "custom"),
            where=["session.id=s2"],
        )
        assert out.count(_CUSTOM_EXPLANATION) == 1

    @pytest.mark.parametrize("name", ["Custom", "customer", "CUSTOM", ""])
    def test_only_the_exact_lowercase_name_counts(self, tmp_path, capsys, name):
        out = self._agents(tmp_path, capsys, self._request("s", name))
        assert _CUSTOM_EXPLANATION not in out

    def test_json_carries_no_explanation(self, tmp_path, capsys):
        _write_stream(tmp_path, [_logs_line([self._request("s", "custom")])])
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        assert _CUSTOM_EXPLANATION not in capsys.readouterr().out


class TestMetricsCoverageRendering:
    """CP-296, CP-297, CP-298: `render` and `render_checks` take the unreadable metrics-line count
    as an optional keyword and state it for 1 or more only; every old call keeps its shape."""

    _CHECKS = (stream_usage.SessionMetricsCheck("s", 1.0, 1.0, True),)

    def test_render_extends_the_first_line_only_for_a_positive_count(self):
        base = "0 agent row(s), 2 unreadable line(s) skipped."
        assert stream_usage.render((), (), None, 2, None).splitlines()[0] == base
        assert (
            stream_usage.render((), (), None, 2, None, skipped_metric_lines=0).splitlines()[0]
            == base
        )
        first = stream_usage.render((), (), None, 2, None, skipped_metric_lines=3).splitlines()[0]
        assert first.startswith(base + " ")
        assert "metrics.jsonl" in first and first.endswith(": 3.")

    def test_render_checks_prepends_the_coverage_line_outside_the_block(self):
        plain = stream_usage.render_checks(self._CHECKS)
        covered = stream_usage.render_checks(self._CHECKS, skipped_metric_lines=2)
        coverage, rest = covered.split("\n", 1)
        assert rest == plain
        assert "metrics.jsonl" in coverage and coverage.endswith(": 2.")

    def test_render_checks_keeps_its_old_call_shape(self):
        assert stream_usage.render_checks(None) == (
            "No metrics.jsonl found — totals check skipped.\n"
        )
        assert stream_usage.render_checks(
            None, skipped_metric_lines=0
        ) == stream_usage.render_checks(
            None
        )

    def test_the_command_passes_the_report_count(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            [_logs_line([{"event.name": "api_request", "session.id": "s", "cost_usd": 0.5}])],
            ["not json", "[1]"],
        )
        stream_usage.main(["--dir", str(tmp_path)])
        assert capsys.readouterr().out.splitlines()[0].endswith(": 2.")
        stream_usage.check_main(["--dir", str(tmp_path)])
        assert capsys.readouterr().out.splitlines()[0].endswith(": 2.")

    def test_json_gets_no_coverage_member(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            [_logs_line([{"event.name": "api_request", "session.id": "s"}])],
            ["not json"],
        )
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        agents = json.loads(capsys.readouterr().out)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        check = json.loads(capsys.readouterr().out)
        assert "skipped_metric_lines" not in agents and "skipped_metric_lines" not in check
        assert agents["skipped_lines"] == 0


_OLD_REPORT_MEMBERS = [
    "skipped_lines",
    "total_cost_usd",
    "rows",
    "subagent_coverage_note",
    "subagent_rows",
    "metrics_check",
    "cost_estimate_note",
]
_COUNT_MEMBER = "unreadable_metric_lines"


def _stream_report(tmp_path: Path, *, with_metrics: bool = True) -> stream_usage.StreamReport:
    metrics = [_metrics_line(_cost_metric([_cost_datapoint("s1", 0.25)]))] if with_metrics else None
    _write_stream(
        tmp_path,
        [
            _logs_line(
                [
                    {"event.name": "api_request", "session.id": "s1", "kensai.arm": "A",
                     "cost_usd": 0.25},
                    {"event.name": "api_request", "session.id": "s2", "kensai.arm": "B",
                     "cost_usd": 0.5},
                ]
            )
        ],
        metrics,
    )
    return stream_usage.read_report(
        stream_usage.build_reader(tmp_path), comparison=("kensai.arm", "A", "B")
    )


def _renderer_args(report: stream_usage.StreamReport) -> tuple:
    return (
        report.rows,
        report.subagent_rows,
        report.metrics_check,
        report.skipped_lines,
        report.compare,
    )


class TestUnreadableMetricLinesMember:
    """CP-330, CP-331: the JSON renderers take the unreadable-metric-lines count as an optional
    keyword; the member stands first, and without it every output is the old one."""

    def test_render_json_default_call_is_unchanged(self, tmp_path):
        """Pins the direct call `TestCostMarking` makes: `render_json((), (), None, 0, None)`."""
        payload = json.loads(stream_usage.render_json((), (), None, 0, None))
        assert list(payload) == _OLD_REPORT_MEMBERS
        assert _COUNT_MEMBER not in payload

        args = _renderer_args(_stream_report(tmp_path))
        with_report = json.loads(stream_usage.render_json(*args))
        assert list(with_report) == [
            *_OLD_REPORT_MEMBERS[:-1],
            "compare",
            "cost_estimate_note",
        ]
        assert _COUNT_MEMBER not in with_report

    def test_render_json_explicit_zero_adds_the_count_first(self, tmp_path):
        shown = _renderer_args(_stream_report(tmp_path))
        old = json.loads(stream_usage.render_json(*shown))
        payload = json.loads(stream_usage.render_json(*shown, skipped_metric_lines=0))
        assert list(payload)[0] == _COUNT_MEMBER
        assert payload[_COUNT_MEMBER] == 0
        assert {key: payload[key] for key in list(payload)[1:]} == old
        assert list(payload)[1:] == list(old)

    def test_render_json_positive_count_is_carried(self):
        payload = json.loads(
            stream_usage.render_json((), (), None, 0, None, skipped_metric_lines=3)
        )
        assert payload[_COUNT_MEMBER] == 3
        assert list(payload) == [_COUNT_MEMBER, *_OLD_REPORT_MEMBERS]

    def test_the_count_is_keyword_only_in_both_renderers(self):
        with pytest.raises(TypeError):
            stream_usage.render_json((), (), None, 0, None, None, 1)
        with pytest.raises(TypeError):
            stream_usage.render_checks_json(None, 1)

    def test_render_checks_json_default_call_is_unchanged(self, tmp_path):
        assert stream_usage.render_checks_json(None) == '{\n  "metrics_check": null\n}'
        assert stream_usage.render_checks_json(()) == '{\n  "metrics_check": []\n}'
        checks = _stream_report(tmp_path).metrics_check
        payload = json.loads(stream_usage.render_checks_json(checks))
        assert list(payload) == ["metrics_check", "cost_estimate_note"]
        assert stream_usage.render_checks_json(checks).startswith('{\n  "metrics_check": [\n')

    @pytest.mark.parametrize("which", ["null", "empty", "non-empty"])
    def test_render_checks_json_explicit_count_adds_the_member_first(self, which, tmp_path):
        checks = {"null": None, "empty": (), "non-empty": _stream_report(tmp_path).metrics_check}[
            which
        ]
        old = json.loads(stream_usage.render_checks_json(checks))
        for count in (0, 2):
            rendered = stream_usage.render_checks_json(checks, skipped_metric_lines=count)
            payload = json.loads(rendered)
            assert list(payload) == [_COUNT_MEMBER, *old]
            assert payload[_COUNT_MEMBER] == count
            assert {key: payload[key] for key in old} == old
            assert rendered.startswith(f'{{\n  "{_COUNT_MEMBER}": {count},\n  "metrics_check": ')

    def test_check_json_shapes_with_the_count(self):
        assert stream_usage.render_checks_json(None, skipped_metric_lines=0) == (
            '{\n  "unreadable_metric_lines": 0,\n  "metrics_check": null\n}'
        )
        assert stream_usage.render_checks_json((), skipped_metric_lines=4) == (
            '{\n  "unreadable_metric_lines": 4,\n  "metrics_check": []\n}'
        )

    def test_cost_marking_member_conditions_are_unchanged_with_the_count(self, tmp_path):
        checks = _stream_report(tmp_path).metrics_check
        for shown_checks, marked in ((None, False), ((), False), (checks, True)):
            payload = json.loads(
                stream_usage.render_checks_json(shown_checks, skipped_metric_lines=1)
            )
            assert ("cost_estimate_note" in payload) is marked
            assert list(payload)[-1] == ("cost_estimate_note" if marked else "metrics_check")

        args = _renderer_args(_stream_report(tmp_path))
        report_payload = json.loads(stream_usage.render_json(*args, skipped_metric_lines=1))
        assert list(report_payload)[-2:] == ["compare", "cost_estimate_note"]
        bare = json.loads(stream_usage.render_json((), (), None, 0, None, skipped_metric_lines=1))
        assert list(bare)[-2:] == ["metrics_check", "cost_estimate_note"]

    def test_report_shapes_list_the_count_first(self, tmp_path):
        """The renderer's own shape; the CLI's carries the count once Task 4 passes it."""
        report = _stream_report(tmp_path)
        payload = json.loads(
            stream_usage.render_json(
                *_renderer_args(report),
                prefix="kensai",
                skipped_metric_lines=report.skipped_metric_lines,
            )
        )
        assert list(payload) == [
            _COUNT_MEMBER,
            "skipped_lines",
            "total_cost_usd",
            "rows",
            "subagent_coverage_note",
            "subagent_rows",
            "metrics_check",
            "compare",
            "cost_estimate_note",
        ]
        assert payload[_COUNT_MEMBER] == 0

    def test_report_text_is_byte_identical(self, tmp_path):
        report = _stream_report(tmp_path)
        shown = _renderer_args(report)
        assert stream_usage.render(*shown) == stream_usage.render(
            *shown, skipped_metric_lines=report.skipped_metric_lines
        )
        assert stream_usage.render_checks(None) == (
            "No metrics.jsonl found \u2014 totals check skipped.\n"
        )
        assert stream_usage.render_checks(report.metrics_check).splitlines()[0] == (
            "Totals check (claude_code.cost.usage vs summed cost_usd):"
        )


class TestUnreadableMetricLinesThroughTheCommands:
    """CP-330, CP-331: both commands pass the report's whole-file count to the JSON renderers, so
    it is always there, first, an integer, and the same number the text form states."""

    _REQUEST = [{"event.name": "api_request", "session.id": "s", "cost_usd": 0.5}]
    _BAD_LINES = {
        "empty file": ([], 0),
        "blank lines only": (["", "   ", "\t"], 0),
        "not json": (["not json"], 1),
        "outer value is not an object": (["[1, 2]"], 1),
        "body missing": (['{"signal": "metrics"}'], 1),
        "body is not an object": (['{"body": 3}'], 1),
        "body is an empty object": (['{"body": {}}'], 1),
        "several together": (["not json", "[1]", '{"body": {}}', ""], 3),
    }

    def _stream(self, tmp_path: Path, metrics: list[str] | None) -> Path:
        return _write_stream(tmp_path, [_logs_line(self._REQUEST)], metrics)

    def _json(self, command, tmp_path: Path, capsys, *arguments: str) -> dict:
        assert command(["--dir", str(tmp_path), "--json", *arguments]) == 0
        printed = capsys.readouterr().out
        document = json.loads(printed)
        assert printed == json.dumps(document, indent=2) + "\n"
        return document

    @pytest.mark.parametrize("category", _BAD_LINES)
    @pytest.mark.parametrize("command", [stream_usage.main, stream_usage.check_main])
    def test_the_count_is_first_and_counts_each_unreadable_line_category(
        self, category, command, tmp_path, capsys
    ):
        """CP-330: the parser's categories, blank lines left out."""
        metrics, expected = self._BAD_LINES[category]
        self._stream(tmp_path, metrics)

        document = self._json(command, tmp_path, capsys)

        assert list(document)[0] == "unreadable_metric_lines"
        assert document["unreadable_metric_lines"] == expected
        assert type(document["unreadable_metric_lines"]) is int

    @pytest.mark.parametrize("command", [stream_usage.main, stream_usage.check_main])
    def test_the_count_is_zero_with_no_metrics_file(self, command, tmp_path, capsys):
        """CP-330: never absent and never null where there is no `metrics.jsonl`."""
        self._stream(tmp_path, None)

        document = self._json(command, tmp_path, capsys)

        assert document["unreadable_metric_lines"] == 0
        # CP-359: an absent file is a state with one entry per session, never `null`.
        assert document["metrics_file"] == {"state": "nothing_received"}
        assert [entry["session_id"] for entry in document["metrics_check"]] == ["s"]

    @pytest.mark.parametrize("command", [stream_usage.main, stream_usage.check_main])
    def test_the_json_count_equals_the_number_the_text_form_states(
        self, command, tmp_path, capsys
    ):
        """CP-330: one count, two forms."""
        self._stream(tmp_path, ["not json", "[1]"])

        document = self._json(command, tmp_path, capsys)
        assert command(["--dir", str(tmp_path)]) == 0

        assert document["unreadable_metric_lines"] == 2
        assert capsys.readouterr().out.splitlines()[0].endswith(": 2.")

    def test_agents_keeps_counting_unreadable_logs_lines_in_skipped_lines(self, tmp_path, capsys):
        """CP-330: the new member is the metrics file's count, and `skipped_lines` is still the
        logs file's."""
        _write_stream(tmp_path, [_logs_line(self._REQUEST), "not json"], ["not json", "[1]"])

        document = self._json(stream_usage.main, tmp_path, capsys)

        assert (document["skipped_lines"], document["unreadable_metric_lines"]) == (1, 2)

    @pytest.mark.parametrize("command", [stream_usage.main, stream_usage.check_main])
    def test_a_readable_export_with_no_cost_metric_is_an_integer_and_keeps_its_marking(
        self, command, tmp_path, capsys
    ):
        """CP-330, CP-331: an export that holds no cost point is readable, so it adds nothing to
        the count, and the cost marking member is there exactly as it was."""
        self._stream(tmp_path, [_NO_COST_EXPORT])

        document = self._json(command, tmp_path, capsys)

        assert document["unreadable_metric_lines"] == 0
        assert list(document)[-1] == "cost_estimate_note"

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param(["--where", "session.id=s"], id="where-selecting-some"),
            pytest.param(["--where", "session.id=nothing"], id="where-selecting-none"),
            pytest.param(["--prefix", "kensai"], id="prefix"),
            pytest.param(["--compare", "session.id", "s", "other"], id="compare"),
            pytest.param(
                ["--where", "session.id=nothing", "--compare", "session.id", "s", "other"],
                id="compare-and-where",
            ),
        ],
    )
    def test_agents_count_is_the_whole_file_under_where_prefix_and_compare(
        self, arguments, tmp_path, capsys
    ):
        """CP-330, CP-336 group C: selecting rows moves no figure of the metrics file."""
        self._stream(tmp_path, ["not json"])

        document = self._json(stream_usage.main, tmp_path, capsys, *arguments)

        assert document["unreadable_metric_lines"] == 1

    def test_an_empty_readable_logs_file_with_malformed_metrics_reports_the_count(
        self, tmp_path, capsys
    ):
        """CP-330: an existing empty `logs.jsonl` is a report, with an empty check."""
        _write_stream(tmp_path, [], ["not json"])

        agents = self._json(stream_usage.main, tmp_path, capsys)
        check = self._json(stream_usage.check_main, tmp_path, capsys)

        assert check == {
            "unreadable_metric_lines": 1,
            "metrics_file": {"state": "read"},
            "skipped_lines": 0,
            "metrics_check": [],
        }
        assert agents["unreadable_metric_lines"] == 1 and agents["metrics_check"] == []
        assert "state" not in agents and "state" not in check

    def test_removing_the_count_gives_the_json_the_renderers_gave_before(self, tmp_path, capsys):
        """CP-331, CP-333: every old member keeps its name, value and relative order."""
        self._stream(tmp_path, [_metrics_line(_cost_metric([_cost_datapoint("s", 0.5)])), "x"])
        report = stream_usage.read_report(
            stream_usage.build_reader(tmp_path), comparison=("session.id", "s", "other")
        )
        old_agents = stream_usage.render_json(*_renderer_args(report))
        old_check = stream_usage.render_checks_json(report.metrics_check)

        agents = self._json(
            stream_usage.main, tmp_path, capsys, "--compare", "session.id", "s", "other"
        )
        check = self._json(stream_usage.check_main, tmp_path, capsys)

        assert agents.pop("unreadable_metric_lines") == 1
        assert check.pop("unreadable_metric_lines") == 1
        # CP-353: both documents carry `metrics_file` next, with the state `read`.
        assert agents.pop("metrics_file") == check.pop("metrics_file") == {"state": "read"}
        # CP-357: `check` also carries the whole-file `logs.jsonl` count, directly after it.
        assert check.pop("skipped_lines") == report.skipped_lines
        assert json.dumps(agents, indent=2) == old_agents
        assert json.dumps(check, indent=2) == old_check

    @pytest.mark.parametrize("arguments", [[], ["--compare", "session.id", "s", "other"]])
    def test_the_text_of_a_read_directory_is_the_text_it_was(self, arguments, tmp_path, capsys):
        """CP-331: text names the count as it did, and no JSON member leaks into it."""
        self._stream(tmp_path, ["not json"])
        report = stream_usage.read_report(
            stream_usage.build_reader(tmp_path),
            comparison=tuple(arguments[1:]) or None,
        )

        assert stream_usage.main(["--dir", str(tmp_path), *arguments]) == 0

        printed = capsys.readouterr().out
        assert printed == stream_usage.render(
            *_renderer_args(report), skipped_metric_lines=report.skipped_metric_lines
        )
        assert "unreadable_metric_lines" not in printed


_NOTHING_RECEIVED_TEXT = (
    "This directory holds no requests received from Claude Code yet.",
    "Check that Claude Code's telemetry is turned on: ka-telemetry turn-on writes the settings "
    "that turn it on.",
    "Check that the receiver is running and writing to {directory}: ka-telemetry receive runs it, "
    "and its --dir option sets the directory it writes to.",
)


def _nothing_received_lines(directory: Path) -> list[str]:
    """CP-325's four lines, written out from `change_text.md`, not read from the product."""
    first, second, third = _NOTHING_RECEIVED_TEXT
    return [
        f"No stream at {directory} \u2014 no logs.jsonl there.",
        first,
        second,
        third.format(directory=directory),
    ]


def _nothing_received_document(directory: Path) -> dict:
    _, statement, first_check, second_check = _nothing_received_lines(directory)
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


def _nothing_received_directories(tmp_path: Path) -> dict[str, Path]:
    """Every way looking at `<dir>/logs.jsonl` finds nothing there (CP-325)."""
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


@pytest.fixture
def mode_000_logs(tmp_path: Path):
    """A directory holding a `logs.jsonl` this user may not read; restored on exit."""
    directory = tmp_path / "refused"
    directory.mkdir()
    logs = directory / "logs.jsonl"
    logs.write_text("secret\n")
    logs.chmod(0)
    yield directory
    logs.chmod(0o600)


@pytest.fixture
def directory_named_logs(tmp_path: Path) -> Path:
    directory = tmp_path / "named"
    (directory / "logs.jsonl").mkdir(parents=True)
    return directory


@pytest.fixture
def mode_000_directory(tmp_path: Path, seal) -> Path:
    """A directory holding a `logs.jsonl`, which the user may not look into."""
    directory = tmp_path / "sealed"
    directory.mkdir()
    (directory / "logs.jsonl").write_text("secret\n")
    return seal(directory)


@pytest.fixture
def logs_link_into_mode_000_directory(tmp_path: Path, seal) -> Path:
    """A readable directory whose `logs.jsonl` is a link into a directory the user may not look
    into: the directory opens, and the link cannot be followed."""
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "logs.jsonl").write_text("secret\n")
    seal(inner)
    stream = tmp_path / "stream"
    stream.mkdir()
    (stream / "logs.jsonl").symlink_to(inner / "logs.jsonl")
    return stream


_ENTRIES = pytest.mark.parametrize("entry", ["main", "check_main"])
_NOTHING_VARIANTS = pytest.mark.parametrize(
    "variant", ["empty-directory", "absent-directory", "path-through-a-file", "dangling-symlink"]
)
_VALID_AGENTS_OPTIONS = [
    [],
    ["--prefix", "kensai"],
    ["--where", "session.id=x"],
    ["--compare", "kensai.arm", "a", "b"],
    ["--prefix", "kensai", "--where", "session.id=x", "--compare", "kensai.arm", "a", "b"],
]


class TestNothingReceivedAndUnopenable:
    """CP-325 to CP-329: what `agents` and `check` print when `logs.jsonl` is not there, or is
    there and cannot be read — the same words in text and JSON, the same document from both."""

    @_ENTRIES
    @_NOTHING_VARIANTS
    def test_check_text_nothing_received_prints_the_four_lines(
        self, entry, variant, tmp_path, capsys
    ):
        """CP-325: both commands print the four lines, nothing else, and exit 0."""
        directory = _nothing_received_directories(tmp_path)[variant]

        assert getattr(stream_usage, entry)(["--dir", str(directory)]) == 0

        captured = capsys.readouterr()
        assert captured.out == "\n".join(_nothing_received_lines(directory)) + "\n"
        assert captured.err == ""
        for absent in ("Totals check", "No metrics.jsonl found", "estimate", "agent row(s)"):
            assert absent not in captured.out

    @pytest.mark.parametrize("options", _VALID_AGENTS_OPTIONS)
    def test_agents_text_nothing_received_keeps_the_four_lines_with_every_option(
        self, options, tmp_path, capsys
    ):
        """CP-325: `agents` prints the same four lines with any valid option combination."""
        assert stream_usage.main(["--dir", str(tmp_path), *options]) == 0
        captured = capsys.readouterr()
        assert captured.out == "\n".join(_nothing_received_lines(tmp_path)) + "\n"
        assert captured.err == ""

    @pytest.mark.parametrize("entry", ["main", "check_main"])
    def test_default_directory_and_tilde_are_expanded_in_the_nothing_received_lines(
        self, entry, tmp_path, monkeypatch, capsys
    ):
        """CP-325: the lines name the expanded default directory and a leading `~`."""
        monkeypatch.setenv("HOME", str(tmp_path))
        run = getattr(stream_usage, entry)

        assert run([]) == 0
        assert capsys.readouterr().out.splitlines() == _nothing_received_lines(
            tmp_path / ".ka-telemetry"
        )
        assert run(["--dir", "~/elsewhere"]) == 0
        assert capsys.readouterr().out.splitlines() == _nothing_received_lines(
            tmp_path / "elsewhere"
        )

    @_ENTRIES
    @_NOTHING_VARIANTS
    def test_json_nothing_received_is_one_object_with_the_exact_schema(
        self, entry, variant, tmp_path, capsys
    ):
        """CP-326: one two-space JSON object, members in the design's order, no report member."""
        directory = _nothing_received_directories(tmp_path)[variant]

        assert getattr(stream_usage, entry)(["--dir", str(directory), "--json"]) == 0

        captured = capsys.readouterr()
        document = json.loads(captured.out)
        assert list(document) == ["state", "directory", "message", "checks"]
        assert document == _nothing_received_document(directory)
        assert len(document["checks"]) == 2
        assert captured.out == json.dumps(document, indent=2, ensure_ascii=True) + "\n"
        assert captured.err == ""
        text_lines = _nothing_received_lines(directory)
        assert [document["message"], *document["checks"]] == text_lines[1:]
        for excluded in (
            "rows", "total_cost_usd", "skipped_lines", "subagent_rows", "subagent_coverage_note",
            "metrics_check", "compare", "cost_estimate_note", "unreadable_metric_lines", "reason",
        ):
            assert excluded not in document

    @pytest.mark.parametrize("options", _VALID_AGENTS_OPTIONS)
    def test_json_nothing_received_is_identical_for_agents_and_check(
        self, options, tmp_path, capsys
    ):
        """CP-326: the same bytes from both commands for the same directory."""
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        from_check = capsys.readouterr().out
        assert stream_usage.main(["--dir", str(tmp_path), "--json", *options]) == 0
        from_agents = capsys.readouterr().out

        assert from_agents == from_check
        assert json.loads(from_agents) == _nothing_received_document(tmp_path)

    @_ENTRIES
    def test_json_specials_in_directory_survive_serialization(self, entry, tmp_path, capsys):
        """CP-326, CP-328: a quote, a backslash and a non-ASCII character in the directory name."""
        awkward = tmp_path / 'qu"ote\\back\u00e9'
        run = getattr(stream_usage, entry)

        assert run(["--dir", str(awkward), "--json"]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document == _nothing_received_document(awkward)
        assert document["directory"] == str(awkward)

        (awkward / "logs.jsonl").mkdir(parents=True)
        assert run(["--dir", str(awkward), "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == _unopenable_document(
            awkward, "Is a directory"
        )

    @_needs_a_non_root_user
    @pytest.mark.parametrize("options", _VALID_AGENTS_OPTIONS)
    def test_unopenable_mode_000_text_for_agents_and_check(
        self, options, mode_000_logs, capsys
    ):
        """CP-327: the statement names the directory, says logs.jsonl is there and could not be
        read, and gives `Permission denied`; both commands print the same bytes."""
        assert stream_usage.check_main(["--dir", str(mode_000_logs)]) == 0
        from_check = capsys.readouterr()
        assert stream_usage.main(["--dir", str(mode_000_logs), *options]) == 0
        from_agents = capsys.readouterr()

        statement = _unopenable_statement(mode_000_logs, "Permission denied")
        assert from_check.out == from_agents.out == statement + "\n"
        assert from_check.err == from_agents.err == ""
        for absent in ("no logs.jsonl there", "Traceback", "holds no requests", "Check that"):
            assert absent not in from_check.out
        assert "agent row(s)" not in from_agents.out and "Total cost_usd" not in from_agents.out

    @_ENTRIES
    def test_unopenable_directory_named_logs_jsonl_text(self, entry, directory_named_logs, capsys):
        """CP-327: a directory named `logs.jsonl` — runs as any user, root included."""
        assert getattr(stream_usage, entry)(["--dir", str(directory_named_logs)]) == 0
        captured = capsys.readouterr()
        assert captured.out == _unopenable_statement(directory_named_logs, "Is a directory") + "\n"
        assert captured.err == ""

    @_needs_a_non_root_user
    @pytest.mark.parametrize("options", _VALID_AGENTS_OPTIONS)
    def test_unopenable_json_for_agents_and_check(
        self, options, mode_000_logs, directory_named_logs, capsys
    ):
        """CP-328: both causes, both commands: exact schema and order, the reason as its own
        string, no report member, no `checks`, identical bytes."""
        for directory, reason in (
            (mode_000_logs, "Permission denied"),
            (directory_named_logs, "Is a directory"),
        ):
            stream_usage.check_main(["--dir", str(directory), "--json"])
            from_check = capsys.readouterr()
            assert stream_usage.main(["--dir", str(directory), "--json", *options]) == 0
            from_agents = capsys.readouterr()

            assert from_check.out == from_agents.out
            assert from_check.err == from_agents.err == ""
            document = json.loads(from_check.out)
            assert list(document) == ["state", "directory", "message", "reason"]
            assert document == _unopenable_document(directory, reason)
            assert from_check.out == json.dumps(document, indent=2) + "\n"
            for absent in ("checks", "rows", "metrics_check", "unreadable_metric_lines"):
                assert absent not in document
            assert "no logs.jsonl there" not in from_check.out and "Traceback" not in from_check.out

    def test_json_preserves_the_original_reason_without_reading_again(self, tmp_path, capsys):
        """CP-328 and design T4: JSON preserves the one read's reason, including JSON specials."""
        reason = 'Device "busy" at \\server: caf\u00e9\ntry later'

        class RefusedFiles:
            def __init__(self):
                self.calls = []

            def read_outcome(self, path):
                self.calls.append(path)
                assert len(self.calls) == 1, "the original read must not be repeated"
                return stream_usage.ReadOutcome(
                    state=stream_usage.ReadState.UNOPENABLE, reason=reason
                )

            def read_lines(self, path):
                pytest.fail(f"a failed logs read must not lead to another read: {path}")

        files = RefusedFiles()
        reader = stream_usage.Reader(files=files, directory=tmp_path)
        result = stream_usage.read_report_result(reader)

        stream_usage._print_unread_state(reader, result, as_json=True)

        captured = capsys.readouterr()
        expected = _unopenable_document(tmp_path, reason)
        assert json.loads(captured.out) == expected
        assert captured.out == json.dumps(expected, indent=2) + "\n"
        assert captured.err == ""
        assert files.calls == [tmp_path / "logs.jsonl"]
        assert result.report is None and result.reason == reason

    @_ENTRIES
    def test_json_states_are_told_apart_by_the_state_member(
        self, entry, directory_named_logs, tmp_path, capsys
    ):
        """CP-329: missing and unopenable carry `state`, with two values; a readable report, an
        existing empty `logs.jsonl` included, carries none."""
        run = getattr(stream_usage, entry)
        missing = tmp_path / "missing"
        empty_logs = tmp_path / "empty-logs"
        empty_logs.mkdir()
        (empty_logs / "logs.jsonl").write_text("")
        readable = tmp_path / "readable"
        readable.mkdir()
        _write_stream(readable, [json.dumps(CAPTURED_LOGS_LINE)])

        states = {}
        for name, directory in (
            ("missing", missing), ("unopenable", directory_named_logs),
            ("empty", empty_logs), ("readable", readable),
        ):
            run(["--dir", str(directory), "--json"])
            states[name] = json.loads(capsys.readouterr().out).get("state")

        assert states == {
            "missing": "nothing_received", "unopenable": "unopenable", "empty": None,
            "readable": None,
        }

    @_ENTRIES
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_unknown_inspection_case_names_the_statement_and_stays_apart(
        self, entry, json_flag, mode_000_directory, capsys
    ):
        """Where looking at the path fails, neither state is claimed: the CP-354 statement, or the
        CP-355 document, alone — and the state is told apart from the other two failed reads."""
        assert getattr(stream_usage, entry)(["--dir", str(mode_000_directory), *json_flag]) == 0
        captured = capsys.readouterr()
        if json_flag:
            document = json.loads(captured.out)
            assert document == _undetermined_logs_document(mode_000_directory, "Permission denied")
            assert document["state"] not in ("nothing_received", "unopenable")
        else:
            assert captured.out == (
                _undetermined_logs_statement(mode_000_directory, "Permission denied") + "\n"
            )
        assert captured.err == ""

    @pytest.mark.parametrize("variant", ["missing", "unopenable"])
    def test_validation_precedes_the_read(self, variant, directory_named_logs, tmp_path, capsys):
        """The existing validation still fails first, whatever the directory holds."""
        directory = tmp_path / "missing" if variant == "missing" else directory_named_logs
        with pytest.raises(SystemExit) as bad_prefix:
            stream_usage.main(["--dir", str(directory), "--prefix", ""])
        assert bad_prefix.value.code == 2
        with pytest.raises(SystemExit) as bad_where:
            stream_usage.main(["--dir", str(directory), "--where", "nokv"])
        assert bad_where.value.code == "error: --where expects KEY=VALUE, got 'nokv'"
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "entry, options",
        [
            ("main", {"--help", "--dir", "--prefix", "--where", "--compare", "--json"}),
            ("check_main", {"--help", "--dir", "--json"}),
        ],
    )
    def test_help_and_options_are_unchanged(self, entry, options, capsys):
        """CP-334: no option gained or lost."""
        with pytest.raises(SystemExit) as helped:
            getattr(stream_usage, entry)(["--help"])
        assert helped.value.code == 0
        import re

        listed = set(
            re.findall(r"^  (?:-\w, )?(-{1,2}[\w-]+)", capsys.readouterr().out, flags=re.MULTILINE)
        )
        assert listed == options


class TestUndeterminedLogs:
    """CP-354, CP-355: what `agents` and `check` print when `logs.jsonl` could not be read and
    looking at its path failed too — one statement, or one JSON document, from one source."""

    @_ENTRIES
    def test_cp354_mode_000_directory_text_names_directory_and_reason(
        self, entry, mode_000_directory, capsys
    ):
        assert getattr(stream_usage, entry)(["--dir", str(mode_000_directory)]) == 0

        captured = capsys.readouterr()
        statement = _undetermined_logs_statement(mode_000_directory, "Permission denied")
        assert captured.out == statement + "\n"
        assert captured.err == ""
        for absent in ("No stream at", "no logs.jsonl there", "Traceback", "agent row(s)"):
            assert absent not in captured.out

    @_ENTRIES
    def test_cp355_mode_000_directory_json_is_one_document_with_four_members(
        self, entry, mode_000_directory, capsys
    ):
        run = getattr(stream_usage, entry)
        assert run(["--dir", str(mode_000_directory)]) == 0
        text = capsys.readouterr().out
        assert run(["--dir", str(mode_000_directory), "--json"]) == 0

        captured = capsys.readouterr()
        document = json.loads(captured.out)
        result = stream_usage.read_report_result(stream_usage.build_reader(mode_000_directory))
        assert list(document) == ["state", "directory", "message", "reason"]
        assert document["state"] == result.state.value
        assert document["state"] not in ("nothing_received", "unopenable")
        assert document["directory"] == str(mode_000_directory)
        assert document["message"] + "\n" == text
        assert document["reason"] == "Permission denied"
        assert captured.out == json.dumps(document, indent=2) + "\n"
        assert captured.err == ""

    @_ENTRIES
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_cp354_logs_link_into_mode_000_directory_makes_no_claim_about_the_directory(
        self, entry, json_flag, logs_link_into_mode_000_directory, capsys
    ):
        stream = logs_link_into_mode_000_directory
        assert getattr(stream_usage, entry)(["--dir", str(stream), *json_flag]) == 0

        captured = capsys.readouterr()
        statement = _undetermined_logs_statement(stream, "Permission denied")
        if json_flag:
            assert json.loads(captured.out) == _undetermined_logs_document(
                stream, "Permission denied"
            )
        else:
            assert captured.out == statement + "\n"
        for claim in (
            "No stream at", "no logs.jsonl there", "There is a logs.jsonl",
            "could not be opened", "directory could not",
        ):
            assert claim not in captured.out
        assert captured.err == ""

    @_ENTRIES
    def test_missing_and_unopenable_logs_keep_their_documents(
        self, entry, tmp_path, directory_named_logs, capsys
    ):
        run = getattr(stream_usage, entry)
        missing = tmp_path / "missing"

        assert run(["--dir", str(missing)]) == 0
        assert capsys.readouterr().out == "\n".join(_nothing_received_lines(missing)) + "\n"
        assert run(["--dir", str(missing), "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == _nothing_received_document(missing)
        assert run(["--dir", str(directory_named_logs)]) == 0
        assert capsys.readouterr().out == (
            _unopenable_statement(directory_named_logs, "Is a directory") + "\n"
        )
        assert run(["--dir", str(directory_named_logs), "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == _unopenable_document(
            directory_named_logs, "Is a directory"
        )

    def test_library_reports_none_and_the_state_and_reason_for_an_undetermined_logs(
        self, mode_000_directory
    ):
        reader = stream_usage.build_reader(mode_000_directory)

        assert stream_usage.read_report(reader) is None
        result = stream_usage.read_report_result(reader)
        assert result.state is stream_usage.ReadState.UNINSPECTABLE
        assert result.reason == "Permission denied"
        assert result.report is None

    @_ENTRIES
    @pytest.mark.parametrize(
        "state",
        [
            stream_usage.ReadState.NOTHING_RECEIVED,
            stream_usage.ReadState.UNOPENABLE,
            stream_usage.ReadState.UNINSPECTABLE,
        ],
    )
    def test_the_unreadable_directory_cases_run_in_the_hand_written_files_fake(
        self, entry, state, tmp_path, capsys
    ):
        """Each failed outcome a fake hands back prints one statement, and the JSON `message` is
        that statement, with a reason holding quotes, a backslash, Unicode and a newline."""
        reason = 'Device "busy" at \\server: caf\u00e9\ntry later'

        class FailingFiles:
            def read_outcome(self, path):
                return stream_usage.ReadOutcome(state=state, reason=reason)

        reader = stream_usage.Reader(files=FailingFiles(), directory=tmp_path)
        result = stream_usage.read_report_result(reader)

        stream_usage._print_unread_state(reader, result, as_json=False)
        text = capsys.readouterr().out
        stream_usage._print_unread_state(reader, result, as_json=True)
        document = json.loads(capsys.readouterr().out)

        assert document["state"] == state.value
        assert document["message"] in text
        if state is stream_usage.ReadState.UNINSPECTABLE:
            assert text == document["message"] + "\n"
            assert document == _undetermined_logs_document(tmp_path, reason)

    def test_mode_000_fixture_restores_the_mode_and_skips_as_root(self, tmp_path):
        directory = tmp_path / "guarded"
        directory.mkdir()
        original = directory.stat().st_mode & 0o777

        with pytest.raises(AssertionError):
            with sealed_paths(1000) as seal:
                seal(directory)
                assert directory.stat().st_mode & 0o777 == 0
                raise AssertionError("the test body failed")
        assert directory.stat().st_mode & 0o777 == original

        with pytest.raises(pytest.skip.Exception):
            with sealed_paths(0):
                pytest.fail("a block run as root must be skipped before it starts")


_STORED_SET = Path(__file__).resolve().parent / "stream_parity"
_CHECK_HEADING = "Totals check (claude_code.cost.usage vs summed cost_usd):"
_CHECK_LOGS_COUNT = (
    "Unreadable lines in logs.jsonl, skipped and left out of the totals check: {count}."
)
_CHECK_METRICS_COUNT = (
    "Unreadable lines in metrics.jsonl, skipped and left out of the totals check: {count}."
)


def _stored_set_copy(
    directory: Path, *, drop_logs_line: int | None = None, extra_metrics=()
) -> Path:
    """A copy of the stored data set, optionally without one `logs.jsonl` line (counted from 1)
    and with unreadable lines appended to `metrics.jsonl`."""
    directory.mkdir(parents=True)
    logs = (_STORED_SET / "logs.jsonl").read_text().splitlines(keepends=True)
    if drop_logs_line is not None:
        del logs[drop_logs_line - 1]
    (directory / "logs.jsonl").write_text("".join(logs))
    metrics = (_STORED_SET / "metrics.jsonl").read_text()
    appended = "".join(f"{line}\n" for line in extra_metrics)
    (directory / "metrics.jsonl").write_text(metrics + appended)
    return directory


class TestCheckCountsUnreadableLogsLines:
    """CP-356, CP-357: `check` states how many `logs.jsonl` lines it could not read — in text when
    the count is not 0, in `--json` always — and `agents`, which already did, does not change."""

    def _check_text(self, directory: Path, capsys) -> list[str]:
        assert stream_usage.check_main(["--dir", str(directory)]) == 0
        return capsys.readouterr().out.splitlines()

    def _json(self, run, directory: Path, capsys, *options: str) -> dict:
        assert run(["--dir", str(directory), "--json", *options]) == 0
        return json.loads(capsys.readouterr().out)

    def test_cp356_check_text_states_the_logs_count_first_of_the_block(self, capsys):
        lines = self._check_text(_STORED_SET, capsys)

        assert lines[0] == _CHECK_LOGS_COUNT.format(count=1)
        assert lines[1] == _CHECK_HEADING

    def test_cp356_count_line_comes_after_the_metrics_coverage_line(self, tmp_path, capsys):
        copy = _stored_set_copy(tmp_path / "copy", extra_metrics=["not json {"])

        lines = self._check_text(copy, capsys)

        assert lines[:3] == [
            _CHECK_METRICS_COUNT.format(count=1), _CHECK_LOGS_COUNT.format(count=1), _CHECK_HEADING,
        ]

    def test_cp356_zero_count_prints_no_logs_line(self, tmp_path, capsys):
        copy = _stored_set_copy(tmp_path / "copy", drop_logs_line=2)

        lines = self._check_text(copy, capsys)

        assert lines[0] == _CHECK_HEADING
        assert not any(line.startswith("Unreadable lines in logs.jsonl") for line in lines)

    def test_cp357_check_json_carries_skipped_lines_equal_to_agents(self, tmp_path, capsys):
        zero = _stored_set_copy(tmp_path / "zero", drop_logs_line=2)

        check = self._json(stream_usage.check_main, _STORED_SET, capsys)
        agents = self._json(stream_usage.main, _STORED_SET, capsys)
        check_zero = self._json(stream_usage.check_main, zero, capsys)
        agents_zero = self._json(stream_usage.main, zero, capsys)

        assert check["skipped_lines"] == agents["skipped_lines"] == 1
        assert "skipped_lines" in check_zero
        assert check_zero["skipped_lines"] == agents_zero["skipped_lines"] == 0
        assert type(check_zero["skipped_lines"]) is int

    def test_cp357_check_json_old_members_keep_name_value_and_relative_order(self, capsys):
        report = stream_usage.read_report(stream_usage.build_reader(_STORED_SET))
        before = stream_usage.render_checks_json(
            report.metrics_check, skipped_metric_lines=report.skipped_metric_lines
        )

        check = self._json(stream_usage.check_main, _STORED_SET, capsys)

        assert list(check)[:3] == ["unreadable_metric_lines", "metrics_file", "skipped_lines"]
        assert check.pop("metrics_file") == {"state": "read"}
        del check["skipped_lines"]
        assert json.dumps(check, indent=2) == before

    def test_agents_text_and_json_are_unchanged_by_the_count(self, capsys):
        assert stream_usage.main(["--dir", str(_STORED_SET), "--prefix", "kensai"]) == 0
        text = capsys.readouterr().out
        as_json = self._json(stream_usage.main, _STORED_SET, capsys, "--prefix", "kensai")
        stored = json.loads((_STORED_SET / "expected" / "agents.json").read_text())

        assert text.splitlines()[0] == "16 agent row(s), 1 unreadable line(s) skipped."
        assert "Unreadable lines in logs.jsonl" not in text
        assert list(as_json) == [
            "unreadable_metric_lines", "metrics_file", *stored, "cost_estimate_note",
        ]
        assert as_json["metrics_file"] == {"state": "read"}
        assert as_json["skipped_lines"] == stored["skipped_lines"]

    def test_direct_renderer_calls_without_skipped_lines_are_unchanged(self):
        check = stream_usage.SessionMetricsCheck("s1", 0.5, None, None)
        marker = "  session=s1  log_cost_usd=0.500000  no metric export"
        session_line = f"{marker} — {_NO_METRIC_REASON}"
        as_text = "\n".join([_CHECK_HEADING, session_line, _COST_MARKING]) + "\n"
        entry = {
            "session_id": "s1", "log_cost_usd": 0.5, "metric_cost_usd": None,
            "within_tolerance": None, "possible_causes": _NO_METRIC_CAUSES,
        }
        document = {"metrics_check": [entry], "cost_estimate_note": _COST_MARKING}

        assert stream_usage.render_checks((check,)) == as_text
        assert stream_usage.render_checks((check,), skipped_lines=None) == as_text
        assert stream_usage.render_checks_json((check,)) == json.dumps(document, indent=2)
        assert stream_usage.render_checks_json((check,), skipped_metric_lines=0) == json.dumps(
            {"unreadable_metric_lines": 0, **document}, indent=2
        )
        assert stream_usage.render_checks((check,), skipped_lines=2) == (
            _CHECK_LOGS_COUNT.format(count=2) + "\n" + as_text
        )

    def test_the_count_is_whole_file_even_when_no_session_is_shown(self, tmp_path, capsys):
        (tmp_path / "logs.jsonl").write_text("not json\n")

        assert self._check_text(tmp_path, capsys) == [
            _CHECK_LOGS_COUNT.format(count=1),
            f"No metrics.jsonl was found in {tmp_path}, "
            "so no session has a cost metric to check against.",
            _CHECK_HEADING,
            "  No sessions to check.",
        ]
        (tmp_path / "metrics.jsonl").write_text("")
        assert self._check_text(tmp_path, capsys) == [
            _CHECK_LOGS_COUNT.format(count=1), _CHECK_HEADING, "  No sessions to check.",
        ]
        document = self._json(stream_usage.check_main, tmp_path, capsys)
        assert document["skipped_lines"] == 1 and document["metrics_check"] == []


class TestReadReport:
    @pytest.mark.parametrize("entry", ["main", "check_main"])
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_unreadable_logs_return_no_stream(self, entry, json_flag, tmp_path, capsys):
        """CP-327, CP-328: the library still answers `None`, and the command now says the file is
        there and why it could not be read, where it said nothing was there."""
        # A directory is an unreadable log file even when tests run with elevated permissions.
        (tmp_path / "logs.jsonl").mkdir()
        reader = stream_usage.build_reader(tmp_path)

        assert stream_usage.read_report(reader) is None
        assert getattr(stream_usage, entry)(["--dir", str(tmp_path), *json_flag]) == 0
        captured = capsys.readouterr()
        statement = _unopenable_statement(tmp_path, "Is a directory")
        if json_flag:
            assert json.loads(captured.out) == _unopenable_document(tmp_path, "Is a directory")
        else:
            assert captured.out == statement + "\n"
        assert "no logs.jsonl there" not in captured.out
        assert captured.err == ""

    def test_missing_logs_is_none_and_missing_metrics_is_a_state_apart_from_empty(self, tmp_path):
        reader = stream_usage.build_reader(tmp_path)
        assert stream_usage.read_report(reader) is None

        _write_stream(tmp_path, [_logs_line([{"event.name": "subagent_completed"}])])
        without_metrics = stream_usage.read_report(reader)
        # CP-360: absent is a state of its own, with no session to check here, not `None`.
        assert without_metrics.metrics_state is stream_usage.ReadState.NOTHING_RECEIVED
        assert without_metrics.metrics_check == ()
        assert without_metrics.rows == ()

        _write_stream(tmp_path, None, [""])
        with_empty_metrics = stream_usage.read_report(reader)
        assert with_empty_metrics.metrics_check == ()
        assert with_empty_metrics.metrics_state is stream_usage.ReadState.READ

    def test_the_report_carries_rows_checks_and_the_comparison_it_was_asked_for(self, tmp_path):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s1", "kensai.arm": "A",
                         "cost_usd": 0.2},
                        {"event.name": "api_request", "session.id": "s2", "kensai.arm": "B",
                         "cost_usd": 0.4},
                    ]
                )
            ],
            [_metrics_line(_cost_metric([_cost_datapoint("s1", 0.2)]))],
        )
        report = stream_usage.read_report(
            stream_usage.build_reader(tmp_path),
            prefix="kensai",
            where=(("session.id", "s1"),),
            comparison=("kensai.arm", "A", "B"),
        )
        assert [row.session_id for row in report.rows] == ["s1"]
        assert [check.session_id for check in report.metrics_check] == ["s1"]
        assert report.compare.a.run_count == 1
        assert report.compare.b is None  # the filter ran before the comparison

    def test_without_a_comparison_the_report_has_none(self, tmp_path):
        _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        assert stream_usage.read_report(stream_usage.build_reader(tmp_path)).compare is None


class TestMissingLogs:
    @pytest.mark.parametrize("entry", ["main", "check_main"])
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_missing_logs_message_and_empty_logs_differ(self, entry, json_flag, tmp_path, capsys):
        """CP-325, CP-326: a directory with no `logs.jsonl` says nothing was received, in the text
        or the JSON; a present empty `logs.jsonl` is a read and says no such thing."""
        run = getattr(stream_usage, entry)
        missing = tmp_path / "nothing"
        assert run(["--dir", str(missing), *json_flag]) == 0
        printed = capsys.readouterr().out
        if json_flag:
            assert json.loads(printed) == _nothing_received_document(missing)
        else:
            assert printed.splitlines() == _nothing_received_lines(missing)

        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "logs.jsonl").write_text("")
        assert run(["--dir", str(empty), *json_flag]) == 0
        read = capsys.readouterr().out
        assert "No stream at" not in read and "holds no requests" not in read

    def test_the_path_named_is_the_directory_with_its_leading_tilde_expanded(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        stream_usage.main(["--dir", "~/elsewhere"])
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == f"No stream at {tmp_path / 'elsewhere'} — no logs.jsonl there."
        assert f"writing to {tmp_path / 'elsewhere'}:" in printed[-1]

    def test_the_default_directory_is_the_one_named_when_no_directory_is_given(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        stream_usage.check_main([])
        assert capsys.readouterr().out.splitlines() == _nothing_received_lines(
            tmp_path / ".ka-telemetry"
        )


class TestMalformedWhere:
    @pytest.mark.parametrize("with_logs", [True, False])
    def test_malformed_where_exits_1_even_when_logs_absent(self, with_logs, tmp_path, capsys):
        if with_logs:
            _write_stream(tmp_path, [json.dumps(CAPTURED_LOGS_LINE)])
        with pytest.raises(SystemExit) as raised:
            stream_usage.main(["--dir", str(tmp_path), "--where", "bad"])
        assert raised.value.code == "error: --where expects KEY=VALUE, got 'bad'"
        assert capsys.readouterr().out == ""

    def test_an_empty_right_hand_side_and_an_equals_inside_the_value_are_accepted(
        self, tmp_path, capsys
    ):
        _write_stream(
            tmp_path,
            [_logs_line([{"event.name": "api_request", "session.id": "s", "query_source": "a=b"}])],
        )
        assert stream_usage.main(["--dir", str(tmp_path), "--where", "query_source=a=b"]) == 0
        assert capsys.readouterr().out.startswith("1 agent row(s)")
        assert stream_usage.main(["--dir", str(tmp_path), "--where", "query_source="]) == 0
        assert capsys.readouterr().out.startswith("0 agent row(s)")


class TestTotalsCheck:
    def _row(self, session_id: str, cost_usd: float) -> stream_usage.AgentRow:
        return stream_usage.AgentRow(
            session_id=session_id,
            workflow_run_id=None,
            query_source=None,
            agent_name=None,
            labels={},
            models=(),
            request_count=1,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost_usd,
            duration_ms=0,
        )

    def _points(self, session_id: str, value: float):
        return stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_cost_datapoint(session_id, value)]))]
        )

    @pytest.mark.parametrize(
        "log, metric, within",
        [
            (100.0, 101.0, True),  # exactly 1% of the log cost
            (100.0, 100.5, True),  # inside
            (100.0, 101.5, False),  # outside
            (100.0, 98.5, False),  # outside, below
            (0.0, 0.5, False),  # zero log, positive metric: measured against the metric
            (0.0, 0.0, True),  # both zero: no difference
        ],
    )
    def test_totals_check_semantics(self, log, metric, within):
        (check,) = stream_usage.metrics_check((self._row("s", log),), self._points("s", metric))
        assert check.within_tolerance is within

    def test_sessions_are_checked_in_session_id_order(self):
        rows = (self._row("s-b", 1.0), self._row("s-a", 1.0))
        checks = stream_usage.metrics_check(rows, ())
        assert [check.session_id for check in checks] == ["s-a", "s-b"]

    def test_a_session_with_no_data_point_has_no_metric(self):
        (check,) = stream_usage.metrics_check((self._row("s", 1.0),), self._points("other", 1.0))
        assert (check.metric_cost_usd, check.within_tolerance) == (None, None)

    def _logs(self, tmp_path: Path, sessions: dict[str, float]) -> None:
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": session, "cost_usd": cost}
                        for session, cost in sessions.items()
                    ]
                )
            ],
        )

    def test_the_check_prints_one_line_per_session_in_the_published_words(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0, "s2": 1.0, "s3": 1.0})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("s1", 1.0), _cost_datapoint("s2", 2.0)]))
            + "\n"
        )
        assert stream_usage.check_main(["--dir", str(tmp_path)]) == 0  # a mismatch is not an error
        assert capsys.readouterr().out == (
            "Totals check (claude_code.cost.usage vs summed cost_usd):\n"
            "  session=s1  log_cost_usd=1.000000  metric_cost_usd=1.000000\n"
            f"  session=s2  log_cost_usd=1.000000  metric_cost_usd=2.000000  MISMATCH >1% — "
            f"{_MISMATCH_REASON}\n"
            f"  session=s3  log_cost_usd=1.000000  no metric export — {_NO_METRIC_REASON}\n"
            f"{_COST_MARKING}\n"
        )

    def test_every_delta_export_is_added_never_only_the_last(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0})
        (tmp_path / "metrics.jsonl").write_text(
            "\n".join(
                _metrics_line(_cost_metric([_cost_datapoint("s1", part)])) for part in (0.25, 0.75)
            )
            + "\n"
        )
        stream_usage.check_main(["--dir", str(tmp_path)])
        assert "metric_cost_usd=1.000000\n" in capsys.readouterr().out

    def test_an_absent_metrics_file_is_not_a_present_empty_or_malformed_one(
        self, tmp_path, capsys
    ):
        self._logs(tmp_path, {"s1": 1.0})
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        absent = json.loads(capsys.readouterr().out)
        assert list(absent) == [
            "unreadable_metric_lines", "metrics_file", "skipped_lines", "metrics_check",
            "cost_estimate_note",
        ]
        assert absent["metrics_file"] == {"state": "nothing_received"}
        assert [entry["session_id"] for entry in absent["metrics_check"]] == ["s1"]
        assert absent["metrics_check"][0]["metric_cost_usd"] is None
        stream_usage.check_main(["--dir", str(tmp_path)])
        lines = capsys.readouterr().out.splitlines()
        # CP-358: absent names its absence and still checks the session, apart from empty.
        assert lines[0].startswith("No metrics.jsonl was found in ")
        assert "no metric export" in lines[2] and "skipped" not in lines[0]

        for content, unreadable in (("", 0), ("not json\n", 1)):
            (tmp_path / "metrics.jsonl").write_text(content)
            stream_usage.check_main(["--dir", str(tmp_path), "--json"])
            document = json.loads(capsys.readouterr().out)
            assert document["unreadable_metric_lines"] == unreadable, "CP-330"
            (entry,) = document["metrics_check"]
            assert entry == {
                "session_id": "s1",
                "log_cost_usd": 1.0,
                "metric_cost_usd": None,
                "within_tolerance": None,
                "possible_causes": _NO_METRIC_CAUSES,
            }

    def test_empty_logs_have_no_session_to_check(self, tmp_path, capsys):
        (tmp_path / "logs.jsonl").write_text("")
        (tmp_path / "metrics.jsonl").write_text("")
        stream_usage.check_main(["--dir", str(tmp_path)])
        assert capsys.readouterr().out == (
            "Totals check (claude_code.cost.usage vs summed cost_usd):\n"
            "  No sessions to check.\n"
        )

    def test_a_session_only_the_metrics_know_is_ignored(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("ghost", 5.0)])) + "\n"
        )
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        checks = json.loads(capsys.readouterr().out)["metrics_check"]
        assert [entry["session_id"] for entry in checks] == ["s1"]

    def test_check_json_carries_only_metrics_check_in_the_two_space_form(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0})
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        out = capsys.readouterr().out
        # CP-330, CP-353, CP-357: the counts and the state stand first, in the two-space form.
        entry = {
            "session_id": "s1", "log_cost_usd": 1.0, "metric_cost_usd": None,
            "within_tolerance": None, "possible_causes": _NO_METRIC_CAUSES,
        }
        expected = {
            "unreadable_metric_lines": 0,
            "metrics_file": {"state": "nothing_received"},
            "skipped_lines": 0,
            "metrics_check": [entry],
            "cost_estimate_note": _COST_MARKING,
        }
        assert out == json.dumps(expected, indent=2) + "\n"

    def test_check_text_equals_the_totals_block_of_agents(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 1.0, "s2": 0.5})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("s1", 1.0)])) + "\n"
        )
        stream_usage.main(["--dir", str(tmp_path)])
        block = _totals_block(capsys.readouterr().out)
        stream_usage.check_main(["--dir", str(tmp_path)])
        # CP-301: the check is the block, once its one framing line, the CP-288 marking, is out.
        *lines, marking = capsys.readouterr().out.splitlines(keepends=True)
        assert marking == _COST_MARKING + "\n"
        assert "".join(lines) == block

    def test_agents_checks_only_the_sessions_its_rows_show_against_their_whole_cost(
        self, tmp_path, capsys
    ):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s1", "query_source": "a",
                         "cost_usd": 0.25},
                        {"event.name": "api_request", "session.id": "s1", "query_source": "b",
                         "cost_usd": 0.75},
                        {"event.name": "api_request", "session.id": "s2", "query_source": "b",
                         "cost_usd": 1.0},
                    ]
                )
            ],
            [_metrics_line(_cost_metric([_cost_datapoint("s1", 1.0)]))],
        )
        stream_usage.main(["--dir", str(tmp_path), "--where", "query_source=a"])
        assert _totals_block(capsys.readouterr().out) == (
            "Totals check (claude_code.cost.usage vs summed cost_usd):\n"
            "  session=s1  log_cost_usd=1.000000  metric_cost_usd=1.000000\n"
        )


class TestBuildReaderExpansion:
    def test_build_reader_expands_directory_and_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert stream_usage.build_reader("~/stream").directory == tmp_path / "stream"
        assert stream_usage.build_reader(Path("~/stream")).directory == tmp_path / "stream"
        assert stream_usage.build_reader().directory == tmp_path / ".ka-telemetry"
        assert stream_usage.build_reader(tmp_path / "plain").directory == tmp_path / "plain"


# ── Labels read in the delivered form (CP-314 to CP-319) ────────────────────
#
# Claude Code delivers each character the turn-on encodes as one NUL and the last four hex digits
# of its code point, as measured on what it delivers. Every received value below is written out as a
# literal in that form, and every expected value as the typed text; neither is produced by the
# production decoder or by the turn-on's encoder.

NUL = "\x00"

# Every whitespace character the turn-on accepts (`str.isspace`), written out so the test does not
# take its own list from the code under test. The assertion below checks the list against Python's
# own predicate, so a character the language adds or drops is noticed.
WHITESPACE = (
    " \t\n\x0b\x0c\r\x1c\x1d\x1e\x1f\x85\xa0 "
    "           "
    "    　"
)
PUNCTUATION = '",;\\=%'


def _delivered(typed: str) -> str:
    """`typed` as Claude Code delivers it, written for this test and not shared with the code."""
    return "".join(
        f"{NUL}{ord(c):04X}" if c in WHITESPACE or c in PUNCTUATION else c for c in typed
    )


def _noted(
    value, *, session: str = "s-1", on: str = "resource", **record
) -> stream_usage.LogRecord:
    """One `api_request` carrying `team.note=value` on its resource (default) or on its record."""
    attrs = {"event.name": "api_request", "session.id": session, "cost_usd": 0.25, **record}
    resource: dict = {}
    (resource if on == "resource" else attrs)["team.note"] = value
    return _record(attrs, resource)


def _shown(value, *, on: str = "resource"):
    rows = stream_usage.agent_rows((_noted(value, on=on),), prefix="team")
    return rows[0].labels["team.note"]


def _report(records, *, as_json: bool = False):
    rows = stream_usage.agent_rows(records, prefix="team")
    show = stream_usage.render_json if as_json else stream_usage.render
    return show(rows, (), None, 0, None, prefix="team")


def _g_records() -> tuple[stream_usage.LogRecord, ...]:
    """Fixture G of the spec: three requests, one per session, each costing 0.25."""
    return (
        _noted("two" + NUL + "0020words", session="s-encoded"),
        _noted("plain", session="s-plain"),
        _noted(
            "a" + NUL + "002Cb" + NUL + "003Bc" + NUL + "0022d" + NUL + "003De",
            session="s-punct",
        ),
    )


def _sessions(rows) -> list[str]:
    return sorted(row.session_id for row in rows)


class TestLabelsReadAsTyped:
    def test_the_whitespace_list_is_the_whole_of_what_python_calls_whitespace(self):
        assert set(WHITESPACE) == {chr(i) for i in range(0x3001) if chr(i).isspace()}

    def test_g_text_label_lines_show_typed_values(self):
        lines = [
            line for line in _report(_g_records()).splitlines() if line.startswith("    team:")
        ]
        assert sorted(lines) == [
            '    team: team.note=a,b;c"d=e',
            "    team: team.note=plain",
            "    team: team.note=two words",
        ]

    def test_text_label_line_keeps_sorted_key_order(self):
        record = _noted("two" + NUL + "0020words", **{"team.zeta": "z", "team.alpha": "a"})
        assert "    team: team.alpha=a, team.note=two words, team.zeta=z\n" in _report((record,))

    def test_g_json_labels_parse_to_typed_values(self):
        payload = json.loads(_report(_g_records(), as_json=True))
        rows = {r["session_id"]: r["team"] for r in payload["rows"]}
        assert rows == {
            "s-encoded": {"team.note": "two words"},
            "s-plain": {"team.note": "plain"},
            "s-punct": {"team.note": 'a,b;c"d=e'},
        }

    @pytest.mark.parametrize("character", list(WHITESPACE) + list(PUNCTUATION))
    def test_every_encoded_character_round_trips(self, character):
        typed = f"a{character}b"
        assert _shown(_delivered(typed)) == typed
        assert _shown(NUL + f"{ord(character):04X}") == character

    @pytest.mark.parametrize(
        ("received", "typed"),
        [
            ("a" + NUL + "0020b", "a b"),
            ("a" + NUL + "0009b" + NUL + "000Ac" + NUL + "000Dd", "a\tb\nc\rd"),
            ("a" + NUL + "000Bb" + NUL + "000Cc", "a\x0bb\x0cc"),
            ("a" + NUL + "00A0b", "a\xa0b"),
            ("a" + NUL + "2003b", "a b"),
            ("a" + NUL + "3000b", "a　b"),
            (NUL + "0020" + NUL + "0020" + NUL + "002C", "  ,"),
            ("naïve", "naïve"),
            ("a\U0001f600b", "a\U0001f600b"),
        ],
    )
    def test_explicit_delivered_values_show_as_typed(self, received, typed):
        assert _shown(received) == typed

    def test_delivered_percent_and_percent_shaped_text_read_once(self):
        assert _shown("50" + NUL + "0025") == "50%"
        assert _shown(NUL + "0025000020") == "%000020"
        # The replacement of the first escape yields a NUL beside hex text; it is never rescanned.
        assert _shown(NUL + "0000" + "0020") == NUL + "0020"
        assert _shown(NUL + "0000" + NUL + "0000" + "0041") == NUL + NUL + "0041"

    @pytest.mark.parametrize(
        ("received", "shown"),
        [
            ("50" + NUL + "0025", "50%"),
            (NUL + "0025000020", "%000020"),
            (NUL + "00000020", NUL + "0020"),
            (NUL + "0000" + NUL + "00000041", NUL + NUL + "0041"),
        ],
    )
    def test_renderers_preserve_the_once_decoded_label(self, received, shown):
        records = (_noted(received),)

        text = _report(records)
        payload = json.loads(_report(records, as_json=True))

        assert f"    team: team.note={shown}\n" in text
        assert payload["rows"][0]["team"] == {"team.note": shown}

    @pytest.mark.parametrize(
        "received", ["plain", "100%", "two%000020words", "%00002c", "a%b%c", "%000025000020", ""]
    )
    def test_value_without_an_escape_is_shown_as_received(self, received):
        assert _shown(received) == received
        assert json.loads(_report((_noted(received),), as_json=True))["rows"][0]["team"] == {
            "team.note": received
        }
        assert f"    team: team.note={received}\n" in _report((_noted(received),))

    @pytest.mark.parametrize(
        ("received", "shown"),
        [
            (NUL, NUL),
            ("a" + NUL, "a" + NUL),
            (NUL + "002", NUL + "002"),
            (NUL + "002c", NUL + "002c"),
            (NUL + "D800x", NUL + "D800x"),
            (NUL + "D800", NUL + "D800"),
            (NUL + "DFFF", NUL + "DFFF"),
            (NUL + "zzzz", NUL + "zzzz"),
            (NUL + "D7FF", "퟿"),
            (NUL + "E000", ""),
            (NUL + "0000", NUL),
            (NUL + "FFFF", "￿"),
            (NUL + "D800" + NUL + "0020", NUL + "D800 "),
            (NUL + "002" + NUL + "0020", NUL + "002 "),
            (NUL + "0020" + NUL + "D800", " " + NUL + "D800"),
            (NUL + "0020" + NUL, " " + NUL),
        ],
    )
    def test_invalid_fragments_are_kept_and_the_scan_continues(self, received, shown):
        assert _shown(received) == shown
        row = json.loads(_report((_noted(received),), as_json=True))["rows"][0]
        assert row["team"] == {"team.note": shown}
        assert f"    team: team.note={shown}\n" in _report((_noted(received),))

    def test_number_and_boolean_labels_keep_their_type(self):
        for value in (7, True, 1.5):
            shown = _shown(value)
            assert shown == value and type(shown) is type(value)
            row = json.loads(_report((_noted(value),), as_json=True))["rows"][0]
            assert row["team"] == {"team.note": value}
            assert type(row["team"]["team.note"]) is type(value)
        assert "    team: team.note=7\n" in _report((_noted(7),))
        assert "    team: team.note=True\n" in _report((_noted(True),))

    def test_record_label_overrides_resource_label_before_decoding(self):
        record = _record(
            {
                "event.name": "api_request",
                "session.id": "s-1",
                "team.note": "two words",
                "team.only_record": "r" + NUL + "0020s",
            },
            {
                "team.note": "other" + NUL + "0020words",
                "team.only_resource": "x" + NUL + "0020y",
            },
        )
        labels = stream_usage.agent_rows((record,), prefix="team")[0].labels
        assert labels == {
            "team.note": "two words",
            "team.only_record": "r s",
            "team.only_resource": "x y",
        }

    def test_run_id_and_instance_labels_decode_like_any_other(self):
        record = _record(
            {"event.name": "api_request", "session.id": "s-1"},
            {"team.run_id": "run" + NUL + "0020one", "team.instance": "i" + NUL + "002Dx"},
        )
        labels = stream_usage.agent_rows((record,), prefix="team")[0].labels
        assert labels == {"team.run_id": "run one", "team.instance": "i-x"}

    def test_reading_does_not_mutate_the_received_maps(self):
        record = _record(
            {"event.name": "api_request", "session.id": "s-1", "team.k": "a" + NUL + "002Cb"},
            {"team.note": "x" + NUL + "0020y"},
        )
        before = (dict(record.record_attrs), dict(record.resource_attrs))
        for prefix in ("team", None, "other"):
            stream_usage.agent_rows((record,), prefix=prefix)
            stream_usage.agent_rows((record,), (("team.note", "x y"),), prefix=prefix)
            _report((record,))
        assert (record.record_attrs, record.resource_attrs) == before


class TestRowsGroupedByShownValue:
    def test_equivalent_shown_values_share_one_row(self):
        delivered = _noted(
            "two" + NUL + "0020words", model="m-a", input_tokens=3, output_tokens=1,
            cache_read_tokens=5, cache_creation_tokens=7, duration_ms=5,
        )
        typed = _noted(
            "two words", on="record", model="m-b", input_tokens=4, output_tokens=2,
            cache_read_tokens=6, cache_creation_tokens=8, duration_ms=6,
        )
        rows = stream_usage.agent_rows((delivered, typed), prefix="team")
        assert len(rows) == 1
        row = rows[0]
        assert row.request_count == 2
        assert row.labels == {"team.note": "two words"}
        assert (row.input_tokens, row.output_tokens) == (7, 3)
        assert (row.cache_read_tokens, row.cache_creation_tokens) == (11, 15)
        assert row.cost_usd == pytest.approx(0.5)
        assert row.duration_ms == 11
        assert row.models == ("m-a", "m-b")

    def test_rows_with_different_shown_values_or_keys_stay_apart(self):
        values = (_noted("a"), _noted("b"), _noted("a" + NUL + "0020b"), _noted("a b", on="record"))
        assert len(stream_usage.agent_rows(values, prefix="team")) == 3
        other_key = _record(
            {"event.name": "api_request", "session.id": "s-1", "team.other": "x"}
        )
        assert len(stream_usage.agent_rows((_noted("x"), other_key), prefix="team")) == 2
        unlabelled = _record({"event.name": "api_request", "session.id": "s-1"})
        assert len(stream_usage.agent_rows((_noted("x"), unlabelled), prefix="team")) == 2

    @pytest.mark.parametrize(
        "field", ["session.id", "workflow.run_id", "query_source", "agent.name"]
    )
    def test_rows_that_differ_in_an_identity_field_stay_apart(self, field):
        first = _noted("two words", **{field: "one"})
        second = _noted("two" + NUL + "0020words", **{field: "two"})
        assert len(stream_usage.agent_rows((first, second), prefix="team")) == 2


class TestWhereSelectsByShownValue:
    @pytest.mark.parametrize(
        ("operand", "session"),
        [
            ("two words", "s-encoded"),
            ('a,b;c"d=e', "s-punct"),
            ("plain", "s-plain"),
        ],
    )
    def test_where_selects_a_label_by_its_typed_value(self, operand, session):
        where = (("team.note", operand),)
        rows = stream_usage.agent_rows(_g_records(), where, prefix="team")
        assert _sessions(rows) == [session]
        selected = stream_usage.api_request_records(_g_records(), where, prefix="team")
        assert [r.record_attrs["session.id"] for r in selected] == [session]

    def test_where_does_not_match_the_received_form(self):
        received = "two" + NUL + "0020words"
        for operand in (received, "two%000020words"):
            rows = stream_usage.agent_rows(_g_records(), (("team.note", operand),), prefix="team")
            assert rows == ()

    def test_a_missing_key_never_matches_none(self):
        unlabelled = _record({"event.name": "api_request", "session.id": "s-1"})
        rows = stream_usage.agent_rows((unlabelled,), (("team.note", "None"),), prefix="team")
        assert rows == ()

    def test_repeated_where_predicates_all_apply(self):
        records = (
            _noted("two" + NUL + "0020words", session="s-1", speed="fast"),
            _noted("two" + NUL + "0020words", session="s-2", speed="slow"),
        )
        where = (("team.note", "two words"), ("speed", "fast"))
        assert _sessions(stream_usage.agent_rows(records, where, prefix="team")) == ["s-1"]

    def test_where_outside_the_prefix_compares_as_received(self):
        delivered = "two" + NUL + "0020words"
        records = _g_records()
        # No prefix: no attribute is a label.
        assert stream_usage.agent_rows(records, (("team.note", "two words"),)) == ()
        assert _sessions(stream_usage.agent_rows(records, (("team.note", delivered),))) == [
            "s-encoded"
        ]
        # A different prefix: `team.note` is not a label under it.
        assert _sessions(
            stream_usage.agent_rows(records, (("team.note", "plain"),), prefix="other")
        ) == ["s-plain"]
        assert stream_usage.agent_rows(
            records, (("team.note", "two words"),), prefix="other"
        ) == ()

    def test_the_prefix_boundary_is_the_prefix_and_a_dot_exactly(self):
        delivered = "a" + NUL + "0020b"
        near = {"teamx.note": delivered, "Team.note": delivered, "team_note": delivered}
        record = _record({"event.name": "api_request", "session.id": "s-1", **near})
        for key in near:
            assert stream_usage.agent_rows((record,), ((key, "a b"),), prefix="team") == ()
            assert len(stream_usage.agent_rows((record,), ((key, delivered),), prefix="team")) == 1

    def test_a_non_label_value_holding_a_delivered_escape_stays_raw(self):
        delivered = "a" + NUL + "0020b"
        record = _noted("x", query_source=delivered)
        assert stream_usage.agent_rows((record,), (("query_source", "a b"),), prefix="team") == ()
        assert len(
            stream_usage.agent_rows((record,), (("query_source", delivered),), prefix="team")
        ) == 1

    def test_numeric_and_boolean_filter_coercion_is_as_before(self):
        records = (_noted(7, session="s-int"), _noted(True, session="s-bool"))
        assert _sessions(
            stream_usage.agent_rows(records, (("team.note", "7"),), prefix="team")
        ) == ["s-int"]
        assert _sessions(
            stream_usage.agent_rows(records, (("team.note", "True"),), prefix="team")
        ) == ["s-bool"]


class TestCompareReadsShownValues:
    def test_compare_counts_a_label_by_its_typed_value(self):
        result = stream_usage.compare(
            _g_records(), "team.note", "two words", "plain", prefix="team"
        )
        assert result.a.value == "two words"
        assert result.a.run_count == 1
        assert result.a.totals.cost_usd == pytest.approx(0.25)
        assert result.b.run_count == 1

        punct = stream_usage.compare(
            _g_records(), "team.note", 'a,b;c"d=e', "plain", prefix="team"
        )
        assert punct.a.run_count == 1
        assert punct.a.value == 'a,b;c"d=e'

    def test_compare_does_not_match_the_received_form_of_a_label(self):
        result = stream_usage.compare(
            _g_records(), "team.note", "two" + NUL + "0020words", "plain", prefix="team"
        )
        assert result.a is None
        assert result.b is not None

    def test_compare_counts_a_run_by_the_shown_run_id(self):
        records = (
            _noted("x", session="s-1", on="resource", **{"team.run_id": "run one"}),
            _record(
                {"event.name": "api_request", "session.id": "s-2", "cost_usd": 0.25,
                 "team.arm": "A"},
                {"team.run_id": "run" + NUL + "0020one"},
            ),
            _record(
                {"event.name": "api_request", "session.id": "s-3", "cost_usd": 0.25,
                 "team.arm": "A", "team.run_id": "run one"},
                {},
            ),
        )
        result = stream_usage.compare(records, "team.arm", "A", "B", prefix="team")
        assert result.a.run_count == 1
        assert result.a.totals.cost_usd == pytest.approx(0.5)

    def test_record_run_id_outranks_the_resource_and_session_is_the_fallback(self):
        records = (
            _record(
                {"event.name": "api_request", "session.id": "s-1", "team.arm": "A",
                 "team.run_id": "run one"},
                {"team.run_id": "ignored" + NUL + "0020x"},
            ),
            _record(
                {"event.name": "api_request", "session.id": "s-2", "team.arm": "A"},
                {},
            ),
            _record(
                {"event.name": "api_request", "session.id": "s-2", "team.arm": "A"},
                {},
            ),
        )
        result = stream_usage.compare(records, "team.arm", "A", "B", prefix="team")
        assert result.a.run_count == 2

    def test_repeated_where_constrains_both_sides(self):
        records = (
            _noted("two" + NUL + "0020words", session="s-1", speed="fast"),
            _noted("two" + NUL + "0020words", session="s-2", speed="slow", cost_usd=9.0),
            _noted("plain", session="s-3", speed="fast", cost_usd=7.0),
        )
        where = (("speed", "fast"), ("team.note", "two words"))
        result = stream_usage.compare(
            records, "team.note", "two words", "plain", where, prefix="team"
        )
        assert result.a.run_count == 1
        assert result.a.totals.cost_usd == pytest.approx(0.25)
        assert result.b is None

    def test_compare_outside_the_prefix_stays_raw(self):
        delivered = "two" + NUL + "0020words"
        records = _g_records()
        for prefix in (None, "other"):
            raw = stream_usage.compare(records, "team.note", delivered, "plain", prefix=prefix)
            assert raw.a.run_count == 1
            typed = stream_usage.compare(records, "team.note", "two words", "plain", prefix=prefix)
            assert typed.a is None
        # A non-label attribute holding a delivered-shaped value is matched as received.
        record = _noted("x", query_source="a" + NUL + "0020b")
        assert stream_usage.compare(
            (record,), "query_source", "a" + NUL + "0020b", "z", prefix="team"
        ).a.run_count == 1
        assert stream_usage.compare((record,), "query_source", "a b", "z", prefix="team").a is None

    def test_a_request_without_the_attribute_still_belongs_to_none(self):
        unlabelled = _record({"event.name": "api_request", "session.id": "s-1", "cost_usd": 1.0})
        result = stream_usage.compare((unlabelled,), "team.note", "None", "x", prefix="team")
        assert result.a.run_count == 1

    def test_zero_mean_difference_and_ratio_keep_their_arithmetic(self):
        records = (
            _noted("one", session="s-1", cost_usd=0.0),
            _noted("two" + NUL + "0020words", session="s-2", cost_usd=0.5),
        )
        result = stream_usage.compare(records, "team.note", "one", "two words", prefix="team")
        assert result.diff == pytest.approx(0.5)
        assert result.ratio is None
        same = stream_usage.compare(records, "team.note", "one", "one", prefix="team")
        assert same.diff == 0 and same.a.run_count == same.b.run_count == 1


class TestLibraryContractForShownValues:
    def test_raw_attr_and_existing_two_argument_selection_are_unchanged(self):
        record = _noted("two" + NUL + "0020words")
        assert stream_usage._attr(record, "team.note") == "two" + NUL + "0020words"
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.record_attrs = {}  # type: ignore[misc]
        records = _g_records()
        received = (("team.note", "two" + NUL + "0020words"),)
        assert [r.record_attrs["session.id"] for r in stream_usage.api_request_records(
            records, received
        )] == ["s-encoded"]
        assert stream_usage.api_request_records(records, (("team.note", "two words"),)) == ()

    def test_prefix_keyword_selects_by_shown_value(self):
        records = _g_records()
        where = (("team.note", "two words"),)
        selected = stream_usage.api_request_records(records, where, prefix="team")
        assert [r.record_attrs["session.id"] for r in selected] == ["s-encoded"]
        assert _sessions(stream_usage.agent_rows(records, where, prefix="team")) == ["s-encoded"]
        result = stream_usage.compare(
            records, "team.note", "two words", "plain", where, prefix="team"
        )
        assert result.a.run_count == 1 and result.b is None

    def test_prefix_keyword_is_keyword_only_and_defaults_to_no_prefix(self):
        with pytest.raises(TypeError):
            stream_usage.api_request_records(_g_records(), (), "team")  # type: ignore[misc]
        records = _g_records()
        assert stream_usage.api_request_records(records) == stream_usage.api_request_records(
            records, (), prefix=None
        )


# ── The completed-agents section follows `--where` (CP-320) ──────────────────────────────

NOTHING_READ_LINE = "No subagent_completed records read."


def _completed(session: str | None, agent_type: str, **extra) -> dict:
    attrs: dict = {"event.name": "subagent_completed", "agent_type": agent_type, **extra}
    if session is not None:
        attrs["session.id"] = session
    return attrs


def _g_stream(tmp_path: Path, *, metrics: bool = False) -> Path:
    """Fixture G written to `tmp_path`: three requests in three sessions, each with the delivered
    label `team.note` on its resource, and two completed agents — `s-encoded` and `s-plain`."""
    notes = {
        "s-encoded": "two" + NUL + "0020words",
        "s-plain": "plain",
        "s-punct": "a" + NUL + "002Cb" + NUL + "003Bc" + NUL + "0022d" + NUL + "003De",
    }
    lines = [
        _logs_line(
            [{"event.name": "api_request", "session.id": session, "cost_usd": 0.25}],
            {"team.note": note},
        )
        for session, note in notes.items()
    ]
    lines.append(
        _logs_line([_completed("s-encoded", "general-purpose"), _completed("s-plain", "Explore")])
    )
    metric_lines = None
    if metrics:
        points = [_cost_datapoint(session, 0.25) for session in notes]
        metric_lines = [_metrics_line(_cost_metric(points))]
    return _write_stream(tmp_path, lines, metric_lines)


def _agents_out(tmp_path: Path, capsys, *arguments: str) -> str:
    assert stream_usage.main(["--dir", str(tmp_path), *arguments]) == 0
    return capsys.readouterr().out


def _completed_section(text: str) -> list[str]:
    """The lines under the coverage note, up to the blank line that ends the section."""
    lines = text.splitlines()
    start = lines.index(stream_usage.SUBAGENT_COVERAGE_NOTE) + 1
    end = lines.index("", start)
    return lines[start:end]


def _completed_sessions(text: str) -> list[str]:
    return sorted(
        line.split()[0].removeprefix("session=")
        for line in _completed_section(text)
        if line.startswith("  session=")
    )


class TestCompletedAgentsFollowWhere:
    @pytest.mark.parametrize("where", ["team.note=plain", "session.id=s-plain"])
    def test_where_narrows_completed_agents_to_the_shown_sessions(self, tmp_path, capsys, where):
        _g_stream(tmp_path, metrics=True)

        arguments = ["--where", where, "--prefix", "team"]
        text = _agents_out(tmp_path, capsys, *arguments)
        payload = json.loads(_agents_out(tmp_path, capsys, *arguments, "--json"))

        assert _completed_sessions(text) == ["s-plain"]
        assert [r["session_id"] for r in payload["subagent_rows"]] == ["s-plain"]
        assert [c["session_id"] for c in payload["metrics_check"]] == ["s-plain"]
        assert "session=s-plain agent_type=Explore" in text
        assert "s-encoded" not in text

    def test_where_on_a_label_reaches_the_completed_section_by_shown_value(self, tmp_path, capsys):
        _g_stream(tmp_path)

        text = _agents_out(tmp_path, capsys, "--prefix", "team", "--where", "team.note=two words")

        assert _completed_sessions(text) == ["s-encoded"]

    def test_the_selected_sessions_agree_with_the_totals_check(self, tmp_path):
        _g_stream(tmp_path, metrics=True)

        report = stream_usage.read_report(
            stream_usage.build_reader(tmp_path), where=(("session.id", "s-plain"),)
        )

        assert [r.session_id for r in report.subagent_rows] == ["s-plain"]
        assert [c.session_id for c in report.metrics_check] == ["s-plain"]

    def test_a_filter_narrower_than_a_session_keeps_the_whole_session(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s1", "query_source": "a",
                         "cost_usd": 0.25},
                        {"event.name": "api_request", "session.id": "s1", "query_source": "b",
                         "cost_usd": 0.75},
                        _completed("s1", "general-purpose"),
                        _completed("s1", "Explore"),
                        _completed("s2", "Plan"),
                    ]
                )
            ],
            [_metrics_line(_cost_metric([_cost_datapoint("s1", 1.0)]))],
        )

        text = _agents_out(tmp_path, capsys, "--where", "query_source=a")

        assert [line.split()[1] for line in _completed_section(text) if "session=" in line] == [
            "agent_type=Explore",
            "agent_type=general-purpose",
        ]
        assert _completed_sessions(text) == ["s1", "s1"]
        assert "session=s1  log_cost_usd=1.000000  metric_cost_usd=1.000000" in text

    def test_a_missing_session_identity_is_kept_or_dropped_by_the_same_membership_test(
        self, tmp_path
    ):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "query_source": "a", "cost_usd": 0.25},
                        {"event.name": "api_request", "session.id": "s1", "query_source": "b",
                         "cost_usd": 0.25},
                        _completed(None, "general-purpose"),
                        _completed("s1", "Explore"),
                    ]
                )
            ],
        )
        reader = stream_usage.build_reader(tmp_path)

        no_identity = stream_usage.read_report(reader, where=(("query_source", "a"),))
        with_identity = stream_usage.read_report(reader, where=(("query_source", "b"),))

        assert [r.session_id for r in no_identity.subagent_rows] == [None]
        assert [r.session_id for r in with_identity.subagent_rows] == ["s1"]

    def test_no_filter_keeps_every_completed_session(self, tmp_path, capsys):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s-encoded", "cost_usd": 0.25},
                        _completed("s-encoded", "general-purpose"),
                        _completed("s-plain", "Explore"),
                        _completed("s-only-completed", "Plan"),
                    ]
                )
            ],
        )

        text = _agents_out(tmp_path, capsys)
        payload = json.loads(_agents_out(tmp_path, capsys, "--json"))

        assert _completed_sessions(text) == ["s-encoded", "s-only-completed", "s-plain"]
        assert len(payload["subagent_rows"]) == 3

    def test_no_match_filter_says_nothing_was_selected_rather_than_nothing_read(
        self, tmp_path, capsys
    ):
        _g_stream(tmp_path)
        unfiltered_json = json.loads(_agents_out(tmp_path, capsys, "--json"))

        filtered = ("--where", "session.id=nothing")
        text = _agents_out(tmp_path, capsys, *filtered)
        payload = json.loads(_agents_out(tmp_path, capsys, *filtered, "--json"))

        assert text.startswith("0 agent row(s)")
        section = _completed_section(text)
        assert NOTHING_READ_LINE not in text
        assert len(section) == 1 and section[0].strip()
        assert stream_usage.SUBAGENT_COVERAGE_NOTE in text
        assert payload["subagent_rows"] == []
        assert set(payload) == set(unfiltered_json)

    def test_a_session_with_no_completed_row_is_filtered_empty_when_others_have_them(
        self, tmp_path, capsys
    ):
        _g_stream(tmp_path)

        text = _agents_out(tmp_path, capsys, "--prefix", "team", "--where", "session.id=s-punct")

        assert text.startswith("1 agent row(s)")
        section = _completed_section(text)
        assert NOTHING_READ_LINE not in text
        assert len(section) == 1 and section[0].strip() and "session=" not in section[0]

    @pytest.mark.parametrize("where", [[], ["--where", "session.id=s1"], ["--where", "speed=zz"]])
    def test_stream_without_completed_records_keeps_the_original_absence_line(
        self, tmp_path, capsys, where
    ):
        _write_stream(
            tmp_path, [_logs_line([{"event.name": "api_request", "session.id": "s1"}])]
        )

        text = _agents_out(tmp_path, capsys, *where)

        assert _completed_section(text) == [NOTHING_READ_LINE]

    def test_the_filtered_out_flag_is_true_exactly_when_filtering_emptied_a_non_empty_section(
        self, tmp_path
    ):
        _g_stream(tmp_path)
        reader = stream_usage.build_reader(tmp_path)

        emptied = stream_usage.read_report(reader, where=(("session.id", "nothing"),))
        narrowed = stream_usage.read_report(reader, where=(("session.id", "s-plain"),))
        unfiltered = stream_usage.read_report(reader)

        assert emptied.subagents_filtered_out is True
        assert narrowed.subagents_filtered_out is False
        assert unfiltered.subagents_filtered_out is False
        assert stream_usage.subagent_rows(
            stream_usage.read_log_lines(
                (tmp_path / "logs.jsonl").read_text().splitlines()
            ).records
        ), "subagent_rows stays the whole-stream aggregator"

    def test_stream_report_and_render_defaults_keep_old_calls_working(self):
        report = stream_usage.StreamReport((), (), None, 0, None)
        explicit = stream_usage.StreamReport((), (), None, 0, None, 0, False)
        sub_row = stream_usage.SubagentRow("s", "Explore", 1, 0, 0)
        selected = stream_usage.StreamReport((), (sub_row,), None, 0, None, 0, False)

        assert report.subagents_filtered_out is False
        assert explicit.subagents_filtered_out is False
        assert selected.subagents_filtered_out is False
        old_call = stream_usage.render((), (), None, 0, None)
        assert NOTHING_READ_LINE in old_call
        assert stream_usage.render((), (), None, 0, None, subagents_filtered_out=False) == old_call

    def test_render_with_the_flag_replaces_the_absence_line_and_keeps_the_note(self):
        text = stream_usage.render((), (), None, 0, None, subagents_filtered_out=True)

        assert NOTHING_READ_LINE not in text
        section = _completed_section(text)
        assert len(section) == 1 and section[0].strip()
        assert stream_usage.SUBAGENT_COVERAGE_NOTE in text



class TestPossibleCauses:
    """CP-338 to CP-344: a session the check cannot settle carries the causes it cannot tell
    apart — on the typed result, in the text line and in the JSON entry, one list for all."""

    def _row(self, session_id: str, cost_usd: float) -> stream_usage.AgentRow:
        return TestMetricsCheck._row(self, session_id, cost_usd)

    def _points(self, session_id: str, value: float):
        return stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_cost_datapoint(session_id, value)]))]
        )

    def _logs(self, directory: Path, sessions: dict[str, float]) -> None:
        _write_stream(
            directory,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": session, "cost_usd": cost}
                        for session, cost in sessions.items()
                    ]
                )
            ],
        )

    def _three_states(self, directory: Path) -> None:
        self._logs(directory, {"s-none": 0.5, "s-off": 1.0, "s-ok": 1.0})
        (directory / "metrics.jsonl").write_text(
            _metrics_line(
                _cost_metric([_cost_datapoint("s-off", 2.0), _cost_datapoint("s-ok", 1.0)])
            )
            + "\n"
        )

    def test_no_metric_session_line_names_three_causes_in_order_in_check_and_agents(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path)])
        check = capsys.readouterr().out.splitlines()
        stream_usage.main(["--dir", str(tmp_path)])
        agents = _totals_block(capsys.readouterr().out).splitlines()
        line = f"  session=s-none  log_cost_usd=0.500000  no metric export — {_NO_METRIC_REASON}"
        assert check.count(line) == 1 and agents.count(line) == 1
        assert "MISMATCH" not in line
        assert [row for row in check if row.startswith("  session=s-none")] == [line]
        assert "or may still be running" not in "\n".join(check + agents)

    def test_no_metric_entry_carries_three_causes_with_null_figures_in_both_json_forms(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        from_check = json.loads(capsys.readouterr().out)["metrics_check"]
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        from_agents = json.loads(capsys.readouterr().out)["metrics_check"]
        for entries in (from_check, from_agents):
            (entry,) = [e for e in entries if e["session_id"] == "s-none"]
            assert entry["possible_causes"] == _NO_METRIC_CAUSES
            assert entry["metric_cost_usd"] is None and entry["within_tolerance"] is None

    def test_outside_one_percent_line_names_three_causes_in_order_after_mismatch(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path)])
        check = capsys.readouterr().out.splitlines()
        stream_usage.main(["--dir", str(tmp_path)])
        agents = _totals_block(capsys.readouterr().out).splitlines()
        line = (
            "  session=s-off  log_cost_usd=1.000000  metric_cost_usd=2.000000  MISMATCH >1% — "
            f"{_MISMATCH_REASON}"
        )
        assert check.count(line) == 1 and agents.count(line) == 1

    def test_outside_one_percent_entry_carries_three_causes_in_both_json_forms(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        from_check = json.loads(capsys.readouterr().out)["metrics_check"]
        stream_usage.main(["--dir", str(tmp_path), "--json"])
        from_agents = json.loads(capsys.readouterr().out)["metrics_check"]
        for entries in (from_check, from_agents):
            (entry,) = [e for e in entries if e["session_id"] == "s-off"]
            assert entry["possible_causes"] == _MISMATCH_CAUSES
            assert entry["within_tolerance"] is False

    def test_within_one_percent_line_is_byte_identical_and_entry_has_empty_list(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path)])
        assert "  session=s-ok  log_cost_usd=1.000000  metric_cost_usd=1.000000\n" in (
            capsys.readouterr().out
        )
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        (entry,) = [
            e for e in json.loads(capsys.readouterr().out)["metrics_check"]
            if e["session_id"] == "s-ok"
        ]
        assert entry["possible_causes"] == []

    def test_possible_causes_never_absent_and_never_null(self, tmp_path, capsys):
        self._three_states(tmp_path)
        for command, argv in (
            (stream_usage.check_main, ["--dir", str(tmp_path), "--json"]),
            (stream_usage.main, ["--dir", str(tmp_path), "--json"]),
        ):
            command(argv)
            for entry in json.loads(capsys.readouterr().out)["metrics_check"]:
                assert isinstance(entry["possible_causes"], list)

    def test_every_json_cause_string_appears_word_for_word_in_the_sessions_text_line(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path)])
        lines = {
            line.split()[0]: line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("  session=")
        }
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        for entry in json.loads(capsys.readouterr().out)["metrics_check"]:
            line = lines[f"session={entry['session_id']}"]
            for cause in entry["possible_causes"]:
                assert cause in line

    def test_json_without_possible_causes_keeps_the_other_members_in_name_value_and_order(
        self, tmp_path, capsys
    ):
        self._three_states(tmp_path)
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        document = json.loads(capsys.readouterr().out)
        assert list(document) == [
            "unreadable_metric_lines", "metrics_file", "skipped_lines", "metrics_check",
            "cost_estimate_note",
        ]
        assert [list(entry) for entry in document["metrics_check"]] == [
            ["session_id", "log_cost_usd", "metric_cost_usd", "within_tolerance", "possible_causes"]
        ] * 3
        stripped = [
            {k: v for k, v in entry.items() if k != "possible_causes"}
            for entry in document["metrics_check"]
        ]
        assert stripped == [
            {"session_id": "s-none", "log_cost_usd": 0.5, "metric_cost_usd": None,
             "within_tolerance": None},
            {"session_id": "s-off", "log_cost_usd": 1.0, "metric_cost_usd": 2.0,
             "within_tolerance": False},
            {"session_id": "s-ok", "log_cost_usd": 1.0, "metric_cost_usd": 1.0,
             "within_tolerance": True},
        ]

    def test_direct_four_argument_check_carries_possible_causes_in_all_three_states(self):
        none = stream_usage.SessionMetricsCheck("s", 0.5, None, None)
        off = stream_usage.SessionMetricsCheck("s", 1.0, 2.0, False)
        ok = stream_usage.SessionMetricsCheck(
            session_id="s", log_cost_usd=1.0, metric_cost_usd=1.0, within_tolerance=True
        )
        assert none.possible_causes == tuple(_NO_METRIC_CAUSES)
        assert off.possible_causes == tuple(_MISMATCH_CAUSES)
        assert ok.possible_causes == ()
        assert list(dataclasses.asdict(none)) == [
            "session_id", "log_cost_usd", "metric_cost_usd", "within_tolerance", "possible_causes",
        ]
        assert dataclasses.asdict(ok)["possible_causes"] == ()

    def test_possible_causes_follow_within_tolerance_for_a_custom_tolerance(self):
        rows = (self._row("s", 100.0),)
        points = self._points("s", 103.0)
        (loose,) = stream_usage.metrics_check(rows, points, tolerance=0.05)
        (tight,) = stream_usage.metrics_check(rows, points)
        assert loose.within_tolerance is True and loose.possible_causes == ()
        assert tight.within_tolerance is False and tight.possible_causes == tuple(_MISMATCH_CAUSES)

    def test_a_request_total_of_zero_against_a_nonzero_metric_is_outside_one_percent_with_causes(
        self,
    ):
        (check,) = stream_usage.metrics_check((self._row("s", 0.0),), self._points("s", 0.5))
        assert check.within_tolerance is False
        assert check.possible_causes == tuple(_MISMATCH_CAUSES)
        line = stream_usage.render_checks((check,)).splitlines()[1]
        assert line == (
            f"  session=s  log_cost_usd=0.000000  metric_cost_usd=0.500000  MISMATCH >1% — "
            f"{_MISMATCH_REASON}"
        )

    @pytest.mark.parametrize(
        "metrics",
        [
            _metrics_line(_cost_metric([_cost_datapoint("other", 0.5)])),
            _metrics_line(
                {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "claude_code.token.usage",
                                            "sum": {"dataPoints": [_cost_datapoint("s1", 9)]},
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ),
            "",
            "not json",
        ],
        ids=["only-another-session", "only-another-metric", "empty", "malformed"],
    )
    def test_a_session_absent_from_a_metrics_file_gets_the_no_metric_causes(
        self, tmp_path, capsys, metrics
    ):
        self._logs(tmp_path, {"s1": 0.5})
        (tmp_path / "metrics.jsonl").write_text(metrics + "\n")
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        (entry,) = json.loads(capsys.readouterr().out)["metrics_check"]
        assert entry["possible_causes"] == _NO_METRIC_CAUSES

    def test_a_present_zero_valued_metric_is_a_metric_not_an_absence(self, tmp_path, capsys):
        self._logs(tmp_path, {"s1": 0.0, "s2": 1.0})
        (tmp_path / "metrics.jsonl").write_text(
            _metrics_line(_cost_metric([_cost_datapoint("s1", 0.0), _cost_datapoint("s2", 0.0)]))
            + "\n"
        )
        stream_usage.check_main(["--dir", str(tmp_path), "--json"])
        zero_zero, zero_metric = json.loads(capsys.readouterr().out)["metrics_check"]
        assert zero_zero["possible_causes"] == []
        assert zero_metric["possible_causes"] == _MISMATCH_CAUSES


class TestPossibleCausesHardening:
    """CP-338 to CP-342 at their edges: the bound of the tolerance, zero totals, and the totals
    check's order, summation and exclusions — now with the causes asserted beside them."""

    def _row(self, session_id: str, cost_usd: float) -> stream_usage.AgentRow:
        return TestMetricsCheck._row(self, session_id, cost_usd)

    def _points(self, session_id: str, value: float):
        return stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_cost_datapoint(session_id, value)]))]
        )

    @pytest.mark.parametrize(
        "log, metric, causes",
        [
            (100.0, 101.0, ()),  # exactly 1% above
            (100.0, 99.0, ()),  # exactly 1% below
            (100.0, 100.99, ()),  # just inside, above
            (100.0, 99.01, ()),  # just inside, below
            (100.0, 101.01, tuple(_MISMATCH_CAUSES)),  # just above the bound
            (100.0, 98.99, tuple(_MISMATCH_CAUSES)),  # just beyond the bound, below
        ],
    )
    def test_causes_at_exactly_one_percent_just_inside_and_just_above_the_bound(
        self, log, metric, causes
    ):
        (check,) = stream_usage.metrics_check((self._row("s", log),), self._points("s", metric))
        assert check.possible_causes == causes

    def test_both_totals_zero_is_within_one_percent_with_no_causes(self):
        (check,) = stream_usage.metrics_check((self._row("s", 0.0),), self._points("s", 0.0))
        assert check.within_tolerance is True and check.possible_causes == ()
        assert "—" not in stream_usage.render_checks((check,)).splitlines()[1]

    def test_totals_check_keeps_session_id_order_all_export_summation_and_metric_only_exclusion(
        self,
    ):
        rows = (
            self._row("s-b", 0.25),
            self._row("s-a", 1.0),
            self._row("s-b", 0.75),
            self._row("s-c", 1.0),
        )
        points = stream_usage.read_metric_lines(
            [
                _metrics_line(
                    _cost_metric([_cost_datapoint("s-b", 0.5), _cost_datapoint("s-a", 3)])
                ),
                _metrics_line(
                    _cost_metric([_cost_datapoint("s-b", 0.5), _cost_datapoint("ghost", 9)])
                ),
            ]
        )
        checks = stream_usage.metrics_check(rows, points)
        assert [c.session_id for c in checks] == ["s-a", "s-b", "s-c"]
        assert checks[1].metric_cost_usd == pytest.approx(1.0) and checks[1].possible_causes == ()
        assert checks[0].possible_causes == tuple(_MISMATCH_CAUSES)
        assert checks[2].possible_causes == tuple(_NO_METRIC_CAUSES)


# ── `metrics.jsonl`'s state is carried and named (CP-351 to CP-360) ────────────────────────


def _metrics_unopenable_statement(directory: Path, reason: str) -> str:
    """CP-351's statement, written out from `change_text.md`, not read from the product."""
    return (
        f"There is a metrics.jsonl at {directory}, but ka-telemetry could not read it: {reason}. "
        "No session was checked in the totals check. "
        "Run this command again once metrics.jsonl can be read."
    )


def _metrics_undetermined_statement(directory: Path, reason: str) -> str:
    """CP-352's statement, written out from `change_text.md`."""
    return (
        f"In {directory}, ka-telemetry could not read metrics.jsonl: {reason}. "
        "It could not tell whether metrics.jsonl is there either. "
        "No session was checked in the totals check. Run this command again once "
        "ka-telemetry can read metrics.jsonl or tell that it is not there."
    )


def _metrics_absent_line(directory: Path) -> str:
    """CP-358's absence line, written out from `change_text.md`."""
    return (
        f"No metrics.jsonl was found in {directory}, "
        "so no session has a cost metric to check against."
    )


_SESSION_WITH_NO_METRIC = "7931ae7c-d999-4286-b091-261680002258"
_SKIPPED_TEXT = "No metrics.jsonl found — totals check skipped."
_METRICS_STATES = ("read", "absent", "unopenable", "uninspectable")


def _stored_set_without_metrics(directory: Path) -> Path:
    _stored_set_copy(directory)
    (directory / "metrics.jsonl").unlink()
    return directory


def _stored_set_with_metrics_as(directory: Path, state: str, seal) -> Path:
    """The stored set with `metrics.jsonl` in one of CP-353's four states."""
    if state == "read":
        return _stored_set_copy(directory, extra_metrics=["not json {"])
    _stored_set_without_metrics(directory)
    metrics = directory / "metrics.jsonl"
    if state == "unopenable":
        metrics.write_text("secret\n")
        seal(metrics)
    elif state == "uninspectable":
        inner = directory.parent / f"{directory.name}-inner"
        inner.mkdir()
        (inner / "metrics.jsonl").write_text("secret\n")
        seal(inner)
        metrics.symlink_to(inner / "metrics.jsonl")
    return directory


@pytest.fixture
def metrics_is_a_directory(tmp_path: Path) -> Path:
    directory = _stored_set_without_metrics(tmp_path / "dir-metrics")
    (directory / "metrics.jsonl").mkdir()
    return directory


@pytest.fixture
def metrics_mode_000(tmp_path: Path, seal) -> Path:
    return _stored_set_with_metrics_as(tmp_path / "refused-metrics", "unopenable", seal)


@pytest.fixture
def metrics_link_into_mode_000_directory(tmp_path: Path, seal) -> Path:
    return _stored_set_with_metrics_as(tmp_path / "linked-metrics", "uninspectable", seal)


def _text_of(run, directory: Path, capsys, *options: str) -> str:
    assert run(["--dir", str(directory), *options]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "Traceback" not in captured.out
    return captured.out


def _json_of(run, directory: Path, capsys, *options: str) -> dict:
    return json.loads(_text_of(run, directory, capsys, "--json", *options))


def _session_lines(text: str) -> list[str]:
    """The totals check's session lines: the ones that say what the metric did."""
    return [line for line in text.splitlines() if "no metric export" in line]


class TestMetricsFileUnreadable:
    """CP-351 to CP-353: a `metrics.jsonl` that is there and cannot be read, or that cannot be
    told from absent, is named — one statement in the totals block, one `metrics_file` member."""

    @_ENTRIES
    def test_cp351_metrics_mode_000_check_and_agents_name_the_unreadable_file(
        self, entry, metrics_mode_000, capsys
    ):
        text = _text_of(getattr(stream_usage, entry), metrics_mode_000, capsys)

        statement = _metrics_unopenable_statement(metrics_mode_000, "Permission denied")
        assert text.splitlines().count(statement) == 1
        for absent in (
            _SKIPPED_TEXT, "No metrics.jsonl found", "Totals check (",
            "No sessions to check.", "no metric export",
        ):
            assert absent not in text
        if entry == "check_main":
            assert text == _CHECK_LOGS_COUNT.format(count=1) + "\n" + statement + "\n"
            assert _COST_MARKING not in text
        else:
            assert "agent row(s)" in text and _COST_MARKING in text

    def test_cp351_metrics_that_is_a_directory_gives_is_a_directory(
        self, metrics_is_a_directory, capsys
    ):
        """This one runs as root as well: a directory cannot be read as a file whoever asks."""
        for run in (stream_usage.check_main, stream_usage.main):
            text = _text_of(run, metrics_is_a_directory, capsys)
            statement = _metrics_unopenable_statement(metrics_is_a_directory, "Is a directory")
            assert statement in text.splitlines()
        assert stream_usage.read_report_result(
            stream_usage.build_reader(metrics_is_a_directory)
        ).report.metrics_state is stream_usage.ReadState.UNOPENABLE

    @_ENTRIES
    @pytest.mark.parametrize("json_flag", [[], ["--json"]])
    def test_cp352_metrics_link_into_mode_000_directory_claims_neither_presence_nor_absence(
        self, entry, json_flag, metrics_link_into_mode_000_directory, capsys
    ):
        directory = metrics_link_into_mode_000_directory
        run = getattr(stream_usage, entry)

        text = _text_of(run, directory, capsys, *json_flag)

        statement = _metrics_undetermined_statement(directory, "Permission denied")
        if json_flag:
            member = json.loads(text)["metrics_file"]
            assert member == {
                "state": "uninspectable", "reason": "Permission denied", "message": statement,
            }
        else:
            assert statement in text.splitlines()
        for claim in ("There is a metrics.jsonl", "No metrics.jsonl was found", "no metrics.jsonl"):
            assert claim not in text

    @_ENTRIES
    def test_cp352_undetermined_metrics_totals_block_is_the_one_statement(
        self, entry, metrics_link_into_mode_000_directory, capsys
    ):
        """CP-352 takes CP-351's block: the one statement, with no heading, no session line, no
        `No sessions to check.` line, and in `check` no cost marking, since no session is shown."""
        directory = metrics_link_into_mode_000_directory
        text = _text_of(getattr(stream_usage, entry), directory, capsys)

        statement = _metrics_undetermined_statement(directory, "Permission denied")
        assert text.splitlines().count(statement) == 1
        for absent in (
            _SKIPPED_TEXT, "No metrics.jsonl found", "Totals check (",
            "No sessions to check.", "no metric export",
        ):
            assert absent not in text
        if entry == "check_main":
            assert text == _CHECK_LOGS_COUNT.format(count=1) + "\n" + statement + "\n"
            assert _COST_MARKING not in text
        else:
            assert "agent row(s)" in text and _COST_MARKING in text

    @_ENTRIES
    def test_cp353_json_metrics_file_member_for_the_failed_states(
        self, entry, metrics_is_a_directory, metrics_link_into_mode_000_directory, capsys
    ):
        run = getattr(stream_usage, entry)
        cases = (
            (metrics_is_a_directory, "unopenable", "Is a directory", _metrics_unopenable_statement),
            (
                metrics_link_into_mode_000_directory, "uninspectable", "Permission denied",
                _metrics_undetermined_statement,
            ),
        )
        for directory, state, reason, statement in cases:
            text = _text_of(run, directory, capsys)
            document = _json_of(run, directory, capsys)

            member = document["metrics_file"]
            assert list(member) == ["state", "reason", "message"]
            assert member["state"] == state and member["reason"] == reason
            assert member["message"] in text.splitlines()
            assert member["message"] == statement(directory, reason)
            assert "metrics_check" not in document and "cost_estimate_note" not in document or (
                entry == "main" and "metrics_check" not in document
            )
            assert list(document)[:2] == ["unreadable_metric_lines", "metrics_file"]
            assert document["unreadable_metric_lines"] == 0
            report = stream_usage.read_report(stream_usage.build_reader(directory))
            old = json.loads(
                stream_usage.render_json(
                    report.rows, report.subagent_rows, None, report.skipped_lines, report.compare,
                    skipped_metric_lines=0,
                )
                if entry == "main"
                else stream_usage.render_checks_json(
                    None, skipped_metric_lines=0, skipped_lines=report.skipped_lines
                )
            )
            old.pop("metrics_check")
            # Every other member keeps its name and its value; only `metrics_file` is new.
            assert {k: v for k, v in document.items() if k != "metrics_file"} == old
            if entry == "check_main":
                assert list(document) == [
                    "unreadable_metric_lines", "metrics_file", "skipped_lines",
                ]
            else:
                assert list(document) == [
                    "unreadable_metric_lines", "metrics_file",
                    *(m for m in _OLD_REPORT_MEMBERS if m != "metrics_check"),
                ]

    def test_cp353_the_four_states_have_four_distinct_spellings(
        self, tmp_path, seal, capsys
    ):
        spellings = {}
        for state in _METRICS_STATES:
            directory = _stored_set_with_metrics_as(tmp_path / state, state, seal)
            spellings[state] = _json_of(stream_usage.check_main, directory, capsys)[
                "metrics_file"
            ]["state"]

        assert spellings == {
            "read": stream_usage.ReadState.READ.value,
            "absent": stream_usage.ReadState.NOTHING_RECEIVED.value,
            "unopenable": stream_usage.ReadState.UNOPENABLE.value,
            "uninspectable": stream_usage.ReadState.UNINSPECTABLE.value,
        }
        assert len(set(spellings.values())) == 4

    def test_empty_or_malformed_readable_metrics_stay_read_and_keep_their_counts(self, tmp_path):
        reader_of = stream_usage.build_reader
        empty = _stored_set_copy(tmp_path / "empty")
        (empty / "metrics.jsonl").write_text("")
        malformed = _stored_set_copy(tmp_path / "malformed", extra_metrics=["not json {"])

        empty_report = stream_usage.read_report(reader_of(empty))
        malformed_report = stream_usage.read_report(reader_of(malformed))

        assert empty_report.metrics_state is stream_usage.ReadState.READ
        assert empty_report.skipped_metric_lines == 0 and empty_report.metrics_reason is None
        assert malformed_report.metrics_state is stream_usage.ReadState.READ
        assert malformed_report.skipped_metric_lines == 1 and malformed_report.metrics_check

    def test_the_failed_reads_hold_no_check_and_a_zero_count(
        self, metrics_is_a_directory, metrics_link_into_mode_000_directory
    ):
        for directory, state in (
            (metrics_is_a_directory, stream_usage.ReadState.UNOPENABLE),
            (metrics_link_into_mode_000_directory, stream_usage.ReadState.UNINSPECTABLE),
        ):
            report = stream_usage.read_report(stream_usage.build_reader(directory))

            assert report.metrics_state is state and report.metrics_reason
            assert report.metrics_check is None and report.skipped_metric_lines == 0
            assert report.rows and report.skipped_lines == 1


class TestAbsentMetricsNamesEverySession:
    """CP-358, CP-359: with no `metrics.jsonl`, the report says so, and then still names every
    session it shows, each as the session line without a metric always read."""

    @_ENTRIES
    def test_cp358_absent_metrics_names_the_absence_then_every_session(
        self, entry, tmp_path, capsys
    ):
        directory = _stored_set_without_metrics(tmp_path / "absent")
        text = _text_of(getattr(stream_usage, entry), directory, capsys)

        lines = text.splitlines()
        absence = _metrics_absent_line(directory)
        assert lines.count(absence) == 1 and "skipped" not in absence
        at = lines.index(absence)
        assert lines[at + 1] == _CHECK_HEADING
        sessions = lines[at + 2 : at + 10]
        assert len(sessions) == 8 and _session_lines(text) == sessions
        assert [line.split()[0] + " " + line.split()[1] for line in sessions] == sorted(
            line.split()[0] + " " + line.split()[1] for line in sessions
        )
        for line in sessions:
            assert line.endswith(f"  no metric export — {_NO_METRIC_REASON}")
            assert "MISMATCH" not in line
        target = [line for line in sessions if _SESSION_WITH_NO_METRIC in line]
        assert len(target) == 1 and "log_cost_usd=0.050333" in target[0]
        assert "MISMATCH >1%" not in text and _SKIPPED_TEXT not in text
        assert "metrics-off" not in text and "disabled" not in text
        if entry == "check_main":
            assert lines[-1] == _COST_MARKING and lines.count(_COST_MARKING) == 1
            assert lines[0] == _CHECK_LOGS_COUNT.format(count=1)

    def test_cp358_absent_metrics_with_where_names_only_the_sessions_shown(
        self, tmp_path, capsys
    ):
        directory = _stored_set_without_metrics(tmp_path / "absent")
        full = _session_lines(_text_of(stream_usage.main, directory, capsys))

        one = _text_of(
            stream_usage.main, directory, capsys, "--where", f"session.id={_SESSION_WITH_NO_METRIC}"
        )
        none = _text_of(stream_usage.main, directory, capsys, "--where", "session.id=nothing")

        assert _session_lines(one) == [line for line in full if _SESSION_WITH_NO_METRIC in line]
        assert _metrics_absent_line(directory) in one.splitlines()
        assert _session_lines(none) == []
        tail = none.splitlines()
        assert tail[tail.index(_metrics_absent_line(directory)) + 1 :][:2] == [
            _CHECK_HEADING, "  No sessions to check.",
        ]

    def test_cp358_a_where_narrower_than_a_session_keeps_the_whole_session_cost(
        self, tmp_path, capsys
    ):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s1", "cost_usd": 0.25,
                         "query_source": "sdk"},
                        {"event.name": "api_request", "session.id": "s1", "cost_usd": 0.5,
                         "query_source": "other"},
                    ]
                )
            ],
        )

        narrowed = _session_lines(
            _text_of(stream_usage.main, tmp_path, capsys, "--where", "query_source=sdk")
        )

        assert len(narrowed) == 1 and "log_cost_usd=0.750000" in narrowed[0]

    @_ENTRIES
    @pytest.mark.parametrize(
        "logs_lines",
        [
            [],
            ["not json"],
            [_logs_line([{"event.name": "subagent_completed", "session.id": "s1"}])],
        ],
        ids=["empty", "malformed-only", "completed-only"],
    )
    def test_cp358_absent_metrics_with_no_session_has_no_marking(
        self, entry, logs_lines, tmp_path, capsys
    ):
        (tmp_path / "logs.jsonl").write_text("".join(f"{line}\n" for line in logs_lines))

        lines = _text_of(getattr(stream_usage, entry), tmp_path, capsys).splitlines()

        at = lines.index(_metrics_absent_line(tmp_path))
        assert lines[at + 1 : at + 3] == [_CHECK_HEADING, "  No sessions to check."]
        assert _COST_MARKING not in lines[at:]

    def test_cp358_a_request_without_session_id_or_with_zero_cost_follows_the_existing_selection(
        self, tmp_path, capsys
    ):
        _write_stream(
            tmp_path,
            [
                _logs_line(
                    [
                        {"event.name": "api_request", "session.id": "s1", "cost_usd": 0.25},
                        {"event.name": "api_request", "session.id": "s2", "cost_usd": 0.0},
                        {"event.name": "api_request", "cost_usd": 0.5},
                    ]
                )
            ],
        )
        absent = _text_of(stream_usage.check_main, tmp_path, capsys).splitlines()
        (tmp_path / "metrics.jsonl").write_text("")
        read = _text_of(stream_usage.check_main, tmp_path, capsys).splitlines()

        assert absent[0] == _metrics_absent_line(tmp_path)
        assert absent[1:] == read
        assert [line.split()[0] for line in read[1:3]] == ["session=s1", "session=s2"]

    @_ENTRIES
    def test_cp359_absent_metrics_json_lists_one_entry_per_session_line(
        self, entry, tmp_path, capsys
    ):
        run = getattr(stream_usage, entry)
        directory = _stored_set_without_metrics(tmp_path / "absent")
        sessions = [
            line.split()[0].removeprefix("session=")
            for line in _session_lines(_text_of(run, directory, capsys))
        ]

        document = _json_of(run, directory, capsys)

        entries = document["metrics_check"]
        assert [e["session_id"] for e in entries] == sessions and len(entries) == 8
        for item in entries:
            assert list(item) == [
                "session_id", "log_cost_usd", "metric_cost_usd", "within_tolerance",
                "possible_causes",
            ]
            assert item["metric_cost_usd"] is None and item["within_tolerance"] is None
            assert item["possible_causes"] == _NO_METRIC_CAUSES
        assert document["metrics_file"] == {"state": "nothing_received"}
        assert document["cost_estimate_note"] == _COST_MARKING
        assert list(document)[-1] == "cost_estimate_note"
        (directory / "logs.jsonl").write_text("")
        assert _json_of(run, directory, capsys)["metrics_check"] == []

    @_ENTRIES
    def test_cp359_absent_and_empty_metrics_documents_differ_only_by_state(
        self, entry, tmp_path, capsys
    ):
        run = getattr(stream_usage, entry)
        absent = _stored_set_without_metrics(tmp_path / "absent")
        empty = _stored_set_without_metrics(tmp_path / "empty")
        (empty / "metrics.jsonl").write_text("")

        before = _json_of(run, absent, capsys)
        after = _json_of(run, empty, capsys)

        assert before["metrics_check"] == after["metrics_check"]
        assert before["metrics_file"] == {"state": "nothing_received"}
        assert after["metrics_file"] == {"state": "read"}
        assert {**before, "metrics_file": None} == {**after, "metrics_file": None}


class TestLibraryCarriesMetricsState:
    """CP-360: the library report holds the metrics state and reason, the commands' words."""

    def test_cp360_library_state_and_reason_equal_the_json_metrics_file_member(
        self, tmp_path, seal, capsys
    ):
        for state in _METRICS_STATES:
            directory = _stored_set_with_metrics_as(tmp_path / state, state, seal)
            report = stream_usage.read_report(stream_usage.build_reader(directory))

            member = _json_of(stream_usage.check_main, directory, capsys)["metrics_file"]

            assert report.metrics_state.value == member["state"]
            assert report.metrics_reason == member.get("reason")

    def test_cp360_library_absent_checks_equal_the_json_entries_through_asdict(
        self, tmp_path, metrics_is_a_directory, capsys
    ):
        absent = _stored_set_without_metrics(tmp_path / "absent")
        report = stream_usage.read_report(stream_usage.build_reader(absent))
        document = _json_of(stream_usage.check_main, absent, capsys)

        as_data = json.loads(json.dumps([dataclasses.asdict(c) for c in report.metrics_check]))
        assert as_data == document["metrics_check"]
        assert stream_usage.read_report(
            stream_usage.build_reader(metrics_is_a_directory)
        ).metrics_check is None

    def test_stream_report_constructs_from_five_positional_arguments(self):
        report = stream_usage.StreamReport((), (), None, 0, None)

        names = [f.name for f in dataclasses.fields(stream_usage.StreamReport)]
        assert names == [
            "rows", "subagent_rows", "metrics_check", "skipped_lines", "compare",
            "skipped_metric_lines", "subagents_filtered_out", "metrics_state", "metrics_reason",
        ]
        assert report.skipped_metric_lines == 0 and report.subagents_filtered_out is False
        assert report.metrics_state is None and report.metrics_reason is None

    def test_metrics_read_once_through_read_outcome(self, tmp_path):
        reason = 'Device "busy" at \\server: café\ntry later'
        line = _logs_line([{"event.name": "api_request", "session.id": "s1", "cost_usd": 0.25}])
        calls: list[Path] = []

        class RecordingFiles:
            def __init__(self, metrics: stream_usage.ReadOutcome) -> None:
                self.metrics = metrics

            def read_outcome(self, path):
                calls.append(path)
                if path.name == "logs.jsonl":
                    return stream_usage.ReadOutcome(
                        state=stream_usage.ReadState.READ, lines=(line,)
                    )
                return self.metrics

        failed = (
            stream_usage.ReadState.NOTHING_RECEIVED,
            stream_usage.ReadState.UNOPENABLE,
            stream_usage.ReadState.UNINSPECTABLE,
        )
        for state in failed:
            calls.clear()
            files = RecordingFiles(stream_usage.ReadOutcome(state=state, reason=reason))

            report = stream_usage.read_report(stream_usage.Reader(files=files, directory=tmp_path))

            assert calls == [tmp_path / "logs.jsonl", tmp_path / "metrics.jsonl"]
            assert report.metrics_state is state
            if state is stream_usage.ReadState.NOTHING_RECEIVED:
                assert report.metrics_reason is None and len(report.metrics_check) == 1
                continue
            assert report.metrics_reason == reason and report.metrics_check is None
            document = json.loads(
                stream_usage.render_checks_json(
                    report.metrics_check, skipped_metric_lines=0, skipped_lines=0,
                    metrics_state=state, metrics_reason=reason, directory=tmp_path,
                )
            )
            text = stream_usage.render_checks(
                report.metrics_check, skipped_metric_lines=0, skipped_lines=0,
                metrics_state=state, metrics_reason=reason, directory=tmp_path,
            )
            assert document["metrics_file"]["reason"] == reason
            assert document["metrics_file"]["message"] + "\n" == text

        calls.clear()

        class FailedLogs:
            def read_outcome(self, path):
                calls.append(path)
                return stream_usage.ReadOutcome(
                    state=stream_usage.ReadState.UNOPENABLE, reason="Permission denied"
                )

        assert stream_usage.read_report(
            stream_usage.Reader(files=FailedLogs(), directory=tmp_path)
        ) is None
        assert calls == [tmp_path / "logs.jsonl"]


class TestMetricsStateInEveryCount:
    """CP-356, CP-357 across the four states, and CP-362: a read `metrics.jsonl` adds only the
    `metrics_file` member to the stored documents."""

    @pytest.mark.parametrize("state", _METRICS_STATES)
    def test_cp356_357_count_crosses_all_four_metrics_states(self, state, tmp_path, seal, capsys):
        directory = _stored_set_with_metrics_as(tmp_path / state, state, seal)

        lines = _text_of(stream_usage.check_main, directory, capsys).splitlines()
        document = _json_of(stream_usage.check_main, directory, capsys)
        agents = _json_of(stream_usage.main, directory, capsys)

        logs_count = _CHECK_LOGS_COUNT.format(count=1)
        assert lines.count(logs_count) == 1
        if state == "read":
            assert lines[:2] == [_CHECK_METRICS_COUNT.format(count=1), logs_count]
        else:
            assert lines[0] == logs_count
        assert document["skipped_lines"] == agents["skipped_lines"] == 1
        assert list(document)[:3] == ["unreadable_metric_lines", "metrics_file", "skipped_lines"]

    def test_cp362_stored_oracle_runs_gain_only_metrics_file_with_state_read(self, capsys):
        stored = json.loads((_STORED_SET / "expected" / "agents.json").read_text())

        agents = _json_of(stream_usage.main, _STORED_SET, capsys, "--prefix", "kensai")
        check = _json_of(stream_usage.check_main, _STORED_SET, capsys)

        assert list(agents) == [
            "unreadable_metric_lines", "metrics_file", *stored, "cost_estimate_note",
        ]
        assert agents["metrics_file"] == {"state": "read"}
        assert list(check)[:3] == ["unreadable_metric_lines", "metrics_file", "skipped_lines"]
        assert check["metrics_file"] == {"state": "read"} and check["skipped_lines"] == 1
        del agents["metrics_file"]
        assert agents["unreadable_metric_lines"] == 0

    def test_direct_renderer_calls_without_context_are_unchanged(self):
        assert stream_usage.render_checks(None) == _SKIPPED_TEXT + "\n"
        assert stream_usage.render_checks_json(None) == json.dumps(
            {"metrics_check": None}, indent=2
        )
        assert _SKIPPED_TEXT in stream_usage.render((), (), None, 0, None)
        assert json.loads(stream_usage.render_json((), (), None, 0, None))["metrics_check"] is None
        assert "metrics_file" not in json.loads(stream_usage.render_json((), (), (), 0, None))

    def test_the_renderers_state_the_context_they_are_given(self, tmp_path):
        absent = stream_usage.ReadState.NOTHING_RECEIVED

        text = stream_usage.render_checks((), metrics_state=absent, directory=tmp_path)
        as_json = json.loads(
            stream_usage.render_checks_json((), metrics_state=absent, directory=tmp_path)
        )

        assert text == "\n".join(
            [_metrics_absent_line(tmp_path), _CHECK_HEADING, "  No sessions to check."]
        ) + "\n"
        assert as_json == {"metrics_file": {"state": "nothing_received"}, "metrics_check": []}


# ── The totals check's one name in `check --help` (CP-386 to CP-389) ─────────────────────────────

# `agents --help` at product commit eaf839c, at 80 columns: it already says "totals check" and the
# change keeps every byte of it (CP-387).
_AGENTS_HELP_AT_EAF839C = """\
usage: ka-telemetry agents [-h] [--dir DIR] [--prefix PREFIX]
                           [--where KEY=VALUE] [--compare ATTR A B] [--json]

Reports the tokens, cost and duration of each agent, read from the files the
receiver writes. Each row is one session, workflow run, query source and
agent, highest cost first, with its requests, its models, its cost and the
four token classes (input, output, cache_read, cache_creation), which are
never added together. Every cost in it comes from the cost_usd Claude Code
reported with each request. Under the rows come the Agent-tool agents Claude
Code reported as completed, then the totals check that ka-telemetry check
prints, for the sessions the rows show.

options:
  -h, --help          show this help message and exit
  --dir DIR           the directory the receiver writes to, holding logs.jsonl
                      and metrics.jsonl (default: ~/.ka-telemetry)
  --prefix PREFIX     read every attribute whose key starts with PREFIX and a
                      dot, such as PREFIX.run_id, as one of your labels: rows
                      that differ in a label are shown apart, each row lists
                      its labels, and --compare counts a run as one
                      PREFIX.run_id value, or as one session where a request
                      has none. Without this flag, no attribute is a label.
  --where KEY=VALUE   keep only the requests whose attribute KEY equals VALUE,
                      looked up on the request first and its resource second;
                      repeat the flag to require every pair
  --compare ATTR A B  compare the requests whose attribute ATTR is A with
                      those whose ATTR is B: runs, totals and mean per run for
                      each, then B minus A and B divided by A of the mean cost
                      per run. A request without ATTR counts as having the
                      value None.
  --json              print the report as JSON instead of a table
"""
_CHECK_USAGE_AT_EAF839C = "usage: ka-telemetry check [-h] [--dir DIR] [--json]\n"
_CHECK_OPTIONS_AT_EAF839C = """\
options:
  -h, --help  show this help message and exit
  --dir DIR   the directory the receiver writes to, holding logs.jsonl and
              metrics.jsonl (default: ~/.ka-telemetry)
  --json      print the check as JSON instead of a table
"""


def _help_text(entry, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit) as helped:
        getattr(stream_usage, entry)(["--help"])
    assert helped.value.code == 0
    return capsys.readouterr().out


def test_agents_help_is_byte_identical(monkeypatch, capsys):
    shown = _help_text("main", monkeypatch, capsys)

    assert shown == _AGENTS_HELP_AT_EAF839C
    assert "totals check that ka-telemetry check prints" in " ".join(shown.split())
    assert "cross" not in shown.lower()


def test_check_help_description_names_totals_check_across_a_wrapped_line(monkeypatch, capsys):
    """CP-386, CP-388: the help is wrapped to the terminal's width, which can break the phrase, so
    it is read with every run of whitespace collapsed; usage line and options stay as they were."""
    shown = _help_text("check_main", monkeypatch, capsys)

    assert "totals check" in " ".join(shown.split())
    assert "cross" not in shown.lower()
    assert shown.startswith(_CHECK_USAGE_AT_EAF839C)
    assert shown.endswith(_CHECK_OPTIONS_AT_EAF839C)


def test_check_help_names_totals_check_at_a_narrow_width(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "40")
    with pytest.raises(SystemExit):
        stream_usage.check_main(["--help"])

    shown = capsys.readouterr().out

    assert "totals check" in " ".join(shown.split())


def test_render_checks_with_no_checks_uses_the_new_heading_and_failure_strings():
    assert stream_usage.render_checks(()).splitlines() == [
        "Totals check (claude_code.cost.usage vs summed cost_usd):",
        "  No sessions to check.",
    ]
    assert "cross" not in stream_usage.render_checks(()).lower()


# ── Invalid-value counts: a present value that was not a number ────────────
#
# CP-624–CP-627. A figure stays the sum of the values that were numbers; beside it a row carries,
# per summed field, how many of its records held a present value that was not a number.

SUMMED_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
)
NOT_A_NUMBER_VALUES = pytest.param("abc", id="string"), pytest.param(True, id="true"), pytest.param(
    False, id="false"
)


def _raw_logs_line(attribute_lists: list[list[dict]]) -> str:
    """One `logs.jsonl` line whose records carry exactly the attribute lists given, envelopes and
    all — so a value arrives in whatever shape the test names, through the real decoder."""
    return json.dumps(
        {
            "signal": "logs",
            "received_at": "2026-10-23T00:00:00+00:00",
            "body": {
                "resourceLogs": [
                    {
                        "resource": {"attributes": []},
                        "scopeLogs": [
                            {
                                "scope": {},
                                "logRecords": [{"attributes": attrs} for attrs in attribute_lists],
                            }
                        ],
                    }
                ]
            },
        }
    )


def _request_attributes(**envelopes) -> list[dict]:
    """The attributes of one `api_request` whose summed fields carry the envelopes given
    (`field=<OTLP value object>`); a field left out is absent, not zero."""
    attributes = [
        _attr("event.name", "api_request"),
        _attr("session.id", "sid-1"),
        _attr("query_source", "sdk"),
    ]
    attributes.extend({"key": key, "value": value} for key, value in envelopes.items())
    return attributes


class TestInvalidValueCounts:
    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    @pytest.mark.parametrize("bad", NOT_A_NUMBER_VALUES)
    def test_invalid_value_adds_nothing_and_is_counted_once_per_field(self, field, bad):
        valid = _api_request(**{field: 7})
        invalid = _api_request(**{field: bad})

        (row,) = stream_usage.agent_rows((valid, invalid))

        assert getattr(row, field) == 7
        assert row.request_count == 2
        assert row.invalid_values == {field: 1}

    @pytest.mark.parametrize(
        "envelope",
        [
            pytest.param(None, id="present-null"),
            pytest.param({"boolValue": True}, id="true"),
            pytest.param({"boolValue": False}, id="false"),
            pytest.param({"stringValue": ""}, id="empty-string"),
            pytest.param({"stringValue": "abc"}, id="non-numeric-string"),
            pytest.param({"intValue": "abc"}, id="int-value-holding-no-number"),
            pytest.param({"unknownValue": 3}, id="object-with-none-of-the-four-keys"),
            pytest.param([1, 2], id="list"),
            pytest.param({"nested": {"a": 1}}, id="object"),
        ],
    )
    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    def test_every_unreadable_shape_is_one_count_through_the_real_decoder(self, field, envelope):
        line = _raw_logs_line(
            [
                _request_attributes(**{field: envelope}),
                _request_attributes(**{field: {"intValue": 4}}),
            ]
        )

        read = stream_usage.read_log_lines([line])
        (row,) = stream_usage.agent_rows(read.records)

        assert read.skipped == 0
        assert row.request_count == 2
        assert getattr(row, field) == 4
        assert row.invalid_values == {field: 1}

    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    def test_absent_field_adds_no_count(self, field):
        with_field = _api_request(**{field: 5})
        without = _api_request()
        del without.record_attrs[field]

        (row,) = stream_usage.agent_rows((with_field, without))

        assert getattr(row, field) == 5
        assert row.invalid_values == {}

    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    def test_value_only_in_resource_attributes_is_absent(self, field):
        attrs = {"event.name": "api_request", "session.id": "sid-1"}
        record = _record(attrs, resource_attrs={field: "abc"})

        (row,) = stream_usage.agent_rows((record,))

        assert getattr(row, field) == 0
        assert row.invalid_values == {}

    def test_present_invalid_record_value_counts_even_when_resource_has_a_valid_one(self):
        record = _record(
            {"event.name": "api_request", "session.id": "sid-1", "cost_usd": "abc"},
            resource_attrs={"cost_usd": 9.0},
        )

        (row,) = stream_usage.agent_rows((record,))

        assert row.cost_usd == 0
        assert row.invalid_values == {"cost_usd": 1}

    @pytest.mark.parametrize(
        "number, expected",
        [
            (3, 3),
            (2.5, 2.5),
            (0, 0),
            (0.0, 0),
            ("0", 0),
            ("7", 7),
            ({"intValue": "3"}, None),
            ("2.5", 2.5),
            ("1e3", 1000.0),
            ("+5", 5),
            ("-5", -5),
            (" 5 ", 5),
        ],
    )
    def test_numbers_keep_their_results_and_are_never_counted(self, number, expected):
        if isinstance(number, dict):
            line = _raw_logs_line([_request_attributes(duration_ms=number)])
            (row,) = stream_usage.agent_rows(stream_usage.read_log_lines([line]).records)
            assert row.duration_ms == 3
        else:
            (row,) = stream_usage.agent_rows((_api_request(duration_ms=number),))
            assert row.duration_ms == expected
        assert row.invalid_values == {}

    @pytest.mark.parametrize(
        "text, reads_as", [("nan", math.isnan), ("inf", lambda n: n == math.inf)]
    )
    def test_nan_and_inf_keep_the_acceptance_as_number_gives_them(self, text, reads_as):
        # Today (6de1734) these strings read as the non-finite floats Python parses them to; the
        # change moves no value across the line (CP-624), so they still do, and are not counted.
        (row,) = stream_usage.agent_rows((_api_request(cost_usd=text),))

        assert reads_as(row.cost_usd)
        assert reads_as(stream_usage.as_number(text))
        assert row.invalid_values == {}

    @pytest.mark.parametrize("zero", [0, 0.0, "0", "0.0"])
    def test_valid_zero_is_not_an_invalid_value(self, zero):
        (row,) = stream_usage.agent_rows((_api_request(**{f: zero for f in SUMMED_FIELDS}),))

        assert [getattr(row, f) for f in SUMMED_FIELDS] == [0] * 6
        assert row.invalid_values == {}

    def test_counts_are_per_row_and_do_not_leak_between_rows(self):
        dirty = _api_request(**{"session.id": "sid-dirty", "cost_usd": "abc"})
        clean = _api_request(**{"session.id": "sid-clean"})

        rows = {r.session_id: r for r in stream_usage.agent_rows((dirty, clean))}

        assert rows["sid-dirty"].invalid_values == {"cost_usd": 1}
        assert rows["sid-clean"].invalid_values == {}

    def test_count_does_not_split_or_reorder_rows(self):
        records = (
            _api_request(**{"session.id": "first", "cost_usd": 0.5}),
            _api_request(**{"session.id": "second", "cost_usd": "abc"}),
            _api_request(**{"session.id": "first", "cost_usd": "abc"}),
            _api_request(**{"session.id": "third", "cost_usd": 0.1}),
        )

        rows = stream_usage.agent_rows(records)

        assert [r.session_id for r in rows] == ["first", "second", "third"]
        assert [r.request_count for r in rows] == [2, 1, 1]

    def test_invalid_value_in_filtered_out_record_is_counted_nowhere(self):
        kept = _api_request(**{"session.id": "keep", "cost_usd": 1.0})
        dropped = _api_request(**{"session.id": "drop", "cost_usd": "abc"})

        rows = stream_usage.agent_rows((kept, dropped), where=(("session.id", "keep"),))
        nothing = stream_usage.agent_rows((kept, dropped), where=(("session.id", "none"),))

        assert [r.invalid_values for r in rows] == [{}]
        assert nothing == ()

    def test_chosen_label_prefix_splits_groups_before_counting(self):
        records = (
            _api_request(**{"team.arm": "a", "cost_usd": "abc"}),
            _api_request(**{"team.arm": "b", "cost_usd": 1.0}),
            _api_request(**{"team.arm": "b", "duration_ms": "abc"}),
        )

        rows = {r.labels["team.arm"]: r for r in stream_usage.agent_rows(records, prefix="team")}

        assert rows["a"].invalid_values == {"cost_usd": 1}
        assert rows["b"].invalid_values == {"duration_ms": 1}

    def test_non_api_request_events_carrying_the_six_names_are_not_counted(self):
        other = _record(
            {"event.name": "subagent_completed", **{f: "abc" for f in SUMMED_FIELDS}}
        )

        assert stream_usage.agent_rows((other, _api_request())) [0].invalid_values == {}

    def test_all_values_invalid_gives_zero_figures_and_six_counts(self):
        records = (
            _api_request(**{f: "abc" for f in SUMMED_FIELDS}),
            _api_request(**{f: True for f in SUMMED_FIELDS}),
        )

        (row,) = stream_usage.agent_rows(records)

        assert [getattr(row, f) for f in SUMMED_FIELDS] == [0] * 6
        assert row.request_count == 2
        assert row.invalid_values == {f: 2 for f in SUMMED_FIELDS}

    def test_all_fields_absent_gives_zero_figures_and_no_count(self):
        record = _record({"event.name": "api_request", "session.id": "sid-1"})

        (row,) = stream_usage.agent_rows((record,))

        assert [getattr(row, f) for f in SUMMED_FIELDS] == [0] * 6
        assert row.request_count == 1
        assert row.invalid_values == {}

    def test_agent_row_default_count_map_is_empty_and_independent(self):
        positional = stream_usage.AgentRow("s", None, None, None, {}, (), 1, 1, 2, 3, 4, 0.5, 6)
        keyword = stream_usage.AgentRow(
            session_id="s",
            workflow_run_id=None,
            query_source=None,
            agent_name=None,
            labels={},
            models=(),
            request_count=1,
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_creation_tokens=4,
            cost_usd=0.5,
            duration_ms=6,
        )

        assert positional.invalid_values == {} == keyword.invalid_values
        assert positional.invalid_values is not keyword.invalid_values

    def test_read_report_rows_carry_the_same_counts_the_renderers_print(self, tmp_path):
        _write_stream(
            tmp_path,
            [_raw_logs_line([_request_attributes(output_tokens={"stringValue": "abc"})])],
        )

        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))

        assert [r.invalid_values for r in report.rows] == [{"output_tokens": 1}]

    def test_as_number_still_reads_a_non_number_as_zero(self):
        assert stream_usage.as_number("abc") == 0
        assert stream_usage.as_number(True) == 0
        assert stream_usage.as_number(None) == 0


# ── Invalid-value counts, printed (library level) ──────────────────────────

_NO_COUNT_TEXT_AT_6DE1734 = (
    "2 agent row(s), 0 unreadable line(s) skipped.\n"
    "  session=s1 workflow_run_id=None query_source=sdk agent=a  requests=2  models=m  "
    "cost_usd=0.500000\n"
    "    input=10 output=20 cache_read=30 cache_creation=40 duration_ms=60\n"
    "  session=s2 workflow_run_id=None query_source=sdk agent=b  requests=1  models=m  "
    "cost_usd=0.250000\n"
    "    input=1 output=2 cache_read=3 cache_creation=4 duration_ms=6\n"
    "    team: team.x=1\n"
    "Total cost_usd: 0.750000\n"
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill.\n"
    "\n"
    "subagent_completed counts Agent-tool agents only — a Workflow step's own requests "
    '(query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so this '
    "count does not cover them.\n"
    "No subagent_completed records read.\n"
    "\n"
    "Totals check (claude_code.cost.usage vs summed cost_usd):\n"
    "  No sessions to check.\n"
)
_NO_COUNT_JSON_AT_6DE1734 = (
    '{\n  "skipped_lines": 0,\n  "total_cost_usd": 0.75,\n  "rows": [\n    {\n'
    '      "session_id": "s1",\n      "workflow_run_id": null,\n      "query_source": "sdk",\n'
    '      "agent_name": "a",\n      "team": {},\n      "models": [\n        "m"\n      ],\n'
    '      "request_count": 2,\n      "input_tokens": 10,\n      "output_tokens": 20,\n'
    '      "cache_read_tokens": 30,\n      "cache_creation_tokens": 40,\n'
    '      "cost_usd": 0.5,\n      "duration_ms": 60\n    },\n    {\n'
    '      "session_id": "s2",\n      "workflow_run_id": null,\n      "query_source": "sdk",\n'
    '      "agent_name": "b",\n      "team": {\n        "team.x": "1"\n      },\n'
    '      "models": [\n        "m"\n      ],\n      "request_count": 1,\n'
    '      "input_tokens": 1,\n      "output_tokens": 2,\n      "cache_read_tokens": 3,\n'
    '      "cache_creation_tokens": 4,\n      "cost_usd": 0.25,\n      "duration_ms": 6\n'
    "    }\n  ],\n"
    '  "subagent_coverage_note": "subagent_completed counts Agent-tool agents only \\u2014 a '
    "Workflow step's own requests (query_source \\\"agent:builtin:workflow-subagent\\\") emit no "
    'subagent_completed record, so this count does not cover them.",\n'
    '  "subagent_rows": [],\n  "metrics_check": [],\n'
    '  "cost_estimate_note": "Every cost here is an estimate: Claude Code\'s own, in US dollars '
    'as Claude Code reported it, or a total, mean or difference of those estimates. None is a '
    'bill."\n}'
)


def _counted_row(counts: dict[str, int] | None = None, **overrides) -> stream_usage.AgentRow:
    fields = dict(
        session_id="s1",
        workflow_run_id=None,
        query_source="sdk",
        agent_name="a",
        labels={},
        models=("m",),
        request_count=2,
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_creation_tokens=40,
        cost_usd=0.5,
        duration_ms=60,
    )
    fields.update(overrides)
    if counts is not None:
        fields["invalid_values"] = counts
    return stream_usage.AgentRow(**fields)


def _rendered(rows, *, prefix=None) -> str:
    return stream_usage.render(rows, (), (), 0, None, prefix=prefix)


def _rendered_json(rows, *, prefix=None) -> dict:
    return json.loads(stream_usage.render_json(rows, (), (), 0, None, prefix=prefix))


class TestInvalidValueCountsPrinted:
    def test_no_invalid_value_prints_byte_for_byte_as_today(self):
        rows = (
            _counted_row(),
            _counted_row(
                session_id="s2",
                agent_name="b",
                labels={"team.x": "1"},
                request_count=1,
                input_tokens=1,
                output_tokens=2,
                cache_read_tokens=3,
                cache_creation_tokens=4,
                cost_usd=0.25,
                duration_ms=6,
            ),
        )

        # CP-633: with no count anywhere, both forms are today's, byte for byte — the literals were
        # printed by the product at 6de1734 for these same rows.
        assert _rendered(rows, prefix="team") == _NO_COUNT_TEXT_AT_6DE1734
        assert (
            stream_usage.render_json(rows, (), (), 0, None, prefix="team")
            == _NO_COUNT_JSON_AT_6DE1734
        )

    def test_old_style_row_prints_as_before(self):
        row = stream_usage.AgentRow("s1", None, "sdk", "a", {}, ("m",), 2, 10, 20, 30, 40, 0.5, 60)

        assert "(invalid:" not in _rendered((row,))
        assert "invalid_values" not in json.dumps(_rendered_json((row,)))

    @pytest.mark.parametrize(
        "zeros", [{"cost_usd": 0}, {f: 0 for f in stream_usage.SUMMED_FIELDS}]
    )
    def test_a_row_whose_counts_are_all_zero_carries_no_member(self, zeros):
        # CP-630: "A row whose counts are all 0 carries no `invalid_values` member" — a row a
        # library caller builds with a zero count reads in JSON as the text report reads it.
        row = _counted_row(zeros)

        assert "(invalid:" not in _rendered((row,))
        document = _rendered_json((row,))
        assert "invalid_values" not in document["rows"][0]
        assert "invalid_values" not in document

    def test_a_zero_count_beside_a_positive_one_is_left_out_of_the_member(self):
        (shown,) = _rendered_json((_counted_row({"cost_usd": 0, "duration_ms": 2}),))["rows"]

        assert shown["invalid_values"] == {"duration_ms": 2}

    def test_all_six_fields_invalid_orders_the_counts_in_the_six_field_order(self):
        scrambled = {
            "duration_ms": 6,
            "cost_usd": 5,
            "cache_creation_tokens": 4,
            "cache_read_tokens": 3,
            "output_tokens": 2,
            "input_tokens": 1,
        }

        document = _rendered_json((_counted_row(scrambled),))
        (row,) = document["rows"]

        assert list(row)[-2:] == ["duration_ms", "invalid_values"]
        assert list(row["invalid_values"]) == list(stream_usage.SUMMED_FIELDS)
        assert list(row["invalid_values"].values()) == [1, 2, 3, 4, 5, 6]
        assert all(type(n) is int for n in row["invalid_values"].values())
        assert document["invalid_values"] == {"total_cost_usd": 5}

    def test_a_chosen_label_prefix_keeps_its_member_position(self):
        row = _counted_row({"cost_usd": 1}, labels={"team.x": "1"})

        (shown,) = _rendered_json((row,), prefix="team")["rows"]

        keys = list(shown)
        assert keys.index("agent_name") + 1 == keys.index("team") == keys.index("models") - 1
        assert keys[-1] == "invalid_values"

    def test_text_report_adds_no_line_for_a_count(self):
        plain = _rendered((_counted_row(),))
        counted = _rendered((_counted_row({"input_tokens": 1, "cost_usd": 2}),))

        assert len(counted.splitlines()) == len(plain.splitlines())
        assert counted.count("(invalid:") == 3

    def test_each_figure_carries_its_own_mark_and_the_total_sums_the_cost_counts(self):
        rows = (
            _counted_row({"cost_usd": 1}),
            _counted_row({"cost_usd": 3, "duration_ms": 2}, session_id="s2", agent_name="b"),
        )

        lines = _rendered(rows).splitlines()

        assert lines[1].endswith("cost_usd=0.500000 (invalid: 1)")
        assert lines[2] == (
            "    input=10 output=20 cache_read=30 cache_creation=40 duration_ms=60"
        )
        assert lines[4] == (
            "    input=10 output=20 cache_read=30 cache_creation=40 duration_ms=60 (invalid: 2)"
        )
        assert "Total cost_usd: 1.000000 (invalid: 4)" in lines
        assert _rendered_json(rows)["invalid_values"] == {"total_cost_usd": 4}
        order = list(_rendered_json(rows))
        assert order.index("invalid_values") == order.index("total_cost_usd") + 1

    def test_only_the_invalid_field_is_annotated(self):
        row = _counted_row({"duration_ms": 1})

        text = _rendered((row,))
        document = _rendered_json((row,))

        assert "duration_ms=60 (invalid: 1)" in text
        assert "cost_usd=0.500000\n" in text
        assert "Total cost_usd: 0.500000\n" in text
        assert document["rows"][0]["invalid_values"] == {"duration_ms": 1}
        assert "invalid_values" not in document

    @pytest.mark.parametrize("figure", ["input", "output", "cache_read", "cache_creation"])
    def test_every_token_figure_is_marked_on_the_second_line(self, figure):
        keys = {
            "input": "input_tokens",
            "output": "output_tokens",
            "cache_read": "cache_read_tokens",
            "cache_creation": "cache_creation_tokens",
        }
        row = _counted_row({keys[figure]: 7})

        second = _rendered((row,)).splitlines()[2]

        assert second.count("(invalid:") == 1
        assert re.search(rf"\b{figure}=\S+ \(invalid: 7\)", second)

    def test_empty_stream_and_zero_selected_rows_have_zero_total_and_no_count(self):
        text = _rendered(())
        document = _rendered_json(())

        assert "Total cost_usd: 0.000000\n" in text
        assert "(invalid:" not in text
        assert document["total_cost_usd"] == 0
        assert "invalid_values" not in document


# ── --prefix invalid_values: refused by `agents` alone ─────────────────────


class TestInvalidValuesPrefixIsAgentsOnly:
    def test_prefix_cost_usd_keeps_todays_refusal(self, capsys):
        with pytest.raises(SystemExit) as stop:
            stream_usage.main(["--prefix", "cost_usd"])

        assert stop.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.splitlines()[-2] == (
            "ka-telemetry agents: error: --prefix 'cost_usd' is also the name of a field in "
            "each row of the --json output, so labels under it cannot be shown; use --where to "
            "select requests by those attributes instead"
        )

    def test_invalid_values_is_refused_by_the_command_with_its_own_words(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as stop:
            stream_usage.main(["--dir", str(tmp_path / "never-read"), "--prefix", "invalid_values"])

        assert stop.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert (
            "a field the --json output adds to a row that left out an invalid value"
            in captured.err
        )

    @pytest.mark.parametrize("prefix", ["team", "invalid", "invalid_values2", "Invalid_Values"])
    def test_other_prefixes_stay_accepted(self, prefix, tmp_path, capsys):
        _write_stream(tmp_path, [_logs_line([{"event.name": "api_request", "session.id": "s"}])])

        assert stream_usage.main(["--dir", str(tmp_path), "--prefix", prefix]) == 0

    @pytest.mark.parametrize("name", stream_usage.ROW_KEYS)
    def test_every_row_key_still_gets_its_existing_refusal(self, name, capsys):
        with pytest.raises(SystemExit) as stop:
            stream_usage.main(["--prefix", name])

        assert stop.value.code == 2
        assert (
            f"--prefix {name!r} is also the name of a field in each row"
            in capsys.readouterr().err
        )

    def test_shared_validation_and_the_library_still_accept_invalid_values(self):
        assert stream_usage.check_prefix("invalid_values") is None
        assert "invalid_values" not in stream_usage.ROW_KEYS
        stream_usage.validate_prefix("invalid_values")
        assert stream_usage.agent_rows((_api_request(),), prefix="invalid_values")


# ── The totals check counts the values it could not read as numbers (CP-639–CP-645) ──────


_UNSET = object()


def _enveloped_attribute(key: str, value) -> dict:
    """One OTLP attribute carrying `value` as the envelope given, whole; `_UNSET` leaves out the
    `value` member altogether."""
    attribute = {"key": key}
    if value is not _UNSET:
        attribute["value"] = value
    return attribute


def _raw_request(
    session_id: str | None,
    cost=_UNSET,
    *,
    event: str = "api_request",
    query_source: str = "sdk",
    agent: str | None = None,
) -> dict:
    """An `api_request` log record whose `cost_usd` arrives as the envelope `cost` (`_UNSET`: no
    `cost_usd` attribute at all)."""
    attributes = [
        _attr("event.name", event),
        _attr("query_source", query_source),
    ]
    if session_id is not None:
        attributes.append(_attr("session.id", session_id))
    if agent is not None:
        attributes.append(_attr("agent.name", agent))
    if cost is not _UNSET:
        attributes.append(_enveloped_attribute("cost_usd", cost))
    return {"attributes": attributes}


def _raw_logs_export_line(
    records: list[dict], resource_attributes: list[dict] | None = None
) -> str:
    return json.dumps(
        {
            "signal": "logs",
            "received_at": "2026-09-11T00:00:00+00:00",
            "body": {
                "resourceLogs": [
                    {
                        "resource": {"attributes": resource_attributes or []},
                        "scopeLogs": [{"logRecords": records}],
                    }
                ]
            },
        }
    )


def _raw_point(session_id: str | None, **members) -> dict:
    """A cost datapoint whose `asDouble` / `asInt` members are exactly `members` (a member left
    out of the call is absent from the point)."""
    attributes = [_attr("query_source", "main")]
    if session_id is not None:
        attributes.append(_attr("session.id", session_id))
    return {
        "attributes": attributes,
        "startTimeUnixNano": "1788989273064000000",
        "timeUnixNano": "1788989273225000000",
        **members,
    }


def _counted_report(
    tmp_path: Path,
    records: list[dict],
    points: list[dict] | list[list[dict]] | None,
    **report_arguments,
) -> stream_usage.StreamReport:
    """The report over real files: one logs export holding `records`, and one metrics export per
    list of points (a flat list is one export); `None` leaves `metrics.jsonl` out."""
    exports = None
    if points is not None:
        groups = points if points and isinstance(points[0], list) else [points]
        exports = [_metrics_line(_cost_metric(group)) for group in groups]
    _write_stream(tmp_path, [_raw_logs_export_line(records)], exports)
    return stream_usage.read_report(stream_usage.build_reader(tmp_path), **report_arguments)


def _counts(report: stream_usage.StreamReport) -> dict[str, tuple[int, int]]:
    """Per shown session: (request-cost count, metric-point count)."""
    return {
        check.session_id: (check.invalid_log_cost_count, check.invalid_metric_cost_count)
        for check in report.metrics_check
    }


_VALID_COST = {"doubleValue": 0.4}


class TestSessionCheckCounts:
    """CP-645: the two counts are read-only properties of the session check; the dataclass keeps
    its five fields, their order and its `asdict`."""

    def test_four_argument_session_check_reads_both_counts_as_zero(self):
        check = stream_usage.SessionMetricsCheck("s", 0.5, None, None)

        assert (check.invalid_log_cost_count, check.invalid_metric_cost_count) == (0, 0)

    def test_session_check_asdict_and_field_order_unchanged_in_all_three_verdict_states(self):
        checks = (
            stream_usage.SessionMetricsCheck("s", 0.5, None, None),
            stream_usage.SessionMetricsCheck("s", 1.0, 2.0, False),
            stream_usage.SessionMetricsCheck("s", 1.0, 1.0, True),
        )
        fields = ["session_id", "log_cost_usd", "metric_cost_usd", "within_tolerance"]

        for check, causes in zip(
            checks, (tuple(_NO_METRIC_CAUSES), tuple(_MISMATCH_CAUSES), ()), strict=True
        ):
            assert [f.name for f in dataclasses.fields(check)] == fields + ["possible_causes"]
            assert dataclasses.asdict(check) == {
                "session_id": check.session_id,
                "log_cost_usd": check.log_cost_usd,
                "metric_cost_usd": check.metric_cost_usd,
                "within_tolerance": check.within_tolerance,
                "possible_causes": causes,
            }
            assert (check.invalid_log_cost_count, check.invalid_metric_cost_count) == (0, 0)

    @pytest.mark.parametrize("name", ["invalid_log_cost_count", "invalid_metric_cost_count"])
    def test_count_properties_cannot_be_assigned(self, name):
        check = stream_usage.SessionMetricsCheck("s", 0.5, None, None)

        assert getattr(check, name) == 0
        with pytest.raises(AttributeError):
            setattr(check, name, 3)
        assert getattr(check, name) == 0

    def test_direct_metrics_check_returns_zero_counts_even_when_rows_carry_invalid_costs(self):
        rows = stream_usage.agent_rows(
            (
                _api_request(**{"session.id": "s"}, cost_usd="abc"),
                _api_request(**{"session.id": "s"}),
            )
        )
        (row,) = rows
        assert row.invalid_values == {"cost_usd": 1}
        points = stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([_raw_point("s", asDouble="abc")]))]
        )

        (check,) = stream_usage.metrics_check(rows, points)

        assert (check.invalid_log_cost_count, check.invalid_metric_cost_count) == (0, 0)
        assert check.log_cost_usd == pytest.approx(0.1)
        assert check.metric_cost_usd == 0.0


class TestReportCountsInvalidValues:
    """CP-639–CP-642 through `read_report`, over real files."""

    def test_report_checks_count_invalid_request_costs_and_metric_points_per_session(
        self, tmp_path
    ):
        records = [
            _raw_request("s1", _VALID_COST),
            _raw_request("s1", {"stringValue": "abc"}),
            _raw_request("s1", {"boolValue": True}),
            _raw_request("s1", _UNSET),
            _raw_request("s1", {"stringValue": "0.1"}),
            _raw_request("s2", {"doubleValue": 0.2}),
        ]
        points = [
            _raw_point("s1", asDouble=0.3),
            _raw_point("s1", asDouble="abc"),
            _raw_point("s1", asDouble=True),
            _raw_point("s1"),
            _raw_point("s1", asDouble=None),
            _raw_point("s1", asInt="0.2"),
            _raw_point("s2", asDouble="xyz"),
        ]

        report = _counted_report(tmp_path, records, points)

        s1, s2 = report.metrics_check
        assert (s1.session_id, s1.invalid_log_cost_count, s1.invalid_metric_cost_count) == (
            "s1", 2, 2,
        )
        assert s1.log_cost_usd == pytest.approx(0.5)
        assert s1.metric_cost_usd == pytest.approx(0.5)
        assert (s2.session_id, s2.invalid_log_cost_count, s2.invalid_metric_cost_count) == (
            "s2", 0, 1,
        )
        assert s2.log_cost_usd == pytest.approx(0.2)
        assert s2.metric_cost_usd == 0.0
        assert s2.within_tolerance is False

    @pytest.mark.parametrize(
        "members, counted, value",
        [
            ({}, 0, 0.0),
            ({"asDouble": None}, 0, 0.0),
            ({"asInt": None}, 0, 0.0),
            ({"asDouble": None, "asInt": None}, 0, 0.0),
            ({"asDouble": None, "asInt": 5}, 0, 5.0),
            ({"asDouble": None, "asInt": "7"}, 0, 7.0),
            ({"asDouble": None, "asInt": "abc"}, 1, 0.0),
            ({"asInt": "abc"}, 1, 0.0),
            ({"asDouble": 0.25, "asInt": "abc"}, 0, 0.25),
            ({"asDouble": "abc", "asInt": 5}, 1, 0.0),
            ({"asDouble": "0.5", "asInt": 5}, 0, 0.5),
            ({"asDouble": False}, 1, 0.0),
            ({"asDouble": True, "asInt": 5}, 1, 0.0),
            ({"asDouble": 0}, 0, 0.0),
            ({"asDouble": ""}, 1, 0.0),
            ({"asDouble": [1]}, 1, 0.0),
            ({"asDouble": {"x": 1}}, 1, 0.0),
            ({"asInt": True}, 1, 0.0),
            ({"asInt": False}, 1, 0.0),
            ({"asInt": 0}, 0, 0.0),
            ({"asInt": ""}, 1, 0.0),
            ({"asInt": [2]}, 1, 0.0),
            ({"asInt": {"x": 2}}, 1, 0.0),
        ],
    )
    def test_report_metric_selection_cases(self, tmp_path, members, counted, value):
        point = _raw_point("s1", **members)

        report = _counted_report(tmp_path, [_raw_request("s1", _VALID_COST)], [point])

        assert _counts(report) == {"s1": (0, counted)}
        (check,) = report.metrics_check
        assert check.metric_cost_usd == pytest.approx(value)
        (legacy,) = stream_usage.read_metric_lines(
            [_metrics_line(_cost_metric([point]))]
        )
        assert legacy.value == pytest.approx(value)

    def test_valid_numbers_are_added_and_not_counted(self, tmp_path):
        numbers = [
            _raw_point("s1", asDouble=1.5),
            _raw_point("s1", asDouble=0),
            _raw_point("s1", asDouble=-2),
            _raw_point("s1", asDouble="0.1"),
            _raw_point("s1", asDouble="43"),
            _raw_point("s1", asDouble="1e3"),
            _raw_point("s1", asInt="0.2"),
        ]
        costs = [
            {"doubleValue": 1.5},
            {"doubleValue": 0},
            {"doubleValue": -2},
            {"stringValue": "0.1"},
            {"stringValue": "43"},
            {"stringValue": "1e3"},
            {"intValue": "7"},
        ]
        records = [_raw_request("s1", cost) for cost in costs]

        report = _counted_report(tmp_path, records, numbers)

        (check,) = report.metrics_check
        assert _counts(report) == {"s1": (0, 0)}
        assert check.metric_cost_usd == pytest.approx(1042.8)
        assert check.log_cost_usd == pytest.approx(1049.6)

    def test_nan_and_inf_strings_are_numbers_to_today_s_rule_and_are_not_counted(self, tmp_path):
        records = [
            _raw_request("s-nan", {"stringValue": "nan"}),
            _raw_request("s-inf", {"stringValue": "inf"}),
        ]
        points = [_raw_point("s-nan", asDouble="nan"), _raw_point("s-inf", asDouble="inf")]

        report = _counted_report(tmp_path, records, points)

        assert _counts(report) == {"s-inf": (0, 0), "s-nan": (0, 0)}
        by_session = {check.session_id: check for check in report.metrics_check}
        assert math.isnan(by_session["s-nan"].metric_cost_usd)
        assert math.isinf(by_session["s-inf"].metric_cost_usd)

    @pytest.mark.parametrize(
        "cost",
        [
            {"intValue": "oops"},
            _UNSET,
            None,
            {},
            {"unsupported": 1},
            {"doubleValue": [1.0]},
            {"stringValue": {"x": 1}},
            {"stringValue": ""},
            {"stringValue": "abc"},
            {"boolValue": True},
            {"boolValue": False},
        ],
        ids=[
            "malformed-int", "no-value-member", "null-value", "empty-envelope", "unsupported",
            "list", "object", "empty-string", "string", "true", "false",
        ],
    )
    def test_request_cost_forms_counted_as_invalid(self, tmp_path, cost):
        record = {
            "attributes": [
                _attr("event.name", "api_request"),
                _attr("session.id", "s1"),
                _enveloped_attribute("cost_usd", cost),
            ]
        }
        records = [_raw_request("s1", _VALID_COST), record]

        report = _counted_report(tmp_path, records, [_raw_point("s1", asDouble=0.4)])

        assert _counts(report) == {"s1": (1, 0)}
        (check,) = report.metrics_check
        assert check.log_cost_usd == pytest.approx(0.4)

    def test_request_cost_in_resource_attributes_only_is_absent_and_other_events_are_ignored(
        self, tmp_path
    ):
        records = [
            _raw_request("s1", _VALID_COST),
            _raw_request("s1", _UNSET),
            _raw_request("s1", {"stringValue": "abc"}, event="user_prompt"),
        ]
        _write_stream(
            tmp_path,
            [
                _raw_logs_export_line(
                    records, [_enveloped_attribute("cost_usd", {"stringValue": "abc"})]
                )
            ],
            [_metrics_line(_cost_metric([_raw_point("s1", asDouble=0.4)]))],
        )

        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))

        assert _counts(report) == {"s1": (0, 0)}
        assert report.metrics_check[0].log_cost_usd == pytest.approx(0.4)

    def test_counts_accumulate_across_exports_and_never_bleed_between_sessions(self, tmp_path):
        records = [
            _raw_request("s1", {"stringValue": "abc"}),
            _raw_request("s2", _VALID_COST),
            _raw_request("s2", {"boolValue": True}),
            _raw_request("s3", _VALID_COST),
        ]
        exports = [
            [_raw_point("s1", asDouble="abc"), _raw_point("s2", asDouble=0.4)],
            [_raw_point("s1", asDouble=False), _raw_point("s1", asDouble=0.1)],
            [
                _raw_point(None, asDouble="abc"),
                _raw_point("ghost", asDouble="abc"),
                _raw_point("s2", asDouble="abc"),
            ],
        ]

        report = _counted_report(tmp_path, records, exports)

        assert _counts(report) == {"s1": (1, 2), "s2": (1, 1), "s3": (0, 0)}
        assert report.metrics_check[2].metric_cost_usd is None

    def test_where_and_prefix_do_not_narrow_counts_within_a_shown_session(self, tmp_path):
        records = [
            _raw_request("s1", _VALID_COST, query_source="sdk", agent="a"),
            _raw_request("s1", {"stringValue": "abc"}, query_source="other", agent="b"),
            _raw_request("s1", {"boolValue": False}, query_source="other", agent="c"),
            _raw_request("s1", {"boolValue": True}, query_source="other", agent="c"),
            _raw_request("s2", {"stringValue": "abc"}, query_source="other"),
        ]
        points = [_raw_point("s1", asDouble=0.4)]

        narrowed = _counted_report(
            tmp_path, records, points, where=(("query_source", "sdk"),), prefix="team"
        )

        assert [row.session_id for row in narrowed.rows] == ["s1"]
        assert narrowed.rows[0].invalid_values == {}
        assert _counts(narrowed) == {"s1": (3, 0)}

    def test_session_with_only_invalid_or_valueless_points_keeps_a_metric_figure(self, tmp_path):
        records = [_raw_request("invalid", _VALID_COST), _raw_request("valueless", _VALID_COST)]
        records.append(_raw_request("none", _VALID_COST))
        points = [
            _raw_point("invalid", asDouble="x"),
            _raw_point("invalid", asDouble=True),
            _raw_point("valueless"),
            _raw_point("valueless", asDouble=None),
        ]

        report = _counted_report(tmp_path, records, points)

        by_session = {check.session_id: check for check in report.metrics_check}
        assert by_session["invalid"].metric_cost_usd == 0.0
        assert by_session["valueless"].metric_cost_usd == 0.0
        assert by_session["none"].metric_cost_usd is None
        assert by_session["invalid"].within_tolerance is False
        assert _counts(report) == {"invalid": (0, 2), "none": (0, 0), "valueless": (0, 0)}

    def test_filtered_out_sessions_and_streams_without_requests_give_no_count_entries(
        self, tmp_path
    ):
        records = [
            _raw_request("s1", {"stringValue": "abc"}, query_source="sdk"),
            _raw_request("s2", {"stringValue": "abc"}, query_source="other"),
        ]
        points = [_raw_point("s1", asDouble="x"), _raw_point("s2", asDouble="x")]

        report = _counted_report(
            tmp_path, records, points, where=(("query_source", "sdk"),)
        )

        assert _counts(report) == {"s1": (1, 1)}

        quiet = tmp_path / "quiet"
        quiet.mkdir()
        empty = _counted_report(
            quiet, [_raw_request("s1", {"stringValue": "abc"}, event="user_prompt")], points
        )
        assert empty.metrics_check == ()

    def test_missing_metrics_file_still_carries_request_cost_counts(self, tmp_path):
        records = [_raw_request("s1", _VALID_COST), _raw_request("s1", {"stringValue": "abc"})]

        report = _counted_report(tmp_path, records, None)

        assert report.metrics_state is stream_usage.ReadState.NOTHING_RECEIVED
        assert _counts(report) == {"s1": (1, 0)}
        assert report.metrics_check[0].metric_cost_usd is None

    def test_unreadable_lines_are_not_counted_as_invalid_values(self, tmp_path):
        records = [_raw_request("s1", _VALID_COST), _raw_request("s1", {"stringValue": "abc"})]
        _write_stream(
            tmp_path,
            ["not json {", _raw_logs_export_line(records), '"a string"'],
            [
                "also not json",
                _metrics_line(_cost_metric([_raw_point("s1", asDouble="abc")])),
                '{"body": {}}',
            ],
        )

        report = stream_usage.read_report(stream_usage.build_reader(tmp_path))

        assert (report.skipped_lines, report.skipped_metric_lines) == (2, 2)
        assert _counts(report) == {"s1": (1, 1)}

    def test_diagnostics_are_independent_between_reads(self, tmp_path):
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        invalid = _counted_report(
            first,
            [_raw_request("s1", {"stringValue": "abc"})],
            [_raw_point("s1", asDouble="abc")],
        )
        clean = _counted_report(
            second, [_raw_request("s1", _VALID_COST)], [_raw_point("s1", asDouble=0.4)]
        )
        again = stream_usage.read_report(stream_usage.build_reader(first))

        assert _counts(invalid) == {"s1": (1, 1)}
        assert _counts(clean) == {"s1": (0, 0)}
        assert _counts(again) == {"s1": (1, 1)}

    def test_unopenable_metrics_make_no_checks_and_no_count_only_result(self, tmp_path):
        line = _raw_logs_export_line([_raw_request("s1", {"stringValue": "abc"})])
        _write_stream(tmp_path, [line])
        (tmp_path / "metrics.jsonl").mkdir()

        result = stream_usage.read_report_result(stream_usage.build_reader(tmp_path))

        assert result.state is stream_usage.ReadState.READ
        assert result.report.metrics_state is stream_usage.ReadState.UNOPENABLE
        assert result.report.metrics_check is None

    def test_uninspectable_metrics_make_no_checks_and_no_count_only_result(self, tmp_path):
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        line = _raw_logs_export_line([_raw_request("s1", {"stringValue": "abc"})])
        _write_stream(sealed, [line])

        class SealedMetrics(stream_usage.Files):
            def read_outcome(self, path):
                if path.name == stream_usage.METRICS_FILE:
                    return stream_usage.ReadOutcome(
                        state=stream_usage.ReadState.UNINSPECTABLE, reason="denied"
                    )
                return super().read_outcome(path)

        reader = stream_usage.Reader(files=SealedMetrics(), directory=sealed)
        report = stream_usage.read_report(reader)

        assert report.metrics_state is stream_usage.ReadState.UNINSPECTABLE
        assert report.metrics_check is None


# ── The Agent-tool agents rows count the values they left out ───────────────

COMPLETED_FIELDS = ("duration_ms", "total_tool_uses")
COUNT_PROPERTIES = {
    "duration_ms": "invalid_duration_ms_count",
    "total_tool_uses": "invalid_total_tool_uses_count",
}


def _completed_attributes(**envelopes) -> list[dict]:
    """A `subagent_completed` record's attribute list, each value in the envelope the test names."""
    attributes = [
        _attr("event.name", "subagent_completed"),
        _attr("session.id", "s1"),
        _attr("agent_type", "Explore"),
    ]
    attributes += [{"key": key, "value": envelope} for key, envelope in envelopes.items()]
    return attributes


def _rows_read(*attribute_lists: list[dict]) -> tuple[stream_usage.SubagentRow, ...]:
    read = stream_usage.read_log_lines([_raw_logs_line(list(attribute_lists))])
    assert read.skipped == 0
    return stream_usage.subagent_rows(read.records)


def _row_counts(row: stream_usage.SubagentRow) -> tuple[int, int]:
    return row.invalid_duration_ms_count, row.invalid_total_tool_uses_count


class TestSubagentRowCounts:
    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    def test_a_non_parsing_string_beside_a_valid_value_is_counted_for_that_field_only(self, field):
        other = next(name for name in COMPLETED_FIELDS if name != field)
        (row,) = _rows_read(
            _completed_attributes(**{field: {"intValue": 40}, other: {"intValue": 2}}),
            _completed_attributes(**{field: {"stringValue": "slow"}, other: {"intValue": 3}}),
        )

        assert getattr(row, field) == 40
        assert getattr(row, COUNT_PROPERTIES[field]) == 1
        assert getattr(row, COUNT_PROPERTIES[other]) == 0
        assert row.count == 2

    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    @pytest.mark.parametrize("bad", [True, False], ids=["true", "false"])
    def test_a_bool_value_is_counted_for_each_field_separately(self, field, bad):
        (row,) = _rows_read(
            _completed_attributes(**{field: {"intValue": 9}}),
            _completed_attributes(**{field: {"boolValue": bad}}),
        )

        assert getattr(row, field) == 9
        assert getattr(row, COUNT_PROPERTIES[field]) == 1

    @pytest.mark.parametrize(
        "envelope",
        [
            pytest.param({"stringValue": ""}, id="empty-string"),
            pytest.param(None, id="explicit-null"),
            pytest.param({"intValue": "abc"}, id="int-value-holding-no-number"),
            pytest.param({"unknownValue": 3}, id="object-with-none-of-the-four-keys"),
            pytest.param([1, 2], id="list"),
            pytest.param({"nested": {"a": 1}}, id="object"),
        ],
    )
    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    def test_each_non_number_shape_is_counted(self, field, envelope):
        (row,) = _rows_read(
            _completed_attributes(**{field: {"intValue": 4}}),
            _completed_attributes(**{field: envelope}),
        )

        assert getattr(row, field) == 4
        assert getattr(row, COUNT_PROPERTIES[field]) == 1

    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    def test_valid_numbers_and_numeric_strings_add_to_the_figure_with_no_count(self, field):
        values = [0, -3, 2.5, "43", "2.5", "1e3", "7"]
        records = [_subagent_completed(**{field: value}) for value in values]

        (row,) = stream_usage.subagent_rows(tuple(records))

        assert getattr(row, field) == pytest.approx(0 - 3 + 2.5 + 43 + 2.5 + 1000 + 7)
        assert getattr(row, COUNT_PROPERTIES[field]) == 0

    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    def test_nan_and_inf_strings_are_read_as_today_with_no_finite_number_rule(self, field):
        record_nan = _subagent_completed(**{field: "nan"})
        record_inf = _subagent_completed(**{field: "inf"})

        (row,) = stream_usage.subagent_rows((record_nan, record_inf))

        assert getattr(row, COUNT_PROPERTIES[field]) == 0
        read_as_today = stream_usage.as_number("nan") + stream_usage.as_number("inf")
        assert str(getattr(row, field)) == str(read_as_today)

    def test_an_invalid_record_still_counts_toward_the_row_count(self):
        (row,) = stream_usage.subagent_rows(
            (_subagent_completed(), _subagent_completed(duration_ms="slow"))
        )

        assert row.count == 2
        assert _row_counts(row) == (1, 0)

    def test_absent_field_adds_nothing_and_counts_nothing(self):
        no_duration = _subagent_completed(total_tool_uses=2)
        del no_duration.record_attrs["duration_ms"]
        no_tools = _subagent_completed(duration_ms=40)
        del no_tools.record_attrs["total_tool_uses"]

        (row,) = stream_usage.subagent_rows(
            (_subagent_completed(duration_ms=100, total_tool_uses=3), no_duration, no_tools)
        )

        assert (row.duration_ms, row.total_tool_uses) == (140, 5)
        assert _row_counts(row) == (0, 0)

    @pytest.mark.parametrize("field", COMPLETED_FIELDS)
    def test_a_field_carried_only_by_resource_attributes_is_absent(self, field):
        record = _record(
            {"event.name": "subagent_completed", "session.id": "s1", "agent_type": "Explore"},
            resource_attrs={field: "abc"},
        )

        (row,) = stream_usage.subagent_rows((record,))

        assert getattr(row, field) == 0
        assert _row_counts(row) == (0, 0)

    def test_all_invalid_or_absent_group_shows_zero_figures(self):
        absent = _subagent_completed(total_tool_uses=True)
        del absent.record_attrs["duration_ms"]

        (row,) = stream_usage.subagent_rows(
            (_subagent_completed(duration_ms="x", total_tool_uses=None), absent)
        )

        assert (row.duration_ms, row.total_tool_uses) == (0, 0)
        assert _row_counts(row) == (1, 2)

    def test_counts_are_kept_per_session_and_agent_type_in_todays_order(self):
        records = (
            _subagent_completed(duration_ms="x", agent_type="b"),
            _subagent_completed(**{"session.id": "s0"}, total_tool_uses="y", agent_type="a"),
            _subagent_completed(agent_type="b"),
            _record({"event.name": "subagent_completed", "duration_ms": "z"}),
        )

        rows = stream_usage.subagent_rows(records)

        assert [(r.session_id, r.agent_type, _row_counts(r)) for r in rows] == [
            (None, None, (1, 0)),
            ("s0", "a", (0, 1)),
            ("sid-1", "b", (1, 0)),
        ]

    def test_other_record_kinds_and_total_tokens_add_no_count(self):
        request = _api_request(duration_ms="slow")
        tokens = _subagent_completed(total_tokens="many")

        (row,) = stream_usage.subagent_rows((request, tokens))

        assert row.count == 1
        assert _row_counts(row) == (0, 0)

    @pytest.mark.parametrize(
        "where",
        [
            pytest.param((("session.id", "s1"),), id="session-filter"),
            # A request-only attribute: the completed records carry none, so a count taken over
            # the records the filter matches would differ from the row's whole-group count.
            pytest.param((("query_source", "main"),), id="request-only-filter"),
        ],
    )
    def test_row_counts_do_not_depend_on_a_where_selection(self, tmp_path, where):
        line = _raw_logs_line(
            [
                _completed_attributes(duration_ms={"stringValue": "slow"}),
                _completed_attributes(total_tool_uses={"boolValue": True}),
                _attr_request_only("s1", query_source="main"),
                _attr_request_only("s1", query_source="other"),
                _attr_request_only("s2", query_source="other"),
            ]
        )
        reader_dir = _write_stream(tmp_path, [line])
        whole = stream_usage.subagent_rows(stream_usage.read_log_lines([line]).records)

        narrowed = stream_usage.read_report(
            stream_usage.build_reader(reader_dir), where=where
        ).subagent_rows

        assert [_row_counts(r) for r in narrowed] == [_row_counts(r) for r in whole] == [(1, 1)]
        assert [r.count for r in narrowed] == [2]

    def test_five_argument_subagent_row_still_builds_with_zero_counts(self):
        row = stream_usage.SubagentRow("s", "Explore", 1, 0, 0)

        assert _row_counts(row) == (0, 0)

    def test_subagent_row_counts_stay_out_of_asdict_and_replace(self):
        clean = stream_usage.SubagentRow("s", "Explore", 1, 5, 2)
        counted = stream_usage.SubagentRow(
            "s", "Explore", 3, 5, 2, duration_ms_invalid=2, total_tool_uses_invalid=1
        )
        keys = ["session_id", "agent_type", "count", "duration_ms", "total_tool_uses"]

        assert list(dataclasses.asdict(clean)) == keys
        assert list(dataclasses.asdict(counted)) == keys
        assert dataclasses.asdict(counted) == dataclasses.asdict(clean) | {"count": 3}
        assert _row_counts(counted) == (2, 1)
        for name in COUNT_PROPERTIES.values():
            with pytest.raises(AttributeError):
                setattr(counted, name, 9)


def _attr_request_only(session: str, *, query_source: str) -> list[dict]:
    """An `api_request` record's attribute list: its session and a `query_source`, which no
    `subagent_completed` record carries."""
    return [
        _attr("event.name", "api_request"),
        _attr("session.id", session),
        _attr("query_source", query_source),
    ]


# ── `--compare`: each side names the values it could not read as numbers ───
#
# CP-667–CP-670, CP-674. A side's figure stays the sum of the values that were numbers and its mean
# stays that sum over the side's runs; beside them the side carries, per summed field, how many of
# its own retained requests held a present value that was not a number.

COMPARE_ARM = "kensai.arm"


def _arm_request(arm: str, session: str = "sid-1", **overrides) -> stream_usage.LogRecord:
    return _api_request(**{COMPARE_ARM: arm, "session.id": session}, **overrides)


def _arm_attributes(arm: str, **envelopes) -> list[dict]:
    """The attributes of one `api_request` of side `arm` whose summed fields carry the envelopes
    given."""
    return [*_request_attributes(**envelopes), _attr(COMPARE_ARM, arm)]


class TestCompareInvalidCounts:
    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    def test_compare_side_counts_each_figure_separately(self, field):
        records = (
            _arm_request("A", "s1"),
            _arm_request("A", "s2", **{field: "abc"}),
            _arm_request("B", "s3"),
        )

        result = stream_usage.compare(records, COMPARE_ARM, "A", "B")

        assert result.a.invalid_values == {field: 1}
        assert result.b.invalid_values == {}

    @pytest.mark.parametrize(
        "envelope",
        [
            pytest.param(None, id="present-null"),
            pytest.param({"boolValue": True}, id="true"),
            pytest.param({"boolValue": False}, id="false"),
            pytest.param({"stringValue": ""}, id="empty-string"),
            pytest.param({"stringValue": "abc"}, id="non-numeric-string"),
            pytest.param({"intValue": "abc"}, id="int-value-holding-no-number"),
            pytest.param({"unknownValue": 3}, id="object-with-none-of-the-four-keys"),
            pytest.param([1, 2], id="list"),
            pytest.param({"nested": {"a": 1}}, id="object"),
        ],
    )
    @pytest.mark.parametrize("field", SUMMED_FIELDS)
    def test_compare_counts_every_unreadable_shape_through_the_real_decoder(
        self, field, envelope
    ):
        line = _raw_logs_line(
            [
                _arm_attributes("A", **{field: envelope}),
                _arm_attributes("A", **{field: {"intValue": 4}}),
            ]
        )

        read = stream_usage.read_log_lines([line])
        result = stream_usage.compare(read.records, COMPARE_ARM, "A", "B")

        assert read.skipped == 0
        assert getattr(result.a.totals, field) == 4
        assert result.a.invalid_values == {field: 1}

    def test_invalid_value_adds_nothing_and_still_counts_toward_runs(self):
        records = (
            _arm_request("A", "s1", cost_usd=0.4),
            _arm_request("A", "s2", cost_usd="abc"),
        )

        side = stream_usage.compare(records, COMPARE_ARM, "A", "B").a

        assert side.run_count == 2
        assert side.totals.cost_usd == pytest.approx(0.4)
        assert side.mean_per_run.cost_usd == pytest.approx(0.2)
        assert side.mean_cost_per_run == pytest.approx(0.2)
        assert side.invalid_values == {"cost_usd": 1}

    def test_all_invalid_or_all_absent_figure_has_zero_total_and_mean(self):
        records = (
            _arm_request("A", "s1", input_tokens="abc"),
            _arm_request("A", "s2", input_tokens=True),
        )
        for record in records:
            del record.record_attrs["output_tokens"]

        side = stream_usage.compare(records, COMPARE_ARM, "A", "B").a

        assert side.run_count == 2
        assert (side.totals.input_tokens, side.mean_per_run.input_tokens) == (0, 0)
        assert (side.totals.output_tokens, side.mean_per_run.output_tokens) == (0, 0)
        assert side.invalid_values == {"input_tokens": 2}

    def test_counts_above_one(self):
        records = tuple(
            _arm_request("A", f"s{n}", duration_ms="slow", cost_usd=True) for n in range(3)
        ) + (_arm_request("A", "s9"),)

        side = stream_usage.compare(records, COMPARE_ARM, "A", "B").a

        assert side.invalid_values == {"cost_usd": 3, "duration_ms": 3}

    def test_counts_cover_only_the_sides_own_retained_records(self):
        other_event = _record(
            {"event.name": "user_prompt", COMPARE_ARM: "A", "cost_usd": "abc"}
        )
        records = (
            _arm_request("A", "s1", speed="normal"),
            _arm_request("A", "s2", speed="fast", cost_usd="abc"),
            _arm_request("C", "s3", speed="normal", cost_usd="abc"),
            other_event,
        )

        result = stream_usage.compare(
            records, COMPARE_ARM, "A", "B", where=(("speed", "normal"),)
        )

        assert result.a.run_count == 1
        assert result.a.invalid_values == {}
        assert result.b is None

    def test_equal_a_and_b_values_give_equal_counts_and_figures(self):
        records = (_arm_request("A", "s1"), _arm_request("A", "s2", cost_usd="abc"))

        result = stream_usage.compare(records, COMPARE_ARM, "A", "A")

        assert result.a == result.b
        assert result.a.invalid_values == result.b.invalid_values == {"cost_usd": 1}

    def test_counts_follow_prefix_run_identity(self):
        records = (
            _api_request(**{"team.arm": "A", "team.run_id": "r1", "session.id": "s1"}),
            _api_request(
                **{"team.arm": "A", "team.run_id": "r1", "session.id": "s2"}, cost_usd="abc"
            ),
        )

        side = stream_usage.compare(records, "team.arm", "A", "B", prefix="team").a

        assert side.run_count == 1
        assert side.mean_per_run.cost_usd == side.totals.cost_usd == pytest.approx(0.1)
        assert side.invalid_values == {"cost_usd": 1}

    @pytest.mark.parametrize("text", ["nan", "inf"])
    def test_non_finite_numeric_strings_keep_today_acceptance_with_no_count(self, text):
        records = (_arm_request("A", "s1", cost_usd=text),)

        side = stream_usage.compare(records, COMPARE_ARM, "A", "B").a

        assert side.invalid_values == {}
        assert math.isnan(side.totals.cost_usd) is (text == "nan")
        assert (side.totals.cost_usd == math.inf) is (text == "inf")

    def test_compare_group_built_with_five_arguments_reads_zero_counts(self):
        totals = stream_usage.CompareTotals(input_tokens=3)
        means = stream_usage.CompareTotals(input_tokens=1.5)
        positional = stream_usage.CompareGroup("A", 2, totals, means, 0.25)
        keyword = stream_usage.CompareGroup(
            value="A", run_count=2, totals=totals, mean_per_run=means, mean_cost_per_run=0.25
        )

        assert positional == keyword
        assert [f.name for f in dataclasses.fields(positional)][:5] == [
            "value", "run_count", "totals", "mean_per_run", "mean_cost_per_run",
        ]
        assert (positional.value, positional.run_count) == ("A", 2)
        assert positional.mean_cost_per_run == 0.25
        assert [positional.invalid_values.get(field, 0) for field in SUMMED_FIELDS] == [0] * 6

    def test_default_count_dictionaries_are_independent(self):
        totals = stream_usage.CompareTotals()
        first = stream_usage.CompareGroup("A", 1, totals, totals, 0.0)
        second = stream_usage.CompareGroup("B", 1, totals, totals, 0.0)

        first.invalid_values["cost_usd"] = 1

        assert second.invalid_values == {}

    def test_compare_totals_keeps_exactly_six_fields(self):
        assert [f.name for f in dataclasses.fields(stream_usage.CompareTotals)] == list(
            SUMMED_FIELDS
        )

    def test_a_side_and_its_comparison_stay_hashable_as_before(self):
        """CP-674 and the spec's Gap 8: the counts are added, so a side and the comparison holding
        it can still be hashed — a set member or a dict key — as they could before the counts."""
        records = (_arm_request("A", "s1"), _arm_request("A", "s2", cost_usd="abc"))
        result = stream_usage.compare(records, COMPARE_ARM, "A", "A")
        totals = stream_usage.CompareTotals()
        five_arguments = stream_usage.CompareGroup("A", 1, totals, totals, 0.0)

        assert result.a.invalid_values == {"cost_usd": 1}
        assert hash(result.a) == hash(result.b)
        assert len({result.a, result.b}) == 1
        assert {result: "seen"}[result] == "seen"
        assert {five_arguments: "side"}[
            stream_usage.CompareGroup("A", 1, totals, totals, 0.0)
        ] == "side"

    def test_read_report_comparison_carries_the_same_counts_as_compare(self, tmp_path):
        lines = [
            _raw_logs_line(
                [
                    _arm_attributes("A", cost_usd={"stringValue": "abc"}),
                    _arm_attributes("A", input_tokens={"boolValue": True}),
                    _arm_attributes("B", cost_usd={"doubleValue": 0.5}),
                ]
            )
        ]
        _write_stream(tmp_path, lines)

        report = stream_usage.read_report(
            stream_usage.build_reader(tmp_path), comparison=(COMPARE_ARM, "A", "B")
        )

        direct = stream_usage.compare(
            stream_usage.read_log_lines(lines).records, COMPARE_ARM, "A", "B"
        )
        assert report.compare == direct
        assert report.compare.a.invalid_values == {"cost_usd": 1, "input_tokens": 1}
        assert report.compare.b.invalid_values == {}


def _built_side(label: str, **counts) -> stream_usage.CompareGroup:
    """A side built in code: one run, a cost of 0.5 and `counts` as its `invalid_values`."""
    totals = stream_usage.CompareTotals(input_tokens=3, cost_usd=0.5)
    return stream_usage.CompareGroup(
        value=label,
        run_count=1,
        totals=totals,
        mean_per_run=totals,
        mean_cost_per_run=0.5,
        **({"invalid_values": counts} if counts else {}),
    )


class TestCompareCountsRendering:
    def test_clean_side_built_with_zero_counts_prints_as_today(self):
        no_counts = stream_usage.CompareResult(
            "arm", "A", "B", _built_side("A"), _built_side("B"), 0.0, 1.0
        )
        zero_counts = stream_usage.CompareResult(
            "arm",
            "A",
            "B",
            _built_side("A", **dict.fromkeys(SUMMED_FIELDS, 0)),
            _built_side("B", **dict.fromkeys(SUMMED_FIELDS, 0)),
            0.0,
            1.0,
        )

        assert stream_usage.render((), (), None, 0, zero_counts) == stream_usage.render(
            (), (), None, 0, no_counts
        )
        assert stream_usage.render_json((), (), None, 0, zero_counts) == stream_usage.render_json(
            (), (), None, 0, no_counts
        )
        assert "(invalid:" not in stream_usage.render((), (), None, 0, zero_counts)
        assert "invalid_values" not in stream_usage.render_json((), (), None, 0, zero_counts)

    def test_json_count_order_ignores_dictionary_insertion_order(self):
        counts = {name: (n % 2) * 2 for n, name in enumerate(reversed(SUMMED_FIELDS))}
        side = _built_side("A", **counts)
        result = stream_usage.CompareResult("arm", "A", "B", side, _built_side("B"), 0.0, 1.0)

        compared = json.loads(stream_usage.render_json((), (), None, 0, result))["compare"]["a"]

        positive = [name for name in SUMMED_FIELDS if counts[name] > 0]
        assert positive and len(positive) < len(SUMMED_FIELDS)
        assert list(compared)[-2:] == ["mean_per_run", "invalid_values"]
        assert list(compared["invalid_values"]) == positive
        assert compared["invalid_values"] == {name: counts[name] for name in positive}
        assert list(compared["totals"]) == list(SUMMED_FIELDS)
        assert list(compared["mean_per_run"]) == list(SUMMED_FIELDS)
        assert "invalid_values" not in json.loads(
            stream_usage.render_json((), (), None, 0, result)
        )["compare"]["b"]
