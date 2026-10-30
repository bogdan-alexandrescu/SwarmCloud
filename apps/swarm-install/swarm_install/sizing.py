"""How big a deployment is: one person or a team of N, turned into the
capacity ceilings and runtime limits stage 0 records as `state.outputs["sizing"]`
(lane DAYONE-SIZING-ASK, part of #2688; decision 7, BGD 2026-10-30).

`propose(mode, members)` is a pure function of the constants below. The
reference shape is the live deployment's own `pool_limits`
(terraform/environments/dev/dev.tfvars): every ceiling scales with `global` by
that shape's ratios. A team of `REFERENCE_MEMBERS` lands half again above
those numbers (`global` 270 against the live 180), because the owner picked
`HEADROOM` 3, not 2; a smaller team scales down by the same ratios, and one
person gets a small deployment of their own.

Every number here is a CEILING, not a reservation: only LEASED, DISPATCHED,
STARTING and RUNNING create infrastructure demand (invariant 1), so a ceiling
nobody reaches costs nothing. The shape is the interface sheet's, exactly:

    {"mode", "members", "pool_limits": {"global", "default_tenant",
     "provider_tenant", "resource_classes", "runner_profiles", "backends"},
     "service_max_instances": {"swarm-api", "swarm-scheduler",
     "swarm-quota-broker", "swarm-reconciler"}, "pool_accounts", "budget_usd"}

`pool_limits` carries no `providers` map, so every `provider:<p>` pool falls
back to `global`; terraform's own rule (terraform/infra/variables.tf, the
pool_limits validation) then reads `global >= tenants x provider_tenant`. The
tenants are the workspaces the sizing serves: one for one person, `members` for
a team, the same count tfvars.py checks at the first infra render
(`provider_tenants`; N5, DAYONE-SIZING-TENANTS). `propose()` chooses
provider_tenant so the rule holds and never lowers `global` for it,
`with_change()` keeps it when `global` is lowered, and `problems()` refuses a
sizing that breaks it.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from swarm_common.profiles import RESOURCE_CLASSES

INDIVIDUAL = "individual"
TEAM = "team"
MODES = (INDIVIDUAL, TEAM)

# -- the constants (stated in the PR body; BGD confirms them, questions.json) ---

#: Agents one person runs at once while they work (a fan-out of a few tasks).
AGENTS_PER_ACTIVE_MEMBER = 3
#: The share of a team working at the same moment. One person is always 1.0.
ACTIVE_SHARE = 0.6
#: Ceilings sit this far above the expected concurrency: room for AIMD to
#: search in and for a burst, never a promise (dev.tfvars, provider_tenant).
#: 3, not the 2 that reproduces the live dev.tfvars for a team of 50: the
#: owner picked "More headroom" (PR #2797), so 1/5/15/50 people get a global
#: of 10/27/81/270. A ceiling nobody reaches costs nothing (invariant 1).
HEADROOM = 3
#: The smallest `global` ceiling, so one person can still run a small fan-out.
MIN_GLOBAL = 10
#: Concurrent agents one Claude subscription sustains: "three accounts at
#: roughly five concurrent agents each" (dev.tfvars, provider_tenant).
AGENTS_PER_ACCOUNT = 5
#: The team size the live deployment's pool_limits serve, and that shape.
REFERENCE_MEMBERS = 50
REFERENCE_GLOBAL = 180
REFERENCE_DEFAULT_TENANT = 40
REFERENCE_PROVIDER_TENANT = 100
#: The live resource:<class> ceilings, in units (a large task holds
#: RESOURCE_CLASSES["large"].units of them).
REFERENCE_RESOURCE_CLASSES = {"standard": 80, "browser": 100, "large": 40}
#: The live runner:<profile> ceilings (mock is the test runner: not proposed).
REFERENCE_RUNNER_PROFILES = {"claude-code": 80, "claude-code-browser": 100, "codex": 20, "generic": 20,
                             "browser": 20}
#: The live backend:<backend> ceilings.
REFERENCE_BACKENDS = {"CLOUD_RUN_JOB": 100, "GKE_AUTOPILOT": 180}
#: Team members per swarm-api instance, and the floor (two: one can roll).
MEMBERS_PER_API_INSTANCE = 10
MIN_API_INSTANCES = 2
#: From this many members the scheduler and the quota broker run two instances.
HA_FROM_MEMBERS = 10
#: The monthly budget: the fixed floor (the cluster, NAT, swarm-api's minimum
#: instances, Firestore) plus the expected agents' pods for the working hours.
BUDGET_FLOOR_USD = 300
#: One standard pod on Autopilot in us-central1: 4 vCPU x $0.0445 + 8 GiB x $0.0049.
USD_PER_AGENT_HOUR = 0.22
#: 8 hours x 22 working days.
ACTIVE_HOURS_PER_MONTH = 176
BUDGET_STEP_USD = 100
#: Regional CPUs kept for the cluster's own system pods before any agent's.
SYSTEM_CPUS = 8
#: The vCPU one admission unit requests at most (standard and browser: 4 for 1 unit).
CPU_PER_UNIT = max(math.ceil(c.cpu / c.units) for c in RESOURCE_CLASSES.values())

SERVICES = ("swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler")


class Sizing(TypedDict):
    """The interface sheet's `state.outputs["sizing"]`. `pool_limits` holds the
    keys of terraform/infra's `pool_limits` variable: "global",
    "default_tenant", "provider_tenant", "resource_classes",
    "runner_profiles" and "backends"."""

    mode: str
    members: int
    pool_limits: dict[str, Any]
    service_max_instances: dict[str, int]
    pool_accounts: int
    budget_usd: int


def _scaled(reference: int, global_: int) -> int:
    """`reference` scaled from the live `global` to this one, at least 1."""
    return max(1, math.ceil(reference * global_ / REFERENCE_GLOBAL))


def cpu_cap(region_cpu_quota: int | None) -> int | None:
    """The most admission units the region's CPUS quota can run at once, or
    None when the quota is unknown or unlimited (-1)."""
    if region_cpu_quota is None or region_cpu_quota < 0:
        return None
    return max(1, (region_cpu_quota - SYSTEM_CPUS) // CPU_PER_UNIT)


def provider_tenants(mode: str, members: int) -> int:
    """The tenants that share each provider pool, as tfvars.py counts them for
    terraform's floor (global >= tenants x provider_tenant): the one person's
    own workspace, or each of a team's `members`."""
    return 1 if mode == INDIVIDUAL else members


def min_region_cpus(mode: str, members: int) -> int:
    """The smallest region CPUS quota a sizing for `mode` and `members` can
    use: the cluster's own CPUs plus one task's for every tenant, so that
    provider_tenant 1 still meets terraform's floor (global >= tenants x
    provider_tenant). Below it no number the person types can pass, so the
    increase is required, not optional (DAYONE-SIZING-CAP, audit 3 A6)."""
    return SYSTEM_CPUS + CPU_PER_UNIT * provider_tenants(mode, members)


def fits_quota(mode: str, members: int, region_cpu_quota: int | None) -> bool:
    """Whether the region's CPUS quota runs at least one task for every
    tenant (beside the cluster's own CPUs); an unknown or unlimited quota
    caps nothing. For a team this is `cpu_cap(quota) >= members`; for one
    person it also refuses a quota with no room for even one task, which
    `cpu_cap`'s floor of 1 would let through."""
    if cpu_cap(region_cpu_quota) is None:
        return True
    return int(region_cpu_quota or 0) >= min_region_cpus(mode, members)


def _fitted_provider_tenant(provider_tenant: int, global_: int, tenants: int) -> int:
    """`provider_tenant`, lowered (never below 1) so `tenants` of it fit in `global_`."""
    return max(1, min(provider_tenant, global_ // tenants))


def expected_agents(mode: str, members: int) -> int:
    """Agents expected to run at once on an ordinary working day."""
    active = members if mode == INDIVIDUAL else math.ceil(members * ACTIVE_SHARE)
    return max(1, active) * AGENTS_PER_ACTIVE_MEMBER


def propose(mode: str, members: int, *, region_cpu_quota: int | None = None) -> Sizing:
    """The proposed sizing for `mode` ("individual" or "team") and `members`.
    With `region_cpu_quota` (the region's CPUS limit) every pool ceiling is
    capped at what that quota can run."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} is not one of {MODES}")
    if isinstance(members, bool) or not isinstance(members, int) or members < 1:
        raise ValueError(f"members must be a positive whole number, not {members!r}")
    if mode == INDIVIDUAL and members != 1:
        raise ValueError(f"a deployment for one person has 1 member, not {members}")

    cap = cpu_cap(region_cpu_quota)
    expected = expected_agents(mode, members)
    if cap is not None:
        expected = min(expected, cap)
    global_ = max(MIN_GLOBAL, expected * HEADROOM)
    if cap is not None:
        global_ = min(global_, cap)

    if mode == INDIVIDUAL:
        default_tenant = global_  # the person's own workspace is the one tenant
        provider_tenant = global_
    else:
        default_tenant = max(AGENTS_PER_ACTIVE_MEMBER * HEADROOM, _scaled(REFERENCE_DEFAULT_TENANT, global_))
        provider_tenant = _scaled(REFERENCE_PROVIDER_TENANT, global_)
    pools: dict[str, Any] = {
        "global": global_,
        "default_tenant": min(default_tenant, global_),
        "provider_tenant": _fitted_provider_tenant(provider_tenant, global_, provider_tenants(mode, members)),
        "resource_classes": {k: min(_scaled(v, global_), global_) for k, v in REFERENCE_RESOURCE_CLASSES.items()},
        "runner_profiles": {k: min(_scaled(v, global_), global_) for k, v in REFERENCE_RUNNER_PROFILES.items()},
        "backends": {k: min(_scaled(v, global_), global_) for k, v in REFERENCE_BACKENDS.items()},
    }
    ha = 2 if mode == TEAM and members >= HA_FROM_MEMBERS else 1
    services = {
        "swarm-api": max(MIN_API_INSTANCES, math.ceil(members / MEMBERS_PER_API_INSTANCE)),
        "swarm-scheduler": ha,
        "swarm-quota-broker": ha,
        "swarm-reconciler": 1,
    }
    budget = BUDGET_FLOOR_USD + expected * USD_PER_AGENT_HOUR * ACTIVE_HOURS_PER_MONTH
    return Sizing(
        mode=mode,
        members=members,
        pool_limits=pools,
        service_max_instances=services,
        pool_accounts=max(1, math.ceil(expected / AGENTS_PER_ACCOUNT)),
        budget_usd=int(math.ceil(budget / BUDGET_STEP_USD) * BUDGET_STEP_USD),
    )


def capped(sizing: Mapping[str, Any], region_cpu_quota: int | None) -> Sizing:
    """A copy of `sizing` (the person's own numbers kept) with every pool
    ceiling lowered to what `region_cpu_quota` runs. provider_tenant is fitted
    again after every ceiling is capped, never set back from the original row
    (audit 3 A6: a team of 2 at 32 CPUs got global 6 and provider_tenant 6),
    so tenants x provider_tenant fits wherever `fits_quota` holds."""
    cap = cpu_cap(region_cpu_quota)
    changed: Sizing = copy.deepcopy(dict(sizing))  # type: ignore[assignment]
    if cap is None:
        return changed
    for path, value in rows(sizing):
        if path[0] == "pool_limits" and value > cap:
            changed = with_change(changed, path, cap)
    pools = changed["pool_limits"]
    pools["provider_tenant"] = _fitted_provider_tenant(
        pools["provider_tenant"], pools["global"], provider_tenants(changed["mode"], changed["members"]))
    return changed


# -- the numbers a person may change, one at a time ------------------------------

#: A path into the sizing, e.g. ("pool_limits", "resource_classes", "browser").
Path = tuple[str, ...]


def rows(sizing: Mapping[str, Any]) -> list[tuple[Path, int]]:
    """Every number the person may change, in the order the table shows them."""
    found: list[tuple[Path, int]] = []
    pools = sizing["pool_limits"]
    for key in ("global", "default_tenant", "provider_tenant"):
        found.append((("pool_limits", key), pools[key]))
    for group in ("resource_classes", "runner_profiles", "backends"):
        for name, value in pools[group].items():
            found.append((("pool_limits", group, name), value))
    for name, value in sizing["service_max_instances"].items():
        found.append((("service_max_instances", name), value))
    found.append((("pool_accounts",), sizing["pool_accounts"]))
    found.append((("budget_usd",), sizing["budget_usd"]))
    return found


def label(path: Sequence[str]) -> str:
    """A row's name as the table shows it."""
    names = {
        ("pool_limits", "global"): "tasks at once, the whole deployment (global)",
        ("pool_limits", "default_tenant"): "tasks at once, one workspace or team (default_tenant)",
        ("pool_limits", "provider_tenant"): "tasks at once on one provider, per workspace (provider_tenant)",
        ("pool_accounts",): "Claude subscription accounts to add",
        ("budget_usd",): "monthly budget, USD",
    }
    if tuple(path) in names:
        return names[tuple(path)]
    kinds = {"resource_classes": "units of class", "runner_profiles": "tasks of profile",
             "backends": "tasks on backend", "service_max_instances": "max instances of"}
    return f"{kinds[path[-2]]} {path[-1]}"


def table(sizing: Mapping[str, Any]) -> str:
    """The proposal as a short numbered table."""
    found = rows(sizing)
    width = max(len(label(p)) for p, _ in found)
    who = "one person" if sizing["mode"] == INDIVIDUAL else f"a team of {sizing['members']}"
    lines = [f"Proposed sizing for {who}:"]
    lines += [f"  {n:>2}) {label(p):<{width}}  {v}" for n, (p, v) in enumerate(found, 1)]
    return "\n".join(lines)


def with_change(sizing: Mapping[str, Any], path: Sequence[str], value: int) -> Sizing:
    """A copy of `sizing` with the number at `path` set to `value`. A new
    `global` lowers provider_tenant to what fits (tenants x provider_tenant <=
    global); a provider_tenant the person types is kept as typed, and
    `problems()` refuses it when it does not fit."""
    changed = copy.deepcopy(dict(sizing))
    node: Any = changed
    for key in path[:-1]:
        node = node[key]
    if path[-1] not in node:
        raise KeyError(f"{'.'.join(path)} is not a number in the sizing")
    node[path[-1]] = value
    if tuple(path) == ("pool_limits", "global") and isinstance(value, int) and value >= 1:
        pools = changed["pool_limits"]
        members = changed.get("members")
        if isinstance(pools.get("provider_tenant"), int) and isinstance(members, int) and members >= 1:
            pools["provider_tenant"] = _fitted_provider_tenant(
                pools["provider_tenant"], value, provider_tenants(changed.get("mode"), members))
    return changed  # type: ignore[return-value]


def problems(sizing: Mapping[str, Any], *, region_cpu_quota: int | None = None) -> list[str]:
    """What is wrong with `sizing`: empty when terraform (the check tfvars.py
    runs at the first infra render) and the region's quota would both take it."""
    found: list[str] = []
    if sizing.get("mode") not in MODES:
        found.append(f"mode {sizing.get('mode')!r} is not one of {MODES}")
    members = sizing.get("members")
    if isinstance(members, bool) or not isinstance(members, int) or members < 1:
        found.append(f"members {members!r} is not a positive whole number")
    elif sizing.get("mode") == INDIVIDUAL and members != 1:
        found.append(f"a deployment for one person has 1 member, not {members}")
    try:
        numbers = rows(sizing)
    except (KeyError, TypeError, AttributeError) as exc:
        return found + [f"the sizing is missing {exc}"]
    for path, value in numbers:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            found.append(f"{'.'.join(path)} must be a positive whole number, not {value!r}")
    if found:
        return found
    pools = sizing["pool_limits"]
    tenants = provider_tenants(sizing["mode"], members)
    if pools["global"] < tenants * pools["provider_tenant"]:
        found.append(
            f"global ({pools['global']}) is below {tenants} x provider_tenant ({pools['provider_tenant']}): every "
            "provider pool falls back to global, and terraform refuses a provider pool below tenants x "
            "provider_tenant (terraform/infra/variables.tf, pool_limits)"
            + (f"; even provider_tenant 1 needs global {tenants}, so raise global (a smaller region CPUS quota "
               "than that needs an increase on the Quotas page)" if pools["global"] < tenants else
               f"; lower provider_tenant to {pools['global'] // tenants} or raise global")
        )
    missing = [s for s in SERVICES if s not in sizing["service_max_instances"]]
    if missing:
        found.append(f"service_max_instances names no {', '.join(missing)}")
    cap = cpu_cap(region_cpu_quota)
    if cap is not None:
        over = [f"{'.'.join(p)} {v}" for p, v in numbers if p[0] == "pool_limits" and v > cap]
        if over:
            found.append(f"above the {cap} tasks the region's {region_cpu_quota} CPUs can run "
                         f"({CPU_PER_UNIT} vCPU each, {SYSTEM_CPUS} kept for the cluster): {', '.join(over)}")
    return found
