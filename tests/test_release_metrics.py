"""The release check's measurements, one seam at a time.

Each test hands a collaborator to the unit under test and asserts on what the unit returned, so a
failing measurement is shown here without building the product or running the whole release check.
The command itself is held to the real thing in `tests/e2e/test_release_check.py`.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from e2e.parity_measure import MeasurementError, measure
from e2e.test_stream_parity import COST_MARKING, CURRENT_HEADING
from test_parity import NO_COST_LINE
from tools import release_metrics, release_network

TESTS = Path(__file__).resolve().parent
PRODUCT_ROOT = TESTS.parent
PARITY = TESTS / "parity"
STREAM = TESTS / "stream_parity"

EXPECTED_FILES = {
    "steps": "tests/parity/expected/steps.txt",
    "steps --json": "tests/parity/expected/steps.json",
    "runs": "tests/parity/expected/runs.txt",
    "runs --json": "tests/parity/expected/runs.json",
    "agents": "tests/stream_parity/expected/agents.txt",
    "agents --json": "tests/stream_parity/expected/agents.json",
    "agents --compare": "tests/stream_parity/expected/compare.txt",
    "agents --json --compare": "tests/stream_parity/expected/compare.json",
    "check": "tests/stream_parity/expected/agents.txt",
    "check --json": "tests/stream_parity/expected/agents.json",
}
CONTEXT_FILE = "tests/parity/expected/context_sizes.json"
NULL_CONTEXT_SESSION = "7ef055ef-5823-4b0d-845c-efc5b4fdab28"
WHOLE_NUMBER = re.compile(r"(?<![\w.\-])(\d+)(?![\w\-])")

Answers = dict[str, str]
Invoker = Callable[[str, tuple[str, ...]], str]


def _source_invoker(home: Path) -> Invoker:
    """The reference commands run from this checkout's source, under an empty home."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env.update(HOME=str(home), PYTHONPATH=str(PRODUCT_ROOT), PYTHONDONTWRITEBYTECODE="1")

    def invoke(label: str, arguments: tuple[str, ...]) -> str:
        done = subprocess.run(
            [sys.executable, "-m", "ka_telemetry", *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=home,
        )
        if done.returncode != 0:
            raise MeasurementError(label, f"exit code {done.returncode}: {done.stderr}")
        return done.stdout

    return invoke


@pytest.fixture(scope="module")
def answers(tmp_path_factory: pytest.TempPathFactory) -> Answers:
    """What the source command prints for every invocation the shared measure makes."""
    home = tmp_path_factory.mktemp("measure-home")
    run = _source_invoker(home)
    recorded: Answers = {}

    def recording(label: str, arguments: tuple[str, ...]) -> str:
        recorded[label] = run(label, arguments)
        return recorded[label]

    assert measure(recording, parity_root=PARITY, stream_root=STREAM) == []
    return recorded


def answering(answered: Answers, *, changed: Answers | None = None, failing=()) -> Invoker:
    """An invocation collaborator that answers from `answered`, with `changed` answers replaced
    and each label in `failing` reporting a failed command."""
    served = {**answered, **(changed or {})}

    def invoke(label: str, arguments: tuple[str, ...]) -> str:
        if label in failing:
            raise MeasurementError(label, "the command did not complete")
        return served[label]

    return invoke


def one_more(text: str) -> str:
    """`text` with the first whole number in it raised by one."""
    found = WHOLE_NUMBER.search(text)
    assert found, text
    return text[: found.start()] + str(int(found.group(1)) + 1) + text[found.end() :]


def paths_of(differences) -> set[str]:
    return {difference.expected_file for difference in differences}


def digest(*trees: Path) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for tree in trees
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


def test_unchanged_reference_set_gives_no_differences(answers):
    assert measure(answering(answers), parity_root=PARITY, stream_root=STREAM) == []


def test_one_figure_changed_by_one_names_that_expected_file(answers):
    context_labels = [label for label in answers if label.startswith("context ")]
    stored_sizes = json.loads((PARITY / "expected/context_sizes.json").read_text())
    assert len(context_labels) == len(stored_sizes)
    unnamed = []
    for label in [*EXPECTED_FILES, *context_labels]:
        expected_file = EXPECTED_FILES.get(label, CONTEXT_FILE)
        if NULL_CONTEXT_SESSION in label:
            continue  # a null size has no figure to change; the nullable test covers it
        differences = measure(
            answering(answers, changed={label: one_more(answers[label])}),
            parity_root=PARITY,
            stream_root=STREAM,
        )
        if expected_file not in paths_of(differences):
            unnamed.append(f"{label}: wanted {expected_file}, got {sorted(paths_of(differences))}")
    assert not unnamed
    assert len(set(EXPECTED_FILES.values()) | {CONTEXT_FILE}) == 9


FIGURE_SWEEP = r"""
import json, re, sys
from pathlib import Path

checkout, answers = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
figure, files = re.compile(sys.argv[3]), json.loads(sys.argv[4])
sys.path[:0] = [str(checkout / "tests"), str(checkout)]
from e2e.parity_measure import measure

missed = []
for relative in files:
    path = checkout / relative
    stored = path.read_text(encoding="utf-8")
    for found in figure.finditer(stored):
        changed = stored[: found.start()] + str(int(found.group(1)) + 1) + stored[found.end() :]
        path.write_text(changed, encoding="utf-8")
        try:
            named = {
                difference.expected_file
                for difference in measure(
                    lambda label, arguments: answers[label],
                    parity_root=checkout / "tests" / "parity",
                    stream_root=checkout / "tests" / "stream_parity",
                )
            }
        except Exception as error:
            named = {f"{type(error).__name__}: {error}"}
        if relative not in named:
            line = stored.count("\n", 0, found.start()) + 1
            missed.append(f"{relative}:{line} {found.group(1)} -> {sorted(named)}")
    path.write_text(stored, encoding="utf-8")
print(json.dumps(missed))
"""
"""Changes every whole number in each expected file of a copy by one, one at a time, and lists
each change the shared measure does not name that file for. It runs in a process of its own with
the copy's `tests/` imported, because the runs expectations are built by `tests/test_parity.py`
from the `tests/parity` beside it."""


def test_every_figure_in_every_expected_file_changed_by_one_names_that_file(answers, tmp_path):
    """CP-546: "A single figure changed by one in any one of those files makes the line end
    fails" — every figure of all nine files, not one sampled figure per file."""
    checkout = scratch_copy(tmp_path)
    recorded = tmp_path / "answers.json"
    recorded.write_text(json.dumps(answers), encoding="utf-8")
    files = sorted({*EXPECTED_FILES.values(), CONTEXT_FILE})
    assert len(files) == 9
    done = subprocess.run(
        [
            sys.executable, "-c", FIGURE_SWEEP,
            str(checkout), str(recorded), WHOLE_NUMBER.pattern, json.dumps(files),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=worker_env(),
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    missed = json.loads(done.stdout)
    assert missed == [], "\n".join(missed)


def test_accepted_wording_adjustments_still_match(answers):
    assert NO_COST_LINE in answers["runs"]
    assert CURRENT_HEADING in answers["check"]
    assert COST_MARKING in answers["agents"]
    assert measure(answering(answers), parity_root=PARITY, stream_root=STREAM) == []


def test_no_numeric_normalization(answers):
    regions = {
        "runs": re.sub(
            r"Coverage: (\d+) read",
            lambda m: f"Coverage: {int(m[1]) + 1} read",
            answers["runs"],
        ),
        "check": re.sub(
            r"(Unreadable lines in logs\.jsonl, skipped and left out of the totals check: )(\d+)",
            lambda m: f"{m[1]}{int(m[2]) + 1}",
            answers["check"],
        ),
    }
    for label, changed in regions.items():
        assert changed != answers[label], label
        differences = measure(
            answering(answers, changed={label: changed}), parity_root=PARITY, stream_root=STREAM
        )
        assert EXPECTED_FILES[label] in paths_of(differences), label


def test_text_and_json_compare_totals_are_both_compared(answers):
    text_lines = answers["agents --compare"].splitlines(keepends=True)
    totals = next(i for i, line in enumerate(text_lines) if line.startswith("Total cost_usd"))
    text_lines[totals] = one_more(text_lines[totals])
    changed_text = "".join(text_lines)
    changed_json = answers["agents --json --compare"].replace(
        '"total_cost_usd": 1.31395265', '"total_cost_usd": 1.31395266', 1
    )
    assert changed_json != answers["agents --json --compare"]
    for label, changed, expected_file in (
        ("agents --compare", changed_text, EXPECTED_FILES["agents --compare"]),
        ("agents --json --compare", changed_json, EXPECTED_FILES["agents --json --compare"]),
    ):
        differences = measure(
            answering(answers, changed={label: changed}), parity_root=PARITY, stream_root=STREAM
        )
        assert paths_of(differences) == {expected_file}, label


def test_nullable_context_entries_compare_as_stored(answers):
    label = next(label for label in answers if NULL_CONTEXT_SESSION in label)
    assert json.loads(answers[label])["context_size"] is None
    numeric = next(
        label
        for label in answers
        if label.startswith("context ") and NULL_CONTEXT_SESSION not in label
    )
    matched = measure(answering(answers), parity_root=PARITY, stream_root=STREAM)
    null_became_number = measure(
        answering(answers, changed={label: json.dumps({"context_size": 0})}),
        parity_root=PARITY,
        stream_root=STREAM,
    )
    number_became_null = measure(
        answering(answers, changed={numeric: json.dumps({"context_size": None})}),
        parity_root=PARITY,
        stream_root=STREAM,
    )
    assert matched == []
    assert paths_of(null_became_number) == {CONTEXT_FILE}
    assert paths_of(number_became_null) == {CONTEXT_FILE}


def test_shared_measure_reports_every_difference_not_just_the_first(answers):
    differences = measure(
        answering(
            answers,
            changed={
                "steps": one_more(answers["steps"]),
                "runs --json": one_more(answers["runs --json"]),
            },
        ),
        parity_root=PARITY,
        stream_root=STREAM,
    )
    assert paths_of(differences) == {EXPECTED_FILES["steps"], EXPECTED_FILES["runs --json"]}


def test_failed_invocation_is_an_error_not_an_empty_result(answers):
    with pytest.raises(MeasurementError) as caught:
        measure(
            answering(answers, failing={"check --json"}), parity_root=PARITY, stream_root=STREAM
        )
    assert caught.value.operation == "check --json"
    assert "did not complete" in caught.value.reason


def test_missing_expected_file_is_an_error(answers, tmp_path):
    reference = tmp_path / "tests"
    shutil.copytree(PARITY, reference / "parity")
    shutil.copytree(STREAM, reference / "stream_parity")
    (reference / "stream_parity" / "expected" / "compare.json").unlink()
    with pytest.raises(MeasurementError) as caught:
        measure(
            answering(answers),
            parity_root=reference / "parity",
            stream_root=reference / "stream_parity",
        )
    assert "compare.json" in caught.value.operation + caught.value.reason


def test_measure_prints_and_writes_nothing(answers, capsys):
    before = digest(PARITY, STREAM)
    measure(answering(answers), parity_root=PARITY, stream_root=STREAM)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert digest(PARITY, STREAM) == before


# ── the worker protocol: one metric, one result, and what the caller makes of a bad one ──


def _worker_result(metric: int, measurement) -> dict:
    """The JSON object a worker prints for `metric`, with `measurement` standing for it."""
    result = release_metrics.run_worker(
        metric, Path("/nowhere"), measurements={metric: measurement}
    )
    return json.loads(release_metrics.result_json(result))


def _read(metric: int, stdout, *, returncode=0, stderr=""):
    return release_metrics.read_result(metric, stdout, returncode=returncode, stderr=stderr)


def test_completed_worker_returns_metric_holds_and_diagnostics():
    assert _worker_result(7, lambda checkout: []) == {"metric": 7, "holds": True, "diagnostics": []}


def test_incomplete_measurement_returns_false_with_a_diagnostic():
    def cannot_finish(checkout):
        raise release_metrics.MeasurementIncomplete("python3 -m pytest x.py", "pytest is missing")

    result = _worker_result(7, cannot_finish)
    assert result["metric"] == 7 and result["holds"] is False
    [diagnostic] = result["diagnostics"]
    assert diagnostic["input_or_operation"] == "python3 -m pytest x.py"
    assert diagnostic["reason"] == "pytest is missing"


def test_a_measurement_that_finds_a_problem_returns_false_with_its_diagnostics():
    found = [
        release_metrics.Diagnostic("difference", "tests/parity/expected/runs.json", "off by one")
    ]
    result = _worker_result(7, lambda checkout: found)
    assert result["holds"] is False
    assert [d["input_or_operation"] for d in result["diagnostics"]] == [
        "tests/parity/expected/runs.json"
    ]


def test_absent_result_is_a_failed_measurement():
    for nothing in (None, "", "  \n"):
        result = _read(3, nothing)
        assert result.metric == 3 and result.holds is False
        assert "metric 3" in result.diagnostics[0].input_or_operation


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        json.dumps({"metric": 2, "holds": True, "diagnostics": []}),  # the wrong metric number
        json.dumps({"metric": 3, "holds": "yes", "diagnostics": []}),
        json.dumps({"metric": 3, "holds": True}),
        json.dumps({"metric": "3", "holds": True, "diagnostics": []}),
        json.dumps({"metric": 3, "holds": False, "diagnostics": []}),
        json.dumps(
            {
                "metric": 3,
                "holds": True,
                "diagnostics": [{"input_or_operation": "x", "reason": "y"}],
            }
        ),
        json.dumps({"metric": 3, "holds": False, "diagnostics": [{"reason": "no operation"}]}),
        json.dumps({"metric": 3, "holds": False, "diagnostics": ["a string"]}),
        json.dumps(["a", "list"]),
    ],
)
def test_malformed_result_is_a_failed_measurement(raw):
    result = _read(3, raw)
    assert result.metric == 3 and result.holds is False
    assert result.diagnostics


def test_a_well_formed_result_is_read_as_it_was_written():
    raw = json.dumps(
        {
            "metric": 3,
            "holds": False,
            "diagnostics": [{"input_or_operation": "msg_1", "reason": "twice", "kind": "k"}],
        }
    )
    result = _read(3, raw)
    assert (result.metric, result.holds) == (3, False)
    assert result.diagnostics[0].input_or_operation == "msg_1"
    assert result.diagnostics[0].reason == "twice"


def test_worker_crash_is_a_failed_measurement():
    crash = "Traceback (most recent call last):\nValueError: boom\n"
    result = _read(2, "", returncode=1, stderr=crash)
    assert result.holds is False
    assert "boom" in result.diagnostics[0].reason


def test_a_crash_after_a_result_was_printed_is_still_a_failed_measurement():
    raw = json.dumps({"metric": 2, "holds": True, "diagnostics": []})
    result = _read(2, raw, returncode=1, stderr="killed")
    assert result.holds is False


@pytest.mark.parametrize("metric", [4, 99])
def test_unknown_metric_number_is_refused(metric):
    result = release_metrics.run_worker(metric, Path("/nowhere"))
    assert result.holds is False
    assert result.diagnostics


def test_worker_never_imports_the_caller_checkout(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "ka_telemetry").mkdir(parents=True)
    (scratch / "ka_telemetry" / "__init__.py").write_text('WHERE = "scratch"\n', encoding="utf-8")
    shadow = tmp_path / "caller"
    (shadow / "ka_telemetry").mkdir(parents=True)
    (shadow / "ka_telemetry" / "__init__.py").write_text(
        'raise RuntimeError("imported the caller checkout")\n', encoding="utf-8"
    )
    program = (
        "import sys; sys.path.insert(0, sys.argv[1]); import release_metrics;"
        " release_metrics.root_imports_at(sys.argv[2]); import ka_telemetry;"
        " print(ka_telemetry.WHERE)"
    )
    done = subprocess.run(
        [sys.executable, "-c", program, str(PRODUCT_ROOT / "tools"), str(scratch)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert (done.returncode, done.stdout.strip()) == (0, "scratch"), done.stderr


# ── metric 1: the reference figures, measured from the checkout's source ──

TOOLS = PRODUCT_ROOT / "tools"
NOT_COPIED = shutil.ignore_patterns(
    ".git", ".worktrees", "__pycache__", ".pytest_cache", "*.egg-info", "build", "dist"
)


def scratch_copy(tmp_path: Path) -> Path:
    """A disposable copy of this checkout, for a worker to measure and for a test to damage."""
    copy = tmp_path / "scratch"
    shutil.copytree(PRODUCT_ROOT, copy, ignore=NOT_COPIED)
    return copy


def worker_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_worker_process(metric: int, checkout: Path, *, cwd: Path) -> release_metrics.WorkerResult:
    """The real worker, as the release check starts it, read the way the release check reads it."""
    done = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "release_metrics.py"),
            "worker",
            "--metric",
            str(metric),
            "--checkout",
            str(checkout),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=worker_env(),
        cwd=cwd,
    )
    return release_metrics.read_result(
        metric, done.stdout, returncode=done.returncode, stderr=done.stderr
    )


def edit(path: Path, change: Callable[[str], str]) -> None:
    before = path.read_text(encoding="utf-8")
    after = change(before)
    assert after != before, path
    path.write_text(after, encoding="utf-8")


def named(result: release_metrics.WorkerResult) -> str:
    return "\n".join(
        f"{diagnostic.input_or_operation}\n{diagnostic.reason}" for diagnostic in result.diagnostics
    )


def test_metric_1_holds_on_the_unbroken_reference_set(tmp_path):
    result = run_worker_process(1, scratch_copy(tmp_path), cwd=tmp_path)
    assert (result.metric, result.holds, result.diagnostics) == (1, True, ())


def test_metric_1_fails_naming_the_runs_json_when_one_figure_is_off_by_one(tmp_path):
    checkout = scratch_copy(tmp_path)
    edit(checkout / "tests/parity/expected/runs.json", lambda text: text.replace("1557", "1558", 1))
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert result.holds is False
    assert "tests/parity/expected/runs.json" in {d.input_or_operation for d in result.diagnostics}


@pytest.mark.parametrize(
    "expected_file",
    [
        "tests/parity/expected/context_sizes.json",
        "tests/parity/expected/runs.txt",
        "tests/parity/expected/steps.json",
        "tests/parity/expected/steps.txt",
        "tests/stream_parity/expected/agents.json",
        "tests/stream_parity/expected/agents.txt",
        "tests/stream_parity/expected/compare.json",
        "tests/stream_parity/expected/compare.txt",
    ],
)
def test_metric_1_fails_naming_each_other_expected_file(tmp_path, expected_file):
    # CP-546: any one figure of the file, the first one included; in the runs table that is the
    # Coverage line's `read`, a figure the accepted adjustments recompute rather than compare.
    checkout = scratch_copy(tmp_path)
    edit(checkout / expected_file, one_more)
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert result.holds is False
    assert expected_file in {d.input_or_operation for d in result.diagnostics}, named(result)


REPEATED_SESSION = "fac96b52-a71e-45e1-bec2-aa7a00d8b2a6"
"""A session the stored runs expectations hold twice, as two rows; the accepted adjustments keep
the first, because ka-telemetry prints one row for a session."""


def in_the_repeated_row(text: str) -> str:
    """`text` with the first figure of the repeated session's second row raised by one."""
    first = text.index(REPEATED_SESSION)
    second = text.index(REPEATED_SESSION, first + 1) + len(REPEATED_SESSION)
    return text[:second] + one_more(text[second:])


@pytest.mark.parametrize(
    ("expected_file", "change"),
    [
        ("tests/parity/expected/runs.json", one_more),
        ("tests/parity/expected/runs.txt", in_the_repeated_row),
        ("tests/parity/expected/runs.json", in_the_repeated_row),
    ],
    ids=["runs.json coverage read", "runs.txt repeated row", "runs.json repeated row"],
)
def test_metric_1_fails_on_a_runs_figure_the_accepted_adjustments_set_aside(
    tmp_path, expected_file, change
):
    """CP-546: the runs adjustments recompute `read` and keep one row of a session the stored
    output holds twice; a figure in either place is still a recorded figure, held to the one
    ka-telemetry gives."""
    checkout = scratch_copy(tmp_path)
    edit(checkout / expected_file, change)
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert result.holds is False
    assert expected_file in {d.input_or_operation for d in result.diagnostics}, named(result)


def test_metric_1_does_not_need_a_build(tmp_path):
    checkout = scratch_copy(tmp_path)
    (checkout / "pyproject.toml").write_text("this is [not valid toml\n", encoding="utf-8")
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert (result.holds, result.diagnostics) == (True, ())


SUITE_PARITY_TESTS = (
    "tests/test_parity.py::test_steps_output_equals_dev_in_both_forms_byte_for_byte",
    "tests/test_parity.py::test_runs_json_equals_dev_after_the_four_adjustments",
    "tests/test_parity.py::test_runs_table_equals_dev_line_for_line_after_the_four_adjustments",
    "tests/test_parity.py::test_context_size_equals_dev_for_every_transcript",
    "tests/e2e/test_stream_parity.py::test_installed_agents_output_equals_stored_oracle",
    "tests/e2e/test_stream_parity.py::test_installed_check_equals_stored_totals_block",
)


def suite_parity_holds(checkout: Path) -> bool:
    """The verdict of the suite's own installed-command parity comparisons on `checkout`."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *SUITE_PARITY_TESTS],
        capture_output=True,
        text=True,
        check=False,
        env=worker_env(),
        cwd=checkout,
    )
    return done.returncode == 0


def test_metric_1_agrees_with_the_suite_parity_comparisons(tmp_path):
    unbroken = tmp_path / "unbroken"
    broken = tmp_path / "broken"
    unbroken.mkdir()
    broken.mkdir()
    whole = scratch_copy(unbroken)
    damaged = scratch_copy(broken)
    edit(damaged / "tests/parity/expected/runs.json", lambda text: text.replace("1557", "1558", 1))
    edit(damaged / "tests/stream_parity/expected/compare.txt", one_more)
    verdicts = {
        "unbroken": (run_worker_process(1, whole, cwd=unbroken).holds, suite_parity_holds(whole)),
        "broken": (run_worker_process(1, damaged, cwd=broken).holds, suite_parity_holds(damaged)),
    }
    assert verdicts == {"unbroken": (True, True), "broken": (False, False)}


def test_metric_1_agrees_with_the_parity_tests_on_a_figure_the_adjustments_recompute(tmp_path):
    """CP-546: the runs table's Coverage `read` changed by one fails the suite's parity tests, as
    the spec's own verification ran them (the whole of `tests/test_parity.py`, in a clone, because
    one of its tests reads a commit of the product's history), and so it fails metric 1 too."""
    checkout = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(PRODUCT_ROOT), str(checkout)], check=True)
    shutil.copytree(PRODUCT_ROOT, checkout, ignore=NOT_COPIED, dirs_exist_ok=True)
    edit(checkout / "tests/parity/expected/runs.txt", one_more)
    parity_tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_parity.py"],
        capture_output=True,
        text=True,
        check=False,
        env=worker_env(),
        cwd=checkout,
    )
    metric_1 = run_worker_process(1, checkout, cwd=tmp_path)
    assert (metric_1.holds, parity_tests.returncode == 0) == (False, False), parity_tests.stdout


@pytest.mark.parametrize(
    "unreadable",
    [
        "tests/parity/expected/steps.txt",
        "tests/stream_parity/expected/compare.json",
        "tests/parity/agents.json",
        "tests/parity/named_sessions.json",
        "tests/parity/projects",
        "tests/stream_parity/logs.jsonl",
        "tests/stream_parity/metrics.jsonl",
    ],
)
def test_metric_1_unreadable_expected_file_fails_naming_it(tmp_path, unreadable):
    checkout = scratch_copy(tmp_path)
    target = checkout / unreadable
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert result.holds is False
    assert result.diagnostics
    assert unreadable in {d.input_or_operation for d in result.diagnostics}, named(result)


def test_metric_1_a_command_that_cannot_run_fails_naming_it_as_a_person_types_it(tmp_path):
    checkout = scratch_copy(tmp_path)
    edit(checkout / "ka_telemetry/cli.py", lambda text: "this is not python\n" + text)
    result = run_worker_process(1, checkout, cwd=tmp_path)
    assert result.holds is False
    [diagnostic] = result.diagnostics
    assert diagnostic.kind == release_metrics.KIND_COMMAND_FAILED
    assert diagnostic.input_or_operation.startswith("python3 -m ka_telemetry ")
    assert str(checkout) not in diagnostic.input_or_operation
    assert "SyntaxError" in diagnostic.reason


# ── metric 2: cost totals agree where recorded, and every exception has a cause ──

AGREEING_SESSION = "6547733b-af89-40a8-a524-2df28af94cae"
OUTSIDE_SESSION = "ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593"
NO_METRIC_SESSION = "7931ae7c-d999-4286-b091-261680002258"
STREAM_BASELINE = "tests/stream_parity/expected/agents.json"
CHECK_COMMAND = "python3 -m ka_telemetry check --dir tests/stream_parity --json"
CAUSE = "the session may have ended before Claude Code exported a cost metric for it"


def reported(session_id: str, within, causes=None) -> dict:
    """One session as `ka-telemetry check --json` reports it."""
    entry = {
        "session_id": session_id,
        "log_cost_usd": 0.05,
        "metric_cost_usd": None if within is None else 0.05,
        "within_tolerance": within,
    }
    entry["possible_causes"] = [CAUSE] if causes is None else causes
    return entry


def judged(agreeing, *entries) -> list[release_metrics.Diagnostic]:
    return release_metrics.judge_checks(
        set(agreeing), release_metrics.index_checks(list(entries), CHECK_COMMAND)
    )


def kinds_by_session(found) -> dict[str, set[str]]:
    kinds: dict[str, set[str]] = {}
    for diagnostic in found:
        kinds.setdefault(diagnostic.input_or_operation, set()).add(diagnostic.kind)
    return kinds


def scale_session_cost(path: Path, session_id: str, factor: float) -> None:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        for resource in record["body"].get("resourceMetrics", []):
            for scope in resource["scopeMetrics"]:
                for metric in scope["metrics"]:
                    for point in metric.get("sum", {}).get("dataPoints", []):
                        owners = {a["value"]["stringValue"] for a in point["attributes"]}
                        if owners == {session_id} and "asDouble" in point:
                            point["asDouble"] *= factor
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_metric_2_holds_on_the_unbroken_reference_set(tmp_path):
    result = run_worker_process(2, scratch_copy(tmp_path), cwd=tmp_path)
    assert (result.metric, result.holds, result.diagnostics) == (2, True, ())
    recorded = json.loads((STREAM / "expected/agents.json").read_text(encoding="utf-8"))
    by_id = {entry["session_id"]: entry["within_tolerance"] for entry in recorded["metrics_check"]}
    assert by_id[OUTSIDE_SESSION] is False and by_id[NO_METRIC_SESSION] is None


def test_metric_2_fails_naming_the_session_when_its_cost_metric_moves_two_percent(tmp_path):
    checkout = scratch_copy(tmp_path)
    scale_session_cost(checkout / "tests/stream_parity/metrics.jsonl", AGREEING_SESSION, 1.02)
    result = run_worker_process(2, checkout, cwd=tmp_path)
    assert result.holds is False
    assert AGREEING_SESSION in {d.input_or_operation for d in result.diagnostics}, named(result)
    assert {d.kind for d in result.diagnostics} >= {release_metrics.KIND_SESSION_APART}


def test_metric_2_fails_when_a_recorded_agreeing_session_is_reported_null_or_missing():
    absent = judged([AGREEING_SESSION], reported("someone-else", True))
    no_metric = judged([AGREEING_SESSION], reported(AGREEING_SESSION, None))
    apart = judged([AGREEING_SESSION], reported(AGREEING_SESSION, False))
    assert kinds_by_session(absent) == {AGREEING_SESSION: {release_metrics.KIND_SESSION_MISSING}}
    assert release_metrics.KIND_SESSION_NO_METRIC in kinds_by_session(no_metric)[AGREEING_SESSION]
    assert release_metrics.KIND_SESSION_APART in kinds_by_session(apart)[AGREEING_SESSION]


@pytest.mark.parametrize("not_a_decision", ["true", 1, 0, "yes"])
def test_metric_2_only_true_false_or_null_is_a_decision_of_the_check(not_a_decision):
    with pytest.raises(release_metrics.MeasurementIncomplete) as caught:
        judged([AGREEING_SESSION], reported(AGREEING_SESSION, not_a_decision))
    assert caught.value.diagnostic.input_or_operation == CHECK_COMMAND


@pytest.mark.parametrize("causes", [[], [""], None, "a cause that is not in a list"])
def test_metric_2_fails_when_an_outside_session_has_no_cause(causes):
    for within, kind in (
        (False, release_metrics.KIND_APART_WITHOUT_CAUSE),
        (None, release_metrics.KIND_NO_METRIC_WITHOUT_CAUSE),
    ):
        entry = reported(OUTSIDE_SESSION, within, [])
        if causes is None:
            del entry["possible_causes"]
        else:
            entry["possible_causes"] = causes
        assert kinds_by_session(judged([], entry)) == {OUTSIDE_SESSION: {kind}}


def test_metric_2_outside_sessions_with_a_cause_hold_whether_or_not_they_are_recorded():
    outside = reported(OUTSIDE_SESSION, False)
    no_metric = reported(NO_METRIC_SESSION, None)
    assert judged([], outside, no_metric) == []
    assert judged([AGREEING_SESSION], reported(AGREEING_SESSION, True), outside, no_metric) == []


def test_metric_2_decides_from_check_results_without_its_own_cost_formula():
    # 2% apart by the figures, yet the check's own decision is "within": that decision stands.
    within_by_its_own_word = {**reported(AGREEING_SESSION, True), "metric_cost_usd": 0.0511}
    assert judged([AGREEING_SESSION], within_by_its_own_word) == []
    # equal figures, yet the check's decision is "outside": again the check's word decides.
    outside_by_its_own_word = {**reported(AGREEING_SESSION, False), "metric_cost_usd": 0.05}
    assert AGREEING_SESSION in kinds_by_session(judged([AGREEING_SESSION], outside_by_its_own_word))


@pytest.mark.parametrize(
    "baseline",
    [None, "not json", "[]", "{}", '{"metrics_check": "x"}', '{"metrics_check": [{"a": 1}]}'],
)
def test_metric_2_fails_on_missing_or_malformed_baseline(tmp_path, baseline):
    checkout = scratch_copy(tmp_path)
    path = checkout / STREAM_BASELINE
    if baseline is None:
        path.unlink()
    else:
        path.write_text(baseline, encoding="utf-8")
    result = run_worker_process(2, checkout, cwd=tmp_path)
    assert result.holds is False
    assert STREAM_BASELINE in {d.input_or_operation for d in result.diagnostics}, named(result)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"metrics_check": "x"}',
        '{"metrics_check": [{"session_id": "a", "within_tolerance": "maybe"}]}',
        '{"metrics_check": [{"within_tolerance": true}]}',
        json.dumps(
            {
                "metrics_check": [
                    {"session_id": "a", "within_tolerance": True},
                    {"session_id": "a", "within_tolerance": False},
                ]
            }
        ),
    ],
)
def test_metric_2_fails_on_malformed_check_output(output):
    with pytest.raises(release_metrics.MeasurementIncomplete) as caught:
        release_metrics.read_check_output(output, CHECK_COMMAND)
    assert caught.value.diagnostic.input_or_operation == CHECK_COMMAND


def test_metric_2_fails_when_the_check_command_cannot_run(tmp_path):
    checkout = scratch_copy(tmp_path)
    edit(checkout / "ka_telemetry/cli.py", lambda text: "this is not python\n" + text)
    result = run_worker_process(2, checkout, cwd=tmp_path)
    assert result.holds is False
    [diagnostic] = result.diagnostics
    assert diagnostic.input_or_operation == CHECK_COMMAND
    assert diagnostic.kind == release_metrics.KIND_COMMAND_FAILED


# ── metric 3: no response is counted more than once by the runs or the steps report ──

CROSS_FILE_RESPONSE = "msg_011Cfh6d1FVJV4chymjSkBss"
RUNS_SOURCE_SESSION = "a839285c-263f-40a6-8801-71f49d00cc68"
RUNS_TARGET_SESSION = "15b8016a-f529-44fe-b6ea-1013fea8a4a8"
FIRST_STEP_AGENT = "a53ef7efd8c468bad"
SECOND_STEP_AGENT = "a85c5919a45174165"


def parity_checkout(tmp_path: Path) -> Path:
    """A directory shaped like a checkout that holds only a copy of `tests/parity`."""
    checkout = tmp_path / "checkout"
    shutil.copytree(PARITY, checkout / "tests" / "parity")
    return checkout


def run_transcript(checkout: Path, session_id: str) -> Path:
    [found] = (checkout / "tests/parity/projects").glob(f"*/{session_id}.jsonl")
    return found


def agent_transcript(checkout: Path, agent_id: str) -> Path:
    [found] = (checkout / "tests/parity/projects").glob(
        f"*/*/subagents/workflows/*/agent-{agent_id}.jsonl"
    )
    return found


def response_lines(path: Path, response_id: str) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("message", {}).get("id") == response_id
    ]


def first_response_id(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "assistant" and record["message"].get("id"):
            return record["message"]["id"]
    raise AssertionError(f"no response in {path}")


def copy_response(source: Path, target: Path, response_id: str) -> None:
    lines = response_lines(source, response_id)
    assert lines, (source, response_id)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def plain_runs_report(checkout: Path):
    """The runs report as the reference commands read it, with nothing observing."""
    from ka_telemetry import run_usage

    parity = checkout / "tests" / "parity"
    reader = run_usage.Reader(
        files=run_usage.Files(),
        processes=run_usage.Processes(),
        projects_root=parity / "projects",
    )
    sessions = json.loads((parity / "named_sessions.json").read_text(encoding="utf-8"))
    listing = (parity / "agents.json").read_text(encoding="utf-8")
    return run_usage.read_runs(listing, sessions, reader)


def plain_step_rows(checkout: Path):
    from ka_telemetry import flow_usage

    reader = flow_usage.Reader(
        files=flow_usage.Files(), projects_root=checkout / "tests" / "parity" / "projects"
    )
    return [row for run in flow_usage.read_steps(reader).runs for row in flow_usage.aggregate(run)]


def audited(report: str, checkout: Path):
    return release_metrics.audit_report(report, checkout)


def repeats_of(audit) -> dict[str, tuple[str, ...]]:
    return {repeat.response_id: repeat.transcripts for repeat in audit.repeats}


def test_metric_3_holds_on_the_unbroken_reference_set(tmp_path):
    result = run_worker_process(3, scratch_copy(tmp_path), cwd=tmp_path)
    assert (result.metric, result.holds, result.diagnostics) == (3, True, ())


def test_metric_3_holds_for_a_response_copied_into_a_second_runs_transcript(tmp_path):
    from ka_telemetry import run_usage

    checkout = scratch_copy(tmp_path)
    copy_response(
        run_transcript(checkout, RUNS_SOURCE_SESSION),
        run_transcript(checkout, RUNS_TARGET_SESSION),
        CROSS_FILE_RESPONSE,
    )

    result = run_worker_process(3, checkout, cwd=tmp_path)
    audit = audited("runs", checkout)
    plain = plain_runs_report(checkout)

    assert (result.metric, result.holds, result.diagnostics) == (3, True, ())
    assert repeats_of(audit) == {}
    assert run_usage.render_json(audit.reading) == run_usage.render_json(plain)
    assert run_usage.render(audit.reading) == run_usage.render(plain)
    assert plain.coverage.responses_in_more_than_one_transcript == 1


@contextlib.contextmanager
def damaged_reader(damage):
    """`run_usage.usage_records` replaced by `damage(original)` while the block runs, so the audit
    observes the damaged reader as it observes the real one."""
    from ka_telemetry import run_usage

    original = run_usage.usage_records
    run_usage.usage_records = damage(original)
    try:
        yield run_usage.usage_records
    finally:
        run_usage.usage_records = original


def forgetting_the_scope(original):
    """A reader that no longer suppresses a response a row before it has counted."""

    def usage_records(path, files, **arguments):
        return original(path, files, **{**arguments, "response_scope": None})

    return usage_records


def losing_the_first_response(original):
    """A reader that accepts a response and never yields it."""
    state = {"lost": False}

    def usage_records(path, files, **arguments):
        for triple in original(path, files, **arguments):
            if not state["lost"]:
                state["lost"] = True
                continue
            yield triple

    return usage_records


def test_metric_3_names_the_id_and_transcripts_when_the_runs_report_counts_one_response_under_two_rows(  # noqa: E501
    tmp_path,
):
    checkout = parity_checkout(tmp_path)
    source = run_transcript(checkout, RUNS_SOURCE_SESSION)
    target = run_transcript(checkout, RUNS_TARGET_SESSION)
    copy_response(source, target, CROSS_FILE_RESPONSE)
    assert repeats_of(audited("runs", checkout)) == {}

    with damaged_reader(forgetting_the_scope) as damaged:
        from ka_telemetry import run_usage

        found = repeats_of(audited("runs", checkout))
        assert run_usage.usage_records is damaged  # the audit put back what it found

    assert set(found) == {CROSS_FILE_RESPONSE}
    assert found[CROSS_FILE_RESPONSE] == tuple(
        sorted(path.relative_to(checkout).as_posix() for path in (source, target))
    )


def test_metric_3_still_stops_when_an_accepted_response_reaches_no_row(tmp_path):
    checkout = parity_checkout(tmp_path)

    with damaged_reader(losing_the_first_response):
        with pytest.raises(release_metrics.MeasurementIncomplete) as stopped:
            audited("runs", checkout)

    assert "accepted responses reached no row" in stopped.value.diagnostic.reason


def steps_reading_equals_the_plain_one(checkout: Path) -> bool:
    from ka_telemetry import flow_usage

    reader = flow_usage.Reader(
        files=flow_usage.Files(), projects_root=checkout / "tests" / "parity" / "projects"
    )
    audit = audited("steps", checkout)
    plain = flow_usage.read_steps(reader)
    return flow_usage.render_json(audit.reading.runs) == flow_usage.render_json(plain.runs)


def twin_agent_in_the_first_agents_row(checkout: Path) -> tuple[Path, str]:
    """A second agent the journal gives the first agent's step and role, so the two are one row;
    the transcript it holds is empty. Returns that transcript and its agent id."""
    source = agent_transcript(checkout, FIRST_STEP_AGENT)
    workflow = source.parent
    twin = "a0000000000000001"
    journal = workflow / "journal.jsonl"
    named = next(
        line
        for line in journal.read_text().splitlines()
        if FIRST_STEP_AGENT in line and "result" in line
    )
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(named.replace(FIRST_STEP_AGENT, twin) + "\n")
    (workflow / f"agent-{twin}.jsonl").write_text("", encoding="utf-8")
    return workflow / f"agent-{twin}.jsonl", twin


def test_metric_3_holds_for_a_response_copied_between_two_workflow_agent_transcripts(tmp_path):
    checkout = parity_checkout(tmp_path)
    source = agent_transcript(checkout, FIRST_STEP_AGENT)
    response = first_response_id(source)

    # across step rows
    copy_response(source, agent_transcript(checkout, SECOND_STEP_AGENT), response)
    assert repeats_of(audited("runs", checkout)) == {}
    assert repeats_of(audited("steps", checkout)) == {}
    assert steps_reading_equals_the_plain_one(checkout)

    # between agents of the same row
    twin, _ = twin_agent_in_the_first_agents_row(checkout)
    copy_response(source, twin, response)
    assert repeats_of(audited("steps", checkout)) == {}
    assert steps_reading_equals_the_plain_one(checkout)


def test_metric_3_holds_for_a_response_copied_into_a_second_workflow_run(tmp_path):
    from ka_telemetry import flow_usage

    checkout = parity_checkout(tmp_path)
    workflow = agent_transcript(checkout, FIRST_STEP_AGENT).parent
    second = workflow.parents[3] / "sid-two" / "subagents" / "workflows" / "wf_two"
    shutil.copytree(workflow, second)

    audit = audited("steps", checkout)

    assert repeats_of(audit) == {}
    assert len(audit.reading.runs) == 2
    assert steps_reading_equals_the_plain_one(checkout)
    assert flow_usage.coverage(audit.reading.runs[0]).responses_in_more_than_one_transcript > 0


def test_metric_3_names_the_id_and_transcripts_when_the_steps_report_counts_a_response_twice(  # noqa: E501
    tmp_path,
):
    checkout = hand_built_steps(
        tmp_path,
        {"a1": (STEP_X, [hand_response("msg_s")]), "a2": (STEP_Y, [hand_response("msg_s")])},
    )

    with damaged_reader(forgetting_the_scope):
        found = repeats_of(audited("steps", checkout))

    run_dir = "tests/parity/projects/slug/sid/subagents/workflows/wf_hand"
    assert found == {"msg_s": (f"{run_dir}/agent-a1.jsonl", f"{run_dir}/agent-a2.jsonl")}


def test_metric_3_detects_a_duplicate_across_two_rows(tmp_path):
    checkout = parity_checkout(tmp_path)
    source = agent_transcript(checkout, FIRST_STEP_AGENT)
    target = agent_transcript(checkout, SECOND_STEP_AGENT)
    response = first_response_id(source)
    copy_response(source, target, response)

    with damaged_reader(forgetting_the_scope):
        found = repeats_of(audited("steps", checkout))

    assert set(found) == {response}
    assert found[response] == tuple(
        sorted(path.relative_to(checkout).as_posix() for path in (source, target))
    )


def test_metric_3_detects_a_duplicate_inside_one_aggregate_row(tmp_path):
    checkout = parity_checkout(tmp_path)
    source = agent_transcript(checkout, FIRST_STEP_AGENT)
    response = first_response_id(source)
    twin, twin_id = twin_agent_in_the_first_agents_row(checkout)
    copy_response(source, twin, response)
    rows = plain_step_rows(checkout)
    assert any(set(row.agent_ids) == {FIRST_STEP_AGENT, twin_id} for row in rows)

    with damaged_reader(forgetting_the_scope):
        found = repeats_of(audited("steps", checkout))

    assert set(found) == {response}
    assert found[response] == tuple(
        sorted(path.relative_to(checkout).as_posix() for path in (source, twin))
    )


def hand_built_steps(tmp_path: Path, agents: dict[str, tuple[dict, list[str]]]) -> Path:
    """A checkout whose one workflow run has the agents given, `{agent_id: (step result, lines)}`,
    named by its journal in that order."""
    checkout = tmp_path / "checkout"
    run_dir = checkout / "tests/parity/projects/slug/sid/subagents/workflows/wf_hand"
    run_dir.mkdir(parents=True)
    journal = []
    for agent_id, (result, lines) in agents.items():
        journal += [
            json.dumps({"type": "started", "key": f"v2:{agent_id}", "agentId": agent_id}),
            json.dumps(
                {"type": "result", "key": f"v2:{agent_id}", "agentId": agent_id, "result": result}
            ),
        ]
        (run_dir / f"agent-{agent_id}.jsonl").write_text("".join(f"{x}\n" for x in lines))
    (run_dir / "journal.jsonl").write_text("\n".join(journal) + "\n")
    return checkout


def hand_response(response_id: str, tokens: int = 1) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-10-01T10:00:00.000Z",
            "message": {
                "id": response_id,
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": tokens, "output_tokens": 1},
            },
        }
    )


STEP_X = {"step": "x", "status": "ok", "artifact": "x"}
STEP_Y = {"step": "y", "status": "ok", "artifact": "y"}


def test_metric_3_matches_contributions_to_transcripts_when_group_order_is_not_agent_order(
    tmp_path,
):
    # a2 and a3 share msg_s; the report reads a3 (row x) before a2 (row y)
    checkout = hand_built_steps(
        tmp_path,
        {
            "a1": (STEP_X, [hand_response("m1")]),
            "a2": (STEP_Y, [hand_response("msg_s", 5)]),
            "a3": (STEP_X, [hand_response("msg_s", 5)]),
        },
    )

    held = audited("steps", checkout)
    assert repeats_of(held) == {} and held.counted == 2

    with damaged_reader(forgetting_the_scope):
        found = repeats_of(audited("steps", checkout))
    run_dir = "tests/parity/projects/slug/sid/subagents/workflows/wf_hand"
    assert found == {"msg_s": (f"{run_dir}/agent-a2.jsonl", f"{run_dir}/agent-a3.jsonl")}


def test_metric_3_empty_agents_invent_no_response(tmp_path):
    checkout = hand_built_steps(
        tmp_path,
        {"a1": (STEP_X, []), "a2": (STEP_Y, [hand_response("m2")]), "a3": (STEP_Y, [])},
    )

    audit = audited("steps", checkout)

    assert repeats_of(audit) == {} and audit.counted == 1


def test_metric_3_steps_still_stops_when_an_accepted_response_reaches_no_row(tmp_path):
    both = [hand_response("m1"), hand_response("m2")]
    checkout = hand_built_steps(tmp_path, {"a1": (STEP_X, both)})

    with damaged_reader(losing_the_first_response):
        with pytest.raises(release_metrics.MeasurementIncomplete) as stopped:
            audited("steps", checkout)

    assert "accepted responses reached no row" in stopped.value.diagnostic.reason


def test_metric_3_restores_the_observers_after_a_steps_audit_stops(tmp_path):
    from ka_telemetry import run_usage

    originals = (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model)
    checkout = hand_built_steps(tmp_path, {"a1": (STEP_X, [hand_response("m1")])})

    with damaged_reader(losing_the_first_response) as damaged:
        with pytest.raises(release_metrics.MeasurementIncomplete):
            audited("steps", checkout)
        assert run_usage.usage_records is damaged
    audited("steps", checkout)

    assert (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model) == originals


def repeating_the_first_response(original):
    """A reader that, after its selection gate, hands the first response it accepted over a
    second time in the same read: the row really counts it twice, and no transcript holds a
    copy of it."""
    state = {"repeated": False}

    def usage_records(path, files, **arguments):
        for triple in original(path, files, **arguments):
            yield triple
            if not state["repeated"]:
                state["repeated"] = True
                yield triple

    return usage_records


def carrying_a_response_into_the_next_read(original):
    """A reader that, after its selection gate, hands the first response it accepted over again
    at the end of the next transcript it reads: one response counted under two rows, though only
    one transcript holds it."""
    state = {"carried": None, "from": None, "done": False}

    def usage_records(path, files, **arguments):
        for triple in original(path, files, **arguments):
            if state["carried"] is None:
                state["carried"], state["from"] = triple, Path(path)
            yield triple
        if not state["done"] and state["carried"] is not None and Path(path) != state["from"]:
            state["done"] = True
            yield state["carried"]

    return usage_records, state


def yielding_a_response_no_record_carried(original):
    """A reader that hands over one response no transcript record it read was accepted as: its
    usage is a copy made after the read, so nothing the audit watched produced it."""
    state = {"done": False}

    def usage_records(path, files, **arguments):
        for usage, model, timestamp in original(path, files, **arguments):
            yield usage, model, timestamp
            if not state["done"]:
                state["done"] = True
                yield dict(usage), model, timestamp

    return usage_records


def test_metric_3_names_a_response_the_runs_report_counts_under_two_rows_with_no_copy(tmp_path):
    # CP-592: the report counts twice, and no transcript was given a copy of the response
    checkout = parity_checkout(tmp_path)
    damage, state = None, None

    def carrying(original):
        nonlocal damage, state
        damage, state = carrying_a_response_into_the_next_read(original)
        return damage

    with damaged_reader(carrying):
        audit = audited("runs", checkout)

    found = repeats_of(audit)
    assert len(found) == 1, found
    ((response, transcripts),) = found.items()
    assert transcripts == (state["from"].relative_to(checkout).as_posix(),)
    assert response in state["from"].read_text(encoding="utf-8")
    assert sum(1 for row in audit.reading.rows if row.spend) > 1


def test_metric_3_names_a_response_the_steps_report_counts_twice_inside_one_row_with_no_copy(
    tmp_path,
):
    # CP-592: one step row counts one response twice; no transcript holds a copy of it
    checkout = hand_built_steps(
        tmp_path,
        {
            "a1": (STEP_X, [hand_response("m1"), hand_response("m2")]),
            "a2": (STEP_X, [hand_response("m3")]),
        },
    )

    with damaged_reader(repeating_the_first_response):
        audit = audited("steps", checkout)

    run_dir = "tests/parity/projects/slug/sid/subagents/workflows/wf_hand"
    assert repeats_of(audit) == {"m1": (f"{run_dir}/agent-a1.jsonl",)}
    assert audit.counted == 4


@pytest.mark.parametrize("report", ["runs", "steps"])
def test_metric_3_stops_when_a_report_counts_a_response_no_transcript_read_accepted(
    tmp_path, report
):
    # an unknown-provenance contribution is a count the audit cannot account for: it stops as
    # an unfinished count, never crashing and never passing over it
    from ka_telemetry import run_usage

    checkout = parity_checkout(tmp_path)
    originals = (run_usage._contribution, run_usage.spend_by_model)

    with damaged_reader(yielding_a_response_no_record_carried) as damaged:
        with pytest.raises(release_metrics.MeasurementIncomplete) as stopped:
            audited(report, checkout)
        assert run_usage.usage_records is damaged

    assert stopped.value.diagnostic.kind == release_metrics.KIND_COUNT_UNFINISHED
    assert stopped.value.diagnostic.data == {"report": report}
    assert (run_usage._contribution, run_usage.spend_by_model) == originals


def test_metric_3_counts_a_repeated_content_block_in_one_file_once(tmp_path):
    checkout = parity_checkout(tmp_path)
    target = run_transcript(checkout, RUNS_SOURCE_SESSION)
    before = audited("runs", checkout)
    copy_response(target, target, CROSS_FILE_RESPONSE)
    after = audited("runs", checkout)
    assert repeats_of(after) == {}
    assert after.counted == before.counted


def test_metric_3_treats_separate_id_less_records_as_separate_responses(tmp_path):
    checkout = parity_checkout(tmp_path)
    target = run_transcript(checkout, RUNS_SOURCE_SESSION)
    before = audited("runs", checkout)
    record = {
        "type": "assistant",
        "timestamp": "2026-10-04T11:13:00.000Z",
        "message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 1, "output_tokens": 1}},
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n" + json.dumps(record) + "\n")
    after = audited("runs", checkout)
    assert repeats_of(after) == {}
    assert after.counted == before.counted + 2


def test_metric_3_identical_ids_across_the_two_reports_are_not_a_duplicate(tmp_path):
    checkout = parity_checkout(tmp_path)
    source = agent_transcript(checkout, FIRST_STEP_AGENT)
    copy_response(source, run_transcript(checkout, RUNS_TARGET_SESSION), first_response_id(source))
    assert repeats_of(audited("runs", checkout)) == {}
    assert repeats_of(audited("steps", checkout)) == {}


def test_metric_3_ignores_a_duplicate_in_a_file_the_report_did_not_select(tmp_path):
    checkout = parity_checkout(tmp_path)
    source = run_transcript(checkout, RUNS_SOURCE_SESSION)
    unselected = source.parent / "11111111-1111-4111-8111-111111111111.jsonl"
    unselected.write_text("\n".join(response_lines(source, CROSS_FILE_RESPONSE)) + "\n")
    workflow = agent_transcript(checkout, FIRST_STEP_AGENT).parent
    orphan = workflow / "agent-a0000000000000002.jsonl"
    orphan.write_text("\n".join(response_lines(source, CROSS_FILE_RESPONSE)) + "\n")
    assert repeats_of(audited("runs", checkout)) == {}
    assert repeats_of(audited("steps", checkout)) == {}


@pytest.mark.parametrize(
    "unreadable",
    ["tests/parity/agents.json", "tests/parity/named_sessions.json", "tests/parity/projects"],
)
def test_metric_3_fails_when_a_required_input_is_unreadable(tmp_path, unreadable):
    checkout = scratch_copy(tmp_path)
    target = checkout / unreadable
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    result = run_worker_process(3, checkout, cwd=tmp_path)
    assert result.holds is False
    assert unreadable in {d.input_or_operation for d in result.diagnostics}, named(result)


def test_metric_3_restores_the_observation_wrapper(tmp_path):
    from ka_telemetry import run_usage

    originals = (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model)
    checkout = parity_checkout(tmp_path)
    audit = audited("runs", checkout)
    assert (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model) == originals
    plain = plain_runs_report(checkout)
    assert run_usage.render_json(audit.reading) == run_usage.render_json(plain)
    audited("steps", checkout)
    assert (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model) == originals


def test_metric_3_restores_the_wrapper_when_the_audit_stops_early(tmp_path):
    from ka_telemetry import run_usage

    originals = (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model)
    checkout = parity_checkout(tmp_path)
    shutil.rmtree(checkout / "tests/parity/projects")
    with pytest.raises(release_metrics.MeasurementIncomplete):
        audited("runs", checkout)
    assert (run_usage.usage_records, run_usage._contribution, run_usage.spend_by_model) == originals


# ── metric 8: every example in the README and the how-to runs as written ──

EXAMPLES_FILE = "tests/e2e/test_documented_examples.py"
README_EXAMPLE_FIGURE = "cost_usd=0.051883"


def tracked_copy(tmp_path: Path) -> Path:
    """A scratch copy that is also a Git working tree with everything tracked, because the
    examples test asserts that the how-to is tracked. The release check's own copy is a clone."""
    copy = scratch_copy(tmp_path)
    subprocess.run(["git", "-C", str(copy), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(copy), "add", "--all"], check=True)
    return copy


def examples_verdict(checkout: Path) -> bool:
    """The examples file's own verdict on `checkout`, from pytest run as a person would run it."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", EXAMPLES_FILE, "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        env=worker_env(),
        cwd=checkout,
    )
    return done.returncode == 0


def test_metric_8_holds_when_the_examples_file_passes(tmp_path):
    checkout = tracked_copy(tmp_path)
    result = run_worker_process(8, checkout, cwd=tmp_path)
    assert (result.metric, result.holds, result.diagnostics) == (8, True, ())
    assert examples_verdict(checkout) is True


@pytest.mark.parametrize(
    ("document", "name"), [("README.md", "README.md"), ("docs/how-to.md", "docs/how-to.md")]
)
def test_metric_8_fails_naming_the_document_that_holds_the_changed_example(
    tmp_path, document, name
):
    checkout = tracked_copy(tmp_path)
    edit(checkout / document, lambda text: text.replace(README_EXAMPLE_FIGURE, "cost_usd=0.051884"))
    result = run_worker_process(8, checkout, cwd=tmp_path)
    assert result.holds is False
    assert examples_verdict(checkout) is False
    [diagnostic] = result.diagnostics
    assert diagnostic.kind == release_metrics.KIND_EXAMPLE_FAILED
    assert diagnostic.input_or_operation == name
    assert diagnostic.data["document"] == name
    assert "0.051884" in diagnostic.data["detail"]
    assert str(checkout) not in diagnostic.data["detail"]


def test_metric_8_selects_the_entire_examples_file_and_only_it(tmp_path):
    command = release_metrics.examples_command(tmp_path, module="pytest")
    assert command[1:3] == ["-m", "pytest"]
    selected = [
        argument for argument in command[3:] if not argument.startswith("-") and "=" not in argument
    ]
    assert selected == [EXAMPLES_FILE]
    for narrowing in ("-k", "-m", "--deselect", "--lf", "--ff"):
        assert narrowing not in command[3:]
    assert "::" not in " ".join(command)


def test_metric_8_build_failure_names_the_build_step_and_reason(tmp_path):
    checkout = tracked_copy(tmp_path)
    edit(
        checkout / "pyproject.toml",
        lambda text: text.replace('requires = ["setuptools>=77,<90"]', "requires = []").replace(
            'build-backend = "setuptools.build_meta"', 'build-backend = "no_such_backend"'
        ),
    )
    before = (checkout / "ka_telemetry" / "run_usage.py").read_bytes()
    result = run_worker_process(8, checkout, cwd=tmp_path)
    assert result.holds is False
    [diagnostic] = result.diagnostics
    assert diagnostic.kind == release_metrics.KIND_BUILD_FAILED
    assert "building" in diagnostic.input_or_operation
    assert "no_such_backend" in diagnostic.reason
    assert (checkout / "ka_telemetry" / "run_usage.py").read_bytes() == before


def test_metric_8_missing_pytest_cannot_hold(tmp_path):
    found = release_metrics.measure_examples(
        tracked_copy(tmp_path), module="no_such_pytest_module"
    )
    [diagnostic] = found
    assert diagnostic.kind == release_metrics.KIND_PYTEST_MISSING
    assert diagnostic.data["python"] == sys.executable


def test_metric_8_missing_test_file_cannot_hold(tmp_path):
    checkout = tracked_copy(tmp_path)
    (checkout / EXAMPLES_FILE).unlink()
    [diagnostic] = release_metrics.measure_examples(checkout, module="pytest")
    assert diagnostic.kind == release_metrics.KIND_COMMAND_FAILED
    assert diagnostic.input_or_operation == "python3 -m pytest " + EXAMPLES_FILE


def test_metric_8_failed_collection_cannot_hold(tmp_path):
    checkout = tracked_copy(tmp_path)
    (checkout / EXAMPLES_FILE).write_text("def (:\n", encoding="utf-8")
    [diagnostic] = release_metrics.measure_examples(checkout, module="pytest")
    assert diagnostic.kind == release_metrics.KIND_COMMAND_FAILED
    assert "SyntaxError" in diagnostic.reason


def test_metric_8_an_examples_file_that_selects_nothing_cannot_hold(tmp_path):
    checkout = tracked_copy(tmp_path)
    (checkout / EXAMPLES_FILE).write_text("", encoding="utf-8")
    [diagnostic] = release_metrics.measure_examples(checkout, module="pytest")
    assert diagnostic.kind == release_metrics.KIND_COMMAND_FAILED


# ── the release-check command's own seams: ordering, exit status and where children write ──


def check_module():
    """`tools/release_check.py`, imported when a test needs it."""
    from tools import release_check

    return release_check


def passing(registry) -> dict[int, release_metrics.WorkerResult]:
    return {metric: release_metrics.WorkerResult(metric, True) for metric in registry}


def test_registry_accepts_a_new_metric_without_renaming_existing_lines():
    check = check_module()
    registry = {**check.DESCRIPTIONS, 9: "A stub bar nine", 4: "A stub bar four"}
    report = check.render_report(registry, passing(registry))
    assert [int(line.split(" ", 1)[0]) for line in report.stdout_lines] == [1, 2, 3, 4, 5, 7, 8, 9]
    by_id = {int(line.split(" ", 1)[0]): line for line in report.stdout_lines}
    for metric, words in check.DESCRIPTIONS.items():
        assert by_id[metric] == f"{metric} {words}: holds"
    assert by_id[9] == "9 A stub bar nine: holds"


def test_the_registry_has_bars_5_and_7_and_the_workers_measure_them():
    check = check_module()
    assert sorted(check.DESCRIPTIONS) == [1, 2, 3, 5, 7, 8]
    assert sorted(release_metrics.MEASUREMENTS) == [1, 2, 3, 5, 7, 8]


NEW_DESCRIPTIONS = {
    5: (
        "The README's install command, run as written on a Python that refuses pip installs as "
        "externally managed, installs the ka-telemetry built from this checkout and no other "
        "package, setting pip, setuptools and wheel aside"
    ),
    7: (
        "No ka-telemetry command that the release check runs tries to reach beyond this machine, "
        "and the receiver started with no --host listens only on this machine"
    ),
}


def test_description_5_is_the_published_words_whichever_verdict_follows():
    check = check_module()
    assert check.DESCRIPTIONS[5] == NEW_DESCRIPTIONS[5]
    for holds in (True, False):
        results = passing(check.DESCRIPTIONS)
        diagnostic = release_metrics.Diagnostic("stopped", "x", "y")
        results[5] = release_metrics.WorkerResult(5, holds, () if holds else (diagnostic,))
        report = check.render_report(check.DESCRIPTIONS, results)
        assert f"5 {NEW_DESCRIPTIONS[5]}: {'holds' if holds else 'fails'}" in report.stdout_lines


def test_description_7_is_the_published_words_whichever_verdict_follows():
    check = check_module()
    for metric, words in ((7, NEW_DESCRIPTIONS[7]),):
        assert check.DESCRIPTIONS[metric] == words
        for holds in (True, False):
            results = passing(check.DESCRIPTIONS)
            diagnostic = release_metrics.Diagnostic("stopped", "x", "y")
            results[metric] = release_metrics.WorkerResult(
                metric, holds, () if holds else (diagnostic,)
            )
            report = check.render_report(check.DESCRIPTIONS, results)
            assert f"{metric} {words}: {'holds' if holds else 'fails'}" in report.stdout_lines


def test_the_report_has_exactly_six_lines_sorted_by_number():
    check = check_module()
    report = check.render_report(check.DESCRIPTIONS, passing(check.DESCRIPTIONS))
    assert [line.split(" ", 1)[0] for line in report.stdout_lines] == list("123578")
    assert report.exit_status == 0 and report.stderr_lines == []


def test_exit_is_zero_only_when_every_line_holds():
    check = check_module()
    registry = check.DESCRIPTIONS
    assert check.render_report(registry, passing(registry)).exit_status == 0
    for metric in registry:
        results = passing(registry)
        results[metric] = release_metrics.WorkerResult(
            metric, False, (release_metrics.Diagnostic("stopped", "x", "y"),)
        )
        report = check.render_report(registry, results)
        assert report.exit_status == 1
        assert [line.rsplit(" ", 1)[1] for line in report.stdout_lines].count("fails") == 1


def test_a_registered_metric_with_no_result_still_prints_a_failed_line():
    check = check_module()
    results = passing(check.DESCRIPTIONS)
    del results[3]
    report = check.render_report(check.DESCRIPTIONS, results)
    assert [line.split(" ", 1)[0] for line in report.stdout_lines] == list("123578")
    assert report.stdout_lines[2].endswith(": fails")
    assert report.exit_status == 1


ESCAPED = "\x1b[31mred\x1b[0m and \x1b]0;title\x07plain"


def test_terminal_control_sequences_are_stripped_from_forwarded_detail():
    check = check_module()
    results = passing(check.DESCRIPTIONS)
    results[8] = release_metrics.WorkerResult(
        8,
        False,
        (release_metrics.Diagnostic("stopped", "x", ESCAPED),),
    )
    report = check.render_report(check.DESCRIPTIONS, results)
    assert report.stderr_lines
    assert all("\x1b" not in line and "\x07" not in line for line in report.stderr_lines)
    assert "red and plain" in " ".join(report.stderr_lines)
    assert all(line.startswith("8 ") for line in report.stderr_lines)


def test_a_multi_line_reason_keeps_the_metric_number_at_the_start_of_every_line():
    check = check_module()
    results = passing(check.DESCRIPTIONS)
    results[3] = release_metrics.WorkerResult(
        3,
        False,
        (release_metrics.Diagnostic("stopped", "x", "first line\nsecond line\nthird line"),),
    )
    report = check.render_report(check.DESCRIPTIONS, results)
    assert len(report.stderr_lines) == 3
    assert all(line.startswith("3 ") for line in report.stderr_lines)


def diagnosed(kind: str, where: str, reason: str = "", **data):
    return release_metrics.Diagnostic(kind, where, reason, data)


def explained(metric: int, *diagnostics) -> list[str]:
    check = check_module()
    results = passing(check.DESCRIPTIONS)
    results[metric] = release_metrics.WorkerResult(metric, False, diagnostics)
    return check.render_report(check.DESCRIPTIONS, results).stderr_lines


NETWORK_RULE = (
    "7 This bar counts only these as this machine: an address from 127.0.0.0 to "
    "127.255.255.255, the same address written in IPv6 form, such as ::ffff:127.0.0.1, the "
    "address ::1, and the host name localhost. No ka-telemetry command is allowed to try to "
    "reach anything else, so the fix goes in ka-telemetry: change each command named above so "
    "that it no longer makes the attempt, then run the release check again."
)
CHECK = "python3 -m ka_telemetry check --dir tests/stream_parity"
HELP = "python3 -m ka_telemetry --help"
RECEIVE = "python3 -m ka_telemetry receive"
LISTED = "ka-telemetry --help lists export"
KIND_INSTALL = release_metrics.KIND_INSTALL_PACKAGES
KIND_UNMEASURED = release_metrics.KIND_NETWORK_UNMEASURED

PUBLISHED_MESSAGES = [
    pytest.param(
        5,
        [diagnosed(KIND_INSTALL, "i", "r", installed=[], requested=["six", "attrs"])],
        [
            "5 What was built from this checkout asks for packages other than ka-telemetry when "
            "it is installed: six, attrs. This bar asks that installing ka-telemetry installs "
            "nothing else, so the fix goes in ka-telemetry: take each of them out of the "
            "dependencies list in pyproject.toml, then run the release check again."
        ],
        id="requested",
    ),
    pytest.param(
        5,
        [diagnosed(KIND_INSTALL, "i", "r", installed=["six", "Attrs"], requested=[])],
        [
            "5 After the README's install command ran, the environment pipx made for "
            "ka-telemetry holds packages other than ka-telemetry, pip, setuptools and wheel: "
            "six, Attrs. This bar asks that installing ka-telemetry installs nothing else, so "
            "the fix goes in ka-telemetry. The packages ka-telemetry asks to have installed with "
            "it are in the dependencies list in pyproject.toml. Take each of these out of that "
            "list, then run the release check again."
        ],
        id="installed",
    ),
    pytest.param(
        5,
        [diagnosed(release_metrics.KIND_INSTALL_STEP_FAILED, "i", "no backend", step="build")],
        [
            "5 This bar was not measured: ka-telemetry could not be built from this checkout to "
            "install it. The build reported: no backend. The build fetches its build backend "
            "with pip over the network. Run the release check again once the build can complete."
        ],
        id="build failed",
    ),
    pytest.param(
        7,
        [
            diagnosed(
                release_metrics.KIND_NETWORK_ATTEMPT, CHECK, "", operation="connect",
                destination="192.0.2.1", port=9,
            ),
            diagnosed(
                release_metrics.KIND_NETWORK_ATTEMPT, CHECK, "", operation="sendto",
                destination="192.0.2.2", port=53,
            ),
            diagnosed(
                release_metrics.KIND_NETWORK_ATTEMPT, CHECK, "", operation="getaddrinfo",
                destination="example.com", port=None,
            ),
            diagnosed(
                release_metrics.KIND_NETWORK_ATTEMPT, CHECK, "", operation="gethostbyaddr",
                destination="192.0.2.3", port=None,
            ),
        ],
        [
            f"7 {CHECK} tried to open a connection to 192.0.2.1, port 9. The release check "
            "refused it.",
            f"7 {CHECK} tried to send a datagram to 192.0.2.2, port 53. The release check "
            "refused it.",
            f"7 {CHECK} tried to look up the host name example.com. The release check refused "
            "the lookup.",
            f"7 {CHECK} tried to look up the name of the address 192.0.2.3. The release check "
            "refused the lookup.",
            NETWORK_RULE,
        ],
        id="attempts, then the rule once",
    ),
    pytest.param(
        7,
        [
            diagnosed(
                release_metrics.KIND_NETWORK_DEFAULT_ADDRESS, "receive", "", address="0.0.0.0",
                port=4318,
            )
        ],
        [
            "7 The receiver, started by the release check with no --host, listened on the "
            "address 0.0.0.0, port 4318. With no --host, this bar allows the receiver to listen "
            "only on an address on this machine, such as 127.0.0.1 or ::1, so the fix goes in "
            "ka-telemetry: make the receiver listen on 127.0.0.1 when it is given no --host, "
            "then run the release check again."
        ],
        id="default address",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, HELP, "boom", cause="help_incomplete")],
        [
            "7 This bar was not measured: python3 -m ka_telemetry --help, which the release "
            "check runs to find the commands it has to run, did not complete. It reported: "
            "boom. Run the release check again once that is resolved."
        ],
        id="help incomplete",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, HELP, "none", cause="no_command_list")],
        [
            "7 This bar was not measured: python3 -m ka_telemetry --help did not print a list "
            "of commands that the release check could read, so it could not tell which "
            "commands to run. To see what it prints, run python3 -m ka_telemetry --help from "
            "the top directory of this checkout."
        ],
        id="no list",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, LISTED, "x", cause="no_run", name="export")],
        [
            "7 ka-telemetry --help lists export, but the release check has no run of export set "
            "up, so it cannot tell whether export tries to reach beyond this machine. Add a run "
            "of export to the release check, then run the release check again."
        ],
        id="no run",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, CHECK, "exit code 2", cause="did_not_complete")],
        [
            f"7 {CHECK} did not complete, so the release check could not watch all of what it "
            "does. It reported: exit code 2. Run the release check again once that is resolved."
        ],
        id="did not complete",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, CHECK, "no such file", cause="not_started")],
        [
            f"7 {CHECK} could not be started, so the release check could not watch what it "
            "does. The system reported: no such file. Run the release check again once that is "
            "resolved."
        ],
        id="not started",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, CHECK, "slow", cause="timed_out", seconds=60)],
        [
            f"7 {CHECK} had not finished 60 seconds after it started, so the release check "
            "stopped it and could not watch all of what it does. Run the release check again."
        ],
        id="timed out",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, CHECK, "no ready record", cause="record_unreadable")],
        [
            f"7 The release check could not read a complete record of what {CHECK} tried to "
            "reach, so it cannot tell whether that command tried to reach beyond this machine. "
            "Run the release check again."
        ],
        id="record unreadable",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, RECEIVE, "bind failed", cause="receiver_not_listening")],
        [
            "7 The release check started the receiver with python3 -m ka_telemetry receive and "
            "did not see it start listening, so it could not watch the receiver at work. The "
            "receiver printed: bind failed. Run the release check again once that is resolved."
        ],
        id="receiver not listening",
    ),
    pytest.param(
        7,
        [
            diagnosed(
                KIND_UNMEASURED, RECEIVE, "HTTP 500", cause="receiver_request_not_accepted",
                path="/v1/logs",
            )
        ],
        [
            "7 The receiver started with python3 -m ka_telemetry receive did not accept the "
            "request the release check sent to /v1/logs from this machine, so the release check "
            "could not watch the receiver at work. The release check got back: HTTP 500. Run "
            "the release check again once that is resolved."
        ],
        id="receiver request",
    ),
    pytest.param(
        7,
        [diagnosed(KIND_UNMEASURED, RECEIVE, "bye", cause="receiver_stopped_early")],
        [
            "7 The receiver started with python3 -m ka_telemetry receive stopped before the "
            "release check had sent it both of its requests, so the release check could not "
            "watch the receiver at work. The receiver printed: bye. Run the release check "
            "again once that is resolved."
        ],
        id="receiver stopped early",
    ),
]


@pytest.mark.parametrize(("metric", "diagnostics", "expected"), PUBLISHED_MESSAGES)
def test_every_new_diagnostic_kind_is_worded_as_published(metric, diagnostics, expected):
    assert explained(metric, *diagnostics) == expected


def test_a_failed_bar_names_every_offending_package_and_attempt_uncapped():
    names = [f"package{number}" for number in range(40)]
    lines = explained(5, diagnosed(KIND_INSTALL, "i", "r", installed=names, requested=[]))
    assert all(name in " ".join(lines) for name in names)
    attempts = [
        diagnosed(
            release_metrics.KIND_NETWORK_ATTEMPT, CHECK, "", operation="connect",
            destination=f"192.0.2.{number}", port=9,
        )
        for number in range(1, 40)
    ]
    lines = explained(7, *attempts)
    assert len([line for line in lines if "tried to open a connection" in line]) == 39
    assert lines.count(NETWORK_RULE) == 1


def test_packages_asked_for_and_packages_installed_print_as_two_messages():
    lines = explained(
        5, diagnosed(KIND_INSTALL, "i", "r", installed=["six"], requested=["attrs"])
    )
    assert len(lines) == 2 and "asks for packages" in lines[0] and "holds packages" in lines[1]


def test_terminal_control_in_bar_5_and_7_detail_is_stripped_and_every_line_is_numbered():
    for metric, diagnostic in (
        (
            5,
            diagnosed(
                release_metrics.KIND_INSTALL_STEP_FAILED, "i", ESCAPED, step="install",
                cause="command_not_started", command="pipx install ka-telemetry",
            ),
        ),
        (7, diagnosed(KIND_UNMEASURED, CHECK, ESCAPED + "\nsecond", cause="did_not_complete")),
    ):
        lines = explained(metric, diagnostic)
        assert all("\x1b" not in line and "\x07" not in line for line in lines)
        assert all(line.startswith(f"{metric} ") for line in lines)


# ── bar 5: the README's install command, worded as published ──

STEP = release_metrics.KIND_INSTALL_STEP_FAILED
WHERE_BAR_5_LOOKS = (
    "This bar runs the one command line in the first shell code block of the Install section of "
    "README.md, a block fenced as sh, shell or bash."
)
PIPX_COMMAND = "pipx install ka-telemetry"
NOT_READ = "This bar was not measured: the README's install command could not be read, because"
EXITED_1 = "5 It exited with status 1."
PRINTED_NOTHING = [
    "5 It printed nothing on standard output.",
    "5 It printed nothing on standard error.",
]
REFUSAL = "error: externally-managed-environment"
REFUSAL_OUTPUT = [
    EXITED_1,
    "5 It printed nothing on standard output.",
    "5 It printed this on standard error:",
    f"5   {REFUSAL}",
    "5   ",
    "5   hint: See PEP 668.",
]
REFUSAL_STDERR = f"{REFUSAL}\n\nhint: See PEP 668.\n"
BAR_5_MESSAGES = [
    pytest.param(
        diagnosed(
            STEP, "README.md", "No such file or directory", step="readme",
            cause="readme_unreadable", reading="file_unreadable", source="README.md",
        ),
        [
            "5 This bar was not measured: the release check could not read README.md in this "
            "checkout, so it could not read the README's install command and ran nothing for "
            "this bar. The system reported: No such file or directory. Run the release check "
            "again once README.md can be read."
        ],
        id="README.md could not be read",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "", step="readme", cause="readme_unreadable",
            reading="no_install_section", source="README.md",
        ),
        [
            f"5 {NOT_READ} README.md has no section headed Install, so the release check ran "
            f"nothing for this bar. {WHERE_BAR_5_LOOKS} Add an Install section to README.md with "
            "the install command in its first shell code block, then run the release check again."
        ],
        id="no Install section",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "", step="readme", cause="readme_unreadable",
            reading="no_shell_block", source="README.md",
        ),
        [
            f"5 {NOT_READ} the Install section of README.md holds no shell code block, so the "
            f"release check ran nothing for this bar. {WHERE_BAR_5_LOOKS} Put the install "
            "command in the first shell code block of that section, then run the release check "
            "again."
        ],
        id="no shell code block",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "", step="readme", cause="readme_unreadable",
            reading="block_unclosed", source="README.md", line=14,
        ),
        [
            f"5 {NOT_READ} the first shell code block in the Install section of README.md, "
            "opened at line 14, is never closed, so the release check ran nothing for this bar. "
            "Close that block with the same fence it opens with, then run the release check again."
        ],
        id="block never closed",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "", step="readme", cause="readme_unreadable",
            reading="not_one_line", source="README.md", line=14,
        ),
        [
            f"5 {NOT_READ} the first shell code block in the Install section of README.md, "
            "opened at line 14, does not hold exactly one line, so the release check could not "
            f"tell which line is the install command and ran nothing for this bar. "
            f"{WHERE_BAR_5_LOOKS} Leave the install command alone in that block, with no blank "
            "line or comment beside it, and put any other command in a later block. Then run the "
            "release check again."
        ],
        id="block not exactly one line",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "No closing quotation", step="readme", cause="readme_unreadable",
            reading="words_unreadable", source="README.md", line=15,
            command="pipx install 'ka-telemetry",
        ),
        [
            f"5 {NOT_READ} the command on line 15 of README.md, pipx install 'ka-telemetry, "
            "could not be split into words: No closing quotation. The release check ran nothing "
            "for this bar. Correct the quoting in that line, then run the release check again."
        ],
        id="words could not be read",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "", step="readme", cause="readme_unreadable",
            reading="target_not_named", source="README.md", line=14, command="brew install pipx",
        ),
        [
            f"5 {NOT_READ} the first shell code block in the Install section of README.md, "
            "opened at line 14, holds brew install pipx, which does not name ka-telemetry. The "
            f"release check did not run it, and ran nothing for this bar. {WHERE_BAR_5_LOOKS} "
            "Put the command that installs ka-telemetry in that first block, and move a command "
            "that prepares for it, such as one that installs pipx, to a later block. Then run the "
            "release check again."
        ],
        id="command does not name ka-telemetry",
    ),
    pytest.param(
        diagnosed(
            STEP, "README.md", "a redirection", step="readme", cause="command_refused",
            source="README.md", line=14, command="pipx install ka-telemetry > out",
        ),
        [
            "5 This bar was not measured: the README's install command, pipx install "
            "ka-telemetry > out, on line 14 of README.md, is not one the release check runs: a "
            "redirection. The release check runs an install command only in a form it can run "
            "without writing anything outside its temporary directory, and it ran nothing for "
            "this bar. Make the first shell code block in the Install section of README.md read "
            "pipx install ka-telemetry, then run the release check again."
        ],
        id="command in a form the check refuses",
    ),
    pytest.param(
        diagnosed(
            STEP, "python", "", step="python", cause="python_unavailable",
            candidates=[
                {"python": "/usr/bin/python3", "outcome": "too_old", "version": "3.10.14"},
                {"python": "/work/venv/bin/python", "outcome": "in_virtual_environment"},
                {"python": "/opt/a/bin/python3", "outcome": "not_marked"},
                {"python": "/opt/b/bin/python3", "outcome": "no_pip"},
                {"python": "/opt/c/bin/python3", "outcome": "not_refused", "reason": "Would run"},
                {"python": "/opt/c/bin/python3", "outcome": "copy_failed", "reason": "disk full"},
            ],
        ),
        [
            "5 This bar was not measured: the release check found no Python 3.11 or later that "
            "refuses pip installs as externally managed, and could not make one in its "
            "temporary directory, so it did not run the README's install command. This bar is "
            "measured on such a Python only. Each Python it tried, and what it found:",
            "5   /usr/bin/python3: it is Python 3.10.14, older than 3.11.",
            "5   /work/venv/bin/python: it runs inside a virtual environment.",
            "5   /opt/a/bin/python3: it is not marked as externally managed.",
            "5   /opt/b/bin/python3: it has no pip the release check could run, so whether it "
            "refuses pip installs could not be confirmed.",
            "5   /opt/c/bin/python3: pip did not refuse a trial install as externally managed. "
            "pip reported: Would run",
            "5   A copy of /opt/c/bin/python3 in the temporary directory could not be made to "
            "refuse pip installs as externally managed. It reported: disk full",
            "5 To measure this bar, run the release check with a Python 3.11 or later that "
            "refuses pip installs as externally managed, such as Homebrew's Python on macOS.",
        ],
        id="no Python that refuses pip installs",
    ),
    pytest.param(
        diagnosed(
            STEP, "python", "", step="python", cause="python_mismatch", command=PIPX_COMMAND,
            found="/opt/other/bin/python3", expected="/opt/a/bin/python3",
        ),
        [
            "5 This bar was not measured: the README's install command, pipx install "
            "ka-telemetry, made the environment for ka-telemetry from /opt/other/bin/python3, "
            "not from /opt/a/bin/python3, the Python the release check had confirmed refuses pip "
            "installs as externally managed. This bar is measured on that Python only. Run the "
            "release check again."
        ],
        id="pipx used another Python",
    ),
    pytest.param(
        diagnosed(
            STEP, "pipx", "", step="pipx", cause="pipx_unavailable", returncode=1,
            stdout="", stderr="no network\n",
        ),
        [
            "5 This bar was not measured: pipx was not found on this machine's PATH, and the "
            "release check could not install its own pipx in its temporary directory, so it did "
            "not run the README's install command. Here is what the release check's install of "
            "pipx did.",
            EXITED_1,
            "5 It printed nothing on standard output.",
            "5 It printed this on standard error:",
            "5   no network",
            "5 Install pipx as the Install section of README.md shows, so that it is on your "
            "PATH, then run the release check again.",
        ],
        id="no pipx and its install did not succeed",
    ),
    pytest.param(
        diagnosed(STEP, "pipx", "No such file", step="pipx", cause="pipx_unavailable"),
        [
            "5 This bar was not measured: pipx was not found on this machine's PATH, and the "
            "release check could not start its own install of pipx in its temporary directory, "
            "so it did not run the README's install command. The system reported: No such file. "
            "Install pipx as the Install section of README.md shows, so that it is on your PATH, "
            "then run the release check again."
        ],
        id="no pipx and its install could not be started",
    ),
    pytest.param(
        diagnosed(
            STEP, "pipx", "it has no pip backend", step="pipx", cause="pipx_not_prepared",
            pipx="/opt/homebrew/bin/pipx",
        ),
        [
            "5 This bar was not measured: the release check could not prepare "
            "/opt/homebrew/bin/pipx to install from what it built from this checkout and "
            "nothing else, so it did not run the README's install command. It reported: it has "
            "no pip backend. Run the release check again once what it reported is resolved."
        ],
        id="pipx could not be prepared",
    ),
    pytest.param(
        diagnosed(
            release_metrics.KIND_INSTALL_TARGET_MISSING, "i", "", cause="target_absent",
            command="python3 -m pip install ka-telemetry", returncode=1, stdout="",
            stderr=REFUSAL_STDERR,
        ),
        [
            "5 The README's install command, python3 -m pip install ka-telemetry, installed "
            "nothing: when it finished, the release check found no ka-telemetry that it had "
            "installed.",
            *REFUSAL_OUTPUT,
            "5 This bar asks that the README's install command installs ka-telemetry. Check that "
            "the first shell code block in the Install section of README.md reads pipx install "
            "ka-telemetry, and that the name set in pyproject.toml is ka-telemetry, then run the "
            "release check again.",
        ],
        id="installed nothing",
    ),
    pytest.param(
        diagnosed(
            STEP, "i", "", step="install", cause="command_failed", command=PIPX_COMMAND,
            returncode=1, stdout="Fetching\nFailed\n", stderr="",
        ),
        [
            "5 The README's install command, pipx install ka-telemetry, did not succeed, so this "
            "bar does not hold. The release check gave it only what it built from this checkout "
            "to install, and nothing could be fetched while it ran.",
            EXITED_1,
            "5 It printed this on standard output:",
            "5   Fetching",
            "5   Failed",
            "5 It printed nothing on standard error.",
            "5 Run the release check again once what the command printed is resolved.",
        ],
        id="did not succeed",
    ),
    pytest.param(
        diagnosed(
            STEP, "i", "No such file: pipx", step="install", cause="command_not_started",
            command=PIPX_COMMAND,
        ),
        [
            "5 This bar was not measured: the README's install command, pipx install "
            "ka-telemetry, could not be started. The system reported: No such file: pipx. Run "
            "the release check again once that is resolved."
        ],
        id="could not be started",
    ),
    pytest.param(
        diagnosed(
            STEP, "i", "not JSON", step="list", cause="list_unreadable", command=PIPX_COMMAND
        ),
        [
            "5 This bar was not measured: the README's install command, pipx install "
            "ka-telemetry, finished, and the release check could not read the list of packages "
            "in the environment pipx made for ka-telemetry. Reading the list reported: not "
            "JSON. Run the release check again."
        ],
        id="list unreadable",
    ),
    pytest.param(
        diagnosed(STEP, "i", "", step="list", cause="list_empty", command=PIPX_COMMAND),
        [
            "5 This bar was not measured: the README's install command, pipx install "
            "ka-telemetry, finished, and reading the list of packages in the environment pipx "
            "made for ka-telemetry gave back nothing. Run the release check again."
        ],
        id="list gave nothing",
    ),
    pytest.param(
        diagnosed(
            STEP, "i", "the version is 0.0.1, not 0.1.0", step="install", cause="not_this_build",
            command=PIPX_COMMAND,
        ),
        [
            "5 The README's install command, pipx install ka-telemetry, installed a ka-telemetry "
            "that is not the one the release check built from this checkout: the version is "
            "0.0.1, not 0.1.0. This bar measures what the README's command installs when what "
            "this checkout builds stands in for the package on the Python Package Index. Run "
            "the release check again."
        ],
        id="a different ka-telemetry",
    ),
]


@pytest.mark.parametrize(("diagnostic", "expected"), BAR_5_MESSAGES)
def test_bar_5_names_each_not_measured_cause_by_its_own_words(diagnostic, expected):
    lines = explained(5, diagnostic)

    assert lines == expected
    assert all(line.startswith("5 ") for line in lines)


def test_every_bar_5_cause_reads_differently_from_every_other():
    openings = [explained(5, case.values[0])[0] for case in BAR_5_MESSAGES]

    assert len(set(openings)) == len(openings)


def test_bar_5_target_absent_says_installed_nothing_quotes_the_command_and_gives_all_output():
    command = "python3 -m pip install ka-telemetry"
    [opening, *middle, closing] = explained(
        5,
        diagnosed(
            release_metrics.KIND_INSTALL_TARGET_MISSING, "i", "", cause="target_absent",
            command=command, returncode=1, stdout="out one\nout two\n", stderr="err\n",
        ),
    )

    assert opening == (
        f"5 The README's install command, {command}, installed nothing: when it finished, the "
        "release check found no ka-telemetry that it had installed."
    )
    assert middle == [
        EXITED_1,
        "5 It printed this on standard output:",
        "5   out one",
        "5   out two",
        "5 It printed this on standard error:",
        "5   err",
    ]
    assert closing.startswith("5 This bar asks that the README's install command installs")


def test_bar_5_names_every_foreign_package():
    names = [f"package{number}" for number in range(25)]
    lines = explained(
        5,
        diagnosed(
            release_metrics.KIND_INSTALL_PACKAGES, "i", "r",
            installed=[*names, "Package3", "package-3"], requested=["Attrs", "attrs", "six"],
        ),
    )

    assert len(lines) == 2
    listed = ", ".join([*names, "Package3", "package-3"])
    assert "other than ka-telemetry when it is installed: Attrs, attrs, six." in lines[0]
    assert f"packages other than ka-telemetry, pip, setuptools and wheel: {listed}." in lines[1]


def test_bar_5_output_has_terminal_control_sequences_removed():
    hostile = ESCAPED + "\nsecond \x1b[2K line"
    diagnostics = [
        diagnosed(
            release_metrics.KIND_INSTALL_TARGET_MISSING, "i", "", cause="target_absent",
            command="pipx install " + ESCAPED + " ka-telemetry", returncode=1, stdout=hostile,
            stderr=hostile,
        ),
        diagnosed(
            STEP, "pipx", hostile + "\nthird\x1b]0;t\x07", step="pipx", cause="pipx_not_prepared",
            pipx="/p/" + ESCAPED,
        ),
        diagnosed(STEP, "pipx", "", step="pipx", cause="pipx_unavailable", returncode=2,
                  stdout=hostile, stderr=hostile),
    ]
    for diagnostic in diagnostics:
        lines = explained(5, diagnostic)
        assert all("\x1b" not in line and "\x07" not in line for line in lines), lines
        assert all(line.startswith("5 ") for line in lines)
        assert any("red and plain" in line for line in lines)
    assert any(
        line.startswith("5   second  line") for line in explained(5, diagnostics[0])
    ), "a hostile output line is indented and numbered like any other"


def test_the_build_failure_prefix_is_unchanged():
    [line] = explained(5, diagnosed(STEP, "i", "no backend", step="build"))

    assert line == (
        "5 This bar was not measured: ka-telemetry could not be built from this checkout to "
        "install it. The build reported: no backend. The build fetches its build backend with "
        "pip over the network. Run the release check again once the build can complete."
    )


def test_other_bars_keep_their_words_and_messages():
    check = check_module()
    assert check.DESCRIPTIONS[1] == (
        "Every figure ka-telemetry reports on the reference set equals its recorded expected figure"
    )
    assert check.DESCRIPTIONS[2] == (
        "Cost totals agree within 1% for every session recorded as agreeing, and every session "
        "outside 1% or with no cost metric is named with a possible cause"
    )
    assert check.DESCRIPTIONS[3] == (
        "No response on the reference set is counted more than once by the runs report or the "
        "steps report"
    )
    assert check.DESCRIPTIONS[7] == NEW_DESCRIPTIONS[7]
    assert check.DESCRIPTIONS[8] == (
        "Every example in the README and the how-to runs as written and prints the output shown"
    )
    report = check.render_report(check.DESCRIPTIONS, passing(check.DESCRIPTIONS))
    assert [line.split(" ", 1)[0] for line in report.stdout_lines] == list("123578")
    assert report.exit_status == 0
    results = passing(check.DESCRIPTIONS)
    results[5] = release_metrics.WorkerResult(
        5, False, (diagnosed(STEP, "i", "no backend", step="build"),)
    )
    failing = check.render_report(check.DESCRIPTIONS, results)
    assert failing.exit_status == 1
    assert [line.rsplit(" ", 1)[1] for line in failing.stdout_lines] == [
        "holds", "holds", "holds", "fails", "holds", "holds"
    ]
    assert explained(
        8, diagnosed(release_metrics.KIND_PYTEST_MISSING, "x", "", python="/usr/bin/python3")
    ) == [
        "8 This bar was not measured: pytest is not installed for /usr/bin/python3, the "
        "Python running the release check. Install it with /usr/bin/python3 -m pip install "
        "pytest and run the release check again."
    ]


INHERITED = {
    "HOME": "/home/caller",
    "TMPDIR": "/tmp/caller",
    "PATH": "/usr/bin",
    "LANG": "C",
    "PYTHONPATH": "/original/checkout",
    "PYTHONPYCACHEPREFIX": "/home/caller/pycache",
    "PYTEST_ADDOPTS": "-x --basetemp=/home/caller/base",
    "PIP_CACHE_DIR": "/home/caller/.cache/pip",
    "PIP_CONFIG_FILE": "/home/caller/.pip/pip.conf",
    "XDG_CACHE_HOME": "/home/caller/.cache",
    "GIT_DIR": "/original/checkout/.git",
}


def inside(path: str, scratch: Path) -> bool:
    return Path(path).is_relative_to(scratch)


def test_children_get_scratch_places_and_no_setting_that_reaches_the_original(tmp_path):
    check = check_module()
    scratch = tmp_path / "scratch"
    env = check.child_environment(scratch, INHERITED)
    for key in (
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "PIP_CACHE_DIR",
        "PYTEST_DEBUG_TEMPROOT",
    ):
        assert inside(env[key], scratch), (key, env[key])
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    for dropped in ("PYTHONPATH", "PYTHONPYCACHEPREFIX", "PYTEST_ADDOPTS", "GIT_DIR"):
        assert dropped not in env
    assert (env["PATH"], env["LANG"]) == ("/usr/bin", "C")
    for value in env.values():
        assert "/home/caller" not in value and "/original/checkout" not in value


def test_inherited_pipx_variables_do_not_reach_any_child(tmp_path):
    check = check_module()
    hostile = {
        **INHERITED,
        "PIPX_HOME": "/home/caller/pipx",
        "PIPX_BIN_DIR": "/home/caller/bin",
        "PIPX_DEFAULT_PYTHON": "/home/caller/python",
        "PIPX_SHARED_LIBS": "/home/caller/shared",
    }
    checkout = original_checkout(tmp_path)
    environments = []

    def recording(argv, **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert not [
        key for key in check.child_environment(tmp_path / "scratch", hostile)
        if key.startswith("PIPX_")
    ]
    check.measure(checkout, tmp_path / "scratch", [1, 5, 8], run=recording, inherited=hostile)

    assert len(environments) == 4, "the copy and three workers"
    for env in environments:
        assert not [key for key in env if key.startswith("PIPX_")]
        assert all("/home/caller" not in value for value in env.values())


def original_checkout(root: Path) -> Path:
    checkout = root / "original"
    (checkout / "tools").mkdir(parents=True)
    (checkout / "tools" / "release_metrics.py").write_text("# worker\n", encoding="utf-8")
    (checkout / "README.md").write_text("edited and not committed\n", encoding="utf-8")
    (checkout / "docs").mkdir()
    (checkout / "docs" / "how-to.md").write_text("how to\n", encoding="utf-8")
    for excluded in (".git", ".worktrees/other", "__pycache__", ".pytest_cache", "build"):
        (checkout / excluded).mkdir(parents=True)
        (checkout / excluded / "kept").write_text("not copied\n", encoding="utf-8")
    return checkout


def test_children_write_only_into_scratch_and_nothing_is_uploaded_or_published(tmp_path):
    check = check_module()
    checkout = original_checkout(tmp_path)
    scratch = tmp_path / "scratch"
    before = digest(checkout)
    calls: list[tuple[list[str], dict]] = []

    def recording(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[:2] == ["git", "clone"]:
            clone = Path(argv[-1])
            (clone / "docs").mkdir(parents=True)
            (clone / "docs" / "deleted.md").write_text("tracked at HEAD\n", encoding="utf-8")
            (clone / "docs" / "how-to.md").write_text("the committed how to\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    check.measure(checkout, scratch, [1, 8], run=recording, inherited=INHERITED)

    clone_call, *worker_calls = calls
    assert clone_call[0][:3] == ["git", "clone", "--quiet"]
    assert "--no-hardlinks" in clone_call[0]
    assert str(checkout) in clone_call[0]
    assert inside(clone_call[0][-1], scratch)
    assert len(worker_calls) == 2
    for argv, kwargs in worker_calls:
        assert argv[2:4] == ["worker", "--metric"] or argv[1].endswith("release_metrics.py")
        assert inside(argv[1], scratch)
        assert inside(argv[argv.index("--checkout") + 1], scratch)
        assert inside(str(kwargs["cwd"]), scratch)
        assert kwargs["env"] == check.child_environment(scratch, INHERITED)
    for argv, _kwargs in calls:
        assert not {"twine", "upload", "publish", "push"} & set(argv)
    clone = Path(clone_call[0][-1])
    assert (clone / "README.md").read_text(encoding="utf-8") == "edited and not committed\n"
    assert (clone / "docs" / "how-to.md").read_text(encoding="utf-8") == "how to\n"
    assert not (clone / "docs" / "deleted.md").exists()
    for excluded in (".git", ".worktrees", "__pycache__", ".pytest_cache", "build"):
        assert not (clone / excluded).exists(), excluded
    assert digest(checkout) == before


# ── the published failure explanations, word for word ──

LOOK_AGAIN = "Run the release check again once that is resolved."
BAR_1_CLOSING = (
    "1 Nothing under tests/parity or tests/stream_parity is edited to make this bar hold, so the "
    "fix goes in ka-telemetry. To see whether anything there has changes that are not committed, "
    "run git status -- tests/parity tests/stream_parity."
)
BAR_2_CLOSING = (
    "2 Nothing under tests/stream_parity is edited to make this bar hold, so the fix goes in "
    "ka-telemetry. To see the check's line for every session, run python3 -m ka_telemetry check "
    "--dir tests/stream_parity from the top directory of this checkout. To see whether anything "
    "under tests/stream_parity has changes that are not committed, run git status -- "
    "tests/stream_parity."
)
BAR_3_CLOSING = (
    "3 Nothing under tests/parity is edited to make this bar hold, so the fix goes in "
    "ka-telemetry. To see whether anything there has changes that are not committed, run git "
    "status -- tests/parity."
)
SESSION = "6547733b-af89-40a8-a524-2df28af94cae"
EXAMPLES_RUN = "python3 -m pytest tests/e2e/test_documented_examples.py"

EXPLANATIONS = {
    "stopped": (
        3,
        ("stopped", "the measurement of metric 3", "it blew up"),
        [
            "3 This bar was not measured: its measurement stopped without giving a result. "
            "It reported: it blew up. Run the release check again."
        ],
    ),
    "stopped without a reason": (
        3,
        ("stopped", "the measurement of metric 3", ""),
        [
            "3 This bar was not measured: its measurement stopped without giving a result or a "
            "reason. Run the release check again."
        ],
    ),
    "unreadable input": (
        2,
        ("unreadable_input", "tests/stream_parity/expected/agents.json", "No such file"),
        [
            "2 This bar was not measured: tests/stream_parity/expected/agents.json could not be "
            "read. The system reported: No such file. Run the release check again once "
            "tests/stream_parity/expected/agents.json can be read."
        ],
    ),
    "command failed": (
        2,
        ("command_failed", "python3 -m ka_telemetry check --dir tests/stream_parity --json", "no"),
        [
            "2 This bar was not measured: python3 -m ka_telemetry check --dir tests/stream_parity"
            f" --json did not complete. It reported: no. {LOOK_AGAIN}"
        ],
    ),
    "bar 1 difference": (
        1,
        ("difference", "tests/parity/expected/runs.json", "output 1557 against 1558"),
        [
            "1 tests/parity/expected/runs.json does not match what ka-telemetry reports on the "
            "same data. The difference: output 1557 against 1558",
            BAR_1_CLOSING,
        ],
    ),
    "bar 2 apart": (
        2,
        ("session_apart", SESSION, "x", {"session_id": SESSION}),
        [
            f"2 Session {SESSION}: the reference set records its cost totals as agreeing within "
            "1%, and ka-telemetry check now reports them more than 1% apart.",
            BAR_2_CLOSING,
        ],
    ),
    "bar 2 no metric": (
        2,
        ("session_no_metric", SESSION, "x", {"session_id": SESSION}),
        [
            f"2 Session {SESSION}: the reference set records its cost totals as agreeing within "
            "1%, and ka-telemetry check now reports no cost metric for it.",
            BAR_2_CLOSING,
        ],
    ),
    "bar 2 missing": (
        2,
        ("session_missing", SESSION, "x", {"session_id": SESSION}),
        [
            f"2 Session {SESSION}: the reference set records its cost totals as agreeing within "
            "1%, and ka-telemetry check does not report the session at all.",
            BAR_2_CLOSING,
        ],
    ),
    "bar 2 apart without a cause": (
        2,
        ("apart_without_cause", SESSION, "x", {"session_id": SESSION}),
        [
            f"2 Session {SESSION}: ka-telemetry check reports its cost totals more than 1% apart"
            " and names no possible cause.",
            BAR_2_CLOSING,
        ],
    ),
    "bar 2 no metric without a cause": (
        2,
        ("no_metric_without_cause", SESSION, "x", {"session_id": SESSION}),
        [
            f"2 Session {SESSION}: ka-telemetry check reports no cost metric for it and names no"
            " possible cause.",
            BAR_2_CLOSING,
        ],
    ),
    "bar 3 runs": (
        3,
        (
            "counted_twice",
            "msg_011Cfh6d1FVJV4chymjSkBss",
            "x",
            {
                "report": "runs",
                "response_id": "msg_011Cfh6d1FVJV4chymjSkBss",
                "transcripts": [
                    "tests/parity/projects/a/one.jsonl",
                    "tests/parity/projects/a/two.jsonl",
                ],
            },
        ),
        [
            "3 The runs report counts response msg_011Cfh6d1FVJV4chymjSkBss more than once. It "
            "counted it from these transcripts: tests/parity/projects/a/one.jsonl, "
            "tests/parity/projects/a/two.jsonl.",
            BAR_3_CLOSING,
        ],
    ),
    "bar 3 steps": (
        3,
        (
            "counted_twice",
            "msg_x",
            "x",
            {"report": "steps", "response_id": "msg_x", "transcripts": ["tests/parity/a.jsonl"]},
        ),
        [
            "3 The steps report counts response msg_x more than once. It counted it from these "
            "transcripts: tests/parity/a.jsonl.",
            BAR_3_CLOSING,
        ],
    ),
    "bar 3 count unfinished": (
        3,
        ("count_unfinished", "counting", "it stopped", {"report": "steps"}),
        [
            "3 This bar was not measured: counting the responses the steps report counts did not "
            f"finish. It reported: it stopped. {LOOK_AGAIN}"
        ],
    ),
    "bar 8 failed on a document": (
        8,
        ("example_failed", "docs/how-to.md", "x", {"document": "docs/how-to.md", "detail": "off"}),
        [
            f"8 The test of the documented examples, {EXAMPLES_RUN.split()[-1]}, failed on an "
            "example in docs/how-to.md. Its report: off. To see the whole failure, run "
            f"{EXAMPLES_RUN} from the top directory of this checkout. Then change the example or "
            "ka-telemetry so that the example prints what is shown beside it."
        ],
    ),
    "bar 8 failed with no document": (
        8,
        (
            "example_failed_without_document",
            "tests/e2e/test_documented_examples.py",
            "x",
            {"detail": "off"},
        ),
        [
            f"8 The test of the documented examples, {EXAMPLES_RUN.split()[-1]}, failed. Its "
            f"report: off. To see the whole failure, run {EXAMPLES_RUN} from the top directory of "
            "this checkout."
        ],
    ),
    "bar 8 build": (
        8,
        ("build_failed", "building ka-telemetry for the examples test", "no backend"),
        [
            "8 This bar was not measured: ka-telemetry could not be built for the test of the "
            "documented examples. The build reported: no backend. The build fetches its build "
            "backend with pip over the network. Run the release check again once the build can "
            "complete."
        ],
    ),
    "bar 8 pytest": (
        8,
        ("pytest_missing", EXAMPLES_RUN, "x", {"python": "/usr/bin/python3"}),
        [
            "8 This bar was not measured: pytest is not installed for /usr/bin/python3, the "
            "Python running the release check. Install it with /usr/bin/python3 -m pip install "
            "pytest and run the release check again."
        ],
    ),
    "scratch not prepared": (
        1,
        ("not_prepared", "the temporary copy", "No space left on device"),
        [
            "1 This bar was not measured: the release check could not prepare the temporary copy "
            "of this checkout that it measures in. The system reported: No space left on device. "
            "Run the release check again once that is resolved."
        ],
    ),
    "git missing": (
        8,
        ("git_missing", "git", "x"),
        [
            "8 This bar was not measured: the release check needs Git to make its temporary copy "
            "of this checkout, and Git was not found. Install Git and run the release check "
            "again."
        ],
    ),
}


@pytest.mark.parametrize("case", sorted(EXPLANATIONS))
def test_every_failure_explanation_reads_as_published(case):
    check = check_module()
    metric, member, expected = EXPLANATIONS[case]
    kind, operation, reason, *data = member
    diagnostic = release_metrics.Diagnostic(kind, operation, reason, data[0] if data else {})
    results = passing(check.DESCRIPTIONS)
    results[metric] = release_metrics.WorkerResult(metric, False, (diagnostic,))
    report = check.render_report(check.DESCRIPTIONS, results)
    assert report.stderr_lines == expected


def test_a_closing_explanation_is_printed_once_after_the_last_difference():
    check = check_module()
    results = passing(check.DESCRIPTIONS)
    results[1] = release_metrics.WorkerResult(
        1,
        False,
        tuple(
            release_metrics.Diagnostic("difference", f"tests/parity/expected/{name}", "differs")
            for name in ("a.txt", "b.txt")
        ),
    )
    lines = check.render_report(check.DESCRIPTIONS, results).stderr_lines
    assert [line.split(" ")[1] for line in lines[:2]] == [
        "tests/parity/expected/a.txt",
        "tests/parity/expected/b.txt",
    ]
    assert lines[2:] == [BAR_1_CLOSING]


# ── metric 5: what the README's install command puts in the environment pipx makes ──

TARGET = "ka-telemetry"
IDENTITY = "/opt/pythons/3.12"
BASE_EXECUTABLE = f"{IDENTITY}/bin/python3"


def make_wheel(
    directory: Path, *, requires: tuple[str, ...] = (), version: str = "0.1.0", extra_members=None
) -> Path:
    """A wheel that is only what the install measure reads: a `METADATA` naming the target and the
    requirements given, and the payload `ka_telemetry/__init__.py`. Installing it needs no build
    backend."""
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"ka_telemetry-{version}-py3-none-any.whl"
    metadata = ["Metadata-Version: 2.1", f"Name: {TARGET}", f"Version: {version}"]
    metadata += [f"Requires-Dist: {requirement}" for requirement in requires]
    members = {
        "ka_telemetry/__init__.py": "VALUE = 1\n",
        f"ka_telemetry-{version}.dist-info/METADATA": "\n".join(metadata) + "\n",
        f"ka_telemetry-{version}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"ka_telemetry-{version}.dist-info/RECORD": "",
    }
    members.update(extra_members or {})
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return wheel


def fixture_project(base: Path, *, dependencies: str = "[]") -> Path:
    """A small project named like the target that builds with the product's own backend, whose
    README gives the install command the product's README gives."""
    project = base / "project"
    (project / "ka_telemetry").mkdir(parents=True)
    (project / "ka_telemetry" / "__init__.py").write_text(
        "VALUE = 1\n\n\ndef main():\n    return 0\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [build-system]
            requires = ["setuptools>=77,<90"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "ka-telemetry"
            version = "0.1.0"
            dependencies = {dependencies}

            [project.scripts]
            ka-telemetry = "ka_telemetry:main"

            [tool.setuptools]
            packages = ["ka_telemetry"]
            """
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text(readme_with("pipx install ka-telemetry"), encoding="utf-8")
    return project


def readme_with(command: str) -> str:
    return install_section(f"```sh\n{command}\n```\n")


def kinds(found) -> list[str]:
    return [diagnostic.kind for diagnostic in found]


class FakeBuild:
    """The build step, answering with the wheel a test made, or failing with the report given."""

    def __init__(self, wheel: Path, failing: str | None = None) -> None:
        self.wheel, self.failing = wheel, failing
        self.sources: list[Path] = []

    def build_wheel(self, source: Path, wheel_dir: Path) -> Path:
        self.sources.append(source)
        if self.failing is not None:
            raise release_metrics.InstallStepFailed("build", self.failing)
        return self.wheel


class StubPreparation:
    """The preparation handed to the measure: a Python of a known identity and a pipx whose
    settings keep everything under the scratch directory it is given, or the failure it was told
    to raise."""

    def __init__(self, *, python_failure=None, pipx_failure=None, provisioned=False) -> None:
        self.python_failure, self.pipx_failure = python_failure, pipx_failure
        self.provisioned = provisioned
        self.stale_environment = False
        self.calls: list[str] = []
        self.home: Path | None = None

    def base_python(self, scratch: Path):
        self.calls.append("python")
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "python-copy").write_text("a private copy", encoding="utf-8")
        if self.python_failure is not None:
            raise release_metrics.PreparationFailed(self.python_failure)
        return release_metrics.BasePython(Path(BASE_EXECUTABLE), (3, 12, 3), IDENTITY)

    def pipx(self, scratch: Path, base):
        self.calls.append("pipx")
        root = scratch / "pipx"
        self.home = root / "home"
        for place in ("home", "bin", "shared"):
            (root / place).mkdir(parents=True, exist_ok=True)
        if self.pipx_failure is not None:
            raise release_metrics.PreparationFailed(self.pipx_failure)
        if self.stale_environment:
            stale = self.home / "venvs" / TARGET / "bin"
            stale.mkdir(parents=True)
            (stale / "python").touch()
        variables = {
            "PIPX_HOME": str(self.home),
            "PIPX_BIN_DIR": str(root / "bin"),
            "PIPX_SHARED_LIBS": str(root / "shared"),
            "PIPX_DEFAULT_PYTHON": str(base.executable),
            "PIPX_DEFAULT_BACKEND": "pip",
        }
        return release_metrics.PreparedPipx(root / "bin" / "pipx", self.provisioned, variables)


def step_failure(step: str, cause: str, reason: str = "it did not work", **data):
    return release_metrics.Diagnostic(
        release_metrics.KIND_INSTALL_STEP_FAILED,
        "preparing",
        reason,
        {"step": step, "cause": cause, **data},
    )


class InstallWorld:
    """The two processes the measure runs after preparation: the README's command, and the
    listing the target environment's own interpreter prints. Both answer from what a test sets
    up; any other command fails the test."""

    def __init__(self, base: Path, wheel: Path) -> None:
        self.site = base / "site-packages"
        self.wheel = wheel
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.directories: list[str] = []
        self.path_links: dict[str, str] = {}
        self.report = (0, "installed package ka-telemetry 0.1.0\n", "")
        self.makes_environment = True
        self.launch_error: OSError | None = None
        self.listing_error: OSError | None = None
        self.listing: tuple[int, str, str] | None = None
        self.version: str | None = None
        self.altered_payload = False
        self.alongside = ("pip", "setuptools")
        self.distributions: list[dict] | None = None
        self.base_executable = BASE_EXECUTABLE
        self.base_prefix = IDENTITY

    def launched(self) -> list[list[str]]:
        """The commands that were the README's, not the listing."""
        return [words for words in self.commands if words[1:2] != ["-I"]]

    def __call__(self, command, **settings):
        words = [str(part) for part in command]
        self.commands.append(words)
        environment = dict(settings.get("env") or {})
        self.environments.append(environment)
        self.directories.append(str(settings.get("cwd")))

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(words, code, out, err)

        if words[1:2] == ["-I"]:
            if self.listing_error is not None:
                raise self.listing_error
            if self.listing is not None:
                return done(*self.listing)
            return done(0, json.dumps(self._listing()))
        assert words[0] in {"pipx", "python3"}, f"a command nothing planned for: {words}"
        first = Path(environment["PATH"].split(os.pathsep)[0])
        self.path_links = {
            link.name: os.readlink(link) for link in first.iterdir() if link.is_symlink()
        }
        if self.launch_error is not None:
            raise self.launch_error
        if self.makes_environment:
            made = Path(environment["PIPX_HOME"]) / "venvs" / TARGET / "bin"
            made.mkdir(parents=True, exist_ok=True)
            (made / "python").touch()
            self._place_payload()
        return done(*self.report)

    def _place_payload(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            for name in archive.namelist():
                if ".dist-info/" in name:
                    continue
                target = self.site / name
                target.parent.mkdir(parents=True, exist_ok=True)
                body = archive.read(name)
                target.write_bytes(body + b"# edited\n" if self.altered_payload else body)

    def _listing(self) -> dict:
        if self.distributions is not None:
            distributions = self.distributions
        else:
            distributions = [
                {"name": TARGET, "version": self.version or "0.1.0", "location": str(self.site)}
            ] + [
                {"name": name, "version": "1.0", "location": str(self.site)}
                for name in self.alongside
            ]
        return {
            "base_prefix": self.base_prefix,
            "base_executable": self.base_executable,
            "distributions": distributions,
        }


class Scene:
    """One run of the measure over a scratch checkout whose README holds `command`, with the build,
    the preparation and the processes all handed in."""

    def __init__(
        self, tmp_path: Path, *, command: str = "pipx install ka-telemetry", requires=(),
        build_failing: str | None = None, **preparation,
    ) -> None:
        self.checkout = readme_in(tmp_path / "checkout", readme_with(command))
        self.wheel = make_wheel(tmp_path / "wheel", requires=requires)
        self.build = FakeBuild(self.wheel, build_failing)
        self.preparation = StubPreparation(**preparation)
        self.world = InstallWorld(tmp_path, self.wheel)
        self.environ: dict[str, str] = {"PATH": "/usr/bin:/bin"}

    def measure(self):
        return release_metrics.measure_install(
            self.checkout, steps=self.build, preparation=self.preparation, run=self.world,
            environ=self.environ,
        )

    def only(self):
        [diagnostic] = self.measure()
        return diagnostic


def test_the_measured_install_runs_the_readmes_words_with_nothing_added(tmp_path):
    scene = Scene(tmp_path)
    assert scene.measure() == []
    assert scene.world.launched() == [list(INSTALL_WORDS)], "no flag and no wheel path was added"

    other = Scene(tmp_path / "plain", command="python3 -m pip install ka-telemetry")
    other.measure()
    assert other.world.launched() == [list(PLAIN_PIP_WORDS)], "the README's words decide"


def test_a_real_install_on_a_pep_668_python_gets_the_built_wheel_and_fetches_nothing(tmp_path):
    project = fixture_project(tmp_path)
    found = release_metrics.the_built_wheel_installs_only_the_target(project)

    for diagnostic in found:
        if diagnostic.data.get("cause") in (
            release_metrics.CAUSE_PYTHON_UNAVAILABLE, release_metrics.CAUSE_PIPX_UNAVAILABLE,
        ) or diagnostic.data.get("step") == release_metrics.BUILD_STEP:
            pytest.skip(f"this machine cannot prepare the real measure: {diagnostic.reason}")
    assert found == [], "the environment held ka-telemetry from the built wheel and nothing else"

    (project / "README.md").write_text(readme_with(" ".join(PLAIN_PIP_WORDS)), encoding="utf-8")
    [refused] = release_metrics.the_built_wheel_installs_only_the_target(project)
    assert refused.data["cause"] == release_metrics.CAUSE_TARGET_ABSENT
    assert refused.data["command"] == " ".join(PLAIN_PIP_WORDS)
    assert "externally-managed-environment" in refused.data["stderr"]


def test_the_target_is_absent_before_the_command_and_an_old_environment_cannot_supply_success(
    tmp_path,
):
    scene = Scene(tmp_path)
    scene.preparation.stale_environment = True
    scene.world.makes_environment = False

    diagnostic = scene.only()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_TARGET_MISSING
    assert diagnostic.data["cause"] == release_metrics.CAUSE_TARGET_ABSENT
    assert scene.world.commands == [list(INSTALL_WORDS)], "nothing was listed from the old one"


@pytest.mark.parametrize("change", ["name", "version", "payload"])
def test_a_stale_same_name_environment_is_not_the_target(tmp_path, change):
    scene = Scene(tmp_path)
    if change == "name":
        metadata = "Metadata-Version: 2.1\nName: other-tool\nVersion: 0.1.0\n"
        make_wheel(
            scene.wheel.parent,
            extra_members={"ka_telemetry-0.1.0.dist-info/METADATA": metadata},
        )
    elif change == "version":
        scene.world.version = "0.0.1"
    else:
        scene.world.altered_payload = True

    diagnostic = scene.only()

    assert diagnostic.data["cause"] == release_metrics.CAUSE_NOT_THIS_BUILD
    assert diagnostic.data["command"] == "pipx install ka-telemetry"
    assert diagnostic.reason
    if change == "name":
        assert "other-tool" in diagnostic.reason, "the reason names the name that differs"


def test_the_target_environments_base_python_is_the_validated_one(tmp_path):
    scene = Scene(tmp_path)
    scene.world.base_executable = "/usr/bin/python3.11"
    scene.world.base_prefix = "/usr"

    diagnostic = scene.only()

    assert diagnostic.data["cause"] == release_metrics.CAUSE_PYTHON_MISMATCH
    assert diagnostic.data["found"] == "/usr/bin/python3.11"
    assert diagnostic.data["expected"] == BASE_EXECUTABLE
    assert diagnostic.data["command"] == "pipx install ka-telemetry"


@pytest.mark.parametrize(
    "requirement",
    [
        "six",
        "six @ http://127.0.0.1:{port}/six-1.0-py3-none-any.whl",
        "colorama ; sys_platform == 'no-such-platform'",
    ],
    ids=["named requirement", "direct reference", "skipped on this platform"],
)
def test_a_declared_runtime_requirement_fails_and_is_named_and_nothing_is_fetched(
    tmp_path, requirement
):
    with CountingIndex() as index:
        scene = Scene(tmp_path, requires=(requirement.format(port=index.port),))

        diagnostic = scene.only()

    name = requirement.split(" ")[0]
    assert diagnostic.kind == release_metrics.KIND_INSTALL_PACKAGES
    assert diagnostic.data == {"installed": [], "requested": [name]}
    assert scene.world.commands == [], "the README's command was not run"
    assert scene.preparation.calls == [], "nothing was prepared for a build that cannot hold"
    assert index.requests == [], "the measure went looking for a package"


def test_inherited_pip_pipx_uv_config_and_cache_settings_are_ignored_and_no_request_is_made(
    tmp_path,
):
    with CountingIndex() as index:
        address = f"http://127.0.0.1:{index.port}"
        scene = Scene(tmp_path)
        scene.environ = {
            "PATH": "/usr/bin:/bin",
            "PIP_INDEX_URL": f"{address}/simple",
            "PIP_EXTRA_INDEX_URL": f"{address}/extra",
            "PIP_FIND_LINKS": f"{address}/links",
            "PIP_NO_INDEX": "0",
            "PIP_CONFIG_FILE": "/hostile/pip.conf",
            "PIPX_HOME": "/hostile/pipx",
            "PIPX_BIN_DIR": "/hostile/bin",
            "UV_INDEX_URL": f"{address}/uv",
            "UV_CACHE_DIR": "/hostile/uv",
            "UV_PYTHON": "/hostile/python",
            "HTTPS_PROXY": address,
            "http_proxy": address,
            "PYTHONPATH": "/hostile/lib",
            "VIRTUAL_ENV": "/hostile/venv",
        }
        assert scene.measure() == []

    environment = scene.world.environments[0]
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_FIND_LINKS"] == str(scene.wheel.parent)
    assert environment["PIP_NO_CACHE_DIR"] == "1"
    assert environment["PIP_PROXY"] == release_metrics.UNREACHABLE_PROXY
    assert environment["PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE"] == "1"
    assert environment["PIPX_HOME"] == str(scene.preparation.home)
    assert environment["PIPX_DEFAULT_PYTHON"] == BASE_EXECUTABLE
    assert environment["PIPX_DEFAULT_BACKEND"] == "pip"
    for name in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_PYTHON", "PYTHONPATH"):
        assert name not in environment, name
    assert not any(address in value or "/hostile" in value for value in environment.values())
    assert not any(name.lower() in ("https_proxy", "http_proxy") for name in environment)
    assert scene.world.path_links == {
        "python3": BASE_EXECUTABLE, "pipx": str(scene.preparation.home.parent / "bin" / "pipx"),
    }
    assert index.requests == []


def test_every_child_runs_in_the_scratch_directory_and_the_cache_places_are_inside_it(tmp_path):
    scene = Scene(tmp_path)
    scene.measure()

    [directory, _] = scene.world.directories
    environment = scene.world.environments[0]
    for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR", "UV_CACHE_DIR"):
        assert environment[name].startswith(directory), name
    assert environment["PIPX_FETCH_PYTHON"] == "never"


def test_extras_and_bootstrap_treatment_of_requested_packages_is_retained(tmp_path):
    scene = Scene(
        tmp_path,
        requires=("pip", "Wheel", "ka_telemetry[extra]", "extra-only ; extra == 'dev'"),
    )
    scene.world.alongside = ("pip", "setuptools", "wheel")

    assert scene.measure() == []


@pytest.mark.parametrize(
    "spelling", ["ka-telemetry", "KA_Telemetry", "ka.telemetry", "Ka--Telemetry", "KA-_.Telemetry"]
)
def test_target_only_ka_telemetry_in_any_case_holds(tmp_path, spelling):
    scene = Scene(tmp_path)
    scene.world.distributions = [
        {"name": spelling, "version": "0.1.0", "location": str(scene.world.site)},
        {"name": "PIP", "version": "1", "location": str(scene.world.site)},
        {"name": "Setuptools", "version": "1", "location": str(scene.world.site)},
    ]

    assert scene.measure() == []
    assert release_metrics.normalize_name(spelling) == TARGET


def test_multiple_foreign_installed_and_requested_packages_are_all_named_once_normalized(tmp_path):
    scene = Scene(tmp_path)
    site = str(scene.world.site)
    scene.world.distributions = [
        {"name": name, "version": "0.1.0" if name == TARGET else "1", "location": site}
        for name in (TARGET, "six", "Requests", "idna", "pip", "SIX", "Six_Pack", "six-pack")
    ]

    [diagnostic] = scene.measure()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_PACKAGES
    named = ["six", "Requests", "idna", "Six_Pack"]
    assert diagnostic.data == {"installed": named, "requested": []}
    asked = Scene(tmp_path / "asked", requires=("six>=1", "Requests[socks]", "SIX", "idna")).only()
    assert asked.data == {"installed": [], "requested": ["six", "Requests", "idna"]}


def test_a_non_zero_command_fails_even_when_a_target_is_left(tmp_path):
    scene = Scene(tmp_path)
    scene.world.report = (1, "some output\n", "ERROR: it stopped\nsecond line\n")

    diagnostic = scene.only()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
    assert diagnostic.data["cause"] == release_metrics.CAUSE_COMMAND_FAILED
    assert diagnostic.data["step"] == release_metrics.INSTALL_STEP
    assert diagnostic.data["command"] == "pipx install ka-telemetry"
    assert diagnostic.data["returncode"] == 1
    assert diagnostic.data["stdout"] == "some output\n"
    assert diagnostic.data["stderr"] == "ERROR: it stopped\nsecond line\n"
    assert len(scene.world.commands) == 1, "nothing is listed after a failed command"


@pytest.mark.parametrize("how", ["no environment", "empty inventory"])
def test_a_zero_exit_that_installs_nothing_fails_as_the_command_installed_nothing(tmp_path, how):
    scene = Scene(tmp_path)
    if how == "no environment":
        scene.world.makes_environment = False
    else:
        scene.world.distributions = []

    diagnostic = scene.only()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_TARGET_MISSING
    assert diagnostic.data["cause"] == release_metrics.CAUSE_TARGET_ABSENT
    assert diagnostic.data["command"] == "pipx install ka-telemetry"
    assert diagnostic.data["returncode"] == 0
    assert diagnostic.data["stdout"] == "installed package ka-telemetry 0.1.0\n"
    assert diagnostic.input_or_operation and diagnostic.reason


def test_the_plain_pip_regression_command_installs_nothing_and_the_refusal_is_kept(tmp_path):
    scene = Scene(tmp_path, command="python3 -m pip install ka-telemetry")
    scene.world.makes_environment = False
    scene.world.report = (1, "", "error: externally-managed-environment\n")

    diagnostic = scene.only()

    assert diagnostic.data["cause"] == release_metrics.CAUSE_TARGET_ABSENT
    assert diagnostic.data["command"] == "python3 -m pip install ka-telemetry"
    assert diagnostic.data["returncode"] == 1
    assert "externally-managed-environment" in diagnostic.data["stderr"]


@pytest.mark.parametrize(
    "how, cause",
    [
        ("not json", release_metrics.CAUSE_LIST_UNREADABLE),
        ("not a document", release_metrics.CAUSE_LIST_UNREADABLE),
        ("a failed listing", release_metrics.CAUSE_LIST_UNREADABLE),
        ("cannot be run", release_metrics.CAUSE_LIST_UNREADABLE),
        ("prints nothing", release_metrics.CAUSE_LIST_EMPTY),
    ],
)
def test_an_unreadable_or_malformed_inventory_is_a_distinct_not_measured_failure(
    tmp_path, how, cause
):
    scene = Scene(tmp_path)
    if how == "not json":
        scene.world.listing = (0, "[oops", "")
    elif how == "not a document":
        scene.world.listing = (0, json.dumps({"distributions": [1]}), "")
    elif how == "a failed listing":
        scene.world.listing = (1, "", "Traceback: boom")
    elif how == "cannot be run":
        scene.world.listing_error = FileNotFoundError(2, "No such file or directory")
    else:
        scene.world.listing = (0, "  \n", "")

    diagnostic = scene.only()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
    assert diagnostic.data["step"] == release_metrics.LIST_STEP
    assert diagnostic.data["cause"] == cause
    assert diagnostic.data["command"] == "pipx install ka-telemetry"
    assert diagnostic.reason or cause == release_metrics.CAUSE_LIST_EMPTY


def test_a_command_that_cannot_be_started_is_not_measured_and_keeps_the_systems_report(tmp_path):
    scene = Scene(tmp_path)
    scene.world.launch_error = FileNotFoundError(2, "No such file or directory")

    diagnostic = scene.only()

    assert diagnostic.data["cause"] == release_metrics.CAUSE_COMMAND_NOT_STARTED
    assert diagnostic.reason == "No such file or directory"
    assert diagnostic.data["command"] == "pipx install ka-telemetry"


@pytest.mark.parametrize("damage", ["missing", "not a zip", "no metadata"])
def test_a_missing_or_corrupt_wheel_keeps_its_stage_and_the_original_report(tmp_path, damage):
    scene = Scene(tmp_path)
    wheel = scene.wheel
    if damage == "missing":
        wheel.unlink()
    elif damage == "not a zip":
        wheel.write_text("this is not a wheel", encoding="utf-8")
    else:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("ka_telemetry/__init__.py", "")

    diagnostic = scene.only()

    assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
    assert diagnostic.data["step"] == release_metrics.BUILD_STEP
    assert diagnostic.reason
    assert scene.world.commands == []


def test_a_failed_build_keeps_its_stage_and_the_original_report(tmp_path):
    scene = Scene(tmp_path, build_failing="no matching distribution for setuptools\nsecond line")

    diagnostic = scene.only()

    assert diagnostic.data == {"step": "build"}
    assert diagnostic.reason == "no matching distribution for setuptools\nsecond line"
    assert scene.preparation.calls == [] and scene.world.commands == []


def test_the_wrong_number_of_wheels_keeps_its_stage_and_the_original_report(tmp_path):
    project = fixture_project(tmp_path)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    make_wheel(wheels)
    make_wheel(wheels, version="0.2.0")

    class TwoWheels(release_metrics.InstallSteps):
        def _complete(self, step, command, environ):
            return ""

    with pytest.raises(release_metrics.InstallStepFailed) as stopped:
        TwoWheels().build_wheel(project, wheels)

    assert stopped.value.step == "build"
    assert "2 wheels, not one" in stopped.value.reason


@pytest.mark.parametrize(
    "command, words",
    [
        ("brew install pipx", "target_not_named"),
        ("pipx install ka-telemetry > /etc/x", "command_refused"),
    ],
)
def test_other_bars_still_run_when_bar_5_fails(tmp_path, command, words):
    scene = Scene(tmp_path, command=command)

    [diagnostic] = scene.measure()

    assert diagnostic.data["step"] == release_metrics.README_STEP
    assert scene.world.commands == [] and scene.preparation.calls == []
    assert words in (diagnostic.data.get("reading"), diagnostic.data.get("cause"))


@pytest.mark.parametrize("provisioned", [False, True], ids=["host pipx", "brought pipx"])
@pytest.mark.parametrize("outcome", ["holds", "python", "pipx", "command", "listing"])
def test_scratch_is_removed_on_success_preparation_failure_command_failure_and_listing_failure(
    tmp_path, monkeypatch, outcome, provisioned
):
    scratch, home = tmp_path / "scratch", tmp_path / "host-home"
    for place in (scratch, home):
        place.mkdir()
    (home / ".zshrc").write_text("export PATH=$PATH\n", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)
    scene = Scene(tmp_path / "scene", provisioned=provisioned)
    scene.environ = {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "PIPX_HOME": str(home / "hostile-pipx"),
        "PIPX_BIN_DIR": str(home / "hostile-bin"), "XDG_CACHE_HOME": str(home / "cache"),
    }
    if outcome == "python":
        scene.preparation.python_failure = step_failure("python", "python_unavailable")
    elif outcome == "pipx":
        scene.preparation.pipx_failure = step_failure("pipx", "pipx_unavailable")
    elif outcome == "command":
        scene.world.report = (1, "", "failed")
    elif outcome == "listing":
        scene.world.listing = (1, "", "boom")
    before = digest(home)

    found = scene.measure()

    assert bool(found) == (outcome != "holds")
    assert list(scratch.iterdir()) == [], "everything the measure made went with its scratch"
    assert digest(home) == before and not (home / "hostile-pipx").exists()


@pytest.mark.parametrize("which", ["python", "pipx"])
def test_a_preparation_failure_is_returned_with_its_own_diagnostic_and_runs_nothing(
    tmp_path, which
):
    failure = step_failure(which, f"{which}_unavailable", "none to be had")
    scene = Scene(tmp_path, **{f"{which}_failure": failure})

    [diagnostic] = scene.measure()

    assert diagnostic == failure
    assert scene.world.commands == []


def test_the_command_is_admitted_before_anything_is_prepared(tmp_path):
    scene = Scene(tmp_path, command="pipx install --user ka-telemetry")

    [diagnostic] = scene.measure()

    assert diagnostic.data["cause"] == release_metrics.CAUSE_COMMAND_REFUSED
    assert scene.preparation.calls == [] and scene.world.commands == []
    assert scene.build.sources == [], "nothing was built for a command that is never run"


def test_the_working_files_of_the_checkout_are_in_the_built_wheel(tmp_path):
    project = fixture_project(tmp_path)
    (project / "ka_telemetry" / "overlay_only.py").write_text("OVERLAY = True\n", encoding="utf-8")
    steps = release_metrics.InstallSteps()

    wheel = steps.build_wheel(project, tmp_path / "wheels")

    with zipfile.ZipFile(wheel) as archive:
        assert "ka_telemetry/overlay_only.py" in archive.namelist()
    assert not (project / "build").exists() and not list(project.glob("*.egg-info"))


class CountingIndex:
    """A local server that answers every request with a 404 and counts them: anything a pip
    install sends here went looking for a package."""

    def __init__(self) -> None:
        counts: list[str] = []
        self.requests = counts

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                counts.append(self.path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *arguments):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> CountingIndex:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


# ── bar 5: reading and admitting the README's install command ──

INSTALL_WORDS = ("pipx", "install", "ka-telemetry")
PLAIN_PIP_WORDS = ("python3", "-m", "pip", "install", "ka-telemetry")


def readme_in(root: Path, text: str) -> Path:
    """`root` with a README.md holding `text`, as the scratch copy of a checkout holds one."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def read_failure(root: Path):
    with pytest.raises(release_metrics.InstallCommandError) as stopped:
        release_metrics.read_install_command(root)
    return stopped.value.diagnostic


def install_section(body: str) -> str:
    return f"# Tool\n\nIntro.\n\n## Install\n\n{textwrap.dedent(body)}\n## Try it\n\nText.\n"


class RecordingProcess:
    """A stand-in for `subprocess.run` that launches nothing and keeps what it was asked to run."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.settings: list[dict] = []

    def __call__(self, command, **settings):
        self.commands.append(list(command))
        self.settings.append(settings)
        return subprocess.CompletedProcess(command, 0, "", "")


def test_the_command_read_is_the_first_shell_block_in_install_only(tmp_path):
    root = readme_in(
        tmp_path,
        """\
        # Tool

        ```sh
        echo before the install section
        ```

        ## Install

        Some prose.

        ```text
        not a shell block
        ```

        ```sh
        pipx install ka-telemetry
        ```

        ```sh
        brew install pipx
        ```

        ### A subsection

        ```sh
        pipx install other-thing ka-telemetry
        ```

        ## Try it

        ```sh
        ka-telemetry --help
        ```
        """,
    )

    command = release_metrics.read_install_command(root)

    assert command.words == INSTALL_WORDS
    assert (command.text, command.source, command.line) == (
        "pipx install ka-telemetry", "README.md", 16,
    )


@pytest.mark.parametrize("language", ["sh", "shell", "bash"])
def test_sh_shell_and_bash_fences_are_shell_blocks(tmp_path, language):
    root = readme_in(tmp_path, install_section(f"```{language}\npipx install ka-telemetry\n```\n"))

    assert release_metrics.read_install_command(root).words == INSTALL_WORDS


def test_a_changed_working_file_readme_changes_the_words_read(tmp_path):
    root = readme_in(tmp_path, install_section("```sh\npipx install ka-telemetry\n```\n"))
    before = release_metrics.read_install_command(root)

    readme_in(tmp_path, install_section("```sh\npython3  -m pip   install 'ka-telemetry'\n```\n"))
    after = release_metrics.read_install_command(root)

    assert before.words == INSTALL_WORDS
    assert after.words == PLAIN_PIP_WORDS, "the words as typed, quotes decoded, nothing added"
    assert after.text == "python3  -m pip   install 'ka-telemetry'"


def test_no_install_section_no_shell_block_or_unclosed_block_each_name_their_own_cause(tmp_path):
    cases = {
        "file_unreadable": None,
        "no_install_section": "# Tool\n\n## Try it\n\n```sh\npipx install ka-telemetry\n```\n",
        "no_shell_block": install_section("Run pipx install ka-telemetry.\n\n```text\nx\n```\n"),
        "block_unclosed": install_section("```sh\npipx install ka-telemetry\n"),
    }
    found = {}
    for reading, text in cases.items():
        root = tmp_path / reading
        root.mkdir()
        if text is not None:
            readme_in(root, text)
        found[reading] = read_failure(root)

    for reading, diagnostic in found.items():
        assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
        assert diagnostic.input_or_operation and diagnostic.reason
        assert diagnostic.data["step"] == "readme"
        assert diagnostic.data["cause"] == "readme_unreadable"
        assert diagnostic.data["reading"] == reading
        assert diagnostic.data["source"] == "README.md"
    assert found["block_unclosed"].data["line"] == 7, "the line where the block opens"
    assert len({diagnostic.reason for diagnostic in found.values()}) == 4


@pytest.mark.parametrize(
    "body",
    [
        "pipx install ka-telemetry\nbrew install pipx",
        "# first install it\npipx install ka-telemetry",
        "\npipx install ka-telemetry",
        "pipx install ka-telemetry\n",
        "# only a comment",
        "",
    ],
    ids=["two commands", "comment above", "blank above", "blank below", "only a comment", "empty"],
)
def test_a_block_of_two_command_lines_or_with_a_comment_or_blank_line_is_unreadable(tmp_path, body):
    root = readme_in(tmp_path, install_section("```sh\n" + body + "\n```\n"))

    diagnostic = read_failure(root)

    assert diagnostic.data["reading"] == "not_one_line"
    assert diagnostic.data["line"] == 7
    assert diagnostic.data["source"] == "README.md"


def test_malformed_quoting_is_unreadable(tmp_path):
    root = readme_in(tmp_path, install_section("```sh\npipx install 'ka-telemetry\n```\n"))

    diagnostic = read_failure(root)

    assert diagnostic.data["reading"] == "words_unreadable"
    assert diagnostic.data["command"] == "pipx install 'ka-telemetry"
    assert diagnostic.data["line"] == 8, "the line the command stands on"
    assert diagnostic.reason


@pytest.mark.parametrize(
    "line",
    [
        "brew install pipx",
        "pipx install ./ka-telemetry",
        "/opt/ka-telemetry/bin/pipx install pipx",
        "ka-telemetry install pipx",
        "pipx install pipx # ka-telemetry",
        "pipx install ka-telemetry-extra",
    ],
)
def test_a_line_not_naming_ka_telemetry_as_an_argument_is_unreadable(tmp_path, line):
    root = readme_in(tmp_path, install_section(f"```sh\n{line}\n```\n"))

    diagnostic = read_failure(root)

    assert diagnostic.data["reading"] == "target_not_named"
    assert diagnostic.data["command"] == line
    assert diagnostic.data["line"] == 7, "the line where the block opens"


def test_the_readme_command_that_names_a_different_package_than_ka_telemetry_is_never_run(tmp_path):
    run = RecordingProcess()
    root = readme_in(tmp_path, install_section("```sh\npipx install ka-telemetry-extra\n```\n"))

    diagnostic = read_failure(root)

    assert diagnostic.data["reading"] == "target_not_named"
    assert run.commands == []


@pytest.mark.parametrize("words", [INSTALL_WORDS, PLAIN_PIP_WORDS])
def test_admission_accepts_the_two_forms_and_returns_the_words_unchanged(tmp_path, words):
    line = " ".join(words)
    root = readme_in(tmp_path, install_section(f"```sh\n{line}\n```\n"))

    admitted = release_metrics.admit_install_command(release_metrics.read_install_command(root))

    assert admitted == words


UNSAFE_FORMS = [
    ("pipx install ka-telemetry > {sentinel}", "redirection"),
    ("pipx install ka-telemetry 2> {sentinel}", "redirection"),
    ("pipx install ka-telemetry $(touch {sentinel})", "command substitution"),
    ("pipx install ka-telemetry `touch {sentinel}`", "command substitution"),
    ("pipx install ka-telemetry ; touch {sentinel}", "command chain"),
    ("pipx install ka-telemetry && touch {sentinel}", "command chain"),
    ("pipx install ka-telemetry | tee {sentinel}", "command chain"),
    ("/usr/bin/pipx install ka-telemetry", "path"),
    ("HOME={sentinel} pipx install ka-telemetry", "environment assignment"),
    ("pipx install ka-telemetry --target {sentinel}", "--target"),
    ("pipx install ka-telemetry --prefix {sentinel}", "--prefix"),
    ("pipx install ka-telemetry --user", "--user"),
    ("pipx install ka-telemetry --root {sentinel}", "--root"),
    ("pipx install --break-system-packages ka-telemetry", "--break-system-packages"),
    ("pipx install --python /usr/bin/python3 ka-telemetry", "--python"),
    ("pipx install --pip-args=--user ka-telemetry", "--pip-args"),
    ("pipx --global install ka-telemetry", "--global"),
    ("pipx --home {sentinel} install ka-telemetry", "--home"),
    ("pipx install ka-telemetry other-package", "other-package"),
    ("python3 -m pip install --target {sentinel} ka-telemetry", "--target"),
    ("python3 -m pip install --user ka-telemetry", "--user"),
    ("python3 -m pip install ka-telemetry --break-system-packages", "--break-system-packages"),
    ("python3.12 -m pip install ka-telemetry", "python3.12"),
    ("uv tool install ka-telemetry", "uv"),
]


@pytest.mark.parametrize(("line", "named"), UNSAFE_FORMS)
def test_admission_refuses_unsafe_single_line_forms_before_launch(tmp_path, line, named):
    sentinel = tmp_path / "outside" / "sentinel"
    line = line.format(sentinel=sentinel)
    root = readme_in(tmp_path / "checkout", install_section(f"```sh\n{line}\n```\n"))
    command = release_metrics.read_install_command(root)
    run = RecordingProcess()

    with pytest.raises(release_metrics.InstallCommandError) as refused:
        release_metrics.launch_install_command(command, run)

    diagnostic = refused.value.diagnostic
    assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
    assert diagnostic.data["step"] == "readme"
    assert diagnostic.data["cause"] == "command_refused"
    assert diagnostic.data["command"] == line
    assert (diagnostic.data["source"], diagnostic.data["line"]) == ("README.md", 8)
    assert named in diagnostic.reason
    assert run.commands == [], "nothing was launched"
    assert not sentinel.parent.exists(), "nothing was written outside the scratch"


def test_the_original_command_is_never_replaced_by_a_hard_coded_one(tmp_path):
    for words in (INSTALL_WORDS, PLAIN_PIP_WORDS):
        root = readme_in(tmp_path / words[0], install_section(f"```sh\n{' '.join(words)}\n```\n"))
        command = release_metrics.read_install_command(root)
        run = RecordingProcess()

        release_metrics.launch_install_command(command, run, cwd=str(tmp_path), env={"A": "b"})

        assert run.commands == [list(words)], "the admitted words, none added, none swapped"
        assert run.settings == [{"cwd": str(tmp_path), "env": {"A": "b"}}]



# ── bar 5: finding a Python that refuses pip, and bringing pipx ──

REFUSAL_REPORT = (
    "error: externally-managed-environment\n\n× This environment is externally managed\n"
)
PIPX_HELP = (
    "usage: pipx [-h] {install,upgrade-shared,ensurepath} ...\n"
    "  PIPX_HOME   Overrides default pipx location.\n"
    "  PIPX_DEFAULT_BACKEND   Overrides which backend (`pip` or `uv`) is used for new venvs.\n"
)
PIPX_HELP_THAT_ALWAYS_USES_UV = (
    "usage: pipx [-h] {install,upgrade-shared,ensurepath} ...\n"
    "  Environments are made with uv whenever uv is on the PATH.\n"
)
PIP_REPORTS = {
    "refuses": (1, "", REFUSAL_REPORT),
    "missing": (1, "", "/x/python3: No module named pip\n"),
    "permission": (1, "", "ERROR: PermissionError: [Errno 13] Permission denied: '/usr/lib'\n"),
    "network": (1, "", "WARNING: Retrying: Failed to establish a connection: Network is down\n"),
    "permissive": (1, "", "ERROR: No matching distribution found for the-package\n"),
}


class PreparationWorld:
    """The processes preparation runs, answered from what a test sets up. Any command it was not
    told about fails the test, so nothing unplanned is ever launched."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.pythons: dict[str, dict] = {}
        self.commands: list[list[str]] = []
        self.environments: list[dict] = []
        self.pipx_help = PIPX_HELP
        self.upgrade_shared = (0, "Upgraded the shared libraries.\n", "")
        self.provision = (0, "Successfully installed pipx\n", "")
        self.launch_error: OSError | None = None

    def python(self, name, *, version=(3, 12, 3), marker=True, pip="refuses", venv=False) -> str:
        prefix = self.base / "hosts" / name
        executable = prefix / "bin" / "python3"
        facts = {
            "version": list(version),
            "prefix": str(self.base / "venvs" / name) if venv else str(prefix),
            "base_prefix": str(prefix),
            "stdlib": str(prefix / f"lib/python{version[0]}.{version[1]}"),
            "marker": marker,
            "executable": str(executable),
        }
        self.pythons[str(executable)] = {"facts": facts, "pip": pip}
        return str(executable)

    def __call__(self, command, **settings):
        words = [str(part) for part in command]
        self.commands.append(words)
        self.environments.append(dict(settings.get("env") or {}))
        program, rest = words[0], words[1:]

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(words, code, out, err)

        if rest[:1] == ["-c"]:
            if program not in self.pythons:
                raise FileNotFoundError(2, "No such file or directory", program)
            return done(0, json.dumps(self.pythons[program]["facts"]))
        if rest[:3] == ["-m", "pip", "install"] and "pipx" in rest:
            if self.launch_error is not None:
                raise self.launch_error
            Path(program).parent.joinpath("pipx").touch()
            return done(*self.provision)
        if rest[:3] == ["-m", "pip", "install"]:
            entry = self.pythons[program]
            if entry["pip"] == "by marker file":
                marked = Path(entry["facts"]["stdlib"], "EXTERNALLY-MANAGED").exists()
                return done(*PIP_REPORTS["refuses" if marked else "permissive"])
            return done(*PIP_REPORTS[entry["pip"]])
        if rest[:2] == ["-m", "venv"]:
            if self.launch_error is not None:
                raise self.launch_error
            Path(rest[2], "bin").mkdir(parents=True)
            Path(rest[2], "bin", "python").touch()
            return done(0)
        if rest[:2] == ["-m", "ensurepip"]:
            if Path(self.pythons[program]["facts"]["stdlib"], "EXTERNALLY-MANAGED").exists():
                return done(*PIP_REPORTS["refuses"])  # a marked Python refuses even ensurepip
            self.pythons[program]["pip"] = "by marker file"
            return done(0)
        if program.endswith("pipx") and rest == ["--help"]:
            return done(0, self.pipx_help)
        if program.endswith("pipx") and rest == ["upgrade-shared"]:
            return done(*self.upgrade_shared)
        raise AssertionError(f"a command nothing planned for: {words}")

    def probes(self) -> list[list[str]]:
        return [words for words in self.commands if words[1:4] == ["-m", "pip", "install"]]

    def preparation(self, tmp_path: Path, *candidates: str, **collaborators):
        kept = {
            "run": self,
            "environ": {"PATH": str(tmp_path / "bin"), "HOME": "/home/caller"},
            "candidates": lambda environ: list(candidates),
            "running": candidates[0] if candidates else sys.executable,
            "find_pipx": lambda environ: None,
            "copy_interpreter": self.refuse_to_copy,
        }
        return release_metrics.Preparation(**{**kept, **collaborators})

    @staticmethod
    def refuse_to_copy(facts, destination):
        raise OSError("this test makes no copy")

    def copy_into_scratch(self, facts, destination: Path) -> Path:
        """A private copy: its own prefix and standard library, unmarked until it is marked."""
        executable = destination / "bin" / "python3"
        stdlib = destination / "lib" / f"python{facts.version[0]}.{facts.version[1]}"
        stdlib.mkdir(parents=True)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        self.pythons[str(executable)] = {
            "facts": {
                "version": list(facts.version), "prefix": str(destination),
                "base_prefix": str(destination), "stdlib": str(stdlib), "marker": False,
                "executable": str(executable),
                "installation": [
                    str(stdlib / "site-packages"), str(destination / "bin"), str(destination),
                ],
            },
            "pip": "missing",
        }
        return executable


def failed_preparation(preparation, *arguments):
    with pytest.raises(release_metrics.PreparationFailed) as stopped:
        preparation(*arguments)
    return stopped.value.diagnostic


def test_a_python_whose_pip_refuses_as_externally_managed_is_chosen(tmp_path):
    preparation = release_metrics.real_preparation(dict(os.environ))
    try:
        chosen = preparation.base_python(tmp_path)
    except release_metrics.PreparationFailed as failure:
        pytest.skip(f"this machine has no Python that refuses pip installs: {failure}")
    attempt = subprocess.run(
        [str(chosen.executable), "-m", "pip", "install", "--no-index", "no-such-package-here"],
        capture_output=True, text=True, check=False,
    )

    assert attempt.returncode != 0 and "externally-managed-environment" in attempt.stderr
    assert chosen.version >= (3, 11)
    assert chosen.identity


def test_the_chosen_python_is_the_first_that_refuses_and_its_identity_is_recorded(tmp_path):
    world = PreparationWorld(tmp_path)
    permissive = world.python("permissive", marker=False)
    good = world.python("good")
    preparation = world.preparation(tmp_path, permissive, good)

    chosen = preparation.base_python(tmp_path / "scratch")

    assert chosen.executable == Path(good)
    assert chosen.version == (3, 12, 3)
    assert chosen.identity == os.path.realpath(tmp_path / "hosts" / "good")


@pytest.mark.parametrize(
    ("pip", "outcome"),
    [("missing", "no_pip"), ("permission", "not_refused"), ("network", "not_refused")],
)
def test_a_marker_alone_is_not_evidence(tmp_path, pip, outcome):
    world = PreparationWorld(tmp_path)
    marked = world.python("marked", marker=True, pip=pip)
    preparation = world.preparation(tmp_path, marked)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    [first, *_] = diagnostic.data["candidates"]
    assert (first["python"], first["outcome"]) == (marked, outcome)
    if outcome == "not_refused":
        assert first["reason"] == PIP_REPORTS[pip][2].strip()


def test_a_python_inside_a_virtual_environment_is_never_chosen(tmp_path):
    world = PreparationWorld(tmp_path)
    caller_venv = world.python("caller", venv=True)
    marked_venv = world.python("marked", venv=True)
    preparation = world.preparation(tmp_path, caller_venv, marked_venv)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    assert [(c["python"], c["outcome"]) for c in diagnostic.data["candidates"][:2]] == [
        (caller_venv, "in_virtual_environment"), (marked_venv, "in_virtual_environment"),
    ]
    assert world.probes() == [], "a virtual environment is never probed"


def test_a_python_older_than_3_11_is_never_chosen(tmp_path):
    world = PreparationWorld(tmp_path)
    old = world.python("old", version=(3, 10, 14))
    preparation = world.preparation(tmp_path, old)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    [first, *_] = diagnostic.data["candidates"]
    assert (first["python"], first["outcome"], first["version"]) == (old, "too_old", "3.10.14")
    assert world.probes() == []


def test_a_permissive_candidate_installs_nothing_during_the_probe(tmp_path):
    world = PreparationWorld(tmp_path)
    permissive = world.python("permissive", marker=True, pip="permissive")
    preparation = world.preparation(tmp_path, permissive)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    [probe] = world.probes()
    assert "--no-index" in probe and "--no-cache-dir" in probe
    assert probe[-1] == release_metrics.NONEXISTENT_PACKAGE
    assert probe[-1] not in {"ka-telemetry", "pip"} and not Path(probe[-1]).suffix
    assert diagnostic.data["candidates"][0]["outcome"] == "not_refused"


def test_every_preparation_child_gets_a_rebuilt_environment(tmp_path):
    world = PreparationWorld(tmp_path)
    good = world.python("good")
    hostile = {
        "PATH": str(tmp_path / "bin"), "HOME": "/home/caller", "PIPX_HOME": "/home/caller/pipx",
        "PIP_INDEX_URL": "http://example.invalid/simple", "UV_CACHE_DIR": "/home/caller/uv",
        "VIRTUAL_ENV": "/home/caller/venv", "PYTHONPATH": "/home/caller/lib",
        "https_proxy": "http://proxy.invalid:3128",
    }
    preparation = world.preparation(tmp_path, good, environ=hostile)
    scratch = tmp_path / "scratch"

    preparation.base_python(scratch)

    for environment in world.environments:
        assert not [v for v in environment.values() if "/home/caller" in v or ".invalid" in v]
        assert "https_proxy" not in environment
        assert Path(environment["HOME"]).is_relative_to(scratch)
        assert environment["PIP_CONFIG_FILE"] == os.devnull
        assert environment["PYTHONNOUSERSITE"] == "1"


def test_with_no_usable_candidate_a_private_copy_is_made_inside_scratch_and_probed(tmp_path):
    world = PreparationWorld(tmp_path)
    host = world.python("host", marker=False)
    host_tree = tmp_path / "hosts" / "host"
    (host_tree / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (host_tree / "lib" / "python3.12" / "os.py").write_text("# host\n", encoding="utf-8")
    before = digest(host_tree)
    scratch = tmp_path / "scratch"
    copies = []

    def copy(facts, destination):
        copies.append(destination)
        return world.copy_into_scratch(facts, destination)

    preparation = world.preparation(tmp_path, host, copy_interpreter=copy)

    chosen = preparation.base_python(scratch)

    assert len(copies) == 1 and copies[0].is_relative_to(scratch)
    assert chosen.executable.is_relative_to(scratch)
    marker = Path(world.pythons[str(chosen.executable)]["facts"]["stdlib"]) / "EXTERNALLY-MANAGED"
    assert marker.is_file() and marker.is_relative_to(scratch)
    assert digest(host_tree) == before, "the host's marker, site-packages and prefix are unchanged"
    assert not (host_tree / "lib" / "python3.12" / "EXTERNALLY-MANAGED").exists()
    on_copy = [words[1:3] for words in world.commands if words[0] == str(chosen.executable)]
    assert ["-m", "ensurepip"] in on_copy
    assert on_copy.index(["-m", "ensurepip"]) < on_copy.index(["-m", "pip"]), "pip, then the probe"


def python_facts(path: str) -> dict | None:
    done = subprocess.run(
        [path, "-c", release_metrics.FACTS_SCRIPT], capture_output=True, text=True, check=False
    )
    return json.loads(done.stdout) if done.returncode == 0 else None


def a_permissive_python() -> str | None:
    """A real Python 3.11 or later that is not a virtual environment and carries no marker, among
    the ones the release check looks at and the ones pyenv keeps."""
    pyenv = sorted(Path.home().glob(".pyenv/versions/*/bin/python3"))
    for path in [
        release_metrics.running_base_python(),
        *release_metrics.path_python_candidates(os.environ),
        *map(str, pyenv),
    ]:
        facts = python_facts(path)
        if facts and tuple(facts["version"]) >= (3, 11) and not facts["marker"]:
            if facts["prefix"] == facts["base_prefix"]:
                return path
    return None


def test_a_real_private_copy_of_a_permissive_python_refuses_pip_and_leaves_the_host_alone(
    tmp_path,
):
    permissive = a_permissive_python()
    if permissive is None:
        pytest.skip("this machine has no Python 3.11 or later without the marker")
    marker = Path(python_facts(permissive)["stdlib"]) / "EXTERNALLY-MANAGED"
    preparation = release_metrics.Preparation(
        run=subprocess.run, environ=dict(os.environ), candidates=lambda environ: [],
        copy_interpreter=release_metrics.copy_host_interpreter, running=permissive,
        find_pipx=release_metrics.find_pipx_on_path,
    )

    chosen = preparation.base_python(tmp_path / "scratch")

    attempt = subprocess.run(
        [str(chosen.executable), "-m", "pip", "install", "--no-index", "no-such-package-here"],
        capture_output=True, text=True, check=False,
    )
    assert chosen.executable.is_relative_to(tmp_path / "scratch")
    assert "externally-managed-environment" in attempt.stderr
    assert not marker.exists(), "no marker was written into the host's prefix"


def test_a_copy_whose_standard_library_is_outside_scratch_is_never_marked(tmp_path):
    world = PreparationWorld(tmp_path)
    host = world.python("host", marker=False)
    outside = tmp_path / "hosts" / "host" / "lib" / "python3.12"
    outside.mkdir(parents=True)

    def copy(facts, destination):
        executable = world.copy_into_scratch(facts, destination)
        world.pythons[str(executable)]["facts"]["stdlib"] = str(outside)
        return executable

    preparation = world.preparation(tmp_path, host, copy_interpreter=copy)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    assert not (outside / "EXTERNALLY-MANAGED").exists()
    assert diagnostic.data["candidates"][-1]["outcome"] == "copy_failed"
    assert diagnostic.data["candidates"][-1]["reason"]


def test_a_copy_whose_installation_paths_are_outside_scratch_gets_no_pip_and_no_marker(tmp_path):
    world = PreparationWorld(tmp_path)
    host = world.python("host", marker=False)
    host_site = tmp_path / "hosts" / "host" / "lib" / "python3.12" / "site-packages"
    host_site.mkdir(parents=True)
    made = []

    def copy(facts, destination):
        executable = world.copy_into_scratch(facts, destination)
        world.pythons[str(executable)]["facts"]["installation"][0] = str(host_site)
        made.append(executable)
        return executable

    preparation = world.preparation(tmp_path, host, copy_interpreter=copy)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    [copy_executable] = made
    on_copy = [words[1:3] for words in world.commands if words[0] == str(copy_executable)]
    assert ["-m", "ensurepip"] not in on_copy, "pip is never bootstrapped toward a host prefix"
    copy_stdlib = Path(world.pythons[str(copy_executable)]["facts"]["stdlib"])
    assert not (copy_stdlib / "EXTERNALLY-MANAGED").exists(), "the copy is never marked"
    assert list(host_site.iterdir()) == []
    [*_, last] = diagnostic.data["candidates"]
    assert last["outcome"] == "copy_failed"
    assert str(host_site) in last["reason"]


def test_with_neither_route_available_the_python_unavailable_diagnostic_lists_the_candidates(
    tmp_path,
):
    world = PreparationWorld(tmp_path)
    old = world.python("old", version=(3, 9, 6))
    permissive = world.python("permissive", marker=False)
    preparation = world.preparation(tmp_path, old, permissive)

    diagnostic = failed_preparation(preparation.base_python, tmp_path / "scratch")

    assert diagnostic.kind == release_metrics.KIND_INSTALL_STEP_FAILED
    assert (diagnostic.data["step"], diagnostic.data["cause"]) == ("python", "python_unavailable")
    assert [(c["python"], c["outcome"]) for c in diagnostic.data["candidates"]] == [
        (old, "too_old"), (permissive, "not_marked"), (permissive, "copy_failed"),
    ]
    assert diagnostic.data["candidates"][2]["reason"] == "this test makes no copy"
    assert diagnostic.input_or_operation and diagnostic.reason


def chosen_python(world: PreparationWorld, tmp_path: Path):
    good = world.python("good")
    return release_metrics.BasePython(
        Path(good), (3, 12, 3), os.path.realpath(tmp_path / "hosts" / "good")
    )


def test_a_pipx_on_the_path_is_used_and_nothing_is_provisioned(tmp_path):
    world = PreparationWorld(tmp_path)
    base = chosen_python(world, tmp_path)
    preparation = world.preparation(
        tmp_path, str(base.executable), find_pipx=lambda environ: "/opt/bin/pipx"
    )

    prepared = preparation.pipx(tmp_path / "scratch", base)

    assert prepared.executable == Path("/opt/bin/pipx") and not prepared.provisioned
    assert not [words for words in world.commands if words[1:3] in (["-m", "venv"], ["-m", "pip"])]


def test_without_pipx_one_is_provisioned_inside_scratch_in_its_own_tooling_environment(tmp_path):
    world = PreparationWorld(tmp_path)
    base = chosen_python(world, tmp_path)
    scratch = tmp_path / "scratch"
    preparation = world.preparation(tmp_path, str(base.executable))

    prepared = preparation.pipx(scratch, base)

    venv_command, install_command = world.commands[:2]
    tooling = Path(venv_command[3])
    assert venv_command[:3] == [str(base.executable), "-m", "venv"]
    assert tooling.is_relative_to(scratch)
    assert install_command == [str(tooling / "bin" / "python"), "-m", "pip", "install", "pipx"]
    assert prepared.provisioned and prepared.executable == tooling / "bin" / "pipx"
    pipx_home = Path(prepared.environment["PIPX_HOME"])
    assert pipx_home.is_relative_to(scratch) and not pipx_home.is_relative_to(tooling)


@pytest.mark.parametrize("launch", ["completed", "not started"])
def test_a_failed_provision_returns_the_tool_result_and_runs_nothing_else(tmp_path, launch):
    world = PreparationWorld(tmp_path)
    base = chosen_python(world, tmp_path)
    if launch == "completed":
        world.provision = (2, "collecting pipx\n", "ERROR: no network\n")
    else:
        world.launch_error = FileNotFoundError(2, "No such file or directory", "python3")
    preparation = world.preparation(tmp_path, str(base.executable))

    diagnostic = failed_preparation(preparation.pipx, tmp_path / "scratch", base)

    assert (diagnostic.data["step"], diagnostic.data["cause"]) == ("pipx", "pipx_unavailable")
    if launch == "completed":
        assert diagnostic.data["returncode"] == 2
        assert (diagnostic.data["stdout"], diagnostic.data["stderr"]) == (
            "collecting pipx\n", "ERROR: no network\n",
        )
    else:
        assert "returncode" not in diagnostic.data
        assert diagnostic.reason == "No such file or directory"
    assert not [words for words in world.commands if words[-1] == "upgrade-shared"]


def test_the_shared_pip_environment_is_prepared_before_the_measured_phase_even_on_a_host_without_uv(
    tmp_path,
):
    world = PreparationWorld(tmp_path)
    base = chosen_python(world, tmp_path)
    scratch = tmp_path / "scratch"
    no_uv = tmp_path / "no-uv-bin"
    preparation = world.preparation(
        tmp_path, str(base.executable), find_pipx=lambda environ: "/opt/bin/pipx",
        environ={"PATH": str(no_uv), "PIPX_HOME": "/home/caller/pipx"},
    )

    prepared = preparation.pipx(scratch, base)

    assert world.commands[-1] == ["/opt/bin/pipx", "upgrade-shared"]
    environment = world.environments[-1]
    assert environment["PIPX_DEFAULT_BACKEND"] == "pip"
    assert environment["PIPX_DEFAULT_PYTHON"] == str(base.executable)
    assert environment["PATH"] == str(no_uv)
    for name in ("PIPX_HOME", "PIPX_BIN_DIR", "PIPX_MAN_DIR", "PIPX_SHARED_LIBS"):
        assert Path(environment[name]).is_relative_to(scratch), name
    assert prepared.environment["PIPX_DEFAULT_BACKEND"] == "pip"


@pytest.mark.parametrize("failure", ["no pip backend", "shared libraries"])
def test_a_pipx_that_cannot_select_the_pip_backend_is_a_preparation_failure_not_a_changed_command(
    tmp_path, failure
):
    world = PreparationWorld(tmp_path)
    base = chosen_python(world, tmp_path)
    if failure == "no pip backend":
        world.pipx_help = PIPX_HELP_THAT_ALWAYS_USES_UV
    else:
        world.upgrade_shared = (1, "", "ERROR: No matching distribution found for pip\n")
    preparation = world.preparation(
        tmp_path, str(base.executable), find_pipx=lambda environ: "/opt/bin/pipx"
    )

    diagnostic = failed_preparation(preparation.pipx, tmp_path / "scratch", base)

    assert (diagnostic.data["step"], diagnostic.data["cause"]) == ("pipx", "pipx_not_prepared")
    assert diagnostic.data["pipx"] == "/opt/bin/pipx"
    assert diagnostic.reason
    assert not [words for words in world.commands if "install" in words[1:]]



# ── metric 7: nothing leaves the machine ──

CAUSE_NO_LIST = "no_command_list"
CAUSE_HELP_INCOMPLETE = "help_incomplete"
CAUSE_NO_RUN = "no_run"
CAUSE_DID_NOT_COMPLETE = "did_not_complete"
CAUSE_NOT_STARTED = "not_started"
CAUSE_TIMED_OUT = "timed_out"
CAUSE_RECORD = "record_unreadable"
CAUSE_NOT_LISTENING = "receiver_not_listening"
CAUSE_REQUEST = "receiver_request_not_accepted"
CAUSE_STOPPED_EARLY = "receiver_stopped_early"
ALL_COMMANDS = ("runs", "steps", "context", "receive", "agents", "check", "turn-on")
DOCUMENTATION_ADDRESS = "192.0.2.1"
Attempt = release_network.Attempt
LOOPBACK = release_network.Listening("inet", "127.0.0.1", 40123)
BOTH_ACCEPTED = (
    release_network.RequestOutcome("/v1/logs", 200),
    release_network.RequestOutcome("/v1/metrics", 200),
)
RECEIVE_ARGUMENTS = (
    "receive", "--port", "0", "--dir", "/scratch/receiver", "--publish-endpoint", "/scratch/e"
)


def ended(kind: str, **fields) -> release_network.Outcome:
    return release_network.Outcome(kind, **fields)


def exited(status: int) -> release_network.Outcome:
    return ended(release_network.EXITED, status=status)


def help_listing(names=ALL_COMMANDS) -> str:
    """`ka-telemetry --help` as argparse prints it for this product's command group."""
    entries = "".join(
        f"    {name:<19} what {name} does, said across\n{'':24}two lines\n" for name in names
    )
    choices = "{" + ",".join(names) + "}"
    return (
        f"usage: ka-telemetry [-h] {choices} ...\n\nReads the transcripts.\n\n"
        "options:\n  -h, --help            show this help message and exit\n\n"
        f"commands:\n  {choices}\n{entries}\nThis tool sends no telemetry.\n"
    )


def seen(arguments: tuple[str, ...], **fields) -> release_network.Observation:
    """What a watched run gave: a clean exit unless the test says otherwise."""
    return release_network.Observation(
        arguments,
        fields.get("stdout", ""),
        fields.get("stderr", ""),
        fields.get("outcome", exited(0)),
        tuple(fields.get("attempts", ())),
        tuple(fields.get("listening", ())),
        tuple(fields.get("requests", ())),
        fields.get("evidence_problem"),
    )


def clean_receiver(**changes) -> release_network.Observation:
    kept = {
        "outcome": ended(release_network.COMPLETED),
        "listening": (LOOPBACK,),
        "requests": BOTH_ACCEPTED,
    }
    return seen(RECEIVE_ARGUMENTS, **{**kept, **changes})


class ScriptedWatcher:
    """A watcher that answers from what a test hands it, and keeps what it was asked to run."""

    def __init__(self, listing: str | None = None, answers=None, help_seen=None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers = dict(answers or {})
        self.help_seen = help_seen or seen(("--help",), stdout=listing or help_listing())

    def finite(self, arguments: tuple[str, ...]) -> release_network.Observation:
        self.calls.append(tuple(arguments))
        if tuple(arguments) == ("--help",):
            return self.help_seen
        return self.answers.get(arguments[0]) or seen(tuple(arguments))

    def receiver(self, logs_body: bytes, metrics_body: bytes) -> release_network.Observation:
        self.calls.append(("receive",))
        json.loads(logs_body), json.loads(metrics_body)
        return self.answers.get("receive") or clean_receiver()

    def started(self) -> list[str]:
        return [call[0] for call in self.calls if call != ("--help",)]


def measured(watcher: ScriptedWatcher, checkout: Path = PRODUCT_ROOT):
    return release_metrics.measure_network(checkout, watching=lambda _checkout, _scratch: watcher)


def causes(found) -> list[str]:
    unmeasured = release_metrics.KIND_NETWORK_UNMEASURED
    return [d.data.get("cause") for d in found if d.kind == unmeasured]


def test_the_help_derived_set_is_compared_with_completed_runs():
    watcher = ScriptedWatcher()

    assert measured(watcher) == []
    assert sorted(watcher.started()) == sorted(ALL_COMMANDS)
    assert watcher.calls[0] == ("--help",)


def test_a_listing_is_read_from_the_commands_section_of_help():
    assert release_metrics.parse_command_listing(help_listing()) == list(ALL_COMMANDS)
    assert release_metrics.parse_command_listing(help_listing(("export", "runs"))) == [
        "export",
        "runs",
    ]


def test_an_eighth_listed_command_with_no_exercise_fails_as_unmeasured():
    watcher = ScriptedWatcher(help_listing((*ALL_COMMANDS, "export")))

    found = measured(watcher)

    [unmeasured] = [d for d in found if d.kind == release_metrics.KIND_NETWORK_UNMEASURED]
    assert unmeasured.data == {"cause": CAUSE_NO_RUN, "name": "export"}
    assert "export" in unmeasured.input_or_operation and unmeasured.reason
    assert sorted(watcher.started()) == sorted(ALL_COMMANDS), "the others are still measured"


@pytest.mark.parametrize(
    ("fields", "cause"),
    [
        ({"stdout": ""}, CAUSE_NO_LIST),
        ({"stdout": "usage: ka-telemetry\n\nno commands here\n"}, CAUSE_NO_LIST),
        ({"stdout": "commands:\n  {}\n"}, CAUSE_NO_LIST),
        (
            {"stdout": help_listing(), "stderr": "bad usage", "outcome": exited(2)},
            CAUSE_HELP_INCOMPLETE,
        ),
        (
            {"outcome": ended(release_network.NOT_STARTED, reason="no such file")},
            CAUSE_HELP_INCOMPLETE,
        ),
        (
            {"outcome": ended(release_network.TIMED_OUT, seconds=60, reason="slow")},
            CAUSE_HELP_INCOMPLETE,
        ),
    ],
    ids=["empty", "no section", "empty section", "nonzero", "not started", "timed out"],
)
def test_empty_malformed_or_failed_help_fails(fields, cause):
    watcher = ScriptedWatcher(help_seen=seen(("--help",), **fields))

    found = measured(watcher)

    assert causes(found) == [cause]
    assert found[0].input_or_operation == "python3 -m ka_telemetry --help"
    assert watcher.started() == [], "no command is run without a list to run"


def test_a_nonzero_command_and_a_timeout_each_fail_with_command_and_report():
    projects = str(PRODUCT_ROOT / "tests/parity/projects")
    watcher = ScriptedWatcher(
        answers={
            "steps": seen(
                ("steps", "--projects-root", projects), stderr="boom\n", outcome=exited(3)
            ),
            "context": seen(
                ("context", "x.jsonl"),
                outcome=ended(release_network.TIMED_OUT, seconds=60, reason="slow"),
            ),
            "agents": seen(
                ("agents",), outcome=ended(release_network.NOT_STARTED, reason="denied")
            ),
        }
    )

    found = measured(watcher)

    by_cause = {d.data["cause"]: d for d in found}
    assert set(by_cause) == {
        CAUSE_DID_NOT_COMPLETE,
        CAUSE_TIMED_OUT,
        CAUSE_NOT_STARTED,
    }
    nonzero = by_cause[CAUSE_DID_NOT_COMPLETE]
    assert nonzero.input_or_operation == (
        "python3 -m ka_telemetry steps --projects-root tests/parity/projects"
    )
    assert nonzero.reason == "boom"
    assert by_cause[CAUSE_TIMED_OUT].data["seconds"] == 60
    assert by_cause[CAUSE_NOT_STARTED].reason == "denied"
    assert by_cause[CAUSE_NOT_STARTED].input_or_operation == (
        "python3 -m ka_telemetry agents"
    )


def test_a_caught_attempt_with_exit_zero_still_fails():
    attempt = release_network.Attempt("connect", DOCUMENTATION_ADDRESS, 9)
    watcher = ScriptedWatcher(answers={"check": seen(("check",), attempts=[attempt])})

    found = measured(watcher)

    [diagnostic] = found
    assert diagnostic.kind == release_metrics.KIND_NETWORK_ATTEMPT


def test_the_attempt_diagnostic_names_the_command_as_typed_and_the_destination_with_port():
    arguments = ("check", "--dir", str(PRODUCT_ROOT / "tests/stream_parity"))
    attempts = [
        release_network.Attempt("connect", DOCUMENTATION_ADDRESS, 9),
        release_network.Attempt("getaddrinfo", "example.com", None),
    ]
    watcher = ScriptedWatcher(answers={"check": seen(arguments, attempts=attempts)})

    found = measured(watcher)

    typed = "python3 -m ka_telemetry check --dir tests/stream_parity"
    assert [d.input_or_operation for d in found] == [typed, typed]
    assert [d.data for d in found] == [
        {"operation": "connect", "destination": DOCUMENTATION_ADDRESS, "port": 9},
        {"operation": "getaddrinfo", "destination": "example.com", "port": None},
    ]
    assert all(d.reason for d in found)


def test_a_listed_command_names_every_offender_in_one_failure():
    watcher = ScriptedWatcher(
        answers={
            "steps": seen(("steps",), attempts=[Attempt("connect", "192.0.2.1", 9)]),
            "check": seen(("check",), attempts=[Attempt("sendto", "192.0.2.2", 53)]),
            "runs": seen(("runs",), outcome=exited(1), stderr="broken"),
        }
    )

    found = measured(watcher)

    attempts = [d for d in found if d.kind == release_metrics.KIND_NETWORK_ATTEMPT]
    assert [d.data["destination"] for d in attempts] == ["192.0.2.1", "192.0.2.2"]
    assert causes(found) == [CAUSE_DID_NOT_COMPLETE]
    assert sorted(watcher.started()) == sorted(ALL_COMMANDS)


def real_help() -> str:
    return subprocess.run(
        [sys.executable, "-m", "ka_telemetry", "--help"],
        capture_output=True,
        text=True,
        check=True,
        cwd=PRODUCT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PRODUCT_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
    ).stdout


def test_exercises_cover_every_discovered_command_with_saved_agents_for_runs_and_a_scratch_settings_write_for_turn_on(  # noqa: E501
    tmp_path,
):
    listed = release_metrics.parse_command_listing(real_help())
    plan = release_metrics.exercise_arguments(PRODUCT_ROOT, tmp_path)

    assert set(plan) | {"receive"} == set(listed)
    runs = plan["runs"]
    assert runs[runs.index("--agents-json") + 1] == str(PRODUCT_ROOT / "tests/parity/agents.json")
    assert "--session" in runs, "the saved named-session input is used"
    assert plan["agents"][-1] == str(PRODUCT_ROOT / "tests/stream_parity")
    assert plan["check"][-1] == str(PRODUCT_ROOT / "tests/stream_parity")
    assert Path(plan["context"][-1]).suffix == ".jsonl"
    settings = Path(plan["turn-on"][plan["turn-on"].index("--settings-file") + 1])
    assert tmp_path in settings.parents


def spying_watcher():
    """A factory for the real watcher that also keeps what `turn-on` wrote and the receiver's
    observation, read before the scratch tree goes."""

    class Spy(release_metrics.SourceWatcher):
        settings: str | None = None
        receiver_seen: release_network.Observation | None = None

        def finite(self, arguments):
            observed = super().finite(arguments)
            if arguments[0] == "turn-on":
                written = Path(arguments[arguments.index("--settings-file") + 1])
                Spy.settings = written.read_text(encoding="utf-8") if written.exists() else None
            return observed

        def receiver(self, logs_body, metrics_body):
            Spy.receiver_seen = super().receiver(logs_body, metrics_body)
            return Spy.receiver_seen

    return Spy


def test_the_unbroken_copy_holds():
    spy = spying_watcher()

    found = release_metrics.measure_network(PRODUCT_ROOT, watching=spy)

    assert found == []
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in (spy.settings or ""), "turn-on really wrote"
    receiver = spy.receiver_seen
    assert receiver.outcome.kind == release_network.COMPLETED
    assert [listening.host for listening in receiver.listening] == ["127.0.0.1"]
    assert all(request.accepted for request in receiver.requests)


def test_receive_requires_a_real_loopback_listening_socket_and_both_requests_accepted():
    for host, family in (("127.0.0.1", "inet"), ("127.0.0.7", "inet"), ("::1", "inet6")):
        listening = release_network.Listening(family, host, 40123)
        watcher = ScriptedWatcher(answers={"receive": clean_receiver(listening=(listening,))})
        assert measured(watcher) == [], host


REFUSED = (
    release_network.RequestOutcome("/v1/logs", 200),
    release_network.RequestOutcome("/v1/metrics", 500, "status 500 Server Error"),
)


@pytest.mark.parametrize(
    "changes",
    [
        {"listening": ()},
        {"evidence_problem": "its record has no ready line"},
        {
            "outcome": ended(release_network.REQUEST_NOT_ACCEPTED, reason="status 500"),
            "requests": REFUSED,
        },
        {"outcome": ended(release_network.NEVER_LISTENED, reason="port in use"), "listening": ()},
        {"outcome": ended(release_network.STOPPED_EARLY, reason="it ended"), "requests": ()},
    ],
    ids=["no listen record", "bad evidence", "request refused", "never listened", "stopped early"],
)
def test_missing_listen_evidence_or_a_failed_or_missing_request_fails(changes):
    watcher = ScriptedWatcher(answers={"receive": clean_receiver(**changes)})

    found = measured(watcher)

    assert found and all(d.kind == release_metrics.KIND_NETWORK_UNMEASURED for d in found)
    assert all(d.input_or_operation.startswith("python3 -m ka_telemetry receive") for d in found)
    assert {d.data["cause"] for d in found} <= {
        CAUSE_RECORD,
        CAUSE_REQUEST,
        CAUSE_NOT_LISTENING,
        CAUSE_STOPPED_EARLY,
    }
    refused = [d for d in found if d.data["cause"] == CAUSE_REQUEST]
    for diagnostic in refused:
        assert diagnostic.data["path"] == "/v1/metrics"
        assert diagnostic.reason == "status 500 Server Error"


def test_a_wildcard_default_bind_is_a_default_address_diagnostic_naming_the_address():
    wildcard = release_network.Listening("inet", "0.0.0.0", 40123)
    watcher = ScriptedWatcher(answers={"receive": clean_receiver(listening=(wildcard,))})

    found = measured(watcher)

    [diagnostic] = found
    assert diagnostic.kind == release_metrics.KIND_NETWORK_DEFAULT_ADDRESS
    assert diagnostic.data == {"address": "0.0.0.0", "port": 40123}
    assert diagnostic.input_or_operation.startswith("python3 -m ka_telemetry receive")
    assert "0.0.0.0" in diagnostic.reason


def test_a_misleading_loopback_start_line_cannot_make_it_hold():
    wildcard = release_network.Listening("inet6", "::", 40123)
    misleading = clean_receiver(
        listening=(wildcard,), stdout="listening on 127.0.0.1:40123\n", stderr="127.0.0.1\n"
    )

    found = measured(ScriptedWatcher(answers={"receive": misleading}))

    assert [d.kind for d in found] == [release_metrics.KIND_NETWORK_DEFAULT_ADDRESS]
    assert found[0].data["address"] == "::"
