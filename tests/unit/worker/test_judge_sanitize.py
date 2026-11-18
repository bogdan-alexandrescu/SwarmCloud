"""What the Jev client sends, and what it never does (context-quality.md §5.4).

Every fake credential here is built at runtime: a literal one would be refused
by the worker's own publish scan before this file could land.
"""

from __future__ import annotations

import json

import pytest

from agent_worker.judge import sanitize


def _github_token() -> str:
    return "ghp_" + "A1b2C3d4E5" * 4  # 44 characters, the classic PAT shape


def _aws_key() -> str:
    return "AKIA" + "Q" * 8 + "7" * 8


def test_a_credential_shaped_excerpt_is_dropped_not_masked() -> None:
    token = _github_token()
    state = {"candidates": [
        {"i": 0, "kind": "symbol", "path": "apps/x/client.py", "symbol": "connect",
         "excerpt": f"def connect():\n    token = \"{token}\"\n"},
        {"i": 1, "kind": "symbol", "path": "apps/x/other.py", "symbol": "other",
         "excerpt": "def other():\n    return 1\n"},
    ]}
    out = sanitize.sanitize_state(state)
    first, second = out.state["candidates"]
    # The candidate survives as identity only: path and symbol, no excerpt.
    assert first == {"i": 0, "kind": "symbol", "path": "apps/x/client.py", "symbol": "connect"}
    assert second["excerpt"].startswith("def other")
    text = json.dumps(out.state)
    assert token not in text
    # Dropped, NOT masked: no mask marker and no fragment of the value either.
    assert "REDACTED" not in text and "***" not in text
    assert token[:12] not in text
    assert out.dropped == 1


@pytest.mark.parametrize("path", [
    ".env", "config/.env.production", "deploy/server.pem", "certs/tls.key", "id.p12",
    "terraform/environments/dev/dev.tfvars", "terraform.tfstate", "terraform.tfstate.backup",
    "ops/secrets/db.yaml", "app/credentials/gcp.json",
])
def test_a_denied_path_sends_its_name_only(path: str) -> None:
    state = {"candidates": [{"i": 3, "path": path, "symbol": "x",
                             "excerpt": "harmless looking content", "signature": "x()"}]}
    out = sanitize.sanitize_state(state)
    assert out.state["candidates"] == [{"i": 3, "path": path, "symbol": "x"}]
    assert out.denied == 1


def test_a_hard_stop_path_is_denied_like_a_secret_path() -> None:
    state = {"code": {"path": "infra/prod/main.tf", "window": "resource {}"}}
    out = sanitize.sanitize_state(state, hard_stop_paths=("infra/prod/",))
    assert out.state == {"code": {"path": "infra/prod/main.tf"}}
    assert sanitize.is_denied_path("infra/prod/main.tf", ("infra/prod/**",))
    assert not sanitize.is_denied_path("infra/dev/main.tf", ("infra/prod/",))


def test_an_ordinary_path_keeps_its_content() -> None:
    assert not sanitize.is_denied_path("apps/agent-worker/agent_worker/judge/client.py")
    assert not sanitize.is_denied_path("docs/keys.md")


def test_a_registered_secret_drops_the_string_it_is_in() -> None:
    secret = "reg-" + "z9" * 10
    state = {"task": {"type": "implement", "brief": f"use {secret} to log in"}}
    out = sanitize.sanitize_state(state, secrets=(secret,))
    assert out.state == {"task": {"type": "implement"}}
    assert secret not in json.dumps(out.state)


def test_a_credential_in_a_list_of_strings_drops_the_element() -> None:
    out = sanitize.sanitize_state({"requirements": ["ship it", f"key {_aws_key()}"]})
    assert out.state == {"requirements": ["ship it"]}


def test_a_bare_string_state_is_parsed_and_sanitised() -> None:
    raw = json.dumps({"finding": {"summary": "bad", "file": "a.py"},
                      "diff_hunk": f"+ token = '{_github_token()}'"})
    out = sanitize.sanitize_state(raw)
    assert out.state == {"finding": {"summary": "bad", "file": "a.py"}}


def test_a_plain_string_state_with_a_credential_leaves_nothing() -> None:
    out = sanitize.sanitize_state(f"the key is {_github_token()}")
    assert out.state is None


def test_environment_and_identity_keys_are_never_sent() -> None:
    state = {"issue": {"title": "t", "author": "someone", "email": "a@example.com"},
             "env": {"HOME": "/root"}, "transcript": "long"}
    out = sanitize.sanitize_state(state)
    assert out.state == {"issue": {"title": "t"}}
