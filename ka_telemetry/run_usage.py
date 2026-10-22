"""ka_telemetry.run_usage — what a background run used, and what this reader could not read.

WHY THIS EXISTS
    Every background run already leaves its own receipt: `claude agents --json --all` names the
    run, and its transcript under `~/.claude/projects/<slug>/<sessionId>.jsonl` carries a
    `usage` block on every `assistant` record. Nothing sums that up today, so a run's usage is
    only visible by opening its transcript by hand. This reads what is already there; it writes
    nothing.

    Measured against the live disk on 2026-09-04 (474 rows, 10,028 usage records sampled):

    1. The run population is `kind != "interactive"` — 471 `background` rows and 3
       `interactive` ones (the owner's own sessions). There is no structured "launcher" field,
       so that is the population this reader defines, and it says so in its own output.
    2. The join is by `sessionId`, one-to-one with a `<sessionId>.jsonl` file under
       `~/.claude/projects/*/`.
    3. **The unit is an API response, not a transcript record.** The harness writes one
       `assistant` record per CONTENT BLOCK of one response — thinking, then tool_use, then
       text — and every one of them repeats that response's `message.usage` verbatim under the
       same `message.id`. Counting per record therefore counted a response two and three times.
       Measured 2026-09-09 on runner transcript `abcf33f9`: 128 usage records, 72 distinct
       `message.id` values, and every one of the 51 repeated ids carrying byte-identical usage.
       The factor is not constant — 1.74x on one population and 3.35x on another — so it never
       cancelled out of a comparison between two of them, which is what this reader is for. The
       figures in the paragraph above were taken before this was found and are per record.
    4. There are five spend classes, not four: `input_tokens`, `output_tokens`,
       `cache_read_input_tokens`, and `usage.cache_creation`'s own split of
       `ephemeral_1h_input_tokens` from `ephemeral_5m_input_tokens` — priced differently, so
       reported apart. `cache_creation_input_tokens` is their sum and is never carried as a
       sixth class.
    5. `output_tokens_details.thinking_tokens` is a subset of `output_tokens`, never an addend.
    6. There is no `durationMs` on any record; wall time is a span — first transcript
       timestamp to last — never a sum of per-message latencies that do not exist.
    7. Spend is kept apart per model id, and `<synthetic>` (a record the CLI itself emits, not
       a billed model) is its own key, never folded into a real model's totals.
    8. Not every run has a readable transcript. Coverage — read / unreadable / total — is
       stated, with the unreadable session ids named, rather than silently undercounted.

    **No price model, no currency.** Rates belong to a vendor and change without this reader
    changing; it counts, it does not price. And **no output anywhere sums the five classes** —
    a cache read and a completion token are not the same thing priced the same way, so adding
    them would report a number nobody can price back apart.

    **A second population: runs the caller names.** A `claude --print` run never appears in
    `claude agents` — "`--print` never starts the interactive session that `claude agents`
    attaches to" (https://code.claude.com/docs/en/agent-view.md). The caller names such a run by
    its session id (`--session`, or `read_runs(..., session_ids, ...)`), and the session joins its
    transcript exactly as a listed row does. A session that is both listed and named is one run,
    the listed one; a session named twice is one run, at its first position. A named run carries
    no name, working directory, state or start time: nothing but its transcript is read for it.

Usage:  ka-telemetry runs [--agents-json FILE] [--projects-root DIR] [--session SESSION_ID]
                          [--json]
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import dataclasses
import errno
import json
import stat
import subprocess
import sys
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ka_telemetry import stream_usage, transcript_notes

_UsageRecord = tuple[dict, str | None, str | None]

POPULATION_NOTE = (
    'Population: background runs only (rows with kind != "interactive"), and — with --session — '
    "every run named by its session id."
)


# ── Task 1 — the population is named, and interactive is out of it ────────


@dataclass(frozen=True)
class Run:
    """One row of the population this reader scopes itself to — a non-interactive agent, with
    a session id to join its transcript on."""

    session_id: str
    name: str | None
    cwd: str | None
    state: str | None
    started_at: int | None


@dataclass(frozen=True)
class ParsedRuns:
    """`runs_from`'s whole answer — the kept population, plus what was excluded and why.

    `interactive_excluded` and `dropped_no_session` are two different reasons a row is not a
    `Run`: the first is out of scope by definition, the second is in scope but unjoinable.
    Conflating them would hide the second, which is the undercount this reader exists to name.
    """

    runs: tuple[Run, ...]
    interactive_excluded: int
    dropped_no_session: int
    total_seen: int
    listed: int = 0
    named: int = 0


def runs_from(agents_json_text: str) -> ParsedRuns:
    """Parse `claude agents --json --all` output into the population this reader scopes itself
    to — `kind != "interactive"`, with a `sessionId` to join a transcript on."""
    try:
        rows = json.loads(agents_json_text)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "could not parse `claude agents --json --all` output as JSON — if you passed "
            "--agents-json, check that file; otherwise run that command yourself to see what it "
            f"prints. The parser reported: {exc}"
        ) from exc
    if not isinstance(rows, list):
        raise ValueError(
            "`claude agents --json --all` output was not a JSON array — if you passed "
            "--agents-json, check that file; otherwise run that command yourself to see what it "
            "prints"
        )

    runs: list[Run] = []
    interactive_excluded = 0
    dropped_no_session = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "interactive":
            interactive_excluded += 1
            continue
        session_id = row.get("sessionId")
        if not session_id:
            dropped_no_session += 1
            continue
        runs.append(
            Run(
                session_id=session_id,
                name=row.get("name"),
                cwd=row.get("cwd"),
                state=row.get("state"),
                started_at=row.get("startedAt"),
            )
        )

    return ParsedRuns(
        runs=tuple(runs),
        interactive_excluded=interactive_excluded,
        dropped_no_session=dropped_no_session,
        total_seen=len(rows),
        listed=len(runs),
    )


def with_named_sessions(parsed: ParsedRuns, session_ids: Iterable[str]) -> ParsedRuns:
    """`parsed` plus one run per named session id the listing does not already hold. Listed runs
    keep their listing order; each unlisted id is appended once, at its first position, with no
    name, working directory, state or start time — the listed row wins when a session is both."""
    held = {run.session_id for run in parsed.runs}
    appended: list[Run] = []
    for session_id in session_ids:
        if session_id in held:
            continue
        held.add(session_id)
        appended.append(
            Run(session_id=session_id, name=None, cwd=None, state=None, started_at=None)
        )
    return dataclasses.replace(
        parsed,
        runs=parsed.runs + tuple(appended),
        total_seen=parsed.total_seen + len(appended),
        named=len(appended),
    )


# ── Task 2 — a session id resolves to one transcript or to none ─────────


def transcript_for(session_id: str, projects_root: Path, files: Files) -> Path | None:
    """The single `<sid>.jsonl` under `<projects_root>/*/`, or `None`.

    A missing `projects_root` is not an error here — `Files.find_transcripts` over a
    nonexistent directory returns no matches rather than raising, which is exactly the "nothing
    to read" answer this function owes. A session id duplicated under two project slugs (not
    observed on this machine, but not ruled out either) resolves to the lexicographically first
    match rather than raising — deterministic, not correct-by-construction, because there is no
    fact on disk that says which of the two is the real one.
    """
    matches = sorted(files.find_transcripts(session_id, projects_root))
    return matches[0] if matches else None


# ── Why a run, or a directory, could not be read ──────────────────────────


class UnreadableRunReason(Enum):
    """Why one run is unreadable. `GONE`: no transcript was found and every directory was looked
    in or is absent, or one was found and was then absent when it was read. `COULD_NOT_BE_OPENED`:
    a transcript was found and could not be read. `PLACE_COULD_NOT_BE_LOOKED_IN`: none was found,
    and a directory it may be in could not be looked in. The members are facts; the words a report
    prints for them are `reason_words`'s."""

    GONE = "gone"
    COULD_NOT_BE_OPENED = "could not be opened"
    PLACE_COULD_NOT_BE_LOOKED_IN = "place could not be looked in"


_REASON_WORDS = {
    UnreadableRunReason.GONE: "gone",
    UnreadableRunReason.COULD_NOT_BE_OPENED: "could not be opened",
    UnreadableRunReason.PLACE_COULD_NOT_BE_LOOKED_IN: (
        "a directory its transcript may be in could not be looked in"
    ),
}


def reason_words(reason: UnreadableRunReason) -> str:
    """The words both renderings print for `reason`."""
    return _REASON_WORDS[reason]


DIRECTORY_FAILURE_REASON = "could not be looked in"


@dataclass(frozen=True)
class DirectoryFailure:
    """A projects root, or a project directory under it, that a report could not look in. `steps`
    carries the same value, so both reports name such a place one way."""

    directory: Path
    reason: str = DIRECTORY_FAILURE_REASON


def directory_failure_json(failure: DirectoryFailure) -> dict[str, str]:
    """The two-member object `--json` carries for `failure`. The path keeps the spelling the
    report read it at."""
    return {"directory": str(failure.directory), "reason": failure.reason}


def directory_failures(places: Iterable[Path], root: Path) -> tuple[DirectoryFailure, ...]:
    """`places` once each, the projects `root` first and then the project directories by name,
    whatever order they were found in."""
    ordered = sorted(set(places), key=lambda place: (place != root, place.name))
    return tuple(DirectoryFailure(directory=place) for place in ordered)


# ── Task 3 — one API response yields one figure ────────────


@dataclass
class UnreadableLines:
    """How many lines one reading of a transcript could not read: the non-blank lines that do not
    parse as JSON, and those that parse but cannot be added (a value that is not an object, or an
    `assistant` record whose message, usage, token figures, model, id or timestamp are not of
    the kind the report reads). One counter belongs to one reading — `usage_records` adds to the
    counter it is handed as it goes, so the count is complete only once its iterator is
    exhausted, and a counter handed to a second reading would carry both. Mutable on purpose: it
    is filled in during the same pass that yields the figures, so a transcript is never read
    twice to count it.

    `unreadable_files` is a different fact, observed in that same read: it becomes `1` when the
    port could not read the transcript at all (`Files.read_lines` returned `None`). It stays `0`
    for a file that was read, empty or all-rejected included, and it never adds to `count`. A
    counter reused across readings accumulates the two independently."""

    count: int = 0
    unreadable_files: int = 0


class ResponseScope:
    """The response ids one report has met, across every transcript it reads. One report makes
    one, hands it to `usage_records` for each transcript in the order the report prints its rows,
    and asks it afterwards how many ids it found in more than one transcript. It is a fresh value
    each time: nothing here outlives a report or reaches another, which keeps `usage_records`
    over one transcript exactly what it was.

    A transcript is told apart by the `Path` it was supplied at: no link is resolved and no file
    is opened. The first transcript to claim an id keeps it, under the row key that claimed it;
    every later claim from another transcript counts nothing but is remembered, so the count of
    ids found in two or more transcripts is complete only once the report has read them all."""

    def __init__(self) -> None:
        self._keeper: dict[str, tuple[Path, Hashable]] = {}
        self._transcripts: dict[str, set[Path]] = {}

    def claim(self, path: Path, response_id: str | None, row_key: Hashable) -> bool:
        """Whether the response counts for `row_key`. A response with no id always does, and so
        does a repeat from the transcript that already kept the id, which is how a transcript
        read for two rows is counted for each, as it was before this scope existed."""
        if response_id is None:
            return True
        keeper = self._keeper.setdefault(response_id, (path, row_key))
        self._transcripts.setdefault(response_id, set()).add(path)
        return keeper[0] == path

    def count_shared(self, row_key: Hashable | None = None) -> int:
        """How many ids were found in at least two distinct transcripts, each once however many
        transcripts hold it. Given a `row_key`, only those ids that row keeps."""
        return sum(
            1
            for response_id, transcripts in self._transcripts.items()
            if len(transcripts) > 1 and (row_key is None or self._keeper[response_id][1] == row_key)
        )


def usage_records(
    path: Path,
    files: Files,
    *,
    unreadable_lines: UnreadableLines | None = None,
    response_scope: ResponseScope | None = None,
    row_key: Hashable | None = None,
) -> Iterator[_UsageRecord]:
    """Yield `(usage, model, timestamp)` once per API RESPONSE in `path` — never once per
    `assistant` record, which is a content block of one.

    The harness splits one response across a record per content block (thinking, tool_use, text)
    and repeats its `message.usage` verbatim on each, keyed by a shared `message.id`. Summing per
    record inflated every figure this module has ever produced, by a factor that differs per
    population, so `message.id` is remembered and a repeat is skipped.

    **A record carrying no `message.id` is always yielded.** It has no identity to compare, so two
    of them are two responses as far as anything here can tell; dropping the second would trade an
    overcount for a silent undercount, which is the worse of the two — a missing figure cannot be
    noticed afterwards, and an inflated one at least shows up against a re-derivation.

    A transcript being read while its own run is still writing to it is the normal case, not an
    edge case — this reads a live agent's own file — so a truncated or malformed final line is
    skipped rather than fatal, and a missing file yields nothing rather than raising.

    Given an `unreadable_lines` counter, each skipped line is counted on it during this same
    read. A line that parses as JSON is counted only when it cannot be added (CP-412 to CP-414);
    an ordinary record that carries no usage is read and adds nothing, uncounted. A rejected line
    adds nothing, reserves no response id and is counted whether or not its id was seen, and the
    count is of input lines, so repeated response ids leave it as it is.

    Given a `response_scope`, a response whose id another transcript of the same report was the
    first to claim is skipped as well, and one claimed here is kept for `row_key`. Without one,
    nothing is remembered between calls.
    """
    lines = files.read_lines(path)
    if lines is None:
        if unreadable_lines is not None:
            unreadable_lines.unreadable_files += 1
        return
    seen_response_ids: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line, parse_int=_json_integer)
        except (ValueError, TypeError):
            if unreadable_lines is not None:
                unreadable_lines.count += 1
            continue
        contribution = _contribution(record)
        if contribution is None:
            continue
        if contribution is _UNREADABLE:
            if unreadable_lines is not None:
                unreadable_lines.count += 1
            continue
        usage, model, response_id, timestamp = contribution
        if response_id is not None:
            if response_id in seen_response_ids:
                continue
            seen_response_ids.add(response_id)
            if response_scope is not None and not response_scope.claim(
                path, response_id, row_key
            ):
                continue
        yield usage, model, timestamp


_UNREADABLE = object()


class _MinusZero(int):
    """The integer zero, written `-0`. CP-413 reads a figure only when it is written as digits
    alone, and `json` would hand `-0` over as a plain `0`, so it is kept apart here; anything
    that reads it as a number still reads zero."""


def _json_integer(text: str) -> int:
    """A JSON integer literal as `json.loads` hands it over, except `-0`, which stays told apart
    as `_MinusZero` (JSON allows no other way to write zero with a sign)."""
    return _MinusZero(0) if text == "-0" else int(text)


_TOKEN_FIGURES = ("input_tokens", "output_tokens", "cache_read_input_tokens")
_CACHE_CREATION_FIGURES = ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens")


def _is_token_count(value: object) -> bool:
    """`True` for `None` (an absent or null figure) or a JSON integer of zero or more written as
    digits alone. `5.0`, `5e0` and `NaN` parse to a `float`, so they fail the `int` test; a
    `bool` is an `int` to Python and is not one to CP-413, and `-0` arrives as `_MinusZero`, so
    both are refused by requiring exactly `int`."""
    return value is None or (type(value) is int and value >= 0)


def _is_string_or_null(value: object) -> bool:
    return value is None or isinstance(value, str)


def _usage_is_readable(usage: dict) -> bool:
    """Whether every value CP-413 governs in `usage` is of the kind it states. Members it does
    not govern — `cache_creation_input_tokens`, `service_tier` and the rest — decide nothing."""
    if not all(_is_token_count(usage.get(name)) for name in _TOKEN_FIGURES):
        return False
    for container, names in (
        ("cache_creation", _CACHE_CREATION_FIGURES),
        ("output_tokens_details", ("thinking_tokens",)),
    ):
        members = usage.get(container)
        if members is None:
            continue
        if not isinstance(members, dict):
            return False
        if not all(_is_token_count(members.get(name)) for name in names):
            return False
    return True


def _contribution(record: object):
    """What one parsed transcript line contributes: `None` for an ordinary record that adds
    nothing, `_UNREADABLE` for a record that cannot be added (CP-412), else the validated
    `(usage, model, response_id, timestamp)` — the whole record is accepted or rejected here,
    before any response id is remembered or any figure is accumulated (CP-415)."""
    if not isinstance(record, dict):
        return _UNREADABLE
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if message is None:
        return None
    if not isinstance(message, dict):
        return _UNREADABLE
    usage = message.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        return _UNREADABLE
    if not usage:
        return None
    model, response_id, timestamp = message.get("model"), message.get("id"), record.get("timestamp")
    if not (
        _usage_is_readable(usage)
        and _is_string_or_null(model)
        and _is_string_or_null(response_id)
        and _is_string_or_null(timestamp)
    ):
        return _UNREADABLE
    return usage, model, response_id, timestamp


# ── Task 4 — the five classes stay apart, and thinking is a share ─────────


@dataclass(frozen=True)
class ModelSpend:
    """One model's totals over a run — five classes, kept apart, none of them the others'
    sum. `thinking` is a share of `output`, never an addend to it, and is `None` when nothing
    this model produced carried `output_tokens_details` — a run whose records are all silent on
    thinking is a different fact from a run that spent zero of it."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_1h: int = 0
    cache_write_5m: int = 0
    thinking: int | None = None


def spend_by_model(records: Iterable[_UsageRecord]) -> dict[str, ModelSpend]:
    """One `ModelSpend` per model id found in `records` — `(usage, model, timestamp)` triples,
    as `usage_records` yields them. `usage.cache_creation_input_tokens` is never read directly:
    it is the sum of the two `cache_creation` ephemeral figures this reports apart, so reading
    it too would be a sixth class standing in for the other two.
    """
    totals: dict[str, dict] = {}
    thinking_seen: dict[str, bool] = {}
    for usage, model, _timestamp in records:
        acc = totals.setdefault(
            model,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write_1h": 0,
                "cache_write_5m": 0,
                "thinking": 0,
            },
        )
        acc["input"] += usage.get("input_tokens") or 0
        acc["output"] += usage.get("output_tokens") or 0
        acc["cache_read"] += usage.get("cache_read_input_tokens") or 0
        creation = usage.get("cache_creation") or {}
        acc["cache_write_1h"] += creation.get("ephemeral_1h_input_tokens") or 0
        acc["cache_write_5m"] += creation.get("ephemeral_5m_input_tokens") or 0
        details = usage.get("output_tokens_details")
        if details is not None:
            thinking_seen[model] = True
            acc["thinking"] += details.get("thinking_tokens") or 0

    return {
        model: ModelSpend(
            input=acc["input"],
            output=acc["output"],
            cache_read=acc["cache_read"],
            cache_write_1h=acc["cache_write_1h"],
            cache_write_5m=acc["cache_write_5m"],
            thinking=acc["thinking"] if thinking_seen.get(model) else None,
        )
        for model, acc in totals.items()
    }


def add_spend(a: ModelSpend, b: ModelSpend) -> ModelSpend:
    """One model's two `ModelSpend`s, added class by class — never a class summed with another
    class, only the same class across two readings. `thinking` stays `None` only when neither side
    carried any, matching `ModelSpend`'s own rule that `None` means "nothing this model produced
    carried `output_tokens_details`", not zero.

    Public and here, rather than private in a reader, because this module owns what a spend IS.
    It was `flow_usage._add_spend` while that reader was the only one folding two readings
    together; a third reader wanting the same arithmetic is what a private name cannot serve.
    """
    thinking = None
    if a.thinking is not None or b.thinking is not None:
        thinking = (a.thinking or 0) + (b.thinking or 0)
    return ModelSpend(
        input=a.input + b.input,
        output=a.output + b.output,
        cache_read=a.cache_read + b.cache_read,
        cache_write_1h=a.cache_write_1h + b.cache_write_1h,
        cache_write_5m=a.cache_write_5m + b.cache_write_5m,
        thinking=thinking,
    )


def merge_spend(
    a: dict[str, ModelSpend], b: dict[str, ModelSpend]
) -> dict[str, ModelSpend]:
    """Two per-model spend dicts, merged model by model with `add_spend`. A model present in only
    one side passes through unchanged, and neither argument is mutated — a caller folding many
    readings into one accumulator would otherwise corrupt the first as it went.
    """
    merged = dict(a)
    for model, spend in b.items():
        merged[model] = add_spend(merged[model], spend) if model in merged else spend
    return merged


# ── Task 5 — the span is measured, never summed ───────────────


def span_seconds(records: Iterable[_UsageRecord]) -> float | None:
    """The run's wall time — its last transcript timestamp minus its first — never a sum of
    per-message latencies, which no record on disk carries (`durationMs` is on none of them).
    Timestamps are read out of order-of-arrival, not order-in-the-file: min to max over the
    whole set, so a transcript whose lines are not chronological still gives the true span.
    """
    timestamps = []
    for _usage, _model, timestamp in records:
        if not timestamp:
            continue
        try:
            moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # A date with no offset is placed as UTC, the offset the transcripts' own `Z` dates
        # carry, so it can be measured against them; dates that all lack one measure as before.
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        timestamps.append(moment)
    if not timestamps:
        return None
    return (max(timestamps) - min(timestamps)).total_seconds()


# ── Task 6 — the ledger states what it could not read ─────────────


@dataclass(frozen=True)
class Coverage:
    """How much of the population this reading actually covered — never implied, always
    stated, so a run with nothing to report and a run whose transcript is gone read as the
    two different facts they are. `dropped_no_session` is a third, earlier fact — a row
    `runs_from` never admitted to the population at all, for having no `sessionId` to join a
    transcript on — kept apart from `unreadable` (in scope but unjoinable) the same way
    `ParsedRuns` keeps it apart from `interactive_excluded`.
    `read + unreadable + dropped_no_session` equals the population before dropping.
    `unreadable_lines` is a different kind of figure from `unreadable`: lines inside the
    transcripts that were read and could not be, totalled over every transcript the ledger
    actually read — a run with no transcript contributes none."""

    read: int
    unreadable: int
    total: int
    unreadable_ids: tuple[str, ...]
    dropped_no_session: int = 0
    listed: int = 0
    named: int = 0
    unreadable_lines: int = 0
    # Not a field: a reading that found no response in more than one transcript is this class
    # itself, so every construction that worked before still does and still compares equal.
    responses_in_more_than_one_transcript = 0


@dataclass(frozen=True)
class CoverageWithSharedResponses(Coverage):
    """A `Coverage` of a reading that found `responses_in_more_than_one_transcript` responses,
    one or more, each in two or more of the transcripts it read and counted once."""

    responses_in_more_than_one_transcript: int = 0


@dataclass(frozen=True)
class LedgerRow:
    """One run's reading: its identity from `claude agents --json --all`, its wall time, and
    its spend by model. `spend == {}` means a transcript that read cleanly and reported
    nothing — never the same fact as the run being absent from the ledger altogether."""

    session_id: str
    name: str | None
    cwd: str | None
    state: str | None
    started_at: int | None
    span_seconds: float | None
    spend: dict[str, ModelSpend]


@dataclass(frozen=True)
class Ledger:
    """The rows read and the coverage of the reading. A reader-made ledger also says why each
    unreadable id could not be read (`unreadable_reasons`, aligned with
    `coverage.unreadable_ids`) and which directories could not be looked in. A hand-built one
    carries neither, and is rendered as it always was."""

    rows: tuple[LedgerRow, ...]
    coverage: Coverage
    unreadable_reasons: tuple[UnreadableRunReason, ...] = ()
    unreadable_directories: tuple[DirectoryFailure, ...] = ()


def ledger(
    runs: Iterable[Run],
    projects_root: Path,
    files: Files,
    *,
    dropped_no_session: int = 0,
    listed: int | None = None,
    named: int = 0,
) -> Ledger:
    """One `LedgerRow` per run whose transcript is readable, plus a `Coverage` naming every run
    this could not read by session id — a transcript that resolves but holds no usage records
    still produces a row (read, zero spend); a run with no transcript at all, and a run whose
    transcript is found and cannot be opened, are unreadable: no row, no pointer, no line count.

    `dropped_no_session` carries `ParsedRuns.dropped_no_session` through to `Coverage` — this
    function never computes it, since `runs` has already had those rows dropped by the time it
    arrives here. A caller that does not pass it gets `0`, a stated count rather than an absent
    field. `listed` and `named` count the two populations the runs came from; a caller that
    supplies neither has every run counted as listed.
    """
    rows: list[LedgerRow] = []
    unreadable_ids: list[str] = []
    reasons: list[UnreadableRunReason] = []
    unreadable_line_total = 0
    scope = ResponseScope()
    runs = list(runs)
    if listed is None:
        listed = len(runs) - named
    search = _search_transcripts(files, projects_root, [run.session_id for run in runs])
    for position, run in enumerate(runs):
        matches = search.matches.get(run.session_id, ())
        if not matches:
            unreadable_ids.append(run.session_id)
            reasons.append(
                UnreadableRunReason.PLACE_COULD_NOT_BE_LOOKED_IN
                if run.session_id in search.incomplete
                else UnreadableRunReason.GONE
            )
            continue
        counter = UnreadableLines()
        with _observing_reads() as observed:
            records = list(
                usage_records(
                    min(matches),
                    files,
                    unreadable_lines=counter,
                    response_scope=scope,
                    row_key=position,
                )
            )
        if counter.unreadable_files:
            unreadable_ids.append(run.session_id)
            reasons.append(_read_failure_reason(observed))
            continue
        unreadable_line_total += counter.count
        rows.append(
            LedgerRow(
                session_id=run.session_id,
                name=run.name,
                cwd=run.cwd,
                state=run.state,
                started_at=run.started_at,
                span_seconds=span_seconds(records),
                spend=spend_by_model(records),
            )
        )

    figures = dict(
        read=len(rows),
        unreadable=len(unreadable_ids),
        total=len(runs),
        unreadable_ids=tuple(unreadable_ids),
        dropped_no_session=dropped_no_session,
        listed=listed,
        named=named,
        unreadable_lines=unreadable_line_total,
    )
    shared = scope.count_shared()
    return Ledger(
        rows=tuple(rows),
        coverage=(
            CoverageWithSharedResponses(**figures, responses_in_more_than_one_transcript=shared)
            if shared
            else Coverage(**figures)
        ),
        unreadable_reasons=tuple(reasons),
        unreadable_directories=directory_failures(search.failed_places, projects_root),
    )


def _search_transcripts(
    files: Files, projects_root: Path, session_ids: Sequence[str]
) -> TranscriptSearch:
    """What looking for every id's transcript came to. A port with no outcome seam of its own
    answers through `find_transcripts` alone: it reports no search that could not finish and no
    failed directory, which is all such a port knows."""
    search = getattr(files, "_search_transcripts", None)
    if search is not None:
        return search(session_ids, projects_root)
    return TranscriptSearch(
        matches={sid: tuple(files.find_transcripts(sid, projects_root)) for sid in session_ids},
        incomplete=frozenset(),
        failed_places=(),
    )


def _read_failure_reason(observed: Sequence[TranscriptRead]) -> UnreadableRunReason:
    """Why the transcript's one read failed, from the outcome that read reported. A port that
    reported none gives no reason to call the transcript gone, so it could not be opened."""
    if observed and observed[-1].missing:
        return UnreadableRunReason.GONE
    return UnreadableRunReason.COULD_NOT_BE_OPENED


def read_runs(agents_json_text: str, session_ids: Iterable[str], reader: Reader) -> Ledger:
    """The library form of `ka-telemetry runs`: parse the listing, add the named sessions, read
    every transcript. Runs no process and renders nothing — `agents_json_text` is already text.
    Raises `ValueError` when that text is not a JSON array."""
    parsed = with_named_sessions(runs_from(agents_json_text), session_ids)
    return ledger(
        parsed.runs,
        reader.projects_root,
        reader.files,
        dropped_no_session=parsed.dropped_no_session,
        listed=parsed.listed,
        named=parsed.named,
    )


# ── Task 7 — the two renderings, and neither one totals ────────────


# The words both reports print when a response was found in more than one transcript: one whole
# string for one and one for more, so no `s` is added at runtime. They are `change_text.md`'s,
# verbatim.
SHARED_RESPONSES_ONE = (
    "1 response found in more than one of the transcripts this report read, counted once"
)
SHARED_RESPONSES_MANY = (
    "{response_count} responses found in more than one of the transcripts this report read, "
    "each counted once"
)
UNREADABLE_LINES_ONE = "1 unreadable line skipped in the transcripts read"
UNREADABLE_LINES_MANY = "{line_count} unreadable lines skipped in the transcripts read"


DIRECTORY_LINE = (
    "{directory} could not be looked in, so nothing in it is in this report. "
    "Run this command again once it can be looked in."
)


def shared_responses_clause(count: int) -> str | None:
    """The coverage clause for `count` responses found in more than one transcript, or `None` at
    zero, where a coverage line says nothing of it. `flow_usage` prints the same words."""
    if count == 1:
        return SHARED_RESPONSES_ONE
    if count > 1:
        return SHARED_RESPONSES_MANY.format(response_count=count)
    return None


def directory_line(failure: DirectoryFailure) -> str:
    """The line both reports print for a directory they could not look in."""
    return DIRECTORY_LINE.format(directory=failure.directory)


def _unreadable_entries(
    ids: tuple[str, ...], reasons: tuple[UnreadableRunReason, ...]
) -> list[str]:
    """Each id with its reason in brackets, or the bare id where the ledger holds no reason for
    it (a hand-built one)."""
    if len(reasons) != len(ids):
        return list(ids)
    return [f"{sid} ({reason_words(reason)})" for sid, reason in zip(ids, reasons, strict=True)]


def _coverage_line(coverage: Coverage, reasons: tuple[UnreadableRunReason, ...] = ()) -> str:
    line = (
        f"Coverage: {coverage.read} read / {coverage.unreadable} unreadable / "
        f"{coverage.total} total ({coverage.listed} listed, {coverage.named} named)"
    )
    if coverage.unreadable_ids:
        entries = _unreadable_entries(coverage.unreadable_ids, reasons)
        line += f" — unreadable: {', '.join(entries)}"
    if coverage.unreadable_lines == 1:
        line += f" — {UNREADABLE_LINES_ONE}"
    elif coverage.unreadable_lines > 1:
        line += " — " + UNREADABLE_LINES_MANY.format(line_count=coverage.unreadable_lines)
    shared = shared_responses_clause(coverage.responses_in_more_than_one_transcript)
    if shared is not None:
        line += f" — {shared}"
    return line


def render(ledger: Ledger, *, matched_sessions: Iterable[str] = ()) -> str:
    """The human summary. Every number here is one of the five classes or a plain count — never
    their sum, so a rendered figure is always traceable back to one thing that was actually
    metered. It ends with the no-cost line, and under it a pointer for each of
    `matched_sessions`: the sessions this report shows that the telemetry stream holds rows for.
    """
    lines = [
        POPULATION_NOTE,
        transcript_notes.SPAN_LINE,
        _coverage_line(ledger.coverage, ledger.unreadable_reasons),
        f"Dropped (no sessionId): {ledger.coverage.dropped_no_session}",
        "",
    ]
    directories = [directory_line(failure) for failure in ledger.unreadable_directories]
    if not ledger.rows:
        lines.extend(directories or ["No runs read."])
        return transcript_notes.append_notes("\n".join(lines), matched_sessions)

    for row in ledger.rows:
        lines.append(
            f"{row.session_id}  {row.name or '(unnamed)'}  cwd={row.cwd}  state={row.state}"
        )
        span = "n/a" if row.span_seconds is None else f"{row.span_seconds:g}s"
        lines.append(f"  started={row.started_at}  span={span}")
        if not row.spend:
            lines.append("  no usage records")
        for model, spend in row.spend.items():
            thinking = "" if spend.thinking is None else f" thinking={spend.thinking}"
            lines.append(
                f"  {model}: input={spend.input} output={spend.output}{thinking} "
                f"cache_read={spend.cache_read} cache_write_1h={spend.cache_write_1h} "
                f"cache_write_5m={spend.cache_write_5m}"
            )
        lines.append("")
    lines.extend(directories)
    return transcript_notes.append_notes("\n".join(lines).rstrip(), matched_sessions)


def printable(text: str) -> str:
    """`text` as a report prints it: each character UTF-8 cannot encode — an unpaired
    surrogate, which a JSON string in a transcript or journal may hold — written as its
    `\\uXXXX` escape, the form `--json` already prints for it, so no string read stops the
    report. Every other character is kept, so text that encodes is returned unchanged."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _reasons_member(ledger: Ledger) -> dict[str, list[str]]:
    """The `unreadable_reasons` member of the JSON coverage object: present only when there is an
    unreadable id and the ledger holds a reason for each, in the ids' order."""
    ids, reasons = ledger.coverage.unreadable_ids, ledger.unreadable_reasons
    if not ids or len(reasons) != len(ids):
        return {}
    return {"unreadable_reasons": [reason_words(reason) for reason in reasons]}


def render_json(ledger: Ledger) -> str:
    """The machine rows — same population sentence and coverage as `render`, one object per
    readable run, and the same never-summed five classes (`thinking` stays `null`, not `0`,
    when nothing this model produced carried it)."""
    payload = {
        "population": POPULATION_NOTE,
        "span": transcript_notes.SPAN_LINE,
        "coverage": {
            "read": ledger.coverage.read,
            "unreadable": ledger.coverage.unreadable,
            "total": ledger.coverage.total,
            "unreadable_ids": list(ledger.coverage.unreadable_ids),
            **_reasons_member(ledger),
            "dropped_no_session": ledger.coverage.dropped_no_session,
            "listed": ledger.coverage.listed,
            "named": ledger.coverage.named,
        },
        "rows": [
            {
                "session_id": row.session_id,
                "name": row.name,
                "cwd": row.cwd,
                "state": row.state,
                "started_at": row.started_at,
                "span_seconds": row.span_seconds,
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
            for row in ledger.rows
        ],
    }
    coverage = payload["coverage"]
    if ledger.unreadable_directories:
        coverage["unreadable_directories"] = [
            directory_failure_json(failure) for failure in ledger.unreadable_directories
        ]
    if ledger.coverage.unreadable_lines > 0:
        coverage["unreadable_lines"] = ledger.coverage.unreadable_lines
    if ledger.coverage.responses_in_more_than_one_transcript > 0:
        coverage["responses_in_more_than_one_transcript"] = (
            ledger.coverage.responses_in_more_than_one_transcript
        )
    return json.dumps(payload, indent=2)


# ── The ports ───────────────────────────
#
# Every world-reach this reader makes — the filesystem, a subprocess — arrives through one of
# these two. Neither is constructed anywhere but `build_reader()`, below.


@dataclass(frozen=True)
class TranscriptRead:
    """What one attempt to read a transcript came to. `lines` holds every line of a completed
    read — an empty tuple for an empty file — and is `None` when the read did not complete, so a
    failed read never exposes part of a file. `missing` tells the two ways a read does not
    complete apart: `True` when nothing is at the path, `False` when something is there and
    could not be opened."""

    lines: tuple[str, ...] | None
    missing: bool


_ABSENCE_ERRORS = (FileNotFoundError, NotADirectoryError)


@dataclass(frozen=True)
class TranscriptSearch:
    """What looking for the transcripts of some session ids came to: the `matches` found for each
    id, the ids whose search could not finish (`incomplete`, each because a directory it needed
    could not be looked in), and every directory that could not be looked in (`failed_places`,
    in the order found). An id with no match and a finished search has no transcript."""

    matches: Mapping[str, tuple[Path, ...]]
    incomplete: frozenset[str]
    failed_places: tuple[Path, ...]


_read_observation: contextvars.ContextVar[list[TranscriptRead] | None] = contextvars.ContextVar(
    "read_observation", default=None
)


def observe_read(outcome: TranscriptRead) -> None:
    """Report how one read ended to the ledger observing it, if one is. A port's `read_lines`
    calls this, so the ledger learns why a read failed from the read it parsed and never from a
    second one."""
    observed = _read_observation.get()
    if observed is not None:
        observed.append(outcome)


@contextlib.contextmanager
def _observing_reads() -> Iterator[list[TranscriptRead]]:
    observed: list[TranscriptRead] = []
    token = _read_observation.set(observed)
    try:
        yield observed
    finally:
        _read_observation.reset(token)


@dataclass(frozen=True)
class Listing:
    """What looking in one directory came to: its `(name, is a directory)` entries, or `failed`
    when it exists and could not be looked in — not listed, or not entered to tell which of its
    entries are directories. Absence is not a failure and lists nothing."""

    entries: tuple[tuple[str, bool], ...]
    failed: bool


def list_directory(path: Path) -> Listing:
    try:
        entries = tuple((entry.name, _is_directory(entry)) for entry in path.iterdir())
    except _ABSENCE_ERRORS:
        return Listing(entries=(), failed=False)
    except OSError:
        return Listing(entries=(), failed=True)
    return Listing(entries=entries, failed=False)


def _is_directory(entry: Path) -> bool:
    """Whether `entry` is a directory, following a link. An entry that is gone, or a link that
    leads nowhere, is not one. Any other failure to find out is raised: the directory holding
    `entry` could not be looked in."""
    try:
        return stat.S_ISDIR(entry.stat().st_mode)
    except _ABSENCE_ERRORS:
        return False
    except OSError as error:
        if error.errno == errno.ELOOP:
            return False
        raise


class Files:
    """Every filesystem touch this reader makes: finding a session's transcript files under a
    projects root, and reading a file's lines. Nothing else."""

    def find_transcripts(self, session_id: str, projects_root: Path) -> list[Path]:
        """Every `<session_id>.jsonl` under `<projects_root>/*/`, in no particular order — the
        caller decides how to pick one. A `projects_root` that does not exist yields no matches
        rather than raising, the same answer as one that exists and holds none."""
        return list(projects_root.glob(f"*/{session_id}.jsonl"))

    def read_lines(self, path: Path) -> list[str] | None:
        """`path`'s lines, or `None` when it cannot be read at all — an absence this reader
        always has a decision for, never an exception every call site would have to catch. The
        read's outcome goes to the ledger observing it (`observe_read`)."""
        outcome = self.read_outcome(path)
        observe_read(outcome)
        return None if outcome.lines is None else list(outcome.lines)

    def read_outcome(self, path: Path) -> TranscriptRead:
        """The same read as `read_lines`, reporting why it failed instead of only that it did.
        The attempt decides: no existence check comes first, so a path that changes between a
        look and a read cannot be misclassified."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except _ABSENCE_ERRORS:
            return TranscriptRead(lines=None, missing=True)
        except OSError:
            return TranscriptRead(lines=None, missing=False)
        return TranscriptRead(lines=tuple(text.splitlines()), missing=False)

    def _search_transcripts(
        self, session_ids: Sequence[str], projects_root: Path
    ) -> TranscriptSearch:
        """Look for each id's transcript in the project directories directly under the root,
        keeping what was found apart from what could not be looked at. A project directory is
        asked for each transcript by name, so one that can be entered but not listed is looked
        in, and an entry by that name is found whatever it turns out to be, as `transcript_for`
        finds it: its own read decides why it could not be read. With no ids to ask for, each
        project directory is listed instead, so a locked one is still named. A root that cannot
        be looked in is the one place that failed: its project directories are not known, and
        none is invented."""
        root_listing = list_directory(projects_root)
        if root_listing.failed:
            return TranscriptSearch(
                matches={},
                incomplete=frozenset(session_ids),
                failed_places=(projects_root,),
            )
        projects = [projects_root / name for name, is_dir in root_listing.entries if is_dir]
        found: dict[str, list[Path]] = {session_id: [] for session_id in session_ids}
        incomplete: set[str] = set()
        failed: set[Path] = set()
        if not found:
            failed.update(project for project in projects if list_directory(project).failed)
        for project in projects:
            for session_id in found:
                candidate = project / f"{session_id}.jsonl"
                try:
                    candidate.lstat()
                except (*_ABSENCE_ERRORS, ValueError):
                    continue
                except OSError:
                    incomplete.add(session_id)
                    failed.add(project)
                    continue
                found[session_id].append(candidate)
        return TranscriptSearch(
            matches={sid: tuple(sorted(paths)) for sid, paths in found.items()},
            incomplete=frozenset(incomplete),
            failed_places=tuple(failed),
        )


_NOT_FOUND_ERROR = (
    "error: could not start `claude agents --json --all` because the system reported that "
    "`claude` was not found — make sure Claude Code's `claude` command is installed and on "
    "PATH, or pass --agents-json a file holding a saved copy of `claude agents --json --all` "
    "output"
)
_OTHER_START_ERROR = (
    "error: could not start `claude agents --json --all` — make sure Claude Code's `claude` "
    "command is installed, on PATH and allowed to run, or pass --agents-json a file holding a "
    "saved copy of `claude agents --json --all` output. The system reported: {reason}"
)


def _start_failure(error: OSError) -> str:
    """The one `error:` line for a process that could not be started. "Not found" says the
    system reported `claude` missing; any other failure carries the system's own reason, with a
    reason spanning several lines folded onto this one."""
    if isinstance(error, FileNotFoundError):
        return _NOT_FOUND_ERROR
    reason = error.strerror or str(error)
    return _OTHER_START_ERROR.format(reason=" ".join(reason.split()))


class Processes:
    """Every process this reader starts."""

    def run(self, argv: list[str]) -> str:
        """`argv`'s stdout, decoded as text. Raises `SystemExit` naming the exit code and the
        captured stderr on a non-zero exit — this reader has no caller above it to report to —
        and, when the process cannot be started at all, one line naming why (`_start_failure`).
        A process that started and ran past its timeout is not a start failure and is not
        caught here."""
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except OSError as exc:
            raise SystemExit(_start_failure(exc)) from exc
        if proc.returncode != 0:
            raise SystemExit(
                f"error: `{' '.join(argv)}` exited {proc.returncode} — fix what it reported, or "
                "pass --agents-json a file holding its saved output. It reported: "
                f"{proc.stderr.strip()}"
            )
        return proc.stdout


# ── Task 8 — the CLI, the docstring and the version ─────────────


def _agents_json_text(
    agents_json_path: str | Path | None, files: Files, processes: Processes
) -> str:
    """The `claude agents --json --all` text this run reads: a saved file when one is given —
    the seam a test uses to shell out to nothing — else the live command.

    A saved file is read through the same two-capability `Files` port `transcript_for` and
    `usage_records` use — `read_lines`, joined back into text — rather than a third filesystem
    reach of its own. A missing saved file is a usage error a caller can act on, so it is
    `SystemExit` here rather than a silent empty ledger.
    """
    if agents_json_path is not None:
        lines = files.read_lines(Path(agents_json_path))
        if lines is None:
            raise SystemExit(
                f"error: could not read {agents_json_path} — check the path given to "
                "--agents-json, or leave --agents-json out to have ka-telemetry run "
                "`claude agents --json --all` itself"
            )
        return "\n".join(lines)
    return processes.run(["claude", "agents", "--json", "--all"])


# ── The composition root: the only place a concrete implementation is constructed ────


@dataclass(frozen=True)
class Reader:
    """The reader's whole environment: both ports it reaches the world through, plus the
    projects root resolved to a concrete `Path` — the one environment reach `build_reader`
    performs itself, because expanding a home directory is a reach into the environment like
    any other and belongs at the composition root, not in an argparse default or a unit's own
    body."""

    files: Files
    processes: Processes
    projects_root: Path
    notes_reader: stream_usage.Reader | None = None


def report_matches(ledger: Ledger, reader: Reader) -> tuple[str, ...]:
    """The sessions this report prints a row for that the telemetry stream holds agent rows for,
    in row order — `()` for a reader with no `notes_reader`, which reads no stream at all."""
    if reader.notes_reader is None:
        return ()
    return transcript_notes.matched_sessions(
        (row.session_id for row in ledger.rows), reader.notes_reader
    )


def build_reader(projects_root: Path | None = None) -> Reader:
    """Wire the real ports. The only place `Files()` or `Processes()` is constructed. A
    `projects_root` of `None` resolves to `~/.claude/projects`, here rather than at an argparse
    default or inside a unit's own body."""
    default_root = Path.home() / ".claude" / "projects"
    return Reader(
        files=Files(),
        processes=Processes(),
        projects_root=projects_root if projects_root is not None else default_root,
        notes_reader=stream_usage.build_reader(),
    )


class CommandParser(argparse.ArgumentParser):
    """The parser every `ka-telemetry` command builds: an option may not be abbreviated, and a
    usage error ends with the next step to take, on its own last line."""

    def __init__(self, *, next_step: str, **kwargs) -> None:
        super().__init__(allow_abbrev=False, **kwargs)
        self._next_step = next_step

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{self._next_step}\n")


def main(argv: list[str] | None = None) -> int:
    parser = CommandParser(
        prog="ka-telemetry runs",
        next_step="Run `ka-telemetry runs --help` to see what it accepts.",
        description=(
            "Reports the tokens each background run used, read from its transcript. The runs "
            "are the background runs that `claude agents --json --all` lists, plus every run "
            "you name with --session. Each model's tokens are shown in separate classes (input, "
            "output, thinking, cache_read, cache_write_1h, cache_write_5m) and never added "
            "together. Thinking tokens are part of output, not added to it."
        ),
    )
    parser.add_argument(
        "--agents-json",
        default=None,
        metavar="FILE",
        help="read the background runs from FILE, a saved copy of `claude agents --json --all` "
        "output (default: run that command)",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="the directory that holds Claude Code's transcripts, as <project folder>/<session "
        "id>.jsonl (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="also report the run with this session id, such as a `claude -p` run that `claude "
        "agents` does not list; repeat the flag to name more runs",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON instead of a table"
    )
    args = parser.parse_args(argv)

    reader = build_reader(args.projects_root)

    text = _agents_json_text(args.agents_json, reader.files, reader.processes)

    try:
        led = read_runs(text, args.session or [], reader)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(render_json(led))
    else:
        print(printable(render(led, matched_sessions=report_matches(led, reader))), end="")
    return 0
