"""The plan guard refuses a plan that replaces a log-based metric (#1822).

Measured 2026-10-22 on release 39712409927 (02:37Z): PR 1911 added
`submitter_service` to `metric_descriptor.labels` of
`module.monitoring.google_logging_metric.events["generation_fenced"]`. A new
label forces replacement, so the plan was delete-then-create; GCP answered
"Error 400: Cannot delete metric swarm/generation-fenced. That metric is still
used in an alerting policy." and every release from main failed until a hand
`gcloud logging metrics update` added the label in place. The trap stays for
the next label added to any metric, so `scripts/lib/plan-guard.sh` now refuses
the plan before apply, naming the resource and the live step that avoids it.

The guard runs from a copy of scripts/lib under tmp_path: its verdict is
written to a fixed path under build/, which concurrent xdist workers would
otherwise share.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
ADDRESS = 'module.monitoring.google_logging_metric.events["generation_fenced"]'

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _metric(actions: list[str], *, address: str = ADDRESS, rtype: str = "google_logging_metric") -> dict:
    values = {"name": "swarm/generation-fenced", "project": "saga-agents-staging",
              "filter": 'jsonPayload.event="generation_fenced"'}
    return {
        "address": address, "type": rtype,
        "change": {"actions": actions,
                   "before": None if actions == ["create"] else values,
                   "after": None if actions == ["delete"] else values},
    }


def _guard(tmp_path: Path, *changes: dict, mode: str = "apply") -> subprocess.CompletedProcess:
    root = tmp_path / "repo"
    shutil.copytree(LIB, root / "scripts" / "lib")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"format_version": "1.2", "resource_changes": list(changes)}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "GCP_", "PROJECT"))}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp_path / "no.env"), PROJECT_ID="saga-agents-staging")
    return subprocess.run([str(root / "scripts" / "lib" / "plan-guard.sh"), "--plan", str(plan), "--mode", mode],
                          capture_output=True, text=True, timeout=60, env=env, check=False)


@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
def test_a_plan_replacing_a_log_based_metric_is_refused_naming_it(tmp_path, actions):
    result = _guard(tmp_path, _metric(actions))
    # MUTATION: drop the metric-replace check from the main path -- the plan exits 0.
    assert result.returncode == 2, result.stderr
    # MUTATION: a refusal that does not list the addresses -- nobody knows which metric.
    assert ADDRESS in result.stderr, result.stderr
    # MUTATION: drop the remedy from the message -- the refusal says no way through.
    assert "gcloud logging metrics update" in result.stderr, result.stderr


def test_an_in_place_metric_update_is_allowed(tmp_path):
    """The control, and the way through: once the label is added live, the next
    plan updates the metric in place (or not at all), and that applies."""
    result = _guard(tmp_path, _metric(["update"]))
    # MUTATION: refuse any change to a metric -- the in-place update is refused too.
    assert result.returncode == 0, result.stderr
    assert "no log-based metric is replaced" in result.stderr, result.stderr


def test_deleting_a_metric_or_replacing_another_type_is_not_this_rule(tmp_path):
    result = _guard(tmp_path, _metric(["delete"]), mode="destroy")
    # MUTATION: match on "delete" alone -- a destroy plan's metric deletion is refused.
    assert result.returncode == 0, result.stderr
    result = _guard(tmp_path / "other", _metric(["delete", "create"], address="module.monitoring.google_monitoring_alert_policy.x",
                                                rtype="google_monitoring_alert_policy"))
    # MUTATION: drop the type filter -- every replace in a plan is refused.
    assert result.returncode == 0, result.stderr


def test_the_self_test_covers_the_metric_replace_rule():
    """`make test` and application.yml run `plan-guard.sh --self-test`."""
    proc = subprocess.run([str(LIB / "plan-guard.sh"), "--self-test"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # MUTATION: delete the self-test's metric cases -- these lines are not printed.
    assert "a replaced log-based metric is refused" in proc.stderr, proc.stderr
    assert "an in-place log-based metric update is allowed" in proc.stderr, proc.stderr
