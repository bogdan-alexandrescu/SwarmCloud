"""The merge step's `-git` token reaches GitHub's Authorization header and nothing else.

Contract request 47 moved the merge onto the tenant's existing forge token.
The owner's rule for it is #219's (docs/merge-step.md §2.2): read at merge
time only, and never put in the workspace, a file, an agent's environment,
an event or a log. This runs the PRODUCTION worker -- the real lifecycle, the
real `merge.run_merge`, over in-memory Firestore -- against a fake GitHub
for a repository that is not SwarmCloud's, and then looks for the token
everywhere the worker could have left it: the captured log stream, every
Firestore document and event, every file under the worker's directories,
and the process environment.

The control is the merge itself: it SUCCEEDS, so the token was read and
used. A worker that never read it would pass the "not found" assertions
vacuously; this one cannot.
"""

from __future__ import annotations

import json
import os

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt
from fake_github import fresh_token
from fakes import ExplodingChildProcess, FakeSecretClient
from merge_world import NUMBER, OPENER, PINNED, PR, REPO_URL, MergeWorld
import spec_keys


def _seed(db) -> None:
    seed_attempt(db, runner_profile="merge", task_input={})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    doc["repository_url"] = REPO_URL
    doc["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                    "merge_target": {"pull_request": OPENER}}}
    opener = {
        "id": OPENER, "tenant_id": "eng", "workflow_id": "wf_1", "step_id": "fix",
        "runner_profile": "claude-code", "state": TaskState.SUCCEEDED.value,
        "repository_url": REPO_URL, "input": {"prompt": "fix it"},
        "metadata": {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                  "role": "integrator"}},
        "result_summary": {"git": {"pushed_head": PINNED, "pull_request": {"number": NUMBER}}},
    }
    spec_keys.sign_document(opener, OPENER)
    db.seed(f"tasks/{OPENER}", opener)
    db.doc("tenants/eng")["credentials"] = ["git"]


def test_the_token_is_only_ever_in_the_authorization_header(
    db, worker_factory, log_stream, tmp_path, monkeypatch
):
    from agent_worker import lifecycle

    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    _seed(db)
    world = MergeWorld(tmp_path / "github")
    token = fresh_token()
    secrets = FakeSecretClient({"swarm-tenant-eng-git": token})
    worker, _, _ = worker_factory(runner_profile="merge", secret_client=secrets)
    db.doc("tenants/eng")["credentials"] = ["git"]
    worker.forge_transport = world.github
    worker.action_sleep = lambda _seconds: None

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["merge"]["merged_by_this_task"] is True
    assert secrets.accessed == ["swarm-tenant-eng-git"]
    assert world.merge_calls(), "the merge was not made, so the token was never used"

    # GitHub saw it in the Authorization header, and only there.
    for seen in world.github.seen:
        carriers = [k for k, v in seen.headers.items() if token in str(v)]
        assert carriers == ["Authorization"], (seen.method, seen.path, carriers)
        assert token not in seen.url
        assert token not in json.dumps(seen.body, default=str)
    assert any(s.path == f"{PR}/merge" for s in world.github.seen)

    # Not in a log line.
    assert token not in log_stream.getvalue()
    # Not in any document or event the worker wrote.
    assert token not in json.dumps(db.documents, default=str)
    # Not in any file under the workspace, the artifacts or anything else.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(errors="replace"), path
    # Not in the environment a runner would inherit.
    assert all(token not in value for value in os.environ.values())
