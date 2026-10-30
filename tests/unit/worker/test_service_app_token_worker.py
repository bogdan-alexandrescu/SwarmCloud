"""A service submission's run asks the broker first for its token (DAYONE-SERVICE-APP-TOKEN).

swarm-api mints a platform service account's run a one-repository App
installation token at `/v1/attempts/forge-credential`
(`swarm_api.credbroker.decide_run_token`) when the repository's owner has
the App installed for the tenant. The worker's half, pinned here:

  * a run whose SIGNED `submitted_by` is a service account -- the field the
    broker decides from; never the unsigned `forge_credential_source` label,
    which `test_specsign_covers` keeps the worker from reading -- asks the
    broker for its slot first, whatever FORGE_BROKER says, and reads no
    Secret Manager slot when it mints;
  * the record's kind is `app_installation` (VERIFY-ENGINE's check);
  * `app_installation_unavailable`, or a worker with no broker address or no
    attempt key, falls back to the slot: today's service rule;
  * a superseded attempt is fenced and a refused write is `ForgeWriteRefused`,
    never the slot in its place;
  * a person's task, or a service submitter without a verified signature,
    never asks;
  * the merge's write read asks for `write` and needs no `git` slot;
  * the minted value is in no log line.

Every fake token is built at runtime, never written as one literal.

MUTATIONS, each caught below: drop `app_installation=` at `_git_token` -- the
minted test sees the slot read. Drop the `_forge_signed` check -- the
unsigned test sees a broker request. Re-raise every refusal -- the fallback
test raises. Fall back on every refusal -- the fenced and write tests read
the slot.
"""

from __future__ import annotations

import pytest

from agent_worker import forgebroker
from agent_worker import secrets as secrets_mod
from agent_worker.errors import FencedError

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt
from test_forge_broker_client import FakeBrokerRoutes, broker_worker, tenant_token
from test_forge_credential import GITHUB_URL, PERSON

SERVICE = "swarm-api@example-project.iam.gserviceaccount.com"
GIT_SECRET = f"swarm-tenant-{TENANT}-git"


def minted_token() -> str:
    """An installation token's shape, built here so no literal is one."""
    return "ghs_" + "m" * 36


def service_worker(db, worker_factory, *, service: bool = True, signed: bool = True,
                   credentials: tuple[str, ...] = ("git",), **kwargs):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = list(credentials)
    routes = FakeBrokerRoutes({GIT_SECRET: minted_token()}, secret=GIT_SECRET)
    client = FakeSecretClient({GIT_SECRET: tenant_token()})
    kwargs.setdefault("forge_broker", False)  # the service path does not wait for it
    worker, client = broker_worker(worker_factory, routes, client=client, **kwargs)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": "git",
                    "submitted_by": SERVICE if service else PERSON.lower(),
                    "forge_access": "write",
                    "metadata": {"forge_credential_source": "app_installation"}}
    worker._forge_signed = signed
    return worker, client, routes


@pytest.mark.parametrize("switch", [False, True])
def test_a_service_run_gets_the_minted_token_and_reads_no_slot(db, worker_factory, switch):
    worker, client, routes = service_worker(db, worker_factory, forge_broker=switch)

    assert worker._git_token() == minted_token()

    assert client.accessed == [], "the slot was read for a run the broker minted for"
    assert routes.accessed == [GIT_SECRET]
    assert routes.requests[-1]["access"] == "read"
    assert worker._forge_credentials_read[-1] == ("app_installation", GIT_SECRET)


@pytest.mark.parametrize("code", [forgebroker.APP_INSTALLATION_UNAVAILABLE])
def test_no_installation_token_falls_back_to_the_slot(db, worker_factory, code, log_stream):
    worker, client, routes = service_worker(db, worker_factory)
    routes.refuse = (403, code)

    assert worker._git_token() == tenant_token()

    assert client.accessed == [GIT_SECRET]
    assert worker._forge_credentials_read[-1] == ("pat", GIT_SECRET)
    assert "reading its slot" in log_stream.getvalue()


def test_a_worker_without_a_broker_address_keeps_the_slot(db, worker_factory):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    client = FakeSecretClient({GIT_SECRET: tenant_token()})
    worker, _, _ = worker_factory(secret_client=client, child_nonce=None, swarm_api_url=None)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": "git",
                    "submitted_by": SERVICE, "forge_access": "write"}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL

    assert worker._service_run() is True
    assert worker._git_token() == tenant_token()
    assert client.accessed == [GIT_SECRET]


def test_a_worker_without_an_attempt_key_keeps_the_slot(db, worker_factory):
    worker, client, routes = service_worker(db, worker_factory, child_nonce=None)

    assert worker._git_token() == tenant_token()
    assert routes.requests == []
    assert client.accessed == [GIT_SECRET]


def test_a_superseded_attempt_is_fenced_and_reads_no_slot(db, worker_factory):
    worker, client, routes = service_worker(db, worker_factory)
    routes.refuse = (403, "attempt_superseded")

    with pytest.raises(FencedError):
        worker._git_token()
    assert client.accessed == []


def test_a_write_refused_by_the_broker_is_forge_write_refused(db, worker_factory):
    worker, client, routes = service_worker(db, worker_factory)
    routes.refuse = (403, "grant_read_only")
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]

    with pytest.raises(secrets_mod.ForgeWriteRefused):
        worker._read_action_git_token()
    assert client.accessed == []
    assert routes.requests[-1]["access"] == "write"


def test_the_merge_asks_for_write_and_needs_no_slot(db, worker_factory):
    worker, client, routes = service_worker(db, worker_factory, credentials=())
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]

    assert worker._read_action_git_token() == minted_token()
    assert routes.requests[-1]["access"] == "write"
    assert client.accessed == []


@pytest.mark.parametrize("service,signed", [(False, True), (True, False)])
def test_a_persons_or_unsigned_task_never_asks(db, worker_factory, service, signed):
    """The label is set on both: it is not what the worker reads."""
    worker, client, routes = service_worker(db, worker_factory, service=service,
                                            signed=signed)

    assert worker._service_run() is False
    assert worker._git_token() == tenant_token()
    assert routes.requests == []
    assert client.accessed == [GIT_SECRET]


def test_the_minted_token_is_in_no_log(db, worker_factory, log_stream):
    worker, _, _ = service_worker(db, worker_factory)
    token = worker._git_token()
    worker.log.info("after the read", detail=f"value {token}")
    assert minted_token() not in log_stream.getvalue()
