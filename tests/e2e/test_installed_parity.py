"""One installed run of all five readers, held to dev's stored figures.

The other parity tests each run some of the commands, one per test. This file holds the single
item that takes one installation of the package and runs every form of `runs`, `steps`,
`context`, `agents` and `check` itself over the stored data sets, then compares each output with
the stored one. It reuses no output another test computed and calls none of their test functions.

The comparison itself is `e2e.parity_measure.measure`, shared with the release check, which runs
the same commands from the checkout's source. What stays here is what only the installed artifact
can show: the shadow checkout it must not import, the empty home, and the stored trees' digests.

The stored files are the expectation and are never written. A difference is a defect in the
package, shown with both sides, and nothing here normalizes what the installed command printed.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from e2e.parity_measure import MeasurementError, measure
from e2e.support import child_env
from test_parity import PARITY, _load, _session_transcripts

TESTS = Path(__file__).resolve().parent.parent
STREAM = TESTS / "stream_parity"
STORED_TREES = (PARITY, STREAM, TESTS / "turn_on_parity")


def _tree_digest() -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for tree in STORED_TREES
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


def test_the_installed_package_gives_dev_figures_for_all_five_readers(
    installed, tmp_path, monkeypatch
):
    before = _tree_digest()
    home = tmp_path / "home"
    home.mkdir()
    # CP-262 requires the installed artifact even when the caller exposes another checkout.
    shadow = tmp_path / "other-checkout" / "ka_telemetry"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        'raise RuntimeError("parity imported the caller checkout")\n', encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(shadow.parent))
    monkeypatch.chdir(shadow.parent)

    def invoke(label: str, arguments: tuple[str, ...]) -> str:
        done = subprocess.run(
            [str(installed.command), *arguments],
            capture_output=True,
            check=False,
            env={
                key: value for key, value in child_env(home).items() if not key.startswith("PYTHON")
            },
            cwd=home,
        )
        diagnostic = done.stderr.decode("utf-8")
        if done.returncode != 0:
            raise MeasurementError(label, f"exit code {done.returncode}\n{diagnostic}")
        return done.stdout.decode("utf-8")

    differences = measure(invoke, parity_root=PARITY, stream_root=STREAM)

    assert not differences, "\n\n".join(
        f"{difference.expected_file}\n{difference.detail}" for difference in differences
    )
    sizes = _session_transcripts()
    stored_sizes = _load("expected/context_sizes.json")
    assert set(sizes) == set(stored_sizes)
    assert _tree_digest() == before, "a stored data tree was written"
