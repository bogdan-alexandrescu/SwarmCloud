"""Schedules lane S9: the seed that replaced lane SWEEP's job, and the job gone.

docs/schedules.md §8.1-§8.2. The owner's decision of 2026-10-11 narrowed S9
to its reversible half: create the schedules and PAUSE SWEEP's Cloud
Scheduler job. The second change (SWEEP-RM, owner-approved 2026-10-12, the
7-day watch waived) deleted the job, its route, SWEEP_ENABLED and the tenant
document's `issue_sweep` settings; the last tests here hold that they stay
deleted.

`docs/schedules-migration.json` holds the `POST /v1/schedules` bodies an
operator posts. Each one is built here through `schedules.build_schedule`,
the function the create route calls, as an ordinary member, so a body the API
would refuse fails CI rather than the operator at the console.

Pure: no Firestore, no directory, no emulator.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_api import auth, schedules

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED = REPO_ROOT / "docs" / "schedules-migration.json"
JOBS_TF = REPO_ROOT / "terraform" / "modules" / "scheduler" / "jobs.tf"
SCHEDULER_VARS = REPO_ROOT / "terraform" / "modules" / "scheduler" / "variables.tf"
INFRA = REPO_ROOT / "terraform" / "infra"
#: SWEEP's job ran at :07 and :37 (the deleted var.issue_sweep_schedule's
#: default, owner decision 2026-10-08); the schedule kept that cadence.
SWEEP_CRON = "7,37 * * * *"

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MEMBER = "member@example.com"


def _seed() -> list[dict]:
    return json.loads(SEED.read_text())["schedules"]


def _built() -> list[dict]:
    """Every body, created in order in its tenant, as a member (not an admin)."""
    docs: list[dict] = []
    for entry in _seed():
        same_tenant = [d for d in docs if d["tenant_id"] == entry["tenant_id"]]
        body = schedules.parse_create(entry["body"])
        docs.append(
            schedules.build_schedule(
                body,
                tenant_id=entry["tenant_id"],
                actor=MEMBER,
                now=NOW,
                registered_repo_ids=[],
                existing_names=[d["name"] for d in same_tenant],
                existing_count=len(same_tenant),
            )
        )
    return docs


def test_every_seeded_body_is_one_the_create_route_accepts_from_a_member():
    docs = _built()
    assert len(docs) == len(_seed()) >= 2
    # The idempotency key is the route's, so a repeated post returns the first
    # schedule instead of a second one (§7.1). Each body carries its own.
    keys = [entry["body"].get("client_request_id") for entry in _seed()]
    assert all(keys) and len(set(keys)) == len(keys)


def test_the_sweep_schedule_keeps_sweeps_cadence_scope_and_values():
    """§8.1 step 2: one `issue-sweep` schedule per tenant that had SWEEP on (dev
    turned it on for `eng`, dev.tfvars `enable_issue_sweep`), at SWEEP's
    cadence, over every registered repository, with SWEEP's cap and fix rounds."""
    sweeps = [d for d in _built() if d["type"] == "issue-sweep"]
    assert [d["tenant_id"] for d in sweeps] == ["eng"]
    sweep = sweeps[0]
    assert sweep["cron"] == SWEEP_CRON
    assert sweep["timezone"] == "UTC"
    assert sweep["scope"] == {"mode": "all", "repo_ids": []}
    assert sweep["params"]["max_live_runs"] == 8
    assert sweep["params"]["fix_rounds"] == 2
    assert sweep["params"]["territory_guard"] is True


def test_the_sweep_schedule_starts_at_the_default_gate_merge_asks_first():
    """§8.1 step 2: plan automatic, merge `approve` -- stricter than SWEEP's
    unattended merge, which is now SD3's audited switch, never a create."""
    sweep = next(d for d in _built() if d["type"] == "issue-sweep")
    assert sweep["gate"]["plan"] == "auto"
    assert sweep["gate"]["merge"] == "approve"
    assert sweep["params"]["merge"] == "approve"
    assert sweep["params"]["plan_approval"] == "auto"
    for entry in _seed():
        assert (entry["body"].get("gate") or {}).get("merge") != "auto"


def test_the_sweep_schedule_is_created_paused_so_its_first_firing_is_a_dry_run():
    """§8.1 step 2: "Its first firing runs dry (§2.8)". Created paused, it fires
    nothing on its cron until the operator's `:run {"dry_run": true}` has been
    compared with SWEEP's last sweep and the schedule is resumed."""
    sweep = next(d for d in _built() if d["type"] == "issue-sweep")
    assert sweep["state"] == "paused"
    assert sweep["next_run_at"] is None


def test_a_second_schedule_of_index_refresh_or_observer_is_seeded():
    """S9's acceptance (§9): "`repo-index-refresh` or `observer` fires from a
    second schedule"."""
    others = [d for d in _built() if d["type"] in ("repo-index-refresh", "observer")]
    assert others and all(d["state"] == "enabled" for d in others)


def test_sweeps_job_is_deleted_with_its_schedule_variable():
    """The second S9 change (2026-10-12): no `issue_sweep` Cloud Scheduler job
    and no `issue_sweep_schedule` variable. The control: its neighbour, the
    stranded-PR sweep, is still declared in the same file."""
    jobs = JOBS_TF.read_text()
    assert 'resource "google_cloud_scheduler_job" "issue_sweep"' not in jobs
    assert 'resource "google_cloud_scheduler_job" "stranded_pr_sweep"' in jobs
    assert 'variable "issue_sweep_schedule"' not in SCHEDULER_VARS.read_text()
    assert "/v1/admin/issues/sweep" not in jobs


@pytest.mark.parametrize("deleted", ["route", "switch", "variable"])
def test_the_second_change_deleted_the_route_and_the_switch(deleted: str):
    """The deletions the 2026-10-11 decision held back, made on 2026-10-12."""
    if deleted == "route":
        assert ("POST", "/v1/admin/issues/sweep") not in auth.ROLLUP_SWEEPER_ROUTES
        assert ("POST", "/v1/admin/runs/advance") in auth.ROLLUP_SWEEPER_ROUTES
    elif deleted == "switch":
        assert "SWEEP_ENABLED" not in (INFRA / "locals.tf").read_text()
    else:
        assert 'variable "enable_issue_sweep"' not in (INFRA / "variables.tf").read_text()
        for tfvars in sorted((REPO_ROOT / "terraform" / "environments").glob("*/*.tfvars")):
            assert not re.search(r"^\s*enable_issue_sweep\s*=", tfvars.read_text(), re.M), tfvars
