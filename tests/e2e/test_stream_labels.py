"""End-to-end tests of the installed `ka-telemetry agents` command's label handling.

The command is driven from the scratch virtualenv the `installed` fixture builds, under a `HOME`
of its own. The label cases feed the read side the form Claude Code delivers, as measured: each character the turn-on encodes arrives as a NUL and four hex digits.
Fixture G is the spec's, written with `\u0000` JSON escapes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import PRODUCT_ROOT, child_env, run

if TYPE_CHECKING:
    from conftest import InstalledProduct

AGENTS_USAGE = (
    "usage: ka-telemetry agents [-h] [--dir DIR] [--prefix PREFIX] [--where KEY=VALUE] "
    "[--compare ATTR A B] [--json]"
)


def _line(session: str, note: str, *, on: str = "resource") -> str:
    """One `logs.jsonl` line: an `api_request` in `session`, `team.note=note` on the resource
    (default) or on the record."""
    attrs = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "api_request"}},
        {"key": "cost_usd", "value": {"doubleValue": 0.25}},
    ]
    note_attr = {"key": "team.note", "value": {"stringValue": note}}
    resource = [note_attr] if on == "resource" else []
    if on == "record":
        attrs.append(note_attr)
    body = {
        "resourceLogs": [
            {
                "resource": {"attributes": resource},
                "scopeLogs": [{"logRecords": [{"attributes": attrs}]}],
            }
        ]
    }
    return json.dumps({"signal": "logs", "body": body})


def _completed_line(session: str, agent_type: str) -> str:
    """One `logs.jsonl` line: a `subagent_completed` record in `session`."""
    attrs = [
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "event.name", "value": {"stringValue": "subagent_completed"}},
        {"key": "agent_type", "value": {"stringValue": agent_type}},
    ]
    body = {
        "resourceLogs": [
            {"resource": {"attributes": []}, "scopeLogs": [{"logRecords": [{"attributes": attrs}]}]}
        ]
    }
    return json.dumps({"signal": "logs", "body": body})


def _fixture_g(directory: Path) -> Path:
    """Fixture G of the spec: three requests, each carrying `team.note` in the form Claude Code
    delivers it — a NUL and four digits for each character the turn-on encodes — and two
    completed agents."""
    directory.mkdir()
    lines = [
        _line("s-encoded", "two\u00000020words"),
        _line("s-plain", "plain"),
        _line("s-punct", 'a\u0000002Cb\u0000003Bc\u00000022d\u0000003De'),
        _completed_line("s-encoded", "general-purpose"),
        _completed_line("s-plain", "Explore"),
    ]
    (directory / "logs.jsonl").write_text("".join(f"{line}\n" for line in lines))
    return directory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _words(text: str) -> str:
    return " ".join(text.split())


def test_agents_help_lists_the_same_options(installed: InstalledProduct, home: Path):
    result = run(installed, "agents", "--help", home=home)

    assert result.returncode == 0, result.stderr
    usage_paragraph = result.stdout.split("\n\n", 1)[0]
    assert _words(usage_paragraph) == AGENTS_USAGE


def _agents(installed: InstalledProduct, home: Path, stream: Path, *arguments: str):
    return run(installed, "agents", "--dir", str(stream), *arguments, home=home)


def _row_sessions(result) -> list[str]:
    assert result.returncode == 0, result.stderr
    return sorted(row["session_id"] for row in json.loads(result.stdout)["rows"])


def test_agents_text_shows_the_typed_label(installed: InstalledProduct, home: Path, tmp_path: Path):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(installed, home, stream, "--prefix", "team")

    assert result.returncode == 0, result.stderr
    assert "\x00" not in result.stdout
    lines = [line for line in result.stdout.splitlines() if line.startswith("    team:")]
    assert sorted(lines) == [
        '    team: team.note=a,b;c"d=e',
        "    team: team.note=plain",
        "    team: team.note=two words",
    ]


def test_agents_json_shows_the_typed_label(installed: InstalledProduct, home: Path, tmp_path: Path):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(installed, home, stream, "--prefix", "team", "--json")

    assert result.returncode == 0, result.stderr
    members = {row["session_id"]: row["team"] for row in json.loads(result.stdout)["rows"]}
    assert members == {
        "s-encoded": {"team.note": "two words"},
        "s-plain": {"team.note": "plain"},
        "s-punct": {"team.note": 'a,b;c"d=e'},
    }


def test_two_values_with_one_shown_value_print_one_row(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = tmp_path / "stream"
    stream.mkdir()
    lines = [
        _line("s-1", "two\u0000" + "0020words"),
        _line("s-1", "two words", on="record"),
    ]
    (stream / "logs.jsonl").write_text("".join(f"{line}\n" for line in lines))

    result = _agents(installed, home, stream, "--prefix", "team", "--json")

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["rows"]
    assert len(rows) == 1
    assert rows[0]["request_count"] == 2
    assert rows[0]["cost_usd"] == pytest.approx(0.5)
    assert rows[0]["team"] == {"team.note": "two words"}


@pytest.mark.parametrize(
    ("operand", "session"),
    [("two words", "s-encoded"), ('a,b;c"d=e', "s-punct"), ("plain", "s-plain")],
)
def test_where_selects_a_label_by_its_typed_value(
    installed: InstalledProduct, home: Path, tmp_path: Path, operand: str, session: str
):
    stream = _fixture_g(tmp_path / "stream")
    where = ("--prefix", "team", "--where", f"team.note={operand}")

    as_json = _agents(installed, home, stream, *where, "--json")
    as_text = _agents(installed, home, stream, *where)

    assert _row_sessions(as_json) == [session]
    assert as_text.returncode == 0, as_text.stderr
    assert as_text.stdout.startswith("1 agent row(s)")
    assert f"session={session}" in as_text.stdout


def test_where_does_not_match_the_received_form(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(
        installed, home, stream, "--prefix", "team", "--where", "team.note=two%000020words"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("0 agent row(s)")


def test_where_outside_the_prefix_compares_as_received(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _fixture_g(tmp_path / "stream")

    no_prefix = _agents(installed, home, stream, "--where", "team.note=two words")
    other_prefix = _agents(
        installed, home, stream, "--prefix", "other", "--where", "team.note=plain", "--json"
    )

    assert no_prefix.stdout.startswith("0 agent row(s)")
    assert _row_sessions(other_prefix) == ["s-plain"]


@pytest.mark.parametrize("operand", ["two words", 'a,b;c"d=e'])
def test_compare_counts_a_label_by_its_typed_value(
    installed: InstalledProduct, home: Path, tmp_path: Path, operand: str
):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(
        installed, home, stream, "--prefix", "team", "--compare", "team.note", operand, "plain"
    )

    assert result.returncode == 0, result.stderr
    assert f"  {operand}: runs=1" in result.stdout
    assert "total_cost_usd=0.250000" in result.stdout
    assert f"{operand}: no runs" not in result.stdout


def test_compare_without_a_prefix_matches_the_received_value(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(installed, home, stream, "--compare", "team.note", "two words", "plain")

    assert result.returncode == 0, result.stderr
    assert "  two words: no runs" in result.stdout
    assert "  plain: runs=1" in result.stdout


# ── `--where` narrows the completed-agents section (CP-320) ──────────────────


def _completed_section(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("subagent_completed counts"))
    return lines[start + 1 : lines.index("", start)]


@pytest.mark.parametrize(
    "where", ["session.id=s-plain", "team.note=plain"], ids=["non-label", "label"]
)
def test_where_narrows_completed_agents_to_the_shown_sessions(
    installed: InstalledProduct, home: Path, tmp_path: Path, where: str
):
    stream = _fixture_g(tmp_path / "stream")
    arguments = ("--prefix", "team", "--where", where)

    as_text = _agents(installed, home, stream, *arguments)
    as_json = _agents(installed, home, stream, *arguments, "--json")

    assert as_text.returncode == 0, as_text.stderr
    assert _completed_section(as_text.stdout) == [
        "  session=s-plain agent_type=Explore count=1 duration_ms=0 total_tool_uses=0"
    ]
    assert [r["session_id"] for r in json.loads(as_json.stdout)["subagent_rows"]] == ["s-plain"]


def test_no_filter_lists_every_completed_session(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _fixture_g(tmp_path / "stream")

    result = _agents(installed, home, stream)

    assert result.returncode == 0, result.stderr
    sessions = [line.split()[0] for line in _completed_section(result.stdout)]
    assert sessions == ["session=s-encoded", "session=s-plain"]


def test_no_match_filter_says_nothing_was_selected_rather_than_nothing_read(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = _fixture_g(tmp_path / "stream")

    as_text = _agents(installed, home, stream, "--where", "session.id=nothing")
    as_json = _agents(installed, home, stream, "--where", "session.id=nothing", "--json")

    assert as_text.returncode == 0, as_text.stderr
    assert as_text.stdout.startswith("0 agent row(s)")
    assert "No subagent_completed records read." not in as_text.stdout
    section = _completed_section(as_text.stdout)
    assert len(section) == 1 and section[0].strip()
    assert json.loads(as_json.stdout)["subagent_rows"] == []


# ── `python3 -m ka_telemetry` (CP-321) ───────────────────────────────────────


def _module(*arguments: str, home: Path) -> subprocess.CompletedProcess[str]:
    """`python3 -m ka_telemetry ARGUMENTS` from the product's root, under the interpreter running
    the suite — which does not have ka-telemetry installed — with `HOME` of its own and no
    `PYTHONPATH`, so the checkout's own directory is the only way the package is found."""
    environment = child_env(home)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "ka_telemetry", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=PRODUCT_ROOT,
    )


def _same_stdout_and_status(
    installed: InstalledProduct, home: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run `arguments` both ways and assert the two agree on stdout and status; return the
    module's result."""
    through_module = _module(*arguments, home=home)
    through_command = run(installed, *arguments, home=home)

    assert through_module.stdout == through_command.stdout
    assert through_module.returncode == through_command.returncode
    return through_module


def test_module_help_matches_the_installed_command(installed: InstalledProduct, home: Path):
    result = _same_stdout_and_status(installed, home, "--help")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == (
        "usage: ka-telemetry [-h] {runs,steps,context,receive,agents,check,turn-on} ..."
    )


def test_module_agents_help_matches_the_installed_command(installed: InstalledProduct, home: Path):
    result = _same_stdout_and_status(installed, home, "agents", "--help")

    assert result.returncode == 0, result.stderr
    assert _words(result.stdout.split("\n\n", 1)[0]) == AGENTS_USAGE


def test_module_agents_report_matches_the_installed_command(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    stream = str(_fixture_g(tmp_path / "stream"))
    variants = [
        ["--prefix", "team"],
        ["--prefix", "team", "--json"],
        ["--prefix", "team", "--where", "team.note=plain"],
        ["--prefix", "team", "--compare", "team.note", "plain", "other"],
    ]

    for variant in variants:
        result = _same_stdout_and_status(installed, home, "agents", "--dir", stream, *variant)
        assert result.returncode == 0, result.stderr
        assert result.stdout


def test_module_usage_errors_match_status(installed: InstalledProduct, home: Path):
    misuses = (
        [],
        ["no-such-command"],
        ["agents", "--no-such-option"],
        ["agents", "--compare", "x"],
    )
    for arguments in misuses:
        through_module = _module(*arguments, home=home)
        through_command = run(installed, *arguments, home=home)

        assert through_module.returncode == through_command.returncode, arguments
        assert through_module.returncode != 0, arguments
        assert through_module.stdout == through_command.stdout, arguments


def test_module_delegates_arbitrary_arguments(
    installed: InstalledProduct, home: Path, tmp_path: Path
):
    transcript = tmp_path / "session.jsonl"
    usage = {"input_tokens": 1, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 300}
    record = {"type": "assistant", "message": {"id": "m", "usage": usage}}
    transcript.write_text(json.dumps(record) + "\n")

    result = _same_stdout_and_status(installed, home, "context", str(transcript))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Context size: 321 tokens\n"
