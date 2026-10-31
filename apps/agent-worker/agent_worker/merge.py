"""The `merge` worker action: squash-merge the pull request a workflow opened (#295).

A step that runs no agent (`WorkerAction.MERGE`). Owner decisions of
2026-10-04 (recorded on #295; contract request 47) replaced the design's
GitHub-App merge with this one:

  * it uses the tenant's EXISTING `-git` token, read at merge time only;
  * it works in ANY repository a workflow runs on -- the workflow's own
    `repository_url`, which the signed spec covers; owner and repo are parsed
    from it, and nothing here names SwarmCloud's repository;
  * after the merge it closes every issue the pull request closes that is
    still open, because the App path did not (#569);
  * it is an opt-in final step, appended by swarm-api when the platform
    default or the job says so (`swarm_api.validation.plan_merge`).

WHICH PULL REQUEST. The signed `dispatch.merge_target` block names the TASK
that opened it (the integrator, or the one `direct-pr` step) and, when the
workflow has one, the review whose verdict file this step stages. The pull
request's number and pushed head are that task's recorded result: claims,
written under the tenant identity, so the live pull request is then checked
to be that task's own branch, from no fork, at exactly that head. Nothing is
read from a pointer the signed spec does not name.

A PULL REQUEST NO WORKFLOW OPENED (#352, owner decision 2026-10-07): a
`merge_pr` workflow's signed `merge_target` names no task but the pull
request's `number` and the `head_sha` the caller named, which swarm-api
checked was its head at submission (`continuation.resolve_merge_pr`). The
sha is pinned exactly as a pushed head is, and every check after it is the
same gate -- open, no fork, the base, the head, every required check green
there -- except the branch name, which no SwarmCloud task chose.

THE ORDER IS #219's (docs/merge-step.md §2.2): the reap and every check that
needs no credential first; then the token; then the forge's facts; then the
merge, pinned to the head; then the record and the issues. A refusal ends the
step FAILED with MERGE_REFUSED and its code in `result_summary.merge.refusal`.
Three facts are waits rather than verdicts -- a required check still pending,
no check reported at all on a branch that requires none, and GitHub not having
computed mergeability -- and PARK the step on CI_PENDING (lane MS2,
docs/merge-step.md "Revised 2026-10-06" §1): no lease, no pool count, the
attempt refunded up to MERGE_CI_MAX_WAKES (invariants 1, 3 and 4). swarm-api's
wake tick marks it once the checks settle, the scheduler promotes it on that
mark or at the fallback instant, and the next attempt reads every fact again.
The merge call itself is never resent: a merge whose answer was lost ends
MERGE_FAILED, never retried blindly.

A BRANCH THAT IS BEHIND a base requiring an up-to-date branch is updated by
GitHub, not merged (lane MS3, the owner's 2026-10-06 decision): behind the
same fencing recheck as the merge, `update-branch` with `expected_head_sha`,
then a CI_PENDING park at the new head, at most MERGE_MAX_BRANCH_UPDATES
times. The head is never read from the task document: every attempt walks
first parents from GitHub's live head back to the head the workflow pushed,
and accepts only GitHub's own merges of commits already on the base. MS3 also
splits `from_fork`, `base_not_default`, `protection_refused`,
`merge_conflict`, `checks_timeout` and `behind_too_often` out of the codes
they were folded into, and gives `_close_issues` the close script's page rule.

A BASE THAT MERGES ONLY THROUGH A MERGE QUEUE is enqueued, not merged (lane
C3H, the owner's 2026-10-06 decision for SwarmCloud's own main). GitHub
answers the REST merge 405 "Changes must be made through the merge queue";
then, behind the same fencing recheck, the step enqueues through GraphQL
`enqueuePullRequest` with the same token, pinned to the head the checks are
green at, records the entry in `merge_queued`, and parks CI_PENDING with the
code MERGE_QUEUED, as every MS2 wait does. swarm-api's wake tick reads the
queue for that park; the next attempt finds the pull request merged
(success, with the record and the issues), still queued (parked again), or
removed (`merge_dequeued`, with GitHub's reason). With no queue rule nothing
here runs: the 405 is the only way in.

A PULL REQUEST THAT CONFLICTS WITH ITS BASE is never waited on for checks
(#1076): GitHub runs none on it. Mergeability is read before the checks, and
a conflict parks for a conflict fix round -- a claude-code continuation of the
branch that merges the base in, submitted by swarm-api's wake tick as a CI-fix
round is -- at most MERGE_CONFLICT_ROUNDS_MAX of them, then `merge_conflict`.
A round's pushed head, one the PR-side CI fixer recorded for this pull
request (#1073, `_ci_fixer_heads`), and one swarm-api's tick recorded for
this pull request from a fix round of ANY workflow (epic #1334,
`_recorded_fix_heads`), is accepted as a fix round's head is: from the
platform's records and signed specs, never from the commit.

THE GITHUB SPECIFICS ARE BEHIND `ForgeMerger`, with `GitHubMerger` its only
implementation. swarm-api refuses a repository whose host has no merger at
SUBMISSION (`validation.MERGE_FORGE_HOSTS`, held equal to
`MERGEABLE_HOSTS` by tests/unit/worker/test_merge_action.py), so the
`forge_unsupported` refusal below is a second line, not the first.

THE OWNER QUESTIONS THE REVIEW SAID HOLD IT (#2070, owner decision
2026-10-23, option (c)). The review is given every earlier step's owner
questions (`owner_questions`) and names, in `verdict.needs_owner`, the
unanswered ones that must hold the merge. Before any credential, this step
re-reads each one's task -- a step of THIS workflow, through the same
tenant-gated upstream read -- and counts it answered when that task's
`metadata.owner_answers` holds its index, so a question answered after the
review counts as answered. While any is still open, or an entry cannot be
read, it parks for a person (`owner_wait`, `handoff.park_human_required`),
naming each question's step and index: no lease, nothing counted, the
attempt refunded within the hand-off's bound. The answer that clears the
hold brings it back (owner decision 2026-10-23, "Resume on answer, no
expiry"; QUESTION-WAKE, PR 2151): answering through `swarm_task_answer`
writes `owner_answers` on the ASKING task, and in the same transaction
swarm-api's answer route (`swarm_api.agent_questions.answer_question`)
returns this park to READY, reason `owner_questions_answered`, once every
question its `owner_questions` hold names is answered. An answer while
another named question is still open leaves it parked, and a hold the route
cannot read -- `unreadable` entries, a malformed or out-of-workflow entry --
waits for a person's `human-ready`, as before. A review re-running wakes
nothing. Either way the next attempt reads every answer again. The
scheduler's human-wait expiry (`_expire_human_waits`) spares this park, as
it spares a `question_wait` or `release_wait` one, so it never dead-letters
at `human_park_max_seconds`. Its pull request merged or closed outside the
workflow ends it instead (MERGE-OUTSIDE-ENDS, #2441): swarm-api's wake tick
marks the hold, the scheduler wakes it, and `_ended_outside` reads the pull
request before any question and ends the step SUCCEEDED or CANCELLED. The review's `open_questions`, the ones it
judged need not hold, never stop the merge; those still unanswered are
listed, in the asking task's own words, in the comment that records the
merge -- so a merge made by `auto-merge.yml` rather than this step lists
none.

A NOT_YET VERDICT THAT NAMES `needs_owner` IS HELD TOO (#2764). A review
may record a finding that waits only on the owner's answer as NOT_YET; the
step refused it `verdict_not_merge` before the hold was read, so the lane
died instead of parking. Now a NOT_YET with `needs_owner` (or unreadable
entries) goes through the same `_ended_outside`, hold and pick-fix round as
a MERGE verdict, and only once no question is open and no pick is left to
build does it refuse `verdict_not_merge`: a NOT_YET is never merged without
a review of the head, which a re-review gives. A NOT_YET with no
`needs_owner` refuses at once, exactly as before.

ANSWERED IS NOT ENOUGH: THE PICK MUST BE WHAT THE BRANCH BUILT
(ANSWER-CONTENT-CHECK, part of #2441; owner decision 2026-10-26). Once every
`needs_owner` question is answered, the step reads the validated
`questions.json` of every step of its workflow it depends on, and of every
fix round appended to it since (`ci-fix:<n>`, `conflict-fix:<n>`,
`pick-fix:<n>`, which the merge depends on none of: PR 2878 merged past a
ci-fix:1 question's answer, #2898), and compares
each ANSWERED question's `choice` with its `built`, the option label the
asking agent said its branch implements -- the `needs_owner` ones and every
other answered one, such as a question the owner answered before the review
ran, which the review lists nowhere (PICK-FIX-ROUND, BGD 2026-10-26). A
different option, or free text that names none, is an unbuilt pick
(`unbuilt_picks`), found before any credential. `built: null` (the answer
changes nothing the branch builds) passes, and so does a question with no
`built` at all, written before this check, so a lane parked on its answers is
not stranded. A round's blocking question still unanswered holds the merge
as a `needs_owner` one does. On 2026-10-26 three lanes (VQ-CAPACITY, VQ-CONFIG-B,
BROWSER-POOL-100) would have merged the opposite of the owner's pick without
it.

AN UNBUILT PICK IS A PICK-FIX ROUND'S TO BUILD (PICK-FIX-ROUND, part of
#2441; BGD 2026-10-26), not the operator's. The step parks CI_PENDING on
`owner_pick_fix_pending`, one `pending` line per pick; swarm-api's wake tick
reads that park as it reads a CI or conflict park and submits a claude-code
round on the pull request's branch whose brief names each pick. The round
builds it, pushes to the same pull request and writes `picks-built.json`;
the merge then runs again. A round counts only for the picks its report says
it built, at a head it pushed -- or, pushing nothing, for the picks it was
sent for that its report says the branch already builds (#2858); one that did
not succeed, or left a pick it was sent for unbuilt, refuses
`owner_pick_not_built`, and at
most MERGE_PICK_ROUNDS_MAX are run, so the step never loops (`_pick_fix`).

THE RULES `auto-merge.yml` ALSO STATES are the module-level functions
`title_is_placeholder`, `other_check_blocks` and `required_check_state`, so
tests/unit/scripts/test_auto_merge_workflow.py holds them to the gate's shell
from one table of cases (§8) until that workflow is retired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, NamedTuple, Protocol
from urllib.parse import quote

from swarm_common.models import EndCause, utcnow
from swarm_common.states import TERMINAL_STATES, TaskState

from . import forge as forge_mod
from . import mergeslot
from . import mergetrain
from . import owner_questions as owner_questions_mod
from . import questions as questions_mod
from . import verdict as verdict_mod
from .errors import ExitCode, FencedError, InputUnavailable
from .expected_outputs import NO_CHANGE_SUMMARY_KEY, left_nothing
from .post_verdict import (
    ActionContext,
    ActionOutcome,
    _outcome,
    _task_id,
    cancelled,
    forge_retry,
    pull_request_belongs,
    pull_request_number,
    refusal,
    unavailable,
)
from .control import MERGE_WAIT_METADATA_KEY
from .specverify import UpstreamSpecUnverified

#: The signed dispatch block naming what this step merges. swarm-api's
#: `validation.MERGE_TARGET_FIELD`, restated because the worker image carries no
#: control plane; tests/unit/worker/test_merge_action.py holds the two equal.
MERGE_TARGET_FIELD = "merge_target"

#: The code an owner-question hold parks with (#2070).
OWNER_QUESTIONS_OPEN = "owner_questions_open"
#: The code a merge refuses with when the owner picked an option the asking
#: branch did not build (ANSWER-CONTENT-CHECK, owner decision 2026-10-26), the
#: `result_summary` key naming each such pick, and the code for a counted
#: `questions.json` the check could not read.
OWNER_PICK_NOT_BUILT = "owner_pick_not_built"
UNBUILT_PICKS_KEY = "unbuilt_picks"
OWNER_QUESTIONS_UNREADABLE = "owner_questions_unreadable"
#: A pick the branch did not build is a pick-fix round's to build
#: (PICK-FIX-ROUND, part of #2441; BGD 2026-10-26), not a refusal: the step
#: parks CI_PENDING on one of these codes, which swarm-api's wake tick reads
#: as it reads a CI or a conflict park. PENDING asks for the next round;
#: RUNNING waits on the one claimed. The rounds are `merge_fix.pick_rounds`,
#: beside the CI and conflict rounds and counted apart from them, each
#: `{round, head, claimed_at, pull_head, picks}` then `workflow_id` and
#: `task_id`, or `error`. TWO at most, the conflict rounds' bound: one for the
#: answers that woke the merge, one for a question answered after it; a pick a
#: round was sent for and left unbuilt is never sent again. Each `pending`
#: line of the park names one pick and starts with PICK_LINE_PREFIX, which is
#: how the tick finds the picks a round is sent for. Held equal to
#: `swarm_api.mergewake` by tests/unit/worker/test_merge_pick_fix_round.py.
OWNER_PICK_FIX_PENDING = "owner_pick_fix_pending"
OWNER_PICK_FIX_RUNNING = "owner_pick_fix_running"
PICK_FIX_CODES = frozenset({OWNER_PICK_FIX_PENDING, OWNER_PICK_FIX_RUNNING})
PICK_ROUNDS_KEY = "pick_rounds"
MERGE_PICK_ROUNDS_MAX = 2
PICK_LINE_PREFIX = "pick "
#: The most characters of a question's text a `pending` line carries.
PICK_LINE_QUESTION_MAX = 400

#: swarm-api's wake tick's record of an owner-question hold
#: (`swarm_api.mergewake.OWNER_HOLD_KEY`), inside `metadata.merge_wait`, and
#: the two wake reasons it writes when the hold's pull request was merged or
#: closed outside the workflow (MERGE-OUTSIDE-ENDS, part of #2441). Held equal
#: by tests/unit/control_plane/test_merge_outside_ends.py.
OWNER_HOLD_KEY = "owner_hold"
PULL_REQUEST_MERGED = "pull_request_merged"
PULL_REQUEST_CLOSED = "pull_request_closed"
OUTSIDE_WAKE_REASONS = frozenset({PULL_REQUEST_MERGED, PULL_REQUEST_CLOSED})
#: `metadata.owner_answers`, swarm-api's record of each answer
#: (`questions.OWNER_ANSWERS_KEY`).
OWNER_ANSWERS_KEY = questions_mod.OWNER_ANSWERS_KEY

#: The worker's own placeholder title, compared case- and leading-whitespace-
#: insensitively: auto-merge.yml gate 1's rule.
PLACEHOLDER_TITLE_PREFIX = "[swarm] task_"
#: The worker's `pr-title.txt` rule (#214): the retired `[swarm] task_...`
#: shape ANYWHERE in the title, any spacing, any case. `lifecycle.
#: _RETIRED_TITLE_RE`, restated because lifecycle imports this module;
#: tests/unit/worker/test_merge_action.py holds the two equal.
RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)

#: §5.2 3: what counts as green for a REQUIRED check. The owner's rule:
#: success or skipped, nothing else -- `neutral` included.
REQUIRED_GREEN = frozenset({"success", "skipped"})
#: The conclusion of a run someone stopped: pending, never failed (#1778,
#: `cancelled_run`).
CANCELLED_CONCLUSION = "cancelled"
#: The one conclusion a check run keeps inside an Actions run that was
#: cancelled (`cancelled_by_its_workflow`, #2582): its job ran and passed.
#: `skipped` is not kept -- a job a cancelled run never ran may say it.
PASSED_IN_ITS_RUN = "success"
#: What any OTHER check may conclude without holding the merge, and what
#: every check must conclude on a base branch that requires none:
#: auto-merge.yml gate 5's own tolerance plus nothing.
OTHER_TOLERATED = frozenset({"success", "skipped", "neutral"})

#: `mergeable: null` is read again at most twice, 2 s apart (§5.1): a bounded
#: pause in seconds, not a provider wait (invariant 4).
MERGEABLE_REREADS = 2
MERGEABLE_REREAD_SECONDS = 2.0

#: How many CI_PENDING parks give their attempt back (the await park's rule,
#: `control.ControlPlane.park_ci_pending`): waiting is not failing, so a slow
#: CI does not use up the step's attempts. Past the bound a wake counts like
#: any attempt, so a pull request whose CI never settles still ends at
#: `max_attempts`. 60, the design's figure: with swarm-api's wake tick marking
#: a park as soon as its checks settle, a park lasts about one CI run, and the
#: fallback below alone gives 60 x 15 min = 15 h of waiting, past the design's
#: 6 h `MERGE_CI_MAX_SECONDS`.
MERGE_CI_MAX_WAKES = 60

#: When the scheduler wakes a CI_PENDING park nobody marked: the park instant
#: plus this, as `next_eligible_at`. A dead wake tick, or a token swarm-api
#: cannot use, then costs a wake every 15 minutes -- one lease of a few
#: seconds -- instead of stranding the merge. Not shorter, the design's 900:
#: each fallback wake is a Job execution and a cold start, which the tick
#: exists to avoid.
MERGE_CI_FALLBACK_SECONDS = 900

#: How many times the step updates its branch WHILE HOLDING its repository's
#: merge slot (`mergeslot`), beyond the one catch-up update its first reading
#: after taking the slot may need, before it refuses `behind_too_often`.
#: Until 2026-10-10 this counted every update and was 3: with 16 merge steps
#: racing one `main`, every sibling's merge put the rest behind, and 7 of
#: them refused (merge race, #295). Holding the slot, no sibling merges, so
#: what is counted is only a base moved by someone outside the slot -- a
#: person's push, a merge made by hand -- and is normally zero. 5, raised
#: from 3 because the count is no longer a measure of the platform's own
#: traffic: a burst of hand merges during one CI run should not fail a
#: step, while five of them, each costing a CI run (10-20 min here) with the
#: slot held and the queue behind it waiting, is a question for a person.
MERGE_MAX_BRANCH_UPDATES = 5

#: How many of GitHub's base merges the first-parent walk accepts on top of
#: the pushed head, in all: the updates counted above, plus a catch-up each
#: time the step took the slot again (after a CI-fix round's release, or a
#: lease that ran out), plus updates an earlier attempt made before this
#: repository had a slot. Also a cap: a step at it refuses
#: `behind_too_often` rather than make an update the walk would not accept.
#: 12 = MERGE_MAX_BRANCH_UPDATES + 7 re-takes, far past any path measured.
MERGE_MAX_HEAD_UPDATES = 12

#: How long, from `merge_wait.first_parked_at`, the step waits for CI before
#: it refuses `checks_timeout`, naming what is still pending. 6 h, the
#: design's figure: about twenty of this repository's CI runs, so only a check
#: that never reports -- a required check no workflow produces, a stuck
#: runner -- reaches it.
MERGE_CI_MAX_SECONDS = 6 * 3600

#: How long a reading whose ONLY problem is cancelled runs -- nothing failed,
#: nothing still running or missing (`cancelled_since`) -- waits for its
#: re-run before the step refuses `checks_cancelled`. BGD set it on
#: 2026-10-27 (#1778, "refuse after a shorter bound", 60 min): a cancelled
#: run is pending, not failed, but nobody re-running it is not CI that has
#: yet to settle, so it does not get the 6 h of `MERGE_CI_MAX_SECONDS`. A
#: reading with anything still running keeps that bound.
MERGE_CI_CANCELLED_MAX_SECONDS = 60 * 60

#: The merge task's record of its CI-fix rounds, written only by swarm-api's
#: wake tick (`swarm_api.mergewake.MERGE_FIX_METADATA_KEY`, lane MS7):
#: `rounds`, oldest first, each `{round, head, claimed_at}` plus the round's
#: `workflow_id`/`task_id` once submitted, or an `error` when it was not.
#: Restated because the worker image carries no control plane;
#: tests/unit/worker/test_merge_fix_round.py holds the two equal. The
#: document is tenant-writable: a round's head is accepted only from its
#: task's SIGNED spec (`_fix_round_heads`), and a forged entry can at most
#: make the step wait, which `MERGE_CI_MAX_SECONDS` bounds.
MERGE_FIX_METADATA_KEY = "merge_fix"
#: The workflow's `metadata.merge_fix_rounds`, copied onto the merge task
#: (`swarm_api.validation.MERGE_FIX_ROUNDS_KEY`), and its ceiling
#: (`MERGE_FIX_ROUNDS_MAX`, the API's): a forged count is read under it.
MERGE_FIX_ROUNDS_KEY = "merge_fix_rounds"
MERGE_FIX_ROUNDS_MAX = 5
#: The tick's free re-run of a red head's failing Actions jobs, once per head
#: and ahead of any round, even when none were asked for (SPEED-D2, #2257):
#: `merge_fix.reruns`, each `{round, head, claimed_at}` then `jobs` and
#: `requested_at`, or `error` when GitHub refused. Written only by swarm-api's
#: wake tick (`swarm_api.mergewake.RERUNS_KEY`/`RERUN_SETTLE_SECONDS`, held
#: equal by tests/unit/worker/test_merge_fix_round.py). For the settle window
#: after `requested_at` the failed check runs this step reads are the ones
#: GitHub has not replaced yet, not the re-run's answer, so a wake inside it
#: (the MERGE_CI_FALLBACK_SECONDS fallback, any other) parks rather than
#: refusing on them: task_076d1ae89d014e4688ce refused `checks_failed` on one
#: flaky shard a re-run would have passed (PR #2229).
RERUNS_KEY = "reruns"
RERUN_SETTLE_SECONDS = 600

#: The park codes of a red reading a CI-fix round may still fix: rounds are
#: left and the tick has not claimed the next one yet, or one is running at
#: this head. Never `checks_failed` while either holds.
CI_FIX_PENDING = "ci_fix_pending"
CI_FIX_RUNNING = "ci_fix_running"
#: The park code of a red reading whose failing jobs the tick re-ran at this
#: head less than RERUN_SETTLE_SECONDS ago: the re-run has not answered yet.
CI_RERUN_PENDING = "ci_rerun_pending"
#: The park code of a red reading with no CI-fix round left to ask for and no
#: re-run recorded at this head (#2351, BGD 2026-10-27): swarm-api's wake
#: tick re-runs the failing Actions jobs only for a merge parked CI_PENDING
#: (`mergewake._rerun_first`), so refusing on the FIRST red reading meant the
#: free re-run never ran -- a transient red (trivy failing to download its
#: DB) refused a whole lane. Parked ONCE per head (`_rerun_due`): a red
#: re-run, a re-run GitHub refused, or a second red reading with nothing
#: re-run (the tick found no Actions job to re-run) refuses `checks_failed`.
#: A CI-fix round that ended at the red head with `no_change` reaches it too
#: (#2684): task_d2698b60 diagnosed a flaky shard on PR #2603 and asked for
#: a re-run, and the step refused without one (task_69eb7362).
CI_RERUN_DUE = "ci_rerun_due"
#: The park code of a red reading no CI-fix round can fix (#2046): the
#: publish that wrote this head -- the opener's, or the CI-fix round's that
#: ended at it -- touched `.github/workflows/` and swarm-api refused the push
#: WORKFLOW_SCOPE_MISSING (the repository's registration does not have
#: `workflow_files` switched on), so the workflow lines are in
#: WORKFLOW_PATCH_FILE in that attempt's artifacts and the branch does not
#: have them. CI is red by construction, and a round can only make the same
#: edit and be refused again (task_58815d23, task_d91ca0c9: `checks_failed`
#: after the round, at the unchanged head). The step parks for the operator
#: instead, and swarm-api's tick waits for the head to move without reading
#: the checks or asking for a round (`swarm_api.mergewake.
#: WORKFLOW_PATCH_PENDING`, held equal by
#: tests/unit/control_plane/test_mergewake_workflow_refused.py).
WORKFLOW_PATCH_PENDING = "workflow_patch_pending"
#: The broker's refusal code, and the patch's name
#: (`agent_worker.lifecycle.WORKFLOW_PATCH_FILE`), restated: this module does
#: not import the lifecycle.
WORKFLOW_SCOPE_MISSING = "workflow_scope_missing"
WORKFLOW_PATCH_FILE = "workflow-lines.patch"
#: The most refused paths a park's message names.
WORKFLOW_PATHS_NAMED = 10

#: A CONFLICTING pull request is never waited on for checks: GitHub runs none
#: on it (#1076, measured on PR 1028: `checks_timeout` after 366 min while
#: GitHub said CONFLICTING and "no checks reported"). It is handed to a
#: conflict fix round instead -- a claude-code continuation on the pull
#: request's branch that merges the base in, submitted by swarm-api's wake
#: tick exactly as a CI-fix round is -- and parks on one of these two codes
#: meanwhile. Its rounds are `merge_fix.conflict_rounds`, beside the CI
#: rounds and counted apart from them
#: (`swarm_api.mergewake.CONFLICT_ROUNDS_KEY`, held equal by
#: tests/unit/worker/test_merge_conflict_round.py).
CONFLICT_FIX_PENDING = "conflict_fix_pending"
CONFLICT_FIX_RUNNING = "conflict_fix_running"
CONFLICT_ROUNDS_KEY = "conflict_rounds"
#: How many conflict fix rounds a merge step gets before it refuses
#: `merge_conflict`. TWO, the PR-side CI fixer's own bound
#: (`swarm_api.cifix.DEFAULT_MAX_ATTEMPTS`, scripts/ci-fix.sh's
#: MAX_FIX_ATTEMPTS): a second conflict at a head a round already merged the
#: base into means the base is moving under the branch faster than an agent
#: resolves it, and a third attempt spends quota on a person's question. Not
#: the workflow's `merge_fix_rounds`, which is about red CI and defaults to
#: none: a conflict nobody resolves is never mergeable at all.
MERGE_CONFLICT_ROUNDS_MAX = 2

#: The PR-side CI fixer's record on its fix task (#1073): scripts/ci-fix.sh
#: submits a `continues_task` workflow for a red `swarm/<task>` pull request
#: and swarm-api stamps `metadata.ci_fix` (`swarm_api.cifix.stamp`), naming
#: the pull request, with a `postback` state only the API writes. Restated
#: because the worker image carries no control plane; held equal by
#: tests/unit/worker/test_merge_ci_fixer_head.py.
CI_FIX_METADATA_FIELD = "ci_fix"
CI_FIX_POSTBACK_FIELD = "postback"
CI_FIX_POSTBACK_STATES = frozenset({"pending", "posted", "gave_up"})
#: How many of the tenant's CI-fixer tasks for one pull request of one branch
#: are read. swarm-api refuses a fixer past `cifix.MAX_ATTEMPTS_CEILING` (3)
#: per pull request, so 10 is that cap with room for any a person submitted
#: by hand before it existed; past it the newest are not looked at, and an
#: unmatched head is `head_moved` as before.
CI_FIXER_TASKS_READ = 10

#: The step's summary key naming each fix-round record it could not read
#: (#2373): its own task, a round's, a recorded fixer's. At most
#: FIX_RECORDS_UNREAD_MAX of them, each reason FIX_RECORDS_UNREAD_CHARS long,
#: so a `merge_fix` record listing many rounds cannot grow the summary.
FIX_RECORDS_UNREAD_KEY = "fix_records_unread"
FIX_RECORDS_UNREAD_MAX = 5
FIX_RECORDS_UNREAD_CHARS = 200

#: swarm-api's record of every platform fix round's pushed head, against the
#: PULL REQUEST (repository + number), on the task whose `swarm/<id>` branch
#: it is on (epic #1334: HANG-LEASE's merge refused PR 1338 at the head a fix
#: task of another workflow pushed, 2026-10-15 02:16Z). Written by
#: `swarm_api.cifix.record_fix_heads`; restated because the worker image
#: carries no control plane, held equal by
#: tests/unit/worker/test_merge_recorded_fix_heads.py.
PR_FIX_HEADS_FIELD = "pull_request_fix_heads"
#: How many of its entries are read, newest first: all that swarm-api keeps
#: (`cifix.FIX_HEADS_KEPT`), each one a task read and a signature check.
PR_FIX_HEADS_READ = 20

#: The park code after an update-branch whose new head GitHub had not made
#: within the bounded re-read: the call is asynchronous. swarm-api's tick
#: (`swarm_api.mergewake.BRANCH_UPDATE_PENDING`) then waits for the head to
#: move rather than reading the old head's checks, which were green.
BRANCH_UPDATE_PENDING = "branch_update_pending"

#: The park code of a pull request this step put in its base's merge queue:
#: swarm-api's wake tick reads the queue for it, not the checks
#: (`swarm_api.mergewake.MERGE_QUEUED`, held equal by
#: tests/unit/control_plane/test_merge_wake.py).
MERGE_QUEUED = "merge_queued"

#: Who commits GitHub's own merge commits -- `update-branch`, the web UI --
#: and signs them, so `verification.verified` is true. A two-parent commit
#: anyone else made can carry any tree, so the walk takes one of those only
#: once its tree is shown to be the clean merge of its parents
#: (`_merge_impurity`, FIX-MERGE-PURE-BASE-MERGE).
GITHUB_COMMITTER_EMAIL = "noreply@github.com"

#: The refusal code of a step whose pull request a person on it pushed a
#: commit to (#2505). BGD, 2026-10-27 (issue triage, epic #2525): "a distinct
#: non-failure end: superseded_by_person_push (not FAILED) when the committer
#: is not the platform identity, so failure-review stops filing it; a foreign
#: head from an unknown branch-mate still refuses." The refusal is unchanged:
#: `_accept` takes no head a person made of content, and nothing merges. The
#: step's end cause is the frozen `EndCause`, which has no such member yet, so
#: until contract request SUPERSEDED_BY_PERSON_PUSH lands the step ends
#: FAILED/MERGE_REFUSED with this code, and failure-review's `person_push`
#: rule (`swarm_api.failuredigest.SUPERSEDED_BY_PERSON_PUSH`, held equal by
#: tests/unit/worker/test_merge_person_push.py) skips it.
SUPERSEDED_BY_PERSON_PUSH = "superseded_by_person_push"

#: The committer addresses the platform commits under when it names no
#: person: `WorkerConfig.git_author_email`'s default and
#: `gitidentity.BOT_GIT_IDENTITY`'s (held equal by
#: tests/unit/worker/test_merge_person_push.py). Restated, not imported, so
#: the merge action does not load the worker's whole configuration module.
PLATFORM_COMMITTER_EMAILS = frozenset({
    "swarmcloud-agent@users.noreply.github.com",
    "swarmcloud@users.noreply.github.com",
})

#: How every commit the worker pushes ends its message
#: (`Worker._worker_commit_message`: "Made by the worker for task <id>.";
#: `lifecycle.replay_agent_commits` ends each kept agent commit with it too).
#: Since #765 the worker publishes AS the person who dispatched the work, so
#: its commits' committer names that person; this line is what still says
#: the platform made them. A platform head the step did not accept is a
#: defect for failure-review, never a person's push.
WORKER_COMMIT_LINE = "Made by the worker for task "

#: The summary key listing every base merge the walk took that GitHub did not
#: commit: a person's (or anyone's) clean merge of the base on top of an
#: accepted head. BGD, 2026-10-26: "accept a pure base merge, and record who
#: made it" -- F05 of #2379, where a person's merge of main ended a parked
#: merge step FAILED head_moved that auto-merge.yml's `is_pure_base_merge`
#: accepts from anyone. Each entry is `_commit_record`'s.
PURE_BASE_MERGES_KEY = "pure_base_merges"

#: A 405/422 from the merge call that means the branch is behind: the
#: update row, not a refusal.
_OUT_OF_DATE = re.compile(r"out of date|not up to date|is behind", re.IGNORECASE)
#: A 405/422 (merge) or 422 (update-branch) that names a merge conflict.
_CONFLICT = re.compile(r"conflict", re.IGNORECASE)
#: A 405/422 from the merge call that means the base merges only through a
#: merge queue: GitHub's "Changes must be made through the merge queue",
#: alone (405) or under "Repository rule violations found" (422).
_MERGE_QUEUE = re.compile(r"merge queue", re.IGNORECASE)
#: A 403 that is GitHub's plan answer, not a missing right: a private
#: repository on a plan without rulesets (or branch protection) is answered
#: "Upgrade to GitHub Pro or make this repository public to enable this
#: feature." (measured on sagaxyz/ai-studio, 2026-10-10). Read as "the plan
#: has none", exactly as a 404 is; any other 403 still refuses.
_PLAN_LACKS_FEATURE = re.compile(
    r"upgrade to github pro|make this repository public to enable this feature",
    re.IGNORECASE,
)
#: Where GitHub's own error bodies point. A `documentation_url` elsewhere is
#: not GitHub's plan answer, whatever its message says.
_GITHUB_DOCS = re.compile(r"^https://docs\.github\.com/", re.IGNORECASE)

#: `RequiredChecks.source`: where the required checks came from, recorded as
#: the step's `required_checks_source`. The two `none_*` sources read nothing
#: required, so every check at the head must be green and one must exist.
CHECKS_FROM_RULESETS = "rulesets"
CHECKS_FROM_CLASSIC = "classic"
CHECKS_NONE_ALL_CHECKS = "none_all_checks"
CHECKS_NONE_PLAN_LIMITED = "none_plan_limited_all_checks"

#: The hosts `GitHubMerger` can merge on: github.com, where the tenant's
#: token may be sent at all (`forge.may_receive_forge_token`, #307).
MERGEABLE_HOSTS = forge_mod.GITHUB_HOSTS

#: The closing references one merge reads: one page, the close script's own
#: (`scripts/close-merged-issues.sh` `PAGE=100`). GitHub links far fewer; past
#: a page the page is closed and `issues_beyond_page` recorded, never a
#: silent partial close.
CLOSING_PAGE = 100

_SHA = re.compile(r"^[0-9a-f]{40}$")

_CLOSING_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    f" closingIssuesReferences(first: {CLOSING_PAGE}) {{"
    " totalCount nodes { number state repository { nameWithOwner } } } } } }"
)

_QUEUE_ENTRY = "mergeQueueEntry { id position state enqueuedAt }"

_ENQUEUE_MUTATION = (
    "mutation($pullRequestId: ID!, $expectedHeadOid: GitObjectID!) {"
    " enqueuePullRequest(input: {pullRequestId: $pullRequestId,"
    " expectedHeadOid: $expectedHeadOid}) {"
    f" {_QUEUE_ENTRY} }} }}"
)

#: Whether the pull request is in its base's merge queue, and the last reason
#: GitHub gave for taking it out.
_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    f" isInMergeQueue {_QUEUE_ENTRY}"
    " timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {"
    " nodes { ... on RemovedFromMergeQueueEvent { reason createdAt } } } } } }"
)

# ---------------------------------------------------------------------------
# The rules auto-merge.yml also states (§8)
# ---------------------------------------------------------------------------


def title_is_placeholder(title: str) -> bool:
    """Gate 1's prefix, OR the worker's own `pr-title.txt` rule.

    Gate 1 refuses `[swarm] task_` after leading whitespace, in any case; the
    author's worker refuses a `pr-title.txt` carrying the retired shape
    anywhere, with any spacing (#214). The union is the worker's rule: STRICTER
    than gate 1 (`Fix [swarm] task_ handling` passes the gate and is refused
    here) -- §8 allows the merge to be the stricter, never the looser.
    """
    return (
        title.lstrip().lower().startswith(PLACEHOLDER_TITLE_PREFIX)
        or RETIRED_TITLE_RE.search(title) is not None
    )


def check_run_finished(run: Mapping[str, Any]) -> bool:
    """Whether a check run has finished: `status` is `completed`, OR it
    carries a conclusion together with a `completed_at`.

    BGD's decision of 2026-10-23 (CHECK-STUCK, part of #2031): a run that
    carries a conclusion and a completed_at is judged by its conclusion,
    whatever `status` says. GitHub left PR 2006's `secret scan` at
    `in_progress` with `conclusion: success` and a `completed_at`, its
    workflow run completed/success, and this step waited out its 6 h on it.
    A run with no conclusion stays pending; one `completed` with no
    conclusion is finished and, having no green conclusion, not green.
    swarm-api's `forgechecks.check_run_finished`, `merge-train.sh judge` and
    auto-merge.yml's gates 5-6 state the same rule, held equal by
    tests/unit/scripts/test_auto_merge_workflow.py.
    """
    if run.get("status") == "completed":
        return True
    return bool(run.get("conclusion")) and bool(run.get("completed_at"))


def other_check_blocks(run: Mapping[str, Any]) -> bool:
    """A check at the head that holds the merge: not finished
    (`check_run_finished`), or finished with a conclusion other than
    success, skipped or neutral. STRICTER than gate 5, which holds only on
    four conclusions (§8)."""
    if not check_run_finished(run):
        return True
    return (run.get("conclusion") or "") not in OTHER_TOLERATED


class ActionsSuite(NamedTuple):
    """What the Actions run that made one check suite says (`actions_suites`)."""

    event: str | None
    branch: str | None
    #: The Actions run's own conclusion (`cancelled_by_its_workflow`, #2582).
    conclusion: str | None = None
    #: A newer Actions run of the same workflow and event, from the same
    #: branch, is at the sha: the run that concurrency cancelled this one.
    superseded: bool = False


_NO_SUITE = ActionsSuite(None, None)


def actions_suites(runs: Iterable[Any]) -> dict[int | None, ActionsSuite]:
    """`GET actions/runs?head_sha=` read as `ActionsSuite`s, by check suite id.

    A run is superseded when another run at the sha has the same
    `workflow_id`, event and head branch and a larger id (GitHub's run ids
    grow), finished or not: a concurrency group cancels the older run when
    the newer one starts, and the newer one may not have created a given
    check run yet (#2582). A run with no `workflow_id` is never superseded.
    """
    rows = [_mapping(run) for run in runs]
    suites: dict[int | None, ActionsSuite] = {}
    for run in rows:
        suite = _int(run.get("check_suite_id"))
        event, branch = _str(run.get("event")), _str(run.get("head_branch"))
        if suite is None or not (event or branch):
            continue
        workflow, run_id = _int(run.get("workflow_id")), _int(run.get("id"))
        superseded = workflow is not None and run_id is not None and any(
            _int(other.get("workflow_id")) == workflow
            and _str(other.get("event")) == event
            and _str(other.get("head_branch")) == branch
            and (_int(other.get("id")) or 0) > run_id
            for other in rows)
        suites[suite] = ActionsSuite(event, branch, _str(run.get("conclusion")), superseded)
    return suites


def _other_branch_push(suite: ActionsSuite | None, head_ref: str | None) -> bool:
    """Whether a check suite was triggered by a push to a known branch that
    is not `head_ref` -- another branch's result at a shared sha, not the
    pull request's (#2365). False when anything is unknown."""
    if suite is None or head_ref is None:
        return False
    return suite.event == "push" and suite.branch is not None and suite.branch != head_ref


def _event_group(run: Mapping[str, Any]) -> str | None:
    """What a run is grouped by: the event that triggered its check suite;
    without one, its check suite (a credential that cannot read Actions runs
    still never lets one suite outvote another); without either, None."""
    event = run.get("event")
    if isinstance(event, str) and event:
        return event
    suite = run.get("check_suite_id")
    return f"check suite {suite}" if suite is not None else None


def latest_per_event(runs: list[Mapping[str, Any]]) -> list[tuple[str | None, Mapping[str, Any]]]:
    """The latest run of each event among the runs of ONE check, as (event, run).

    BGD's decision of 2026-10-22 (CHECK-RULE, part of #1999 and #1778): a
    re-run replaces the earlier runs of ITS OWN event, never another's. Runs
    are ordered as `latest_reading` orders them: by GitHub's run id, which a
    re-run increases, then by position; runs with no id (commit statuses)
    first. Groups come oldest latest run first, which is the label's order.
    """
    ordered = sorted(enumerate(runs), key=lambda item: (
        item[1].get("run_id") is not None, item[1].get("run_id") or 0, item[0]))
    latest: dict[str | None, tuple[int, Mapping[str, Any]]] = {}
    for rank, (_position, run) in enumerate(ordered):
        latest[_event_group(run)] = (rank, run)
    deciding = [(event, run) for event, (_rank, run)
                in sorted(latest.items(), key=lambda item: item[1][0])]
    others = [run for _event, run in deciding]
    return [(event, {**run, "conclusion": CANCELLED_CONCLUSION}
             if cancelled_by_its_workflow(run, others) else run)
            for event, run in deciding]


def cancelled_by_its_workflow(run: Mapping[str, Any],
                              deciding: Iterable[Mapping[str, Any]]) -> bool:
    """Whether a check run that finished red is read as `cancelled` because
    its Actions run was (#2582, BGD 2026-10-28).

    `ci-gate` and security.yml's folded name-keepers run under `if: always()`,
    so inside a workflow run concurrency cancelled they still run and conclude
    `failure` over needs that were only cancelled: PR #2565's push ci-gate at
    968d9949 read red, the branch's newer push run had not created its own
    yet, and the step refused `checks_failed` although no job had failed. So a
    check run that finished with any conclusion but `success`
    (`PASSED_IN_ITS_RUN`) -- `skipped` too, which is what a gate under
    `!cancelled()` may conclude in a cancelled run, and which would otherwise
    read green -- in an
    Actions run that concluded `cancelled` (`workflow_conclusion`), is read
    as cancelled -- pending, never green (`cancelled_run`) -- when a newer run
    of the same workflow and event is at the head (`workflow_superseded`) or
    another event's deciding run (`deciding`, the check's latest run of each
    event) passed the check. A red check in a run nobody cancelled stays red,
    and so does one in a cancelled run with neither: nothing then says the
    failure was not real. swarm-api's `forgechecks.cancelled_by_its_workflow`
    is the same rule, held equal by
    tests/unit/control_plane/test_forgechecks_cancelled_workflow_run.py.
    """
    if run.get("workflow_conclusion") != CANCELLED_CONCLUSION or not check_run_finished(run):
        return False
    if (run.get("conclusion") or "") in (PASSED_IN_ITS_RUN, CANCELLED_CONCLUSION):
        return False
    if run.get("workflow_superseded") is True:
        return True
    return any(other is not run and check_run_finished(other)
               and (other.get("conclusion") or "") in REQUIRED_GREEN for other in deciding)


def cancelled_run(run: Mapping[str, Any]) -> bool:
    """A run that finished `cancelled`: pending, never failed (#1778).

    BGD released bundle H09, 2026-10-27, #1778's second expectation: "a check
    whose LATEST run is cancelled is still not green and waits, rather than
    failing the merge". A cancelled run is one someone stopped -- the operator
    cancelling stranded runs to re-run them, on 2026-10-21 -- not a verdict on
    the head, so it waits for its re-run as a run still in progress does
    (`MERGE_CI_MAX_SECONDS` still bounds the wait). A red latest run of
    another event still wins: a re-run of the cancelled one cannot make it
    green. swarm-api's `forgechecks`, `merge-train.sh judge` and auto-merge.yml's
    gates 5-6 state the same rule.
    """
    return check_run_finished(run) and run.get("conclusion") == CANCELLED_CONCLUSION


def _completed_instant(run: Mapping[str, Any]) -> datetime | None:
    """A run's `completed_at` as an aware instant, or None when it has none."""
    value = run.get("completed_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant if instant.tzinfo is not None else None


def cancelled_since(groups: Iterable[list[Mapping[str, Any]]]) -> datetime | None:
    """When a reading became one whose ONLY problem is cancelled runs (#1778).

    `groups` are the runs of each check the decision judges, one list per
    check. The reading is cancelled-only when every check has runs, every
    event's latest run (`latest_per_event`) has finished, at least one is
    `cancelled` (`cancelled_run`) and none failed -- the caller asks only of
    a reading with nothing failed. Every deciding run having finished, the
    reading has not changed since the last of them did, so the newest
    `completed_at` among them is when the first such reading could be made:
    the start of `MERGE_CI_CANCELLED_MAX_SECONDS`. Read from GitHub, not from
    the park record, because a CI park's record (`merge_wait`) carries no
    field for it and swarm-api's park body is strict. A re-run makes a new
    latest run, still running or green, so it ends the bound; one cancelled
    again starts it afresh. None when the reading is not cancelled-only, or
    when a deciding run has no `completed_at` (a commit status): that reading
    keeps `MERGE_CI_MAX_SECONDS`.
    """
    latest: list[Mapping[str, Any]] = []
    for runs in groups:
        mine = [run for _event, run in latest_per_event(runs)]
        if not mine:
            return None
        latest.extend(mine)
    if not latest or not all(check_run_finished(run) for run in latest):
        return None
    if not any(cancelled_run(run) for run in latest):
        return None
    instants = [_completed_instant(run) for run in latest]
    if any(instant is None for instant in instants):
        return None
    return max(instant for instant in instants if instant is not None)


def _judge_latest(runs: list[Mapping[str, Any]], green: frozenset[str] | set[str]) -> str:
    latest = [run for _event, run in latest_per_event(runs)]
    if not latest:
        return "pending"
    if not all(check_run_finished(run) for run in latest):
        return "pending"
    if all((run.get("conclusion") or "") in green for run in latest):
        return "green"
    if all(cancelled_run(run) or (run.get("conclusion") or "") in green for run in latest):
        return "pending"
    return "failed"


def required_check_state(runs: list[Mapping[str, Any]]) -> str:
    """The runs of ONE required check: "green", "pending" or "failed".

    Decided by the LATEST run of EACH event that ran it (BGD, 2026-10-22):
    green only when every event's latest run is green, pending when any is
    not finished (`check_run_finished`; or there is none) or every one not
    green is cancelled (`cancelled_run`, #1778), failed otherwise. A red push run is
    never outvoted by a green pull_request run (CI-EARLY, #1694); a flake
    re-run green no longer holds the check (#1999, #1778). swarm-api's
    `forgechecks.required_check_state` is the same rule, held equal by
    tests/unit/control_plane/test_forgechecks_per_event.py.
    """
    return _judge_latest(runs, REQUIRED_GREEN)


def failing_run_ids(runs: list[Mapping[str, Any]],
                    green: frozenset[str] | set[str]) -> set[int] | None:
    """GitHub's ids of the runs that make ONE check red: each event's latest
    run (`latest_per_event`) that finished with a conclusion not in `green`,
    and not `cancelled` (`cancelled_run`, #1778: pending, not red). None
    when one of them has no id -- a commit status, which no re-run
    replaces. What `_rerun_settling` matches against the jobs the tick
    re-ran: a re-run makes new ids, so a red one is no longer among them."""
    ids: set[int] = set()
    for _event, run in latest_per_event(runs):
        if (not check_run_finished(run) or cancelled_run(run)
                or (run.get("conclusion") or "") in green):
            continue
        run_id = run.get("run_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            return None
        ids.add(run_id)
    return ids


def required_check_label(runs: list[Mapping[str, Any]]) -> str:
    """What a failed check's label says in brackets: the latest run's
    conclusion per event, `push: success, pull_request: failure`; one
    conclusion alone when no run carries an event."""
    return ", ".join(
        str(run.get("conclusion") or run.get("status")) if event is None
        else f"{event}: {run.get('conclusion') or run.get('status')}"
        for event, run in latest_per_event(runs)
    )


def judge_other_checks(checks: list[CheckFacts]) -> tuple[list[str], list[str]]:
    """The no-required path's decision over every check at the head: the
    labels of the checks pending and of the checks failed.

    One rule for every check (BGD, 2026-10-22, CHECK-RULE-2, part of #1999):
    each check -- its name and App, so one App's run never supersedes
    another's -- is judged by the latest run of each event that ran it
    (`latest_per_event`), and only those deciding runs by
    `other_check_blocks`. A superseded failure, or a superseded run still in
    progress, no longer holds the merge. A check with an event's latest run
    failed is failed even beside one still running: waiting cannot make it
    green, and the merge refused on it before this rule too. A deciding run
    that finished `cancelled` is pending, not failed (`cancelled_run`,
    #1778). The label names the deciding run of each event, as
    `required_check_label` does.
    """
    by_check: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for check in checks:
        by_check.setdefault((check.name, check.app_id), []).append(check.as_run())
    pending: list[str] = []
    failed: list[str] = []
    for (name, _app_id), runs in by_check.items():
        blocking = [run for _event, run in latest_per_event(runs) if other_check_blocks(run)]
        if not blocking:
            continue
        label = f"{name} ({required_check_label(runs)})"
        if any(check_run_finished(run) and not cancelled_run(run) for run in blocking):
            failed.append(label)
        else:
            pending.append(label)
    return pending, failed


def latest_reading(runs_by_check: Mapping[str, list[CheckFacts]],
                   green: frozenset[str] | set[str]) -> tuple[list[str], list[str]]:
    """What a CI park RECORDS (WAIT-RECORD, part of #1782): the checks pending
    and the checks failed, by name, each judged by its LATEST run of each
    event (`latest_per_event`), as the decision is.

    A check's runs are ordered by GitHub's run id, which a re-run increases,
    so a run cancelled and then superseded by a success of the same event is
    green here. No run is pending; so is any event's latest run not
    finished (`check_run_finished`), and a check whose every latest run not
    in `green` is cancelled (`cancelled_run`, #1778). Otherwise a check with any event's latest run's conclusion
    not in `green` is failed.

    A RECORD, NOT THE DECISION: the merge decides on `required_check_state`
    and `judge_other_checks`. The record is what the "Why it's waiting"
    panel names; it never decides anything, and for a required check it
    reads exactly what the decision reads (CHECK-RULE, part of #1999).
    """
    pending: list[str] = []
    failed: list[str] = []
    for name, runs in runs_by_check.items():
        state = _judge_latest([run.as_run() for run in runs], green)
        if state == "pending":
            pending.append(name)
        elif state == "failed":
            failed.append(name)
    return sorted(pending), sorted(failed)


# ---------------------------------------------------------------------------
# The forge, behind one small interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestFacts:
    number: int
    state: str
    merged: bool
    draft: bool
    title: str
    head_sha: str | None
    head_ref: str | None
    head_repo: str | None
    base_ref: str | None
    base_repo: str | None
    #: True, False, or None while GitHub has not computed it.
    mergeable: bool | None
    merge_commit_sha: str | None = None
    #: GitHub's `mergeable_state`: `behind` when the base requires an
    #: up-to-date branch and this one is not; `dirty` on a conflict.
    mergeable_state: str | None = None
    #: GraphQL's id for the pull request, which `enqueuePullRequest` takes.
    node_id: str | None = None
    #: Who merged it (GitHub's `merged_by.login`) and when (`merged_at`), as
    #: GitHub says: what a merge made outside the workflow is recorded with.
    merged_by: str | None = None
    merged_at: str | None = None
    #: The people on the pull request: its author and its assignees, each a
    #: login GitHub says is a `User` account (`_person_pusher`, #2505).
    people: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckFacts:
    """One check run, or one commit status read as a check run."""

    name: str
    status: str
    conclusion: str | None
    app_id: int | None = None
    #: GitHub's id for a check run, which grows with every run, a re-run
    #: included: what orders a check's runs (`latest_reading`). None for a
    #: commit status, of which GitHub serves only the newest per context.
    run_id: int | None = None
    #: The event that triggered the run's check suite (push, pull_request,
    #: ...), read from the suite's Actions run: what a check's runs are
    #: grouped by (`latest_per_event`). None for a commit status, another
    #: App's suite, or when it was not read. A push suite from a branch other
    #: than the pull request's head ref never reaches here (`checks_at`, #2365).
    event: str | None = None
    #: GitHub's id for the run's check suite: the group when no event is known.
    check_suite_id: int | None = None
    #: When GitHub says the run finished: with a conclusion, it finishes a
    #: run whatever `status` says (`check_run_finished`, CHECK-STUCK). None
    #: for a commit status, whose state alone says whether it finished.
    completed_at: str | None = None
    #: The conclusion of the Actions run whose suite made this check run, and
    #: whether a newer run of the same workflow and event, from the same
    #: branch, is at the head: what `cancelled_by_its_workflow` reads
    #: (#2582). None and False when the Actions runs were not read.
    workflow_conclusion: str | None = None
    workflow_superseded: bool = False

    def as_run(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "conclusion": self.conclusion,
                "completed_at": self.completed_at, "run_id": self.run_id, "event": self.event,
                "check_suite_id": self.check_suite_id,
                "workflow_conclusion": self.workflow_conclusion,
                "workflow_superseded": self.workflow_superseded}


@dataclass(frozen=True)
class MergeAnswer:
    merged: bool
    status: int
    sha: str | None = None
    message: str = ""


@dataclass(frozen=True)
class ClosingIssue:
    number: int
    #: GitHub's GraphQL issue state: OPEN or CLOSED.
    state: str
    #: `owner/name` of the repository the issue is in.
    repository: str


@dataclass(frozen=True)
class ClosingReferences:
    """One page of a pull request's closing references, and how many it has."""

    issues: tuple[ClosingIssue, ...] = ()
    total: int = 0


@dataclass(frozen=True)
class CommitFacts:
    sha: str
    parents: tuple[str, ...]
    committer_email: str | None
    verified: bool
    #: The commit's tree, what a pure base merge's is compared by
    #: (`_merge_impurity`). None when GitHub did not say.
    tree: str | None = None
    #: The GitHub accounts GitHub links the committer and the author to
    #: (the response's top-level `committer`/`author`), each a login only
    #: when the account is a `User`: who a person's push was made by
    #: (`_person_pusher`). None when GitHub links none, or a bot.
    committer_login: str | None = None
    author_login: str | None = None
    #: The message's last line is the worker's (`WORKER_COMMIT_LINE`).
    by_worker: bool = False

    @property
    def by_github(self) -> bool:
        """Committed and signed by GitHub itself, as `update-branch` commits are."""
        return self.verified and (self.committer_email or "").lower() == GITHUB_COMMITTER_EMAIL


@dataclass(frozen=True)
class TreeFacts:
    """A recursive tree: every path that is not a directory, as (mode, object sha).

    `truncated` is GitHub saying it served only part of the tree, which then
    proves nothing about the rest."""

    entries: Mapping[str, tuple[str, str]]
    truncated: bool = False


@dataclass(frozen=True)
class UpdateAnswer:
    status: int
    message: str = ""


@dataclass(frozen=True)
class RequiredChecks:
    """What the base branch requires. `checks` empty means it requires none."""

    checks: tuple[forge_mod.RequiredCheck, ...] = ()
    protected: bool = False
    #: Which rule the checks were read by: one of the `CHECKS_*` sources.
    source: str = CHECKS_NONE_ALL_CHECKS


@dataclass(frozen=True)
class QueueFacts:
    """Where a pull request stands in its base's merge queue."""

    queued: bool
    entry: str | None = None
    position: int | None = None
    state: str | None = None
    enqueued_at: str | None = None
    #: The reason GitHub gave the last time it removed the pull request.
    removed_reason: str | None = None

    def record(self, head: str) -> dict[str, Any]:
        return {"entry": self.entry, "position": self.position, "state": self.state,
                "enqueued_at": self.enqueued_at, "head": head}


@dataclass(frozen=True)
class EnqueueAnswer:
    enqueued: bool
    status: int
    entry: QueueFacts | None = None
    message: str = ""


class ForgeMerger(Protocol):
    """What the merge step asks of a forge. One implementation: `GitHubMerger`."""

    full_name: str

    def can_push(self) -> bool: ...

    def default_branch(self) -> str | None: ...

    def pull_request(self, number: int) -> PullRequestFacts: ...

    def required_checks(self, branch: str) -> RequiredChecks: ...

    def checks_at(self, sha: str, *, head_ref: str | None = None) -> list[CheckFacts]: ...

    def commit(self, sha: str) -> CommitFacts: ...

    def on_base(self, base: str, sha: str) -> bool: ...

    def merge_base(self, first: str, second: str) -> str | None: ...

    def tree(self, sha: str) -> TreeFacts: ...

    def update_branch(self, number: int, *, expected_head_sha: str) -> UpdateAnswer: ...

    def merge(self, number: int, *, sha: str, title: str, message: str) -> MergeAnswer: ...

    def enqueue(self, number: int, *, node_id: str, expected_head_sha: str) -> EnqueueAnswer: ...

    def queue_state(self, number: int) -> QueueFacts: ...

    def comment(self, number: int, body: str) -> bool: ...

    def closing_issues(self, number: int) -> ClosingReferences: ...

    def close_issue(self, number: int, *, comment: str) -> bool: ...


class UnsupportedForge(ValueError):
    """The repository is on a host no `ForgeMerger` serves."""


def plan_lacks_feature(exc: BaseException) -> bool:
    """A 403 that is GitHub saying the repository's plan has no such feature
    (`_PLAN_LACKS_FEATURE`), with GitHub's documentation link if it gave one.
    Any other 403, and every other status, is not."""
    if getattr(exc, "status", None) != 403:
        return False
    if not _PLAN_LACKS_FEATURE.search(str(getattr(exc, "message", "") or "")):
        return False
    url = str(getattr(exc, "documentation_url", "") or "")
    return not url or _GITHUB_DOCS.search(url) is not None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _user_login(account: Any) -> str | None:
    """A GitHub account's login when GitHub says it is a person's (`User`)."""
    account = _mapping(account)
    return _str(account.get("login")) if account.get("type") == "User" else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


#: A commit status's `state`, as a check run's (status, conclusion).
_STATUS_AS_RUN = {
    "success": ("completed", "success"),
    "pending": ("in_progress", None),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
}


class GitHubMerger:
    """`ForgeMerger` over GitHub's REST and GraphQL APIs on api.github.com.

    Every request goes through `forge.PinnedForgeClient`: one host, the token
    in the Authorization header only, no redirect followed, GETs retried on a
    transient failure and nothing else resent.
    """

    def __init__(self, client: forge_mod.PinnedForgeClient, *, owner: str, repo: str) -> None:
        self._client = client
        self.owner = owner
        self.repo = repo
        self.full_name = f"{owner}/{repo}"
        self._base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self._repository: Mapping[str, Any] | None = None

    def __repr__(self) -> str:
        return f"GitHubMerger({self.full_name!r})"

    def _repository_doc(self) -> Mapping[str, Any]:
        if self._repository is None:
            self._repository = _mapping(self._client.get_ok(self._base))
        return self._repository

    def can_push(self) -> bool:
        return _mapping(self._repository_doc().get("permissions")).get("push") is True

    def default_branch(self) -> str | None:
        return _str(self._repository_doc().get("default_branch"))

    def pull_request(self, number: int) -> PullRequestFacts:
        data = _mapping(self._client.get_ok(f"{self._base}/pulls/{number}"))
        head = _mapping(data.get("head"))
        base = _mapping(data.get("base"))
        mergeable = data.get("mergeable")
        return PullRequestFacts(
            number=number,
            state=str(data.get("state") or ""),
            merged=data.get("merged") is True,
            draft=data.get("draft") is True,
            title=data.get("title") if isinstance(data.get("title"), str) else "",
            head_sha=_str(head.get("sha")),
            head_ref=_str(head.get("ref")),
            head_repo=_str(_mapping(head.get("repo")).get("full_name")),
            base_ref=_str(base.get("ref")),
            base_repo=_str(_mapping(base.get("repo")).get("full_name")),
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            merge_commit_sha=_str(data.get("merge_commit_sha")),
            mergeable_state=_str(data.get("mergeable_state")),
            node_id=_str(data.get("node_id")),
            merged_by=_str(_mapping(data.get("merged_by")).get("login")),
            merged_at=_str(data.get("merged_at")),
            people=tuple(login for login in (
                _user_login(account) for account in
                [data.get("user"), *(data.get("assignees") or [])]) if login),
        )

    def required_checks(self, branch: str) -> RequiredChecks:
        """Every required check on `branch`: its rulesets' and its classic protection's.

        `rules/branches/{branch}` gives the rulesets' rules; the branch's own
        read gives classic protection, which needs only read access to see
        (`protection.required_status_checks`). Both, because a repository may
        use either, and a required check read from one alone would be missed.

        A 404, or a 403 that is GitHub's plan answer (`plan_lacks_feature`: a
        private repository on a plan without rulesets), reads as "none"; any
        other answer raises. `source` records which rule the step then used.
        """
        plan_limited = False
        try:
            rules = self._client.rules_for_branch(self.owner, self.repo, branch)
        except forge_mod.ForgeAnswered as exc:
            if plan_lacks_feature(exc):
                plan_limited = True
            elif getattr(exc, "status", None) != 404:
                raise
            rules = []
        found = {(c.context, c.app_id): c for c in forge_mod.required_status_checks(rules)}
        from_rulesets = bool(found)
        try:
            branch_doc = _mapping(
                self._client.get_ok(f"{self._base}/branches/{quote(branch, safe='')}")
            )
        except forge_mod.ForgeAnswered as exc:
            if not plan_lacks_feature(exc):
                raise
            plan_limited = True
            branch_doc = {}
        protection = _mapping(_mapping(branch_doc.get("protection")).get("required_status_checks"))
        for check in protection.get("checks") or []:
            check = _mapping(check)
            context = _str(check.get("context"))
            if context is not None:
                key = (context, _int(check.get("app_id")))
                found.setdefault(key, forge_mod.RequiredCheck(context, key[1]))
        for context in protection.get("contexts") or []:
            if isinstance(context, str) and context and not any(
                c == context for c, _ in found
            ):
                found[(context, None)] = forge_mod.RequiredCheck(context, None)
        protected = bool(rules) or branch_doc.get("protected") is True
        if from_rulesets:
            source = CHECKS_FROM_RULESETS
        elif found:
            source = CHECKS_FROM_CLASSIC
        elif plan_limited and not protected:
            source = CHECKS_NONE_PLAN_LIMITED
        else:
            source = CHECKS_NONE_ALL_CHECKS
        return RequiredChecks(checks=tuple(found.values()), protected=protected, source=source)

    def checks_at(self, sha: str, *, head_ref: str | None = None) -> list[CheckFacts]:
        """Every check run, and every commit status, reported at `sha` for
        the pull request whose head branch is `head_ref`.

        A check run carries the event that triggered its check suite, read
        from the Actions runs at `sha` -- ONE more read, made only when some
        check has more than one run there, since only then does the event
        decide anything (`latest_per_event`), or when some run finished
        neither green nor tolerated, since that run may not be this pull
        request's. A head whose every check has one green run costs nothing.

        `commits/{sha}/check-runs` lists every check suite at the sha, from
        whichever branch pushed it, and swarm/ lanes branch from one another:
        at edb9c942 (PR #2340) a sibling branch's red push ci-gate stood as
        the PR's push result while the PR branch's own push run was still
        running, and the step refused `checks_failed` (#2365). So with a
        `head_ref`, a run whose suite was triggered by a push to a KNOWN
        branch other than `head_ref` is dropped. pull_request suites (a
        fork's names the fork's branch), commit statuses, and suites whose
        branch is unknown (no `actions: read`, another App) are kept.
        """
        out: list[CheckFacts] = []
        for run in self._client.paginate(f"{self._base}/commits/{sha}/check-runs",
                                         key="check_runs"):
            run = _mapping(run)
            name = _str(run.get("name"))
            if name is None:
                continue
            out.append(CheckFacts(
                name=name,
                status=str(run.get("status") or ""),
                conclusion=_str(run.get("conclusion")),
                completed_at=_str(run.get("completed_at")),
                app_id=_int(_mapping(run.get("app")).get("id")),
                run_id=_int(run.get("id")),
                check_suite_id=_int(_mapping(run.get("check_suite")).get("id")),
            ))
        names = [check.name for check in out]
        red = any(check_run_finished(run) and (run.get("conclusion") or "") not in OTHER_TOLERATED
                  for run in (check.as_run() for check in out))
        if len(names) != len(set(names)) or red:
            suites = self._suite_events(sha)
            out = [replace(check, event=suite.event, workflow_conclusion=suite.conclusion,
                           workflow_superseded=suite.superseded)
                   for check in out
                   for suite in (suites.get(check.check_suite_id, _NO_SUITE),)
                   if not _other_branch_push(suites.get(check.check_suite_id), head_ref)]
        combined = _mapping(self._client.get_ok(f"{self._base}/commits/{sha}/status"))
        for status in combined.get("statuses") or []:
            status = _mapping(status)
            name = _str(status.get("context"))
            if name is None:
                continue
            state, conclusion = _STATUS_AS_RUN.get(
                str(status.get("state") or ""), ("completed", "failure")
            )
            out.append(CheckFacts(name=name, status=state, conclusion=conclusion))
        return out

    def _suite_events(self, sha: str) -> dict[int | None, ActionsSuite]:
        """The Actions run of each check suite at `sha`, by suite id: its
        event and head branch, either None when GitHub did not say, its own
        conclusion, and whether it is superseded (`ActionsSuite`, #2582).

        A credential without `actions: read` (403), or a repository with
        Actions off (404), reads as no event and no branch: each check suite
        is then its own group, so a green suite still never outvotes a red
        one, and no suite is dropped as another branch's.
        """
        try:
            runs = self._client.paginate(f"{self._base}/actions/runs",
                                         query={"head_sha": sha}, key="workflow_runs")
        except forge_mod.ForgeAnswered as exc:
            if exc.status in (403, 404):
                return {}
            raise
        return actions_suites(runs)

    def commit(self, sha: str) -> CommitFacts:
        data = _mapping(self._client.get_ok(f"{self._base}/commits/{quote(sha, safe='')}"))
        detail = _mapping(data.get("commit"))
        parents = tuple(
            p for p in (_str(_mapping(parent).get("sha")) for parent in data.get("parents") or [])
            if p is not None
        )
        message = detail.get("message") if isinstance(detail.get("message"), str) else ""
        last = message.strip().splitlines()[-1] if message.strip() else ""
        return CommitFacts(
            sha=_str(data.get("sha")) or sha,
            parents=parents,
            committer_email=_str(_mapping(detail.get("committer")).get("email")),
            verified=_mapping(detail.get("verification")).get("verified") is True,
            tree=_str(_mapping(detail.get("tree")).get("sha")),
            committer_login=_user_login(data.get("committer")),
            author_login=_user_login(data.get("author")),
            by_worker=last.startswith(WORKER_COMMIT_LINE),
        )

    def merge_base(self, first: str, second: str) -> str | None:
        """The commit `compare` says `first` and `second` both descend from."""
        data = _mapping(self._client.get_ok(
            f"{self._base}/compare/{quote(first, safe='')}...{quote(second, safe='')}"
        ))
        return _str(_mapping(data.get("merge_base_commit")).get("sha"))

    def tree(self, sha: str) -> TreeFacts:
        """The tree `sha`, recursively: blobs and submodules, never directories."""
        data = _mapping(self._client.get_ok(f"{self._base}/git/trees/{quote(sha, safe='')}",
                                            query={"recursive": "1"}))
        entries: dict[str, tuple[str, str]] = {}
        for raw in data.get("tree") or []:
            entry = _mapping(raw)
            path, kind = _str(entry.get("path")), _str(entry.get("type"))
            if path is None or kind == "tree":
                continue
            entries[path] = (_str(entry.get("mode")) or "", _str(entry.get("sha")) or "")
        return TreeFacts(entries=entries, truncated=data.get("truncated") is True)

    def on_base(self, base: str, sha: str) -> bool:
        """Whether `sha` is already on `base`: `compare` says the commit is
        behind the base, or is its tip."""
        data = _mapping(self._client.get_ok(
            f"{self._base}/compare/{quote(base, safe='')}...{quote(sha, safe='')}"
        ))
        return data.get("status") in ("behind", "identical")

    def update_branch(self, number: int, *, expected_head_sha: str) -> UpdateAnswer:
        """GitHub merges the base into the branch, if the head is still `expected_head_sha`."""
        answer = self._client.request(
            "PUT", f"{self._base}/pulls/{number}/update-branch",
            payload={"expected_head_sha": expected_head_sha},
        )
        return UpdateAnswer(status=answer.status, message=forge_mod._message_of(answer.data))

    def merge(self, number: int, *, sha: str, title: str, message: str) -> MergeAnswer:
        answer = self._client.request(
            "PUT", f"{self._base}/pulls/{number}/merge",
            payload={"merge_method": "squash", "sha": sha,
                     "commit_title": f"{title} (#{number})", "commit_message": message},
        )
        data = _mapping(answer.data)
        return MergeAnswer(
            merged=answer.status == 200 and data.get("merged") is True,
            status=answer.status,
            sha=_str(data.get("sha")),
            message=forge_mod._message_of(answer.data),
        )

    def enqueue(self, number: int, *, node_id: str, expected_head_sha: str) -> EnqueueAnswer:
        """Add the pull request to its base's merge queue, if its head is still
        `expected_head_sha`. GraphQL answers a refusal 200 with `errors`."""
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _ENQUEUE_MUTATION,
                     "variables": {"pullRequestId": node_id,
                                   "expectedHeadOid": expected_head_sha}},
        )
        data = _mapping(answer.data)
        entry = _mapping(_mapping(_mapping(data.get("data")).get("enqueuePullRequest"))
                         .get("mergeQueueEntry"))
        if answer.status != 200 or data.get("errors") or not entry:
            return EnqueueAnswer(False, answer.status,
                                 message=_graphql_message(answer.data)
                                 or f"GitHub answered {answer.status} and enqueued nothing")
        return EnqueueAnswer(True, answer.status, _queue_entry(entry))

    def queue_state(self, number: int) -> QueueFacts:
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _QUEUE_QUERY,
                     "variables": {"owner": self.owner, "name": self.repo, "number": number}},
        )
        data = _mapping(answer.data)
        pull = _mapping(_mapping(_mapping(data.get("data")).get("repository")).get("pullRequest"))
        if answer.status != 200 or data.get("errors") or not isinstance(
            pull.get("isInMergeQueue"), bool
        ):
            raise forge_mod.ForgeAnswered(answer.status, "/graphql",
                                          _graphql_message(answer.data)
                                          or "the merge queue could not be read")
        removals = _mapping(pull.get("timelineItems")).get("nodes") or []
        reason = _str(_mapping(removals[-1]).get("reason")) if removals else None
        if pull["isInMergeQueue"]:
            entry = _queue_entry(_mapping(pull.get("mergeQueueEntry")))
            return QueueFacts(True, entry.entry, entry.position, entry.state,
                              entry.enqueued_at, reason)
        return QueueFacts(False, removed_reason=reason)

    def comment(self, number: int, body: str) -> bool:
        answer = self._client.request(
            "POST", f"{self._base}/issues/{number}/comments", payload={"body": body}
        )
        return answer.status == 201

    def closing_issues(self, number: int) -> ClosingReferences:
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _CLOSING_QUERY,
                     "variables": {"owner": self.owner, "name": self.repo, "number": number}},
        )
        data = _mapping(answer.data)
        if answer.status != 200 or data.get("errors"):
            raise forge_mod.ForgeAnswered(answer.status, "/graphql",
                                          forge_mod._message_of(answer.data)
                                          or "the closing references could not be read")
        pull = _mapping(_mapping(_mapping(data.get("data")).get("repository")).get("pullRequest"))
        references = _mapping(pull.get("closingIssuesReferences"))
        nodes = references.get("nodes") or []
        out: list[ClosingIssue] = []
        for node in nodes:
            node = _mapping(node)
            issue = _int(node.get("number"))
            repository = _str(_mapping(node.get("repository")).get("nameWithOwner"))
            if issue is None or issue <= 0 or repository is None:
                continue
            out.append(ClosingIssue(issue, str(node.get("state") or ""), repository))
        total = _int(references.get("totalCount"))
        return ClosingReferences(tuple(out), max(total or 0, len(nodes)))

    def close_issue(self, number: int, *, comment: str) -> bool:
        commented = self.comment(number, comment)
        answer = self._client.request(
            "PATCH", f"{self._base}/issues/{number}",
            payload={"state": "closed", "state_reason": "completed"},
        )
        return commented and answer.status == 200


def _queue_entry(entry: Mapping[str, Any]) -> QueueFacts:
    return QueueFacts(True, _str(entry.get("id")), _int(entry.get("position")),
                      _str(entry.get("state")), _str(entry.get("enqueuedAt")))


def _graphql_message(data: Any) -> str:
    """GraphQL's first error message, or REST's `message`, at most 300 characters."""
    errors = data.get("errors") if isinstance(data, Mapping) else None
    if isinstance(errors, list):
        for error in errors:
            message = _str(_mapping(error).get("message"))
            if message:
                return message[:300]
    return forge_mod._message_of(data)


def merger_for(
    repository_url: str | None,
    *,
    token: str,
    transport: forge_mod.Transport | None = None,
    retry: forge_mod.RetryPolicy | None = None,
) -> ForgeMerger:
    """The `ForgeMerger` for the workflow's repository, or `UnsupportedForge`."""
    ref = forge_mod.parse_repo(repository_url or "")
    if ref is None or ref.host not in MERGEABLE_HOSTS:
        raise UnsupportedForge(
            "the merge step merges on github.com only, and this workflow's repository "
            "is not there"
        )
    client = forge_mod.PinnedForgeClient(token=token, transport=transport, retry=retry)
    return GitHubMerger(client, owner=ref.owner, repo=ref.name)


def mergeable_repository(repository_url: str | None) -> str | None:
    """`owner/repo` when the merge step can act on this repository, else None."""
    ref = forge_mod.parse_repo(repository_url or "")
    if ref is None or ref.host not in MERGEABLE_HOSTS:
        return None
    return ref.full_name


# ---------------------------------------------------------------------------
# What the signed spec names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeTarget:
    #: The task that opened the pull request; None for a named one.
    pull_request: str | None
    #: The review whose verdict file this step staged, when there is one.
    review: str | None = None
    verdict_file: str | None = None
    #: The default branch the tenant registered the repository with, written
    #: by swarm-api at submission (lane MS1); None when it registered none.
    #: The `base_not_default` refusal reads it first, GitHub's own second.
    base: str | None = None
    #: A `merge_pr` pull request (#352): its number and the head to pin,
    #: both set, and `pull_request` None.
    number: int | None = None
    head_sha: str | None = None
    #: A merge-only continuation's (#900): the workflow the continued task
    #: `pull_request` belongs to, which swarm-api read from that task's record
    #: at submission and signed here. The opener's spec is verified against
    #: THIS workflow, not the merge's own; None for a merge inside the
    #: workflow that opened the pull request.
    pull_request_workflow: str | None = None
    #: How this step lands its pull request (TRAIN-WS): `direct`, the merge
    #: call below, or `train`, registered for the repository's merge train
    #: (`mergetrain`). Signed by swarm-api from the workspace's settings at
    #: submission; absent is direct.
    merge_mode: str = mergetrain.DIRECT


class TargetInvalid(ValueError):
    pass


def parse_merge_target(dispatch: Mapping[str, Any]) -> MergeTarget:
    target = _parse_merge_target(dispatch)
    raw = dispatch.get(MERGE_TARGET_FIELD)
    mode = raw.get("merge_mode", mergetrain.DIRECT) if isinstance(raw, Mapping) else None
    if mode not in (mergetrain.DIRECT, mergetrain.TRAIN):
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.merge_mode is not direct or train")
    return replace(target, merge_mode=mode) if mode != target.merge_mode else target


def _parse_merge_target(dispatch: Mapping[str, Any]) -> MergeTarget:
    raw = dispatch.get(MERGE_TARGET_FIELD)
    if not isinstance(raw, Mapping):
        raise TargetInvalid(
            f"this step's signed dispatch block has no {MERGE_TARGET_FIELD} block naming the "
            "task that opened the pull request"
        )
    unknown = sorted(
        str(k) for k in raw
        if k not in ("pull_request", "review", "verdict_file", "base", "number", "head_sha",
                     "pull_request_workflow", "merge_mode")
    )
    if unknown:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD} names {', '.join(unknown)}")
    base = raw.get("base")
    if "base" in raw and (not isinstance(base, str) or not base):
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.base is not a branch name")
    if "number" in raw or "head_sha" in raw:
        return _named_target(raw, base)
    pull_request = _task_id(raw.get("pull_request"))
    if pull_request is None:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.pull_request is not a task id")
    review = raw.get("review")
    verdict_file = raw.get("verdict_file")
    if "pull_request_workflow" in raw:
        # A merge-only continuation's target (#900): the continued task and
        # its workflow, and no review -- `validation.merge_sources` gives a
        # continuation none. A workflow id has a task id's shape.
        workflow = _task_id(raw.get("pull_request_workflow"))
        if workflow is None:
            raise TargetInvalid(f"{MERGE_TARGET_FIELD}.pull_request_workflow is not a workflow id")
        if review is not None or verdict_file is not None:
            raise TargetInvalid(
                f"{MERGE_TARGET_FIELD} names another workflow's pull request and also a review"
            )
        return MergeTarget(pull_request, base=base, pull_request_workflow=workflow)
    if review is None and verdict_file is None:
        return MergeTarget(pull_request, base=base)
    if _task_id(review) is None or not isinstance(verdict_file, str) or not verdict_file:
        raise TargetInvalid(
            f"{MERGE_TARGET_FIELD} names a review without its verdict file, or one without a review"
        )
    return MergeTarget(pull_request, review, verdict_file, base=base)


def _named_target(raw: Mapping[str, Any], base: Any) -> MergeTarget:
    """A `merge_pr` target: a number and a head sha, and nothing a task names."""
    beside = sorted(k for k in ("pull_request", "review", "verdict_file") if k in raw)
    if beside:
        raise TargetInvalid(
            f"{MERGE_TARGET_FIELD} names a pull request by number and also {', '.join(beside)}"
        )
    number = _int(raw.get("number"))
    head = raw.get("head_sha")
    if number is None or number < 1:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.number is not a pull request number")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.head_sha is not a full commit sha")
    return MergeTarget(None, base=base, number=number, head_sha=head)


def _pushed_head(doc: Mapping[str, Any]) -> str | None:
    git = _mapping(_mapping(doc.get("result_summary")).get("git"))
    value = git.get("pushed_head")
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _branch_task(doc: Mapping[str, Any], task_id: str) -> str:
    """The task whose `swarm/<id>` branch the pull request is on.

    The opener's own, or -- for a continuation -- the task it continues,
    read from its SIGNED dispatch block, never from its recorded branch.
    """
    block = _mapping(_mapping(doc.get("metadata")).get("dispatch"))
    return _task_id(block.get("continues")) or task_id


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    ctx: ActionContext
    summary: dict[str, Any] = field(default_factory=dict)
    #: The checks read at a head, as a CI park records them (`latest_reading`):
    #: (head, pending names, failed names). None until this attempt reads them.
    reading: tuple[str, list[str], list[str]] | None = None
    #: The opener's refused workflow publish (#2046): (opener task, the head
    #: it pinned, `_workflow_refusal`). None when it was not refused.
    refused_publish: tuple[str, str, dict[str, Any]] | None = None

    def refuse(self, code: str, message: str) -> ActionOutcome:
        return refusal(self.summary, EndCause.MERGE_REFUSED, code, message)

    def wait_for_owner(self, held: list[dict[str, Any]], *, unreadable: int) -> ActionOutcome:
        """The owner's questions are a hold no tick can clear: park for a person.

        `held` is each still-unanswered `needs_owner` question, or each
        unanswered blocking question a fix round asked (#2898), `unreadable`
        how many entries could not be read. The lifecycle writes the park in
        one fenced transaction (`handoff.park_human_required`), releases the
        lease and exits 75.
        """
        named = [f"step {q.get('step_id') or q.get('task_id')} question {q.get('question_index')}"
                 for q in held]
        if unreadable:
            named.append(f"{unreadable} needs_owner entr{'y' if unreadable == 1 else 'ies'} "
                         "the merge cannot read")
        message = ("the review, or a blocking question of a fix round, says these owner "
                   "questions hold the merge, and they are unanswered: " + "; ".join(named))
        wait = {"code": OWNER_QUESTIONS_OPEN, "questions": held, "unreadable": unreadable}
        self.summary["wait"] = {"code": OWNER_QUESTIONS_OPEN, "message": message}
        return _outcome(
            TaskState.PARKED, None, self.summary, f"{OWNER_QUESTIONS_OPEN}: {message}",
            exit_code=ExitCode.PARKED, owner_wait=wait,
        )

    def wait(self, code: str, message: str, *, head: str, pull_request: int,
             pending: list[str]) -> ActionOutcome:
        """A fact that may change by itself: park CI_PENDING, holding nothing.

        Not a retry: the lifecycle writes the park, the refund and the
        fallback instant in one fenced transaction
        (`control.ControlPlane.park_ci_pending`), releases the lease and exits
        75. `head` is the head the checks were read at, which swarm-api's wake
        tick reads them at again.

        WHAT IS RECORDED (WAIT-RECORD, part of #1782). When this attempt read
        the checks at `head`, the park also records `failed`, the checks whose
        latest run failed, and -- where the caller names no pending check, as
        a red reading's waits do -- the reading's pending ones, so a park on a
        red check still says what else is running. A wait made before any
        check was read at `head` records no `failed` at all: not read is not
        none failed.
        """
        self.summary["wait"] = {"code": code, "message": message}
        ci_wait: dict[str, Any] = {"code": code, "head": head, "pull_request": pull_request,
                                   "pending": sorted(pending)}
        if self.reading is not None and self.reading[0] == head:
            _, read_pending, read_failed = self.reading
            ci_wait["pending"] = sorted(pending) or list(read_pending)
            ci_wait["failed"] = list(read_failed)
        return _outcome(
            TaskState.PARKED, None, self.summary, f"{code}: {message}",
            exit_code=ExitCode.PARKED,
            ci_wait=ci_wait,
        )


def run_merge(ctx: ActionContext) -> ActionOutcome:
    """Merge the workflow's pull request, or refuse with the first failed check."""
    run = _Run(ctx, {"action": "merge", "merged_by_this_task": False})
    summary = run.summary

    # ---- §2.2 1 and 4: no credential in a process that is dumpable, or
    # beside a process that survived the reap.
    if ctx.unprotected:
        return run.refuse("worker_unprotected", ctx.unprotected)
    survivors = ctx.reap()
    if survivors:
        return run.refuse("processes_alive", f"{len(survivors)} process(es) survived the reap")

    # ---- §2.2 5: every claim that needs no credential.
    try:
        target = parse_merge_target(ctx.dispatch)
    except TargetInvalid as exc:
        return run.refuse("merge_target_invalid", str(exc))
    if target.pull_request is None:
        return _merge_named(run, target)
    summary["pull_request_task"] = target.pull_request
    if target.review is not None:
        summary["review_task"] = target.review

    try:
        opener = ctx.fetch_upstream(target.pull_request)
        _verify_opener(ctx, target, opener)
        if target.review is not None:
            ctx.verify_upstream(target.review, ctx.fetch_upstream(target.review))
    except UpstreamSpecUnverified as exc:
        return refusal(summary, EndCause.MERGE_REFUSED, "spec_unverified", str(exc),
                       spec_check=exc.spec_check())
    except InputUnavailable as exc:
        summary["refusal"] = {"code": "upstream_unreadable", "message": str(exc)}
        return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc))

    if target.review is not None:
        staged = ctx.staged.get(target.verdict_file or "")
        if staged is None:
            return run.refuse("verdict_unreadable",
                              f"the review's {target.verdict_file} was not staged")
        try:
            verdict = verdict_mod.read_verdict(
                staged, task_id=target.review, filename=target.verdict_file or ""
            )
        except InputUnavailable as exc:
            return run.refuse("verdict_unreadable", str(exc))
        summary["verdict"] = verdict.verdict
        found = f" ({len(verdict.findings)} finding(s))" if verdict.findings else ""
        # #2764: a NOT_YET that names no owner question refuses here, as it
        # always has; one that names some is held and pick-fixed like a MERGE
        # verdict below, and refused only after.
        if verdict.verdict != "MERGE" and not (
                verdict.needs_owner or verdict.needs_owner_unreadable):
            return run.refuse(
                "verdict_not_merge",
                f"the review's verdict is {verdict.verdict}, not MERGE{found}",
            )
        # #2441: a hold woken because its pull request was merged or closed
        # outside the workflow ends here, before any question is read again:
        # a merged pull request needs no answers.
        ended = _ended_outside(run, target, opener)
        if ended is not None:
            return ended
        # #2070: the owner questions the review said hold the merge, each
        # answer re-read; then the ones it said need not, listed if open.
        held = _unanswered(ctx, verdict.needs_owner)
        if held or verdict.needs_owner_unreadable:
            return run.wait_for_owner(held, unreadable=verdict.needs_owner_unreadable)
        # ANSWER-CONTENT-CHECK: answered is not enough; the pick must be
        # the option the asking branch built -- for every answered question
        # that names `built`, held or not (PICK-FIX-ROUND). One it did not
        # build is a pick-fix round's to build, bounded. #2898: the merge's
        # own fix rounds (`ci-fix:<n>`, `conflict-fix:<n>`, `pick-fix:<n>`)
        # ran after the review, so their questions are read here, and a
        # blocking one still unanswered holds the merge as `needs_owner` does.
        try:
            asked_by_rounds = _round_questions(ctx, target, opener)
            unbuilt = _unbuilt_picks(ctx, target, verdict.needs_owner, asked_by_rounds)
        except InputUnavailable as exc:
            summary["refusal"] = {"code": OWNER_QUESTIONS_UNREADABLE, "message": str(exc)}
            return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc))
        held = [_held_row(row) for row in asked_by_rounds
                if row.get("blocking") is True and row.get("answer") is None]
        if held:
            return run.wait_for_owner(held, unreadable=0)
        if unbuilt:
            fixing = _pick_fix(run, target, opener, unbuilt)
            if fixing is not None:
                return fixing
        # #2764: the owner's answers are in and their picks built, but a
        # NOT_YET verdict's findings still stand: never merged without a
        # review of the head.
        if verdict.verdict != "MERGE":
            return run.refuse(
                "verdict_not_merge",
                f"the review's verdict is {verdict.verdict}, not MERGE{found}; its "
                "needs_owner questions are answered and their picks built, so a "
                "re-review must judge the head",
            )
        still_open = _unanswered(ctx, verdict.open_questions, asked_text=True)
        if still_open:
            summary["open_questions"] = still_open

    number = pull_request_number(opener)
    if number is None:
        return run.refuse("pull_request_unknown",
                          f"task {target.pull_request} recorded no pull request")
    pinned = _pushed_head(opener)
    if pinned is None:
        return run.refuse("head_unknown",
                          f"task {target.pull_request} recorded no pushed head to pin")
    summary["pull_request"] = number
    summary["pinned"] = pinned
    refused = _workflow_refusal(opener)
    if refused is not None:
        run.refused_publish = (target.pull_request, pinned, refused)
    # The step's own record of its CI-fix rounds, and the heads they pushed:
    # task-store reads that need no credential, so before the token.
    unread: list[str] = []
    own = _own_doc(ctx, unread)
    fix_heads = _distinct(_fix_round_heads(ctx, own, opener, target.pull_request, unread)
                          + _ci_fixer_heads(ctx, opener, target.pull_request, number)
                          + _recorded_fix_heads(ctx, opener, target.pull_request, number,
                                                unread))
    if unread:
        summary[FIX_RECORDS_UNREAD_KEY] = unread[:FIX_RECORDS_UNREAD_MAX]
    return _merge_with_token(
        run, number=number, pinned=pinned,
        branch=f"{ctx.branch_prefix}{_branch_task(opener, target.pull_request)}",
        own=own, fix_heads=fix_heads,
    )


def _ended_outside(run: _Run, target: MergeTarget,
                   opener: Mapping[str, Any]) -> ActionOutcome | None:
    """The end of a hold whose pull request was merged or closed outside the workflow.

    MERGE-OUTSIDE-ENDS (part of #2441): JOIN-UI-ADMIN's merge step stayed
    PARKED on an owner question after its PR 2387 was merged by hand, and
    nothing would ever have ended it. swarm-api's wake tick reads a held
    step's pull request and marks the hold `pull_request_merged` or
    `pull_request_closed` (`swarm_api.mergewake`); the scheduler returns it
    to READY on that mark. Only then -- the mark is on a tenant-writable
    document, so it is a reason to look, never a fact -- this reads the pull
    request again with the tenant's token:

      * merged, by anyone: SUCCEEDED, naming the merge commit, who merged it
        and when. Nothing is merged and no question is asked again;
      * closed unmerged: CANCELLED, saying so;
      * open (a forged or stale mark), not this workflow's pull request, or
        unreadable: None, and the hold parks again exactly as before.

    No mark, no read: an ordinary hold stays credential-free.
    """
    ctx, summary = run.ctx, run.summary
    wait = _mapping(_mapping(_own_doc(ctx).get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    # A pick-fix park (PICK-FIX-ROUND) is marked by the tick in `merge_wait`
    # itself, as any CI_PENDING park is; an owner hold in its `owner_hold`.
    picked = wait.get("wake_reason") if wait.get("code") in PICK_FIX_CODES else None
    if (_mapping(wait.get(OWNER_HOLD_KEY)).get("wake_reason") not in OUTSIDE_WAKE_REASONS
            and picked not in OUTSIDE_WAKE_REASONS):
        return None
    assert target.pull_request is not None
    number = pull_request_number(opener)
    if number is None or mergeable_repository(ctx.repository_url) is None \
            or ctx.read_git_token is None:
        return None
    try:
        token = ctx.read_git_token()
    except Exception as exc:  # noqa: BLE001 - never the value; the type is enough
        summary["outside_unread"] = f"the forge token could not be read ({type(exc).__name__})"
        return None
    ctx.register_secret(token)
    try:
        merger = merger_for(ctx.repository_url, token=token, transport=ctx.transport,
                            retry=forge_retry(ctx))
        pr = merger.pull_request(number)
    except Exception as exc:  # noqa: BLE001 - unread is not ended: the hold parks again
        summary["outside_unread"] = (f"pull request #{number} could not be read "
                                     f"({type(exc).__name__})")
        return None
    finally:
        del token
    branch = f"{ctx.branch_prefix}{_branch_task(opener, target.pull_request)}"
    if not pull_request_belongs(
        {"head": {"ref": pr.head_ref, "repo": {"full_name": pr.head_repo}},
         "base": {"repo": {"full_name": pr.base_repo}}},
        author_branch=branch,
    ):
        return None
    summary["pull_request"] = number
    if pr.merged:
        words = f"pull request #{number} was merged outside the workflow"
        if pr.merged_by:
            words += f" by {pr.merged_by}"
        if pr.merged_at:
            words += f" at {pr.merged_at}"
        if pr.merge_commit_sha:
            words += f" as {pr.merge_commit_sha}"
        summary.update({"merged_outside_workflow": True, "merge_commit": pr.merge_commit_sha,
                        "merged_by": pr.merged_by, "merged_at": pr.merged_at,
                        "outcome": words})
        return _outcome(TaskState.SUCCEEDED, None, summary, words)
    if pr.state == "closed":
        message = (f"pull request #{number} was closed without merging, outside the "
                   "workflow; there is nothing left to merge")
        summary["refusal"] = {"code": "pull_request_closed", "message": message}
        return _outcome(TaskState.CANCELLED, EndCause.MERGE_REFUSED, summary,
                        f"pull_request_closed: {message}", exit_code=ExitCode.CANCELLED)
    return None


def _answered(doc: Mapping[str, Any], index: int) -> bool:
    """Whether the task's `metadata.owner_answers` holds an answer to question `index`."""
    for entry in _mapping(doc.get("metadata")).get(OWNER_ANSWERS_KEY) or ():
        if isinstance(entry, Mapping) and type(entry.get("question_index")) is int \
                and entry.get("question_index") == index:
            return True
    return False


def _unbuilt_picks(ctx: ActionContext, target: MergeTarget,
                   refs: tuple[verdict_mod.OwnerQuestionRef, ...],
                   asked_by_rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each answered question whose pick is not the option its branch built.

    Which questions: every `refs` question -- the review's `needs_owner`,
    each answered by now (`_unanswered` found none held) -- and every other
    question a step of this workflow that this one depends on asked and the
    owner answered (`owner_questions.workflow_questions`, from the step's own
    task document, or from its signed target's review and opener when that
    cannot be read): one answered before the review ran is in neither of the
    review's lists, and its pick binds the merge all the same (PICK-FIX-ROUND,
    BGD 2026-10-26). And every answered question in `asked_by_rounds`, the
    questions this merge's own fix rounds asked (`_round_questions`): a round
    is a step appended to the workflow after the merge, which depends on none
    of them (#2898). Each step's validated `questions.json` is read again
    (`owner_questions.questions_of`, which raises `InputUnavailable` for a
    counted file it cannot read, as the walk does for a parent it cannot).

    A question passes when its asker wrote no `built` (a file from before this
    check), `built: null` (the answer changes nothing the branch builds), or
    the answer's `choice` is `built` -- as the LATEST step that asked the same
    question (`owner_questions.normalised` text) records it, nearest step
    first, so a fix step that built the other option after the question was
    asked decides it (#2858). Anything else -- another option, or free
    text with no option -- is a row naming the question, the pick (None for
    free text, with the answer's `text`) and the build.
    """
    asked: dict[str, dict[int, Mapping[str, Any]]] = {}
    keys: list[tuple[str, int]] = []
    steps: dict[tuple[str, int], str | None] = {}
    for ref in refs:
        if ref.task_id not in asked:
            doc = ctx.fetch_upstream(ref.task_id)
            rows = owner_questions_mod.questions_of(
                ref.task_id, doc, store=ctx.store, tenant_id=ctx.tenant_id)
            asked[ref.task_id] = {row["question_index"]: row for row in rows}
        key = (ref.task_id, ref.question_index)
        if key not in keys:
            keys.append(key)
            steps[key] = ref.step_id
    # From the step's own document; where that cannot be read (#2373), from
    # the steps its signed target names, which this attempt has read already.
    start = _own_doc(ctx)
    if not start.get("depends_on"):
        start = {"workflow_id": ctx.workflow_id,
                 "depends_on": [t for t in (target.review, target.pull_request) if t]}
    # The latest build of each question, by its text: the walk is nearest
    # step first, so a fix step that asked the same question again after it
    # built the other option is read before the step that first asked it
    # (#2858: PR 2855's fix built 'apply' after implement recorded
    # 'follow-up', and the owner's 'apply' was refused).
    # The rounds ran after every step the merge depends on, so they are read
    # first, newest first, as `_round_questions` orders them.
    latest: dict[str, str] = {}
    for row in [*asked_by_rounds, *owner_questions_mod.workflow_questions(
            start, fetch=ctx.fetch_upstream, store=ctx.store, tenant_id=ctx.tenant_id)]:
        asked.setdefault(row["task_id"], {}).setdefault(row["question_index"], row)
        key = (row["task_id"], row["question_index"])
        if row.get("answer") is not None and key not in keys:
            keys.append(key)
        text = owner_questions_mod.normalised(row.get("question"))
        if text and isinstance(row.get(owner_questions_mod.BUILT_KEY), str):
            latest.setdefault(text, row[owner_questions_mod.BUILT_KEY])
    out: list[dict[str, Any]] = []
    for task_id, index in keys:
        row = asked.get(task_id, {}).get(index)
        built = row.get(owner_questions_mod.BUILT_KEY) if row is not None else None
        if row is None or not isinstance(built, str):
            continue
        built = latest.get(owner_questions_mod.normalised(row.get("question")), built)
        answer = _mapping(row.get("answer"))
        choice = answer.get("choice")
        if choice == built:
            continue
        found: dict[str, Any] = {
            "task_id": task_id,
            "step_id": row.get("step_id") or steps.get((task_id, index)),
            "question_index": index,
            "question": row.get("question"),
            "picked": choice if isinstance(choice, str) else None,
            "built": built,
        }
        if found["picked"] is None and isinstance(answer.get("text"), str):
            found["text"] = answer["text"]
        out.append(found)
    return out


#: `merge_fix`'s round lists and the step each kind is appended to the
#: workflow as (`swarm_api.mergewake.round_step_id`: `<kind>-fix:<n>`).
_ROUND_STEP_KINDS = (("rounds", "ci"), (CONFLICT_ROUNDS_KEY, "conflict"),
                     (PICK_ROUNDS_KEY, "pick"))


def _round_questions(ctx: ActionContext, target: MergeTarget,
                     opener: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every question this merge's own fix rounds asked, newest round first (#2898).

    swarm-api's wake tick appends each CI-fix, conflict fix and pick-fix round
    to the merge's workflow as a step (`ci-fix:<n>`, `conflict-fix:<n>`,
    `pick-fix:<n>`) AFTER the merge step, so the merge depends on none of them
    and the `depends_on` walk never reaches them: PR 2878 merged with a
    ci-fix:1 exemption the owner had answered "forbid". The rounds are named
    by the step's own `merge_fix` record, and each is held to what
    `_pick_round_doc` holds any round to (the tenant-gated read, this tenant,
    its signed spec, its signed `continues` the branch's root), so a forged
    entry adds no question. A round that fails that, or names no task yet,
    asked nothing this merge reads. Its validated `questions.json` is read as
    any step's is (`owner_questions.questions_of`, raising `InputUnavailable`
    for a counted file it cannot read); each row's `step_id` is the step the
    round is on the workflow as.
    """
    assert target.pull_request is not None
    own = _own_doc(ctx)
    root = _branch_task(opener, target.pull_request)
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for key, kind in _ROUND_STEP_KINDS:
        entries += [(kind, entry) for entry in reversed(_fix_rounds(own, key))]
    # Newest first across the kinds: the claim instants are ISO strings, and
    # sorted() is stable, so a kind's own order holds where they tie.
    entries.sort(key=lambda item: str(item[1].get("claimed_at") or ""), reverse=True)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, entry in entries:
        task_id = _task_id(entry.get("task_id"))
        if task_id is None or task_id in seen:
            continue
        doc = _pick_round_doc(ctx, entry, root=root, target=target.pull_request)
        if doc is None:
            continue
        seen.add(task_id)
        step = f"{kind}-fix:{entry.get('round')}"
        for row in owner_questions_mod.questions_of(
                task_id, doc, store=ctx.store, tenant_id=ctx.tenant_id):
            rows.append({**row, "step_id": step})
    return rows


def _held_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """An unanswered blocking round question as an owner hold names it."""
    return {"task_id": row.get("task_id"), "step_id": row.get("step_id"),
            "question_index": row.get("question_index"), "question": row.get("question")}


def _picks_named(rows: list[Mapping[str, Any]]) -> str:
    """Each question, what the owner picked, what the branch built, `; `-joined."""
    named = []
    for row in rows:
        where = f"step {row.get('step_id') or row.get('task_id')} question {row.get('question_index')}"
        picked = row.get("picked")
        pick = (f"picked {picked!r}" if picked is not None
                else "answered in free text that names no option")
        named.append(f"{where}: {pick}, the branch builds {row.get('built')!r}")
    return "; ".join(named)


def unbuilt_picks_message(rows: list[Mapping[str, Any]]) -> str:
    """The reason a merge does not merge: each question, the pick, the build."""
    return "the owner picked an option this pull request does not build: " + _picks_named(rows)


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def pick_line(row: Mapping[str, Any]) -> str:
    """One unbuilt pick as a `pending` line of the pick-fix park.

    What the tick puts in the round's brief, fenced as data: the asking task
    and question index first (`PICK_LINE_PREFIX`, which the tick parses to
    record which picks a round was sent for), then the step, the pick (or the
    owner's free text), the build, and the question as the asker wrote it.
    """
    picked = row.get("picked")
    pick = (f"picked {picked!r}" if picked is not None
            else f"answered in free text: {_one_line(row.get('text'), PICK_LINE_QUESTION_MAX)!r}")
    return (f"{PICK_LINE_PREFIX}{row.get('task_id')} question {row.get('question_index')} "
            f"(step {row.get('step_id') or row.get('task_id')}): {pick}, the branch builds "
            f"{row.get('built')!r}; question: "
            f"{_one_line(row.get('question'), PICK_LINE_QUESTION_MAX)}")


def _pick_round_doc(ctx: ActionContext, entry: Mapping[str, Any], *, root: str,
                    target: str) -> Mapping[str, Any] | None:
    """The task of pick-fix round `entry`, when it is a round of this branch; else None.

    `merge_fix` is tenant-writable, so the round is held to what
    `_fix_round_heads` holds a CI or conflict round to: read through the
    tenant-gated upstream read, this tenant's, its signed spec verifying
    against the round's own workflow, and its SIGNED `continues` the root
    whose branch the pull request is on.
    """
    task_id = _task_id(entry.get("task_id"))
    if task_id is None or task_id in (target, root, ctx.task_id):
        return None
    try:
        doc = _mapping(ctx.fetch_upstream(task_id))
        workflow = _task_id(entry.get("workflow_id")) or _task_id(doc.get("workflow_id"))
        if workflow is None:
            return None
        ctx.verify_upstream(task_id, doc, of_workflow=workflow)
    except Exception:  # noqa: BLE001 - unreadable or unsigned: not this branch's round
        return None
    if doc.get("tenant_id") != ctx.tenant_id:
        return None
    if _task_id(_mapping(_mapping(doc.get("metadata")).get("dispatch")).get("continues")) != root:
        return None
    return doc


def _round_pushed(doc: Mapping[str, Any], entry: Mapping[str, Any]) -> str | None:
    """The head round `doc` pushed, unless it pushed nothing or the head it started at."""
    pushed = _pushed_head(doc)
    start = entry.get("pull_head") or entry.get("head")
    return pushed if pushed is not None and pushed != start else None


def _round_picks(entry: Mapping[str, Any]) -> set[tuple[Any, Any]]:
    """The `(task, question index)` picks round `entry` was sent for; empty if none."""
    picks = entry.get("picks")
    return set() if not isinstance(picks, list) else {
        (_mapping(p).get("task_id"), _mapping(p).get("question_index")) for p in picks}


def _pick_fix(run: _Run, target: MergeTarget, opener: Mapping[str, Any],
              unbuilt: list[dict[str, Any]]) -> ActionOutcome | None:
    """Picks the branch did not build: a pick-fix round's, or `owner_pick_not_built`.

    PICK-FIX-ROUND (part of #2441; BGD 2026-10-26). None when the step's
    pick-fix rounds built every pick in `unbuilt`, so the merge goes on;
    otherwise the park or the refusal. The shape is `_conflicted`'s:

      * a round claimed and not yet submitted, or still running, parks
        OWNER_PICK_FIX_RUNNING; one whose submission was refused is
        `owner_pick_not_built`;
      * a round counts as building a pick only when its task is this
        branch's round (`_pick_round_doc`), SUCCEEDED, pushed a head other
        than the one it started at, and its `picks-built.json`
        (`owner_questions.picks_built`) says it builds THAT pick -- the
        owner's option as now answered, or the free text -- and the head it
        pushed is accepted by the merge like any round's (`_fix_round_heads`);
      * a round that SUCCEEDED and pushed nothing counts as finding a pick
        it was sent for ALREADY built when its `picks-built.json` says the
        branch builds THAT pick: the merge goes on, and the summary says so
        (`picks_already_built`, #2858) -- the question's recorded `built`
        can be stale, a fix step having built the other option since;
      * once the last round has ended, a round that did not succeed, pushed
        nothing and reported no pick built, or left a pick it was sent for
        unbuilt, refuses: no pick is sent to a second round, so the step
        never loops;
      * a pick no round was sent for (answered after the last one) parks
        OWNER_PICK_FIX_PENDING for the next round, at most
        MERGE_PICK_ROUNDS_MAX of them; past that, `owner_pick_not_built`.

    Before any credential, as the hold is. The park is CI_PENDING at the
    opener's pushed head: it holds nothing (invariant 1), and the next
    attempt is admitted and fenced like any other (invariant 5).
    """
    ctx, summary = run.ctx, run.summary
    assert target.pull_request is not None
    number = pull_request_number(opener)
    if number is None:
        return run.refuse("pull_request_unknown",
                          f"task {target.pull_request} recorded no pull request")
    pinned = _pushed_head(opener)
    if pinned is None:
        return run.refuse("head_unknown",
                          f"task {target.pull_request} recorded no pushed head to pin")
    root = _branch_task(opener, target.pull_request)
    own = _own_doc(ctx)
    rounds = _fix_rounds(own, PICK_ROUNDS_KEY)
    last = rounds[-1] if rounds else None

    def refuse(rows: list[dict[str, Any]], why: str) -> ActionOutcome:
        summary[UNBUILT_PICKS_KEY] = rows
        return run.refuse(OWNER_PICK_NOT_BUILT, f"{unbuilt_picks_message(rows)}; {why}")

    def wait(code: str, rows: list[dict[str, Any]], why: str) -> ActionOutcome:
        summary[UNBUILT_PICKS_KEY] = rows
        waited = _ci_wait_age(ctx)
        if waited is not None:
            return refuse(rows, f"{why}, and the step has waited {int(waited // 60)} min, "
                                f"past {MERGE_CI_MAX_SECONDS // 3600} h")
        return run.wait(code, f"{unbuilt_picks_message(rows)}; {why}", head=pinned,
                        pull_request=number, pending=[pick_line(row) for row in rows])

    if last is not None:
        n = last.get("round")
        if last.get("error"):
            return refuse(unbuilt, f"pick fix round {n} was not run: {str(last['error'])[:300]}")
        task_id = _task_id(last.get("task_id"))
        if task_id is None:
            return wait(OWNER_PICK_FIX_RUNNING, unbuilt, f"pick fix round {n} is being submitted")
        if not _ended(ctx, task_id):
            return wait(OWNER_PICK_FIX_RUNNING, unbuilt,
                        f"pick fix round {n} ({task_id}) is running")

    # Every ended round's report, oldest first, so a later round's word wins.
    # A round that pushed nothing reports picks the branch ALREADY built at
    # the head it started from (#2858): its finding is the build, not the
    # question's stale `built`; it counts only for picks it was sent for.
    reports: dict[tuple[str, int], tuple[str | None, bool]] = {}
    already: set[tuple[str, int]] = set()
    for entry in rounds:
        doc = _pick_round_doc(ctx, entry, root=root, target=target.pull_request)
        if doc is None or doc.get("state") != TaskState.SUCCEEDED.value:
            continue
        pushed = _round_pushed(doc, entry) is not None
        sent = _round_picks(entry)
        if not pushed and not sent:
            continue
        round_task = str(_task_id(entry.get("task_id")))
        try:
            report = owner_questions_mod.picks_built(
                round_task, doc, store=ctx.store, tenant_id=ctx.tenant_id)
        except Exception as exc:  # noqa: BLE001 - an unread report built nothing
            why = str(exc)[:300] if isinstance(exc, InputUnavailable) else type(exc).__name__
            summary.setdefault("pick_reports_unread", []).append(f"{round_task}: {why}")
            continue
        if not pushed:
            report = {key: value for key, value in report.items() if key in sent}
        for key, value in report.items():
            reports[key] = value
            if pushed:
                already.discard(key)
            else:
                already.add(key)

    def built(row: Mapping[str, Any]) -> bool:
        reported = reports.get((row["task_id"], row["question_index"]))
        return reported is not None and reported[1] is True and reported[0] == row.get("picked")

    still = [row for row in unbuilt if not built(row)]
    if not still:
        done = [{"task_id": row["task_id"], "question_index": row["question_index"],
                 "picked": row.get("picked")} for row in unbuilt]
        summary["picks_built_by_rounds"] = [
            row for row in done if (row["task_id"], row["question_index"]) not in already]
        summary["picks_already_built"] = [
            row for row in done if (row["task_id"], row["question_index"]) in already]
        return None

    if last is not None:
        n = last.get("round")
        task_id = _task_id(last.get("task_id"))
        doc = _pick_round_doc(ctx, last, root=root, target=target.pull_request)
        if doc is None:
            return refuse(still, f"pick fix round {n} ({task_id}) is not a round of this "
                                 "pull request's branch")
        state = doc.get("state")
        if state != TaskState.SUCCEEDED.value:
            return refuse(still, f"pick fix round {n} ({task_id}) ended {state} without "
                                 "building it")
        if _round_pushed(doc, last) is None:
            return refuse(still, f"pick fix round {n} ({task_id}) changed nothing: it pushed "
                                 "no new head")
        picks = last.get("picks")
        sent = _round_picks(last) if isinstance(picks, list) else None
        repeated = [row for row in still
                    if sent is None or (row["task_id"], row["question_index"]) in sent]
        if repeated:
            return refuse(repeated, f"still not built after pick fix round {n} ({task_id}), "
                                    "which was sent to build it")
    if len(rounds) >= MERGE_PICK_ROUNDS_MAX:
        return refuse(still, f"after {len(rounds)} pick fix round(s), the most a merge step "
                             "gets")
    return wait(OWNER_PICK_FIX_PENDING, still,
                f"pick fix round {len(rounds) + 1} of {MERGE_PICK_ROUNDS_MAX} is due to build it")


def _unanswered(ctx: ActionContext,
                refs: tuple[verdict_mod.OwnerQuestionRef, ...], *,
                asked_text: bool = False) -> list[dict[str, Any]]:
    """The `refs` still unanswered, read again from each one's task now.

    FAIL CLOSED: a task that cannot be read, or is not a step of this
    workflow, answers nothing -- an answer on another workflow's task is not
    an answer to this one's question. The step id is the task's own when it
    records one, not the verdict's word for it.

    `asked_text` (the listing on the pull request) puts the question's text
    as the asking task's own validated `questions.json` holds it
    (`owner_questions.questions_of`) in place of the review's copy, so a
    review that paraphrased a question does not put the paraphrase on the
    pull request. Where that file cannot be read, or holds no such index, the
    review's copy stays: the listing informs, it gates nothing.
    """
    out: list[dict[str, Any]] = []
    docs: dict[str, Mapping[str, Any] | None] = {}
    asked: dict[str, dict[int, Any]] = {}
    for ref in refs:
        if ref.task_id not in docs:
            try:
                docs[ref.task_id] = ctx.fetch_upstream(ref.task_id)
            except InputUnavailable:
                docs[ref.task_id] = None
        doc = docs[ref.task_id]
        ours = (doc is not None and ctx.workflow_id is not None
                and doc.get("workflow_id") == ctx.workflow_id)
        if ours and doc is not None and _answered(doc, ref.question_index):
            continue
        row = ref.as_dict()
        if ours and doc is not None and isinstance(doc.get("step_id"), str) and doc["step_id"]:
            row["step_id"] = doc["step_id"]
        if asked_text and ours and doc is not None:
            if ref.task_id not in asked:
                asked[ref.task_id] = _asked_questions(ctx, ref.task_id, doc)
            text = asked[ref.task_id].get(ref.question_index)
            if isinstance(text, str) and text.strip():
                row["question"] = text
        out.append(row)
    return out


def _asked_questions(ctx: ActionContext, task_id: str,
                     doc: Mapping[str, Any]) -> dict[int, Any]:
    """`{question_index: question text}` as task `task_id` asked them; {} unreadable."""
    try:
        rows = owner_questions_mod.questions_of(
            task_id, doc, store=ctx.store, tenant_id=ctx.tenant_id)
    except InputUnavailable:
        return {}
    return {row["question_index"]: row.get("question") for row in rows}


def open_questions_lines(rows: list[Mapping[str, Any]]) -> list[str]:
    """The comment's section listing the open questions that did not hold the merge.

    The question text is an agent's, UNTRUSTED, on a page anyone with read
    access sees: inside one fenced block with every run of three backticks
    broken, as `verdict.pull_request_lines` renders findings.
    """
    if not rows:
        return []
    lines = ["", "", "Open owner questions the review judged need not hold this merge "
             "(answer them on their task):", "", "```text"]
    for row in rows:
        text = " ".join(str(row.get("question") or "").split()).replace("```", "ʼʼʼ")
        where = row.get("step_id") or row.get("task_id")
        lines.append(f"- step {where} question {row.get('question_index')} "
                     f"(task {row.get('task_id')}): {text}".replace("```", "ʼʼʼ"))
    lines.append("```")
    return lines


def _verify_opener(ctx: ActionContext, target: MergeTarget, opener: Mapping[str, Any]) -> None:
    """The opener's signed spec, against the workflow IT belongs to (#900).

    Inside one workflow that is this step's own, as for every upstream. A
    merge-only continuation merges a task of an EARLIER workflow -- an issue
    run's integrator, or its last CI-fix round -- so its spec is verified
    against the workflow the merge's own signed target names, which the
    opener's signed `workflow_id` must then equal: a forged or edited spec
    is still `signature_mismatch`, and an honest spec of any other workflow
    still `workflow_mismatch`. Authority is bound separately, by this
    step's own signed spec: a continuation's dispatch block `continues` the
    branch the opener's pull request is on, or the target is not this
    continuation's to merge (`workflow_mismatch` too, fail closed).
    swarm-api bound the target to the issue run's record before signing it
    (`issueci.merge_target_unbound`).
    """
    assert target.pull_request is not None
    if target.pull_request_workflow is None:
        ctx.verify_upstream(target.pull_request, opener)
        return
    ctx.verify_upstream(target.pull_request, opener, of_workflow=target.pull_request_workflow)
    if _task_id(ctx.dispatch.get("continues")) != _branch_task(opener, target.pull_request):
        raise UpstreamSpecUnverified(target.pull_request, "workflow_mismatch")


def _merge_named(run: _Run, target: MergeTarget) -> ActionOutcome:
    """A `merge_pr` target (#352): no opener to read, so straight to the token.

    The pinned head is the sha the caller named, signed; no CI-fix round can
    exist (swarm-api refuses `merge_fix_rounds` beside `merge_pr`), and the
    branch is whatever the pull request's author called it."""
    number, pinned = target.number, target.head_sha
    if number is None or pinned is None:
        return run.refuse("merge_target_invalid",
                          f"{MERGE_TARGET_FIELD} names neither a task nor a pull request")
    run.summary["pull_request"] = number
    run.summary["pinned"] = pinned
    return _merge_with_token(run, number=number, pinned=pinned,
                             branch=None, own=_own_doc(run.ctx), fix_heads=[])


def _merge_with_token(run: _Run, *, number: int, pinned: str, branch: str | None,
                      own: Mapping[str, Any], fix_heads: list[tuple[str, str]]) -> ActionOutcome:
    """§2.2 6-10: read the token, build the merger, and act with it. `branch`
    None skips the branch-name check, and only that, for a named pull request."""
    ctx, summary = run.ctx, run.summary
    repository = mergeable_repository(ctx.repository_url)
    if repository is None:
        return run.refuse("forge_unsupported",
                          "the merge step merges on github.com only, and this workflow's "
                          "repository is not there")
    summary["repository"] = repository
    if ctx.merge_slots is None:
        return run.refuse("merge_slot_unavailable",
                          "this worker has no merge slot store, and a merge without the "
                          "repository's slot races every other merge into the same base")

    # ---- §2.2 6-7: the token, at merge time only.
    from .secrets import CredentialMissing

    if ctx.read_git_token is None:
        return run.refuse("credential_unreadable", "this worker has no forge credential reader")
    try:
        token = ctx.read_git_token()
    except CredentialMissing as exc:
        return _outcome(TaskState.PARKED, None, summary, str(exc), credential_missing=exc)
    except Exception as exc:  # noqa: BLE001 - never the value; the type is enough
        summary["refusal"] = {"code": "credential_unreadable",
                              "message": f"the forge token could not be read ({type(exc).__name__})"}
        return _outcome(TaskState.FAILED, EndCause.CANNOT_START, summary,
                        f"credential_unreadable: {type(exc).__name__}")
    ctx.register_secret(token)
    try:
        merger = merger_for(ctx.repository_url, token=token, transport=ctx.transport,
                            retry=forge_retry(ctx))
    except UnsupportedForge as exc:
        return run.refuse("forge_unsupported", str(exc))
    finally:
        del token
    slot = _slot_use(ctx, repository, own)
    try:
        outcome = _forge_outcome(run, merger, slot, number=number, pinned=pinned,
                                 branch=branch, own=own, fix_heads=fix_heads)
        return _settle(run, slot, merger, outcome)
    finally:
        del merger


def _forge_outcome(run: _Run, merger: ForgeMerger, slot: mergeslot.SlotUse, *, number: int,
                   pinned: str, branch: str | None, own: Mapping[str, Any],
                   fix_heads: list[tuple[str, str]]) -> ActionOutcome:
    """`_with_forge`, with the forge's errors read as the step's outcome."""
    summary = run.summary
    repository = merger.full_name
    try:
        return _with_forge(run, merger, number=number, pinned=pinned,
                           branch=branch, own=own, fix_heads=fix_heads, slot=slot)
    except forge_mod.ForgeRedirectRefused as exc:
        cause = EndCause.MERGE_FAILED if summary.get("merge_called") else EndCause.MERGE_REFUSED
        return refusal(summary, cause, exc.code, str(exc))
    except forge_mod.ForgeUnavailable as exc:
        if summary.get("merge_called"):
            # Never resent: whether it merged is GitHub's to say, and the
            # next reader of this pull request will see.
            return refusal(summary, EndCause.MERGE_FAILED, "merge_unanswered",
                           f"the merge call did not answer ({exc}); whether it merged is "
                           "unknown, so it is not sent again")
        return unavailable(summary, EndCause.MERGE_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        status = getattr(exc, "status", None)
        if status in (401, 403, 404) and not summary.get("merge_called"):
            return run.refuse("token_lacks_rights",
                              f"the tenant's token cannot read {repository} as needed ({exc})")
        cause = EndCause.MERGE_FAILED if summary.get("merge_called") else EndCause.MERGE_REFUSED
        return refusal(summary, cause, getattr(exc, "code", "forge_refused"), str(exc))


def _with_forge(run: _Run, merger: ForgeMerger, *, number: int, pinned: str,
                branch: str | None, own: Mapping[str, Any] | None = None,
                fix_heads: list[tuple[str, str]] | None = None,
                slot: mergeslot.SlotUse | None = None) -> ActionOutcome:
    """§2.2 8-10 and §5, with the token in hand. Raises the forge's errors.

    `pinned` is the head the opening step pushed; `fix_heads` the heads this
    workflow's CI-fix rounds pushed (`_fix_round_heads`). The head this
    attempt acts at is GitHub's live head, accepted only through `_accept`.
    `own` is the step's own document, for its CI-fix rounds. `slot` is the
    repository's merge slot (`mergeslot`): taken once the checks are green,
    confirmed before every forge write, given back by `_settle`.
    """
    own = own or {}
    fix_heads = fix_heads or []
    ctx, summary = run.ctx, run.summary
    if ctx.recheck():
        return cancelled(summary)
    if not merger.can_push():
        return run.refuse("token_lacks_rights",
                          f"the tenant's token cannot write to {merger.full_name}")

    pr = merger.pull_request(number)
    summary["base"] = pr.base_ref
    # The registered default first (swarm-api wrote it into the signed
    # target, MS1), GitHub's own when the tenant registered none. Before the
    # merged row: a merge into another branch closes nothing, as the close
    # script says, so it is not this step's success either.
    default = parse_merge_target(ctx.dispatch).base or merger.default_branch()
    if not default or pr.base_ref != default:
        return run.refuse("base_not_default",
                          f"pull request #{number} targets {pr.base_ref}, not the repository's "
                          f"default branch {default or '(unreadable)'}")
    if slot is not None:
        slot.base = default
    if pr.merged:
        if pr.head_sha is not None and _accept(
            merger, head=pr.head_sha, pinned=pinned, fix_heads=fix_heads, base=default
        ) is not None:
            landed = _landed_by_train(ctx, own, head=pr.head_sha)
            if landed is not None:
                # The repository's merge train landed what this step
                # registered (TRAIN-WS): this step's merge, recorded as the
                # direct merge's is.
                summary["merged_by_this_task"] = True
                summary["merged_through_train"] = True
                summary["merge_train"] = {"entry": landed.get("entry_id"),
                                          "train": landed.get("train_id")}
                summary["merge_commit"] = pr.merge_commit_sha or landed.get("merge_commit")
                _record(summary, merger, number,
                        train_provenance(ctx, merger, number=number, head=pr.head_sha,
                                         base=default, train=landed.get("train_id")))
                _close_issues(run, merger, number)
                return _outcome(TaskState.SUCCEEDED, None, summary, "")
            if _queued_by_this_step(own):
                # The merge queue merged what this step enqueued (C3H): this
                # step's merge, recorded as the direct merge's is.
                summary["merged_by_this_task"] = True
                summary["merged_through_queue"] = True
                summary["merge_commit"] = pr.merge_commit_sha
                _record(summary, merger, number,
                        queued_provenance(ctx, merger, number=number, head=pr.head_sha,
                                          base=default))
                _close_issues(run, merger, number)
                return _outcome(TaskState.SUCCEEDED, None, summary, "")
            # A lost attempt that merged, or another merger after this step's
            # update: the merge stands, and the issues it closes are closed
            # below as if this attempt had made it.
            summary["already_merged"] = True
            summary["merge_commit"] = pr.merge_commit_sha
            _close_issues(run, merger, number)
            return _outcome(TaskState.SUCCEEDED, None, summary, "")
        return run.refuse("merged_at_other_head",
                          f"pull request #{number} was merged at {pr.head_sha}, not {pinned}")
    if pr.state != "open":
        return run.refuse("pull_request_closed", f"pull request #{number} is closed, not merged")
    if pr.head_repo is None or pr.head_repo.lower() != (pr.base_repo or "").lower():
        return run.refuse("from_fork",
                          f"pull request #{number} comes from {pr.head_repo or 'an unknown fork'}, "
                          f"not {merger.full_name}")
    belongs = branch is None or pull_request_belongs(
        {"head": {"ref": pr.head_ref, "repo": {"full_name": pr.head_repo}},
         "base": {"repo": {"full_name": pr.base_repo}}},
        author_branch=branch,
    )
    if not belongs:
        return run.refuse("pull_request_not_this_workflows",
                          f"pull request #{number} is not {branch} from {merger.full_name}")
    if pr.draft:
        return run.refuse("draft", f"pull request #{number} is a draft")
    head = pr.head_sha
    proven: dict[str, str | None] = {}
    accepted = None if head is None else _accept(
        merger, head=head, pinned=pinned, fix_heads=fix_heads, base=default, proven=proven
    )
    if head is None or accepted is None:
        unread = summary.get(FIX_RECORDS_UNREAD_KEY) or []
        record = None if head is None else _head_record(
            merger, head=head, pinned=pinned, fix_heads=fix_heads, base=default, proven=proven)
        person = None if head is None else _person_push(merger, head=head, pr=pr)
        if person is not None:
            return _superseded(run, number=number, head=head, was=pinned, person=person,
                               record=record)
        outcome = run.refuse(
            "head_moved",
            f"pull request #{number}'s head is {head}, not {pinned}, "
            "the head this workflow pushed, nor one of its CI-fix or conflict "
            "fix rounds' heads, nor a head the platform's CI fixer recorded "
            "for this pull request, nor a clean merge of the base into any of them"
            + (f"; and this step could not read {'; '.join(unread)}, so a "
               "round's head there was never a candidate" if unread else "")
            + ("" if record is None else _named(record)))
        if record is not None:
            summary["refusal"]["commit"] = record
        return outcome
    updates, pushed_by, others = accepted
    summary["head"] = head
    summary["updates"] = updates
    if pushed_by is not None:
        summary["head_pushed_by"] = pushed_by
    if others:
        summary[PURE_BASE_MERGES_KEY] = others
    if title_is_placeholder(pr.title):
        return run.refuse("title_placeholder",
                          "the pull request's title is the worker's placeholder")
    if slot is not None:
        # The slot's heartbeat: a holder -- an earlier attempt of this step,
        # or a waiter a release handed it to -- renews its lease on every
        # attempt, whatever this one goes on to read.
        slot.renew(updates=updates)

    def wait(code: str, message: str, pending: list[str]) -> ActionOutcome:
        waited = _ci_wait_age(ctx)
        if waited is not None:
            return run.refuse("checks_timeout",
                              f"CI has not settled in {int(waited // 60)} min, past "
                              f"{MERGE_CI_MAX_SECONDS // 3600} h: {message}")
        return run.wait(code, message, head=head, pull_request=number, pending=pending)

    if _queued_by_this_step(own):
        # An earlier attempt enqueued it (C3H). Open and unmerged at an
        # accepted head: the queue still has it, or took it out.
        return _in_queue(run, merger, number=number, head=head, base=default, wait=wait)

    # Mergeability BEFORE the checks (#1076): GitHub runs no checks on a
    # pull request that conflicts with its base, so waiting for them waits
    # out MERGE_CI_MAX_SECONDS for nothing. `mergeable` None (not computed
    # yet) is not a conflict: the checks are read, and the green path below
    # reads mergeability again before it merges.
    if conflicting(pr):
        return _conflicted(run, own, head=head, number=number, base=default,
                           fixable=branch is not None)

    # The checks, at the head this attempt acts at.
    required = merger.required_checks(pr.base_ref or "")
    checks = merger.checks_at(head, head_ref=pr.head_ref)
    summary["required_checks"] = sorted({c.context for c in required.checks})
    summary["required_checks_source"] = required.source
    pending: list[str] = []
    failed: list[str] = []
    #: None once a failing check is one no re-run replaces (`failing_run_ids`).
    failing_ids: set[int] | None = set()
    #: The runs of each check the decision judges, for `cancelled_since`.
    judged: list[list[dict[str, Any]]] = []
    if required.checks:
        run.reading = (head, *latest_reading(
            {check.context: [c for c in checks if c.name == check.context
                             and (check.app_id is None or c.app_id == check.app_id)]
             for check in required.checks},
            REQUIRED_GREEN,
        ))
        for check in required.checks:
            mine = [c.as_run() for c in checks if c.name == check.context
                    and (check.app_id is None or c.app_id == check.app_id)]
            judged.append(mine)
            state = required_check_state(mine)
            if state == "pending":
                pending.append(check.context)
            elif state == "failed":
                failed.append(f"{check.context} ({required_check_label(mine)})")
                failing_ids = _with_failing(failing_ids, failing_run_ids(mine, REQUIRED_GREEN))
    else:
        # No protection, or protection that requires no check, or a plan that
        # has neither rulesets nor protection to read (`required.source`
        # `none_plan_limited_all_checks`): every check reported at the head
        # must be green, and there must be one.
        by_name: dict[str, list[CheckFacts]] = {}
        for check in checks:
            by_name.setdefault(check.name, []).append(check)
        run.reading = (head, *latest_reading(by_name, OTHER_TOLERATED))
        if not checks:
            return wait("no_checks",
                        f"{pr.base_ref} requires no check and none has reported at "
                        f"{head}; the merge needs at least one green check", [])
        pending, failed = judge_other_checks(checks)
        by_check: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
        for check in checks:
            by_check.setdefault((check.name, check.app_id), []).append(check.as_run())
        for runs in by_check.values():
            failing_ids = _with_failing(failing_ids, failing_run_ids(runs, OTHER_TOLERATED))
        judged = list(by_check.values())
    if failed:
        return _red(run, own, head=head, failed=failed, failing_ids=failing_ids, wait=wait)
    if pending:
        since = cancelled_since(judged)
        if since is not None:
            waited = (utcnow() - since).total_seconds()
            summary["cancelled_since"] = since.isoformat()
            if waited > MERGE_CI_CANCELLED_MAX_SECONDS:
                return run.refuse("checks_cancelled",
                                  f"at {head} the only checks not green were cancelled "
                                  f"{int(waited // 60)} min ago and nothing re-ran them, past "
                                  f"{MERGE_CI_CANCELLED_MAX_SECONDS // 60} min: "
                                  + ", ".join(sorted(pending)))
        return wait("checks_pending", f"at {head}: " + ", ".join(sorted(pending)), pending)

    # Green at this step's own head. In a workspace that merges in trains
    # (TRAIN-WS) the step lands nothing itself: it registers the pull request
    # for the repository's train and parks, and takes no merge slot -- the
    # train coordinator is the repository's one merger. Everything above,
    # conflicts and fix rounds included, ran exactly as in direct mode.
    if parse_merge_target(ctx.dispatch).merge_mode == mergetrain.TRAIN:
        return _train(run, merger, title=pr.title, number=number, head=head, base=default)

    # Green: from here on this step updates and merges, which only the
    # holder of the repository's merge slot may do. Waiting is a park.
    if slot is not None and slot.held is None:
        taken = slot.acquire(updates=updates)
        if isinstance(taken, mergeslot.Waiting):
            return _slot_wait(run, taken, head=head, number=number, base=default)

    rereads = 0
    while pr.mergeable is None and rereads < MERGEABLE_REREADS:
        rereads += 1
        ctx.sleep(MERGEABLE_REREAD_SECONDS)
        pr = merger.pull_request(number)
    if pr.head_sha != head:
        person = None if pr.head_sha is None else _person_push(merger, head=pr.head_sha, pr=pr)
        if person is not None:
            return _superseded(run, number=number, head=pr.head_sha, was=head, person=person)
        return run.refuse("head_moved",
                          f"pull request #{number}'s head moved to {pr.head_sha} while its "
                          f"mergeability was read at {head}")
    if conflicting(pr):
        return _conflicted(run, own, head=head, number=number, base=default,
                           fixable=branch is not None)
    if pr.mergeable is None:
        return wait("mergeability_unknown",
                    f"GitHub had not computed mergeability after {MERGEABLE_REREADS} rereads", [])
    if pr.mergeable_state == "behind":
        return _update_branch(run, merger, number=number, head=head, updates=updates,
                              base=default, slot=slot, own=own, fixable=branch is not None)

    # ---- §2.2 8: fencing and cancel, immediately before the call.
    if ctx.recheck():
        return cancelled(summary)
    lost = _slot_lost(run, slot, head=head, number=number, base=default, updates=updates)
    if lost is not None:
        return lost

    # ---- §5.3: the merge, squashed, pinned to the head the checks are green at.
    message = provenance(ctx, merger, number=number, pinned=head)
    summary["merge_called"] = True
    answer = merger.merge(number, sha=head, title=pr.title, message=message)
    if not answer.merged:
        detail = f": {answer.message}" if answer.message else ""
        if answer.status == 409:
            return run.refuse("head_moved", f"GitHub answered 409: the head is no longer {head}")
        if answer.status in (405, 422):
            if _MERGE_QUEUE.search(answer.message):
                # Answered, so nothing was merged: the base merges only
                # through its merge queue, and the enqueue is a different
                # call, not the merge sent again.
                summary.pop("merge_called")
                summary["merge_answered"] = {"status": answer.status, "message": answer.message}
                return _enqueue(run, merger, number=number, node_id=pr.node_id, head=head,
                                base=default, wait=wait)
            if _OUT_OF_DATE.search(answer.message):
                # Answered, so nothing was merged: the update row, which is a
                # different call, not the merge sent again.
                summary.pop("merge_called")
                summary["merge_answered"] = {"status": answer.status, "message": answer.message}
                return _update_branch(run, merger, number=number, head=head, updates=updates,
                                      base=default, slot=slot, own=own,
                                      fixable=branch is not None)
            if _CONFLICT.search(answer.message):
                # Answered, so nothing was merged: a conflict GitHub found at
                # merge time is handed to a conflict fix round, as one read
                # from `mergeable` is.
                summary.pop("merge_called")
                summary["merge_answered"] = {"status": answer.status, "message": answer.message}
                return _conflicted(run, own, head=head, number=number, base=default,
                                   fixable=branch is not None,
                                   detail=f"GitHub refused the merge ({answer.status}){detail}")
            return run.refuse("protection_refused",
                              f"GitHub's branch protection refused the merge "
                              f"({answer.status}){detail}")
        if answer.status in (401, 403, 404):
            return run.refuse("token_lacks_rights",
                              f"GitHub refused the merge ({answer.status}){detail}")
        return refusal(summary, EndCause.MERGE_FAILED, "forge_refused",
                       f"the merge call answered {answer.status}{detail}")
    summary["merged_by_this_task"] = True
    summary["merge_commit"] = answer.sha

    # ---- §5.4: the record, then the issues. The merge stands whatever these
    # answer; each failure is recorded, never raised.
    _record(summary, merger, number, message)
    _close_issues(run, merger, number)
    return _outcome(TaskState.SUCCEEDED, None, summary, "")


def _train(run: _Run, merger: ForgeMerger, *, title: str, number: int, head: str,
           base: str) -> ActionOutcome:
    """Register the pull request for the repository's merge train, then park.

    Behind the fencing and cancel recheck, as every write is: a superseded
    attempt registers nothing (invariant 5). An entry the train EJECTED ends
    the step `train_ejected`, with the train's reason; any other state parks
    on `TRAIN_WAITING` until the coordinator marks the park or the fallback
    instant passes. A RETURNED entry, or one at an older head, is queued
    again by `mergetrain.register_entry`, keeping its place.
    """
    ctx, summary = run.ctx, run.summary
    trains = ctx.merge_trains
    if trains is None:
        return run.refuse("merge_train_unavailable",
                          "this step merges through the repository's merge train, and the "
                          "worker was given no train store to register it in")
    if ctx.recheck():
        return cancelled(summary)
    entry = trains.register({
        "task_id": ctx.task_id,
        "repository": merger.full_name,
        "repository_url": ctx.repository_url or "",
        "base": base,
        "number": number,
        "head": head,
        "title": title,
        "message": provenance(ctx, merger, number=number, pinned=head),
    })
    state = str(entry.get("state") or mergetrain.QUEUED)
    summary["merge_train"] = {"entry": entry.get("entry_id"), "state": state,
                              "train": entry.get("train_id")}
    if state == mergetrain.EJECTED:
        reason = str(entry.get("reason") or "no reason recorded")
        detail = str(entry.get("detail") or "")
        return run.refuse("train_ejected",
                          f"pull request #{number} was ejected from {base}'s merge train at "
                          f"{head}: {reason}" + (f": {detail}" if detail else ""))
    return run.wait(mergetrain.TRAIN_WAITING,
                    f"pull request #{number} is {state} in {base}'s merge train at {head}",
                    head=head, pull_request=number, pending=[])


def _landed_by_train(ctx: ActionContext, own: Mapping[str, Any], *,
                     head: str) -> Mapping[str, Any] | None:
    """This step's entry, when its last park was on the train and the train
    recorded it LANDED at `head`; None otherwise (then a merged pull request
    is `already_merged`, as before). The entry is tenant-writable: a forged
    one can at worst credit this step with a merge GitHub already shows."""
    wait = _mapping(_mapping(own.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    if wait.get("code") != mergetrain.TRAIN_WAITING or ctx.merge_trains is None:
        return None
    entry = ctx.merge_trains.peek(ctx.task_id)
    if entry is None or entry.get("state") != mergetrain.LANDED or entry.get("head") != head:
        return None
    return entry


def train_provenance(ctx: ActionContext, merger: ForgeMerger, *, number: int, head: str,
                     base: str, train: Any) -> str:
    """The pull request comment for a merge the train made (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}, "
        f"attempt {ctx.attempt_id}) into {merger.full_name}#{number},",
        f"through {base}'s merge train{f' {train}' if train else ''}, squashed at {head}, "
        "the head this task registered, after the base's required checks were green on "
        "the train's composed head.",
    ])


def _record(summary: dict[str, Any], merger: ForgeMerger, number: int, message: str) -> None:
    """The comment naming the task that merged the pull request, and the owner
    questions still open (#2070); a failure is recorded."""
    rows = summary.get("open_questions")
    if isinstance(rows, list) and rows:
        message = message + "\n".join(open_questions_lines(rows))
    try:
        summary["recorded_on_pull_request"] = merger.comment(number, message)
    except forge_mod.ForgeError as exc:
        summary["recorded_on_pull_request"] = False
        summary["recorded_on_pull_request_reason"] = str(exc)[:300]


# ---------------------------------------------------------------------------
# A base that merges only through a merge queue (lane C3H)
# ---------------------------------------------------------------------------


def _queued_by_this_step(own: Mapping[str, Any]) -> bool:
    """Whether this step's last park was in the merge queue.

    `merge_wait.code` on the step's own document, written by the fenced park.
    The document is tenant-writable, so a forged code can at worst make the
    step read the queue and refuse `merge_dequeued`, which changes nothing on
    the forge -- `_ci_wait_age`'s rule.
    """
    wait = _mapping(_mapping(own.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    return wait.get("code") == MERGE_QUEUED


def _queued_wait(run: _Run, state: QueueFacts, *, number: int, head: str, base: str,
                 wait: Any) -> ActionOutcome:
    run.summary["merge_queued"] = state.record(head)
    where = f" at position {state.position}" if state.position is not None else ""
    return wait(MERGE_QUEUED,
                f"pull request #{number} is in {base}'s merge queue{where} at {head} "
                f"(entry {state.entry or 'unnamed'}, {state.state or 'state unknown'})", [])


def _enqueue(run: _Run, merger: ForgeMerger, *, number: int, node_id: str | None, head: str,
             base: str, wait: Any) -> ActionOutcome:
    """GitHub's merge queue takes the pull request at `head`; then park on it.

    Behind the same fencing recheck as the merge: a superseded worker
    enqueues nothing (invariant 5). A refusal from GitHub is read against the
    queue once -- a lost answer, an operator or auto-merge may have queued it
    first -- and is `merge_queue_refused` only when it is not there.
    """
    ctx, summary = run.ctx, run.summary
    if node_id is None:
        return run.refuse("merge_queue_refused",
                          f"{base} merges through a merge queue, and GitHub gave no id for "
                          f"pull request #{number} to enqueue it by")
    if ctx.recheck():
        return cancelled(summary)
    answer = merger.enqueue(number, node_id=node_id, expected_head_sha=head)
    state = answer.entry
    if not answer.enqueued or state is None:
        state = merger.queue_state(number)
        if not state.queued:
            return run.refuse("merge_queue_refused",
                              f"{base} merges through a merge queue, and GitHub refused to "
                              f"add pull request #{number} to it: {answer.message}")
    return _queued_wait(run, state, number=number, head=head, base=base, wait=wait)


def _in_queue(run: _Run, merger: ForgeMerger, *, number: int, head: str, base: str,
              wait: Any) -> ActionOutcome:
    """The wake of a queued park: still queued parks again; out of the queue
    unmerged refuses with GitHub's reason, and is never enqueued again -- a
    merge group that failed would fail again."""
    state = merger.queue_state(number)
    if state.queued:
        return _queued_wait(run, state, number=number, head=head, base=base, wait=wait)
    return run.refuse("merge_dequeued",
                      f"pull request #{number} left {base}'s merge queue unmerged: "
                      f"{state.removed_reason or 'GitHub recorded no reason'}")


def queued_provenance(ctx: ActionContext, merger: ForgeMerger, *, number: int,
                      head: str | None, base: str) -> str:
    """The pull request comment for a merge the queue made (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}, "
        f"attempt {ctx.attempt_id}) into {merger.full_name}#{number},",
        f"through {base}'s merge queue, which this task enqueued at {head}, the head "
        "the workflow pushed or GitHub's update of it onto the base, with every "
        "required check green there.",
    ])


def _updates_onto(merger: ForgeMerger, *, head: str, pushed: str, base: str,
                  proven: dict[str, str | None] | None = None
                  ) -> tuple[int, list[dict[str, Any]]] | None:
    """How many base merges lead from `pushed` to `head`, and the record of
    each GitHub did not commit, or None.

    The first-parent walk (docs/merge-step.md "Revised 2026-10-06" §1): from
    the live head, each step down must be a two-parent commit whose second
    parent is already on `base`, and either GitHub itself committed and
    signed it or its tree is the clean merge of its two parents
    (`_merge_impurity`; BGD 2026-10-26, F05 of #2379). Its first parent is
    the next step. It must reach `pushed` within MERGE_MAX_HEAD_UPDATES
    steps. A fact about GitHub, never about the tenant-writable task
    document, so a lost attempt's update is re-derived here and a forged
    count changes nothing. Anything else is `head_moved`. `proven` caches
    `_merge_impurity` by commit across the candidates `_accept` walks.
    """
    proven = {} if proven is None else proven
    steps = 0
    others: list[dict[str, Any]] = []
    while head != pushed:
        if steps >= MERGE_MAX_HEAD_UPDATES:
            return None
        try:
            commit = merger.commit(head)
            if len(commit.parents) != 2:
                return None
            if not merger.on_base(base, commit.parents[1]):
                return None
            if not commit.by_github:
                if commit.sha not in proven:
                    proven[commit.sha] = _merge_impurity(merger, commit)
                if proven[commit.sha] is not None:
                    return None
                others.append(_commit_record(commit))
        except forge_mod.ForgeAnswered as exc:
            # A commit or comparison GitHub does not have is not an update.
            if getattr(exc, "status", None) in (404, 422):
                return None
            raise
        head = commit.parents[0]
        steps += 1
    return steps, others


def _person_pusher(commit: CommitFacts, pr: PullRequestFacts) -> str | None:
    """The login of the person on `pr` who made `commit`, or None (#2505).

    A person's push is a commit that is not a merge (BGD's decision is about
    a person's content commit: a two-parent merge the purity proof refuses
    stays `head_moved`, since the proof may refuse what git accepts and that
    is the platform's to look at), that the platform did not make -- not
    under one of its own committer addresses, nor ending with the worker's
    line -- and that GitHub links to a `User` account which is the pull
    request's author or one of its assignees. GitHub commits a web-UI edit
    itself, so for those the person is the commit's author. Anyone else is
    an unknown branch-mate, and their head is `head_moved` as before.
    """
    if len(commit.parents) >= 2 or commit.by_worker:
        return None
    email = (commit.committer_email or "").lower()
    if email in PLATFORM_COMMITTER_EMAILS:
        return None
    login = commit.author_login if email == GITHUB_COMMITTER_EMAIL else commit.committer_login
    if login is None:
        return None
    people = {p.lower() for p in pr.people}
    return login if login.lower() in people else None


def _person_push(merger: ForgeMerger, *, head: str, pr: PullRequestFacts) -> str | None:
    """`_person_pusher` of the commit at `head`, read best-effort: a read that
    fails names nobody, and the refusal stays `head_moved`."""
    try:
        return _person_pusher(merger.commit(head), pr)
    except (forge_mod.ForgeError, ValueError):
        return None


def _superseded(run: _Run, *, number: int, head: str, was: str, person: str,
                record: Mapping[str, Any] | None = None) -> ActionOutcome:
    """A person on the pull request pushed `head` over the head this step
    acts at: refused as `head_moved` is -- nothing merges -- and named
    `SUPERSEDED_BY_PERSON_PUSH`, with who and, where read, the commit."""
    outcome = run.refuse(
        SUPERSEDED_BY_PERSON_PUSH,
        f"pull request #{number}'s head is {head}, a commit {person} pushed, a person on "
        f"the pull request, over {was}: the merge is theirs now, and this step merges "
        "nothing a person pushed" + ("" if record is None else _named(record)))
    run.summary["refusal"]["person"] = person
    if record is not None:
        run.summary["refusal"]["commit"] = record
    return outcome


def _named(record: Mapping[str, Any]) -> str:
    """`_head_record`, as the clause a `head_moved` message ends with."""
    if "unreadable" in record:
        return f"; {record['sha']} could not be read ({record['unreadable']})"
    parents = ", ".join(str(p)[:12] for p in record.get("parents") or []) or "none"
    signed = "signed" if record.get("verified") else "unsigned"
    return (f"; {record['sha']} was committed by {record.get('committer_email') or 'nobody named'} "
            f"({signed}), parents {parents}: {record.get('why') or 'not taken'}")


def _commit_record(commit: CommitFacts) -> dict[str, Any]:
    """Who made a commit the walk read, as the step's summary records it."""
    return {"sha": commit.sha, "parents": list(commit.parents),
            "committer_email": commit.committer_email, "verified": commit.verified}


def _merge_impurity(merger: ForgeMerger, commit: CommitFacts) -> str | None:
    """None when `commit`'s tree is the clean merge of its two parents; else why not.

    What auto-merge.yml's `is_pure_base_merge` asks of `git merge-tree`,
    asked of GitHub's tree API, since the merge step has no clone. Path by
    path over the merge base B, the first parent O and the second T (each a
    (mode, sha), or absent): O and T agree, or one of them is B and the other
    is taken, and the merge must carry exactly that. Then `_rename_risk`.

    THIS PROOF MAY REFUSE WHAT GIT ACCEPTS, NEVER ACCEPT WHAT GIT REFUSES. So a
    path both parents changed differently is not shown clean, even on lines
    apart: a line merge here (difflib) aligned lines differently from git's
    diff and took merges git conflicts on or merges to other bytes (the
    review of FIX-MERGE-PURE-BASE-MERGE measured 107 of 1427). Nor is a tree
    GitHub truncated. A merge not shown clean stays head_moved.
    """
    ours, theirs = commit.parents
    base = merger.merge_base(ours, theirs)
    if base is None:
        return "its parents share no merge base"
    tree_of: dict[str, TreeFacts] = {}
    for name, sha in (("the merge base", base), ("its first parent", ours),
                      ("its second parent", theirs), ("the merge", commit.sha)):
        tree = commit.tree if sha == commit.sha else merger.commit(sha).tree
        if tree is None:
            return f"{name}'s tree is unreadable"
        facts = merger.tree(tree)
        if facts.truncated:
            return f"GitHub truncated {name}'s tree"
        tree_of[sha] = facts
    b, o, t, m = (tree_of[sha].entries for sha in (base, ours, theirs, commit.sha))
    for path in sorted(set(b) | set(o) | set(t) | set(m)):
        at_b, at_o, at_t, at_m = b.get(path), o.get(path), t.get(path), m.get(path)
        if at_o == at_t:
            want = at_o
        elif at_o == at_b:
            want = at_t
        elif at_t == at_b:
            want = at_o
        else:
            return f"both parents change {path}"
        if at_m != want:
            return f"{path} is not the clean merge of its parents"
    return _rename_risk(b, o, t)


def _rename_risk(base: Mapping[str, Any], *sides: Mapping[str, Any]) -> str | None:
    """Why git's rename detection might merge these trees differently from
    the path-wise walk, or None.

    The walk merges path by path; git pairs a side's deleted path with one
    it added as a rename, and a directory a side removes entirely as a
    directory rename. A rename on one side meets the other only through its
    source, its target or that directory, so the trees are refused when one
    side both deletes and adds paths (it may have renamed) and the other
    changes or deletes a path it deleted (rename/delete, rename/rename) or
    adds a path it added (rename/add); or when one side adds a path under a
    directory the other removed entirely (git moves it, and merge-tree
    reports the move as a conflict).
    """
    for side, other in ((sides[0], sides[1]), (sides[1], sides[0])):
        removed = sorted(p for p in base if p not in side)
        added = sorted(p for p in side if p not in base)
        if removed and added:
            for path in removed:
                if other.get(path) != base[path]:
                    return (f"{path} is deleted by one parent, which may have renamed it, "
                            "and changed or deleted by the other")
            for path in added:
                if path in other:
                    return (f"{path} is added by one parent, which may have renamed a file "
                            "to it, and by the other")
        gone = {d for p in base for d in _dirs(p)} - {d for p in side for d in _dirs(p)}
        for path in sorted(p for p in other if p not in base):
            for d in _dirs(path):
                if d in gone:
                    return (f"{path} is added in {d}/, which the other parent removes "
                            "entirely, so git may move it as a directory rename")
    return None


def _dirs(path: str) -> list[str]:
    """Every directory `path` sits under, outermost first."""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _update_branch(run: _Run, merger: ForgeMerger, *, number: int, head: str, updates: int,
                   base: str, slot: mergeslot.SlotUse | None = None,
                   own: Mapping[str, Any] | None = None,
                   fixable: bool = False) -> ActionOutcome:
    """GitHub merges the base into a branch that is behind; then park at the new head.

    A write to the forge, so the fencing and cancel recheck runs first and a
    stale worker raises out of it with no call made (invariant 5), and then
    the slot is confirmed this step's. `expected_head_sha` is the head the
    checks were read at: GitHub refuses the update if anyone pushed in
    between.

    THE ALLOWANCE COUNTS ONLY WHAT MOVED THE BASE WHILE THIS STEP HELD THE
    SLOT. Updates already on the head when the slot was taken
    (`Held.updates_at_acquire`) were caused by the merges this step queued
    behind, and the first update after taking it catches up with them; any
    further one means the base moved under a held slot, which no SwarmCloud
    merge does. `MERGE_MAX_BRANCH_UPDATES` of those is `behind_too_often`.
    """
    ctx, summary = run.ctx, run.summary
    held = slot.held if slot is not None else None
    since = max(0, updates - (held.updates_at_acquire if held is not None else 0))
    summary["updates_while_held"] = since
    if since > MERGE_MAX_BRANCH_UPDATES or updates >= MERGE_MAX_HEAD_UPDATES:
        return run.refuse("behind_too_often",
                          f"pull request #{number} is behind {base} again after {since} "
                          f"update(s) while it held the merge slot ({updates} in all; at most "
                          f"{MERGE_MAX_BRANCH_UPDATES} past the catch-up, "
                          f"{MERGE_MAX_HEAD_UPDATES} in all): the base moves under the slot "
                          "faster than CI")
    if ctx.recheck():
        return cancelled(summary)
    lost = _slot_lost(run, slot, head=head, number=number, base=base, updates=updates)
    if lost is not None:
        return lost
    answer = merger.update_branch(number, expected_head_sha=head)
    detail = f": {answer.message}" if answer.message else ""
    if answer.status == 422:
        if _CONFLICT.search(answer.message):
            # GitHub could not merge the base in by itself: a conflict fix
            # round's to resolve (#1076), as a conflict read from `mergeable`.
            summary["update_answered"] = {"status": answer.status, "message": answer.message}
            return _conflicted(run, own or {}, head=head, number=number, base=base,
                               fixable=fixable, detail=f"GitHub could not merge {base} into "
                               f"#{number}{detail}")
        return run.refuse("head_moved",
                          f"GitHub refused to update #{number} at {head}{detail}")
    if answer.status in (401, 403, 404):
        return run.refuse("token_lacks_rights",
                          f"GitHub refused to update #{number} ({answer.status}){detail}")
    if answer.status not in (200, 202):
        return run.refuse("forge_refused",
                          f"the update-branch call answered {answer.status}{detail}")
    # GitHub makes the merge commit ASYNCHRONOUSLY: the 202 only says it will.
    # The new head is read again a bounded few times (invariant 4: seconds,
    # not a provider wait). A head that moved is parked at, and the tick reads
    # the checks there. One that has not is parked at as it is, with
    # BRANCH_UPDATE_PENDING, on which the tick waits for the head to move
    # rather than reading this head's checks -- green, since it was only
    # behind -- and the fallback instant covers an update GitHub never makes.
    # Either way nothing merges here: the old head is never merged after an
    # update was asked for, and the next wake re-reads everything.
    new_head = merger.pull_request(number).head_sha
    rereads = 0
    while (not new_head or new_head == head) and rereads < MERGEABLE_REREADS:
        rereads += 1
        ctx.sleep(MERGEABLE_REREAD_SECONDS)
        new_head = merger.pull_request(number).head_sha
    if not new_head or new_head == head:
        summary["branch_updated"] = {"from": head, "to": None, "updates": updates + 1}
        return run.wait(BRANCH_UPDATE_PENDING,
                        f"GitHub accepted the update of #{number} at {head} and had not made "
                        f"it after {MERGEABLE_REREADS} rereads; the step waits for the new head",
                        head=head, pull_request=number, pending=[])
    summary["branch_updated"] = {"from": head, "to": new_head, "updates": updates + 1}
    return run.wait("branch_updated",
                    f"GitHub merged {base} into #{number} at {head}; the checks run again "
                    f"at {new_head}",
                    head=new_head, pull_request=number, pending=[])


def _accept(merger: ForgeMerger, *, head: str, pinned: str,
            fix_heads: list[tuple[str, str]], base: str,
            proven: dict[str, str | None] | None = None
            ) -> tuple[int, str | None, list[dict[str, Any]]] | None:
    """(base merges on top, the CI-fix round's task or None, the record of each
    base merge GitHub did not commit), or None.

    The live head is accepted when it is the pinned head or a head one of
    this workflow's CI-fix rounds pushed (`fix_heads`, newest first), itself
    or under clean base merges (`_updates_onto`). Exact matches are tried
    first, so the common case reads nothing more from GitHub. `proven` caches
    `_merge_impurity` by commit, and is shared with `_head_record`.
    """
    candidates: list[tuple[str | None, str]] = [(None, pinned), *fix_heads]
    for task_id, pushed in candidates:
        if head == pushed:
            return 0, task_id, []
    proven = {} if proven is None else proven
    for task_id, pushed in candidates:
        walked = _updates_onto(merger, head=head, pushed=pushed, base=base, proven=proven)
        if walked is not None:
            return walked[0], task_id, walked[1]
    return None


def _head_record(merger: ForgeMerger, *, head: str, pinned: str,
                 fix_heads: list[tuple[str, str]], base: str,
                 proven: dict[str, str | None] | None = None) -> dict[str, Any]:
    """The commit a `head_moved` refusal names: its parents, who committed it,
    whether it is signed, whether its first parent is an accepted head, and
    the first reason the walk did not take it. Read best-effort: a read that
    fails says so in `unreadable`, and the refusal stands either way.
    `proven` is the walk's `_merge_impurity` cache, so no tree is proved twice."""
    proven = {} if proven is None else proven
    record: dict[str, Any] = {"sha": head}
    try:
        commit = merger.commit(head)
        record.update(_commit_record(commit))
        first = commit.parents[0] if commit.parents else None
        accepted = first is not None and _accept(
            merger, head=first, pinned=pinned, fix_heads=fix_heads, base=base,
            proven=proven) is not None
        record["first_parent_accepted"] = accepted
        if len(commit.parents) != 2:
            record["why"] = "it is not a two-parent merge"
        elif not accepted:
            record["why"] = "its first parent is not an accepted head"
        elif not merger.on_base(base, commit.parents[1]):
            record["why"] = f"its second parent is not on {base}"
        elif not commit.by_github:
            if commit.sha not in proven:
                proven[commit.sha] = _merge_impurity(merger, commit)
            record["why"] = proven[commit.sha] or "it is the clean merge of its parents"
    except (forge_mod.ForgeError, ValueError) as exc:
        record["unreadable"] = f"{type(exc).__name__}: {getattr(exc, 'status', None) or exc}"[:200]
    return record


# ---------------------------------------------------------------------------
# The repository's merge slot (merge race, #295; `mergeslot`)
# ---------------------------------------------------------------------------

#: The park codes on which a holder gives the slot back: a CI-fix or conflict
#: fix round runs an agent for as long as it takes, and a merge queue
#: serialises the merges itself. The next green reading takes the slot again.
_RELEASE_ON_PARK = frozenset({CI_FIX_PENDING, CI_FIX_RUNNING, CONFLICT_FIX_PENDING,
                              CONFLICT_FIX_RUNNING, MERGE_QUEUED})


def _slot_use(ctx: ActionContext, repository: str,
              own: Mapping[str, Any]) -> mergeslot.SlotUse:
    """This attempt's view of its repository's slot. FIFO is by the step's
    `created_at`, its submission; one that cannot be read queues as of now."""
    slots = ctx.merge_slots
    submitted = own.get("created_at")
    if not isinstance(submitted, datetime) or submitted.tzinfo is None:
        submitted = slots.now()
    return mergeslot.SlotUse(
        slots=slots,
        me=mergeslot.Claimant(ctx.task_id, ctx.attempt_id, submitted),
        repository=repository,
        task_state=lambda task_id: _task_state(ctx, task_id),
        log=ctx.log,
    )


def _task_state(ctx: ActionContext, task_id: str) -> TaskState | None:
    """A task's state through the tenant-gated read; None when it cannot be read."""
    try:
        return TaskState(_mapping(ctx.fetch_upstream(task_id)).get("state"))
    except Exception:  # noqa: BLE001 - unreadable is not ended: a live holder keeps its slot
        return None


def _slot_wait(run: _Run, waiting: mergeslot.Waiting, *, head: str, number: int,
               base: str) -> ActionOutcome:
    """Another step holds the slot: park CI_PENDING as `MERGE_SLOT_WAIT`, holding nothing."""
    return run.wait(mergeslot.MERGE_SLOT_WAIT,
                    f"#{number} is green at {head} and waits for {base}'s merge slot, held by "
                    f"{waiting.holder or 'nobody'}: {waiting.position} of {waiting.waiters} "
                    "waiting, in submission order",
                    head=head, pull_request=number, pending=[])


def _slot_lost(run: _Run, slot: mergeslot.SlotUse | None, *, head: str, number: int,
               base: str, updates: int) -> ActionOutcome | None:
    """None when this attempt still holds its slot, immediately before a forge write.

    A slot that ran out and was taken over is queued for again, as on any
    green reading: a park, or -- handed straight back -- the write goes on
    at the new generation."""
    if slot is None:
        return None
    try:
        slot.confirm()
        return None
    except mergeslot.SlotLost as exc:
        run.summary["merge_slot_lost"] = str(exc)
    taken = slot.acquire(updates=updates)
    if isinstance(taken, mergeslot.Waiting):
        return _slot_wait(run, taken, head=head, number=number, base=base)
    return None


def _settle(run: _Run, slot: mergeslot.SlotUse, merger: ForgeMerger,
            outcome: ActionOutcome) -> ActionOutcome:
    """Give the slot back on every exit that ends the step, or stops it merging.

    Merged, refused, failed and cancelled release it, handing it to the first
    live waiter and waking that waiter's park. A CI wait while holding keeps
    it (the holder renews it on its next attempt); a CI-fix round or a merge
    queue gives it back (`_RELEASE_ON_PARK`); a retry keeps it, and if the
    retry never comes the lease runs out. A crash releases nothing: the lease
    does, or the first waiter that reads the holder's task ended. A stale
    worker never reaches here -- `FencedError` raises past it.

    A slot write that fails is recorded and the outcome stands: what the
    step did on the forge is done, and the lease frees the slot.
    """
    wait = outcome.ci_wait
    code = wait.get("code") if isinstance(wait, Mapping) else None
    try:
        if code == mergeslot.MERGE_SLOT_WAIT or outcome.retryable or (
            outcome.credential_missing is not None
        ):
            pass
        elif wait is not None and code not in _RELEASE_ON_PARK:
            pass
        else:
            if slot.base is None:
                slot.base = _slot_base(run.ctx, merger)
            refused = _mapping(outcome.summary.get("refusal")).get("code")
            slot.release(str(code or refused or outcome.state.value.lower()))
    except FencedError:
        raise
    except Exception as exc:  # noqa: BLE001 - the lease frees what a failed write kept
        slot.record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if run.ctx.log is not None:
            run.ctx.log.warning("merge slot not released", error=type(exc).__name__)
    if slot.record:
        outcome.summary["merge_slot"] = dict(slot.record)
    return outcome


def _slot_base(ctx: ActionContext, merger: ForgeMerger) -> str | None:
    """The base an exit before the pull request was read releases the slot of."""
    try:
        base = parse_merge_target(ctx.dispatch).base
    except TargetInvalid:
        return None
    return base or merger.default_branch()


def _unread(ctx: ActionContext, unread: list[str] | None, what: str, exc: Exception) -> None:
    """Note in `unread` that `what` could not be read, and why (#2373).

    A record the step cannot read names no round, so the head that round
    pushed is `head_moved` -- the right answer, and on 2026-10-25 the only
    one four MERGE-verdict PRs got, with nothing saying the read was what
    failed (a tenant on the state routes, whose `state/upstream` serves
    neither the step's own task nor a round's). The refusal now says so.
    The reason is the exception's own text, scrubbed and bounded: an id and
    a refusal code, never a document.
    """
    if unread is None:
        return
    reason = ctx.scrub(str(exc) or type(exc).__name__)[:FIX_RECORDS_UNREAD_CHARS]
    unread.append(f"{what} ({reason})")


def _own_doc(ctx: ActionContext, unread: list[str] | None = None) -> Mapping[str, Any]:
    """The step's own task document, or {} when it cannot be read."""
    try:
        return _mapping(ctx.fetch_upstream(ctx.task_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable record has no rounds
        _unread(ctx, unread, f"its own task {ctx.task_id}, which records its rounds", exc)
        return {}


def _fix_rounds(own: Mapping[str, Any], key: str = "rounds") -> list[Mapping[str, Any]]:
    """The tick's record of one kind of round: CI-fix `rounds`, or `CONFLICT_ROUNDS_KEY`."""
    rounds = _mapping(_mapping(own.get("metadata")).get(MERGE_FIX_METADATA_KEY)).get(key)
    return [r for r in rounds if isinstance(r, Mapping)] if isinstance(rounds, list) else []


_ROUND_KINDS = (("CI", "rounds"), ("conflict", CONFLICT_ROUNDS_KEY), ("pick", PICK_ROUNDS_KEY))


def _live_round(ctx: ActionContext, own: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """The first fix round of any kind whose task is still running, at any head (#2577).

    One writer per pull request: while a round's task has not ended, the
    next round would push onto the same branch and fail to replay over it.
    A round without a task (claimed, not submitted yet) is the tick's to
    hold for LOST_ROUND_SECONDS; an errored one never ran."""
    for kind, key in _ROUND_KINDS:
        for entry in _fix_rounds(own, key):
            if entry.get("error"):
                continue
            task_id = _task_id(entry.get("task_id"))
            if task_id is not None and not _ended(ctx, task_id):
                return kind, entry
    return None


def _with_failing(ids: set[int] | None, more: set[int] | None) -> set[int] | None:
    """The union of two `failing_run_ids` answers; None when either is None."""
    return None if ids is None or more is None else ids | more


def _rerun_settling(own: Mapping[str, Any], head: str,
                    failing_ids: set[int] | None) -> datetime | None:
    """When the tick's last re-run at `head` was requested, while it settles.

    None unless GitHub accepted it (no `error`) less than
    RERUN_SETTLE_SECONDS ago, and EVERY failing run this step reads
    (`failing_ids`) is one of the jobs it re-ran: the reading is still the
    one the re-run replaces. A red check it did not re-run -- another job,
    a commit status (`failing_ids` None), or a re-run job's own red answer
    under a new id -- is a failure no waiting changes, so it holds nothing
    even while another re-run job settles (PR 2268 re-review; the tick's
    `_rerunnable` likewise re-runs only a reading made wholly of Actions
    jobs). The document is tenant-writable, so only an
    instant the tick could have written counts: a tz-aware datetime not in
    the future, or its ISO 8601 text, which is how swarm-api's state routes
    serve a Firestore instant (`attemptstate.jsonable`). A forged one can at
    most make the step wait that long.
    """
    mine = [r for r in _fix_rounds(own, RERUNS_KEY) if r.get("head") == head]
    if not mine or mine[-1].get("error"):
        return None
    jobs = mine[-1].get("jobs")
    asked = {j for j in jobs if isinstance(j, int) and not isinstance(j, bool)} if isinstance(
        jobs, list) else set()
    if not failing_ids or not failing_ids <= asked:
        return None
    since = mine[-1].get("requested_at")
    if isinstance(since, str):
        try:
            since = datetime.fromisoformat(since)
        except ValueError:
            return None
    if not isinstance(since, datetime) or since.tzinfo is None:
        return None
    age = (utcnow() - since).total_seconds()
    return since if 0 <= age < RERUN_SETTLE_SECONDS else None


def _rerun_due(own: Mapping[str, Any], head: str, failing_ids: set[int] | None) -> bool:
    """Whether a red reading at `head` parks `CI_RERUN_DUE` for the tick's free re-run (#2351).

    Only while nothing at this head has answered it: no re-run recorded at
    `head` (answered, refused or settling -- `_rerun_settling` holds the
    last), and this step's own last park (`merge_wait`) was not already
    `CI_RERUN_DUE` at `head`, so a tick that found no Actions job to re-run
    and woke the step red is answered with the refusal, not a second park.
    Never for a failing run no re-run replaces (`failing_ids` None, a commit
    status): waiting changes nothing. The document is tenant-writable; a
    forged record can at most refuse at once, as before #2351, or park once.
    """
    if not failing_ids:
        return False
    if any(r.get("head") == head for r in _fix_rounds(own, RERUNS_KEY)):
        return False
    parked = _mapping(_mapping(own.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    return not (parked.get("code") == CI_RERUN_DUE and parked.get("head") == head)


def _rounds_asked(own: Mapping[str, Any]) -> int:
    value = _mapping(own.get("metadata")).get(MERGE_FIX_ROUNDS_KEY)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, MERGE_FIX_ROUNDS_MAX)


def _fix_round_heads(ctx: ActionContext, own: Mapping[str, Any], opener: Mapping[str, Any],
                     target: str, unread: list[str] | None = None) -> list[tuple[str, str]]:
    """(task, pushed head) of each CI-fix, conflict fix and pick-fix round of this merge, newest first.

    The rounds are named by the tenant-writable `merge_fix` record, so each
    is held to facts the record cannot forge, the rule swarm-api's
    `issueci._pushing_task` applies, rechecked here: the round's task is
    read through the tenant-gated upstream read, is this tenant's, its
    signed spec verifies, and its SIGNED dispatch block `continues` the same
    root as the signed target -- the branch the pull request is on. Its
    pushed head is its own `result_summary`, as the opener's is. A round
    that fails any of these is not a candidate, and its head is `head_moved`.

    A round is its OWN workflow -- the tick submits it with
    `submit_workflow` -- so its spec is verified against the workflow the
    round's entry names (or, for an entry without one, its own signed
    `workflow_id`), never this merge's: against this merge's workflow every
    round would be `workflow_mismatch`, and its head refused. What binds it
    to this merge is the signed `continues`, not the workflow.

    A round's task that cannot be read at all is noted in `unread` (#2373);
    one that reads but fails a check is not: that is the guard working.
    """
    root = _branch_task(opener, target)
    heads: list[tuple[str, str]] = []
    entries = [*reversed(_fix_rounds(own, CONFLICT_ROUNDS_KEY)),
               *reversed(_fix_rounds(own, PICK_ROUNDS_KEY)), *reversed(_fix_rounds(own))]
    for entry in entries:
        task_id = _task_id(entry.get("task_id"))
        if task_id is None or task_id in (target, root, ctx.task_id):
            continue
        try:
            doc = _mapping(ctx.fetch_upstream(task_id))
        except Exception as exc:  # noqa: BLE001 - unreadable: not a round's head
            _unread(ctx, unread, f"round task {task_id}", exc)
            continue
        try:
            workflow = _task_id(entry.get("workflow_id")) or _task_id(doc.get("workflow_id"))
            if workflow is None:
                continue
            ctx.verify_upstream(task_id, doc, of_workflow=workflow)
        except Exception:  # noqa: BLE001 - unreadable or unsigned: not a round's head
            continue
        if doc.get("tenant_id") != ctx.tenant_id:
            continue
        block = _mapping(_mapping(doc.get("metadata")).get("dispatch"))
        if _task_id(block.get("continues")) != root:
            continue
        pushed = _pushed_head(doc)
        if pushed is not None:
            heads.append((task_id, pushed))
    return heads


def _ci_fixer_heads(ctx: ActionContext, opener: Mapping[str, Any], target: str,
                    number: int) -> list[tuple[str, str]]:
    """(task, pushed head) of each fix the PR-side CI fixer pushed FOR THIS pull request (#1073).

    scripts/ci-fix.sh, run by GitHub Actions on a red `swarm/<task>` pull
    request, submits a fix workflow that `continues_task` the branch's task;
    its pushed head moves the pull request under a parked merge step, which
    until #1073 refused it `head_moved` (PR 1071, merge task
    task_f745c31a39014359bccd). Such a head counts as one of this merge's
    fix rounds when -- and only when -- the platform's own records say the
    fixer pushed it for THIS pull request:

      * the task is this tenant's (the query asks, and this asks again);
      * its signed spec verifies, against its own signed workflow;
      * its SIGNED dispatch block `continues` the root whose branch the
        pull request is on, and its SIGNED `repository_url` is this
        merge's repository: the branch and the repository, both facts;
      * its `metadata.ci_fix` names THIS pull request's number and carries
        the `postback` state only swarm-api's `cifix.stamp` writes;
      * it recorded a pushed head.

    Never the commit's author or message, which anyone with push access
    writes. The `ci_fix` record is tenant-writable, as the opener's pushed
    head is; a forged one can name only a head pushed to this same branch
    of this same repository by a task whose spec swarm-api signed. Any other
    head is still `head_moved`.
    """
    find = ctx.find_ci_fix_tasks
    if find is None:
        return []
    root = _branch_task(opener, target)
    repository = mergeable_repository(ctx.repository_url)
    if repository is None:
        return []
    try:
        found = list(find(number, root))
    except Exception:  # noqa: BLE001 - an unreadable query names no fixer
        if ctx.log is not None:
            ctx.log.warning("merge: the CI fixer's tasks could not be read",
                            pull_request=number)
        return []
    heads: list[tuple[str, str]] = []
    for task_id, raw in found[:CI_FIXER_TASKS_READ]:
        doc = _mapping(raw)
        task_id = _task_id(task_id)
        if task_id is None or task_id in (target, root, ctx.task_id):
            continue
        if doc.get("tenant_id") != ctx.tenant_id:
            continue
        record = _mapping(_mapping(doc.get("metadata")).get(CI_FIX_METADATA_FIELD))
        if _int(record.get("pull_request")) != number:
            continue
        if record.get(CI_FIX_POSTBACK_FIELD) not in CI_FIX_POSTBACK_STATES:
            continue
        pushed = _fixer_head(ctx, task_id, doc, root=root, repository=repository)
        if pushed is not None:
            heads.append((task_id, pushed))
    return heads


def _fixer_head(ctx: ActionContext, task_id: str, doc: Mapping[str, Any], *,
                root: str, repository: str) -> str | None:
    """The head fix task `task_id` pushed to `root`'s branch of `repository`, or None.

    The facts no tenant-writable record can forge, shared by every fixer a
    record names (`_ci_fixer_heads`, `_recorded_fix_heads`): the task is this
    tenant's; its SIGNED dispatch block `continues` the root whose branch the
    pull request is on; its SIGNED `repository_url` is this merge's
    repository; its signed spec verifies, against its own signed workflow;
    and it recorded a pushed head.
    """
    if doc.get("tenant_id") != ctx.tenant_id:
        return None
    block = _mapping(_mapping(doc.get("metadata")).get("dispatch"))
    if _task_id(block.get("continues")) != root:
        return None
    theirs = mergeable_repository(doc.get("repository_url")
                                  if isinstance(doc.get("repository_url"), str) else None)
    if theirs is None or theirs.lower() != repository.lower():
        return None
    workflow = _task_id(doc.get("workflow_id"))
    if workflow is None:
        return None
    try:
        ctx.verify_upstream(task_id, doc, of_workflow=workflow)
    except Exception:  # noqa: BLE001 - unsigned or edited: not the fixer's head
        return None
    return _pushed_head(doc)


def _recorded_fix_heads(ctx: ActionContext, opener: Mapping[str, Any], target: str,
                        number: int, unread: list[str] | None = None) -> list[tuple[str, str]]:
    """(task, pushed head) of each fix round swarm-api recorded for THIS pull request
    (epic #1334), newest first, whichever workflow the round ran in.

    HANG-LEASE's merge step refused PR 1338 `head_moved` at 34ce829e
    (2026-10-15 02:16Z), the head fix task task_fcdff76b pushed from its own
    workflow wf_87a59d23: this step's `merge_fix` record never named it.
    swarm-api's merge-wake tick now appends every terminal fix round's
    pushed head -- a PR-side CI fix, a merge step's CI-fix round, a conflict
    fix round -- to `PR_FIX_HEADS_FIELD` on the task whose `swarm/<id>`
    branch the pull request is on, keyed by the repository and the pull
    request's number (`swarm_api.cifix.record_fix_heads`). That task is read
    through the tenant-gated upstream read.

    An entry only NAMES a task: it counts when it is for this repository
    and this pull request, and the task it names passes `_fixer_head` and
    pushed the head the entry says. Anything else is still `head_moved`.
    """
    root = _branch_task(opener, target)
    repository = mergeable_repository(ctx.repository_url)
    if repository is None:
        return []
    try:
        branch_doc = opener if root == target else _mapping(ctx.fetch_upstream(root))
    except Exception:  # noqa: BLE001 - an unreadable branch task records no round
        return []
    if branch_doc.get("tenant_id") != ctx.tenant_id:
        return []
    entries = _mapping(branch_doc.get("metadata")).get(PR_FIX_HEADS_FIELD)
    if not isinstance(entries, list):
        return []
    heads: list[tuple[str, str]] = []
    for entry in [e for e in reversed(entries) if isinstance(e, Mapping)][:PR_FIX_HEADS_READ]:
        task_id = _task_id(entry.get("task_id"))
        if task_id is None or task_id in (target, root, ctx.task_id):
            continue
        if _int(entry.get("pull_request")) != number:
            continue
        recorded = entry.get("repository")
        if not isinstance(recorded, str) or recorded.lower() != repository.lower():
            continue
        try:
            doc = _mapping(ctx.fetch_upstream(task_id))
        except Exception as exc:  # noqa: BLE001 - unreadable: not a round's head
            _unread(ctx, unread, f"recorded fix task {task_id}", exc)
            continue
        pushed = _fixer_head(ctx, task_id, doc, root=root, repository=repository)
        if pushed is not None and pushed == entry.get("head"):
            heads.append((task_id, pushed))
    return heads


def _distinct(heads: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """`heads` in order, each (task, head) once: two records may name one round."""
    distinct: list[tuple[str, str]] = []
    for head in heads:
        if head not in distinct:
            distinct.append(head)
    return distinct


def conflicting(pr: PullRequestFacts) -> bool:
    """GitHub says the pull request conflicts with its base: `mergeable`
    false, or `mergeable_state` `dirty` (GraphQL's CONFLICTING / DIRTY).
    `mergeable` None -- not computed yet -- is not a conflict."""
    return pr.mergeable is False or (pr.mergeable_state or "").lower() == "dirty"


def _conflicted(run: _Run, own: Mapping[str, Any], *, head: str, number: int, base: str,
                fixable: bool, detail: str = "") -> ActionOutcome:
    """A pull request that conflicts with `base` at `head`: a conflict fix round's, or
    `merge_conflict` (#1076).

    Never a wait for checks: GitHub runs none on a conflicting pull request.
    The shape is `_red`'s. A round running at this head, or one due that the
    tick has not claimed yet, parks (CONFLICT_FIX_RUNNING / _PENDING, which
    release the merge slot). A round that ended at this head without moving
    it, or was never submitted, is spent. With MERGE_CONFLICT_ROUNDS_MAX
    spent -- or for a pull request no SwarmCloud task's branch carries
    (`fixable` False: a `merge_pr` target, whose branch no round may push
    to) -- the step fails fast with `merge_conflict`, naming the pull
    request. A park past MERGE_CI_MAX_SECONDS is `merge_conflict` too.

    One writer per pull request (#2577): while an earlier round of any kind
    (CI-fix, conflict or pick) is still running at another head, the step
    parks CONFLICT_FIX_RUNNING, naming it, rather than _PENDING (which tells
    the tick the next round is due) or the rounds-spent refusal: a second
    round on the same branch cannot replay its merge over the first's.
    """
    message = f"pull request #{number} conflicts with {base} at {head}"
    if detail:
        message += f" ({detail})"
    if not fixable:
        return run.refuse("merge_conflict",
                          f"{message}; it is not on a SwarmCloud task's branch, so no conflict "
                          "fix round can push to it")

    def wait(code: str, why: str) -> ActionOutcome:
        waited = _ci_wait_age(run.ctx)
        if waited is not None:
            return run.refuse("merge_conflict",
                              f"{message}; {why}, and the step has waited {int(waited // 60)} "
                              f"min, past {MERGE_CI_MAX_SECONDS // 3600} h")
        return run.wait(code, f"{message}; {why}", head=head, pull_request=number, pending=[])

    rounds = _fix_rounds(own, CONFLICT_ROUNDS_KEY)
    last = rounds[-1] if rounds else {}
    if rounds and last.get("head") == head:
        n = last.get("round")
        error = last.get("error")
        if error:
            return run.refuse("merge_conflict",
                              f"{message}; conflict fix round {n} was not run: {str(error)[:300]}")
        task_id = _task_id(last.get("task_id"))
        if task_id is None:
            return wait(CONFLICT_FIX_RUNNING, f"conflict fix round {n} is being submitted")
        if not _ended(run.ctx, task_id):
            return wait(CONFLICT_FIX_RUNNING, f"conflict fix round {n} ({task_id}) is running")
        return run.refuse("merge_conflict",
                          f"{message}, after conflict fix round {n} ({task_id}) ended at this "
                          "head")
    live = _live_round(run.ctx, own)
    if live is not None:
        kind, entry = live
        return wait(CONFLICT_FIX_RUNNING,
                    f"{kind} fix round {entry.get('round')} ({_task_id(entry.get('task_id'))}) "
                    f"is still running at {str(entry.get('head') or '?')[:12]}")
    if len(rounds) < MERGE_CONFLICT_ROUNDS_MAX:
        return wait(CONFLICT_FIX_PENDING,
                    f"conflict fix round {len(rounds) + 1} of {MERGE_CONFLICT_ROUNDS_MAX} is due")
    return run.refuse("merge_conflict",
                      f"{message}, after {len(rounds)} conflict fix round(s), the most a merge "
                      "step gets")


def _ended(ctx: ActionContext, task_id: str) -> bool:
    """Whether a round's task has ended; one that cannot be read has, for this step."""
    try:
        state = _mapping(ctx.fetch_upstream(task_id)).get("state")
        return TaskState(state) in TERMINAL_STATES
    except Exception:  # noqa: BLE001 - nothing left to wait on
        return True


def _red(run: _Run, own: Mapping[str, Any], *, head: str, failed: list[str],
         failing_ids: set[int] | None, wait: Any) -> ActionOutcome:
    """A failing required check at `head`: a CI-fix round's to fix, or `checks_failed`.

    docs/merge-step.md "Revised 2026-10-06" §1: with rounds left the tick
    hands the red reading to a round first, so the refusal only runs once
    the rounds are spent or there were none. A round running at this head,
    or one due that the tick has not claimed yet, parks (`wait`, which
    `MERGE_CI_MAX_SECONDS` bounds). A round that ended at this head without
    moving it, or was never submitted, is spent.

    Before the budget is read, a re-run of the failing Actions jobs the tick
    asked GitHub for at this head (#2257), accepted and inside
    RERUN_SETTLE_SECONDS, parks `CI_RERUN_PENDING` whatever the budget, even
    none, while every failing run is one it re-ran (`failing_ids`): the
    reading is the one the re-run replaces. Once any of it answers red -- a
    failing run is a new id, inside the window or not -- or a check it did
    not re-run is red, this refuses (or hands it to a round) as before.

    With no round left to ask for, the FIRST red reading at a head with no
    re-run recorded there parks once `CI_RERUN_DUE` (#2351), so the tick's
    free re-run runs; its red answer refuses here as before. A round that
    ended at this head having found nothing to change (`no_change`,
    `_round_left_nothing`) gets the same: the re-run settling parks
    `CI_RERUN_PENDING`, none recorded at the head parks `CI_RERUN_DUE` once,
    and anything else refuses after the round (#2684). Still one free re-run
    per head: a head the tick re-ran before the round refuses.

    A head written by a publish swarm-api refused WORKFLOW_SCOPE_MISSING --
    the opener's pinned head, or the head a round ended at -- parks
    WORKFLOW_PATCH_PENDING for the operator instead (#2046): no round is
    asked for, and none ended there is read as spent.
    """
    message = f"at {head}: " + ", ".join(sorted(failed))
    rounds = _fix_rounds(own)
    last = rounds[-1] if rounds else {}
    if rounds and last.get("head") == head:
        number = last.get("round")
        error = last.get("error")
        if error:
            return run.refuse("checks_failed",
                              f"{message}; CI fix round {number} was not run: {str(error)[:300]}")
        task_id = _task_id(last.get("task_id"))
        if task_id is None:
            return wait(CI_FIX_RUNNING, f"{message}; CI fix round {number} is being submitted", [])
        if not _ended(run.ctx, task_id):
            return wait(CI_FIX_RUNNING, f"{message}; CI fix round {number} ({task_id}) is running",
                        [])
        refused = _round_refusal(run.ctx, task_id)
        if refused is not None:
            return _await_workflow_patch(run, message, task_id, refused, wait)
        if _round_left_nothing(run.ctx, task_id):
            parked = _rerun_wait(own, head, failing_ids, wait,
                                 f"{message}; CI fix round {number} ({task_id}) found "
                                 "nothing to change")
            if parked is not None:
                return parked
        return run.refuse("checks_failed",
                          f"{message}, after CI fix round {number} ({task_id}) ended at this head")
    if run.refused_publish is not None and run.refused_publish[1] == head:
        opener, _, refused = run.refused_publish
        return _await_workflow_patch(run, message, opener, refused, wait)
    parked = _rerun_wait(own, head, failing_ids, wait, message, due=False)
    if parked is not None:
        return parked
    asked = _rounds_asked(own)
    if len(rounds) < asked:
        return wait(CI_FIX_PENDING,
                    f"{message}; CI fix round {len(rounds) + 1} of {asked} is due", [])
    parked = _rerun_wait(own, head, failing_ids, wait, message)
    if parked is not None:
        return parked
    if asked:
        message += f", after {len(rounds)} CI fix round(s)"
    return run.refuse("checks_failed", message)


def _rerun_wait(own: Mapping[str, Any], head: str, failing_ids: set[int] | None, wait: Any,
                message: str, *, due: bool = True) -> ActionOutcome | None:
    """The park a red reading at `head` owes the tick's free re-run, or None.

    `CI_RERUN_PENDING` while a re-run at `head` settles (`_rerun_settling`);
    then, with `due`, `CI_RERUN_DUE` once when none is recorded there
    (`_rerun_due`). One rule for a red reading with no round at the head and
    for a round that ended there with nothing to change (#2684).
    """
    requested_at = _rerun_settling(own, head, failing_ids)
    if requested_at is not None:
        return wait(CI_RERUN_PENDING,
                    f"{message}; the failing jobs were re-run at {requested_at.isoformat()}, "
                    "and the re-run has not answered yet", [])
    if due and _rerun_due(own, head, failing_ids):
        return wait(CI_RERUN_DUE,
                    f"{message}; the failing jobs are due their one free re-run at this head", [])
    return None


def _round_left_nothing(ctx: ActionContext, task_id: str) -> bool:
    """Whether a CI-fix round's task SUCCEEDED having found nothing to change (#2684).

    Its `result_summary.no_change` is True (`expected_outputs.left_nothing`):
    the round read the failure as a flake, or one outside the branch, and
    pushed nothing. Only this tenant's task counts; one that cannot be read
    did not. The round's document is tenant-writable, so a forged one can at
    most park the step once for the re-run, which MERGE_CI_MAX_SECONDS bounds.
    """
    try:
        doc = _mapping(ctx.fetch_upstream(task_id))
    except Exception:  # noqa: BLE001 - unreadable: nothing to read
        return False
    if doc.get("tenant_id") != ctx.tenant_id or doc.get("state") != TaskState.SUCCEEDED.value:
        return False
    return left_nothing(doc.get("result_summary")) == NO_CHANGE_SUMMARY_KEY


def _workflow_refusal(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """A task's workflow publish swarm-api refused WORKFLOW_SCOPE_MISSING, with its patch.

    Read from the task's `result_summary.git`, as the lifecycle's
    `_workflow_push_refused` writes it: `workflow_refused.code`, the patch's
    `workflow_patch` entry and `workflow_paths`. None for any other refusal
    (`fenced` and `api_unavailable` are not a switch a round lacks) or with
    no patch recorded, which would leave the operator nothing to push.
    """
    git = _mapping(_mapping(doc.get("result_summary")).get("git"))
    refused = _mapping(git.get("workflow_refused"))
    patch = _mapping(git.get("workflow_patch"))
    if refused.get("code") != WORKFLOW_SCOPE_MISSING or not patch:
        return None
    paths = git.get("workflow_paths")
    return {
        "paths": [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else [],
        "patch": _str(patch.get("uri")),
    }


def _round_refusal(ctx: ActionContext, task_id: str) -> dict[str, Any] | None:
    """`_workflow_refusal` of a CI-fix round's task that ended, when it is this tenant's.

    The round's document is tenant-writable, so a forged refusal can at most
    park the step for the operator instead of refusing: nothing on the forge
    changes, and MERGE_CI_MAX_SECONDS still ends the wait.
    """
    try:
        doc = _mapping(ctx.fetch_upstream(task_id))
    except Exception:  # noqa: BLE001 - unreadable: no refusal to read
        return None
    if doc.get("tenant_id") != ctx.tenant_id:
        return None
    return _workflow_refusal(doc)


def _await_workflow_patch(run: _Run, message: str, task_id: str, refused: Mapping[str, Any],
                          wait: Any) -> ActionOutcome:
    """Park WORKFLOW_PATCH_PENDING: the operator pushes the patch, and the head moves (#2046)."""
    paths = list(refused.get("paths") or [])
    named = ", ".join(paths[:WORKFLOW_PATHS_NAMED]) or "a workflow file"
    if len(paths) > WORKFLOW_PATHS_NAMED:
        named += f" and {len(paths) - WORKFLOW_PATHS_NAMED} more"
    where = f" ({refused['patch']})" if refused.get("patch") else ""
    run.summary["workflow_refused"] = {"task_id": task_id, "paths": paths,
                                       "patch": refused.get("patch")}
    return wait(WORKFLOW_PATCH_PENDING,
                f"{message}; task {task_id}'s push of {named} was refused "
                f"{WORKFLOW_SCOPE_MISSING}, so a CI fix round could only be refused again: "
                f"an operator pushes {WORKFLOW_PATCH_FILE}{where} to the branch, and this "
                "step reads the pull request again once its head moves", [])


def _ci_wait_age(ctx: ActionContext) -> float | None:
    """Seconds since this step first parked for CI, when past MERGE_CI_MAX_SECONDS.

    `merge_wait.first_parked_at` on the step's own document, written by the
    fenced park (`control.ControlPlane.park_ci_pending`). The document is
    tenant-writable, so a forged instant can at worst end the step early as a
    refusal, which changes nothing on the forge; an unreadable one waits on,
    and `max_attempts` still bounds the wakes.
    """
    try:
        doc = ctx.fetch_upstream(ctx.task_id)
    except Exception:  # noqa: BLE001 - an unreadable record is no timeout
        return None
    wait = _mapping(_mapping(_mapping(doc).get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    first = wait.get("first_parked_at")
    if not isinstance(first, datetime):
        return None
    try:
        waited = (utcnow() - first).total_seconds()
    except TypeError:  # a naive instant: not one the park wrote
        return None
    return waited if waited > MERGE_CI_MAX_SECONDS else None


def _close_issues(run: _Run, merger: ForgeMerger, number: int) -> None:
    """Close every still-open issue the pull request closes (#569).

    GitHub's own `closingIssuesReferences`: the issues its closing keywords
    (and manual links) name. A `part of #N` pull request names none, so it
    closes none. An issue in another repository is left alone -- the token's
    reach there is not this step's to assume -- and recorded as such. The
    rules of `scripts/close-merged-issues.sh`, held equal to it by
    tests/unit/worker/test_merge_action.py, page rule included: past one page
    the page is closed and `issues_beyond_page` recorded.
    """
    summary = run.summary
    closed: list[int] = []
    already: list[int] = []
    elsewhere: list[str] = []
    failed: list[dict[str, Any]] = []
    try:
        references = merger.closing_issues(number)
    except forge_mod.ForgeError as exc:
        summary["issues_unread"] = str(exc)[:300]
        return
    note = f"Closed by #{number}, merged by SwarmCloud task {run.ctx.task_id}"
    for issue in references.issues:
        if issue.repository.lower() != merger.full_name.lower():
            elsewhere.append(f"{issue.repository}#{issue.number}")
            continue
        if issue.state.upper() != "OPEN":
            already.append(issue.number)
            continue
        try:
            if merger.close_issue(issue.number, comment=note):
                closed.append(issue.number)
            else:
                failed.append({"number": issue.number, "reason": "the forge refused"})
        except forge_mod.ForgeError as exc:
            failed.append({"number": issue.number, "reason": str(exc)[:200]})
    summary["issues_closed"] = closed
    if already:
        summary["issues_already_closed"] = already
    if elsewhere:
        summary["issues_in_other_repositories"] = elsewhere
    if failed:
        summary["issues_not_closed"] = failed
    if references.total > len(references.issues):
        summary["issues_beyond_page"] = {"total": references.total,
                                         "listed": len(references.issues)}


def provenance(ctx: ActionContext, merger: ForgeMerger, *, number: int, pinned: str) -> str:
    """The squash commit's body and the pull request comment (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}, "
        f"attempt {ctx.attempt_id}) into {merger.full_name}#{number},",
        f"squashed at {pinned}, the head the workflow pushed or GitHub's update of it "
        "onto the base, with every required check green there.",
    ])
