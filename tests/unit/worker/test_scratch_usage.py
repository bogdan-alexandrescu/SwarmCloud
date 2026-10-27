"""What fills a scratch volume, read before kubelet evicts the pod for it (#2436).

kubelet evicts a pod whose emptyDir passes its `sizeLimit` with no grace
period, so the worker reads its volumes on every heartbeat instead
(`agent_worker.scratchusage`), and the reconciler reports the newest reading.
These pin the reading against a volume of known contents: allocated bytes, the
largest paths first, a descent through a single wrapper directory, a bounded
walk that says so, and paths that are cut and redacted.

Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from agent_worker import cachedirs
from agent_worker import workspace as workspace_mod
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext
from worker_seeds import seed_attempt

MIB = 1024**2
GIB = 1024**3
#: Directory blocks and file tails: what block rounding may add to a size.
ROUNDING = 256 * 1024


def _file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written, not truncated: a sparse file allocates nothing, and the
    # reading counts allocated blocks as kubelet does.
    path.write_bytes(b"x" * size)


def _shown(path: Path) -> str:
    """A path as a reading reports it: cut at `PATH_CHARS`, however long the test's tmp path is."""
    from agent_worker.scratchusage import PATH_CHARS

    return str(path)[:PATH_CHARS]


def _fill(root: Path) -> None:
    for i in range(3):
        _file(root / "pgdata" / "base" / f"rel{i}", 10 * MIB)
    _file(root / "tr" / "run.log", 10 * MIB)
    _file(root / "ms-playwright" / "chromium" / "chrome", 5 * MIB)
    for i in range(4):
        _file(root / f"note{i}.txt", 100)


def test_sample_names_the_largest_paths_of_a_volume_with_known_contents(tmp_path: Path):
    # MUTATION: sort `top` ascending in `scratchusage.sample` -- the small
    # notes come first and the order assertion fails.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "tmp"
    _fill(volume)

    reading = sample([("tmp", volume, 3 * GIB)])

    entry = reading["tmp"]
    assert entry["limit_bytes"] == 3 * GIB
    assert entry["partial"] is False
    paths = [path for path, _ in entry["top"]]
    assert paths[:3] == [_shown(volume / "pgdata"), _shown(volume / "tr"), _shown(volume / "ms-playwright")]
    assert len(entry["top"]) == 5
    sizes = dict((path, size) for path, size in entry["top"])
    for name, expected in (("pgdata", 30 * MIB), ("tr", 10 * MIB), ("ms-playwright", 5 * MIB)):
        assert expected <= sizes[_shown(volume / name)] < expected + ROUNDING, name
    assert 45 * MIB <= entry["used_bytes"] < 45 * MIB + ROUNDING


def test_sample_descends_through_a_single_wrapper_directory(tmp_path: Path):
    # MUTATION: set `MAX_DESCENT` to 0 -- the report names the wrapper alone.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "workspace"
    wrapper = volume / "att_1" / "work"
    _fill(wrapper)
    _file(volume / "stray.txt", 100)

    entry = sample([("workspace", volume, 5 * GIB)])["workspace"]

    assert [path for path, _ in entry["top"]][:3] == [
        _shown(wrapper / "pgdata"), _shown(wrapper / "tr"), _shown(wrapper / "ms-playwright"),
    ]


def test_a_bounded_walk_says_it_is_partial(tmp_path: Path):
    # MUTATION: return `False` for partial at the entry bound -- the floor
    # reads as the whole volume.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "tmp"
    for i in range(50):
        _file(volume / f"f{i:02d}", 100)

    assert sample([("tmp", volume, GIB)], max_entries=10)["tmp"]["partial"] is True
    assert sample([("tmp", volume, GIB)])["tmp"]["partial"] is False


def test_paths_are_cut_and_redacted(tmp_path: Path, monkeypatch):
    # MUTATION: cut before redacting in `scratchusage._shown` -- the secret's
    # head survives the cut, unredacted.
    from agent_worker import scratchusage
    from agent_worker.redact import REDACTED, scrub_text
    from agent_worker.scratchusage import sample

    value = secrets.token_hex(24)
    volume = tmp_path / "tmp"
    # The cut is set 60 characters into the directory's name, whatever the
    # tmp path's length, and the secret starts 20 before it: a path cut first
    # would keep the secret's head and leave nothing for the redactor to match.
    cut = len(str(volume) + "/") + 60
    monkeypatch.setattr(scratchusage, "PATH_CHARS", cut)
    _file(volume / ("a" * 40 + value + "b" * 100) / "blob", MIB)

    entry = sample([("tmp", volume, GIB)], scrub=lambda text: scrub_text(text, [value]))["tmp"]

    path = entry["top"][0][0]
    assert value not in path and value[:16] not in path
    assert REDACTED in path
    assert len(path) == cut


def test_format_top_reads_as_path_and_size_pairs():
    from agent_worker.scratchusage import format_top

    assert format_top({"top": [["/tmp/pgdata", 30 * MIB], ["/tmp/tr", 10 * MIB]]}) == (
        "/tmp/pgdata 30.0 MiB, /tmp/tr 10.0 MiB"
    )
    assert format_top({"top": [["/tmp/pgdata", int(2.1 * GIB)], ["/tmp/ms-playwright", int(0.7 * GIB)]]}) == (
        "/tmp/pgdata 2.1 GiB, /tmp/ms-playwright 716.8 MiB"
    )
    assert format_top({"top": []}) == ""


def test_parse_volumes_reads_the_dispatchers_value_and_refuses_a_malformed_one():
    from agent_worker.scratchusage import parse_volumes, warn_threshold

    assert parse_volumes("") == []
    assert parse_volumes(None) == []
    assert parse_volumes("workspace=/workspace:5,tmp=/tmp:3,home=/home/swarm:2") == [
        ("workspace", Path("/workspace"), 5 * GIB),
        ("tmp", Path("/tmp"), 3 * GIB),
        ("home", Path("/home/swarm"), 2 * GIB),
    ]
    for bad in ("tmp=/tmp", "tmp=tmp:3", "tmp=/tmp:x", "tmp=/tmp:0", "=/tmp:3", "tmp=/tmp:3,junk"):
        assert parse_volumes(bad) == [], bad
    assert warn_threshold(3 * GIB) == 2 * GIB


def test_a_reading_stays_within_its_share_of_the_event():
    # MUTATION: return `reading` unchanged from `bounded` -- three volumes of
    # long paths outgrow the event's size cap.
    import json

    from agent_worker.scratchusage import SCRATCH_DETAIL_MAX_BYTES, bounded

    long = "/" + "p" * 119
    reading = {
        name: {"used_bytes": used, "limit_bytes": 3 * GIB, "top": [[long, 1] for _ in range(5)], "partial": False}
        for name, used in (("workspace", GIB), ("tmp", int(2.5 * GIB)), ("home", 0))
    }

    out = bounded(reading)

    assert len(json.dumps(out).encode()) <= SCRATCH_DETAIL_MAX_BYTES
    assert len(out["tmp"]["top"]) == 5, "the fullest volume keeps its paths"


# ---------------------------------------------------------------------------
# PLAYWRIGHT_BROWSERS_PATH goes under the cache when the image sets none
# ---------------------------------------------------------------------------


def test_playwright_browsers_path_is_under_the_cache_when_the_image_sets_none(
    tmp_path, monkeypatch
):
    """Issue #2436: task_73850f24085447368991 was evicted twice with
    `EmptyDir volume "tmp" exceeds 3Gi`, Chromium's files among what filled
    /tmp. An image that installs no browser leaves Playwright's install to
    the agent: it goes to the cache volume, by name."""
    from agent_worker import workspace as workspace_mod
    from agent_worker.cachedirs import (
        BROWSERS_DIR_NAME,
        BROWSERS_PATH_ENV,
        cache_env,
        image_browsers_path,
    )

    monkeypatch.delenv(BROWSERS_PATH_ENV, raising=False)
    cache_root = tmp_path / "cache-volume"
    cache_root.mkdir()
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)

    env = ws.child_env()

    # MUTATION: drop PLAYWRIGHT_BROWSERS_PATH from `cache_env` -> this fails.
    assert env[BROWSERS_PATH_ENV] == str(ws.cache / BROWSERS_DIR_NAME)
    assert Path(env[BROWSERS_PATH_ENV]).resolve().is_relative_to(cache_root.resolve())
    assert cache_env(ws.cache, ws.tmp, own_volume=True)[BROWSERS_PATH_ENV] == env[BROWSERS_PATH_ENV]
    # The runner reads it as the cache's, not as an image's browser.
    assert image_browsers_path(env) == ""


def test_the_browser_images_playwright_path_is_kept(tmp_path, monkeypatch):
    """agent-runtime-browser's /opt/playwright (its ENV, which lifecycle's
    passthrough carries into `base`) is the browser the runner hands on; the
    cache's default must never replace it."""
    from agent_worker import workspace as workspace_mod
    from agent_worker.cachedirs import BROWSERS_PATH_ENV, cache_env, image_browsers_path

    monkeypatch.setenv(BROWSERS_PATH_ENV, "/opt/playwright")
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=tmp_path / "c")

    env = ws.child_env({BROWSERS_PATH_ENV: "/opt/playwright"})

    # MUTATION: ignore the worker's own PLAYWRIGHT_BROWSERS_PATH in
    # `cache_env` -> this fails.
    assert env[BROWSERS_PATH_ENV] == "/opt/playwright"
    assert BROWSERS_PATH_ENV not in cache_env(ws.cache, ws.tmp, own_volume=True)
    assert BROWSERS_PATH_ENV not in cache_env(
        ws.cache, ws.tmp, own_volume=True, browsers_path_set=True)
    assert image_browsers_path(env) == "/opt/playwright"


def test_the_live_browser_stub_is_not_overwritten_by_the_cli_runners_passthrough():
    """cliagent copies `_PLAIN_PASSTHROUGH` from the runner's environment
    AFTER the runner's own `extra_env`, so a PLAYWRIGHT_BROWSERS_PATH there
    would replace claude-code's live-browser stub (`AGENT_BROWSERS_PATH`)
    with the image's real Chromium. The claude-code runner hands the cache's
    path on itself instead."""
    from agent_worker.cachedirs import AGENT_CACHE_ENV, BROWSERS_PATH_ENV
    from agent_worker.runners import cliagent

    assert BROWSERS_PATH_ENV not in AGENT_CACHE_ENV
    assert BROWSERS_PATH_ENV not in cliagent._PLAIN_PASSTHROUGH


# ---------------------------------------------------------------------------
# The claude-code agent is told where big scratch data belongs
# ---------------------------------------------------------------------------
# Every repository step on this runner gets one sentence after its prompt:
# big scratch data under $TMPDIR, never a literal /tmp, never the workspace --
# and on Cloud Run, where there is no cache volume, a shorter one that
# promises none.


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


# ---------------------------------------------------------------------------
# The reading rides on the heartbeats, and warns before kubelet evicts
# ---------------------------------------------------------------------------
# kubelet evicts with no grace period, so the worker puts its newest reading
# on every HEARTBEAT event, warns once per volume past two thirds of its
# limit, and writes an out-of-cadence reading at that crossing (and at every
# further 10% of the limit). `scratchusage.sample` is patched: what is pinned
# here is the heartbeat's use of a reading, not the walk (above).


WARNING = "past it kubelet evicts this pod"


def _reading(used: int) -> dict:
    return {
        "tmp": {
            "used_bytes": used,
            "limit_bytes": 3 * GIB,
            "top": [["/tmp/pgdata", used - 700 * MIB], ["/tmp/ms-playwright", 700 * MIB]],
            "partial": False,
        }
    }


@pytest.fixture
def worker(db, worker_factory, monkeypatch):
    seed_attempt(db)
    built, config, _ = worker_factory()
    built.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    return built


def _scripted(monkeypatch, readings: list[int]) -> list[object]:
    from agent_worker import scratchusage

    calls: list[object] = []

    def fake_sample(volumes, **_kwargs):
        calls.append(volumes)
        return _reading(readings.pop(0))

    monkeypatch.setattr(scratchusage, "sample", fake_sample)
    return calls


def _beats(db) -> list[dict]:
    return [e for e in db.events("task_1") if e.get("type") == "heartbeat"]


def _warnings(log_stream) -> list[dict]:
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    return [line for line in lines if WARNING in str(line.get("message"))]


def test_heartbeat_event_carries_the_scratch_reading(db, worker, monkeypatch):
    from agent_worker import scratchusage

    # MUTATION: drop `detail["scratch"] = scratch` in `Worker._heartbeat`.
    monkeypatch.setenv(scratchusage.SCRATCH_VOLUMES_ENV, "tmp=/tmp:3")
    _scripted(monkeypatch, [GIB])

    worker._heartbeat()

    (beat,) = _beats(db)
    assert beat["detail"]["scratch"] == _reading(GIB)
    # Still a whole reading: the readers of the usage figures see them.
    assert "memory_limit_bytes" in beat["detail"] and "series" in beat["detail"]


def test_tmp_past_two_thirds_warns_once_and_emits_a_fresh_reading(db, worker, monkeypatch, log_stream):
    from agent_worker import scratchusage

    # MUTATION: drop the `or fresh` from the emit condition in
    # `Worker._heartbeat` -- the crossing waits for the next fifth beat, and
    # the event count after beat 2 is 1.
    monkeypatch.setenv(scratchusage.SCRATCH_VOLUMES_ENV, "tmp=/tmp:3")
    used = [GIB, int(2.2 * GIB), int(2.25 * GIB), int(2.6 * GIB)]
    _scripted(monkeypatch, list(used))

    worker._heartbeat()  # beat 1: the cadence's event, under the threshold
    assert len(_beats(db)) == 1
    assert _warnings(log_stream) == []

    worker._heartbeat()  # beat 2: past two thirds -- one warning, one extra event
    (warning,) = _warnings(log_stream)
    assert warning["message"] == (
        "/tmp is at 2.2 GiB of its 3 GiB limit; past it kubelet evicts this pod. "
        "Largest: /tmp/pgdata 1.5 GiB, /tmp/ms-playwright 700.0 MiB. "
        "Move large scratch data under $TMPDIR (the cache volume)."
    )
    beats = _beats(db)
    assert len(beats) == 2
    assert beats[-1]["detail"]["scratch"]["tmp"]["used_bytes"] == used[1]

    worker._heartbeat()  # beat 3: still over, grown under 10% of the limit
    assert len(_warnings(log_stream)) == 1
    assert len(_beats(db)) == 2

    worker._heartbeat()  # beat 4: another 10% of the limit -- a fresh reading, no new warning
    assert len(_warnings(log_stream)) == 1
    beats = _beats(db)
    assert len(beats) == 3
    assert beats[-1]["detail"]["scratch"]["tmp"]["used_bytes"] == used[3]


def test_nothing_is_sampled_without_scratch_volumes(db, worker, monkeypatch):
    from agent_worker import scratchusage

    # MUTATION: sample `[]` volumes instead of returning None in
    # `Worker._scratch_reading` -- the patched sample is called.
    monkeypatch.delenv(scratchusage.SCRATCH_VOLUMES_ENV, raising=False)
    calls = _scripted(monkeypatch, [GIB])

    worker._heartbeat()

    assert calls == []
    (beat,) = _beats(db)
    assert "scratch" not in beat["detail"]
