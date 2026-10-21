"""`scripts/build-images.sh` reads Cloud Build's status sparingly, and a 429
from that quota is waited out rather than reported as a broken image.

THE FAILURE THIS PINS (CB-429-RETRY, epic #1822). Main's image build failed 27
times in two bursts -- 2026-10-20 08:04-11:24Z over 18 commits and 2026-10-21
02:44-03:22Z over 10 -- and four dev releases with it, every one on

    429 RESOURCE_EXHAUSTED ... cloudbuild.googleapis.com/get_requests

the per-minute status-read quota of saga-agents-staging, a project SHARED with
another team that draws on the same quota. Every main commit runs its own
build, and each ran up to `--parallel` (4) `gcloud builds submit` processes at
once, each streaming its log and polling its own build. Nothing was wrong with
any image: the poll was refused, gcloud exited non-zero, and the script called
that a failed build.

Asserted against the REAL script with the fake `gcloud` of
test_build_images_concurrency.py, which models both shapes: a blocking submit
records the status reads gcloud makes on its own while it streams, and an
`--async` submit returns at once and leaves the build to `builds list` /
`builds describe` / `builds log`.

WHAT THIS CANNOT PROVE: real gcloud's polling rate, or that the quota stops
running out. Only main's build runs over the next busy day show that.
"""

from __future__ import annotations

from .test_build_images_concurrency import _failure_summary, _run

FOUR = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]


def _status_reads(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "status"]


def test_a_429_on_the_first_status_read_is_retried_and_the_build_succeeds(tmp_path):
    root, proc, events = _run(
        tmp_path, ["swarm-api"], parallel=1, seconds=0.3, FAKE_429_STATUS="1"
    )
    # MUTATION: treat a failed status read as a failed build (what the blocking
    # `gcloud builds submit` did, and what drop the quota check in
    # gcloud_quota_retry does) and this exits 1 naming swarm-api.
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert [e for e in events if e["event"] == "429"], (
        "the fake never refused a status read, so this test proved nothing"
    )
    # MUTATION: drop the warning in gcloud_quota_retry and nothing in the job
    # log says why a build took longer than it should have.
    assert "RESOURCE_EXHAUSTED" in proc.stderr and "attempt 1/" in proc.stderr, (
        "the retry is not logged with its attempt number: " + proc.stderr[-2000:]
    )
    assert (root / "build" / "images-dev.json").exists(), "the build succeeded but no manifest was written"


def test_a_429_on_a_submit_is_retried_and_the_build_succeeds(tmp_path):
    root, proc, events = _run(
        tmp_path, ["swarm-api"], parallel=1, seconds=0.2, FAKE_429_SUBMIT="2"
    )
    # MUTATION: remove the retry loop around `builds submit` in launch_build
    # and the first refused submit fails the image.
    assert proc.returncode == 0, proc.stderr[-3000:]
    refused = [e for e in events if e["event"] == "429" and e["kind"] == "submit"]
    assert len(refused) == 2, f"expected two refused submits, saw {refused}"
    started = [e for e in events if e["event"] == "start"]
    assert len(started) == 1, f"a refused submit created no build, so one build is expected: {started}"
    assert "attempt 2/" in proc.stderr, proc.stderr[-2000:]


def test_a_429_that_never_ends_fails_the_submit_after_bounded_attempts(tmp_path):
    _, proc, events = _run(
        tmp_path, ["swarm-api"], parallel=1, seconds=0.2,
        FAKE_429_SUBMIT="1000", BUILD_QUOTA_RETRIES="3",
    )
    # MUTATION: make the submit retry unbounded and this run never ends
    # (subprocess timeout).
    assert proc.returncode != 0, "the submit was never accepted and the script exited 0"
    refused = [e for e in events if e["event"] == "429" and e["kind"] == "submit"]
    assert len(refused) == 3, f"BUILD_QUOTA_RETRIES=3 allowed {len(refused)} submits"
    assert "swarm-api" in _failure_summary(proc), proc.stderr[-2000:]


def test_a_real_build_failure_still_fails_the_script(tmp_path):
    # Three status reads refused as well, so a FAILURE read after a 429 is
    # still a failure: the retry must not swallow it.
    root, proc, events = _run(
        tmp_path, ["swarm-api", "swarm-ui"], parallel=2, seconds=0.3,
        fail=("swarm-api",), FAKE_429_STATUS="3",
    )
    # MUTATION: map every terminal status to success in read_build_states
    # (or `|| true` the describe) and this exits 0 over a failed image.
    assert proc.returncode != 0, "swarm-api's build FAILED and the script exited 0"
    summary = _failure_summary(proc)
    assert "swarm-api" in summary and "swarm-ui" not in summary, summary
    assert not (root / "build" / "images-dev.json").exists(), "a manifest was written for a failed build"


def test_four_builds_are_waited_on_by_one_status_loop(tmp_path):
    seconds = 3.0
    interval = 1
    _, proc, events = _run(
        tmp_path, FOUR, parallel=4, seconds=seconds, BUILD_STATUS_INTERVAL=str(interval)
    )
    assert proc.returncode == 0, proc.stderr[-3000:]

    starts = [e for e in events if e["event"] == "start"]
    assert len(starts) == 4, starts
    # MUTATION: drop --async from SUBMIT_ARGS and every submit blocks,
    # streaming and polling its own build.
    assert all(e["asynchronous"] for e in starts), (
        "a build was submitted without --async, so its gcloud polls the build on its own"
    )

    reads = _status_reads(events)
    # The old shape: four submits each polling every 0.1 s for 3 s is about
    # 120 reads (and in real time, four reads a second for the whole build).
    # The one loop reads ONE list per round, every `interval` seconds, and
    # describes a build only once the list no longer shows it.
    lists = [e for e in reads if e["kind"] == "list"]
    describes = [e for e in reads if e["kind"] == "describe"]
    streams = [e for e in reads if e["kind"] == "stream"]
    # MUTATION: poll each build by itself (a describe per build per round)
    # and describes outnumber the builds several times over.
    assert not streams, f"{len(streams)} status reads came from blocking submits"
    assert len(describes) <= 2 * len(FOUR), (
        f"{len(describes)} describes for {len(FOUR)} builds: builds are being polled one by one"
    )
    rounds_allowed = int(seconds / interval) + 3
    # MUTATION: set the status read to happen every scheduler tick (0.1 s
    # here) and the list is read ~30 times.
    assert 1 <= len(lists) <= rounds_allowed, (
        f"{len(lists)} list reads for a {seconds}s build read every {interval}s"
    )
    assert all(e["ongoing"] for e in lists), "the list read was not narrowed to --ongoing builds"
    times = sorted(e["t"] for e in lists)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # MUTATION: schedule the next round from the whole second the last one
    # ended in (date +%s rounds down) without the extra second, and two rounds
    # can land a few milliseconds apart.
    assert all(gap >= 0.9 * interval for gap in gaps), f"status rounds closer than {interval}s: {gaps}"
    old_shape = len(FOUR) * seconds / 0.1
    assert len(reads) * 5 < old_shape, (
        f"{len(reads)} status reads against ~{old_shape:.0f} for four polling submits"
    )
