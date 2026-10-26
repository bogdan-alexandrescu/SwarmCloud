"""The claude-code agent is told where big scratch data belongs (issue #2436).

Measured 2026-10-26: task_73850f24085447368991 (Product Oracle's integration
lane) was evicted twice with `EmptyDir volume "tmp" exceeds 3Gi`, after about
two hours each, and published nothing. Its /tmp held a Postgres data dir
(/tmp/pgdata), test logs (/tmp/tr) and Chromium's unpacked libraries and
fonts. TMPDIR already pointed at the 20 GiB cache volume; the agent named
/tmp literally. So every repository step on this runner gets one sentence
after its prompt: big scratch data under $TMPDIR, never a literal /tmp, never
the workspace -- and on Cloud Run, where there is no cache volume, a shorter
one that promises none.
"""

from __future__ import annotations

from pathlib import Path

from agent_worker import cachedirs
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext


def _ctx(root: Path) -> RunnerContext:
    work = root / "work"
    artifacts = root / "artifacts"
    checkout = work / "repo"
    checkout.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "Fix the thing."},
        repo_dir=checkout,
    )


def _run(ctx: RunnerContext, monkeypatch, *, tmpdir: Path, workspace: Path,
         browsers: str | None = None) -> dict:
    seen: dict = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        seen["env"] = dict(extra_env or {})
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    monkeypatch.setattr(claude_code, "prepare_test_browsers", lambda *a, **k: None)
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setenv("SWARM_WORKSPACE", str(workspace))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmpdir.parent / "xdg"))
    if browsers is not None:
        monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, browsers)
    claude_code.body(ctx)
    return seen


def test_the_claude_code_prompt_steers_scratch_data_to_tmpdir_not_literal_tmp(
    tmp_path, monkeypatch
):
    # GKE: TMPDIR is `<cache>/tmp`, outside the attempt tree (`workspace.create`).
    cache = tmp_path / "cache-volume" / "att_1"
    seen = _run(_ctx(tmp_path), monkeypatch, tmpdir=cache / "tmp",
                workspace=tmp_path / "workspace" / "att_1")
    prompt = seen["prompt"]

    # MUTATION: stop appending the note in `body` -> this fails.
    assert prompt.startswith("Fix the thing.")
    assert prompt.count(cachedirs.scratch_note(own_volume=True)) == 1
    for phrase in ("$TMPDIR", "literal /tmp", "20 GiB", "never evicts the pod",
                   "Postgres data directory", "test logs", "unpacked libraries and fonts",
                   "3 GiB", "the workspace (at most 5 GiB", "checkpointed"):
        assert phrase in prompt, phrase


def test_the_cloud_run_note_does_not_promise_a_cache_volume(tmp_path, monkeypatch):
    # Cloud Run: TMPDIR is inside the attempt tree, on the workspace tmpfs.
    workspace = tmp_path / "workspace" / "att_1"
    seen = _run(_ctx(tmp_path), monkeypatch, tmpdir=workspace / "tmp", workspace=workspace)
    prompt = seen["prompt"]
    note = cachedirs.scratch_note(own_volume=False)

    # MUTATION: give Cloud Run the GKE sentence -> this fails.
    assert prompt.count(note) == 1
    assert "$TMPDIR" in note and "memory" in note
    assert "20 GiB" not in prompt
    assert "cache volume" not in prompt


def test_the_cache_browsers_path_reaches_the_agent_and_is_not_an_image_browser(
    tmp_path, monkeypatch
):
    """The worker's `cache_env` puts PLAYWRIGHT_BROWSERS_PATH under the cache
    on an image with no browser; the runner hands it to the agent itself
    (not through cliagent's passthrough) and says nothing of a browser."""
    cache = tmp_path / "cache-volume" / "att_1"
    seen = _run(_ctx(tmp_path), monkeypatch, tmpdir=cache / "tmp",
                workspace=tmp_path / "workspace" / "att_1",
                browsers=str(cache / cachedirs.BROWSERS_DIR_NAME))

    # MUTATION: read the cache's path as an image's browser -> BROWSER_NOTE
    # is added and this fails.
    assert seen["env"][claude_code.BROWSERS_PATH_ENV] == str(cache / cachedirs.BROWSERS_DIR_NAME)
    assert claude_code.BROWSER_NOTE not in seen["prompt"]


def test_the_note_is_after_the_prompt_and_before_the_browser_note(tmp_path, monkeypatch):
    cache = tmp_path / "cache-volume" / "att_1"
    browsers = tmp_path / "playwright"
    browsers.mkdir()
    seen = _run(_ctx(tmp_path), monkeypatch, tmpdir=cache / "tmp",
                workspace=tmp_path / "workspace" / "att_1", browsers=str(browsers))

    assert seen["prompt"].endswith(claude_code.BROWSER_NOTE)
    assert cachedirs.scratch_note(own_volume=True) in seen["prompt"]
    assert seen["env"][claude_code.BROWSERS_PATH_ENV] == str(browsers)


def test_no_note_without_a_worker_built_tmpdir():
    """Only the worker's TMPDIR is named: without it there is nothing to point at."""
    assert claude_code.scratch_note_for({}) is None
    assert claude_code.scratch_note_for({"TMPDIR": "/c/att_1/tmp"}) is None
    assert claude_code.scratch_note_for(
        {"TMPDIR": "/c/att_1/tmp", "SWARM_WORKSPACE": "/w/att_1"}
    ) == cachedirs.scratch_note(own_volume=True)
    assert claude_code.scratch_note_for(
        {"TMPDIR": "/w/att_1/tmp", "SWARM_WORKSPACE": "/w/att_1"}
    ) == cachedirs.scratch_note(own_volume=False)


def test_the_mirrored_sizes_are_the_dispatchers():
    """The note's numbers are mirrored from scheduler/dispatch.py (the worker
    does not import the scheduler at run time)."""
    from scheduler.dispatch import CACHE_VOLUME_GIB, GKE_DISK

    assert cachedirs.CACHE_VOLUME_GIB == CACHE_VOLUME_GIB
    assert {d.tmp_gib for d in GKE_DISK.values()} == {cachedirs.TMP_VOLUME_GIB}
    assert max(d.workspace_gib for d in GKE_DISK.values()) == cachedirs.WORKSPACE_VOLUME_GIB
