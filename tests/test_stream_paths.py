"""Tests for `ka_telemetry.stream_paths`: the one shared home of the default stream directory.

The value is the raw, unexpanded string. Expanding `~` belongs to the two composition roots that
read the environment (`receiver.build_receiver`, `stream_usage.build_reader`), so importing this
module must never need a usable home directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ka_telemetry import stream_paths

PRODUCT_ROOT = Path(__file__).resolve().parent.parent


def test_default_directory_is_raw_home_relative_string():
    assert stream_paths.DEFAULT_STREAM_DIRECTORY == "~/.ka-telemetry"


def test_import_performs_no_home_expansion():
    probe = (
        "import os, sys\n"
        "os.environ['HOME'] = '/nonexistent/home/for/this/probe'\n"
        "os.environ.pop('USERPROFILE', None)\n"
        "from ka_telemetry import stream_paths\n"
        "print(stream_paths.DEFAULT_STREAM_DIRECTORY)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=PRODUCT_ROOT,
        env={"PATH": "", "PYTHONPATH": str(PRODUCT_ROOT), "HOME": "/nonexistent/home"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "~/.ka-telemetry"
    assert stream_paths.DEFAULT_STREAM_DIRECTORY.startswith("~")


def test_module_exposes_only_the_constant():
    public = {name for name in vars(stream_paths) if not name.startswith("_")}
    own = {
        name
        for name in public
        if getattr(getattr(stream_paths, name), "__module__", stream_paths.__name__)
        == stream_paths.__name__
    }

    assert own == {"DEFAULT_STREAM_DIRECTORY"}
    assert not [n for n in public if callable(getattr(stream_paths, n))]
    assert not hasattr(stream_paths, "main")
