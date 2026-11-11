"""A step on a ready plugin bundle runs on that bundle's image (managed-plugins.md §1.4.4).

A step whose signed plugin set (`metadata.dispatch.plugins`) carries a bundle
key runs the bundle record's output digest for its runner image instead of the
release worker image, and only when:

  * the scheduler names the one repository a bundle digest may be in
    (PLUGIN_BUNDLE_REPOSITORY);
  * a `plugin_bundles` record with that key, on the LIVE base (the digest
    WORKER_IMAGE_REFS names for the runner image), is `ready`;
  * its output for the runner image is a digest under that repository.

Anything else refuses the dispatch `plugin_bundle_missing`, with its reason in
the message, before any backend is asked. The code is a platform refusal, so
the drain gives the lease back in the same write and parks the task
PLATFORM_REFUSED: no pod, no attempt spent, no pool count held.

A step with no bundle key is dispatched exactly as today, and nothing the
caller submits names an image (invariant 10): the digest comes only from the
bundle record the key selects.

MUTATIONS: return `image_uri` for a step with a key (the bundle test fails);
drop the repository check (the outside-the-repository test fails); drop the
`ready` check (the building test fails); drop `plugin_bundle_missing` from
PLATFORM_REFUSAL_CODES (the drain test finds an attempt spent).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend
from swarm_common.specsign import jcs
from swarm_common.states import ParkReason, TaskState

from scheduler.dispatch import (
    PLATFORM_REFUSAL_CODES,
    PLUGIN_BUNDLE_MISSING,
    WORKER_IMAGE_ENV,
    BackendRouter,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    image_uri,
    read_plugin_bundles,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, REGION, scheduler_settings, seed_pool, seed_task, seed_tenant
from .test_dispatch_manifests import FakeBatchApi, make_lease, make_task

PROFILE = "claude-code"
IMAGE = RUNNER_PROFILES[PROFILE].image
REGISTRY = f"{REGION}-docker.pkg.dev/{PROJECT}"
REPOSITORY = f"{REGISTRY}/swarm-plugin-bundles"


def _digest() -> str:
    return "sha256:" + secrets.token_hex(32)


LIVE_BASE = f"{REGISTRY}/swarm-images/{IMAGE}@{_digest()}"
BUNDLE_OUTPUT = f"{REPOSITORY}/{IMAGE}@{_digest()}"
KEY = [
    {"install_id": "builtin:gstack", "version_id": "v-" + secrets.token_hex(6),
     "source_sha256": secrets.token_hex(32)},
    {"install_id": "builtin:security-audit", "version_id": "v-" + secrets.token_hex(6),
     "source_sha256": secrets.token_hex(32)},
]
KEY_SHA256 = hashlib.sha256(jcs(KEY)).hexdigest()


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@example.com",
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _settings(**overrides: Any) -> SchedulerSettings:
    base: dict[str, Any] = {"worker_image_refs": {IMAGE: LIVE_BASE},
                            "plugin_bundle_repository": REPOSITORY}
    base.update(overrides)
    return scheduler_settings(**base)


def _record(**overrides: Any) -> dict[str, Any]:
    record = {
        "bundle_id": "b-" + secrets.token_hex(6), "key": KEY, "key_sha256": KEY_SHA256,
        "base": {IMAGE: LIVE_BASE}, "outputs": {IMAGE: BUNDLE_OUTPUT}, "state": "ready",
        "created_at": datetime(2026, 11, 11, tzinfo=timezone.utc),
    }
    record.update(overrides)
    return record


def _with_key(task, key: Any = KEY, **dispatch: Any):
    block = {"plugins": {"bundle": key}, **dispatch}
    return task.model_copy(update={"metadata": {**(task.metadata or {}), "dispatch": block}})


class _Bundles:
    """`plugin_bundles` by key digest; records every digest asked for."""

    def __init__(self, *records: dict[str, Any]) -> None:
        self.records = list(records)
        self.asked: list[str] = []

    def __call__(self, key_sha256: str) -> list[dict[str, Any]]:
        self.asked.append(key_sha256)
        return [r for r in self.records if r.get("key_sha256") == key_sha256]


def _dispatch(settings, tenant, task, bundles) -> FakeBatchApi:
    api = FakeBatchApi()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    router = BackendRouter(cloud_run=None, gke=gke, settings=settings, bundles=bundles)
    router.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[task.runner_profile],
                    tenant=tenant, backend=Backend.GKE_AUTOPILOT)
    return api


def _worker(api: FakeBatchApi) -> dict[str, Any]:
    spec = api.created[0][1]["spec"]["template"]["spec"]
    return next(c for c in spec["containers"] if c["name"] == "worker")


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def _refused(settings, tenant, task, bundles) -> tuple[DispatchError, FakeBatchApi]:
    api = FakeBatchApi()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    router = BackendRouter(cloud_run=None, gke=gke, settings=settings, bundles=bundles)
    with pytest.raises(DispatchError) as caught:
        router.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[task.runner_profile],
                        tenant=tenant, backend=Backend.GKE_AUTOPILOT)
    return caught.value, api


# --- the image choice ------------------------------------------------------------


def test_a_step_with_no_bundle_key_runs_the_release_image_and_reads_no_bundle(tenant):
    settings = _settings()
    bundles = _Bundles(_record())
    api = _dispatch(settings, tenant, make_task(PROFILE), bundles)
    worker = _worker(api)
    assert worker["image"] == image_uri(settings, RUNNER_PROFILES[PROFILE]) == LIVE_BASE
    assert _env(worker)[WORKER_IMAGE_ENV] == LIVE_BASE
    assert bundles.asked == [], "a step with no bundle key reads nothing it did not read before"


def test_a_step_with_an_empty_plugin_set_runs_the_release_image(tenant):
    settings = _settings()
    task = make_task(PROFILE).model_copy(update={"metadata": {"dispatch": {"plugins": {}}}})
    api = _dispatch(settings, tenant, task, _Bundles())
    assert _worker(api)["image"] == LIVE_BASE


def test_a_ready_bundle_under_the_repository_runs_its_digest(tenant):
    settings = _settings()
    bundles = _Bundles(_record())
    api = _dispatch(settings, tenant, _with_key(make_task(PROFILE)), bundles)
    worker = _worker(api)
    assert worker["image"] == BUNDLE_OUTPUT
    assert _env(worker)[WORKER_IMAGE_ENV] == BUNDLE_OUTPUT, "the worker is told the image it runs"
    assert bundles.asked == [KEY_SHA256]


def test_a_bundle_built_on_another_base_is_not_run(tenant):
    # A bundle of the same key on the previous release's base stays for the
    # upgrade's rollback; it is never run against the live control plane.
    settings = _settings()
    old_base = f"{REGISTRY}/swarm-images/{IMAGE}@{_digest()}"
    error, api = _refused(settings, tenant, _with_key(make_task(PROFILE)),
                          _Bundles(_record(base={IMAGE: old_base})))
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "bundle_not_found" in str(error)
    assert api.created == []


def test_a_digest_outside_the_repository_is_refused(tenant):
    # A forged record (a tenant account can write Firestore) naming the
    # release repository, or any other image, runs nothing.
    settings = _settings()
    for output in (f"{REGISTRY}/swarm-images/{IMAGE}@{_digest()}",
                   f"{REPOSITORY}-other/{IMAGE}@{_digest()}",
                   f"{REPOSITORY}/agent-runtime-browser@{_digest()}",
                   f"{REPOSITORY}/{IMAGE}:b-{secrets.token_hex(6)}",
                   None):
        error, api = _refused(settings, tenant, _with_key(make_task(PROFILE)),
                              _Bundles(_record(outputs={IMAGE: output})))
        assert error.code == PLUGIN_BUNDLE_MISSING, output
        assert "digest_outside_repository" in str(error), output
        assert api.created == [], output


@pytest.mark.parametrize("state", ["building", "failed", "retired"])
def test_a_bundle_that_is_not_ready_is_refused(tenant, state):
    settings = _settings()
    error, api = _refused(settings, tenant, _with_key(make_task(PROFILE)),
                          _Bundles(_record(state=state)))
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "bundle_not_ready" in str(error)
    assert api.created == []


def test_no_bundle_record_is_refused(tenant):
    error, api = _refused(_settings(), tenant, _with_key(make_task(PROFILE)), _Bundles())
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "bundle_not_found" in str(error)
    assert api.created == []


def test_an_unset_repository_is_refused_before_any_read(tenant):
    bundles = _Bundles(_record())
    error, api = _refused(_settings(plugin_bundle_repository=""), tenant,
                          _with_key(make_task(PROFILE)), bundles)
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "repository_unset" in str(error)
    assert api.created == [] and bundles.asked == []


def test_a_repository_outside_this_deployments_registry_is_refused(tenant):
    other = f"{REGION}-docker.pkg.dev/other-project-1/swarm-plugin-bundles"
    error, api = _refused(_settings(plugin_bundle_repository=other), tenant,
                          _with_key(make_task(PROFILE)),
                          _Bundles(_record(outputs={IMAGE: f"{other}/{IMAGE}@{_digest()}"})))
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "repository_invalid" in str(error)
    assert api.created == []


def test_an_unreadable_bundle_store_is_refused(tenant):
    def broken(_key: str) -> list[dict[str, Any]]:
        raise RuntimeError("firestore unavailable")

    error, api = _refused(_settings(), tenant, _with_key(make_task(PROFILE)), broken)
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "bundle_unreadable" in str(error)
    assert api.created == []


@pytest.mark.parametrize("key", [[], "b-0123456789ab", [{"install_id": "builtin:gstack"}], {"a": 1}])
def test_a_malformed_bundle_key_is_refused(tenant, key):
    error, api = _refused(_settings(), tenant, _with_key(make_task(PROFILE), key=key), _Bundles(_record()))
    assert error.code == PLUGIN_BUNDLE_MISSING
    assert "bundle_key_invalid" in str(error)
    assert api.created == []


def test_a_cloud_run_profile_with_a_bundle_key_is_refused(tenant):
    # A Cloud Run Job carries its image on the Job, shared by every execution,
    # and an execution override cannot change it.
    settings = _settings()
    ran: list[str] = []

    class _CloudRun:
        def dispatch(self, **_kw: Any) -> str:
            ran.append("ran")
            return "execution"

    router = BackendRouter(cloud_run=_CloudRun(), gke=None, settings=settings,
                           bundles=_Bundles(_record()))
    task = _with_key(make_task("claude-code-review"))
    with pytest.raises(DispatchError) as caught:
        router.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code-review"],
                        tenant=tenant, backend=Backend.CLOUD_RUN_JOB)
    assert caught.value.code == PLUGIN_BUNDLE_MISSING
    assert "backend_runs_release_image" in str(caught.value)
    assert ran == []


def test_the_image_is_never_taken_from_the_caller(tenant):
    # Invariant 10: whatever image-shaped fields a submission carries, the
    # digest run is the bundle record's, selected by the key alone.
    settings = _settings()
    forged = f"{REPOSITORY}/{IMAGE}@{_digest()}"
    task = _with_key(make_task(PROFILE), image=forged, worker_image=forged)
    task = task.model_copy(update={"input": {"image": forged}})
    plugins = dict(task.metadata["dispatch"]["plugins"], image=forged, outputs={IMAGE: forged})
    task = task.model_copy(update={"metadata": {"dispatch": {**task.metadata["dispatch"], "plugins": plugins}}})
    api = _dispatch(settings, tenant, task, _Bundles(_record()))
    assert _worker(api)["image"] == BUNDLE_OUTPUT

    plain = make_task(PROFILE).model_copy(update={"input": {"image": forged},
                                                  "metadata": {"dispatch": {"image": forged}}})
    api = _dispatch(settings, tenant, plain, _Bundles(_record()))
    assert _worker(api)["image"] == LIVE_BASE


# --- the settings ------------------------------------------------------------------


def test_the_repository_is_read_from_its_variable(monkeypatch):
    monkeypatch.setenv("PLUGIN_BUNDLE_REPOSITORY", f"  {REPOSITORY}  ")
    assert SchedulerSettings.from_env().plugin_bundle_repository == REPOSITORY
    monkeypatch.delenv("PLUGIN_BUNDLE_REPOSITORY")
    assert SchedulerSettings.from_env().plugin_bundle_repository == ""


# --- the Firestore read --------------------------------------------------------------


def test_the_reader_selects_by_key_digest(db):
    db.docs["plugin_bundles/b-000000000001"] = _record(bundle_id=None)
    db.docs["plugin_bundles/b-000000000002"] = _record(key_sha256="0" * 64)
    found = read_plugin_bundles(db, KEY_SHA256)
    assert [r["bundle_id"] for r in found] == ["b-000000000001"]


# --- the drain: a refusal holds nothing ---------------------------------------------


def test_a_refused_step_parks_and_holds_no_capacity(db):
    start = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    seed_tenant(db, "eng", max_active=5)
    seed_pool(db, "global", hard_limit=5)
    seed_task(db, task_id="task_bundle", tenant_id="eng", created_at=start)
    db.docs["tasks/task_bundle"]["metadata"] = {"dispatch": {"plugins": {"bundle": KEY}}}
    asked: list[str] = []

    class _Backend:
        def dispatch(self, **_kw: Any) -> str:
            asked.append("dispatch")
            return "execution"

    settings = scheduler_settings(plugin_bundle_repository="")
    scheduler = Scheduler(
        settings=settings,
        store=SchedulerStore(db, now=lambda: start),
        router=BackendRouter(cloud_run=_Backend(), gke=_Backend(), settings=settings,
                             bundles=_Bundles(_record())),
        metrics=SchedulerMetrics(),
        now=lambda: start,
    )
    report = scheduler.drain()

    assert asked == [], "no backend is asked for a refused step"
    stored = db.docs["tasks/task_bundle"]
    assert stored["state"] == TaskState.PARKED.value, report.to_dict()
    assert stored["park_reason"] == ParkReason.PLATFORM_REFUSED.value
    assert stored["attempt_count"] == 0, "a missing bundle spends no attempt"
    assert PLUGIN_BUNDLE_MISSING in stored["last_error"]
    assert stored["current_lease_id"] is None
    assert [p for p, d in db.docs.items() if p.startswith("leases/") and d.get("released_at") is None] == []
    assert db.docs["pools/global"]["active"] == 0, "the slot came back in the same write"
    assert PLUGIN_BUNDLE_MISSING in PLATFORM_REFUSAL_CODES
