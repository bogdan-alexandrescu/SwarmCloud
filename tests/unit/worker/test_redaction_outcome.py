"""`scrub_file` has to say WHY it did not rewrite a file.

It returned a bool, and False meant six different things: nothing to redact,
nothing registered, the file was not there, it was too large, it was not text,
or the rewrite itself failed. Only the first two are "fine". The lifecycle
discarded the value entirely (docs/audits/2026-09-18/02 §4), so an oversized or
binary artifact holding the provider key went to GCS with no signal at all.

Leaving such a file unscrubbed is a recorded trade -- rewriting bytes inside a
binary artifact corrupts it. What these tests pin is the part that was never
decided: that the reason is distinguishable, and that "not rewritten" can be
followed by "and here is whether the key is actually in it".

Imports are inside the tests on purpose, so that each one fails on its own
rather than the whole file failing to collect.
"""

from __future__ import annotations

from pathlib import Path

SECRET = "sk-ant-api03-this-is-the-tenant-key-0123456789"


def test_each_reason_for_not_rewriting_is_distinguishable(tmp_path: Path):
    from agent_worker.redact import ScrubOutcome, scrub_file_outcome

    text = tmp_path / "notes.txt"
    text.write_text(f"the key is {SECRET}\n")
    assert scrub_file_outcome(text, [SECRET]) is ScrubOutcome.REWRITTEN
    assert SECRET not in text.read_text()

    clean = tmp_path / "clean.txt"
    clean.write_text("nothing to see\n")
    assert scrub_file_outcome(clean, [SECRET]) is ScrubOutcome.CLEAN

    assert scrub_file_outcome(clean, []) is ScrubOutcome.NOTHING_REGISTERED
    assert scrub_file_outcome(tmp_path / "missing.txt", [SECRET]) is ScrubOutcome.ABSENT

    big = tmp_path / "big.log"
    big.write_text("x" * 100 + SECRET)
    assert scrub_file_outcome(big, [SECRET], max_bytes=50) is ScrubOutcome.TOO_LARGE
    assert SECRET in big.read_text(), "a skipped file must be left exactly as it was"

    binary = tmp_path / "shot.png"
    binary.write_bytes(b"\x89PNG\xff\xfe" + SECRET.encode() + b"\x00\xff")
    assert scrub_file_outcome(binary, [SECRET]) is ScrubOutcome.NOT_TEXT
    assert SECRET.encode() in binary.read_bytes()


def test_only_the_reasons_that_left_a_file_unexamined_count_as_skipped():
    from agent_worker.redact import ScrubOutcome

    skipped = {o for o in ScrubOutcome if o.skipped}
    assert skipped == {
        ScrubOutcome.TOO_LARGE,
        ScrubOutcome.NOT_TEXT,
        ScrubOutcome.UNREADABLE,
        ScrubOutcome.WRITE_FAILED,
    }


def test_the_old_boolean_still_means_rewritten(tmp_path: Path):
    """`runners/cliagent.py` calls `scrub_file` for its own two logs; its
    meaning must not move under it."""
    from agent_worker.redact import scrub_file

    path = tmp_path / "a.txt"
    path.write_text(SECRET)
    assert scrub_file(path, [SECRET]) is True
    assert scrub_file(path, [SECRET]) is False


def test_a_secret_is_found_in_raw_bytes_even_across_a_read_boundary(tmp_path: Path):
    """The scan reads in chunks; a key split across two of them is still a key."""
    from agent_worker.redact import file_contains_secret

    path = tmp_path / "blob.bin"
    prefix = b"\xff" * 1000
    path.write_bytes(prefix + SECRET.encode() + b"\x00" * 1000)

    # A chunk size that puts the boundary inside the secret.
    assert file_contains_secret(path, [SECRET], chunk_bytes=1010) is True
    assert file_contains_secret(path, ["sk-ant-some-other-key-000000"], chunk_bytes=1010) is False
    assert file_contains_secret(path, [], chunk_bytes=1010) is False


def test_the_logger_reports_the_outcome_with_its_own_registered_set(tmp_path: Path):
    from agent_worker.logs import StructuredLogger
    from agent_worker.redact import ScrubOutcome

    log = StructuredLogger(stream=open(tmp_path / "log.jsonl", "w"))
    log.register_secret(SECRET)

    binary = tmp_path / "trace.bin"
    binary.write_bytes(b"\xff\xfe" + SECRET.encode())
    assert log.scrub_file_outcome(binary) is ScrubOutcome.NOT_TEXT
    assert log.file_contains_secret(binary) is True
    assert log.file_contains_secret(tmp_path / "gone.bin") is None
