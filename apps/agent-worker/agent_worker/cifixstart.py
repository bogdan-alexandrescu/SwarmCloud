"""A CI fix whose red head was superseded while it waited ends before its agent runs (#2276).

WHY. `scripts/ci-fix.sh` checks that the red commit is still the pull
request's head when it SUBMITS the fix. The fix then waits for capacity, and
PR #2224's two fix tasks started 1.5-2 h later, when the owner had already
fixed and merged the branch (the clone started from b9a8d23; the red head
was 2508b6b). One agent found nothing to change and failed
`published_nothing`; the other reset onto the red sha and failed with its
HEAD moved off the task's branch. Neither had anything left to fix.

WHAT SWARM-API SIGNS. `metadata.dispatch.ci_fix_head` is
`{"pull_request": N, "head_sha": <40 hex>}`, copied from `metadata.ci_fix`
at submission on a continuation (`swarm_api.validation.CI_FIX_HEAD_FIELD`).
`metadata.ci_fix` itself is outside the step-spec signature, and the worker
acts on no unsigned metadata, so only this copy is read.

WHAT THE WORKER CHECKS. Once, at attempt start, after a FRESH clone of the
continued branch and before any runner starts: the commit the clone started
from against `head_sha`, and the pull request as the forge reports it.
`superseded` says why the fix has nothing left to do, and the attempt then
ends SUCCEEDED with `no_change` and that reason (`Worker._finish_superseded_ci_fix`).
An attempt restored from a checkpoint is not checked: its agent already ran
on the red head, and its work is what the push will judge.

Kept out of lifecycle.py on purpose; lifecycle calls two functions from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import WorkerError
from .forge import PullHead

#: The dispatch block's key, as `swarm_api.validation.CI_FIX_HEAD_FIELD`
#: writes it; the worker cannot import swarm_api, so a parity test
#: (tests/unit/worker/test_ci_fix_superseded_at_start.py) holds the two equal.
FIELD = "ci_fix_head"

#: Every reason `superseded` returns starts with this, so a run's record can
#: be grepped for it and the post-back can tell it from an agent's no-change.
SUPERSEDED = "superseded"

#: A full commit sha, as `swarm_api.cifix` holds `ci_fix.head_sha` to.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Record:
    """The red head a CI fix was submitted for, as swarm-api signed it."""

    pull_request: int
    head_sha: str


def record(metadata: Any) -> Record | None:
    """The signed `ci_fix_head` of a task's metadata, or None when absent.

    Raises WorkerError for a value swarm-api could not have written: a pull
    request that is not a positive integer, or a head that is not a full sha.
    """
    if not isinstance(metadata, dict):
        return None
    block = metadata.get("dispatch")
    if not isinstance(block, dict) or FIELD not in block:
        return None
    raw = block.get(FIELD)
    number = raw.get("pull_request") if isinstance(raw, dict) else None
    head = raw.get("head_sha") if isinstance(raw, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise WorkerError(
            f"metadata.dispatch.{FIELD} names no pull request number; refusing to "
            "start a CI fix against a pull request it does not name"
        )
    if not isinstance(head, str) or not _SHA_RE.match(head):
        raise WorkerError(
            f"metadata.dispatch.{FIELD} names no full head sha; refusing to start a "
            "CI fix whose red commit is unknown"
        )
    return Record(pull_request=number, head_sha=head)


def superseded(rec: Record, clone_commit: str | None, pull: PullHead | None) -> str | None:
    """Why this CI fix has nothing left to fix, starting 'superseded: '; None when it does.

    `clone_commit` is the commit the fresh clone of the continued branch
    started from; `pull` is the pull request read now, or None when the
    forge could not be read, and then the clone alone decides.
    """
    name = f"pull request #{rec.pull_request}"
    red = rec.head_sha[:12]
    if isinstance(clone_commit, str) and _SHA_RE.match(clone_commit) \
            and clone_commit != rec.head_sha:
        return (
            f"{SUPERSEDED}: the branch moved past the red commit {red} to "
            f"{clone_commit[:12]} while this fix waited to start; nothing is left to fix "
            "on the commit CI failed"
        )
    if pull is None:
        return None
    if pull.merged:
        return f"{SUPERSEDED}: {name} was merged while this fix waited to start"
    if pull.state != "open":
        return f"{SUPERSEDED}: {name} was closed while this fix waited to start"
    if _SHA_RE.match(pull.head_sha or "") and pull.head_sha != rec.head_sha:
        return (
            f"{SUPERSEDED}: {name}'s head moved past the red commit {red} to "
            f"{pull.head_sha[:12]} while this fix waited to start"
        )
    return None
