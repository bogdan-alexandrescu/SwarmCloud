"""swarm-api mints the attempt egress ticket (docs/browser-egress.md §2.2, §2.5,
§3.1, §6.1; lane EGRESS-SVC, part of #1030).

`POST /v1/attempts/{attempt_id}/egress-ticket`, called by the worker with the
attempt-key signature it already uses for `/live-session`, with the same
fence checks. It mints only when the platform switch is on AND the tenant is
listed AND the profile is listed and a live-browser profile; the default is
off. Otherwise the worker is told why (409 `egress_off` with a cause) and
the attempt runs direct for its whole life.

Drives the real route over the in-memory Firestore with the helpers of
test_live_api.py. Every key and token is built at runtime.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

import pytest

from swarm_api.egress import egress_ticket_message, verify_egress_ticket
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
EGRESS_KEY = "-".join(("egress", "ticket", "key")) + "-" + secrets.token_hex(16)
PATH = f"/v1/attempts/{ATTEMPT}/egress-ticket"

ON = {
    "egress_key": EGRESS_KEY,
    "browser_egress_enabled": True,
    "browser_egress_tenants": ("eng",),
    "browser_egress_profiles": ("claude-code-browser",),
}


def sign(attempt: Attempt, body: bytes, *, path: str = PATH) -> dict[str, str]:
    stamp = str(int(time.time()))
    sig = attempt.private.sign(egress_ticket_message("POST", path, body, stamp))
    return {"X-Swarm-Attempt-Proof": _b64url(sig), "X-Swarm-Attempt-Timestamp": stamp}


def ask(client, attempt: Attempt, *, signed: bool = True, **over: Any):
    body = attempt.body(**over)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    if signed:
        headers.update(sign(attempt, body))
    return client.post(PATH, content=body, headers=headers)


def claims(ticket: str) -> dict[str, Any]:
    payload = ticket.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@pytest.fixture
def make(ldb, people, groups, objects, clock, relay):
    def _make(**settings: Any):
        return make_client(ldb, people, groups, objects, clock, relay, **settings)
    return _make


def test_the_switch_is_off_by_default(make, ldb):
    assert ApiSettings.__dataclass_fields__["browser_egress_enabled"].default is False
    assert ApiSettings.__dataclass_fields__["browser_egress_tenants"].default == ()
    client = make(egress_key=EGRESS_KEY)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = ask(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "egress_off"
    assert "disabled" in response.json()["message"]


def test_a_switched_on_tenant_and_profile_get_a_ticket_scoped_to_the_attempt(make, ldb):
    client = make(**ON)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt)
    response = ask(client, attempt)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["route"] == "proxy" and body["country"] == "us"
    assert 60 <= body["expires_in"] <= 900
    got = claims(body["ticket"])
    assert (got["tenant_id"], got["task_id"], got["attempt_id"], got["generation"]) == (
        "eng", TASK, ATTEMPT, 2)
    assert got["exp"] <= int(time.time()) + body["expires_in"] + 1
    verified = verify_egress_ticket([EGRESS_KEY], body["ticket"], now=time.time())
    assert verified["attempt_id"] == ATTEMPT


@pytest.mark.parametrize("settings,cause", [
    ({**ON, "browser_egress_enabled": False}, "disabled"),
    ({**ON, "browser_egress_tenants": ("research",)}, "tenant_off"),
    ({**ON, "browser_egress_profiles": ("claude-code",)}, "profile_off"),
])
def test_each_switch_must_be_on(make, ldb, settings, cause):
    client = make(**settings)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = ask(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "egress_off"
    assert cause in response.json()["message"]


def test_a_listed_profile_that_is_not_a_live_browser_never_gets_a_ticket(make, ldb):
    client = make(**{**ON, "browser_egress_profiles": ("claude-code",)})
    attempt = Attempt()
    seed_running(client, ldb, attempt, profile="claude-code")
    response = ask(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] in ("egress_off", "live_not_available")


def test_without_the_attempt_proof_nothing_is_minted(make, ldb):
    client = make(**ON)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = ask(client, attempt, signed=False)
    assert response.status_code == 403
    assert "ticket" not in response.json()


def test_a_live_session_signature_is_not_an_egress_signature(make, ldb):
    client = make(**ON)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    body = attempt.body()
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    headers.update(attempt.sign(PATH, body))  # the live-session purpose
    assert client.post(PATH, content=body, headers=headers).status_code == 403


def test_a_stale_generation_is_fenced(make, ldb):
    client = make(**ON)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt)
    response = ask(client, attempt, generation=1)
    assert response.status_code in (403, 409)
    assert "ticket" not in response.json()


def test_a_task_that_is_not_running_is_fenced(make, ldb):
    client = make(**ON)
    attempt = Attempt()
    task = seed_running(client, ldb, attempt)
    task["state"] = "PARKED"
    response = ask(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "egress_fenced"


def test_no_egress_key_is_a_503_and_never_a_ticket(make, ldb):
    client = make(**{**ON, "egress_key": ""})
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = ask(client, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "egress_unavailable"


def test_the_ticket_life_is_clamped_to_what_swarm_egress_accepts(make, ldb):
    client = make(**{**ON, "egress_ticket_seconds": 86400})
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    response = ask(client, attempt)
    assert response.status_code == 201
    assert response.json()["expires_in"] == 900


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("BROWSER_EGRESS_ENABLED", "true")
    monkeypatch.setenv("BROWSER_EGRESS_TENANTS", "eng, research")
    monkeypatch.setenv("BROWSER_EGRESS_PROFILES", "claude-code-browser")
    monkeypatch.setenv("BROWSER_EGRESS_COUNTRY", "us")
    monkeypatch.setenv("EGRESS_TICKET_SECONDS", "300")
    settings = ApiSettings.from_env()
    assert settings.browser_egress_enabled is True
    assert settings.browser_egress_tenants == ("eng", "research")
    assert settings.browser_egress_profiles == ("claude-code-browser",)
    assert settings.egress_ticket_seconds == 300
    assert "egress_key" not in repr(settings) or EGRESS_KEY not in repr(settings)
