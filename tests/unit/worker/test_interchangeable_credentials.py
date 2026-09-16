"""A profile may declare INTERCHANGEABLE credentials, not a required set.

claude-code takes either metered API access (ANTHROPIC_API_KEY) or a Claude
subscription token (CLAUDE_CODE_OAUTH_TOKEN, from `claude setup-token`). A
tenant has exactly one. Requiring every declared name refused a JSON secret that
supplied precisely the credential the tenant pays for:

    SecretError: secret swarm-tenant-eng-anthropic does not supply
                 CLAUDE_CODE_OAUTH_TOKEN for provider anthropic

The deployed path hid it, because scripts/create-secrets.sh writes a BARE value
that fans out into every declared name. Only the documented JSON-object shape
broke, and only for profiles that declare alternatives.
"""

from __future__ import annotations

import json

import pytest

from agent_worker.secrets import SecretError
from agent_worker.secrets import resolve_credentials
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from datetime import datetime, timezone

TENANT = Tenant(
    tenant_id="eng", kind="group", principal="eng@saga.xyz",
    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), credentials=["anthropic"],
)
NAMES = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class _Client:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def access(self, name: str) -> str:
        return self._payload


class _Logger:
    def register_secret(self, *_a, **_k): ...
    def info(self, *_a, **_k): ...
    def warning(self, *_a, **_k): ...


def _resolve(payload: str, any_of: bool):
    return resolve_credentials(
        tenant=TENANT, provider="anthropic", secret_env_names=NAMES,
        any_of=any_of, client=_Client(payload), logger=_Logger(),
    )


def test_the_catalogue_marks_claude_code_credentials_interchangeable():
    p = RUNNER_PROFILES["claude-code"]
    assert p.secrets_any_of is True
    assert set(p.secrets) == set(NAMES)


def test_an_api_key_only_json_secret_is_accepted():
    env = _resolve(json.dumps({"ANTHROPIC_API_KEY": "sk-ant-api03-x"}), any_of=True).env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-x"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_subscription_only_json_secret_is_accepted():
    env = _resolve(json.dumps({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}), any_of=True).env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    assert "ANTHROPIC_API_KEY" not in env


def test_supplying_neither_is_still_refused():
    with pytest.raises(SecretError):
        _resolve(json.dumps({"SOMETHING_ELSE": "x"}), any_of=True)


def test_a_profile_needing_ALL_names_still_requires_all():
    """The any_of relaxation must not weaken a profile that genuinely needs a set
    (a key plus a base URL, say) -- that case must still fail loudly."""
    with pytest.raises(SecretError):
        _resolve(json.dumps({"ANTHROPIC_API_KEY": "sk-ant-api03-x"}), any_of=False)


def test_a_bare_value_still_fans_out_to_every_name():
    """What create-secrets.sh actually writes, and the deployed path today."""
    env = _resolve("sk-ant-oat01-x", any_of=True).env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-oat01-x"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
