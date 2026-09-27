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

import json
import re
from datetime import datetime, timezone

import pytest

from agent_worker import workspace as workspace_mod

#: The tail header since #184: the window's raw offset, the stream's raw size,
#: and the RFC 3339 UTC second the window was cut.
HEADER = re.compile(
    r"^#swarm-tail offset=(\d+) size=(\d+) at=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$"
)


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
    writes its progress to files rather than to stdout, so there is nothing to
    tail until the attempt is over, and the live objects never appear -- and
    it has no agent CLI, so it publishes no agent tails either.

    UPDATED DELIBERATELY FOR #184, as this docstring asked. claude-code now
    runs `--output-format stream-json --verbose` (`runners/claude_code.py`),
    which prints each event as it happens instead of one object at the end,
    and `procman.StreamCapture` now reads with `read1`, so what a child
    flushes reaches disk while it runs (`test_live_capture.py`). A claude-code
    attempt therefore HAS live output; see
    `test_the_agent_cli_streams_get_their_own_live_objects`. What stays true,
    and is asserted here, is the mock's silence.
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


# ---------------------------------------------------------------------------
# #184: the agent's own streams, the publish time, whole lines
# ---------------------------------------------------------------------------


def _running_as(worker_factory, profile, **overrides):
    worker, config, _ = worker_factory(runner_profile=profile, **overrides)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    return worker, config


def _live_names(store, config):
    return sorted(key.rsplit("/", 1)[-1] for key in store.list_keys(f"{config.log_prefix}/live/"))


def test_the_agent_cli_streams_get_their_own_live_objects(worker_factory, store):
    """The agent CLI's stdout and stderr are published beside the runner's.

    `stdout`/`stderr` are the RUNNER process's streams -- its JSON log lines.
    The agent's are the files the runner captures it into under `artifacts/`
    (`runners.streams.agent_stream_files`); for claude-code, the stream-json
    transcript. They get objects of their own, and neither leaks into the other.
    """
    worker, config = _running_as(worker_factory, "claude-code")
    (worker.ws.artifacts / "claude-code.stdout.log").write_text(
        '{"type":"system","subtype":"init"}\n'
    )
    (worker.ws.artifacts / "claude-code.stderr.log").write_text("agent warning\n")
    worker.ws.stderr_path.write_text('{"message":"child started"}\n')

    worker._publish_live_logs()

    assert _live_names(store, config) == [
        "agent_stderr.tail.log",
        "agent_stdout.tail.log",
        "stderr.tail.log",
    ]
    assert '"subtype":"init"' in _tail(store, config, "agent_stdout")
    assert "agent warning" in _tail(store, config, "agent_stderr")
    assert "child started" in _tail(store, config, "stderr")
    assert "child started" not in _tail(store, config, "agent_stdout")


def test_a_runner_with_no_agent_cli_publishes_no_agent_tail(running, store):
    """mock has no agent child. A file at a CLI runner's capture name is not its."""
    worker, config = running
    (worker.ws.artifacts / "claude-code.stdout.log").write_text("not this runner's\n")
    worker.ws.stdout_path.write_text("out\n")

    worker._publish_live_logs()

    assert _live_names(store, config) == ["stdout.tail.log"]


def test_an_agent_stream_that_is_a_symlink_is_not_followed(worker_factory, store, tmp_path):
    """The agent can write inside `artifacts/`. A link planted at a capture
    file's name must not publish what it points at."""
    worker, config = _running_as(worker_factory, "claude-code")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("a file outside the attempt\n")
    (worker.ws.artifacts / "claude-code.stdout.log").symlink_to(outside)

    worker._publish_live_logs()

    assert "agent_stdout.tail.log" not in _live_names(store, config)


def test_the_header_says_when_the_window_was_cut(running, store):
    """`at=` is what a reader shows as the live read's age. Old headers had no
    `at`, and the API still parses those."""
    worker, config = running
    worker.ws.stdout_path.write_text("hello\n")
    before = datetime.now(timezone.utc).replace(microsecond=0)

    worker._publish_live_logs()

    first = _tail(store, config).splitlines()[0]
    match = HEADER.match(first)
    assert match, f"the header does not carry offset, size and at: {first!r}"
    assert (match.group(1), match.group(2)) == ("0", "6")
    at = datetime.strptime(match.group(3), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= at <= datetime.now(timezone.utc)


def test_a_window_smaller_than_the_stream_starts_on_a_whole_line(worker_factory, store):
    """An NDJSON transcript cut mid-line starts with a fragment that parses as
    nothing. The window moves past the first newline, and the header's offset
    is the raw byte the served window really starts at."""
    worker, config = _running_as(worker_factory, "claude-code", live_log_tail_bytes=100)
    body = "".join(json.dumps({"type": "assistant", "n": i}) + "\n" for i in range(20)).encode()
    (worker.ws.artifacts / "claude-code.stdout.log").write_bytes(body)

    worker._publish_live_logs()

    header, _, served = _tail(store, config, "agent_stdout").partition("\n")
    match = HEADER.match(header)
    assert match, header
    offset = int(match.group(1))
    assert int(match.group(2)) == len(body)
    assert offset >= len(body) - 100, "the window is no larger than the configured tail"
    assert body[offset - 1 : offset] == b"\n", "the window starts right after a newline"
    assert served.encode() == body[offset:], "the offset names the first served byte"
    for line in served.splitlines():
        json.loads(line)
