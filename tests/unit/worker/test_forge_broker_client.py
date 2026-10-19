"""The worker obtains a person's slot only from swarm-api's broker (#1041 step 4).

docs/design/user-scoped-secrets.md §3.2. A group tenant's workers share one
service account, so IAM cannot keep Alice's `git-u-` slot from Bob's attempt;
swarm-api can, and releases it only to an attempt that signs its request with
the attempt key its worker registered before the agent existed. The worker's
half, pinned here:

  * a `git-u-<hex>` suffix is NEVER read from Secret Manager -- it is POSTed
    for at `/v1/attempts/forge-credential`, signed with the attempt key, and
    the fake route below verifies that proof with swarm-api's own code
    (`swarm_api.childkey`) and parses the body with swarm-api's own strict
    model (`swarm_api.credbroker.ForgeCredentialRequest`);
  * `git` and `git-r-<hex>` are read directly, as before;
  * an attempt without a registered key, or one the broker refuses, gets no
    token and NEVER the tenant's: running a person's task on the team token
    would push as somebody it was not resolved to;
  * a superseded attempt is fenced, and a refused write is `ForgeWriteRefused`;
  * the token is registered for redaction before it is returned, and is in
    no environment, no log line and no event;
  * the 401 re-read asks the broker again;
  * with FORGE_BROKER off -- the default, and dev's setting even though dev
    has child tasks on and so hands its workers SWARM_API_URL -- the worker
    reads the slot with its own account, exactly as before #1041, while IAM
    allows it, and fails closed, without the tenant token, once IAM does not;
  * with FORGE_BROKER on and no SWARM_API_URL, it is refused, never a
    direct read;
  * an additional repository's slot (#1434) goes through the broker too.

Every fake token is built at runtime, never written as one literal.

MUTATIONS, each caught below: read a user slot with `client.access` -- the
never-from-Secret-Manager test sees the access. Sign a different path or body
-- the fake route's proof check refuses and the token test gets None. Fall
back to `resolve_git_token(provider="git")` on a refusal -- the fail-closed
test sees the tenant secret read. Register the token after returning it --
the redaction test sees it in the log. Drop the `SWARM_CHILD_PATH` check --
the child test sees a spool.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import forgebroker
from agent_worker import lifecycle
from agent_worker import secrets as secrets_mod
from agent_worker.errors import FencedError
from agent_worker.hardening import FAILED, MemoryProtection
from swarm_api import childkey as api_childkey
from swarm_api import children as api_children
from swarm_api import credbroker as api_credbroker
from swarm_api import gittokens

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt
from test_forge_credential import (
    GITHUB_URL,
    PERSON,
    _clone_from,
    _seed_user_task,
    seed_grant,
)
from test_strategy_end_to_end import forge, local_urls, origin  # noqa: F401 -- fixtures

NONCE = "a1b2c3d4-broker-nonce-not-a-secret"
API = "https://swarm-api.example"


def user_token(fill: str = "x") -> str:
    """A GitHub user access token's shape, built here so no literal is one."""
    return "ghu_" + fill * 36


def tenant_token() -> str:
    return "ghp_" + "t" * 36


def slot(user: str = PERSON) -> str:
    return gittokens.provider_suffix(gittokens.Scope.USER, user=user)


def slot_secret(user: str = PERSON) -> str:
    return f"swarm-tenant-{TENANT}-{slot(user)}"


class FakeBrokerRoutes:
    """swarm-api's child-key registration and its forge-credential broker.

    The proof is verified with swarm-api's code, and the body is parsed by its
    strict request model, so a worker that signed the wrong bytes or sent a
    field the route does not accept fails here as it would against the route.
    `values` is the slots the broker holds, by secret NAME; `accessed` lists
    every slot it released, in order, the way `FakeSecretClient.accessed`
    lists reads. `refuse` makes it answer that refusal code instead.
    """

    def __init__(self, values: dict[str, str] | None = None, *, secret: str | None = None) -> None:
        self.values = dict(values or {})
        self.secret = secret or next(iter(self.values), slot_secret())
        self.accessed: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.refuse: tuple[int, str] | None = None

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            payload = json.loads(body)
            self.public_key = payload.get("public_key")
            return 201, {"registered": "key" if self.public_key else "tombstone"}
        if method == "GET":  # the child listing a root attempt reads after its agent
            return 200, {"children": []}
        assert (method, path) == ("POST", forgebroker.BROKER_PATH), (method, path)
        proof = headers.get(api_children.PROOF_HEADER)
        stamp = headers.get(api_children.TIMESTAMP_HEADER)
        message = api_childkey.request_message(method, path, body or b"", stamp or "")
        if not (self.public_key and proof
                and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(
                403, "forge_credential_unproven", "the attempt proof did not verify",
                retryable=False)
        request = api_credbroker.ForgeCredentialRequest.model_validate(json.loads(body))
        self.requests.append(request.model_dump())
        if self.refuse is not None:
            status, code = self.refuse
            raise children_mod.ChildApiError(status, code, "refused by the fake broker",
                                             retryable=status >= 500)
        self.accessed.append(self.secret)
        return 200, {"secret": self.secret, "token": self.values[self.secret]}


def broker_worker(worker_factory, routes: FakeBrokerRoutes, *, register: bool = True,
                  client: FakeSecretClient | None = None, **kwargs: Any):
    kwargs.setdefault("child_nonce", NONCE)
    kwargs.setdefault("swarm_api_url", API)
    # FORGE_BROKER on: the path these tests are about. Off is the default
    # and every environment's setting today; the tests of the switch say so.
    kwargs.setdefault("forge_broker", True)
    client = client if client is not None else FakeSecretClient(
        {slot_secret(): user_token("s"), f"swarm-tenant-{TENANT}-git": tenant_token()})
    worker, config, _ = worker_factory(child_api=routes, secret_client=client, **kwargs)
    if register:
        worker.children.register(lambda note: None)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": slot(),
                    "submitted_by": PERSON.lower(), "forge_access": "write"}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL
    return worker, client


# --------------------------------------------------------------------------
# The restatements held to swarm-api's
# --------------------------------------------------------------------------


def test_the_path_and_headers_are_the_routes():
    from swarm_api.routes import credentials

    assert any(getattr(r, "path", None) == forgebroker.BROKER_PATH
               for r in credentials.router.routes)
    assert children_mod.PROOF_HEADER == api_children.PROOF_HEADER
    assert children_mod.TIMESTAMP_HEADER == api_children.TIMESTAMP_HEADER
    assert forgebroker.GRANT_CAUSES == {
        "grant_removed": lifecycle.FORGE_GRANT_REVOKED,
        "grant_read_only": lifecycle.FORGE_READ_GRANT,
    }


# --------------------------------------------------------------------------
# Which path reads which slot
# --------------------------------------------------------------------------


def test_a_user_slot_is_never_read_from_secret_manager(db, worker_factory):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, client = broker_worker(worker_factory, routes)

    assert worker._git_token() == user_token()

    assert client.accessed == [], "a person's slot was read from Secret Manager"
    assert routes.accessed == [slot_secret()]
    (request,) = routes.requests
    assert request == {
        "tenant_id": TENANT, "task_id": "task_1", "attempt_id": "att_1",
        "lease_id": "lease_1", "generation": 1, "access": "read",
    }
    # Without a broker at all, still never Secret Manager: a named refusal.
    tenant = secrets_mod.load_tenant(db, TENANT)
    with pytest.raises(forgebroker.ForgeCredentialRefused):
        secrets_mod.resolve_git_token(tenant=tenant, client=client, logger=worker.log,
                                      provider=slot())
    assert client.accessed == []


def test_the_tenant_and_repository_tokens_stay_direct_reads(db, worker_factory):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, client = broker_worker(worker_factory, routes)
    repo_slot = "git-r-" + "0123456789abcdef"
    for named in ("git", repo_slot):
        worker._task = {"repository_url": GITHUB_URL, "forge_credential": named}
        worker._git_token()
    assert client.accessed == [f"swarm-tenant-{TENANT}-git", f"swarm-tenant-{TENANT}-{repo_slot}"]
    assert routes.requests == [], "a team credential went through the person's broker"


def test_the_request_carries_a_valid_signature_over_the_request_message(db, worker_factory):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, _ = broker_worker(worker_factory, routes)

    worker._git_token()

    call = routes.calls[-1]
    stamp = call["headers"][api_children.TIMESTAMP_HEADER]
    message = api_childkey.request_message("POST", forgebroker.BROKER_PATH, call["body"], stamp)
    assert api_childkey.verify_proof(
        routes.public_key, call["headers"][api_children.PROOF_HEADER], message)
    # A proof over other bytes does not verify: the signature is over THIS body.
    other = api_childkey.request_message("POST", forgebroker.BROKER_PATH, call["body"] + b" ",
                                         stamp)
    assert not api_childkey.verify_proof(
        routes.public_key, call["headers"][api_children.PROOF_HEADER], other)
    # The key never left the heap: not in the body, the headers or the repr.
    assert "private" not in call["body"].decode()
    assert repr(worker.children).find("PrivateKey") == -1
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(worker.children._key)


def test_a_write_asks_the_broker_for_write(db, worker_factory):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, _ = broker_worker(worker_factory, routes)
    tenant = secrets_mod.load_tenant(db, TENANT)
    secrets_mod.resolve_git_token(tenant=tenant, client=None, logger=worker.log,
                                  provider=slot(), broker=worker.forge_broker, write=True)
    assert routes.requests[-1]["access"] == "write"


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["credential_not_submitters", "spec_unverified",
                                  "forge_credential_unproven", "grant_removed"])
def test_a_refused_attempt_fails_closed_without_the_tenant_token(db, worker_factory, code,
                                                                 log_stream):
    """Bob's attempt asking for Alice's slot: no token, and never the team's instead."""
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    routes.refuse = (403, code)
    worker, client = broker_worker(worker_factory, routes)
    tenant = secrets_mod.load_tenant(db, TENANT)

    with pytest.raises(forgebroker.ForgeCredentialRefused) as raised:
        secrets_mod.resolve_git_token(tenant=tenant, client=client, logger=worker.log,
                                      provider=slot(), broker=worker.forge_broker)
    assert raised.value.code == code
    assert raised.value.secret == slot_secret()
    assert isinstance(raised.value, secrets_mod.SecretError)

    # The clone's read: no token, and no tenant token in its place.
    assert worker._git_token() is None
    assert client.accessed == [], f"the tenant token was read instead: {client.accessed}"
    assert tenant_token() not in log_stream.getvalue()
    assert user_token() not in log_stream.getvalue()


@pytest.mark.parametrize("how", ["no_nonce", "tombstone", "registration_refused"])
def test_an_attempt_without_a_key_fails_closed_before_asking(db, worker_factory, how):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    kwargs: dict[str, Any] = {}
    if how == "no_nonce":
        kwargs["child_nonce"] = None
    if how == "tombstone":
        kwargs["memory"] = MemoryProtection(FAILED, "a test's unprotected worker")
    if how == "registration_refused":
        real = routes.call

        def call(method, path, **kw):
            if path.endswith("/child-key"):
                raise children_mod.ChildApiError(403, "child_submit_unauthenticated", "",
                                                 retryable=False)
            return real(method, path, **kw)

        routes.call = call  # type: ignore[method-assign]
    worker, client = broker_worker(worker_factory, routes, **kwargs)
    tenant = secrets_mod.load_tenant(db, TENANT)

    with pytest.raises(forgebroker.ForgeCredentialRefused) as raised:
        secrets_mod.resolve_git_token(tenant=tenant, client=client, logger=worker.log,
                                      provider=slot(), broker=worker.forge_broker)
    assert raised.value.code == forgebroker.NO_ATTEMPT_KEY
    assert routes.requests == []
    assert client.accessed == []
    # The merge's read names the failure, and never reads the tenant token.
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]
    with pytest.raises(secrets_mod.SecretError):
        worker._read_action_git_token()
    assert client.accessed == []


@pytest.mark.parametrize("child_tasks", [False, True], ids=["child-tasks-off", "dev-child-tasks-on"])
def test_a_deployment_without_the_broker_reads_the_slot_with_its_own_account(
        db, worker_factory, log_stream, child_tasks):
    """FORGE_BROKER off: the release must not stop Alice's push.

    Off is the default and every environment's setting until the owner turns
    it on -- dev included, where child tasks ARE on, so its workers hold a
    nonce and SWARM_API_URL. The address is not the switch: the worker reads
    the slot with its own account, exactly as before #1041, while
    `forge_slot_reader` grants it (docs/design/user-scoped-secrets.md §5),
    and asks the broker nothing.
    """
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    client = FakeSecretClient({slot_secret(): user_token("s"),
                               f"swarm-tenant-{TENANT}-git": tenant_token()})
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, _, _ = worker_factory(
        secret_client=client, child_api=routes if child_tasks else None,
        child_nonce=NONCE if child_tasks else None, swarm_api_url=API if child_tasks else None)
    if child_tasks:
        worker.children.register(lambda note: None)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": slot(),
                    "submitted_by": PERSON.lower(), "forge_access": "write"}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL
    assert worker.forge_broker.deployed is False

    assert worker._git_token() == user_token("s")
    assert client.accessed == [slot_secret()], "the person's slot, and only it, was read"
    assert routes.requests == [], "the broker was asked with FORGE_BROKER off"
    assert user_token("s") not in log_stream.getvalue()


def test_with_the_broker_on_and_no_address_the_slot_is_refused_not_read(db, worker_factory):
    """FORGE_BROKER on, but this attempt was handed no SWARM_API_URL: the
    switch means the broker is the only path, so no direct read happens."""
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    client = FakeSecretClient({slot_secret(): user_token("s"),
                               f"swarm-tenant-{TENANT}-git": tenant_token()})
    worker, _, _ = worker_factory(secret_client=client, child_nonce=None, swarm_api_url=None,
                                  forge_broker=True)
    assert worker.forge_broker.deployed is True
    tenant = secrets_mod.load_tenant(db, TENANT)
    with pytest.raises(forgebroker.ForgeCredentialRefused) as raised:
        secrets_mod.resolve_git_token(tenant=tenant, client=client, logger=worker.log,
                                      provider=slot(), broker=worker.forge_broker)
    assert raised.value.code == forgebroker.NO_BROKER
    assert client.accessed == []


def test_the_switch_reads_forge_broker_from_the_environment(monkeypatch):
    """FORGE_BROKER is what terraform renders and the scheduler passes on;
    unset is off."""
    from agent_worker.config import WorkerConfig
    from worker_seeds import PROJECT

    for name, value in {"TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
                        "TENANT_ID": TENANT, "RUNNER_PROFILE": "mock", "PROJECT_ID": PROJECT,
                        "GENERATION": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FORGE_BROKER", raising=False)
    assert WorkerConfig.from_env().forge_broker is False
    monkeypatch.setenv("FORGE_BROKER", "true")
    assert WorkerConfig.from_env().forge_broker is True


def test_an_additional_repositorys_user_slot_comes_from_the_broker(db, worker_factory):
    """#1434's extra clones read their entry's slot through the broker, like
    the primary: before, `_additional_git_token` passed no broker, so a
    `git-u-` entry always raised NO_BROKER and cloned unauthenticated."""
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, client = broker_worker(worker_factory, routes)
    entry = {"name": "web", "repository_url": GITHUB_URL, "forge_credential": slot()}

    assert worker._additional_git_token(entry) == user_token()
    assert client.accessed == [], "a person's slot was read from Secret Manager"
    assert routes.accessed == [slot_secret()]
    assert routes.requests[-1]["access"] == "read"

    # Refused, the entry clones with no token, and never the tenant's.
    routes.refuse = (403, "credential_not_submitters")
    assert worker._additional_git_token(entry) is None
    assert client.accessed == []


def test_without_the_broker_a_removed_read_fails_closed_without_the_tenant_token(
        db, worker_factory):
    """After the bootstrap apply removes forge_slot_reader, a deployment still
    without the broker gets PERMISSION_DENIED: a named failure, never the team token."""
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]

    class DeniedForSlots(FakeSecretClient):
        def access(self, secret_name: str, version: str = "latest") -> str:
            self.accessed.append(secret_name)
            if "-git-u-" in secret_name:
                raise PermissionError("403 PERMISSION_DENIED")
            return self.values.get(secret_name, "")

    client = DeniedForSlots({f"swarm-tenant-{TENANT}-git": tenant_token()})
    worker, _, _ = worker_factory(secret_client=client, child_nonce=None, swarm_api_url=None)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": slot(),
                    "submitted_by": PERSON.lower(), "forge_access": "write"}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL

    assert worker._git_token() is None
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]
    with pytest.raises(secrets_mod.SecretError):
        worker._read_action_git_token()
    assert client.accessed == [slot_secret(), slot_secret()], client.accessed


def test_a_superseded_attempt_is_fenced(db, worker_factory):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    routes.refuse = (403, "attempt_superseded")
    worker, _ = broker_worker(worker_factory, routes)
    with pytest.raises(FencedError):
        worker._git_token()


@pytest.mark.parametrize("code,cause", [("grant_removed", "forge_grant_revoked"),
                                        ("grant_read_only", "forge_read_grant")])
def test_a_refused_write_is_forge_write_refused(db, worker_factory, code, cause):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    routes.refuse = (403, code)
    worker, client = broker_worker(worker_factory, routes)
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]
    with pytest.raises(secrets_mod.ForgeWriteRefused) as raised:
        worker._read_action_git_token()
    assert raised.value.cause == cause
    assert client.accessed == []
    # A push's read asks for `read` (its write gate is `_forge_write_refusal`,
    # asked first); refused, it gets no token rather than the tenant's.
    assert worker._git_token() is None
    assert [r["access"] for r in routes.requests] == ["write", "read"]


# --------------------------------------------------------------------------
# The value: registered, then nowhere
# --------------------------------------------------------------------------


def test_the_token_is_in_no_env_and_no_log(db, worker_factory, monkeypatch, origin,  # noqa: F811
                                           local_urls, forge, log_stream):  # noqa: F811
    """A whole attempt on a person's slot: cloned with the brokered token, which
    is redacted, and in no environment, log line, event or task document."""
    _seed_user_task(db)
    seed_grant(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, client = broker_worker(worker_factory, routes, register=False)
    registered: list[str] = []
    real_register = worker.log.register_secret

    def register(value: str) -> None:
        registered.append(value)
        real_register(value)

    monkeypatch.setattr(worker.log, "register_secret", register)
    seen_env: list[dict[str, str]] = []
    real_run = worker._run_child_supervised

    def run_child(child_env):
        seen_env.append(dict(child_env))
        return real_run(child_env)

    monkeypatch.setattr(worker, "_run_child_supervised", run_child)
    _clone_from(worker, monkeypatch, origin)

    assert worker.run() == 0, db.doc("tasks/task_1").get("last_error")

    assert routes.accessed, "the clone never asked the broker"
    assert client.accessed == [], "a person's slot was read from Secret Manager"
    assert user_token() in registered, "the token was not registered for redaction"
    assert seen_env, "the agent never started"
    for env in seen_env:
        assert not [k for k, v in env.items() if user_token() in v], "the token is in the child env"
    assert user_token() not in json.dumps(dict(os.environ)), "the token is in the Job env"
    assert user_token() not in log_stream.getvalue()
    assert user_token() not in json.dumps(db.documents, default=str)


def test_the_token_is_registered_before_it_is_returned(db, worker_factory, monkeypatch):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): json.dumps({"GIT_TOKEN": user_token()})})
    worker, _ = broker_worker(worker_factory, routes)
    order: list[str] = []
    real_register = worker.log.register_secret
    monkeypatch.setattr(worker.log, "register_secret",
                        lambda value: (order.append(value), real_register(value)))
    assert worker._git_token() == user_token()
    assert user_token() in order


# --------------------------------------------------------------------------
# The 401 re-read and the child flag
# --------------------------------------------------------------------------


def test_the_401_reread_goes_through_the_broker_again(db, worker_factory, monkeypatch, tmp_path):
    from agent_worker import issue as issue_mod

    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token("a")})
    worker, client = broker_worker(worker_factory, routes)
    seen: list[str | None] = []

    def stage_issue(**kwargs: Any):
        seen.append(kwargs["token"])
        if len(seen) == 1:
            routes.values[slot_secret()] = user_token("b")
            raise issue_mod.IssueUnavailable(
                "could not fetch issue #1 of saga/widgets: the forge refused the credential "
                "(401: Bad credentials)")
        return tmp_path / "issue.md"

    monkeypatch.setattr(lifecycle.issue_mod, "stage_issue", stage_issue)
    worker._stage_issue(1, tmp_path, None)
    assert seen == [user_token("a"), user_token("b")]
    assert routes.accessed == [slot_secret(), slot_secret()]
    assert client.accessed == []


def test_a_child_registers_its_key_and_is_offered_no_child_path(db, worker_factory, tmp_path):
    seed_attempt(db)
    routes = FakeBrokerRoutes({slot_secret(): user_token()})
    worker, _ = broker_worker(worker_factory, routes, child_path=False)
    assert routes.public_key, "the child's key was not registered"
    assert not worker.children.offered
    assert worker.children.prepare(tmp_path) == {}
    assert worker._git_token() == user_token(), "a child could not prove itself to the broker"
