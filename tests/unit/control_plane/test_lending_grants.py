"""Lending an account grants the borrower's worker its secret, and unlending revokes it.

MEASURED 2026-10-06. The owner lent `eng:team` to tenant `smoke`
(`lend_to: ['smoke']`), and every smoke claude-code attempt then read
`PermissionDenied: 403 Permission 'secretmanager.versions.access' denied` on
`swarm-account-eng--team` -- the broker logged "a worker could not read the
account it was assigned" five times that day. Nothing granted a BORROWER's
worker the secret: provisioning bound only the broker and the OWNER's worker,
and the lending route wrote Firestore and nothing else. The operator granted
smoke's worker by hand as a stopgap.

What these pin:

  * lending grants `secretAccessor` on the account's ACCESS-token secret to
    exactly the borrower's `swarm-agent-worker-<tenant>`; unlending revokes
    exactly that member, and the owner's worker and the broker stay;
  * the `-refresh` secret is never widened -- a borrower that could read it
    could mint successors forever;
  * no other secret's policy is read or written;
  * re-lending to the same tenants writes no policy at all;
  * a grant that fails leaves `lend_to` unchanged, so the pool never assigns a
    borrower an account it cannot read.

No cloud, no emulator: the real `SecretManagerStore` runs against a fake client
that holds REAL `google.iam.v1` policy messages and enforces etags.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core import exceptions as gexc
from google.iam.v1 import policy_pb2

from quota_broker.secretstore import ACCESSOR_ROLE, SecretManagerStore

from .conftest import PROJECT

OWNER = "eng"
BORROWER = "smoke"
OTHER = "research"
SA_DOMAIN = "p.iam.gserviceaccount.com"
BROKER_SA = f"serviceAccount:swarm-quota-broker@{SA_DOMAIN}"
HUMAN = "group:swarm-admins@example.com"
BASE = f"swarm-account-{OWNER}--team"
REFRESH = f"{BASE}-refresh"
UNRELATED = f"swarm-account-{OTHER}--own"


def worker(tenant: str) -> str:
    return f"serviceAccount:swarm-agent-worker-{tenant}@{SA_DOMAIN}"


MANAGED = re.compile(rf"serviceAccount:swarm-agent-worker-[a-z0-9-]+@{re.escape(SA_DOMAIN)}")


def manages(member: str) -> bool:
    return MANAGED.fullmatch(member) is not None


def resource(name: str) -> str:
    return f"projects/{PROJECT}/secrets/{name}"


class FakeSecretManager:
    """Enough of SecretManagerServiceClient for provisioning and IAM."""

    def __init__(self) -> None:
        self.policies: dict[str, policy_pb2.Policy] = {}
        self.versions: dict[str, list[bytes]] = {}
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.deny_set_on: str | None = None

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.policies:
            raise gexc.AlreadyExists(name)
        self.policies[name] = policy_pb2.Policy(etag=b"v1")
        return object()

    def add_secret_version(self, request):
        self.versions.setdefault(request["parent"], []).append(request["payload"].data)
        n = len(self.versions[request["parent"]])
        return type("Version", (), {"name": f"{request['parent']}/versions/{n}"})()

    def list_secret_versions(self, request):
        return []

    def get_iam_policy(self, request):
        self.reads.append(request["resource"])
        policy = policy_pb2.Policy()
        policy.CopyFrom(self.policies.get(request["resource"], policy_pb2.Policy(etag=b"v1")))
        return policy

    def set_iam_policy(self, request):
        res, policy = request["resource"], request["policy"]
        if self.deny_set_on and self.deny_set_on == res:
            raise gexc.PermissionDenied("secretmanager.secrets.setIamPolicy denied")
        stored = self.policies.get(res, policy_pb2.Policy(etag=b"v1"))
        if bytes(policy.etag) != bytes(stored.etag):
            raise gexc.Aborted("the policy etag is no longer current")
        written = policy_pb2.Policy()
        written.CopyFrom(policy)
        written.etag = bytes(stored.etag) + b"+"
        self.policies[res] = written
        self.writes.append(res)
        return written

    def accessors(self, name: str) -> list[str]:
        return sorted(
            m
            for b in self.policies.get(resource(name), policy_pb2.Policy()).bindings
            if b.role == ACCESSOR_ROLE and not b.condition.expression
            for m in b.members
        )


def _seed(client: FakeSecretManager, name: str, *members: str) -> None:
    policy = policy_pb2.Policy(etag=b"seed")
    binding = policy.bindings.add()
    binding.role = ACCESSOR_ROLE
    binding.members.extend(members)
    adder = policy.bindings.add()
    adder.role = "roles/secretmanager.secretVersionAdder"
    adder.members.append(HUMAN)
    client.policies[resource(name)] = policy


# -- the store: one secret, exactly the members it manages ------------------


def test_lending_grants_exactly_the_borrower_and_leaves_everyone_else():
    client = FakeSecretManager()
    _seed(client, BASE, BROKER_SA, worker(OWNER), HUMAN)
    store = SecretManagerStore(PROJECT, client=client)

    added, removed = store.set_worker_readers(
        BASE, readers=[worker(OWNER), worker(BORROWER)], manages=manages
    )

    assert added == [worker(BORROWER)] and removed == []
    assert client.accessors(BASE) == sorted(
        [BROKER_SA, worker(OWNER), worker(BORROWER), HUMAN]
    )
    adder = [b for b in client.policies[resource(BASE)].bindings if b.role != ACCESSOR_ROLE]
    assert [list(b.members) for b in adder] == [[HUMAN]], "other roles untouched"


def test_unlending_revokes_exactly_the_tenant_no_longer_listed():
    client = FakeSecretManager()
    _seed(client, BASE, BROKER_SA, worker(OWNER), worker(BORROWER), worker(OTHER), HUMAN)
    store = SecretManagerStore(PROJECT, client=client)

    added, removed = store.set_worker_readers(
        BASE, readers=[worker(OWNER), worker(OTHER)], manages=manages
    )

    assert added == [] and removed == [worker(BORROWER)]
    assert client.accessors(BASE) == sorted([BROKER_SA, worker(OWNER), worker(OTHER), HUMAN])


def test_a_grant_only_pass_never_revokes():
    """Grants land BEFORE the Firestore write and revokes after it, so the
    first pass must leave a borrower that is about to be dropped alone."""
    client = FakeSecretManager()
    _seed(client, BASE, worker(OWNER), worker(OTHER))
    store = SecretManagerStore(PROJECT, client=client)

    added, removed = store.set_worker_readers(
        BASE, readers=[worker(OWNER), worker(BORROWER)], manages=manages, revoke=False
    )

    assert added == [worker(BORROWER)] and removed == []
    assert worker(OTHER) in client.accessors(BASE)


def test_re_lending_to_the_same_tenants_writes_no_policy():
    client = FakeSecretManager()
    _seed(client, BASE, BROKER_SA, worker(OWNER))
    store = SecretManagerStore(PROJECT, client=client)
    readers = [worker(OWNER), worker(BORROWER)]

    store.set_worker_readers(BASE, readers=readers, manages=manages)
    writes = list(client.writes)
    again = store.set_worker_readers(BASE, readers=readers, manages=manages)

    assert again == ([], [])
    assert client.writes == writes, "an unchanged policy is not written again"


def test_no_other_secret_is_read_or_written():
    client = FakeSecretManager()
    _seed(client, BASE, worker(OWNER))
    _seed(client, UNRELATED, worker(OTHER), worker(BORROWER))
    before = policy_pb2.Policy()
    before.CopyFrom(client.policies[resource(UNRELATED)])
    store = SecretManagerStore(PROJECT, client=client)

    store.set_worker_readers(BASE, readers=[worker(OWNER)], manages=manages)

    assert set(client.reads) == {resource(BASE)}
    assert set(client.writes) <= {resource(BASE)}
    assert client.policies[resource(UNRELATED)] == before


def test_a_conditional_binding_is_never_rewritten():
    client = FakeSecretManager()
    _seed(client, BASE, worker(OWNER))
    conditional = client.policies[resource(BASE)].bindings.add()
    conditional.role = ACCESSOR_ROLE
    conditional.members.append(worker(BORROWER))
    conditional.condition.expression = "request.time < timestamp('2030-01-01T00:00:00Z')"
    store = SecretManagerStore(PROJECT, client=client)

    store.set_worker_readers(BASE, readers=[worker(OWNER)], manages=manages)

    kept = [b for b in client.policies[resource(BASE)].bindings if b.condition.expression]
    assert [list(b.members) for b in kept] == [[worker(BORROWER)]]


@pytest.mark.parametrize(
    "name",
    [
        REFRESH,                                  # the pair: broker-only, always
        f"swarm-tenant-{OWNER}-anthropic",        # a tenant's own key
        "unrelated-team-config",                   # not this platform's
        f"{BASE}\n",                              # newline smuggling
        f"{BASE}/../other",                       # path traversal
    ],
)
def test_only_an_account_access_token_secret_is_ever_widened(name):
    client = FakeSecretManager()
    store = SecretManagerStore(PROJECT, client=client)

    with pytest.raises(ValueError):
        store.set_worker_readers(name, readers=[worker(BORROWER)], manages=manages)
    assert client.reads == [] and client.writes == []


def test_a_reader_the_call_does_not_manage_is_refused():
    """`readers` and `manages` must agree, or a sync could grant a member it
    would never be able to revoke."""
    client = FakeSecretManager()
    store = SecretManagerStore(PROJECT, client=client)

    with pytest.raises(ValueError):
        store.set_worker_readers(BASE, readers=[HUMAN], manages=manages)
    assert client.writes == []


def test_a_concurrent_policy_write_is_retried_from_a_fresh_read():
    client = FakeSecretManager()
    _seed(client, BASE, worker(OWNER))
    real_set = client.set_iam_policy
    raced = []

    def set_once_stale(request):
        if not raced:
            raced.append(1)
            client.policies[resource(BASE)].etag = b"moved"
        return real_set(request)

    client.set_iam_policy = set_once_stale
    store = SecretManagerStore(PROJECT, client=client)

    store.set_worker_readers(BASE, readers=[worker(OWNER), worker(BORROWER)], manages=manages)

    assert worker(BORROWER) in client.accessors(BASE)


# -- the routes: lending and provisioning drive the sync ---------------------


def _credential() -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    return json.dumps(
        {
            "accessToken": "at-" + "a" * 16,
            "refreshToken": "rt-" + "b" * 8,
            "expiresAt": int(expires.timestamp() * 1000),
        }
    )


@pytest.fixture()
def gsm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture()
def client(broker, gsm, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import WorkerIdentity, create_app

    class PlatformIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return None, True

    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("BROKER_SERVICE_ACCOUNT", BROKER_SA.split(":", 1)[1])
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE", f"swarm-agent-worker-{{tenant}}@{SA_DOMAIN}"
    )
    app = create_app(
        broker,
        identity=PlatformIdentity(project_id=PROJECT),
        account_store=AccountStore(broker.db),
    )
    app.state.secret_store = SecretManagerStore(PROJECT, client=gsm)
    return TestClient(app, raise_server_exceptions=False)


def _register(client, label="team", lend_to=(), owner=OWNER):
    r = client.post(
        "/v1/accounts",
        json={
            "owner_tenant": owner,
            "label": label,
            "lend_to": list(lend_to),
            "credential": _credential(),
        },
    )
    assert r.status_code == 201, r.text
    return r


def _lend(client, lend_to, account=f"{OWNER}:team"):
    return client.put(f"/v1/accounts/{account}/lending", json={"lend_to": list(lend_to)})


def test_registering_with_a_borrower_grants_it_the_access_token_secret(client, gsm):
    _register(client, lend_to=[BORROWER])

    assert gsm.accessors(BASE) == sorted([BROKER_SA, worker(OWNER), worker(BORROWER)])
    assert gsm.accessors(REFRESH) == [BROKER_SA], "a borrower never reads the pair"


def test_lending_grants_the_borrower_and_unlending_revokes_it(client, gsm):
    _register(client)
    assert worker(BORROWER) not in gsm.accessors(BASE)

    lent = _lend(client, [BORROWER])
    assert lent.status_code == 200, lent.text
    assert lent.json()["account"]["lend_to"] == [BORROWER]
    assert gsm.accessors(BASE) == sorted([BROKER_SA, worker(OWNER), worker(BORROWER)])

    unlent = _lend(client, [])
    assert unlent.status_code == 200, unlent.text
    assert gsm.accessors(BASE) == sorted([BROKER_SA, worker(OWNER)])
    assert gsm.accessors(REFRESH) == [BROKER_SA]


def test_re_lending_is_idempotent(client, gsm):
    _register(client)
    assert _lend(client, [BORROWER]).status_code == 200
    writes = list(gsm.writes)

    assert _lend(client, [BORROWER]).status_code == 200
    assert gsm.writes == writes
    assert gsm.accessors(BASE).count(worker(BORROWER)) == 1


def test_lending_one_account_leaves_another_accounts_secret_alone(client, gsm):
    _register(client, owner=OTHER, label="own", lend_to=[BORROWER])
    _register(client)
    before = policy_pb2.Policy()
    before.CopyFrom(gsm.policies[resource(UNRELATED)])

    assert _lend(client, [OTHER]).status_code == 200
    assert _lend(client, []).status_code == 200

    assert gsm.policies[resource(UNRELATED)] == before
    assert worker(BORROWER) in gsm.accessors(UNRELATED)


def test_re_registering_with_a_shorter_list_revokes_the_dropped_borrower(client, gsm):
    """Re-registration is the other supported way to change who an account is
    lent to (`AccountStore.register`), so provisioning revokes too."""
    _register(client, lend_to=[BORROWER, OTHER])
    _register(client, lend_to=[OTHER])

    assert gsm.accessors(BASE) == sorted([BROKER_SA, worker(OWNER), worker(OTHER)])


def test_a_grant_that_fails_leaves_lend_to_unchanged(client, gsm):
    _register(client)
    gsm.deny_set_on = resource(BASE)

    r = _lend(client, [BORROWER])

    assert r.status_code == 422
    listed = client.get("/v1/accounts").json()["accounts"]
    assert [a["lend_to"] for a in listed if a["account_id"] == f"{OWNER}:team"] == [[]]


def test_a_borrower_that_is_not_a_tenant_id_is_refused(client, gsm):
    _register(client)
    writes = list(gsm.writes)

    r = _lend(client, ["Smoke@elsewhere.example"])

    assert r.status_code == 422
    assert gsm.writes == writes
