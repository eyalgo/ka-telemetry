"""What the README says about checking a release.

The README is read, never run: the release check is not run here. The section is held to the
words the change's published text fixes, and to the shape the PRD's success metric asks for: the
command as typed, where to run it, what it needs, what its lines and exit status mean, and no
shown output. Its claims about bar 5 and the three network fetches are held by `section_problems`,
which takes a section string, so the real section and each invalid variant of it go through the
same assertions.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from e2e.documentation_examples import parse_document
from e2e.support import base_readme

README = Path(__file__).resolve().parent.parent / "README.md"
HEADING = "## Check a release"
COMMAND = "python3 tools/release_check.py"
SECTION = """## Check a release

Every release of ka-telemetry has to meet six bars, numbered 1, 2, 3, 5, 7 and 8 among its success metrics. The release check measures all six of them. It measures bars 1, 2 and 3 on the reference set, the recorded telemetry and transcripts this repository keeps in `tests/parity` and `tests/stream_parity` with the figures expected from them. It measures bar 5 by building ka-telemetry from your copy and then running this README's own pipx install command, the first command in the Install section above, as written, on a Python that refuses pip installs as externally managed. What it built is the only package that command can install. It measures bar 7 by running every `ka-telemetry` command, refusing any attempt a command makes to reach beyond this machine, and by checking that the receiver, started with no `--host`, listens only on this machine. It does not watch the programs a command starts, such as the Git that `turn-on` runs. It measures bar 8 on the examples in this README and the how-to. Run it from the top directory of a copy of this repository, the directory that holds the README:

```sh
python3 tools/release_check.py
```

It needs Python 3.11 or later with pip, pytest installed for that Python, and Git. It measures the files in your copy as they are, including changes you have not committed, and changes none of them: it works in a temporary directory that it removes before it exits. The settings file `turn-on` writes during the check and the files the receiver writes are in that directory too, and the receiver is stopped before the check ends. For bar 5 it uses the pipx on your PATH, or, when there is none, a pipx of its own that it brings into that directory. What pipx writes during the check is in that directory as well.

The check fetches packages over the network with pip in these three steps and in no other, and none of them is part of the install it measures:

- Each build of ka-telemetry, one for the install and one for the examples, fetches its build backend.
- When there is no pipx on your PATH, bringing in the check's own pipx fetches pipx and the packages pipx depends on.
- Before the install it measures, readying the libraries pipx shares among the environments it makes, pip among them, fetches those libraries. This step runs whether the pipx is yours or the check's own.

The install it measures fetches no package: it installs only what it built.

It prints one line for each bar it checks: the bar's number, what the bar asks, and then `holds` or `fails`. A bar it could not measure ends `fails` too. For each line that ends `fails`, it writes lines to standard error that begin with the same number and say what did not hold, or what it could not read or run. It exits with status 0 when every line ends `holds`, and with status 1 when any line ends `fails`.
"""
EXAMPLES_TESTS = ("tests/test_documentation_examples.py", "tests/e2e/test_documented_examples.py")


BAR_5 = (
    "It measures bar 5 by building ka-telemetry from your copy and then running this README's own "
    "pipx install command, the first command in the Install section above, as written, on a "
    "Python that refuses pip installs as externally managed. What it built is the only package "
    "that command can install."
)
PIPX_SOURCES = (
    "For bar 5 it uses the pipx on your PATH, or, when there is none, a pipx of its own that it "
    "brings into that directory."
)
PIPX_WRITES = "What pipx writes during the check is in that directory as well."
THREE_STEPS = "in these three steps and in no other"
OUTSIDE_THE_MEASURED_INSTALL = "none of them is part of the install it measures"
MEASURED_INSTALL = "The install it measures fetches no package: it installs only what it built."
FETCHED_PYTHON = re.compile(
    r"(?i)\b(fetch\w*|download\w*)\s+(a\s+|the\s+|its\s+)?python\b"
    r"|\bpython\b[^.\n]{0,20}\b(is|are)\s+(fetched|downloaded)"
)
COMPLETE_PACKAGE_LIST = re.compile(
    r"(?i)\b(setuptools|wheel)\b|complete list|full list|exactly these (libraries|packages)"
)
CODE_BLOCK = re.compile(r"(?m)^```")
FENCED_BLOCK = re.compile(r"(?ms)^```.*?^```")
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def fetch_steps(section: str) -> list[str]:
    """The items of the section's list of fetches."""
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


def section_problems(section: str) -> set[str]:
    """What is wrong with a `## Check a release` section, by claim; empty when it holds them all.
    Takes the section as a string so the README and each invalid variant are judged alike."""
    paragraphs = section.split("\n\n")
    steps = fetch_steps(section)
    lead = next(
        (i for i, text in enumerate(paragraphs) if text.startswith("The check fetches")), -1
    )
    held = {
        "bar-5-runs-the-readmes-install-with-pipx": BAR_5 in section,
        "no-virtual-environment": "virtual environment" not in section,
        "pipx-source": PIPX_SOURCES in section,
        "pipx-writes": PIPX_WRITES in section,
        "exactly-three-fetches": len(steps) == 3 and THREE_STEPS in section,
        "fetch-build-backend": any("Each build" in s and "build backend" in s for s in steps),
        "fetch-pipx-and-dependencies": any(
            "no pipx on your PATH" in s and "pipx and the packages pipx depends on" in s
            for s in steps
        ),
        "fetch-shared-libraries": any(
            "libraries pipx shares" in s
            and "pip among them" in s
            and "yours or the check's own" in s
            for s in steps
        ),
        "no-fetched-python": not FETCHED_PYTHON.search(section),
        "no-complete-package-list": not COMPLETE_PACKAGE_LIST.search(section),
        "fetches-stay-outside-the-measured-install": OUTSIDE_THE_MEASURED_INSTALL in section,
        "measured-install-fetches-nothing-and-stands-apart": (
            MEASURED_INSTALL in paragraphs and paragraphs.index(MEASURED_INSTALL) > lead >= 0
        ),
        "one-code-block": len(CODE_BLOCK.findall(section)) == 2,
        "no-inline-install-command": not any(
            re.search(r"\binstall\b", span)
            for span in CODE_SPAN.findall(FENCED_BLOCK.sub("", section))
        ),
        "no-install-site": not parse_document(section, README).installs,
    }
    return {claim for claim, holds in held.items() if not holds}


def varied(old: str, new: str) -> str:
    """The published section with `old` replaced by `new`; `old` must be there to be replaced."""
    assert old in SECTION, old
    return SECTION.replace(old, new, 1)


def without_step(prefix: str) -> str:
    [line] = [line for line in SECTION.splitlines() if line.startswith(f"- {prefix}")]
    return SECTION.replace(line + "\n", "", 1)


def sections_of(text: str) -> dict[str, str]:
    """Each `## ` section of a README text by its heading line, from that line to the next one."""
    parts = re.split(r"(?m)^(?=## )", text)
    return {part.splitlines()[0]: part for part in parts if part.startswith("## ")}


def sections() -> dict[str, str]:
    return sections_of(README.read_text(encoding="utf-8"))


def release_section() -> str:
    return sections()[HEADING]


def test_readme_names_the_command_exactly_as_typed():
    assert f"```sh\n{COMMAND}\n```" in release_section()


def test_readme_says_where_to_run_it():
    assert (
        "Run it from the top directory of a copy of this repository, the directory that holds "
        "the README:"
    ) in release_section()


def test_readme_states_what_it_needs():
    assert "It needs Python 3.11 or later with pip, pytest installed for that Python, and Git." in (
        release_section()
    )


def test_readme_states_the_verdict_lines_and_the_exit_meaning():
    section = release_section()
    assert "It prints one line for each bar it checks" in section
    assert "`holds` or `fails`" in section
    assert "It exits with status 0 when every line ends `holds`" in section
    assert "with status 1 when any line ends `fails`" in section


def test_readme_shows_no_output_block_for_the_command():
    section = release_section()
    blocks = re.findall(r"(?ms)^```(\w*)\n(.*?)^```", section)
    assert blocks == [("sh", f"{COMMAND}\n")]
    assert section.rstrip().endswith("ends `fails`.")


def assert_flagged(claim: str, variant: str) -> None:
    assert claim in section_problems(variant), claim


def test_section_says_bar_5_runs_the_readmes_install_with_pipx_on_a_refusing_python():
    claim = "bar-5-runs-the-readmes-install-with-pipx"
    assert claim not in section_problems(release_section())
    no_pipx = varied("then running this README's own pipx install command", "then installing")
    assert_flagged(claim, no_pipx)
    assert_flagged(claim, varied("as written, ", ""))
    assert_flagged(claim, varied(" that refuses pip installs as externally managed", ""))
    only_package = " What it built is the only package that command can install."
    assert_flagged(claim, varied(only_package, ""))


def test_section_rejects_the_old_virtual_environment_sentence():
    old = "It measures bar 5 by building ka-telemetry from your copy and installing what it built"
    restored = varied(BAR_5, old + " into a new virtual environment.")
    assert "no-virtual-environment" not in section_problems(release_section())
    assert_flagged("no-virtual-environment", restored)
    assert_flagged("bar-5-runs-the-readmes-install-with-pipx", restored)


def test_section_names_both_pipx_sources_and_where_pipx_writes():
    assert not section_problems(release_section()) & {"pipx-source", "pipx-writes"}
    own_only = varied("the pipx on your PATH, or, when there is none, a pipx", "a pipx")
    assert_flagged("pipx-source", own_only)
    own = ", or, when there is none, a pipx of its own that it brings into that directory"
    path_only = varied(own, "")
    assert_flagged("pipx-source", path_only)
    assert_flagged("pipx-writes", varied(" " + PIPX_WRITES, ""))
    elsewhere = varied("is in that directory as well.", "is wherever pipx puts it.")
    assert_flagged("pipx-writes", elsewhere)


def test_section_names_exactly_the_three_fetches():
    fetches = {"fetch-build-backend", "fetch-pipx-and-dependencies", "fetch-shared-libraries"}
    assert not section_problems(release_section()) & (fetches | {"exactly-three-fetches"})
    assert_flagged("fetch-build-backend", without_step("Each build"))
    assert_flagged("fetch-pipx-and-dependencies", without_step("When there is no pipx"))
    assert_flagged("fetch-shared-libraries", without_step("Before the install it measures"))
    assert_flagged("exactly-three-fetches", without_step("Each build"))
    own_only = varied("This step runs whether the pipx is yours or the check's own.", "")
    assert_flagged("fetch-shared-libraries", own_only)


def test_section_names_no_fourth_fetch():
    assert not section_problems(release_section()) & {
        "no-fetched-python",
        "no-complete-package-list",
        "exactly-three-fetches",
    }
    step = "- The check fetches a Python 3.11 when none is found.\n"
    fourth_step = varied("\nThe install it measures", f"\n{step}\nThe install it measures")
    assert_flagged("no-fetched-python", fourth_step)
    other = "- Bar 7 fetches pytest for the commands it runs.\n"
    fourth_package = varied("\nThe install it measures", f"\n{other}\nThe install it measures")
    assert_flagged("exactly-three-fetches", fourth_package)
    for sentence in ("Python is fetched too.", "A Python is downloaded when none is found."):
        said = varied(MEASURED_INSTALL, f"{MEASURED_INSTALL} {sentence}")
        assert_flagged("no-fetched-python", said)
    complete = "which are exactly these libraries: pip, setuptools and wheel,"
    listed = varied("pip among them,", complete)
    assert_flagged("no-complete-package-list", listed)


def test_section_keeps_the_measured_install_fetch_free_and_apart():
    outside = "fetches-stay-outside-the-measured-install"
    apart = "measured-install-fetches-nothing-and-stands-apart"
    assert not section_problems(release_section()) & {outside, apart}
    inside = varied(OUTSIDE_THE_MEASURED_INSTALL, "all of them are part of the install it measures")
    assert_flagged(outside, inside)
    assert_flagged(apart, varied(MEASURED_INSTALL + "\n\n", ""))
    assert_flagged(apart, varied(f"\n{MEASURED_INSTALL}\n", f"\n- {MEASURED_INSTALL}\n"))
    before_the_list = varied(MEASURED_INSTALL + "\n\n", "").replace(
        "\nThe check fetches", f"\n{MEASURED_INSTALL}\n\nThe check fetches", 1
    )
    assert_flagged(apart, before_the_list)


def test_section_adds_no_install_site():
    sites = {"one-code-block", "no-inline-install-command", "no-install-site"}
    assert not section_problems(release_section()) & sites
    span = varied("the first command in the Install section above", "`pipx install ka-telemetry`")
    assert_flagged("no-inline-install-command", span)
    assert_flagged("no-install-site", span)
    block = "```sh\npipx install ka-telemetry\n```\n\n"
    second_block = varied("```\n\nIt needs Python", f"```\n\n{block}It needs Python")
    assert {"one-code-block", "no-install-site"} <= section_problems(second_block)


def test_protected_text_of_the_section_matches_f2b7a6e():
    base = sections_of(base_readme())[HEADING].rstrip("\n").split("\n\n")
    heading, first, block, second, verdict = base
    current = release_section().rstrip("\n").split("\n\n")
    assert len(current) == 8
    assert (current[0], current[2], current[-1]) == (heading, block, verdict)
    # The opening sentence named the requirements document, which the public repository leaves
    # out; it now names the six bars itself. What follows it is held as it was.
    after_opening = first.index("The release check measures all six")
    assert current[1].startswith(
        "Every release of ka-telemetry has to meet six bars, numbered 1, 2, 3, 5, 7 and 8 among "
        "its success metrics. " + first[after_opening : first.index("It measures bar 5")]
    )
    assert current[1].endswith(first[first.index(" It measures bar 7") :])
    kept = second[: second.index(" To check the install and the examples")]
    assert current[3].startswith(kept)
    assert kept.startswith("It needs Python 3.11 or later with pip, pytest installed for that")
    assert release_section().rstrip().endswith("ends `fails`.")


def test_readme_section_is_the_published_text():
    assert release_section() == SECTION + "\n"


def test_readme_section_is_outside_status():
    headings = list(sections())
    assert headings.index(HEADING) < headings.index("## Status")


def test_the_existing_examples_tests_still_pass_with_the_new_section():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *EXAMPLES_TESTS, "-q", "-pno:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=README.parent,
    )
    assert done.returncode == 0, done.stdout[-3000:]
