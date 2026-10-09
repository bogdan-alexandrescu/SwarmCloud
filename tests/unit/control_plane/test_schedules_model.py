"""The schedule model: the document, gates with floors, budgets, and the type catalogue.

docs/schedules.md §1.1, §3, §4.2, §4.3, and lane S1's acceptance (§9):
"Every catalogue entry validates its own defaults. A gate below its floor is
refused."

Pure: no Firestore, no directory, no emulator. Availability is the presence of
an executor FILE, so each test that needs a type available points the
catalogue at a temporary directory holding that file.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api import schedules, scheduletypes
from swarm_api.errors import Conflict, Forbidden, ValidationFailed
from swarm_api.scheduletypes import GATE_POINTS, BudgetCaps, strictness

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ALICE = "alice@example.com"
REPOS = ["repo_a", "repo_b"]

#: §3's table, in its order, with each type's minimum interval.
CATALOGUE = [
    ("issue-sweep", timedelta(minutes=15)),
    ("issue-plan-only", timedelta(minutes=15)),
    ("repo-index-refresh", timedelta(hours=1)),
    ("observer", timedelta(hours=1)),
    ("epic-triage", timedelta(hours=6)),
    ("pr-shepherd", timedelta(minutes=30)),
    ("ci-flake-hunter", timedelta(hours=6)),
    ("dependency-cve-refresh", timedelta(hours=24)),
    ("release-health", timedelta(minutes=15)),
    ("docs-drift", timedelta(hours=24)),
    ("cost-report", timedelta(hours=24)),
    ("custom-prompt", timedelta(hours=6)),
]


def _spec(steps: int = 1) -> dict:
    return {"steps": [{"step_id": f"s{i}", "runner_profile": "claude-code"} for i in range(steps)]}


def _required(name: str) -> dict:
    """The parameters a type cannot default: only custom-prompt's spec."""
    return {"spec": _spec()} if name == "custom-prompt" else {}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An executor directory holding the first four types' files (lane S6's)."""
    for name in ("issue_sweep", "issue_plan_only", "repo_index_refresh", "observer"):
        (tmp_path / f"{name}.py").write_text("")
    return tmp_path


def _body(**over) -> schedules.ScheduleCreate:
    raw = {
        "name": "nightly sweep",
        "type": "issue-sweep",
        "scope": {"mode": "repos", "repo_ids": ["repo_a"]},
        "cron": "0 9 * * 1-5",
        "timezone": "Europe/London",
    }
    raw.update(over)
    return schedules.parse_create(raw)


def _build(root: Path, **over):
    kwargs = {
        "tenant_id": "eng",
        "actor": ALICE,
        "now": NOW,
        "registered_repo_ids": REPOS,
        "root": root,
    }
    for key in ("is_admin", "is_owner", "existing_names", "existing_count", "schedule_id"):
        if key in over:
            kwargs[key] = over.pop(key)
    return schedules.build_schedule(_body(**over), **kwargs)


def _code(exc_info) -> str:
    return exc_info.value.code


# --------------------------------------------------------------------------
# The catalogue: twelve entries, each valid on its own defaults
# --------------------------------------------------------------------------


def test_the_catalogue_is_the_twelve_types_of_section_3_in_order():
    assert [(t.name, t.min_interval) for t in scheduletypes.TYPES] == CATALOGUE


@pytest.mark.parametrize("entry", scheduletypes.TYPES, ids=lambda t: t.name)
def test_every_entry_validates_its_own_defaults(entry):
    # The default gate is at or above the floor at every point.
    for point in GATE_POINTS:
        assert strictness(point, getattr(entry.default_gate, point)) >= strictness(
            point, getattr(entry.floor_gate, point)
        ), point
    # The default gate resolves for an ordinary member, with nothing requested.
    gate = schedules.resolve_gate(entry, None)
    assert {k: gate[k] for k in GATE_POINTS} == entry.default_gate.as_dict()
    assert gate["approvers"] == "members" and gate["approval_ttl_hours"] == 72
    # Restating the default gate is not "lowering" it.
    requested = schedules.GateIn(**{k: v for k, v in entry.default_gate.as_dict().items()})
    assert schedules.resolve_gate(entry, requested) == gate
    # The parameters' defaults validate, and a stored set re-validates unchanged.
    params = schedules.resolve_params(entry, _required(entry.name), gate=gate)
    assert schedules.resolve_params(entry, params, gate=gate) == params
    # The tier is one of §4.1's, and the default policy carries the type's catch-up.
    assert schedules.risk_tier(entry, gate, params) in schedules.TIERS
    assert schedules.resolve_policy(entry, None)["catch_up"] == entry.catch_up
    # Its caps, where the catalogue holds them, are valid defaults.
    if entry.budget is not None:
        caps = entry.budget.as_dict()
        assert schedules.resolve_budget(entry, None, root=Path("/nonexistent")) == {
            "per_run_usd": float(caps["per_run_usd"]),
            "per_day_usd": float(caps["per_day_usd"]),
            "max_concurrent": caps["max_concurrent"],
        }


def test_a_type_that_runs_an_agent_names_a_real_profile_in_code():
    from swarm_common.profiles import RUNNER_PROFILES

    for entry in scheduletypes.TYPES:
        if entry.profile is not None:
            assert entry.profile in RUNNER_PROFILES, entry.name
        else:
            assert entry.executor == "api", entry.name


@pytest.mark.parametrize("entry", scheduletypes.TYPES, ids=lambda t: t.name)
@pytest.mark.parametrize("key", ["image", "command", "runner_profile", "resource_class", "backend"])
def test_no_type_accepts_an_image_a_command_or_a_profile(entry, key):
    gate = schedules.resolve_gate(entry, None)
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_params(entry, {**_required(entry.name), key: "x"}, gate=gate)
    assert _code(caught) == "invalid_params"
    assert key in str(caught.value.detail)


@pytest.mark.parametrize("key", ["image", "command", "tenant_id", "owner", "runner_profile"])
def test_the_create_body_refuses_keys_a_caller_may_not_set(key):
    with pytest.raises(ValidationFailed) as caught:
        _body(**{key: "x"})
    assert key in str(caught.value.detail)


def test_the_first_four_types_carry_the_owners_sd4_caps():
    caps = {t.name: t.budget for t in scheduletypes.TYPES if t.budget}
    assert caps == {
        "issue-sweep": BudgetCaps(15, 120, 8),
        "issue-plan-only": BudgetCaps(3, 30, 5),
        "observer": BudgetCaps(5, 10, 1),
        "repo-index-refresh": BudgetCaps(5, 40, 4),
    }


def test_catch_up_defaults_follow_sd6():
    run_once = {t.name for t in scheduletypes.TYPES if t.catch_up == "run_once"}
    assert run_once == {"observer", "cost-report", "release-health", "docs-drift", "repo-index-refresh"}


def test_risk_tiers_follow_section_4_1():
    def tier(name, gate=None, **params):
        entry = scheduletypes.get(name)
        resolved = gate or schedules.resolve_gate(entry, None)
        return schedules.risk_tier(entry, resolved, schedules.resolve_params(entry, {**_required(name), **params}, gate=resolved))

    sweep = scheduletypes.get("issue-sweep")
    auto = schedules.resolve_gate(sweep, schedules.GateIn(merge="auto"), via_merge_switch=True)
    assert tier("issue-sweep") == "R2" and tier("issue-sweep", gate=auto) == "R3"
    assert tier("pr-shepherd") == "R1" and tier("pr-shepherd", fix_conflicts=True) == "R2"
    assert tier("docs-drift") == "R1" and tier("docs-drift", fix=True) == "R2"
    assert [tier(n) for n in ("repo-index-refresh", "observer", "cost-report")] == ["R0"] * 3
    assert [tier(n) for n in ("issue-plan-only", "epic-triage", "release-health", "ci-flake-hunter")] == ["R1"] * 4
    assert tier("dependency-cve-refresh") == "R2" and tier("custom-prompt") == "R3"


# --------------------------------------------------------------------------
# Availability is the executor file's presence
# --------------------------------------------------------------------------


def test_the_executor_directory_is_swarm_api_schedtypes():
    assert scheduletypes.EXECUTOR_DIR == Path(scheduletypes.__file__).resolve().parent / "schedtypes"


def test_no_file_no_type_and_the_reason_names_the_file_and_lane(tmp_path):
    for entry in scheduletypes.TYPES:
        available, reason = scheduletypes.availability(entry, tmp_path)
        assert not available
        assert f"swarm_api/schedtypes/{entry.module_name}.py" in reason and entry.lane in reason
    assert scheduletypes.available_names(tmp_path) == []
    # §0.1 check 1: release-health's reason also says what the App lacks.
    assert "actions: read" in scheduletypes.availability(scheduletypes.get("release-health"), tmp_path)[1]


def test_adding_a_file_makes_a_type_available_and_touches_nothing_else(root):
    assert scheduletypes.available_names(root) == ["issue-sweep", "issue-plan-only", "repo-index-refresh", "observer"]
    rows = {row["name"]: row for row in scheduletypes.catalogue(root)}
    assert rows["observer"]["available"] and rows["observer"]["disabled_reason"] == ""
    assert not rows["epic-triage"]["available"] and rows["epic-triage"]["disabled_reason"]


def test_a_later_type_takes_its_caps_from_its_own_module(tmp_path, monkeypatch):
    entry = scheduletypes.get("cost-report")
    (tmp_path / "cost_report.py").write_text("")
    # Present, but declaring no caps: still unavailable, because unbounded.
    monkeypatch.setitem(sys.modules, entry.module, types.ModuleType(entry.module))
    available, reason = scheduletypes.availability(entry, tmp_path)
    assert not available and "BUDGET_CAPS" in reason
    # Declaring them: available, and they are its defaults and caps.
    module = types.ModuleType(entry.module)
    module.BUDGET_CAPS = BudgetCaps(1, 2, 1)
    monkeypatch.setitem(sys.modules, entry.module, module)
    assert scheduletypes.availability(entry, tmp_path) == (True, "")
    assert schedules.resolve_budget(entry, None, root=tmp_path) == {
        "per_run_usd": 1.0, "per_day_usd": 2.0, "max_concurrent": 1,
    }


def test_an_unknown_or_unavailable_type_is_a_422_naming_the_available_ones(root):
    with pytest.raises(ValidationFailed) as caught:
        _build(root, type="image-builder")
    assert _code(caught) == "unknown_type"
    assert caught.value.detail["available"] == ["issue-sweep", "issue-plan-only", "repo-index-refresh", "observer"]
    with pytest.raises(ValidationFailed) as caught:
        _build(root, type="epic-triage")
    assert _code(caught) == "type_unavailable"


# --------------------------------------------------------------------------
# Gates: the floor, the default, and the merge switch
# --------------------------------------------------------------------------


def _below_floor():
    for entry in scheduletypes.TYPES:
        for point, values in GATE_POINTS.items():
            floor = getattr(entry.floor_gate, point)
            for value in values[: values.index(floor)]:
                yield pytest.param(entry, point, value, id=f"{entry.name}-{point}-{value}")


BELOW_FLOOR = list(_below_floor())


def test_there_are_points_below_a_floor_to_test():
    # Control: the parametrised test below is not vacuous.
    assert len(BELOW_FLOOR) >= 20


@pytest.mark.parametrize("entry, point, value", BELOW_FLOOR)
def test_a_gate_below_its_floor_is_refused_even_for_an_admin(entry, point, value):
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_gate(
            entry, schedules.GateIn(**{point: value}), is_admin=True, via_merge_switch=(value == "auto")
        )
    assert _code(caught) in ("gate_below_floor", "merge_auto_not_allowed")


def test_a_type_that_does_not_merge_only_takes_merge_off():
    observer = scheduletypes.get("observer")
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_gate(observer, schedules.GateIn(merge="approve"), is_admin=True)
    assert _code(caught) == "gate_below_floor" and caught.value.detail["point"] == "merge"


def test_between_floor_and_default_needs_an_admin():
    shepherd = scheduletypes.get("pr-shepherd")  # run: default approve, floor auto
    with pytest.raises(Forbidden) as caught:
        schedules.resolve_gate(shepherd, schedules.GateIn(run="auto"))
    assert _code(caught) == "gate_below_default"
    assert schedules.resolve_gate(shepherd, schedules.GateIn(run="auto"), is_admin=True)["run"] == "auto"


def test_raising_a_gate_is_always_allowed():
    observer = scheduletypes.get("observer")
    gate = schedules.resolve_gate(observer, schedules.GateIn(run="approve", plan="approve", approvers="owner_only"))
    assert (gate["run"], gate["plan"], gate["approvers"]) == ("approve", "approve", "owner_only")


def test_a_member_editing_another_point_keeps_an_admins_lowered_one():
    shepherd = scheduletypes.get("pr-shepherd")
    lowered = schedules.resolve_gate(shepherd, schedules.GateIn(run="auto"), is_admin=True)
    edited = schedules.resolve_gate(shepherd, schedules.GateIn(approval_ttl_hours=24), current=lowered)
    assert edited["run"] == "auto" and edited["approval_ttl_hours"] == 24


def test_merge_auto_is_refused_outside_the_switch_even_for_an_admin():
    sweep = scheduletypes.get("issue-sweep")
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_gate(sweep, schedules.GateIn(merge="auto"), is_admin=True)
    assert _code(caught) == "use_merge_switch"


def test_the_switch_reaches_merge_auto_only_where_the_floor_allows_it():
    sweep = scheduletypes.get("issue-sweep")
    # A member may use the switch (who may is lane S5's rule, not the arithmetic's).
    assert schedules.resolve_gate(sweep, schedules.GateIn(merge="auto"), via_merge_switch=True)["merge"] == "auto"
    for name in ("custom-prompt", "dependency-cve-refresh", "issue-plan-only", "docs-drift"):
        with pytest.raises(ValidationFailed) as caught:
            schedules.resolve_gate(
                scheduletypes.get(name), schedules.GateIn(merge="auto"), is_admin=True, via_merge_switch=True
            )
        assert _code(caught) == "merge_auto_not_allowed", name


def test_approvers_lists_are_members_emails_once_each():
    observer = scheduletypes.get("observer")
    gate = schedules.resolve_gate(observer, schedules.GateIn(approvers=["Bob@Example.com"]))
    assert gate["approvers"] == ["bob@example.com"]
    for bad in ([], ["not-an-email"], ["a@x.io", "A@x.io"]):
        with pytest.raises(ValidationFailed):
            schedules.resolve_gate(observer, schedules.GateIn(approvers=bad))


# --------------------------------------------------------------------------
# Params
# --------------------------------------------------------------------------


def test_sweep_params_follow_the_gate_and_refuse_a_contradiction():
    sweep = scheduletypes.get("issue-sweep")
    gate = schedules.resolve_gate(sweep, schedules.GateIn(plan="approve"))
    assert schedules.resolve_params(sweep, None, gate=gate)["plan_approval"] == "required"
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_params(sweep, {"plan_approval": "auto"}, gate=gate)
    assert _code(caught) == "params_disagree_with_gate"
    with pytest.raises(ValidationFailed) as caught:
        schedules.resolve_params(sweep, {"merge": "auto"}, gate=gate)
    assert _code(caught) == "use_merge_switch"


def test_the_sweep_cap_is_lane_sweeps_eight():
    sweep = scheduletypes.get("issue-sweep")
    gate = schedules.resolve_gate(sweep, None)
    assert schedules.resolve_params(sweep, {"max_live_runs": 8}, gate=gate)["max_live_runs"] == 8
    with pytest.raises(ValidationFailed):
        schedules.resolve_params(sweep, {"max_live_runs": 9}, gate=gate)


def test_only_an_admin_turns_the_territory_guard_off():
    sweep = scheduletypes.get("issue-sweep")
    gate = schedules.resolve_gate(sweep, None)
    with pytest.raises(Forbidden) as caught:
        schedules.resolve_params(sweep, {"territory_guard": False}, gate=gate)
    assert _code(caught) == "territory_guard_admin_only"
    assert schedules.resolve_params(sweep, {"territory_guard": False}, gate=gate, is_admin=True)["territory_guard"] is False


def test_custom_prompt_spec_is_a_workflow_and_bounded_by_max_steps():
    custom = scheduletypes.get("custom-prompt")
    gate = schedules.resolve_gate(custom, None)
    with pytest.raises(ValidationFailed):
        schedules.resolve_params(custom, {"spec": _spec(6)}, gate=gate)
    with pytest.raises(ValidationFailed):
        schedules.resolve_params(custom, {"spec": {"steps": [{"step_id": "s", "image": "x"}]}}, gate=gate)
    assert len(schedules.resolve_params(custom, {"spec": _spec(6), "max_steps": 6}, gate=gate)["spec"]["steps"]) == 6


@pytest.mark.parametrize("path", ["/etc", "../outside", "docs/../../x", "a b"])
def test_path_params_stay_inside_the_repository(path):
    drift = scheduletypes.get("docs-drift")
    with pytest.raises(ValidationFailed):
        schedules.resolve_params(drift, {"paths": [path]}, gate=schedules.resolve_gate(drift, None))


# --------------------------------------------------------------------------
# Budget arithmetic (§4.3)
# --------------------------------------------------------------------------

SWEEP_BUDGET = {"per_run_usd": 15.0, "per_day_usd": 120.0, "max_concurrent": 8}


def test_a_budget_above_its_cap_or_below_one_run_a_day_is_refused():
    sweep = scheduletypes.get("issue-sweep")
    root = Path("/nonexistent")
    for bad, code in (
        ({"per_run_usd": 15.01}, "budget_above_cap"),
        ({"per_day_usd": 121.0}, "budget_above_cap"),
        ({"max_concurrent": 9}, "budget_above_cap"),
        ({"max_concurrent": 0}, "budget_above_cap"),
        ({"per_run_usd": 0.0}, "invalid_budget"),
        ({"per_run_usd": 10.0, "per_day_usd": 9.99}, "invalid_budget"),
    ):
        with pytest.raises(ValidationFailed) as caught:
            schedules.resolve_budget(sweep, schedules.BudgetIn(**bad), root=root)
        assert _code(caught) == code, bad
    lowered = schedules.resolve_budget(sweep, schedules.BudgetIn(per_run_usd=5, max_concurrent=2), root=root)
    assert lowered == {"per_run_usd": 5.0, "per_day_usd": 120.0, "max_concurrent": 2}


def test_an_unknown_cost_counts_at_the_per_run_cap_never_as_zero():
    # Nothing reported, but 8 attempts with no cost: 8 x $15 = $120, not over.
    assert not schedules.budget_exhausted(SWEEP_BUDGET, reported_today=0, live_items=0, unreported_attempts=8)
    # One more unknown is $135 > $120: exhausted, though $0 was reported.
    assert schedules.budget_exhausted(SWEEP_BUDGET, reported_today=0, live_items=0, unreported_attempts=9)
    # Control: had unknown counted as zero, nothing would be exhausted.
    assert not schedules.budget_exhausted(SWEEP_BUDGET, reported_today=0, live_items=0, unreported_attempts=0)
    assert schedules.reserved_usd(SWEEP_BUDGET, live_items=2, unreported_attempts=1) == 45.0


def test_reported_spend_and_live_reservations_add():
    assert not schedules.budget_exhausted(SWEEP_BUDGET, reported_today=90, live_items=2, unreported_attempts=0)
    assert schedules.budget_exhausted(SWEEP_BUDGET, reported_today=90.01, live_items=2, unreported_attempts=0)


def test_money_is_compared_in_cents_not_floats():
    budget = {"per_run_usd": 0.1, "per_day_usd": 0.3, "max_concurrent": 3}
    # 0.1 * 3 in floats is 0.30000000000000004 > 0.3; in cents it is equal.
    assert not schedules.budget_exhausted(budget, reported_today=0, live_items=3, unreported_attempts=0)


def test_the_concurrency_room_and_the_run_over_budget_tripwire():
    assert schedules.concurrency_room(SWEEP_BUDGET, live_items=5) == 3
    assert schedules.concurrency_room(SWEEP_BUDGET, live_items=11) == 0
    assert schedules.run_over_budget(SWEEP_BUDGET, 15.01)
    assert not schedules.run_over_budget(SWEEP_BUDGET, 15.0)
    assert not schedules.run_over_budget(SWEEP_BUDGET, None)


def test_the_worst_case_day_is_what_the_budget_card_states():
    # per_day + max_concurrent x (actual - per_run) = 120 + 8 x 5
    assert schedules.worst_case_day_usd(SWEEP_BUDGET, 20.0) == 160.0
    assert schedules.worst_case_day_usd(SWEEP_BUDGET, 10.0) == 120.0


def test_the_spend_day_is_the_schedules_timezones_and_rolls_over():
    late = datetime(2026, 10, 9, 23, 30, tzinfo=UTC)
    assert schedules.spend_day(late, "UTC") == "2026-10-09"
    assert schedules.spend_day(late, "Asia/Tokyo") == "2026-10-10"
    kept = {"day": "2026-10-09", "reported_usd": 4.5, "unreported_attempts": 1, "reserved_usd": 15.0}
    assert schedules.spend_for(kept, "2026-10-09") == kept
    assert schedules.spend_for(kept, "2026-10-10") == {
        "day": "2026-10-10", "reported_usd": 0.0, "unreported_attempts": 0, "reserved_usd": 0.0,
    }


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

#: §1.1's field table, every row.
FIELDS = {
    "schedule_id", "tenant_id", "name", "type", "scope", "cron", "timezone", "params", "gate",
    "budget", "policy", "state", "pause", "owner", "created_by", "created_at", "updated_by",
    "updated_at", "next_run_at", "next_slot", "last_firing", "consecutive_failures", "spend", "revision",
}


def test_a_created_schedule_is_section_1_1s_document_fully_resolved(root):
    doc = _build(root)
    assert set(doc) == FIELDS
    assert doc["schedule_id"].startswith("sch_") and len(doc["schedule_id"]) == 16
    assert (doc["tenant_id"], doc["owner"], doc["created_by"], doc["updated_by"]) == ("eng", ALICE, ALICE, ALICE)
    assert doc["gate"] == {"run": "auto", "plan": "auto", "merge": "approve", "approvers": "members", "approval_ttl_hours": 72}
    assert doc["budget"] == SWEEP_BUDGET
    assert doc["policy"] == {"overlap": "skip", "catch_up": "skip", "jitter": True, "dry_run": False}
    assert doc["params"]["max_live_runs"] == 8 and doc["params"]["merge"] == "approve"
    assert doc["state"] == "enabled" and doc["pause"] is None and doc["revision"] == 1
    # Monday 2026-10-12 09:00 BST is the next weekday slot after Friday noon.
    assert doc["next_slot"] == datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    offset = (doc["next_run_at"] - doc["next_slot"]).total_seconds()
    assert 0 <= offset < 300
    assert doc["spend"]["day"] == "2026-10-09"


def test_a_schedule_created_paused_has_no_next_run(root):
    doc = _build(root, state="paused")
    assert doc["next_run_at"] is None and doc["next_slot"] is None
    assert doc["pause"]["by"] == ALICE


def test_jitter_is_deterministic_and_bounded():
    interval = timedelta(minutes=15)  # 10% is 90 s, under the 300 s cap
    offsets = {schedules.jitter_seconds(f"sch_{i:012x}", interval) for i in range(200)}
    assert all(0 <= o < 90 for o in offsets) and len(offsets) > 20
    assert schedules.jitter_seconds("sch_abc", interval) == schedules.jitter_seconds("sch_abc", interval)
    assert all(schedules.jitter_seconds(f"sch_{i}", timedelta(days=1)) < 300 for i in range(200))


def test_the_scope_must_be_this_tenants_registrations(root):
    with pytest.raises(ValidationFailed) as caught:
        _build(root, scope={"mode": "repos", "repo_ids": ["repo_a", "repo_other_tenant"]})
    assert _code(caught) == "repository_not_registered"
    assert caught.value.detail["repo_ids"] == ["repo_other_tenant"]
    for scope in ({"mode": "repos", "repo_ids": []}, {"mode": "repos", "repo_ids": ["repo_a", "repo_a"]},
                  {"mode": "all", "repo_ids": ["repo_a"]}):
        with pytest.raises(ValidationFailed):
            _build(root, scope=scope)
    assert _build(root, scope={"mode": "all"})["scope"] == {"mode": "all", "repo_ids": []}


def test_platform_scope_is_owner_only_for_observer_and_refused_for_the_sweep(root):
    platform = {"mode": "platform"}
    with pytest.raises(Forbidden) as caught:
        _build(root, type="observer", scope=platform, cron="0 6 * * *", is_admin=True)
    assert _code(caught) == "owner_required"
    assert _build(root, type="observer", scope=platform, cron="0 6 * * *", is_owner=True)["scope"]["mode"] == "platform"
    with pytest.raises(ValidationFailed) as caught:
        _build(root, scope=platform, is_owner=True)
    assert _code(caught) == "scope_not_allowed"


def test_creator_roles_in_a_platform_repository():
    health = scheduletypes.get("release-health")
    assert scheduletypes.creator_role(health, "repos") == "member"
    assert scheduletypes.creator_role(health, "repos", platform_repository=True) == "owner"
    sweep = scheduletypes.get("issue-sweep")
    assert scheduletypes.creator_role(sweep, "repos", platform_repository=True) == "member"
    assert scheduletypes.creator_role(sweep, "repos", platform_repository=True, merge_auto=True) == "owner"
    assert scheduletypes.creator_role(scheduletypes.get("cost-report"), "platform") == "admin"


def test_a_cron_shorter_than_the_types_minimum_is_refused_whatever_its_spelling(root):
    for cron in ("*/5 * * * *", "0,5,10,15,20,25,30,35,40,45,50,55 * * * *", "0,10 9 * * *"):
        with pytest.raises(ValidationFailed) as caught:
            _build(root, cron=cron)
        assert _code(caught) == "interval_too_short", cron
    assert _build(root, cron="*/15 * * * *")["cron"] == "*/15 * * * *"
    with pytest.raises(ValidationFailed) as caught:
        _build(root, type="observer", cron="*/30 * * * *")
    assert caught.value.detail["min_interval_minutes"] == 60


def test_bad_cron_and_timezone_are_422s_with_the_parsers_reason(root):
    with pytest.raises(ValidationFailed) as caught:
        _build(root, cron="0 0 L * *")
    assert _code(caught) == "invalid_cron" and caught.value.detail["reason"] == "quartz_syntax"
    with pytest.raises(ValidationFailed) as caught:
        _build(root, timezone="Europe/Atlantis")
    assert _code(caught) == "invalid_timezone"


def test_the_tenant_limit_and_unique_names(root):
    with pytest.raises(ValidationFailed) as caught:
        _build(root, existing_count=25)
    assert _code(caught) == "schedule_limit"
    assert _build(root, existing_count=24)
    with pytest.raises(Conflict) as caught:
        _build(root, existing_names=["Nightly Sweep "])
    assert _code(caught) == "name_taken"


def test_preview_carries_the_types_refusal_beside_the_words():
    answer = schedules.preview("*/5 * * * *", "UTC", NOW, type_name="issue-sweep")
    assert answer["words"] == "every 5 minutes" and len(answer["next"]) == 5
    assert answer["refusal"]["code"] == "interval_too_short"
    assert schedules.preview("*/15 * * * *", "UTC", NOW, type_name="issue-sweep")["refusal"] is None
    assert schedules.preview("*/5 * * * *", "UTC", NOW)["refusal"] is None


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------


def _patch(**raw):
    return schedules.parse_patch(raw)


def _edit(root, stored, patch, **kw):
    return schedules.apply_edit(stored, patch, actor="bob@example.com", now=NOW, registered_repo_ids=REPOS, root=root, **kw)


def test_a_stale_revision_is_409_schedule_changed(root):
    stored = _build(root)
    with pytest.raises(Conflict) as caught:
        _edit(root, stored, _patch(revision=2, name="x"))
    assert _code(caught) == "schedule_changed"


def test_an_edit_lays_over_what_is_stored_and_bumps_the_revision(root):
    stored = _build(root, budget={"per_run_usd": 5.0})
    edited = _edit(root, stored, _patch(revision=1, cron="0 10 * * 1-5", params={"max_new_per_firing": 5}))
    assert edited["revision"] == 2 and edited["updated_by"] == "bob@example.com" and edited["owner"] == ALICE
    assert edited["budget"]["per_run_usd"] == 5.0
    assert edited["params"]["max_new_per_firing"] == 5 and edited["params"]["cooldown_hours"] == 72
    assert edited["next_slot"] == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def test_patch_refuses_merge_auto_and_a_type_change(root):
    stored = _build(root)
    with pytest.raises(ValidationFailed) as caught:
        _edit(root, stored, _patch(revision=1, gate={"merge": "auto"}))
    assert _code(caught) == "use_merge_switch"
    with pytest.raises(ValidationFailed):
        _patch(revision=1, type="observer")


def test_a_scope_edit_meets_the_same_creator_rule_as_a_create(root, monkeypatch):
    observer = _build(root, type="observer", cron="0 6 * * *")
    to_platform = _patch(revision=1, scope={"mode": "platform"})
    for kw in ({}, {"is_admin": True}):
        with pytest.raises(Forbidden) as caught:
            _edit(root, observer, to_platform, **kw)
        assert _code(caught) == "owner_required"
    assert _edit(root, observer, to_platform, is_owner=True)["scope"] == {"mode": "platform", "repo_ids": []}
    # A member's edit that leaves the scope as it is, or keeps it member-level, is not refused.
    assert _edit(root, observer, _patch(revision=1, scope={"mode": "repos", "repo_ids": ["repo_a"]}))["revision"] == 2
    assert _edit(root, observer, _patch(revision=1, scope={"mode": "all"}))["scope"]["mode"] == "all"
    (root / "cost_report.py").write_text("")
    module = types.ModuleType(scheduletypes.get("cost-report").module)
    module.BUDGET_CAPS = BudgetCaps(1, 2, 1)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    report = _build(root, type="cost-report", cron="0 6 * * *")
    with pytest.raises(Forbidden) as caught:
        _edit(root, report, to_platform)
    assert _code(caught) == "admin_required"
    assert _edit(root, report, to_platform, is_admin=True)["scope"]["mode"] == "platform"


def test_a_member_edit_keeps_an_admins_territory_guard_choice(root):
    stored = _build(root, params={"territory_guard": False}, is_admin=True)
    edited = _edit(root, stored, _patch(revision=1, params={"cooldown_hours": 24}))
    assert edited["params"]["territory_guard"] is False
    fresh = _build(root, name="other")
    with pytest.raises(Forbidden):
        _edit(root, fresh, _patch(revision=1, params={"territory_guard": False}))
