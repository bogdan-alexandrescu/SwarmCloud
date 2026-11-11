"""The `api-key` one-time link (docs/design/llm-providers.md §4.1, lane LP-A1).

A platform admin mints a link naming the vendor, the label and the workspace
that will own the key, for one person's verified email. That person types the
key into the console's form once; swarm-api forwards it to the broker's API key
registration (`POST /v1/accounts`, a pool key) and keeps no copy. The link is
single use and lasts ten minutes. A key the broker refuses leaves the link
unused and is answered with a fixed reason. The key never reaches an answer, a
log record or a stored document.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta

import pytest

from swarm_api.brokerclient import AccountPoolUnavailable, BrokerClient
from swarm_api.errors import ValidationFailed
from swarm_api.setup_links import API_ACCOUNT, API_KEY_TTL, COLLECTION, digest

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ALICE,
    CAROL,
    ROOT,
    create_team,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

LINKS = "/v1/admin/setup/links"
REDEEM = "/v1/setup/links/redeem"


def _team(client) -> str:
    response = create_team(client)
    assert response.status_code in (200, 202), response.text
    return response.json()["team"]


def _mint(client, *, provider: str = "anthropic-api", label: str = "team-key",
          owner: str = ALICE, as_user: str = "root", **extra):
    return client.post(LINKS, json={"kind": "api-key", "label": label, "owner": owner,
                                    "provider": provider, **extra},
                       headers=auth_header(as_user))


def _token(response) -> str:
    assert response.status_code == 201, response.text
    return response.json()["path"].rsplit("/", 1)[1]


def _key(provider: str = "anthropic-api") -> str:
    # Built at runtime, never one literal (CLAUDE.md: nothing may look like a
    # credential).
    prefix = "sk-" + ("ant-api03-" if provider == "anthropic-api" else "proj-")
    return prefix + secrets.token_hex(32)


def _record(db, token: str) -> dict:
    return db.docs[f"{COLLECTION}/{digest(token)}"]


def _everything_logged(records: list[logging.LogRecord]) -> str:
    """Every captured record, its formatted message AND its raw fields
    (args, `extra`), so a value passed as structured data is caught too."""
    return "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in records)


def test_mint_names_vendor_label_and_workspace_for_ten_minutes(setup_client, db) -> None:
    team = _team(setup_client)
    minted = _mint(setup_client, workspace=team)
    token = _token(minted)
    record = _record(db, token)
    assert record["kind"] == API_ACCOUNT == "api-key"
    assert record["owner"] == ALICE and record["tenant_id"] == team
    assert record["provider"] == "anthropic-api" and record["label"] == "team-key"
    assert record["share_with_voice_it"] is False
    assert record["expires_at"] - record["created_at"] == timedelta(minutes=10) == API_KEY_TTL
    body = minted.json()
    assert body["kind"] == "api-key" and body["provider"] == "anthropic-api"
    assert body["tenant"] == team and body["owner"] == ALICE


def test_mint_without_a_workspace_files_the_key_under_the_admins_own(setup_client, db) -> None:
    token = _token(_mint(setup_client, provider="openai-api", owner=ROOT))
    record = _record(db, token)
    assert record["tenant_id"] and record["provider"] == "openai-api"


def test_mint_is_a_platform_admins_and_names_a_vendor_and_a_real_workspace(setup_client,
                                                                          db) -> None:
    by_alice = _mint(setup_client, as_user="alice")
    assert by_alice.status_code == 403, by_alice.text
    no_vendor = setup_client.post(LINKS, json={"kind": "api-key", "label": "team-key",
                                               "owner": ALICE}, headers=auth_header("root"))
    assert no_vendor.status_code == 422, no_vendor.text
    other_vendor = _mint(setup_client, provider="openai")
    assert other_vendor.status_code == 422, other_vendor.text
    nowhere = _mint(setup_client, workspace="no-such-workspace")
    assert nowhere.status_code == 404, nowhere.text
    # A vendor or workspace on another kind is refused, not ignored.
    stray = setup_client.post(LINKS, json={"kind": "addon-secret", "label": "tailscale",
                                           "owner": ALICE, "provider": "anthropic-api"},
                              headers=auth_header("root"))
    assert stray.status_code == 422, stray.text
    assert not [path for path in db.docs if path.startswith(f"{COLLECTION}/")]


def test_mint_then_redeem_calls_the_broker_once_and_returns_no_key(setup_client, db, pool,
                                                                  caplog) -> None:
    caplog.set_level(logging.DEBUG)
    team = _team(setup_client)
    token = _token(_mint(setup_client, workspace=team, share_with_voice_it=True))
    key = _key()
    opened = setup_client.post(REDEEM, json={"token": token, "value": key},
                               headers=auth_header("alice"))
    assert opened.status_code == 200, opened.text
    assert pool.registered == [{"owner_tenant": team, "label": "team-key",
                                "provider": "anthropic-api", "credential": key,
                                "added_by": ALICE, "share_with_voice_it": True}]
    body = opened.json()
    assert body == {"kind": "api-key", "label": "team-key", "tenant": team,
                    "provider": "anthropic-api", "account_id": f"{team}:team-key",
                    "written": True}
    assert key not in opened.text
    assert key not in _everything_logged(caplog.records)
    assert key not in json.dumps(db.docs, default=str)
    assert _record(db, token)["used_at"] is not None


def test_a_second_redeem_is_refused(setup_client, db, pool) -> None:
    token = _token(_mint(setup_client, workspace=_team(setup_client)))
    first = setup_client.post(REDEEM, json={"token": token, "value": _key()},
                              headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    key = _key()
    second = setup_client.post(REDEEM, json={"token": token, "value": key},
                               headers=auth_header("alice"))
    assert second.status_code == 404, second.text
    assert key not in second.text
    assert len(pool.registered) == 1


def test_an_expired_link_is_refused(setup_client, db, pool) -> None:
    token = _token(_mint(setup_client, workspace=_team(setup_client)))
    record = _record(db, token)
    record["expires_at"] = datetime.now(record["created_at"].tzinfo) - timedelta(seconds=1)
    expired = setup_client.post(REDEEM, json={"token": token, "value": _key()},
                                headers=auth_header("alice"))
    assert expired.status_code == 404, expired.text
    assert pool.registered == []
    assert record["used_at"] is None


@pytest.mark.parametrize("user", ["root", "carol"])
def test_only_the_named_person_opens_it(setup_client, db, pool, user) -> None:
    token = _token(_mint(setup_client, workspace=_team(setup_client)))
    other = setup_client.post(REDEEM, json={"token": token, "value": _key()},
                              headers=auth_header(user))
    assert other.status_code == 403, other.text
    assert pool.registered == []
    assert _record(db, token)["used_at"] is None


def test_without_its_key_the_link_stays_unused(setup_client, db, pool) -> None:
    token = _token(_mint(setup_client, workspace=_team(setup_client)))
    bare = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert bare.status_code == 422, bare.text
    assert bare.json()["detail"]["expires_at"]
    assert pool.registered == [] and _record(db, token)["used_at"] is None


@pytest.mark.parametrize("refusal", [
    ValidationFailed("an Anthropic API key begins with 'sk-ant-'; this is not one"),
    ValidationFailed("the provider refused this key; nothing was stored"),
])
def test_a_broker_refusal_leaves_the_link_unredeemed(setup_client, db, pool, caplog,
                                                     refusal) -> None:
    caplog.set_level(logging.DEBUG)
    team = _team(setup_client)
    token = _token(_mint(setup_client, workspace=team))
    calls: list[str] = []
    accept = pool.register_api_key

    def refuse(**kwargs):
        calls.append(kwargs["credential"])
        raise refusal

    pool.register_api_key = refuse
    pasted = _key()
    refused = setup_client.post(REDEEM, json={"token": token, "value": pasted},
                                headers=auth_header("alice"))
    assert refused.status_code == 422, refused.text
    answer = refused.json()
    assert answer["code"] == "validation_failed"
    assert answer["detail"]["reason"] == "api_key_refused"
    assert answer["detail"]["expires_at"]
    assert refusal.message in answer["message"]
    assert pasted not in refused.text
    assert pasted not in _everything_logged(caplog.records)
    assert calls == [pasted]
    assert _record(db, token)["used_at"] is None
    # The link still opens: the right key goes through on it.
    pool.register_api_key = accept
    right = _key()
    opened = setup_client.post(REDEEM, json={"token": token, "value": right},
                               headers=auth_header("alice"))
    assert opened.status_code == 200, opened.text
    assert [r["credential"] for r in pool.registered] == [right]
    assert right not in opened.text


def test_an_unreachable_broker_leaves_the_link_unredeemed(setup_client, db, pool) -> None:
    token = _token(_mint(setup_client, workspace=_team(setup_client)))

    def down(**_):
        raise AccountPoolUnavailable("the account pool is unavailable")

    pool.register_api_key = down
    pasted = _key()
    answered = setup_client.post(REDEEM, json={"token": token, "value": pasted},
                                 headers=auth_header("alice"))
    assert answered.status_code == 503, answered.text
    assert pasted not in answered.text
    assert _record(db, token)["used_at"] is None


def test_a_key_shaped_value_never_reaches_a_log_record(setup_client, db, pool,
                                                      caplog) -> None:
    """Mint, a refused redeem, a stranger's redeem, a used link's redeem and
    the accepted one: no captured record carries the value, in its message or
    in its fields."""
    caplog.set_level(logging.DEBUG)
    team = _team(setup_client)
    token = _token(_mint(setup_client, workspace=team))
    key = _key()
    accept = pool.register_api_key

    def refuse(**_):
        raise ValidationFailed("the provider refused this key; nothing was stored")

    pool.register_api_key = refuse
    setup_client.post(REDEEM, json={"token": token, "value": key},
                      headers=auth_header("alice"))
    setup_client.post(REDEEM, json={"token": token, "value": key},
                      headers=auth_header("carol"))
    pool.register_api_key = accept
    assert setup_client.post(REDEEM, json={"token": token, "value": key},
                             headers=auth_header("alice")).status_code == 200
    setup_client.post(REDEEM, json={"token": token, "value": key},
                      headers=auth_header("alice"))
    assert caplog.records, "nothing was captured, so nothing was checked"
    assert key not in _everything_logged(caplog.records)
    assert key not in json.dumps(db.docs, default=str)


def test_a_link_for_one_workspace_never_registers_a_key_for_another(setup_client, db,
                                                                   pool) -> None:
    """Invariant 9: the owning workspace is the one recorded at mint. The
    redeemer cannot name another, and the key is sent with no lend."""
    team = _team(setup_client)
    token = _token(_mint(setup_client, workspace=team))
    for stray in ({"workspace": "u-alice"}, {"tenant_id": "u-alice"},
                  {"lend_to": ["u-alice"]}, {"lends": {"u-alice": {"max_agents": 1}}},
                  {"provider": "openai-api"}):
        refused = setup_client.post(REDEEM, json={"token": token, "value": _key(), **stray},
                                    headers=auth_header("alice"))
        assert refused.status_code == 422, (stray, refused.text)
    assert pool.registered == [] and _record(db, token)["used_at"] is None
    # The key is filed under the link's workspace, whoever opens it.
    opened = setup_client.post(REDEEM, json={"token": token, "value": _key()},
                               headers=auth_header("alice"))
    assert opened.status_code == 200, opened.text
    assert [r["owner_tenant"] for r in pool.registered] == [team]
    assert "lend_to" not in pool.registered[0] and "lends" not in pool.registered[0]


# -- the broker call itself, through the real client ---------------------------


class _Recorder:
    def __init__(self, status=201, body=None):
        self.calls: list[dict] = []
        self._status = status
        self._body = body if body is not None else {"account": {"account_id": "eng:k"}}

    def __call__(self, method, url, *, headers, body=None, timeout=30.0):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body.decode()) if body else None})
        return self._status, self._body


class _Token:
    def token(self, audience: str) -> str:
        return "t"


def _client(recorder) -> BrokerClient:
    return BrokerClient(base_url="https://broker.invalid", audience="https://broker.invalid",
                        transport=recorder, tokens=_Token())


def test_register_api_key_posts_a_pool_key_with_no_lend(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rec = _Recorder()
    key = _key("openai-api")
    _client(rec).register_api_key(owner_tenant="eng", label="k", provider="openai-api",
                                  credential=key, added_by=ALICE, share_with_voice_it=False)
    assert rec.calls == [{"method": "POST", "url": "https://broker.invalid/v1/accounts",
                          "body": {"owner_tenant": "eng", "label": "k",
                                   "provider": "openai-api", "credential": key,
                                   "added_by": ALICE, "voice_dedicated": False,
                                   "share_with_voice_it": False}}]
    assert key not in _everything_logged(caplog.records)


def test_register_api_key_answers_a_broker_refusal_as_the_callers_422() -> None:
    rec = _Recorder(status=422, body={"code": "validation_failed",
                                      "message": "the provider refused this key"})
    with pytest.raises(ValidationFailed):
        _client(rec).register_api_key(owner_tenant="eng", label="k", provider="anthropic-api",
                                      credential=_key(), added_by=ALICE)
