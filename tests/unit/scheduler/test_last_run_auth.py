"""`/last-run` checks its caller like `/tick` and `/pubsub/push` (issue 57).

Cloud Run invoker IAM gated `/last-run` alone; the other two routes also check
WHICH service account called. Pinned here against the real `PushVerifier`, with
only google-auth's token check replaced, so no credentials are needed:

  L-1  No Authorization header, a non-bearer one, a token that fails
       verification or one minted for another service account: 401
       `unauthenticated`, and the body carries no report.
  L-2  The push service account's token: `{"report": ...}` exactly as before.
  L-3  `/tick` and `/pubsub/push` still refuse without a token and still
       drain with one.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.oauth2 import id_token as google_id_token

from scheduler.main import PushVerifier, create_app

PUSH_SA = "swarm-push@example.invalid"
AUDIENCE = "https://scheduler.example.invalid"


class _Report:
    def __init__(self, admitted: int) -> None:
        self.admitted = admitted

    def to_dict(self) -> dict[str, Any]:
        return {"admitted": self.admitted}


class _Scheduler:
    """Only what the three routes touch."""

    def __init__(self) -> None:
        self.drains = 0

    def drain(self) -> _Report:
        self.drains += 1
        return _Report(self.drains)


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A good token, one for another account, and google-auth faked over them."""
    good = secrets.token_hex(16)
    other = secrets.token_hex(16)
    emails = {good: PUSH_SA, other: "someone-else@example.invalid"}

    def verify(token: str, _request: Any, audience: str | None = None) -> dict[str, str]:
        assert audience == AUDIENCE
        if token not in emails:
            raise ValueError("bad token")
        return {"email": emails[token]}

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", verify)
    return {"good": good, "other": other, "bad": secrets.token_hex(16)}


@pytest.fixture
def app_and_scheduler() -> tuple[TestClient, _Scheduler]:
    scheduler = _Scheduler()
    app = create_app(scheduler)  # type: ignore[arg-type]
    app.state.verifier = PushVerifier(PUSH_SA, AUDIENCE)
    return TestClient(app), scheduler


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _refused(response: Any) -> None:
    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "unauthenticated"
    assert "report" not in body


def test_last_run_without_authorization_is_401_with_no_report(app_and_scheduler, tokens):
    client, scheduler = app_and_scheduler
    client.post("/tick", headers=_bearer(tokens["good"]))
    assert scheduler.drains == 1
    _refused(client.get("/last-run"))


@pytest.mark.parametrize("kind", ["bad", "other"])
def test_last_run_with_a_wrong_token_is_401_with_no_report(app_and_scheduler, tokens, kind):
    client, _ = app_and_scheduler
    client.post("/tick", headers=_bearer(tokens["good"]))
    _refused(client.get("/last-run", headers=_bearer(tokens[kind])))


def test_last_run_with_a_non_bearer_header_is_401(app_and_scheduler, tokens):
    client, _ = app_and_scheduler
    _refused(client.get("/last-run", headers={"Authorization": f"Basic {tokens['good']}"}))


def test_last_run_for_the_push_account_returns_the_report(app_and_scheduler, tokens):
    client, _ = app_and_scheduler
    before = client.get("/last-run", headers=_bearer(tokens["good"]))
    assert before.status_code == 200
    assert before.json() == {"report": None}

    client.post("/tick", headers=_bearer(tokens["good"]))
    after = client.get("/last-run", headers=_bearer(tokens["good"]))
    assert after.status_code == 200
    assert after.json() == {"report": {"admitted": 1}}


@pytest.mark.parametrize("path", ["/tick", "/pubsub/push"])
def test_tick_and_push_still_refuse_without_a_token(app_and_scheduler, tokens, path):
    client, scheduler = app_and_scheduler
    response = client.post(path, json={})
    assert response.status_code == 401
    assert response.json()["status"] == "unauthenticated"
    assert scheduler.drains == 0


@pytest.mark.parametrize("path", ["/tick", "/pubsub/push"])
def test_tick_and_push_still_drain_for_the_push_account(app_and_scheduler, tokens, path):
    client, scheduler = app_and_scheduler
    response = client.post(path, json={}, headers=_bearer(tokens["good"]))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert scheduler.drains == 1
