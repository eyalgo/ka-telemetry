"""Shared test support: one scratch virtualenv holding the built product.

CP-23 forbids installing into the checkout, so the installed-command tests share a copy of the
checkout built into a fresh virtualenv under pytest's temporary directory. It is made once per
session and reused. Building needs the build backend named in `pyproject.toml`, which pip fetches
into its own isolated build environment: that is the one network call the suite can make, and
it is made by the build tool, never by the product.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
_NOT_COPIED = shutil.ignore_patterns(
    ".git", ".worktrees", "__pycache__", ".pytest_cache", "*.egg-info", "build", "dist"
)


@dataclass(frozen=True)
class InstalledProduct:
    """The product as a user gets it: built from a copy of the checkout into an empty virtualenv."""

    venv: Path
    site_packages: Path
    baseline: frozenset[str]  # what an empty virtualenv lists, before the install
    installed: frozenset[str]  # what it lists after

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def command(self) -> Path:
        return self.venv / "bin" / "ka-telemetry"


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def _listed(python: Path) -> frozenset[str]:
    listing = _run(
        [str(python), "-m", "pip", "list", "--format=json", "--disable-pip-version-check"]
    )
    assert listing.returncode == 0, listing.stderr
    return frozenset(entry["name"] for entry in json.loads(listing.stdout))


@pytest.fixture(scope="session")
def installed(tmp_path_factory: pytest.TempPathFactory) -> InstalledProduct:
    scratch = tmp_path_factory.mktemp("installed")
    source = scratch / "source"
    shutil.copytree(PRODUCT_ROOT, source, ignore=_NOT_COPIED)

    venv = scratch / "venv"
    created = _run([sys.executable, "-m", "venv", str(venv)])
    assert created.returncode == 0, created.stderr
    python = venv / "bin" / "python"
    baseline = _listed(python)

    built = _run(
        [
            str(python), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", str(source),
        ]
    )
    if built.returncode != 0:
        pytest.fail(f"building the product failed in a scratch virtualenv:\n{built.stderr[-2000:]}")

    where = _run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
    return InstalledProduct(
        venv=venv,
        site_packages=Path(where.stdout.strip()),
        baseline=baseline,
        installed=_listed(python),
    )


ROOT_EFFECTIVE_UID = 0
NO_PERMISSIONS = 0


@contextmanager
def sealed_paths(effective_uid: int) -> Iterator[Callable[[Path], Path]]:
    """Hands out a function that takes every permission off a path, and puts back the mode the path
    held before it was sealed when the block ends, whether the block passed or failed.

    Mode 000 does not stop a read at effective uid 0, so a block run as root is skipped. A path
    sealed inside an earlier-sealed directory is put back first, because the directory has to be
    readable again before its member's mode can be set."""
    if effective_uid == ROOT_EFFECTIVE_UID:
        pytest.skip("mode 000 does not stop a read at effective uid 0")
    sealed: list[tuple[Path, int]] = []

    def seal(path: Path) -> Path:
        sealed.append((path, stat.S_IMODE(path.stat().st_mode)))
        path.chmod(NO_PERMISSIONS)
        return path

    try:
        yield seal
    finally:
        for path, mode in reversed(sealed):
            path.chmod(mode)


@pytest.fixture
def seal() -> Iterator[Callable[[Path], Path]]:
    """`seal(path)` makes `path` unreadable for the length of the test, as the user the suite runs
    as, and restores its mode afterwards. The test is skipped when that user is root."""
    with sealed_paths(os.geteuid()) as seal_path:
        yield seal_path


LIST_ONLY = 0o444  # the names inside may be read; the directory may not be entered


@pytest.fixture
def list_only() -> Iterator[Callable[[Path], Path]]:
    """`list_only(directory)` leaves the names inside `directory` readable but stops anything from
    entering it, so nothing inside can be looked at, for the length of the test, and restores its
    mode afterwards. Mode bits do not stop root, so the test is skipped when it runs as root."""
    if os.geteuid() == ROOT_EFFECTIVE_UID:
        pytest.skip("mode bits do not stop a directory being entered at effective uid 0")
    changed: list[tuple[Path, int]] = []

    def restrict(directory: Path) -> Path:
        changed.append((directory, stat.S_IMODE(directory.stat().st_mode)))
        directory.chmod(LIST_ONLY)
        return directory

    try:
        yield restrict
    finally:
        for directory, mode in reversed(changed):
            directory.chmod(mode)


@pytest.fixture
def with_mode() -> Iterator[Callable[[Path, int], Path]]:
    """`with_mode(path, mode)` gives `path` exactly `mode` for the length of the test and restores
    the mode it held afterwards. Mode bits do not stop root, so the test is skipped as root."""
    if os.geteuid() == ROOT_EFFECTIVE_UID:
        pytest.skip("mode bits do not stop a directory being entered at effective uid 0")
    changed: list[tuple[Path, int]] = []

    def apply(path: Path, mode: int) -> Path:
        changed.append((path, stat.S_IMODE(path.stat().st_mode)))
        path.chmod(mode)
        return path

    try:
        yield apply
    finally:
        for path, mode in reversed(changed):
            path.chmod(mode)
