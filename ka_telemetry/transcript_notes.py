"""ka_telemetry.transcript_notes — the lines the transcript reports end with about cost.

WHY THIS EXISTS
    `runs` and `steps` read tokens off transcripts, and a transcript carries no cost. Each report
    says so in one line, and where the telemetry stream holds agent rows for a session the report
    shows, points at the command that shows that session's cost. Two reports need the same lookup
    and the same words, so they live here once rather than in each report.

    A session is **matched** when `ka-telemetry agents --where session.id=<id>` would show at
    least one row for it. This module answers that with the stream reader's own file port, decoder
    and `agent_rows`, on one read of `logs.jsonl`; it duplicates none of them.

The lookup is best-effort, and says so: the stream only decides whether a pointer line prints.
An absent, empty or unreadable stream, a line it cannot decode, and any failure while decoding or
selecting all come to the same answer, no match, with nothing written to stderr. That tolerance
belongs to this optional seam alone; the transcript readers never share it.

Imports: the standard library and `ka_telemetry.stream_usage`. Neither transcript reader is
imported here, so both may import this.
"""

from __future__ import annotations

from collections.abc import Iterable

from ka_telemetry import stream_usage

__version__ = "0.1.0"

NO_COST_LINE = (
    "This report carries no cost, because ka-telemetry never computes a cost from tokens."
)
POINTER_LINE = (
    "Run `ka-telemetry agents --where session.id={session_id}` to see Claude Code's cost "
    "estimate for session {session_id}."
)
SPAN_LINE = (
    "Span: wall-clock time from the first to the last model response counted for the run or step, "
    "not from when it started; one response reads 0s, and n/a means no counted response carried "
    "a readable time."
)
SESSION_ID_KEY = "session.id"


def matched_sessions(session_ids: Iterable[str], reader: stream_usage.Reader) -> tuple[str, ...]:
    """The distinct ids among `session_ids`, in first-occurrence order, that `reader`'s stream
    holds at least one agent row for — the same `session.id` filter `agents --where` applies,
    with no label prefix.

    Best-effort by contract: `logs.jsonl` is read once and parsed once however many ids are
    asked, no metrics are read and no process is started, and anything that goes wrong reading or
    decoding the stream — an exception included — answers "no match" and prints nothing. Nothing
    is read when no id is asked about.
    """
    asked = tuple(dict.fromkeys(session_ids))
    if not asked:
        return ()
    try:
        logs = reader.files.read_outcome(reader.directory / stream_usage.LOGS_FILE)
        if logs.state is not stream_usage.ReadState.READ:
            return ()
        records = stream_usage.read_log_lines(list(logs.lines)).records
        return tuple(
            session_id
            for session_id in asked
            if stream_usage.agent_rows(records, ((SESSION_ID_KEY, session_id),))
        )
    except Exception:
        return ()


def note_lines(matched: Iterable[str]) -> tuple[str, ...]:
    """The note block: the no-cost line, then one pointer line for each id in `matched`, in the
    order given. Pure — no I/O — and no line carries a trailing newline."""
    return (NO_COST_LINE, *(POINTER_LINE.format(session_id=sid) for sid in matched))


def append_notes(report: str, matched: Iterable[str]) -> str:
    """`report` with the note block directly under it — no blank line between — ending in one
    newline. Pure: the one place both text reports join their body to the block."""
    return report.rstrip("\n") + "\n" + "\n".join(note_lines(matched)) + "\n"
