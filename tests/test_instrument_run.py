"""Tests for the settings a run is given: `ka-telemetry turn-on`, its write and print modes.

These tests began as the tests of a sourced bash library, `instrument_run.sh`. They now run the
package command, so what is pinned here is the command's own contract: the JSON `claude --settings`
takes on stdout, the notices on stderr, and the five-key entry a write puts in a settings file.
The collected names are the ones the library's tests had.

The library stamped a set of tags that belong to the host it came from. The package stamps none of
them: the labels are the ones the person gives, under a prefix the person chooses (these tests use
`kensai` as one such prefix), and the one thing the command adds itself is a run id and, inside a
git repository, the instance. A test whose subject was one of those tags now asserts that the tag
is not attached — and that the same words given as a label are.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
_TURN_ON_SOURCE = PRODUCT_ROOT / "ka_telemetry" / "turn_on.py"

# The command as a child process runs it: the package's own entry function, found through the
# checkout, with no console script installed.
_COMMAND = "import sys; from ka_telemetry.cli import main; sys.exit(main())"
_ENDPOINT = "http://localhost:4318"
_PREFIX = ["--prefix", "kensai"]
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")

# The five keys a write puts in a settings file, and the sixth a print adds under a prefix.
FIVE_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
TELEMETRY_KEYS = (*FIVE_KEYS, "OTEL_RESOURCE_ATTRIBUTES")

# The variables the host this command came from used to read. The command reads none of them, and
# a test that sets them asserts exactly that.
CONTEXT_CAP_KEY = "KENSAIAPP_CONTEXT_CAP"
HOST_TAG_VARIABLES = {
    "INSTRUMENT_RUN_ARM": "canary",
    "INSTRUMENT_RUN_WALK_ID": "walk-123",
    "INSTRUMENT_RUN_CHAIN": "example-foundation",
    "INSTRUMENT_RUN_ROLE": "kensaiapp-auditor",
    "INSTRUMENT_RUN_AUDIT_TARGET": "development-plan",
}


def _clean_environ() -> dict[str, str]:
    """The inherited environment less every telemetry variable, the context cap and the host's own
    variables, so a test measures the code and not the machine it runs on."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OTEL_", "CLAUDE_CODE_ENABLE_TELEMETRY", "INSTRUMENT_RUN_"))
        and key != CONTEXT_CAP_KEY
    }


def _child_environment(
    tmp_path: Path, env_overrides: dict[str, str] | None = None
) -> dict[str, str]:
    """A child's environment: the checkout importable, and a home no test reads from, so the
    default endpoint file is never the machine's own."""
    env = _clean_environ()
    env["PYTHONPATH"] = str(PRODUCT_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = str(tmp_path / "home-of-the-test")
    if env_overrides:
        env.update(env_overrides)
    return env


def _caller_directory(tmp_path: Path, subdir: str = "caller") -> Path:
    caller = tmp_path / subdir
    caller.mkdir(parents=True, exist_ok=True)
    return caller


def _turn_on(
    tmp_path: Path,
    argv: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Runs `ka-telemetry turn-on <argv>` as a child. `cwd` defaults to a directory outside any git
    repository: the pytest process's own directory is a real checkout, and the instance derivation
    would attach its name to every call."""
    return subprocess.run(
        [sys.executable, "-c", _COMMAND, "turn-on", *argv],
        cwd=str(cwd or _caller_directory(tmp_path)),
        capture_output=True,
        text=True,
        env=_child_environment(tmp_path, env_overrides),
        timeout=30,
    )


def _endpoint_file(tmp_path: Path, text: str = _ENDPOINT + "\n") -> Path:
    endpoint_file = tmp_path / "endpoint"
    endpoint_file.write_text(text)
    return endpoint_file


def _print(
    tmp_path: Path,
    labels: list[str] | None = None,
    *,
    prefix: bool = True,
    endpoint_file: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """`--print` for one run: under `--prefix kensai` by default, with one `--label` per entry."""
    argv = ["--print", "--endpoint-file", str(endpoint_file or _endpoint_file(tmp_path))]
    if prefix:
        argv += _PREFIX
    for label in labels or ():
        argv += ["--label", label]
    return _turn_on(tmp_path, argv, env_overrides=env_overrides, cwd=cwd)


def _write(
    tmp_path: Path, settings_file: Path, endpoint_file: Path
) -> subprocess.CompletedProcess:
    return _turn_on(
        tmp_path, ["--settings-file", str(settings_file), "--endpoint-file", str(endpoint_file)]
    )


def _settings(result: subprocess.CompletedProcess) -> dict:
    """The JSON a print gives `claude --settings`."""
    return json.loads(result.stdout)


def _env_of(result: subprocess.CompletedProcess) -> dict[str, str]:
    return _settings(result)["env"]


def _attributes(result: subprocess.CompletedProcess) -> str:
    return _env_of(result).get("OTEL_RESOURCE_ATTRIBUTES", "")


def _pairs(result: subprocess.CompletedProcess) -> list[str]:
    attributes = _attributes(result)
    return attributes.split(",") if attributes else []


def _keys(result: subprocess.CompletedProcess) -> list[str]:
    return [pair.partition("=")[0] for pair in _pairs(result)]


def _run_id_of(result: subprocess.CompletedProcess) -> str:
    for pair in _pairs(result):
        key, _, value = pair.partition("=")
        if key == "kensai.run_id":
            return value
    raise AssertionError(f"no kensai.run_id in {_attributes(result)!r}")


def _notices(result: subprocess.CompletedProcess) -> list[str]:
    """The stderr lines saying something could not be attached or used: ten spaces, then `(`."""
    return [line for line in result.stderr.splitlines() if line.startswith("          (")]


def _summary_attributes(result: subprocess.CompletedProcess) -> str:
    """The attributes value the one summary line on stderr reports."""
    for line in result.stderr.splitlines():
        if line.startswith("          Telemetry:    "):
            return line.removeprefix("          Telemetry:    ").rpartition(" → ")[0]
    raise AssertionError(f"no summary line in {result.stderr!r}")


def _without_run_id(text: str) -> str:
    return _UUID.sub("<run id>", text)


def _same_output_ignoring_the_run_id(
    first: subprocess.CompletedProcess, second: subprocess.CompletedProcess
) -> None:
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    assert _without_run_id(first.stdout) == _without_run_id(second.stdout)
    assert _without_run_id(first.stderr) == _without_run_id(second.stderr)


def _split_pair(pair: str) -> tuple[str, str, str]:
    return pair.partition("=")


def _decode_carried(carried: str) -> str:
    """The typed value, by the procedure `ka-telemetry turn-on --help` states for a carried label
    value: left to right, each `%` and the six hexadecimal digits after it become the character at
    that code point, once. It calls nothing in the package."""
    typed, position = [], 0
    while position < len(carried):
        if carried[position] == "%":
            typed.append(chr(int(carried[position + 1 : position + 7], 16)))
            position += 7
        else:
            typed.append(carried[position])
            position += 1
    return "".join(typed)


# ── The base OTLP export, gated on the published endpoint ────────


class TestBaseTelemetryExport:
    def test_sourcing_alone_has_no_side_effect(self, tmp_path):
        # Importing the module is not running it: no output, no environment variable set.
        probe = (
            "import os; before = dict(os.environ); import ka_telemetry.turn_on; "
            "assert dict(os.environ) == before, 'the import set an environment variable'"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(_caller_directory(tmp_path)),
            capture_output=True,
            text=True,
            env=_child_environment(tmp_path),
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert result.stderr == ""

    def test_given_a_published_endpoint_the_call_exports_otlp_http_json_to_it(self, tmp_path):
        result = _print(tmp_path, prefix=False)
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        assert env_out["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env_out["OTEL_METRICS_EXPORTER"] == "otlp"
        assert env_out["OTEL_LOGS_EXPORTER"] == "otlp"
        assert env_out["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
        assert env_out["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_given_no_such_file_it_exports_nothing_and_returns_success(self, tmp_path):
        missing = tmp_path / "does-not-exist" / "endpoint"
        result = _print(tmp_path, endpoint_file=missing)
        assert result.returncode == 0, result.stderr
        assert "run uninstrumented" in result.stderr
        assert result.stdout == ""

    def test_never_fails_the_caller_even_though_the_wrapper_runs_under_set_dash_e(self, tmp_path):
        # A caller that runs under `set -e` aborts on any non-zero status, so a print with no
        # endpoint exits 0.
        missing = tmp_path / "still-missing"
        result = _print(tmp_path, prefix=False, endpoint_file=missing)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_given_an_unreadable_or_malformed_file_it_behaves_as_if_there_were_none(self, tmp_path):
        garbage = _endpoint_file(tmp_path, "not a url\n")
        result = _print(tmp_path, endpoint_file=garbage)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://telemetry.example.com:4318",
            "https://10.0.0.5:4318",
            "http://127.example.com:4318",
            "http://localhost@telemetry.example.com:4318",
            "http://localhost.example.com:4318",
        ],
    )
    def test_an_endpoint_off_this_machine_is_refused_and_nothing_is_exported(
        self, tmp_path, endpoint
    ):
        """Telemetry stays on the user's machine: a published endpoint naming any host but
        loopback is refused like a malformed one."""
        result = _print(tmp_path, endpoint_file=_endpoint_file(tmp_path, endpoint + "\n"))
        assert result.returncode == 0, result.stderr
        assert "not on this machine" in result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://localhost:4318",
            "http://127.0.0.1:4318/",
            "https://[::1]:4318",
            "http://localhost",
        ],
    )
    def test_a_loopback_endpoint_is_exported(self, tmp_path, endpoint):
        result = _print(tmp_path, endpoint_file=_endpoint_file(tmp_path, endpoint + "\n"))
        assert result.returncode == 0, result.stderr
        assert _env_of(result)["OTEL_EXPORTER_OTLP_ENDPOINT"] == endpoint

    def test_a_directory_where_a_file_is_expected_behaves_as_if_there_were_none(self, tmp_path):
        a_directory = tmp_path / "a-directory"
        a_directory.mkdir()
        result = _print(tmp_path, endpoint_file=a_directory)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_the_default_path_is_resolved_in_one_place_and_the_suite_never_touches_it(
        self, tmp_path
    ):
        # With `HOME=H` and no `--endpoint-file`, the endpoint is read from the file beneath H.
        # Every child in this suite gets a `HOME` of its own making, so the machine's real file is
        # never the one read.
        home = tmp_path / "home-of-the-test"
        published = home / ".claude" / "tools" / "otel-receiver" / "endpoint"
        published.parent.mkdir(parents=True)
        published.write_text("http://localhost:4999\n")

        result = _turn_on(tmp_path, ["--print"])

        assert result.returncode == 0, result.stderr
        assert _env_of(result)["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4999"
        assert _child_environment(tmp_path)["HOME"] == str(home)
        assert os.environ.get("HOME") != str(home)

    def test_the_library_names_no_port_and_no_test_only_path(self):
        assert "4318" not in _TURN_ON_SOURCE.read_text()


# ── The settings-file entry, the same variable set the print gives ────────


class TestUserEntryLibrary:
    """A write builds the five-key entry from the same variable set a print hands a run, never a
    second copy of the list."""

    def test_given_a_published_endpoint_it_yields_the_base_entry(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        result = _write(tmp_path, settings_file, _endpoint_file(tmp_path))
        assert result.returncode == 0, result.stderr
        assert json.loads(settings_file.read_text())["env"] == {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
        }

    def test_the_entry_carries_no_resource_attributes(self, tmp_path):
        # A write puts no OTEL_RESOURCE_ATTRIBUTES in the file: labels are a print's, per run.
        settings_file = tmp_path / "settings.json"
        _write(tmp_path, settings_file, _endpoint_file(tmp_path))
        assert "OTEL_RESOURCE_ATTRIBUTES" not in json.loads(settings_file.read_text())["env"]

    @pytest.mark.parametrize(
        "make_endpoint_file",
        [
            lambda tmp_path: tmp_path / "does-not-exist",
            lambda tmp_path: _garbage_endpoint_file(tmp_path),
            lambda tmp_path: _directory_endpoint_file(tmp_path),
        ],
        ids=["missing", "malformed", "directory"],
    )
    def test_given_no_usable_endpoint_it_yields_nothing_and_returns_success(
        self, tmp_path, make_endpoint_file
    ):
        settings_file = tmp_path / "settings.json"
        result = _write(tmp_path, settings_file, make_endpoint_file(tmp_path))
        assert result.returncode == 0, result.stderr
        assert not settings_file.exists()

    @_NOT_ROOT
    def test_given_an_unreadable_endpoint_file_it_yields_nothing_and_returns_success(
        self, tmp_path
    ):
        endpoint_file = _endpoint_file(tmp_path)
        endpoint_file.chmod(0o000)
        settings_file = tmp_path / "settings.json"
        try:
            result = _write(tmp_path, settings_file, endpoint_file)
        finally:
            endpoint_file.chmod(0o600)
        assert result.returncode == 0, result.stderr
        assert not settings_file.exists()

    def test_key_set_matches_the_launchers_settings_env_less_resource_attributes(self, tmp_path):
        # The parity this test exists to hold: the five keys a write puts in the file equal the
        # printed `env` less its resource attributes. A change to the variable set must reach both.
        endpoint_file = _endpoint_file(tmp_path)
        printed = _env_of(_print(tmp_path, endpoint_file=endpoint_file))
        printed_keys = {key for key in printed if key != "OTEL_RESOURCE_ATTRIBUTES"}

        settings_file = tmp_path / "settings.json"
        _write(tmp_path, settings_file, endpoint_file)
        written = json.loads(settings_file.read_text())["env"]

        assert set(written) == printed_keys == set(FIVE_KEYS)
        assert written == {key: printed[key] for key in printed_keys}


def _garbage_endpoint_file(tmp_path: Path) -> Path:
    garbage = tmp_path / "garbage-endpoint"
    garbage.write_text("not a url\n")
    return garbage


def _directory_endpoint_file(tmp_path: Path) -> Path:
    a_directory = tmp_path / "a-directory"
    a_directory.mkdir()
    return a_directory


# ── The run id, and no arm ────────


class TestArmAndRunId:
    """There is no arm: the host's launchers named one after their own directory, and the package
    attaches nothing the caller's directory or a variable names. What it does attach is a run id."""

    def test_with_nothing_set_the_arm_is_the_calling_launchers_own_directory_name(self, tmp_path):
        # Run from a directory named as a launcher's would be: no `kensai.arm` is attached.
        caller = _caller_directory(tmp_path, "ad-hoc-flow")
        result = _print(tmp_path, cwd=caller)
        assert result.returncode == 0, result.stderr
        assert "kensai.arm" not in _attributes(result)
        assert "kensai.run_id=" in _attributes(result)

    def test_a_launcher_copied_to_a_new_directory_gets_the_new_directorys_arm(self, tmp_path):
        caller = _caller_directory(tmp_path, "ratify")
        result = _print(tmp_path, cwd=caller)
        assert "kensai.arm" not in _attributes(result)
        assert "ratify" not in _attributes(result)

    def test_an_environment_override_replaces_the_arm_and_wins(self, tmp_path):
        caller = _caller_directory(tmp_path, "ad-hoc-flow")
        result = _print(tmp_path, cwd=caller, env_overrides={"INSTRUMENT_RUN_ARM": "canary"})
        assert result.returncode == 0, result.stderr
        assert "kensai.arm" not in _attributes(result)
        assert "canary" not in result.stdout and "canary" not in result.stderr

    def test_an_arm_value_containing_a_space_is_refused_rather_than_exported(self, tmp_path):
        # The variable is not read, so a value that could not be carried is not even mentioned;
        # the run is still reported to the endpoint, with no resource attributes (no prefix).
        result = _print(
            tmp_path,
            prefix=False,
            env_overrides={"INSTRUMENT_RUN_ARM": "two words"},
        )
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        assert "OTEL_RESOURCE_ATTRIBUTES" not in env_out
        assert env_out["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
        assert "two words" not in result.stdout and "two words" not in result.stderr

    def test_a_run_id_accompanies_the_arm_and_is_distinct_per_invocation(self, tmp_path):
        first_id = _run_id_of(_print(tmp_path))
        second_id = _run_id_of(_print(tmp_path))
        assert first_id and second_id
        assert first_id != second_id
        # Distinct AND a real uuid4 — not just two different short-lived tokens.
        assert uuid.UUID(first_id).version == 4
        assert uuid.UUID(second_id).version == 4

    def test_both_keys_carry_the_kensai_prefix(self, tmp_path):
        result = _print(tmp_path, cwd=_caller_directory(tmp_path, "ad-hoc-flow"))
        assert set(_keys(result)) == {"kensai.run_id"}


class TestArmSurvivesHowTheLauncherWasStarted:
    """No arm is attached under any of the shapes a launcher is really started in: through a path
    relative to its own folder, from its own folder against an instance root elsewhere, by absolute
    path from an unrelated directory, and as a copy in a sibling folder. The attribute keys do not
    depend on the folder it was started from.

    A real launcher resolves its own `SCRIPT_DIR`, takes the instance root from the working
    directory or `--instance-root`, `cd`s there, and only then runs the command.
    """

    LAUNCHER_SHAPED_STUB = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'INSTANCE_ROOT="$PWD"\n'
        '[ "${1:-}" = "--instance-root" ] && { INSTANCE_ROOT="$2"; shift 2; }\n'
        'cd "$INSTANCE_ROOT"\n'
        'exec "__PYTHON__" -c "__COMMAND__" turn-on --print --endpoint-file "$TEST_ENDPOINT_FILE" '
        "--prefix kensai\n"
    )

    def _launcher(self, tmp_path: Path, name: str) -> Path:
        launcher_dir = tmp_path / "launchers" / name
        launcher_dir.mkdir(parents=True, exist_ok=True)
        launcher = launcher_dir / "launcher.sh"
        launcher.write_text(
            self.LAUNCHER_SHAPED_STUB.replace("__PYTHON__", sys.executable).replace(
                "__COMMAND__", _COMMAND
            )
        )
        launcher.chmod(0o755)
        return launcher

    def _keys_of(self, result: subprocess.CompletedProcess) -> list[str]:
        assert result.returncode == 0, result.stderr
        assert "kensai.arm" not in result.stdout
        return _keys(result)

    def _run_from(self, tmp_path: Path, cwd: Path, argv: list[str], endpoint_file: Path):
        env = _child_environment(tmp_path, {"TEST_ENDPOINT_FILE": str(endpoint_file)})
        return subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=30
        )

    @pytest.fixture
    def endpoint_file(self, tmp_path) -> Path:
        return _endpoint_file(tmp_path)

    def test_started_by_a_path_relative_to_its_own_folder(self, tmp_path, endpoint_file):
        """`cd <the launcher's folder> && ./launcher.sh` — the instance root is the cwd, which
        is where the launcher already sits. No arm is attached."""
        launcher = self._launcher(tmp_path, "ratify")
        result = self._run_from(tmp_path, launcher.parent, ["./launcher.sh"], endpoint_file)
        assert self._keys_of(result) == ["kensai.run_id"]

    def test_started_from_its_own_folder_against_an_instance_root_elsewhere(
        self, tmp_path, endpoint_file
    ):
        """`cd <the launcher's folder> && ./launcher.sh --instance-root <elsewhere>` — the
        launcher's own flag for the case where the instance is not the cwd. Neither the launcher's
        name nor the instance root's turns up in the output."""
        launcher = self._launcher(tmp_path, "ratify")
        instance_root = tmp_path / "some-instance"
        instance_root.mkdir()
        result = self._run_from(
            tmp_path,
            launcher.parent,
            ["./launcher.sh", "--instance-root", str(instance_root)],
            endpoint_file,
        )
        assert self._keys_of(result) == ["kensai.run_id"]
        assert "some-instance" not in result.stdout
        assert "ratify" not in result.stdout

    def test_started_by_absolute_path_from_an_unrelated_cwd(self, tmp_path, endpoint_file):
        """The documented invocation — the launcher by absolute path, run from the instance
        root. Held beside the other two so all three shapes are one comparison."""
        launcher = self._launcher(tmp_path, "ratify")
        instance_root = tmp_path / "some-instance"
        instance_root.mkdir()
        result = self._run_from(tmp_path, instance_root, ["bash", str(launcher)], endpoint_file)
        assert self._keys_of(result) == ["kensai.run_id"]

    def test_a_launcher_copied_to_a_sibling_folder_reports_the_new_folder(
        self, tmp_path, endpoint_file
    ):
        """A byte-identical file in a differently-named folder gives the same attribute keys: no
        key depends on the folder it was started from."""
        copied = self._launcher(tmp_path, "ratify-copy")
        result = self._run_from(tmp_path, copied.parent, ["./launcher.sh"], endpoint_file)
        assert self._keys_of(result) == ["kensai.run_id"]
        assert "ratify-copy" not in result.stdout


# ── The settings a run is handed ────────


class TestHandsItsVariablesOverAsSettings:
    """An exported variable never reaches a `claude --bg` session; `--settings '{"env": …}'`
    does —
    measured 2026-09-10 over three haiku probes against the live receiver. So a print gives that
    JSON on stdout, and it must carry exactly what the summary on stderr reports."""

    def test_given_a_published_endpoint_the_settings_carry_exactly_what_was_exported(
        self, tmp_path
    ):
        result = _print(tmp_path, prefix=False, cwd=_caller_directory(tmp_path, "ad-hoc-flow"))
        assert result.returncode == 0, result.stderr
        settings = _settings(result)
        assert set(settings) == {"env"}
        assert set(settings["env"]) == set(FIVE_KEYS)
        assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
        assert "kensai.arm" not in result.stdout

    def test_given_no_endpoint_no_settings_are_left_behind(self, tmp_path):
        result = _print(tmp_path, prefix=False, endpoint_file=tmp_path / "absent")
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_a_refused_arm_leaves_the_settings_without_the_resource_attributes(self, tmp_path):
        # With no prefix no attributes are attached at all; the endpoint still goes out.
        result = _print(
            tmp_path, prefix=False, env_overrides={"INSTRUMENT_RUN_ARM": "two words"}
        )
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        assert "OTEL_RESOURCE_ATTRIBUTES" not in env_out
        assert env_out["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_the_settings_name_no_console_exporter(self, tmp_path):
        result = _print(tmp_path, prefix=False)
        assert not any("console" in value for value in _env_of(result).values())

    def test_an_exported_context_cap_rides_the_settings_env(self, tmp_path):
        # The host's context cap is not the command's to hand over: it never appears, set or not.
        result = _print(tmp_path, env_overrides={CONTEXT_CAP_KEY: "80000"})
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        assert CONTEXT_CAP_KEY not in env_out
        assert "80000" not in result.stdout and "80000" not in result.stderr
        assert set(env_out) == set(TELEMETRY_KEYS)

    def test_an_unexported_context_cap_is_absent_and_nothing_raises(self, tmp_path):
        result = _print(tmp_path)
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        assert CONTEXT_CAP_KEY not in env_out
        assert env_out["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_an_exported_context_cap_reaches_the_settings_of_an_uninstrumented_run(self, tmp_path):
        """A run left uninstrumented hands over nothing: with no endpoint, stdout is empty, so no
        context cap reaches a `claude --bg` session through this command."""
        result = _print(
            tmp_path, endpoint_file=tmp_path / "absent", env_overrides={CONTEXT_CAP_KEY: "80000"}
        )
        assert result.returncode == 0, result.stderr
        assert "run uninstrumented" in result.stderr
        assert result.stdout == ""
        assert CONTEXT_CAP_KEY not in result.stderr

    def test_an_empty_context_cap_is_left_out_like_an_unexported_one(self, tmp_path):
        result = _print(tmp_path, env_overrides={CONTEXT_CAP_KEY: ""})
        assert result.returncode == 0, result.stderr
        assert CONTEXT_CAP_KEY not in _env_of(result)
        assert CONTEXT_CAP_KEY not in result.stderr


class TestNoConsoleFallback:
    """A detached `claude --bg` run's console export lands nowhere a launcher's redirected file can
    read back — measured, and recorded in the history of the library this suite began as. So the
    treatment that matches the answer is exporting no console exporter at all, asserted on the
    printed variable VALUES, not by grepping the source for the word."""

    def test_no_exported_value_contains_console(self, tmp_path):
        env_out = _env_of(_print(tmp_path, prefix=False))
        exporter_keys = (
            "OTEL_METRICS_EXPORTER",
            "OTEL_LOGS_EXPORTER",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
        )
        for key in exporter_keys:
            assert "console" not in env_out.get(key, ""), (
                f"{key}={env_out.get(key)!r} names a console exporter, but a --bg run's console "
                "output cannot be read back from anywhere"
            )

    def test_killing_the_receiver_leaves_a_readable_message_not_a_silent_success(self, tmp_path):
        """"Killing the receiver" is, from the command's own vantage point, indistinguishable
        from the receiver never having been installed: both mean the published endpoint file is
        gone. Simulated the same way as the absent-file case: a path nothing occupies."""
        missing = tmp_path / "receiver-was-here-and-is-now-gone"
        result = _print(tmp_path, endpoint_file=missing)

        assert result.returncode == 0, result.stderr
        assert "run uninstrumented" in result.stderr, (
            "a dead receiver must leave something a person can read that says the run was not "
            "collected, not a silent success"
        )
        assert result.stdout == ""


# ── The instance is derived, not passed ────────


class TestInstanceIsDerived:
    """Every record says which instance ran it, and a caller cannot get it wrong because the
    caller never states it — the instance is read from git, not passed."""

    def _git(self, *args: str, cwd: Path) -> None:
        # The scratch repository is isolated from whatever commit hook the machine configures
        # through GIT_CONFIG_*: `-c` on the command line wins over them, per command, so no
        # configuration is modified and the inherited variables stay as the machine set them.
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    def _repo(self, tmp_path: Path, name: str) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        self._git("init", "-q", cwd=repo)
        self._git("config", "user.email", "a@b.c", cwd=repo)
        self._git("config", "user.name", "a", cwd=repo)
        (repo / "README.md").write_text("x")
        self._git("add", ".", cwd=repo)
        self._git("commit", "-q", "-m", "first", cwd=repo)
        return repo

    def test_called_inside_a_git_repository_attaches_the_repos_own_name(self, tmp_path):
        repo = self._repo(tmp_path, "an-instance")
        result = _print(tmp_path, cwd=repo)
        assert result.returncode == 0, result.stderr
        assert "kensai.instance=an-instance" in _attributes(result)

    def test_a_worktree_of_that_repository_yields_the_same_name_as_its_main_checkout(
        self, tmp_path
    ):
        repo = self._repo(tmp_path, "an-instance")
        worktree = tmp_path / "worktree-of-an-instance"
        self._git("worktree", "add", "-q", str(worktree), "-b", "wt-branch", cwd=repo)
        result = _print(tmp_path, cwd=worktree)
        assert result.returncode == 0, result.stderr
        assert "kensai.instance=an-instance" in _attributes(result)

    def test_called_outside_any_git_repository_attaches_no_instance_and_the_rest_still_goes_out(
        self, tmp_path
    ):
        outside = tmp_path / "no-git-here"
        outside.mkdir()
        result = _print(tmp_path, cwd=outside)
        assert result.returncode == 0, result.stderr
        attributes = _attributes(result)
        assert "kensai.instance" not in attributes
        assert "kensai.run_id=" in attributes

    def test_the_value_reaches_the_settings_exactly_as_it_reaches_the_export(self, tmp_path):
        # The package exports nothing, so "the export" is the summary line: the attributes the
        # JSON hands over equal the ones the summary reports.
        repo = self._repo(tmp_path, "an-instance")
        result = _print(tmp_path, cwd=repo)
        assert _attributes(result) == _summary_attributes(result)
        assert "kensai.instance=an-instance" in _attributes(result)

    @pytest.mark.parametrize(
        "repo_name", ["an instance", "an,instance"], ids=["space", "comma"]
    )
    def test_an_instance_name_otel_cannot_carry_drops_that_key_alone(self, tmp_path, repo_name):
        """A bad value drops its own key, not the run's tags. A checkout directory is free to carry
        a space or a comma; the derived instance is a value like any other."""
        repo = self._repo(tmp_path, repo_name)
        result = _print(tmp_path, cwd=repo)
        assert result.returncode == 0, result.stderr
        assert _keys(result) == ["kensai.run_id"]


# ── The labels a person gives ────────


class TestLauncherSubjectAndProduct:
    """The caller already holds the project, document or skill it works on. It hands them over as
    labels, and each is stamped under the prefix. Nothing is read from a project card."""

    def test_each_accepted_key_is_stamped_as_kensai_key(self, tmp_path):
        result = _print(
            tmp_path,
            [
                "project=widgetco",
                "document=fdr",
                "skill=cold-read",
                "target_instance=sample-instance",
            ],
        )
        assert result.returncode == 0, result.stderr
        attrs = _attributes(result)
        assert "kensai.project=widgetco" in attrs
        assert "kensai.document=fdr" in attrs
        assert "kensai.skill=cold-read" in attrs
        assert "kensai.target_instance=sample-instance" in attrs

    def test_an_unknown_key_is_refused_with_one_line_and_the_call_still_returns_0(self, tmp_path):
        # Any key is accepted, so `bogus=value` goes out as `kensai.bogus=value` with no line.
        result = _print(tmp_path, ["bogus=value"])
        assert result.returncode == 0, result.stderr
        assert "kensai.bogus=value" in _attributes(result)
        assert _notices(result) == []

    def test_an_empty_value_attaches_nothing_for_that_key(self, tmp_path):
        result = _print(tmp_path, ["document="])
        assert result.returncode == 0, result.stderr
        assert "kensai.document" not in _attributes(result)

    @pytest.mark.parametrize(
        "typed_value",
        ["has space", 'has"quote', "has,comma", "has;semi", "has\\back", "has=eq"],
        ids=["space", "quote", "comma", "semicolon", "backslash", "equals"],
    )
    def test_a_value_otel_cannot_carry_as_typed_is_attached_encoded_and_decodes_back(
        self, tmp_path, typed_value
    ):
        result = _print(tmp_path, [f"document={typed_value}", "skill=cold-read"])
        assert result.returncode == 0, result.stderr
        carried = {key: value for key, _, value in map(_split_pair, _pairs(result))}
        assert _decode_carried(carried["kensai.document"]) == typed_value
        assert carried["kensai.skill"] == "cold-read"
        assert not any(c.isspace() or c in '",;\\=' for c in carried["kensai.document"])
        assert _notices(result) == []

    def _card(self, caller: Path, product_line: str | None) -> None:
        project_dir = caller / "system" / "projects" / "widgetco"
        project_dir.mkdir(parents=True)
        text = product_line or "# Project Context\nNo product line here.\n"
        (project_dir / "project.md").write_text(text)

    def test_project_with_a_readable_product_path_attaches_kensai_product(self, tmp_path):
        # The host read the product from a project card. The package reads no card, so no
        # `kensai.product` is attached, card or no card.
        caller = _caller_directory(tmp_path, "ad-hoc-flow")
        self._card(
            caller,
            "**Product path:** /home/user/work/private/widgetco-corp/widgetco"
            "   <!-- absolute -->\n",
        )
        result = _print(tmp_path, ["project=widgetco"], cwd=caller)
        assert result.returncode == 0, result.stderr
        assert "kensai.project=widgetco" in _attributes(result)
        assert "kensai.product" not in _attributes(result)

    def test_no_project_md_attaches_no_product(self, tmp_path):
        result = _print(tmp_path, ["project=widgetco"])
        assert result.returncode == 0, result.stderr
        assert "kensai.product" not in _attributes(result)

    def test_no_product_path_line_attaches_no_product(self, tmp_path):
        caller = _caller_directory(tmp_path)
        self._card(caller, None)
        result = _print(tmp_path, ["project=widgetco"], cwd=caller)
        assert result.returncode == 0, result.stderr
        assert "kensai.product" not in _attributes(result)

    def test_an_unknown_key_does_not_drop_an_accepted_key_beside_it(self, tmp_path):
        """A refusal drops its own key, never the run's other tags — and `bogus=value` is not one:
        it goes out beside `skill=cold-read`, with no line."""
        result = _print(tmp_path, ["bogus=value", "skill=cold-read"])
        assert result.returncode == 0, result.stderr
        attrs = _attributes(result)
        assert "kensai.skill=cold-read" in attrs
        assert "kensai.bogus=value" in attrs
        assert _notices(result) == []

    def test_a_product_otel_cannot_carry_is_dropped_and_the_project_still_goes_out(self, tmp_path):
        """A project card whose product path would not be a carriable value changes nothing: the
        project goes out and no product is attached."""
        caller = _caller_directory(tmp_path, "ad-hoc-flow")
        self._card(caller, "**Product path:** /srv/products/widget,co   <!-- absolute -->\n")
        result = _print(tmp_path, ["project=widgetco"], cwd=caller)
        assert result.returncode == 0, result.stderr
        attrs = _attributes(result)
        assert "kensai.project=widgetco" in attrs
        assert "kensai.product" not in attrs

    def test_called_with_no_arguments_behaves_exactly_as_today(self, tmp_path):
        result = _print(tmp_path, cwd=_caller_directory(tmp_path, "ad-hoc-flow"))
        assert result.returncode == 0, result.stderr
        assert set(_keys(result)) == {"kensai.run_id"}


# ── Host tags are not attached ────────


class TestChainWalkAttributes:
    """The host read `INSTRUMENT_RUN_WALK_ID` / `INSTRUMENT_RUN_CHAIN` from its caller's environment
    and stamped `kensai.walk_id` / `kensai.chain`. The package reads neither: set, set to a value
    it could not carry, or unset, the output is the same, run id aside."""

    def test_both_set_and_exportable_are_stamped(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(
            tmp_path,
            env_overrides={
                "INSTRUMENT_RUN_WALK_ID": "walk-123",
                "INSTRUMENT_RUN_CHAIN": "example-foundation",
            },
        )
        attrs = _attributes(result)
        assert "kensai.walk_id" not in attrs
        assert "kensai.chain" not in attrs
        _same_output_ignoring_the_run_id(result, baseline)

    def test_neither_set_stamps_neither(self, tmp_path):
        attrs = _attributes(_print(tmp_path))
        assert "kensai.walk_id" not in attrs
        assert "kensai.chain" not in attrs

    def test_an_unexportable_walk_id_is_dropped_and_the_rest_still_goes_out(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(
            tmp_path,
            env_overrides={
                "INSTRUMENT_RUN_WALK_ID": "bad,id",
                "INSTRUMENT_RUN_CHAIN": "example-foundation",
            },
        )
        assert "kensai.walk_id" not in _attributes(result)
        assert "kensai.chain" not in _attributes(result)
        assert "bad,id" not in result.stdout and "bad,id" not in result.stderr
        _same_output_ignoring_the_run_id(result, baseline)

    def test_an_unexportable_chain_name_is_dropped_and_the_walk_id_still_goes_out(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(
            tmp_path,
            env_overrides={
                "INSTRUMENT_RUN_WALK_ID": "walk-123",
                "INSTRUMENT_RUN_CHAIN": "bad chain",
            },
        )
        assert "kensai.chain" not in _attributes(result)
        assert "kensai.walk_id" not in _attributes(result)
        _same_output_ignoring_the_run_id(result, baseline)


class TestDirectIdentityAttributes:
    """The host read `INSTRUMENT_RUN_ROLE` / `INSTRUMENT_RUN_AUDIT_TARGET` and stamped
    `kensai.role` / `kensai.audit_target`. The package reads neither, and a person who wants
    those pairs gives them as labels."""

    def test_role_and_audit_target_are_stamped_directly(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(tmp_path, env_overrides=dict(HOST_TAG_VARIABLES))
        attrs = _attributes(result)
        for tag in ("role", "audit_target", "walk_id", "chain", "arm"):
            assert f"kensai.{tag}" not in attrs
        _same_output_ignoring_the_run_id(result, baseline)

    def test_unset_role_and_audit_target_stamp_neither(self, tmp_path):
        attrs = _attributes(_print(tmp_path))
        assert "kensai.role" not in attrs
        assert "kensai.audit_target" not in attrs

    def test_unexportable_role_drops_only_role(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(
            tmp_path,
            env_overrides={**HOST_TAG_VARIABLES, "INSTRUMENT_RUN_ROLE": "bad role"},
        )
        attrs = _attributes(result)
        assert "kensai.role" not in attrs
        assert "kensai.audit_target" not in attrs
        assert _notices(result) == []
        _same_output_ignoring_the_run_id(result, baseline)

    def test_unexportable_audit_target_drops_only_audit_target(self, tmp_path):
        baseline = _print(tmp_path)
        result = _print(
            tmp_path,
            env_overrides={**HOST_TAG_VARIABLES, "INSTRUMENT_RUN_AUDIT_TARGET": "bad,target"},
        )
        attrs = _attributes(result)
        assert "kensai.audit_target" not in attrs
        assert "kensai.role" not in attrs
        assert _notices(result) == []
        _same_output_ignoring_the_run_id(result, baseline)

    def test_direct_identity_coexists_with_the_launchers_subject_attributes(self, tmp_path):
        """The labels a caller gives still go out on the same run, with the host variables set and
        none of them attached."""
        result = _print(
            tmp_path,
            ["skill=development-audit", "project=tools"],
            cwd=_caller_directory(tmp_path, "skill"),
            env_overrides=dict(HOST_TAG_VARIABLES),
        )
        assert result.returncode == 0, result.stderr
        pairs = _pairs(result)
        keys = _keys(result)
        assert "kensai.skill=development-audit" in pairs
        assert "kensai.project=tools" in pairs
        assert "kensai.run_id" in keys
        for tag in ("arm", "walk_id", "chain", "role", "audit_target"):
            assert f"kensai.{tag}" not in keys
        # Each identity is stamped once — never a duplicate key a reader must disambiguate.
        assert len(keys) == len(set(keys))

    def test_role_and_audit_target_are_not_accepted_as_key_value_arguments(self, tmp_path):
        """Any key is accepted as a label, so a person who gives `role=` or `audit_target=` gets
        them: `kensai.role=...` goes out because the user gave it."""
        result = _print(tmp_path, ["role=kensaiapp-auditor", "audit_target=development-plan"])
        assert result.returncode == 0, result.stderr
        attrs = _attributes(result)
        assert "kensai.role=kensaiapp-auditor" in attrs
        assert "kensai.audit_target=development-plan" in attrs

    def test_settings_carry_direct_identity_without_tool_details(self, tmp_path):
        result = _print(tmp_path, env_overrides=dict(HOST_TAG_VARIABLES))
        assert result.returncode == 0, result.stderr
        env_out = _env_of(result)
        # No settings key is added beyond the telemetry set, and no tool details are asked for.
        assert set(env_out) == set(TELEMETRY_KEYS)
        assert "OTEL_LOG_TOOL_DETAILS" not in env_out
        attrs = env_out["OTEL_RESOURCE_ATTRIBUTES"]
        assert "kensai.role" not in attrs
        assert "kensai.audit_target" not in attrs
        forbidden = ("prompt", "tool_input", "tool-input", "command", "file_path", "file-path")
        for key, value in env_out.items():
            for term in forbidden:
                assert term not in key.lower()
                assert term not in value.lower()


# ── Vendor neutrality — no port, no host-specific literal ────────


def test_library_source_carries_no_literal_port():
    assert "4318" not in _TURN_ON_SOURCE.read_text()


class TestSurfacedByIntegrity:
    """Two items a code-integrity report raised against the library this suite began as: a bare
    word is named rather than dropped in silence, and a value holding a character
    `OTEL_RESOURCE_ATTRIBUTES` cannot carry as typed is carried, encoded, like every other
    value."""

    def test_a_bare_word_argument_is_named_in_one_line_and_the_call_still_returns_0(self, tmp_path):
        result = _print(tmp_path, ["widgetco", "project=widgetco"])
        assert result.returncode == 0, result.stderr
        assert [
            line for line in result.stderr.splitlines() if "'widgetco'" in line and "no '='" in line
        ]
        assert "kensai.project=widgetco" in _attributes(result)

    def test_an_arm_holding_a_comma_is_refused_and_the_rest_still_goes_out(self, tmp_path):
        # The host's arm is gone; a label value holding a comma is now carried (CP-270), so the
        # rest still goes out and the comma value goes out with it, encoded.
        result = _print(tmp_path, ["project=widgetco", "skill=a,b"])
        assert result.returncode == 0, result.stderr
        attrs = _attributes(result)
        assert "kensai.arm=" not in attrs
        assert "kensai.skill=a%00002Cb" in attrs
        assert "kensai.project=widgetco" in attrs
        assert _notices(result) == []


# ── Provenance — the provider a run went through, the model and effort it went at, and where
#    those came from ────


class TestProvenanceKeys:
    """`provider=`, `model=`, `effort=` and `source=` are labels like any other: each is stamped
    `kensai.<key>` under the rules every label follows."""

    _NEW_KEYS = ("provider", "model", "effort", "source")

    def _call(self, tmp_path: Path, labels: list[str]) -> subprocess.CompletedProcess:
        return _print(tmp_path, labels, cwd=_caller_directory(tmp_path, "skill"))

    def test_provider_model_effort_source_are_stamped_as_kensai_keys(self, tmp_path):
        result = self._call(
            tmp_path,
            ["provider=codex", "model=gpt-6.1-sol", "effort=high", "source=intelligence"],
        )
        assert result.returncode == 0, result.stderr
        handed_over = _pairs(result)
        reported = _summary_attributes(result).split(",")
        for pair in ("kensai.provider=codex", "kensai.model=gpt-6.1-sol",
                     "kensai.effort=high", "kensai.source=intelligence"):
            assert pair in handed_over
            assert pair in reported

    def test_empty_model_and_effort_attach_nothing_but_source_is_stamped(self, tmp_path):
        result = self._call(tmp_path, ["provider=codex", "model=", "effort=", "source=default"])
        assert result.returncode == 0, result.stderr
        keys = _keys(result)
        assert "kensai.model" not in keys
        assert "kensai.effort" not in keys
        assert "kensai.provider" in keys
        assert "kensai.source" in keys

    def test_model_containing_a_comma_drops_only_that_key(self, tmp_path):
        # The id is a base id and stays; since CP-270 the comma value is carried, encoded.
        result = self._call(
            tmp_path, ["skill=ad-hoc-plan", "provider=codex", "model=a,b", "source=tier"]
        )
        assert result.returncode == 0, result.stderr
        pairs = _pairs(result)
        assert "kensai.model=a%00002Cb" in pairs
        assert "kensai.provider=codex" in pairs
        assert "kensai.source=tier" in pairs
        assert "kensai.skill=ad-hoc-plan" in pairs
        assert _notices(result) == []

    @pytest.mark.parametrize("key", _NEW_KEYS)
    def test_each_new_key_with_an_unexportable_value_drops_only_itself(self, tmp_path, key):
        """The id is a base id and stays. Since CP-270 no key is refused: every one of the four
        carries a value with a space, encoded, and the others are untouched."""
        values = {"provider": "codex", "model": "m", "effort": "high", "source": "tier"}
        values[key] = "has space"
        result = self._call(tmp_path, [f"{k}={v}" for k, v in values.items()])
        assert result.returncode == 0, result.stderr
        pairs = _pairs(result)
        assert f"kensai.{key}=has%000020space" in pairs
        for other in self._NEW_KEYS:
            if other != key:
                assert f"kensai.{other}={values[other]}" in pairs

    def test_existing_keys_and_order_are_unchanged(self, tmp_path):
        """The labels keep the order they were given in: the run id first, then each label, here
        the skill launcher's own order, subject first."""
        result = self._call(
            tmp_path,
            ["skill=development-specs", "project=tools", "provider=claude",
             "model=claude-opus-5-5", "effort=high", "source=intelligence"],
        )
        assert result.returncode == 0, result.stderr
        assert _keys(result) == [
            "kensai.run_id", "kensai.skill", "kensai.project",
            "kensai.provider", "kensai.model", "kensai.effort", "kensai.source",
        ]

    def test_unknown_key_message_names_all_eight_accepted_keys(self, tmp_path):
        # There is no list of accepted keys, so no refusal line: `bogus=value` goes out.
        result = self._call(tmp_path, ["bogus=value"])
        assert result.returncode == 0, result.stderr
        assert _notices(result) == []
        assert "kensai.bogus=value" in _pairs(result)
