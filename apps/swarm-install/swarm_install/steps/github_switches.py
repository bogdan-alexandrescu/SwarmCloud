"""Stage 9's switches, after the console's GitHub step (plan R8, §5.5; review
findings 2 and 10; lesson 39).

The App is made in the console (Admin › Setup, D26), after the hand-off; the
CLI waits on the install record and runs these three as **triggered steps of
the hand-off wait** (`handoff.py`):

| Step | Trigger | What it does |
|---|---|---|
| `github.pass-3` | `github_app.secrets_written_at` and the App's three secrets each have an ENABLED version | infra pass 3: the App ids, `enable_github_app`, `enable_forge_refresh` |
| `github.hook` | pass 3 done and the console's self-check `ok` | `POST /v1/admin/setup/github-app/hook` as `swarm-tf-deployer`: swarm-api signs the App JWT, so the key never reaches the laptop |
| `github.pass-4` | the hook is active | infra pass 4: `enable_github_hooks` |

A switch is true only when its secret has an ENABLED version (lesson 39);
whether it has one is read from the version's **metadata**. The engine holds
no accessor on any `swarm-github-app-*` secret and never reads one (review
finding 10).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import api_client, tfvars
from ..exitcodes import MET, UNMET
from . import Observation, Plan
from .bootstrap import DEPS, Deps, PassStep, key_versions
from .preflight import remember

GITHUB_APP = "github_app"
#: The App fields infra pass 3 needs; none is a secret.
APP_FIELDS = ("id", "client_id", "slug", "name")


def app_document(ctx: Any) -> dict[str, Any]:
    return dict(ctx.read_console(GITHUB_APP) or {})


def app_keys_present(gcp: Any, project: str) -> bool:
    versions = key_versions(gcp, project, tfvars.APP_SECRETS)
    return all(versions.values())


class Pass3Step(PassStep):
    pass_name = "infra-3"
    _explain = ("The App exists only after the console's GitHub step (R8). Once its three secrets have versions, "
                "infra pass 3 gives the services the App's ids and turns on enable_github_app and "
                "enable_forge_refresh (key before switch).")

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="github.pass-3", stage=9,
                         satisfies=("switch.enable_github_app", "switch.enable_forge_refresh"))

    def trigger(self, ctx: Any) -> bool:
        app = app_document(ctx)
        return bool(app.get("secrets_written_at") and app.get("id")) and app_keys_present(self.deps.gcp(),
                                                                                           ctx.project)

    def apply(self, ctx: Any, plan: Plan) -> None:
        app = app_document(ctx)
        remember(ctx, outputs={"github_app": {k: app[k] for k in APP_FIELDS if app.get(k)}})
        super().apply(ctx, plan)


class HookStep:
    id = "github.hook"
    stage = 9
    kind = "engine"
    satisfies: tuple[str, ...] = ()

    def __init__(self, deps: Deps, *, api: Callable[[Any], Any] = api_client.for_install) -> None:
        self.deps = deps
        self._api = api

    def explain(self) -> str:
        return ("The webhook is turned on only after the App's self-check passed and its secret has a version. "
                "swarm-api does it, signing the App JWT itself; the engine never holds the App's key.")

    def trigger(self, ctx: Any) -> bool:
        return ctx.state.done("github.pass-3") and bool((app_document(ctx).get("self_check") or {}).get("ok"))

    def check(self, ctx: Any) -> Observation:
        if ctx.outputs.get("github_hook_active"):
            return Observation(MET, "swarm-api set the App's webhook active")
        return Observation(UNMET, "the App's webhook is not active yet")

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        return Plan("ask swarm-api to set the App's webhook active",
                    (f"POST {api_client.HOOK_ROUTE} as {api_client.deployer_email(ctx.project)}",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self._api(ctx).activate_hook()
        remember(ctx, outputs={"github_hook_active": True})


class Pass4Step(PassStep):
    pass_name = "infra-4"
    _explain = "Infra pass 4 turns on enable_github_hooks, the one switch left, once the webhook is active."

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="github.pass-4", stage=9, satisfies=())

    def trigger(self, ctx: Any) -> bool:
        return ctx.state.done("github.hook")


def build_triggered(deps: Deps = DEPS) -> tuple[Any, ...]:
    """The three, in order, for the hand-off wait to carry (not registered on their own)."""
    return (Pass3Step(deps), HookStep(deps), Pass4Step(deps))
