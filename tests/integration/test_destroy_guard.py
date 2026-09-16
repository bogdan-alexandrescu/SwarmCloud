"""Independent rehearsal of the destroy guard, driving the real jq filter.

`scripts/destroy.sh --self-test` already covers deny-listed neighbours, unlabelled
resources, unlabelable types and foreign projects. This file exists to cover what that
fixture does NOT: a near-miss label value, and failing closed on unusable input.

Why bother: the deploy runs unattended against a SHARED project holding a live 4-node
cluster `agents-staging`, VPC `agents-staging-vpc`, and service accounts for promptlab,
crawler, aipipeline, external-secrets and tournament-digest. The guard is the only thing
between a bad plan and another team's production.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD_JQ = REPO / "scripts" / "lib" / "destroy-guard.jq"
DESTROY = REPO / "scripts" / "destroy.sh"
PROJECT = "saga-agents-staging"

# The real neighbours in the shared project.
DENY = [
    "agents-staging", "agents-staging-vpc", "agents-staging-subnet",
    "promptlab-runner", "promptlab-deployer", "crawler", "aipipeline",
    "external-secrets", "tournament-digest", "staging-gke-nodes",
    "api-service", "publisher", "saga-storage-ro", "saga-storage-rw",
]
UNLABELABLE = [
    "google_service_account", "google_service_account_key",
    "google_project_iam_member", "google_secret_manager_secret_version",
    "random_id", "random_string", "null_resource",
]

pytestmark = pytest.mark.skipif(
    not GUARD_JQ.exists(), reason="scripts/lib/destroy-guard.jq not built yet"
)


def run_guard(resource_changes: list[dict], tmp_path: Path) -> dict:
    fixture = tmp_path / "plan.json"
    fixture.write_text(json.dumps({"resource_changes": resource_changes}))
    proc = subprocess.run(
        ["jq", "-f", str(GUARD_JQ),
         "--argjson", "deny", json.dumps(DENY),
         "--argjson", "allow_types", json.dumps(UNLABELABLE),
         "--arg", "project", PROJECT,
         str(fixture)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"guard jq failed: {proc.stderr}"
    return json.loads(proc.stdout)


def deletion(address: str, type_: str, **before) -> dict:
    before.setdefault("project", PROJECT)
    return {"address": address, "type": type_,
            "change": {"actions": ["delete"], "before": before, "after": None}}


def addresses(verdict: dict, key: str) -> set[str]:
    return {e["address"] for e in verdict.get(key, [])}


def test_near_miss_label_value_is_an_offender(tmp_path):
    """`managed-by=other-terraform` must NOT pass.

    A substring or truthiness check on the label would let this through and delete
    another team's resource. The self-test fixture never exercises a wrong VALUE.
    """
    verdict = run_guard([
        deletion("ours", "google_storage_bucket",
                 name="saga-agents-staging-swarm-artifacts",
                 labels={"managed-by": "swarm-terraform"}),
        deletion("near_miss", "google_storage_bucket",
                 name="someone-elses-bucket",
                 labels={"managed-by": "other-terraform"}),
    ], tmp_path)
    assert "near_miss" in addresses(verdict, "offenders"), (
        "GUARD FAILED OPEN on managed-by=other-terraform — a wrong label value was "
        f"treated as ours. Verdict: {json.dumps(verdict)[:400]}"
    )
    assert "ours" not in addresses(verdict, "offenders")


def test_label_key_present_but_empty_is_an_offender(tmp_path):
    verdict = run_guard([
        deletion("empty_label", "google_storage_bucket", name="x", labels={"managed-by": ""}),
    ], tmp_path)
    assert "empty_label" in addresses(verdict, "offenders")


def test_no_labels_key_at_all_is_an_offender(tmp_path):
    verdict = run_guard([
        deletion("no_labels", "google_storage_bucket", name="x"),
    ], tmp_path)
    assert "no_labels" in addresses(verdict, "offenders")


@pytest.mark.parametrize("name", [
    "promptlab-runner", "aipipeline", "external-secrets", "tournament-digest",
])
def test_every_real_neighbour_is_caught_even_if_mislabelled(name, tmp_path):
    """A neighbour must be caught by the deny-list even when it carries OUR label.

    Defence in depth: if a bad import ever pulled a foreign resource into our state
    and it inherited our label, the deny-list is the remaining backstop.
    """
    verdict = run_guard([
        deletion("intruder", "google_service_account",
                 name=name, email=f"{name}@{PROJECT}.iam.gserviceaccount.com",
                 labels={"managed-by": "swarm-terraform"}),
    ], tmp_path)
    caught = addresses(verdict, "denylist_hits")
    assert "intruder" in caught, (
        f"GUARD FAILED OPEN: {name} carried our label and slipped past the deny-list"
    )


def test_guard_fails_closed_on_unparseable_plan(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    proc = subprocess.run(
        ["jq", "-f", str(GUARD_JQ), "--argjson", "deny", json.dumps(DENY),
         "--argjson", "allow_types", json.dumps(UNLABELABLE),
         "--arg", "project", PROJECT, str(bad)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "guard parsed invalid JSON as a safe plan"


@pytest.mark.skipif(not DESTROY.exists(), reason="destroy.sh not built yet")
def test_shipped_self_test_passes(tmp_path):
    """The guard's own self-test must pass, or we do not deploy at all."""
    proc = subprocess.run(
        ["bash", str(DESTROY), "--self-test"],
        capture_output=True, text=True, cwd=REPO, timeout=180,
        env={**os.environ, "PROJECT_ID": PROJECT, "REGION": "us-central1",
             "ENVIRONMENT": "dev"},
    )
    assert proc.returncode == 0, f"destroy.sh --self-test FAILED:\n{proc.stdout}\n{proc.stderr}"
