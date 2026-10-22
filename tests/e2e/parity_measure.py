"""One comparison of what `ka-telemetry` prints against the figures the reference set records.

The reference set is `tests/parity` and `tests/stream_parity`: recorded inputs and, under each
one's `expected/`, the nine files that hold the figures the five readers must print. `measure` runs
every invocation the installed-parity test has always made, through an invocation collaborator it is
handed, and returns every difference it finds. A caller that holds the installed command and a
caller that holds only the checkout's source give the collaborator different commands and share
everything else, so the two can never disagree about a figure.

It prints nothing and writes nothing. An empty result means every comparison completed and every
one matched. An input that cannot be read, or a command that did not complete, is a
`MeasurementError`, never an empty result.

The accepted wording adjustments are the existing owners' and are imported, not copied:
`tests/test_parity.py` for the runs expectations and `tests/e2e/test_stream_parity.py` for the
agents and check ones. The runs expectations those adapters build are read from the `tests/parity`
beside them, so a `parity_root` that is not this checkout's own is compared against this checkout's
runs figures.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from e2e.test_stream_parity import (
    _totals_block,
    agents_text_without_declared_lines,
    check_text_without_declared_lines,
    json_without_declared_members,
)
from test_parity import (
    NO_COST_LINE,
    UNNAMED_FIELDS,
    _dev_table_blocks,
    _listed_session_ids,
    _session_of_block,
    _without_statement,
    adjusted_runs_json,
    adjusted_runs_table,
)

PREFIX = "kensai"
COMPARISON = ("kensai.arm", "two-arms-inline", "two-arms-subagent")
STORED_CHECK_SKIPPED_LINES = 1
CONTEXT_SIZES_FILE = "context_sizes.json"
COVERAGE_READ = re.compile(r"^Coverage: (\d+) read", re.MULTILINE)


class MeasurementError(Exception):
    """A measurement that could not be completed: the operation that stopped and the reason.

    `input_path` names the input, as a path from the product's top directory, when what stopped
    it was an input that could not be read; `command` is the command as a person would type it,
    when what stopped it was a command that did not complete. A caller words the two differently.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        *,
        input_path: str | None = None,
        command: str | None = None,
    ) -> None:
        super().__init__(f"{operation}: {reason}")
        self.operation = operation
        self.reason = reason
        self.input_path = input_path
        self.command = command


@dataclass(frozen=True)
class Difference:
    """One comparison that did not match: the expected file, as a path from the product's top
    directory, and the comparison's own account of what differs."""

    expected_file: str
    detail: str


Invoker = Callable[[str, tuple[str, ...]], str]
"""A label for the operation and the arguments after the program; gives the captured standard
output, or raises `MeasurementError` naming the operation and the reason it did not complete."""


def _text_difference(label: str, expected: str, actual: str) -> str:
    return f"{label}:\n" + "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="stored (dev)",
            tofile="ka-telemetry",
        )
    )


def _value_difference(label: str, expected: object, actual: object) -> str:
    def stable(value: object) -> str:
        return json.dumps(value, indent=2, sort_keys=True) + "\n"

    return _text_difference(label, stable(expected), stable(actual))


def _runs_json_with_all_four_fields_cleared() -> str:
    """Dev's stored runs JSON after the four adjustments to dev's side: `name`, `cwd`, `state`
    and `started_at` are null on every row absent from the curated listing. The shared helper
    clears the first three; the fourth is cleared here so the contract holds by construction
    and not by today's stored nulls. Every span and spend figure is left as stored."""
    document = json.loads(adjusted_runs_json())
    listed = _listed_session_ids()
    for row in document["rows"]:
        if row["session_id"] not in listed:
            row["started_at"] = None
    return json.dumps(document, indent=2) + "\n"


def _product_relative(root: Path, name: str) -> str:
    return f"tests/{root.name}/expected/{name}"


def _stored(root: Path, name: str) -> str:
    try:
        return (root / "expected" / name).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MeasurementError(
            f"reading {_product_relative(root, name)}",
            getattr(error, "strerror", None) or str(error),
            input_path=_product_relative(root, name),
        ) from error


def _read_json(root: Path, relative: str):
    try:
        return json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MeasurementError(
            f"reading tests/{root.name}/{relative}",
            getattr(error, "strerror", None) or str(error),
            input_path=f"tests/{root.name}/{relative}",
        ) from error


def _runs_table_rows(text: str) -> tuple[str, list[list[str]]]:
    """A runs table's Coverage line and its row blocks, read past the span statement and the
    closing no-cost line, the way the accepted adjustments read the stored table."""
    unstated = _without_statement(text)
    body, _, note = unstated.rstrip("\n").rpartition("\n")
    header, blocks = _dev_table_blocks(body if note == NO_COST_LINE else unstated)
    return (header[1] if len(header) > 1 else ""), blocks


def _runs_table_set_aside(stored: str, printed: str) -> list[str]:
    """Each figure of the stored runs table that the accepted adjustments do not hold to what
    ka-telemetry printed, compared here: the Coverage line's `read`, which they recompute from
    `total` and `unreadable`, and every row of a session the stored table holds more than once,
    of which they keep the first. ka-telemetry prints one row for a session, so each of those rows'
    figures is held to it, and `read` less the repeated rows is held to the printed `read`."""
    coverage, stored_rows = _runs_table_rows(stored)
    _, printed_rows = _runs_table_rows(printed)
    shown = {_session_of_block(block): block[1:] for block in printed_rows}
    seen: set[str] = set()
    found = []
    for block in stored_rows:
        session_id = _session_of_block(block)
        if session_id not in seen:
            seen.add(session_id)
            continue
        if block[1:] != shown.get(session_id):
            found.append(
                _text_difference(
                    f"runs, the repeated row of session {session_id}",
                    "\n".join(block[1:]) + "\n",
                    "\n".join(shown.get(session_id, [])) + "\n",
                )
            )
    stored_read, printed_read = COVERAGE_READ.search(coverage), COVERAGE_READ.search(printed)
    if stored_read and printed_read:
        repeated = len(stored_rows) - len(seen)
        expected = int(stored_read[1]) - repeated
        if expected != int(printed_read[1]):
            found.append(
                f"runs: the stored Coverage line reads {stored_read[1]} read, which is {expected} "
                f"once its {repeated} repeated rows are taken out, and ka-telemetry reports "
                f"{printed_read[1]} read"
            )
    return found


def _runs_json_set_aside(stored: str, printed: str) -> list[str]:
    """`_runs_table_set_aside` for the JSON form: `coverage.read`, and every member but the
    name, cwd and state of a session's repeated row. Output that does not parse is left to the
    whole-text comparison, which already names the file."""
    try:
        document, shown = json.loads(stored), json.loads(printed)
    except ValueError:
        return []
    if not isinstance(document, dict) or not isinstance(shown, dict):
        return []
    rows = shown.get("rows") if isinstance(shown.get("rows"), list) else []
    printed_rows = {row.get("session_id"): row for row in rows if isinstance(row, dict)}

    def figures(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in UNNAMED_FIELDS}

    seen: set[str] = set()
    found = []
    for row in document.get("rows", []):
        session_id = row.get("session_id")
        if session_id not in seen:
            seen.add(session_id)
            continue
        printed_row = figures(printed_rows.get(session_id, {}))
        if figures(row) != printed_row:
            found.append(
                _value_difference(
                    f"runs --json, the repeated row of session {session_id}",
                    figures(row),
                    printed_row,
                )
            )
    stored_read = document.get("coverage", {}).get("read")
    printed_coverage = shown.get("coverage")
    printed_read = printed_coverage.get("read") if isinstance(printed_coverage, dict) else None
    if isinstance(stored_read, int) and isinstance(printed_read, int):
        repeated = len(document.get("rows", [])) - len(seen)
        if stored_read - repeated != printed_read:
            found.append(
                f"runs --json: the stored coverage reads {stored_read} read, which is "
                f"{stored_read - repeated} once its {repeated} repeated rows are taken out, and "
                f"ka-telemetry reports {printed_read} read"
            )
    return found


def _json_answer(label: str, printed: str):
    try:
        return json.loads(printed)
    except ValueError as error:
        raise MeasurementError(label, f"what it printed is not JSON: {error}") from error


def measure(invoke: Invoker, *, parity_root: Path, stream_root: Path) -> list[Difference]:
    """Every difference between what `invoke` prints over the reference set and what the set
    records. `parity_root` holds `agents.json`, `named_sessions.json`, `projects/` and the five
    runs, steps and context expectations; `stream_root` holds the stream data and the four agents
    and compare expectations."""
    stored = {
        "steps": _stored(parity_root, "steps.txt"),
        "steps --json": _stored(parity_root, "steps.json"),
        "agents": _stored(stream_root, "agents.txt"),
        "agents --json": _stored(stream_root, "agents.json"),
        "agents --compare": _stored(stream_root, "compare.txt"),
        "agents --json --compare": _stored(stream_root, "compare.json"),
    }
    stored_runs = {name: _stored(parity_root, name) for name in ("runs.txt", "runs.json")}
    stored_sizes = json.loads(_stored(parity_root, CONTEXT_SIZES_FILE))
    named = [
        argument
        for session in _read_json(parity_root, "named_sessions.json")
        for argument in ("--session", session)
    ]
    transcripts = {path.stem: path for path in sorted(parity_root.glob("projects/*/*.jsonl"))}

    runs = (
        "runs",
        "--agents-json",
        str(parity_root / "agents.json"),
        "--projects-root",
        str(parity_root / "projects"),
    )
    steps = ("steps", "--projects-root", str(parity_root / "projects"))
    agents = ("agents", "--dir", str(stream_root), "--prefix", PREFIX)
    compare = ("--compare", *COMPARISON)
    check = ("check", "--dir", str(stream_root))

    printed = {
        "steps": invoke("steps", steps),
        "steps --json": invoke("steps --json", (*steps, "--json")),
        "runs": invoke("runs", (*runs, *named)),
        "runs --json": invoke("runs --json", (*runs, *named, "--json")),
        "agents": invoke("agents", agents),
        "agents --json": invoke("agents --json", (*agents, "--json")),
        "agents --compare": invoke("agents --compare", (*agents, *compare)),
        "agents --json --compare": invoke(
            "agents --json --compare", (*agents, "--json", *compare)
        ),
        "check": invoke("check", check),
        "check --json": invoke("check --json", (*check, "--json")),
    }
    sizes = {
        session_id: _json_answer(
            f"context {path.name}",
            invoke(f"context {path.name}", ("context", "--json", str(path))),
        )
        for session_id, path in transcripts.items()
    }

    # Each label's expectation, the file it comes from and what the printed text is reduced to
    # before the two are compared: nothing but the declared additions comes out (see the
    # `test_stream_parity` helpers), and every other byte is held to the stored output.
    comparisons = {
        "steps": ("steps.txt", parity_root, stored["steps"], lambda text: text),
        "steps --json": ("steps.json", parity_root, stored["steps --json"], lambda text: text),
        "runs": ("runs.txt", parity_root, adjusted_runs_table(), lambda text: text),
        "runs --json": (
            "runs.json",
            parity_root,
            _runs_json_with_all_four_fields_cleared(),
            lambda text: text,
        ),
        "agents": (
            "agents.txt",
            stream_root,
            stored["agents"],
            agents_text_without_declared_lines,
        ),
        "agents --json": (
            "agents.json",
            stream_root,
            stored["agents --json"],
            json_without_declared_members,
        ),
        "agents --compare": (
            "compare.txt",
            stream_root,
            stored["agents --compare"],
            agents_text_without_declared_lines,
        ),
        "agents --json --compare": (
            "compare.json",
            stream_root,
            stored["agents --json --compare"],
            json_without_declared_members,
        ),
        "check": (
            "agents.txt",
            stream_root,
            _totals_block(stored["agents"]),
            check_text_without_declared_lines,
        ),
    }
    differences: list[Difference] = []
    for label, (name, root, expected, reduce) in comparisons.items():
        try:
            actual = reduce(printed[label])
        except json.JSONDecodeError as error:
            raise MeasurementError(label, f"what it printed is not JSON: {error}") from error
        except (AssertionError, KeyError, TypeError, IndexError) as error:
            differences.append(Difference(_product_relative(root, name), f"{label}: {error!r}"))
            continue
        if actual != expected:
            differences.append(
                Difference(_product_relative(root, name), _text_difference(label, expected, actual))
            )
    for name, label, set_aside in (
        ("runs.txt", "runs", _runs_table_set_aside),
        ("runs.json", "runs --json", _runs_json_set_aside),
    ):
        differences.extend(
            Difference(_product_relative(parity_root, name), detail)
            for detail in set_aside(stored_runs[name], printed[label])
        )

    stored_checks = json.loads(stored["agents --json"])["metrics_check"]
    try:
        printed_checks = json.loads(
            json_without_declared_members(
                printed["check --json"], check_skipped_lines=STORED_CHECK_SKIPPED_LINES
            )
        )
    except json.JSONDecodeError as error:
        raise MeasurementError("check --json", f"what it printed is not JSON: {error}") from error
    except (AssertionError, KeyError, TypeError, IndexError) as error:
        differences.append(
            Difference(_product_relative(stream_root, "agents.json"), f"check --json: {error!r}")
        )
    else:
        if printed_checks != {"metrics_check": stored_checks}:
            differences.append(
                Difference(
                    _product_relative(stream_root, "agents.json"),
                    _value_difference(
                        "check --json", {"metrics_check": stored_checks}, printed_checks
                    ),
                )
            )

    context_file = _product_relative(parity_root, CONTEXT_SIZES_FILE)
    for session_id, stored_size in stored_sizes.items():
        if session_id not in sizes:
            differences.append(
                Difference(context_file, f"context {session_id}: no transcript in the data set")
            )
            continue
        answer = sizes[session_id]
        if not isinstance(answer, dict):
            raise MeasurementError(f"context {session_id}", "it did not print a JSON object")
        printed_size = answer.get("context_size")
        if printed_size != stored_size:
            differences.append(
                Difference(
                    context_file,
                    _value_difference(f"context {session_id}", stored_size, printed_size),
                )
            )
    for session_id in sorted(set(sizes) - set(stored_sizes)):
        differences.append(
            Difference(context_file, f"context {session_id}: no stored size for this transcript")
        )
    return differences
