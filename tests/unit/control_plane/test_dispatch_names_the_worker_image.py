"""Every dispatched worker is told the image it runs, and every worker image its commit (part of #2688).

On 2026-11-03 a visual-QA run's captures could not say whether the live gate
that refused their requests was the build carrying the change two releases had
deployed: the worker logged no image and no commit. `agent_worker.buildinfo`
reports `commit` from SWARM_GIT_SHA and `image` from SWARM_WORKER_IMAGE, and
the owner picked "add both" (2026-11-03): the dispatch sets the image, and
every worker image sets the commit.

What holds:

* the scheduler's WORKER_IMAGE_ENV is the name the worker reads;
* a Cloud Run execution override carries `image_uri` -- the image the Job it
  runs was given -- for every Cloud Run profile;
* a GKE pod's worker container carries its own `image`, for every GKE profile,
  and the live pod's display sidecar, where the gate runs, carries its own;
* every Dockerfile whose ENTRYPOINT is the worker turns GIT_SHA into
  SWARM_GIT_SHA after its last RUN, so the value is the image's own commit
  and not one inherited from the base it was built FROM.

MUTATIONS: drop the override's entry (the Cloud Run test names the profile);
drop the pod's or the sidecar's (the GKE test); delete one Dockerfile's ENV
line or move it above a RUN (the Dockerfile test names the file).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_worker import buildinfo
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    WORKER_IMAGE_ENV,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    image_uri,
)

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, FakeJobsClient, make_lease, make_task

IMAGES = Path(__file__).resolve().parents[3] / "images"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _profiles(backend: Backend) -> list[str]:
    names = sorted(n for n, p in RUNNER_PROFILES.items() if resolve_backend(p) is backend)
    if backend is Backend.GKE_AUTOPILOT:
        # claude-code is on GKE too (contract request 53), as test_gke_cache_volume sends it.
        names = sorted({"claude-code", *names})
    assert names, f"no {backend} profile; this would check nothing"
    return names


def test_the_scheduler_sets_the_name_the_worker_reads():
    assert WORKER_IMAGE_ENV == buildinfo.IMAGE_ENV


def test_a_cloud_run_execution_is_told_its_jobs_image(tenant):
    settings = scheduler_settings()
    missing: dict[str, str | None] = {}
    for name in _profiles(Backend.CLOUD_RUN_JOB):
        client = FakeJobsClient()
        task = make_task(name)
        profile = RUNNER_PROFILES[name]
        CloudRunJobDispatcher(settings, client=client).dispatch(
            task=task, lease=make_lease(task), profile=profile, tenant=tenant)
        env = {e.name: e.value for o in client.runs[0]["overrides"].container_overrides for e in o.env}
        job_image = client.created[0]["job"].template.template.containers[0].image
        if not (env.get(WORKER_IMAGE_ENV) == job_image == image_uri(settings, profile)):
            missing[name] = env.get(WORKER_IMAGE_ENV)
    assert not missing, f"a Cloud Run worker was not told the image its Job runs: {missing}"


@pytest.mark.parametrize("live", [False, True])
def test_a_gke_pods_containers_are_told_their_own_image(tenant, live):
    settings = scheduler_settings(live_browser_sidecar=live)
    wrong: dict[str, str] = {}
    for name in _profiles(Backend.GKE_AUTOPILOT):
        task = make_task(name)
        api = FakeBatchApi()
        GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api).dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES[name], tenant=tenant)
        spec = api.created[0][1]["spec"]["template"]["spec"]
        for container in spec["containers"] + spec.get("initContainers", []):
            if container["name"] not in {"worker", "display"}:
                continue
            values = [e.get("value") for e in container.get("env", []) if e["name"] == WORKER_IMAGE_ENV]
            if values != [container["image"]]:
                wrong[f"{name}/{container['name']}"] = repr(values)
    assert not wrong, f"a GKE container was not told the image it runs: {wrong}"


WORKER_DOCKERFILES = sorted(
    p for p in IMAGES.glob("*/Dockerfile")
    if '"python", "-m", "agent_worker"]' in p.read_text(encoding="utf-8")
)


def test_every_worker_image_is_found():
    names = {p.parent.name for p in WORKER_DOCKERFILES}
    assert {"agent-runtime-base", "agent-runtime-browser"} <= names, names


@pytest.mark.parametrize("dockerfile", WORKER_DOCKERFILES, ids=lambda p: p.parent.name)
def test_every_worker_image_records_its_own_commit(dockerfile):
    lines = [ln.strip() for ln in dockerfile.read_text(encoding="utf-8").splitlines()]
    stage = lines[max(i for i, ln in enumerate(lines) if ln.startswith("FROM ")):]
    env_at = [i for i, ln in enumerate(stage) if re.fullmatch(r"ENV SWARM_GIT_SHA=\$\{GIT_SHA\}", ln)]
    assert len(env_at) == 1, f"{dockerfile}: no ENV SWARM_GIT_SHA=${{GIT_SHA}} in its last stage"
    assert any(re.fullmatch(r'ARG GIT_SHA(="")?', ln) for ln in stage[:env_at[0]]), (
        f"{dockerfile}: GIT_SHA is not declared in the last stage before the ENV reads it")
    runs_after = [ln for ln in stage[env_at[0]:] if ln.startswith("RUN ")]
    assert not runs_after, f"{dockerfile}: a RUN after the ENV rebuilds on every commit: {runs_after}"
