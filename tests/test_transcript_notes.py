"""Tests for `ka_telemetry.transcript_notes`: the optional stream match lookup and the pure note
block the two transcript reports end with.

The lookup is held to the one rule that decides whether a pointer prints: a session matches when
`ka-telemetry agents --where session.id=<id>` would show at least one row for it. Every stream
failure is a "no match", never an exception and never a line on stderr. The reader's file port is a
hand-written fake handed to the reader, so no test patches the module under test.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from ka_telemetry import stream_usage, transcript_notes

STREAM_DIRECTORY = Path("/stream")
LOGS_PATH = STREAM_DIRECTORY / "logs.jsonl"
SESSION_A = "a839285c-263f-40a6-8801-71f49d00cc68"
SESSION_B = "174d0111-bfa9-4539-92d9-5560e0ab4c36"
SESSION_C = "15b8016a-f529-44fe-b6ea-1013fea8a4a8"
NO_COST_LINE = (
    "This report carries no cost, because ka-telemetry never computes a cost from tokens."
)


def _pointer(session_id: str) -> str:
    return (
        f"Run `ka-telemetry agents --where session.id={session_id}` to see Claude Code's cost "
        f"estimate for session {session_id}."
    )


def _log_line(session_id: str, *, event: str = "api_request", cost: float = 0.5) -> str:
    """One `logs.jsonl` line holding a single log record of `event` for `session_id`."""

    def attribute(key: str, value: dict) -> dict:
        return {"key": key, "value": value}

    record = {
        "attributes": [
            attribute("session.id", {"stringValue": session_id}),
            attribute("event.name", {"stringValue": event}),
            attribute("cost_usd", {"doubleValue": cost}),
        ]
    }
    scope = {"logRecords": [record]}
    body = {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [scope]}]}
    return json.dumps({"body": body})


class FakeStreamFiles:
    """The stream reader's file port: answers `read_outcome` with what it was built with and
    records every path it was asked for."""

    def __init__(self, lines=(), *, state=stream_usage.ReadState.READ, error=None):
        self._outcome = stream_usage.ReadOutcome(state=state, lines=tuple(lines))
        self._error = error
        self.read_paths: list[Path] = []

    def read_outcome(self, path: Path) -> stream_usage.ReadOutcome:
        self.read_paths.append(path)
        if self._error is not None:
            raise self._error
        return self._outcome


def _reader(files: FakeStreamFiles) -> stream_usage.Reader:
    return stream_usage.Reader(files=files, directory=STREAM_DIRECTORY)


def _matched(session_ids, *lines, **files_options):
    files = FakeStreamFiles(lines, **files_options)
    return transcript_notes.matched_sessions(session_ids, _reader(files)), files


# ── matched_sessions: what counts as a match ─────────────────────────────────


def test_match_is_an_agent_row_for_the_session():
    matched, _ = _matched([SESSION_A], _log_line(SESSION_A))

    assert matched == (SESSION_A,)


def test_match_uses_the_filter_agents_where_uses_and_no_prefix():
    reader_lines = (_log_line(SESSION_A),)
    records = stream_usage.read_log_lines(list(reader_lines)).records

    selected = stream_usage.agent_rows(records, (("session.id", SESSION_A),))
    matched, _ = _matched([SESSION_A], *reader_lines)

    assert bool(selected) is True
    assert matched == (SESSION_A,)


def test_zero_cost_api_request_still_matches():
    matched, _ = _matched([SESSION_A], _log_line(SESSION_A, cost=0))

    assert matched == (SESSION_A,)


def test_session_with_only_a_completed_agent_event_does_not_match():
    matched, _ = _matched([SESSION_A], _log_line(SESSION_A, event="subagent_completed"))

    assert matched == ()


def test_another_sessions_rows_do_not_match():
    matched, _ = _matched([SESSION_A], _log_line(SESSION_B))

    assert matched == ()


def test_a_session_id_that_only_contains_the_asked_one_does_not_match():
    matched, _ = _matched([SESSION_A], _log_line(SESSION_A + "-extra"))

    assert matched == ()


def test_matches_keep_input_order_and_repeat_once():
    matched, _ = _matched(
        [SESSION_A, SESSION_B, SESSION_A],
        _log_line(SESSION_B, cost=9.0),
        _log_line(SESSION_A, cost=0.1),
    )

    assert matched == (SESSION_A, SESSION_B)


def test_a_session_asked_for_twice_and_unmatched_is_not_returned():
    matched, _ = _matched([SESSION_C, SESSION_A, SESSION_C], _log_line(SESSION_A))

    assert matched == (SESSION_A,)


def test_only_the_sessions_asked_for_are_checked():
    matched, _ = _matched(
        [SESSION_A], _log_line(SESSION_A), _log_line(SESSION_B), _log_line(SESSION_C)
    )

    assert matched == (SESSION_A,)


# ── matched_sessions: a stream that is not there, or not readable, is no match ─


def test_absent_directory_absent_or_empty_logs_yield_no_matches(capsys):
    for state in (stream_usage.ReadState.NOTHING_RECEIVED, stream_usage.ReadState.READ):
        matched, _ = _matched([SESSION_A], state=state)

        assert matched == ()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_logs_holding_only_non_json_lines_yield_no_matches():
    matched, _ = _matched([SESSION_A], "not json {", "also not json")

    assert matched == ()


def test_mixed_valid_and_bad_lines_match_on_the_valid_ones():
    matched, _ = _matched(
        [SESSION_A, SESSION_B], "not json {", _log_line(SESSION_A), "[1, 2]", _log_line(SESSION_C)
    )

    assert matched == (SESSION_A,)


@pytest.mark.parametrize(
    "state", [stream_usage.ReadState.UNOPENABLE, stream_usage.ReadState.UNINSPECTABLE]
)
def test_unopenable_logs_yield_no_matches(state, capsys):
    matched, _ = _matched([SESSION_A], state=state)

    assert matched == ()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_a_file_port_that_raises_a_permission_error_yields_no_matches(capsys):
    matched, _ = _matched([SESSION_A], error=PermissionError(13, "Permission denied"))

    assert matched == ()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_malformed_nested_stream_shapes_yield_no_matches_silently(capsys):
    # A line the decoder cannot even strip: the failure is inside the optional lookup, so it
    # is swallowed there rather than reaching the report.
    files = FakeStreamFiles((5,))

    matched = transcript_notes.matched_sessions([SESSION_A], _reader(files))

    assert matched == ()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_an_attribute_list_of_the_wrong_type_yields_no_matches_silently(capsys):
    scope = {"logRecords": [{"attributes": 3}]}
    broken = json.dumps({"body": {"resourceLogs": [{"scopeLogs": [scope]}]}})

    matched, _ = _matched([SESSION_A], broken, _log_line(SESSION_A))

    assert matched == ()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_nested_shapes_of_the_wrong_type_are_walked_past():
    wrong_shapes = [
        json.dumps({"body": {"resourceLogs": "x"}}),
        json.dumps({"body": {"resourceLogs": [1, {"scopeLogs": [None, {"logRecords": "y"}]}]}}),
    ]

    matched, _ = _matched([SESSION_A], *wrong_shapes, _log_line(SESSION_A))

    assert matched == (SESSION_A,)


# ── matched_sessions: what the lookup reads and starts ────────────────────────


def test_logs_are_read_once_for_many_sessions():
    files = FakeStreamFiles([_log_line(SESSION_A), _log_line(SESSION_B)])

    transcript_notes.matched_sessions([SESSION_A, SESSION_B, SESSION_C], _reader(files))

    assert files.read_paths == [LOGS_PATH]


def test_empty_session_list_does_no_read():
    files = FakeStreamFiles([_log_line(SESSION_A)])

    matched = transcript_notes.matched_sessions([], _reader(files))

    assert (matched, files.read_paths) == ((), [])


def test_lookup_consults_no_metrics_and_starts_no_process(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the lookup started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    files = FakeStreamFiles([_log_line(SESSION_A)])

    matched = transcript_notes.matched_sessions([SESSION_A], _reader(files))

    assert matched == (SESSION_A,)
    assert [p.name for p in files.read_paths] == ["logs.jsonl"]


def test_lookup_accepts_any_iterable_of_ids():
    matched, _ = _matched(iter([SESSION_B, SESSION_A]), _log_line(SESSION_A), _log_line(SESSION_B))

    assert matched == (SESSION_B, SESSION_A)


# ── note_lines: the pure block ────────────────────────────────────────────────


def test_note_lines_is_one_no_cost_line_then_one_pointer_per_id_in_order():
    lines = transcript_notes.note_lines([SESSION_B, SESSION_A])

    assert lines == (NO_COST_LINE, _pointer(SESSION_B), _pointer(SESSION_A))
    assert sum("carries no cost" in line for line in lines) == 1
    assert lines[1].count(f"ka-telemetry agents --where session.id={SESSION_B}") == 1


def test_no_cost_line_names_neither_the_command_nor_a_session_id():
    no_cost = transcript_notes.note_lines([SESSION_A])[0]

    assert "ka-telemetry agents" not in no_cost
    assert SESSION_A not in no_cost


def test_note_lines_with_no_ids_is_the_no_cost_line_alone():
    assert transcript_notes.note_lines([]) == (NO_COST_LINE,)


def test_note_lines_does_no_io_and_has_no_trailing_newline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("note_lines touched the filesystem")

    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)

    lines = transcript_notes.note_lines([SESSION_A, SESSION_B])

    assert all(not line.endswith("\n") and "\n" not in line for line in lines)
    assert isinstance(lines, tuple)


def test_append_notes_puts_the_block_directly_under_the_report_with_one_final_newline():
    joined = transcript_notes.append_notes("first\nlast\n\n", [SESSION_A])

    assert joined == "\n".join(["first", "last", NO_COST_LINE, _pointer(SESSION_A)]) + "\n"
    assert transcript_notes.append_notes("", []) == "\n" + NO_COST_LINE + "\n"


# ── The sentence both transcript reports print about what their time covers ──


def test_span_line_is_one_unindented_line():
    line = transcript_notes.SPAN_LINE

    assert isinstance(line, str)
    assert line != ""
    assert "\n" not in line and "\r" not in line
    assert line == line.strip()


def test_span_line_matches_first_last_response_pattern():
    assert re.search(r"first .*last .*response", transcript_notes.SPAN_LINE, re.IGNORECASE)


@pytest.mark.parametrize("word", ["sum", "total", "duration", "cost"])
def test_span_line_does_not_claim_a_sum_total_duration_or_cost(word):
    assert word not in transcript_notes.SPAN_LINE.lower()


def test_span_line_claims_no_time_before_the_first_response():
    lowered = transcript_notes.SPAN_LINE.lower()

    # The span runs from the first counted response to the last, in that order, and the sentence
    # words no earlier start: nothing says it covers time "before" or "prior to" the first response.
    assert lowered.index("first") < lowered.index("last") < lowered.index("response")
    for earlier_start in ("before the first", "prior to", "since the start", "including the time"):
        assert earlier_start not in lowered


def test_note_lines_and_append_notes_are_unchanged_by_the_constant():
    assert transcript_notes.note_lines([SESSION_A]) == (NO_COST_LINE, _pointer(SESSION_A))
    assert transcript_notes.SPAN_LINE not in transcript_notes.note_lines([SESSION_A, SESSION_B])
    assert transcript_notes.append_notes("report", [SESSION_A]) == (
        "report\n" + NO_COST_LINE + "\n" + _pointer(SESSION_A) + "\n"
    )
