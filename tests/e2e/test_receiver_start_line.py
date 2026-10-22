"""End-to-end tests of what `python -m ka_telemetry receive` prints, through a real child process.

Every test starts the command the way a person does, with stdout and stderr each a pipe, an
isolated `HOME` and its own working directory. Nothing patches the receiver's startup: the proof is
what reaches the pipes.

Two rules hold for the helpers. A read of the child's stdout is bounded, so a receiver that never
prints fails the test rather than hanging the run. And the child is never started in unbuffered
mode: that would hide a missing flush, which is exactly what a reader holding the output as a pipe
would meet.

The failure tests assert absolute expectations — empty stdout, an exit status, a final stderr line
named by its exception shape and not by a platform's errno number — so they stay true however the
receiver's failure output later changes elsewhere.
"""

from __future__ import annotations

import builtins
import http.client
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ka_telemetry import receiver

PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_SECONDS = 30
START_LINE_SECONDS = 5
# An address in the documentation range (RFC 5737): no machine owns it, so binding it is refused.
FOREIGN_HOST = "203.0.113.1"
USAGE_NEXT_STEP = "ka-telemetry receive --help"


def child_env(home: Path) -> dict[str, str]:
    """This process's environment with `HOME` replaced, the product root importable, and no
    unbuffered mode, so a missing flush is visible to the test."""
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(PRODUCT_ROOT)}
    env.pop("PYTHONUNBUFFERED", None)
    return env


def receive_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "ka_telemetry", "receive", *arguments]


def run_to_end(
    arguments: list[str],
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """A run that ends on its own, bounded, with stdout and stderr captured as bytes."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        receive_command(*arguments),
        capture_output=True,
        check=False,
        env={**child_env(home), **(env_extra or {})},
        cwd=cwd or tmp_path,
        timeout=RUN_SECONDS,
    )


def last_stderr_line(completed: subprocess.CompletedProcess[bytes]) -> str:
    lines = completed.stderr.decode(errors="replace").splitlines()
    assert lines, "stderr is empty"
    return lines[-1]


def read_start_line(process: subprocess.Popen[bytes]) -> bytes:
    """One complete newline-terminated stdout line from a running child, within a bound.

    Reads the pipe's file descriptor directly, so a child that prints nothing costs the bound and
    not the run.
    """
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    received = b""
    deadline = time.monotonic() + START_LINE_SECONDS
    while b"\n" not in received:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"no complete stdout line in {START_LINE_SECONDS} s: {received!r}"
        assert process.poll() is None, f"the receiver ended first: {process.communicate()}"
        ready, _, _ = select.select([descriptor], [], [], remaining)
        if ready:
            chunk = os.read(descriptor, 4096)
            assert chunk, "stdout closed before a complete line"
            received += chunk
    return received


def accepts_connection(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def endpoint_port(endpoint_file: Path) -> int:
    """The port in an endpoint file, read whole: it must be exactly `http://localhost:<port>\\n`."""
    published = endpoint_file.read_text()
    port = int(published.strip().rsplit(":", 1)[1])
    assert published == f"http://localhost:{port}\n"
    return port


def names_port(text: str, port: int) -> bool:
    """Whether `text` carries `port` as a whole token, not as part of a longer number or name."""
    return re.search(rf"(?<![\w.]){port}(?![\w.])", text) is not None


def post(port: int, path: str, body: bytes) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, body=body)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def get(port: int, path: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


class RunningReceiver:
    """A `receive` child that has printed its first stdout line and is still running."""

    def __init__(self, process: subprocess.Popen[bytes], first_line: bytes, endpoint_file: Path):
        self.process = process
        self.first_line = first_line
        self.endpoint_file = endpoint_file
        self.stdout_after_line = b""
        self.stderr = b""

    @property
    def line(self) -> str:
        return self.first_line.decode("utf-8")

    @property
    def port(self) -> int:
        return endpoint_port(self.endpoint_file)

    @property
    def stdout(self) -> bytes:
        return self.first_line + self.stdout_after_line

    def stop(self, stop_signal: int = signal.SIGTERM) -> int:
        """Stop the child with `stop_signal`, keep what it wrote, and return its exit status."""
        self.process.send_signal(stop_signal)
        self.stdout_after_line, self.stderr = self.process.communicate(timeout=RUN_SECONDS)
        return self.process.returncode


@contextmanager
def running_receiver(
    tmp_path: Path,
    *arguments: str,
    home: Path | None = None,
    cwd: Path | None = None,
    publish: bool = True,
) -> Iterator[RunningReceiver]:
    """`receive` as a child under its own `HOME`, once its first stdout line has been read.

    The endpoint file is how a test learns the port of `--port 0`; `publish=False` leaves it off,
    for the test that must work without it. The child is reaped even after a failed assertion.
    """
    home = home or tmp_path / "home"
    home.mkdir(exist_ok=True)
    endpoint_file = tmp_path / "endpoint.txt"
    flags = ["--publish-endpoint", str(endpoint_file)] if publish else []
    process = subprocess.Popen(
        receive_command(*arguments, *flags),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(home),
        cwd=cwd or tmp_path,
    )
    try:
        yield RunningReceiver(process, read_start_line(process), endpoint_file)
    finally:
        if process.returncode is None:
            process.kill()
            process.communicate()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def assert_failed_before_serving(completed: subprocess.CompletedProcess[bytes], status: int):
    assert completed.stdout == b""
    assert completed.returncode == status


class TestFailuresBeforeServingPrintNoLine:
    def test_held_port_prints_no_line_and_exits_one(self, tmp_path):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            completed = run_to_end(["--port", str(holder.getsockname()[1])], tmp_path)
        assert_failed_before_serving(completed, 1)
        final = last_stderr_line(completed)
        assert final.startswith("OSError:")
        assert final.endswith("Address already in use")

    def test_unresolvable_host_prints_no_line_and_exits_one(self, tmp_path):
        completed = run_to_end(["--port", "0", "--host", "nonexistent.invalid"], tmp_path)
        assert_failed_before_serving(completed, 1)
        assert last_stderr_line(completed).startswith("socket.gaierror:")

    @pytest.mark.parametrize("port", ["70000", "-1"])
    def test_out_of_range_port_prints_no_line_and_exits_one(self, tmp_path, port):
        completed = run_to_end(["--port", port], tmp_path)
        assert_failed_before_serving(completed, 1)
        assert last_stderr_line(completed) == "OverflowError: bind(): port must be 0-65535."

    def test_host_that_is_not_this_machines_prints_no_line_and_exits_one(self, tmp_path):
        with socket.socket() as probe:
            try:
                probe.bind((FOREIGN_HOST, 0))
            except OSError:
                pass
            else:
                pytest.skip(f"this machine can bind {FOREIGN_HOST}")
        completed = run_to_end(["--port", "0", "--host", FOREIGN_HOST], tmp_path)
        assert_failed_before_serving(completed, 1)
        assert last_stderr_line(completed).startswith("OSError:")

    def test_unwritable_endpoint_parent_prints_no_line_and_exits_one(self, tmp_path):
        completed = run_to_end(
            ["--port", "0", "--publish-endpoint", "/nonexistent/x/f"], tmp_path
        )
        assert_failed_before_serving(completed, 1)
        assert last_stderr_line(completed).startswith("FileNotFoundError:")

    def test_endpoint_path_that_is_a_directory_prints_no_line_and_exits_one(self, tmp_path):
        target = tmp_path / "a-directory"
        target.mkdir()
        completed = run_to_end(["--port", "0", "--publish-endpoint", str(target)], tmp_path)
        assert_failed_before_serving(completed, 1)
        name = last_stderr_line(completed).split(":", 1)[0]
        raised = getattr(builtins, name, None)
        assert isinstance(raised, type) and issubclass(raised, OSError), name


class TestUsageErrorsStaySilentOnStdout:
    @staticmethod
    def assert_usage_error_ending(completed: subprocess.CompletedProcess[bytes]):
        assert_failed_before_serving(completed, 2)
        assert completed.stderr.startswith(b"usage:")
        lines = completed.stderr.decode(errors="replace").splitlines()
        assert lines[-2].endswith("the following arguments are required: --port")
        assert USAGE_NEXT_STEP in lines[-1]

    def test_missing_required_port_is_a_usage_error(self, tmp_path):
        self.assert_usage_error_ending(run_to_end([], tmp_path))

    def test_abbreviated_option_is_a_usage_error(self, tmp_path):
        self.assert_usage_error_ending(run_to_end(["--por", "0"], tmp_path))

    def test_non_integer_port_is_a_usage_error(self, tmp_path):
        completed = run_to_end(["--port", "abc"], tmp_path)
        assert_failed_before_serving(completed, 2)
        assert b"usage:" in completed.stderr
        assert re.search(rb"invalid int value: 'abc'", completed.stderr)


class TestStartLine:
    def test_line_is_readable_before_any_request_while_receiver_runs(self, tmp_path):
        directory = tmp_path / "data"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            line = receiving.line
            assert line.endswith("\n")
            assert receiving.process.poll() is None
            assert "127.0.0.1" in line
            assert receiving.port != 0
            assert str(directory) in line
            assert "logs.jsonl" in line
            assert "metrics.jsonl" in line
            receiving.stop()
        assert receiving.stderr == b""

    def test_line_port_equals_the_published_endpoint_port(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0", "--dir", str(tmp_path / "d")) as receiving:
            assert names_port(receiving.line, receiving.port)

    def test_line_without_publish_endpoint_still_names_a_non_zero_port(self, tmp_path):
        with running_receiver(
            tmp_path, "--port", "0", "--dir", str(tmp_path / "d"), publish=False
        ) as receiving:
            candidates = {
                int(token)
                for token in re.findall(r"(?<![\w.])\d+(?![\w.])", receiving.line)
                if 0 < int(token) < 65536
            }
            assert candidates, receiving.line
            assert any(accepts_connection(candidate) for candidate in candidates)

    def test_given_port_is_shown_as_given(self, tmp_path):
        port = free_port()
        with running_receiver(tmp_path, "--port", str(port), "--dir", str(tmp_path / "d")) as r:
            assert names_port(r.line, port)

    def test_default_directory_is_shown_resolved_under_home(self, tmp_path):
        home = tmp_path / "isolated-home"
        with running_receiver(tmp_path, "--port", "0", home=home) as receiving:
            assert str(home / ".ka-telemetry") in receiving.line
            assert "~" not in receiving.line

    def test_relative_directory_is_shown_under_the_start_directory(self, tmp_path):
        start = tmp_path / "start"
        start.mkdir()
        with running_receiver(tmp_path, "--port", "0", "--dir", "rel/data", cwd=start) as r:
            assert str(start / "rel" / "data") in r.line

    def test_home_relative_directory_is_expanded(self, tmp_path):
        home = tmp_path / "isolated-home"
        with running_receiver(tmp_path, "--port", "0", "--dir", "~/x", home=home) as receiving:
            assert str(home / "x") in receiving.line
            assert "~" not in receiving.line

    def test_absolute_directory_with_trailing_slash_is_shown_as_given(self, tmp_path):
        directory = tmp_path / "data"
        with running_receiver(tmp_path, "--port", "0", "--dir", f"{directory}/") as receiving:
            assert str(directory) in receiving.line

    def test_directory_with_spaces_and_non_ascii_is_shown_verbatim(self, tmp_path):
        directory = tmp_path / "data dir \u00e9\u00fc"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            assert str(directory) in receiving.line

    def test_directory_under_a_symlink_is_not_resolved(self, tmp_path):
        target = tmp_path / "realtarget"
        target.mkdir()
        link = tmp_path / "via-link"
        link.symlink_to(target)
        directory = link / "data"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            assert str(directory) in receiving.line
            assert "realtarget" not in receiving.line

    def test_home_that_is_a_symlink_is_not_resolved(self, tmp_path):
        target = tmp_path / "home-target"
        target.mkdir()
        home = tmp_path / "home-link"
        home.symlink_to(target)
        with running_receiver(tmp_path, "--port", "0", home=home) as receiving:
            assert str(home / ".ka-telemetry") in receiving.line
            assert "home-target" not in receiving.line

    def test_wildcard_host_is_shown_exactly_as_typed(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0", "--host", "0.0.0.0") as receiving:
            assert "0.0.0.0" in receiving.line
            assert "127.0.0.1" not in receiving.line

    def test_named_host_is_shown_exactly_as_typed(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0", "--host", "localhost") as receiving:
            assert "localhost" in receiving.line

    def test_line_holds_no_escape_byte(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0") as receiving:
            receiving.stop()
        assert b"\x1b" not in receiving.stdout

    def test_line_associates_both_filenames_with_the_directory(self, tmp_path):
        directory = tmp_path / "data"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            assert "logs.jsonl" in receiving.line
            assert "metrics.jsonl" in receiving.line
            assert str(directory) in receiving.line


class TestDirectoryIsCreatedByTheFirstRecordOnly:
    def test_directory_is_not_created_by_the_line(self, tmp_path):
        directory = tmp_path / "data" / "new"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            assert str(directory) in receiving.line
            time.sleep(1.5)
            assert not directory.exists()

    @pytest.mark.parametrize("signal_name", ["logs", "metrics"])
    def test_directory_and_file_appear_after_first_post(self, tmp_path, signal_name):
        directory = tmp_path / "data" / "new"
        with running_receiver(tmp_path, "--port", "0", "--dir", str(directory)) as receiving:
            assert post(receiving.port, f"/v1/{signal_name}", b'{"n": 1}') == 200
        assert (directory / f"{signal_name}.jsonl").is_file()

    @pytest.mark.parametrize("form", ["default", "relative", "home-relative"])
    def test_records_land_in_the_directory_the_line_names(self, tmp_path, form):
        home = tmp_path / "isolated-home"
        start = tmp_path / "start"
        start.mkdir()
        arguments = {
            "default": [],
            "relative": ["--dir", "rel/data"],
            "home-relative": ["--dir", "~/x"],
        }[form]
        expected = {
            "default": home / ".ka-telemetry",
            "relative": start / "rel" / "data",
            "home-relative": home / "x",
        }[form]
        with running_receiver(
            tmp_path, "--port", "0", *arguments, home=home, cwd=start
        ) as receiving:
            assert str(expected) in receiving.line
            assert post(receiving.port, "/v1/logs", b'{"n": 1}') == 200
            assert post(receiving.port, "/v1/metrics", b'{"n": 1}') == 200
        assert sorted(path.name for path in expected.iterdir()) == ["logs.jsonl", "metrics.jsonl"]


class TestStartLineStaysOneLine:
    def test_exactly_one_line_across_accepted_refused_and_malformed_requests(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0", "--dir", str(tmp_path / "d")) as receiving:
            port = receiving.port
            assert post(port, "/v1/logs", b'{"n": 1}') == 200
            assert post(port, "/v1/metrics", b'{"n": 1}') == 200
            assert post(port, "/v1/traces", b'{"n": 1}') == 404
            assert get(port, "/v1/logs") == 405
            assert post(port, "/v1/logs", b"not json") == 400
            receiving.stop()
        assert len(receiving.stdout.splitlines()) == 1
        assert receiving.stdout.endswith(b"\n")
        assert receiving.stderr == b""

    def test_sigterm_ends_with_empty_stderr_and_by_the_signal(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0") as receiving:
            status = receiving.stop(signal.SIGTERM)
        assert receiving.stderr == b""
        assert status == -signal.SIGTERM

    def test_sigint_ends_with_keyboard_interrupt_and_only_the_one_stdout_line(self, tmp_path):
        with running_receiver(tmp_path, "--port", "0") as receiving:
            status = receiving.stop(signal.SIGINT)
        assert status == -signal.SIGINT
        assert receiving.stdout_after_line == b""
        assert len(receiving.stdout.splitlines()) == 1
        assert receiving.stderr.decode(errors="replace").splitlines()[-1] == "KeyboardInterrupt"


class TestLibraryPathStaysSilent:
    def test_build_receiver_alone_prints_nothing(self, tmp_path, capfd):
        server = receiver.build_receiver(0, str(tmp_path / "data"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert post(server.server_port, "/v1/logs", b'{"n": 1}') == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        captured = capfd.readouterr()
        assert (captured.out, captured.err) == ("", "")


def help_text(tmp_path: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[bytes], str]:
    """`receive ... --help` and its stdout with the line breaks undone. The terminal is made wide
    so the parser does not break a word at a hyphen, which no joining of lines could repair."""
    completed = run_to_end([*arguments, "--help"], tmp_path, env_extra={"COLUMNS": "1000"})
    return completed, " ".join(completed.stdout.decode().split())


class TestReceiveHelp:
    def test_help_no_longer_claims_silence(self, tmp_path):
        completed, text = help_text(tmp_path)
        assert completed.returncode == 0
        assert completed.stderr == b""
        assert "It prints nothing while it runs" not in text

    def test_help_says_what_the_start_line_holds(self, tmp_path):
        _, text = help_text(tmp_path)
        text = text.lower()
        for words in (
            "once it is listening",
            "prints one line",
            "address",
            "port",
            "directory",
            "logs.jsonl",
            "metrics.jsonl",
        ):
            assert words in text, words

    def test_help_lists_exactly_the_same_options(self, tmp_path):
        completed, _ = help_text(tmp_path)
        lines = completed.stdout.decode().splitlines()
        assert lines[0] == (
            "usage: ka-telemetry receive [-h] --port PORT [--dir DIR] [--host HOST] "
            "[--publish-endpoint PATH]"
        )
        listed = [line.strip().split("  ")[0] for line in lines if line.startswith("  -")]
        assert listed == [
            "-h, --help",
            "--port PORT",
            "--dir DIR",
            "--host HOST",
            "--publish-endpoint PATH",
        ]

    def test_help_keeps_every_other_statement(self, tmp_path):
        _, text = help_text(tmp_path)
        for statement in (
            "/v1/logs and /v1/metrics",
            "appends each record as one line to logs.jsonl or metrics.jsonl",
            "removes every user.email attribute from what it received",
            "it changes nothing else in it",
            "serves until you stop it",
            "Deleting logs.jsonl or metrics.jsonl removes the records that file holds",
            "never requires stopping the receiver",
            "never loses or corrupts a record being written at the time",
            "0 lets the system choose a free port, and --publish-endpoint records which one",
            "(default: ~/.ka-telemetry)",
            "asks for no authentication",
        ):
            assert statement in text, statement

    def test_help_prints_no_start_line_and_creates_nothing(self, tmp_path):
        endpoint_file = tmp_path / "endpoint.txt"
        directory = tmp_path / "data"
        completed, text = help_text(
            tmp_path,
            "--port",
            "0",
            "--dir",
            str(directory),
            "--publish-endpoint",
            str(endpoint_file),
        )
        assert completed.stdout.startswith(b"usage:")
        assert "The receiver is listening" not in text
        assert not endpoint_file.exists()
        assert not directory.exists()
