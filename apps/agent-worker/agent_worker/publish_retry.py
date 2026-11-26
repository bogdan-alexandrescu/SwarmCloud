"""The publish's retryable ends: a branch push the forge never answered
(archived issue 2833) and a publish fetch a partial clone could not complete
(issue 367), with the markers the retry reads and the worker's own question
to the forge about a missing object.

Moved out of `lifecycle.py` (issue 68, a slice of splitting that file by
concern) when issue 367 added to it: `Worker` inherits `PublishRetryMixin`,
so the methods keep their `self` access and every `worker._fail_for_...`
call and test still resolves. The constants and helpers they read stay in
`lifecycle`, whose tests read or patch them there, and are imported at call
time: a module-level import of `lifecycle` from here would be circular.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .gitops import PublishFetchIncomplete, PushUnreachable, forge_holds_object

if TYPE_CHECKING:
    from .lifecycle import Outcome


class PublishRetryMixin:
    """The publish-retry methods of `lifecycle.Worker`, which inherits them."""

    # What `lifecycle.Worker` supplies to these methods; annotations only (no
    # value, so nothing is bound at runtime), so the type checker resolves them.
    _checkpointed_base: Any
    _fail_retryably_terminal: Any
    _git_token: Any
    _git_token_refusal: Any
    _repo_url: Any
    _scrub: Any
    cfg: Any
    checkpoints: Any
    log: Any
    ws: Any

    def _push_unreachable_marker(
        self, exc: PushUnreachable, *, work_head: str | None
    ) -> dict[str, Any]:
        """What `_publish_git` records of a push the forge never answered (archived issue 2833).

        `_unreachable_marker`'s shape, so `_publish_unreachable` reads it the
        same way, and `publish: push` with the two shas the retry checks
        before it publishes this work without running the agent again
        (`_push_to_republish`): `head`, the clone's HEAD the harvest read
        (the agent's last commit), and `base`, the clone base the worker
        recorded in the checkpoint manifest -- before any rebase the harvest
        adopted, since that is what the retry's restore reads back.
        """
        from .lifecycle import REPUBLISH_PUSH

        base = self._checkpointed_base or self.checkpoints.clone_base
        return {
            "reason": str(self._scrub(str(exc)[:500])),
            "tries": int(exc.tries),
            "retry_after_seconds": None,
            "publish": REPUBLISH_PUSH,
            "head": work_head if isinstance(work_head, str) else None,
            "base": base if isinstance(base, str) else None,
        }

    def _fetch_incomplete_marker(
        self, exc: PublishFetchIncomplete, *, work_head: str | None
    ) -> dict[str, Any]:
        """What the publish records of a fetch a partial clone failed (issue 367).

        `_push_unreachable_marker`'s shape, so `_publish_unreachable` and the
        retry read it the same way, with `publish: fetch` and the object git
        named (`missing`) and origin's answer about it (`on_origin`).
        """
        from .lifecycle import REPUBLISH_FETCH

        base = self._checkpointed_base or self.checkpoints.clone_base
        return {
            "reason": str(self._scrub(str(exc)[:500])),
            "tries": 1,
            "retry_after_seconds": None,
            "publish": REPUBLISH_FETCH,
            "head": work_head if isinstance(work_head, str) else None,
            "base": base if isinstance(base, str) else None,
            "missing": exc.missing,
            "on_origin": exc.on_origin,
        }

    def _fail_for_publish_unreachable(
        self, marker: dict[str, Any], summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this ATTEMPT, retryably, because the forge stayed down at publish.

        The same path `_fail_issue_unreachable` takes before the agent runs:
        `fail_retryably` with cause `forge_unreachable`, so the task goes back
        to READY while it has attempts left, its lease and its pool capacity
        are released, and the scheduler admits it again after the forge's own
        `Retry-After` or `FORGE_UNREACHABLE_RETRY_DELAY_SECONDS`, whichever is
        longer. Not `finish(SUCCEEDED)`: a pull-request step with no pull
        request has not delivered. Not `_fail_for_published_nothing`: that is
        for a forge that answered "no", which a retry would meet again; an
        outage may be over by then. The branch, if it was pushed, stays; the
        next attempt resumes from the final checkpoint, pushes the same branch
        and adopts any pull request this attempt opened before losing the
        answer (`open_pull_request`'s 422). Once attempts are spent it ends
        OUTPUTS_MISSING, as `_fail_for_published_nothing` does: the step's
        promised deliverable does not exist.

        A PUSH THE FORGE NEVER ANSWERED ENDS HERE TOO (archived issue 2833), not in
        `_publish_failed`: its marker says `publish: push`. The task goes back
        to READY, so its dependants keep waiting rather than being cancelled,
        and the retry publishes the work this attempt kept without running
        the agent again (`_push_to_republish`).
        """
        from .lifecycle import (
            FORGE_UNREACHABLE,
            FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
            PATCH_NAME,
            REPUBLISH_FETCH,
            REPUBLISH_PUSH,
            EndCause,
            ExitCode,
            Outcome,
        )

        reason = str(marker.get("reason") or "the forge did not answer")
        tries = marker.get("tries")
        retry_after = marker.get("retry_after_seconds")
        if marker.get("publish") == REPUBLISH_FETCH:
            # A PARTIAL CLONE'S PUBLISH FETCH (issue 367): retried, the work
            # kept, rather than `publish_failed` ending the workflow.
            return self._fail_for_fetch_incomplete(marker, reason, summary,
                                                   exit_code=exit_code)
        pushing = marker.get("publish") == REPUBLISH_PUSH
        error = self._scrub(
            f"{FORGE_UNREACHABLE}: the step's branch push met a forge that did not "
            f"answer after {tries} tries: {reason}. The work is kept in {PATCH_NAME} "
            "and the final checkpoint; the attempt is retried while the task has "
            "attempts left, and the retry publishes that work without running the "
            "agent again."
            if pushing else
            f"{FORGE_UNREACHABLE}: the step was to open a pull request, and the "
            f"forge did not answer after {tries} tries: {reason}. The attempt is "
            "retried while the task has attempts left."
        )
        state = self._fail_retryably_terminal(
            exit_code=exit_code,
            error=error,
            cause=FORGE_UNREACHABLE,
            result_summary=summary,
            retry_delay_seconds=max(
                FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                int(retry_after) if isinstance(retry_after, int) else 0,
            ),
            detail={"publish": REPUBLISH_PUSH if pushing else "pull_request", "tries": tries},
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.warning(
            "the forge did not answer at publish; failing the attempt retryably",
            cause=FORGE_UNREACHABLE,
            tries=tries,
            task_state=state.value,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _fail_for_fetch_incomplete(
        self, marker: dict[str, Any], reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this ATTEMPT, retryably, because the clean repository could not
        fetch the agent's work from its partial clone (issue 367).

        Measured 2026-11-24: such a step ended `publish_failed`, its workflow
        FAILED, and about thirteen agent-hours stayed only in the patch. The
        task goes back to READY while it has attempts left, so its dependants
        keep waiting; the retry publishes the kept work without the agent when
        origin has, or was not asked about, the missing object, and runs the
        agent when origin does not have it (`_push_to_republish`). Once
        attempts are spent it ends OUTPUTS_MISSING, as an outage does.
        """
        from .lifecycle import (
            FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
            PATCH_NAME,
            PUBLISH_FETCH_INCOMPLETE,
            REPUBLISH_FETCH,
            EndCause,
            ExitCode,
            Outcome,
        )

        on_origin = marker.get("on_origin")
        retry = (
            "the retry runs the agent again, since origin does not have the object"
            if on_origin is False else
            "the retry publishes that work without running the agent again"
        )
        error = self._scrub(
            f"{PUBLISH_FETCH_INCOMPLETE}: {reason}. The work is kept in {PATCH_NAME} "
            f"and the final checkpoint; the attempt is retried while the task has "
            f"attempts left, and {retry}."
        )
        state = self._fail_retryably_terminal(
            exit_code=exit_code,
            error=error,
            cause=PUBLISH_FETCH_INCOMPLETE,
            result_summary=summary,
            retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
            detail={"publish": REPUBLISH_FETCH, "missing": marker.get("missing"),
                    "on_origin": on_origin},
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.warning(
            "the publish fetch met an object the partial clone never downloaded; "
            "failing the attempt retryably",
            cause=PUBLISH_FETCH_INCOMPLETE,
            on_origin=on_origin,
            task_state=state.value,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _origin_object_prober(
        self, fenced: list[Any]
    ) -> Callable[[str], bool | None] | None:
        """The worker's own question to the forge, whether it serves the object
        a partial clone's publish fetch could not read (`forge_holds_object`,
        issue 367), or None when this worker may not ask.

        `_origin_line_fetcher`'s gates and token handling: a repository URL,
        `_git_token_refusal` before any token is read, the token read at the
        call (only on that failure path), a probe in a fresh repository under
        the worker's private scratch. A superseded attempt's `FencedError` is
        appended to `fenced` for the caller to raise (invariant 5)."""
        from .lifecycle import FencedError

        url = self._repo_url or self.cfg.repository_url
        if not url or self._git_token_refusal():
            return None

        def probe(sha: str) -> bool | None:
            ws = self.ws
            assert ws is not None
            try:
                token = self._git_token()
            except FencedError as exc:
                fenced.append(exc)
                raise
            return forge_holds_object(
                url=url, sha=sha, token=token, private_dir=ws.private, logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds, logger=self.log,
            )

        return probe
