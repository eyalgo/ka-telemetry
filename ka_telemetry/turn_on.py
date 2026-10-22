"""turn_on — `ka-telemetry turn-on`: write the five telemetry keys into a Claude Code settings
file, remove them, or print them for one run.

WHY THIS EXISTS
    Claude Code sends no telemetry until five `env` keys point it at the receiver on this machine.
    This command is the one place those keys and their values are built, so nobody edits the
    settings JSON by hand and no second copy of the key set can drift from the first.

    The command never connects to the endpoint it reads, never starts or stops a receiver, and
    reads nothing from the environment: where it looks for the endpoint is a flag or the home
    directory, and the label prefix is a value the user chooses.

The collaborators the command reaches the world through (files, a read-only git lookup, a run id,
the home and working directories) arrive as one `Runtime`. `build_runtime` is the only place a
concrete one is made, and only `main` calls it.
"""

from __future__ import annotations

import argparse
import errno
import io
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ka_telemetry import run_usage, stream_usage

# The characters an OTEL_RESOURCE_ATTRIBUTES pair cannot carry: whitespace, a double quote, a
# comma, a semicolon, a backslash and an equals sign.
_FORBIDDEN_CHARACTERS = frozenset(' \t\n\r\v\f",;\\=')

_WHITESPACE = str.maketrans("", "", " \t\n\r\v\f")

# A label value is carried as typed except for whitespace of any kind and these characters: the
# five a pair cannot hold, and `%`, which the escape itself starts with.
_ESCAPED_IN_VALUES = frozenset('",;\\=%')

# The home-relative place the receiver publishes its endpoint, when asked to.
_DEFAULT_ENDPOINT = Path(".claude/tools/otel-receiver/endpoint")

# A published endpoint stays on this machine: the host, with an optional port, is a loopback one.
# Anchored whole, so a lookalike host or a userinfo prefix cannot satisfy it.
_LOOPBACK_AUTHORITY = re.compile(r"(localhost|127\.[0-9]+\.[0-9]+\.[0-9]+|\[::1\])(:[0-9]+)?")

_CANNOT_CARRY = "OTEL_RESOURCE_ATTRIBUTES cannot carry"

_NEXT_STEP = "Run `ka-telemetry turn-on --help` to see what it accepts."

_DESCRIPTION = (
    "Writes the five keys that make Claude Code send its telemetry to the receiver on this "
    "machine under env in a Claude Code settings file, removes from it the ones this command "
    "wrote (--remove), or prints them for one run (--print). The five keys are "
    "CLAUDE_CODE_ENABLE_TELEMETRY, OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER, "
    "OTEL_EXPORTER_OTLP_PROTOCOL and OTEL_EXPORTER_OTLP_ENDPOINT. The endpoint is read from the "
    "endpoint file (see --endpoint-file), which ka-telemetry receive --publish-endpoint PATH "
    "writes to PATH. An endpoint not on this machine is not used, and with no endpoint on this "
    "machine the five keys are neither written nor printed. This command records each key it "
    "writes, with the value it wrote, in the same settings file but outside env: under the first "
    "of the top-level keys _ka_telemetry_turn_on, _ka_telemetry_turn_on_1, "
    "_ka_telemetry_turn_on_2 and so on that holds nothing else. A key counts as written by this "
    "command while the record names it and the key still holds the value recorded. Any other "
    "value on the five keys counts as one you set, including one an earlier version of this "
    "command wrote, which kept no record. A value you already set on one of the five keys is "
    "never overwritten unless you pass --replace, and --remove takes out only what this command "
    "wrote unless you pass --force. A write writes each key that is absent or was written by "
    "this command, leaves each value you set, and says on stdout which keys it wrote and which "
    "it left. --print touches no settings file: it prints on stdout the JSON that claude "
    "--settings accepts, and its notices on stderr."
)


class Mode(Enum):
    """What one invocation does. `--print` and `--remove` choose it; with neither, it writes."""

    WRITE = "write"
    REMOVE = "remove"
    PRINT = "print"


class EndpointOutcome(Enum):
    """What reading the endpoint file found."""

    PUBLISHED = "published"
    ABSENT = "absent"  # no file, or one that cannot be read
    MALFORMED = "malformed"  # readable, but the first line is not an http(s) endpoint
    OFF_MACHINE = "off machine"  # an http(s) endpoint naming a host that is not loopback


@dataclass(frozen=True)
class Endpoint:
    """The one result every mode consumes. `path_text` is the path as typed, or the default with
    the home directory written out; `given` says whether `--endpoint-file` named it."""

    outcome: EndpointOutcome
    value: str | None
    path_text: str
    given: bool


class _MissingParent(OSError):
    """An unusable path, distinct from an absent file in an existing directory."""


class Files:
    """The filesystem port. Paths arrive as the user typed them."""

    def read_bytes(self, path: str) -> bytes:
        """The file's bytes. Raises the specific `OSError` for absence, an unreadable file or a
        directory."""
        target = Path(path)
        try:
            return target.read_bytes()
        except FileNotFoundError as failure:
            if not target.resolve().parent.is_dir():
                raise _MissingParent(failure.errno, failure.strerror, path) from None
            raise

    def check_publishable(self, path: str) -> None:
        """Raises the `OSError` `write_bytes` would raise for a target that cannot be replaced,
        and changes nothing: an existing file must permit writing, and the directory around the
        resolved target must permit staging a file beside it and replacing it."""
        target = Path(path).resolve()
        if target.exists() and not os.access(target, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        if not os.access(target.parent, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    def write_bytes(self, path: str, content: bytes) -> None:
        """Publishes `content` as the file's whole new content, or raises `OSError` and leaves
        the file as it was. The bytes are staged beside the resolved target (so a symlink keeps
        pointing where it did) and replace it in one step; an existing file keeps its mode."""
        target = Path(path).resolve()
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        if mode is not None and not os.access(target, os.W_OK):
            # Replacing would succeed through the writable directory around a file that forbids
            # writing, and that bypasses the file's own protection.
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(descriptor, "wb") as staged:
                staged.write(content)
            if mode is not None:
                os.chmod(staging, mode)
            os.replace(staging, target)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise


class Processes:
    """The process port: the one read-only question the command asks git."""

    def git_common_dir(self, cwd: Path) -> str | None:
        """The absolute common directory git reports for `cwd`, or `None` when git is missing,
        `cwd` is not in a repository, or git answers nothing."""
        try:
            done = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        answer = done.stdout.strip()
        return answer if done.returncode == 0 and answer else None


@dataclass(frozen=True, kw_only=True)
class Runtime:
    files: Files
    processes: Processes
    new_run_id: Callable[[], str]
    home: Path
    cwd: Path


_KEY_ORDER = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


# The ownership record: a tagged object at a top-level key of the settings file, outside `env`.
_RECORD_FORMAT = "ka-telemetry.turn-on/1"
_RECORD_SLOT = "_ka_telemetry_turn_on"
_RECORD_SLOT_NAME = re.compile(rf"{_RECORD_SLOT}(?:_([1-9][0-9]*))?")


def telemetry_entry(endpoint: str) -> dict[str, str]:
    """The five keys and their values, in the order they are written."""
    values = ("1", "otlp", "otlp", "http/json", endpoint)
    return dict(zip(_KEY_ORDER, values, strict=True))


def _classify_endpoint(first_line: str) -> EndpointOutcome:
    if not first_line.startswith(("http://", "https://")):
        return EndpointOutcome.MALFORMED
    authority = first_line.split("://", 1)[1].split("/", 1)[0]
    if _LOOPBACK_AUTHORITY.fullmatch(authority) is None:
        return EndpointOutcome.OFF_MACHINE
    return EndpointOutcome.PUBLISHED


def resolve_endpoint(endpoint_file: str | None, runtime: Runtime) -> Endpoint:
    """Reads the published endpoint from `endpoint_file`, or from the default beneath the home
    directory when none is named. No environment variable changes where it looks."""
    given = endpoint_file is not None
    path_text = str(runtime.home / _DEFAULT_ENDPOINT) if endpoint_file is None else endpoint_file
    try:
        content = runtime.files.read_bytes(path_text)
    except IsADirectoryError:
        return Endpoint(EndpointOutcome.MALFORMED, None, path_text, given)
    except OSError:
        return Endpoint(EndpointOutcome.ABSENT, None, path_text, given)
    first_line = content.decode("utf-8", errors="replace").split("\n", 1)[0].translate(_WHITESPACE)
    outcome = _classify_endpoint(first_line)
    value = first_line if outcome is EndpointOutcome.PUBLISHED else None
    return Endpoint(outcome, value, path_text, given)


def merge_entry(settings: dict, entry: dict[str, str]) -> dict:
    """`settings` with `entry` merged under `env`, which is created when absent. Every other
    top-level key and every other `env` key keeps its value and its place."""
    merged = dict(settings)
    merged_env = dict(merged.get("env") or {})
    merged_env.update(entry)
    merged["env"] = merged_env
    return merged


def conflicting_keys(env: dict, entry: dict[str, str]) -> list[str]:
    """The keys `env` already sets to a value other than the one `entry` would write."""
    return [key for key in entry if key in env and env[key] != entry[key]]


def remove_keys(settings: dict, keys: list[str]) -> dict:
    """`settings` without `keys` under `env`; `env` is dropped once nothing is left in it. Every
    other top-level key keeps its value and its place."""
    updated = dict(settings)
    env = {name: value for name, value in (updated.get("env") or {}).items() if name not in keys}
    if env:
        updated["env"] = env
    else:
        updated.pop("env", None)
    return updated


def _slot_order(name: str) -> tuple[int, str] | None:
    """Numeric order of a record slot, or `None` for a key outside the family.

    Compare digit count then digits to avoid imposing Python's integer-conversion limit on
    settings keys. The slot grammar excludes leading zeroes, so this is numeric order.
    """
    matched = _RECORD_SLOT_NAME.fullmatch(name)
    if matched is None:
        return None
    digits = matched.group(1) or ""
    return len(digits), digits


def _slot_name(number: int) -> str:
    return _RECORD_SLOT if number == 0 else f"{_RECORD_SLOT}_{number}"


def _recorded_writes(value: object) -> dict[str, str] | None:
    """What a recognized record names as written, or `None` when `value` is not one: the exact
    format string, exactly the two fields, and a non-empty `written` object of the five keys with
    string values."""
    if not isinstance(value, dict) or set(value) != {"format", "written"}:
        return None
    written = value["written"]
    if value["format"] != _RECORD_FORMAT or not isinstance(written, dict) or not written:
        return None
    if any(key not in _KEY_ORDER or not isinstance(text, str) for key, text in written.items()):
        return None
    return written


def _recognized_slots(settings: dict) -> list[str]:
    """The top-level keys holding a recognized record, lowest-numbered first."""
    slots = [
        name
        for name, value in settings.items()
        if _slot_order(name) is not None and _recorded_writes(value) is not None
    ]
    return sorted(slots, key=_slot_order)


def _written_by_the_turn_on(settings: dict, env: dict) -> set[str]:
    """The keys a recognized record shows the command wrote and the file still holds as recorded."""
    return {
        key
        for slot in _recognized_slots(settings)
        for key, recorded in _recorded_writes(settings[slot]).items()
        if key in env and env[key] == recorded
    }


def _record_slot(settings: dict) -> str:
    """The slot a new record takes: the lowest recognized one, else the first unoccupied."""
    recognized = _recognized_slots(settings)
    if recognized:
        return recognized[0]
    number = 0
    while _slot_name(number) in settings:
        number += 1
    return _slot_name(number)


def _with_record(settings: dict, written: dict[str, str]) -> dict:
    """`settings` with every recognized record replaced by one naming `written`, in the lowest
    recognized slot or a new last top-level key. An unrecognized value in any slot stays."""
    slot = _record_slot(settings)
    updated = {
        name: value
        for name, value in settings.items()
        if name == slot or name not in _recognized_slots(settings)
    }
    updated[slot] = {"format": _RECORD_FORMAT, "written": written}
    return updated


def _without_records(settings: dict) -> dict:
    """`settings` without any recognized record; every other top-level key keeps its place."""
    recognized = set(_recognized_slots(settings))
    return {name: value for name, value in settings.items() if name not in recognized}


def _status_lines(written: list[str], left: list[str], other_valued: list[str]) -> list[str]:
    """What a write says on stdout: the keys it wrote, then the keys it left, in the order given.
    No value is named."""
    lines = []
    if written:
        lines.append(f"wrote {', '.join(written)}")
    if left and other_valued:
        lines.append(
            f"left {', '.join(left)} \u2014 already set. Pass --replace to overwrite "
            f"{', '.join(other_valued)} with the value this command writes."
        )
    elif left:
        lines.append(f"left {', '.join(left)} \u2014 already set to the value this command writes")
    return lines


class SettingsRefused(Exception):
    """The command's refusal, worded for stderr after `error: `."""


def _reason(failure: OSError) -> str:
    """The operating system's description of a failure: no class name, no number, no path."""
    return failure.strerror or str(failure)


def _parse_settings(raw: bytes, path_text: str) -> dict:
    """The settings object `raw` holds. The bytes are read as text mode reads them (UTF-8,
    universal newlines) so the decoder's message, which the refusal quotes, is the same one."""
    try:
        text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline=None).read()
        settings = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as problem:
        raise SettingsRefused(f"{path_text} is not valid JSON ({problem}); nothing written")
    if not isinstance(settings, dict):
        raise SettingsRefused(f"{path_text} is not a JSON object; nothing written")
    if not isinstance(settings.get("env") or {}, dict):
        raise SettingsRefused(f"{path_text} has an env that is not a JSON object; nothing written")
    return settings


def _read_settings(path_text: str, files: Files) -> dict:
    """The settings the file holds, `{}` for one that does not exist yet."""
    try:
        raw = files.read_bytes(path_text)
    except FileNotFoundError:
        return {}
    except OSError as failure:
        raise SettingsRefused(
            f"cannot read {path_text} ({_reason(failure)}); nothing written. Give "
            "--settings-file a file you can read and write, in a directory that exists."
        ) from None
    return _parse_settings(raw, path_text)


def _cannot_write(path_text: str, failure: OSError) -> SettingsRefused:
    return SettingsRefused(
        f"cannot write {path_text} ({_reason(failure)}); nothing written. Give "
        "--settings-file a file you can read and write, in a directory that exists."
    )


def _publish_settings(path_text: str, settings: dict, files: Files) -> None:
    try:
        files.write_bytes(path_text, _serialize_settings(settings))
    except OSError as failure:
        raise _cannot_write(path_text, failure) from None


def _check_publishable(path_text: str, files: Files) -> None:
    """Refuses, as a publication would, a settings file this command could not replace, so a
    removal that leaves every present key still says so rather than reporting a state it could
    not have written."""
    try:
        files.check_publishable(path_text)
    except OSError as failure:
        raise _cannot_write(path_text, failure) from None


def _serialize_settings(settings: dict) -> bytes:
    """The wire form of a settings file: two-space indent, non-ASCII as typed, a final newline."""
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _check_prefix(prefix: str) -> str | None:
    """Why `--prefix` is refused, worded for the person who typed it; `None` when it is usable."""
    problem = stream_usage.check_prefix(prefix)
    if problem is stream_usage.PrefixProblem.EMPTY:
        return (
            "--prefix is empty; give the text your label keys start with, without the dot after "
            "it — for labels such as team.run_id, use --prefix team"
        )
    if problem is stream_usage.PrefixProblem.TRAILING_DOT:
        return (
            f"--prefix '{prefix}' ends with a dot; give it without the dot — for labels such as "
            "team.run_id, use --prefix team"
        )
    if problem is stream_usage.PrefixProblem.ROW_KEY:
        return (
            f"--prefix '{prefix}' is also the name of a field in each row of ka-telemetry agents "
            "--json output, so labels under it could not be read back; choose another prefix"
        )
    if any(character in _FORBIDDEN_CHARACTERS for character in prefix):
        return (
            f"--prefix '{prefix}' contains a character OTEL_RESOURCE_ATTRIBUTES cannot carry; "
            "give one without whitespace, a double quote, a comma, a semicolon, a backslash or "
            "an ="
        )
    return None


def _settings_file_text(value: str) -> str:
    if value == "":
        raise ValueError(value)
    return value


class _TurnOnParser(run_usage.CommandParser):
    """The command's parser: argparse's own refusals, then the combinations CP-223 refuses."""

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        parsed.mode = Mode.PRINT if parsed.print_settings else (
            Mode.REMOVE if parsed.remove else Mode.WRITE
        )
        problem = self._combination_problem(parsed)
        if problem is not None:
            self.error(problem)
        return parsed

    @staticmethod
    def _combination_problem(parsed: argparse.Namespace) -> str | None:
        if parsed.mode is Mode.PRINT:
            for flag, given in (
                ("--settings-file", parsed.settings_file is not None),
                ("--remove", parsed.remove),
                ("--replace", parsed.replace),
                ("--force", parsed.force),
            ):
                if given:
                    return (
                        f"--print cannot be given with {flag}, because --print touches no "
                        "settings file; drop one of the two"
                    )
            if parsed.label and parsed.prefix is None:
                return (
                    "--label needs --prefix, because each label is attached as PREFIX.KEY=VALUE; "
                    "add --prefix with the text your label keys start with, such as --prefix team"
                )
            return None if parsed.prefix is None else _check_prefix(parsed.prefix)
        if parsed.settings_file is None:
            return (
                "no --settings-file and no --print; give --settings-file PATH to write the five "
                "keys into a settings file, or --print to print them for one run"
            )
        for flag, given in (("--prefix", parsed.prefix is not None), ("--label", parsed.label)):
            if given:
                return (
                    f"{flag} works only with --print, because labels go on one run and never "
                    f"into a settings file; add --print, or drop {flag}"
                )
        return None


def _build_parser() -> _TurnOnParser:
    parser = _TurnOnParser(
        prog="ka-telemetry turn-on", next_step=_NEXT_STEP, description=_DESCRIPTION
    )
    parser.add_argument(
        "--settings-file",
        type=_settings_file_text,
        default=None,
        metavar="PATH",
        help="the Claude Code settings file to write the five keys into, or with --remove to "
        "remove them from; a file that does not exist is created when the keys are written",
    )
    parser.add_argument(
        "--endpoint-file",
        default=None,
        metavar="PATH",
        help="the file to read the receiver's endpoint from: its first line, an http:// or "
        "https:// address on this machine (default: ~/.claude/tools/otel-receiver/endpoint)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="when writing, overwrite a value you already set on one of the five keys, and "
        "record the key as written by this command, so a later --remove takes it out and does "
        "not put your value back. A value you set that already equals the one this command "
        "writes is left either way.",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="remove the five keys instead of writing them: a key this command wrote is "
        "removed, and a key holding a value you set is left and named on stdout. Whenever "
        "--remove changes the settings file, it also removes the record.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --remove, delete the five keys whatever their values, even with no published "
        "endpoint",
    )
    parser.add_argument(
        "--print",
        dest="print_settings",
        action="store_true",
        help="print the five keys for one run as the JSON that claude --settings accepts, "
        "instead of writing a settings file",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        metavar="PREFIX",
        help="with --print, the text every label key starts with: PREFIX.run_id, a new random id "
        "each time the command runs, is always attached, then PREFIX.instance, the name of the "
        "main checkout of the git repository the command runs in, when there is one, then each "
        "--label in the order given. Without this flag, no label is attached. Read the labels "
        "back with ka-telemetry agents --prefix PREFIX.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="with --prefix, attach the label PREFIX.KEY=VALUE to the run; repeat the flag for "
        "more labels. A label whose key holds whitespace, a double quote, a comma, a semicolon, a "
        "backslash or an = is not attached, and a line on stderr says so. A label with an empty "
        "value attaches nothing. A value is carried as typed unless it holds whitespace of any "
        "kind, including whitespace outside ASCII, a double quote, a comma, a semicolon, a "
        "backslash, an = or a %%. Each of those characters is written as %% followed by six "
        "hexadecimal digits, 0-9 and A-F, giving the character's Unicode code point, with zeroes "
        "in front to make six: %%000020 is a space, %%000009 a tab, %%00000A a newline, %%000022 a "
        "double quote, %%00002C a comma, %%00003B a semicolon, %%00005C a backslash, %%00003D an = "
        "and %%000025 a %%. So two words is carried as two%%000020words, and 50%% as "
        "50%%000025. To get the typed value back, read the carried value from left to right and "
        "replace each %% and the six digits after it with the character whose code point they "
        "give. Do this once: a %% that a replacement produced is not read again.",
    )
    return parser


def _write(args: argparse.Namespace, runtime: Runtime) -> int:
    settings = _read_settings(args.settings_file, runtime.files)
    endpoint = resolve_endpoint(args.endpoint_file, runtime)
    if endpoint.outcome is not EndpointOutcome.PUBLISHED:
        where = f" at {endpoint.path_text}" if endpoint.given else ""
        print(
            f"no published telemetry endpoint{where} \u2014 machine has no receiver, "
            "nothing written"
        )
        return 0
    entry = telemetry_entry(endpoint.value)
    env = settings.get("env") or {}
    ours = _written_by_the_turn_on(settings, env)
    other_valued = conflicting_keys(env, entry)
    written = [
        key
        for key in _KEY_ORDER
        if key not in env or key in ours or (key in other_valued and args.replace)
    ]
    left = [key for key in _KEY_ORDER if key not in written]
    if written:
        recorded = {key: entry[key] for key in written}
        merged = _with_record(merge_entry(settings, recorded), recorded)
        _publish_settings(args.settings_file, merged, runtime.files)
    else:
        _check_publishable(args.settings_file, runtime.files)
    for line in _status_lines(written, left, [key for key in left if key in other_valued]):
        print(line)
    return 0


def _remove(args: argparse.Namespace, runtime: Runtime) -> int:
    settings = _read_settings(args.settings_file, runtime.files)
    env = settings.get("env") or {}
    present = [key for key in _KEY_ORDER if key in env]
    if not present:
        print("no telemetry keys to remove — nothing written")
        return 0
    if args.force:
        _publish_settings(
            args.settings_file, _without_records(remove_keys(settings, present)), runtime.files
        )
        return 0
    endpoint = resolve_endpoint(args.endpoint_file, runtime)
    if endpoint.outcome is not EndpointOutcome.PUBLISHED:
        raise SettingsRefused(
            "no published telemetry endpoint — cannot tell this command's entry from values you "
            "set yourself; nothing written. Pass --force to delete the five keys anyway."
        )
    ours = _written_by_the_turn_on(settings, env)
    doomed = [key for key in present if key in ours]
    left = [key for key in present if key not in doomed]
    if doomed:
        _publish_settings(
            args.settings_file, _without_records(remove_keys(settings, doomed)), runtime.files
        )
    elif _recognized_slots(settings):
        # Every present key is the user's, so only the stale record goes: a key later set back to
        # a recorded value is then not claimed by metadata that no longer describes the file.
        _publish_settings(args.settings_file, _without_records(settings), runtime.files)
    else:
        _check_publishable(args.settings_file, runtime.files)
    if left:
        print(
            f"left {', '.join(left)} \u2014 no record shows this command wrote the value now set. "
            "Pass --force to remove what was left."
        )
    return 0


_NOTICE_INDENT = " " * 10


def _unusable_endpoint_notice(endpoint: Endpoint) -> str:
    """The one stderr line that says why no endpoint is used, one whole sentence per reason."""
    path = endpoint.path_text
    if endpoint.outcome is EndpointOutcome.ABSENT:
        return f"{_NOTICE_INDENT}(no published telemetry endpoint at {path} — run uninstrumented)"
    if endpoint.outcome is EndpointOutcome.MALFORMED:
        return (
            f"{_NOTICE_INDENT}(telemetry endpoint at {path} is unreadable or malformed — run "
            "uninstrumented)"
        )
    return (
        f"{_NOTICE_INDENT}(telemetry endpoint at {path} is not on this machine (loopback only) "
        "— run uninstrumented)"
    )


def _is_carriable(text: str) -> bool:
    return not any(character in _FORBIDDEN_CHARACTERS for character in text)


def _encode_label_value(value: str) -> str:
    """`value` as an `OTEL_RESOURCE_ATTRIBUTES` pair carries it: each whitespace character and each
    of `"`, `,`, `;`, `\\`, `=` and `%` becomes `%` and six uppercase hexadecimal digits of its code
    point; every other character stays as typed. Reading left to right and replacing each `%` and
    its six digits with that character, once, gives the value back."""
    return "".join(_escape_value_character(character) for character in value)


def _escape_value_character(character: str) -> str:
    if character.isspace() or character in _ESCAPED_IN_VALUES:
        return f"%{ord(character):06X}"
    return character


def _refusal(message: str) -> str:
    return f"{_NOTICE_INDENT}({message} — not attached)"


def _instance_pair(prefix: str, runtime: Runtime, notices: list[str]) -> list[str]:
    """The `PREFIX.instance` pair when there is a name to attach, a notice when the name cannot
    be carried, nothing when git has no answer."""
    name = _instance_name(runtime)
    if name is None:
        return []
    if not _is_carriable(name):
        notices.append(
            _refusal(f"{prefix}.instance '{name}' contains a character {_CANNOT_CARRY}")
        )
        return []
    return [f"{prefix}.instance={name}"]


_COMMAND_KEYS = ("run_id", "instance")


def _label_pair(prefix: str, argument: str, notices: list[str]) -> list[str]:
    """The pair one `--label` attaches, its value encoded: none when it is refused (its notice is
    added) or its value is empty (silently)."""
    key, equals, value = argument.partition("=")
    if not equals:
        notices.append(_refusal(f"--label '{argument}' has no '='; give it as KEY=VALUE"))
    elif not key:
        notices.append(
            _refusal(
                f"label key '' in --label '{argument}' is empty and cannot be carried; give it "
                "as KEY=VALUE"
            )
        )
    elif not _is_carriable(key):
        notices.append(
            _refusal(
                f"label key '{key}' contains a character {_CANNOT_CARRY}; remove it from the key"
            )
        )
    elif key in _COMMAND_KEYS:
        notices.append(
            _refusal(f"{prefix}.{key} is set by this command itself; give your label another key")
        )
    elif not value:
        pass
    else:
        return [f"{prefix}.{key}={_encode_label_value(value)}"]
    return []


def _instance_name(runtime: Runtime) -> str | None:
    """The name of the main checkout of the repository the command runs in: the parent of git's
    common directory, so a linked worktree answers with the checkout it was cut from."""
    common_dir = runtime.processes.git_common_dir(runtime.cwd)
    if not common_dir:
        return None
    return Path(common_dir).parent.name or None


def _resource_attributes(args: argparse.Namespace, runtime: Runtime) -> tuple[str, list[str]]:
    """The attributes value for `--prefix` and the notices for what could not be attached,
    instance first, then each label in the order given."""
    notices: list[str] = []
    pairs = [f"{args.prefix}.run_id={runtime.new_run_id()}"]
    pairs += _instance_pair(args.prefix, runtime, notices)
    for label in args.label or ():
        pairs += _label_pair(args.prefix, label, notices)
    return ",".join(pairs), notices


def _print(args: argparse.Namespace, runtime: Runtime) -> int:
    endpoint = resolve_endpoint(args.endpoint_file, runtime)
    if endpoint.outcome is not EndpointOutcome.PUBLISHED:
        print(_unusable_endpoint_notice(endpoint), file=sys.stderr)
        return 0
    env = telemetry_entry(endpoint.value)
    summary = "OTLP"
    if args.prefix is not None:
        summary, notices = _resource_attributes(args, runtime)
        env["OTEL_RESOURCE_ATTRIBUTES"] = summary
        for notice in notices:
            print(notice, file=sys.stderr)
    print(json.dumps({"env": env}, separators=(",", ":")))
    print(f"{_NOTICE_INDENT}Telemetry:    {summary} → {endpoint.value}", file=sys.stderr)
    return 0


def run(argv: list[str], *, runtime: Runtime) -> int:
    """One invocation, in process. Usage errors end here before the runtime is used."""
    args = _build_parser().parse_args(argv)
    try:
        if args.mode is Mode.WRITE:
            return _write(args, runtime)
        if args.mode is Mode.REMOVE:
            return _remove(args, runtime)
        return _print(args, runtime)
    except SettingsRefused as refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1
    return 0


def build_runtime() -> Runtime:
    """The composition root: the only place the concrete collaborators are made."""
    return Runtime(
        files=Files(),
        processes=Processes(),
        new_run_id=lambda: str(uuid.uuid4()),
        home=Path.home(),
        cwd=Path.cwd(),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return run(arguments, runtime=build_runtime())
