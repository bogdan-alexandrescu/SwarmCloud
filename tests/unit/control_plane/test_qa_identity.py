"""swarm-api's half of the QA identity (docs/design/qa-identity.md §4.2, §4.3,
§4.9, §6 row QI-API; part of #1649).

Option B (owner, 2026-10-19): a QA service account whose IAP JWT swarm-api
signs for a live attempt, and the gate adds. Measured here, over the real app
and the in-memory Firestore:

  * a platform admin grants the identity per tenant, and the grant and its
    `admin_audit` row commit together or not at all;
  * a submission naming `input.qa_identity` from a tenant with no `granted`
    document is 403 `qa_identity_not_granted`, for a task, a batch and a
    workflow step, and a revoke takes effect on the next renewal;
  * the signing route fences like the egress ticket's: a stale generation is
    409 and the signer is never called; the payload's `aud` is exactly
    `https://<console host>/*` and its life the configured one, clamped; the
    answer is `no-store` and no log line carries the credential;
  * the QA account's writes need the console's own Origin (`qa_identity_origin`);
  * `GET /v1/qa-identities/mine` tells any member whether its own tenant is
    granted, and nothing else;
  * §4.9, M3 measured NO: with an IAP assertion present the `Authorization`
    header is never read, every request carrying one logs whether
    `Authorization` and `Proxy-Authorization` were present (never a value),
    and no error page reflects a credential header's value;
  * M4: `/v1/tenants/mine` lists a service account's own workspace, first, as
    its default.

Every fake JWT, token and assertion is built at run time from parts.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from typing import Any

import pytest
from fastapi import Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from swarm_common.identity import AuthError, tenant_id_for_user

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import ApiError, ValidationFailed
from swarm_api.handoff import LIVE_AUDIT
from swarm_api.main import UNCAPTURED_ROUTES, create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    CHILD_KEY,
    LIVE_KEY,
    TASK,
    Attempt,
    CommitLog,
    _b64url,
    _tokens,
    clock,
    groups,
    ldb,
    relay,
    seed_running,
)

SIGNER = LocalSpecSigner()
HOST = "console.example.com"
CONSOLE = f"https://{HOST}"
#: The QA account, as an address: built from parts, and never the deployment's.
QA = "qa-console" + "@" + "qa-project.iam.gserviceaccount.com"
QA_TENANT = tenant_id_for_user(QA)
PATH = f"/v1/attempts/{ATTEMPT}/qa-identity"
MINE = "/v1/qa-identities/mine"
ADMIN = "/v1/admin/qa-identities"
GRANT = f"{ADMIN}/console/grants"


def _assertion(who: str) -> str:
    return "-".join(("iap", who, secrets.token_hex(8)))


class FakeIap:
    """The IAP assertion verifier: assertion -> claims, counting calls."""

    configured = True

    def __init__(self) -> None:
        self.claims: dict[str, dict[str, Any]] = {}
        self.calls = 0

    def add(self, email: str) -> str:
        assertion = _assertion(email.split("@")[0])
        self.claims[assertion] = {"email": email, "sub": f"accounts.google.com:{email}"}
        return assertion

    def verify(self, assertion: str) -> dict[str, Any]:
        self.calls += 1
        if assertion not in self.claims:
            raise AuthError("IAP assertion verification failed")
        return dict(self.claims[assertion])


class CountingVerifier(StaticTokenVerifier):
    """The bearer verifier, counting what it was asked to verify."""

    def __init__(self, tokens: dict[str, dict[str, Any]]) -> None:
        super().__init__(tokens)
        self.seen: list[str] = []

    def verify(self, token: str) -> dict[str, Any]:
        self.seen.append(token)
        return super().verify(token)


class FakeJwtSigner:
    """IAM Credentials' signJwt: records each payload, answers a JWT-shaped
    string made at run time."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False
        self.issued: list[str] = []

    def sign_jwt(self, account: str, claims: dict[str, Any]) -> str:
        self.calls.append((account, dict(claims)))
        if self.fail:
            raise RuntimeError("signJwt answered 403")
        jwt = ".".join((_b64url(b'{"alg":"RS256"}'), _b64url(json.dumps(claims).encode()),
                        secrets.token_hex(32)))
        self.issued.append(jwt)
        return jwt


@pytest.fixture
def people() -> dict[str, tuple[str, ...]]:
    """alice and dave are eng members, bob is research, ops is a platform admin."""
    return {
        "alice@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "dave@example.com": (ENG_GROUP,),
        "ops@example.com": (ADMIN_GROUP,),
    }


def _settings(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "child_key": CHILD_KEY,
        "live_key": LIVE_KEY,
        "console_url": CONSOLE,
        "qa_identity_console_account": QA,
        "allowed_users": (QA,),
        "spec_signing_key_version": KEY_VERSION,
        "spec_verify_keys": json.dumps({KEY_VERSION: SIGNER.public_pem()}),
    }
    base.update(over)
    return base


class Harness:
    def __init__(self, client: TestClient, db: CommitLog, iap: FakeIap,
                 verifier: CountingVerifier, signer: FakeJwtSigner) -> None:
        self.client = client
        self.db = db
        self.iap = iap
        self.verifier = verifier
        self.signer = signer

    def as_(self, user: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {'-'.join(('token', user))}", **extra}

    def as_qa(self, **extra: str) -> dict[str, str]:
        return {"X-Goog-IAP-JWT-Assertion": self.iap.add(QA), **extra}


def _build(db, people, groups, objects, clock, **over: Any) -> Harness:
    tokens = _tokens(people)
    verifier = CountingVerifier(tokens)
    ctx = build_context(
        settings=api_settings(**_settings(**over)),
        db=db,
        verifier=verifier,
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        signer=SIGNER,
    )
    iap = FakeIap()
    ctx.authenticator._iap = iap
    app = create_app(ctx)
    signer = FakeJwtSigner()
    app.state.qa_identity_signer = signer
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    # The QA account's own service tenant, as the workspace flow writes it.
    seed_tenant(db, QA_TENANT)
    db.docs[f"tenants/{QA_TENANT}"].update({"kind": "user", "principal": QA})
    return Harness(TestClient(app, raise_server_exceptions=False), db, iap, verifier, signer)


@pytest.fixture
def make(ldb, people, groups, objects, clock):
    def _make(**over: Any) -> Harness:
        return _build(ldb, people, groups, objects, clock, **over)
    return _make


@pytest.fixture
def h(make) -> Harness:
    return make()


def grant(h: Harness, tenant: str = "eng", reason: str = "console visual QA") -> Any:
    return h.client.put(f"{GRANT}/{tenant}", headers=h.as_("ops"), json={"reason": reason})


def revoke(h: Harness, tenant: str = "eng") -> Any:
    return h.client.delete(f"{GRANT}/{tenant}", headers=h.as_("ops"))


# --------------------------------------------------------------------------
# Submission (§4.3)
# --------------------------------------------------------------------------


def _task(**extra: Any) -> dict[str, Any]:
    return {"runner_profile": "claude-code-browser",
            "input": {"prompt": "Sweep the console.", **extra}}


def _workflow(**extra: Any) -> dict[str, Any]:
    return {"steps": [{"step_id": "sweep", "runner_profile": "claude-code-browser",
                       "input": {"prompt": "Sweep the console.", **extra}}]}


SUBMISSIONS = [
    ("/v1/tasks", lambda **x: _task(**x)),
    ("/v1/tasks/batch", lambda **x: {"tasks": [_task(**x)]}),
    ("/v1/workflows", lambda **x: _workflow(**x)),
]


@pytest.mark.parametrize("path,body", SUBMISSIONS)
def test_an_ungranted_tenants_submission_is_refused(h, path, body):
    response = h.client.post(path, headers=h.as_("alice"), json=body(qa_identity="console"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_identity_not_granted"
    assert "platform admin" in response.json()["message"]


@pytest.mark.parametrize("path,body", SUBMISSIONS)
def test_a_granted_tenant_submits_and_a_revoked_one_is_refused_again(h, path, body):
    assert grant(h).status_code == 200
    response = h.client.post(path, headers=h.as_("alice"), json=body(qa_identity="console"))
    assert response.status_code == 201, response.text
    assert revoke(h).status_code == 200
    response = h.client.post(path, headers=h.as_("alice"), json=body(qa_identity="console"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_identity_not_granted"


def test_another_tenants_grant_admits_nobody_else(h):
    assert grant(h, "research").status_code == 200
    response = h.client.post("/v1/tasks", headers=h.as_("alice"), json=_task(qa_identity="console"))
    assert response.json()["code"] == "qa_identity_not_granted"


def test_a_submission_without_the_identity_reads_no_grant(h):
    response = h.client.post("/v1/tasks", headers=h.as_("alice"), json=_task())
    assert response.status_code == 201, response.text


def test_a_deployment_without_the_account_refuses_the_submission_unconfigured(make):
    h = make(qa_identity_console_account="")
    response = h.client.post("/v1/tasks", headers=h.as_("alice"), json=_task(qa_identity="console"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "qa_identity_unconfigured"


# --------------------------------------------------------------------------
# The worker's signing route (§4.3)
# --------------------------------------------------------------------------


def _qa_message(path: str, body: bytes, stamp: str) -> bytes:
    from swarm_api.qa_identity import qa_identity_message

    return qa_identity_message("POST", path, body, stamp)


def ask(h: Harness, attempt: Attempt, *, signed: bool = True, **over: Any):
    body = attempt.body(identity="console", **over)
    headers = {"Authorization": f"Bearer {'-'.join(('token', 'worker', 'eng'))}",
               "Content-Type": "application/json"}
    if signed:
        stamp = str(int(time.time()))
        headers["X-Swarm-Attempt-Proof"] = _b64url(attempt.private.sign(_qa_message(PATH, body, stamp)))
        headers["X-Swarm-Attempt-Timestamp"] = stamp
    return h.client.post(PATH, content=body, headers=headers)


def running(h: Harness, attempt: Attempt | None = None, *, requested: bool = True) -> tuple[Attempt, dict]:
    attempt = attempt or Attempt()
    doc = seed_running(h.client, h.db, attempt)
    doc["input"] = {"prompt": "Sweep the console.", **({"qa_identity": "console"} if requested else {})}
    SIGNER.sign_document(doc, TASK)
    return attempt, doc


def _claims(jwt: str) -> dict[str, Any]:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_a_granted_live_attempt_gets_a_credential_bound_to_the_console(h):
    grant(h)
    attempt, _ = running(h)
    response = ask(h, attempt)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["identity"], body["host"], body["scheme"]) == ("console", HOST, "Bearer")
    # M3 measured NO: Chromium refuses to add Proxy-Authorization, so the gate
    # sends the JWT in Authorization (qa-identity-measurements.md).
    assert body["header"] == "Authorization"
    assert body["expires_in"] == 900
    assert body["credential"] == h.signer.issued[-1]
    account, claims = h.signer.calls[-1]
    assert account == QA
    assert claims["aud"] == f"https://{HOST}/*"
    assert claims["iss"] == QA and claims["sub"] == QA
    assert claims["exp"] - claims["iat"] == 900
    assert claims["jti"] == body["jti"]


@pytest.mark.parametrize("configured,expected", [(60, 300), (900, 900), (1800, 1800), (86400, 3600)])
def test_the_life_is_the_configured_one_clamped(make, configured, expected):
    h = make(qa_identity_lifetime_seconds=configured)
    grant(h)
    attempt, _ = running(h)
    response = ask(h, attempt)
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == expected
    _, claims = h.signer.calls[-1]
    assert claims["exp"] - claims["iat"] == expected


def test_the_answer_is_no_store_and_no_log_line_carries_the_credential(h, caplog):
    caplog.set_level(logging.DEBUG)
    grant(h)
    attempt, _ = running(h)
    response = ask(h, attempt)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    credential = response.json()["credential"]
    signature = credential.rsplit(".", 1)[1]
    logged = "\n".join(
        r.getMessage() + (r.exc_text or "") + json.dumps(r.__dict__.get("args"), default=str)
        for r in caplog.records
    )
    assert any("qa identity issued" in r.getMessage() for r in caplog.records), \
        "the control: the route logs its issue, so a scan that finds nothing scanned something"
    assert credential not in logged and signature not in logged


def test_the_issue_is_audited_without_the_credential(h):
    grant(h)
    attempt, _ = running(h)
    body = ask(h, attempt).json()
    rows = [d for p, d in h.db.docs.items()
            if p.startswith(f"{LIVE_AUDIT}/") and d.get("entry") == "qa_identity_issued"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["tenant_id"], row["task_id"], row["attempt_id"], row["generation"]) == (
        "eng", TASK, ATTEMPT, 1)
    assert row["detail"]["identity"] == "console" and row["detail"]["jti"] == body["jti"]
    assert body["credential"] not in json.dumps(h.db.docs, default=str)


def test_the_route_is_never_captured():
    assert PATH.replace(ATTEMPT, "{attempt_id}") in UNCAPTURED_ROUTES


def test_a_stale_generation_is_409_and_never_reaches_the_signer(h):
    grant(h)
    attempt, doc = running(h)
    # The reconciler moved the task on: its live generation is 2 now.
    doc["current_generation"] = 2
    response = ask(h, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "stale_generation"
    assert "credential" not in response.json()
    assert h.signer.calls == []


def test_a_released_lease_is_409_lease_not_live(h):
    grant(h)
    attempt, _ = running(h)
    h.db.docs[f"leases/{attempt.tuple.lease_id}"]["released_at"] = h.db.docs[
        f"leases/{attempt.tuple.lease_id}"]["created_at"]
    response = ask(h, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "lease_not_live"
    assert h.signer.calls == []


def test_without_the_attempt_proof_it_is_bad_signature(h):
    grant(h)
    attempt, _ = running(h)
    response = ask(h, attempt, signed=False)
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "bad_signature"
    assert h.signer.calls == []


def test_a_live_session_signature_does_not_sign_here(h):
    grant(h)
    attempt, _ = running(h)
    body = attempt.body(identity="console")
    headers = {"Authorization": f"Bearer {'-'.join(('token', 'worker', 'eng'))}",
               "Content-Type": "application/json", **attempt.sign(PATH, body)}
    response = h.client.post(PATH, content=body, headers=headers)
    assert response.status_code == 401, response.text
    assert h.signer.calls == []


def test_a_task_whose_signed_input_does_not_name_it_is_not_requested(h):
    grant(h)
    attempt, _ = running(h, requested=False)
    response = ask(h, attempt)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "qa_identity_not_requested"
    assert h.signer.calls == []


def test_an_input_rewritten_after_signing_is_refused(h):
    grant(h)
    attempt, doc = running(h, requested=False)
    doc["input"]["qa_identity"] = "console"  # the worker's own edit, unsigned
    response = ask(h, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "spec_unverified"
    assert h.signer.calls == []


def test_a_revoke_between_issue_and_renewal_refuses_the_renewal(h):
    grant(h)
    attempt, _ = running(h)
    assert ask(h, attempt).status_code == 200
    assert revoke(h).status_code == 200
    response = ask(h, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_identity_not_granted"
    assert len(h.signer.calls) == 1


def test_an_unconfigured_deployment_answers_503_at_signing(make):
    h = make(qa_identity_console_account="")
    attempt, _ = running(h)
    response = ask(h, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "qa_identity_unconfigured"


def test_a_signer_failure_is_503_sign_failed(h):
    grant(h)
    attempt, _ = running(h)
    h.signer.fail = True
    response = ask(h, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "qa_identity_sign_failed"
    assert "credential" not in response.json()


def test_twelve_an_hour_per_attempt(h):
    grant(h)
    attempt, _ = running(h)
    for _ in range(12):
        assert ask(h, attempt).status_code == 200
    response = ask(h, attempt)
    assert response.status_code == 429, response.text
    assert len(h.signer.calls) == 12


# --------------------------------------------------------------------------
# The admin routes (§4.2, §4.3)
# --------------------------------------------------------------------------


def test_a_grant_and_its_audit_row_commit_together(h):
    response = grant(h, reason="console visual QA (#1649)")
    assert response.status_code == 200, response.text
    doc = h.db.docs["qa_identity_grants/eng"]
    assert (doc["tenant_id"], doc["identity"], doc["state"]) == ("eng", "console", "granted")
    assert doc["granted_by"] == "ops@example.com" and doc["reason"] == "console visual QA (#1649)"
    rows = [d for p, d in h.db.docs.items() if p.startswith(f"{AUDIT_COLLECTION}/")
            and d.get("action") == "qa_identity_granted"]
    assert len(rows) == 1
    assert rows[0]["target_tenant_id"] == "eng" and rows[0]["detail"]["identity"] == "console"
    together = [c for c in h.db.commits
                if "qa_identity_grants/eng" in {p for _, p in c}
                and any(p.startswith(f"{AUDIT_COLLECTION}/") for _, p in c)]
    assert len(together) == 1, h.db.commits


def test_an_audit_write_that_fails_leaves_no_grant(h, monkeypatch):
    from swarm_api import qa_identity as module

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(module, "_audit", broken)
    response = grant(h)
    assert response.status_code == 500
    assert "qa_identity_grants/eng" not in h.db.docs
    assert not [p for p in h.db.docs if p.startswith(f"{AUDIT_COLLECTION}/")]


def test_a_revoke_keeps_the_document_and_audits(h):
    grant(h)
    response = revoke(h)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "revoked"
    doc = h.db.docs["qa_identity_grants/eng"]
    assert doc["state"] == "revoked" and doc["revoked_by"] == "ops@example.com"
    actions = sorted(d["action"] for p, d in h.db.docs.items()
                     if p.startswith(f"{AUDIT_COLLECTION}/") and d["action"].startswith("qa_"))
    assert actions == ["qa_identity_granted", "qa_identity_revoked"]


def test_the_qa_workspace_cannot_be_granted_to_itself(h):
    response = grant(h, QA_TENANT)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_identity_self"


def test_an_unknown_tenant_is_404(h):
    response = grant(h, "nobody")
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("reason", ["", "x" * 201])
def test_a_reason_is_one_to_two_hundred_characters(h, reason):
    assert grant(h, reason=reason).status_code == 422


def test_only_a_platform_admin_grants_or_lists(h):
    response = h.client.put(f"{GRANT}/eng", headers=h.as_("alice"), json={"reason": "mine"})
    assert response.status_code == 403
    assert h.client.get(ADMIN, headers=h.as_("alice")).status_code == 403
    assert "qa_identity_grants/eng" not in h.db.docs


def test_the_admin_list_names_the_account_host_and_grants(h):
    grant(h)
    body = h.client.get(ADMIN, headers=h.as_("ops")).json()
    (entry,) = body["identities"]
    assert (entry["identity"], entry["host"], entry["account"], entry["configured"]) == (
        "console", HOST, QA, True)
    assert [(g["tenant_id"], g["state"]) for g in entry["grants"]] == [("eng", "granted")]


# --------------------------------------------------------------------------
# A member reads its own tenant's grant (§4.3)
# --------------------------------------------------------------------------


def test_a_member_reads_its_own_tenants_grant_and_no_other(h):
    grant(h)
    eng = h.client.get(MINE, headers=h.as_("dave")).json()
    assert eng == {"identities": [{"identity": "console", "configured": True, "granted": True}]}
    research = h.client.get(MINE, headers=h.as_("bob")).json()
    assert research == {"identities": [{"identity": "console", "configured": True, "granted": False}]}


def test_any_workspace_role_may_read_mine():
    from swarm_api.roles import VIEWER, required_role

    assert required_role("GET", MINE) == VIEWER


# --------------------------------------------------------------------------
# The front door (§4.3): the QA account's writes carry the console's Origin
# --------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["https://evil.example", None, f"http://{HOST}",
                                    f"https://{HOST}.evil.example"])
def test_a_qa_write_from_another_origin_is_refused(h, origin):
    headers = h.as_qa(**({"Origin": origin} if origin else {}))
    response = h.client.post("/v1/notifications/read-all", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_identity_origin"


def test_a_qa_write_from_the_console_passes_and_reads_need_no_origin(h):
    response = h.client.post("/v1/notifications/read-all", headers=h.as_qa(Origin=CONSOLE))
    assert response.json().get("code") != "qa_identity_origin", response.text
    assert response.status_code < 400, response.text
    response = h.client.get("/v1/tenants/me", headers=h.as_qa(Origin="https://evil.example"))
    assert response.status_code == 200, response.text


def test_a_person_is_not_held_to_the_qa_origin_rule(h):
    headers = {"X-Goog-IAP-JWT-Assertion": h.iap.add("alice@example.com"),
               "Origin": "https://evil.example"}
    response = h.client.post("/v1/notifications/read-all", headers=headers)
    assert response.json().get("code") != "qa_identity_origin", response.text


# --------------------------------------------------------------------------
# §4.9: an IAP assertion means Authorization is never read
# --------------------------------------------------------------------------


def test_with_an_assertion_the_bearer_is_never_read(h):
    headers = {"X-Goog-IAP-JWT-Assertion": h.iap.add("dave@example.com"),
               **h.as_("alice")}
    response = h.client.get("/v1/tenants/me", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["principal"]["email"] == "dave@example.com"
    assert h.verifier.seen == []


def test_an_assertion_with_iap_unpinned_never_falls_back_to_the_bearer(h):
    h.iap.configured = False
    headers = {"X-Goog-IAP-JWT-Assertion": _assertion("dave"), **h.as_("alice")}
    response = h.client.get("/v1/tenants/me", headers=headers)
    assert response.status_code == 401, response.text
    assert h.verifier.seen == []


def _probe(h: Harness) -> None:
    app = h.client.app

    async def headers_seen(
        authorization: str | None = Header(default=None),
        proxy_authorization: str | None = Header(default=None),
        x_serverless_authorization: str | None = Header(default=None),
    ) -> dict:
        return {"authorization": authorization, "proxy": proxy_authorization,
                "serverless": x_serverless_authorization}

    app.add_api_route("/probe/headers", headers_seen, methods=["GET"])


def test_a_route_never_sees_authorization_beside_an_assertion(h):
    _probe(h)
    value = "Bearer " + secrets.token_hex(16)
    response = h.client.get("/probe/headers", headers={
        "X-Goog-IAP-JWT-Assertion": h.iap.add(QA), "Authorization": value,
        "Proxy-Authorization": value, "X-Serverless-Authorization": value})
    assert response.json() == {"authorization": None, "proxy": None, "serverless": None}
    # The control: without an assertion the worker's bearer still arrives.
    response = h.client.get("/probe/headers", headers={"Authorization": value})
    assert response.json()["authorization"] == value


def _presence(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "proxy_authorization_present=" in r.getMessage()]


def test_each_iap_request_logs_header_presence_and_never_a_value(h, caplog):
    caplog.set_level(logging.INFO)
    value = "Bearer " + secrets.token_hex(16)
    h.client.get("/v1/tenants/me", headers=h.as_qa(**{"Authorization": value,
                                                       "Proxy-Authorization": value}))
    lines = _presence(caplog)
    assert len(lines) == 1, lines
    assert "authorization_present=true" in lines[0]
    assert "proxy_authorization_present=true" in lines[0]
    assert value[7:] not in "\n".join(r.getMessage() for r in caplog.records)
    caplog.clear()
    h.client.get("/v1/tenants/me", headers=h.as_qa())
    lines = _presence(caplog)
    assert len(lines) == 1, lines
    assert "authorization_present=false" in lines[0]
    assert "proxy_authorization_present=false" in lines[0]
    caplog.clear()
    h.client.get("/v1/tenants/me", headers=h.as_("alice"))
    assert _presence(caplog) == [], "a request with no assertion is not an IAP request"


def _echoing_routes(h: Harness) -> None:
    """Routes that make each error handler try to reflect a header: what a
    careless route added tomorrow would do. The handlers must scrub it."""
    app = h.client.app

    def leak(authorization: str | None, proxy: str | None) -> str:
        return f"{authorization} {proxy}"

    async def api_error(authorization: str | None = Header(default=None),
                        proxy_authorization: str | None = Header(default=None)) -> dict:
        raise ApiError(leak(authorization, proxy_authorization),
                       detail={"echo": leak(authorization, proxy_authorization)})

    async def validation(authorization: str | None = Header(default=None),
                         proxy_authorization: str | None = Header(default=None)) -> dict:
        raise ValidationFailed(leak(authorization, proxy_authorization))

    async def auth_error(authorization: str | None = Header(default=None),
                         proxy_authorization: str | None = Header(default=None)) -> dict:
        raise AuthError(leak(authorization, proxy_authorization))

    async def http_error(authorization: str | None = Header(default=None),
                         proxy_authorization: str | None = Header(default=None)) -> dict:
        raise HTTPException(status_code=400, detail=leak(authorization, proxy_authorization))

    async def request_validation(authorization: str | None = Header(default=None),
                                 proxy_authorization: str | None = Header(default=None)) -> dict:
        raise RequestValidationError([{"loc": ("header", "authorization"),
                                       "msg": leak(authorization, proxy_authorization),
                                       "type": "value_error"}])

    for name, route in (("api", api_error), ("validation", validation), ("auth", auth_error),
                        ("http", http_error), ("request", request_validation)):
        app.add_api_route(f"/probe/echo/{name}", route, methods=["GET"])


@pytest.mark.parametrize("name", ["api", "validation", "auth", "http", "request"])
def test_no_error_page_reflects_a_credential_header(h, name):
    _echoing_routes(h)
    token = secrets.token_hex(20)
    proxy = secrets.token_hex(20)
    response = h.client.get(f"/probe/echo/{name}", headers={
        "Authorization": f"Bearer {token}", "Proxy-Authorization": f"Bearer {proxy}"})
    assert response.status_code >= 400
    assert token not in response.text and proxy not in response.text, response.text
    assert "[redacted]" in response.text, "the control: the route did try to echo"


# --------------------------------------------------------------------------
# M4: a service account's own workspace is listed, first, as its default
# --------------------------------------------------------------------------


def test_the_qa_account_lists_its_own_workspace_as_its_default(h):
    me = h.client.get("/v1/tenants/me", headers=h.as_qa())
    assert me.status_code == 200, me.text
    assert me.json()["tenant"]["tenant_id"] == QA_TENANT
    mine = h.client.get("/v1/tenants/mine", headers=h.as_qa())
    assert mine.status_code == 200, mine.text
    entries = mine.json()
    assert [e["tenant_id"] for e in entries] == [QA_TENANT]
    assert entries[0]["is_default"] is True
    # Selecting it by name acts in the same tenant.
    named = h.client.get("/v1/tenants/me", headers=h.as_qa(**{"X-Swarm-Tenant": QA_TENANT}))
    assert named.json()["tenant"]["tenant_id"] == QA_TENANT


def test_a_derived_tenant_naming_another_principal_is_not_listed(h):
    h.db.docs[f"tenants/{QA_TENANT}"]["principal"] = "someone-else@example.com"
    mine = h.client.get("/v1/tenants/mine", headers=h.as_qa())
    assert mine.status_code == 200, mine.text
    assert mine.json() == []
