"""A review's runtime reads the paths its parent's OWN work changed (#2337).

A lane that merges main, or the pull request it builds on, carries every UI
file that merge brought into its `result_summary.git.files`. The worker's
rendered-review gate already judges by `result_summary.git.own_files` when a
step recorded it (`agent_worker.verdict.ui_paths_in_own_files`); the
scheduler's `browser_if_ui` resolution must read the same list, or a review
of an API-only lane that merged main's UI work is sent to the browser
runtime. A parent harvested before #2337 has no `own_files` and keeps the
whole-patch rule.
"""

from __future__ import annotations

from typing import Any

from scheduler import runtime as runtime_mod

POLICY = {"policy": "browser_if_ui", "browser": runtime_mod.BROWSER_PROFILE,
          "code": runtime_mod.CODE_PROFILE}
MERGED_UI_FILE = "apps/swarm-ui/src/App.tsx"
API_FILE = "apps/swarm-api/swarm_api/routes/tasks.py"


def _summary(files: list[str], own: list[str] | None = None, *,
             own_truncated: bool = False) -> dict[str, Any]:
    git: dict[str, Any] = {"files": [{"path": p, "status": "M"} for p in files],
                           "files_truncated": False}
    if own is not None:
        git["own_files"] = own
        git["own_files_truncated"] = own_truncated
    return {"git": git}


def _resolve(summary: dict[str, Any]) -> runtime_mod.Resolution:
    documents = {"parent": {"result_summary": summary}}
    return runtime_mod.resolve(["parent"], POLICY, documents.get)


def test_a_ui_file_merged_in_but_not_changed_resolves_to_code():
    resolution = _resolve(_summary([MERGED_UI_FILE, API_FILE], own=[API_FILE]))
    assert resolution.profile == runtime_mod.CODE_PROFILE, resolution
    assert resolution.reason == runtime_mod.REASON_NO_UI
    assert resolution.ui_paths == ()


def test_a_ui_file_the_parent_changed_itself_resolves_to_the_browser():
    resolution = _resolve(_summary([MERGED_UI_FILE, API_FILE], own=[MERGED_UI_FILE]))
    assert resolution.profile == runtime_mod.BROWSER_PROFILE
    assert resolution.ui_paths == (MERGED_UI_FILE,)


def test_a_parent_with_no_own_files_keeps_the_whole_patch_rule():
    resolution = _resolve(_summary([MERGED_UI_FILE, API_FILE]))
    assert resolution.profile == runtime_mod.BROWSER_PROFILE
    assert resolution.reason == runtime_mod.REASON_UI


def test_a_cut_own_list_with_no_ui_path_resolves_to_the_browser():
    resolution = _resolve(_summary([API_FILE], own=[API_FILE], own_truncated=True))
    assert resolution.profile == runtime_mod.BROWSER_PROFILE
    assert resolution.reason == runtime_mod.REASON_CUT


def test_own_test_files_are_not_ui():
    own = ["apps/swarm-ui/src/__tests__/App.test.tsx", API_FILE]
    resolution = _resolve(_summary([MERGED_UI_FILE, *own], own=own))
    assert resolution.profile == runtime_mod.CODE_PROFILE


def test_files_of_matches_the_worker_reader_on_own_files():
    from agent_worker import verdict as verdict_mod

    for summary in (
        _summary([MERGED_UI_FILE, API_FILE], own=[API_FILE]),
        _summary([MERGED_UI_FILE, API_FILE], own=[MERGED_UI_FILE]),
        _summary([API_FILE], own=[API_FILE], own_truncated=True),
        _summary([MERGED_UI_FILE]),
    ):
        paths, why = runtime_mod.files_of({"result_summary": summary})
        worker_paths, unknown = verdict_mod.ui_paths_in_files(summary)
        assert paths == worker_paths, summary
        assert (why == runtime_mod.REASON_CUT) == unknown, summary
