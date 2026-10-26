"""The `cf-access` slot's host list, swarm-api's half (CF-ACCESS-TOKEN, part of #2442).

A Cloudflare Access service token is a workspace secret of kind
`oauth_client` granted to `profile:claude-code-browser` in its `cf-access`
slot, with the hosts the gate may send it to set beside the slot, on the
grant. The list decides where a credential is SENT, and a grant document is
tenant-writable, so swarm-api signs it in attestation form "v3" and the
release honours a host-scoped grant only when that attestation verifies.

The catalogue declares no `cf-access` slot yet (contract request 117,
docs/contract-change-requests.md), so these tests add it to
`profile:claude-code-browser` for their own run, as the frozen contract
would. Drives the real store and release route over the in-memory Firestore
with the helpers of test_grant_attest.py. Every key and token is built at
runtime, never a literal.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from swarm_api import workspace_secrets as ws
from swarm_api.errors import ApiError, ValidationFailed
from swarm_api.grant_attest import ATTESTATION_FIELD, VALID, GrantKeys, attested_form, canonical

from .test_child_tasks_api import worker_tokens  # noqa: F401  (a fixture)
from .test_grant_attest import grant_doc, keys, sid, sm, store, world  # noqa: F401  (fixtures)
from .test_secret_release import ROOT, _tenant, release, running

BROWSER = "profile:claude-code-browser"
HOSTS = ["staging-admin.example.dev", "admin.example.com"]


@pytest.fixture
def cf_slot(monkeypatch):
    """`profile:claude-code-browser` with the slot request 117 asks for."""
    entry = ws.CONSUMERS_BY_NAME[BROWSER]
    slot = ws.Slot("cf-access", ("oauth_client",),
                   "sent by the live gate to the grant's hosts as CF-Access-Client-Id and "
                   "CF-Access-Client-Secret; never in the agent's reach")
    monkeypatch.setitem(ws.CONSUMERS_BY_NAME, BROWSER,
                        ws.Consumer(entry.kind, entry.name, entry.slots + (slot,)))


def _token() -> tuple[str, str]:
    return secrets.token_hex(16) + ".access", secrets.token_hex(32)


@pytest.fixture
def token(db, sm, world, keys):  # noqa: F811
    client_id, client_secret = _token()
    store(db, sm, keys["one"]).create(
        tenant=_tenant(db, "eng"), name="platform-admin", provider="other", kind="oauth_client",
        payload=json.dumps({"client_id": client_id, "client_secret": client_secret}).encode(),
        by=ROOT, via_link=False)
    return client_id, client_secret


def _grant(db, sm, key: str, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"tenant_id": "eng", "name": "platform-admin", "consumer": BROWSER,
                            "slot": "cf-access", "enabled": True, "by": ROOT, "hosts": HOSTS}
    args.update(over)
    return store(db, sm, key).set_grant(**args)


def _released(world, db, keys) -> list[dict[str, Any]]:  # noqa: F811
    client = world(workspace_grant_key=keys["one"])
    response = release(client, running(client, db, profile="claude-code-browser"))
    assert response.status_code == 200, response.text
    return response.json()["secrets"]


# --------------------------------------------------------------------------
# Writing the list
# --------------------------------------------------------------------------


def test_the_grant_stores_the_hosts_sorted_and_signs_them_in_form_v3(db, sm, world, keys, token, cf_slot):
    record = _grant(db, sm, keys["one"], hosts=HOSTS + [HOSTS[0]])
    doc = grant_doc(db, sid(db, sm, "platform-admin"), BROWSER)
    assert doc["hosts"] == sorted(set(HOSTS))
    assert attested_form(doc) == "v3" and GrantKeys(keys["one"]).check(doc) == VALID
    [listed] = [g for g in record["grants"] if g["consumer"] == BROWSER]
    assert listed["hosts"] == sorted(set(HOSTS)) and ATTESTATION_FIELD not in listed


@pytest.mark.parametrize("hosts", [
    None, [], ["*.example.com"], ["https://admin.example.com"], ["admin.example.com/path"],
    ["admin.example.com:443"], ["203.0.113.7"], ["Admin.Example.com"], ["localhost"],
    [f"h{i}.example.com" for i in range(ws.MAX_SLOT_HOSTS + 1)],
])
def test_an_enabled_cf_access_grant_without_a_good_host_list_is_refused(db, sm, world, keys, token, cf_slot,
                                                                        hosts):
    with pytest.raises(ValidationFailed):
        _grant(db, sm, keys["one"], hosts=hosts)
    assert f"{ws.GRANTS_COLLECTION}/{sid(db, sm, 'platform-admin')}~{BROWSER}" not in db.docs


def test_another_slot_takes_no_host_list(db, sm, world, keys, cf_slot):
    with pytest.raises(ValidationFailed):
        store(db, sm, keys["one"]).set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code",
                                             slot=None, enabled=True, by=ROOT, hosts=HOSTS)


def test_without_a_grant_key_a_host_list_is_never_written(db, sm, world, keys, token, cf_slot):
    with pytest.raises(ApiError):
        _grant(db, sm, "")
    assert f"{ws.GRANTS_COLLECTION}/{sid(db, sm, 'platform-admin')}~{BROWSER}" not in db.docs


def test_without_request_117_the_slot_is_refused(db, sm, world, keys, token):
    with pytest.raises(ValidationFailed):
        _grant(db, sm, keys["one"])


# --------------------------------------------------------------------------
# The release
# --------------------------------------------------------------------------


def test_the_release_answers_the_token_with_its_hosts(db, sm, world, keys, token, cf_slot):
    _grant(db, sm, keys["one"])
    [item] = _released(world, db, keys)
    assert item["slot"] == "cf-access" and item["hosts"] == sorted(HOSTS)
    assert json.loads(item["value"]) == {"client_id": token[0], "client_secret": token[1]}


def test_a_files_item_carries_no_hosts(db, sm, world, keys, cf_slot):
    store(db, sm, keys["one"]).set_grant(tenant_id="eng", name="a-key", consumer=BROWSER, slot="files",
                                         enabled=True, by=ROOT)
    [item] = _released(world, db, keys)
    assert item["slot"] == "files" and "hosts" not in item


@pytest.mark.parametrize("rewrite", ["host_added", "host_swapped", "hosts_dropped", "relabelled_v1",
                                     "unattested"])
def test_a_host_list_rewritten_in_firestore_is_never_released(db, sm, world, keys, token, cf_slot, rewrite):
    """An agent that writes Firestore cannot send the token to its own host."""
    _grant(db, sm, keys["one"])
    doc = grant_doc(db, sid(db, sm, "platform-admin"), BROWSER)
    if rewrite == "host_added":
        doc["hosts"] = sorted(doc["hosts"] + ["evil.example"])
    elif rewrite == "host_swapped":
        doc["hosts"] = ["evil.example"]
    elif rewrite == "hosts_dropped":
        # Without its list the grant is form v1, which never carries a host-scoped item.
        doc.pop("hosts")
        doc[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(doc)
    elif rewrite == "relabelled_v1":
        doc[ATTESTATION_FIELD] = "v1." + doc[ATTESTATION_FIELD].split(".")[1]
    else:
        doc[ATTESTATION_FIELD] = None
    assert _released(world, db, keys) == []


def test_form_v3_ends_with_the_repository_and_the_hosts():
    form = json.loads(canonical("s", "t", "c", "cf-access", True, None, ["a.example.com"]))
    assert form == ["v3", "s", "t", "c", "cf-access", True, None, ["a.example.com"]]
    assert json.loads(canonical("s", "t", "c", "files", True)) == ["v1", "s", "t", "c", "files", True]
