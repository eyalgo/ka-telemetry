"""End-to-end proof that `steps` says how long each step took, on the real command.

Every command is `python -m ka_telemetry` run as a child process of the interpreter running this
suite, under an empty `HOME` and an empty directory as `PATH`, so no result depends on the running
user's own telemetry stream. Two fixtures carry the proof:

- `R`: a copy of `tests/parity/projects/`, whose ten step rows have the spans below;
- `B`: a tree built here, holding a step whose agent transcript holds no readable timestamp and a
  step grouping two agents whose time ranges differ.

Every expected figure is written out here as a number the transcripts give, measured by hand from
each row's earliest and latest `timestamp`; none is derived with the production helper. The base
product, `8788845`, is extracted from git for the one comparison that needs what it printed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ka_telemetry.transcript_notes import SPAN_LINE

PATTERN = re.compile(r"first .*last .*response", re.IGNORECASE)
PRODUCT_ROOT = Path(__file__).resolve().parent.parent.parent
PARITY = PRODUCT_ROOT / "tests" / "parity"
AGENTS_JSON = PARITY / "agents.json"
BASE_COMMIT = "8788845"
WORKFLOW = "wf_ec5e24de-808"
KENSAI = "-home-user-work-demo"
RUN_SESSION = "a839285c-263f-40a6-8801-71f49d00cc68"

# The ten rows of `R`, in the order `steps` prints them: header, then the span, then the model line.
REFERENCE_ROWS = [
    ("development-plan (produce)", "113.727", 113.727),
    ("pm [fallback]", "93.63", 93.63),
    ("development-code-integrity (produce)", "366.182", 366.182),
    ("development-specs (audit)", "215.143", 215.143),
    ("development-design (produce)", "118.955", 118.955),
    ("development-implementation (audit)", "177.325", 177.325),
    ("development-specs (produce)", "144.245", 144.245),
    ("development-plan (audit)", "286.435", 286.435),
    ("development-implementation (produce)", "86.566", 86.566),
    ("development-design (audit)", "270.837", 270.837),
]
# What the first row read before a step was timed over the model's responses only: it spanned every
# record its agent's transcript held, the prompts and tool results included.
ALL_RECORDS_FIRST_ROW = "115.716"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("step-spans")
    projects = root / "R"
    shutil.copytree(PARITY / "projects", projects)
    for name in ("home", "empty-path"):
        (root / name).mkdir()
    return {
        "R": projects,
        "B": _built_tree(root / "B"),
        "home": root / "home",
        "path": root / "empty-path",
        "root": root,
    }


def _command(world, *argv: str, pythonpath: Path = PRODUCT_ROOT):
    return subprocess.run(
        [sys.executable, "-B", "-m", "ka_telemetry", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=world["home"],
        env={
            "HOME": str(world["home"]),
            "PATH": str(world["path"]),
            "PYTHONPATH": str(pythonpath),
        },
    )


def _steps(world, projects: Path, *extra: str):
    return _command(world, "steps", "--projects-root", str(projects), *extra)


def _usage(timestamp: str | None, *, response_id: str, tokens: int) -> str:
    record = {
        "type": "assistant",
        "message": {
            "id": response_id,
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": tokens, "output_tokens": 1},
        },
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return json.dumps(record)


def _prompt(timestamp: str) -> str:
    return json.dumps({"type": "user", "timestamp": timestamp})


def _built_tree(projects: Path) -> Path:
    """`B`: one workflow run of five agents. `n1` is a step whose transcript holds no timestamp at
    all, `p1` one whose transcript holds only prompts with timestamps, and `z1` one whose single
    response sits between two prompts. `a1` and `a2` are one step: `a1` prompts at 10:00:00 and
    responds at 10:01:00, `a2` prompts at 10:05:00 and responds at 10:07:00."""
    run_dir = projects / "slug" / "sid" / "subagents" / "workflows" / "wf_built"
    run_dir.mkdir(parents=True)
    journal = []
    steps = (
        ("n1", "untimed-step"),
        ("p1", "prompts-only-step"),
        ("z1", "one-response-step"),
        ("a1", "S"),
        ("a2", "S"),
    )
    for agent_id, step in steps:
        journal.append(json.dumps({"type": "started", "agentId": agent_id}))
        journal.append(
            json.dumps(
                {
                    "type": "result",
                    "agentId": agent_id,
                    "result": {"step": step, "status": "ok", "artifact": "x.md"},
                }
            )
        )
    (run_dir / "journal.jsonl").write_text("\n".join(journal) + "\n")
    transcripts = {
        "n1": [_usage(None, response_id="r1", tokens=3), json.dumps({"type": "user"})],
        "p1": [_prompt("2026-09-12T11:00:00Z"), _prompt("2026-09-12T11:05:00Z")],
        "z1": [
            _prompt("2026-09-12T13:00:00Z"),
            _usage("2026-09-12T13:00:05Z", response_id="r4", tokens=6),
            _prompt("2026-09-12T13:09:00Z"),
        ],
        "a1": [
            _prompt("2026-09-12T10:00:00Z"),
            _usage("2026-09-12T10:01:00Z", response_id="r2", tokens=4),
        ],
        "a2": [
            _prompt("2026-09-12T10:05:00Z"),
            _usage("2026-09-12T10:07:00Z", response_id="r3", tokens=5),
        ],
    }
    for agent_id, lines in transcripts.items():
        (run_dir / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
    return projects


def _without_statement(text: str) -> str:
    """A report with its one statement of what the time covers taken out: the text form's whole
    line, or the JSON form's top-level `span` member line. Nothing else is changed."""
    lines = (f"{SPAN_LINE}\n", f'  "span": {json.dumps(SPAN_LINE)},\n')
    return "".join(line for line in text.splitlines(keepends=True) if line not in lines)


def _rows(text: str) -> dict[str, list[str]]:
    """Each row's lines, by its label: the row header and every indented line below it."""
    rows: dict[str, list[str]] = {}
    label = None
    for line in text.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and "Coverage:" not in line:
            label = line.strip().split("  types=")[0]
            rows[label] = [line]
        elif line.startswith("    ") and label is not None:
            rows[label].append(line)
        else:
            label = None
    return rows


def test_reference_steps_text_gives_each_of_the_ten_rows_its_span_directly_under_its_line(world):
    done = _steps(world, world["R"])

    assert (done.returncode, done.stderr) == (0, "")
    rows = _rows(done.stdout)
    assert list(rows) == [label for label, _, _ in REFERENCE_ROWS]
    for label, text, _ in REFERENCE_ROWS:
        header, span, *models = rows[label]
        assert span == f"    span={text}s", label
        assert models and all(line.startswith("    claude-") for line in models), label
        assert [line for line in rows[label] if line.startswith("    span=")] == [span], label
    assert done.stdout.count("span=") == 10
    first = rows["development-plan (produce)"]
    assert first[0] == "  development-plan (produce)  types=tech-lead  agents=a53ef7efd8c468bad"
    assert first[1:3] == [
        "    span=113.727s",
        "    claude-opus-5: input=40 output=415 thinking=0 cache_read=1459608 "
        "cache_write_1h=0 cache_write_5m=103692",
    ]


def test_reference_steps_json_carries_span_seconds_on_every_row_at_full_precision(world):
    done = _steps(world, world["R"], "--json")

    assert (done.returncode, done.stderr) == (0, "")
    (run,) = json.loads(done.stdout)["runs"]
    assert [row["span_seconds"] for row in run["rows"]] == [value for _, _, value in REFERENCE_ROWS]
    assert all(isinstance(row["span_seconds"], float) for row in run["rows"])
    produce = next(
        row
        for row in run["rows"]
        if row["label"] == "development-plan" and row["role"] == "produce"
    )
    assert list(produce) == [
        "span_seconds",
        "label",
        "role",
        "is_fallback",
        "agent_ids",
        "agent_types",
        "spend",
    ]
    assert produce["span_seconds"] == 113.727
    assert produce["agent_ids"] == ["a53ef7efd8c468bad"]
    assert produce["agent_types"] == ["tech-lead"]
    assert produce["spend"]["claude-opus-5"]["input"] == 40
    assert '"span_seconds": 113.727,\n' in done.stdout


def test_wf_filter_shows_the_same_spans_in_both_forms(world):
    for extra in ((), ("--json",)):
        everything = _steps(world, world["R"], *extra)
        selected = _steps(world, world["R"], "--wf", WORKFLOW, *extra)

        assert (selected.returncode, selected.stderr) == (0, "")
        assert selected.stdout == everything.stdout, extra
    assert _steps(world, world["R"], "--wf", WORKFLOW).stdout.count("span=") == 10


def test_response_only_span_would_differ(world):
    text = _steps(world, world["R"]).stdout

    assert f"span={ALL_RECORDS_FIRST_ROW}s" not in text
    assert "    span=113.727s" in text


def test_step_with_no_readable_timestamp_prints_na_and_null(world):
    text = _steps(world, world["B"])
    as_json = _steps(world, world["B"], "--json")

    assert (text.returncode, text.stderr, as_json.returncode, as_json.stderr) == (0, "", 0, "")
    rows = _rows(text.stdout)
    assert rows["untimed-step (produce)"][1:] == [
        "    span=n/a",
        "    claude-sonnet-5: input=3 output=1 cache_read=0 cache_write_1h=0 cache_write_5m=0",
    ]
    assert (
        "  Coverage: 5 with a step / 0 without / 0 started with no result / "
        "0 agent files with no journal entry at all"
    ) in text.stdout.splitlines()
    (run,) = json.loads(as_json.stdout)["runs"]
    untimed = next(row for row in run["rows"] if row["label"] == "untimed-step")
    assert untimed["span_seconds"] is None
    assert untimed["spend"]["claude-sonnet-5"]["input"] == 3
    assert '"span_seconds": null,\n' in as_json.stdout
    assert run["coverage"] == {
        "with_step": 5,
        "without_step": 0,
        "started_no_result": 0,
        "files_with_no_journal_entry": 0,
    }


def test_step_whose_only_timestamps_are_prompts_prints_na_and_null_never_zero(world):
    text = _steps(world, world["B"])
    as_json = _steps(world, world["B"], "--json")

    rows = _rows(text.stdout)
    assert rows["prompts-only-step (produce)"] == [
        "  prompts-only-step (produce)  types=(none)  agents=p1",
        "    span=n/a",
    ]
    (run,) = json.loads(as_json.stdout)["runs"]
    row = next(row for row in run["rows"] if row["label"] == "prompts-only-step")
    assert row["span_seconds"] is None and row["spend"] == {}


def test_step_with_one_timed_response_among_prompts_prints_zero(world):
    rows = _rows(_steps(world, world["B"]).stdout)
    (run,) = json.loads(_steps(world, world["B"], "--json").stdout)["runs"]

    assert rows["one-response-step (produce)"][1] == "    span=0s"  # not the prompts' 540s
    row = next(row for row in run["rows"] if row["label"] == "one-response-step")
    assert row["span_seconds"] == 0.0 and isinstance(row["span_seconds"], float)


def test_two_agent_row_spans_earliest_to_latest_not_the_sum(world):
    text = _steps(world, world["B"]).stdout
    (run,) = json.loads(_steps(world, world["B"], "--json").stdout)["runs"]

    both = _rows(text)["S (produce)"]
    assert both[0].endswith("agents=a1,a2")
    # 10:01:00 to 10:07:00 over the two responses: not 420 (which counts the prompts), not 0 (the
    # sum of two one-response spans), and not 60 + 120.
    assert both[1] == "    span=360s"
    row = next(row for row in run["rows"] if row["label"] == "S")
    assert row["agent_ids"] == ["a1", "a2"] and row["span_seconds"] == 360.0
    assert both[2].startswith("    claude-sonnet-5: input=9 ")


def test_runs_reference_outputs_match_first_last_response_pattern(world):
    argv = ("runs", "--agents-json", str(AGENTS_JSON), "--projects-root", str(world["R"]))
    for extra in ((), ("--json",)):
        done = _command(world, *argv, *extra)

        assert (done.returncode, done.stderr) == (0, "")
        matching = [line for line in done.stdout.splitlines() if PATTERN.search(line)]
        assert len(matching) == 1 and SPAN_LINE in matching[0], extra


@pytest.fixture(scope="module")
def base_product(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The product as `8788845` held it, extracted from git."""
    exists = subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        pytest.skip(f"the base commit {BASE_COMMIT} is not in this repository")
    target = tmp_path_factory.mktemp("base-product")
    archive = subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), "archive", BASE_COMMIT, "ka_telemetry"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return target


def test_reference_runs_runs_json_and_context_are_byte_identical_to_product_8788845(
    world, base_product
):
    transcript = world["R"] / KENSAI / f"{RUN_SESSION}.jsonl"
    commands = [
        ("runs", "--agents-json", str(AGENTS_JSON), "--projects-root", str(world["R"])),
        ("runs", "--agents-json", str(AGENTS_JSON), "--projects-root", str(world["R"]), "--json"),
        ("context", str(transcript)),
        ("context", "--json", str(transcript)),
    ]
    for argv in commands:
        now = _command(world, *argv)
        then = _command(world, *argv, pythonpath=base_product)

        # `context` is byte-identical; `runs` differs only by the added statement.
        shown = _without_statement(now.stdout) if argv[0] == "runs" else now.stdout
        assert (now.returncode, shown, now.stderr) == (
            then.returncode,
            then.stdout,
            then.stderr,
        ), argv
        assert now.returncode == 0 and now.stdout, argv
        assert (now.stdout != shown) == (argv[0] == "runs"), argv


def test_the_base_product_prints_no_span_on_the_reference_set(world, base_product):
    text = _command(world, "steps", "--projects-root", str(world["R"]), pythonpath=base_product)
    as_json = _command(
        world, "steps", "--projects-root", str(world["R"]), "--json", pythonpath=base_product
    )

    assert "span=" not in text.stdout and "span_seconds" not in as_json.stdout


def _without_spans(text: str) -> str:
    """`steps` output with the step spans taken out: the text form's `    span=` lines, or the JSON
    form's `span_seconds` member lines (the first member of a row, so each ends in a comma)."""
    without_lines = re.sub(r"^    span=[^\n]*\n", "", text, flags=re.MULTILINE)
    return re.sub(r'^ *"span_seconds": [^\n]*,\n', "", without_lines, flags=re.MULTILINE)


def test_steps_on_the_reference_set_differs_from_the_base_only_by_spans_and_the_statement(
    world, base_product
):
    for extra in ((), ("--json",)):
        argv = ("steps", "--projects-root", str(world["R"]), *extra)
        now = _command(world, *argv)
        then = _command(world, *argv, pythonpath=base_product)

        assert now.returncode == then.returncode == 0 and now.stderr == then.stderr == ""
        assert _without_spans(_without_statement(now.stdout)) == then.stdout, extra
        assert now.stdout.count(SPAN_LINE) == 1, extra
        assert _without_statement(now.stdout) != now.stdout, extra
