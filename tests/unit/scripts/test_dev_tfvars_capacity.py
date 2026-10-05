"""dev.tfvars states the capacity the live dev environment runs (owner decision 2026-10-02).

The owner doubled every capacity setting across all tenants and all runtimes on
2026-10-02. The live values were changed at ~05:20Z through
`scripts/pool-limit.sh` (pools) and `PUT /v1/admin/tenants/{id}/limits`
(tenants). The pool documents are under `ignore_changes`, so dev.tfvars is the
ceiling a NEW environment is born with and the `expected` side of
`scripts/pool-limit.sh --check`: if it does not state the live values, `--check`
reports drift and a rebuilt environment comes up at half the capacity.

This derives every pool the way terraform/infra/locals.tf does -- `global`,
`tenant:<t>` as min(max_active, capacity_units), `resource:`, `runner:`,
`backend:` and `provider:` by lookup falling back to `global`, and
`provider:<p>:tenant:<t>` from the one `provider_tenant` for every provider a
tenant declares -- over the catalogue in swarm_common.profiles, which
tests/terraform/catalogue.tftest.hcl holds terraform's mirror to.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend

REPO = Path(__file__).resolve().parents[3]
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"

#: hard_limit of every live dev pool, read 2026-10-02 after the doubling.
LIVE_POOLS = {
    "global": 100,
    "backend:CLOUD_RUN_JOB": 100,
    "backend:GKE_AUTOPILOT": 40,
    "provider:anthropic": 100,
    "provider:anthropic:tenant:eng": 40,
    "provider:anthropic:tenant:u-bogdan": 40,
    "provider:mock-provider": 100,
    "provider:mock-provider:tenant:eng": 100,
    "provider:openai": 40,
    "provider:openai:tenant:eng": 40,
    "resource:browser": 20,
    "resource:large": 40,
    "resource:standard": 80,
    "runner:browser": 20,
    "runner:claude-code": 80,
    "runner:codex": 20,
    "runner:generic": 20,
    "runner:mock": 40,
    "tenant:eng": 40,
    "tenant:smoke": 8,
    "tenant:u-bogdan": 80,
    "tenant:u-sw-c90291": 4,
}

#: Pools #628 changes in dev.tfvars, which are NOT live until an operator sets
#: them (`scripts/pool-limit.sh --pool provider:anthropic --limit 120`; the new
#: per-tenant pool is created by the apply that adds smoke's provider). smoke
#: declares anthropic so release acceptance can run its claude-code checks in
#: smoke, which lifts the provider floor to 3 x provider_tenant = 120. Move
#: these into LIVE_POOLS, with the date read, once they are live.
PENDING_POOLS = {
    "provider:anthropic": 120,
    "provider:anthropic:tenant:smoke": 40,
}

#: (max_active, capacity_units) of every live dev tenant, read 2026-10-02.
LIVE_TENANTS = {
    "eng": (40, 40),
    "smoke": (20, 8),
    "u-bogdan": (80, 80),
    "u-sw-c90291": (4, 8),
}

#: Live pools no tfvars key can create. `mock-provider` is the provider the mock
#: runner reports at runtime (agent_worker.runners.mock); it is in no catalogue
#: profile, so terraform materialises neither pool and `--check`, which compares
#: only the names terraform derives, does not look at them.
NOT_DERIVED_BY_TERRAFORM = {"provider:mock-provider", "provider:mock-provider:tenant:eng"}


def _strip_comments(text: str) -> str:
    return re.sub(r"(?m)#.*$", "", text)


def _block(text: str, name: str) -> str:
    """The body between the braces of the top-level `name = { ... }`."""
    match = re.search(rf"(?m)^{re.escape(name)}\s*=\s*\{{", text)
    assert match, f"no top-level `{name}` block in dev.tfvars"
    depth, start = 1, match.end()
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise AssertionError(f"`{name}` block in dev.tfvars is not closed")


def _entries(body: str) -> dict[str, str]:
    """Top-level `key = value` entries of a block; nested blocks keep their body."""
    out: dict[str, str] = {}
    pos = 0
    entry = re.compile(r'\s*"?([A-Za-z0-9_-]+)"?\s*=\s*')
    while pos < len(body):
        match = entry.match(body, pos)
        if not match:
            pos += 1
            continue
        key, pos = match.group(1), match.end()
        if body[pos] == "{":
            depth, start = 1, pos + 1
            pos += 1
            while depth:
                depth += {"{": 1, "}": -1}.get(body[pos], 0)
                pos += 1
            out[key] = body[start : pos - 1]
        else:
            end = body.find("\n", pos)
            end = len(body) if end == -1 else end
            out[key] = body[pos:end].strip()
            pos = end
    return out


def _numbers(body: str) -> dict[str, int]:
    return {k: int(v) for k, v in _entries(body).items()}


def _dev():
    text = _strip_comments(DEV_TFVARS.read_text())
    limits = _entries(_block(text, "pool_limits"))
    tenants = {}
    for name, body in _entries(_block(text, "tenants")).items():
        cfg = _entries(body)
        tenants[name] = {
            "max_active": int(cfg["max_active"]) if "max_active" in cfg else None,
            "capacity_units": int(cfg["capacity_units"]),
            "providers": re.findall(r'"([^"]+)"', cfg.get("providers", "[]")),
        }
    return limits, tenants


def _derived_pools() -> dict[str, int]:
    limits, tenants = _dev()
    glob = int(limits["global"])
    default_tenant = int(limits["default_tenant"])
    provider_tenant = int(limits["provider_tenant"])

    def lookup(key: str, name: str) -> int:
        return _numbers(limits.get(key, "")).get(name, glob)

    pools = {"global": glob}
    for t, cfg in tenants.items():
        max_active = cfg["max_active"] if cfg["max_active"] is not None else default_tenant
        pools[f"tenant:{t}"] = min(max_active, cfg["capacity_units"])
    for name in RESOURCE_CLASSES:
        pools[f"resource:{name}"] = lookup("resource_classes", name)
    for name in RUNNER_PROFILES:
        pools[f"runner:{name}"] = lookup("runner_profiles", name)
    for backend in ("CLOUD_RUN_JOB", "GKE_AUTOPILOT"):
        pools[f"backend:{backend}"] = lookup("backends", backend)
    providers = {str(p.provider) for p in RUNNER_PROFILES.values() if p.provider}
    for p in providers:
        pools[f"provider:{p}"] = lookup("providers", p)
    for t, cfg in tenants.items():
        for p in cfg["providers"]:
            pools[f"provider:{p}:tenant:{t}"] = provider_tenant
    return pools


def test_the_backends_in_the_catalogue_are_the_two_terraform_derives():
    assert {getattr(resolve_backend(p), "value", resolve_backend(p)) for p in RUNNER_PROFILES.values()} <= {
        "CLOUD_RUN_JOB",
        "GKE_AUTOPILOT",
    }


def test_every_live_pool_terraform_derives_is_stated_at_its_live_value():
    derived = _derived_pools()
    drift = {
        name: {"live": live, "tfvars": derived.get(name)}
        for name, live in {**LIVE_POOLS, **PENDING_POOLS}.items()
        if name not in NOT_DERIVED_BY_TERRAFORM and derived.get(name) != live
    }
    assert not drift, f"dev.tfvars disagrees with the live pools: {drift}"


def test_the_pools_tfvars_cannot_state_are_still_not_derived():
    # If a catalogue profile ever names mock-provider, terraform starts creating
    # these pools and they belong in the comparison above instead.
    assert not NOT_DERIVED_BY_TERRAFORM & set(_derived_pools())


def test_every_live_tenant_limit_is_stated_at_its_live_value():
    _, tenants = _dev()
    stated = {t: (cfg["max_active"], cfg["capacity_units"]) for t, cfg in tenants.items()}
    assert stated == LIVE_TENANTS


def test_the_doubled_provider_pools_still_satisfy_the_provider_tenant_floor():
    # variables.tf's pool_limits validation: a provider-wide pool below
    # (tenants holding it) x provider_tenant fails the plan.
    limits, tenants = _dev()
    providers = _numbers(limits["providers"])
    for p in {p for cfg in tenants.values() for p in cfg["providers"]}:
        holders = sum(1 for cfg in tenants.values() if p in cfg["providers"])
        floor = holders * int(limits["provider_tenant"])
        assert providers.get(p, int(limits["global"])) >= floor, p
