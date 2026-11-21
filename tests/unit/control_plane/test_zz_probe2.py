from __future__ import annotations
from datetime import timedelta
from swarm_common.states import ParkReason
from .conftest import seed_pool
from .test_fix_loop_drive import (  # noqa: F401 -- fixtures
    MERGE, NOT_YET, NOW, Loop, _members_hold_grants, clock, finish, github, loop_client,
    loop_enabled, push, pushed, round_task, FIX_HEAD, ROUND_HEAD, REPO)

def spec():
    return {"strategy": "integrate", "repository_url": REPO, "on_step_failure": "fail_workflow",
            "steps": [
              {"step_id": "review", "runner_profile": "mock", "input": {"prompt": "review", "artifact_name": "verdict.json", "artifact_text": "{}", "steps": 1, "sleep_seconds": 0}},
              {"step_id": "fix", "runner_profile": "claude-code", "depends_on": ["review"], "input_from": {"review": "verdict.json"},
               "when": {"step": "review", "verdict_in": ["NOT_YET"]}, "input": {"prompt": "fix"}},
              {"step_id": "loop", "runner_profile": "fix-loop", "depends_on": ["fix"],
               "fix_loop": {"review": "review", "fix": "fix", "max_rounds": 3,
                            "fix_round": {"runner_profile": "claude-code", "prompt": "append"},
                            "re_review": {"runner_profile": "claude-code", "prompt": "judge"}}}]}

def test_probe(loop_client, db, objects, make_scheduler, clock):
    seed_pool(db, "global", hard_limit=50)
    loop = Loop(loop_client, db, spec())
    sch = make_scheduler(now=clock)
    def show(tag):
        print(tag, {k: (db.docs[f"tasks/{v}"]["state"], db.docs[f"tasks/{v}"].get("park_reason")) for k, v in loop.tasks.items()})
    show("submitted")
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET, completed_at=NOW)
    print(push(loop_client, loop.tasks["review"]))
    sch.release_dependants(loop.tasks["review"]); show("review done")
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD), completed_at=NOW)
    print(push(loop_client, loop.tasks["fix"]))
    sch.release_dependants(loop.tasks["fix"]); show("fix done")
    clock.now = NOW + timedelta(minutes=10)
    print(push(loop_client, loop.tasks["fix"]))
    show("after push"); print(loop.round_steps().keys(), loop.state())
    r1 = round_task(loop, "re-review:1")
    finish(db, objects, r1, verdict={"verdict": "NOT_YET", "commit": FIX_HEAD, "findings": [{"severity": "major", "summary": "x"}]}, completed_at=clock.now)
    print(push(loop_client, r1)); show("r1"); print(list(loop.round_steps()))
    f2 = round_task(loop, "review-fix:2")
    finish(db, objects, f2, git=pushed(f2, ROUND_HEAD), completed_at=clock.now)
    print(push(loop_client, f2)); show("f2"); print(list(loop.round_steps()))
    r2 = round_task(loop, "re-review:2")
    finish(db, objects, r2, verdict=MERGE, completed_at=clock.now)
    print(push(loop_client, r2)); show("r2"); print(list(loop.round_steps()))
    print(loop.doc().get("result_summary"))
    assert False
