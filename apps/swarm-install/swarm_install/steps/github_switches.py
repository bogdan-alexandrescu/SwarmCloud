"""Stage 9's switches, after the console's GitHub step (plan R8, §5.5; review
findings 2 and 10; lesson 39).

The App is made in the console (Admin › Setup, D26), after the hand-off; the
CLI waits on the install record and runs these three as **triggered steps of
the hand-off wait** (`handoff.py`):

| Step | Trigger | What it does |
|---|---|---|
| `github.pass-3` | `github_app.secrets_written_at` and the App's three secrets each have an ENABLED version | infra pass 3: the App ids, `enable_github_app`, `enable_forge_refresh` |
| `github.pass-4` | pass 3 done and the console's self-check `ok` | infra pass 4: `enable_github_hooks`, which makes the receiver and gives swarm-api its `GITHUB_HOOKS_URL` |
| `github.hook` | pass 4 done and its apply reported the receiver serving | `POST /v1/admin/setup/github-app/hook` as `swarm-tf-deployer`: swarm-api signs the App JWT, so the key never reaches the laptop |

**The webhook waits for its receiver.** swarm-api has `GITHUB_HOOKS_URL` only
once the receiver serves (terraform/infra/locals.tf, `api_day_one_env`), which
is infra pass 4's apply; before it, the hook route answers 409
`setup_unconfigured`. So the hook runs after pass 4, and an
`setup_unconfigured` answer (a revision still rolling out) is "not yet": the
step keeps polling within `RECEIVER_WAIT` and never stops the hand-off on it.

A switch is true only when its secret has an ENABLED version (lesson 39);
whether it has one is read from the version's **metadata**. The engine holds
no accessor on any `swarm-github-app-*` secret and never reads one (review
finding 10).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import api_client, tfvars
from ..api_client import ApiCallError
from ..exitcodes import MET, UNMET, WAITING
from ..waits import WaitSpec, wait_until
from . import Observation, Plan, bootstrap
from .bootstrap import DEPS, Deps, PassStep, key_versions
from .preflight import remember

GITHUB_APP = "github_app"
#: The App fields infra pass 3 needs; none is a secret.
APP_FIELDS = ("id", "client_id", "slug", "name")
#: swarm-api's answer while it has no GITHUB_HOOKS_URL (setup_github.py).
NOT_YET = "setup_unconfigured"
#: Pass 4's apply waits for the new swarm-api revision, so the URL is normally
#: served at once; this covers a revision still taking traffic. Well inside the
#: lease (30 min), which the engine renews only between polls of the hand-off.
RECEIVER_WAIT = WaitSpec(
    reason="swarm-api serving GITHUB_HOOKS_URL after infra pass 4",
    expected="under 1 min",
    expected_s=60.0,
    bound_s=300.0,
    look_at="the swarm-api revision infra pass 4 deployed, and its GITHUB_HOOKS_URL",
    interval_s=10.0,
)


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
        return ctx.state.done("github.pass-4") and bool(ctx.outputs.get("github_hooks_serving"))

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
        api = self._api(ctx)

        def attempt() -> str:
            try:
                api.activate_hook()
            except ApiCallError as refused:
                if refused.code == NOT_YET:
                    return WAITING
                raise
            return MET

        wait_until(self.id, RECEIVER_WAIT, attempt, clock=ctx.clock, events=ctx.events)
        remember(ctx, outputs={"github_hook_active": True})


class Pass4Step(PassStep):
    pass_name = "infra-4"
    _explain = ("Infra pass 4 turns on enable_github_hooks once the App's self-check passed: it makes the "
                "receiver and gives swarm-api the URL the webhook is then pointed at.")

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="github.pass-4", stage=9, satisfies=())

    def trigger(self, ctx: Any) -> bool:
        return ctx.state.done("github.pass-3") and bool((app_document(ctx).get("self_check") or {}).get("ok"))

    def apply(self, ctx: Any, plan: Plan) -> None:
        outputs = bootstrap.apply_pass(ctx, self.deps, self.pass_name, targets=self.targets) or {}
        hooks = outputs.get("github_hooks") or {}
        remember(ctx, outputs={"github_hooks_serving": bool(hooks.get("serving"))})


def build_triggered(deps: Deps = DEPS) -> tuple[Any, ...]:
    """The three, in order, for the hand-off wait to carry (not registered on their own)."""
    return (Pass3Step(deps), Pass4Step(deps), HookStep(deps))
