"""End-to-end tests of `ka-telemetry turn-on` as the installed command.

Driven the way a person drives it: the command from the scratch virtualenv the `installed`
fixture builds, relative paths in a directory outside the checkout, and a `HOME` of its own so
the default endpoint file is never the machine's.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from e2e.support import PRODUCT_ROOT, run

if TYPE_CHECKING:
    from conftest import InstalledProduct

SEVEN_JOBS = "{runs,steps,context,receive,agents,check,turn-on}"
TURN_ON_JOB_LINE = (
    "write the keys that make Claude Code send its telemetry to the receiver on this machine "
    "into a Claude Code settings file, remove them, or print them for one run"
)
NETWORK_PROMISE = (
    "This tool sends no telemetry and makes no network call except the ones the job you asked "
    "for requires."
)
EIGHT_FLAGS = (
    "--settings-file",
    "--endpoint-file",
    "--replace",
    "--remove",
    "--force",
    "--print",
    "--prefix",
    "--label",
)
ENDPOINT = "http://localhost:4318"
FIVE_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
OTHER_JOBS = ("runs", "steps", "context", "receive", "agents", "check")


def _words(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def test_installing_into_an_empty_venv_adds_only_this_distribution(installed):
    assert installed.installed - installed.baseline == {"ka-telemetry"}
    project = tomllib.loads((PRODUCT_ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == []


def test_the_installed_command_names_the_seven_jobs_and_exits_0_on_help(installed, home):
    result = run(installed, "--help", home=home)

    assert result.returncode == 0, result.stderr
    text = _words(result.stdout)
    assert SEVEN_JOBS in text
    assert _words(TURN_ON_JOB_LINE) in text
    assert text.endswith(NETWORK_PROMISE)


def test_turn_on_help_exits_0_and_lists_the_eight_flags(installed, home):
    result = run(installed, "turn-on", "--help", home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: ka-telemetry turn-on")
    for flag in EIGHT_FLAGS:
        assert flag in result.stdout, flag
    # Exactly the eight (CP-282): every option line names one of them, and none is added.
    assert set(re.findall(r"^  (--[a-z-]+)", result.stdout, re.MULTILINE)) == set(EIGHT_FLAGS)


def test_installed_write_remove_and_print_work_from_outside_the_checkout(
    installed, home, workdir
):
    (workdir / "ep").write_text(ENDPOINT + "\n")
    write = run(
        installed,
        "turn-on",
        "--settings-file",
        "s.json",
        "--endpoint-file",
        "ep",
        home=home,
        cwd=workdir,
    )
    assert (write.returncode, write.stderr) == (0, "")
    assert all(key in write.stdout for key in FIVE_KEYS)
    assert ENDPOINT not in write.stdout
    written = json.loads((workdir / "s.json").read_text())
    assert written["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == ENDPOINT
    assert "_ka_telemetry_turn_on" in written

    printed = run(
        installed, "turn-on", "--print", "--endpoint-file", "ep", home=home, cwd=workdir
    )
    assert printed.returncode == 0, printed.stderr
    assert json.loads(printed.stdout)["env"] == written["env"]
    assert printed.stderr.endswith(f"Telemetry:    OTLP → {ENDPOINT}\n")

    removed = run(
        installed,
        "turn-on",
        "--settings-file",
        "s.json",
        "--endpoint-file",
        "ep",
        "--remove",
        home=home,
        cwd=workdir,
    )
    assert (removed.returncode, removed.stdout, removed.stderr) == (0, "", "")
    assert json.loads((workdir / "s.json").read_text()) == {}


def test_installed_remove_takes_only_what_the_record_shows_and_force_takes_the_rest(
    installed, home, workdir
):
    (workdir / "ep").write_text(ENDPOINT + "\n")
    preloaded = {"env": {"KEEP": "me", "OTEL_METRICS_EXPORTER": "otlp"}}
    (workdir / "s.json").write_text(json.dumps(preloaded))
    arguments = ("turn-on", "--settings-file", "s.json", "--endpoint-file", "ep")

    run(installed, *arguments, home=home, cwd=workdir)
    removed = run(installed, *arguments, "--remove", home=home, cwd=workdir)
    assert (removed.returncode, removed.stderr) == (0, "")
    assert "OTEL_METRICS_EXPORTER" in removed.stdout and "otlp" not in removed.stdout
    assert json.loads((workdir / "s.json").read_text()) == preloaded

    user_values = {"KEEP": "me", **dict.fromkeys(FIVE_KEYS, "mine")}
    (workdir / "s.json").write_text(json.dumps({"env": user_values}))
    forced = run(installed, *arguments, "--remove", "--force", home=home, cwd=workdir)
    assert (forced.returncode, forced.stdout, forced.stderr) == (0, "", "")
    assert json.loads((workdir / "s.json").read_text()) == {"env": {"KEEP": "me"}}


def test_cli_passes_flags_unparsed_to_the_command(installed, home, workdir):
    (workdir / "ep").write_text(ENDPOINT + "\n")
    accepted = run(
        installed,
        "turn-on",
        "--print",
        "--endpoint-file",
        "ep",
        "--prefix",
        "acme",
        "--label",
        "k=v",
        home=home,
        cwd=workdir,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "acme.k=v" in json.loads(accepted.stdout)["env"]["OTEL_RESOURCE_ATTRIBUTES"]

    refused = run(installed, "turn-on", "--bogus", home=home, cwd=workdir)
    assert refused.returncode == 2
    assert "ka-telemetry turn-on: error:" in refused.stderr
    assert refused.stdout == ""


@pytest.mark.parametrize("job", OTHER_JOBS)
def test_the_six_other_commands_keep_their_help_and_exit_codes(installed, home, job):
    helped = run(installed, job, "--help", home=home)
    assert helped.returncode == 0, (job, helped.stderr)
    assert helped.stdout.startswith(f"usage: ka-telemetry {job}")

    refused = run(installed, job, "--no-such-flag", home=home)
    assert refused.returncode == 2, job
    assert refused.stdout == ""


PROMPT_TEXT_KEYS = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_ASSISTANT_RESPONSES",
)


def test_no_help_offers_a_prompt_text_key(installed, home):
    for arguments in (("--help",), ("turn-on", "--help")):
        result = run(installed, *arguments, home=home)
        assert result.returncode == 0, result.stderr
        for key in PROMPT_TEXT_KEYS:
            assert key not in result.stdout, (arguments, key)


def test_no_installed_mode_writes_or_prints_a_prompt_text_key(installed, home, workdir):
    (workdir / "ep").write_text(ENDPOINT + "\n")
    (workdir / "s.json").write_text("{}")
    invocations = (
        ("turn-on", "--settings-file", "s.json", "--endpoint-file", "ep"),
        ("turn-on", "--print", "--endpoint-file", "ep", "--prefix", "acme"),
        ("turn-on", "--settings-file", "s.json", "--endpoint-file", "ep", "--remove"),
    )
    for arguments in invocations:
        result = run(installed, *arguments, home=home, cwd=workdir)
        assert result.returncode == 0, (arguments, result.stderr)
        seen = result.stdout + result.stderr + (workdir / "s.json").read_text()
        for key in PROMPT_TEXT_KEYS:
            assert key not in seen, (arguments, key)
