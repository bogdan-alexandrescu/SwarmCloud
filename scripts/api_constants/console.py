"""The `console` group: the swarm-api and worker values the console restates.

`swarm_common`'s enums are the `states` group's. Here:

  * `blocked_reason_groups`: which refusals need an action and which are only a
    full pool (`swarm_api.headroom`);
  * `admission_example`: `analyse_profile`'s own answer for each profile over
    `EXAMPLE_POOLS`, the pools the console's fixture serves. The analyser is an
    algorithm, so a table of constants cannot hold a console to it; its output
    over stated inputs can;
  * `outcome_sections`: the ledger's sections (`swarm_api.outcomes`), and
    `outcome_page_asks`: the sections each read of the Timeline page asks for.
    tests/unit/control_plane/test_outcomes_sections.py prices the page by these
    asks and the console's test holds the page to them, so a card that starts
    asking for more fails until it is priced;
  * `voice`: the Voice it card catalogue (`swarm_api.voice.cards`);
  * `dispatch_timeout_seconds`: the default dispatch deadline
    (`swarm_common.config.Settings`), which the console's stall threshold must
    sit above;
  * `task_id_env`: the variable the worker hands the agent its task id in.
"""

from __future__ import annotations

from typing import Any

INPUTS = (
    "scripts/api_constants/console.py",
    "apps/swarm-api/swarm_api/",
    "apps/agent-worker/agent_worker/",
    "apps/common/swarm_common/",
    "apps/jev-client/swarm_jev/",
    "apps/redaction/swarm_redaction/",
    "apps/workflow-rollup/swarm_rollup/",
)

#: The pools behind `admission_example`, as (hard_limit, active, and the
#: adaptive target and quota-derived limit its quota document gives it).
#: Three pools tie at 15 free and `provider:anthropic:tenant:u-owner` is full, so
#: the example shows both a refusal and a ceiling that is pointless to raise
#: alone. `runner:claude-code-browser` is absent on purpose: an uncapped pool.
EXAMPLE_POOLS: dict[str, tuple[int, int, int | None, int | None]] = {
    "global": (22, 7, None, None),
    "tenant:u-owner": (40, 7, None, None),
    "provider:anthropic": (10, 5, None, None),
    "provider:anthropic:tenant:u-owner": (5, 5, 6, 6),
    "provider:openai": (10, 0, None, None),
    "provider:openai:tenant:u-owner": (10, 0, None, 50),
    "runner:claude-code": (10, 5, None, None),
    "runner:mock": (20, 0, None, None),
    "runner:browser": (4, 0, None, None),
    "runner:codex": (10, 0, None, None),
    "runner:generic": (10, 0, None, None),
    "resource:standard": (20, 5, None, None),
    "resource:browser": (4, 0, None, None),
    "backend:CLOUD_RUN_JOB": (20, 5, None, None),
    "backend:GKE_AUTOPILOT": (4, 0, None, None),
}

#: The tenant whose pool lists the example's profiles take.
EXAMPLE_TENANT = "u-owner"

#: The profiles the example analyses, each with the backend the console's
#: fixture runs it on. The backend is an INPUT of the example, not a claim about
#: the catalogue: `resolve_backend` places `generic`, `claude-code` and `codex`
#: on GKE_AUTOPILOT now and the fixture still shows them on Cloud Run. The
#: provider, the class and its units are read live.
EXAMPLE_PROFILES: dict[str, str] = {
    "mock": "CLOUD_RUN_JOB",
    "generic": "CLOUD_RUN_JOB",
    "claude-code": "CLOUD_RUN_JOB",
    "codex": "CLOUD_RUN_JOB",
    "browser": "GKE_AUTOPILOT",
    "claude-code-browser": "GKE_AUTOPILOT",
}

#: What each read of the Timeline page asks for: the headline, its second read,
#: and each card when it scrolls into view.
OUTCOME_PAGE_ASKS: dict[str, Any] = {
    "headline": ["buckets", "totals"],
    "late": ["coverage", "previous"],
    "cards": {
        "failures": ["buckets", "totals"],
        "retries": ["buckets", "totals", "retries"],
        "latency": ["buckets", "totals", "latency"],
        "reliability": ["buckets", "totals", "groups"],
        "workflows": ["buckets", "totals", "workflows_failed"],
        "cost": ["buckets", "totals"],
        "cancels": ["buckets", "totals"],
    },
}


def _admission_example() -> dict[str, Any]:
    from swarm_api.headroom import analyse_profile
    from swarm_common.models import SlotPool, pool_names_for
    from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

    pools = {
        name: SlotPool(name=name, hard_limit=hard, active=active,
                       adaptive_target=adaptive, quota_derived_limit=derived)
        for name, (hard, active, adaptive, derived) in EXAMPLE_POOLS.items()
    }
    profiles: dict[str, Any] = {}
    for name, backend in EXAMPLE_PROFILES.items():
        profile = RUNNER_PROFILES[name]
        required = pool_names_for(
            tenant_id=EXAMPLE_TENANT,
            provider=profile.provider,
            resource_class=profile.resource_class,
            runner_profile=name,
            backend=Backend(backend).value,
        )
        units = RESOURCE_CLASSES[profile.resource_class].units
        readable = {n: pools[n] for n in required if n in pools}
        profiles[name] = {
            "units": units,
            "pools": required,
            "admission": analyse_profile(required=required, pools=readable, units=units),
        }
    return {
        "pools": [
            {
                "name": p.name,
                "hard_limit": p.hard_limit,
                "quota_derived_limit": p.quota_derived_limit,
                "adaptive_target": p.adaptive_target,
                "active": p.active,
                "enabled": p.enabled,
                "effective_limit": p.effective_limit,
                "available": p.available,
            }
            for p in pools.values()
        ],
        "profiles": profiles,
    }


def _task_id_env() -> str:
    """The one task-id variable the agent CLI's environment is given."""
    from agent_worker.runners.cliagent import _PLAIN_PASSTHROUGH

    names = [name for name in _PLAIN_PASSTHROUGH if name.endswith("TASK_ID")]
    if len(names) != 1:
        raise SystemExit(f"the agent's environment carries {names}, not one task-id variable")
    return names[0]


def _voice() -> dict[str, Any]:
    from swarm_api.voice import cards

    return {
        "card_kinds": [
            {
                "kind": k.kind,
                "label": k.label,
                "content": k.content,
                "fields": [f.name for f in k.fields],
                "states": list(k.states),
                "actions": list(k.actions),
            }
            for k in cards.KINDS
        ],
        "card_fields": [f.name for f in cards.ENVELOPE],
        "card_states": list(cards.STATES),
        "card_creators": list(cards.CREATORS),
        "card_references": list(cards.REFERENCES),
        "card_contents": [{"content": c.content, "renderer": c.renderer} for c in cards.CONTENTS],
        "common_actions": list(cards.COMMON_ACTIONS),
        "deciding_actions": list(cards.DECIDING_ACTIONS),
        "navigate_destinations": list(cards.NAVIGATE_DESTINATIONS),
    }


def build() -> dict[str, Any]:
    from swarm_api.headroom import blocked_reason_groups
    from swarm_api.outcomes import SECTIONS
    from swarm_common.config import Settings

    return {
        "blocked_reason_groups": blocked_reason_groups(),
        "admission_example": _admission_example(),
        "outcome_sections": list(SECTIONS),
        "outcome_page_asks": OUTCOME_PAGE_ASKS,
        "voice": _voice(),
        "dispatch_timeout_seconds": Settings(project_id="x").dispatch_timeout_seconds,
        "task_id_env": _task_id_env(),
    }
