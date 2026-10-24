"""A UI lane's MERGE counts only from a review that saw the pages rendered (#2184).

BGD's decision, 2026-10-24: on 2026-10-23 ten mock-up lanes' reviews ran on
`claude-code`, with no browser, and gave MERGE on pages they had only read as
HTML; the pages had clipped frames and contradictory counts. Now a review
whose patch touches `apps/swarm-ui/**` or `docs/web-ui/**` has its MERGE
recorded as NOT_YET, with the reason, unless it ran on `claude-code-browser`
and left `review.md` and an image under `shots/`. A lane that touches no UI
path, and a NOT_YET, are unchanged.

The worker cases run the production worker over in-memory Firestore and a
local object store, with the mock runner (whose profile is not the browser's,
as `claude-code`'s is not); the profile and the artifacts are the pure
function's inputs, so the browser cases are proved there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream

UI_PATCH = """diff --git a/apps/swarm-ui/src/App.tsx b/apps/swarm-ui/src/App.tsx
index 1111111..2222222 100644
--- a/apps/swarm-ui/src/App.tsx
+++ b/apps/swarm-ui/src/App.tsx
@@ -1 +1 @@
-old
+new
"""
PLAIN_PATCH = UI_PATCH.replace("apps/swarm-ui/src/App.tsx", "apps/scheduler/scheduler/drain.py")
MERGE = json.dumps({"verdict": "MERGE", "findings": []})


def _review(db: Any, worker_factory: Any, patch: str, verdict_text: str = MERGE) -> dict[str, Any]:
    run_upstream(db, worker_factory, artifact_name="swarm-work.patch", artifact_text=patch)
    seed_attempt(db, task_id="task_2", attempt_id="att_2", lease_id="lease_2", task_input={
        "prompt": "review the patch", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "verdict.json", "artifact_text": verdict_text,
    })
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "swarm-work.patch"},
        # A dependant stages `verdict.json` from it: this step IS a review.
        "expected_outputs": ["verdict.json"],
    }
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2")
    assert worker.run() == ExitCode.OK
    doc = db.doc("tasks/task_2")
    assert doc["state"] == TaskState.SUCCEEDED.value
    return doc


def _uploaded_verdict(store: LocalObjectStore) -> dict[str, Any]:
    key = f"tenants/{TENANT}/tasks/task_2/attempts/att_2/artifacts/verdict.json"
    return json.loads(store.download_bytes(key))


def test_a_ui_lanes_merge_from_a_review_without_a_browser_reads_not_yet(
    db, store, worker_factory, runner_inputs, tmp_path
):
    doc = _review(db, worker_factory, UI_PATCH)

    uploaded = _uploaded_verdict(store)
    # MUTATION: skip the rewrite and the MERGE the review gave goes up.
    assert uploaded["verdict"] == "NOT_YET"
    reason = uploaded["findings"][0]
    assert reason.startswith("not reviewed rendered: ")
    assert "'mock', not 'claude-code-browser'" in reason
    assert "no review.md" in reason and "no image under shots/" in reason
    # And the dependant's gate reads it as NOT_YET, so the fix step runs.
    staged = tmp_path / "staged-verdict.json"
    staged.write_text(json.dumps(uploaded))
    read = verdict_mod.read_verdict(staged, task_id="task_2", filename="verdict.json")
    assert read.verdict == "NOT_YET" and read.findings[0] == reason

    record = doc["result_summary"]["rendered_review"]
    assert record["verdict_given"] == "MERGE" and record["verdict"] == "NOT_YET"
    assert record["reason"] == reason
    assert record["ui_paths"] == ["apps/swarm-ui/src/App.tsx"]

    # The rule is in the review's own instructions, before it writes a verdict.
    prompt = runner_inputs[-1]["prompt"]
    assert prompt.startswith("review the patch")
    assert "claude-code-browser" in prompt and "review.md" in prompt and "shots/" in prompt
    assert "apps/swarm-ui/src/App.tsx" in prompt


def test_a_lane_that_touches_no_ui_path_keeps_its_merge(
    db, store, worker_factory, runner_inputs
):
    doc = _review(db, worker_factory, PLAIN_PATCH)

    # MUTATION: gate every review and this reads NOT_YET.
    assert _uploaded_verdict(store) == json.loads(MERGE)
    assert "rendered_review" not in doc["result_summary"]
    assert "claude-code-browser" not in runner_inputs[-1]["prompt"]


def test_a_ui_lanes_not_yet_is_left_as_the_review_wrote_it(db, store, worker_factory):
    text = json.dumps({"verdict": "NOT_YET", "findings": ["the frame clips"]})
    doc = _review(db, worker_factory, UI_PATCH, verdict_text=text)

    assert _uploaded_verdict(store) == json.loads(text)
    assert doc["result_summary"]["rendered_review"]["verdict"] == "NOT_YET"
    assert "reason" not in doc["result_summary"]["rendered_review"]


# ---------------------------------------------------------------------------
# the pure gate: profile and artifacts
# ---------------------------------------------------------------------------


def _verdict_file(tmp_path: Path, verdict: str = "MERGE") -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    path = artifacts / "verdict.json"
    path.write_text(json.dumps({"verdict": verdict, "findings": [{"summary": "nit"}]}))
    return path


def _gate(path: Path, profile: str) -> dict[str, Any] | None:
    return verdict_mod.gate_rendered_review(
        path, ui_paths=["docs/web-ui/index.html"], runner_profile=profile,
        artifacts=path.parent,
    )


def test_a_merge_from_claude_code_reads_not_yet_with_the_reason(tmp_path):
    path = _verdict_file(tmp_path)
    (path.parent / "review.md").write_text("looked")
    (path.parent / "shots").mkdir()
    (path.parent / "shots" / "home.png").write_bytes(b"\x89PNG")

    record = _gate(path, "claude-code")

    written = json.loads(path.read_text())
    assert written["verdict"] == "NOT_YET"
    assert written["findings"][0] == record["reason"]
    assert written["findings"][1] == {"summary": "nit"}
    assert record["missing"] == ["ran on runner profile 'claude-code', not 'claude-code-browser'"]


def test_a_merge_from_the_browser_without_shots_reads_not_yet(tmp_path):
    path = _verdict_file(tmp_path)
    (path.parent / "review.md").write_text("looked")
    (path.parent / "shots").mkdir()
    # Not an image, and an empty image: neither is a screenshot.
    (path.parent / "shots" / "notes.txt").write_text("x")
    (path.parent / "shots" / "blank.png").write_bytes(b"")

    record = _gate(path, "claude-code-browser")

    # MUTATION: count any file under shots/, or an empty one, and this is MERGE.
    assert json.loads(path.read_text())["verdict"] == "NOT_YET"
    assert record["missing"] == ["no image under shots/ in its artifacts"]
    assert record["reason"] == "not reviewed rendered: no image under shots/ in its artifacts"


def test_a_merge_from_the_browser_without_review_md_reads_not_yet(tmp_path):
    path = _verdict_file(tmp_path)
    (path.parent / "shots" / "nested").mkdir(parents=True)
    (path.parent / "shots" / "nested" / "page.PNG").write_bytes(b"\x89PNG")

    record = _gate(path, "claude-code-browser")

    assert json.loads(path.read_text())["verdict"] == "NOT_YET"
    assert record["missing"] == ["no review.md in its artifacts"]


def test_a_rendered_review_on_the_browser_stays_merge(tmp_path):
    path = _verdict_file(tmp_path)
    before = path.read_text()
    (path.parent / "review.md").write_text("rendered at 1280 and 390 wide")
    (path.parent / "shots" / "nested").mkdir(parents=True)
    (path.parent / "shots" / "nested" / "page.webp").write_bytes(b"RIFF")

    record = _gate(path, "claude-code-browser")

    # MUTATION: downgrade every UI-gated MERGE and this is NOT_YET.
    assert path.read_text() == before
    assert record == {"ui_paths": ["docs/web-ui/index.html"],
                      "verdict_given": "MERGE", "verdict": "MERGE"}


def test_an_unreadable_verdict_is_left_for_the_gate_to_refuse(tmp_path):
    path = _verdict_file(tmp_path)
    path.write_text("not json")
    assert _gate(path, "claude-code") is None
    assert path.read_text() == "not json"


def test_the_patch_paths_that_make_a_review_ui_gated():
    assert verdict_mod.ui_paths_in_patch(UI_PATCH) == ["apps/swarm-ui/src/App.tsx"]
    assert verdict_mod.ui_paths_in_patch(PLAIN_PATCH) == []
    created = ("diff --git a/docs/web-ui/new.html b/docs/web-ui/new.html\nnew file mode 100644\n"
               "--- /dev/null\n+++ b/docs/web-ui/new.html\n")
    assert verdict_mod.ui_paths_in_patch(created) == ["docs/web-ui/new.html"]
    moved = ("diff --git a/apps/swarm-ui/a.ts b/apps/other/a.ts\n"
             "rename from apps/swarm-ui/a.ts\nrename to apps/other/a.ts\n")
    # Moving a file out of the UI changes what people see too.
    assert verdict_mod.ui_paths_in_patch(moved) == ["apps/swarm-ui/a.ts"]
    # A body line that merely mentions a UI path is not a header.
    assert verdict_mod.ui_paths_in_patch(PLAIN_PATCH + "+see apps/swarm-ui/x.ts\n") == []


def test_a_builds_on_steps_file_list_is_read_and_a_cut_one_is_unknown():
    summary = {"git": {"files": [{"path": "apps/swarm-ui/src/x.tsx", "old_path": None}],
                       "files_truncated": False}}
    assert verdict_mod.ui_paths_in_files(summary) == (["apps/swarm-ui/src/x.tsx"], False)
    plain = {"git": {"files": [{"path": "apps/scheduler/s.py"}], "files_truncated": False}}
    assert verdict_mod.ui_paths_in_files(plain) == ([], False)
    cut = {"git": {"files": [{"path": "apps/scheduler/s.py"}], "files_truncated": True}}
    assert verdict_mod.ui_paths_in_files(cut) == ([], True)
