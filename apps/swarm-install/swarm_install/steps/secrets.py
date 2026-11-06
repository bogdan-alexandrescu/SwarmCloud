"""Stage 6, the platform keys: each key infra pass 1 made a container for gets
a version, before any switch that reads it is turned on (design §5.4;
lesson 39: key first, then the switch).

| Key | Switch it gates |
|---|---|
| `swarm-child-key` | `enable_child_tasks` |
| `swarm-live-key` | `enable_live_browser` |
| `swarm-settings-key` | `settings_mac_key_ready` |
| `swarm-workspace-grant-key` | `workspace_grant_attestation.serve` |

A value is generated the way `scripts/create-secrets.sh` generates it
(`secrets.token_urlsafe(48)`), sent straight to Secret Manager and never
printed, logged or kept. A key that already has an ENABLED version is never
given another (a rotation is `create-secrets.sh --overwrite`, a person's
decision). The App's three secrets are not here: swarm-api writes them from
the console (plan §5.5); the engine never reads or writes them.
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any

from .. import tfvars
from ..exitcodes import MET, UNMET
from . import Observation, Plan
from .bootstrap import DEPS, Deps


class PlatformKeysStep:
    id = "secrets.platform-keys"
    stage = 6
    kind = "engine"
    #: None declared: each value's entry depends on its container, whose entry
    #: depends on the switch infra pass 2 turns on after this stage.
    satisfies: tuple[str, ...] = ()

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    def explain(self) -> str:
        return ("A Cloud Run revision that names a secret with no version never starts (lesson 39), so each "
                "platform key gets its version here and its switch is turned on in infra pass 2.")

    def _missing(self, ctx: Any) -> tuple[list[str], list[str]]:
        """(keys whose container exists and has no version, keys with no container)."""
        gcp = self.deps.gcp()
        unversioned, absent = [], []
        for name in tfvars.KEY_SWITCHES:
            if gcp.secret(ctx.project, name) is None:
                absent.append(name)
            elif gcp.enabled_version(ctx.project, name) is None:
                unversioned.append(name)
        return unversioned, absent

    def check(self, ctx: Any) -> Observation:
        unversioned, absent = self._missing(ctx)
        if unversioned:
            return Observation(UNMET, f"no ENABLED version yet: {', '.join(unversioned)}")
        detail = "every platform key infra made has a version"
        if absent:
            detail += f" (not made by this release's infra: {', '.join(absent)}; their switches stay off)"
        return Observation(MET, detail)

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        unversioned, _ = self._missing(ctx)
        return Plan("give each platform key its first version (the values are never shown)",
                    tuple(f"add a generated version to {name}" for name in unversioned))

    def apply(self, ctx: Any, plan: Plan) -> None:
        gcp = self.deps.gcp()
        unversioned, _ = self._missing(ctx)
        for name in unversioned:
            version = gcp.add_secret_version(ctx.project, name, _secrets.token_urlsafe(48).encode())
            ctx.events.emit("step_apply", step=self.id, message=f"{name}: version {version} added")


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [PlatformKeysStep(deps)]


STEPS = build_steps()
