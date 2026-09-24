"""Literal-value redaction, shared by the worker and by the runner children.

The worker's logger scrubs registered secrets out of its own log lines, but a
log line is only one of the ways a provider key leaves the pod. The others are
the runner's captured stdout and stderr (uploaded to GCS), the transcript
artifact, and the result summary that becomes `task.result_summary` in
Firestore -- and a runner runs in a SEPARATE process that has no access to the
worker's logger, only to the key in its own environment.

So the mechanism lives here, as functions over an explicit set of values, and
both sides use it: `logs.StructuredLogger` for the log stream, and
`runners.cliagent` for the files and the summary it produces before the worker
ever sees them.

Short values are never registered. Redacting a 3-character string would corrupt
ordinary prose far more often than it would protect anything, and no real
provider key is that short.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Iterable

REDACTED = "***REDACTED***"

#: Below this length a value is not distinctive enough to redact safely.
MIN_SECRET_LENGTH = 8

#: Above this size a file is not rewritten. 64 MiB, because the rewrite reads
#: the whole file into memory as text and a worker's memory is sized for the
#: agent, not for a second copy of its largest artifact. Named rather than
#: inlined so the lifecycle, the logger and the tests all mean the same number.
MAX_SCRUB_BYTES = 64 * 1024 * 1024

#: The raw-byte scan reads this much at a time. Bounded for the same reason:
#: an artifact may be up to `max_artifact_bytes` (512 MiB), and the scan must
#: not hold it in memory to answer a yes/no question.
SCAN_CHUNK_BYTES = 1024 * 1024


def collect_secrets(values: Iterable[Any]) -> tuple[str, ...]:
    """Keep the values that are long enough to redact without collateral damage."""
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            out.append(value)
    # Longest first: a key that contains another registered value as a substring
    # must be replaced whole, or the shorter replacement leaves a fragment.
    return tuple(sorted(set(out), key=len, reverse=True))


def scrub_text(text: str, secrets: Iterable[str]) -> str:
    """Replace every registered value, longest first.

    The ordering is not cosmetic. A set has no order, and replacing a short
    secret that happens to be a prefix of a longer one first would leave the
    tail of the longer one in the output.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class ScrubOutcome(str, Enum):
    """What `scrub_file_outcome` did to a file, and -- when nothing -- why.

    A bool used to carry this, and False meant six different things. Only two
    of them are "fine": the file held nothing to redact, or nothing was
    registered to look for. The other four mean a file is about to leave the
    pod WITHOUT having been examined, which is the one fact the caller needs
    and could not get (docs/audits/2026-09-18/02-agent-worker-credentials.md
    section 4).
    """

    #: A registered value was found and replaced.
    REWRITTEN = "rewritten"
    #: Read in full as text; nothing registered was in it.
    CLEAN = "clean"
    #: No registered values, so there was nothing to look for.
    NOTHING_REGISTERED = "nothing_registered"
    #: Not a regular file (missing, a directory, a symlink). Nothing is
    #: uploaded from a path like this either.
    ABSENT = "absent"
    #: Larger than `max_bytes`; left exactly as it was.
    TOO_LARGE = "too_large"
    #: Not UTF-8; left exactly as it was, because a text rewrite would corrupt it.
    NOT_TEXT = "not_text"
    #: Could not be read.
    UNREADABLE = "unreadable"
    #: A registered value WAS found and the rewrite failed, so it is still there.
    WRITE_FAILED = "write_failed"

    @property
    def skipped(self) -> bool:
        """True when the file leaves this function unexamined or unredacted."""
        return self in _SKIPPED


_SKIPPED = frozenset(
    {
        ScrubOutcome.TOO_LARGE,
        ScrubOutcome.NOT_TEXT,
        ScrubOutcome.UNREADABLE,
        ScrubOutcome.WRITE_FAILED,
    }
)


def scrub_file_outcome(
    path: Path, secrets: Iterable[str], *, max_bytes: int = MAX_SCRUB_BYTES
) -> ScrubOutcome:
    """Rewrite a text file in place with every secret redacted, and say what happened.

    A binary file, a symlink or a file larger than `max_bytes` is left
    untouched: a partial rewrite would corrupt a tenant's artifact, and
    corrupting an artifact to protect a key that is probably not in it is the
    wrong trade. That trade is unchanged. What changed is that the caller can
    now tell "clean" from "never looked", and follow the second with
    `file_contains_secret` to find out whether "probably not in it" held.
    """
    secrets = tuple(s for s in secrets if s)
    target = Path(path)
    if not secrets:
        return ScrubOutcome.NOTHING_REGISTERED
    if not target.is_file() or target.is_symlink():
        return ScrubOutcome.ABSENT
    try:
        if target.stat().st_size > max_bytes:
            return ScrubOutcome.TOO_LARGE
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ScrubOutcome.NOT_TEXT
    except OSError:
        return ScrubOutcome.UNREADABLE
    scrubbed = scrub_text(original, secrets)
    if scrubbed == original:
        return ScrubOutcome.CLEAN
    try:
        target.write_text(scrubbed, encoding="utf-8")
    except OSError:
        return ScrubOutcome.WRITE_FAILED
    return ScrubOutcome.REWRITTEN


def scrub_file(path: Path, secrets: Iterable[str], *, max_bytes: int = MAX_SCRUB_BYTES) -> bool:
    """Rewrite a text file in place with every secret redacted.

    Returns True when the file was rewritten. Kept with exactly its old meaning
    for `runners/cliagent.py`, which scrubs its own two logs before the worker
    ever sees them; the worker's own pass over the same files uses
    `scrub_file_outcome`, because it is the one that has to report a skip.
    """
    return scrub_file_outcome(path, secrets, max_bytes=max_bytes) is ScrubOutcome.REWRITTEN


def file_contains_secret(
    path: Path, secrets: Iterable[str], *, chunk_bytes: int = SCAN_CHUNK_BYTES
) -> bool:
    """Whether any registered value appears, byte for byte, anywhere in the file.

    For the files `scrub_file_outcome` would not rewrite. It reads in bounded
    chunks and carries the last `len(longest) - 1` bytes across each boundary,
    so a value split between two reads is still found.

    LITERAL BYTES ONLY, and that is its whole reach: a key stored compressed,
    base64-encoded or as UTF-16 inside the file is invisible to it -- exactly as
    it is invisible to the text rewriter. A False here means "the registered
    value is not in these bytes as written", not "this file is safe".

    Raises OSError when the file cannot be read; the caller decides what an
    unexaminable file means.
    """
    needles = tuple({s.encode("utf-8") for s in secrets if s})
    if not needles:
        return False
    overlap = max(len(n) for n in needles) - 1
    carry = b""
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(max(1, chunk_bytes))
            if not chunk:
                return False
            window = carry + chunk
            if any(needle in window for needle in needles):
                return True
            carry = window[-overlap:] if overlap > 0 else b""
