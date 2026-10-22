"""Watched processes: run a `ka-telemetry` command from source and observe what it tries to reach.

WHY THIS EXISTS
    The release check's bar 7 is "nothing leaves the machine", and a bar like that is decided by
    trying: every command is run, and what its own process attempts is recorded. This module
    owns that trying and nothing else. It returns observations (what was attempted, what listened,
    how the command ended) and never a verdict or the words a person reads; `release_metrics`
    judges and `release_check` words.

    The watch is installed before the product is imported, in the child process that runs it. It
    refuses an attempt as well as recording it, so a broken copy cannot send anything while the
    release check runs. Each record is written to a file of its own, flushed before the refusal
    is raised, because a command may catch the refusal and exit 0, and because a hung or dead
    command must not take its record with it. The record is never read from the command's output.

    It watches Python socket and name-lookup calls made by the command's own process and its
    threads. It is not an operating-system sandbox: a program the command starts is outside it.

    Only the standard library is imported, because the release check imports this from the
    original checkout, where nothing of the product may be imported. This file is also the script
    the watched child runs (`python tools/release_network.py <evidence file> <arguments>`).
"""

from __future__ import annotations

import atexit
import errno
import http.client
import ipaddress
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True

__version__ = "0.1.0"

TARGET_MODULE = "ka_telemetry"
WATCH_SCRIPT = Path(__file__).resolve()

COMMAND_TIMEOUT_SECONDS = 60
"""How long a finite command, or a receiver starting up, is waited for."""
REQUEST_TIMEOUT_SECONDS = 10
STOP_GRACE_SECONDS = 3
POLL_SECONDS = 0.05

EVIDENCE_NAME = "watch-evidence.jsonl"
ENDPOINT_NAME = "endpoint.txt"
RECEIVER_DIRECTORY = "receiver"
STDOUT_NAME = "stdout.txt"
STDERR_NAME = "stderr.txt"
EXIT_WATCH_NOT_READY = 70
LOGS_PATH = "/v1/logs"
METRICS_PATH = "/v1/metrics"
REFUSAL = "refused by the release check"

# How a command ended.
EXITED = "exited"  # a finite command ended on its own, with `status`
NOT_STARTED = "not_started"  # the system could not start it
TIMED_OUT = "timed_out"  # a finite command was still running after `seconds`
COMPLETED = "completed"  # a receiver accepted both requests and was then stopped, as meant
NEVER_LISTENED = "never_listened"  # a receiver did not start listening
REQUEST_NOT_ACCEPTED = "request_not_accepted"  # a receiver did not accept a request
STOPPED_EARLY = "stopped_early"  # a receiver ended before both requests were answered

CONNECTING = frozenset({"connect", "connect_ex"})
SENDING = frozenset({"sendto", "sendmsg"})
LOOKING_UP_A_NAME = frozenset({"getaddrinfo", "gethostbyname"})
LOOKING_UP_AN_ADDRESS = frozenset({"gethostbyaddr", "getnameinfo"})
LISTENING_FAMILIES = {socket.AF_INET: "inet", socket.AF_INET6: "inet6"}
AUDITED = {
    "socket.connect": "connect",
    "socket.sendto": "sendto",
    "socket.sendmsg": "sendmsg",
    "socket.getaddrinfo": "getaddrinfo",
    "socket.gethostbyname": "gethostbyname",
    "socket.gethostbyaddr": "gethostbyaddr",
    "socket.getnameinfo": "getnameinfo",
}
WRAPPED_AT_THE_SOCKET = (
    ("connect", "connect"),
    ("connect_ex", "connect_ex"),
    ("sendto", "sendto"),
    ("sendmsg", "sendmsg"),
)

RECORD_READY = "ready"
RECORD_ATTEMPT = "attempt"
RECORD_LISTENING = "listening"
RECORD_END = "end"


# ── what an observation holds ──


@dataclass(frozen=True)
class Attempt:
    """One try to reach beyond the machine: the socket operation and the place it named, as the
    command named it. `port` is `None` where the operation names none."""

    operation: str
    destination: str
    port: int | None


@dataclass(frozen=True)
class Listening:
    """A socket that listened, as the system reported it afterwards: `family` is `inet` or
    `inet6`, `host` and `port` are the socket's own `getsockname()`."""

    family: str
    host: str
    port: int


@dataclass(frozen=True)
class Outcome:
    """How a command ended. `status` is the exit status for `EXITED`, `seconds` the wait for
    `TIMED_OUT`, and `reason` says why for every other kind."""

    kind: str
    status: int | None = None
    reason: str = ""
    seconds: int | None = None


@dataclass(frozen=True)
class RequestOutcome:
    """The answer to one request sent to a receiver: its path, the status it gave (`None` when
    none came back) and, when it was not accepted, what came back instead."""

    path: str
    status: int | None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass(frozen=True)
class EvidenceRead:
    """What a watch left on its evidence channel. `problem` is set whenever the record cannot be
    trusted to be complete, and then no attempt list in it may be read as an empty one."""

    attempts: tuple[Attempt, ...]
    listening: tuple[Listening, ...]
    problem: str | None


@dataclass(frozen=True)
class Observation:
    """What one watched run gave: the arguments it ran, what it printed, how it ended, every
    attempt and every listening socket recorded, the receiver's request outcomes, and whether the
    record can be trusted (`evidence_problem` is `None` only for a complete one)."""

    arguments: tuple[str, ...]
    stdout: str
    stderr: str
    outcome: Outcome
    attempts: tuple[Attempt, ...] = ()
    listening: tuple[Listening, ...] = ()
    requests: tuple[RequestOutcome, ...] = ()
    evidence_problem: str | None = None
    pid: int | None = None


# ── the policy: which places are this machine ──


def _text(host: object) -> str:
    if isinstance(host, bytes):
        return host.decode("ascii", "replace")
    return host if isinstance(host, str) else str(host)


def _numeric(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` spells, or `None` when it is a name. A name is never resolved."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def is_this_machine(host: object) -> bool:
    """True for the machine's own places only: `127.0.0.0/8`, `::1`, an IPv4-mapped IPv6 address of
    one in `127.0.0.0/8`, and the name `localhost`. Every other address and name is not, the
    wildcards `0.0.0.0` and `::` included."""
    name = _text(host)
    if name.lower() == "localhost":
        return True
    address = _numeric(name)
    if address is None:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _port(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sockaddr_attempt(operation: str, sockaddr: object) -> Attempt | None:
    if not isinstance(sockaddr, tuple) or not sockaddr:
        return None  # a Unix-domain path, or nothing: no network address is named
    host = sockaddr[0]
    if is_this_machine(host):
        return None
    return Attempt(operation, _text(host), _port(sockaddr[1]) if len(sockaddr) > 1 else None)


def classify(operation: str, argument: object, *, family: int = socket.AF_INET) -> Attempt | None:
    """The attempt `operation` makes, or `None` when it stays on this machine or reaches no
    network address. For a connection or a datagram `argument` is the socket address; for a lookup
    it is the tuple of arguments the lookup was called with. A numeric address is not a host name
    to look up, so a forward lookup of one is not an attempt: the connection that follows is."""
    if operation in CONNECTING | SENDING:
        if family == socket.AF_UNIX:
            return None
        return _sockaddr_attempt(operation, argument)
    arguments = argument if isinstance(argument, tuple) else ()
    if not arguments:
        return None
    if operation in LOOKING_UP_A_NAME:
        host = arguments[0]
        if host is None:
            return None
        name = _text(host)
        if _numeric(name) is not None or is_this_machine(name):
            return None
        return Attempt(operation, name, _port(arguments[1]) if len(arguments) > 1 else None)
    if operation == "gethostbyaddr":
        host = arguments[0]
        return None if is_this_machine(host) else Attempt(operation, _text(host), None)
    if operation == "getnameinfo":
        return _sockaddr_attempt(operation, arguments[0])
    return None


# ── the record the watch leaves ──


class Evidence:
    """The watch's evidence channel: one JSON object per line, each written whole and at once.
    A write that fails is raised and remembered, so the record never ends cleanly after it."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._lock = threading.Lock()
        self.failed = False

    def record(self, member: dict) -> None:
        line = (json.dumps(member) + "\n").encode("utf-8")
        with self._lock:
            try:
                while line:
                    line = line[os.write(self._descriptor, line) :]
            except OSError:
                self.failed = True
                raise

    def end(self) -> None:
        if not self.failed:
            with suppress(OSError):
                self.record({"record": RECORD_END})


def _member_problem(member: object) -> str | None:
    if not isinstance(member, dict) or "record" not in member:
        return "a record is not an object with a kind"
    kind = member["record"]
    if kind == RECORD_ATTEMPT:
        if not (
            isinstance(member.get("operation"), str)
            and isinstance(member.get("destination"), str)
            and (member.get("port") is None or _port(member.get("port")) is not None)
        ):
            return "an attempt record is malformed"
    elif kind == RECORD_LISTENING:
        if not (
            isinstance(member.get("family"), str)
            and isinstance(member.get("host"), str)
            and _port(member.get("port")) is not None
        ):
            return "a listening record is malformed"
    elif kind not in (RECORD_READY, RECORD_END):
        return f"a record has the unknown kind {kind!r}"
    return None


def read_evidence(text: str | None, expect_end: bool) -> EvidenceRead:
    """Read the evidence channel. Any missing, unreadable, malformed or unfinished record is a
    `problem`, because the attempts in a record like that prove nothing."""

    def unreadable(reason: str) -> EvidenceRead:
        return EvidenceRead((), (), reason)

    if text is None:
        return unreadable("the watch left no record that could be read")
    if not text.strip():
        return unreadable("the watch left an empty record")
    if not text.endswith("\n"):
        return unreadable("the last record was cut short")
    members = []
    for line in text.splitlines():
        try:
            members.append(json.loads(line))
        except ValueError:
            return unreadable("a record is not JSON")
    for member in members:
        problem = _member_problem(member)
        if problem is not None:
            return unreadable(problem)
    kinds = [member["record"] for member in members]
    if kinds[0] != RECORD_READY or kinds.count(RECORD_READY) != 1:
        return unreadable("the record does not begin with the watch being ready")
    if RECORD_END in kinds and kinds[-1] != RECORD_END:
        return unreadable("the record goes on after its end")
    if expect_end and kinds[-1] != RECORD_END:
        return unreadable("the record has no end")
    attempts = tuple(
        Attempt(member["operation"], member["destination"], member["port"])
        for member in members
        if member["record"] == RECORD_ATTEMPT
    )
    listening = tuple(
        Listening(member["family"], member["host"], member["port"])
        for member in members
        if member["record"] == RECORD_LISTENING
    )
    return EvidenceRead(attempts, listening, None)


# ── the watch, inside the child ──


def _refusal(operation: str) -> OSError:
    if operation in CONNECTING | SENDING:
        return ConnectionRefusedError(errno.ECONNREFUSED, REFUSAL)
    if operation == "gethostbyaddr":
        return socket.herror(1, REFUSAL)
    return socket.gaierror(socket.EAI_FAIL, REFUSAL)


class Watch:
    """Records and refuses every outbound attempt the process makes, and records every socket that
    listens. It is installed once, before the product is imported."""

    def __init__(self, evidence: Evidence) -> None:
        self._evidence = evidence

    def screen(self, operation: str, argument: object, family: int = socket.AF_INET) -> None:
        """Record and refuse `operation` if it reaches beyond this machine."""
        attempt = classify(operation, argument, family=family)
        if attempt is None:
            return
        self._evidence.record(
            {
                "record": RECORD_ATTEMPT,
                "operation": attempt.operation,
                "destination": attempt.destination,
                "port": attempt.port,
            }
        )
        raise _refusal(operation)

    def _audit(self, event: str, arguments: tuple) -> None:
        operation = AUDITED.get(event)
        if operation is None:
            return
        if operation in CONNECTING | SENDING:
            sock, address = arguments[0], arguments[1]
            self.screen(operation, address, sock.family)
        else:
            self.screen(operation, arguments)

    def _wrap_at_the_socket(self, method: str, operation: str) -> None:
        """Screen an address before the socket resolves it: a host name given to a connect or a
        send is looked up inside the call, ahead of the audit event, so the audit alone would let
        the lookup happen."""
        original = getattr(socket.socket, method)
        watch = self

        def screened(sock: socket.socket, *arguments, **keywords):
            address = _address_given(operation, arguments, keywords)
            try:
                watch.screen(operation, address, sock.family)
            except ConnectionRefusedError as refused:
                if method == "connect_ex":
                    return refused.errno
                raise
            return original(sock, *arguments, **keywords)

        screened.__name__ = method
        setattr(socket.socket, method, screened)

    def _wrap_listen(self) -> None:
        original = socket.socket.listen
        evidence = self._evidence

        def listen(sock: socket.socket, *arguments, **keywords):
            result = original(sock, *arguments, **keywords)
            family = LISTENING_FAMILIES.get(sock.family)
            if family is not None:
                name = sock.getsockname()
                evidence.record(
                    {
                        "record": RECORD_LISTENING,
                        "family": family,
                        "host": _text(name[0]),
                        "port": name[1],
                    }
                )
            return result

        socket.socket.listen = listen

    def install(self) -> None:
        sys.addaudithook(self._audit)
        for method, operation in WRAPPED_AT_THE_SOCKET:
            self._wrap_at_the_socket(method, operation)
        self._wrap_listen()


def _address_given(operation: str, arguments: tuple, keywords: dict) -> object:
    """The address a connect or a send was called with, wherever the caller put it."""
    if operation in CONNECTING:
        return arguments[0] if arguments else keywords.get("address")
    if operation == "sendmsg":
        return arguments[3] if len(arguments) > 3 else keywords.get("address")
    return arguments[-1] if len(arguments) in (2, 3) else keywords.get("address")


def watched_main(argv: Sequence[str]) -> int:
    """Run `python3 -m ka_telemetry <arguments>` under the watch. `argv[0]` is the evidence file,
    the rest are the command's arguments."""
    if sys.path and Path(sys.path[0]).resolve() == WATCH_SCRIPT.parent:
        del sys.path[0]
    evidence_path, arguments = argv[0], list(argv[1:])
    try:
        descriptor = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        evidence = Evidence(descriptor)
        Watch(evidence).install()
        evidence.record({"record": RECORD_READY})
    except OSError as failure:
        print(f"the release check's watch could not be made ready: {failure}", file=sys.stderr)
        return EXIT_WATCH_NOT_READY
    atexit.register(evidence.end)
    sys.argv = [TARGET_MODULE, *arguments]
    runpy.run_module(TARGET_MODULE, run_name="__main__", alter_sys=True)
    return 0


# ── the controller: starting a watched command and reaping it ──


def _environment(checkout: Path, home: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    inherited = os.environ if environ is None else environ
    env = {key: value for key, value in inherited.items() if not key.startswith("PYTHON")}
    env.update(HOME=str(home), PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE="1")
    return env


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_evidence_file(path: Path, *, expect_end: bool) -> EvidenceRead:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    return read_evidence(text, expect_end)


def _start(
    command: list[str], env: dict[str, str], home: Path, workdir: Path
) -> subprocess.Popen:
    """Start `command` in a process group of its own, its output going to files in `workdir`."""
    with (
        open(workdir / STDOUT_NAME, "wb") as out,
        open(workdir / STDERR_NAME, "wb") as err,
    ):
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=env,
            cwd=home,
            start_new_session=True,
        )


def _signal_group(process: subprocess.Popen, number: int) -> None:
    with suppress(OSError):
        os.killpg(process.pid, number)


def _stop(process: subprocess.Popen) -> None:
    """End the command and everything it started: ask, then kill, then wait for it and reap it.
    A command that has already ended may have left a process behind, which is killed too."""
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    _signal_group(process, signal.SIGKILL)
    process.wait()


def _failure_text(failure: Exception) -> str:
    return getattr(failure, "strerror", None) or str(failure) or type(failure).__name__


def _unstarted(arguments: tuple[str, ...], failure: OSError) -> Observation:
    return Observation(
        arguments,
        "",
        "",
        Outcome(NOT_STARTED, reason=_failure_text(failure)),
        evidence_problem="the command never ran, so there is no record",
    )


def _watched_command(python: str, workdir: Path, arguments: tuple[str, ...]) -> list[str]:
    return [python, str(WATCH_SCRIPT), str(workdir / EVIDENCE_NAME), *arguments]


def run_finite(
    checkout: Path,
    arguments: Sequence[str],
    *,
    home: Path,
    workdir: Path,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    python: str = sys.executable,
    environ: Mapping[str, str] | None = None,
) -> Observation:
    """Run `python3 -m ka_telemetry <arguments>` from `checkout`'s source under the watch, in
    `home`, and wait up to `timeout` seconds for it to end. `workdir` holds the evidence channel
    and the captured output. Whatever happens, nothing the command started is left running."""
    arguments = tuple(arguments)
    command = _watched_command(python, workdir, arguments)
    try:
        process = _start(command, _environment(checkout, home, environ), home, workdir)
    except OSError as failure:
        return _unstarted(arguments, failure)
    try:
        process.wait(timeout)
        outcome = Outcome(EXITED, status=process.returncode)
    except subprocess.TimeoutExpired:
        outcome = Outcome(
            TIMED_OUT,
            seconds=int(timeout),
            reason=f"it had not finished {int(timeout)} seconds after it started",
        )
    finally:
        _stop(process)
    evidence = _read_evidence_file(
        workdir / EVIDENCE_NAME, expect_end=outcome.kind == EXITED and outcome.status >= 0
    )
    return Observation(
        arguments,
        _read(workdir / STDOUT_NAME),
        _read(workdir / STDERR_NAME),
        outcome,
        evidence.attempts,
        evidence.listening,
        evidence_problem=evidence.problem,
        pid=process.pid,
    )


def _await_port(process: subprocess.Popen, endpoint: Path, timeout: float) -> int | None:
    """The port the receiver published once it is listening, or `None` if it ended or did not
    publish one within `timeout` seconds. The endpoint file says only that it is ready."""
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        published = _read(endpoint).strip()
        if published:
            _, _, port = published.rpartition(":")
            return int(port) if port.isdigit() else None
        time.sleep(POLL_SECONDS)
    return None


def _post(port: int, path: str, body: bytes) -> RequestOutcome:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        status = response.status
        reason = "" if 200 <= status < 300 else f"status {status} {response.reason}".strip()
        return RequestOutcome(path, status, reason)
    except (OSError, http.client.HTTPException) as failure:
        return RequestOutcome(path, None, _failure_text(failure))
    finally:
        connection.close()


def _stopped_text(process: subprocess.Popen, stderr: str) -> str:
    return stderr.strip() or f"it ended with exit status {process.returncode}"


def run_receiver(
    checkout: Path,
    *,
    home: Path,
    workdir: Path,
    logs_body: bytes,
    metrics_body: bytes,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    python: str = sys.executable,
    environ: Mapping[str, str] | None = None,
) -> Observation:
    """Run `ka-telemetry receive --port 0` with no `--host` under the watch, send it one logs
    request and one metrics request from this machine, then stop it. The published endpoint says
    the receiver is ready and which port it chose; where it listens is read from the socket."""
    endpoint = workdir / ENDPOINT_NAME
    arguments = (
        "receive",
        "--port",
        "0",
        "--dir",
        str(workdir / RECEIVER_DIRECTORY),
        "--publish-endpoint",
        str(endpoint),
    )
    command = _watched_command(python, workdir, arguments)
    try:
        process = _start(command, _environment(checkout, home, environ), home, workdir)
    except OSError as failure:
        return _unstarted(arguments, failure)
    requests: tuple[RequestOutcome, ...] = ()
    try:
        port = _await_port(process, endpoint, timeout)
        if port is not None:
            requests = (
                _post(port, LOGS_PATH, logs_body),
                _post(port, METRICS_PATH, metrics_body),
            )
        ended_before_stop = process.poll() is not None
    finally:
        _stop(process)
    stderr = _read(workdir / STDERR_NAME)
    if port is None:
        waited = f"it was not listening {int(timeout)} seconds after it started"
        said = _stopped_text(process, stderr) if ended_before_stop else stderr.strip() or waited
        outcome = Outcome(NEVER_LISTENED, reason=said)
    elif ended_before_stop:
        outcome = Outcome(STOPPED_EARLY, reason=_stopped_text(process, stderr))
    elif not all(request.accepted for request in requests):
        refused = next(request for request in requests if not request.accepted)
        outcome = Outcome(REQUEST_NOT_ACCEPTED, reason=refused.reason)
    else:
        outcome = Outcome(COMPLETED)
    evidence = _read_evidence_file(workdir / EVIDENCE_NAME, expect_end=False)
    return Observation(
        arguments,
        _read(workdir / STDOUT_NAME),
        stderr,
        outcome,
        evidence.attempts,
        evidence.listening,
        requests,
        evidence.problem,
        process.pid,
    )


if __name__ == "__main__":
    sys.exit(watched_main(sys.argv[1:]))
