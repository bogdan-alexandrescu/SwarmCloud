"""The rendered-review gate judges only the paths a lane changed itself (#2337).

On 2026-10-25 two code-only lanes started from other open PRs' branches and
ran `git merge origin/main` first (RUNNER-ERROR-KINDS, PR 2334, and
MERGE-PARK-BELL, PR 2335). Their patches carried 20 and 10 UI paths they
had not written, so their claude-code reviews' MERGE was forced to NOT_YET
"not reviewed rendered". Now the harvest records the paths of the step's own
non-merge first-parent commits and its uncommitted work as
`result_summary.git.own_files`, and the gate reads that list when it is
there. A step recorded without it keeps the whole-patch rule, so nothing
harvested before this change is waved through.

The harvest cases run real git, as `test_harvest_files.py` does: what is
under test is which commits `git log --first-parent --no-merges` walks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from test_harvest import _git, _harvest, dirs, repo  # noqa: F401 -- fixtures
from test_input_from import run_upstream
from worker_seeds import TENANT, seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

UI_FILE = "apps/swarm-ui/src/App.tsx"
API_FILE = "apps/swarm-api/swarm_api/notifications.py"


def _head(path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _write(path, rel: str, text: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _main_with_a_ui_change(path, base: str) -> None:
    """`main` moves on with a UI change the lane did not make."""
    _git(path, "checkout", "--quiet", "-b", "upstream-main", base)
    _write(path, UI_FILE, "main's page\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "main changes the UI")
    _git(path, "checkout", "--quiet", "main")


# ---------------------------------------------------------------------------
# the harvest: what counts as the step's own
# ---------------------------------------------------------------------------


@needs_git
def test_a_merge_commits_paths_are_never_the_lanes_own(repo, dirs):
    path, base = repo
    _main_with_a_ui_change(path, base)
    _write(path, API_FILE, "the lane's change\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "the lane's own commit")
    _git(path, "merge", "--quiet", "--no-edit", "upstream-main")

    work = _harvest(path, base, dirs)

    # The patch and its file list carry the merged UI file, as they always did...
    assert {f.path for f in work.files} == {API_FILE, UI_FILE}
    # ...and the lane's own list does not. MUTATION: drop --first-parent (or
    # --no-merges) and main's commit, or the merge's diff, lands here.
    assert work.own_files == (API_FILE,)
    assert work.own_files_truncated is False


@needs_git
def test_a_merge_with_no_commit_of_the_lanes_own_leaves_an_empty_list(repo, dirs):
    path, base = repo
    _main_with_a_ui_change(path, base)
    _git(path, "merge", "--quiet", "--no-ff", "--no-edit", "upstream-main")

    work = _harvest(path, base, dirs)

    assert [f.path for f in work.files] == [UI_FILE]
    # Measured and empty, which is not "not measured".
    assert work.own_files == ()


@needs_git
def test_the_lanes_own_commits_and_uncommitted_work_are_its_own(repo, dirs):
    path, base = repo
    _write(path, UI_FILE, "the lane's page\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "the lane changes the UI")
    (path / "apps" / "other").mkdir()
    _git(path, "mv", UI_FILE, "apps/other/App.tsx")
    _git(path, "commit", "--quiet", "-m", "and moves it out")
    _write(path, "docs/web-ui/new.html", "not committed\n")

    work = _harvest(path, base, dirs)

    assert set(work.own_files) == {UI_FILE, "apps/other/App.tsx", "docs/web-ui/new.html"}


@needs_git
def test_no_base_records_no_own_list(repo, dirs):
    path, _base = repo
    _write(path, UI_FILE, "x\n")
    work = _harvest(path, None, dirs)
    # MUTATION: report () here and an unmeasured step reads as "touched nothing".
    assert work.own_files is None


# ---------------------------------------------------------------------------
# the readers: own paths when recorded, the whole list otherwise
# ---------------------------------------------------------------------------


def test_the_own_list_is_read_instead_of_the_whole_file_list():
    summary = {"git": {"files": [{"path": UI_FILE}, {"path": API_FILE}],
                       "files_truncated": False,
                       "own_files": [API_FILE], "own_files_truncated": False}}
    assert verdict_mod.ui_paths_in_own_files(summary) == ([], False)
    # MUTATION: ignore own_files and the merged UI file gates the review.
    assert verdict_mod.ui_paths_in_files(summary) == ([], False)
    gated = {"git": {**summary["git"], "own_files": [UI_FILE]}}
    assert verdict_mod.ui_paths_in_files(gated) == ([UI_FILE], False)


def test_a_cut_own_list_with_no_ui_path_is_unknown():
    cut = {"git": {"files": [], "own_files": [API_FILE], "own_files_truncated": True}}
    assert verdict_mod.ui_paths_in_files(cut) == ([], True)


def test_a_step_recorded_without_an_own_list_keeps_the_whole_list():
    old = {"git": {"files": [{"path": UI_FILE}], "files_truncated": False}}
    assert verdict_mod.ui_paths_in_own_files(old) is None
    assert verdict_mod.ui_paths_in_files(old) == ([UI_FILE], False)


# ---------------------------------------------------------------------------
# the worker: a review of a lane that merged main
# ---------------------------------------------------------------------------

PATCH = "".join(
    f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n--- a/{rel}\n+++ b/{rel}\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    for rel in (API_FILE, UI_FILE)
)
MERGE = json.dumps({"verdict": "MERGE", "findings": []})


def _review(db: Any, worker_factory: Any, git: dict[str, Any] | None) -> dict[str, Any]:
    run_upstream(db, worker_factory, artifact_name="swarm-work.patch", artifact_text=PATCH)
    upstream = db.doc("tasks/task_up")
    if git is not None:
        upstream["result_summary"] = {**(upstream.get("result_summary") or {}), "git": git}
    else:
        assert "own_files" not in ((upstream.get("result_summary") or {}).get("git") or {})
    seed_attempt(db, task_id="task_2", attempt_id="att_2", lease_id="lease_2", task_input={
        "prompt": "review the patch", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "verdict.json", "artifact_text": MERGE,
    })
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "swarm-work.patch"},
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


def _git_record(own: list[str]) -> dict[str, Any]:
    return {"files": [{"path": API_FILE}, {"path": UI_FILE}], "files_truncated": False,
            "own_files": own, "own_files_truncated": False}


def test_a_lane_whose_own_commit_is_api_only_keeps_its_merge(
    db, store, worker_factory, runner_inputs
):
    # The patch carries a UI file merged in from main; the lane's own commit
    # touched only the API file.
    doc = _review(db, worker_factory, _git_record([API_FILE]))

    # MUTATION: judge by the whole patch and this reads NOT_YET.
    assert _uploaded_verdict(store) == json.loads(MERGE)
    assert "rendered_review" not in doc["result_summary"]
    assert "claude-code-browser" not in runner_inputs[-1]["prompt"]


def test_a_lane_whose_own_commit_touches_ui_is_still_gated(
    db, store, worker_factory, runner_inputs
):
    doc = _review(db, worker_factory, _git_record([API_FILE, UI_FILE]))

    uploaded = _uploaded_verdict(store)
    assert uploaded["verdict"] == "NOT_YET"
    assert uploaded["findings"][0].startswith("not reviewed rendered: ")
    assert doc["result_summary"]["rendered_review"]["ui_paths"] == [UI_FILE]
    assert UI_FILE in runner_inputs[-1]["prompt"]


def test_a_step_recorded_without_own_paths_is_judged_by_the_whole_patch(
    db, store, worker_factory
):
    doc = _review(db, worker_factory, None)

    # MUTATION: treat a missing own list as [] and this older step is waved through.
    assert _uploaded_verdict(store)["verdict"] == "NOT_YET"
    assert doc["result_summary"]["rendered_review"]["ui_paths"] == [UI_FILE]
