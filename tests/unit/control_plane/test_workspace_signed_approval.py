"""swarm-api signs every workspace approval and retry, and the job's check accepts it.

Lane WK-SIGNED-APPROVAL of #847 (owner decision 2026-10-19, R8 of
docs/design/workspace-kinds.md §4.5). A worker can write a record naming the
platform owner as its approver; it cannot write a signature by the step-spec
KMS key, which only swarm-api may sign with. So every route that approves
(an admin's click, the owner's own service request, an admin's own request)
and every retry stores `approval` -- the signature, its key version, and the
signed decider, time and request id -- in the same write as the decision, and
images/workspace-apply/entry.py's `verify_approval` accepts exactly that.

Here the routes run on the fake Firestore, the signer is a P-256 key made with
`cryptography` standing in for Cloud KMS, and the check is the REAL entry.py's,
loaded from the image's source. No cloud, no credentials.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient

from swarm_api import workspaces as ws
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import CloudIdentityGroups, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.specsigning import SpecSignature
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, NoForgeTokens, api_settings, auth_header
from .test_workspace_kinds_record import FakePublisher, RecordingDirectory

REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / "images" / "workspace-apply" / "entry.py"

ROOT = "root@saga.xyz"
DANA = "dana@saga.xyz"
REPORTS_BOT = f"reports-bot@{PROJECT}.iam.gserviceaccount.com"
ROUTE = "/v1/admin/workspaces"
KEY = f"projects/{PROJECT}/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
VERSION = KEY + "/cryptoKeyVersions/1"


class KmsKey:
    """Cloud KMS's AsymmetricSign on the step-spec key, in process."""

    def __init__(self, *, fail: bool = False) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.fail = fail
        self.digests: list[bytes] = []

    def sign(self, digest: bytes) -> SpecSignature:
        if self.fail:
            raise RuntimeError("KMS is down")
        self.digests.append(digest)
        der = self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return SpecSignature(signature=der, key_version=VERSION)

    def public_key(self, version: str) -> str:
        assert version == VERSION
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


@pytest.fixture()
def entry():
    spec = importlib.util.spec_from_file_location("workspace_apply_entry_signed", ENTRY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SIGNING_KEYS[PROJECT] == (KEY,), "the job trusts another key than this test signs with"
    return module


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {"alice@saga.xyz": (ENG_GROUP,), ROOT: (ADMIN_GROUP, ENG_GROUP), DANA: (ADMIN_GROUP,)}


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def kms() -> KmsKey:
    return KmsKey()


def _client(db, tokens, group_map, objects, publisher, signer: Any) -> TestClient:
    context = build_context(
        settings=api_settings(platform_owner=ROOT),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens())
    app = create_app(context)
    app.state.workspace_publisher = publisher
    app.state.workspace_directory = CloudIdentityGroups(PROJECT, session=RecordingDirectory())
    app.state.workspace_approval_signer = signer
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(db, tokens, group_map, objects, publisher, kms) -> TestClient:
    return _client(db, tokens, group_map, objects, publisher, kms)


def _record(db, tenant_id: str) -> dict[str, Any]:
    return copy.deepcopy(db.docs[f"workspaces/{tenant_id}"])


def _verifies(entry, kms: KmsKey, record: dict[str, Any]) -> None:
    """The job's own check, over the record as swarm-api stored it."""
    entry.verify_approval(record, project=PROJECT, public_key=kms.public_key)


def test_the_owners_approval_of_a_service_verifies_in_the_job(client, db, kms, entry) -> None:
    filed = client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT},
                        headers=auth_header("dana"))
    assert filed.status_code == 202, filed.text
    workspace_id = filed.json()["workspace_id"]
    tenant_id = ws.personal_tenant_id(REPORTS_BOT)
    assert _record(db, tenant_id).get("approval") is None, "a request nobody approved was signed"

    approved = client.post(f"{ROUTE}/{workspace_id}/approve", headers=auth_header("root"))
    assert approved.status_code == 200, approved.text
    record = _record(db, tenant_id)
    assert record["approval"]["decided_by"] == ROOT == record["decision"]["by"]
    assert record["approval"]["request_id"] == record["request_id"]
    assert record["approval"]["key_version"] == VERSION
    _verifies(entry, kms, record)

    # The control: the same record with its decider rewritten is refused.
    forged = copy.deepcopy(record)
    forged["decision"]["by"] = forged["approval"]["decided_by"] = DANA
    with pytest.raises(entry.Unsigned):
        _verifies(entry, kms, forged)


def test_the_owners_own_service_request_is_signed_as_it_is_approved(client, db, kms, entry) -> None:
    response = client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT},
                           headers=auth_header("root"))
    assert response.status_code == 202, response.text
    _verifies(entry, kms, _record(db, ws.personal_tenant_id(REPORTS_BOT)))


def test_an_admins_group_approval_verifies_in_the_job(client, db, kms, entry) -> None:
    response = client.post(ROUTE, json={"kind": "group", "principal": "reports@saga.xyz"},
                           headers=auth_header("dana"))
    assert response.status_code == 202, response.text
    record = _record(db, "reports")
    assert record["approval"]["decided_by"] == DANA
    _verifies(entry, kms, record)


def test_a_retry_is_signed_afresh_and_verifies_in_the_job(client, db, kms, entry) -> None:
    response = client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT},
                           headers=auth_header("root"))
    workspace_id = response.json()["workspace_id"]
    tenant_id = ws.personal_tenant_id(REPORTS_BOT)
    first = _record(db, tenant_id)
    db.docs[f"workspaces/{tenant_id}"].update(state=ws.FAILED, failure={"code": "STEP_FAILED"})

    retried = client.post(f"{ROUTE}/{workspace_id}/retry", headers=auth_header("root"))
    assert retried.status_code == 200, retried.text
    record = _record(db, tenant_id)
    assert record["request_id"] != first["request_id"]
    assert record["approval"]["request_id"] == record["request_id"]
    assert record["approval"]["decided_by"] == record["retry"]["by"] == ROOT
    _verifies(entry, kms, record)
    # The earlier signature, put back on the retried record, is for another request.
    stale = {**record, "approval": first["approval"]}
    with pytest.raises(entry.Unsigned):
        _verifies(entry, kms, stale)


def test_a_persons_own_request_approved_automatically_is_signed(client, db, kms, entry) -> None:
    response = client.post("/v1/workspace", json={"via": "console"}, headers=auth_header("dana"))
    assert response.status_code == 202, response.text
    record = _record(db, ws.personal_tenant_id(DANA))
    assert record["state"] == ws.APPROVED
    assert record["approval"]["decided_by"] == DANA
    entry.verify_approval(record, project=PROJECT, public_key=kms.public_key)


def test_kms_failing_leaves_the_record_unapproved_and_publishes_nothing(
        db, tokens, group_map, objects, publisher) -> None:
    client = _client(db, tokens, group_map, objects, publisher, KmsKey(fail=True))
    filed = client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT},
                        headers=auth_header("dana"))
    assert filed.status_code == 202, filed.text
    tenant_id = ws.personal_tenant_id(REPORTS_BOT)
    before = _record(db, tenant_id)
    approved = client.post(f"{ROUTE}/{filed.json()['workspace_id']}/approve", headers=auth_header("root"))
    assert approved.status_code == 503, approved.text
    assert _record(db, tenant_id) == before
    assert publisher.messages == []


def test_the_signature_covers_the_approval_payload_and_nothing_a_worker_chooses(client, db, kms) -> None:
    """What KMS was asked to sign is the canonical approval of the stored record."""
    client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT}, headers=auth_header("root"))
    record = _record(db, ws.personal_tenant_id(REPORTS_BOT))
    canonical = ws.canonical_approval({
        "workspace_id": record["workspace_id"], "tenant_id": record["tenant_id"], "kind": "service",
        "principal": REPORTS_BOT, "decided_by": ROOT, "decided_at": record["approval"]["decided_at"],
        "request_id": record["request_id"]})
    assert kms.digests == [hashlib.sha256(canonical).digest()]
    assert b'"purpose":"swarm.workspace-approval/v1"' in canonical


def test_a_denial_drops_the_approval_signature(client, db, kms, entry) -> None:
    response = client.post(ROUTE, json={"kind": "service", "principal": REPORTS_BOT},
                           headers=auth_header("root"))
    workspace_id = response.json()["workspace_id"]
    tenant_id = ws.personal_tenant_id(REPORTS_BOT)
    assert _record(db, tenant_id)["approval"] is not None
    db.docs[f"workspaces/{tenant_id}"].update(state=ws.FAILED, failure={"code": "STEP_FAILED"})
    denied = client.post(f"{ROUTE}/{workspace_id}/deny", json={"reason": "not this account"},
                         headers=auth_header("root"))
    assert denied.status_code == 200, denied.text
    record = _record(db, tenant_id)
    assert record["approval"] is None
    # What a worker that flips the verdict back gets: no signature to verify.
    record["decision"] = {**record["decision"], "verdict": ws.APPROVED}
    with pytest.raises(entry.Unsigned):
        _verifies(entry, kms, record)
