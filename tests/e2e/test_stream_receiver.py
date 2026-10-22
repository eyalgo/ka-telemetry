"""End-to-end tests of the package's receiver against a real server on loopback.

Two things are proved here that a unit test cannot reach. The first is that the address Claude
Code attaches to its telemetry never reaches a disk. The second is that deleting `logs.jsonl` or
`metrics.jsonl` while the receiver runs never loses the record being written at that moment.

The second needs a deletion that falls inside a write, and a race that hopes for that proves
nothing. So the tests use a **file-I/O barrier**: `pathlib.Path.open` is wrapped so a chosen step
of the receiver's real append (just after the open, just after the flush, or after the file is
closed) waits until the test says go. Neither `Store.append` nor the handler is replaced; the code
under test runs as shipped, and the barrier only holds one of its own file operations still.

Both scenarios are also run against the receiver as it stood in the commit this one was copied
from (`91d580f`), loaded from `git show 91d580f:otel_receiver.py` into a scratch directory. It
keeps the address and loses the record being written, and the tests assert exactly that, so each
test that passes for the package is one that fails for the copy.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import pathlib
import queue
import subprocess
import threading
from pathlib import Path

import pytest
from e2e.support import needs_history

from ka_telemetry import receiver

PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
COPY_COMMIT = "91d580f"
ADDRESS = "owner@example.com"

_REAL_OPEN = pathlib.Path.open


# ── The barrier ─────────────────────────────────────────────────────────────


class IoBarrier:
    """Holds one step of an append still until the test releases it.

    `arm(stage, filename, times)` makes the next `times` appends to `filename` stop at `stage`:
    `opened` (the file is open, nothing is written), `flushed` (the line is written and flushed,
    the receiver has not yet checked the file is still the one at its path) or `closed` (the
    append is complete and the file is closed). `wait_for_arrival()` returns when an append is
    stopped there, and `release()` lets it go on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage: str | None = None
        self._filename: str | None = None
        self._remaining = 0
        self._arrived: queue.Queue[bool] = queue.Queue()
        self._released = threading.Semaphore(0)

    def arm(self, stage: str, filename: str, times: int = 1) -> None:
        with self._lock:
            self._stage, self._filename, self._remaining = stage, filename, times

    def wait_for_arrival(self) -> None:
        assert self._arrived.get(timeout=15), "no append reached the barrier"

    def release(self) -> None:
        self._released.release()

    def reach(self, stage: str, filename: str) -> None:
        with self._lock:
            hold = self._stage == stage and self._filename == filename and self._remaining > 0
            if hold:
                self._remaining -= 1
        if hold:
            self._arrived.put(True)
            assert self._released.acquire(timeout=30), "the barrier was never released"


class _GatedHandle:
    def __init__(self, handle, barrier: IoBarrier, filename: str) -> None:
        self._handle = handle
        self._barrier = barrier
        self._filename = filename

    def write(self, data):
        return self._handle.write(data)

    def flush(self):
        self._handle.flush()
        self._barrier.reach("flushed", self._filename)

    def fileno(self):
        return self._handle.fileno()

    def __enter__(self):
        self._barrier.reach("opened", self._filename)
        return self

    def __exit__(self, *exc):
        self._handle.close()
        self._barrier.reach("closed", self._filename)


@pytest.fixture
def barrier(monkeypatch) -> IoBarrier:
    gate = IoBarrier()

    def gated_open(self, mode="r", *args, **kwargs):
        handle = _REAL_OPEN(self, mode, *args, **kwargs)
        if mode.startswith("a") and self.name in {"logs.jsonl", "metrics.jsonl"}:
            return _GatedHandle(handle, gate, self.name)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", gated_open)
    return gate


# ── The receivers: the package's, and the copy commit's ─────────────────────


def _copy_commit_receiver(scratch: Path):
    """The receiver as it stood at the copy commit, loaded from a file in a scratch directory.
    Command:
    `git -C <product> show 91d580f:otel_receiver.py > <scratch>/copy_commit_receiver.py`."""
    needs_history()
    shown = subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), "show", f"{COPY_COMMIT}:otel_receiver.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr
    source = scratch / "copy_commit_receiver.py"
    source.write_text(shown.stdout)
    spec = importlib.util.spec_from_file_location("copy_commit_receiver", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["package", "copy_commit"])
def receiver_module(request, tmp_path_factory):
    if request.param == "package":
        return receiver
    return _copy_commit_receiver(tmp_path_factory.mktemp("copy_commit"))


class _Running:
    def __init__(self, module, directory: Path) -> None:
        self.directory = directory
        self.server = module.build_receiver(0, str(directory))
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def post(self, signal: str, body) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", f"/v1/{signal}", body=json.dumps(body).encode())
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def numbers(self, signal: str) -> list[int]:
        path = self.directory / f"{signal}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["body"]["n"] for line in path.read_text().splitlines()]


@pytest.fixture
def running(receiver_module, tmp_path):
    """The receiver under test: the package's, then the copy commit's."""
    started = _Running(receiver_module, tmp_path / "stream")
    yield started
    started.stop()


@pytest.fixture
def running_package(tmp_path):
    """The package's receiver alone, for the contract only the package has."""
    started = _Running(receiver, tmp_path / "stream")
    yield started
    started.stop()


def _is_package(module) -> bool:
    return module is receiver


# ── The address never reaches a disk ────────────────────────────────────────


def _three_records() -> list[tuple[str, dict]]:
    address = {"key": "user.email", "value": {"stringValue": ADDRESS}}
    other = {"key": "session.id", "value": {"stringValue": "s-1"}}
    return [
        (
            "logs",
            {"resourceLogs": [{"resource": {"attributes": [other]}, "scopeLogs": [
                {"logRecords": [{"attributes": [address, other]}]}]}]},
        ),
        (
            "logs",
            {"resourceLogs": [{"resource": {"attributes": [other, address]}, "scopeLogs": [
                {"logRecords": [{"attributes": [other]}]}]}]},
        ),
        (
            "metrics",
            {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{"name": "claude_code.cost.usage",
                "sum": {"dataPoints": [{"asDouble": 0.5, "attributes": [other, address]}]}}]}]}]},
        ),
    ]


def test_email_removed_over_three_real_loopback_posts(running, receiver_module):
    for signal, body in _three_records():
        assert running.post(signal, body) == 200

    written = {
        signal: (running.directory / f"{signal}.jsonl").read_text()
        for signal in ("logs", "metrics")
    }
    if _is_package(receiver_module):
        assert ADDRESS not in written["logs"] + written["metrics"]
        assert "user.email" not in written["logs"] + written["metrics"]
        bodies = [json.loads(line)["body"] for line in written["logs"].splitlines()]
        bodies.append(json.loads(written["metrics"])["body"])
        for stored, (_, posted) in zip(bodies, _three_records()):
            assert stored == _without_email(posted)
    else:
        # The copy commit stores the address as received: the failing run CP-215 asks for.
        assert ADDRESS in written["logs"]
        assert ADDRESS in written["metrics"]


def _without_email(value):
    if isinstance(value, dict):
        return {
            key: (
                [e for e in item if not (isinstance(e, dict) and e.get("key") == "user.email")]
                if key == "attributes" and isinstance(item, list)
                else _without_email(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_email(item) for item in value]
    return value


# ── A deletion that falls inside a write loses nothing ──────────────────────


def _seed(running, signal: str) -> None:
    for number in (1, 2, 3):
        assert running.post(signal, {"n": number}) == 200


def _post_in_thread(running, signal: str, number: int) -> tuple[threading.Thread, list[int]]:
    outcome: list[int] = []
    thread = threading.Thread(target=lambda: outcome.append(running.post(signal, {"n": number})))
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_deletion_during_active_append_recovers_pending_line_once(
    running, barrier, receiver_module, signal
):
    _seed(running, signal)
    barrier.arm("opened", f"{signal}.jsonl")

    pending, outcome = _post_in_thread(running, signal, 4)
    barrier.wait_for_arrival()
    (running.directory / f"{signal}.jsonl").unlink()
    barrier.release()
    pending.join(timeout=30)
    assert outcome == [200]
    assert running.post(signal, {"n": 5}) == 200
    assert running.post(signal, {"n": 6}) == 200

    if _is_package(receiver_module):
        # The deleted records are gone; the pending one and every later one appear once each.
        assert running.numbers(signal) == [4, 5, 6]
    else:
        # The copy commit writes into the file that was just deleted: record 4 is lost.
        assert running.numbers(signal) == [5, 6]


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_deletion_between_flush_and_the_identity_check_recovers_only_the_pending_line(
    running_package, barrier, signal
):
    running = running_package
    _seed(running, signal)
    barrier.arm("flushed", f"{signal}.jsonl")

    pending, outcome = _post_in_thread(running, signal, 4)
    barrier.wait_for_arrival()
    (running.directory / f"{signal}.jsonl").unlink()
    barrier.release()
    pending.join(timeout=30)

    assert outcome == [200]
    assert running.post(signal, {"n": 5}) == 200
    # Records 1-3 completed in the deleted file and are not replayed.
    assert running.numbers(signal) == [4, 5]


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_repeated_deletions_before_the_pending_append_completes(
    running_package, barrier, signal
):
    running = running_package
    _seed(running, signal)
    barrier.arm("opened", f"{signal}.jsonl", times=3)

    pending, outcome = _post_in_thread(running, signal, 4)
    for _ in range(3):
        barrier.wait_for_arrival()
        (running.directory / f"{signal}.jsonl").unlink()
        barrier.release()
    pending.join(timeout=30)

    assert outcome == [200]
    assert running.numbers(signal) == [4]


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_several_clients_while_a_deletion_falls_inside_a_write(running_package, barrier, signal):
    running = running_package
    _seed(running, signal)
    barrier.arm("opened", f"{signal}.jsonl")

    pending, first = _post_in_thread(running, signal, 4)
    barrier.wait_for_arrival()
    others = [_post_in_thread(running, signal, number) for number in (5, 6, 7)]
    (running.directory / f"{signal}.jsonl").unlink()
    barrier.release()
    for thread, _ in [(pending, first), *others]:
        thread.join(timeout=30)

    assert first == [200] and all(outcome == [200] for _, outcome in others)
    numbers = running.numbers(signal)
    assert sorted(numbers) == [4, 5, 6, 7]
    assert numbers[0] == 4
    lines = (running.directory / f"{signal}.jsonl").read_text().splitlines()
    assert all(json.loads(line)["signal"] == signal for line in lines)


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_completed_record_stays_deleted_when_response_arrives_late(
    running_package, barrier, signal
):
    running = running_package
    _seed(running, signal)
    barrier.arm("closed", f"{signal}.jsonl")

    completed, outcome = _post_in_thread(running, signal, 4)
    barrier.wait_for_arrival()
    # The append is complete and its file closed; the 200 has not been sent yet.
    assert running.numbers(signal) == [1, 2, 3, 4]
    (running.directory / f"{signal}.jsonl").unlink()
    barrier.release()
    completed.join(timeout=30)

    assert outcome == [200]
    # A deletion after completion removes that record too, and nothing puts it back.
    assert running.numbers(signal) == []
    assert running.post(signal, {"n": 5}) == 200
    assert running.numbers(signal) == [5]
