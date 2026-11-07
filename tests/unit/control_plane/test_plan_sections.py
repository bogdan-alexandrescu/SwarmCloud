"""A plan's approval sections: purpose, why, changes, risk, validation and the
change graph (BGD, 6 November 2026: "super quickly what the plan wants to do,
why, whats going to change, where, and risk profiles plus test and qa plans
for the validation part and diagrams or code graphs").

Every section is optional, so an older plan reads and digests as it did; a
malformed one is dropped with a note in `section_notes`, never failing the
plan; an oversized graph is cut; the run read serves them with the plan; and
the plan comment on the issue leads with the purpose, the why, the size and
the risk.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from swarm_api import issuecomments, issueruns, plansections
from swarm_api.issueruns import IssueRef, IssueRun, RunState, parse_plan, plan_digest

from .test_issue_run_tick import (  # noqa: F401 -- fixtures are used by name
    _stored,
    api_context,
    client,
    github,
)
from .test_issue_runs import PLAN, _create, _finish_planner, _run

SECTIONS = {
    "purpose": "Sort the widget list by name.",
    "why": "Issue #42 reports the list is unordered. Users cannot find a widget.",
    "changes": [
        {"file": "apps/web/WidgetList.tsx", "area": "web", "kind": "modify",
         "what": "add a name sort key"},
        {"file": "apps/web/__tests__/WidgetList.test.tsx", "area": "web", "kind": "test",
         "what": "the header toggles the order"},
    ],
    "risk": {"level": "low", "factors": ["one component"], "blast_radius": "the widget page",
             "invariants": [], "mitigations": ["unit test first"], "rollback": "revert the PR"},
    "validation": {
        "tests": [{"file": "apps/web/__tests__/WidgetList.test.tsx",
                   "proves": "the header sorts by name", "requirements": [1, 2]}],
        "qa": [{"check": "click Name twice", "where": "/widgets", "when": "after deploy"}],
    },
    "graph": {
        "nodes": [
            {"id": "WidgetList.sortKey", "label": "sortKey", "kind": "changed",
             "file": "apps/web/WidgetList.tsx"},
            {"id": "WidgetPage", "label": "WidgetPage", "kind": "caller"},
            {"id": "WidgetList.test", "label": "WidgetList.test", "kind": "test"},
        ],
        "edges": [
            {"from": "WidgetPage", "to": "WidgetList.sortKey", "kind": "calls"},
            {"from": "WidgetList.test", "to": "WidgetList.sortKey", "kind": "tests"},
        ],
    },
}

FULL = {
    **copy.deepcopy(PLAN),
    "requirements": ["The list sorts by name.", "The header toggles the order."],
    "overlaps": [{"ref": "example-org/widgets#7", "kind": "pull_request", "action": "none",
                  "note": "touches the same header",
                  "theirs": "renames the header's CSS class"}],
    **copy.deepcopy(SECTIONS),
}


def _comment_run(plan: dict) -> IssueRun:
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    return IssueRun(
        id="run_abc123", tenant_id="eng", created_by="alice@example.com", created_at=now,
        updated_at=now, state=RunState.PLANNED,
        issue=IssueRef(owner="example-org", repo="widgets", number=42),
        plan_approval="required", auto_merge=False, fix_rounds=3, planner_task_id="tsk_1",
        plan=plan, plan_digest=plan_digest(plan), plan_revision=1,
    )


def test_a_plan_with_every_section_is_stored_and_served_on_the_run_read(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, FULL)

    served = _run(client, run["id"]).json()["run"]

    assert served["state"] == "PLANNED"
    plan = served["plan"]
    for key in plansections.SECTION_KEYS:
        assert plan[key] == SECTIONS[key], key
    assert plan["overlaps"][0]["theirs"] == "renames the header's CSS class"
    assert plan["validation"]["tests"][0]["requirements"] == [1, 2]
    assert "section_notes" not in plan
    assert db.docs[f"issue_runs/{run['id']}"]["plan"] == plan


def test_a_plan_without_the_sections_still_parses_and_digests_as_before():
    plan = parse_plan(copy.deepcopy(PLAN))

    assert plan == PLAN
    assert plan_digest(plan) == plan_digest(PLAN)


def test_a_stored_plan_with_sections_reads_back_to_the_same_digest():
    once = parse_plan(copy.deepcopy(FULL))
    again = parse_plan(copy.deepcopy(once), stored=True)

    assert again == once
    assert plan_digest(again) == plan_digest(once)


def test_an_oversized_graph_is_cut_to_its_caps_changed_nodes_first_with_a_note():
    callers = [{"id": f"caller{i}", "label": f"caller {i}", "kind": "caller"}
               for i in range(plansections.MAX_GRAPH_NODES)]
    changed = [{"id": f"changed{i}", "label": f"changed {i}", "kind": "changed"}
               for i in range(5)]
    edges = [{"from": f"caller{i}", "to": f"changed{i % 5}", "kind": "calls"}
             for i in range(plansections.MAX_GRAPH_NODES)]
    edges += [{"from": f"changed{i % 5}", "to": f"changed{(i + 1) % 5}", "kind": "imports"}
              for i in range(plansections.MAX_GRAPH_EDGES)]
    plan = parse_plan({**PLAN, "graph": {"nodes": callers + changed, "edges": edges}})

    graph = plan["graph"]
    ids = {node["id"] for node in graph["nodes"]}
    assert len(graph["nodes"]) == plansections.MAX_GRAPH_NODES
    assert {f"changed{i}" for i in range(5)} <= ids
    assert len(graph["edges"]) <= plansections.MAX_GRAPH_EDGES
    assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
    assert any(note.startswith("graph: cut to 40 of 45 nodes") for note in plan["section_notes"])


def test_a_risk_level_outside_the_set_is_dropped_with_a_note_and_the_plan_stays_valid():
    plan = parse_plan({**PLAN, "purpose": "Sort it.",
                       "risk": {"level": "catastrophic", "factors": ["x"]}})

    assert "risk" not in plan
    assert plan["purpose"] == "Sort it."
    assert plan["steps"] == PLAN["steps"]
    assert len(plan["section_notes"]) == 1
    assert plan["section_notes"][0].startswith("risk: dropped: level:")


def test_malformed_sections_are_each_dropped_with_a_note_never_failing_the_plan():
    plan = parse_plan({
        **PLAN,
        "requirements": ["one"],
        "purpose": "line one\nline two",
        "why": "One. Two. Three.",
        "changes": [{"file": "a.py", "area": "api", "kind": "rename", "what": "x"}],
        "validation": {"tests": [{"file": "t.py", "proves": "it", "requirements": [1, 3]}]},
        "graph": {"nodes": [{"id": "a", "label": "a", "kind": "changed"}],
                  "edges": [{"from": "a", "to": "ghost", "kind": "calls"}]},
        "overlaps": [{"ref": "example-org/widgets#7", "kind": "issue", "action": "none",
                      "note": "n", "theirs": "x" * 400}],
    })

    notes = plan["section_notes"]
    assert "purpose" not in plan and "why" not in plan and "changes" not in plan
    assert plan["validation"]["tests"][0]["requirements"] == [1]
    assert plan["graph"] == {"nodes": [{"id": "a", "label": "a", "kind": "changed"}], "edges": []}
    assert "theirs" not in plan["overlaps"][0]
    for prefix in ("purpose: dropped", "why: dropped", "changes: dropped",
                   "validation: requirement ids 3 dropped", "graph: 1 edge naming no node",
                   "overlaps.0.theirs: dropped"):
        assert any(note.startswith(prefix) for note in notes), prefix


def test_the_issue_comment_leads_with_the_purpose_why_size_and_risk():
    body = issuecomments.render_plan_comment(_comment_run(parse_plan(copy.deepcopy(FULL))))

    head = body.split("#### Requirements", 1)[0]
    purpose = head.index("**Sort the widget list by name.**")
    assert purpose < head.index("**Approval: pending.**")
    assert "**Why:** Issue #42 reports the list is unordered." in head
    assert "**Size:** 2 steps · 2 files (1 modified, 1 test)" in head
    assert "**Risk:** low — the widget page" in head
    assert "**Validation:** 1 test, 1 QA check" in body
    assert "renames the header's CSS class" in body
    assert "```mermaid" in body


def test_the_issue_comment_of_a_plan_without_sections_is_unchanged_in_its_lead():
    body = issuecomments.render_plan_comment(_comment_run(parse_plan(copy.deepcopy(PLAN))))

    assert "**Why:**" not in body and "**Risk:**" not in body
    assert "```mermaid" not in body


def test_the_planner_is_asked_for_every_section_with_its_caps():
    prompt = issueruns.planner_prompt(IssueRef(owner="example-org", repo="widgets", number=42))

    for key in (*plansections.SECTION_KEYS, "theirs", "blast_radius", "rollback"):
        assert f'"{key}"' in prompt, key
    assert f"at most {plansections.MAX_GRAPH_NODES} nodes" in prompt
    assert f"{plansections.MAX_GRAPH_EDGES} edges" in prompt
