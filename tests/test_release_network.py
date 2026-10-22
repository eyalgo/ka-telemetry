"""The release check's watched processes: what a `ka-telemetry` command tries to reach, recorded.

Real children run under the watch. A stand-in product (a package named `ka_telemetry` made under
pytest's temporary directory) does the harmless thing each case needs, so a refusal is seen from
outside the process that made the attempt; the real receiver is driven where the case is about the
receiver itself. Nothing here contacts a real address: every non-local place is refused before it
is resolved or reached, and the destinations are documentation addresses and reserved names.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tools import release_network
from tools.release_network import Attempt

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTATION_ADDRESS = "192.0.2.1"
SHORT_BOUND = 3


def make_product(base: Path, main_source: str, init_source: str = "") -> Path:
    """A stand-in `ka_telemetry` package whose `python3 -m ka_telemetry` runs `main_source`."""
    package = base / "product" / "ka_telemetry"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(textwrap.dedent(init_source), encoding="utf-8")
    (package / "__main__.py").write_text(textwrap.dedent(main_source), encoding="utf-8")
    return package.parent


def places(base: Path) -> tuple[Path, Path]:
    home, workdir = base / "home", base / "work"
    home.mkdir()
    workdir.mkdir()
    return home, workdir


def watched(base: Path, main_source: str, *arguments: str, init_source: str = "", timeout=20):
    product = make_product(base, main_source, init_source)
    home, workdir = places(base)
    return release_network.run_finite(
        product, arguments, home=home, workdir=workdir, timeout=timeout
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


# ── what is recorded ──


def test_tcp_connect_to_a_documentation_address_is_recorded_and_refused(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket
        try:
            socket.create_connection(("{DOCUMENTATION_ADDRESS}", 9), timeout=2)
        except OSError as error:
            print("refused:", type(error).__name__)
        """,
    )

    assert seen.attempts == (Attempt("connect", DOCUMENTATION_ADDRESS, 9),)
    assert "refused:" in seen.stdout
    assert seen.evidence_problem is None


def test_connect_ex_udp_connect_and_unconnected_datagram_send_are_recorded(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print("connect_ex", tcp.connect_ex(("{DOCUMENTATION_ADDRESS}", 9)))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.connect(("{DOCUMENTATION_ADDRESS}", 10))
        except OSError:
            pass
        try:
            udp.sendto(b"x", ("{DOCUMENTATION_ADDRESS}", 11))
        except OSError:
            pass
        try:
            udp.sendto(b"x", 0, ("{DOCUMENTATION_ADDRESS}", 12))
        except OSError:
            pass
        """,
    )

    assert seen.attempts == (
        Attempt("connect_ex", DOCUMENTATION_ADDRESS, 9),
        Attempt("connect", DOCUMENTATION_ADDRESS, 10),
        Attempt("sendto", DOCUMENTATION_ADDRESS, 11),
        Attempt("sendto", DOCUMENTATION_ADDRESS, 12),
    )
    assert "connect_ex 0" not in seen.stdout, "connect_ex must report the refusal as an error code"


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="no sendmsg on this platform")
def test_addressed_sendmsg_is_recorded_where_available(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.sendmsg([b"x"], [], 0, ("{DOCUMENTATION_ADDRESS}", 13))
        except OSError:
            pass
        """,
    )

    assert seen.attempts == (Attempt("sendmsg", DOCUMENTATION_ADDRESS, 13),)


def test_forward_and_reverse_lookups_of_remote_names_are_recorded_and_refused(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket
        for call in (
            lambda: socket.getaddrinfo("example.com", 80),
            lambda: socket.gethostbyname("example.com"),
            lambda: socket.gethostbyname_ex("example.net"),
            lambda: socket.gethostbyaddr("{DOCUMENTATION_ADDRESS}"),
            lambda: socket.getnameinfo(("{DOCUMENTATION_ADDRESS}", 80), 0),
        ):
            try:
                call()
            except OSError as error:
                print("refused", type(error).__name__)
        """,
    )

    assert seen.attempts == (
        Attempt("getaddrinfo", "example.com", 80),
        Attempt("gethostbyname", "example.com", None),
        Attempt("gethostbyname", "example.net", None),
        Attempt("gethostbyaddr", DOCUMENTATION_ADDRESS, None),
        Attempt("getnameinfo", DOCUMENTATION_ADDRESS, 80),
    )
    assert seen.stdout.count("refused") == 5


def test_a_connection_or_datagram_given_a_host_name_is_recorded_by_that_name_and_refused(tmp_path):
    # CP-563/564: a connection or a datagram aimed at a host name reaches for the network as much
    # as one aimed at an address. `.invalid` never resolves (RFC 6761), so a name that were looked
    # up before the watch saw it would fail inside the call and leave no record at all.
    seen = watched(
        tmp_path,
        """
        import socket
        try:
            socket.socket().connect(("nowhere.invalid", 80))
        except OSError as error:
            print("connect refused", type(error).__name__)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.sendto(b"x", ("nowhere.invalid", 53))
        except OSError as error:
            print("sendto refused", type(error).__name__)
        """,
    )

    assert seen.attempts == (
        Attempt("connect", "nowhere.invalid", 80),
        Attempt("sendto", "nowhere.invalid", 53),
    )
    assert "connect refused ConnectionRefusedError" in seen.stdout
    assert "sendto refused ConnectionRefusedError" in seen.stdout
    assert seen.evidence_problem is None


def test_a_caught_refusal_with_exit_zero_still_leaves_the_record(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket, sys
        try:
            socket.socket().connect(("{DOCUMENTATION_ADDRESS}", 9))
        except OSError:
            pass
        sys.exit(0)
        """,
    )

    assert seen.outcome == release_network.Outcome(release_network.EXITED, status=0)
    assert seen.attempts == (Attempt("connect", DOCUMENTATION_ADDRESS, 9),)


def test_every_attempt_is_recorded_not_only_the_first(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket
        for port in (21, 22, 23):
            try:
                socket.socket().connect(("{DOCUMENTATION_ADDRESS}", port))
            except OSError:
                pass
        try:
            socket.getaddrinfo("example.com", 80)
        except OSError:
            pass
        """,
    )

    assert [attempt.port for attempt in seen.attempts] == [21, 22, 23, 80]


def test_attempts_from_other_threads_persist(tmp_path):
    seen = watched(
        tmp_path,
        f"""
        import socket, threading

        def attempt(port):
            try:
                socket.socket().connect(("{DOCUMENTATION_ADDRESS}", port))
            except OSError:
                pass

        threads = [threading.Thread(target=attempt, args=(port,)) for port in (31, 32, 33)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        """,
    )

    assert sorted(attempt.port for attempt in seen.attempts) == [31, 32, 33]


def test_the_watch_is_ready_before_an_import_time_attempt(tmp_path):
    seen = watched(
        tmp_path,
        "print('main ran')\n",
        init_source=f"""
            import socket
            try:
                socket.socket().connect(("{DOCUMENTATION_ADDRESS}", 41))
            except OSError:
                pass
            """,
    )

    assert seen.attempts == (Attempt("connect", DOCUMENTATION_ADDRESS, 41),)


# ── what counts as outbound ──

LOCAL_PLACES = [
    "127.0.0.1",
    "127.0.0.2",
    "127.255.255.254",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:127.9.9.9",
    "localhost",
    "LocalHost",
]
OUTBOUND_PLACES = [
    "0.0.0.0",
    "::",
    "8.8.8.8",
    "128.0.0.1",
    "126.255.255.255",
    "::2",
    "::ffff:128.0.0.1",
    "::ffff:0.0.0.0",
    "example.com",
    "localhost.example.com",
    "loopback.example",
    "",
]


@pytest.mark.parametrize("place", LOCAL_PLACES)
def test_loopback_places_are_not_outbound(place):
    assert release_network.classify("connect", (place, 80)) is None
    assert release_network.classify("sendto", (place, 80)) is None


def test_loopback_places_are_not_outbound_on_real_sockets(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        seen = watched(
            tmp_path,
            f"""
            import socket
            socket.create_connection(("127.0.0.1", {port}), timeout=2).close()
            socket.create_connection(("localhost", {port}), timeout=2).close()
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.sendto(b"x", ("127.0.0.2", 9))
            """,
        )

    assert seen.attempts == ()
    assert seen.outcome == release_network.Outcome(release_network.EXITED, status=0)


def test_unix_domain_connection_is_not_an_attempt(tmp_path):
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rn") as short:
        path = os.path.join(short, "s")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen()
            seen = watched(
                tmp_path,
                f"""
                import socket
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect({path!r})
                client.close()
                """,
            )

    assert seen.attempts == ()
    assert seen.outcome == release_network.Outcome(release_network.EXITED, status=0)
    assert release_network.classify("connect", "/tmp/anything.sock", family=socket.AF_UNIX) is None
    assert release_network.classify("connect", b"/tmp/anything.sock", family=socket.AF_UNIX) is None


@pytest.mark.parametrize("place", OUTBOUND_PLACES)
def test_wildcard_and_every_other_address_or_name_is_outbound(place):
    # A name is never resolved to decide: "loopback.example" is outbound whatever it points at.
    attempt = release_network.classify("connect", (place, 80))
    assert attempt == Attempt("connect", place, 80)


def test_an_ipv6_socket_address_is_judged_by_its_address_whatever_its_flow_and_scope():
    # An IPv6 socket address has four parts; CP-563 judges the address alone.
    assert release_network.classify("connect", ("::1", 80, 0, 0), family=socket.AF_INET6) is None
    mapped = ("::ffff:127.0.0.1", 80, 0, 0)
    assert release_network.classify("sendto", mapped, family=socket.AF_INET6) is None
    assert release_network.classify(
        "connect", ("2001:db8::1", 80, 0, 0), family=socket.AF_INET6
    ) == Attempt("connect", "2001:db8::1", 80)
    assert release_network.classify(
        "connect", ("fe80::1%lo0", 80, 0, 1), family=socket.AF_INET6
    ) == Attempt("connect", "fe80::1%lo0", 80)


def test_a_numeric_address_is_not_a_host_name_to_look_up():
    assert release_network.classify("getaddrinfo", (DOCUMENTATION_ADDRESS, 80)) is None
    assert release_network.classify("gethostbyname", (DOCUMENTATION_ADDRESS,)) is None
    assert release_network.classify("getaddrinfo", (None, 80)) is None
    assert release_network.classify("getaddrinfo", ("localhost", 80)) is None
    assert release_network.classify("getaddrinfo", ("example.com", 80)) == Attempt(
        "getaddrinfo", "example.com", 80
    )


def test_reverse_lookup_of_a_loopback_address_is_not_outbound(tmp_path):
    assert release_network.classify("gethostbyaddr", ("127.0.0.1",)) is None
    assert release_network.classify("getnameinfo", (("127.0.0.1", 80),)) is None
    seen = watched(
        tmp_path,
        """
        import socket
        socket.gethostbyaddr("127.0.0.1")
        socket.getnameinfo(("127.0.0.1", 80), 0)
        """,
        # A real reverse lookup: on a machine with a slow resolver it alone took 20 seconds.
        timeout=120,
    )

    assert seen.attempts == ()
    assert seen.outcome == release_network.Outcome(release_network.EXITED, status=0)


# ── a record that cannot read as empty ──


def test_missing_ready_record_corrupt_evidence_or_failed_write_cannot_read_as_empty(tmp_path):
    ready = '{"record": "ready"}\n'
    end = '{"record": "end"}\n'
    attempt = (
        '{"record": "attempt", "operation": "connect", "destination": "192.0.2.1", "port": 9}\n'
    )

    absent = release_network.read_evidence(None, expect_end=False)
    assert absent.problem
    assert release_network.read_evidence("", expect_end=False).problem
    assert release_network.read_evidence(attempt, expect_end=False).problem, "no ready record"
    assert release_network.read_evidence(ready + "not json\n", expect_end=False).problem
    assert release_network.read_evidence(ready + '{"record": "mystery"}\n', False).problem
    assert release_network.read_evidence(ready + attempt[:-12], expect_end=False).problem
    assert release_network.read_evidence(ready, expect_end=True).problem, "no end record"
    intact = release_network.read_evidence(ready + attempt + end, expect_end=True)
    assert intact.problem is None
    assert intact.attempts == (Attempt("connect", DOCUMENTATION_ADDRESS, 9),)

    # A write that fails is raised to the caller and is remembered, so no end record follows it.
    read_end, write_end = os.pipe()
    os.close(read_end)
    evidence = release_network.Evidence(write_end)
    with pytest.raises(OSError):
        evidence.record({"record": "ready"})
    assert evidence.failed
    evidence.end()
    os.close(write_end)

    # A child that cannot open its evidence channel never runs the product and cannot read as empty.
    product = make_product(tmp_path, "print('ran')\n")
    home, workdir = places(tmp_path)
    (workdir / release_network.EVIDENCE_NAME).mkdir()
    seen = release_network.run_finite(product, (), home=home, workdir=workdir, timeout=20)
    assert seen.evidence_problem
    assert "ran" not in seen.stdout
    assert seen.attempts == ()


def test_nonzero_exit_startup_failure_and_hang_return_incomplete_outcomes(tmp_path):
    failing = tmp_path / "failing"
    failing.mkdir()
    failed = watched(failing, "import sys\nsys.exit(3)\n")
    assert failed.outcome == release_network.Outcome(release_network.EXITED, status=3)

    unstartable = tmp_path / "unstartable"
    unstartable.mkdir()
    product = make_product(unstartable, "print('never')\n")
    home, workdir = places(unstartable)
    missing = release_network.run_finite(
        product, (), home=home, workdir=workdir, python=str(tmp_path / "no-such-python")
    )
    assert missing.outcome.kind == release_network.NOT_STARTED
    assert missing.outcome.reason

    hanging = tmp_path / "hanging"
    hanging.mkdir()
    started = time.monotonic()
    hung = watched(hanging, "import time\ntime.sleep(60)\n", timeout=SHORT_BOUND)
    assert time.monotonic() - started < SHORT_BOUND + 15
    assert hung.outcome.kind == release_network.TIMED_OUT
    assert hung.outcome.seconds == SHORT_BOUND
    assert hung.pid is not None and gone_soon(hung.pid)


# ── the receiver ──

LOGS = b'{"resourceLogs": []}'
METRICS = b'{"resourceMetrics": []}'


def receiver_watched(base: Path, product: Path | None = None, timeout=30):
    home, workdir = places(base)
    return release_network.run_receiver(
        product or PRODUCT_ROOT,
        home=home,
        workdir=workdir,
        logs_body=LOGS,
        metrics_body=METRICS,
        timeout=timeout,
    )


def test_receiver_default_host_reports_its_actual_loopback_listening_socket(tmp_path):
    seen = receiver_watched(tmp_path)

    assert [(item.family, item.host) for item in seen.listening] == [("inet", "127.0.0.1")]
    assert seen.listening[0].port > 0
    assert seen.attempts == ()


def test_receiver_accepts_one_logs_and_one_metrics_request_then_is_stopped(tmp_path):
    seen = receiver_watched(tmp_path)

    assert seen.outcome.kind == release_network.COMPLETED
    assert [(item.path, item.status) for item in seen.requests] == [
        ("/v1/logs", 200),
        ("/v1/metrics", 200),
    ]
    assert seen.pid is not None and gone_soon(seen.pid)
    assert (tmp_path / "work").exists() and not list(tmp_path.glob("**/*.pid"))


LISTENING_STAND_IN = """
    import argparse, http.server, pathlib, socket, socketserver, sys, threading

    MODE = {mode!r}
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--port", type=int)
    parser.add_argument("--dir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--publish-endpoint")
    args = parser.parse_args()

    if MODE == "exit_at_once":
        sys.exit(2)
    if MODE == "never_listens":
        import time
        time.sleep(60)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if MODE == "reject":
                self.send_response(500)
            elif MODE == "die_on_request":
                import os
                os._exit(9)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):
            # No getfqdn: it looks up this machine's own name when bound to a wildcard.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "stand-in"
            self.server_port = self.server_address[1]

    server = Server(("{bind}", 0), Handler)
    pathlib.Path(args.publish_endpoint).write_text(
        "http://localhost:%d" % server.server_port, encoding="utf-8"
    )
    server.serve_forever()
"""


def stand_in_receiver(base: Path, mode: str = "serve", bind: str = "127.0.0.1") -> Path:
    return make_product(base, LISTENING_STAND_IN.format(mode=mode, bind=bind))


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("exit_at_once", release_network.NEVER_LISTENED),
        ("never_listens", release_network.NEVER_LISTENED),
        ("reject", release_network.REQUEST_NOT_ACCEPTED),
        ("die_on_request", release_network.STOPPED_EARLY),
    ],
)
def test_receiver_missing_startup_listen_or_either_request_is_incomplete(tmp_path, mode, kind):
    seen = receiver_watched(tmp_path, stand_in_receiver(tmp_path, mode), timeout=SHORT_BOUND)

    assert seen.outcome.kind == kind
    assert seen.outcome.reason
    if seen.pid is not None:
        assert gone_soon(seen.pid)


def test_a_wildcard_bind_is_a_listening_observation_not_an_outbound_attempt(tmp_path):
    seen = receiver_watched(tmp_path, stand_in_receiver(tmp_path, bind="0.0.0.0"))

    assert [(item.family, item.host) for item in seen.listening] == [("inet", "0.0.0.0")]
    assert seen.attempts == ()
    assert seen.outcome.kind == release_network.COMPLETED


def test_receiver_still_starts_when_port_4318_is_occupied(tmp_path):
    occupant = socket.socket()
    try:
        occupant.bind(("127.0.0.1", 4318))
        occupant.listen()
    except OSError:
        pass  # something else already holds 4318, which is the case this test is about
    try:
        seen = receiver_watched(tmp_path)
    finally:
        occupant.close()

    assert seen.outcome.kind == release_network.COMPLETED
    assert seen.listening[0].port != 4318


# ── nothing is left running ──

SPAWNING_SOURCE = """
    import subprocess, sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print(child.pid, flush=True)
    {ending}
"""


def grandchild_pid(seen) -> int:
    return int(seen.stdout.split()[0])


@pytest.mark.parametrize(
    ("ending", "timeout"),
    [("", 20), ("sys.exit(5)", 20), ("import time; time.sleep(60)", SHORT_BOUND)],
    ids=["success", "failure", "timeout"],
)
def test_no_child_is_left_running_on_success_failure_or_timeout(tmp_path, ending, timeout):
    seen = watched(tmp_path, SPAWNING_SOURCE.format(ending=ending), timeout=timeout)

    assert seen.pid is not None and gone_soon(seen.pid)
    assert gone_soon(grandchild_pid(seen))
