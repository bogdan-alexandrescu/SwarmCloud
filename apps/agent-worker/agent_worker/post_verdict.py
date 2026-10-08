"""The `post-verdict` worker action, and the verdict it carries (#295).

WHAT IT IS. A step that runs no agent (`WorkerAction.POST_VERDICT`, contract
request 35). It reads the review step's `review.json` from a path it derives
itself, and submits it as a GitHub pull-request review with the review App's
key, which only its own service account can read (docs/merge-step.md §4.3).
The merge step later accepts ONLY a review that App posted (§5.2a), so this is
the one place a verdict becomes something a later agent cannot forge.

WHERE THE VERDICT LIVES, AND WHO WRITES IT.

    tenants/<tenant>/verdicts/<workflow id>/<review task id>/review.json

`verdict_key` is the one derivation; the review's worker writes there
(`publish_verdict`, from `lifecycle.Worker._publish_review_verdict`), and
`post-verdict` and `merge` read there. Every segment is a fact, never a
pointer a tenant identity can write: the tenant is this execution's, the
workflow id is the signed spec's, and the review task id is the signed
`dispatch.verdict_source.review` (post-verdict) or `dispatch.merges.review`
(merge). The ordinary `input_from` staging is NOT used for this read: it
resolves an object from the upstream's own `result_summary`, which any agent
of the tenant can rewrite (§4.3, BLOCKER).

WRITE-ONCE (CR 36, the M2 bucket split). Only the review's own service
account can create under `verdicts/`, and with `objectCreator` alone, so it
cannot overwrite either. `publish_verdict` asks GCS for exactly that --
`ifGenerationMatch=0` -- and REFUSES when an object is already there with
other bytes: a verdict already at this path was written by someone, and
replacing it is the one thing the prefix exists to prevent. The same bytes
again (an attempt that died between its write and its finish) are already
published, not refused.

THE ACTION, IN THE ORDER §2.2 GIVES THE MERGE (§6a for the end causes):

  1. the worker is non-dumpable, else VERDICT_REFUSED / worker_unprotected;
  2. nothing the image started is alive, else processes_alive;
  3. the review's and the author's signed specs verify, and are this
     workflow's, else spec_unverified (`upstream:<id>:<why>`);
  4. the forge record on the Job is the pinned one, else CANNOT_START /
     forge_host_invalid;
  5. review.json is at the derived path and is the schema, else
     INPUTS_UNAVAILABLE / verdict_unreadable;
  6. the secret is read, the token minted for this repository with
     pull_requests: write alone, and the key dropped;
  7. fencing and cancel are re-checked, and the pull request is the
     author's own branch, unforked and open;
  8. the review is posted at `review.json.sha`: APPROVE when the verdict is
     MERGE, REQUEST_CHANGES otherwise;
  9. the token is revoked, whatever happened.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from swarm_common.models import EndCause
from swarm_common.states import TaskState

from . import forge as forge_mod
from .errors import ExitCode, InputUnavailable
from .objectstore import GcsObjectStore, LocalObjectStore, ObjectStore, ObjectStoreError
from .specverify import UpstreamSpecUnverified
from .verdict import REVIEW_VERDICTS

#: The file the review agent writes and this action reads.
VERDICT_FILENAME = "review.json"
#: The profile whose service account alone may create under `verdicts/`
#: (terraform/modules/tenancy `review_verdicts`). Its worker is the only one
#: that publishes a verdict.
REVIEW_PROFILE = "claude-code-review"
#: The most of review.json read. A verdict and a summary are a few KiB.
MAX_REVIEW_BYTES = 256 * 1024
#: GitHub's own bound on a review body is 65536 characters.
MAX_REVIEW_BODY_CHARS = 60000

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")


# ---------------------------------------------------------------------------
# What a worker action hands back to the lifecycle
# ---------------------------------------------------------------------------


@dataclass
class ActionOutcome:
    """How a worker action ended, for `lifecycle.Worker._end_worker_action`.

    `retryable` ends the ATTEMPT (`control.fail_retryably`), so `end_cause`
    is the task's only once its attempts are spent; otherwise `state` and
    `end_cause` are the task's now. `summary` is `result_summary[<key>]`.
    `credential_missing` parks the task CREDENTIAL_MISSING, at no cost, like
    any profile whose provider the tenant has not registered. `ci_wait` parks
    it CI_PENDING (`control.ControlPlane.park_ci_pending`): the merge's facts
    may change by themselves, so it waits holding nothing until swarm-api's
    wake tick or the fallback instant (docs/merge-step.md, 2026-10-06, §1).
    """

    state: TaskState
    end_cause: EndCause | None
    exit_code: int
    summary: dict[str, Any]
    message: str = ""
    retryable: bool = False
    retry_delay_seconds: int = 0
    spec_check: dict[str, Any] | None = None
    credential_missing: Any = None
    #: `{"code", "head", "pull_request", "pending"}`: what the park waits on.
    ci_wait: dict[str, Any] | None = None


@dataclass
class ActionContext:
    """Everything a worker action may touch, handed in by the lifecycle.

    Nothing here is read from a task document except `dispatch` and
    `workflow_id`, both of the execution's own VERIFIED spec.
    """

    tenant_id: str
    task_id: str
    attempt_id: str
    workflow_id: str | None
    #: The signed `metadata.dispatch` block.
    dispatch: Mapping[str, Any]
    store: ObjectStore
    #: The tenant-gated upstream read (`inputs.fetch_upstream_task`).
    fetch_upstream: Callable[[str], dict[str, Any]]
    #: `specverify.verify_upstream_spec` bound to this worker's keys.
    verify_upstream: Callable[[str, Mapping[str, Any]], Any]
    #: Reads this action's App secret; raises `secrets.CredentialMissing`
    #: when the tenant has not registered it.
    read_app_key: Callable[[], forge_mod.AppKey]
    environ: Mapping[str, str]
    #: Re-checks the fencing generation (raising `FencedError`) and returns
    #: True when a cancel has been requested.
    recheck: Callable[[], bool]
    #: The pre-credential reap; returns the pids that survived.
    reap: Callable[[], tuple[int, ...]]
    #: Why this process must not hold a credential, or None.
    unprotected: str | None
    scrub: Callable[[str], str]
    log: Any
    branch_prefix: str = "swarm/"
    #: `{filename: path}` of what `input_from` staged (merge: proof.json).
    staged: Mapping[str, Path] = field(default_factory=dict)
    transport: forge_mod.Transport | None = None
    sleep: Callable[[float], None] = lambda _s: None
    #: B13r's `merge_human_gate`: True makes the merge stop, SUCCEEDED, before
    #: the merge call, awaiting a person (`merge.run_merge`).
    human_gate: bool = False
    #: The longest wait an action may ask a retry to be delayed by.
    max_in_worker_retry_delay_seconds: int = 45
    #: Tries of one forge READ that failed transiently (`forge_retry`); the
    #: worker's `forge_read_attempts`.
    forge_read_attempts: int = forge_mod.DEFAULT_READ_ATTEMPTS
    #: Seconds left before the step's deadline, read when a retry starts.
    remaining_seconds: Callable[[], float] = lambda: float("inf")
    #: Registers a minted token with the logger's redaction (never logged
    #: anyway: belt and braces for any message that might quote a header).
    register_secret: Callable[[str], None] = lambda _value: None
    #: The merge's credential (contract request 47): reads the tenant's own
    #: `-git` token at merge time, registered with the log redaction before it
    #: is returned; raises `secrets.CredentialMissing` when the tenant has not
    #: registered one. None for an action that holds no forge token.
    read_git_token: Callable[[], str] | None = None
    #: The task's own `repository_url`, from its VERIFIED spec: the repository
    #: the merge acts on (owner and repo are parsed from it, never a pointer).
    repository_url: str | None = None


def _outcome(
    state: TaskState,
    cause: EndCause | None,
    summary: dict[str, Any],
    message: str,
    *,
    exit_code: int | None = None,
    **kwargs: Any,
) -> ActionOutcome:
    if exit_code is None:
        exit_code = ExitCode.OK if state is TaskState.SUCCEEDED else ExitCode.FAILED
    return ActionOutcome(
        state=state, end_cause=cause, exit_code=exit_code, summary=summary, message=message,
        **kwargs,
    )


def refusal(summary: dict[str, Any], cause: EndCause, code: str, message: str,
            **kwargs: Any) -> ActionOutcome:
    """A condition was not met; nothing changed on the forge. Never retried."""
    summary["refusal"] = {"code": code, "message": message}
    return _outcome(TaskState.FAILED, cause, summary, f"{code}: {message}", **kwargs)


def cannot_start(summary: dict[str, Any], code: str, message: str) -> ActionOutcome:
    summary["refusal"] = {"code": code, "message": message}
    return _outcome(TaskState.FAILED, EndCause.CANNOT_START, summary, f"{code}: {message}",
                    exit_code=ExitCode.CONFIG)


def unavailable(summary: dict[str, Any], cause: EndCause, message: str,
                retry_after: int | None) -> ActionOutcome:
    """An outage: the attempt fails retryably and the task takes `cause` once
    its attempts are spent (§6 rows 37-38). Never a sleep in here (invariant 4):
    a long `retry-after` becomes the retry's delay."""
    summary["refusal"] = {"code": "forge_unavailable", "message": message}
    return _outcome(
        TaskState.FAILED, cause, summary, f"forge_unavailable: {message}",
        retryable=True, retry_delay_seconds=max(0, int(retry_after or 0)),
    )


def forge_retry(ctx: ActionContext) -> forge_mod.RetryPolicy:
    """How a worker action retries a forge READ that failed transiently (F1).

    GETs only (`PinnedForgeClient` applies it to nothing else): the merge and
    the review post are not resent blindly. Bounded by the platform's
    in-worker wait and the step's deadline; past that the read's
    `ForgeUnavailable` reaches `unavailable`, which fails the attempt
    retryably with the forge's `retry-after` as its delay.
    """
    return forge_mod.RetryPolicy.bounded(
        attempts=ctx.forge_read_attempts,
        max_in_worker_retry_delay_seconds=ctx.max_in_worker_retry_delay_seconds,
        remaining_seconds=ctx.remaining_seconds(),
        sleep=ctx.sleep,
        log=ctx.log,
    )


def cancelled(summary: dict[str, Any]) -> ActionOutcome:
    return _outcome(TaskState.CANCELLED, EndCause.CANCEL_REQUESTED, summary,
                    "cancelled before the forge was asked to act", exit_code=ExitCode.CANCELLED)


# ---------------------------------------------------------------------------
# The verdict's path, its schema, and its one write
# ---------------------------------------------------------------------------


class VerdictUnreadable(ValueError):
    """review.json is not the schema in docs/merge-step.md §4.1."""


class VerdictAlreadyPublished(RuntimeError):
    """An object with other bytes already sits at the verdict's path (CR 36)."""


def verdict_key(tenant_id: str, workflow_id: str, review_task_id: str) -> str:
    """`tenants/<tenant>/verdicts/<workflow id>/<review task id>/review.json`.

    Each segment must be one key segment, so no value can climb out of the
    tenant's `verdicts/` prefix or name another workflow's.
    """
    for name, value in (("tenant", tenant_id), ("workflow", workflow_id),
                        ("review task", review_task_id)):
        if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
            raise VerdictUnreadable(f"the {name} id is not one key segment")
    return f"tenants/{tenant_id}/verdicts/{workflow_id}/{review_task_id}/{VERDICT_FILENAME}"


def verdict_source(dispatch: Mapping[str, Any]) -> str:
    """The review TASK id in the signed `dispatch.verdict_source.review`."""
    source = dispatch.get("verdict_source")
    review = source.get("review") if isinstance(source, Mapping) else None
    if not isinstance(review, str) or not _SEGMENT.fullmatch(review):
        raise VerdictUnreadable(
            "this step's signed dispatch block names no verdict_source.review task id"
        )
    return review


@dataclass(frozen=True)
class Review:
    verdict: str
    sha: str
    title: str
    summary: str


def parse_review(raw: bytes) -> Review:
    """review.json, checked: `{"verdict", "sha", "title", "summary"}` (§4.1).

    `verdict` is one of `verdict.REVIEW_VERDICTS` (MERGE, NOT_YET), the
    platform's one verdict vocabulary; `sha` 40 lower-case hex; `title` one
    non-blank line. Anything else is unreadable -- never read as either
    verdict.
    """
    if len(raw) > MAX_REVIEW_BYTES:
        raise VerdictUnreadable(f"{VERDICT_FILENAME} is larger than {MAX_REVIEW_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VerdictUnreadable(f"{VERDICT_FILENAME} is not JSON") from None
    if not isinstance(data, dict):
        raise VerdictUnreadable(f"{VERDICT_FILENAME} is not a JSON object")
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict.strip().upper() not in REVIEW_VERDICTS:
        raise VerdictUnreadable(
            f"{VERDICT_FILENAME}'s verdict is not one of {', '.join(REVIEW_VERDICTS)}"
        )
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise VerdictUnreadable(f"{VERDICT_FILENAME}'s sha is not a 40-hex commit")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or "\n" in title.strip() \
            or "\r" in title:
        raise VerdictUnreadable(f"{VERDICT_FILENAME}'s title is not one non-blank line")
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        raise VerdictUnreadable(f"{VERDICT_FILENAME}'s summary is not text")
    return Review(verdict=verdict.strip().upper(), sha=sha, title=title.strip(), summary=summary)


def _create_only(store: ObjectStore, key: str, data: bytes) -> bool:
    """Write `data` at `key` only if nothing is there. False when something is.

    Atomic in both real stores: GCS's `ifGenerationMatch=0` precondition,
    and `O_EXCL` on the local one. A store with neither is refused rather
    than written with an exists-then-write race.
    """
    native = getattr(store, "upload_bytes_if_absent", None)
    if callable(native):
        return bool(native(key, data, "application/json"))
    if isinstance(store, GcsObjectStore):
        from google.api_core.exceptions import PreconditionFailed  # lazy, as the store is

        from .objectstore import validate_key

        blob = store._get_bucket().blob(validate_key(key))
        try:
            blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
        except PreconditionFailed:
            return False
        return True
    if isinstance(store, LocalObjectStore):
        path = store._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return True
    raise ObjectStoreError(f"{type(store).__name__} has no create-only write; refusing to publish")


def publish_verdict(store: ObjectStore, key: str, data: bytes) -> str:
    """Write the verdict once. `"created"`, `"unchanged"`, or `VerdictAlreadyPublished`."""
    if _create_only(store, key, data):
        return "created"
    try:
        existing = store.download_bytes(key)
    except Exception:  # noqa: BLE001 - unreadable is "not ours", and refused
        existing = None
    if existing == data:
        return "unchanged"
    raise VerdictAlreadyPublished(
        f"a different {VERDICT_FILENAME} is already published at {key}; the verdicts "
        "prefix is write-once, so it is not replaced"
    )


def read_verdict(store: ObjectStore, key: str) -> Review:
    """The verdict at `key`, parsed. `InputUnavailable` when absent or transient.

    Absent is final (the review fails its own attempt first when it wrote
    none, §6a row 4); a read that failed while the object exists is the
    caller's to retry, and is marked so with `retryable`.
    """
    try:
        raw = store.download_bytes(key)
    except Exception as exc:  # noqa: BLE001 - which store raised what is not the point
        try:
            present = store.exists(key)
        except Exception:  # noqa: BLE001
            present = True
        error = InputUnavailable(
            f"verdict_unreadable: no {VERDICT_FILENAME} at {key}" if not present
            else f"verdict_unreadable: {VERDICT_FILENAME} at {key} could not be read "
                 f"({type(exc).__name__})"
        )
        error.retryable = present  # type: ignore[attr-defined]
        raise error from None
    try:
        return parse_review(raw)
    except VerdictUnreadable as exc:
        error = InputUnavailable(f"verdict_unreadable: {exc}")
        error.retryable = False  # type: ignore[attr-defined]
        raise error from None


# ---------------------------------------------------------------------------
# The upstream facts both actions read
# ---------------------------------------------------------------------------


def _task_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SEGMENT.fullmatch(value) else None


def pull_request_number(author_doc: Mapping[str, Any]) -> int | None:
    """The author's `result_summary.git.pull_request.number`, or None.

    A CLAIM, written under the tenant identity (§0): the live pull request it
    names is then checked to be the author's own branch, so a forged number
    names a pull request the action refuses.
    """
    summary = author_doc.get("result_summary")
    git = summary.get("git") if isinstance(summary, Mapping) else None
    pr = git.get("pull_request") if isinstance(git, Mapping) else None
    number = pr.get("number") if isinstance(pr, Mapping) else None
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    return number


def pull_request_belongs(pr: Mapping[str, Any], *, author_branch: str) -> bool:
    """The live pull request is the author's derived branch, from no fork."""
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), Mapping) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
    return (
        head.get("ref") == author_branch
        and isinstance(head_repo.get("full_name"), str)
        and head_repo.get("full_name") == base_repo.get("full_name")
    )


def repo_path(target: forge_mod.ForgeTarget, rest: str = "") -> str:
    return f"/repos/{quote(target.owner, safe='')}/{quote(target.repo, safe='')}{rest}"


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------

#: The one permission post-verdict's token is minted with.
POST_VERDICT_PERMISSIONS = {"pull_requests": "write"}


def run_post_verdict(ctx: ActionContext) -> ActionOutcome:
    """Post the review's verdict on the pull request as the review App. See above."""
    summary: dict[str, Any] = {"action": "post_verdict"}

    def refuse(code: str, message: str, **kwargs: Any) -> ActionOutcome:
        return refusal(summary, EndCause.VERDICT_REFUSED, code, message, **kwargs)

    if ctx.unprotected:
        return refuse("worker_unprotected", ctx.unprotected)
    survivors = ctx.reap()
    if survivors:
        return refuse("processes_alive", f"{len(survivors)} process(es) survived the reap")

    try:
        review_id = verdict_source(ctx.dispatch)
    except VerdictUnreadable as exc:
        return refuse("verdict_source_invalid", str(exc))
    summary["review_task_id"] = review_id

    # The review's spec, and the author's: the verdict and the pull request
    # this action acts on come from them.
    try:
        review_doc = ctx.fetch_upstream(review_id)
        ctx.verify_upstream(review_id, review_doc)
        review_block = (review_doc.get("metadata") or {}).get("dispatch") or {}
        author_id = _task_id(review_block.get("pr_author")) if isinstance(review_block, Mapping) \
            else None
        if author_id is None:
            return refuse("pull_request_unknown",
                          f"the review task {review_id}'s signed dispatch names no pr_author")
        author_doc = ctx.fetch_upstream(author_id)
        ctx.verify_upstream(author_id, author_doc)
    except UpstreamSpecUnverified as exc:
        return refuse("spec_unverified", str(exc), spec_check=exc.spec_check())
    except InputUnavailable as exc:
        summary["refusal"] = {"code": "upstream_unreadable", "message": str(exc)}
        return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc))
    summary["author_task_id"] = author_id

    try:
        target = forge_mod.forge_target_from_env(ctx.environ)
    except forge_mod.ForgeHostRefused as exc:
        return cannot_start(summary, "forge_host_invalid", str(exc))
    summary["repository"] = target.full_name

    try:
        key_name = verdict_key(ctx.tenant_id, ctx.workflow_id or "", review_id)
    except VerdictUnreadable as exc:
        return cannot_start(summary, "verdict_prefix_unavailable", str(exc))
    try:
        review = read_verdict(ctx.store, key_name)
    except InputUnavailable as exc:
        summary["refusal"] = {"code": "verdict_unreadable", "message": str(exc)}
        return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc),
                        retryable=bool(getattr(exc, "retryable", False)))
    event = "APPROVE" if review.verdict == "MERGE" else "REQUEST_CHANGES"
    summary.update({"verdict": review.verdict, "commit_id": review.sha, "event": event})

    number = pull_request_number(author_doc)
    if number is None:
        return refuse("pull_request_unknown",
                      f"the author task {author_id} recorded no pull request number")
    summary["pull_request"] = number

    from .secrets import CredentialMissing

    try:
        key = ctx.read_app_key()
    except CredentialMissing as exc:
        return _outcome(TaskState.PARKED, None, summary, str(exc), credential_missing=exc)
    except Exception as exc:  # noqa: BLE001 - its message names the secret, never the value
        return cannot_start(summary, "credential_unreadable",
                            f"the review App secret could not be read ({type(exc).__name__})")
    if target.review_app_id is not None and key.app_id != target.review_app_id:
        return refuse("app_mismatch",
                      f"the key read is App {key.app_id}, not the registered review App")
    try:
        token = forge_mod.mint_installation_token(
            key=key, owner=target.owner, repo=target.repo,
            permissions=POST_VERDICT_PERMISSIONS, transport=ctx.transport,
            retry=forge_retry(ctx),
        )
    except forge_mod.AppRejected as exc:
        return cannot_start(summary, "app_rejected", str(exc))
    except forge_mod.ForgeUnavailable as exc:
        return unavailable(summary, EndCause.VERDICT_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        return refusal(summary, EndCause.VERDICT_FAILED, getattr(exc, "code", "forge_refused"),
                       str(exc))
    finally:
        del key
    ctx.register_secret(token.token)
    summary.update({"app_id": token.app_id, "installation_id": token.installation_id,
                    "token_expires_at": token.expires_at})
    client = forge_mod.PinnedForgeClient(token=token.token, transport=ctx.transport,
                                         retry=forge_retry(ctx))
    del token
    try:
        if ctx.recheck():
            return cancelled(summary)
        pr = client.get_ok(repo_path(target, f"/pulls/{number}"))
        if not isinstance(pr, Mapping):
            return refuse("pull_request_unknown", f"pull request {number} did not read as one")
        if not pull_request_belongs(pr, author_branch=f"{ctx.branch_prefix}{author_id}"):
            return refuse("pull_request_not_this_workflows",
                          f"pull request {number} is not {ctx.branch_prefix}{author_id} "
                          "from this repository")
        if pr.get("state") != "open":
            return refuse("pull_request_closed", f"pull request {number} is not open")
        body = ctx.scrub(review.summary or f"Verdict: {review.verdict}")[:MAX_REVIEW_BODY_CHARS]
        posted = client.request(
            "POST", repo_path(target, f"/pulls/{number}/reviews"),
            payload={"commit_id": review.sha, "event": event, "body": body},
        )
        if posted.status == 200 and isinstance(posted.data, Mapping):
            summary["review_id"] = posted.data.get("id")
            summary["posted"] = True
            return _outcome(TaskState.SUCCEEDED, None, summary, "")
        message = forge_mod._message_of(posted.data)
        return refusal(summary, EndCause.VERDICT_FAILED, "forge_refused",
                       f"the forge answered {posted.status} to the review"
                       + (f": {message}" if message else ""))
    except forge_mod.ForgeRedirectRefused as exc:
        return refusal(summary, EndCause.VERDICT_FAILED, exc.code, str(exc))
    except forge_mod.ForgeUnavailable as exc:
        return unavailable(summary, EndCause.VERDICT_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        return refusal(summary, EndCause.VERDICT_FAILED, getattr(exc, "code", "forge_refused"),
                       str(exc))
    finally:
        summary["token_revoked"] = forge_mod.revoke_installation_token(client)
        del client
