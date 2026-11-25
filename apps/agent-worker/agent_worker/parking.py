"""Parking: every way an attempt stops without failing and without holding
capacity -- on the owner's questions, a CI wait, its children, a release, an
unfinished parent, an empty account pool, a missing credential, a person, the
time limit (to continue), the provider's quota and a lapsed MCP credential --
and the helpers only those parks read.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `ParkMixin`, so the methods keep their `self`
access and every `worker._park_for_quota(...)` call and test still resolves.
`QUESTION_ANSWER_NAME`, `NO_CONTINUATION_CANCEL` and `_parse_iso` live here
alone (issue 193); `lifecycle` no longer binds them, nor the imports only these
methods read. `Outcome`, `_FULL_SHA_RE`, `CONTINUATION_NOTE_NAME`, the other
`NO_CONTINUATION_*` reasons, `NO_ACCOUNT_RETRY_SECONDS`,
`STALE_READING_RETRY_SECONDS`, `POOL_UNANSWERED` and `agent_stream_files` stay
in `lifecycle`, which uses them outside this concern or whose tests read or
patch them there, and are imported where they are used, at call time, so a
patch of `lifecycle.agent_stream_files` still reaches
`_park_for_bridged_question`: a module-level import of `lifecycle` from here
would be circular.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from swarm_common.models import ProviderState, utcnow
from swarm_common.profiles import WorkerAction
from swarm_common.states import EventType, ParkReason, TaskState

from . import children as children_mod
from . import handoff as handoff_mod
from . import mcp_attach as mcp_attach_mod
from . import merge as merge_mod
from . import mergeslot as mergeslot_mod
from . import mergetrain as mergetrain_mod
from . import questions as questions_mod
from .accountlease import (
    ACCOUNTS_FULL,
    ACCOUNT_UNREADABLE,
    BROKER_REFUSED,
    NO_RECENT_READING,
    POOL_PAUSED,
    NoAccountAvailable,
)
from .checkpoint import CheckpointRecord
from .control import (
    RELEASE_WAIT_MAX_RESUMES,
    TIME_LIMIT_CONTINUATIONS_METADATA_KEY,
    TIME_LIMIT_DETAIL_KEY,
    TIME_LIMIT_MAX_CONTINUATIONS,
)
from .errors import ControlPlaneError, ExitCode, FencedWriteRefused, WorkerError
from .procman import ChildProcess, ChildResult
from .quota import QuotaDecision, read_runner_signal
from .runners import question_bridge
from .runners.base import EXIT_QUOTA_EXHAUSTED
from .runners.cliagent import (
    QUESTION_ANSWER_FILE_ENV,
    RESUME_ANSWERED,
    RESUME_REASON_ENV,
    RESUME_TIME_LIMIT,
)
from .runners.streams import cli_agent_spec
from .secrets import CredentialMissing

if TYPE_CHECKING:
    from . import post_verdict as post_verdict_mod
    from .lifecycle import Outcome

#: The answer a session parked on a question resumes with, in `private/`
#: (`Lifecycle._resume_parked_question`): never in `work/`, which the agent
#: can write and a checkpoint archives.
QUESTION_ANSWER_NAME = "question-answer.txt"

#: Why an attempt stopped at its limit does not continue: a cancel was requested
#: (the other reasons, `NO_CONTINUATION_*`, stay in `lifecycle`).
NO_CONTINUATION_CANCEL = "cancel_requested"


class ParkMixin:
    """The park methods of `lifecycle.Worker`, which inherits them."""

    # What `lifecycle.Worker` supplies to these methods; annotations only (no
    # value, so nothing is bound at runtime), so the type checker resolves them.
    _account: Any
    _cancel_requested_now: Any
    _checkpoint: Any
    _child_ended: Any
    _continued_at_time_limit: Any
    _fail_retryably_terminal: Any
    _heartbeat_meanwhile: Any
    _metrics_after_the_record: Any
    _note_session: Any
    _plugins: Any
    _question_answer_path: Any
    _quota_account: Any
    _read_session_file: Any
    _repo_url: Any
    _resume_reason: Any
    _resume_session: Any
    _scrub: Any
    _session_id: Any
    _task: Any
    _upload_outputs: Any
    budget: Any
    cfg: Any
    children: Any
    control: Any
    handoff: Any
    log: Any
    ws: Any

    def _park_for_owner_questions(
        self, action: WorkerAction, outcome: "post_verdict_mod.ActionOutcome"
    ) -> Outcome:
        """The merge's owner-question hold: PARKED(HUMAN_REQUIRED), release, exit 75 (archived issue 2070).

        The hand-off's park exactly (`handoff_mod.park_human_required`, as
        `_park_for_answers`): one fenced transaction, no lease, the attempt
        refunded within `max_human_resumes`. The answer that clears every
        question the hold names returns it to READY (swarm-api's answer
        route, QUESTION-WAKE, PR 2151), as a person's `human-ready` does --
        whereupon the merge reads every answer again. A review re-running
        does not wake it, and the scheduler's human-wait expiry spares it as
        it spares `question_wait` / `release_wait` parks: see `merge`. The
        questions ride in the park's `blocked_by` entry
        under `owner_questions`, NOT `question_wait`: they are not this
        task's own, and swarm-api's answer route reads `question_wait` as
        questions asked by the parked task. No checkpoint: a worker action
        keeps no workspace.
        """
        from .lifecycle import Outcome

        wait = dict(outcome.owner_wait or {})
        # A CI wait handed to a person (`merge.CI_WAIT_STUCK`, part of archived issue 2688)
        # names its own phase, and its evidence rides under it; the owner
        # questions' hold keeps `owner_questions`.
        phase = str(wait.pop("park_phase", None) or "owner_questions")
        uploaded = self._upload_outputs().get("artifacts")
        self.log.info("the merge waits for a person; parking", phase=phase,
                      action=action.value, questions=len(wait.get("questions") or []),
                      unreadable=wait.get("unreadable"))
        with self._metrics_after_the_record():
            park = handoff_mod.park_human_required(
                self.control,
                max_resumes=self.handoff.max_resumes,
                detail={"park_phase": phase, phase: self._scrub(wait)},
                uploads=uploaded if isinstance(uploaded, list) else None,
            )
        if park.returned_by is not None:
            return Outcome(exit_code=ExitCode.PARKED, state=TaskState.READY)
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_ci_pending(
        self, action: WorkerAction, wait: dict[str, Any]
    ) -> Outcome:
        """The merge's CI wait: park CI_PENDING, release, exit 75 (MS2).

        docs/merge-step.md "Revised 2026-10-06" §1: a wait is a park, not a
        retry. The fenced park, its refund and its fallback instant are one
        transaction (`ControlPlane.park_ci_pending`); a superseded attempt
        raises `FencedWriteRefused` there, which `run` stands down on, with
        nothing written and the lease untouched (invariant 5). No checkpoint:
        a worker action keeps no workspace.
        """
        from .lifecycle import Outcome

        code = str(wait.get("code") or "checks_pending")
        # A wait for the repository's merge slot (merge race, archived issue 295) is not a
        # wait for CI: counted on its own bound, and not on the CI clock that
        # `checks_timeout` reads, so a long queue neither spends the step's CI
        # wakes nor times its checks out.
        slot_wait = code == mergeslot_mod.MERGE_SLOT_WAIT
        # A wait on the repository's merge train (TRAIN-WS) likewise: its own
        # bound and counter, off the CI clock -- several trains ahead of it
        # are a queue, not a slow CI.
        train_wait = code == mergetrain_mod.TRAIN_WAITING
        # Recorded before any metrics export (TEL archived pull request 718, `_metrics_after_the_record`).
        with self._metrics_after_the_record():
            refunded = self.control.park_ci_pending(
                code=code,
                head=str(wait.get("head") or ""),
                pull_request=int(wait.get("pull_request") or 0),
                pending=[str(name) for name in wait.get("pending") or []],
                # Absent when the step read no check at this head: not read
                # is not "none failed" (WAIT-RECORD, part of archived issue 1782).
                failed=([str(name) for name in wait["failed"]]
                        if isinstance(wait.get("failed"), list) else None),
                max_wakes=(mergeslot_mod.MERGE_SLOT_MAX_WAITS if slot_wait
                           else mergetrain_mod.TRAIN_MAX_WAITS if train_wait
                           else merge_mod.MERGE_CI_MAX_WAKES),
                fallback_seconds=merge_mod.MERGE_CI_FALLBACK_SECONDS,
                counter=("slot_waits" if slot_wait else "train_waits" if train_wait
                         else "wakes"),
                ci_clock=not (slot_wait or train_wait),
            )
        self.log.info("worker action parked until CI settles", action=action.value,
                      code=wait.get("code"), attempt_refunded=refunded)
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _await_children(self, result: ChildResult) -> Outcome | None:
        """Park on CHILDREN_INCOMPLETE when this parent's children still run.

        §3.3: every outstanding request is answered first (F3), in at most
        one `child_submit_retry_seconds` (`ChildPath.drain`); then, for an
        agent that exited 0 --
        having asked to `await`, or not (step 8: a parent never succeeds over
        running children) -- a live child means checkpoint, upload, park,
        release, exit, exactly as the quota park does. No live child, no park:
        the await is ignored (F15). An agent that failed fails its attempt in
        the ordinary way; its children are kept (F4). None means "finalise".

        WITHOUT A CHILD PATH this attempt can neither submit nor list, but
        an earlier attempt of this task may have made children: the spool,
        restored from the checkpoint, records the ids it was answered with.
        Then the implicit await is NOT skipped -- whether they still run is
        unknown, and a parent must never succeed over running children
        (step 8) -- so it parks conservatively, as for a listing that failed;
        the scheduler's await sweep reads the children itself and promotes
        it once they are done. Bounded like every await: past
        `max_child_await_resumes` the park counts as an attempt.

        Fences propagate as `FencedWriteRefused`, which `run` stands down on.
        """
        ws = self.ws
        assert ws is not None
        if not self.children.offered:
            if result.exit_code != 0 or result.timed_out or result.killed:
                return None
            known = self.children.known_children(ws.work)
            if not known:
                return None
            self.children.clear_await(ws.work)
            self.log.warning(
                "no child path this attempt, and the spool records children; "
                "parking until the scheduler finds them done",
                known_children=len(known),
                child_path=self.children.unavailable,
            )
            return self._park_awaiting(
                requested=False, live=None, child_path=self.children.unavailable
            )
        try:
            with self._heartbeat_meanwhile("child requests"):
                self.children.drain(ws.work)
        except children_mod.ChildSubmitFenced as exc:
            raise FencedWriteRefused(
                self.cfg.generation, -1, f"child submission: {exc.code}", write="child submission"
            ) from None
        requested = self.children.await_requested(ws.work)
        # Cleared BEFORE the checkpoint, so a resumed attempt starts clean.
        self.children.clear_await(ws.work)
        if result.exit_code != 0 or result.timed_out or result.killed:
            return None
        live = self.children.live_children(ws.work)
        if live == []:
            if requested:
                self.log.info("await asked with no live child; ignored")
            return None
        self.log.warning(
            "children still running; parking until they end",
            live_children=len(live) if live is not None else None,
            asked=requested,
        )
        return self._park_awaiting(requested=requested, live=live)

    def _park_awaiting(
        self, *, requested: bool, live: list[str] | None, child_path: str | None = None
    ) -> Outcome:
        """Checkpoint, upload, park on CHILDREN_INCOMPLETE, release, exit (§3.3 step 3)."""
        from .lifecycle import Outcome

        self._checkpoint("child-await")
        uploaded = self._upload_outputs().get("artifacts")
        detail: dict[str, Any] = {
            "park_phase": "child_await",
            "asked": requested,
            "live_children": sorted(live) if live is not None else None,
        }
        if child_path is not None:
            detail["child_path"] = child_path
        # Recorded with the park, for the attempt that finishes the parent (archived issue 166).
        with self._metrics_after_the_record():
            self.control.park_awaiting_children(
                max_resumes=self.cfg.max_child_await_resumes,
                detail=detail,
                uploads=uploaded if isinstance(uploaded, list) else None,
            )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _await_owner_or_release(self, result: ChildResult) -> Outcome | None:
        """Park for the owner's answers or for a release, when the agent asked (AU-7).

        docs/design/agent-autonomy.md §3.8 and §3.9. Only for an agent that
        exited 0: one that failed fails its attempt in the ordinary way. An
        `await-answers` beside a `questions.json` with a blocking question
        parks HUMAN_REQUIRED; an `await-release.json` parks
        DEPENDENCY_INCOMPLETE. A file that asks and is not well formed is
        not a park: the attempt ends as it would have, with a line saying
        why, and the questions file's own fault in its summary. None means
        "finalise". Both parks checkpoint and upload first (invariants 4
        and 8), and both are fenced (invariant 5).
        """
        ws = self.ws
        assert ws is not None
        if result.exit_code != 0 or result.timed_out or result.killed:
            return None
        if self._question_bridge_on():
            bridged = self._park_for_bridged_question()
            if bridged is not None:
                return bridged
        try:
            asked = questions_mod.await_answers(ws.artifacts)
        except questions_mod.Rejected as exc:
            self.log.warning("the agent asked to wait for answers; not parked", reason=str(exc))
            asked = None
        if asked is not None:
            return self._park_for_answers(asked)
        try:
            sha = questions_mod.await_release(ws.artifacts)
        except questions_mod.Rejected as exc:
            self.log.warning("the agent asked to wait for a release; not parked", reason=str(exc))
            sha = None
        if sha is not None:
            return self._park_for_release(sha)
        return None

    def _question_bridge_on(self) -> bool:
        """The question bridge, for this attempt: the worker's own switch
        (`question_bridge.ENABLE_ENV`, exactly "1"), a step that started
        agent plugins (`plugins_mod.start`), and a Claude Code runner. Off,
        nothing here reads or parks on a question, and the runner registers
        no hook."""
        spec = cli_agent_spec(self.cfg.runner_profile)
        return (question_bridge.enabled(os.environ) and self._plugins is not None
                and getattr(spec, "name", None) == "claude-code")

    def _question_bridge_env(self) -> dict[str, str]:
        """The runner's switch, and the answer a parked session resumes with
        (whatever the switch says now: a session parked is resumed with its
        answer even on a worker the switch was since turned off on)."""
        env: dict[str, str] = {}
        if self._question_answer_path is not None and self._resume_reason == RESUME_ANSWERED:
            env[QUESTION_ANSWER_FILE_ENV] = str(self._question_answer_path)
        if self._question_bridge_on():
            env[question_bridge.ENABLE_ENV] = "1"
        return env

    def _resume_parked_question(self, task: dict[str, Any]) -> None:
        """Resume the session a bridged question parked, once it is answered.

        After the restore, before the runner: the restored transcript must be
        the parked session's (`question_bridge.restored_session`), or the
        attempt fails with nothing run -- a fresh session would not know what
        it asked. The answer message goes to `private/`, outside `work/`, and
        the runner is told to `--resume` the session with it. Nothing is
        written to the task here: a stale attempt is fenced before it reaches
        this (invariant 5) and the answer stays as recorded.
        """
        ws = self.ws
        assert ws is not None
        try:
            resume = questions_mod.bridge_resume(task)
        except ValueError as exc:
            raise WorkerError(
                f"question_resume_failed: the parked question's record is not one the worker wrote ({exc})"
            ) from None
        if resume is None:
            return
        session = resume["session_id"]
        if not question_bridge.restored_session(ws.work, session):
            raise WorkerError(
                "question_resume_failed: the restored workspace does not hold the session that "
                "parked on the question; it is not started without it"
            )
        path = ws.private / QUESTION_ANSWER_NAME
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(resume["message"])
        self._question_answer_path = path
        self._note_session(session)
        self._resume_session = session
        self._resume_reason = RESUME_ANSWERED
        self.log.info("resuming the session that parked on a question, with its answer")

    def _checkout_revision(self) -> str | None:
        """The checkout's HEAD, the revision a parked question was asked about; None without one."""
        from .lifecycle import _FULL_SHA_RE

        ws = self.ws
        if ws is None or not self._repo_url:
            return None
        try:
            done = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
                 "-C", str(ws.checkout()), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        head = done.stdout.strip()
        return head if done.returncode == 0 and _FULL_SHA_RE.fullmatch(head) else None

    def _park_for_bridged_question(self) -> Outcome | None:
        """Park the step on the question its agent asked through the bridge, or None.

        The hook's record is read ONCE and checked against the agent's
        stream (`question_bridge.classify`), then removed: from here on the
        park's `question_wait` is the record (`question_bridge.wait_record`),
        with the session and tool use, the attempt, its fencing generation
        and the checkout's revision. A run and a record that disagree fail
        the attempt (`question_bridge_failed`). The park is refused, and the
        attempt fails, when the checkpoint before it would not hold the
        session's transcript and gstack's state or did not upload: a park
        nobody can resume is a lost step.
        """
        from .lifecycle import agent_stream_files

        ws = self.ws
        assert ws is not None
        from .runners.claude_code import SETTINGS_DIR_NAME  # lazy: the claude-code runner's

        files = agent_stream_files(self.cfg.runner_profile)
        events = question_bridge.read_stream(ws.artifacts / files.stdout) if files else []
        pending_path = ws.work / SETTINGS_DIR_NAME / question_bridge.PENDING_NAME
        outcome, pending = question_bridge.classify(events, pending_path)
        try:
            question_bridge.clear_pending(pending_path)
        except OSError:
            pass
        if outcome == question_bridge.NOT_ASKED:
            return None
        if outcome != question_bridge.PARKED or pending is None:
            return self._fail_bridged_question(
                "question_bridge_failed: the agent's question and its record disagree")
        gaps = self._bridge_checkpoint_gaps(pending.session_id)
        if gaps:
            return self._fail_bridged_question(
                "question_park_refused: the checkpoint would not hold " + "; ".join(gaps))
        record = question_bridge.wait_record(
            pending, attempt_id=self.cfg.attempt_id, generation=self.cfg.generation,
            source_revision=self._checkout_revision(), asked_at=utcnow().isoformat(),
            skill=question_bridge.asked_by_skill(events, pending.tool_use_id))
        return self._park_for_answers(self._scrub(record), require_checkpoint=True)

    def _bridge_checkpoint_gaps(self, session: str) -> list[str]:
        """What a checkpoint of `work/` would miss to resume `session`: its
        transcript, and gstack's state root when it is not under `work/`."""
        ws = self.ws
        assert ws is not None
        gaps = []
        if not question_bridge.restored_session(ws.work, session):
            gaps.append("the session's transcript under work/.claude/projects")
        env = self._plugins.env if self._plugins is not None else {}
        home = env.get("GSTACK_HOME")
        if home:
            try:
                Path(home).resolve().relative_to(ws.work.resolve())
            except ValueError:
                gaps.append("GSTACK_HOME, which is outside the attempt's work/")
        return gaps

    def _fail_bridged_question(self, error: str) -> Outcome:
        """Fail the attempt retryably: a question the step cannot park on."""
        from .lifecycle import Outcome

        self.log.error("not parked on the agent's question", reason=error)
        state = self._fail_retryably_terminal(
            exit_code=0, error=error, cause=error.split(":", 1)[0])
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _park_for_answers(self, asked: dict[str, Any], *, require_checkpoint: bool = False) -> Outcome:
        """Checkpoint, upload, PARKED(HUMAN_REQUIRED) with `question_wait`, release, exit.

        §3.9 step 1. The hand-off's park exactly (`handoff_mod.park_human_required`):
        no lease, no tokens, the attempt refunded within `max_human_resumes`,
        and only a person -- here, the last blocking answer -- returns it to
        READY. The record carries the validated questions, which is how
        swarm-api serves them while the task has no result summary.

        `require_checkpoint` (a bridged question): a checkpoint that did not
        upload refuses the park and fails the attempt, since the session the
        answer resumes is in it.
        """
        from .lifecycle import Outcome

        cfg = self.cfg
        if self._checkpoint("question-wait") is None and require_checkpoint:
            return self._fail_bridged_question(
                "question_park_refused: the checkpoint before the park failed")
        uploaded = self._upload_outputs().get("artifacts")
        wait = {**asked, "attempt_id": cfg.attempt_id, "asked_at": utcnow().isoformat()}
        self.log.info(
            "the agent asked the owner blocking questions; parking until they answer",
            questions=len(asked["questions"]),
            blocking=len(asked["blocking"]),
        )
        with self._metrics_after_the_record():
            park = handoff_mod.park_human_required(
                self.control,
                max_resumes=self.handoff.max_resumes,
                detail={"park_phase": "question_wait", questions_mod.QUESTION_WAIT_KEY: wait},
                uploads=uploaded if isinstance(uploaded, list) else None,
            )
        if park.returned_by is not None:
            return Outcome(exit_code=ExitCode.PARKED, state=TaskState.READY)
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_for_release(self, sha: str) -> Outcome:
        """Checkpoint, upload, PARKED(DEPENDENCY_INCOMPLETE) with `release_wait`, release, exit.

        §3.8. swarm-api's merge-wake tick reads the release of `sha` and
        returns the task to READY once it is terminal; nothing here waits.

        THE WAIT REFUNDS ITS ATTEMPT, WITHIN A BOUND (AU7-REFUND, decided
        2026-10-22): `ControlPlane.park_release_wait`, one fenced
        transaction like the CI wait's park. A plain `park` spent one of
        `max_attempts` on every wait, and a wait on the last attempt was
        never woken. A superseded attempt raises `FencedWriteRefused` there
        with nothing written, which `run` stands down on (invariant 5).
        """
        from .lifecycle import Outcome

        cfg = self.cfg
        self._checkpoint("release-wait")
        uploaded = self._upload_outputs().get("artifacts")
        now = utcnow()
        self.log.info("the agent asked to wait for a release; parking", sha=sha)
        with self._metrics_after_the_record():
            refunded = self.control.park_release_wait(
                detail={
                    "park_phase": "release_wait",
                    questions_mod.RELEASE_WAIT_KEY: {
                        "sha": sha, "attempt_id": cfg.attempt_id, "asked_at": now.isoformat()},
                    **({"artifacts": uploaded} if isinstance(uploaded, list) else {}),
                },
                max_resumes=RELEASE_WAIT_MAX_RESUMES,
                uploads=uploaded if isinstance(uploaded, list) else None,
            )
        self.log.info("parked until the release settles", sha=sha, attempt_refunded=refunded)
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _incomplete_parents(self, task: dict[str, Any]) -> dict[str, str | None]:
        """The signed parents of `task` that have not SUCCEEDED, with their state.

        `task` is the document `_verify_spec` just verified, and the ids come
        from its `depends_on`, which the signature covers
        (`swarm_common.specsign`). None as a state means the parent has no
        document, or a state the contract does not name.

        A `depends_on` that is not a list of task ids is refused, not
        skipped: skipping it would run a step whose dependencies nobody
        checked. swarm-api never signs one, so this is reachable only by a
        task the legacy window admitted unsigned.
        """
        parents = task.get("depends_on")
        if parents is None:
            return {}
        if not isinstance(parents, list) or not all(
            isinstance(p, str) and p for p in parents
        ):
            raise ControlPlaneError("depends_on is not a list of task ids")
        if not parents:
            return {}
        states = self.control.fetch_parent_states(parents)
        return {
            parent: (state.value if state is not None else None)
            for parent, state in states.items()
            if state is not TaskState.SUCCEEDED
        }

    def _park_dependency_incomplete(self, waiting_on: dict[str, str | None]) -> Outcome:
        """A parent has not SUCCEEDED: park, give the slot back, run nothing.

        No checkpoint and no output upload, unlike the other parks: nothing
        has run and nothing has been restored, so the workspace is empty, and
        a checkpoint of it would become the task's `latest_checkpoint` over
        the real one. Due at once: the scheduler's dependency sweep, not a
        clock, decides when it is READY again.
        """
        from .lifecycle import Outcome

        self.log.warning(
            "a signed parent has not succeeded; parking without running",
            park_reason=ParkReason.DEPENDENCY_INCOMPLETE.value,
            waiting_on=waiting_on,
        )
        self.control.park(
            reason=ParkReason.DEPENDENCY_INCOMPLETE,
            next_eligible_at=utcnow(),
            detail={
                "waiting_on": sorted(waiting_on),
                "parent_states": waiting_on,
                "park_phase": "parent_states",
            },
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_no_account(self, exc: NoAccountAvailable) -> Outcome:
        """The pool cannot serve this attempt. Park -- it costs nothing.

        NOT A FAILURE, for any of the reasons that land here. The tenant did
        nothing wrong, the credential is fine, and one of the three attempts
        must not be spent on a queue -- nor on a missing IAM grant, which no
        number of retries will produce. Parking gives the slot and the memory
        back and the scheduler returns the task by itself.

        THE WAIT DEPENDS ON WHAT IS ACTUALLY BEING WAITED FOR, and the reasons
        are not interchangeable:

          * a reset instant from the broker's `Account.next_reset` -- wake
            exactly then, rather than polling;
          * `no_recent_reading` -- nothing says the accounts are spent, only
            that nobody has looked lately, and the broker's usage poll looks
            every sweep. Minutes, not a quarter of an hour -- or the broker's
            instant, when a window clears sooner than that;
          * `accounts_full` -- every account with room already holds its cap
            of agents (archived issue 2222). A slot frees when one of them finishes, which
            no clock says, so the same minutes as a stale reading -- or a
            window instant, when one clears sooner;
          * `pool_paused` -- waiting on a PERSON. There is no instant to wake
            at, so the long fallback;
          * `broker_refused` / `account_unreadable` -- a configuration error.
            It parks as CREDENTIAL_MISSING rather than as quota, because the
            honest summary is "an admin has to fix something", not "come back
            when there is room".
        """
        from .lifecycle import (
            NO_ACCOUNT_RETRY_SECONDS,
            POOL_UNANSWERED,
            STALE_READING_RETRY_SECONDS,
            Outcome,
        )

        reason = exc.decision.reason
        configuration_error = reason in (BROKER_REFUSED, ACCOUNT_UNREADABLE)
        # Both undated waits that clear in minutes: a usage poll, or an agent
        # on a full account finishing.
        undated_soon = reason in (NO_RECENT_READING, ACCOUNTS_FULL)
        fallback_seconds = (
            STALE_READING_RETRY_SECONDS
            if undated_soon
            else NO_ACCOUNT_RETRY_SECONDS
        )
        fallback = utcnow() + timedelta(seconds=fallback_seconds)
        reset = _parse_iso(exc.decision.next_reset_at)
        if undated_soon:
            # THE SOONER OF THE TWO (archived issue 1885). The broker answers
            # `no_recent_reading` when ANY candidate waits on its next usage
            # poll, and still sends the earliest window instant it knows.
            # That instant may be another account's seven-day clear: parking
            # on it would sleep for days past a poll minutes away, and parking
            # on the poll alone would sleep past a window that clears sooner.
            next_eligible = min(reset, fallback) if reset is not None else fallback
        else:
            next_eligible = reset or fallback
        self.log.warning(
            "the account pool cannot serve this attempt; parking",
            provider=exc.provider,
            reason=reason,
            waiting_on=(
                "an administrator" if configuration_error
                else "a person" if reason == POOL_PAUSED
                else "the broker's next usage poll" if reason == NO_RECENT_READING
                else "an agent on a full account to finish" if reason == ACCOUNTS_FULL
                else "the quota broker to answer" if reason in POOL_UNANSWERED
                else "a quota window"
            ),
            next_eligible_at=next_eligible.isoformat(),
        )
        self._checkpoint("no-account")
        summary = self._upload_outputs()
        detail = {
            "provider": exc.provider,
            "account_pool_reason": exc.decision.reason,
            **summary,
        }
        with self._metrics_after_the_record():
            # Refunded within the server's bound when PROVIDER_QUOTA_EXHAUSTED
            # (archived issue 2350); a CREDENTIAL_MISSING park refunds nothing.
            refunded = self.control.park(
                # PROVIDER_QUOTA_EXHAUSTED when the pool will recover on its own:
                # the tenant HAS credentials, they are simply all spent or all
                # unobserved, and sending an operator to look at that wastes their
                # time. CREDENTIAL_MISSING when it will NOT recover on its own --
                # a broker that refused this worker, or an account whose secret
                # nobody granted it access to, is a configuration error, and that
                # ParkReason is the one that means "an admin must act".
                #
                # A ParkReason naming the pool would be more honest than either;
                # ParkReason is in the frozen contract, so that is a request in the
                # report, not a change.
                reason=(
                    ParkReason.CREDENTIAL_MISSING
                    if configuration_error
                    else ParkReason.PROVIDER_QUOTA_EXHAUSTED
                ),
                next_eligible_at=next_eligible,
                detail={**detail, "park_phase": "account_assign"},
                # THE ANNOUNCEMENT IS WRITTEN IN THE PARK'S OWN TRANSACTION. It used
                # to be emitted just above this call. A fence that landed during
                # the checkpoint's upload or the output upload was met by the park,
                # which refused, and QUOTA_EXHAUSTED was already in a task stream
                # that belonged to a newer generation, announcing a park that never
                # happened. Now it commits with the park or not at all.
                announce=[
                    (
                        EventType.QUOTA_EXHAUSTED,
                        {
                            **detail,
                            "next_eligible_at": next_eligible,
                            "park_phase": "account_assign",
                        },
                    )
                ],
            )
        self.log.info("parked without an account", attempt_refunded=bool(refunded))
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_credential_missing(self, exc: CredentialMissing) -> Outcome:
        """No key for this provider. Park; it costs nothing while an admin fixes it."""
        from .lifecycle import Outcome

        self.log.error("tenant credential missing; parking", provider=exc.provider)
        self._checkpoint("credential-missing")
        summary = self._upload_outputs()
        with self._metrics_after_the_record():
            self.control.park(
                reason=ParkReason.CREDENTIAL_MISSING,
                next_eligible_at=utcnow() + timedelta(hours=1),
                detail={"provider": exc.provider, **summary},
            )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_for_human(self, child: ChildProcess, *, cause: str) -> Outcome:
        """A hand-off's hold ended with nobody in control (live-browser §4.3).

        The agent's `wait` exits 3 and it gets `EXPIRY_NOTES_SECONDS` to write
        its notes into `work/`; then: wipe, checkpoint, upload,
        PARKED(HUMAN_REQUIRED) with the attempt refunded within
        `max_human_resumes`, release, exit. Only a person's `human-ready`
        promotes it. `cause` is `hold_expired`, or `gate_lost` when the gate's
        channel dropped (§5.4.2), which ends the hand-off the same way.
        """
        from .lifecycle import Outcome

        cfg = self.cfg
        ws = self.ws
        assert ws is not None
        self.log.warning(
            "nobody took control within the hand-off's hold; parking for a person",
            cause=cause,
        )
        # Resolved at swarm-api while the task is RUNNING, the agent told, and
        # the browser wiped before the checkpoint (§5.5).
        self.handoff.end(ws.work, outcome="expired", cause=cause)
        with self._heartbeat_meanwhile("hand-off notes"):
            if child.wait(handoff_mod.EXPIRY_NOTES_SECONDS) is None:
                child.terminate(cfg.termination_grace_seconds, reason="hand-off hold expired")
        child.finish()
        self._child_ended()
        self._checkpoint("human-wait")
        uploaded = self._upload_outputs().get("artifacts")
        with self._metrics_after_the_record():
            park = handoff_mod.park_human_required(
                self.control,
                max_resumes=self.handoff.max_resumes,
                detail={"park_phase": "human_wait", "cause": cause},
                uploads=uploaded if isinstance(uploaded, list) else None,
            )
        if park.returned_by is not None:
            # A person returned control while this attempt parked: re-queued
            # READY, lease released all the same (LB-RETURN).
            self.log.info("a person returned control as the hold ended; re-queued")
            return Outcome(exit_code=ExitCode.PARKED, state=TaskState.READY)
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    # -- continuing at the time limit (the owner, 2026-11-02) ---------------------
    def _time_limit_continuations(self) -> int:
        """The continuations at the time limit this task has had: the park's
        count on the task this attempt verified when it started."""
        metadata = (self._task or {}).get("metadata")
        value = metadata.get(TIME_LIMIT_CONTINUATIONS_METADATA_KEY) if isinstance(
            metadata, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    def _time_limit_refusal(self, final: CheckpointRecord | None) -> str | None:
        """Why this attempt, stopped at its time limit, does NOT continue; None
        when it does. Never raises.

        * no final checkpoint uploaded: the next attempt would start from an
          older tree, or none, so the stop fails as it always did;
        * a requested cancel: somebody stopped the task, and a park would put
          it back in line;
        * the cap: `TIME_LIMIT_MAX_CONTINUATIONS` continuations already;
        * the budget: the attempt spent its budget, or the task's next attempt
          would take it past it. The budget is pinned per attempt, so the
          projection assumes every attempt of the chain spends what this one
          did: the attempts so far, this one and the next, against the
          limit, in each unit the task has one.
        """
        from .lifecycle import (
            NO_CONTINUATION_BUDGET,
            NO_CONTINUATION_CAP,
            NO_CONTINUATION_CHECKPOINT,
        )

        if final is None:
            return NO_CONTINUATION_CHECKPOINT
        if self._cancel_requested_now():
            return NO_CONTINUATION_CANCEL
        used = self._time_limit_continuations()
        if used >= TIME_LIMIT_MAX_CONTINUATIONS:
            return NO_CONTINUATION_CAP
        guard = self.budget
        if guard is not None:
            if guard.exceeded:
                return NO_CONTINUATION_BUDGET
            attempts = used + 2
            if guard.limits.tokens is not None and guard.tokens * attempts > guard.limits.tokens:
                return NO_CONTINUATION_BUDGET
            usd = guard.usd
            if guard.limits.usd is not None and usd is not None and usd * attempts > guard.limits.usd:
                return NO_CONTINUATION_BUDGET
        return None

    def _continuation_note_path(self) -> Path:
        from .lifecycle import CONTINUATION_NOTE_NAME

        assert self.ws is not None
        return self.ws.work / CONTINUATION_NOTE_NAME

    def _write_continuation_note(self) -> None:
        """Leave the session to resume in `work/`, for the final checkpoint to
        carry to the next attempt. Never raises: without the note the next
        attempt starts the task again in the restored tree, told why."""
        try:
            if self._account is None:
                self._note_session(self._read_session_file())
            body = {
                "continuation": self._time_limit_continuations() + 1,
                "session_id": self._session_id,
            }
            self._continuation_note_path().write_text(json.dumps(body))
        except OSError as exc:
            self.log.warning("could not leave the continuation note", error=str(exc))

    def _read_continuation_note(self) -> None:
        """After a restore: resume the session the attempt stopped at the time
        limit left, when this task's own count says it continued. Removed
        either way, so it never reaches the agent's tree or a publish."""
        path = self._continuation_note_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        finally:
            path.unlink(missing_ok=True)
        if not isinstance(data, dict):
            return
        continuation = data.get("continuation")
        used = self._time_limit_continuations()
        if not used or continuation != used:
            # A note from a stop that did not continue (it failed, or a
            # person retried the task): nothing to resume.
            return
        self._continued_at_time_limit = used
        session = data.get("session_id")
        if isinstance(session, str) and session:
            self._note_session(session)
            self._resume_session = session
            self._resume_reason = RESUME_TIME_LIMIT
        self.log.info(
            "continuing after the time limit",
            continuation=used,
            max_continuations=TIME_LIMIT_MAX_CONTINUATIONS,
            resumes_session=self._resume_session is not None,
        )

    def _continuation_env(self) -> dict[str, str]:
        """The runner's note that this attempt continues after a time limit,
        when there is no session to resume (a resumed one is told by the
        channel's own RESUME_REASON_ENV)."""
        if not self._continued_at_time_limit or self._resume_session:
            return {}
        return {RESUME_REASON_ENV: RESUME_TIME_LIMIT}

    def _continue_at_time_limit(
        self, final: CheckpointRecord | None, summary: dict[str, Any]
    ) -> Outcome:
        """Park SCHEDULED_RETRY, now: the stop at the time limit continues in a
        new attempt (the owner, 2026-11-02). The park is the SIGTERM park's
        (`_handle_interruption`): one fenced transaction that parks, releases
        the lease and the pools and ends the attempt, so nothing holds
        capacity while the task waits (invariant 1) and nothing sleeps
        (invariant 4). The scheduler's `_promote_scheduled_retries` returns
        it to READY, and admission leases the next attempt at a new
        generation. A fence is raised to `run`, which stands down."""
        from .lifecycle import Outcome

        continuation = self._time_limit_continuations() + 1
        self.log.warning(
            "the step reached its time limit with its checkpoint uploaded; "
            "continuing in a new attempt",
            timeout_seconds=self.cfg.timeout_seconds,
            continuation=continuation,
            max_continuations=TIME_LIMIT_MAX_CONTINUATIONS,
        )
        block: dict[str, Any] = {
            "continuation": continuation,
            "max_continuations": TIME_LIMIT_MAX_CONTINUATIONS,
            "timeout_seconds": self.cfg.timeout_seconds,
            "checkpoint_id": final.checkpoint_id if final is not None else None,
        }
        if self.budget is not None:
            block["budget"] = self.budget.summary()
        with self._metrics_after_the_record():
            self.control.park(
                reason=ParkReason.SCHEDULED_RETRY,
                next_eligible_at=utcnow(),
                detail={"cause": "time_limit_continuation", TIME_LIMIT_DETAIL_KEY: block,
                        **summary},
            )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_for_quota(self, decision: QuotaDecision, *, source: str) -> Outcome:
        """Checkpoint -> upload -> publish quota -> PARK -> release -> exit."""
        from .lifecycle import Outcome

        self.log.warning(
            "parking on provider quota",
            wait_seconds=decision.wait_seconds,
            reason=decision.reason.value,
            source=source,
        )
        self._checkpoint("quota-park")
        summary = self._upload_outputs()
        # No metrics export here: it comes after the broker update and the
        # park below (`_metrics_after_the_record`, measured 2026-10-06).
        provider = str(decision.detail.get("provider") or self.cfg.provider or "")
        if provider:
            # NOT FENCED, and not a task write. This is the tenant's document
            # for the provider (`quota/{provider}:{tenant}`). What it records,
            # a 429 and its retry-after, was observed whether or not the park
            # below finds this attempt fenced, so an attempt fenced during the
            # uploads above still publishes it. Its place ahead of the park is
            # unchanged.
            try:
                state = ProviderState(str(decision.detail.get("provider_state", "EXHAUSTED")))
            except ValueError:
                state = ProviderState.EXHAUSTED
            # The account this attempt still holds -- it is given back on the
            # way out, in `_cleanup` -- so a park on an account's 429 names it
            # (archived issue 2302). Only a RUNNER's 429 is the account's: a stop read from
            # the broker (preflight, backpressure) was no account's refusal.
            # The park itself is unchanged (invariant 4).
            from_runner = decision.detail.get("source") == "runner"
            self.control.update_quota_state(
                provider=provider,
                state=state,
                retry_after_seconds=decision.wait_seconds,
                reset_at=decision.next_eligible_at,
                account=self._quota_account() if from_runner else None,
                message=str(decision.detail.get("signal_detail") or ""),
            )
        # `source` is where the signal was OBSERVED (the runner's 429, the
        # control plane's aggregate, the pre-flight check); `decision.detail`
        # already carries where it came FROM. Both are kept, under distinct keys.
        #
        # The QUOTA_EXHAUSTED announcement is written in the park's own
        # transaction. It used to be emitted here, ahead of the park. A fence
        # that landed during the uploads above was met by the park, which
        # refused, and the announcement of a park that never happened was
        # already in a stream that belonged to a newer generation.
        # A PROVIDER_QUOTA_EXHAUSTED park refunds its attempt within the
        # server's bound (archived issue 2350): parking is invariant 4's designed path, so it
        # does not spend one of `max_attempts` until the bound is passed.
        with self._metrics_after_the_record():
            refunded = self.control.park(
                reason=decision.reason,
                next_eligible_at=decision.next_eligible_at,
                detail={**decision.detail, "park_phase": source, **summary},
                announce=[
                    (
                        EventType.QUOTA_EXHAUSTED,
                        {
                            **decision.detail,
                            "next_eligible_at": decision.next_eligible_at,
                            "park_phase": source,
                        },
                    )
                ],
            )
        self.log.info("parked on provider quota", source=source,
                      attempt_refunded=bool(refunded))
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _quota_from_child(self, result: ChildResult) -> Any:
        ws = self.ws
        assert ws is not None
        signal_ = read_runner_signal(ws.quota_path, self.cfg.provider)
        if signal_ is None and result.exit_code == EXIT_QUOTA_EXHAUSTED:
            # The runner announced a rate limit but could not describe it.
            from .quota import QuotaSignal

            signal_ = QuotaSignal(
                provider=self.cfg.provider or "unknown",
                state=ProviderState.EXHAUSTED,
                source="runner",
                detail=f"runner exited {EXIT_QUOTA_EXHAUSTED} without a quota signal file",
            )
        return signal_

    def _park_mcp_credential(self, lapsed: list[tuple[str, str]]) -> Outcome:
        """A workflow step's per-run MCP credential lapsed between admission
        and this attach (registered-mcp-servers.md §4.10, the owner 2026-10-26).
        Its caller is online and re-stores it through
        `PUT /v1/workflows/{id}/mcp-credentials`, so this is a wait, not a
        failure: checkpoint, PARKED(CREDENTIAL_MISSING), release, exit, with
        the detail a caller watches for (`credential: "mcp"`, the code, the
        server by name). The attach does not say when the row expired, so
        `expired_at` is null here; the scheduler's park says it. The park's
        instant is `next_eligible_at`, so the credential sweep promotes it
        the moment the row is live again. Names and reason words only.
        """
        from .lifecycle import Outcome

        name, reason = lapsed[0]
        code = ("mcp_credential_missing" if reason == mcp_attach_mod.CREDENTIAL_MISSING
                else "mcp_credential_expired")
        self.log.warning(
            "a workflow's mcp credential lapsed before the agent; parking for its caller",
            servers=[n for n, _ in lapsed], code=code,
        )
        self._checkpoint("mcp-credential")
        summary = self._upload_outputs()
        now = utcnow()
        with self._metrics_after_the_record():
            self.control.park(
                reason=ParkReason.CREDENTIAL_MISSING,
                next_eligible_at=now,
                detail={
                    "credential": "mcp",
                    "code": code,
                    "server": name,
                    "expired_at": None,
                    "parked_at": now.isoformat(),
                    "mcp_servers": [
                        {"name": n,
                         "code": ("mcp_credential_missing"
                                  if r == mcp_attach_mod.CREDENTIAL_MISSING
                                  else "mcp_credential_expired"),
                         "why": r}
                        for n, r in lapsed
                    ],
                    **summary,
                },
            )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)


def _parse_iso(value: Any) -> datetime | None:
    """An ISO instant from the broker, or None.

    None on anything unparseable rather than an exception: this decides only
    WHEN a parked task wakes up, and a broker that grows a new timestamp format
    must not turn a free park into a failed attempt. The caller has a default.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
