"""Tests for `ka_telemetry.turn_on` — the command that writes the five telemetry keys into a
Claude Code settings file, removes them, or prints them for one run.

Every test enters through `turn_on.run` with a runtime built here, through the pure
transformations, or through the parser. The collaborators the command reaches the world through
are real (`turn_on.Files` over a temporary directory) or hand-written fakes handed to the
runtime's constructor; nothing patches the module under test.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from ka_telemetry import stream_usage, turn_on

SETTINGS_FLAG = "--settings-file"
ENDPOINT_FLAG = "--endpoint-file"
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
PROMPT_TEXT_KEYS = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_ASSISTANT_RESPONSES",
)
# Whitespace, a double quote, a comma, a semicolon, a backslash and an equals sign: what an
# OTEL_RESOURCE_ATTRIBUTES pair cannot carry.
FORBIDDEN_CHARACTERS = (" ", "\t", "\n", "\r", "\v", "\f", '"', ",", ";", "\\", "=")


class _Forbidden:
    """A collaborator that fails the test the moment anything touches it."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str):
        raise AssertionError(f"usage was refused, yet {self._name}.{attribute} was touched")

    def __truediv__(self, other):
        raise AssertionError(f"usage was refused, yet {self._name} was touched")

    def __fspath__(self):
        raise AssertionError(f"usage was refused, yet {self._name} was touched")


class FakeProcesses:
    """The process port, answering the git lookup with a canned value and recording each ask."""

    def __init__(self, common_dir: str | None = None) -> None:
        self.common_dir = common_dir
        self.asked: list[Path] = []

    def git_common_dir(self, cwd: Path) -> str | None:
        self.asked.append(cwd)
        return self.common_dir


RUN_ID = "7f3c2a9e-4b1d-4c6a-9e0f-2d5b8a1c3e47"
ENDPOINT = "http://localhost:4318"
TELEMETRY_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")


def _runtime(tmp_path: Path, *, processes=None, home: Path | None = None) -> turn_on.Runtime:
    """A runtime over real files in `tmp_path`, with its own empty home and a fixed run id."""
    home = tmp_path / "home" if home is None else home
    home.mkdir(parents=True, exist_ok=True)
    return turn_on.Runtime(
        files=turn_on.Files(),
        processes=FakeProcesses() if processes is None else processes,
        new_run_id=lambda: RUN_ID,
        home=home,
        cwd=tmp_path,
    )


def _forbidden_new_run_id() -> str:
    raise AssertionError("usage was refused, yet a run id was drawn")


def _untouchable_runtime() -> turn_on.Runtime:
    return turn_on.Runtime(
        files=_Forbidden("files"),
        processes=_Forbidden("processes"),
        new_run_id=_forbidden_new_run_id,
        home=_Forbidden("home"),
        cwd=_Forbidden("cwd"),
    )


def _refused(argv: list[str], capsys) -> str:
    """Runs `argv` against a runtime nothing may touch; checks the refusal and returns stderr."""
    with pytest.raises(SystemExit) as raised:
        turn_on.run(argv, runtime=_untouchable_runtime())
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    return captured.err


# Every refusal of CP-223 and CP-237, with the flag its message must name.
REFUSALS = [
    pytest.param(["--bogus"], "--bogus", id="unknown-flag"),
    pytest.param(["--sett", "s.json"], "--sett", id="abbreviated-flag"),
    pytest.param(["--settings-file"], "--settings-file", id="settings-file-without-value"),
    pytest.param(["--print", "--endpoint-file"], "--endpoint-file", id="endpoint-file-no-value"),
    pytest.param(["--print", "--prefix"], "--prefix", id="prefix-without-value"),
    pytest.param(["--print", "--prefix", "acme", "--label"], "--label", id="label-without-value"),
    pytest.param([], "--print", id="no-mode"),
    pytest.param(["--print", "--settings-file", "s.json"], "--settings-file", id="print-settings"),
    pytest.param(["--print", "--remove"], "--remove", id="print-remove"),
    pytest.param(["--print", "--replace"], "--replace", id="print-replace"),
    pytest.param(["--print", "--force"], "--force", id="print-force"),
    pytest.param(["--settings-file", "s.json", "--prefix", "acme"], "--prefix", id="write-prefix"),
    pytest.param(["--settings-file", "s.json", "--label", "a=1"], "--label", id="write-label"),
    pytest.param(
        ["--settings-file", "s.json", "--remove", "--prefix", "acme"],
        "--prefix",
        id="remove-prefix",
    ),
    pytest.param(["--print", "--label", "a=1"], "--label", id="label-without-prefix"),
    pytest.param(["--print", "--prefix", ""], "--prefix", id="empty-prefix"),
    pytest.param(["--print", "--prefix", "acme."], "--prefix", id="prefix-ends-with-dot"),
    *[
        pytest.param(["--print", "--prefix", key], "--prefix", id=f"prefix-row-key-{key}")
        for key in stream_usage.ROW_KEYS
    ],
    *[
        pytest.param(
            ["--print", "--prefix", f"a{char}b"], "--prefix", id=f"prefix-char-{ord(char)}"
        )
        for char in FORBIDDEN_CHARACTERS
    ],
]


class TestArgumentContract:
    def test_unknown_flag_exits_2_names_the_flag_and_prints_nothing_on_stdout(self, capsys):
        for argv, flag in [
            (["--bogus"], "--bogus"),
            (["--sett", "s.json"], "--sett"),
            (["--settings-file"], "--settings-file"),
            (["--print", "--endpoint-file"], "--endpoint-file"),
            (["--print", "--prefix"], "--prefix"),
            (["--print", "--prefix", "acme", "--label"], "--label"),
        ]:
            assert flag in _refused(argv, capsys), argv

    def test_no_print_and_no_settings_file_exits_2_naming_settings_file_or_print(self, capsys):
        error = _refused([], capsys)
        assert "--settings-file" in error or "--print" in error

    @pytest.mark.parametrize(
        ("argv", "flag"),
        [
            (["--print", "--settings-file", "s.json"], "--settings-file"),
            (["--print", "--remove"], "--remove"),
            (["--print", "--replace"], "--replace"),
            (["--print", "--force"], "--force"),
        ],
        ids=["settings-file", "remove", "replace", "force"],
    )
    def test_print_with_settings_file_remove_replace_or_force_exits_2(self, capsys, argv, flag):
        assert flag in _refused(argv, capsys)

    @pytest.mark.parametrize(
        ("argv", "flag"),
        [
            (["--settings-file", "s.json", "--prefix", "acme"], "--prefix"),
            (["--settings-file", "s.json", "--label", "a=1"], "--label"),
            (["--settings-file", "s.json", "--remove", "--prefix", "acme"], "--prefix"),
            (["--settings-file", "s.json", "--remove", "--label", "a=1"], "--label"),
        ],
        ids=["write-prefix", "write-label", "remove-prefix", "remove-label"],
    )
    def test_prefix_or_label_without_print_exits_2(self, capsys, argv, flag):
        assert flag in _refused(argv, capsys)

    def test_label_without_prefix_exits_2_naming_label_or_prefix(self, capsys):
        error = _refused(["--print", "--label", "a=1"], capsys)
        assert "--label" in error or "--prefix" in error

    @pytest.mark.parametrize(
        "prefix",
        [
            "",
            "acme.",
            *stream_usage.ROW_KEYS,
            *[f"a{char}b" for char in FORBIDDEN_CHARACTERS],
            *[f"{char}" for char in FORBIDDEN_CHARACTERS],
        ],
        ids=lambda prefix: repr(prefix),
    )
    def test_refused_prefix_exits_2_naming_prefix(self, capsys, prefix):
        assert "--prefix" in _refused(["--print", "--prefix", prefix], capsys)

    @pytest.mark.parametrize(
        "prefix", ["acme", "team", "a-b", "a_b", "x.y", "Ünï", "a'b", "1", "a:b", "ACME"]
    )
    def test_every_prefix_turn_on_accepts_is_accepted_by_the_reader(self, prefix):
        parsed = turn_on._build_parser().parse_args(["--print", "--prefix", prefix])
        assert parsed.prefix == prefix
        assert stream_usage.check_prefix(prefix) is None

    def test_force_without_remove_and_replace_with_remove_parse_as_accepted(self):
        parser = turn_on._build_parser()
        forced = parser.parse_args(["--settings-file", "s.json", "--force"])
        replaced = parser.parse_args(["--settings-file", "s.json", "--remove", "--replace"])
        assert forced.mode is turn_on.Mode.WRITE
        assert replaced.mode is turn_on.Mode.REMOVE

    def test_the_mode_is_chosen_by_print_and_remove(self):
        parser = turn_on._build_parser()
        assert parser.parse_args(["--settings-file", "s.json"]).mode is turn_on.Mode.WRITE
        assert parser.parse_args(["--settings-file", "s.json", "--remove"]).mode is (
            turn_on.Mode.REMOVE
        )
        assert parser.parse_args(["--print"]).mode is turn_on.Mode.PRINT

    def test_an_empty_settings_file_value_is_refused_by_the_parser_seam(self, capsys):
        with pytest.raises(SystemExit) as raised:
            turn_on._build_parser().parse_args(["--settings-file", ""])
        assert raised.value.code == 2
        assert "--settings-file" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("argv", "flag"), [p.values for p in REFUSALS], ids=[p.id for p in REFUSALS]
    )
    def test_usage_errors_resolve_before_the_runtime_is_touched(self, capsys, argv, flag):
        assert flag in _refused(argv, capsys)

    def test_a_usage_error_ends_with_the_next_step_naming_the_command_help(self, capsys):
        error = _refused(["--bogus"], capsys)
        next_step = "Run `ka-telemetry turn-on --help` to see what it accepts."
        assert error.rstrip("\n").endswith(next_step)


class TestHelp:
    def test_help_exits_0_and_lists_the_eight_flags(self, capsys):
        with pytest.raises(SystemExit) as raised:
            turn_on.run(["--help"], runtime=_untouchable_runtime())
        captured = capsys.readouterr()
        assert raised.value.code == 0
        assert captured.err == ""
        for flag in EIGHT_FLAGS:
            assert flag in captured.out

    def test_help_names_none_of_the_three_prompt_text_keys(self, capsys):
        with pytest.raises(SystemExit):
            turn_on.run(["--help"], runtime=_untouchable_runtime())
        output = capsys.readouterr().out
        for key in PROMPT_TEXT_KEYS:
            assert key not in output


class TestPublishedEndpoint:
    @staticmethod
    def _resolve(tmp_path, content, *, name="ep"):
        (tmp_path / name).write_bytes(content if isinstance(content, bytes) else content.encode())
        return turn_on.resolve_endpoint(str(tmp_path / name), _runtime(tmp_path))

    @pytest.mark.parametrize("scheme", ["http", "https"])
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.1.2.3", "[::1]"])
    @pytest.mark.parametrize("port", ["", ":4318", ":80"])
    @pytest.mark.parametrize("path", ["", "/", "/v1/logs"])
    def test_loopback_endpoints_are_published(self, tmp_path, scheme, host, port, path):
        value = f"{scheme}://{host}{port}{path}"
        endpoint = self._resolve(tmp_path, value + "\n")
        assert endpoint.outcome is turn_on.EndpointOutcome.PUBLISHED
        assert endpoint.value == value

    def test_the_value_is_the_first_line_with_all_whitespace_removed(self, tmp_path):
        endpoint = self._resolve(
            tmp_path, "  http://local\thost:43 18  \nhttp://example.com:1\nsecond\n"
        )
        assert endpoint.outcome is turn_on.EndpointOutcome.PUBLISHED
        assert endpoint.value == "http://localhost:4318"

    def test_a_trailing_slash_is_kept(self, tmp_path):
        assert self._resolve(tmp_path, "http://127.0.0.1:4318/\n").value == "http://127.0.0.1:4318/"

    @pytest.mark.parametrize(
        "value",
        [
            "http://localhost.example.com:4318",
            "http://127.0.0.1.example.com",
            "http://example.com:4318",
        ],
    )
    def test_other_hosts_are_off_machine(self, tmp_path, value):
        endpoint = self._resolve(tmp_path, value + "\n")
        assert endpoint.outcome is turn_on.EndpointOutcome.OFF_MACHINE
        assert endpoint.value is None

    @pytest.mark.parametrize(
        "value", ["http://localhost@example.com", "http://127.0.0.1:80@example.com"]
    )
    def test_an_authority_with_userinfo_is_not_published(self, tmp_path, value):
        endpoint = self._resolve(tmp_path, value + "\n")
        assert endpoint.outcome is turn_on.EndpointOutcome.OFF_MACHINE

    @pytest.mark.parametrize(
        "content",
        ["ftp://localhost\n", "localhost:4318\n", "", "   \t \nhttp://localhost:4318\n"],
        ids=["ftp", "no-scheme", "empty-file", "whitespace-first-line"],
    )
    def test_a_non_http_scheme_or_no_scheme_is_malformed(self, tmp_path, content):
        assert self._resolve(tmp_path, content).outcome is turn_on.EndpointOutcome.MALFORMED

    def test_a_missing_file_is_absent(self, tmp_path):
        endpoint = turn_on.resolve_endpoint(str(tmp_path / "nowhere"), _runtime(tmp_path))
        assert endpoint.outcome is turn_on.EndpointOutcome.ABSENT

    @NOT_ROOT
    def test_an_unreadable_file_is_absent(self, tmp_path):
        (tmp_path / "ep").write_text(ENDPOINT + "\n")
        (tmp_path / "ep").chmod(0)
        endpoint = turn_on.resolve_endpoint(str(tmp_path / "ep"), _runtime(tmp_path))
        assert endpoint.outcome is turn_on.EndpointOutcome.ABSENT

    def test_a_directory_is_malformed_not_absent(self, tmp_path):
        (tmp_path / "ep").mkdir()
        endpoint = turn_on.resolve_endpoint(str(tmp_path / "ep"), _runtime(tmp_path))
        assert endpoint.outcome is turn_on.EndpointOutcome.MALFORMED

    def test_the_path_text_is_carried_as_typed(self, tmp_path):
        endpoint = turn_on.resolve_endpoint("./sub//ep", _runtime(tmp_path))
        assert endpoint.path_text == "./sub//ep"
        assert endpoint.given is True

    def test_without_an_endpoint_file_the_default_beneath_home_is_read(self, tmp_path):
        home = tmp_path / "home"
        default = home / ".claude" / "tools" / "otel-receiver" / "endpoint"
        default.parent.mkdir(parents=True)
        default.write_text(ENDPOINT + "\n")
        endpoint = turn_on.resolve_endpoint(None, _runtime(tmp_path, home=home))
        assert endpoint.outcome is turn_on.EndpointOutcome.PUBLISHED
        assert endpoint.value == ENDPOINT
        assert endpoint.path_text == str(default)
        assert endpoint.given is False

    def test_with_an_endpoint_file_only_that_file_is_read(self, tmp_path):
        home = tmp_path / "home"
        default = home / ".claude" / "tools" / "otel-receiver" / "endpoint"
        default.parent.mkdir(parents=True)
        default.write_text("http://localhost:1111\n")
        (tmp_path / "ep").write_text("http://localhost:2222\n")
        endpoint = turn_on.resolve_endpoint(str(tmp_path / "ep"), _runtime(tmp_path, home=home))
        assert endpoint.value == "http://localhost:2222"

    def test_no_environment_variable_changes_where_it_looks(self, tmp_path, monkeypatch):
        misleading = tmp_path / "misleading"
        misleading.mkdir()
        (misleading / "elsewhere").write_text("http://localhost:3333\n")
        monkeypatch.setenv("INSTRUMENT_RUN_ENDPOINT_FILE", str(misleading / "elsewhere"))
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:3333")
        monkeypatch.setenv("HOME", str(misleading))
        endpoint = turn_on.resolve_endpoint(None, _runtime(tmp_path))
        assert endpoint.outcome is turn_on.EndpointOutcome.ABSENT
        assert endpoint.path_text == str(
            tmp_path / "home" / ".claude" / "tools" / "otel-receiver" / "endpoint"
        )


class TestTelemetryEntry:
    def test_the_five_keys_come_in_order_with_their_values(self):
        entry = turn_on.telemetry_entry("http://localhost:9")
        assert list(entry.items()) == [
            ("CLAUDE_CODE_ENABLE_TELEMETRY", "1"),
            ("OTEL_METRICS_EXPORTER", "otlp"),
            ("OTEL_LOGS_EXPORTER", "otlp"),
            ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:9"),
        ]


def _endpoint_file(tmp_path: Path, content: str = ENDPOINT + "\n", name: str = "ep") -> str:
    (tmp_path / name).write_text(content)
    return str(tmp_path / name)


def _settings_file(tmp_path: Path, content: str | bytes, name: str = "settings.json") -> str:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return str(path)


def _invoke(argv: list[str], tmp_path: Path, capsys, *, runtime=None) -> tuple[int, str, str]:
    """Runs the command in process; returns its status, stdout and stderr."""
    status = turn_on.run(argv, runtime=_runtime(tmp_path) if runtime is None else runtime)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _write_argv(settings: str, endpoint: str, *extra: str) -> list[str]:
    return ["--settings-file", settings, "--endpoint-file", endpoint, *extra]


def _run_write(settings: str, tmp_path: Path, capsys, *extra: str) -> tuple[int, str, str]:
    """A write with an endpoint file that publishes `ENDPOINT`."""
    return _invoke(_write_argv(settings, _endpoint_file(tmp_path), *extra), tmp_path, capsys)


def _file_json(path: str) -> dict:
    return json.loads(Path(path).read_text())


RECORD_SLOT = "_ka_telemetry_turn_on"
RECORD_FORMAT = "ka-telemetry.turn-on/1"


def _slot(number: int) -> str:
    """The record's top-level key: the preferred slot, or the collision slot `number`."""
    return RECORD_SLOT if number == 0 else f"{RECORD_SLOT}_{number}"


def _record(written: dict[str, str]) -> dict:
    return {"format": RECORD_FORMAT, "written": written}


def _written_by_a_write(endpoint: str = ENDPOINT, keys=TELEMETRY_KEYS) -> dict[str, str]:
    """What a write puts in the record: the keys it wrote, with the value it gave each."""
    entry = turn_on.telemetry_entry(endpoint)
    return {key: entry[key] for key in keys}


def _named(line: str) -> list[str]:
    """The telemetry keys a status line names, in the order the line first names them."""
    return sorted((key for key in TELEMETRY_KEYS if key in line), key=line.index)


def _in_five_key_order(keys) -> list[str]:
    return [key for key in TELEMETRY_KEYS if key in keys]


NO_RECEIVER = "no published telemetry endpoint — machine has no receiver, nothing written"


@pytest.mark.parametrize("flags", [[], ["--remove"], ["--remove", "--force"]],
                         ids=["write", "remove", "force-remove"])
@pytest.mark.parametrize("published", [False, True], ids=["no-endpoint", "published"])
def test_a_missing_settings_parent_is_refused_before_any_no_op(
    tmp_path, capsys, flags, published
):
    settings = tmp_path / "missing-parent" / "s.json"
    endpoint = _endpoint_file(tmp_path) if published else str(tmp_path / "no-endpoint")
    status, out, err = _invoke(
        _write_argv(str(settings), endpoint, *flags), tmp_path, capsys
    )
    assert status == 1
    assert out == ""
    assert str(settings) in err
    assert "receiver" not in err
    assert "Traceback" not in err
    assert not settings.parent.exists()


@NOT_ROOT
@pytest.mark.parametrize("flags", [[], ["--remove"]], ids=["write", "remove"])
def test_failed_publication_preserves_symlink_target_bytes_mode_and_directory(
    tmp_path, capsys, flags
):
    directory = tmp_path / "locked"
    directory.mkdir()
    target = directory / "settings.json"
    original = b'{"env":{"CLAUDE_CODE_ENABLE_TELEMETRY":"1","KEEP":"me"}}'
    target.write_bytes(original)
    target.chmod(0o640)
    link = tmp_path / "settings-link.json"
    link.symlink_to(target)
    directory.chmod(0o555)
    try:
        status, out, err = _invoke(
            _write_argv(str(link), _endpoint_file(tmp_path), *flags), tmp_path, capsys
        )
        assert status == 1
        assert out == ""
        assert str(link) in err
        assert link.is_symlink()
        assert link.readlink() == target
        assert target.read_bytes() == original
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert list(directory.iterdir()) == [target]
    finally:
        directory.chmod(0o755)


class TestWriteMode:
    def test_write_into_an_absent_settings_file_creates_it_with_the_five_keys(
        self, tmp_path, capsys
    ):
        settings = str(tmp_path / "s.json")
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        assert _file_json(settings) == {
            "env": turn_on.telemetry_entry(ENDPOINT),
            RECORD_SLOT: _record(_written_by_a_write()),
        }

    def test_write_keeps_every_other_key_and_its_place(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"café"},"x":1}')
        status, _, _ = _run_write(settings, tmp_path, capsys)
        assert status == 0
        expected_env = {"KEEP": "café", **turn_on.telemetry_entry(ENDPOINT)}
        expected = {"env": expected_env, "x": 1, RECORD_SLOT: _record(_written_by_a_write())}
        assert Path(settings).read_text() == (
            json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
        )
        assert list(_file_json(settings)) == ["env", "x", RECORD_SLOT]
        assert list(_file_json(settings)["env"])[0] == "KEEP"

    @pytest.mark.parametrize("key", TELEMETRY_KEYS)
    def test_a_key_already_present_with_the_same_value_keeps_its_place(
        self, tmp_path, capsys, key
    ):
        entry = turn_on.telemetry_entry(ENDPOINT)
        settings = _settings_file(
            tmp_path, json.dumps({"env": {"BEFORE": "1", key: entry[key], "AFTER": "2"}})
        )
        _run_write(settings, tmp_path, capsys)
        others = [other for other in TELEMETRY_KEYS if other != key]
        assert list(_file_json(settings)["env"]) == ["BEFORE", key, "AFTER", *others]

    @pytest.mark.parametrize(
        "env",
        [None, False, 0, "", []],
        ids=["null", "false", "zero", "empty-string", "empty-list"],
    )
    def test_a_missing_env_is_created(self, tmp_path, capsys, env):
        settings = _settings_file(tmp_path, json.dumps({"env": env, "other": 1}))
        status, _, _ = _run_write(settings, tmp_path, capsys)
        assert status == 0
        assert _file_json(settings) == {
            "env": turn_on.telemetry_entry(ENDPOINT),
            "other": 1,
            RECORD_SLOT: _record(_written_by_a_write()),
        }

    def test_a_second_write_leaves_the_file_byte_identical(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        argv = _write_argv(settings, _endpoint_file(tmp_path))
        _invoke(argv, tmp_path, capsys)
        first = Path(settings).read_bytes()
        _invoke(argv, tmp_path, capsys)
        assert Path(settings).read_bytes() == first

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (None, "absent"),
            ("not a url\n", "malformed"),
            ("http://example.com:4318\n", "off machine"),
        ],
    )
    @pytest.mark.parametrize("given", [True, False])
    def test_no_published_endpoint_writes_nothing_and_creates_no_file(
        self, tmp_path, capsys, monkeypatch, content, reason, given
    ):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        endpoint_path = "./sub//ep" if given else str(home / ".claude/tools/otel-receiver/endpoint")
        if content is not None:
            Path(endpoint_path).parent.mkdir(parents=True, exist_ok=True)
            Path(endpoint_path).write_text(content)
        argv = ["--settings-file", "s.json"] + (["--endpoint-file", endpoint_path] if given else [])
        status, out, err = _invoke(argv, tmp_path, capsys, runtime=_runtime(tmp_path, home=home))
        expected = (
            f"no published telemetry endpoint at {endpoint_path} — machine has no receiver, "
            "nothing written"
            if given
            else NO_RECEIVER
        )
        assert (status, out, err) == (0, expected + "\n", "")
        assert not (tmp_path / "s.json").exists()

    def test_no_published_endpoint_leaves_an_existing_settings_file_untouched(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        before = Path(settings).read_bytes()
        _invoke(_write_argv(settings, str(tmp_path / "nowhere")), tmp_path, capsys)
        assert Path(settings).read_bytes() == before

    def test_a_user_value_on_a_key_is_kept_and_named(self, tmp_path, capsys):
        settings = _settings_file(
            tmp_path, '{"env":{"OTEL_EXPORTER_OTLP_ENDPOINT":"https://c.example"}}'
        )
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert _file_json(settings)["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://c.example"
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in out.splitlines()[-1]
        assert "--replace" in out
        assert "c.example" not in out

    def test_several_user_values_are_named_in_canonical_order_with_the_others_written(
        self, tmp_path, capsys
    ):
        settings = _settings_file(
            tmp_path,
            json.dumps(
                {"env": {"OTEL_LOGS_EXPORTER": "none", "CLAUDE_CODE_ENABLE_TELEMETRY": "0"}}
            ),
        )
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        written_line, left_line = out.splitlines()
        assert _named(written_line) == [
            "OTEL_METRICS_EXPORTER",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        ]
        assert _named(left_line) == ["CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_LOGS_EXPORTER"]
        env = _file_json(settings)["env"]
        assert (env["OTEL_LOGS_EXPORTER"], env["CLAUDE_CODE_ENABLE_TELEMETRY"]) == ("none", "0")
        assert "none" not in out

    def test_replace_overwrites_the_users_value_and_keeps_the_rest(self, tmp_path, capsys):
        settings = _settings_file(
            tmp_path,
            '{"env":{"OTEL_EXPORTER_OTLP_ENDPOINT":"https://c.example","KEEP":"me"},"x":1}',
        )
        status, out, err = _run_write(settings, tmp_path, capsys, "--replace")
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        written = _file_json(settings)
        assert written["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == ENDPOINT
        assert written["env"]["KEEP"] == "me"
        assert written["x"] == 1

    def test_a_user_value_equal_to_the_turn_ons_is_not_a_conflict(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, json.dumps({"env": {"OTEL_METRICS_EXPORTER": "otlp"}}))
        status, _, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")

    def test_invalid_json_is_refused_with_pythons_own_reason(self, tmp_path, capsys):
        text = "{\n  nope"
        with pytest.raises(json.JSONDecodeError) as decoder:
            json.loads(text)
        settings = _settings_file(tmp_path, text)
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err == f"error: {settings} is not valid JSON ({decoder.value}); nothing written\n"
        assert Path(settings).read_text() == text

    def test_bytes_that_are_not_utf8_are_refused_with_pythons_own_reason(self, tmp_path, capsys):
        raw = b'{"env": {"A": "\xff"}}'
        with pytest.raises(UnicodeDecodeError) as decoder:
            raw.decode("utf-8")
        settings = _settings_file(tmp_path, raw)
        status, _, err = _run_write(settings, tmp_path, capsys)
        assert status == 1
        assert err == f"error: {settings} is not valid JSON ({decoder.value}); nothing written\n"
        assert Path(settings).read_bytes() == raw

    @pytest.mark.parametrize(
        ("text", "line"),
        [
            ("[1, 2]", "is not a JSON object"),
            ('"x"', "is not a JSON object"),
            ('{"env": [1]}', "has an env that is not a JSON object"),
            ('{"env": "x"}', "has an env that is not a JSON object"),
        ],
    )
    def test_non_object_json_and_a_non_object_env_are_refused_with_their_lines(
        self, tmp_path, capsys, text, line
    ):
        settings = _settings_file(tmp_path, text)
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err == f"error: {settings} {line}; nothing written\n"
        assert Path(settings).read_text() == text

    def test_a_refused_settings_file_says_nothing_about_a_receiver(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, "nope")
        _, out, err = _run_write(settings, tmp_path, capsys)
        assert "receiver" not in out + err

    def test_a_settings_error_wins_over_a_missing_endpoint(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, "nope")
        argv = _write_argv(settings, str(tmp_path / "nowhere"))
        status, out, err = _invoke(argv, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert "is not valid JSON" in err

    def test_a_directory_as_the_settings_path_names_the_path_and_creates_nothing(
        self, tmp_path, capsys
    ):
        directory = tmp_path / "sdir"
        directory.mkdir()
        status, out, err = _invoke(
            _write_argv(str(directory), _endpoint_file(tmp_path)), tmp_path, capsys
        )
        assert status != 0
        assert out == ""
        assert str(directory) in err
        assert "receiver" not in err
        assert list(directory.iterdir()) == []

    def test_a_settings_path_whose_parent_does_not_exist_names_the_path_and_creates_nothing(
        self, tmp_path, capsys
    ):
        settings = str(tmp_path / "nodir" / "s.json")
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert status != 0
        assert out == ""
        assert settings in err
        assert "receiver" not in err
        assert "Traceback" not in err
        assert not (tmp_path / "nodir").exists()

    @NOT_ROOT
    def test_a_settings_file_whose_mode_forbids_writing_is_left_unchanged_and_fails_with_its_path(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        Path(settings).chmod(0o444)
        before = Path(settings).read_bytes()
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert Path(settings).read_bytes() == before
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["ep", "home", "settings.json"]

    @NOT_ROOT
    def test_an_unreadable_settings_file_fails_with_its_path_and_is_left_alone(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        Path(settings).chmod(0)
        status, _, err = _run_write(settings, tmp_path, capsys)
        assert status == 1
        assert err.startswith(f"error: cannot read {settings} (")
        assert "receiver" not in err

    def test_a_failed_publish_leaves_no_temporary_file(self, tmp_path):
        target = tmp_path / "is-a-directory"
        target.mkdir()
        with pytest.raises(OSError):
            turn_on.Files().write_bytes(str(target), b"{}\n")
        assert [entry.name for entry in tmp_path.iterdir()] == ["is-a-directory"]

    def test_a_publish_keeps_the_original_mode(self, tmp_path):
        target = tmp_path / "s.json"
        target.write_text("{}")
        target.chmod(0o600)
        turn_on.Files().write_bytes(str(target), b'{"a": 1}\n')
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_bytes() == b'{"a": 1}\n'

    def test_a_publish_through_a_symlink_writes_the_target_and_keeps_the_link(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        target = real / "settings.json"
        target.write_text("{}")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        turn_on.Files().write_bytes(str(link), b'{"a": 1}\n')
        assert link.is_symlink()
        assert os.path.realpath(link) == os.path.realpath(target)
        assert target.read_bytes() == b'{"a": 1}\n'

    def test_a_new_settings_file_gets_the_usual_mode_for_a_new_file(self, tmp_path):
        target = tmp_path / "new.json"
        turn_on.Files().write_bytes(str(target), b"{}\n")
        control = tmp_path / "control"
        control.write_text("")
        assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(control.stat().st_mode)

    def test_force_without_remove_is_accepted_and_changes_nothing(self, tmp_path, capsys):
        endpoint = _endpoint_file(tmp_path)
        plain = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}', "plain.json")
        forced = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}', "forced.json")
        plain_result = _invoke(_write_argv(plain, endpoint), tmp_path, capsys)
        forced_result = _invoke(_write_argv(forced, endpoint, "--force"), tmp_path, capsys)
        assert plain_result[0] == forced_result[0]
        assert plain_result[1:] == forced_result[1:]
        assert Path(plain).read_bytes() == Path(forced).read_bytes()

    def test_the_three_prompt_text_keys_already_in_the_file_are_untouched_by_a_write(
        self, tmp_path, capsys
    ):
        before = {key: f"value-of-{key}" for key in PROMPT_TEXT_KEYS}
        settings = _settings_file(tmp_path, json.dumps({"env": before}))
        _run_write(settings, tmp_path, capsys, "--replace")
        written = _file_json(settings)["env"]
        assert {key: written[key] for key in PROMPT_TEXT_KEYS} == before
        assert set(written) == set(PROMPT_TEXT_KEYS) | set(TELEMETRY_KEYS)


class _FilesThatFailToPublish(turn_on.Files):
    """The filesystem port with a publication that fails, counting the attempts."""

    def __init__(self) -> None:
        self.attempts = 0

    def write_bytes(self, path: str, content: bytes) -> None:
        self.attempts += 1
        raise PermissionError(13, "Permission denied")


class _CountingFiles(turn_on.Files):
    """The real filesystem port, counting its publications."""

    def __init__(self) -> None:
        self.published = 0

    def write_bytes(self, path: str, content: bytes) -> None:
        self.published += 1
        super().write_bytes(path, content)


def _write_with(files, settings: str, tmp_path: Path, capsys, *extra: str):
    runtime = dataclasses.replace(_runtime(tmp_path), files=files)
    return _invoke(
        _write_argv(settings, _endpoint_file(tmp_path), *extra), tmp_path, capsys, runtime=runtime
    )


def _env_json(**env: str) -> str:
    return json.dumps({"env": env})


class TestWriteDecisionsAndRecord:
    """CP-273, CP-274, CP-278 to CP-280: a write writes the keys the user had not set, records
    what it wrote outside `env`, and says which keys it wrote and which it left. The tests assert
    which keys each line names, in which order, that no value is printed and what the file holds;
    the words of the lines are the content stage's."""

    @NOT_ROOT
    @pytest.mark.parametrize("equal", [True, False], ids=["equal-values", "different-values"])
    def test_a_zero_write_still_refuses_an_unusable_settings_file(self, tmp_path, capsys, equal):
        """CP-279 preserves unusable-file refusals even when CP-278 leaves all five keys."""
        directory = tmp_path / "locked"
        directory.mkdir()
        target = directory / "settings.json"
        values = _written_by_a_write() if equal else {key: "mine" for key in TELEMETRY_KEYS}
        original = json.dumps({"env": values}).encode()
        target.write_bytes(original)
        target.chmod(0o640)
        link = tmp_path / "settings-link.json"
        link.symlink_to(target)
        directory.chmod(0o555)
        try:
            status, out, err = _run_write(str(link), tmp_path, capsys)
            assert (status, out) == (1, "")
            assert err.startswith(f"error: cannot write {link} (")
            assert target.read_bytes() == original
            assert link.is_symlink() and link.readlink() == target
            assert stat.S_IMODE(target.stat().st_mode) == 0o640
            assert list(directory.iterdir()) == [target]
        finally:
            directory.chmod(0o755)

    def test_a_zero_write_uses_the_files_port_to_check_usability(self, tmp_path, capsys):
        """CP-279's refusal uses the existing filesystem boundary without publishing."""
        settings = _settings_file(tmp_path, _env_json(**_written_by_a_write()))
        before = Path(settings).read_bytes()
        files = _FilesThatCannotReplace()
        status, out, err = _write_with(files, settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert (files.checked, files.published) == (1, 0)
        assert Path(settings).read_bytes() == before

    def test_a_write_without_an_endpoint_does_not_check_publication(self, tmp_path, capsys):
        """CP-279 leaves the no-endpoint success path unchanged."""
        settings = _settings_file(tmp_path, _env_json(**_written_by_a_write()))
        before = Path(settings).read_bytes()
        files = _FilesThatCannotReplace()
        status, out, err = _invoke(
            _write_argv(settings, str(tmp_path / "absent-endpoint")),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, err) == (0, "")
        assert "no published telemetry endpoint" in out
        assert (files.checked, files.published) == (0, 0)
        assert Path(settings).read_bytes() == before

    @pytest.mark.parametrize("mode", ["write", "remove"])
    def test_a_long_slot_name_holding_user_data_is_preserved(self, tmp_path, capsys, mode):
        """CP-273/CP-278: an unrelated top-level value cannot prevent writing or removal."""
        slot = RECORD_SLOT + "_" + "9" * 5000
        user_data = {slot: {"mine": True}, "env": {"OTEL_METRICS_EXPORTER": "otlp"}}
        settings = _settings_file(tmp_path, json.dumps(user_data))
        if mode == "write":
            status, out, err = _run_write(settings, tmp_path, capsys)
            assert (status, err) == (0, "")
            assert _file_json(settings)[slot] == user_data[slot]
            assert _file_json(settings)["env"] == _written_by_a_write()
        else:
            before = Path(settings).read_bytes()
            status, out, err = _run_remove(settings, tmp_path, capsys)
            assert (status, err) == (0, "")
            assert _named(out) == ["OTEL_METRICS_EXPORTER"]
            assert Path(settings).read_bytes() == before

    def test_large_record_slot_numbers_still_consolidate_in_numeric_order(self, tmp_path, capsys):
        """Task 4: lowest-numbered recognized slot wins, with no size limit in the contract."""
        smaller = RECORD_SLOT + "_" + "9" * 5000
        larger = RECORD_SLOT + "_1" + "0" * 5000
        settings = _settings_file(
            tmp_path,
            json.dumps({
                larger: _record({"OTEL_LOGS_EXPORTER": "otlp"}),
                smaller: _record({"OTEL_METRICS_EXPORTER": "otlp"}),
                "env": {"OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp"},
            }),
        )
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        written = _file_json(settings)
        assert larger not in written and RECORD_SLOT not in written
        assert written[smaller] == _record(_written_by_a_write())
        assert _run_remove(settings, tmp_path, capsys) == (0, "", "")
        assert _file_json(settings) == {}

    def test_check_3_a_clean_write_into_an_absent_file_names_all_five_as_written(
        self, tmp_path, capsys
    ):
        """Station check 3 (CP-284): a clean write names all five keys as written."""
        settings = str(tmp_path / "s.json")
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        lines = out.splitlines()
        assert [_named(line) for line in lines] == [list(TELEMETRY_KEYS)]
        assert "nothing written" not in out
        assert ENDPOINT not in out and "otlp" not in out

    @pytest.mark.parametrize("key", TELEMETRY_KEYS)
    def test_check_3_a_user_value_on_one_key_is_left_and_the_other_four_are_written(
        self, tmp_path, capsys, key
    ):
        """Station check 3 and CP-280: the user's different value on any one of the five keys."""
        settings = _settings_file(tmp_path, _env_json(**{key: "https://c.example"}))
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        others = [other for other in TELEMETRY_KEYS if other != key]
        written_line, left_line = out.splitlines()
        assert _named(written_line) == others
        assert _named(left_line) == [key]
        assert "--replace" in out
        assert "c.example" not in out
        env = _file_json(settings)["env"]
        assert env[key] == "https://c.example"
        assert {other: env[other] for other in others} == {
            other: turn_on.telemetry_entry(ENDPOINT)[other] for other in others
        }
        assert _file_json(settings)[RECORD_SLOT] == _record(_written_by_a_write(keys=others))

    def test_several_user_values_are_all_left_named_in_five_key_order(self, tmp_path, capsys):
        different = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example",
            "OTEL_LOGS_EXPORTER": "none",
        }
        settings = _settings_file(tmp_path, _env_json(**different))
        _, out, _ = _run_write(settings, tmp_path, capsys)
        written_line, left_line = out.splitlines()
        assert _named(written_line) == [
            "CLAUDE_CODE_ENABLE_TELEMETRY",
            "OTEL_METRICS_EXPORTER",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
        ]
        assert _named(left_line) == ["OTEL_LOGS_EXPORTER", "OTEL_EXPORTER_OTLP_ENDPOINT"]
        assert "--replace" in out

    def test_five_different_user_values_write_nothing_and_keep_no_record(self, tmp_path, capsys):
        settings = _settings_file(
            tmp_path, _env_json(**{key: f"mine-{key}" for key in TELEMETRY_KEYS})
        )
        before = Path(settings).read_bytes()
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        assert "--replace" in out
        assert "mine-" not in out
        assert Path(settings).read_bytes() == before

    def test_all_five_equal_user_values_leave_the_bytes_identical_and_keep_no_record(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, _env_json(**turn_on.telemetry_entry(ENDPOINT)))
        before = Path(settings).read_bytes()
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        assert "--replace" not in out
        assert Path(settings).read_bytes() == before

    def test_a_mixed_fixture_decides_each_key_independently(self, tmp_path, capsys):
        # Absent: CLAUDE_CODE_ENABLE_TELEMETRY. The user's, equal: OTEL_METRICS_EXPORTER. The
        # user's, different: OTEL_LOGS_EXPORTER. The turn-on's: the protocol and the endpoint.
        old = "http://localhost:1111"
        settings = _settings_file(
            tmp_path,
            json.dumps(
                {
                    "env": {
                        "OTEL_METRICS_EXPORTER": "otlp",
                        "OTEL_LOGS_EXPORTER": "none",
                        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                        "OTEL_EXPORTER_OTLP_ENDPOINT": old,
                    },
                    RECORD_SLOT: _record(
                        {
                            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                            "OTEL_EXPORTER_OTLP_ENDPOINT": old,
                        }
                    ),
                }
            ),
        )
        status, out, err = _run_write(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        written_line, left_line = out.splitlines()
        assert _named(written_line) == [
            "CLAUDE_CODE_ENABLE_TELEMETRY",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
        ]
        assert _named(left_line) == ["OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER"]
        written = _file_json(settings)
        assert written["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == ENDPOINT
        assert written["env"]["OTEL_LOGS_EXPORTER"] == "none"
        assert written[RECORD_SLOT] == _record(
            _written_by_a_write(
                keys=[
                    "CLAUDE_CODE_ENABLE_TELEMETRY",
                    "OTEL_EXPORTER_OTLP_PROTOCOL",
                    "OTEL_EXPORTER_OTLP_ENDPOINT",
                ]
            )
        )

    @pytest.mark.parametrize(
        "content",
        [None, "{}", '{"env": null}', '{"env": {}}'],
        ids=["absent-file", "absent-env", "null-env", "empty-env"],
    )
    def test_the_record_is_a_top_level_key_not_under_env_and_env_holds_only_the_five(
        self, tmp_path, capsys, content
    ):
        settings = (
            str(tmp_path / "s.json") if content is None else _settings_file(tmp_path, content)
        )
        _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        assert set(written["env"]) == set(TELEMETRY_KEYS)
        assert list(written)[-1] == RECORD_SLOT
        record = written[RECORD_SLOT]
        assert set(record) == {"format", "written"}
        assert record["format"] == RECORD_FORMAT
        assert not set(PROMPT_TEXT_KEYS) & set(record["written"])
        assert not any(key.startswith(RECORD_SLOT) for key in written["env"])

    def test_the_record_names_exactly_the_keys_this_invocation_wrote_with_their_values(
        self, tmp_path, capsys
    ):
        settings = _settings_file(
            tmp_path,
            _env_json(OTEL_LOGS_EXPORTER="none", OTEL_METRICS_EXPORTER="otlp"),
        )
        _run_write(settings, tmp_path, capsys, "--replace")
        # The overwrite is recorded; the equal user value is never recorded, even with --replace.
        assert _file_json(settings)[RECORD_SLOT] == _record(
            _written_by_a_write(
                keys=[key for key in TELEMETRY_KEYS if key != "OTEL_METRICS_EXPORTER"]
            )
        )

    @pytest.mark.parametrize(
        "occupant",
        ["a user's text", 7, {"theirs": True}, {}, {"format": "other/1", "written": {"A": "1"}}],
        ids=["scalar", "number", "other-object", "empty-object", "invalid-record"],
    )
    def test_a_slot_collision_never_overwrites_a_user_value(self, tmp_path, capsys, occupant):
        settings = _settings_file(tmp_path, json.dumps({RECORD_SLOT: occupant}))
        _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        assert written[RECORD_SLOT] == occupant
        assert written[_slot(1)] == _record(_written_by_a_write())
        assert list(written) == [RECORD_SLOT, "env", _slot(1)]

    def test_a_slot_collision_moves_on_to_the_next_unoccupied_slot_in_numeric_order(
        self, tmp_path, capsys
    ):
        occupied = {RECORD_SLOT: "a", _slot(1): "b", _slot(2): "c", _slot(10): "d"}
        settings = _settings_file(tmp_path, json.dumps(occupied))
        _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        assert {slot: written[slot] for slot in occupied} == occupied
        assert written[_slot(3)] == _record(_written_by_a_write())

    def test_several_recognized_records_consolidate_into_the_lowest_slot_on_a_write(
        self, tmp_path, capsys
    ):
        entry = turn_on.telemetry_entry(ENDPOINT)
        settings = _settings_file(
            tmp_path,
            json.dumps(
                {
                    _slot(10): _record({"OTEL_LOGS_EXPORTER": "otlp"}),
                    _slot(2): _record({"OTEL_METRICS_EXPORTER": "otlp"}),
                    _slot(3): {"theirs": True},
                    "env": {"OTEL_LOGS_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp"},
                }
            ),
        )
        _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        assert written[_slot(2)] == _record(_written_by_a_write())
        assert _slot(10) not in written and RECORD_SLOT not in written
        assert written[_slot(3)] == {"theirs": True}
        assert written["env"] == entry

    @pytest.mark.parametrize(
        "unrecognized",
        [
            {"format": "ka-telemetry.turn-on/2", "written": {"OTEL_LOGS_EXPORTER": "otlp"}},
            {"format": RECORD_FORMAT, "written": {"OTEL_LOGS_EXPORTER": "otlp"}, "extra": 1},
            {"format": RECORD_FORMAT, "written": {"OTEL_LOG_USER_PROMPTS": "x"}},
            {"format": RECORD_FORMAT, "written": {"OTEL_LOGS_EXPORTER": 1}},
            {"format": RECORD_FORMAT, "written": {}},
            {"format": RECORD_FORMAT},
            "OTEL_LOGS_EXPORTER",
        ],
        ids=[
            "wrong-version",
            "unknown-field",
            "unknown-key",
            "non-string",
            "empty-written",
            "no-written",
            "scalar",
        ],
    )
    def test_a_record_of_any_other_shape_confers_no_ownership_and_is_never_deleted(
        self, tmp_path, capsys, unrecognized
    ):
        settings = _settings_file(
            tmp_path,
            json.dumps({RECORD_SLOT: unrecognized, "env": {"OTEL_LOGS_EXPORTER": "otlp"}}),
        )
        _, out, _ = _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        assert written[RECORD_SLOT] == unrecognized
        # The key it names is the user's value, equal to the turn-on's: left, not recorded.
        assert "OTEL_LOGS_EXPORTER" in out.splitlines()[-1]
        assert "OTEL_LOGS_EXPORTER" not in written[_slot(1)]["written"]

    def test_a_key_the_turn_on_wrote_is_rewritten_with_todays_value_when_the_endpoint_changed(
        self, tmp_path, capsys
    ):
        settings = str(tmp_path / "s.json")
        _run_write(settings, tmp_path, capsys)
        endpoint = _endpoint_file(tmp_path, "http://localhost:9999\n")
        status, out, err = _invoke(_write_argv(settings, endpoint), tmp_path, capsys)
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        written = _file_json(settings)
        assert written["env"] == turn_on.telemetry_entry("http://localhost:9999")
        assert written[RECORD_SLOT] == _record(_written_by_a_write("http://localhost:9999"))

    def test_a_key_the_user_edited_after_the_turn_on_wrote_it_is_left_without_replace(
        self, tmp_path, capsys
    ):
        settings = str(tmp_path / "s.json")
        _run_write(settings, tmp_path, capsys)
        edited = _file_json(settings)
        edited["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c.example"
        Path(settings).write_text(json.dumps(edited))
        endpoint = _endpoint_file(tmp_path, "http://localhost:9999\n")
        _, out, _ = _invoke(_write_argv(settings, endpoint), tmp_path, capsys)
        written_line, left_line = out.splitlines()
        assert _named(left_line) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        assert "--replace" in out
        assert _file_json(settings)["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://c.example"

    def test_a_key_set_back_to_the_recorded_value_is_the_turn_ons_again(self, tmp_path, capsys):
        # Gap 5: the file shows the recorded value, and that is all the command can see.
        settings = str(tmp_path / "s.json")
        _run_write(settings, tmp_path, capsys)
        edited = _file_json(settings)
        edited["env"]["OTEL_LOGS_EXPORTER"] = "none"
        Path(settings).write_text(json.dumps(edited))
        edited["env"]["OTEL_LOGS_EXPORTER"] = "otlp"
        Path(settings).write_text(json.dumps(edited))
        endpoint = _endpoint_file(tmp_path, "http://localhost:9999\n")
        _, out, _ = _invoke(_write_argv(settings, endpoint), tmp_path, capsys)
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]

    def test_a_key_the_user_changed_to_todays_value_is_left_and_not_recorded(
        self, tmp_path, capsys
    ):
        settings = str(tmp_path / "s.json")
        _run_write(settings, tmp_path, capsys)
        edited = _file_json(settings)
        edited["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:9999"
        Path(settings).write_text(json.dumps(edited))
        endpoint = _endpoint_file(tmp_path, "http://localhost:9999\n")
        _, out, _ = _invoke(_write_argv(settings, endpoint), tmp_path, capsys)
        written_line, left_line = out.splitlines()
        assert _named(left_line) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        assert "--replace" not in out
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in _file_json(settings)[RECORD_SLOT]["written"]

    def test_running_the_same_write_twice_leaves_settings_and_record_byte_identical(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"},"a":1}')
        argv = _write_argv(settings, _endpoint_file(tmp_path))
        _invoke(argv, tmp_path, capsys)
        first = Path(settings).read_bytes()
        _, out, _ = _invoke(argv, tmp_path, capsys)
        assert Path(settings).read_bytes() == first
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]

    def test_a_write_that_writes_nothing_leaves_even_a_stale_record_byte_identical(
        self, tmp_path, capsys
    ):
        # An endpoint-only record; the user's endpoint is now the published one and the other
        # four are the user's equal values.
        settings = _settings_file(
            tmp_path,
            json.dumps(
                {
                    "env": turn_on.telemetry_entry(ENDPOINT),
                    RECORD_SLOT: _record(
                        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:1111"}
                    ),
                }
            ),
        )
        before = Path(settings).read_bytes()
        files = _CountingFiles()
        status, out, err = _write_with(files, settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert [_named(line) for line in out.splitlines()] == [list(TELEMETRY_KEYS)]
        assert Path(settings).read_bytes() == before
        assert files.published == 0

    def test_a_write_publishes_the_settings_and_the_record_in_one_publication_and_no_sidecar(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        files = _CountingFiles()
        _write_with(files, settings, tmp_path, capsys)
        assert files.published == 1
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["ep", "home", "settings.json"]

    def test_a_failed_publication_publishes_neither_settings_nor_record_and_prints_no_success_line(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}')
        before = Path(settings).read_bytes()
        files = _FilesThatFailToPublish()
        status, out, err = _write_with(files, settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert files.attempts == 1
        assert Path(settings).read_bytes() == before

    def test_a_write_with_no_published_endpoint_is_unchanged_whatever_the_file_holds(
        self, tmp_path, capsys
    ):
        settings = _settings_file(
            tmp_path, json.dumps({"env": {}, RECORD_SLOT: _record({"OTEL_LOGS_EXPORTER": "otlp"})})
        )
        before = Path(settings).read_bytes()
        nowhere = str(tmp_path / "nowhere")
        status, out, err = _invoke(_write_argv(settings, nowhere), tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == (
            f"no published telemetry endpoint at {nowhere} — machine has no receiver, "
            "nothing written\n"
        )
        assert Path(settings).read_bytes() == before

    @pytest.mark.parametrize(
        "flags", [["--remove"], ["--remove", "--force"]], ids=["by-value", "forced"]
    )
    def test_a_removal_that_deletes_keys_also_retires_every_recognized_record(
        self, tmp_path, capsys, flags
    ):
        original = {"permissions": {"allow": ["Read"]}, "env": {"KEEP": "me"}}
        settings = _settings_file(tmp_path, json.dumps(original))
        _run_write(settings, tmp_path, capsys)
        written = _file_json(settings)
        written[_slot(1)] = _record({"OTEL_LOGS_EXPORTER": "otlp"})
        written[_slot(2)] = {"theirs": True}
        written["added"] = 1
        Path(settings).write_text(json.dumps(written))
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), *flags), tmp_path, capsys
        )
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {**original, _slot(2): {"theirs": True}, "added": 1}
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["ep", "home", "settings.json"]

    def test_help_says_what_a_write_writes_and_leaves_and_where_the_record_is_kept(self, capsys):
        text = " ".join(_help_text(capsys).split())
        assert RECORD_SLOT in text and f"{RECORD_SLOT}_1" in text and f"{RECORD_SLOT}_2" in text
        assert "outside env" in text
        assert (
            "A write writes each key that is absent or was written by this command, leaves each "
            "value you set, and says on stdout which keys it wrote and which it left"
        ) in text
        assert "unless you pass --replace" in text

    def test_the_help_entries_for_replace_remove_and_force_state_the_ownership_rule(self, capsys):
        text = " ".join(_help_text(capsys).split())
        assert (
            "overwrite a value you already set on one of the five keys, and record the key as "
            "written by this command, so a later --remove takes it out and does not put your "
            "value back"
        ) in text
        assert "Whenever --remove changes the settings file, it also removes the record" in text
        assert "with --remove, delete the five keys whatever their values" in text
        assert "takes out only the values this command would write" not in text
        assert "removes them from it (--remove)" not in text


class TestPureTransformations:
    def test_merge_entry_creates_env_and_keeps_every_other_key(self):
        entry = turn_on.telemetry_entry(ENDPOINT)
        merged = turn_on.merge_entry({"x": 1}, entry)
        assert merged == {"x": 1, "env": entry}

    def test_merge_entry_does_not_change_its_input(self):
        settings = {"env": {"KEEP": "me"}}
        turn_on.merge_entry(settings, turn_on.telemetry_entry(ENDPOINT))
        assert settings == {"env": {"KEEP": "me"}}

    def test_conflicting_keys_are_those_set_to_another_value(self):
        entry = turn_on.telemetry_entry(ENDPOINT)
        env = {"OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "none", "OTHER": "x"}
        assert turn_on.conflicting_keys(env, entry) == ["OTEL_LOGS_EXPORTER"]


NO_KEYS_TO_REMOVE = "no telemetry keys to remove — nothing written"
NO_ENDPOINT_REMOVE = (
    "error: no published telemetry endpoint — cannot tell this command's entry from values you "
    "set yourself; nothing written. Pass --force to delete the five keys anyway."
)


REMOVAL_LEFT_WORDS = (
    " — no record shows this command wrote the value now set. "
    "Pass --force to remove what was left.\n"
)


def _removal_left_line(*keys: str) -> str:
    """The one stdout line a removal prints for the keys it left (`change_text.md`)."""
    return f"left {', '.join(keys)}{REMOVAL_LEFT_WORDS}"


class _FilesThatCannotReplace(turn_on.Files):
    """The filesystem port whose non-mutating check refuses, counting publications and checks."""

    def __init__(self) -> None:
        self.published = 0
        self.checked = 0

    def check_publishable(self, path: str) -> None:
        self.checked += 1
        raise PermissionError(13, "Permission denied")

    def write_bytes(self, path: str, content: bytes) -> None:
        self.published += 1
        raise PermissionError(13, "Permission denied")


def _run_remove(
    settings: str, tmp_path: Path, capsys, *extra: str, endpoint: str | None = ENDPOINT
) -> tuple[int, str, str]:
    """A remove with an endpoint file publishing `endpoint`, or with no endpoint file when
    `endpoint` is None."""
    endpoint_path = (
        str(tmp_path / "no-such-endpoint")
        if endpoint is None
        else _endpoint_file(tmp_path, endpoint + "\n")
    )
    return _invoke(_write_argv(settings, endpoint_path, "--remove", *extra), tmp_path, capsys)


def _env_with_entry(endpoint: str = ENDPOINT, **extra: str) -> dict[str, str]:
    return {**extra, **turn_on.telemetry_entry(endpoint)}


class TestRemoveMode:
    def test_remove_with_nothing_to_remove_prints_the_line_and_leaves_the_file_alone(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"permissions": {}}\n')
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, NO_KEYS_TO_REMOVE + "\n", "")
        assert Path(settings).read_bytes() == before

    def test_remove_from_a_file_that_does_not_exist_creates_nothing(self, tmp_path, capsys):
        settings = str(tmp_path / "absent.json")
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, NO_KEYS_TO_REMOVE + "\n", "")
        assert not Path(settings).exists()

    def test_remove_with_nothing_to_remove_works_with_no_endpoint_published(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env": {"KEEP": "me"}}\n')
        status, out, err = _run_remove(settings, tmp_path, capsys, endpoint=None)
        assert (status, out, err) == (0, NO_KEYS_TO_REMOVE + "\n", "")

    def test_remove_takes_only_keys_that_equal_the_published_values(self, tmp_path, capsys):
        # Gap 14: the file holds the five keys and no record, so every one is the user's.
        env = _env_with_entry()
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c.example"
        env["OTEL_LOGS_EXPORTER"] = "none"
        settings = _settings_file(tmp_path, json.dumps({"env": {"KEEP": "me", **env}}))
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line(*TELEMETRY_KEYS)
        assert Path(settings).read_bytes() == before
        assert "none" not in out and "c.example" not in out

    def test_remove_where_every_present_key_differs_writes_nothing_and_says_so(
        self, tmp_path, capsys
    ):
        settings = _settings_file(
            tmp_path, '{"env":{"OTEL_LOGS_EXPORTER":"none"},"x":1}'
        )
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line("OTEL_LOGS_EXPORTER")
        assert "none" not in out
        assert Path(settings).read_bytes() == before

    def test_remove_drops_env_when_nothing_is_left(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, json.dumps({"a": 1, "z": 2}))
        _run_write(settings, tmp_path, capsys)
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, "", "")
        assert Path(settings).read_text() == json.dumps({"a": 1, "z": 2}, indent=2) + "\n"

    def test_remove_with_present_keys_and_no_endpoint_refuses_without_force(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, json.dumps({"env": _env_with_entry(KEEP="me")}))
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys, endpoint=None)
        assert (status, out, err) == (1, "", NO_ENDPOINT_REMOVE + "\n")
        assert Path(settings).read_bytes() == before

    def test_force_removes_every_present_key_whatever_its_value_and_endpoint(
        self, tmp_path, capsys
    ):
        env = {key: f"mine-{key}" for key in TELEMETRY_KEYS}
        settings = _settings_file(tmp_path, json.dumps({"env": {"KEEP": "me", **env}}))
        status, out, err = _run_remove(settings, tmp_path, capsys, "--force", endpoint=None)
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {"env": {"KEEP": "me"}}

    def test_force_keeps_every_other_env_key_and_other_top_level_keys(self, tmp_path, capsys):
        settings = _settings_file(
            tmp_path,
            json.dumps({"b": 1, "env": {"KEEP": "café", **_env_with_entry()}, "a": [1]}),
        )
        _run_remove(settings, tmp_path, capsys, "--force")
        assert _file_json(settings) == {"b": 1, "env": {"KEEP": "café"}, "a": [1]}
        assert list(_file_json(settings)) == ["b", "env", "a"]
        assert "café" in Path(settings).read_text()

    def test_a_changed_endpoint_leaves_the_old_endpoint_value_in_place(self, tmp_path, capsys):
        # Written by the base or another tool: no record, so it is left whatever the endpoint is.
        settings = _settings_file(tmp_path, json.dumps({"env": _env_with_entry(ENDPOINT)}))
        before = Path(settings).read_bytes()
        new_endpoint = "http://localhost:9999"
        status, out, _ = _run_remove(settings, tmp_path, capsys, endpoint=new_endpoint)
        assert status == 0
        assert out == _removal_left_line(*TELEMETRY_KEYS)
        assert Path(settings).read_bytes() == before

    def test_turn_on_then_remove_leaves_the_users_own_settings(self, tmp_path, capsys):
        original = {"permissions": {"allow": ["Read"]}, "env": {"KEEP": "me"}}
        settings = _settings_file(tmp_path, json.dumps(original))
        _run_write_status = _run_write(settings, tmp_path, capsys)
        assert _run_write_status[0] == 0
        written = _file_json(settings)
        written["added"] = {"later": True}
        written["env"]["ADDED"] = "after"
        Path(settings).write_text(json.dumps(written))
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {
            "permissions": {"allow": ["Read"]},
            "env": {"KEEP": "me", "ADDED": "after"},
            "added": {"later": True},
        }

    def test_a_pre_existing_value_equal_to_the_turn_ons_is_removed_with_the_rest(
        self, tmp_path, capsys
    ):
        # The id is kept (CP-285); with no record the equal value is the user's and is left.
        settings = _settings_file(
            tmp_path, json.dumps({"env": {"OTEL_METRICS_EXPORTER": "otlp", "KEEP": "me"}})
        )
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line("OTEL_METRICS_EXPORTER")
        assert Path(settings).read_bytes() == before

    def test_a_value_overwritten_by_replace_is_not_restored_by_remove(self, tmp_path, capsys):
        settings = _settings_file(tmp_path, json.dumps({"env": {"OTEL_LOGS_EXPORTER": "none"}}))
        _run_write(settings, tmp_path, capsys, "--replace")
        status, _, _ = _run_remove(settings, tmp_path, capsys)
        assert status == 0
        assert "env" not in _file_json(settings)

    def test_remove_with_replace_is_accepted_and_changes_nothing(self, tmp_path, capsys):
        # Preloaded keys carry no record, so both runs leave them and name them the same way.
        text = json.dumps({"env": _env_with_entry(KEEP="me")})
        plain = _settings_file(tmp_path, text, "plain.json")
        replaced = _settings_file(tmp_path, text, "replaced.json")
        first = _run_remove(plain, tmp_path, capsys)
        second = _run_remove(replaced, tmp_path, capsys, "--replace")
        assert first[0] == second[0] == 0
        assert first[1:] == second[1:] == (_removal_left_line(*TELEMETRY_KEYS), "")
        assert Path(plain).read_bytes() == Path(replaced).read_bytes() == text.encode()

    @pytest.mark.parametrize(
        ("content", "line"),
        [
            (
                "nope",
                "is not valid JSON (Expecting value: line 1 column 1 (char 0)); nothing written",
            ),
            ("[1, 2]", "is not a JSON object; nothing written"),
            ('{"env": [1]}', "has an env that is not a JSON object; nothing written"),
        ],
        ids=["invalid-json", "not-an-object", "env-not-an-object"],
    )
    def test_remove_refuses_an_unusable_settings_file_as_write_does(
        self, tmp_path, capsys, content, line
    ):
        settings = _settings_file(tmp_path, content)
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out) == (1, "")
        assert err == f"error: {settings} {line}\n"
        assert Path(settings).read_text() == content

    def test_remove_names_a_settings_path_it_cannot_use(self, tmp_path, capsys):
        directory = tmp_path / "a-directory"
        directory.mkdir()
        status, out, err = _run_remove(str(directory), tmp_path, capsys)
        assert (status, out) == (1, "")
        assert str(directory) in err and "receiver" not in err

    def test_the_three_prompt_text_keys_are_never_removed(self, tmp_path, capsys):
        prompt_keys = {key: f"value-of-{key}" for key in PROMPT_TEXT_KEYS}
        settings = _settings_file(tmp_path, json.dumps({"env": prompt_keys}))
        _run_write(settings, tmp_path, capsys)
        _run_remove(settings, tmp_path, capsys)
        assert _file_json(settings) == {"env": prompt_keys}
        forced = _settings_file(
            tmp_path, json.dumps({"env": {**prompt_keys, **_env_with_entry()}}), "forced.json"
        )
        _run_remove(forced, tmp_path, capsys, "--force")
        assert _file_json(forced) == {"env": prompt_keys}

    def test_remove_keys_deletes_only_the_named_keys_and_drops_an_emptied_env(self):
        assert turn_on.remove_keys({"env": {"A": "1", "B": "2"}, "x": 1}, ["A"]) == {
            "env": {"B": "2"},
            "x": 1,
        }
        assert turn_on.remove_keys({"env": {"A": "1"}, "x": 1}, ["A"]) == {"x": 1}
        assert turn_on.remove_keys({"x": 1}, ["A"]) == {"x": 1}

    def test_remove_keys_does_not_change_its_input(self):
        settings = {"env": {"A": "1"}}
        turn_on.remove_keys(settings, ["A"])
        assert settings == {"env": {"A": "1"}}


def _written_into(tmp_path: Path, capsys, original: dict, *flags: str) -> str:
    """A settings file holding `original`, after a write: the keys and their record."""
    settings = _settings_file(tmp_path, json.dumps(original))
    assert _run_write(settings, tmp_path, capsys, *flags)[0] == 0
    return settings


class TestRemoveByRecord:
    """CP-274 to CP-277: a removal takes only the keys a recognized record shows the command wrote
    and the file still holds as recorded, and says which it left. The tests assert which keys each
    line names, in which order, that no value is printed and what the file holds afterwards."""

    @pytest.mark.parametrize("replace", [False, True], ids=["write", "write-with-replace"])
    @pytest.mark.parametrize("key", TELEMETRY_KEYS)
    def test_check_2_a_user_value_equal_to_the_turn_ons_survives_a_write_and_a_removal(
        self, tmp_path, capsys, key, replace
    ):
        """Station check 2 (CP-284, CP-277): the user's equal value is neither recorded nor
        removed; the other four are written and then removed."""
        original = {"env": {key: turn_on.telemetry_entry(ENDPOINT)[key], "KEEP": "me"}}
        settings = _settings_file(tmp_path, json.dumps(original))
        _run_write(settings, tmp_path, capsys, *(("--replace",) if replace else ()))
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line(key)
        assert _file_json(settings) == original

    @pytest.mark.parametrize(
        "user_env",
        [
            pytest.param(turn_on.telemetry_entry(ENDPOINT), id="equal-values"),
            pytest.param({key: f"mine-{key}" for key in TELEMETRY_KEYS}, id="different-values"),
            pytest.param(
                {
                    "CLAUDE_CODE_ENABLE_TELEMETRY": 1,
                    "OTEL_METRICS_EXPORTER": None,
                    "OTEL_LOGS_EXPORTER": ["otlp"],
                    "OTEL_EXPORTER_OTLP_PROTOCOL": {"a": 1},
                    "OTEL_EXPORTER_OTLP_ENDPOINT": True,
                },
                id="non-string-values",
            ),
        ],
    )
    def test_a_file_with_the_five_keys_and_no_record_is_left_alone_by_remove(
        self, tmp_path, capsys, user_env
    ):
        settings = _settings_file(tmp_path, json.dumps({"env": {"KEEP": "me", **user_env}}))
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line(*TELEMETRY_KEYS)
        assert "mine-" not in out and ENDPOINT not in out
        assert Path(settings).read_bytes() == before

    @pytest.mark.parametrize("key", TELEMETRY_KEYS)
    def test_the_left_line_is_true_of_a_key_holding_the_turn_ons_own_value(
        self, tmp_path, capsys, key
    ):
        settings = _settings_file(
            tmp_path, json.dumps({"env": {key: turn_on.telemetry_entry(ENDPOINT)[key]}})
        )
        _, out, _ = _run_remove(settings, tmp_path, capsys)
        assert _named(out) == [key]
        assert "differs" not in out and "not the value this command writes" not in out

    def test_remove_after_a_write_takes_the_recorded_keys_and_keeps_later_additions(
        self, tmp_path, capsys
    ):
        original = {"permissions": {"allow": ["Read"]}, "env": {"KEEP": "me"}}
        settings = _written_into(tmp_path, capsys, original)
        written = _file_json(settings)
        written["env"]["ADDED"] = "after"
        written["added"] = {"later": True}
        Path(settings).write_text(json.dumps(written))
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {
            "permissions": {"allow": ["Read"]},
            "env": {"KEEP": "me", "ADDED": "after"},
            "added": {"later": True},
        }
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["ep", "home", "settings.json"]

    def test_a_key_the_user_edited_after_the_write_is_left_and_a_second_remove_leaves_it_again(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        edited = _file_json(settings)
        edited["env"]["OTEL_LOGS_EXPORTER"] = "none"
        Path(settings).write_text(json.dumps(edited))
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, err) == (0, "")
        assert out == _removal_left_line("OTEL_LOGS_EXPORTER")
        assert "none" not in out
        assert _file_json(settings) == {"env": {"KEEP": "me", "OTEL_LOGS_EXPORTER": "none"}}
        again = Path(settings).read_bytes()
        assert _run_remove(settings, tmp_path, capsys) == (
            0,
            _removal_left_line("OTEL_LOGS_EXPORTER"),
            "",
        )
        assert Path(settings).read_bytes() == again

    def test_all_five_keys_edited_after_the_write_retire_the_record_and_remove_nothing(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        edited = _file_json(settings)
        edited["env"].update({key: f"mine-{key}" for key in TELEMETRY_KEYS})
        Path(settings).write_text(json.dumps(edited))
        files = _CountingFiles()
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, err) == (0, "")
        assert out == _removal_left_line(*TELEMETRY_KEYS)
        assert files.published == 1
        retired = _file_json(settings)
        assert RECORD_SLOT not in retired
        assert retired["env"] == {"KEEP": "me", **{k: f"mine-{k}" for k in TELEMETRY_KEYS}}
        # A key later set back to the value the record once held is not claimed by stale metadata.
        retired["env"]["OTEL_LOGS_EXPORTER"] = "otlp"
        Path(settings).write_text(json.dumps(retired))
        _run_remove(settings, tmp_path, capsys)
        assert _file_json(settings)["env"]["OTEL_LOGS_EXPORTER"] == "otlp"

    def test_a_changed_endpoint_does_not_stop_removal_of_the_recorded_old_endpoint(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        status, out, err = _run_remove(
            settings, tmp_path, capsys, endpoint="http://localhost:9999"
        )
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {"env": {"KEEP": "me"}}

    def test_remove_where_no_key_is_present_leaves_even_a_stale_record_byte_identical(
        self, tmp_path, capsys
    ):
        settings = _settings_file(
            tmp_path,
            json.dumps(
                {"env": {"KEEP": "me"}, RECORD_SLOT: _record({"OTEL_LOGS_EXPORTER": "otlp"})}
            ),
        )
        before = Path(settings).read_bytes()
        files = _CountingFiles()
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, out, err) == (0, NO_KEYS_TO_REMOVE + "\n", "")
        assert Path(settings).read_bytes() == before
        assert files.published == 0
        absent = str(tmp_path / "absent.json")
        assert _run_remove(absent, tmp_path, capsys) == (0, NO_KEYS_TO_REMOVE + "\n", "")
        assert not Path(absent).exists()

    def test_remove_with_no_endpoint_and_present_keys_refuses_even_with_a_valid_record(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        before = Path(settings).read_bytes()
        status, out, err = _run_remove(settings, tmp_path, capsys, endpoint=None)
        assert (status, out, err) == (1, "", NO_ENDPOINT_REMOVE + "\n")
        assert Path(settings).read_bytes() == before

    @pytest.mark.parametrize("endpoint", [ENDPOINT, None], ids=["endpoint", "no-endpoint"])
    @pytest.mark.parametrize("record", ["none", "valid", "malformed"])
    def test_force_removes_every_present_key_with_or_without_an_endpoint_and_retires_records(
        self, tmp_path, capsys, record, endpoint
    ):
        env = {"KEEP": "me", **{key: f"mine-{key}" for key in TELEMETRY_KEYS}}
        settings = {"env": env}
        if record == "valid":
            settings[RECORD_SLOT] = _record({"OTEL_LOGS_EXPORTER": "mine-OTEL_LOGS_EXPORTER"})
        if record == "malformed":
            settings[RECORD_SLOT] = {"format": "other", "written": {}}
        path = _settings_file(tmp_path, json.dumps(settings))
        status, out, err = _run_remove(path, tmp_path, capsys, "--force", endpoint=endpoint)
        assert (status, out, err) == (0, "", "")
        expected = {"env": {"KEEP": "me"}}
        if record == "malformed":
            expected[RECORD_SLOT] = {"format": "other", "written": {}}
        assert _file_json(path) == expected

    def test_a_value_overwritten_by_replace_is_recorded_and_removed_and_not_restored(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, json.dumps({"env": {"OTEL_LOGS_EXPORTER": "none"}}))
        _run_write(settings, tmp_path, capsys, "--replace")
        assert _file_json(settings)[RECORD_SLOT]["written"]["OTEL_LOGS_EXPORTER"] == "otlp"
        status, out, err = _run_remove(settings, tmp_path, capsys)
        assert (status, out, err) == (0, "", "")
        assert _file_json(settings) == {}

    def test_remove_with_replace_changes_nothing_after_a_write_too(self, tmp_path, capsys):
        original = {"env": {"KEEP": "me"}}
        plain = _written_into(tmp_path, capsys, original)
        replaced = str(tmp_path / "replaced.json")
        Path(replaced).write_bytes(Path(plain).read_bytes())
        first = _run_remove(plain, tmp_path, capsys)
        second = _run_remove(replaced, tmp_path, capsys, "--replace")
        assert first == second == (0, "", "")
        assert Path(plain).read_bytes() == Path(replaced).read_bytes()
        assert _file_json(plain) == original

    def test_the_three_prompt_text_keys_are_never_removed_and_non_telemetry_env_values_survive(
        self, tmp_path, capsys
    ):
        survivors = {"KEEP": "café", **{key: f"value-of-{key}" for key in PROMPT_TEXT_KEYS}}
        settings = _written_into(tmp_path, capsys, {"env": survivors})
        assert _run_remove(settings, tmp_path, capsys) == (0, "", "")
        assert _file_json(settings) == {"env": survivors}
        assert "café" in Path(settings).read_text()

    def test_remove_names_no_value_even_when_a_left_key_holds_a_secret(self, tmp_path, capsys):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        edited = _file_json(settings)
        edited["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://user:secret@c.example"
        Path(settings).write_text(json.dumps(edited))
        _, out, err = _run_remove(settings, tmp_path, capsys)
        assert "secret" not in out + err and "c.example" not in out + err

    @NOT_ROOT
    def test_remove_refuses_an_unusable_settings_file_even_when_every_present_key_is_left(
        self, tmp_path, capsys
    ):
        directory = tmp_path / "locked"
        directory.mkdir()
        target = directory / "settings.json"
        original = b'{"env":{"CLAUDE_CODE_ENABLE_TELEMETRY":"0","KEEP":"me"}}'
        target.write_bytes(original)
        target.chmod(0o640)
        link = tmp_path / "settings-link.json"
        link.symlink_to(target)
        directory.chmod(0o555)
        try:
            status, out, err = _run_remove(str(link), tmp_path, capsys)
            assert (status, out) == (1, "")
            assert str(link) in err
            assert link.is_symlink() and link.readlink() == target
            assert target.read_bytes() == original
            assert stat.S_IMODE(target.stat().st_mode) == 0o640
            assert list(directory.iterdir()) == [target]
        finally:
            directory.chmod(0o755)

    def test_the_target_is_checked_through_the_files_port_when_nothing_would_be_published(
        self, tmp_path, capsys
    ):
        settings = _settings_file(tmp_path, '{"env":{"OTEL_LOGS_EXPORTER":"none"}}')
        before = Path(settings).read_bytes()
        files = _FilesThatCannotReplace()
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert (files.checked, files.published) == (1, 0)
        assert Path(settings).read_bytes() == before

    def test_the_target_check_comes_after_the_absent_key_and_missing_endpoint_gates(
        self, tmp_path, capsys
    ):
        no_keys = _settings_file(tmp_path, '{"env":{"KEEP":"me"}}', "no-keys.json")
        files = _FilesThatCannotReplace()
        runtime = dataclasses.replace(_runtime(tmp_path), files=files)
        status, out, err = _invoke(
            _write_argv(no_keys, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=runtime,
        )
        assert (status, out, err) == (0, NO_KEYS_TO_REMOVE + "\n", "")
        present = _settings_file(tmp_path, json.dumps({"env": _env_with_entry()}), "present.json")
        status, out, err = _invoke(
            _write_argv(present, str(tmp_path / "nope"), "--remove"),
            tmp_path,
            capsys,
            runtime=runtime,
        )
        assert (status, out, err) == (1, "", NO_ENDPOINT_REMOVE + "\n")
        assert (files.checked, files.published) == (0, 0)

    def test_a_failed_remove_publication_preserves_the_record_and_settings_together(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        before = Path(settings).read_bytes()
        files = _FilesThatFailToPublish()
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert files.attempts == 1
        assert Path(settings).read_bytes() == before

    def test_a_failed_metadata_only_publication_publishes_nothing_and_prints_no_success_line(
        self, tmp_path, capsys
    ):
        settings = _written_into(tmp_path, capsys, {"env": {"KEEP": "me"}})
        edited = _file_json(settings)
        edited["env"].update({key: f"mine-{key}" for key in TELEMETRY_KEYS})
        Path(settings).write_text(json.dumps(edited))
        before = Path(settings).read_bytes()
        files = _FilesThatFailToPublish()
        status, out, err = _invoke(
            _write_argv(settings, _endpoint_file(tmp_path), "--remove"),
            tmp_path,
            capsys,
            runtime=dataclasses.replace(_runtime(tmp_path), files=files),
        )
        assert (status, out) == (1, "")
        assert err.startswith(f"error: cannot write {settings} (")
        assert files.attempts == 1
        assert Path(settings).read_bytes() == before

    def test_help_says_removal_takes_only_what_the_record_shows_and_force_removes_the_five(
        self, capsys
    ):
        text = " ".join(_help_text(capsys).split())
        assert "--remove takes out only what this command wrote unless you pass --force" in text
        assert (
            "a key this command wrote is removed, and a key holding a value you set is left"
        ) in text
        assert "takes out only the values this command would write" not in text


TEN_SPACES = " " * 10


def _absent_notice(path) -> str:
    return f"{TEN_SPACES}(no published telemetry endpoint at {path} — run uninstrumented)\n"


def _print_argv(endpoint: str | None, *extra: str) -> list[str]:
    return ["--print", *(() if endpoint is None else ("--endpoint-file", endpoint)), *extra]


def _run_print(tmp_path: Path, capsys, *extra: str, endpoint: str | None = None):
    """A print against `endpoint` (a path), or against the default beneath the empty home."""
    return _invoke(_print_argv(endpoint, *extra), tmp_path, capsys)


class TestPrintMode:
    def test_print_gives_one_compact_line_of_json_with_the_five_keys_in_order(
        self, tmp_path, capsys
    ):
        status, out, _ = _run_print(tmp_path, capsys, endpoint=_endpoint_file(tmp_path))
        assert status == 0
        entry = turn_on.telemetry_entry(ENDPOINT)
        assert out == json.dumps({"env": entry}, separators=(",", ":")) + "\n"
        assert list(json.loads(out)["env"]) == list(TELEMETRY_KEYS)
        assert ", " not in out and ": " not in out

    def test_print_with_no_prefix_reports_otlp_and_the_endpoint_on_stderr(
        self, tmp_path, capsys
    ):
        _, _, err = _run_print(tmp_path, capsys, endpoint=_endpoint_file(tmp_path))
        assert err == f"{TEN_SPACES}Telemetry:    OTLP → {ENDPOINT}\n"

    def test_print_touches_no_file(self, tmp_path, capsys):
        endpoint = _endpoint_file(tmp_path)
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
        _, out, _ = _run_print(tmp_path, capsys, endpoint=endpoint)
        assert out != ""
        after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
        assert after == before
        assert sorted(path.name for path in tmp_path.iterdir()) == ["ep", "home"]

    def test_stdout_is_the_json_alone(self, tmp_path, capsys):
        _, out, err = _run_print(tmp_path, capsys, endpoint=_endpoint_file(tmp_path))
        assert err != ""
        assert out.endswith("\n") and out.count("\n") == 1
        assert json.loads(out) == {"env": turn_on.telemetry_entry(ENDPOINT)}

    def test_print_with_an_absent_endpoint_prints_nothing_on_stdout_and_one_line_on_stderr(
        self, tmp_path, capsys
    ):
        path = str(tmp_path / "absent")
        status, out, err = _run_print(tmp_path, capsys, endpoint=path)
        assert (status, out) == (0, "")
        assert err == _absent_notice(path)

    @NOT_ROOT
    def test_print_with_a_mode_000_endpoint_file_gives_the_absent_reason(self, tmp_path, capsys):
        path = _endpoint_file(tmp_path)
        os.chmod(path, 0)
        status, out, err = _run_print(tmp_path, capsys, endpoint=path)
        assert (status, out) == (0, "")
        assert err == _absent_notice(path)

    @pytest.mark.parametrize(
        "content", ["ftp://localhost\n", "localhost:4318\n", "", "   \n"], ids=repr
    )
    def test_print_with_a_malformed_endpoint_gives_the_unreadable_reason(
        self, tmp_path, capsys, content
    ):
        path = _endpoint_file(tmp_path, content)
        status, out, err = _run_print(tmp_path, capsys, endpoint=path)
        assert (status, out) == (0, "")
        assert err == (
            f"{TEN_SPACES}(telemetry endpoint at {path} is unreadable or malformed — run "
            "uninstrumented)\n"
        )

    def test_print_with_a_directory_endpoint_gives_the_unreadable_reason(self, tmp_path, capsys):
        directory = tmp_path / "a-directory"
        directory.mkdir()
        status, out, err = _run_print(tmp_path, capsys, endpoint=str(directory))
        assert (status, out) == (0, "")
        assert err == (
            f"{TEN_SPACES}(telemetry endpoint at {directory} is unreadable or malformed — run "
            "uninstrumented)\n"
        )

    def test_print_with_a_non_loopback_endpoint_gives_the_off_machine_reason(
        self, tmp_path, capsys
    ):
        path = _endpoint_file(tmp_path, "http://example.com:4318\n")
        status, out, err = _run_print(tmp_path, capsys, endpoint=path)
        assert (status, out) == (0, "")
        assert err == (
            f"{TEN_SPACES}(telemetry endpoint at {path} is not on this machine (loopback only) "
            "— run uninstrumented)\n"
        )

    def test_without_an_endpoint_file_the_reason_shows_the_default_with_the_home_written_out(
        self, tmp_path, capsys
    ):
        status, out, err = _run_print(tmp_path, capsys)
        default = tmp_path / "home" / ".claude" / "tools" / "otel-receiver" / "endpoint"
        assert (status, out) == (0, "")
        assert err == _absent_notice(default)

    def test_an_unusable_endpoint_prints_no_summary_and_no_other_notice(self, tmp_path, capsys):
        path = _endpoint_file(tmp_path, "http://example.com\n")
        _, out, err = _run_print(tmp_path, capsys, endpoint=path)
        assert out == ""
        assert err.count("\n") == 1
        assert "Telemetry:" not in err


ATTRIBUTES_KEY = "OTEL_RESOURCE_ATTRIBUTES"
CARRY_REFUSAL = "contains a character OTEL_RESOURCE_ATTRIBUTES cannot carry"


def _print_with_prefix(
    tmp_path: Path,
    capsys,
    *extra: str,
    prefix: str = "acme",
    runtime: turn_on.Runtime | None = None,
) -> tuple[int, str, list[str]]:
    """A print with a published endpoint and `--prefix`; returns the status, the attributes value
    ('' when none was printed) and the stderr lines."""
    argv = _print_argv(_endpoint_file(tmp_path), "--prefix", prefix, *extra)
    status, out, err = _invoke(argv, tmp_path, capsys, runtime=runtime)
    env = json.loads(out)["env"] if out else {}
    return status, env.get(ATTRIBUTES_KEY, ""), err.removesuffix("\n").split("\n") if err else []


def _runtime_in_repo(tmp_path: Path, name: str) -> turn_on.Runtime:
    """A runtime whose git answers that the main checkout is a directory called `name`."""
    return _runtime(tmp_path, processes=FakeProcesses(str(tmp_path / name / ".git")))


def _label_args(*labels: str) -> list[str]:
    return [argument for label in labels for argument in ("--label", label)]


def _pairs(attributes: str) -> list[str]:
    return attributes.split(",") if attributes else []


class TestRunIdentity:
    def test_the_run_id_is_a_canonical_lowercase_uuid4_and_differs_per_invocation(
        self, tmp_path, capsys
    ):
        ids = []
        for _ in range(2):
            runtime = turn_on.build_runtime()
            _, attributes, _ = _print_with_prefix(tmp_path, capsys, runtime=runtime)
            ids.append(_pairs(attributes)[0].removeprefix("acme.run_id="))
        for run_id in ids:
            parsed = uuid.UUID(run_id)
            assert parsed.version == 4 and str(parsed) == run_id
        assert ids[0] != ids[1]

    def test_run_id_comes_first_then_instance_then_labels_in_argument_order(
        self, tmp_path, capsys
    ):
        runtime = _runtime_in_repo(tmp_path, "an-instance")
        status, attributes, _ = _print_with_prefix(
            tmp_path, capsys, *_label_args("b=2", "a=1", "b=3"), runtime=runtime
        )
        assert status == 0
        assert _pairs(attributes) == [
            f"acme.run_id={RUN_ID}",
            "acme.instance=an-instance",
            "acme.b=2",
            "acme.a=1",
            "acme.b=3",
        ]

    def test_every_pair_carries_exactly_the_chosen_prefix(self, tmp_path, capsys):
        runtime = _runtime_in_repo(tmp_path, "an-instance")
        _, attributes, _ = _print_with_prefix(
            tmp_path, capsys, *_label_args("k=v"), prefix="team", runtime=runtime
        )
        assert [pair.split(".", 1)[0] for pair in _pairs(attributes)] == ["team"] * 3
        assert "acme" not in attributes

    def test_the_json_and_the_summary_carry_the_same_attributes_value(self, tmp_path, capsys):
        argv = _print_argv(_endpoint_file(tmp_path), "--prefix", "acme", *_label_args("k=café"))
        _, out, err = _invoke(argv, tmp_path, capsys)
        attributes = json.loads(out)["env"][ATTRIBUTES_KEY]
        assert "\\u00e9" in out and "café" not in out
        assert err == f"{TEN_SPACES}Telemetry:    {attributes} → {ENDPOINT}\n"
        assert attributes.endswith("acme.k=café")

    def test_with_no_prefix_no_pairs_and_no_identity_notice(self, tmp_path, capsys):
        parent = tmp_path / "an instance"
        parent.mkdir()
        runtime = _runtime(tmp_path, processes=FakeProcesses(str(parent / ".git")))
        argv = _print_argv(_endpoint_file(tmp_path))
        _, out, err = _invoke(argv, tmp_path, capsys, runtime=runtime)
        assert ATTRIBUTES_KEY not in json.loads(out)["env"]
        assert err == f"{TEN_SPACES}Telemetry:    OTLP → {ENDPOINT}\n"
        assert runtime.processes.asked == []

    def test_a_non_ascii_label_is_escaped_in_the_json_and_typed_in_the_summary(
        self, tmp_path, capsys
    ):
        argv = _print_argv(
            _endpoint_file(tmp_path), "--prefix", "acme", *_label_args("k=é😀")
        )
        _, out, err = _invoke(argv, tmp_path, capsys)
        assert "acme.k=\\u00e9\\ud83d\\ude00" in out
        assert "é" not in out and "😀" not in out
        assert err.endswith(f"acme.k=é😀 → {ENDPOINT}\n")

    def test_the_labels_carry_only_inside_resource_attributes(self, tmp_path, capsys):
        argv = _print_argv(
            _endpoint_file(tmp_path), "--prefix", "acme", *_label_args("OTEL_LOG_USER_PROMPTS=1")
        )
        _, out, _ = _invoke(argv, tmp_path, capsys)
        env = json.loads(out)["env"]
        assert list(env) == [*TELEMETRY_KEYS, ATTRIBUTES_KEY]
        assert "acme.OTEL_LOG_USER_PROMPTS=1" in env[ATTRIBUTES_KEY]


def _git(cwd: Path, *argv: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(
        ["git", *identity, *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class TestInstance:
    def test_the_instance_is_the_main_checkouts_name_in_a_repo_and_in_its_worktree(
        self, tmp_path, capsys
    ):
        main = tmp_path / "an-instance"
        main.mkdir()
        _git(main, "init", "-q")
        _git(main, "commit", "-q", "--allow-empty", "-m", "first")
        linked = tmp_path / "somewhere-else"
        _git(main, "worktree", "add", "-q", str(linked), "-b", "side")
        for directory in (main, linked):
            runtime = _runtime(tmp_path, processes=turn_on.Processes())
            runtime = dataclasses.replace(runtime, cwd=directory)
            _, attributes, _ = _print_with_prefix(tmp_path, capsys, runtime=runtime)
            assert "acme.instance=an-instance" in _pairs(attributes)

    @pytest.mark.parametrize("answer", [None, ""], ids=["no-answer", "empty-answer"])
    def test_a_git_that_cannot_answer_attaches_no_instance_and_says_nothing(
        self, tmp_path, capsys, answer
    ):
        runtime = _runtime(tmp_path, processes=FakeProcesses(answer))
        status, attributes, err = _print_with_prefix(tmp_path, capsys, runtime=runtime)
        assert status == 0
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        assert err == [f"{TEN_SPACES}Telemetry:    {attributes} → {ENDPOINT}"]

    def test_outside_a_repo_attaches_no_instance_and_says_nothing(self, tmp_path, capsys):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        runtime = _runtime(tmp_path, processes=turn_on.Processes())
        runtime = dataclasses.replace(runtime, cwd=outside)
        _, attributes, err = _print_with_prefix(tmp_path, capsys, runtime=runtime)
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        assert len(err) == 1

    def test_git_missing_attaches_no_instance_and_says_nothing(
        self, tmp_path, capsys, monkeypatch
    ):
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        runtime = _runtime(tmp_path, processes=turn_on.Processes())
        _, attributes, err = _print_with_prefix(tmp_path, capsys, runtime=runtime)
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        assert len(err) == 1

    def test_an_instance_name_with_a_forbidden_character_drops_the_instance_alone(
        self, tmp_path, capsys
    ):
        runtime = _runtime_in_repo(tmp_path, "an instance")
        _, attributes, err = _print_with_prefix(
            tmp_path, capsys, *_label_args("k=v"), runtime=runtime
        )
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", "acme.k=v"]
        assert err[0] == (
            f"{TEN_SPACES}(acme.instance 'an instance' {CARRY_REFUSAL} — not attached)"
        )
        assert len(err) == 2


class TestLabels:
    @pytest.mark.parametrize("character", FORBIDDEN_CHARACTERS, ids=lambda c: f"char-{ord(c)}")
    def test_a_label_value_with_a_formerly_forbidden_character_is_attached_and_decodes_back(
        self, tmp_path, capsys, character
    ):
        # CP-270: the value the base refused is now carried, and only that label changes.
        value = f"a{character}b"
        _, attributes, err = _print_with_prefix(
            tmp_path, capsys, *_label_args("first=1", f"bad={value}", "last=3")
        )
        pairs = _pairs(attributes)
        assert [pair.partition("=")[0] for pair in pairs] == [
            "acme.run_id",
            "acme.first",
            "acme.bad",
            "acme.last",
        ]
        assert _decode_carried(pairs[2].partition("=")[2]) == value
        assert len(err) == 1 and err[0].startswith(TEN_SPACES + "Telemetry:")

    def test_an_empty_label_value_attaches_nothing_and_says_nothing(self, tmp_path, capsys):
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args("k="))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        assert len(err) == 1

    def test_a_label_is_split_at_its_first_equals(self, tmp_path, capsys):
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args("k=a", "j=a=b"))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", "acme.k=a", "acme.j=a%00003Db"]
        assert len(err) == 1

    def test_a_label_without_equals_is_refused_with_a_shaped_line(self, tmp_path, capsys):
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args("bare", "k=v"))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", "acme.k=v"]
        line = err[0]
        assert line.startswith(TEN_SPACES + "(") and line.endswith("not attached)")
        assert "'bare'" in line and "no '='" in line

    def test_an_empty_or_forbidden_key_is_refused_with_a_shaped_line_naming_the_key(
        self, tmp_path, capsys
    ):
        for argument, named in [("=1", "''"), ("a b=1", "'a b'"), ("a,b=1", "'a,b'")]:
            _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args(argument))
            assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"], argument
            line = err[0]
            assert line.startswith(TEN_SPACES + "(") and line.endswith("not attached)")
            assert named in line
            assert "cannot be carried" in line or "cannot carry" in line

    @pytest.mark.parametrize("key", ["run_id", "instance"])
    def test_run_id_and_instance_as_label_keys_are_refused_naming_prefix_dot_key(
        self, tmp_path, capsys, key
    ):
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args(f"{key}=mine"))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        assert err[0].startswith(TEN_SPACES + f"(acme.{key} ")
        assert err[0].endswith("not attached)")

    @pytest.mark.parametrize(
        "key", ["bogus", "arm", "walk_id", "chain", "role", "audit_target", "product"]
    )
    def test_any_other_key_is_attached(self, tmp_path, capsys, key):
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args(f"{key}=value"))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", f"acme.{key}=value"]
        assert len(err) == 1

    def test_a_refused_label_never_drops_its_neighbours(self, tmp_path, capsys):
        _, attributes, err = _print_with_prefix(
            tmp_path, capsys, *_label_args("a=1", "bare", "b b=2", "run_id=x", "c=3")
        )
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", "acme.a=1", "acme.c=3"]
        assert len(err) == 4

    def test_notices_come_before_the_summary_instance_first_then_labels_in_order(
        self, tmp_path, capsys
    ):
        runtime = _runtime_in_repo(tmp_path, "an instance")
        _, attributes, err = _print_with_prefix(
            tmp_path, capsys, *_label_args("bare", "k=a b"), runtime=runtime
        )
        # `k=a b` is now attached (CP-270), so the notices are the instance's and `bare`'s.
        assert "acme.k=a%000020b" in _pairs(attributes)
        assert len(err) == 3
        assert "acme.instance" in err[0]
        assert "'bare'" in err[1]
        assert err[2].startswith(TEN_SPACES + "Telemetry:")

    def test_an_unusable_endpoint_with_a_prefix_draws_no_run_id_and_asks_no_git(
        self, tmp_path, capsys
    ):
        runtime = _runtime_in_repo(tmp_path, "x")
        runtime = dataclasses.replace(runtime, new_run_id=_forbidden_new_run_id)
        argv = _print_argv(str(tmp_path / "absent"), "--prefix", "acme", *_label_args("bare"))
        status, out, err = _invoke(argv, tmp_path, capsys, runtime=runtime)
        assert (status, out) == (0, "")
        assert err.count("\n") == 1
        assert runtime.processes.asked == []


def _help_text(capsys) -> str:
    with pytest.raises(SystemExit):
        turn_on.run(["--help"], runtime=_untouchable_runtime())
    return " ".join(capsys.readouterr().out.split())


def _decode_carried(carried: str) -> str:
    """The typed value, recovered by the procedure `--help` states for a carried label value: read
    from left to right, replace each `%` and the six hexadecimal digits after it with the character
    whose code point they give, and do it once, so a `%` that a replacement produced is not read
    again. It calls nothing in the package."""
    typed = []
    position = 0
    while position < len(carried):
        if carried[position] == "%":
            digits = carried[position + 1 : position + 7]
            assert len(digits) == 6 and set(digits) <= set("0123456789ABCDEF"), carried
            typed.append(chr(int(digits, 16)))
            position += 7
        else:
            typed.append(carried[position])
            position += 1
    return "".join(typed)


# CP-270's characters: each whitespace character, the five punctuation characters, and `%`.
CARRIED_WHITESPACE = (
    " ", "\t", "\n", "\r", "\v", "\f", "\u00a0", "\u2003", "\u3000", "\u2028",
)
CARRIED_PUNCTUATION = ('"', ",", ";", "\\", "=")


def _carried_value(tmp_path: Path, capsys, value: str, key: str = "note") -> str:
    """The value the command carries for `--label key=value` under `--prefix team`; every other
    thing the run printed is checked to be the summary alone."""
    status, attributes, err = _print_with_prefix(
        tmp_path, capsys, *_label_args(f"{key}={value}"), prefix="team"
    )
    assert status == 0
    assert len(err) == 1 and err[0].startswith(TEN_SPACES + "Telemetry:"), err
    pairs = _pairs(attributes)
    assert pairs[0] == f"team.run_id={RUN_ID}" and len(pairs) == 2, pairs
    return pairs[1].removeprefix(f"team.{key}=")


class TestCarriedLabelValues:
    """CP-270 to CP-272: a non-empty label value is carried, encoded, and `--help` says how to
    decode it. The decoder here follows the help's words and calls no production code."""

    def test_check_1_a_label_value_with_a_space_is_carried_and_decodes_to_the_typed_value(
        self, tmp_path, capsys
    ):
        """Station check 1 (CP-284): `--label note="two words"` is carried, with no refusal line,
        and a value holding `,` `;` `"` `=` is held and recovered the same way."""
        carried = _carried_value(tmp_path, capsys, "two words")
        assert " " not in carried
        assert _decode_carried(carried) == "two words"
        punctuation = 'a,b;c"d=e'
        assert _decode_carried(_carried_value(tmp_path, capsys, punctuation)) == punctuation

    def test_the_test_decoder_follows_the_procedure_the_help_states(self, capsys):
        help_text = _help_text(capsys)
        # The words of `--help` this decoder follows (CP-271).
        assert "A value is carried as typed unless it holds whitespace of any kind" in help_text
        assert "or a %" in help_text
        assert "% followed by six hexadecimal digits" in help_text
        assert "read the carried value from left to right" in help_text
        assert "Do this once: a % that a replacement produced is not read again." in help_text
        assert "turn_on" not in _decode_carried.__code__.co_names
        assert _decode_carried("%000025000020") == "%000020"

    @pytest.mark.parametrize("character", [*CARRIED_WHITESPACE, *CARRIED_PUNCTUATION, "%"])
    def test_every_cp_270_character_is_carried_and_none_survives_in_the_carried_form(
        self, tmp_path, capsys, character
    ):
        value = f"a{character}b"
        carried = _carried_value(tmp_path, capsys, value)
        assert not any(c.isspace() or c in '",;\\=' for c in carried)
        assert _decode_carried(carried) == value

    def test_a_combination_value_decodes_back(self, tmp_path, capsys):
        value = 'a,b;c"d=e\\f\tg'
        assert _decode_carried(_carried_value(tmp_path, capsys, value)) == value

    @pytest.mark.parametrize("value", ["widgetco", "café", "🙂", "a-b_c.d/e:f"])
    def test_a_value_with_none_of_the_reserved_characters_is_carried_exactly_as_typed(
        self, tmp_path, capsys, value
    ):
        assert _carried_value(tmp_path, capsys, value) == value

    def test_a_non_ascii_value_is_escaped_in_the_json_and_typed_in_the_summary(
        self, tmp_path, capsys
    ):
        argv = _print_argv(_endpoint_file(tmp_path), "--prefix", "team", *_label_args("note=café"))
        _, out, err = _invoke(argv, tmp_path, capsys)
        assert "team.note=caf\\u00e9" in out
        assert "team.note=café" in err

    @pytest.mark.parametrize(
        ("typed", "carried"),
        [
            ("%", "%000025"),
            ("100%", "100%000025"),
            ("%%", "%000025%000025"),
            ("%000020", "%000025000020"),
            ("50% off", "50%000025%000020off"),
        ],
    )
    def test_a_literal_percent_is_carried_encoded_and_escape_looking_text_decodes_to_itself(
        self, tmp_path, capsys, typed, carried
    ):
        assert _carried_value(tmp_path, capsys, typed) == carried
        assert _decode_carried(carried) == typed

    def test_a_value_of_only_whitespace_is_carried(self, tmp_path, capsys):
        assert _decode_carried(_carried_value(tmp_path, capsys, " \t ")) == " \t "

    def test_a_supplementary_plane_character_stays_typed_and_decodes_from_six_digits(
        self, tmp_path, capsys
    ):
        # The six-digit width holds any code point: a `%` beside a supplementary-plane character
        # is escaped in six digits and the character itself is left as typed.
        value = "%🙂"
        assert _carried_value(tmp_path, capsys, value) == "%000025🙂"
        assert _decode_carried("%01F642") == "🙂"

    def test_pair_order_is_run_id_instance_then_labels_in_the_order_given(self, tmp_path, capsys):
        runtime = _runtime_in_repo(tmp_path, "an-instance")
        _, attributes, _ = _print_with_prefix(
            tmp_path,
            capsys,
            *_label_args("b=2", "a=two words", "c=3"),
            runtime=runtime,
        )
        assert _pairs(attributes) == [
            f"acme.run_id={RUN_ID}",
            "acme.instance=an-instance",
            "acme.b=2",
            "acme.a=two%000020words",
            "acme.c=3",
        ]

    def test_the_summary_line_shows_the_carried_value_and_equals_the_json_attributes(
        self, tmp_path, capsys
    ):
        argv = _print_argv(_endpoint_file(tmp_path), "--prefix", "acme", *_label_args("k=a b,c"))
        _, out, err = _invoke(argv, tmp_path, capsys)
        attributes = json.loads(out)["env"][ATTRIBUTES_KEY]
        assert err == f"{TEN_SPACES}Telemetry:    {attributes} → {ENDPOINT}\n"
        assert "a%000020b%00002Cc" in err and "a b,c" not in err

    def test_the_refusals_of_cp_272_are_unchanged(self, tmp_path, capsys):
        for argument, named in [
            ("a b=1", "'a b'"),
            ("a,b=1", "'a,b'"),
            ("run_id=x", "acme.run_id"),
            ("instance=x", "acme.instance"),
            ("bare", "'bare'"),
            ("=1", "''"),
        ]:
            _, attributes, err = _print_with_prefix(
                tmp_path, capsys, *_label_args(argument, "ok=1 2")
            )
            assert _pairs(attributes) == [f"acme.run_id={RUN_ID}", "acme.ok=1%0000202"]
            assert len(err) == 2, argument
            assert err[0].startswith(TEN_SPACES + "(") and err[0].endswith("not attached)")
            assert named in err[0]
        _, attributes, err = _print_with_prefix(tmp_path, capsys, *_label_args("k="))
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"] and len(err) == 1

    def test_the_instance_pair_still_refuses_a_name_the_format_cannot_take(self, tmp_path, capsys):
        runtime = _runtime_in_repo(tmp_path, "an instance")
        _, attributes, err = _print_with_prefix(tmp_path, capsys, runtime=runtime)
        assert _pairs(attributes) == [f"acme.run_id={RUN_ID}"]
        notice = f"{TEN_SPACES}(acme.instance 'an instance' {CARRY_REFUSAL} — not attached)"
        assert err[0] == notice

    def test_help_states_the_decoding_and_no_longer_says_such_a_value_is_not_attached(
        self, capsys
    ):
        help_text = _help_text(capsys)
        assert "%000020 is a space" in help_text and "%000025 a %" in help_text
        assert "two words is carried as two%000020words" in help_text
        assert "A label whose key or value holds" not in help_text
        assert "A label whose key holds whitespace" in help_text


HOST_ENVIRONMENT = {
    "INSTRUMENT_RUN_ARM": "arm-from-host",
    "INSTRUMENT_RUN_WALK_ID": "walk-from-host",
    "INSTRUMENT_RUN_CHAIN": "chain-from-host",
    "INSTRUMENT_RUN_ROLE": "role-from-host",
    "INSTRUMENT_RUN_AUDIT_TARGET": "target-from-host",
    "KENSAIAPP_CONTEXT_CAP": "123456",
}
HOST_TAG_KEYS = ("arm", "walk_id", "chain", "role", "audit_target", "product")
SETTINGS_ARGV = ["--settings-file", "s.json", "--endpoint-file", "ep"]
EVERY_MODE_AND_FLAG = [
    SETTINGS_ARGV,
    [*SETTINGS_ARGV, "--replace"],
    [*SETTINGS_ARGV, "--force"],
    [*SETTINGS_ARGV, "--remove"],
    [*SETTINGS_ARGV, "--remove", "--force"],
    [*SETTINGS_ARGV, "--remove", "--replace"],
    ["--print", "--endpoint-file", "ep"],
    ["--print", "--endpoint-file", "ep", "--prefix", "acme", "--label", "project=p"],
]


def _exercise_every_mode(directory: Path, capsys, initial_env: dict[str, str]) -> list[tuple]:
    """Every mode with every flag it accepts, run in `directory` (the working directory) against
    the same files. Returns one (argv, status, stdout, stderr, settings text) per invocation."""
    (directory / "ep").write_text(ENDPOINT + "\n")
    results = []
    for argv in EVERY_MODE_AND_FLAG:
        settings = directory / "s.json"
        settings.write_text(json.dumps({"env": initial_env}))
        status = turn_on.run(argv, runtime=_runtime(directory))
        captured = capsys.readouterr()
        results.append((argv, status, captured.out, captured.err, settings.read_text()))
    return results


class TestGuardsAcrossEveryMode:
    def test_host_variables_and_a_project_card_change_no_output(
        self, tmp_path, capsys, monkeypatch
    ):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        without = _exercise_every_mode(plain, capsys, {"KEEP": "me"})

        carded = tmp_path / "carded"
        card = carded / "system" / "projects" / "p" / "project.md"
        card.parent.mkdir(parents=True)
        card.write_text("**Product path:** /somewhere/else\n")
        for name, value in HOST_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.chdir(carded)
        with_host = _exercise_every_mode(carded, capsys, {"KEEP": "me"})

        assert with_host == without
        everything = "".join(part for result in with_host for part in result[2:])
        assert "KENSAIAPP_CONTEXT_CAP" not in everything
        for value in HOST_ENVIRONMENT.values():
            assert value not in everything

    def test_a_user_given_label_named_like_a_host_tag_is_attached(
        self, tmp_path, capsys, monkeypatch
    ):
        for name, value in HOST_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        labels = _label_args(*(f"{key}=mine" for key in HOST_TAG_KEYS))
        _, attributes, _ = _print_with_prefix(tmp_path, capsys, *labels)
        assert _pairs(attributes) == [
            f"acme.run_id={RUN_ID}",
            *(f"acme.{key}=mine" for key in HOST_TAG_KEYS),
        ]

    def test_importing_the_module_prints_nothing_and_sets_no_environment_variable(self):
        root = Path(turn_on.__file__).resolve().parent.parent
        code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "before = dict(os.environ)\n"
            "import ka_telemetry.turn_on\n"
            "assert dict(os.environ) == before\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code, str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")

    def test_no_mode_or_flag_writes_or_prints_a_prompt_text_key(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        for argv, _, out, err, settings in _exercise_every_mode(
            tmp_path, capsys, {"OTEL_LOGS_EXPORTER": "none"}
        ):
            for key in PROMPT_TEXT_KEYS:
                assert key not in out + err + settings, (argv, key)
        with pytest.raises(SystemExit):
            turn_on.run(["--help"], runtime=_untouchable_runtime())
        assert not any(key in capsys.readouterr().out for key in PROMPT_TEXT_KEYS)

    def test_a_label_with_a_prompt_text_name_stays_inside_the_attributes_value(
        self, tmp_path, capsys
    ):
        labels = _label_args(*(f"{key}=1" for key in PROMPT_TEXT_KEYS))
        argv = _print_argv(_endpoint_file(tmp_path), "--prefix", "acme", *labels)
        _, out, _ = _invoke(argv, tmp_path, capsys)
        env = json.loads(out)["env"]
        assert set(env) == {*TELEMETRY_KEYS, ATTRIBUTES_KEY}
        assert all(f"acme.{key}=1" in env[ATTRIBUTES_KEY] for key in PROMPT_TEXT_KEYS)

    def test_prompt_text_keys_the_user_already_has_survive_write_remove_and_force(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mine = {key: f"mine-{key}" for key in PROMPT_TEXT_KEYS}
        for argv, status, _, _, settings in _exercise_every_mode(tmp_path, capsys, mine):
            if "--print" in argv:
                continue
            written = json.loads(settings)["env"]
            assert status == 0, argv
            assert {key: written[key] for key in PROMPT_TEXT_KEYS} == mine, argv

    def test_no_mode_opens_a_network_connection(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ep").write_text("http://localhost:9\n")
        argvs = [
            [*SETTINGS_ARGV],
            [*SETTINGS_ARGV, "--remove"],
            ["--print", "--endpoint-file", "ep", "--prefix", "acme"],
        ]

        def run_all() -> list[tuple]:
            results = []
            for argv in argvs:
                (tmp_path / "s.json").write_text("{}")
                status = turn_on.run(argv, runtime=_runtime(tmp_path))
                results.append((status, *capsys.readouterr()))
            return results

        allowed = run_all()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("a network connection was attempted")

        for name in ("connect", "connect_ex", "bind"):
            monkeypatch.setattr(socket.socket, name, forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        assert run_all() == allowed

    def test_the_module_starts_no_receiver_and_launches_no_shell(self):
        source = Path(turn_on.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "receiver" not in imported and "socket" not in imported
        assert "shell=True" not in source
        for forbidden in ("os.system", "os.popen", "Popen", "os.exec", "os.spawn"):
            assert forbidden not in source, forbidden
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ]
        assert len(calls) == 1
        command = calls[0].args[0]
        assert [element.value for element in command.elts[:2]] == ["git", "rev-parse"]
