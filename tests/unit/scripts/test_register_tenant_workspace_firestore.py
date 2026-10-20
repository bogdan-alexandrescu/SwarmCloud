"""The job-made workspace follows its tenant's Firestore switch (#1457, design B).

PR 1484 (ISO-IAM) added the per-tenant switch that takes a tenant worker off
direct Firestore: `worker_firestore_access` in terraform/modules/tenancy and
`--worker-firestore-access` in scripts/register-tenant.sh
(test_register_tenant_worker_firestore.py). The in-cloud workspace path --
`register-tenant.sh --workspace`, under the call guard -- never read it: A5
granted `{firestore_role}` from the guard's expectation and A9 required it, so
a job-made workspace always got the worker database role, whatever the switch
said, and cutting one over would have failed silently (lane ISO-WS-FIRESTORE).

The value travels the way every other name the job acts on does: swarm-api
writes `worker_firestore_access` on the workspace record, the guard's `expect`
copies it into the expectation, and the script reads it with `ws_expect`.

* absent or true: today's grant, exactly as before the switch existed;
* false: the expectation holds no `firestore_role`, so C5 admits no database
  binding for the worker; A5 makes none and removes nothing else; A9 fails a
  workspace whose worker still holds one;
* anything else is refused at `expect`: a typo never falls through to either
  answer, least of all to a grant the record meant to withhold.

Nothing here needs credentials, a network or a cluster: the fake world of
test_register_tenant_workspace.py stands behind the real guard and shims.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from .test_register_tenant_workspace import (  # noqa: F401  (fixtures, by name)
    GUARD,
    PROJECT,
    TENANT,
    WORKER,
    WORKSPACE,
    Job,
    _field,
    _from_ready,
    _members,
    _out,
    _record,
    _steps,
    created,
    job,
    ready_world,
)

FIRESTORE_ROLE = f"projects/{PROJECT}/roles/swarmTenantWorkerFirestore"
TELEMETRY = ("roles/logging.logWriter", "roles/monitoring.metricWriter")
MEMBER = f"serviceAccount:{WORKER}"
REFUSED = 86


def _access(value: bool) -> dict:
    return {"worker_firestore_access": {"booleanValue": value}}


def _set_access(world: dict, value: bool | None) -> None:
    fields = world["firestore"][f"workspaces/{TENANT}"]["fields"]
    if value is None:
        fields.pop("worker_firestore_access", None)
    else:
        fields.update(_access(value))


def _drop_firestore_role(world: dict) -> None:
    policy = world["project_policy"]
    policy["bindings"] = [b for b in policy.get("bindings", []) if b["role"] != FIRESTORE_ROLE]


def _expectation(job: Job, record: dict) -> subprocess.CompletedProcess[str]:
    job.init_guard()
    path = job.tmp / "record.json"
    # As A1 reads it: a Firestore document, with its name.
    path.write_text(json.dumps({"name": f"projects/{PROJECT}/databases/swarm/documents/workspaces/{TENANT}",
                                **record}))
    return subprocess.run([str(GUARD), "expect", "--record", str(path)], env=job.env,
                          capture_output=True, text=True, timeout=120, check=False)


def _firestore_grants(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls
            if c[:2] == ["projects", "add-iam-policy-binding"] and FIRESTORE_ROLE in c]


# ---------------------------------------------------------------------------
# the expectation: what the guard admits is what the record says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra,granted", [({}, True), (_access(True), True), (_access(False), False)],
                         ids=["absent", "true", "false"])
def test_the_expectation_carries_the_records_switch(job: Job, extra: dict, granted: bool) -> None:
    done = _expectation(job, _record(**extra))
    assert done.returncode == 0, done.stderr
    expect = json.loads(job.expect.read_text())
    # MUTATION: write `worker_firestore_access` into the expectation from
    # anything but the record (or drop it) and this goes red.
    assert expect["worker_firestore_access"] is granted
    # MUTATION: keep `firestore_role` in a false expectation -- the one name C5
    # admits the database binding by -- and this goes red for "false".
    assert ("firestore_role" in expect) is granted
    if granted:
        assert expect["firestore_role"] == FIRESTORE_ROLE


@pytest.mark.parametrize("value", [{"stringValue": "false"}, {"stringValue": "no"}, {"integerValue": "0"}],
                         ids=["string-false", "string-no", "zero"])
def test_a_switch_that_is_not_a_boolean_gets_no_expectation(job: Job, value: dict) -> None:
    done = _expectation(job, _record(worker_firestore_access=value))
    # MUTATION: read anything but a boolean (or absence) as an answer -- e.g.
    # `.worker_firestore_access != false` -- and this goes red.
    assert done.returncode != 0, done.stdout
    assert "worker_firestore_access" in done.stderr
    assert json.loads(job.expect.read_text())["phase"] == "bootstrap"


# ---------------------------------------------------------------------------
# the guard: C5 admits the database binding only when the expectation says true
# ---------------------------------------------------------------------------


def _check_grant(job: Job, role: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GUARD), "check", "--", "gcloud", "projects", "add-iam-policy-binding", PROJECT,
         "--member", MEMBER, "--role", role, "--condition", "None", "--quiet"],
        env=job.env, capture_output=True, text=True, timeout=120, check=False)


def test_the_guard_refuses_the_database_grant_under_false(job: Job) -> None:
    assert _expectation(job, _record(**_access(False))).returncode == 0
    refused = _check_grant(job, FIRESTORE_ROLE)
    # MUTATION: leave `firestore_role` in a false expectation and C5 allows
    # this call: red.
    assert refused.returncode == REFUSED, refused.stdout + refused.stderr
    assert "C5" in refused.stderr
    # The telemetry roles are not the switch's: still admitted on the fallback.
    for role in TELEMETRY:
        Path(str(job.expect) + ".stop").unlink(missing_ok=True)
        allowed = _check_grant(job, role)
        assert allowed.returncode == 0, allowed.stderr
        assert allowed.stdout.strip() == "allow C5"


@pytest.mark.parametrize("extra", [{}, _access(True)], ids=["absent", "true"])
def test_the_guard_admits_the_database_grant_when_absent_or_true(job: Job, extra: dict) -> None:
    assert _expectation(job, _record(**extra)).returncode == 0
    allowed = _check_grant(job, FIRESTORE_ROLE)
    # MUTATION: drop `firestore_role` from every expectation and this goes red.
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "allow C5"


# ---------------------------------------------------------------------------
# A5: the grant
# ---------------------------------------------------------------------------


def test_absent_grants_the_database_role_as_today(created: dict) -> None:
    assert created["returncode"] == 0, created["stderr"][-6000:]
    policy = created["world"]["project_policy"]
    # MUTATION: skip the database role whatever the switch says and this goes red.
    assert MEMBER in _members(policy, FIRESTORE_ROLE)
    for role in TELEMETRY:
        assert MEMBER in _members(policy, role)


def test_false_makes_no_firestore_grant_and_keeps_the_other_grants(job: Job) -> None:
    job.edit(lambda w: _set_access(w, False))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 0, out
    record = job.record()
    assert _field(record, "state") == "ready", out
    assert _steps(record)["A9"] == "done", out
    world = job.read()
    # MUTATION: grant `{firestore_role}` in A5's loop whatever the expectation
    # says (the bug this lane closes) and the guard stops the run, or, with the
    # role kept in the expectation, the binding appears: red either way.
    assert _firestore_grants(job.calls("gcloud")) == []
    assert MEMBER not in _members(world["project_policy"], FIRESTORE_ROLE)
    # Nothing else changes: the telemetry roles and the bucket grants are made.
    for role in TELEMETRY:
        assert MEMBER in _members(world["project_policy"], role)
    assert MEMBER in _members(world["bucket_policy"], "roles/storage.objectViewer")
    assert MEMBER in _members(world["bucket_policy"], "roles/storage.objectUser")
    assert "worker database role withheld" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# A9: the outcome, both ways
# ---------------------------------------------------------------------------


def _verify(job: Job) -> subprocess.CompletedProcess[str]:
    return job.run("--workspace", WORKSPACE, "--mode", "verify")


def test_verify_fails_a_false_workspace_that_still_holds_the_role(job: Job, ready_world: dict) -> None:
    world = copy.deepcopy(ready_world)
    assert MEMBER in _members(world["project_policy"], FIRESTORE_ROLE)
    _set_access(world, False)
    _from_ready(job, world)
    proc = _verify(job)
    out = _out(proc)
    # MUTATION: let A9 skip the database role under false (or check only that
    # the telemetry roles are there) and a worker that kept its grant passes: red.
    assert proc.returncode == 1, out
    record = job.record()
    assert _field(record, "state") == "failed"
    assert (_field(record, "failure", "step"), _field(record, "failure", "code"),
            _field(record, "failure", "object")) == ("A9", "VERIFY_FAILED", "project grant")
    # The owner's step, named; the job itself removes nothing (C9).
    assert "remove-iam-policy-binding" in proc.stderr
    assert not any("remove-iam-policy-binding" in c for c in job.calls("gcloud"))


def test_verify_fails_a_false_workspace_holding_a_conditioned_database_role(
        job: Job, ready_world: dict) -> None:
    world = copy.deepcopy(ready_world)
    _drop_firestore_role(world)
    world["project_policy"]["bindings"].append({
        "role": "roles/datastore.user", "members": [MEMBER],
        "condition": {"title": "t", "expression": "true"}})
    _set_access(world, False)
    _from_ready(job, world)
    proc = _verify(job)
    # MUTATION: check only the custom role's unconditioned binding under false,
    # and a predefined or conditioned database role passes: red.
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "project grant"


def test_verify_passes_a_false_workspace_whose_worker_holds_no_firestore_role(
        job: Job, ready_world: dict) -> None:
    world = copy.deepcopy(ready_world)
    _drop_firestore_role(world)
    _set_access(world, False)
    _from_ready(job, world)
    proc = _verify(job)
    out = _out(proc)
    # MUTATION: require the database role whatever the switch says and this
    # goes red: a cut-over workspace could never verify.
    assert proc.returncode == 0, out
    assert _field(job.record(), "state") == "ready"
    assert _steps(job.record())["A9"] == "done"


@pytest.mark.parametrize("value", [None, True], ids=["absent", "true"])
def test_verify_still_requires_the_role_when_absent_or_true(
        job: Job, ready_world: dict, value: bool | None) -> None:
    world = copy.deepcopy(ready_world)
    _drop_firestore_role(world)
    _set_access(world, value)
    _from_ready(job, world)
    proc = _verify(job)
    # MUTATION: drop the database role from A9's true check and this goes red.
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "project grant"
