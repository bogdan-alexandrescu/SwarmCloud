"""The workspace secrets release, swarm-api's half (SEC-RELEASE, part of #1347).

Design: docs/design/workspace-secrets.md §3.3, §4.1, §4.4, §4.5 and the owner
decisions of §10. Drives the real route, `POST /v1/attempts/{attempt_id}/
secrets:release`, over the in-memory Firestore, with the tenant's worker
identity from the static verifier, an attempt key registered exactly as a
worker registers one (`test_child_tasks_api`) and a task signed as submission
signs it (`spec_signer.LocalSpecSigner`). Records and grants are written by
the store's own code (`WorkspaceSecrets`), so the release reads what SEC-
STORE-API writes. Secret Manager is an in-memory stand-in: THE ONE PLACE A
VALUE LIVES in these tests, as it is the one place it lives in production.

The attackers these tests stand in for:

  * A STALE WORKER (invariant 5): a superseded generation, a released lease
    or a finished task gets 409 and nothing, and nothing is read.
  * AN AGENT IN THE POD: it cannot sign as the worker, so an unsigned call,
    a call signed by another key, or signed under another route's purpose
    gets 403 and nothing. It CAN write Firestore documents whose id it can
    guess, so a forged grant or record naming another workspace's secret is
    released nothing, and a rewritten task document (another profile, another
    browser_tool, another submitter) fails the spec signature.
  * ANOTHER WORKSPACE: its secrets are never in this workspace's release.
  * A PERSON'S GROUP COLLEAGUE (owner decision Q3): a personal secret reaches
    a group task only with its opt-in, its owner's own submission and their
    current membership; each of the three missing releases nothing.
  * A LOG OR DATABASE READER (§4.5): a runtime-built value is in no captured
    log line and no Firestore document.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like
a credential).
"""

from __future__ import annotations

import json
import logging
import logging.config
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.childkey import request_message
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import UNCAPTURED_ROUTES, create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import attempts_secrets as release_mod
from swarm_api.routes.attempts_secrets import RELEASE_ROUTE, ReadUnavailable, release_message
from swarm_api.waker import NullWaker
from swarm_api import workspace_secrets as ws_mod
from swarm_api.workspace_secrets import (
    GRANTS_COLLECTION,
    RELEASE_MARKS_COLLECTION,
    RELEASES_COLLECTION,
    SECRETS_COLLECTION,
    WorkspaceSecrets,
)
from swarm_common.models import Tenant

from .conftest import ENG_GROUP, PROJECT, api_settings, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    _b64url,
    register,
    seed_parent,
    worker_tokens,
)

SIGNER = LocalSpecSigner()
ALICE = "alice@example.com"
ROOT = "root@example.com"


def fake_value(fill: str = "q") -> str:
    """Shaped like an API key, built at runtime so no literal looks like one."""
    return "sk" + "-" + fill * 36 + secrets.token_hex(4)


class FakeSecretManager:
    """Secret Manager, in memory: the store's writer and the release's reader."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.fail: str | None = None

    def create_secret(self, tenant_id: str, secret_id: str) -> None:
        pass

    def add_version(self, secret_id: str, payload: bytes) -> str:
        n = sum(1 for v in self.payloads if f"/secrets/{secret_id}/" in v) + 1
        # The project NUMBER, as Secret Manager answers: the release rebuilds
        # the name under its own project id before it reads.
        version = f"projects/123456789/secrets/{secret_id}/versions/{n}"
        self.payloads[f"projects/{PROJECT}/secrets/{secret_id}/versions/{n}"] = payload
        return version

    def disable_version(self, version: str) -> None:
        pass

    def destroy_versions(self, secret_id: str) -> int:
        return 0

    def access(self, version: str) -> bytes:
        self.reads.append(version)
        if self.fail:
            raise ReadUnavailable(self.fail)
        return self.payloads[version]


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


def _settings(**extra: Any):
    return api_settings(child_key=KEY, spec_signing_key_version=KEY_VERSION,
                        spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}), **extra)


def _client(db, worker_tokens, group_map, objects, sm) -> TestClient:  # noqa: F811
    ctx = build_context(
        settings=_settings(), db=db, verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
    )
    application = create_app(ctx)
    application.state.workspace_secret_reader = sm
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def client(db, worker_tokens, group_map, objects, sm) -> TestClient:  # noqa: F811
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _personal(db, "u-alice", ALICE)
    return _client(db, worker_tokens, group_map, objects, sm)


def _personal(db, tenant_id: str, email: str) -> Tenant:
    tenant = Tenant(tenant_id=tenant_id, kind="user", principal=email,
                    created_at=datetime.now(timezone.utc), display_name=email)
    db.docs[f"tenants/{tenant_id}"] = asdict(tenant)
    return tenant


def _tenant(db, tenant_id: str) -> Tenant:
    doc = dict(db.docs[f"tenants/{tenant_id}"])
    return Tenant(**{k: doc[k] for k in Tenant.__dataclass_fields__ if k in doc})


def add_secret(db, sm, tenant_id: str, name: str, value: str, *, grants: tuple[str, ...] = (),
               slot: str | None = None, provider: str = "other", kind: str = "api_key",
               by: str = ROOT, use_in: tuple[str, ...] = ()) -> dict[str, Any]:
    """A record, its grants and its opt-in list, written by the store's own code."""
    store = WorkspaceSecrets(db, sm, now=lambda: datetime.now(timezone.utc))
    store.create(tenant=_tenant(db, tenant_id), name=name, provider=provider, kind=kind,
                 payload=value.encode(), by=by, via_link=False)
    for consumer in grants:
        store.set_grant(tenant_id=tenant_id, name=name, consumer=consumer, slot=slot,
                        enabled=True, by=by)
    if use_in:
        store.set_use_in(tenant_id=tenant_id, name=name, workspaces=list(use_in), by=by)
    return store.find(tenant_id, name)


def untick(db, sm, tenant_id: str, name: str, consumer: str) -> None:
    WorkspaceSecrets(db, sm, now=lambda: datetime.now(timezone.utc)).set_grant(
        tenant_id=tenant_id, name=name, consumer=consumer, slot=None, enabled=False, by=ROOT)


def running(client, db, attempt: Attempt | None = None, *, profile: str = "claude-code",
            task_input: dict[str, Any] | None = None, submitted_by: str = ALICE) -> Attempt:
    """A task registered while STARTING, signed as submission signs it, then RUNNING."""
    attempt = attempt or Attempt()
    doc = seed_parent(db, attempt, state="STARTING", runner_profile=profile,
                      input=dict(task_input or {"prompt": "do it"}), submitted_by=submitted_by)
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    SIGNER.sign_document(doc, attempt.tuple.task_id)
    doc["state"] = "RUNNING"
    return attempt


def sign_release(attempt: Attempt, path: str, body: bytes, *, purpose_message=release_message,
                 at: float | None = None) -> dict[str, str]:
    stamp = str(int(at if at is not None else time.time()))
    signature = attempt.private.sign(purpose_message("POST", path, body, stamp))
    return {"X-Swarm-Attempt-Proof": _b64url(signature), "X-Swarm-Attempt-Timestamp": stamp}


def release(client, attempt: Attempt, *, generation: int | None = None,
            token: str | None = WORKER_TOKEN, sign: bool = True,
            body: dict[str, Any] | None = None, signer: Attempt | None = None,
            purpose_message=release_message) -> Any:
    t = attempt.tuple
    raw = json.dumps(body if body is not None else
                     {"generation": t.generation if generation is None else generation}).encode()
    path = RELEASE_ROUTE.replace("{attempt_id}", t.attempt_id)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update(sign_release(signer or attempt, path, raw, purpose_message=purpose_message))
    return client.post(path, content=raw, headers=headers)


def names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(item["name"] for item in response.json()["secrets"])


def rows(db) -> list[dict[str, Any]]:
    return [doc for path, doc in sorted(db.docs.items())
            if path.startswith(f"{RELEASES_COLLECTION}/")]


def marks(db) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items()
            if path.startswith(f"{RELEASE_MARKS_COLLECTION}/")]


# --------------------------------------------------------------------------
# the release, once, with its rows
# --------------------------------------------------------------------------


def test_an_attempt_is_released_its_granted_secret_once_with_one_row(client, db, sm):
    value = fake_value()
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    attempt = running(client, db)
    first = release(client, attempt)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["secrets"] == [{
        "name": "brave-key", "slot": "files", "kind": "api_key",
        "version": first.json()["secrets"][0]["version"], "value": value,
    }]
    [row] = rows(db)
    assert row["task_id"] == attempt.tuple.task_id
    assert row["attempt_id"] == attempt.tuple.attempt_id
    assert row["generation"] == attempt.tuple.generation
    assert row["tenant_id"] == "eng" and row["submitted_by"] == ALICE
    assert row["consumers"] == ["profile:claude-code"]
    assert row["secret_id"].startswith("swarm-tenant-eng-wsec-")
    # Owner decision Q6: kept 90 days by a Firestore TTL on expire_at.
    assert row["expire_at"] - row["at"] == timedelta(days=90)
    assert [m["attempt_id"] for m in marks(db)] == [attempt.tuple.attempt_id]
    assert marks(db)[0]["expire_at"] - marks(db)[0]["at"] == timedelta(days=90)

    # ONCE PER ATTEMPT: a second call is 410, reads nothing and writes nothing.
    reads = len(sm.reads)
    again = release(client, attempt)
    assert again.status_code == 410, again.text
    assert again.json()["code"] == "secrets_release_spent"
    assert value not in again.text
    assert len(sm.reads) == reads and len(rows(db)) == 1


def test_an_attempt_with_no_grant_is_released_nothing_and_marked(client, db, sm):
    add_secret(db, sm, "eng", "unticked", fake_value())
    attempt = running(client, db)
    assert names(release(client, attempt)) == []
    assert rows(db) == [] and sm.reads == []
    assert len(marks(db)) == 1


# --------------------------------------------------------------------------
# invariant 5: a stale worker holds nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["superseded", "lease_released", "finished", "wrong_lease"])
def test_a_stale_generation_gets_409_and_nothing(client, db, sm, change):
    value = fake_value()
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    attempt = running(client, db)
    t = attempt.tuple
    task = db.docs[f"tasks/{t.task_id}"]
    if change == "superseded":
        task["current_generation"] = t.generation + 1
    elif change == "lease_released":
        db.docs[f"leases/{t.lease_id}"]["released_at"] = datetime.now(timezone.utc)
    elif change == "finished":
        task["state"] = "SUCCEEDED"
    else:
        task["current_lease_id"] = "lease_other"
    response = release(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "secrets_release_fenced"
    assert value not in response.text and "brave-key" not in response.text
    assert sm.reads == [] and rows(db) == [] and marks(db) == []


def test_a_worker_superseded_while_secret_manager_answered_is_sent_nothing(client, db, sm):
    value = fake_value()
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    attempt = running(client, db)
    task = db.docs[f"tasks/{attempt.tuple.task_id}"]
    original = sm.access

    def supersede_then_read(version: str) -> bytes:
        task["current_generation"] = attempt.tuple.generation + 1
        return original(version)

    sm.access = supersede_then_read  # type: ignore[method-assign]
    response = release(client, attempt)
    assert response.status_code == 409, response.text
    assert value not in response.text
    assert rows(db) == [] and marks(db) == []


def test_the_body_names_the_generation_and_another_one_is_unproven(client, db, sm):
    """The registration is for (tenant, task, attempt, lease, generation): a
    body naming another generation has no key to verify against."""
    add_secret(db, sm, "eng", "brave-key", fake_value(), grants=("profile:claude-code",))
    attempt = running(client, db)
    response = release(client, attempt, generation=attempt.tuple.generation + 1)
    assert response.status_code == 403, response.text
    assert sm.reads == [] and rows(db) == []


# --------------------------------------------------------------------------
# only the worker, signed under this route's purpose
# --------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["unsigned", "other_key", "child_purpose", "other_tenant_token",
                                 "no_token", "stale_timestamp", "unknown_attempt"])
def test_an_unsigned_or_wrongly_signed_call_gets_nothing(client, db, sm, how):
    value = fake_value()
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    attempt = running(client, db)
    if how == "unsigned":
        response = release(client, attempt, sign=False)
    elif how == "other_key":
        response = release(client, attempt, signer=Attempt())
    elif how == "child_purpose":
        response = release(client, attempt, purpose_message=request_message)
    elif how == "other_tenant_token":
        response = release(client, attempt, token=OTHER_WORKER_TOKEN)
    elif how == "no_token":
        response = release(client, attempt, token=None)
    elif how == "stale_timestamp":
        t = attempt.tuple
        raw = json.dumps({"generation": t.generation}).encode()
        path = RELEASE_ROUTE.replace("{attempt_id}", t.attempt_id)
        headers = {"Authorization": f"Bearer {WORKER_TOKEN}",
                   **sign_release(attempt, path, raw, at=time.time() - 3600)}
        response = client.post(path, content=raw, headers=headers)
    else:
        ghost = Attempt(attempt="att_nobody")
        response = release(client, ghost)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "secrets_release_unproven"
    assert value not in response.text and "brave-key" not in response.text
    assert sm.reads == [] and rows(db) == [] and marks(db) == []


@pytest.mark.parametrize("extra", ["name", "tenant_id", "consumer", "secret_id"])
def test_the_body_is_the_generation_and_nothing_else(client, db, sm, extra):
    attempt = running(client, db)
    probe = fake_value("z")
    response = release(client, attempt, body={"generation": 1, extra: probe})
    assert response.status_code == 422, response.text
    # The refusal names the rule, never the value it was sent.
    assert probe not in response.text
    assert sm.reads == [] and rows(db) == []


def test_a_rewritten_task_document_is_released_nothing(client, db, sm):
    """An agent can write its own task document: another profile there would
    widen its consumers, so the spec signature is checked first."""
    add_secret(db, sm, "eng", "codex-only", fake_value(), grants=("profile:codex",))
    attempt = running(client, db)
    db.docs[f"tasks/{attempt.tuple.task_id}"]["runner_profile"] = "codex"
    response = release(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "secrets_release_spec_unverified"
    assert sm.reads == [] and rows(db) == []


# --------------------------------------------------------------------------
# the consumers come from the signed spec
# --------------------------------------------------------------------------


def test_a_jev_browser_grant_is_not_released_to_a_playwright_attempt(client, db, sm, monkeypatch):
    """§3.6: a tool-level checkbox means something. With the TypeSafe slot
    delivered as a file (the control: Q8 withholds it by default, below), a
    `browser_tool: jev` attempt is released the jev-browser key and a
    `browser_tool: playwright` attempt of the same profile is not."""
    monkeypatch.setattr(ws_mod, "INJECT_ONLY_SLOTS", ())
    add_secret(db, sm, "eng", "typesafe-key", fake_value(), grants=("tool:jev-browser",),
               provider="typesafe")
    jev = running(client, db, profile="claude-code-browser", task_input={"browser_tool": "jev"})
    assert names(release(client, jev)) == ["typesafe-key"]

    playwright = running(client, db, Attempt(task="task_pw", attempt="att_pw", lease="lease_pw"),
                         profile="claude-code-browser", task_input={"browser_tool": "playwright"})
    answer = release(client, playwright)
    assert names(answer) == []
    assert answer.json()["withheld"] == []
    assert "tool:jev-browser" not in answer.json()["consumers"]


def test_the_typesafe_slot_never_enters_a_pod_by_owner_decision_q8(client, db, sm):
    """§10 Q8: the TypeSafe key reaches jev through the platform forwarder,
    never as a file in the pod. Matched, withheld by name, never read."""
    value = fake_value()
    add_secret(db, sm, "eng", "typesafe-key", value, grants=("tool:jev-browser",),
               provider="typesafe")
    attempt = running(client, db, profile="claude-code-browser", task_input={"browser_tool": "auto"})
    answer = release(client, attempt)
    assert names(answer) == []
    assert answer.json()["withheld"] == [{"name": "typesafe-key", "reason": "inject_only"}]
    assert value not in answer.text and sm.reads == [] and rows(db) == []


def test_an_untick_is_honoured_by_the_next_attempt(client, db, sm):
    add_secret(db, sm, "eng", "brave-key", fake_value(), grants=("profile:claude-code",))
    first = running(client, db)
    assert names(release(client, first)) == ["brave-key"]
    untick(db, sm, "eng", "brave-key", "profile:claude-code")
    second = running(client, db, Attempt(attempt="att_parent_2", lease="lease_parent_2",
                                         generation=2))
    assert names(release(client, second)) == []


# --------------------------------------------------------------------------
# invariant 9: another workspace's secret is never released
# --------------------------------------------------------------------------


def test_another_tenants_secret_is_never_in_a_release(client, db, sm):
    theirs = add_secret(db, sm, "research", "their-key", fake_value("r"),
                        grants=("profile:claude-code",))
    mine = add_secret(db, sm, "eng", "my-key", fake_value("m"), grants=("profile:claude-code",))

    # What a hostile agent in eng can write: a grant naming research's secret
    # under eng's tenant, and a record of eng's pointing at research's version.
    db.docs[f"{GRANTS_COLLECTION}/{theirs['secret_id']}~profile:codex"] = {
        "secret_id": theirs["secret_id"], "tenant_id": "eng", "consumer": "profile:claude-code",
        "slot": "files", "enabled": True,
    }
    forged = dict(mine, current_version=theirs["current_version"])
    db.docs[f"{SECRETS_COLLECTION}/{mine['secret_id']}"] = forged
    forged_id = "swarm-tenant-eng-wsec-" + "0" * 16
    db.docs[f"{SECRETS_COLLECTION}/{forged_id}"] = dict(theirs, tenant_id="eng",
                                                         name="stolen")
    db.docs[f"{GRANTS_COLLECTION}/{forged_id}~profile:claude-code"] = {
        "secret_id": forged_id, "tenant_id": "eng", "consumer": "profile:claude-code",
        "slot": "files", "enabled": True,
    }

    # And research's own record, rewritten to claim eng: only its NAME,
    # swarm-tenant-research-wsec-..., still says whose it is.
    db.docs[f"{SECRETS_COLLECTION}/{theirs['secret_id']}"]["tenant_id"] = "eng"

    attempt = running(client, db)
    answer = release(client, attempt)
    assert names(answer) == []
    assert theirs["secret_id"] not in json.dumps(sm.reads)
    assert all(not r.startswith(f"projects/{PROJECT}/secrets/swarm-tenant-research-")
               for r in sm.reads)


def test_a_secret_is_released_only_in_a_slot_its_consumer_declares(client, db, sm):
    """A grant document is tenant-writable: a forge slot or an undeclared one
    is refused again at read time."""
    record = add_secret(db, sm, "eng", "brave-key", fake_value(), grants=("profile:claude-code",))
    grant = db.docs[f"{GRANTS_COLLECTION}/{record['secret_id']}~profile:claude-code"]
    grant["slot"] = "forge"
    attempt = running(client, db)
    assert names(release(client, attempt)) == []


# --------------------------------------------------------------------------
# owner decision Q3: a person's own secret on a group task
# --------------------------------------------------------------------------


def _personal_secret(db, sm, *, use_in: tuple[str, ...] = ("eng",)) -> str:
    value = fake_value("p")
    add_secret(db, sm, "u-alice", "alice-key", value, grants=("profile:claude-code",),
               by=ALICE, use_in=use_in)
    return value


def test_a_personal_secret_reaches_a_group_task_with_opt_in_own_submission_and_membership(
        client, db, sm):
    value = _personal_secret(db, sm)
    attempt = running(client, db, submitted_by=ALICE)
    answer = release(client, attempt)
    assert names(answer) == ["alice-key"]
    assert answer.json()["secrets"][0]["value"] == value
    [row] = rows(db)
    assert row["tenant_id"] == "eng" and row["secret_tenant_id"] == "u-alice"


def test_without_the_opt_in_a_personal_secret_is_not_released(client, db, sm):
    _personal_secret(db, sm, use_in=())
    attempt = running(client, db, submitted_by=ALICE)
    assert names(release(client, attempt)) == []


def test_another_members_submission_is_not_released_a_personal_secret(client, db, sm):
    _personal_secret(db, sm)
    attempt = running(client, db, submitted_by=ROOT)  # root is in eng too
    assert names(release(client, attempt)) == []


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def release_log():
    """The route logger's own records, whatever configured logging before.

    `caplog` reads a handler on the ROOT logger, so it sees a record only if
    the route's logger is enabled and every logger up to the root propagates
    and the root still holds caplog's handler. Another test on the same xdist
    worker can undo any of the three (`dictConfig`, `configure_logging`
    replacing the root's handlers); this test then went red with the warning
    in captured stdout (run 39578760162, shard 5/8). A handler on the logger
    itself, with the logger enabled and at DEBUG, needs none of them."""
    logger = logging.getLogger(release_mod.__name__)
    saved = (logger.disabled, logger.level)
    handler = _Records()
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.disabled, logger.level = saved


def _a_lapsed_membership_releases_nothing_and_says_why(
        db, worker_tokens, group_map, objects, sm, records) -> None:  # noqa: F811
    seed_tenant(db, "eng")
    _personal(db, "u-alice", ALICE)
    value = _personal_secret(db, sm)
    lapsed = {**group_map, ALICE: ()}
    client = _client(db, worker_tokens, lapsed, objects, sm)
    attempt = running(client, db, submitted_by=ALICE)
    answer = release(client, attempt)
    assert names(answer) == []
    assert answer.json()["withheld"] == [{"name": "alice-key", "reason": "membership_lapsed"}]
    assert sm.reads == []
    assert any("no longer a member" in r.getMessage() for r in records)
    assert all(value not in r.getMessage() for r in records)


def test_a_lapsed_membership_releases_nothing_and_says_why_by_name(
        db, worker_tokens, group_map, objects, sm, release_log):  # noqa: F811
    _a_lapsed_membership_releases_nothing_and_says_why(db, worker_tokens, group_map, objects, sm, release_log)


@pytest.fixture
def logging_reconfigured_by_an_earlier_test():
    """What another test on the same xdist worker can leave behind (red on run
    39578760162, shard 5/8): `dictConfig` disabling every existing logger, the
    root's handlers replaced (as `configure_logging` does) and propagation off
    above the route's logger. Every logger's state is put back afterwards, so
    this test poisons nothing that runs after it."""
    manager = logging.root.manager
    loggers = [logging.root, *(lg for lg in manager.loggerDict.values() if isinstance(lg, logging.Logger))]
    saved = [(lg, lg.disabled, lg.propagate, lg.level, list(lg.handlers)) for lg in loggers]
    logging.getLogger(release_mod.__name__)  # exists before dictConfig, as it does once imported
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": True})
    logging.root.handlers = [logging.NullHandler()]
    logging.root.setLevel(logging.CRITICAL)
    logging.getLogger("swarm_api").propagate = False
    logging.getLogger("swarm_api.routes").propagate = False
    try:
        yield
    finally:
        for lg, disabled, propagate, level, handlers in saved:
            lg.disabled, lg.propagate, lg.level, lg.handlers = disabled, propagate, level, handlers
        for name, lg in list(manager.loggerDict.items()):
            if isinstance(lg, logging.Logger) and all(lg is not s[0] for s in saved):
                lg.disabled = False


def test_the_lapsed_membership_warning_is_seen_whatever_reconfigured_logging_first(
        logging_reconfigured_by_an_earlier_test, db, worker_tokens, group_map, objects, sm,
        release_log):  # noqa: F811
    # The fixture order matters: logging is reconfigured BEFORE release_log
    # attaches, as an earlier test would have done it.
    # MUTATION: pass caplog.records instead of release_log -- the root's handler is
    # gone and propagation stops at swarm_api, so the warning is never seen.
    # MUTATION: drop `logger.disabled = False` from release_log -- dictConfig
    # disabled the logger, nothing is emitted, and this fails.
    _a_lapsed_membership_releases_nothing_and_says_why(db, worker_tokens, group_map, objects, sm, release_log)


def test_a_rewritten_submitter_fails_the_signature(client, db, sm):
    _personal_secret(db, sm)
    attempt = running(client, db, submitted_by=ROOT)
    db.docs[f"tasks/{attempt.tuple.task_id}"]["submitted_by"] = ALICE
    response = release(client, attempt)
    assert response.status_code == 403
    assert sm.reads == []


# --------------------------------------------------------------------------
# §4.5: the value is nowhere but Secret Manager and the one response
# --------------------------------------------------------------------------


def test_a_value_is_in_no_log_line_and_no_firestore_document(client, db, sm, caplog):
    caplog.set_level(logging.DEBUG)
    value = fake_value("v")
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    attempt = running(client, db)
    answer = release(client, attempt)
    assert value in answer.text  # the one place it is served
    for record in caplog.records:
        assert value not in record.getMessage()
        assert value not in str(record.args or "")
    for path, doc in db.docs.items():
        assert value not in json.dumps(doc, default=str), path
    assert any(value.encode() == payload for payload in sm.payloads.values())


def test_a_secret_swarm_api_may_not_read_yet_is_503_with_no_name(client, db, sm):
    """Before the owner applies SEC-IAM's grant every read is
    PERMISSION_DENIED: a reason, no value, no secret name, nothing written."""
    value = fake_value()
    add_secret(db, sm, "eng", "brave-key", value, grants=("profile:claude-code",))
    sm.fail = "permission_denied"
    attempt = running(client, db)
    response = release(client, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "secrets_release_unavailable"
    assert "workspace_secrets.tf" in response.json()["message"]
    assert value not in response.text and "brave-key" not in response.text
    assert rows(db) == [] and marks(db) == []


def test_the_release_route_is_uncaptured_by_construction():
    assert RELEASE_ROUTE in UNCAPTURED_ROUTES
    assert release_mod.RELEASE_PURPOSE == "swarm-secrets-release/v1"
    # Read raw, never by a FastAPI body model whose validation error echoes input.
    route = next(r for r in release_mod.router.routes if getattr(r, "path", "") == RELEASE_ROUTE)
    assert route.body_field is None
    assert route.methods == {"POST"}


def test_no_child_key_means_no_release_path(db, worker_tokens, group_map, objects, sm):  # noqa: F811
    seed_tenant(db, "eng")
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
    )
    application = create_app(ctx)
    application.state.workspace_secret_reader = sm
    response = TestClient(application).post(
        RELEASE_ROUTE.replace("{attempt_id}", "att_x"), content=b'{"generation": 1}')
    assert response.status_code == 503
    assert sm.reads == []


def test_a_group_task_is_known_by_configuration_not_by_its_tenant_document(client, db, sm):
    """The membership re-check names the group from ApiSettings.tenant_groups,
    the walk tenant resolution makes; a tenant document is agent-writable."""
    _personal_secret(db, sm)
    db.docs["tenants/eng"]["principal"] = "somewhere-else@example.com"
    attempt = running(client, db, submitted_by=ALICE)
    assert names(release(client, attempt)) == ["alice-key"]
    assert ENG_GROUP.split("@")[0] == "eng"
