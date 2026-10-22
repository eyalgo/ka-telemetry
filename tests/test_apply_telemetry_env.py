"""Tests for the settings-file half of `ka-telemetry turn-on` — the command a person runs once per
machine to write (or remove) the telemetry `env` entry in a Claude Code settings file.

These tests began as the tests of a root script and a shell library that built the entry. They now
run the package command in process (`ka_telemetry.turn_on.run`) with real files, so the entry is
`turn_on.telemetry_entry` and never a second copy of the variable set. The collected names are the
ones the root script's tests had.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ka_telemetry import turn_on

_TELEMETRY_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
_ENDPOINT = "http://localhost:4318"
# A home no test reads from: every test that is not about the default names its endpoint file.
_NO_HOME = Path("/nonexistent-home-of-the-turn-on-tests")
_RUN_ID = "7f3c2a9e-4b1d-4c6a-9e0f-2d5b8a1c3e47"
_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")


def _entry(endpoint: str) -> dict[str, str]:
    return turn_on.telemetry_entry(endpoint)


def _endpoint_file(tmp_path: Path) -> Path:
    endpoint_file = tmp_path / "endpoint"
    endpoint_file.write_text(_ENDPOINT + "\n")
    return endpoint_file


def _run_main(argv: list[str], home: Path = _NO_HOME) -> int:
    runtime = turn_on.Runtime(
        files=turn_on.Files(),
        processes=turn_on.Processes(),
        new_run_id=lambda: _RUN_ID,
        home=home,
        cwd=Path.cwd(),
    )
    return turn_on.run(argv, runtime=runtime)


class TestMerge:
    def test_merges_the_five_keys_creating_env_if_absent(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])
        assert rc == 0

        result = json.loads(settings_file.read_text())
        assert result["permissions"] == {"allow": ["Bash"]}
        assert result["env"] == _entry(_ENDPOINT)

    def test_other_env_keys_and_other_top_level_keys_survive_byte_for_byte(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        original = {
            "env": {"SOME_OTHER_VAR": "kept", "OTEL_RESOURCE_ATTRIBUTES": "kensai.arm=x"},
            "hooks": {"PreToolUse": [{"matcher": "Bash"}]},
            "aNumber": 7,
            "unicode": "café",
        }
        settings_file.write_text(json.dumps(original))

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])
        assert rc == 0

        result = json.loads(settings_file.read_text())
        assert result["hooks"] == original["hooks"]
        assert result["aNumber"] == 7
        assert result["unicode"] == "café"
        assert result["env"]["SOME_OTHER_VAR"] == "kept"
        assert result["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "kensai.arm=x"
        for key in _TELEMETRY_KEYS:
            assert result["env"][key]

    def test_running_twice_changes_nothing(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"env": {"KEEP": "me"}}))
        argv = ["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))]

        assert _run_main(argv) == 0
        first = settings_file.read_text()
        assert _run_main(argv) == 0
        assert settings_file.read_text() == first

    def test_given_no_published_endpoint_it_writes_nothing_and_exits_zero(self, tmp_path, capsys):
        settings_file = tmp_path / "settings.json"
        original_text = json.dumps({"env": {"KEEP": "me"}})
        settings_file.write_text(original_text)

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(tmp_path / "does-not-exist")])
        assert rc == 0
        assert settings_file.read_text() == original_text
        assert "no receiver" in capsys.readouterr().out

    def test_given_settings_file_that_is_not_valid_json_it_writes_nothing_and_exits_nonzero(self, tmp_path, capsys):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text("not json at all")

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])
        assert rc != 0
        assert settings_file.read_text() == "not json at all"
        assert str(settings_file) in capsys.readouterr().err

    def test_given_settings_bytes_that_are_not_utf8_it_writes_nothing_and_names_the_file(self, tmp_path, capsys):
        # JSON is UTF-8 (RFC 8259 §8.1), so bytes that do not decode are "not valid JSON" — the
        # same refusal, naming the file, not a traceback.
        settings_file = tmp_path / "settings.json"
        original_bytes = b'{"env": {"A": "\xff"}}'
        settings_file.write_bytes(original_bytes)

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])
        assert rc != 0
        assert settings_file.read_bytes() == original_bytes
        assert str(settings_file) in capsys.readouterr().err

    def test_given_no_published_endpoint_a_missing_settings_file_is_not_created(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(tmp_path / "does-not-exist")])
        assert rc == 0
        assert not settings_file.exists()

    def test_a_settings_file_that_does_not_exist_yet_is_treated_as_empty(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])
        assert rc == 0
        assert json.loads(settings_file.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == _ENDPOINT


class TestYourOwnValuesAreNotOverwritten:
    """A user who already set one of the five keys to something else keeps it unless they say so."""

    def _settings(self, tmp_path: Path) -> Path:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.corp.example"}})
        )
        return settings_file

    def test_a_different_value_already_there_is_refused_by_name_and_nothing_is_written(self, tmp_path, capsys):
        # Kept under its id. The user's value is kept and the key named, with `--replace` named;
        # CP-278 writes the other four keys rather than refusing.
        settings_file = self._settings(tmp_path)

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))])

        assert rc == 0
        written = json.loads(settings_file.read_text())["env"]
        assert written["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.corp.example"
        assert set(written) == set(_TELEMETRY_KEYS)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in captured.out.splitlines()[-1]
        assert "collector.corp.example" not in captured.out
        assert "--replace" in captured.out

    def test_replace_overwrites_it(self, tmp_path):
        settings_file = self._settings(tmp_path)
        rc = _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path)), "--replace"]
        )
        assert rc == 0
        assert json.loads(settings_file.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == _ENDPOINT


class TestRemove:
    def _argv(self, settings_file: Path, tmp_path: Path, *extra: str) -> list[str]:
        return ["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path)), "--remove", *extra]

    def _write_argv(self, settings_file: Path, tmp_path: Path) -> list[str]:
        endpoint_file = str(_endpoint_file(tmp_path))
        return ["--settings-file", str(settings_file), "--endpoint-file", endpoint_file]

    def test_deletes_exactly_the_five_keys_and_leaves_everything_else(self, tmp_path, capsys):
        # What a write put there is the command's and goes; what it did not write stays.
        settings_file = tmp_path / "settings.json"
        original = {"env": {"KEEP_ME": "yes"}, "permissions": {"allow": ["Bash"]}}
        settings_file.write_text(json.dumps(original))
        assert _run_main(self._write_argv(settings_file, tmp_path)) == 0
        assert _run_main(self._argv(settings_file, tmp_path)) == 0
        assert json.loads(settings_file.read_text()) == original
        # Five keys with no record are the user's: left whole, and named.
        preloaded = {
            "env": {**_entry(_ENDPOINT), "KEEP_ME": "yes"},
            "permissions": {"allow": ["Bash"]},
        }
        settings_file.write_text(json.dumps(preloaded))
        capsys.readouterr()
        assert _run_main(self._argv(settings_file, tmp_path)) == 0
        assert json.loads(settings_file.read_text()) == preloaded
        out = capsys.readouterr().out
        assert all(key in out for key in _TELEMETRY_KEYS)

    def test_env_key_is_dropped_entirely_when_no_other_keys_remain_in_it(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))

        assert _run_main(self._write_argv(settings_file, tmp_path)) == 0
        assert _run_main(self._argv(settings_file, tmp_path)) == 0

        result = json.loads(settings_file.read_text())
        assert "env" not in result
        assert result == {"permissions": {"allow": ["Bash"]}}

    def test_nothing_to_remove_leaves_the_file_byte_for_byte(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        original_text = '{"permissions":{"allow":["Bash"]},"env":{"KEEP":"me"}}'
        settings_file.write_text(original_text)

        assert _run_main(self._argv(settings_file, tmp_path)) == 0
        assert settings_file.read_text() == original_text

    def test_a_missing_settings_file_is_not_created(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        assert _run_main(self._argv(settings_file, tmp_path)) == 0
        assert not settings_file.exists()

    def test_remove_deletes_exactly_what_merge_wrote(self, tmp_path):
        # A merge then a remove returns the original, so the remove path's own key list cannot
        # drift from the library's variable set unseen.
        settings_file = tmp_path / "settings.json"
        original = {"env": {"KEEP": "me"}, "permissions": {"allow": ["Bash"]}}
        settings_file.write_text(json.dumps(original))

        assert _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))]) == 0
        assert _run_main(self._argv(settings_file, tmp_path)) == 0
        assert json.loads(settings_file.read_text()) == original

    def test_a_value_this_command_would_not_have_written_is_left_and_named(self, tmp_path, capsys):
        settings_file = tmp_path / "settings.json"
        assert _run_main(self._write_argv(settings_file, tmp_path)) == 0
        written = json.loads(settings_file.read_text())
        written["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://collector.corp.example"
        settings_file.write_text(json.dumps(written))
        capsys.readouterr()

        assert _run_main(self._argv(settings_file, tmp_path)) == 0

        assert json.loads(settings_file.read_text()) == {
            "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.corp.example"}
        }
        out = capsys.readouterr().out
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in out
        assert "collector.corp.example" not in out

    def test_with_no_published_endpoint_it_refuses_rather_than_guess(self, tmp_path, capsys):
        settings_file = tmp_path / "settings.json"
        before = json.dumps({"env": {**_entry(_ENDPOINT), "KEEP": "me"}})
        settings_file.write_text(before)

        rc = _run_main(["--settings-file", str(settings_file), "--endpoint-file", str(tmp_path / "nope"), "--remove"])

        assert rc != 0
        assert settings_file.read_text() == before
        assert "--force" in capsys.readouterr().err

    def test_force_deletes_the_five_keys_whoever_set_them(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"env": {**_entry("https://collector.corp.example"), "KEEP": "me"}})
        )

        rc = _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(tmp_path / "nope"), "--remove", "--force"]
        )

        assert rc == 0
        assert json.loads(settings_file.read_text())["env"] == {"KEEP": "me"}


class TestALibraryFailureIsNotNoReceiver:
    """A settings path the command cannot use is a failure, reported as one: it names the path,
    writes and creates nothing, and is never reported as "no receiver". The ids are those of the
    root script's three library failures; the cases are CP-228's three settings-path failures."""

    @pytest.mark.parametrize(
        "kind",
        [
            pytest.param("directory", id="nonzero-exit"),
            pytest.param("missing-parent", id="stderr-only"),
            pytest.param("unreadable", id="not-json"),
        ],
    )
    def test_it_exits_nonzero_says_the_library_failed_and_writes_nothing(
        self, tmp_path, capsys, kind
    ):
        if kind == "unreadable" and os.geteuid() == 0:
            pytest.skip("root reads files whatever their mode")
        settings_file = tmp_path / "settings.json"
        if kind == "directory":
            settings_file.mkdir()
        elif kind == "missing-parent":
            settings_file = tmp_path / "no-such-directory" / "settings.json"
        else:
            settings_file.write_text(json.dumps({"env": {"KEEP": "me"}}))
            settings_file.chmod(0)

        rc = _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))]
        )

        assert rc != 0
        if kind == "directory":
            assert settings_file.is_dir() and not list(settings_file.iterdir())
        elif kind == "missing-parent":
            assert not settings_file.parent.exists()
        else:
            settings_file.chmod(0o600)
            assert json.loads(settings_file.read_text()) == {"env": {"KEEP": "me"}}
        captured = capsys.readouterr()
        assert "no receiver" not in captured.out + captured.err
        assert str(settings_file) in captured.err


class TestRunIsGivenItsRunner:
    def test_run_has_no_default_library(self, tmp_path, capsys):
        # The command reads no endpoint but the one named, or the default under the home.
        home = tmp_path / "home"
        home.mkdir()
        settings_file = tmp_path / "settings.json"

        rc = _run_main(["--settings-file", str(settings_file)], home=home)

        assert rc == 0
        assert not settings_file.exists()
        assert "no receiver" in capsys.readouterr().out


class TestUserEntryIsGivenItsRunner:
    """The endpoint the entry carries is the one read from the file named, or from the default
    under the home; no process starts and no environment variable decides where to look."""

    def test_it_hands_the_endpoint_file_to_the_runner_and_returns_its_answer(self, tmp_path):
        endpoint_file = tmp_path / "endpoint"
        endpoint_file.write_text("http://localhost:9999\n")
        settings_file = tmp_path / "settings.json"

        assert _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(endpoint_file)]
        ) == 0
        assert json.loads(settings_file.read_text())["env"] == _entry("http://localhost:9999")

    def test_no_endpoint_file_reaches_the_runner_as_none(self, tmp_path):
        home = tmp_path / "home"
        default = home / ".claude" / "tools" / "otel-receiver" / "endpoint"
        default.parent.mkdir(parents=True)
        default.write_text("http://localhost:7777\n")
        settings_file = tmp_path / "settings.json"

        assert _run_main(["--settings-file", str(settings_file)], home=home) == 0
        assert json.loads(settings_file.read_text())["env"] == _entry("http://localhost:7777")

    def test_an_empty_answer_from_the_runner_is_none(self, tmp_path, capsys):
        endpoint_file = tmp_path / "endpoint"
        endpoint_file.write_text("  \n")
        settings_file = tmp_path / "settings.json"

        assert _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(endpoint_file)]
        ) == 0
        assert not settings_file.exists()
        assert "no receiver" in capsys.readouterr().out

    @_NOT_ROOT
    def test_a_failed_answer_raises(self, tmp_path, capsys):
        endpoint_file = _endpoint_file(tmp_path)
        endpoint_file.chmod(0)
        settings_file = tmp_path / "settings.json"

        assert _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(endpoint_file)]
        ) == 0
        assert not settings_file.exists()
        assert "no receiver" in capsys.readouterr().out


class TestNotAnObject:
    """A settings file that parses but is not a JSON object is refused like invalid JSON — `[]`
    would otherwise be replaced by `{"env": …}`."""

    @pytest.mark.parametrize("text", ["[1, 2]\n", "[]\n", '"a string"\n'])
    @pytest.mark.parametrize("remove", [False, True])
    def test_it_writes_nothing_exits_nonzero_and_names_the_file(self, tmp_path, capsys, text, remove):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(text)
        argv = ["--settings-file", str(settings_file), "--endpoint-file", str(_endpoint_file(tmp_path))]
        code = _run_main(argv + (["--remove"] if remove else []))
        assert code != 0
        assert settings_file.read_text() == text
        assert str(settings_file) in capsys.readouterr().err


class TestUserEntryHelper:
    """Through the package command, the entry is `turn_on.telemetry_entry` and nothing rebuilds the
    variable set."""

    def _written(self, tmp_path: Path, endpoint_file: Path):
        settings_file = tmp_path / "settings.json"
        rc = _run_main(
            ["--settings-file", str(settings_file), "--endpoint-file", str(endpoint_file)]
        )
        return rc, settings_file

    def test_given_a_published_endpoint_it_returns_the_five_keys(self, tmp_path):
        rc, settings_file = self._written(tmp_path, _endpoint_file(tmp_path))
        assert rc == 0
        assert json.loads(settings_file.read_text())["env"] == _entry(_ENDPOINT)

    def test_given_no_published_endpoint_it_returns_none(self, tmp_path):
        rc, settings_file = self._written(tmp_path, tmp_path / "does-not-exist")
        assert rc == 0 and not settings_file.exists()

    def test_given_a_malformed_endpoint_file_it_returns_none(self, tmp_path):
        garbage = tmp_path / "garbage"
        garbage.write_text("not a url\n")
        rc, settings_file = self._written(tmp_path, garbage)
        assert rc == 0 and not settings_file.exists()

    def test_a_bash_env_file_in_the_environment_cannot_reach_the_library(
        self, tmp_path, capsys, monkeypatch
    ):
        noisy = tmp_path / "bash_env.sh"
        noisy.write_text('echo "noise from BASH_ENV"\n')
        monkeypatch.setenv("BASH_ENV", str(noisy))
        monkeypatch.setenv("ENV", str(noisy))

        rc, settings_file = self._written(tmp_path, _endpoint_file(tmp_path))

        assert rc == 0
        assert json.loads(settings_file.read_text())["env"] == _entry(_ENDPOINT)
        # The written-keys line, and nothing else: no noise from the shell the environment names.
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1 and all(key in lines[0] for key in _TELEMETRY_KEYS)
        assert "noise" not in lines[0]


def test_module_names_no_port_and_no_user_specific_path():
    source = Path(turn_on.__file__).read_text()
    assert "4318" not in source
    assert "/Users/" not in source


def test_the_settings_file_is_always_named_by_the_caller():
    # The command carries no default settings-file path of its own: with none named, it refuses.
    with pytest.raises(SystemExit):
        turn_on._build_parser().parse_args([])
