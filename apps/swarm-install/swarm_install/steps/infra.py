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
"""

from __future__ import annotations

from typing import Any

from .. import tfvars
from ..exitcodes import MET, UNMET
from . import Observation, Plan
from .bootstrap import DEPS, BootstrapPass2Step, Deps, PassStep
from .secrets import PlatformKeysStep


class InfraPass1Step(PassStep):
    pass_name = "infra-1"
    _explain = (
        "Infra pass 1 creates the platform with every key-gated switch off: a Cloud Run revision that names a "
        "secret with no version never starts (lesson 39). It runs as swarm-tf-deployer, never as you, so a "
        "missing actAs shows here and not in the first release (lesson 23)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="infra.pass-1", stage=5, satisfies=())


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


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [InfraPass1Step(deps), InfraPass2Step(deps), BootstrapPass2Step(deps), IdentityPassStep(deps),
            ClosingPassStep(deps)]


STEPS = build_steps()
