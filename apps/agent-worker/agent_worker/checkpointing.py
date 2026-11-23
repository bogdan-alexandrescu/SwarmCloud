"""Checkpointing: the mandatory checkpoint -- its owner check, archive, start
event and record -- and the restore of the checkpoint an earlier attempt of the
same task recorded.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `CheckpointMixin`, so the methods keep their
`self` access and every `worker._checkpoint(...)` call and test still resolves.
`_KEY_SEGMENT_RE` lives here alone (issue 193); `lifecycle` no longer binds it.
`_refunded_resumes` stays in `lifecycle`, which counts resumes with it outside
this concern too, and is imported where it is used: a module-level import of
`lifecycle` from here would be circular.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from swarm_common.states import EventType

from .checkpoint import (
    ARCHIVE_NAME,
    MANIFEST_NAME,
    CheckpointRecord,
    RestoreResourceError,
    _empty_directory,
    checkpoint_prefix,
)
from .control import ARCHIVE_DIGEST_RE, recorded_checkpoint_digests
from .errors import CheckpointError, FencedError, TenantMismatchError

#: One path segment of a checkpoint's key, as a manifest's `attempt_id` and
#: `checkpoint_id` must be before a restore builds a key from them (archived issue 347). A
#: manifest is bucket data: an id carrying `/` or `..` would name a key that
#: starts with this task's prefix and leaves it.
_KEY_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


class CheckpointMixin:
    """The checkpoint methods of `lifecycle.Worker`, which inherits them."""

    def _checkpoint(self, label: str, *, push: bool = True) -> CheckpointRecord | None:
        """Mandatory checkpoint. A failure here is logged, never swallowed.

        The one thing a failed checkpoint must not do is end the attempt: the
        agent is still working, and the correct response to "this checkpoint did
        not upload" is to try again at the next interval, not to throw away the
        run that is currently succeeding.

        A FENCE IS NOT A FAILED CHECKPOINT. The attempt is over, and
        `FencedWriteRefused` is raised to say so. It is checked twice. The first
        check comes before anything is uploaded, because a stale archive in the
        task's prefix is bytes a superseded attempt should not have written
        (a restore no longer lists the prefix, archived issue 347, but the archive still
        costs storage and reads as this task's work). The second is in
        the pointer's own transaction (`ControlPlane.record_checkpoint`),
        because a fence can land during the upload.

        THE LEASE IS BEATEN THROUGH ALL OF IT (archived issue 426), not only under the
        archive (archived issue 286): the owner check, the start event and the pointer's
        transaction are Firestore calls with budgets of 30-60 s each, and they
        ran with nothing beating. The carrier push after it beats on its own.

        ONLY THE PERIODIC CHECKPOINT IS EVER SKIPPED (archived issue 637), and that is
        decided by its label here, so no other caller can ask for it: when the
        tree is as the last checkpoint left it, nothing is announced or
        written, None is returned, and `checkpoints.last_unchanged` says so.
        The final, park, cancellation, interruption, outage and child-await
        checkpoints are written whatever changed (invariant 8).

        `push=False` takes the checkpoint without the carrier push. Only the
        SIGTERM exits ask for it (archived issue 453): the forge is not called inside the
        grace window that the park or terminal write has to land in.
        """
        if self.ws is None:
            return None
        with self._heartbeat_meanwhile(
            f"checkpoint ({label})", progress=self.checkpoints.progress.reading
        ):
            record = self._write_checkpoint(label, only_if_changed=label == "periodic")
        if record is None:
            return None
        self._last_checkpoint = record
        if push:
            self._push_carrier_branch(label)
        return record

    def _write_checkpoint(
        self, label: str, *, only_if_changed: bool = False
    ) -> CheckpointRecord | None:
        """`_checkpoint`'s owner check, archive and record, under its heartbeat."""
        assert self.ws is not None  # checked by `_checkpoint`
        try:
            if only_if_changed:
                # The owner check and the start event run once the walk has
                # found a change, before the first byte is uploaded: an
                # unchanged tick announces nothing.
                record = self.checkpoints.create_if_changed(
                    self.ws, label=label, before_upload=lambda: self._announce_checkpoint(label)
                )
                if record is None:
                    # Still the owner check: the periodic tick is one of the
                    # places a mid-run fence is met (`_exit_fenced_mid_run`),
                    # and a tree that stopped changing must not stop that.
                    self._confirm_owner(label)
                    return None
            else:
                self._announce_checkpoint(label)
                record = self.checkpoints.create(self.ws, label=label)
        except (FencedError, TenantMismatchError):
            raise
        except CheckpointError as exc:
            self.log.error("CHECKPOINT FAILED", label=label, error=str(exc))
            return None
        except Exception as exc:
            self.log.exception("checkpoint failed unexpectedly", exc, label=label)
            return None
        return self._record_checkpoint(label, record)

    def _announce_checkpoint(self, label: str) -> None:
        """The owner check and the start event every written checkpoint is preceded by."""
        self._confirm_owner(label)
        try:
            # The announcement is an audit record, written to Firestore; the
            # archive goes to the bucket. An event that cannot be written must
            # not cost the checkpoint, and during an outage (archived issue 70) it is the
            # checkpoint that is needed.
            self.control.emit(EventType.CHECKPOINT_STARTED, {"label": label})
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:  # noqa: BLE001 -- logged; the checkpoint is taken anyway
            self.log.warning(
                "could not record the checkpoint's start; taking it anyway",
                label=label,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _confirm_owner(self, label: str) -> None:
        """Raise the fence if this attempt no longer owns its task; a failed read is not one."""
        try:
            self.control.ensure_owner(write=f"checkpoint ({label})")
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:  # noqa: BLE001 -- logged; the pointer write checks again
            # A read that could not be made is not a fence. Carry on, as a
            # checkpoint always has. The pointer's own transaction is still the
            # authority, and it re-checks.
            self.log.warning(
                "could not confirm this attempt still owns its task before "
                "checkpointing; the pointer write will check again",
                label=label,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _record_checkpoint(self, label: str, record: CheckpointRecord) -> CheckpointRecord | None:
        """Write a checkpoint's record: the attempt's list and digest, the pointer, the event."""
        # THE RECORD'S ERRORS STAY HERE (archived issue 70). The archive is in the bucket; the
        # record -- the attempt's list and digest, the task's pointer, the
        # event -- is Firestore's, under its budget. A record that could not
        # be written leaves a checkpoint no restore trusts (archived issue 347), which is
        # what a failed checkpoint is, and the next interval tries again. It
        # used to raise into the crash handler, which FAILED the task. A fence
        # met in the pointer's transaction is still raised: that is not a
        # failed checkpoint, the attempt is over.
        try:
            self.control.record_checkpoint(
                checkpoint_id=record.checkpoint_id,
                uri=record.uri,
                size_bytes=record.archive_bytes,
                seq=record.seq,
                archive_sha256=record.archive_sha256,
            )
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:  # noqa: BLE001 -- logged; a failed checkpoint, retried next interval
            self.log.error(
                "CHECKPOINT NOT RECORDED: the archive was uploaded and its record "
                "could not be written, so no restore will use it",
                label=label,
                checkpoint_id=record.checkpoint_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        return record

    def _restore_checkpoint(self, task: dict[str, Any]) -> None:
        """Restore the checkpoint this task's own earlier attempt recorded, or nothing.

        ONLY A RECORDED CHECKPOINT, AND NEVER ON A FIRST ATTEMPT (archived issue 347, owner
        decision 2026-09-29). This used to fall back to the newest manifest
        under the task's prefix when the pointer resolved to nothing, and to
        do so on attempt 1 as well. The tenant's worker account can write
        anywhere under `tenants/<tenant>/`, so any agent of the tenant could
        plant a checkpoint under another task's prefix -- an implement step
        under its parked review step's -- and HOME is `work/`, whose
        `.claude/` travels in every checkpoint: the planter's settings, and
        the hooks in them, arrived in the next step, where a hook runs code
        without persuading any model. Now:

        * a first attempt restores nothing, whatever is under the prefix;
        * a retry restores only what `_recorded_checkpoint` accepts;
        * nothing lists the prefix to choose a checkpoint.

        Every refusal starts the attempt from an empty workspace, which is
        what a first attempt does anyway. The restore's own checks --
        ownership, digest, the member filter, the escaping-link skip, the
        size caps -- still apply to whatever is accepted, and a
        `CheckpointError` from them is a refusal like the rest: an empty
        workspace, not a failed attempt (archived issue 346).
        """
        ws = self.ws
        assert ws is not None
        if self.cfg.profile.never_restore_checkpoint:
            # CONTRACT REQUEST 36 (merge-step.md §10 item 4a): a profile that
            # sets the flag restores NOTHING, on any attempt, not only the
            # first -- `claude-code-review` judges the head it checks out, and
            # a workspace an earlier attempt (or a planter) left behind is
            # not that head. Before `_recorded_checkpoint`, so not even the
            # pointer is followed. Checkpointing itself stays on (invariant
            # 8): only the restore is skipped.
            self.log.info(
                "no checkpoint is restored; starting from an empty workspace",
                reason="the runner profile never restores a checkpoint",
                runner_profile=self.cfg.runner_profile,
            )
            return
        record = self._recorded_checkpoint(task)
        if record is None:
            return
        try:
            files = self.checkpoints.restore(record, ws)
        except RestoreResourceError:
            # Out of local room, not a refusal: the attempt fails and a retry
            # resumes this same checkpoint (see `RestoreResourceError`).
            raise
        except CheckpointError as exc:
            # A REFUSED RESTORE IS A CLEAN START, NOT A FAILED ATTEMPT (archived issue 346).
            # The restore's own checks -- the archive's bytes against the
            # recorded digest above all, but also ownership, the member filter
            # and the size caps -- refuse what `_recorded_checkpoint` could
            # not see: an archive rewritten under an unchanged manifest. That
            # used to fail the attempt and burn a retry, where every other
            # refusal starts from an empty workspace; the restore leaves
            # `work/` empty on a refusal, and this makes sure of it. Not a
            # download's object-store error, nor an extraction that ran out of
            # local room (`RestoreResourceError`, above): those say nothing
            # against the checkpoint, so they still fail the attempt, and a
            # retry resumes it.
            _empty_directory(ws.work)
            self.log.error(
                "refusing the recorded checkpoint: the restore's own checks failed",
                checkpoint_id=record.checkpoint_id,
                from_attempt=record.attempt_id,
                error=str(exc),
            )
            self.log.info(
                "no checkpoint is restored; starting from an empty workspace",
                reason="the recorded checkpoint failed the restore's own checks",
            )
            return
        self._restored_from = record
        self._read_continuation_note()
        self.control.emit(
            EventType.CHECKPOINT_RESTORED,
            {
                "checkpoint_id": record.checkpoint_id,
                "from_attempt": record.attempt_id,
                "files": files,
                "bytes": record.archive_bytes,
            },
        )

    def _recorded_checkpoint(self, task: dict[str, Any]) -> CheckpointRecord | None:
        """The checkpoint an earlier attempt of THIS task recorded, or None.

        Accepted only when every one of these holds:

        1. the task has had an earlier attempt: its `attempt_count`, which
           admission increments in the lease's own transaction, is above 1;
        2. the task's `latest_checkpoint` names one, and it resolves inside
           this task's own prefix to a manifest naming this tenant and task
           (`CheckpointManager.find_by_uri`, which refuses anything outside
           the prefix);
        3. the pointer, the manifest and the archive all lie where the
           manifest's own ids put them, so the attempt and checkpoint it
           names are the ones at that path;
        4. it is not this attempt's own -- this attempt has recorded nothing;
        5. the attempt document of the attempt that wrote it exists, is this
           tenant's and this task's, lists that checkpoint id, and records
           the archive digest the manifest carries -- as
           `ControlPlane.record_checkpoint` writes both, before it moves the
           pointer. The restore then checks the archive's bytes against that
           digest, so bytes rewritten in the bucket after the attempt
           recorded them are refused even when manifest and archive were
           rewritten together.

        WHAT THIS DOES NOT STOP. Firestore has no document-level IAM
        (docs/multi-tenancy.md), so an agent of the tenant that knows this
        task's id can write `attempt_count`, `latest_checkpoint` and an
        attempt document of an id it chose, and satisfy every check above.
        What it stops is every first attempt, and on a retry the planter
        that has only the bucket -- the issue's reproduction -- whether it
        adds a checkpoint beside the recorded one or rewrites the recorded
        one. The complete fix needs a record the tenant cannot write, which
        is the signed step specs' work (archived issue 342).
        """
        from .lifecycle import _refunded_resumes

        refuse = functools.partial(
            self.log.info, "no checkpoint is restored; starting from an empty workspace"
        )
        count = task.get("attempt_count")
        # A refunded child await (child tasks, §3.3 step 5) takes one off
        # `attempt_count`, so the attempt after it can read 1 and still be a
        # resume. Counted back here; a tenant that can write the counter can
        # write `attempt_count` itself, so this adds no reach (see below).
        #
        # A refunded HAND-OFF park (live-browser §4.3) takes one off the same
        # way, and is counted back the same way (RESUME-PROGRESS, QA12-14).
        # It was not: MEASURED 2026-10-13 on task_4defa61c5eea4a5c87f5, the
        # attempt after a hand-off park read `attempt_count` 1, was taken for
        # the task's first, restored nothing, and its agent started the task
        # over -- asking again for the hand-off already done.
        #
        # A refunded QUOTA park takes one off the same way (archived issue 2650): the
        # attempt after a quota park read 1, restored nothing, and the file
        # its agent wrote before the park never reached the attempt that
        # succeeded (the workflow group's carry check).
        resumes = _refunded_resumes(task)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count + resumes <= 1
        ):
            refuse(reason="first attempt of this task", attempt_count=count)
            return None
        pointer = task.get("latest_checkpoint")
        if not isinstance(pointer, str) or not pointer:
            refuse(reason="no earlier attempt of this task recorded a checkpoint")
            return None
        record = self.checkpoints.find_by_uri(pointer)
        if record is None:
            refuse(reason="the recorded checkpoint does not resolve to one of this task's")
            return None
        ids = (record.attempt_id, record.checkpoint_id)
        if not all(isinstance(i, str) and _KEY_SEGMENT_RE.fullmatch(i) for i in ids):
            self.log.error(
                "refusing a checkpoint whose ids are not single key segments",
                attempt_id=str(record.attempt_id)[:200],
                checkpoint_id=str(record.checkpoint_id)[:200],
            )
            refuse(reason="the checkpoint's ids are not single key segments")
            return None
        expected = checkpoint_prefix(
            tenant_id=self.cfg.tenant_id,
            task_id=self.cfg.task_id,
            attempt_id=record.attempt_id,
            checkpoint_id=record.checkpoint_id,
        )
        named = pointer.rstrip("/")
        if (
            record.manifest_key != f"{expected}/{MANIFEST_NAME}"
            or record.archive_key != f"{expected}/{ARCHIVE_NAME}"
            or not (named == expected or named.endswith("/" + expected))
        ):
            self.log.error(
                "refusing a checkpoint that is not where its own ids put it",
                pointer=pointer,
                manifest_key=record.manifest_key,
                archive_key=record.archive_key,
                expected_prefix=expected,
            )
            refuse(reason="the checkpoint's ids do not match its path")
            return None
        if record.attempt_id == self.cfg.attempt_id:
            refuse(reason="the pointer names this attempt, which has recorded nothing")
            return None
        # A DIGEST IS A DIGEST BEFORE IT IS COMPARED (archived issue 346). A manifest is
        # bucket data, and one carrying `archive_sha256: null` matched an
        # attempt document with no digest for it: None == None.
        if not isinstance(record.archive_sha256, str) or not ARCHIVE_DIGEST_RE.fullmatch(
            record.archive_sha256
        ):
            self.log.error(
                "refusing a checkpoint whose manifest carries no archive digest",
                attempt_id=record.attempt_id,
                checkpoint_id=record.checkpoint_id,
            )
            refuse(reason="the checkpoint's manifest carries no archive digest")
            return None
        try:
            attempt = self.control.fetch_attempt(record.attempt_id)
        except TenantMismatchError as exc:
            self.log.error(
                "refusing a checkpoint whose attempt document is another tenant's",
                attempt_id=record.attempt_id,
                error=str(exc),
            )
            refuse(reason="the recording attempt is another tenant's")
            return None
        listed = attempt.get("checkpoints") if attempt else None
        # `Attempt.checkpoint_sha256` in its typed shape (contract request 51).
        # Empty when the document has none -- written before archived pull request 348 -- which is
        # "no digest recorded", so the retry starts clean.
        digests = recorded_checkpoint_digests(attempt)
        if (
            attempt is None
            or attempt.get("task_id") != self.cfg.task_id
            or not isinstance(listed, list)
            or record.checkpoint_id not in listed
            or digests.get(record.checkpoint_id) != record.archive_sha256
        ):
            self.log.error(
                "refusing a checkpoint no earlier attempt of this task recorded",
                attempt_id=record.attempt_id,
                checkpoint_id=record.checkpoint_id,
                attempt_document=attempt is not None,
                digest_recorded=record.checkpoint_id in digests,
            )
            refuse(reason="no attempt document of this task lists the checkpoint")
            return None
        return record
