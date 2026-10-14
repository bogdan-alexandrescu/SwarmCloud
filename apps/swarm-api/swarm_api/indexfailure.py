"""Why a repository's index runs fail, and when the poll stops re-running them.

Operator QA p14 (2026-10-13, lane INDEX-FAIL): in tenant u-bogdan the poll
re-queued an index run of sagaxyz/ai-studio every 15 minutes, and every one
ended "repository clone failed ... remote: Repository not found. fatal:
Authentication failed", while the Repositories card said only "no index" or
"indexing now". Two things were missing, and both live here:

  * the failure, classified from the clone error the worker wrote into the
    task's `last_error` and stored on the registration as
    `index.last_failure`, so the card can say what went wrong, when, and
    what to do about it;
  * a back-off: an ACCESS failure (the forge answered "not found" or refused
    the credential) is deterministic -- the next run with the same
    credential fails the same way -- so after `BACKOFF_AFTER` consecutive
    access failures the poll stops queueing that repository
    (`index.backoff`) until the tenant's git credentials change or a person
    presses Index now. A TRANSIENT failure (the forge or the network did not
    answer, the worker was lost) and an unclassified one never back off:
    the next run may well succeed, and stopping would leave an index behind
    for a reason that has passed.

A success resets the streak and clears both. Everything stored is a fixed
sentence chosen here plus one scrubbed line of the error: URL user-info
(where a clone URL carries `x-access-token:<token>@`) is cut before the
API's redaction runs, so no token or URL credential reaches the document.
Pure: no Firestore, no clock.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

from .forge import neutral_line

#: Consecutive failures of the SAME access class after which the poll stops
#: re-running a repository. Two, not one: a single "not found" can be a
#: repository renamed between the head read and the clone, and one more run
#: costs a few minutes of indexer capacity; the twelfth costs an afternoon.
BACKOFF_AFTER = 2

ACCESS = "access"
TRANSIENT = "transient"
OTHER = "other"
CLASSES = (ACCESS, TRANSIENT, OTHER)

#: What the card says, per class. The API owns the sentence so a reworded
#: one changes in one place; the console adds the link to Git tokens.
CAUSES = {
    ACCESS: "SwarmCloud's GitHub access can't read this repository",
    TRANSIENT: "the repository could not be reached; the next run retries",
    OTHER: "the index run failed",
}

#: The end causes that are never the repository's fault: the run was lost,
#: timed out or never dispatched, so the next one is likely to succeed.
TRANSIENT_END_CAUSES = frozenset({"lost_worker", "timeout", "dispatch_failed"})

# git's and GitHub's words for "this credential cannot read this repository".
_ACCESS = re.compile(
    r"repository not found|authentication failed|could not read username"
    r"|terminal prompts disabled|invalid username or password"
    r"|permission to \S+ denied|requested url returned error: (?:401|403|404)"
    r"|write access to repository not granted|access denied",
    re.IGNORECASE,
)
# ... and for "nobody answered": the network, DNS, a 5xx, a rate limit.
_TRANSIENT = re.compile(
    r"could not resolve host|timed out|timeout|connection (?:reset|refused)"
    r"|failed to connect|recv failure|early eof|remote end hung up"
    r"|requested url returned error: (?:429|5\d\d)|temporarily unavailable"
    r"|rate limit|did not answer|unreachable",
    re.IGNORECASE,
)
# `scheme://user:secret@host`: the user-info is cut, the host kept.
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@")

DETAIL_LIMIT = 240


def scrub(text: str, limit: int = DETAIL_LIMIT) -> str:
    """One stored line of an error: URL user-info cut, then the API's redaction."""
    return neutral_line(_USERINFO.sub(r"\1", text or ""), limit)


def classify(last_error: str | None, end_cause: str | None) -> str:
    """`access`, `transient` or `other`, from what the worker reported.

    Access first: git prints "Authentication failed" beside "the remote end
    hung up", and the credential is the cause.
    """
    text = last_error or ""
    if _ACCESS.search(text):
        return ACCESS
    if _TRANSIENT.search(text) or (end_cause or "") in TRANSIENT_END_CAUSES:
        return TRANSIENT
    return OTHER


def _detail_line(last_error: str | None) -> str | None:
    """The line of the error that says most: from its first `remote:` or
    `fatal:` when there is one (the worker's prefix says only that the clone
    failed), otherwise its start."""
    if not last_error:
        return None
    at = min((i for i in (last_error.find("remote:"), last_error.find("fatal:")) if i >= 0),
             default=0)
    return scrub(last_error[at:]) or None


def failure_record(*, task_id: str, commit_sha: str | None, at: Any,
                   last_error: str | None, end_cause: str | None) -> dict[str, Any]:
    """`index.last_failure`: what failed, when, its class, the sentence and one
    scrubbed line of the error."""
    kind = classify(last_error, end_cause)
    return {
        "task_id": task_id,
        "commit_sha": commit_sha,
        "at": at,
        "class": kind,
        "cause": CAUSES[kind],
        "detail": _detail_line(last_error),
        "end_cause": end_cause,
    }


def after_failure(index: Mapping[str, Any], failure: Mapping[str, Any], *,
                  credential: str | None, at: Any) -> dict[str, Any]:
    """The registration's index state once `failure` is recorded.

    The streak counts consecutive failures of one class; a different class
    starts it again. The same task recorded twice counts once. At
    `BACKOFF_AFTER` access failures `index.backoff` is set, with the
    credential mark it was set under, so a changed credential is seen.
    """
    out = dict(index)
    previous = index.get("last_failure") or {}
    if previous.get("task_id") == failure["task_id"]:
        return out
    streak = index.get("failure_streak") or {}
    count = (int(streak.get("count") or 0) + 1) if streak.get("class") == failure["class"] else 1
    out["last_failure"] = dict(failure)
    out["failure_streak"] = {"class": failure["class"], "count": count}
    if failure["class"] == ACCESS and count >= BACKOFF_AFTER:
        out["backoff"] = {
            "since": (index.get("backoff") or {}).get("since") or at,
            "class": failure["class"],
            "reason": (f"polling stopped after {count} runs failed: {failure['cause']}; "
                       "it resumes when the git credentials change or on Index now"),
            "failures": count,
            "task_id": failure["task_id"],
            "credential": credential,
        }
    return out


def after_success(index: Mapping[str, Any]) -> dict[str, Any]:
    """A run that built an index: the streak, the failure and any back-off end."""
    out = dict(index)
    for key in ("last_failure", "failure_streak", "backoff"):
        out[key] = None
    return out


def resumed(index: Mapping[str, Any]) -> dict[str, Any]:
    """The back-off lifted (Index now, or a credential change). The streak is
    kept: one more access failure stops the poll again at once, so a resume
    costs one run, not `BACKOFF_AFTER`."""
    out = dict(index)
    out["backoff"] = None
    return out


def credential_mark(records: Iterable[Any], *, slot: str | None = None) -> str:
    """A digest of the tenant's git token records as far as a grant or a
    rotation moves them: which slots exist, their state, the secret version
    and when each was registered, rotated or revoked. Never a value; the
    probe's own timestamps are left out, so a daily re-verification that
    changes nothing is not a change.

    `slot` is the slot an index run would clone with (`forge.service_slot`,
    INDEX-CRED), so a change of WHICH credential the poll uses lifts the
    back-off even when no record moved: in u-bogdan the owner's connection
    already existed, and the runs failed only because they cloned with a
    `-git` slot the workspace never had. The tenant's `-git` slot adds
    nothing to the digest: every mark recorded before INDEX-CRED was taken
    while every index run used it, so a tenant still on it keeps its mark,
    and its back-off, across the release."""
    parts = sorted(
        "|".join(str(x) for x in (
            getattr(r, "token_id", None),
            getattr(getattr(r, "state", None), "value", getattr(r, "state", None)),
            getattr(r, "secret_version", None),
            getattr(r, "registered_at", None),
            getattr(r, "rotated_at", None),
            getattr(r, "revoked_at", None),
            ",".join(sorted(getattr(r, "repo_ids", None) or [])),
        ))
        for r in records
    )
    if slot and slot != "git":
        parts.append(f"service-slot|{slot}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def credential_changed(backoff: Mapping[str, Any], mark: str | None) -> bool:
    """Whether the credentials moved since the back-off was set. Unknown on
    either side is no change: an unreadable token list never resumes a loop."""
    then = backoff.get("credential")
    return bool(then) and bool(mark) and then != mark


__all__ = [
    "ACCESS", "BACKOFF_AFTER", "CAUSES", "CLASSES", "OTHER", "TRANSIENT",
    "after_failure", "after_success", "classify", "credential_changed", "credential_mark",
    "failure_record", "resumed", "scrub",
]
