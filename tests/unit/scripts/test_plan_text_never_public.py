"""A Terraform plan's text never reaches a public log, summary or artifact.

This repository's Actions logs, run summaries and artifacts are public, and a
plan's text prints attribute values from the private tfvars (members, domains,
addresses). The workflows write it to `plan.txt` and print only its summary
line (`Plan:` / `No changes`); the run summary lists resource addresses and
actions from `plan.json`. The owner's pick, 2026-11-14.

These read the workflow files as text: nothing runs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = sorted([*(ROOT / ".github" / "workflows").glob("*.yml"), *(ROOT / ".github" / "actions").rglob("*.yml")])


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _texts() -> dict[str, str]:
    return {str(p.relative_to(ROOT)): _code(p.read_text(encoding="utf-8")) for p in FILES}


def test_the_scan_reads_the_plan_steps() -> None:
    """The workflows that plan are read, so an empty scan cannot pass."""
    planning = [name for name, text in _texts().items() if "plan.txt" in text]
    assert ".github/workflows/terraform.yml" in planning, planning
    assert ".github/actions/release-apply/action.yml" in planning, planning


def test_no_plan_output_is_teed_to_the_log() -> None:
    """MUTATION: put back `-out=plan.tfplan -no-color | tee "${TF_ROOT}/plan.txt"`."""
    bad = [(n, m.group(0)) for n, t in _texts().items()
           for m in re.finditer(r"-out=\S*plan\.tfplan[^\n]*\|\s*tee\b[^\n]*", t)]
    assert not bad, f"a plan's text goes to a public log: {bad}"


#: The only patterns a grep over plan.txt may print with: the summary line,
#: optionally with each error's headline (a failed plan's first lines).
ALLOWED_GREPS = (
    "grep -E '^(Plan:|No changes)'",
    "grep -E '^(Plan:|No changes)|^((│|\\|) )?Error:'",
)


def test_plan_text_is_printed_only_through_its_summary_line() -> None:
    """MUTATION: `tail -c 60000 terraform/infra/plan.txt` in the summarise step,
    or a grep over plan.txt with any wider pattern."""
    bad = []
    for name, text in _texts().items():
        for line in text.splitlines():
            if "plan.txt" not in line or re.search(r"(>|-out=)\s*\"?\$\{?\w*\}?/?[\w/]*plan\.txt", line):
                continue
            if re.search(r"\b(cat|tail|head|less|sed)\b[^|]*plan\.txt", line):
                bad.append((name, line.strip()))
            elif re.search(r"\bgrep\b", line) and not any(f"{g} " in line for g in ALLOWED_GREPS):
                bad.append((name, line.strip()))
    assert not bad, f"plan text printed beyond its summary line: {bad}"


def test_no_plan_text_artifact_is_uploaded() -> None:
    """MUTATION: put back the `upload the human-readable plan` step."""
    bad = [n for n, t in _texts().items() if re.search(r"tfplan-text|path:\s*\S*plan\.(txt|json|tfplan)\b", t)]
    assert not bad, f"a plan file is uploaded as an artifact, which is public: {bad}"


def test_a_plans_stderr_goes_to_the_file_too() -> None:
    """A failed plan's diagnostics (stderr) quote the offending values, so the
    plan's stderr is redirected with its stdout, and only error headlines are printed.
    MUTATION: drop `2>&1` from the release-apply action's plan line."""
    plans = [(n, line.strip()) for n, t in _texts().items() for line in t.splitlines()
             if re.search(r"-out=\S*plan\.tfplan", line)]
    assert len(plans) >= 2, plans
    bad = [(n, line) for n, line in plans if not re.search(r"plan\.txt\"?\s+2>&1", line)]
    assert not bad, f"a plan's stderr reaches the public log: {bad}"


def test_the_summary_masks_an_address_shaped_resource_key(tmp_path) -> None:
    """The run summary lists changing resources' addresses, and an address can
    carry an email as its key; the summary prints it masked.
    MUTATION: drop the `gsub` from terraform.yml's summarise step."""
    import json
    import shutil
    import subprocess

    import pytest

    if shutil.which("jq") is None:
        pytest.skip("jq is required")
    text = (ROOT / ".github" / "workflows" / "terraform.yml").read_text(encoding="utf-8")
    m = re.search(r"jq -r '(\.resource_changes\[\]\?.*?)' terraform/infra/plan\.json", text)
    assert m, "terraform.yml's summarise step has no jq over plan.json"
    plan = {"resource_changes": [
        {"address": 'module.m.google_monitoring_notification_channel.email["' + "someone" + "@example.com" + '"]',
         "change": {"actions": ["delete"]}},
        {"address": "data.google_project.p", "change": {"actions": ["read"]}},
    ]}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    out = subprocess.run(["jq", "-r", m.group(1), str(tmp_path / "plan.json")],
                         capture_output=True, text=True, check=True).stdout
    assert "@example.com" not in out, out
    assert 'delete module.m.google_monitoring_notification_channel.email["<an address>"]' in out, out
    assert "data.google_project.p" not in out, out
