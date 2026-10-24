"""End-to-end tests of the installed `ka-telemetry agents --compare ATTR A B` naming the values it
could not read as numbers (CP-667–CP-680).

The command is driven from the scratch virtualenv the `installed` fixture builds, under a `HOME` of
its own, on real temporary streams written as `logs.jsonl` lines, as
`tests/e2e/test_agent_invalid_values.py` does for the per-agent rows. Each request is built from a
valid base; a test names the envelope one figure arrives in, or leaves it absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import run

if TYPE_CHECKING:
    from conftest import InstalledProduct

ABSENT = object()

FIGURES = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
)
# The valid value each figure carries in a base request.
BASE = {
    "input_tokens": {"intValue": 3},
    "output_tokens": {"intValue": 4},
    "cache_read_tokens": {"intValue": 5},
    "cache_creation_tokens": {"intValue": 6},
    "cost_usd": {"doubleValue": 0.5},
    "duration_ms": {"intValue": 7},
}
BASE_FIGURE = {
    "input_tokens": 3,
    "output_tokens": 4,
    "cache_read_tokens": 5,
    "cache_creation_tokens": 6,
    "cost_usd": 0.5,
    "duration_ms": 7,
}
# The side members the command prints today, in order.
SIDE_MEMBERS = ["run_count", "mean_cost_per_run", "total_cost_usd", "totals", "mean_per_run"]


def _request(arm: str, session: str, **figures) -> dict:
    """One `api_request` of side `arm`: the base figures, each replaced by the envelope given
    (`ABSENT` leaves the figure out of the record altogether)."""
    merged = {**BASE, **figures}
    attributes = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "api_request"}},
        {"key": "model", "value": {"stringValue": "claude-model"}},
        {"key": "query_source", "value": {"stringValue": "sdk"}},
        {"key": "arm", "value": {"stringValue": arm}},
    ]
    attributes.extend(
        {"key": name, "value": value} for name, value in merged.items() if value is not ABSENT
    )
    return {"attributes": attributes}


def _stream(directory: Path, requests: list[dict], *, resource: dict | None = None) -> Path:
    """`directory` holding a `logs.jsonl` with one export carrying `requests`."""
    directory.mkdir(exist_ok=True)
    resource_attributes = [{"key": k, "value": v} for k, v in (resource or {}).items()]
    body = {
        "resourceLogs": [
            {
                "resource": {"attributes": resource_attributes},
                "scopeLogs": [{"logRecords": requests}],
            }
        ]
    }
    (directory / "logs.jsonl").write_text(json.dumps({"signal": "logs", "body": body}) + "\n")
    return directory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _compared(installed: InstalledProduct, home: Path, stream: Path, *extra: str):
    """The text report and the parsed JSON document of `agents --compare arm A B` on `stream`;
    both exit 0 with an empty stderr."""
    arguments = ("agents", "--dir", str(stream), *extra, "--compare", "arm", "A", "B")
    text = run(installed, *arguments, home=home)
    as_json = run(installed, *arguments, "--json", home=home)
    for result in (text, as_json):
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    return text.stdout, json.loads(as_json.stdout)


def _compare_block(text: str) -> list[str]:
    """The lines of the `--compare` block, which closes the report."""
    return text[text.index("--compare arm A B:") :].splitlines()


def _probe(directory: Path) -> Path:
    """The five-request probe of the spec: side `A` over sessions `s1` (twice) and `s2`, side `B`
    over `s3` twice."""
    valid = {
        "input_tokens": {"intValue": 10},
        "output_tokens": {"intValue": 20},
        "cache_read_tokens": {"intValue": 30},
        "cache_creation_tokens": {"intValue": 40},
        "cost_usd": {"doubleValue": 0.1},
        "duration_ms": {"intValue": 100},
    }
    invalid = {
        "input_tokens": {"stringValue": "abc"},
        "output_tokens": {"boolValue": True},
        "cache_read_tokens": {"intValue": "oops"},
        "cache_creation_tokens": {"stringValue": ""},
        "cost_usd": {"boolValue": False},
        "duration_ms": {"stringValue": "slow"},
    }
    second = {
        "input_tokens": {"stringValue": "5"},
        "output_tokens": ABSENT,
        "cache_read_tokens": {"intValue": 7},
        "cache_creation_tokens": {"intValue": 8},
        "cost_usd": {"doubleValue": 0.3},
        "duration_ms": {"intValue": 50},
    }
    side_b = {
        "input_tokens": {"intValue": 1},
        "output_tokens": {"intValue": 2},
        "cache_read_tokens": {"intValue": 3},
        "cache_creation_tokens": {"intValue": 4},
        "duration_ms": {"intValue": 60},
    }
    return _stream(
        directory,
        [
            _request("A", "s1", **valid),
            _request("A", "s1", **invalid),
            _request("A", "s2", **second),
            _request("B", "s3", **side_b, cost_usd={"doubleValue": 0.5}),
            _request("B", "s3", **side_b, cost_usd={"stringValue": "x"}),
        ],
    )


@pytest.mark.parametrize("figure", FIGURES)
def test_absent_figure_adds_no_count_for_each_of_the_six(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str
):
    stream = _stream(
        tmp_path / "s",
        [
            _request("A", "s1"),
            _request("A", "s2", **{figure: ABSENT}),
            _request("B", "s3"),
        ],
    )

    text, document = _compared(installed, home, stream)

    side = document["compare"]["a"]
    assert side["run_count"] == 2
    assert side["totals"][figure] == pytest.approx(BASE_FIGURE[figure])
    assert side["mean_per_run"][figure] == pytest.approx(BASE_FIGURE[figure] / 2)
    assert "(invalid:" not in text
    assert "invalid_values" not in json.dumps(document["compare"])


@pytest.mark.parametrize("figure", FIGURES)
def test_figure_carried_only_in_resource_attributes_is_absent(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str
):
    stream = _stream(
        tmp_path / "s",
        [_request("A", "s1", **{figure: ABSENT}), _request("B", "s2")],
        resource={figure: {"stringValue": "abc"}},
    )

    text, document = _compared(installed, home, stream)

    side = document["compare"]["a"]
    assert side["totals"][figure] == 0
    assert "(invalid:" not in text
    assert "invalid_values" not in json.dumps(document["compare"])


def test_clean_side_prints_exactly_its_three_lines(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(tmp_path / "s", [_request("A", "s1"), _request("B", "s2")])

    text, document = _compared(installed, home, stream)

    assert _compare_block(text)[:4] == [
        "--compare arm A B:",
        "  A: runs=1  mean_cost_per_run=0.500000  total_cost_usd=0.500000",
        "    totals:  input=3 output=4 cache_read=5 cache_creation=6 cost_usd=0.500000 "
        "duration_ms=7",
        "    per run: input=3.00 output=4.00 cache_read=5.00 cache_creation=6.00 "
        "cost_usd=0.500000 duration_ms=7.00",
    ]
    assert list(document["compare"]["a"]) == SIDE_MEMBERS
    assert list(document["compare"]["b"]) == SIDE_MEMBERS


def test_check_refuses_compare_as_today(installed: InstalledProduct, home: Path, tmp_path: Path):
    stream = _probe(tmp_path / "s")

    result = run(installed, "check", "--dir", str(stream), "--compare", "arm", "A", "B", home=home)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "usage: ka-telemetry check [-h] [--dir DIR] [--json]\n"
        "ka-telemetry check: error: unrecognized arguments: --compare arm A B\n"
        "Run `ka-telemetry check --help` to see what it accepts.\n"
    )


def test_agents_prefix_invalid_values_with_compare_is_refused_as_today(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _probe(tmp_path / "s")

    result = run(
        installed, "agents", "--dir", str(stream), "--prefix", "invalid_values",
        "--compare", "arm", "A", "B", home=home,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert (
        "ka-telemetry agents: error: --prefix 'invalid_values' is also the name of a field the "
        "--json output adds to a row that left out an invalid value, so labels under it cannot "
        "be shown; use --where to select requests by those attributes instead\n"
        "Run `ka-telemetry agents --help` to see what it accepts.\n"
    ) in result.stderr


NO_METRIC = (
    "no metric export — possible causes, which ka-telemetry cannot tell apart: the session may "
    "have ended before Claude Code exported a cost metric for it; the session may still be "
    "running; the receiver (ka-telemetry receive) may not have been running when Claude Code "
    "sent the cost metric"
)
CAUSES = [
    "the session may have ended before Claude Code exported a cost metric for it",
    "the session may still be running",
    "the receiver (ka-telemetry receive) may not have been running when Claude Code sent the "
    "cost metric",
]
COST_NOTE = (
    "Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported "
    "it, or a total, mean or difference of those estimates. None is a bill."
)


def test_check_output_unchanged_for_a_stream_with_invalid_fields(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _probe(tmp_path / "s")

    text = run(installed, "check", "--dir", str(stream), home=home)
    as_json = run(installed, "check", "--dir", str(stream), "--json", home=home)

    assert text.returncode == as_json.returncode == 0
    assert text.stdout == (
        f"No metrics.jsonl was found in {stream}, so no session has a cost metric to check "
        "against.\n"
        "Totals check (claude_code.cost.usage vs summed cost_usd):\n"
        f"  session=s1  log_cost_usd=0.100000 (invalid: 1)  {NO_METRIC}\n"
        f"  session=s2  log_cost_usd=0.300000  {NO_METRIC}\n"
        f"  session=s3  log_cost_usd=0.500000 (invalid: 1)  {NO_METRIC}\n"
        f"{COST_NOTE}\n"
    )

    def entry(session: str, cost: float, counts: dict | None) -> dict:
        checked = {
            "session_id": session,
            "log_cost_usd": cost,
            "metric_cost_usd": None,
            "within_tolerance": None,
            "possible_causes": CAUSES,
        }
        return {**checked, "invalid_values": counts} if counts else checked

    assert json.loads(as_json.stdout) == {
        "unreadable_metric_lines": 0,
        "metrics_file": {"state": "nothing_received"},
        "skipped_lines": 0,
        "metrics_check": [
            entry("s1", 0.1, {"log_cost_usd": 1}),
            entry("s2", 0.3, None),
            entry("s3", 0.5, {"log_cost_usd": 1}),
        ],
        "cost_estimate_note": COST_NOTE,
    }


@pytest.mark.parametrize("figure", FIGURES)
def test_valid_zero_and_numeric_strings_are_not_counted(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str
):
    envelopes = [
        {"intValue": 0},
        {"stringValue": "43"},
        {"stringValue": "2.5"},
        {"stringValue": "1e3"},
    ]
    stream = _stream(
        tmp_path / "s",
        [_request("A", f"s{n}", **{figure: envelope}) for n, envelope in enumerate(envelopes)]
        + [_request("B", "s9")],
    )

    text, document = _compared(installed, home, stream)

    side = document["compare"]["a"]
    assert side["run_count"] == 4
    assert side["totals"][figure] == pytest.approx(1045.5)
    assert side["mean_per_run"][figure] == pytest.approx(1045.5 / 4)
    assert "(invalid:" not in text
    assert "invalid_values" not in json.dumps(document["compare"])


# ── A side names the values it left out ─────────────────────────────────────

TEXT_NAME = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_creation_tokens": "cache_creation",
    "cost_usd": "cost_usd",
    "duration_ms": "duration_ms",
}


def _figures_line(prefix: str, values: dict, counts: dict, *, per_run: bool) -> str:
    """A `totals:` or `per run:` line: counts print as integers (a mean to two places), cost to six,
    and a figure with a count is followed by `(invalid: n)`."""
    parts = []
    for figure in FIGURES:
        value = values[figure]
        if figure == "cost_usd":
            shown = f"{value:.6f}"
        else:
            shown = f"{value:.2f}" if per_run else str(value)
        mark = f" (invalid: {counts[figure]})" if figure in counts else ""
        parts.append(f"{TEXT_NAME[figure]}={shown}{mark}")
    return f"    {prefix} {' '.join(parts)}"


def _side_lines(label: str, runs: int, totals: dict, counts: dict) -> list[str]:
    """The three lines a side prints."""
    mean_cost = totals["cost_usd"] / runs
    cost_mark = f" (invalid: {counts['cost_usd']})" if "cost_usd" in counts else ""
    means = {figure: value / runs for figure, value in totals.items()}
    return [
        f"  {label}: runs={runs}  mean_cost_per_run={mean_cost:.6f}{cost_mark}  "
        f"total_cost_usd={totals['cost_usd']:.6f}{cost_mark}",
        _figures_line("totals: ", totals, counts, per_run=False),
        _figures_line("per run:", means, counts, per_run=True),
    ]


def _one_invalid_figure(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str, envelope: dict
):
    """Side `A` holds two runs, `s2` carrying `envelope` for `figure`; side `B` is clean."""
    stream = _stream(
        tmp_path / "s",
        [_request("A", "s1"), _request("A", "s2", **{figure: envelope}), _request("B", "s3")],
    )

    text, document = _compared(installed, home, stream)

    totals = {
        name: BASE_FIGURE[name] * (1 if name == figure else 2) for name in FIGURES
    }
    clean = dict(BASE_FIGURE)
    assert _compare_block(text)[1:7] == [
        *_side_lines("A", 2, totals, {figure: 1}),
        *_side_lines("B", 1, clean, {}),
    ]
    side_a, side_b = document["compare"]["a"], document["compare"]["b"]
    assert side_a["totals"][figure] == pytest.approx(totals[figure])
    assert side_a["mean_per_run"][figure] == pytest.approx(totals[figure] / 2)
    assert list(side_a)[-2:] == ["mean_per_run", "invalid_values"]
    assert side_a["invalid_values"] == {figure: 1}
    assert type(side_a["invalid_values"][figure]) is int
    assert list(side_b) == SIDE_MEMBERS
    assert "\n".join(_compare_block(text)).count("(invalid:") == (4 if figure == "cost_usd" else 2)
    assert document["compare"]["diff"] is not None


@pytest.mark.parametrize("figure", FIGURES)
def test_each_figure_with_a_non_parsing_string_is_left_out_and_counted(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str
):
    _one_invalid_figure(installed, home, tmp_path, figure, {"stringValue": "abc"})


@pytest.mark.parametrize("figure", FIGURES)
def test_each_figure_with_a_bool_is_left_out_and_counted(
    installed: InstalledProduct, home: Path, tmp_path: Path, figure: str
):
    _one_invalid_figure(installed, home, tmp_path, figure, {"boolValue": True})


PROBE_A = [
    "  A: runs=2  mean_cost_per_run=0.200000 (invalid: 1)  total_cost_usd=0.400000 (invalid: 1)",
    "    totals:  input=15 (invalid: 1) output=20 (invalid: 1) cache_read=37 (invalid: 1) "
    "cache_creation=48 (invalid: 1) cost_usd=0.400000 (invalid: 1) duration_ms=150 (invalid: 1)",
    "    per run: input=7.50 (invalid: 1) output=10.00 (invalid: 1) cache_read=18.50 "
    "(invalid: 1) cache_creation=24.00 (invalid: 1) cost_usd=0.200000 (invalid: 1) "
    "duration_ms=75.00 (invalid: 1)",
]
PROBE_B = [
    "  B: runs=1  mean_cost_per_run=0.500000 (invalid: 1)  total_cost_usd=0.500000 (invalid: 1)",
    "    totals:  input=2 output=4 cache_read=6 cache_creation=8 cost_usd=0.500000 (invalid: 1) "
    "duration_ms=120",
    "    per run: input=2.00 output=4.00 cache_read=6.00 cache_creation=8.00 "
    "cost_usd=0.500000 (invalid: 1) duration_ms=120.00",
]


def test_probe_text_matches_the_spec_lines_for_both_sides(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    text, _ = _compared(installed, home, _probe(tmp_path / "s"))

    assert _compare_block(text) == [
        "--compare arm A B:",
        *PROBE_A,
        *PROBE_B,
        "  B - A = 0.300000",
        "  B / A = 2.500000",
    ]


def test_probe_json_side_members_and_order(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    _, document = _compared(installed, home, _probe(tmp_path / "s"))

    compare = document["compare"]
    assert list(compare["a"]) == [*SIDE_MEMBERS, "invalid_values"]
    assert compare["a"]["invalid_values"] == dict.fromkeys(FIGURES, 1)
    assert list(compare["a"]["invalid_values"]) == list(FIGURES)
    assert all(type(count) is int for count in compare["a"]["invalid_values"].values())
    assert list(compare["b"]) == [*SIDE_MEMBERS, "invalid_values"]
    assert compare["b"]["invalid_values"] == {"cost_usd": 1}
    assert compare["a"]["totals"] == {
        "input_tokens": 15, "output_tokens": 20, "cache_read_tokens": 37,
        "cache_creation_tokens": 48, "cost_usd": pytest.approx(0.4), "duration_ms": 150,
    }
    assert list(compare["a"]["totals"]) == list(FIGURES)
    assert list(compare["a"]["mean_per_run"]) == list(FIGURES)
    assert compare["diff"] == pytest.approx(0.3)
    assert compare["ratio"] == pytest.approx(2.5)


A_ONLY_S1 = [
    "  A: runs=1  mean_cost_per_run=0.100000 (invalid: 1)  total_cost_usd=0.100000 (invalid: 1)",
    "    totals:  input=10 (invalid: 1) output=20 (invalid: 1) cache_read=30 (invalid: 1) "
    "cache_creation=40 (invalid: 1) cost_usd=0.100000 (invalid: 1) duration_ms=100 (invalid: 1)",
    "    per run: input=10.00 (invalid: 1) output=20.00 (invalid: 1) cache_read=30.00 "
    "(invalid: 1) cache_creation=40.00 (invalid: 1) cost_usd=0.100000 (invalid: 1) "
    "duration_ms=100.00 (invalid: 1)",
]
A_ONLY_S2 = [
    "  A: runs=1  mean_cost_per_run=0.300000  total_cost_usd=0.300000",
    "    totals:  input=5 output=0 cache_read=7 cache_creation=8 cost_usd=0.300000 "
    "duration_ms=50",
    "    per run: input=5.00 output=0.00 cache_read=7.00 cache_creation=8.00 "
    "cost_usd=0.300000 duration_ms=50.00",
]


@pytest.mark.parametrize(
    ("where", "expected_a"),
    [
        (("session.id=s1",), A_ONLY_S1),
        (("session.id=s1", "query_source=sdk"), A_ONLY_S1),
        (("session.id=s2",), A_ONLY_S2),
    ],
    ids=["s1", "s1-and-sdk", "s2"],
)
def test_where_narrows_the_counts_with_the_figures(
    installed: InstalledProduct, home: Path, tmp_path: Path, where: tuple, expected_a: list
):
    arguments = [part for clause in where for part in ("--where", clause)]

    text, document = _compared(installed, home, _probe(tmp_path / "s"), *arguments)

    assert _compare_block(text) == ["--compare arm A B:", *expected_a, "  B: no runs"]
    assert document["compare"]["b"] is None
    assert document["compare"]["diff"] is None
    assert ("invalid_values" in document["compare"]["a"]) is (expected_a is A_ONLY_S1)


def test_where_matching_nothing_leaves_two_empty_sides(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    text, document = _compared(
        installed, home, _probe(tmp_path / "s"), "--where", "session.id=nothing"
    )

    assert _compare_block(text) == ["--compare arm A B:", "  A: no runs", "  B: no runs"]
    assert document["compare"]["a"] is None and document["compare"]["b"] is None
    assert "invalid_values" not in json.dumps(document)


def test_sole_invalid_cost_gives_zero_mean_marked_everywhere_and_undefined_ratio(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [_request("A", "s1", cost_usd={"stringValue": "x"}), _request("B", "s2")],
    )

    text, document = _compared(installed, home, stream)

    zero_cost = {**BASE_FIGURE, "cost_usd": 0.0}
    assert _compare_block(text) == [
        "--compare arm A B:",
        *_side_lines("A", 1, zero_cost, {"cost_usd": 1}),
        *_side_lines("B", 1, dict(BASE_FIGURE), {}),
        "  B - A = 0.500000",
        "  B / A: ratio undefined (zero mean)",
    ]
    assert "\n".join(_compare_block(text)).count("(invalid:") == 4
    assert document["compare"]["a"]["invalid_values"] == {"cost_usd": 1}
    assert "invalid_values" not in document["compare"]["b"]
    assert document["compare"]["diff"] == pytest.approx(0.5)
    assert document["compare"]["ratio"] is None


def test_all_invalid_and_all_absent_figures_and_two_empty_sides(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _stream(
        tmp_path / "s",
        [
            _request("A", "s1", input_tokens={"stringValue": "abc"}, output_tokens=ABSENT),
            _request("A", "s2", input_tokens={"boolValue": False}, output_tokens=ABSENT),
            _request("B", "s3"),
        ],
    )

    text, document = _compared(installed, home, stream)

    side = document["compare"]["a"]
    assert side["run_count"] == 2
    assert side["totals"]["input_tokens"] == 0 and side["mean_per_run"]["input_tokens"] == 0
    assert side["totals"]["output_tokens"] == 0 and side["mean_per_run"]["output_tokens"] == 0
    assert side["invalid_values"] == {"input_tokens": 2}
    assert "input=0 (invalid: 2) output=0 " in text
    assert "input=0.00 (invalid: 2) output=0.00 " in text
    assert "\n".join(_compare_block(text)).count("(invalid:") == 2

    empty_text, empty_document = _compared(
        installed, home, stream, "--where", "session.id=nothing"
    )
    assert _compare_block(empty_text) == ["--compare arm A B:", "  A: no runs", "  B: no runs"]
    assert empty_document["compare"]["a"] is None and empty_document["compare"]["b"] is None
