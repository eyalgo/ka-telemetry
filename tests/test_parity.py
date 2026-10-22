"""Tests over the recorded parity data set in `tests/parity/`: its shape, its provenance and the
reference outputs dev's readers produced over it.

The set was recorded once, from the owner's machine, and reduced to the fields dev's three
readers read before any reference output was generated. These tests never regenerate it.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from e2e.support import api_request_body, child_env, needs_history

from ka_telemetry.transcript_notes import SPAN_LINE

if TYPE_CHECKING:
    from conftest import InstalledProduct

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
PARITY = Path(__file__).resolve().parent / "parity"
PROJECTS = PARITY / "projects"

DOUBLY_NAMED = ("fac96b52-a71e-45e1-bec2-aa7a00d8b2a6", "f727de4b-eb69-446a-989b-493cb10e2e4f")
WORKFLOW_PARENT_SESSION = "174d0111-bfa9-4539-92d9-5560e0ab4c36"
EXCLUDED_RUN_LOGS = (
    "claude-self-answer-tracker17-SDD-20261006-152522-33858-plan.jsonl",
    "claude-self-answer-tracker17-SDD-20261006-195517-66728-plan.jsonl",
)
USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation",
    "output_tokens_details",
}
RAW_OUTPUTS = (
    "expected/runs.txt",
    "expected/runs.json",
    "expected/steps.txt",
    "expected/steps.json",
    "expected/context_sizes.json",
)


def _load(name: str):
    return json.loads((PARITY / name).read_text())


def _jsonl(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            assert isinstance(record, dict), f"{path}: a line that is not a JSON object"
            records.append(record)
    return records


def _session_transcripts() -> dict[str, Path]:
    return {p.stem: p for p in sorted(PROJECTS.glob("*/*.jsonl"))}


def _assistant_usage_records(path: Path) -> list[dict]:
    return [
        r
        for r in _jsonl(path)
        if r.get("type") == "assistant" and (r.get("message") or {}).get("usage")
    ]


def _data_files() -> list[Path]:
    """Every recorded input: the transcripts tree, the listing and the named-session list."""
    return sorted(
        [p for p in PROJECTS.rglob("*") if p.is_file()]
        + [PARITY / "agents.json", PARITY / "named_sessions.json"]
    )


# ── the data set holds every case CP-19 names ──────────────────────────────


def test_every_required_case_is_present():
    listing = _load("agents.json")
    named = _load("named_sessions.json")
    transcripts = _session_transcripts()
    listed_ids = {
        r["sessionId"] for r in listing if r.get("kind") != "interactive" and "sessionId" in r
    }

    assert listed_ids & transcripts.keys(), "a listed run"
    assert (set(named) - listed_ids) & transcripts.keys(), "a named run that is not listed"
    assert listed_ids & set(named), "a session both listed and named"
    for session_id in DOUBLY_NAMED:
        assert named.count(session_id) == 2, f"{session_id} named by two run logs"
    assert set(named) - transcripts.keys(), "a named session whose transcript is not in the set"
    assert any(r.get("kind") == "interactive" for r in listing), "an interactive listing row"
    assert any(
        r.get("kind") != "interactive" and "sessionId" not in r for r in listing
    ), "a non-interactive row with no sessionId"

    def split_response(path: Path) -> bool:
        ids = [r["message"].get("id") for r in _assistant_usage_records(path)]
        return any(ids.count(i) > 1 for i in set(ids) if i)

    assert any(
        split_response(p) for p in transcripts.values()
    ), "one message.id over several records"
    assert any(not _assistant_usage_records(p) for p in transcripts.values()), "no assistant usage"

    # Which agent is a fallback is dev's own call, so it is read from dev's stored output rather
    # than re-derived from the journal here.
    rows = [row for run in _load("expected/steps.json")["runs"] for row in run["rows"]]
    assert any(not row["is_fallback"] for row in rows), "a workflow run with a step row"
    assert any(row["is_fallback"] for row in rows), "a workflow run with a fallback row"


def test_named_sessions_keep_the_ordered_repeated_id_list():
    named = _load("named_sessions.json")
    manifest = _load("manifest.json")

    assert isinstance(named, list) and all(isinstance(s, str) for s in named)
    assert named == [entry["session_id"] for entry in manifest["run_logs"]]
    log_names = [entry["log"] for entry in manifest["run_logs"]]
    assert log_names == sorted(log_names)
    assert len(named) > len(set(named))  # an id is repeated where two logs name it


# ── the data set holds no more than dev's readers read ──────────────────────


def _check_transcript_record(record: dict, where: str):
    assert set(record) <= {"type", "timestamp", "message"}, where
    message = record.get("message", {})
    assert set(message) <= {"id", "model", "usage"}, where
    if "usage" in message:
        assert set(message["usage"]) <= USAGE_FIELDS, where
        assert set(message["usage"].get("cache_creation", {})) <= {
            "ephemeral_1h_input_tokens",
            "ephemeral_5m_input_tokens",
        }, where
        assert set(message["usage"].get("output_tokens_details", {})) <= {"thinking_tokens"}, where


def _check_journal_record(record: dict, where: str):
    assert set(record) <= {"agentId", "type", "result"}, where
    payload = record.get("result", "")
    if isinstance(payload, dict):
        assert set(payload) <= {"step", "status", "result"}, where
        assert all(isinstance(v, str) for v in payload.values()), where
    else:
        assert payload == "", f"{where}: a plain-text payload is conversation text"


def test_records_hold_only_the_allowed_fields():
    checked = {"transcript": 0, "journal": 0, "meta": 0, "listing": 0}
    for path in sorted(p for p in PROJECTS.rglob("*") if p.is_file()):
        where = str(path.relative_to(PARITY))
        if path.name == "journal.jsonl":
            kind, check, records = "journal", _check_journal_record, _jsonl(path)
        elif path.name.endswith(".meta.json"):
            record = json.loads(path.read_text())
            assert set(record) <= {"agentType", "model"}, where
            checked["meta"] += 1
            continue
        elif path.suffix == ".jsonl":
            kind, check, records = "transcript", _check_transcript_record, _jsonl(path)
        else:
            pytest.fail(f"{where}: a file of a kind no reader reads")
        for record in records:
            check(record, where)
        checked[kind] += len(records)
    for row in _load("agents.json"):
        assert set(row) <= {"kind", "sessionId", "name", "cwd", "state", "startedAt"}
        checked["listing"] += 1
    assert all(checked.values()), checked


def test_no_conversation_text_anywhere():
    marker = '"' + "content" + '"'
    files = sorted(p for p in PARITY.rglob("*") if p.is_file())
    assert files
    for path in files:
        assert marker not in path.read_text(), str(path.relative_to(PARITY))


# ── provenance and the stored outputs ──────────────────────────────────────


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), *args], capture_output=True, text=True, check=False
    )


def test_manifest_names_source_copy_inputs_and_commands():
    needs_history()
    manifest = _load("manifest.json")

    assert re.fullmatch(r"[0-9a-f]{40}", manifest["dev_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["copy_commit"])
    copy_message = _git("log", "-1", "--format=%B", manifest["copy_commit"])
    assert copy_message.returncode == 0 and manifest["dev_commit"] in copy_message.stdout
    assert _git("merge-base", "--is-ancestor", manifest["copy_commit"], "HEAD").returncode == 0

    recorded = {str(p.relative_to(PARITY)) for p in _data_files()}
    assert set(manifest["inputs"]) == recorded
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in manifest["inputs"].values())

    assert all(set(entry) == {"log", "session_id"} for entry in manifest["run_logs"])
    assert tuple(manifest["excluded_run_logs"]) == EXCLUDED_RUN_LOGS
    assert not {e["log"] for e in manifest["run_logs"]} & set(EXCLUDED_RUN_LOGS)

    produced = {entry["output"] for entry in manifest["commands"]}
    assert produced == set(RAW_OUTPUTS)
    for entry in manifest["commands"]:
        assert entry["command"] and entry["cwd"] and entry["source"], entry["output"]


def test_stored_outputs_exist_for_both_forms():
    for name in RAW_OUTPUTS:
        assert (PARITY / name).is_file(), name
    assert (PARITY / "expected/runs.txt").read_text().startswith("Population: ")
    assert {"coverage", "rows", "population"} <= set(_load("expected/runs.json"))
    assert (PARITY / "expected/steps.txt").read_text().startswith("Fallback: ")
    assert {"fallback", "runs"} <= set(_load("expected/steps.json"))

    sizes = _load("expected/context_sizes.json")
    assert set(sizes) == set(_session_transcripts())
    assert all(value is None or isinstance(value, int) for value in sizes.values())
    assert None in sizes.values() and any(isinstance(v, int) and v > 0 for v in sizes.values())


def test_input_hashes_match_the_recorded_files():
    manifest = _load("manifest.json")

    for path in _data_files():
        relative = str(path.relative_to(PARITY))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == manifest["inputs"][relative], relative


# ── the package's commands and readers against dev's stored outputs ────────
#
# The expected side below is written without the package's renderers: it edits dev's stored text
# in the four places the change's contract allows and nowhere else, and the comparison is then
# an equality over text.

POPULATION_NOW = (
    'Population: background runs only (rows with kind != "interactive"), and — with --session — '
    "every run named by its session id."
)
UNNAMED_FIELDS = ("name", "cwd", "state")
NO_COST_LINE = (
    "This report carries no cost, because ka-telemetry never computes a cost from tokens."
)
# What the stored expectations held before the no-cost line was added, so a test can show that the
# line is the only thing that changed in the text, and that the JSON was not touched at all.
RUNS_TXT_BEFORE_NO_COST_LINE_SHA256 = (
    "14e8e8d05fe5c180c754e5c75f13ac56f10a858e327e95023ccbccd99012d112"
)
RUNS_JSON_SHA256 = "87636cdc8088d628a95014a35388ea7bca4ded87ad7fa0733465ca9cf0cad484"
STEPS_TXT_BEFORE_NO_COST_LINE_SHA256 = (
    "4b79370168fbe7877992a9d1af0cfcc42fb33fccb7f4dbdb835917054e0e0dce"
)
STEPS_JSON_SHA256 = "a0531409340651a32ebb18b67c778428c0886e86c4a70671dae85e0e3a538809"


def _listed_session_ids() -> list[str]:
    """The sessions the curated listing carries, once each, in listing order."""
    ids: list[str] = []
    for row in _load("agents.json"):
        session_id = row.get("sessionId")
        if row.get("kind") != "interactive" and session_id and session_id not in ids:
            ids.append(session_id)
    return ids


def _stored(name: str) -> str:
    return (PARITY / name).read_text()


def _without_statement(stored: str) -> str:
    """A stored expectation with the one statement of what the time covers taken out and nothing
    else changed: the text form's whole line, or the JSON form's `span` member line. The surviving
    bytes, their order and the trailing newline are as stored."""
    statement_lines = (f"{SPAN_LINE}\n", f'  "span": {json.dumps(SPAN_LINE)},\n')
    return "".join(
        line for line in stored.splitlines(keepends=True) if line not in statement_lines
    )


def _dev_table_blocks(text: str) -> tuple[list[str], list[list[str]]]:
    """Dev's table as its three header lines and its row blocks, each block a list of lines."""
    lines = text.rstrip("\n").split("\n")
    header, rest = lines[:3], lines[4:]
    blocks: list[list[str]] = [[]]
    for line in rest:
        if line == "":
            blocks.append([])
        else:
            blocks[-1].append(line)
    return header, [b for b in blocks if b]


def _session_of_block(block: list[str]) -> str:
    return block[0].split("  ", 1)[0]


GONE = "gone"


def adjusted_runs_table(text: str | None = None, listed: list[str] | None = None) -> str:
    text = _without_statement(_stored("expected/runs.txt")) if text is None else text
    # the one line this package adds under the table; dev printed none
    body, _, note = text.rstrip("\n").rpartition("\n")
    header, blocks = _dev_table_blocks(body if note == NO_COST_LINE else text)
    match = re.fullmatch(
        r"Coverage: (\d+) read / (\d+) unreadable / (\d+) total( — unreadable: (.*))?", header[1]
    )
    assert match, header[1]
    listed = _listed_session_ids() if listed is None else listed
    kept, seen = [], set()
    for block in blocks:
        session_id = _session_of_block(block)
        if session_id in seen:
            continue
        seen.add(session_id)
        if session_id not in listed:
            # a row dev took from a run log: name, cwd and state are absent in the package
            block = [f"{session_id}  (unnamed)  cwd=None  state=None", *block[1:]]
        kept.append(block)

    removed = len(blocks) - len(kept)
    dev_total = int(match.group(3))
    ids = list(dict.fromkeys(match.group(5).split(", "))) if match.group(5) else []
    unreadable = len(ids)
    removed_unreadable = int(match.group(2)) - unreadable
    total = dev_total - removed - removed_unreadable
    read = total - unreadable
    coverage = (
        f"Coverage: {read} read / {unreadable} unreadable / {total} total "
        f"({len(listed)} listed, {total - len(listed)} named)"
    )
    if ids:
        coverage += f" — unreadable: {', '.join(f'{i} ({GONE})' for i in ids)}"
    lines = [POPULATION_NOW, SPAN_LINE, coverage, header[2], ""]
    for block in kept:
        lines += [*block, ""]
    return "\n".join(lines) + (f"{NO_COST_LINE}\n" if note == NO_COST_LINE else "")


def adjusted_runs_json() -> str:
    stored = json.loads(_stored("expected/runs.json"))
    del stored["span"]
    listed = _listed_session_ids()
    document = copy.deepcopy(stored)

    seen, rows = set(), []
    for row in stored["rows"]:
        if row["session_id"] in seen:
            continue
        seen.add(row["session_id"])
        if row["session_id"] not in listed:
            for field in UNNAMED_FIELDS:
                row[field] = None
        rows.append(row)

    coverage = stored["coverage"]
    ids = list(dict.fromkeys(coverage["unreadable_ids"]))
    unreadable = len(ids)
    total = coverage["total"] - (len(stored["rows"]) - len(rows)) - (
        coverage["unreadable"] - unreadable
    )
    document["population"] = POPULATION_NOW
    document["rows"] = rows
    figures = {
        **coverage,
        "read": total - unreadable,
        "unreadable": unreadable,
        "total": total,
        "unreadable_ids": ids,
        "listed": len(listed),
        "named": total - len(listed),
    }
    document["coverage"] = {}
    for key, value in figures.items():
        document["coverage"][key] = value
        if key == "unreadable_ids" and ids:
            # the transcripts the reference named are not in this data set: their reason is gone
            document["coverage"]["unreadable_reasons"] = [GONE] * len(ids)
    members = {key: value for key, value in document.items() if key != "population"}
    ordered = {"population": document["population"], "span": SPAN_LINE, **members}
    return json.dumps(ordered, indent=2) + "\n"


def shown_diff(expected: str, actual: str, label: str) -> str:
    """The difference between two texts, shown as a diff; empty when they are equal."""
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"dev: {label}",
            tofile=f"ka-telemetry: {label}",
        )
    )


@pytest.fixture(scope="module")
def parity_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The `HOME` every parity command and library comparison runs under: a fresh, empty
    directory, so the suite's verdict never depends on what the running user's own
    `~/.ka-telemetry` holds."""
    return tmp_path_factory.mktemp("parity-home")


@pytest.fixture(scope="module")
def commands(installed: InstalledProduct, parity_home: Path):
    """What the installed command prints over the recorded data set, for every form."""

    def run(*argv: str) -> str:
        done = subprocess.run(
            [str(installed.command), *argv],
            capture_output=True,
            text=True,
            check=False,
            env=child_env(parity_home),
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    named = [arg for session in _load("named_sessions.json") for arg in ("--session", session)]
    runs = ("runs", "--agents-json", str(PARITY / "agents.json"), "--projects-root", str(PROJECTS))
    steps = ("steps", "--projects-root", str(PROJECTS))
    return {
        "runs.txt": run(*runs, *named),
        "runs.json": run(*runs, *named, "--json"),
        "steps.txt": run(*steps),
        "steps.json": run(*steps, "--json"),
        "run": run,
        "home": parity_home,
        "runs_argv": (*runs, *named),
    }


def test_steps_output_equals_dev_in_both_forms_byte_for_byte(commands):
    assert commands["steps.txt"] == _stored("expected/steps.txt")
    assert commands["steps.json"] == _stored("expected/steps.json")


def test_runs_json_equals_dev_after_the_four_adjustments(commands):
    expected = adjusted_runs_json()
    difference = shown_diff(expected, commands["runs.json"], "runs --json")
    assert commands["runs.json"] == expected, difference


def test_runs_table_equals_dev_line_for_line_after_the_four_adjustments(commands):
    expected = adjusted_runs_table()
    assert commands["runs.txt"] == expected, shown_diff(expected, commands["runs.txt"], "runs")


def test_parity_commands_run_under_an_empty_home(commands):
    home = commands["home"]

    assert child_env(home)["HOME"] == str(home)
    assert home.is_dir()
    assert list(home.iterdir()) == []


def test_parity_verdict_ignores_the_running_users_stream(commands, tmp_path, monkeypatch):
    outer = tmp_path / "outer-home"
    stream = outer / ".ka-telemetry"
    stream.mkdir(parents=True)
    sessions = [*_listed_session_ids(), WORKFLOW_PARENT_SESSION]
    records = [{"body": api_request_body(session_id, 0.5)} for session_id in sessions]
    (stream / "logs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    monkeypatch.setenv("HOME", str(outer))

    assert commands["run"](*commands["runs_argv"]) == commands["runs.txt"]
    assert commands["run"]("steps", "--projects-root", str(PROJECTS)) == commands["steps.txt"]


def test_runs_parity_expectations_gain_the_statement_once_as_the_second_line_and_member():
    text = _stored("expected/runs.txt").splitlines()
    members = list(json.loads(_stored("expected/runs.json")))

    assert text[1] == SPAN_LINE and text.count(SPAN_LINE) == 1
    assert members[:2] == ["population", "span"]
    assert json.loads(_stored("expected/runs.json"))["span"] == SPAN_LINE
    assert _stored("expected/runs.json").count('"span":') == 1


def test_steps_parity_expectations_gain_the_statement_once_as_the_second_line_and_member():
    text = _stored("expected/steps.txt").splitlines()
    members = list(json.loads(_stored("expected/steps.json")))

    assert text[1] == SPAN_LINE and text.count(SPAN_LINE) == 1
    assert members[:2] == ["fallback", "span"]
    assert json.loads(_stored("expected/steps.json"))["span"] == SPAN_LINE
    assert _stored("expected/steps.json").count('"span":') == 1


def test_runs_parity_text_expectation_gains_only_the_no_cost_line():
    stored = _without_statement(_stored("expected/runs.txt"))
    lines = stored.splitlines(keepends=True)
    before = "".join(lines[:-1])

    assert lines[-1] == f"{NO_COST_LINE}\n"
    assert hashlib.sha256(before.encode()).hexdigest() == RUNS_TXT_BEFORE_NO_COST_LINE_SHA256
    assert "ka-telemetry agents" not in stored
    stored_json = _without_statement(_stored("expected/runs.json"))
    assert hashlib.sha256(stored_json.encode()).hexdigest() == RUNS_JSON_SHA256


def _without_step_spans(stored: str) -> str:
    """A stored `steps` expectation with the span this change adds taken out and nothing else
    changed: the text form's `    span=` lines, or the JSON form's `span_seconds` member lines
    (the first member of a row, so each ends in a comma). The surviving bytes, their indentation,
    member order and trailing newline are as stored."""
    unstated = _without_statement(stored)
    without_lines = re.sub(r"^    span=[^\n]*\n", "", unstated, flags=re.MULTILINE)
    return re.sub(r'^ *"span_seconds": [^\n]*,\n', "", without_lines, flags=re.MULTILINE)


def test_steps_parity_text_expectation_gains_only_the_no_cost_line():
    stored = _without_step_spans(_stored("expected/steps.txt"))
    lines = stored.splitlines(keepends=True)
    before = "".join(lines[:-1])

    assert lines[-1] == f"{NO_COST_LINE}\n"
    assert hashlib.sha256(before.encode()).hexdigest() == STEPS_TXT_BEFORE_NO_COST_LINE_SHA256
    assert "ka-telemetry agents" not in stored
    stored_json = _without_step_spans(_stored("expected/steps.json"))
    assert hashlib.sha256(stored_json.encode()).hexdigest() == STEPS_JSON_SHA256


def test_context_size_equals_dev_for_every_transcript(commands):
    stored = _load("expected/context_sizes.json")
    assert set(stored) == set(_session_transcripts())

    for session_id, transcript in _session_transcripts().items():
        printed = json.loads(commands["run"]("context", "--json", str(transcript)))
        assert printed["context_size"] == stored[session_id], session_id
    assert None in stored.values()


def test_library_readers_equal_the_command_output(commands, monkeypatch):
    from ka_telemetry import context_gauge, flow_usage, run_usage

    monkeypatch.setenv("HOME", str(commands["home"]))
    reader = run_usage.build_reader(PROJECTS)
    ledger = run_usage.read_runs(
        (PARITY / "agents.json").read_text(), _load("named_sessions.json"), reader
    )
    assert run_usage.render(ledger) == commands["runs.txt"]
    assert run_usage.render_json(ledger) + "\n" == commands["runs.json"]

    steps_reader = flow_usage.build_reader(PROJECTS)
    readings = [
        flow_usage.read_run(run, steps_reader.files)
        for run in flow_usage.flow_runs(steps_reader.projects_root, steps_reader.files)
    ]
    assert flow_usage.render(readings) == commands["steps.txt"]
    assert flow_usage.render_json(readings) + "\n" == commands["steps.json"]

    stored = _load("expected/context_sizes.json")
    for session_id, transcript in _session_transcripts().items():
        assert context_gauge.current_context(transcript, run_usage.Files()) == stored[session_id]


def test_duplicate_session_keeps_the_first_and_reduces_counts(commands):
    stored = _load("expected/runs.json")
    shown = json.loads(commands["runs.json"])
    doubled = [s for s in DOUBLY_NAMED if sum(r["session_id"] == s for r in stored["rows"]) == 2]
    assert doubled == list(DOUBLY_NAMED)

    for session_id in DOUBLY_NAMED:
        assert sum(r["session_id"] == session_id for r in shown["rows"]) == 1
        first = next(r for r in stored["rows"] if r["session_id"] == session_id)
        kept = next(r for r in shown["rows"] if r["session_id"] == session_id)
        assert (kept["span_seconds"], kept["spend"]) == (first["span_seconds"], first["spend"])

    removed = len(stored["rows"]) - len(shown["rows"])
    assert removed == len(DOUBLY_NAMED)
    assert shown["coverage"]["total"] == stored["coverage"]["total"] - removed
    assert shown["coverage"]["read"] == stored["coverage"]["read"] - removed
    assert shown["coverage"]["unreadable"] == stored["coverage"]["unreadable"]
    assert shown["coverage"]["unreadable_ids"] == stored["coverage"]["unreadable_ids"]


def test_an_off_by_one_figure_fails_and_shows_a_diff(commands):
    stored_before = {n: hashlib.sha256((PARITY / n).read_bytes()).hexdigest() for n in RAW_OUTPUTS}
    printed = commands["runs.txt"]
    figure = re.search(r"output=(\d+)", printed)
    assert figure
    off_by_one = printed.replace(figure.group(0), f"output={int(figure.group(1)) + 1}", 1)

    expected = adjusted_runs_table()
    assert off_by_one != expected
    difference = shown_diff(expected, off_by_one, "runs")
    assert "\n-  " in difference and figure.group(0) in difference
    assert f"output={int(figure.group(1)) + 1}" in difference

    stored_after = {n: hashlib.sha256((PARITY / n).read_bytes()).hexdigest() for n in RAW_OUTPUTS}
    assert stored_after == stored_before


def test_spend_and_span_are_never_normalized():
    dev_table = _stored("expected/runs.txt").rstrip("\n").rpartition("\n")[0]  # without the note
    stored_blocks = _dev_table_blocks(dev_table)[1]
    _, kept = _dev_table_blocks(adjusted_runs_table().rstrip("\n").rpartition("\n")[0])
    first_seen = {}
    for block in stored_blocks:
        first_seen.setdefault(_session_of_block(block), block)

    assert [_session_of_block(b) for b in kept] == list(first_seen)
    for block in kept:
        assert block[1:] == first_seen[_session_of_block(block)][1:]

    stored_json = json.loads(_stored("expected/runs.json"))["rows"]
    adjusted = json.loads(adjusted_runs_json())["rows"]
    firsts = {r["session_id"]: r for r in reversed(stored_json)}
    for row in adjusted:
        assert (row["span_seconds"], row["spend"], row["started_at"]) == (
            firsts[row["session_id"]]["span_seconds"],
            firsts[row["session_id"]]["spend"],
            firsts[row["session_id"]]["started_at"],
        )


def test_table_adapter_reduces_duplicate_unreadable_counts_without_changing_read_rows():
    raw = (
        "Population: reference\n"
        "Coverage: 1 read / 3 unreadable / 4 total — unreadable: missing-b, missing-a, missing-b\n"
        "Dropped (no sessionId): 0\n\n"
        "readable  log-name  cwd=/reference  state=done\n"
        "  started=None  span=42s\n"
        "  model: input=7 output=11 cache_read=13 cache_write_1h=17 cache_write_5m=19\n"
    )
    expected = (
        f"{POPULATION_NOW}\n"
        f"{SPAN_LINE}\n"
        "Coverage: 1 read / 2 unreadable / 3 total (0 listed, 3 named) — "
        "unreadable: missing-b (gone), missing-a (gone)\n"
        "Dropped (no sessionId): 0\n\n"
        "readable  (unnamed)  cwd=None  state=None\n"
        "  started=None  span=42s\n"
        "  model: input=7 output=11 cache_read=13 cache_write_1h=17 cache_write_5m=19\n"
    )

    assert adjusted_runs_table(raw, []) == expected
