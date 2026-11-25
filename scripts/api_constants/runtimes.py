"""The `runtimes` group: the frozen catalogue's names (part of #72).

`GET /v1/runtimes` exists so the console restates no catalogue value: the
Runtimes screen and its development fixture must name no runner profile, no
resource class and no backend. The console's check needs the names to look
for; they are read here from the frozen catalogue (`swarm_common.profiles`).
"""

from __future__ import annotations

from typing import Any

INPUTS = (
    "scripts/api_constants/runtimes.py",
    "apps/common/swarm_common/",
)


def build() -> dict[str, Any]:
    from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

    return {
        "runner_profiles": sorted(RUNNER_PROFILES),
        "resource_classes": sorted(RESOURCE_CLASSES),
        "backends": [b.value for b in Backend],
    }
