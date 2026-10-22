"""cli — the one `ka-telemetry` command: `runs`, `steps`, `context`, `receive`, `agents`, `check`,
`turn-on`.

`runs`, `steps`, `receive`, `agents`, `check` and `turn-on` hand their arguments to the module's
own `main`, so each keeps its own parser and nothing is parsed twice. `context` is the one command
with no `main` of its own: it wires the filesystem port here, calls `context_gauge.read_context`
with `context_gauge.current_context` as its size reader, and prints the size and what could not be
read. No counting, grouping, parsing or storing is done in this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ka_telemetry import context_gauge, flow_usage, receiver, run_usage, stream_usage, turn_on

_NETWORK_PROMISE = (
    "This tool sends no telemetry and makes no network call except the ones the job you "
    "asked for requires."
)


def _top_parser() -> run_usage.CommandParser:
    """The parser for `ka-telemetry` itself. It lists the commands and rejects what is not one;
    a command's own arguments belong to the command's parser, built where the command is run."""
    parser = run_usage.CommandParser(
        prog="ka-telemetry",
        next_step="Run `ka-telemetry --help` to see the commands and what each one accepts.",
        description=(
            "Reads the transcripts Claude Code keeps on this machine and reports the tokens "
            "they record: per background run, per workflow step, and the current context size "
            "of one transcript. Those three reports show no cost, because ka-telemetry counts "
            "tokens and does not price them. It also receives the telemetry Claude Code can "
            "send as OpenTelemetry over HTTP in JSON, writes it to files on this machine, and "
            "reads those files for a per-agent report of tokens, cost and duration, and for a "
            "totals check that holds each session's request costs against Claude Code's cost "
            "metric. Every cost in those two comes from what Claude Code reported; "
            "ka-telemetry never computes a cost from tokens."
        ),
        epilog=_NETWORK_PROMISE,
    )
    commands = parser.add_subparsers(
        title="commands",
        dest="command",
        required=True,
        metavar="{runs,steps,context,receive,agents,check,turn-on}",
        parser_class=argparse.ArgumentParser,
    )
    commands.add_parser(
        "runs",
        add_help=False,
        help="tokens used by each background run, and by each run you name by session id",
    )
    commands.add_parser(
        "steps", add_help=False, help="tokens used by each step of a workflow run"
    )
    commands.add_parser(
        "context", add_help=False, help="the current context size of one transcript"
    )
    commands.add_parser(
        "receive",
        add_help=False,
        help="receive the telemetry Claude Code sends and write it to files on this machine",
    )
    commands.add_parser(
        "agents",
        add_help=False,
        help="tokens, cost and duration per agent, read from the received telemetry",
    )
    commands.add_parser(
        "check",
        add_help=False,
        help="each session's request costs checked against Claude Code's cost metric",
    )
    commands.add_parser(
        "turn-on",
        add_help=False,
        help="write the keys that make Claude Code send its telemetry to the receiver on this "
        "machine into a Claude Code settings file, remove them, or print them for one run",
    )
    return parser


def _context_parser() -> run_usage.CommandParser:
    parser = run_usage.CommandParser(
        prog="ka-telemetry context",
        next_step="Run `ka-telemetry context --help` to see what it accepts.",
        description=(
            "Reports the current context size of one Claude Code transcript: the input, "
            "cache-read and cache-write tokens of the last response in it, added together. "
            "Output tokens are not part of it."
        ),
    )
    parser.add_argument(
        "transcript",
        metavar="TRANSCRIPT",
        help="path to a transcript file, named <session id>.jsonl",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the result as JSON instead of a line of text"
    )
    return parser


def build_files() -> run_usage.Files:
    """The filesystem port the context command reads through. The one place this module makes it."""
    return run_usage.Files()


def context_size_reader(path: Path, files: run_usage.Files) -> int | None:
    """The size reader `read_context` is wired with: the scalar `current_context`, so the size
    arithmetic stays in `context_gauge` and is never restated here."""
    return context_gauge.current_context(path, files)


# The words are `change_text.md`'s, each line whole so none is assembled from fragments.
_CONTEXT_SIZE_KNOWN = "Context size: {size} tokens"
_CONTEXT_SIZE_UNKNOWN = "Context size: unknown — no assistant usage read from {path}"
_CONTEXT_LINES_SKIPPED_ONE = " — 1 unreadable line skipped in this transcript"
_CONTEXT_LINES_SKIPPED_MANY = " — {line_count} unreadable lines skipped in this transcript"
_CONTEXT_NOT_OPENED = (
    "Context size: unknown — the transcript {path} could not be opened. "
    "Run this command again once it can be read."
)


def _context_text(reading: context_gauge.ContextReading, transcript: str) -> str:
    if reading.unreadable_transcripts:
        return _CONTEXT_NOT_OPENED.format(path=transcript)
    if reading.context_size is None:
        line = _CONTEXT_SIZE_UNKNOWN.format(path=transcript)
    else:
        line = _CONTEXT_SIZE_KNOWN.format(size=reading.context_size)
    if reading.unreadable_lines == 1:
        line += _CONTEXT_LINES_SKIPPED_ONE
    elif reading.unreadable_lines > 1:
        line += _CONTEXT_LINES_SKIPPED_MANY.format(line_count=reading.unreadable_lines)
    return line


def _context(argv: list[str]) -> int:
    args = _context_parser().parse_args(argv)
    reading = context_gauge.read_context(
        Path(args.transcript), build_files(), size_reader=context_size_reader
    )

    if args.json:
        payload = {
            "transcript": args.transcript,
            "context_size": reading.context_size,
            "unreadable_lines": reading.unreadable_lines,
        }
        if reading.unreadable_transcripts:
            payload["unreadable_transcripts"] = reading.unreadable_transcripts
        print(json.dumps(payload, indent=2))
    else:
        print(_context_text(reading, args.transcript))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command, rest = (args[0], args[1:]) if args else (None, [])

    if command == "runs":
        return run_usage.main(rest)
    if command == "steps":
        return flow_usage.main(rest)
    if command == "context":
        return _context(rest)
    if command == "receive":
        return receiver.main(rest)
    if command == "agents":
        return stream_usage.main(rest)
    if command == "check":
        return stream_usage.check_main(rest)
    if command == "turn-on":
        return turn_on.main(rest)

    # Nothing to run: a help request, or a usage error (no command, an unknown one, a stray flag).
    _top_parser().parse_args(args)
    return 0
