"""Parallel plan steps never share a file (docs/design/knowledge-graph.md §4.9, lane KG1).

Two steps of a plan that are NOT on one dependency line -- neither depends on
the other, directly or through others -- run side by side on their own
branches, and the integrator's 3-way merge is where two edits of one file
meet. Until this lane the rule was a sentence in the planner's prompt, and a
plan that broke it was approved and failed at the join
(`issueruns._compile_staged`). Owner decision 2026-10-08 (§9 Q4): REFUSE such
a plan with the reason, so the planner re-plans; never auto-chain the steps.

What these tests hold:

  1. Two parallel steps naming the same file, or one naming a directory
     prefix of the other's file, are refused, and the refusal names BOTH
     steps and the shared path.
  2. A shared file on one dependency line -- direct or transitive -- and any
     plan that states no `depends_on` (today's chain) are accepted.
  3. A plan STORED before the rule still reads, compiles and digests; an
     EDIT, like a planner's plan, is held to it.
  4. The success measure "join conflicts per staged run -> 0 for declared
     files": over many generated plans, every accepted plan's parallel pairs
     have disjoint declared files, and every refused one really had a pair.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import random

import pytest

from swarm_api import issueruns
from swarm_api.issueruns import InvalidPlan, parse_edited_plan, parse_plan, parse_planner_output
from swarm_api.validation import IssueRef

from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    _create,
    _finish_planner,
    _run,
    api_context,
    forge_tokens,
    github,
)


def step(step_id: str, *files: str, depends_on: list[str] | None = None) -> dict:
    out: dict = {"step_id": step_id, "title": f"Step {step_id}", "prompt": f"Do {step_id}."}
    if files:
        out["files"] = list(files)
    if depends_on is not None:
        out["depends_on"] = depends_on
    return out


def plan(*steps: dict) -> dict:
    return {"summary": "A plan.", "steps": list(steps)}


def refusal(value: dict) -> str:
    with pytest.raises(InvalidPlan) as caught:
        parse_planner_output(value)
    return caught.value.message


# --------------------------------------------------------------------------
# 1. refused, naming both steps and the file
# --------------------------------------------------------------------------

def test_two_parallel_steps_on_one_file_are_refused_naming_both_and_the_file():
    why = refusal(plan(
        step("api", "apps/swarm-api/swarm_api/main.py", depends_on=[]),
        step("ui", "apps/swarm-ui/src/App.tsx", "apps/swarm-api/swarm_api/main.py",
             depends_on=[]),
    ))
    assert "'api'" in why and "'ui'" in why
    assert "apps/swarm-api/swarm_api/main.py" in why
    assert "parallel" in why
    # What the planner does about it: one dependency line, or one owner.
    assert "depends_on" in why


def test_a_directory_prefix_of_the_other_steps_file_is_refused():
    why = refusal(plan(
        step("screen", "apps/swarm-ui/src/", depends_on=[]),
        step("nav", "apps/swarm-ui/src/App.tsx", depends_on=[]),
    ))
    assert "'screen'" in why and "'nav'" in why
    assert "apps/swarm-ui/src" in why and "apps/swarm-ui/src/App.tsx" in why


def test_paths_are_compared_normalised():
    why = refusal(plan(
        step("a", "./src/widgets/sort.py", depends_on=[]),
        step("b", "src/widgets/sort.py/", depends_on=[]),
    ))
    assert "src/widgets/sort.py" in why


def test_a_name_that_only_starts_like_the_other_is_not_a_prefix():
    parsed = parse_planner_output(plan(
        step("a", "apps/swarm-api", depends_on=[]),
        step("b", "apps/swarm-api-extra/x.py", depends_on=[]),
    ))
    assert parsed[0] == "plan"


def test_two_branches_of_a_diamond_sharing_a_file_are_refused():
    why = refusal(plan(
        step("base", "src/a.py", depends_on=[]),
        step("left", "src/shared.py", depends_on=["base"]),
        step("right", "src/shared.py", depends_on=["base"]),
    ))
    assert "'left'" in why and "'right'" in why and "src/shared.py" in why


def test_every_conflict_is_listed_and_the_list_is_bounded():
    steps = [step(f"s{i}", "src/hot.py", depends_on=[]) for i in range(8)]
    why = refusal(plan(*steps))
    # 8 parallel steps on one file: 28 pairs. Each named up to the bound, then a count.
    assert why.count("src/hot.py") >= issueruns.MAX_TERRITORY_CONFLICTS_SHOWN
    assert "28 pairs" in why


def test_the_conflicts_function_names_each_pair_once_in_plan_order():
    conflicts = issueruns.parallel_file_conflicts(plan(
        step("a", "x/", "y.py", depends_on=[]),
        step("b", "x/one.py", depends_on=[]),
        step("c", "y.py", depends_on=["b"]),
    )["steps"])
    assert conflicts == [("a", "b", "x/one.py"), ("a", "c", "y.py")]


# --------------------------------------------------------------------------
# 2. accepted on one dependency line, and in a chain
# --------------------------------------------------------------------------

def test_one_file_on_one_dependency_line_is_accepted():
    kind, parsed = parse_planner_output(plan(
        step("model", "src/widgets/sort.py", depends_on=[]),
        step("ui", "src/widgets/sort.py", depends_on=["model"]),
    ))
    assert kind == "plan" and len(parsed["steps"]) == 2


def test_one_file_on_a_transitive_line_is_accepted():
    kind, _ = parse_planner_output(plan(
        step("a", "src/core.py", depends_on=[]),
        step("b", "src/other.py", depends_on=["a"]),
        step("c", "src/core.py", depends_on=["b"]),
    ))
    assert kind == "plan"


def test_a_join_may_share_a_file_with_each_of_its_ancestors():
    kind, _ = parse_planner_output(plan(
        step("left", "src/left.py", depends_on=[]),
        step("right", "src/right.py", depends_on=[]),
        step("join", "src/left.py", "src/right.py", depends_on=["left", "right"]),
    ))
    assert kind == "plan"


def test_a_plan_with_no_depends_on_is_a_chain_and_may_share_files():
    kind, _ = parse_planner_output(plan(
        step("one", "src/widgets/sort.py"),
        step("two", "src/widgets/sort.py"),
    ))
    assert kind == "plan"


def test_steps_without_files_never_conflict():
    kind, _ = parse_planner_output(plan(step("a", depends_on=[]), step("b", depends_on=[])))
    assert kind == "plan"


# --------------------------------------------------------------------------
# 3. stored plans still read; edits are held to the rule
# --------------------------------------------------------------------------

CONFLICTED = plan(
    step("a", "src/shared.py", depends_on=[]),
    step("b", "src/shared.py", depends_on=[]),
)


def test_a_plan_stored_before_the_rule_still_reads_compiles_and_keeps_its_digest():
    stored = parse_plan(CONFLICTED, stored=True)
    assert issueruns.plan_digest(stored) == issueruns.plan_digest(CONFLICTED)
    assert issueruns.plan_shape(stored) is not None


def test_an_edit_that_puts_two_parallel_steps_on_one_file_is_refused():
    current = plan(step("a", "src/a.py", depends_on=[]), step("b", "src/b.py", depends_on=[]))
    with pytest.raises(InvalidPlan) as caught:
        parse_edited_plan(CONFLICTED, current)
    assert "'a'" in caught.value.message and "src/shared.py" in caught.value.message


def test_an_edit_that_resolves_the_conflict_is_accepted():
    fixed = plan(
        step("a", "src/shared.py", depends_on=[]),
        step("b", "src/shared.py", depends_on=["a"]),
    )
    assert parse_edited_plan(fixed, CONFLICTED)["steps"][1]["depends_on"] == ["a"]


# --------------------------------------------------------------------------
# the planner is told, and a refused plan says why on the run
# --------------------------------------------------------------------------

def test_the_planner_is_told_the_rule_is_enforced_and_how_it_is_compared():
    text = issueruns.planner_prompt(IssueRef(owner="saga-xyz", repo="widgets", number=42))
    assert "refused" in text
    assert "directory" in text and "prefix" in text


def test_a_planner_plan_with_parallel_steps_on_one_file_fails_the_run_naming_them(
    client, db, objects
):
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, CONFLICTED)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "FAILED"
    assert "'a'" in read["error"] and "'b'" in read["error"]
    assert "src/shared.py" in read["error"]
    # Nothing was compiled or submitted for a refused plan.
    assert read["workflow_id"] is None


# --------------------------------------------------------------------------
# 4. the measure: zero join conflicts on declared files
# --------------------------------------------------------------------------

def _ancestors(steps: list[dict]) -> dict[str, set[str]]:
    """An independent oracle: each step's transitive dependencies."""
    found: dict[str, set[str]] = {}
    chain = all("depends_on" not in s for s in steps)
    previous = None
    for s in steps:
        deps = ([previous] if previous else []) if chain else s.get("depends_on", [])
        found[s["step_id"]] = set(deps).union(*(found[d] for d in deps))
        previous = s["step_id"]
    return found


def _norm(path: str) -> str:
    return path.strip().removeprefix("./").strip("/")


def _clash(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _parallel_clashes(steps: list[dict]) -> int:
    up = _ancestors(steps)
    count = 0
    for i, one in enumerate(steps):
        for other in steps[i + 1:]:
            if one["step_id"] in up[other["step_id"]] or other["step_id"] in up[one["step_id"]]:
                continue
            if any(_clash(x, y) for x in one.get("files", []) for y in other.get("files", [])):
                count += 1
    return count


def _random_plan(rng: random.Random) -> dict:
    pool = ["src/a.py", "src/b.py", "src/c/", "src/c/d.py", "docs/x.md", "./src/a.py",
            "tests/t.py", "src/c/e.py/"]
    chain = rng.random() < 0.15
    steps = []
    for i in range(rng.randint(1, issueruns.MAX_PLAN_STEPS)):
        earlier = [s["step_id"] for s in steps]
        files = rng.sample(pool, rng.randint(0, 3))
        deps = None if chain else rng.sample(earlier, rng.randint(0, min(2, len(earlier))))
        steps.append(step(f"s{i}", *files, depends_on=deps))
    return plan(*steps)


def test_no_accepted_plan_runs_two_steps_on_one_declared_file_side_by_side():
    rng = random.Random(4242)
    accepted = refused = 0
    for _ in range(3000):
        candidate = _random_plan(rng)
        clashes = _parallel_clashes(candidate["steps"])
        try:
            parse_planner_output(candidate)
        except InvalidPlan as exc:
            if "parallel" not in exc.message:
                continue  # refused for another reason (e.g. stage width); not this rule
            refused += 1
            assert clashes > 0, candidate
            continue
        accepted += 1
        assert clashes == 0, candidate
    # Both sides were exercised, so the assertions above could have failed.
    assert accepted > 300 and refused > 300, (accepted, refused)
