"""swarm-api's create_app refuses to start only when both sign-in lists are empty.

Who may sign in is the hosted domains in `settings.core.allowed_domains`
(ALLOWED_DOMAINS) plus the exact addresses in `settings.allowed_users`
(ALLOWED_USERS); the deployment names its own
(docs/design/generic-repository.md section 2). Either list alone is enough:
a one-person deployment has one allowed user and no domain, and must start
(owner's rule, 2026-11-07). Both empty must stop the service at startup with
a fixed message naming both variables, never serve with open authentication.

The frozen `Settings` refuses an empty tuple when it is built, so the test
empties a built one, the way a later default change could leave it.
No credentials, no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import SIGN_IN_LIST_REQUIRED, create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, core_settings

_SPEC = importlib.util.spec_from_file_location(
    "placeholders", Path(__file__).resolve().parents[2] / "placeholders.py")
assert _SPEC and _SPEC.loader
placeholders = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(placeholders)


def _context(db, tokens, group_map, objects, *, domains: tuple[str, ...],
             users: tuple[str, ...]):
    core = core_settings()
    # Past the frozen dataclass's own check: what create_app must catch itself.
    object.__setattr__(core, "allowed_domains", domains)
    return build_context(
        settings=api_settings(core=core, allowed_users=users),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


def test_create_app_refuses_when_both_lists_are_empty(db, tokens, group_map, objects):
    ctx = _context(db, tokens, group_map, objects, domains=(), users=())
    # MUTATION: drop the startup check and the app is built.
    with pytest.raises(RuntimeError) as refused:
        create_app(ctx)
    assert str(refused.value) == SIGN_IN_LIST_REQUIRED
    assert str(refused.value) == (
        "ALLOWED_DOMAINS and ALLOWED_USERS are both empty: set at least one; "
        "swarm-api refuses to start with open authentication")


def test_create_app_starts_with_allowed_users_only(db, tokens, group_map, objects):
    # The one-person deployment: one allowed user, no domain.
    # MUTATION: check only the domains and this refuses.
    ctx = _context(db, tokens, group_map, objects, domains=(), users=(placeholders.OWNER,))
    assert create_app(ctx).state.ctx is ctx


def test_create_app_starts_with_allowed_domains_only(db, tokens, group_map, objects):
    # MUTATION: check only the users and this refuses.
    ctx = _context(db, tokens, group_map, objects,
                   domains=(placeholders.ORG_DOMAIN,), users=())
    assert create_app(ctx).state.ctx is ctx
