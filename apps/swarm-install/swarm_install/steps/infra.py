"""Stages 5 and 7: infra pass 1 (every key switch off), then infra pass 2
(switches computed from key versions) and bootstrap pass 2, all as
`swarm-tf-deployer` (design §4, §5; plan R7; lessons 22, 23, 35, 39).

| Step | Stage | Pass | Manifest entries |
|---|---|---|---|
| `infra.pass-1` | 5 | `infra-1` | the services, jobs, Scheduler, Pub/Sub, Firestore, network, cluster and the secret containers |
| `infra.pass-2` | 7 | `infra-2` | `tenant_job_vpc_egress`, the key switches, `enable_console_app_setup`, `require_alert_channels` |
| `bootstrap.pass-2` | 7 | `bootstrap-2` | the IAP programmatic clients and the log view grant |

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
"""

from __future__ import annotations

from typing import Any

from .bootstrap import DEPS, BootstrapPass2Step, Deps, PassStep


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


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [InfraPass1Step(deps), InfraPass2Step(deps), BootstrapPass2Step(deps)]


STEPS = build_steps()
