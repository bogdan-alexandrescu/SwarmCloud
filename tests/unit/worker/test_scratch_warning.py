"""The worker's scratch reading rides on its heartbeats, and warns before kubelet evicts (#2436).

kubelet evicts a pod past an emptyDir `sizeLimit` with no grace period, so the
worker cannot say anything at the moment of eviction. It puts its newest
reading of the scratch volumes on every HEARTBEAT event instead, warns once
per volume past two thirds of its limit, and writes an out-of-cadence reading
at that crossing (and at every further 10% of the limit), so the reading the
reconciler reports is at most one beat older than the eviction.

`scratchusage.sample` is patched: what is pinned here is the heartbeat's use
of a reading, not the walk (`test_scratch_usage.py`).
"""

from __future__ import annotations

import json

import pytest

from agent_worker import scratchusage
from agent_worker import workspace as workspace_mod
from worker_seeds import seed_attempt

GIB = 1024**3
MIB = 1024**2
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
    # MUTATION: drop `detail["scratch"] = scratch` in `Worker._heartbeat`.
    monkeypatch.setenv(scratchusage.SCRATCH_VOLUMES_ENV, "tmp=/tmp:3")
    _scripted(monkeypatch, [GIB])

    worker._heartbeat()

    (beat,) = _beats(db)
    assert beat["detail"]["scratch"] == _reading(GIB)
    # Still a whole reading: the readers of the usage figures see them.
    assert "memory_limit_bytes" in beat["detail"] and "series" in beat["detail"]


def test_tmp_past_two_thirds_warns_once_and_emits_a_fresh_reading(db, worker, monkeypatch, log_stream):
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
    # MUTATION: sample `[]` volumes instead of returning None in
    # `Worker._scratch_reading` -- the patched sample is called.
    monkeypatch.delenv(scratchusage.SCRATCH_VOLUMES_ENV, raising=False)
    calls = _scripted(monkeypatch, [GIB])

    worker._heartbeat()

    assert calls == []
    (beat,) = _beats(db)
    assert "scratch" not in beat["detail"]
