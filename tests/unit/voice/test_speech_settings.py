"""Voice it › Settings: catalogue, store and routes (docs/design/voice-it-own-speech.md §11).

Names only (a machine type, image, command or path as text is 422 with the
field named), platform admins only for a write, a stale version is 409,
pending versus applied, and the OpenAI key's status served without the key.
The router is tested directly, on its own app: Firestore is the in-memory
fake, the broker a fake pool, no cloud credential and no emulator. Every
fake secret is built at runtime with `secrets`.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.identity import Principal  # noqa: E402

from swarm_api import setup_links as sl  # noqa: E402
from swarm_api.auth import AuthContext  # noqa: E402
from swarm_api.deps import current_auth, get_context  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import voice_settings as routes  # noqa: E402
from swarm_api.routes.accounts import account_pool  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import settings as vsettings  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"   # a Voice it person, not an admin
ROOT = "root@example.com"     # a platform admin, not a Voice it person
CAROL = "carol@example.com"   # neither


class FakePool:
    def __init__(self, answer: Any = None, *, fails: bool = False) -> None:
        self.answer = answer if answer is not None else {"account": None,
                                                         "reason": vs.NO_LIVE_ACCOUNT}
        self.fails = fails
        self.asked: list[str] = []

    def voice_pool(self, person: str) -> dict[str, Any]:
        self.asked.append(person)
        if self.fails:
            raise OSError("broker unreachable")
        return self.answer


def _app(db: FakeFirestore, pool: FakePool, *, zones=("us-central1-a",)) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.state.speech_zones = zones

    @app.exception_handler(ApiError)
    async def _error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    ctx = SimpleNamespace(db=db, now=lambda: NOW,
                          settings=SimpleNamespace(voice_people=(ALICE,)))

    def _auth(request: Request) -> AuthContext:
        email = request.headers.get("x-test-user", CAROL)
        return AuthContext(principal=Principal(email=email, subject=email, domain="example.com"),
                           tenant_id="eng", is_admin=email == ROOT)

    app.dependency_overrides[current_auth] = _auth
    app.dependency_overrides[get_context] = lambda: ctx
    app.dependency_overrides[account_pool] = lambda: pool
    return TestClient(app)


def _as(user: str) -> dict[str, str]:
    return {"x-test-user": user}


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def client(db, pool) -> TestClient:
    return _app(db, pool)


def _doc(db: FakeFirestore) -> dict[str, Any] | None:
    return db.docs.get(f"{vsettings.COLLECTION}/{vsettings.DOCUMENT}")


def _audits(db: FakeFirestore) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{vs.AUDIT}/")]


# ---- the catalogue -----------------------------------------------------------


def test_the_catalogue_starts_with_the_single_l4_machines_and_a_price_per_hour(client):
    r = client.get("/v1/voice/settings/catalogue", headers=_as(ALICE))

    assert r.status_code == 200, r.text
    machines = r.json()["machines"]
    assert [m["name"] for m in machines][0] == "g2-standard-4"
    assert machines[0]["usd_per_hour"] == 0.71
    for m in machines:
        assert re.fullmatch(r"g2-standard-\d+", m["name"]), m
        assert (m["gpu"], m["gpus"]) == ("nvidia-l4", 1), m
        assert m["usd_per_hour"] > 0 and m["price_read_at"] == cat.READ_AT
    prices = [m["usd_per_hour"] for m in machines]
    assert prices == sorted(prices)
    assert r.json()["defaults"] == {**cat.DEFAULTS, "zone": "us-central1-a"}


def test_every_model_is_a_named_entry_pinned_to_a_revision_and_its_weights_hashes(client):
    body = client.get("/v1/voice/settings/catalogue", headers=_as(ALICE)).json()

    for model in body["stt_models"] + body["tts_models"]:
        assert re.fullmatch(r"[0-9a-f]{40}", model["revision"]), model
        assert model["files"] and all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
                                      for f in model["files"])
    kokoro = body["tts_models"][0]
    assert kokoro["default_voice"] in kokoro["voices"]
    assert {f"voices/{v}.pt" for v in kokoro["voices"]} <= {f["path"] for f in kokoro["files"]}


def test_the_own_speech_engine_names_no_openai_model(client):
    body = client.get("/v1/voice/settings/catalogue", headers=_as(ALICE)).json()

    assert {m["provider"] for m in body["conversation_models"]} == {"anthropic"}
    assert body["defaults"]["engine"] == cat.ENGINE_OWN
    assert {e["name"] for e in body["engines"]} == {cat.ENGINE_OWN, cat.ENGINE_GPT_LIVE}


def test_the_conversation_model_is_a_curated_list_of_the_models_the_platform_has(client):
    """The owner's answer (2026-11-05): "selected from a curated list that we
    have available". Every entry is a Claude model the platform prices, so
    each pick runs on the pool and is costed; Haiku 4.5 is the default."""
    from swarm_api import modelprices

    body = client.get("/v1/voice/settings/catalogue", headers=_as(ALICE)).json()
    listed = body["conversation_models"]

    assert len(listed) > 1
    assert [m["name"] for m in listed][0] == body["defaults"]["conversation_model"]
    assert body["defaults"]["conversation_model"] == "claude-haiku-4-5"
    for m in listed:
        assert m["name"] in modelprices.SEED, m
        assert m["model"].startswith(m["name"]) and m["label"], m


def test_an_admin_picks_another_listed_conversation_model_and_an_unlisted_one_is_refused(
        client, db):
    picked = client.patch("/v1/voice/settings",
                          json={"version": 0, "conversation_model": "claude-sonnet-5"},
                          headers=_as(ROOT))
    assert picked.status_code == 200, picked.text
    assert picked.json()["settings"]["conversation_model"] == "claude-sonnet-5"
    # Priced by the platform, but not on the curated list.
    refused = client.patch("/v1/voice/settings",
                           json={"version": 1, "conversation_model": "claude-opus-5-5"},
                           headers=_as(ROOT))
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["field"] == "conversation_model"


#: Terraform's `local.runner_models`, as `model_class_env` hands it to swarm-api.
PINS = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1", "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5"}


def test_the_planner_model_is_the_pools_model_classes_and_the_planning_class_by_default(
        client, monkeypatch):
    """BGD, 2026-11-05: the planner runs the planning class, "make it
    configurable as well". The names are the frozen MODEL_CLASSES, each shown
    with the pin Terraform's runner_models gives it; never a model id."""
    from swarm_common.profiles import MODEL_CLASSES

    monkeypatch.setenv("WORKER_CLASS_MODELS", json.dumps(PINS))
    body = client.get("/v1/voice/settings/catalogue", headers=_as(ALICE)).json()

    listed = body["planner_models"]
    assert [m["name"] for m in listed] == list(MODEL_CLASSES)
    assert {m["name"]: m["model"] for m in listed} == PINS
    assert all(m["label"] for m in listed)
    assert body["defaults"]["planner_model"] == "fable"
    settings = client.get("/v1/voice/settings", headers=_as(ALICE)).json()
    assert settings["settings"]["planner_model"] == "fable"


@pytest.mark.parametrize("value", ["claude-opus-5-5", "claude-fable-5-1", "gpt-5", "mythos",
                                   "Fable", "", 5])
def test_a_planner_model_outside_the_classes_is_refused(client, db, value):
    r = client.patch("/v1/voice/settings", json={"version": 0, "planner_model": value},
                     headers=_as(ROOT))

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "planner_model"
    # Refused as a value off the list, which the answer names: the classes.
    assert r.json()["detail"]["choices"] == ["opus", "fable", "sonnet", "haiku"]
    assert _doc(db) is None and _audits(db) == []


def test_an_admin_picks_another_class_for_the_next_session_and_it_is_never_pending(client, db):
    service = vsettings.VoiceSettings(db, now=lambda: NOW, zones=("us-central1-a",))
    service.mark_applied(values=cat.defaults(("us-central1-a",)),
                         image="sha256:" + secrets.token_hex(32))

    r = client.patch("/v1/voice/settings", json={"version": 0, "planner_model": "opus"},
                     headers=_as(ROOT))

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["planner_model"] == "opus"
    # It changes no machine: the next session's start reads it.
    assert r.json()["pending"] == []
    assert service.session_planner_model(PINS) == "claude-opus-5-5"
    [audit] = _audits(db)
    assert audit["detail"]["changed"] == {"planner_model": {"from": "fable", "to": "opus"}}


def test_an_unset_planner_model_gives_the_session_no_model_so_swarm_voice_keeps_its_pin(db):
    service = vsettings.VoiceSettings(db, now=lambda: NOW, zones=("us-central1-a",))

    # No document at all, then a document that never set the field.
    assert service.session_planner_model(PINS) is None
    db.collection(vsettings.COLLECTION).document(vsettings.DOCUMENT).set(
        {"machine": "g2-standard-8", "version": 1})
    assert service.session_planner_model(PINS) is None


def test_a_planner_class_with_no_pin_or_a_name_no_longer_listed_falls_back_too(db):
    service = vsettings.VoiceSettings(db, now=lambda: NOW, zones=("us-central1-a",))
    ref = db.collection(vsettings.COLLECTION).document(vsettings.DOCUMENT)

    ref.set({"planner_model": "sonnet", "version": 1})
    assert service.session_planner_model(PINS) == "claude-sonnet-5-5"
    # The local loop pins no class, and a pin of the wrong class is never sent.
    assert service.session_planner_model({}) is None
    assert service.session_planner_model({**PINS, "sonnet": "claude-opus-5-5"}) is None
    ref.set({"planner_model": "mythos", "version": 2})
    assert service.session_planner_model(PINS) is None


def test_the_zones_are_the_deployments_machines_one_by_default():
    assert cat.configured_zones({}) == ("us-central1-a",)
    assert cat.configured_zones({cat.ZONES_ENV: " us-central1-b, ,us-central1-b,us-east4-c"}) == (
        "us-central1-b", "us-east4-c")


# ---- who may read and write ----------------------------------------------------


def test_reading_is_for_a_voice_it_person_or_an_admin(client):
    assert client.get("/v1/voice/settings", headers=_as(ALICE)).status_code == 200
    assert client.get("/v1/voice/settings", headers=_as(ROOT)).status_code == 200
    assert client.get("/v1/voice/settings", headers=_as(CAROL)).status_code == 403
    assert client.get("/v1/voice/settings/catalogue", headers=_as(CAROL)).status_code == 403


def test_a_missing_document_reads_as_the_catalogues_defaults(client):
    body = client.get("/v1/voice/settings", headers=_as(ALICE)).json()

    assert body["settings"] == {**cat.DEFAULTS, "zone": "us-central1-a"}
    assert body["version"] == 0 and body["applied"] is None
    assert body["pending"] == list(vsettings.MACHINE_FIELDS)
    assert body["zones"] == ["us-central1-a"]


def test_only_a_platform_admin_may_change_settings(client, db):
    r = client.patch("/v1/voice/settings", json={"version": 0, "machine": "g2-standard-8"},
                     headers=_as(ALICE))

    assert r.status_code == 403, r.text
    assert _doc(db) is None and _audits(db) == []


def test_an_admin_change_bumps_the_version_and_writes_its_audit_row(client, db):
    r = client.patch("/v1/voice/settings",
                     json={"version": 0, "machine": "g2-standard-8", "idle": "10m"},
                     headers=_as(ROOT))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["settings"]["machine"] == "g2-standard-8"
    assert body["updated_by"] == ROOT and body["updated_at"] == NOW.isoformat()
    [audit] = _audits(db)
    assert audit["action"] == vsettings.AUDIT_CHANGED and audit["by"] == ROOT
    assert audit["detail"]["changed"] == {
        "machine": {"from": "g2-standard-4", "to": "g2-standard-8"},
        "idle": {"from": "5m", "to": "10m"}}


# ---- names only ----------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), [
    ("machine", "a2-ultragpu-8g"),
    ("machine", "--machine-type=g2-standard-96"),
    ("stt_model", "Qwen/Qwen3-ASR-1.7B"),
    ("stt_model", "/var/lib/swarm-speech/models/mine"),
    ("tts_model", "https://example.com/weights.pth"),
    ("tts_voice", "af_sky; rm -rf /"),
    ("engine", "openai-realtime"),
    ("conversation_model", "gpt-4o"),
    ("idle", "600"),
    ("idle", 600),
    ("zone", "us-east4-c"),
])
def test_a_value_that_is_not_a_catalogue_name_is_refused_with_its_field(client, db, field, value):
    r = client.patch("/v1/voice/settings", json={"version": 0, field: value}, headers=_as(ROOT))

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == field
    assert _doc(db) is None and _audits(db) == []


@pytest.mark.parametrize("field", ["image", "command", "machine_type", "model_path", "url"])
def test_a_field_the_catalogue_does_not_define_is_refused_with_its_name(client, db, field):
    r = client.patch("/v1/voice/settings",
                     json={"version": 0, field: "us-docker.pkg.dev/x/y@sha256:" + "0" * 64},
                     headers=_as(ROOT))

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == field
    assert _doc(db) is None


def test_a_zone_with_a_machine_is_accepted(db, pool):
    client = _app(db, pool, zones=("us-central1-a", "us-central1-b"))

    r = client.patch("/v1/voice/settings", json={"version": 0, "zone": "us-central1-b"},
                     headers=_as(ROOT))

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["zone"] == "us-central1-b"


def test_an_empty_change_and_a_missing_version_are_refused(client):
    assert client.patch("/v1/voice/settings", json={"version": 0},
                        headers=_as(ROOT)).status_code == 422
    r = client.patch("/v1/voice/settings", json={"machine": "g2-standard-8"}, headers=_as(ROOT))
    assert r.status_code == 422 and r.json()["detail"]["field"] == "version"


def test_a_stored_name_the_catalogue_no_longer_lists_reads_as_the_default(client, db):
    db.collection(vsettings.COLLECTION).document(vsettings.DOCUMENT).set(
        {"machine": "n1-standard-96", "tts_voice": "zz_gone", "version": 3})

    body = client.get("/v1/voice/settings", headers=_as(ALICE)).json()

    assert body["settings"]["machine"] == "g2-standard-4"
    assert body["settings"]["tts_voice"] == "af_heart"
    assert body["version"] == 3


# ---- the version conflict --------------------------------------------------------


def test_a_stale_version_is_409_with_the_current_one_and_writes_nothing(client, db):
    assert client.patch("/v1/voice/settings", json={"version": 0, "machine": "g2-standard-8"},
                        headers=_as(ROOT)).status_code == 200

    r = client.patch("/v1/voice/settings", json={"version": 0, "machine": "g2-standard-12"},
                     headers=_as(ROOT))

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"field": "version", "version": 1}
    assert _doc(db)["machine"] == "g2-standard-8"
    assert len(_audits(db)) == 1


# ---- pending versus applied --------------------------------------------------------


def test_a_change_after_a_start_is_pending_until_the_next_start_applies_it(client, db):
    service = vsettings.VoiceSettings(db, now=lambda: NOW, zones=("us-central1-a",))
    applied = service.mark_applied(values=cat.defaults(("us-central1-a",)),
                                   image="sha256:" + secrets.token_hex(32))
    assert applied["pending"] == []

    body = client.patch("/v1/voice/settings",
                        json={"version": 0, "machine": "g2-standard-8", "engine": "gpt-live",
                              "tts_voice": "bm_george"},
                        headers=_as(ROOT)).json()

    # Only the machine is changed by a start; the engine and the voice take
    # effect at the next session or sentence.
    assert body["pending"] == ["machine"]
    assert body["applied"]["machine"] == "g2-standard-4"
    assert body["applied"]["at"] == NOW.isoformat()

    service.mark_applied(values=body["settings"], image=body["applied"]["image"])
    assert client.get("/v1/voice/settings", headers=_as(ALICE)).json()["pending"] == []


def test_marking_applied_refuses_a_name_the_catalogue_lacks(db):
    service = vsettings.VoiceSettings(db, now=lambda: NOW, zones=("us-central1-a",))

    with pytest.raises(ApiError):
        service.mark_applied(values={**cat.defaults(("us-central1-a",)), "machine": "a2-highgpu-1g"},
                             image="sha256:" + secrets.token_hex(32))
    assert _doc(db) is None


# ---- the OpenAI key's status ----------------------------------------------------------


def _link(db: FakeFirestore, *, owner: str, label: str, used_at, expires_at) -> None:
    db.collection(sl.COLLECTION).document(secrets.token_hex(32)).set({
        "kind": sl.VOICE_API_KEY, "label": label, "owner": owner, "tenant_id": "eng",
        "created_by": owner, "created_at": expires_at - sl.VOICE_KEY_TTL,
        "expires_at": expires_at, "used_at": used_at})


def test_a_set_key_is_served_as_who_set_it_and_when_never_the_key(db):
    key = "sk-" + secrets.token_hex(24)
    pool = FakePool({"account": {"account_id": "eng:voice-key", "label": "voice-key",
                                 "display_name": None, "provider": "openai-api",
                                 "credential": key},
                     "reason": ""})
    first = NOW - timedelta(days=3)
    _link(db, owner="dana@example.com", label="voice-key", used_at=first,
          expires_at=first + timedelta(minutes=10))
    _link(db, owner=ROOT, label="voice-key", used_at=NOW - timedelta(hours=1),
          expires_at=NOW - timedelta(minutes=55))

    r = _app(db, pool).get("/v1/voice/settings", headers=_as(ROOT))

    assert r.status_code == 200, r.text
    assert r.json()["openai_key"] == {"set": True, "by": ROOT,
                                      "at": (NOW - timedelta(hours=1)).isoformat(),
                                      "label": "voice-key",
                                      "link": {"open": False, "expires_at": None}}
    assert key not in r.text
    # An admin who is not a Voice it person is answered for the first one.
    assert pool.asked == [ALICE]


def test_the_brokers_record_of_who_added_the_key_wins_over_the_link(db):
    """The broker writes who added the key and when on the key's account; a
    link redeemed by someone else, later, does not change the answer."""
    added = NOW - timedelta(days=2)
    pool = FakePool({"account": {"account_id": "eng:voice-key", "label": "voice-key",
                                 "display_name": None, "provider": "openai-api",
                                 "key_added_by": "dana@example.com",
                                 "key_added_at": added.isoformat()},
                     "reason": ""})
    _link(db, owner=ROOT, label="voice-key", used_at=NOW - timedelta(hours=1),
          expires_at=NOW - timedelta(minutes=55))

    key = _app(db, pool).get("/v1/voice/settings", headers=_as(ROOT)).json()["openai_key"]

    assert (key["set"], key["by"], key["at"]) == (True, "dana@example.com", added.isoformat())


def test_no_key_is_served_as_unset_and_an_open_link_with_its_expiry(db):
    pool = FakePool({"account": None, "reason": vs.NO_LIVE_ACCOUNT})
    expires = NOW + timedelta(minutes=7)
    _link(db, owner=ROOT, label="voice-key", used_at=None, expires_at=expires)
    _link(db, owner=ROOT, label="voice-key", used_at=None, expires_at=NOW - timedelta(minutes=1))

    body = _app(db, pool).get("/v1/voice/settings", headers=_as(ALICE)).json()

    assert body["openai_key"] == {"set": False, "by": None, "at": None, "label": None,
                                  "link": {"open": True, "expires_at": expires.isoformat()}}


def test_a_broker_that_cannot_be_asked_reads_the_key_as_unknown(db):
    body = _app(db, FakePool(fails=True)).get("/v1/voice/settings", headers=_as(ALICE)).json()

    assert body["openai_key"]["set"] is None
    assert body["settings"]["engine"] == cat.ENGINE_OWN


def test_a_key_held_by_a_session_still_reads_as_set():
    status = vsettings.key_status({"account": None, "reason": vsettings.KEY_BUSY}, [], now=NOW)

    assert status["set"] is True and status["by"] is None
