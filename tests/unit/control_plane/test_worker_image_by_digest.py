"""The dispatcher names a worker image by DIGEST, never by a tag.

THE DEFECT THIS PINS. `scheduler.dispatch.image_uri` built every worker image
reference as `<registry>/<image>:<WORKER_IMAGE_TAG>`, for both backends. A tag
is resolved when the image is PULLED, so what ran was whatever the tag pointed
at on that node at that moment -- and the GKE template pulls with
`IfNotPresent`, so a node holding a cached layer for an older push of the same
tag ran the older code. docs/security.md said so in as many words: "Closing it
means the dispatcher naming a digest."

The deployment now hands the scheduler one digest per runner image
(`WORKER_IMAGE_REFS`, a JSON object written by terraform/infra/locals.tf from
the promotion manifest), and these tests hold the dispatcher to it:

  * both backends put the digest on the container, not the tag;
  * a profile whose image has no digest in a deployment that pins digests is a
    DISPATCH ERROR, not a quiet fall back to the tag -- a fallback there is the
    exact silent path this closes;
  * a value that is not digest-pinned is refused when the settings are read,
    so a scheduler configured with a tag fails at start rather than at the
    first dispatch, minutes later, after a lease has been taken;
  * a deployment that sets nothing keeps the old tag behaviour, which is what
    the local emulator loop and every existing fixture rely on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from scheduler.dispatch import (
    CloudRunJobDispatcher,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    image_uri,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, REGION, scheduler_settings

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
HOST = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images"

BASE_DIGEST = "sha256:" + "a" * 64
BROWSER_DIGEST = "sha256:" + "b" * 64
REFS = {
    "agent-runtime-base": f"{HOST}/agent-runtime-base@{BASE_DIGEST}",
    "agent-runtime-browser": f"{HOST}/agent-runtime-browser@{BROWSER_DIGEST}",
}


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def make_task(profile_name: str) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_digest1",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        timeout_seconds=1800,
    )


def make_lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_d1",
        task_id=task.id,
        attempt_id="att_d1",
        tenant_id=task.tenant_id,
        generation=1,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )


class FakeOperation:
    def __init__(self) -> None:
        self.metadata = type("Meta", (), {"name": "projects/p/locations/l/jobs/j/executions/e"})()

    def result(self, timeout=None):
        return self.metadata


class FakeJobsClient:
    def __init__(self) -> None:
        self.created: list = []

    def get_job(self, request):
        from google.api_core import exceptions as gexc

        raise gexc.NotFound(request.name)

    def create_job(self, request):
        self.created.append(request.job)
        return FakeOperation()

    def run_job(self, request):
        return FakeOperation()


class FakeBatchApi:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))
        return {"metadata": {"name": body["metadata"]["name"]}}


# -- both backends ----------------------------------------------------------


def test_a_cloud_run_job_the_dispatcher_creates_names_the_digest(tenant):
    settings = scheduler_settings(worker_image_refs=REFS)
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    task = make_task("claude-code")

    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )

    image = client.created[0].template.template.containers[0].image
    assert image == REFS["agent-runtime-base"], (
        f"the Cloud Run job was created with {image!r}; a tag is resolved at pull "
        "time, so the code that runs is whatever the tag points at then"
    )
    assert "@sha256:" in image and ":test" not in image


def test_a_gke_job_names_the_digest(tenant):
    settings = scheduler_settings(worker_image_refs=REFS)
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    )
    task = make_task("browser")

    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )

    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == REFS["agent-runtime-browser"], (
        f"the GKE Job names {container['image']!r}; with imagePullPolicy "
        "IfNotPresent a node holding an older layer for that tag runs the older code"
    )


def test_each_profile_gets_its_own_images_digest():
    settings = scheduler_settings(worker_image_refs=REFS)
    assert image_uri(settings, RUNNER_PROFILES["mock"]) == REFS["agent-runtime-base"]
    assert image_uri(settings, RUNNER_PROFILES["browser"]) == REFS["agent-runtime-browser"]


# -- no silent fallback ---------------------------------------------------


def test_a_profile_with_no_digest_is_refused_rather_than_run_at_a_tag(tenant):
    only_base = {"agent-runtime-base": REFS["agent-runtime-base"]}
    settings = scheduler_settings(worker_image_refs=only_base)
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    )
    task = make_task("browser")

    with pytest.raises(DispatchError) as caught:
        dispatcher.dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
        )

    assert caught.value.code == "worker_image_not_pinned"
    assert "agent-runtime-browser" in str(caught.value)
    assert api.created == [], "nothing may reach the cluster at a tag"


def test_without_any_digests_the_local_tag_behaviour_is_unchanged():
    settings = scheduler_settings()
    assert image_uri(settings, RUNNER_PROFILES["mock"]) == f"{HOST}/agent-runtime-base:test"


# -- read from the environment --------------------------------------------


def _from_env(monkeypatch, value: str | None) -> SchedulerSettings:
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", REGION)
    if value is None:
        monkeypatch.delenv("WORKER_IMAGE_REFS", raising=False)
    else:
        monkeypatch.setenv("WORKER_IMAGE_REFS", value)
    return SchedulerSettings.from_env()


def test_worker_image_refs_is_read_from_the_environment(monkeypatch):
    import json

    settings = _from_env(monkeypatch, json.dumps(REFS))
    assert dict(settings.worker_image_refs) == REFS


def test_an_unset_worker_image_refs_reads_as_no_digests(monkeypatch):
    settings = _from_env(monkeypatch, None)
    assert dict(settings.worker_image_refs) == {}


@pytest.mark.parametrize(
    "bad",
    [
        # a tag, which is the thing being removed
        '{"agent-runtime-base": "%s/agent-runtime-base:7c5276212251"}' % HOST,
        # a tag AND a digest: the tag is ignored by the runtime and misleads a reader
        '{"agent-runtime-base": "%s/agent-runtime-base:dev@%s"}' % (HOST, BASE_DIGEST),
        # a truncated digest
        '{"agent-runtime-base": "%s/agent-runtime-base@sha256:abc"}' % HOST,
        # the digest of a DIFFERENT image under this image's name
        '{"agent-runtime-base": "%s/agent-runtime-browser@%s"}' % (HOST, BROWSER_DIGEST),
        # not an object
        '["%s/agent-runtime-base@%s"]' % (HOST, BASE_DIGEST),
        "not json",
    ],
)
def test_a_value_that_is_not_digest_pinned_is_refused_at_start(monkeypatch, bad):
    with pytest.raises(ValueError, match="WORKER_IMAGE_REFS"):
        _from_env(monkeypatch, bad)
