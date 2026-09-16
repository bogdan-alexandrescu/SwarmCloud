"""Claude Code runs on a subscription token as well as a metered API key.

A tenant has ONE secret per provider (`swarm-tenant-<id>-anthropic`), and the
Cloud Run Job projects it into every variable the profile declares. Both
ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN therefore arrive holding the SAME
string, so choosing by name order would hand a subscription token to the
API-key variable and Claude Code would reject it.

The value disambiguates: `claude setup-token` mints `sk-ant-oat...`, metered
keys are `sk-ant-api...`.
"""

from __future__ import annotations

import pytest

from agent_worker.runners.cliagent import CliAgentSpec, _credential_env

SPEC = CliAgentSpec(
    name="claude-code",
    provider="anthropic",
    binary_env="CLAUDE_CODE_BIN",
    binary_default="claude",
    args_env="CLAUDE_CODE_ARGS",
    args_default=("--print",),
    key_env="ANTHROPIC_API_KEY",
    alt_key_envs=("CLAUDE_CODE_OAUTH_TOKEN",),
)


def test_a_metered_api_key_uses_the_api_key_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdef")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _credential_env(SPEC) == "ANTHROPIC_API_KEY"


def test_a_subscription_token_uses_the_oauth_variable(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdef")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _credential_env(SPEC) == "CLAUDE_CODE_OAUTH_TOKEN"


def test_the_same_secret_projected_into_both_still_picks_oauth(monkeypatch):
    """The real deployed shape: one secret, two variables, identical values.

    Picking by name order here would return ANTHROPIC_API_KEY holding an OAuth
    token, and every claude-code attempt for a subscription tenant would fail
    authentication with a credential that is perfectly valid.
    """
    token = "sk-ant-oat01-abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", token)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    assert _credential_env(SPEC) == "CLAUDE_CODE_OAUTH_TOKEN"


def test_the_same_secret_projected_into_both_still_picks_api_key(monkeypatch):
    key = "sk-ant-api03-abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", key)
    assert _credential_env(SPEC) == "ANTHROPIC_API_KEY"


def test_no_credential_at_all_is_reported(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _credential_env(SPEC) is None


def test_an_empty_value_is_not_a_credential(monkeypatch):
    """An empty secret version must read as absent, not as a usable credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _credential_env(SPEC) is None
