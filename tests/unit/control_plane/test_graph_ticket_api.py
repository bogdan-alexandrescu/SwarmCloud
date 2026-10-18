"""swarm-api mints the attempt graph ticket (docs/design/graph-service.md
§3.1-§3.2; lane GS-TICKET, part of #1384).

`POST /v1/attempts/{attempt_id}/graph-ticket`, called by the worker with the
attempt key's signature under this route's OWN purpose, fenced like the
egress and TypeSafe tickets: the task is RUNNING under this attempt's live
lease at this generation and no cancel was requested (invariant 5). The
repository comes from the TASK, never the body; the manifest digest comes from
the `index_versions` document swarm-api itself wrote, never the body or GCS;
and the stored code-intelligence setting decides (invariant 10): `off` is 403.

Drives the real route over the in-memory Firestore with the helpers of
test_live_api.py. Every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any

import pytest

from swarm_api.graph_ticket import (
    MAX_TICKET_SECONDS,
    MIN_TICKET_SECONDS,
    TICKET_DOMAIN,
    TICKET_FIELDS,
    TICKET_PURPOSE,
    TicketRefused,
    graph_ticket_message,
    mint_graph_ticket,
    ticket_seconds,
    verify_graph_ticket,
)
from swarm_api.repositories import repo_id_for
from swarm_api.settings import ApiSettings

from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    TASK,
    WORKER_TOKEN,
    Attempt,
    _b64url,
    clock,
    groups,
    ldb,
    make_client,
    people,
    relay,
    seed_running,
)

#: Built at runtime, never one literal (CLAUDE.md).
GRAPH_KEY = "-".join(("graph", "ticket", "key")) + "-" + secrets.token_hex(16)
PATH = f"/v1/attempts/{ATTEMPT}/graph-ticket"
URL = "https://github.com/Saga/Widgets"
REPO_ID = repo_id_for("eng", "saga", "widgets")
SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + hashlib.sha256(b"the promoted manifest").hexdigest()


def sign(attempt: Attempt, body: bytes, *, path: str = PATH, message=None) -> dict[str, str]:
    stamp = str(int(time.time()))
    make = message or graph_ticket_message
    sig = attempt.private.sign(make("POST", path, body, stamp))
    return {"X-Swarm-Attempt-Proof": _b64url(sig), "X-Swarm-Attempt-Timestamp": stamp}


def ask(client, attempt: Attempt, *, signed: bool = True, message=None, **over: Any):
    over.setdefault("index_sha", SHA)
    body = attempt.body(**over)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    if signed:
        headers.update(sign(attempt, body, message=message))
    return client.post(PATH, content=body, headers=headers)


def claims(ticket: str) -> dict[str, Any]:
    payload = ticket.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def register(db, *, tenant_id: str = "eng", repo_id: str = REPO_ID, **extra: Any) -> None:
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga", "repo": "widgets",
        "repository_url": URL, **extra,
    }


def version(db, sha: str = SHA, *, repo_id: str = REPO_ID, digest: Any = DIGEST) -> None:
    db.docs[f"repositories/{repo_id}/index_versions/{sha}"] = {
        "commit_sha": sha, "repo_id": repo_id, "graph_digest": digest,
    }


def set_context(db, value: str, tenant_id: str = "eng") -> None:
    db.docs[f"workspace_settings/{tenant_id}"] = {
        "tenant_id": tenant_id, "code_intelligence": {"context": value},
    }


@pytest.fixture
def make(ldb, people, groups, objects, clock, relay):
    def _make(**settings: Any):
        return make_client(ldb, people, groups, objects, clock, relay, **settings)
    return _make


@pytest.fixture
def ready(make, ldb):
    """A RUNNING attempt in `eng` on a registered repository whose index at
    SHA was promoted with a graph."""
    client = make(graph_ticket_key=GRAPH_KEY)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL)
    register(ldb)
    version(ldb)
    return client, attempt


def refused(response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert "ticket" not in body
    return body


# -- the wire ----------------------------------------------------------------


def mint(key: str = GRAPH_KEY, **over: Any) -> str:
    fields: dict[str, Any] = dict(
        tenant_id="eng", repo_id=REPO_ID, task_id=TASK, attempt_id=ATTEMPT, generation=1,
        index_sha=SHA, manifest_digest=DIGEST, exp=int(time.time()) + 600)
    fields.update(over)
    return mint_graph_ticket(key, **fields)


def test_the_constants_are_the_designs():
    assert TICKET_PURPOSE == "swarm-graph-ticket-request/v1"
    assert TICKET_DOMAIN == b"swarm-graph-ticket/v1\0"
    assert TICKET_FIELDS == frozenset({"tenant_id", "repo_id", "task_id", "attempt_id",
                                       "generation", "index_sha", "manifest_digest", "exp",
                                       "nonce"})
    assert (MIN_TICKET_SECONDS, MAX_TICKET_SECONDS) == (60, 900)


def test_the_ticket_carries_exactly_the_field_set_and_verifies_with_the_previous_key():
    old = "-".join(("old", "graph")) + secrets.token_hex(16)
    ticket = mint(old)
    got = verify_graph_ticket([GRAPH_KEY, old], ticket, now=time.time())
    assert set(got) == TICKET_FIELDS
    assert got["manifest_digest"] == DIGEST and got["repo_id"] == REPO_ID
    with pytest.raises(TicketRefused):
        verify_graph_ticket([GRAPH_KEY], ticket, now=time.time())


def test_the_encoding_is_unpadded_b64url_of_sorted_compact_json_and_a_domain_mac():
    import hmac

    ticket = mint()
    payload, mac = ticket.split(".")
    assert "=" not in ticket
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    want = hmac.new(GRAPH_KEY.encode(), TICKET_DOMAIN + payload.encode(), hashlib.sha256)
    assert mac == base64.urlsafe_b64encode(want.digest()).decode().rstrip("=")


def test_an_expired_or_overlong_or_forged_ticket_is_refused():
    now = time.time()
    with pytest.raises(TicketRefused):
        verify_graph_ticket([GRAPH_KEY], mint(exp=int(now) - 1), now=now)
    with pytest.raises(TicketRefused):
        verify_graph_ticket([GRAPH_KEY], mint(exp=int(now) + MAX_TICKET_SECONDS + 5), now=now)
    ticket = mint()
    payload, mac = ticket.split(".")
    forged = dict(claims(ticket), tenant_id="research")
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    swapped = base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + mac
    for bad in (swapped, payload + "." + _b64url(b"\0" * 32), payload, "", None, 7):
        with pytest.raises(TicketRefused):
            verify_graph_ticket([GRAPH_KEY], bad, now=now)


def test_a_ticket_with_a_field_more_or_less_is_refused_even_with_a_good_mac():
    from swarm_api import graph_ticket

    for body in ({**claims(mint()), "extra": 1},
                 {k: v for k, v in claims(mint()).items() if k != "manifest_digest"}):
        payload = graph_ticket._b64(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        ticket = f"{payload}.{graph_ticket._b64(graph_ticket._mac(GRAPH_KEY, payload))}"
        with pytest.raises(TicketRefused):
            verify_graph_ticket([GRAPH_KEY], ticket, now=time.time())


def test_another_tickets_domain_never_makes_a_graph_ticket():
    from swarm_api.typesafe_ticket import TICKET_DOMAIN as TYPESAFE_DOMAIN
    from swarm_api.typesafe_ticket import mint_typesafe_ticket

    assert TYPESAFE_DOMAIN != TICKET_DOMAIN
    other = mint_typesafe_ticket(GRAPH_KEY, tenant_id="eng", task_id=TASK, attempt_id=ATTEMPT,
                                 generation=1, scope="tenant", secret_id="s",
                                 secret_version="1", exp=int(time.time()) + 300)
    with pytest.raises(TicketRefused):
        verify_graph_ticket([GRAPH_KEY], other, now=time.time())


# -- the setting and the clamp ----------------------------------------------


def test_the_settings_default_to_no_key_and_ten_minutes_and_hide_the_key(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("ENVIRONMENT", "test")
    fields = ApiSettings.__dataclass_fields__
    assert fields["graph_ticket_key"].default == ""
    assert fields["graph_ticket_key"].repr is False
    assert fields["graph_ticket_seconds"].default == 600
    monkeypatch.setenv("GRAPH_TICKET_SECONDS", "120")
    monkeypatch.setenv("SWARM_GRAPH_TICKET_KEY", " " + GRAPH_KEY + " ")
    settings = ApiSettings.from_env()
    assert settings.graph_ticket_seconds == 120
    assert settings.graph_ticket_key == GRAPH_KEY
    assert GRAPH_KEY not in repr(settings)


def test_the_lifetime_is_clamped_to_60_to_900():
    assert ticket_seconds(5) == 60
    assert ticket_seconds(600) == 600
    assert ticket_seconds(5000) == 900


@pytest.mark.parametrize(("configured", "served"), [(5, 60), (300, 300), (5000, 900)])
def test_the_route_serves_the_clamped_life(make, ldb, configured, served):
    client = make(graph_ticket_key=GRAPH_KEY, graph_ticket_seconds=configured)
    attempt = Attempt()
    seed_running(client, ldb, attempt, repository_url=URL)
    register(ldb)
    version(ldb)
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == served
    exp = claims(body["ticket"])["exp"]
    assert abs(exp - (time.time() + served)) <= 5


def test_no_key_is_a_503_and_never_a_ticket(make, ldb):
    client = make()
    attempt = Attempt()
    seed_running(client, ldb, attempt, repository_url=URL)
    register(ldb)
    version(ldb)
    body = refused(ask(client, attempt), 503)
    assert body["code"] == "graph_ticket_unavailable"


# -- minting ------------------------------------------------------------------


def test_the_ticket_pins_tenant_repository_attempt_commit_and_digest(ready):
    client, attempt = ready
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ticket", "expires_in", "index_sha", "manifest_digest"}
    assert body["index_sha"] == SHA and body["manifest_digest"] == DIGEST
    got = verify_graph_ticket([GRAPH_KEY], body["ticket"], now=time.time())
    assert {k: got[k] for k in TICKET_FIELDS - {"exp", "nonce"}} == {
        "tenant_id": "eng", "repo_id": REPO_ID, "task_id": TASK, "attempt_id": ATTEMPT,
        "generation": 2, "index_sha": SHA, "manifest_digest": DIGEST,
    }


def test_an_unsigned_or_wrongly_purposed_request_is_refused(ready):
    from swarm_api.typesafe_ticket import typesafe_ticket_message

    client, attempt = ready
    refused(ask(client, attempt, signed=False), 403)
    refused(ask(client, attempt, message=typesafe_ticket_message), 403)


# -- the fence (invariant 5) -------------------------------------------------


def test_a_stale_generation_is_a_409_and_nothing_is_minted(ready, ldb):
    """The attempt's key was registered at generation 2 and still proves the
    request; the task has since moved to generation 3. The proof verifies and
    the fence refuses, so this is the 409 a superseded worker sees."""
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["current_generation"] = 3
    ldb.docs[f"leases/{attempt.tuple.lease_id}"]["generation"] = 3
    body = refused(ask(client, attempt), 409)
    assert body["code"] == "conflict" and body["detail"]["reason"] == "fenced"
    # A body that claims the new generation is not this attempt's key's.
    refused(ask(client, attempt, generation=3), 403)


def test_a_cancel_or_a_task_not_running_is_a_409(ready, ldb):
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    refused(ask(client, attempt), 409)
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = False
    ldb.docs[f"tasks/{TASK}"]["state"] = "PARKED"
    refused(ask(client, attempt), 409)
    ldb.docs[f"tasks/{TASK}"]["state"] = "RUNNING"
    assert ask(client, attempt).status_code == 200


# -- the repository comes from the task ---------------------------------------


def test_the_body_cannot_name_a_repository_or_a_digest(ready):
    client, attempt = ready
    for extra in ({"repo_id": REPO_ID}, {"repository_url": URL},
                  {"manifest_digest": DIGEST}, {"tenant": "research"}):
        refused(ask(client, attempt, **extra), 422)


def test_the_repository_is_the_tasks_even_when_another_is_indexed(ready, ldb):
    """Another registration of the same tenant has an index at OTHER_SHA; the
    task's own repository does not. Asking for OTHER_SHA is a 404: the body
    never moves the ticket to another repository."""
    client, attempt = ready
    other = repo_id_for("eng", "saga", "gadgets")
    register(ldb, repo_id=other)
    version(ldb, OTHER_SHA, repo_id=other)
    refused(ask(client, attempt, index_sha=OTHER_SHA), 404)


def test_a_registration_of_another_tenant_is_never_used(ready, ldb):
    client, attempt = ready
    register(ldb, tenant_id="research")
    refused(ask(client, attempt), 404)


def test_a_task_without_a_github_repository_gets_no_ticket(ready, ldb):
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["repository_url"] = None
    refused(ask(client, attempt), 404)
    ldb.docs[f"tasks/{TASK}"]["repository_url"] = "https://gitlab.com/saga/widgets"
    refused(ask(client, attempt), 404)


def test_a_malformed_index_sha_is_refused(ready):
    client, attempt = ready
    for bad in ("A" * 40, "a" * 39, "../" + "a" * 37, 7):
        refused(ask(client, attempt, index_sha=bad), 422)


# -- the digest comes from Firestore -----------------------------------------


def test_the_digest_is_firestores_never_the_buckets(ready, ldb, objects):
    """A manifest rewritten in GCS (any agent of the tenant can write under
    tenants/<t>/) changes nothing: the ticket names the promoted digest."""
    client, attempt = ready
    key = f"tenants/eng/repos/{REPO_ID}/graph/{SHA}/manifest.json"
    objects.put(key, b'{"rewritten": true}')
    body = ask(client, attempt).json()
    assert claims(body["ticket"])["manifest_digest"] == DIGEST


def test_an_index_version_without_a_graph_digest_gets_no_ticket(ready, ldb):
    client, attempt = ready
    for digest in (None, "", "md5:abc", "sha256:" + "z" * 64):
        version(ldb, digest=digest)
        refused(ask(client, attempt), 404)


def test_an_unindexed_commit_gets_no_ticket(ready):
    client, attempt = ready
    refused(ask(client, attempt, index_sha=OTHER_SHA), 404)


# -- the stored setting decides (invariant 10) --------------------------------


def test_the_workspace_setting_off_is_403_context_disabled(ready, ldb):
    client, attempt = ready
    set_context(ldb, "off")
    body = refused(ask(client, attempt), 403)
    assert body.get("detail", {}).get("reason") == "context_disabled"


def test_the_repositorys_override_beats_the_workspace(ready, ldb):
    client, attempt = ready
    set_context(ldb, "off")
    register(ldb, context="on")
    assert ask(client, attempt).status_code == 200
    set_context(ldb, "on")
    register(ldb, context="off")
    body = refused(ask(client, attempt), 403)
    assert body["detail"]["reason"] == "context_disabled"


@pytest.mark.parametrize("tenants,status", [((), 403), (("eng",), 200)])
def test_a_cut_over_workspaces_unsigned_setting_reads_as_unset(make, ldb, tenants, status):
    # ISO-API-SETTINGS (#1457): the graph ticket's code-intelligence reader is
    # built from ApiSettings' settings key and cutover list. eng cut over, a
    # worker's unsigned `context: off` is no setting at all and the default
    # lets the ticket through; with eng not listed it still refuses.
    client = make(graph_ticket_key=GRAPH_KEY, settings_key="settings-" + secrets.token_hex(24),
                  settings_mac_tenants=tenants)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL)
    register(ldb)
    version(ldb)
    set_context(ldb, "off")
    assert ask(client, attempt).status_code == status


def test_another_workspaces_setting_never_decides(ready, ldb):
    client, attempt = ready
    set_context(ldb, "off", tenant_id="research")
    assert ask(client, attempt).status_code == 200
