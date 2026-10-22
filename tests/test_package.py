"""Tests for the package boundary and the installed artifact.

Everything that needs the product installed uses the `installed` fixture (`conftest.py`): a
scratch virtualenv built from a copy of the checkout, never the checkout itself.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import InstalledProduct

PRODUCT_ROOT = Path(__file__).resolve().parent.parent

PACKAGE_DIR = PRODUCT_ROOT / "ka_telemetry"
PACKAGE_SOURCES = sorted(PACKAGE_DIR.glob("*.py"))
READER_MODULES = ["run_usage", "flow_usage", "context_gauge"]
COPY_ONLY_SCRIPTS = [
    "otel_receiver.py",
    "agent_usage.py",
    "apply_telemetry_env.py",
    "instrument_run.sh",
]

STREAM_MODULES = ("receiver", "stream_usage")
RETIRED_TURN_ON_FILES = ("apply_telemetry_env.py", "instrument_run.sh")
ROOT_COPIES = ("otel_receiver.py", "agent_usage.py")
PACKAGE_NAME_GREPS = {
    "the old default directory or a kensai-dotted name": ["-E", r"\.kensaiapp|kensai\."],
    "the word kensai": ["-w", "kensai"],
    "a rotation claim": ["-i", "no rotation"],
    "a copied root basename": ["-E", "otel_receiver|agent_usage"],
}
ORIGINAL_EXPORTS = ["run_usage", "flow_usage", "context_gauge"]
NOT_INSTALLED_NAMES = {"tests", "parity", "stream_parity", "e2e", "expected", "conftest.py"}

# The retired helper's name, built from pieces: a file that spelled it out would itself be found
# by the guard below.
RETIRED_HELPER = "agent" + "_" + "turn"


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def test_install_adds_only_ka_telemetry(installed: InstalledProduct):
    assert installed.installed - installed.baseline == {"ka-telemetry"}


def test_pyproject_declares_no_runtime_dependency():
    project = tomllib.loads((PRODUCT_ROOT / "pyproject.toml").read_text())["project"]

    assert project.get("dependencies", []) == []
    assert "optional-dependencies" not in project


def test_installed_metadata_requires_nothing(installed: InstalledProduct):
    probe = (
        "from importlib.metadata import requires; print(requires('ka-telemetry'))"
    )
    result = _run([str(installed.python), "-c", probe])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"


def test_command_is_on_path_and_help_names_the_three_subcommands(installed: InstalledProduct):
    assert installed.command.is_file()

    result = _run([str(installed.command), "--help"])

    assert result.returncode == 0, result.stderr
    for subcommand in ("runs", "steps", "context"):
        assert subcommand in result.stdout


def test_installed_unknown_flag_exits_2_with_usage_on_stderr(installed: InstalledProduct):
    result = _run([str(installed.command), "--bogus"])

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("usage: ka-telemetry")
    assert result.stderr.splitlines()[-1] == (
        "Run `ka-telemetry --help` to see the commands and what each one accepts."
    )


def test_library_import_works_from_outside_the_checkout(installed: InstalledProduct, tmp_path):
    probe = (
        "from ka_telemetry import run_usage, flow_usage, context_gauge; "
        "print(run_usage.__file__); print(flow_usage.__file__); print(context_gauge.__file__)"
    )
    result = _run([str(installed.python), "-I", "-c", probe], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    for printed in result.stdout.splitlines():
        assert Path(printed).is_relative_to(installed.site_packages)


def test_package_exports_the_three_reader_modules(installed: InstalledProduct, tmp_path):
    probe = (
        "import ka_telemetry; "
        "print(sorted(ka_telemetry.__all__)); "
        "print(ka_telemetry.run_usage.__name__, ka_telemetry.flow_usage.__name__, "
        "ka_telemetry.context_gauge.__name__)"
    )
    result = _run([str(installed.python), "-I", "-c", probe], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(sorted(READER_MODULES)),
        "ka_telemetry.run_usage ka_telemetry.flow_usage ka_telemetry.context_gauge",
    ]


def test_package_initializer_holds_no_implementation():
    tree = ast.parse((PACKAGE_DIR / "__init__.py").read_text())

    assert not [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)]
    assert [n for n in tree.body if isinstance(n, ast.Assign)] and "__all__" in ast.unparse(tree)


def test_installed_artifact_excludes_root_scripts_and_parity_data(installed: InstalledProduct):
    probe = (
        "from importlib.metadata import files; "
        "print('\\n'.join(sorted(str(f) for f in files('ka-telemetry'))))"
    )
    result = _run([str(installed.python), "-c", probe])
    assert result.returncode == 0, result.stderr
    paths = [Path(line) for line in result.stdout.splitlines()]

    shipped_names = {part for path in paths for part in path.parts}
    assert not shipped_names & set(COPY_ONLY_SCRIPTS)
    assert not shipped_names & {"tests", "parity", "conftest.py"}
    # What is installed is the package, its metadata and the command, and nothing beside them.
    for path in paths:
        assert path.parts[0] == "ka_telemetry" or path.parts[0].endswith(".dist-info") or (
            path.name == "ka-telemetry" and path.parts[0] == ".."
        ), path


def test_reader_and_cli_source_import_only_stdlib_and_package_modules():
    assert {p.name for p in PACKAGE_SOURCES} >= {
        "__init__.py", "run_usage.py", "flow_usage.py", "context_gauge.py", "cli.py",
    }
    for source in PACKAGE_SOURCES:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{source.name}: a relative import"
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert roots <= set(sys.stdlib_module_names) | {"ka_telemetry"}, (source.name, roots)


def test_reader_and_cli_source_has_no_kensaiapp_reference():
    for source in PACKAGE_SOURCES:
        assert re.search(r"\.kensaiapp|kensai\.", source.read_text()) is None, source.name


def test_the_retired_helper_is_absent_from_tracked_files_outside_docs(
    installed: InstalledProduct, tmp_path
):
    found = _run(
        ["git", "-C", str(PRODUCT_ROOT), "grep", "-n", "-F", RETIRED_HELPER]
        + ["HEAD", "--", ".", ":!docs"]
    )
    assert (found.returncode, found.stdout) == (1, "")

    imported = _run([str(installed.python), "-c", f"import {RETIRED_HELPER}"], cwd=tmp_path)
    assert imported.returncode != 0
    assert "ModuleNotFoundError" in imported.stderr


@pytest.mark.parametrize("path", ["./missing.json", "folder//missing.json", "missing.json/"])
def test_installed_listing_error_preserves_the_path_as_supplied(installed, tmp_path, path):
    result = _run([str(installed.command), "runs", "--agents-json", path], cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"error: could not read {path} — check the path given to --agents-json, or leave "
        "--agents-json out to have ka-telemetry run `claude agents --json --all` itself\n"
    )


# ── What the stream move left in place ──────────────────────────────────────


def _git(*args: str) -> str:
    done = _run(["git", "-C", str(PRODUCT_ROOT), *args])
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _package_modules_imported_by(source: Path) -> set[str]:
    """The `ka_telemetry` modules a file imports, whichever way it spells the import."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import):
            found |= {
                a.name.split(".")[1] for a in node.names if a.name.startswith("ka_telemetry.")
            }
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "ka_telemetry":
                found |= {alias.name for alias in node.names}
            elif node.module.startswith("ka_telemetry."):
                found.add(node.module.split(".")[1])
    return found


def test_root_receiver_and_reader_not_tracked():
    assert _git("ls-files", *ROOT_COPIES) == ""
    assert _git("ls-tree", "HEAD", "--name-only", "--", *ROOT_COPIES) == ""
    for name in ROOT_COPIES:
        assert not (PRODUCT_ROOT / name).exists(), name
    # The writer and the shell library are gone from the root: the command runs from the package
    # (CP-221).
    for name in RETIRED_TURN_ON_FILES:
        assert _git("ls-files", name) == "", name
        assert not (PRODUCT_ROOT / name).exists(), name


@pytest.mark.parametrize("looking_for", PACKAGE_NAME_GREPS)
def test_no_fixed_default_or_fixed_name_in_package(looking_for):
    found = _run(
        ["git", "-C", str(PRODUCT_ROOT), "grep", "-n", *PACKAGE_NAME_GREPS[looking_for]]
        + ["HEAD", "--", "ka_telemetry/"]
    )

    assert (found.returncode, found.stdout) == (1, ""), looking_for


def test_every_package_module_enumerated_from_disk_imports_only_stdlib_or_the_package():
    modules = sorted(PACKAGE_DIR.rglob("*.py"))

    assert {m.stem for m in modules} >= {*STREAM_MODULES, "stream_paths", "cli"}
    for source in modules:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{source.name}: a relative import"
                roots = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            else:
                continue
            assert roots <= set(sys.stdlib_module_names) | {"ka_telemetry"}, (source.name, roots)


def test_the_two_stream_modules_never_import_each_other_or_the_transcript_readers():
    for name in STREAM_MODULES:
        imported = _package_modules_imported_by(PACKAGE_DIR / f"{name}.py")

        assert imported <= {"stream_paths"}, (name, imported)
    assert _package_modules_imported_by(PACKAGE_DIR / "stream_paths.py") == set()
    assert {"receiver", "stream_usage"} <= _package_modules_imported_by(PACKAGE_DIR / "cli.py")


def test_the_note_helper_imports_the_stream_reader_and_neither_transcript_reader():
    imported = _package_modules_imported_by(PACKAGE_DIR / "transcript_notes.py")

    assert imported <= {"stream_usage"}, imported
    assert not imported & {"run_usage", "flow_usage"}


def test_neither_parity_tree_is_installed(installed: InstalledProduct):
    configured = tomllib.loads((PRODUCT_ROOT / "pyproject.toml").read_text())
    assert configured["tool"]["setuptools"]["packages"] == ["ka_telemetry"]

    probe = (
        "from importlib.metadata import files; "
        "print('\\n'.join(sorted(str(f) for f in files('ka-telemetry'))))"
    )
    result = _run([str(installed.python), "-c", probe])
    assert result.returncode == 0, result.stderr
    paths = [Path(line) for line in result.stdout.splitlines()]

    assert {part for path in paths for part in path.parts} & NOT_INSTALLED_NAMES == set()
    stream_modules = {f"ka_telemetry/{name}.py" for name in (*STREAM_MODULES, "stream_paths")}
    assert stream_modules <= {path.as_posix() for path in paths}


def test_no_installed_path_component_carries_a_copied_root_basename(installed: InstalledProduct):
    copied_stems = {Path(name).stem for name in ROOT_COPIES}
    shipped = [
        path.relative_to(installed.site_packages)
        for path in installed.site_packages.rglob("*")
        if path.is_relative_to(installed.site_packages / "ka_telemetry")
        or path.name.startswith("ka_telemetry")
    ]

    assert shipped, "nothing of the package was found in site-packages"
    for path in shipped:
        for part in path.parts:
            assert part.split(".")[0] not in copied_stems, path


def test_existing_export_and_initializer_assertions_unchanged():
    tree = ast.parse((PACKAGE_DIR / "__init__.py").read_text())
    exported = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    )

    assert sorted(exported) == sorted(ORIGINAL_EXPORTS)
    assert not _package_modules_imported_by(PACKAGE_DIR / "__init__.py") & {
        *STREAM_MODULES, "stream_paths",
    }
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)]


def _collected(*arguments: str) -> list[str]:
    done = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
        + list(arguments),
        cwd=PRODUCT_ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_suite_continuity_the_five_non_strict_xfails_remain():
    # The five cases that were expected to fail on a machine with a commit hook now run isolated
    # from it, so no test under tests/ carries an xfail mark (CP-246). The suite still does not
    # configure xfail_strict.
    done = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-m", "xfail", "tests/"],
        cwd=PRODUCT_ROOT,
    )
    # pytest exits 5 when the selection is empty, which is the answer wanted here.
    assert done.returncode in (0, 5), done.stdout + done.stderr
    xfailed = [line for line in done.stdout.splitlines() if "::" in line]
    marked = [
        path.name
        for path in sorted((PRODUCT_ROOT / "tests").rglob("test_*.py"))
        if re.search(r"^\s*@pytest\.mark\.xfail|pytest\.xfail\(", path.read_text(), re.MULTILINE)
        and path.name != "test_package.py"
    ]
    configured = tomllib.loads((PRODUCT_ROOT / "pyproject.toml").read_text())

    assert xfailed == [], xfailed
    assert marked == [], marked
    assert "xfail_strict" not in configured["tool"]["pytest"]["ini_options"]


# ── The finished package: what it imports, names and installs, and what stayed put ──────

# CP-222: the three greps that print nothing over `ka_telemetry/`.
CP_222_PATTERNS = (
    ["-E", r"bash|\.sh"],
    ["-E", r"kensai|\.kensaiapp"],
    ["-i", "-E", r"kensai|instrument_run"],
)
BASE_TEST_IDS = PRODUCT_ROOT / "tests" / "turn_on_parity" / "base_test_ids.txt"

_ENV_TESTS = "tests/test_apply_telemetry_env.py"
_LAUNCHER_REFUSAL_TEST = (
    "tests/test_instrument_run.py::TestLauncherSubjectAndProduct::"
    "test_a_value_otel_cannot_carry_is_refused_for_that_key_alone"
)
# The ten base ids whose subject CP-270, CP-275 or CP-278 reverses; the guard's only exceptions.
EXCEPTED_BASE_TEST_IDS = frozenset(
    {
        f"{_ENV_TESTS}::TestYourOwnValuesAreNotOverwritten::"
        "test_a_different_value_already_there_is_refused_by_name_and_nothing_is_written",
        f"{_ENV_TESTS}::TestRemove::test_deletes_exactly_the_five_keys_and_leaves_everything_else",
        f"{_ENV_TESTS}::TestRemove::"
        "test_env_key_is_dropped_entirely_when_no_other_keys_remain_in_it",
        f"{_ENV_TESTS}::TestRemove::"
        "test_a_value_this_command_would_not_have_written_is_left_and_named",
        *(
            f"{_LAUNCHER_REFUSAL_TEST}[{name}]"
            for name in ("space", "quote", "comma", "semicolon", "backslash", "equals")
        ),
    }
)


def test_every_package_module_imports_only_the_standard_library_and_the_package():
    modules = sorted(PACKAGE_DIR.rglob("*.py"))

    assert {module.stem for module in modules} >= {"cli", "turn_on", "receiver", "stream_usage"}
    for source in modules:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{source.name}: a relative import"
                roots = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            else:
                continue
            assert roots <= set(sys.stdlib_module_names) | {"ka_telemetry"}, (source.name, roots)


@pytest.mark.parametrize(
    "arguments", CP_222_PATTERNS, ids=["bash-or-sh", "kensai", "kensai-or-instrument_run"]
)
def test_no_bash_sh_kensai_or_instrument_run_in_the_package(arguments):
    # The files on disk, not `git grep HEAD`: a HEAD grep would not see code written and not yet
    # committed, so the guard would pass in the very state it exists to catch.
    found = _run(["grep", "-rn", "--include=*.py", *arguments, str(PACKAGE_DIR)])

    assert (found.returncode, found.stdout) == (1, ""), found.stdout


def test_the_installed_files_hold_neither_root_basename_nor_parity_data(
    installed: InstalledProduct,
):
    probe = (
        "from importlib.metadata import files; "
        "print('\\n'.join(sorted(str(f) for f in files('ka-telemetry'))))"
    )
    result = _run([str(installed.python), "-c", probe])
    assert result.returncode == 0, result.stderr
    parts = {part for line in result.stdout.splitlines() for part in Path(line).parts}

    assert not parts & set(RETIRED_TURN_ON_FILES)
    assert "turn_on_parity" not in parts
    assert "ka_telemetry/turn_on.py" in result.stdout.splitlines()


def _base_test_ids() -> set[str]:
    return {line for line in BASE_TEST_IDS.read_text().splitlines() if line}


def test_every_base_test_id_of_the_two_copied_files_is_still_collected():
    base = _base_test_ids()
    collected = set(
        _collected("tests/test_apply_telemetry_env.py", "tests/test_instrument_run.py")
    )
    required = base - EXCEPTED_BASE_TEST_IDS

    assert len(base) == 126, len(base)
    assert len(required) == 116, len(required)
    assert required <= collected, sorted(required - collected)


def test_the_ten_excepted_ids_are_exactly_cp_285s_and_all_listed_in_the_base_list():
    # CP-285: no eleventh id may be excused, and each excused id is one the base list holds.
    assert len(EXCEPTED_BASE_TEST_IDS) == 10
    assert EXCEPTED_BASE_TEST_IDS <= _base_test_ids()
    assert {
        identity for identity in EXCEPTED_BASE_TEST_IDS if "test_apply_telemetry_env.py" in identity
    } == {
        f"{_ENV_TESTS}::TestYourOwnValuesAreNotOverwritten::"
        "test_a_different_value_already_there_is_refused_by_name_and_nothing_is_written",
        f"{_ENV_TESTS}::TestRemove::test_deletes_exactly_the_five_keys_and_leaves_everything_else",
        f"{_ENV_TESTS}::TestRemove::"
        "test_env_key_is_dropped_entirely_when_no_other_keys_remain_in_it",
        f"{_ENV_TESTS}::TestRemove::"
        "test_a_value_this_command_would_not_have_written_is_left_and_named",
    }


def test_the_distribution_declares_no_runtime_dependency():
    project = tomllib.loads((PRODUCT_ROOT / "pyproject.toml").read_text())["project"]

    assert project["dependencies"] == []
    assert "optional-dependencies" not in project


# The Apache License, Version 2.0 as published at https://www.apache.org/licenses/LICENSE-2.0.txt,
# pinned by its digest so the test needs no network and a changed byte is found.
LICENSE_FILE = PRODUCT_ROOT / "LICENSE"
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
APACHE_2_0_TITLE_LINES = 4  # lines holding "Apache License"
APACHE_2_0_VERSION_LINES = 2  # lines holding "Version 2.0"
LICENSE_EXPRESSION = "Apache-2.0"
DESCRIPTION_SOURCES = ("transcripts", "telemetry stream")
DISTRIBUTION_VERSION = "0.1.1"

PYPROJECT_FILE = PRODUCT_ROOT / "pyproject.toml"


def _licence_problems(path: Path) -> list[str]:
    """What is wrong with the licence file at `path`; empty when it is the published text."""
    if not path.is_file():
        return ["the licence file is missing"]
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    problems = []
    if hashlib.sha256(raw).hexdigest() != APACHE_2_0_SHA256:
        problems.append("the licence file is not the published text")
    if sum("Apache License" in line for line in text.splitlines()) != APACHE_2_0_TITLE_LINES:
        problems.append("the licence file does not name the Apache License as published")
    if sum("Version 2.0" in line for line in text.splitlines()) != APACHE_2_0_VERSION_LINES:
        problems.append("the licence file does not name Version 2.0 as published")
    return problems


def _description_problems(pyproject_text: str) -> list[str]:
    """What is wrong with the package description in `pyproject_text`, and with the version."""
    lines = re.findall(r"^description.*telemetry.*$", pyproject_text, flags=re.MULTILINE)
    if len(lines) != 1:
        return [f"{len(lines)} description lines name telemetry, not one"]
    problems = [
        f"the description does not name {source}"
        for source in DESCRIPTION_SOURCES
        if source not in lines[0]
    ]
    if f'version = "{DISTRIBUTION_VERSION}"' not in pyproject_text:
        problems.append(f"the version is not {DISTRIBUTION_VERSION}")
    return problems


def test_license_file_is_the_published_apache_2_0_text():
    assert _licence_problems(LICENSE_FILE) == []


def test_pyproject_declares_the_apache_2_0_expression():
    text = PYPROJECT_FILE.read_text()
    project = tomllib.loads(text)["project"]

    assert project["license"] == LICENSE_EXPRESSION
    assert text.lower().count("license") > 0


def test_installed_distribution_reports_the_licence_expression_and_carries_the_license_file(
    installed: InstalledProduct, tmp_path
):
    probe = (
        "from importlib.metadata import distribution; "
        "dist = distribution('ka-telemetry'); "
        "print(dist.metadata['License-Expression']); "
        "print(*[dist.locate_file(f) for f in dist.files if f.name == 'LICENSE'], sep='\\n')"
    )
    result = _run([str(installed.python), "-I", "-c", probe], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    expression, *licence_files = result.stdout.splitlines()
    assert expression == LICENSE_EXPRESSION
    assert len(licence_files) == 1
    shipped = Path(licence_files[0]).read_bytes()
    assert hashlib.sha256(shipped).hexdigest() == APACHE_2_0_SHA256
    assert shipped == LICENSE_FILE.read_bytes()


def test_description_names_both_sources_and_the_version_is_unchanged():
    text = PYPROJECT_FILE.read_text()

    assert _description_problems(text) == []
    # CP-506 is about the [project] table's own version, not any line that spells it.
    assert tomllib.loads(text)["project"]["version"] == DISTRIBUTION_VERSION


def test_description_missing_either_source_is_rejected():
    real = PYPROJECT_FILE.read_text()
    assert _description_problems(real) == []
    real_line = re.search(r"^description.*$", real, flags=re.MULTILINE).group(0)
    stream_only = 'description = "Receives the telemetry stream Claude Code can send."'
    transcripts_only = 'description = "Reads the transcripts Claude Code keeps, as telemetry."'

    assert _description_problems(real.replace(real_line, stream_only)) == [
        "the description does not name transcripts"
    ]
    assert _description_problems(real.replace(real_line, transcripts_only)) == [
        "the description does not name telemetry stream"
    ]


def test_license_missing_or_altered_is_rejected(tmp_path):
    assert _licence_problems(LICENSE_FILE) == []
    real = LICENSE_FILE.read_bytes()
    truncated = tmp_path / "truncated"
    truncated.write_bytes(real[: len(real) // 2])
    filled = tmp_path / "filled"
    filled.write_bytes(real.replace(b"[yyyy] [name of copyright owner]", b"2026 Someone"))

    assert _licence_problems(tmp_path / "absent") == ["the licence file is missing"]
    assert _licence_problems(truncated) != []
    assert _licence_problems(filled) == ["the licence file is not the published text"]


STREAM_USAGE_SOURCE = PACKAGE_DIR / "stream_usage.py"
VERSION_ASSIGNMENT = re.compile(r"^__version__ = .*$", flags=re.MULTILINE)


def _declared_version() -> str:
    return tomllib.loads(PYPROJECT_FILE.read_text())["project"]["version"]


def test_stream_usage_version_is_the_distribution_version():
    from ka_telemetry import stream_usage

    assert stream_usage.__version__ == DISTRIBUTION_VERSION
    assert stream_usage.__version__ == _declared_version()


def test_stream_usage_version_assignment_on_disk_is_0_1_0():
    assignments = VERSION_ASSIGNMENT.findall(STREAM_USAGE_SOURCE.read_text())

    assert assignments == [f'__version__ = "{DISTRIBUTION_VERSION}"']


TRANSCRIPT_NOTES_SOURCE = PACKAGE_DIR / "transcript_notes.py"
VERSION_PROBE = (
    "from ka_telemetry import stream_usage, transcript_notes; "
    "print(stream_usage.__version__, transcript_notes.__version__)"
)


def test_transcript_notes_version_is_the_distribution_version():
    from ka_telemetry import transcript_notes

    assert transcript_notes.__version__ == DISTRIBUTION_VERSION
    assert transcript_notes.__version__ == _declared_version()


def test_exactly_two_version_assignments_exist_in_the_package_each_0_1_0():
    found = {
        source.name: VERSION_ASSIGNMENT.findall(source.read_text())
        for source in sorted(PACKAGE_DIR.glob("*.py"))
        if VERSION_ASSIGNMENT.search(source.read_text())
    }

    assert found == {
        "stream_usage.py": [f'__version__ = "{DISTRIBUTION_VERSION}"'],
        "transcript_notes.py": [f'__version__ = "{DISTRIBUTION_VERSION}"'],
    }


def test_the_two_modules_report_0_1_0_together():
    result = _run([sys.executable, "-c", VERSION_PROBE], cwd=PRODUCT_ROOT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{DISTRIBUTION_VERSION} {DISTRIBUTION_VERSION}"
