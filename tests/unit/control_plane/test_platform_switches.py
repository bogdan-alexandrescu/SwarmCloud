"""The platform switches as console settings (docs/design/managed-plugins.md section 11, lane 9).

    GET /v1/admin/platform-switches   each switch: the value in force, the
                                      stored one, the Terraform default; the
                                      document's version
    PUT /v1/admin/platform-switches   `{version, work_contracts?, contract_review?}`,
                                      a null resetting a switch to its
                                      Terraform default; platform admins
                                      only; audited per switch

`SWARM_WORK_CONTRACTS` and `SWARM_CONTRACT_REVIEW` were Terraform-only. Now a
platform admin sets either from the console; until one does, and whenever the
stored document's MAC (`canonical_platform_settings`, contract request 144)
fails, the Terraform value is what the readers in workcontract.py and
contractreview.py act on. Offline: FakeFirestore, a random settings key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.specsign import canonical_platform_settings

from swarm_api import contractreview, platformswitches as ps, workcontract as wc
from swarm_api.judgment import SettingsMac
from swarm_api.main import create_app

from .conftest import auth_header
from .test_settings_mac import configure, new_key

URL = "/v1/admin/platform-switches"


@pytest.fixture(autouse=True)
def _no_reader_left_behind():
    """create_app installs this instance's reader; none outlives its test,
    so a stored switch here never reaches another file's readers."""
    ps.uninstall()
    yield
    ps.uninstall()


@pytest.fixture
def key(api_context) -> str:
    value = new_key()
    configure(api_context, settings_key=value)
    return value


@pytest.fixture
def app_client(api_context, key) -> TestClient:
    return TestClient(create_app(api_context))


@pytest.fixture
def terraform(monkeypatch):
    """The Terraform values, as the environment carries them."""
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    monkeypatch.setenv(contractreview.MODE_ENV, "report")


def _audit(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == ps.AUDIT_ACTION]


def _stored(db: Any) -> dict[str, Any]:
    return db.docs.get(f"{ps.COLLECTION}/{ps.DOCUMENT}") or {}


def _reference_verifies(key: str, doc: dict[str, Any]) -> bool:
    """The contract's MAC, recomputed without swarm_api."""
    mac = doc.get("mac")
    if not isinstance(mac, dict) or mac.get("v") != 1:
        return False
    want = hmac.new(key.encode(), canonical_platform_settings(doc), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64decode(mac["sig"]), want)


def _put(client: TestClient, body: dict[str, Any], user: str = "root"):
    return client.put(URL, json=body, headers=auth_header(user))


# -- the Terraform default until set ------------------------------------------------


def test_each_switch_reads_its_terraform_default_until_an_admin_sets_one(app_client, db, terraform):
    response = app_client.get(URL, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 0
    assert body["set_by"] is None and body["set_at"] is None
    assert body["switches"]["work_contracts"] == {
        "value": "on", "stored": None, "terraform_default": "on", "source": "terraform",
        "options": ["off", "on"]}
    assert body["switches"]["contract_review"] == {
        "value": "report", "stored": None, "terraform_default": "report", "source": "terraform",
        "options": ["off", "report", "enforce"]}
    assert _stored(db) == {}
    # Both readers act on the Terraform value while nothing is stored.
    assert wc.enabled() is True
    assert contractreview.mode() == "report"


def test_with_no_terraform_value_either_switch_is_off(app_client, monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    monkeypatch.delenv(contractreview.MODE_ENV, raising=False)
    body = app_client.get(URL, headers=auth_header("root")).json()
    assert body["switches"]["work_contracts"]["value"] == "off"
    assert body["switches"]["contract_review"]["value"] == "off"


# -- a set read back -------------------------------------------------------------------


def test_a_set_is_stored_signed_and_read_back(app_client, db, key, terraform):
    response = _put(app_client, {"version": 0, "work_contracts": "off",
                                 "contract_review": "enforce"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["set_by"] == "root@example.com"
    assert body["switches"]["work_contracts"]["value"] == "off"
    assert body["switches"]["work_contracts"]["source"] == "setting"
    assert body["switches"]["work_contracts"]["terraform_default"] == "on"
    assert body["switches"]["contract_review"]["value"] == "enforce"
    stored = _stored(db)
    assert stored["work_contracts"] == "off" and stored["contract_review"] == "enforce"
    assert _reference_verifies(key, stored)
    again = app_client.get(URL, headers=auth_header("root")).json()
    assert again["switches"] == body["switches"] and again["version"] == 1


def test_a_switch_the_body_leaves_out_keeps_its_value_and_a_null_resets_it(app_client, db, terraform):
    assert _put(app_client, {"version": 0, "work_contracts": "off",
                             "contract_review": "off"}).status_code == 200
    kept = _put(app_client, {"version": 1, "contract_review": "enforce"}).json()
    assert kept["switches"]["work_contracts"]["value"] == "off"
    reset = _put(app_client, {"version": 2, "work_contracts": None}).json()
    assert reset["switches"]["work_contracts"] == {
        "value": "on", "stored": None, "terraform_default": "on", "source": "terraform",
        "options": ["off", "on"]}
    assert reset["switches"]["contract_review"]["value"] == "enforce"


def test_a_value_outside_the_switch_s_options_is_refused(app_client, db):
    for body in ({"version": 0, "work_contracts": "enforce"},
                 {"version": 0, "contract_review": "on"},
                 {"version": 0, "plugins": "on"},
                 {"work_contracts": "on"}):
        assert _put(app_client, body).status_code == 422, body
    assert _stored(db) == {} and _audit(db) == []


def test_without_a_settings_key_nothing_is_stored(api_context, db):
    client = TestClient(create_app(api_context))
    response = _put(client, {"version": 0, "work_contracts": "on"})
    assert response.status_code == 409
    assert response.json()["code"] == ps.UNSIGNED_CODE
    assert _stored(db) == {} and _audit(db) == []


# -- a version conflict refused ----------------------------------------------------------


def test_a_stale_version_is_refused_and_changes_nothing(app_client, db, terraform):
    assert _put(app_client, {"version": 0, "work_contracts": "off"}).status_code == 200
    before = dict(_stored(db))
    stale = _put(app_client, {"version": 0, "work_contracts": "on"})
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == ps.CONFLICT_CODE
    assert stale.json()["detail"] == {"version": 1}
    assert _stored(db) == before
    assert len(_audit(db)) == 1


# -- non-admins refused --------------------------------------------------------------------


@pytest.mark.parametrize("user", ["alice", "bob", "carol"])
def test_a_person_who_is_not_a_platform_admin_can_neither_read_nor_set(app_client, db, user):
    assert app_client.get(URL, headers=auth_header(user)).status_code == 403
    assert _put(app_client, {"version": 0, "work_contracts": "on"}, user).status_code == 403
    assert _stored(db) == {} and _audit(db) == []


# -- every change audited ---------------------------------------------------------------------


def test_every_changed_switch_is_audited_with_who_when_and_both_values(app_client, db, terraform):
    _put(app_client, {"version": 0, "work_contracts": "off", "contract_review": "report"})
    rows = sorted(_audit(db), key=lambda r: r["detail"]["switch"])
    # contract_review was `report` by default and is now `report` by
    # setting: stored, so audited; its value in force did not move.
    assert [r["detail"] for r in rows] == [
        {"switch": "contract_review", "from": None, "to": "report",
         "from_value": "report", "to_value": "report"},
        {"switch": "work_contracts", "from": None, "to": "off",
         "from_value": "on", "to_value": "off"},
    ]
    for row in rows:
        assert row["by"] == "root@example.com"
        assert row["at"] is not None
        assert row["target"] == f"{ps.COLLECTION}/{ps.DOCUMENT}"
    # The same values again write nothing and audit nothing.
    same = _put(app_client, {"version": 1, "work_contracts": "off"})
    assert same.status_code == 200 and same.json()["version"] == 1
    assert len(_audit(db)) == 2
    _put(app_client, {"version": 1, "work_contracts": None})
    last = [r for r in _audit(db) if r["detail"]["switch"] == "work_contracts"][-1]
    assert last["detail"] == {"switch": "work_contracts", "from": "off", "to": None,
                              "from_value": "off", "to_value": "on"}


# -- both readers following the switch ------------------------------------------------------------


def test_both_readers_follow_a_set_on_this_instance_at_once(app_client, terraform):
    assert wc.enabled() is True and contractreview.mode() == "report"
    _put(app_client, {"version": 0, "work_contracts": "off", "contract_review": "enforce"})
    assert wc.enabled() is False
    assert contractreview.mode() == "enforce"
    # An explicit environment is the Terraform value; the setting still wins.
    assert wc.enabled({wc.SWITCH_ENV: "on"}) is False
    assert contractreview.mode({contractreview.MODE_ENV: "off"}) == "enforce"
    _put(app_client, {"version": 1, "work_contracts": None, "contract_review": None})
    assert wc.enabled() is True and contractreview.mode() == "report"


def test_another_instance_sees_a_change_once_its_cache_expires(db, key, terraform):
    now = [1000.0]
    reader = ps.PlatformSwitches(db, mac=SettingsMac(keys=(key,)), clock=lambda: now[0])
    ps.install(reader)
    assert wc.enabled() is True
    writer = ps.PlatformSwitches(db, mac=SettingsMac(keys=(key,)))
    writer.set({"work_contracts": "off"}, version=0, by="root@example.com")
    now[0] += ps.CACHE_SECONDS - 1
    assert wc.enabled() is True, "inside the cache window the old read stands"
    now[0] += 2
    assert wc.enabled() is False


def test_a_document_whose_mac_fails_reads_as_the_terraform_default(db, key, terraform, caplog):
    writer = ps.PlatformSwitches(db, mac=SettingsMac(keys=(key,)))
    writer.set({"work_contracts": "off", "contract_review": "off"}, version=0, by="root@example.com")
    path = f"{ps.COLLECTION}/{ps.DOCUMENT}"
    db.docs[path] = {**db.docs[path], "contract_review": "enforce"}  # a direct write
    ps.install(ps.PlatformSwitches(db, mac=SettingsMac(keys=(key,))))
    with caplog.at_level("WARNING"):
        assert wc.enabled() is True
        assert contractreview.mode() == "report"
    assert sum("platform_switch_unsigned" in r.getMessage() for r in caplog.records) == 1
    # Under another key nothing verifies either.
    ps.install(ps.PlatformSwitches(db, mac=SettingsMac(keys=(new_key(),))))
    assert contractreview.mode() == "report"


def test_a_write_over_a_document_that_does_not_verify_signs_no_planted_value(db, key, terraform):
    path = f"{ps.COLLECTION}/{ps.DOCUMENT}"
    db.docs[path] = {"work_contracts": "off", "contract_review": "enforce", "version": 3}
    writer = ps.PlatformSwitches(db, mac=SettingsMac(keys=(key,)))
    writer.set({"work_contracts": "on"}, version=3, by="root@example.com")
    stored = db.docs[path]
    assert stored["contract_review"] is None, "the planted value was carried into the MAC"
    assert stored["work_contracts"] == "on" and _reference_verifies(key, stored)


def test_a_store_that_cannot_be_read_falls_back_to_terraform(terraform):
    class Broken:
        def collection(self, name: str):
            raise RuntimeError("unavailable")

    ps.install(ps.PlatformSwitches(Broken(), mac=SettingsMac(keys=(new_key(),))))
    assert wc.enabled() is True and contractreview.mode() == "report"


def test_with_no_reader_installed_the_readers_are_the_environment_as_before(monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    assert wc.enabled() is True
    assert wc.enabled({}) is False
    assert contractreview.mode({contractreview.MODE_ENV: "Enforce "}) == "enforce"
    assert contractreview.mode({contractreview.MODE_ENV: "loud"}) == "off"
