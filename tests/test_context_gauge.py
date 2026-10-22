"""Tests for `ka_telemetry.context_gauge` — the current context of one transcript.

The fixtures are shaped off the transcript records `run_usage.usage_records` already reads: an
`assistant` record whose `message` carries an `id` and a `usage` block. The gauge is handed the
same `Files` port `run_usage` defines, over a real file under `tmp_path`.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest
from conftest import PRODUCT_ROOT

from ka_telemetry import context_gauge, run_usage


def _response(message_id: str, *, input_tokens=0, cache_read=0, cache_creation=0) -> dict:
    usage = {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": 999,
    }
    return {"type": "assistant", "message": {"id": message_id, "usage": usage}}


def _write(path: Path, records: list[dict], *, tail: str = "") -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records) + tail)
    return path


def test_last_distinct_response_sets_the_size(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _response("m1", input_tokens=10, cache_read=100, cache_creation=1000),
            _response("m2", input_tokens=20, cache_read=200, cache_creation=2000),
            _response("m3", input_tokens=30, cache_read=300, cache_creation=3000),
        ],
    )
    assert context_gauge.current_context(transcript, run_usage.Files()) == 3330


def test_a_response_split_across_records_is_read_once(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _response("m1", input_tokens=5, cache_read=50, cache_creation=500),
            _response("m2", input_tokens=7, cache_read=70, cache_creation=700),
            _response("m2", input_tokens=7, cache_read=70, cache_creation=700),
            _response("m2", input_tokens=7, cache_read=70, cache_creation=700),
        ],
    )
    assert context_gauge.current_context(transcript, run_usage.Files()) == 777


def test_missing_class_counts_as_zero(tmp_path):
    record = {
        "type": "assistant",
        "message": {"id": "m1", "usage": {"input_tokens": 12, "cache_read_input_tokens": 34}},
    }
    transcript = _write(tmp_path / "t.jsonl", [record])
    assert context_gauge.current_context(transcript, run_usage.Files()) == 46


def test_absent_or_unreadable_file_gives_none(tmp_path):
    assert context_gauge.current_context(tmp_path / "absent.jsonl", run_usage.Files()) is None
    # A path that exists but cannot be read as a file is the same answer, never an exception.
    assert context_gauge.current_context(tmp_path, run_usage.Files()) is None


def test_no_assistant_usage_gives_none(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [{"type": "user", "message": {"content": "hi"}}, {"type": "assistant", "message": {}}],
    )
    assert context_gauge.current_context(transcript, run_usage.Files()) is None


def test_partial_final_line_is_skipped(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [_response("m1", input_tokens=1, cache_read=2, cache_creation=3)],
        tail='{"type": "assistant", "message": {"id": "m2", "usage": {"input_to',
    )
    assert context_gauge.current_context(transcript, run_usage.Files()) == 6


def test_a_null_class_counts_as_zero(tmp_path):
    record = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "usage": {
                "input_tokens": 5,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": 7,
            },
        },
    }
    transcript = _write(tmp_path / "t.jsonl", [record])
    assert context_gauge.current_context(transcript, run_usage.Files()) == 12


def test_records_without_an_id_each_count(tmp_path):
    """No id means no identity to compare, so `run_usage` keeps both and the newer one is the
    current context — a first-wins de-duplication would read the older figure."""
    older = {"type": "assistant", "message": {"usage": {"input_tokens": 10}}}
    newer = {"type": "assistant", "message": {"usage": {"input_tokens": 30}}}
    transcript = _write(tmp_path / "t.jsonl", [older, newer])
    assert context_gauge.current_context(transcript, run_usage.Files()) == 30


def test_all_three_classes_absent_gives_zero_when_usage_is_present(tmp_path):
    record = {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5}}}
    transcript = _write(tmp_path / "t.jsonl", [record])
    assert context_gauge.current_context(transcript, run_usage.Files()) == 0


def test_output_tokens_are_never_part_of_the_size(tmp_path):
    record = _response("m1", input_tokens=1, cache_read=2, cache_creation=3)
    record["message"]["usage"]["output_tokens"] = 100000
    transcript = _write(tmp_path / "t.jsonl", [record])
    assert context_gauge.current_context(transcript, run_usage.Files()) == 6


def test_repeated_non_adjacent_response_id_is_one_response(tmp_path):
    """m1 comes back after m2. The repeat is not a new response, so the last distinct response in
    file order is still m2 — counting the repeat would read m1's figure."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _response("m1", input_tokens=10),
            _response("m2", input_tokens=20),
            _response("m1", input_tokens=10),
        ],
    )
    assert context_gauge.current_context(transcript, run_usage.Files()) == 20


# ── `read_context` — the size and what could not be read, from one read of the transcript ──

PARITY = Path(__file__).resolve().parent / "parity"
REFERENCE_TRANSCRIPT = (
    PARITY
    / "projects"
    / "-home-user-work-demo"
    / "a839285c-263f-40a6-8801-71f49d00cc68.jsonl"
)
BAD_JSON = "not json {"
NOT_AN_OBJECT = "[1, 2]"


def _reference(tmp_path: Path, *extra_lines: str) -> Path:
    """A copy of the reference transcript, `extra_lines` appended."""
    copy = tmp_path / "t.jsonl"
    shutil.copyfile(REFERENCE_TRANSCRIPT, copy)
    text = copy.read_text()
    separator = "" if text.endswith("\n") else "\n"
    copy.write_text(text + separator + "".join(f"{line}\n" for line in extra_lines))
    return copy


def _rich(path, files=None):
    return context_gauge.read_context(
        path, files or run_usage.Files(), size_reader=context_gauge.current_context
    )


def _triple(reading):
    return (reading.context_size, reading.unreadable_lines, reading.unreadable_transcripts)


def test_clean_reference_transcript_has_size_34224_and_zero_counts(tmp_path):
    assert _triple(_rich(_reference(tmp_path))) == (34224, 0, 0)


def test_appended_invalid_json_and_non_object_keep_the_size_and_count_two(tmp_path):
    transcript = _reference(tmp_path, BAD_JSON, NOT_AN_OBJECT)

    assert _triple(_rich(transcript)) == (34224, 2, 0)


def test_only_invalid_json_gives_unknown_size_and_count_one(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(BAD_JSON + "\n")

    assert _triple(_rich(transcript)) == (None, 1, 0)


@pytest.mark.parametrize(
    "text",
    ["", "\n\n", json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
     json.dumps({"type": "assistant", "message": {}}) + "\n"],
    ids=["empty", "blank-lines", "ordinary-record", "no-usage"],
)
def test_readable_empty_ordinary_and_no_usage_files_are_unknown_with_zero_file_failures(
    tmp_path, text
):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(text)

    assert _triple(_rich(transcript)) == (None, 0, 0)


def test_readable_all_rejected_file_is_unknown_with_its_line_count(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(f"{BAD_JSON}\n{NOT_AN_OBJECT}\n{BAD_JSON}\n")

    assert _triple(_rich(transcript)) == (None, 3, 0)


def test_directory_is_one_unreadable_transcript(tmp_path):
    assert _triple(_rich(tmp_path)) == (None, 0, 1)


def test_absent_and_path_under_a_regular_file_are_not_unreadable_transcripts(tmp_path):
    regular = tmp_path / "regular"
    regular.write_text("x")

    assert _triple(_rich(tmp_path / "absent.jsonl")) == (None, 0, 0)
    assert _triple(_rich(regular / "x.jsonl")) == (None, 0, 0)


class _ScriptedFiles:
    """A port that answers `read_outcome` with `outcome` and records every call it receives, so
    a test asserts what was asked of the filesystem without a filesystem."""

    def __init__(self, outcome: run_usage.TranscriptRead):
        self._outcome = outcome
        self.calls: list[tuple[str, Path]] = []

    def read_outcome(self, path):
        self.calls.append(("read_outcome", path))
        return self._outcome

    def read_lines(self, path):
        self.calls.append(("read_lines", path))
        raise AssertionError("the reader reopened the transcript")


def test_other_non_absence_read_failures_are_unreadable_transcripts():
    files = _ScriptedFiles(run_usage.TranscriptRead(lines=None, missing=False))

    assert _triple(_rich(Path("/t.jsonl"), files)) == (None, 0, 1)


def test_whole_read_failure_after_partial_progress_contributes_nothing():
    # A read that fails after producing part of the file reaches the reader as `lines=None`.
    files = _ScriptedFiles(run_usage.TranscriptRead(lines=None, missing=False))

    reading = _rich(Path("/t.jsonl"), files)

    assert reading == context_gauge.ContextReading(
        context_size=None, unreadable_lines=0, unreadable_transcripts=1
    )


def test_one_physical_read_and_no_existence_probe():
    lines = (json.dumps(_response("m1", input_tokens=4)), BAD_JSON)
    files = _ScriptedFiles(run_usage.TranscriptRead(lines=lines, missing=False))
    path = Path("/never/touched.jsonl")

    reading = _rich(path, files)

    assert files.calls == [("read_outcome", path)]
    assert (reading.context_size, reading.unreadable_lines) == (4, 1)


def test_size_reader_receives_the_buffered_snapshot_and_its_result_is_the_size():
    lines = (json.dumps(_response("m1", input_tokens=4)), BAD_JSON)
    files = _ScriptedFiles(run_usage.TranscriptRead(lines=lines, missing=False))
    path = Path("/never/touched.jsonl")
    received = []

    def size_reader(given_path, port):
        received.append((given_path, port.read_lines(given_path), port))
        return 987654

    reading = context_gauge.read_context(path, files, size_reader=size_reader)

    assert reading.context_size == 987654
    (given_path, snapshot_lines, port) = received[0]
    assert given_path == path
    assert snapshot_lines == list(lines)
    assert port is not files
    assert len(received) == 1


def test_size_reader_is_required_and_has_no_default():
    with pytest.raises(TypeError):
        context_gauge.read_context(Path("/t.jsonl"), run_usage.Files())  # type: ignore[call-arg]


RICH_SCALAR_CASES = {
    "duplicates": ([_response("m1", input_tokens=5)] * 3, 0),
    "repeated-non-adjacent-id": (
        [_response("m1", input_tokens=10), _response("m2", input_tokens=20),
         _response("m1", input_tokens=10)],
        0,
    ),
    "absent-class": (
        [{"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 12}}}], 0
    ),
    "null-class": (
        [{"type": "assistant",
          "message": {"id": "m1", "usage": {"input_tokens": 5, "cache_read_input_tokens": None}}}],
        0,
    ),
    "zero-not-unknown": (
        [{"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5}}}], 0
    ),
    "rejected-line-before-valid-with-reused-id": (
        [{"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": "x"}}},
         _response("m1", input_tokens=9)],
        1,
    ),
    "unknown-with-only-rejected": (
        [{"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": -1}}}], 1
    ),
}


@pytest.mark.parametrize("name", list(RICH_SCALAR_CASES))
def test_rich_reading_agrees_with_the_scalar_reader(tmp_path, name):
    records, expected_rejected = RICH_SCALAR_CASES[name]
    transcript = _write(tmp_path / "t.jsonl", records)

    reading = _rich(transcript)

    assert reading.context_size == context_gauge.current_context(transcript, run_usage.Files())
    assert reading.unreadable_lines == expected_rejected


def test_rich_reading_counts_repeated_rejected_lines_and_skips_a_partial_final_line(tmp_path):
    transcript = _write(
        tmp_path / "t.jsonl",
        [_response("m1", input_tokens=3)],
        tail=f"{BAD_JSON}\n{BAD_JSON}\n" + '{"type": "assistant", "message": {"id": "m2", "usa',
    )

    reading = _rich(transcript)

    assert reading.context_size == context_gauge.current_context(transcript, run_usage.Files())
    assert reading.context_size == 3
    assert reading.unreadable_lines == 3


def test_current_context_is_unchanged(tmp_path):
    assert context_gauge.current_context(tmp_path, run_usage.Files()) is None
    assert context_gauge.current_context(tmp_path / "absent.jsonl", run_usage.Files()) is None
    assert context_gauge.current_context(_reference(tmp_path, BAD_JSON, NOT_AN_OBJECT),
                                         run_usage.Files()) == 34224


def test_current_context_still_runs_through_a_read_lines_only_port(tmp_path):
    class ReadLinesOnly:
        def read_lines(self, path):
            return [json.dumps(_response("m1", input_tokens=2, cache_read=3))]

    assert context_gauge.current_context(tmp_path / "t.jsonl", ReadLinesOnly()) == 5


def test_context_reading_is_immutable():
    reading = context_gauge.ContextReading(context_size=1)

    assert (reading.unreadable_lines, reading.unreadable_transcripts) == (0, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.context_size = 2  # type: ignore[misc]


# ── The callers' functions answer as they did before reasons existed (CP-450, C1) ──

PARITY_PROJECTS = PRODUCT_ROOT / "tests" / "parity" / "projects"
DEV_DIR = "-home-user-work-demo"
SESSION_T = "a839285c-263f-40a6-8801-71f49d00cc68"

# Captured from the product at 1130e57, in a disposable clone, before this change: per fixture
# and session, `transcript_for`'s path under the root, the count and span ends of `usage_records`,
# `spend_by_model` as [input, output, cache_read, cache_write_1h, cache_write_5m, thinking] and
# `current_context`. `None` is a session with no transcript found.
BASELINE = {
    "R mode 000": {
        "15b8016a-f529-44fe-b6ea-1013fea8a4a8": None,
        "7ef055ef-5823-4b0d-845c-efc5b4fdab28": None,
        "a839285c-263f-40a6-8801-71f49d00cc68": None
    },
    "T deleted": {
        "15b8016a-f529-44fe-b6ea-1013fea8a4a8": {
            "context": 23922,
            "first": "2026-09-23T19:57:01.514Z",
            "last": "2026-09-23T19:57:01.514Z",
            "path": (
                "-home-user-work-demo/"
                "15b8016a-f529-44fe-b6ea-1013fea8a4a8.jsonl"
            ),
            "records": 1,
            "spend": {
                "claude-haiku-4-5-20251001": [
                    10,
                    98,
                    23912,
                    0,
                    0,
                    84
                ]
            }
        },
        "7ef055ef-5823-4b0d-845c-efc5b4fdab28": {
            "context": None,
            "first": None,
            "last": None,
            "path": "-home-user-code-reader/7ef055ef-5823-4b0d-845c-efc5b4fdab28.jsonl",
            "records": 0,
            "spend": {}
        },
        "a839285c-263f-40a6-8801-71f49d00cc68": None
    },
    "T mode 000": {
        "15b8016a-f529-44fe-b6ea-1013fea8a4a8": {
            "context": 23922,
            "first": "2026-09-23T19:57:01.514Z",
            "last": "2026-09-23T19:57:01.514Z",
            "path": (
                "-home-user-work-demo/"
                "15b8016a-f529-44fe-b6ea-1013fea8a4a8.jsonl"
            ),
            "records": 1,
            "spend": {
                "claude-haiku-4-5-20251001": [
                    10,
                    98,
                    23912,
                    0,
                    0,
                    84
                ]
            }
        },
        "7ef055ef-5823-4b0d-845c-efc5b4fdab28": {
            "context": None,
            "first": None,
            "last": None,
            "path": "-home-user-code-reader/7ef055ef-5823-4b0d-845c-efc5b4fdab28.jsonl",
            "records": 0,
            "spend": {}
        },
        "a839285c-263f-40a6-8801-71f49d00cc68": {
            "context": None,
            "first": None,
            "last": None,
            "path": (
                "-home-user-work-demo/"
                "a839285c-263f-40a6-8801-71f49d00cc68.jsonl"
            ),
            "records": 0,
            "spend": {}
        }
    },
    "only D mode 000": {
        "15b8016a-f529-44fe-b6ea-1013fea8a4a8": None,
        "7ef055ef-5823-4b0d-845c-efc5b4fdab28": {
            "context": None,
            "first": None,
            "last": None,
            "path": "-home-user-code-reader/7ef055ef-5823-4b0d-845c-efc5b4fdab28.jsonl",
            "records": 0,
            "spend": {}
        },
        "a839285c-263f-40a6-8801-71f49d00cc68": None
    }
}

def _station_fixture(name, work, seal):
    root = work / "R"
    shutil.copytree(PARITY_PROJECTS, root)
    if name == "T deleted":
        (root / DEV_DIR / f"{SESSION_T}.jsonl").unlink()
    elif name == "T mode 000":
        seal(root / DEV_DIR / f"{SESSION_T}.jsonl")
    elif name == "R mode 000":
        seal(root)
    elif name == "only D mode 000":
        seal(root / DEV_DIR)
    return root


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_caller_facing_functions_return_baseline_values_on_all_four_station_fixtures(
    name, tmp_path, seal
):
    root = _station_fixture(name, tmp_path, seal)
    files = run_usage.Files()
    for session_id, expected in BASELINE[name].items():
        path = run_usage.transcript_for(session_id, root, files)
        if expected is None:
            assert path is None
            continue
        records = list(run_usage.usage_records(path, files))
        spend = run_usage.spend_by_model(records)
        assert str(path.relative_to(root)) == expected["path"]
        assert len(records) == expected["records"]
        assert (records[0][2] if records else None) == expected["first"]
        assert (records[-1][2] if records else None) == expected["last"]
        assert {
            model: [s.input, s.output, s.cache_read, s.cache_write_1h, s.cache_write_5m, s.thinking]
            for model, s in spend.items()
        } == expected["spend"]
        assert context_gauge.current_context(path, files) == expected["context"]
