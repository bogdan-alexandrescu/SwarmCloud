"""`scripts/build-images.sh` reads each image's digest back more than once when
the registry has not given an answer yet, and only then gives up.

THE FAILURE THIS PINS. Two dev releases failed on 2026-11-23 in the digest
read-back, each after about forty minutes of building, and a re-run of the same
job passed. The loop made ONE `gcloud artifacts docker tags list` per image and
treated any miss as fatal for the whole manifest:

  (a) the read failed: `ERROR: (gcloud.artifacts.docker.tags.list) INTERNAL:
      Internal error encountered.`;
  (b) the read succeeded but listed no entry for the tag, seconds after the
      same run had added that tag to a reused image.

Both are the registry not answering YET, on a read. What must still end the
read at once: an authentication failure (die_if_auth_failure ends the run), a
permission denial, NOT_FOUND, and an answer naming MORE than one digest for
the tag -- a real ambiguity.

Asserted against the REAL script with the fake `gcloud` of
test_build_images_concurrency.py: FAKE_TAGS_TARGET's `tags list` calls answer
in the order FAKE_TAGS_PLAN lists. A fake `sleep` records every wait and
spends no real time on a backoff, so the default schedule is what is tested.

WHAT THIS CANNOT PROVE: how long the live registry takes to become
consistent. Only release runs show whether the default schedule covers it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from .test_build_images_concurrency import TAG, _failure_summary, _run

TARGET = "swarm-api"
# The script's defaults: BUILD_DIGEST_READ_ATTEMPTS and BUILD_DIGEST_READ_BACKOFF.
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF = 2.0
DEFAULT_BACKOFF_MAX = 8.0
REPO = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
UNREADABLE = "no manifest written: 1 of 1 image(s) built or reused but unreadable (swarm-api)"


def _read(tmp_path, plan: str, **env: str):
    return _run(
        tmp_path, [TARGET], parallel=1, seconds=0.2, fake_sleep=True,
        FAKE_TAGS_TARGET=TARGET, FAKE_TAGS_PLAN=plan, **env,
    )


def _reads(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "tags" and e["target"] == TARGET]


def _backoffs(events: list[dict]) -> list[float]:
    """The waits the digest read made: every sleep longer than the scheduler's
    0.1 s poll."""
    return [e["seconds"] for e in events if e["event"] == "sleep" and e["seconds"] > 0.1]


def _own_digest() -> str:
    return "sha256:" + hashlib.sha256(f"{REPO}/{TARGET}@{TAG}".encode()).hexdigest()


def _manifest_digest(root) -> str:
    manifest = json.loads((root / "build" / "images-dev.json").read_text())
    (entry,) = [e for e in manifest["images"] if e["name"] == TARGET]
    return entry["digest"]


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        ("internal", "INTERNAL"),
        ("unavailable", "UNAVAILABLE"),
        ("http503", "503"),
        ("empty", "no entry for the tag"),
    ],
)
def test_a_read_without_an_answer_is_read_again_and_the_manifest_is_written(tmp_path, first, reason):
    root, proc, events = _read(tmp_path, first)
    # MUTATION: drop the retry and the first miss is fatal: exit 1, no manifest.
    assert proc.returncode == 0, proc.stderr[-3000:]
    reads = _reads(events)
    assert [r["outcome"] for r in reads] == [first, "ok"], reads
    assert _manifest_digest(root) == _own_digest()
    # MUTATION: drop the warning and nothing in the job log says why the
    # read-back took seconds longer.
    retries = [line for line in proc.stderr.splitlines() if "attempt 1/" in line]
    assert len(retries) == 1, proc.stderr[-2000:]
    assert TARGET in retries[0] and reason in retries[0], retries[0]
    assert "could not be read back" not in proc.stderr, proc.stderr[-2000:]


@pytest.mark.parametrize(
    ("kind", "said"),
    [
        ("auth", "the gcloud session is not usable"),
        ("denied", "PERMISSION_DENIED"),
        ("notfound", "NOT_FOUND"),
    ],
)
def test_a_refusal_is_not_read_again(tmp_path, kind, said):
    root, proc, events = _read(tmp_path, f"{kind}*")
    assert proc.returncode != 0, "the read was refused and the script exited 0"
    # MUTATION: retry every failed read and this sees DEFAULT_ATTEMPTS reads.
    assert len(_reads(events)) == 1, _reads(events)
    assert not _backoffs(events), _backoffs(events)
    assert said in proc.stderr, proc.stderr[-2000:]
    assert not (root / "build" / "images-dev.json").exists()
    if kind == "auth":
        # die_if_auth_failure ends the run before the image is called unreadable.
        assert "no manifest written" not in proc.stderr, proc.stderr[-2000:]
    else:
        assert UNREADABLE in _failure_summary(proc), _failure_summary(proc)


def test_two_digests_for_one_tag_is_not_read_again_and_fails(tmp_path):
    root, proc, events = _read(tmp_path, "two*")
    assert proc.returncode != 0, "the tag names two digests and the script exited 0"
    # MUTATION: treat "not exactly one digest" as "no answer yet" and this
    # reads DEFAULT_ATTEMPTS times before failing.
    assert len(_reads(events)) == 1, _reads(events)
    assert not _backoffs(events), _backoffs(events)
    assert f"{TARGET}: built, but its digest could not be read back" in proc.stderr
    assert UNREADABLE in _failure_summary(proc), _failure_summary(proc)
    assert not (root / "build" / "images-dev.json").exists()


@pytest.mark.parametrize("never", ["internal*", "empty*"])
def test_a_read_that_never_answers_fails_after_the_stated_attempts(tmp_path, never):
    root, proc, events = _read(tmp_path, never)
    assert proc.returncode != 0
    # MUTATION: make the read retry unbounded and this never ends; read once
    # and this sees one read.
    assert len(_reads(events)) == DEFAULT_ATTEMPTS, _reads(events)
    assert f"{TARGET}: built, but its digest could not be read back" in proc.stderr
    # Today's message, exactly: never a manifest that omits the image.
    assert UNREADABLE in _failure_summary(proc), _failure_summary(proc)
    assert not (root / "build" / "images-dev.json").exists()
    for attempt in range(1, DEFAULT_ATTEMPTS):
        assert f"attempt {attempt}/{DEFAULT_ATTEMPTS}" in proc.stderr, proc.stderr[-2000:]


def test_the_number_of_attempts_is_the_one_configured(tmp_path):
    _, proc, events = _read(tmp_path, "internal*", BUILD_DIGEST_READ_ATTEMPTS="2")
    assert proc.returncode != 0
    assert len(_reads(events)) == 2, _reads(events)
    assert UNREADABLE in _failure_summary(proc)


def test_the_wait_between_reads_is_backoff_with_jitter_and_costs_the_test_nothing(tmp_path):
    _, proc, events = _read(tmp_path, "internal*")
    assert proc.returncode != 0
    waits = _backoffs(events)
    # One wait between each two reads, none after the last.
    assert len(waits) == DEFAULT_ATTEMPTS - 1, waits
    for attempt, wait in enumerate(waits, start=1):
        full = min(DEFAULT_BACKOFF * 2 ** (attempt - 1), DEFAULT_BACKOFF_MAX)
        # MUTATION: drop the jitter (or the doubling) and a wait leaves its band.
        assert 0.5 * full <= wait <= full, f"wait {attempt} was {wait}s, outside [{0.5 * full}, {full}]"
    # The fake sleep spent no real time on them: consecutive reads are closer
    # together than the wait the script asked for between them.
    times = [r["t"] for r in _reads(events)]
    for (a, b), wait in zip(zip(times, times[1:]), waits):
        assert b - a < wait, f"reads {b - a:.2f}s apart: the {wait}s backoff was really slept"
