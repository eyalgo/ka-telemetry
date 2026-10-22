"""End-to-end parity: the installed command against dev's stored outputs.

`tests/stream_parity/` holds the data set and the four outputs dev's reader produced over it, once
(`tests/test_stream_parity.py` reads them). Here the installed `ka-telemetry agents` and `check`
are run over the same files and must print the same bytes. The stored outputs are the expectation
and are never written: a difference is a defect in the package, shown as a diff, and nothing in
this file regenerates or edits a stored file to make it go away.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import DATA_SET, diff, run

if TYPE_CHECKING:
    from conftest import InstalledProduct

EXPECTED = DATA_SET / "expected"
PREFIX = "kensai"
COMPARISON = ("kensai.arm", "two-arms-inline", "two-arms-subagent")
# The stored outputs are the host reader's bytes and still carry its heading; they are read by this
# prefix. The package's heading is the CURRENT one (CP-383); only the stored side may hold the
# historical one.
CHECK_HEADINGS = ("Metrics cross-check", "No metrics.jsonl found")
HISTORICAL_HEADING = "Metrics cross-check (claude_code.cost.usage vs summed cost_usd):"
CURRENT_HEADING = "Totals check (claude_code.cost.usage vs summed cost_usd):"

# The words and members the reports now carry that the stored outputs do not (`change_text.md`,
# CP-330, CP-383). Each is a declared departure from dev's stored output, tied to the rule that
# causes it. Nothing else may differ: the helpers below remove exactly these and the comparison
# that follows is byte for byte.
COST_MARKING = (
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, "
    "or a total, mean or difference of those estimates. None is a bill."
)
COST_NOTE_MEMBER = "cost_estimate_note"
# CP-330, CP-331: the one member this change adds to a successful JSON report, first.
UNREADABLE_COUNT_MEMBER = "unreadable_metric_lines"
# CP-353, CP-362: the member every successful JSON report gains, second, for a `metrics.jsonl` that
# was read. Over the stored data set it is exactly `{"state": "read"}`, and nothing else.
METRICS_FILE_MEMBER = "metrics_file"
METRICS_FILE_READ = {"state": "read"}
# CP-356, CP-357: what `check` now states of the `logs.jsonl` lines it could not read. The stored
# data set holds one such line. The text line comes first in the block unless a `metrics.jsonl`
# line precedes it; the member is `check --json`'s, directly after the `metrics.jsonl` count.
LOGS_COVERAGE_LINE = (
    "Unreadable lines in logs.jsonl, skipped and left out of the totals check: {count}."
)
SKIPPED_LINES_MEMBER = "skipped_lines"
STORED_SKIPPED_LINES = 1
# CP-338: the one stored session line that goes on to name its causes. The stored line is the whole
# prefix; the causes are what the line adds.
NO_METRIC_SESSION = "7931ae7c-d999-4286-b091-261680002258"
NO_METRIC_STORED_LINE = (
    f"  session={NO_METRIC_SESSION}  log_cost_usd=0.050333  no metric export"
)
NO_METRIC_CAUSES = [
    "the session may have ended before Claude Code exported a cost metric for it",
    "the session may still be running",
    "the receiver (ka-telemetry receive) may not have been running when Claude Code sent the "
    "cost metric",
]
NO_METRIC_REASON = (
    " — possible causes, which ka-telemetry cannot tell apart: the session may have ended before "
    "Claude Code exported a cost metric for it; the session may still be running; the receiver "
    "(ka-telemetry receive) may not have been running when Claude Code sent the cost metric"
)
# CP-340: the one stored session line outside 1%.
MISMATCH_SESSION = "ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593"
MISMATCH_STORED_LINE = (
    f"  session={MISMATCH_SESSION}  log_cost_usd=0.950151  metric_cost_usd=0.727485  MISMATCH >1%"
)
MISMATCH_CAUSES = [
    "the session may still be running, or may have ended before Claude Code exported all of it, "
    "so one of its two totals may not have caught up with the other",
    "the receiver (ka-telemetry receive) may not have been running for part of the session, so "
    "some of what Claude Code sent may never have arrived",
    "Claude Code's request costs and its cost metric may really disagree",
]
MISMATCH_REASON = (
    " — possible causes, which ka-telemetry cannot tell apart: the session may still be running, "
    "or may have ended before Claude Code exported all of it, so one of its two totals may not "
    "have caught up with the other; the receiver (ka-telemetry receive) may not have been running "
    "for part of the session, so some of what Claude Code sent may never have arrived; Claude "
    "Code's request costs and its cost metric may really disagree"
)
CAUSES_MEMBER = "possible_causes"


def _stored(name: str) -> str:
    return (EXPECTED / name).read_text()


def _manifest_hashes() -> dict[str, str]:
    return json.loads((DATA_SET / "manifest.json").read_text())["hashes"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _agents(installed, home, *arguments: str) -> str:
    done = run(
        installed, "agents", "--dir", str(DATA_SET), "--prefix", PREFIX, *arguments, home=home
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _totals_block(table: str) -> str:
    """The lines from the check's heading to the line before the next empty one, and a newline."""
    lines = table.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(CHECK_HEADINGS))
    end = next((i for i in range(start, len(lines)) if lines[i] == ""), len(lines))
    return "\n".join(lines[start:end]) + "\n"


def _heading_as_stored(lines: list[str]) -> list[str]:
    """CP-383, CP-391: `lines` with the one declared heading departure undone. The package's actual
    output must hold the complete current heading exactly once and no line of the historical
    name; that one line is then written back as the stored heading so the comparison that follows
    stays byte for byte. A missing, repeated, reworded, legacy or respaced heading is not
    translated, so the assertion fails on it."""
    found = [i for i, line in enumerate(lines) if line == CURRENT_HEADING + "\n"]
    assert len(found) == 1, f"CP-383: the heading must be printed once, found {found}"
    legacy = [line for line in lines if line.lower().startswith("metrics cross-check")]
    assert not legacy, f"CP-391: the historical heading is the oracle's only, got {legacy!r}"
    restored = list(lines)
    restored[found[0]] = HISTORICAL_HEADING + "\n"
    return restored


def current_heading_in(stored_block: str) -> str:
    """A stored text with its historical heading line written as the package now prints it."""
    assert stored_block.count(HISTORICAL_HEADING + "\n") == 1
    return stored_block.replace(HISTORICAL_HEADING + "\n", CURRENT_HEADING + "\n", 1)


def _cut_declared_causes(lines: list[str]) -> list[str]:
    """`lines` with the causes at the end of the CP-338 and CP-340 lines of the two named sessions
    cut off. Each line must be printed once and end in its causes exactly: a missing, reworded or
    reordered cause is not cut, so the assertion fails on it. What stays is the printed prefix,
    whatever it holds, so the comparison that follows still catches a changed figure."""
    cut = list(lines)
    for session, reason in (
        (NO_METRIC_SESSION, NO_METRIC_REASON),
        (MISMATCH_SESSION, MISMATCH_REASON),
    ):
        session_line = f"  session={session}  log_cost_usd="
        found = [i for i, line in enumerate(cut) if line.startswith(session_line)]
        assert len(found) == 1, f"CP-338: the session line must be printed once, found {found}"
        assert cut[found[0]].endswith(reason + "\n"), (
            f"CP-338: the line must end in its causes, got {cut[found[0]]!r}"
        )
        cut[found[0]] = cut[found[0]][: -len(reason) - 1] + "\n"
    return cut


def agents_text_without_declared_lines(printed: str) -> str:
    """`agents` text with the one line this change adds taken out: the CP-287 marking, which must
    be present, once, directly after `Total cost_usd:`."""
    lines = printed.splitlines(keepends=True)
    marks = [i for i, line in enumerate(lines) if line == COST_MARKING + "\n"]
    assert len(marks) == 1, f"CP-287: the cost marking must be printed once, found {len(marks)}"
    assert lines[marks[0] - 1].startswith("Total cost_usd: "), (
        "CP-287: the cost marking must stand directly after the Total cost_usd line"
    )
    del lines[marks[0]]
    return "".join(_cut_declared_causes(_heading_as_stored(lines)))


def check_text_without_declared_lines(
    printed: str, *, skipped_lines: int = STORED_SKIPPED_LINES
) -> str:
    """`check` text with the lines this change adds taken out: the CP-288 marking, which must be
    present, once, as the last line, and the CP-356 `logs.jsonl` count line, which must be present
    once with `skipped_lines` as its count (the stored data set's), and absent when that is 0."""
    lines = printed.splitlines(keepends=True)
    assert lines and lines[-1] == COST_MARKING + "\n", (
        "CP-288: the cost marking must be the last line of the check"
    )
    assert lines.count(COST_MARKING + "\n") == 1, "CP-288: the cost marking must be printed once"
    counted = [
        i for i, line in enumerate(lines) if line.startswith("Unreadable lines in logs.jsonl")
    ]
    if skipped_lines:
        expected_line = LOGS_COVERAGE_LINE.format(count=skipped_lines) + "\n"
        assert [lines[i] for i in counted] == [expected_line], (
            f"CP-356: the logs.jsonl count line must be printed once, stating {skipped_lines}"
        )
        del lines[counted[0]]
    else:
        assert not counted, "CP-356: a count of 0 prints no logs.jsonl line"
    return "".join(_cut_declared_causes(_heading_as_stored(lines[:-1])))


def json_without_declared_members(
    printed: str, *, unreadable_metric_lines: int = 0, check_skipped_lines: int | None = None
) -> str:
    """A JSON report with the members this change adds taken out, and nothing else changed:
    CP-330 — the count, first, equal to `unreadable_metric_lines` (0 over the stored data set) —
    CP-353, CP-362 — `metrics_file`, directly after the count, exactly `{"state": "read"}` — CP-289
    — the cost note, last, and the marking's words — and, for `check --json` only, CP-357 —
    `skipped_lines`, directly after `metrics_file`, equal to `check_skipped_lines`."""
    document = json.loads(printed)
    serialized = json.dumps(document, indent=2) + "\n"
    assert printed == serialized, diff(serialized, printed)
    assert list(document)[0] == UNREADABLE_COUNT_MEMBER, (
        f"CP-330: {UNREADABLE_COUNT_MEMBER} must be first"
    )
    assert document[UNREADABLE_COUNT_MEMBER] == unreadable_metric_lines, (
        f"CP-330: {UNREADABLE_COUNT_MEMBER} is {unreadable_metric_lines}"
    )
    assert list(document)[-1] == COST_NOTE_MEMBER, f"CP-289: {COST_NOTE_MEMBER} must be last"
    assert document[COST_NOTE_MEMBER] == COST_MARKING, f"CP-289: {COST_NOTE_MEMBER} is the marking"
    assert list(document)[1] == METRICS_FILE_MEMBER, (
        f"CP-353: {METRICS_FILE_MEMBER} must follow {UNREADABLE_COUNT_MEMBER}"
    )
    assert document[METRICS_FILE_MEMBER] == METRICS_FILE_READ, (
        f"CP-362: {METRICS_FILE_MEMBER} is exactly {METRICS_FILE_READ}"
    )
    del document[UNREADABLE_COUNT_MEMBER], document[COST_NOTE_MEMBER]
    del document[METRICS_FILE_MEMBER]
    if check_skipped_lines is not None:
        assert list(document)[0] == SKIPPED_LINES_MEMBER, (
            f"CP-357: {SKIPPED_LINES_MEMBER} must follow {METRICS_FILE_MEMBER}"
        )
        assert document[SKIPPED_LINES_MEMBER] == check_skipped_lines, (
            f"CP-357: {SKIPPED_LINES_MEMBER} is {check_skipped_lines}"
        )
        del document[SKIPPED_LINES_MEMBER]
    for entry in document.get("metrics_check") or []:
        _cut_declared_member(entry)
    return json.dumps(document, indent=2) + "\n"


def _cut_declared_member(entry: dict) -> None:
    """CP-342, CP-343: `possible_causes` is the entry's last member, a list, and holds the causes
    of the state the entry's own figures are in; then it is cut and the other four stay."""
    assert list(entry)[-1] == CAUSES_MEMBER, f"CP-343: {CAUSES_MEMBER} must be the last member"
    if entry["metric_cost_usd"] is None:
        expected = NO_METRIC_CAUSES
    elif entry["within_tolerance"] is False:
        expected = MISMATCH_CAUSES
    else:
        expected = []
    assert entry[CAUSES_MEMBER] == expected, (
        f"CP-342: {entry['session_id']} must carry {expected!r}, got {entry[CAUSES_MEMBER]!r}"
    )
    del entry[CAUSES_MEMBER]


def test_the_oracle_files_the_command_is_held_to_are_the_recorded_ones():
    hashes = _manifest_hashes()

    for name in ("logs.jsonl", "metrics.jsonl", *(f"expected/{n}" for n in (
        "agents.txt", "agents.json", "compare.txt", "compare.json"
    ))):
        assert hashlib.sha256((DATA_SET / name).read_bytes()).hexdigest() == hashes[name], name


def test_installed_agents_output_equals_stored_oracle(installed, home):
    table = _agents(installed, home)
    as_json = _agents(installed, home, "--json")
    compared = _agents(installed, home, "--compare", *COMPARISON)
    compared_json = _agents(installed, home, "--json", "--compare", *COMPARISON)

    for name, printed in (
        ("agents.txt", agents_text_without_declared_lines(table)),
        ("agents.json", json_without_declared_members(as_json)),
        ("compare.txt", agents_text_without_declared_lines(compared)),
        ("compare.json", json_without_declared_members(compared_json)),
    ):
        assert printed == _stored(name), diff(_stored(name), printed)


def test_installed_check_equals_stored_totals_block(installed, home):
    stored_table = _stored("agents.txt")

    text = run(installed, "check", "--dir", str(DATA_SET), home=home)
    as_json = run(installed, "check", "--dir", str(DATA_SET), "--json", home=home)

    assert text.returncode == 0 and as_json.returncode == 0
    printed_text = check_text_without_declared_lines(text.stdout)
    assert printed_text == _totals_block(stored_table), diff(
        _totals_block(stored_table), printed_text
    )
    stored_checks = json.loads(_stored("agents.json"))["metrics_check"]
    printed_json = json_without_declared_members(
        as_json.stdout, check_skipped_lines=STORED_SKIPPED_LINES
    )
    expected_json = json.dumps({"metrics_check": stored_checks}, indent=2) + "\n"
    assert printed_json == expected_json, diff(expected_json, printed_json)
    assert list(json.loads(as_json.stdout)) == [
        UNREADABLE_COUNT_MEMBER,
        METRICS_FILE_MEMBER,
        SKIPPED_LINES_MEMBER,
        "metrics_check",
        COST_NOTE_MEMBER,
    ]


def test_the_compare_blocks_stored_beside_the_table_are_the_table_plus_the_comparison():
    """The compare outputs were produced by the same run as the table, so they begin with it: a
    check that the stored files are what this file takes them for."""
    assert _stored("compare.txt").startswith(_stored("agents.txt"))


def test_a_cost_difference_of_one_millionth_shows_in_the_failure_diff():
    stored = _stored("agents.txt")
    changed = stored.replace("cost_usd=0.950151", "cost_usd=0.950152", 1)
    assert changed != stored

    shown = diff(stored, changed)

    assert "-  session=ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593" in shown
    assert "cost_usd=0.950151" in shown and "cost_usd=0.950152" in shown


def _stored_check_text() -> str:
    """What `check` prints over the stored data set once this change's lines are in."""
    return LOGS_COVERAGE_LINE.format(count=STORED_SKIPPED_LINES) + "\n" + current_heading_in(
        _totals_block(_stored("agents.txt"))
    ) + COST_MARKING + "\n"


def _stored_check_json() -> str:
    document = {
        UNREADABLE_COUNT_MEMBER: 0,
        METRICS_FILE_MEMBER: METRICS_FILE_READ,
        SKIPPED_LINES_MEMBER: STORED_SKIPPED_LINES,
        "metrics_check": json.loads(_stored("agents.json"))["metrics_check"],
        COST_NOTE_MEMBER: COST_MARKING,
    }
    return json.dumps(document, indent=2) + "\n"


def _with_causes_in_place(text: str) -> str:
    """The stored totals block with the two named sessions' causes written back on."""
    return text.replace(NO_METRIC_STORED_LINE, NO_METRIC_STORED_LINE + NO_METRIC_REASON).replace(
        MISMATCH_STORED_LINE, MISMATCH_STORED_LINE + MISMATCH_REASON
    )


def test_the_check_text_normalisation_takes_out_the_logs_count_line_and_nothing_else():
    printed = _with_causes_in_place(_stored_check_text())

    assert check_text_without_declared_lines(printed) == _totals_block(_stored("agents.txt"))


@pytest.mark.parametrize(
    "damage",
    ["missing line", "wrong count", "line twice", "line reworded"],
)
def test_the_check_text_normalisation_rejects_a_missing_or_wrong_logs_count_line(damage):
    printed = _with_causes_in_place(_stored_check_text())
    line = LOGS_COVERAGE_LINE.format(count=STORED_SKIPPED_LINES) + "\n"
    damaged = {
        "missing line": printed.replace(line, "", 1),
        "wrong count": printed.replace(line, LOGS_COVERAGE_LINE.format(count=2) + "\n", 1),
        "line twice": line + printed,
        "line reworded": printed.replace(line, line.replace("skipped", "ignored"), 1),
    }[damage]

    with pytest.raises(AssertionError):
        check_text_without_declared_lines(damaged)


@pytest.mark.parametrize(
    "damage",
    ["missing member", "wrong count", "member moved last-but-one", "member renamed"],
)
def test_the_check_json_normalisation_rejects_a_missing_or_wrong_skipped_lines(damage):
    document = json.loads(_stored_check_json())
    for entry in document["metrics_check"]:
        entry["possible_causes"] = (
            NO_METRIC_CAUSES if entry["metric_cost_usd"] is None
            else MISMATCH_CAUSES if entry["within_tolerance"] is False else []
        )
    if damage == "missing member":
        del document[SKIPPED_LINES_MEMBER]
    elif damage == "wrong count":
        document[SKIPPED_LINES_MEMBER] = 2
    elif damage == "member moved last-but-one":
        document = {
            UNREADABLE_COUNT_MEMBER: 0,
            METRICS_FILE_MEMBER: METRICS_FILE_READ,
            "metrics_check": document["metrics_check"],
            SKIPPED_LINES_MEMBER: STORED_SKIPPED_LINES,
            COST_NOTE_MEMBER: COST_MARKING,
        }
    else:
        document["skipped"] = document.pop(SKIPPED_LINES_MEMBER)
    printed = json.dumps(document, indent=2) + "\n"

    with pytest.raises(AssertionError):
        json_without_declared_members(printed, check_skipped_lines=STORED_SKIPPED_LINES)


@pytest.mark.parametrize(
    "damage",
    ["missing member", "wrong state", "extra member", "member moved last", "member renamed"],
)
def test_the_json_normalisation_rejects_a_missing_or_wrong_metrics_file_member(damage):
    """CP-353, CP-362: `metrics_file` is second and exactly `{"state": "read"}` over the stored
    set; removing it is the one thing the normalisation does to it."""
    document = json.loads(_stored_check_json())
    for entry in document["metrics_check"]:
        entry["possible_causes"] = (
            NO_METRIC_CAUSES if entry["metric_cost_usd"] is None
            else MISMATCH_CAUSES if entry["within_tolerance"] is False else []
        )
    if damage == "missing member":
        del document[METRICS_FILE_MEMBER]
    elif damage == "wrong state":
        document[METRICS_FILE_MEMBER] = {"state": "nothing_received"}
    elif damage == "extra member":
        document[METRICS_FILE_MEMBER] = {"state": "read", "reason": None}
    elif damage == "member moved last":
        document[METRICS_FILE_MEMBER] = document.pop(METRICS_FILE_MEMBER)
    else:
        document["metrics"] = document.pop(METRICS_FILE_MEMBER)
    printed = json.dumps(document, indent=2) + "\n"

    with pytest.raises(AssertionError):
        json_without_declared_members(printed, check_skipped_lines=STORED_SKIPPED_LINES)


def _stored_agents_text() -> str:
    """What `agents` would print over the stored data set: the package's heading, the marking and
    the two sessions' causes in place."""
    text = current_heading_in(_stored("agents.txt"))
    lines = text.splitlines(keepends=True)
    total = next(i for i, line in enumerate(lines) if line.startswith("Total cost_usd: "))
    marked = "".join([*lines[: total + 1], COST_MARKING + "\n", *lines[total + 1 :]])
    return _with_causes_in_place(marked)


def test_parity_accepts_the_complete_new_heading_once_and_restores_the_stored_one():
    assert agents_text_without_declared_lines(_stored_agents_text()) == _stored("agents.txt")
    assert check_text_without_declared_lines(_with_causes_in_place(_stored_check_text())) == (
        _totals_block(_stored("agents.txt"))
    )


@pytest.mark.parametrize("command", ["agents", "check"])
@pytest.mark.parametrize(
    "damage",
    [
        "heading missing",
        "heading twice",
        "heading reworded",
        "historical heading in the actual output",
        "both headings",
        "extra text after the heading",
        "changed spacing",
        "changed case",
    ],
)
def test_parity_accepts_only_the_complete_new_heading_once(command, damage):
    printed = _stored_agents_text() if command == "agents" else _with_causes_in_place(
        _stored_check_text()
    )
    current = CURRENT_HEADING + "\n"
    damaged = {
        "heading missing": printed.replace(current, "", 1),
        "heading twice": printed.replace(current, current + current, 1),
        "heading reworded": printed.replace(current, current.replace("Totals", "Total"), 1),
        "historical heading in the actual output": printed.replace(
            current, HISTORICAL_HEADING + "\n", 1
        ),
        "both headings": printed.replace(current, HISTORICAL_HEADING + "\n" + current, 1),
        "extra text after the heading": printed.replace(current, current[:-1] + " extra\n", 1),
        "changed spacing": printed.replace(current, current.replace("vs summed", "vs  summed"), 1),
        "changed case": printed.replace(current, current.replace("Totals", "totals"), 1),
    }[damage]
    assert damaged != printed
    cut = (
        agents_text_without_declared_lines if command == "agents"
        else check_text_without_declared_lines
    )

    with pytest.raises(AssertionError):
        cut(damaged)


@pytest.mark.parametrize("command", ["agents", "check"])
def test_parity_rejects_a_changed_figure_alongside_the_renamed_heading(command):
    printed = _stored_agents_text() if command == "agents" else _with_causes_in_place(
        _stored_check_text()
    )
    damaged = printed.replace("cost_usd=0.950151", "cost_usd=0.950152", 1)
    assert damaged != printed
    cut = (
        agents_text_without_declared_lines if command == "agents"
        else check_text_without_declared_lines
    )

    translated = cut(damaged)

    stored = _stored("agents.txt") if command == "agents" else _totals_block(_stored("agents.txt"))
    assert translated != stored, "the comparison must still see the one-millionth difference"
