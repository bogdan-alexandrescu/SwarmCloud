"""Stages 5 and 7: infra pass 1 (every key switch off), then infra pass 2
(switches computed from key versions) and bootstrap pass 2, all as
`swarm-tf-deployer` (design §4, §5; plan R7; lessons 22, 23, 35, 39).

| Step | Stage | Pass | Manifest entries |
|---|---|---|---|
| `infra.pass-1` | 5 | `infra-1` | the services, jobs, Scheduler, Pub/Sub, Firestore, network, cluster and the secret containers |
| `infra.pass-2` | 7 | `infra-2` | `tenant_job_vpc_egress`, the key switches, `enable_console_app_setup`, `require_alert_channels` |
| `bootstrap.pass-2` | 7 | `bootstrap-2` | the IAP programmatic clients and the log view grant |
| `infra.pass-identity` | 8 | `infra-2` again | none: the admin groups stage 8 recorded, and a version for any platform key pass 2 made (the graph ticket key) |
| `infra.pass-close` | 14 | `infra-close` | `other.engine-cluster-reach-closed`: the installer's address leaves the control plane's authorised networks, after stage 14's verify steps |

The passes declare no manifest entries as `satisfies`: the manifest's
`depends_on` among the resources one apply makes runs both ways (a container
depends on its switch, the switch on the container's version), so declaring
them would make the engine's dependency gate refuse the stage. Each pass's
check is that its generated inputs are the ones it applied.

Pass 1 runs with every key-gated switch false, so no revision names a secret
that has no version (lesson 39). Stage 6 (`secrets.py`) gives the platform
keys their versions; pass 2 turns each switch on exactly when its key has an
ENABLED version. Pass 2 also records the API backend's IAP audience, which the
hand-off needs to call swarm-api as the deployer. Bootstrap pass 2 follows it
in the same stage (registration order: it is listed after pass 2 here).

**Stage 8 applies what it recorded.** `identity.admin-group` records
`admin_groups` only once swarm-api has read the group, which is after pass 2;
`infra.pass-identity` re-applies pass 2 with them, so a later run's stage 8
checks see what stage 8 recorded. It runs after every identity step: the
registry is discovered by module name and `infra` follows `identity`, and it
declares no manifest entry (each stage 8 entry is an identity step's), so the
engine keeps that order. It first gives a version to any
platform key whose container pass 2 made (the graph service's ticket key is
made in pass 2, once the cluster's workload pool exists), key before switch.

**The closing pass** (decision 2). Every infra pass before it, infra-3 and
infra-4 included, carries the installer's `/32` in
`gke_master_authorized_cidrs`: the control plane admits no public Google range
(`gcp_public_cidrs_access_enabled = false`), and the cluster stages (10, 11)
and stage 14's `verify.reconciler-jobs` run kubectl from the laptop.
`infra.pass-close` is the last pass: in stage 14, its manifest entry depends on
`other.verify-end-to-end` and `other.firestore-migrations`, so the engine
orders it after every verify step and before `handoff.ready` (stage 15). It
applies infra-4's inputs without the address and is met while those are the
inputs it applied, so a run that stopped before it has the leftover entry
removed by the next run.

**Pass 1 adopts the reserved address** (finding 6). Stage 2's
`edge.global-ip` reserves `swarm-ui-ip` by API so the A record can be set
early; the frontend module declares the same address. Before pass 1 plans,
the engine imports it into `module.frontend[0].google_compute_global_address.this`
when the project holds it and the state does not (as bootstrap imports its
keys), so the apply never fails on "already exists". No `import {}` block is
added to terraform/infra: the live deployment's plan does not change.

**Reopening after the close** (finding 16). Once `infra-close` has applied,
a re-run's cluster stages, `swarm team add`, `swarm upgrade` and an add-on
still run kubectl from the laptop. `ClusterReach.reopen`, which `kube_for`'s
`Kube` calls before its first kubectl call, refreshes `installer_cidr` from
this machine's address and applies infra-4's inputs (the close's inputs with
the address) as the deployer, and records `cluster_reach_reopened`. The
closing pass is unmet while that is recorded, so `swarm install`'s stage 14
closes it again; the verbs close it themselves when they finish
(`ClusterReach.close`), and a verb that died in between leaves the record for
the next run's close.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import record as install_record
from .. import tfvars
from ..api_client import deployer_email
from ..exitcodes import MET, UNMET
from ..terraform import Terraform
from . import Observation, Plan, bootstrap
from .bootstrap import DEPS, BootstrapPass2Step, Deps, PassStep, applied_key
from .edge import ADDRESS
from .secrets import PlatformKeysStep

#: Where infra's frontend module keeps the address stage 2 reserved
#: (terraform/modules/frontend/main.tf `google_compute_global_address.this`).
FRONTEND_ADDRESS = "module.frontend[0].google_compute_global_address.this"

#: Recorded while an infra pass has put the installer's address back after the
#: close; the closing pass is unmet until it removes it again.
REOPENED = "cluster_reach_reopened"
#: The pass whose inputs reopen the control plane: infra-close's, with the address.
REOPEN_PASS = "infra-4"


def address_imports(gcp: Any, project: str, in_state: list[str]) -> list[tuple[str, str]]:
    """(address, id) of stage 2's global address when the project holds it and
    infra's state does not: pass 1 imports it instead of creating it again."""
    if FRONTEND_ADDRESS in in_state or gcp.global_address(project, ADDRESS) is None:
        return []
    return [(FRONTEND_ADDRESS, f"projects/{project}/global/addresses/{ADDRESS}")]


class InfraPass1Step(PassStep):
    pass_name = "infra-1"
    _explain = (
        "Infra pass 1 creates the platform with every key-gated switch off: a Cloud Run revision that names a "
        "secret with no version never starts (lesson 39). It runs as swarm-tf-deployer, never as you, so a "
        "missing actAs shows here and not in the first release (lesson 23)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-1", stage=5, satisfies=())

    def apply(self, ctx: Any, plan: Plan) -> None:
        gcp = self.deps.gcp()
        deployer = deployer_email(ctx.project)

        def adopt_address(tf: Terraform) -> None:
            for address, resource_id in address_imports(gcp, ctx.project, tf.state_list(impersonate=deployer)):
                ctx.events.emit("step_apply", step=self.id, message=f"import {address} (stage 2 reserved it)")
                tf.run("import", "-input=false", address, resource_id, impersonate=deployer)

        bootstrap.apply_pass(ctx, self.deps, self.pass_name, before_plan=adopt_address)
        # Firestore exists now: from here every save mirrors to the install
        # record and the hand-off reads the console (N1). A resumed run skips
        # this apply; `swarm install` attaches it then (verbs/install.py).
        ctx.attach_record(install_record.open_record(ctx.project, ctx.environment))


class InfraPass2Step(PassStep):
    pass_name = "infra-2"
    _explain = (
        "Infra pass 2 turns on each switch whose key now has a version (key before switch), the console's "
        "App setup grants, Direct VPC egress for scheduler-made Jobs (lesson 35) and the alert channel with "
        "require_alert_channels (#1817)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-2", stage=7, satisfies=())


class IdentityPassStep(PassStep):
    """Infra pass 2 again, in stage 8, after the identity steps."""

    pass_name = "infra-2"
    _explain = (
        "Stage 8 registers the admin group only once swarm-api has read it, after infra pass 2 applied. This "
        "applies pass 2 again with the group, so the console admits its members, and gives a version to any "
        "platform key pass 2 made first (key before switch)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-identity", stage=8, satisfies=())
        self.keys = PlatformKeysStep(deps)

    def check(self, ctx: Any) -> Observation:
        keys = self.keys.check(ctx)
        if keys.status != MET:
            return Observation(UNMET, keys.detail)
        return super().check(ctx)

    def plan(self, ctx: Any) -> Plan:
        keys = self.keys.plan(ctx)
        base = super().plan(ctx)
        return Plan("Terraform infra-2 with the admin groups stage 8 recorded", (*keys.actions, *base.actions))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.keys.apply(ctx, self.keys.plan(ctx))
        super().apply(ctx, plan)


class ClosingPassStep(PassStep):
    """Decision 2: the installer's address leaves the control plane's authorised networks."""

    pass_name = "infra-close"
    _explain = (
        f"The install reached the cluster from your address, listed as {tfvars.CLUSTER_REACH_NAME!r} in the "
        "control plane's authorised networks. With the cluster stages and the verify reads done, this pass "
        "removes it and changes nothing else."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-close", stage=14, satisfies=("other.engine-cluster-reach-closed",))

    def _pending(self, ctx: Any) -> str | None:
        if ctx.outputs.get(REOPENED):
            return f"the control plane admits {ctx.outputs[REOPENED]} again (reopened for kubectl after the close)"
        return super()._pending(ctx)

    def apply(self, ctx: Any, plan: Plan) -> None:
        from .preflight import remember

        bootstrap.apply_pass(ctx, self.deps, self.pass_name)
        if ctx.outputs.get(REOPENED):
            remember(ctx, outputs={REOPENED: None})


def reach_closed(outputs: Any) -> bool:
    """The closing pass has removed the installer's address and nothing has put it back."""
    return bool(outputs.get(applied_key("infra-close"))) and not outputs.get(REOPENED)


def _refresh_address(ctx: Any) -> None:
    """`installer_cidr` from this machine's address now (preflight.installer-address)."""
    from .preflight import ASK, GCP, InstallerAddressStep

    step = InstallerAddressStep(GCP, ASK)
    if step.check(ctx).status != MET:
        step.apply(ctx, step.plan(ctx))


class ClusterReach:
    """Decision 2 after the close: put this machine's /32 back for kubectl, then take it out again."""

    def __init__(self, deps: Deps = DEPS, *, address: Callable[[Any], None] = _refresh_address) -> None:
        self.deps = deps
        self.address = address

    def reopen(self, ctx: Any) -> bool:
        """Apply infra-4's inputs with the current address when the close removed it; True when it did."""
        from .preflight import remember

        if not reach_closed(ctx.outputs):
            return False
        if ctx.dry_run:
            raise RuntimeError("the closing pass removed this machine's address from the control plane's "
                               "authorised networks; a dry run never applies the infra pass that reopens it")
        self.address(ctx)
        ctx.events.emit("step_apply", step="infra.reopen",
                        message=f"the control plane admits {ctx.outputs.get('installer_cidr')} for kubectl "
                                "until the closing pass removes it again")
        bootstrap.apply_pass(ctx, self.deps, REOPEN_PASS)
        remember(ctx, outputs={REOPENED: str(ctx.outputs.get("installer_cidr") or "")})
        return True

    def close(self, ctx: Any) -> bool:
        """The closing pass again when a reopen left the address; True when it applied."""
        step = ClosingPassStep(self.deps)
        if not ctx.outputs.get(REOPENED):
            return False
        step.apply(ctx, step.plan(ctx))
        return True

    def close_after(self, ctx: Any, verb: str) -> str | None:
        """`close` at the end of a verb; a failure is the line the verb prints, and
        the record stays for `swarm install`'s closing pass."""
        try:
            self.close(ctx)
        except Exception as exc:  # noqa: BLE001 - the verb's own outcome stands; this line says what is left
            return (f"{verb}: the closing pass did not apply ({type(exc).__name__}: {str(exc)[:300]}); the control "
                    f"plane still admits {ctx.outputs.get(REOPENED)}: swarm install removes it")
        return None


REACH = ClusterReach()


class VerbContext:
    """What `ClusterReach` needs of an engine context, for a verb that runs
    outside the engine loop (`swarm team add`, an add-on): the state, saved
    through `save` (a store's re-reading `update`, which returns the state it
wrote), and the bucket
    a pass locks and writes its inputs to."""

    dry_run = False

    def __init__(self, *, project: str, environment: str, holder: str, state: Any, events: Any, bucket: Any,
                 save: Callable[[Callable[[Any], None]], Any]) -> None:
        self.project = project
        self.environment = environment
        self.holder = holder
        self.state = state
        self.events = events
        self.bucket = bucket
        self._save = save

    @property
    def outputs(self) -> dict[str, Any]:
        return self.state.outputs

    @property
    def engine(self) -> VerbContext:
        return self

    @property
    def store(self) -> VerbContext:
        return self

    def save(self, change: Callable[[Any], None]) -> None:
        self.state = self._save(change)


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [InfraPass1Step(deps), InfraPass2Step(deps), BootstrapPass2Step(deps), IdentityPassStep(deps),
            ClosingPassStep(deps)]


STEPS = build_steps()
