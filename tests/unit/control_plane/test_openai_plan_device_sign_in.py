"""A ChatGPT plan added from the browser with a device code (the owner, 11 November 2026).

A plan's PKCE sign-in redirects to the signing-in machine
(`localhost:1455`), so the console could not add one. The codex CLI's own
`codex login --device-auth` shows a one-time code to enter on the vendor's
page instead; the broker now runs that flow, and swarm-api forwards it for the
caller's own workspace.

WHAT IS PINNED HERE

  * start answers the user code, the verification address and the state, and
    nothing a caller could read back holds the device code: not the answer,
    not any route, not a log line;
  * poll before approval answers `pending`; after approval it files the plan
    exactly as a PKCE sign-in does (the pair in `-refresh`, the access token
    and account id in the published secret), once -- a second poll finds it
    filed and the vendor is not asked again; after expiry it answers
    `expired`;
  * another workspace's poll is refused before the vendor is asked, and the
    sign-in is left for its owner;
  * no token, device code or refresh token in any answer or log line.

The end-to-end tests drive the shipped swarm-api routes through the shipped
`BrokerClient`, whose transport hands the bytes to the shipped broker app.
Nothing leaves the process; every fake token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker import openai_plan
from quota_broker.accounts import secret_name
from quota_broker.credentials import REFRESH_SUFFIX

from .conftest import auth_header
from .test_account_provider_signin import TENANT, api, broker_app  # noqa: F401 -- fixtures
from .test_openai_pool_accounts import id_token

START = "/v1/accounts/device/start"
POLL = "/v1/accounts/device/poll"


class _DeviceEndpoint:
    """The vendor's device sign-in and token endpoint, without a network."""

    def __init__(self):
        self.device_auth_id = secrets.token_hex(12)
        self.user_code = f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        self.authorization_code = secrets.token_hex(10)
        self.code_verifier = secrets.token_hex(16)
        self.account_id = secrets.token_hex(6)
        self.answer = {
            "access_token": secrets.token_hex(16),
            "refresh_token": secrets.token_hex(16),
            "id_token": id_token(self.account_id),
            "expires_in": 3600,
        }
        self.approved = False
        self.started = 0
        self.polls = []
        self.redeemed = []

    def start_device(self):
        self.started += 1
        return {
            "device_auth_id": self.device_auth_id,
            "user_code": self.user_code,
            "interval": "5",
        }

    def poll_device(self, *, device_auth_id, user_code):
        self.polls.append((device_auth_id, user_code))
        if not self.approved:
            return None
        return {
            "authorization_code": self.authorization_code,
            "code_challenge": secrets.token_hex(8),
            "code_verifier": self.code_verifier,
        }

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        self.redeemed.append({"code": code, "verifier": verifier, "redirect_uri": redirect_uri})
        return dict(self.answer)

    def exchange(self, refresh_token):
        return dict(self.answer)

    def material(self) -> list[str]:
        return [
            self.device_auth_id,
            self.authorization_code,
            self.code_verifier,
            self.answer["access_token"],
            self.answer["refresh_token"],
            self.answer["id_token"],
        ]


@pytest.fixture()
def device(broker_app):  # noqa: F811
    endpoint = _DeviceEndpoint()
    broker_app.state.plan_token_endpoint = endpoint
    return endpoint


def _start(api, user="alice", label="plan"):  # noqa: F811
    r = api.post(START, headers=auth_header(user), json={"label": label})
    assert r.status_code == 200, r.text
    return r


def _pending(broker_app, state):  # noqa: F811
    return broker_app.state.broker.db.docs[f"account_auth/{state}"]


# -- start -------------------------------------------------------------------


def test_start_answers_the_code_the_address_and_the_state_and_nothing_else(
    api, broker_app, device, caplog  # noqa: F811
):
    caplog.set_level(logging.DEBUG)
    r = _start(api)

    body = r.json()
    assert body["user_code"] == device.user_code
    assert body["verification_uri"] == openai_plan.DEVICE_VERIFICATION_URI
    assert body["provider"] == "openai"
    assert body["state"]
    assert body["expires_in_seconds"] == 15 * 60
    assert body["interval_seconds"] == 5
    assert set(body) == {
        "state", "user_code", "verification_uri", "expires_in_seconds",
        "interval_seconds", "provider",
    }
    assert device.device_auth_id not in r.text
    assert device.device_auth_id not in caplog.text
    # swarm-api sent the caller's tenant and the plan provider, never a body's.
    assert api.bridge.sent(START) == [
        {"owner_tenant": TENANT, "label": "plan", "provider": "openai", "lend_to": []}
    ]
    # The device code is held server-side, keyed by the state, for its owner.
    held = _pending(broker_app, body["state"])
    assert held["device_auth_id"] == device.device_auth_id
    assert held["owner_tenant"] == TENANT


def test_no_route_reads_the_device_code_back(api, broker_app, device):  # noqa: F811
    state = _start(api).json()["state"]
    answers = [
        api.post(POLL, headers=auth_header("alice"), json={"state": state}),
        api.get("/v1/accounts", headers=auth_header("alice")),
    ]
    for answer in answers:
        assert device.device_auth_id not in answer.text


def test_a_claude_sign_in_has_no_device_code_at_the_broker(broker_app, device):  # noqa: F811
    from fastapi.testclient import TestClient

    r = TestClient(broker_app).post(
        START, json={"owner_tenant": TENANT, "label": "team", "provider": "anthropic"}
    )
    assert r.status_code == 422, r.text
    assert device.started == 0


# -- poll --------------------------------------------------------------------


def test_poll_before_approval_answers_pending_and_files_nothing(
    api, broker_app, device  # noqa: F811
):
    state = _start(api).json()["state"]

    r = api.post(POLL, headers=auth_header("alice"), json={"state": state})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert device.polls == [(device.device_auth_id, device.user_code)]
    assert device.redeemed == []
    assert broker_app.state.secret_store.versions == {}
    # swarm-api named the caller's tenant as the expected owner.
    assert api.bridge.sent(POLL) == [{"state": state, "expected_owner": TENANT}]


def test_poll_after_approval_files_the_plan_once_like_a_pkce_sign_in(
    api, broker_app, device, caplog  # noqa: F811
):
    caplog.set_level(logging.DEBUG)
    start = _start(api)
    state = start.json()["state"]
    pending = api.post(POLL, headers=auth_header("alice"), json={"state": state})
    device.approved = True

    filed = api.post(POLL, headers=auth_header("alice"), json={"state": state})

    assert filed.status_code == 200, filed.text
    body = filed.json()
    assert body["status"] == "filed"
    assert body["account"]["account_id"] == f"{TENANT}:plan"
    assert body["account"]["provider"] == "openai"
    assert body["account"]["owner_tenant"] == TENANT
    assert body["organisation"]["name"] == "ChatGPT"
    # Redeemed with the verifier the vendor returned, at the device redirect.
    assert device.redeemed == [
        {
            "code": device.authorization_code,
            "verifier": device.code_verifier,
            "redirect_uri": openai_plan.DEVICE_REDIRECT_URI,
        }
    ]
    # The storage rule of a PKCE sign-in: the pair is the broker's alone, the
    # worker's secret holds the access token and the account id.
    versions = broker_app.state.secret_store.versions
    pair = json.loads(versions[f"{secret_name(TENANT, 'plan')}{REFRESH_SUFFIX}"][-1])
    assert pair["refresh_token"] == device.answer["refresh_token"]
    assert pair["kind"] == openai_plan.KIND
    published = json.loads(versions[secret_name(TENANT, "plan")][-1])
    assert published == {
        "access_token": device.answer["access_token"],
        "account_id": device.account_id,
    }
    # The device code is gone from the held record once the plan is filed.
    assert "device_auth_id" not in _pending(broker_app, state)

    again = api.post(POLL, headers=auth_header("alice"), json={"state": state})

    assert again.status_code == 200, again.text
    assert again.json()["status"] == "filed"
    assert again.json()["account"]["account_id"] == f"{TENANT}:plan"
    # Filed once: the vendor was not asked again and no second version exists.
    assert len(device.polls) == 2 and len(device.redeemed) == 1
    assert len(versions[f"{secret_name(TENANT, 'plan')}{REFRESH_SUFFIX}"]) == 1
    accounts = api.get("/v1/accounts", headers=auth_header("alice")).json()
    rows = accounts["accounts"] if isinstance(accounts, dict) else accounts
    assert [a["account_id"] for a in rows if a["provider"] == "openai"] == [f"{TENANT}:plan"]

    for value in device.material():
        for answer in (start, pending, filed, again):
            assert value not in answer.text
        assert value not in caplog.text


def test_poll_after_expiry_answers_expired_and_never_asks_the_vendor(
    api, broker_app, device  # noqa: F811
):
    state = _start(api).json()["state"]
    _pending(broker_app, state)["created_at"] = datetime.now(timezone.utc) - timedelta(
        minutes=16
    )
    device.approved = True

    r = api.post(POLL, headers=auth_header("alice"), json={"state": state})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "expired"
    assert device.polls == [] and device.redeemed == []
    assert broker_app.state.secret_store.versions == {}
    # And it stays expired: the held device code was dropped.
    again = api.post(POLL, headers=auth_header("alice"), json={"state": state})
    assert again.json()["status"] == "expired"


def test_another_workspaces_poll_is_refused_and_the_sign_in_is_left_for_its_owner(
    api, broker_app, device  # noqa: F811
):
    state = _start(api).json()["state"]
    device.approved = True

    r = api.post(POLL, headers=auth_header("bob"), json={"state": state})

    assert r.status_code == 403, r.text
    assert device.polls == [] and device.redeemed == []
    assert broker_app.state.secret_store.versions == {}
    assert api.bridge.sent(POLL)[0]["expected_owner"] == "research"
    # The owner can still finish it.
    mine = api.post(POLL, headers=auth_header("alice"), json={"state": state})
    assert mine.json()["status"] == "filed", mine.text


def test_a_platform_poll_with_no_expected_owner_is_refused(broker_app, device):  # noqa: F811
    from fastapi.testclient import TestClient

    c = TestClient(broker_app, raise_server_exceptions=False)
    started = c.post(START, json={"owner_tenant": TENANT, "label": "plan", "provider": "openai"})
    assert started.status_code == 200, started.text
    device.approved = True

    r = c.post(POLL, json={"state": started.json()["state"]})

    assert r.status_code in (401, 403), r.text
    assert device.polls == []


# -- the vendor's wire, as read from the codex CLI ----------------------------


class _Wire:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, url, body):
        self.sent.append((url, body))
        return self.answers.pop(0)


def test_the_http_endpoint_speaks_the_device_wire_codex_speaks():
    device_auth_id, user_code = secrets.token_hex(12), secrets.token_hex(4)
    code, verifier = secrets.token_hex(8), secrets.token_hex(16)
    wire = _Wire([
        (200, {"device_auth_id": device_auth_id, "usercode": user_code, "interval": "5"}),
        (403, {}),
        (404, {}),
        (200, {"authorization_code": code, "code_challenge": "c", "code_verifier": verifier}),
    ])
    endpoint = openai_plan.HttpPlanTokenEndpoint(json_post=wire)

    started = endpoint.start_device()
    assert started == {"device_auth_id": device_auth_id, "user_code": user_code, "interval": 5}
    assert endpoint.poll_device(device_auth_id=device_auth_id, user_code=user_code) is None
    assert endpoint.poll_device(device_auth_id=device_auth_id, user_code=user_code) is None
    approved = endpoint.poll_device(device_auth_id=device_auth_id, user_code=user_code)
    assert approved == {"authorization_code": code, "code_verifier": verifier}
    assert wire.sent[0] == (
        openai_plan.DEVICE_USERCODE_ENDPOINT, {"client_id": openai_plan.CODEX_CLIENT_ID}
    )
    assert wire.sent[1] == (
        openai_plan.DEVICE_TOKEN_ENDPOINT,
        {"device_auth_id": device_auth_id, "user_code": user_code},
    )
    assert openai_plan.DEVICE_USERCODE_ENDPOINT == (
        "https://auth.openai.com/api/accounts/deviceauth/usercode"
    )
    assert openai_plan.DEVICE_TOKEN_ENDPOINT == (
        "https://auth.openai.com/api/accounts/deviceauth/token"
    )
    assert openai_plan.DEVICE_VERIFICATION_URI == "https://auth.openai.com/codex/device"
    assert openai_plan.DEVICE_REDIRECT_URI == "https://auth.openai.com/deviceauth/callback"


@pytest.mark.parametrize("status", [400, 429, 500])
def test_a_device_poll_the_vendor_refuses_is_an_error_that_names_only_the_status(status):
    device_auth_id = secrets.token_hex(12)
    wire = _Wire([(status, {"error": device_auth_id})])
    endpoint = openai_plan.HttpPlanTokenEndpoint(json_post=wire)

    with pytest.raises(openai_plan.CredentialError) as raised:
        endpoint.poll_device(device_auth_id=device_auth_id, user_code="X")
    assert str(status) in str(raised.value)
    assert device_auth_id not in str(raised.value)


def test_a_vendor_without_device_sign_in_is_said_plainly():
    endpoint = openai_plan.HttpPlanTokenEndpoint(json_post=_Wire([(404, {})]))
    with pytest.raises(openai_plan.CredentialError) as raised:
        endpoint.start_device()
    assert "not enabled" in str(raised.value)
