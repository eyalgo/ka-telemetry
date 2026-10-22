"""Tests of the stored data set and dev's stored outputs under `tests/stream_parity/`.

The data set was recorded once from dev's receiver directory and reduced to the fields dev's
reader reads; the four outputs beside it were produced once, by dev's reader at the copy commit,
over those very files. These tests only read them: nothing here writes, regenerates or edits a
stored file, and a difference between the package and the stored output is a defect in the
package (`tests/e2e/test_stream_parity.py` compares them).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from e2e.support import needs_history

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DATA_SET = Path(__file__).resolve().parent / "stream_parity"
STORED_OUTPUTS = (
    "expected/agents.txt",
    "expected/agents.json",
    "expected/compare.txt",
    "expected/compare.json",
)
DATA_FILES = ("logs.jsonl", "metrics.jsonl", *STORED_OUTPUTS)
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
TOLERANCE = 0.01


def _sha256(relative: str) -> str:
    return hashlib.sha256((DATA_SET / relative).read_bytes()).hexdigest()


def _manifest() -> dict:
    return json.loads((DATA_SET / "manifest.json").read_text())


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(PRODUCT_ROOT), *args], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _attributes(attribute_list) -> dict:
    flat = {}
    for attribute in attribute_list or []:
        value = attribute.get("value", {})
        flat[attribute["key"]] = next(iter(value.values()), None)
    return flat


def _log_records() -> tuple[list[dict], list[dict], int]:
    """(api_request attribute maps with the resource's merged under, subagent maps, unreadable
    line count) read straight off logs.jsonl with the standard library."""
    api, subagent, unreadable = [], [], 0
    for line in (DATA_SET / "logs.jsonl").read_text().splitlines():
        try:
            outer = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        body = outer.get("body") if isinstance(outer, dict) else None
        if not isinstance(body, dict) or not body:
            unreadable += 1
            continue
        for resource_log in body.get("resourceLogs", []):
            resource = _attributes(resource_log.get("resource", {}).get("attributes"))
            for scope_log in resource_log.get("scopeLogs", []):
                for record in scope_log.get("logRecords", []):
                    attrs = _attributes(record.get("attributes"))
                    merged = {**{k: v for k, v in resource.items()}, **attrs}
                    if attrs.get("event.name") == "api_request":
                        api.append(merged)
                    elif attrs.get("event.name") == "subagent_completed":
                        subagent.append(merged)
    return api, subagent, unreadable


def _metric_totals() -> dict[str, float]:
    totals: dict[str, float] = {}
    for line in (DATA_SET / "metrics.jsonl").read_text().splitlines():
        body = json.loads(line)["body"]
        for resource_metric in body["resourceMetrics"]:
            for scope_metric in resource_metric["scopeMetrics"]:
                for metric in scope_metric["metrics"]:
                    assert metric["name"] == "claude_code.cost.usage"
                    for point in metric["sum"]["dataPoints"]:
                        session = _attributes(point["attributes"])["session.id"]
                        value = point.get("asDouble", point.get("asInt"))
                        totals[session] = totals.get(session, 0.0) + value
    return totals


def test_manifest_records_selection_reduction_order_commands_and_hashes():
    needs_history()
    manifest = _manifest()
    for key in (
        "source_commits",
        "sessions",
        "reduction_policy",
        "comparison",
        "oracle_commands",
        "hashes",
    ):
        assert manifest[key], key

    commits = manifest["source_commits"]
    assert _git("cat-file", "-t", commits["copy_commit"]) == "commit"
    assert _git("rev-parse", f"{commits['copy_commit']}:agent_usage.py") == commits["oracle_blob"]
    assert commits["dev_revision"]

    assert [entry["output"] for entry in manifest["oracle_commands"]] == list(STORED_OUTPUTS)
    for entry in manifest["oracle_commands"]:
        assert "agent_usage.py" in entry["command"] and "--dir" in entry["command"]

    assert set(manifest["hashes"]) == set(DATA_FILES)
    for relative, digest in manifest["hashes"].items():
        assert _sha256(relative) == digest, relative

    for stream in ("source_log_lines", "source_metric_lines"):
        recorded = [number for entry in manifest["sessions"] for number in entry[stream]]
        assert sorted(set(recorded)) == sorted(manifest[stream + "_kept"]), stream
        assert manifest[stream + "_kept"] == sorted(manifest[stream + "_kept"]), stream


def test_data_set_exercises_required_cases():
    manifest = _manifest()
    api, subagent, unreadable = _log_records()
    assert any(any(k.startswith("kensai.") for k in rec) for rec in api), "a labelled row"
    assert any(not any(k.startswith("kensai.") for k in rec) for rec in api), "an unlabelled row"
    assert subagent, "a subagent_completed record"
    assert unreadable >= 1, "an unreadable line"

    log_cost: dict[str, float] = {}
    for record in api:
        log_cost[record["session.id"]] = log_cost.get(record["session.id"], 0.0) + float(
            record["cost_usd"]
        )
    metric_cost = _metric_totals()
    gaps = {
        session: abs(metric_cost[session] - cost) / cost
        for session, cost in log_cost.items()
        if session in metric_cost and cost
    }
    assert any(gap <= TOLERANCE for gap in gaps.values()), "a session within 1%"
    assert any(gap > TOLERANCE for gap in gaps.values()), "a session outside 1%"
    assert any(session not in metric_cost for session in log_cost), "a session with no metric"

    comparison = manifest["comparison"]
    assert comparison["attr"].startswith("kensai.")
    values = {str(record.get(comparison["attr"])) for record in api}
    assert {comparison["a"], comparison["b"]} <= values


@pytest.mark.parametrize("relative", DATA_FILES + ("manifest.json",))
def test_data_set_carries_no_email(relative):
    text = (DATA_SET / relative).read_text()
    assert "user.email" not in text
    assert ADDRESS.search(text) is None


def test_expected_outputs_are_the_four_stored_outputs():
    manifest = _manifest()
    for relative in STORED_OUTPUTS:
        assert (DATA_SET / relative).is_file(), relative
        assert _sha256(relative) == manifest["hashes"][relative], relative
    assert (DATA_SET / "expected/agents.txt").read_text().splitlines()[0].endswith(
        "unreadable line(s) skipped."
    )
    assert "rows" in json.loads((DATA_SET / "expected/agents.json").read_text())
    assert "compare" in json.loads((DATA_SET / "expected/compare.json").read_text())
    assert (DATA_SET / "expected/compare.txt").read_text().count("--compare ") == 1


@pytest.mark.parametrize("output", STORED_OUTPUTS)
def test_stored_output_matches_the_copy_commit_reader(output, tmp_path):
    needs_history()
    if sys.version_info < (3, 12):
        # The copy-commit reader adds costs with plain `sum`, whose float result changed in
        # Python 3.12; the stored output is what it prints on 3.12 and later.
        pytest.skip("the copy-commit reader's float sums differ before Python 3.12")
    manifest = _manifest()
    revision = manifest["source_commits"]["copy_commit"]
    source = _git("show", f"{revision}:agent_usage.py")
    oracle = tmp_path / "agent_usage.py"
    oracle.write_text(source, encoding="utf-8")
    arguments = [sys.executable, str(oracle), "--dir", str(DATA_SET)]
    if "compare" in output:
        comparison = manifest["comparison"]
        arguments += ["--compare", comparison["attr"], comparison["a"], comparison["b"]]
    if output.endswith(".json"):
        arguments.append("--json")

    done = subprocess.run(arguments, cwd=tmp_path, capture_output=True, check=False)

    assert done.returncode == 0, done.stderr.decode()
    assert done.stderr == b""
    assert done.stdout == (DATA_SET / output).read_bytes()
