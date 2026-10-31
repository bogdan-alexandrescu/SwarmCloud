"""`swarm_install.sizing` (DAYONE-SIZING-ASK, part of #2688; decision 7).

`propose()` is a pure function: one person gets a small deployment, a team
the size the live deployment serves lands half again above the live
pool_limits (terraform/environments/dev/dev.tfvars; the owner picked "More
headroom": 3x the expected concurrency, not 2x), every ceiling is capped at the
region's CPU quota when it is known, and terraform's own floor
(global >= tenants x provider_tenant, a team's tenants being its `members`, the
rule tfvars.py renders by) holds for every proposal and is enforced on every
edit (DAYONE-SIZING-TENANTS, N5).
"""

from __future__ import annotations

import pytest

from swarm_install import sizing, tfvars

#: The interface sheet's keys, exactly.
POOL_KEYS = {"global", "default_tenant", "provider_tenant", "resource_classes", "runner_profiles", "backends"}
SERVICES = {"swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"}
SHAPES = [("individual", 1), ("team", 1), ("team", 2), ("team", 5), ("team", 15), ("team", 50), ("team", 200)]


def _tenants(found: dict) -> int:
    return 1 if found["mode"] == "individual" else found["members"]


@pytest.mark.parametrize(("mode", "members"), SHAPES)
def test_proposal_has_the_interface_sheet_shape(mode: str, members: int) -> None:
    found = sizing.propose(mode, members)
    assert set(found) == {"mode", "members", "pool_limits", "service_max_instances", "pool_accounts", "budget_usd"}
    assert found["mode"] == mode and found["members"] == members
    assert set(found["pool_limits"]) == POOL_KEYS
    assert set(found["service_max_instances"]) == SERVICES
    assert set(found["pool_limits"]["resource_classes"]) == {"standard", "browser", "large"}
    assert set(found["pool_limits"]["backends"]) == {"CLOUD_RUN_JOB", "GKE_AUTOPILOT"}
    assert "mock" not in found["pool_limits"]["runner_profiles"]
    for path, value in sizing.rows(found):
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 1, path
    assert sizing.problems(found) == []


@pytest.mark.parametrize(("mode", "members"), SHAPES)
@pytest.mark.parametrize("quota", [None, -1, 0, 12, 48, 108, 3000])
def test_terraform_floor_holds_for_every_proposal(mode: str, members: int, quota: int | None) -> None:
    """terraform/infra/variables.tf: a provider pool (global, here: no
    providers map) is at least tenants x provider_tenant."""
    found = sizing.propose(mode, members, region_cpu_quota=quota)
    pools = found["pool_limits"]
    if pools["global"] < _tenants(found):
        # The region's CPUs run fewer tasks than the team has tenants: no
        # provider_tenant fits, and problems() says so (asked again, not passed).
        assert "x provider_tenant" in " ".join(sizing.problems(found, region_cpu_quota=quota))
    else:
        assert pools["global"] >= _tenants(found) * pools["provider_tenant"]
        assert sizing.problems(found, region_cpu_quota=quota) == []
    for group in ("resource_classes", "runner_profiles", "backends"):
        assert all(v <= pools["global"] for v in pools[group].values()), group
    assert pools["default_tenant"] <= pools["global"]


@pytest.mark.parametrize("members", range(1, 201))
def test_every_team_size_passes_the_render_tfvars_runs(members: int) -> None:
    """N5: the proposal goes through the same check the first infra render
    runs (tfvars.pool_limits, imported, not restated) and is never refused."""
    mode = "individual" if members == 1 else "team"
    for found in (sizing.propose(mode, members), sizing.propose("team", members)):
        assert tfvars.pool_limits(found) == found["pool_limits"]
        assert sizing.problems(found) == []


@pytest.mark.parametrize("members", [2, 5, 15, 50, 200])
def test_problems_refuse_what_the_render_refuses(members: int) -> None:
    """problems() counts a team's members as its tenants, as tfvars.py does,
    so preflight asks again instead of passing a sizing the render refuses."""
    found = sizing.propose("team", members)
    pools = found["pool_limits"]
    broken = sizing.with_change(found, ("pool_limits", "provider_tenant"), pools["global"] // members + 1)
    with pytest.raises(ValueError, match="pool_limits validation"):
        tfvars.pool_limits(broken)
    assert sizing.problems(broken)[0].startswith(f"global ({pools['global']}) is below {members} x provider_tenant")


@pytest.mark.parametrize("members", [1, 5, 15, 50, 200])
def test_lowering_global_lowers_provider_tenant_to_fit(members: int) -> None:
    """with_change() keeps members x provider_tenant <= global when the person
    lowers global, by lowering the per-tenant ceiling, never global."""
    mode = "individual" if members == 1 else "team"
    for new_global in (members, members + 1, 2 * members + 1, 999):
        changed = sizing.with_change(sizing.propose(mode, members), ("pool_limits", "global"), new_global)
        assert changed["pool_limits"]["global"] == new_global
        assert sizing.problems(changed) == []
        assert tfvars.pool_limits(changed)["global"] == new_global


def test_one_person_gets_a_small_deployment() -> None:
    found = sizing.propose("individual", 1)
    pools = found["pool_limits"]
    assert pools["global"] == sizing.MIN_GLOBAL == 10
    assert pools["default_tenant"] == pools["provider_tenant"] == pools["global"]  # their workspace is the one tenant
    assert found["service_max_instances"] == {"swarm-api": 2, "swarm-scheduler": 1, "swarm-quota-broker": 1,
                                              "swarm-reconciler": 1}
    assert found["pool_accounts"] == 1
    assert found["budget_usd"] == 500


def test_a_team_the_live_size_lands_above_the_live_values() -> None:
    """The ceilings sit 3x above the expected concurrency (the owner's "More
    headroom"), so a team the live size gets dev.tfvars' pool_limits (global
    180, default_tenant 40, standard 80, browser 100, large 40, the runner and
    backend ceilings) half again over, in the same ratios; service_max_instances
    stays the live 5/2/2/1. provider_tenant is the one exception: 50 tenants
    share the provider pool, so it is global // 50 (N5)."""
    assert sizing.HEADROOM == 3
    found = sizing.propose("team", sizing.REFERENCE_MEMBERS)
    assert found["pool_limits"] == {
        "global": 270, "default_tenant": 60, "provider_tenant": 5,  # 270 // 50 tenants
        "resource_classes": {"standard": 120, "browser": 150, "large": 60},
        "runner_profiles": {"claude-code": 120, "claude-code-browser": 150, "codex": 30, "generic": 30, "browser": 30},
        "backends": {"CLOUD_RUN_JOB": 150, "GKE_AUTOPILOT": 270},
    }
    assert found["service_max_instances"] == {"swarm-api": 5, "swarm-scheduler": 2, "swarm-quota-broker": 2,
                                              "swarm-reconciler": 1}
    assert found["pool_accounts"] == 18
    assert found["budget_usd"] == 3800


def test_the_table_for_1_5_15_and_50() -> None:
    """The table the PR body states."""
    got = {n: sizing.propose("individual" if n == 1 else "team", n) for n in (1, 5, 15, 50)}
    assert [got[n]["pool_limits"]["global"] for n in (1, 5, 15, 50)] == [10, 27, 81, 270]
    assert [got[n]["pool_limits"]["default_tenant"] for n in (1, 5, 15, 50)] == [10, 9, 18, 60]
    assert [got[n]["pool_limits"]["provider_tenant"] for n in (1, 5, 15, 50)] == [10, 5, 5, 5]
    assert [got[n]["service_max_instances"]["swarm-api"] for n in (1, 5, 15, 50)] == [2, 2, 2, 5]
    assert [got[n]["pool_accounts"] for n in (1, 5, 15, 50)] == [1, 2, 6, 18]
    assert [got[n]["budget_usd"] for n in (1, 5, 15, 50)] == [500, 700, 1400, 3800]


def test_ceilings_grow_with_the_team() -> None:
    sizes = [sizing.propose("team", n)["pool_limits"]["global"] for n in (1, 2, 5, 15, 50, 200)]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]


def test_every_ceiling_capped_at_the_region_cpu_quota() -> None:
    """(quota - SYSTEM_CPUS) / CPU_PER_UNIT tasks at once: 48 CPUs run 10."""
    assert sizing.CPU_PER_UNIT == 4 and sizing.SYSTEM_CPUS == 8
    assert sizing.cpu_cap(48) == 10
    assert sizing.cpu_cap(None) is None and sizing.cpu_cap(-1) is None
    assert sizing.cpu_cap(0) == 1
    found = sizing.propose("team", 5, region_cpu_quota=48)
    assert all(v <= 10 for p, v in sizing.rows(found) if p[0] == "pool_limits")
    assert found["pool_limits"]["global"] == 10
    assert found["pool_limits"]["provider_tenant"] == 2  # 10 // 5 tenants
    assert found["pool_accounts"] == 2  # the 10 the quota runs, at 5 agents an account
    assert sizing.problems(found, region_cpu_quota=48) == []
    tfvars.pool_limits(found)
    assert sizing.propose("team", 50, region_cpu_quota=3000) == sizing.propose("team", 50)
    assert sizing.propose("team", 50, region_cpu_quota=-1) == sizing.propose("team", 50)


def test_a_team_larger_than_the_quota_runs_is_asked_again() -> None:
    """48 CPUs run 10 tasks, fewer than a team of 50's tenants: no
    provider_tenant fits terraform's rule, so problems() says so."""
    found = sizing.propose("team", 50, region_cpu_quota=48)
    assert found["pool_limits"]["global"] == 10
    assert any("50 x provider_tenant" in p and "CPUS" in p for p in sizing.problems(found, region_cpu_quota=48))


def test_capped_keeps_the_persons_numbers_under_the_cap() -> None:
    mine = sizing.with_change(sizing.propose("team", 15), ("budget_usd",), 9000)
    lowered = sizing.capped(mine, 108)  # 25 tasks
    assert lowered["pool_limits"]["global"] == 25
    assert lowered["pool_limits"]["provider_tenant"] == 1  # 25 // 15 tenants
    assert lowered["pool_limits"]["runner_profiles"]["codex"] == 9  # already under the cap: kept
    assert lowered["pool_limits"]["runner_profiles"]["claude-code"] == 25
    assert lowered["service_max_instances"]["swarm-api"] == 2  # not a pool ceiling: kept
    assert lowered["budget_usd"] == 9000
    assert sizing.problems(lowered, region_cpu_quota=108) == []
    tfvars.pool_limits(lowered)
    assert sizing.capped(mine, None) == mine
    assert mine["pool_limits"]["global"] == 81  # never changed in place


@pytest.mark.parametrize(("mode", "members"), [("solo", 1), ("team", 0), ("team", -3), ("individual", 2),
                                               ("team", True), ("team", 2.5)])
def test_bad_inputs_are_refused(mode: str, members: object) -> None:
    with pytest.raises(ValueError):
        sizing.propose(mode, members)  # type: ignore[arg-type]


def test_problems_name_every_refused_edit() -> None:
    base = sizing.propose("team", 15)
    assert sizing.problems(sizing.with_change(base, ("pool_limits", "provider_tenant"), 6))[0].startswith(
        "global (81) is below 15 x provider_tenant (6)")
    assert sizing.problems(sizing.with_change(base, ("budget_usd",), 0)) == [
        "budget_usd must be a positive whole number, not 0"]
    assert "above the 10 tasks" in sizing.problems(base, region_cpu_quota=48)[0]
    assert sizing.problems({**base, "members": 0})
    assert sizing.problems({**base, "mode": "individual"})  # one person has 1 member
    assert sizing.problems({"mode": "team", "members": 3})[0].startswith("the sizing is missing")
    missing = {**base, "service_max_instances": {"swarm-api": 2, "swarm-scheduler": 1, "swarm-quota-broker": 1}}
    assert "swarm-reconciler" in " ".join(sizing.problems(missing))
    with pytest.raises(KeyError):
        sizing.with_change(base, ("pool_limits", "nothing"), 3)


def test_table_numbers_every_row() -> None:
    shown = sizing.table(sizing.propose("team", 15))
    lines = shown.splitlines()
    assert lines[0] == "Proposed sizing for a team of 15:"
    assert len(lines) == 1 + len(sizing.rows(sizing.propose("team", 15)))
    assert "1) tasks at once, the whole deployment (global)" in lines[1] and lines[1].endswith("81")
    assert lines[-1].endswith("1400") and "monthly budget" in lines[-1]


# -- DAYONE-SIZING-CAP (audit 3, A6): a quota too small for the team is a wait ----------------

#: The audit's quotas against one person and teams of 2, 5, 10 and 50.
AUDIT_QUOTAS = (24, 32, 96)
AUDIT_SHAPES = [("individual", 1), ("team", 2), ("team", 5), ("team", 10), ("team", 50)]


@pytest.mark.parametrize("quota", AUDIT_QUOTAS)
@pytest.mark.parametrize(("mode", "members"), AUDIT_SHAPES)
def test_the_audits_quotas_either_fit_or_need_the_increase(mode: str, members: int, quota: int) -> None:
    """A6: a quota whose tasks cover every tenant gives a proposal (and a cap of
    the uncapped one) that terraform's rule takes; a smaller one cannot fit
    even provider_tenant 1, so the increase is required, not optional."""
    tenants = sizing.provider_tenants(mode, members)
    room = sizing.cpu_cap(quota)
    assert room is not None
    if room >= tenants:
        assert sizing.fits_quota(mode, members, quota)
        assert sizing.min_region_cpus(mode, members) <= quota
        for found in (sizing.propose(mode, members, region_cpu_quota=quota),
                      sizing.capped(sizing.propose(mode, members), quota)):
            assert sizing.problems(found, region_cpu_quota=quota) == []
            assert found["pool_limits"]["global"] >= tenants * found["pool_limits"]["provider_tenant"]
            tfvars.pool_limits(found)
    else:
        assert not sizing.fits_quota(mode, members, quota)
        assert sizing.min_region_cpus(mode, members) > quota


def test_the_audits_numbers_split_as_it_said() -> None:
    """Quota 24 runs 4 tasks, 32 runs 6 and 96 runs 22 (4 vCPU each, 8 kept)."""
    needs = {(q, m): sizing.fits_quota(mode, m, q) for q in AUDIT_QUOTAS for mode, m in AUDIT_SHAPES}
    assert [m for (q, m), ok in needs.items() if q == 24 and not ok] == [5, 10, 50]
    assert [m for (q, m), ok in needs.items() if q == 32 and not ok] == [10, 50]
    assert [m for (q, m), ok in needs.items() if q == 96 and not ok] == [50]


@pytest.mark.parametrize(("mode", "members", "cpus"), [("individual", 1, 12), ("team", 2, 16), ("team", 5, 28),
                                                       ("team", 10, 48), ("team", 50, 208)])
def test_the_smallest_quota_a_sizing_can_use(mode: str, members: int, cpus: int) -> None:
    """The cluster's own CPUs plus one task's CPUs for every tenant: one below
    it cannot fit provider_tenant 1, it itself can."""
    assert sizing.min_region_cpus(mode, members) == cpus
    assert sizing.fits_quota(mode, members, cpus)
    assert not sizing.fits_quota(mode, members, cpus - 1)
    assert sizing.fits_quota(mode, members, None)  # unknown or unlimited: nothing to wait for
    assert sizing.fits_quota(mode, members, -1)


def test_capping_global_refits_provider_tenant() -> None:
    """A6, as the audit ran it: a team of 2 at quota 32 was capped to global 6
    and provider_tenant 6 (the original provider_tenant row set it back)."""
    lowered = sizing.capped(sizing.propose("team", 2), 32)
    assert lowered["pool_limits"]["global"] == 6
    assert lowered["pool_limits"]["provider_tenant"] == 3
    mine = sizing.with_change(sizing.propose("team", 5), ("pool_limits", "provider_tenant"), 2)
    assert sizing.capped(mine, 96)["pool_limits"]["provider_tenant"] == 2  # already fits: the person's own kept


@pytest.mark.parametrize(("region", "all_regions", "usable"), [
    (200, 12, 12),      # a new project: the project-wide quota binds, not the region's
    (24, 3000, 24),
    (48, None, 48),     # no CPUS_ALL_REGIONS reported
    (None, 12, 12),
    (-1, 32, 32),       # an unlimited region still draws on the project-wide quota
    (-1, -1, -1),
    (None, None, None),
])
def test_usable_cpus_is_the_smaller_of_the_regional_and_project_wide_quota(
        region: int | None, all_regions: int | None, usable: int | None) -> None:
    """Audit 4a F5: every Autopilot node counts against both CPUS (regional)
    and CPUS_ALL_REGIONS (project-wide), so the sizing caps and requires on the
    smaller; a 200-CPU region in a project held to 12 cannot fit a team of 5."""
    assert sizing.usable_cpus(region, all_regions) == usable
    assert not sizing.fits_quota("team", 5, sizing.usable_cpus(200, 12))
