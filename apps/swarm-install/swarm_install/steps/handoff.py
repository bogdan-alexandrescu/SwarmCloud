"""The hand-off to the console, and READY (plan §5.5; D26; B05, B09b; R8).

| Step | Stage | Kind | What it does |
|---|---|---|---|
| `handoff.console` | 13 | wait | once `https://<H>/` answers behind IAP, writes `hand_off_at` and opens the one URL (`open_page`: printed, and opened in the browser), once; then waits for `console/finished`, keeping the lease (the engine heartbeats on every poll) and running stage 9's switches (`github_switches.py`) as triggered steps when the console's App step lands |
| `handoff.ready` | 15 | engine | after stage 14's verify steps all passed (they run only once the wait above is met, B09b) and the webhook pass and the hook are done, writes `ready_at` and prints READY |

**The wait is met only once the switches are on, too.** The console can
finish while no CLI runs (its steps need none up to the App's self-check), and
the triggered steps run only on the wait's polls. So `console/finished` alone
does not meet the hand-off: while any of stage 9's switches is not done, the
check is `waiting`, the engine applies the step again, and the wait's first
poll, before its first check, runs every switch whose trigger has landed
(audit finding 17). The page is not opened again once the console is finished.

The wait has a bound (a person finishing the console's steps); past it the
install pauses with exit 4, and `swarm install` again resumes at the wait:
every console step still works while no CLI runs, up to the App's self-check.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import swarm_context
from ..exitcodes import MET, UNMET, WAITING
from ..waits import WaitGaveUp, WaitSpec, wait_until
from . import Observation, Plan, bootstrap, open_page
from .bootstrap import DEPS, Deps, PassStep
from .edge import CLIENT_ID, CONSOLE_ANSWERS, IAP_ANSWERS, SignInRecordStep, client_pending, client_steps
from .github_switches import build_triggered
from .infra import latest_pass
from .preflight import ASK, remember

FINISHED = "finished"
#: What READY needs besides the verify steps: the webhook pass and the hook.
SWITCHES = ("github.pass-4", "github.hook")

#: A person working through Admin › Setup: GitHub App, first team, Claude
#: accounts, send to your team.
CONSOLE = WaitSpec(
    reason="you finishing Admin › Setup in the console",
    expected="20 to 60 min",
    expected_s=60 * 60,
    bound_s=6 * 60 * 60,
    look_at="the console's Admin › Setup page says which step is next; run swarm install again to resume",
    interval_s=10,
)
#: The output the sign-in passes record the Desktop client they applied under:
#: `iap_programmatic_client` is in IAP's programmatic-clients list (bootstrap)
#: and the engine document's `sign_in_client`, which Admin › Setup reads as
#: `applied`; `discovery_client` is in the discovery document (infra).
IAP_CLIENT = "iap_programmatic_client"
DISCOVERY_CLIENT = "discovery_client"


def _recorded_client(ctx: Any) -> str:
    client = str(ctx.outputs.get("desktop_client_id") or "")
    return client if CLIENT_ID.match(client) else ""


class _SignInPass(PassStep):
    """A pass the hand-off wait runs once the Desktop client is taken in and
    the pass last applied did not carry it. Its check is the pass's own (the
    applied inputs equal what it would generate now, which name the client),
    so a client the old terminal prompt recorded before stage 5 is met at
    once, with no pass run."""

    output = ""

    def _name(self, ctx: Any) -> str:
        return self.pass_name

    def trigger(self, ctx: Any) -> bool:
        client = _recorded_client(ctx)
        return bool(client) and not client_pending(ctx) and ctx.outputs.get(self.output) != client

    def check(self, ctx: Any) -> Observation:
        client = _recorded_client(ctx)
        if not client:
            return Observation(UNMET, "no Desktop client is recorded yet")
        self.pass_name = self._name(ctx)
        why = self._pending(ctx)
        if why:
            return Observation(UNMET, why)
        return Observation(MET, f"{self.pass_name} applied with the Desktop client {client}", {self.output: client})

    def plan(self, ctx: Any) -> Plan:
        self.pass_name = self._name(ctx)
        return super().plan(ctx)

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.pass_name = self._name(ctx)
        bootstrap.apply_pass(ctx, self.deps, self.pass_name, targets=self.targets)
        remember(ctx, outputs={self.output: _recorded_client(ctx)})


class SignInBootstrapStep(_SignInPass):
    pass_name = "bootstrap-2"
    output = IAP_CLIENT
    _explain = ("Bootstrap pass 2 again, now with the Desktop client the console saved: it writes IAP's "
                "programmatic-clients list (frontend_iap_programmatic_clients) through the IAP settings API, so "
                "sc's sign-in through that client passes IAP.")

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="handoff.sign-in-bootstrap", stage=13, satisfies=())


class SignInInfraStep(_SignInPass):
    pass_name = "infra-2"
    output = DISCOVERY_CLIENT
    _explain = ("The latest infra pass again, now with the Desktop client: the discovery document names it and "
                "serves its secret, so sc needs only the deployment's URL.")

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="handoff.sign-in-infra", stage=13, satisfies=())

    def _name(self, ctx: Any) -> str:
        """The latest infra pass the install reached (never earlier than
        infra-2), so no switch a later pass turned on goes off again."""
        return latest_pass(ctx.outputs)


def sign_in_triggered(deps: Deps = DEPS, *,
                      contexts: swarm_context.Run = swarm_context.run_command) -> tuple[Any, ...]:
    """The console's Sign-in client step's three, in order, for the hand-off
    wait to carry: take the client in, then the bootstrap and infra passes."""
    return (SignInRecordStep(deps.gcp, ASK, contexts=contexts), SignInBootstrapStep(deps), SignInInfraStep(deps))


def certificates_page(project: str) -> str:
    """The Google Cloud console's list of the project's classic SSL
    certificates: the frontend's managed certificate and its domain status."""
    return ("https://console.cloud.google.com/net-services/loadbalancing/advanced/sslCertificates/list"
            f"?project={project}")


def console_url(ctx: Any) -> str:
    hostname = ctx.state.hostname or ctx.outputs.get("hostname")
    if not hostname:
        raise LookupError("no hostname is recorded (preflight.inputs asks it)")
    return f"https://{hostname}/admin/setup"


class HandoffStep:
    id = "handoff.console"
    stage = 13
    kind = "wait"
    wait = CONSOLE
    satisfies: tuple[str, ...] = ()

    def __init__(self, deps: Deps, *, status: Callable[[str], int] | None = None,
                 say: Callable[[str], None] = print) -> None:
        self.deps = deps
        self._status = status
        self._say = say
        self.triggered = (*sign_in_triggered(deps), *build_triggered(deps))

    def explain(self) -> str:
        return ("The rest is people's work in the console (D26): the GitHub App, the first team, the Claude "
                "accounts. This terminal keeps the install lease while you do it, turns the App's switches on "
                "when its step lands, and runs verify once the console says it is finished.")

    def answers(self, ctx: Any) -> bool:
        status = self._status or self.deps.gcp().http_status
        try:
            return status(f"https://{ctx.state.hostname or ctx.outputs.get('hostname')}/") in IAP_ANSWERS
        except OSError:
            return False

    def switches_off(self, ctx: Any) -> list[str]:
        return [step.id for step in self.triggered if not ctx.state.done(step.id)]

    def check(self, ctx: Any) -> Observation:
        if ctx.read_console(FINISHED):
            off = self.switches_off(ctx)
            if not off:
                return Observation(MET, "the console's setup is finished and the App's switches are on")
            return Observation(WAITING, f"the console's setup is finished; waiting for {', '.join(off)}")
        if ctx.state.hand_off_at is None:
            return Observation(UNMET, "the console has not been handed off yet")
        return Observation(WAITING, "waiting for the console's setup to finish")

    def verify(self, ctx: Any) -> Observation:
        observed = self.check(ctx)
        return observed if observed.status == MET else Observation(UNMET, observed.detail)

    def plan(self, ctx: Any) -> Plan:
        return Plan("hand off to the console", (f"open {console_url(ctx)}",
                                                "this terminal waits, keeping the lease, until the console is done"))

    def apply(self, ctx: Any, plan: Plan) -> None:
        # The certificate can take longer than the lease: this wait renews it on
        # every poll, as the engine's own wait does (B05). Past its bound its
        # WaitGaveUp is left to the engine, which pauses the run (exit 4), once
        # the certificates page its look_at names is open.
        try:
            wait_until(self.id + ":answers", CONSOLE_ANSWERS, lambda: MET if self.answers(ctx) else UNMET,
                       clock=ctx.clock, events=ctx.events, on_poll=getattr(ctx.engine, "_heartbeat", None))
        except WaitGaveUp:
            open_page(certificates_page(ctx.project), why=(
                f"\nThe console at {ctx.state.hostname or ctx.outputs.get('hostname')} has not answered "
                "behind IAP. Look at the managed certificate on this page (opening it now): a domain status other "
                "than ACTIVE means the hostname's A record does not reach the load balancer yet. Run swarm install "
                "again to resume."), say=self._say)
            raise
        url = console_url(ctx)
        if ctx.state.hand_off_at is None:
            now = ctx.clock.now()
            ctx.engine.save(lambda s: setattr(s, "hand_off_at", now))
        if ctx.read_console(FINISHED):
            # Finished while no CLI ran: nothing is left for the person. The
            # engine's wait runs the landed switches on its first poll.
            ctx.events.emit("step_apply", step=self.id, url=url,
                            message="the console's setup is finished; turning the GitHub App's switches on")
            return
        ctx.events.emit("step_apply", step=self.id, url=url,
                        message=f"open {url} to finish setup (keep this terminal open)")
        open_page(url, why="\nFinish setup in the console's Admin › Setup (opening it now). Keep this terminal "
                           "open: it turns the GitHub App on and runs verify when the console is done.",
                  say=self._say)


class ReadyStep:
    id = "handoff.ready"
    stage = 15
    kind = "engine"
    satisfies = ("other.ready-screen",)

    def __init__(self, deps: Deps, *, verify_ids: Callable[[], list[str]] | None = None,
                 say: Callable[[str], None] = print) -> None:
        self.deps = deps
        self._verify_ids = verify_ids or _verify_step_ids
        self._say = say

    def explain(self) -> str:
        return ("READY is written only after every verify check passed and the GitHub App's webhook is on "
                "(D3: done is proven, never assumed).")

    def unverified(self, ctx: Any) -> list[str]:
        return [step for step in (*self._verify_ids(), *SWITCHES) if not ctx.state.done(step)]

    def check(self, ctx: Any) -> Observation:
        if ctx.state.ready_at is not None and not self.unverified(ctx):
            return Observation(MET, "READY")
        return Observation(UNMET, "not ready yet")

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        return Plan("write ready_at and print READY", tuple(f"needs {s}" for s in self.unverified(ctx)))

    def apply(self, ctx: Any, plan: Plan) -> None:
        missing = self.unverified(ctx)
        if missing:
            raise RuntimeError(f"not READY: these have not passed: {', '.join(missing)}")
        now = ctx.clock.now()
        ctx.engine.save(lambda s: setattr(s, "ready_at", now))
        url = console_url(ctx).removesuffix("/admin/setup")
        ctx.events.emit("step_apply", step=self.id, url=url, message=f"READY: {url}")
        self._say(f"\nREADY: SwarmCloud answers at {url}\n")


def _verify_step_ids() -> list[str]:
    from . import verify

    return [step.id for step in verify.STEPS]


def build_steps(deps: Deps = DEPS) -> list[Any]:
    """The hand-off, then the brand and Desktop client waits (met by the
    console's Sign-in client step, issue #2964), then READY."""
    return [HandoffStep(deps), *client_steps(deps.gcp, ASK), ReadyStep(deps)]


STEPS = build_steps()
