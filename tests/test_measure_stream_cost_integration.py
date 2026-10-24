"""The cost-measurement script against the real reports and real child processes, at small sizes.

Each case that must watch a process from outside runs the script's code in a fresh interpreter (a
"driver", started with `-I` so nothing inherited changes what it loads) and reads back what the
driver wrote. Two reasons: the Linux figure for a child's peak memory starts from the resident size
of the process that started it, so a measurement is only meaningful from a small parent, and a
driver can be sent a signal without ending the test run. The full default workload (1 GB) is never
run here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ka_telemetry.stream_usage import read_metric_lines
from tools import measure_stream_cost as cost

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PRODUCT_ROOT / "tools" / "measure_stream_cost.py"
SMALL_REQUEST = 40_000
FIRST_LINE = re.compile(r"^(\d+) agent row\(s\), 0 unreadable line\(s\) skipped\.$")

# Loads the script by path in a fresh interpreter, as `python3 tools/measure_stream_cost.py`
# would find it, and records every file the interpreter opens while it generates one pair.
GENERATION_DRIVER = """
import importlib.util
import json
import sys
from pathlib import Path

script, base, requested, evidence = sys.argv[1:5]
opened = []
sys.addaudithook(lambda event, args: opened.append(str(args[0])) if event == "open" else None)
spec = importlib.util.spec_from_file_location("measure_stream_cost", script)
module = importlib.util.module_from_spec(spec)
sys.modules["measure_stream_cost"] = module
spec.loader.exec_module(module)
storage = module.TemporaryStorage(parent=Path(base))
directory = Path(base) / "pair"
storage.make_directory(directory)
module.generate_pair(directory, int(requested), storage=storage)
recorded = list(opened)
Path(evidence).write_text(json.dumps(recorded), encoding="utf-8")
"""


def generate(base: Path, name: str, requested_bytes: int = SMALL_REQUEST) -> Path:
    storage = cost.TemporaryStorage(parent=base)
    directory = base / name
    storage.make_directory(directory)
    cost.generate_pair(directory, requested_bytes, storage=storage)
    return directory


def run_report(report: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ka_telemetry", report, "--dir", str(directory)],
        cwd=PRODUCT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestGeneratedPairIsReadAsData:
    def test_two_independent_generations_are_byte_identical(self, tmp_path):
        first = generate(tmp_path, "first")
        second = generate(tmp_path, "second")

        for name in ("logs.jsonl", "metrics.jsonl"):
            assert (first / name).read_bytes() == (second / name).read_bytes()

    def test_agents_reads_generated_pair_with_no_skipped_lines(self, tmp_path):
        directory = generate(tmp_path, "pair")

        result = run_report("agents", directory)

        assert result.returncode == 0, result.stderr
        first_line = result.stdout.splitlines()[0]
        matched = FIRST_LINE.match(first_line)
        assert matched, first_line
        assert int(matched.group(1)) >= 1
        lines = result.stdout.splitlines()
        assert not any(line.startswith("Unreadable lines in") for line in lines)

    def test_check_reads_generated_pair_and_prints_session_line(self, tmp_path):
        directory = generate(tmp_path, "pair")

        result = run_report("check", directory)

        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert any(line.startswith("  session=") for line in lines)
        assert not any(line.startswith("Unreadable lines in") for line in lines)

    def test_metric_points_are_numeric_and_nonempty(self, tmp_path):
        directory = generate(tmp_path, "pair")
        raw_lines = (directory / "metrics.jsonl").read_text(encoding="utf-8").splitlines()

        points = read_metric_lines(raw_lines)

        assert points, "the metrics file holds no cost.usage point"
        assert all(point.value > 0 for point in points)
        for line in raw_lines:
            for resource in json.loads(line)["body"]["resourceMetrics"]:
                for scope in resource["scopeMetrics"]:
                    for metric in scope["metrics"]:
                        assert metric["name"] == "claude_code.cost.usage"
                        for point in metric["sum"]["dataPoints"]:
                            assert isinstance(point["asDouble"], float)


class TestGenerationTouchesNoRealStream:
    def test_generation_reads_no_real_stream_path(self, tmp_path):
        home = tmp_path / "home"
        for stream_directory in (home / ".ka-telemetry", home / ".kensaiapp" / "telemetry"):
            stream_directory.mkdir(parents=True)
            (stream_directory / "logs.jsonl").write_text("{}\n", encoding="utf-8")
            (stream_directory / "metrics.jsonl").write_text("{}\n", encoding="utf-8")
        evidence = tmp_path / "evidence.json"

        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                GENERATION_DRIVER,
                str(SCRIPT),
                str(tmp_path / "work"),
                str(SMALL_REQUEST),
                str(evidence),
            ],
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        opened = json.loads(evidence.read_text(encoding="utf-8"))

        assert any(path.endswith("logs.jsonl") for path in opened), "the watch saw no open"
        for path in opened:
            assert not path.startswith(str(home)), path
            assert ".ka-telemetry" not in path and ".kensaiapp" not in path, path


# ── The measurement adapter with real children ───────────────────────────────

LOAD_SCRIPT = """
import importlib.util
import json
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("measure_stream_cost", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["measure_stream_cost"] = module
spec.loader.exec_module(module)
plan = json.loads(sys.argv[2])
"""

MEASURE_DRIVER = LOAD_SCRIPT + """
processes = module.ChildProcesses()
results = []
for directory in plan["directories"]:
    run = module.measure_report(
        plan["report"],
        Path(directory),
        checkout_root=Path(plan["checkout"]),
        output_path=Path(plan["output"]),
        processes=processes,
        clock=time.perf_counter,
    )
    results.append(
        [run.status, run.peak_resident_bytes, run.elapsed_seconds, run.signal_number]
    )
print(json.dumps(results))
"""

INTERRUPT_DRIVER = LOAD_SCRIPT + """
import os

try:
    module.measure_report(
        "agents",
        Path(plan["directory"]),
        checkout_root=Path(plan["checkout"]),
        output_path=Path(plan["output"]),
        processes=module.ChildProcesses(),
        clock=time.perf_counter,
    )
except KeyboardInterrupt:
    try:
        os.waitpid(-1, os.WNOHANG)
        reaped = False
    except ChildProcessError:
        reaped = True
    print(json.dumps({"interrupted": True, "reaped": reaped,
                      "output_kept": Path(plan["output"]).exists()}))
"""

ALLOCATE_THEN_EXIT = """
import sys
from pathlib import Path

directory = Path(sys.argv[sys.argv.index("--dir") + 1])
held = b"x" * (int((directory / "megabytes").read_text()) * 1_000_000)
print("REPORT-OUTPUT-MARKER", len(held))
"""

EXIT_WITH_STATUS_3 = """
import sys
print("REPORT-OUTPUT-MARKER")
sys.exit(3)
"""

KILL_ITSELF = """
import os
import signal

os.kill(os.getpid(), signal.SIGKILL)
"""

SIGNAL_THE_PARENT_THEN_WAIT = """
import os
import signal
import sys
import time
from pathlib import Path

directory = Path(sys.argv[sys.argv.index("--dir") + 1])
(directory / "child.pid").write_text(str(os.getpid()))
os.kill(os.getppid(), signal.SIGINT)
time.sleep(60)
"""

# A stand-in checkout whose `ka_telemetry` package watches the process before it hands over to the
# real package: it records every file the process opens and every network call it begins, to a
# file, and then runs the real `__init__`, so `python -m ka_telemetry` still reaches the real
# report entry point.
WATCHING_PACKAGE = """
import os
import sys

_EVIDENCE = {evidence!r}
_recording = False


def _record(kind, detail):
    global _recording
    if _recording:
        return
    _recording = True
    try:
        descriptor = os.open(_EVIDENCE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (kind + " " + detail + "\\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    finally:
        _recording = False


def _watch(event, args):
    if event == "open":
        _record("open", str(args[0]))
    elif event.startswith("socket."):
        _record("net", event)


sys.addaudithook(_watch)
__path__ = [{real!r}]
exec(compile(open({real_init!r}, encoding="utf-8").read(), {real_init!r}, "exec"))
"""


def run_driver(source: str, script: Path, plan: dict, *, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-I", "-c", source, str(script), json.dumps(plan)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def make_stand_in(base: Path, main_source: str) -> Path:
    """A checkout whose `python -m ka_telemetry` runs `main_source` instead of a report."""
    package = base / "stand-in" / "ka_telemetry"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(textwrap.dedent(main_source), encoding="utf-8")
    return package.parent


def make_watched_checkout(base: Path, evidence: Path) -> Path:
    package = base / "watched" / "ka_telemetry"
    package.mkdir(parents=True)
    real = PRODUCT_ROOT / "ka_telemetry"
    source = WATCHING_PACKAGE.format(
        evidence=str(evidence), real=str(real), real_init=str(real / "__init__.py")
    )
    (package / "__init__.py").write_text(source, encoding="utf-8")
    return package.parent


def measure_in_driver(checkout: Path, directories: list[Path], output: Path, *, report="agents",
                      env=None):
    plan = {
        "report": report,
        "checkout": str(checkout),
        "output": str(output),
        "directories": [str(directory) for directory in directories],
    }
    completed = run_driver(MEASURE_DRIVER, SCRIPT, plan, env=env)
    assert completed.returncode == 0, completed.stderr
    return completed, json.loads(completed.stdout)


class TestOwnPeakAndExit:
    def test_second_child_reports_its_own_peak_not_the_first_childs(self, tmp_path):
        checkout = make_stand_in(tmp_path, ALLOCATE_THEN_EXIT)
        large, small = tmp_path / "large", tmp_path / "small"
        for directory, megabytes in ((large, 400), (small, 10)):
            directory.mkdir()
            (directory / "megabytes").write_text(str(megabytes), encoding="utf-8")

        _completed, ((_s1, first_peak, _t1, _g1), (_s2, second_peak, _t2, _g2)) = measure_in_driver(
            checkout, [large, small], tmp_path / "report.out"
        )

        assert first_peak > 400 * 1_000_000
        assert second_peak < first_peak / 2

    def test_a_larger_child_reports_a_larger_peak_than_a_smaller_one(self, tmp_path):
        checkout = make_stand_in(tmp_path, ALLOCATE_THEN_EXIT)
        small, large = tmp_path / "small", tmp_path / "large"
        for directory, megabytes in ((small, 10), (large, 300)):
            directory.mkdir()
            (directory / "megabytes").write_text(str(megabytes), encoding="utf-8")

        _completed, ((_s1, small_peak, _t1, _g1), (_s2, large_peak, _t2, _g2)) = measure_in_driver(
            checkout, [small, large], tmp_path / "report.out"
        )

        assert large_peak - small_peak > 200 * 1_000_000

    def test_nonzero_exit_status_is_returned_unchanged_by_a_real_child(self, tmp_path):
        checkout = make_stand_in(tmp_path, EXIT_WITH_STATUS_3)
        directory = tmp_path / "pair"
        directory.mkdir()

        _completed, ((status, _peak, _elapsed, signal_number),) = measure_in_driver(
            checkout, [directory], tmp_path / "report.out"
        )

        assert status == 3
        assert signal_number is None

    def test_a_real_child_killed_by_a_signal_gives_a_nonzero_status(self, tmp_path):
        checkout = make_stand_in(tmp_path, KILL_ITSELF)
        directory = tmp_path / "pair"
        directory.mkdir()

        _completed, ((status, _peak, _elapsed, signal_number),) = measure_in_driver(
            checkout, [directory], tmp_path / "report.out"
        )

        assert status != 0
        assert signal_number == signal.SIGKILL

    def test_report_stdout_does_not_reach_the_scripts_stdout(self, tmp_path):
        checkout = make_stand_in(tmp_path, EXIT_WITH_STATUS_3)
        directory = tmp_path / "pair"
        directory.mkdir()
        output = tmp_path / "report.out"

        completed, _results = measure_in_driver(checkout, [directory], output)

        assert "REPORT-OUTPUT-MARKER" not in completed.stdout
        assert "REPORT-OUTPUT-MARKER" in output.read_text(encoding="utf-8")


class TestWhatTheChildMayReach:
    def test_installed_ka_telemetry_on_path_is_never_started(self, tmp_path):
        sentinel_directory = tmp_path / "sentinel-bin"
        sentinel_directory.mkdir()
        started = tmp_path / "sentinel-was-started"
        sentinel = sentinel_directory / "ka-telemetry"
        sentinel.write_text(f"#!/bin/sh\ntouch {started}\nexit 9\n", encoding="utf-8")
        sentinel.chmod(0o755)
        directory = generate(tmp_path, "pair")

        _completed, ((status, _peak, _elapsed, _signal),) = measure_in_driver(
            PRODUCT_ROOT,
            [directory],
            tmp_path / "report.out",
            env={"PATH": f"{sentinel_directory}{os.pathsep}{os.environ['PATH']}"},
        )

        assert status == 0
        assert not started.exists()

    def test_default_stream_directories_are_never_read(self, tmp_path):
        home = tmp_path / "home"
        for stream_directory in (home / ".ka-telemetry", home / ".kensaiapp" / "telemetry"):
            stream_directory.mkdir(parents=True)
            for name in ("logs.jsonl", "metrics.jsonl"):
                (stream_directory / name).write_text("{}\n", encoding="utf-8")
        directory = generate(tmp_path, "pair")
        evidence = tmp_path / "evidence.txt"
        checkout = make_watched_checkout(tmp_path, evidence)

        _completed, ((status, _peak, _elapsed, _signal),) = measure_in_driver(
            checkout, [directory], tmp_path / "with-streams.out", env={"HOME": str(home)}
        )
        bare_home = tmp_path / "bare-home"
        bare_home.mkdir()
        measure_in_driver(
            checkout, [directory], tmp_path / "without-streams.out", env={"HOME": str(bare_home)}
        )

        assert status == 0
        opened = [
            line[len("open "):] for line in evidence.read_text(encoding="utf-8").splitlines()
            if line.startswith("open ")
        ]
        assert any(path.endswith("logs.jsonl") and str(directory) in path for path in opened), (
            "the watch saw no open of the generated pair"
        )
        for path in opened:
            assert not path.startswith(str(home)), path
            assert ".ka-telemetry" not in path and ".kensaiapp" not in path, path
        assert (tmp_path / "with-streams.out").read_bytes() == (
            tmp_path / "without-streams.out"
        ).read_bytes()

    def test_no_network_connection_is_attempted_by_the_child(self, tmp_path):
        directory = generate(tmp_path, "pair")
        evidence = tmp_path / "evidence.txt"
        checkout = make_watched_checkout(tmp_path, evidence)

        _completed, ((status, _peak, _elapsed, _signal),) = measure_in_driver(
            checkout, [directory], tmp_path / "report.out"
        )

        assert status == 0
        assert evidence.exists(), "the watch recorded nothing at all"
        assert not [
            line for line in evidence.read_text(encoding="utf-8").splitlines()
            if line.startswith("net ")
        ]

    def test_the_watch_records_a_network_call_when_one_is_made(self, tmp_path):
        evidence = tmp_path / "evidence.txt"
        checkout = make_watched_checkout(tmp_path, evidence)
        attempt = (
            "import socket, ka_telemetry\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n    s.connect('/nonexistent/measure-stream-cost')\n"
            "except OSError:\n    pass\n"
        )

        subprocess.run(
            [sys.executable, "-E", "-S", "-B", "-c", attempt], cwd=checkout, check=True
        )

        assert "net socket.connect" in evidence.read_text(encoding="utf-8")


class TestInterruption:
    def test_interrupt_terminates_and_reaps_the_owned_child(self, tmp_path):
        checkout = make_stand_in(tmp_path, SIGNAL_THE_PARENT_THEN_WAIT)
        directory = tmp_path / "pair"
        directory.mkdir()
        plan = {
            "directory": str(directory),
            "checkout": str(checkout),
            "output": str(tmp_path / "report.out"),
        }

        completed = run_driver(INTERRUPT_DRIVER, SCRIPT, plan)

        assert completed.returncode == 0, completed.stderr
        outcome = json.loads(completed.stdout)
        assert outcome == {"interrupted": True, "reaped": True, "output_kept": True}
        child_pid = int((directory / "child.pid").read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


# ── Interruption and a clean checkout, through the whole orchestration ───────

ORCHESTRATION_DRIVER = LOAD_SCRIPT + """
import functools
import os
import signal

parent = Path(plan["parent"])
storage = module.TemporaryStorage(parent=parent)
mode = plan["mode"]
real_measure = functools.partial(
    module.measure_report,
    checkout_root=Path(plan["checkout"]),
    processes=module.ChildProcesses(),
    clock=time.perf_counter,
)


def interrupt():
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(10)


def generate(directory, requested_bytes):
    if mode == "during_generation":
        (directory / "logs.jsonl").write_text("partial", encoding="utf-8")
        interrupt()
    module.generate_pair(directory, requested_bytes, storage=storage)


def measure(report, directory, *, output_path):
    if mode == "before_report":
        interrupt()
    if mode == "failure":
        return module.ReportRun(status=1, peak_resident_bytes=0, elapsed_seconds=0.0)
    return real_measure(report, directory, output_path=output_path)


sys.exit(
    module.status_of(
        lambda: module.run_measurements(
            sizes=tuple(plan["sizes"]),
            generate=generate,
            measure=measure,
            storage=storage,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
)
"""


def run_orchestration(mode: str, checkout: Path, parent: Path, *, env=None):
    plan = {"mode": mode, "checkout": str(checkout), "parent": str(parent),
            "sizes": [SMALL_REQUEST, 2 * SMALL_REQUEST]}
    parent.mkdir(exist_ok=True)
    return run_driver(ORCHESTRATION_DRIVER, SCRIPT, plan, env=env)


class TestInterruptedOrchestration:
    @pytest.mark.parametrize("mode", ["before_report", "during_generation", "while_child_runs"])
    def test_interrupt_exits_130_and_removes_data_at_each_point(self, tmp_path, mode):
        pid_file = tmp_path / "child.pid"
        checkout = make_stand_in(
            tmp_path,
            SIGNAL_THE_PARENT_THEN_WAIT.replace(
                '(directory / "child.pid")', f"Path({str(pid_file)!r})"
            ),
        )
        parent = tmp_path / "scratch"

        completed = run_orchestration(mode, checkout, parent)

        assert completed.returncode == 130, completed.stderr
        assert completed.stdout == ""
        assert list(parent.iterdir()) == [], "the data was left behind"
        if mode == "while_child_runs":
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), 0)


class TestCheckoutStaysClean:
    def make_clone(self, base: Path) -> Path:
        clone = base / "clone"
        (clone / "tools").mkdir(parents=True)
        shutil.copytree(
            PRODUCT_ROOT / "ka_telemetry", clone / "ka_telemetry",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copy(SCRIPT, clone / "tools" / "measure_stream_cost.py")
        shutil.copy(PRODUCT_ROOT / ".gitignore", clone / ".gitignore")
        git = [
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(clone),
        ]
        subprocess.run([*git, "init", "--quiet"], check=True)
        subprocess.run([*git, "add", "--all"], check=True)
        subprocess.run([*git, "commit", "--quiet", "-m", "base"], check=True)
        return clone

    def status_of(self, clone: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout

    def test_checkout_stays_clean_after_a_run(self, tmp_path):
        clone = self.make_clone(tmp_path)
        script_in_clone = clone / "tools" / "measure_stream_cost.py"
        for mode, expected_status in (("success", 0), ("failure", 1), ("before_report", 130)):
            plan = {"mode": mode, "checkout": str(clone), "parent": str(tmp_path / mode),
                    "sizes": [SMALL_REQUEST, 2 * SMALL_REQUEST]}
            (tmp_path / mode).mkdir()

            completed = run_driver(ORCHESTRATION_DRIVER, script_in_clone, plan)

            assert completed.returncode == expected_status, (mode, completed.stderr)
            assert self.status_of(clone) == "", mode
            assert list((tmp_path / mode).iterdir()) == [], mode

    def test_a_successful_small_run_prints_two_lines_per_size(self, tmp_path):
        clone = self.make_clone(tmp_path)
        script_in_clone = clone / "tools" / "measure_stream_cost.py"
        plan = {"mode": "success", "checkout": str(clone), "parent": str(tmp_path / "scratch"),
                "sizes": [SMALL_REQUEST, 2 * SMALL_REQUEST]}
        (tmp_path / "scratch").mkdir()

        completed = run_driver(ORCHESTRATION_DRIVER, script_in_clone, plan)

        assert completed.returncode == 0, completed.stderr
        lines = completed.stdout.splitlines()
        assert len(lines) == 4
        assert [line.split(" ")[0] for line in lines] == ["agents", "check", "agents", "check"]


# ── Added by the code-integrity review: real children through the whole orchestration ──────────

RESULT_LINE = re.compile(
    r"^(agents|check) on (\d+ (?:MB|GB)) of stream data: "
    r"peak memory (\d+\.\d) MB, time (\d+\.\d) s$"
)
NEXT_STEP_AFTER_REPORT_FAILURE = (
    "If {command} wrote an error above this line, resolve it, then run "
    "python3 tools/measure_stream_cost.py again."
)

WRITE_TO_STDERR_THEN_EXIT_3 = """
import sys

print("REPORT-ERROR-MARKER", file=sys.stderr)
sys.exit(3)
"""

KILL_ITSELF_WITH = """
import os
import signal

os.kill(os.getpid(), signal.{name})
"""


class TestRealReportsThroughTheOrchestration:
    def test_a_real_small_run_prints_only_result_lines_on_stdout(self, tmp_path):
        clone = TestCheckoutStaysClean().make_clone(tmp_path)
        plan = {"mode": "success", "checkout": str(clone), "parent": str(tmp_path / "scratch"),
                "sizes": [SMALL_REQUEST, 2 * SMALL_REQUEST]}
        (tmp_path / "scratch").mkdir()

        completed = run_driver(
            ORCHESTRATION_DRIVER, clone / "tools" / "measure_stream_cost.py", plan
        )

        assert completed.returncode == 0, completed.stderr
        lines = completed.stdout.splitlines()
        assert [RESULT_LINE.match(line) is not None for line in lines] == [True] * 4, lines
        assert completed.stdout.endswith("\n")

    def test_report_standard_error_reaches_the_scripts_standard_error_above_the_failure(
        self, tmp_path
    ):
        checkout = make_stand_in(tmp_path, WRITE_TO_STDERR_THEN_EXIT_3)
        parent = tmp_path / "scratch"

        completed = run_orchestration("success", checkout, parent)

        size = cost.size_label(SMALL_REQUEST)
        assert completed.returncode == 1, completed.stderr
        assert completed.stdout == ""
        assert completed.stderr.splitlines() == [
            "REPORT-ERROR-MARKER",
            f"agents on {size} of stream data exited with status 3; "
            "no figure was printed for it.",
            NEXT_STEP_AFTER_REPORT_FAILURE.format(command="agents"),
        ]
        assert list(parent.iterdir()) == []

    @pytest.mark.parametrize("name", ["SIGKILL", "SIGINT"])
    def test_a_real_report_ended_by_a_signal_is_a_failed_run_not_an_interruption(
        self, tmp_path, name
    ):
        checkout = make_stand_in(tmp_path, KILL_ITSELF_WITH.format(name=name))
        parent = tmp_path / "scratch"

        completed = run_orchestration("success", checkout, parent)

        size = cost.size_label(SMALL_REQUEST)
        assert completed.returncode == 1, completed.stderr
        assert completed.stdout == ""
        assert completed.stderr.splitlines()[-2:] == [
            f"agents on {size} of stream data was ended by signal {name}; "
            "no figure was printed for it.",
            NEXT_STEP_AFTER_REPORT_FAILURE.format(command="agents"),
        ]
        assert list(parent.iterdir()) == []
