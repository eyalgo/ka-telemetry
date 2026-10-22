"""Tests that `python3 -m ka_telemetry` hands every argument list to `cli.main` (CP-321).

They live apart from `tests/test_cli.py`, which `tests/e2e/test_stream_cli.py` holds byte-identical
to the commit the transcript commands were copied from. The installed-command comparison is in
`tests/e2e/test_stream_labels.py`; these check the module against `cli.main` in-process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ka_telemetry import cli


def _response(message_id: str, **classes: int) -> str:
    usage = {
        "input_tokens": classes.get("input_tokens", 0),
        "cache_read_input_tokens": classes.get("cache_read", 0),
        "cache_creation_input_tokens": classes.get("cache_creation", 0),
        "output_tokens": 999,
    }
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage}})


def _transcript(path: Path) -> Path:
    path.write_text(_response("m1", input_tokens=10, cache_read=100, cache_creation=1000) + "\n")
    return path


PRODUCT_ROOT = Path(__file__).resolve().parent.parent


def _module(*arguments: str, home: Path) -> subprocess.CompletedProcess[str]:
    """`python3 -m ka_telemetry ARGUMENTS` from the product's root, with `HOME` of its own."""
    return subprocess.run(
        [sys.executable, "-m", "ka_telemetry", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(home), "PATH": ""},
        cwd=PRODUCT_ROOT,
    )


def test_module_help_is_the_help_cli_main_prints(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    expected = capsys.readouterr().out

    result = _module("--help", home=tmp_path)

    assert (result.returncode, raised.value.code) == (0, 0)
    assert result.stdout == expected
    assert expected.startswith(
        "usage: ka-telemetry [-h] {runs,steps,context,receive,agents,check,turn-on} ..."
    )


def test_module_usage_error_exits_with_the_status_of_cli_main(tmp_path: Path, capsys):
    for arguments in ([], ["frobnicate"], ["agents", "--bogus"]):
        with pytest.raises(SystemExit) as raised:
            cli.main(arguments)
        in_process = capsys.readouterr()

        result = _module(*arguments, home=tmp_path)

        assert result.returncode == raised.value.code == 2, arguments
        assert result.stderr == in_process.err, arguments


def test_module_returns_the_status_cli_main_returns(tmp_path: Path, capsys):
    transcript = _transcript(tmp_path / "session.jsonl")
    assert cli.main(["context", str(transcript)]) == 0
    expected = capsys.readouterr().out

    result = _module("context", str(transcript), home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
