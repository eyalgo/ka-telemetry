"""context_gauge — how large a run's context is right now.

A caller that wants to know how full a run's context is needs one number: the size of the context
the run's latest request carried. `run_usage` sums a run's spend per class over the whole
transcript, which is the wrong figure for this: the cache classes repeat on every request, so a
sum over responses counts the same context once per turn. The current context is the **last**
response's `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`, and nothing
else. `output_tokens` is what the response produced, not what it was given, so it is left out.

The de-duplication per `message.id` and the tolerance of a half-written final line are
`run_usage.usage_records`'s, reused rather than restated: a transcript is read while its own run
is still writing to it. This module takes no arguments and prints nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ka_telemetry import run_usage

_CONTEXT_CLASSES = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def current_context(path: Path, files: run_usage.Files) -> int | None:
    """The three input classes of the newest API response in `path`, summed, or `None` when the
    transcript is unreadable or holds no assistant usage.

    A class the response does not carry counts as 0, so a response that never wrote the cache
    still gives a number rather than hiding the ones it did carry.
    """
    latest_usage = None
    for usage, _model, _timestamp in run_usage.usage_records(path, files):
        latest_usage = usage
    if latest_usage is None:
        return None
    return sum(latest_usage.get(name) or 0 for name in _CONTEXT_CLASSES)


@dataclass(frozen=True)
class ContextReading:
    """What one transcript came to for `context`: its `context_size` (`None` when no assistant
    usage was read), how many of its lines could not be read, and `1` in `unreadable_transcripts`
    when something is at the path and it could not be opened. A transcript with nothing at its
    path is `(None, 0, 0)`: absent, which is not a file that could not be opened."""

    context_size: int | None
    unreadable_lines: int = 0
    unreadable_transcripts: int = 0


class _Snapshot:
    """The one completed read of a transcript, handed back as the `read_lines` seam
    `run_usage.usage_records` reads through. It reaches no filesystem, so every pass over the
    snapshot reads memory."""

    def __init__(self, lines: tuple[str, ...]):
        self._lines = lines

    def read_lines(self, path: Path) -> list[str]:
        return list(self._lines)


def read_context(
    path: Path,
    files: run_usage.Files,
    *,
    size_reader: Callable[[Path, run_usage.Files], int | None],
) -> ContextReading:
    """The context size of `path` together with what could not be read, from one read of it.

    The transcript is opened once, through `files.read_outcome`. A completed read is replayed
    from memory twice: `size_reader` — which owns which response sets the size, and is given the
    snapshot as its `files` — gives the size, and `run_usage.usage_records` with a fresh counter
    gives the number of lines it could not read. `size_reader` is required and has no default, so
    the size arithmetic stays where `current_context` keeps it and is never restated here.
    """
    outcome = files.read_outcome(path)
    if outcome.lines is None:
        return ContextReading(context_size=None, unreadable_transcripts=0 if outcome.missing else 1)
    snapshot = _Snapshot(outcome.lines)
    context_size = size_reader(path, snapshot)
    counter = run_usage.UnreadableLines()
    for _record in run_usage.usage_records(path, snapshot, unreadable_lines=counter):
        pass
    return ContextReading(context_size=context_size, unreadable_lines=counter.count)
