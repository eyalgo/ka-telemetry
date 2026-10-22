"""The release check, run as a person runs it: `python3 tools/release_check.py` over real copies.

Each copy is a Git repository made from this checkout under pytest's temporary directory, because
the command clones the checkout it lives in. A broken copy is a copy with one file damaged after
the commit, which is the shape of a contributor's uncommitted change. The checkout under test is
never edited. Each run is given an empty `HOME` and an empty `TMPDIR` of its own, so what the
command leaves behind in them can be seen.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_release_metrics import (
    CROSS_FILE_RESPONSE,
    NOT_COPIED,
    PRODUCT_ROOT,
    README_EXAMPLE_FIGURE,
    edit,
    scale_session_cost,
)

SCRIPT = Path("tools") / "release_check.py"
AGREEING_SESSION = "6547733b-af89-40a8-a524-2df28af94cae"
RUNS_JSON = "tests/parity/expected/runs.json"
STEPS_TEXT = "tests/parity/expected/steps.txt"
BARS = {
    1: "Every figure ka-telemetry reports on the reference set equals its recorded expected figure",
    2: (
        "Cost totals agree within 1% for every session recorded as agreeing, and every session "
        "outside 1% or with no cost metric is named with a possible cause"
    ),
    3: (
        "No response on the reference set is counted more than once by the runs report or the "
        "steps report"
    ),
    5: (
        "The README's install command, run as written on a Python that refuses pip installs as "
        "externally managed, installs the ka-telemetry built from this checkout and no other "
        "package, setting pip, setuptools and wheel aside"
    ),
    7: (
        "No ka-telemetry command that the release check runs tries to reach beyond this machine, "
        "and the receiver started with no --host listens only on this machine"
    ),
    8: "Every example in the README and the how-to runs as written and prints the output shown",
}
TERMINAL_ESCAPE = "\x1b"


@dataclass(frozen=True)
class Run:
    """One run of the command: what it printed, how it ended, and where it ran."""

    checkout: Path
    stdout: str
    stderr: str
    returncode: int
    home: Path
    tmpdir: Path

    @property
    def lines(self) -> list[str]:
        return self.stdout.splitlines()

    def verdict(self, metric: int) -> str:
        [line] = [line for line in self.lines if line.startswith(f"{metric} ")]
        return line.rsplit(" ", 1)[1]

    def explanations(self, metric: int) -> list[str]:
        return [line for line in self.stderr.splitlines() if line.startswith(f"{metric} ")]


def committed_copy(base: Path) -> Path:
    """A copy of this checkout that is a Git repository with everything committed."""
    copy = base / "checkout"
    shutil.copytree(PRODUCT_ROOT, copy, ignore=NOT_COPIED)
    git = [
        "git", "-C", str(copy),
        "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null",
    ]
    subprocess.run([*git, "init", "--quiet"], check=True)
    subprocess.run([*git, "add", "--all"], check=True)
    subprocess.run([*git, "commit", "--quiet", "--message", "copy"], check=True)
    return copy


def caller_environment(home: Path, tmpdir: Path, path: str | None = None) -> dict[str, str]:
    """A person's shell: no Python or pytest setting, an empty home, an empty temporary place."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST_"))
    }
    env.update(HOME=str(home), TMPDIR=str(tmpdir))
    if path is not None:
        env["PATH"] = path
    return env


def run_check(
    checkout: Path,
    base: Path,
    *,
    script: Path | None = None,
    cwd: Path | None = None,
    path: str | None = None,
) -> Run:
    home, tmpdir = base / "home", base / "tmpdir"
    home.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    done = subprocess.run(
        [sys.executable, str(script or checkout / SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=caller_environment(home, tmpdir, path),
        cwd=cwd or checkout,
    )
    return Run(checkout, done.stdout, done.stderr, done.returncode, home, tmpdir)


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under `root`, by relative path: a file by its digest, a link by its target,
    a directory by a marker."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = "link:" + os.readlink(path)
        elif path.is_dir():
            found[relative] = "directory"
        else:
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def make_run(tmp_path_factory, name: str, damage: Callable[[Path], None] | None = None) -> Run:
    base = tmp_path_factory.mktemp(name)
    checkout = committed_copy(base)
    if damage is not None:
        damage(checkout)
    return run_check(checkout, base)


def break_1(checkout: Path) -> None:
    edit(checkout / RUNS_JSON, lambda text: text.replace("1557", "1558", 1))


def break_2(checkout: Path) -> None:
    scale_session_cost(checkout / "tests/stream_parity/metrics.jsonl", AGREEING_SESSION, 1.02)


COUNTED_RESPONSE_YIELD = "        yield usage, model, timestamp\n"


def break_3(checkout: Path) -> None:
    """The runs report counts one response twice: the reader, after its selection gate, hands one
    accepted response over a second time. No transcript is touched, so a response that sits in
    two transcripts is not what fails the check."""
    reader = checkout / "ka_telemetry/run_usage.py"
    text = reader.read_text(encoding="utf-8")
    assert text.count(COUNTED_RESPONSE_YIELD) == 1, "the damage did not find its anchor"
    repeat = f'        if response_id == "{CROSS_FILE_RESPONSE}":\n    ' + COUNTED_RESPONSE_YIELD
    reader.write_text(
        text.replace(COUNTED_RESPONSE_YIELD, COUNTED_RESPONSE_YIELD + repeat), encoding="utf-8"
    )


def break_5(checkout: Path) -> None:
    edit(
        checkout / "pyproject.toml",
        lambda text: text.replace("dependencies = []", 'dependencies = ["six"]'),
    )


OUTBOUND_ATTEMPT = (
    "    if command == 'check':\n"
    "        import socket\n"
    "        try:\n"
    "            socket.create_connection(('192.0.2.1', 9), timeout=1)\n"
    "        except OSError:\n"
    "            pass\n"
)
OUTBOUND_AFTER = "    if command == \"check\":\n"


def break_7_outbound(checkout: Path) -> None:
    """The check command catches a refused connection to a documentation address and goes on."""
    edit(
        checkout / "ka_telemetry/cli.py",
        lambda text: text.replace(
            OUTBOUND_AFTER, OUTBOUND_ATTEMPT + OUTBOUND_AFTER, 1
        ),
    )


def break_7_wildcard(checkout: Path) -> None:
    """With no --host, the receiver binds every interface."""
    edit(
        checkout / "ka_telemetry/receiver.py",
        lambda text: text.replace('LOOPBACK = "127.0.0.1"', 'LOOPBACK = "0.0.0.0"', 1),
    )


def break_8(checkout: Path) -> None:
    edit(
        checkout / "README.md",
        lambda text: text.replace(README_EXAMPLE_FIGURE, "cost_usd=0.051884"),
    )


def break_the_build(checkout: Path) -> None:
    edit(
        checkout / "pyproject.toml",
        lambda text: text.replace('requires = ["setuptools>=77,<90"]', "requires = []").replace(
            'build-backend = "setuptools.build_meta"', 'build-backend = "no_such_backend"'
        ),
    )


CRASH = '    raise RuntimeError("the worker crashed")\n'


def crash_metric_3(checkout: Path) -> None:
    anchor = "    found: list[Diagnostic] = []\n    for report in REPORTS:"
    edit(
        checkout / "tools/release_metrics.py",
        lambda text: text.replace(anchor, CRASH + anchor),
    )


def malform_metric_3(checkout: Path) -> None:
    edit(
        checkout / "tools/release_metrics.py",
        lambda text: text.replace(
            "    print(result_json(result))",
            '    print("not a result" if arguments.metric == 3 else result_json(result))',
        ),
    )


BREAKS = {
    1: break_1,
    2: break_2,
    3: break_3,
    5: break_5,
    7: break_7_outbound,
    8: break_8,
}


@pytest.fixture(scope="module")
def unbroken(tmp_path_factory) -> Run:
    return make_run(tmp_path_factory, "unbroken")


@pytest.fixture(scope="module")
def broken(tmp_path_factory) -> dict[int, Run]:
    """One copy per bar, each damaged in the one way that bar measures."""
    return {
        metric: make_run(tmp_path_factory, f"break{metric}", damage)
        for metric, damage in BREAKS.items()
    }


@pytest.fixture(scope="module")
def wildcard_default(tmp_path_factory) -> Run:
    """The receiver's default host changed to the wildcard address."""
    return make_run(tmp_path_factory, "wildcard", break_7_wildcard)


@pytest.fixture(scope="module")
def build_failure(tmp_path_factory) -> Run:
    return make_run(tmp_path_factory, "build", break_the_build)


@pytest.fixture(scope="module")
def worker_crash(tmp_path_factory) -> Run:
    return make_run(tmp_path_factory, "crash", crash_metric_3)


@pytest.fixture(scope="module")
def malformed_result(tmp_path_factory) -> Run:
    return make_run(tmp_path_factory, "malformed", malform_metric_3)


def numbered(run: Run) -> list[int]:
    return [int(line.split(" ", 1)[0]) for line in run.lines]


def test_unbroken_checkout_prints_each_metric_once_ascending_and_exits_zero(unbroken):
    ids = numbered(unbroken)
    assert [i for i in ids if i in BARS] == sorted(BARS)
    assert ids == sorted(set(ids))
    for metric in BARS:
        assert unbroken.verdict(metric) == "holds"
    assert unbroken.returncode == 0, unbroken.stderr
    assert unbroken.stderr == ""


def test_every_stdout_line_has_the_published_shape(unbroken):
    shape = re.compile(r"^(1|2|3|5|7|8) .*(holds|fails)$")
    assert len([line for line in unbroken.lines if shape.match(line)]) == 6
    assert len(unbroken.lines) == 6
    for metric, words in BARS.items():
        assert f"{metric} {words}: holds" in unbroken.lines


def test_each_line_is_plain_text_without_terminal_escapes(unbroken, build_failure):
    for run in (unbroken, build_failure):
        assert TERMINAL_ESCAPE not in run.stdout
        assert TERMINAL_ESCAPE not in run.stderr


@pytest.mark.parametrize("metric", sorted(BREAKS))
def test_a_metric_description_reads_the_same_for_holds_and_fails(unbroken, broken, metric):
    assert broken[metric].verdict(metric) == "fails"
    assert f"{metric} {BARS[metric]}: fails" in broken[metric].lines
    assert f"{metric} {BARS[metric]}: holds" in unbroken.lines


def test_break_1_one_figure_off_by_one_fails_metric_1(broken):
    run = broken[1]
    assert run.verdict(1) == "fails"
    assert run.returncode == 1
    assert any(RUNS_JSON in line for line in run.explanations(1)), run.stderr


def test_break_2_session_cost_moved_outside_one_percent_fails_metric_2(broken):
    run = broken[2]
    assert run.verdict(2) == "fails" and run.returncode == 1
    assert any(AGREEING_SESSION in line for line in run.explanations(2)), run.stderr


def test_break_3_double_counting_report_fails_metric_3_naming_the_response_and_report(broken):
    run = broken[3]
    assert run.verdict(3) == "fails" and run.returncode == 1
    assert any(
        CROSS_FILE_RESPONSE in line and "runs report" in line for line in run.explanations(3)
    ), run.stderr


def test_break_8_changed_readme_example_output_fails_metric_8(broken):
    run = broken[8]
    assert run.verdict(8) == "fails" and run.returncode == 1
    assert any("README.md" in line for line in run.explanations(8)), run.stderr


@pytest.mark.parametrize("metric", sorted(BREAKS))
def test_every_broken_copy_fails_its_own_metric_with_an_explanation(broken, metric):
    run = broken[metric]
    assert run.verdict(metric) == "fails"
    assert run.explanations(metric), run.stderr
    assert run.returncode == 1


def test_break_3_damage_confirms_it_found_its_anchor(tmp_path):
    reader = tmp_path / "ka_telemetry" / "run_usage.py"
    reader.parent.mkdir()
    reader.write_text("def usage_records():\n    return []\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="anchor"):
        break_3(tmp_path)
    assert reader.read_text(encoding="utf-8") == "def usage_records():\n    return []\n"


PLAIN_PIP = "python3 -m pip install ka-telemetry"
CANONICAL_INSTALL = "pipx install ka-telemetry\n"


def break_5_plain_pip(checkout: Path) -> None:
    """The README's first Install block holds a plain pip install, which a Python that refuses
    pip installs rejects."""
    edit(
        checkout / "README.md",
        lambda text: text.replace(CANONICAL_INSTALL, PLAIN_PIP + "\n", 1),
    )


def test_plain_pip_in_the_readme_fails_bar_5_names_the_command_and_exits_1(tmp_path):
    base = tmp_path / "plain-pip"
    base.mkdir()
    checkout = committed_copy(base)
    break_5_plain_pip(checkout)
    assert PLAIN_PIP in (checkout / "README.md").read_text(encoding="utf-8")

    run = run_check(checkout, base)

    assert f"5 {BARS[5]}: fails" in run.lines
    installed_nothing = [line for line in run.explanations(5) if "installed nothing" in line]
    assert installed_nothing and PLAIN_PIP in installed_nothing[0], run.stderr
    assert any("externally-managed-environment" in line for line in run.explanations(5)), run.stderr
    assert run.returncode == 1


def test_break_5_a_runtime_dependency_fails_metric_5_and_names_it(broken):
    run = broken[5]
    assert run.verdict(5) == "fails" and run.returncode == 1
    assert any("six" in line for line in run.explanations(5)), run.stderr
    assert run.stderr.strip(), "bar 5 explains itself on standard error"


def test_break_7_a_caught_connection_fails_metric_7_while_the_command_exits_zero(broken):
    run = broken[7]
    assert run.verdict(7) == "fails" and run.returncode == 1
    explained = "\n".join(run.explanations(7))
    assert "python3 -m ka_telemetry check" in explained, run.stderr
    assert "192.0.2.1, port 9" in explained, run.stderr
    assert "did not complete" not in explained, run.stderr  # the check itself ended with status 0


def test_break_7_a_wildcard_default_address_fails_metric_7_naming_the_address(wildcard_default):
    run = wildcard_default
    assert run.verdict(7) == "fails" and run.returncode == 1
    assert any("0.0.0.0" in line and "port" in line for line in run.explanations(7)), run.stderr


@pytest.mark.parametrize("metric", [1, 5, 7, 8])
def test_a_break_that_only_one_bar_reads_fails_only_that_bar(broken, metric):
    run = broken[metric]
    expected = {m: "fails" if m == metric else "holds" for m in BARS}
    assert {m: run.verdict(m) for m in BARS} == expected


def test_a_build_failure_fails_metrics_5_and_8_while_1_2_3_hold(build_failure):
    run = build_failure
    assert [run.verdict(metric) for metric in (1, 2, 3, 5, 8)] == [
        "holds", "holds", "holds", "fails", "fails",
    ]
    assert run.verdict(7) == "holds"
    assert run.returncode == 1
    explained = run.explanations(8)
    assert explained[0].startswith("8 This bar was not measured: ka-telemetry could not be built")
    assert "no_such_backend" in "\n".join(explained), run.stderr
    assert run.explanations(5)[0].startswith(
        "5 This bar was not measured: ka-telemetry could not be built from this checkout"
    )
    assert all(line.startswith(("5 ", "8 ")) for line in run.stderr.splitlines())


def test_a_worker_crash_or_malformed_result_fails_only_that_metric_and_later_lines_still_print(
    worker_crash, malformed_result
):
    for run in (worker_crash, malformed_result):
        assert [m for m in numbered(run) if m in BARS] == sorted(BARS)
        assert run.verdict(3) == "fails" and run.returncode == 1
        assert [run.verdict(metric) for metric in (1, 2, 5, 7, 8)] == ["holds"] * 5
        assert any(
            line.startswith("3 This bar was not measured: its measurement stopped")
            for line in run.explanations(3)
        ), run.stderr
    assert any("the worker crashed" in line for line in worker_crash.explanations(3))


def test_missing_executable_or_failed_scratch_preparation_fails_every_registered_metric(
    tmp_path,
):
    git_less, plain = tmp_path / "git-less", tmp_path / "plain"
    git_less.mkdir()
    plain.mkdir()
    no_git = tmp_path / "no-git"
    no_git.mkdir()
    missing = run_check(committed_copy(git_less), git_less, path=str(no_git))
    not_a_repository = plain / "checkout"
    shutil.copytree(PRODUCT_ROOT, not_a_repository, ignore=NOT_COPIED)
    unprepared = run_check(not_a_repository, plain)
    for run, words in (
        (missing, "the release check needs Git to make its temporary copy of this checkout"),
        (unprepared, "the release check could not prepare the temporary copy of this checkout"),
    ):
        assert [run.verdict(metric) for metric in BARS] == ["fails"] * 6
        assert run.returncode == 1
        for metric in BARS:
            assert [line for line in run.explanations(metric) if words in line], run.stderr


def test_invoked_by_absolute_path_from_an_unrelated_directory_measures_the_owning_checkout(
    broken, tmp_path
):
    at_root = broken[1]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run = run_check(
        at_root.checkout, tmp_path, script=at_root.checkout.resolve() / SCRIPT, cwd=elsewhere
    )
    assert run.stdout == at_root.stdout
    assert run.returncode == at_root.returncode == 1
    assert run.verdict(1) == "fails"


def test_the_checkout_is_unchanged_after_a_run_on_success_and_on_failure(tmp_path):
    ok_base, failing_base = tmp_path / "ok", tmp_path / "failing"
    ok_base.mkdir()
    failing_base.mkdir()
    ok = committed_copy(ok_base)
    failing = committed_copy(failing_base)
    for checkout in (ok, failing):
        (checkout / "build").mkdir()
        (checkout / "build" / "sentinel").write_text("kept\n", encoding="utf-8")
        (checkout / ".pytest_cache").mkdir()
        (checkout / ".pytest_cache" / "sentinel").write_text("kept\n", encoding="utf-8")
        (checkout / "untracked.txt").write_text("kept\n", encoding="utf-8")
    break_8(failing)
    (failing / STEPS_TEXT).unlink()
    before_ok, before_failing = snapshot(ok), snapshot(failing)

    succeeded = run_check(ok, ok_base)
    failed = run_check(failing, failing_base)

    assert succeeded.returncode == 0, succeeded.stderr
    assert snapshot(ok) == before_ok
    assert failed.returncode == 1
    assert snapshot(failing) == before_failing
    assert any("README.md" in line for line in failed.explanations(8)), failed.stderr
    assert any(STEPS_TEXT in line for line in failed.explanations(1)), failed.stderr
    assert not (failing / STEPS_TEXT).exists()


def test_caller_home_and_tmpdir_are_empty_again_after_the_run(
    unbroken, build_failure, worker_crash, broken, wildcard_default
):
    for run in (unbroken, build_failure, worker_crash, broken[5], broken[7], wildcard_default):
        assert list(run.home.iterdir()) == [], run.home
        assert list(run.tmpdir.iterdir()) == [], run.tmpdir


def test_no_receiver_or_other_process_of_a_run_survives_it(
    unbroken, broken, wildcard_default, build_failure
):
    """Every child of a run works under that run's own temporary directory, so a process still
    alive after the run is one whose command line names a place under it."""
    listing = subprocess.run(
        ["ps", "-axo", "args"], capture_output=True, text=True, check=True
    ).stdout
    for run in (unbroken, broken[5], broken[7], wildcard_default, build_failure):
        assert str(run.tmpdir) not in listing, run.tmpdir
