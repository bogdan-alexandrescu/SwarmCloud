"""The App's write mint for a workflow-touching push (#2029, WF-APP-TOKEN).

BGD, 2026-10-22: "can we use the github app creds for this?" A push whose new
commits touch `.github/workflows/**` or `.github/actions/**` is made with an
installation token swarm-api mints at push time. What these tests hold:

  * one mint, for ONE repository, with `contents: write` and
    `workflows: write` exactly, from the tenant's OWN installation;
  * a second tenant that enabled the same owner never gets the first
    tenant's installation (invariant 9), and an owner reached through a
    person's token (`method: pat`) has none;
  * never cached: two pushes are two mints, and the read cache never hands
    the write token out (nor does the read mint ever ask for write);
  * each failure is typed: no installation, Workflows not granted, the
    repository not covered, anything else;
  * the token never reaches a log line or an exception's text.

Every token is built at runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import forge_installation, forgeapp
from swarm_api.forge_installation import (
    READ_PERMISSIONS, WORKFLOW_PUSH_PERMISSIONS, InstallationTokens, WorkflowMintRefused,
)

T0 = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)
ENG_INSTALLATION = 4401
RESEARCH_INSTALLATION = 4402


def _value() -> str:
    return secrets.token_hex(20)


class Mints:
    """`POST /app/installations/{id}/access_tokens`, every call recorded."""

    def __init__(self, *, status: int = 201, message: str = "",
                 raises: Exception | None = None) -> None:
        self.status = status
        self.message = message
        self.raises = raises
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.issued: list[str] = []

    def __call__(self, method, url, headers, body, timeout) -> forgeapp.HttpAnswer:
        self.calls.append((method, url, json.loads(body or b"{}")))
        if self.raises is not None:
            raise self.raises
        if self.status != 201:
            return forgeapp.HttpAnswer(self.status, {}, json.dumps(
                {"message": self.message}).encode("utf-8"))
        token = _value()
        self.issued.append(token)
        expires = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return forgeapp.HttpAnswer(201, {}, json.dumps(
            {"token": token, "expires_at": expires}).encode("utf-8"))


def _enable(db, tenant_id: str, owner: str, installation: int | None, *,
            method: str | None = None, user: str = "alice@example.com") -> None:
    doc: dict[str, Any] = {
        "tenant_id": tenant_id, "user": user, "owner": owner.lower(), "owner_login": owner,
        "installation_id": installation,
        "install_state": "installed" if method is None else "token",
    }
    if method is not None:
        doc["method"] = method
    db.collection(forge_installation.ORGS).document(
        f"{tenant_id}__{forgeapp.user_hash(user)}__{owner.lower()}").set(doc)


def _tokens(db, mints: Mints) -> InstallationTokens:
    jwt = "jwt." + _value()
    return InstallationTokens(db, app_jwt=lambda: jwt, send=mints, clock=lambda: T0.timestamp())


def test_one_mint_for_one_repository_with_exactly_contents_and_workflows_write(db):
    _enable(db, "eng", "Example-Org", ENG_INSTALLATION)
    mints = Mints()
    token = _tokens(db, mints).workflow_push_token("eng", "example-org", "widgets")
    # MUTATION: return the read token here and this mints nothing new.
    assert token == mints.issued[-1]
    (method, url, body), = mints.calls
    assert method == "POST"
    # MUTATION: mint from any installation but the tenant's own and this url differs.
    assert url.endswith(f"/app/installations/{ENG_INSTALLATION}/access_tokens")
    # MUTATION: drop `repositories` and the token reaches every repository installed.
    assert body["repositories"] == ["widgets"]
    # MUTATION: add a permission (or ask `contents: read`) and this is not equal.
    assert body["permissions"] == {"contents": "write", "workflows": "write"}
    assert dict(WORKFLOW_PUSH_PERMISSIONS) == {"contents": "write", "workflows": "write"}


def test_a_second_tenant_enabling_the_same_owner_never_gets_the_first_tenants_installation(db):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    _enable(db, "research", "example-org", RESEARCH_INSTALLATION, user="bob@example.com")
    mints = Mints()
    tokens = _tokens(db, mints)
    tokens.workflow_push_token("research", "example-org", "widgets")
    # MUTATION: drop the tenant filter in `installation_for` and research's mint
    # can go to eng's installation.
    assert [url for _m, url, _b in mints.calls] == [
        f"{forge_installation.API}/app/installations/{RESEARCH_INSTALLATION}/access_tokens"]


def test_a_tenant_with_no_installation_of_the_owner_mints_nothing_even_when_another_has_one(db):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    mints = Mints()
    with pytest.raises(WorkflowMintRefused) as refused:
        _tokens(db, mints).workflow_push_token("research", "example-org", "widgets")
    # MUTATION: fall back to any tenant's installation and this mints.
    assert refused.value.reason == forge_installation.MINT_NO_INSTALLATION
    assert mints.calls == []


def test_an_owner_reached_through_a_persons_token_has_no_installation(db):
    _enable(db, "eng", "example-org", ENG_INSTALLATION, method="pat")
    mints = Mints()
    with pytest.raises(WorkflowMintRefused) as refused:
        _tokens(db, mints).workflow_push_token("eng", "example-org", "widgets")
    # MUTATION: drop the `method: pat` check and this mints on a person's grant.
    assert refused.value.reason == forge_installation.MINT_NO_INSTALLATION
    assert mints.calls == []


def test_the_write_token_is_never_cached_and_the_read_cache_never_hands_it_out(db):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    mints = Mints()
    tokens = _tokens(db, mints)
    first = tokens.workflow_push_token("eng", "example-org", "widgets")
    second = tokens.workflow_push_token("eng", "example-org", "widgets")
    # MUTATION: hold the write token in `_held` and the second push reuses it.
    assert first != second and len(mints.calls) == 2
    read = tokens.token_for_repo("eng", "example-org", "widgets")
    # MUTATION: share the cache between the mints and the read answers a write token.
    assert read not in (first, second)
    assert len(mints.calls) == 3
    # MUTATION: let the read mint ask for write and this is not READ_PERMISSIONS.
    assert mints.calls[-1][2]["permissions"] == dict(READ_PERMISSIONS)
    assert all(level == "read" for level in mints.calls[-1][2]["permissions"].values())
    # And a held read token does not satisfy a write mint either.
    third = tokens.workflow_push_token("eng", "example-org", "widgets")
    assert third not in (first, second, read) and len(mints.calls) == 4
    assert mints.calls[-1][2]["permissions"] == {"contents": "write", "workflows": "write"}


@pytest.mark.parametrize("status,message,reason", [
    (422, "The permissions requested are not granted to this installation.",
     forge_installation.MINT_NOT_GRANTED),
    (403, "Resource not accessible by integration: permissions",
     forge_installation.MINT_NOT_GRANTED),
    (422, "There is at least one repository that does not exist or is not accessible "
          "to the parent installation.", forge_installation.MINT_NOT_COVERED),
    (500, "", forge_installation.MINT_FAILED),
    (404, "Not Found", forge_installation.MINT_FAILED),
])
def test_each_refused_mint_is_typed(db, status, message, reason):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    with pytest.raises(WorkflowMintRefused) as refused:
        _tokens(db, Mints(status=status, message=message)).workflow_push_token(
            "eng", "example-org", "widgets")
    # MUTATION: map every 422 to one reason and the "not covered" case is red.
    assert refused.value.reason == reason
    assert refused.value.status == status


def test_a_transport_error_is_a_failure_and_names_no_value(db, caplog):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    leaked = "ghs_" + _value()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(WorkflowMintRefused) as refused:
            _tokens(db, Mints(raises=RuntimeError(f"connection reset {leaked}"))) \
                .workflow_push_token("eng", "example-org", "widgets")
    assert refused.value.reason == forge_installation.MINT_FAILED
    # MUTATION: put the transport's message in the refusal and the value is in it.
    assert leaked not in str(refused.value) and leaked not in caplog.text


def test_the_minted_token_and_the_jwt_never_reach_a_log_line(db, caplog):
    _enable(db, "eng", "example-org", ENG_INSTALLATION)
    mints = Mints()
    jwt = "jwt." + _value()
    tokens = InstallationTokens(db, app_jwt=lambda: jwt, send=mints,
                                clock=lambda: T0.timestamp())
    with caplog.at_level(logging.DEBUG):
        token = tokens.workflow_push_token("eng", "example-org", "widgets")
    # MUTATION: log the minted answer and the token is in the captured output.
    assert token not in caplog.text and jwt not in caplog.text
    assert "workflow push token minted" in caplog.text
    # MUTATION: keep the write token in the read cache and it is in `_held`.
    assert token not in repr(tokens.__dict__) and all(
        held.token != token for held in tokens._held.values())
