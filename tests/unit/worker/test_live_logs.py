"""The live log tail: the difference between watching a run and reading about it.

The complete streams are uploaded once, at exit. That is correct for the record
and useless for watching -- a twenty-minute task is a twenty-minute blind spot,
which is the whole reason dispatching work to this platform felt nothing like
running it locally.

Two properties are worth pinning, and they pull in opposite directions:

* the tail must be CHEAP, because it runs every few seconds for every running
  agent, and GCS has no append -- so it publishes a bounded window, not the
  file;
* the tail must be SCRUBBED, because `_redact_before_upload` runs once on the
  way out and anything published during the run has never been through it.
  "The object is overwritten five seconds later" is not a property anyone
  should rely on for a provider key.
"""

from __future__ import annotations

import pytest

from agent_worker import workspace as workspace_mod


@pytest.fixture()
def running(worker_factory):
    """A worker with a workspace, as it exists mid-run."""
    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    return worker, config


def _tail(store, config, stream="stdout"):
    return store.download_bytes(f"{config.log_prefix}/live/{stream}.tail.log").decode()


def test_nothing_is_published_before_the_runner_writes_anything(running, store):
    worker, config = running
    worker._publish_live_logs()
    assert store.list_keys(f"{config.log_prefix}/live/") == []


def test_a_growing_stream_is_published_while_the_agent_is_still_running(running, store):
    worker, config = running
    worker.ws.stdout_path.write_text("reading apps/swarm-ui/src/fetch.ts\n")
    worker._publish_live_logs()

    assert "reading apps/swarm-ui/src/fetch.ts" in _tail(store, config)


def test_both_streams_get_their_own_object(running, store):
    worker, config = running
    worker.ws.stdout_path.write_text("out\n")
    worker.ws.stderr_path.write_text("err\n")
    worker._publish_live_logs()

    assert "out" in _tail(store, config, "stdout")
    assert "err" in _tail(store, config, "stderr")


def test_only_the_last_window_is_published_so_the_cost_stays_flat(worker_factory, store):
    """GCS has no append. Publishing the whole stream would rewrite up to
    `max_stdout_bytes` every interval, making the cost of WATCHING a run grow
    with the length of the run -- exactly backwards for a tail."""
    worker, config, _ = worker_factory(live_log_tail_bytes=512)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker.ws.stdout_path.write_text("A" * 5000 + "THE-END\n")

    worker._publish_live_logs()
    body = _tail(store, config)

    assert "THE-END" in body, "the tail must contain the NEWEST output"
    assert len(body) < 1200, "the whole 5KB file was published, not a 512B window"


def test_the_window_records_where_it_starts_so_a_gap_is_visible(worker_factory, store):
    """A reader that polls too slowly loses the middle. Without the offset it
    silently stitches two non-adjacent pieces of output together and shows a
    transcript that never happened."""
    worker, config, _ = worker_factory(live_log_tail_bytes=64)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker.ws.stdout_path.write_text("B" * 500)

    worker._publish_live_logs()
    first = _tail(store, config).splitlines()[0]

    assert first.startswith("#swarm-tail ")
    assert "offset=" in first and "size=500" in first


def test_a_credential_echoed_into_stdout_is_scrubbed_before_it_reaches_the_bucket(
    running, store
):
    """The property that makes publishing DURING a run safe at all.

    `_redact_before_upload` runs once, on the way out. A tail published while
    the agent is running has never been through it, so it does its own pass.
    """
    worker, config = running
    worker.log.register_secret("ghp_a_very_real_looking_token")
    worker.ws.stdout_path.write_text("cloning with ghp_a_very_real_looking_token now\n")

    worker._publish_live_logs()
    body = _tail(store, config)

    assert "ghp_a_very_real_looking_token" not in body
    assert "REDACTED" in body


def test_publishing_is_skipped_entirely_when_it_is_switched_off(worker_factory, store):
    worker, config, _ = worker_factory(live_logs_enabled=False)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker.ws.stdout_path.write_text("hello\n")

    worker._publish_live_logs()

    assert store.list_keys(f"{config.log_prefix}/live/") == []


def test_a_failing_upload_never_fails_the_attempt(running, monkeypatch):
    """A failed flush costs the watcher five seconds of staleness. Failing the
    run over it would trade a working agent for a cosmetic feature."""
    worker, _ = running
    worker.ws.stdout_path.write_text("hello\n")

    def boom(*args, **kwargs):
        raise RuntimeError("bucket is on fire")

    monkeypatch.setattr(worker.store, "upload_bytes", boom)
    worker._publish_live_logs()  # must not raise


def test_invalid_utf8_in_the_stream_does_not_stop_the_tail(running, store):
    """A runner that writes a raw byte sequence -- a progress bar, a binary
    blob -- must not take the watcher down with it."""
    worker, config = running
    worker.ws.stdout_path.write_bytes(b"before \xff\xfe after\n")

    worker._publish_live_logs()

    assert "after" in _tail(store, config)


def test_the_supervision_loop_actually_calls_the_publisher(db, store, worker_factory, monkeypatch):
    """THE TEST THAT WAS MISSING, and its absence cost a deploy.

    Every other test in this file calls `_publish_live_logs()` directly. All of
    them passed while the feature produced nothing on a real cluster run,
    because none of them proved anything CALLS it. A publisher that is never
    invoked is indistinguishable from a broken one, and the failure path inside
    it logs at debug, so the deployment said nothing at all.

    This drives the production supervision loop and asserts the call happens
    while the child is still running.
    """
    from conftest import seed_attempt

    seed_attempt(
        db,
        task_input={
            "prompt": "outlive the live-log interval",
            "steps": 4,
            # `sleep_seconds` is the TOTAL, divided across steps -- not per
            # step. Getting that backwards is what made the first live
            # verification run for four seconds against a five-second timer.
            "sleep_seconds": 2.0,
        },
    )
    worker, _, _ = worker_factory(live_log_interval_seconds=1)
    calls: list[int] = []
    original = worker._publish_live_logs
    monkeypatch.setattr(
        worker, "_publish_live_logs", lambda: (calls.append(1), original())[1]
    )

    worker.run()

    assert calls, "the supervision loop never called the live-log publisher"


def test_a_runner_that_writes_nothing_until_it_exits_yields_no_tail(db, store, worker_factory):
    """AND THE LIMIT OF THE FEATURE, written down where it will be found.

    A tail can only show what the child has already flushed. The mock runner
    writes its progress to files rather than to stdout, and `claude --print
    --output-format json` emits ONE object when it finishes -- so for both of
    the runners this platform ships today there is nothing to tail until the
    attempt is over, and the live objects never appear.

    That is not a bug in the publisher; it is a property of what the child
    writes. The fix is `--output-format stream-json`, which also carries the
    `rate_limit_event` readings the account pool needs. Asserting the current
    behaviour here means that change cannot land silently: this test will fail
    and have to be updated deliberately.
    """
    from conftest import seed_attempt

    seed_attempt(
        db,
        task_input={"prompt": "silent runner", "steps": 4, "sleep_seconds": 2.0},
    )
    worker, config, _ = worker_factory(live_log_interval_seconds=1)
    worker.run()

    assert store.list_keys(f"{config.log_prefix}/live/") == [], (
        "a runner that now streams output would make this pass a tail -- good, "
        "but update this test and the comment above deliberately"
    )
