"""The forge-credential broker against the Firestore EMULATOR, through the real client.

docs/design/user-scoped-secrets.md §3.2. The unit suite
(tests/unit/control_plane/test_forge_credential_broker.py) drives every
refusal over FakeFirestore; this file proves the two cases the design exists
for with the client that runs in production -- the child-key registration's
transaction, the task, lease, registration and grant reads by id:

  * Alice's attempt in group tenant eng is released Alice's slot;
  * Bob's attempt in the same tenant, whose task names Alice's slot, is
    refused, and Secret Manager is never asked.

Secret Manager itself is a recording fake (no credentials here); every token
is built at runtime. Each test gets its own emulator project id, so the
suite's `-n auto` workers never see each other's documents.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()
if not EMULATOR:
    # CI's integration job starts the emulator and exports this; on a runner
    # without it this file must fail, not pass having run nothing.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError(
            "FIRESTORE_EMULATOR_HOST is not set in CI; the forge-credential broker "
            "emulator tests would otherwise pass having run nothing"
        )
    pytest.skip("needs the Firestore emulator (FIRESTORE_EMULATOR_HOST)", allow_module_level=True)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.cloud import firestore  # noqa: E402

from swarm_common import specsign  # noqa: E402
from swarm_common.config import Settings  # noqa: E402
from swarm_common.models import Tenant  # noqa: E402

from swarm_api import forge  # noqa: E402
from swarm_api.access import grant_id_for  # noqa: E402
from swarm_api.auth import StaticTokenVerifier  # noqa: E402
from swarm_api.childkey import AttemptTuple, nonce, request_message  # noqa: E402
from swarm_api.credentials import InMemoryCredentials  # noqa: E402
from swarm_api.deps import build_context  # noqa: E402
from swarm_api.forgeapp import GRANTS, user_hash  # noqa: E402
from swarm_api.gittokens import Scope, provider_suffix, secret_name_for  # noqa: E402
from swarm_api.groups import StaticGroups  # noqa: E402
from swarm_api.main import create_app  # noqa: E402
from swarm_api.metrics import ApiMetrics  # noqa: E402
from swarm_api.repositories import repo_id_for  # noqa: E402
from swarm_api.settings import ApiSettings  # noqa: E402
from swarm_api.waker import NullWaker  # noqa: E402

PROJECT = "saga-agents-staging"
ENG_GROUP = "eng@saga.xyz"
ALICE = "alice@saga.xyz"
BOB = "bob@saga.xyz"
CHILD_KEY = "emulator-child-key-not-a-real-secret"
#: The static verifier's name for eng's worker identity, built at runtime.
WORKER_ID_TOKEN_NAME = "-".join(("token", "worker", "eng"))
KEY_VERSION = (
    "projects/swarm-test/locations/us-central1/keyRings/swarm-test-specs/"
    "cryptoKeys/step-spec/cryptoKeyVersions/1"
)
PATH = "/v1/attempts/forge-credential"
OWNER, REPO = "acme", "widgets"
REPO_URL = f"https://github.com/{OWNER}/{REPO}.git"

TOKENS = {
    WORKER_ID_TOKEN_NAME: {
        "email": f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        "email_verified": True,
        "sub": "1000000000000000000001",
    },
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class SpecKey:
    """A local P-256 step-spec key, signing as Cloud KMS signs (DER ECDSA over
    the SHA-256 digest): tests/unit/control_plane/spec_signer.py, restated
    because that module is package-relative to the unit suite."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())

    def pem(self) -> str:
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    def sign(self, doc: dict[str, Any], task_id: str) -> None:
        spec_format = specsign.signing_format(doc)
        digest = specsign.spec_digest(
            specsign.canonical_step_spec(doc, task_id=task_id, spec_format=spec_format)
        )
        der = self._key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        doc["spec_signature"] = base64.b64encode(der).decode("ascii")
        doc["spec_key_version"] = KEY_VERSION
        doc["spec_format"] = spec_format


class SecretStore:
    """Secret Manager's slots, read through `read_slot`; `asked` records every name."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.asked: list[str] = []

    def token_for(self, tenant) -> str:
        return self.read_slot(tenant, forge.GIT_PROVIDER).value

    def read_slot(self, tenant, provider: str) -> forge.SlotValue:
        secret_id = tenant.secret_name(provider)
        self.asked.append(secret_id)
        if secret_id not in self.values:
            raise forge.NoForgeCredential(f"no value stored in {secret_id}")
        return forge.SlotValue(self.values[secret_id], "1")


def api_settings(spec_key: SpecKey) -> ApiSettings:
    """The unit suite's settings, restated (tests/integration cannot import that conftest)."""
    core = Settings(
        project_id=PROJECT,
        region="us-central1",
        environment="test",
        firestore_database="swarm",
        artifact_bucket=f"{PROJECT}-swarm-artifacts",
        allowed_domains=("saga.xyz",),
        max_batch_size=100,
        max_input_bytes=256 * 1024,
        max_workflow_steps=50,
        requests_per_second=20,
        default_tenant_max_active=20,
        default_tenant_capacity_units=40,
        prewarm_max_agents=10,
        prewarm_lead_seconds=120,
    )
    return ApiSettings(
        core=core,
        tenant_groups=(ENG_GROUP,),
        admin_groups=("swarm-admins@saga.xyz",),
        group_cache_ttl_seconds=60,
        dispatch_topic="",
        max_page_size=200,
        default_page_size=50,
        rate_limit_burst=200,
        child_key=CHILD_KEY,
        spec_signing_key_version=KEY_VERSION,
        spec_verify_keys=json.dumps({KEY_VERSION: spec_key.pem()}),
    )


class Attempt:
    def __init__(self, task_id: str, n: int) -> None:
        self.tuple = AttemptTuple("eng", task_id, f"att_{n}", f"lease_{n}", 1)
        self.private = Ed25519PrivateKey.generate()
        self.public = _b64url(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def body(self, access: str = "write") -> bytes:
        t = self.tuple
        return json.dumps({"tenant_id": t.tenant_id, "task_id": t.task_id,
                           "attempt_id": t.attempt_id, "lease_id": t.lease_id,
                           "generation": t.generation, "access": access}).encode()

    def headers(self, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self.private.sign(request_message("POST", PATH, body, timestamp))
        return {"Authorization": f"Bearer {WORKER_ID_TOKEN_NAME}", "Content-Type": "application/json",
                "X-Swarm-Attempt-Proof": _b64url(signature),
                "X-Swarm-Attempt-Timestamp": timestamp}


@pytest.fixture
def db() -> Any:
    return firestore.Client(project=f"credbroker-{uuid.uuid4().hex[:12]}", database="(default)")


@pytest.fixture
def spec_key() -> SpecKey:
    return SpecKey()


@pytest.fixture
def store() -> SecretStore:
    return SecretStore()


@pytest.fixture
def api(db, spec_key, store) -> TestClient:
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id="eng", kind="group", principal=ENG_GROUP, created_at=now,
        display_name="eng", max_active=20, capacity_units=40, enabled=True, credentials=[],
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )
    db.document("tenants/eng").set(asdict(tenant))
    ctx = build_context(
        settings=api_settings(spec_key),
        db=db,
        verifier=StaticTokenVerifier(TOKENS),
        groups=StaticGroups({ALICE: (ENG_GROUP,), BOB: (ENG_GROUP,)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        forge_tokens=store,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def grant(db: Any, email: str) -> None:
    repo_id = repo_id_for("eng", OWNER, REPO)
    db.document(f"{GRANTS}/{grant_id_for('eng', email, repo_id)}").set({
        "tenant_id": "eng", "user": email, "user_hash": user_hash(email), "repo_id": repo_id,
        "repository": f"{OWNER}/{REPO}", "owner": OWNER, "mode": "write", "can_push": True,
        "archived": False, "granted_at": datetime.now(timezone.utc), "granted_by": email,
        "checks": {}, "verified_at": None,
    })


def running_task(api: TestClient, db: Any, spec_key: SpecKey, attempt: Attempt, *,
                 submitted_by: str, forge_credential: str) -> None:
    """Seed the task STARTING with a live lease, register the attempt key
    through the real route, then mark it RUNNING, signed as submission signs."""
    t = attempt.tuple
    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "id": t.task_id, "tenant_id": "eng", "created_at": now, "updated_at": now,
        "state": "STARTING", "runner_profile": "mock", "resource_class": "standard",
        "input": {}, "submitted_by": submitted_by, "provider": None, "model": None,
        "priority": 0, "metadata": {}, "repository_url": REPO_URL, "repository_ref": "main",
        "timeout_seconds": 600, "max_attempts": 3, "attempt_count": 1,
        "current_lease_id": t.lease_id, "current_generation": t.generation,
        "workflow_id": None, "step_id": None, "depends_on": [], "parent_task_id": None,
        "parent_attempt_id": None, "forge_credential": forge_credential,
        "forge_access": "write",
    }
    db.document(f"tasks/{t.task_id}").set(doc)
    db.document(f"leases/{t.lease_id}").set({
        "lease_id": t.lease_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "tenant_id": "eng", "generation": t.generation, "pools": [], "units": 1,
        "state": "STARTING", "created_at": now,
        "dispatch_deadline": now + timedelta(minutes=10),
        "expires_at": now + timedelta(minutes=10), "released_at": None,
    })
    registered = api.post(
        f"/v1/attempts/{t.attempt_id}/child-key",
        content=json.dumps({"tenant_id": "eng", "task_id": t.task_id, "lease_id": t.lease_id,
                            "generation": t.generation, "nonce": nonce(CHILD_KEY, t),
                            "public_key": attempt.public}).encode(),
        headers={"Authorization": f"Bearer {WORKER_ID_TOKEN_NAME}", "Content-Type": "application/json"},
    )
    assert registered.status_code == 201, registered.text
    doc["state"] = "RUNNING"
    spec_key.sign(doc, t.task_id)
    db.document(f"tasks/{t.task_id}").set(doc)


def test_alices_attempt_receives_alices_slot_through_the_emulator(api, db, spec_key, store):
    grant(db, ALICE)
    slot = provider_suffix(Scope.USER, user=ALICE)
    value = "".join(("ghu_", "a" * 36))
    store.values[secret_name_for("eng", slot)] = value
    attempt = Attempt("task_alice_em", 1)
    running_task(api, db, spec_key, attempt, submitted_by=ALICE, forge_credential=slot)

    body = attempt.body()
    response = api.post(PATH, content=body, headers=attempt.headers(body))
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": secret_name_for("eng", slot), "token": value}
    assert response.headers["cache-control"] == "no-store"
    assert store.asked == [secret_name_for("eng", slot)]


def test_bobs_attempt_is_refused_alices_slot_through_the_emulator(api, db, spec_key, store):
    grant(db, ALICE)
    grant(db, BOB)
    alices = provider_suffix(Scope.USER, user=ALICE)
    store.values[secret_name_for("eng", alices)] = "ghu_" + "b" * 36
    attempt = Attempt("task_bob_em", 2)
    running_task(api, db, spec_key, attempt, submitted_by=BOB, forge_credential=alices)

    body = attempt.body()
    response = api.post(PATH, content=body, headers=attempt.headers(body))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "credential_not_submitters"
    assert "token" not in response.json()
    assert store.asked == []
