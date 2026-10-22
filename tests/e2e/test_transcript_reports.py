"""End-to-end proof that `runs` and `steps` say what they hold, on the real commands.

The station this change walks (`ka-telemetry · shukuba · 2026-10-13_transcript-reports-say-what-
they-hold`, § Reached when) names four fixtures, built here and nowhere else:

- `R`: a copy of `tests/parity/projects/`;
- `U`: `R` with the line `not json {` appended to one run transcript and to one agent transcript;
- `H`: a home whose `.ka-telemetry/logs.jsonl` holds one `api_request` for each of two sessions;
- `E`: an empty home.

Every command is a child process of the absolute interpreter running this suite, under an explicit
temporary `HOME` and a `PATH` that is an empty directory, so no result depends on the running
user's own telemetry stream or on a `claude` that happens to be installed. No fixture copies the
user's stream: `H`'s two records are the first line of `tests/stream_parity/logs.jsonl` with
`session.id` replaced.

The words are `change_text.md`'s; these tests assert the fragments the rules fix.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from e2e.support import DATA_SET

from ka_telemetry.transcript_notes import SPAN_LINE

PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
PARITY = PRODUCT_ROOT / "tests" / "parity"
AGENTS_JSON = PARITY / "agents.json"
RUN_SESSION = "a839285c-263f-40a6-8801-71f49d00cc68"
FLOW_SESSION = "174d0111-bfa9-4539-92d9-5560e0ab4c36"
OTHER_RUN_SESSION = "15b8016a-f529-44fe-b6ea-1013fea8a4a8"
KENSAI = "-home-user-work-demo"
WORKFLOW = "wf_ec5e24de-808"
BAD_LINE = "not json {"

# What `0c6b14a` printed on `R`, so a change to a line that must not change is seen.
RUNS_COVERAGE_AT_BASE = "Coverage: 3 read / 0 unreadable / 3 total (3 listed, 0 named)"
STEPS_COVERAGE_AT_BASE = (
    "  Coverage: 9 with a step / 1 without / 0 started with no result / "
    "0 agent files with no journal entry at all"
)
NO_COST = "carries no cost"
POINTER = "ka-telemetry agents --where session.id="
SPAN_PATTERN = re.compile(r"first .*last .*response", re.IGNORECASE)
MISSING_CLAUDE_ERROR = re.compile(r"error: .*`claude`.*--agents-json[^\n]*")


def _api_request_line(session_id: str) -> str:
    """One stream line: the first line of the stream fixture, for `session_id`."""
    with (DATA_SET / "logs.jsonl").open(encoding="utf-8") as logs:
        record = json.loads(logs.readline())
    for resource in record["body"]["resourceLogs"]:
        for scope in resource["scopeLogs"]:
            for log_record in scope["logRecords"]:
                for attribute in log_record["attributes"]:
                    if attribute["key"] == "session.id":
                        attribute["value"]["stringValue"] = session_id
    return json.dumps(record)


def _home(root: Path, name: str, *session_ids: str, extra_lines: tuple[str, ...] = ()) -> Path:
    """A home holding a stream with one `api_request` per session id, then `extra_lines`."""
    home = root / name
    home.mkdir()
    if session_ids or extra_lines:
        stream = home / ".ka-telemetry"
        stream.mkdir()
        lines = [*(_api_request_line(sid) for sid in session_ids), *extra_lines]
        (stream / "logs.jsonl").write_text("".join(f"{line}\n" for line in lines))
    return home


def _append_bad_line(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    separator = "" if text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{BAD_LINE}\n", encoding="utf-8")


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("transcript-reports")
    projects_r = root / "R"
    shutil.copytree(PARITY / "projects", projects_r)
    projects_u = root / "U"
    shutil.copytree(projects_r, projects_u)
    _append_bad_line(projects_u / KENSAI / f"{RUN_SESSION}.jsonl")
    workflow = projects_u / KENSAI / FLOW_SESSION / "subagents" / "workflows" / WORKFLOW
    _append_bad_line(workflow / "agent-ab58aa559157db106.jsonl")
    empty_path = root / "empty-path"
    empty_path.mkdir()
    return {
        "R": projects_r,
        "U": projects_u,
        "E": _home(root, "E"),
        "H": _home(root, "H", RUN_SESSION, FLOW_SESSION),
        "path": empty_path,
        "root": root,
    }


def _command(
    world: dict[str, Path], home: Path, *argv: str, path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """`python3 -m ka_telemetry <argv>` under `home`, with `path` (an empty directory unless
    given) as the whole of `PATH`."""
    return subprocess.run(
        [sys.executable, "-B", "-m", "ka_telemetry", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=home,
        env={
            "HOME": str(home),
            "PATH": str(path if path is not None else world["path"]),
            "PYTHONPATH": str(PRODUCT_ROOT),
        },
    )


def _runs(world, projects, home, *extra, agents_json=AGENTS_JSON):
    return _command(
        world, home, "runs", "--agents-json", str(agents_json), "--projects-root", str(projects),
        *extra,
    )


def _steps(world, projects, home, *extra):
    return _command(world, home, "steps", "--projects-root", str(projects), *extra)


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def _coverage_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("Coverage:")]


def _both(world, projects, home, *extra):
    return {
        "runs": _runs(world, projects, home, *extra),
        "steps": _steps(world, projects, home, *extra),
    }


def test_no_cost_line_once_and_no_pointer_on_r_with_an_empty_home(world, tmp_path):
    for name, done in _both(world, world["R"], world["E"]).items():
        assert done.returncode == 0, (name, done.stderr)
        assert _count(NO_COST, done.stdout) == 1, name
        assert done.stdout.count("ka-telemetry agents") == 0, name

    workflows = tmp_path / "several"
    shutil.copytree(world["R"], workflows)
    sibling = workflows / KENSAI / FLOW_SESSION / "subagents" / "workflows" / "wf_second"
    shutil.copytree(sibling.parent / WORKFLOW, sibling)
    several = _steps(world, workflows, world["E"])
    assert several.stdout.count("session=") == 2
    assert _count(NO_COST, several.stdout) == 1


def test_a_projects_root_with_no_transcripts_still_prints_one_no_cost_line(world, tmp_path):
    empty_root = tmp_path / "no-transcripts"
    empty_root.mkdir()

    runs = _runs(world, empty_root, world["E"], agents_json=_agents_json(tmp_path))
    steps = _steps(world, empty_root, world["E"])

    assert "No runs read." in runs.stdout and _count(NO_COST, runs.stdout) == 1
    assert "No workflow runs read." in steps.stdout and _count(NO_COST, steps.stdout) == 1


def _agents_json(root: Path, *session_ids: str) -> Path:
    path = root / "agents.json"
    path.write_text(json.dumps([
        {"sessionId": sid, "kind": "background", "name": sid, "cwd": "/x", "state": "completed"}
        for sid in session_ids
    ]))
    return path


@pytest.mark.parametrize("home", ["E", "H"])
def test_json_forms_print_no_cost_word_under_both_homes(world, home):
    for name, done in _both(world, world["R"], world[home], "--json").items():
        assert done.returncode == 0, (name, done.stderr)
        assert _count("cost", done.stdout) == 0, name
        assert "unreadable_lines" not in done.stdout, name
    stored_steps = (PARITY / "expected" / "steps.json").read_text()
    assert _steps(world, world["R"], world[home], "--json").stdout == stored_steps


def test_pointer_names_the_displayed_session_only_under_h(world):
    runs = _runs(world, world["R"], world["H"]).stdout
    steps = _steps(world, world["R"], world["H"]).stdout

    assert [line for line in runs.splitlines() if "ka-telemetry agents" in line] == [
        f"Run `{POINTER}{RUN_SESSION}` to see Claude Code's cost estimate for session "
        f"{RUN_SESSION}."
    ]
    assert FLOW_SESSION not in runs
    assert [line for line in steps.splitlines() if "ka-telemetry agents" in line] == [
        f"Run `{POINTER}{FLOW_SESSION}` to see Claude Code's cost estimate for session "
        f"{FLOW_SESSION}."
    ]
    assert RUN_SESSION not in steps


def test_following_the_pointer_shows_a_row_with_real_agents(world):
    done = _command(world, world["H"], "agents", "--where", f"session.id={RUN_SESSION}")

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("1 agent row(s)")
    assert f"session={RUN_SESSION}" in done.stdout
    assert "cost_usd=" in done.stdout


def test_pointers_follow_report_order_under_the_no_cost_line_and_repeat_once(world, tmp_path):
    # the stream names the later run first; the report prints the earlier one first
    both = _home(tmp_path, "both", OTHER_RUN_SESSION, RUN_SESSION, RUN_SESSION)

    lines = _runs(world, world["R"], both).stdout.splitlines()

    assert _count(NO_COST, lines[-3]) == 1
    assert POINTER + RUN_SESSION in lines[-2]
    assert POINTER + OTHER_RUN_SESSION in lines[-1]
    assert sum("ka-telemetry agents" in line for line in lines) == 2

    shared = tmp_path / "shared-parent"
    shutil.copytree(world["R"], shared)
    parent = shared / KENSAI / FLOW_SESSION / "subagents" / "workflows"
    shutil.copytree(parent / WORKFLOW, parent / "wf_second")
    steps = _steps(world, shared, world["H"]).stdout.splitlines()
    assert _count(NO_COST, steps[-2]) == 1
    assert POINTER + FLOW_SESSION in steps[-1]
    assert sum("ka-telemetry agents" in line for line in steps) == 1


def test_unmatched_sessions_and_a_run_that_could_not_be_read_get_no_pointer_though_in_the_stream(
    world, tmp_path
):
    root = tmp_path / "projects"
    project = root / "proj"
    project.mkdir(parents=True)
    shutil.copy(
        PARITY / "projects" / KENSAI / f"{RUN_SESSION}.jsonl", project / f"{RUN_SESSION}.jsonl"
    )
    listing = _agents_json(tmp_path, RUN_SESSION, "gone-session", "unmatched-session")
    (project / "unmatched-session.jsonl").write_text("")
    home = _home(tmp_path, "stream", RUN_SESSION, "gone-session")

    out = _runs(world, root, home, agents_json=listing).stdout

    assert out.count("ka-telemetry agents") == 1 and POINTER + RUN_SESSION in out
    assert "gone-session" in out.splitlines()[2]  # still counted unreadable in Coverage:
    assert POINTER + "gone-session" not in out
    assert POINTER + "unmatched-session" not in out


@pytest.mark.parametrize("problem", ["absent-directory", "non-json-only", "unopenable"])
def test_stream_problems_change_nothing_but_the_pointers_for_both_commands(
    world, tmp_path, seal, problem
):
    if problem == "absent-directory":
        home = _home(tmp_path, "problem")
    elif problem == "non-json-only":
        home = _home(tmp_path, "problem", extra_lines=(BAD_LINE, "[1, 2]", "{}"))
    else:
        home = _home(tmp_path, "problem", RUN_SESSION, FLOW_SESSION)
        seal(home / ".ka-telemetry" / "logs.jsonl")

    for command in (_runs, _steps):
        expected = command(world, world["R"], world["E"])
        actual = command(world, world["R"], home)
        assert (actual.stdout, actual.stderr, actual.returncode) == (
            expected.stdout, expected.stderr, expected.returncode
        ) == (expected.stdout, "", 0)


def test_unreadable_line_is_counted_in_both_coverage_lines_on_u_and_nowhere_on_r(world):
    runs_r = _coverage_lines(_runs(world, world["R"], world["E"]).stdout)
    steps_r = _coverage_lines(_steps(world, world["R"], world["E"]).stdout)
    runs_u = _coverage_lines(_runs(world, world["U"], world["E"]).stdout)
    steps_u = _coverage_lines(_steps(world, world["U"], world["E"]).stdout)

    assert runs_r == [RUNS_COVERAGE_AT_BASE]
    assert steps_r == [STEPS_COVERAGE_AT_BASE]
    assert len(runs_u) == len(steps_u) == 1
    assert runs_u[0].startswith(RUNS_COVERAGE_AT_BASE) and "1 unreadable line" in runs_u[0]
    assert steps_u[0].startswith(STEPS_COVERAGE_AT_BASE) and "1 unreadable line" in steps_u[0]


def test_every_token_figure_and_span_on_u_equals_r_outside_the_note_block_and_coverage_suffix(
    world,
):
    for command in (_runs, _steps):
        on_r = command(world, world["R"], world["E"]).stdout.splitlines()
        on_u = command(world, world["U"], world["E"]).stdout.splitlines()
        assert len(on_r) == len(on_u)
        different = [(r, u) for r, u in zip(on_r, on_u, strict=True) if r != u]
        assert len(different) == 1
        before, after = different[0]
        assert before.lstrip().startswith("Coverage:") and after.startswith(before)


def test_json_coverage_on_u_has_one_member_more_valued_1_for_runs_and_each_steps_run_entry(world):
    runs_r = json.loads(_runs(world, world["R"], world["E"], "--json").stdout)
    runs_u = json.loads(_runs(world, world["U"], world["E"], "--json").stdout)
    steps_r = json.loads(_steps(world, world["R"], world["E"], "--json").stdout)
    steps_u = json.loads(_steps(world, world["U"], world["E"], "--json").stdout)

    assert "unreadable_lines" not in runs_r["coverage"]
    assert runs_u["coverage"] == {**runs_r["coverage"], "unreadable_lines": 1}
    assert runs_u["rows"] == runs_r["rows"]
    assert len(steps_u["runs"]) == len(steps_r["runs"]) >= 1
    for on_r, on_u in zip(steps_r["runs"], steps_u["runs"], strict=True):
        assert "unreadable_lines" not in on_r["coverage"]
        assert on_u["coverage"] == {**on_r["coverage"], "unreadable_lines": 1}
        assert on_u["rows"] == on_r["rows"]


@pytest.mark.parametrize(
    "extra", [(), ("--session", RUN_SESSION), ("--json",), ("--session", RUN_SESSION, "--json")]
)
def test_missing_claude_is_one_error_line_for_runs_with_and_without_session(world, extra):
    done = _command(world, world["E"], "runs", *extra)

    assert done.returncode == 1
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    assert len(done.stderr.splitlines()) == 1
    assert MISSING_CLAUDE_ERROR.fullmatch(done.stderr.rstrip("\n"))
    assert done.stderr.startswith("error:")


def test_steps_starts_no_process_and_is_unaffected_by_a_missing_claude(world, tmp_path):
    # a `claude` that would leave a mark if it were ever started
    bin_dir = tmp_path / "marking-bin"
    bin_dir.mkdir()
    mark = tmp_path / "started"
    claude = bin_dir / "claude"
    claude.write_text(f"#!/bin/sh\necho started > {mark}\n")
    claude.chmod(0o755)

    with_claude = _command(
        world, world["E"], "steps", "--projects-root", str(world["R"]), path=bin_dir
    )
    without = _steps(world, world["R"], world["E"])

    assert (with_claude.stdout, with_claude.returncode) == (without.stdout, without.returncode)
    assert without.returncode == 0 and without.stderr == ""
    assert not mark.exists()


# ── Every record the reports could not read is counted and named (this change) ──
#
# Each fixture is its own fresh copy of `tests/parity/projects/` (`R`), changed in the one place
# the case names, and read through the real commands as child processes under `E`, an empty home.

RUNS_COVERAGE_JSON_AT_BASE = {
    "read": 3,
    "unreadable": 0,
    "total": 3,
    "unreadable_ids": [],
    "dropped_no_session": 1,
    "listed": 3,
    "named": 0,
}
STEPS_COVERAGE_JSON_AT_BASE = {
    "with_step": 9,
    "without_step": 1,
    "started_no_result": 0,
    "files_with_no_journal_entry": 0,
}
# the `development-code-integrity (produce)` agent, `types=senior-engineer`
AGENT = "ab58aa559157db106"
RUNS_SUFFIX_ONE = " — 1 unreadable line skipped in the transcripts read"
TRANSCRIPT_LINES_ONE = " — 1 unreadable line skipped in this workflow run's agent transcripts"
JOURNAL_ONE = " — 1 unreadable journal line skipped"
META_ONE = " — 1 unreadable meta file, so its agent's type shows as (none)"
TRANSCRIPT_ONE = " — 1 unreadable agent transcript, so no spend is read for its agent"

UNADDABLE_LINES = [
    "[1, 2]",
    '{"type":"assistant","message":"x"}',
    '{"type":"assistant","message":{"usage":{"input_tokens":"5"}}}',
]
MALFORMED_GOVERNED_LINES = [
    "null",
    "42",
    '"s"',
    '{"type":"assistant","message":{"usage":[1]}}',
    '{"type":"assistant","message":{"usage":{"cache_creation":"x"}}}',
    '{"type":"assistant","message":{"usage":{"output_tokens_details":5}}}',
    '{"type":"assistant","message":{"usage":{"input_tokens":true}}}',
    '{"type":"assistant","message":{"usage":{"input_tokens":5.0}}}',
    '{"type":"assistant","message":{"usage":{"input_tokens":5e0}}}',
    '{"type":"assistant","message":{"usage":{"output_tokens":-5}}}',
    '{"type":"assistant","message":{"usage":{"output_tokens":-0}}}',
    '{"type":"assistant","message":{"usage":{"cache_read_input_tokens":[1]}}}',
    '{"type":"assistant","message":{"usage":{"cache_creation":{"ephemeral_1h_input_tokens":"1"}}}}',
    '{"type":"assistant","message":{"usage":{"cache_creation":{"ephemeral_5m_input_tokens":1.5}}}}',
    '{"type":"assistant","message":{"usage":{"output_tokens_details":{"thinking_tokens":{}}}}}',
    '{"type":"assistant","timestamp":5,"message":{"usage":{"input_tokens":1}}}',
    '{"type":"assistant","message":{"model":["m"],"usage":{"input_tokens":1}}}',
    '{"type":"assistant","message":{"id":["i"],"usage":{"input_tokens":1}}}',
]


def _fresh(world, tmp_path, name="R") -> Path:
    """A fresh copy of the reference projects, changed by nothing."""
    copy = tmp_path / name
    shutil.copytree(PARITY / "projects", copy)
    return copy


def _transcript(projects: Path) -> Path:
    return projects / KENSAI / f"{RUN_SESSION}.jsonl"


def _workflow(projects: Path) -> Path:
    return projects / KENSAI / FLOW_SESSION / "subagents" / "workflows" / WORKFLOW


def _append(path: Path, *lines: str) -> None:
    text = path.read_text(encoding="utf-8")
    separator = "" if text.endswith("\n") else "\n"
    path.write_text(text + separator + "".join(f"{line}\n" for line in lines), encoding="utf-8")


def _json_of(done) -> dict:
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return json.loads(done.stdout)


def _steps_coverage(world, projects, *extra) -> list[dict]:
    done = _steps(world, projects, world["E"], "--json", *extra)
    return [r["coverage"] for r in _json_of(done)["runs"]]


def _differing_lines(before: str, after: str) -> list[tuple[str, str]]:
    assert len(before.splitlines()) == len(after.splitlines())
    return [(b, a) for b, a in zip(before.splitlines(), after.splitlines(), strict=True) if b != a]


def _line_removed(before: str, after: str) -> list[str]:
    after_lines = after.splitlines()
    return [line for line in before.splitlines() if line not in after_lines]


@pytest.mark.parametrize("line", UNADDABLE_LINES)
def test_runs_counts_each_record_that_parses_but_cannot_be_added(world, tmp_path, line):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_transcript(dirty), line)

    on_clean = _runs(world, clean, world["E"])
    on_dirty = _runs(world, dirty, world["E"])

    assert on_dirty.returncode == 0 and on_dirty.stderr == ""
    assert _coverage_lines(on_dirty.stdout) == [RUNS_COVERAGE_AT_BASE + RUNS_SUFFIX_ONE]
    assert _differing_lines(on_clean.stdout, on_dirty.stdout) == [
        (RUNS_COVERAGE_AT_BASE, RUNS_COVERAGE_AT_BASE + RUNS_SUFFIX_ONE)
    ]
    on_dirty_json = _json_of(_runs(world, dirty, world["E"], "--json"))
    assert on_dirty_json["coverage"] == {**RUNS_COVERAGE_JSON_AT_BASE, "unreadable_lines": 1}
    assert on_dirty_json["rows"] == _json_of(_runs(world, clean, world["E"], "--json"))["rows"]


@pytest.mark.parametrize("line", UNADDABLE_LINES[:2])
def test_steps_counts_the_first_two_of_those_in_an_agent_transcript(world, tmp_path, line):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_workflow(dirty) / f"agent-{AGENT}.jsonl", line)

    on_clean = _steps(world, clean, world["E"])
    on_dirty = _steps(world, dirty, world["E"])

    assert on_dirty.returncode == 0 and on_dirty.stderr == ""
    assert _coverage_lines(on_dirty.stdout) == [STEPS_COVERAGE_AT_BASE + TRANSCRIPT_LINES_ONE]
    assert _differing_lines(on_clean.stdout, on_dirty.stdout) == [
        (STEPS_COVERAGE_AT_BASE, STEPS_COVERAGE_AT_BASE + TRANSCRIPT_LINES_ONE)
    ]
    assert _steps_coverage(world, dirty) == [{**STEPS_COVERAGE_JSON_AT_BASE, "unreadable_lines": 1}]


def test_steps_names_an_unreadable_journal_line(world, tmp_path):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_workflow(dirty) / "journal.jsonl", BAD_LINE)

    on_dirty = _steps(world, dirty, world["E"])

    assert on_dirty.returncode == 0 and on_dirty.stderr == ""
    assert _differing_lines(_steps(world, clean, world["E"]).stdout, on_dirty.stdout) == [
        (STEPS_COVERAGE_AT_BASE, STEPS_COVERAGE_AT_BASE + JOURNAL_ONE)
    ]
    assert _steps_coverage(world, dirty) == [
        {**STEPS_COVERAGE_JSON_AT_BASE, "unreadable_journal_lines": 1}
    ]


def test_steps_names_an_unreadable_meta_file(world, tmp_path):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    (_workflow(dirty) / f"agent-{AGENT}.meta.json").write_text(BAD_LINE)

    on_dirty = _steps(world, dirty, world["E"])

    assert on_dirty.returncode == 0 and on_dirty.stderr == ""
    differing = _differing_lines(_steps(world, clean, world["E"]).stdout, on_dirty.stdout)
    assert differing[0] == (STEPS_COVERAGE_AT_BASE, STEPS_COVERAGE_AT_BASE + META_ONE)
    (row_before, row_after) = differing[1]
    assert "types=senior-engineer" in row_before and "types=(none)" in row_after
    assert row_before.replace("types=senior-engineer", "types=(none)") == row_after
    assert len(differing) == 2
    assert _steps_coverage(world, dirty) == [
        {**STEPS_COVERAGE_JSON_AT_BASE, "unreadable_meta_files": 1}
    ]


def test_steps_names_an_agent_transcript_it_could_not_read(world, tmp_path):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    (_workflow(dirty) / f"agent-{AGENT}.jsonl").unlink()

    on_clean = _steps(world, clean, world["E"])
    on_dirty = _steps(world, dirty, world["E"])

    assert on_dirty.returncode == 0 and on_dirty.stderr == ""
    assert _coverage_lines(on_dirty.stdout) == [STEPS_COVERAGE_AT_BASE + TRANSCRIPT_ONE]
    assert STEPS_COVERAGE_AT_BASE + TRANSCRIPT_ONE in on_dirty.stdout
    removed = _line_removed(on_clean.stdout, on_dirty.stdout)
    assert [line for line in removed if not line.lstrip().startswith("Coverage:")] == [
        "    span=366.182s",
        "    claude-opus-5: input=32 output=218 thinking=0 cache_read=1012546 "
        "cache_write_1h=0 cache_write_5m=102593",
    ]
    assert f"agents={AGENT}" in on_dirty.stdout
    dirty_lines = on_dirty.stdout.splitlines()
    row = next(n for n, line in enumerate(dirty_lines) if f"agents={AGENT}" in line)
    assert dirty_lines[row + 1] == "    span=n/a"
    assert _steps_coverage(world, dirty) == [
        {**STEPS_COVERAGE_JSON_AT_BASE, "unreadable_agent_transcripts": 1}
    ]


def _break(world, tmp_path, fixture) -> tuple[Path, str]:
    """A fresh tree changed by `fixture`; the command that reads it."""
    projects = _fresh(world, tmp_path, fixture)
    workflow = _workflow(projects)
    if fixture == "runs-line":
        _append(_transcript(projects), "[1, 2]")
    elif fixture == "runs-message":
        _append(_transcript(projects), UNADDABLE_LINES[1])
    elif fixture == "runs-token":
        _append(_transcript(projects), UNADDABLE_LINES[2])
    elif fixture == "steps-line":
        _append(workflow / f"agent-{AGENT}.jsonl", "[1, 2]")
    elif fixture == "steps-message":
        _append(workflow / f"agent-{AGENT}.jsonl", UNADDABLE_LINES[1])
    elif fixture == "journal":
        _append(workflow / "journal.jsonl", BAD_LINE)
    elif fixture == "meta":
        (workflow / f"agent-{AGENT}.meta.json").write_text(BAD_LINE)
    else:
        (workflow / f"agent-{AGENT}.jsonl").unlink()
    return projects, "runs" if fixture.startswith("runs") else "steps"


FIXTURES = [
    "runs-line", "runs-message", "runs-token", "steps-line", "steps-message", "journal", "meta",
    "deleted",
]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_json_differs_from_clean_only_in_count_members_equal_to_one_in_total(
    world, tmp_path, fixture
):
    projects, command = _break(world, tmp_path, fixture)
    clean = _fresh(world, tmp_path, "clean")
    run = _runs if command == "runs" else _steps

    on_dirty = _json_of(run(world, projects, world["E"], "--json"))
    on_clean = _json_of(run(world, clean, world["E"], "--json"))
    if command == "runs":
        dirty_coverage, clean_coverage = on_dirty["coverage"], on_clean["coverage"]
        rows_dirty, rows_clean = on_dirty["rows"], on_clean["rows"]
    else:
        (dirty_run,), (clean_run,) = on_dirty["runs"], on_clean["runs"]
        dirty_coverage, clean_coverage = dirty_run["coverage"], clean_run["coverage"]
        rows_dirty, rows_clean = dirty_run["rows"], clean_run["rows"]

    added = {k: v for k, v in dirty_coverage.items() if k not in clean_coverage}
    assert {k: v for k, v in dirty_coverage.items() if k in clean_coverage} == clean_coverage
    assert all(k.startswith("unreadable_") for k in added)
    assert sum(added.values()) == 1 and all(v == 1 for v in added.values())
    if fixture not in {"meta", "deleted"}:
        assert rows_dirty == rows_clean


def test_clean_coverage_text_and_json_equal_the_baseline(world, tmp_path):
    clean = _fresh(world, tmp_path)

    runs, steps = _runs(world, clean, world["E"]), _steps(world, clean, world["E"])

    assert _coverage_lines(runs.stdout) == [RUNS_COVERAGE_AT_BASE]
    assert _coverage_lines(steps.stdout) == [STEPS_COVERAGE_AT_BASE]
    assert _json_of(_runs(world, clean, world["E"], "--json"))["coverage"] == (
        RUNS_COVERAGE_JSON_AT_BASE
    )
    assert _steps_coverage(world, clean) == [STEPS_COVERAGE_JSON_AT_BASE]
    assert (runs.stderr, steps.stderr) == ("", "")


@pytest.mark.parametrize("line", MALFORMED_GOVERNED_LINES)
def test_malformed_governed_fields_never_stop_either_command(world, tmp_path, line):
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_transcript(dirty), line)
    _append(_workflow(dirty) / f"agent-{AGENT}.jsonl", line)

    runs = _json_of(_runs(world, dirty, world["E"], "--json"))
    (steps,) = _steps_coverage(world, dirty)

    assert runs["coverage"]["unreadable_lines"] == 1
    assert steps["unreadable_lines"] == 1
    clean = _fresh(world, tmp_path, "clean")
    assert runs["rows"] == _json_of(_runs(world, clean, world["E"], "--json"))["rows"]
    figures = {k: v for k, v in steps.items() if k != "unreadable_lines"}
    assert figures == STEPS_COVERAGE_JSON_AT_BASE


def _response(response_id: str, input_tokens) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": response_id,
                "model": "claude-opus-5",
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        }
    )


@pytest.mark.parametrize("order", ["invalid-then-valid", "valid-then-invalid"])
def test_both_response_id_orderings_through_the_commands(world, tmp_path, order):
    invalid, valid = _response("zz-r1", "5"), _response("zz-r1", 7)
    lines = [invalid, valid] if order == "invalid-then-valid" else [valid, invalid]
    dirty, equivalent = _fresh(world, tmp_path, "dirty"), _fresh(world, tmp_path, "equivalent")
    _append(_transcript(dirty), *lines)
    _append(_transcript(equivalent), valid)
    _append(_workflow(dirty) / f"agent-{AGENT}.jsonl", *lines)
    _append(_workflow(equivalent) / f"agent-{AGENT}.jsonl", valid)

    runs_dirty = _json_of(_runs(world, dirty, world["E"], "--json"))
    runs_equivalent = _json_of(_runs(world, equivalent, world["E"], "--json"))
    (steps_dirty,) = _json_of(_steps(world, dirty, world["E"], "--json"))["runs"]
    (steps_equivalent,) = _json_of(_steps(world, equivalent, world["E"], "--json"))["runs"]

    assert runs_dirty["rows"] == runs_equivalent["rows"]
    assert runs_dirty["coverage"] == {**runs_equivalent["coverage"], "unreadable_lines": 1}
    assert steps_dirty["rows"] == steps_equivalent["rows"]
    assert steps_dirty["coverage"] == {**steps_equivalent["coverage"], "unreadable_lines": 1}


def test_combined_kinds_and_counts_above_one_use_plural_counts_and_omit_zero_clauses(
    world, tmp_path
):
    dirty = _fresh(world, tmp_path, "dirty")
    workflow = _workflow(dirty)
    other = "a53ef7efd8c468bad"
    _append(workflow / f"agent-{AGENT}.jsonl", "[1, 2]", '"s"')
    _append(workflow / "journal.jsonl", BAD_LINE, "[1]")
    (workflow / f"agent-{AGENT}.meta.json").write_text(BAD_LINE)
    (workflow / f"agent-{other}.meta.json").write_text("[1]")
    (workflow / f"agent-{other}.jsonl").unlink()
    (workflow / "agent-a85c5919a45174165.jsonl").unlink()

    done = _steps(world, dirty, world["E"])

    assert done.returncode == 0 and done.stderr == ""
    assert _coverage_lines(done.stdout) == [
        STEPS_COVERAGE_AT_BASE
        + " — 2 unreadable lines skipped in this workflow run's agent transcripts"
        + " — 2 unreadable journal lines skipped"
        + " — 2 unreadable meta files, so their agents' types show as (none)"
        + " — 2 unreadable agent transcripts, so no spend is read for their agents"
    ]
    only_journal = _fresh(world, tmp_path, "only-journal")
    _append(_workflow(only_journal) / "journal.jsonl", BAD_LINE, "[1]", "null")
    text = _coverage_lines(_steps(world, only_journal, world["E"]).stdout)
    assert text == [STEPS_COVERAGE_AT_BASE + " — 3 unreadable journal lines skipped"]


def test_two_workflow_runs_count_independently(world, tmp_path):
    dirty = _fresh(world, tmp_path, "dirty")
    first = _workflow(dirty)
    shutil.copytree(first, first.parent / "wf_second")
    _own_response_ids(first.parent / "wf_second")
    _append(first.parent / "wf_second" / "journal.jsonl", BAD_LINE)

    done = _steps(world, dirty, world["E"])
    runs = _json_of(_steps(world, dirty, world["E"], "--json"))["runs"]

    assert _coverage_lines(done.stdout) == [
        STEPS_COVERAGE_AT_BASE,
        STEPS_COVERAGE_AT_BASE + JOURNAL_ONE,
    ]
    assert [r["wf_id"] for r in runs] == [WORKFLOW, "wf_second"]
    assert "unreadable_journal_lines" not in runs[0]["coverage"]
    assert runs[1]["coverage"]["unreadable_journal_lines"] == 1


# A journal `agentId` that can name no file: one holding a NUL, or an unpaired surrogate. The
# agent's transcript cannot be opened (CP-418) and its sidecar is absent (CP-417), and `steps` still
# prints its whole report with nothing on stderr (CP-424).
STEPS_COVERAGE_WITH_ONE_MORE_UNFINISHED = (
    "  Coverage: 9 with a step / 2 without / 1 started with no result / "
    "0 agent files with no journal entry at all"
)


@pytest.mark.parametrize("agent_id", ["a\u0000b", "\ud800"])
def test_an_agent_id_that_can_name_no_file_never_stops_steps(world, tmp_path, agent_id):
    dirty = _fresh(world, tmp_path, "dirty")
    started = json.dumps({"type": "started", "agentId": agent_id})
    _append(_workflow(dirty) / "journal.jsonl", started)

    done = _steps(world, dirty, world["E"])

    assert done.returncode == 0 and done.stderr == ""
    assert _coverage_lines(done.stdout) == [
        STEPS_COVERAGE_WITH_ONE_MORE_UNFINISHED + TRANSCRIPT_ONE
    ]
    assert _steps_coverage(world, dirty) == [
        {
            **STEPS_COVERAGE_JSON_AT_BASE,
            "without_step": 2,
            "started_no_result": 1,
            "unreadable_agent_transcripts": 1,
        }
    ]


def _readable_response(model: str, timestamp: str | None = None) -> str:
    record = {
        "type": "assistant",
        "message": {"id": "zz-readable", "model": model, "usage": {"input_tokens": 1}},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return json.dumps(record)


def test_a_string_the_output_cannot_encode_never_stops_either_text_report(world, tmp_path):
    """CP-412: every line is read or unreadable, and no line stops the report. A `model` holding
    an unpaired surrogate is a JSON string (CP-414), so the line is read; the text form prints the
    character as the `\\uXXXX` escape the `--json` form already prints for it (the call is
    recorded in this change's code-integrity report)."""
    dirty = _fresh(world, tmp_path, "dirty")
    line = _readable_response("\ud800")
    _append(_transcript(dirty), line)
    _append(_workflow(dirty) / f"agent-{AGENT}.jsonl", line)

    for name, done in _both(world, dirty, world["E"]).items():
        assert done.returncode == 0 and done.stderr == "", name
        assert "\\ud800" in done.stdout, name
    assert _coverage_lines(_runs(world, dirty, world["E"]).stdout) == [RUNS_COVERAGE_AT_BASE]
    assert _coverage_lines(_steps(world, dirty, world["E"]).stdout) == [STEPS_COVERAGE_AT_BASE]


def test_a_date_with_no_offset_never_stops_runs_and_is_read(world, tmp_path):
    """CP-414: a `timestamp` string is readable and the record is read; CP-412: no line stops the
    report. The reference transcript's own dates carry `Z`."""
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_transcript(dirty), _readable_response("claude-probe", "2026-10-01T00:00:00"))

    done = _runs(world, dirty, world["E"])

    assert done.returncode == 0 and done.stderr == ""
    assert _coverage_lines(done.stdout) == [RUNS_COVERAGE_AT_BASE]
    assert "claude-probe: input=1 " in done.stdout
    assert _json_of(_runs(world, dirty, world["E"], "--json"))["coverage"] == (
        RUNS_COVERAGE_JSON_AT_BASE
    )


def test_no_agents_and_all_lines_rejected_and_missing_versus_empty_files(world, tmp_path):
    no_agents = _fresh(world, tmp_path, "no-agents")
    (_workflow(no_agents) / "journal.jsonl").write_text(f"[1]\n{BAD_LINE}\n")
    done = _steps(world, no_agents, world["E"])
    assert done.returncode == 0 and done.stderr == ""
    assert "No agents read for this run." in done.stdout
    assert _coverage_lines(done.stdout) == [
        "  Coverage: 0 with a step / 0 without / 0 started with no result / "
        "10 agent files with no journal entry at all — 2 unreadable journal lines skipped"
    ]

    rejected = _fresh(world, tmp_path, "rejected")
    _transcript(rejected).write_text(f"[1]\nnull\n{BAD_LINE}\n")
    runs = _runs(world, rejected, world["E"])
    assert runs.returncode == 0 and runs.stderr == ""
    assert _coverage_lines(runs.stdout) == [
        RUNS_COVERAGE_AT_BASE + " — 3 unreadable lines skipped in the transcripts read"
    ]

    empty, missing = _fresh(world, tmp_path, "empty"), _fresh(world, tmp_path, "missing")
    (_workflow(empty) / f"agent-{AGENT}.jsonl").write_text("")
    (_workflow(missing) / f"agent-{AGENT}.jsonl").unlink()
    assert _coverage_lines(_steps(world, empty, world["E"]).stdout) == [STEPS_COVERAGE_AT_BASE]
    assert _coverage_lines(_steps(world, missing, world["E"]).stdout) == [
        STEPS_COVERAGE_AT_BASE + TRANSCRIPT_ONE
    ]


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("json_form", [False, True])
def test_dirty_fixtures_with_session_wf_json_and_explicit_projects_root(
    world, tmp_path, fixture, json_form
):
    projects, command = _break(world, tmp_path, fixture)
    extra = ("--json",) if json_form else ()
    if command == "runs":
        scoped = _runs(world, projects, world["E"], "--session", RUN_SESSION, *extra)
        unscoped = _runs(world, projects, world["E"], *extra)
    else:
        scoped = _steps(world, projects, world["E"], "--wf", WORKFLOW, *extra)
        unscoped = _steps(world, projects, world["E"], *extra)

    assert scoped.returncode == 0 and scoped.stderr == ""
    assert unscoped.returncode == 0 and unscoped.stderr == ""
    if json_form:
        key = "coverage"
        scoped_data, unscoped_data = json.loads(scoped.stdout), json.loads(unscoped.stdout)
        if command == "runs":
            assert scoped_data[key] == unscoped_data[key]
        else:
            assert [r[key] for r in scoped_data["runs"]] == [r[key] for r in unscoped_data["runs"]]
    else:
        assert _coverage_lines(scoped.stdout) == _coverage_lines(unscoped.stdout)
        assert "unreadable" in "".join(_coverage_lines(scoped.stdout)).replace(
            "0 unreadable", ""
        )


def _context(world, transcript: Path) -> subprocess.CompletedProcess[str]:
    return _command(world, world["E"], "context", str(transcript), "--json")


def test_context_json_equals_clean_on_R_and_on_R_plus_bad_json_lines(world, tmp_path):
    clean = _fresh(world, tmp_path, "clean")
    dirty = _fresh(world, tmp_path, "dirty")
    _append(_transcript(dirty), BAD_LINE, BAD_LINE)

    on_clean, on_dirty = _context(world, _transcript(clean)), _context(world, _transcript(dirty))

    assert on_clean.returncode == on_dirty.returncode == 0
    assert on_clean.stderr == on_dirty.stderr == ""
    clean_payload, dirty_payload = json.loads(on_clean.stdout), json.loads(on_dirty.stdout)
    assert (clean_payload.pop("unreadable_lines"), dirty_payload.pop("unreadable_lines")) == (0, 2)
    assert json.dumps(clean_payload).replace(str(clean), "<R>") == (
        json.dumps(dirty_payload).replace(str(dirty), "<R>")
    )
    assert clean_payload["context_size"] == dirty_payload["context_size"] == 34224


@pytest.mark.parametrize("fixture", FIXTURES)
def test_missing_claude_no_cost_line_and_pointer_lines_are_unchanged(world, tmp_path, fixture):
    projects, command = _break(world, tmp_path, fixture)
    clean = _fresh(world, tmp_path, "clean")
    run = _runs if command == "runs" else _steps

    on_dirty, on_clean = run(world, projects, world["H"]), run(world, clean, world["H"])

    assert _count(NO_COST, on_dirty.stdout) == 1
    pointers = [line for line in on_dirty.stdout.splitlines() if "ka-telemetry agents" in line]
    clean_pointers = [ln for ln in on_clean.stdout.splitlines() if "ka-telemetry agents" in ln]
    assert pointers == clean_pointers
    assert len(pointers) == 1
    missing = _command(world, world["E"], "runs")
    assert missing.returncode == 1 and missing.stdout == ""
    assert MISSING_CLAUDE_ERROR.fullmatch(missing.stderr.rstrip("\n"))


def _without_reason(text: str) -> str:
    """`text` with the reason `runs` prints beside an unreadable id and, in JSON, its list."""
    text = re.sub(r" \((gone|could not be opened)\)", "", text)
    return re.sub(r'\n\s*"unreadable_reasons": \[[^\]]*\],?', "", text)


def test_runs_directory_in_place_of_transcript_prints_what_the_removed_one_prints(world, tmp_path):
    directory = _fresh(world, tmp_path, "directory")
    removed = _fresh(world, tmp_path, "removed")
    transcript = _transcript(directory)
    transcript.unlink()
    transcript.mkdir()
    _transcript(removed).unlink()

    for extra in ((), ("--json",), ("--session", RUN_SESSION)):
        on_directory = _runs(world, directory, world["E"], *extra)
        on_removed = _runs(world, removed, world["E"], *extra)
        assert on_directory.returncode == on_removed.returncode == 0
        assert on_directory.stderr == on_removed.stderr == ""
        # They differ only in the reason each prints for the one unreadable run (CP-443).
        assert _without_reason(on_directory.stdout.replace(str(directory), "<R>")) == (
            _without_reason(on_removed.stdout.replace(str(removed), "<R>"))
        )

    text = _runs(world, directory, world["E"])
    assert _coverage_lines(text.stdout)[0].startswith("Coverage: 2 read / 1 unreadable / 3 total")
    assert RUN_SESSION in _coverage_lines(text.stdout)[0]
    payload = _json_of(_runs(world, directory, world["E"], "--json"))
    clean_payload = _json_of(_runs(world, _fresh(world, tmp_path, "clean"), world["E"], "--json"))
    assert RUN_SESSION not in [row["session_id"] for row in payload["rows"]]
    assert payload["coverage"]["unreadable"] == 1
    assert payload["coverage"]["unreadable_ids"] == [RUN_SESSION]
    assert payload["coverage"]["unreadable_reasons"] == ["could not be opened"]
    assert payload["coverage"].keys() == clean_payload["coverage"].keys() | {"unreadable_reasons"}
    gone = _json_of(_runs(world, removed, world["E"], "--json"))
    assert gone["coverage"]["unreadable_reasons"] == ["gone"]
    assert RUN_SESSION + " (could not be opened)" in _coverage_lines(text.stdout)[0]
    assert RUN_SESSION + " (gone)" in _coverage_lines(_runs(world, removed, world["E"]).stdout)[0]


NEIGHBOUR = "wf_neighbour-1"


def _section(text: str, wf_id: str) -> list[str]:
    """The lines of `steps` text that belong to one workflow run: its header to the next blank."""
    lines = text.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith(f"{wf_id} "))
    end = next((n for n in range(start, len(lines)) if lines[n] == ""), len(lines))
    return lines[start:end]


def _own_response_ids(run_dir: Path) -> None:
    """Every response id in `run_dir`'s agent transcripts changed to one of its own, so a run
    copied from another shares no response with it."""
    for transcript in run_dir.glob("agent-*.jsonl"):
        text = transcript.read_text(encoding="utf-8")
        transcript.write_text(text.replace('"id":"msg_', '"id":"copy_msg_'), encoding="utf-8")


def _with_neighbour(world, tmp_path, name: str) -> Path:
    """A fresh `R` whose workflow directory is copied beside itself as a second, readable run
    that holds every response the original does."""
    projects = _fresh(world, tmp_path, name)
    shutil.copytree(_workflow(projects), _workflow(projects).parent / NEIGHBOUR)
    return projects


def test_steps_directory_in_place_of_journal_is_named_not_claimed(world, tmp_path):
    control = _with_neighbour(world, tmp_path, "control")
    failed = _with_neighbour(world, tmp_path, "failed")
    journal = _workflow(failed) / "journal.jsonl"
    journal.unlink()
    journal.mkdir()

    text = _steps(world, failed, world["E"])
    scoped = _steps(world, failed, world["E"], "--wf", WORKFLOW)

    assert text.returncode == scoped.returncode == 0
    assert text.stderr == scoped.stderr == ""
    section = _section(text.stdout, WORKFLOW)
    assert section[1].startswith(
        "  Coverage: 0 with a step / 0 without / 0 started with no result / "
        "0 agent files with no journal entry at all"
    )
    assert "1 unreadable journal file" in section[1]
    assert section[2] == "  No agents read for this run."
    assert _coverage_lines(scoped.stdout) == [section[1]]
    assert not any("10 agent files with no journal entry at all" in line for line in section)
    alone = _steps(world, control, world["E"], "--wf", NEIGHBOUR)
    assert _section(text.stdout, NEIGHBOUR)[1:] == _section(alone.stdout, NEIGHBOUR)[1:]
    payload = _json_of(_steps(world, failed, world["E"], "--json"))
    (run,) = [r for r in payload["runs"] if r["wf_id"] == WORKFLOW]
    assert run["coverage"] == {
        "with_step": 0,
        "without_step": 0,
        "started_no_result": 0,
        "files_with_no_journal_entry": 0,
        "unreadable_journal_files": 1,
    }
    assert run["rows"] == []
    control_payload = _json_of(_steps(world, control, world["E"], "--wf", NEIGHBOUR, "--json"))
    (neighbour,) = [r for r in payload["runs"] if r["wf_id"] == NEIGHBOUR]
    (neighbour_alone,) = control_payload["runs"]
    assert neighbour == neighbour_alone
    # With both runs read, the neighbour's copies of every response count nothing.
    both = _json_of(_steps(world, control, world["E"], "--json"))
    (neighbour_of_both,) = [r for r in both["runs"] if r["wf_id"] == NEIGHBOUR]
    assert all(row["spend"] == {} for row in neighbour_of_both["rows"])


def _context_text(world, path: str) -> subprocess.CompletedProcess[str]:
    return _command(world, world["E"], "context", path)


def test_context_command_on_every_case_of_the_station(world, tmp_path):
    root = _fresh(world, tmp_path)
    clean = _transcript(root)
    dirty = tmp_path / "dirty.jsonl"
    shutil.copyfile(clean, dirty)
    _append(dirty, BAD_LINE, "[1, 2]")
    only_bad = tmp_path / "only-bad.jsonl"
    only_bad.write_text(f"{BAD_LINE}\n")
    directory = tmp_path / "directory.jsonl"
    directory.mkdir()
    missing = str(tmp_path / "missing.jsonl")
    under_file = f"{clean}/x.jsonl"
    base_members = ["transcript", "context_size", "unreadable_lines"]
    old_unknown = "Context size: unknown — no assistant usage read from {path}"
    cases = [
        (clean, "Context size: 34224 tokens", 34224, 0, base_members),
        (
            dirty,
            "Context size: 34224 tokens — 2 unreadable lines skipped in this transcript",
            34224,
            2,
            base_members,
        ),
        (
            only_bad,
            old_unknown.format(path=only_bad) + " — 1 unreadable line skipped in this transcript",
            None,
            1,
            base_members,
        ),
        (
            directory,
            f"Context size: unknown — the transcript {directory} could not be opened. "
            "Run this command again once it can be read.",
            None,
            0,
            [*base_members, "unreadable_transcripts"],
        ),
        (missing, old_unknown.format(path=missing), None, 0, base_members),
        (under_file, old_unknown.format(path=under_file), None, 0, base_members),
    ]

    for path, text, size, lines, members in cases:
        as_text = _context_text(world, str(path))
        as_json = _command(world, world["E"], "context", str(path), "--json")

        assert (as_text.returncode, as_text.stderr) == (0, "")
        assert as_text.stdout == text + "\n"
        assert (as_json.returncode, as_json.stderr) == (0, "")
        payload = json.loads(as_json.stdout)
        assert list(payload) == members
        assert (payload["transcript"], payload["context_size"], payload["unreadable_lines"]) == (
            str(path),
            size,
            lines,
        )
        if "unreadable_transcripts" in payload:
            assert payload["unreadable_transcripts"] == 1


# ── the one statement of what the time covers, on the real commands ──


def _reference_reports(world, home="E"):
    """The four reference outputs: `steps` and `runs`, each as text and as `--json`."""
    reports = {}
    for extra in ((), ("--json",)):
        reports["steps", extra] = _steps(world, world["R"], world[home], *extra)
        reports["runs", extra] = _runs(world, world["R"], world[home], *extra)
    return reports


def test_both_reports_use_the_same_sentence(world):
    reports = _reference_reports(world)

    for (name, extra), done in reports.items():
        assert (done.returncode, done.stderr) == (0, ""), (name, extra)
        if extra:
            assert json.loads(done.stdout)["span"] == SPAN_LINE, (name, extra)
        else:
            assert done.stdout.splitlines().count(SPAN_LINE) == 1, (name, extra)


def test_all_four_reference_outputs_match_the_pattern_and_json_has_no_cost_word(world):
    for (name, extra), done in _reference_reports(world).items():
        matching = [line for line in done.stdout.splitlines() if SPAN_PATTERN.search(line)]
        assert matching, (name, extra)
        if extra:
            assert "cost" not in done.stdout.lower(), (name, extra)


def test_steps_statement_is_present_once_with_a_stream_pointer_under_h(world):
    text = _steps(world, world["R"], world["H"]).stdout
    document = json.loads(_steps(world, world["R"], world["H"], "--json").stdout)

    lines = text.splitlines()
    assert lines[1] == SPAN_LINE and lines.count(SPAN_LINE) == 1
    assert list(document)[:3] == ["fallback", "span", "runs"]


def test_steps_non_zero_exits_print_no_report_and_no_statement(world, tmp_path):
    cases = (
        ("steps", "--projects-root", str(world["R"]), "--wf", "wf_nope"),
        ("steps", "--projects-root", str(tmp_path / "absent"), "--wf", "wf_nope"),
        ("steps", "--no-such-option"),
        ("steps", "--projects-root", str(world["R"]), "--json", "--wf", "wf_nope"),
    )
    for argv in cases:
        done = _command(world, world["E"], *argv)

        assert done.returncode != 0, argv
        assert done.stdout == "", argv
        assert SPAN_LINE not in done.stderr, argv


# ── A response found in more than one transcript is counted once (CP-573 to CP-588) ──


def _assistant_line(response_id: str, input_tokens: int, output_tokens: int, stamp: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": f"2026-10-01T{stamp}.000Z",
        "message": {
            "id": response_id,
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    })


def _two_runs_sharing_a_response(root: Path) -> tuple[Path, Path]:
    """Example 1: runs `s-a` and `s-b` both hold `msg_shared`. Returns the projects root and the
    listing naming `s-a` then `s-b`."""
    projects = root / "shared-projects"
    (projects / "proj").mkdir(parents=True)
    (projects / "proj" / "s-a.jsonl").write_text(
        _assistant_line("msg_a", 100, 10, "10:00:00") + "\n"
        + _assistant_line("msg_shared", 7, 3, "10:00:30") + "\n"
    )
    (projects / "proj" / "s-b.jsonl").write_text(
        _assistant_line("msg_shared", 7, 3, "10:00:30") + "\n"
        + _assistant_line("msg_b", 200, 20, "10:05:00") + "\n"
    )
    return projects, _agents_json(root, "s-a", "s-b")


def test_runs_command_counts_a_shared_response_once_in_text_and_json(world, tmp_path):
    from ka_telemetry.run_usage import shared_responses_clause

    projects, listing = _two_runs_sharing_a_response(tmp_path)

    text = _runs(world, projects, world["E"], agents_json=listing)
    payload = _json_of(_runs(world, projects, world["E"], "--json", agents_json=listing))

    assert text.returncode == 0 and text.stderr == ""
    assert "  started=None  span=30s\n  claude-sonnet-5: input=107 output=13 " in text.stdout
    assert "  started=None  span=0s\n  claude-sonnet-5: input=200 output=20 " in text.stdout
    assert _coverage_lines(text.stdout) == [
        "Coverage: 2 read / 0 unreadable / 2 total (2 listed, 0 named) — "
        + shared_responses_clause(1)
    ]
    assert payload["coverage"]["responses_in_more_than_one_transcript"] == 1
    assert [row["span_seconds"] for row in payload["rows"]] == [30, 0]


def test_runs_reference_outputs_are_byte_identical(world):
    from test_parity import adjusted_runs_json, adjusted_runs_table

    named = [
        arg for session in json.loads((PARITY / "named_sessions.json").read_text())
        for arg in ("--session", session)
    ]
    text = _runs(world, world["R"], world["E"], *named)
    as_json = _runs(world, world["R"], world["E"], *named, "--json")

    assert (text.returncode, as_json.returncode) == (0, 0), (text.stderr, as_json.stderr)
    assert text.stdout == adjusted_runs_table()
    assert as_json.stdout == adjusted_runs_json()
    assert "found in more than one" not in text.stdout
    assert "responses_in_more_than_one_transcript" not in as_json.stdout


def _two_step_rows_sharing_a_response(root: Path) -> Path:
    """Example 4: agents `a1` (step-x) and `a2` (step-y) of one workflow run both hold
    `msg_shared`. Returns the projects root."""
    projects = root / "shared-steps"
    run_dir = projects / "proj" / "sid-1" / "subagents" / "workflows" / "wf_1"
    run_dir.mkdir(parents=True)
    journal = []
    for agent_id, step in (("a1", "step-x"), ("a2", "step-y")):
        journal.append(
            json.dumps({"type": "started", "key": f"v2:{agent_id}", "agentId": agent_id})
        )
        journal.append(json.dumps({
            "type": "result", "key": f"v2:{agent_id}", "agentId": agent_id,
            "result": {"step": step, "status": "ok", "artifact": f"{step}.md"},
        }))
    (run_dir / "journal.jsonl").write_text("\n".join(journal) + "\n")
    (run_dir / "agent-a1.jsonl").write_text(
        _assistant_line("msg_1", 50, 5, "11:00:00") + "\n"
        + _assistant_line("msg_shared", 7, 3, "11:00:10") + "\n"
    )
    (run_dir / "agent-a2.jsonl").write_text(
        _assistant_line("msg_shared", 7, 3, "11:00:10") + "\n"
        + _assistant_line("msg_2", 60, 6, "11:02:00") + "\n"
    )
    return projects


def test_steps_command_counts_a_shared_response_once_and_states_how_many(world, tmp_path):
    from ka_telemetry.run_usage import shared_responses_clause

    projects = _two_step_rows_sharing_a_response(tmp_path)

    text = _steps(world, projects, world["E"])
    payload = _json_of(_steps(world, projects, world["E"], "--json"))

    assert text.returncode == 0 and text.stderr == ""
    assert "    span=10s\n    claude-sonnet-5: input=57 output=8 " in text.stdout
    assert "    span=0s\n    claude-sonnet-5: input=60 output=6 " in text.stdout
    assert _coverage_lines(text.stdout)[0].endswith(" — " + shared_responses_clause(1))
    assert payload["runs"][0]["coverage"]["responses_in_more_than_one_transcript"] == 1
    assert [row["span_seconds"] for row in payload["runs"][0]["rows"]] == [10, 0]


def test_steps_reference_outputs_stay_byte_identical_on_the_real_command(world):
    text = _steps(world, world["R"], world["E"])
    as_json = _steps(world, world["R"], world["E"], "--json")

    assert text.stdout == (PARITY / "expected" / "steps.txt").read_text()
    assert as_json.stdout == (PARITY / "expected" / "steps.json").read_text()
