"""A TypeSafe judge never runs as a Cloud Run Job (lane JUDGE-GKE, owner
decision 2026-10-16 on #1347; part of #1347).

The TypeSafe forwarder (SEC-MIGRATE-TYPESAFE) is an in-cluster Service, so a
judge on Cloud Run could only read the key file. The router refuses a Cloud
Run dispatch when ALL of these hold, and each is turned off once below:

  * the forwarder's switch serves the tenant (`typesafe_forwarder_enabled`,
    the tenant in `typesafe_forwarder_tenants` by exact name);
  * the attempt would be a Cloud Run Job;
  * the profile carries the Jev client (claude_code / codex runners, not the
    indexer);
  * the workspace's judgment backend, read as the worker's judge reads it,
    is `typesafe` -- or the document could not be read (fails closed).

The scheduler has no runtime Cloud Run fallback: claude-code reaches Cloud
Run only through a rollback of its catalogue line, which is what
`ROLLED_BACK` builds here. Switched off, the router dispatches exactly what
it did, and reads nothing new.

MUTATION: drop any one condition from `typesafe_judge_needs_gke`, read the
top-level `judgment_backend` ahead of `code_intelligence`'s, fail open on an
unreadable document, or move the check after `for_backend(...).dispatch`, and
the named case fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    JEV_CLIENT_EXCLUDED_PROFILES,
    JEV_CLIENT_RUNNERS,
    TYPESAFE_JUDGE_NEEDS_GKE,
    BackendRouter,
    DispatchError,
    judge_backend,
    read_judge_backend,
    runs_jev_client,
    typesafe_judge_needs_gke,
)
from scheduler.judgment import SETTINGS_COLLECTION

from .conftest import PROJECT, scheduler_settings
from .fakes import FakeFirestore
from .test_dispatch_manifests import NOW, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs/contract-change-requests.md"

#: claude-code as a rollback of contract request 53 would leave it: one line
#: of the catalogue back to CLOUD_RUN_JOB.
ROLLED_BACK = dataclasses.replace(RUNNER_PROFILES["claude-code"], backend=Backend.CLOUD_RUN_JOB)


class Recording:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{self.name}-execution"


class Reader:
    """tenant id -> judgment backend, counting reads."""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.reads: list[str] = []

    def __call__(self, tenant_id: str) -> str | None:
        self.reads.append(tenant_id)
        return self.value


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _router(*, forwarder: bool = True, tenants: tuple[str, ...] = ("eng",),
            judgment: Any = "typesafe") -> tuple[BackendRouter, Recording, Recording, Reader | None]:
    settings = scheduler_settings(
        typesafe_forwarder_enabled=forwarder, typesafe_forwarder_tenants=tenants)
    cloud_run, gke = Recording("cloud-run"), Recording("gke")
    reader = Reader(judgment) if judgment != "unwired" else None
    router = BackendRouter(cloud_run=cloud_run, gke=gke, settings=settings, judgment=reader)
    return router, cloud_run, gke, reader


def _dispatch(router: BackendRouter, tenant: Tenant, profile: Any, backend: Backend) -> str:
    task = make_task("claude-code")
    return router.dispatch(task=task, lease=make_lease(task), profile=profile,
                           tenant=tenant, backend=backend)


# -- the behaviour ------------------------------------------------------------------

def test_a_typesafe_judging_claude_code_attempt_never_reaches_cloud_run(tenant):
    router, cloud_run, gke, _ = _router()

    with pytest.raises(DispatchError) as raised:
        _dispatch(router, tenant, ROLLED_BACK, Backend.CLOUD_RUN_JOB)

    assert raised.value.code == TYPESAFE_JUDGE_NEEDS_GKE == "typesafe_judge_needs_gke"
    assert cloud_run.calls == [] and gke.calls == []


def test_claude_code_on_its_catalogue_backend_goes_to_gke_with_the_switch_on(tenant):
    router, cloud_run, gke, _ = _router()
    profile = RUNNER_PROFILES["claude-code"]

    assert _dispatch(router, tenant, profile, resolve_backend(profile)) == "gke-execution"
    assert cloud_run.calls == [] and len(gke.calls) == 1


@pytest.mark.parametrize("runner", ["codex", "claude-code-review"])
def test_every_jev_client_runner_is_held_off_cloud_run(tenant, runner):
    router, cloud_run, _, _ = _router()
    profile = dataclasses.replace(RUNNER_PROFILES[runner], backend=Backend.CLOUD_RUN_JOB)

    with pytest.raises(DispatchError):
        _dispatch(router, tenant, profile, Backend.CLOUD_RUN_JOB)
    assert cloud_run.calls == []


def test_an_unreadable_setting_fails_closed(tenant):
    router, cloud_run, _, _ = _router(judgment=None)
    with pytest.raises(DispatchError):
        _dispatch(router, tenant, ROLLED_BACK, Backend.CLOUD_RUN_JOB)

    router, cloud_run, _, _ = _router(judgment="unwired")
    with pytest.raises(DispatchError):
        _dispatch(router, tenant, ROLLED_BACK, Backend.CLOUD_RUN_JOB)
    assert cloud_run.calls == []


# -- each condition, turned off once: today's routing ------------------------------

@pytest.mark.parametrize("case", [
    dict(forwarder=False),
    dict(tenants=()),
    dict(tenants=("eng-other",)),
    dict(judgment="internal-jev"),
    dict(judgment="internal-laya"),
])
def test_with_any_condition_off_cloud_run_is_dispatched_as_today(tenant, case):
    router, cloud_run, gke, _ = _router(**case)

    assert _dispatch(router, tenant, ROLLED_BACK, Backend.CLOUD_RUN_JOB) == "cloud-run-execution"
    assert len(cloud_run.calls) == 1 and gke.calls == []


@pytest.mark.parametrize("name", ["mock", "post-verdict", "merge", "generic", "indexer"])
def test_a_profile_without_the_jev_client_still_runs_on_cloud_run(tenant, name):
    router, cloud_run, _, _ = _router()
    profile = dataclasses.replace(RUNNER_PROFILES[name], backend=Backend.CLOUD_RUN_JOB)

    assert _dispatch(router, tenant, profile, Backend.CLOUD_RUN_JOB) == "cloud-run-execution"
    assert len(cloud_run.calls) == 1


def test_switched_off_the_router_reads_nothing_and_dispatches_byte_for_byte(tenant):
    """The off path is the router as it was: the same dispatcher, the same
    arguments, the same return value, and no settings read."""
    router, cloud_run, _, reader = _router(forwarder=False)
    task = make_task("claude-code")
    lease = make_lease(task)

    out = router.dispatch(task=task, lease=lease, profile=ROLLED_BACK, tenant=tenant,
                          backend=Backend.CLOUD_RUN_JOB)

    assert out == "cloud-run-execution"
    assert cloud_run.calls == [dict(task=task, lease=lease, profile=ROLLED_BACK, tenant=tenant)]
    assert reader is not None and reader.reads == []


def test_the_setting_is_read_only_once_every_other_condition_holds(tenant):
    router, _, _, reader = _router()
    profile = RUNNER_PROFILES["claude-code"]
    _dispatch(router, tenant, profile, Backend.GKE_AUTOPILOT)
    _dispatch(router, tenant, RUNNER_PROFILES["mock"], Backend.CLOUD_RUN_JOB)
    assert reader is not None and reader.reads == []


def test_the_decision_is_pure_over_its_inputs():
    on = scheduler_settings(typesafe_forwarder_enabled=True, typesafe_forwarder_tenants=("eng",))
    kw = dict(tenant_id="eng", profile=ROLLED_BACK, backend=Backend.CLOUD_RUN_JOB)
    assert typesafe_judge_needs_gke(on, judgment="typesafe", **kw)
    assert typesafe_judge_needs_gke(on, judgment=None, **kw)
    assert not typesafe_judge_needs_gke(on, judgment="internal-jev", **kw)
    assert not typesafe_judge_needs_gke(scheduler_settings(), judgment="typesafe", **kw)


# -- the stored setting, read as the worker's judge reads it ------------------------

DOCS: list[dict[str, Any]] = [
    {},
    {"judgment_backend": "typesafe"},
    {"judgment_backend": "internal-laya"},
    {"judgment_backend": 7},
    {"code_intelligence": {"judgment_backend": "typesafe"}, "judgment_backend": "internal-jev"},
    {"code_intelligence": {"judgment_backend": "internal-jev"}, "judgment_backend": "typesafe"},
    {"code_intelligence": {"jev": True}, "judgment_backend": "typesafe"},
    {"code_intelligence": "not a block", "judgment_backend": "typesafe"},
    {"code_intelligence": {"judgment_backend": "bogus"}, "judgment_backend": "typesafe"},
]


@pytest.mark.parametrize("doc", DOCS)
def test_the_backend_is_the_one_the_workers_judge_resolves(doc):
    from agent_worker.judge import attempt

    assert judge_backend(doc) == attempt.judge_settings(doc, None).backend


def test_the_reader_reads_the_workspace_settings_document():
    db = FakeFirestore()
    assert read_judge_backend(db, "eng") == "internal-jev"
    db.docs[f"{SETTINGS_COLLECTION}/eng"] = {"code_intelligence": {"judgment_backend": "typesafe"}}
    assert read_judge_backend(db, "eng") == "typesafe"

    class Broken:
        def collection(self, name: str) -> Any:
            raise RuntimeError("firestore down")

    assert read_judge_backend(Broken(), "eng") is None


# -- which profiles carry the Jev client --------------------------------------------

def test_the_jev_client_runners_are_the_workers():
    from agent_worker import graphstage, indexrun

    assert JEV_CLIENT_RUNNERS == graphstage.GRAPH_RUNNERS
    assert JEV_CLIENT_EXCLUDED_PROFILES == (indexrun.INDEXER_PROFILE,)


def test_the_map_in_the_pr_body_is_the_catalogues():
    carrying = {n for n, p in RUNNER_PROFILES.items() if runs_jev_client(p)}
    assert carrying == {"claude-code", "codex", "claude-code-review", "claude-code-browser",
                        "claude-code-large", "claude-code-lightpanda"}
    on_cloud_run = {n for n in carrying
                    if resolve_backend(RUNNER_PROFILES[n]) is Backend.CLOUD_RUN_JOB}
    assert on_cloud_run == {"claude-code-review"}


def test_the_contract_request_for_the_cloud_run_profile_is_filed():
    text = REQUESTS.read_text(encoding="utf-8")
    heading = re.search(r"^## 84\. (.+)$", text, re.M)
    assert heading, "contract request 84 (JUDGE-GKE) is missing"
    assert "claude-code-review" in heading.group(1) and "JUDGE-GKE" in heading.group(1)
