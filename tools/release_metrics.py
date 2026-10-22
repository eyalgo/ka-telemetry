"""The release check's measurements: one worker per metric, one result per worker.

`tools/release_check.py` makes a disposable copy of the checkout and runs each registered metric
here, as a child process of its own, against that copy. A worker measures one metric and prints
one JSON object, `{"metric": int, "holds": bool, "diagnostics": [...]}`; it prints no verdict
text, because the words a person reads are the release check's. A diagnostic is
`{"input_or_operation": str, "reason": str}` plus a `kind` the release check chooses its message
by, and an optional `data` object for the figures a message names.

This module imports only the standard library at the top, because the release check imports it
from the original checkout, where nothing of the product may be imported. A measurement imports
what it measures inside its own function, after `root_imports_at` has pointed the import path at
the scratch copy, so the caller's installed package and original checkout are never reached.

The protocol is private: it adds no installed command and no `ka-telemetry` option.
"""

from __future__ import annotations

import argparse
import contextlib
import email.message
import email.parser
import hashlib
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True

KIND_STOPPED = "stopped"
KIND_UNKNOWN_METRIC = "unknown_metric"
KIND_REPORTED = "reported"
KIND_DIFFERENCE = "difference"
KIND_UNREADABLE_INPUT = "unreadable_input"
KIND_COMMAND_FAILED = "command_failed"
KIND_SESSION_APART = "session_apart"
KIND_SESSION_NO_METRIC = "session_no_metric"
KIND_SESSION_MISSING = "session_missing"
KIND_APART_WITHOUT_CAUSE = "apart_without_cause"
KIND_NO_METRIC_WITHOUT_CAUSE = "no_metric_without_cause"
KIND_COUNTED_TWICE = "counted_twice"
KIND_COUNT_UNFINISHED = "count_unfinished"
KIND_EXAMPLE_FAILED = "example_failed"
KIND_EXAMPLE_FAILED_NO_DOCUMENT = "example_failed_without_document"
KIND_BUILD_FAILED = "build_failed"
KIND_PYTEST_MISSING = "pytest_missing"
KIND_INSTALL_PACKAGES = "install_packages"
KIND_INSTALL_TARGET_MISSING = "install_target_missing"
KIND_INSTALL_STEP_FAILED = "install_step_failed"
KIND_NETWORK_ATTEMPT = "network_attempt"
KIND_NETWORK_DEFAULT_ADDRESS = "network_default_address"
KIND_NETWORK_UNMEASURED = "network_command_unmeasured"


@dataclass(frozen=True)
class Diagnostic:
    """What did not hold, or could not be measured: the input or operation and the reason, and the
    kind of failure the release check words it by."""

    kind: str
    input_or_operation: str
    reason: str
    data: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        member = {
            "kind": self.kind,
            "input_or_operation": self.input_or_operation,
            "reason": self.reason,
        }
        if self.data:
            member["data"] = self.data
        return member


@dataclass(frozen=True)
class WorkerResult:
    """One metric's outcome: `holds` is true only for a completed measurement that found nothing."""

    metric: int
    holds: bool
    diagnostics: tuple[Diagnostic, ...] = ()


class MeasurementIncomplete(Exception):
    """A measurement that could not be finished: the input or operation that stopped it and why."""

    def __init__(
        self, input_or_operation: str, reason: str, *, kind: str = KIND_STOPPED, data=None
    ) -> None:
        super().__init__(f"{input_or_operation}: {reason}")
        self.diagnostic = Diagnostic(kind, input_or_operation, reason, dict(data or {}))


Measurement = Callable[[Path], list[Diagnostic]]
MEASUREMENTS: dict[int, Measurement] = {}
"""The registry, keyed by integer metric number. A measurement takes the scratch checkout and
returns what did not hold, an empty list when the metric holds."""


def measurement(metric: int) -> Callable[[Measurement], Measurement]:
    """Register the decorated function as the measurement of `metric`."""

    def register(function: Measurement) -> Measurement:
        MEASUREMENTS[metric] = function
        return function

    return register


def failed(metric: int, diagnostic: Diagnostic) -> WorkerResult:
    return WorkerResult(metric, False, (diagnostic,))


def run_worker(
    metric: int, checkout: Path, *, measurements: dict[int, Measurement] | None = None
) -> WorkerResult:
    """Run `metric` against the scratch `checkout`. An unknown number is refused, never held."""
    registry = MEASUREMENTS if measurements is None else measurements
    if metric not in registry:
        return failed(
            metric,
            Diagnostic(
                KIND_UNKNOWN_METRIC, f"metric {metric}", "the release check has no such metric"
            ),
        )
    try:
        found = registry[metric](checkout)
    except MeasurementIncomplete as incomplete:
        return failed(metric, incomplete.diagnostic)
    return WorkerResult(metric, not found, tuple(found))


def result_json(result: WorkerResult) -> str:
    return json.dumps(
        {
            "metric": result.metric,
            "holds": result.holds,
            "diagnostics": [diagnostic.as_json() for diagnostic in result.diagnostics],
        }
    )


def _unmeasured(metric: int, reason: str) -> WorkerResult:
    return failed(
        metric, Diagnostic(KIND_STOPPED, f"the measurement of metric {metric}", reason.strip())
    )


def _diagnostic_from(member: object) -> Diagnostic | None:
    if not isinstance(member, dict):
        return None
    operation, reason = member.get("input_or_operation"), member.get("reason")
    kind, data = member.get("kind", KIND_REPORTED), member.get("data", {})
    if not (
        isinstance(operation, str)
        and isinstance(reason, str)
        and isinstance(kind, str)
        and isinstance(data, dict)
    ):
        return None
    return Diagnostic(kind, operation, reason, data)


def read_result(
    metric: int, stdout: str | None, *, returncode: int | None, stderr: str
) -> WorkerResult:
    """What the release check makes of one worker's raw result. A crashed worker, an absent result
    and a malformed one are each a failed measurement of `metric`, never a hold."""
    if returncode not in (0, None):
        return _unmeasured(metric, stderr or f"the worker exited with status {returncode}")
    if stdout is None or not stdout.strip():
        return _unmeasured(metric, stderr)
    try:
        document = json.loads(stdout)
    except ValueError as error:
        return _unmeasured(metric, f"its result is not JSON: {error}")
    members = document.get("diagnostics") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or type(document.get("metric")) is not int
        or document["metric"] != metric
        or not isinstance(document.get("holds"), bool)
        or not isinstance(members, list)
    ):
        return _unmeasured(metric, f"its result is not the one metric {metric} should give")
    diagnostics = [_diagnostic_from(member) for member in members]
    if None in diagnostics or document["holds"] == bool(diagnostics):
        return _unmeasured(metric, "its result and its diagnostics disagree or are malformed")
    return WorkerResult(metric, document["holds"], tuple(diagnostics))


# ── metric 1: every figure the five readers print equals its recorded expected figure ──

REFERENCE_INPUTS = (
    "tests/parity/agents.json",
    "tests/parity/named_sessions.json",
    "tests/parity/projects",
    "tests/stream_parity/logs.jsonl",
    "tests/stream_parity/metrics.jsonl",
)
"""What the reference commands read besides the nine expected files, which the shared measure
reads itself. A required input that cannot be read is named, never left to a command's exit."""


def _parity_measure():
    """The shared comparison support, imported from the scratch checkout the path now points at."""
    import e2e.parity_measure

    return e2e.parity_measure


def _unreadable(path: Path) -> OSError | None:
    try:
        if path.is_dir():
            next(iter(os.scandir(path)), None)
        else:
            path.read_bytes()
    except OSError as error:
        return error
    return None


def _require_readable(checkout: Path, relatives: tuple[str, ...]) -> None:
    for relative in relatives:
        error = _unreadable(checkout / relative)
        if error is not None:
            raise MeasurementIncomplete(
                relative, error.strerror or str(error), kind=KIND_UNREADABLE_INPUT
            )


def _as_typed(checkout: Path, arguments: tuple[str, ...]) -> str:
    """The command as a person types it from the checkout's top directory."""
    prefix = str(checkout) + os.sep
    shown = [argument.removeprefix(prefix) for argument in arguments]
    return "python3 -m ka_telemetry " + " ".join(shlex.quote(argument) for argument in shown)


class CommandDidNotComplete(Exception):
    """A source command that could not be started or that exited with a failure: the command as a
    person types it, and what it gave as its error."""

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"{command}: {reason}")
        self.command = command
        self.reason = reason


def run_source_command(checkout: Path, home: Path, arguments: tuple[str, ...]) -> str:
    """Run `python3 -m ka_telemetry <arguments>` from the scratch checkout's source under an empty
    `home`, and give its standard output. A command that does not complete raises
    `CommandDidNotComplete`."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env.update(HOME=str(home), PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE="1")
    typed = _as_typed(checkout, arguments)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "ka_telemetry", *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=home,
        )
    except OSError as failure:
        raise CommandDidNotComplete(typed, failure.strerror or str(failure)) from failure
    if done.returncode != 0:
        raise CommandDidNotComplete(typed, done.stderr.strip() or f"exit code {done.returncode}")
    return done.stdout


def source_invoker(checkout: Path, home: Path) -> Callable[[str, tuple[str, ...]], str]:
    """The reference commands for the shared measure, run from the scratch source."""

    def invoke(label: str, arguments: tuple[str, ...]) -> str:
        try:
            return run_source_command(checkout, home, arguments)
        except CommandDidNotComplete as failure:
            raise _parity_measure().MeasurementError(
                label, failure.reason, command=failure.command
            ) from failure

    return invoke


def _incomplete_from(error) -> MeasurementIncomplete:
    """What stopped the shared measure, as the diagnostic the release check words it by."""
    if error.input_path is not None:
        return MeasurementIncomplete(error.input_path, error.reason, kind=KIND_UNREADABLE_INPUT)
    if error.command is not None:
        return MeasurementIncomplete(error.command, error.reason, kind=KIND_COMMAND_FAILED)
    return MeasurementIncomplete(error.operation, error.reason)


@measurement(1)
def reference_figures_equal_their_expected_figures(checkout: Path) -> list[Diagnostic]:
    """Run every reference command from the scratch source and hold each printed figure to the
    one the reference set records. No build, no fixture-hash gate and no historical copy reader
    stands between the checkout and the verdict."""
    _require_readable(checkout, REFERENCE_INPUTS)
    parity = _parity_measure()
    with tempfile.TemporaryDirectory(prefix="release-check-home-") as home:
        try:
            differences = parity.measure(
                source_invoker(checkout, Path(home)),
                parity_root=checkout / "tests" / "parity",
                stream_root=checkout / "tests" / "stream_parity",
            )
        except parity.MeasurementError as error:
            raise _incomplete_from(error) from error
    return [
        Diagnostic(KIND_DIFFERENCE, difference.expected_file, difference.detail)
        for difference in differences
    ]


# ── metric 2: cost totals agree where the reference set records them agreeing ──

STREAM_BASELINE = "tests/stream_parity/expected/agents.json"
STREAM_DATA = "tests/stream_parity"
CHECK_DECISIONS = (True, False, None)


def read_recorded_agreement(checkout: Path) -> set[str]:
    """The sessions the reference set records as agreeing within 1%, by session id."""

    def unreadable(reason: str) -> MeasurementIncomplete:
        return MeasurementIncomplete(STREAM_BASELINE, reason, kind=KIND_UNREADABLE_INPUT)

    try:
        text = (checkout / STREAM_BASELINE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise unreadable(getattr(error, "strerror", None) or str(error)) from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise unreadable(f"it is not JSON: {error}") from error
    checks = document.get("metrics_check") if isinstance(document, dict) else None
    if not isinstance(checks, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("session_id"), str) for entry in checks
    ):
        raise unreadable("it has no list of sessions under metrics_check")
    return {entry["session_id"] for entry in checks if entry.get("within_tolerance") is True}


def index_checks(checks: object, command: str) -> dict[str, dict]:
    """The check's sessions by session id. An entry the check could not have printed, or a session
    printed twice, is output that cannot be read as the check's decisions."""

    def malformed(reason: str) -> MeasurementIncomplete:
        return MeasurementIncomplete(command, reason, kind=KIND_COMMAND_FAILED)

    if not isinstance(checks, list):
        raise malformed("its output has no list of sessions under metrics_check")
    indexed: dict[str, dict] = {}
    for entry in checks:
        if not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str):
            raise malformed("a session in its output has no session id")
        within = entry.get("within_tolerance")
        if not any(within is decision for decision in CHECK_DECISIONS):
            raise malformed(f"session {entry['session_id']} has within_tolerance {within!r}")
        if entry["session_id"] in indexed:
            raise malformed(f"session {entry['session_id']} appears twice")
        indexed[entry["session_id"]] = entry
    return indexed


def read_check_output(text: str, command: str) -> dict[str, dict]:
    """What `check --json` printed, as its sessions by session id."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise MeasurementIncomplete(
            command, f"its output is not JSON: {error}", kind=KIND_COMMAND_FAILED
        ) from error
    if not isinstance(document, dict):
        raise MeasurementIncomplete(
            command, "its output is not a JSON object", kind=KIND_COMMAND_FAILED
        )
    return index_checks(document.get("metrics_check"), command)


def _has_a_cause(entry: dict) -> bool:
    causes = entry.get("possible_causes")
    return (
        isinstance(causes, list)
        and any(isinstance(cause, str) and cause.strip() for cause in causes)
    )


def judge_checks(agreeing: set[str], reported: dict[str, dict]) -> list[Diagnostic]:
    """Hold the check's own decisions to the reference set. Every session recorded as agreeing is
    reported, with a decision of exactly true; every session reported outside 1% or with no
    metric names a possible cause. The 1% threshold is the check's and is never recomputed."""
    found = []
    for session_id in sorted(agreeing):
        entry = reported.get(session_id)
        if entry is None:
            kind, reason = KIND_SESSION_MISSING, "the check does not report this session at all"
        elif entry["within_tolerance"] is False:
            kind, reason = KIND_SESSION_APART, "the check reports its costs more than 1% apart"
        elif entry["within_tolerance"] is None:
            kind, reason = KIND_SESSION_NO_METRIC, "the check reports no cost metric for it"
        else:
            continue
        found.append(Diagnostic(kind, session_id, reason, {"session_id": session_id}))
    for session_id, entry in sorted(reported.items()):
        if entry["within_tolerance"] is True or _has_a_cause(entry):
            continue
        apart = entry["within_tolerance"] is False
        found.append(
            Diagnostic(
                KIND_APART_WITHOUT_CAUSE if apart else KIND_NO_METRIC_WITHOUT_CAUSE,
                session_id,
                "the check reports "
                + ("its cost totals more than 1% apart" if apart else "no cost metric for it")
                + " and names no possible cause",
                {"session_id": session_id},
            )
        )
    return found


@measurement(2)
def cost_totals_agree_within_one_percent(checkout: Path) -> list[Diagnostic]:
    """Run the check on the scratch source and hold its decisions to the recorded agreement."""
    agreeing = read_recorded_agreement(checkout)
    arguments = ("check", "--dir", str(checkout / STREAM_DATA), "--json")
    typed = _as_typed(checkout, arguments)
    with tempfile.TemporaryDirectory(prefix="release-check-home-") as home:
        try:
            printed = run_source_command(checkout, Path(home), arguments)
        except CommandDidNotComplete as failure:
            raise MeasurementIncomplete(
                failure.command, failure.reason, kind=KIND_COMMAND_FAILED
            ) from failure
    return judge_checks(agreeing, read_check_output(printed, typed))


# ── metric 3: no response is counted more than once by the runs or the steps report ──

PARITY_DATA = "tests/parity"
REPORTS = ("runs", "steps")


@dataclass(frozen=True)
class Repeat:
    """A response a report counted more than once, and each transcript it was counted from."""

    response_id: str
    transcripts: tuple[str, ...]


@dataclass(frozen=True)
class ReportAudit:
    """What auditing one report found: the responses it counted more than once, how many
    responses it counted in all, and the reading the audit made of the report."""

    report: str
    repeats: tuple[Repeat, ...]
    counted: int
    reading: object


@dataclass
class _ObservedCall:
    """One call of the reader that yields a transcript's responses, and the identity of every
    contribution it accepted. An identity is the response id, which is the same in every
    transcript; a record with no id has the transcript and its place among that call's accepted
    records, because two such records are two responses."""

    path: Path
    accepted: list[tuple] = field(default_factory=list)
    without_id: int = 0

    def identity_of(self, response_id: str | None) -> tuple:
        if response_id is not None:
            return ("id", response_id)
        self.without_id += 1
        return ("record", str(self.path), self.without_id)


class _Observer:
    """Watches the readers count, and changes nothing they do. Contribution acceptance tells it
    which response a usage object belongs to; the reader's generator tells it which of those the
    transcript read yielded; the accumulator tells it which yields reached a report row. It keeps
    a reference to each object it identifies, so an identity cannot be reused while it is held."""

    def __init__(self) -> None:
        self.calls: list[_ObservedCall] = []
        self.rows: list[list[tuple[_ObservedCall, tuple]]] = []
        self._current: _ObservedCall | None = None
        self._accepted: dict[int, tuple] = {}
        self._yielded: dict[int, tuple] = {}
        # Responses a read handed over that no record it read was accepted as: a count with no
        # provenance, which the audit stops on rather than passing over or crashing on.
        self.unaccepted = 0

    def wrap_contribution(self, original):
        def contribution(record):
            found = original(record)
            if isinstance(found, tuple):
                usage, _model, response_id, _timestamp = found
                call = self._current
                if call is None:
                    raise MeasurementIncomplete(
                        "the response count", "a response was accepted outside a transcript read"
                    )
                identity = call.identity_of(response_id)
                call.accepted.append(identity)
                self._accepted[id(usage)] = (usage, call, identity)
            return found

        return contribution

    def wrap_usage_records(self, original):
        def usage_records(
            path, files, *, unreadable_lines=None, response_scope=None, row_key=None
        ):
            call = _ObservedCall(Path(path))
            self.calls.append(call)
            inner = original(
                path,
                files,
                unreadable_lines=unreadable_lines,
                response_scope=response_scope,
                row_key=row_key,
            )
            while True:
                self._current = call
                try:
                    triple = next(inner)
                except StopIteration:
                    return
                finally:
                    self._current = None
                accepted = self._accepted.get(id(triple[0]))
                if accepted is None:
                    self.unaccepted += 1
                else:
                    self._yielded[id(triple)] = (triple, *accepted[1:])
                yield triple

        return usage_records

    def wrap_spend_by_model(self, original):
        def spend_by_model(records):
            records = list(records)
            self.rows.append(
                [self._yielded[id(r)][1:] for r in records if id(r) in self._yielded]
            )
            return original(records)

        return spend_by_model


@contextlib.contextmanager
def _observing(run_usage):
    """Install the observer on the reader's functions, and put the originals back however the
    audit ends."""
    observer = _Observer()
    originals = (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model)
    run_usage.usage_records = observer.wrap_usage_records(originals[0])
    run_usage._contribution = observer.wrap_contribution(originals[1])
    run_usage.spend_by_model = observer.wrap_spend_by_model(originals[2])
    try:
        yield observer
    finally:
        run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model = originals


def _shown(identity: tuple) -> str:
    if identity[0] == "id":
        return identity[1]
    return f"record {identity[2]} of {identity[1]}, which carries no response id"


def _repeats(
    counted_by_row: list[list[tuple[_ObservedCall, tuple]]], checkout: Path
) -> tuple[Repeat, ...]:
    sources: dict[tuple, list[Path]] = {}
    for row in counted_by_row:
        for call, identity in row:
            sources.setdefault(identity, []).append(call.path)
    return tuple(
        Repeat(
            _shown(identity),
            tuple(sorted({path.relative_to(checkout).as_posix() for path in paths})),
        )
        for identity, paths in sources.items()
        if len(paths) > 1
    )


def _unfinished(report: str, reason: str) -> MeasurementIncomplete:
    return MeasurementIncomplete(
        f"counting the responses the {report} report counts",
        reason,
        kind=KIND_COUNT_UNFINISHED,
        data={"report": report},
    )


def _relative(checkout: Path, path: Path) -> str:
    try:
        return path.relative_to(checkout).as_posix()
    except ValueError:
        return str(path)


def _check_accounted(report: str, observer: _Observer, counted_rows: list) -> None:
    accepted = set(itertools.chain.from_iterable(call.accepted for call in observer.calls))
    counted = {identity for row in counted_rows for _call, identity in row}
    if accepted - counted:
        raise _unfinished(
            report, f"{len(accepted - counted)} accepted responses reached no row of the report"
        )


def _audit_runs(checkout: Path) -> ReportAudit:
    from ka_telemetry import run_usage

    parity = checkout / PARITY_DATA
    _require_readable(
        checkout, (f"{PARITY_DATA}/agents.json", f"{PARITY_DATA}/named_sessions.json")
    )
    _require_readable(checkout, (f"{PARITY_DATA}/projects",))
    listing = (parity / "agents.json").read_text(encoding="utf-8")
    sessions = json.loads((parity / "named_sessions.json").read_text(encoding="utf-8"))
    reader = run_usage.Reader(
        files=run_usage.Files(), processes=run_usage.Processes(), projects_root=parity / "projects"
    )
    with _observing(run_usage) as observer:
        ledger = run_usage.read_runs(listing, sessions, reader)
    for failure in ledger.unreadable_directories:
        raise MeasurementIncomplete(
            _relative(checkout, failure.directory), failure.reason, kind=KIND_UNREADABLE_INPUT
        )
    stopped = (
        run_usage.UnreadableRunReason.COULD_NOT_BE_OPENED,
        run_usage.UnreadableRunReason.PLACE_COULD_NOT_BE_LOOKED_IN,
    )
    for session_id, reason in zip(
        ledger.coverage.unreadable_ids, ledger.unreadable_reasons, strict=True
    ):
        if reason in stopped:
            raise _unfinished("runs", f"the transcript of session {session_id} could not be read")
    if len(observer.rows) != len(ledger.rows) or observer.unaccepted:
        raise _unfinished("runs", "the report's rows and the responses counted did not line up")
    _check_accounted("runs", observer, observer.rows)
    counted = sum(len(row) for row in observer.rows)
    return ReportAudit("runs", _repeats(observer.rows, checkout), counted, ledger)


def _agent_transcript(run, agent_id: str) -> Path:
    """Where the workflow run's agent transcript is: how an observed read is matched to an agent,
    whatever order the report read them in."""
    return run.flow_run.run_dir / f"agent-{agent_id}.jsonl"


def _audit_steps(checkout: Path) -> ReportAudit:
    from ka_telemetry import flow_usage, run_usage

    projects = checkout / PARITY_DATA / "projects"
    _require_readable(checkout, (f"{PARITY_DATA}/projects",))
    reader = flow_usage.Reader(files=flow_usage.Files(), projects_root=projects)
    with _observing(run_usage) as observer:
        steps = flow_usage.read_steps(reader)
    for failure in steps.unreadable_directories:
        raise MeasurementIncomplete(
            _relative(checkout, failure.directory), failure.reason, kind=KIND_UNREADABLE_INPUT
        )
    readings = [(run, agent) for run in steps.runs for agent in run.agents]
    for run in steps.runs:
        journal = _relative(checkout, run.flow_run.run_dir / "journal.jsonl")
        if run.unreadable_journal_files:
            raise MeasurementIncomplete(journal, "it could not be read", kind=KIND_UNREADABLE_INPUT)
        if run.agent_files_failure is not None:
            failure = run.agent_files_failure
            raise MeasurementIncomplete(
                _relative(checkout, failure.directory), failure.reason, kind=KIND_UNREADABLE_INPUT
            )
    for run, agent in readings:
        if agent.unreadable_agent_transcript:
            name = f"agent-{agent.agent_id}.jsonl"
            raise MeasurementIncomplete(
                _relative(checkout, run.flow_run.run_dir / name),
                "it could not be read",
                kind=KIND_UNREADABLE_INPUT,
            )
    transcripts = [_agent_transcript(run, agent.agent_id) for run, agent in readings]
    if sorted(call.path for call in observer.calls) != sorted(transcripts) or observer.unaccepted:
        raise _unfinished("steps", "the agents read and the responses counted did not line up")
    by_transcript: dict[Path, list[tuple[_ObservedCall, tuple]]] = {}
    for row in observer.rows:
        for call, identity in row:
            by_transcript.setdefault(call.path, []).append((call, identity))
    counted_rows = []
    for run in steps.runs:
        for row in flow_usage.aggregate(run):
            counted_rows.append(
                [
                    entry
                    for agent_id in row.agent_ids
                    for entry in by_transcript.get(_agent_transcript(run, agent_id), [])
                ]
            )
    _check_accounted("steps", observer, counted_rows)
    counted = sum(len(row) for row in counted_rows)
    return ReportAudit("steps", _repeats(counted_rows, checkout), counted, steps)


def audit_report(report: str, checkout: Path) -> ReportAudit:
    """Audit `report` ("runs" or "steps") over the scratch checkout's `tests/parity`. A required
    input that cannot be read, or a count that cannot be accounted for, raises
    `MeasurementIncomplete`."""
    audit = {"runs": _audit_runs, "steps": _audit_steps}[report]
    try:
        return audit(checkout)
    except (OSError, ValueError) as error:
        raise _unfinished(report, getattr(error, "strerror", None) or str(error)) from error


def _repeat_diagnostic(report: str, repeat: Repeat) -> Diagnostic:
    return Diagnostic(
        KIND_COUNTED_TWICE,
        repeat.response_id,
        f"the {report} report counts it more than once, from {', '.join(repeat.transcripts)}",
        {
            "report": report,
            "response_id": repeat.response_id,
            "transcripts": list(repeat.transcripts),
        },
    )


@measurement(3)
def no_response_counted_more_than_once(checkout: Path) -> list[Diagnostic]:
    """Audit the runs report and the steps report separately; a response shared by the two is a
    duplicate in neither."""
    found: list[Diagnostic] = []
    for report in REPORTS:
        try:
            audit = audit_report(report, checkout)
        except MeasurementIncomplete as incomplete:
            if incomplete.diagnostic not in found:
                found.append(incomplete.diagnostic)
            continue
        found.extend(_repeat_diagnostic(report, repeat) for repeat in audit.repeats)
    return found


# ── metric 5: installing what is built from the checkout installs only ka-telemetry ──

INSTALL_TARGET = "ka-telemetry"
BOOTSTRAP_NAMES = frozenset({"pip", "setuptools", "wheel"})
"""The packages set aside by name, whether or not a new environment holds them."""
BUILD_STEP, INSTALL_STEP, LIST_STEP = "build", "install", "list"
README_STEP, PYTHON_STEP, PIPX_STEP = "readme", "python", "pipx"
CAUSE_README_UNREADABLE, CAUSE_COMMAND_REFUSED = "readme_unreadable", "command_refused"
CAUSE_PYTHON_UNAVAILABLE, CAUSE_PYTHON_MISMATCH = "python_unavailable", "python_mismatch"
CAUSE_PIPX_UNAVAILABLE, CAUSE_PIPX_NOT_PREPARED = "pipx_unavailable", "pipx_not_prepared"
CAUSE_TARGET_ABSENT, CAUSE_COMMAND_FAILED = "target_absent", "command_failed"
CAUSE_COMMAND_NOT_STARTED, CAUSE_NOT_THIS_BUILD = "command_not_started", "not_this_build"
CAUSE_LIST_UNREADABLE, CAUSE_LIST_EMPTY = "list_unreadable", "list_empty"
"""The closed `cause` values of bar 5's diagnostics. The release check words a message by the
cause, never by matching a tool's own prose."""
(
    READING_FILE_UNREADABLE, READING_NO_INSTALL_SECTION, READING_NO_SHELL_BLOCK,
    READING_BLOCK_UNCLOSED, READING_NOT_ONE_LINE, READING_WORDS_UNREADABLE,
    READING_TARGET_NOT_NAMED,
) = (
    "file_unreadable", "no_install_section", "no_shell_block", "block_unclosed", "not_one_line",
    "words_unreadable", "target_not_named",
)
"""Why the README's install command could not be read: the `reading` of a `readme_unreadable`
diagnostic."""
(
    CANDIDATE_TOO_OLD, CANDIDATE_IN_VIRTUAL_ENVIRONMENT, CANDIDATE_NOT_MARKED,
    CANDIDATE_NO_PIP, CANDIDATE_NOT_REFUSED, CANDIDATE_COPY_FAILED,
) = (
    "too_old", "in_virtual_environment", "not_marked", "no_pip", "not_refused", "copy_failed",
)
"""What became of each Python tried: the `outcome` of a member of a `python_unavailable`
diagnostic's `candidates`."""
INSTALL_OPERATION = "the README's install command"
UNREACHABLE_PROXY = "http://127.0.0.1:1"
"""Every address the install is given is sent here, so a requirement that names its own address
(a direct reference) finds nothing listening and fetches nothing."""
COPY_LEFT_OUT = frozenset({".git", ".worktrees", "build", "dist"})
NAME_RUN = re.compile(r"[-_.]+")
REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
EXTRA_MARKER = re.compile(r"\bextra\s*==")
METADATA_MEMBER = re.compile(r"[^/]+\.dist-info/METADATA")


def normalize_name(name: str) -> str:
    """A project name as the Python Package Index compares it: case ignored, and any run of `-`,
    `_` and `.` the same as one `-`."""
    return NAME_RUN.sub("-", name).lower()


class InstallStepFailed(Exception):
    """A step of the install measure that stopped: which step, and what the tool reported."""

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


# The README's install command: read from the checkout, admitted, then launched as typed.

README_FILE = "README.md"
INSTALL_HEADING = re.compile(r"##[ \t]+Install[ \t]*")
LEVEL_TWO_HEADING = re.compile(r"##(?:[ \t]|$)")
FENCE_OPENING = re.compile(r"(`{3,}|~{3,})[ \t]*([^\s`]*)")
SHELL_LANGUAGES = frozenset({"sh", "shell", "bash"})
PIPX_INSTALL = ("pipx", "install", INSTALL_TARGET)
PIP_INSTALL = ("python3", "-m", "pip", "install", INSTALL_TARGET)
ADMITTED_FORMS = (PIPX_INSTALL, PIP_INSTALL)
"""The two command lines the release check runs: the install the README gives, and the plain pip
install a regression test puts there to see line 5 fail."""
CHAIN_WORDS = frozenset({";", "&&", "||", "|", "&"})
PERMITTED_OPTION = "-m"


class InstallMeasureStopped(Exception):
    """A step of bar 5 that stopped before the README's command ran. It carries the `Diagnostic`
    line 5 reports, so the measure returns it and every other bar still runs."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.reason)
        self.diagnostic = diagnostic


class InstallCommandError(InstallMeasureStopped):
    """The README's install command could not be read, or is not one that is run: nothing was
    launched."""


@dataclass(frozen=True)
class InstallCommand:
    """The README's install command: its words as typed, the line they were read from, and where
    in the README that line stands."""

    words: tuple[str, ...]
    text: str
    source: str
    line: int


@dataclass(frozen=True)
class _Heading:
    number: int
    text: str


@dataclass(frozen=True)
class _Block:
    opened: int
    language: str
    body: tuple[tuple[int, str], ...]
    closed: bool


def _command_error(
    cause: str, reason: str, *, reading: str | None = None, line: int | None = None,
    command: str | None = None,
) -> InstallCommandError:
    data: dict = {"step": README_STEP, "cause": cause, "source": README_FILE}
    for name, value in (("reading", reading), ("line", line), ("command", command)):
        if value is not None:
            data[name] = value
    return InstallCommandError(
        Diagnostic(KIND_INSTALL_STEP_FAILED, README_FILE, reason, data)
    )


def _readme_unreadable(reading: str, reason: str, **where) -> InstallCommandError:
    return _command_error(CAUSE_README_UNREADABLE, reason, reading=reading, **where)


def _document_parts(text: str) -> list[_Heading | _Block]:
    """The level-two headings and fenced blocks of a Markdown text, in order. A heading inside a
    fence is not a heading, and a fence that is never closed runs to the end of the text."""
    parts: list[_Heading | _Block] = []
    fence: tuple[str, str, int, list[tuple[int, str]]] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is None:
            opening = FENCE_OPENING.match(line)
            if opening:
                fence = (opening.group(1), opening.group(2).lower(), number, [])
            elif LEVEL_TWO_HEADING.match(line):
                parts.append(_Heading(number, line))
            continue
        marker, language, opened, body = fence
        closing = line.strip()
        if closing and set(closing) == {marker[0]} and len(closing) >= len(marker):
            parts.append(_Block(opened, language, tuple(body), True))
            fence = None
        else:
            body.append((number, line))
    if fence is not None:
        parts.append(_Block(fence[2], fence[1], tuple(fence[3]), False))
    return parts


def _first_shell_block(text: str) -> _Block:
    parts = _document_parts(text)
    for index, part in enumerate(parts):
        if isinstance(part, _Heading) and INSTALL_HEADING.fullmatch(part.text):
            break
    else:
        raise _readme_unreadable(
            READING_NO_INSTALL_SECTION, f"{README_FILE} has no section headed Install"
        )
    for part in parts[index + 1 :]:
        if isinstance(part, _Heading):
            break
        if isinstance(part, _Block) and part.language in SHELL_LANGUAGES:
            return part
    raise _readme_unreadable(
        READING_NO_SHELL_BLOCK, "the Install section holds no shell code block"
    )


def read_install_command(checkout: Path) -> InstallCommand:
    """The README's install command, read from `checkout`'s working file: the one line in the first
    shell code block of the `## Install` section, its words decoded as a person types them and
    never run through a shell. A README that cannot give that line raises `InstallCommandError`
    saying which of the cases held."""
    try:
        text = (checkout / README_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as failure:
        reason = getattr(failure, "strerror", None) or str(failure)
        raise _readme_unreadable(READING_FILE_UNREADABLE, reason) from failure
    block = _first_shell_block(text)
    if not block.closed:
        raise _readme_unreadable(
            READING_BLOCK_UNCLOSED, "the first shell code block is never closed", line=block.opened
        )
    if len(block.body) != 1 or not block.body[0][1].strip() or block.body[0][1].lstrip()[0] == "#":
        raise _readme_unreadable(
            READING_NOT_ONE_LINE,
            "the first shell code block does not hold exactly one command line",
            line=block.opened,
        )
    number, line = block.body[0]
    typed = line.strip()
    try:
        words = tuple(shlex.split(typed, comments=True))
    except ValueError as failure:
        raise _readme_unreadable(
            READING_WORDS_UNREADABLE, str(failure), line=number, command=typed
        ) from failure
    if INSTALL_TARGET not in words[1:]:
        raise _readme_unreadable(
            READING_TARGET_NOT_NAMED,
            f"the command does not name {INSTALL_TARGET} as an argument",
            line=block.opened,
            command=typed,
        )
    return InstallCommand(words, typed, README_FILE, number)


def _refusal_reason(words: tuple[str, ...]) -> str:
    """The part of a command that is not one of the admitted forms."""
    for word in words:
        if "<" in word or ">" in word:
            return "a redirection"
        if "$(" in word or "`" in word:
            return "a command substitution"
        if word in CHAIN_WORDS:
            return f"a command chain ({word})"
    if "=" in words[0] and not words[0].startswith("-"):
        return f"an environment assignment ({words[0]})"
    if "/" in words[0]:
        return f"a program named by its path ({words[0]})"
    for word in words[1:]:
        if word.startswith("-") and word != PERMITTED_OPTION:
            return f"the option {word.partition('=')[0]}"
    if words[0] not in {PIPX_INSTALL[0], PIP_INSTALL[0]}:
        return f"the program {words[0]}"
    for form in ADMITTED_FORMS:
        if words[: len(form)] == form:
            return f"the extra words {' '.join(words[len(form) :])}"
    return f"a form other than {' '.join(PIPX_INSTALL)}"


def admit_install_command(command: InstallCommand) -> tuple[str, ...]:
    """The words to run, unchanged, when the command is one of the admitted forms; otherwise raise
    `InstallCommandError` before anything is launched. No shell ever sees these words."""
    if command.words in ADMITTED_FORMS:
        return command.words
    raise _command_error(
        CAUSE_COMMAND_REFUSED,
        _refusal_reason(command.words),
        line=command.line,
        command=command.text,
    )


def launch_install_command(
    command: InstallCommand, run: Callable[..., subprocess.CompletedProcess], **settings
) -> subprocess.CompletedProcess:
    """Run the admitted words through the process seam `run` with `settings`, and nothing else:
    the one place the README's command leaves this module."""
    return run(list(admit_install_command(command)), **settings)


def _tool_report(done: subprocess.CompletedProcess) -> str:
    return done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"

# Preparation for the measured install: a Python that refuses pip installs, and a pipx.

MINIMUM_PYTHON = (3, 11)
NONEXISTENT_PACKAGE = "release-check-no-such-package"
"""What the refusal probe asks pip for: a name no index holds, with no index to ask, so a Python
that does not refuse still installs nothing."""
REFUSAL_REPORT = "externally-managed-environment"
MARKER_FILE = "EXTERNALLY-MANAGED"
MARKER_TEXT = "[externally-managed]\nError=This Python is managed outside pip.\n"
PRIVATE_COPY = "python-copy"
NO_PIP_REPORT = "No module named pip"
FACTS_SCRIPT = (
    "import json, os, sys, sysconfig\n"
    "stdlib = sysconfig.get_path('stdlib')\n"
    "print(json.dumps({'version': list(sys.version_info[:3]), 'prefix': sys.prefix,"
    " 'base_prefix': sys.base_prefix, 'stdlib': stdlib,"
    " 'marker': os.path.exists(os.path.join(stdlib, 'EXTERNALLY-MANAGED')),"
    " 'executable': sys.executable,"
    " 'installation': [sysconfig.get_path(name)"
    " for name in ('purelib', 'platlib', 'scripts', 'data')]}))\n"
)
PIPX_PACKAGE = "pipx"
PIPX_TOOLING = "pipx-tooling"
PIPX_SCRATCH = "pipx"
PIPX_UPGRADE_SHARED = "upgrade-shared"
BACKEND_VARIABLE = "PIPX_DEFAULT_BACKEND"
UV_WORD = re.compile(r"\buv\b")
CLEARED_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "PIP_", "PIPX_", "UV_", "CONDA_")
PREPARATION_OPERATION = "preparing a Python and pipx for the README's install command"
PRIVATE_COPY_IGNORED = ("site-packages", "test", "tests", "__pycache__", MARKER_FILE)


class PreparationFailed(InstallMeasureStopped):
    """The Python or the pipx the measured install needs could not be found, made or readied."""


@dataclass(frozen=True)
class PythonFacts:
    """What a Python says about itself: where it runs from, whether it is a virtual environment's,
    where its standard library is, whether that library carries the externally-managed marker,
    and where pip would install into it (`installation`: its purelib, platlib, scripts and data
    paths)."""

    path: str
    version: tuple[int, int, int]
    prefix: str
    base_prefix: str
    stdlib: str
    marker: bool
    executable: str
    installation: tuple[str, ...] = ()


@dataclass(frozen=True)
class BasePython:
    """The Python the install is measured on, proved to refuse pip installs. `identity` is the
    real path of its base prefix, so a later step can check that an environment made from it has
    this one for its base."""

    executable: Path
    version: tuple[int, int, int]
    identity: str


@dataclass(frozen=True)
class PreparedPipx:
    """A pipx ready for the measured install: where it is, whether the release check brought it,
    and the `PIPX_` settings that keep everything it writes under the scratch directory."""

    executable: Path
    provisioned: bool
    environment: dict[str, str]


def isolated_environment(
    scratch: Path, inherited: Mapping[str, str], *, fetching: bool = False
) -> dict[str, str]:
    """The environment of a child of the measure: the caller's own, less every Python, virtual
    environment, pip, pipx, uv and conda setting and (unless `fetching`) every proxy, with home,
    temporary files, caches and configuration moved under `scratch`, user configuration and user
    site off, and no interpreter fetched. Preparation may fetch tooling, so it keeps the proxies
    the caller needs to reach the network; the measured install does not."""
    kept = {
        key: value
        for key, value in inherited.items()
        if not key.startswith(CLEARED_PREFIXES) and (fetching or not key.lower().endswith("_proxy"))
    }
    home, cache, temporary = scratch / "home", scratch / "cache", scratch / "tmp"
    for directory in (home, cache, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    kept.update(
        HOME=str(home),
        TMPDIR=str(temporary),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CACHE_HOME=str(cache),
        PIP_CACHE_DIR=str(cache / "pip"),
        UV_CACHE_DIR=str(cache / "uv"),
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONNOUSERSITE="1",
        UV_NO_CONFIG="1",
        UV_PYTHON_DOWNLOADS="never",
        PIPX_FETCH_PYTHON="never",
    )
    return kept


def path_python_candidates(environ: Mapping[str, str]) -> list[str]:
    """Every `python3` an executable search of the inherited PATH reaches, in PATH order."""
    found = []
    for directory in environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "python3" if directory else None
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(str(candidate))
    return found


def running_base_python() -> str:
    """The interpreter the release check runs on, or when that is a virtual environment's, the
    Python it was made from."""
    named = Path(sys.base_prefix) / "bin" / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    return str(named) if named.is_file() else sys.executable


def find_pipx_on_path(environ: Mapping[str, str]) -> str | None:
    return shutil.which(PIPX_PACKAGE, path=environ.get("PATH"))


def copy_host_interpreter(facts: PythonFacts, destination: Path) -> Path:
    """A private copy of a Python 3.11 or later, with its own prefix and standard library under
    `destination`. Nothing is written to the host's prefix."""
    major, minor = facts.version[:2]
    shutil.copytree(
        facts.stdlib,
        destination / "lib" / f"python{major}.{minor}",
        ignore=shutil.ignore_patterns(*PRIVATE_COPY_IGNORED),
    )
    binary = destination / "bin" / f"python{major}.{minor}"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(facts.executable, binary)
    link = destination / "bin" / "python3"
    link.symlink_to(binary.name)
    return link


def _inside(path: str | Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


class Preparation:
    """What the measured install needs before it runs: a Python that refuses pip installs, and a
    pipx whose shared pip is ready. Every collaborator is handed in — the process seam, the
    environment, the search for Pythons and for pipx, and the copy — so a test supplies a
    hand-written one. None has a default: the real ones are built by `real_preparation`."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess],
        environ: Mapping[str, str],
        candidates: Callable[[Mapping[str, str]], list[str]],
        copy_interpreter: Callable[[PythonFacts, Path], Path],
        running: str,
        find_pipx: Callable[[Mapping[str, str]], str | None],
    ) -> None:
        self._run = run
        self._environ = dict(environ)
        self._candidates = candidates
        self._copy = copy_interpreter
        self._running = running
        self._find_pipx = find_pipx

    def _launch(self, command: list[str], scratch: Path, environment: dict[str, str]):
        return self._run(
            command, capture_output=True, text=True, check=False, env=environment,
            cwd=str(scratch),
        )

    def _facts(self, path: str, scratch: Path, environment: dict[str, str]) -> PythonFacts | None:
        """What the Python at `path` says about itself, or None when it cannot be run."""
        try:
            done = self._launch([path, "-c", FACTS_SCRIPT], scratch, environment)
            said = json.loads(done.stdout) if done.returncode == 0 else None
            return said and PythonFacts(
                path, tuple(said["version"]), said["prefix"], said["base_prefix"],
                said["stdlib"], bool(said["marker"]), said["executable"],
                tuple(map(str, said.get("installation") or ())),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _probe(self, facts: PythonFacts, scratch: Path, environment: dict[str, str]):
        """A `BasePython` when pip on this Python refuses a trial install as externally managed,
        else the candidate entry saying what it did instead. The trial asks for a package no
        index holds, with none to ask, so a Python that does not refuse installs nothing."""
        command = [
            facts.path, "-m", "pip", "install", "--isolated", "--no-index", "--no-cache-dir",
            "--disable-pip-version-check", "--no-input", NONEXISTENT_PACKAGE,
        ]
        entry = {"python": facts.path, "outcome": CANDIDATE_NOT_REFUSED}
        try:
            done = self._launch(command, scratch, environment)
        except OSError as failure:
            return {**entry, "reason": failure.strerror or str(failure)}
        printed = done.stdout + done.stderr
        if done.returncode != 0 and REFUSAL_REPORT in printed:
            return BasePython(
                Path(facts.path), facts.version, str(Path(facts.base_prefix).resolve())
            )
        if NO_PIP_REPORT in printed:
            return {**entry, "outcome": CANDIDATE_NO_PIP}
        return {**entry, "reason": _tool_report(done)}

    def _verdict(self, facts: PythonFacts, scratch: Path, environment: dict[str, str]):
        entry = {"python": facts.path}
        if facts.version < MINIMUM_PYTHON:
            return {
                **entry, "outcome": CANDIDATE_TOO_OLD,
                "version": ".".join(map(str, facts.version)),
            }
        if facts.prefix != facts.base_prefix:
            return {**entry, "outcome": CANDIDATE_IN_VIRTUAL_ENVIRONMENT}
        if not facts.marker:
            return {**entry, "outcome": CANDIDATE_NOT_MARKED}
        return self._probe(facts, scratch, environment)

    def _private_copy(self, source: PythonFacts, scratch: Path, environment: dict[str, str]):
        """A `BasePython` made from a private copy of `source` inside `scratch`, marked and probed
        there, or the `copy_failed` entry saying why not. pip goes into the copy first, because a
        marked Python refuses even that. pip is installed, and the marker written, only after the
        copy's own standard library and every path pip installs into are seen to lie inside
        `scratch`."""
        destination = scratch / PRIVATE_COPY
        try:
            executable = self._copy(source, destination)
            facts = self._facts(str(executable), scratch, environment)
            if facts is None:
                raise OSError(f"{executable} cannot be run")
            if not _inside(facts.stdlib, destination):
                raise OSError(f"its standard library, {facts.stdlib}, is outside {destination}")
            if not facts.installation:
                raise OSError("it reported no paths that pip installs into")
            for place in facts.installation:
                if not _inside(place, destination):
                    raise OSError(f"pip would install into {place}, outside {destination}")
            installed = self._launch(
                [facts.path, "-m", "ensurepip", "--default-pip"], scratch, environment
            )
            if installed.returncode != 0:
                raise OSError(_tool_report(installed))
            Path(facts.stdlib, MARKER_FILE).write_text(MARKER_TEXT, encoding="utf-8")
            verdict = self._probe(facts, scratch, environment)
        except OSError as failure:
            return _copy_failed(source, failure.strerror or str(failure))
        if isinstance(verdict, dict):
            return _copy_failed(source, verdict.get("reason") or "pip did not refuse the trial")
        return verdict

    def base_python(self, scratch: Path) -> BasePython:
        """A Python 3.11 or later outside a virtual environment whose pip refuses a trial install
        as externally managed: the running interpreter's base, then each `python3` on the PATH,
        then a private copy of the first of those that is new enough. Raises `PreparationFailed`,
        naming every Python tried, when none refuses."""
        scratch.mkdir(parents=True, exist_ok=True)
        environment = isolated_environment(scratch, self._environ)
        tried: list[dict] = []
        copy_source: PythonFacts | None = None
        for path in dict.fromkeys([self._running, *self._candidates(self._environ)]):
            facts = self._facts(path, scratch, environment)
            if facts is None:
                continue
            verdict = self._verdict(facts, scratch, environment)
            if isinstance(verdict, BasePython):
                return verdict
            tried.append(verdict)
            if copy_source is None and _can_be_copied(facts):
                copy_source = facts
        if copy_source is not None:
            made = self._private_copy(copy_source, scratch, environment)
            if isinstance(made, BasePython):
                return made
            tried.append(made)
        raise PreparationFailed(
            Diagnostic(
                KIND_INSTALL_STEP_FAILED,
                PREPARATION_OPERATION,
                "no Python 3.11 or later that refuses pip installs as externally managed was "
                "found or made",
                {"step": PYTHON_STEP, "cause": CAUSE_PYTHON_UNAVAILABLE, "candidates": tried},
            )
        )

    def _pipx_variables(self, scratch: Path, base: BasePython) -> dict[str, str]:
        """Where everything pipx writes goes, and the Python it makes environments from."""
        root = scratch / PIPX_SCRATCH
        places = {
            "PIPX_HOME": "home", "PIPX_BIN_DIR": "bin", "PIPX_MAN_DIR": "man",
            "PIPX_SHARED_LIBS": "shared", "PIPX_COMPLETION_DIR": "completions",
        }
        for directory in places.values():
            (root / directory).mkdir(parents=True, exist_ok=True)
        variables = {name: str(root / directory) for name, directory in places.items()}
        variables["PIPX_DEFAULT_PYTHON"] = str(base.executable)
        return variables

    def _provision(self, scratch: Path, base: BasePython) -> Path:
        """pipx in a tooling environment of its own under `scratch`, never the environment the
        README's command makes."""
        tooling = scratch / PIPX_TOOLING
        environment = isolated_environment(scratch, self._environ, fetching=True)
        for command in (
            [str(base.executable), "-m", "venv", str(tooling)],
            [str(tooling / "bin" / "python"), "-m", "pip", "install", PIPX_PACKAGE],
        ):
            try:
                done = self._launch(command, scratch, environment)
            except OSError as failure:
                raise _pipx_unavailable(failure.strerror or str(failure)) from failure
            if done.returncode != 0:
                raise _pipx_unavailable(
                    _tool_report(done), returncode=done.returncode, stdout=done.stdout,
                    stderr=done.stderr,
                )
        return tooling / "bin" / PIPX_PACKAGE

    def _not_prepared(self, executable: Path, reason: str) -> PreparationFailed:
        return PreparationFailed(
            Diagnostic(
                KIND_INSTALL_STEP_FAILED,
                PREPARATION_OPERATION,
                reason,
                {"step": PIPX_STEP, "cause": CAUSE_PIPX_NOT_PREPARED, "pipx": str(executable)},
            )
        )

    def _ready(self, executable: Path, variables: dict[str, str], environment: dict[str, str]):
        """Check the pipx can be readied, select its pip backend, and prepare its shared pip
        while the network is still allowed. A capability it lacks is a failure, never a different
        command."""
        try:
            described = self._launch(
                [str(executable), "--help"], Path(variables["PIPX_HOME"]), environment
            )
        except OSError as failure:
            raise self._not_prepared(executable, failure.strerror or str(failure)) from failure
        text = described.stdout + described.stderr
        if described.returncode != 0:
            raise self._not_prepared(executable, _tool_report(described))
        if PIPX_UPGRADE_SHARED not in text:
            reason = f"this pipx has no {PIPX_UPGRADE_SHARED} command to ready its shared pip"
            raise self._not_prepared(executable, reason)
        if BACKEND_VARIABLE in text:
            variables[BACKEND_VARIABLE] = "pip"
            environment[BACKEND_VARIABLE] = "pip"
        elif UV_WORD.search(text):
            raise self._not_prepared(executable, "this pipx uses uv and cannot be told to use pip")
        try:
            done = self._launch(
                [str(executable), PIPX_UPGRADE_SHARED], Path(variables["PIPX_HOME"]), environment
            )
        except OSError as failure:
            raise self._not_prepared(executable, failure.strerror or str(failure)) from failure
        if done.returncode != 0:
            raise self._not_prepared(executable, _tool_report(done))

    def pipx(self, scratch: Path, base: BasePython) -> PreparedPipx:
        """The machine's pipx, found before the PATH is narrowed, or one brought into `scratch`;
        with its pip backend selected and its shared pip prepared, so the measured install has no
        setup left to fetch."""
        scratch.mkdir(parents=True, exist_ok=True)
        found = self._find_pipx(self._environ)
        executable = Path(found) if found else self._provision(scratch, base)
        variables = self._pipx_variables(scratch, base)
        environment = {**isolated_environment(scratch, self._environ, fetching=True), **variables}
        self._ready(executable, variables, environment)
        return PreparedPipx(executable, found is None, variables)


def _can_be_copied(facts: PythonFacts) -> bool:
    """A Python new enough to measure on, outside a virtual environment: the only kind a private
    copy can be made from."""
    return facts.version >= MINIMUM_PYTHON and facts.prefix == facts.base_prefix


def _copy_failed(source: PythonFacts, reason: str) -> dict:
    return {"python": source.path, "outcome": CANDIDATE_COPY_FAILED, "reason": reason}


def _pipx_unavailable(reason: str, **completed) -> PreparationFailed:
    return PreparationFailed(
        Diagnostic(
            KIND_INSTALL_STEP_FAILED,
            "bringing pipx",
            reason,
            {"step": PIPX_STEP, "cause": CAUSE_PIPX_UNAVAILABLE, **completed},
        )
    )



class InstallSteps:
    """The build step of the install measure, run with the Python running the release check: build
    one wheel. A step that does not complete raises `InstallStepFailed` with the tool's own
    report."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environ: dict[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self._run = run
        self._environ = dict(os.environ if environ is None else environ)
        self._home = home
        """The home, and the caches under it, every step sees when one is given. pip asks `rustc`
        for its version, and a `rustc` that rustup manages writes its toolchain directory into the
        home it finds."""

    def _build_environment(self) -> dict[str, str]:
        """The caller's environment, without a Python setting that could redirect the build."""
        return {key: value for key, value in self._environ.items() if not key.startswith("PYTHON")}

    def _complete(self, step: str, command: list[str], environ: dict[str, str]) -> str:
        if self._home is not None:
            self._home.mkdir(parents=True, exist_ok=True)
            cache = self._home / ".cache"
            environ = {
                **environ,
                "HOME": str(self._home),
                "XDG_CACHE_HOME": str(cache),
                "PIP_CACHE_DIR": str(cache / "pip"),
            }
        try:
            done = self._run(command, capture_output=True, text=True, check=False, env=environ)
        except OSError as failure:
            raise InstallStepFailed(step, failure.strerror or str(failure)) from failure
        if done.returncode != 0:
            raise InstallStepFailed(step, _tool_report(done))
        return done.stdout

    def build_wheel(self, source: Path, wheel_dir: Path) -> Path:
        """Build one wheel from a copy of `source`, so the build leaves nothing in it. The build
        keeps pip's isolation and may fetch its backend; it builds no dependency."""
        wheel_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="release-check-build-") as scratch:
            copy = Path(scratch) / "source"
            shutil.copytree(source, copy, ignore=_left_out(source))
            self._complete(
                BUILD_STEP,
                [
                    sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-input",
                    "--disable-pip-version-check", "--cache-dir", str(Path(scratch) / "cache"),
                    "--wheel-dir", str(wheel_dir), str(copy),
                ],
                self._build_environment(),
            )
        wheels = sorted(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise InstallStepFailed(BUILD_STEP, f"the build made {len(wheels)} wheels, not one")
        return wheels[0]


def _left_out(source: Path) -> Callable[[str, list[str]], set[str]]:
    """What the build copy leaves behind: the repository's own bookkeeping, earlier build output
    at the top, and caches and egg-info anywhere."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        top = Path(directory) == source
        return {
            name
            for name in names
            if (top and name in COPY_LEFT_OUT)
            or name == "__pycache__"
            or name.endswith(".egg-info")
        }

    return ignore


@dataclass(frozen=True)
class WheelIdentity:
    """What a built wheel says it is: its normalized name, its version, and the digest of every
    file it installs apart from its own metadata."""

    name: str
    version: str
    payload: dict[str, str]


def _wheel_archive(wheel: Path) -> tuple[email.message.Message, dict[str, str]]:
    """The wheel's metadata and the digest of each payload file. A wheel that cannot be read, or
    holds no single metadata file, is a failed build."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            members = [name for name in names if METADATA_MEMBER.fullmatch(name)]
            if len(members) != 1:
                raise InstallStepFailed(BUILD_STEP, f"{wheel.name} holds no metadata to read")
            text = archive.read(members[0]).decode("utf-8")
            payload = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in names
                if not name.endswith("/") and ".dist-info/" not in name and ".data/" not in name
            }
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as failure:
        raise InstallStepFailed(
            BUILD_STEP, getattr(failure, "strerror", None) or f"{wheel.name}: {failure}"
        ) from failure
    return email.parser.Parser().parsestr(text), payload


def requested_packages(wheel: Path) -> list[str]:
    """The packages the wheel's metadata asks to have installed with it, each once as it is named
    there. The target, the packages set aside and requirements for an extra are left out."""
    metadata, _ = _wheel_archive(wheel)
    named: dict[str, str] = {}
    for requirement in metadata.get_all("Requires-Dist") or []:
        found = REQUIREMENT_NAME.match(requirement)
        if found is None or EXTRA_MARKER.search(requirement.partition(";")[2]):
            continue
        named.setdefault(normalize_name(found.group(1)), found.group(1))
    return [
        name
        for key, name in named.items()
        if key != INSTALL_TARGET and key not in BOOTSTRAP_NAMES
    ]


def wheel_identity(wheel: Path) -> WheelIdentity:
    metadata, payload = _wheel_archive(wheel)
    return WheelIdentity(
        normalize_name(metadata.get("Name", "")), metadata.get("Version", ""), payload
    )


def judge_install(requested: list[str], listed: list[str]) -> list[Diagnostic]:
    """Hold the environment's package names to the target alone, apart from the packages set
    aside. Names compare by `normalize_name`; versions are never judged."""
    installed: dict[str, str] = {}
    for name in listed:
        key = normalize_name(name)
        if key != INSTALL_TARGET and key not in BOOTSTRAP_NAMES:
            installed.setdefault(key, name)
    if installed or requested:
        return [_packages_diagnostic(list(installed.values()), requested)]
    return []


def _packages_diagnostic(installed: list[str], requested: list[str]) -> Diagnostic:
    return Diagnostic(
        KIND_INSTALL_PACKAGES,
        INSTALL_OPERATION,
        "the install installs or asks for packages other than ka-telemetry",
        {"installed": installed, "requested": requested},
    )


def _step_failed(failure: InstallStepFailed) -> Diagnostic:
    return Diagnostic(
        KIND_INSTALL_STEP_FAILED, INSTALL_OPERATION, failure.reason, {"step": failure.step}
    )


# The measured install: the README's command, run as written, and what it left behind.

SYSTEM_PATH = ("/usr/bin", "/bin")
LISTING_SCRIPT = (
    "import importlib.metadata as metadata, json, os, sys\n"
    "print(json.dumps({'base_prefix': os.path.realpath(sys.base_prefix),"
    " 'base_executable': getattr(sys, '_base_executable', sys.executable),"
    " 'distributions': [{'name': d.metadata['Name'] or '', 'version': d.version or '',"
    " 'location': str(d.locate_file(''))} for d in metadata.distributions()]}))\n"
)
"""What the target environment's own interpreter runs to list what it holds and where its base
Python is: the standard library's metadata, so no pip is needed in the environment."""
PIPX_ENVIRONMENTS = "venvs"


@dataclass(frozen=True)
class _Listing:
    base_prefix: str
    base_executable: str
    distributions: list[dict[str, str]]


def measured_environment(
    work: Path, inherited: Mapping[str, str], pipx: PreparedPipx, wheel_dir: Path
) -> dict[str, str]:
    """The environment of the README's command: nothing of the caller's Python, pip, pipx, uv or
    proxy settings; every place it writes inside `work`; pipx's own places and Python from the
    preparation; and the one directory it may install from, with no index, no cache, and no way
    to reach the network or to upgrade pipx's shared libraries."""
    environment = isolated_environment(work, inherited)
    environment.update(pipx.environment)
    environment.update(
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(wheel_dir),
        PIP_NO_CACHE_DIR="1",
        PIP_PROXY=UNREACHABLE_PROXY,
        PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE="1",
    )
    return environment


def _path_entry(work: Path, base: BasePython, pipx: PreparedPipx) -> Path:
    """A directory holding `python3`, the validated base Python, and `pipx`, the prepared one: the
    first entry of the command's PATH."""
    entry = work / "path"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "python3").symlink_to(base.executable)
    (entry / PIPX_PACKAGE).symlink_to(pipx.executable)
    return entry


def _target_environment(pipx_home: Path) -> Path | None:
    """The environment pipx made for the target under the scratch pipx home, if any."""
    root = pipx_home / PIPX_ENVIRONMENTS
    if not root.is_dir():
        return None
    named = (e for e in sorted(root.iterdir()) if normalize_name(e.name) == INSTALL_TARGET)
    return next(named, None)


def _command_data(
    command: InstallCommand, done: subprocess.CompletedProcess | None = None
) -> dict:
    data: dict = {"command": command.text}
    if done is not None:
        data.update(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)
    return data


def _install_failure(
    cause: str, reason: str, command: InstallCommand, step: str = INSTALL_STEP,
    done: subprocess.CompletedProcess | None = None, **data,
) -> Diagnostic:
    return Diagnostic(
        KIND_INSTALL_STEP_FAILED,
        INSTALL_OPERATION,
        reason,
        {"step": step, "cause": cause, **_command_data(command, done), **data},
    )


def _list_target(
    environment_dir: Path, run, environment: dict[str, str], work: Path, command: InstallCommand
) -> _Listing:
    """What the target environment's own interpreter says it holds. Raises `InstallMeasureStopped`
    when it cannot be run, prints nothing, or prints something that is not a listing."""

    def stopped(cause: str, reason: str) -> InstallMeasureStopped:
        return InstallMeasureStopped(_install_failure(cause, reason, command, step=LIST_STEP))

    python = environment_dir / "bin" / "python"
    try:
        done = run(
            [str(python), "-I", "-c", LISTING_SCRIPT], capture_output=True, text=True,
            check=False, env=environment, cwd=str(work),
        )
    except OSError as failure:
        raise stopped(CAUSE_LIST_UNREADABLE, failure.strerror or str(failure)) from failure
    if done.returncode != 0:
        raise stopped(CAUSE_LIST_UNREADABLE, _tool_report(done))
    if not done.stdout.strip():
        raise stopped(CAUSE_LIST_EMPTY, "")
    try:
        said = json.loads(done.stdout)
        entries = said["distributions"]
        if not all(
            isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in LISTED_KEYS)
            for entry in entries
        ):
            raise TypeError("a listed distribution lacks a name, version or location")
        return _Listing(str(said["base_prefix"]), str(said["base_executable"]), entries)
    except (ValueError, KeyError, TypeError) as failure:
        raise stopped(CAUSE_LIST_UNREADABLE, done.stdout.strip()) from failure


LISTED_KEYS = ("name", "version", "location")


def _not_this_build(listed: dict[str, str], built: WheelIdentity) -> str | None:
    """What differs between the installed target and the wheel built from the checkout, or None
    when they are the same distribution."""
    if (installed_name := normalize_name(listed["name"])) != built.name:
        return f"the name is {installed_name}, not {built.name}"
    if listed["version"] != built.version:
        return f"the version is {listed['version']}, not {built.version}"
    for name, digest in built.payload.items():
        installed = Path(listed["location"], name)
        try:
            same = hashlib.sha256(installed.read_bytes()).hexdigest() == digest
        except OSError:
            same = False
        if not same:
            return f"the installed file {name} is not the one in the built wheel"
    return None


def _measure_command(
    command: InstallCommand, built: WheelIdentity, wheel_dir: Path, work: Path,
    base: BasePython, pipx: PreparedPipx, run, inherited: Mapping[str, str],
) -> list[Diagnostic]:
    """Run the README's command as written, with only the built wheel to install from, and judge
    the environment pipx made for it."""
    environment = measured_environment(work, inherited, pipx, wheel_dir)
    environment["PATH"] = os.pathsep.join([str(_path_entry(work, base, pipx)), *SYSTEM_PATH])
    home = Path(pipx.environment["PIPX_HOME"])
    if (old := _target_environment(home)) is not None:
        shutil.rmtree(old)
    try:
        done = launch_install_command(
            command, run, capture_output=True, text=True, check=False, env=environment,
            cwd=str(work),
        )
    except OSError as failure:
        return [_install_failure(
            CAUSE_COMMAND_NOT_STARTED, failure.strerror or str(failure), command
        )]
    target = _target_environment(home)
    if target is None:
        return [Diagnostic(
            KIND_INSTALL_TARGET_MISSING, INSTALL_OPERATION,
            "the command installed no ka-telemetry",
            {"step": INSTALL_STEP, "cause": CAUSE_TARGET_ABSENT, **_command_data(command, done)},
        )]
    if done.returncode != 0:
        return [_install_failure(CAUSE_COMMAND_FAILED, _tool_report(done), command, done=done)]
    listing = _list_target(target, run, environment, work, command)
    ours = [d for d in listing.distributions if normalize_name(d["name"]) == INSTALL_TARGET]
    if not ours:
        return [Diagnostic(
            KIND_INSTALL_TARGET_MISSING, INSTALL_OPERATION,
            "the environment does not hold ka-telemetry",
            {"step": INSTALL_STEP, "cause": CAUSE_TARGET_ABSENT, **_command_data(command, done)},
        )]
    if listing.base_prefix != base.identity:
        return [_install_failure(
            CAUSE_PYTHON_MISMATCH, "the environment's base Python is not the validated one",
            command, step=PYTHON_STEP, found=listing.base_executable, expected=str(base.executable),
        )]
    differs = _not_this_build(ours[0], built)
    if differs is not None:
        return [_install_failure(CAUSE_NOT_THIS_BUILD, differs, command)]
    return judge_install([], [d["name"] for d in listing.distributions])


def measure_install(
    checkout: Path,
    *,
    preparation: Preparation,
    run: Callable[..., subprocess.CompletedProcess],
    environ: Mapping[str, str],
    steps: InstallSteps | None = None,
) -> list[Diagnostic]:
    """Run the README's install command as written, on a Python that refuses pip installs, with
    the wheel built from `checkout` the only thing it can install, and judge what pipx made. A
    step that cannot be taken is a diagnostic naming it, so the other measurements still run;
    everything this makes is removed with its scratch directory, whatever the outcome."""
    inherited = dict(environ)
    with tempfile.TemporaryDirectory(prefix="release-check-install-") as name:
        work = Path(name)
        steps = InstallSteps(home=work / "home") if steps is None else steps
        try:
            command = read_install_command(checkout)
            admit_install_command(command)
            try:
                wheel = steps.build_wheel(checkout, work / "wheels")
                requested = requested_packages(wheel)
                built = wheel_identity(wheel)
            except InstallStepFailed as failure:
                return [_step_failed(failure)]
            if requested:
                return [_packages_diagnostic([], requested)]
            base = preparation.base_python(work)
            pipx = preparation.pipx(work, base)
            return _measure_command(
                command, built, wheel.parent, work, base, pipx, run, inherited
            )
        except InstallMeasureStopped as stopped:
            return [stopped.diagnostic]


def real_preparation(environ: Mapping[str, str]) -> Preparation:
    """The preparation bar 5 runs with: the real process seam, Python search, copy and pipx search
    over `environ`."""
    return Preparation(
        run=subprocess.run,
        environ=environ,
        candidates=path_python_candidates,
        copy_interpreter=copy_host_interpreter,
        running=running_base_python(),
        find_pipx=find_pipx_on_path,
    )


@measurement(5)
def the_built_wheel_installs_only_the_target(checkout: Path) -> list[Diagnostic]:
    """Bar 5's composition root: the only place its real collaborators are built."""
    environ = dict(os.environ)
    return measure_install(
        checkout, preparation=real_preparation(environ), run=subprocess.run, environ=environ
    )


# ── metric 7: no command reaches beyond the machine, and the receiver listens on it ──

CAUSE_HELP_INCOMPLETE = "help_incomplete"
CAUSE_NO_LIST = "no_command_list"
CAUSE_NO_RUN = "no_run"
CAUSE_DID_NOT_COMPLETE = "did_not_complete"
CAUSE_NOT_STARTED = "not_started"
CAUSE_TIMED_OUT = "timed_out"
CAUSE_RECORD = "record_unreadable"
CAUSE_NOT_LISTENING = "receiver_not_listening"
CAUSE_REQUEST = "receiver_request_not_accepted"
CAUSE_STOPPED_EARLY = "receiver_stopped_early"
"""Why a command could not be watched to the end: the `cause` of a `network_command_unmeasured`
diagnostic, which the release check words it by."""
HELP_ARGUMENTS = ("--help",)
RECEIVE_COMMAND = "receive"
COMMAND_ENTRY = re.compile(r"^ {4}([A-Za-z0-9][\w-]*)(?: {2,}.*)?$")
LOCAL_ENDPOINT = "http://127.0.0.1:4318\n"
ENDPOINT_FILE, SETTINGS_FILE = "endpoint", "claude-settings.json"
STREAM_DATA_FILES = ("logs", "metrics")


def _network():
    """The watched-process module, loaded from the scratch checkout's `tools` once the import
    path points there, or beside this file when this file is the worker's script."""
    try:
        from tools import release_network
    except ImportError:
        import release_network
    return release_network


def parse_command_listing(help_text: str) -> list[str]:
    """The commands `ka-telemetry --help` lists, in the order it lists them: the entries under its
    `commands:` heading. Text with no such heading, or an empty one, gives none."""
    lines = help_text.splitlines()
    if "commands:" not in lines:
        return []
    names: list[str] = []
    for line in lines[lines.index("commands:") + 1 :]:
        if not line.strip() and names:
            break
        found = COMMAND_ENTRY.match(line)
        if found:
            names.append(found.group(1))
    return names


def exercise_arguments(checkout: Path, scratch: Path) -> dict[str, tuple[str, ...]]:
    """The arguments each finite command is run with, on the scratch reference set or under
    `scratch`. `receive` has none: it is started and sent requests, not run to its end."""
    parity, stream = checkout / PARITY_DATA, checkout / STREAM_DATA
    _require_readable(
        checkout, (f"{PARITY_DATA}/agents.json", f"{PARITY_DATA}/named_sessions.json", PARITY_DATA)
    )
    _require_readable(checkout, (f"{PARITY_DATA}/projects", STREAM_DATA))
    try:
        sessions = json.loads((parity / "named_sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MeasurementIncomplete(
            f"{PARITY_DATA}/named_sessions.json",
            getattr(error, "strerror", None) or str(error),
            kind=KIND_UNREADABLE_INPUT,
        ) from error
    transcripts = sorted((parity / "projects").glob("*/*.jsonl"))
    if not transcripts:
        raise MeasurementIncomplete(
            f"{PARITY_DATA}/projects", "it holds no transcript", kind=KIND_UNREADABLE_INPUT
        )
    named = [argument for session in sessions for argument in ("--session", session)]
    return {
        "runs": (
            "runs", "--agents-json", str(parity / "agents.json"),
            "--projects-root", str(parity / "projects"), *named,
        ),
        "steps": ("steps", "--projects-root", str(parity / "projects")),
        "context": ("context", str(transcripts[0])),
        "agents": ("agents", "--dir", str(stream)),
        "check": ("check", "--dir", str(stream)),
        "turn-on": (
            "turn-on", "--settings-file", str(scratch / SETTINGS_FILE),
            "--endpoint-file", str(scratch / ENDPOINT_FILE),
        ),
    }


def reference_bodies(checkout: Path) -> tuple[bytes, bytes]:
    """One logs body and one metrics body for the receiver, made from the first record the
    reference set keeps of each."""
    bodies = []
    for name in STREAM_DATA_FILES:
        relative = f"{STREAM_DATA}/{name}.jsonl"
        try:
            first = (checkout / relative).read_text(encoding="utf-8").splitlines()[0]
            bodies.append(json.dumps(json.loads(first)["body"]).encode("utf-8"))
        except (OSError, ValueError, IndexError, KeyError, TypeError) as error:
            raise MeasurementIncomplete(
                relative,
                getattr(error, "strerror", None) or "it holds no record to send",
                kind=KIND_UNREADABLE_INPUT,
            ) from error
    return bodies[0], bodies[1]


class SourceWatcher:
    """Runs `ka-telemetry` commands from the scratch checkout's source under the network watch,
    each in a home and a work directory of its own under `scratch`."""

    def __init__(self, checkout: Path, scratch: Path) -> None:
        self._checkout = checkout
        self._scratch = scratch
        self._runs = itertools.count(1)

    def _places(self) -> tuple[Path, Path]:
        base = self._scratch / f"watch-{next(self._runs)}"
        home, workdir = base / "home", base / "work"
        home.mkdir(parents=True)
        workdir.mkdir()
        return home, workdir

    def finite(self, arguments: tuple[str, ...]):
        home, workdir = self._places()
        return _network().run_finite(self._checkout, arguments, home=home, workdir=workdir)

    def receiver(self, logs_body: bytes, metrics_body: bytes):
        home, workdir = self._places()
        return _network().run_receiver(
            self._checkout, home=home, workdir=workdir, logs_body=logs_body,
            metrics_body=metrics_body,
        )


def _unmeasured_command(typed: str, cause: str, reason: str, **data) -> Diagnostic:
    return Diagnostic(KIND_NETWORK_UNMEASURED, typed, reason, {"cause": cause, **data})


def _outcome_diagnostics(typed: str, seen) -> list[Diagnostic]:
    """Why the run did not complete, if it did not: none for a clean exit or a receiver that took
    both requests and was stopped as meant."""
    network, outcome = _network(), seen.outcome
    if outcome.kind in (network.EXITED, network.COMPLETED):
        if outcome.kind == network.COMPLETED or outcome.status == 0:
            return []
        report = seen.stderr.strip() or f"exit code {outcome.status}"
        return [_unmeasured_command(typed, CAUSE_DID_NOT_COMPLETE, report)]
    if outcome.kind == network.NOT_STARTED:
        return [_unmeasured_command(typed, CAUSE_NOT_STARTED, outcome.reason)]
    if outcome.kind == network.TIMED_OUT:
        return [
            _unmeasured_command(typed, CAUSE_TIMED_OUT, outcome.reason, seconds=outcome.seconds)
        ]
    if outcome.kind == network.NEVER_LISTENED:
        return [_unmeasured_command(typed, CAUSE_NOT_LISTENING, outcome.reason)]
    if outcome.kind == network.REQUEST_NOT_ACCEPTED:
        refused = next((request for request in seen.requests if not request.accepted), None)
        reason = refused.reason if refused else outcome.reason
        return [
            _unmeasured_command(typed, CAUSE_REQUEST, reason, path=refused.path if refused else "")
        ]
    return [_unmeasured_command(typed, CAUSE_STOPPED_EARLY, outcome.reason)]


def judge_observation(
    checkout: Path, seen, *, receiver: bool = False, help_command: bool = False
) -> list[Diagnostic]:
    """What one watched run shows: every outbound attempt it recorded, whether it completed, whether
    its record can be trusted and, for the receiver, whether it listened on this machine. The
    verdict reads the record only, never an exit status that a caught refusal leaves clean."""
    network = _network()
    typed = _as_typed(checkout, seen.arguments)
    found = [
        Diagnostic(
            KIND_NETWORK_ATTEMPT,
            typed,
            f"it tried {attempt.operation} on {attempt.destination}"
            + (f" port {attempt.port}" if attempt.port is not None else ""),
            {
                "operation": attempt.operation,
                "destination": attempt.destination,
                "port": attempt.port,
            },
        )
        for attempt in seen.attempts
    ]
    for diagnostic in _outcome_diagnostics(typed, seen):
        if help_command:
            diagnostic = Diagnostic(
                diagnostic.kind, diagnostic.input_or_operation, diagnostic.reason,
                {**diagnostic.data, "cause": CAUSE_HELP_INCOMPLETE},
            )
        found.append(diagnostic)
    if seen.evidence_problem is not None and seen.outcome.kind != network.NOT_STARTED:
        found.append(_unmeasured_command(typed, CAUSE_RECORD, seen.evidence_problem))
    if receiver:
        found.extend(_listening_diagnostics(typed, seen))
    return found


def _listening_diagnostics(typed: str, seen) -> list[Diagnostic]:
    network = _network()
    found = [
        Diagnostic(
            KIND_NETWORK_DEFAULT_ADDRESS,
            typed,
            f"the receiver listened on {listening.host}, which is not on this machine",
            {"address": listening.host, "port": listening.port},
        )
        for listening in seen.listening
        if not network.is_this_machine(listening.host)
    ]
    if seen.outcome.kind == network.COMPLETED and not seen.listening:
        found.append(_unmeasured_command(typed, CAUSE_RECORD, "no listening socket was recorded"))
    return found


def measure_network(checkout: Path, *, watching=SourceWatcher) -> list[Diagnostic]:
    """Run every command `ka-telemetry --help` lists under the watch and hold the record to the
    bar: all completed, no outbound attempt, and a receiver started with no `--host` that listened
    on this machine. `watching` makes the watcher from the checkout and the scratch directory."""
    with tempfile.TemporaryDirectory(prefix="release-check-network-") as scratch:
        plan = exercise_arguments(checkout, Path(scratch))
        logs_body, metrics_body = reference_bodies(checkout)
        (Path(scratch) / ENDPOINT_FILE).write_text(LOCAL_ENDPOINT, encoding="utf-8")
        watcher = watching(checkout, Path(scratch))
        helped = watcher.finite(HELP_ARGUMENTS)
        found = judge_observation(checkout, helped, help_command=True)
        if any(diagnostic.kind == KIND_NETWORK_UNMEASURED for diagnostic in found):
            return found
        listed = parse_command_listing(helped.stdout)
        if not listed:
            typed = _as_typed(checkout, HELP_ARGUMENTS)
            return [
                *found,
                _unmeasured_command(typed, CAUSE_NO_LIST, "it printed no list of commands"),
            ]
        for name in listed:
            if name == RECEIVE_COMMAND:
                found.extend(
                    judge_observation(
                        checkout, watcher.receiver(logs_body, metrics_body), receiver=True
                    )
                )
            elif name in plan:
                found.extend(judge_observation(checkout, watcher.finite(plan[name])))
            else:
                found.append(
                    _unmeasured_command(
                        f"ka-telemetry --help lists {name}",
                        CAUSE_NO_RUN,
                        "the release check has no run of it set up",
                        name=name,
                    )
                )
        return found


@measurement(7)
def no_command_reaches_beyond_the_machine(checkout: Path) -> list[Diagnostic]:
    return measure_network(checkout)


# ── metric 8: every example in the README and the how-to runs as written ──

EXAMPLES_FILE = "tests/e2e/test_documented_examples.py"
PYTEST_MODULE = "pytest"
BUILD_FAILURE_PREFIX = "building the product failed in a scratch virtualenv:"
COLLECTION_FAILURE = "collection failure"
EXAMPLE_LOCATION = re.compile(r"(?:^|[\s/])(README\.md|docs/how-to\.md):\d+")
ERROR_LINE = re.compile(r"^E {1,3}", re.MULTILINE)
REPORT_FILE = "examples.xml"
REPORT_TAIL = 2000
"""How much of a command's own output a diagnostic keeps when the command left no report."""


def examples_command(report_dir: Path, *, module: str) -> list[str]:
    """The command that runs the whole examples file, and only it, writing a report of each
    test's outcome into `report_dir`."""
    return [
        sys.executable,
        "-m",
        module,
        EXAMPLES_FILE,
        "-q",
        "-pno:cacheprovider",
        f"--junit-xml={report_dir / REPORT_FILE}",
    ]


def _pytest_environment() -> dict[str, str]:
    """The caller's environment without a Python or pytest setting that could redirect the run
    at another tree, with bytecode and plugin autoload off."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST_"))
    }
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def _as_a_person_types_it(module: str) -> str:
    return f"python3 -m {module} {EXAMPLES_FILE}"


def _from_the_top(checkout: Path, text: str) -> str:
    """`text` with the scratch checkout's own path taken out, so a path reads from its top."""
    for prefix in {str(checkout), str(checkout.resolve())}:
        text = text.replace(prefix + os.sep, "")
    return text.strip()


@dataclass(frozen=True)
class _FailedTest:
    """One test the report records as failed or errored, and the text pytest gave for it."""

    reason: str
    text: str


def _failed_tests(report: Path) -> tuple[int, list[_FailedTest]]:
    """The number of tests the report records, and those that failed or errored."""
    try:
        suite = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return 0, []
    tests, failed = 0, []
    for case in suite.iter("testcase"):
        tests += 1
        for outcome in case:
            if outcome.tag in ("failure", "error"):
                message = outcome.get("message") or ""
                failed.append(_FailedTest(message, outcome.text or message))
    return tests, failed


def _account(checkout: Path, text: str) -> str:
    """What pytest said about a failure: its error lines, from the top directory."""
    lines = [
        ERROR_LINE.sub("", line, count=1) for line in text.splitlines() if ERROR_LINE.match(line)
    ]
    return _from_the_top(checkout, "\n".join(lines) if lines else text)


def _diagnostic_for(checkout: Path, command: str, failed: _FailedTest) -> Diagnostic:
    account = _account(checkout, failed.text)
    if failed.reason == COLLECTION_FAILURE:
        return Diagnostic(KIND_COMMAND_FAILED, command, account or failed.reason)
    if BUILD_FAILURE_PREFIX in account:
        reason = account.split(BUILD_FAILURE_PREFIX, 1)[1].strip()
        return Diagnostic(
            KIND_BUILD_FAILED, "building ka-telemetry for the examples test", reason
        )
    located = EXAMPLE_LOCATION.search(account)
    if located is not None:
        document = located.group(1)
        return Diagnostic(
            KIND_EXAMPLE_FAILED, document, "an example does not run as written",
            {"document": document, "detail": account},
        )
    return Diagnostic(
        KIND_EXAMPLE_FAILED_NO_DOCUMENT, EXAMPLES_FILE, "the examples test failed",
        {"detail": account},
    )


def measure_examples(checkout: Path, *, module: str) -> list[Diagnostic]:
    """Run the whole examples file in the scratch checkout, with its real build, and hold the
    checkout to that run's verdict. A run that could not start, collect or select a test cannot
    hold: it is a diagnostic here, as a failed example is."""
    try:
        return _run_examples(checkout, module)
    except MeasurementIncomplete as incomplete:
        return [incomplete.diagnostic]


def _run_examples(checkout: Path, module: str) -> list[Diagnostic]:
    command = _as_a_person_types_it(module)
    with tempfile.TemporaryDirectory(prefix="release-check-examples-") as reports:
        try:
            done = subprocess.run(
                examples_command(Path(reports), module=module),
                capture_output=True,
                text=True,
                check=False,
                env=_pytest_environment(),
                cwd=checkout,
            )
        except OSError as failure:
            raise MeasurementIncomplete(
                command, failure.strerror or str(failure), kind=KIND_COMMAND_FAILED
            ) from failure
        tests, failed = _failed_tests(Path(reports) / REPORT_FILE)
    if f"No module named {module}" in done.stderr:
        raise MeasurementIncomplete(
            command,
            done.stderr.strip(),
            kind=KIND_PYTEST_MISSING,
            data={"python": sys.executable},
        )
    if failed:
        found: list[Diagnostic] = []
        for one in failed:
            diagnostic = _diagnostic_for(checkout, command, one)
            if diagnostic not in found:
                found.append(diagnostic)
        return found
    if done.returncode == 0 and tests > 0:
        return []
    said = (done.stderr.strip() or done.stdout.strip())[-REPORT_TAIL:]
    reason = said or (
        "it selected no test" if done.returncode == 0 else f"exit code {done.returncode}"
    )
    raise MeasurementIncomplete(command, _from_the_top(checkout, reason), kind=KIND_COMMAND_FAILED)


@measurement(8)
def documented_examples_run_as_written(checkout: Path) -> list[Diagnostic]:
    """The documented examples' own test is the oracle; there is no second examples parser."""
    return measure_examples(checkout, module=PYTEST_MODULE)


def root_imports_at(checkout: str | Path) -> None:
    """Point the import path at the scratch checkout, ahead of anything the caller exposed: the
    checkout's `tests/` for the verification support and the checkout for `ka_telemetry`."""
    root = Path(checkout)
    for entry in (str(root), str(root / "tests")):
        while entry in sys.path:
            sys.path.remove(entry)
    sys.path[:0] = [str(root / "tests"), str(root)]


def worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_metrics", description=__doc__)
    parser.add_argument("worker", choices=["worker"])
    parser.add_argument("--metric", type=int, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    arguments = parser.parse_args(argv)
    root_imports_at(arguments.checkout)
    with contextlib.redirect_stdout(sys.stderr):
        result = run_worker(arguments.metric, arguments.checkout)
    print(result_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main())
