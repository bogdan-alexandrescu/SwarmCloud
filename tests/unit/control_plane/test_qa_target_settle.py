"""A leg's run settles what its crawling capture steps found
(docs/design/qa-environments.md §3.5, §5, §7; lane QAENV-SETTLE of §8; BGD's
Q5 (c) and Q8 (a), 2026-10-23; part of #2136).

What these tests hold:

  * `qaspec.found_routes` holds a capture step's `found.json` as
    `discovered_screens` holds a discovery's `screens.json`: a path off the
    leg's origin or on an excluded path is dropped, a malformed id or path
    refuses the file, and a route state's `reveal` is held to the start-click
    rules (an exclusion word or Playwright syntax refuses the file); a crawl
    is complete only when it stopped by `done`;
  * when every capture step of a leg's run has ended, the `task_finished` push
    reads each step's `capture-<g>/found.json` through the tenant-checked
    artifact read and merges the union into THAT leg's `discovered`, each
    route carrying `first_seen`, `last_seen` and `workflow_id`; the other
    leg, `screens` and `revision` are never written;
  * a route unseen by a complete crawl is marked `missing_since`, and after
    three consecutive complete crawls it is dropped; a crawl stopped by
    `time` changes nothing;
  * a run settles once, and not before its last capture step has ended;
  * the target's answer lists each leg's `new_since_last_run`, for Promote;
  * the file step's dedupe key names the leg, its titles start `[<leg>]`,
    and a grouped run's body links the run group's page.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import qaspec

from .conftest import auth_header
from .test_qa_target_discovery import (  # noqa: F401 -- pytest fixtures, used by name
    BUCKET,
    api_context,
    group_map,
    resolver,
    seeded,
)
from .test_qa_target_environments import entry_of
from .test_qa_run_groups import approved_two, run, tasks_of
from .test_qa_targets import TARGETS

# --------------------------------------------------------------------------
# found.json, held (qaspec.found_routes)
# --------------------------------------------------------------------------


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a", "revision": 1, "name": "Shop", "base_url": "https://shop.example.com",
        "repository": "https://github.com/example-org/shop", "screens": [{"id": "home", "path": "/"}],
        "exclusions": {"paths": ["/admin/*"], "controls": ["archive"]},
    }
    target.update(over)
    return target


def found(*, new: list[dict[str, Any]] | None = None, not_owned: list[dict[str, Any]] | None = None,
          over_cap: list[dict[str, Any]] | None = None, stored: list[str] | None = None,
          stopped_by: str = "done") -> dict[str, Any]:
    """`swarm_qa capture --crawl`'s found.json (§3.5)."""
    return {
        "format": 1, "stored": list(stored if stored is not None else ["/"]),
        "captured_new": list(new or []), "not_owned": list(not_owned or []), "over_cap": list(over_cap or []),
        "skipped": {"off_origin": 0, "outside_base": 0, "excluded": 0, "robots": 0, "http_error": 0,
                    "failed": 0, "write_attempting": []},
        "crawl": {"pages": 4, "depth": 2, "seconds": 12.5, "stopped_by": stopped_by},
    }


def row(rid: str, path: str, **over: Any) -> dict[str, Any]:
    return {"id": rid, "path": path, "depth": 1, **over}


def test_found_routes_drops_off_origin_and_excluded_paths_and_keeps_the_rest() -> None:
    document = found(new=[row("billing", "/billing"), row("away", "https://elsewhere.example.com/x"),
                          row("abs", "https://shop.example.com/abs")],
                     not_owned=[{**row("admin-users", "/admin/users"), "owner": 2},
                                {**row("team", "/team"), "owner": 2}],
                     over_cap=[row("app-settings", "/app#/settings")])
    held = qaspec.found_routes(_target(), document)
    # MUTATION: keep an off-origin path, or skip the exclusions, and these lists differ.
    assert [r["path"] for r in held.routes] == ["/billing", "/abs", "/team", "/app#/settings"]
    assert [r["id"] for r in held.routes] == ["billing", "abs", "team", "app-settings"]
    # MUTATION: read only `captured_new` and a route another step owns is never stored.
    assert "/team" in held.seen and "/" in held.seen
    assert held.complete is True


@pytest.mark.parametrize("bad", [
    row("Bad Id", "/x"),
    row("x", "no-slash"),
    row("x", "/x#section"),
    row("x", "/x", reveal=[{"text": "Delete everything"}]),
    row("x", "/x", reveal=[{"text": "Archive"}]),
    row("x", "/x", reveal=[{"text": "Billing", "css": "button:has-text('Billing')"}]),
    row("x", "/x", reveal=[]),
])
def test_found_routes_refuses_a_malformed_route_or_reveal(bad: dict[str, Any]) -> None:
    # MUTATION: drop a malformed row instead of refusing, or skip the
    # start-click rules for `reveal`, and this does not raise.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.found_routes(_target(), found(new=[bad]))


def test_found_routes_keeps_a_route_state_and_keys_it_by_its_reveal_texts() -> None:
    held = qaspec.found_routes(_target(), found(new=[row("billing-plans", "/billing",
                                                          reveal=[{"text": "Plans", "css": '[role="tab"]'}])]))
    [state] = held.routes
    # MUTATION: drop `reveal` and the state is stored as its bare path.
    assert state["reveal"] == [{"text": "Plans", "css": '[role="tab"]'}]
    assert qaspec.route_key(state) == "/billing >> Plans"
    assert "/billing >> Plans" in held.seen


@pytest.mark.parametrize("stopped_by,complete", [("done", True), ("time", False), ("pages", False),
                                                 ("depth", False)])
def test_only_a_crawl_stopped_by_done_is_complete(stopped_by: str, complete: bool) -> None:
    # MUTATION: count any stop as complete and a bounded crawl drops routes.
    assert qaspec.found_routes(_target(), found(stopped_by=stopped_by)).complete is complete


@pytest.mark.parametrize("document", [None, [], {"format": 2}, {**found(), "crawl": {"stopped_by": "later"}},
                                      {**found(), "captured_new": "x"}])
def test_found_routes_refuses_a_document_that_is_not_found_json(document: Any) -> None:
    with pytest.raises(qaspec.QaSpecError):
        qaspec.found_routes(_target(), document)


# --------------------------------------------------------------------------
# The file step names its leg (§7, Q8 (a))
# --------------------------------------------------------------------------


def _file_prompt(target: dict[str, Any], **kw: Any) -> str:
    [step] = [s for s in qaspec.render(target, **kw)["steps"] if s["step_id"] == "file"]
    return step["prompt"]


def test_the_file_steps_dedupe_key_and_title_name_the_leg() -> None:
    legs = {"environments": [
        {"name": "staging", "base_url": "https://app.example.com", "environment": "non_production"},
        {"name": "production", "base_url": "https://shop.example.com"}]}
    target = _target(**legs)
    staging = _file_prompt(qaspec.leg(target, "staging"))
    production = _file_prompt(qaspec.leg(target, "production"))
    # MUTATION: leave the leg out of the key and the two legs' issues dedupe into one.
    assert '"visual-qa:qat_3f9a:staging:<screen>:' in staging
    assert '"visual-qa:qat_3f9a:production:<screen>:' in production
    # MUTATION: title the issue without its leg and a reader cannot tell staging's from production's.
    assert "starts `[staging]`" in staging and "starts `[production]`" in production
    # A target of one origin names its legacy leg.
    assert '"visual-qa:qat_3f9a:production:<screen>:' in _file_prompt(_target())


def test_a_grouped_runs_file_body_links_the_run_group() -> None:
    group = {"group_id": "qrg_0123456789abcdef", "legs": ["staging", "production"]}
    prompt = _file_prompt(_target(), group=group)
    # MUTATION: ignore the group and the issue never links the other leg's run.
    assert "/qa-runs/qrg_0123456789abcdef" in prompt and "staging" in prompt
    assert "/qa-runs/" not in _file_prompt(_target())


def test_runnable_legs_render_both_legs_with_one_group_and_open_it_under_that_id(client, db, seeded) -> None:
    target = approved_two(client)
    response = run(client, target["id"], environments=["staging", "production"], file="none")
    assert response.status_code == 201, response.text
    group_id = response.json()["group_id"]
    # The group is the one the legs were rendered with: the id is minted
    # before the render, so the file prompt and the group agree.
    # MUTATION: mint a second id in `open_group` and `runnable_legs`'s is lost.
    from swarm_api.qatargets import QaTargets
    legs = QaTargets(db).runnable_legs(target["tenant_id"], target["id"], file="none", environments=None)
    assert legs[0].group_id == legs[1].group_id and legs[0].group_id.startswith("qrg_")
    assert db.docs[f"qa_run_groups/{group_id}"]["group_id"] == group_id


# --------------------------------------------------------------------------
# The settle (§5)
# --------------------------------------------------------------------------


def finish_capture(client, db, objects, workflow_id: str, group: str, document: Any, *,
                   state: str = "SUCCEEDED", push: bool = True) -> dict[str, Any]:
    """The run's `capture-<group>` step ends `state`, its `found.json`
    uploaded where `capture --out $SWARM_ARTIFACTS_DIR/capture-<g>` writes it,
    and the worker's `task_finished` wake is pushed."""
    [task] = [t for t in tasks_of(db, workflow_id) if t["step_id"] == f"capture-{group}"]
    task["state"] = state
    entries = []
    if document is not None:
        text = json.dumps(document)
        name = f"capture-{group}/found.json"
        key = f"tenants/{task['tenant_id']}/tasks/{task['id']}/attempts/att_1/artifacts/{name}"
        objects.put(key, text.encode("utf-8"))
        entries.append({"name": name, "bytes": len(text.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"})
    task["result_summary"] = {"artifacts": entries, "logs": {}}
    if push:
        ring(client, task)
    return task


def ring(client, task: dict[str, Any]) -> None:
    response = client.post("/v1/admin/tasks/finished", headers=auth_header("root"), json={
        "message": {"attributes": {"reason": "task_finished", "task_id": task["id"],
                                   "tenant_id": task["tenant_id"]}}})
    assert response.status_code == 200, response.text


def run_leg(client, target_id: str, name: str) -> str:
    response = run(client, target_id, environments=[name])
    assert response.status_code == 201, response.text
    [leg_] = response.json()["legs"]
    return leg_["workflow_id"]


def shown(client, target_id: str) -> dict[str, Any]:
    return client.get(f"{TARGETS}/{target_id}", headers=auth_header("carol")).json()["qa_target"]


def by_path(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["path"]: r for r in entry.get("discovered") or []}


def test_a_legs_found_routes_merge_into_that_legs_discovered_only(client, db, objects, seeded) -> None:
    target = approved_two(client)
    before = db.docs[f"qa_targets/{target['id']}"]
    screens, revision = list(before["screens"]), before["revision"]
    workflow_id = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, workflow_id, "g1", found(
        new=[row("billing", "/billing")], not_owned=[{**row("team", "/team"), "owner": 2}],
        over_cap=[row("away", "https://shop.example.com/away")]))
    answer = shown(client, target["id"])
    staging = by_path(entry_of(answer, "staging"))
    # MUTATION: skip the settle in the doorbell and nothing is stored.
    # MUTATION: hold to the other leg's origin and the absolute production URL is kept.
    assert sorted(staging) == ["/billing", "/team"]
    for route in staging.values():
        # MUTATION: leave out a stamp and the console cannot say when a route was seen.
        assert route["first_seen"] and route["last_seen"] and route["workflow_id"] == workflow_id
    # MUTATION: merge into every entry and production's list is set too.
    assert entry_of(answer, "production")["discovered"] is None
    after = db.docs[f"qa_targets/{target['id']}"]
    # MUTATION: write `screens` or bump `revision` and a running leg's mark is refused.
    assert after["screens"] == screens and after["revision"] == revision


def test_a_second_run_keeps_first_seen_and_moves_last_seen(client, db, objects, seeded, api_context) -> None:
    target = approved_two(client)
    first = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, first, "g1", found(new=[row("billing", "/billing")]))
    seen_first = by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"]
    api_context.now = _later(api_context.now, 1)
    second = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, second, "g1", found(new=[row("billing", "/billing")]))
    seen_second = by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"]
    # MUTATION: overwrite `first_seen` on every settle and the route looks new every run.
    assert seen_second["first_seen"] == seen_first["first_seen"]
    assert seen_second["last_seen"] != seen_first["last_seen"] and seen_second["workflow_id"] == second


def test_a_route_unseen_by_three_complete_crawls_is_dropped(client, db, objects, seeded, api_context) -> None:
    target = approved_two(client)
    first = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, first, "g1", found(new=[row("billing", "/billing"), row("team", "/team")]))
    for n in (1, 2):
        api_context.now = _later(api_context.now, n)
        workflow_id = run_leg(client, target["id"], "staging")
        finish_capture(client, db, objects, workflow_id, "g1", found(new=[row("team", "/team")]))
        billing = by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"]
        # MUTATION: drop on the first miss and this is a KeyError.
        assert billing["missing_since"] is not None and billing["missed_crawls"] == n
    team = by_path(entry_of(shown(client, target["id"]), "staging"))["/team"]
    assert team.get("missing_since") is None
    api_context.now = _later(api_context.now, 3)
    third = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, third, "g1", found(new=[row("team", "/team")]))
    # MUTATION: never drop a missing route and /billing is still listed.
    assert sorted(by_path(entry_of(shown(client, target["id"]), "staging"))) == ["/team"]


def test_a_crawl_stopped_by_time_changes_nothing_about_missing(client, db, objects, seeded) -> None:
    target = approved_two(client)
    first = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, first, "g1", found(new=[row("billing", "/billing")]))
    for _ in range(3):
        workflow_id = run_leg(client, target["id"], "staging")
        finish_capture(client, db, objects, workflow_id, "g1", found(stopped_by="time"))
    billing = by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"]
    # MUTATION: count a bounded crawl as complete and /billing is marked, then dropped.
    assert billing.get("missing_since") is None and not billing.get("missed_crawls")


def test_a_refused_found_json_stores_nothing_and_says_why(client, db, objects, seeded) -> None:
    target = approved_two(client)
    workflow_id = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, workflow_id, "g1", found(new=[row("Bad Id", "/x")]))
    staging = entry_of(shown(client, target["id"]), "staging")
    # MUTATION: store a malformed file's routes and this lists them.
    assert staging["discovered"] is None
    assert "found.json" in (staging["discovery_error"] or "")


def test_a_run_settles_once_and_only_after_its_captures_ended(client, db, objects, seeded) -> None:
    target = approved_two(client)
    first = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, first, "g1", found(new=[row("billing", "/billing")]))
    workflow_id = run_leg(client, target["id"], "staging")
    task = finish_capture(client, db, objects, workflow_id, "g1", found(new=[]), state="RUNNING")
    # MUTATION: settle a run whose capture has not ended and /billing is marked missing.
    assert by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"].get("missing_since") is None
    task["state"] = "SUCCEEDED"
    ring(client, task)
    ring(client, task)
    billing = by_path(entry_of(shown(client, target["id"]), "staging"))["/billing"]
    # MUTATION: settle on every ring and the one complete crawl counts twice.
    assert billing["missed_crawls"] == 1
    assert db.docs[f"qa_target_runs/{workflow_id}"]["settled_at"] is not None


def test_the_answer_lists_each_legs_new_since_last_run(client, db, objects, seeded, api_context) -> None:
    target = approved_two(client)
    first = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, first, "g1", found(new=[row("billing", "/billing")]))
    staging = entry_of(shown(client, target["id"]), "staging")
    # MUTATION: leave `new_since_last_run` out of the answer and Promote has nothing to offer.
    assert [r["path"] for r in staging["new_since_last_run"]] == ["/billing"]
    assert entry_of(shown(client, target["id"]), "production")["new_since_last_run"] == []
    api_context.now = _later(api_context.now, 1)
    second = run_leg(client, target["id"], "staging")
    finish_capture(client, db, objects, second, "g1", found(new=[row("billing", "/billing"), row("team", "/team")]))
    staging = entry_of(shown(client, target["id"]), "staging")
    # MUTATION: list every discovered route and /billing is offered again.
    assert [r["path"] for r in staging["new_since_last_run"]] == ["/team"]


def _later(now: Any, minutes: int) -> Any:
    from datetime import timedelta

    moment = now() + timedelta(minutes=minutes)
    return lambda: moment
