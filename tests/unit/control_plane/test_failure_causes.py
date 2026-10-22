"""Root causes kept across `failure-review` firings (docs/design/failure-review.md §4's
fingerprint and validation, §5; lane FR-2 DIGEST, part of #1933).

Each test names the rule of the row it holds:

  * a fingerprint is stable across reruns;
  * the same failure in two overlapping windows is one failure (counted once);
  * §4: nothing validates that does not account for every failure exactly once;
  * §5: a new cause is filed once, a filed one commented, an open fix gets no
    second run, a fixed one that recurs one new run, two failed runs are held.

Offline: the in-memory Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import failurecauses as fc
from swarm_api.errors import ValidationFailed
from swarm_api.failurecauses import Action, Facts, FailureCauses

from .fakes import FakeFirestore

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SITE = "apps/reconciler/reconciler/detect.py::detect_stale_leases"
SIG = "lease silent for 912s (grace 300s, expired=True, dispatch_overdue=False)"


def cause_json(failures: list[str], **overrides: Any) -> dict[str, Any]:
    cause = {
        "fingerprint_of": {"component": "reconciler", "site": SITE,
                           "class": "oom_reclaimed_as_silent", "signature": SIG},
        "known_fingerprint": None,
        "failures": failures,
        "cause": "The reconciler's lease-silent reclaim fires before the OOM classifier reads the pod.",
        "evidence": [{"command": "rg -n 'lease silent' apps/reconciler", "result": "detect.py:88"}],
        "fix": "Read the pod's status before reclaiming; test_detect_oom first.",
        "files": ["apps/reconciler/reconciler/detect.py"],
        "hold": "none",
        "where": "platform",
    }
    cause.update(overrides)
    return cause


def rca(*causes: dict[str, Any], unexplained: list[dict[str, Any]] | None = None,
        firing_id: str = "fr_1") -> str:
    return json.dumps({"firing_id": firing_id, "causes": list(causes), "unexplained": unexplained or []})


# --------------------------------------------------------------------------
# The fingerprint
# --------------------------------------------------------------------------


def test_a_fingerprint_is_stable_across_reruns_and_figures():
    one = fc.fingerprint("reconciler", SITE, "oom_reclaimed_as_silent", SIG)
    # Recomputed (a rerun), with other figures, case and spacing: the same cause.
    two = fc.fingerprint(" Reconciler ", SITE, "OOM_reclaimed_as_silent",
                         "lease  silent for 18s (grace 600s, expired=False, dispatch_overdue=True)")
    # MUTATION: hash the raw signature (no `normalise`) and the figures split it.
    assert one == two
    # The design's shape: fr- and 12 hex of SHA-256.
    assert fc.FINGERPRINT.fullmatch(one)
    # MUTATION: leave a part out of the hash and a different site collides.
    assert one != fc.fingerprint("reconciler", "apps/reconciler/reconciler/loop.py::sweep",
                                 "oom_reclaimed_as_silent", SIG)
    assert one != fc.fingerprint("scheduler", SITE, "oom_reclaimed_as_silent", SIG)


def test_validation_gives_the_same_fingerprints_on_a_rerun():
    text = rca(cause_json(["f-001", "f-002"]))
    first, _ = fc.validate_rca(text, firing_id="fr_1", refs=["f-001", "f-002"])
    second, _ = fc.validate_rca(text, firing_id="fr_1", refs=["f-001", "f-002"])
    # MUTATION: a random or time-based component in the fingerprint.
    assert [c.fingerprint for c in first] == [c.fingerprint for c in second]
    assert first[0].fingerprint == fc.fingerprint("reconciler", SITE, "oom_reclaimed_as_silent", SIG)


def test_a_known_fingerprint_is_honoured_only_when_swarm_api_holds_it():
    held = "fr-" + "a" * 12
    text = rca(cause_json(["f-001"], known_fingerprint=held))
    causes, _ = fc.validate_rca(text, firing_id="fr_1", refs=["f-001"], known=[held])
    # MUTATION: ignore known_fingerprint and a rewording is filed twice.
    assert causes[0].fingerprint == held and causes[0].known
    causes, _ = fc.validate_rca(text, firing_id="fr_1", refs=["f-001"], known=[])
    # MUTATION: trust any known_fingerprint and an agent can attach to any record.
    assert causes[0].fingerprint != held and not causes[0].known


def test_markers_round_trip():
    fp = fc.fingerprint("a", "b", "c", "d")
    assert fc.marker_in("intro\n" + fc.marker(fp) + "\nbody") == fp
    assert fc.marker_in(fc.comment_marker(fp, "fr_9")) == fp
    # MUTATION: a loose pattern that reads any comment as a marker.
    assert fc.marker_in("<!-- failure-review fingerprint=nope -->") is None


# --------------------------------------------------------------------------
# §4: every failure accounted for exactly once
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (rca(cause_json(["f-001"])), "not accounted for: f-002"),
        (rca(cause_json(["f-001", "f-002"]), unexplained=[{"failure": "f-002", "tried": "read it"}]),
         "more than once: f-002"),
        (rca(cause_json(["f-001", "f-002", "f-009"])), "not in the digest: f-009"),
        (rca(cause_json(["f-001", "f-002"], evidence=[])), "evidence"),
        (rca(cause_json(["f-001", "f-002"], hold="maybe")), "hold"),
        (rca(cause_json(["f-001", "f-002"], where="elsewhere")), "where"),
        (rca(cause_json(["f-001", "f-002"]), firing_id="fr_other"), "another firing"),
        ("not json", "not JSON"),
    ],
)
def test_an_rca_that_does_not_account_for_every_failure_exactly_once_files_nothing(text, fragment):
    # MUTATION: drop any one check in `validate_rca` and its case validates.
    with pytest.raises(ValidationFailed) as raised:
        fc.validate_rca(text, firing_id="fr_1", refs=["f-001", "f-002"])
    assert fragment in str(raised.value)


def test_a_complete_rca_validates_with_its_vocabularies():
    causes, unexplained = fc.validate_rca(
        rca(cause_json(["f-001"], hold="iam"), unexplained=[{"failure": "f-002", "tried": "content not shared"}]),
        firing_id="fr_1", refs=["f-001", "f-002"])
    assert [c.hold for c in causes] == ["iam"] and unexplained[0].failure == "f-002"
    assert fc.HOLDS == ("none", "iam", "contract", "secrets", "tenancy", "shared", "cost")


# --------------------------------------------------------------------------
# §5: what to do with a cause
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "hold", "where", "facts", "action"),
    [
        (None, "none", "platform", Facts(), Action.FILE_AND_RUN),
        (None, "iam", "platform", Facts(), Action.FILE_HELD),
        (None, "none", "tenant_repo", Facts(), Action.REPORT),
        ({"issue": 5, "state": "open", "runs": [{"run_id": "r1"}]}, "none", "platform",
         Facts(live_run=True), Action.COMMENT),
        ({"issue": 5, "state": "fixing", "runs": [{"run_id": "r1"}]}, "none", "platform",
         Facts(open_pr=True), Action.COMMENT),
        ({"issue": 5, "state": "fixed", "runs": [{"run_id": "r1"}]}, "none", "platform",
         Facts(), Action.COMMENT_AND_RUN),
        ({"issue": 5, "state": "fixed", "runs": [{"run_id": "r1"}, {"run_id": "r2"}]}, "none",
         "platform", Facts(), Action.HOLD),
        ({"issue": 5, "state": "open", "runs": []}, "none", "platform",
         Facts(issue_closed="not_planned"), Action.COMMENT),
        ({"issue": 5, "state": "held", "runs": []}, "none", "platform", Facts(), Action.COMMENT),
        ({"issue": 5, "state": "open", "runs": []}, "secrets", "platform", Facts(), Action.HOLD),
    ],
)
def test_decide_files_once_comments_after_and_never_runs_twice_at_once(record, hold, where, facts, action):
    # MUTATION: any branch of `decide` reordered or removed changes one row.
    assert fc.decide(record, hold=hold, where=where, facts=facts) is action


# --------------------------------------------------------------------------
# The record: the same failure in two overlapping windows is one failure
# --------------------------------------------------------------------------


def _cause(failures: list[str]) -> fc.Cause:
    causes, _ = fc.validate_rca(rca(cause_json(failures)), firing_id="fr_1", refs=failures)
    return causes[0]


def test_the_same_failure_in_two_overlapping_windows_is_counted_once():
    db = FakeFirestore()
    store = FailureCauses(db, now=lambda: NOW)
    cause = _cause(["f-001", "f-002"])
    first = store.observe(cause, failure_ids=["task_a", "task_b"], firing_id="fr_1", title="t")
    assert first.created and first.new_failures == ["task_a", "task_b"]
    # The next firing's window overlaps by an hour: task_b again, task_c new.
    second = store.observe(cause, failure_ids=["task_b", "task_c"], firing_id="fr_2")
    # MUTATION: count every id observed and task_b is two failures.
    assert second.new_failures == ["task_c"] and not second.created
    record = store.get(cause.fingerprint)
    assert record["failures_seen"] == 3 and record["last_firing"] == "fr_2"
    # A retried firing that observes again changes nothing.
    again = store.observe(cause, failure_ids=["task_b", "task_c"], firing_id="fr_2")
    # MUTATION: no per-firing guard and a retry reads as a new sighting.
    assert again.new_failures == [] and not again.first_this_firing
    assert store.get(cause.fingerprint)["failures_seen"] == 3


def test_a_new_held_cause_is_recorded_held_and_a_tenant_cause_reported():
    store = FailureCauses(FakeFirestore(), now=lambda: NOW)
    held, _ = fc.validate_rca(rca(cause_json(["f-001"], hold="iam")), firing_id="fr_1", refs=["f-001"])
    obs = store.observe(held[0], failure_ids=["task_a"], firing_id="fr_1")
    assert obs.record["state"] == fc.HELD
    other = cause_json(["f-001"], where="tenant_repo")
    other["fingerprint_of"]["component"] = "tenant"
    theirs, _ = fc.validate_rca(rca(other), firing_id="fr_1", refs=["f-001"])
    # MUTATION: a tenant_repo cause recorded `open` would be filed and run here.
    assert store.observe(theirs[0], failure_ids=["task_x"], firing_id="fr_1").record["state"] == fc.REPORTED
    assert [r["fingerprint"] for r in store.known()] == [held[0].fingerprint]


def test_record_run_allows_two_fix_runs_and_is_idempotent_per_run():
    store = FailureCauses(FakeFirestore(), now=lambda: NOW + timedelta(minutes=1))
    cause = _cause(["f-001"])
    store.observe(cause, failure_ids=["task_a"], firing_id="fr_1")
    store.record_issue(cause.fingerprint, 1933)
    assert store.record_run(cause.fingerprint, "run_1", firing_id="fr_1")
    assert store.record_run(cause.fingerprint, "run_1", firing_id="fr_1")  # a retry
    assert store.record_run(cause.fingerprint, "run_2", firing_id="fr_2")
    # MUTATION: MAX_FIX_RUNS = 3, or no cap, and a third run starts.
    assert not store.record_run(cause.fingerprint, "run_3", firing_id="fr_3")
    record = store.get(cause.fingerprint)
    assert [r["run_id"] for r in record["runs"]] == ["run_1", "run_2"]
    assert record["state"] == fc.FIXING and record["issue"] == 1933
    assert fc.decide(record, hold="none", where="platform") is Action.HOLD
    store.set_state(cause.fingerprint, fc.HELD)
    assert store.get(cause.fingerprint)["state"] == fc.HELD
    with pytest.raises(ValidationFailed):
        store.set_state(cause.fingerprint, "gone")
    with pytest.raises(ValidationFailed):
        store.get("../tasks/x")
