"""The package's turn-on held to dev's stored output, byte for byte.

`tests/turn_on_parity/manifest.json` holds each case's inputs and the outputs dev's own writer and
library produced at the commit it names. This module replays every case through the package, from
a directory outside any git repository and with the same relative paths, and compares raw bytes:
stdout, stderr, exit status and the settings file after. It reads stored data only; it never runs
dev's code and never writes to what is stored. A mismatch is a defect in the package, never a
reason to regenerate the evidence.

Two things differ by design and are the only changes made before comparing. dev derived an `arm`
pair the package does not, so dev's `kensai.arm=fixed,` is removed from its expected attributes
and summary. And a run id is new on every run, so each `kensai.run_id=` value that is a version-4
UUID is replaced by a marker, on both sides, once the JSON's run id and the summary's are found to
agree. Nothing else is normalised: no JSON is re-serialised, and whitespace, escaping, paths,
notices and ordering are compared as they are.
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest

from ka_telemetry import turn_on

DATA_DIR = Path(__file__).resolve().parent / "turn_on_parity"
MANIFEST = DATA_DIR / "manifest.json"
DEV_COMMIT = "3b5746f91d70763ea2115f896c1d694b7ab77e89"
PREFIX = "kensai"
ARM_PAIR = "kensai.arm=fixed,"
RUN_ID_MARKER = "<UUID4>"
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_RUN_ID_PAIR = re.compile(rf"{PREFIX}\.run_id=({_UUID4})")
_MACHINE_PATH = re.compile(r"(?:^|[\s'\"(])/[\w.-]+(?:/[\w.-]+)+")
_MACHINE_PATH_PARTS = ("/Users/", "/private/", "/tmp/", "/var/", "/home/")

# One case of each group CP-241 names, by the id the manifest gives it.
MANDATORY_IDS = (
    "print-no-labels",
    "print-two-plain-labels",
    "print-a-value-with-a-space",
    "print-a-value-with-a-comma",
    "print-a-value-with-a-semicolon",
    "print-a-value-with-a-backslash",
    "print-a-value-with-an-equals-sign",
    "print-a-value-with-a-double-quote",
    "print-a-non-ascii-value",
    "print-an-empty-value",
    "print-with-an-absent-endpoint-file",
    "print-with-a-malformed-endpoint-file",
    "print-with-a-non-loopback-endpoint-file",
    "write-into-an-absent-settings-file",
    "write-keeps-other-keys-and-a-non-ascii-value",
    "write-over-a-user-value-without-replace",
    "write-over-a-user-value-with-replace",
    "write-with-no-endpoint-and-no-settings-file",
    "write-with-no-endpoint-and-a-settings-file",
    "write-again-over-its-own-output",
    "remove-after-a-write",
    "remove-where-a-key-holds-the-users-value",
    "remove-with-no-endpoint-without-force",
    "remove-with-no-endpoint-with-force",
    "remove-with-nothing-to-remove",
    "write-into-invalid-json",
    "write-into-a-json-array",
)


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one invocation did: its two channels, its status and the settings file after."""

    stdout: str
    stderr: str
    status: int
    settings_after: dict | None


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


CASES = _manifest()["cases"]
WRITE_REMOVE_CASES = [case for case in CASES if case["kind"] == "write-or-remove"]
PRINT_CASES = [case for case in CASES if case["kind"] == "print"]


def _stored_bytes(entry: dict) -> bytes:
    return entry["text"].encode("utf-8") if "text" in entry else base64.b64decode(entry["b64"])


def _stored_form(content: bytes) -> dict:
    try:
        return {"text": content.decode("utf-8")}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(content).decode("ascii")}


def _lay_out(case: dict, directory: Path) -> None:
    for name, entry in case["files_before"].items():
        (directory / name).write_bytes(_stored_bytes(entry))


def _argv(case: dict) -> list[str]:
    if case["kind"] == "write-or-remove":
        return list(case["argv"])
    argv = ["--print", "--endpoint-file", case["endpoint_file"], "--prefix", PREFIX]
    for label in case["labels"]:
        argv += ["--label", label]
    return argv


def _run_package(case: dict, directory: Path, monkeypatch, capsys) -> Outcome:
    """The package, run in `directory` with the case's own relative paths."""
    monkeypatch.chdir(directory)
    runtime = dataclasses.replace(turn_on.build_runtime(), home=directory / "no-home")
    status = turn_on.run(_argv(case), runtime=runtime)
    captured = capsys.readouterr()
    settings = directory / "s.json"
    after = _stored_form(settings.read_bytes()) if settings.exists() else None
    return Outcome(captured.out, captured.err, status, after)


def _expected(case: dict) -> Outcome:
    """dev's stored outcome, in the form the package's is read in."""
    if case["kind"] == "write-or-remove":
        return Outcome(
            case["stdout"], case["stderr"], case["returncode"], case["settings_after"]
        )
    stdout = "" if case["settings"] is None else case["settings"] + "\n"
    return Outcome(stdout, case["printed"], 0, None)


def _run_ids(text: str) -> list[str]:
    return _RUN_ID_PAIR.findall(text)


def _mask_run_ids(text: str) -> str:
    return _RUN_ID_PAIR.sub(f"{PREFIX}.run_id={RUN_ID_MARKER}", text)


def _restore(masked: str, run_ids: list[str]) -> str:
    for run_id in run_ids:
        masked = masked.replace(RUN_ID_MARKER, run_id, 1)
    return masked


def _check_run_ids_agree(outcome: Outcome, side: str) -> None:
    """The JSON's run id and the summary's are one value, on this side of the comparison."""
    in_json, in_summary = _run_ids(outcome.stdout), _run_ids(outcome.stderr)
    if in_json or in_summary:
        assert in_json == in_summary, (side, in_json, in_summary)
        assert len(in_json) == 1, (side, in_json)
        assert str(uuid.UUID(in_json[0])) == in_json[0]


def _without_arm(text: str) -> str:
    return text.replace(ARM_PAIR, "", 1)


def _normalise(outcome: Outcome, *, expected: bool) -> Outcome:
    """The only changes made before comparing: dev's arm pair out of its expected text, and each
    validated run id replaced by the marker."""
    stdout, stderr = outcome.stdout, outcome.stderr
    if expected:
        stdout, stderr = _without_arm(stdout), _without_arm(stderr)
    return dataclasses.replace(
        outcome, stdout=_mask_run_ids(stdout), stderr=_mask_run_ids(stderr)
    )


def _differences(expected: Outcome, actual: Outcome) -> str:
    """Empty when every byte matches; otherwise a readable account of each channel that does not."""
    report: list[str] = []
    for channel in ("stdout", "stderr"):
        want, got = getattr(expected, channel), getattr(actual, channel)
        if want.encode("utf-8") != got.encode("utf-8"):
            report.append(f"{channel} differs:")
            report += difflib.unified_diff(
                want.splitlines(keepends=True),
                got.splitlines(keepends=True),
                "dev (stored)",
                "package",
            )
            report.append(f"  stored : {want!r}\n  package: {got!r}")
    if expected.status != actual.status:
        report.append(f"exit status: stored {expected.status}, package {actual.status}")
    if expected.settings_after != actual.settings_after:
        report.append(
            f"settings file after: stored {expected.settings_after!r}, "
            f"package {actual.settings_after!r}"
        )
    return "\n".join(report)


_CHANNELS = ("stdout", "stderr", "status", "settings_after")
_RULE = re.compile(r"CP-\d+(?:[/\u2013]CP-\d+)*")
_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"


@dataclasses.dataclass(frozen=True)
class Departure:
    """One declared departure from a stored outcome (CP-281): the case's `id`, the CP rule that
    departs it, and the outcome expected instead on each channel the rule reaches. A channel the
    declaration does not name is compared with the stored one. `expected` holds a value for each
    channel it names, in the comparison's own form (stdout and stderr normalised as `_normalise`
    leaves them, settings as stored); there is no value that means "any". `departing_pairs` is the
    `OTEL_RESOURCE_ATTRIBUTES` keys a print case's rule changes; every other pair is compared
    too."""

    case_id: str
    rule: str
    expected: dict
    departing_pairs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _RULE.fullmatch(self.rule) is None:
            raise ValueError(f"{self.case_id}: {self.rule!r} does not name a CP rule")
        if not self.expected:
            raise ValueError(f"{self.case_id}: a declaration states at least one channel")
        for channel, value in self.expected.items():
            if channel not in _CHANNELS or not _is_outcome_value(channel, value):
                raise ValueError(f"{self.case_id}: no usable expected value for {channel!r}")


def _is_outcome_value(channel: str, value: object) -> bool:
    if channel in ("stdout", "stderr"):
        return isinstance(value, str)
    if channel == "status":
        return isinstance(value, int) and not isinstance(value, bool)
    return value is None or (
        isinstance(value, dict) and set(value) in ({"text"}, {"b64"})
    )  # the stored form of a settings file, or its absence


def _declared(*departures: Departure) -> dict[str, Departure]:
    """The declarations keyed by case id; a second declaration for one case is refused."""
    declared: dict[str, Departure] = {}
    for departure in departures:
        if departure.case_id in declared:
            raise ValueError(f"{departure.case_id} is declared twice")
        declared[departure.case_id] = departure
    return declared


def _declaration_problems(departures: dict[str, Departure], cases: list[dict]) -> list[str]:
    """Each declaration that names an id the manifest does not hold, so a typo hides no case."""
    held = {case["id"] for case in cases}
    return [
        f"declaration names {case_id!r}, which the manifest does not hold"
        for case_id in departures
        if case_id not in held
    ]


_ENDPOINT = "http://localhost:4318"
_PRINTED_KEYS = (
    '"CLAUDE_CODE_ENABLE_TELEMETRY":"1","OTEL_METRICS_EXPORTER":"otlp",'
    '"OTEL_LOGS_EXPORTER":"otlp","OTEL_EXPORTER_OTLP_PROTOCOL":"http/json",'
    f'"OTEL_EXPORTER_OTLP_ENDPOINT":"{_ENDPOINT}"'
)


def _carried_label_departure(case_id: str, key: str, carried: str) -> Departure:
    """CP-270: the stored case refused its value and attached `skill=s` alone. Now the value is
    carried: stdout and the summary gain `kensai.<key>=<carried>` after `kensai.skill=s`, and the
    refusal line leaves stderr. Exit and settings after are the stored ones."""
    attributes = f"{PREFIX}.run_id={RUN_ID_MARKER},{PREFIX}.skill=s,{PREFIX}.{key}={carried}"
    return Departure(
        case_id,
        "CP-270",
        {
            "stdout": f'{{"env":{{{_PRINTED_KEYS},"OTEL_RESOURCE_ATTRIBUTES":"{attributes}"}}}}\n',
            "stderr": f"          Telemetry:    {attributes} \u2192 {_ENDPOINT}\n",
        },
        departing_pairs=(f"{PREFIX}.{key}",),
    )


_FIVE_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
_RECORD_SLOT = "_ka_telemetry_turn_on"
_WRITTEN_VALUES = dict(
    zip(_FIVE_KEYS, ("1", "otlp", "otlp", "http/json", _ENDPOINT), strict=True)
)
_ALL_WRITTEN = "wrote " + ", ".join(_FIVE_KEYS) + "\n"
_ALREADY_THE_VALUE = "\u2014 already set to the value this command writes"
_FOUR_WRITTEN = "wrote " + ", ".join(_FIVE_KEYS[:4]) + "\n"


def _settings_text(settings: dict) -> dict:
    """A settings object in the stored form of a settings file the command wrote."""
    return {"text": json.dumps(settings, indent=2, ensure_ascii=False) + "\n"}


def _record_of(keys: tuple[str, ...]) -> dict:
    written = {key: _WRITTEN_VALUES[key] for key in keys}
    return {"format": "ka-telemetry.turn-on/1", "written": written}


def _stored_settings_after(case_id: str) -> dict:
    case = next(case for case in CASES if case["id"] == case_id)
    return json.loads(case["settings_after"]["text"])


def _written_with_a_record_departure(case_id: str) -> Departure:
    """CP-273, CP-279: stdout names all five keys as written, and the settings file after is the
    stored one plus the record of all five, as the last top-level key. Exit and stderr are the
    stored ones."""
    settings = _stored_settings_after(case_id)
    settings[_RECORD_SLOT] = _record_of(_FIVE_KEYS)
    expected = {"stdout": _ALL_WRITTEN, "settings_after": _settings_text(settings)}
    return Departure(case_id, "CP-273/CP-279", expected)


def _removal_left_departure(case_id: str, keys: tuple[str, ...]) -> Departure:
    """CP-274, CP-275: the stored file holds the keys with no record, so each is the user's. The
    removal leaves them all, names them in one stdout line, and the settings file after is the
    complete settings file before. Exit and stderr are the stored ones."""
    case = next(case for case in CASES if case["id"] == case_id)
    line = (
        f"left {', '.join(keys)} \u2014 no record shows this command wrote the value now set. "
        "Pass --force to remove what was left.\n"
    )
    return Departure(
        case_id,
        "CP-274/CP-275",
        {"stdout": line, "settings_after": case["files_before"]["s.json"]},
    )


# Declared by the task that makes the package depart: each adds its cases here.
DEPARTURES: dict[str, Departure] = _declared(
    _carried_label_departure("print-a-value-with-a-space", "model", "a%000020b"),
    _carried_label_departure("print-a-value-with-a-comma", "model", "a%00002Cb"),
    _carried_label_departure("print-a-value-with-a-semicolon", "model", "a%00003Bb"),
    _carried_label_departure("print-a-value-with-a-backslash", "model", "a%00005Cb"),
    _carried_label_departure("print-a-value-with-an-equals-sign", "model", "a%00003Db"),
    _carried_label_departure("print-a-value-with-a-double-quote", "effort", "x%000022y"),
    _written_with_a_record_departure("write-into-an-absent-settings-file"),
    _written_with_a_record_departure("write-keeps-other-keys-and-a-non-ascii-value"),
    _written_with_a_record_departure("write-over-a-user-value-with-replace"),
    _written_with_a_record_departure("write-into-an-env-of-null"),
    Departure(
        "write-again-over-its-own-output",
        "CP-274/CP-278/CP-279",
        {"stdout": f"left {', '.join(_FIVE_KEYS)} {_ALREADY_THE_VALUE}\n"},
    ),
    Departure(
        "write-over-a-user-value-without-replace",
        "CP-278/CP-280",
        {
            "stdout": _FOUR_WRITTEN
            + "left OTEL_EXPORTER_OTLP_ENDPOINT \u2014 already set. Pass --replace to overwrite "
            "OTEL_EXPORTER_OTLP_ENDPOINT with the value this command writes.\n",
            "stderr": "",
            "status": 0,
            "settings_after": _settings_text(
                {
                    "env": {
                        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example",
                        **{key: _WRITTEN_VALUES[key] for key in _FIVE_KEYS[:4]},
                    },
                    _RECORD_SLOT: _record_of(_FIVE_KEYS[:4]),
                }
            ),
        },
    ),
    _removal_left_departure("remove-after-a-write", _FIVE_KEYS),
    _removal_left_departure("remove-drops-the-emptied-env", _FIVE_KEYS),
    _removal_left_departure(
        "remove-where-a-key-holds-the-users-value",
        ("CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    ),
)

# CP-281's list of the cases the package departs from, spelled out apart from the declarations so
# that a case declared by mistake, or left undeclared, cannot become an exception unseen.
CP_281_DEPARTING_IDS = frozenset(
    {
        "print-a-value-with-a-space",
        "print-a-value-with-a-comma",
        "print-a-value-with-a-semicolon",
        "print-a-value-with-a-backslash",
        "print-a-value-with-an-equals-sign",
        "print-a-value-with-a-double-quote",
        "write-over-a-user-value-without-replace",
        "write-into-an-absent-settings-file",
        "write-keeps-other-keys-and-a-non-ascii-value",
        "write-again-over-its-own-output",
        "write-over-a-user-value-with-replace",
        "write-into-an-env-of-null",
        "remove-after-a-write",
        "remove-drops-the-emptied-env",
        "remove-where-a-key-holds-the-users-value",
    }
)


def _print_remainder_differences(
    stored: Outcome, actual: Outcome, departure: Departure
) -> list[str]:
    """For a print case that departs: every `env` key and every attributes pair the rule does not
    reach, compared with the stored one."""
    try:
        want, got = json.loads(stored.stdout)["env"], json.loads(actual.stdout)["env"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ["stdout is not the printed settings JSON on one side"]
    report = []
    other_keys = [key for key in (*want, *got) if key != _RESOURCE_ATTRIBUTES]
    for key in dict.fromkeys(other_keys):
        if want.get(key) != got.get(key) or (key in want) != (key in got):
            report.append(f"env key {key}: stored {want.get(key)!r}, package {got.get(key)!r}")
    if list(want) != list(got):
        report.append(f"env keys and their order: stored {list(want)}, package {list(got)}")

    def kept(env: dict) -> list[str]:
        pairs = env.get(_RESOURCE_ATTRIBUTES, "").split(",")
        return [pair for pair in pairs if pair.partition("=")[0] not in departure.departing_pairs]

    if kept(want) != kept(got):
        report.append(
            f"attribute pairs the rule does not reach: stored {kept(want)}, package {kept(got)}"
        )
    return report


def _compare(case: dict, actual: Outcome, departures: dict[str, Departure] | None = None) -> str:
    """Empty when the package's outcome is the stored one, or the stored one with the case's
    declared departure applied; otherwise a report naming the case."""
    departures = DEPARTURES if departures is None else departures
    expected = _expected(case)
    _check_run_ids_agree(expected, "dev")
    _check_run_ids_agree(actual, "package")
    stored = _normalise(expected, expected=True)
    got = _normalise(actual, expected=False)
    departure = departures.get(case["id"])
    if departure is None:
        report = _differences(stored, got)
        heading = f"{case['id']} does not match its stored outcome:"
    else:
        report = _differences(dataclasses.replace(stored, **departure.expected), got)
        heading = f"{case['id']} departs under {departure.rule}, and does not match:"
        if case["kind"] == "print":
            remainder = _print_remainder_differences(stored, got, departure)
            report = "\n".join([report, *remainder]).strip()
    return f"{heading}\n{report}" if report else ""


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A new directory outside any git repository, as every stored case ran in."""
    directory = tmp_path / "case"
    directory.mkdir()
    if turn_on.Processes().git_common_dir(directory) is not None:
        pytest.fail(f"{directory} is inside a git repository; the parity cases run outside one")
    return directory


def _replay(case: dict, scratch: Path, monkeypatch, capsys) -> Outcome:
    _lay_out(case, scratch)
    return _run_package(case, scratch, monkeypatch, capsys)


class TestAgainstDev:
    @pytest.mark.parametrize("case", WRITE_REMOVE_CASES, ids=lambda case: case["id"])
    def test_every_stored_write_and_remove_case_matches_dev(
        self, case, scratch, monkeypatch, capsys
    ):
        actual = _replay(case, scratch, monkeypatch, capsys)
        assert _compare(case, actual) == ""

    @pytest.mark.parametrize("case", PRINT_CASES, ids=lambda case: case["id"])
    def test_every_stored_print_case_matches_dev_after_the_arm_and_run_id_handling(
        self, case, scratch, monkeypatch, capsys
    ):
        actual = _replay(case, scratch, monkeypatch, capsys)
        assert actual.status == 0
        assert _compare(case, actual) == ""

    @pytest.mark.parametrize("case", PRINT_CASES, ids=lambda case: case["id"])
    def test_the_json_and_summary_run_ids_agree_on_each_side_before_comparing(
        self, case, scratch, monkeypatch, capsys
    ):
        actual = _replay(case, scratch, monkeypatch, capsys)
        for side, outcome in (("dev", _expected(case)), ("package", actual)):
            _check_run_ids_agree(outcome, side)
        usable = case["settings"] is not None
        assert bool(_run_ids(actual.stdout)) == usable

    @pytest.mark.parametrize("case", PRINT_CASES, ids=lambda case: case["id"])
    def test_only_the_arm_pair_and_validated_uuids_are_changed_before_comparing(self, case):
        expected = _expected(case)
        normalised = _normalise(expected, expected=True)
        for channel in ("stdout", "stderr"):
            original = getattr(expected, channel)
            masked = getattr(normalised, channel)
            restored = _restore(masked, _run_ids(original))
            if ARM_PAIR in original:
                restored = restored.replace(
                    f"{PREFIX}.run_id=", f"{ARM_PAIR}{PREFIX}.run_id=", 1
                )
            assert restored == original, channel

    def test_a_run_id_that_is_not_a_version_4_uuid_is_not_masked(self):
        text = f"{PREFIX}.run_id=not-a-uuid,{PREFIX}.run_id=00000000-0000-1000-8000-000000000000"
        assert _mask_run_ids(text) == text


class TestTheComparatorFails:
    def _case_and_outcome(self, scratch, monkeypatch, capsys):
        case = next(case for case in PRINT_CASES if case["id"] == "print-two-plain-labels")
        return case, _replay(case, scratch, monkeypatch, capsys)

    def test_a_one_character_notice_difference_fails_and_shows_the_difference(
        self, scratch, monkeypatch, capsys
    ):
        case, actual = self._case_and_outcome(scratch, monkeypatch, capsys)
        assert _compare(case, actual) == ""
        misspelt = actual.stderr.replace("Telemetry", "Telemetri")
        changed = dataclasses.replace(actual, stderr=misspelt)
        report = _compare(case, changed)
        assert report != ""
        assert "stderr differs" in report and "Telemetri" in report and "Telemetry" in report

    def test_a_whitespace_or_json_escape_difference_fails(self, scratch, monkeypatch, capsys):
        case, actual = self._case_and_outcome(scratch, monkeypatch, capsys)
        spaced = json.dumps(json.loads(actual.stdout)) + "\n"
        assert spaced != actual.stdout
        assert "stdout differs" in _compare(case, dataclasses.replace(actual, stdout=spaced))
        escaped_case = next(case for case in PRINT_CASES if case["id"] == "print-a-non-ascii-value")
        escaped = _replay(escaped_case, scratch, monkeypatch, capsys)
        assert "\\u00e9" in escaped.stdout
        unescaped = dataclasses.replace(escaped, stdout=escaped.stdout.replace("\\u00e9", "é"))
        assert "stdout differs" in _compare(escaped_case, unescaped)

    def test_a_different_exit_status_or_settings_file_fails(self, scratch, monkeypatch, capsys):
        case = next(
            case
            for case in WRITE_REMOVE_CASES
            if case["id"] == "remove-with-no-endpoint-with-force"
        )
        assert case["id"] not in DEPARTURES
        actual = _replay(case, scratch, monkeypatch, capsys)
        assert _compare(case, actual) == ""
        assert "exit status" in _compare(case, dataclasses.replace(actual, status=1))
        assert "settings file after" in _compare(
            case, dataclasses.replace(actual, settings_after={"text": '{"x": 1}\n'})
        )


class TestDeclaredDepartures:
    """CP-281: a case departs from its stored outcome only by a declaration naming the case's id
    and the CP rule, stating the outcome expected on each channel the rule reaches; every other
    channel is still compared with the stored one. Outcomes here are synthetic (stored outcomes with
    one channel changed), not package runs."""

    @staticmethod
    def _case(case_id: str) -> dict:
        return next(case for case in CASES if case["id"] == case_id)

    @staticmethod
    def _declared(case_id: str, **expected) -> dict:
        return _declared(Departure(case_id, "CP-275", expected))

    def test_an_undeclared_case_that_differs_from_its_stored_outcome_fails(self):
        case = self._case("remove-after-a-write")
        changed = dataclasses.replace(_expected(case), stdout="left everything\n")

        report = _compare(case, changed, {})

        assert "remove-after-a-write" in report
        assert "stdout differs" in report and "left everything" in report

    def test_a_declared_departure_passes_when_declared_channels_hold_and_the_rest_match(self):
        case = self._case("remove-after-a-write")
        stored = _expected(case)
        departures = self._declared(
            case["id"], stdout="left a — b\n", settings_after=stored.settings_after
        )
        departed = dataclasses.replace(stored, stdout="left a — b\n")

        assert _compare(case, departed, departures) == ""

    @pytest.mark.parametrize(
        ("channel", "declared", "differing"),
        [
            ("stdout", "left a\n", "left b\n"),
            ("stderr", "warned\n", "warned!\n"),
            ("status", 0, 1),
            ("settings_after", {"text": "{}\n"}, {"text": "{ }\n"}),
        ],
    )
    def test_a_declared_departure_fails_when_a_declared_channel_differs_from_the_declared_outcome(
        self, channel, declared, differing
    ):
        case = self._case("remove-after-a-write")
        departures = self._declared(case["id"], **{channel: declared})
        stored = _expected(case)

        assert _compare(case, dataclasses.replace(stored, **{channel: declared}), departures) == ""
        report = _compare(case, dataclasses.replace(stored, **{channel: differing}), departures)

        assert report != ""
        assert case["id"] in report

    def test_a_declared_departure_fails_when_an_undeclared_channel_differs_from_the_stored_one(
        self,
    ):
        case = self._case("remove-after-a-write")
        stored = _expected(case)
        departures = self._declared(case["id"], stdout="left a\n")
        actual = dataclasses.replace(stored, stdout="left a\n", stderr="x")

        report = _compare(case, actual, departures)

        assert "stderr differs" in report
        assert "stdout differs" not in report

    def test_a_declared_print_departure_compares_every_other_pair_and_env_key_to_the_stored_ones(
        self,
    ):
        case = self._case("print-a-value-with-a-space")
        stored = _normalise(_expected(case), expected=True)
        carried = json.loads(stored.stdout)
        carried["env"]["OTEL_RESOURCE_ATTRIBUTES"] += f",{PREFIX}.model=a%000020b"
        departing = (f"{PREFIX}.model",)

        def departed(alter) -> Outcome:
            env = json.loads(json.dumps(carried))
            alter(env["env"])
            stdout = json.dumps(env, separators=(",", ":")) + "\n"
            return Outcome(stdout, stored.stderr, 0, None)

        def declare(outcome: Outcome) -> dict:
            return _declared(
                Departure(
                    case["id"], "CP-270", {"stdout": outcome.stdout}, departing_pairs=departing
                )
            )

        intact = departed(lambda env: None)
        other_pair = departed(
            lambda env: env.update(
                OTEL_RESOURCE_ATTRIBUTES=env["OTEL_RESOURCE_ATTRIBUTES"].replace(
                    f"{PREFIX}.skill=s", f"{PREFIX}.skill=t"
                )
            )
        )
        other_key = departed(lambda env: env.update(OTEL_METRICS_EXPORTER="none"))

        assert _compare(case, intact, declare(intact)) == ""
        # The declaration is written to agree with each altered outcome, so only the comparison
        # of everything else with the stored one can refuse it.
        assert _compare(case, other_pair, declare(other_pair)) != ""
        assert _compare(case, other_key, declare(other_key)) != ""

    def test_a_declaration_for_an_id_the_manifest_does_not_hold_fails(self):
        typo = self._declared("remove-after-a-wirte", stdout="x")
        real = self._declared("remove-after-a-write", stdout="x")

        assert "remove-after-a-wirte" in "\n".join(_declaration_problems(typo, CASES))
        assert _declaration_problems(real, CASES) == []

    def test_the_declarations_the_suite_carries_name_only_ids_the_manifest_holds(self):
        assert _declaration_problems(DEPARTURES, CASES) == []

    def test_the_declared_departure_ids_are_exactly_cp_281s_list(self):
        assert len(CP_281_DEPARTING_IDS) == 15
        assert set(DEPARTURES) == CP_281_DEPARTING_IDS

    def test_every_declared_removal_departure_leaves_the_stored_file_before_as_the_file_after(self):
        for case_id in (
            "remove-after-a-write",
            "remove-drops-the-emptied-env",
            "remove-where-a-key-holds-the-users-value",
        ):
            case = next(case for case in CASES if case["id"] == case_id)
            expected = DEPARTURES[case_id].expected
            assert expected["settings_after"] == case["files_before"]["s.json"]
            assert set(expected) == {"stdout", "settings_after"}

    @pytest.mark.parametrize("rule", ["", "  ", "rule 275", "CP-"])
    def test_a_declaration_names_a_cp_rule(self, rule):
        with pytest.raises(ValueError, match="CP rule"):
            Departure("remove-after-a-write", rule, {"stdout": "x"})

    @pytest.mark.parametrize(
        "expected",
        [{}, {"stdout": None}, {"stdout": ...}, {"status": "0"}, {"colour": "red"}],
    )
    def test_a_declaration_states_a_value_for_each_channel_it_names_and_never_any(self, expected):
        with pytest.raises(ValueError):
            Departure("remove-after-a-write", "CP-275", expected)

    def test_two_declarations_for_one_case_are_refused(self):
        first = Departure("remove-after-a-write", "CP-275", {"stdout": "x"})
        with pytest.raises(ValueError, match="remove-after-a-write"):
            _declared(first, first)


class TestTheStoredEvidence:
    def test_the_comparator_leaves_the_stored_evidence_byte_identical(
        self, scratch, monkeypatch, capsys
    ):
        before = _digests()
        for case in CASES:
            for entry in scratch.iterdir():
                entry.unlink()
            _replay(case, scratch, monkeypatch, capsys)
        after = _digests()
        assert after == before

    def test_the_manifest_names_the_dev_commit_and_every_mandatory_case(self):
        manifest = _manifest()
        assert manifest["dev_commit"] == DEV_COMMIT
        assert manifest["dev_sources"] == [
            "core/tools/apply_telemetry_env.py",
            "core/tools/instrument_run.sh",
        ]
        ids = [case["id"] for case in CASES]
        assert len(ids) == len(set(ids))
        assert set(MANDATORY_IDS) <= set(ids)
        for case in WRITE_REMOVE_CASES:
            assert {"argv", "environment", "files_before", "stdout", "stderr", "returncode"} <= set(
                case
            )
            assert "settings_after" in case
        for case in PRINT_CASES:
            assert {"labels", "endpoint_file", "environment", "files_before", "printed"} <= set(
                case
            )
            assert "settings" in case

    def test_absence_is_recorded_apart_from_an_empty_file_and_an_empty_string(self):
        by_id = {case["id"]: case for case in CASES}
        absent = by_id["remove-from-an-absent-settings-file"]
        assert absent["settings_after"] is None and "s.json" not in absent["files_before"]
        empty = by_id["write-into-an-empty-settings-file"]
        assert empty["files_before"]["s.json"] == {"text": ""}
        assert by_id["print-with-an-absent-endpoint-file"]["settings"] is None

    def test_no_stored_output_holds_an_absolute_path(self):
        text = "\n".join(json.dumps(case, ensure_ascii=False) for case in CASES)
        for part in _MACHINE_PATH_PARTS:
            assert part not in text, part
        for case in CASES:
            for value in _strings(case):
                assert _MACHINE_PATH.search(value) is None, (case["id"], value)


def _digests() -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in DATA_DIR.iterdir()
        if path.is_file()
    }


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []
