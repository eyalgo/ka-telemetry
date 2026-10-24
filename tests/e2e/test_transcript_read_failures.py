"""End-to-end proof that `runs` and `steps` say why they could not read, on the real commands.

The station this change walks (`ka-telemetry · shukuba · 2026-10-13_transcript-reports-say-why-they-
could-not-read`, § Reached when) names its fixtures. They are built here and nowhere else:

- `R`: a copy of `tests/parity/projects/`, `E`: an empty home;
- `T`: the run transcript `R/<D>/a839285c-….jsonl`, deleted, or at mode 000;
- `D`: the project directory `R/-home-user-work-demo`, at mode 000; and
  `R` itself at mode 000.

Every command is a child process of the absolute interpreter running this suite, under `E` as `HOME`
and an empty directory as `PATH`. The bytes a command printed at `1130e57`, the commit before this
change, were captured once in a disposable clone of that commit and are stored in
`transcript_read_failures_baseline.json`: nothing there was produced by the code under test.
The bytes `steps` and every `--help` printed at `e671a05`, the commit before the change that named
the places below a project directory, were captured the same way into
`steps_places_baseline.json`, with the fixture's root written `{root}`.

A fixture that needs mode 000 is skipped when the suite runs as root, because mode 000 stops no read
there; the clean and absent fixtures run either way.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import sealed_paths
from e2e.support import api_request_body

from ka_telemetry.transcript_notes import SPAN_LINE as STATEMENT

PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
PARITY = PRODUCT_ROOT / "tests" / "parity"
AGENTS_JSON = PARITY / "agents.json"
BASELINE = json.loads(
    (Path(__file__).parent / "transcript_read_failures_baseline.json").read_text(encoding="utf-8")
)

PLACES_BASELINE = json.loads(
    (Path(__file__).parent / "steps_places_baseline.json").read_text(encoding="utf-8")
)

RUN_SESSION = "a839285c-263f-40a6-8801-71f49d00cc68"
OTHER_RUN_SESSION = "15b8016a-f529-44fe-b6ea-1013fea8a4a8"
NOZOKI_RUN_SESSION = "7ef055ef-5823-4b0d-845c-efc5b4fdab28"
FLOW_SESSION = "174d0111-bfa9-4539-92d9-5560e0ab4c36"
KENSAI = "-home-user-work-demo"
NOZOKI = "-home-user-code-reader"
WORKFLOW = "wf_ec5e24de-808"
WORKFLOW_DIR = f"{KENSAI}/{FLOW_SESSION}/subagents/workflows/{WORKFLOW}"

GONE = "gone"
NOT_OPENED = "could not be opened"
THIRD = "a directory its transcript may be in could not be looked in"
LOOKED_IN = "could not be looked in"
DIRECTORY_LINE = (
    "{directory} could not be looked in, so nothing in it is in this report. "
    "Run this command again once it can be looked in."
)
NO_COST = "This report carries no cost, because ka-telemetry never computes a cost from tokens."
NO_RUNS = "No runs read."
NO_WORKFLOWS = "No workflow runs read."
UNCOUNTED_LINE = (
    "{directory} could not be listed, so its agent files could not be counted and this report "
    "has no count of agent files with no journal entry at all for its workflow run. "
    "The system reported: {reason}. Run this command again once that directory can be listed."
)
FOURTH_FIGURE = re.compile(r" / \d+ agent files with no journal entry at all")
DENIED = "Permission denied"
ORPHAN_FILE = "agent-orphan000000.jsonl"
ORPHAN_SOURCE = "agent-a53ef7efd8c468bad.jsonl"
SECOND_SESSION = "274d0111-bfa9-4539-92d9-5560e0ab4c36"
SECOND_WORKFLOW = "wf_second-000"
SECOND_WORKFLOW_DIR = f"{KENSAI}/{SECOND_SESSION}/subagents/workflows/{SECOND_WORKFLOW}"
FORMS = ((), ("--json",), ("--wf", WORKFLOW), ("--wf", WORKFLOW, "--json"))
POINTER = "ka-telemetry agents"
WF_MISSING_ERROR = re.compile(r"error: no workflow run found for --wf \S+ under .+ — run `ka-")


# ── Running the real commands ──────────────────────────────────────────────


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "E"
    home.mkdir(exist_ok=True)
    return home


def _stream_home(tmp_path: Path, *session_ids: str) -> Path:
    """A home whose `.ka-telemetry/logs.jsonl` holds one `api_request` per session id."""
    home = tmp_path / "H"
    stream = home / ".ka-telemetry"
    stream.mkdir(parents=True)
    lines = [json.dumps({"body": api_request_body(sid, 0.5)}) for sid in session_ids]
    (stream / "logs.jsonl").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return home


def _ka(tmp_path: Path, home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-B", "-m", "ka_telemetry", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=home,
        env={"HOME": str(home), "PATH": str(empty_path), "PYTHONPATH": str(PRODUCT_ROOT)},
    )


def _runs(tmp_path, root, *extra, home=None):
    """`runs` as the baselines hold it: the statement this change adds is not in what is returned.
    `test_every_exit_zero_report_carries_the_statement_once` is where it is asserted."""
    done = _ka(
        tmp_path,
        home or _home(tmp_path),
        "runs", "--agents-json", str(AGENTS_JSON), "--projects-root", str(root), *extra,
    )
    return subprocess.CompletedProcess(
        done.args, done.returncode, _without_statement(done.stdout), done.stderr
    )


SPAN_LINE = re.compile(r"^    span=[^\n]*\n", re.MULTILINE)
SPAN_MEMBER = re.compile(r'^ *"span_seconds": [^\n]*,\n', re.MULTILINE)


def _without_statement(text: str) -> str:
    """A report with the one statement of what its time covers taken out, and nothing else
    changed: the text form's whole line, or the JSON form's top-level `span` member line."""
    statement_lines = (f"{STATEMENT}\n", f'  "span": {json.dumps(STATEMENT)},\n')
    return "".join(
        line for line in text.splitlines(keepends=True) if line not in statement_lines
    )


def _without_spans(text: str) -> str:
    """`steps` output with the span this change adds taken out, and nothing else changed: the
    text form's `    span=` lines, or the JSON form's `span_seconds` member lines (the first
    member of a row, so each ends in a comma). The bytes that remain are what the stored baselines
    hold; the lines are removed, never the text reserialised."""
    return _without_statement(SPAN_MEMBER.sub("", SPAN_LINE.sub("", text)))


def _steps(tmp_path, root, *extra, home=None):
    """`steps` as the baselines hold it: the span lines and members this change adds are not in
    what is returned (`runs` has none and is never projected)."""
    done = _ka(tmp_path, home or _home(tmp_path), "steps", "--projects-root", str(root), *extra)
    return subprocess.CompletedProcess(
        done.args, done.returncode, _without_spans(done.stdout), done.stderr
    )


def _tree(tmp_path: Path, name: str = "R") -> Path:
    """A copy of the reference projects, `R`."""
    root = tmp_path / name
    shutil.copytree(PARITY / "projects", root)
    return root


def _by_name(tmp_path, root, name, **options):
    """All the outputs of both commands that the baseline holds under `name`, as a dict."""
    wanted = BASELINE[name]
    runs_argv = ("--session", RUN_SESSION) if options.get("session") else ()
    outputs = {}
    for key in wanted:
        if key.startswith("runs"):
            extra = (("--json",) if key.endswith("json") else ()) + (
                runs_argv if "session" in key else ()
            )
            outputs[key] = _runs(tmp_path, root, *extra)
        else:
            outputs[key] = _steps(tmp_path, root, *(("--json",) if key.endswith("json") else ()))
    return outputs


# ── Reading what they printed ──────────────────────────────────────────────


def _lines(done) -> list[str]:
    return done.stdout.splitlines()


def _coverage_line(done) -> str:
    return next(line for line in _lines(done) if line.startswith("Coverage:"))


def _steps_coverage_line(done) -> str:
    """The coverage line of `steps`' first run section, without its indent."""
    return next(line.strip() for line in _lines(done) if line.strip().startswith("Coverage:"))


def _document(done) -> dict:
    assert done.returncode == 0 and done.stderr == "", done.stderr
    return json.loads(done.stdout)


def _without_reasons(text: str) -> str:
    """Text as `1130e57` printed it: with no reason after an unreadable id."""
    for reason in (THIRD, NOT_OPENED, GONE):
        text = text.replace(f" ({reason})", "")
    return text


def _document_at_base(done) -> dict:
    """A `runs --json` document with the member this change adds taken out."""
    document = _document(done)
    document["coverage"].pop("unreadable_reasons", None)
    return document


def _is_at_base(done, key, name):
    """`done` is what `1130e57` printed for `key` on the fixture `name`, apart from the reasons."""
    expected = BASELINE[name][key]
    assert (done.returncode, done.stderr) == (expected["returncode"], expected["stderr"])
    if key.endswith("json"):
        assert _document_at_base(done) == json.loads(expected["stdout"]), (key, name)
    else:
        assert _without_reasons(done.stdout) == expected["stdout"], (key, name)


def _exactly_baseline(done, key, name):
    expected = BASELINE[name][key]
    assert (done.returncode, done.stdout, done.stderr) == (
        expected["returncode"],
        expected["stdout"],
        expected["stderr"],
    ), (key, name)


def _section(done, wf_id: str) -> list[str]:
    """The lines of `steps` text from a workflow run's header to the blank line after it, or to
    the note block when the section is the report's last."""
    lines = _lines(done)
    start = next(n for n, line in enumerate(lines) if line.startswith(f"{wf_id} "))
    end = next(
        (n for n in range(start, len(lines)) if lines[n] in ("", NO_COST)), len(lines)
    )
    return lines[start:end]


def _unreadable_entries(done) -> list[str]:
    _, _, tail = _coverage_line(done).partition(" — unreadable: ")
    return tail.split(", ")


def _row(done, session_id: str) -> list[str]:
    lines = _lines(done)
    start = next(n for n, line in enumerate(lines) if line.startswith(f"{session_id} "))
    end = next((n for n in range(start, len(lines)) if lines[n] == ""), len(lines))
    return lines[start:end]


def _directory_objects(*paths: Path) -> list[dict]:
    return [{"directory": str(path), "reason": LOOKED_IN} for path in paths]


# ── E1 — clean input ───────────────────────────────────────────────────────


def test_clean_root_both_commands_text_and_json_are_byte_identical_to_baseline(tmp_path):
    root = _tree(tmp_path)
    for key, done in _by_name(tmp_path, root, "clean", session=True).items():
        _exactly_baseline(done, key, "clean")

    stored_steps = _without_spans((PARITY / "expected" / "steps.json").read_text(encoding="utf-8"))
    assert _steps(tmp_path, root, "--json").stdout == stored_steps
    document = _document(_runs(tmp_path, root, "--json"))
    assert (document["coverage"]["read"], document["coverage"]["unreadable"]) == (3, 0)
    assert "unreadable_reasons" not in document["coverage"]
    assert "unreadable_directories" not in document["coverage"]
    assert "unreadable_directories" not in _document(_steps(tmp_path, root, "--json"))


# ── E2 — gone is not unopenable ────────────────────────────────────────────


def test_gone_and_unopenable_transcript_differ_by_reason_and_only_by_reason(tmp_path, seal):
    gone_root, locked_root = _tree(tmp_path, "gone"), _tree(tmp_path, "locked")
    (gone_root / KENSAI / f"{RUN_SESSION}.jsonl").unlink()
    seal(locked_root / KENSAI / f"{RUN_SESSION}.jsonl")

    outputs = {
        "gone": (_by_name(tmp_path, gone_root, "gone", session=True), GONE),
        "unopenable": (_by_name(tmp_path, locked_root, "unopenable", session=True), NOT_OPENED),
    }
    for name, (done_by_key, reason) in outputs.items():
        for key, done in done_by_key.items():
            if key.startswith("steps"):
                _exactly_baseline(done, key, name)
                _exactly_baseline(done, key, "clean")
                continue
            _is_at_base(done, key, name)
            if key.endswith("json"):
                coverage = _document(done)["coverage"]
                assert coverage["unreadable_ids"] == [RUN_SESSION]
                assert coverage["unreadable_reasons"] == [reason]
                assert "unreadable_directories" not in coverage
            else:
                assert (
                    _coverage_line(done)
                    == f"Coverage: 2 read / 1 unreadable / 3 total (3 listed, 0 named)"
                    f" — unreadable: {RUN_SESSION} ({reason})"
                ), key
                assert LOOKED_IN not in done.stdout

    assert _coverage_line(outputs["gone"][0]["runs"]) != _coverage_line(
        outputs["unopenable"][0]["runs"]
    )


# ── E3 — a root that cannot be looked in ───────────────────────────────────


def test_locked_root_gives_every_run_the_third_reason_and_names_only_the_root(tmp_path, seal):
    root = _tree(tmp_path)
    seal(root)
    line = DIRECTORY_LINE.format(directory=root)

    text = _runs(tmp_path, root)
    document = _document(_runs(tmp_path, root, "--json"))
    steps_text = _steps(tmp_path, root)
    steps_document = _document(_steps(tmp_path, root, "--json"))

    assert text.returncode == 0 and text.stderr == ""
    assert _coverage_line(text).startswith("Coverage: 0 read / 3 unreadable / 3 total")
    assert _unreadable_entries(text) == [
        f"{session} ({THIRD})"
        for session in (RUN_SESSION, OTHER_RUN_SESSION, NOZOKI_RUN_SESSION)
    ]
    assert _lines(text).count(line) == 1 and NO_RUNS not in text.stdout
    assert _lines(text)[-2:] == [line, NO_COST]
    assert document["rows"] == []
    assert document["coverage"]["unreadable_reasons"] == [THIRD] * 3
    assert document["coverage"]["unreadable_directories"] == _directory_objects(root)

    assert steps_text.returncode == 0 and steps_text.stderr == ""
    assert _lines(steps_text).count(line) == 1 and NO_WORKFLOWS not in steps_text.stdout
    assert steps_document["runs"] == []
    assert steps_document["unreadable_directories"] == _directory_objects(root)


# ── E4 — only one project directory locked ─────────────────────────────────


def test_only_one_locked_project_dir_keeps_the_readable_row_and_names_the_dir_once(
    tmp_path, seal
):
    root = _tree(tmp_path)
    locked = seal(root / KENSAI)
    line = DIRECTORY_LINE.format(directory=locked)

    text = _runs(tmp_path, root)
    document = _document(_runs(tmp_path, root, "--json"))
    steps_text = _steps(tmp_path, root)
    steps_document = _document(_steps(tmp_path, root, "--json"))
    missing = _steps(tmp_path, root, "--wf", WORKFLOW)

    assert _coverage_line(text).startswith("Coverage: 1 read / 2 unreadable / 3 total")
    assert _unreadable_entries(text) == [
        f"{RUN_SESSION} ({THIRD})",
        f"{OTHER_RUN_SESSION} ({THIRD})",
    ]
    clean_runs = BASELINE["clean"]["runs"]["stdout"].splitlines()
    first = next(n for n, row in enumerate(clean_runs) if row.startswith(NOZOKI_RUN_SESSION))
    assert _row(text, NOZOKI_RUN_SESSION) == clean_runs[first : first + 3]
    assert _lines(text).count(line) == 1 and _lines(text)[-2:] == [line, NO_COST]
    assert [row["session_id"] for row in document["rows"]] == [NOZOKI_RUN_SESSION]
    assert document["coverage"]["unreadable_reasons"] == [THIRD, THIRD]
    assert document["coverage"]["unreadable_directories"] == _directory_objects(locked)

    assert _lines(steps_text).count(line) == 1 and NO_WORKFLOWS not in steps_text.stdout
    assert steps_document["runs"] == []
    assert steps_document["unreadable_directories"] == _directory_objects(locked)
    assert (missing.returncode, missing.stdout) == (1, "")
    assert WF_MISSING_ERROR.match(missing.stderr)


# ── E5 / E6 — locked project directories beside a readable one ─────────────


def _tree_with_a_third_project(tmp_path: Path, name: str) -> Path:
    """`R` and a third, readable project directory holding a copy of the workflow run, its
    responses given ids of their own so the copy shares none with the run it was made from."""
    root = _tree(tmp_path, name)
    copy = root / "-home-user-code-copy" / "copy-session" / "subagents" / "workflows"
    shutil.copytree(root / WORKFLOW_DIR, copy / "wf_copy")
    for transcript in (copy / "wf_copy").glob("agent-*.jsonl"):
        text = transcript.read_text(encoding="utf-8")
        transcript.write_text(text.replace('"id":"msg_', '"id":"copy_msg_'), encoding="utf-8")
    return root


def test_two_locked_project_dirs_are_named_in_ascending_order_beside_a_readable_one(
    tmp_path, seal
):
    plain = _tree_with_a_third_project(tmp_path, "plain")
    root = _tree_with_a_third_project(tmp_path, "locked")
    first, second = seal(root / NOZOKI), seal(root / KENSAI)
    lines = [DIRECTORY_LINE.format(directory=place) for place in (first, second)]

    text = _steps(tmp_path, root)
    document = _document(_steps(tmp_path, root, "--json"))
    unlocked = _steps(tmp_path, plain)
    unlocked_document = _document(_steps(tmp_path, plain, "--json"))

    assert text.returncode == 0 and text.stderr == ""
    assert _lines(text)[-3:] == [*lines, NO_COST]
    assert _lines(text)[-4] == ""
    assert NO_WORKFLOWS not in text.stdout
    assert _section(text, "wf_copy") == _section(unlocked, "wf_copy")
    assert WORKFLOW not in text.stdout
    assert document["unreadable_directories"] == _directory_objects(first, second)
    assert [run["wf_id"] for run in document["runs"]] == ["wf_copy"]
    assert document["runs"][0] == next(
        run for run in unlocked_document["runs"] if run["wf_id"] == "wf_copy"
    )


def test_successful_wf_beside_a_locked_project_dir_names_it_outside_the_section_and_exits_zero(
    tmp_path, seal
):
    plain = _tree(tmp_path, "plain")
    root = _tree(tmp_path, "locked")
    locked = seal(root / NOZOKI)
    line = DIRECTORY_LINE.format(directory=locked)

    text = _steps(tmp_path, root, "--wf", WORKFLOW)
    unlocked = _steps(tmp_path, plain, "--wf", WORKFLOW)
    document = _document(_steps(tmp_path, root, "--wf", WORKFLOW, "--json"))

    assert text.returncode == 0 and text.stderr == ""
    assert _section(text, WORKFLOW) == _section(unlocked, WORKFLOW)
    assert line not in _section(text, WORKFLOW)
    assert _lines(text).count(line) == 1 and _lines(text)[-2:] == [line, NO_COST]
    assert document["unreadable_directories"] == _directory_objects(locked)
    assert [run["wf_id"] for run in document["runs"]] == [WORKFLOW]


# ── E7 — absent, empty and not-a-place roots keep their baseline ───────────


def test_missing_root_root_under_a_file_and_empty_root_keep_the_baseline_empty_states(
    tmp_path, seal
):
    (tmp_path / "plain").write_text("", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    roots = {
        "absent_root": tmp_path / "absent",
        "root_under_a_file": tmp_path / "plain" / "below",
        "empty_root": tmp_path / "empty",
    }
    for name, root in roots.items():
        for key, done in _by_name(tmp_path, root, name).items():
            if key.startswith("steps"):
                _exactly_baseline(done, key, name)
                continue
            _is_at_base(done, key, name)
            if key.endswith("json"):
                coverage = _document(done)["coverage"]
                assert coverage["unreadable_reasons"] == [GONE] * 3
                assert "unreadable_directories" not in coverage
            else:
                assert _unreadable_entries(done) == [
                    f"{session} ({GONE})"
                    for session in (RUN_SESSION, OTHER_RUN_SESSION, NOZOKI_RUN_SESSION)
                ]
                assert NO_RUNS in done.stdout and LOOKED_IN not in done.stdout

    root = _tree(tmp_path)
    (root / "notes.txt").write_text("", encoding="utf-8")
    seal(root / "notes.txt")
    for key, done in _by_name(tmp_path, root, "file_under_root").items():
        _exactly_baseline(done, key, "file_under_root")


# ── E8 — a lock below a project directory is not a place ──────────────────

BELOW_A_PROJECT_DIRECTORY = {
    "session_locked": f"{KENSAI}/{FLOW_SESSION}",
    "subagents_locked": f"{KENSAI}/{FLOW_SESSION}/subagents",
    "workflows_locked": f"{KENSAI}/{FLOW_SESSION}/subagents/workflows",
    "workflow_dir_locked": WORKFLOW_DIR,
    "journal_locked": f"{WORKFLOW_DIR}/journal.jsonl",
    "agent_transcript_locked": f"{WORKFLOW_DIR}/agent-ab58aa559157db106.jsonl",
    "meta_locked": f"{WORKFLOW_DIR}/agent-ab58aa559157db106.meta.json",
}


LOCKED_FILES = ("journal_locked", "agent_transcript_locked", "meta_locked")
LOCKED_DIRECTORIES = (
    "session_locked",
    "subagents_locked",
    "workflows_locked",
    "workflow_dir_locked",
)


@pytest.mark.parametrize("name", sorted(LOCKED_FILES))
def test_locked_files_below_a_project_dir_are_not_places(name, tmp_path, seal):
    root = _tree(tmp_path)
    seal(root / BELOW_A_PROJECT_DIRECTORY[name])

    for key, done in _by_name(tmp_path, root, name).items():
        _exactly_baseline(done, key, name)
        assert LOOKED_IN not in done.stdout


@pytest.mark.parametrize("name", sorted(LOCKED_DIRECTORIES))
def test_steps_names_a_locked_directory_below_a_project_dir(name, tmp_path, seal):
    root = _tree(tmp_path)
    locked = seal(root / BELOW_A_PROJECT_DIRECTORY[name])
    line = DIRECTORY_LINE.format(directory=locked)

    for key, done in _by_name(tmp_path, root, name).items():
        if key.startswith("runs"):
            _exactly_baseline(done, key, name)
            assert LOOKED_IN not in done.stdout
    text = _steps(tmp_path, root)
    document = _document(_steps(tmp_path, root, "--json"))

    assert (text.returncode, text.stderr) == (0, "")
    lines = _lines(text)
    assert lines[0].startswith("Fallback:") and lines[1] == ""
    assert lines[2:] == [line, NO_COST]
    assert NO_WORKFLOWS not in text.stdout
    assert document["runs"] == []
    assert document["unreadable_directories"] == _directory_objects(locked)


def test_steps_names_a_locked_place_beside_a_found_run(tmp_path, seal):
    plain = _tree(tmp_path, "plain")
    root = _tree(tmp_path, "locked")
    project = root / KENSAI
    places = []
    for name in ("S2", "a", "b", "memory"):
        (project / name).mkdir()
        places.append(seal(project / name))
    lines = [DIRECTORY_LINE.format(directory=place) for place in places]

    unlocked = _steps(tmp_path, plain)
    unlocked_document = _document(_steps(tmp_path, plain, "--json"))
    for extra in ((), ("--wf", WORKFLOW)):
        text = _steps(tmp_path, root, *extra)
        document = _document(_steps(tmp_path, root, *extra, "--json"))

        assert (text.returncode, text.stderr) == (0, "")
        assert _section(text, WORKFLOW) == _section(unlocked, WORKFLOW)
        assert not [line for line in _section(text, WORKFLOW) if LOOKED_IN in line]
        assert _lines(text)[-(len(lines) + 1) :] == [*lines, NO_COST]
        assert _lines(text)[-(len(lines) + 2)] == ""
        assert document["unreadable_directories"] == _directory_objects(*places)
        assert document["runs"] == unlocked_document["runs"]


def test_steps_with_execute_only_directories_prints_as_baseline(tmp_path, with_mode):
    for case in ("session", "subagents", "workflow_dir"):
        root = _tree(tmp_path, case)
        with_mode(
            root
            / {
                "session": f"{KENSAI}/{FLOW_SESSION}",
                "subagents": f"{KENSAI}/{FLOW_SESSION}/subagents",
                "workflow_dir": WORKFLOW_DIR,
            }[case],
            0o111,
        )
        for key, done in _by_name(tmp_path, root, "clean").items():
            if key.startswith("steps"):
                assert LOOKED_IN not in done.stdout
                if case == "workflow_dir":
                    _is_baseline_but_for_the_uncounted_run(done, key, root / WORKFLOW_DIR)
                else:
                    _exactly_baseline(done, key, "clean")


@pytest.mark.parametrize(
    ("directory", "mode"),
    [
        (f"{KENSAI}/{FLOW_SESSION}/subagents/workflows", 0o111),
        (f"{KENSAI}/{FLOW_SESSION}", 0o444),
        (f"{KENSAI}/{FLOW_SESSION}/subagents/workflows", 0o444),
        (WORKFLOW_DIR, 0o444),
    ],
)
def test_steps_names_a_directory_that_cannot_be_entered(directory, mode, tmp_path, with_mode):
    root = _tree(tmp_path)
    locked = with_mode(root / directory, mode)

    text = _steps(tmp_path, root)
    document = _document(_steps(tmp_path, root, "--json"))

    assert (text.returncode, text.stderr) == (0, "")
    lines = _lines(text)
    assert lines[0].startswith("Fallback:") and lines[1] == ""
    assert lines[2:] == [DIRECTORY_LINE.format(directory=locked), NO_COST]
    assert NO_WORKFLOWS not in text.stdout
    assert document["runs"] == []
    assert document["unreadable_directories"] == _directory_objects(locked)


# ── A run directory that can be entered but not listed ────────────────────

def _uncounted_text(baseline_stdout: str, directory: Path, *more_lines: str) -> str:
    """What `steps` prints for a run it could not list: the readable text with the fourth figure
    left out of the coverage line and the uncounted line (then any `more_lines`) before the
    no-cost line."""
    text = FOURTH_FIGURE.sub("", baseline_stdout)
    block = "\n".join([UNCOUNTED_LINE.format(directory=directory, reason=DENIED), *more_lines])
    return text.replace(NO_COST, f"\n{block}\n{NO_COST}")


def _uncounted_document(baseline_stdout: str, *directories: Path) -> dict:
    document = json.loads(baseline_stdout)
    for run in document["runs"]:
        run["coverage"]["files_with_no_journal_entry"] = None
    document["unreadable_directories"] = [
        {"directory": str(d), "reason": DENIED} for d in directories
    ]
    return document


def _is_baseline_but_for_the_uncounted_run(done, key, run_dir):
    assert (done.returncode, done.stderr) == (0, "")
    baseline = BASELINE["clean"][key]["stdout"]
    if key.endswith("json"):
        assert json.loads(done.stdout) == _uncounted_document(baseline, run_dir)
    else:
        assert done.stdout == _uncounted_text(baseline, run_dir)


def _orphan_tree(tmp_path, name="R2"):
    """`R2`: the reference projects with an agent file added to the workflow run `W` that its
    journal never names."""
    root = _tree(tmp_path, name)
    shutil.copy(root / WORKFLOW_DIR / ORPHAN_SOURCE, root / WORKFLOW_DIR / ORPHAN_FILE)
    return root


def _readable_baseline(key: str, orphans: int) -> str:
    stdout = BASELINE["clean"][key]["stdout"]
    if key.endswith("json"):
        document = json.loads(stdout)
        document["runs"][0]["coverage"]["files_with_no_journal_entry"] = orphans
        return json.dumps(document, indent=2) + "\n"
    return stdout.replace(
        " / 0 agent files with no journal entry at all",
        f" / {orphans} agent files with no journal entry at all",
    )


def _text_and_json(tmp_path, root, *extra):
    return _steps(tmp_path, root, *extra), _steps(tmp_path, root, *extra, "--json")


def _assert_unlistable_report(text, as_json, directory, readable_json):
    """The report of a run `steps` read but could not list: three figures, one uncounted line
    before the no-cost line, and `null` in `--json`."""
    assert (text.returncode, text.stderr) == (0, "")
    assert _steps_coverage_line(text).endswith("0 started with no result")
    assert FOURTH_FIGURE.search(text.stdout) is None
    assert "agent files with no journal entry at all" not in text.stdout.replace(
        UNCOUNTED_LINE.format(directory=directory, reason=DENIED), ""
    )
    line = UNCOUNTED_LINE.format(directory=directory, reason=DENIED)
    lines = _lines(text)
    assert lines.count(line) == 1 and LOOKED_IN not in line
    assert lines[-3:] == ["", line, NO_COST]
    document = _document(as_json)
    run = document["runs"][0]
    assert run["coverage"]["files_with_no_journal_entry"] is None
    assert run["rows"] == readable_json["runs"][0]["rows"]
    assert {k: v for k, v in run["coverage"].items() if k != "files_with_no_journal_entry"} == {
        k: v
        for k, v in readable_json["runs"][0]["coverage"].items()
        if k != "files_with_no_journal_entry"
    }
    assert document["unreadable_directories"] == [{"directory": str(directory), "reason": DENIED}]


def test_steps_orphan_copy_with_unlistable_run_dir_omits_fourth_figure_and_names_it(
    tmp_path, with_mode
):
    root = _orphan_tree(tmp_path)
    readable = _document(_steps(tmp_path, root, "--json"))
    readable_text = _steps(tmp_path, root)
    assert _steps_coverage_line(readable_text).endswith(
        "0 started with no result / 1 agent files with no journal entry at all"
    )
    with_mode(root / WORKFLOW_DIR, 0o111)

    text, as_json = _text_and_json(tmp_path, root)

    _assert_unlistable_report(text, as_json, root / WORKFLOW_DIR, readable)
    assert _steps_coverage_line(text) == (
        "Coverage: 9 with a step / 1 without / 0 started with no result"
    )
    assert text.stdout == _uncounted_text(readable_text.stdout, root / WORKFLOW_DIR)


def test_steps_json_orphan_copy_unlistable_run_dir_has_null_and_reason(tmp_path, with_mode):
    root = _orphan_tree(tmp_path)
    readable = _document(_steps(tmp_path, root, "--json"))
    with_mode(root / WORKFLOW_DIR, 0o111)

    done = _steps(tmp_path, root, "--json")

    document = _document(done)
    assert set(document) == {"fallback", "runs", "unreadable_directories"}
    assert document["runs"][0]["coverage"]["files_with_no_journal_entry"] is None
    assert document["runs"][0]["rows"] == readable["runs"][0]["rows"]
    assert document["unreadable_directories"] == [
        {"directory": str(root / WORKFLOW_DIR), "reason": "Permission denied"}
    ]


def test_steps_wf_forms_with_unlistable_run_dir_match_full_forms(tmp_path, with_mode):
    root = _orphan_tree(tmp_path)
    readable = _document(_steps(tmp_path, root, "--json"))
    with_mode(root / WORKFLOW_DIR, 0o111)

    text, as_json = _text_and_json(tmp_path, root, "--wf", WORKFLOW)

    _assert_unlistable_report(text, as_json, root / WORKFLOW_DIR, readable)
    assert text.stdout == _steps(tmp_path, root).stdout


def test_steps_unmodified_parity_with_unlistable_run_dir_does_not_print_zero(
    tmp_path, with_mode
):
    root = _tree(tmp_path)
    readable = _document(_steps(tmp_path, root, "--json"))
    with_mode(root / WORKFLOW_DIR, 0o111)

    text, as_json = _text_and_json(tmp_path, root)

    _assert_unlistable_report(text, as_json, root / WORKFLOW_DIR, readable)
    assert "0 agent files" not in text.stdout


def test_steps_readable_input_is_byte_identical_to_base(tmp_path):
    stored = _without_spans((PARITY / "expected" / "steps.json").read_text(encoding="utf-8"))
    for name, root, orphans in (("R", _tree(tmp_path), 0), ("R2", _orphan_tree(tmp_path), 1)):
        for form in FORMS:
            done = _steps(tmp_path, root, *form)
            key = "steps_json" if "--json" in form else "steps"
            assert (done.returncode, done.stderr) == (0, ""), (name, form)
            assert done.stdout == _readable_baseline(key, orphans), (name, form)
            assert "unreadable_directories" not in done.stdout
            assert "could not be listed" not in done.stdout
        if orphans == 0:
            assert _steps(tmp_path, root, "--json").stdout == stored
    assert _steps_coverage_line(_steps(tmp_path, root)).endswith(
        "/ 1 agent files with no journal entry at all"
    )


def test_steps_uncounted_line_precedes_unchanged_directory_line(tmp_path, with_mode, seal):
    root = _tree(tmp_path)
    readable = _document(_steps(tmp_path, root, "--json"))
    with_mode(root / WORKFLOW_DIR, 0o111)
    locked = seal(_mkdir(root / KENSAI / "a"))
    line = DIRECTORY_LINE.format(directory=locked)
    uncounted = UNCOUNTED_LINE.format(directory=root / WORKFLOW_DIR, reason=DENIED)

    text, as_json = _text_and_json(tmp_path, root)

    lines = _lines(text)
    assert lines[-4:] == ["", uncounted, line, NO_COST]
    assert LOOKED_IN not in uncounted
    document = _document(as_json)
    assert document["unreadable_directories"] == [
        {"directory": str(root / WORKFLOW_DIR), "reason": DENIED},
        {"directory": str(locked), "reason": LOOKED_IN},
    ]
    assert document["runs"][0]["rows"] == readable["runs"][0]["rows"]


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


def test_steps_unreadable_journal_keeps_base_output(tmp_path, with_mode, seal):
    root = _orphan_tree(tmp_path)
    with_mode(root / WORKFLOW_DIR, 0o111)
    seal(root / WORKFLOW_DIR / "journal.jsonl")

    text, as_json = _text_and_json(tmp_path, root)

    assert _steps_coverage_line(text) == (
        "Coverage: 0 with a step / 0 without / 0 started with no result / "
        "0 agent files with no journal entry at all — 1 unreadable journal file, so no agent "
        "file in this workflow run is read or counted"
    )
    assert "could not be listed" not in text.stdout
    document = _document(as_json)
    assert document["runs"][0]["coverage"]["files_with_no_journal_entry"] == 0
    assert "unreadable_directories" not in document


def test_steps_unknown_wf_with_unlistable_run_dir_keeps_base_error(tmp_path, with_mode):
    root = _orphan_tree(tmp_path)
    with_mode(root / WORKFLOW_DIR, 0o111)
    expected = (
        f"error: no workflow run found for --wf wf_doesnotexist under {root} — run "
        "`ka-telemetry steps` without --wf to see the workflow runs found there\n"
    )

    for extra in ((), ("--json",)):
        done = _steps(tmp_path, root, "--wf", "wf_doesnotexist", *extra)

        assert (done.returncode, done.stdout, done.stderr) == (1, "", expected)


def _two_run_tree(tmp_path) -> Path:
    root = _tree(tmp_path)
    second = root / KENSAI / SECOND_SESSION
    shutil.copytree(root / KENSAI / FLOW_SESSION, second)
    (second / "subagents" / "workflows" / WORKFLOW).rename(root / SECOND_WORKFLOW_DIR)
    return root


def test_steps_two_runs_one_unlistable_wf_selects_only_the_shown_run(tmp_path, with_mode):
    root = _two_run_tree(tmp_path)
    first, second = root / WORKFLOW_DIR, root / SECOND_WORKFLOW_DIR
    with_mode(first, 0o111)

    shown = _steps(tmp_path, root, "--wf", SECOND_WORKFLOW)
    shown_json = _document(_steps(tmp_path, root, "--wf", SECOND_WORKFLOW, "--json"))
    failed = _steps(tmp_path, root, "--wf", WORKFLOW)
    failed_json = _document(_steps(tmp_path, root, "--wf", WORKFLOW, "--json"))

    assert _steps_coverage_line(shown).endswith("agent files with no journal entry at all")
    assert "could not be listed" not in shown.stdout and str(first) not in shown.stdout
    assert "unreadable_directories" not in shown_json
    assert shown_json["runs"][0]["coverage"]["files_with_no_journal_entry"] is not None
    assert failed.stdout.count("could not be listed") == 1 and str(second) not in failed.stdout
    assert failed_json["unreadable_directories"] == [{"directory": str(first), "reason": DENIED}]

    with_mode(second, 0o111)
    both = _steps(tmp_path, root)
    both_json = _document(_steps(tmp_path, root, "--json"))

    assert FOURTH_FIGURE.search(both.stdout) is None
    lines = _lines(both)
    assert lines[-4:] == [
        "",
        UNCOUNTED_LINE.format(directory=first, reason=DENIED),
        UNCOUNTED_LINE.format(directory=second, reason=DENIED),
        NO_COST,
    ]
    assert both_json["unreadable_directories"] == [
        {"directory": str(first), "reason": DENIED},
        {"directory": str(second), "reason": DENIED},
    ]
    assert [r["coverage"]["files_with_no_journal_entry"] for r in both_json["runs"]] == [None, None]


@pytest.mark.parametrize(
    ("directory", "mode"),
    [
        (WORKFLOW_DIR, 0o000),
        (WORKFLOW_DIR, 0o444),
        (f"{KENSAI}/{FLOW_SESSION}/subagents/workflows", 0o000),
        (f"{KENSAI}/{FLOW_SESSION}", 0o000),
    ],
)
def test_steps_mode_000_0444_and_locked_ancestors_stay_directory_lines(
    directory, mode, tmp_path, with_mode
):
    root = _orphan_tree(tmp_path)
    locked = with_mode(root / directory, mode)

    text, as_json = _text_and_json(tmp_path, root)

    assert (text.returncode, text.stderr) == (0, "")
    assert _lines(text)[-2:] == [DIRECTORY_LINE.format(directory=locked), NO_COST]
    assert "could not be listed" not in text.stdout
    assert _document(as_json)["unreadable_directories"] == _directory_objects(locked)


def test_runs_on_orphan_copy_with_unlistable_run_dir_matches_baseline(tmp_path, with_mode):
    root = _orphan_tree(tmp_path)
    before = [_runs(tmp_path, root, *extra) for extra in ((), ("--json",))]
    with_mode(root / WORKFLOW_DIR, 0o111)

    after = [_runs(tmp_path, root, *extra) for extra in ((), ("--json",))]

    for done_before, done_after in zip(before, after, strict=True):
        assert (done_after.returncode, done_after.stdout, done_after.stderr) == (
            done_before.returncode,
            done_before.stdout,
            done_before.stderr,
        )


# ── E8b — `steps --wf` that finds nothing says where it did not look ────────

WF_PLACE_LINE = (
    "{directory} could not be looked in, so this command did not look for a workflow run there. "
    "Run this command again once it can be looked in."
)


def _not_found_line(root: Path, wf_id: str) -> str:
    return (
        f"error: no workflow run found for --wf {wf_id} under {root} "
        "— run `ka-telemetry steps` without --wf to see the workflow runs found there"
    )


@pytest.mark.parametrize("wf_id", [WORKFLOW, "wf_doesnotexist"])
@pytest.mark.parametrize("extra", [(), ("--json",)])
@pytest.mark.parametrize("place", ["", KENSAI, f"{KENSAI}/{FLOW_SESSION}", WORKFLOW_DIR])
def test_steps_wf_not_found_names_the_place_that_could_not_be_looked_in(
    place, extra, wf_id, tmp_path, seal
):
    root = _tree(tmp_path)
    locked = seal(root / place if place else root)

    done = _steps(tmp_path, root, "--wf", wf_id, *extra)

    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.splitlines() == [
        _not_found_line(root, wf_id),
        WF_PLACE_LINE.format(directory=locked),
    ]


def test_steps_wf_not_found_names_several_places_in_order_once_each(tmp_path, seal):
    root = _tree(tmp_path)
    project = root / KENSAI
    locked = [seal(project / FLOW_SESSION / "subagents")]
    for name in ("b", "a"):
        (project / name).mkdir()
        locked.append(seal(project / name))

    done = _steps(tmp_path, root, "--wf", "wf_doesnotexist")

    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.splitlines()[1:] == [
        WF_PLACE_LINE.format(directory=place) for place in (locked[0], locked[2], locked[1])
    ]


@pytest.mark.parametrize("extra", [(), ("--json",)])
def test_steps_wf_not_found_on_clean_input_is_the_baseline_line_only(extra, tmp_path):
    root = _tree(tmp_path)

    done = _steps(tmp_path, root, "--wf", "wf_doesnotexist", *extra)

    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == _not_found_line(root, "wf_doesnotexist") + "\n"


# ── E8c — what the places change leaves alone ──────────────────────────────

CLEAN_STEPS_FORMS = {
    "steps": (),
    "steps_json": ("--json",),
    "steps_wf": ("--wf", WORKFLOW),
    "steps_wf_json": ("--wf", WORKFLOW, "--json"),
    "steps_wf_missing": ("--wf", "wf_doesnotexist"),
    "steps_wf_missing_json": ("--wf", "wf_doesnotexist", "--json"),
}


def _is_places_baseline(done, expected, root):
    assert (done.returncode, done.stdout, done.stderr) == (
        expected["returncode"],
        expected["stdout"].replace("{root}", str(root)),
        expected["stderr"].replace("{root}", str(root)),
    )


def test_clean_steps_forms_are_byte_identical_to_baseline(tmp_path):
    root = _tree(tmp_path)
    for key, extra in CLEAN_STEPS_FORMS.items():
        done = _steps(tmp_path, root, *extra)

        _is_places_baseline(done, PLACES_BASELINE[key], root)
        assert LOOKED_IN not in done.stdout + done.stderr
        assert "unreadable_directories" not in done.stdout


def _below_a_project_directory_fixtures(tmp_path, seal):
    """Each directory a lock now names, one fixture apiece: its name, root, and the place."""
    fixtures = {}
    for name in sorted(LOCKED_DIRECTORIES):
        root = _tree(tmp_path, name)
        fixtures[name] = (root, seal(root / BELOW_A_PROJECT_DIRECTORY[name]))
    return fixtures


def test_json_member_is_exact_where_present_and_one_document_with_empty_stderr(tmp_path, seal):
    for name, (root, locked) in _below_a_project_directory_fixtures(tmp_path, seal).items():
        done = _steps(tmp_path, root, "--json")

        document = _document(done)
        assert document["unreadable_directories"] == _directory_objects(locked), name
        members = document["unreadable_directories"]
        assert all(set(member) == {"directory", "reason"} for member in members)
        assert list(document)[:2] == ["fallback", "runs"], name


def test_every_run_and_step_figure_matches_baseline_beside_a_failed_place(tmp_path, seal):
    root = _tree(tmp_path)
    project = root / KENSAI
    (project / "a").mkdir()
    seal(project / "a")
    clean_runs = json.loads(PLACES_BASELINE["steps_json"]["stdout"])["runs"]

    for extra in ((), ("--wf", WORKFLOW)):
        document = _document(_steps(tmp_path, root, *extra, "--json"))

        assert document["runs"] == clean_runs
        assert len(document["unreadable_directories"]) == 1


def test_runs_is_byte_identical_on_every_fixture_above(tmp_path, seal, with_mode):
    mixed = _tree(tmp_path, "mixed")
    for name in ("S2", "a", "b", "memory"):
        (mixed / KENSAI / name).mkdir()
        seal(mixed / KENSAI / name)
    roots = [mixed]
    for mode, directory in (
        (0o111, f"{KENSAI}/{FLOW_SESSION}"),
        (0o111, f"{KENSAI}/{FLOW_SESSION}/subagents"),
        (0o111, WORKFLOW_DIR),
        (0o444, f"{KENSAI}/{FLOW_SESSION}/subagents/workflows"),
    ):
        root = _tree(tmp_path, f"mode-{len(roots)}")
        with_mode(root / directory, mode)
        roots.append(root)

    for root in roots:
        _exactly_baseline(_runs(tmp_path, root), "runs", "clean")
        _exactly_baseline(_runs(tmp_path, root, "--json"), "runs_json", "clean")


def test_directory_lines_come_before_the_no_cost_line_and_pointers_after(tmp_path, seal):
    root = _tree(tmp_path)
    (root / KENSAI / "memory").mkdir()
    locked = seal(root / KENSAI / "memory")
    home = _stream_home(tmp_path, FLOW_SESSION)

    lines = _lines(_steps(tmp_path, root, home=home))

    header = next(line for line in lines if line.startswith(WORKFLOW))
    section_end = lines.index("", lines.index(header))
    directory_line = lines.index(DIRECTORY_LINE.format(directory=locked))
    no_cost = lines.index(NO_COST)
    assert section_end < directory_line < no_cost < len(lines) - 1
    assert all(POINTER in line for line in lines[no_cost + 1 :])
    assert FLOW_SESSION in lines[-1]


def test_other_commands_and_every_help_text_are_unchanged(tmp_path):
    home = _home(tmp_path)
    for name, expected in PLACES_BASELINE["help"].items():
        argv = ("--help",) if name == "(top)" else (name, "--help")

        done = _ka(tmp_path, home, *argv)

        assert (done.returncode, done.stdout, done.stderr) == (
            expected["returncode"],
            expected["stdout"],
            expected["stderr"],
        ), name


# ── E9 — every fixture is one clean report ─────────────────────────────────


def _fixtures(tmp_path, seal):
    """Each fixture of the station once: its name and root."""
    gone = _tree(tmp_path, "gone")
    (gone / KENSAI / f"{RUN_SESSION}.jsonl").unlink()
    unopenable = _tree(tmp_path, "unopenable")
    seal(unopenable / KENSAI / f"{RUN_SESSION}.jsonl")
    root_locked = _tree(tmp_path, "root-locked")
    seal(root_locked)
    one_locked = _tree(tmp_path, "one-locked")
    seal(one_locked / KENSAI)
    return {
        "gone": gone,
        "unopenable": unopenable,
        "root-locked": root_locked,
        "one-locked": one_locked,
    }


def test_every_station_fixture_is_one_clean_report_with_the_cost_line_then_pointers(
    tmp_path, seal
):
    """Exit `0`, empty stderr, one JSON document, and the cost line followed only by pointers."""
    home = _stream_home(tmp_path, RUN_SESSION, OTHER_RUN_SESSION, FLOW_SESSION)
    for name, root in _fixtures(tmp_path, seal).items():
        for command in (_runs, _steps):
            text = command(tmp_path, root, home=home)
            assert (text.returncode, text.stderr) == (0, ""), (name, command.__name__)
            lines = _lines(text)
            assert lines.count(NO_COST) == 1, (name, command.__name__)
            after = lines[lines.index(NO_COST) + 1 :]
            assert all(POINTER in line for line in after), (name, command.__name__)
            document = _document(command(tmp_path, root, "--json", home=home))
            assert isinstance(document, dict) and "cost" not in json.dumps(document)

    gone = _tree(tmp_path, "pointers")
    (gone / KENSAI / f"{RUN_SESSION}.jsonl").unlink()
    runs = _runs(tmp_path, gone, home=home)
    pointers = [line for line in _lines(runs) if POINTER in line]
    assert len(pointers) == 1 and OTHER_RUN_SESSION in pointers[0]
    assert RUN_SESSION not in "".join(pointers)

    locked = _tree(tmp_path, "pointers-locked")
    seal(locked / KENSAI)
    assert not [line for line in _lines(_runs(tmp_path, locked, home=home)) if POINTER in line]
    assert not [line for line in _lines(_steps(tmp_path, locked, home=home)) if POINTER in line]
    steps = _steps(tmp_path, gone, home=home)
    assert [line for line in _lines(steps) if POINTER in line] == [
        line for line in _lines(_steps(tmp_path, _tree(tmp_path, "plain"), home=home))
        if POINTER in line
    ]


# ── E10 — the permission proof, on real output ─────────────────────────────


def test_permission_proof_skips_as_root_and_asserts_station_items_1_to_4(tmp_path):
    with pytest.raises(pytest.skip.Exception):
        with sealed_paths(0):
            pytest.fail("a block run as root must be skipped, not entered")

    root = _tree(tmp_path)
    with sealed_paths(os.geteuid()) as seal:
        transcript = seal(root / KENSAI / f"{RUN_SESSION}.jsonl")
        opened = _runs(tmp_path, root)
        opened_json = _runs(tmp_path, root, "--json")
        seal(root)
        locked = _runs(tmp_path, root)
        locked_json = _runs(tmp_path, root, "--json")
        locked_steps = _steps(tmp_path, root)
    assert transcript.name == f"{RUN_SESSION}.jsonl"

    # Item 1: the transcript that could not be opened says so beside its id.
    assert _coverage_line(opened).endswith(f"unreadable: {RUN_SESSION} ({NOT_OPENED})")
    # Item 2: the root that could not be looked in is named, and neither report says it is empty.
    assert DIRECTORY_LINE.format(directory=root) in _lines(locked)
    assert NO_RUNS not in locked.stdout and NO_WORKFLOWS not in locked_steps.stdout
    # Item 3: `--json` is one document a fresh interpreter accepts, and carries each reason.
    for done in (opened_json, locked_json):
        parsed = subprocess.run(
            [sys.executable, "-c", "import json,sys; json.load(sys.stdin)"],
            input=done.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (done.returncode, parsed.returncode) == (0, 0), parsed.stderr
    assert json.loads(opened_json.stdout)["coverage"]["unreadable_reasons"] == [NOT_OPENED]
    assert json.loads(locked_json.stdout)["coverage"]["unreadable_directories"] == (
        _directory_objects(root)
    )
    # Item 4: the unmodified root's two reports are the baseline's, byte for byte.
    clean = _tree(tmp_path, "clean")
    _exactly_baseline(_runs(tmp_path, clean), "runs", "clean")
    _exactly_baseline(_steps(tmp_path, clean, "--json"), "steps_json", "clean")


def test_steps_unreadable_journal_prints_the_same_bytes_whether_or_not_its_directory_lists(
    tmp_path, with_mode, seal
):
    """CP-465 and E7: a run whose journal could not be opened lists none of its agent files, so
    `steps` prints for it exactly what it prints at `cd62d0f` — the same bytes, stderr and exit,
    with the run directory listable or not, in every form."""
    root = _orphan_tree(tmp_path)
    seal(root / WORKFLOW_DIR / "journal.jsonl")
    listable = [_steps(tmp_path, root, *form) for form in FORMS]
    with_mode(root / WORKFLOW_DIR, 0o111)

    unlistable = [_steps(tmp_path, root, *form) for form in FORMS]

    for form, before, after in zip(FORMS, listable, unlistable, strict=True):
        assert (after.returncode, after.stdout, after.stderr) == (
            before.returncode,
            before.stdout,
            before.stderr,
        ), form
        assert "could not be listed" not in after.stdout


# ── The one statement of what the time covers, in every exit-0 report ─────

STATEMENT_MEMBER = f'  "span": {json.dumps(STATEMENT)},\n'


def _statement_root(tmp_path: Path, case: str, request) -> Path:
    """The root each case reads: the reports these fixtures exercise, built as above."""
    if case == "absent_root":
        return tmp_path / "absent"
    if case == "empty_root":
        (tmp_path / "empty").mkdir()
        return tmp_path / "empty"
    root = _tree(tmp_path)
    if case == "gone_transcript":
        (root / KENSAI / f"{RUN_SESSION}.jsonl").unlink()
    elif case != "clean":
        seal = request.getfixturevalue("seal")
        target = {
            "locked_root": root,
            "locked_project_dir": root / KENSAI,
            "unopenable_transcript": root / KENSAI / f"{RUN_SESSION}.jsonl",
        }[case]
        seal(target)
    return root


@pytest.mark.parametrize(
    "case",
    [
        "clean",
        "gone_transcript",
        "absent_root",
        "empty_root",
        "locked_root",
        "locked_project_dir",
        "unopenable_transcript",
    ],
)
def test_every_exit_zero_report_carries_the_statement_once(tmp_path, case, request):
    """CP-496 and CP-498 on the real commands: every report `runs` and `steps` print with exit 0
    — populated, empty, or naming a directory or file they could not read — carries the one
    statement of what their time covers exactly once, as the text form's second, unindented line
    and as the JSON form's top-level `span` member. The helpers above take it out before comparing
    with the stored baselines; this reads the raw output, so a report that lost the statement, or
    printed it twice, fails here."""
    root = _statement_root(tmp_path, case, request)
    home = _home(tmp_path)
    commands = [
        ("runs", "--agents-json", str(AGENTS_JSON), "--projects-root", str(root)),
        ("steps", "--projects-root", str(root)),
    ]
    if case in ("clean", "locked_project_dir"):
        commands.append(("steps", "--projects-root", str(root), "--wf", WORKFLOW))

    for argv in commands:
        if case == "locked_project_dir" and "--wf" in argv:
            # The workflow sits in the locked directory: not found, exit 1, no report.
            done = _ka(tmp_path, home, *argv)
            assert (done.returncode, done.stdout) == (1, ""), argv
            assert STATEMENT not in done.stderr, argv
            continue
        text = _ka(tmp_path, home, *argv)
        as_json = _ka(tmp_path, home, *argv, "--json")

        assert (text.returncode, text.stderr) == (0, ""), argv
        lines = text.stdout.splitlines()
        assert lines[1] == STATEMENT and lines.count(STATEMENT) == 1, argv
        assert sum(STATEMENT in line for line in lines) == 1, argv
        assert (as_json.returncode, as_json.stderr) == (0, ""), argv
        assert json.loads(as_json.stdout)["span"] == STATEMENT, argv
        assert as_json.stdout.count(STATEMENT_MEMBER) == 1, argv
        assert as_json.stdout.count(json.dumps(STATEMENT)) == 1, argv
