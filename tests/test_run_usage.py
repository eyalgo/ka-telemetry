"""Tests for `run_usage.py` — what a background run used, read off its own transcript.

Fixtures below are shaped off the live disk, not invented: `claude agents --json --all` field
names and the `<synthetic>`/thinking-tokens shapes were read from this machine's own
`~/.claude/projects/*/*.jsonl` on 2026-09-04, the same day the plan was written.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import json
import os
import re
from pathlib import Path

import pytest
from e2e.support import api_request_body
from test_transcript_notes import FakeStreamFiles

from ka_telemetry import run_usage, stream_usage, transcript_notes

NO_COST = "This report carries no cost, because ka-telemetry never computes a cost from tokens."
SPAN = transcript_notes.SPAN_LINE


def _pointer(session_id):
    return (
        f"Run `ka-telemetry agents --where session.id={session_id}` to see Claude Code's cost "
        f"estimate for session {session_id}."
    )


@pytest.fixture(autouse=True)
def _empty_home(tmp_path_factory, monkeypatch):
    """Every test runs under an empty `HOME`, so no test's output depends on a telemetry stream
    the running user's own `~/.ka-telemetry` happens to hold. A test that wants a stream sets
    `HOME` itself, after this."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("empty-home")))


# ── Reader ports (this station) — the two ports, and one composition root ──
#
# `core/doctrine/engineering/languages/python/conventions.md` § Constructs: "A module is given
# its collaborators; it constructs none." These tests hold that against `run_usage.py` itself —
# by effect, via `ast`/`inspect`, never by re-deriving what the ports do from their own bodies.


class TestPortsAndTheCompositionRoot:
    def test_files_finds_transcripts_matching_a_session_id_under_a_root(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        transcript = project / "sid-1.jsonl"
        transcript.write_text("")
        files = run_usage.Files()
        assert files.find_transcripts("sid-1", tmp_path) == [transcript]

    def test_files_find_transcripts_on_a_root_with_no_match_is_empty(self, tmp_path):
        (tmp_path / "proj").mkdir()
        files = run_usage.Files()
        assert files.find_transcripts("no-such-sid", tmp_path) == []

    def test_files_find_transcripts_on_a_nonexistent_root_is_empty_not_raising(self, tmp_path):
        files = run_usage.Files()
        assert files.find_transcripts("any-sid", tmp_path / "does-not-exist") == []

    def test_files_read_lines_returns_the_lines_of_a_file(self, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("a\nb\n")
        files = run_usage.Files()
        assert files.read_lines(path) == ["a", "b"]

    def test_files_read_lines_returns_none_for_a_missing_file(self, tmp_path):
        files = run_usage.Files()
        assert files.read_lines(tmp_path / "does-not-exist") is None

    def test_processes_run_returns_stdout_on_success(self):
        out = run_usage.Processes().run(["python3", "-c", "print('hi')"])
        assert out == "hi\n"

    def test_processes_run_raises_system_exit_naming_code_and_stderr_on_nonzero_exit(self):
        with pytest.raises(SystemExit) as exc_info:
            run_usage.Processes().run(
                ["python3", "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]
            )
        message = str(exc_info.value)
        assert "3" in message
        assert "boom" in message

    def test_files_exposes_find_transcripts_read_lines_and_read_outcome_only(self):
        # "finding the transcripts matching a session id under a root, and reading a file's
        # lines. Nothing else." `read_outcome` is the same read reporting why it failed, so it
        # is the one sanctioned addition. A further capability would be a world-reach nothing
        # sanctioned, and no other test in this file would notice one arriving. Run-log
        # discovery, the third capability the copied reader had, is gone: runs are named by
        # session id instead.
        assert {name for name in vars(run_usage.Files) if not name.startswith("_")} == {
            "find_transcripts",
            "read_lines",
            "read_outcome",
        }

    def test_processes_exposes_exactly_one_capability(self):
        assert {name for name in vars(run_usage.Processes) if not name.startswith("_")} == {"run"}

    def test_no_module_level_name_holds_the_expanded_projects_root(self):
        # The default root is resolved "not in a unit body and not as an argparse default" —
        # and an argparse default is filled from a module constant evaluated at import, which
        # is the shape this replaced. By effect: importing the module expands no home.
        default_root = Path.home() / ".claude" / "projects"
        holders = [
            name
            for name, value in vars(run_usage).items()
            if value == default_root or value == str(default_root)
        ]
        assert holders == []

    def test_build_reader_resolves_the_default_projects_root(self):
        reader = run_usage.build_reader()
        assert reader.projects_root == Path.home() / ".claude" / "projects"
        assert isinstance(reader.files, run_usage.Files)
        assert isinstance(reader.processes, run_usage.Processes)

    def test_build_reader_honors_an_explicit_projects_root(self, tmp_path):
        reader = run_usage.build_reader(tmp_path)
        assert reader.projects_root == tmp_path

    def test_only_build_reader_constructs_a_files_or_a_processes(self):
        # By effect, not by name: walk every function body in the module and record which one
        # names `Files(` or `Processes(` as a call — `build_reader` should be the only hit.
        source = Path(run_usage.__file__).read_text()
        tree = ast.parse(source)
        constructing_functions = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                        if call.func.id in ("Files", "Processes"):
                            constructing_functions.add(node.name)
        assert constructing_functions == {"build_reader"}

    def test_main_obtains_ports_from_build_reader_and_constructs_none_itself(self):
        source = inspect.getsource(run_usage.main)
        assert "build_reader(" in source
        assert "Files(" not in source
        assert "Processes(" not in source


# ── Task 1 — the population is named, and interactive is out of it ────────


def _agents_json(*rows: dict) -> str:
    return json.dumps(list(rows))


def _row(**overrides) -> dict:
    row = {
        "id": "abc123",
        "cwd": "/home/user/projects/kensai/example-host",
        "kind": "background",
        "startedAt": 1788504031568,
        "sessionId": "4ec3eb2b-7c27-4633-b057-1a5c83257e9f",
        "name": "some run",
        "state": "done",
    }
    row.update(overrides)
    return row


def test_interactive_row_is_excluded_background_row_is_kept():
    text = _agents_json(
        _row(kind="interactive", sessionId="s-interactive"),
        _row(kind="background", sessionId="s-background"),
    )
    parsed = run_usage.runs_from(text)
    assert [r.session_id for r in parsed.runs] == ["s-background"]
    assert parsed.interactive_excluded == 1


def test_row_with_no_name_is_kept_not_skipped():
    text = _agents_json(_row(sessionId="s-1"))
    del_row = json.loads(text)
    del del_row[0]["name"]
    text = json.dumps(del_row)
    parsed = run_usage.runs_from(text)
    assert len(parsed.runs) == 1
    assert parsed.runs[0].name is None


def test_row_missing_session_id_is_dropped_and_counted():
    rows = json.loads(_agents_json(_row()))
    del rows[0]["sessionId"]
    text = json.dumps(rows)
    parsed = run_usage.runs_from(text)
    assert parsed.runs == ()
    assert parsed.dropped_no_session == 1


def test_cwd_state_and_started_at_survive_unchanged():
    text = _agents_json(_row(cwd="/some/path", state="blocked", startedAt=42))
    parsed = run_usage.runs_from(text)
    run = parsed.runs[0]
    assert run.cwd == "/some/path"
    assert run.state == "blocked"
    assert run.started_at == 42


def test_malformed_top_level_json_raises_naming_the_input():
    with pytest.raises(ValueError, match="agents"):
        run_usage.runs_from("{not json")


# ── Task 2 — a session id resolves to one transcript or to none ─────────


class FakeFiles:
    """A scripted answer for each `Files` method — constructed per test, never patched onto the
    module. `transcripts` maps a session id to the list of `Path`s `find_transcripts` answers
    with; `lines` maps a `Path` to the list of lines `read_lines` answers with (absent → `None`,
    the "unreadable" case)."""

    def __init__(self, transcripts: dict | None = None, lines: dict | None = None):
        self._transcripts = transcripts or {}
        self._lines = lines or {}

    def find_transcripts(self, session_id, projects_root):
        return list(self._transcripts.get(session_id, []))

    def read_lines(self, path):
        return self._lines.get(path)


def test_session_id_with_a_file_resolves():
    transcript = Path("/projects/-Users-someone/sid-1.jsonl")
    files = FakeFiles(transcripts={"sid-1": [transcript]})
    result = run_usage.transcript_for("sid-1", Path("/projects"), files)
    assert result == transcript
    assert isinstance(result, Path)


def test_session_id_with_no_file_returns_none():
    files = FakeFiles()
    assert run_usage.transcript_for("no-such-sid", Path("/projects"), files) is None


def test_session_id_under_two_project_slugs_returns_one_deterministic_path():
    first = Path("/projects/project-a/dup-sid.jsonl")
    second = Path("/projects/project-b/dup-sid.jsonl")
    files = FakeFiles(transcripts={"dup-sid": [second, first]})  # scripted out of sorted order
    result = run_usage.transcript_for("dup-sid", Path("/projects"), files)
    assert result == first  # lexicographically first, regardless of the port's own order
    assert result == run_usage.transcript_for("dup-sid", Path("/projects"), files)


def test_nonexistent_projects_root_returns_none_rather_than_raising(tmp_path):
    # A real `Files()` against a real, nonexistent root — proving the port's own contract,
    # not a scripted stand-in for it.
    files = run_usage.Files()
    missing = tmp_path / "does-not-exist"
    assert run_usage.transcript_for("any-sid", missing, files) is None


def test_transcript_for_body_reaches_the_filesystem_only_through_files():
    source = inspect.getsource(run_usage.transcript_for)
    assert "glob" not in source
    assert "os.path" not in source
    assert "open(" not in source


# ── Task 3 — only assistant records with usage are read ────────────


def _usage_line(
    model="claude-sonnet-5", input_tokens=1, output_tokens=1, timestamp="2026-09-04T00:00:00.000Z"
):
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
            },
        }
    )


def test_assistant_record_with_usage_is_yielded_others_are_not(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        _usage_line(),
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps({"type": "system"}),
        json.dumps({"type": "attachment"}),
    ]
    path.write_text("\n".join(lines) + "\n")
    records = list(run_usage.usage_records(path, run_usage.Files()))
    assert len(records) == 1
    usage, model, timestamp = records[0]
    assert model == "claude-sonnet-5"
    assert usage["input_tokens"] == 1
    assert timestamp == "2026-09-04T00:00:00.000Z"


def test_assistant_record_with_no_usage_is_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    record = {"type": "assistant", "message": {"model": "claude-sonnet-5"}}
    path.write_text(json.dumps(record) + "\n")
    assert list(run_usage.usage_records(path, run_usage.Files())) == []


def test_truncated_final_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(_usage_line() + "\n" + '{"type": "assistant", "message": {"usage"')
    records = list(run_usage.usage_records(path, run_usage.Files()))
    assert len(records) == 1


def test_empty_file_yields_nothing_and_does_not_raise(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("")
    assert list(run_usage.usage_records(path, run_usage.Files())) == []


def test_missing_file_yields_nothing_rather_than_raising(tmp_path):
    # Not covered before this task: every existing case reached `usage_records` through a
    # transcript `transcript_for` had already resolved, so the file always existed. The
    # docstring's own claim — "a missing file yields nothing rather than raising" — had no test.
    missing = tmp_path / "does-not-exist.jsonl"
    assert list(run_usage.usage_records(missing, run_usage.Files())) == []


def _usage_line_with_id(message_id, output_tokens=1, cache_read=0):
    """One `assistant` record carrying an explicit `message.id`.

    The harness writes one record per CONTENT BLOCK of a single API response — thinking, then
    tool_use, then text — and every one of them repeats that response's `message.usage` verbatim
    under the same `message.id`. So a fixture for this behaviour has to be able to say "these two
    records are one response", which `_usage_line` cannot: it writes no id at all.
    """
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-09-04T00:00:00.000Z",
            "message": {
                "id": message_id,
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
            },
        }
    )


def test_one_api_response_split_across_content_blocks_is_counted_once(tmp_path):
    # The defect this fixes: three records, one response, one `message.id`, the same usage block
    # repeated verbatim on each. Counting per record multiplied every figure this reader has ever
    # produced — and by a factor that differs per population (1.74x to 3.35x measured on live
    # transcripts 2026-09-09), so it never cancelled out of a comparison between two of them.
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(_usage_line_with_id("msg_01", output_tokens=212, cache_read=39729) for _ in range(3))
        + "\n"
    )
    records = list(run_usage.usage_records(path, run_usage.Files()))
    assert len(records) == 1
    usage, _model, _timestamp = records[0]
    assert usage["output_tokens"] == 212
    assert usage["cache_read_input_tokens"] == 39729


def test_distinct_api_responses_are_each_counted(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [_usage_line_with_id("msg_01"), _usage_line_with_id("msg_02"), _usage_line_with_id("msg_03")]
    path.write_text("\n".join(lines) + "\n")
    assert len(list(run_usage.usage_records(path, run_usage.Files()))) == 3


def test_a_record_carrying_no_message_id_is_never_deduplicated(tmp_path):
    # A record with no id has no identity to compare, so two of them are two responses as far as
    # anything here can tell. Dropping the second would be a silent UNDERcount, which is the one
    # failure worse than the overcount this task removes — and every fixture written before this
    # task omits the id, so this is also what keeps their meaning intact.
    path = tmp_path / "t.jsonl"
    path.write_text(_usage_line() + "\n" + _usage_line() + "\n")
    assert len(list(run_usage.usage_records(path, run_usage.Files()))) == 2


def test_the_deduplication_key_is_the_message_id_not_the_usage_payload(tmp_path):
    # Two genuinely different responses can carry identical usage — a cached pair of short turns
    # does exactly that. Keying on the payload would collapse them; keying on the id does not.
    path = tmp_path / "t.jsonl"
    lines = [_usage_line_with_id("msg_01", output_tokens=7), _usage_line_with_id("msg_02", output_tokens=7)]
    path.write_text("\n".join(lines) + "\n")
    assert len(list(run_usage.usage_records(path, run_usage.Files()))) == 2


def test_usage_records_body_reaches_the_filesystem_only_through_files():
    source = inspect.getsource(run_usage.usage_records)
    assert "open(" not in source


# ── Task 4 — the five classes stay apart, and thinking is a share ─────────


def _usage(
    input_tokens=1,
    output_tokens=1,
    cache_read=0,
    ephemeral_1h=0,
    ephemeral_5m=0,
    thinking_tokens=None,
):
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": ephemeral_1h + ephemeral_5m,
        "cache_creation": {
            "ephemeral_1h_input_tokens": ephemeral_1h,
            "ephemeral_5m_input_tokens": ephemeral_5m,
        },
    }
    if thinking_tokens is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
    return usage


def test_five_classes_are_separate_fields_none_is_their_sum():
    usage = _usage(
        input_tokens=10, output_tokens=20, cache_read=30, ephemeral_1h=40, ephemeral_5m=50
    )
    records = [(usage, "m", "t")]
    spend = run_usage.spend_by_model(records)["m"]
    assert spend.input == 10
    assert spend.output == 20
    assert spend.cache_read == 30
    assert spend.cache_write_1h == 40
    assert spend.cache_write_5m == 50


def test_cache_creation_reconciles_against_the_two_ephemeral_figures():
    records = [(_usage(ephemeral_1h=100, ephemeral_5m=25), "m", "t")]
    spend = run_usage.spend_by_model(records)["m"]
    assert spend.cache_write_1h == 100
    assert spend.cache_write_5m == 25


def test_cache_creation_input_tokens_is_never_carried_as_a_sixth_class():
    # The fixture's `cache_creation_input_tokens` is 125 — the figure the two ephemerals
    # reconcile to. Task 4's intent keeps the parts and refuses the sum: five classes, and
    # `thinking`, which is a share of `output` rather than a class of its own.
    usage = _usage(
        input_tokens=2, output_tokens=3, cache_read=7, ephemeral_1h=100, ephemeral_5m=25
    )
    spend = run_usage.spend_by_model([(usage, "m", "t")])["m"]
    assert {f.name for f in dataclasses.fields(run_usage.ModelSpend)} == {
        "input",
        "output",
        "cache_read",
        "cache_write_1h",
        "cache_write_5m",
        "thinking",
    }
    classes = [
        spend.input,
        spend.output,
        spend.cache_read,
        spend.cache_write_1h,
        spend.cache_write_5m,
    ]
    assert 125 not in classes, "the cache-creation sum is carried as a class"
    assert sum(classes) not in classes, "a class holds the five-class grand total"


def test_thinking_is_an_int_when_only_some_of_a_models_records_carried_details():
    # 9,717 of 10,028 records on the live disk carry `output_tokens_details`, so a model whose
    # records are split across both shapes is the ordinary case, not an edge one.
    records = [
        (_usage(output_tokens=10), "m", "t"),
        (_usage(output_tokens=20, thinking_tokens=7), "m", "t"),
    ]
    spend = run_usage.spend_by_model(records)["m"]
    assert spend.output == 30
    assert spend.thinking == 7


def test_thinking_is_none_when_absent_and_int_when_present():
    no_thinking = run_usage.spend_by_model([(_usage(), "m", "t")])["m"]
    assert no_thinking.thinking is None
    with_thinking = run_usage.spend_by_model([(_usage(thinking_tokens=0), "m", "t")])["m"]
    assert with_thinking.thinking == 0


def test_thinking_is_never_added_to_output():
    records = [(_usage(output_tokens=194, thinking_tokens=112), "m", "t")]
    spend = run_usage.spend_by_model(records)["m"]
    assert spend.output == 194
    assert spend.thinking == 112


def test_synthetic_is_its_own_key_never_folded_into_a_real_model():
    records = [
        (_usage(input_tokens=5), "claude-sonnet-5", "t"),
        (_usage(input_tokens=7), "<synthetic>", "t"),
    ]
    spend = run_usage.spend_by_model(records)
    assert spend["claude-sonnet-5"].input == 5
    assert spend["<synthetic>"].input == 7


def test_two_models_produce_two_rows_one_model_produces_one():
    two = run_usage.spend_by_model([(_usage(), "a", "t"), (_usage(), "b", "t")])
    assert set(two.keys()) == {"a", "b"}
    one = run_usage.spend_by_model([(_usage(), "a", "t"), (_usage(), "a", "t")])
    assert set(one.keys()) == {"a"}


# ── Task 5 — the span is measured, never summed ───────────────


def _at(ts):
    return (_usage(), "m", ts)


def test_two_records_an_hour_apart_give_3600_regardless_of_count_between():
    records = [
        _at("2026-09-04T00:00:00.000Z"),
        _at("2026-09-04T00:20:00.000Z"),
        _at("2026-09-04T00:40:00.000Z"),
        _at("2026-09-04T01:00:00.000Z"),
    ]
    assert run_usage.span_seconds(records) == 3600


def test_one_record_gives_zero():
    assert run_usage.span_seconds([_at("2026-09-04T00:00:00.000Z")]) == 0


def test_no_records_gives_none_not_zero():
    assert run_usage.span_seconds([]) is None


def test_out_of_order_records_still_give_the_full_span():
    records = [
        _at("2026-09-04T00:40:00.000Z"),
        _at("2026-09-04T00:00:00.000Z"),
        _at("2026-09-04T01:00:00.000Z"),
        _at("2026-09-04T00:20:00.000Z"),
    ]
    assert run_usage.span_seconds(records) == 3600


# ── Task 6 — the ledger states what it could not read ─────────────


def _run(session_id, **overrides):
    fields = {
        "session_id": session_id,
        "name": "a run",
        "cwd": "/cwd",
        "state": "done",
        "started_at": 1,
    }
    fields.update(overrides)
    return run_usage.Run(**fields)


def _project(tmp_path, slug="proj"):
    project = tmp_path / slug
    project.mkdir(exist_ok=True)
    return project


def test_one_unreadable_of_three_gives_two_rows_and_coverage(tmp_path):
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    (project / "sid-2.jsonl").write_text(_usage_line())
    # sid-3 has no transcript file at all
    runs = [_run("sid-1"), _run("sid-2"), _run("sid-3")]
    led = run_usage.ledger(runs, tmp_path, run_usage.Files())
    assert len(led.rows) == 2
    assert led.coverage.read == 2
    assert led.coverage.unreadable == 1
    assert led.coverage.total == 3
    assert led.coverage.unreadable_ids == ("sid-3",)


def test_transcript_with_no_usage_records_counts_as_read_with_zero_spend(tmp_path):
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(json.dumps({"type": "user", "message": {}}) + "\n")
    led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())
    assert led.coverage.read == 1
    assert led.coverage.unreadable == 0
    assert led.rows[0].spend == {}
    assert led.rows[0].span_seconds is None


def test_ledger_threads_the_resolved_transcript_as_a_path_into_usage_records(tmp_path):
    """Every path in and out of `ledger` is a `Path` — proven by a spy `Files` that records the
    type of what it was actually asked to read, not by re-deriving the value from the plain
    `str` this used to be."""
    project = _project(tmp_path)
    transcript = project / "sid-1.jsonl"
    transcript.write_text(_usage_line())

    class SpyFiles(run_usage.Files):
        def __init__(self):
            self.read_paths = []

        def read_lines(self, path):
            self.read_paths.append(path)
            return super().read_lines(path)

    files = SpyFiles()
    led = run_usage.ledger([_run("sid-1")], tmp_path, files)
    assert led.coverage.read == 1
    assert files.read_paths == [transcript]
    assert all(isinstance(path, Path) for path in files.read_paths)


def test_coverage_present_even_when_everything_read_cleanly(tmp_path):
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())
    assert led.coverage.read == 1
    assert led.coverage.unreadable == 0
    assert led.coverage.total == 1
    assert led.coverage.unreadable_ids == ()


# ── Task 7 — the two renderings, and neither one totals ────────────


def _ledger_row(session_id="sid-1", spend=None, span=60):
    return run_usage.LedgerRow(
        session_id=session_id,
        name="a run",
        cwd="/cwd",
        state="done",
        started_at=1,
        span_seconds=span,
        spend=spend if spend is not None else {},
    )


def _small_ledger():
    row = _ledger_row(
        spend={"claude-sonnet-5": run_usage.ModelSpend(input=1, output=194, thinking=112)}
    )
    return run_usage.Ledger(
        rows=(row,),
        coverage=run_usage.Coverage(
            read=1, unreadable=1, total=2, unreadable_ids=("sid-2",), listed=1, named=1
        ),
    )


def test_both_renderings_carry_the_population_sentence_verbatim():
    # The words the reader defines `kind != "interactive"` by, not merely those two tokens
    # appearing somewhere in the output.
    led = _small_ledger()
    assert run_usage.POPULATION_NOTE in run_usage.render(led)
    assert 'kind != "interactive"' in run_usage.POPULATION_NOTE
    assert json.loads(run_usage.render_json(led))["population"] == run_usage.POPULATION_NOTE


def test_rendered_coverage_object_has_all_seven_fields():
    led = _small_ledger()  # read 1 / unreadable 1 / total 2, the unreadable one being sid-2
    human = run_usage.render(led)
    assert "1 read" in human
    assert "1 unreadable" in human
    assert "2 total" in human
    assert "sid-2" in human
    assert "(1 listed, 1 named)" in human
    assert json.loads(run_usage.render_json(led))["coverage"] == {
        "read": 1,
        "unreadable": 1,
        "total": 2,
        "unreadable_ids": ["sid-2"],
        "dropped_no_session": 0,
        "listed": 1,
        "named": 1,
    }


def test_no_rendered_number_equals_the_grand_total():
    # cache reads ~250x everything else — the trap: a naive summary might print the sum.
    spend = {
        "claude-opus-5": run_usage.ModelSpend(
            input=2, output=3, cache_read=250_000, cache_write_1h=5, cache_write_5m=1, thinking=None
        )
    }
    grand_total = 2 + 3 + 250_000 + 5 + 1
    led = run_usage.Ledger(
        rows=(_ledger_row(spend=spend),),
        coverage=run_usage.Coverage(read=1, unreadable=0, total=1, unreadable_ids=()),
    )
    for text in (run_usage.render(led), run_usage.render_json(led)):
        assert str(grand_total) not in text


def test_render_json_round_trips_and_carries_one_object_per_run():
    led = run_usage.Ledger(
        rows=(_ledger_row("sid-1"), _ledger_row("sid-2")),
        coverage=run_usage.Coverage(read=2, unreadable=0, total=2, unreadable_ids=()),
    )
    payload = json.loads(run_usage.render_json(led))
    assert len(payload["rows"]) == 2
    assert {r["session_id"] for r in payload["rows"]} == {"sid-1", "sid-2"}


def test_thinking_none_renders_no_thinking_figure_not_zero():
    row = _ledger_row(spend={"m": run_usage.ModelSpend(input=1, output=1, thinking=None)})
    coverage = run_usage.Coverage(read=1, unreadable=0, total=1, unreadable_ids=())
    led = run_usage.Ledger(rows=(row,), coverage=coverage)
    assert "thinking" not in run_usage.render(led)
    payload = json.loads(run_usage.render_json(led))
    assert payload["rows"][0]["spend"]["m"]["thinking"] is None


# ── Task 8 (this station) — a dropped row is visible in both renderings ────


def test_dropped_no_session_row_is_visible_in_both_renderings(tmp_path):
    rows = json.loads(_agents_json(_row(sessionId="sid-1"), _row()))
    del rows[1]["sessionId"]
    parsed = run_usage.runs_from(json.dumps(rows))
    assert parsed.dropped_no_session == 1

    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    led = run_usage.ledger(
        parsed.runs, tmp_path, run_usage.Files(), dropped_no_session=parsed.dropped_no_session
    )

    human = run_usage.render(led)
    assert "Dropped (no sessionId): 1" in human
    payload = json.loads(run_usage.render_json(led))
    assert payload["coverage"]["dropped_no_session"] == 1

    # read + unreadable + dropped == the population before dropping (both rows the fixture
    # carried: the one kept and the one dropped for having no sessionId).
    assert led.coverage.read + led.coverage.unreadable + led.coverage.dropped_no_session == 2


def test_zero_dropped_rows_still_states_the_count_not_silent():
    coverage = run_usage.Coverage(
        read=0, unreadable=0, total=0, unreadable_ids=(), dropped_no_session=0
    )
    led = run_usage.Ledger(rows=(), coverage=coverage)
    assert "Dropped (no sessionId): 0" in run_usage.render(led)
    payload = json.loads(run_usage.render_json(led))
    assert payload["coverage"]["dropped_no_session"] == 0


def test_main_states_a_dropped_row_in_the_human_summary_and_in_the_json(tmp_path, capsys):
    # Reached-when 4 is about the reader's own output, not about `ledger`'s return value: the
    # count has to survive `main`'s wiring from `runs_from` through to both renderings. Every
    # other dropped-row test here calls `ledger` directly and would stay green if that wiring
    # were cut.
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    rows = [_row(sessionId="sid-1"), _row()]
    del rows[1]["sessionId"]
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(json.dumps(rows))
    argv = ["--agents-json", str(agents_path), "--projects-root", str(tmp_path)]

    run_usage.main(argv)
    assert "Dropped (no sessionId): 1" in capsys.readouterr().out

    run_usage.main(argv + ["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["dropped_no_session"] == 1
    assert payload["coverage"]["read"] == 1


def test_ledger_defaults_dropped_no_session_to_zero_when_not_passed(tmp_path):
    # A caller that does not know about dropped rows (every pre-existing test) still gets a
    # Coverage that states zero rather than one with no opinion at all.
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())
    assert led.coverage.dropped_no_session == 0


# ── pathlib.Path throughout (this station) ─────────────────


def test_run_usage_module_imports_pathlib_and_neither_os_path_nor_glob():
    source = Path(run_usage.__file__).read_text()
    assert "from pathlib import Path" in source
    assert re.search(r"^import glob$", source, re.MULTILINE) is None
    assert re.search(r"^import os$", source, re.MULTILINE) is None
    assert "os.path" not in source


def test_a_string_projects_root_from_the_command_line_becomes_a_path_at_the_boundary(
    tmp_path, capsys
):
    # By effect: `Files.find_transcripts` calls `projects_root.glob(...)`, so a root that
    # reached it still a `str` could resolve nothing at all. The transcript being read is the
    # proof the conversion happened at the flag, not the annotation saying it should.
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    agents_path = _write_agents_json(tmp_path, _row(sessionId="sid-1"))
    run_usage.main(["--agents-json", agents_path, "--projects-root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["read"] == 1
    assert payload["rows"][0]["spend"]["claude-sonnet-5"]["input"] == 1


def test_ledger_rows_carry_no_path_object_anywhere(tmp_path):
    # Exercised end to end — through the real Files() port, over a real transcript on disk —
    # rather than asserted from a field's type annotation alone.
    project = _project(tmp_path)
    (project / "sid-1.jsonl").write_text(_usage_line())
    led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())
    for row in led.rows:
        for field in dataclasses.fields(row):
            value = getattr(row, field.name)
            assert not isinstance(value, Path), f"{field.name} carries a Path: {value!r}"
    # `render_json` would have raised on any `Path` it was handed — this is the same fact,
    # proven the other way, over the actual rendering rather than the dataclass alone.
    json.loads(run_usage.render_json(led))


# ── The agents JSON arrives through both ports (this station) ───────────


class FakeProcesses:
    """Records every call, answering with a scripted stdout — constructed per test, never
    patched onto the module."""

    def __init__(self, stdout: str = ""):
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def run(self, argv):
        self.calls.append(list(argv))
        return self._stdout


class RaisingProcesses:
    """A `Processes` that must never be called — proves a code path shells out to nothing by
    failing loudly if it does, rather than by asserting on a global afterward."""

    def run(self, argv):
        raise AssertionError(f"Processes.run must not be called; called with {argv!r}")


def test_agents_json_text_given_a_path_reads_it_through_files_and_never_shells_out(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text("[]")
    text = run_usage._agents_json_text(path, run_usage.Files(), RaisingProcesses())
    assert text.strip() == "[]"


def test_agents_json_text_given_no_path_shells_out_through_processes():
    processes = FakeProcesses(stdout="[]")
    text = run_usage._agents_json_text(None, run_usage.Files(), processes)
    assert text == "[]"
    assert processes.calls == [["claude", "agents", "--json", "--all"]]


def test_agents_json_text_propagates_a_nonzero_exit_as_system_exit():
    class FailingProcesses:
        def run(self, argv):
            raise SystemExit("error: `claude agents --json --all` exited 3: boom")

    with pytest.raises(SystemExit, match="exited 3"):
        run_usage._agents_json_text(None, run_usage.Files(), FailingProcesses())


def test_agents_json_text_body_reaches_the_world_only_through_ports():
    source = inspect.getsource(run_usage._agents_json_text)
    assert "open(" not in source
    assert "subprocess" not in source


# ── Task 8 — the CLI, the docstring and the version ─────────────


def _write_agents_json(tmp_path, *rows):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(list(rows)))
    return str(path)


def test_agents_json_flag_reads_the_file_and_shells_out_to_nothing(tmp_path):
    agents_path = _write_agents_json(tmp_path, _row(sessionId="sid-1"))
    # A `Processes` that raises if it is ever called — the behaviour this test exists to prove
    # is proven by the fake, not by asserting on a monkeypatched global afterward.
    text = run_usage._agents_json_text(Path(agents_path), run_usage.Files(), RaisingProcesses())
    parsed = run_usage.runs_from(text)
    assert [r.session_id for r in parsed.runs] == ["sid-1"]


def test_no_agents_json_flag_shells_out():
    processes = FakeProcesses(stdout=json.dumps([_row(sessionId="sid-1")]))
    text = run_usage._agents_json_text(None, run_usage.Files(), processes)
    assert processes.calls == [["claude", "agents", "--json", "--all"]]
    parsed = run_usage.runs_from(text)
    assert [r.session_id for r in parsed.runs] == ["sid-1"]


def test_projects_root_defaults_to_the_claude_projects_dir():
    # The behaviour is the flag's default reaching the reader, not a module constant holding
    # the right string — a constant nothing passes would satisfy the second and not the first.
    reader = run_usage.build_reader()
    assert reader.projects_root == Path(os.path.expanduser("~/.claude/projects"))


def test_json_flag_emits_only_the_ledger(tmp_path, capsys):
    agents_path = _write_agents_json(tmp_path, _row(sessionId="sid-1"))
    projects_root = str(tmp_path / "projects")
    run_usage.main(["--agents-json", agents_path, "--projects-root", projects_root, "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "rows" in payload


def test_default_emits_the_human_summary_not_json(tmp_path, capsys):
    agents_path = _write_agents_json(tmp_path, _row(sessionId="sid-1"))
    projects_root = str(tmp_path / "projects")
    run_usage.main(["--agents-json", agents_path, "--projects-root", projects_root])
    out = capsys.readouterr().out
    assert "Population:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_docstring_states_no_price_model_and_never_sums_and_names_the_cache_creation_split():
    doc = run_usage.__doc__
    assert "No price model" in doc or "no price model" in doc.lower()
    assert "never" in doc.lower() and "sum" in doc.lower()
    assert "cache_creation" in doc
    # the 1h/5m split named as the reason there are five classes, not four
    assert "ephemeral_1h_input_tokens" in doc
    assert "ephemeral_5m_input_tokens" in doc


# ── Adding two spends: one home, because a third reader now wants it ──────


class TestAddAndMergeSpend:
    """`add_spend` / `merge_spend` live here, beside `ModelSpend` and `spend_by_model`, because
    this module owns what a spend IS. They were private in `flow_usage.py` until a third reader
    (`chain_usage.py`) needed the same arithmetic and had to route around them — the next one would
    have copied them or reached into a private name, and a class-by-class accumulator copied twice
    is a rule with three homes to disagree in.
    """

    def test_add_spend_adds_each_class_apart_never_summing_two_classes(self):
        a = run_usage.ModelSpend(input=1, output=2, cache_read=3, cache_write_1h=4, cache_write_5m=5)
        b = run_usage.ModelSpend(
            input=10, output=20, cache_read=30, cache_write_1h=40, cache_write_5m=50
        )
        assert run_usage.add_spend(a, b) == run_usage.ModelSpend(
            input=11, output=22, cache_read=33, cache_write_1h=44, cache_write_5m=55
        )

    def test_add_spend_thinking_stays_none_only_when_both_sides_carry_none(self):
        """`None` means "nothing this model produced carried `output_tokens_details`", which is a
        different fact from zero — so it survives addition only when neither side carried any."""
        a = run_usage.ModelSpend(thinking=None)
        assert run_usage.add_spend(a, run_usage.ModelSpend(thinking=None)).thinking is None
        assert run_usage.add_spend(a, run_usage.ModelSpend(thinking=3)).thinking == 3

    def test_merge_spend_merges_per_model_dicts(self):
        a = {"sonnet": run_usage.ModelSpend(input=1)}
        b = {"sonnet": run_usage.ModelSpend(input=10), "haiku": run_usage.ModelSpend(input=100)}
        merged = run_usage.merge_spend(a, b)
        assert merged["sonnet"].input == 11
        assert merged["haiku"].input == 100

    def test_merge_spend_does_not_mutate_either_argument(self):
        """Not covered where these functions previously lived. `merge_spend` copies its left side,
        and a caller folding many readings into an accumulator would silently corrupt the first
        one if it did not."""
        a = {"sonnet": run_usage.ModelSpend(input=1)}
        b = {"sonnet": run_usage.ModelSpend(input=10)}
        run_usage.merge_spend(a, b)
        assert a == {"sonnet": run_usage.ModelSpend(input=1)}
        assert b == {"sonnet": run_usage.ModelSpend(input=10)}


# ── Runs named by session id: `--session` replaces the run-log population ──────

POPULATION = (
    'Population: background runs only (rows with kind != "interactive"), and — with --session — '
    "every run named by its session id."
)


def _response_line(message_id, input_tokens=1, output_tokens=1, model="claude-sonnet-5",
                   timestamp="2026-09-25T09:00:00.000Z", thinking=None):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
             "cache_read_input_tokens": 0,
             "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    message = {"model": model, "usage": usage}
    if message_id is not None:
        message["id"] = message_id
    return json.dumps({"type": "assistant", "timestamp": timestamp, "message": message})


def _transcript(root, session_id, *lines, slug="proj"):
    project = root / slug
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text("".join(line + "\n" for line in lines))


def _main(argv, capsys):
    status = run_usage.main(argv)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _runs_argv(tmp_path, rows, sessions=(), json_form=True):
    argv = ["--agents-json", _write_agents_json(tmp_path, *rows), "--projects-root", str(tmp_path)]
    for session_id in sessions:
        argv += ["--session", session_id]
    return argv + (["--json"] if json_form else [])


def test_listed_and_named_overlap_is_one_run(tmp_path, capsys):
    _transcript(tmp_path, "sid-p", _response_line("m1"))
    argv = _runs_argv(tmp_path, [_row(sessionId="sid-p", name="listed run")], ["sid-p"])
    _status, out, _err = _main(argv, capsys)
    payload = json.loads(out)
    assert [row["session_id"] for row in payload["rows"]] == ["sid-p"]
    assert payload["rows"][0]["name"] == "listed run"
    assert (payload["coverage"]["listed"], payload["coverage"]["named"]) == (1, 0)


def test_repeated_named_id_is_one_run_at_its_first_position(tmp_path, capsys):
    for sid in ("sid-a", "sid-b"):
        _transcript(tmp_path, sid, _response_line("m1"))
    _status, out, _err = _main(_runs_argv(tmp_path, [], ["sid-n", "sid-n"]), capsys)
    assert json.loads(out)["coverage"]["total"] == 1
    _status, out, _err = _main(_runs_argv(tmp_path, [], ["sid-b", "sid-a", "sid-b"]), capsys)
    assert [row["session_id"] for row in json.loads(out)["rows"]] == ["sid-b", "sid-a"]


def test_named_only_run_has_no_identity(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", _response_line("m1"))
    argv = _runs_argv(tmp_path, [], ["sid-n"])
    row = json.loads(_main(argv, capsys)[1])["rows"][0]
    assert [row[key] for key in ("name", "cwd", "state", "started_at")] == [None] * 4
    table = _main(_runs_argv(tmp_path, [], ["sid-n"], json_form=False), capsys)[1]
    assert "sid-n  (unnamed)  cwd=None  state=None\n  started=None  span=0s\n" in table


def test_population_counts_in_both_forms(tmp_path, capsys):
    rows = [_row(sessionId="sid-bg"), _row(sessionId="sid-p")]
    for sid in ("sid-bg", "sid-p", "sid-n"):
        _transcript(tmp_path, sid, _response_line(f"m-{sid}"))
    table = _main(_runs_argv(tmp_path, rows, ["sid-p", "sid-n", "sid-n"], False), capsys)[1]
    assert table.splitlines()[2] == "Coverage: 3 read / 0 unreadable / 3 total (2 listed, 1 named)"
    payload = json.loads(_main(_runs_argv(tmp_path, rows, ["sid-p", "sid-n", "sid-n"]), capsys)[1])
    assert (payload["coverage"]["listed"], payload["coverage"]["named"]) == (2, 1)
    assert [row["session_id"] for row in payload["rows"]] == ["sid-bg", "sid-p", "sid-n"]


def test_interactive_and_dropped_rows(tmp_path, capsys):
    rows = [_row(kind="interactive", sessionId="x"), _row()]
    del rows[1]["sessionId"]
    table = _main(_runs_argv(tmp_path, rows, json_form=False), capsys)[1]
    assert table == (
        f"{POPULATION}\n{SPAN}\nCoverage: 0 read / 0 unreadable / 0 total (0 listed, 0 named)\n"
        "Dropped (no sessionId): 1\n\nNo runs read.\n"
        f"{NO_COST}\n"
    )


def test_library_read_runs_equals_cli_rows(tmp_path, capsys):
    _transcript(tmp_path, "sid-bg", _response_line("m1", 4, 5, thinking=2))
    _transcript(tmp_path, "sid-n", _response_line("m2", 7, 8))
    text = _agents_json(_row(sessionId="sid-bg"))
    led = run_usage.read_runs(text, ["sid-n"], run_usage.build_reader(tmp_path))
    argv = _runs_argv(tmp_path, [_row(sessionId="sid-bg")], ["sid-n"])
    payload = json.loads(_main(argv, capsys)[1])
    assert len(led.rows) == len(payload["rows"]) == 2
    for row, printed in zip(led.rows, payload["rows"]):
        assert (row.session_id, row.name, row.cwd, row.state, row.started_at) == (
            printed["session_id"], printed["name"], printed["cwd"], printed["state"],
            printed["started_at"])
        assert row.span_seconds == printed["span_seconds"]
        assert {m: dataclasses.asdict(s) for m, s in row.spend.items()} == printed["spend"]


def test_with_named_sessions_keeps_listing_order_and_appends_unlisted_once():
    parsed = run_usage.runs_from(_agents_json(_row(sessionId="a"), _row(sessionId="b")))
    merged = run_usage.with_named_sessions(parsed, ["c", "b", "c", "d"])
    assert [run.session_id for run in merged.runs] == ["a", "b", "c", "d"]
    assert (merged.listed, merged.named) == (2, 2)
    assert merged.runs[2] == run_usage.Run("c", None, None, None, None)


def test_no_listing_and_no_named_runs(tmp_path, capsys):
    status, out, _err = _main(_runs_argv(tmp_path, [], json_form=False), capsys)
    assert status == 0
    assert out == (
        f"{POPULATION}\n{SPAN}\nCoverage: 0 read / 0 unreadable / 0 total (0 listed, 0 named)\n"
        "Dropped (no sessionId): 0\n\nNo runs read.\n"
        f"{NO_COST}\n"
    )


def test_named_session_with_no_transcript_is_unreadable(tmp_path, capsys):
    status, out, _err = _main(_runs_argv(tmp_path, [], ["sid-gone"], False), capsys)
    assert status == 0
    assert out == (
        f"{POPULATION}\n{SPAN}\nCoverage: 0 read / 1 unreadable / 1 total (0 listed, 1 named) — "
        "unreadable: sid-gone (gone)\nDropped (no sessionId): 0\n\nNo runs read.\n"
        f"{NO_COST}\n"
    )


def test_mixed_readable_and_unreadable_keep_run_order(tmp_path, capsys):
    _transcript(tmp_path, "sid-b", _response_line("m1"))
    rows = [_row(sessionId="sid-a"), _row(sessionId="sid-b"), _row(sessionId="sid-c")]
    payload = json.loads(_main(_runs_argv(tmp_path, rows, ["sid-d"]), capsys)[1])
    coverage = payload["coverage"]
    assert coverage["unreadable_ids"] == ["sid-a", "sid-c", "sid-d"]
    assert coverage["total"] == coverage["read"] + coverage["unreadable"] == 4


def test_missing_projects_root_marks_every_run_unreadable(tmp_path, capsys):
    argv = ["--agents-json", _write_agents_json(tmp_path, _row(sessionId="sid-a")),
            "--projects-root", str(tmp_path / "absent"), "--session", "sid-n", "--json"]
    coverage = json.loads(_main(argv, capsys)[1])["coverage"]
    assert (coverage["read"], coverage["unreadable"]) == (0, 2)


def test_two_project_dirs_read_the_lexicographically_first_transcript(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", _response_line("m1", input_tokens=9), slug="proj-b")
    _transcript(tmp_path, "sid-n", _response_line("m1", input_tokens=1), slug="proj-a")
    payload = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])
    assert payload["rows"][0]["spend"]["claude-sonnet-5"]["input"] == 1


def test_transcript_with_no_usage_still_counts_as_read(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", json.dumps({"type": "user", "message": {}}))
    table = _main(_runs_argv(tmp_path, [], ["sid-n"], False), capsys)[1]
    assert "Coverage: 1 read / 0 unreadable / 1 total (0 listed, 1 named)" in table
    assert "  started=None  span=n/a\n  no usage records\n" in table


def test_row_shape_and_span_format_uses_distinct_response_ids(tmp_path, capsys):
    _transcript(tmp_path, "sid-n",
                _response_line("m1", 3, 4, thinking=2, timestamp="2026-09-25T09:00:00.000Z"),
                _response_line("m2", 5, 6, model="other", timestamp="2026-09-25T09:21:12.390Z"))
    _transcript(tmp_path, "sid-s", _response_line("n1", timestamp="2026-09-25T09:00:00Z"),
                _response_line("n2", timestamp="2026-09-25T09:00:42Z"))
    table = _main(_runs_argv(tmp_path, [], ["sid-n", "sid-s"], False), capsys)[1]
    assert table == (
        f"{POPULATION}\n{SPAN}\nCoverage: 2 read / 0 unreadable / 2 total (0 listed, 2 named)\n"
        "Dropped (no sessionId): 0\n\n"
        "sid-n  (unnamed)  cwd=None  state=None\n  started=None  span=1272.39s\n"
        "  claude-sonnet-5: input=3 output=4 thinking=2 cache_read=0 cache_write_1h=0 "
        "cache_write_5m=0\n"
        "  other: input=5 output=6 cache_read=0 cache_write_1h=0 cache_write_5m=0\n\n"
        "sid-s  (unnamed)  cwd=None  state=None\n  started=None  span=42s\n"
        "  claude-sonnet-5: input=2 output=2 cache_read=0 cache_write_1h=0 cache_write_5m=0\n"
        f"{NO_COST}\n"
    )
    out = _main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1]
    spend = {"claude-sonnet-5": {"input": 3, "output": 4, "thinking": 2, "cache_read": 0,
                                 "cache_write_1h": 0, "cache_write_5m": 0},
             "other": {"input": 5, "output": 6, "thinking": None, "cache_read": 0,
                       "cache_write_1h": 0, "cache_write_5m": 0}}
    row = {"session_id": "sid-n", "name": None, "cwd": None, "state": None, "started_at": None,
           "span_seconds": 1272.39, "spend": spend}
    coverage = {"read": 1, "unreadable": 0, "total": 1, "unreadable_ids": [],
                "dropped_no_session": 0, "listed": 0, "named": 1}
    assert out == json.dumps(
        {"population": POPULATION, "span": SPAN, "coverage": coverage, "rows": [row]}, indent=2
    ) + "\n"


def test_thinking_is_nullable_and_never_added_to_output(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", _response_line("m1", output_tokens=10, model="quiet"),
                _response_line("m2", output_tokens=20, model="loud", thinking=7))
    table = _main(_runs_argv(tmp_path, [], ["sid-n"], False), capsys)[1]
    assert "  quiet: input=1 output=10 cache_read" in table
    assert "  loud: input=1 output=20 thinking=7 cache_read" in table
    spend = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])["rows"][0]["spend"]
    assert spend["quiet"]["thinking"] is None and spend["loud"]["output"] == 20


def test_records_sharing_a_message_id_count_once(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", _response_line("m2", 7), _response_line("m2", 7),
                _response_line("m2", 7), _response_line("m1", 5))
    spend = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])["rows"][0]["spend"]
    assert spend["claude-sonnet-5"]["input"] == 12


def test_equal_usage_under_distinct_ids_counts_separately():
    records = [(_usage(input_tokens=3), "m", "t"), (_usage(input_tokens=3), "m", "t")]
    assert run_usage.spend_by_model(records)["m"].input == 6


def test_records_without_a_message_id_count_each(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", _response_line(None, 3), _response_line(None, 3))
    spend = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])["rows"][0]["spend"]
    assert spend["claude-sonnet-5"]["input"] == 6


def test_malformed_partial_and_blank_lines_are_skipped(tmp_path, capsys):
    _transcript(tmp_path, "sid-n", "", "not json", _response_line("m1", 4), '{"type": "assi')
    spend = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])["rows"][0]["spend"]
    assert spend["claude-sonnet-5"]["input"] == 4


def test_missing_and_out_of_order_timestamps(tmp_path, capsys):
    _transcript(tmp_path, "sid-n",
                _response_line("m1", timestamp="2026-09-25T09:10:00Z"),
                _response_line("m2", timestamp=None),
                _response_line("m3", timestamp="2026-09-25T09:00:00Z"),
                _response_line("m4", timestamp="2026-09-25T09:05:00Z"))
    _transcript(tmp_path, "sid-x", _response_line("m1", timestamp=None))
    rows = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n", "sid-x"]), capsys)[1])["rows"]
    assert [row["span_seconds"] for row in rows] == [600, None]


def test_distinct_models_keep_first_appearance_order_and_classes_are_never_summed(
    tmp_path, capsys
):
    _transcript(tmp_path, "sid-n", _response_line("m1", model="zeta"),
                _response_line("m2", model="alpha"), _response_line("m3", model="zeta"))
    spend = json.loads(_main(_runs_argv(tmp_path, [], ["sid-n"]), capsys)[1])["rows"][0]["spend"]
    assert list(spend) == ["zeta", "alpha"]
    assert set(spend["zeta"]) == {"input", "output", "thinking", "cache_read", "cache_write_1h",
                                  "cache_write_5m"}


def _fake_claude(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_saved_listing_runs_no_subprocess(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # any attempt to start `claude` would fail loudly
    status, _out, _err = _main(_runs_argv(tmp_path, [_row(sessionId="sid-a")]), capsys)
    assert status == 0


def test_omitted_listing_runs_the_declared_claude_command_through_the_process_port(
    tmp_path, capsys, monkeypatch
):
    record = tmp_path / "argv.txt"
    _fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{record}"\necho "[]"\n')
    status, _out, _err = _main(["--projects-root", str(tmp_path), "--json"], capsys)
    assert status == 0
    assert record.read_text().split() == ["agents", "--json", "--all"]


def test_listing_errors_go_to_stderr_with_exit_1(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "missing.json"
    with pytest.raises(SystemExit) as unreadable:
        run_usage.main(["--agents-json", str(missing)])
    assert str(unreadable.value) == (
        f"error: could not read {missing} — check the path given to --agents-json, or leave "
        "--agents-json out to have ka-telemetry run `claude agents --json --all` itself"
    )
    assert capsys.readouterr().out == ""

    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    status, out, err = _main(["--agents-json", str(bad)], capsys)
    assert (status, out) == (1, "")
    assert err.startswith(
        "error: could not parse `claude agents --json --all` output as JSON — if you passed "
        "--agents-json, check that file; otherwise run that command yourself to see what it "
        "prints. The parser reported: Expecting value")
    assert "run_usage:" not in err

    bad.write_text("{}")
    status, out, err = _main(["--agents-json", str(bad)], capsys)
    assert (status, out, err) == (1, "", (
        "error: `claude agents --json --all` output was not a JSON array — if you passed "
        "--agents-json, check that file; otherwise run that command yourself to see what it "
        "prints\n"))

    _fake_claude(tmp_path, monkeypatch, 'echo boom >&2\nexit 3\n')
    with pytest.raises(SystemExit) as failed:
        run_usage.main([])
    assert str(failed.value) == (
        "error: `claude agents --json --all` exited 3 — fix what it reported, or pass "
        "--agents-json a file holding its saved output. It reported: boom"
    )

    bad.write_text("[]")
    assert _main(["--agents-json", str(bad), "--projects-root", str(tmp_path)], capsys)[0] == 0


@pytest.mark.parametrize("argv", [["--run-logs", "logs/"], ["--agents"], ["--bogus"]])
def test_a_flag_the_command_does_not_take_is_a_usage_error_with_a_next_step(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        run_usage.main(argv)
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: ka-telemetry runs")
    assert err.splitlines()[-1] == "Run `ka-telemetry runs --help` to see what it accepts."


def test_help_carries_the_published_description_and_flag_words(capsys):
    with pytest.raises(SystemExit) as raised:
        run_usage.main(["--help"])
    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for words in (
        "Reports the tokens each background run used, read from its transcript. The runs are "
        "the background runs that `claude agents --json --all` lists, plus every run you name "
        "with --session.",
        "Thinking tokens are part of output, not added to it.",
        "also report the run with this session id, such as a `claude -p` run that `claude "
        "agents` does not list; repeat the flag to name more runs",
        "print the report as JSON instead of a table",
    ):
        assert words in text


# ── Unreadable lines — a transcript line the report could not read is counted, not hidden ──
#
# The count is of non-blank lines that do not parse as JSON, taken in the same single read of
# the transcript that yields the figures. A line that parses as JSON is outside the class.


def _read(path, files=None, counter=None):
    """`usage_records` over `path`, fully consumed; `counter` is passed only when given."""
    files = files or run_usage.Files()
    if counter is None:
        return list(run_usage.usage_records(path, files))
    return list(run_usage.usage_records(path, files, unreadable_lines=counter))


def _write(path, *lines, final_newline=True):
    path.write_text("\n".join(lines) + ("\n" if final_newline else ""))
    return path


def _counted(path):
    counter = run_usage.UnreadableLines()
    records = _read(path, counter=counter)
    return counter.count, records


BAD = "not json {"


def test_the_counter_starts_at_zero_and_is_an_integer():
    counter = run_usage.UnreadableLines()

    assert counter.count == 0
    assert isinstance(counter.count, int)


def test_malformed_line_before_between_and_after_valid_records_is_counted_once_each(tmp_path):
    clean = _write(tmp_path / "clean.jsonl", _response_line("m1", 4), _response_line("m2", 5))
    dirty = _write(
        tmp_path / "dirty.jsonl",
        BAD,
        _response_line("m1", 4),
        BAD,
        _response_line("m2", 5),
        BAD,
    )

    count, records = _counted(dirty)

    assert count == 3
    assert records == _read(clean)


def test_truncated_final_record_with_no_newline_is_counted(tmp_path):
    path = _write(
        tmp_path / "live.jsonl",
        _response_line("m1", 4),
        _response_line("m2", 5),
        '{"type": "assistant", "message": {"usage"',
        final_newline=False,
    )

    count, records = _counted(path)

    assert count == 1
    assert [usage["input_tokens"] for usage, _model, _stamp in records] == [4, 5]


def test_blank_and_whitespace_only_lines_are_not_counted(tmp_path):
    path = _write(tmp_path / "t.jsonl", "", _response_line("m1"), "   ", "\t", "")

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


def test_multiple_bad_lines_are_all_counted(tmp_path):
    path = _write(tmp_path / "t.jsonl", BAD, BAD, _response_line("m1"), "{", "[", "}")

    assert _counted(path)[0] == 5


def test_count_is_independent_of_response_id_deduplication(tmp_path):
    repeated = _write(
        tmp_path / "repeated.jsonl",
        _response_line("m1", 7),
        BAD,
        _response_line("m1", 7),
        _response_line("m1", 7),
        BAD,
    )
    distinct = _write(
        tmp_path / "distinct.jsonl",
        _response_line("m1", 7),
        BAD,
        _response_line("m2", 7),
        _response_line("m3", 7),
        BAD,
    )

    repeated_count, repeated_records = _counted(repeated)
    distinct_count, distinct_records = _counted(distinct)

    assert (repeated_count, len(repeated_records)) == (2, 1)
    assert (distinct_count, len(distinct_records)) == (2, 3)


def test_json_shaped_lines_are_outside_the_count(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        "{}",
        json.dumps({"type": "assistant", "message": {"model": "m"}}),
        _response_line("m1"),
    )

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


@pytest.mark.parametrize("json_line", ["[1, 2]", '"just a string"', "42", "null", "true"])
def test_non_object_json_line_is_counted_and_reading_continues(tmp_path, json_line):
    path = _write(tmp_path / "t.jsonl", _response_line("m1"), BAD, json_line, _response_line("m2"))

    count, records = _counted(path)

    assert count == 2
    assert [usage["input_tokens"] for usage, _m, _t in records] == [1, 1]
    assert len(records) == 2


# ── CP-412 to CP-415 — a record that parses but cannot be added is counted whole ──

TOKEN_FIGURES = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation.ephemeral_1h_input_tokens",
    "cache_creation.ephemeral_5m_input_tokens",
    "output_tokens_details.thinking_tokens",
)
VALID_FIGURES = (0, 7, None)


def _usage_with(figure, value):
    """A full valid `usage` with the dotted `figure` set to `value` (or removed for `ABSENT`)."""
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 1,
        "cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": 1},
        "output_tokens_details": {"thinking_tokens": 1},
    }
    head, _, tail = figure.partition(".")
    if tail:
        usage[head][tail] = value
    else:
        usage[head] = value
    return usage


def _assistant(usage="__default__", *, message_extra=None, timestamp="2026-09-25T09:00:00.000Z",
               model="claude-sonnet-5", message_id=None, message="__default__"):
    if message == "__default__":
        message = {"model": model}
        if usage != "__default__":
            message["usage"] = usage
        if message_id is not None:
            message["id"] = message_id
        message.update(message_extra or {})
    record = {"type": "assistant", "timestamp": timestamp}
    if message != "__absent__":
        record["message"] = message
    return json.dumps(record)


@pytest.mark.parametrize("message", ["x", ["m"], 5, True])
def test_message_that_is_not_an_object_is_counted(tmp_path, message):
    path = _write(tmp_path / "t.jsonl", _assistant(message=message), _response_line("m1"))

    count, records = _counted(path)

    assert (count, len(records)) == (1, 1)


@pytest.mark.parametrize("message", [None, "__absent__"])
def test_absent_or_null_message_is_not_counted_and_not_read(tmp_path, message):
    path = _write(tmp_path / "t.jsonl", _assistant(message=message), _response_line("m1"))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


@pytest.mark.parametrize("usage", ["x", [1], 5, True])
def test_usage_that_is_not_an_object_is_counted(tmp_path, usage):
    path = _write(tmp_path / "t.jsonl", _assistant(usage), _response_line("m1"))

    count, records = _counted(path)

    assert (count, len(records)) == (1, 1)


@pytest.mark.parametrize("usage", [None, {}])
def test_null_and_empty_usage_stay_uncounted_and_unread(tmp_path, usage):
    path = _write(tmp_path / "t.jsonl", _assistant(usage), _response_line("m1"))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


def test_absent_usage_stays_uncounted_and_unread(tmp_path):
    path = _write(tmp_path / "t.jsonl", _assistant(), _response_line("m1"))

    assert _counted(path)[0] == 0
    assert len(_counted(path)[1]) == 1


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
@pytest.mark.parametrize("value", VALID_FIGURES)
def test_each_token_figure_accepts_zero_positive_absent_null(tmp_path, figure, value):
    path = _write(tmp_path / "t.jsonl", _assistant(_usage_with(figure, value)))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
def test_each_token_figure_may_be_left_out_entirely(tmp_path, figure):
    usage = _usage_with(figure, 1)
    head, _, tail = figure.partition(".")
    if tail:
        del usage[head][tail]
    else:
        del usage[head]
    path = _write(tmp_path / "t.jsonl", _assistant(usage))

    assert _counted(path)[0] == 0


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
@pytest.mark.parametrize("value", ["5", 5.5, 5.0, -1, True, False, [1], {"a": 1}])
def test_each_token_figure_rejects_string_fraction_exponent_negative_bool_array_object(
    tmp_path, figure, value
):
    path = _write(tmp_path / "t.jsonl", _assistant(_usage_with(figure, value)))

    count, records = _counted(path)

    assert (count, records) == (1, [])


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
def test_an_exponent_written_figure_is_unreadable(tmp_path, figure):
    line = _assistant(_usage_with(figure, 5)).replace(": 5,", ": 5e0,").replace(": 5}", ": 5e0}")
    assert "5e0" in line
    path = _write(tmp_path / "t.jsonl", line)

    assert _counted(path) == (1, [])


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
def test_a_figure_written_with_a_minus_sign_is_unreadable_even_at_zero(tmp_path, figure):
    """CP-413: readable is "a JSON integer of zero or more, written as digits alone (`0`, `5`)".
    `-0` is not written as digits alone, though it parses to Python's integer zero."""
    line = _assistant(_usage_with(figure, 5)).replace(": 5,", ": -0,").replace(": 5}", ": -0}")
    assert "-0" in line
    path = _write(tmp_path / "t.jsonl", line)

    assert _counted(path) == (1, [])


@pytest.mark.parametrize("figure", TOKEN_FIGURES)
@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_token_is_unreadable_not_an_integer(tmp_path, figure, value):
    path = _write(tmp_path / "t.jsonl", _assistant(_usage_with(figure, value)))

    assert _counted(path) == (1, [])


@pytest.mark.parametrize("container", ["cache_creation", "output_tokens_details"])
@pytest.mark.parametrize("value", [None, {}, {"x": 1}])
def test_cache_creation_and_output_tokens_details_accept_absent_null_object(
    tmp_path, container, value
):
    usage = _usage_with("input_tokens", 1)
    usage[container] = value
    path = _write(tmp_path / "t.jsonl", _assistant(usage))

    assert _counted(path)[0] == 0
    assert len(_counted(path)[1]) == 1


@pytest.mark.parametrize("container", ["cache_creation", "output_tokens_details"])
@pytest.mark.parametrize("value", ["x", [1], 5, True, False])
def test_cache_creation_and_output_tokens_details_reject_any_other_kind(
    tmp_path, container, value
):
    usage = _usage_with("input_tokens", 1)
    usage[container] = value
    path = _write(tmp_path / "t.jsonl", _assistant(usage))

    assert _counted(path) == (1, [])


@pytest.mark.parametrize("extra", ["x", [1], {"a": 1}, None, 5.5, True])
def test_other_usage_members_never_decide_readability(tmp_path, extra):
    usage = _usage_with("input_tokens", 1)
    usage["cache_creation_input_tokens"] = extra
    usage["service_tier"] = extra
    usage["server_tool_use"] = extra
    path = _write(tmp_path / "t.jsonl", _assistant(usage))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


@pytest.mark.parametrize("field", ["model", "id", "timestamp"])
@pytest.mark.parametrize("value", [["m"], 5, 5.5, True, {"a": 1}])
def test_model_id_timestamp_reject_any_kind_but_string_null_or_absent(tmp_path, field, value):
    usage = _usage_with("input_tokens", 1)
    if field == "timestamp":
        line = _assistant(usage, timestamp=value)
    else:
        line = _assistant(usage, message_extra={field: value})
    path = _write(tmp_path / "t.jsonl", line)

    assert _counted(path) == (1, [])


@pytest.mark.parametrize("field", ["model", "id", "timestamp"])
@pytest.mark.parametrize("value", [None, "text"])
def test_model_id_timestamp_accept_null_or_a_string(tmp_path, field, value):
    usage = _usage_with("input_tokens", 1)
    if field == "timestamp":
        line = _assistant(usage, timestamp=value)
    else:
        line = _assistant(usage, message_extra={field: value})
    path = _write(tmp_path / "t.jsonl", line)

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


def test_model_id_and_timestamp_may_be_absent(tmp_path):
    record = {"type": "assistant", "message": {"usage": _usage_with("input_tokens", 1)}}
    path = _write(tmp_path / "t.jsonl", json.dumps(record))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


def test_a_non_date_timestamp_string_is_accepted_and_left_out_of_the_span(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        _response_line("m1", timestamp="2026-09-25T09:00:00.000Z"),
        _response_line("m2", timestamp="not a date"),
        _response_line("m3", timestamp="2026-09-25T09:10:00.000Z"),
    )

    count, records = _counted(path)

    assert (count, len(records)) == (0, 3)
    assert run_usage.span_seconds(records) == 600


def test_a_date_with_no_offset_beside_dates_with_one_is_read_and_measured_as_utc(tmp_path):
    """CP-414: a `timestamp` that is a JSON string is readable and the record is read; only a
    string that is not a date is left out of the span. CP-412: no line stops the report. A date
    with no offset is a date, so it is in the span — placed as UTC, the offset the transcripts'
    own `Z` dates carry (the call is recorded in this change's code-integrity report)."""
    path = _write(
        tmp_path / "t.jsonl",
        _response_line("m1", timestamp="2026-09-25T09:00:00.000Z"),
        _response_line("m2", timestamp="2026-09-25T09:10:00"),
    )

    count, records = _counted(path)

    assert (count, len(records)) == (0, 2)
    assert run_usage.span_seconds(records) == 600


def test_dates_with_no_offset_alone_measure_as_they_did_before(tmp_path):
    """CP-425: input holding no line in the unreadable classes prints every figure as before."""
    path = _write(
        tmp_path / "t.jsonl",
        _response_line("m1", timestamp="2026-09-25T09:00:00"),
        _response_line("m2", timestamp="2026-09-25T09:10:00"),
    )

    assert run_usage.span_seconds(_read(path)) == 600


def test_a_rejected_record_adds_nothing_partial(tmp_path):
    rejected = _assistant(
        {
            "input_tokens": "bad",
            "output_tokens": 9,
            "cache_read_input_tokens": 9,
            "cache_creation": {"ephemeral_1h_input_tokens": 9, "ephemeral_5m_input_tokens": 9},
            "output_tokens_details": {"thinking_tokens": 9},
        },
        model="other-model",
        timestamp="2030-01-01T00:00:00.000Z",
    )
    clean = _write(
        tmp_path / "clean.jsonl",
        _response_line("m1", 4, timestamp="2026-09-25T09:00:00.000Z"),
        _response_line("m2", 5, timestamp="2026-09-25T09:10:00.000Z"),
    )
    dirty = _write(
        tmp_path / "dirty.jsonl",
        _response_line("m1", 4, timestamp="2026-09-25T09:00:00.000Z"),
        rejected,
        _response_line("m2", 5, timestamp="2026-09-25T09:10:00.000Z"),
    )

    count, records = _counted(dirty)
    spend = run_usage.spend_by_model(records)

    assert count == 1
    assert spend == run_usage.spend_by_model(_read(clean))
    assert "other-model" not in spend
    assert run_usage.span_seconds(records) == 600


def test_an_invalid_line_then_a_valid_line_with_the_same_id_counts_one_and_keeps_the_valid(
    tmp_path,
):
    invalid = _assistant(_usage_with("input_tokens", "5"), message_id="r1")
    valid = _assistant(_usage_with("input_tokens", 7), message_id="r1")
    path = _write(tmp_path / "t.jsonl", invalid, valid)

    count, records = _counted(path)

    assert count == 1
    assert [usage["input_tokens"] for usage, _m, _t in records] == [7]


def test_a_valid_line_then_an_invalid_line_with_the_same_id_counts_one_and_keeps_the_first_once(
    tmp_path,
):
    valid = _assistant(_usage_with("input_tokens", 7), message_id="r1")
    invalid = _assistant(_usage_with("input_tokens", "5"), message_id="r1")
    path = _write(tmp_path / "t.jsonl", valid, invalid)

    count, records = _counted(path)

    assert count == 1
    assert [usage["input_tokens"] for usage, _m, _t in records] == [7]


def test_several_invalid_lines_sharing_an_id_are_each_counted(tmp_path):
    invalid = _assistant(_usage_with("input_tokens", "5"), message_id="r1")
    path = _write(tmp_path / "t.jsonl", invalid, invalid, invalid)

    assert _counted(path) == (3, [])


def test_a_rejected_line_with_a_seen_response_id_is_still_counted(tmp_path):
    valid = _assistant(_usage_with("input_tokens", 7), message_id="r1")
    invalid = _assistant(_usage_with("input_tokens", "5"), message_id="r1")
    path = _write(tmp_path / "t.jsonl", valid, invalid, valid)

    count, records = _counted(path)

    assert (count, len(records)) == (1, 1)


def test_a_rejected_line_does_not_reserve_its_response_id(tmp_path):
    invalid = _assistant(_usage_with("input_tokens", "5"), message_id="r1")
    valid = _assistant(_usage_with("input_tokens", 7), message_id="r1")
    path = _write(tmp_path / "t.jsonl", invalid, valid, valid)

    count, records = _counted(path)

    assert (count, len(records)) == (1, 1)


def test_valid_duplicates_and_records_without_an_id_behave_as_before(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        _response_line("m1", 4),
        _response_line("m1", 4),
        _response_line(None, 6),
        _response_line(None, 6),
    )

    count, records = _counted(path)

    assert count == 0
    assert [usage["input_tokens"] for usage, _m, _t in records] == [4, 6, 6]


@pytest.mark.parametrize(
    "line",
    [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps({"type": "user", "message": "x", "timestamp": 5}),
        json.dumps({"type": "system", "message": {"usage": "bad"}}),
        json.dumps({"type": ["a"], "message": "x"}),
        "{}",
        json.dumps({"type": "assistant", "message": {"model": ["m"], "id": ["i"]}}),
        json.dumps({"type": "assistant", "timestamp": 5, "message": {"model": "m"}}),
    ],
)
def test_ordinary_objects_stay_uncounted_and_unread(tmp_path, line):
    path = _write(tmp_path / "t.jsonl", line, _response_line("m1"))

    count, records = _counted(path)

    assert (count, len(records)) == (0, 1)


def test_thinking_absent_versus_zero_and_token_fallback_unchanged(tmp_path):
    no_thinking = _assistant(
        {"input_tokens": 3, "output_tokens": None, "cache_creation": None}, model="a"
    )
    zero_thinking = _assistant(
        {"input_tokens": 3, "output_tokens_details": {"thinking_tokens": 0}}, model="b"
    )
    path = _write(tmp_path / "t.jsonl", no_thinking, zero_thinking)

    count, records = _counted(path)
    spend = run_usage.spend_by_model(records)

    assert count == 0
    assert spend["a"].thinking is None and spend["a"].output == 0
    assert spend["b"].thinking == 0


def test_blank_empty_and_all_rejected_files_yield_nothing_and_count_correctly(tmp_path):
    empty = _write(tmp_path / "empty.jsonl")
    blank = _write(tmp_path / "blank.jsonl", "", "  ", "\t")
    rejected = _write(tmp_path / "rejected.jsonl", "[1]", "null", BAD, _assistant(message="x"))

    assert _counted(empty) == (0, [])
    assert _counted(blank) == (0, [])
    assert _counted(rejected) == (4, [])


def test_usage_records_without_a_counter_still_skips_rejected_lines(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        "[1]",
        _assistant(message="x"),
        _assistant(_usage_with("input_tokens", "5")),
        _response_line("m1", 4),
    )

    records = _read(path)

    assert [usage["input_tokens"] for usage, _m, _t in records] == [4]


# ── The whole-file observation — a transcript that could not be read at all ──


class _UnreadableFiles(run_usage.Files):
    """A port whose every read fails, the way `Files.read_lines` reports a file it cannot open."""

    def read_lines(self, path):
        return None


class _SpyFiles(run_usage.Files):
    def __init__(self):
        self.read_paths = []

    def read_lines(self, path):
        self.read_paths.append(path)
        return super().read_lines(path)


def test_a_missing_file_gives_one_unreadable_file_and_zero_lines(tmp_path):
    counter = run_usage.UnreadableLines()

    assert _read(tmp_path / "absent.jsonl", counter=counter) == []
    assert (counter.unreadable_files, counter.count) == (1, 0)


def test_an_unopenable_file_gives_one_unreadable_file_and_zero_lines(tmp_path):
    path = _write(tmp_path / "t.jsonl", _response_line("m1"))
    counter = run_usage.UnreadableLines()

    assert _read(path, files=_UnreadableFiles(), counter=counter) == []
    assert (counter.unreadable_files, counter.count) == (1, 0)


def test_a_file_that_is_a_directory_is_an_unreadable_file(tmp_path):
    (tmp_path / "t.jsonl").mkdir()
    counter = run_usage.UnreadableLines()

    _read(tmp_path / "t.jsonl", counter=counter)

    assert counter.unreadable_files == 1


def test_a_readable_empty_file_has_no_unreadable_file(tmp_path):
    counter = run_usage.UnreadableLines()

    _read(_write(tmp_path / "t.jsonl"), counter=counter)

    assert (counter.unreadable_files, counter.count) == (0, 0)


def test_an_all_rejected_readable_file_has_no_unreadable_file(tmp_path):
    path = _write(tmp_path / "t.jsonl", BAD, "[1]", _assistant(message="x"))
    counter = run_usage.UnreadableLines()

    _read(path, counter=counter)

    assert (counter.unreadable_files, counter.count) == (0, 3)


def test_unreadable_files_never_adds_to_count(tmp_path):
    counter = run_usage.UnreadableLines()

    _read(tmp_path / "absent.jsonl", counter=counter)
    _read(_write(tmp_path / "t.jsonl", BAD), counter=counter)

    assert (counter.unreadable_files, counter.count) == (1, 1)


def test_a_counter_reused_across_readings_accumulates_both_observations_independently(tmp_path):
    counter = run_usage.UnreadableLines()
    two_lines = _write(tmp_path / "two.jsonl", BAD, "[1]")

    _read(two_lines, counter=counter)
    _read(tmp_path / "absent-1.jsonl", counter=counter)
    _read(tmp_path / "absent-2.jsonl", counter=counter)
    _read(two_lines, counter=counter)

    assert (counter.count, counter.unreadable_files) == (4, 2)


def test_usage_records_reads_the_port_once_per_transcript(tmp_path):
    path = _write(tmp_path / "t.jsonl", BAD, _response_line("m1"))
    files = _SpyFiles()

    _read(path, files=files, counter=run_usage.UnreadableLines())
    absent = tmp_path / "absent.jsonl"
    _read(absent, files=files, counter=run_usage.UnreadableLines())

    assert files.read_paths == [path, absent]


def test_the_counter_completes_lazily_after_the_iterator_is_consumed(tmp_path):
    counter = run_usage.UnreadableLines()
    iterator = run_usage.usage_records(
        tmp_path / "absent.jsonl", run_usage.Files(), unreadable_lines=counter
    )

    assert counter.unreadable_files == 0
    assert list(iterator) == []
    assert counter.unreadable_files == 1


def test_legacy_construction_of_unreadable_lines_still_works():
    assert run_usage.UnreadableLines().unreadable_files == 0
    assert run_usage.UnreadableLines(3).count == 3
    assert run_usage.UnreadableLines(count=3).unreadable_files == 0
    assert [f.name for f in dataclasses.fields(run_usage.UnreadableLines)] == [
        "count",
        "unreadable_files",
    ]


def test_runs_coverage_text_and_json_gain_no_file_count(tmp_path):
    _transcript(tmp_path, "sid-a", BAD, _response_line("m1"))
    rows = [_row(sessionId=sid) for sid in ("sid-a", "sid-gone")]

    led = run_usage.read_runs(_agents_json(*rows), [], run_usage.build_reader(tmp_path))

    assert "unreadable_files" not in run_usage.render(led)
    assert "unreadable_files" not in run_usage.render_json(led)
    assert "unreadable_files" not in [f.name for f in dataclasses.fields(run_usage.Coverage)]


def test_usage_records_without_a_counter_behaves_as_before(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        _response_line("m1", 4),
        BAD,
        _response_line("m1", 4),
        _response_line("m2", 5),
        _response_line(None, 6),
    )
    counted = run_usage.UnreadableLines()

    assert _read(path) == _read(path, counter=counted)
    assert [usage["input_tokens"] for usage, _m, _t in _read(path)] == [4, 5, 6]
    assert _read(tmp_path / "absent.jsonl") == []


def test_a_missing_transcript_counts_nothing(tmp_path):
    counter = run_usage.UnreadableLines()

    assert _read(tmp_path / "absent.jsonl", counter=counter) == []
    assert counter.count == 0


def test_counter_is_complete_only_after_the_iterator_is_exhausted(tmp_path):
    path = _write(
        tmp_path / "t.jsonl", _response_line("m1"), BAD, _response_line("m2"), BAD, BAD
    )
    counter = run_usage.UnreadableLines()
    iterator = run_usage.usage_records(path, run_usage.Files(), unreadable_lines=counter)

    next(iterator)
    assert counter.count == 0
    next(iterator)
    assert counter.count == 1
    assert list(iterator) == []
    assert counter.count == 3


def test_counter_is_not_shared_across_readings(tmp_path):
    two = _write(tmp_path / "two.jsonl", BAD, _response_line("m1"), BAD)
    one = _write(tmp_path / "one.jsonl", _response_line("m1"), BAD)
    first, second = run_usage.UnreadableLines(), run_usage.UnreadableLines()

    _read(two, counter=first)
    _read(one, counter=second)
    _read(two)

    assert (first.count, second.count) == (2, 1)


def test_bad_lines_change_no_token_figure_or_span(tmp_path):
    clean = _write(
        tmp_path / "clean.jsonl",
        _response_line("m1", 4, timestamp="2026-09-25T09:00:00.000Z"),
        _response_line("m2", 5, timestamp="2026-09-25T09:10:00.000Z"),
    )
    dirty = _write(
        tmp_path / "dirty.jsonl",
        BAD,
        _response_line("m1", 4, timestamp="2026-09-25T09:00:00.000Z"),
        BAD,
        _response_line("m2", 5, timestamp="2026-09-25T09:10:00.000Z"),
        BAD,
    )
    clean_records, dirty_records = _read(clean), _read(dirty, counter=run_usage.UnreadableLines())

    assert run_usage.spend_by_model(dirty_records) == run_usage.spend_by_model(clean_records)
    assert run_usage.span_seconds(dirty_records) == run_usage.span_seconds(clean_records) == 600


def test_transcript_is_read_once_for_figures_and_count(tmp_path):
    project = _project(tmp_path)
    transcript = _write(project / "sid-1.jsonl", BAD, _response_line("m1", 4), BAD)

    class SpyFiles(run_usage.Files):
        def __init__(self):
            self.read_paths = []

        def read_lines(self, path):
            self.read_paths.append(path)
            return super().read_lines(path)

    files = SpyFiles()
    led = run_usage.ledger([_run("sid-1")], tmp_path, files)

    assert files.read_paths == [transcript]
    assert led.coverage.unreadable_lines == 2
    assert led.rows[0].spend["claude-sonnet-5"].input == 4


def test_read_runs_coverage_carries_the_total_over_every_transcript_read(tmp_path):
    _transcript(tmp_path, "sid-a", BAD, _response_line("m1"), BAD)
    _transcript(tmp_path, "sid-b", _response_line("m1"), BAD, BAD, BAD)
    _transcript(tmp_path, "sid-c", _response_line("m1"))
    rows = [_row(sessionId=sid) for sid in ("sid-a", "sid-b", "sid-c", "sid-gone")]

    led = run_usage.read_runs(_agents_json(*rows), [], run_usage.build_reader(tmp_path))

    assert led.coverage.unreadable_lines == 5
    assert (led.coverage.read, led.coverage.unreadable) == (3, 1)
    assert led.coverage.unreadable_ids == ("sid-gone",)


def test_coverage_and_legacy_constructors_default_the_count_to_zero():
    legacy = run_usage.Coverage(read=1, unreadable=0, total=1, unreadable_ids=())

    assert legacy.unreadable_lines == 0
    assert legacy == run_usage.Coverage(
        read=1, unreadable=0, total=1, unreadable_ids=(), unreadable_lines=0
    )
    assert [f.name for f in dataclasses.fields(run_usage.Coverage)][-1] == "unreadable_lines"


def test_a_ledger_over_clean_transcripts_counts_no_unreadable_lines(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"), "")

    led = run_usage.ledger([_run("sid-a")], tmp_path, run_usage.Files())

    assert led.coverage.unreadable_lines == 0


# ── `runs` states what it holds — one no-cost line, pointers where matched, unreadable lines ──


SESSION_A = "a839285c-263f-40a6-8801-71f49d00cc68"
SESSION_B = "174d0111-bfa9-4539-92d9-5560e0ab4c36"


def _stream_home(tmp_path, *session_ids, extra_lines=()):
    """A `HOME` whose `.ka-telemetry/logs.jsonl` holds one `api_request` for each session id."""
    home = tmp_path / "stream-home"
    stream = home / ".ka-telemetry"
    stream.mkdir(parents=True)
    lines = [json.dumps({"body": api_request_body(sid, 0.5)}) for sid in session_ids]
    (stream / "logs.jsonl").write_text("".join(f"{line}\n" for line in (*lines, *extra_lines)))
    return home


def _text(tmp_path, capsys, rows=(), sessions=()):
    status, out, err = _main(_runs_argv(tmp_path, list(rows), sessions, json_form=False), capsys)
    assert (status, err) == (0, "")
    return out


def _two_runs(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    _transcript(tmp_path, "sid-b", _response_line("m2"))
    return [_row(sessionId="sid-a"), _row(sessionId="sid-b")]


def _count_no_cost(text):
    return len(re.findall("carries no cost", text, flags=re.IGNORECASE))


@pytest.mark.parametrize("shape", ["nothing", "one-read", "one-unreadable", "mixed"])
def test_text_report_ends_with_the_no_cost_line_exactly_once(shape, tmp_path, capsys):
    if shape != "nothing":
        _transcript(tmp_path, "sid-a", _response_line("m1"))
    rows = {
        "nothing": [],
        "one-read": [_row(sessionId="sid-a")],
        "one-unreadable": [_row(sessionId="sid-gone")],
        "mixed": [_row(sessionId="sid-a"), _row(sessionId="sid-gone")],
    }[shape]

    out = _text(tmp_path, capsys, rows)

    assert out.endswith(f"{NO_COST}\n")
    assert _count_no_cost(out) == 1


def test_empty_report_prints_no_runs_read_and_one_no_cost_line(tmp_path, capsys):
    absent_root = tmp_path / "absent"
    argv = ["--agents-json", _write_agents_json(tmp_path), "--projects-root", str(absent_root)]
    _status, nonexistent_root, _err = _main(argv, capsys)
    zero_readable = _text(tmp_path, capsys, [_row(sessionId="sid-gone")])

    for out in (nonexistent_root, zero_readable):
        assert out.endswith(f"No runs read.\n{NO_COST}\n")
        assert _count_no_cost(out) == 1


def test_no_cost_line_is_one_per_report_not_per_run(tmp_path, capsys):
    out = _text(tmp_path, capsys, _two_runs(tmp_path))

    assert _count_no_cost(out) == 1
    assert out.count("  started=") == 2


def test_no_cost_line_follows_the_last_row_with_no_blank_line_between(tmp_path, capsys):
    lines = _text(tmp_path, capsys, _two_runs(tmp_path)).splitlines()

    assert lines[-1] == NO_COST
    assert lines[-2].startswith("  claude-sonnet-5:")


def test_pointer_lines_sit_directly_under_the_no_cost_line_one_per_matched_session_in_report_order(
    tmp_path, capsys, monkeypatch
):
    rows = _two_runs(tmp_path)
    # the stream lists the later run first, so stream order is not report order
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-b", "sid-a", "sid-b")))

    lines = _text(tmp_path, capsys, rows).splitlines()

    assert lines[-3:] == [NO_COST, _pointer("sid-a"), _pointer("sid-b")]
    assert _count_no_cost("\n".join(lines)) == 1


def test_unmatched_session_gets_no_pointer_and_nothing_in_its_place(tmp_path, capsys, monkeypatch):
    rows = _two_runs(tmp_path)
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-a", SESSION_B)))
    with_stream = _text(tmp_path, capsys, rows)
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path / "other", SESSION_B)))
    with_no_match = _text(tmp_path, capsys, rows)

    assert with_stream.splitlines()[-2:] == [NO_COST, _pointer("sid-a")]
    assert with_no_match.splitlines()[-1] == NO_COST
    assert "ka-telemetry agents" not in with_no_match
    assert with_no_match.endswith(f"{NO_COST}\n")


def test_run_that_could_not_be_read_gets_no_pointer_though_matched_in_the_stream(
    tmp_path, capsys, monkeypatch
):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    rows = [_row(sessionId="sid-a"), _row(sessionId="sid-gone")]
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-a", "sid-gone")))

    out = _text(tmp_path, capsys, rows)

    assert "sid-gone" in out.splitlines()[2]
    assert out.splitlines()[-2:] == [NO_COST, _pointer("sid-a")]
    assert "session.id=sid-gone" not in out


def test_readable_empty_usage_transcript_has_a_row_and_may_get_a_pointer(
    tmp_path, capsys, monkeypatch
):
    _transcript(tmp_path, "sid-a", json.dumps({"type": "user", "message": {}}))
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-a")))

    out = _text(tmp_path, capsys, [_row(sessionId="sid-a")])

    assert "  no usage records" in out
    assert out.splitlines()[-2:] == [NO_COST, _pointer("sid-a")]


def test_reader_without_notes_reader_prints_no_pointer_and_reads_no_stream(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    reader = run_usage.Reader(
        files=run_usage.Files(), processes=RaisingProcesses(), projects_root=tmp_path
    )
    led = run_usage.read_runs(_agents_json(_row(sessionId="sid-a")), [], reader)

    assert reader.notes_reader is None
    assert run_usage.report_matches(led, reader) == ()
    assert run_usage.render(led).endswith(f"{NO_COST}\n")
    assert "ka-telemetry agents" not in run_usage.render(led)


def test_report_matches_asks_for_the_rows_the_report_prints_in_row_order(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    _transcript(tmp_path, "sid-b", _response_line("m1"))
    rows = [_row(sessionId="sid-b"), _row(sessionId="sid-a"), _row(sessionId="sid-gone")]
    stream_files = FakeStreamFiles(
        [json.dumps({"body": api_request_body(s, 1.0)}) for s in ("sid-a", "sid-b", "sid-gone")]
    )
    reader = dataclasses.replace(
        run_usage.build_reader(tmp_path),
        notes_reader=stream_usage.Reader(files=stream_files, directory=Path("/stream")),
    )
    led = run_usage.read_runs(_agents_json(*rows), [], reader)

    assert run_usage.report_matches(led, reader) == ("sid-b", "sid-a")
    assert len(stream_files.read_paths) == 1


def test_build_reader_wires_the_stream_reader_for_the_default_directory():
    reader = run_usage.build_reader()

    assert isinstance(reader.notes_reader, stream_usage.Reader)
    assert reader.notes_reader.directory == Path(os.environ["HOME"]) / ".ka-telemetry"


def _seal_logs(home, seal):
    seal(home / ".ka-telemetry" / "logs.jsonl")


@pytest.mark.parametrize("problem", ["absent-directory", "empty-logs", "bad-lines", "unopenable"])
def test_stream_problems_change_nothing_but_the_pointers(
    problem, tmp_path, capsys, monkeypatch, seal
):
    rows = _two_runs(tmp_path)
    baseline = _text(tmp_path, capsys, rows)
    if problem == "absent-directory":
        home = tmp_path / "no-stream-here"
        home.mkdir()
    elif problem == "empty-logs":
        home = _stream_home(tmp_path)
    elif problem == "bad-lines":
        home = _stream_home(tmp_path, extra_lines=("not json {", "[1, 2]", "{}"))
    else:
        home = _stream_home(tmp_path, "sid-a")
        _seal_logs(home, seal)
    monkeypatch.setenv("HOME", str(home))

    status, out, err = _main(_runs_argv(tmp_path, rows, json_form=False), capsys)

    assert (status, out, err) == (0, baseline, "")


def test_json_form_has_no_cost_word_no_note_member_and_reads_no_stream(
    tmp_path, capsys, monkeypatch
):
    rows = _two_runs(tmp_path)
    reads = []
    original = stream_usage.Files.read_outcome

    def spy(self, path):
        reads.append(path)
        return original(self, path)

    monkeypatch.setattr(stream_usage.Files, "read_outcome", spy)
    empty_json = _main(_runs_argv(tmp_path, rows), capsys)[1]
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-a", "sid-b")))
    matched_json = _main(_runs_argv(tmp_path, rows), capsys)[1]

    assert reads == []
    assert matched_json == empty_json
    assert "cost" not in matched_json.lower()
    assert set(json.loads(matched_json)) == {"population", "span", "coverage", "rows"}


# ── the Coverage line and the `--json` member that count unreadable lines ──


def _coverage(**overrides):
    fields = {"read": 3, "unreadable": 0, "total": 3, "unreadable_ids": (), "listed": 3}
    fields.update(overrides)
    return run_usage.Coverage(**fields)


def _text_of(coverage):
    return run_usage.render(run_usage.Ledger(rows=(), coverage=coverage)).splitlines()[2]


def test_coverage_line_is_byte_identical_at_zero_unreadable_lines():
    clean = _coverage()
    with_ids = _coverage(read=2, unreadable=1, unreadable_ids=("sid-x",))

    assert _text_of(clean) == "Coverage: 3 read / 0 unreadable / 3 total (3 listed, 0 named)"
    assert _text_of(with_ids) == (
        "Coverage: 2 read / 1 unreadable / 3 total (3 listed, 0 named) — unreadable: sid-x"
    )


def test_coverage_line_keeps_every_word_and_figure_in_order_and_adds_the_count_when_positive():
    base = "Coverage: 3 read / 0 unreadable / 3 total (3 listed, 0 named) — "
    with_ids_base = (
        "Coverage: 2 read / 1 unreadable / 3 total (3 listed, 0 named) — unreadable: sid-x, sid-y"
        " — "
    )
    ids = {"unreadable_ids": ("sid-x", "sid-y"), "read": 2, "unreadable": 1}

    assert _text_of(_coverage(unreadable_lines=1)) == (
        base + "1 unreadable line skipped in the transcripts read"
    )
    assert _text_of(_coverage(unreadable_lines=4)) == (
        base + "4 unreadable lines skipped in the transcripts read"
    )
    assert _text_of(_coverage(unreadable_lines=1, **ids)) == (
        with_ids_base + "1 unreadable line skipped in the transcripts read"
    )
    assert _text_of(_coverage(unreadable_lines=12, **ids)) == (
        with_ids_base + "12 unreadable lines skipped in the transcripts read"
    )


def test_json_coverage_gains_unreadable_lines_only_when_positive():
    def coverage_json(**overrides):
        led = run_usage.Ledger(rows=(), coverage=_coverage(**overrides))
        return json.loads(run_usage.render_json(led))["coverage"]

    baseline = coverage_json()

    assert "unreadable_lines" not in baseline
    assert coverage_json(unreadable_lines=0) == baseline
    assert coverage_json(unreadable_lines=1) == {**baseline, "unreadable_lines": 1}
    assert coverage_json(unreadable_lines=7) == {**baseline, "unreadable_lines": 7}


def test_json_row_fields_carry_no_count():
    row = _ledger_row(spend={"m": run_usage.ModelSpend(input=1, output=1)})
    led = run_usage.Ledger(rows=(row,), coverage=_coverage(read=1, total=1, unreadable_lines=2))

    printed = json.loads(run_usage.render_json(led))["rows"][0]

    assert set(printed) == {
        "session_id", "name", "cwd", "state", "started_at", "span_seconds", "spend"
    }


def test_render_appends_the_note_block_it_is_given_after_the_whole_report():
    led = run_usage.Ledger(rows=(_ledger_row("sid-1"),), coverage=_coverage(read=1, total=1))

    lines = run_usage.render(led, matched_sessions=("sid-1", "sid-9")).splitlines()

    assert lines[-3:] == [NO_COST, _pointer("sid-1"), _pointer("sid-9")]
    assert run_usage.render(led).splitlines()[-1] == NO_COST


def test_an_unreadable_line_in_a_transcript_reaches_the_command_output(tmp_path, capsys):
    _transcript(tmp_path, "sid-a", "not json {", _response_line("m1"))
    rows = [_row(sessionId="sid-a")]

    out = _text(tmp_path, capsys, rows)
    payload = json.loads(_main(_runs_argv(tmp_path, rows), capsys)[1])

    assert out.splitlines()[2] == (
        "Coverage: 1 read / 0 unreadable / 1 total (1 listed, 0 named) — "
        "1 unreadable line skipped in the transcripts read"
    )
    assert payload["coverage"]["unreadable_lines"] == 1


# ── A `claude` that cannot be started is one `error:` line ──────────────

REMEDY_TAIL = (
    "pass --agents-json a file holding a saved copy of `claude agents --json --all` output"
)
NOT_FOUND_ERROR = (
    "error: could not start `claude agents --json --all` because the system reported that "
    "`claude` was not found — make sure Claude Code's `claude` command is installed and on "
    f"PATH, or {REMEDY_TAIL}"
)


def _other_reason_error(reason):
    return (
        "error: could not start `claude agents --json --all` — make sure Claude Code's `claude` "
        f"command is installed, on PATH and allowed to run, or {REMEDY_TAIL}. "
        f"The system reported: {reason}"
    )


def _raising_start(monkeypatch, error):
    """The one process-start call in `Processes.run`, made to fail as the system would."""

    def start(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(run_usage.subprocess, "run", start)


def _run_command(tmp_path, *arguments, path_dir=None):
    """`ka-telemetry runs` as a process: an absolute interpreter, an empty `HOME`, and a `PATH`
    that holds only `path_dir` (an empty directory by default)."""
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_dir = path_dir if path_dir is not None else tmp_path / "empty-path"
    bin_dir.mkdir(exist_ok=True)
    product = Path(run_usage.__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-B", "-m", "ka_telemetry", "runs", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(home), "PATH": str(bin_dir), "PYTHONPATH": str(product)},
        cwd=home,
    )


def test_missing_claude_is_one_error_line_with_no_traceback_and_exit_1(tmp_path):
    done = _run_command(tmp_path)

    assert done.stderr == NOT_FOUND_ERROR + "\n"
    assert "Traceback" not in done.stderr
    assert done.stdout == ""
    assert done.returncode == 1


@pytest.mark.parametrize(
    "flags",
    [["--session", "sid-1"], ["--projects-root", "PROJECTS"], ["--json"],
     ["--session", "sid-1", "--projects-root", "PROJECTS", "--json"]],
)
def test_missing_claude_holds_with_session_projects_root_and_json_flags(tmp_path, flags):
    flags = [str(tmp_path) if flag == "PROJECTS" else flag for flag in flags]

    done = _run_command(tmp_path, *flags)

    assert done.stderr == NOT_FOUND_ERROR + "\n"
    assert (done.stdout, done.returncode) == ("", 1)


def test_start_failure_other_than_not_found_states_the_systems_reason(tmp_path):
    claude = tmp_path / "bin"
    claude.mkdir()
    (claude / "claude").write_text("#!/bin/sh\necho []\n")  # present, but not executable

    done = _run_command(tmp_path, path_dir=claude)

    assert done.stderr == _other_reason_error("Permission denied") + "\n"
    assert "was not found" not in done.stderr
    assert (done.stdout, done.returncode) == ("", 1)


def test_start_failure_reason_is_the_systems_own_text_when_it_carries_no_strerror(monkeypatch):
    _raising_start(monkeypatch, OSError("exec format error"))

    with pytest.raises(SystemExit) as failed:
        run_usage.Processes().run(["claude", "agents", "--json", "--all"])

    assert str(failed.value) == _other_reason_error("exec format error")


def test_multiline_start_reason_stays_one_line(monkeypatch):
    _raising_start(monkeypatch, PermissionError("first line\nsecond line\n\n  third"))

    with pytest.raises(SystemExit) as failed:
        run_usage.Processes().run(["claude", "agents", "--json", "--all"])

    assert str(failed.value) == _other_reason_error("first line second line third")
    assert "\n" not in str(failed.value)


def test_a_file_not_found_start_failure_is_the_not_found_line(monkeypatch):
    _raising_start(monkeypatch, FileNotFoundError(2, "No such file or directory", "claude"))

    with pytest.raises(SystemExit) as failed:
        run_usage.Processes().run(["claude", "agents", "--json", "--all"])

    assert str(failed.value) == NOT_FOUND_ERROR


def test_saved_agents_json_bypasses_process_start(tmp_path, monkeypatch, capsys):
    _raising_start(monkeypatch, AssertionError("no process may be started"))
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    status, _out, err = _main(_runs_argv(tmp_path, [_row(sessionId="sid-a")]), capsys)

    assert (status, err) == (0, "")


def test_started_process_exiting_non_zero_keeps_its_message(tmp_path):
    claude = _fake_claude_dir(tmp_path, "echo boom >&2\nexit 3\n")

    done = _run_command(tmp_path, path_dir=claude)

    assert done.stderr == (
        "error: `claude agents --json --all` exited 3 — fix what it reported, or pass "
        "--agents-json a file holding its saved output. It reported: boom\n"
    )
    assert (done.stdout, done.returncode) == ("", 1)


def _fake_claude_dir(tmp_path, body):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return bin_dir


def test_timeout_of_a_started_process_is_not_caught_by_the_start_handler(monkeypatch):
    import subprocess

    _raising_start(monkeypatch, subprocess.TimeoutExpired(["claude"], 60))

    with pytest.raises(subprocess.TimeoutExpired):
        run_usage.Processes().run(["claude", "agents", "--json", "--all"])


# ── The transcript read outcome — absent, unopenable, or read ──


class _FailingPath:
    """A path whose one read raises `error`, so a failure kind that a real file cannot be made to
    produce without file modes is injected rather than built."""

    def __init__(self, error: OSError):
        self._error = error
        self.reads = 0

    def read_text(self, **_kwargs):
        self.reads += 1
        raise self._error


def test_read_outcome_of_a_populated_file_holds_its_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("a\nb\n")

    outcome = run_usage.Files().read_outcome(path)

    assert outcome.lines == ("a", "b")
    assert outcome.missing is False


def test_read_outcome_of_an_empty_file_is_a_read(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("")

    outcome = run_usage.Files().read_outcome(path)

    assert outcome.lines == ()
    assert outcome.lines is not None
    assert outcome.missing is False


def test_read_outcome_of_an_absent_path_is_missing(tmp_path):
    outcome = run_usage.Files().read_outcome(tmp_path / "absent.jsonl")

    assert outcome.lines is None
    assert outcome.missing is True


def test_read_outcome_of_a_path_under_a_regular_file_is_missing(tmp_path):
    regular = tmp_path / "regular"
    regular.write_text("x")

    outcome = run_usage.Files().read_outcome(regular / "x.jsonl")

    assert outcome.lines is None
    assert outcome.missing is True


def test_read_outcome_of_a_directory_is_not_missing(tmp_path):
    directory = tmp_path / "t.jsonl"
    directory.mkdir()

    outcome = run_usage.Files().read_outcome(directory)

    assert outcome.lines is None
    assert outcome.missing is False


def test_read_outcome_of_other_os_errors_is_not_missing():
    path = _FailingPath(PermissionError("denied"))

    outcome = run_usage.Files().read_outcome(path)

    assert outcome.lines is None
    assert outcome.missing is False
    assert path.reads == 1


def test_read_outcome_of_a_late_failure_holds_no_partial_lines():
    # A whole-file read either returns all its text or raises: whatever the failing read had
    # produced before it raised never reaches the outcome.
    path = _FailingPath(OSError(5, "input/output error"))

    outcome = run_usage.Files().read_outcome(path)

    assert outcome == run_usage.TranscriptRead(lines=None, missing=False)


def test_read_outcome_decodes_with_replacement(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"ok\n\xff\xfebad\n")
    files = run_usage.Files()

    outcome = files.read_outcome(path)

    assert outcome.lines == tuple(files.read_lines(path))
    assert "\ufffd" in outcome.lines[1]


def test_transcript_read_is_immutable():
    outcome = run_usage.TranscriptRead(lines=("a",), missing=False)

    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.missing = True


def test_read_lines_results_are_unchanged(tmp_path):
    populated = tmp_path / "t.jsonl"
    populated.write_text("a\nb\n")
    directory = tmp_path / "d.jsonl"
    directory.mkdir()
    files = run_usage.Files()

    assert files.read_lines(populated) == ["a", "b"]
    assert files.read_lines(tmp_path / "absent.jsonl") is None
    assert files.read_lines(directory) is None


def test_a_read_lines_only_port_still_drives_usage_records(tmp_path):
    path = tmp_path / "t.jsonl"
    fake = FakeFiles(lines={path: [_response_line("m1")]})

    assert len(list(run_usage.usage_records(path, fake))) == 1


# ── A transcript that is found and cannot be opened is an unreadable run ──


def _directory_in_place_of_transcript(root, session_id, slug="proj"):
    directory = root / slug / f"{session_id}.jsonl"
    directory.mkdir(parents=True)
    return directory


def test_found_transcript_that_cannot_be_read_is_an_unreadable_run():
    found = Path("/projects/proj/sid-1.jsonl")
    files = FakeFiles(transcripts={"sid-1": [found]})  # found, but `read_lines` answers `None`

    led = run_usage.ledger([_run("sid-1")], Path("/projects"), files)

    assert led.rows == ()
    assert (led.coverage.read, led.coverage.unreadable, led.coverage.total) == (0, 1, 1)
    assert led.coverage.unreadable_ids == ("sid-1",)
    assert led.coverage.unreadable_lines == 0


def _two_worlds(tmp_path):
    """Two projects roots with the same readable run `sid-a`: in `directory`, `sid-b`'s and
    `sid-c`'s transcripts are directories; in `removed`, they are absent."""
    worlds = {}
    for name in ("directory", "removed"):
        root = tmp_path / name
        _transcript(root, "sid-a", _response_line("m1"))
        worlds[name] = root
    for session_id in ("sid-b", "sid-c"):
        _directory_in_place_of_transcript(worlds["directory"], session_id)
    return worlds["directory"], worlds["removed"]


def _without_reasons(text):
    return re.sub(r" \((gone|could not be opened)\)", "", text)


def _json_without_reasons(ledger):
    document = json.loads(run_usage.render_json(ledger))
    document["coverage"].pop("unreadable_reasons", None)
    return document


def test_real_directory_in_place_of_transcript_equals_the_removed_transcript(tmp_path):
    directory, removed = _two_worlds(tmp_path)
    listed = [_row(sessionId="sid-a"), _row(sessionId="sid-b")]

    runs = [_run("sid-a"), _run("sid-b"), _run("sid-c")]
    on_directory = run_usage.ledger(runs, directory, run_usage.Files())
    on_removed = run_usage.ledger(runs, removed, run_usage.Files())
    # They differ only in why the two runs are unreadable: a transcript that could not be
    # opened is not one that is gone (CP-443).
    assert dataclasses.replace(on_directory, unreadable_reasons=()) == dataclasses.replace(
        on_removed, unreadable_reasons=()
    )
    assert on_directory.unreadable_reasons == (REASON.COULD_NOT_BE_OPENED,) * 2
    assert on_removed.unreadable_reasons == (REASON.GONE,) * 2
    for named in ([], ["sid-c"], ["sid-b", "sid-c"]):
        # `sid-b` is both listed and named in the last form, and `sid-c` is named only.
        on_directory = run_usage.read_runs(
            _agents_json(*listed), named, run_usage.build_reader(directory)
        )
        on_removed = run_usage.read_runs(
            _agents_json(*listed), named, run_usage.build_reader(removed)
        )
        assert dataclasses.replace(on_directory, unreadable_reasons=()) == dataclasses.replace(
            on_removed, unreadable_reasons=()
        )
        # The two renderings differ only in the reason each prints beside an unreadable id.
        assert _without_reasons(run_usage.render(on_directory)) == _without_reasons(
            run_usage.render(on_removed)
        )
        assert _json_without_reasons(on_directory) == _json_without_reasons(on_removed)
        assert "sid-b" in on_directory.coverage.unreadable_ids


def test_unreadable_ids_keep_run_order_among_readable_runs(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    _transcript(tmp_path, "sid-d", _response_line("m2"))
    _directory_in_place_of_transcript(tmp_path, "sid-b")
    _directory_in_place_of_transcript(tmp_path, "sid-e")
    runs = [_run(sid) for sid in ("sid-e", "sid-a", "sid-b", "sid-d", "sid-gone")]

    led = run_usage.ledger(runs, tmp_path, run_usage.Files())

    assert [row.session_id for row in led.rows] == ["sid-a", "sid-d"]
    assert led.coverage.unreadable_ids == ("sid-e", "sid-b", "sid-gone")
    assert led.coverage.unreadable == 3


def test_unreadable_transcript_has_no_pointer_line_even_when_a_stream_matches(tmp_path):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    _directory_in_place_of_transcript(tmp_path, "sid-b")
    stream_files = FakeStreamFiles(
        [json.dumps({"body": api_request_body(s, 1.0)}) for s in ("sid-a", "sid-b")]
    )
    reader = dataclasses.replace(
        run_usage.build_reader(tmp_path),
        notes_reader=stream_usage.Reader(files=stream_files, directory=Path("/stream")),
    )
    rows = [_row(sessionId="sid-b"), _row(sessionId="sid-a")]

    led = run_usage.read_runs(_agents_json(*rows), [], reader)

    assert run_usage.report_matches(led, reader) == ("sid-a",)
    assert _pointer("sid-b") not in run_usage.render(led)


def test_readable_empty_and_all_rejected_transcripts_keep_their_rows_and_counts(tmp_path):
    _transcript(tmp_path, "sid-empty")
    _transcript(tmp_path, "sid-rejected", BAD, "[1]")
    _directory_in_place_of_transcript(tmp_path, "sid-dir")
    runs = [_run("sid-empty"), _run("sid-rejected"), _run("sid-dir")]

    led = run_usage.ledger(runs, tmp_path, run_usage.Files())

    assert [row.session_id for row in led.rows] == ["sid-empty", "sid-rejected"]
    assert all(row.spend == {} for row in led.rows)
    assert (led.coverage.read, led.coverage.unreadable) == (2, 1)
    assert led.coverage.unreadable_lines == 2


def test_unreadable_transcript_does_not_disturb_other_runs_unreadable_line_total(tmp_path):
    _transcript(tmp_path, "sid-a", BAD, _response_line("m1"))
    _directory_in_place_of_transcript(tmp_path, "sid-b")
    _transcript(tmp_path, "sid-c", BAD, BAD, _response_line("m2"))
    runs = [_run("sid-a"), _run("sid-b"), _run("sid-c")]

    led = run_usage.ledger(runs, tmp_path, run_usage.Files())

    assert led.coverage.unreadable_lines == 3
    assert led.coverage.unreadable_ids == ("sid-b",)


def test_late_whole_read_failure_exposes_no_partial_usage():
    class LateFailureFiles(FakeFiles):
        """Answers the first transcript's lines and then fails the second whole read."""

        def read_lines(self, path):
            if path.name == "sid-late.jsonl":
                return None
            return [_response_line("m1")]

    files = LateFailureFiles(
        transcripts={
            "sid-a": [Path("/p/proj/sid-a.jsonl")],
            "sid-late": [Path("/p/proj/sid-late.jsonl")],
        }
    )

    led = run_usage.ledger([_run("sid-a"), _run("sid-late")], Path("/p"), files)

    assert [row.session_id for row in led.rows] == ["sid-a"]
    assert led.coverage.unreadable_ids == ("sid-late",)
    assert sum(spend.input for row in led.rows for spend in row.spend.values()) == 1


# ── Why a run could not be read, and which places could not be looked in ───────────────────


class OutcomeFiles:
    """A scripted port that also answers the internal outcome seams — which transcripts a search
    found, which searches could not finish, which places failed, and how each read ended. It has
    no filesystem behind it."""

    def __init__(self, *, found=None, incomplete=(), failed_places=(), reads=None):
        self._found = found or {}
        self._incomplete = frozenset(incomplete)
        self._failed_places = tuple(failed_places)
        self._reads = reads or {}

    def find_transcripts(self, session_id, projects_root):
        return list(self._found.get(session_id, []))

    def _search_transcripts(self, session_ids, projects_root):
        return run_usage.TranscriptSearch(
            matches={sid: tuple(self._found.get(sid, ())) for sid in session_ids},
            incomplete=frozenset(sid for sid in session_ids if sid in self._incomplete),
            failed_places=self._failed_places,
        )

    def read_lines(self, path):
        outcome = self._reads[path]
        run_usage.observe_read(outcome)
        return None if outcome.lines is None else list(outcome.lines)


def _completed(*lines):
    return run_usage.TranscriptRead(lines=tuple(lines), missing=False)


GONE_AT_READ = run_usage.TranscriptRead(lines=None, missing=True)
UNOPENABLE = run_usage.TranscriptRead(lines=None, missing=False)
REASON = run_usage.UnreadableRunReason
ROOT = Path("/projects")
PROJECT_D = ROOT / "dir-d"


def test_reasons_in_run_order_for_mixed_absent_open_failure_and_incomplete_lookup():
    found = {
        "sid-open": [PROJECT_D / "sid-open.jsonl"],
        "sid-late": [PROJECT_D / "sid-late.jsonl"],
    }
    files = OutcomeFiles(
        found=found,
        incomplete={"sid-blind"},
        failed_places=[PROJECT_D],
        reads={
            PROJECT_D / "sid-open.jsonl": UNOPENABLE,
            PROJECT_D / "sid-late.jsonl": GONE_AT_READ,
        },
    )
    runs = [_run(sid) for sid in ("sid-blind", "sid-open", "sid-none", "sid-late")]

    led = run_usage.ledger(runs, ROOT, files)

    assert led.coverage.unreadable_ids == ("sid-blind", "sid-open", "sid-none", "sid-late")
    assert led.unreadable_reasons == (
        REASON.PLACE_COULD_NOT_BE_LOOKED_IN,
        REASON.COULD_NOT_BE_OPENED,
        REASON.GONE,
        REASON.GONE,
    )


def test_found_readable_transcript_is_a_row_despite_a_failed_sibling_place():
    path = PROJECT_D / "sid-1.jsonl"
    files = OutcomeFiles(
        found={"sid-1": [path]},
        incomplete={"sid-1"},
        failed_places=[ROOT / "dir-locked"],
        reads={path: _completed(_usage_line())},
    )

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert [row.session_id for row in led.rows] == ["sid-1"]
    assert led.unreadable_reasons == ()
    assert [d.directory for d in led.unreadable_directories] == [ROOT / "dir-locked"]


def test_found_then_absent_is_gone_despite_a_failed_sibling_place():
    path = PROJECT_D / "sid-1.jsonl"
    files = OutcomeFiles(
        found={"sid-1": [path]},
        incomplete={"sid-1"},
        failed_places=[ROOT / "dir-locked"],
        reads={path: GONE_AT_READ},
    )

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert led.unreadable_reasons == (REASON.GONE,)


def test_found_then_unopenable_is_could_not_be_opened():
    path = PROJECT_D / "sid-1.jsonl"
    files = OutcomeFiles(found={"sid-1": [path]}, reads={path: UNOPENABLE})

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert led.unreadable_reasons == (REASON.COULD_NOT_BE_OPENED,)


def test_no_transcript_found_and_a_place_failed_gets_the_third_reason():
    files = OutcomeFiles(incomplete={"sid-1"}, failed_places=[PROJECT_D])

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert led.unreadable_reasons == (REASON.PLACE_COULD_NOT_BE_LOOKED_IN,)
    third = run_usage.reason_words(REASON.PLACE_COULD_NOT_BE_LOOKED_IN)
    assert "could not be looked in" in third
    assert third not in (
        run_usage.reason_words(REASON.GONE),
        run_usage.reason_words(REASON.COULD_NOT_BE_OPENED),
    )


def test_reason_words_for_the_other_two_are_the_agreed_words():
    assert run_usage.reason_words(REASON.GONE) == "gone"
    assert run_usage.reason_words(REASON.COULD_NOT_BE_OPENED) == "could not be opened"


def test_no_transcript_found_and_every_place_looked_in_or_absent_is_gone():
    led = run_usage.ledger([_run("sid-1")], ROOT, OutcomeFiles())

    assert led.unreadable_reasons == (REASON.GONE,)
    assert led.unreadable_directories == ()


def test_completed_empty_transcript_stays_a_readable_row():
    path = PROJECT_D / "sid-1.jsonl"
    files = OutcomeFiles(found={"sid-1": [path]}, reads={path: _completed()})

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert [row.session_id for row in led.rows] == ["sid-1"]
    assert led.unreadable_reasons == ()


def test_failed_whole_read_contributes_no_records_figures_or_pointer():
    path = PROJECT_D / "sid-1.jsonl"
    files = OutcomeFiles(found={"sid-1": [path]}, reads={path: UNOPENABLE})

    led = run_usage.ledger([_run("sid-1")], ROOT, files)

    assert led.rows == ()
    assert led.coverage.unreadable_lines == 0
    assert run_usage.render(led, matched_sessions=()).count("sid-1") == 1  # the coverage id only


def test_ledger_reads_each_transcript_once_through_the_read_lines_path_seam(tmp_path, monkeypatch):
    project = _project(tmp_path)
    transcript = project / "sid-1.jsonl"
    transcript.write_text(_usage_line())
    _directory_in_place_of_transcript(tmp_path, "sid-dir")

    class SpyFiles(run_usage.Files):
        def __init__(self):
            self.read_paths = []

        def read_lines(self, path):
            self.read_paths.append(path)
            return super().read_lines(path)

    read_texts = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        read_texts.append(self)
        return real_read_text(self, *args, **kwargs)

    probed = []
    real_exists, real_is_file = Path.exists, Path.is_file

    def noting_exists(self, *args, **kwargs):
        probed.append(self)
        return real_exists(self, *args, **kwargs)

    def noting_is_file(self, *args, **kwargs):
        probed.append(self)
        return real_is_file(self, *args, **kwargs)

    files = SpyFiles()
    monkeypatch.setattr(Path, "read_text", counting_read_text)
    monkeypatch.setattr(Path, "exists", noting_exists)
    monkeypatch.setattr(Path, "is_file", noting_is_file)
    led = run_usage.ledger([_run("sid-1"), _run("sid-dir")], tmp_path, files)

    assert files.read_paths == [transcript, project / "sid-dir.jsonl"]
    assert read_texts == files.read_paths
    assert [p for p in probed if p.suffix == ".jsonl"] == []
    assert led.unreadable_reasons == (REASON.COULD_NOT_BE_OPENED,)


def test_named_transcript_found_in_a_searchable_project_dir_does_not_require_enumerating_it(
    tmp_path,
):
    if os.geteuid() == 0:
        pytest.skip("mode bits do not stop a read at effective uid 0")
    project = _project(tmp_path, "searchable")
    (project / "sid-1.jsonl").write_text(_usage_line())
    project.chmod(0o100)  # may be entered, may not be listed
    try:
        led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())
    finally:
        project.chmod(0o700)

    assert [row.session_id for row in led.rows] == ["sid-1"]
    assert led.unreadable_directories == ()


def test_root_inspection_failure_reports_only_the_root_and_not_unseen_children(tmp_path, seal):
    _project(tmp_path, "dir-a")
    _project(tmp_path, "dir-b")
    root = tmp_path / "root"
    root.mkdir()
    (root / "dir-a").mkdir()
    seal(root)

    led = run_usage.ledger([_run("sid-1"), _run("sid-2")], root, run_usage.Files())

    assert led.unreadable_reasons == (REASON.PLACE_COULD_NOT_BE_LOOKED_IN,) * 2
    assert [d.directory for d in led.unreadable_directories] == [root]


def test_root_failure_with_zero_listed_or_named_runs_still_reports_the_root(tmp_path, seal):
    root = tmp_path / "root"
    root.mkdir()
    seal(root)

    led = run_usage.ledger([], root, run_usage.Files())

    assert led.coverage.total == 0
    assert [d.directory for d in led.unreadable_directories] == [root]


def test_a_locked_project_dir_is_reported_even_with_zero_runs(tmp_path, seal):
    root = tmp_path / "root"
    (root / "dir-ok").mkdir(parents=True)
    locked = root / "dir-b"
    locked.mkdir()
    seal(locked)

    led = run_usage.ledger([], root, run_usage.Files())

    assert [d.directory for d in led.unreadable_directories] == [root / "dir-b"]


def test_genuine_errors_other_than_mode_000_are_could_not_be_looked_in(tmp_path, list_only):
    # CP-442: a root whose names can be listed but which cannot be entered does not say which of
    # its entries are project directories, nor what they hold — it could not be looked in.
    root = tmp_path / "root"
    root.mkdir()
    (_project(root, "proj") / "sid-1.jsonl").write_text(_usage_line())
    list_only(root)

    led = run_usage.ledger([_run("sid-1")], root, run_usage.Files())
    empty = run_usage.ledger([], root, run_usage.Files())

    assert led.unreadable_reasons == (REASON.PLACE_COULD_NOT_BE_LOOKED_IN,)
    assert [d.directory for d in led.unreadable_directories] == [root]
    assert [d.directory for d in empty.unreadable_directories] == [root]
    assert "No runs read." not in run_usage.render(led)


def test_a_project_dir_that_can_be_listed_but_not_entered_could_not_be_looked_in(
    tmp_path, list_only
):
    root = tmp_path / "root"
    root.mkdir()
    _project(root, "dir-ok")
    (_project(root, "dir-b") / "sid-1.jsonl").write_text(_usage_line())
    list_only(root / "dir-b")

    asked = run_usage.ledger([_run("sid-1")], root, run_usage.Files())
    none_asked = run_usage.ledger([], root, run_usage.Files())

    assert asked.unreadable_reasons == (REASON.PLACE_COULD_NOT_BE_LOOKED_IN,)
    for led in (asked, none_asked):
        assert [d.directory for d in led.unreadable_directories] == [root / "dir-b"]


def test_a_looping_link_at_the_transcript_path_is_found_and_could_not_be_opened(tmp_path):
    # CP-442/CP-443: the project directory was looked in — it holds an entry by the transcript's
    # name, as `transcript_for` finds it. That entry cannot be read, so the run could not be
    # opened, and the directory, whose other transcript is read, is not named as unlooked.
    project = _project(tmp_path)
    (project / "sid-ok.jsonl").write_text(_usage_line())
    (project / "sid-1.jsonl").symlink_to("sid-1.jsonl")  # a loop: there, and never readable

    led = run_usage.ledger([_run("sid-ok"), _run("sid-1")], tmp_path, run_usage.Files())

    assert [row.session_id for row in led.rows] == ["sid-ok"]
    assert led.coverage.unreadable_ids == ("sid-1",)
    assert led.unreadable_reasons == (REASON.COULD_NOT_BE_OPENED,)
    assert led.unreadable_directories == ()


def test_links_under_the_root_that_resolve_to_nothing_are_not_places(tmp_path):
    _project(tmp_path, "proj")
    (tmp_path / "dangling").symlink_to("nowhere")
    (tmp_path / "looping").symlink_to("looping")

    for runs in ([_run("sid-1")], []):
        led = run_usage.ledger(runs, tmp_path, run_usage.Files())
        assert led.unreadable_directories == ()
    assert run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files()).unreadable_reasons == (
        REASON.GONE,
    )


def test_absent_root_or_root_under_a_regular_file_is_absent_not_failed(tmp_path):
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    for root in (tmp_path / "absent", a_file / "below"):
        led = run_usage.ledger([_run("sid-1")], root, run_usage.Files())
        assert led.unreadable_reasons == (REASON.GONE,)
        assert led.unreadable_directories == ()


def test_regular_file_directly_under_the_root_is_not_a_place(tmp_path):
    (tmp_path / "stray-file").write_text("x")
    for runs in ([_run("sid-1")], []):
        led = run_usage.ledger(runs, tmp_path, run_usage.Files())
        assert led.unreadable_directories == ()


def test_duplicate_transcript_matches_keep_the_lexical_first_choice(tmp_path):
    (_project(tmp_path, "proj-b") / "sid-1.jsonl").write_text(_usage_line(input_tokens=9))
    (_project(tmp_path, "proj-a") / "sid-1.jsonl").write_text(_usage_line(input_tokens=1))

    led = run_usage.ledger([_run("sid-1")], tmp_path, run_usage.Files())

    assert led.rows[0].spend["claude-sonnet-5"].input == 1


def test_failed_places_are_deduplicated_and_sorted_independently_of_discovery_order():
    files = OutcomeFiles(
        failed_places=[ROOT / "dir-b", ROOT / "dir-a", ROOT, ROOT / "dir-b", ROOT / "dir-a"]
    )

    led = run_usage.ledger([], ROOT, files)

    assert [d.directory for d in led.unreadable_directories] == [
        ROOT,
        ROOT / "dir-a",
        ROOT / "dir-b",
    ]


def test_failed_project_dirs_found_in_reverse_creation_order_are_sorted_by_name(tmp_path, seal):
    root = tmp_path / "root"
    root.mkdir()
    for name in ("dir-z", "dir-m", "dir-a"):
        seal(_project(root, name))

    led = run_usage.ledger([_run("sid-1")], root, run_usage.Files())

    assert [d.directory.name for d in led.unreadable_directories] == ["dir-a", "dir-m", "dir-z"]


def test_directory_failure_carries_the_fixed_reason_and_a_two_member_json_object():
    failure = run_usage.DirectoryFailure(directory=Path("tests/parity/projects"))

    assert failure.reason == "could not be looked in"
    assert run_usage.directory_failure_json(failure) == {
        "directory": "tests/parity/projects",
        "reason": "could not be looked in",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.directory = Path("elsewhere")


def test_hand_built_coverage_and_two_argument_ledger_are_unchanged_and_invent_no_reason():
    led = _small_ledger()

    assert led.unreadable_reasons == ()
    assert led.unreadable_directories == ()


def test_coverage_constructor_still_builds_from_the_baseline_arguments():
    coverage = run_usage.Coverage(read=1, unreadable=0, total=1, unreadable_ids=())

    assert (coverage.read, coverage.unreadable, coverage.total) == (1, 0, 1)
    assert coverage.unreadable_ids == ()
    assert [f.name for f in dataclasses.fields(run_usage.Coverage)] == [
        "read", "unreadable", "total", "unreadable_ids",
        "dropped_no_session", "listed", "named", "unreadable_lines",
    ]


def test_read_runs_carries_the_diagnostics_even_with_no_readable_rows(tmp_path, seal):
    root = tmp_path / "root"
    root.mkdir()
    seal(root)
    reader = run_usage.build_reader(root)

    led = run_usage.read_runs(_agents_json(_row(sessionId="sid-1")), ["sid-2"], reader)

    assert led.rows == ()
    assert led.unreadable_reasons == (REASON.PLACE_COULD_NOT_BE_LOOKED_IN,) * 2
    assert [d.directory for d in led.unreadable_directories] == [root]


# ── Task 2 — the reasons and the directories a `runs` report prints ──────

DIRECTORY_LINE = (
    "{directory} could not be looked in, so nothing in it is in this report. "
    "Run this command again once it can be looked in."
)
THIRD_REASON = "a directory its transcript may be in could not be looked in"


def _diagnosed(*, rows=(), ids=(), reasons=(), directories=(), read=None, named=0):
    coverage = run_usage.Coverage(
        read=len(rows) if read is None else read,
        unreadable=len(ids),
        total=len(rows) + len(ids),
        unreadable_ids=tuple(ids),
        listed=len(rows) + len(ids) - named,
        named=named,
    )
    return run_usage.Ledger(
        rows=tuple(rows),
        coverage=coverage,
        unreadable_reasons=tuple(reasons),
        unreadable_directories=tuple(
            run_usage.DirectoryFailure(directory=Path(d)) for d in directories
        ),
    )


def test_coverage_line_follows_each_unreadable_id_with_its_reason_in_brackets():
    led = _diagnosed(
        rows=[_ledger_row()],
        ids=("sid-x", "sid-y", "sid-z"),
        reasons=(REASON.GONE, REASON.COULD_NOT_BE_OPENED, REASON.PLACE_COULD_NOT_BE_LOOKED_IN),
    )

    assert run_usage.render(led).splitlines()[2] == (
        "Coverage: 1 read / 3 unreadable / 4 total (4 listed, 0 named) — unreadable: "
        f"sid-x (gone), sid-y (could not be opened), sid-z ({THIRD_REASON})"
    )


def test_unreadable_lines_clause_still_follows_the_reasons():
    led = _diagnosed(ids=("sid-x",), reasons=(REASON.GONE,))
    led = dataclasses.replace(
        led, coverage=dataclasses.replace(led.coverage, unreadable_lines=2)
    )

    assert run_usage.render(led).splitlines()[2].endswith(
        "— unreadable: sid-x (gone) — 2 unreadable lines skipped in the transcripts read"
    )


def test_json_unreadable_reasons_aligned_and_worded_like_the_text():
    led = _diagnosed(
        ids=("sid-x", "sid-y"),
        reasons=(REASON.COULD_NOT_BE_OPENED, REASON.PLACE_COULD_NOT_BE_LOOKED_IN),
    )

    coverage = json.loads(run_usage.render_json(led))["coverage"]

    assert coverage["unreadable_ids"] == ["sid-x", "sid-y"]
    assert coverage["unreadable_reasons"] == ["could not be opened", THIRD_REASON]


def test_json_unreadable_reasons_absent_when_no_id_is_unreadable():
    coverage = json.loads(run_usage.render_json(_diagnosed(rows=[_ledger_row()])))["coverage"]

    assert "unreadable_reasons" not in coverage


def test_directory_lines_sit_in_one_block_outside_rows_and_before_the_note_block():
    led = _diagnosed(
        rows=[_ledger_row("sid-1"), _ledger_row("sid-2")],
        ids=("sid-x",),
        reasons=(REASON.PLACE_COULD_NOT_BE_LOOKED_IN,),
        directories=("root", "root/a", "root/b"),
    )

    lines = run_usage.render(led).splitlines()

    first = lines.index(DIRECTORY_LINE.format(directory="root"))
    assert lines[first : first + 3] == [
        DIRECTORY_LINE.format(directory=d) for d in ("root", "root/a", "root/b")
    ]
    # after every row and its separating blank line, directly above the note block
    assert lines[first - 1] == ""
    assert lines[first - 2].startswith("  ") and "started=" not in lines[first - 2]
    assert lines[first + 3] == run_usage.transcript_notes.NO_COST_LINE
    assert not any("could not be looked in, so nothing" in line for line in lines[:first])
    assert "could not be looked in, so nothing" not in lines[1]


def test_directory_block_replaces_no_runs_read_only_when_non_empty():
    empty = run_usage.render(_diagnosed())
    locked = run_usage.render(_diagnosed(directories=("root",)))

    assert "No runs read." in empty
    assert "No runs read." not in locked
    assert locked.splitlines()[-2] == DIRECTORY_LINE.format(directory="root")
    assert locked.splitlines()[-3] == ""


def test_json_unreadable_directories_has_exactly_directory_and_reason_and_is_absent_when_empty():
    absent = json.loads(run_usage.render_json(_diagnosed(rows=[_ledger_row()])))["coverage"]
    present = json.loads(
        run_usage.render_json(_diagnosed(directories=("root", "root/a")))
    )["coverage"]

    assert "unreadable_directories" not in absent
    assert present["unreadable_directories"] == [
        {"directory": "root", "reason": "could not be looked in"},
        {"directory": "root/a", "reason": "could not be looked in"},
    ]


def test_clean_text_and_json_are_byte_identical_to_baseline():
    led = _small_ledger()
    clean = dataclasses.replace(
        led,
        coverage=dataclasses.replace(led.coverage, unreadable=0, unreadable_ids=(), total=1),
    )

    assert run_usage.render(clean) == (
        f"{run_usage.POPULATION_NOTE}\n"
        f"{transcript_notes.SPAN_LINE}\n"
        "Coverage: 1 read / 0 unreadable / 1 total (1 listed, 1 named)\n"
        "Dropped (no sessionId): 0\n\n"
        "sid-1  a run  cwd=/cwd  state=done\n"
        "  started=1  span=60s\n"
        "  claude-sonnet-5: input=1 output=194 thinking=112 cache_read=0 cache_write_1h=0 "
        "cache_write_5m=0\n"
        f"{run_usage.transcript_notes.NO_COST_LINE}\n"
    )
    members = json.loads(run_usage.render_json(clean))["coverage"]
    assert "unreadable_reasons" not in members
    assert "unreadable_directories" not in members


def test_manual_coverage_renders_without_a_reason_and_without_a_member():
    led = _small_ledger()

    assert run_usage.render(led).splitlines()[2].endswith("— unreadable: sid-2")
    assert "unreadable_reasons" not in json.loads(run_usage.render_json(led))["coverage"]


# ── The statement of what the run report's time covers ───────────────────


def _section_starts(text: str) -> list[int]:
    return [n for n, line in enumerate(text.splitlines()) if line.startswith("sid-")]


def test_runs_text_prints_the_statement_once_as_the_second_line(tmp_path, capsys):
    for sid in ("sid-a", "sid-b"):
        _transcript(tmp_path, sid, _response_line("m1"), _response_line("m2"))
    text = _main(_runs_argv(tmp_path, [], ["sid-a", "sid-b"], False), capsys)[1]

    lines = text.splitlines()
    assert lines[1] == transcript_notes.SPAN_LINE and lines.count(transcript_notes.SPAN_LINE) == 1
    assert len(_section_starts(text)) == 2
    assert lines[0] == POPULATION and lines[2].startswith("Coverage: ")


def test_runs_json_carries_top_level_span_equal_to_the_text_line(tmp_path, capsys):
    _transcript(tmp_path, "sid-a", _response_line("m1"))
    payload = json.loads(_main(_runs_argv(tmp_path, [], ["sid-a"]), capsys)[1])

    assert list(payload)[:3] == ["population", "span", "coverage"]
    assert payload["span"] == transcript_notes.SPAN_LINE
    assert payload["population"] == POPULATION and payload["rows"][0]["session_id"] == "sid-a"


def test_runs_statement_is_present_in_empty_and_diagnostic_reports(tmp_path, capsys):
    empty = _main(_runs_argv(tmp_path, [], [], False), capsys)[1]
    gone = _main(_runs_argv(tmp_path, [], ["sid-gone"], False), capsys)[1]
    for text in (empty, gone):
        lines = text.splitlines()
        assert lines[1] == transcript_notes.SPAN_LINE and lines.count(lines[1]) == 1
        assert "No runs read." in lines and lines[-1] == NO_COST
    for argv in (_runs_argv(tmp_path, [], []), _runs_argv(tmp_path, [], ["sid-gone"])):
        assert json.loads(_main(argv, capsys)[1])["span"] == transcript_notes.SPAN_LINE
    assert "sid-gone (gone)" in gone


def test_runs_non_zero_exits_print_no_report_and_no_statement(tmp_path, capsys):
    with pytest.raises(SystemExit) as argument_error:
        run_usage.main(["--session"])
    captured = capsys.readouterr()
    assert argument_error.value.code != 0
    assert captured.out == "" and transcript_notes.SPAN_LINE not in captured.err

    missing = ["--agents-json", str(tmp_path / "no-such-listing.json")]
    with pytest.raises(SystemExit) as unreadable_listing:
        run_usage.main([*missing, "--projects-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert str(unreadable_listing.value).startswith("error: ")
    assert captured.out == "" and transcript_notes.SPAN_LINE not in str(unreadable_listing.value)


# ── A response found in more than one transcript is counted once (CP-573 to CP-587) ──


def _stamp(minute, second=0):
    return f"2026-10-01T10:{minute:02d}:{second:02d}.000Z"


def _share_a_response(tmp_path, first="s-a", second="s-b"):
    """Example 1: `msg_shared` sits in both transcripts, each also holding a response of its own."""
    _transcript(tmp_path, first, _response_line("msg_a", 100, 10, timestamp=_stamp(0)),
                _response_line("msg_shared", 7, 3, timestamp=_stamp(0, 30)))
    _transcript(tmp_path, second, _response_line("msg_shared", 7, 3, timestamp=_stamp(0, 30)),
                _response_line("msg_b", 200, 20, timestamp=_stamp(5)))


def _runs_of(tmp_path, capsys, listed, named=(), json_form=True):
    rows = [_row(sessionId=sid, name=sid) for sid in listed]
    out = _main(_runs_argv(tmp_path, rows, named, json_form), capsys)
    assert out[0] == 0, out[2]
    return json.loads(out[1]) if json_form else out[1]


def _spend(row, model="claude-sonnet-5"):
    return row["spend"][model]["input"], row["spend"][model]["output"]


def _coverage_line(text):
    return next(line for line in text.splitlines() if line.startswith("Coverage:"))


def test_runs_count_a_shared_response_under_the_first_listed_row(tmp_path, capsys):
    _share_a_response(tmp_path)

    payload = _runs_of(tmp_path, capsys, ["s-a", "s-b"])
    text = _runs_of(tmp_path, capsys, ["s-a", "s-b"], json_form=False)

    first, second = payload["rows"]
    assert (_spend(first), first["span_seconds"]) == ((107, 13), 30)
    assert (_spend(second), second["span_seconds"]) == ((200, 20), 0)
    assert "span=30s\n  claude-sonnet-5: input=107 output=13 " in text
    assert "span=0s\n  claude-sonnet-5: input=200 output=20 " in text
    assert _spend(first)[0] + _spend(second)[0] == 307


def test_runs_keeping_row_follows_the_listing_order(tmp_path, capsys):
    _share_a_response(tmp_path)

    reversed_rows = _runs_of(tmp_path, capsys, ["s-b", "s-a"])["rows"]
    assert [row["session_id"] for row in reversed_rows] == ["s-b", "s-a"]
    assert (_spend(reversed_rows[0]), reversed_rows[0]["span_seconds"]) == ((207, 23), 270)
    assert (_spend(reversed_rows[1]), reversed_rows[1]["span_seconds"]) == ((100, 10), 0)

    # listed rows come first, then --session in the order named
    listed_first = _runs_of(tmp_path, capsys, ["s-b"], named=["s-a"])["rows"]
    assert _spend(listed_first[0]) == (207, 23)
    named_in_order = _runs_of(tmp_path, capsys, [], named=["s-a", "s-b"])["rows"]
    assert _spend(named_in_order[0]) == (107, 13)
    named_reversed = _runs_of(tmp_path, capsys, [], named=["s-b", "s-a"])["rows"]
    assert _spend(named_reversed[0]) == (207, 23)


def test_runs_report_run_twice_gives_byte_identical_output_and_the_same_keeping_row(
    tmp_path, capsys
):
    _share_a_response(tmp_path)

    outputs = [
        [_main(_runs_argv(tmp_path, [_row(sessionId=s, name=s) for s in ("s-a", "s-b")],
                          json_form=form), capsys)[1] for form in (True, False)]
        for _ in range(2)
    ]

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0][0])["rows"][0]["spend"]["claude-sonnet-5"]["input"] == 107


def test_runs_coverage_line_ends_with_the_shared_clause_after_every_other_clause(
    tmp_path, capsys
):
    _share_a_response(tmp_path)
    clause = run_usage.shared_responses_clause(1)

    plain = _runs_of(tmp_path, capsys, ["s-a", "s-b"], json_form=False)
    assert _coverage_line(plain) == (
        f"Coverage: 2 read / 0 unreadable / 2 total (2 listed, 0 named) — {clause}"
    )
    assert clause == (
        "1 response found in more than one of the transcripts this report read, counted once"
    )

    (tmp_path / "proj" / "s-a.jsonl").write_text(
        (tmp_path / "proj" / "s-a.jsonl").read_text() + "not json {\n"
    )
    crowded = _coverage_line(
        _runs_of(tmp_path, capsys, ["s-a", "s-b", "s-gone"], json_form=False)
    )
    assert crowded.endswith(
        f" — unreadable: s-gone (gone) — {run_usage.UNREADABLE_LINES_ONE} — {clause}"
    )
    coverage = _runs_of(tmp_path, capsys, ["s-a", "s-b"])["coverage"]
    assert coverage["responses_in_more_than_one_transcript"] == 1
    assert type(coverage["responses_in_more_than_one_transcript"]) is int


def test_runs_clause_and_member_are_absent_at_zero(tmp_path, capsys):
    _transcript(tmp_path, "s-a", _response_line("msg_a", 100, 10))
    _transcript(tmp_path, "s-b", _response_line("msg_b", 200, 20))

    text = _runs_of(tmp_path, capsys, ["s-a", "s-b"], json_form=False)
    payload = _runs_of(tmp_path, capsys, ["s-a", "s-b"])

    assert _coverage_line(text) == "Coverage: 2 read / 0 unreadable / 2 total (2 listed, 0 named)"
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]
    assert "found in more than one" not in text


def test_runs_clause_singular_and_plural_use_their_own_whole_template(tmp_path, capsys):
    for sid, model, tokens in (("s-a", "m-one", 1), ("s-b", "m-two", 50), ("s-c", "m-one", 9)):
        _transcript(tmp_path, sid, _response_line("msg_x", tokens, 1, model=model))
    listing = ["s-a", "s-b", "s-c"]

    singular = _runs_of(tmp_path, capsys, listing, json_form=False)
    assert _coverage_line(singular).endswith(f" — {run_usage.shared_responses_clause(1)}")
    assert run_usage.shared_responses_clause(0) is None
    assert run_usage.shared_responses_clause(2) == (
        "2 responses found in more than one of the transcripts this report read, each counted once"
    )
    assert _runs_of(tmp_path, capsys, listing)["coverage"][
        "responses_in_more_than_one_transcript"] == 1

    for sid in ("s-a", "s-b"):
        path = tmp_path / "proj" / f"{sid}.jsonl"
        path.write_text(path.read_text() + _response_line("msg_y", 4, 4) + "\n")
    plural = _runs_of(tmp_path, capsys, listing, json_form=False)
    assert _coverage_line(plural).endswith(f" — {run_usage.shared_responses_clause(2)}")
    assert _runs_of(tmp_path, capsys, listing)["coverage"][
        "responses_in_more_than_one_transcript"] == 2


def test_runs_repeated_blocks_inside_one_file_are_not_shared(tmp_path, capsys):
    _transcript(tmp_path, "s-a", _response_line("msg_x", 5, 5), _response_line("msg_x", 5, 5),
                _response_line("msg_x", 5, 5))

    payload = _runs_of(tmp_path, capsys, ["s-a"])

    assert _spend(payload["rows"][0]) == (5, 5)
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]


def test_runs_a_row_left_with_nothing_counted_renders_as_an_empty_row(tmp_path, capsys):
    _transcript(tmp_path, "s-a", _response_line("msg_shared", 7, 3))
    _transcript(tmp_path, "s-b", _response_line("msg_shared", 7, 3))

    text = _runs_of(tmp_path, capsys, ["s-a", "s-b"], json_form=False)
    payload = _runs_of(tmp_path, capsys, ["s-a", "s-b"])

    assert "s-b  s-b  cwd=" in text
    assert text.split("s-b  s-b")[1].split("\n", 3)[1:3] == [
        "  started=1788504031568  span=n/a", "  no usage records"
    ]
    assert [row["session_id"] for row in payload["rows"]] == ["s-a", "s-b"]
    assert payload["rows"][1]["spend"] == {}
    assert payload["rows"][1]["span_seconds"] is None


def test_runs_mixed_unique_and_shared_responses_keep_every_token_class_per_model(
    tmp_path, capsys
):
    rich = json.loads(_response_line("msg_shared", 4, 6, model="other", thinking=2))
    rich["message"]["usage"].update(
        cache_read_input_tokens=11,
        cache_creation={"ephemeral_1h_input_tokens": 12, "ephemeral_5m_input_tokens": 13},
    )
    shared = json.dumps(rich)
    _transcript(tmp_path, "s-a", _response_line("msg_a", 1, 1), shared)
    _transcript(tmp_path, "s-b", shared, _response_line("msg_b", 2, 2, thinking=1))

    rows = _runs_of(tmp_path, capsys, ["s-a", "s-b"])["rows"]

    assert rows[0]["spend"]["other"] == {
        "input": 4, "output": 6, "thinking": 2, "cache_read": 11,
        "cache_write_1h": 12, "cache_write_5m": 13,
    }
    assert set(rows[1]["spend"]) == {"claude-sonnet-5"}
    assert rows[1]["spend"]["claude-sonnet-5"]["thinking"] == 1
    text = _runs_of(tmp_path, capsys, ["s-a", "s-b"], json_form=False)
    assert text.split("s-b  s-b")[1].count("other:") == 0


def test_runs_copies_with_different_usage_count_the_first_record_met_in_the_keeping_row(
    tmp_path, capsys
):
    _transcript(tmp_path, "s-a", _response_line("msg_x", 10, 1), _response_line("msg_x", 99, 9))
    _transcript(tmp_path, "s-b", _response_line("msg_x", 500, 50, model="other"))

    rows = _runs_of(tmp_path, capsys, ["s-a", "s-b"])["rows"]

    assert rows[0]["spend"] == {"claude-sonnet-5": rows[0]["spend"]["claude-sonnet-5"]}
    assert _spend(rows[0]) == (10, 1)
    assert rows[1]["spend"] == {}


def test_runs_a_report_reading_one_transcript_counts_the_response_there(tmp_path, capsys):
    _share_a_response(tmp_path)

    payload = _runs_of(tmp_path, capsys, ["s-b"])

    assert (_spend(payload["rows"][0]), payload["rows"][0]["span_seconds"]) == ((207, 23), 270)
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]


def test_runs_unreadable_or_unselected_counterpart_does_not_make_an_id_shared(tmp_path, capsys):
    # a counterpart that exists but is not selected, and one that is listed but absent
    _share_a_response(tmp_path)
    assert "responses_in_more_than_one_transcript" not in _runs_of(
        tmp_path, capsys, ["s-b", "s-gone"])["coverage"]

    # a malformed record carrying the id, and a usage-free record carrying it
    malformed = json.loads(_response_line("msg_shared", 7, 3))
    malformed["message"]["usage"]["input_tokens"] = "seven"
    _transcript(tmp_path, "m-a", json.dumps(malformed), _response_line("msg_ma", 1, 1))
    free = json.loads(_response_line("msg_shared", 7, 3))
    free["message"]["usage"] = {}
    _transcript(tmp_path, "f-a", json.dumps(free), _response_line("msg_fa", 1, 1))
    for counterpart, lines in (("m-a", 1), ("f-a", 0)):
        payload = _runs_of(tmp_path, capsys, [counterpart, "s-b"])
        assert "responses_in_more_than_one_transcript" not in payload["coverage"]
        assert _spend(payload["rows"][1]) == (207, 23)
        assert payload["coverage"].get("unreadable_lines", 0) == lines


def test_runs_records_with_no_id_are_each_counted(tmp_path, capsys):
    for sid in ("s-a", "s-b"):
        _transcript(tmp_path, sid, _response_line(None, 9, 1))
    payload = _runs_of(tmp_path, capsys, ["s-a", "s-b"])
    assert [_spend(row) for row in payload["rows"]] == [(9, 1), (9, 1)]
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]

    # a null id follows the rule of an absent one; an empty-string id is an id like any other
    null_id = json.loads(_response_line("x", 9, 1))
    null_id["message"]["id"] = None
    for sid in ("s-a", "s-b"):
        _transcript(tmp_path, sid, json.dumps(null_id), _response_line("", 3, 1))
    payload = _runs_of(tmp_path, capsys, ["s-a", "s-b"])
    assert [_spend(row) for row in payload["rows"]] == [(12, 2), (9, 1)]
    assert payload["coverage"]["responses_in_more_than_one_transcript"] == 1


def test_runs_with_zero_responses_only_noid_responses_or_a_single_timestamp(tmp_path, capsys):
    _transcript(tmp_path, "s-a", json.dumps({"type": "user", "message": {}}))
    _transcript(tmp_path, "s-b", _response_line(None, 2, 2, timestamp="not a time"),
                _response_line(None, 3, 3, timestamp=None))
    _transcript(tmp_path, "s-c", _response_line("msg_c", 4, 4, timestamp=_stamp(3)))

    rows = _runs_of(tmp_path, capsys, ["s-a", "s-b", "s-c"])["rows"]

    assert (rows[0]["spend"], rows[0]["span_seconds"]) == ({}, None)
    assert (_spend(rows[1]), rows[1]["span_seconds"]) == ((5, 5), None)
    assert (_spend(rows[2]), rows[2]["span_seconds"]) == ((4, 4), 0)


def test_read_runs_returns_the_rows_and_the_coverage_value_runs_prints(tmp_path, capsys):
    _share_a_response(tmp_path)
    listing = json.dumps([_row(sessionId=s, name=s) for s in ("s-a", "s-b")])
    shown = _runs_of(tmp_path, capsys, ["s-a", "s-b"])

    led = run_usage.read_runs(listing, [], run_usage.build_reader(tmp_path))

    assert [(r.session_id, r.span_seconds) for r in led.rows] == [
        (r["session_id"], r["span_seconds"]) for r in shown["rows"]
    ]
    assert led.rows[0].spend["claude-sonnet-5"].input == 107
    assert led.coverage.responses_in_more_than_one_transcript == 1
    assert run_usage.Coverage(1, 0, 1, ()).responses_in_more_than_one_transcript == 0
    assert run_usage.Coverage(read=1, unreadable=0, total=1, unreadable_ids=(), listed=1) == (
        run_usage.Coverage(1, 0, 1, (), 0, 1)
    )
    assert [f.name for f in dataclasses.fields(run_usage.Coverage)] == [
        "read", "unreadable", "total", "unreadable_ids",
        "dropped_no_session", "listed", "named", "unreadable_lines",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        led.coverage.read = 9
    unshared = run_usage.read_runs(listing, [], run_usage.build_reader(tmp_path))
    assert type(unshared.coverage) is not run_usage.Coverage  # N = 1 is the extended value
    only_b = run_usage.read_runs(json.dumps([_row(sessionId="s-b")]), [],
                                 run_usage.build_reader(tmp_path))
    assert type(only_b.coverage) is run_usage.Coverage  # N = 0 is the base value


def test_unscoped_usage_records_is_unchanged_after_a_scoped_report(tmp_path, capsys):
    _share_a_response(tmp_path)
    _transcript(tmp_path, "s-n", _response_line(None, 1, 1), _response_line(None, 1, 1))
    _runs_of(tmp_path, capsys, ["s-a", "s-b"])  # a scoped report, run first

    class CountingFiles(run_usage.Files):
        reads = 0

        def read_lines(self, path):
            type(self).reads += 1
            return super().read_lines(path)

    files = CountingFiles()
    shared = [r[0]["output_tokens"] for r in run_usage.usage_records(
        tmp_path / "proj" / "s-b.jsonl", files)]
    assert shared == [3, 20]  # msg_shared then msg_b, though s-a keeps msg_shared in the report
    assert CountingFiles.reads == 1
    assert len(list(run_usage.usage_records(tmp_path / "proj" / "s-n.jsonl", files))) == 2
    for name in ("usage_records", "spend_by_model", "transcript_for", "span_seconds", "ModelSpend",
                 "Processes", "Files", "build_reader", "runs_from"):
        assert hasattr(run_usage, name), name
    assert list(inspect.signature(run_usage.usage_records).parameters)[:3] == [
        "path", "files", "unreadable_lines"]


def test_response_scope_counts_each_id_once_and_attributes_it_to_the_keeping_row():
    scope = run_usage.ResponseScope()
    a, b, c = Path("a.jsonl"), Path("b.jsonl"), Path("c.jsonl")

    assert scope.claim(a, "x", 0) is True
    assert scope.claim(b, "x", 1) is False
    assert scope.claim(c, "x", 2) is False
    assert scope.claim(b, "y", 1) is True
    assert scope.claim(a, "y", 0) is False
    assert scope.claim(a, None, 0) is True and scope.claim(b, None, 1) is True
    assert scope.claim(a, "z", 0) is True and scope.claim(a, "z", 0) is True  # same transcript
    assert scope.count_shared() == 2
    assert (scope.count_shared(0), scope.count_shared(1), scope.count_shared(2)) == (1, 1, 0)


def test_runs_a_counterpart_found_and_not_openable_makes_nothing_shared(tmp_path):
    # `s-a` holds the shared response but cannot be opened: it is not a transcript the report
    # read, so `s-b` counts the response as it does when it is the only one read (CP-579)
    _share_a_response(tmp_path)
    blocked = tmp_path / "proj" / "s-a.jsonl"

    class _BlockedFiles(run_usage.Files):
        def read_lines(self, path):
            return None if path == blocked else super().read_lines(path)

    led = run_usage.ledger([_run("s-a"), _run("s-b")], tmp_path, _BlockedFiles())

    assert led.coverage.unreadable_ids == ("s-a",)
    assert led.unreadable_reasons == (REASON.COULD_NOT_BE_OPENED,)
    ((session, spend, span),) = [(r.session_id, r.spend, r.span_seconds) for r in led.rows]
    assert (session, spend["claude-sonnet-5"].input, spend["claude-sonnet-5"].output) == (
        "s-b", 207, 23
    )
    assert span == 270
    assert type(led.coverage) is run_usage.Coverage
    assert led.coverage.responses_in_more_than_one_transcript == 0


def test_runs_a_rejected_copy_of_a_response_another_row_kept_is_still_an_unreadable_line(
    tmp_path, capsys
):
    # `s-b` keeps `msg_shared` first; `m-a`'s malformed record carrying it is a rejected line,
    # counted as one whether or not its id was seen, and it makes nothing shared (CP-579, CP-586)
    _share_a_response(tmp_path)
    malformed = json.loads(_response_line("msg_shared", 7, 3))
    malformed["message"]["usage"]["input_tokens"] = "seven"
    _transcript(tmp_path, "m-a", json.dumps(malformed), _response_line("msg_ma", 1, 1))

    payload = _runs_of(tmp_path, capsys, ["s-b", "m-a"])

    assert payload["coverage"]["unreadable_lines"] == 1
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]
    assert [_spend(row) for row in payload["rows"]] == [(207, 23), (1, 1)]
