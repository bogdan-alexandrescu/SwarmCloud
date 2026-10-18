"""A workspace's judgment backend counts only when swarm-api signed it (lane
ISO-SCHED, interim A of docs/design/tenant-settings-integrity.md §4.5; part of
#1457).

Every tenant's worker can write `workspace_settings/<any tenant>`. With
`judgment_backend: typesafe` forged onto tenant B's document, B's judgments
went to TypeSafe on B's key and B's pods were labelled for the forwarder.
With SETTINGS_MAC_REQUIRED on, a block whose MAC is missing or wrong reads as
unset in BOTH readers -- `judgment.read_backend` (the pod) and
`dispatch.read_judge_backend` (the router) -- so the forged value is the
default, internal-jev. OFF by default, where both read what they read before.

MUTATION: make `block_verifies` return True, drop the tenant from
`settings_mac`'s bytes, verify only the top level in `trusted_settings`, or
wire `build_router` without `settings_mac_key`, and the named case fails.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import scheduler_settings  # noqa: E402
from control_plane.fakes import FakeFirestore  # noqa: E402

from scheduler.dispatch import build_router, read_judge_backend  # noqa: E402
from scheduler.judgment import (  # noqa: E402
    SETTINGS_COLLECTION,
    block_verifies,
    read_backend,
    settings_mac,
    settings_mac_key,
    trusted_settings,
)
from scheduler.settings import SchedulerSettings  # noqa: E402

#: Built at run time: nothing here may look like a credential.
KEY = secrets.token_hex(32)


def signed(tenant_id: str, block: str, fields: dict, key: str = KEY) -> dict:
    """`fields` with the MAC swarm-api stores beside them (§4.5)."""
    return {**fields, "mac": {"v": 1, "key": "1",
                              "sig": settings_mac(key, tenant_id=tenant_id, block=block,
                                                  fields=fields)}}


def db_with(tenant_id: str, doc: dict) -> FakeFirestore:
    db = FakeFirestore()
    db.docs[f"{SETTINGS_COLLECTION}/{tenant_id}"] = doc
    return db


def both(db, tenant_id: str, mac_key: str | None) -> tuple[str, str | None]:
    return (read_backend(db, tenant_id, mac_key=mac_key),
            read_judge_backend(db, tenant_id, mac_key=mac_key))


FORGED_TOP = {"judgment_backend": "typesafe"}


@pytest.mark.parametrize("doc", [
    pytest.param(FORGED_TOP, id="no-mac"),
    pytest.param({**FORGED_TOP, "mac": {"v": 1, "key": "1", "sig": "not base64!"}},
                 id="sig-not-base64"),
    pytest.param(signed("eng", "top", FORGED_TOP, key=secrets.token_hex(32)),
                 id="another-key"),
    pytest.param(signed("other", "top", FORGED_TOP), id="signed-for-another-tenant"),
    pytest.param(signed("eng", "code_intelligence", FORGED_TOP), id="signed-as-another-block"),
    pytest.param({**signed("eng", "top", {"judgment_backend": "internal-laya"}),
                  "judgment_backend": "typesafe"}, id="value-changed-after-signing"),
    pytest.param({**FORGED_TOP, "mac": {**signed("eng", "top", FORGED_TOP)["mac"], "v": 2}},
                 id="another-version"),
])
def test_an_unsigned_or_badly_signed_typesafe_reads_as_internal_jev_in_both_readers(doc):
    assert both(db_with("eng", doc), "eng", KEY) == ("internal-jev", "internal-jev")


def test_a_signed_choice_counts_in_both_readers():
    db = db_with("eng", signed("eng", "top", FORGED_TOP))
    assert both(db, "eng", KEY) == ("typesafe", "typesafe")


def test_an_unsigned_nested_judgment_backend_is_ignored():
    """The router reads `code_intelligence.judgment_backend` first; unsigned,
    it is dropped and the signed top level decides."""
    doc = {**signed("eng", "top", {"judgment_backend": "internal-laya"}),
           "code_intelligence": {"judgment_backend": "typesafe", "jev": True,
                                 "uncovered": 1}}
    assert read_judge_backend(db_with("eng", doc), "eng", mac_key=KEY) == "internal-laya"
    # Every covered field of the unsigned block goes; a field outside
    # SETTINGS_MAC_FIELDS is not this reader's to judge.
    assert trusted_settings(doc, tenant_id="eng", key=KEY)["code_intelligence"] == {
        "uncovered": 1}

    alone = {"code_intelligence": {"judgment_backend": "typesafe"}}
    assert read_judge_backend(db_with("eng", alone), "eng", mac_key=KEY) == "internal-jev"


def test_a_signed_nested_judgment_backend_counts():
    block = {"judgment_backend": "typesafe", "jev": True}
    doc = {"code_intelligence": signed("eng", "code_intelligence", block)}
    assert read_judge_backend(db_with("eng", doc), "eng", mac_key=KEY) == "typesafe"


def test_with_the_switch_off_both_readers_read_what_they_did():
    db = db_with("eng", {"judgment_backend": "typesafe",
                         "code_intelligence": {"judgment_backend": "internal-laya"}})
    assert both(db, "eng", None) == ("typesafe", "internal-laya")


def test_no_key_trusts_nothing():
    doc = signed("eng", "top", FORGED_TOP)
    assert not block_verifies("", tenant_id="eng", block="top", fields=doc)
    assert both(db_with("eng", doc), "eng", "") == ("internal-jev", "internal-jev")


# -- the switch ---------------------------------------------------------------

def test_the_switch_defaults_off_and_its_key_stays_out_of_the_repr(monkeypatch):
    for name in ("SETTINGS_MAC_REQUIRED", "SWARM_SETTINGS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROJECT_ID", "proj-iso-sched")
    off = SchedulerSettings.from_env()
    assert off.settings_mac_required is False
    assert settings_mac_key(off) is None

    monkeypatch.setenv("SETTINGS_MAC_REQUIRED", "true")
    monkeypatch.setenv("SWARM_SETTINGS_KEY", KEY)
    on = SchedulerSettings.from_env()
    assert settings_mac_key(on) == KEY
    assert KEY not in repr(on)


def test_the_switch_on_without_a_key_refuses_to_start():
    with pytest.raises(ValueError, match="SWARM_SETTINGS_KEY"):
        scheduler_settings(settings_mac_required=True)


@pytest.mark.parametrize("required,expected", [(False, "typesafe"), (True, "internal-jev")])
def test_the_router_and_the_pod_read_through_the_switch(monkeypatch, required, expected):
    for name in ("GKE_ENDPOINT", "GKE_CA_CERT_B64"):
        monkeypatch.delenv(name, raising=False)
    settings = scheduler_settings(settings_mac_required=required,
                                  settings_mac_key=KEY if required else "")
    db = db_with("eng", {"code_intelligence": {"judgment_backend": "typesafe"},
                         "judgment_backend": "typesafe"})
    router = build_router(settings, db=db)
    assert router._judgment("eng") == expected
    assert router._gke._judgment("eng") == expected
