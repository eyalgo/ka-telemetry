"""receiver — an OTLP/http-json receiver that writes the telemetry Claude Code sends to files on
this machine, one flat append-only file per signal.

WHY THIS EXISTS
    A subagent's spend is recorded nowhere else on disk: the transcript readers read a transcript,
    and an agent a Workflow spawns leaves none. Claude Code can send that spend itself, per API
    request, as OpenTelemetry: the metrics stream (`claude_code.token.usage`,
    `claude_code.cost.usage`) and the logs stream (`api_request`, `subagent_completed`) both carry
    it. The transport is OTLP over HTTP in JSON, because that keeps the receiver on the standard
    library: the payload arrives as plain JSON POSTed to `/v1/metrics` and `/v1/logs`, with no
    protobuf and no gRPC.

    **The port and the clock are handed in, and the directory has one shared default.** Nothing
    here names a port. A test binds an ephemeral one against a temporary directory while a real
    receiver is up on its own, so the two never fight over a socket or a file.

    **The layout is one flat, append-only file per signal:** `<dir>/metrics.jsonl` and
    `<dir>/logs.jsonl`. Nothing is split per session or per day, and this module never deletes,
    truncates or renames what it wrote. Removing records is the user's act: deleting a whole file.

    **What is stored is what was received, minus the address.** Every attribute keyed `user.email`
    is removed before the first byte of a record reaches a disk, wherever an `attributes` array
    holds one. Nothing else in the body is touched.

    **Deleting a file while the receiver runs is safe.** `Store.append` holds a lock across the
    write, the flush and a check that the file it wrote to is still the file at its path. A
    record whose file was deleted under it is written again, whole, to the file now at the path.
    Only that one pending line is kept in memory; records already completed are never replayed.
    "Completed" is that check passing. A record that completed and was then deleted stays
    deleted, even when its HTTP response is still on the way: the response follows the storage
    and cannot undo a deletion that came between them.
"""

from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
import threading
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ka_telemetry import stream_paths


class Signal(str, Enum):
    """The two OTLP paths this receiver serves — closed to exactly these two, so a record's
    own tag is never a bare string standing in for a set nothing else names."""

    METRICS = "metrics"
    LOGS = "logs"


PATH_SIGNALS: dict[str, Signal] = {
    "/v1/metrics": Signal.METRICS,
    "/v1/logs": Signal.LOGS,
}

# The one line `main` prints once the receiver is listening. The words are `change_text.md`'s.
START_LINE = (
    "The receiver is listening on {host}, port {port}, and appends the records it accepts to "
    "logs.jsonl and metrics.jsonl in {directory} until you stop it."
)

# ── What this receiver refuses to be told by a client ──────────────────────────────────
#
# Port, directory and clock are handed in because they are DEPLOYMENT facts. These two are
# not: they are the receiver's own posture toward anything that can reach its socket, and a
# posture with a permissive default is a posture nobody chose.
#
# LOOPBACK is the bind host. An empty host string is INADDR_ANY, which put this receiver on
# every interface — measured with `lsof -iTCP:<port>`, reporting `*:<port>` on a machine whose
# firewall `socketfilterfw --getglobalstate` reports disabled. `publish_endpoint()` below
# already writes `http://localhost:…`, so the module was asserting loopback in one place while
# binding the world in another.
LOOPBACK = "127.0.0.1"

# MAX_BODY_BYTES caps what a client may DECLARE, refused before a byte of it is read. The
# largest payload this receiver has actually been sent is 56,536 bytes, measured over a live
# `logs.jsonl`; this is ~148x that, so nothing a Claude run emits comes near it.
MAX_BODY_BYTES = 8 * 1024 * 1024

# The one attribute this receiver removes before it stores a record.
_EMAIL_ATTRIBUTE_KEY = "user.email"


# ── Removing the address ───────────────────────────────────────────────────────────────


def _is_email_attribute(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("key") == _EMAIL_ATTRIBUTE_KEY


def _without_email_attributes(value: Any) -> Any:
    """`value` with every entry keyed `user.email` removed from every array named `attributes`,
    at any depth. Everything else — other attributes and their order, every other field and
    value, a JSON scalar or list standing where an object was expected — is returned as it is.
    The caller's own value is never modified."""
    if isinstance(value, dict):
        cleaned = {}
        for key, member in value.items():
            if key == "attributes" and isinstance(member, list):
                member = [entry for entry in member if not _is_email_attribute(entry)]
            cleaned[key] = _without_email_attributes(member)
        return cleaned
    if isinstance(value, list):
        return [_without_email_attributes(item) for item in value]
    return value


# ── The ports — every world-reach this receiver makes arrives through one of these two.
# Neither is constructed anywhere but `build_receiver()`, below.


class Clock:
    """The one capability this receiver takes from the outside world to stamp a record: the
    current time. Nothing else — this is not a scheduler and it does not sleep."""

    def now(self) -> str:
        """An ISO-8601 UTC timestamp — string-typed, because that is what a JSON line carries."""
        return datetime.now(timezone.utc).isoformat()


class Store:
    """The one world-reach this receiver makes to keep what it received: appending one record
    for a named `Signal`. Nothing else — no read-back, no query, no removal.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        # The server below serves requests on threads, so two exporters can be mid-append at
        # once. POSIX lets `write(2)` transfer fewer bytes than asked, and nothing documents
        # one Python `write()` of a ~56 KB record as one syscall — so without this, two
        # records can interleave into lines that parse as neither run's. An ordinary run
        # rarely shows it (twelve concurrent unguarded 40 KB appends, three runs, no
        # corruption); the lock is for the case the contract permits, not one that has been
        # seen. It is internal state, not a collaborator: nothing outside this class can
        # observe it or wants to hand one in. It is also held across the check that follows a
        # write, so a second append cannot slip in between a write and its check.
        self._lock = threading.Lock()

    def append(self, signal: Signal, received_at: str, body: Any) -> None:
        """Append one record: the envelope and the body without its `user.email` attributes, then
        one newline. Returns when the line is in the file now at this signal's path, rewriting
        it once for each time that file was deleted before the line was complete."""
        line = (
            json.dumps(
                {
                    "signal": signal.value,
                    "received_at": received_at,
                    "body": _without_email_attributes(body),
                }
            )
            + "\n"
        )
        path = self._directory / f"{signal.value}.jsonl"
        with self._lock:
            while True:
                self._directory.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    if _is_file_at(handle.fileno(), path):
                        return


def _is_file_at(descriptor: int, path: Path) -> bool:
    """Whether the open file `descriptor` is the file now at `path`. Deleting a file removes its
    path and not the open descriptor, so a write that lands after a deletion succeeds into a file
    nobody can reach. Device and inode say it is the same file; a missing path says it is not."""
    opened = os.fstat(descriptor)
    try:
        current = path.stat()
    except FileNotFoundError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


class TelemetryServer(ThreadingHTTPServer):
    """The one server this receiver runs — **threading**, and that is load-bearing rather
    than incidental. On the single-threaded `HTTPServer` this started as, one client that
    promised a body it never sent held the only handler and the collector stopped answering
    anybody; the handler `timeout` below bounds that wait, but only a thread per request
    stops it being everyone's wait. `store` and `clock` travel on it because
    `BaseHTTPRequestHandler` is instantiated per request by the server itself, with no
    constructor slot of its own to hand a collaborator through."""

    # Where `build_receiver` writes records, as the person should read it: absolute, with `~`
    # expanded and symbolic links left as given. Wiring information for `main`'s start line, not a
    # capability of a store, so a server built directly with any append-capable store leaves it
    # unset.
    storage_directory: Path | None = None

    def __init__(self, server_address, handler_class, store: Store, clock: Clock) -> None:
        super().__init__(server_address, handler_class)
        self.store = store
        self.clock = clock

    def server_bind(self) -> None:
        """Bind as `HTTPServer` does, without its `socket.getfqdn` of the host: that is a name
        lookup, which on a machine whose resolver is slow held the start for tens of seconds
        before the receiver listened. Nothing here reads `server_name`; it is the host as given."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def publish_endpoint(path: str, port: int) -> None:
    """Write the endpoint a client should POST to, built from the port the receiver actually
    bound — never a port this module names itself. Called only when `--publish-endpoint` is
    given."""
    Path(path).expanduser().write_text(f"http://localhost:{port}\n", encoding="utf-8")


def build_receiver(
    port: int, directory: str | None = None, host: str = LOOPBACK
) -> TelemetryServer:
    """Wire the real collaborators — the only place `Store`, `Clock` or `TelemetryServer` is
    constructed. `directory` is expanded here, never as an argparse default and never inside
    `main()`'s own body, because resolving `~` is a reach into the environment like any other.
    With none given, it is the shared default every command uses.

    `host` defaults to loopback and is the one argument here that carries a default of its own,
    because it is who may reach this socket at all, and the permissive answer has to be typed
    rather than inherited.
    """
    chosen = directory if directory is not None else stream_paths.DEFAULT_STREAM_DIRECTORY
    expanded = Path(chosen).expanduser()
    server = TelemetryServer((host, port), TelemetryHandler, Store(expanded), Clock())
    # `absolute()` and not `resolve()`: the person sees the path they gave, not a link's target.
    server.storage_directory = expanded.absolute()
    return server


class TelemetryHandler(BaseHTTPRequestHandler):
    """Serves exactly the two OTLP paths this receiver understands; every other path and
    every non-POST method is refused. `store` and `clock` come from `self.server` — the
    `TelemetryServer` that constructs one of these per request — never from a module default.
    """

    # A client that opens a connection and then dribbles — or promises a body it never
    # sends — must not hold this receiver. A handler blocked in `rfile.read()` would otherwise
    # be the whole collector stopped. `StreamRequestHandler` applies this to the connection, so
    # it covers the request line and the headers too.
    timeout = 10

    def do_POST(self) -> None:
        signal = PATH_SIGNALS.get(self.path)
        if signal is None:
            self._respond(404, b'{"error": "not found"}')
            return
        length = self._declared_length()
        if length is None:
            self._respond(400, b'{"error": "invalid Content-Length"}')
            return
        if length > MAX_BODY_BYTES:
            # Refused on the claim, before the read — the point is not to accept the bytes
            # and then discard them.
            self._respond(413, b'{"error": "payload too large"}')
            return
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except OSError:
            # `socket.timeout` is an `OSError`. A client that promised bytes it never sent
            # has already cost this receiver `timeout` seconds; it does not also get to cost
            # it a traceback and an unanswered socket.
            self.close_connection = True
            return
        if not raw:
            self._respond(400, b'{"error": "empty body"}')
            return
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            self._respond(400, b'{"error": "invalid JSON"}')
            return
        try:
            self.server.store.append(signal, self.server.clock.now(), body)
        except Exception:
            # Best-effort by contract: nothing a store does can take the receiver down, so any
            # failure here becomes a response rather than a propagated exception.
            self._respond(500, b'{"error": "could not store record"}')
            return
        self._respond(200, b"{}")

    def do_GET(self) -> None:
        self._respond(405, b'{"error": "method not allowed"}')

    def _declared_length(self) -> int | None:
        """The body size a client declared: `None` when the header is present and is not a
        number, `0` when it is absent or non-positive.

        Bare `int()` here raised out of `do_POST` and closed the connection with no response
        at all — measured against `Content-Length: abc`. The receiver survived it; the client
        learned nothing.
        """
        declared = self.headers.get("Content-Length")
        if declared is None:
            return 0
        try:
            length = int(declared)
        except (TypeError, ValueError):
            return None
        return length if length > 0 else 0

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass


class _CommandParser(argparse.ArgumentParser):
    """The parser a `ka-telemetry` command builds: an option may not be abbreviated, and a
    usage error ends with the next step to take, on its own last line. The stream modules import
    nothing from the transcript readers, so each builds this itself."""

    def __init__(self, *, next_step: str, **kwargs) -> None:
        super().__init__(allow_abbrev=False, **kwargs)
        self._next_step = next_step

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{self._next_step}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _CommandParser(
        prog="ka-telemetry receive",
        next_step="Run `ka-telemetry receive --help` to see what it accepts.",
        description=(
            "Receives the telemetry Claude Code sends as OpenTelemetry over HTTP in JSON, on "
            "/v1/logs and /v1/metrics, and appends each record as one line to logs.jsonl or "
            "metrics.jsonl in its directory (see --dir). Before writing, it removes every "
            "user.email attribute from what it received, and it changes nothing else in it. Once "
            "it is listening, it prints one line naming the address and the port it listens on "
            "and the directory it writes logs.jsonl and metrics.jsonl in, and prints nothing more "
            "while it serves. It serves until you stop it. Deleting logs.jsonl or metrics.jsonl "
            "removes the records that file holds; doing so never requires stopping the receiver, "
            "and never loses or corrupts a record being written at the time."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        metavar="PORT",
        help="the TCP port to listen on (required); 0 lets the system choose a free port, and "
        "--publish-endpoint records which one",
    )
    parser.add_argument(
        "--dir",
        metavar="DIR",
        help="the directory to write logs.jsonl and metrics.jsonl in, created if it does not "
        "exist (default: ~/.ka-telemetry)",
    )
    parser.add_argument(
        "--host",
        default=LOOPBACK,
        metavar="HOST",
        help="the address to listen on (default: 127.0.0.1, reachable only from this machine). "
        "The receiver asks for no authentication and writes any JSON it is sent to /v1/logs or "
        "/v1/metrics, so with another address anyone who can reach it can add records to these "
        "files.",
    )
    parser.add_argument(
        "--publish-endpoint",
        metavar="PATH",
        help="write http://localhost:PORT to the file PATH, with PORT the port the receiver is "
        "actually listening on; without this flag, no file is written",
    )
    args = parser.parse_args(argv)

    server = build_receiver(args.port, args.dir, args.host)
    if args.publish_endpoint:
        publish_endpoint(args.publish_endpoint, server.server_port)
    try:
        print(
            START_LINE.format(
                host=args.host, port=server.server_port, directory=server.storage_directory
            ),
            flush=True,
        )
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
