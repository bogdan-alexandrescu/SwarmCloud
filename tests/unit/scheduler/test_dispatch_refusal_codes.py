"""A 403 on GKE Job create is classified by the API server's reason (#1820).

On 2026-10-20 19:24-20:06Z the admission policy `swarm-worker-job-constraints`
refused every CPU-burst Job and all 54-56 refusals were coded
`gke_create_job_forbidden`, the code for an RBAC or namespace fault. A
Job-quota 403 read the same. Each shape the API server sends now has its own
code, so that the PLATFORM_REFUSED park (contract request 123,
docs/design/dispatch-refusals.md) can tell the platform's refusal from ours:

  * a ValidatingAdmissionPolicy denial and an admission webhook denial are
    `gke_admission_refused`;
  * a ResourceQuota refusal is `gke_job_quota_exceeded`;
  * an RBAC denial, which is also what a missing namespace looks like, and any
    403 text not recognised, keep `gke_create_job_forbidden` and its
    namespace diagnosis.

The Kubernetes client is faked with its own `ApiException`, carrying the body
the API server sends, so the classifier reads what production reads.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from kubernetes.client import ApiException

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import scheduler_settings  # noqa: E402

from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from scheduler.dispatch import (  # noqa: E402
    PLATFORM_REFUSAL_CODES,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    forbidden_code,
)

NOW = datetime(2026, 10, 20, 19, 56, tzinfo=timezone.utc)
TENANT = "eng"
NAMESPACE = f"swarm-tenant-{TENANT}"
PROFILE = "browser"
JOB = "swarm-task-1cdb7e05-g1"


def forbidden(message: str) -> ApiException:
    """A 403 as `kubernetes.client` raises it: the Status object on `.body`."""
    error = ApiException(status=403, reason="Forbidden")
    error.body = json.dumps({
        "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
        "message": message, "reason": "Forbidden",
        "details": {"name": JOB, "group": "batch", "kind": "jobs"}, "code": 403,
    })
    return error


#: The denial measured on 2026-10-20, as the API server words a
#: ValidatingAdmissionPolicy refusal.
ADMISSION_POLICY = (
    f'jobs.batch "{JOB}" is forbidden: ValidatingAdmissionPolicy '
    "'swarm-worker-job-constraints' with binding "
    "'swarm-worker-job-constraints-binding' denied request: requests must equal limits"
)
#: A webhook's refusal (GKE Warden on Autopilot answers this way).
ADMISSION_WEBHOOK = (
    'admission webhook "warden-validating.common-webhooks.networking.gke.io" '
    "denied the request: GKE Warden rejected the request because it violates "
    "one or more constraints."
)
#: The 201st Job in a namespace whose quota allows 200.
JOB_QUOTA = (
    f'jobs.batch "{JOB}" is forbidden: exceeded quota: swarm-tenant-quota, '
    "requested: count/jobs.batch=1, used: count/jobs.batch=200, "
    "limited: count/jobs.batch=200"
)
#: What a missing permission, and a missing namespace, look like.
RBAC = (
    'jobs.batch is forbidden: User "117405034245659033603" cannot create '
    f'resource "jobs" in API group "batch" in the namespace "{NAMESPACE}"'
)


class RefusingBatch:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise self.error


def dispatch(error: BaseException) -> DispatchError:
    profile = RUNNER_PROFILES[PROFILE]
    task = Task(
        id="task_1cdb7e05a1b2c3d4e5f6", tenant_id=TENANT, created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=PROFILE, resource_class=profile.resource_class,
        input={}, submitted_by="accept@example.com", provider=profile.provider,
        timeout_seconds=1800,
    )
    lease = Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_2c1a232102544ce8b634",
        tenant_id=TENANT, generation=1, pools=["global"], units=1,
        state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )
    tenant = Tenant(
        tenant_id=TENANT, kind="group", principal=f"{TENANT}@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-{TENANT}@p.iam.gserviceaccount.com",
        gcs_prefix=f"gs://p-swarm-artifacts/tenants/{TENANT}", namespace=NAMESPACE,
    )
    batch = RefusingBatch(error)
    dispatcher = GkeJobDispatcher(scheduler_settings(), target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=batch, identity="swarm-scheduler@p.example")
    with pytest.raises(DispatchError) as raised:
        dispatcher.dispatch(task=task, lease=lease, profile=profile, tenant=tenant)
    # A refusal is never sent twice inside one dispatch: only the quota 409
    # race is retried there.
    assert batch.calls == 1
    return raised.value


@pytest.mark.parametrize(("message", "code"), [
    (ADMISSION_POLICY, "gke_admission_refused"),
    (ADMISSION_WEBHOOK, "gke_admission_refused"),
    (JOB_QUOTA, "gke_job_quota_exceeded"),
    (RBAC, "gke_create_job_forbidden"),
    ("jobs.batch is forbidden: something no release has worded yet",
     "gke_create_job_forbidden"),
])
def test_each_403_shape_maps_to_its_code(message: str, code: str) -> None:
    # MUTATION: map every 403 to gke_create_job_forbidden (the code before
    # #1820) -- the admission and quota rows fail.
    assert dispatch(forbidden(message)).code == code


@pytest.mark.parametrize("message", [ADMISSION_POLICY, JOB_QUOTA])
def test_a_403_without_a_status_body_is_classified_from_its_text(message: str) -> None:
    # A transport wrapper or a plain exception carries no `.body`: the text is
    # the fallback, as it is for `_is_forbidden`.
    # MUTATION: read only the Status body -- these fall back to forbidden.
    assert forbidden_code(RuntimeError(f"(403) Reason: Forbidden {message}")) in \
        PLATFORM_REFUSAL_CODES


def test_a_platform_refusal_does_not_carry_the_namespace_diagnosis() -> None:
    raised = dispatch(forbidden(ADMISSION_POLICY))
    message = str(raised)
    # The operator meets this on the `dispatch failed` log line; the upstream
    # text, naming the policy and its rule, has to be on it.
    assert "swarm-worker-job-constraints" in message
    assert "requests must equal limits" in message
    assert "docs/design/dispatch-refusals.md" in message
    # MUTATION: keep the 403-means-404 sentence on a refusal that got past
    # authorisation -- the reader is sent at RBAC and the namespace, which is
    # where the 2026-09-23 investigations lost two days.
    assert "does not prove this is a permissions problem" not in message


def test_an_rbac_403_keeps_its_namespace_diagnosis() -> None:
    message = str(dispatch(forbidden(RBAC)))
    # MUTATION: give every 403 the refusal message -- a missing namespace is
    # no longer named as a possible cause.
    assert "does not prove this is a permissions problem" in message
    assert f"kubectl get ns {NAMESPACE}" in message


def test_only_the_two_platform_codes_are_platform_refusals() -> None:
    # MUTATION: add gke_create_job_forbidden to the set -- an RBAC fault would
    # be parked as the platform's refusal once PLATFORM_REFUSED is applied.
    assert PLATFORM_REFUSAL_CODES == {"gke_admission_refused", "gke_job_quota_exceeded"}
