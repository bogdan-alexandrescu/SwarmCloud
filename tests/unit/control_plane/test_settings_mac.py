"""swarm-api signs the `workspace_settings` blocks the control plane acts on
(#1457; docs/design/tenant-settings-integrity.md §3.4, §4.5 and §5 request B;
contract request 86; lane ISO-SETTINGS-MAC).

Any tenant's worker can write `workspace_settings/<B>` directly. So every
write swarm-api makes stores, beside the block it wrote, a MAC under the
`swarm-settings-key` over `swarm_common.specsign.canonical_settings_block`,
and a reader for a tenant cut over in SETTINGS_MAC_TENANTS acts on a block
only when that MAC verifies. What these tests hold:

  * a write through each route (judgment backend, merge settings, code
    intelligence) stores a MAC that a verifier built from the CONTRACT alone
    accepts -- `reference_verifies` below is written from the contract's
    words, not from swarm_api, so it is what the scheduler's verifier computes;
  * with no key configured nothing is signed (today's write), and with no
    tenant cut over a forged value reads as it did before this lane;
  * for a tenant cut over, a forged block (no MAC, a bad one, or another
    tenant's) reads as unset through every route;
  * the code-intelligence writer never signs `code_intelligence.
    judgment_backend`, which no swarm-api path writes;
  * the re-sign pass is idempotent, writes only each block's `mac`, holds a
    block carrying a covered key no writer writes, keys on the document id,
    and a dry run writes nothing.

Every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import secrets

import pytest

from swarm_api import codeintel, judgment, mergesettings
from swarm_api.auth import TENANT_HEADER
from swarm_common.identity import tenant_id_for_user
from swarm_common.specsign import SETTINGS_MAC_FIELDS, canonical_settings_block

from .conftest import auth_header, seed_tenant
from .fakes import FakeFirestore

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)


def new_key() -> str:
    return "settings-" + secrets.token_hex(24)


def reference_verifies(key: str, tenant_id: str, block: str, fields: dict) -> bool:
    """The contract's MAC, recomputed without swarm_api: HMAC-SHA256 under the
    key over canonical_settings_block, base64 in `mac.sig`, `mac.v == 1`."""
    mac = fields.get("mac")
    if not isinstance(mac, dict) or mac.get("v") != 1:
        return False
    want = hmac.new(key.encode(), canonical_settings_block(
        tenant_id=tenant_id, block=block, fields=fields), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64decode(mac["sig"]), want)


def configure(ctx, **mac) -> None:
    """Give the running app these ApiSettings fields (settings_key,
    settings_key_previous, settings_mac_tenants): the routes build each
    reader from `ctx.settings` per request, as a deployment's are built from
    the settings it started with."""
    ctx.settings = dataclasses.replace(ctx.settings, **mac)


@pytest.fixture
def key(api_context) -> str:
    value = new_key()
    configure(api_context, settings_key=value)
    return value


@pytest.fixture
def cut_over(api_context, key) -> str:
    configure(api_context, settings_mac_tenants=(CAROL_TENANT, "eng"))
    return key


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    return db


def doc(db, tenant_id: str) -> dict:
    return db.docs.get(f"workspace_settings/{tenant_id}") or {}


def put_backend(client, tenant_id: str, backend: str):
    headers = auth_header("root")
    headers[TENANT_HEADER] = "eng"
    return client.put(f"/v1/workspaces/{tenant_id}/judgment-backend", headers=headers,
                      json={"judgment_backend": backend})


def get_backend(client, tenant_id: str) -> str:
    headers = auth_header("root")
    headers[TENANT_HEADER] = "eng"
    response = client.get(f"/v1/workspaces/{tenant_id}/judgment-backend", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["judgment_backend"]


def put_merge(client, tenant_id: str, body: dict):
    return client.put(f"/v1/workspaces/{tenant_id}/merge-settings",
                      headers=auth_header("root"), json=body)


def get_merge(client, tenant_id: str) -> dict:
    response = client.get(f"/v1/workspaces/{tenant_id}/merge-settings",
                          headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return response.json()


def put_ci(client, tenant_id: str, body: dict):
    return client.put(f"/v1/workspaces/{tenant_id}/code-intelligence",
                      headers=auth_header("root"), json=body)


def get_ci(client, tenant_id: str) -> dict:
    response = client.get(f"/v1/workspaces/{tenant_id}/code-intelligence",
                          headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return response.json()


# -- every write signs ----------------------------------------------------------


def test_a_judgment_backend_write_stores_a_mac_the_contracts_verifier_accepts(client, seeded, key):
    assert put_backend(client, "eng", "typesafe").status_code == 200
    stored = doc(seeded, "eng")
    assert stored["judgment_backend"] == "typesafe"
    assert reference_verifies(key, "eng", "top", stored)
    assert stored["mac"]["key"] == judgment.key_id(key)
    # The tenant is inside the bytes: the same block does not verify as another's.
    assert not reference_verifies(key, "research", "top", stored)


def test_a_merge_settings_write_stores_a_mac_the_contracts_verifier_accepts(client, seeded, key):
    response = put_merge(client, "eng", {"mode": "train", "train_size": 6})
    assert response.status_code == 200, response.text
    block = doc(seeded, "eng")["merge"]
    assert (block["mode"], block["train_size"]) == ("train", 6)
    assert reference_verifies(key, "eng", "merge", block)
    assert not reference_verifies(key, "eng", "code_intelligence", block)


def test_a_code_intelligence_write_stores_a_mac_the_contracts_verifier_accepts(client, seeded,
                                                                               key):
    response = put_ci(client, "eng", {"jev": True, "context": "off"})
    assert response.status_code == 200, response.text
    block = doc(seeded, "eng")["code_intelligence"]
    assert block["jev"] is True and block["context"] == "off"
    assert reference_verifies(key, "eng", "code_intelligence", block)
    response = put_ci(client, "eng", {"graph_delivery": "service"})
    assert response.status_code == 200, response.text
    block = doc(seeded, "eng")["code_intelligence"]
    assert reference_verifies(key, "eng", "code_intelligence", block)


def test_all_three_blocks_of_one_document_verify_together(client, seeded, key):
    assert put_backend(client, "eng", "internal-laya").status_code == 200
    assert put_merge(client, "eng", {"mode": "train"}).status_code == 200
    assert put_ci(client, "eng", {"jev": True}).status_code == 200
    stored = doc(seeded, "eng")
    assert reference_verifies(key, "eng", "top", stored)
    assert reference_verifies(key, "eng", "merge", stored["merge"])
    assert reference_verifies(key, "eng", "code_intelligence", stored["code_intelligence"])


def test_with_no_key_a_write_is_what_it_was(client, seeded):
    assert put_backend(client, "eng", "typesafe").status_code == 200
    assert put_merge(client, "eng", {"mode": "train"}).status_code == 200
    assert put_ci(client, "eng", {"jev": True}).status_code == 200
    stored = doc(seeded, "eng")
    assert "mac" not in stored
    assert "mac" not in stored["merge"] and "mac" not in stored["code_intelligence"]


def test_the_code_intelligence_writer_never_signs_a_planted_judgment_backend(client, seeded,
                                                                             key):
    seeded.docs["workspace_settings/eng"] = {
        "tenant_id": "eng",
        "code_intelligence": {"judgment_backend": "typesafe", "jev_monthly_cap_tokens": 5000},
    }
    assert put_ci(client, "eng", {"jev": True}).status_code == 200
    block = doc(seeded, "eng")["code_intelligence"]
    assert "judgment_backend" not in block
    assert block["jev_monthly_cap_tokens"] == 5000  # a key it does not own is carried over
    assert reference_verifies(key, "eng", "code_intelligence", block)


def test_each_writers_fields_are_the_ones_the_module_writes():
    assert judgment.WRITER_FIELDS["top"] == (judgment.SETTING_FIELD,)
    assert judgment.WRITER_FIELDS["merge"] == tuple(mergesettings.MergeSettings().as_dict())
    assert judgment.WRITER_FIELDS["code_intelligence"] == codeintel.OWNED_FIELDS
    assert set(judgment.WRITER_FIELDS) == set(SETTINGS_MAC_FIELDS)
    for block, fields in judgment.WRITER_FIELDS.items():
        assert set(fields) <= set(SETTINGS_MAC_FIELDS[block])
    assert judgment.unowned_covered_keys("code_intelligence") == ("judgment_backend",)
    assert judgment.unowned_covered_keys("top") == ()
    assert judgment.unowned_covered_keys("merge") == ()


# -- the readers ------------------------------------------------------------------


def forge(db, tenant_id: str) -> None:
    """What a tenant worker can write: every block, unsigned."""
    db.docs[f"workspace_settings/{tenant_id}"] = {
        "tenant_id": tenant_id, "judgment_backend": "typesafe",
        "merge": {"mode": "train", "train_size": 9, "max_train_age_minutes": 30},
        "code_intelligence": {"jev": True, "context": "off", "graph_delivery": "service"},
    }


def test_before_a_tenant_is_cut_over_an_unsigned_value_reads_as_before(client, seeded, key):
    forge(seeded, "eng")
    assert get_backend(client, "eng") == "typesafe"
    assert get_merge(client, "eng")["mode"] == "train"
    assert get_ci(client, "eng")["jev"] is True


def test_a_tenant_cut_over_reads_a_forged_block_as_unset(client, seeded, cut_over):
    forge(seeded, "eng")
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND
    merge = get_merge(client, "eng")
    assert (merge["mode"], merge["train_size"]) == ("direct", mergesettings.TRAIN_SIZE_DEFAULT)
    ci = get_ci(client, "eng")
    assert ci["jev"] is False and ci["context"] == "inherit"
    assert ci["graph_delivery"] == "inherit"
    mac = judgment.SettingsMac(keys=(cut_over,), tenants=("eng",))
    store = mergesettings.MergeSettingsStore(seeded, now=lambda: None, mac=mac)
    assert store.mode_for("eng", None) == "direct"
    assert codeintel.CodeIntelligenceStore(seeded, now=lambda: None, mac=mac).get("eng").jev \
        is False


def test_a_tenant_cut_over_reads_its_signed_settings(client, seeded, cut_over):
    assert put_backend(client, "eng", "typesafe").status_code == 200
    assert put_merge(client, "eng", {"mode": "train"}).status_code == 200
    assert put_ci(client, "eng", {"jev": True}).status_code == 200
    assert get_backend(client, "eng") == "typesafe"
    assert get_merge(client, "eng")["mode"] == "train"
    assert get_ci(client, "eng")["jev"] is True


def test_a_signed_value_changed_afterwards_or_copied_from_another_tenant_is_unset(
        client, seeded, cut_over):
    assert put_backend(client, "eng", "internal-laya").status_code == 200
    seeded.docs["workspace_settings/eng"]["judgment_backend"] = "typesafe"
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND
    # research chose TypeSafe and signed it; its block pasted onto eng does not verify.
    assert put_backend(client, "research", "typesafe").status_code == 200
    seeded.docs["workspace_settings/eng"] = copy.deepcopy(doc(seeded, "research"))
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND


def test_a_mac_under_the_previous_key_still_verifies_during_a_rotation(client, seeded, cut_over,
                                                                     api_context):
    assert put_backend(client, "eng", "typesafe").status_code == 200
    configure(api_context, settings_key=new_key(), settings_key_previous=cut_over)
    assert get_backend(client, "eng") == "typesafe"
    configure(api_context, settings_key_previous="")
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND


def test_a_cut_over_tenant_with_no_key_fails_closed(client, seeded, cut_over, api_context):
    assert put_backend(client, "eng", "typesafe").status_code == 200
    configure(api_context, settings_key="")
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND


@pytest.mark.parametrize("bad", [None, "nope", {"v": 2, "sig": ""}, {"v": 1, "sig": 7},
                                 {"v": 1, "sig": "!!not base64!!"},
                                 {"v": 1, "sig": base64.b64encode(b"\0" * 32).decode()}])
def test_a_malformed_mac_never_verifies(key, bad):
    fields = {"judgment_backend": "typesafe", "mac": bad}
    assert judgment.block_verifies("eng", "top", fields, (key,)) is False


def test_the_cutover_list_takes_names_or_everyone():
    assert not judgment.mac_enforced("eng", ())
    assert not judgment.mac_enforced("eng", ("research",))
    assert judgment.mac_enforced("eng", ("research", " eng "))
    assert judgment.mac_enforced("eng", ("*",))


def test_the_cutover_ships_empty_and_the_key_id_says_nothing_of_the_key(key):
    assert judgment.SETTINGS_MAC_TENANTS_ENV == "SETTINGS_MAC_TENANTS"
    assert judgment.SETTINGS_KEY_SECRET_ID == "swarm-settings-key"
    ident = judgment.key_id(key)
    assert len(ident) == 16 and ident not in key and key not in ident


# -- the re-sign pass -----------------------------------------------------------------


def legacy(db: FakeFirestore) -> None:
    """Documents written before this lane: valid settings, no MAC."""
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng", "judgment_backend": "typesafe",
        "judgment_backend_set_by": "root@saga.xyz",
        "merge": {"tenant_id": "eng", "mode": "train", "train_size": 4,
                  "max_train_age_minutes": 90, "updated_by": "root@saga.xyz"},
        "code_intelligence": {"jev": True, "jev_monthly_cap_tokens": 1000},
    }
    db.docs["workspace_settings/research"] = {
        # A forged tenant_id field: the pass keys on the document id.
        "tenant_id": "eng", "judgment_backend": "internal-laya",
        "code_intelligence": {"jev": True, "judgment_backend": "typesafe"},
    }
    db.docs["workspace_settings/empty"] = {"tenant_id": "empty"}


def test_a_dry_run_plans_and_writes_nothing():
    db = FakeFirestore()
    legacy(db)
    before = copy.deepcopy(db.docs)
    key = new_key()
    plan = judgment.resign_workspace_settings(db, key, apply=False)
    assert db.docs == before
    assert plan["written"] == 0 and plan["applied"] is False and plan["documents"] == 3
    statuses = {(b["tenant_id"], b["block"]): b["status"] for b in plan["blocks"]}
    assert statuses == {
        ("eng", "top"): "unsigned", ("eng", "merge"): "unsigned",
        ("eng", "code_intelligence"): "unsigned",
        ("research", "top"): "unsigned", ("research", "code_intelligence"): "held",
    }
    assert plan["key"] == judgment.key_id(key)
    assert key not in repr(plan)


def test_apply_signs_only_the_mac_and_a_second_run_writes_nothing():
    db = FakeFirestore()
    legacy(db)
    before = copy.deepcopy(db.docs)
    key = new_key()
    result = judgment.resign_workspace_settings(db, key, apply=True)
    assert result["written"] == 4
    research = db.docs["workspace_settings/research"]
    assert reference_verifies(key, "research", "top", research)
    assert not reference_verifies(key, "eng", "top", research)
    eng = db.docs["workspace_settings/eng"]
    assert reference_verifies(key, "eng", "top", eng)
    assert reference_verifies(key, "eng", "merge", eng["merge"])
    assert reference_verifies(key, "eng", "code_intelligence", eng["code_intelligence"])

    def without_macs(d: dict) -> dict:
        d = copy.deepcopy(d)
        d.pop("mac", None)
        for block in ("merge", "code_intelligence"):
            if isinstance(d.get(block), dict):
                d[block].pop("mac", None)
        return d

    # Nothing but the MACs changed: every setting, and every field outside the
    # contract's lists, is as it was.
    assert {p: without_macs(d) for p, d in db.docs.items()} == before
    # The held block is never signed.
    assert "mac" not in research["code_intelligence"]

    signed = copy.deepcopy(db.docs)
    again = judgment.resign_workspace_settings(db, key, apply=True)
    assert again["written"] == 0
    assert db.docs == signed
    assert {b["status"] for b in again["blocks"]} == {"signed", "held"}


def test_a_field_outside_the_contract_does_not_need_a_new_signature():
    db = FakeFirestore()
    legacy(db)
    key = new_key()
    judgment.resign_workspace_settings(db, key, apply=True)
    db.docs["workspace_settings/eng"]["merge"]["updated_by"] = "someone-else"
    assert judgment.resign_workspace_settings(db, key, apply=True)["written"] == 0


def test_a_block_signed_under_a_rotated_key_is_signed_again():
    db = FakeFirestore()
    legacy(db)
    old, new = new_key(), new_key()
    judgment.resign_workspace_settings(db, old, apply=True)
    plan = judgment.resign_workspace_settings(db, new, apply=False)
    assert {b["status"] for b in plan["blocks"] if b["tenant_id"] == "eng"} == {"unsigned"}
    # A MAC that verifies but names another key id is `stale`, and re-signed.
    eng = db.docs["workspace_settings/eng"]
    eng["mac"]["key"] = "0" * 16
    assert judgment.resign_workspace_settings(db, old, apply=False)["blocks"][0]["status"] == \
        "stale"
    assert judgment.resign_workspace_settings(db, new, apply=True)["written"] == 4
    assert reference_verifies(new, "eng", "top", db.docs["workspace_settings/eng"])


def test_one_tenant_at_a_time_and_never_without_a_key():
    db = FakeFirestore()
    legacy(db)
    key = new_key()
    result = judgment.resign_workspace_settings(db, key, apply=True, tenant_id="eng")
    assert result["documents"] == 1 and result["written"] == 3
    assert {b["tenant_id"] for b in result["blocks"]} == {"eng"}
    assert judgment.resign_workspace_settings(db, key, apply=True, tenant_id="nobody")[
        "documents"] == 0
    with pytest.raises(ValueError):
        judgment.resign_workspace_settings(db, "", apply=True)


def test_a_resigned_document_reads_the_same_once_cut_over(client, seeded, api_context):
    key = new_key()
    legacy(seeded)
    configure(api_context, settings_key=key, settings_mac_tenants=("*",))
    assert get_backend(client, "eng") == judgment.DEFAULT_BACKEND  # unsigned: unset
    judgment.resign_workspace_settings(seeded, key, apply=True)
    assert get_backend(client, "eng") == "typesafe"
    assert get_merge(client, "eng")["mode"] == "train"
    assert get_ci(client, "eng")["jev"] is True
