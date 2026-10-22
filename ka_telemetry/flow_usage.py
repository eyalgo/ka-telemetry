"""flow_usage — what a workflow run spent, one row per step.

`run_usage` reads what a background run spent, but it cannot reach a workflow's own steps: its
population is the `claude agents --json --all` rows, which name no step agent. A workflow's agents
live one level deeper, at `<projects_root>/<slug>/<sessionId>/subagents/workflows/<wf_id>/`,
reached through the parent session's directory. Two populations, two joins, one unit apiece: a
run there, a step here.

What the transcripts and journals on disk look like, as measured:

1. Each workflow run holds `agent-<agentId>.jsonl`, `agent-<agentId>.meta.json` and one
   `journal.jsonl`. The flat sibling `<sessionId>/subagents/agent-*.jsonl` holds subagents of the
   agent tool: the same record shape, no journal, no workflow. This reader never reaches it.
2. `agent-<id>.meta.json` names the agent: `agentType` always, `model` sometimes. A sidecar this
   reader cannot find or parse reports `None` for both, which is a different fact from a model or
   type named `"unknown"`.
3. The step comes from `journal.jsonl`, and only sometimes: most `result` records carry no `step`
   at all. That majority is reported under a stated `agentType` fallback rather than dropped.
4. Producer and auditor share a step name and come apart only on the result payload:
   `status: "ok"` with an `artifact` is the producer, `result: "findings"` is the auditor. A
   result that fits neither shape is treated as one with no step: attributed, never guessed.
5. The usage records need no new handling: `message.usage` on `assistant` records is what
   `run_usage.usage_records` and `run_usage.spend_by_model` already split into classes, imported
   here. The fold that sums two readings, `run_usage.merge_spend`, lives beside `ModelSpend`.
6. An agent that started and never reported is unfinished, not dropped: a run read mid-flight
   is the normal case for a reader over live disk state.

No price model and no currency: this counts, it does not price. The orchestrator is not
attributed: a workflow runs detached from the session that launched it, and that parent's own
spend inside the run's window is overlap, not the workflow's.

Usage:  ka-telemetry steps [--projects-root DIR] [--wf WF_ID] [--json]
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TypeVar

from ka_telemetry import run_usage, stream_usage, transcript_notes
from ka_telemetry.run_usage import shared_responses_clause

FALLBACK_NOTE = (
    "Fallback: an agent whose journal result carries no step (or none this reader could "
    "attribute to a producer or an auditor) is grouped by its agentType instead, marked "
    "fallback so a reader can tell a step from a name."
)


# ── Task 1 — the workflow runs are found ───────────────────────────────


@dataclass(frozen=True)
class FlowRun:
    """One workflow run this reader can reach — its `wf_id`, the parent session id and project
    slug it is nested under, and the directory holding its `journal.jsonl` and its agents'
    transcripts. All three identity fields are read off the path itself, not guessed: the tree
    shape (`<projects_root>/<slug>/<sid>/subagents/workflows/<wf_id>/`) is the only source for
    them, because nothing inside `journal.jsonl` restates its own workflow id or parent
    session."""

    wf_id: str
    session_id: str
    slug: str
    run_dir: Path


@dataclass(frozen=True)
class WorkflowDiscovery:
    """What one look under a projects root came to: the workflow run directories found, and the
    places that could not be looked in — the root, a project directory, a session directory, its
    `subagents` or `workflows` directory, or a workflow run's own directory — ordered by the
    directory names from the root down. A place that is merely absent is not one that failed, and
    a place inside one that failed is not reported: it was never reached."""

    run_dirs: tuple[Path, ...]
    failed_places: tuple[Path, ...]


@dataclass(frozen=True)
class _Walk:
    """What looking in one place and everything below it found: the run directories, and the
    places that could not be looked in."""

    run_dirs: tuple[Path, ...] = ()
    failed_places: tuple[Path, ...] = ()

    @staticmethod
    def of(walks: Iterable[_Walk]) -> _Walk:
        collected = tuple(walks)
        return _Walk(
            run_dirs=tuple(run_dir for walk in collected for run_dir in walk.run_dirs),
            failed_places=tuple(place for walk in collected for place in walk.failed_places),
        )


_ABSENCE_ERRORS = (FileNotFoundError, NotADirectoryError)
AGENT_FILE_PREFIX = "agent-"
AGENT_FILE_SUFFIX = ".jsonl"


# The line `steps --wf` adds, one per place, after its not-found error. The words are
# `change_text.md`'s: no report is printed on this path, so it says what the command did instead.
WF_NOT_LOOKED_IN_LINE = (
    "{directory} could not be looked in, so this command did not look for a workflow run there. "
    "Run this command again once it can be looked in."
)


def flow_runs(projects_root: Path, files: Files) -> tuple[FlowRun, ...]:
    """Every workflow run under `projects_root` whose directory holds a `journal.jsonl` — the
    population this reader scopes itself to. Sorted by `wf_id` for a deterministic reading. A
    `projects_root` that does not exist yields nothing, the same answer `Files.find_workflow_dirs`
    already gives for one that exists and holds none."""
    return flow_runs_from(files.find_workflow_dirs(projects_root))


def flow_runs_from(run_dirs: Iterable[Path]) -> tuple[FlowRun, ...]:
    """The `FlowRun` for each of `run_dirs`, identities read off the path, sorted by `wf_id`."""
    runs = []
    for run_dir in run_dirs:
        # run_dir == <projects_root>/<slug>/<sid>/subagents/workflows/<wf_id>
        wf_id = run_dir.name
        session_id = run_dir.parent.parent.parent.name
        slug = run_dir.parent.parent.parent.parent.name
        runs.append(FlowRun(wf_id=wf_id, session_id=session_id, slug=slug, run_dir=run_dir))
    return tuple(sorted(runs, key=lambda r: r.wf_id))


# ── Task 2 — the journal names each agent's step and its role ─────────────


class Role(str, Enum):
    """Which side of a step an agent ran — the value is written into `--json` output, so it is
    the `(str, Enum)` form per `languages/python/conventions.md` § Constructs."""

    PRODUCE = "produce"
    AUDIT = "audit"


@dataclass(frozen=True)
class AgentStep:
    """One agent's step attribution — its journal `agentId`, the step it ran, and which side of
    that step it was."""

    agent_id: str
    step: str
    role: Role


@dataclass(frozen=True)
class StepsFromJournal:
    """`steps_from`'s whole answer. `step_less` and `unfinished` are two different reasons an
    agent carries no step: the first got a `result` whose payload named none (or named one this
    reader could not attribute to a role); the second never got a `result` or a `failed` at
    all. Conflating them would hide that a run read mid-flight (`unfinished`) is a different
    fact from a run whose step this reader could not read (`step_less`).

    `unreadable_journal_lines` counts the non-blank journal lines this reader could not use
    (`_is_unreadable_journal_record`); each contributed nothing to any other field."""

    steps: tuple[AgentStep, ...]
    step_less: tuple[str, ...]
    unfinished: tuple[str, ...]
    failed: tuple[str, ...]
    unreadable_journal_lines: int = 0


def _step_and_role(payload: object) -> tuple[str | None, Role | None]:
    """The step and role a `result` record's payload names, or `(None, None)` when it names
    none — a plain string payload, a dict with no `step`, or (defensively) a dict that carries a
    `step` but matches neither the producer shape (`status: "ok"`) nor the auditor shape
    (`result: "findings"`): the two shapes the plan's own measurement found on the live disk. A
    third shape is not guessed a role — it is reported as step-less, the same as no step at all."""
    if not isinstance(payload, dict):
        return None, None
    step = payload.get("step")
    if not step:
        return None, None
    if payload.get("status") == "ok":
        return step, Role.PRODUCE
    if payload.get("result") == "findings":
        return step, Role.AUDIT
    return None, None


def _is_string_or_null(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_unreadable_journal_record(record: dict) -> bool:
    """Whether a journal object names an `agentId`, or a `result` names a `step`, of a kind this
    reader cannot use (CP-416): present and neither `null` nor a string. A `step` is judged
    whether or not the record names an agent."""
    if not _is_string_or_null(record.get("agentId")):
        return True
    payload = record.get("result")
    return (
        record.get("type") == "result"
        and isinstance(payload, dict)
        and not _is_string_or_null(payload.get("step"))
    )


def steps_from(journal_text: str) -> StepsFromJournal:
    """Parse a `journal.jsonl`'s text into a step attribution per `agentId`, plus the agents
    that started and never reported. A line this reader cannot use is counted and skipped before
    it can start, finish, fail or name a step for any agent — the same read-a-live-file posture
    `run_usage.usage_records` already takes. A record naming no agent is passed over uncounted."""
    started: set[str] = set()
    resulted: set[str] = set()
    failed: set[str] = set()
    steps: list[AgentStep] = []
    step_less: list[str] = []
    unreadable_journal_lines = 0

    for line in journal_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            unreadable_journal_lines += 1
            continue
        if not isinstance(record, dict) or _is_unreadable_journal_record(record):
            unreadable_journal_lines += 1
            continue
        agent_id = record.get("agentId")
        if not agent_id:
            continue
        record_type = record.get("type")
        if record_type == "started":
            started.add(agent_id)
        elif record_type == "failed":
            failed.add(agent_id)
        elif record_type == "result":
            resulted.add(agent_id)
            step, role = _step_and_role(record.get("result"))
            if step is None or role is None:
                step_less.append(agent_id)
            else:
                steps.append(AgentStep(agent_id=agent_id, step=step, role=role))

    unfinished = tuple(sorted(started - resulted - failed))
    return StepsFromJournal(
        steps=tuple(steps),
        step_less=tuple(step_less),
        unfinished=unfinished,
        failed=tuple(sorted(failed)),
        unreadable_journal_lines=unreadable_journal_lines,
    )


# ── Task 3 — the meta sidecar names the agent ──────────────────────────


@dataclass(frozen=True)
class AgentMeta:
    """What `agent-<id>.meta.json` says about one agent — `None` for either field it omits, or
    for a sidecar this reader could not use, never invented. `unreadable` is `True` only for a
    sidecar that is present and failed the read or CP-417's validation; an absent one is `False`."""

    agent_type: str | None
    model: str | None
    unreadable: bool = False


_NO_META = AgentMeta(agent_type=None, model=None)
_UNREADABLE_META = AgentMeta(agent_type=None, model=None, unreadable=True)


def agent_meta(run_dir: Path, agent_id: str, files: Files) -> AgentMeta:
    """`agentType` and `model` from `<run_dir>/agent-<agent_id>.meta.json`. An absent sidecar
    reports `AgentMeta(None, None)`. A present one that cannot be opened or read, is not JSON,
    is not an object, or holds an `agentType` that is neither `null` nor a string reports the
    same two `None`s with `unreadable=True` (CP-417). The sidecar's `model` is never validated:
    neither report prints it. Either way this reader still reports the agent."""
    read = files.read_meta_text(run_dir / f"agent-{agent_id}.meta.json")
    if read.missing:
        return _NO_META
    if read.text is None:
        return _UNREADABLE_META
    try:
        data = json.loads(read.text)
    except (ValueError, TypeError):
        return _UNREADABLE_META
    if not isinstance(data, dict) or not _is_string_or_null(data.get("agentType")):
        return _UNREADABLE_META
    return AgentMeta(agent_type=data.get("agentType"), model=data.get("model"))


# ── Task 4 — an agent's spend uses the sibling's accumulator, not a copy ─────


class _TranscriptSnapshot:
    """One read of an agent transcript, standing in for `Files` so `run_usage.usage_records` takes
    its lines from the one read `read_run` makes. `lines` is `None` when the port could not read
    the file — an absence is a snapshot too, and is never read a second time."""

    def __init__(self, lines: list[str] | None) -> None:
        self._lines = lines

    def read_lines(self, path: Path) -> list[str] | None:
        return self._lines


def agent_spend(
    run_dir: Path,
    agent_id: str,
    files: Files,
    *,
    unreadable_lines: run_usage.UnreadableLines | None = None,
) -> dict[str, run_usage.ModelSpend]:
    """One `ModelSpend` per model id found in `<run_dir>/agent-<agent_id>.jsonl` — read through
    `run_usage.usage_records` and summed by `run_usage.spend_by_model`, the sibling reader's own
    accumulator, not a second one. An agent file that does not exist reads as no records, which
    `run_usage.spend_by_model` turns into `{}` — an empty spend the caller still counts, never a
    skip. A counter handed in is filled by that same single read; it is complete once this
    returns, and its `unreadable_files` says whether the transcript could not be read at all —
    the one thing an empty spend cannot say."""
    path = run_dir / f"agent-{agent_id}.jsonl"
    records = run_usage.usage_records(path, files, unreadable_lines=unreadable_lines)
    return run_usage.spend_by_model(records)


# ── The assembly this reader needs between Tasks 4 and 5 ────────────────
#
# The plan cuts step attribution (Task 2), agent identity (Task 3) and agent spend (Task 4) into
# three separate functions, and `aggregate(run)` / `coverage(run)` (Tasks 5-6) both consume one
# already-assembled reading of a run. `read_run` is that assembly — it is not named by the plan,
# and exists because "aggregate(run)" and "coverage(run)" both need the same "run" to consume.


@dataclass(frozen=True)
class AgentReading:
    """One agent, fully read: its step attribution (if any), its identity from the meta
    sidecar, its spend, and whether the journal ever saw it finish."""

    agent_id: str
    step: str | None
    role: Role | None
    agent_type: str | None
    model_hint: str | None
    spend: dict[str, run_usage.ModelSpend] = field(default_factory=dict)
    unfinished: bool = False
    failed: bool = False
    unreadable_lines: int = 0
    unreadable_meta_file: bool = False
    unreadable_agent_transcript: bool = False
    # The timestamps of this agent's counted responses only (`usage_records` yields one per
    # response, at the first record of its response id) — never a prompt's or a tool result's.
    timestamps: tuple[str, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class AgentFilesFailure:
    """A workflow run whose directory could not be listed, so its agent files could not be
    counted: the `directory` as the report read it, and the operating system's `reason`, with
    line breaks folded into single spaces. It is not a place discovery could not look in — the
    run's journal and every agent it names were read."""

    directory: Path
    reason: str


@dataclass(frozen=True)
class RunReading:
    """One `FlowRun`, fully read — every agent the journal named, plus the agent files the
    journal never mentioned at all (coverage's fourth figure, kept apart from the per-agent list
    because an orphan file has no step, no role and no journal-derived identity to report a row
    for). When the run directory could not be listed, `agent_files_failure` says so and
    `orphan_agent_files` is empty without being a count: the fourth figure is unknown."""

    flow_run: FlowRun
    agents: tuple[AgentReading, ...]
    orphan_agent_files: tuple[str, ...]
    agent_files_failure: AgentFilesFailure | None = field(default=None, kw_only=True)
    # How many responses this run's rows keep that the report also found in another transcript:
    # filled in once every selected workflow run has been read, never while one is being read.
    responses_in_more_than_one_transcript: int = field(default=0, kw_only=True)
    unreadable_journal_lines: int = 0
    unreadable_journal_files: int = 0


def _reason_of(error: OSError) -> str:
    """The operating system's own message for `error`, each line break (`\\r\\n`, `\\r` or `\\n`)
    folded into a single space and nothing else changed."""
    return re.sub(r"\r\n|\r|\n", " ", error.strerror or str(error))


_GroupKey = tuple[str, Role | None, bool]
_Item = TypeVar("_Item")


def _group_key(step: str | None, role: Role | None, agent_type: str | None) -> _GroupKey:
    """The row an agent belongs to: `(step, role)` where its step is known, else its `agentType`
    marked as a fallback. The one definition `aggregate` and the order responses are counted in
    both use."""
    if step is not None and role is not None:
        return (step, role, False)
    return (agent_type or "(unknown)", None, True)


def _grouped(
    items: Iterable[_Item], key_of: Callable[[_Item], _GroupKey]
) -> dict[_GroupKey, list[_Item]]:
    """`items` under their row keys, the rows in the order they first appear and each row's items
    in the order given — never sorted by label or by first response time."""
    grouped: dict[_GroupKey, list[_Item]] = {}
    for item in items:
        grouped.setdefault(key_of(item), []).append(item)
    return grouped


@dataclass(frozen=True)
class _AgentHead:
    """What the journal and the sidecar say of one agent, before any of its transcript is read."""

    agent_id: str
    step_entry: AgentStep | None
    meta: AgentMeta
    unfinished: bool
    failed: bool

    @property
    def group(self) -> _GroupKey:
        step = self.step_entry
        return _group_key(
            step.step if step else None, step.role if step else None, self.meta.agent_type
        )


@dataclass(frozen=True)
class _PreparedRun:
    """A workflow run whose journal and sidecars are read and whose rows are therefore known, with
    no transcript read yet."""

    flow_run: FlowRun
    heads: tuple[_AgentHead, ...]
    orphan_agent_files: tuple[str, ...]
    agent_files_failure: AgentFilesFailure | None
    unreadable_journal_lines: int

    @property
    def groups(self) -> dict[_GroupKey, list[_AgentHead]]:
        return _grouped(self.heads, lambda head: head.group)


def _prepare_run(flow_run: FlowRun, files: Files) -> _PreparedRun | RunReading:
    """Parse `flow_run`'s journal (Task 2) and look up each named agent's identity (Task 3). A
    journal that could not be opened is already a whole reading, with nothing left to count."""
    journal = files.read_journal_text(flow_run.run_dir / "journal.jsonl")
    if journal.text is None and not journal.missing:
        return RunReading(
            flow_run=flow_run,
            agents=(),
            orphan_agent_files=(),
            unreadable_journal_files=1,
        )
    parsed = steps_from(journal.text or "")
    step_by_agent = {s.agent_id: s for s in parsed.steps}
    journal_agent_ids = (
        set(step_by_agent) | set(parsed.step_less) | set(parsed.unfinished) | set(parsed.failed)
    )
    agent_files_failure = None
    try:
        file_agent_ids = set(files.agent_ids_in(flow_run.run_dir))
    except OSError as error:
        file_agent_ids = set()
        if not journal.missing:
            agent_files_failure = AgentFilesFailure(
                directory=flow_run.run_dir, reason=_reason_of(error)
            )

    heads = tuple(
        _AgentHead(
            agent_id=agent_id,
            step_entry=step_by_agent.get(agent_id),
            meta=agent_meta(flow_run.run_dir, agent_id, files),
            unfinished=agent_id in parsed.unfinished,
            failed=agent_id in parsed.failed,
        )
        for agent_id in sorted(journal_agent_ids)
    )
    return _PreparedRun(
        flow_run=flow_run,
        heads=heads,
        orphan_agent_files=tuple(sorted(file_agent_ids - journal_agent_ids)),
        agent_files_failure=agent_files_failure,
        unreadable_journal_lines=parsed.unreadable_journal_lines,
    )


def _read_agent(
    head: _AgentHead,
    flow_run: FlowRun,
    files: Files,
    scope: run_usage.ResponseScope,
    row_key: Hashable,
) -> AgentReading:
    """One agent's spend, read from one snapshot of its transcript, counting only the responses
    `scope` lets `row_key` keep."""
    path = flow_run.run_dir / f"agent-{head.agent_id}.jsonl"
    counter = run_usage.UnreadableLines()
    lines = files.read_lines(path)
    responses = tuple(
        run_usage.usage_records(
            path,
            _TranscriptSnapshot(lines),  # type: ignore[arg-type]
            unreadable_lines=counter,
            response_scope=scope,
            row_key=row_key,
        )
    )
    step_entry = head.step_entry
    return AgentReading(
        agent_id=head.agent_id,
        step=step_entry.step if step_entry else None,
        role=step_entry.role if step_entry else None,
        agent_type=head.meta.agent_type,
        model_hint=head.meta.model,
        spend=run_usage.spend_by_model(responses),
        unfinished=head.unfinished,
        failed=head.failed,
        unreadable_lines=counter.count,
        unreadable_meta_file=head.meta.unreadable,
        unreadable_agent_transcript=counter.unreadable_files > 0,
        timestamps=tuple(timestamp for _, _, timestamp in responses if timestamp),
    )


def _row_key(flow_run: FlowRun, group: _GroupKey) -> Hashable:
    """A row's identity for one report: its workflow run and its group, so rows of the same label
    in two sections never collide."""
    return (flow_run.run_dir, group)


def _read_prepared(
    prepared: _PreparedRun, files: Files, scope: run_usage.ResponseScope
) -> RunReading:
    """Read each agent's transcript, a row at a time in the order the rows print and each row's
    agents in header order, so the first row to print a response is the one that keeps it. The
    agents come back in their own order."""
    readings: dict[str, AgentReading] = {}
    for group, heads in prepared.groups.items():
        key = _row_key(prepared.flow_run, group)
        for head in heads:
            readings[head.agent_id] = _read_agent(head, prepared.flow_run, files, scope, key)
    return RunReading(
        flow_run=prepared.flow_run,
        agents=tuple(readings[head.agent_id] for head in prepared.heads),
        orphan_agent_files=prepared.orphan_agent_files,
        agent_files_failure=prepared.agent_files_failure,
        unreadable_journal_lines=prepared.unreadable_journal_lines,
    )


def read_selected_runs(flow_runs: Iterable[FlowRun], files: Files) -> tuple[RunReading, ...]:
    """Every one of `flow_runs`, read as one report: one scope across them, so a response found in
    more than one transcript is counted once, under the first row to print it. Each run's count
    of such responses is set only after the last run is read, because a copy in a later run is
    what makes an earlier run's response shared."""
    scope = run_usage.ResponseScope()
    readings = []
    prepared_runs = []
    for flow_run in flow_runs:
        prepared = _prepare_run(flow_run, files)
        prepared_runs.append(prepared if isinstance(prepared, _PreparedRun) else None)
        readings.append(
            prepared if isinstance(prepared, RunReading) else _read_prepared(prepared, files, scope)
        )
    return tuple(
        reading
        if prepared is None
        else replace(
            reading,
            responses_in_more_than_one_transcript=sum(
                scope.count_shared(_row_key(prepared.flow_run, group)) for group in prepared.groups
            ),
        )
        for reading, prepared in zip(readings, prepared_runs, strict=True)
    )


def read_run(flow_run: FlowRun, files: Files) -> RunReading:
    """Assemble one `RunReading` for `flow_run`, as a report of that one workflow run."""
    return read_selected_runs([flow_run], files)[0]


# ── Task 5 — a run aggregates per step, and step-less agents fall back by name ──


@dataclass(frozen=True)
class AggregateRow:
    """One row of a run's aggregated spend — keyed by `(label, role)` when a step is known, or
    by `label` alone (an `agentType`) when it is not, in which case `is_fallback` is `True`.
    `spend` is summed class-by-class across every agent in `agent_ids`, never across classes.

    `agent_types` is a column of its own, not a restatement of `label`: on a step row the label
    is the step, so without this a reader sees that `development-specs` ran and never which
    agent ran it. It carries the distinct types across the row's agents — a `(step, role)` row
    can hold several — and keeps `None` as the stated absence `agent_meta` reports for a sidecar
    that named no type, rather than dropping it or inventing one."""

    label: str
    role: Role | None
    is_fallback: bool
    agent_ids: tuple[str, ...]
    agent_types: tuple[str | None, ...]
    spend: dict[str, run_usage.ModelSpend]
    span_seconds: float | None = field(default=None, kw_only=True)


def _row_span(agents: list[AgentReading]) -> float | None:
    """The wall time of a row: its grouped agents' timestamps taken together, earliest to latest,
    by `run_usage.span_seconds` — never a sum of per-agent spans, which would lose the endpoints
    a union needs. `None` when no agent holds a readable moment."""
    return run_usage.span_seconds(
        ({}, None, timestamp) for agent in agents for timestamp in agent.timestamps
    )


def _distinct_agent_types(agents: list[AgentReading]) -> tuple[str | None, ...]:
    """The distinct `agentType`s across `agents`, named ones first in name order and the stated
    `None` last — `sorted` cannot order `None` against a `str` directly, and dropping it would
    turn "this sidecar named no type" into "this row has no agents without one"."""
    seen = {a.agent_type for a in agents}
    named = sorted(t for t in seen if t is not None)
    return tuple(named) + ((None,) if None in seen else ())


def aggregate(run: RunReading) -> tuple[AggregateRow, ...]:
    """One row per `(step, role)` where an agent's step is known, one row per `agentType` where
    it is not (marked `is_fallback`). Two agents sharing a `(step, role)` merge into one row;
    two agents on the same step with different roles stay two rows. A run with no agents
    returns no rows."""
    grouped = _grouped(run.agents, lambda a: _group_key(a.step, a.role, a.agent_type))

    rows = []
    for (label, role, is_fallback), agents in grouped.items():
        spend: dict[str, run_usage.ModelSpend] = {}
        for agent in agents:
            spend = run_usage.merge_spend(spend, agent.spend)
        rows.append(
            AggregateRow(
                label=label,
                role=role,
                is_fallback=is_fallback,
                agent_ids=tuple(a.agent_id for a in agents),
                agent_types=_distinct_agent_types(agents),
                spend=spend,
                span_seconds=_row_span(agents),
            )
        )
    return tuple(rows)


# ── Task 6 — coverage is stated, never implied ─────────────────────────


@dataclass(frozen=True)
class Coverage:
    """How much of one run's population this reading actually covered. `with_step` +
    `without_step` accounts for every agent the journal named at all; `without_step` and
    `started_no_result` overlap on purpose — an agent that never got a result is both. A file
    on disk the journal never mentioned counts only in `files_with_no_journal_entry`, which is
    what tells a reader the journal is not the whole truth.

    The five `unreadable_*` counts are kept apart and each line or file is in at most one: the
    lines of this run's agent transcripts, the lines of its journal, the meta files and agent
    transcripts of the agents its journal names, and its journal file itself — `1` when the
    journal was found and could not be opened, in which case no agent file was looked at and
    every other figure is `0`."""

    with_step: int
    without_step: int
    started_no_result: int
    files_with_no_journal_entry: int | None
    unreadable_lines: int = 0
    unreadable_journal_lines: int = 0
    unreadable_meta_files: int = 0
    unreadable_agent_transcripts: int = 0
    unreadable_journal_files: int = 0
    # Not a field, as in `run_usage.Coverage`: a run that shared no response is this class itself.
    responses_in_more_than_one_transcript = 0


@dataclass(frozen=True)
class CoverageWithSharedResponses(Coverage):
    """A `Coverage` of a run whose rows keep `responses_in_more_than_one_transcript` responses,
    one or more, that the report also found in another transcript."""

    responses_in_more_than_one_transcript: int = 0


def coverage(run: RunReading) -> Coverage:
    """The four coverage figures for `run` — each counted independently over `run.agents` and
    `run.orphan_agent_files`, never derived from one another, the fourth `None` when the run's
    agent files could not be listed — and the four unreadable counts,
    folded from what `read_run` already observed. This performs no I/O."""
    with_step = sum(1 for a in run.agents if a.step is not None)
    without_step = sum(1 for a in run.agents if a.step is None)
    started_no_result = sum(1 for a in run.agents if a.unfinished)
    figures = dict(
        with_step=with_step,
        without_step=without_step,
        started_no_result=started_no_result,
        files_with_no_journal_entry=(
            None if run.agent_files_failure else len(run.orphan_agent_files)
        ),
        unreadable_lines=sum(a.unreadable_lines for a in run.agents),
        unreadable_journal_lines=run.unreadable_journal_lines,
        unreadable_meta_files=sum(1 for a in run.agents if a.unreadable_meta_file),
        unreadable_agent_transcripts=sum(1 for a in run.agents if a.unreadable_agent_transcript),
        unreadable_journal_files=run.unreadable_journal_files,
    )
    if run.responses_in_more_than_one_transcript:
        return CoverageWithSharedResponses(
            **figures,
            responses_in_more_than_one_transcript=run.responses_in_more_than_one_transcript,
        )
    return Coverage(**figures)


# ── Task 7 — the report renders, and `--json` carries the same numbers ────


# The words each unreadable kind is named in, one whole string for one and one for more, so no
# `s` is added at runtime. They are `change_text.md`'s, verbatim; the first pair is as it was
# before this kind of clause had siblings.
UNREADABLE_LINES_ONE = "1 unreadable line skipped in this workflow run's agent transcripts"
UNREADABLE_LINES_MANY = (
    "{line_count} unreadable lines skipped in this workflow run's agent transcripts"
)
UNREADABLE_JOURNAL_LINES_ONE = "1 unreadable journal line skipped"
UNREADABLE_JOURNAL_LINES_MANY = "{line_count} unreadable journal lines skipped"
UNREADABLE_META_FILES_ONE = "1 unreadable meta file, so its agent's type shows as (none)"
UNREADABLE_META_FILES_MANY = (
    "{file_count} unreadable meta files, so their agents' types show as (none)"
)
UNREADABLE_AGENT_TRANSCRIPTS_ONE = (
    "1 unreadable agent transcript, so no spend is read for its agent"
)
UNREADABLE_AGENT_TRANSCRIPTS_MANY = (
    "{transcript_count} unreadable agent transcripts, so no spend is read for their agents"
)
# One journal per workflow run, so the count is 0 or 1 and only the singular string exists.
UNREADABLE_JOURNAL_FILES_ONE = (
    "1 unreadable journal file, so no agent file in this workflow run is read or counted"
)


def _unreadable_clauses(cov: Coverage) -> list[str]:
    """One clause per unreadable count of 1 or more, in the fixed kind order: transcript lines,
    journal lines, meta files, agent transcripts, journal file. A count of 0 has no clause."""
    clauses = []
    if cov.unreadable_lines == 1:
        clauses.append(UNREADABLE_LINES_ONE)
    elif cov.unreadable_lines > 1:
        clauses.append(UNREADABLE_LINES_MANY.format(line_count=cov.unreadable_lines))
    if cov.unreadable_journal_lines == 1:
        clauses.append(UNREADABLE_JOURNAL_LINES_ONE)
    elif cov.unreadable_journal_lines > 1:
        clauses.append(
            UNREADABLE_JOURNAL_LINES_MANY.format(line_count=cov.unreadable_journal_lines)
        )
    if cov.unreadable_meta_files == 1:
        clauses.append(UNREADABLE_META_FILES_ONE)
    elif cov.unreadable_meta_files > 1:
        clauses.append(UNREADABLE_META_FILES_MANY.format(file_count=cov.unreadable_meta_files))
    if cov.unreadable_agent_transcripts == 1:
        clauses.append(UNREADABLE_AGENT_TRANSCRIPTS_ONE)
    elif cov.unreadable_agent_transcripts > 1:
        clauses.append(
            UNREADABLE_AGENT_TRANSCRIPTS_MANY.format(
                transcript_count=cov.unreadable_agent_transcripts
            )
        )
    if cov.unreadable_journal_files > 0:
        clauses.append(UNREADABLE_JOURNAL_FILES_ONE)
    return clauses


def _coverage_line(cov: Coverage) -> str:
    line = (
        f"Coverage: {cov.with_step} with a step / {cov.without_step} without / "
        f"{cov.started_no_result} started with no result"
    )
    if cov.files_with_no_journal_entry is not None:
        line += f" / {cov.files_with_no_journal_entry} agent files with no journal entry at all"
    shared = shared_responses_clause(cov.responses_in_more_than_one_transcript)
    clauses = _unreadable_clauses(cov) + ([shared] if shared is not None else [])
    return "".join([line] + [f" — {clause}" for clause in clauses])


# The words of a run's uncounted line, `change_text.md`'s, verbatim.
AGENT_FILES_UNCOUNTED_LINE = (
    "{directory} could not be listed, so its agent files could not be counted and this report "
    "has no count of agent files with no journal entry at all for its workflow run. "
    "The system reported: {reason}. Run this command again once that directory can be listed."
)

_Diagnostic = run_usage.DirectoryFailure | AgentFilesFailure


def _diagnostics(
    runs: Iterable[RunReading], unreadable_directories: Iterable[run_usage.DirectoryFailure]
) -> list[_Diagnostic]:
    """Every directory statement the report makes: the places discovery could not look in and
    each run's agent files that could not be counted, in one list. With an uncounted run it is
    sorted by the components of each directory, root down — never by workflow id, joined text or
    the order the errors came; with none, discovery's order stands."""
    places: list[_Diagnostic] = list(unreadable_directories)
    uncounted = [run.agent_files_failure for run in runs if run.agent_files_failure is not None]
    if not uncounted:
        return places
    return sorted([*places, *uncounted], key=lambda failure: failure.directory.parts)


def _diagnostic_line(diagnostic: _Diagnostic) -> str:
    if isinstance(diagnostic, AgentFilesFailure):
        return AGENT_FILES_UNCOUNTED_LINE.format(
            directory=diagnostic.directory, reason=diagnostic.reason
        )
    return run_usage.directory_line(diagnostic)


def _types_column(row: AggregateRow) -> str:
    """The row's agent-type column. `None` renders as `(none)` — the parenthesised form keeps it
    apart from a real `agentType` literally named `none`, the same distinction `agent_meta`'s
    `None` draws against a type named `"unknown"`."""
    return ",".join(t if t is not None else "(none)" for t in row.agent_types) or "(none)"


def _span_text(span_seconds: float | None) -> str:
    """A row's span as `runs` writes its own: `{value:g}s`, or `n/a` where no moment was read."""
    return "n/a" if span_seconds is None else f"{span_seconds:g}s"


def render(
    runs: list[RunReading],
    *,
    matched_sessions: Iterable[str] = (),
    unreadable_directories: Iterable[run_usage.DirectoryFailure] = (),
) -> str:
    """The human table — one section per run, one line per `(step, role)` or fallback
    `agentType` row, carrying the row's agent types, then per model the five classes with
    `thinking` reported as a share of `output`, never added to it. States the fallback rule, under
    it the one statement of what the time covers, and each run's four coverage figures in the
    reader's own words, never leaving them implied. It ends, once for the whole report, with the
    no-cost line, and under it a pointer for each of `matched_sessions`: the parent sessions this
    report shows that the telemetry stream holds rows for. A line for each of
    `unreadable_directories` — the places that could not be looked in — follows every run's
    section, directly above that block, and takes the place of the empty-state line when there is
    one."""
    lines = [FALLBACK_NOTE, transcript_notes.SPAN_LINE, ""]
    directories = [
        _diagnostic_line(diagnostic) for diagnostic in _diagnostics(runs, unreadable_directories)
    ]
    if not runs:
        lines.extend(directories or ["No workflow runs read."])
        return transcript_notes.append_notes("\n".join(lines), matched_sessions)

    for run in runs:
        lines.append(
            f"{run.flow_run.wf_id}  session={run.flow_run.session_id}  slug={run.flow_run.slug}"
        )
        lines.append(f"  {_coverage_line(coverage(run))}")
        rows = aggregate(run)
        if not rows:
            lines.append("  No agents read for this run.")
            lines.append("")
            continue
        for row in rows:
            row_label = (
                f"{row.label} ({row.role.value})" if row.role else f"{row.label} [fallback]"
            )
            lines.append(
                f"  {row_label}  types={_types_column(row)}  "
                f"agents={','.join(row.agent_ids)}"
            )
            lines.append(f"    span={_span_text(row.span_seconds)}")
            for model, spend in row.spend.items():
                thinking = "" if spend.thinking is None else f" thinking={spend.thinking}"
                lines.append(
                    f"    {model}: input={spend.input} output={spend.output}{thinking} "
                    f"cache_read={spend.cache_read} cache_write_1h={spend.cache_write_1h} "
                    f"cache_write_5m={spend.cache_write_5m}"
                )
        lines.append("")
    lines.extend(directories)
    return transcript_notes.append_notes("\n".join(lines).rstrip(), matched_sessions)


def _coverage_json(run: RunReading) -> dict[str, int | None]:
    cov = coverage(run)
    members = {
        "with_step": cov.with_step,
        "without_step": cov.without_step,
        "started_no_result": cov.started_no_result,
        "files_with_no_journal_entry": cov.files_with_no_journal_entry,
    }
    for name in (
        "unreadable_lines",
        "unreadable_journal_lines",
        "unreadable_meta_files",
        "unreadable_agent_transcripts",
        "unreadable_journal_files",
    ):
        if getattr(cov, name) > 0:
            members[name] = getattr(cov, name)
    if cov.responses_in_more_than_one_transcript > 0:
        members["responses_in_more_than_one_transcript"] = cov.responses_in_more_than_one_transcript
    return members


def render_json(
    runs: list[RunReading],
    *,
    unreadable_directories: Iterable[run_usage.DirectoryFailure] = (),
) -> str:
    """The machine rows — same fallback note, `span` statement and per-run coverage as `render`,
    the same never-summed five classes, and `unreadable_directories` when any place could not be
    looked in. It carries no note block and reads no stream."""
    payload = {
        "fallback": FALLBACK_NOTE,
        "span": transcript_notes.SPAN_LINE,
        "runs": [
            {
                "wf_id": run.flow_run.wf_id,
                "session_id": run.flow_run.session_id,
                "slug": run.flow_run.slug,
                "coverage": _coverage_json(run),
                "rows": [
                    {
                        "span_seconds": row.span_seconds,
                        "label": row.label,
                        "role": row.role.value if row.role else None,
                        "is_fallback": row.is_fallback,
                        "agent_ids": list(row.agent_ids),
                        "agent_types": list(row.agent_types),
                        "spend": {
                            model: {
                                "input": spend.input,
                                "output": spend.output,
                                "thinking": spend.thinking,
                                "cache_read": spend.cache_read,
                                "cache_write_1h": spend.cache_write_1h,
                                "cache_write_5m": spend.cache_write_5m,
                            }
                            for model, spend in row.spend.items()
                        },
                    }
                    for row in aggregate(run)
                ],
            }
            for run in runs
        ],
    }
    directories = [
        {"directory": str(d.directory), "reason": d.reason}
        for d in _diagnostics(runs, unreadable_directories)
    ]
    if directories:
        payload["unreadable_directories"] = directories
    return json.dumps(payload, indent=2)


# ── The ports ────────────────────────────
#
# Every world-reach this reader makes — all of it filesystem — arrives through `Files`. Nothing
# else is constructed anywhere but `build_reader()`.


@dataclass(frozen=True)
class TextRead:
    """What one read of a text file found: its `text`, or `None` when it could not be read, and
    `missing` — `True` only when the file does not exist, the one failure that is not a record
    this reader could not read."""

    text: str | None
    missing: bool


def _stat_entry(path: Path) -> os.stat_result | None:
    """What `path` is, following a link, or `None` when it is absent: gone, under something that
    is not a directory, or a link that leads nowhere. Any other failure to find out is raised,
    the same convention `run_usage.list_directory` applies to the entries it lists."""
    try:
        return path.stat()
    except _ABSENCE_ERRORS:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return None
        raise


def _in_order_from(projects_root: Path, places: Iterable[Path]) -> tuple[Path, ...]:
    """`places` once each, ordered by the directory names below `projects_root`, taken from the
    root down and compared one name at a time. A failed root is the only place there is."""
    return tuple(sorted(set(places), key=lambda place: place.relative_to(projects_root).parts))


class Files:
    """Every filesystem touch this reader makes."""

    def find_workflow_dirs(self, projects_root: Path) -> list[Path]:
        """Every `<projects_root>/<slug>/<sid>/subagents/workflows/<wf_id>/` that holds a
        `journal.jsonl` — never the flat sibling `<slug>/<sid>/subagents/agent-*.jsonl` (an
        Agent-tool subagent, no workflow) and never `<slug>/<sid>.jsonl` (an interactive
        session), because neither matches this glob's shape. A nonexistent `projects_root`
        yields no matches rather than raising, the same answer `Path.glob` gives for one that
        exists and holds none."""
        return [p.parent for p in projects_root.glob("*/*/subagents/workflows/*/journal.jsonl")]

    def discover_workflows(self, projects_root: Path) -> WorkflowDiscovery:
        """The same workflow run directories as `find_workflow_dirs`, found by looking in each
        place on the way down — the root, each project directory, each session directory, its
        `subagents` and `workflows` directories, and each workflow run's directory — so a place
        that could not be looked in is reported rather than read as empty. A failed place ends
        its own branch: nothing below it is reported and it contributes no run, while its
        siblings are still read. An absent place, one under a regular file, and an entry that is
        not a directory are no failure, and the supplied root's spelling is kept, never
        resolved. Where only one named entry is needed (`subagents`, `workflows`,
        `journal.jsonl`) that entry is observed by name, so a directory that can be entered but
        not listed is still looked in."""
        walk = self._projects_root_walk(projects_root)
        return WorkflowDiscovery(
            run_dirs=walk.run_dirs,
            failed_places=_in_order_from(projects_root, walk.failed_places),
        )

    def _projects_root_walk(self, projects_root: Path) -> _Walk:
        listing = run_usage.list_directory(projects_root)
        if listing.failed:
            return _Walk(failed_places=(projects_root,))
        return _Walk.of(
            self._project_walk(projects_root / name)
            for name, is_directory in listing.entries
            if is_directory
        )

    def _project_walk(self, project: Path) -> _Walk:
        listing = run_usage.list_directory(project)
        if listing.failed:
            return _Walk(failed_places=(project,))
        return _Walk.of(
            self._through_named_directory(project / name, "subagents", self._subagents_walk)
            for name, is_directory in listing.entries
            if is_directory
        )

    def _subagents_walk(self, subagents: Path) -> _Walk:
        return self._through_named_directory(subagents, "workflows", self._workflows_walk)

    def _workflows_walk(self, workflows: Path) -> _Walk:
        listing = run_usage.list_directory(workflows)
        if listing.failed:
            return _Walk(failed_places=(workflows,))
        return _Walk.of(
            self._run_walk(workflows / name)
            for name, is_directory in listing.entries
            if is_directory
        )

    def _run_walk(self, run_dir: Path) -> _Walk:
        """A run directory holds a run when it holds an entry named `journal.jsonl`, of any kind
        and wherever a link there leads, as `find_workflow_dirs` finds one. The entry is observed
        without following it, so a link that leads nowhere still names a run."""
        try:
            (run_dir / "journal.jsonl").lstat()
        except _ABSENCE_ERRORS:
            return _Walk()
        except OSError:
            return _Walk(failed_places=(run_dir,))
        return _Walk(run_dirs=(run_dir,))

    def _through_named_directory(
        self, parent: Path, name: str, inside: Callable[[Path], _Walk]
    ) -> _Walk:
        """Walk `inside` the directory `parent` holds under `name`, found by observing that one
        entry. `parent` is the failed place when the entry cannot be observed; an entry that is
        absent, or not a directory, ends the branch with no failure."""
        child = parent / name
        try:
            observed = _stat_entry(child)
        except OSError:
            return _Walk(failed_places=(parent,))
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            return _Walk()
        return inside(child)

    def agent_ids_in(self, run_dir: Path) -> list[str]:
        """Every agent id with an `agent-<id>.jsonl` transcript under `run_dir` — the left side
        of the fourth coverage figure, a file present with no journal entry at all. The listing
        is completed before any name is returned. An absent directory, or a path below something
        that is not a directory, holds none; any other failure to list it is raised with the
        system's own reason, and no name from a listing that broke part-way is exposed. An entry
        is matched by its name alone, never opened or looked at."""
        try:
            names = [entry.name for entry in run_dir.iterdir()]
        except _ABSENCE_ERRORS:
            return []
        return [
            name[len(AGENT_FILE_PREFIX) : -len(AGENT_FILE_SUFFIX)]
            for name in names
            if len(name) >= len(AGENT_FILE_PREFIX) + len(AGENT_FILE_SUFFIX)
            and name.startswith(AGENT_FILE_PREFIX)
            and name.endswith(AGENT_FILE_SUFFIX)
        ]

    def read_text(self, path: Path) -> str | None:
        """`path`'s whole text, or `None` when it cannot be read at all. `read_run` reads the
        journal through `read_journal_text` and a meta file through `read_meta_text`, which keep
        an absent file apart from one that cannot be opened; this answer cannot."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def read_journal_text(self, path: Path) -> TextRead:
        """`journal.jsonl`'s whole text as an outcome that keeps an absent journal apart from
        one that is present and cannot be opened — one read, no existence check before it. A
        completed read, an empty one included, has `text` set; a path with nothing at it
        (`FileNotFoundError`, or `NotADirectoryError` for a path under a regular file) is
        `missing`; any other `OSError` is neither."""
        try:
            return TextRead(text=path.read_text(encoding="utf-8", errors="replace"), missing=False)
        except (FileNotFoundError, NotADirectoryError):
            return TextRead(text=None, missing=True)
        except OSError:
            return TextRead(text=None, missing=False)

    def read_meta_text(self, path: Path) -> TextRead:
        """`path`'s whole text as an outcome that keeps an absent file apart from one that is
        present and cannot be read — one read, no existence check before it. Used for
        `agent-<id>.meta.json`, where CP-417 counts the second and not the first. A name no file
        can have — the journal's `agentId` holding a NUL or an unpaired surrogate, which `open`
        refuses with `ValueError` — is absent: nothing can be there to read."""
        try:
            return TextRead(text=path.read_text(encoding="utf-8", errors="replace"), missing=False)
        except (FileNotFoundError, ValueError):
            return TextRead(text=None, missing=True)
        except OSError:
            return TextRead(text=None, missing=False)

    def read_lines(self, path: Path) -> list[str] | None:
        """`path`'s lines, or `None` when it cannot be read at all. The exact shape
        `run_usage.usage_records` expects of its own `files` argument — this port hands itself
        straight to that imported function rather than owning a second implementation. A name no
        file can have (`open` refuses it with `ValueError`, as in `read_meta_text`) reads as
        `None`, the same as an absent file."""
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return None


# ── The composition root: the only place a concrete implementation is constructed ────


@dataclass(frozen=True)
class Reader:
    """The reader's whole environment: the one port it reaches the world through, plus the
    projects root resolved to a concrete `Path` — the one environment reach `build_reader`
    performs itself, per `run_usage`'s own `Reader`/`build_reader` shape."""

    files: Files
    projects_root: Path
    notes_reader: stream_usage.Reader | None = None


@dataclass(frozen=True)
class StepsReading:
    """Every workflow run the reader's projects root yields, read, and the places that could not
    be looked in to find them."""

    runs: tuple[RunReading, ...]
    unreadable_directories: tuple[run_usage.DirectoryFailure, ...]


def read_steps(reader: Reader) -> StepsReading:
    """Every workflow run under `reader.projects_root`, read, with the directories that could not
    be looked in, from one discovery. Starts no process and renders nothing."""
    discovery = reader.files.discover_workflows(reader.projects_root)
    return StepsReading(
        runs=read_selected_runs(flow_runs_from(discovery.run_dirs), reader.files),
        unreadable_directories=tuple(
            run_usage.DirectoryFailure(directory=place) for place in discovery.failed_places
        ),
    )


def report_matches(runs: Iterable[RunReading], reader: Reader) -> tuple[str, ...]:
    """The parent sessions this report shows that the telemetry stream holds agent rows for, in
    the order the workflow runs are displayed, once each — `()` for a reader with no
    `notes_reader`, which reads no stream at all."""
    if reader.notes_reader is None:
        return ()
    return transcript_notes.matched_sessions(
        (run.flow_run.session_id for run in runs), reader.notes_reader
    )


def build_reader(projects_root: Path | None = None) -> Reader:
    """Wire the real port. The only place `Files()` is constructed. A `projects_root` of `None`
    resolves to `~/.claude/projects`, here rather than at an argparse default or inside a unit's
    own body."""
    default_root = Path.home() / ".claude" / "projects"
    return Reader(
        files=Files(),
        projects_root=projects_root if projects_root is not None else default_root,
        notes_reader=stream_usage.build_reader(),
    )


# ── Task 8 — the CLI ────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = run_usage.CommandParser(
        prog="ka-telemetry steps",
        next_step="Run `ka-telemetry steps --help` to see what it accepts.",
        description=(
            "Reports the tokens each step of a workflow run used, read from the workflow run's "
            "journal and its agents' transcripts. An agent whose journal entry names no step is "
            "grouped under its agent type and marked [fallback]. Each model's tokens are shown "
            "in separate classes (input, output, thinking, cache_read, cache_write_1h, "
            "cache_write_5m) and never added together. Thinking tokens are part of output, not "
            "added to it."
        ),
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "the directory that holds Claude Code's transcripts; workflow runs are found under "
            "<project folder>/<session id>/subagents/workflows/<workflow run id>/ "
            "(default: ~/.claude/projects)"
        ),
    )
    parser.add_argument(
        "--wf",
        default=None,
        metavar="WF_ID",
        help="report only the workflow run with this id (default: every workflow run found)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON instead of a table"
    )
    args = parser.parse_args(argv)

    reader = build_reader(args.projects_root)
    discovery = reader.files.discover_workflows(reader.projects_root)
    all_runs = flow_runs_from(discovery.run_dirs)
    unreadable_directories = [
        run_usage.DirectoryFailure(directory=place) for place in discovery.failed_places
    ]

    if args.wf is not None:
        selected = [r for r in all_runs if r.wf_id == args.wf]
        if not selected:
            print(
                f"error: no workflow run found for --wf {args.wf} under {reader.projects_root} "
                "— run `ka-telemetry steps` without --wf to see the workflow runs found there",
                file=sys.stderr,
            )
            for failure in unreadable_directories:
                print(
                    WF_NOT_LOOKED_IN_LINE.format(directory=failure.directory), file=sys.stderr
                )
            return 1
    else:
        selected = list(all_runs)

    readings = list(read_selected_runs(selected, reader.files))

    if args.json:
        print(render_json(readings, unreadable_directories=unreadable_directories))
    else:
        text = render(
            readings,
            matched_sessions=report_matches(readings, reader),
            unreadable_directories=unreadable_directories,
        )
        print(run_usage.printable(text), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
