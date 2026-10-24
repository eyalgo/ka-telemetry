"""The cost-measurement script, checked at small sizes with the collaborators handed in.

`tools/measure_stream_cost.py` makes stream data, runs the two reports on it as child processes and
prints what each run cost. These tests never run the default workload: sizes here are bytes or
kilobytes, and the pieces that touch the machine (the child processes, the temporary storage) are
replaced by recording or failing stand-ins wherever the case is about the script's own decisions.
Cases that need a real child process or the real reports are in
`test_measure_stream_cost_integration.py`.
"""

from __future__ import annotations

import errno
import functools
import inspect
import io
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest

from tools import measure_stream_cost as cost

SMALL_REQUEST = 20_000


def generate(base: Path, name: str, requested_bytes: int) -> Path:
    """Generate one pair of `requested_bytes` into `base/name` through real storage."""
    storage = cost.TemporaryStorage(parent=base)
    directory = base / name
    storage.make_directory(directory)
    cost.generate_pair(directory, requested_bytes, storage=storage)
    return directory


def lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


class TestDefaultConfiguration:
    def test_default_sizes_are_three_ascending_with_largest_one_gigabyte(self):
        assert cost.DEFAULT_SIZES_BYTES == (100_000_000, 300_000_000, 1_000_000_000)


class TestPairGenerator:
    def test_pair_meets_requested_combined_size(self, tmp_path):
        directory = generate(tmp_path, "pair", SMALL_REQUEST)

        combined = sum(
            (directory / name).stat().st_size for name in ("logs.jsonl", "metrics.jsonl")
        )

        assert combined >= SMALL_REQUEST

    def test_each_file_receives_at_least_half_the_request(self, tmp_path):
        directory = generate(tmp_path, "pair", SMALL_REQUEST + 1)

        for name in ("logs.jsonl", "metrics.jsonl"):
            assert (directory / name).stat().st_size >= (SMALL_REQUEST + 2) // 2

    def test_both_files_are_nonempty_for_every_small_request(self, tmp_path):
        one_logs_line = len(lines_of(generate(tmp_path, "probe", 1) / "logs.jsonl")[0])
        requests = [1, one_logs_line - 1, one_logs_line, 2 * one_logs_line, 2 * one_logs_line + 1]

        for index, requested in enumerate(requests):
            directory = generate(tmp_path, f"pair-{index}", requested)

            for name in ("logs.jsonl", "metrics.jsonl"):
                assert (directory / name).stat().st_size > 0, (requested, name)

    def test_request_crossing_a_line_boundary_by_a_byte_adds_a_whole_line(self, tmp_path):
        one_logs_line = len(lines_of(generate(tmp_path, "probe", 1) / "logs.jsonl")[0])

        at_boundary = generate(tmp_path, "at", 2 * one_logs_line)
        across = generate(tmp_path, "across", 2 * one_logs_line + 1)

        assert len(lines_of(across / "logs.jsonl")) == len(lines_of(at_boundary / "logs.jsonl")) + 1

    def test_pair_is_nonempty_when_requested_size_is_zero_or_tiny(self, tmp_path):
        for index, requested in enumerate((0, 1)):
            directory = generate(tmp_path, f"tiny-{index}", requested)

            for name in ("logs.jsonl", "metrics.jsonl"):
                data = (directory / name).read_bytes()
                assert data, (requested, name)
                assert data.endswith(b"\n")
                assert len(data.splitlines()) >= 1

    def test_every_line_is_complete_json_ending_in_newline(self, tmp_path):
        directory = generate(tmp_path, "pair", SMALL_REQUEST)

        for name in ("logs.jsonl", "metrics.jsonl"):
            lines = lines_of(directory / name)
            assert lines
            for line in lines:
                assert line.endswith(b"\n")
                assert line.strip(), "a blank line was generated"
                assert isinstance(json.loads(line), dict)

    def test_every_line_carries_a_nonempty_body(self, tmp_path):
        directory = generate(tmp_path, "pair", SMALL_REQUEST)

        for name in ("logs.jsonl", "metrics.jsonl"):
            for line in lines_of(directory / name):
                assert json.loads(line)["body"]

    def test_two_generations_of_the_same_size_are_byte_identical(self, tmp_path):
        first = generate(tmp_path, "first", SMALL_REQUEST)
        second = generate(tmp_path, "second", SMALL_REQUEST)

        for name in ("logs.jsonl", "metrics.jsonl"):
            assert (first / name).read_bytes() == (second / name).read_bytes()

    def test_larger_request_generates_more_data_of_the_same_shape(self, tmp_path):
        small = generate(tmp_path, "small", SMALL_REQUEST)
        large = generate(tmp_path, "large", 4 * SMALL_REQUEST)

        small_logs, large_logs = lines_of(small / "logs.jsonl"), lines_of(large / "logs.jsonl")
        assert len(large_logs) > 3 * len(small_logs)
        assert set(large_logs) == set(small_logs)


# ── The measurement adapter ──────────────────────────────────────────────────

CHILD_PID = 4242
EXITED_WITH_STATUS_3 = 3 << 8
KILLED_BY_SIGKILL = 9


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingProcesses:
    """The process port, recording what it was asked and answering with a fixed exit."""

    def __init__(self, clock: FakeClock | None = None, *, wait_status: int = 0,
                 max_resident_raw: int = 0, seconds_to_exit: float = 0.0) -> None:
        self.clock = clock
        self.wait_status = wait_status
        self.max_resident_raw = max_resident_raw
        self.seconds_to_exit = seconds_to_exit
        self.started: list[tuple[list[str], Path, Path]] = []
        self.waited: list[int] = []
        self.reaped: list[int] = []

    def start(self, argv, *, cwd, stdout_path):
        self.started.append((list(argv), cwd, stdout_path))
        return CHILD_PID

    def wait(self, pid):
        self.waited.append(pid)
        if self.clock is not None:
            self.clock.now += self.seconds_to_exit
        return cost.ChildExit(self.wait_status, self.max_resident_raw)

    def terminate_and_reap(self, pid):
        self.reaped.append(pid)


class InterruptedWait(RecordingProcesses):
    def wait(self, pid):
        self.waited.append(pid)
        raise KeyboardInterrupt


def measure(processes, *, clock=None, directory=Path("generated"), checkout=Path("checkout")):
    return cost.measure_report(
        "agents",
        directory,
        checkout_root=checkout,
        output_path=Path("report.out"),
        processes=processes,
        clock=clock or FakeClock(),
    )


class TestResidentSizeConversion:
    def test_macos_bytes_and_linux_kib_convert_to_the_same_decimal_megabytes(self):
        same_size_in_bytes = 199_999_488
        same_size_in_kib = same_size_in_bytes // 1024

        from_macos = cost.peak_bytes_from_maxrss(same_size_in_bytes, platform="darwin")
        from_linux = cost.peak_bytes_from_maxrss(same_size_in_kib, platform="linux")

        assert from_macos == from_linux == same_size_in_bytes
        assert cost.megabytes_of(from_macos) == cost.megabytes_of(from_linux) == pytest.approx(
            199.999488
        )

    def test_a_megabyte_is_one_million_bytes_not_a_mebibyte(self):
        assert cost.megabytes_of(1_000_000) == 1.0


class TestChildCommand:
    def test_child_command_is_current_interpreter_module_entry_with_explicit_directory(self):
        processes = RecordingProcesses()
        relative_directory = Path("somewhere") / "generated"

        measure(processes, directory=relative_directory, checkout=Path("the-checkout"))

        (argv, cwd, stdout_path), = processes.started
        assert argv == [
            os.path.abspath(sys.executable),
            "-E", "-S", "-B", "-m", "ka_telemetry",
            "agents",
            "--dir", os.path.abspath(relative_directory),
        ]
        assert os.path.isabs(argv[0]) and os.path.isabs(argv[-1])
        assert cwd == Path("the-checkout")
        assert stdout_path == Path("report.out")


class TestExitNormalisation:
    def test_nonzero_exit_status_is_returned_unchanged(self):
        run = measure(RecordingProcesses(wait_status=EXITED_WITH_STATUS_3))

        assert run.status == 3
        assert run.signal_number is None

    def test_zero_exit_reports_zero_and_the_converted_peak(self):
        run = measure(RecordingProcesses(max_resident_raw=2048))

        assert run.status == 0
        assert run.peak_resident_bytes == cost.peak_bytes_from_maxrss(2048, platform=sys.platform)

    def test_adapter_reports_signal_death_as_nonzero_status(self):
        run = measure(RecordingProcesses(wait_status=KILLED_BY_SIGKILL))

        assert run.status == 128 + KILLED_BY_SIGKILL
        assert run.signal_number == KILLED_BY_SIGKILL


class TestElapsedTime:
    def test_elapsed_time_runs_from_launch_to_exit_and_excludes_generation(self):
        clock = FakeClock(now=100.0)  # generation has already taken the clock to 100
        processes = RecordingProcesses(clock, seconds_to_exit=7.0)

        run = measure(processes, clock=clock)
        clock.now += 50.0  # formatting that follows the run is not part of it

        assert run.elapsed_seconds == 7.0


class TestInterruption:
    def test_interrupt_terminates_and_reaps_the_owned_child(self):
        processes = InterruptedWait()

        with pytest.raises(KeyboardInterrupt):
            measure(processes)

        assert processes.reaped == [CHILD_PID]


# ── The orchestration and the entry point ────────────────────────────────────

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
NEXT_STEP_AFTER_REPORT_FAILURE = (
    "If {command} wrote an error above this line, resolve it, then run "
    "python3 tools/measure_stream_cost.py again."
)
RESULT_LINE = re.compile(
    r"^(agents|check) on (\d+ (?:MB|GB)) of stream data: "
    r"peak memory (\d+\.\d) MB, time (\d+\.\d) s$"
)
SMALL_SIZES = (20_000, 40_000, 60_000)


def result(peak_megabytes: float = 12.0, seconds: float = 1.0, *, status: int = 0,
           signal_number: int | None = None) -> cost.ReportRun:
    return cost.ReportRun(
        status=status,
        peak_resident_bytes=int(peak_megabytes * cost.BYTES_PER_MEGABYTE),
        elapsed_seconds=seconds,
        signal_number=signal_number,
    )


class FakeMeasure:
    """The measurement port: records every call's start and end, answers from a script."""

    def __init__(self, answers: dict[int, cost.ReportRun] | None = None) -> None:
        self.answers = answers or {}
        self.events: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, report: str, directory: Path, *, output_path: Path) -> cost.ReportRun:
        self.calls.append((report, directory.name))
        self.events.append(("start", report))
        answer = self.answers.get(len(self.calls), result(float(len(self.calls)), 0.5))
        self.events.append(("end", report))
        return answer


class FakeGenerate:
    """The generator: leaves a file in the directory so removal can be seen, and records sizes."""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.sizes: list[int] = []
        self.events = events if events is not None else []

    def __call__(self, directory: Path, requested_bytes: int) -> None:
        self.sizes.append(requested_bytes)
        self.events.append(("generate", str(requested_bytes)))
        (directory / "logs.jsonl").write_text("{}\n", encoding="utf-8")


def orchestrate(storage, *, sizes=cost.DEFAULT_SIZES_BYTES, generate=None, measure=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    status = cost.run_measurements(
        sizes=sizes,
        generate=generate or FakeGenerate(),
        measure=measure or FakeMeasure(),
        storage=storage,
        stdout=stdout,
        stderr=stderr,
    )
    return status, stdout.getvalue().splitlines(), stderr.getvalue().splitlines()


def real_generate(storage):
    return lambda directory, requested_bytes: cost.generate_pair(
        directory, requested_bytes, storage=storage
    )


def assert_nothing_left(parent: Path) -> None:
    assert list(parent.iterdir()) == [], "the temporary directory was left behind"


def write_failure_lines(reason: str, parent: Path) -> list[str]:
    return [
        f"Could not write the stream data: {reason}",
        f"Check that {parent} can be written to and has more than 1 GB free, then run "
        "python3 tools/measure_stream_cost.py again.",
    ]


class FailingStorage(cost.TemporaryStorage):
    """Real storage with one chosen place to fail, injected through the storage boundary."""

    def __init__(self, *, parent: Path, fail_make_root: OSError | None = None,
                 fail_open_number: int | None = None, fail_write_after: int | None = None,
                 fail_close: bool = False, fail_in_open_number: int | None = None) -> None:
        super().__init__(parent=parent)
        self.fail_make_root = fail_make_root
        self.fail_open_number = fail_open_number
        self.fail_write_after = fail_write_after
        self.fail_close = fail_close
        self.fail_in_open_number = fail_in_open_number
        self.opens = 0

    def make_root(self) -> Path:
        if self.fail_make_root is not None:
            raise self.fail_make_root
        return super().make_root()

    def open_for_write(self, path: Path):
        self.opens += 1
        if self.opens == self.fail_open_number:
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        handle = super().open_for_write(path)
        if self.opens == self.fail_in_open_number and (
            self.fail_write_after is not None or self.fail_close
        ):
            return _FailingHandle(handle, self.fail_write_after, self.fail_close)
        return handle


class _FailingHandle:
    def __init__(self, handle, fail_write_after: int | None, fail_close: bool) -> None:
        self.handle = handle
        self.fail_write_after = fail_write_after
        self.fail_close = fail_close
        self.writes = 0

    def write(self, data: bytes) -> int:
        self.writes += 1
        if self.fail_write_after is not None and self.writes > self.fail_write_after:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return self.handle.write(data)

    def close(self) -> None:
        self.handle.close()
        if self.fail_close:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class TestEntryPointComposition:
    def test_entry_point_composes_default_sizes_and_real_collaborators_without_options(
        self, tmp_path
    ):
        arguments = cost.default_arguments(checkout_root=PRODUCT_ROOT, environ={})

        assert inspect.signature(cost.main).parameters == {}
        assert arguments["sizes"] == cost.DEFAULT_SIZES_BYTES
        assert isinstance(arguments["storage"], cost.TemporaryStorage)
        assert arguments["generate"].func is cost.generate_pair
        assert arguments["measure"].func is cost.measure_report
        measure_keywords = arguments["measure"].keywords
        assert measure_keywords["checkout_root"] == PRODUCT_ROOT
        assert isinstance(measure_keywords["processes"], cost.ChildProcesses)
        assert measure_keywords["clock"] is time.perf_counter
        assert arguments["stdout"] is sys.stdout and arguments["stderr"] is sys.stderr

    def test_temporary_parent_inside_the_checkout_is_bypassed(self, tmp_path):
        checkout = tmp_path / "checkout"
        inside = checkout / "scratch"
        inside.mkdir(parents=True)

        arguments = cost.default_arguments(checkout_root=checkout, environ={"TMPDIR": str(inside)})

        parent = arguments["storage"].parent.resolve()
        assert checkout.resolve() not in (parent, *parent.parents)

    def test_a_temporary_parent_that_is_not_there_is_passed_over(self, tmp_path):
        usable = tmp_path / "usable"
        usable.mkdir()

        chosen = cost.choose_temporary_parent(
            tmp_path / "checkout", candidates=[tmp_path / "missing", usable]
        )

        assert chosen == usable.resolve()

    def test_no_usable_temporary_parent_raises_the_system_error(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        with pytest.raises(OSError):
            cost.choose_temporary_parent(checkout, candidates=[checkout, tmp_path / "missing"])

    def test_no_usable_temporary_parent_is_a_write_failure_naming_the_directory(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        arguments = cost.default_arguments(
            checkout_root=Path("/"), environ={"TMPDIR": str(checkout)}
        )

        status, stdout, stderr = orchestrate(arguments["storage"], measure=FakeMeasure())

        assert (status, stdout) == (1, [])
        assert stderr[0].startswith("Could not write the stream data: ")
        assert stderr[1].startswith(f"Check that {checkout} can be written to")

    def test_interrupt_maps_to_status_130_at_the_entry_point_only(self):
        def interrupted() -> int:
            raise KeyboardInterrupt

        assert cost.status_of(interrupted) == 130
        assert cost.status_of(lambda: 0) == 0
        assert cost.status_of(lambda: 1) == 1


class TestOrdering:
    def test_runs_agents_then_check_for_each_size_smallest_first_one_at_a_time(self, tmp_path):
        measure, generate = FakeMeasure(), FakeGenerate()
        measure.events = generate.events

        status, _stdout, _stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), generate=generate, measure=measure
        )

        assert status == 0
        assert measure.calls == [
            ("agents", "100000000"), ("check", "100000000"),
            ("agents", "300000000"), ("check", "300000000"),
            ("agents", "1000000000"), ("check", "1000000000"),
        ]
        assert generate.sizes == [100_000_000, 300_000_000, 1_000_000_000]
        assert generate.events == [
            ("generate", "100000000"),
            ("start", "agents"), ("end", "agents"), ("start", "check"), ("end", "check"),
            ("generate", "300000000"),
            ("start", "agents"), ("end", "agents"), ("start", "check"), ("end", "check"),
            ("generate", "1000000000"),
            ("start", "agents"), ("end", "agents"), ("start", "check"), ("end", "check"),
        ]


class TestOutput:
    def test_stdout_is_exactly_one_line_per_run_and_nothing_else(self, tmp_path):
        measure = FakeMeasure({
            1: result(1234.56, 41.25), 2: result(1000.0, 3.0), 3: result(2000.04, 9.99),
            4: result(5.0, 0.04), 5: result(2345.67, 120.0), 6: result(2210.0, 39.84),
        })

        storage = cost.TemporaryStorage(parent=tmp_path)

        status, stdout, stderr = orchestrate(storage, measure=measure)

        assert status == 0
        assert stdout == [
            "agents on 100 MB of stream data: peak memory 1234.6 MB, time 41.2 s",
            "check on 100 MB of stream data: peak memory 1000.0 MB, time 3.0 s",
            "agents on 300 MB of stream data: peak memory 2000.0 MB, time 10.0 s",
            "check on 300 MB of stream data: peak memory 5.0 MB, time 0.0 s",
            "agents on 1 GB of stream data: peak memory 2345.7 MB, time 120.0 s",
            "check on 1 GB of stream data: peak memory 2210.0 MB, time 39.8 s",
        ]
        assert stderr == []

    def test_size_label_switches_from_mb_to_gb_at_one_gigabyte(self):
        assert cost.size_label(300_000_000) == "300 MB"
        assert cost.size_label(999_999_999) == "999 MB"
        assert cost.size_label(1_000_000_000) == "1 GB"

    def test_figures_have_one_decimal_place(self, tmp_path):
        measure = FakeMeasure({1: result(7.0, 2.0), 2: result(7.25, 0.06), 3: result(0.0, 100.0)})

        _status, stdout, _stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure
        )

        for line in stdout:
            assert RESULT_LINE.match(line), line

    def test_progress_goes_to_standard_error_only(self, tmp_path):
        measure = FakeMeasure({3: result(status=1)})

        _status, stdout, stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure
        )

        assert all(RESULT_LINE.match(line) for line in stdout)
        assert len(stdout) == 2
        assert stderr, "the failure was not reported on standard error"

    def test_all_runs_exit_zero_gives_script_status_zero(self, tmp_path):
        status, stdout, _stderr = orchestrate(cost.TemporaryStorage(parent=tmp_path))

        assert status == 0
        assert len(stdout) == 6


class TestReportFailure:
    def test_first_report_failure_stops_everything_and_exits_one(self, tmp_path):
        measure = FakeMeasure({1: result(status=1)})
        generate = FakeGenerate()

        status, stdout, stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure, generate=generate
        )

        assert status == 1
        assert stdout == []
        assert stderr == [
            "agents on 100 MB of stream data exited with status 1; no figure was printed for it.",
            NEXT_STEP_AFTER_REPORT_FAILURE.format(command="agents"),
        ]
        assert len(measure.calls) == 1 and generate.sizes == [100_000_000]
        assert_nothing_left(tmp_path)

    def test_later_agents_failure_keeps_earlier_lines(self, tmp_path):
        measure = FakeMeasure({3: result(status=2)})

        status, stdout, stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure
        )

        assert status == 1
        assert [line.split(" of ")[0] for line in stdout] == [
            "agents on 100 MB", "check on 100 MB"
        ]
        assert stderr[0] == (
            "agents on 300 MB of stream data exited with status 2; no figure was printed for it."
        )
        assert len(measure.calls) == 3
        assert_nothing_left(tmp_path)

    def test_final_check_failure_prints_the_five_lines_before_it(self, tmp_path):
        measure = FakeMeasure({6: result(status=1)})

        status, stdout, stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure
        )

        assert status == 1
        assert len(stdout) == 5
        assert stderr == [
            "check on 1 GB of stream data exited with status 1; no figure was printed for it.",
            NEXT_STEP_AFTER_REPORT_FAILURE.format(command="check"),
        ]
        assert_nothing_left(tmp_path)

    def test_signal_killed_report_follows_the_nonzero_path(self, tmp_path):
        killed = result(status=128 + signal.SIGKILL, signal_number=signal.SIGKILL)
        measure = FakeMeasure({1: killed})

        status, stdout, stderr = orchestrate(
            cost.TemporaryStorage(parent=tmp_path), measure=measure
        )

        assert status == 1
        assert stdout == []
        assert stderr == [
            "agents on 100 MB of stream data was ended by signal SIGKILL; "
            "no figure was printed for it.",
            NEXT_STEP_AFTER_REPORT_FAILURE.format(command="agents"),
        ]
        assert len(measure.calls) == 1
        assert_nothing_left(tmp_path)


class TestWriteFailure:
    def test_cannot_make_temporary_directory_reports_write_failure(self, tmp_path):
        storage = FailingStorage(
            parent=tmp_path, fail_make_root=PermissionError(errno.EACCES, "Permission denied")
        )
        measure = FakeMeasure()

        status, stdout, stderr = orchestrate(storage, measure=measure)

        assert status == 1
        assert stdout == []
        assert stderr == write_failure_lines("Permission denied", tmp_path)
        assert measure.calls == []

    def test_open_failure_on_first_file_and_on_second_file_report_write_failure(self, tmp_path):
        for failing_open in (1, 2):
            parent = tmp_path / f"open-{failing_open}"
            parent.mkdir()
            storage = FailingStorage(parent=parent, fail_open_number=failing_open)
            measure = FakeMeasure()

            status, stdout, stderr = orchestrate(
                storage, sizes=SMALL_SIZES, generate=real_generate(storage), measure=measure
            )

            assert status == 1
            assert stdout == []
            assert stderr == write_failure_lines("Permission denied", parent)
            assert measure.calls == []
            assert_nothing_left(parent)

    def test_disk_full_while_writing_reports_write_failure_and_removes_partial_data(self, tmp_path):
        first = tmp_path / "first"
        first.mkdir()
        storage = FailingStorage(parent=first, fail_in_open_number=1, fail_write_after=0)

        status, stdout, stderr = orchestrate(
            storage, sizes=SMALL_SIZES, generate=real_generate(storage)
        )

        assert (status, stdout) == (1, [])
        assert stderr == write_failure_lines("No space left on device", first)
        assert_nothing_left(first)

        later = tmp_path / "later"
        later.mkdir()
        storage = FailingStorage(parent=later, fail_in_open_number=3, fail_write_after=0)
        measure = FakeMeasure()

        status, stdout, stderr = orchestrate(
            storage, sizes=SMALL_SIZES, generate=real_generate(storage), measure=measure
        )

        assert status == 1
        assert len(stdout) == 2 and all(RESULT_LINE.match(line) for line in stdout)
        assert stderr == write_failure_lines("No space left on device", later)
        assert len(measure.calls) == 2
        assert_nothing_left(later)

    def test_close_failure_of_a_buffered_file_reports_write_failure(self, tmp_path):
        storage = FailingStorage(parent=tmp_path, fail_in_open_number=2, fail_close=True)
        measure = FakeMeasure()

        status, stdout, stderr = orchestrate(
            storage, sizes=SMALL_SIZES, generate=real_generate(storage), measure=measure
        )

        assert (status, stdout) == (1, [])
        assert stderr == write_failure_lines("No space left on device", tmp_path)
        assert measure.calls == []
        assert_nothing_left(tmp_path)


class TestCleanup:
    def test_temporary_root_is_removed_after_success_and_after_each_failure(self, tmp_path):
        scenarios = {
            "success": FakeMeasure(),
            "first failure": FakeMeasure({1: result(status=1)}),
            "last failure": FakeMeasure({6: result(status=1)}),
            "signal": FakeMeasure({2: result(status=137, signal_number=9)}),
        }
        for name, measure in scenarios.items():
            parent = tmp_path / name.replace(" ", "-")
            parent.mkdir()

            orchestrate(cost.TemporaryStorage(parent=parent), measure=measure)

            assert_nothing_left(parent)

    def test_a_size_directory_is_removed_before_the_next_size_is_generated(self, tmp_path):
        directories_seen: list[list[str]] = []

        def generate(directory: Path, requested_bytes: int) -> None:
            directories_seen.append(sorted(path.name for path in directory.parent.iterdir()))

        orchestrate(cost.TemporaryStorage(parent=tmp_path), generate=generate)

        assert directories_seen == [["100000000"], ["300000000"], ["1000000000"]]


class TestInterruptedRunOrder:
    """Added by the code-integrity review: the instruction's "its files are not removed before it is
    gone" (Task 2) and the design's "terminates and reaps its owned child before deleting that
    child's files" (change_design.md § 3), asserted as an order across the whole orchestration."""

    def test_an_interrupted_run_reaps_the_child_before_its_files_are_removed(self, tmp_path):
        events: list[tuple[str, object]] = []

        class InterruptedChild(RecordingProcesses):
            def wait(self, pid):
                events.append(("wait", pid))
                raise KeyboardInterrupt

            def terminate_and_reap(self, pid):
                events.append(("reaped", pid))

        class RecordingStorage(cost.TemporaryStorage):
            def remove_tree(self, path: Path) -> None:
                events.append(("removed", path))
                super().remove_tree(path)

        storage = RecordingStorage(parent=tmp_path)
        measure = functools.partial(
            cost.measure_report,
            checkout_root=PRODUCT_ROOT,
            processes=InterruptedChild(),
            clock=FakeClock(),
        )

        with pytest.raises(KeyboardInterrupt):
            cost.run_measurements(
                sizes=SMALL_SIZES,
                generate=FakeGenerate(),
                measure=measure,
                storage=storage,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        assert [kind for kind, _detail in events] == ["wait", "reaped", "removed"]
        assert events[1] == ("reaped", CHILD_PID)
        assert events[2][1].parent == tmp_path
        assert_nothing_left(tmp_path)
