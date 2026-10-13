"""A workflow read from the wrong tenant says so; the bridge re-reads its own.

THE DEFECT (docs/runner-git-tools.md §6.3, part of #1225). Three coordinator
status reads of `GET /v1/workflows/{id}` answered 404 after the build step while
the workflows' steps kept running, and the coordinator re-ran the review
locally, which opened duplicate pull requests. `Store.get_workflow` raised the
same bare `workflow '<id>' not found` for a missing document, for a document in
another tenant, and for another submitter's workflow, so a caller whose
resolved tenant had moved (#1204: a request with no `X-Swarm-Tenant` resolves to
the personal workspace once it is ready; the bridge reads `SWARM_TENANT` once at
start) could not tell "gone" from "look elsewhere".

Pinned here:

* the tenant-mismatch branch answers 404 `workflow_not_in_tenant`, naming the
  tenant the CALL resolved to -- the caller's own choice, so not an oracle --
  and never the tenant the workflow is stored under;
* a missing workflow and another submitter's workflow still answer the bare
  `not_found` (the controls: only the one branch the design names changes);
* the MCP bridge, reading a workflow IT launched, re-reads with the launch's
  tenant instead of reporting the workflow gone; a workflow it did not launch
  is not re-read anywhere (the bridge has no tenant of record for it).
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from swarm_api.auth import TENANT_HEADER
from swarm_api.codec import workflow_to_firestore
from swarm_api.errors import NotFound
from swarm_api.main import create_app
from swarm_api.store import Store

from swarm_mcp import client as mcp_client
from swarm_mcp import workflows
from swarm_mcp.client import SwarmClient, SwarmError

from .conftest import ENG_GROUP, RESEARCH_GROUP, auth_header, seed_tenant

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """`dave` is in eng AND research; eng is what a header-less call resolves."""
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "dave@saga.xyz": (ENG_GROUP, RESEARCH_GROUP),
    }


def stored(db, *, workflow_id: str = "wf_elsewhere", tenant_id: str = "research",
           submitted_by: str = "dave@saga.xyz") -> Workflow:
    workflow = Workflow(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.QUEUED,
        submitted_by=submitted_by,
        steps=[WorkflowStep(step_id="build", runner_profile="mock", input={},
                            depends_on=[], task_id=None)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)
    return workflow


# --------------------------------------------------------------------------
# swarm-api: Store.get_workflow and the route
# --------------------------------------------------------------------------

def test_another_tenants_workflow_answers_workflow_not_in_tenant_naming_the_resolved_one(db):
    stored(db, tenant_id="research")

    with pytest.raises(NotFound) as caught:
        Store(db).get_workflow("eng", "wf_elsewhere", submitted_by=None)

    err = caught.value
    assert err.status_code == 404
    assert err.code == "workflow_not_in_tenant", err.message
    assert err.detail == {"workflow_id": "wf_elsewhere", "tenant_id": "eng"}
    assert "'eng'" in err.message
    # The stored tenant is never named: the answer tells the caller where it
    # looked, not where the workflow is.
    assert "research" not in err.message
    assert "research" not in repr(err.to_payload())


def test_a_missing_workflow_is_still_a_bare_not_found(db):
    with pytest.raises(NotFound) as caught:
        Store(db).get_workflow("eng", "wf_nowhere", submitted_by=None)
    assert caught.value.code == "not_found"
    assert caught.value.message == "workflow 'wf_nowhere' not found"


def test_another_submitters_workflow_is_still_a_bare_not_found(db):
    stored(db, tenant_id="eng", submitted_by="alice@saga.xyz")
    with pytest.raises(NotFound) as caught:
        Store(db).get_workflow("eng", "wf_elsewhere", submitted_by="dave@saga.xyz")
    assert caught.value.code == "not_found"


def test_the_route_serves_the_code_and_the_resolved_tenant(client: TestClient, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    stored(db, tenant_id="research")

    # No header: dave resolves to eng, his first group.
    response = client.get("/v1/workflows/wf_elsewhere", headers=auth_header("dave"))
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "workflow_not_in_tenant", body
    assert body["detail"] == {"workflow_id": "wf_elsewhere", "tenant_id": "eng"}
    assert "research" not in response.text

    # The control: with the launch's tenant named, the same id reads.
    headers = {**auth_header("dave"), TENANT_HEADER: "research"}
    assert client.get("/v1/workflows/wf_elsewhere", headers=headers).status_code == 200


# --------------------------------------------------------------------------
# The MCP bridge: a workflow it launched is re-read with the launch's tenant
# --------------------------------------------------------------------------

class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.code = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def bridge(api_context, db, monkeypatch):
    """Real SwarmClients for dave whose socket is the real application.

    Returns a factory, so a test can make the launching client and the later
    reading client separately, as a restarted or re-resolved bridge would. The
    tenant each request named is recorded, in order.
    """
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    api = TestClient(create_app(api_context), raise_server_exceptions=False)
    named: list[str | None] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        headers = {**dict(req.headers), **auth_header("dave")}
        named.append(req.get_header(TENANT_HEADER.capitalize()))
        response = api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers,
                io.BytesIO(response.content),
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)

    def _make(tenant: str | None) -> SwarmClient:
        return SwarmClient(base_url="http://api.invalid", tenant=tenant)

    _make.named = named  # type: ignore[attr-defined]
    return _make


def test_the_bridge_rereads_a_workflow_it_launched_with_the_launch_tenant(bridge):
    launched = workflows.submit(
        bridge("research"),
        steps=[{"step_id": "build", "runner_profile": "mock", "input": {"prompt": "go"}}],
    )
    workflow_id = launched["workflow"]["workflow_id"]
    assert launched["workflow"]["tenant_id"] == "research"

    # Later the bridge sends no header (SWARM_TENANT unset, or #1204 moved the
    # default), so the API resolves eng and the first read answers 404.
    reader = bridge(None)
    del bridge.named[:]
    envelope = workflows.fetch(reader, workflow_id)

    assert envelope["workflow"]["workflow_id"] == workflow_id
    assert envelope["workflow"]["tenant_id"] == "research"
    assert bridge.named == [None, "research"]
    # The re-read did not change the reading client's own tenant.
    assert reader.tenant is None


def test_a_workflow_the_bridge_did_not_launch_is_not_reread(bridge, db):
    stored(db, workflow_id="wf_not_ours", tenant_id="research")

    with pytest.raises(SwarmError) as caught:
        workflows.fetch(bridge(None), "wf_not_ours")

    assert caught.value.status == 404
    assert caught.value.code == "workflow_not_in_tenant"
    assert bridge.named == [None]
