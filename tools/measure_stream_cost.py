#!/usr/bin/env python3
"""Measure what it costs `ka-telemetry agents` and `ka-telemetry check` to read stream files.

Run as `python3 tools/measure_stream_cost.py` from the top directory of a copy of this repository.

WHY THIS EXISTS
    Both reports read the whole of `logs.jsonl` and `metrics.jsonl` before they print anything, and
    the files only grow. The how-to used to say a report "takes a minute or more" on gigabytes
    without a measurement behind it. This script makes the measurement: it generates stream data at
    three sizes, runs each report on each size as a child process of its own, and prints the peak
    memory and the time of every run.

    It stands outside the package on purpose. The reports are measured as a user runs them, through
    the module entry point, and nothing is added to the reader to be able to watch it.

    Three parts live in this one file and meet only through the arguments `run_measurements` is
    given: the pair generator, the measurement adapter (one report as one child process) and the
    orchestration that composes them. Everything the orchestration touches on the machine arrives as
    a parameter, so a test runs it at kilobytes with stand-ins, and the real ones are built in one
    place, `default_arguments`.
"""

from __future__ import annotations

import errno
import functools
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

__version__ = "0.1.0"

DEFAULT_SIZES_BYTES = (100_000_000, 300_000_000, 1_000_000_000)

LOGS_FILE_NAME = "logs.jsonl"
METRICS_FILE_NAME = "metrics.jsonl"

BYTES_PER_MEGABYTE = 1_000_000
BYTES_PER_GIGABYTE = 1_000_000_000
REPORTS = ("agents", "check")
INTERRUPTED_STATUS = 130
_BYTES_PER_KIBIBYTE = 1024
_SIGNAL_EXIT_OFFSET = 128
_TERMINATION_GRACE_SECONDS = 2.0
_REAP_POLL_SECONDS = 0.01
_CHILD_START_FAILED_STATUS = 127

_WRITE_BATCH_BYTES = 1 << 20
_RECEIVED_AT = "2026-01-01T00:00:00+00:00"
_COST_METRIC_NAME = "claude_code.cost.usage"

# Fixed synthetic labels. A few sessions, models and agents keep the number of rows a report shows
# steady while the number of records grows with the requested size.
_SESSIONS = (
    ("00000000-0000-4000-8000-000000000001", "claude-synthetic-large", "repl_main_thread", None),
    ("00000000-0000-4000-8000-000000000002", "claude-synthetic-small", "sdk", None),
    ("00000000-0000-4000-8000-000000000003", "claude-synthetic-large", "agent", "reviewer"),
    ("00000000-0000-4000-8000-000000000004", "claude-synthetic-small", "agent", "builder"),
)
_REQUEST_COST_USD = 0.0125


# ── The pair generator ───────────────────────────────────────────────────────


class TemporaryStorage:
    """Where the generated data lives: one new directory under `parent`, and the files in it.

    Every call that touches the disk for the data goes through here, so a test can make any one of
    them fail without patching the script.
    """

    def __init__(self, *, parent: Path) -> None:
        self.parent = parent

    def make_root(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="measure-stream-cost-", dir=self.parent))

    def make_directory(self, path: Path) -> None:
        path.mkdir(parents=True)

    def open_for_write(self, path: Path) -> BinaryIO:
        return path.open("wb")

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)


def _attribute(key: str, value_kind: str, value: object) -> dict[str, object]:
    return {"key": key, "value": {value_kind: value}}


def _serialise(export: dict[str, object]) -> bytes:
    return (json.dumps(export, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _logs_line(session: str, model: str, query_source: str, agent: str | None) -> bytes:
    attributes = [
        _attribute("session.id", "stringValue", session),
        _attribute("event.name", "stringValue", "api_request"),
        _attribute("model", "stringValue", model),
        _attribute("query_source", "stringValue", query_source),
        _attribute("input_tokens", "intValue", 120),
        _attribute("output_tokens", "intValue", 480),
        _attribute("cache_read_tokens", "intValue", 9000),
        _attribute("cache_creation_tokens", "intValue", 1500),
        _attribute("cost_usd", "doubleValue", _REQUEST_COST_USD),
        _attribute("duration_ms", "intValue", 1500),
    ]
    if agent is not None:
        attributes.append(_attribute("agent.name", "stringValue", agent))
    record = {"attributes": attributes}
    scope = {"logRecords": [record]}
    body = {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [scope]}]}
    return _serialise({"signal": "logs", "received_at": _RECEIVED_AT, "body": body})


def _metrics_line(session: str) -> bytes:
    point = {
        "attributes": [_attribute("session.id", "stringValue", session)],
        "asDouble": _REQUEST_COST_USD,
    }
    metric = {"name": _COST_METRIC_NAME, "sum": {"dataPoints": [point, dict(point)]}}
    body = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [metric]}]}]}
    return _serialise({"signal": "metrics", "received_at": _RECEIVED_AT, "body": body})


def _write_whole_lines(handle: BinaryIO, lines: Sequence[bytes], minimum_bytes: int) -> None:
    """Write the lines in rotation, whole, until at least `minimum_bytes` (and one line) are out.

    The bulk goes in batches of about a megabyte, so memory holds one batch and not the file.
    """
    cycle = b"".join(lines)
    batch = cycle * max(1, _WRITE_BATCH_BYTES // len(cycle))
    written = 0
    while minimum_bytes - written >= len(batch):
        handle.write(batch)
        written += len(batch)
    index = 0
    while written < minimum_bytes or written == 0:
        line = lines[index % len(lines)]
        handle.write(line)
        written += len(line)
        index += 1


def generate_pair(directory: Path, requested_bytes: int, *, storage: TemporaryStorage) -> None:
    """Write `logs.jsonl` and `metrics.jsonl` into `directory`, together at least `requested_bytes`.

    Each file gets at least half the request, rounded up to a whole line, and never less than one
    line. The same request always writes the same bytes. Both files are closed on return, and a
    failure to open, write or close either one is raised as the `OSError` the system gave.
    """
    half = -(-requested_bytes // 2)
    logs_lines = tuple(_logs_line(*session) for session in _SESSIONS)
    metrics_lines = tuple(_metrics_line(session[0]) for session in _SESSIONS)
    for file_name, lines in ((LOGS_FILE_NAME, logs_lines), (METRICS_FILE_NAME, metrics_lines)):
        with storage.open_for_write(directory / file_name) as handle:
            _write_whole_lines(handle, lines, half)


# ── The measurement adapter ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ChildExit:
    """What the system says of an ended child: its raw wait status and its resource usage."""

    wait_status: int
    max_resident_raw: int  # `ru_maxrss`, in the platform's own unit


@dataclass(frozen=True)
class ReportRun:
    """One report run, measured. `status` is 0, the exit status, or 128 plus the ending signal."""

    status: int
    peak_resident_bytes: int
    elapsed_seconds: float
    signal_number: int | None = None


def peak_bytes_from_maxrss(raw: int, *, platform: str) -> int:
    """`ru_maxrss` in bytes. macOS counts bytes and Linux counts kibibytes, as their getrusage
    manuals each state; a test cannot assert either from the other, so only this conversion is
    tested and the platform's own contract is relied on for the rest."""
    return raw if platform == "darwin" else raw * _BYTES_PER_KIBIBYTE


def megabytes_of(byte_count: int) -> float:
    """Decimal megabytes: 1 MB is 1,000,000 bytes, as the stream sizes are counted."""
    return byte_count / BYTES_PER_MEGABYTE


class ChildProcesses:
    """The real process port: start a child, wait for that exact child, end one that must go.

    The child is made with `fork` and `execv`, and the wait is one `wait4` on its pid, which returns
    that child's own resource usage. `posix_spawn` would do for the start, but it has no
    working-directory action on Python 3.11, the floor, and `-m ka_telemetry` finds the package
    through the working directory. Nothing else waits on the child, so no second wait competes
    for it. The Linux figure starts from the resident size of the process that forked, so the
    script keeps its own memory small: the generator writes in batches and holds no dataset.
    """

    def start(self, argv: Sequence[str], *, cwd: Path, stdout_path: Path) -> int:
        descriptor = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            pid = os.fork()
            if pid == 0:  # the child: never returns into the caller's code
                try:
                    os.dup2(descriptor, 1)
                    os.chdir(cwd)
                    os.execv(argv[0], list(argv))
                finally:
                    os._exit(_CHILD_START_FAILED_STATUS)
            return pid
        finally:
            os.close(descriptor)

    def wait(self, pid: int) -> ChildExit:
        _pid, wait_status, usage = os.wait4(pid, 0)
        return ChildExit(wait_status, usage.ru_maxrss)

    def terminate_and_reap(self, pid: int) -> None:
        """End the child and collect it, so it is gone before its files are."""
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
            while time.monotonic() < deadline:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
                time.sleep(_REAP_POLL_SECONDS)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            return  # already ended and collected


def measure_report(
    report: str,
    directory: Path,
    *,
    checkout_root: Path,
    output_path: Path,
    processes: ChildProcesses,
    clock: Callable[[], float],
) -> ReportRun:
    """Run `report` on the pair in `directory` as a child of its own and measure that child.

    The child is the interpreter running this script, started by absolute path with no shell and no
    PATH lookup, so the `ka_telemetry` beside this script is the one measured and never one
    installed elsewhere. Its standard output goes to `output_path`; its standard error is the
    script's own. Time runs from just before the launch to the exit being collected. If the wait is
    interrupted the child is ended and collected before the interruption continues.
    """
    argv = [
        str(Path(sys.executable).absolute()),
        "-E", "-S", "-B", "-m", "ka_telemetry",
        report,
        "--dir", str(directory.absolute()),
    ]
    started = clock()
    pid = processes.start(argv, cwd=checkout_root, stdout_path=output_path)
    try:
        ended = processes.wait(pid)
    except BaseException:
        processes.terminate_and_reap(pid)
        raise
    elapsed = clock() - started
    exit_code = os.waitstatus_to_exitcode(ended.wait_status)
    signal_number = -exit_code if exit_code < 0 else None
    return ReportRun(
        status=_SIGNAL_EXIT_OFFSET + signal_number if signal_number is not None else exit_code,
        peak_resident_bytes=peak_bytes_from_maxrss(ended.max_resident_raw, platform=sys.platform),
        elapsed_seconds=elapsed,
        signal_number=signal_number,
    )


# ── The orchestration and the entry point ────────────────────────────────────

_FALLBACK_TEMPORARY_PARENTS = (Path("/var/tmp"), Path("/tmp"))

_RESULT_LINE = (
    "{command} on {size} of stream data: "
    "peak memory {memory:.1f} MB, time {seconds:.1f} s"
)
_EXIT_FAILURE = (
    "{command} on {size} of stream data exited with status {status}; "
    "no figure was printed for it."
)
_SIGNAL_FAILURE = (
    "{command} on {size} of stream data was ended by signal {signal}; "
    "no figure was printed for it."
)
_REPORT_FAILURE_NEXT_STEP = (
    "If {command} wrote an error above this line, resolve it, then run "
    "python3 tools/measure_stream_cost.py again."
)
_WRITE_FAILURE = "Could not write the stream data: {reason}"
_WRITE_FAILURE_NEXT_STEP = (
    "Check that {directory} can be written to and has more than 1 GB free, then run "
    "python3 tools/measure_stream_cost.py again."
)


def size_label(size_bytes: int) -> str:
    """The stated size as a whole number: MB below 1 GB, GB from 1 GB up."""
    if size_bytes >= BYTES_PER_GIGABYTE:
        return f"{size_bytes // BYTES_PER_GIGABYTE} GB"
    return f"{size_bytes // BYTES_PER_MEGABYTE} MB"


def format_result_line(report: str, size_bytes: int, run: ReportRun) -> str:
    return _RESULT_LINE.format(
        command=report,
        size=size_label(size_bytes),
        memory=megabytes_of(run.peak_resident_bytes),
        seconds=run.elapsed_seconds,
    )


def choose_temporary_parent(checkout_root: Path, *, candidates: Sequence[Path]) -> Path:
    """The first candidate directory that exists and lies outside the checkout, resolved.

    An inherited temporary-directory setting can point inside the checkout, where the data would
    show in `git status`; that candidate is passed over. With none left, the system's own
    "no such file or directory" is raised.
    """
    checkout = checkout_root.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        inside_checkout = resolved == checkout or checkout in resolved.parents
        if resolved.is_dir() and not inside_checkout:
            return resolved
    raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))


class _UnusableStorage(TemporaryStorage):
    """Storage for a machine with no temporary directory outside the checkout: it fails at once,
    with the system's own reason, so the run reports it as every other write failure."""

    def __init__(self, *, parent: Path, error: OSError) -> None:
        super().__init__(parent=parent)
        self._error = error

    def make_root(self) -> Path:
        raise self._error


def _report_write_failure(error: OSError, storage: TemporaryStorage, stderr: TextIO) -> int:
    reason = error.strerror or str(error)
    stderr.write(_WRITE_FAILURE.format(reason=reason) + "\n")
    stderr.write(_WRITE_FAILURE_NEXT_STEP.format(directory=storage.parent) + "\n")
    return 1


def _report_report_failure(report: str, size_bytes: int, run: ReportRun, stderr: TextIO) -> int:
    size = size_label(size_bytes)
    if run.signal_number is None:
        stderr.write(_EXIT_FAILURE.format(command=report, size=size, status=run.status) + "\n")
    else:
        try:
            signal_name = signal.Signals(run.signal_number).name
        except ValueError:
            signal_name = str(run.signal_number)
        stderr.write(_SIGNAL_FAILURE.format(command=report, size=size, signal=signal_name) + "\n")
    stderr.write(_REPORT_FAILURE_NEXT_STEP.format(command=report) + "\n")
    return 1


def run_measurements(
    *,
    sizes: Sequence[int],
    generate: Callable[[Path, int], None],
    measure: Callable[..., ReportRun],
    storage: TemporaryStorage,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Generate each size, run `agents` then `check` on it one at a time, print one line per run.

    Sizes run smallest first as given. The first failed report or unwritable file stops the run
    after the lines already printed, and the temporary root is removed however the run ends,
    including by an interrupt, which is left to the entry point to turn into a status.
    """
    root: Path | None = None
    try:
        root = storage.make_root()
        for size_bytes in sizes:
            size_directory = root / str(size_bytes)
            storage.make_directory(size_directory)
            generate(size_directory, size_bytes)
            for report in REPORTS:
                run = measure(report, size_directory, output_path=size_directory / f"{report}.out")
                if run.status != 0:
                    return _report_report_failure(report, size_bytes, run, stderr)
                stdout.write(format_result_line(report, size_bytes, run) + "\n")
                stdout.flush()
            storage.remove_tree(size_directory)
        return 0
    except OSError as error:
        return _report_write_failure(error, storage, stderr)
    finally:
        if root is not None:
            storage.remove_tree(root)


def default_arguments(*, checkout_root: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """The real collaborators and the fixed sizes: the one place they are built."""
    inherited = [Path(environ["TMPDIR"])] if environ.get("TMPDIR") else []
    candidates = [*inherited, *_FALLBACK_TEMPORARY_PARENTS]
    try:
        storage = TemporaryStorage(
            parent=choose_temporary_parent(checkout_root, candidates=candidates)
        )
    except OSError as error:
        storage = _UnusableStorage(parent=candidates[0], error=error)
    return {
        "sizes": DEFAULT_SIZES_BYTES,
        "generate": functools.partial(generate_pair, storage=storage),
        "measure": functools.partial(
            measure_report,
            checkout_root=checkout_root,
            processes=ChildProcesses(),
            clock=time.perf_counter,
        ),
        "storage": storage,
        "stdout": sys.stdout,
        "stderr": sys.stderr,
    }


def status_of(run: Callable[[], int]) -> int:
    """The exit status of `run`: its own, or 130 when it is interrupted with Ctrl-C."""
    try:
        return run()
    except KeyboardInterrupt:
        return INTERRUPTED_STATUS


def main() -> int:
    """Measure both reports at the three default sizes. It takes no argument and adds no option."""
    checkout_root = Path(__file__).resolve().parent.parent
    arguments = default_arguments(checkout_root=checkout_root, environ=os.environ)
    return status_of(lambda: run_measurements(**arguments))


if __name__ == "__main__":
    sys.exit(main())
