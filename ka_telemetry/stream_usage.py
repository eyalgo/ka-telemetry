"""stream_usage — what each agent spent, read off the telemetry stream the receiver wrote.

The receiver (`ka_telemetry.receiver`) appends every request Claude Code sends to `logs.jsonl` and
`metrics.jsonl` in one directory. This module reads those two files, and is the one reader in the
package that reads that stream rather than a transcript. It reads it three ways: a per-agent report
(`read_report`, printed by `main`), a comparison of two values of one attribute (`compare`), and a
totals check that holds each session's request costs against Claude Code's own cost metric
(`metrics_check`, printed alone by `check_main`).

What the stream carries, and what this reader assumes of it:

1. **The logs carry the rows.** An `api_request` record carries `session.id`, `query_source`,
   `agent.name`, `workflow.run_id`, `model`, the four token classes, `cost_usd` and `duration_ms`.
   `query_source` is not a closed set, and this reader assumes none of its values.
2. **The metrics are the check, not the source.** `claude_code.cost.usage` data points are deltas
   (`aggregationTemporality: 1`), so a reader that takes only the last export undercounts: they are
   summed per session across every export.
3. **`subagent_completed` counts Agent-tool agents only.** A Workflow step's own requests emit none.
   Its `total_tokens` is one lumped figure, which this reader never shows beside the four token
   classes it keeps apart everywhere else.
4. **Every value arrives inside a typed envelope**, and the key is not fixed per field: an int64
   may arrive as `{"intValue": "43"}` as easily as `{"intValue": 43}`, and a numeric field has been
   seen arriving as a `stringValue`. A reader that trusts one key per field reads nothing on the
   day the vendor picks the other.

**Labels are the caller's.** No attribute is a label unless the caller names a prefix: a label is
an attribute whose key starts with the prefix and a dot. With a prefix, rows that differ in a label
are kept apart; without one, labels do not exist and nothing is split by them.

**A label is read in the form it was typed, not the form Claude Code delivers it.** Claude Code
hands over each character the turn-on encoded as one NUL and the last four of its six hexadecimal
digits, as measured on what Claude Code delivers. `_shown_value` turns that back into the typed character, once,
and only for a label; everything that groups, selects or compares a label reads its shown value,
and nothing downstream decodes again. A value that is not a label is compared as received.

**No price model, no currency.** `cost_usd` is the figure Claude Code reported; this counts it and
prices nothing itself.

Imports: the standard library and `ka_telemetry.stream_paths`, nothing else. The two stream
modules never import each other.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from ka_telemetry import stream_paths

__version__ = "0.1.0"

EVENT_API_REQUEST = "api_request"
EVENT_SUBAGENT_COMPLETED = "subagent_completed"
LOGS_FILE = "logs.jsonl"
METRICS_FILE = "metrics.jsonl"

# The keys of a row in the JSON report. A label prefix equal to one would put the labels under a key
# the row already has, so such a prefix is refused.
ROW_KEYS = (
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


# ── A value is read whatever envelope it arrives in ──────────────────────────


def decode_value(value: dict[str, Any] | None) -> Any:
    """An OTLP attribute `value` object, decoded from whichever of `stringValue`, `intValue`,
    `doubleValue` or `boolValue` it carries — never assumed to be one fixed key. `intValue` is
    read as an `int` whether it arrived as a JSON number or (OTLP/JSON's other legal shape for an
    int64) a JSON string. A value carrying none of the four keys, that is not itself a dict, or an
    `intValue` string that is not a number, decodes to `None` rather than raising — a record this
    reader cannot make sense of is a record it reports nothing for, not one that stops the read."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        raw = value["intValue"]
        if not isinstance(raw, str):
            return raw
        try:
            return int(raw)
        except ValueError:
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    return None


def as_number(raw: Any) -> int | float:
    """Coerce a decoded attribute value to a number for summing. An `int`/`float` passes through;
    a `str` that parses as one is parsed (the numeric-field-arriving-as-a-string case `decode_value`
    itself cannot rule out); anything else — `None`, a non-numeric string, a `bool` — reads as
    zero. A summary that stops on one malformed field is worse than one that under-counts a
    single field it could not read."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return 0
    return 0


def _flatten_attributes(attrs: list[dict] | None) -> dict[str, Any]:
    """An OTLP `attributes` list — `[{"key": ..., "value": {...}}, ...]` — flattened to
    `{key: decoded value}`. A malformed entry (not a dict, or carrying no `key`) is skipped
    rather than raising."""
    flat: dict[str, Any] = {}
    for attr in attrs or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        if key is None:
            continue
        flat[key] = decode_value(attr.get("value"))
    return flat


@dataclass(frozen=True)
class LogRecord:
    """One OTLP log record, flattened to two attribute maps: its own, and the resource attributes
    of the export batch it arrived in. Kept apart rather than merged, because a label, a filter and
    a comparison all read the record first and the resource second — merging here would discard
    which side a value came from."""

    record_attrs: dict[str, Any]
    resource_attrs: dict[str, Any]


@dataclass(frozen=True)
class LogLines:
    """`read_log_lines`'s whole answer — the records found, and how many lines could not be read
    at all. `skipped` is reported, never silent: a line skipped for being malformed is a
    different fact from a line that legitimately carried no log records."""

    records: tuple[LogRecord, ...]
    skipped: int


def read_log_lines(lines: list[str]) -> LogLines:
    """Parse `logs.jsonl`'s lines into flattened records. A blank line is ignored. A line that is
    not JSON, whose outer object is not a dict, or whose `body` is missing or empty (`{}` — the
    receiver's own shape for an export it had nothing to store) is skipped and counted; it never
    stops the read of the rest. A `body` naming no `resourceLogs`, or a malformed
    `resourceLogs`/`scopeLogs`/`logRecords` entry, is walked past rather than raised on."""
    records: list[LogRecord] = []
    skipped = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            outer = json.loads(line)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if not isinstance(outer, dict):
            skipped += 1
            continue
        body = outer.get("body")
        if not isinstance(body, dict) or not body:
            skipped += 1
            continue
        for resource_log in body.get("resourceLogs") or []:
            if not isinstance(resource_log, dict):
                continue
            resource_attrs = _flatten_attributes(
                (resource_log.get("resource") or {}).get("attributes")
            )
            for scope_log in resource_log.get("scopeLogs") or []:
                if not isinstance(scope_log, dict):
                    continue
                for log_record in scope_log.get("logRecords") or []:
                    if not isinstance(log_record, dict):
                        continue
                    records.append(
                        LogRecord(
                            record_attrs=_flatten_attributes(log_record.get("attributes")),
                            resource_attrs=resource_attrs,
                        )
                    )
    return LogLines(records=tuple(records), skipped=skipped)


# ── A label prefix is chosen, never assumed ──────────────────────────────────


class PrefixProblem(Enum):
    """Why a label prefix is refused. The value is the reason in the library's own words."""

    EMPTY = "it is empty"
    TRAILING_DOT = "it ends with a dot"
    ROW_KEY = "it is the name of a field in each row of the JSON report"


def check_prefix(prefix: str | None) -> PrefixProblem | None:
    """What is wrong with `prefix`, or `None` when it can be used — `None` itself (no labels) is
    always usable. One rule for the library and for the command line: a prefix that is empty, ends
    with a dot, or names a row field cannot be shown as a label key without collision."""
    if prefix is None:
        return None
    if prefix == "":
        return PrefixProblem.EMPTY
    if prefix.endswith("."):
        return PrefixProblem.TRAILING_DOT
    if prefix in ROW_KEYS:
        return PrefixProblem.ROW_KEY
    return None


def validate_prefix(prefix: str | None) -> None:
    """Raise `ValueError` when `check_prefix` finds a problem. The command-line refusals are worded
    for a person who typed `--prefix`; a library caller typed no flag, so this error is its own."""
    problem = check_prefix(prefix)
    if problem is not None:
        raise ValueError(f"invalid label prefix {prefix!r}: {problem.value}")


# ── One row per agent ────────────────────────────────────────────────────────


def _attr(record: LogRecord, key: str) -> Any:
    """`key`'s value read off `record`'s own attributes first, the resource's second — the
    resolution a label, `--where` and `--compare` all share."""
    if key in record.record_attrs:
        return record.record_attrs[key]
    return record.resource_attrs.get(key)


_ESCAPE_MARKER = "\x00"
_ESCAPE_DIGIT_COUNT = 4
_ESCAPE_DIGITS = frozenset("0123456789ABCDEF")
_SURROGATES = range(0xD800, 0xE000)


def _shown_value(received: Any) -> Any:
    """`received` as the person typed it. Each NUL followed by four uppercase hexadecimal digits
    naming a code point outside the surrogates is replaced by that one character; every other part
    is kept as received, and a part that is not an escape never stops the scan of what follows it.
    The scan is a single pass over `received`, so a replacement is never read again. A value that
    is not a string is its own shown value, unchanged in type."""
    if not isinstance(received, str) or _ESCAPE_MARKER not in received:
        return received
    shown: list[str] = []
    position = 0
    while position < len(received):
        character = received[position]
        digits = received[position + 1 : position + 1 + _ESCAPE_DIGIT_COUNT]
        if (
            character == _ESCAPE_MARKER
            and len(digits) == _ESCAPE_DIGIT_COUNT
            and _ESCAPE_DIGITS.issuperset(digits)
            and int(digits, 16) not in _SURROGATES
        ):
            shown.append(chr(int(digits, 16)))
            position += 1 + _ESCAPE_DIGIT_COUNT
        else:
            shown.append(character)
            position += 1
    return "".join(shown)


def _is_label(key: str, prefix: str | None) -> bool:
    """Whether `key` is a label under `prefix` — it starts with the prefix and a dot, exactly and
    case-sensitively. With no prefix nothing is."""
    return prefix is not None and key.startswith(f"{prefix}.")


def _report_value(record: LogRecord, key: str, prefix: str | None) -> Any:
    """`key`'s value as the report reads it: resolved through `_attr` first — record before
    resource — and then, for a label alone, turned into its shown value. The one place a label is
    decoded, so grouping, `--where`, `--compare` and run identity all see the same value."""
    value = _attr(record, key)
    return _shown_value(value) if _is_label(key, prefix) else value


def _label_attrs(record: LogRecord, prefix: str | None) -> dict[str, Any]:
    """Every label this record carries — an attribute whose key starts with `prefix` and a dot,
    exactly and case-sensitively — at its shown value, over the union of keys either side names.
    With no prefix there are no labels."""
    if prefix is None:
        return {}
    keys = {k for k in record.record_attrs if _is_label(k, prefix)}
    keys |= {k for k in record.resource_attrs if _is_label(k, prefix)}
    return {key: _report_value(record, key, prefix) for key in keys}


def _where_matches(
    record: LogRecord, where: tuple[tuple[str, str], ...], prefix: str | None = None
) -> bool:
    """A record lacking `key` never matches — `str(None)` is `"None"`, and `--where KEY=None` must
    not select every record that carries no such key. A label is matched at its shown value, any
    other key at its received one."""
    return all(
        (actual := _report_value(record, key, prefix)) is not None and str(actual) == value
        for key, value in where
    )


def _summed_field(records: tuple[LogRecord, ...], field: str) -> int | float:
    """`field` summed across `records`' own attributes via `as_number` — the one summation
    `agent_rows` and `_compare_group` both perform over the same four token classes, `cost_usd`
    and `duration_ms`, on two different groupings of the same records."""
    return sum(as_number(r.record_attrs.get(field)) for r in records)


def api_request_records(
    records: tuple[LogRecord, ...],
    where: tuple[tuple[str, str], ...] = (),
    *,
    prefix: str | None = None,
) -> tuple[LogRecord, ...]:
    """Every `api_request` record in `records`, `--where`-filtered before anything groups
    them — `compare` filters through this same function, on the same rule. With a `prefix`, a
    `--where` on a label of that prefix matches its shown value; without one, every value is
    matched as received."""
    return tuple(
        r
        for r in records
        if r.record_attrs.get("event.name") == EVENT_API_REQUEST
        and _where_matches(r, where, prefix)
    )


@dataclass(frozen=True)
class AgentRow:
    """One agent's spend, folded from every `api_request` record sharing its key — `session.id`,
    `workflow.run_id`, `query_source` and `agent.name`, further split (never merged) when the
    group's records disagree on a label of the chosen prefix. The four token classes, `cost_usd`
    and `duration_ms` are sums; `models` is the distinct set of models seen, sorted; `labels` is
    empty when no prefix was chosen."""

    session_id: str | None
    workflow_run_id: str | None
    query_source: str | None
    agent_name: str | None
    labels: dict[str, Any]
    models: tuple[str, ...]
    request_count: int
    input_tokens: int | float
    output_tokens: int | float
    cache_read_tokens: int | float
    cache_creation_tokens: int | float
    cost_usd: int | float
    duration_ms: int | float


def agent_rows(
    records: tuple[LogRecord, ...],
    where: tuple[tuple[str, str], ...] = (),
    *,
    prefix: str | None = None,
) -> tuple[AgentRow, ...]:
    """One `AgentRow` per distinct (`session.id`, `workflow.run_id`, `query_source`, `agent.name`,
    label-signature) key among `records`'s `api_request` records, in the order each key is first
    seen. The label signature — the sorted `(key, value)` pairs `_label_attrs` reads — is part of
    the grouping key precisely so two records that would otherwise share a row, but disagree on a
    label, land on two rows instead of one merged (and therefore wrong) row. Raises `ValueError`
    for a prefix `check_prefix` refuses."""
    validate_prefix(prefix)
    groups: dict[tuple, list[LogRecord]] = {}
    for record in api_request_records(records, where, prefix=prefix):
        label_items = tuple(sorted(_label_attrs(record, prefix).items()))
        key = (
            record.record_attrs.get("session.id"),
            record.record_attrs.get("workflow.run_id"),
            record.record_attrs.get("query_source"),
            record.record_attrs.get("agent.name"),
            label_items,
        )
        groups.setdefault(key, []).append(record)

    rows = []
    for key, group in groups.items():
        session_id, workflow_run_id, query_source, agent_name, label_items = key
        seen_models = {
            g.record_attrs.get("model") for g in group if g.record_attrs.get("model") is not None
        }
        models = tuple(sorted(seen_models))
        rows.append(
            AgentRow(
                session_id=session_id,
                workflow_run_id=workflow_run_id,
                query_source=query_source,
                agent_name=agent_name,
                labels=dict(label_items),
                models=models,
                request_count=len(group),
                input_tokens=_summed_field(tuple(group), "input_tokens"),
                output_tokens=_summed_field(tuple(group), "output_tokens"),
                cache_read_tokens=_summed_field(tuple(group), "cache_read_tokens"),
                cache_creation_tokens=_summed_field(tuple(group), "cache_creation_tokens"),
                cost_usd=_summed_field(tuple(group), "cost_usd"),
                duration_ms=_summed_field(tuple(group), "duration_ms"),
            )
        )
    return tuple(rows)


def _by_cost(rows: tuple[AgentRow, ...]) -> tuple[AgentRow, ...]:
    """`rows` highest `cost_usd` first. The sort is stable, so rows that tie keep the order in
    which each one's first record appeared."""
    return tuple(sorted(rows, key=lambda r: r.cost_usd, reverse=True))


# ── The metrics check ────────────────────────────────────────────────────────

METRIC_COST_USAGE = "claude_code.cost.usage"


@dataclass(frozen=True)
class MetricPoint:
    """One `claude_code.cost.usage` datapoint — its attributes, flattened, and its `asDouble` /
    `asInt` value, decoded through `as_number` the same as any other numeric field. A datapoint
    of any other metric name is never turned into one of these — `read_metric_lines` filters
    before this type is ever built."""

    attrs: dict[str, Any]
    value: float


@dataclass(frozen=True)
class MetricLines:
    """`_parse_metric_lines`'s whole answer — the cost datapoints found, and how many lines could
    not be read at all. As `LogLines.skipped` does for the logs, `skipped` counts a line that is
    not an export (not JSON, not an object, no usable `body`); a readable export that holds no cost
    datapoint is a different fact and is not counted."""

    points: tuple[MetricPoint, ...]
    skipped: int


def _parse_metric_lines(lines: list[str] | None) -> MetricLines:
    """Parse `metrics.jsonl`'s lines into `claude_code.cost.usage` datapoints only — every other
    metric name is walked past — and count the lines that are not exports. `lines` of `None` (the
    file does not exist) reads as no points and none skipped. A blank line is ignored. A line that
    is not JSON, whose outer value is not an object, or whose `body` is missing, not an object or
    empty is skipped and counted: the test `read_log_lines` applies to its own lines. Each export
    is one delta reading (`aggregationTemporality: 1`); summing across every export, never reading
    only the last one, is `metrics_check`'s job — this function only decodes what is there."""
    if lines is None:
        return MetricLines(points=(), skipped=0)
    points: list[MetricPoint] = []
    skipped = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            outer = json.loads(line)
        except (ValueError, TypeError):
            skipped += 1
            continue
        body = outer.get("body") if isinstance(outer, dict) else None
        if not isinstance(body, dict) or not body:
            skipped += 1
            continue
        for resource_metric in body.get("resourceMetrics") or []:
            if not isinstance(resource_metric, dict):
                continue
            for scope_metric in resource_metric.get("scopeMetrics") or []:
                if not isinstance(scope_metric, dict):
                    continue
                for metric in scope_metric.get("metrics") or []:
                    if not isinstance(metric, dict) or metric.get("name") != METRIC_COST_USAGE:
                        continue
                    for point in (metric.get("sum") or {}).get("dataPoints") or []:
                        if not isinstance(point, dict):
                            continue
                        raw_value = point.get("asDouble")
                        if raw_value is None:
                            raw_value = point.get("asInt")
                        points.append(
                            MetricPoint(
                                attrs=_flatten_attributes(point.get("attributes")),
                                value=float(as_number(raw_value)),
                            )
                        )
    return MetricLines(points=tuple(points), skipped=skipped)


def read_metric_lines(lines: list[str] | None) -> tuple[MetricPoint, ...]:
    """The `claude_code.cost.usage` datapoints of `metrics.jsonl`'s lines, in export order — the
    points `_parse_metric_lines` finds, without its count. `None` (no file) reads as `()`."""
    return _parse_metric_lines(lines).points


# The causes a session's check cannot tell apart, in the order they are shown. The tool observed
# one thing — no cost datapoint for a session, or two totals over 1% apart — so it names each way
# that can come about as a possibility and claims none. Held once: the text line, the JSON entry
# and the library result all read them from the check.
NO_METRIC_CAUSES = (
    "the session may have ended before Claude Code exported a cost metric for it",
    "the session may still be running",
    "the receiver (ka-telemetry receive) may not have been running when Claude Code sent the "
    "cost metric",
)
MISMATCH_CAUSES = (
    "the session may still be running, or may have ended before Claude Code exported all of it, "
    "so one of its two totals may not have caught up with the other",
    "the receiver (ka-telemetry receive) may not have been running for part of the session, so "
    "some of what Claude Code sent may never have arrived",
    "Claude Code's request costs and its cost metric may really disagree",
)


@dataclass(frozen=True)
class SessionMetricsCheck:
    """One session's cost held against the vendor's own metric — the summed `api_request`
    `cost_usd` from the log stream, and the summed `claude_code.cost.usage` datapoints from the
    metrics stream. `metric_cost_usd` and `within_tolerance` are both `None` for a session with
    requests but no metric export at all — a different fact from a metric that agrees.

    `possible_causes` is derived from those two, never passed: the causes of a missing metric, the
    causes of a total outside the tolerance, or `()` for a session that agrees. Selecting from the
    state the check already holds keeps it following a custom `tolerance`."""

    session_id: str
    log_cost_usd: float
    metric_cost_usd: float | None
    within_tolerance: bool | None
    possible_causes: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if self.metric_cost_usd is None:
            causes = NO_METRIC_CAUSES
        elif self.within_tolerance is False:
            causes = MISMATCH_CAUSES
        else:
            causes = ()
        object.__setattr__(self, "possible_causes", causes)


def metrics_check(
    rows: tuple[AgentRow, ...],
    metric_points: tuple[MetricPoint, ...],
    tolerance: float = 0.01,
) -> tuple[SessionMetricsCheck, ...]:
    """Every session named by `rows`, held against `metric_points` summed per `session.id` —
    across every export the points came from, never only the last. A session absent from
    `metric_points` entirely reports `metric_cost_usd=None`; one present is flagged when the two
    figures differ by more than `tolerance` (relative to the log figure, or to the metric figure
    when the log figure is exactly zero)."""
    log_cost_by_session: dict[str, float] = {}
    for row in rows:
        if row.session_id is None:
            continue
        prior = log_cost_by_session.get(row.session_id, 0.0)
        log_cost_by_session[row.session_id] = prior + row.cost_usd

    metric_cost_by_session: dict[str, float] = {}
    for point in metric_points:
        session_id = point.attrs.get("session.id")
        if session_id is None:
            continue
        prior = metric_cost_by_session.get(session_id, 0.0)
        metric_cost_by_session[session_id] = prior + point.value

    checks = []
    for session_id in sorted(log_cost_by_session):
        log_cost = log_cost_by_session[session_id]
        metric_cost = metric_cost_by_session.get(session_id)
        if metric_cost is None:
            checks.append(SessionMetricsCheck(session_id, log_cost, None, None))
            continue
        denominator = log_cost if log_cost else metric_cost
        relative_gap = abs(metric_cost - log_cost) / denominator if denominator else 0.0
        within = relative_gap <= tolerance
        checks.append(SessionMetricsCheck(session_id, log_cost, metric_cost, within))
    return tuple(checks)


# ── The Agent-tool agent count ───────────────────────────────────────────────

SUBAGENT_COVERAGE_NOTE = (
    "subagent_completed counts Agent-tool agents only — a Workflow step's own requests "
    '(query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so '
    "this count does not cover them."
)


# The completed-agents section's two absences. Both print directly under the coverage note, and they
# are two states: the stream holds no `subagent_completed` record, or it holds some and `--where`
# left none of them in a session the rows show. The second line's words are `change_text.md`'s.
NO_SUBAGENT_RECORDS_LINE = "No subagent_completed records read."
SUBAGENTS_FILTERED_OUT_LINE = (
    "subagent_completed records were read, but none is in a session the rows show. "
    "Run without --where to list them."
)


def subagent_records(records: tuple[LogRecord, ...]) -> tuple[LogRecord, ...]:
    """Every `subagent_completed` record in `records`."""
    return tuple(r for r in records if r.record_attrs.get("event.name") == EVENT_SUBAGENT_COMPLETED)


@dataclass(frozen=True)
class SubagentRow:
    """One `(session.id, agent_type)`'s Agent-tool agent count — `duration_ms` and
    `total_tool_uses` summed across every `subagent_completed` record sharing that key.
    **Carries no `total_tokens`** — that figure is a single lumped total on the source record,
    the one shape this reader forbids beside the four token classes it keeps apart everywhere
    else, so it is never read onto this row at all."""

    session_id: str | None
    agent_type: str | None
    count: int
    duration_ms: int | float
    total_tool_uses: int | float


def subagent_rows(records: tuple[LogRecord, ...]) -> tuple[SubagentRow, ...]:
    """One `SubagentRow` per distinct `(session.id, agent_type)` among `records`'s
    `subagent_completed` records, sorted for a deterministic reading. See `SUBAGENT_COVERAGE_NOTE`
    for what this count does not reach."""
    groups: dict[tuple, list[LogRecord]] = {}
    for record in subagent_records(records):
        key = (record.record_attrs.get("session.id"), record.record_attrs.get("agent_type"))
        groups.setdefault(key, []).append(record)

    rows = [
        SubagentRow(
            session_id=session_id,
            agent_type=agent_type,
            count=len(group),
            duration_ms=_summed_field(tuple(group), "duration_ms"),
            total_tool_uses=_summed_field(tuple(group), "total_tool_uses"),
        )
        for (session_id, agent_type), group in groups.items()
    ]
    return tuple(sorted(rows, key=lambda r: (r.session_id or "", r.agent_type or "")))


# ── `--compare ATTR A B` ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompareTotals:
    """The four token classes, `cost_usd` and `duration_ms`, summed over one `--compare` group's
    `api_request` records — the same figures `AgentRow` sums, over a different grouping."""

    input_tokens: int | float = 0
    output_tokens: int | float = 0
    cache_read_tokens: int | float = 0
    cache_creation_tokens: int | float = 0
    cost_usd: int | float = 0.0
    duration_ms: int | float = 0


@dataclass(frozen=True)
class CompareGroup:
    """One side of a `--compare` — the attribute value it matched, how many runs it covers, its
    summed totals, and each of those totals' mean per run. `mean_cost_per_run` is
    `mean_per_run.cost_usd`, kept by name because the `B − A` and `B / A` figures are taken on
    it."""

    value: str
    run_count: int
    totals: CompareTotals
    mean_per_run: CompareTotals
    mean_cost_per_run: float


@dataclass(frozen=True)
class CompareResult:
    """`compare`'s whole answer. `a`/`b` are `None` exactly when that value matched no records —
    "no runs," never a zero-filled group standing in for absence. `diff` and `ratio` are `None`
    together whenever either side is empty; `ratio` is `None` alone when `a`'s mean is zero,
    since dividing by it would be the exact zero-division this reader refuses."""

    attr: str
    a_value: str
    b_value: str
    a: CompareGroup | None
    b: CompareGroup | None
    diff: float | None
    ratio: float | None


def _run_key(record: LogRecord, prefix: str | None) -> str:
    """A run's identity for the per-run mean — the record's `{prefix}.run_id` (record first,
    resource second) when a prefix is chosen and the record carries one, else its `session.id`."""
    if prefix is not None:
        run_id = _report_value(record, f"{prefix}.run_id", prefix)
        if run_id is not None:
            return str(run_id)
    return str(record.record_attrs.get("session.id"))


def _compare_group(
    records: tuple[LogRecord, ...], attr: str, value: str, prefix: str | None
) -> CompareGroup | None:
    matching = tuple(r for r in records if str(_report_value(r, attr, prefix)) == value)
    if not matching:
        return None
    run_count = len({_run_key(r, prefix) for r in matching})
    totals = CompareTotals(
        input_tokens=_summed_field(matching, "input_tokens"),
        output_tokens=_summed_field(matching, "output_tokens"),
        cache_read_tokens=_summed_field(matching, "cache_read_tokens"),
        cache_creation_tokens=_summed_field(matching, "cache_creation_tokens"),
        cost_usd=_summed_field(matching, "cost_usd"),
        duration_ms=_summed_field(matching, "duration_ms"),
    )
    mean_per_run = CompareTotals(
        **{
            figure.name: (getattr(totals, figure.name) / run_count if run_count else 0.0)
            for figure in fields(CompareTotals)
        }
    )
    return CompareGroup(
        value=value,
        run_count=run_count,
        totals=totals,
        mean_per_run=mean_per_run,
        mean_cost_per_run=mean_per_run.cost_usd,
    )


def compare(
    records: tuple[LogRecord, ...],
    attr: str,
    a_value: str,
    b_value: str,
    where: tuple[tuple[str, str], ...] = (),
    *,
    prefix: str | None = None,
) -> CompareResult:
    """Group `api_request` records — `--where`-filtered first, the same rule `agent_rows` uses —
    by `attr`'s value (record first, resource second; a record without `attr` has the value
    `None`), keeping only the `a_value` and `b_value` groups, and difference their per-run mean
    `cost_usd`. `prefix` decides which attributes are labels, read at their shown value — `attr`
    and the `where` filters included — and what a run is (`_run_key`). Raises `ValueError` for a
    prefix `check_prefix` refuses."""
    validate_prefix(prefix)
    filtered = api_request_records(records, where, prefix=prefix)
    a = _compare_group(filtered, attr, a_value, prefix)
    b = _compare_group(filtered, attr, b_value, prefix)
    diff = None
    ratio = None
    if a is not None and b is not None:
        diff = b.mean_cost_per_run - a.mean_cost_per_run
        if a.mean_cost_per_run:
            ratio = b.mean_cost_per_run / a.mean_cost_per_run
    return CompareResult(
        attr=attr, a_value=a_value, b_value=b_value, a=a, b=b, diff=diff, ratio=ratio
    )


# ── Reading the stream ───────────────────────────────────────────────────────


class ReadState(str, Enum):
    """What one attempt to read a stream file came to. `NOTHING_RECEIVED` and `UNOPENABLE` are the
    two failed reads where looking at the path says which they are: nothing is at it, or something
    is. `UNINSPECTABLE` is a failed read where looking at the path fails as well, so neither is
    known."""

    READ = "read"
    NOTHING_RECEIVED = "nothing_received"
    UNOPENABLE = "unopenable"
    UNINSPECTABLE = "uninspectable"


class Presence(Enum):
    """What inspecting a path, without reading it, establishes."""

    ABSENT = "absent"
    PRESENT = "present"
    UNINSPECTABLE = "uninspectable"


@dataclass(frozen=True)
class ReadOutcome:
    """One read attempt's whole answer: the lines when it read, and otherwise the system's reason
    for the failure, held as data from the one attempt and never recovered by a second."""

    state: ReadState
    lines: tuple[str, ...] = ()
    reason: str | None = None


class Files:
    """Every filesystem touch this reader makes: reading a stream file's lines whole, keeping what
    went wrong when it cannot be read; and telling a path that is absent from one that is there but
    cannot be read."""

    def read_outcome(self, path: Path) -> ReadOutcome:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as failure:
            return ReadOutcome(
                state=_failed_read_state(self._presence(path)),
                reason=failure.strerror or str(failure),
            )
        return ReadOutcome(state=ReadState.READ, lines=tuple(text.splitlines()))

    def read_lines(self, path: Path) -> list[str] | None:
        outcome = self.read_outcome(path)
        return list(outcome.lines) if outcome.state is ReadState.READ else None

    def is_missing(self, path: Path) -> bool:
        """`True` only when inspecting `path` establishes that nothing is there — it or a part of
        its path is not found. A path that exists, and a path that could not be inspected for any
        other reason, are not established absent."""
        return self._presence(path) is Presence.ABSENT

    def _presence(self, path: Path) -> Presence:
        try:
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return Presence.ABSENT
        except OSError:
            return Presence.UNINSPECTABLE
        return Presence.PRESENT


# The two failed reads that keep the system's reason: something is there and cannot be read, or the
# path itself cannot be looked at. An absent file has nothing to say about why.
_FAILED_METRICS_STATES = (ReadState.UNOPENABLE, ReadState.UNINSPECTABLE)


def _failed_read_state(presence: Presence) -> ReadState:
    if presence is Presence.ABSENT:
        return ReadState.NOTHING_RECEIVED
    if presence is Presence.PRESENT:
        return ReadState.UNOPENABLE
    return ReadState.UNINSPECTABLE


@dataclass(frozen=True)
class Reader:
    """The reader's whole environment: the one port it reaches the world through, plus the
    telemetry directory resolved to a concrete `Path` — the one environment reach `build_reader`
    performs itself."""

    files: Files
    directory: Path


def build_reader(directory: Path | str | None = None) -> Reader:
    """Wire the real port. The only place `Files()` is constructed, and the only place the default
    directory is expanded: an explicit directory has a leading `~` expanded, and none given means
    `stream_paths.DEFAULT_STREAM_DIRECTORY` expanded — never an argparse default, never resolved
    again inside a command's own body."""
    chosen = directory if directory is not None else stream_paths.DEFAULT_STREAM_DIRECTORY
    return Reader(files=Files(), directory=Path(chosen).expanduser())


@dataclass(frozen=True)
class StreamReport:
    """`read_report`'s whole answer. `rows` is highest cost first. `metrics_state` is what reading
    `metrics.jsonl` came to, and `metrics_reason` the system's reason in the two failed states
    (`UNOPENABLE`, `UNINSPECTABLE`) and `None` otherwise; a report read off a directory always
    carries a state, and `None` is only a hand-built report that was given none. `metrics_check`
    holds one check per shown session when the file was read or is absent (an absent file gives
    every one its no-metric reading), an empty tuple when there is no session to check, and `None`
    in the two failed states, where no session could be checked. `compare` is `None` unless a
    comparison was asked for. `skipped_metric_lines` counts the unreadable lines of the whole
    `metrics.jsonl`, whatever `where` narrowed, and is 0 when none was read. `subagent_rows` is the
    whole stream's unless a `--where` was given, and then only the sessions `rows` show;
    `subagents_filtered_out` says that the stream holds completed agents and that exactly such a
    `--where` left none of them in a shown session — a fact an empty `subagent_rows` cannot carry,
    because it cannot tell "none read" from "none selected"."""

    rows: tuple[AgentRow, ...]
    subagent_rows: tuple[SubagentRow, ...]
    metrics_check: tuple[SessionMetricsCheck, ...] | None
    skipped_lines: int
    compare: CompareResult | None
    skipped_metric_lines: int = 0
    subagents_filtered_out: bool = False
    metrics_state: ReadState | None = None
    metrics_reason: str | None = None


def _shown_checks(
    records: tuple[LogRecord, ...],
    shown_rows: tuple[AgentRow, ...],
    metric_points: tuple[MetricPoint, ...],
) -> tuple[SessionMetricsCheck, ...]:
    """The metric is a whole session's, so it is held against the session's whole log cost —
    never the `--where`-narrowed rows, which would report a false gap for every session a filter
    narrower than a session touches. The narrowed rows still decide which sessions are shown."""
    shown = {row.session_id for row in shown_rows}
    return tuple(
        check
        for check in metrics_check(agent_rows(records), metric_points)
        if check.session_id in shown
    )


def _shown_sessions_only(
    completed: tuple[SubagentRow, ...], shown_rows: tuple[AgentRow, ...]
) -> tuple[SubagentRow, ...]:
    """The completed-agent rows in a session some agent row shows. A session is kept or dropped as
    a whole and by equality of identity, so a missing identity is held to the same test as any
    other; a request-attribute filter is never replayed against a completed record, which carries
    none of a request's attributes."""
    shown = {row.session_id for row in shown_rows}
    return tuple(row for row in completed if row.session_id in shown)


@dataclass(frozen=True)
class ReportResult:
    """What reading a directory's report came to: the report when `logs.jsonl` was read, and
    otherwise the failed read's state and the system's reason for it."""

    state: ReadState
    report: StreamReport | None = None
    reason: str | None = None


def read_report_result(
    reader: Reader,
    *,
    prefix: str | None = None,
    where: tuple[tuple[str, str], ...] = (),
    comparison: tuple[str, str, str] | None = None,
) -> ReportResult:
    """The report off `reader`'s directory with the state of the one `logs.jsonl` read behind it.
    `comparison` is `(ATTR, A, B)`. Raises `ValueError` for a prefix `check_prefix` refuses, before
    any file is read."""
    validate_prefix(prefix)
    logs = reader.files.read_outcome(reader.directory / LOGS_FILE)
    if logs.state is not ReadState.READ:
        return ReportResult(state=logs.state, reason=logs.reason)

    parsed = read_log_lines(logs.lines)
    rows = _by_cost(agent_rows(parsed.records, where, prefix=prefix))

    metrics_file = reader.files.read_outcome(reader.directory / METRICS_FILE)
    metrics = _parse_metric_lines(
        list(metrics_file.lines) if metrics_file.state is ReadState.READ else None
    )
    checks = None
    if metrics_file.state in (ReadState.READ, ReadState.NOTHING_RECEIVED):
        checks = _shown_checks(parsed.records, rows, metrics.points)

    compare_result = None
    if comparison is not None:
        attr, a_value, b_value = comparison
        compare_result = compare(parsed.records, attr, a_value, b_value, where, prefix=prefix)

    completed = subagent_rows(parsed.records)
    shown_completed = _shown_sessions_only(completed, rows) if where else completed

    report = StreamReport(
        rows=rows,
        subagent_rows=shown_completed,
        metrics_check=checks,
        skipped_lines=parsed.skipped,
        compare=compare_result,
        skipped_metric_lines=metrics.skipped,
        subagents_filtered_out=bool(where and completed and not shown_completed),
        metrics_state=metrics_file.state,
        metrics_reason=(
            metrics_file.reason if metrics_file.state in _FAILED_METRICS_STATES else None
        ),
    )
    return ReportResult(state=ReadState.READ, report=report)


def read_report(
    reader: Reader,
    *,
    prefix: str | None = None,
    where: tuple[tuple[str, str], ...] = (),
    comparison: tuple[str, str, str] | None = None,
) -> StreamReport | None:
    """The whole report off `reader`'s directory, or `None` when `logs.jsonl` could not be read —
    the one answer that means "no stream here", apart from a stream that is empty. `comparison`
    is `(ATTR, A, B)`. Raises `ValueError` for a prefix `check_prefix` refuses, before any file
    is read."""
    return read_report_result(reader, prefix=prefix, where=where, comparison=comparison).report


# ── Presenting it ────────────────────────────────────────────────────────────

# One statement about every cost a report shows: the text reports print it on a line of their own
# and the JSON reports carry it as `cost_estimate_note`. It covers a total, a mean and a difference
# as well as each figure Claude Code reported, so it does not say "as reported" of all of them.
COST_ESTIMATE_NOTE = (
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill."
)
COST_ESTIMATE_NOTE_MEMBER = "cost_estimate_note"

# The JSON member that counts the `metrics.jsonl` lines a report could not read. It stands first.
UNREADABLE_METRIC_LINES_MEMBER = "unreadable_metric_lines"

SKIPPED_LINES_MEMBER = "skipped_lines"

# What a report says when `metrics.jsonl` holds lines it could not read. `agents` already counts
# unreadable `logs.jsonl` lines at the start of its first line, so its extension says "also";
# `check` has no earlier count and says "skipped" alone. Both name the totals check the lines were
# left out of.
AGENTS_METRIC_COVERAGE = (
    "Unreadable lines in metrics.jsonl, also skipped and left out of the totals check: "
    "{count}."
)
CHECK_METRIC_COVERAGE = (
    "Unreadable lines in metrics.jsonl, skipped and left out of the totals check: {count}."
)
CHECK_LOGS_COVERAGE = (
    "Unreadable lines in logs.jsonl, skipped and left out of the totals check: {count}."
)

# What `agents` and `check` say after their no-stream line when nothing is at `logs.jsonl` in the
# directory: what the directory holds, and the two things to check with the command that sets each
# right. A directory can lack the file after receiving a great deal (deleting it is safe), so the
# first line states what was observed and does not claim that nothing was ever received. The JSON
# forms carry these same strings, the no-stream line left out.
NOTHING_RECEIVED_LINES = (
    "This directory holds no requests received from Claude Code yet.",
    "Check that Claude Code's telemetry is turned on: ka-telemetry turn-on writes the settings "
    "that turn it on.",
    "Check that the receiver is running and writing to {directory}: ka-telemetry receive runs it, "
    "and its --dir option sets the directory it writes to.",
)

# What `agents` and `check` say when something is at `logs.jsonl` and reading it fails: the system's
# own words for the failure, after what the tool was doing, and the one action true whatever the
# reason was. The JSON forms carry this statement and the reason on its own.
UNOPENABLE_STATEMENT = (
    "There is a logs.jsonl at {directory}, but ka-telemetry could not read it: {reason}. "
    "Run this command again once logs.jsonl can be read."
)

# What the totals block says when `metrics.jsonl` could not be read, and when it is not there. The
# two failed reads follow the shape of the `logs.jsonl` statements and add that no session was
# checked; the undetermined one claims neither presence nor absence, and its reason is the read's
# alone. The absence line stands above the existing heading and session lines, which say each
# session's possible causes, so it gives no next action. The words are `change_text.md`'s; the JSON
# forms carry the two failed statements and the reason on its own.
METRICS_UNOPENABLE_STATEMENT = (
    "There is a metrics.jsonl at {directory}, but ka-telemetry could not read it: {reason}. "
    "No session was checked in the totals check. "
    "Run this command again once metrics.jsonl can be read."
)
METRICS_UNDETERMINED_STATEMENT = (
    "In {directory}, ka-telemetry could not read metrics.jsonl: {reason}. "
    "It could not tell whether metrics.jsonl is there either. "
    "No session was checked in the totals check. "
    "Run this command again once ka-telemetry can read metrics.jsonl or tell that it is not there."
)
METRICS_ABSENT_LINE = (
    "No metrics.jsonl was found in {directory}, so no session has a cost metric to check against."
)

# The JSON member that states `metrics.jsonl`'s state. It stands directly after the unreadable
# `metrics.jsonl` line count, ahead of everything else.
METRICS_FILE_MEMBER = "metrics_file"

# What `agents` and `check` say when `logs.jsonl` could not be read and looking at its path failed
# too, so neither its presence nor its absence is known. The reason is the read's alone: the failed
# look keeps none of its own. The JSON forms carry this statement and the reason on its own.
UNDETERMINED_LOGS_STATEMENT = (
    "In {directory}, ka-telemetry could not read logs.jsonl: {reason}. "
    "It could not tell whether logs.jsonl is there either. "
    "Run this command again once ka-telemetry can read logs.jsonl or tell that it is not there."
)

# What the per-agent report says when a printed row shows an agent as `custom`: why it reads that
# way, and what lets a person tell such runs apart. It does not tell the reader to turn Claude
# Code's detailed tool logging on, which puts tool inputs into the files the receiver writes.
CUSTOM_AGENT_NAME = "custom"
CUSTOM_AGENT_EXPLANATION = (
    "Claude Code reports an agent you defined yourself as custom, and names it only when Claude "
    "Code's detailed tool logging is on. To tell such agents apart, label your runs with "
    "ka-telemetry turn-on --prefix PREFIX --label KEY=VALUE and read this report with --prefix "
    "PREFIX: rows from runs you labelled differently are shown apart."
)

# What a session line says after `no metric export` or `MISMATCH >1%`: the lead-in, then the
# session's own causes. The lead-in names who cannot tell them apart, so no cause reads as found.
CAUSES_LEAD_IN = "possible causes, which ka-telemetry cannot tell apart: "


def _parse_where(pairs: list[str] | None) -> tuple[tuple[str, str], ...]:
    parsed = []
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"error: --where expects KEY=VALUE, got {pair!r}")
        parsed.append((key, value))
    return tuple(parsed)


def _shows_custom_agent(rows: tuple[AgentRow, ...], sub_rows: tuple[SubagentRow, ...]) -> bool:
    """Whether a row about to be printed names a `custom` agent, matched exactly and
    case-sensitively. Decided from the rows as presented — the agent rows `--where` narrowed and
    the Agent-tool rows it does not — never from the raw records."""
    return any(row.agent_name == CUSTOM_AGENT_NAME for row in rows) or any(
        sub_row.agent_type == CUSTOM_AGENT_NAME for sub_row in sub_rows
    )


def _row_label(row: AgentRow) -> str:
    return (
        f"session={row.session_id} workflow_run_id={row.workflow_run_id} "
        f"query_source={row.query_source} agent={row.agent_name}"
    )


def _causes_text(check: SessionMetricsCheck) -> str:
    """What follows a session line's marker: ` — `, the lead-in and the causes joined by `; `, or
    nothing for a session with none."""
    if not check.possible_causes:
        return ""
    return f" — {CAUSES_LEAD_IN}{'; '.join(check.possible_causes)}"


_FAILED_METRICS_STATEMENTS = {
    ReadState.UNOPENABLE: METRICS_UNOPENABLE_STATEMENT,
    ReadState.UNINSPECTABLE: METRICS_UNDETERMINED_STATEMENT,
}


def _metrics_failure(
    metrics_state: ReadState | None,
    metrics_reason: str | None,
    directory: Path | str | None,
) -> str | None:
    """The one statement for a `metrics.jsonl` that could not be read, or `None` for every other
    state. The text report prints it and the JSON `message` carries it, so both come from here."""
    template = _FAILED_METRICS_STATEMENTS.get(metrics_state)
    if template is None:
        return None
    return template.format(directory=_context_directory(directory), reason=metrics_reason)


def _context_directory(directory: Path | str | None) -> str:
    if directory is None:
        raise ValueError("a directory is needed to state a metrics.jsonl that was not read")
    return str(directory)


def _check_lines(
    checks: tuple[SessionMetricsCheck, ...] | None,
    *,
    metrics_state: ReadState | None = None,
    metrics_reason: str | None = None,
    directory: Path | str | None = None,
) -> list[str]:
    """The totals block, the one place it is worded, for `agents` and for `check` alike. With no
    `metrics_state` (a call that was told nothing about the file) it is one line saying why there
    is no check, or the heading and one line per session. Given the state: one statement when
    `metrics.jsonl` could not be read, the absence line above the heading and the sessions when it
    is not there, and the heading and sessions when it was read. A session the check cannot settle
    names its possible causes on its own line."""
    failure = _metrics_failure(metrics_state, metrics_reason, directory)
    if failure is not None:
        return [failure]
    lines: list[str] = []
    if metrics_state is ReadState.NOTHING_RECEIVED:
        lines.append(METRICS_ABSENT_LINE.format(directory=_context_directory(directory)))
        checks = checks or ()
    elif checks is None:
        return ["No metrics.jsonl found — totals check skipped."]
    lines.append("Totals check (claude_code.cost.usage vs summed cost_usd):")
    if not checks:
        lines.append("  No sessions to check.")
    for check in checks:
        if check.metric_cost_usd is None:
            lines.append(
                f"  session={check.session_id}  log_cost_usd={check.log_cost_usd:.6f}  "
                f"no metric export{_causes_text(check)}"
            )
        elif check.within_tolerance:
            lines.append(
                f"  session={check.session_id}  log_cost_usd={check.log_cost_usd:.6f}  "
                f"metric_cost_usd={check.metric_cost_usd:.6f}"
            )
        else:
            lines.append(
                f"  session={check.session_id}  log_cost_usd={check.log_cost_usd:.6f}  "
                f"metric_cost_usd={check.metric_cost_usd:.6f}  MISMATCH >1%{_causes_text(check)}"
            )
    return lines


def render_checks(
    checks: tuple[SessionMetricsCheck, ...] | None,
    *,
    skipped_metric_lines: int = 0,
    skipped_lines: int | None = None,
    metrics_state: ReadState | None = None,
    metrics_reason: str | None = None,
    directory: Path | str | None = None,
) -> str:
    """The totals block as text, ending in one newline — what `check` prints, and the block
    inside what `agents` prints — framed, as the block itself never is: led by a line stating the
    unreadable `metrics.jsonl` lines when there are any, then one stating the unreadable
    `logs.jsonl` lines when `skipped_lines` is given and not 0, and followed, when it shows at
    least one session, by the cost marking. `metrics_state`, `metrics_reason` and `directory` say
    what reading `metrics.jsonl` came to; the commands always give them, and a call that omits
    them prints what it always has."""
    lines = _check_lines(
        checks, metrics_state=metrics_state, metrics_reason=metrics_reason, directory=directory
    )
    if skipped_lines:
        lines.insert(0, CHECK_LOGS_COVERAGE.format(count=skipped_lines))
    if skipped_metric_lines:
        lines.insert(0, CHECK_METRIC_COVERAGE.format(count=skipped_metric_lines))
    if checks:
        lines.append(COST_ESTIMATE_NOTE)
    return "\n".join(lines) + "\n"


def _checks_payload(checks: tuple[SessionMetricsCheck, ...] | None) -> list[dict[str, Any]] | None:
    if checks is None:
        return None
    return [
        {
            "session_id": c.session_id,
            "log_cost_usd": c.log_cost_usd,
            "metric_cost_usd": c.metric_cost_usd,
            "within_tolerance": c.within_tolerance,
            "possible_causes": list(c.possible_causes),
        }
        for c in checks
    ]


def _metrics_file_member(
    metrics_state: ReadState | None,
    metrics_reason: str | None,
    directory: Path | str | None,
) -> dict[str, Any] | None:
    """`metrics_file`: the state alone for a file that was read or is absent, and for the two
    failed reads the state, the system's reason and the statement the text report prints. `None`
    for a call that was given no state."""
    if metrics_state is None:
        return None
    member: dict[str, Any] = {"state": metrics_state.value}
    failure = _metrics_failure(metrics_state, metrics_reason, directory)
    if failure is not None:
        member["reason"] = metrics_reason
        member["message"] = failure
    return member


def _with_leading_members(
    payload: dict[str, Any],
    skipped_metric_lines: int | None,
    metrics_file: dict[str, Any] | None,
    skipped_lines: int | None = None,
) -> dict[str, Any]:
    """`payload` led by the unreadable-`metrics.jsonl`-line count when one is given — `0` included
    — then by `metrics_file`, then by the unreadable-`logs.jsonl`-line count, each when it is
    given, and `payload` itself when none is, so a direct renderer call keeps its old output."""
    leading: dict[str, Any] = {}
    if skipped_metric_lines is not None:
        leading[UNREADABLE_METRIC_LINES_MEMBER] = skipped_metric_lines
    if metrics_file is not None:
        leading[METRICS_FILE_MEMBER] = metrics_file
    if skipped_lines is not None:
        leading[SKIPPED_LINES_MEMBER] = skipped_lines
    return {**leading, **payload}


def render_checks_json(
    checks: tuple[SessionMetricsCheck, ...] | None,
    *,
    skipped_metric_lines: int | None = None,
    skipped_lines: int | None = None,
    metrics_state: ReadState | None = None,
    metrics_reason: str | None = None,
    directory: Path | str | None = None,
) -> str:
    """The totals block as JSON — what `check --json` prints, and the `metrics_check` value
    inside what `agents --json` prints. A non-empty check carries the cost marking after it; an
    absent or empty one shows no cost and keeps the bare payload. `skipped_metric_lines`, when
    given, is the first member, `metrics_file` the one after it, and `skipped_lines` the one after
    that; the commands always give all three. A `metrics.jsonl` that could not be read has no
    `metrics_check` member: no session was checked, which `null` would not say."""
    payload: dict[str, Any] = {}
    if metrics_state not in _FAILED_METRICS_STATEMENTS:
        payload["metrics_check"] = _checks_payload(checks)
    if checks:
        payload[COST_ESTIMATE_NOTE_MEMBER] = COST_ESTIMATE_NOTE
    metrics_file = _metrics_file_member(metrics_state, metrics_reason, directory)
    return json.dumps(
        _with_leading_members(payload, skipped_metric_lines, metrics_file, skipped_lines),
        indent=2,
    )


def render(
    rows: tuple[AgentRow, ...],
    sub_rows: tuple[SubagentRow, ...],
    checks: tuple[SessionMetricsCheck, ...] | None,
    skipped_lines: int,
    compare_result: CompareResult | None,
    *,
    prefix: str | None = None,
    skipped_metric_lines: int = 0,
    subagents_filtered_out: bool = False,
    metrics_state: ReadState | None = None,
    metrics_reason: str | None = None,
    directory: Path | str | None = None,
) -> str:
    """The human table — agent rows led by `cost_usd`, highest first, with the total stated;
    the Agent-tool agent count, always carrying `SUBAGENT_COVERAGE_NOTE`; the totals check, or a
    stated reason it did not run; and, when asked for, the `--compare` block. Every section
    states its own absence in words rather than rendering an empty table. A row that carries
    labels gets one more line, headed by the chosen `prefix`. Unreadable `metrics.jsonl` lines, when
    there are any, are stated at the end of the first line, after the unchanged `logs.jsonl`
    count. With `subagents_filtered_out`, an empty completed-agents section says a `--where` left
    nothing in it, rather than that nothing was read. `metrics_state`, `metrics_reason` and
    `directory` say what reading `metrics.jsonl` came to, as `render_checks` takes them."""
    validate_prefix(prefix)
    ordered = _by_cost(rows)
    total_cost = sum(r.cost_usd for r in ordered)

    first = f"{len(ordered)} agent row(s), {skipped_lines} unreadable line(s) skipped."
    if skipped_metric_lines:
        first += " " + AGENTS_METRIC_COVERAGE.format(count=skipped_metric_lines)
    lines: list[str] = [first]
    for row in ordered:
        lines.append(
            f"  {_row_label(row)}  requests={row.request_count}  "
            f"models={','.join(row.models) or '(none)'}  cost_usd={row.cost_usd:.6f}"
        )
        lines.append(
            f"    input={row.input_tokens} output={row.output_tokens} "
            f"cache_read={row.cache_read_tokens} cache_creation={row.cache_creation_tokens} "
            f"duration_ms={row.duration_ms}"
        )
        if prefix is not None and row.labels:
            label_text = ", ".join(f"{k}={v}" for k, v in sorted(row.labels.items()))
            lines.append(f"    {prefix}: {label_text}")
    lines.append(f"Total cost_usd: {total_cost:.6f}")
    lines.append(COST_ESTIMATE_NOTE)
    if _shows_custom_agent(ordered, sub_rows):
        lines.append(CUSTOM_AGENT_EXPLANATION)
    lines.append("")

    lines.append(SUBAGENT_COVERAGE_NOTE)
    if not sub_rows:
        lines.append(
            SUBAGENTS_FILTERED_OUT_LINE if subagents_filtered_out else NO_SUBAGENT_RECORDS_LINE
        )
    for sub_row in sub_rows:
        lines.append(
            f"  session={sub_row.session_id} agent_type={sub_row.agent_type} "
            f"count={sub_row.count} duration_ms={sub_row.duration_ms} "
            f"total_tool_uses={sub_row.total_tool_uses}"
        )
    lines.append("")

    lines.extend(
        _check_lines(
            checks, metrics_state=metrics_state, metrics_reason=metrics_reason, directory=directory
        )
    )
    lines.append("")

    if compare_result is not None:
        lines.extend(_render_compare(compare_result))

    return "\n".join(lines).rstrip() + "\n"


def _compare_figures_text(figures: CompareTotals) -> str:
    """The four token classes, `cost_usd` and `duration_ms` on one line — a summed count prints
    as the integer it is, a per-run mean to two places, and cost to six, as everywhere else."""

    def count(value: int | float) -> str:
        return str(value) if isinstance(value, int) else f"{value:.2f}"

    return (
        f"input={count(figures.input_tokens)} output={count(figures.output_tokens)} "
        f"cache_read={count(figures.cache_read_tokens)} "
        f"cache_creation={count(figures.cache_creation_tokens)} "
        f"cost_usd={figures.cost_usd:.6f} duration_ms={count(figures.duration_ms)}"
    )


def _render_compare(result: CompareResult) -> list[str]:
    lines = [f"--compare {result.attr} {result.a_value} {result.b_value}:"]
    for group, label in ((result.a, result.a_value), (result.b, result.b_value)):
        if group is None:
            lines.append(f"  {label}: no runs")
        else:
            lines.append(
                f"  {label}: runs={group.run_count}  "
                f"mean_cost_per_run={group.mean_cost_per_run:.6f}  "
                f"total_cost_usd={group.totals.cost_usd:.6f}"
            )
            lines.append(f"    totals:  {_compare_figures_text(group.totals)}")
            lines.append(f"    per run: {_compare_figures_text(group.mean_per_run)}")
    if result.diff is not None:
        lines.append(f"  {result.b_value} - {result.a_value} = {result.diff:.6f}")
        if result.ratio is not None:
            lines.append(f"  {result.b_value} / {result.a_value} = {result.ratio:.6f}")
        else:
            lines.append(f"  {result.b_value} / {result.a_value}: ratio undefined (zero mean)")
    return lines


def _compare_group_json(group: CompareGroup | None) -> dict[str, Any] | None:
    """One `--compare` side keyed for `--json` — the figures the table prints, or `None` for a
    value that matched no runs."""
    if group is None:
        return None
    return {
        "run_count": group.run_count,
        "mean_cost_per_run": group.mean_cost_per_run,
        "total_cost_usd": group.totals.cost_usd,
        "totals": asdict(group.totals),
        "mean_per_run": asdict(group.mean_per_run),
    }


def _row_json(row: AgentRow, prefix: str | None) -> dict[str, Any]:
    """One row keyed for `--json`. With a prefix, its labels sit under that prefix as the key,
    between `agent_name` and `models` — never under a fixed name of this module's own."""
    entry: dict[str, Any] = {
        "session_id": row.session_id,
        "workflow_run_id": row.workflow_run_id,
        "query_source": row.query_source,
        "agent_name": row.agent_name,
    }
    if prefix is not None:
        entry[prefix] = row.labels
    entry.update(
        {
            "models": list(row.models),
            "request_count": row.request_count,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_creation_tokens": row.cache_creation_tokens,
            "cost_usd": row.cost_usd,
            "duration_ms": row.duration_ms,
        }
    )
    return entry


def render_json(
    rows: tuple[AgentRow, ...],
    sub_rows: tuple[SubagentRow, ...],
    checks: tuple[SessionMetricsCheck, ...] | None,
    skipped_lines: int,
    compare_result: CompareResult | None,
    *,
    prefix: str | None = None,
    skipped_metric_lines: int | None = None,
    metrics_state: ReadState | None = None,
    metrics_reason: str | None = None,
    directory: Path | str | None = None,
) -> str:
    """The machine rows — the same figures `render` prints, keyed rather than columned, and the
    cost marking as the last member. `skipped_metric_lines`, when given, is the first member and
    `metrics_file` the one after it; the commands always give both, as `render_checks_json` takes
    them. A `metrics.jsonl` that could not be read leaves out `metrics_check`."""
    validate_prefix(prefix)
    ordered = _by_cost(rows)
    payload: dict[str, Any] = {
        "skipped_lines": skipped_lines,
        "total_cost_usd": sum(r.cost_usd for r in ordered),
        "rows": [_row_json(row, prefix) for row in ordered],
        "subagent_coverage_note": SUBAGENT_COVERAGE_NOTE,
        "subagent_rows": [
            {
                "session_id": r.session_id,
                "agent_type": r.agent_type,
                "count": r.count,
                "duration_ms": r.duration_ms,
                "total_tool_uses": r.total_tool_uses,
            }
            for r in sub_rows
        ],
    }
    if metrics_state not in _FAILED_METRICS_STATEMENTS:
        payload["metrics_check"] = _checks_payload(checks)
    if compare_result is not None:
        payload["compare"] = {
            "attr": compare_result.attr,
            "a_value": compare_result.a_value,
            "b_value": compare_result.b_value,
            "a": _compare_group_json(compare_result.a),
            "b": _compare_group_json(compare_result.b),
            "diff": compare_result.diff,
            "ratio": compare_result.ratio,
        }
    payload[COST_ESTIMATE_NOTE_MEMBER] = COST_ESTIMATE_NOTE
    metrics_file = _metrics_file_member(metrics_state, metrics_reason, directory)
    return json.dumps(_with_leading_members(payload, skipped_metric_lines, metrics_file), indent=2)


# ── The two commands ─────────────────────────────────────────────────────────

_DIR_HELP = (
    "the directory the receiver writes to, holding logs.jsonl and metrics.jsonl "
    "(default: ~/.ka-telemetry)"
)

_PREFIX_REFUSALS = {
    PrefixProblem.EMPTY: lambda prefix: (
        "--prefix is empty; give the text your label keys start with, without the dot after "
        "it — for labels such as team.run_id, use --prefix team"
    ),
    PrefixProblem.TRAILING_DOT: lambda prefix: (
        f"--prefix {prefix!r} ends with a dot; give it without the dot — for labels such as "
        "team.run_id, use --prefix team"
    ),
    PrefixProblem.ROW_KEY: lambda prefix: (
        f"--prefix {prefix!r} is also the name of a field in each row of the --json output, so "
        "labels under it cannot be shown; use --where to select requests by those attributes "
        "instead"
    ),
}


class _CommandParser(argparse.ArgumentParser):
    """The parser a `ka-telemetry` command builds: an option may not be abbreviated, and a
    usage error ends with the next step to take, on its own last line. The stream modules import
    nothing from each other or from the transcript readers, so each builds this itself."""

    def __init__(self, *, next_step: str, **kwargs) -> None:
        super().__init__(allow_abbrev=False, **kwargs)
        self._next_step = next_step

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{self._next_step}\n")


def _agents_parser() -> _CommandParser:
    parser = _CommandParser(
        prog="ka-telemetry agents",
        next_step="Run `ka-telemetry agents --help` to see what it accepts.",
        description=(
            "Reports the tokens, cost and duration of each agent, read from the files the "
            "receiver writes. Each row is one session, workflow run, query source and agent, "
            "highest cost first, with its requests, its models, its cost and the four token "
            "classes (input, output, cache_read, cache_creation), which are never added "
            "together. Every cost in it comes from the cost_usd Claude Code reported with each "
            "request. Under the rows come the Agent-tool agents Claude Code reported as "
            "completed, then the totals check that ka-telemetry check prints, for the sessions "
            "the rows show."
        ),
    )
    parser.add_argument("--dir", metavar="DIR", help=_DIR_HELP)
    parser.add_argument(
        "--prefix",
        metavar="PREFIX",
        help="read every attribute whose key starts with PREFIX and a dot, such as "
        "PREFIX.run_id, as one of your labels: rows that differ in a label are shown apart, "
        "each row lists its labels, and --compare counts a run as one PREFIX.run_id value, or "
        "as one session where a request has none. Without this flag, no attribute is a label.",
    )
    parser.add_argument(
        "--where",
        action="append",
        metavar="KEY=VALUE",
        help="keep only the requests whose attribute KEY equals VALUE, looked up on the "
        "request first and its resource second; repeat the flag to require every pair",
    )
    parser.add_argument(
        "--compare",
        nargs=3,
        metavar=("ATTR", "A", "B"),
        help="compare the requests whose attribute ATTR is A with those whose ATTR is B: runs, "
        "totals and mean per run for each, then B minus A and B divided by A of the mean cost "
        "per run. A request without ATTR counts as having the value None.",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON instead of a table"
    )
    return parser


def _check_parser() -> _CommandParser:
    parser = _CommandParser(
        prog="ka-telemetry check",
        next_step="Run `ka-telemetry check --help` to see what it accepts.",
        description=(
            "Prints the totals check for each session that has at least one request in the "
            "files the receiver writes: the cost_usd Claude Code reported with each request, "
            "added up, against Claude Code's claude_code.cost.usage metric, added up over every "
            "export. A session whose two costs differ by more than 1% of the request total (of "
            "the metric total, when the request total is 0) is marked MISMATCH >1%, and a "
            "session with no data point in that metric is marked no metric export. Neither is "
            "an error: the command exits 0."
        ),
    )
    parser.add_argument("--dir", metavar="DIR", help=_DIR_HELP)
    parser.add_argument(
        "--json", action="store_true", help="print the check as JSON instead of a table"
    )
    return parser


def _no_stream_line(reader: Reader) -> str:
    return f"No stream at {reader.directory} — no {LOGS_FILE} there."


def _unread_state_payload(reader: Reader, result: ReportResult) -> dict[str, Any]:
    """The document for a directory whose `logs.jsonl` could not be read, for each of the three
    states that name themselves — the one source both commands, and both forms, draw their words
    from."""
    directory = str(reader.directory)
    payload: dict[str, Any] = {"state": result.state.value, "directory": directory}
    if result.state is ReadState.NOTHING_RECEIVED:
        statement, *checks = (line.format(directory=directory) for line in NOTHING_RECEIVED_LINES)
        payload["message"] = statement
        payload["checks"] = checks
    else:
        template = (
            UNOPENABLE_STATEMENT
            if result.state is ReadState.UNOPENABLE
            else UNDETERMINED_LOGS_STATEMENT
        )
        payload["message"] = template.format(directory=directory, reason=result.reason)
        payload["reason"] = result.reason
    return payload


def _print_unread_state(reader: Reader, result: ReportResult, *, as_json: bool) -> None:
    """What a command prints when `logs.jsonl` could not be read: the document in `--json`, and in
    text the same statement, led by the no-stream line only where nothing is at the path."""
    payload = _unread_state_payload(reader, result)
    if as_json:
        print(json.dumps(payload, indent=2))
    elif result.state is ReadState.NOTHING_RECEIVED:
        print("\n".join([_no_stream_line(reader), payload["message"], *payload["checks"]]))
    else:
        print(payload["message"])


def main(argv: list[str] | None = None) -> int:
    """`ka-telemetry agents`: the per-agent report. A usage error exits 2, and a `--where` with no
    `=` exits 1 — both before the directory is looked at, so they hold with or without a stream."""
    parser = _agents_parser()
    args = parser.parse_args(argv)
    problem = check_prefix(args.prefix)
    if problem is not None:
        parser.error(_PREFIX_REFUSALS[problem](args.prefix))
    where = _parse_where(args.where)

    reader = build_reader(args.dir)
    comparison = tuple(args.compare) if args.compare else None
    result = read_report_result(reader, prefix=args.prefix, where=where, comparison=comparison)
    report = result.report
    if report is None:
        _print_unread_state(reader, result, as_json=args.json)
        return 0

    shown = (
        report.rows,
        report.subagent_rows,
        report.metrics_check,
        report.skipped_lines,
        report.compare,
    )
    metrics_file = {
        "metrics_state": report.metrics_state,
        "metrics_reason": report.metrics_reason,
        "directory": reader.directory,
    }
    if args.json:
        print(
            render_json(
                *shown,
                prefix=args.prefix,
                skipped_metric_lines=report.skipped_metric_lines,
                **metrics_file,
            )
        )
    else:
        print(
            render(
                *shown,
                prefix=args.prefix,
                skipped_metric_lines=report.skipped_metric_lines,
                subagents_filtered_out=report.subagents_filtered_out,
                **metrics_file,
            ),
            end="",
        )
    return 0


def check_main(argv: list[str] | None = None) -> int:
    """`ka-telemetry check`: the totals check alone, the same block `agents` prints."""
    args = _check_parser().parse_args(argv)

    reader = build_reader(args.dir)
    result = read_report_result(reader)
    report = result.report
    if report is None:
        _print_unread_state(reader, result, as_json=args.json)
        return 0

    shown = {
        "skipped_metric_lines": report.skipped_metric_lines,
        "skipped_lines": report.skipped_lines,
        "metrics_state": report.metrics_state,
        "metrics_reason": report.metrics_reason,
        "directory": reader.directory,
    }
    if args.json:
        print(render_checks_json(report.metrics_check, **shown))
    else:
        print(render_checks(report.metrics_check, **shown), end="")
    return 0
