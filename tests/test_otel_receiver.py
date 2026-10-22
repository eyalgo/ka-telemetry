"""Tests for `ka_telemetry.receiver` — the OTLP/http-json receiver that turns a Claude run's
telemetry into bytes on disk.

`core/doctrine/engineering/languages/python/conventions.md` § Constructs: "A module is given
its collaborators; it constructs none." These tests hold that against the receiver module itself
— by effect, via `ast`/`inspect`, never by re-deriving what the ports do from their own bodies.
The live receiver holds one port and one directory, so a suite that binds either fights the
daemon or proves nothing; every end-to-end test below binds an ephemeral port (`0`) against a
`tmp_path`, never the real `4318` / `~/.kensaiapp/telemetry`.

The tests were copied from kensaiapp's receiver and are pointed at the package's. Their identity
and assertions stay, except where the change that moved the receiver into the package names a
clause it alters: the directory is now optional (it has a default under the home directory), and
the script the plist starts is now the package's receiver module.
"""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ka_telemetry import receiver, stream_paths, turn_on

PRODUCT_ROOT = Path(__file__).resolve().parent.parent


def _child_env() -> dict[str, str]:
    """The environment a test hands a receiver it starts as a separate process by file path: the
    product root on its import path, so `from ka_telemetry import ...` resolves in that child.
    Only the test's child gets it; the receiver module adds nothing to `sys.path` itself."""
    return {**os.environ, "PYTHONPATH": str(PRODUCT_ROOT)}


# ── The seam every end-to-end test below drives requests against ──────────


@pytest.fixture
def running_receiver(tmp_path):
    """A real `TelemetryServer` bound to an ephemeral port (`0`), serving out of a temporary
    directory, in a background thread — torn down after the test whatever it did. Yields
    `(server, tmp_path)`; the port to connect on is `server.server_port`."""
    server = receiver.build_receiver(0, str(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _free_port() -> int:
    """A port nothing is listening on — for the one test that must hand a port to a *separate
    process* on a command line, where `0` cannot be read back off a bound socket."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


# ── Task 1 — the two ports exist, and one function builds them ────────


class TestPortsAndTheCompositionRoot:
    def test_binding_looks_up_no_name(self, tmp_path, monkeypatch):
        # A slow resolver once held the start for tens of seconds: binding asks it nothing.
        def no_lookup(*args):
            raise AssertionError("the receiver looked up a name while binding")

        monkeypatch.setattr(socket, "getfqdn", no_lookup)
        server = receiver.build_receiver(0, str(tmp_path))
        try:
            assert server.server_port != 0
            assert server.server_name == "127.0.0.1"
        finally:
            server.server_close()

    def test_store_exposes_exactly_one_capability(self):
        # "appending one record for a named signal. Nothing else." — no read-back, no query.
        assert {name for name in vars(receiver.Store) if not name.startswith("_")} == {
            "append"
        }

    def test_clock_exposes_exactly_one_capability(self):
        # "the current time, as the value written onto a record." — nothing else.
        assert {name for name in vars(receiver.Clock) if not name.startswith("_")} == {
            "now"
        }

    def test_clock_now_returns_a_string_timestamp(self):
        assert isinstance(receiver.Clock().now(), str)

    def test_only_build_receiver_constructs_a_store_clock_or_server(self):
        # By effect, not by name: walk every function body in the module and record which one
        # names `Store(`, `Clock(` or `TelemetryServer(` as a call — `build_receiver` should be
        # the only hit.
        source = Path(receiver.__file__).read_text()
        tree = ast.parse(source)
        constructing_functions = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                        if call.func.id in ("Store", "Clock", "TelemetryServer"):
                            constructing_functions.add(node.name)
        assert constructing_functions == {"build_receiver"}

    def test_main_obtains_its_collaborators_from_build_receiver_and_constructs_none_itself(self):
        source = inspect.getsource(receiver.main)
        assert "build_receiver(" in source
        assert "Store(" not in source
        assert "Clock(" not in source
        assert "TelemetryServer(" not in source

    def test_build_receiver_is_the_last_top_level_function_ahead_of_main(self):
        # "sits at the bottom of the module" — the composition root is the last thing defined
        # before the CLI entry point.
        source = Path(receiver.__file__).read_text()
        tree = ast.parse(source)
        top_level_funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert top_level_funcs[-2:] == ["build_receiver", "main"]

    def test_build_receiver_resolves_a_home_relative_directory(self, monkeypatch, tmp_path):
        # Expanding `~` is an environment reach, and Task 1 puts it inside `build_receiver()`
        # — never as an argparse default, never in a unit's own body.
        monkeypatch.setenv("HOME", str(tmp_path))
        server = receiver.build_receiver(0, "~/nested/telemetry")
        try:
            assert server.store._directory == tmp_path / "nested" / "telemetry"
        finally:
            server.server_close()

    def test_dir_argument_carries_no_default_and_is_required(self):
        # "never as an argparse default" — the CLI declares `--dir` once with nothing to fall
        # back to. It is no longer required: with none given, `build_receiver` resolves the
        # shared default, and that is the one place the home directory is read.
        source = Path(receiver.__file__).read_text()
        match = ast.parse(source)
        add_argument_calls = [
            node
            for node in ast.walk(match)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ]
        dir_calls = [
            call
            for call in add_argument_calls
            if call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "--dir"
        ]
        assert len(dir_calls) == 1
        keywords = {kw.arg: kw.value for kw in dir_calls[0].keywords}
        assert "default" not in keywords
        assert "required" not in keywords
        help_text = ast.literal_eval(keywords["help"])
        assert "directory" in help_text

    def test_main_does_not_expand_the_directory_itself(self):
        # The reach lives in `build_receiver()`; `main()` only hands the raw string through.
        source = inspect.getsource(receiver.main)
        assert "expanduser" not in source


# ── Task 2 — the two OTLP paths are served, and nothing else is ────────


class TestPathsServed:
    def test_post_metrics_returns_2xx_and_appends_one_record_tagged_metrics(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        status, _ = _post(server.server_port, "/v1/metrics", b'{"a": 1}')
        assert 200 <= status < 300
        lines = (tmp_path / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 1
        # "tagged `metrics`" is half the stated behaviour — the file it lands in is the other
        # half, and a record that lands in the right file under the wrong tag still fails it.
        assert json.loads(lines[0])["signal"] == "metrics"

    def test_post_logs_returns_2xx_and_appends_one_record_tagged_logs(self, running_receiver):
        server, tmp_path = running_receiver
        status, _ = _post(server.server_port, "/v1/logs", b'{"a": 1}')
        assert 200 <= status < 300
        lines = (tmp_path / "logs.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["signal"] == "logs"

    def test_get_on_metrics_is_refused_and_the_store_is_untouched(self, running_receiver):
        server, tmp_path = running_receiver
        status = _get(server.server_port, "/v1/metrics")
        assert status >= 400
        assert not (tmp_path / "metrics.jsonl").exists()

    def test_get_on_logs_is_refused_and_the_store_is_untouched(self, running_receiver):
        server, tmp_path = running_receiver
        status = _get(server.server_port, "/v1/logs")
        assert status >= 400
        assert not (tmp_path / "logs.jsonl").exists()

    def test_post_to_an_unknown_path_returns_404_and_the_store_is_untouched(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        status, _ = _post(server.server_port, "/v1/traces", b'{"a": 1}')
        assert status == 404
        assert list(tmp_path.iterdir()) == []


# ── Task 3 — a stored record keeps the body verbatim ────────


class TestRecordShape:
    def test_written_line_is_json_carrying_signal_received_at_and_the_body(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        status, _ = _post(
            server.server_port, "/v1/metrics", json.dumps({"cost_usd": 0.4}).encode()
        )
        assert status // 100 == 2
        record = json.loads((tmp_path / "metrics.jsonl").read_text().splitlines()[0])
        assert record["signal"] == "metrics"
        assert record["received_at"]  # a non-empty timestamp from Clock
        assert record["body"]["cost_usd"] == 0.4

    def test_received_at_comes_from_the_injected_clock_not_the_wall_clock(self, tmp_path):
        class FixedClock:
            def now(self) -> str:
                return "2026-09-09T00:00:00+00:00"

        store = receiver.Store(tmp_path)
        store.append(receiver.Signal.METRICS, FixedClock().now(), {"x": 1})
        record = json.loads((tmp_path / "metrics.jsonl").read_text().splitlines()[0])
        assert record["received_at"] == "2026-09-09T00:00:00+00:00"

    def test_two_posts_append_two_lines_neither_rewriting_the_other(self, running_receiver):
        server, tmp_path = running_receiver
        _post(server.server_port, "/v1/metrics", b'{"n": 1}')
        _post(server.server_port, "/v1/metrics", b'{"n": 2}')
        lines = (tmp_path / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["body"]["n"] == 1
        assert json.loads(lines[1])["body"]["n"] == 2

    def test_a_body_containing_a_newline_does_not_split_into_two_records(self, running_receiver):
        server, tmp_path = running_receiver
        body = json.dumps({"note": "line one\nline two"}).encode()
        _post(server.server_port, "/v1/metrics", body)
        lines = (tmp_path / "metrics.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["body"]["note"] == "line one\nline two"


# ── Task 4 — a bad request cannot take the receiver down ────────


class TestResilience:
    def test_malformed_json_body_returns_4xx_appends_nothing_and_serves_the_next_request(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        status, _ = _post(server.server_port, "/v1/metrics", b"{not json")
        assert 400 <= status < 500
        assert not (tmp_path / "metrics.jsonl").exists()
        status2, _ = _post(server.server_port, "/v1/metrics", b'{"ok": true}')
        assert status2 // 100 == 2

    def test_empty_body_returns_4xx_appends_nothing_and_serves_the_next_request(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        status, _ = _post(server.server_port, "/v1/metrics", b"")
        assert 400 <= status < 500
        assert not (tmp_path / "metrics.jsonl").exists()
        status2, _ = _post(server.server_port, "/v1/metrics", b'{"ok": true}')
        assert status2 // 100 == 2

    def test_a_store_that_raises_on_append_does_not_take_the_receiver_down(self):
        class RaisingStore:
            def append(self, signal, received_at, body):
                raise RuntimeError("boom")

        server = receiver.TelemetryServer(
            ("127.0.0.1", 0), receiver.TelemetryHandler, RaisingStore(), receiver.Clock()
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status1, _ = _post(server.server_port, "/v1/metrics", b'{"ok": true}')
            assert status1 >= 500
            # The receiver is still up: a second request against the same (still-raising)
            # store is served rather than the connection failing outright.
            status2, _ = _post(server.server_port, "/v1/logs", b'{"ok": true}')
            assert status2 >= 500
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_a_header_the_receiver_cannot_parse_does_not_take_it_down(self, running_receiver):
        # Task 4's intent is the whole sentence — "the receiver outlives *anything* a client
        # sends it" — not only the three bodies its bullets name. A Content-Length that is not
        # a number is the nearest thing a client can send that the bullets do not cover.
        server, tmp_path = running_receiver
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            raw.sendall(
                b"POST /v1/metrics HTTP/1.0\r\nContent-Length: banana\r\n\r\n{}"
            )
            raw.recv(256)
        finally:
            raw.close()
        status, _ = _post(server.server_port, "/v1/metrics", b'{"ok": true}')
        assert status // 100 == 2


# ── Found while walking — what a client may not do to this receiver ────────
#
# A review on 2026-09-10 read this module and reported it network-exposed with an unbounded
# body. Two of its four sub-claims were already handled — a missing and a negative
# `Content-Length` both reached the empty-body 400. Two were real, and the second was worse
# than reported: `HTTPServer` serves one request at a time, so a handler blocked in
# `rfile.read()` is not one slow client, it is the collector down for everybody.
#
# `<CORE>/doctrine/governance/found_while_walking.md` clause 1 — small, fixed on the branch
# that found it.


class TestWhatAClientMayNotDo:
    def test_the_receiver_binds_loopback_and_not_every_interface(self, running_receiver):
        # The defect, stated as the bar that would have caught it: an empty host string is
        # INADDR_ANY, and `server_address[0]` reported `0.0.0.0` before this.
        server, _ = running_receiver
        assert server.server_address[0] == receiver.LOOPBACK
        assert server.server_address[0] != "0.0.0.0"

    def test_a_wider_bind_is_available_but_has_to_be_asked_for(self, tmp_path):
        # Loopback is a default, not a prohibition — an operator who means it can say so.
        server = receiver.build_receiver(0, str(tmp_path), "0.0.0.0")
        try:
            assert server.server_address[0] == "0.0.0.0"
        finally:
            server.server_close()

    def test_host_carries_a_loopback_default_and_is_not_required(self):
        # The same AST idiom `test_dir_argument_carries_no_default_and_is_required` uses.
        # `--host` is the one argument here that SHOULD carry a default, and the default has
        # to be the loopback constant rather than a second spelling of it.
        tree = ast.parse(Path(receiver.__file__).read_text())
        host_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--host"
        ]
        assert len(host_calls) == 1
        keywords = {kw.arg: kw.value for kw in host_calls[0].keywords}
        assert "required" not in keywords
        assert isinstance(keywords["default"], ast.Name)
        assert keywords["default"].id == "LOOPBACK"

    def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        oversize = receiver.MAX_BODY_BYTES + 1
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            # The body is never sent. If the receiver reads before it checks, this hangs and
            # the recv below times out instead of returning a status line.
            raw.sendall(
                b"POST /v1/logs HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % oversize
            )
            first_line = raw.recv(256).split(b"\r\n")[0]
        finally:
            raw.close()
        assert b"413" in first_line, first_line
        assert not list(tmp_path.iterdir())

    def test_one_client_promising_a_body_it_never_sends_does_not_stop_the_collector(
        self, running_receiver
    ):
        # The sharp one. The attacker holds the socket open, so the handler cannot be
        # unblocked by a disconnect — which is what an earlier probe accidentally did,
        # masking the stall.
        server, _ = running_receiver
        held = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            held.sendall(
                b"POST /v1/logs HTTP/1.0\r\nContent-Length: 4096\r\n\r\n{"
            )
            # A second, entirely ordinary client. Before the fix this got no response at all.
            status, _body = _post(server.server_port, "/v1/logs", b'{"ok": true}')
            assert status // 100 == 2
        finally:
            held.close()

    def test_a_content_length_that_is_not_a_number_is_answered_rather_than_dropped(
        self, running_receiver
    ):
        # `test_a_header_the_receiver_cannot_parse_does_not_take_it_down` already pins that
        # the receiver survives this. It did — by letting `int()` raise out of `do_POST`,
        # closing the connection with no response. Surviving and answering are different
        # promises.
        server, tmp_path = running_receiver
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            raw.sendall(b"POST /v1/logs HTTP/1.0\r\nContent-Length: banana\r\n\r\n{}")
            first_line = raw.recv(256).split(b"\r\n")[0]
        finally:
            raw.close()
        assert b"400" in first_line, first_line
        assert not list(tmp_path.iterdir())

    def test_a_missing_or_negative_length_still_reaches_the_empty_body_refusal(self):
        # The two sub-claims the review got wrong, pinned so a later change cannot quietly
        # make them true.
        assert receiver.TelemetryHandler._declared_length is not None
        for declared, expected in ((None, 0), ("-1", 0), ("0", 0), ("12", 12)):
            headers = {} if declared is None else {"Content-Length": declared}
            assert _declared_length_of(headers) == expected

    def test_one_record_is_one_uninterrupted_append_even_when_the_os_writes_in_pieces(
        self, tmp_path, monkeypatch
    ):
        # What serving on threads costs, paid. POSIX lets `write(2)` transfer fewer bytes than
        # asked, and nothing documents one Python `write()` of a ~56 KB line as one syscall.
        # An ordinary run rarely shows the interleave — twelve concurrent unguarded 40 KB
        # appends, three runs, no corruption, measured 2026-09-10 — so a test that hopes for
        # it passes with the lock deleted, which the first version of this one did. This one
        # forces the case the contract permits: every write lands in 1 KB pieces with a yield
        # between them.
        real_open = Path.open

        class _Piecewise:
            def __init__(self, handle):
                self._handle = handle

            def write(self, data):
                for start in range(0, len(data), 1024):
                    self._handle.write(data[start : start + 1024])
                    self._handle.flush()
                    time.sleep(0.0005)
                return len(data)

            # The Store now validates the descriptor after a flush, so the adapter passes those
            # two through. The write interleave it forces is unchanged.
            def flush(self):
                self._handle.flush()

            def fileno(self):
                return self._handle.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        monkeypatch.setattr(
            Path, "open", lambda self, *args, **kwargs: _Piecewise(real_open(self, *args, **kwargs))
        )
        store = receiver.Store(tmp_path)
        bodies = [{"who": index, "pad": "x" * 8_000} for index in range(8)]
        threads = [
            threading.Thread(target=store.append, args=(receiver.Signal.LOGS, "t", body))
            for body in bodies
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        monkeypatch.undo()

        lines = (tmp_path / "logs.jsonl").read_text().splitlines()
        assert len(lines) == len(bodies)
        recovered = sorted(json.loads(line)["body"]["who"] for line in lines)
        assert recovered == list(range(len(bodies)))

    def test_the_cap_is_far_above_anything_a_real_run_emits(self):
        # 56,536 bytes is the largest payload measured on the live stream, 2026-09-10.
        assert receiver.MAX_BODY_BYTES > 100 * 56_536


def _declared_length_of(headers: dict) -> int | None:
    handler = receiver.TelemetryHandler.__new__(receiver.TelemetryHandler)
    handler.headers = headers
    return handler._declared_length()


# ── Task 5 — the module names neither the port nor the directory ────────


class TestNoHardcodedPortOrDirectory:
    def test_module_source_names_neither_the_real_port_nor_the_real_directory(self):
        source = Path(receiver.__file__).read_text()
        matches = re.findall(r"4318|\.claude/telemetry|\.kensaiapp/telemetry", source)
        assert matches == []

    def test_port_and_dir_are_required_cli_arguments(self):
        # No arguments, and `--dir` without `--port`, are refused. `--port` alone is no longer:
        # the directory has a default.
        with pytest.raises(SystemExit):
            receiver.main([])
        with pytest.raises(SystemExit):
            receiver.main(["--dir", "/tmp/does-not-matter"])

    def test_receiver_runs_end_to_end_on_an_ephemeral_port_against_a_temp_dir(self, tmp_path):
        server = receiver.build_receiver(0, str(tmp_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _ = _post(
                server.server_port, "/v1/logs", json.dumps({"cost_usd": 0.1}).encode()
            )
            assert status // 100 == 2
            record = json.loads((tmp_path / "logs.jsonl").read_text().splitlines()[0])
            assert record["body"]["cost_usd"] == 0.1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ── `a-launcher-tags-its-own-run` Task 1 — the receiver publishes where it is listening ────────
#
# The port is authored once, in the plist; the receiver writes it where a client can read it, so
# no launcher and no library ever names the number. `--publish-endpoint` is optional and carries no
# default of its own, exactly like `--port` and `--dir` carry no defaults of their own (they are
# merely *required*, where this one is merely *absent when omitted*) — the singleton is still the
# plist, not this module.


class TestPublishedEndpoint:
    def test_publish_endpoint_argument_carries_no_default_and_is_optional(self):
        source = Path(receiver.__file__).read_text()
        tree = ast.parse(source)
        add_argument_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ]
        publish_calls = [
            call
            for call in add_argument_calls
            if call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "--publish-endpoint"
        ]
        assert len(publish_calls) == 1
        keywords = {kw.arg for kw in publish_calls[0].keywords}
        assert "default" not in keywords
        assert "required" not in keywords

    def test_publish_endpoint_is_the_only_new_top_level_function_and_sits_ahead_of_build_receiver(
        self,
    ):
        # The composition-root ordering Task 1 (station 1) pinned — `top_level_funcs[-2:] ==
        # ["build_receiver", "main"]` — must still hold: a new function earns a home ahead of
        # `build_receiver`, never between it and `main`.
        source = Path(receiver.__file__).read_text()
        tree = ast.parse(source)
        top_level_funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert top_level_funcs[-2:] == ["build_receiver", "main"]
        assert "publish_endpoint" in top_level_funcs

    def test_started_without_publish_endpoint_writes_nothing_and_serves_as_before(self, tmp_path):
        port = _free_port()
        data_dir = tmp_path / "data"
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(receiver.__file__)),
                "--port",
                str(port),
                "--dir",
                str(data_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(),
        )
        try:
            deadline = time.monotonic() + 10
            status = None
            while True:
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    pytest.fail(
                        f"receiver exited before serving: {stderr.decode(errors='replace')}"
                    )
                try:
                    status, _ = _post(port, "/v1/logs", b'{"ok": true}')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("receiver never started serving")
                    time.sleep(0.1)
            assert status // 100 == 2
            # Nothing named like a published endpoint appears anywhere under the data directory —
            # the whole of "writes nothing" for an argument nobody handed it a path for.
            assert list(data_dir.rglob("*endpoint*")) == []
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_started_with_publish_endpoint_writes_the_built_endpoint_and_it_round_trips(
        self, tmp_path
    ):
        port = _free_port()
        pub_file = tmp_path / "endpoint"
        data_dir = tmp_path / "data"
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(receiver.__file__)),
                "--port",
                str(port),
                "--dir",
                str(data_dir),
                "--publish-endpoint",
                str(pub_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(),
        )
        try:
            deadline = time.monotonic() + 10
            while not pub_file.exists():
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    pytest.fail(
                        "receiver exited before publishing an endpoint: "
                        f"{stderr.decode(errors='replace')}"
                    )
                if time.monotonic() > deadline:
                    pytest.fail("receiver never published an endpoint")
                time.sleep(0.1)

            endpoint = pub_file.read_text().strip()
            # Built from the port it was handed — the same port passed on the command line, since
            # this test hands a real (non-zero) port rather than an ephemeral 0.
            assert endpoint == f"http://localhost:{port}"

            # The round trip: a client reads the published file and POSTs to what it names.
            status, _ = _post(port, "/v1/logs", json.dumps({"cost_usd": 0.3}).encode())
            assert status // 100 == 2
            record = json.loads((data_dir / "logs.jsonl").read_text().splitlines()[0])
            assert record["body"]["cost_usd"] == 0.3
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_publish_endpoint_function_writes_the_built_url_from_the_bound_port(self, tmp_path):
        # Exercises `publish_endpoint()` directly against a live, ephemeral-port receiver — the
        # "driven end to end on an ephemeral port" half of the behaviour, without a subprocess.
        server = receiver.build_receiver(0, str(tmp_path / "data"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pub_file = tmp_path / "endpoint"
        try:
            receiver.publish_endpoint(str(pub_file), server.server_port)
            assert pub_file.read_text().strip() == f"http://localhost:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_module_source_still_names_neither_the_real_port_nor_the_real_directory(self):
        # Task 1's own guard, re-asserted here because the new code is what could have
        # reintroduced a literal.
        source = Path(receiver.__file__).read_text()
        matches = re.findall(r"4318|\.claude/telemetry|\.kensaiapp/telemetry", source)
        assert matches == []


# ── Task 6 — the machine can start it, by hand ────────


class TestInstallMaterials:
    """The install materials are **machine state**, not repo files —
    `<CORE>/doctrine/setup/kensaiapp_structure.md` § Machine state — never `[create]`. They name
    this machine's user and this machine's checkout, which is why they cannot be vendored.

    That same doctrine says no tool may *require* machine state to exist, so every test here skips
    when the plist has not been installed. A fresh satellite, or a checkout on another machine, runs
    this suite green without it — and on a machine where it IS installed, these run for real.
    """

    INSTALL_DIR = Path.home() / ".claude" / "tools" / "otel-receiver"
    PLIST_PATH = next(INSTALL_DIR.glob("*.plist"), INSTALL_DIR / "absent.plist")
    INSTALL_PATH = INSTALL_DIR / "INSTALL.md"

    @pytest.fixture(autouse=True)
    def _skip_when_not_installed(self):
        if not self.PLIST_PATH.is_file():
            pytest.skip(
                f"no receiver installed at {self.INSTALL_DIR} — machine state, not repo state"
            )

    def test_a_keepalive_plist_and_an_install_md_sit_together(self):
        assert self.PLIST_PATH.is_file()
        assert self.INSTALL_PATH.is_file()

    def test_plist_names_the_label_port_and_directory_each_exactly_once(self):
        text = self.PLIST_PATH.read_text()
        plist = plistlib.loads(text.encode())
        # The label is machine state's to choose; core asserts its SHAPE, not this machine's
        # name. Pinning the literal would put a person's name in the vendored layer.
        assert plist["Label"] == self.PLIST_PATH.stem
        assert re.fullmatch(r"[a-z0-9]+(\.[a-z0-9-]+)+", plist["Label"])
        assert plist["KeepAlive"] is True
        assert len(re.findall(r"4318", text)) == 1
        assert len(re.findall(r"\.kensaiapp/telemetry", text)) == 1

    def test_plist_publishes_the_endpoint_where_instrument_run_reads_it(self):
        # The receiver and the turn-on meet at one file. The plist names where the receiver
        # writes it and the turn-on names where it reads it by default; if the two ever disagree,
        # every run goes uninstrumented and says so politely, which is easy to miss.
        command = plistlib.loads(self.PLIST_PATH.read_bytes())["ProgramArguments"][-1]
        assert "--publish-endpoint" in command
        assert ".claude/tools/otel-receiver/endpoint" in command
        assert turn_on._DEFAULT_ENDPOINT.as_posix() in command

    def test_install_md_states_the_launchctl_commands_and_that_they_are_the_owners_move(self):
        text = self.INSTALL_PATH.read_text()
        assert "launchctl bootstrap" in text
        assert "launchctl kickstart" in text
        assert "launchctl bootout" in text
        assert re.search(r"owner", text, re.IGNORECASE)

    def test_the_plists_own_command_starts_a_serving_receiver(self, tmp_path):
        """Task 6's intent is that **the machine can start it**. Counting the strings in the
        plist cannot see whether the command it hands launchd is one launchd can run — and a
        `KeepAlive` agent whose command cannot exec does not fail loudly, it respawns forever
        while every run's telemetry goes nowhere.

        So: drive the plist's *own* `ProgramArguments`, swapping only the three values it pins
        to this machine — the script path, the port and the directory — for test-safe ones. The
        shape of the command bash is handed is the plist's, untouched.
        """
        plist = plistlib.loads(self.PLIST_PATH.read_bytes())
        argv = list(plist["ProgramArguments"])
        port = _free_port()
        # Each substitution replaces the path/port *token* only and leaves whatever quoting
        # the plist puts around it exactly as it is — the quoting is the thing under test.
        command = argv[-1]
        command = re.sub(r"[^\s\"']*otel_receiver\.py", str(Path(receiver.__file__)), command)
        # The package needs Python 3.11 or later (`requires-python` in pyproject.toml), and the
        # plist's bare `python3` is whatever a login shell finds first, which here is the
        # system's older one. So the interpreter token is swapped too, for the one running this
        # suite; the quoting around the path is still the plist's.
        assert "/usr/bin/env python3" in command, command
        command = command.replace("/usr/bin/env python3", f"/usr/bin/env {sys.executable}")
        command = command.replace("4318", str(port))
        command = re.sub(r"[^\s\"']*\.kensaiapp/telemetry", str(tmp_path), command)
        # The fourth value the plist pins to this machine: where the receiver publishes its
        # endpoint for the turn-on to read by default. Left unswapped, this test's receiver would
        # write a throwaway port into the LIVE file, and every launcher after it would POST to
        # a port nothing listens on.
        command = re.sub(
            r"[^\s\"']*\.claude/tools/otel-receiver/endpoint",
            str(tmp_path / "published-endpoint"),
            command,
        )
        assert ".claude/tools/otel-receiver" not in command, command
        argv[-1] = command

        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env()
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    _, stderr = process.communicate()
                    pytest.fail(
                        "the plist's command exited with "
                        f"{process.returncode} instead of serving: "
                        f"{stderr.decode(errors='replace').strip()}"
                    )
                try:
                    status, _ = _post(port, "/v1/logs", b'{"cost_usd": 0.2}')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("the plist's command never started serving")
                    time.sleep(0.2)
            assert status // 100 == 2
            record = json.loads((tmp_path / "logs.jsonl").read_text().splitlines()[0])
            assert record["body"]["cost_usd"] == 0.2
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_nothing_under_launchagents_is_referenced_by_the_module_itself(self):
        source = Path(receiver.__file__).read_text()
        assert "LaunchAgents" not in source
        assert "launchctl" not in source


# ── The stream change — the receiver as the package's own ────────
#
# What follows is new with the move into the package: the directory is optional, the flags are
# exact, an `attributes` entry keyed `user.email` never reaches a disk, and the receiver never
# removes anything it wrote. The deletion-recovery cases drive a real receiver through a file-I/O
# barrier, so they live in `tests/e2e/test_stream_receiver.py`.


def _raw_exchange(port: int, head: bytes, body: bytes = b"") -> tuple[int, bytes]:
    """One request written byte for byte, so a test can send what `http.client` would not: a
    missing, malformed or non-positive `Content-Length`. Returns (status, response body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        raw.sendall(head + body)
        received = b""
        while True:
            chunk = raw.recv(65536)
            if not chunk:
                break
            received += chunk
    head_text, _, payload = received.partition(b"\r\n\r\n")
    return int(head_text.split(b" ", 2)[1]), payload


def _start_receiver_process(arguments: list[str], **kwargs) -> subprocess.Popen:
    """The receiver as a user runs it: `python -m ka_telemetry.receiver`, with the product root
    on the child's import path and nothing else of the test's configuration."""
    return subprocess.Popen(
        [sys.executable, "-m", "ka_telemetry.receiver", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **{"cwd": PRODUCT_ROOT, "env": _child_env(), **kwargs},
    )


def _wait_until_serving(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 15
    while True:
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f"receiver exited before serving: {stderr.decode(errors='replace')}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail("receiver never started serving")
            time.sleep(0.05)


def _stop(process: subprocess.Popen) -> tuple[bytes, bytes]:
    process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


class TestDefaultDirectory:
    def test_default_directory_created_under_home_when_omitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = receiver.build_receiver(0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            expected = tmp_path / ".ka-telemetry"
            assert server.store._directory == expected
            status, _ = _post(server.server_port, "/v1/logs", b'{"a": 1}')
            assert status == 200
            assert json.loads((expected / "logs.jsonl").read_text())["body"] == {"a": 1}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_the_default_is_the_one_shared_value_expanded_in_build_receiver(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = receiver.build_receiver(0)
        try:
            default = Path(stream_paths.DEFAULT_STREAM_DIRECTORY).expanduser()
            assert server.store._directory == default
        finally:
            server.server_close()

    def test_a_leading_tilde_in_an_explicit_directory_is_expanded_and_overrides_the_default(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        server = receiver.build_receiver(0, "~/elsewhere")
        try:
            assert server.store._directory == tmp_path / "elsewhere"
            assert not (tmp_path / ".ka-telemetry").exists()
        finally:
            server.server_close()


class TestReceiverFlags:
    def test_loopback_default_and_explicit_host(self, tmp_path):
        default = receiver.build_receiver(0, str(tmp_path))
        explicit = receiver.build_receiver(0, str(tmp_path), "127.0.0.1")
        try:
            assert default.server_address[0] == "127.0.0.1"
            assert explicit.server_address[0] == "127.0.0.1"
        finally:
            default.server_close()
            explicit.server_close()

    def test_port_required(self, tmp_path):
        with pytest.raises(SystemExit) as refused:
            receiver.main(["--dir", str(tmp_path)])
        assert refused.value.code == 2

    def test_abbreviated_and_unknown_flags_are_rejected(self, tmp_path):
        for arguments in (
            ["--port", str(_free_port()), "--di", str(tmp_path)],
            ["--por", "0"],
            ["--port", "0", "--publish", str(tmp_path / "endpoint")],
            ["--port", "0", "--ho", "127.0.0.1"],
            ["--port", "0", "--bogus"],
        ):
            process = _start_receiver_process(arguments)
            try:
                _, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                _stop(process)
                pytest.fail(f"{arguments} was accepted and the receiver started serving")
            assert process.returncode == 2, arguments
            assert b"usage:" in stderr, arguments

    def test_endpoint_optional_and_absent_endpoint_writes_nothing(self, tmp_path):
        port = _free_port()
        data_dir = tmp_path / "data"
        process = _start_receiver_process(
            ["--port", str(port), "--dir", str(data_dir)], cwd=tmp_path
        )
        try:
            _wait_until_serving(process, port)
            assert _post(port, "/v1/logs", b'{"ok": true}')[0] == 200
        finally:
            _stop(process)
        assert sorted(path.name for path in tmp_path.iterdir()) == ["data"]
        assert [path.name for path in data_dir.iterdir()] == ["logs.jsonl"]

    def test_port_zero_publishes_actual_bound_port(self, tmp_path):
        endpoint_file = tmp_path / "endpoint"
        process = _start_receiver_process(
            [
                "--port",
                "0",
                "--dir",
                str(tmp_path / "data"),
                "--publish-endpoint",
                str(endpoint_file),
            ]
        )
        try:
            deadline = time.monotonic() + 15
            while not endpoint_file.exists():
                assert process.poll() is None, process.communicate()[1]
                assert time.monotonic() < deadline, "no endpoint was published"
                time.sleep(0.05)
            published = endpoint_file.read_text()
            port = int(published.strip().rsplit(":", 1)[1])
            assert published == f"http://localhost:{port}\n"
            assert port != 0
            assert _post(port, "/v1/metrics", b'{"n": 1}')[0] == 200
        finally:
            _stop(process)

    def test_receiver_silent_and_no_outbound_client(self, tmp_path):
        port = _free_port()
        process = _start_receiver_process(["--port", str(port), "--dir", str(tmp_path / "data")])
        try:
            _wait_until_serving(process, port)
            for path in ("/v1/logs", "/v1/metrics", "/v1/traces"):
                _post(port, path, b'{"n": 1}')
            _get(port, "/v1/logs")
        finally:
            stdout, stderr = _stop(process)
        assert stderr == b""
        assert stdout.endswith(b"\n") and len(stdout.splitlines()) == 1

        tree = ast.parse(Path(receiver.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not imported & {"urllib", "urllib.request", "http.client", "requests", "ssl"}
        assert "create_connection" not in Path(receiver.__file__).read_text()


class TestHttpContractRetained:
    def test_http_status_and_body_cases_retained(self, running_receiver):
        server, tmp_path = running_receiver
        port = server.server_port
        post = b"POST %s HTTP/1.0\r\n"
        two_bytes = b"Content-Length: 2\r\n\r\n"
        assert _raw_exchange(port, post % b"/v1/logs" + two_bytes, b"{}") == (200, b"{}")
        assert _raw_exchange(port, post % b"/v1/metrics" + two_bytes, b"{}") == (200, b"{}")
        assert _raw_exchange(port, post % b"/v1/traces" + b"Content-Length: 2\r\n\r\n", b"{}") == (
            404,
            b'{"error": "not found"}',
        )
        assert _raw_exchange(port, b"GET /v1/logs HTTP/1.0\r\n\r\n") == (
            405,
            b'{"error": "method not allowed"}',
        )
        for declared in (b"0", b"-5"):
            request = post % b"/v1/logs" + b"Content-Length: " + declared + b"\r\n\r\n"
            assert _raw_exchange(port, request) == (
                400,
                b'{"error": "empty body"}',
            )
        assert _raw_exchange(port, post % b"/v1/logs" + b"\r\n") == (
            400,
            b'{"error": "empty body"}',
        )
        assert _raw_exchange(port, post % b"/v1/logs" + b"Content-Length: abc\r\n\r\n", b"{}") == (
            400,
            b'{"error": "invalid Content-Length"}',
        )
        assert _raw_exchange(port, post % b"/v1/logs" + b"Content-Length: 5\r\n\r\n", b"{nope") == (
            400,
            b'{"error": "invalid JSON"}',
        )
        over = b"Content-Length: %d\r\n\r\n" % (receiver.MAX_BODY_BYTES + 1)
        assert _raw_exchange(port, post % b"/v1/logs" + over) == (
            413,
            b'{"error": "payload too large"}',
        )
        # Exactly at the cap is accepted: a JSON string padded to MAX_BODY_BYTES bytes.
        at_cap = b'"' + b"x" * (receiver.MAX_BODY_BYTES - 2) + b'"'
        head = post % b"/v1/logs" + b"Content-Length: %d\r\n\r\n" % len(at_cap)
        assert _raw_exchange(port, head, at_cap) == (200, b"{}")
        written = (tmp_path / "logs.jsonl").read_text().splitlines()
        assert len(written) == 2
        assert len(json.loads(written[1])["body"]) == receiver.MAX_BODY_BYTES - 2

    def test_a_failing_store_gives_500_with_its_body_and_the_server_then_serves(self, tmp_path):
        class FailsOnce:
            def __init__(self):
                self.calls = 0
                self.kept = []

            def append(self, signal, received_at, body):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("disk full")
                self.kept.append(body)

        store = FailsOnce()
        server = receiver.TelemetryServer(
            ("127.0.0.1", 0), receiver.TelemetryHandler, store, receiver.Clock()
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert _post(server.server_port, "/v1/logs", b'{"n": 1}') == (
                500,
                b'{"error": "could not store record"}',
            )
            assert _post(server.server_port, "/v1/logs", b'{"n": 2}') == (200, b"{}")
            assert store.kept == [{"n": 2}]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_json_scalar_and_list_bodies_still_accepted(self, running_receiver):
        server, tmp_path = running_receiver
        bodies = [[1, 2, {"attributes": []}], 5, "text", None, True, [[{"attributes": [3]}]]]
        for body in bodies:
            status, payload = _post(server.server_port, "/v1/logs", json.dumps(body).encode())
            assert (status, payload) == (200, b"{}"), body
        lines = (tmp_path / "logs.jsonl").read_text().splitlines()
        stored = [json.loads(line)["body"] for line in lines]
        assert stored == bodies

    def test_envelope_utc_clock_newline_and_concurrent_piecewise_writes_retained(
        self, running_receiver
    ):
        server, tmp_path = running_receiver
        bodies = [{"who": index, "pad": "x" * 2_000, "note": "a\nb"} for index in range(12)]
        threads = [
            threading.Thread(
                target=_post, args=(server.server_port, "/v1/metrics", json.dumps(body).encode())
            )
            for body in bodies
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        raw = (tmp_path / "metrics.jsonl").read_text()
        assert raw.endswith("\n") and not raw.endswith("\n\n")
        records = [json.loads(line) for line in raw.splitlines()]
        assert len(records) == len(bodies)
        for record in records:
            assert list(record) == ["signal", "received_at", "body"]
            assert record["signal"] == "metrics"
            stamp = datetime.fromisoformat(record["received_at"])
            assert stamp.utcoffset() == timedelta(0)
        assert sorted(record["body"]["who"] for record in records) == list(range(12))


def _email(value: str = "someone@example.com") -> dict:
    return {"key": "user.email", "value": {"stringValue": value}}


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


class TestEmailRemoval:
    def test_email_attribute_removed_at_every_depth(self, tmp_path):
        keep = [_attr("event.name", "api_request"), _attr("session.id", "s-1")]
        body = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [_email(), *keep, _email("again@example.com")]},
                    "scopeLogs": [
                        {
                            "scope": {"attributes": [keep[0], _email()]},
                            "logRecords": [
                                {"attributes": [keep[1], _email(), keep[0]], "body": {}}
                            ],
                        }
                    ],
                }
            ],
            "resourceMetrics": [
                {
                    "resource": {"attributes": [_email()]},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "m",
                                    "sum": {"dataPoints": [{"attributes": [_email(), keep[1]]}]},
                                }
                            ]
                        }
                    ],
                }
            ],
            "nested": [[{"attributes": [_email(), {"key": "x", "value": {"intValue": "1"}}]}]],
        }
        expected = json.loads(json.dumps(body))
        expected["resourceLogs"][0]["resource"]["attributes"] = keep
        expected["resourceLogs"][0]["scopeLogs"][0]["scope"]["attributes"] = [keep[0]]
        log_record = expected["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        log_record["attributes"] = [keep[1], keep[0]]
        expected["resourceMetrics"][0]["resource"]["attributes"] = []
        expected["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"][0][
            "attributes"
        ] = [keep[1]]
        expected["nested"][0][0]["attributes"] = [{"key": "x", "value": {"intValue": "1"}}]

        store = receiver.Store(tmp_path)
        store.append(receiver.Signal.LOGS, "t", body)

        stored = json.loads((tmp_path / "logs.jsonl").read_text())["body"]
        assert stored == expected
        assert "user.email" not in (tmp_path / "logs.jsonl").read_text()

    def test_email_free_bodies_are_stored_unchanged_and_an_address_in_other_text_is_left_alone(
        self, tmp_path
    ):
        body = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [_attr("user.name", "someone@example.com")]},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {"attributes": [_attr("prompt", "mail someone@example.com")]}
                            ]
                        }
                    ],
                }
            ],
            "emailish": {"user.email": "kept: not an attributes entry"},
        }
        receiver.Store(tmp_path).append(receiver.Signal.LOGS, "t", body)

        assert json.loads((tmp_path / "logs.jsonl").read_text())["body"] == body

    def test_removal_happens_in_the_store_so_a_direct_append_cannot_bypass_it(self, tmp_path):
        body = {"resourceMetrics": [{"resource": {"attributes": [_email(), _attr("k", "v")]}}]}
        receiver.Store(tmp_path).append(receiver.Signal.METRICS, "t", body)

        stored = json.loads((tmp_path / "metrics.jsonl").read_text())["body"]
        assert stored["resourceMetrics"][0]["resource"]["attributes"] == [_attr("k", "v")]

    def test_the_caller_s_body_is_not_modified(self, tmp_path):
        body = {"resource": {"attributes": [_email()]}}
        receiver.Store(tmp_path).append(receiver.Signal.LOGS, "t", body)

        assert body == {"resource": {"attributes": [_email()]}}


class TestTheReceiverNeverRemovesWhatItWrote:
    def test_append_never_deletes_truncates_or_renames(self, running_receiver):
        server, tmp_path = running_receiver
        seen = {}
        for signal in ("logs", "metrics"):
            for index in range(5):
                body = json.dumps({"n": index}).encode()
                status, _ = _post(server.server_port, f"/v1/{signal}", body)
                assert status == 200
                path = tmp_path / f"{signal}.jsonl"
                stat = path.stat()
                if signal in seen:
                    previous = seen[signal]
                    assert (stat.st_dev, stat.st_ino) == previous["identity"]
                    assert stat.st_size > previous["size"]
                    assert path.read_text().splitlines()[: previous["lines"]] == previous["text"]
                seen[signal] = {
                    "identity": (stat.st_dev, stat.st_ino),
                    "size": stat.st_size,
                    "lines": index + 1,
                    "text": path.read_text().splitlines(),
                }
        assert sorted(path.name for path in tmp_path.iterdir()) == ["logs.jsonl", "metrics.jsonl"]
        for signal in ("logs", "metrics"):
            lines = (tmp_path / f"{signal}.jsonl").read_text().splitlines()
            bodies = [json.loads(line)["body"]["n"] for line in lines]
            assert bodies == [0, 1, 2, 3, 4]
