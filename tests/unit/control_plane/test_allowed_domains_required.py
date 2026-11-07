"""swarm-api's create_app refuses to start with no allowed domain.

The hosted domains in `settings.core.allowed_domains` are who may sign in,
and the deployment names its own through ALLOWED_DOMAINS
(docs/design/generic-repository.md section 2): an empty list must stop the
service at startup with a fixed message naming the variable, never serve
with open authentication.

The frozen `Settings` refuses an empty tuple when it is built, so the test
empties a built one, the way a later default change could leave it.
No credentials, no network.
"""

from __future__ import annotations

import pytest

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import ALLOWED_DOMAINS_REQUIRED, create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, core_settings


def _context(db, tokens, group_map, objects, *, domains: tuple[str, ...]):
    core = core_settings()
    # Past the frozen dataclass's own check: what create_app must catch itself.
    object.__setattr__(core, "allowed_domains", domains)
    return build_context(
        settings=api_settings(core=core),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


def test_create_app_refuses_an_empty_allowed_domains(db, tokens, group_map, objects):
    ctx = _context(db, tokens, group_map, objects, domains=())
    # MUTATION: drop the startup check and the app is built.
    with pytest.raises(RuntimeError) as refused:
        create_app(ctx)
    assert str(refused.value) == ALLOWED_DOMAINS_REQUIRED
    assert "ALLOWED_DOMAINS" in str(refused.value)


def test_create_app_starts_with_an_allowed_domain(db, tokens, group_map, objects):
    ctx = _context(db, tokens, group_map, objects, domains=("example.com",))
    assert create_app(ctx).state.ctx is ctx
