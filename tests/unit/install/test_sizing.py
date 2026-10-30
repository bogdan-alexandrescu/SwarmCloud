"""`swarm_install.sizing` (DAYONE-SIZING-ASK, part of #2688; decision 7).

`propose()` is a pure function: one person gets a small deployment, a team
the size the live deployment serves lands on the live pool_limits
(terraform/environments/dev/dev.tfvars), every ceiling is capped at the
region's CPU quota when it is known, and terraform's own floor
(global >= tenants x provider_tenant) holds for every proposal and is
enforced on every edit.
"""

from __future__ import annotations

import pytest

from swarm_install import sizing

#: The interface sheet's keys, exactly.
POOL_KEYS = {"global", "default_tenant", "provider_tenant", "resource_classes", "runner_profiles", "backends"}
SERVICES = {"swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"}
SHAPES = [("individual", 1), ("team", 1), ("team", 2), ("team", 5), ("team", 15), ("team", 50), ("team", 200)]


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
    pools = sizing.propose(mode, members, region_cpu_quota=quota)["pool_limits"]
    assert pools["global"] >= sizing.PROVIDER_TENANTS * pools["provider_tenant"]
    for group in ("resource_classes", "runner_profiles", "backends"):
        assert all(v <= pools["global"] for v in pools[group].values()), group
    assert pools["default_tenant"] <= pools["global"]


def test_one_person_gets_a_small_deployment() -> None:
    found = sizing.propose("individual", 1)
    pools = found["pool_limits"]
    assert pools["global"] == sizing.MIN_GLOBAL == 10
    assert pools["default_tenant"] == pools["provider_tenant"] == pools["global"]  # their workspace is the one tenant
    assert found["service_max_instances"] == {"swarm-api": 2, "swarm-scheduler": 1, "swarm-quota-broker": 1,
                                              "swarm-reconciler": 1}
    assert found["pool_accounts"] == 1
    assert found["budget_usd"] == 500


def test_a_team_the_live_size_lands_on_the_live_values() -> None:
    """dev.tfvars: global 180, default_tenant 40, provider_tenant 100, standard
    80, browser 100, large 40, the runner and backend ceilings, and
    service_max_instances 5/2/2/1."""
    found = sizing.propose("team", sizing.REFERENCE_MEMBERS)
    assert found["pool_limits"] == {
        "global": 180, "default_tenant": 40, "provider_tenant": 100,
        "resource_classes": {"standard": 80, "browser": 100, "large": 40},
        "runner_profiles": {"claude-code": 80, "claude-code-browser": 100, "codex": 20, "generic": 20, "browser": 20},
        "backends": {"CLOUD_RUN_JOB": 100, "GKE_AUTOPILOT": 180},
    }
    assert found["service_max_instances"] == {"swarm-api": 5, "swarm-scheduler": 2, "swarm-quota-broker": 2,
                                              "swarm-reconciler": 1}
    assert found["pool_accounts"] == 18
    assert found["budget_usd"] == 3800


def test_the_table_for_1_5_15_and_50() -> None:
    """The table the PR body states."""
    got = {n: sizing.propose("individual" if n == 1 else "team", n) for n in (1, 5, 15, 50)}
    assert [got[n]["pool_limits"]["global"] for n in (1, 5, 15, 50)] == [10, 18, 54, 180]
    assert [got[n]["pool_limits"]["default_tenant"] for n in (1, 5, 15, 50)] == [10, 6, 12, 40]
    assert [got[n]["pool_limits"]["provider_tenant"] for n in (1, 5, 15, 50)] == [10, 10, 30, 100]
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
    found = sizing.propose("team", 50, region_cpu_quota=48)
    assert all(v <= 10 for p, v in sizing.rows(found) if p[0] == "pool_limits")
    assert found["pool_limits"]["global"] == 10
    assert found["pool_accounts"] == 2  # the 10 the quota runs, at 5 agents an account
    assert sizing.problems(found, region_cpu_quota=48) == []
    assert sizing.propose("team", 50, region_cpu_quota=3000) == sizing.propose("team", 50)
    assert sizing.propose("team", 50, region_cpu_quota=-1) == sizing.propose("team", 50)


def test_capped_keeps_the_persons_numbers_under_the_cap() -> None:
    mine = sizing.with_change(sizing.propose("team", 50), ("budget_usd",), 9000)
    lowered = sizing.capped(mine, 108)  # 25 tasks
    assert lowered["pool_limits"]["global"] == 25
    assert lowered["pool_limits"]["runner_profiles"]["codex"] == 20
    assert lowered["budget_usd"] == 9000
    assert sizing.problems(lowered, region_cpu_quota=108) == []
    assert sizing.capped(mine, None) == mine
    assert mine["pool_limits"]["global"] == 180  # never changed in place


@pytest.mark.parametrize(("mode", "members"), [("solo", 1), ("team", 0), ("team", -3), ("individual", 2),
                                               ("team", True), ("team", 2.5)])
def test_bad_inputs_are_refused(mode: str, members: object) -> None:
    with pytest.raises(ValueError):
        sizing.propose(mode, members)  # type: ignore[arg-type]


def test_problems_name_every_refused_edit() -> None:
    base = sizing.propose("team", 15)
    assert sizing.problems(sizing.with_change(base, ("pool_limits", "provider_tenant"), 55))[0].startswith(
        "global (54) is below 1 x provider_tenant (55)")
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
    assert "1) tasks at once, the whole deployment (global)" in lines[1] and lines[1].endswith("54")
    assert lines[-1].endswith("1400") and "monthly budget" in lines[-1]
