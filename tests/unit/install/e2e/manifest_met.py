"""Every core manifest switch and API, read back from what the fakes hold.

The engine counts an entry done when its owning step's check passes; A1 was
a switch the manifest counted done that no pass rendered. Here a switch is
met only when the LAST apply of the root that declares it carried it on
(its rendered value, else the variable's own default), and an API only when
the fake Service Usage holds it enabled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swarm_install import manifest

from . import terraform_fake
from .world import World

#: A switch id whose variable is an object, mapped to (variable, attribute).
_OBJECT_SWITCHES = {
    "switch.graph_service-enabled": ("graph_service", "enabled"),
    "switch.graph_service-serve": ("graph_service", "serve"),
    "switch.workspace_grant_attestation-serve": ("workspace_grant_attestation", "serve"),
}

#: A switch the manifest records as deliberately OFF on a Day One install:
#: its entry says the default is false and the install does not override it.
_OFF_BY_DESIGN = {"switch.front_door_admit_role_bound"}
#: A switch whose variable is a map with no single "on" value (api_refusals,
#: map(bool) default {}): met when its root declares it.
_NO_ON_VALUE = {"switch.api_refusals"}


def _on(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if isinstance(value, dict):
        return bool(value)
    return value is True


def unmet(world: World, repo_root: Path) -> list[str]:
    """`<entry id>: <why>` for each core switch or API the fakes do not hold."""
    last: dict[str, dict[str, Any]] = {}
    for root, inputs, _targets in world.tf_applies:  # type: ignore[attr-defined]
        last[root] = inputs
    roots = {name: repo_root / "terraform" / name for name in ("bootstrap", "infra")}
    declared = {name: terraform_fake.declared(path) for name, path in roots.items()}
    defaults = {name: terraform_fake.defaults(path) for name, path in roots.items()}
    enabled = world.cloud.services.get(world.project, set())  # type: ignore[attr-defined]
    found: list[str] = []
    for entry in manifest.load().entries:
        if entry.scope != "core":
            continue
        if entry.kind == "gcp-api":
            api = entry.id.removeprefix("api.") + ".googleapis.com"
            if api not in enabled and entry.id != "api.dns":
                found.append(f"{entry.id}: {api} is not enabled in the project")
            continue
        if entry.kind != "switch" or entry.id in _OFF_BY_DESIGN:
            continue
        if "terraform/" not in (entry.apply or "") and "tfvars" not in (entry.apply or ""):
            continue  # a right the person holds (preflight.rights), not a Terraform input
        variable, attribute = _OBJECT_SWITCHES.get(entry.id, (entry.id.removeprefix("switch."), None))
        root = next((r for r in ("bootstrap", "infra") if variable in declared[r]), None)
        if root is None:
            found.append(f"{entry.id}: no root declares {variable}")
            continue
        if root not in last:
            found.append(f"{entry.id}: {root} was never applied")
            continue
        if entry.id in _NO_ON_VALUE:
            continue
        if variable in last[root]:
            value = last[root][variable]
        else:
            raw = defaults[root].get(variable, "null")
            value = json.loads(raw) if raw in ("true", "false", "null") else raw
        if attribute is not None:
            value = value.get(attribute) if isinstance(value, dict) else None
        if not _on(value):
            found.append(f"{entry.id}: {root}'s last apply carried {variable}"
                         f"{'.' + attribute if attribute else ''} = {json.dumps(value)}")
    return found
