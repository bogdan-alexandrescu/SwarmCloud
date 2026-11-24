"""A Terraform plan's text never reaches a public run summary.

This repository's Actions logs, run summaries and artifacts are public, and a
plan's text prints attribute values from the private tfvars (members, domains,
addresses). terraform.yml's summarise step prints the plan's summary line and
lists resource addresses and actions from `plan.json`. The owner's pick,
2026-11-14. This runs that step's shell, read through `ci_run_blocks`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from .ci_run_blocks import run_block


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
def test_the_summary_prints_no_plan_text_and_masks_an_address_shaped_resource_key(tmp_path) -> None:
    """The run summary lists changing resources' addresses, and an address can
    carry an email as its key; the summary prints it masked, and of plan.txt
    only the summary line.
    MUTATION: drop the `gsub` from terraform.yml's summarise step, or print
    plan.txt with `tail`."""
    infra = tmp_path / "terraform" / "infra"
    infra.mkdir(parents=True)
    (infra / "plan.txt").write_text('  + member = "a-private-value"\nPlan: 0 to add, 0 to change, 1 to destroy.\n')
    plan = {"resource_changes": [
        {"address": 'module.m.google_monitoring_notification_channel.email["' + "someone" + "@example.com" + '"]',
         "change": {"actions": ["delete"]}},
        {"address": "data.google_project.p", "change": {"actions": ["read"]}},
    ]}
    (infra / "plan.json").write_text(json.dumps(plan))
    script = re.sub(r"\$\{\{.*?\}\}", "dev", run_block("terraform.yml", "plan", "summarise"))
    summary = tmp_path / "summary.md"
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=tmp_path, capture_output=True, text=True,
                          env={"PATH": "/usr/local/bin:/usr/bin:/bin", "GITHUB_STEP_SUMMARY": str(summary)},
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    out = summary.read_text() + proc.stdout + proc.stderr
    assert "@example.com" not in out and "a-private-value" not in out, out
    assert "Plan: 0 to add, 0 to change, 1 to destroy." in out, out
    assert 'delete module.m.google_monitoring_notification_channel.email["<an address>"]' in out, out
    assert "data.google_project.p" not in out, out
