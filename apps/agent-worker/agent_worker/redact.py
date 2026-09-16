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

from pathlib import Path
from typing import Any, Iterable

REDACTED = "***REDACTED***"

#: Below this length a value is not distinctive enough to redact safely.
MIN_SECRET_LENGTH = 8


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


def scrub_file(path: Path, secrets: Iterable[str], *, max_bytes: int = 64 * 1024 * 1024) -> bool:
    """Rewrite a text file in place with every secret redacted.

    Returns True when the file was rewritten. A binary file, a symlink or a file
    larger than `max_bytes` is left untouched: a partial rewrite would corrupt a
    tenant's artifact, and corrupting an artifact to protect a key that is
    probably not in it is the wrong trade.
    """
    secrets = tuple(secrets)
    target = Path(path)
    if not secrets or not target.is_file() or target.is_symlink():
        return False
    try:
        if target.stat().st_size > max_bytes:
            return False
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    scrubbed = scrub_text(original, secrets)
    if scrubbed == original:
        return False
    try:
        target.write_text(scrubbed, encoding="utf-8")
    except OSError:
        return False
    return True
