"""The GitHub App's webhook: its URL carries the receiver's route, the manifest
is never posted without it, and the install engine recognises swarm-api's
"not yet" answer while a revision without the URL still takes traffic
(DAYONE-WEBHOOK; audit 4c M1, audit 4d F9 and F10; part of #2688).

The engine's `ApiClient` talks to swarm-api's REAL app and its real `ApiError`
handler under TestClient, not to a hand-written error body: a fake body is what
let the engine wait for a code (`setup_unconfigured`) swarm-api never served.
"""

from __future__ import annotations

import io
import re
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from .conftest import PROJECT, auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    HOOKS_URL,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

REPO = Path(__file__).resolve().parents[3]
_ENGINE = str(REPO / "apps" / "swarm-install")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from swarm_install.api_client import ApiCallError, ApiClient, Response  # noqa: E402
from swarm_install.events import Events  # noqa: E402
from swarm_install.steps.bootstrap import Deps  # noqa: E402
from swarm_install.steps.github_switches import RECEIVER_WAIT, HookStep  # noqa: E402
from swarm_install.waits import WaitGaveUp  # noqa: E402

GITHUB_APP_DOC = f"swarm-install/{ENV}/console/github_app"
MANIFEST = "/v1/admin/setup/github-app/manifest"
WEBHOOK_SECRET = "swarm-github-app-webhook-secret"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Ctx:
    """What `github.hook` reads: the project, a clock, events and the outputs."""

    def __init__(self, clock: Clock) -> None:
        self.project = PROJECT
        self.clock = clock
        self.events = Events(clock.now, json_lines=True, out=io.StringIO())
        self.dry_run = True
        self.state = type("State", (), {"outputs": {}})()

    @property
    def outputs(self) -> dict[str, Any]:
        return self.state.outputs


class DeployerToken:
    """`generateIdToken` for the deployer: the token swarm-api's verifier knows."""

    def generate_id_token(self, service_account: str, audience: str, *, include_email: bool) -> str:
        return auth_header("deployer")["Authorization"].removeprefix("Bearer ")


def _engine_client(setup_client, on_call=None) -> ApiClient:
    def http(method: str, url: str, headers: Any, body: Any) -> Response:
        if on_call is not None:
            on_call()
        answer = setup_client.request(method, url.removeprefix("https://swarm.example.com"),
                                      headers=dict(headers), json=dict(body))
        return Response(answer.status_code, answer.json())

    return ApiClient(project=PROJECT, base_url="https://swarm.example.com", audience="/projects/1/x",
                     minter=DeployerToken(), http=http)


def _without_hooks_url(setup_app) -> None:
    ctx = setup_app.state.ctx
    ctx.settings = replace(ctx.settings, setup_github_hooks_url="")


def _with_hooks_url(setup_app) -> None:
    ctx = setup_app.state.ctx
    ctx.settings = replace(ctx.settings, setup_github_hooks_url=HOOKS_URL)


def _app_made(db, fake_secrets) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud"}
    fake_secrets.add_version(WEBHOOK_SECRET, secrets.token_hex(16))


# -- M1: the hook's "not yet" during a rolling revision ------------------------


def test_the_engine_waits_through_a_revision_without_the_hook_url(setup_app, setup_client, db,
                                                                  fake_secrets, github) -> None:
    """Two answers from a revision that has no GITHUB_HOOKS_URL, then the new
    revision: the step polls through both and turns the webhook on."""
    _app_made(db, fake_secrets)
    _without_hooks_url(setup_app)
    calls: list[int] = []

    def roll_out() -> None:
        calls.append(1)
        if len(calls) == 3:
            _with_hooks_url(setup_app)

    clock = Clock()
    ctx = Ctx(clock)
    hook = HookStep(Deps(gcp=lambda: None), api=lambda c: _engine_client(setup_client, roll_out))
    hook.apply(ctx, hook.plan(ctx))
    assert len(calls) == 3, "it kept polling through the two 'not yet' answers"
    assert clock.now() > 1_800_000_000.0, "between attempts it waited"
    assert ctx.outputs["github_hook_active"] is True
    [patch] = [r for r in github.requests if r["method"] == "PATCH"]
    assert patch["url"] == "https://api.github.com/app/hook/config"


def test_the_not_yet_answer_is_read_from_the_served_body(setup_app, setup_client, db,
                                                         fake_secrets) -> None:
    """The engine reads swarm-api's flat `{code, message, detail}`; the code
    stays the established `conflict` and `detail.unset` names the setting."""
    _app_made(db, fake_secrets)
    _without_hooks_url(setup_app)
    with pytest.raises(ApiCallError) as refused:
        _engine_client(setup_client).activate_hook()
    assert refused.value.status == 409
    assert refused.value.code == "conflict"
    assert refused.value.detail == {"unset": ["GITHUB_HOOKS_URL"]}
    assert refused.value.message == "GITHUB_HOOKS_URL is not set"


def test_the_engine_stops_on_any_other_409(setup_app, setup_client, db) -> None:
    """No webhook secret version is a 409 `conflict` too, and is not "not
    yet": the step stops at once rather than waiting out RECEIVER_WAIT."""
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "slug": "swarmcloud"}
    calls: list[int] = []
    clock = Clock()
    ctx = Ctx(clock)
    hook = HookStep(Deps(gcp=lambda: None),
                    api=lambda c: _engine_client(setup_client, lambda: calls.append(1)))
    with pytest.raises(ApiCallError) as refused:
        hook.apply(ctx, hook.plan(ctx))
    assert refused.value.code == "conflict" and "has no enabled version" in refused.value.message
    assert len(calls) == 1
    assert "github_hook_active" not in ctx.outputs


def test_a_revision_that_never_gets_the_url_gives_up_within_the_bound(setup_app, setup_client, db,
                                                                       fake_secrets) -> None:
    _app_made(db, fake_secrets)
    _without_hooks_url(setup_app)
    clock = Clock()
    ctx = Ctx(clock)
    hook = HookStep(Deps(gcp=lambda: None), api=lambda c: _engine_client(setup_client))
    with pytest.raises(WaitGaveUp):
        hook.apply(ctx, hook.plan(ctx))
    assert clock.now() - 1_800_000_000.0 <= RECEIVER_WAIT.bound_s + RECEIVER_WAIT.interval_s


# -- F10: the manifest is never posted with an empty hook URL -------------------


def test_the_manifest_waits_for_the_hook_url(setup_app, setup_client, db, github) -> None:
    """Without GITHUB_HOOKS_URL the console's manifest request is the 409 "not
    yet": no state is written and GitHub is not asked, so no App is made
    whose conversion could lack its webhook secret."""
    _without_hooks_url(setup_app)
    response = setup_client.post(MANIFEST, json={}, headers=auth_header("root"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert response.json()["detail"] == {"unset": ["GITHUB_HOOKS_URL"]}
    assert GITHUB_APP_DOC not in db.docs
    assert github.requests == []


# -- F9: the rendered URL carries the receiver's route -------------------------


def _rendered_hooks_url() -> tuple[str, str]:
    text = (REPO / "terraform" / "infra" / "locals.tf").read_text()
    found = re.search(r"^\s*(.*)\{\s*GITHUB_HOOKS_URL\s*=\s*\"([^\"]+)\"", text, re.M)
    assert found, "terraform/infra/locals.tf no longer renders GITHUB_HOOKS_URL"
    return found.group(1), found.group(2)


def _receiver_path() -> str:
    text = (REPO / "apps" / "swarm-github-hooks" / "swarm_github_hooks" / "receiver.py").read_text()
    found = re.search(r'^PATH = "([^"]+)"', text, re.M)
    assert found, "the receiver no longer declares PATH"
    return found.group(1)


def test_the_rendered_hook_url_ends_in_the_receivers_route() -> None:
    """The receiver answers only `POST <PATH>`; a bare host is a 404 on every push."""
    _, url = _rendered_hooks_url()
    assert url.endswith(".run.app" + _receiver_path()), url


def test_the_hook_url_is_rendered_before_the_receiver_serves() -> None:
    """The manifest (made after infra pass 2) names the URL, so it is rendered
    under enable_console_app_setup alone, not once the receiver serves."""
    condition, _ = _rendered_hooks_url()
    assert "enable_console_app_setup" in condition
    assert "github_hooks_serving" not in condition
