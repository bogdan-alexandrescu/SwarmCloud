"""The API's restatement of the agent-stream names, held to the worker's.

swarm-api cannot import `agent_worker` (its image installs swarm-api and
swarm-common only), so `swarm_api.agent_streams.AGENT_STREAM_FILES` restates
which artifact is each runner's agent stdout, stderr and transcript. It is
what the log routes fall back on for an attempt made before the worker
published agent streams (the reference task, task_73b5f4d9ca3641fbb914), and
what gives a pre-#184 listing its roles. Every rule restated twice in this
repository has since drifted, so the two are compared here for EVERY profile
in the frozen catalogue -- a profile added without a row fails.

Also pinned: the one name table's image half, and the new 410.
"""

from __future__ import annotations

from swarm_common.profiles import RUNNER_PROFILES


def test_every_catalogue_profile_has_the_same_agent_streams_in_the_api_and_the_worker():
    from agent_worker.runners.streams import agent_stream_files
    from swarm_api.agent_streams import AGENT_STREAM_FILES

    assert set(AGENT_STREAM_FILES) == set(RUNNER_PROFILES), (
        "every catalogue profile needs a row -- None for one with no agent CLI"
    )
    for name in RUNNER_PROFILES:
        worker = agent_stream_files(name)
        expected = None if worker is None else (worker.stdout, worker.stderr, worker.transcript)
        assert AGENT_STREAM_FILES[name] == expected, name


def test_the_api_names_the_streams_the_worker_publishes():
    """The worker's live and final objects are `logs/live/<label>.tail.log` and
    `logs/<label>.log`, one label per stream (`Worker._stream_files`); the API
    builds the same keys from its stream names. Compared by calling the
    worker's own method, so a stream renamed on one side fails here."""
    from pathlib import Path
    from types import SimpleNamespace

    from agent_worker.lifecycle import Worker
    from swarm_api.inspect import LOG_STREAMS

    worker = SimpleNamespace(cfg=SimpleNamespace(runner_profile="claude-code"))
    ws = SimpleNamespace(
        stdout_path=Path("/w/logs/stdout.log"),
        stderr_path=Path("/w/logs/stderr.log"),
        artifacts=Path("/w/artifacts"),
    )
    labels = tuple(label for label, _path in Worker._stream_files(worker, ws))
    assert labels == LOG_STREAMS == ("stdout", "stderr", "agent_stdout", "agent_stderr")

    worker.cfg.runner_profile = "mock"
    assert tuple(label for label, _ in Worker._stream_files(worker, ws)) == ("stdout", "stderr")


def test_the_image_half_of_the_name_table():
    from swarm_api.checkpoint_content import IMAGE_EXTENSIONS, artifact_kind

    assert set(IMAGE_EXTENSIONS) == {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    assert ".svg" not in IMAGE_EXTENSIONS, "SVG can carry script; it is text, never an image"
    assert artifact_kind("diagram.SVG") == ("text", "text/plain")
    assert artifact_kind("shot.PNG") == ("image", "image/png")
    assert artifact_kind("photo.jpeg") == ("image", "image/jpeg")
    assert artifact_kind("run.log") == ("log", "text/plain")
    assert artifact_kind("events.ndjson") == ("ndjson", "application/x-ndjson")
    assert artifact_kind("notes.md") == ("markdown", "text/markdown")
    assert artifact_kind("data.json") == ("json", "application/json")
    assert artifact_kind("archive.tar.gz") == ("binary", None)
    assert artifact_kind("LICENSE") == ("text", "text/plain")


def test_checkpoint_members_are_still_text_or_refused():
    """The image half is for ARTIFACTS. A checkpoint member stays on the text
    allowlist, so an image in a checkpoint is refused exactly as before."""
    from swarm_api.checkpoint_content import content_type_for

    assert content_type_for("photo.jpg") is None
    assert content_type_for("diagram.svg") == "text/plain"


def test_the_api_recognises_the_line_the_workers_capture_writes_where_it_cut():
    """`/transcript` and `/answer` tell a capped capture from a whole one by
    the notice the worker's `StreamCapture` writes where it dropped bytes.
    Restated in the API for the reason the stream table is, and held here."""
    from agent_worker.procman import TRUNCATION_MARK, TRUNCATION_NOTICE
    from swarm_api.agent_streams import TRUNCATION_MARK as API_MARK

    assert API_MARK == TRUNCATION_MARK
    assert TRUNCATION_NOTICE.lstrip(b"\n").startswith(TRUNCATION_MARK)


def test_a_reclaimed_artifact_is_a_410_of_its_own():
    from swarm_api.errors import Gone, NotFound

    gone = Gone("reclaimed", detail={"name": "x"})
    assert gone.status_code == 410
    assert gone.code == "artifact_gone"
    assert not isinstance(gone, NotFound), "listed-and-gone is not never-listed"
