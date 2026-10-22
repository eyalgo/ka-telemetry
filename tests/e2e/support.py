"""What the end-to-end tests of the installed `ka-telemetry` command share.

Every test here drives the command from the scratch virtualenv the `installed` fixture builds
(`conftest.py`), never the checkout, and gives each child process its own `HOME`, so nothing a
test starts can read or write the real home directory.
"""

from __future__ import annotations

import difflib
import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import InstalledProduct

PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_SET = PRODUCT_ROOT / "tests" / "stream_parity"
STARTUP_SECONDS = 30
README_BASE_COMMIT = "f2b7a6e5a89f3faea60b8c1a0a0a855f720d5eb0"
# The recorded data sets name neutral directories and labels in place of the machine they were
# recorded on. A test that holds a file to an older commit maps that commit's paths and bytes
# through these first, so the renaming is the only change it allows. The recorded names are kept
# only in the private repository, ka-telemetry-dev, the public one is cut from, beside the history those tests read.
RECORDED_NAMES = PRODUCT_ROOT / "tests" / "recorded_names.json"


def needs_history() -> None:
    """Skip the calling test in the public cut, which has none of the older commits it reads."""
    if not RECORDED_NAMES.is_file():
        pytest.skip("no recorded names: this copy is the public cut, with no older commits to read")


def neutral(text: str) -> str:
    """`text` with each recorded name replaced by its neutral one."""
    needs_history()
    for recorded, shown in json.loads(RECORDED_NAMES.read_text(encoding="utf-8")):
        text = text.replace(recorded, shown)
    return text


class BaselineMissing(AssertionError):
    """The commit a README comparison is anchored at is not in this repository's history."""


def _commits_in_history(repo: Path) -> int:
    counted = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(counted.stdout) if counted.returncode == 0 else 0


def base_readme(commit: str = README_BASE_COMMIT, repo: Path = PRODUCT_ROOT) -> str:
    """The README as `commit` holds it in `repo`.

    A commit missing from a repository that has history fails: a comparison that cannot run proves
    nothing about the text it was meant to protect, and a skip would hide that. A repository with
    one commit or none is a copy, such as the ones the release check and its tests make of this
    checkout, and has no past to compare with; there the comparison is skipped by name.
    """
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:README.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode == 0:
        return done.stdout
    if _commits_in_history(repo) < 2:
        pytest.skip(f"{repo} is a copy with no history, so {commit} cannot be compared with")
    reason = done.stderr.strip()
    raise BaselineMissing(f"{commit} is not in this repository's history: {reason}")


def child_env(home: Path) -> dict[str, str]:
    """The environment of a child process: this one's, with `HOME` replaced."""
    return {**os.environ, "HOME": str(home)}


def run(
    installed: InstalledProduct, *arguments: str, home: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """One `ka-telemetry` invocation to completion, under its own `HOME`."""
    return subprocess.run(
        [str(installed.command), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(home),
        cwd=cwd,
    )


def api_request_body(session_id: str, cost: float) -> dict:
    """The smallest OTLP/JSON logs body the reader counts as one request: an `api_request`."""

    def attribute(key: str, value: dict) -> dict:
        return {"key": key, "value": value}

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "attributes": [
                                    attribute("session.id", {"stringValue": session_id}),
                                    attribute("event.name", {"stringValue": "api_request"}),
                                    attribute("model", {"stringValue": "claude-model"}),
                                    attribute("input_tokens", {"intValue": 3}),
                                    attribute("output_tokens", {"intValue": 4}),
                                    attribute("cache_read_tokens", {"intValue": 5}),
                                    attribute("cache_creation_tokens", {"intValue": 6}),
                                    attribute("cost_usd", {"doubleValue": cost}),
                                    attribute("duration_ms", {"intValue": 7}),
                                    attribute("query_source", {"stringValue": "sdk"}),
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    }


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Receiver:
    """The installed `ka-telemetry receive`, running as a child process until the block ends.

    With `publish=True` (the default) it is started on port 0 with `--publish-endpoint`, and the
    port is read back from the file it writes — the one way a person learns which port the system
    chose. With `publish=False` it is started on `port` with no endpoint file, and is ready once
    that port accepts a connection.
    """

    def __init__(
        self,
        installed: InstalledProduct,
        home: Path,
        *arguments: str,
        scratch: Path,
        publish: bool = True,
        port: int = 0,
    ) -> None:
        self._installed = installed
        self._home = home
        self._arguments = arguments
        self._publish = publish
        self.endpoint_file = scratch / "endpoint.txt"
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.stdout = ""
        self.stderr = ""

    def __enter__(self) -> Receiver:
        # A receiver started earlier with the same scratch directory left its endpoint behind.
        self.endpoint_file.unlink(missing_ok=True)
        flags = ["--publish-endpoint", str(self.endpoint_file)] if self._publish else []
        self.process = subprocess.Popen(
            [
                str(self._installed.command), "receive", "--port", str(self.port),
                *flags, *self._arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env(self._home),
        )
        self._wait_until_listening()
        return self

    def __exit__(self, *exc_info) -> None:
        assert self.process is not None
        self.process.terminate()
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate()

    def _wait_until_listening(self) -> None:
        assert self.process is not None
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                _, stderr = self.process.communicate()
                raise AssertionError(f"the receiver exited at start: {stderr}")
            if self._publish:
                text = self.endpoint_file.read_text() if self.endpoint_file.exists() else ""
                if text.endswith("\n"):
                    self.port = int(text.rsplit(":", 1)[1])
                    return
            elif self._accepts_connections():
                return
            time.sleep(0.05)
        raise AssertionError("the receiver never started listening")

    def _accepts_connections(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def post(self, signal: str, body: dict) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", f"/v1/{signal}", json.dumps(body), {"Content-Type": "application/json"}
            )
            return connection.getresponse().status
        finally:
            connection.close()


def diff(expected: str, actual: str) -> str:
    """A unified diff that shows every changed character of every changed line — enough to expose
    a cost that differs in the sixth decimal."""
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="stored oracle",
            tofile="installed command",
            n=0,
        )
    )
