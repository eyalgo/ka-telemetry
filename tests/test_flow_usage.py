"""Tests for `flow_usage.py` — what a `/development` flow run spent, one row per step.

Fixtures below are shaped off the live disk, not invented: the tree shape, the journal record
shapes, and the meta-sidecar shape were read from this machine's own
`~/.claude/projects/*/*/subagents/workflows/*/` on 2026-09-07, the same day the plan was
written — see the plan at `system/projects/tools/journey/shukuba/trail/`,
`2026-09-07_the-step-reader-has-a-plan.md`.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import inspect
import itertools
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from e2e.support import api_request_body
from test_transcript_notes import FakeStreamFiles

from ka_telemetry import flow_usage, run_usage, stream_usage, transcript_notes

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


# ── Task 1 — the workflow runs are found ───────────────────────────────


def _make_workflow_run(root: Path, slug: str, sid: str, wf_id: str) -> Path:
    """`<root>/<slug>/<sid>/subagents/workflows/<wf_id>/journal.jsonl` — the real tree shape."""
    run_dir = root / slug / sid / "subagents" / "workflows" / wf_id
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text("")
    return run_dir


def test_a_directory_with_a_journal_is_yielded_one_without_is_not(tmp_path):
    good = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_good")
    # A workflows sibling with no journal.jsonl at all.
    no_journal = tmp_path / "slug-a" / "sid-1" / "subagents" / "workflows" / "wf_bad"
    no_journal.mkdir(parents=True)

    runs = flow_usage.flow_runs(tmp_path, flow_usage.Files())
    assert [r.run_dir for r in runs] == [good]


def test_flat_agent_tool_sibling_is_never_yielded(tmp_path):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_good")
    # The flat sibling: <slug>/<sid>/subagents/agent-*.jsonl, no workflows/ level at all.
    flat_dir = tmp_path / "slug-a" / "sid-1" / "subagents"
    (flat_dir / "agent-flatid.jsonl").write_text("")

    runs = flow_usage.flow_runs(tmp_path, flow_usage.Files())
    assert len(runs) == 1
    assert runs[0].wf_id == "wf_good"


def test_interactive_session_transcript_is_never_yielded(tmp_path):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_good")
    # <slug>/<sid>.jsonl — an interactive session's own transcript, flat under the slug.
    (tmp_path / "slug-a" / "sid-1.jsonl").write_text("")

    runs = flow_usage.flow_runs(tmp_path, flow_usage.Files())
    assert len(runs) == 1


def test_wf_id_session_id_and_slug_are_read_off_the_path(tmp_path):
    _make_workflow_run(tmp_path, "my-slug", "my-sid", "wf_abc123")
    runs = flow_usage.flow_runs(tmp_path, flow_usage.Files())
    assert len(runs) == 1
    run = runs[0]
    assert run.wf_id == "wf_abc123"
    assert run.session_id == "my-sid"
    assert run.slug == "my-slug"


def test_nonexistent_projects_root_yields_nothing_and_does_not_raise(tmp_path):
    missing = tmp_path / "does-not-exist"
    runs = flow_usage.flow_runs(missing, flow_usage.Files())
    assert runs == ()


def test_flow_runs_are_sorted_by_wf_id(tmp_path):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_zzz")
    _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_aaa")
    runs = flow_usage.flow_runs(tmp_path, flow_usage.Files())
    assert [r.wf_id for r in runs] == ["wf_aaa", "wf_zzz"]


class TestFilesFindWorkflowDirs:
    def test_finds_a_real_directory_holding_a_journal(self, tmp_path):
        run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_good")
        files = flow_usage.Files()
        assert files.find_workflow_dirs(tmp_path) == [run_dir]

    def test_a_nonexistent_root_yields_no_matches(self, tmp_path):
        files = flow_usage.Files()
        assert files.find_workflow_dirs(tmp_path / "does-not-exist") == []


# ── Task 2 — the journal names each agent's step and its role ─────────────


def _started(agent_id: str) -> str:
    return json.dumps({"type": "started", "key": f"v2:{agent_id}", "agentId": agent_id})


def _result(agent_id: str, result) -> str:
    return json.dumps(
        {"type": "result", "key": f"v2:{agent_id}", "agentId": agent_id, "result": result}
    )


def _failed(agent_id: str) -> str:
    return json.dumps({"type": "failed", "key": f"v2:{agent_id}", "agentId": agent_id})


def test_status_ok_with_artifact_maps_to_step_and_produce_role():
    journal = "\n".join(
        [
            _started("a1"),
            _result(
                "a1", {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
            ),
        ]
    )
    parsed = flow_usage.steps_from(journal)
    assert parsed.steps == (
        flow_usage.AgentStep(agent_id="a1", step="development-specs", role=flow_usage.Role.PRODUCE),
    )


def test_result_findings_maps_to_step_and_audit_role_as_a_separate_entry():
    journal = "\n".join(
        [
            _started("a1"),
            _result(
                "a1", {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
            ),
            _started("a2"),
            _result(
                "a2", {"step": "development-specs", "result": "findings", "findings": 7}
            ),
        ]
    )
    parsed = flow_usage.steps_from(journal)
    assert set(parsed.steps) == {
        flow_usage.AgentStep(agent_id="a1", step="development-specs", role=flow_usage.Role.PRODUCE),
        flow_usage.AgentStep(agent_id="a2", step="development-specs", role=flow_usage.Role.AUDIT),
    }
    # Two agents on the same step are two separate entries, not merged into one.
    assert len(parsed.steps) == 2


def test_plain_string_result_yields_no_step_and_is_counted_step_less():
    journal = "\n".join([_started("a1"), _result("a1", "some plain string result")])
    parsed = flow_usage.steps_from(journal)
    assert parsed.steps == ()
    assert parsed.step_less == ("a1",)


def test_dict_result_with_no_step_yields_no_step_and_is_counted_step_less():
    journal = "\n".join([_started("a1"), _result("a1", {"status": "ok", "artifact": "x.md"})])
    parsed = flow_usage.steps_from(journal)
    assert parsed.steps == ()
    assert parsed.step_less == ("a1",)


def test_started_with_no_later_result_or_failed_is_unfinished():
    journal = "\n".join(
        [_started("a1"), _started("a2"), _result("a1", {"step": "s", "status": "ok"})]
    )
    parsed = flow_usage.steps_from(journal)
    assert parsed.unfinished == ("a2",)


def test_failed_record_marks_agent_failed_and_is_not_unfinished():
    journal = "\n".join([_started("a1"), _failed("a1")])
    parsed = flow_usage.steps_from(journal)
    assert parsed.failed == ("a1",)
    assert parsed.unfinished == ()


def test_malformed_line_is_skipped_rather_than_fatal():
    journal = "\n".join([_started("a1"), "{not json", _result("a1", {"step": "s", "status": "ok"})])
    parsed = flow_usage.steps_from(journal)
    assert parsed.steps == (
        flow_usage.AgentStep(agent_id="a1", step="s", role=flow_usage.Role.PRODUCE),
    )


def test_a_step_present_but_neither_produce_nor_audit_shape_is_step_less_not_guessed():
    # Defensive: the plan's own measurement found only these two shapes on the live disk. A
    # third is not attributed a role — it is reported as step-less, the same as no step at all.
    journal = "\n".join([_started("a1"), _result("a1", {"step": "s"})])
    parsed = flow_usage.steps_from(journal)
    assert parsed.steps == ()
    assert parsed.step_less == ("a1",)


def test_empty_journal_yields_nothing_and_does_not_raise():
    parsed = flow_usage.steps_from("")
    assert parsed.steps == ()
    assert parsed.step_less == ()
    assert parsed.unfinished == ()
    assert parsed.failed == ()


# ── Task 3 — the meta sidecar names the agent ──────────────────────────


def _write_meta(run_dir: Path, agent_id: str, text: str) -> None:
    (run_dir / f"agent-{agent_id}.meta.json").write_text(text)


def test_meta_with_no_model_returns_type_and_none_for_model(tmp_path):
    _write_meta(tmp_path, "a1", json.dumps({"agentType": "developer", "spawnDepth": 1}))
    meta = flow_usage.agent_meta(tmp_path, "a1", flow_usage.Files())
    assert meta.agent_type == "developer"
    assert meta.model is None


def test_meta_with_a_model_returns_both(tmp_path):
    _write_meta(
        tmp_path,
        "a1",
        json.dumps({"agentType": "workflow-subagent", "spawnDepth": 1, "model": "haiku"}),
    )
    meta = flow_usage.agent_meta(tmp_path, "a1", flow_usage.Files())
    assert meta.agent_type == "workflow-subagent"
    assert meta.model == "haiku"


def test_missing_sidecar_returns_none_for_both_and_does_not_raise(tmp_path):
    meta = flow_usage.agent_meta(tmp_path, "no-such-agent", flow_usage.Files())
    assert meta.agent_type is None
    assert meta.model is None


def test_malformed_meta_json_is_treated_as_missing_not_a_crash(tmp_path):
    _write_meta(tmp_path, "a1", "{not json")
    meta = flow_usage.agent_meta(tmp_path, "a1", flow_usage.Files())
    assert meta.agent_type is None
    assert meta.model is None


class TestFilesReadText:
    def test_reads_a_real_files_whole_text(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text('{"a": 1}')
        assert flow_usage.Files().read_text(path) == '{"a": 1}'

    def test_returns_none_for_a_missing_file(self, tmp_path):
        assert flow_usage.Files().read_text(tmp_path / "does-not-exist") is None


# ── Task 4 — an agent's spend uses the sibling's accumulator, not a copy ─────


def _usage_line(
    model="claude-sonnet-5",
    input_tokens=1,
    output_tokens=1,
    cache_read=0,
    cache_write_1h=0,
    cache_write_5m=0,
    thinking=None,
    timestamp="2026-09-07T00:00:00.000Z",
):
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write_1h + cache_write_5m,
        "cache_creation": {
            "ephemeral_1h_input_tokens": cache_write_1h,
            "ephemeral_5m_input_tokens": cache_write_5m,
        },
    }
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {"model": model, "usage": usage},
        }
    )


def _write_agent_transcript(run_dir: Path, agent_id: str, lines: list[str]) -> Path:
    path = run_dir / f"agent-{agent_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_flow_usage_defines_no_second_five_class_accumulator():
    source = inspect.getsource(flow_usage)
    assert "def spend_by_model" not in source
    assert "def usage_records" not in source


def test_agent_spend_calls_run_usages_own_functions_not_a_copy():
    source = inspect.getsource(flow_usage.agent_spend)
    assert "run_usage.usage_records(" in source
    assert "run_usage.spend_by_model(" in source


def test_a_workflow_agent_transcript_produces_the_same_modelspend_run_usage_would(tmp_path):
    _write_agent_transcript(tmp_path, "a1", [_usage_line(input_tokens=5, output_tokens=7)])
    files = flow_usage.Files()

    flow_spend = flow_usage.agent_spend(tmp_path, "a1", files)

    path = tmp_path / "agent-a1.jsonl"
    sibling_records = run_usage.usage_records(path, files)
    sibling_spend = run_usage.spend_by_model(sibling_records)

    assert flow_spend == sibling_spend
    assert flow_spend["claude-sonnet-5"].input == 5
    assert flow_spend["claude-sonnet-5"].output == 7


def test_a_missing_agent_file_returns_an_empty_spend_not_a_raise(tmp_path):
    spend = flow_usage.agent_spend(tmp_path, "no-such-agent", flow_usage.Files())
    assert spend == {}


def test_agent_spend_does_not_raise_on_a_truncated_final_line(tmp_path):
    _write_agent_transcript(tmp_path, "a1", [_usage_line()])
    path = tmp_path / "agent-a1.jsonl"
    with path.open("a") as f:
        f.write('{"type": "assistant", "message": {"usage"')  # truncated, no trailing newline
    spend = flow_usage.agent_spend(tmp_path, "a1", flow_usage.Files())
    assert spend["claude-sonnet-5"].input == 1


def test_flow_usage_defines_no_spend_accumulator_of_its_own():
    """`_add_spend` and `_merge_spend` moved to `run_usage.py` when a third reader wanted them.
    They must not grow back here: the rule they encode — no class summed with another, and a
    `thinking` of `None` meaning "nothing carried `output_tokens_details`" rather than zero — is
    one rule, and a second copy is a second place for it to drift.

    Checked in the parse tree, not by a text search: this module's own docstring names
    `run_usage.add_spend` / `merge_spend`, so a string search passes on the prose alone and would
    keep passing over a hand-rolled accumulator sitting right beneath it.
    """
    tree = ast.parse(Path(flow_usage.__file__).read_text())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not defined & {"_add_spend", "_merge_spend", "add_spend", "merge_spend"}


def test_the_fold_calls_the_siblings_accumulator():
    """And it must actually call it — the guard above only proves nothing was re-declared."""
    tree = ast.parse(Path(flow_usage.__file__).read_text())
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "run_usage.merge_spend" in called


def test_folding_two_agents_matches_the_siblings_own_arithmetic():
    """The behaviour the four moved tests covered, kept here as an end-to-end check that the fold
    this module performs is the sibling's arithmetic and not an accident of ordering."""
    a = {"sonnet": run_usage.ModelSpend(input=1, output=2)}
    b = {"sonnet": run_usage.ModelSpend(input=10, output=20), "haiku": run_usage.ModelSpend(input=5)}
    merged = run_usage.merge_spend(a, b)
    assert merged["sonnet"] == run_usage.ModelSpend(input=11, output=22)
    assert merged["haiku"] == run_usage.ModelSpend(input=5)


# ── Task 5 — a run aggregates per step, and step-less agents fall back by name ──


def _flow_run_fixture(
    tmp_path: Path,
    journal_lines: list[str],
    transcripts: dict[str, list[str]] | None = None,
    metas: dict[str, dict] | None = None,
) -> flow_usage.FlowRun:
    """A full workflow-run tree under `tmp_path`, matching the plan's real shape."""
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_test")
    (run_dir / "journal.jsonl").write_text("\n".join(journal_lines))
    for agent_id, lines in (transcripts or {}).items():
        _write_agent_transcript(run_dir, agent_id, lines)
    for agent_id, meta in (metas or {}).items():
        _write_meta(run_dir, agent_id, json.dumps(meta))
    return flow_usage.FlowRun(wf_id="wf_test", session_id="sid-1", slug="slug-a", run_dir=run_dir)


def test_read_run_reads_the_journal_and_every_named_agent(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [_usage_line(input_tokens=3)]},
        metas={"a1": {"agentType": "developer"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    assert len(run.agents) == 1
    agent = run.agents[0]
    assert agent.agent_id == "a1"
    assert agent.step == "s"
    assert agent.role == flow_usage.Role.PRODUCE
    assert agent.agent_type == "developer"
    assert agent.spend["claude-sonnet-5"].input == 3


def test_two_agents_same_step_different_role_are_two_rows(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result("a1", {"step": "development-specs", "status": "ok", "artifact": "x.md"}),
            _started("a2"),
            _result("a2", {"step": "development-specs", "result": "findings", "findings": 3}),
        ],
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    rows = flow_usage.aggregate(run)
    assert len(rows) == 2
    labels = {(r.label, r.role) for r in rows}
    assert labels == {
        ("development-specs", flow_usage.Role.PRODUCE),
        ("development-specs", flow_usage.Role.AUDIT),
    }


def test_two_agents_same_step_same_role_merge_into_one_row_with_summed_classes(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result("a1", {"step": "development-specs", "status": "ok", "artifact": "x.md"}),
            _started("a2"),
            _result("a2", {"step": "development-specs", "status": "ok", "artifact": "y.md"}),
        ],
        transcripts={
            "a1": [_usage_line(input_tokens=3)],
            "a2": [_usage_line(input_tokens=4)],
        },
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    rows = flow_usage.aggregate(run)
    assert len(rows) == 1
    row = rows[0]
    assert set(row.agent_ids) == {"a1", "a2"}
    assert row.spend["claude-sonnet-5"].input == 7


def test_agent_with_no_step_falls_back_to_its_agent_type_marked_fallback(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", "plain string, no step")],
        metas={"a1": {"agentType": "workflow-subagent"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    rows = flow_usage.aggregate(run)
    assert len(rows) == 1
    row = rows[0]
    assert row.label == "workflow-subagent"
    assert row.is_fallback is True
    assert row.role is None


def test_no_row_anywhere_carries_the_sum_of_two_classes(tmp_path):
    # Every class gets a distinct value, so any class standing in for a sum of two others
    # would land on a number no class was given. A pairwise-distinct fixture is what makes
    # that detectable at all -- with 0s or repeats a summed column reads as a correct one.
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={
            "a1": [
                _usage_line(
                    input_tokens=5,
                    output_tokens=9,
                    cache_read=17,
                    cache_write_1h=33,
                    cache_write_5m=65,
                )
            ]
        },
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    row = flow_usage.aggregate(run)[0]
    spend = row.spend["claude-sonnet-5"]
    assert (
        spend.input,
        spend.output,
        spend.cache_read,
        spend.cache_write_1h,
        spend.cache_write_5m,
    ) == (5, 9, 17, 33, 65)


def test_an_agent_named_in_the_journal_with_no_transcript_is_counted_not_skipped(tmp_path):
    # Task 4: "an agent file that does not exist returns an empty spend and is counted, not
    # skipped silently" -- the row exists with no models under it, and coverage sees the agent.
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    rows = flow_usage.aggregate(run)
    assert len(rows) == 1
    assert rows[0].agent_ids == ("a1",)
    assert rows[0].spend == {}
    assert flow_usage.coverage(run).with_step == 1


def test_an_agent_whose_sidecar_is_missing_is_still_reported(tmp_path):
    # Task 3: "a missing sidecar returns None for both and does not raise; the agent is still
    # reported" -- it does not vanish from the rows just because nothing named it.
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", "plain string, no step")],
        transcripts={"a1": [_usage_line(input_tokens=2)]},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    assert run.agents[0].agent_type is None
    rows = flow_usage.aggregate(run)
    assert len(rows) == 1
    assert rows[0].agent_ids == ("a1",)
    assert rows[0].spend["claude-sonnet-5"].input == 2


def test_a_run_with_no_agents_returns_no_rows_and_does_not_raise(tmp_path):
    flow_run = _flow_run_fixture(tmp_path, [])
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    assert flow_usage.aggregate(run) == ()


# ── Task 6 — coverage is stated, never implied ─────────────────────────


def test_each_of_the_four_figures_is_independent_an_agent_counts_in_both_2_and_3(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),  # never resolves -> without_step AND started_no_result
            _started("a2"),
            _result("a2", {"step": "s", "status": "ok", "artifact": "x.md"}),  # with_step
        ],
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    cov = flow_usage.coverage(run)
    assert cov.with_step == 1
    assert cov.without_step == 1
    assert cov.started_no_result == 1


def test_agent_file_with_no_started_record_at_all_is_counted_in_the_fourth_figure(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [], "orphan": []},  # "orphan" has a transcript file, no journal entry
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    cov = flow_usage.coverage(run)
    assert cov.files_with_no_journal_entry == 1


def test_a_fully_clean_run_reports_zeroes_for_the_other_three_figures(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": []},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    cov = flow_usage.coverage(run)
    assert cov.with_step == 1
    assert cov.without_step == 0
    assert cov.started_no_result == 0
    assert cov.files_with_no_journal_entry == 0


# ── Task 7 — the report renders, and `--json` carries the same numbers ────


def test_rendered_output_states_the_fallback_rule_and_the_coverage_figures(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [_usage_line()]},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    text = flow_usage.render([run])
    assert "fallback" in text.lower()
    assert "agenttype" in text.lower()
    assert "with a step" in text or "with_step" in text
    assert "no journal entry" in text


def test_no_rendered_column_or_total_row_sums_two_classes(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [_usage_line(input_tokens=11, output_tokens=13)]},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    text = flow_usage.render([run])
    assert "input=11" in text
    assert "output=13" in text
    assert "24" not in text  # 11 + 13, would only appear if a column summed two classes


def test_json_round_trips_and_carries_every_figure_the_table_shows(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [_usage_line(input_tokens=5)]},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    payload = json.loads(flow_usage.render_json([run]))
    assert payload["runs"][0]["wf_id"] == "wf_test"
    assert payload["runs"][0]["coverage"]["with_step"] == 1
    assert payload["runs"][0]["rows"][0]["spend"]["claude-sonnet-5"]["input"] == 5


def test_the_rendered_table_names_the_agent_type_of_a_step_row(tmp_path):
    # Task 7 names the table's columns: "run, step, role, agent type, model, five classes,
    # thinking". The agent type is one of them, and it is a different fact from the step and
    # from the role -- a reader must be able to see which agent ran `development-specs`, not
    # only that something did. The fallback row happens to be *keyed* by the agent type, so a
    # step row is the only place this column can be checked for.
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result(
                "a1", {"step": "development-specs", "status": "ok", "artifact": "x.md"}
            ),
        ],
        transcripts={"a1": [_usage_line()]},
        metas={"a1": {"agentType": "product-owner", "model": "claude-opus-5"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    text = flow_usage.render([run])
    step_line = next(
        line for line in text.splitlines() if "development-specs" in line
    )
    assert "product-owner" in step_line


def test_json_carries_the_agent_type_the_table_shows(tmp_path):
    # Task 7: "the `--json` path emits the same figures as data" -- the agent-type column is
    # one of them, so a reader consuming the JSON is not handed less than the table shows.
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result(
                "a1", {"step": "development-specs", "status": "ok", "artifact": "x.md"}
            ),
        ],
        transcripts={"a1": [_usage_line()]},
        metas={"a1": {"agentType": "product-owner", "model": "claude-opus-5"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    payload = json.loads(flow_usage.render_json([run]))
    assert payload["runs"][0]["rows"][0]["agent_types"] == ["product-owner"]


def test_thinking_is_rendered_as_a_share_of_output_never_added_to_it(tmp_path):
    # Reached-when 3: "thinking is reported as a share of output, never added to it." The
    # rendered output figure stays exactly what the transcript carried; thinking is reported
    # beside it as its own figure, and 40 + 100 appears nowhere.
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})],
        transcripts={"a1": [_usage_line(input_tokens=7, output_tokens=100, thinking=40)]},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    text = flow_usage.render([run])
    assert "output=100" in text
    assert "thinking=40" in text
    assert "output=140" not in text
    payload = json.loads(flow_usage.render_json([run]))
    spend = payload["runs"][0]["rows"][0]["spend"]["claude-sonnet-5"]
    assert spend["output"] == 100
    assert spend["thinking"] == 40


def test_a_run_with_no_readable_agents_renders_a_line_saying_so(tmp_path):
    flow_run = _flow_run_fixture(tmp_path, [])
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    text = flow_usage.render([run])
    assert "no agents" in text.lower()


def test_render_with_no_runs_at_all_says_so_rather_than_an_empty_table():
    text = flow_usage.render([])
    assert "no workflow runs" in text.lower()


# ── Task 8 — the CLI ────────────────────────────


class TestCompositionRoot:
    def test_no_module_level_name_holds_the_expanded_projects_root(self):
        default_root = Path.home() / ".claude" / "projects"
        holders = [
            name
            for name, value in vars(flow_usage).items()
            if value == default_root or value == str(default_root)
        ]
        assert holders == []

    def test_build_reader_resolves_the_default_projects_root(self):
        reader = flow_usage.build_reader()
        assert reader.projects_root == Path.home() / ".claude" / "projects"
        assert isinstance(reader.files, flow_usage.Files)

    def test_build_reader_honors_an_explicit_projects_root(self, tmp_path):
        reader = flow_usage.build_reader(tmp_path)
        assert reader.projects_root == tmp_path

    def test_only_build_reader_constructs_a_files(self):
        source = Path(flow_usage.__file__).read_text()
        import ast as _ast

        tree = _ast.parse(source)
        constructing_functions = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                for call in _ast.walk(node):
                    if isinstance(call, _ast.Call) and isinstance(call.func, _ast.Name):
                        if call.func.id == "Files":
                            constructing_functions.add(node.name)
        assert constructing_functions == {"build_reader"}

    def test_main_obtains_ports_from_build_reader_and_constructs_none_itself(self):
        source = inspect.getsource(flow_usage.main)
        assert "build_reader(" in source
        assert "Files(" not in source


def test_no_arguments_reads_every_workflow_run_under_the_projects_root(tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_two")
    exit_code = flow_usage.main(["--projects-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "wf_one" in out
    assert "wf_two" in out


def test_wf_flag_reads_only_that_run(tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_two")
    exit_code = flow_usage.main(["--projects-root", str(tmp_path), "--wf", "wf_one"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "wf_one" in out
    assert "wf_two" not in out


def test_unknown_wf_id_exits_non_zero_naming_the_id(tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    exit_code = flow_usage.main(["--projects-root", str(tmp_path), "--wf", "wf_does_not_exist"])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "wf_does_not_exist" in err


def test_json_and_table_report_the_same_numbers(tmp_path, capsys):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    (run_dir / "journal.jsonl").write_text(
        "\n".join(
            [_started("a1"), _result("a1", {"step": "s", "status": "ok", "artifact": "x.md"})]
        )
    )
    _write_agent_transcript(run_dir, "a1", [_usage_line(input_tokens=9)])

    flow_usage.main(["--projects-root", str(tmp_path)])
    table_out = capsys.readouterr().out
    assert "input=9" in table_out

    flow_usage.main(["--projects-root", str(tmp_path), "--json"])
    json_out = capsys.readouterr().out
    payload = json.loads(json_out)
    assert payload["runs"][0]["rows"][0]["spend"]["claude-sonnet-5"]["input"] == 9


# ── The package placement: the step reader's own words and shapes ─────────────

FALLBACK_LINE = (
    "Fallback: an agent whose journal result carries no step (or none this reader could "
    "attribute to a producer or an auditor) is grouped by its agentType instead, marked "
    "fallback so a reader can tell a step from a name."
)
PRODUCED = {"step": "development-specs", "status": "ok", "artifact": "x.md"}
AUDITED = {"step": "development-specs", "result": "findings", "findings": 0}


def _mixed_run(tmp_path: Path) -> flow_usage.FlowRun:
    """One run holding a producer, an auditor, two fallback agents and one orphan file."""
    return _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result("a1", PRODUCED),
            _started("a2"),
            _result("a2", AUDITED),
            _started("a3"),
            _result("a3", "plain text, no step"),
            _started("a4"),
            _result("a4", {"status": "ok"}),
        ],
        transcripts={
            "a1": [_usage_line(input_tokens=10, output_tokens=20, thinking=3)],
            "a2": [_usage_line(input_tokens=4, output_tokens=5)],
            "a3": [_usage_line(input_tokens=6, output_tokens=7)],
            "a4": [_usage_line(input_tokens=8, output_tokens=9)],
            "a9": [_usage_line()],
        },
        metas={
            "a1": {"agentType": "product-owner"},
            "a2": {"agentType": "auditor"},
            "a3": {"agentType": "Explore"},
        },
    )


def test_missing_projects_root_and_empty_tree_read_no_workflow_runs(tmp_path, capsys):
    expected = FALLBACK_LINE + "\n" + SPAN + "\n\nNo workflow runs read.\n" + NO_COST + "\n"

    assert flow_usage.main(["--projects-root", str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out == expected

    assert flow_usage.main(["--projects-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == expected


def test_missing_selected_workflow_errors(tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")

    exit_code = flow_usage.main(["--projects-root", str(tmp_path), "--wf", "wf_nope"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        f"error: no workflow run found for --wf wf_nope under {tmp_path} — run "
        "`ka-telemetry steps` without --wf to see the workflow runs found there\n"
    )


def test_workflow_ids_and_agent_ids_are_sorted_explicitly(tmp_path, capsys):
    for slug, sid, wf_id in (("slug-b", "sid-1", "wf_b"), ("slug-a", "sid-2", "wf_a")):
        run_dir = _make_workflow_run(tmp_path, slug, sid, wf_id)
        # The journal names its agents out of order: the reader sorts, it does not trust order.
        (run_dir / "journal.jsonl").write_text(
            "\n".join(
                [_started("a2"), _result("a2", "no step"), _started("a1"), _result("a1", "no step")]
            )
        )
    flow_usage.main(["--projects-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.index("wf_a  session=") < out.index("wf_b  session=")

    run = flow_usage.read_run(flow_usage.flow_runs(tmp_path, flow_usage.Files())[0], flow_usage.Files())
    assert [agent.agent_id for agent in run.agents] == ["a1", "a2"]


def test_a_row_sits_where_its_first_agent_falls(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a3"),
            _result("a3", "no step"),
            _started("a2"),
            _result("a2", {"step": "s", "status": "ok", "artifact": "x.md"}),
            _started("a1"),
            _result("a1", "no step"),
        ],
        metas={"a1": {"agentType": "Explore"}, "a3": {"agentType": "Explore"}},
    )
    rows = flow_usage.aggregate(flow_usage.read_run(flow_run, flow_usage.Files()))

    assert [(row.label, row.agent_ids) for row in rows] == [
        ("Explore", ("a1", "a3")),
        ("s", ("a2",)),
    ]


def test_producer_auditor_and_fallback_rows(tmp_path):
    run = flow_usage.read_run(_mixed_run(tmp_path), flow_usage.Files())
    lines = flow_usage.render([run]).splitlines()

    assert "  development-specs (produce)  types=product-owner  agents=a1" in lines
    assert "  development-specs (audit)  types=auditor  agents=a2" in lines
    assert "  Explore [fallback]  types=Explore  agents=a3" in lines
    assert "  (unknown) [fallback]  types=(none)  agents=a4" in lines


def test_journal_with_no_agents_prints_no_agents_read(tmp_path):
    run = flow_usage.read_run(_flow_run_fixture(tmp_path, []), flow_usage.Files())

    assert flow_usage.render([run]).splitlines()[3:6] == [
        "wf_test  session=sid-1  slug=slug-a",
        "  Coverage: 0 with a step / 0 without / 0 started with no result / "
        "0 agent files with no journal entry at all",
        "  No agents read for this run.",
    ]


def test_every_coverage_field(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result("a1", PRODUCED),
            _started("a2"),
            _result("a2", "no step"),
            _started("a3"),
            _started("a4"),
            _failed("a4"),
        ],
        transcripts={"a9": [_usage_line()]},
    )
    cov = flow_usage.coverage(flow_usage.read_run(flow_run, flow_usage.Files()))

    assert cov == flow_usage.Coverage(
        with_step=1,
        without_step=3,
        started_no_result=1,
        files_with_no_journal_entry=1,
        unreadable_agent_transcripts=4,
    )


def test_missing_sidecar_missing_transcript_unfinished_agent_and_orphan_stay_distinct(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"),
            _result("a1", PRODUCED),
            _started("a2"),
            _result("a2", AUDITED),
            _started("a3"),
        ],
        transcripts={"a1": [_usage_line()], "a3": [_usage_line()], "a9": [_usage_line()]},
        metas={"a2": {"agentType": "auditor"}, "a3": {"agentType": "x"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())
    by_id = {agent.agent_id: agent for agent in run.agents}

    assert by_id["a1"].agent_type is None and by_id["a1"].spend  # no sidecar, transcript read
    assert by_id["a2"].agent_type == "auditor" and by_id["a2"].spend == {}  # no transcript
    assert by_id["a3"].unfinished and not by_id["a1"].unfinished  # started, no result
    assert run.orphan_agent_files == ("a9",)  # a file the journal never names


def test_empty_or_malformed_journal_and_meta_are_tolerated(tmp_path, capsys):
    flow_run = _flow_run_fixture(
        tmp_path,
        ["{not json", "", '"just a string"', _started("a1"), _result("a1", PRODUCED)],
        transcripts={"a1": [_usage_line()]},
    )
    _write_meta(flow_run.run_dir, "a1", "{not json")

    assert flow_usage.main(["--projects-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "  development-specs (produce)  types=(none)  agents=a1" in out


def test_distinct_agent_types_order_named_first_in_name_order_with_none_marker(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started(a) + "\n" + _result(a, PRODUCED) for a in ("a1", "a2", "a3", "a4")],
        metas={"a1": {"agentType": "zeta"}, "a2": {"agentType": "alpha"}, "a3": {"agentType": "zeta"}},
    )
    run = flow_usage.read_run(flow_run, flow_usage.Files())

    (row,) = flow_usage.aggregate(run)
    assert row.agent_types == ("alpha", "zeta", None)
    assert "types=alpha,zeta,(none)" in flow_usage.render([run])


def test_json_shape_has_fallback_and_runs(tmp_path, capsys):
    _mixed_run(tmp_path)

    assert flow_usage.main(["--projects-root", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out

    assert out.startswith('{\n  "fallback": ')
    assert out.endswith("}\n") and not out.endswith("\n\n")
    payload = json.loads(out)
    assert list(payload) == ["fallback", "span", "runs"]
    assert payload["fallback"] == FALLBACK_LINE
    assert payload["span"] == SPAN
    (run,) = payload["runs"]
    assert list(run) == ["wf_id", "session_id", "slug", "coverage", "rows"]
    assert list(run["coverage"]) == [
        "with_step",
        "without_step",
        "started_no_result",
        "files_with_no_journal_entry",
    ]
    producer, auditor, explore, unknown = run["rows"]
    assert list(producer) == [
        "span_seconds",
        "label",
        "role",
        "is_fallback",
        "agent_ids",
        "agent_types",
        "spend",
    ]
    assert list(producer["spend"]["claude-sonnet-5"]) == [
        "input",
        "output",
        "thinking",
        "cache_read",
        "cache_write_1h",
        "cache_write_5m",
    ]
    assert producer["role"] == "produce" and producer["is_fallback"] is False
    assert explore["role"] is None and explore["is_fallback"] is True
    assert unknown["agent_types"] == [None]
    assert auditor["spend"]["claude-sonnet-5"]["thinking"] is None


def test_table_text_and_trailing_newline_match_the_spec_exactly(tmp_path, capsys):
    _mixed_run(tmp_path)

    assert flow_usage.main(["--projects-root", str(tmp_path)]) == 0

    assert capsys.readouterr().out == (
        FALLBACK_LINE + "\n"
        + SPAN + "\n"
        "\n"
        "wf_test  session=sid-1  slug=slug-a\n"
        "  Coverage: 2 with a step / 2 without / 0 started with no result / "
        "1 agent files with no journal entry at all\n"
        "  development-specs (produce)  types=product-owner  agents=a1\n"
        "    span=0s\n"
        "    claude-sonnet-5: input=10 output=20 thinking=3 cache_read=0 cache_write_1h=0 "
        "cache_write_5m=0\n"
        "  development-specs (audit)  types=auditor  agents=a2\n"
        "    span=0s\n"
        "    claude-sonnet-5: input=4 output=5 cache_read=0 cache_write_1h=0 cache_write_5m=0\n"
        "  Explore [fallback]  types=Explore  agents=a3\n"
        "    span=0s\n"
        "    claude-sonnet-5: input=6 output=7 cache_read=0 cache_write_1h=0 cache_write_5m=0\n"
        "  (unknown) [fallback]  types=(none)  agents=a4\n"
        "    span=0s\n"
        "    claude-sonnet-5: input=8 output=9 cache_read=0 cache_write_1h=0 cache_write_5m=0\n"
        + NO_COST
        + "\n"
    )


def test_library_typed_rows_equal_cli_json(tmp_path, capsys):
    _mixed_run(tmp_path)
    reader = flow_usage.build_reader(tmp_path)
    runs = [
        flow_usage.read_run(r, reader.files)
        for r in flow_usage.flow_runs(tmp_path, reader.files)
    ]

    flow_usage.main(["--projects-root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    typed = [
        {
            "span_seconds": row.span_seconds,
            "label": row.label,
            "role": row.role.value if row.role else None,
            "is_fallback": row.is_fallback,
            "agent_ids": list(row.agent_ids),
            "agent_types": list(row.agent_types),
            "spend": {
                model: {
                    "input": spend.input,
                    "output": spend.output,
                    "thinking": spend.thinking,
                    "cache_read": spend.cache_read,
                    "cache_write_1h": spend.cache_write_1h,
                    "cache_write_5m": spend.cache_write_5m,
                }
                for model, spend in row.spend.items()
            },
        }
        for run in runs
        for row in flow_usage.aggregate(run)
    ]
    assert typed == [row for run in payload["runs"] for row in run["rows"]]


@pytest.mark.parametrize(
    "argv",
    [["--run-logs", "x"], ["--projects-roo", "x"], ["--agents-json", "x"]],
)
def test_a_flag_the_command_does_not_take_is_a_usage_error_with_a_next_step(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        flow_usage.main(argv)

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: ka-telemetry steps")
    assert err.splitlines()[-1] == "Run `ka-telemetry steps --help` to see what it accepts."


def test_help_carries_the_published_description_and_flag_words(capsys):
    with pytest.raises(SystemExit) as raised:
        flow_usage.main(["--help"])

    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for words in (
        "Reports the tokens each step of a workflow run used, read from the workflow run's "
        "journal and its agents' transcripts.",
        "An agent whose journal entry names no step is grouped under its agent type and "
        "marked [fallback].",
        "Thinking tokens are part of output, not added to it.",
        "the directory that holds Claude Code's transcripts; workflow runs are found under "
        "<project folder>/<session id>/subagents/workflows/<workflow run id>/ "
        "(default: ~/.claude/projects)",
        "report only the workflow run with this id (default: every workflow run found)",
        "print the report as JSON instead of a table",
    ):
        assert words in text


# ── The unreadable-line count a workflow run's agent transcripts carry ──

BAD_LINE = "not json {"


def _counted_run(tmp_path: Path, journal: list[str], transcripts: dict[str, list[str]]):
    return flow_usage.read_run(
        _flow_run_fixture(tmp_path, journal, transcripts=transcripts), flow_usage.Files()
    )


def test_workflow_count_sums_every_distinct_journal_named_agent(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    run = _counted_run(
        tmp_path,
        [
            _started("a1"), _result("a1", produced),  # attributed
            _started("a2"), _result("a2", "no step"),  # step-less
            _started("a3"),  # unfinished
            _started("a4"), _failed("a4"),  # failed
        ],
        {
            "a1": [BAD_LINE, _usage_line()],
            "a2": [_usage_line(), BAD_LINE, BAD_LINE],
            "a3": [BAD_LINE],
            "a4": [BAD_LINE, BAD_LINE, BAD_LINE],
        },
    )

    assert {agent.agent_id: agent.unfinished for agent in run.agents}["a3"] is True
    assert {agent.agent_id: agent.unreadable_lines for agent in run.agents} == {
        "a1": 1, "a2": 2, "a3": 1, "a4": 3,
    }
    assert flow_usage.coverage(run).unreadable_lines == 7


def test_agent_named_twice_in_the_journal_counts_its_transcript_once(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    run = _counted_run(
        tmp_path,
        [_started("a1"), _started("a1"), _result("a1", produced), _result("a1", produced)],
        {"a1": [BAD_LINE, _usage_line()]},
    )

    assert [agent.unreadable_lines for agent in run.agents] == [1]
    assert flow_usage.coverage(run).unreadable_lines == 1


def test_bad_lines_in_orphan_transcripts_journal_meta_and_other_workflows_do_not_enter_the_count(
    tmp_path,
):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    flow_run = _flow_run_fixture(
        tmp_path,
        [BAD_LINE, _started("a1"), _result("a1", produced)],
        transcripts={"a1": [_usage_line()], "orphan": [BAD_LINE, BAD_LINE]},
        metas={},
    )
    _write_meta(flow_run.run_dir, "a1", BAD_LINE)
    other = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_other")
    (other / "journal.jsonl").write_text("\n".join([_started("b1"), _result("b1", produced)]))
    _write_agent_transcript(other, "b1", [BAD_LINE, BAD_LINE, BAD_LINE])

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert run.orphan_agent_files == ("orphan",)
    cov = flow_usage.coverage(run)
    assert cov.unreadable_lines == 0
    assert (cov.unreadable_journal_lines, cov.unreadable_meta_files) == (1, 1)
    assert cov.unreadable_agent_transcripts == 0
    assert [agent.unreadable_lines for agent in run.agents] == [0]


def test_missing_agent_transcript_keeps_existing_spend_and_coverage_and_adds_zero(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    run = _counted_run(
        tmp_path,
        [_started("a1"), _result("a1", produced), _started("a2"), _result("a2", produced)],
        {"a1": [BAD_LINE, _usage_line(input_tokens=4)]},
    )
    by_id = {agent.agent_id: agent for agent in run.agents}

    assert by_id["a2"].spend == {} and by_id["a2"].unreadable_lines == 0
    assert by_id["a1"].spend["claude-sonnet-5"].input == 4
    cov = flow_usage.coverage(run)
    assert (cov.with_step, cov.without_step, cov.unreadable_lines) == (2, 0, 1)


def test_bad_lines_change_no_spend_or_span(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    journal = [_started("a1"), _result("a1", produced)]
    clean = _counted_run(tmp_path / "clean", journal, {"a1": [_usage_line(input_tokens=4)]})
    dirty = _counted_run(
        tmp_path / "dirty", journal, {"a1": [BAD_LINE, _usage_line(input_tokens=4), BAD_LINE]}
    )

    assert [a.spend for a in dirty.agents] == [a.spend for a in clean.agents]
    assert flow_usage.aggregate(dirty) == flow_usage.aggregate(clean)


def test_agent_spend_without_a_counter_behaves_as_before_and_with_one_counts(tmp_path):
    _write_agent_transcript(tmp_path, "a1", [BAD_LINE, _usage_line(input_tokens=5), BAD_LINE])
    files = flow_usage.Files()
    counter = run_usage.UnreadableLines()

    counted = flow_usage.agent_spend(tmp_path, "a1", files, unreadable_lines=counter)
    uncounted = flow_usage.agent_spend(tmp_path, "a1", files)

    assert counted == uncounted
    assert counted["claude-sonnet-5"].input == 5
    assert counter.count == 2
    source = inspect.getsource(flow_usage.agent_spend)
    assert "run_usage.usage_records(" in source and "run_usage.spend_by_model(" in source


def test_read_run_agent_count_and_coverage_count_equal(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    run = _counted_run(
        tmp_path,
        [_started("a1"), _result("a1", produced), _started("a2"), _result("a2", "no step")],
        {"a1": [BAD_LINE, BAD_LINE, _usage_line()], "a2": [BAD_LINE, BAD_LINE, BAD_LINE]},
    )

    assert sum(agent.unreadable_lines for agent in run.agents) == 5
    assert flow_usage.coverage(run).unreadable_lines == 5


def test_legacy_constructors_default_the_new_fields_to_zero():
    assert flow_usage.Coverage(1, 2, 3, 4).unreadable_lines == 0
    assert flow_usage.Coverage(1, 2, 3, 4) == flow_usage.Coverage(1, 2, 3, 4, unreadable_lines=0)
    agent = flow_usage.AgentReading(
        agent_id="a1", step=None, role=None, agent_type=None, model_hint=None
    )
    assert agent.unreadable_lines == 0
    assert agent == flow_usage.AgentReading(
        agent_id="a1", step=None, role=None, agent_type=None, model_hint=None, unreadable_lines=0
    )


# ── `steps` states what it holds: the no-cost line, the pointers, the unreadable lines ──


def _stream_home(tmp_path, *session_ids, extra_lines=()):
    """A `HOME` whose `.ka-telemetry/logs.jsonl` holds one `api_request` for each session id."""
    home = tmp_path / "stream-home"
    stream = home / ".ka-telemetry"
    stream.mkdir(parents=True)
    lines = [json.dumps({"body": api_request_body(sid, 0.5)}) for sid in session_ids]
    (stream / "logs.jsonl").write_text("".join(f"{line}\n" for line in (*lines, *extra_lines)))
    return home


def _workflows(tmp_path, *pairs):
    """One workflow run, with no agents, for each `(wf_id, parent_session_id)`."""
    for wf_id, session_id in pairs:
        _make_workflow_run(tmp_path, "slug-a", session_id, wf_id)


def _steps(tmp_path, capsys, *extra):
    status = flow_usage.main(["--projects-root", str(tmp_path), *extra])
    captured = capsys.readouterr()
    assert (status, captured.err) == (0, "")
    return captured.out


def _count_no_cost(text):
    return len(re.findall("carries no cost", text, flags=re.IGNORECASE))


def test_steps_report_has_one_no_cost_line_for_the_whole_report_not_per_workflow(
    tmp_path, capsys
):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-2"), ("wf_c", "sid-3"))

    out = _steps(tmp_path, capsys)

    assert _count_no_cost(out) == 1
    assert out.splitlines()[-1] == NO_COST
    assert out.endswith(NO_COST + "\n") and not out.endswith("\n\n")


def test_report_with_no_workflow_runs_has_the_whole_report_block_and_its_existing_empty_message(
    tmp_path, capsys
):
    out = _steps(tmp_path / "absent", capsys)

    assert out.splitlines()[-2:] == ["No workflow runs read.", NO_COST]
    assert _count_no_cost(out) == 1

    _workflows(tmp_path, ("wf_a", "sid-1"))
    with_one = _steps(tmp_path, capsys).splitlines()
    assert with_one[-2:] == ["  No agents read for this run.", NO_COST]


def test_the_block_follows_the_last_line_with_no_blank_line_between(tmp_path, capsys):
    _mixed_run(tmp_path)

    lines = _steps(tmp_path, capsys).splitlines()

    assert lines[-1] == NO_COST
    assert lines[-2] == (
        "    claude-sonnet-5: input=8 output=9 cache_read=0 cache_write_1h=0 cache_write_5m=0"
    )


def test_pointers_sit_directly_under_the_no_cost_line_in_displayed_workflow_order(
    tmp_path, capsys, monkeypatch
):
    _workflows(tmp_path, ("wf_a", "sid-2"), ("wf_b", "sid-1"), ("wf_c", "sid-3"))
    # the stream names the sessions in the opposite order to the one the report displays them in
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-3", "sid-1", "sid-2")))

    out = _steps(tmp_path, capsys)

    assert out.splitlines()[-4:] == [
        NO_COST, _pointer("sid-2"), _pointer("sid-1"), _pointer("sid-3")
    ]


def test_two_workflows_sharing_a_parent_session_yield_one_pointer_for_it(
    tmp_path, capsys, monkeypatch
):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-1"))
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-1")))

    out = _steps(tmp_path, capsys)

    assert out.splitlines()[-2:] == [NO_COST, _pointer("sid-1")]
    assert out.count("ka-telemetry agents") == 1


def test_wf_selection_matches_only_the_selected_runs_parent_session(
    tmp_path, capsys, monkeypatch
):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-2"))
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-1", "sid-2")))

    out = _steps(tmp_path, capsys, "--wf", "wf_b")

    assert out.splitlines()[-2:] == [NO_COST, _pointer("sid-2")]
    assert "sid-1" not in out


def test_unmatched_parent_session_gets_no_pointer(tmp_path, capsys, monkeypatch):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-2"))
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-2", "sid-elsewhere")))

    out = _steps(tmp_path, capsys)

    assert out.splitlines()[-2:] == [NO_COST, _pointer("sid-2")]
    assert "sid-elsewhere" not in out
    assert _pointer("sid-1") not in out


@pytest.mark.parametrize("problem", ["absent-directory", "empty-logs", "bad-lines", "unopenable"])
def test_stream_problems_change_nothing_but_the_pointers_in_steps(
    problem, tmp_path, capsys, monkeypatch, seal
):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-2"))
    baseline = _steps(tmp_path, capsys)
    if problem == "absent-directory":
        home = tmp_path / "no-stream-here"
        home.mkdir()
    elif problem == "empty-logs":
        home = _stream_home(tmp_path)
    elif problem == "bad-lines":
        home = _stream_home(tmp_path, extra_lines=("not json {", "[1, 2]", "{}"))
    else:
        home = _stream_home(tmp_path, "sid-1")
        seal(home / ".ka-telemetry" / "logs.jsonl")
    monkeypatch.setenv("HOME", str(home))

    assert _steps(tmp_path, capsys) == baseline


def test_steps_reader_without_notes_reader_reads_no_stream(tmp_path):
    _workflows(tmp_path, ("wf_a", "sid-1"))
    reader = flow_usage.Reader(files=flow_usage.Files(), projects_root=tmp_path)
    runs = [
        flow_usage.read_run(r, reader.files)
        for r in flow_usage.flow_runs(tmp_path, reader.files)
    ]

    assert reader.notes_reader is None
    assert flow_usage.report_matches(runs, reader) == ()


def test_report_matches_asks_for_the_parent_sessions_in_displayed_order_in_one_read(tmp_path):
    _workflows(tmp_path, ("wf_a", "sid-2"), ("wf_b", "sid-1"), ("wf_c", "sid-2"))
    stream_files = FakeStreamFiles(
        [json.dumps({"body": api_request_body(s, 1.0)}) for s in ("sid-1", "sid-2", "sid-gone")]
    )
    reader = dataclasses.replace(
        flow_usage.build_reader(tmp_path),
        notes_reader=stream_usage.Reader(files=stream_files, directory=Path("/stream")),
    )
    runs = [
        flow_usage.read_run(r, reader.files)
        for r in flow_usage.flow_runs(tmp_path, reader.files)
    ]

    assert flow_usage.report_matches(runs, reader) == ("sid-2", "sid-1")
    assert len(stream_files.read_paths) == 1


def test_build_reader_wires_the_stream_reader_for_the_default_directory():
    reader = flow_usage.build_reader()

    assert isinstance(reader.notes_reader, stream_usage.Reader)
    assert reader.notes_reader.directory == Path(os.environ["HOME"]) / ".ka-telemetry"


def test_render_given_matched_sessions_ends_with_the_block():
    run = flow_usage.RunReading(
        flow_run=flow_usage.FlowRun(wf_id="wf_a", session_id="sid-1", slug="s", run_dir=Path("/x")),
        agents=(),
        orphan_agent_files=(),
    )

    lines = flow_usage.render([run], matched_sessions=("sid-1", "sid-9")).splitlines()

    assert lines[-3:] == [NO_COST, _pointer("sid-1"), _pointer("sid-9")]
    assert flow_usage.render([run]).splitlines()[-1] == NO_COST


def test_steps_json_form_has_no_cost_word_no_note_member_and_does_no_stream_read(
    tmp_path, capsys, monkeypatch
):
    _mixed_run(tmp_path)
    reads = []
    original = stream_usage.Files.read_outcome

    def spy(self, path):
        reads.append(path)
        return original(self, path)

    monkeypatch.setattr(stream_usage.Files, "read_outcome", spy)
    empty_json = _steps(tmp_path, capsys, "--json")
    monkeypatch.setenv("HOME", str(_stream_home(tmp_path, "sid-1")))
    matched_json = _steps(tmp_path, capsys, "--json")

    assert reads == []
    assert matched_json == empty_json
    assert "cost" not in matched_json.lower()
    assert set(json.loads(matched_json)) == {"fallback", "span", "runs"}


def _run_with_bad_lines(tmp_path, count):
    produced = {"step": "development-specs", "status": "ok", "artifact": "change_spec.md"}
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", produced)],
        transcripts={"a1": [BAD_LINE] * count + [_usage_line()]},
    )
    return flow_run


COVERAGE_FIGURES = (
    "Coverage: 1 with a step / 0 without / 0 started with no result / "
    "0 agent files with no journal entry at all"
)


def test_workflow_coverage_line_is_unchanged_at_zero_and_adds_the_count_when_positive(
    tmp_path, capsys
):
    _run_with_bad_lines(tmp_path / "none", 0)
    _run_with_bad_lines(tmp_path / "one", 1)
    _run_with_bad_lines(tmp_path / "three", 3)

    at_zero = _steps(tmp_path / "none", capsys).splitlines()
    at_one = _steps(tmp_path / "one", capsys).splitlines()
    at_three = _steps(tmp_path / "three", capsys).splitlines()

    assert f"  {COVERAGE_FIGURES}" in at_zero
    assert (
        f"  {COVERAGE_FIGURES} — 1 unreadable line skipped in this workflow run's "
        "agent transcripts" in at_one
    )
    assert (
        f"  {COVERAGE_FIGURES} — 3 unreadable lines skipped in this workflow run's agent "
        "transcripts" in at_three
    )


def test_the_count_is_over_each_workflows_own_agent_transcripts(tmp_path, capsys):
    _run_with_bad_lines(tmp_path, 2)
    other = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_other")
    (other / "journal.jsonl").write_text("")

    coverage_lines = [
        line for line in _steps(tmp_path, capsys).splitlines() if line.startswith("  Coverage:")
    ]

    assert len(coverage_lines) == 2
    assert "2 unreadable lines skipped" in coverage_lines[1]
    assert "unreadable" not in coverage_lines[0]


def test_json_workflow_coverage_gains_unreadable_lines_only_when_positive(tmp_path, capsys):
    _run_with_bad_lines(tmp_path / "none", 0)
    _run_with_bad_lines(tmp_path / "one", 1)
    _run_with_bad_lines(tmp_path / "three", 3)

    at_zero = json.loads(_steps(tmp_path / "none", capsys, "--json"))["runs"][0]
    at_one = json.loads(_steps(tmp_path / "one", capsys, "--json"))["runs"][0]
    at_three = json.loads(_steps(tmp_path / "three", capsys, "--json"))["runs"][0]

    assert list(at_zero["coverage"]) == [
        "with_step", "without_step", "started_no_result", "files_with_no_journal_entry",
    ]
    assert at_one["coverage"] == {**at_zero["coverage"], "unreadable_lines": 1}
    assert at_three["coverage"]["unreadable_lines"] == 3
    assert at_one["rows"] == at_zero["rows"] == at_three["rows"]
    assert all("unreadable_lines" not in row for row in at_three["rows"])


# ── Journal lines the report could not read (CP-416) ──

JOURNAL_BAD_JSON = "not json {"


def _journal_line(**members) -> str:
    return json.dumps(members)


@pytest.mark.parametrize("value", ["not json {", "{", '{"agentId": "a1"', "[1, 2"])
def test_invalid_json_journal_line_is_counted_and_adds_nothing(value):
    parsed = flow_usage.steps_from("\n".join([_started("a1"), value]))

    assert parsed.unreadable_journal_lines == 1
    assert parsed.unfinished == ("a1",)
    assert (parsed.steps, parsed.step_less, parsed.failed) == ((), (), ())


@pytest.mark.parametrize("value", ["[1, 2]", '"s"', "42", "null", "true", "false", "1.5"])
def test_non_object_journal_line_is_counted(value):
    parsed = flow_usage.steps_from("\n".join([_started("a1"), value]))

    assert parsed.unreadable_journal_lines == 1
    assert parsed.unfinished == ("a1",)


@pytest.mark.parametrize("agent_id", [["a"], 5, 1.5, True, False, {"a": 1}])
@pytest.mark.parametrize("record_type", ["started", "failed", "result", "launched"])
def test_an_invalid_agent_id_is_counted(agent_id, record_type):
    parsed = flow_usage.steps_from(_journal_line(agentId=agent_id, type=record_type))

    assert parsed.unreadable_journal_lines == 1
    assert (parsed.steps, parsed.step_less, parsed.unfinished, parsed.failed) == (
        (),
        (),
        (),
        (),
    )


@pytest.mark.parametrize("step", [["s"], 5, 1.5, True, False, {"a": 1}])
def test_an_invalid_result_step_is_counted(step):
    line = _result("a1", {"step": step, "status": "ok"})
    no_agent = _journal_line(type="result", result={"step": step, "status": "ok"})

    assert flow_usage.steps_from(line).unreadable_journal_lines == 1
    assert flow_usage.steps_from(no_agent).unreadable_journal_lines == 1


def test_a_rejected_started_failed_or_result_record_does_not_mutate_state():
    journal = "\n".join(
        [
            _started("a1"),
            _journal_line(type="started", agentId=["x"]),
            _journal_line(type="failed", agentId=5),
            _result("a1", {"step": ["s"], "status": "ok"}),
            _result("a2", {"step": 5, "status": "ok"}),
        ]
    )

    parsed = flow_usage.steps_from(journal)

    assert parsed.unreadable_journal_lines == 4
    assert (parsed.steps, parsed.step_less, parsed.failed) == ((), (), ())
    assert parsed.unfinished == ("a1",)


def test_an_agent_whose_only_result_line_is_rejected_reads_as_started_with_no_result():
    journal = "\n".join([_started("a1"), _result("a1", {"step": ["s"], "status": "ok"})])

    parsed = flow_usage.steps_from(journal)
    assert parsed.unfinished == ("a1",)
    assert parsed.unreadable_journal_lines == 1
    assert dataclasses.replace(parsed, unreadable_journal_lines=0) == flow_usage.steps_from(
        _started("a1")
    )


@pytest.mark.parametrize("agent_id", ["__absent__", None, ""])
def test_absent_null_or_empty_agent_id_is_passed_over_uncounted(agent_id):
    members = {"type": "result", "result": {"step": "s", "status": "ok"}}
    if agent_id != "__absent__":
        members["agentId"] = agent_id

    parsed = flow_usage.steps_from("\n".join([json.dumps(members), _started("a1")]))

    assert parsed.unreadable_journal_lines == 0
    assert parsed.steps == ()
    assert parsed.unfinished == ("a1",)


@pytest.mark.parametrize("payload", ["text", [1], 5, None, True])
def test_result_payloads_that_are_not_objects_keep_existing_attribution(payload):
    parsed = flow_usage.steps_from("\n".join([_started("a1"), _result("a1", payload)]))

    assert parsed.unreadable_journal_lines == 0
    assert parsed.step_less == ("a1",)


@pytest.mark.parametrize("members", [{}, {"step": None}, {"step": ""}, {"step": "s"}])
def test_absent_null_or_string_steps_keep_existing_attribution(members):
    parsed = flow_usage.steps_from("\n".join([_started("a1"), _result("a1", members)]))

    assert parsed.unreadable_journal_lines == 0
    assert parsed.unfinished == ()


@pytest.mark.parametrize("text", ["", "\n\n", "  \n\t\n"])
def test_blank_and_empty_journals_give_zero_agents_and_zero_count(text):
    parsed = flow_usage.steps_from(text)

    assert parsed.unreadable_journal_lines == 0
    assert (parsed.steps, parsed.step_less, parsed.unfinished, parsed.failed) == ((), (), (), ())


def test_an_all_rejected_journal_gives_zero_agents_and_the_right_count():
    parsed = flow_usage.steps_from("\n".join([JOURNAL_BAD_JSON, "[1]", "null", "", "  "]))

    assert parsed.unreadable_journal_lines == 3
    assert (parsed.steps, parsed.step_less, parsed.unfinished, parsed.failed) == ((), (), (), ())


def test_read_run_and_coverage_carry_the_journal_count_per_run(tmp_path):
    produced = {"step": "development-specs", "status": "ok", "artifact": "x.md"}
    first = _flow_run_fixture(
        tmp_path, [JOURNAL_BAD_JSON, _started("a1"), _result("a1", produced), "[1]"]
    )
    second_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_other")
    (second_dir / "journal.jsonl").write_text("\n".join([_started("b1"), _result("b1", produced)]))
    second = flow_usage.FlowRun(
        wf_id="wf_other", session_id="sid-1", slug="slug-a", run_dir=second_dir
    )

    first_run = flow_usage.read_run(first, flow_usage.Files())
    second_run = flow_usage.read_run(second, flow_usage.Files())

    assert first_run.unreadable_journal_lines == 2
    assert second_run.unreadable_journal_lines == 0
    assert flow_usage.coverage(first_run).unreadable_journal_lines == 2
    assert flow_usage.coverage(second_run).unreadable_journal_lines == 0
    assert flow_usage.coverage(first_run).unreadable_lines == 0


def test_a_journal_line_is_counted_in_no_other_kind(tmp_path):
    run = flow_usage.read_run(
        _flow_run_fixture(tmp_path, [JOURNAL_BAD_JSON, _started("a1")]), flow_usage.Files()
    )

    cov = flow_usage.coverage(run)
    assert (cov.unreadable_journal_lines, cov.unreadable_lines) == (1, 0)


def test_existing_constructors_without_the_new_fields_still_work():
    parsed = flow_usage.StepsFromJournal(steps=(), step_less=(), unfinished=(), failed=())
    reading = flow_usage.RunReading(
        flow_run=flow_usage.FlowRun(wf_id="w", session_id="s", slug="g", run_dir=Path("x")),
        agents=(),
        orphan_agent_files=(),
    )

    assert parsed.unreadable_journal_lines == 0
    assert reading.unreadable_journal_lines == 0
    assert flow_usage.Coverage(1, 2, 3, 4).unreadable_journal_lines == 0
    assert [f.name for f in dataclasses.fields(flow_usage.StepsFromJournal)][-1] == (
        "unreadable_journal_lines"
    )


# ── Meta sidecars the report could not read (CP-417) ──

PRODUCED_STEP = {"step": "development-specs", "status": "ok", "artifact": "x.md"}


def _sidecar_run(tmp_path: Path, sidecar: str | None, *, journal=None, extra_agents=()):
    """A run whose journal names `a1` (and `extra_agents`); `a1`'s sidecar holds `sidecar`, or
    is absent when `None`."""
    lines = journal or [_started("a1"), _result("a1", PRODUCED_STEP)]
    flow_run = _flow_run_fixture(tmp_path, lines, transcripts={"a1": [_usage_line()]})
    if sidecar is not None:
        _write_meta(flow_run.run_dir, "a1", sidecar)
    return flow_usage.read_run(flow_run, flow_usage.Files())


@pytest.mark.parametrize(
    "sidecar", ["not json {", "", "   ", "[1]", '"s"', "5", "null", "true", '{"agentType": ["t"]}']
)
def test_a_sidecar_that_cannot_be_used_is_one_unreadable_meta_file_and_falls_back(
    tmp_path, sidecar
):
    run = _sidecar_run(tmp_path, sidecar)
    (agent,) = run.agents

    assert agent.unreadable_meta_file is True
    assert (agent.agent_type, agent.model_hint) == (None, None)
    assert flow_usage.coverage(run).unreadable_meta_files == 1


@pytest.mark.parametrize("agent_type", [["t"], 5, 1.5, True, False, {"a": 1}])
def test_invalid_agent_type_is_unreadable(tmp_path, agent_type):
    meta = flow_usage.agent_meta(
        _write_sidecar(tmp_path, json.dumps({"agentType": agent_type, "model": "m"})),
        "a1",
        flow_usage.Files(),
    )

    assert meta == flow_usage.AgentMeta(agent_type=None, model=None, unreadable=True)


def _write_sidecar(tmp_path: Path, text: str) -> Path:
    _write_meta(tmp_path, "a1", text)
    return tmp_path


def test_non_object_sidecar_is_unreadable(tmp_path):
    meta = flow_usage.agent_meta(_write_sidecar(tmp_path, "[1]"), "a1", flow_usage.Files())

    assert meta.unreadable is True


def test_a_present_unopenable_sidecar_is_unreadable(tmp_path):
    (tmp_path / "agent-a1.meta.json").mkdir()

    meta = flow_usage.agent_meta(tmp_path, "a1", flow_usage.Files())

    assert meta == flow_usage.AgentMeta(agent_type=None, model=None, unreadable=True)


def test_an_empty_present_sidecar_is_unreadable_but_an_empty_object_is_not(tmp_path):
    empty = flow_usage.agent_meta(_write_sidecar(tmp_path, ""), "a1", flow_usage.Files())
    braces = flow_usage.agent_meta(_write_sidecar(tmp_path, "{}"), "a1", flow_usage.Files())

    assert empty.unreadable is True
    assert braces == flow_usage.AgentMeta(agent_type=None, model=None, unreadable=False)


def test_a_missing_sidecar_is_not_counted_and_keeps_old_metadata_fields(tmp_path):
    run = _sidecar_run(tmp_path, None)
    (agent,) = run.agents

    assert agent.unreadable_meta_file is False
    assert (agent.agent_type, agent.model_hint) == (None, None)
    assert flow_usage.coverage(run).unreadable_meta_files == 0
    meta = flow_usage.agent_meta(tmp_path / "nowhere", "a1", flow_usage.Files())
    assert meta == flow_usage.AgentMeta(agent_type=None, model=None, unreadable=False)


@pytest.mark.parametrize(
    "members, expected",
    [({}, None), ({"agentType": None}, None), ({"agentType": "developer"}, "developer")],
)
def test_absent_null_or_string_agent_type_is_accepted(tmp_path, members, expected):
    meta = flow_usage.agent_meta(
        _write_sidecar(tmp_path, json.dumps(members)), "a1", flow_usage.Files()
    )

    assert meta == flow_usage.AgentMeta(agent_type=expected, model=None, unreadable=False)


@pytest.mark.parametrize("model", [["m"], 5, True, {"a": 1}, None, "haiku"])
def test_an_arbitrary_model_hint_never_classifies_the_file(tmp_path, model):
    meta = flow_usage.agent_meta(
        _write_sidecar(tmp_path, json.dumps({"agentType": "developer", "model": model})),
        "a1",
        flow_usage.Files(),
    )

    assert meta.unreadable is False
    assert meta.agent_type == "developer"


def test_an_unreadable_sidecar_shows_types_none_and_unknown_fallback_like_an_absent_one(tmp_path):
    unreadable = _sidecar_run(tmp_path / "bad", "not json {", journal=[_started("a1")])
    absent = _sidecar_run(tmp_path / "absent", None, journal=[_started("a1")])

    rows_unreadable = flow_usage.aggregate(unreadable)
    rows_absent = flow_usage.aggregate(absent)

    assert [(r.label, r.is_fallback, r.agent_types) for r in rows_unreadable] == [
        ("(unknown)", True, (None,))
    ]
    assert [(r.label, r.is_fallback, r.agent_types, r.spend) for r in rows_unreadable] == [
        (r.label, r.is_fallback, r.agent_types, r.spend) for r in rows_absent
    ]


def test_orphan_sidecars_are_not_read_or_counted(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", PRODUCED_STEP)],
        transcripts={"a1": [_usage_line()], "orphan": [_usage_line()]},
    )
    _write_meta(flow_run.run_dir, "orphan", "not json {")
    (flow_run.run_dir / "agent-lone.meta.json").write_text("[1]")

    class _SpyFiles(flow_usage.Files):
        def __init__(self):
            self.meta_paths = []

        def read_meta_text(self, path):
            self.meta_paths.append(path.name)
            return super().read_meta_text(path)

    files = _SpyFiles()
    run = flow_usage.read_run(flow_run, files)

    assert files.meta_paths == ["agent-a1.meta.json"]
    assert flow_usage.coverage(run).unreadable_meta_files == 0


def test_the_real_files_port_returns_success_missing_and_failure_outcomes_on_temporary_files(
    tmp_path,
):
    files = flow_usage.Files()
    present = tmp_path / "present.json"
    present.write_text("{}")
    (tmp_path / "dir.json").mkdir()

    assert files.read_meta_text(present) == flow_usage.TextRead(text="{}", missing=False)
    assert files.read_meta_text(tmp_path / "absent.json") == flow_usage.TextRead(
        text=None, missing=True
    )
    assert files.read_meta_text(tmp_path / "dir.json") == flow_usage.TextRead(
        text=None, missing=False
    )


# A journal's `agentId` is any string (CP-416), and a few strings can name no file at all: one
# holding a NUL, or an unpaired surrogate the file system cannot encode. No file can sit at such a
# name, so the agent's sidecar is absent (CP-417 counts no absent sidecar) and its transcript is
# absent (CP-418 counts it) — and neither read may stop the report (CP-424).
UNNAMEABLE_AGENT_IDS = ["a\x00b", "\ud800"]


@pytest.mark.parametrize("agent_id", UNNAMEABLE_AGENT_IDS)
def test_the_real_files_port_reads_a_name_no_file_can_have_as_absent(tmp_path, agent_id):
    files = flow_usage.Files()

    assert files.read_meta_text(tmp_path / f"agent-{agent_id}.meta.json") == flow_usage.TextRead(
        text=None, missing=True
    )
    assert files.read_lines(tmp_path / f"agent-{agent_id}.jsonl") is None


def test_an_agent_whose_id_can_name_no_file_counts_an_unreadable_transcript_and_no_meta_file(
    tmp_path,
):
    flow_run = _flow_run_fixture(tmp_path, [_started(a) for a in UNNAMEABLE_AGENT_IDS])

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert sorted(a.agent_id for a in run.agents) == sorted(UNNAMEABLE_AGENT_IDS)
    assert [a.unreadable_agent_transcript for a in run.agents] == [True, True]
    assert [a.unreadable_meta_file for a in run.agents] == [False, False]
    cov = flow_usage.coverage(run)
    assert (cov.unreadable_agent_transcripts, cov.unreadable_meta_files) == (2, 0)
    assert (cov.without_step, cov.started_no_result) == (2, 2)


def test_read_text_and_read_lines_contracts_are_unchanged(tmp_path):
    files = flow_usage.Files()
    present = tmp_path / "p.json"
    present.write_text("a\nb")

    assert files.read_text(present) == "a\nb"
    assert files.read_text(tmp_path / "absent") is None
    assert files.read_lines(present) == ["a", "b"]
    assert files.read_lines(tmp_path / "absent") is None


def test_a_hand_written_files_port_fake_failure_is_counted_at_assembly(tmp_path):
    class _FailingMetaFiles(flow_usage.Files):
        def read_meta_text(self, path):
            return flow_usage.TextRead(text=None, missing=False)

    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", PRODUCED_STEP)],
        transcripts={"a1": [_usage_line()]},
        metas={"a1": {"agentType": "developer"}},
    )

    run = flow_usage.read_run(flow_run, _FailingMetaFiles())

    assert run.agents[0].unreadable_meta_file is True
    assert run.agents[0].agent_type is None
    assert flow_usage.coverage(run).unreadable_meta_files == 1


def test_the_sidecar_count_is_per_agent_once_when_the_journal_names_an_agent_repeatedly(
    tmp_path,
):
    run = _sidecar_run(
        tmp_path,
        "not json {",
        journal=[_started("a1"), _started("a1"), _result("a1", PRODUCED_STEP), _failed("a1")],
    )

    assert [a.unreadable_meta_file for a in run.agents] == [True]
    assert flow_usage.coverage(run).unreadable_meta_files == 1


def test_a_sidecar_is_counted_in_no_other_kind_and_each_run_counts_its_own(tmp_path):
    run = _sidecar_run(tmp_path, "not json {")
    other_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_other")
    (other_dir / "journal.jsonl").write_text(_started("b1"))
    other = flow_usage.read_run(
        flow_usage.FlowRun(wf_id="wf_other", session_id="sid-1", slug="slug-a", run_dir=other_dir),
        flow_usage.Files(),
    )

    cov = flow_usage.coverage(run)
    assert (cov.unreadable_meta_files, cov.unreadable_lines, cov.unreadable_journal_lines) == (
        1,
        0,
        0,
    )
    assert flow_usage.coverage(other).unreadable_meta_files == 0


def test_existing_agent_constructors_without_the_new_meta_fields_still_work():
    assert flow_usage.AgentMeta(agent_type=None, model=None).unreadable is False
    agent = flow_usage.AgentReading(
        agent_id="a1", step=None, role=None, agent_type=None, model_hint=None
    )
    assert agent.unreadable_meta_file is False
    assert flow_usage.Coverage(1, 2, 3, 4).unreadable_meta_files == 0


# ── Agent transcripts the report could not read (CP-418) ──


def _transcript_run(tmp_path: Path, journal: list[str], transcripts=None, metas=None):
    return flow_usage.read_run(
        _flow_run_fixture(tmp_path, journal, transcripts=transcripts or {}, metas=metas),
        flow_usage.Files(),
    )


def test_a_missing_transcript_counts_once_for_attributed_stepless_failed_and_unfinished_agents(
    tmp_path,
):
    run = _transcript_run(
        tmp_path,
        [
            _started("a1"), _result("a1", PRODUCED_STEP),  # attributed
            _started("a2"), _result("a2", "no step"),  # step-less
            _started("a3"), _failed("a3"),  # failed
            _started("a4"),  # unfinished
        ],
    )

    assert [a.unreadable_agent_transcript for a in run.agents] == [True] * 4
    cov = flow_usage.coverage(run)
    assert cov.unreadable_agent_transcripts == 4
    assert (cov.with_step, cov.without_step, cov.started_no_result) == (1, 3, 1)


def test_an_unopenable_transcript_counts_once(tmp_path):
    flow_run = _flow_run_fixture(tmp_path, [_started("a1"), _result("a1", PRODUCED_STEP)])
    (flow_run.run_dir / "agent-a1.jsonl").mkdir()

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert [a.unreadable_agent_transcript for a in run.agents] == [True]
    assert flow_usage.coverage(run).unreadable_agent_transcripts == 1
    assert run.agents[0].spend == {}


def test_an_unreadable_agent_transcript_keeps_its_row_agent_id_and_the_original_four_figures(
    tmp_path,
):
    run = _transcript_run(
        tmp_path,
        [
            _started("a1"),
            _result("a1", PRODUCED_STEP),
            _started("a2"),
            _result("a2", PRODUCED_STEP),
        ],
        {"a2": [_usage_line(input_tokens=4)]},
    )

    (row,) = flow_usage.aggregate(run)
    cov = flow_usage.coverage(run)
    assert row.agent_ids == ("a1", "a2")
    assert row.spend["claude-sonnet-5"].input == 4
    assert (cov.with_step, cov.without_step, cov.started_no_result) == (2, 0, 0)
    assert cov.files_with_no_journal_entry == 0
    assert cov.unreadable_agent_transcripts == 1


def test_an_unreadable_agent_transcript_adds_no_spend_and_no_unreadable_line(tmp_path):
    run = _transcript_run(tmp_path, [_started("a1"), _result("a1", PRODUCED_STEP)])
    (agent,) = run.agents

    assert agent.spend == {}
    assert agent.unreadable_lines == 0
    assert flow_usage.coverage(run).unreadable_lines == 0


def test_an_agent_named_repeatedly_by_the_journal_is_counted_once_and_read_once(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _started("a1"), _result("a1", PRODUCED_STEP), _failed("a1")],
    )
    paths = []

    class _SpyFiles(flow_usage.Files):
        def read_lines(self, path):
            paths.append(path.name)
            return super().read_lines(path)

        def read_meta_text(self, path):
            paths.append(path.name)
            return super().read_meta_text(path)

    run = flow_usage.read_run(flow_run, _SpyFiles())

    assert flow_usage.coverage(run).unreadable_agent_transcripts == 1
    assert paths == ["agent-a1.meta.json", "agent-a1.jsonl"]


@pytest.mark.parametrize(
    "lines, rejected",
    [([], 0), ([""], 0), ([json.dumps({"type": "user"})], 0), ([BAD_LINE, "[1]"], 2)],
)
def test_empty_no_usage_and_all_rejected_readable_transcripts_have_no_file_failure(
    tmp_path, lines, rejected
):
    run = _transcript_run(
        tmp_path, [_started("a1"), _result("a1", PRODUCED_STEP)], {"a1": lines}
    )

    cov = flow_usage.coverage(run)
    assert cov.unreadable_agent_transcripts == 0
    assert cov.unreadable_lines == rejected


def test_agent_spend_consumes_the_shared_reader_once_including_a_read_failure(tmp_path):
    reads = []

    class _FailingFiles(flow_usage.Files):
        def read_lines(self, path):
            reads.append(path)
            return None

    counter = run_usage.UnreadableLines()
    spend = flow_usage.agent_spend(tmp_path, "a1", _FailingFiles(), unreadable_lines=counter)

    assert spend == {}
    assert reads == [tmp_path / "agent-a1.jsonl"]
    assert (counter.unreadable_files, counter.count) == (1, 0)


def test_a_malformed_orphan_transcript_is_not_read_for_these_counts(tmp_path):
    run = _transcript_run(
        tmp_path,
        [_started("a1"), _result("a1", PRODUCED_STEP)],
        {"a1": [_usage_line()], "orphan": [BAD_LINE, "[1]"]},
    )

    cov = flow_usage.coverage(run)
    assert run.orphan_agent_files == ("orphan",)
    assert cov.files_with_no_journal_entry == 1
    assert (cov.unreadable_lines, cov.unreadable_agent_transcripts) == (0, 0)


def test_a_mixed_run_counts_each_kind_only_in_its_own_field_and_run(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            BAD_LINE, "[1]",
            _started("a1"), _started("a1"), _result("a1", PRODUCED_STEP),
            _started("a2"), _result("a2", PRODUCED_STEP),
            _started("a3"), _result("a3", PRODUCED_STEP),
        ],
        transcripts={"a1": [BAD_LINE, _usage_line()], "a2": [_usage_line()], "orphan": [BAD_LINE]},
    )
    _write_meta(flow_run.run_dir, "a1", BAD_LINE)
    _write_meta(flow_run.run_dir, "a2", json.dumps({"agentType": ["t"]}))
    _write_meta(flow_run.run_dir, "orphan", BAD_LINE)
    other_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_other")
    (other_dir / "journal.jsonl").write_text(_started("b1"))
    other_run = flow_usage.FlowRun(
        wf_id="wf_other", session_id="sid-1", slug="slug-a", run_dir=other_dir
    )

    run = flow_usage.read_run(flow_run, flow_usage.Files())
    other = flow_usage.read_run(other_run, flow_usage.Files())

    assert flow_usage.coverage(run) == flow_usage.Coverage(
        with_step=3,
        without_step=0,
        started_no_result=0,
        files_with_no_journal_entry=1,
        unreadable_lines=1,
        unreadable_journal_lines=2,
        unreadable_meta_files=2,
        unreadable_agent_transcripts=1,
    )
    assert flow_usage.coverage(other) == flow_usage.Coverage(
        with_step=0,
        without_step=1,
        started_no_result=1,
        files_with_no_journal_entry=0,
        unreadable_agent_transcripts=1,
    )


def test_existing_agent_constructors_without_the_transcript_field_still_work():
    agent = flow_usage.AgentReading(
        agent_id="a1", step=None, role=None, agent_type=None, model_hint=None
    )
    assert agent.unreadable_agent_transcript is False
    assert flow_usage.Coverage(1, 2, 3, 4).unreadable_agent_transcripts == 0


# ── What the Coverage line says about each kind (CP-420, CP-422; words from change_text.md) ──

CLEAN_COVERAGE = (
    "Coverage: 2 with a step / 1 without / 1 started with no result / "
    "3 agent files with no journal entry at all"
)


def _reading(*, lines=0, journal=0, meta=0, transcripts=0) -> flow_usage.RunReading:
    """A run whose figures are fixed (2 with a step, 1 without, 1 started with no result, 3
    orphans) and whose four unreadable counts are exactly the ones given."""

    def agent(agent_id, **members):
        return flow_usage.AgentReading(
            agent_id=agent_id, step=None, role=None, agent_type=None, model_hint=None, **members
        )

    def stepped(agent_id, **members):
        return flow_usage.AgentReading(
            agent_id=agent_id,
            step="development-specs",
            role=flow_usage.Role.PRODUCE,
            agent_type=None,
            model_hint=None,
            **members,
        )

    fixed = [stepped("s1"), stepped("s2"), agent("u1", unfinished=True)]
    # The counts ride on step-less agents beyond the three fixed ones, one count per agent.
    extras = [agent(f"x{n}") for n in range(max(lines, meta, transcripts))]
    for n in range(lines):
        extras[n] = dataclasses.replace(extras[n], unreadable_lines=1)
    for n in range(meta):
        extras[n] = dataclasses.replace(extras[n], unreadable_meta_file=True)
    for n in range(transcripts):
        extras[n] = dataclasses.replace(extras[n], unreadable_agent_transcript=True)
    flow_run = flow_usage.FlowRun(wf_id="wf_x", session_id="sid", slug="slug", run_dir=Path("x"))
    return flow_usage.RunReading(
        flow_run=flow_run,
        agents=tuple(fixed + extras),
        orphan_agent_files=("o1", "o2", "o3"),
        unreadable_journal_lines=journal,
    )


def _coverage_text(reading: flow_usage.RunReading) -> str:
    (line,) = [
        ln.strip()
        for ln in flow_usage.render([reading]).splitlines()
        if ln.strip().startswith("Coverage:")
    ]
    return line


def _figures(reading: flow_usage.RunReading) -> str:
    """The four figures' words as `c227f26` prints them, for this reading's own figures."""
    cov = flow_usage.coverage(reading)
    return (
        f"Coverage: {cov.with_step} with a step / {cov.without_step} without / "
        f"{cov.started_no_result} started with no result / "
        f"{cov.files_with_no_journal_entry} agent files with no journal entry at all"
    )


def _coverage_members(reading: flow_usage.RunReading) -> dict:
    return json.loads(flow_usage.render_json([reading]))["runs"][0]["coverage"]


def test_the_helper_reading_has_the_figures_it_promises():
    cov = flow_usage.coverage(_reading(lines=2, journal=3, meta=1, transcripts=2))

    assert (cov.with_step, cov.without_step, cov.started_no_result) == (2, 1 + 2, 1)
    assert cov.files_with_no_journal_entry == 3
    assert (
        cov.unreadable_lines,
        cov.unreadable_journal_lines,
        cov.unreadable_meta_files,
        cov.unreadable_agent_transcripts,
    ) == (2, 3, 1, 2)


def test_a_clean_run_coverage_text_and_json_are_unchanged():
    reading = _reading()

    assert _coverage_text(reading) == CLEAN_COVERAGE
    assert _coverage_members(reading) == {
        "with_step": 2,
        "without_step": 1,
        "started_no_result": 1,
        "files_with_no_journal_entry": 3,
    }


@pytest.mark.parametrize(
    "counts, clause",
    [
        ({"journal": 1}, "1 unreadable journal line skipped"),
        ({"meta": 1}, "1 unreadable meta file, so its agent's type shows as (none)"),
        (
            {"transcripts": 1},
            "1 unreadable agent transcript, so no spend is read for its agent",
        ),
        (
            {"lines": 1},
            "1 unreadable line skipped in this workflow run's agent transcripts",
        ),
    ],
)
def test_each_new_count_at_one_names_its_kind(counts, clause):
    reading = _reading(**counts)

    assert _coverage_text(reading) == f"{_figures(reading)} — {clause}"


@pytest.mark.parametrize(
    "counts, clause",
    [
        ({"journal": 2}, "2 unreadable journal lines skipped"),
        ({"journal": 5}, "5 unreadable journal lines skipped"),
        ({"meta": 2}, "2 unreadable meta files, so their agents' types show as (none)"),
        (
            {"transcripts": 3},
            "3 unreadable agent transcripts, so no spend is read for their agents",
        ),
        (
            {"lines": 2},
            "2 unreadable lines skipped in this workflow run's agent transcripts",
        ),
    ],
)
def test_each_new_count_above_one_carries_the_count_and_the_kind_noun(counts, clause):
    reading = _reading(**counts)

    assert _coverage_text(reading) == f"{_figures(reading)} — {clause}"


def test_the_transcript_line_clause_is_worded_as_before():
    assert flow_usage.UNREADABLE_LINES_ONE == (
        "1 unreadable line skipped in this workflow run's agent transcripts"
    )
    assert flow_usage.UNREADABLE_LINES_MANY == (
        "{line_count} unreadable lines skipped in this workflow run's agent transcripts"
    )


def test_mixed_counts_appear_in_the_fixed_kind_order_after_the_existing_figures():
    reading = _reading(lines=1, journal=2, meta=1, transcripts=1)

    assert _coverage_text(reading) == (
        f"{_figures(reading)}"
        " — 1 unreadable line skipped in this workflow run's agent transcripts"
        " — 2 unreadable journal lines skipped"
        " — 1 unreadable meta file, so its agent's type shows as (none)"
        " — 1 unreadable agent transcript, so no spend is read for its agent"
    )


def test_two_counts_read_as_the_spec_example():
    reading = _reading(journal=2, meta=1)

    assert _coverage_text(reading) == (
        f"{_figures(reading)} — 2 unreadable journal lines skipped"
        " — 1 unreadable meta file, so its agent's type shows as (none)"
    )


def test_a_zero_count_adds_no_clause_and_no_json_member():
    reading = _reading(journal=1)

    assert _coverage_text(reading).count(" — ") == 1
    assert "meta" not in _coverage_text(reading) and "transcript" not in _coverage_text(reading)
    members = _coverage_members(reading)
    assert members["unreadable_journal_lines"] == 1
    assert not {"unreadable_lines", "unreadable_meta_files", "unreadable_agent_transcripts"} & set(
        members
    )


def test_json_members_are_present_only_when_positive_and_nothing_else_is_added():
    members = _coverage_members(_reading(lines=2, journal=3, meta=1, transcripts=4))

    assert members == {
        "with_step": 2,
        "without_step": 1 + 4,
        "started_no_result": 1,
        "files_with_no_journal_entry": 3,
        "unreadable_lines": 2,
        "unreadable_journal_lines": 3,
        "unreadable_meta_files": 1,
        "unreadable_agent_transcripts": 4,
    }


def test_text_and_json_come_from_the_same_coverage_value():
    reading = _reading(lines=1, journal=2, meta=3, transcripts=1)
    cov = flow_usage.coverage(reading)
    members = _coverage_members(reading)

    assert members["unreadable_lines"] == cov.unreadable_lines
    assert members["unreadable_journal_lines"] == cov.unreadable_journal_lines
    assert members["unreadable_meta_files"] == cov.unreadable_meta_files
    assert members["unreadable_agent_transcripts"] == cov.unreadable_agent_transcripts
    assert "3 unreadable meta files" in _coverage_text(reading)


def test_the_new_counts_never_enter_a_row_or_the_json_outside_coverage():
    clean, dirty = _reading(), _reading(journal=2, meta=1, transcripts=1)
    clean_json = json.loads(flow_usage.render_json([clean]))
    dirty_json = json.loads(flow_usage.render_json([dirty]))

    assert set(dirty_json) == set(clean_json)
    assert set(dirty_json["runs"][0]) == set(clean_json["runs"][0])


# ── A journal that is found and cannot be opened ends the reading of its workflow run ──


class _FailingPath:
    """A path whose one read raises `error` — a failure kind a real file cannot be made to
    produce without file modes, injected instead."""

    def __init__(self, error: OSError):
        self._error = error

    def read_text(self, **_kwargs):
        raise self._error


def _directory_in_place_of_journal(flow_run: flow_usage.FlowRun) -> None:
    journal = flow_run.run_dir / "journal.jsonl"
    journal.unlink()
    journal.mkdir()


def _run_with_agent_files(tmp_path: Path) -> flow_usage.FlowRun:
    return _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", PRODUCED_STEP)],
        transcripts={"a1": [_usage_line()], "orphan": [_usage_line()]},
        metas={"a1": {"agentType": "developer"}},
    )


class _RecordingFiles(flow_usage.Files):
    """The real port, recording every agent-file reach so a test can assert there was none."""

    def __init__(self):
        self.agent_files_reached = []

    def agent_ids_in(self, run_dir):
        self.agent_files_reached.append("agent_ids_in")
        return super().agent_ids_in(run_dir)

    def read_meta_text(self, path):
        self.agent_files_reached.append(f"read_meta_text {path.name}")
        return super().read_meta_text(path)

    def read_lines(self, path):
        self.agent_files_reached.append(f"read_lines {path.name}")
        return super().read_lines(path)


def test_read_journal_text_outcomes(tmp_path):
    files = flow_usage.Files()
    populated = tmp_path / "populated.jsonl"
    populated.write_text("a\nb\n")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    regular = tmp_path / "regular"
    regular.write_text("x")
    (tmp_path / "dir.jsonl").mkdir()

    assert files.read_journal_text(populated) == flow_usage.TextRead(text="a\nb\n", missing=False)
    assert files.read_journal_text(empty) == flow_usage.TextRead(text="", missing=False)
    assert files.read_journal_text(tmp_path / "absent.jsonl") == flow_usage.TextRead(
        text=None, missing=True
    )
    assert files.read_journal_text(regular / "x.jsonl") == flow_usage.TextRead(
        text=None, missing=True
    )
    assert files.read_journal_text(tmp_path / "dir.jsonl") == flow_usage.TextRead(
        text=None, missing=False
    )
    assert files.read_journal_text(_FailingPath(PermissionError("denied"))) == (
        flow_usage.TextRead(text=None, missing=False)
    )


def test_failed_journal_reads_no_agent_file(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    _directory_in_place_of_journal(flow_run)
    files = _RecordingFiles()

    flow_usage.read_run(flow_run, files)

    assert files.agent_files_reached == []


def test_failed_journal_reading_has_identity_and_nothing_else(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    _directory_in_place_of_journal(flow_run)

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert run.flow_run == flow_run
    assert run.agents == ()
    assert run.orphan_agent_files == ()
    assert run.unreadable_journal_lines == 0
    assert run.unreadable_journal_files == 1


def test_coverage_of_a_failed_journal_has_every_old_figure_zero(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    _directory_in_place_of_journal(flow_run)
    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert flow_usage.coverage(run) == flow_usage.Coverage(
        with_step=0,
        without_step=0,
        started_no_result=0,
        files_with_no_journal_entry=0,
        unreadable_lines=0,
        unreadable_journal_lines=0,
        unreadable_meta_files=0,
        unreadable_agent_transcripts=0,
        unreadable_journal_files=1,
    )
    assert flow_usage.aggregate(run) == ()


def test_absent_journal_supplied_directly_keeps_the_old_orphan_path(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    (flow_run.run_dir / "journal.jsonl").unlink()

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert sorted(run.orphan_agent_files) == ["a1", "orphan"]
    assert run.agents == ()
    assert run.unreadable_journal_files == 0
    assert flow_usage.coverage(run).files_with_no_journal_entry == 2


def test_journal_removed_after_discovery_keeps_the_old_path(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    files = flow_usage.Files()
    (discovered,) = files.find_workflow_dirs(tmp_path)
    (discovered / "journal.jsonl").unlink()

    run = flow_usage.read_run(flow_run, files)

    assert sorted(run.orphan_agent_files) == ["a1", "orphan"]
    assert run.unreadable_journal_files == 0


def test_empty_readable_journal_still_discovers_orphan_files(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    (flow_run.run_dir / "journal.jsonl").write_text("")

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert sorted(run.orphan_agent_files) == ["a1", "orphan"]
    assert run.unreadable_journal_files == 0


def test_readable_journal_with_rejected_lines_still_counts_them(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    journal = flow_run.run_dir / "journal.jsonl"
    journal.write_text(journal.read_text() + "\nnot json {\n")

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    assert run.unreadable_journal_lines == 1
    assert run.unreadable_journal_files == 0
    assert [a.agent_id for a in run.agents] == ["a1"]


def test_late_journal_read_failure_names_no_agent(tmp_path):
    class _LateFailureFiles(flow_usage.Files):
        """Holds the journal's text and then fails its read whole, as a read that breaks after
        partial progress does: no part of the text reaches the reading."""

        def read_journal_text(self, path):
            return flow_usage.TextRead(text=None, missing=False)

    flow_run = _run_with_agent_files(tmp_path)

    run = flow_usage.read_run(flow_run, _LateFailureFiles())

    assert run.agents == ()
    assert run.unreadable_journal_lines == 0
    assert run.unreadable_journal_files == 1


def test_legacy_construction_of_run_reading_and_coverage_defaults_the_new_count_to_zero(tmp_path):
    flow_run = flow_usage.FlowRun(
        wf_id="wf_test", session_id="sid-1", slug="slug-a", run_dir=tmp_path
    )

    assert flow_usage.RunReading(flow_run, (), ()).unreadable_journal_files == 0
    assert flow_usage.Coverage(1, 2, 3, 4).unreadable_journal_files == 0
    assert [f.name for f in dataclasses.fields(flow_usage.Coverage)][-1] == (
        "unreadable_journal_files"
    )
    assert [f.name for f in dataclasses.fields(flow_usage.RunReading)][-1] == (
        "unreadable_journal_files"
    )


# ── `steps` text and `--json` carry the journal-file count ──

JOURNAL_FILE_CLAUSE = (
    "1 unreadable journal file, so no agent file in this workflow run is read or counted"
)


def _with_journal_file(reading: flow_usage.RunReading) -> flow_usage.RunReading:
    return dataclasses.replace(reading, unreadable_journal_files=1)


def test_zero_journal_file_count_leaves_text_and_json_unchanged():
    reading = dataclasses.replace(_reading(), unreadable_journal_files=0)

    assert _coverage_text(reading) == CLEAN_COVERAGE
    assert set(_coverage_members(reading)) == {
        "with_step",
        "without_step",
        "started_no_result",
        "files_with_no_journal_entry",
    }


def test_the_journal_file_clause_is_worded_as_change_text_gives_it():
    assert flow_usage.UNREADABLE_JOURNAL_FILES_ONE == JOURNAL_FILE_CLAUSE


def test_one_journal_file_adds_one_clause_after_the_existing_kinds():
    alone = _with_journal_file(_reading())
    assert _coverage_text(alone) == f"{_figures(alone)} — {JOURNAL_FILE_CLAUSE}"

    mixed = _with_journal_file(_reading(lines=1, journal=2, meta=1, transcripts=1))
    assert _coverage_text(mixed) == (
        f"{_figures(mixed)}"
        " — 1 unreadable line skipped in this workflow run's agent transcripts"
        " — 2 unreadable journal lines skipped"
        " — 1 unreadable meta file, so its agent's type shows as (none)"
        " — 1 unreadable agent transcript, so no spend is read for its agent"
        f" — {JOURNAL_FILE_CLAUSE}"
    )
    assert "1 unreadable journal file" in _coverage_text(mixed)


def test_json_adds_only_the_positive_member():
    clean = _coverage_members(_reading())
    failed = _coverage_members(_with_journal_file(_reading()))

    assert failed == {**clean, "unreadable_journal_files": 1}


def _two_runs(tmp_path):
    readable = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_readable")
    (readable / "journal.jsonl").write_text(
        "\n".join([_started("a1"), _result("a1", PRODUCED_STEP)])
    )
    _write_agent_transcript(readable, "a1", [_usage_line(input_tokens=7)])
    failed = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_failed")
    (failed / "journal.jsonl").unlink()
    (failed / "journal.jsonl").mkdir()
    _write_agent_transcript(failed, "a9", [_usage_line()])
    return readable, failed


def _run_section(text: str, wf_id: str) -> list[str]:
    """The lines of `render` text that belong to one workflow run: its header and the indented
    lines under it. The last run is followed by the no-cost line with no blank between, so the
    section ends at the first line that is not indented."""
    lines = text.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith(f"{wf_id} "))
    end = next(
        (n for n in range(start + 1, len(lines)) if not lines[n].startswith("  ")), len(lines)
    )
    return lines[start:end]


def _json_run(rendered: str, wf_id: str) -> dict:
    (run,) = [r for r in json.loads(rendered)["runs"] if r["wf_id"] == wf_id]
    return run


def test_neighbouring_readable_workflow_is_read_independently(tmp_path):
    # The spec's example: a readable run beside the failed one has "its `Coverage:` line and
    # `coverage` object ... as they are without the other run". The failed run comes first, so a
    # terminal reading that leaked into the next run would show here.
    readable_dir, failed_dir = _two_runs(tmp_path)
    files = flow_usage.Files()

    def reading(run_dir, wf_id, session_id):
        flow_run = flow_usage.FlowRun(wf_id, session_id, "slug-a", run_dir)
        return flow_usage.read_run(flow_run, files)

    neighbour = reading(readable_dir, "wf_readable", "sid-1")
    failed = reading(failed_dir, "wf_failed", "sid-2")

    both = flow_usage.render([failed, neighbour])
    alone = flow_usage.render([neighbour])
    neighbour_section = _run_section(alone, "wf_readable")
    assert len(neighbour_section) == 5  # header, Coverage:, its one row, its span, its spend
    assert _run_section(both, "wf_readable") == neighbour_section

    both_json = flow_usage.render_json([failed, neighbour])
    alone_json = flow_usage.render_json([neighbour])
    assert _json_run(both_json, "wf_readable") == _json_run(alone_json, "wf_readable")
    assert "unreadable_journal_files" not in _json_run(both_json, "wf_readable")["coverage"]
    assert _json_run(both_json, "wf_failed")["coverage"]["unreadable_journal_files"] == 1

    failed_section = _run_section(both, "wf_failed")
    assert failed_section[1].endswith(JOURNAL_FILE_CLAUSE)
    assert failed_section[2] == "  No agents read for this run."
    assert both.count(JOURNAL_FILE_CLAUSE) == 1


def test_wf_selects_the_failed_journal_workflow(tmp_path, capsys):
    _two_runs(tmp_path)

    flow_usage.main(["--projects-root", str(tmp_path)])
    everything = capsys.readouterr().out
    flow_usage.main(["--projects-root", str(tmp_path), "--wf", "wf_failed"])
    selected = capsys.readouterr().out

    (failed_line,) = [ln for ln in selected.splitlines() if ln.strip().startswith("Coverage:")]
    assert failed_line in everything.splitlines()
    assert failed_line.endswith(JOURNAL_FILE_CLAUSE)
    assert "wf_readable" not in selected


# ── Task 3 — discovery says where it could not look, and `read_steps` carries it ──


class _CountingFiles(flow_usage.Files):
    """The real port, counting how often each way of finding workflow directories is used."""

    def __init__(self):
        self.discoveries = 0
        self.legacy_finds = 0

    def discover_workflows(self, projects_root):
        self.discoveries += 1
        return super().discover_workflows(projects_root)

    def find_workflow_dirs(self, projects_root):
        self.legacy_finds += 1
        return super().find_workflow_dirs(projects_root)


def _reader(root, files=None):
    return flow_usage.Reader(files=files or flow_usage.Files(), projects_root=root)


def test_read_steps_returns_workflow_readings_and_failed_places_from_one_discovery(tmp_path, seal):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    (tmp_path / "slug-locked").mkdir()
    seal(tmp_path / "slug-locked")
    files = _CountingFiles()

    reading = flow_usage.read_steps(_reader(tmp_path, files))

    assert [r.flow_run.wf_id for r in reading.runs] == ["wf_one"]
    assert reading.unreadable_directories == (
        run_usage.DirectoryFailure(directory=tmp_path / "slug-locked"),
    )
    assert (files.discoveries, files.legacy_finds) == (1, 0)


def test_read_steps_on_an_empty_population_still_reports_an_unreadable_root(tmp_path, seal):
    root = tmp_path / "root"
    root.mkdir()
    seal(root)

    reading = flow_usage.read_steps(_reader(root))

    assert reading.runs == ()
    assert reading.unreadable_directories == (run_usage.DirectoryFailure(directory=root),)


def test_read_steps_on_an_absent_root_reports_no_failure_and_no_runs(tmp_path):
    under_a_file = tmp_path / "plain"
    under_a_file.write_text("")

    for root in (tmp_path / "absent", under_a_file / "below"):
        reading = flow_usage.read_steps(_reader(root))

        assert reading == flow_usage.StepsReading(runs=(), unreadable_directories=())


def test_non_directory_entries_directly_under_the_root_are_not_places(tmp_path, seal):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    (tmp_path / "notes.txt").write_text("")
    locked_file = tmp_path / "locked.jsonl"
    locked_file.write_text("")
    seal(locked_file)

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [r.flow_run.wf_id for r in reading.runs] == ["wf_one"]
    assert reading.unreadable_directories == ()


def test_unreadable_root_reports_only_the_root(tmp_path, seal):
    root = tmp_path / "root"
    _make_workflow_run(root, "slug-a", "sid-1", "wf_one")
    seal(root)

    reading = flow_usage.read_steps(_reader(root))

    assert reading.runs == ()
    assert [failure.directory for failure in reading.unreadable_directories] == [root]


def test_failed_project_directories_are_sorted_by_name_independent_of_creation_order(
    tmp_path, seal
):
    for name in ("slug-c", "slug-a", "slug-b"):
        (tmp_path / name).mkdir()
        seal(tmp_path / name)

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [failure.directory.name for failure in reading.unreadable_directories] == [
        "slug-a",
        "slug-b",
        "slug-c",
    ]


def test_root_that_can_be_listed_but_not_entered_reports_only_the_root(tmp_path, list_only):
    # CP-442: its names can be read, but not which are project directories nor what they hold.
    root = tmp_path / "root"
    _make_workflow_run(root, "slug-a", "sid-1", "wf_one")
    list_only(root)

    reading = flow_usage.read_steps(_reader(root))

    assert reading.runs == ()
    assert reading.unreadable_directories == (run_usage.DirectoryFailure(directory=root),)
    assert "No workflow runs read." not in flow_usage.render(
        list(reading.runs), unreadable_directories=reading.unreadable_directories
    )


def test_project_dir_that_can_be_listed_but_not_entered_is_named_beside_a_readable_one(
    tmp_path, list_only
):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    _make_workflow_run(tmp_path, "slug-b", "sid-2", "wf_two")
    list_only(tmp_path / "slug-b")

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [r.flow_run.wf_id for r in reading.runs] == ["wf_one"]
    assert reading.unreadable_directories == (
        run_usage.DirectoryFailure(directory=tmp_path / "slug-b"),
    )


DEPTHS = ("session", "subagents", "workflows", "workflow-run")


def _depth_paths(run_dir):
    return {
        "session": run_dir.parent.parent.parent,
        "subagents": run_dir.parent.parent,
        "workflows": run_dir.parent,
        "workflow-run": run_dir,
    }


def _failed_places(reading):
    return [(f.directory, f.reason) for f in reading.unreadable_directories]


@pytest.mark.parametrize("below", DEPTHS)
def test_read_steps_names_a_locked_directory_at_each_of_four_depths(below, tmp_path, seal):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    other = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_two")
    locked = _depth_paths(run_dir)[below]
    seal(locked)
    legacy = flow_usage.flow_runs(tmp_path, flow_usage.Files())

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert reading.unreadable_directories == (run_usage.DirectoryFailure(directory=locked),)
    assert reading.unreadable_directories[0].reason == run_usage.DIRECTORY_FAILURE_REASON
    assert [r.flow_run for r in reading.runs] == list(legacy)
    assert [r.flow_run.run_dir for r in reading.runs] == [other]


def test_a_place_inside_a_locked_place_is_not_named(tmp_path, seal):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    paths = _depth_paths(run_dir)
    seal(paths["subagents"])
    seal(paths["session"])

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert _failed_places(reading) == [(paths["session"], run_usage.DIRECTORY_FAILURE_REASON)]
    assert reading.runs == ()


@pytest.mark.parametrize("below", ["workflows", "workflow-run"])
def test_a_failed_place_contributes_no_partial_run(below, tmp_path, seal):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_sibling")
    kept = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_kept")
    locked = _depth_paths(run_dir)[below]
    seal(locked)

    reading = flow_usage.read_steps(_reader(tmp_path))

    expected_runs = {"workflows": ["wf_kept"], "workflow-run": ["wf_kept", "wf_sibling"]}[below]
    assert sorted(r.flow_run.wf_id for r in reading.runs) == expected_runs
    assert kept in [r.flow_run.run_dir for r in reading.runs]
    assert [place for place, _ in _failed_places(reading)] == [locked]


def test_a_failed_observation_in_a_workflows_listing_contributes_no_run_from_it(
    tmp_path, with_mode
):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_two")
    _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_elsewhere")
    with_mode(run_dir.parent, 0o444)

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [r.flow_run.wf_id for r in reading.runs] == ["wf_elsewhere"]
    assert [place for place, _ in _failed_places(reading)] == [run_dir.parent]


def test_execute_only_directories_follow_the_look_in_rule(tmp_path, with_mode):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    paths = _depth_paths(run_dir)
    for depth in ("session", "subagents", "workflow-run"):
        with_mode(paths[depth], 0o111)

    looked_in = flow_usage.read_steps(_reader(tmp_path))

    assert looked_in.unreadable_directories == ()
    assert [r.flow_run.run_dir for r in looked_in.runs] == [run_dir]

    with_mode(paths["workflows"], 0o111)

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [place for place, _ in _failed_places(reading)] == [paths["workflows"]]
    assert reading.runs == ()


@pytest.mark.parametrize("below", DEPTHS)
def test_directories_that_can_be_listed_but_not_entered_fail_at_every_deeper_depth(
    below, tmp_path, list_only
):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    locked = list_only(_depth_paths(run_dir)[below])

    reading = flow_usage.read_steps(_reader(tmp_path))

    assert [place for place, _ in _failed_places(reading)] == [locked]
    assert reading.runs == ()


def test_absent_and_non_directory_entries_are_no_failure(tmp_path):
    files = flow_usage.Files()
    (tmp_path / "plain").write_text("")
    for root in (tmp_path / "absent", tmp_path / "plain", tmp_path / "plain" / "below"):
        assert files.discover_workflows(root) == flow_usage.WorkflowDiscovery((), ())

    root = tmp_path / "projects"
    (root / "empty-project").mkdir(parents=True)
    (root / "stray-file").write_text("")
    (root / "p" / "no-subagents").mkdir(parents=True)
    (root / "p" / "file-subagents").mkdir()
    (root / "p" / "file-subagents" / "subagents").write_text("")
    (root / "p" / "no-workflows" / "subagents").mkdir(parents=True)
    (root / "p" / "file-workflows" / "subagents").mkdir(parents=True)
    (root / "p" / "file-workflows" / "subagents" / "workflows").write_text("")
    (root / "p" / "empty-workflows" / "subagents" / "workflows").mkdir(parents=True)
    (root / "p" / "stray-session-file").write_text("")
    runs = root / "p" / "has-runs" / "subagents" / "workflows"
    (runs / "no-journal").mkdir(parents=True)
    (runs / "stray-run-file").write_text("")

    discovery = files.discover_workflows(root)

    assert discovery == flow_usage.WorkflowDiscovery((), ())


def _make_bare_run(root: Path) -> Path:
    """A second workflow run directory beside `wf_one`, holding nothing yet."""
    run_dir = root / "slug-a" / "sid-1" / "subagents" / "workflows" / "wf_two"
    run_dir.mkdir(parents=True)
    return run_dir


def _journal_link_to_nowhere(root):
    run_dir = _make_bare_run(root)
    (run_dir / "journal.jsonl").symlink_to(run_dir / "nowhere")


def _journal_link_to_itself(root):
    run_dir = _make_bare_run(root)
    (run_dir / "journal.jsonl").symlink_to(run_dir / "journal.jsonl")


def _journal_directory(root):
    (_make_bare_run(root) / "journal.jsonl").mkdir()


def _linked_session(root):
    (root / "slug-a" / "sid-linked").symlink_to(root / "slug-a" / "sid-1")


def _linked_run_dir(root):
    workflows = root / "slug-a" / "sid-1" / "subagents" / "workflows"
    (workflows / "wf_linked").symlink_to(workflows / "wf_one")


def _hidden_names(root):
    _make_workflow_run(root, ".hidden-project", "sid-h", "wf_h1")
    _make_workflow_run(root, "slug-a", ".hidden-session", "wf_h2")
    _make_workflow_run(root, "slug-a", "sid-1", ".wf_hidden")


def _links_that_lead_nowhere(root):
    project = root / "slug-a"
    (project / "sid-gone").symlink_to(root / "nowhere")
    (project / "sid-self").symlink_to(project / "sid-self")
    (project / "sid-dangling").mkdir()
    (project / "sid-dangling" / "subagents").symlink_to(root / "nowhere")
    (project / "sid-looping" / "subagents").mkdir(parents=True)
    looping = project / "sid-looping" / "subagents" / "workflows"
    looping.symlink_to(looping)
    (project / "sid-1" / "subagents" / "workflows" / "wf_gone").symlink_to(root / "nowhere")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_journal_link_to_nowhere, id="journal-link-to-nowhere"),
        pytest.param(_journal_link_to_itself, id="journal-link-to-itself"),
        pytest.param(_journal_directory, id="journal-directory"),
        pytest.param(_linked_session, id="linked-session"),
        pytest.param(_linked_run_dir, id="linked-run-dir"),
        pytest.param(_hidden_names, id="hidden-names"),
        pytest.param(_links_that_lead_nowhere, id="links-that-lead-nowhere"),
    ],
)
def test_discovery_finds_the_baseline_runs_where_every_place_is_looked_in(build, tmp_path):
    # CP-462: where every place is looked in or absent, `steps` reports what `e671a05` did, so
    # discovery finds the runs `find_workflow_dirs` (CP-461, unchanged since `e671a05`) finds.
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    build(tmp_path)
    files = flow_usage.Files()

    discovery = files.discover_workflows(tmp_path)
    reading = flow_usage.read_steps(_reader(tmp_path))

    assert discovery.failed_places == ()
    assert sorted(discovery.run_dirs) == sorted(files.find_workflow_dirs(tmp_path))
    assert [r.flow_run for r in reading.runs] == list(flow_usage.flow_runs(tmp_path, files))
    assert reading.unreadable_directories == ()


def test_failures_are_ordered_by_directory_name_component_from_the_root(tmp_path, seal):
    paths = []
    for project, session in (("p2", "aa"), ("p1", "zz"), ("a-b", "s"), ("a", "s")):
        run_dir = _make_workflow_run(tmp_path, project, session, "wf_x")
        paths.append(_depth_paths(run_dir)["session"])
    for path in paths:
        seal(path)

    discovery = flow_usage.Files().discover_workflows(tmp_path)

    assert discovery.failed_places == (
        tmp_path / "a" / "s",
        tmp_path / "a-b" / "s",
        tmp_path / "p1" / "zz",
        tmp_path / "p2" / "aa",
    )


def test_a_failed_root_is_the_only_failure(tmp_path, seal):
    root = tmp_path / "root"
    _make_workflow_run(root, "slug-a", "sid-1", "wf_one")
    seal(root)

    assert flow_usage.Files().discover_workflows(root).failed_places == (root,)


def test_deep_failures_keep_the_supplied_root_spelling(tmp_path, monkeypatch, seal):
    home = tmp_path / "home"
    default_root = home / ".claude" / "projects"
    seal(_depth_paths(_make_workflow_run(default_root, "slug-a", "sid-1", "wf_one"))["subagents"])
    monkeypatch.setenv("HOME", str(home))

    default = flow_usage.read_steps(flow_usage.build_reader(None))

    assert [str(f.directory) for f in default.unreadable_directories] == [
        str(default_root / "slug-a" / "sid-1" / "subagents")
    ]

    seal(_depth_paths(_make_workflow_run(tmp_path / "rel", "slug-b", "sid-2", "wf_two"))["session"])
    monkeypatch.chdir(tmp_path)

    relative = flow_usage.read_steps(flow_usage.build_reader(Path("rel")))

    assert [str(f.directory) for f in relative.unreadable_directories] == ["rel/slug-b/sid-2"]


@pytest.mark.parametrize(
    ("below", "mode", "found"),
    [
        ("session", 0o111, True),
        ("subagents", 0o111, True),
        ("workflow-run", 0o111, True),
        ("workflows", 0o111, False),
        ("session", 0o444, False),
        ("subagents", 0o444, False),
        ("workflows", 0o444, False),
        ("workflow-run", 0o444, False),
    ],
)
def test_legacy_discovery_returns_what_it_returned_at_baseline_under_each_mode(
    below, mode, found, tmp_path, with_mode
):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    with_mode(_depth_paths(run_dir)[below], mode)
    files = flow_usage.Files()

    expected = [run_dir] if found else []
    assert files.find_workflow_dirs(tmp_path) == expected
    assert [r.run_dir for r in flow_usage.flow_runs(tmp_path, files)] == expected


def test_legacy_discovery_returns_nothing_for_an_absent_or_non_directory_root(tmp_path):
    (tmp_path / "plain").write_text("")
    files = flow_usage.Files()

    for root in (tmp_path / "absent", tmp_path / "plain", tmp_path / "plain" / "below"):
        assert files.find_workflow_dirs(root) == []
        assert flow_usage.flow_runs(root, files) == ()


def test_discovery_does_not_run_the_legacy_glob_then_reinspect(tmp_path):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")

    class _NoLegacyFiles(flow_usage.Files):
        def find_workflow_dirs(self, projects_root):
            raise AssertionError("the legacy glob ran")

    reading = flow_usage.read_steps(_reader(tmp_path, _NoLegacyFiles()))

    assert [r.flow_run.wf_id for r in reading.runs] == ["wf_one"]


def test_flow_runs_and_find_workflow_dirs_keep_their_baseline_returns(tmp_path, seal):
    good = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_good")
    (tmp_path / "slug-locked").mkdir()
    seal(tmp_path / "slug-locked")
    files = flow_usage.Files()

    assert files.find_workflow_dirs(tmp_path) == [good]
    assert [r.run_dir for r in flow_usage.flow_runs(tmp_path, files)] == [good]
    discovery = files.discover_workflows(tmp_path)
    assert list(discovery.run_dirs) == [good]
    assert discovery.failed_places == (tmp_path / "slug-locked",)


@pytest.mark.parametrize("below", DEPTHS)
def test_legacy_discovery_returns_what_it_returned_at_baseline_below_a_locked_depth(
    below, tmp_path, seal
):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    other = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_two")
    seal(_depth_paths(run_dir)[below])
    files = flow_usage.Files()

    assert files.find_workflow_dirs(tmp_path) == [other]
    assert [r.run_dir for r in flow_usage.flow_runs(tmp_path, files)] == [other]


def test_default_and_relative_root_paths_keep_their_lexical_spelling_in_failures(
    tmp_path, monkeypatch, seal
):
    home = tmp_path / "home"
    default_root = home / ".claude" / "projects"
    (default_root / "slug-a").mkdir(parents=True)
    seal(default_root / "slug-a")
    monkeypatch.setenv("HOME", str(home))

    default = flow_usage.read_steps(flow_usage.build_reader(None))

    assert [str(f.directory) for f in default.unreadable_directories] == [
        str(default_root / "slug-a")
    ]

    (tmp_path / "rel" / "slug-b").mkdir(parents=True)
    seal(tmp_path / "rel" / "slug-b")
    monkeypatch.chdir(tmp_path)

    relative = flow_usage.read_steps(flow_usage.build_reader(Path("rel")))

    assert [str(f.directory) for f in relative.unreadable_directories] == ["rel/slug-b"]


# ── Task 4 — `steps` names the directories it could not look in, in text and JSON ──

STEPS_DIRECTORY_LINE = (
    "{directory} could not be looked in, so nothing in it is in this report. "
    "Run this command again once it can be looked in."
)


def _failures(*directories):
    return [run_usage.DirectoryFailure(directory=Path(d)) for d in directories]


def _read_two_runs(tmp_path):
    readable, failed = _two_runs(tmp_path)
    files = flow_usage.Files()
    return [
        flow_usage.read_run(flow_run, files)
        for flow_run in flow_usage.flow_runs(tmp_path, files)
    ]


def test_render_with_default_directories_is_byte_identical_to_baseline(tmp_path):
    runs = _read_two_runs(tmp_path)

    assert flow_usage.render(runs, unreadable_directories=()) == flow_usage.render(runs)
    assert flow_usage.render_json(runs, unreadable_directories=()) == flow_usage.render_json(runs)
    assert flow_usage.render([]) == (
        FALLBACK_LINE + "\n" + SPAN + "\n\nNo workflow runs read.\n" + NO_COST + "\n"
    )
    assert set(json.loads(flow_usage.render_json(runs))) == {"fallback", "span", "runs"}


def test_supplied_directories_suppress_the_empty_state_only_when_non_empty():
    locked = flow_usage.render([], unreadable_directories=_failures("root"))

    assert "No workflow runs read." not in locked
    assert STEPS_DIRECTORY_LINE.format(directory="root") in locked.splitlines()
    assert "No workflow runs read." in flow_usage.render([], unreadable_directories=[])


def test_directory_lines_follow_all_workflow_sections_and_precede_the_note_block(tmp_path):
    runs = _read_two_runs(tmp_path)

    lines = flow_usage.render(runs, unreadable_directories=_failures("root", "root/a")).splitlines()

    first = lines.index(STEPS_DIRECTORY_LINE.format(directory="root"))
    assert lines[first : first + 2] == [
        STEPS_DIRECTORY_LINE.format(directory="root"),
        STEPS_DIRECTORY_LINE.format(directory="root/a"),
    ]
    assert lines[first - 1] == ""
    assert lines[first + 2] == NO_COST
    for run in runs:
        wf_id = run.flow_run.wf_id
        header = next(n for n, line in enumerate(lines) if line.startswith(f"{wf_id} "))
        assert header < first - 1
    assert not any("could not be looked in" in line for line in lines[:first])


def test_json_unreadable_directories_is_top_level_and_absent_when_empty(tmp_path):
    runs = _read_two_runs(tmp_path)

    absent = json.loads(flow_usage.render_json(runs, unreadable_directories=()))
    present = json.loads(
        flow_usage.render_json(runs, unreadable_directories=_failures("root", "root/a"))
    )

    assert "unreadable_directories" not in absent
    assert list(present)[:3] == ["fallback", "span", "runs"]
    assert present["unreadable_directories"] == [
        {"directory": "root", "reason": "could not be looked in"},
        {"directory": "root/a", "reason": "could not be looked in"},
    ]
    assert present["runs"] == absent["runs"]


def test_wf_selection_prints_the_directory_line_outside_the_selected_section_and_exits_zero(
    tmp_path, capsys, seal
):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    (tmp_path / "slug-locked").mkdir()
    seal(tmp_path / "slug-locked")
    line = STEPS_DIRECTORY_LINE.format(directory=tmp_path / "slug-locked")

    text = _steps(tmp_path, capsys, "--wf", "wf_one")
    document = json.loads(_steps(tmp_path, capsys, "--wf", "wf_one", "--json"))

    assert line in text.splitlines()
    assert line not in _run_section(text, "wf_one")
    assert "No workflow runs read." not in text
    assert document["unreadable_directories"] == [
        {"directory": str(tmp_path / "slug-locked"), "reason": "could not be looked in"}
    ]


NOT_FOUND_LINE = (
    "error: no workflow run found for --wf {wf_id} under {root} "
    "— run `ka-telemetry steps` without --wf to see the workflow runs found there"
)
WF_PLACE_LINE = (
    "{directory} could not be looked in, so this command did not look for a workflow run there. "
    "Run this command again once it can be looked in."
)


def _wf_not_found(root, capsys, wf_id, *extra):
    exit_code = flow_usage.main(["--projects-root", str(root), "--wf", wf_id, *extra])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err.splitlines()


def test_wf_not_found_names_the_place_that_could_not_be_looked_in(tmp_path, capsys, seal):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    locked = tmp_path / "slug-locked"
    locked.mkdir()
    seal(locked)

    exit_code, out, err = _wf_not_found(tmp_path, capsys, "wf_nope")

    assert exit_code == 1
    assert out == ""
    assert err == [
        NOT_FOUND_LINE.format(wf_id="wf_nope", root=tmp_path),
        WF_PLACE_LINE.format(directory=locked),
    ]


@pytest.mark.parametrize("depth", ["root", "project", *DEPTHS])
@pytest.mark.parametrize("wf_id", ["wf_one", "wf_doesnotexist"])
def test_wf_not_found_names_every_place_for_each_depth(depth, wf_id, tmp_path, capsys, seal):
    root = tmp_path / "root"
    run_dir = _make_workflow_run(root, "slug-a", "sid-1", "wf_one")
    locked = {"root": root, "project": root / "slug-a", **_depth_paths(run_dir)}[depth]
    seal(locked)

    exit_code, out, err = _wf_not_found(root, capsys, wf_id)

    assert (exit_code, out) == (1, "")
    assert err == [
        NOT_FOUND_LINE.format(wf_id=wf_id, root=root),
        WF_PLACE_LINE.format(directory=locked),
    ]


def test_wf_not_found_names_several_places_in_order_once_each(tmp_path, capsys, seal):
    _make_workflow_run(tmp_path, "slug-a", "sid-readable", "wf_readable")
    locked = [
        _depth_paths(_make_workflow_run(tmp_path, "slug-b", "sid-2", "wf_two"))["session"],
        _depth_paths(_make_workflow_run(tmp_path, "slug-a", "sid-9", "wf_nine"))["subagents"],
        _make_workflow_run(tmp_path, "slug-c", "sid-3", "wf_three").parent.parent.parent.parent,
    ]
    for place in locked:
        seal(place)

    exit_code, out, err = _wf_not_found(tmp_path, capsys, "wf_nope")

    assert (exit_code, out) == (1, "")
    assert err[1:] == [
        WF_PLACE_LINE.format(directory=place)
        for place in (locked[1], locked[0], locked[2])
    ]
    assert len(err) == 4


def test_wf_not_found_with_json_is_the_same_as_without(tmp_path, capsys, seal):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    seal(_depth_paths(run_dir)["workflows"])

    plain = _wf_not_found(tmp_path, capsys, "wf_nope")
    as_json = _wf_not_found(tmp_path, capsys, "wf_nope", "--json")

    assert plain == as_json
    assert len(plain[2]) == 2


@pytest.mark.parametrize("extra", [(), ("--json",)])
def test_wf_not_found_on_clean_input_is_the_single_baseline_line(extra, tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")

    exit_code, out, err = _wf_not_found(tmp_path, capsys, "wf_nope", *extra)

    assert (exit_code, out) == (1, "")
    assert err == [NOT_FOUND_LINE.format(wf_id="wf_nope", root=tmp_path)]


def test_wf_found_beside_a_locked_place_prints_the_report_and_exits_zero(tmp_path, capsys, seal):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    other = _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_two")
    locked = seal(_depth_paths(other)["subagents"])

    exit_code = flow_usage.main(["--projects-root", str(tmp_path), "--wf", run_dir.name])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert STEPS_DIRECTORY_LINE.format(directory=locked) in captured.out.splitlines()


def test_main_discovers_once_and_does_not_reread_the_root_to_render(tmp_path, capsys, monkeypatch):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    files = _CountingFiles()
    monkeypatch.setattr(flow_usage, "Files", lambda: files)
    listed = []
    real = run_usage.list_directory
    monkeypatch.setattr(
        run_usage, "list_directory", lambda path: (listed.append(path), real(path))[1]
    )

    _steps(tmp_path, capsys)

    assert (files.discoveries, files.legacy_finds) == (1, 0)
    assert listed.count(tmp_path) == 1


def test_selected_wf_path_does_not_read_every_workflow(tmp_path, capsys, monkeypatch):
    for wf_id, sid in (("wf_one", "sid-1"), ("wf_two", "sid-2"), ("wf_three", "sid-3")):
        _make_workflow_run(tmp_path, "slug-a", sid, wf_id)
    read = []
    real = flow_usage.read_selected_runs

    def spy(runs, files):
        runs = list(runs)
        read.extend(run.wf_id for run in runs)
        return real(runs, files)

    monkeypatch.setattr(flow_usage, "read_selected_runs", spy)

    _steps(tmp_path, capsys, "--wf", "wf_two")

    assert read == ["wf_two"]


# ── A run directory that cannot be listed: its agent files are named as uncounted ──

UNCOUNTED_LINE = (
    "{directory} could not be listed, so its agent files could not be counted and this report "
    "has no count of agent files with no journal entry at all for its workflow run. "
    "The system reported: {reason}. Run this command again once that directory can be listed."
)
FOURTH_FIGURE = "agent files with no journal entry at all"


class _ListingFailsFiles(flow_usage.Files):
    """The real port, except that listing a run directory raises `error` — for the run
    directories in `only` when given, otherwise for every one."""

    def __init__(self, error: OSError, only: tuple[Path, ...] | None = None):
        self._error = error
        self._only = only

    def agent_ids_in(self, run_dir):
        if self._only is None or run_dir in self._only:
            raise self._error
        return super().agent_ids_in(run_dir)


class _PartialDirectory:
    """A directory whose listing yields matching names and then raises, as a listing that breaks
    part-way does."""

    def __init__(self, error: OSError):
        self._error = error

    def iterdir(self):
        yield Path("agent-yielded-before-failure.jsonl")
        yield Path("agent-also-yielded.jsonl")
        raise self._error


class _PartialListingFiles(flow_usage.Files):
    def agent_ids_in(self, run_dir):
        return super().agent_ids_in(_PartialDirectory(PermissionError(13, "Permission denied")))


def _without_fourth(cov: flow_usage.Coverage) -> flow_usage.Coverage:
    return dataclasses.replace(cov, files_with_no_journal_entry=0)


def _hand_built_run(directory: str, wf_id: str, failure=None) -> flow_usage.RunReading:
    flow_run = flow_usage.FlowRun(
        wf_id=wf_id, session_id="sid", slug="slug", run_dir=Path(directory)
    )
    return flow_usage.RunReading(
        flow_run=flow_run, agents=(), orphan_agent_files=(), agent_files_failure=failure
    )


def test_read_run_carries_failure_for_non_permission_oserror_after_journal_read(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    readable = flow_usage.read_run(flow_run, flow_usage.Files())

    run = flow_usage.read_run(
        flow_run, _ListingFailsFiles(OSError(errno.EIO, "Input/output\nerror\r\nmid-listing"))
    )

    assert run.agent_files_failure == flow_usage.AgentFilesFailure(
        directory=flow_run.run_dir, reason="Input/output error mid-listing"
    )
    assert run.orphan_agent_files == ()
    cov = flow_usage.coverage(run)
    assert cov.files_with_no_journal_entry is None
    assert _without_fourth(cov) == _without_fourth(flow_usage.coverage(readable))
    assert [a.agent_id for a in run.agents] == ["a1"]
    assert run.agents == readable.agents
    assert flow_usage.aggregate(run) == flow_usage.aggregate(readable)
    assert dataclasses.is_dataclass(run.agent_files_failure)
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.agent_files_failure.reason = "changed"


def test_read_run_empty_readable_journal_still_reports_listing_failure(tmp_path):
    flow_run = _flow_run_fixture(tmp_path, [])

    files = _ListingFailsFiles(PermissionError(13, "Permission denied"))

    run = flow_usage.read_run(flow_run, files)

    assert run.agent_files_failure == flow_usage.AgentFilesFailure(
        directory=flow_run.run_dir, reason="Permission denied"
    )
    assert flow_usage.coverage(run).files_with_no_journal_entry is None


def test_agent_ids_in_returns_empty_for_absent_dir_and_for_path_below_non_directory(tmp_path):
    files = flow_usage.Files()
    regular = tmp_path / "regular"
    regular.write_text("x")

    assert files.agent_ids_in(tmp_path / "absent") == []
    assert files.agent_ids_in(regular / "below") == []

    flow_run = _flow_run_fixture(tmp_path / "known", [_started("a1"), _result("a1", PRODUCED_STEP)])
    run = flow_usage.read_run(flow_run, files)
    assert run.agent_files_failure is None
    assert flow_usage.coverage(run).files_with_no_journal_entry == 0


def test_read_run_missing_journal_keeps_existing_path(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    (flow_run.run_dir / "journal.jsonl").unlink()

    files = _ListingFailsFiles(PermissionError(13, "Permission denied"))

    run = flow_usage.read_run(flow_run, files)

    assert run.agent_files_failure is None
    assert run.orphan_agent_files == ()
    assert flow_usage.coverage(run).files_with_no_journal_entry == 0


def test_agent_ids_in_failure_after_partial_enumeration_returns_no_names(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    readable = flow_usage.read_run(flow_run, flow_usage.Files())
    partial_error = PermissionError(13, "Permission denied")

    with pytest.raises(PermissionError) as raised:
        flow_usage.Files().agent_ids_in(_PartialDirectory(partial_error))
    assert raised.value is partial_error

    run = flow_usage.read_run(flow_run, _PartialListingFiles())

    assert run.orphan_agent_files == ()
    assert run.agent_files_failure.reason == "Permission denied"
    assert run.agents == readable.agents
    assert "yielded" not in repr(run)


def test_agent_ids_in_matching_is_unchanged_on_success(tmp_path):
    for name in (
        "agent-a.jsonl",
        "agent-.jsonl",
        "agent-a.meta.json",
        "agent-x.json",
        "other-agent-y.jsonl",
        "agent-z.jsonl.bak",
        "journal.jsonl",
    ):
        (tmp_path / name).write_text("")
    (tmp_path / "agent-dir.jsonl").mkdir()
    (tmp_path / "agent-link.jsonl").symlink_to(tmp_path / "nowhere")

    found = flow_usage.Files().agent_ids_in(tmp_path)

    assert sorted(found) == ["", "a", "dir", "link"]
    assert sorted(found) == sorted(p.stem[len("agent-") :] for p in tmp_path.glob("agent-*.jsonl"))


def test_read_run_unreadable_journal_does_not_enumerate_agents(tmp_path):
    flow_run = _run_with_agent_files(tmp_path)
    _directory_in_place_of_journal(flow_run)
    files = _RecordingFiles()

    run = flow_usage.read_run(flow_run, files)

    assert files.agent_files_reached == []
    assert run.agent_files_failure is None
    assert run.unreadable_journal_files == 1


def test_failed_run_beside_readable_sibling_keeps_sibling_all_four_figures(tmp_path):
    readable, failed = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_readable"), (
        _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_failed")
    )
    files = _ListingFailsFiles(PermissionError(13, "Permission denied"), only=(failed,))
    runs = [flow_usage.read_run(r, files) for r in flow_usage.flow_runs(tmp_path, files)]

    sibling = next(r for r in runs if r.flow_run.run_dir == readable)
    assert flow_usage.coverage(sibling).files_with_no_journal_entry == 0
    text = flow_usage.render(runs)
    assert _run_section(text, "wf_readable")[1].endswith(f"0 {FOURTH_FIGURE}")
    assert FOURTH_FIGURE not in "\n".join(_run_section(text, "wf_failed"))


def test_read_steps_exposes_failure_on_the_run_and_keeps_discovery_directories_discovery_only(
    tmp_path, with_mode, seal
):
    failed = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_failed")
    _make_workflow_run(tmp_path, "slug-a", "sid-2", "wf_readable")
    (tmp_path / "slug-locked").mkdir()
    seal(tmp_path / "slug-locked")
    with_mode(failed, 0o111)

    reading = flow_usage.read_steps(_reader(tmp_path))

    by_id = {r.flow_run.wf_id: r for r in reading.runs}
    assert by_id["wf_failed"].agent_files_failure == flow_usage.AgentFilesFailure(
        directory=failed, reason="Permission denied"
    )
    assert by_id["wf_readable"].agent_files_failure is None
    assert reading.unreadable_directories == (
        run_usage.DirectoryFailure(directory=tmp_path / "slug-locked"),
    )


def test_run_reading_accepts_agent_files_failure_keyword_and_keeps_unreadable_journal_files_last(
    tmp_path,
):
    flow_run = flow_usage.FlowRun(wf_id="wf_t", session_id="sid", slug="slug", run_dir=tmp_path)
    failure = flow_usage.AgentFilesFailure(directory=tmp_path, reason="Permission denied")

    run = flow_usage.RunReading(flow_run, (), (), 5, 6, agent_files_failure=failure)

    assert (run.unreadable_journal_lines, run.unreadable_journal_files) == (5, 6)
    assert run.agent_files_failure is failure
    assert flow_usage.RunReading(flow_run, (), ()).agent_files_failure is None
    names = [f.name for f in dataclasses.fields(flow_usage.RunReading)]
    assert names[-1] == "unreadable_journal_files"
    assert names.index("agent_files_failure") < names.index("unreadable_journal_lines")
    assert next(
        f for f in dataclasses.fields(flow_usage.RunReading) if f.name == "agent_files_failure"
    ).kw_only
    assert [f.name for f in dataclasses.fields(flow_usage.Coverage)][-1] == (
        "unreadable_journal_files"
    )
    assert flow_usage.Coverage(1, 2, 3, 4).files_with_no_journal_entry == 4


def test_coverage_fourth_figure_is_none_exactly_when_run_has_failure(tmp_path):
    flow_run = flow_usage.FlowRun(wf_id="wf_t", session_id="sid", slug="slug", run_dir=tmp_path)
    failure = flow_usage.AgentFilesFailure(directory=tmp_path, reason="Permission denied")

    failed = flow_usage.RunReading(flow_run, (), (), agent_files_failure=failure)
    counted = flow_usage.RunReading(flow_run, (), ("o1", "o2"))
    legacy = flow_usage.RunReading(flow_run, (), ())

    assert flow_usage.coverage(failed).files_with_no_journal_entry is None
    assert flow_usage.coverage(counted).files_with_no_journal_entry == 2
    assert flow_usage.coverage(legacy).files_with_no_journal_entry == 0


@pytest.mark.parametrize(
    ("depth", "mode"),
    [(None, None), ("workflow-run", 0o111), ("workflow-run", 0o000), ("workflow-run", 0o444),
     ("session", 0o000), ("workflows", 0o000)],
)
def test_flow_runs_and_find_workflow_dirs_return_base_results_on_every_fixture(
    depth, mode, tmp_path, with_mode
):
    run_dir = _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")
    if depth is not None:
        with_mode(_depth_paths(run_dir)[depth], mode)
    files = flow_usage.Files()
    # What `Path.glob("*/*/subagents/workflows/*/journal.jsonl")` returns on Python 3.13 and
    # later. Earlier Pythons' own glob answers differently, so it is not asked here.
    base_result = [run_dir] if mode in (None, 0o111) else []

    assert files.find_workflow_dirs(tmp_path) == base_result
    assert [r.run_dir for r in flow_usage.flow_runs(tmp_path, files)] == base_result
    assert files.find_workflow_dirs(tmp_path / "missing-root") == []


def test_diagnostics_are_sorted_by_path_components_not_joined_text_or_workflow_id():
    discovery = [
        run_usage.DirectoryFailure(directory=Path("root/a-elsewhere")),
        run_usage.DirectoryFailure(directory=Path("root/a/child")),
    ]
    runs = [
        _hand_built_run(
            "root/a-b", "wf_a", flow_usage.AgentFilesFailure(Path("root/a-b"), "reason b")
        ),
        _hand_built_run(
            "root/a/z", "wf_z", flow_usage.AgentFilesFailure(Path("root/a/z"), "reason z")
        ),
    ]
    expected = [
        STEPS_DIRECTORY_LINE.format(directory="root/a/child"),
        UNCOUNTED_LINE.format(directory="root/a/z", reason="reason z"),
        UNCOUNTED_LINE.format(directory="root/a-b", reason="reason b"),
        STEPS_DIRECTORY_LINE.format(directory="root/a-elsewhere"),
    ]

    lines = flow_usage.render(runs, unreadable_directories=discovery).splitlines()
    document = json.loads(flow_usage.render_json(runs, unreadable_directories=discovery))

    first = lines.index(expected[0])
    assert lines[first : first + 4] == expected
    assert lines[first + 4] == NO_COST
    assert [(o["directory"], o["reason"]) for o in document["unreadable_directories"]] == [
        ("root/a/child", "could not be looked in"),
        ("root/a/z", "reason z"),
        ("root/a-b", "reason b"),
        ("root/a-elsewhere", "could not be looked in"),
    ]
    assert all(set(o) == {"directory", "reason"} for o in document["unreadable_directories"])
    assert [r["coverage"]["files_with_no_journal_entry"] for r in document["runs"]] == [None, None]
    coverage_lines = [line for line in lines if line.strip().startswith("Coverage:")]
    assert not [line for line in coverage_lines if FOURTH_FIGURE in line]


def test_render_without_new_failures_keeps_discovery_order_and_bytes():
    runs = [_hand_built_run("root/b", "wf_b"), _hand_built_run("root/a", "wf_a")]
    discovery = _failures("root/z", "root/c", "root/a")

    lines = flow_usage.render(runs, unreadable_directories=discovery).splitlines()
    document = json.loads(flow_usage.render_json(runs, unreadable_directories=discovery))

    assert [line.split(" ")[0] for line in lines if "could not be looked in" in line] == [
        "root/z", "root/c", "root/a"
    ]
    assert [o["directory"] for o in document["unreadable_directories"]] == [
        "root/z", "root/c", "root/a"
    ]
    assert lines[3].startswith("wf_b ")
    assert f"/ 0 {FOURTH_FIGURE}" in flow_usage.render(runs)
    assert "unreadable_directories" not in json.loads(flow_usage.render_json(runs))
    assert json.loads(flow_usage.render_json(runs))["runs"][0]["coverage"] == {
        "with_step": 0,
        "without_step": 0,
        "started_no_result": 0,
        "files_with_no_journal_entry": 0,
    }


def test_render_selects_line_by_failure_kind_not_by_reason_text():
    same_reason = "Permission denied"
    discovery = [run_usage.DirectoryFailure(directory=Path("root/d"), reason=same_reason)]
    runs = [
        _hand_built_run(
            "root/e", "wf_e", flow_usage.AgentFilesFailure(Path("root/e"), same_reason)
        )
    ]

    lines = flow_usage.render(runs, unreadable_directories=discovery).splitlines()

    assert STEPS_DIRECTORY_LINE.format(directory="root/d") in lines
    assert UNCOUNTED_LINE.format(directory="root/e", reason=same_reason) in lines
    assert lines.index(STEPS_DIRECTORY_LINE.format(directory="root/d")) < lines.index(
        UNCOUNTED_LINE.format(directory="root/e", reason=same_reason)
    )


# ── Integrity review: the system's reason and the coverage line of an uncounted run ──


@pytest.mark.parametrize(
    ("given", "carried"),
    [
        ("first\nsecond", "first second"),
        ("first\r\nsecond", "first second"),
        ("first\rsecond", "first second"),
        ("first\n\nsecond", "first  second"),
        ("first \n second", "first   second"),
    ],
)
def test_read_run_folds_each_line_break_of_the_reason_into_one_space(given, carried, tmp_path):
    """CP-467: the system's reason is carried as the system gave it, each line break folded into
    a single space and nothing else changed — `\\r\\n` is one line break, two breaks are two."""
    flow_run = _flow_run_fixture(tmp_path, [])

    run = flow_usage.read_run(flow_run, _ListingFailsFiles(OSError(errno.EIO, given)))

    assert run.agent_files_failure.reason == carried


def test_uncounted_run_keeps_its_unreadable_clauses_after_the_three_figures(tmp_path):
    """CP-466: the coverage line of a run whose agent files could not be counted drops only the
    fourth figure and the ` / ` before it; the ` — ` clauses that follow it are those the run
    prints when its directory can be listed. CP-468: in `--json` only that member turns `null`."""
    flow_run = flow_usage.FlowRun(wf_id="wf_t", session_id="sid", slug="slug", run_dir=tmp_path)
    failure = flow_usage.AgentFilesFailure(directory=tmp_path, reason="Permission denied")
    counted = flow_usage.RunReading(flow_run, (), (), unreadable_journal_lines=2)
    uncounted = dataclasses.replace(counted, agent_files_failure=failure)

    counted_line = _run_section(flow_usage.render([counted]), "wf_t")[1].strip()
    uncounted_line = _run_section(flow_usage.render([uncounted]), "wf_t")[1].strip()

    assert counted_line == (
        f"Coverage: 0 with a step / 0 without / 0 started with no result / 0 {FOURTH_FIGURE}"
        " — 2 unreadable journal lines skipped"
    )
    assert uncounted_line == (
        "Coverage: 0 with a step / 0 without / 0 started with no result"
        " — 2 unreadable journal lines skipped"
    )
    counted_json = json.loads(flow_usage.render_json([counted]))["runs"][0]["coverage"]
    uncounted_json = json.loads(flow_usage.render_json([uncounted]))["runs"][0]["coverage"]
    assert uncounted_json == {**counted_json, "files_with_no_journal_entry": None}


# ── A span on every step row — from its earliest readable timestamp to its latest ──

SPAN_START = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
STEP_ROW = {"step": "development-specs", "status": "ok", "artifact": "x.md"}


def _at(seconds: float) -> str:
    """The moment `seconds` after 10:00:00 UTC on the day the fixtures use, written as a
    transcript writes it."""
    return (SPAN_START + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record(timestamp, kind: str = "user") -> str:
    """A transcript record that carries no usage: a prompt, a tool result, anything but a
    response. A `timestamp` of `None` is written as `null`."""
    return json.dumps({"type": kind, "timestamp": timestamp})


def _response(timestamp, response_id: str | None, input_tokens: int = 1) -> str:
    """One content block of a response: carries its usage verbatim, keyed by `response_id`."""
    message = {
        "model": "claude-sonnet-5",
        "usage": {"input_tokens": input_tokens, "output_tokens": 1},
    }
    if response_id is not None:
        message["id"] = response_id
    return json.dumps({"type": "assistant", "timestamp": timestamp, "message": message})


_RESPONSE_RUNS = itertools.count()


def _responses_at(moments, prefix: str | None = None) -> list[str]:
    """One counted response per moment, each with an id of its own, so none is a repeat of
    another and each is timed at the moment given (`None` writes a response with no moment). Two
    calls without a `prefix` never share an id, so two agents built this way share no response
    and the report counts every one of them."""
    prefix = prefix if prefix is not None else f"r{next(_RESPONSE_RUNS)}-"
    return [_response(moment, f"{prefix}{number}") for number, moment in enumerate(moments)]


def _span_run(
    tmp_path: Path,
    transcripts: dict[str, list[str]],
    *,
    results: dict[str, object] | None = None,
    files: flow_usage.Files | None = None,
) -> flow_usage.RunReading:
    """A run whose every agent in `transcripts` is journalled with the result `results` gives it
    (the one step's producer where it gives none), read through `files`."""
    journal = []
    for agent_id in transcripts:
        journal += [_started(agent_id), _result(agent_id, (results or {}).get(agent_id, STEP_ROW))]
    flow_run = _flow_run_fixture(tmp_path, journal, transcripts=transcripts)
    return flow_usage.read_run(flow_run, files or flow_usage.Files())


def _only_row(run: flow_usage.RunReading) -> flow_usage.AggregateRow:
    (row,) = flow_usage.aggregate(run)
    return row


def _row_span_of(tmp_path: Path, lines: list[str]) -> float | None:
    return _only_row(_span_run(tmp_path, {"a1": lines})).span_seconds


NO_MOMENT_LINES = [
    pytest.param([], id="empty-transcript"),
    pytest.param([""], id="blank-line"),
    pytest.param([json.dumps({"type": "user"})], id="no-timestamp"),
    pytest.param([_record(None)], id="null"),
    pytest.param([_record(5)], id="number"),
    pytest.param([_record(True)], id="boolean"),
    pytest.param([_record(["2026-09-07T10:00:00Z"])], id="list"),
    pytest.param([_record({"at": "2026-09-07T10:00:00Z"})], id="object"),
    pytest.param([_record("")], id="empty-string"),
    pytest.param([_record("not a date"), _record("2026-13-45T99:00:00Z")], id="invalid-dates"),
]


@pytest.mark.parametrize("lines", NO_MOMENT_LINES)
def test_row_with_no_readable_moment_has_no_span(tmp_path, lines):
    run = _span_run(tmp_path, {"a1": lines})

    row = _only_row(run)
    assert row.span_seconds is None
    assert flow_usage.coverage(run).unreadable_lines == 0
    assert "    span=n/a\n" in flow_usage.render([run]) + "\n"
    assert json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]["span_seconds"] is None


@pytest.mark.parametrize("lines", NO_MOMENT_LINES)
def test_row_with_no_readable_moment_and_model_spend_keeps_its_spend_and_gets_no_span(
    tmp_path, lines
):
    with_spend = [*lines, _response(None, "r1", input_tokens=7)]
    run = _span_run(tmp_path, {"a1": with_spend})

    row = _only_row(run)
    assert row.span_seconds is None
    assert row.spend["claude-sonnet-5"].input == 7
    assert flow_usage.coverage(run).unreadable_lines == 0
    text = flow_usage.render([run])
    assert text.splitlines().index("    span=n/a") + 1 == next(
        n for n, line in enumerate(text.splitlines()) if line.startswith("    claude-sonnet-5")
    )


def test_row_without_model_lines_still_gets_a_span_line(tmp_path):
    run = _span_run(tmp_path, {"a1": [_record(_at(0)), _record(_at(30))]})

    lines = flow_usage.render([run]).splitlines()
    header = next(n for n, line in enumerate(lines) if line.startswith("  development-specs"))
    assert lines[header + 1] == "    span=n/a"
    assert not any(line.startswith("    claude-") for line in lines)


def test_span_line_sits_before_the_rows_model_lines(tmp_path):
    run = _span_run(tmp_path, {"a1": [_response(_at(0), "r1"), _response(_at(8), "r2")]})

    lines = flow_usage.render([run]).splitlines()
    header = next(n for n, line in enumerate(lines) if line.startswith("  development-specs"))
    assert lines[header + 1] == "    span=8s"
    assert lines[header + 2].startswith("    claude-sonnet-5: input=2 ")


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param((0, 60), (30, 120), 120.0, id="overlapping"),
        pytest.param((0, 60), (60, 120), 120.0, id="abutting"),
        pytest.param((0, 60), (300, 420), 420.0, id="gapped"),
        pytest.param((0, 600), (100, 200), 600.0, id="contained"),
    ],
)
def test_grouped_agents_span_the_union_not_the_sum(tmp_path, first, second, expected):
    other_role = {"step": "development-specs", "result": "findings", "findings": 0}
    run = _span_run(
        tmp_path,
        {
            "a1": _responses_at([_at(first[0]), _at(first[1])]),
            "a2": _responses_at([_at(second[0]), _at(second[1])]),
            # Another role on the same step is another row, and adds nothing to this one.
            "a3": _responses_at([_at(-5000), _at(9000)]),
        },
        results={"a3": other_role},
    )

    produce, audit = flow_usage.aggregate(run)
    assert produce.agent_ids == ("a1", "a2") and produce.span_seconds == expected
    assert audit.agent_ids == ("a3",) and audit.span_seconds == 14000.0


def test_a_grouped_fallback_row_spans_its_agents_together(tmp_path):
    run = _span_run(
        tmp_path,
        {
            "a1": _responses_at([_at(0), _at(60)]),
            "a2": _responses_at([_at(300), _at(420)]),
        },
        results={"a1": "no step", "a2": "no step"},
    )

    (row,) = flow_usage.aggregate(run)
    assert row.is_fallback and row.agent_ids == ("a1", "a2")
    assert row.span_seconds == 420.0
    assert "    span=420s" in flow_usage.render([run]).splitlines()


def test_single_or_identical_moments_give_zero(tmp_path):
    same_instant = [
        "2026-09-07T10:00:00Z",
        "2026-09-07T12:00:00+02:00",
        "2026-09-07T05:00:00-05:00",
    ]
    for number, lines in enumerate(
        [
            _responses_at([_at(0)]),
            _responses_at([_at(0), _at(0), _at(0)]),
            _responses_at(same_instant),
        ]
    ):
        run = _span_run(tmp_path / str(number), {"a1": lines})
        assert _only_row(run).span_seconds == 0.0, number
        assert "    span=0s" in flow_usage.render([run]).splitlines(), number
        row = json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]
        assert row["span_seconds"] == 0.0 and isinstance(row["span_seconds"], float), number


def test_span_is_independent_of_line_and_agent_order(tmp_path):
    moments = [_at(seconds) for seconds in (40, 5, 90, 0, 62)]
    forward = _span_run(
        tmp_path / "forward",
        {"a1": _responses_at(moments[:3], "p"), "a2": _responses_at(moments[3:], "q")},
    )
    backward = _span_run(
        tmp_path / "backward",
        {
            "a2": _responses_at(list(reversed(moments[3:])), "q"),
            "a1": _responses_at(list(reversed(moments[:3])), "p"),
        },
    )

    assert _only_row(forward).span_seconds == 90.0
    assert _only_row(backward).span_seconds == 90.0


def test_invalid_moments_beside_one_valid_moment_give_zero(tmp_path):
    lines = [
        *_responses_at(["nope", _at(12), None, "2026-02-30"]),
        _record(7),  # not a response, and a moment that is no string
    ]
    assert _row_span_of(tmp_path, lines) == 0.0


def test_valid_and_invalid_response_moments_use_only_the_valid_ones(tmp_path):
    lines = _responses_at([_at(0), "not a date", _at(30), None, "2026-13-45T99:00:00Z"])

    assert _row_span_of(tmp_path, lines) == 30.0


def test_prompt_and_tool_result_timestamps_do_not_extend_the_span(tmp_path):
    lines = [
        _record(_at(0), kind="user"),
        _response(_at(5), "r1"),
        _response(_at(20), "r2"),
        _record(_at(29), kind="system"),
    ]
    assert _row_span_of(tmp_path / "beside", lines) == 15.0
    assert _row_span_of(tmp_path / "alone", [_record(_at(0)), _record(_at(29))]) is None


def test_assistant_record_without_usage_does_not_extend_the_span(tmp_path):
    """An `assistant` record the reader accepts but takes no spend from — one with no message,
    one whose message has no usage, one whose usage is empty — is not a counted response: it moves
    neither end of the span, adds nothing to spend and nothing to coverage."""
    lines = [
        json.dumps({"type": "assistant", "timestamp": _at(0)}),
        _response(_at(10), "r1", input_tokens=4),
        json.dumps(
            {"type": "assistant", "timestamp": _at(25), "message": {"model": "claude-sonnet-5"}}
        ),
        json.dumps({"type": "assistant", "timestamp": _at(33), "message": {"usage": {}}}),
        _response(_at(20), "r2", input_tokens=0),
    ]
    run = _span_run(tmp_path, {"a1": lines})

    row = _only_row(run)
    assert row.span_seconds == 10.0
    assert row.spend["claude-sonnet-5"].input == 4
    assert flow_usage.coverage(run).unreadable_lines == 0


def test_response_only_timing_is_the_span_the_row_gives(tmp_path):
    lines = [_record(_at(0)), _response(_at(5), "r1"), _response(_at(20), "r2"), _record(_at(29))]
    run = _span_run(tmp_path, {"a1": lines})

    responses_only = run_usage.span_seconds(
        run_usage.usage_records(
            run.flow_run.run_dir / "agent-a1.jsonl", flow_usage.Files()
        )
    )
    assert responses_only == 15.0
    assert _only_row(run).span_seconds == responses_only


def test_later_block_of_a_counted_response_does_not_extend_the_span_and_spend_counts_once(
    tmp_path,
):
    lines = [
        _response(_at(0), "r1", input_tokens=9),
        _response(_at(41), "r1", input_tokens=9),  # a later content block of the same response
    ]
    row = _only_row(_span_run(tmp_path, {"a1": lines}))

    assert row.span_seconds == 0.0
    assert row.spend["claude-sonnet-5"].input == 9


def test_row_with_only_non_response_records_has_no_span_and_no_model_lines(tmp_path):
    run = _span_run(tmp_path, {"a1": [_record(_at(3)), _record(_at(10), kind="attachment")]})

    row = _only_row(run)
    assert row.span_seconds is None and row.spend == {}
    lines = flow_usage.render([run]).splitlines()
    assert "    span=n/a" in lines
    assert not any(line.startswith("    claude-") for line in lines)
    assert json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]["span_seconds"] is None


def test_counted_response_without_a_moment_counts_tokens_and_gives_no_span(tmp_path):
    lines = [_record(_at(0)), _response(None, "r1", input_tokens=7), _record(_at(60))]
    run = _span_run(tmp_path, {"a1": lines})

    row = _only_row(run)
    assert row.span_seconds is None and row.spend["claude-sonnet-5"].input == 7
    text = flow_usage.render([run]).splitlines()
    assert text.index("    span=n/a") + 1 == next(
        n for n, line in enumerate(text) if line.startswith("    claude-sonnet-5: input=7 ")
    )


def test_non_string_timestamp_keeps_the_readers_rejection_and_unreadable_count(tmp_path):
    rejected = json.dumps(
        {
            "type": "assistant",
            "timestamp": 5,
            "message": {"id": "r0", "model": "claude-sonnet-5", "usage": {"input_tokens": 90}},
        }
    )
    run = _span_run(tmp_path, {"a1": [rejected, _response(_at(0), "r1"), _response(_at(8), "r2")]})

    row = _only_row(run)
    assert row.span_seconds == 8.0 and row.spend["claude-sonnet-5"].input == 2
    assert flow_usage.coverage(run).unreadable_lines == 1
    (agent,) = run.agents
    assert agent.unreadable_lines == 1 and len(agent.timestamps) == 2


def test_one_timed_response_among_prompts_spans_zero(tmp_path):
    lines = [_record(_at(0)), _response(_at(5), "r1"), _record(_at(545))]
    run = _span_run(tmp_path, {"a1": lines})

    assert _only_row(run).span_seconds == 0.0
    assert "    span=0s" in flow_usage.render([run]).splitlines()
    row = json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]
    assert row["span_seconds"] == 0.0 and isinstance(row["span_seconds"], float)


def test_distinct_responses_at_one_instant_span_zero(tmp_path):
    lines = [_response(_at(12), "r1"), _response(_at(12), "r2"), _response(_at(12), "r3")]

    assert _row_span_of(tmp_path, lines) == 0.0


def test_split_response_is_timed_at_its_first_record_and_counted_once(tmp_path):
    lines = [
        _response("2026-09-07T12:00:00Z", "x1", input_tokens=3),
        _response("2026-09-07T12:00:10Z", "x2", input_tokens=6),
        _response("2026-09-07T12:00:30Z", "x2", input_tokens=6),
    ]
    row = _only_row(_span_run(tmp_path, {"a1": lines}))

    assert row.span_seconds == 10.0
    assert row.spend["claude-sonnet-5"].input == 9


@pytest.mark.parametrize("first_moment", [None, "not a date"])
def test_later_fragment_never_supplies_the_moment_the_first_lacks(tmp_path, first_moment):
    lines = [
        _response(first_moment, "x1", input_tokens=2),
        _response(_at(30), "x1", input_tokens=2),
    ]
    row = _only_row(_span_run(tmp_path, {"a1": lines}))

    assert row.span_seconds is None and row.spend["claude-sonnet-5"].input == 2


def test_records_without_response_id_each_count_and_each_time(tmp_path):
    lines = [_response(_at(0), None, input_tokens=3), _response(_at(30), None, input_tokens=4)]
    row = _only_row(_span_run(tmp_path, {"a1": lines}))

    assert row.span_seconds == 30.0 and row.spend["claude-sonnet-5"].input == 7


def test_same_response_id_in_two_agents_is_counted_once_and_not_merged(tmp_path):
    run = _span_run(
        tmp_path,
        {
            "a1": [_response(_at(0), "shared", input_tokens=3)],
            "a2": [_response(_at(60), "shared", input_tokens=4)],
        },
    )

    row = _only_row(run)
    assert row.span_seconds == 0.0 and row.spend["claude-sonnet-5"].input == 3


def test_rejected_record_before_a_valid_one_with_the_same_id_reserves_nothing(tmp_path):
    rejected = json.dumps(
        {
            "type": "assistant",
            "timestamp": _at(0),
            "message": {"id": "r1", "usage": {"input_tokens": -1}},
        }
    )
    lines = [rejected, _response(_at(10), "r1", input_tokens=5), _response(_at(40), "r2")]
    run = _span_run(tmp_path, {"a1": lines})

    row = _only_row(run)
    assert row.span_seconds == 30.0 and row.spend["claude-sonnet-5"].input == 6
    assert flow_usage.coverage(run).unreadable_lines == 1


@pytest.mark.parametrize(
    ("moments", "expected"),
    [
        pytest.param(
            ["2026-09-07T10:00:00", "2026-09-07T10:01:00Z"], 60.0, id="offset-less-is-utc"
        ),
        pytest.param(
            ["2026-09-07T10:00:00", "2026-09-07T10:01:00"], 60.0, id="all-offset-less"
        ),
        pytest.param(
            ["2026-09-07T10:00:00+02:00", "2026-09-07T08:01:00Z"], 60.0, id="plus-offset"
        ),
        pytest.param(
            ["2026-09-07T10:00:00-05:00", "2026-09-07T15:00:30Z"], 30.0, id="minus-offset"
        ),
        pytest.param(["2026-09-07", "2026-09-07T00:01:00Z"], 60.0, id="date-only"),
        pytest.param(
            ["garbage", "2026-09-07T10:00:00Z", "2026-09-07T10:00:30Z"], 30.0, id="garbage"
        ),
        pytest.param(
            ["2026-09-07T10:00:00Z", "2026-09-07T25:00:00Z"], 0.0, id="hour-out-of-range"
        ),
    ],
)
def test_timestamp_domain_is_the_one_runs_measures_a_run_in(tmp_path, moments, expected):
    assert _row_span_of(tmp_path, _responses_at(moments)) == expected


def test_offset_less_timestamp_is_read_as_utc(tmp_path):
    lines = _responses_at(["2026-09-07T10:00:00", "2026-09-07T10:02:00Z"])
    assert _row_span_of(tmp_path, lines) == 120.0


def test_offset_less_moment_is_read_as_utc_beside_a_z_moment(tmp_path):
    lines = _responses_at(["2026-09-12T10:00:00", "2026-09-12T10:00:30Z"])

    assert _row_span_of(tmp_path, lines) == 30.0


def test_explicit_offsets_agree_and_invalid_strings_are_ignored(tmp_path):
    lines = _responses_at(["2026-09-07T12:00:00+02:00", "2026-09-07T10:00:00Z", "yesterday"])
    assert _row_span_of(tmp_path, lines) == 0.0


EARLIEST = "2026-09-07T00:00:00Z"
REJECTED_LINES = [
    pytest.param('{"type": "user", "timestamp": "2026-09-07T00:00:00Z"', id="malformed-json"),
    pytest.param(json.dumps([EARLIEST]), id="json-array"),
    pytest.param(json.dumps(EARLIEST), id="json-string"),
    pytest.param(
        json.dumps({"type": "assistant", "timestamp": EARLIEST, "message": "text"}),
        id="message-not-an-object",
    ),
    pytest.param(
        json.dumps({"type": "assistant", "timestamp": EARLIEST, "message": {"usage": [1]}}),
        id="usage-not-an-object",
    ),
    pytest.param(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": EARLIEST,
                "message": {"usage": {"input_tokens": 1}, "model": 5},
            }
        ),
        id="model-not-a-string",
    ),
    pytest.param(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": EARLIEST,
                "message": {"usage": {"input_tokens": 1}, "id": ["r"]},
            }
        ),
        id="response-id-not-a-string",
    ),
    pytest.param(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": EARLIEST,
                "message": {"usage": {"input_tokens": -1}},
            }
        ),
        id="negative-tokens",
    ),
    pytest.param(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": EARLIEST,
                "message": {"usage": {"input_tokens": 1.5}},
            }
        ),
        id="fractional-tokens",
    ),
    pytest.param(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": EARLIEST,
                "message": {"usage": {"input_tokens": True}},
            }
        ),
        id="boolean-tokens",
    ),
    pytest.param(
        '{"type": "assistant", "timestamp": "2026-09-07T00:00:00Z", '
        '"message": {"usage": {"input_tokens": -0}}}',
        id="signed-integer-zero",
    ),
]


@pytest.mark.parametrize("rejected", REJECTED_LINES)
def test_rejected_records_give_no_moment(tmp_path, rejected):
    lines = [rejected, _response(_at(0), "r1", input_tokens=3), _response(_at(60), "r2")]
    run = _span_run(tmp_path, {"a1": lines})
    path = run.flow_run.run_dir / "agent-a1.jsonl"

    # The earliest moment sits on the rejected line; the span starts at the first accepted one.
    assert _only_row(run).span_seconds == 60.0
    # Spend and every coverage count are what the unchanged reader finds in the same file.
    counter = run_usage.UnreadableLines()
    expected_spend = run_usage.spend_by_model(
        run_usage.usage_records(path, flow_usage.Files(), unreadable_lines=counter)
    )
    assert _only_row(run).spend == expected_spend
    (agent,) = run.agents
    assert (agent.unreadable_lines, agent.unreadable_agent_transcript) == (counter.count, False)
    assert counter.count == 1
    assert flow_usage.coverage(run).unreadable_lines == 1


def test_invalid_timestamp_data_adds_no_unreadable_category(tmp_path):
    lines = [_record("not a date"), _record(None), _record(5), _response(_at(0), "r1")]
    run = _span_run(tmp_path, {"a1": lines})

    assert flow_usage.coverage(run) == flow_usage.Coverage(
        with_step=1, without_step=0, started_no_result=0, files_with_no_journal_entry=0
    )


class _UnreadableTranscript(flow_usage.Files):
    """The real port, except that one agent's transcript cannot be read."""

    def __init__(self, agent_id: str) -> None:
        self._name = f"agent-{agent_id}.jsonl"

    def read_lines(self, path):
        return None if path.name == self._name else super().read_lines(path)


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_missing_or_unreadable_transcript_gives_no_span_alone(tmp_path, how):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", STEP_ROW)],
        transcripts={"a1": _responses_at([_at(0), _at(50)])},
    )
    if how == "missing":
        (flow_run.run_dir / "agent-a1.jsonl").unlink()

    run = flow_usage.read_run(flow_run, _UnreadableTranscript("a1"))

    row = _only_row(run)
    assert row.span_seconds is None and row.spend == {}
    assert flow_usage.coverage(run).unreadable_agent_transcripts == 1
    assert "    span=n/a" in flow_usage.render([run]).splitlines()


def test_grouped_row_with_a_timed_and_an_untimed_agent_takes_endpoints_from_the_timed_one(
    tmp_path,
):
    """CP-487, CP-488 and CP-490 in one grouped row: `a2` is readable and counted — its response
    carries tokens but no moment — and its prompts and tool results stretch far past `a1`'s
    responses. Only `a1`'s responses give the row its ends; `a2` still adds its tokens."""
    run = _span_run(
        tmp_path,
        {
            "a1": [_response(_at(0), "r1", input_tokens=3), _response(_at(50), "r2")],
            "a2": [
                _record(_at(-5000)),
                _response(None, "s1", input_tokens=40),
                _record(_at(9000), kind="tool_result"),
            ],
        },
    )

    row = _only_row(run)
    assert row.agent_ids == ("a1", "a2")
    assert row.span_seconds == 50.0
    assert row.spend["claude-sonnet-5"].input == 3 + 1 + 40
    assert "    span=50s" in flow_usage.render([run]).splitlines()
    assert json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]["span_seconds"] == 50.0


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_only_the_readable_agents_span_beside_a_timed_one(tmp_path, how):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", STEP_ROW), _started("a2"), _result("a2", STEP_ROW)],
        transcripts={
            "a1": _responses_at([_at(0), _at(50)]),
            "a2": _responses_at([_at(9000)]),
        },
    )
    if how == "missing":
        (flow_run.run_dir / "agent-a2.jsonl").unlink()

    run = flow_usage.read_run(flow_run, _UnreadableTranscript("a2"))

    row = _only_row(run)
    assert row.agent_ids == ("a1", "a2") and row.span_seconds == 50.0
    assert flow_usage.coverage(run).unreadable_agent_transcripts == 1


def test_one_snapshot_supplies_spend_and_timestamps_and_counts_unreadable_lines_once(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", STEP_ROW)],
        transcripts={"a1": [_response(_at(0), "r1", input_tokens=5)]},
    )
    reads = []

    class Changing(flow_usage.Files):
        """The first physical read of the transcript is one thing, any later one another."""

        def read_lines(self, path):
            if path.name != "agent-a1.jsonl":
                return super().read_lines(path)
            reads.append(path.name)
            if len(reads) == 1:
                return [
                    _response(_at(0), "r1", input_tokens=5),
                    _response(_at(10), "r2", input_tokens=0),
                    "{not json",
                ]
            return [_response(_at(0), "r9", input_tokens=500), _response(_at(9999), "r8")]

    run = flow_usage.read_run(flow_run, Changing())

    assert reads == ["agent-a1.jsonl"]
    row = _only_row(run)
    assert row.span_seconds == 10.0
    assert row.spend["claude-sonnet-5"].input == 5
    (agent,) = run.agents
    assert agent.unreadable_lines == 1 and flow_usage.coverage(run).unreadable_lines == 1


def test_journal_sidecar_and_orphan_timestamps_do_not_affect_spans(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            json.dumps(
                {"type": "started", "agentId": "a1", "key": "v2:a1", "timestamp": _at(-9000)}
            ),
            json.dumps(
                {
                    "type": "result",
                    "agentId": "a1",
                    "key": "v2:a1",
                    "timestamp": _at(9000),
                    "result": STEP_ROW,
                }
            ),
        ],
        transcripts={
            "a1": _responses_at([_at(0), _at(5)]),
            "orphan": _responses_at([_at(-5000), _at(5000)]),
        },
        metas={"a1": {"agentType": "developer", "timestamp": _at(-4000)}},
    )

    run = flow_usage.read_run(flow_run, flow_usage.Files())
    assert _only_row(run).span_seconds == 5.0


def test_failed_or_unfinished_agents_keep_their_fallback_and_gain_the_span_their_transcript_holds(
    tmp_path,
):
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _started("a2"), _failed("a2"), _started("a3"), _failed("a3")],
        transcripts={
            "a1": _responses_at([_at(0), _at(11)]),
            "a2": _responses_at([_at(100), _at(130)]),
            "a3": [],
        },
        metas={"a1": {"agentType": "alpha"}, "a2": {"agentType": "beta"}},
    )

    rows = flow_usage.aggregate(flow_usage.read_run(flow_run, flow_usage.Files()))

    by_label = {row.label: row for row in rows}
    assert {label: row.is_fallback for label, row in by_label.items()} == {
        "alpha": True,
        "beta": True,
        "(unknown)": True,
    }
    assert by_label["alpha"].span_seconds == 11.0
    assert by_label["beta"].span_seconds == 30.0
    assert by_label["(unknown)"].span_seconds is None


def test_unreadable_journal_or_no_agents_creates_no_span_row(tmp_path):
    flow_run = _flow_run_fixture(tmp_path / "empty", [])
    no_agents = flow_usage.read_run(flow_run, flow_usage.Files())
    broken = _flow_run_fixture(tmp_path / "broken", [_started("a1")], {"a1": [_record(_at(0))]})
    _directory_in_place_of_journal(broken)
    unreadable = flow_usage.read_run(broken, flow_usage.Files())

    for run in (no_agents, unreadable):
        assert flow_usage.aggregate(run) == ()
        assert "span" not in flow_usage.render([run])
        assert json.loads(flow_usage.render_json([run]))["runs"][0]["rows"] == []


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        pytest.param(0, "span=0s", id="zero"),
        pytest.param(120, "span=120s", id="integral"),
        pytest.param(115.716, "span=115.716s", id="three-decimals"),
        pytest.param(1234.5678, "span=1234.57s", id="six-significant-digits"),
        pytest.param(1234567.8, "span=1.23457e+06s", id="exponent-form"),
        pytest.param(0.25, "span=0.25s", id="subsecond"),
    ],
)
def test_span_text_precision(tmp_path, seconds, text):
    lines = _responses_at([_at(0), _at(seconds)])
    run = _span_run(tmp_path, {"a1": lines})

    assert f"    {text}" in flow_usage.render([run]).splitlines()
    member = json.loads(flow_usage.render_json([run]))["runs"][0]["rows"][0]["span_seconds"]
    assert member == pytest.approx(seconds, abs=1e-6)
    assert member == _only_row(run).span_seconds


def test_typed_aggregate_row_span_equals_its_json_member(tmp_path):
    run = _span_run(
        tmp_path,
        {
            "a1": _responses_at([_at(0), _at(37.5)]),
            "a2": [_record(None)],
            "a3": _responses_at([_at(10)]),
        },
        results={"a2": "no step", "a3": {"step": "development-specs", "result": "findings"}},
    )

    typed = [row.span_seconds for row in flow_usage.aggregate(run)]
    members = [
        row["span_seconds"] for row in json.loads(flow_usage.render_json([run]))["runs"][0]["rows"]
    ]
    assert typed == members == [37.5, None, 0.0]


def test_typed_aggregate_span_equals_the_json_member_on_the_reference_rows(tmp_path):
    projects = tmp_path / "R"
    shutil.copytree(Path(__file__).parent / "parity" / "projects", projects)
    (flow_run,) = flow_usage.flow_runs(projects, flow_usage.Files())
    run = flow_usage.read_run(flow_run, flow_usage.Files())

    typed = [row.span_seconds for row in flow_usage.aggregate(run)]
    members = [
        row["span_seconds"] for row in json.loads(flow_usage.render_json([run]))["runs"][0]["rows"]
    ]
    assert typed == members
    assert typed == [
        113.727, 93.63, 366.182, 215.143, 118.955, 177.325, 144.245, 286.435, 86.566, 270.837,
    ]
    first = next(row for row in flow_usage.aggregate(run) if row.label == "development-plan")
    assert first.span_seconds == 113.727


def test_aggregate_row_and_agent_reading_constructors_without_the_new_arguments_still_work():
    row = flow_usage.AggregateRow(
        label="s", role=None, is_fallback=True, agent_ids=("a",), agent_types=("t",), spend={}
    )
    agent = flow_usage.AgentReading(
        agent_id="a", step=None, role=None, agent_type=None, model_hint=None
    )

    assert row.span_seconds is None and agent.timestamps == ()
    assert {f.name: f.kw_only for f in dataclasses.fields(flow_usage.AggregateRow)}[
        "span_seconds"
    ]
    assert {f.name: f.kw_only for f in dataclasses.fields(flow_usage.AgentReading)}["timestamps"]


def test_agent_spend_call_forms_and_counters_are_unchanged(tmp_path):
    _write_agent_transcript(tmp_path, "a1", [BAD_LINE, _usage_line(input_tokens=5)])
    counter = run_usage.UnreadableLines()

    assert flow_usage.agent_spend(tmp_path, "a1", flow_usage.Files())["claude-sonnet-5"].input == 5
    flow_usage.agent_spend(tmp_path, "a1", flow_usage.Files(), unreadable_lines=counter)
    assert counter.count == 1 and counter.unreadable_files == 0
    flow_usage.agent_spend(tmp_path, "nope", flow_usage.Files(), unreadable_lines=counter)
    assert counter.unreadable_files == 1
    parameters = inspect.signature(flow_usage.agent_spend).parameters
    assert list(parameters) == ["run_dir", "agent_id", "files", "unreadable_lines"]
    assert parameters["unreadable_lines"].kind is inspect.Parameter.KEYWORD_ONLY


# ── the one statement of what the time covers ──


def _lines_of(tmp_path, capsys, *extra):
    return _steps(tmp_path, capsys, *extra).splitlines()


def test_steps_text_prints_the_statement_once_as_the_second_line(tmp_path, capsys):
    _workflows(tmp_path, ("wf_a", "sid-1"), ("wf_b", "sid-2"), ("wf_c", "sid-3"))

    lines = _lines_of(tmp_path, capsys)

    assert lines[0] == FALLBACK_LINE and lines[1] == SPAN and lines[2] == ""
    assert lines.count(SPAN) == 1
    assert sum(line.startswith("wf_") for line in lines) == 3


def test_steps_statement_stays_clear_of_every_row_and_section_on_a_populated_run(
    tmp_path, capsys
):
    _mixed_run(tmp_path)

    lines = _lines_of(tmp_path, capsys)

    assert lines.count(SPAN) == 1 and lines.index(SPAN) == 1
    assert not any(line.startswith(" ") and SPAN in line for line in lines)
    assert lines[-1] == NO_COST


def test_steps_json_carries_top_level_span_equal_to_the_text_line(tmp_path, capsys):
    _mixed_run(tmp_path)

    payload = json.loads(_steps(tmp_path, capsys, "--json"))
    text = _lines_of(tmp_path, capsys)

    assert list(payload)[:3] == ["fallback", "span", "runs"]
    assert payload["span"] == text[1] == SPAN
    assert payload["fallback"] == FALLBACK_LINE
    assert [row["label"] for row in payload["runs"][0]["rows"]] == [
        "development-specs", "development-specs", "Explore", "(unknown)"
    ]


def test_both_reports_use_the_same_sentence(tmp_path, capsys):
    _workflows(tmp_path, ("wf_a", "sid-1"))
    steps_text = _lines_of(tmp_path, capsys)[1]
    steps_json = json.loads(_steps(tmp_path, capsys, "--json"))["span"]
    ledger = run_usage.Ledger(rows=(), coverage=run_usage.Coverage(0, 0, 0, ()))
    runs_text = run_usage.render(ledger).splitlines()[1]
    runs_json = json.loads(run_usage.render_json(ledger))["span"]

    assert {steps_text, steps_json, runs_text, runs_json} == {SPAN}


def _journal_lines_run(tmp_path, journal_lines, transcripts=None, metas=None):
    return flow_usage.read_run(
        _flow_run_fixture(tmp_path, journal_lines, transcripts, metas), flow_usage.Files()
    )


@pytest.mark.parametrize(
    "case",
    ["no-agents", "unreadable-journal-line", "missing-transcript", "unreadable-sidecar"],
)
def test_steps_statement_is_present_in_empty_and_diagnostic_reports(tmp_path, case):
    produced = [_started("a1"), _result("a1", PRODUCED)]
    run = {
        "no-agents": lambda: _journal_lines_run(tmp_path, []),
        "unreadable-journal-line": lambda: _journal_lines_run(
            tmp_path, ["not json {", *produced], {"a1": [_usage_line()]}
        ),
        "missing-transcript": lambda: _journal_lines_run(tmp_path, produced),
        "unreadable-sidecar": lambda: _journal_lines_run(
            tmp_path, produced, {"a1": [_usage_line()]}
        ),
    }[case]()
    if case == "unreadable-sidecar":
        _write_meta(run.flow_run.run_dir, "a1", "not json {")
        run = flow_usage.read_run(run.flow_run, flow_usage.Files())

    text = flow_usage.render([run]).splitlines()
    document = json.loads(flow_usage.render_json([run]))

    assert text[1] == SPAN and text.count(SPAN) == 1 and text[-1] == NO_COST
    assert list(document)[:3] == ["fallback", "span", "runs"] and document["span"] == SPAN


def test_steps_statement_is_present_with_no_runs_and_with_failed_directories_only():
    plain = flow_usage.render([]).splitlines()
    failed = flow_usage.render([], unreadable_directories=_failures("root")).splitlines()

    assert plain[:4] == [FALLBACK_LINE, SPAN, "", "No workflow runs read."]
    assert failed[:3] == [FALLBACK_LINE, SPAN, ""] and failed.count(SPAN) == 1
    assert STEPS_DIRECTORY_LINE.format(directory="root") in failed
    for directories in ((), _failures("root")):
        document = json.loads(flow_usage.render_json([], unreadable_directories=directories))
        assert list(document)[:3] == ["fallback", "span", "runs"] and document["span"] == SPAN


def test_steps_non_zero_exits_print_no_report_and_no_statement(tmp_path, capsys):
    _make_workflow_run(tmp_path, "slug-a", "sid-1", "wf_one")

    for argv in (
        ["--projects-root", str(tmp_path), "--wf", "wf_nope"],
        ["--projects-root", str(tmp_path), "--json", "--wf", "wf_nope"],
    ):
        assert flow_usage.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and SPAN not in captured.err

    with pytest.raises(SystemExit) as argument_error:
        flow_usage.main(["--no-such-option"])
    captured = capsys.readouterr()
    assert argument_error.value.code != 0
    assert captured.out == "" and SPAN not in captured.err


# ── A response found in more than one transcript is counted once, under its keeping row ──

MODEL = "claude-sonnet-5"
STEP_X = {"step": "step-x", "status": "ok", "artifact": "x.md"}
STEP_Y = {"step": "step-y", "status": "ok", "artifact": "y.md"}
STEP_X_AUDIT = {"step": "step-x", "result": "findings", "findings": 0}


def _answer(response_id, seconds, input_tokens, output_tokens, model=MODEL):
    """One response, stamped `seconds` after 10:00:00, carrying the usage given."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": _at(seconds),
            "message": {
                "id": response_id,
                "model": model,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        }
    )


def _shared_steps(tmp_path, agents):
    """One workflow run, `agents` being `(agent_id, step result, transcript lines)` in the order
    the journal names them, read as the library reads a whole report of it."""
    journal = []
    for agent_id, result, _lines in agents:
        journal += [_started(agent_id), _result(agent_id, result)]
    flow_run = _flow_run_fixture(
        tmp_path, journal, transcripts={agent_id: lines for agent_id, _r, lines in agents}
    )
    return flow_usage.read_selected_runs([flow_run], flow_usage.Files())[0]


def _workflow_runs(tmp_path, runs):
    """Several workflow runs under one projects root, `runs` being `(wf_id, [(agent_id, step
    result, transcript lines)])`, and the projects root's reading of them, in report order."""
    for number, (wf_id, agents) in enumerate(runs):
        run_dir = _make_workflow_run(tmp_path, "slug-a", f"sid-{number}", wf_id)
        journal = []
        for agent_id, result, lines in agents:
            journal += [_started(agent_id), _result(agent_id, result)]
            _write_agent_transcript(run_dir, agent_id, lines)
        (run_dir / "journal.jsonl").write_text("\n".join(journal))
    reader = flow_usage.Reader(files=flow_usage.Files(), projects_root=tmp_path)
    return flow_usage.read_steps(reader).runs


def _spent(row):
    spend = row.spend[MODEL] if isinstance(row, flow_usage.AggregateRow) else row[MODEL]
    return spend.input, spend.output


def _example_four():
    return [
        ("a1", STEP_X, [_answer("msg_1", 0, 50, 5), _answer("msg_shared", 10, 7, 3)]),
        ("a2", STEP_Y, [_answer("msg_shared", 10, 7, 3), _answer("msg_2", 120, 60, 6)]),
    ]


def _coverage_line_of(text, wf_id):
    return next(line for line in _run_section(text, wf_id) if line.startswith("  Coverage:"))


def test_steps_count_a_shared_response_under_the_first_printed_step_row(tmp_path, capsys):
    run = _shared_steps(tmp_path, _example_four())

    rows = flow_usage.aggregate(run)
    assert [(row.label, _spent(row), row.span_seconds) for row in rows] == [
        ("step-x", (57, 8), 10.0),
        ("step-y", (60, 6), 0.0),
    ]
    text = _steps(tmp_path, capsys)
    assert "input=57 output=8" in text and "input=60 output=6" in text
    assert "input=67" not in text
    payload = json.loads(_steps(tmp_path, capsys, "--json"))["runs"][0]
    assert [(r["spend"][MODEL]["input"], r["span_seconds"]) for r in payload["rows"]] == [
        (57, 10.0),
        (60, 0.0),
    ]


def test_steps_coverage_line_and_json_member_state_the_section_count(tmp_path, capsys):
    _shared_steps(tmp_path, _example_four())

    line = _coverage_line_of(_steps(tmp_path, capsys), "wf_test")
    payload = json.loads(_steps(tmp_path, capsys, "--json"))["runs"][0]
    assert line.endswith(f" — {run_usage.SHARED_RESPONSES_ONE}")
    assert payload["coverage"]["responses_in_more_than_one_transcript"] == 1
    assert type(payload["coverage"]["responses_in_more_than_one_transcript"]) is int


def test_steps_clause_and_member_are_absent_at_zero(tmp_path, capsys):
    run = _shared_steps(
        tmp_path,
        [("a1", STEP_X, [_answer("m1", 0, 1, 1)]), ("a2", STEP_Y, [_answer("m2", 5, 2, 2)])],
    )

    line = _coverage_line_of(_steps(tmp_path, capsys), "wf_test")
    payload = json.loads(_steps(tmp_path, capsys, "--json"))["runs"][0]
    assert "more than one of the transcripts" not in line
    assert "responses_in_more_than_one_transcript" not in payload["coverage"]
    assert type(flow_usage.coverage(run)) is flow_usage.Coverage
    assert flow_usage.coverage(run).responses_in_more_than_one_transcript == 0


def test_steps_keeping_row_follows_printed_group_order_not_agent_id_order(tmp_path, capsys):
    run = _shared_steps(
        tmp_path,
        [
            ("a1", STEP_X, [_answer("m1", 0, 10, 1)]),
            ("a2", STEP_Y, [_answer("msg_s", 20, 5, 5), _answer("m2", 30, 100, 1)]),
            ("a3", STEP_X, [_answer("msg_s", 20, 5, 5)]),
        ],
    )

    x_row, y_row = flow_usage.aggregate(run)
    assert (x_row.label, x_row.agent_ids, _spent(x_row)) == ("step-x", ("a1", "a3"), (15, 6))
    assert (y_row.label, y_row.agent_ids, _spent(y_row)) == ("step-y", ("a2",), (100, 1))
    assert [agent.agent_id for agent in run.agents] == ["a1", "a2", "a3"]
    printed = [line for line in flow_usage.render([run]).splitlines() if "agents=" in line]
    assert [line.split("agents=")[1] for line in printed] == ["a1,a3", "a2"]


def test_steps_copies_with_different_usage_model_and_timestamp_count_the_first_record_in_header_agent_order(  # noqa: E501
    tmp_path,
):
    run = _shared_steps(
        tmp_path,
        [
            ("a1", STEP_X, [_answer("msg_s", 0, 7, 3)]),
            ("a2", STEP_Y, [_answer("msg_s", 500, 90, 40, model="other-model")]),
        ],
    )

    x_row, y_row = flow_usage.aggregate(run)
    assert set(x_row.spend) == {MODEL} and _spent(x_row) == (7, 3)
    assert y_row.spend == {} and y_row.span_seconds is None


def test_steps_two_agents_of_one_row_sharing_an_id_count_it_once(tmp_path):
    run = _shared_steps(
        tmp_path,
        [
            ("a1", STEP_X, [_answer("msg_s", 0, 7, 3)]),
            ("a2", STEP_X, [_answer("msg_s", 0, 7, 3)]),
        ],
    )

    (row,) = flow_usage.aggregate(run)
    assert _spent(row) == (7, 3)
    assert flow_usage.coverage(run).responses_in_more_than_one_transcript == 1


def _two_sections(tmp_path):
    return _workflow_runs(
        tmp_path,
        [
            ("wf_a", [("a1", STEP_X, [_answer("m_a", 0, 1, 1), _answer("msg_s", 5, 7, 3)])]),
            ("wf_b", [("b1", STEP_X, [_answer("msg_s", 5, 7, 3), _answer("m_b", 9, 2, 2)])]),
        ],
    )


def test_steps_a_response_shared_by_two_workflow_runs_is_kept_by_the_first_section(
    tmp_path, capsys
):
    runs = _two_sections(tmp_path)

    # The earlier section's count is only known once the later transcript has been read.
    assert [r.responses_in_more_than_one_transcript for r in runs] == [1, 0]
    text = _steps(tmp_path, capsys)
    assert _coverage_line_of(text, "wf_a").endswith(run_usage.SHARED_RESPONSES_ONE)
    assert "more than one of the transcripts" not in _coverage_line_of(text, "wf_b")
    payload = json.loads(_steps(tmp_path, capsys, "--json"))["runs"]
    assert payload[0]["coverage"]["responses_in_more_than_one_transcript"] == 1
    assert "responses_in_more_than_one_transcript" not in payload[1]["coverage"]
    kept = [flow_usage.aggregate(run)[0] for run in runs]
    assert [_spent(row) for row in kept] == [(8, 4), (2, 2)]


def test_steps_wf_selecting_one_section_counts_locally_with_n_zero(tmp_path, capsys):
    _two_sections(tmp_path)

    only_b = json.loads(_steps(tmp_path, capsys, "--wf", "wf_b", "--json"))["runs"]
    only_a = json.loads(_steps(tmp_path, capsys, "--wf", "wf_a", "--json"))["runs"]

    assert len(only_b) == 1 and len(only_a) == 1
    assert only_b[0]["rows"][0]["spend"][MODEL]["input"] == 9
    assert only_a[0]["rows"][0]["spend"][MODEL]["input"] == 8
    for (run,) in (only_b, only_a):
        assert "responses_in_more_than_one_transcript" not in run["coverage"]
    assert "more than one of the transcripts" not in _steps(tmp_path, capsys, "--wf", "wf_b")


def test_steps_section_counts_add_up_to_the_report_total(tmp_path, capsys):
    runs = _workflow_runs(
        tmp_path,
        [
            ("wf_a", [("a1", STEP_X, [_answer("s1", 0, 1, 1), _answer("s2", 1, 1, 1)])]),
            ("wf_b", [("b1", STEP_X, [_answer("s1", 0, 1, 1), _answer("s2", 1, 1, 1)])]),
            ("wf_c", [("c1", STEP_X, [_answer("s1", 0, 1, 1)])]),
        ],
    )

    assert [r.responses_in_more_than_one_transcript for r in runs] == [2, 0, 0]
    text = _steps(tmp_path, capsys)
    assert _coverage_line_of(text, "wf_a").endswith(
        run_usage.shared_responses_clause(2)
    ) and "2 responses found in more than one" in text


def test_steps_an_id_in_three_files_is_counted_once_in_one_section(tmp_path):
    runs = _workflow_runs(
        tmp_path,
        [
            ("wf_a", [("a1", STEP_X, [_answer("s1", 0, 4, 1)])]),
            ("wf_b", [("b1", STEP_X, [_answer("s1", 0, 4, 1)])]),
            ("wf_c", [("c1", STEP_X, [_answer("s1", 0, 4, 1)])]),
        ],
    )

    assert [r.responses_in_more_than_one_transcript for r in runs] == [1, 0, 0]
    assert [bool(flow_usage.aggregate(r)[0].spend) for r in runs] == [True, False, False]


def test_steps_fallback_producer_auditor_unfinished_and_failed_rows_keep_their_identity_and_coverage_counters(  # noqa: E501
    tmp_path,
):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"), _result("a1", STEP_X),
            _started("a2"), _result("a2", STEP_X_AUDIT),
            _started("a3"), _result("a3", "no step"),
            _started("a4"),
            _started("a5"), _failed("a5"),
        ],
        transcripts={
            "a1": [_answer("msg_s", 0, 1, 1)],
            "a2": [_answer("msg_s", 0, 1, 1)],
            "a3": [_answer("msg_s", 0, 1, 1)],
            "a4": [_answer("msg_s", 0, 1, 1)],
            "a5": [_answer("msg_s", 0, 1, 1)],
        },
        metas={"a3": {"agentType": "alpha"}, "a4": {"agentType": "alpha"}},
    )

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    rows = flow_usage.aggregate(run)
    assert [(r.label, r.role, r.is_fallback, r.agent_ids) for r in rows] == [
        ("step-x", flow_usage.Role.PRODUCE, False, ("a1",)),
        ("step-x", flow_usage.Role.AUDIT, False, ("a2",)),
        ("alpha", None, True, ("a3", "a4")),
        ("(unknown)", None, True, ("a5",)),
    ]
    assert [bool(r.spend) for r in rows] == [True, False, False, False]
    cov = flow_usage.coverage(run)
    assert (cov.with_step, cov.without_step, cov.started_no_result) == (2, 3, 1)
    assert cov.responses_in_more_than_one_transcript == 1


def test_steps_losing_rows_stay_with_empty_spend_and_null_span(tmp_path, capsys):
    run = _shared_steps(
        tmp_path,
        [
            ("a1", STEP_X, [_answer("msg_s", 0, 7, 3)]),
            ("a2", STEP_Y, [_answer("msg_s", 0, 7, 3)]),
        ],
    )

    assert [(r.label, r.spend, r.span_seconds) for r in flow_usage.aggregate(run)][1] == (
        "step-y", {}, None
    )
    lines = _steps(tmp_path, capsys).splitlines()
    header = next(n for n, line in enumerate(lines) if line.startswith("  step-y"))
    assert lines[header + 1] == "    span=n/a"
    assert not lines[header + 2].startswith("    ")
    rows = json.loads(_steps(tmp_path, capsys, "--json"))["runs"][0]["rows"]
    assert len(rows) == 2 and rows[1]["spend"] == {} and rows[1]["span_seconds"] is None


def test_steps_zero_responses_only_noid_responses_and_a_single_kept_timestamp(tmp_path):
    no_id = [_answer(None, 0, 2, 2), _answer(None, 9, 3, 3)]
    run = _shared_steps(
        tmp_path,
        [
            ("a1", STEP_X, []),
            ("a2", STEP_Y, no_id),
            ("a3", {"step": "step-z", "status": "ok", "artifact": "z"}, [_answer("only", 4, 1, 1)]),
        ],
    )

    zero, noid, single = flow_usage.aggregate(run)
    assert (zero.spend, zero.span_seconds) == ({}, None)
    assert (_spent(noid), noid.span_seconds) == ((5, 5), 9.0)
    assert (_spent(single), single.span_seconds) == ((1, 1), 0.0)
    assert flow_usage.coverage(run).responses_in_more_than_one_transcript == 0


def test_steps_a_report_reading_one_transcript_counts_the_response_there(tmp_path):
    malformed = json.loads(_answer("msg_s", 0, 7, 3))
    malformed["message"]["usage"]["input_tokens"] = "seven"
    usage_free = json.loads(_answer("msg_s", 0, 7, 3))
    usage_free["message"]["usage"] = {}
    run_dir_lines = {
        "a1": [_answer("msg_s", 0, 7, 3)],
        "a2": [json.dumps(malformed)],
        "a3": [json.dumps(usage_free)],
    }
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started(a) + "\n" + _result(a, STEP_X) for a in ("a1", "a2", "a3", "a4")],
        transcripts=run_dir_lines,
    )
    # an agent the journal names whose transcript is absent, and a transcript nothing names
    _write_agent_transcript(flow_run.run_dir, "orphan", [_answer("msg_s", 0, 7, 3)])

    run = flow_usage.read_run(flow_run, flow_usage.Files())

    (row,) = flow_usage.aggregate(run)
    assert _spent(row) == (7, 3)
    assert run.orphan_agent_files == ("orphan",)
    assert flow_usage.coverage(run).responses_in_more_than_one_transcript == 0
    assert flow_usage.coverage(run).unreadable_lines == 1


def test_steps_a_counterpart_found_and_not_openable_makes_nothing_shared(tmp_path):
    # `a1` would keep `msg_s`, but its transcript cannot be opened: it is not a transcript the
    # report read, so `a2` counts the response and nothing is shared (CP-579)
    flow_run = _flow_run_fixture(
        tmp_path,
        [_started("a1"), _result("a1", STEP_X), _started("a2"), _result("a2", STEP_Y)],
        transcripts={
            "a1": [_answer("msg_s", 0, 7, 3)],
            "a2": [_answer("msg_s", 0, 7, 3), _answer("m2", 5, 1, 1)],
        },
    )
    blocked = flow_run.run_dir / "agent-a1.jsonl"

    class _BlockedFiles(flow_usage.Files):
        def read_lines(self, path):
            return None if path == blocked else super().read_lines(path)

    run = flow_usage.read_selected_runs([flow_run], _BlockedFiles())[0]

    x_row, y_row = flow_usage.aggregate(run)
    assert (x_row.label, x_row.spend) == ("step-x", {})
    assert (y_row.label, _spent(y_row), y_row.span_seconds) == ("step-y", (8, 4), 5.0)
    cov = flow_usage.coverage(run)
    assert cov.unreadable_agent_transcripts == 1
    assert type(cov) is flow_usage.Coverage
    assert cov.responses_in_more_than_one_transcript == 0


def test_steps_aggregate_and_coverage_are_pure_repeatable_reads(tmp_path):
    run = _shared_steps(tmp_path, _example_four())
    flow_run = run.flow_run
    reader = flow_usage.Reader(files=flow_usage.Files(), projects_root=tmp_path)

    assert flow_usage.aggregate(run) == flow_usage.aggregate(run)
    assert flow_usage.coverage(run) == flow_usage.coverage(run)
    assert flow_usage.read_steps(reader).runs == (run,)
    assert flow_usage.read_run(flow_run, flow_usage.Files()) == run
    positional = flow_usage.RunReading(flow_run, (), (), 2, 1)
    assert (positional.unreadable_journal_lines, positional.unreadable_journal_files) == (2, 1)
    assert positional.responses_in_more_than_one_transcript == 0
    built = flow_usage.Coverage(1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert (built.unreadable_lines, built.unreadable_journal_files) == (5, 9)
    assert not any(
        isinstance(value, run_usage.ResponseScope)
        for value in (*dataclasses.astuple(run), *run.agents)
    )


class _TranscriptReadLog(flow_usage.Files):
    def __init__(self):
        self.reads = []

    def read_lines(self, path):
        self.reads.append(path.name)
        return super().read_lines(path)


def test_steps_each_agent_transcript_is_read_once(tmp_path):
    flow_run = _flow_run_fixture(
        tmp_path,
        [
            _started("a1"), _result("a1", STEP_X),
            _started("a2"), _result("a2", STEP_Y),
            _started("a3"), _result("a3", STEP_X),
        ],
        transcripts={
            "a1": [_answer("m1", 0, 1, 1)],
            "a2": [_answer("msg_s", 0, 1, 1)],
            "a3": [_answer("msg_s", 0, 1, 1)],
        },
    )
    files = _TranscriptReadLog()

    run = flow_usage.read_selected_runs([flow_run], files)[0]

    assert sorted(files.reads) == ["agent-a1.jsonl", "agent-a2.jsonl", "agent-a3.jsonl"]
    assert [agent.agent_id for agent in run.agents] == ["a1", "a2", "a3"]


def test_steps_reference_outputs_are_byte_identical(capsys):
    parity = Path(__file__).parent / "parity"

    for extra, stored in (((), "steps.txt"), (("--json",), "steps.json")):
        out = _steps(parity / "projects", capsys, *extra)
        assert out == (parity / "expected" / stored).read_text()
