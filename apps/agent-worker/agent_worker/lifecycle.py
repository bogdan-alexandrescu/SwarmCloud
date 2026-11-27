"""The worker lifecycle, written as an ordered state machine.

The order below is the contract. It is not a suggestion, and two steps in it are
load-bearing in a way the rest are not.

    1.  VALIDATE THE FENCING GENERATION        <-- before anything else
    2.  STARTING -> RUNNING
    3.  create the isolated workspace
    4.  restore the latest checkpoint, if any
    5.  optional shallow git clone, with the tenant token only if the
        entrypoint made this process non-dumpable (hardening.py)
    5b. stage every artifact this step declared in `input_from`
    5c. link `work/artifacts` to `artifacts/`, unless the name is taken (archived issue 149)
    6.  resolve the tenant's provider credential
    7.  start the runner child
    8.  while it runs: heartbeat, MANDATORY periodic checkpoint, watch for
        cancellation / quota exhaustion / a generation change
    9.  capture stdout and stderr
    10. upload artifacts and a final checkpoint; for a CLI agent with no
        repository, also what it created in its working folder, under
        `workdir/` (#184)
    11. persist the terminal state, or READY for a retry when a clean run left
        out an expected output and the task has attempts left (archived issue 149)
    12. release the lease
    13. exit

**Step 1 is the single most important safety property in the platform.** If the
generation in this worker's environment is not the generation on the task
document, another attempt owns this task -- a reconciler decided this one was
dead and the scheduler admitted a replacement. Running anyway would give the
tenant two concurrent agents editing the same repository with the same
credentials, and the second one to finish would silently overwrite the first.
So the check happens before the workspace exists, before a secret is read, and
before the runner is started; a fenced worker emits `generation_fenced` and
exits, and it does NOT touch the lease, because the live lease is not its own.

**The same rule holds on the way out, and step 1 alone did not enforce it.**
A worker can reach the end of an attempt before it has seen the fence: a
SIGTERM, a crash, or a runner that finishes before the next control poll. Steps
10 to 12 used to
write the checkpoint pointer, the parked or terminal state, and the lease
release without asking. So every task write now re-checks the fence inside
its own transaction (`ControlPlane._fenced_task`). A refused write raises
`FencedWriteRefused`, and the worker stands down (`_stand_down`). From that
point the only document it writes is its own attempt.

**Step 8's checkpoint is mandatory.** Cloud Run's ephemeral disk is a Preview
feature that disables live migration. Without a checkpoint, an infrastructure
event two hours into an agent run costs the whole attempt; with one, it costs
`checkpoint_interval_seconds`.

Between them sits the quota rule: a wait longer than
`max_in_worker_retry_delay_seconds` is never slept through. The worker
checkpoints, publishes what it learned about the provider, parks the task with
`next_eligible_at`, releases its lease and exits, so the slot and the memory go
back to the platform instead of idling.

**A SIGTERM means something different before the runner exists.** Three
windows, and the handler (`_on_signal`) knows which one it is in:

  * During step 1, nothing has been written. The worker names the phase and
    exits at once with `EXIT_INTERRUPTED` (143).
  * From step 2 until the runner child is created (steps 2 to 6, the fence
    re-check and the quota preflight), the handler raises `StartupInterrupted`
    wherever the main thread is, including inside a blocked Firestore call or
    a clone. The worker writes neither the task, the lease nor an event, fenced
    or not. It records the interruption and its phase on its OWN attempt
    document, gives back a pool account, and exits 143. The reconciler reclaims
    the lease once it is silent and requeues the task. A park would strand it,
    because nothing promotes a SCHEDULED_RETRY park.
    The handler also RECORDS the interrupt before raising it, and `_execute`
    routes on that record, not on the exception that arrives. A library can
    replace the exception on its way up: `firestore.transactional` rolls back
    on any BaseException, and its rollback of a transaction that had not begun
    raises a ValueError in place of the interrupt.
  * Once the child exists, the handler sets `_interrupted`, says "stopping:
    SIGTERM in phase <phase>" in one INFO line, and the supervision loop
    stops the runner, checkpoints and parks or stands down exactly as before
    (`_handle_interruption`). No stack dump: `faulthandler` dumps on SIGTERM
    only until the runner starts (`startup.disarm_stack_dump`). A running
    attempt is stopped by SIGTERM routinely, and GKE files every stderr line
    as ERROR (owner, 2026-09-25).

Every startup phase is also announced (`startup.Phases`), so a worker that
stalls says where.

**Step 1's read is asked again before it gives up (archived issue 198).** A Cloud Run
instance can take a minute or more to pass traffic after it starts (Direct
VPC egress), so the generation check runs on a schedule of attempts that
spans more than a minute (`startup.CONTROL_PLANE_READ_SCHEDULE_SECONDS`), one
warning per failed attempt, before its 69. Only what would exit 69 is asked
again: a refusal, a fence and a tenant mismatch end the attempt at once. The
wait between attempts is inside step 1's window, so a SIGTERM there exits
143 at once, having written nothing.

**A dependency that is unavailable in the same window exits 69, as at step
1, and leaves the task to the reconciler.** Every Firestore call from step 1 to
the runner carries the startup budget (`ControlPlane.startup_budget`), so an
outage there raises within about 40 s. Failing the task would be terminal: the
reconciler never requeues FAILED, and `max_attempts` would never be consulted
for what is a lost attempt, not a failed one. It was 78 until 2026-09-25. 78 is
now "cannot start", which the reconciler fails without a retry, so an outage
cannot share it. See `_exit_unavailable_before_runner` for which errors count.
A refusal, such as PERMISSION_DENIED, still fails the attempt with its reason.
At step 1 only a named refusal is a 78, and every other API error is a 69
(`_exit_control_plane_unreachable`, `_refusal_cause`).
"""

from __future__ import annotations

import base64
import dataclasses
import functools
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from swarm_common.config import Settings
from swarm_common.models import EndCause, ProviderState, retries_exhausted, utcnow
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    WorkerAction,
)
from swarm_common.states import EventType, ParkReason, TaskState
from swarm_redaction import credentials as _credentials
from swarm_redaction import redact as redact_text

from . import artifact_manifest as manifest_mod
from . import budget as budget_mod
from . import cachedirs as cachedirs_mod
from . import children as children_mod
from . import cifixstart as cifixstart_mod
from . import continuation as continuation_mod
from . import publishpr as publishpr_mod
from . import publish_guard as publish_guard_mod
from . import egress as egress_mod
from . import expected_outputs as expected_mod
from . import gitops as gitops_mod
from . import forgebroker as forgebroker_mod
from . import spendprice as spendprice_mod
from . import graph_ticket as graph_ticket_mod
from . import graphroute as graphroute_mod
from . import graphstage as graphstage_mod
from . import handoff as handoff_mod
from . import inputs as inputs_mod
from . import issue as issue_mod
from . import pull_request as pull_request_mod
from . import jevkey as jevkey_mod
from . import pluginload as pluginload_mod
from . import plugins as plugins_mod
from . import qualitygates as qualitygates_mod
from . import security_review as security_review_mod
from . import mcp_attach as mcp_attach_mod
from . import secretrelease as secretrelease_mod
from . import stepservers as stepservers_mod
from . import territory as territory_mod
from . import testservices as testservices_mod
from . import typesafe_shim as typesafe_shim_mod
from . import usage as usage_mod
from . import pythonenv as pythonenv_mod
from .heartbeat import HeartbeatMixin
from .checkpointing import CheckpointMixin
from .spend import SpendMixin
from .live_browser_end import LiveBrowserEndMixin
from .accountpool import AccountPoolMixin
from .parking import ParkMixin
from .publish_retry import PublishRetryMixin
from .gitpublish import GitPublishMixin
from . import owner_questions as owner_questions_mod
from . import questions as questions_mod
from . import flow_output as flow_output_mod
from . import qa_identity as qa_identity_mod
from . import redact as redact_mod
from . import extrarepos as extrarepos_mod
from . import standalone_outputs as standalone_mod
from . import verdict as verdict_mod
from . import workspace as workspace_mod
from .statecall import new_request_id
from .accountlease import (
    PLAN_EXPIRY_MARKER,
    AccountBroker,
    Assignment,
    NoAccountAvailable,
    ReadingForwarder,
)
from .checkpoint import (
    CheckpointBackoff,
    CheckpointManager,
    CheckpointRecord,
)
from .config import WorkerConfig
from .control import (
    CHILD_AWAIT_RESUMES_METADATA_KEY,
    QUOTA_PARK_RESUMES_METADATA_KEY,
    TIME_LIMIT_CONTINUATIONS_METADATA_KEY,
    TIME_LIMIT_MAX_CONTINUATIONS,
    ControlPlane,
    ControlSignals,
)
from .errors import (
    ExitCode,
    FencedError,
    FencedWriteRefused,
    InputUnavailable,
    SpecSignatureInvalid,
    TenantMismatchError,
    WorkerError,
)
from . import findings_epic as findings_epic_mod
from . import indexrun as indexrun_mod
from . import indexspec as indexspec_mod
from . import forge as forge_mod
from . import merge as merge_mod
from . import mergeslot as mergeslot_mod
from . import mergetrain as mergetrain_mod
from . import post_verdict as post_verdict_mod
from . import specverify
from .forge import ForgeError, PullRequest, RepoAccess, probe_repository
from .forge import PullHead, read_pull_request
from .gitops import (
    DETACHED_HEAD,
    EMPTY_CLONE_BASE,
    CloneResult,
    GitError,
    GitTransient,
    MergeOutcome,
    PeerPin,
    PushRejected,
    branch_commits,
    clone_at_commit,
    commit_tree_onto,
    fetch_branch_tip,
    forge_holds_commit,
    hide_from_git,
    is_branch_ref_name,
    is_full_refname,
    origin_tips_as_data,
    own_moved_head,
    WORK_BUNDLE_NAME,
    push_branch,
    read_head_as_data,
    read_head_branch_as_data,
    read_origin_ref_as_data,
    retry_clone,
    shallow_clone,
    validate_ref,
    published_paths,
    verify_worker_authorship,
)
# The helpers every function in `gitops` builds its git commands from. Borrowed
# rather than restated for `replay_agent_commits` below, so its commands carry
# exactly the hook, fsmonitor, signing and identity overrides the fold's do.
from .gitops import (
    _NO_HOOKS,
    _SHA_RE,
    _git_text,
    _git_text_full,
)
from .gitidentity import BOT_GIT_IDENTITY, commit_identity, git_identity_env
from .hardening import FAILED, MemoryProtection
from .metrics import (
    ResourceSampler,
    ResourceUsage,
    attempt_cpu_fields,
    cgroup_cpu_limit_cores,
    cgroup_memory_max,
    combine_usage,
)
from .objectstore import ObjectStore
from .procman import ChildProcess, ChildResult, reap_foreign_processes
from .quota import decide, signal_from_control
from .runners.base import EXIT_TERMINATED
from .runners.cliagent import (
    RESUME_RELOADED,
    RESUME_SESSION_ENV,
    STEP_PUBLISHES_ENV,
)
from .runners.limits import GRACE_ENV, STDERR_ENV, STDOUT_ENV, TIMEOUT_ENV
from .runners.streams import agent_stream_files, cli_agent_spec
from .secrets import WRITE as FORGE_WRITE
from .secrets import (
    FORGE_REFUSED_TWICE,
    GIT_PROVIDER,
    CredentialMissing,
    ForgeWriteRefused,
    SecretError,
    SecretManagerClient,
    call_reading_again,
    forge_credential_kind,
    forge_credential_refused,
    forge_read_only,
    forge_suffix,
    grant_refusal,
    load_tenant,
    resolve_credentials,
    resolve_git_token,
    user_slot_hash,
)
from .startup import (
    CONTROL_PLANE_READ_SCHEDULE_SECONDS,
    EXIT_INTERRUPTED,
    Phases,
    StartupInterrupted,
    call_on_schedule,
    disarm_stack_dump,
    route_signals,
    say_from_signal_handler,
    signal_name,
    write_termination_message,
)

#: What the SIGTERM/SIGINT handler does, by window. See the module docstring.
_SIGNAL_EXIT_NOW = "exit_now"   # nothing written yet: name the phase, exit
_SIGNAL_RAISE = "raise"         # preparing the runner: unwind to run()
_SIGNAL_FLAG = "flag"           # a runner exists, or the worker is leaving

#: How many times the runner may be restarted in place after a SHORT provider
#: wait. A long wait parks instead, so this bound is only ever reached by a
#: provider that is flapping.
MAX_IN_WORKER_RETRIES = 3

#: How many times an attempt may reload its credential and restart in place.
#:
#: Two, not three, and not unbounded. A credential that was ROTATED under a
#: running agent is fixed by exactly one reload; a second covers the unlucky
#: case of a rotation landing again during the restart. Beyond that the
#: credential is not rotating, it is broken -- and retrying a broken credential
#: forever would turn one bad account into an attempt that never fails and
#: never finishes, holding its lease the whole time.
MAX_CREDENTIAL_RELOADS = 2

#: How many times an attempt on a ChatGPT plan may reload its account because
#: the codex runner stopped itself before the access token's hour (part of
#: archived issue 2688; `runners.codex.EXPIRY_MARGIN_SECONDS`). A plan token lives about an
#: hour and the longest profile timeout is four hours (14,400 s), so a run
#: needs at most five; eight leaves room for a resumed run's shorter first
#: token. Past it the stop is judged as a refusal, MAX_CREDENTIAL_RELOADS and
#: the dead-token move included, so a broker that never refreshes cannot keep
#: an attempt restarting.
MAX_EXPIRY_RELOADS = 8

#: How long to park when the account pool has nothing and cannot say when it
#: will. Used ONLY as the fallback: when the broker reports the instant the
#: binding window clears, the task waits until exactly that instant instead.
#:
#: Fifteen minutes rather than one: the pool is empty because other agents are
#: using it, and a task that wakes every minute to be told the same thing is a
#: dispatch, an image pull and a lease for nothing. Rather than five, because a
#: five-hour window releases capacity in bursts.
NO_ACCOUNT_RETRY_SECONDS = 900

#: How long to park when the pool's readings have all gone STALE.
#:
#: A different number from the one above because it is a different claim.
#: "Spent" is a fact about the accounts and clears when the provider's window
#: rolls over, hours away. "Not observed recently" says nothing about whether
#: there is room -- only that nobody has looked lately -- and the broker's own
#: usage poll looks on every sweep tick, nominally every five minutes. Parking
#: for fifteen would leave a pool with plenty of headroom idle for three sweeps
#: after it had already refreshed its readings.
STALE_READING_RETRY_SECONDS = 300

#: How long a task failed for a missing expected output waits before it is
#: eligible again (archived issue 149). Zero: the agent left a file out, and nothing about
#: that clears with time, so a delay would only hold the dependants back
#: longer. The retry is bounded by `max_attempts`, not by this, and the
#: scheduler admits it on its next drain like any other READY task.
EXPECTED_OUTPUT_RETRY_DELAY_SECONDS = 0

#: The cause a `carrier: branches` attempt fails with, before its agent runs,
#: when the tenant's forge token cannot push to the step's repository or the
#: tenant has no git credential at all (D13; owner decision 2026-10-02). A
#: worker vocabulary in the error text and `result_summary.carrier_check`,
#: not a frozen EndCause: the task's end cause is CANNOT_START. NOT RETRIED:
#: the next attempt reads the same secret and asks the same forge.
FORGE_READ_ONLY = "forge_read_only"
#: The cause a `carrier: branches` attempt fails with, before its agent runs,
#: when this worker's memory guard refuses to hold the tenant git token at all
#: (`hardening.MemoryProtection.git_token_refusal`, archived issue 453). Such a worker can
#: never push the step's branch, and the carrier is what a dependant starts
#: from, so the agent is not run for work that could not be carried. NOT
#: RETRIED: the next attempt runs the same image in the same sandbox, and the
#: refusal is that sandbox's, not the forge's.
GIT_TOKEN_REFUSED = "git_token_refused"
#: The retryable sibling: the forge could not be asked (a network failure, a
#: 429, a 5xx), which the next attempt may not meet.
FORGE_UNREACHABLE = "forge_unreachable"
#: How long a task failed for an unreachable forge waits before it is eligible
#: again. A forge outage clears with time, unlike a missing output, so a short
#: delay keeps the retry from meeting the same outage at once. Bounded by
#: `max_attempts`, like every retry.
FORGE_UNREACHABLE_RETRY_DELAY_SECONDS = 60
#: Why the worker refused a forge write the task's credential does not allow
#: (docs/onboarding.md §3.3 steps 3-4, archived issue 780 lane OB5). Worker vocabulary in
#: the error text and `result_summary.forge_check`, like the three above; the
#: task's end cause is the frozen CANNOT_START before the agent ran and
#: PUBLISH_REFUSED after it.
#:
#: FORGE_READ_GRANT: the task's signed `forge_access` is `read` (D9), and the
#: step pushes or opens a pull request. NOT RETRIED: the next attempt reads the
#: same signed document.
FORGE_READ_GRANT = "forge_read_grant"
#: FORGE_GRANT_REVOKED: the submitter's grant on the repository was deleted,
#: or changed to `read`, after submission. NOT RETRIED: the next attempt would
#: read the same grant; a person who grants again submits again.
FORGE_GRANT_REVOKED = "forge_grant_revoked"
#: FORGE_GRANT_UNREADABLE: the grant document could not be read. Retryable,
#: like an unreachable forge: it says nothing about the grant.
FORGE_GRANT_UNREADABLE = "forge_grant_unreadable"
#: The answers `forge.probe_repository` turns into a `can_push=False` access
#: whose reason starts "the forge answered <status>", and which say the forge
#: could not answer now rather than that the token cannot push. Matched on
#: that reason because the probe returns no status; the wording is pinned by
#: a test that runs the real probe, so a reworded reason fails there.
_FORGE_TRANSIENT_ANSWER = re.compile(r"^the forge answered (?:429|5\d\d)\b")

#: How many different accounts one attempt will try before it gives up and
#: parks. Three, not one: a freshly onboarded account whose secret has no
#: version yet, and a borrowed account this worker was never granted access
#: to, both look identical at `choose()` -- which is deterministic, so asking
#: again without excluding the one that failed returns the same answer.
#:
#: Bounded, and small, because every miss is a Secret Manager call and a
#: round trip to the broker while this worker holds a concurrency slot. If
#: three different accounts in a row cannot be read, the pool needs a person,
#: not a fourth try.
MAX_ACCOUNT_TRIES = 3

#: How many times one attempt may move to another account mid-run (S13/S14).
#: Each move is a stop at a turn boundary and a `--resume`, so the cap is
#: about a pool that keeps handing out accounts that run out at once -- every
#: account spent, or a drain racing another drain. Past it the attempt
#: checkpoints and parks as it would have without the swap.
MAX_ACCOUNT_SWAPS = 6

#: Why an attempt moves, as the broker spells it (`quota_broker.accounts`).
SWAP_EXHAUSTED = "exhausted"
SWAP_DRAIN = "drain"
SWAP_UNUSABLE = "unusable"

#: The pool declines where the workspace HAS a pool and its broker did not
#: answer (archived issue 2560). Asked once more, then parked: never the tenant's own secret,
#: which in task_d212c88b was a key the provider refused. Every other decline
#: means the workspace has no pool for this run, and keeps the tenant secret.
POOL_UNANSWERED = ("broker_unreachable", "broker_client_error")

#: Utilisation points one HEARTBEAT event carries at most, newest kept
#: (LIVE-UTIL-API, archived issue 1689). 30 x `metrics.POINT_SECONDS` is 150 s, one heartbeat
#: period at the platform's interval; a beat that came later than that loses
#: its oldest points rather than growing the event.
HEARTBEAT_POINTS = 30
#: The size one HEARTBEAT event's detail is held under, serialised. 30 points
#: of four short keys are about 2 KiB; this is the bound a test holds, so a
#: change that lets the series grow is caught before it reaches every event.
HEARTBEAT_DETAIL_MAX_BYTES = 4096


def _rounded3(value: float | None) -> float | None:
    """Three decimals, and None -- not measured -- kept None."""
    return None if value is None else round(float(value), 3)


#: What an index run's phase may write to each of its captures (lane IX1).
#: The extractor prints one summary line and the writer one JSON report; the
#: cap only bounds a tool gone wrong, and the tail -- its last error -- is kept.
INDEX_PHASE_CAPTURE_BYTES = 1024 * 1024

#: `work/repo`. Spelled once, in `workspace`, because the checkpoint needs it
#: too: the `./artifacts` link inside the checkout is never archived (archived issue 226).
REPO_DIR_NAME = workspace_mod.REPO_DIR_NAME

#: Worker-owned scratch INSIDE the checkpointed work directory. It has to be
#: inside `work/` so a resumed attempt still knows which commit its clone
#: landed on, and outside `work/repo/` so nothing it holds can turn up in the
#: agent's own diff.
WORKER_STATE_DIR = ".swarm"
CLONE_BASE_FILE = "clone-base"
PATCH_NAME = "swarm-work.patch"
#: The artifact a moved HEAD's line is kept in (`Worker._keep_moved_head`):
#: nothing of it is published, and `git fetch moved-head.bundle` brings it
#: back (reported by a peer application's session, 5 November 2026).
MOVED_HEAD_BUNDLE = "moved-head.bundle"

#: The checkpoint label, and the cause on the attempt's document, of an attempt
#: that left because Firestore stayed unreachable while its agent ran (archived issue 70).
CONTROL_PLANE_OUTAGE = "control_plane_outage"

#: What `builds_on` may be (archived issue 264): a task id as `swarm_common.models.new_id`
#: mints them, and nothing that could make the derived branch name a path
#: (`..`, `/`) or an option (a leading `-`).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")

#: The most steps one workflow may have, read from the FROZEN `Settings`
#: default rather than restated. It bounds the walk back along `builds_on`
#: (`Worker._builds_on_base`, archived issue 978): each step of the chain is a distinct
#: step of one workflow, so a longer chain is a document nobody of ours wrote.
_MAX_WORKFLOW_STEPS: int = next(
    f.default for f in dataclasses.fields(Settings) if f.name == "max_workflow_steps"
)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: How `forge.open_pull_request` words the forge's own refusal of a pull
#: request (its 422 with no open one to reuse), as opposed to a forge that
#: could not be reached. The publish reads it to tell a refusal, which fails a
#: pull-request step (`_published_nothing`), from an outage, which does not.
#: Held to `forge.open_pull_request`'s wording by a test of the publish.
PULL_REQUEST_REFUSED = "the forge refused the pull request"

#: The `result_summary.git` key `_publish_git` sets when a pull request
#: refused 401 after its push was still not opened by the one re-read
#: (`Worker._open_pull_request_reading_again`, archived issue 1623): "branch <name> pushed;
#: pull request not opened (401 twice)", or the re-read's refusal.
PULL_REQUEST_NOT_OPENED_FIELD = "pull_request_not_opened"

#: The `result_summary.git` key `_publish_git` sets when the forge stayed
#: unreachable past the in-process retry (`forge.retry_transient`) while
#: probing the repository or opening the pull request (F1, 2026-10-04). Read
#: by `_publish_unreachable`: a pull-request step that ends with it fails its
#: ATTEMPT retryably rather than SUCCEEDING with no pull request -- the shape
#: that turned release 37017777271's acceptance red on 2026-10-02.
PUBLISH_UNREACHABLE_FIELD = "forge_unreachable"
#: The `publish` a `PUBLISH_UNREACHABLE_FIELD` marker carries when it was the
#: branch PUSH the forge never answered (`gitops.PushUnreachable`, archived issue 2833),
#: rather than the probe or the pull request. Read by the retry, which then
#: publishes the kept work without running the agent (`_push_to_republish`).
REPUBLISH_PUSH = "push"
#: The `publish` a `PUBLISH_UNREACHABLE_FIELD` marker carries when the clean
#: repository's fetch of the agent's work met an object its partial clone
#: never downloaded and origin could not complete it
#: (`gitops.PublishFetchIncomplete`, issue 367). The marker also carries
#: `missing` and `on_origin`; the retry publishes the kept work without the
#: agent unless origin said it does not have the object (`_push_to_republish`).
REPUBLISH_FETCH = "fetch"
#: The cause, and the error's prefix, of an attempt that ended on such a fetch.
PUBLISH_FETCH_INCOMPLETE = "publish_fetch_incomplete"
#: The `result_summary.git` key `_publish_git` sets when git itself failed
#: the publish -- the push, or the commit, fold or authorship check before it
#: (archived issue 872). Read by `_publish_failed`: a pull-request step that ends with it
#: ends FAILED naming the cause and the patch, not SUCCEEDED with a
#: `publish_reason` nobody reads. Measured 2026-10-08 on
#: task_6a0c9afdbaea449eb53d: "push failed with exit 1", state SUCCEEDED, the
#: workflow green, and the work found in `swarm-work.patch` six hours later.
PUBLISH_FAILED_FIELD = "publish_failed"
#: The `result_summary.git` key `_publish_git` sets when a continued branch
#: moved only by merges of the default branch already holds every change this
#: round made (archived issue 2179: archived pull request 2093's GitHub update carried main's identical fix),
#: or when origin's tip of the branch already holds the round's HEAD (archived issue 2506:
#: archived pull request 2446's head was the commit the round ended on). The name is the first
#: case's. Its value is the reason, naming the tip; nothing was pushed. Read by
#: `_no_change_after_publish`: the attempt ends `no_change`, not
#: `publish_failed`.
BASE_MERGE_NO_CHANGE_FIELD = "base_merge_no_change"
#: The `result_summary.git` key `_publish_git` sets when the worker's commit
#: of uncommitted work would have put the tree back at the publish base's
#: though the agent's own commits had changed it (archived issue 2536): the reason, naming
#: the paths. Nothing was pushed. Read by `_record_finalised`, which ends the
#: attempt FAILED with `publish_refused` (`_fail_for_auto_commit_undid`).
AUTO_COMMIT_UNDID_FIELD = "auto_commit_undid_agent_commits"
#: The `result_summary.git` key `_push_onto_base_merge` sets when the replay
#: onto a base-merged tip took the tip's version of paths the base merge
#: rewrote and this round also changed (archived issue 2367: four rounds' hand-patched
#: unit_test_durations.json, rewritten by main's archived pull request 2312). A scrubbed list of at
#: most `BASE_MERGE_SUPERSEDED_MAX` paths. Set whether the rest of the round
#: was pushed or, with nothing left, the round ended `no_change` with a
#: `superseded_by_base` reason in `BASE_MERGE_NO_CHANGE_FIELD`.
BASE_MERGE_SUPERSEDED_FIELD = "base_merge_superseded"
#: Enough to name what main rewrote under a round -- one file, measured --
#: without a vendored-tree conflict filling the record.
BASE_MERGE_SUPERSEDED_MAX = 20
#: The `result_summary.git` key `_push_onto_base_merge` sets when a continued
#: branch was moved by something other than merges of the default branch --
#: another fix round's commit, a conflict fix -- and this round's commits were
#: replayed onto that tip (archived issue 1821, owner decision 2026-10-27): the tip's sha.
#: `moved_tip_from` beside it holds the clone base the round started from.
MOVED_TIP_FIELD = "moved_tip"
#: The paths a push may touch only with the `workflow` scope (AU-3,
#: docs/design/agent-autonomy.md §3.3): `_publish_git` hands a branch whose
#: new commits touch either to swarm-api's `git_push_branch`. Restated from
#: `swarm_api.gittools.WORKFLOW_PATHSPECS`, which this worker cannot import.
WORKFLOW_PATHSPECS = (".github/workflows", ".github/actions")
#: The artifact a refused workflow publish leaves: the workflow and action
#: lines it did not push, as a unified diff the operator applies by hand.
WORKFLOW_PATCH_FILE = "workflow-lines.patch"
#: How many workflow paths the publish names, as swarm-api's refusal does.
WORKFLOW_PATHS_NAMED = 20
#: The most of `workflow-lines.patch` read from git; a longer diff is cut and
#: says so. Workflow files are YAML, so this is far past any real one.
WORKFLOW_PATCH_MAX_BYTES = 8 * 1024 * 1024
#: The most of swarm-api's refusal message a refused workflow publish quotes
#: in `publish_reason`, and so in the step's error (archived issue 2023). It was 300, cut
#: mid-sentence: "Push without those files and write them as. Nothing was
#: pushed" read as a different sentence. Swarm-api's refusals run to a few
#: hundred characters, so 1,500 keeps every real one whole; it is a bound
#: only because the message is another service's text. With up to
#: `WORKFLOW_PATHS_NAMED` paths and the patch's URI beside it, the reason
#: stays under the 4,000 characters `_fail_for_published_nothing` keeps of
#: an error, so the patch's name at its end is never the part cut. A longer
#: message is cut at a word boundary and ends with "…" (`_cut_at_word`).
WORKFLOW_REFUSAL_MAX_CHARS = 1500
#: The dispatch block's mark of a merge step's conflict fix round (archived issue 1321):
#: swarm-api copies it from the `metadata.merge_conflict_round` record
#: `mergewake._on_conflict` submits the round with, INTO the signed block,
#: because the worker acts on no metadata the step-spec signature does not
#: cover. Restated here because the worker cannot import swarm_api
#: (`swarm_api.validation.CONFLICT_ROUND_FIELD`); a test of the conflict
#: round's merge parents holds the two together, so a rename on either
#: side fails there. A round checks, before it pushes, that it merged the default
#: branch (`gitops.verify_merged_default_branch`).
CONFLICT_ROUND_FIELD = "conflict_round"
#: The file whose presence makes a clean run that changed nothing a RESULT
#: (Proposal J, owner decision 2026-10-08): the agent checked, found nothing
#: to change, and wrote down why. Read from `$SWARM_ARTIFACTS_DIR`; its text is
#: kept as `result_summary.no_change_reason`.
VERIFICATION_FILE = "verification.md"
NO_CHANGE_REASON_SUMMARY_KEY = "no_change_reason"
#: A check-only `publish_to_pr` step's other evidence (CHECKONLY-VERDICT, part
#: of archived issue 2241): a review's `verdict.json` beside a diff that is empty is the
#: `no_change` result too, so a rendered review that found nothing to fix
#: becomes a verdict instead of failing `empty_diff`. Only for a step that
#: publishes onto an existing pull request: there the verdict has a pull
#: request to land on (`_post_no_change_evidence`).
CHECK_VERDICT_FILE = "verdict.json"
#: Where the comment that carries a `publish_to_pr` step's no-change evidence
#: is recorded, in `result_summary` (`_post_no_change_evidence`).
NO_CHANGE_COMMENT_SUMMARY_KEY = "no_change_comment"
#: At most this many bytes of `verification.md` are kept in the summary; the
#: file itself is uploaded whole with the other artifacts.
NO_CHANGE_REASON_MAX_BYTES = 4000
#: What a pull request titled from its issue, because the agent wrote no
#: usable `pr-title.txt` even after its follow-up turn, starts with (Proposal
#: B, owner decision 2026-10-08). The observer counted 17 of 32 pull requests
#: titled with the issue's title on 2026-10-08, none of them distinguishable
#: from a title the agent chose; the prefix makes the fallback visible.
TITLE_MISSING_PREFIX = "[title missing] "

#: The files an agent leaves in `$SWARM_ARTIFACTS_DIR` to write its own pull
#: request's title and body (archived issue 214), so a platform pull request can say what it
#: closes. Read without following a link, scrubbed, bounded, and refused -- in
#: favour of the generated text -- on attribution (the body only when it is
#: nothing but attribution: its attribution lines are removed, archived issue 735); see
#: `Worker._agent_pull_request_text`.
PR_TITLE_FILE = "pr-title.txt"
PR_BODY_FILE = "pr-body.md"
#: The runner's pre-publish section (SPEED-D1; `prepublish.PR_SECTION_NAME` and
#: `prepublish.PR_SECTION_HEADING`, restated because the runner never imports
#: the worker; a test of the pre-publish gate holds them equal).
PREPUBLISH_SECTION_FILE = "prepublish-checks.md"
#: The runner's deadline, an epoch instant (`prepublish.RUNNER_DEADLINE_ENV`,
#: restated like the names above; a test of the deadline holds them equal).
RUNNER_DEADLINE_ENV = "SWARM_RUNNER_DEADLINE_EPOCH"
#: The task document's record of the `timeout_seconds` its spec asked for, when
#: the API lowered it to the profile's cap (requested of swarm-api in archived issue 2981),
#: and the runner's name for it (`cliagent.TIMEOUT_ASKED_ENV`, held equal by
#: the same deadline test).
TIMEOUT_ASKED_FIELD = "timeout_seconds_asked"
TIMEOUT_ASKED_ENV = "SWARM_TIMEOUT_ASKED_SECONDS"

#: CONTINUING AT THE TIME LIMIT (the owner, 2026-11-02). In the 14 days to that date
#: 46 tasks were killed at their 1 or 2 hour limit, 75 hours of work, and each
#: failure ended its workflow. An attempt that reaches its step's limit and
#: whose final checkpoint uploaded now parks SCHEDULED_RETRY at once (the
#: SIGTERM park's shape, `_handle_interruption`) instead of failing: the
#: scheduler promotes it (`_promote_scheduled_retries`), admission leases a
#: NEW attempt at a new fencing generation, and that attempt restores the
#: checkpoint and resumes the agent's session with `TIME_LIMIT_RESUME_PROMPT`.
#: At most `control.TIME_LIMIT_MAX_CONTINUATIONS` (2) times per task, counted
#: by the park in `metadata.time_limit_continuations`; the stop after that,
#: an attempt with no uploaded final checkpoint, a requested cancel, or a
#: next attempt the task's budget would not hold fails as before.
#:
#: The note the stopped attempt leaves in `work/` for the attempt after it:
#: the session to resume, and which continuation it is. It is written before
#: the final checkpoint, so the checkpoint carries it, and read and removed
#: by the next attempt after its restore, which acts on it only when the
#: task's own counter says that continuation happened.
CONTINUATION_NOTE_NAME = ".swarm-continuation.json"
#: Why an attempt stopped at its limit does not continue, as its error says.
NO_CONTINUATION_CHECKPOINT = "no_checkpoint"
NO_CONTINUATION_CAP = "out_of_continuations"
NO_CONTINUATION_BUDGET = "budget"
#: The verified task's resolved turn cap (contract request 133, `Task.max_turns`)
#: and the runner's name for it (`claude_code.MAX_TURNS_ENV`, held equal by
#: a test of the runners).
MAX_TURNS_FIELD = "max_turns"
MAX_TURNS_ENV = "SWARM_MAX_TURNS"
PREPUBLISH_SECTION_HEADING = "## Pre-publish checks failed"
PREPUBLISH_SECTION_MAX_CHARS = 20000
#: The runner's advisory section (CTXQ-G; `prepublish.ADVISORY_SECTION_NAME`
#: and `_HEADING`, held equal by a test of the pre-publish graph gate):
#: test reach, the agent's self-check, what the graph selected. Appended for
#: the reader; no merge path reads it, and it never marks the checks failed.
PREPUBLISH_ADVISORY_FILE = "prepublish-advisory.md"
PREPUBLISH_ADVISORY_HEADING = "## Advisory signals (not a gate)"
PREPUBLISH_ADVISORY_MAX_CHARS = 10000
#: One line, at most this many characters: GitHub's own title field is 256.
PR_TITLE_MAX_CHARS = 256
#: At most this many bytes of the agent's body, before the platform's block. A
#: GitHub body holds 65,536 characters; the platform's block has to fit too, so
#: the agent's part is cut here rather than letting the forge refuse the lot.
PR_BODY_MAX_BYTES = 60 * 1024
#: How much of either file is read at all. Past this the text is cut anyway.
PR_READ_LIMIT_BYTES = 256 * 1024

#: The subject of the one commit a fold makes when the agent wrote no usable
#: `pr-title.txt`; never the task id, like `UNCOMMITTED_COMMIT_SUBJECT`.
FOLDED_COMMIT_SUBJECT = "Everything the agent changed, as one commit"

#: The retired generated title's shape, `[swarm] task_...`, in any case. A
#: title that matches is treated as carrying the task id even when the id in
#: it is not this task's (an agent that copied another task's old title).
_RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)
#: The issue-number title `_title_from_issue_input` writes without an issue
#: title. Like the retired shape, a worker's default a written title replaces.
_DEFAULT_ISSUE_TITLE_RE = re.compile(r"^Work on issue #\d+ \(part of #\d+\)$")


def _issue_title_from(path: Path, number: int) -> str | None:
    """The issue's title from the header `issue.render` writes, or None.

    `# Issue #<number>: <title>` is the file's first line, with the title
    already scrubbed of every registered secret and folded onto one line;
    `(no title)` is what an untitled issue reads as, and is no title.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\n")
    except OSError:
        return None
    prefix = f"# Issue #{number}: "
    if not first.startswith(prefix):
        return None
    title = first[len(prefix):].strip()
    return title if title and title != "(no title)" else None


def _as_issue_number(value: Any) -> int | None:
    """A GitHub issue number from whatever `input.issue` turns out to hold.

    `#265` is landing separately and had not, as of this change, fixed that
    input's shape -- so this reads defensively: a bare number, a numeric
    string, or a leading `#N`. Anything else, including a non-positive
    number, is "no number here" rather than a guess.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip().lstrip("#")
        if stripped.isdigit():
            n = int(stripped)
            return n if n > 0 else None
    return None


#: The worker-owned repository the token-bearing git commands run in lives under
#: `ws.private` -- never checkpointed, never uploaded, never named in the agent's
#: environment -- so the push and the integrator's fetch authenticate from
#: configuration the agent could not write. `gitops.prepare_publish_repo` gives
#: it an unpredictable name (`tempfile.mkdtemp`): `ws.private` shares a uid with
#: the agent, so a constant name would let the agent pre-plant a symlink there.


@dataclass
class WorkerDeps:
    control: ControlPlane
    store: ObjectStore
    logger: Any
    db: Any
    metrics_exporter: Any
    secret_client: SecretManagerClient | None = None
    #: The account pool client. None means "build one from the config, if the
    #: config names a broker" -- which is what the entrypoint does. Injected in
    #: tests so no unit test needs a metadata server or a network.
    account_broker: Any | None = None
    #: The entrypoint's phase tracker, carried on so the lifecycle's phases
    #: continue the same clock. None builds one on the worker's logger.
    phases: Phases | None = None
    #: What the entrypoint's `hardening.make_non_dumpable` established before
    #: any credential was read. The tenant git token is held only when the
    #: agent cannot read this process's memory (`Worker._git_token_refusal`).
    #: None means nothing established it, and is treated as FAILED: a worker
    #: built some other way holds no token rather than an unprotected one.
    memory: MemoryProtection | None = None
    #: The client for swarm-api's worker-only child routes. None builds one
    #: from SWARM_API_URL when the attempt has a child path; injected in tests.
    child_api: Any | None = None


@dataclass(frozen=True)
class _Upload:
    """What `Worker._upload_copy` did with one file.

    `bytes` is what was uploaded, or None when nothing was, with `skipped` the
    reason (`standalone_outputs`' wording). `unredacted` is the file's
    `redaction_skipped` entry when it left without being rewritten and its
    bytes were not found clean.
    """

    bytes: int | None = None
    skipped: str = ""
    unredacted: dict[str, Any] | None = None


def _skip_cause(reason: str) -> str:
    """`_Upload.skipped`, as the cause the missing-output check reads (archived issue 165).

    Over the byte cap is the cap. An upload the store refused, or a file that
    could not be read, is an upload error, the one a retry may not meet. A
    link or a non-regular file is refused, and the agent of the next attempt
    is handed the same instructions.
    """
    if reason == standalone_mod.OVER_CAP:
        return expected_mod.CAUSE_CAP
    if reason in (standalone_mod.UPLOAD_FAILED, standalone_mod.UNREADABLE):
        return expected_mod.CAUSE_UPLOAD_ERROR
    return expected_mod.CAUSE_REFUSED


def published_nothing(summary: Any) -> bool:
    """True when a SUCCEEDED contributor's `result_summary` says it had no
    branch to push (archived issue 760): its harvest ran (no `error`), recorded
    `published: false`, and found no commit and no uncommitted path.

    A review step under `integrate` -- one that writes only `verdict.json`
    and leaves its clone as cloned -- ends exactly so. Every condition is
    read, none defaulted: a contributor that committed, left work
    uncommitted, or whose harvest failed could have pushed, and its absent
    branch is "missing" on the pull request as before.
    """
    if not isinstance(summary, dict):
        return False
    git = summary.get("git")
    if not isinstance(git, dict) or git.get("error"):
        return False
    return (
        git.get("published") is False
        and git.get("commit_count") == 0
        and git.get("dirty_count", 0) == 0
    )


@dataclass
class Outcome:
    exit_code: int
    state: TaskState | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class _PullUnreachable(GitError):
    """The forge did not answer the re-read of a `publish_to_pr` pull
    request before the push (archived issue 2183): nothing was pushed, and the attempt ends
    retryably, as any publish the forge did not answer does."""

    def __init__(self, cause: "forge_mod.ForgeUnavailable") -> None:
        super().__init__(f"the pull request could not be read before the push: {cause}")
        self.cause = cause


class _CloneUnreachable(WorkerError):
    """The clone's forge did not answer through every in-process try (archived issue 623).

    Raised by `_maybe_clone` and turned by `_prepare` into a retryable end of
    the attempt (`_fail_clone_unreachable`). A `WorkerError`, so a caller
    that does not tell it apart still fails deliberately rather than crashing.
    `reason` is already scrubbed.
    """

    def __init__(self, reason: str, tries: int) -> None:
        super().__init__(f"repository clone failed: {reason}")
        self.reason = reason
        self.tries = tries


def _cut_at_word(text: str, limit: int) -> str:
    """`text` whole when it is at most `limit` characters; otherwise cut at the
    last whitespace within the limit and ended with "…" (archived issue 2023), so the cut
    never ends mid-word and never reads as a sentence of its own. A text with
    no whitespace in reach is cut at the limit, still with the "…"."""
    text = str(text).rstrip()
    if len(text) <= limit:
        return text
    room = max(limit - 1, 0)  # one character for the "…"
    head = text[:room]
    if not text[room].isspace():
        # The cut falls inside a word: drop that word's start.
        partial = re.search(r"\s\S*$", head)
        if partial is not None and partial.start() > 0:
            head = head[: partial.start()]
    return head.rstrip().rstrip(".,;:") + "…"


class Worker(
    HeartbeatMixin, CheckpointMixin, SpendMixin, LiveBrowserEndMixin, AccountPoolMixin, ParkMixin,
    PublishRetryMixin, GitPublishMixin,
):
    """One attempt, start to finish."""

    def __init__(self, config: WorkerConfig, deps: WorkerDeps) -> None:
        self.cfg = config
        self.control = deps.control
        self.store = deps.store
        self.log = deps.logger
        self.db = deps.db
        self.metrics = deps.metrics_exporter
        self.secret_client = deps.secret_client
        self.memory = deps.memory
        self.ws: workspace_mod.Workspace | None = None
        #: The staged graph's runner variables (CTXQ-S), decided once per
        #: attempt: a credential reload rebuilds the env and must not restage.
        self._graph_env: dict[str, str] | None = None
        #: The lane quality gates' runner variable (`qualitygates`), once per attempt.
        self._gates_env: dict[str, str] | None = None
        #: The step's agent plugins (`plugins.start`), set up once per attempt
        #: before the agent; None for a step that names none.
        self._plugins: plugins_mod.Started | None = None
        #: The step's signed work contract named for the runner's JC2
        #: (`_stage_work_contract`), set once per attempt before the agent.
        self._work_contract_env: dict[str, str] = {}
        #: A security review step's record (`security_review`): {} once its
        #: plugin started, filled after the agent; None for any other step.
        self._security_review: dict[str, Any] | None = None
        self.checkpoints = CheckpointManager(
            store=deps.store,
            tenant_id=config.tenant_id,
            task_id=config.task_id,
            attempt_id=config.attempt_id,
            generation=config.generation,
            logger=deps.logger,
            max_bytes=config.max_checkpoint_bytes,
        )
        self._interrupted = False
        self.phases = deps.phases if deps.phases is not None else Phases(deps.logger)
        # What the generation check's scheduled attempts wait with, and time
        # themselves by (`startup.call_on_schedule`). Replaceable so a test can
        # run the schedule without waiting it out.
        self.startup_sleep: Callable[[float], Any] = time.sleep
        self.startup_clock: Callable[[], float] = time.monotonic
        # What a beat and `_heartbeat_meanwhile`'s thread read the time from,
        # and what that thread waits with (`wait(stop, seconds)`, True once
        # stopped). Replaceable so a test runs the thread on a virtual clock:
        # on real sleeps a busy runner scheduled one beat 0.01 s past its
        # band with nothing wrong in the thread (archived issue 2233). A replacement holds
        # for those two only; the loop and the lease's outage check read
        # `time.monotonic`. A test that runs the loop with one installed starts
        # it at a real `time.monotonic()` reading and only moves it forward
        # (the tests' virtual clock helper), so a beat's stamp stays
        # within the test's own few seconds of the loop's clock.
        self.heartbeat_clock: Callable[[], float] = time.monotonic
        self.heartbeat_wait: Callable[[threading.Event, float], bool] = threading.Event.wait
        # The egress probe (archived issue 721 (a), `agent_worker.egress`): started at the
        # top of `run`, waited for by the clone only, stopped when the startup
        # window closes. The connect, cap and interval are replaceable so a
        # test never touches the network nor waits out the cap. None until
        # `run` starts it; `_maybe_clone` called alone then waits for nothing.
        self.egress_connect: Callable[..., Any] | None = None
        self.egress_cap_seconds: float = egress_mod.EGRESS_PROBE_CAP_SECONDS
        self.egress_interval_seconds: float = egress_mod.EGRESS_PROBE_INTERVAL_SECONDS
        self._egress: egress_mod.EgressProbe | None = None
        self._egress_marked = False
        # Which window a SIGTERM lands in. `_on_signal` reads it; `run` and
        # `_execute` set it, always through `_signal_policy` so that it is
        # restored however the window is left.
        self._signal_mode = _SIGNAL_FLAG
        # The first SIGTERM or SIGINT that arrived in the startup window, kept
        # here as well as raised. See `_execute` for why the record, not the
        # exception that reaches it, decides the exit.
        self._startup_interrupt: StartupInterrupted | None = None
        self._child: ChildProcess | None = None
        # BROWSER-USE-FLAG: whether this attempt has looked, once, for a
        # browser call after BROWSER_IDLE_SECONDS (`_note_browser_idle`).
        self._browser_idle_checked = False
        # How the publish path makes sure no process the agent started is alive
        # before the tenant credential is used (`_publish_git`). A callable, not
        # a direct call, so a unit test can substitute a scoped reaper: the real
        # one runs `os.kill(-1, SIGKILL)`, which in a shared test process would
        # kill the test runner. Returns the PIDs still alive after the reap --
        # empty means clean, non-empty means refuse to publish.
        self.reap_before_publish: Callable[[], tuple[int, ...]] = self._default_reap
        # The clean publish repository's `.git/config` as the worker built it,
        # keyed by the repository (`_build_clean_repo`), re-checked after the
        # second reap before the credential-bearing publish (archived issue 346 box 40).
        self._publish_config_seals: dict[str, str | None] = {}
        # A worker action's seams (archived issue 295): the forge transport (None is the
        # real, no-redirect one in `forge._open`), the environment its Job's
        # forge record is read from, and the bounded pause between two
        # `mergeable` reads. Replaceable so a unit test talks to no forge and
        # waits for nothing.
        self.forge_transport: forge_mod.Transport | None = None
        self.action_environ: Any = os.environ
        self.action_sleep: Callable[[float], Any] = time.sleep
        # The pause between two tries of a forge call that failed
        # transiently (`_forge_retry`). Replaceable so a test waits for nothing.
        self.forge_sleep: Callable[[float], Any] = time.sleep
        # The LIVE runner's sampler, or None between runners. The runners that
        # have ended are in `_runner_usage`, and `_attempt_usage` combines the
        # two: every figure this worker reports -- the attempt document, the
        # export, the HEARTBEAT series -- is the attempt's, not one runner's.
        self._sampler: ResourceSampler | None = None
        self._runner_usage: list[ResourceUsage] = []
        #: The utilisation series' clock (archived issue 1689): `time.monotonic()` at the
        #: attempt's first runner, shared by every runner's sampler, and the
        #: points a reaped runner took that no heartbeat has carried yet.
        self._series_origin: float | None = None
        self._pending_points: list[dict[str, Any]] = []
        self._metrics_exported = False
        self._last_checkpoint: CheckpointRecord | None = None
        self._restored_from: CheckpointRecord | None = None
        self._repo_url: str | None = None
        self._task: dict[str, Any] | None = None
        #: True once `_verify_spec` VERIFIED the signature over `_task` -- not
        #: for a legacy unsigned task. Only then is its `forge_credential`
        #: trusted (`_forge_suffix`): the generation check, then the
        #: signature, then the credential read (invariant 5).
        self._forge_signed = False
        #: The own spec's check, set by `_verify_spec` (MODEL-WORKER). Until
        #: then the check of a document that names no class: an unverified
        #: document never reaches the model check, which runs after it.
        self._spec_check = specverify.SpecCheck(reason="unverified", task_id=config.task_id)
        #: An `input.issue` number's title, when something upstream of the pull
        #: request title (archived issue 265's own fetch, once it lands) already read it.
        #: Nothing sets this yet -- archived issue 265 is landing separately and had not, as
        #: of this change, shipped the fetch -- so it stays None and
        #: `_title_from_issue_input` names the work by the issue number,
        #: always as "part of #N", never "Fixes #N".
        self._issue_title: str | None = None
        #: The issue fetch started beside the clone (archived issue 721, P29,
        #: `_start_issue_prefetch`), joined at STEP 6b. None when the task
        #: asks for no issue, or nothing let it start early.
        self._issue_prefetch: issue_mod.IssuePrefetch | None = None
        #: When the issue fetch started and ended, seconds since process
        #: start, for `agent_started`. None until a fetch has been joined.
        self._issue_fetch_timing: dict[str, Any] | None = None
        #: The attempt's one Python environment warm-up (pythonenv.py), run
        #: at the end of `_prepare`; it remembers it ran, so no later session
        #: runs it again.
        self._python_env = pythonenv_mod.PythonEnvWarmup(
            redact=lambda text: redact_text(self.log.scrub_text(text)).text,
            emit=self._note_python_env,
        )
        #: The pull request this step works on (`_requested_pull_request`),
        #: worked out once: `False` until then, None when it names none.
        self._pull_request_number: int | None | bool = False
        self._clone_base: str | None = None
        #: The commit this attempt's clone checked out, recorded as
        #: `result_summary.git.clone_commit` (archived issue 295, merge-step.md §3).
        self._clone_commit: str | None = None
        #: `fetch_pull_base`'s record of a publish_to_pr clone's base (archived issue 2901 §5.8).
        self._pull_base: dict[str, Any] | None = None
        # The clone base the PUBLISH trusts, which is not always the one above.
        # `_clone_base` may come from `work/.swarm/clone-base`, a file in the
        # agent's HOME (its working directory's parent, when it starts in the
        # checkout -- archived issue 226), which it can write; that is good enough to describe the
        # work and not good enough to decide what reaches a forge. This one is
        # only ever the worker's own knowledge: the commit its clone landed on
        # in this process, or the base a previous attempt's worker recorded in
        # the checkpoint manifest. `EMPTY_CLONE_BASE` for an empty repository.
        self._publish_base: str | None = None
        # Origin's tip of the branch this publish pushes, read from the forge
        # into the publish repository, kept only when HEAD descends from it
        # (`_read_publish_remote_tip`, archived issue 2506): commits origin already has,
        # which the workflow range and the broker's bundle leave out
        # (`_workflow_negatives`). Reset at the start of every publish.
        self._publish_remote_tip: str | None = None
        # The branch this attempt's clone left HEAD on (`refs/heads/<name>`, or
        # `DETACHED_HEAD` for a clone at a commit), read by the worker before
        # any agent process existed: the branch the step was given (archived issue 2094).
        # A resumed attempt reads it back from the checkpoint manifest the
        # worker wrote (`CheckpointRecord.start_branch`). None when it is not
        # known -- a manifest from before it was recorded -- and then only the
        # start sha decides whether HEAD moved (`_head_moved`).
        self._start_branch: str | None = None
        # The head of the pull request a `publish_to_pr` step started from
        # (archived issue 2183, `agent_worker.publishpr`): read from GitHub before the
        # clone, or back from the checkpoint manifest on a resume. The push
        # onto that pull request fast-forwards exactly this sha or nothing.
        self._pr_start_head: str | None = None
        # That pull request's base branch, read with its head (archived issue 2901): the
        # clone fetches it as `origin/<base>`, deep enough to merge it in.
        self._pr_base_ref: str = ""
        # The base BRANCH the runner rebases the lane onto before it publishes
        # (SPEED-D1, `agent_worker.prepublish`), when that is safe for this
        # step (`_prepublish_rebase_ref`); None, and no rebase, otherwise.
        self._prepublish_rebase: str | None = None
        # What `metadata.input_from` put in the workspace. Kept so the result
        # summary can report which upstream artifact this attempt actually ran
        # on -- the question every debugging of a wrong workflow output starts
        # with, and one the workspace cannot answer because it is destroyed.
        self._staged_inputs: list[inputs_mod.StagedInput] = []
        # Every forge credential `_git_token` read this attempt, as `(kind,
        # secret NAME or None)` -- never a value (lesson 43; see
        # `_forge_credential_summary`).
        self._forge_credentials_read: list[tuple[str, str | None]] = []
        # The declared inputs NOT staged because their upstream left nothing
        # to stage -- a no-change step's patch, anything of a skipped step --
        # while another input of this step changed something (archived issue 978). Each is
        # `{task_id, filename, left}`, and `result_summary` names them.
        self._inputs_left_nothing: list[dict[str, str]] = []
        # Where this step starts when it `builds_on` a step that left nothing
        # (`_builds_on_base`, archived issue 978): (the task whose branch is cloned, or ""
        # for the default branch; the no-change steps walked past). None
        # until resolved.
        self._builds_on_resolved: tuple[str, list[str]] | None = None
        # The text of each question the owner answered on the step this one
        # builds on (archived issue 2836), staged into the prompt and dropped from this
        # step's own questions.json if its agent asks one again.
        self._owner_answered: list[str] = []
        # The verdict this step's gate read (archived issue 264), as `result_summary` and the
        # pull request report it; None for a step with no gate.
        self._verdict: dict[str, Any] | None = None
        # The findings that verdict marked minor, which this step files on the
        # tenant's wave epic after its own work is done (archived issue 638).
        self._verdict_minors: tuple[verdict_mod.MinorFinding, ...] = ()
        self._verdict_minors_dropped = 0
        # The verdict file that gate read, as staged: `(path on disk, its
        # input_from name)`. A step whose gate stays shut re-publishes it as
        # its own artifact (`_republish_staged_verdict`); None with no gate.
        self._verdict_staged: tuple[Path, str] | None = None
        # The UI paths the patch this REVIEW step reviews touches (archived issue 2184),
        # measured before its agent starts; None when it is no review or the
        # patch touches none, which leaves its verdict exactly as written.
        self._ui_review_paths: list[str] | None = None
        # What `verdict.gate_rendered_review` found, for `result_summary`.
        self._rendered_review: dict[str, Any] | None = None
        # What `flow_output.normalise` found for a QA flow step, for `result_summary`.
        self._flow_output: dict[str, Any] | None = None
        # Where the pull request text of a step whose verdict gate stayed shut
        # came from (`_adopt_pull_request_text`), `{"title", "body"}`, each
        # `implementer`, `label` or None; None when the gate did not shut.
        self._pr_text_from: dict[str, str | None] | None = None
        # The terminal transition `_finish_terminal` decided on, kept while it
        # is written: when Firestore's contention outlasts every retry, the
        # crash path writes THIS -- the runner's state, exit code, message and
        # summary -- rather than the abort's text (archived issue 2650).
        self._decided_terminal: _DecidedTerminal | None = None
        # The contributors this integrator left out of its merge because they
        # changed nothing (`_integrates_with_changes`, 2026-10-05).
        self._no_change_contributors: list[str] = []
        # The contributors it left out because they SUCCEEDED having
        # published nothing by design -- a review that writes only a verdict
        # (`_integrates_with_changes`, archived issue 760).
        self._read_only_contributors: list[str] = []
        # What this task's dependants will stage from it, per
        # `metadata.expected_outputs` (archived issue 149). Kept so `_finalise` can name any
        # the attempt did not upload; see `agent_worker.expected_outputs`.
        self._expected_outputs: tuple[str, ...] = ()
        # The names a dependant declared, before the worker added its own
        # `pr-title.txt` request: a title a dependant stages is never excused.
        self._declared_names: tuple[str, ...] = ()
        # Every file in the artifacts folder the last `_upload_outputs` did not
        # upload for the file cap or the name bound (archived issue 228), by its WHOLE name.
        # The summary counts them and does not list them, so a declared output
        # among them is named here as written and not uploaded rather than as
        # never written (`_missing_causes`).
        self._artifacts_not_uploaded: tuple[str, ...] = ()
        # The same names, each with its cause (archived issue 165): the file cap is `cap`,
        # the name bound `refused`.
        self._not_uploaded_causes: dict[str, str] = {}
        # Whether an owed `verdict.json` was uploaded but is not a verdict
        # (archived issue 2837), as `_report_missing_outputs` last found it.
        self._verdict_unreadable = False
        # A CLI agent's task with no repository has what its agent CREATED in
        # the working folder uploaded, under `workdir/` (#184, owner decision
        # of 2026-09-26; `agent_worker.standalone_outputs`). `_standalone` is
        # decided in `_prepare`, once the task is known. `_restored_workdir` is
        # what a checkpoint brought back into `work/`. `_workdir_baseline` is
        # what was there just before the attempt's FIRST runner started,
        # less what an earlier attempt's agent created; None until then, and
        # None means nothing is uploaded from the working folder.
        self._standalone = False
        #: The task's additional repositories as they landed on disk this
        #: attempt (`extrarepos.prepare`, MULTI-REPO archived issue 1434), in the task's
        #: order; empty for a task with none, which is every task before it.
        self._extra_checkouts: list[extrarepos_mod.Checkout] = []
        #: The services the clone's `.swarm/test-services.toml` declares, up
        #: for the whole attempt (TS-RUN, archived issue 1627); None when nothing runs.
        self._test_services: testservices_mod.RunningServices | None = None
        self._restored_workdir: frozenset[str] = frozenset()
        self._workdir_baseline: frozenset[str] | None = None
        self._heartbeats = 0
        # When the last beat that returned started (monotonic), from whichever
        # thread made it: the beat thread under a long operation counts its
        # next beat from here, not from its own start (archived issue 426).
        self._last_beat_started: float | None = None
        self._deadline = time.monotonic() + config.timeout_seconds
        # THE RUNNER'S OWN DEADLINE. The task's, except on an index run, whose
        # graph write runs AFTER the agent (lane IX1, `indexrun`), and the
        # sweep after it (lane INDEX-RETENTION): both budgets are taken off
        # the agent's time before the agent starts, so an agent that uses all
        # of its time still leaves each its own.
        self._runner_deadline = self._deadline
        if indexrun_mod.is_index_run(config.runner_profile):
            reserve = indexrun_mod.budgets(config.timeout_seconds)
            self._runner_deadline -= reserve.graph_write + reserve.sweep
        # An index run's phases, in order, as they end (lane IX1); None on
        # every other task. `_finalise` puts them in the result summary.
        self._index_phases: list[indexrun_mod.PhaseRecord] | None = None
        self._index_agent_started: float | None = None
        # The base an incremental index run staged (lane IX2), for the graph
        # write's `--base-commit`; None on a full run and on every other task.
        self._index_base: str | None = None
        # Why promotion would refuse the agent's repo-index.json once its one
        # repair turn is spent (`indexspec`, archived issue 1693); None when it would not,
        # or on every other task. `_record_finalised` fails the attempt on it.
        self._index_refused: str | None = None
        # The account this attempt holds, if the pool gave it one. Set once and
        # kept: a credential reload must re-read the SAME account's secret, not
        # move the agent onto a different subscription mid-run.
        self._account: Assignment | None = None
        self._account_released = False
        # Once this attempt has decided it is NOT running on the pool, it stays
        # decided. `_build_child_env` is called again on the credential-reload
        # path, and asking the broker a second time there could move a running
        # agent from the tenant secret onto a pool account halfway through its
        # run -- or, if the pool had emptied in the meantime, raise
        # NoAccountAvailable from a call site that is not a parking one.
        self._account_declined: str | None = None
        # Accounts this attempt was given and could not read. Sent back to the
        # broker as `exclude` so the next ask does not return the same one.
        self._account_rejected: list[str] = []
        # A digest of the token each held account's secret last gave this
        # attempt (archived issue 1855), never the token: a refused credential whose secret
        # still holds the same token moves the attempt rather than reloading it.
        self._account_token_digests: dict[str, str] = {}
        # Why the runner was asked to stop at its turn boundary when it was not
        # a drain: the broker marked the held account `reauth_required`.
        self._move_why: str | None = None
        # The hold-status `move` that stopped the runner when it was not a
        # drain's: `plan_available`, a key holder asked back to a plan (Q1).
        self._move_asked: str | None = None
        # The attempt's `total_cost_usd` when the held account's hold began:
        # what ran on THIS hold is the total since (`_hold_spend`), the figure
        # its release, swap and hold-status read carry (§4.6).
        self._hold_spend_base = 0.0
        # The key whose credit-gone answer this attempt already reported
        # (`_report_credit_exhausted`): its 429 is not reported again as a
        # rate limit.
        self._credit_reported: str | None = None
        # The credential the attempt KEPT LAST (contract request 152), written
        # beside its spend (`_record_spend`): `account_id`, `credential_source`,
        # `credential_tier`. None until the pool decided either way.
        self._credential_kept: dict[str, Any] | None = None
        # THE ACCOUNT MID-RUN (S13-S15), for an attempt that holds one. The
        # session id the runner's stream named, kept to `--resume` it under
        # another account and NEVER logged (it is registered as a secret);
        # the session the next runner start continues, once a swap set it;
        # the forwarder for the held account's readings; the last turn count
        # the hold's move mark was asked at, so it is asked once per turn;
        # the moves so far; and whether the last start followed a swap.
        self._session_id: str | None = None
        self._resume_session: str | None = None
        #: Which continuation at the time limit this attempt is, once its
        #: restore found the note (`_read_continuation_note`); 0 otherwise.
        self._continued_at_time_limit = 0
        #: Why the session is resumed (RESUME_MOVED / RESUME_RELOADED), which
        #: decides what the resumed CLI is told.
        self._resume_reason: str | None = None
        #: The answer a session parked on a question resumes with, in
        #: `private/` (`_resume_parked_question`); None otherwise.
        self._question_answer_path: Path | None = None
        self._readings: ReadingForwarder | None = None
        self._turns_checked = 0
        self._channel_seen: int | None = None
        self._swaps = 0
        self._just_swapped = False
        #: The attempt's budget (archived issue 2459, `budget`), built with the first child
        #: environment of a budgeted CLI task; None for every other attempt,
        #: which then runs exactly as before.
        self.budget: budget_mod.BudgetGuard | None = None
        # EVERY AGENT RUN, COUNTED (SWAP-RUNS, archived issue 2457). An attempt can start
        # several runners -- an account swap, a short rate limit and a
        # reloaded credential all restart the agent in place -- and the
        # summary's `duration_seconds` used to be the LAST one's alone: two
        # 7,200 s timeouts on 2026-10-26 read "agent ran 4,600 s" because a
        # swap at minute 42 restarted it. One entry per runner start, closed
        # when it is reaped (`_close_run`); one per swap (`_emit_swapped`).
        # `_run_open` is the run now running, with what was spent before it.
        self._runs: list[dict[str, Any]] = []
        self._swap_log: list[dict[str, Any]] = []
        self._run_open: dict[str, Any] | None = None
        # WHAT THIS ATTEMPT SPENT, summed over every runner it started. An
        # attempt can start several -- a short rate limit and a reloaded
        # credential both restart in place -- and each one's result.json is
        # overwritten by the next, so the numbers are taken off each run as it
        # ends (`_collect_spend`) rather than read once at the end.
        self._spend: dict[str, Any] = {}
        # What `record_spend` last wrote, so the several exits that call
        # `_record_spend` write once per change rather than once per call.
        self._spend_recorded: dict[str, Any] | None = None
        # True from a runner's start until its result has been collected. It
        # is what stops `_cleanup` collecting the same run a second time.
        self._spend_pending = False
        # A RESUMED SESSION REPORTS ITS COST FOR THE WHOLE SESSION (archived issue 667, P14).
        # Whether the runner now running continues an earlier runner's session
        # (`--resume`), and `_spend` as it stood when that session began: the
        # session's later figures replace what it reported before, and what
        # was spent before it is kept (`_add_spend`). Set at each runner start
        # (`_note_runner_spend_start`).
        self._spend_resumes_session = False
        self._session_spend_base: dict[str, Any] | None = None
        # Set on the tenant-mismatch exit, the one path that must write NOTHING
        # -- not even spend onto what may be another tenant's attempt.
        self._writes_forbidden = False
        # The publish `_finalise` defers until the missing-output check has
        # passed (archived issue 165): what `_harvest_git(defer=True)` left for
        # `_publish_checked`. None when there is nothing to publish.
        self._deferred_publish: dict[str, Any] | None = None
        #: The clone base the final checkpoint's manifest recorded, when the
        #: harvest then adopted a rebase onto a newer one: what a retry's
        #: restore reads back, so `_push_unreachable_marker` records it (archived issue 2833).
        self._checkpointed_base: str | None = None
        # Every name the last upload skipped, with its cause (archived issue 165), uncut.
        self._skip_causes: dict[str, str] = {}
        # Until when (monotonic) the lease this worker last extended is live
        # (archived issue 70): the last heartbeat that landed, plus the extension it gave.
        # A mid-run Firestore call that fails before then is logged and the
        # loop goes on; one that fails after it is an outage, and the worker
        # leaves through `_exit_control_plane_outage`. None until a beat lands.
        self._lease_live_until: float | None = None
        # Set on every fenced exit (`_exit_fenced`, `_exit_fenced_mid_run`,
        # `_stand_down`). The task's event stream is then a newer generation's,
        # so `_give_back` returns the account and writes no `account_released`
        # into it (archived issue 380).
        self._fenced_exit = False
        # Set by `_exit_control_plane_outage` (archived issue 70): Firestore is what could not
        # be reached, so no event is attempted on the way out.
        self._control_plane_down = False
        # `carrier: branches` (D13): the branch and head the last push of this
        # step's work landed, `{"name", "head"}`, or None before any.
        self._carrier_pushed: dict[str, str] | None = None
        # The branch this attempt last pushed, as git in the publish repository
        # read it after the push (archived issue 667, observer P11): `{name, head, head_sha,
        # base_sha, commits: [{sha, subject}], commits_truncated}`. Becomes
        # `result_summary.branch`. None until a push lands, so a step that
        # pushed nothing never names a branch.
        self._pushed_branch: dict[str, Any] | None = None
        # The startup marks on the task's events (archived issue 667, lane OB1), each made
        # at most once per attempt: `clone_timed` after the clone,
        # `agent_started` at the first runner start. `_clone_marked` is also
        # what the agent start reads to say whether this attempt cloned.
        self._clone_marked = False
        self._agent_start_marked = False
        # `graph_ready` (lane GS-MEASURE): what staging the graph cost, once.
        self._graph_marked = False
        # The workflow base pin (`_upstream_base_pin`): what this step's clone
        # started from and why, `result_summary.git.base_pin`. None for a root
        # step, a non-workflow task, a step that starts from an upstream
        # branch, and an attempt resumed from a checkpoint (its clone is not
        # made again, so nothing is decided again).
        self._base_pin: dict[str, Any] | None = None
        # What `record_cpu_usage` last wrote onto the attempt (contract request
        # archived pull request 15), so the periodic readings and each runner's end write once per
        # change rather than once per heartbeat.
        self._cpu_recorded: dict[str, float | str] | None = None
        # The child-task path (docs/design/child-tasks.md): the attempt key,
        # the spool and the await. Inert unless the scheduler passed a nonce.
        self.children = children_mod.ChildPath(
            config, logger=deps.logger, memory=deps.memory, api=deps.child_api
        )
        # A person's `git-u-` slot comes only from swarm-api's broker, proven
        # with the attempt key `children` registered (archived issue 1041,
        # docs/design/user-scoped-secrets.md §3.2). Inert for a team token.
        self.forge_broker = forgebroker_mod.ForgeBroker(
            config, children=self.children, logger=deps.logger
        )
        # A stopped run's cost is priced by swarm-api from the platform's
        # `model_prices` setting (archived issue 1481), proven with the same attempt key.
        # The worker holds no price table. Without an API or a key, the cost
        # is not reported.
        self.spend_pricer = spendprice_mod.SpendPricer(
            config, children=self.children, logger=deps.logger
        )
        # The shared graph service's attempt ticket (docs/design/graph-service.md
        # §4.1, lane GS-WORKER): minted by `graphstage.prepare` only when the
        # workspace stored `graph_delivery: service`, then renewed on the
        # control poll. Signs with the child path's attempt key. Inert otherwise.
        self.graph_ticket = graph_ticket_mod.GraphTicket(
            config, children=self.children, logger=deps.logger
        )
        # The step's graph context, read by swarm-api (lane GRAPH-STAGING, part
        # of archived issue 1694): the registration, kept index versions and settings
        # `graphstage` stages from, so an unswitched tenant's worker lists no
        # Firestore documents its role cannot list. Signs with the same key.
        self.graph_context = graphroute_mod.GraphContextRoute(config, children=self.children)
        # The workspace secrets release (docs/design/workspace-secrets.md
        # §3.3, lane SEC-RELEASE): once per attempt, before the runner exists,
        # signed with the child path's attempt key. Inert for a profile whose
        # catalogue entry, and whose launched tools, declare no secret slot.
        self.secret_release = secretrelease_mod.SecretRelease(
            config, children=self.children, logger=deps.logger
        )
        # The caller's registered MCP servers (docs/design/registered-mcp-
        # servers.md §4.7, lane MCP-WORKER): attached once per attempt, after
        # the release, and served through a loopback relay that holds the
        # credentials in this heap. Inert for a task whose signed input names
        # no server -- which is every task while SWARM_MCP_SERVERS is off.
        self.mcp = mcp_attach_mod.McpAttachment(
            config, children=self.children, logger=deps.logger
        )
        # The TypeSafe forwarder's ticket and the loopback shim jev and the
        # judge call (workspace-secrets.md §3.5, SEC-MIGRATE-TYPESAFE): only
        # with SWARM_TYPESAFE_INJECT=on, which the scheduler sets with the
        # pod's `swarm-typesafe: inject` label, and only on GKE. Inert
        # otherwise: the key file path is today's. Signs with the child
        # path's attempt key.
        self.typesafe = typesafe_shim_mod.TypesafeInject(
            config, children=self.children, logger=deps.logger, environ=os.environ,
            backend=_config_backend(config),
        )
        # The live-browser path (docs/design/live-browser.md §4, lane L4): the
        # gate's ctl.sock slot, the tunnel ticket and the hand-off spool. Inert
        # unless the profile is a live-browser one; signs with the child
        # path's attempt key, so it needs that registered.
        self.handoff = handoff_mod.HandoffPath(
            config,
            logger=deps.logger,
            memory=deps.memory,
            children=self.children,
            progress=self._emit_startup_progress,
        )
        self._account_broker = deps.account_broker
        if self._account_broker is None and config.quota_broker_url:
            self._account_broker = AccountBroker(
                config.quota_broker_url,
                logger=deps.logger,
                audience=config.quota_broker_audience,
            )

    # ------------------------------------------------------------------
    # entrypoint
    # ------------------------------------------------------------------
    def run(self) -> int:
        # FIRST, so the probe runs alongside everything below -- the
        # generation check, the spec, the checkpoint restore, the secret
        # reads -- and none of it waits for the probe. Stopped when the
        # startup window closes (`_execute`); here as well, for every exit
        # that never reaches it, so no probe thread outlives the run.
        self._start_egress_probe()
        try:
            return self._run()
        finally:
            self._stop_egress_probe()
            # The scratch volumes' walker (`_scratch_reading`), started by the
            # first heartbeat: no sampler thread outlives the attempt.
            self._stop_scratch_sampler()

    def _start_egress_probe(self) -> None:
        """Probe the forge's path from process start (archived issue 721 (a)). Never raises.

        github.com always, repository or not (`egress.probe_targets`): the
        dispatcher sets no REPOSITORY_URL (`scheduler.dispatch.worker_env`),
        so the repository is known only from the signed task document, read
        well after this. `_add_egress_target` adds the clone host then, when
        it is elsewhere. A task with nothing to clone only has the probe
        stopped at the end of the startup window.
        """
        try:
            targets = egress_mod.probe_targets(self.cfg.repository_url)
            if not targets:
                return
            # Ready time counted from PROCESS START (observer P28): the same
            # origin as `agent_started`'s `seconds_since_process_start`, the
            # `Phases` clock the entrypoint started first. From the probe's
            # own start it read 1.6-4.0 s low.
            origin = time.monotonic() - float(self.phases.seconds_since_start())
            probe = egress_mod.EgressProbe(
                targets,
                cap_seconds=self.egress_cap_seconds,
                interval_seconds=self.egress_interval_seconds,
                connect=self.egress_connect,
                origin=origin,
            )
            probe.start()
            self._egress = probe
        except Exception as exc:  # a measurement is never worth an attempt  # noqa: BLE001
            self.log.warning("egress probe did not start", error=type(exc).__name__)

    def _add_egress_target(self, url: Any) -> None:
        """Probe the clone host too, once the verified task names it. Never raises."""
        probe = self._egress
        if probe is None or not isinstance(url, str):
            return
        try:
            probe.add_target(egress_mod.target_of(url))
        except Exception as exc:  # a measurement is never worth an attempt  # noqa: BLE001
            self.log.warning("egress probe: clone host not added", error=type(exc).__name__)

    def _stop_egress_probe(self) -> None:
        probe = self._egress
        if probe is not None:
            probe.stop()

    def _mark_egress_ready(self) -> None:
        """`egress_ready`: when the forge's path opened (archived issue 721 (a)). Once per attempt.

        Next to `clone_timed`, on the same RUNNING startup-mark path, written
        by `_maybe_clone` once the clone is done -- or has failed, the
        attempts whose timing matters most -- so inside the startup window and
        its budget. A task that clones nothing (or restores its clone from a
        checkpoint) writes none: its probe is only stopped with the window.
        The probe is stopped first: the clone was its only waiter. The
        detail is `egress`: `egress_ready_seconds` (process start to the first
        github.com connect that succeeded, None if none did), `probe_attempts`,
        how the probe `ended` (ready, cap, stopped) and the same two numbers
        per target. Numbers and host names only.
        """
        probe = self._egress
        if probe is None or self._egress_marked:
            return
        self._egress_marked = True
        probe.stop()
        self._emit_startup_mark("egress_ready", {"egress": probe.result()})

    def _run(self) -> int:
        self._install_signal_handlers()

        # ---- STEP 1: fencing, before anything else exists ---------------
        self.phases.enter("validate_generation")
        try:
            # Reads only. A SIGTERM here, in a read or in the wait between two
            # attempts, has nothing to undo, so it exits now.
            with self._signal_policy(_SIGNAL_EXIT_NOW), self.control.startup_budget():
                read = call_on_schedule(
                    # Looked up at each attempt, not bound once: a test may
                    # stand in for the control plane's method.
                    lambda: self.control.validate_generation(),
                    retryable=_generation_check_retryable,
                    schedule=CONTROL_PLANE_READ_SCHEDULE_SECONDS,
                    sleep=self.startup_sleep,
                    clock=self.startup_clock,
                    on_failed_attempt=self._control_plane_read_failed,
                )
        except FencedError as exc:
            return self._exit_fenced(exc)
        except TenantMismatchError as exc:
            return self._exit_tenant_mismatch(exc)
        except Exception as exc:
            # Not retried: a refusal, or something that is not Firestore's.
            if not _control_plane_unreachable(exc):
                raise
            return self._exit_control_plane_unreachable(exc)
        if read.error is not None:
            return self._exit_control_plane_unreachable(
                read.error, attempts=read.attempts, seconds=read.seconds
            )
        signals = read.value

        if signals.cancel_requested:
            # Cancelled between admission and start: nothing ran, so there is
            # nothing to clean up beyond giving the slot back.
            self.log.warning("task cancelled before the runner started")
            try:
                self.control.finish(
                    state=TaskState.CANCELLED,
                    exit_code=None,
                    error="cancelled before execution started",
                    end_cause=self.control.cancel_cause(),
                )
            except FencedWriteRefused as exc:
                # Fenced between the gate above and this write.
                return self._stand_down(exc, where="cancelled before start")
            return ExitCode.CANCELLED

        try:
            outcome = self._execute()
        except StartupInterrupted as exc:
            # FIRST, and not an Exception at all. A SIGTERM while the runner
            # was being prepared. See `_exit_interrupted_before_runner`.
            return self._exit_interrupted_before_runner(exc)
        except FencedWriteRefused as exc:
            # BEFORE `except FencedError`, of which it is a subclass. A task
            # write found the attempt superseded, either on the way out (a
            # park, a terminal state, a checkpoint) or at the transition into
            # RUNNING. Either way the attempt writes only its own record now.
            # `_exit_fenced` would emit into a task stream that belongs to
            # someone else.
            return self._stand_down(exc, where="leaving")
        except FencedError as exc:
            return self._exit_fenced(exc)
        except TenantMismatchError as exc:
            # BEFORE `except WorkerError`, and the order is the point. The
            # generic handler calls `_safe_finish`, which writes a terminal
            # state, an attempt record and an event -- and on this path every
            # one of those writes would land on ANOTHER TENANT's documents.
            return self._exit_tenant_mismatch(exc)
        except SpecSignatureInvalid as exc:
            # BEFORE `except WorkerError`. FAILED at once, whatever attempts
            # are left -- another attempt reads the same document -- with the
            # check's reason, task, version and digest, and no spec content.
            # Fenced like every terminal write: a superseded worker stands down.
            fenced = self._safe_finish(
                TaskState.FAILED,
                exit_code=exc.exit_code,
                error=str(exc),
                end_cause=EndCause.SPEC_SIGNATURE_INVALID,
                extra_summary={"spec_check": exc.spec_check()},
            )
            return exc.exit_code if fenced is None else fenced
        except WorkerError as exc:
            self.log.exception("worker failed", exc)
            fenced = self._safe_finish(
                TaskState.FAILED,
                exit_code=exc.exit_code,
                error=str(exc),
                end_cause=_end_cause_of(exc),
            )
            return exc.exit_code if fenced is None else fenced
        except Exception as exc:  # never exit without a durable terminal state
            self.log.exception("worker crashed", exc)
            decided = self._decided_terminal
            if decided is not None and _terminal_write_contended(exc):
                # The attempt had decided its end, and only Firestore's
                # contention kept it from landing (archived issue 2650): write that end, not
                # "409 Aborted ..." as the runner's error with no exit code.
                fenced = self._safe_finish(
                    decided.state,
                    exit_code=decided.exit_code,
                    error=decided.error,
                    end_cause=decided.end_cause,
                    extra_summary=decided.result_summary,
                )
                if fenced is not None:
                    return fenced
                return ExitCode.OK if decided.state == TaskState.SUCCEEDED else ExitCode.FAILED
            fenced = self._safe_finish(
                TaskState.FAILED,
                exit_code=ExitCode.FAILED,
                error=str(exc),
                end_cause=EndCause.RUNNER_ERROR,
            )
            return ExitCode.FAILED if fenced is None else fenced
        finally:
            self._cleanup()
        return outcome.exit_code

    # ------------------------------------------------------------------
    # the main path
    # ------------------------------------------------------------------
    def _execute(self) -> Outcome:
        # ---- STEPS 2-6: prepare the runner, inside the startup window ----
        #
        # A SIGTERM in here raises `StartupInterrupted` wherever the main
        # thread is, and `run` turns it into an exit that writes neither the
        # task nor the lease. Every Firestore call in here carries the startup
        # budget. The window closes BEFORE any park it decided on is made: a
        # park is several writes (state, event, lease release), and an
        # exception landing between two of them would leave the task parked
        # with its lease still held.
        try:
            with self._signal_policy(_SIGNAL_RAISE), self.control.startup_budget():
                prepared = self._prepare()
                if self._startup_interrupt is not None:
                    # A SIGTERM whose exception something caught and dropped
                    # (an `except BaseException` with no re-raise). The
                    # platform still asked this process to stop.
                    raise self._startup_interrupt
        except StartupInterrupted:
            raise
        except BaseException as exc:
            interrupt = self._startup_interrupt
            if interrupt is not None:
                # REPLACED ON THE WAY UP. `firestore.transactional` rolls back
                # on any BaseException, and the rollback can raise: a
                # ValueError for a transaction whose begin had not returned,
                # or the rollback RPC's own error. Whatever arrives here, a
                # SIGTERM was delivered in this window, and that decides the
                # exit. Raised again, not handled here, so `run` has one path
                # for every interrupt.
                interrupt.replaced_by = exc
                raise interrupt
            if not isinstance(exc, Exception) or isinstance(exc, WorkerError):
                # Fenced, tenant mismatch, cancelled and the other deliberate
                # exits keep their own handlers in `run`.
                raise
            unavailable = _unavailable_cause(exc)
            if unavailable is None:
                raise
            return Outcome(exit_code=self._exit_unavailable_before_runner(exc, unavailable))
        finally:
            # The startup window is over: no probe thread runs past it.
            self._stop_egress_probe()
        if callable(prepared):
            # A worker action, run after the startup window: it classifies its
            # own outages as exit 69 (`_exit_action_unavailable`, archived issue 2000).
            return prepared()
        child_env = prepared
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        # What is in the working folder before the FIRST runner starts: once
        # per attempt, so an in-place restart below does not make the first
        # run's files "existing" ones (#184, standalone tasks only).
        self._take_workdir_baseline()

        # ---- STEP 6a: an index run's extractor, before its agent (IX1) --
        if indexrun_mod.is_index_run(cfg.runner_profile):
            stopped = self._index_extract()
            if stopped is not None:
                return stopped

        # ---- STEPS 7-9: run the child, supervised -----------------------
        attempt_number = 0
        credential_reloads = 0
        # The codex runner's own stops before its plan token's hour, counted
        # apart from the refusals (part of archived issue 2688).
        expiry_reloads = 0
        while True:
            attempt_number += 1
            result = self._run_child_supervised(child_env)
            if isinstance(result, Outcome):
                return result                      # cancelled / fenced / parked
            # ON A POOL ACCOUNT, A STOP MAY BE A MOVE (S13/S14): the account
            # ran out or is draining, and the attempt continues on another one
            # in this same lease, attempt and workspace with `--resume`.
            moved = self._move_account_after(result)
            if isinstance(moved, Outcome):
                return moved
            if moved is not None:
                child_env = moved
                # A move is not one of the in-place quota retries.
                attempt_number -= 1
                continue
            quota = self._quota_from_child(result)
            if quota is None:
                # A REFUSED credential, which is not a failed attempt. The
                # platform refreshes accounts on a timer whether or not an
                # agent is holding one, and refreshing an OAuth credential
                # revokes the previously issued token -- so a long attempt can
                # have its token pulled out from under it. The secret already
                # holds the replacement; re-reading it costs one API call and
                # saves the attempt.
                refusal = self._credential_refusal()
                # A SCHEDULED RELOAD, NOT A REFUSAL (part of archived issue 2688). The codex
                # runner stopped itself before its ChatGPT plan's access token
                # reached its hour (`runners.codex.ExpiryStop`): nothing
                # refused the account, so no auth error goes on its health
                # record, the dead-token test is not asked (the broker may not
                # have written the successor yet, and a token still valid for
                # minutes is not dead) and the bounded refusal reloads are not
                # spent. It has its own bound, MAX_EXPIRY_RELOADS; past it the
                # stop is judged as any refusal is.
                scheduled = (
                    refusal is not None
                    and self._account is not None
                    and refusal.get("marker") == PLAN_EXPIRY_MARKER
                    and expiry_reloads < MAX_EXPIRY_RELOADS
                )
                if refusal is not None and self._account is not None and not scheduled:
                    # An auth error on the held account, for its health record
                    # (archived issue 2302); every refusal, the reloaded ones included.
                    self.control.report_account_error(
                        provider=str(refusal.get("provider") or self.cfg.provider or ""),
                        account=self._account,
                        kind="auth",
                        message="the provider refused the account's credential",
                    )
                    # A DEAD TOKEN MOVES THE ATTEMPT (archived issue 1855). Reloading helps
                    # only when the refresher wrote a successor; a token that
                    # came back unchanged, a successor refused again, or an
                    # account the broker marked `reauth_required` moves to a
                    # healthy account of the same pool and resumes there, in
                    # this lease, attempt and generation -- or parks on the
                    # pool when there is none. Never a failure.
                    why = self._dead_credential(credential_reloads)
                    if why is not None:
                        ws.credential_path.unlink(missing_ok=True)
                        moved = self._swap_account(SWAP_UNUSABLE, why=why)
                        if isinstance(moved, Outcome):
                            return moved
                        if moved is not None:
                            child_env = moved
                            attempt_number -= 1
                            continue
                if refusal is not None and (
                    scheduled or credential_reloads < MAX_CREDENTIAL_RELOADS
                ):
                    if scheduled:
                        expiry_reloads += 1
                    else:
                        credential_reloads += 1
                    # RESUMED, NOT STARTED AGAIN (archived issue 626). A restart from the
                    # prompt threw away everything the agent had done -- 26
                    # restarts and ~169 agent-minutes in the 2026-10-05
                    # history. On a held account the runner's channel named
                    # the session (`_move_account_after` read it above); on
                    # the tenant's own credential -- and codex, which never
                    # holds an account, is always there -- the runner's
                    # session file did (`_session_channel_env`). Either way
                    # the restart continues it with the CLI's resume flag,
                    # exactly as an account move does. A run that named no
                    # session, or a runner that cannot resume (neither file
                    # exists for one), restarts from the prompt as before.
                    if self._account is None:
                        self._note_session(self._read_session_file())
                    resumed = self._session_id is not None
                    if resumed:
                        self._resume_session = self._session_id
                        self._resume_reason = RESUME_RELOADED
                        # The next runner counts its turns from zero.
                        self._turns_checked = 0
                    self.log.warning(
                        "credential refused; reloading it and restarting in place",
                        provider=refusal.get("provider"),
                        marker=refusal.get("marker"),
                        reload=credential_reloads,
                        resumed=resumed,
                    )
                    try:
                        # An audit record (archived issue 70): an event that cannot be
                        # written must not cost the reload, as it does not
                        # cost a checkpoint (`_announce_checkpoint`).
                        self.control.emit(
                            EventType.RETRYING,
                            {
                                "cause": "credential_reloaded",
                                "provider": refusal.get("provider"),
                                "reload": credential_reloads,
                                "resumed": resumed,
                            },
                        )
                    except (FencedError, TenantMismatchError):
                        raise
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(
                            "could not record the credential reload; reloading anyway",
                            error=f"{type(exc).__name__}: {_one_line(exc)}",
                        )
                    ws.credential_path.unlink(missing_ok=True)
                    # Rebuilt, not patched: `_build_child_env` is the one place
                    # that knows which env names this profile's credential maps
                    # to, and `access()` always reads `latest`, so this picks up
                    # whatever the refresher wrote.
                    #
                    # GUARDED LIKE STEP 6, because it is the same call and it
                    # can raise the same two exceptions. Unguarded, a pool that
                    # had emptied or an account whose secret had become
                    # unreadable since the agent started would leave
                    # NoAccountAvailable -- a plain RuntimeError -- to reach
                    # run()'s generic handler and write TaskState.FAILED, which
                    # is the one outcome this whole path exists to avoid.
                    try:
                        child_env = self._build_child_env()
                    except CredentialMissing as exc:
                        return self._park_credential_missing(exc)
                    except NoAccountAvailable as exc:
                        return self._park_no_account(exc)
                    except Exception as exc:
                        # THE TENANT READ COULD NOT REACH FIRESTORE (archived issue 70),
                        # after its budget. There is no runner to keep going
                        # and no credential to start the next one with: this
                        # is a runner start, and an outage at a runner start
                        # is exit 69, as before the first runner. Anything
                        # else -- a fence included -- is raised as before.
                        if isinstance(
                            exc, (FencedError, TenantMismatchError)
                        ) or not _control_plane_unreachable(exc):
                            raise
                        return self._exit_control_plane_outage(
                            None, exc, where="for the credential reload's tenant read"
                        )
                    continue
                if refusal is not None:
                    self.log.error(
                        "credential still refused after reloading; this account "
                        "needs re-authentication rather than another attempt",
                        provider=refusal.get("provider"),
                    )
                break
            decision = decide(
                quota,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            self.control.update_quota_state(
                provider=quota.provider,
                state=quota.state,
                retry_after_seconds=quota.retry_after_seconds,
                reset_at=quota.reset_at,
                # The pool account the runner was refused on, when it held
                # one (archived issue 2302): the 429 is that account's, not the tenant's.
                # Not a key whose credit is gone: that went as `exhausted`.
                account=self._quota_account(),
                message=quota.detail,
            )
            if decision.park or attempt_number > MAX_IN_WORKER_RETRIES:
                return self._park_for_quota(decision, source="runner")
            # Short wait only: the slot stays held because reacquiring it would
            # cost more than the wait itself.
            self.log.warning(
                "short provider wait; retrying in place",
                wait_seconds=decision.wait_seconds,
                attempt=attempt_number,
            )
            self.control.emit(
                EventType.RETRYING,
                {"wait_seconds": decision.wait_seconds, "attempt": attempt_number},
            )
            self._sleep_with_heartbeat(decision.wait_seconds)
            ws.quota_path.unlink(missing_ok=True)

        # ---- STEP 9a': a question the owner already answered (archived issue 2836) -----
        # Dropped from questions.json before anything parks on it or uploads
        # it, so the owner is never asked twice.
        if self._owner_answered:
            dropped = owner_questions_mod.drop_answered(ws.artifacts, self._owner_answered)
            if dropped:
                self.log.info(
                    "questions already answered on the step this one builds on; dropped",
                    dropped=len(dropped),
                )

        # ---- STEP 9b: the children's await (child tasks, §3.3) ----------
        awaited = self._await_children(result)
        if awaited is not None:
            return awaited

        # ---- STEP 9b': the owner's answers, or a release (AU-7) ----------
        waited = self._await_owner_or_release(result)
        if waited is not None:
            return waited

        # ---- STEP 9c: an index run's graph write, after its agent (IX1) --
        if self._index_phases is not None:
            stopped = self._index_after_agent(result)
            if stopped is not None:
                return stopped

        # ---- STEPS 10-12: artifacts, checkpoint, terminal state, lease --
        return self._finalise(result)

    # ------------------------------------------------------------------
    # an index run's deterministic passes (lane IX1, `indexrun`)
    # ------------------------------------------------------------------
    def _index_env(self) -> dict[str, str]:
        ws = self.ws
        assert ws is not None
        home = ws.private / "index-home"
        home.mkdir(parents=True, exist_ok=True)
        return indexrun_mod.phase_env(
            home=home, tmp=ws.tmp, tenant_id=self.cfg.tenant_id, bucket=self.cfg.artifact_bucket
        )

    def _note_index_phase(self, record: indexrun_mod.PhaseRecord) -> None:
        """Keep, log and write down one phase: the agent reads the file (`PHASES_FILE`)."""
        ws = self.ws
        assert ws is not None and self._index_phases is not None
        self._index_phases.append(record)
        self.log.info("index phase finished", **record.as_dict())
        try:
            indexrun_mod.write_phases(ws.work, self._index_phases)
        except OSError as exc:
            self.log.warning("could not write the index phases file", error=str(exc))

    def _index_extract(self) -> Outcome | None:
        """`swarm-repo-index` on the checkout, before the agent. None to go on.

        A failed, timed-out or missing extractor does not stop the run: the
        agent computes the mechanical fields itself, and the phases file
        tells it why. Only a stop the supervision would honour mid-agent -- a
        fence, a cancel, a SIGTERM, an outage past the lease -- ends it here.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        self._index_phases = []
        # A restored checkpoint may hold an earlier attempt's outputs. They
        # are not this run's, and the agent must not start from them.
        for name in (indexrun_mod.EXTRACT_FILE, indexrun_mod.GRAPH_FILE,
                     indexrun_mod.BASE_INDEX_FILE, indexrun_mod.BASE_GRAPH_FILE):
            (ws.work / name).unlink(missing_ok=True)
        env = self._index_env()
        program = indexrun_mod.resolve(indexrun_mod.EXTRACTOR_COMMAND, env)
        checkout = ws.checkout()
        base_sha: str | None = None
        if checkout.is_dir() and program is not None:
            staged = self._index_stage_base(env)
            if isinstance(staged, Outcome):
                return staged
            base_sha = staged
        self._index_base = base_sha
        if not checkout.is_dir():
            record = indexrun_mod.PhaseRecord(
                "extract", "skipped", 0.0, reason="the task has no checkout"
            )
        elif program is None:
            record = indexrun_mod.PhaseRecord(
                "extract", "skipped", 0.0, reason="not installed in this image"
            )
        else:
            self.phases.enter("index_extract")
            budget = indexrun_mod.budgets(cfg.timeout_seconds)
            ran = self._run_index_phase(
                "extract",
                indexrun_mod.extractor_argv(
                    program, checkout=checkout, work=ws.work, lsp_total=budget.lsp_total,
                    base_sha=base_sha,
                    # A full run's base carries only the servers' answers (archived issue 2991).
                    base_lsp_only=indexrun_mod.base_lsp_only((self._task or {}).get("input")),
                    # What each server cost last time, from the signed prompt
                    # (INDEX-BUDGETS, archived issue 1693); None is the equal split.
                    lsp_costs=indexrun_mod.lsp_costs((self._task or {}).get("input")),
                ),
                cwd=checkout,
                env=env,
                timeout=budget.extract,
            )
            if isinstance(ran, Outcome):
                return ran
            record = ran
        self._note_index_phase(record)
        self._index_agent_started = time.monotonic()
        return None

    def _index_stage_base(self, env: dict[str, str]) -> str | None | Outcome:
        """Stage an incremental run's base (lane IX2): the base sha, None, or a stop.

        None -- and a full run -- when the task asked for none, or when the
        base cannot be staged; the `stage_base` record says why, and the
        agent reads it in the phases file. See `indexrun`'s module doc for
        what is read, from where, and what each read is checked against.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        task = self._task or {}
        # The signed prompt names the base; the unsigned metadata is never read.
        base_sha = indexrun_mod.requested_base(task.get("input"))
        if base_sha is None:
            return None
        started = time.monotonic()

        def skipped(reason: str) -> None:
            for name in (indexrun_mod.BASE_INDEX_FILE, indexrun_mod.BASE_GRAPH_FILE):
                (ws.work / name).unlink(missing_ok=True)
            self._note_index_phase(indexrun_mod.PhaseRecord(
                "stage_base", "skipped", round(time.monotonic() - started, 3),
                reason=f"a full run: {reason}",
            ))

        # The signed spec's repository, as the graph write's; never metadata.
        where = indexrun_mod.target(
            cfg.tenant_id, task.get("repository_url") or cfg.repository_url
        )
        if isinstance(where, str):
            skipped(where)
            return None
        program = indexrun_mod.resolve(indexrun_mod.GRAPH_WRITER_COMMAND, env)
        if program is None:
            skipped(f"{indexrun_mod.GRAPH_WRITER_COMMAND} is not installed in this image")
            return None
        self.phases.enter("index_stage_base")
        try:
            base = indexrun_mod.resolve_base(
                self.db, tenant_id=cfg.tenant_id, where=where, base_sha=base_sha,
                call_options=self.control.call_options("tenant"),
            )
        except Exception as exc:  # noqa: BLE001
            skipped(f"the base version could not be read ({type(exc).__name__})")
            return None
        if isinstance(base, str):
            skipped(base)
            return None
        # The base index: its copy under repos/ first (lane IX3), which no
        # lifecycle rule expires; then the staged-input path a workflow input
        # takes -- the successful attempt's manifest, a key under this
        # tenant's prefix -- for a version promoted before the copy existed.
        # Either is checked against the digest promotion recorded.
        destination = ws.work / indexrun_mod.BASE_INDEX_FILE
        try:
            kept = self.store.download_bytes(where.index_key(base_sha))
        except Exception:  # noqa: BLE001
            kept = None
        if (kept is not None and len(kept) <= indexrun_mod.MAX_BASE_INDEX_BYTES
                and indexrun_mod.content_digest(kept) == base.digest):
            destination.write_bytes(kept)
            return self._index_read_base_graph(program, env, where, base, base_sha, started,
                                               skipped, source="its copy under repos/")
        try:
            upstream = inputs_mod.fetch_upstream_task(
                self.db, upstream_task_id=base.task_id, tenant_id=cfg.tenant_id,
                call_options=self.control.call_options("tenant"),
            )
            reference = inputs_mod.artifact_reference(
                upstream, tenant_id=cfg.tenant_id, upstream_task_id=base.task_id,
                filename=indexrun_mod.INDEX_FILE,
            )
            if reference.size_bytes > indexrun_mod.MAX_BASE_INDEX_BYTES:
                skipped(f"the base index is {reference.size_bytes} bytes, over the "
                        f"{indexrun_mod.MAX_BASE_INDEX_BYTES} an index may be")
                return None
            self.store.download_file(reference.key, destination)
            data = destination.read_bytes()
        except InputUnavailable as exc:
            skipped(self._scrub(str(exc)))
            return None
        except Exception as exc:  # noqa: BLE001
            skipped(f"the base index could not be fetched ({type(exc).__name__})")
            return None
        if indexrun_mod.content_digest(data) != base.digest:
            skipped("the base index no longer matches the digest recorded when it was "
                    "promoted, so it is not built on")
            return None
        return self._index_read_base_graph(program, env, where, base, base_sha, started, skipped,
                                           source="the index task's artifact")

    def _index_read_base_graph(
        self, program: str, env: dict[str, str], where: indexrun_mod.Target,
        base: indexrun_mod.BaseVersion, base_sha: str, started: float,
        skipped: Callable[[str], None], *, source: str,
    ) -> str | None | Outcome:
        """`swarm-repo-graph read` of the base, once its index is staged from `source`.

        Its budget is sized from the base graph's size, which the staged
        index's graph counts give (`indexrun.base_graph_bytes`, archived issue 1469). A
        read that runs out is named, with the last step the tool said it
        finished, in the log and the `stage_base` record; `skipped` removes
        whatever it left, so the run is FULL, never built on part of a base.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        try:
            staged_index = json.loads(
                (ws.work / indexrun_mod.BASE_INDEX_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            staged_index = None
        size = indexrun_mod.base_graph_bytes(staged_index)
        budget = indexrun_mod.budgets(cfg.timeout_seconds, base_graph_bytes=size).base_read
        ran = self._run_index_phase(
            "stage_base",
            indexrun_mod.graph_read_argv(
                program, work=ws.work, where=where, bucket=cfg.artifact_bucket, base=base
            ),
            cwd=ws.work,
            env=env,
            timeout=budget,
        )
        if isinstance(ran, Outcome):
            return ran
        if ran.status == "timed_out":
            said = indexrun_mod.last_line(ws.logs / "repo-index.stage_base.stderr.log")
            got = self._scrub(said) if said else "it said nothing before it was stopped"
            sized = f"{size} bytes" if size is not None else "an unknown size"
            self.log.warning(
                "index base graph read ran out of its budget",
                read=f"{indexrun_mod.GRAPH_WRITER_COMMAND} read", base=base_sha[:12],
                budget_seconds=budget, base_graph_bytes=size, seconds=ran.seconds,
                last_progress=got,
            )
            skipped(f"{indexrun_mod.GRAPH_WRITER_COMMAND} read of base {base_sha[:12]}'s "
                    f"graph ran out of its {budget}-second budget (sized for {sized}) "
                    f"after {ran.seconds} s; the last it said: {got}")
            return None
        if ran.status != "ok" or not (ws.work / indexrun_mod.BASE_GRAPH_FILE).is_file():
            skipped(f"the base graph was not read ({ran.status}: {ran.reason or 'no file'})")
            return None
        ran.seconds = round(time.monotonic() - started, 3)
        ran.reason = f"base {base_sha[:12]} staged, its index from {source}"
        self._note_index_phase(ran)
        return base_sha

    def _index_after_agent(self, result: ChildResult) -> Outcome | None:
        """Record the agent's phase, then `swarm-repo-graph write`. None to go on.

        The write runs only on what it needs: an agent that finished cleanly,
        the extractor's graph and the agent's index, and a target derived
        from the signed spec (`indexrun.target`). A write that fails leaves
        the index without `graph.manifest_digest`, which promotion reads as
        no graph; the write is resumable, so the next run completes it.
        The write measures the answer latency only when the signed prompt
        says so (`indexrun.measure_latency_hours`, archived issue 1840): opt-in per
        registration, and a measurement never fails the write.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        started = self._index_agent_started or time.monotonic()
        agent_ok = not result.timed_out and result.exit_code == 0
        self._note_index_phase(
            indexrun_mod.PhaseRecord(
                "agent",
                "ok" if agent_ok else ("timed_out" if result.timed_out else "failed"),
                round(time.monotonic() - started, 3),
                exit_code=result.exit_code,
            )
        )
        # The signed spec's repository (the worker refuses a REPOSITORY_URL
        # that disagrees with it); never the unsigned metadata.
        where = indexrun_mod.target(
            cfg.tenant_id, (self._task or {}).get("repository_url") or cfg.repository_url
        )
        env = self._index_env()
        program = indexrun_mod.resolve(indexrun_mod.GRAPH_WRITER_COMMAND, env)
        budget = min(
            float(indexrun_mod.budgets(cfg.timeout_seconds).graph_write), self._remaining_seconds()
        )
        # THE DOCUMENT, BY PROMOTION'S RULES (lane INDEX-ROUTES, archived issue 1693). The
        # runner gave the agent one turn to fix what this finds
        # (`cliagent.INDEX_REPAIR_MAX_TURNS`); what is still wrong fails the
        # attempt with the errors, and the graph write never stores it.
        index_path = ws.artifacts / indexrun_mod.INDEX_FILE
        if agent_ok and index_path.is_file():
            checked = time.monotonic()
            self._index_refused = indexspec_mod.refusal(
                indexspec_mod.problems_of_file(index_path) or [])
            self._note_index_phase(indexrun_mod.PhaseRecord(
                "index_check", "ok" if self._index_refused is None else "failed",
                round(time.monotonic() - checked, 3),
                reason=self._scrub(self._index_refused) if self._index_refused else None))
        reason = None
        if not agent_ok:
            reason = "the agent did not finish cleanly"
        elif self._index_refused is not None:
            reason = f"promotion would refuse {indexrun_mod.INDEX_FILE}"
        elif not (ws.work / indexrun_mod.GRAPH_FILE).is_file():
            reason = "the extractor wrote no graph"
        elif not (ws.artifacts / indexrun_mod.INDEX_FILE).is_file():
            reason = f"the agent wrote no {indexrun_mod.INDEX_FILE}"
        elif isinstance(where, str):
            reason = where
        elif program is None:
            reason = "not installed in this image"
        elif budget < 1:
            reason = "the task's time is spent"
        if reason is not None:
            self._note_index_phase(
                indexrun_mod.PhaseRecord("graph_write", "skipped", 0.0, reason=reason)
            )
            self._note_index_phase(indexrun_mod.PhaseRecord(
                indexrun_mod.SWEEP_PHASE, "skipped", 0.0,
                reason=f"the graph write did not run ({reason}); nothing is retired"))
            return None
        assert not isinstance(where, str) and program is not None
        self.phases.enter("index_graph_write")
        ran = self._run_index_phase(
            "graph_write",
            indexrun_mod.graph_write_argv(
                program, work=ws.work, artifacts=ws.artifacts, where=where,
                bucket=cfg.artifact_bucket, base_sha=self._index_base,
                measure_latency_hours=indexrun_mod.measure_latency_hours(
                    (self._task or {}).get("input")),
            ),
            cwd=ws.work,
            env=env,
            timeout=budget,
        )
        if isinstance(ran, Outcome):
            return ran
        self._note_index_phase(ran)
        return self._index_sweep(program, where, env, left=max(0.0, budget - ran.seconds))

    def _index_sweep(
        self, program: str, where: indexrun_mod.Target, env: dict[str, str], *, left: float
    ) -> Outcome | None:
        """RETENTION OF repos/ (§2.3; lane INDEX-RETENTION, part of archived issue 1693), after the write.

        Best effort: the run's outcome never depends on it. It keeps the
        commits swarm-api named in the signed prompt (`indexrun.kept_commits`),
        and reads no database for them. Its budget is its own reserve plus
        whatever the write left of its own. Only a stop the supervision would
        honour mid-agent -- a fence, a cancel, a SIGTERM, an outage past the
        lease -- ends the attempt here, exactly as during the write.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        keep = indexrun_mod.kept_commits((self._task or {}).get("input"),
                                         own=self._index_commit())
        budget = min(float(indexrun_mod.budgets(cfg.timeout_seconds).sweep) + left,
                     self._remaining_seconds())
        reason = keep if isinstance(keep, str) else (
            "the task's time is spent" if budget < 1 else None)
        if reason is not None:
            self.log.info("index retention skipped; nothing is retired", reason=reason)
            self._note_index_phase(indexrun_mod.PhaseRecord(
                indexrun_mod.SWEEP_PHASE, "skipped", 0.0, reason=reason))
            return None
        assert not isinstance(keep, str)
        self.phases.enter("index_sweep")
        ran = self._run_index_phase(
            indexrun_mod.SWEEP_PHASE,
            indexrun_mod.sweep_argv(program, where=where, bucket=cfg.artifact_bucket, keep=keep),
            cwd=ws.work,
            env=env,
            timeout=budget,
        )
        if isinstance(ran, Outcome):
            return ran
        try:
            stdout = (ws.logs / f"repo-index.{indexrun_mod.SWEEP_PHASE}.stdout.log").read_text(
                "utf-8", "replace")
        except OSError:
            stdout = ""
        ran.retention = indexrun_mod.sweep_retention(stdout)
        refused = indexrun_mod.sweep_refusal(stdout)
        if ran.status == "ok" and refused is not None:
            ran.reason = self._scrub(refused)
        self._note_index_phase(ran)
        return None

    def _index_commit(self) -> str | None:
        """The commit an index run indexed: the SIGNED spec's `repository_ref`
        (`swarm_api.repoindex.indexer_task` sets it to the sha), never metadata."""
        ref = (self._task or {}).get("repository_ref") or self.cfg.repository_ref
        return ref if isinstance(ref, str) and indexrun_mod.is_commit_sha(ref) else None

    def _index_keep_copy(self, artifacts: list[dict[str, Any]]) -> None:
        """Copy the uploaded repo-index.json to its own home under repos/ (§2, lane IX3).

        The artifact lives under `tasks/`, which the bucket's lifecycle cold-
        stores and expires; the index must outlive it, so the worker -- the
        only identity that may write under the tenant's prefix, swarm-api
        reads only -- copies it to `indexrun.Target.index_key`. The bytes are
        read back from the uploaded object, so the copy is the artifact byte
        for byte, which promotion checks before it records the copy. A copy
        that fails is logged and the run goes on: promotion then records the
        artifact alone, as before.
        """
        cfg = self.cfg
        if not indexrun_mod.is_index_run(cfg.runner_profile):
            return
        if not any(entry.get("name") == indexrun_mod.INDEX_FILE for entry in artifacts):
            return
        where = indexrun_mod.target(
            cfg.tenant_id, (self._task or {}).get("repository_url") or cfg.repository_url
        )
        commit = self._index_commit()
        if isinstance(where, str) or commit is None:
            self.log.warning(
                "the index was not copied under repos/",
                reason=where if isinstance(where, str) else "the task names no commit sha",
            )
            return
        key = where.index_key(commit)
        try:
            data = self.store.download_bytes(f"{cfg.artifact_prefix}/{indexrun_mod.INDEX_FILE}")
            self.store.upload_bytes(key, data, content_type="application/json")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("the index was not copied under repos/", error=type(exc).__name__)
            return
        self.log.info("index copied under repos/", key=key, bytes=len(data))

    def _run_index_phase(
        self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> indexrun_mod.PhaseRecord | Outcome:
        """One deterministic phase, supervised as the runner is.

        The lease is beaten, the control plane polled and the work tree
        checkpointed on the runner's own cadences (invariants 5 and 8): an
        extractor takes minutes, longer than `_heartbeat_meanwhile` is
        bounded to. A fence, a cancel, a SIGTERM or an outage past the lease
        stops the phase and ends the attempt exactly as it would end the
        agent's. A phase over its timeout is stopped and recorded
        `timed_out`; the run goes on.
        """
        cfg, ws = self.cfg, self.ws
        assert ws is not None
        started = time.monotonic()
        deadline = min(started + timeout, self._deadline)
        child = ChildProcess(
            argv,
            cwd=cwd,
            env=env,
            stdout_path=ws.logs / f"repo-index.{name}.stdout.log",
            stderr_path=ws.logs / f"repo-index.{name}.stderr.log",
            max_stdout_bytes=INDEX_PHASE_CAPTURE_BYTES,
            max_stderr_bytes=INDEX_PHASE_CAPTURE_BYTES,
            keep_tail=True,
            niceness=cfg.runner_niceness,
            logger=self.log,
        )
        self.log.info("index phase started", phase=name, timeout_seconds=round(timeout, 3))
        child.start()
        next_heartbeat = started + cfg.heartbeat_interval_seconds
        next_poll = started + cfg.control_poll_seconds
        next_checkpoint = started + cfg.checkpoint_interval_seconds
        while True:
            due = min(next_heartbeat, next_poll, next_checkpoint, deadline)
            if child.wait(max(0.05, min(due - time.monotonic(), 5.0))) is not None:
                break
            now = time.monotonic()
            if self._interrupted:
                return self._handle_interruption(child)
            if now >= next_heartbeat:
                next_heartbeat = now + cfg.heartbeat_interval_seconds
                try:
                    self._heartbeat()
                except Exception as exc:  # noqa: BLE001
                    outage = self._control_plane_call_failed("heartbeat", exc)
                    if outage is not None:
                        return self._exit_control_plane_outage(
                            child, outage, where=f"past the lease during the index {name}"
                        )
            if now >= next_poll:
                next_poll = now + cfg.control_poll_seconds
                try:
                    signals = self.control.poll(cfg.provider)
                except Exception as exc:  # noqa: BLE001
                    outage = self._control_plane_call_failed("control poll", exc)
                    if outage is not None:
                        return self._exit_control_plane_outage(
                            child, outage, where=f"past the lease during the index {name}"
                        )
                    signals = None
                if signals is not None and signals.is_fenced(cfg.generation):
                    return self._exit_fenced_mid_run(
                        child,
                        observed_generation=signals.generation,
                        reason=f"fenced during the index {name}: task at generation "
                        f"{signals.generation} in {signals.state.value}",
                    )
                if signals is not None and signals.cancel_requested:
                    self.log.warning("cancellation requested; stopping the index phase",
                                     phase=name)
                    child.terminate(cfg.termination_grace_seconds, reason="cancelled")
                    child.finish()
                    return self._finish_cancelled()
            if now >= next_checkpoint:
                try:
                    self._checkpoint("periodic")
                except FencedError as exc:
                    return self._exit_fenced_mid_run(
                        child, observed_generation=exc.actual, reason=str(exc)
                    )
                next_checkpoint = now + cfg.checkpoint_interval_seconds
            if now >= deadline:
                self.log.warning("index phase timed out", phase=name,
                                 timeout_seconds=round(timeout, 3))
                child.mark_timed_out()
                child.terminate(cfg.termination_grace_seconds, reason=f"index {name} timeout")
                break
        done = child.finish()
        seconds = round(time.monotonic() - started, 3)
        if done.timed_out:
            return indexrun_mod.PhaseRecord(
                name, "timed_out", seconds, exit_code=done.exit_code,
                reason=f"over its {round(timeout)}-second budget",
            )
        if done.exit_code != 0:
            said = indexrun_mod.last_line(ws.logs / f"repo-index.{name}.stderr.log")
            return indexrun_mod.PhaseRecord(
                name, "failed", seconds, exit_code=done.exit_code,
                reason=self._scrub(said) if said else f"exited {done.exit_code}",
            )
        return indexrun_mod.PhaseRecord(name, "ok", seconds, exit_code=0)

    # ------------------------------------------------------------------
    # worker actions (archived issue 295): merge and post-verdict, no runner
    # ------------------------------------------------------------------
    def _run_worker_action(
        self,
        action: WorkerAction,
        task: dict[str, Any],
        staged: list[inputs_mod.StagedInput],
    ) -> Outcome:
        """Perform `action` in this process and end the task by its outcome.

        No runner child is started, so there is nothing to supervise, stop or
        harvest. The lease is heartbeaten from a thread while the action talks
        to the forge (`_heartbeat_meanwhile`): a merge reads several lists.
        No checkpoint is written: there is no workspace work to keep, and a
        lost attempt repeats the action, which the pinned head makes either a
        no-op or a refusal (merge-step.md §6 row 39).
        """
        ws = self.ws
        assert ws is not None
        workflow_id = task.get("workflow_id")
        workflow_id = workflow_id if isinstance(workflow_id, str) else None
        ctx = post_verdict_mod.ActionContext(
            tenant_id=self.cfg.tenant_id,
            task_id=self.cfg.task_id,
            attempt_id=self.cfg.attempt_id,
            workflow_id=workflow_id,
            dispatch=self._dispatch_block(),
            store=self.store,
            fetch_upstream=lambda upstream: inputs_mod.fetch_upstream_task(
                self.db,
                upstream_task_id=upstream,
                tenant_id=self.cfg.tenant_id,
                call_options=self.control.call_options(),
            ),
            verify_upstream=specverify.upstream_verifier(self.cfg, workflow_id),
            read_app_key=self._read_action_app_key,
            environ=self.action_environ,
            recheck=self._action_recheck,
            reap=self.reap_before_publish,
            unprotected=self._git_token_refusal(),
            scrub=lambda text: str(self._scrub(text)),
            log=self.log,
            branch_prefix=self.cfg.git_branch_prefix,
            staged={item.filename: ws.work / item.path for item in staged},
            transport=self.forge_transport,
            sleep=self.action_sleep,
            # B13r builds `merge_human_gate` (owner decision): its value reaches
            # the merge here, and True ends the step SUCCEEDED awaiting a
            # person, every check passed and nothing merged. Until B13r it is
            # off, which is the merge as merge-step.md §5 designs it.
            human_gate=False,
            max_in_worker_retry_delay_seconds=self.cfg.max_in_worker_retry_delay_seconds,
            forge_read_attempts=self.cfg.forge_read_attempts,
            remaining_seconds=self._remaining_seconds,
            register_secret=self.log.register_secret,
            # The merge's credential since contract request 47: the tenant's
            # own `-git` token, read only when `run_merge` asks, after the
            # reap and every check that needs none (archived pull request 219).
            read_git_token=self._read_action_git_token,
            # The VERIFIED spec's own repository: the one the merge acts on.
            repository_url=(
                task.get("repository_url")
                if isinstance(task.get("repository_url"), str) else None
            ),
            # The repository's merge slot (merge race, archived issue 295): every slot
            # write is fenced on this attempt's task and lease, as the park is.
            # A tenant switched to the state routes (archived issue 1457) holds no Firestore
            # client: its `db` is the attempt's state client, and swarm-api
            # fences each slot write on the same tuple.
            merge_slots=(
                (mergeslot_mod.RemoteMergeSlots(self.db, tenant_id=self.cfg.tenant_id)
                 if getattr(self.control, "state_via_api", False) else
                 mergeslot_mod.FirestoreMergeSlots(
                    self.db,
                    tenant_id=self.cfg.tenant_id,
                    run_transaction=self.control._run_transaction,
                    fence=lambda txn: self.control._fenced_task(txn, write="merge slot"),
                    call_options=self.control.call_options,
                ))
                if action is WorkerAction.MERGE else None
            ),
            # The repository's merge train (TRAIN-WS): this step's entry,
            # written only in train mode, fenced on this attempt as the slot is.
            merge_trains=(
                (mergetrain_mod.RemoteTrainEntries(self.db, tenant_id=self.cfg.tenant_id,
                                                   task_id=self.cfg.task_id)
                 if getattr(self.control, "state_via_api", False) else
                 mergetrain_mod.FirestoreTrainEntries(
                    self.db,
                    tenant_id=self.cfg.tenant_id,
                    run_transaction=self.control._run_transaction,
                    fence=lambda txn: self.control._fenced_task(txn, write="merge train entry"),
                    call_options=self.control.call_options,
                ))
                if action is WorkerAction.MERGE else None
            ),
            # The PR-side CI fixer's tasks for the merged pull request (archived issue 1073):
            # this tenant's only, each held to its signed spec by the merge.
            find_ci_fix_tasks=(
                (lambda number, root: inputs_mod.find_ci_fix_tasks(
                    self.db, tenant_id=self.cfg.tenant_id, pull_request=number,
                    root_task_id=root, limit=merge_mod.CI_FIXER_TASKS_READ,
                    call_options=self.control.call_options(),
                ))
                if action is WorkerAction.MERGE else None
            ),
        )
        self.phases.enter("worker_action")
        self.log.info("worker action: no runner is started", action=action.value)
        run = merge_mod.run_merge if action is WorkerAction.MERGE else (
            post_verdict_mod.run_post_verdict
        )
        try:
            # The heartbeat thread is joined before the outage exit below
            # writes this attempt's document: the `with` ends first.
            with self._heartbeat_meanwhile(f"worker_action:{action.value}"):
                outcome = run(ctx)
        except WorkerError:
            # Fenced, tenant mismatch, a missing credential and the other
            # deliberate exits keep their own handlers in `run`, even with an
            # outage further down their chain.
            raise
        except Exception as exc:
            # An outage the action did not answer for itself (archived issue 2000). Anything
            # else still reaches `run`'s crash handler and fails the task.
            unavailable = _unavailable_cause(exc)
            if unavailable is None:
                raise
            return Outcome(exit_code=self._exit_action_unavailable(action, exc, unavailable))
        return self._end_worker_action(action, outcome)

    def _exit_action_unavailable(
        self, action: WorkerAction, exc: Exception, cause: BaseException
    ) -> int:
        """A dependency was unavailable inside a worker action: leave the task, exit 69.

        archived issue 2000. The action runs as the callable `_prepare` returns, after the
        startup window, so `_execute`'s classification never saw its errors:
        a Firestore ABORTED that outlasted `firestore.transactional`'s own
        retries in the merge slot's fenced transaction (the heartbeat thread
        writes the same task and lease documents meanwhile) reached `run`'s
        crash handler, and the task was FAILED for good as runner_error with
        9 of its 10 attempts unused. An outage is the one thing here the next
        attempt may not meet, so it gets the startup window's exit
        (`_exit_unavailable_before_runner`): 69, which the reconciler reads as
        "requeue", or "fail" once the attempts are spent.

        WHY A REPEAT IS SAFE. A worker action is repeatable: the pinned head
        makes a repeated merge either a no-op or a refusal (merge-step.md §6
        row 39), and the merge slot is held by the TASK, renewed by every
        attempt of its holder (mergeslot.py), so a requeued attempt re-enters
        its own slot. No checkpoint is written: an action has no workspace
        work to keep.

        WHAT IS WRITTEN: only this attempt's own document, best effort. Not
        the task, the lease or an event -- a requeue made from here would be a
        fenced transition, the retry cap and a lease release against the
        service that just failed, and the reconciler already does all three.

        `raised_in` names the action's frames the error came up through
        (`slot.renew` or `slot.acquire`, a train entry, a control-plane read), so
        which write aborted is in the log without a traceback reader.
        """
        phase = self.phases.current
        self.log.error(
            f"{type(cause).__name__} inside the worker action; exiting 69 without "
            "writing the task, the lease or an event",
            action=action.value,
            phase=phase,
            seconds_in_phase=self.phases.seconds_in_phase(),
            error_type=type(exc).__name__,
            error=self._scrub(_one_line(exc)),
            unavailable=self._scrub(f"{type(cause).__name__}: {_one_line(cause, 300)}"),
            raised_in=_action_frames(exc),
            exit_code=ExitCode.UNAVAILABLE,
            then=(
                "the reconciler reads this execution's exit code and requeues the "
                "task, or fails it when its attempts are spent"
            ),
        )
        if not self._writes_forbidden:
            try:
                self.control.record_attempt_end(
                    exit_code=ExitCode.UNAVAILABLE,
                    error=self._scrub(
                        f"{type(cause).__name__} inside the {action.value} worker action "
                        f"(phase {phase}): {_one_line(cause, 300)}; the attempt exited "
                        "for a requeue"
                    ),
                )
            except Exception as write_exc:  # noqa: BLE001
                self.log.warning(
                    "could not record the outage on this attempt's own document",
                    error=f"{type(write_exc).__name__}: {write_exc}",
                )
        return ExitCode.UNAVAILABLE

    def _read_action_app_key(self) -> forge_mod.AppKey:
        """This action's App key, from `swarm-tenant-<tenant>-<provider>`.

        `provider` is the profile's (`git-review`, post-verdict's; the merge
        reads the tenant's `-git` token instead since contract request 47,
        `_read_action_git_token`), read by this Job's own service account, the
        secret's sole accessor. Registered with
        the logger's redaction at once, as `resolve_git_token` does a token.
        A tenant that has not registered the provider raises
        `CredentialMissing`, which parks the task at no cost.
        """
        provider = self.cfg.profile.provider or ""
        tenant = load_tenant(self.db, self.cfg.tenant_id, call_options=self.control.call_options())
        if provider not in tenant.credentials or self.secret_client is None:
            raise CredentialMissing(self.cfg.tenant_id, provider)
        payload = self.secret_client.access(tenant.secret_name(provider))
        self.log.register_secret(payload.strip())
        key = forge_mod.parse_app_key(payload)
        self.log.register_secret(key.private_key)
        del payload
        self.log.info("worker action credential read", secret=tenant.secret_name(provider))
        return key

    def _read_action_git_token(self) -> str:
        """The task's forge token, for the merge (contract request 47).

        The secret `swarm-tenant-<tenant>-<suffix>`, the suffix being the
        signed `forge_credential` (`_forge_suffix`, `git` when it names none,
        contract request 54), read by the Job's account -- the tenant's worker
        account, the secret's ordinary reader -- through `resolve_git_token`,
        which registers the value with the logger's redaction before returning
        it and logs the secret's NAME only. Held in this process's heap and
        nowhere else: never written to the workspace, a file, an environment
        or an event. A tenant that has not registered its `-git` token raises
        `CredentialMissing`, which parks the step at no cost.

        A merge is a forge write, so a read grant, or a grant gone since
        submission, raises `ForgeWriteRefused` before any token is read; the
        merge ends the step FAILED and names that type.

        A person's `git-u-` slot is asked of swarm-api's broker, not read
        (archived issue 1041): its refusal raises a named `SecretError`, or
        `ForgeWriteRefused` for a grant gone, and the merge fails on it.
        """
        forge_refused = self._forge_write_refusal()
        if forge_refused is not None:
            raise ForgeWriteRefused(*forge_refused)
        suffix = self._forge_suffix()
        tenant = load_tenant(self.db, self.cfg.tenant_id, call_options=self.control.call_options())
        person = user_slot_hash(suffix) is not None
        # A service submission's run asks the broker first for a token minted
        # for it (DAYONE-SERVICE-APP-TOKEN), so it needs no slot.
        app_installation = self._service_run()
        if not app_installation and ((self.secret_client is None and not person) or (
            suffix == GIT_PROVIDER and GIT_PROVIDER not in tenant.credentials
        )):
            raise CredentialMissing(self.cfg.tenant_id, suffix)
        # A person's slot comes from the broker, asked for a write (archived issue 1041): a
        # refusal is `ForgeCredentialRefused`, or `ForgeWriteRefused` for a
        # grant gone, and never the tenant token in its place.
        token = resolve_git_token(
            tenant=tenant, client=self.secret_client, logger=self.log, provider=suffix,
            broker=self.forge_broker, write=True, app_installation=app_installation,
        )
        if not token:
            raise CredentialMissing(self.cfg.tenant_id, suffix)
        return token

    def _action_recheck(self) -> bool:
        """Fencing (raising `FencedError`), then whether a cancel was requested."""
        signals = self.control.validate_generation()
        return bool(getattr(signals, "cancel_requested", False))

    def _end_worker_action(
        self, action: WorkerAction, outcome: "post_verdict_mod.ActionOutcome"
    ) -> Outcome:
        """End the task the way the action's outcome says (merge-step.md §6, §6a)."""
        if outcome.credential_missing is not None:
            return self._park_credential_missing(outcome.credential_missing)
        if outcome.ci_wait is not None:
            return self._park_ci_pending(action, outcome.ci_wait)
        if outcome.owner_wait is not None:
            return self._park_for_owner_questions(action, outcome)
        with self._metrics_after_the_record():
            return self._record_worker_action_end(action, outcome)

    def _record_worker_action_end(
        self, action: WorkerAction, outcome: "post_verdict_mod.ActionOutcome"
    ) -> Outcome:
        """`_end_worker_action`'s uploads and terminal write, before any metrics export."""
        summary = self._upload_outputs()
        summary["merge" if action is WorkerAction.MERGE else "verdict"] = self._scrub(
            dict(outcome.summary)
        )
        if outcome.spec_check is not None:
            summary["spec_check"] = dict(outcome.spec_check)
        error = str(self._scrub(outcome.message[:4000])) if outcome.message else None
        refusal = outcome.summary.get("refusal") if isinstance(outcome.summary, dict) else None
        code = refusal.get("code") if isinstance(refusal, dict) else None
        if outcome.retryable:
            state = self.control.fail_retryably(
                exit_code=outcome.exit_code,
                error=error or "the worker action failed",
                cause=str(code or action.value),
                result_summary=summary,
                retry_delay_seconds=outcome.retry_delay_seconds,
                detail={"worker_action": action.value},
                end_cause=outcome.end_cause,
            )
            self.log.warning("worker action failed retryably", action=action.value, code=code)
            if state is TaskState.FAILED:
                # The last attempt: the task ended as `end_cause` (an outage
                # that spent its retries is how most MERGE_FAILED /
                # VERDICT_FAILED end; CANCELLED ends as the cancel's cause and
                # is not logged here). The same line as the non-retryable end
                # below, which the monitoring module's worker-action-ended
                # metric counts.
                self.log.error("worker action ended", action=action.value, code=code,
                               end_cause=outcome.end_cause.value if outcome.end_cause else None)
            return Outcome(exit_code=ExitCode.FAILED, state=state)
        self.control.finish(
            state=outcome.state,
            exit_code=outcome.exit_code,
            error=None if outcome.state is TaskState.SUCCEEDED else error,
            result_summary=summary,
            end_cause=outcome.end_cause,
        )
        if outcome.state is TaskState.SUCCEEDED:
            self.log.info("worker action succeeded", action=action.value)
        else:
            self.log.error("worker action ended", action=action.value, code=code,
                           end_cause=outcome.end_cause.value if outcome.end_cause else None)
        return Outcome(exit_code=outcome.exit_code, state=outcome.state)

    def _verify_spec(self, task: dict[str, Any], create_time: Any) -> None:
        """Contract request 34's check, with its log line and its legacy note.

        The spec itself is never logged: a prompt can hold anything. The
        check is kept as `_spec_check`: its `model_class` is the signed class
        the model check holds MODEL to.
        """
        clock = self.cfg.spec_clock
        now = clock() if clock is not None else datetime.now(timezone.utc)
        try:
            check = specverify.verify_step_spec(
                task, create_time=create_time, cfg=self.cfg, now=now, log=self.log
            )
        except SpecSignatureInvalid as exc:
            self.log.error(
                "spec signature invalid: refusing to run this task",
                end_cause=EndCause.SPEC_SIGNATURE_INVALID.value,
                spec_check=exc.spec_check(),
            )
            raise
        if check.reason == "legacy_unsigned":
            self.log.warning(
                "unsigned task admitted by the legacy window (SPEC_SIGNATURE_MODE=legacy)",
                spec_check=check.as_detail(),
            )
            # On the task's own stream, so the unsigned run is visible where
            # its operator looks, not only in the log. RUNNING, because there
            # is no note-only event type in the frozen contract and the task
            # is RUNNING by now; `phase` says which step wrote it.
            self.control.emit(
                EventType.RUNNING,
                detail={"phase": "verify_spec", "spec_check": check.as_detail()},
            )
        else:
            self.log.info("spec signature verified", spec_check=check.as_detail())
            self._forge_signed = True
        self._spec_check = check

    def _fail_model_check(self, refusal: specverify.ModelRefusal) -> Outcome:
        """FAILED, not retried, before the agent ran: MODEL cannot run this step.

        docs/design/model-per-step.md §4.5. NOT RETRIED: the next attempt is
        dispatched from the same signed class by the same scheduler, and
        meets the same MODEL. CANNOT_START, like every refusal before the
        agent. The class and the ID are logged and recorded: neither is a
        secret, and an operator needs both to see which side is wrong.
        """
        error = self._scrub(
            f"{refusal.cause}: {refusal.reason}. The agent was not started."
        )
        self.log.error(
            "the execution's MODEL cannot run this step's model class; failing "
            "before the agent runs",
            cause=refusal.cause,
            model_class=refusal.model_class,
            job_model=refusal.model,
        )
        summary = self._upload_outputs()
        summary["model_check"] = refusal.as_detail()
        with self._metrics_after_the_record():
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _prepare(self) -> dict[str, str] | Callable[[], Outcome]:
        """Steps 2 to 6, the fence re-check and the quota preflight.

        Returns the runner's environment, or a park to make once the startup
        window has closed (see `_execute`).
        """
        cfg = self.cfg

        # ---- STEP 2: STARTING -> RUNNING --------------------------------
        self.phases.enter("record_attempt_start")
        # `model` is the ID this attempt was started with (contract request
        # 103), written with `started_at`. STEP 1's generation check has
        # passed, so a stale generation never reaches this write.
        self.control.record_attempt_start(
            backend=cfg.backend, execution_name=_execution_name(), model=cfg.model
        )
        self.phases.enter("advance_to_running")
        # STEP 2a, inside STEP 2: the child-task attempt key is registered
        # while the task is STARTING, before the agent exists (child tasks,
        # §3.2 step 3). swarm-api refuses a registration once it is RUNNING.
        # ONE STARTING event: the walk to STARTING writes none, and the
        # registration's note (no child path, and why) is its detail.
        notes: list[dict[str, Any]] = []
        if getattr(self.control, "state_via_api", False):
            # THE TENANT IS SWITCHED TO THE STATE ROUTES (archived issue 1457, §4.3). The
            # key was generated and registered by `state/open`, the first call
            # this attempt made (STEP 1), so the child path takes that key
            # rather than spending the nonce again. swarm-api writes each
            # step's event with its transition, and the ONE STARTING event
            # carries the registration's note, as below.
            self.children.adopt(
                self.control.attempt_key, self.control.registration, notes.append
            )
            self.control.advance_to_running(starting_detail=notes[-1] if notes else None)
        else:
            if self.cfg.child_nonce:
                moved = self.control.advance_to_starting()
                self.children.register(notes.append)
                if moved or notes:
                    self.control.emit(EventType.STARTING, notes[-1] if notes else None)
            self.control.advance_to_running()
        # STEP 2b: the live path (live-browser §3.3, §5.4.2), once RUNNING --
        # the live-session route checks the fence on RUNNING -- and long before
        # the agent exists, so the gate's one registration is this worker's.
        # Never raises; an attempt without a live path runs without one -- but
        # never SILENTLY: a live profile whose path did not open says so on the
        # task's events, where the console shows it beside an empty Browser
        # tab (task_23cc2e82178640709a45, 2026-10-12, LB-KEYRACE).
        self.handoff.setup(self._live_browser_unavailable)
        # NOLIVE-GUARD (part of archived issue 1366): the agent's browser follows the GATE,
        # never the viewer's tunnel. A live-browser attempt whose gate is not
        # ready has no CDP proxy, and it never starts its agent headless: the
        # ATTEMPT fails retryably, before the agent, after the startup window.
        # BROWSER-START: likewise a gate whose CDP endpoint was not passed or
        # never answered within `setup`'s bounded wait (`browser_missing`).
        if self.handoff.gate_missing:
            return functools.partial(self._fail_live_gate_missing, self.handoff.browser_missing)

        # THE FIRST HEARTBEAT GOES HERE, BEFORE ANY SLOW WORK.
        #
        # It used to happen only once the agent child was running -- after the
        # workspace, the checkpoint restore and the clone. But the reconciler
        # measures silence from LEASE ACQUISITION, so everything before this
        # point counted against `heartbeat_grace_seconds` (90): container cold
        # start, image pull, workspace setup, restoring a checkpoint and a
        # shallow clone.
        #
        # On 2026-09-19 that reclaimed a task three times in a row. Each
        # replacement worker started, found its generation superseded, logged
        # "FENCED: this attempt has been superseded; exiting without running the
        # agent" and exited 70 -- invariant 5 doing exactly what it exists to
        # do, on workers that were never unhealthy. The task never ran at all.
        #
        # Raising the grace was the other option and is worse: it delays every
        # genuine reclaim to accommodate startup cost. Heartbeating here makes
        # the grace measure LIVENESS, which is what it is for.
        self._heartbeat()

        # ---- STEP 3: isolated workspace ---------------------------------
        self.phases.enter("workspace")
        self.ws = workspace_mod.create(
            cfg.workspace_root, cfg.attempt_id, cache_root=cfg.cache_root
        )
        ws = self.ws
        self.log.info("workspace created", path=str(ws.root), cache=str(ws.cache))
        # AU8-SEED: the image's warmed npm cache, copied into the attempt's
        # own before the agent starts. Never raises: a failed copy is logged
        # and leaves the empty cache npm had before.
        if ws.cache is not None:
            cachedirs_mod.seed_npm_cache(ws.cache, self.log)

        # ---- STEP 4: fetch the task and VERIFY ITS SPEC -----------------
        # Contract request 34 (archived issue 342). Immediately after the fetch and before
        # anything reads the document for what to do: the checkpoint restore,
        # the clone (and its git token), the staged inputs, the child's
        # credentials and the issue. A phase of its own, so the phase log
        # proves the order. A refusal raises `SpecSignatureInvalid`, which
        # `_execute` ends FAILED with SPEC_SIGNATURE_INVALID, fenced.
        self.phases.enter("verify_spec")
        task, create_time = self.control.fetch_task_snapshot()
        self._verify_spec(task, create_time)
        # `max_attempts` is covered: a retryable failure decides on this
        # verified copy, never on the live field a tenant agent can rewrite
        # (archived issue 346).
        self.control.pin_verified_max_attempts(task.get("max_attempts"))

        # ---- STEP 4 (cont.): the MODEL this execution was given ----------
        # docs/design/model-per-step.md §4.5. After the signature, on its
        # signed class, and before a credential is read or the CLI started:
        # a classed step with no pin, or with the pin of another class, is
        # refused rather than run on whatever model the CLI would pick.
        _, model_refusal = specverify.check_model(cfg.profile, self._spec_check, cfg.model)
        if model_refusal is not None:
            return functools.partial(self._fail_model_check, model_refusal)
        # The clone host joins the egress probe (archived issue 721 (a)) as soon as the
        # document naming it is verified, the same URL `_maybe_clone` reads.
        self._add_egress_target(self.cfg.repository_url or task.get("repository_url"))

        # ---- STEP 4a: every signed parent has SUCCEEDED -------------------
        # Contract request 34, decision 7. A tenant agent can write a parked
        # step's `state` to READY without touching its spec, and the scheduler
        # will admit it: the signature still verifies. So, on the SAME verified
        # `task` -- never a re-fetch -- each parent named by its signed
        # `depends_on` is read through the tenant gate, and one that has not
        # SUCCEEDED parks this task as DEPENDENCY_INCOMPLETE before a
        # checkpoint is restored, a repository cloned, an input staged or a
        # credential read. The scheduler's dependency sweep returns it to
        # READY, or cancels it on a failed parent. Defence in depth: it makes
        # the early-release rewrite forge every parent's state as well; it
        # does not close it. Inside the `verify_spec` phase, not one of its
        # own: it is the second half of the same check on the same document.
        waiting_on = self._incomplete_parents(task)
        if waiting_on:
            return functools.partial(self._park_dependency_incomplete, waiting_on)

        # ---- STEP 4a'': an upstream this step needs changed nothing ---------
        # Owner decision, 2026-10-05. A step allowed an empty diff
        # (`allow_empty_diff`) and had nothing to change; this step needed
        # that change -- its patch, its branch, or every branch it integrates
        # -- or stages from a step that was itself skipped. It ends SUCCEEDED
        # with `result_summary.skipped` here, on the same verified document,
        # before a checkpoint is restored, a repository cloned (the branch it
        # would start from was never pushed), an input staged (the patch was
        # never written) or a credential read. After the window closes, like
        # every other ending `_prepare` decides on.
        self._task = task
        nothing = self._nothing_to_work_on(task)
        if nothing:
            return functools.partial(self._finish_nothing_to_change, nothing)

        # ---- STEP 4a': a WORKER ACTION starts no runner (contract request 33)
        # `merge` and `post-verdict` name a `WorkerAction` in the frozen
        # catalogue and have no runner_argv: the worker performs the action
        # itself, so no agent ever runs under the Job identity that holds the
        # App key (docs/merge-step.md §0, §1.3). Nothing an agent step needs is
        # done: no checkpoint is restored, nothing is cloned, no runner input
        # is written and no runner credential is read. The declared inputs
        # are staged (the merge's proof.json), inside the startup window like
        # every other step's. The action itself runs after the window closes,
        # because it talks to the forge (`_run_worker_action`).
        action = cfg.profile.worker_action
        if action is not None:
            _read_input_as_checked(task, cfg.runner_profile)
            self._task = task
            self.phases.enter("stage_inputs")
            staged_inputs = self._stage_declared_inputs(task)
            return functools.partial(self._run_worker_action, action, task, staged_inputs)

        # ---- STEP 4b: restore the latest checkpoint ---------------------
        self.phases.enter("restore_checkpoint")
        # THE STORED INPUT IS CHECKED AGAIN, AT THE FIRST READ OF THE TASK
        # (contract request 32, *Preconditions*; the owner's decision on archived pull request 345:
        # every profile, no exemption). swarm-api and the bridge refuse an
        # input its profile does not declare at submission; this asks the same
        # rule of the input AS STORED -- `task["input"]`, before the worker adds
        # `task_id`, `attempt_id`, `repository`, `attempt_count`,
        # `resumed_from_checkpoint` or `staged_inputs` of its own -- so a task
        # written before its profile declared, or by a path that skipped the
        # API, is refused by the process that would run it. Here, and not where
        # `input.json` is written: before a checkpoint is restored, a
        # repository cloned, an upstream artifact staged or a credential
        # mounted for an input that was always going to be refused.
        #
        # Runs AFTER `verify_spec` (STEP 4, contract request 34) and on the
        # SAME `task` object that step verified -- never a fresh re-fetch.
        # A spec that fails `verify_spec` raises `SpecSignatureInvalid` and
        # ends the attempt before this line is reached, so an unverified
        # snapshot never reaches `_recheck_runner_input`. The owner's
        # decision on archived pull request 353/#345 ordering: verify the signature first, then
        # re-check the input, both on the one verified document.
        #
        # And from here on the worker reads the input AS CHECKED (contract
        # request 58): an `integer` input stored as `7.0` -- which JCS gives
        # the same bytes as the `7` that was signed -- is read as `7`, so the
        # issue the title names is the issue the signer signed.
        _read_input_as_checked(task, cfg.runner_profile)
        # Kept because the publish gate, several steps later, needs the
        # caller's dispatch strategy and re-fetching it there would be a
        # second read of a document that cannot have changed. It is the
        # VERIFIED document: nothing later reads a covered field from Firestore.
        self._task = task

        # ---- STEP 4b: the QA identity's first credential (QI-WORKER) -------
        # docs/design/qa-identity.md §4.5. From the VERIFIED task, once the
        # gate is registered (STEP 2b) and before the agent exists; a task
        # whose input names no identity asks nothing. A first refusal as not
        # granted or unconfigured ends the attempt here: nothing the agent
        # could do would be signed in. Any other failure runs it signed out
        # and told why (`handoff.QA_IDENTITY_NOTICE_NAME`).
        qa_refused = self.handoff.start_qa_identity(task)
        if qa_refused is not None:
            return functools.partial(self._end_qa_identity_refused_before_start, qa_refused)

        # ---- STEP 4b': the submitter's grant still stands (OB5) -----------
        # docs/onboarding.md §3.3 step 4. A task signed for a person's user
        # slot re-reads that person's grant on the repository BEFORE a
        # checkpoint is restored, the issue prefetch reads the token, or the
        # repository is cloned: a grant deleted since submission ends the
        # attempt here, before the agent starts, and the end releases the lease
        # and touches it no further. After `verify_spec`, so the slot it reads
        # by is the signed one. A tenant or repository token reads nothing.
        if user_slot_hash(self._forge_suffix()) is not None:
            self.phases.enter("forge_grant")
            revoked = self._forge_grant_refusal(write=False)
            if revoked is not None:
                return functools.partial(self._end_forge_refused_before_start, *revoked)
        self._standalone = self._uploads_working_folder(task)
        self._restore_checkpoint(task)
        if self._standalone and self._restored_from is not None:
            # What the checkpoint brought back, BEFORE the worker stages
            # anything: whatever in here is not a staged input was written by
            # an earlier attempt's agent, and counts as created (see
            # `agent_worker.standalone_outputs`, "A resumed attempt").
            self._restored_workdir = frozenset(
                standalone_mod.scan(ws.work, reserved=self._workdir_reserved()).files
            )
        # Restoring a large checkpoint is unbounded; prove liveness after it.
        self._heartbeat()

        # ---- STEP 5: optional shallow clone -----------------------------
        # The issue fetch starts first, in a thread, and runs beside the
        # clone (archived issue 721, P29): it needs only the token and the issue number.
        self._start_issue_prefetch(task)
        self.phases.enter("clone")
        try:
            repo_info = self._clone_keeping_lease(task)
        except _CloneUnreachable as exc:
            # The forge did not answer through every in-process try (archived issue 623):
            # the ATTEMPT ends retryably, after the startup window closes.
            return functools.partial(self._fail_clone_unreachable, exc.reason, exc.tries)
        # A clone is the single slowest step before the agent starts, and the
        # one most likely to vary with repository size.
        self._heartbeat()
        # ---- STEP 5 (after): a superseded CI fix runs no agent (archived issue 2276) ------
        # A CI fix waits for capacity after `scripts/ci-fix.sh` checked its red
        # head, and the branch can be fixed, merged or closed meanwhile. On a
        # fresh clone, before any runner starts, it ends SUCCEEDED no_change
        # (`cifixstart.superseded`), after the window closes like every other
        # ending `_prepare` decides on.
        superseded = self._ci_fix_superseded(repo_info)
        if superseded:
            return functools.partial(self._finish_superseded_ci_fix, superseded)
        # The services the clone declares, up from here to `_cleanup` (TS-RUN).
        self._start_test_services()

        # ---- STEP 5': the additional repositories (MULTI-REPO, archived issue 1434) ------
        # docs/design/multi-repository-steps.md §4.4: right after the primary
        # clone, each signed entry cloned at its signed commit onto the cache
        # volume, read-only, and linked under `work/repos`. All of them or the
        # agent does not start: a revoked grant or an oversized set ends the
        # attempt for good, a clone that failed ends it retryably. A task
        # with no primary repository takes them too (the owner's Q8).
        try:
            self._extra_checkouts = self._clone_additional_keeping_lease(task)
        except extrarepos_mod.Refused as exc:
            return functools.partial(self._end_additional_refused, exc)
        if self._extra_checkouts:
            self._heartbeat()

        # ---- before STEP 5a: a READ grant on a step that must push (D9, OB5) -
        # A step whose job is to open a pull request, or to carry its work on a
        # branch, could only ever be refused at its push, so it is refused now,
        # before the agent spends time and provider quota on work that can
        # never be delivered, and before `carrier_scope` reads a token to ask
        # the forge a question whose answer would not matter. A step that
        # pushes only incidentally (a contributor's branch) is refused at that
        # push instead (`_forge_write_refusal`).
        if forge_read_only(task) and (
            self._opens_pull_request() or self._dispatch_carrier() == "branches"
        ):
            return functools.partial(
                self._end_forge_refused_before_start,
                FORGE_READ_GRANT,
                "this task's grant on its repository is read-only, and this step "
                "pushes or opens a pull request",
            )
        # ---- STEP 5a: carrier: branches needs a token that can push (D13) --
        # Asked HERE, by the worker, and not by swarm-api at submission: the
        # answer needs the tenant's git secret, and exactly one identity may
        # read each secret -- this tenant's worker GSA (owner decision
        # 2026-10-02; terraform/modules/secret_manager). Before the agent
        # runs, so a token that could never push costs no agent time and no
        # provider quota. Only for `branches`: the phase is not entered at all
        # otherwise, so `checkpoints` reads no extra secret and asks no forge.
        if self._dispatch_carrier() == "branches":
            self.phases.enter("carrier_scope")
            refused = self._carrier_scope_refusal()
            if refused is not None:
                return refused

        # ---- STEP 5b: stage the artifacts this step declared -------------
        # Order relative to the clone is not load-bearing: the two write to
        # different paths inside `work/`, and a declared name that would collide
        # with the clone directory is refused rather than resolved. Order
        # relative to the RUNNER INPUT is: `input.json` has to be able to tell
        # the agent what it was given, so staging happens first. A staged file
        # lands in `work/`, never in the checkout, even though the agent of a
        # repository task starts in the checkout (archived issue 226): the prompt names each
        # one by absolute path instead (`expected_outputs.staged_paths`).
        self.phases.enter("stage_inputs")
        staged_inputs = self._stage_declared_inputs(task)
        if staged_inputs:
            # Downloading an upstream artifact is unbounded in the same way a
            # clone is; prove liveness after it for the same reason.
            self._heartbeat()
        # A resumed parent's children, staged before its agent starts again
        # (child tasks, §3.3 step 7). Nothing for a task that has none.
        self.children.clear_await(ws.work)
        if self.children.stage_results(
            ws.work, store=self.store, max_total_bytes=self.cfg.max_artifact_bytes
        ) is not None:
            self._heartbeat()

        # ---- STEP 5c: ./artifacts is the artifacts directory ----------------
        # After the restore, the clone and the staging, never before: a
        # restore refuses a non-empty `work/`, and a staged input or a
        # restored directory that already owns the name keeps it.
        self._link_artifacts()
        # And in the checkout, where a repository task's agent starts (archived issue 226).
        self._link_checkout_artifacts()

        # ---- STEP 5d: the verdict gate (archived issue 264) -------------------------------
        # After staging, which put the verdict file on disk. A verdict that
        # does not name this step skips the AGENT, not the step: the step
        # still finishes and publishes, because it is the one that opens the
        # pull request. The generation is checked first, as it is before an
        # agent starts, because that publish pushes.
        if self._evaluate_verdict_gate(staged_inputs) is False:
            self.phases.enter("revalidate_generation")
            self.control.validate_generation()
            return self._finish_without_agent

        # ---- STEP 5e: a retry whose push the forge never answered (archived issue 2833) ---
        # The previous attempt's agent finished and its push met a forge
        # outage; the checkpoint restored above holds that work unchanged.
        # The step publishes it without running the agent again. The
        # generation is checked first, as for the gate: the publish pushes.
        republish = self._push_to_republish(task)
        if republish is not None:
            self.phases.enter("revalidate_generation")
            self.control.validate_generation()
            return functools.partial(self._republish_without_agent, republish, task)

        # ---- runner input -----------------------------------------------
        payload = dict(task.get("input") or {})
        if repo_info:
            payload.setdefault("repository", repo_info)
        if self._extra_checkouts:
            # Assigned, not `setdefault`: what is on disk now, as
            # `staged_inputs` below is.
            payload[extrarepos_mod.INPUT_KEY] = [c.as_dict() for c in self._extra_checkouts]
        elif extrarepos_mod.INPUT_KEY in payload:
            # Only the worker's own record names checkouts to the agent.
            payload.pop(extrarepos_mod.INPUT_KEY)
            self.log.warning(
                "dropped the caller's own input.additional_repositories: only the "
                "checkouts the worker made for this task are named to the agent",
            )
        if staged_inputs:
            # Assigned, not `setdefault`: this key describes what is actually on
            # disk right now, so a caller's own `staged_inputs` in the step input
            # must not shadow it and leave the agent reading a stale claim.
            payload["staged_inputs"] = [item.as_dict() for item in staged_inputs]
        elif "staged_inputs" in payload:
            # AND REMOVED WHEN NOTHING WAS STAGED (archived issue 226). A CLI runner now turns
            # this key into a line of the prompt, in the platform's voice, naming
            # files "earlier steps of this workflow gave you". Only the worker's
            # own record may fill it, exactly as for `expected_outputs` below.
            dropped = payload.pop("staged_inputs")
            self.log.warning(
                "dropped the caller's own input.staged_inputs: only the files the "
                "worker staged for this task are named to the agent",
                dropped_entries=len(dropped) if isinstance(dropped, (list, tuple)) else 1,
            )
        if "model" in payload:
            # A CALLER NEVER CHOOSES THE MODEL (archived issue 226, invariant 10). The API
            # refuses `input.model` on every profile that declares its inputs
            # (archived pull request 213); this is the same rule for a task written before that
            # refusal shipped (every profile declares since contract request
            # 32, archived issue 218, so the API now refuses it for all of them), or a path
            # that does not go through the API. The model is the Job's `MODEL`, which reaches the runner
            # in its environment (`_build_child_env`), never through input.json.
            payload.pop("model")
            self.log.warning(
                "dropped the caller's own input.model: the model an agent runs is "
                "the runner profile's, set on its Job as MODEL",
                job_model=cfg.model,
            )
        expected = self._declared_outputs(task)
        if expected_mod.METADATA_KEY in payload:
            # A CALLER'S OWN `input.expected_outputs` NEVER REACHES THE AGENT,
            # whether or not the API recorded names of its own. A CLI runner
            # turns this key into instructions in the platform's voice ("later
            # steps of this workflow need these files"), so only the names the
            # API wrote into `task.metadata` may fill it -- and the API refuses
            # that key from callers. Dropped with a line, not silently.
            dropped = payload.pop(expected_mod.METADATA_KEY)
            self.log.warning(
                "dropped the caller's own input.expected_outputs: only the names "
                "the API recorded for this task's dependants reach the agent",
                dropped_entries=len(dropped) if isinstance(dropped, (list, tuple)) else 1,
            )
        told = expected_mod.without_platform_names(expected, (PATCH_NAME,))
        if told:
            # The platform's statement of what later steps need. A CLI runner
            # turns it into the agent's instructions; the other runners ignore
            # it. `swarm-work.patch` is left out: it is the platform's record
            # of the agent's repository changes, which the harvest writes after
            # the agent exits, over whatever is there, whenever the diff is
            # non-empty and under the cap (`gitops.summarize_work`). An agent
            # told to write it would have its file replaced, or -- with an empty
            # or oversized diff -- uploaded under a name every reader takes for
            # the platform's diff. It stays in `self._expected_outputs`, which
            # the end-of-attempt check reads, because a dependant that stages
            # it still needs it uploaded.
            payload[expected_mod.METADATA_KEY] = list(told)
        # `cfg.model` is NOT written here any more (archived issue 226). It was, with
        # `setdefault`, so a caller's `input.model` won over the Job's MODEL.
        payload.setdefault("task_id", cfg.task_id)
        payload.setdefault("attempt_id", cfg.attempt_id)
        # This attempt's number: the task's `attempt_count`, which admission
        # increments in the lease's own transaction, so 1 on the first lease
        # and one more on every lease after, a park's next attempt included.
        # A platform record, not the workspace's: it reaches the runner
        # whether or not a checkpoint did, which is what bounds the mock's
        # simulated park (runners/mock.py, the review of archived pull request 213). ASSIGNED, not
        # `setdefault`: a count a caller could set would be a bound a caller
        # could lift. Counted with the refunded resumes (`_refunded_resumes`):
        # a refunded quota park takes one off `attempt_count`, so without them
        # every lease after the mock's park read 1 again and parked again
        # (dev acceptance, archived issue 2650).
        payload["attempt_count"] = int(task.get("attempt_count") or 0) + _refunded_resumes(task)
        payload.setdefault("resumed_from_checkpoint", bool(self._restored_from))
        # The owner's answers to the blocking questions this task parked on
        # (AU-7, §3.9 step 4): `work/answers.json`, and the prompt's last
        # block between nonce lines, as data. Nothing for a task that never
        # waited on one.
        questions_mod.resume_with_answers(ws.work, payload, task)
        # A plugin step parked on its agent's question (the question bridge):
        # the same session resumes, with the answer as its next message.
        self._resume_parked_question(task)
        # A review's owner questions (archived issue 2070, option (c)): every earlier step
        # of its workflow's validated questions, with the answers so far, in
        # `work/owner-questions.json`, and the verdict instructions closing
        # the prompt. `input_from` stages one file per parent, so they never
        # reached a review before. Nothing for any other step, or a workflow
        # that asked nothing. archived issue 3086: an earlier step's questions.json the
        # worker did not take is named in the prompt, with its reason.
        def fetch_upstream(upstream: str) -> dict[str, Any]:
            return inputs_mod.fetch_upstream_task(
                self.db,
                upstream_task_id=upstream,
                tenant_id=cfg.tenant_id,
                call_options=self.control.call_options(),
            )

        owner_questions_mod.stage(
            ws.work, payload,
            owner_questions_mod.collect(
                task, fetch=fetch_upstream, store=self.store, tenant_id=cfg.tenant_id),
            rejected=owner_questions_mod.collect_rejected(task, fetch=fetch_upstream),
        )
        # A step that builds on another (archived issue 2836): the owner's answers to the
        # questions that step asked, in `work/owner-answers.json`, and a
        # prompt section naming the decisions already made, so its agent never
        # asks the owner one again. Nothing for a step without `builds_on`.
        answered = self._builds_on_owner_answers(task)
        inputs_mod.stage_owner_answers(ws.work, payload, answered)
        self._owner_answered = [row["question"] for row in answered]
        # A review of a patch that touches a UI path (archived issue 2184): its MERGE counts
        # only from a rendered review, and its prompt says so before it writes
        # a verdict. `_upload_outputs` holds the verdict to it.
        self._ui_review_paths = self._ui_paths_reviewed(task, staged_inputs)
        if self._ui_review_paths is not None and isinstance(payload.get("prompt"), str):
            payload["prompt"] += verdict_mod.rendered_review_guidance(
                self._ui_review_paths, cfg.runner_profile
            )
        # The step's signed work contract, for the runner's end-of-step
        # judgment (JC2, jev-contract.md section 5): its body in work/ and the
        # SIGNED digest, both from the task document, never from `input`.
        self._work_contract_env = self._stage_work_contract(task, ws.work)
        # The step's plugins, their guidance and the input file follow the
        # release and the TypeSafe shim (STEP 6a+), whose answers a managed
        # plugin needs.

        # ---- STEP 6: tenant credentials ---------------------------------
        self.phases.enter("credentials")
        # STEP 6a: the workspace secrets granted to this attempt's consumers
        # (SEC-RELEASE), ONCE, before the runner exists: every value is
        # registered for redaction as the answer is parsed, and the runner is
        # handed only the manifest's path (`_build_child_env`). A 409 is the
        # fence: stand down without touching the lease (invariant 5). A
        # required slot with nothing in it -- no grant, or swarm-api not
        # reachable -- parks CREDENTIAL_MISSING at no cost (invariant 1);
        # optional slots only, the attempt starts without them. A step whose
        # signed plugin set names a plugin secret or file asks too, with no
        # slot (managed-plugins.md §6.2): those stay in this heap for STEP
        # 6a+ and reach only their plugin's launcher.
        try:
            released = self.secret_release.release(
                profile=cfg.profile, inputs=task.get("input"), private_dir=ws.private,
                plugins=pluginload_mod.wants_release(task),
            )
        except secretrelease_mod.SecretReleaseFenced as exc:
            raise FencedWriteRefused(
                cfg.generation, -1, f"secrets release: {exc.code}", write="secrets release"
            ) from None
        if released.missing_required:
            secretrelease_mod.remove(ws.private)
            return functools.partial(
                self._park_credential_missing,
                CredentialMissing(
                    cfg.tenant_id,
                    "workspace-secret:" + ",".join(released.missing_required),
                ),
            )
        # STEP 6a*: a Cloudflare Access token (CF-ACCESS-TOKEN), held in this
        # heap by the release and never written, goes to the live gate now,
        # before the runner exists; the release's copy is emptied either way.
        # None granted, nothing is sent and the gate intercepts nothing.
        self.handoff.give_cf_access(released.gate)
        # STEP 6a': the TypeSafe forwarder's first ticket and the shim, before
        # the runner and before `_build_child_env` (whose jev key and judge
        # setup read the shim file). Switched off, nothing happens. A 409 is
        # the fence, as for the release: stand down, leave the lease.
        try:
            self.typesafe.start(ws.private)
        except typesafe_shim_mod.TypesafeTicketFenced as exc:
            raise FencedWriteRefused(
                cfg.generation, -1, str(exc), write="typesafe ticket"
            ) from None
        # STEP 6a+: the step's agent plugins (docs/design/agent-plugins.md,
        # "The start of a step"): only those signed with the step, linked from
        # the image's read-only install, ONLY the step's skills, before the
        # agent. An image without the install fails here (`PluginUnavailable`,
        # CANNOT_START), naming the image, before anything is linked. After
        # the release and the shim, because a managed plugin (managed-plugins.md
        # §4.4, §6.2) is handed what they hold: its own released secrets
        # (`released.plugins`, dropped from this heap once the loader has
        # written them 0600 for its launcher alone, never the agent's
        # environment), the workspace's TypeSafe key for a binding
        # (`_plugin_provider_key`), and the baked gates' filter inputs, the
        # ones `qualitygates.prepare` gets.
        try:
            self._plugins = plugins_mod.start(
                task, work=ws.work, checkout=ws.checkout() if self._repo_url else None,
                profile=cfg.runner_profile, image=str(getattr(cfg.profile, "image", "") or "?"),
                released=released.plugins, provider_key=self._plugin_provider_key,
                secret_env_names=tuple(cfg.profile.secrets or ()),
                hard_stop_paths=self._plugin_hard_stop_paths(task),
            )
        finally:
            released.plugins.clear()
        if self._plugins is not None and isinstance(payload.get("prompt"), str):
            payload["prompt"] += self._plugins.guidance
        # The optional security review step (docs/design/security-review-step.md):
        # a step whose signed contract loads security-audit is told the owner's
        # run settings here, and its output is checked after the agent.
        if security_review_mod.applies(self._plugins):
            self._security_review = {}
            if isinstance(payload.get("prompt"), str):
                payload["prompt"] += security_review_mod.guidance(ws.artifacts)
        # The pull request this step works on (a peer application, 2026-11-06):
        # one line of the brief names `work/pull_request.md`, which STEP 6b'
        # writes after the credentials, before the agent starts.
        if self._requested_pull_request() is not None and isinstance(payload.get("prompt"), str):
            payload["prompt"] += "\n\n" + pull_request_mod.prompt_line(
                pull_request_mod.pull_request_path(ws.work)
            )
        ws.input_path.write_text(json.dumps(payload, indent=2, default=str))
        # STEP 6a'': the caller's registered MCP servers (MCP-WORKER, design
        # §4.7), ONCE, before the runner's environment: every credential is
        # registered for redaction as the answer is parsed, and the runner is
        # handed only the relay file's path (`_build_child_env`). A 409 is the
        # fence: stand down without the agent, leave the lease (invariant 5).
        # A withheld server, or an attach that could not be made, fails the
        # attempt before the agent runs: it was named because it is needed.
        try:
            attached = self.mcp.attach(profile=cfg.profile, inputs=task.get("input"))
        except mcp_attach_mod.McpAttachFenced as exc:
            raise FencedWriteRefused(
                cfg.generation, -1, f"mcp attach: {exc.code}", write="mcp attach"
            ) from None
        unattached = attached.failure()
        if unattached is None and attached.servers:
            try:
                self.mcp.start(ws.private)
            except OSError as exc:
                unattached = (
                    mcp_attach_mod.UNAVAILABLE, f"relay_not_started:{type(exc).__name__}", True
                )
        if unattached is not None:
            self.mcp.close()
            # A WORKFLOW STEP'S per-run credential that lapsed while it waited
            # parks for its caller to re-store it (the owner 2026-10-26, §4.10);
            # anything else withheld still fails the attempt.
            lapsed = attached.lapsed_credentials() if task.get("workflow_id") else []
            if lapsed:
                return functools.partial(self._park_mcp_credential, lapsed)
            return functools.partial(self._fail_mcp_unattached, *unattached)
        try:
            child_env = self._build_child_env()
        except CredentialMissing as exc:
            return functools.partial(self._park_credential_missing, exc)
        except NoAccountAvailable as exc:
            # The pool is this tenant's way of running and it is momentarily
            # empty. A wait, not a failure -- see `_park_no_account`.
            return functools.partial(self._park_no_account, exc)

        # ---- STEP 6b: the issue this step was pointed at (archived issue 265) -----------
        # After the credentials, so every secret this attempt holds is
        # registered before the issue's text is scrubbed. A fetch that fails
        # fails the attempt here, before the agent starts (`agent_worker.issue`).
        # A forge that did not answer is retried in-process (`_forge_retry`),
        # and if it stays down the ATTEMPT fails retryably -- never
        # INPUTS_UNAVAILABLE, which is for a forge that answered "no" (F1).
        #
        # BESIDE THE CLONE (archived issue 721, P29): the network read started before the
        # clone (`_start_issue_prefetch`) and is joined here; the scrub and
        # the write still happen here, after the credentials. A fetch that
        # cannot be used -- none started, or for another repository URL than
        # the clone settled on -- is made here as it always was. Either way
        # a failure is raised from `stage_issue` exactly as before.
        issue_number = issue_mod.requested(task.get("input"), cfg.profile)
        if issue_number is not None:
            self.phases.enter("fetch_issue")
            prefetch = self._issue_prefetch
            if prefetch is not None and (
                prefetch.number != issue_number or prefetch.repository_url != self._repo_url
            ):
                prefetch = None
            if prefetch is not None and prefetch.running:
                # Still reading after the clone: the lease is beaten while
                # this waits, as it was while the clone ran.
                with self._heartbeat_meanwhile("fetch_issue"):
                    prefetch.wait()
            fetch_started = float(self.phases.seconds_since_start())
            try:
                staged = self._stage_issue(issue_number, ws.work, prefetch)
            except forge_mod.ForgeUnavailable as exc:
                return functools.partial(
                    self._fail_issue_unreachable,
                    issue_number,
                    str(self._scrub(str(exc)[:600])),
                    exc.retry_after_seconds,
                    exc.tries,
                )
            # The pull request's title names the issue by its title (archived issue 453):
            # read back from the file just written -- scrubbed, one line --
            # here, before the agent starts and can write that file.
            self._issue_title = _issue_title_from(staged, issue_number)
            self._issue_fetch_timing = (
                {**prefetch.timing(), "beside_clone": True}
                if prefetch is not None
                else {
                    "started_seconds": round(fetch_started, 3),
                    "ended_seconds": float(self.phases.seconds_since_start()),
                    "ok": True,
                    "beside_clone": False,
                }
            )

        # ---- STEP 6b': the pull request this step works on ----------------
        # After the credentials, for the scrub, as the issue. A read that
        # fails writes a file saying so and the agent starts: the pull
        # request is context beside the checkout (`agent_worker.pull_request`).
        pull_number = self._requested_pull_request()
        if pull_number is not None:
            self.phases.enter("fetch_pull_request")
            self._stage_pull_request(pull_number, ws.work)

        # Re-check fencing immediately before the agent starts. Cloning a large
        # repository can take minutes, and the whole point of step 1 is that
        # nothing runs under a stale generation -- including after a slow setup.
        self.phases.enter("revalidate_generation")
        self.control.validate_generation()

        # A provider that is already exhausted must not be hit again.
        self.phases.enter("quota_preflight")
        preflight = self.control.poll(cfg.provider)
        if self._paused_before_runner(preflight):
            return functools.partial(self._park_for_pause, None)
        quota_signal = signal_from_control(preflight, cfg.provider)
        if quota_signal is not None:
            decision = decide(
                quota_signal,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            if decision.park:
                return functools.partial(self._park_for_quota, decision, source="preflight")
        # STEP 6d: warm the clone's Python environment, once per attempt
        # (pythonenv.py says why). After the preflight, so a park pays nothing
        # for it; the generation and the cancel are read again before it
        # starts, and the generation again after it ran, so a stale worker
        # never starts the agent (invariant 5). Never fatal.
        checkout = ws.checkout() if self._repo_url else None
        if pythonenv_mod.applies(checkout):
            self.phases.enter("python_env")
            warmed = self._python_env.warm(
                checkout, child_env,
                may_start=lambda: not self.control.validate_generation().cancel_requested,
                meanwhile=self._heartbeat_meanwhile,
            )
            if warmed is not None and warmed.status in pythonenv_mod.RAN:
                self.control.validate_generation()
        return child_env

    # ------------------------------------------------------------------
    # child supervision
    # ------------------------------------------------------------------
    def _note_python_env(self, detail: dict[str, Any]) -> None:
        """The Python warm-up's one event line, on RUNNING with `detail.phase`
        (as `verify_spec` notes): an event that does not land is logged, and
        the attempt goes on."""
        try:
            self.control.emit(EventType.RUNNING, detail=detail)
        except FencedError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.warning("the Python warm-up's event was not written",
                             error=type(exc).__name__, warmup=detail)

    def _run_child_supervised(self, child_env: dict[str, str]) -> ChildResult | Outcome:
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        argv = _runner_argv(cfg)

        # THE RUNNER'S REPLY FILES ARE CLEARED BEFORE IT STARTS. result.json,
        # quota.json and credential.json are how a runner answers the worker,
        # and all three live in `work/` -- which is exactly what a checkpoint
        # archives and the next attempt restores. So a resumed attempt used to
        # start with the PREVIOUS attempt's answers already on disk:
        #
        #   * quota.json from the run that parked it. `_quota_from_child` read
        #     it after the NEW runner exited -- whatever it exited with -- and
        #     parked the task again on a 429 that belonged to another attempt.
        #     Every resumed attempt did its work and re-parked, so a task that
        #     was rate-limited once could never finish;
        #   * result.json, which a runner killed before writing its own left in
        #     place to be read as this run's result and counted as its spend.
        #
        # Anything in these paths before the runner starts is, by definition,
        # not from this runner. The in-place retry and the credential reload
        # already removed one file each for the same reason; this is the one
        # place that covers all three for every start.
        for stale in (ws.result_path, ws.quota_path, ws.credential_path):
            stale.unlink(missing_ok=True)
        # The session file is in `private/`, not `work/`, but the rule is the
        # same: what it holds before this start was the last start's.
        self._session_file_path().unlink(missing_ok=True)
        self._spend_pending = True
        # The session this runner reports for: its own, or an earlier
        # runner's it continues with `--resume` (`_account_channel_env`).
        self._note_runner_spend_start(resumed=bool(child_env.get(RESUME_SESSION_ENV)))
        self._open_run(resumed=bool(child_env.get(RESUME_SESSION_ENV)))

        if self.phases.current != "runner":
            # The last startup line. An in-place restart is not a new phase.
            self.phases.enter("runner")
        child = ChildProcess(
            argv,
            cwd=ws.work,
            env=child_env,
            stdout_path=ws.stdout_path,
            stderr_path=ws.stderr_path,
            max_stdout_bytes=cfg.max_stdout_bytes,
            max_stderr_bytes=cfg.max_stderr_bytes,
            # THE END OF EACH STREAM IS KEPT (archived issue 208). A runner's stderr ends
            # with the line it failed on, and `last_error` is the last 2,000
            # characters of it when the runner wrote no `error` of its own
            # (`_finalise`). Head-only, a stderr past its cap ended with the
            # truncation notice, and `last_error` quoted output from before
            # it. Head-only is git's rule -- a patch with its middle removed
            # must never read as one that fitted -- and a log is not a patch.
            keep_tail=True,
            # BELOW THE WORKER (archived issue 426): the agent and every process it forks
            # run `runner_niceness` under the worker, so a build on every core
            # cannot starve the heartbeat (`config.RUNNER_NICENESS_DEFAULT`).
            niceness=cfg.runner_niceness,
            logger=self.log,
        )
        self._child = child
        # THE STARTUP IS OVER. The SIGTERM stack dump is for a worker stuck
        # before its runner, where the stack is the diagnosis. From here a
        # SIGTERM is how an attempt is ordinarily stopped, and GKE would file
        # the dump as a page of ERROR lines per stop (owner, 2026-09-25).
        # Disarmed before `start`, so the window where the child exists and
        # the dump is armed is empty. Idempotent across in-place restarts.
        disarm_stack_dump()
        child.start()
        self._start_sampler(child)
        # After `start`, so the event's `at` is when the agent process exists.
        self._mark_agent_started()
        # Contract request 132: the start's task totals, held back through
        # the startup window so that a SIGTERM exit from it writes only the
        # attempt's own document. Made now, under its mid-run budget; nothing
        # once the first start has made it.
        self.control.store_pending_attempt_totals()

        now = time.monotonic()
        if self._lease_live_until is None:
            # The startup's beats landed (a failed one would have ended the
            # startup), so the lease is good for one extension from about now.
            self._lease_live_until = now + self.control.heartbeat_extension_seconds
        next_heartbeat = now + cfg.heartbeat_interval_seconds
        next_checkpoint = now + cfg.checkpoint_interval_seconds
        # archived issue 637: while the tree is unchanged the periodic checkpoint writes
        # nothing and the wait to the next look doubles, to the cap; a change
        # resets it. Only the periodic one: every exit's checkpoint is written.
        checkpoint_backoff = CheckpointBackoff(
            base_seconds=cfg.checkpoint_interval_seconds,
            cap_seconds=cfg.checkpoint_max_interval_seconds,
        )
        next_poll = now + cfg.control_poll_seconds
        next_live_log = now + cfg.live_log_interval_seconds

        while True:
            slice_ = max(
                0.05,
                min(
                    next_heartbeat - time.monotonic(),
                    next_checkpoint - time.monotonic(),
                    next_poll - time.monotonic(),
                    next_live_log - time.monotonic(),
                    self._runner_deadline - time.monotonic(),
                    5.0,
                ),
            )
            if child.wait(slice_) is not None:
                break
            now = time.monotonic()

            if self._interrupted:
                return self._handle_interruption(child)

            if now >= next_heartbeat:
                next_heartbeat = now + cfg.heartbeat_interval_seconds
                try:
                    self._heartbeat()
                except Exception as exc:  # noqa: BLE001
                    outage = self._control_plane_call_failed("heartbeat", exc)
                    if outage is not None:
                        return self._exit_control_plane_outage(child, outage)

            if now >= next_checkpoint:
                try:
                    self._checkpoint("periodic")
                except FencedError as exc:
                    # The checkpoint writes the task's pointer, so it is fenced
                    # like any task write. Meeting the fence here is meeting
                    # it mid-run, and it ends the same way as when the control
                    # poll finds it.
                    return self._exit_fenced_mid_run(
                        child, observed_generation=exc.actual, reason=str(exc)
                    )
                # A failed checkpoint is not an unchanged one: it is retried
                # at the base interval.
                next_checkpoint = now + checkpoint_backoff.next_interval(
                    changed=not self.checkpoints.last_unchanged
                )

            # BEATEN THROUGH, LIKE THE CHECKPOINT (archived issue 426). Both leave the control
            # plane for another service -- up to four GCS uploads with a 60 s
            # request timeout each, and the account broker over HTTP -- and
            # held this loop, and so the beat, for as long as either took.
            # Under `_heartbeat_meanwhile` a block shorter than the time left
            # to the next beat costs a thread start and no beat.
            if now >= next_live_log:
                with self._heartbeat_meanwhile("live log tails"):
                    self._publish_live_logs()
                next_live_log = now + cfg.live_log_interval_seconds

            if self._account is not None:
                # A file stat when nothing changed; a forward at most once per
                # window per minute; a hold read at most once per turn.
                with self._heartbeat_meanwhile("account channel"):
                    self._watch_account()

            if self.budget is not None:
                # A file stat when nothing changed; a price asked of swarm-api
                # at most every `budget.PRICE_INTERVAL_SECONDS` (archived issue 2459).
                with self._heartbeat_meanwhile("budget"):
                    self.budget.watch()

            if now >= next_poll:
                next_poll = now + cfg.control_poll_seconds
                try:
                    signals = self.control.poll(cfg.provider)
                except Exception as exc:  # noqa: BLE001
                    outage = self._control_plane_call_failed("control poll", exc)
                    if outage is not None:
                        return self._exit_control_plane_outage(child, outage)
                    continue
                diverted = self._apply_control_signals(child, signals)
                if diverted is not None:
                    return diverted
                try:
                    if self.children.has_requests(ws.work):
                        with self._heartbeat_meanwhile("child requests"):
                            self.children.tick(ws.work)
                except children_mod.ChildSubmitFenced as exc:
                    return self._exit_fenced_mid_run(
                        child, observed_generation=-1, reason=f"child submission: {exc.code}"
                    )
                # The graph ticket (GS-WORKER), renewed when a third of its
                # life is left; a stat-free clock check when it is not due.
                # A 409 is the fence: stop the agent, leave the lease.
                if self.graph_ticket.due():
                    try:
                        with self._heartbeat_meanwhile("graph ticket"):
                            self.graph_ticket.keep()
                    except graph_ticket_mod.GraphTicketFenced as exc:
                        return self._exit_fenced_mid_run(
                            child, observed_generation=-1, reason=str(exc)
                        )
                # The TypeSafe ticket (SEC-MIGRATE-TYPESAFE), the same way: a
                # third of its life left, or a fence the shim found (a 409 at
                # an on-demand renewal, or the forwarder's stale_generation).
                if self.typesafe.due():
                    try:
                        with self._heartbeat_meanwhile("typesafe ticket"):
                            self.typesafe.keep()
                    except typesafe_shim_mod.TypesafeTicketFenced as exc:
                        return self._exit_fenced_mid_run(
                            child, observed_generation=-1, reason=str(exc)
                        )
                if self.handoff.spooled:
                    # BROWSER-USE-FLAG: one mark when the agent has run
                    # BROWSER_IDLE_SECONDS without touching its browser.
                    self._note_browser_idle(child)
                    outcome = self._hand_off_tick(child, ws.work)
                    if outcome is not None:
                        return outcome

            if now >= self._runner_deadline:
                self.log.error("task timeout reached", timeout_seconds=cfg.timeout_seconds)
                child.mark_timed_out()
                child.terminate(cfg.termination_grace_seconds, reason="task timeout")
                break

        result = child.finish()
        self._child_ended(result)
        # An agent that ended with a hand-off open is waiting for nobody now:
        # resolved while the task is still RUNNING (live-browser §4.1).
        self.handoff.agent_exited(ws.work)
        self.log.info(
            "runner finished",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            killed=result.killed,
            seconds=round(result.duration_seconds, 2),
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
        )
        return result

    def _control_plane_call_failed(self, what: str, exc: Exception) -> Exception | None:
        """A mid-run Firestore call failed after its budget: log it; the outage, or None (archived issue 70).

        ONE FAILED CALL IS NOT FATAL WHILE THE LEASE IS LIVE. The call already
        retried for its budget (`control.MID_RUN_BUDGETS`), and the lease this
        worker last extended still holds the task, so the loop goes on and the
        next beat or poll asks again. Returned is the error that makes it an
        OUTAGE: the call failed at or after the moment that lease runs out
        (`_lease_live_until`), so Firestore has been unreachable for longer than
        the lease survives without a beat.

        Only an error that says the control plane could not be reached
        (`_control_plane_unreachable`) is handled here. A fence or a tenant
        mismatch is raised, as is anything else, which is a defect and goes
        to the crash handler as before.
        """
        if isinstance(exc, (FencedError, TenantMismatchError)):
            raise exc
        if not _control_plane_unreachable(exc):
            raise exc
        now = time.monotonic()
        live_until = self._lease_live_until
        outage = live_until is not None and now >= live_until
        self.log.warning(
            f"a {what} could not reach the control plane"
            + (
                "; the lease this worker last extended has run out, so this is an outage"
                if outage
                else "; the lease is still live, so the loop carries on"
            ),
            call=what,
            error_type=type(exc).__name__,
            error=_one_line(exc),
            lease_live_seconds=(round(live_until - now, 1) if live_until is not None else None),
        )
        return exc if outage else None

    def _exit_control_plane_outage(
        self,
        child: ChildProcess | None,
        exc: Exception,
        *,
        where: str = "past the lease while the agent ran",
    ) -> Outcome:
        """Firestore stayed unreachable past the lease: checkpoint, stop, exit 69 (archived issue 70).

        `child` is None on the credential reload's exit, taken between two
        runners when the tenant read could not be made: there is nothing to
        stop, and the rest is the same. `where` says which, in the log and on
        the attempt's document.

        Owner decision, 2026-09-28. Until now a Firestore call that failed
        while the agent ran raised into the crash handler, which FAILED the
        task for good -- a terminal state, written (when it could be written
        at all) for an outage the next attempt would not have met, without
        `max_attempts` being consulted. This leaves the way an unavailable
        dependency before the runner does (`_exit_unavailable_before_runner`):

          1. CHECKPOINT, label `control_plane_outage`. The archive goes to the
             bucket, which is not the service that is down; its pointer write
             is Firestore's and is attempted under its budget, and a failure
             there is logged, not raised (`_checkpoint`).
          2. STOP THE RUNNER, so no agent works on a task this worker can no
             longer prove it owns.
          3. WRITE THIS ATTEMPT'S OWN DOCUMENT: exit 69 and the cause, best
             effort under its budget.
          4. EXIT 69, which the reconciler reads as "requeue": the lease falls
             silent, it fences and releases it, and requeues the task or fails
             it once its attempts are spent.

        NOT WRITTEN: the task, the lease and the event stream. Firestore is
        what cannot be reached, and a requeue made from here would be a fenced
        transition, the retry cap and a lease release against it -- the
        reconciler's job, as on the startup exit.
        """
        cfg = self.cfg
        self.log.error(
            f"the control plane stayed unreachable {where}; checkpointing, "
            "stopping the runner and exiting 69 for a requeue",
            error_type=type(exc).__name__,
            error=_one_line(exc),
            exit_code=ExitCode.UNAVAILABLE,
            then=(
                "the reconciler reads this execution's exit code and requeues the "
                "task, or fails it when its attempts are spent"
            ),
        )
        try:
            self._checkpoint(CONTROL_PLANE_OUTAGE)
        except (FencedError, TenantMismatchError):
            # STOP THE RUNNER FIRST, then let the fence end the attempt (archived issue 453).
            # `child` need not be `self._child` -- an index phase's is not --
            # so the stand-down's `_stop_runner` would leave it running, an
            # agent at work on a task this attempt no longer owns.
            if child is not None:
                if child.poll() is None:
                    child.terminate(cfg.termination_grace_seconds, reason="generation fenced")
                child.finish()
                self._child_ended()
            raise
        if child is not None:
            child.terminate(cfg.termination_grace_seconds, reason="control plane outage")
            child.finish()
            self._child_ended()
        self._control_plane_down = True
        try:
            self.control.record_attempt_end(
                exit_code=ExitCode.UNAVAILABLE,
                error=self._scrub(
                    f"{CONTROL_PLANE_OUTAGE}: the control plane was unreachable "
                    f"{where} ({type(exc).__name__}: "
                    f"{_one_line(exc, 300)}); the attempt checkpointed and exited "
                    "for a requeue"
                ),
            )
        except Exception as write_exc:  # noqa: BLE001
            self.log.warning(
                "could not record the outage on this attempt's own document",
                error=f"{type(write_exc).__name__}: {write_exc}",
            )
        return Outcome(exit_code=ExitCode.UNAVAILABLE, detail={"cause": CONTROL_PLANE_OUTAGE})

    def _apply_control_signals(
        self, child: ChildProcess, signals: ControlSignals | None = None
    ) -> Outcome | None:
        cfg = self.cfg
        if signals is None:
            signals = self.control.poll(cfg.provider)

        if signals.is_fenced(cfg.generation):
            return self._exit_fenced_mid_run(
                child,
                observed_generation=signals.generation,
                reason=(
                    f"fenced mid-run: task at generation {signals.generation} in "
                    f"{signals.state.value}, lease released={signals.lease_released}"
                ),
            )

        if signals.cancel_requested:
            self.log.warning("cancellation requested; stopping the runner")
            # CANCEL DURING A HAND-OFF'S HOLD (live-browser §4.4): the agent's
            # `wait` exits 4, the gate ends control and wipes, swarm-api's
            # wait is resolved `cancelled`; then the ordinary cancel path.
            if self.handoff.end(
                self.ws.work if self.ws is not None else None,
                outcome="cancelled",
                cause="cancelled",
            ):
                child.wait(handoff_mod.CANCEL_ANSWER_SECONDS)
            child.terminate(cfg.termination_grace_seconds, reason="cancelled")
            child.finish()
            self._child_ended()
            return self._finish_cancelled()

        if signals.pause_requested:  # after the fence and the cancel (`_park_for_pause`)
            return self._park_for_pause(child)
        quota = signal_from_control(signals, cfg.provider)
        if quota is not None:
            decision = decide(
                quota,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            if decision.park:
                self.log.warning(
                    "provider unavailable for longer than the worker may wait; parking",
                    wait_seconds=decision.wait_seconds,
                )
                child.terminate(cfg.termination_grace_seconds, reason="provider backpressure")
                child.finish()
                self._child_ended()
                return self._park_for_quota(decision, source="backpressure")
        return None

    def _handle_interruption(self, child: ChildProcess) -> Outcome:
        """SIGTERM to the WORKER: something is taking the sandbox away.

        Two senders, and they are not alike.

          * The platform reclaiming an instance, or a backend deadline. This
            worker still owns its task, so it checkpoints, parks the task and
            hands the slot back.
          * The reconciler deleting the Job. That worker has usually been
            FENCED already: `obsolete_generation` deletes the Job of a
            superseded attempt, and the browser-eviction repair (archived pull request 41)
            fences in one pass and deletes in a later one. Such a worker owns
            nothing. Every write it would make here lands on a task, a lease
            or pools that now belong to a newer generation. The old code made
            them all. It parked a task the reconciler had fenced, and it
            parked or failed a newer attempt the scheduler had already
            admitted (found by archived pull request 41).

        So the fence is checked first. `_checkpoint` checks it before it
        uploads anything. Every write after that checks again inside its own
        transaction (`ControlPlane._fenced_task`), because a fence can land
        between the check and the write. A fenced worker stands down
        (`_stand_down`).

        THE PARK IS UNCHANGED FOR A WORKER THAT STILL OWNS ITS TASK, and it
        has a gap this method does not close. Nothing in the platform promotes
        a SCHEDULED_RETRY park today. The scheduler's sweeps cover quota,
        dependency and credential parks only (scheduler/loop.py), and
        scheduler/dispatch.py says so about this path. This docstring used to
        claim the scheduler picks the task up on its next pass. That was not
        true. The gap is recorded as open in
        docs/incidents/2026-09-24-gke-dispatch.md, which also names the
        decision a promoter needs: what an interrupted last attempt becomes.
        """
        cfg = self.cfg
        # The signal already said "stopping: SIGTERM in phase runner", once,
        # from the handler (`_on_signal`). A second line here would make one
        # stop read as two.
        child.terminate(cfg.termination_grace_seconds, reason="worker interrupted")
        child.finish()
        self._child_ended()
        if self._cancel_requested_now():
            # THE CANCEL ARRIVING AS A SIGTERM (archived issue 627). The API's cancel route
            # asks the backend to cancel the execution directly, and Cloud
            # Run's cancel and a GKE Job delete both reach this process as a
            # SIGTERM -- usually before the next control poll has read the
            # flag. Parking would put a task somebody stopped back in line, so
            # it ends CANCELLED here, with its spend and its end recorded, the
            # way the control poll's cancel ends it.
            self.log.warning("SIGTERM on a task whose cancel was requested; ending it cancelled")
            try:
                return self._finish_cancelled(push=False)
            except FencedError as exc:
                return Outcome(
                    exit_code=self._stand_down(exc, where="SIGTERM"), detail={"fenced": True}
                )
        try:
            # NO CARRIER PUSH IN THE GRACE WINDOW (archived issue 453). The fetch of the
            # branch's tip and the push are forge calls, each bounded only by
            # `git_clone_timeout_seconds`, and they ran here, before the park:
            # a slow forge let SIGKILL land first, leaving a task neither
            # parked nor released for the reconciler to find. The checkpoint
            # still holds the committed work, the next attempt restores it,
            # and its own checkpoints and finish push it -- a dependant starts
            # only from a SUCCEEDED parent's recorded head, which is the finish's.
            self._checkpoint("interrupted", push=False)
            summary = self._upload_outputs()
            with self._metrics_after_the_record():
                self.control.park(
                    reason=ParkReason.SCHEDULED_RETRY,
                    next_eligible_at=utcnow(),
                    detail={"cause": "worker_interrupted", **summary},
                )
        except FencedError as exc:
            return Outcome(
                exit_code=self._stand_down(exc, where="SIGTERM"), detail={"fenced": True}
            )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _cancel_requested_now(self) -> bool:
        """Whether the task's cancel has been requested, read now. Never raises.

        A read that fails answers False: the SIGTERM path then parks as it
        always has, and a parked task with a cancel on it is cancelled by the
        scheduler's drain rather than run again.
        """
        try:
            return bool((self.control.fetch_task() or {}).get("cancel_requested"))
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not read whether a cancel was requested",
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def _finish_cancelled(self, *, push: bool = True) -> Outcome:
        """End a cancelled task once its runner has stopped and been collected.

        The checkpoint, then the uploads (which record the attempt's spend,
        `_upload_outputs`), then the terminal write, which records the
        attempt's end and releases the lease (`control.finish`). `push=False`
        from a SIGTERM (archived issue 627): no carrier push inside the grace window (archived issue 453).
        """
        self._checkpoint("cancellation", push=push)
        summary = self._upload_outputs()
        self._add_runner_block(summary)
        with self._metrics_after_the_record():
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=None,
                error="cancelled by request",
                result_summary=summary,
                end_cause=self.control.cancel_cause(),
            )
        return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

    # -- the fenced exits ---------------------------------------------------
    def _exit_fenced_mid_run(
        self, child: ChildProcess, *, observed_generation: int, reason: str
    ) -> Outcome:
        """Someone else owns this task now: stop the agent and leave the lease alone.

        Reached from the control poll and from a periodic checkpoint that met
        the fence. Both are the RUNNING phase, so both emit `generation_fenced`
        with phase `running`, as the poll always has. The lease is not ours to
        release any more.
        """
        cfg = self.cfg
        self._fenced_exit = True
        # Before the runner is stopped: from here the relay forwards nothing.
        self.mcp.mark_fenced()
        self.log.error(
            "generation fenced mid-run; stopping the runner",
            observed_generation=observed_generation,
            expected_generation=cfg.generation,
            reason=reason,
        )
        child.terminate(cfg.termination_grace_seconds, reason="generation fenced")
        child.finish()
        self._child_ended()
        self.control.emit(
            EventType.GENERATION_FENCED,
            {
                "expected_generation": cfg.generation,
                "observed_generation": observed_generation,
                "phase": "running",
            },
        )
        self._record_fenced_end(reason)
        return Outcome(exit_code=ExitCode.GENERATION_FENCED, detail={"fenced": True})

    def _stand_down(self, exc: FencedError, *, where: str) -> int:
        """Superseded on the way out: stop, record it on OUR attempt, leave.

        Reached when a write that would end or record this attempt was refused
        because the attempt is fenced. The write may be a SIGTERM park, a quota
        or credential park, a terminal state (including the crash handler's
        FAILED), a checkpoint, or the transition into RUNNING. Everything the
        attempt still meant to write is dropped except its own attempt
        document. That means no task state, no checkpoint pointer, no lease
        release, no pool change and no event.

        WHY NOT EVEN AN EVENT, when the startup and mid-run fences emit
        `generation_fenced`? The event stream belongs to the task, and the task
        now belongs to a newer generation or to whatever ended it. The
        component that fenced this attempt has its own record there. The
        attempt document is the one record that is this worker's own, and the
        API's attempt view reads it (`swarm_api/codec.py`).

        The reconciler finishes what this does not. A lease that is not
        released is reclaimed by the rules that release it after the Job is
        gone (`obsolete_generation`, `stale_lease`, `missing_execution`).

        WHAT HOLDS FOR THE WHOLE EXIT, and not only from here on. An event
        that announces a write is committed with that write
        (`ControlPlane.transition`'s `events`), so a park that is refused
        leaves no QUOTA_EXHAUSTED behind. Writes made BEFORE the refusal can
        still come from an attempt that was already fenced:

          * the tenant's provider document, which `_park_for_quota`
            publishes ahead of its park (see there);
          * an event emitted in the gap between a check that passed and the
            next call. CHECKPOINT_STARTED follows `ensure_owner` that way.
            PARKED, the terminal events, CHECKPOINT_COMPLETED and
            LEASE_RELEASED each follow the write they record, which this
            attempt made while it still owned the task.
        """
        own = self._own_end_exit(where=where)
        if own is not None:
            # Not superseded: the fence is this attempt's OWN committed end.
            self._stop_runner(reason="attempt ended")
            return own
        self._fenced_exit = True
        self.mcp.mark_fenced()
        self.log.error(
            "FENCED on the way out: this attempt has been superseded; exiting "
            "without writing the task, the lease or an event",
            where=where,
            refused=getattr(exc, "write", "") or None,
            expected_generation=exc.expected,
            observed_generation=exc.actual,
            reason=str(exc),
        )
        # A runner still alive here (a crash, a fenced checkpoint mid-run) is
        # stopped before anything is recorded, so the attempt's end is not
        # written while its agent is still working.
        self._stop_runner(reason="generation fenced")
        self._record_fenced_end(str(exc))
        return ExitCode.GENERATION_FENCED

    def _own_end_exit(self, *, where: str) -> int | None:
        """The exit of an attempt whose OWN end has committed, or None (archived issue 2064).

        A park, a finish or a retry writes the task's new state, the attempt's
        end and the lease release in ONE commit (`ControlPlane._end_in_transaction`),
        and the control plane records which state that commit wrote
        (`committed_end`). A crash after it, or a later write it fences, used
        to read the task's PARKED as "superseded" and stand down (2026-10-23
        09:05:41Z, task_7782dd6c7a9e4cb3ade0): the exit said fenced and the
        attempt's park record was overwritten. It is this attempt's own end.
        Same attempt, same generation, and its lease was released in that
        commit, so there is nothing left to release and nothing to write: the
        exit is the one the end means.

        The release in the same commit is checked, not assumed. A control
        plane that reports an end without the release is a broken invariant
        1, and it is logged as one; the reconciler's stale-lease rule is the
        only thing that would return that slot.
        """
        ended = getattr(self.control, "committed_end", None)
        if ended is None:
            return None
        if getattr(self.control, "lease_released", False) is not True:
            self.log.error(
                "this attempt's own end committed but its lease was not released "
                "with it: invariant 1 is broken, the reconciler must reclaim it",
                where=where,
                state=ended.value,
            )
        else:
            self.log.warning(
                "this attempt's own end had already committed, its lease released "
                "in the same commit; nothing is left to write or release",
                where=where,
                state=ended.value,
            )
        return {
            TaskState.PARKED: ExitCode.PARKED,
            TaskState.SUCCEEDED: ExitCode.OK,
            TaskState.CANCELLED: ExitCode.CANCELLED,
        }.get(ended, ExitCode.FAILED)

    def _default_reap(self) -> tuple[int, ...]:
        """The production pre-publish reap. See `reap_foreign_processes`."""
        return reap_foreign_processes(logger=self.log)

    def _stop_runner(self, *, reason: str) -> None:
        """Stop the live runner, if any, and take its usage and spend. Never raises.

        Safe on a runner that has already been reaped: `finish` may be called
        twice, and `_child_ended` does nothing the second time.
        """
        child = self._child
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate(self.cfg.termination_grace_seconds, reason=reason)
            child.finish()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not stop the runner cleanly", error=f"{type(exc).__name__}: {exc}")
        try:
            self._child_ended()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not record the runner's usage", error=f"{type(exc).__name__}: {exc}"
            )

    def _record_fenced_end(self, reason: str) -> None:
        """Close THIS attempt's own document with the fence as its reason. Never raises.

        The attempt document is the worker's own, so writing it is inside
        invariant 5. An attempt that never records `completed_at` looks alive
        forever. The checkpoint collector then holds its checkpoints until the
        backstop, as `reconciler/checkpoints.py` describes for the same shape.
        """
        try:
            self.control.record_attempt_end(
                exit_code=ExitCode.GENERATION_FENCED, error=self._scrub(reason[:4000])
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not record the fenced exit on this attempt's document",
                error=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # finalisation
    # ------------------------------------------------------------------
    def _finalise(self, result: ChildResult) -> Outcome:
        """The runner ended on its own: record the end, THEN export metrics.

        Every branch of `_record_finalised` ends in a terminal write, and the
        export waits for whichever one it took (`_metrics_after_the_record`).
        """
        with self._metrics_after_the_record():
            return self._record_finalised(result)

    def _record_finalised(self, result: ChildResult) -> Outcome:
        ws = self.ws
        assert ws is not None

        if result.timed_out:
            # Before the final checkpoint, so it carries the note (the owner, 2026-11-02).
            self._write_continuation_note()
        final = self._checkpoint("final")
        # Whether this stop at the limit continues: decided before the
        # publish, which a continued attempt withholds (`_publish_withheld`'s
        # rule: an attempt that runs again publishes nothing).
        no_continuation = (
            self._time_limit_refusal(final) if result.timed_out else None
        )
        # A runner that finished cleanly, by the same three tests the SUCCEEDED
        # branch below applies: the one outcome a missing expected output turns
        # into a retryable failure (archived issue 149). Anything else fails, or ends, on its
        # own terms. result.json lives in `work/`, which neither the harvest
        # nor the upload writes, so reading it here reads what is read below.
        ran_clean = (
            not result.timed_out
            and result.exit_code == 0
            and _read_json(ws.result_path) is not None
        )
        # Read BEFORE the harvest and the upload: the reap in the harvest is
        # what makes the agent's files stop changing, and the title file is
        # the agent's.
        title_refused = self._refused_title_reason() if ran_clean else None
        # HARVEST AND PUBLISH UNDER A BEATING LEASE (archived issue 2686). The supervision
        # loop has stopped and `_checkpoint("final")` beats only for itself, so
        # the harvest, the upload and the forge publish below ran with nothing
        # beating: task_f06e7358 went 94-111 s silent here on each of three
        # attempts, task_118cd828 108 s and 97 s, and the reconciler fenced a
        # live, finishing worker every time. The thread asks for the fence
        # before every beat and stops on it (invariant 5), and the publish
        # still reads it before each forge write (`_own_before_forge_write`).
        # The block ends before any terminal write, park or release below, so
        # no beat races the write that ends the lease.
        with self._heartbeat_meanwhile("harvest/publish"):
            # The ONLY call that may lead to a publish. The agent exited on its own
            # here; the other five call sites are parks and crashes. The harvest
            # runs and the publish WAITS (archived issue 165, owner decision 2026-09-28): it is
            # made below, only once the upload manifest has passed the check.
            summary = self._upload_outputs(defer_publish=True)
            if ran_clean and self._no_change(summary):
                # An empty diff this step was allowed (2026-10-05): its result,
                # which the missing-output check and `_published_nothing` read,
                # and which a dependant that needed the change skips on.
                summary[expected_mod.NO_CHANGE_SUMMARY_KEY] = True
                self.log.info(
                    "nothing to change: the diff is empty and this step allows it",
                    allow_empty_diff=True,
                )
            elif ran_clean and (verified := self._verified_no_change(summary)) is not None:
                # Proposal J (owner decision 2026-10-08): an agent that changed
                # nothing AND wrote verification.md checked and found the work
                # done, which is the same result `allow_empty_diff` produces,
                # whatever the step's flag says. Its text is the reason. Without
                # the file, `empty_diff` / `published_nothing` fail it as before.
                summary[expected_mod.NO_CHANGE_SUMMARY_KEY] = True
                summary[NO_CHANGE_REASON_SUMMARY_KEY] = verified
                self.log.info(
                    "nothing to change: the diff is empty and the agent wrote "
                    f"{VERIFICATION_FILE} (or, publishing onto a pull request, {CHECK_VERDICT_FILE})",
                    allow_empty_diff=self._allows_empty_diff(),
                )
            # Before the check, which counts what is carried as present (archived issue 166).
            self._carry_parked_uploads(summary)
            # Here, where the runner ended on its own, and not on the park, cancel
            # and crash paths: a parked attempt resumes later and may still write
            # the file.
            missing = self._report_missing_outputs(summary, fails_the_attempt=ran_clean)
            withheld = self._publish_withheld(ran_clean, missing=missing, title_refused=title_refused)
            if result.timed_out and no_continuation is None:
                withheld = ("this attempt continues at the time limit; the attempt "
                            "that finishes the step publishes")
            self._publish_checked(summary, withheld)
            if (
                ran_clean
                and withheld is None
                and summary.get(expected_mod.NO_CHANGE_SUMMARY_KEY) is True
            ):
                # A `publish_to_pr` step that checked and changed nothing (archived issue 2241):
                # its evidence goes onto the pull request as a comment, not a commit.
                self._post_no_change_evidence(summary)
                self._record_forge_credential(summary)
            if ran_clean and self._no_change_after_publish(summary):
                self.log.info("nothing to change: the moved branch already holds this round's change",
                              reason=summary.get(NO_CHANGE_REASON_SUMMARY_KEY))
            if self._pushed_branch is not None:
                # The branch this attempt actually pushed, read from git after the
                # push (archived issue 667, observer P11): a step summary can be checked against
                # it. Its `name` and `head` are also what `carrier: branches` (D13)
                # reads to start the next step from this one's work.
                summary["branch"] = self._scrub(dict(self._pushed_branch))
            elif self._carrier_pushed is not None:
                # `carrier: branches` (D13): where this step's work is kept for
                # the next step, by name and head, as the last push left it.
                summary["branch"] = self._scrub(dict(self._carrier_pushed))
            summary["exit_code"] = result.exit_code
            summary["duration_seconds"] = round(result.duration_seconds, 3)
            if len(self._runs) > 1:
                # EVERY RUN (SWAP-RUNS, archived issue 2457): the attempt's agent time is the
                # sum of its runs; the last run's own figure keeps its own name.
                # A run whose duration is unknown is left out of the sum and
                # the total says `duration_partial` (issue 56), never summed as 0.0.
                summary.update(_runs_duration(self._runs))
                summary["last_run_duration_seconds"] = round(result.duration_seconds, 3)
            if self._index_phases is not None:
                summary["repo_index_phases"] = [r.as_dict() for r in self._index_phases]
            if self._verdict is not None:
                summary["verdict_gate"] = dict(self._verdict)
                if not self._verdict.get("agent_ran", True):
                    # The owner's words for it (2026-10-05): on MERGE, no fix
                    # agent; the step only published the reviewed work.
                    summary["skipped_agent"] = f"review verdict {self._verdict['verdict']}"
                    noop = self._verdict.get(verdict_mod.NOOP_PRECHECK_KEY)
                    if noop is not None:
                        # archived issue 2524 check (b): a NOT_YET whose head a MERGE already covers.
                        summary[verdict_mod.NOOP_PRECHECK_KEY] = dict(noop)
                        summary["skipped_agent"] = f"no-op precheck (b): {noop['reason']}"
                if result.exit_code != EXIT_TERMINATED:
                    # After the publish, so the epic hears of a review only once
                    # the step it gated has done its own work; never on a stop.
                    summary[findings_epic_mod.SUMMARY_KEY] = self._file_review_minors()
            if self._security_review is not None:
                record = dict(self._security_review)
                if not record.get("error") and result.exit_code != EXIT_TERMINATED:
                    record["comment"] = self._post_security_report()
                summary[security_review_mod.SUMMARY_KEY] = self._scrub(record)
            if self._pr_text_from is not None:
                summary["pull_request_text_from"] = dict(self._pr_text_from)
            if self._rendered_review is not None:
                summary[verdict_mod.RENDERED_REVIEW_KEY] = self._scrub(dict(self._rendered_review))
            if self._flow_output is not None:
                summary[flow_output_mod.SUMMARY_KEY] = self._scrub(dict(self._flow_output))

            runner_result = self._add_runner_block(summary)
            self._record_quality_gates(summary)
            # The plugins this step had and how often each skill ran.
            plugins_record = plugins_mod.record(
                self._plugins, ws.artifacts / "claude-code.stdout.log")
            if plugins_record is not None:
                summary[plugins_mod.SUMMARY_KEY] = plugins_record
            self._settle_jev()

        if self.budget is not None:
            # The limits, what the attempt used and, when it stopped the
            # agent, how (archived issue 2459); on every budgeted attempt, however it ended.
            summary[budget_mod.SUMMARY_KEY] = self.budget.summary()
        if self.budget is not None and self.budget.exceeded:
            # STOPPED BY ITS BUDGET: checkpointed and published above, as a
            # timed-out step is. FAILED with the cause `budget_exceeded`
            # (contract request 118), written only here.
            self._finish_terminal(
                state=TaskState.FAILED,
                exit_code=result.exit_code,
                error=self.budget.error(),
                result_summary=summary,
                end_cause=EndCause.BUDGET_EXCEEDED,
            )
            return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

        if result.timed_out and no_continuation is None:
            return self._continue_at_time_limit(final, summary)
        if result.timed_out:
            error = f"runner exceeded its {self.cfg.timeout_seconds}s timeout and was killed"
            if no_continuation == NO_CONTINUATION_CAP:
                error += (
                    f"; it ran out of continuations: it had already continued at the "
                    f"time limit {self._time_limit_continuations()} of "
                    f"{TIME_LIMIT_MAX_CONTINUATIONS} times"
                )
            elif no_continuation == NO_CONTINUATION_BUDGET:
                error += "; it was not continued, because another attempt would exceed its budget"
            elif no_continuation == NO_CONTINUATION_CHECKPOINT:
                error += "; it was not continued, because its final checkpoint did not upload"
            self._finish_terminal(
                state=TaskState.FAILED,
                exit_code=result.exit_code,
                error=error,
                result_summary=summary,
                end_cause=EndCause.TIMEOUT,
            )
            return Outcome(exit_code=ExitCode.TIMEOUT, state=TaskState.FAILED)

        if result.exit_code == 0:
            if runner_result is None:
                error = "runner exited 0 without writing result.json"
                self._finish_terminal(
                    state=TaskState.FAILED,
                    exit_code=0,
                    error=error,
                    result_summary=summary,
                    end_cause=EndCause.RUNNER_ERROR,
                )
                return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)
            if self.cfg.provider:
                # A clean run is evidence the provider is healthy again.
                self.control.update_quota_state(
                    provider=self.cfg.provider, state=ProviderState.AVAILABLE
                )
            if missing:
                return self._fail_for_missing_outputs(missing, summary, exit_code=0)
            if title_refused is not None:
                return self._fail_for_refused_title(title_refused, summary, exit_code=0)
            if self._index_refused is not None:
                return self._fail_for_refused_index(self._index_refused, summary, exit_code=0)
            git_summary = summary.get("git")
            leak = git_summary.get("final_tree_leak") if isinstance(git_summary, dict) else None
            if leak:
                return self._fail_for_final_tree_leak(str(leak), summary, exit_code=0)
            undid = git_summary.get(AUTO_COMMIT_UNDID_FIELD) if isinstance(git_summary, dict) else None
            if undid:
                return self._fail_for_auto_commit_undid(str(undid), summary, exit_code=0)
            forge_refused = (
                git_summary.get("forge_write_refused") if isinstance(git_summary, dict) else None
            )
            if isinstance(forge_refused, dict):
                return self._fail_for_forge_write_refused(forge_refused, summary, exit_code=0)
            nothing = self._published_nothing(summary)
            if nothing is not None:
                return self._fail_for_published_nothing(nothing, summary, exit_code=0)
            push_failed = self._publish_failed(summary)
            if push_failed is not None:
                return self._fail_for_published_nothing(push_failed, summary, exit_code=0)
            unreachable = self._publish_unreachable(summary)
            if unreachable is not None:
                return self._fail_for_publish_unreachable(unreachable, summary, exit_code=0)
            # After the outage, which stays retryable: anything else that left
            # a skip-path step without its branch fails it now (PUB-SKIP).
            unpushed = self._skipped_publish_missing(summary)
            if unpushed is not None:
                self._mark_publish(summary, failed=str(self._scrub(unpushed)))
                return self._fail_for_published_nothing(unpushed, summary, exit_code=0)
            self._mark_publish(summary)
            # JC2's verdict, AFTER the `published` mark the route checks its
            # commit against (section 9, check 4) and before the success: a
            # send-back ends the attempt in the route's own transaction.
            judged = self._deliver_contract_verdict(summary, runner_result)
            if judged is not None:
                return judged
            # LAST before the success, so an attempt that fails above never
            # leaves a write-once verdict behind it for its retry to meet.
            unpublished = self._publish_review_verdict(summary)
            if unpublished is not None:
                return unpublished
            self._finish_terminal(
                state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary
            )
            return Outcome(exit_code=ExitCode.OK, state=TaskState.SUCCEEDED)

        if result.exit_code == EXIT_TERMINATED:
            self._finish_terminal(
                state=TaskState.CANCELLED,
                exit_code=result.exit_code,
                error="runner stopped on SIGTERM",
                result_summary=summary,
            )
            return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

        error = (runner_result or {}).get("error") or _tail_text(ws.stderr_path)
        self._finish_terminal(
            state=TaskState.FAILED,
            end_cause=EndCause.RUNNER_ERROR,
            exit_code=result.exit_code,
            # `last_error` is a Firestore field and a failing CLI is exactly the
            # thing that echoes its own configuration, so the tail is scrubbed.
            error=self._scrub(str(error)[:4000]) if error else f"runner exited {result.exit_code}",
            result_summary=summary,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _stage_work_contract(self, task: Mapping[str, Any], work: Path) -> dict[str, str]:
        """The runner's variables naming the step's SIGNED work contract for
        JC2 (`poststeps.read_contract`): `SWARM_WORK_CONTRACT_DIGEST`, the
        digest in the signed dispatch block (`metadata.dispatch.work_contract
        .digest`), and `SWARM_WORK_CONTRACT_FILE`, the stored body
        (`metadata.work_contract`, as `plugins.selected` reads it) written to
        `work/WORK_CONTRACT_FILE`. {} for a step with no signed reference.

        The digest is the authority: the runner judges a body only when it
        digests to it, so a body the agent rewrote in work/ is not judged. A
        reference whose body is missing or cannot be written still names its
        digest, and JC2 says `not_run` (`contract_unreadable`) rather than
        nothing. A file a restored checkpoint carried is replaced or removed,
        never read for this attempt. Never raises."""
        from .judge import poststeps

        path = work / WORK_CONTRACT_FILE
        metadata = task.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        dispatch = metadata.get("dispatch")
        reference = dispatch.get("work_contract") if isinstance(dispatch, Mapping) else None
        digest = reference.get("digest") if isinstance(reference, Mapping) else None
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning("a previous work contract file could not be removed",
                             error=type(exc).__name__)
        if not isinstance(digest, str) or not digest.strip():
            return {}
        env = {poststeps.CONTRACT_DIGEST_ENV: digest.strip()}
        # The Jev work-contract experiment's arm (`contract_jev`, signed in
        # the same block): `off` makes JC2 `skipped:arm` with no call.
        arm = graphstage_mod.switch_of(dispatch, poststeps.CONTRACT_JEV_SWITCH)
        if arm is not None:
            env[poststeps.CONTRACT_JEV_ENV] = arm
        document = metadata.get("work_contract")
        if isinstance(document, Mapping):
            try:
                # No `default=`: a body that is not plain JSON would digest
                # differently once written, so it is not written at all.
                path.write_text(json.dumps(document))
            except (OSError, TypeError, ValueError) as exc:
                self.log.warning("the work contract could not be written for the runner",
                                 error=type(exc).__name__)
            else:
                env[poststeps.CONTRACT_FILE_ENV] = str(path)
        return env

    def _deliver_contract_verdict(
        self, summary: dict[str, Any], runner_result: Mapping[str, Any] | None
    ) -> Outcome | None:
        """JC2's verdict, from the runner's report (`contract_judgment`), posted
        once to `state/contract-verdict` about the head this attempt pushed
        (`poststeps.deliver_contract_verdict`, a new request id), or None to
        go on to the success.

        * A STALE GENERATION (409 `attempt_fenced`) stands down as every other
          fenced write here does (`_stand_down`): nothing else is posted, the
          task and the lease are not touched (invariant 5).
        * `unmet` SENT BACK: the route ended this attempt and returned the step
          to READY in its own fenced transaction (section 5.4; invariant 1,
          READY costs nothing), so the attempt ends there with no further
          terminal write.
        * Any other refusal or outage, or a worker on the direct Firestore path
          (no state route to post to), is recorded in the summary and the step
          goes on: the control plane re-asks from the published diff
          (`contractreask`, the owner's answer 5)."""
        from .judge import poststeps

        output = runner_result.get("output") if isinstance(runner_result, Mapping) else None
        verdict = output.get(CONTRACT_JUDGMENT_KEY) if isinstance(output, Mapping) else None
        if not isinstance(verdict, Mapping):
            return None
        git = summary.get("git")
        head = (git.get("pushed_head")
                if isinstance(git, dict) and git.get("published") is True else None)
        if not getattr(self.control, "state_via_api", False):
            summary[CONTRACT_VERDICT_SUMMARY_KEY] = {"delivery": "no_state_route"}
            self.log.warning("the contract verdict has no state route on the direct path; "
                             "the control plane re-asks from the published diff")
            return None
        answers: list[Mapping[str, Any]] = []

        def post(op: str, fields: Mapping[str, Any]) -> None:
            answers.append(self.control._post(op, fields, call=None) or {})

        try:
            delivery = poststeps.deliver_contract_verdict(
                post, verdict, published_head=head, request_id=new_request_id())
        except FencedError as exc:
            return Outcome(exit_code=self._stand_down(exc, where="contract verdict"),
                           detail={"fenced": True})
        answer = answers[-1] if answers else {}
        summary[CONTRACT_VERDICT_SUMMARY_KEY] = {
            "delivery": delivery, "outcome": answer.get("outcome"),
            "waiting_on": answer.get("waiting_on")}
        self.log.info("contract verdict", delivery=delivery, outcome=answer.get("outcome"),
                      head=head)
        if answer.get("sent_back") is True:
            return Outcome(exit_code=ExitCode.FAILED, state=TaskState.READY,
                           detail={"sent_back": "contract_unmet"})
        return None

    def _publish_review_verdict(self, summary: dict[str, Any]) -> Outcome | None:
        """The review step's `review.json`, written once to the verdicts prefix (CR 36).

        Only `claude-code-review` publishes: its service account is the one
        identity that may create under `tenants/<tenant>/verdicts/` (the M2
        bucket split), so a verdict there was written by a review worker and
        by nothing an agent of another step controls. The path is
        `post_verdict.verdict_key`, from this execution's tenant, its signed
        workflow id and its own task id; `post-verdict` and `merge` derive the
        same path from their own signed specs (docs/merge-step.md §4.3).

        Returns None when there is nothing to publish (another profile, or a
        task outside a workflow, whose verdict nothing reads) or it was
        published; otherwise the attempt's end:

          * no `review.json`, or one that is not the schema: the attempt
            fails retryably, OUTPUTS_MISSING once its attempts are spent --
            the agent may write a usable one next time;
          * A DIFFERENT OBJECT IS ALREADY AT THE PATH: refused, FAILED with
            PUBLISH_REFUSED, never retried. The prefix is write-once
            (`ifGenerationMatch=0`), and a retry would meet the same object.
            The same bytes again are already published, not refused;
          * the store could not be written: retryable, OUTPUTS_MISSING.
        """
        if self.cfg.runner_profile != post_verdict_mod.REVIEW_PROFILE:
            return None
        workflow_id = (self._task or {}).get("workflow_id")
        ws = self.ws
        if not isinstance(workflow_id, str) or not workflow_id or ws is None:
            return None
        name = post_verdict_mod.VERDICT_FILENAME
        try:
            key = post_verdict_mod.verdict_key(self.cfg.tenant_id, workflow_id, self.cfg.task_id)
            # Not followed if it is a link: the artifacts directory is the
            # agent's, and the verdict is the bytes of a file it wrote there.
            fd = os.open(ws.artifacts / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(post_verdict_mod.MAX_REVIEW_BYTES + 1)
            post_verdict_mod.parse_review(data)
        except (OSError, post_verdict_mod.VerdictUnreadable) as exc:
            reason = (
                f"verdict_unpublished: the review wrote no usable {name} "
                f"({exc if isinstance(exc, post_verdict_mod.VerdictUnreadable) else type(exc).__name__})"
            )
            return self._fail_verdict_unpublished(reason, summary, retryable=True,
                                                  end_cause=EndCause.OUTPUTS_MISSING)
        try:
            result = post_verdict_mod.publish_verdict(self.store, key, data)
        except post_verdict_mod.VerdictAlreadyPublished as exc:
            return self._fail_verdict_unpublished(f"verdict_exists: {exc}", summary,
                                                  retryable=False,
                                                  end_cause=EndCause.PUBLISH_REFUSED)
        except Exception as exc:  # noqa: BLE001 - an outage of the store, retried
            return self._fail_verdict_unpublished(
                f"verdict_unpublished: the verdict could not be written ({type(exc).__name__})",
                summary, retryable=True, end_cause=EndCause.OUTPUTS_MISSING,
            )
        summary["verdict_published"] = {"key": key, "result": result}
        self.log.info("review verdict published", key=key, result=result)
        return None

    def _fail_verdict_unpublished(
        self, reason: str, summary: dict[str, Any], *, retryable: bool, end_cause: EndCause
    ) -> Outcome:
        error = str(self._scrub(reason[:4000]))
        summary["verdict_published"] = {"refused": error}
        self.log.error("the review's verdict was not published", reason=error)
        if retryable:
            state = self._fail_retryably_terminal(
                exit_code=0, error=error, cause="verdict_unpublished",
                result_summary=summary, end_cause=end_cause,
            )
            return Outcome(exit_code=ExitCode.FAILED, state=state)
        self._finish_terminal(state=TaskState.FAILED, exit_code=0, error=error,
                              result_summary=summary, end_cause=end_cause)
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _carry_parked_uploads(self, summary: dict[str, Any]) -> None:
        """List what earlier PARKED attempts uploaded and this one did not (archived issue 166).

        Owner decision, 2026-09-28. A park uploads what the agent wrote so far
        and the next attempt resumes the work; but a retry starts with an empty
        artifacts folder (only `work/` is checkpointed), so a file written
        before the park and not again was absent from the finishing attempt's
        manifest, and its expected-output check and its dependants' staging
        both failed with the object sitting in the bucket.

        BY REFERENCE: `{name, bytes, uri, carried_from}`, the parked attempt's
        own object. Nothing is downloaded into the workspace or uploaded again.
        A name this attempt uploaded itself wins, and among parked attempts the
        later park wins. Each park recorded its uploads in its own fenced
        transaction, and they are read by id, never by query: the tenant
        worker role cannot list (`ControlPlane.parked_uploads`).

        AN ENTRY IS DATA, CHECKED BEFORE IT IS LISTED: its `uri` must name the
        object `artifacts/<name>` of the very attempt that parked, under this
        tenant's prefix for THIS task -- the shape the dependant's staging
        requires anyway (`inputs.artifact_key`) -- with a whole number of
        bytes, and the object must still exist. Anything else is dropped and
        logged. Never raises: a carry that cannot be read leaves the manifest
        as this attempt uploaded it, and the check then says what is missing.
        """
        cfg = self.cfg
        try:
            parks = self.control.parked_uploads()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not read the earlier parked attempts' uploads; only this "
                "attempt's own are listed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        artifacts = summary.get("artifacts")
        if not isinstance(artifacts, list):
            return
        own = {entry.get("name") for entry in artifacts if isinstance(entry, dict)}
        prefix = f"tenants/{cfg.tenant_id}/tasks/{cfg.task_id}/attempts/"
        carried: dict[str, dict[str, Any]] = {}
        dropped = 0
        for attempt_id, entries in parks:
            for entry in entries:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name or name in own:
                    continue
                uri = entry.get("uri")
                size = entry.get("bytes")
                key = f"{prefix}{attempt_id}/artifacts/{name}"
                if (
                    not isinstance(uri, str)
                    or not uri.endswith(key)
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                ):
                    dropped += 1
                    continue
                carried[name] = {
                    "name": name, "bytes": size, "uri": uri, "carried_from": attempt_id,
                }
        present: list[dict[str, Any]] = []
        for name, entry in carried.items():
            try:
                exists = self.store.exists(f"{prefix}{entry['carried_from']}/artifacts/{name}")
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "could not confirm a parked attempt's upload still exists; not carried",
                    artifact=standalone_mod.shown(self._scrub(name)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if exists:
                present.append(entry)
            else:
                dropped += 1
        if dropped:
            self.log.warning(
                "earlier parked attempts' uploads not carried: not an artifact of "
                "the attempt that parked, or no longer in the bucket",
                count=dropped,
            )
        if not present:
            return
        artifacts.extend(self._scrub(present))
        artifacts.sort(key=lambda entry: entry["name"].split("/"))
        self.log.info(
            "listed uploads of earlier parked attempts by reference",
            count=len(present),
            attempts=sorted({entry["carried_from"] for entry in present}),
        )

    def _add_runner_block(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        """Put the runner's own report into `summary["runner"]`; the result file, or None.

        ON EVERY TERMINAL PATH WHERE THE RUNNER WROTE ITS RESULT FILE (archived issue 361):
        the runner's end (`_finalise`), a requested cancel
        (`_apply_control_signals`) and the crash path (`_safe_finish`). The
        cancel path used to skip it, so a cancelled task's summary lost the
        steps the runner reported and its spend -- the one record of how far
        a stopped run got. Called after `_upload_outputs`, which records the
        spend this block quotes.
        """
        ws = self.ws
        if ws is None:
            return None
        # Every run and swap, when there was more than one run (SWAP-RUNS).
        summary.update(_runs_summary(self._runs, self._swap_log))
        if self._spend.get("cost_estimated"):
            # The attempt's `cost_usd` includes an estimate for a run stopped
            # before it reported one (archived issue 627). Said here, at the top, because a
            # CLI runner stopped on SIGTERM may leave no result for the block
            # below to be written from.
            summary["cost_estimated"] = True
            if self._spend.get("tier_estimated"):
                summary["tier_estimated"] = True
        if self._spend.get("cost_incomplete"):
            # A session of the attempt had its usage unread: `cost_usd`, if
            # any, does not cover it (`_collect_spend`). Free-form summary
            # only -- a typed `Attempt` field would be a contract change.
            summary["cost_incomplete"] = True
        runner_result = _read_json(ws.result_path)
        if runner_result:
            # The SAME figure `_record_spend` wrote onto the attempt (it ran
            # inside `_upload_outputs`): the attempt's total across every
            # runner it started, so the summary a human reads and the typed
            # fields a query reads cannot disagree after an in-place retry.
            usage_summary = dict(self._spend)
            runner = self._scrub(
                {
                    "status": runner_result.get("status"),
                    "summary": str(runner_result.get("summary", ""))[:4000],
                    "output": _truncate_json(runner_result.get("output"), 8000),
                    # Extracted BEFORE the line above discards it. See
                    # _usage_summary: the truncation dropped token counts on
                    # precisely the most expensive runs.
                    "usage": usage_summary,
                    "metrics": runner_result.get("metrics") or {},
                }
            )
            # NO LONE SURROGATE REACHES THE SUMMARY (archived issue 227). The agent CLI's
            # JSON can carry a `\ud800` escape, which parses to a lone
            # surrogate a Firestore write cannot carry: `finish` raised, and
            # every retry read the same output. Each is replaced with U+FFFD,
            # here, the one place every field above is written from, and
            # counted, so a reader knows the text is not quite what the agent
            # printed.
            runner, replaced = redact_mod.replace_lone_surrogates(runner)
            if replaced:
                runner["surrogates_replaced"] = replaced
                self.log.warning(
                    "the runner's result held lone surrogates, which a Firestore "
                    "write cannot carry; each was replaced with U+FFFD",
                    count=replaced,
                )
            summary["runner"] = runner
        # BROWSER-USE-FLAG: read off the runner's own result, before the
        # truncation above could have cut it from `runner.output`.
        # The seconds are the runner's own duration where `_finalise` has
        # set it, else how long the agent child has been running (a cancel,
        # a crash).
        output = runner_result.get("output") if runner_result else None
        seconds = summary.get("duration_seconds")
        if not isinstance(seconds, (int, float)) and self._child is not None:
            seconds = self._child.elapsed_seconds
        summary.update(browser_use_record(
            bool(getattr(self.handoff, "live", False)),
            output.get(BROWSER_USE_SUMMARY_KEY) if isinstance(output, dict) else None,
            seconds if isinstance(seconds, (int, float)) else None,
        ))
        return runner_result

    # ------------------------------------------------------------------
    # pieces
    # ------------------------------------------------------------------
    def _exit_fenced(self, exc: FencedError) -> int:
        """Emit the event and leave. No workspace, no agent, no lease change."""
        self._fenced_exit = True
        self.mcp.mark_fenced()
        self.log.error(
            "FENCED: this attempt has been superseded; exiting without running the agent",
            expected_generation=exc.expected,
            observed_generation=exc.actual,
            reason=str(exc),
        )
        try:
            self.control.emit(
                EventType.GENERATION_FENCED,
                {
                    "expected_generation": exc.expected,
                    "observed_generation": exc.actual,
                    "reason": str(exc),
                    "phase": "startup",
                },
            )
        except Exception as emit_exc:  # the exit code is the real signal  # noqa: BLE001
            self.log.warning("could not record the fencing event", error=str(emit_exc))
        return ExitCode.GENERATION_FENCED

    def _exit_tenant_mismatch(self, exc: TenantMismatchError) -> int:
        """Stop, having written nothing. Not even an event.

        A control-plane document names a tenant that is not this attempt's, which
        is either corruption or another tenant writing into these documents.
        Either way the worker must not touch them: `emit` would create an event
        under another tenant's task, `finish` would rewrite their state and
        `release_lease` would decrement pools their live attempt is holding. So
        this path logs to stdout -- the one channel that is this pod's own -- and
        exits. The reconciler is the backstop: the lease stops heartbeating and
        is reclaimed by the component that is allowed to act across tenants.

        `_cleanup` still runs after this, in `run()`'s `finally`, and it records
        spend on every other exit. The flag is what stops it here.
        """
        self._writes_forbidden = True
        self.log.error(
            "TENANT MISMATCH: a control-plane document belongs to another tenant; "
            "exiting without writing anything",
            kind=exc.kind,
            document_id=exc.document_id,
            expected_tenant=exc.expected,
            actual_tenant=exc.actual,
        )
        return ExitCode.TENANT_MISMATCH

    def _control_plane_read_failed(
        self, attempt: int, exc: BaseException, retry_in: float
    ) -> None:
        """One WARNING for a generation check that failed and will be asked again (archived issue 198).

        The incident's worker was silent for the 28 s between its phase line
        and its verdict. Each failed attempt now says what it met and when
        the next one starts. The last attempt's failure is the verdict, logged
        by `_exit_control_plane_unreachable`, not here.
        """
        cause = getattr(exc, "cause", None)
        self.log.warning(
            f"could not read the control plane at startup (attempt {attempt} of "
            f"{len(CONTROL_PLANE_READ_SCHEDULE_SECONDS)}): {type(exc).__name__}; "
            f"retrying in {retry_in:g}s",
            phase=self.phases.current,
            attempt=attempt,
            attempts=len(CONTROL_PLANE_READ_SCHEDULE_SECONDS),
            retry_in_seconds=retry_in,
            schedule_seconds=list(CONTROL_PLANE_READ_SCHEDULE_SECONDS),
            error_type=type(exc).__name__,
            error=_one_line(exc),
            cause=f"{type(cause).__name__}: {_one_line(cause)}" if cause is not None else None,
        )

    def _exit_control_plane_unreachable(
        self, exc: BaseException, *, attempts: int = 1, seconds: float | None = None
    ) -> int:
        """The generation check could not read Firestore. Say so and leave.

        Nothing is written to Firestore. Firestore is the thing that could not
        be read, and without the generation check this attempt does not know
        that it owns the task, so even a write that could land would not be
        its to make. See `__main__` for what the reconciler does with each
        exit.

        WHICH EXIT depends on what Firestore said:

          * A named REFUSAL (`_refusal_cause`), with nothing in the chain that
            says the service could not be reached: PERMISSION_DENIED,
            UNAUTHENTICATED, NOT_FOUND, INVALID_ARGUMENT, FAILED_PRECONDITION,
            a credential google-auth would not refresh or could not find. The
            service was reached and said no, and it will say no to the next
            attempt too. 78, "cannot start", which the reconciler fails
            without a retry (owner, 2026-09-25). Its cause goes to the
            Kubernetes termination message, the one place this worker can
            leave it that the reconciler reads.
          * EVERYTHING ELSE is 69, which the reconciler retries: UNAVAILABLE,
            a spent startup budget, any 5xx or gRPC UNKNOWN, CANCELLED, a
            google-auth transport error (`_unavailable_cause`), and an API
            error of any kind the refusal list does not name.

        The refusals are named, not the outages. The first version did it
        the other way round: 69 for a named list of outages, 78 for the rest.
        So gRPC UNKNOWN, which Firestore raises when a stream is reset under
        it, failed the task for good (review of archived pull request 59). A wrong 69 costs one
        bounded retry, and a wrong 78 ends a task that would have run.

        Before 2026-09-25 both were 78, and nothing read the 78, so the
        difference cost nothing. Once 78 fails the task, a Firestore outage
        at startup would have ended the task for good.

        The error is logged whole, with its type and its cause. Before archived pull request 57,
        every one of these looked like 300 s of nothing.

        A 69 comes after every attempt of `CONTROL_PLANE_READ_SCHEDULE_SECONDS`
        failed (archived issue 198); `attempts` and `seconds` say how many, and over how
        long. A refusal is not asked again, and comes from the first.
        """
        options = self.control.startup_call_options
        retry = options.get("retry")
        cause = getattr(exc, "cause", None)
        transient = _unavailable_cause(exc)
        refusal = _refusal_cause(exc) if transient is None else None
        exit_code = ExitCode.CONFIG if refusal is not None else ExitCode.UNAVAILABLE
        message = "could not read the control plane at startup; exiting without writing anything"
        self.log.exception(
            message,
            exc,
            phase=self.phases.current,
            seconds_in_phase=self.phases.seconds_in_phase(),
            cause=f"{type(cause).__name__}: {cause}" if cause is not None else None,
            retry_budget_seconds=getattr(retry, "timeout", None),
            call_timeout_seconds=options.get("timeout"),
            attempts=attempts,
            seconds=seconds,
            schedule_seconds=list(CONTROL_PLANE_READ_SCHEDULE_SECONDS),
            exit_code=exit_code,
            retryable=exit_code == ExitCode.UNAVAILABLE,
            refused_by=type(refusal).__name__ if refusal is not None else None,
            hint=_unreachable_hint(exc) if refusal is None else None,
            then=(
                "the reconciler reads this execution's exit code once it has ended "
                "and requeues the task, or fails it when its attempts are spent"
                if refusal is None
                else "the reconciler reads this execution's exit code and fails the task"
            ),
        )
        if refusal is not None:
            write_termination_message(
                message=message,
                cause=(
                    "the control plane refused the generation check: "
                    f"{type(refusal).__name__}: {refusal}"
                ),
                phase=self.phases.current,
                exit_code=exit_code,
                execution=_execution_name(),
            )
        return exit_code

    def _exit_interrupted_before_runner(self, exc: StartupInterrupted) -> int:
        """A SIGTERM while the runner was being prepared: record it on OUR attempt, leave.

        WHAT IS NOT WRITTEN: the task, the lease, the pools and the event
        stream. That holds whether this attempt is fenced or not, and it is why
        the fence does not need checking here. The two senders are the ones
        `_handle_interruption` describes:

          * The reconciler deleting the Job. It fences first, so this attempt
            owns nothing, and archived pull request 49's rule applies: the stale worker writes
            only its own attempt document.
          * The platform taking the instance away. This attempt still owns
            its task, but no agent has run, so there is nothing to checkpoint.
            A SCHEDULED_RETRY park is promoted by nothing
            (docs/incidents/2026-09-24-gke-dispatch.md), so parking here would
            strand the task. RUNNING -> READY is a legal transition, and the
            worker could requeue the task itself. It would then have to apply
            the retry cap that the reconciler applies (`retries_exhausted`), and
            make a fenced transition plus a lease release inside the SIGTERM
            grace, against a Firestore that may be the reason it is stuck. The
            reconciler already does all of that once the lease falls silent:
            it fences the attempt, releases the lease, and requeues the task or
            fails it when its attempts are spent. What this costs is one
            `heartbeat_grace_seconds` with the slot held.

        WHAT IS WRITTEN: this attempt's own document, with the phase, under the
        startup budget, so that a Firestore that cannot be reached costs
        seconds of the SIGTERM grace, not all of it. That write is inside
        invariant 5, as in `_record_fenced_end`. The window only opens once
        step 1 has checked that document's tenant, and `record_attempt_start`
        then wrote it with this tenant's id. `_cleanup` then gives back a pool
        account, if one was leased, and removes the workspace.

        `replaced_by` is logged when a library raised something else in the
        interrupt's place on the way up (see `_execute`). It is how an
        operator tells a rollback that failed from one that was not needed.
        """
        replaced_by = getattr(exc, "replaced_by", None)
        self.log.warning(
            f"{exc.signal_name} before the runner started; exiting without writing "
            "the task, the lease or an event",
            signal=exc.signal_name,
            phase=exc.phase,
            seconds_in_phase=self.phases.seconds_in_phase(),
            seconds_since_start=self.phases.seconds_since_start(),
            exit_code=EXIT_INTERRUPTED,
            replaced_by=(
                f"{type(replaced_by).__name__}: {replaced_by}" if replaced_by is not None else None
            ),
            then=(
                "a task still DISPATCHED or STARTING is requeued by the reconciler "
                "once it sees this execution has ended, and a RUNNING one once its "
                "lease is silent; either is failed when its attempts are spent"
            ),
        )
        self._record_startup_end(
            EXIT_INTERRUPTED,
            f"interrupted by {exc.signal_name} during startup phase {exc.phase}; "
            "the runner never started",
        )
        return EXIT_INTERRUPTED

    def _exit_unavailable_before_runner(self, exc: Exception, cause: BaseException) -> int:
        """A dependency was unavailable before the runner started: leave the task, exit 69.

        69, not 78. It was 78 until 2026-09-25, when the reconciler began
        reading exit codes and failing a 78 without a retry. An outage is the
        one thing here that the next attempt may not meet, so it keeps the
        retry it always had.

        `cause` is the error in `exc`'s chain that says so (`_unavailable_cause`).
        It is usually `exc` itself: a RetryError from the startup budget, or
        UNAVAILABLE. It is further down the chain when a library raised
        something else on the way up, such as Firestore's rollback of a
        transaction whose begin had failed.

        WHY NOT FAIL THE TASK. FAILED is terminal. `finish` sets completed_at
        and releases the lease, and the reconciler never requeues a FAILED
        task, so one Firestore outage longer than the 30 s budget would end
        the task for good without `max_attempts` being consulted. Before the
        startup budget, the same reads retried for 60 to 300 s and would have
        ridden most such outages out. Exiting as step 1 does leaves the task
        where the reconciler can see it: the lease falls silent, the reconciler
        fences and releases it, and requeues the task or fails it once its
        attempts are spent.

        WHY NOT REQUEUE IT HERE. The same reason as the interrupted exit: a
        fenced transition, the retry cap and a lease release, made against
        the service that just failed. The reconciler already does all three.

        WHAT IS WRITTEN: only this attempt's own document, best effort and
        under the budget, as in `_exit_interrupted_before_runner`. When the
        unavailable dependency was not Firestore (the checkpoint store, Secret
        Manager), it records why the attempt ended where an operator will look.
        """
        options = self.control.startup_call_options
        retry = options.get("retry")
        phase = self.phases.current
        self.log.exception(
            f"{type(cause).__name__} before the runner started; exiting without "
            "writing the task, the lease or an event",
            exc,
            phase=phase,
            seconds_in_phase=self.phases.seconds_in_phase(),
            unavailable=f"{type(cause).__name__}: {cause}",
            retry_budget_seconds=getattr(retry, "timeout", None),
            call_timeout_seconds=options.get("timeout"),
            exit_code=ExitCode.UNAVAILABLE,
            then=(
                "a task still DISPATCHED or STARTING is requeued by the reconciler "
                "once it sees this execution has ended, and a RUNNING one once its "
                "lease is silent; either is failed when its attempts are spent"
            ),
        )
        self._record_startup_end(
            ExitCode.UNAVAILABLE,
            f"{type(cause).__name__} during startup phase {phase}; the runner never "
            f"started: {cause}",
        )
        return ExitCode.UNAVAILABLE

    def _record_startup_end(self, exit_code: int, error: str) -> None:
        """This attempt's own end, best effort, under the startup budget. Never raises."""
        if self._writes_forbidden:
            return
        try:
            with self.control.startup_budget():
                self.control.record_attempt_end(
                    exit_code=exit_code, error=self._scrub(error[:4000])
                )
        except Exception as write_exc:  # noqa: BLE001
            self.log.warning(
                "could not record the end of this attempt on its own document",
                error=f"{type(write_exc).__name__}: {write_exc}",
            )

    def _start_issue_prefetch(self, task: dict[str, Any]) -> None:
        """Start the issue fetch in a thread, beside the clone (archived issue 721, P29). Never raises.

        Owner decision 2026-10-06 (chunk-3 observer P29): the fetch needs only
        the token and the issue number, and both are known here, before the
        clone. Started only when the task asks for an issue and names a
        repository the clone will accept; in every other case nothing starts
        and STEP 6b fetches, or refuses, exactly as it always did -- so a
        refusal that never needed the forge (no repository, an input the
        profile refuses) is still raised there and nowhere else.

        The token is read here, by the same `_git_token` the fetch always
        used, and only when an issue is asked for: a task without one reads
        no extra secret. The URL is the one `_maybe_clone` settles on
        (`validate_repository_url` of the same field it reads), so the join
        uses this fetch; a different one is dropped there.
        """
        try:
            number = issue_mod.requested(task.get("input"), self.cfg.profile)
            raw = self.cfg.repository_url or task.get("repository_url")
            if number is None or not raw:
                return
            try:
                url = gitops_mod.validate_repository_url(raw)
            except GitError:
                return
            self.phases.enter("issue_prefetch", issue=number)
            prefetch = issue_mod.IssuePrefetch(
                number=number,
                repository_url=url,
                token=self._git_token(),
                retry=self._forge_retry(),
                clock=self.phases.seconds_since_start,
            )
            prefetch.start()
            self._issue_prefetch = prefetch
        except Exception as exc:  # noqa: BLE001 -- STEP 6b fetches as before
            self.log.warning(
                "the issue fetch could not start beside the clone; fetching it later",
                error=type(exc).__name__,
            )

    def _clone_keeping_lease(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """`_maybe_clone`, with the lease heartbeated from a thread meanwhile (archived issue 742).

        MEASURED 2026-10-06: the worker beat before the clone and after it,
        never during. A clone that hung about 134 s before its retry landed
        got a healthy attempt reclaimed as `dead_worker` 2.2 s after its agent
        started, and six chunk-3 steps went 82-92 s between beats around the
        clone against the reconciler's 90 s grace. The block covers the egress
        probe's wait (`gitops.await_egress`, up to its 80 s cap, archived pull request 734) and
        every clone try and the waits between them (`gitops.retry_clone`).

        `_heartbeat_meanwhile`, the one the checkpoint uses, so FENCING IS
        UNCHANGED (invariant 5): it asks the control plane before every beat
        and stops at once when another generation owns the task or the lease
        is released, so it only ever extends a lease this generation holds. A
        stale generation never gets here: `_prepare` runs after step 1's
        generation check, which exits without touching the lease.
        """
        with self._heartbeat_meanwhile("clone"):
            return self._maybe_clone(task)

    def _start_test_services(self) -> None:
        """Run what the clone's `.swarm/test-services.toml` declares, for the
        whole attempt (TS-RUN, archived issue 1627; docs/design/test-services.md §5.4, Q4).

        After the clone, so the declaration read is the checkout's, and
        before the agent, so its own test runs reach `127.0.0.1:5432`.
        Stopped, its data removed, by `_stop_test_services` in `_cleanup`,
        which every exit reaches: finish, park, cancel, fence (§5.5).

        NEVER ENDS THE ATTEMPT. No file is the default and starts nothing;
        an invalid file starts nothing (the gate reports it, §2.2); a
        service that does not come up is logged with its scrubbed error and
        the agent runs without it, as it did before; so is a worker with no
        SWARM_CACHE_ROOT, whose services are refused before `initdb`
        (`testservices.NO_CACHE_VOLUME`, the owner 2026-10-24). The lease is
        heartbeated meanwhile: the ready wait alone may take 30 s.
        Invariant 7: the server shares this container's fixed resources;
        nothing is requested or added to the pod.
        """
        ws = self.ws
        if ws is None or ws.cache is None:
            return
        repo = ws.work / REPO_DIR_NAME
        try:
            if not repo.is_dir():
                return
            declaration = testservices_mod.read_declaration(repo)
            if not declaration.valid:
                self.log.warning(
                    "test services: the declaration is invalid; nothing started",
                    refusals=len(declaration.refusals),
                )
                return
            if not declaration.declared:
                return
            self.phases.enter("test_services")
            with self._heartbeat_meanwhile("test_services"):
                running = testservices_mod.start(
                    declaration, ws.cache, scrub=lambda text: str(self._scrub(text))
                )
            self._test_services = running
            report = iter(running.report())
            for service in running.services:
                if service.error is None:
                    self.log.info(
                        "test service ready",
                        service=service.service.name,
                        version=service.service.version,
                        databases=len(service.service.databases),
                        seconds=service.ready_seconds,
                    )
                else:
                    self.log.warning(
                        "test service did not start; the agent runs without it",
                        service=service.service.name,
                        error=service.error,
                        report=next(report),
                    )
        except Exception as exc:  # noqa: BLE001 -- a test service never ends the attempt
            self.log.warning("test services did not start", error=type(exc).__name__)

    def _stop_test_services(self) -> None:
        """Stop the declared services and remove their data (§5.5). Idempotent."""
        running, self._test_services = self._test_services, None
        if running is not None:
            running.stop()

    def _clone_additional_keeping_lease(
        self, task: dict[str, Any]
    ) -> list[extrarepos_mod.Checkout]:
        """The task's additional repositories on disk (`extrarepos.prepare`), the
        lease heartbeated from a thread meanwhile, as for the primary clone.

        Raises `extrarepos.Refused` when they cannot all be put there. From the
        VERIFIED document only (`_task`, after `_verify_spec`), whose spec
        signature covers the list at format 4.
        """
        entries = extrarepos_mod.entries_of(task)
        if not entries:
            return []
        ws = self.ws
        assert ws is not None
        self.phases.enter("additional_clone", count=len(entries))
        budget = extrarepos_mod.budget_bytes(
            self.db, logger=self.log, call_options=self.control.call_options()
        )
        with self._heartbeat_meanwhile("additional_clone"):
            checkouts = extrarepos_mod.prepare(
                ws=ws,
                entries=entries,
                grant_refusal=self._additional_grant_refusal,
                token_for=self._additional_git_token,
                budget=budget,
                logger=self.log,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                max_wait_seconds=self.cfg.max_in_worker_retry_delay_seconds,
                remaining_seconds=self._remaining_seconds,
                sleep=self.forge_sleep,
                egress=self._egress,
            )
        # From now on every checkpoint leaves `work/repos` out (§4.6), and
        # the standalone upload skips it (`_workdir_reserved`).
        self.checkpoints.leaves_out_extra_repos = True
        self._extra_checkouts = checkouts
        return checkouts

    def _additional_suffix(self, entry: dict[str, Any]) -> str:
        """The forge slot an additional repository is cloned with.

        The entry's signed `forge_credential`, only once the spec signature
        was verified; an unsigned task reads the tenant token, as
        `_forge_suffix` does for the primary, so a field anyone with write on
        the document could set never chooses whose slot this worker reads.
        """
        return entry["forge_credential"] if self._forge_signed else GIT_PROVIDER

    def _additional_grant_refusal(self, entry: dict[str, Any]) -> str | None:
        """Why the submitter's grant no longer covers this entry, or None.

        `secrets.grant_refusal` for the entry's own slot and repository, read
        again by id. Nothing for a tenant token. Raises when it cannot be read.
        """
        suffix = self._additional_suffix(entry)
        if user_slot_hash(suffix) is None:
            return None
        submitted_by = (self._task or {}).get("submitted_by")
        return grant_refusal(
            self.db,
            tenant_id=self.cfg.tenant_id,
            suffix=suffix,
            repository_url=entry["repository_url"],
            write=False,
            call_options=self.control.call_options(),
            submitted_by=submitted_by if isinstance(submitted_by, str) else None,
        )

    def _additional_git_token(self, entry: dict[str, Any]) -> str | None:
        """This entry's forge token, read now, or None: `_git_token`'s path for
        the entry's own slot. Never cached, never put in an environment.

        A person's `git-u-` slot goes through `self.forge_broker`, as the
        primary's does (archived issue 1041): without it `resolve_git_token` refuses every
        user slot, and the entry cloned unauthenticated."""
        refusal = self._git_token_refusal()
        suffix = self._additional_suffix(entry)
        if refusal or (self.secret_client is None and user_slot_hash(suffix) is None):
            return None
        try:
            tenant = load_tenant(
                self.db, self.cfg.tenant_id, call_options=self.control.call_options()
            )
            return resolve_git_token(
                tenant=tenant, client=self.secret_client, logger=self.log,
                provider=suffix, broker=self.forge_broker,
            )
        except SecretError as exc:
            self.log.warning(
                "no usable git credential for an additional repository; cloning "
                "unauthenticated", name=entry["name"], error=str(exc),
            )
            return None

    def _end_additional_refused(self, exc: extrarepos_mod.Refused) -> Outcome:
        """End the attempt before the agent ran: its additional repositories
        could not all be put on disk.

        After the startup window closes, like every ending `_prepare` decides
        on. `control.finish` or `fail_retryably` writes the task's state and
        releases the lease; nothing else touches it. The reason names entries
        by `name` only.
        """
        reason = self._scrub(exc.reason)
        error = self._scrub(f"{exc.cause}: {reason}. The agent was not started.")
        self.log.error(
            "the additional repositories could not be prepared; ending before the agent runs",
            cause=exc.cause, retryable=exc.retryable,
        )
        summary = self._upload_outputs()
        summary["additional_repositories_check"] = {"cause": exc.cause, "reason": reason}
        with self._metrics_after_the_record():
            if exc.retryable:
                state = self.control.fail_retryably(
                    exit_code=None,
                    error=error,
                    cause=exc.cause,
                    result_summary=summary,
                    retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                    detail={"clone": "additional_repositories"},
                    end_cause=EndCause.CANNOT_START,
                )
                return Outcome(exit_code=ExitCode.FAILED, state=state)
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _maybe_clone(self, task: dict[str, Any]) -> dict[str, Any] | None:
        ws = self.ws
        assert ws is not None
        url = self.cfg.repository_url or task.get("repository_url")
        if not url:
            return None
        destination = ws.work / REPO_DIR_NAME
        self._repo_url = url
        # AN INDEX RUN'S CHECKPOINTS LEAVE ITS CHECKOUT OUT (INDEX-CKPT, owner
        # decision 2026-10-17): the indexer only reads it, and a resumed
        # attempt clones it again below, at the manifest's `clone_base`
        # (`indexrun.resume_clone_ref`). Set before the first checkpoint, so
        # a chain never changes what it covers. Empty for every other profile.
        self.checkpoints.excluded_dirs = indexrun_mod.checkpoint_excludes(
            self.cfg.runner_profile
        )
        if destination.exists() and any(destination.iterdir()):
            # Restored from a checkpoint that already contains the clone. The
            # base commit is read back from the marker the FIRST attempt wrote,
            # because by now the agent's own commits sit on top of it and HEAD
            # no longer answers "what did this repository look like on arrival".
            self.log.info("repository already present from checkpoint; skipping clone")
            # THE BASE THE PUSH TRUSTS COMES FROM THE MANIFEST, NOT THE TREE.
            # The marker file sits in `work/`, the agent's HOME and writable by
            # it, and travels in the archive the agent's own files travel in. An
            # agent that wrote a later commit of its own into it lifted the
            # fold's floor above its earlier commits, and those were pushed as
            # the agent wrote them. The manifest is written by the worker,
            # outside that tree. A manifest from before the worker recorded a
            # base holds none, and then nothing is published -- the marker
            # still gives the harvest its diff base, so the work is described.
            recorded = self._restored_from.clone_base if self._restored_from else None
            self._publish_base = recorded
            self.checkpoints.clone_base = recorded
            self._clone_base = (
                recorded if recorded and recorded != EMPTY_CLONE_BASE else None
            ) or self._read_clone_base()
            # The commit this step's clone started from, as the manifest the
            # worker wrote records it -- never the marker in the tree.
            self._clone_commit = (
                recorded if isinstance(recorded, str) and _FULL_SHA_RE.fullmatch(recorded) else None
            )
            # The branch the first attempt's clone landed on, from the same
            # manifest (archived issue 2094). An older manifest records none: the branch
            # check is then skipped, and the start sha still decides.
            self._start_branch = _recorded_start_branch(
                self._restored_from.start_branch if self._restored_from else None
            )
            self.checkpoints.start_branch = self._start_branch
            # The pull request's head the FIRST attempt started from (archived issue 2183),
            # so a head moved while this task was parked is still refused.
            self._pr_start_head = (
                self._restored_from.pr_start_head if self._restored_from else None
            )
            self.checkpoints.pr_start_head = self._pr_start_head
            return {
                "path": REPO_DIR_NAME,
                "from_checkpoint": True,
                "commit": self._clone_base,
            }
        # A fix step clones the branch it continues (archived issue 263, continuation.py)
        # first; failing that, a step that BUILDS ON an upstream step (archived issue 264)
        # starts from the branch that step pushed, so a fix sees the code it
        # is fixing. Either way the branch is derived from a task id with
        # this worker's own prefix, never read as a name, exactly as the
        # integrator's contributor branches are.
        #
        # A `single-pr` READER or AMENDER (archived issue 295, docs/merge-step.md §3) clones
        # the author's branch, `swarm/<pr_author>`, derived from the author's
        # task id in the signed dispatch block -- never a branch name read
        # from metadata -- and nothing below chooses for it. An author, and
        # every other strategy, is unchanged.
        pr_branch = (
            self._pr_author_branch() if self._pr_role() in ("reader", "amender") else ""
        )
        declared_builds_on = "" if pr_branch else self._dispatch_builds_on()
        # A `builds_on` step that left nothing pushed no branch: this step
        # starts from the nearest ancestor that did, or from the default
        # branch exactly as a step with no `builds_on` does (archived issue 978).
        builds_on = self._builds_on_base() if declared_builds_on else ""
        continued = "" if pr_branch else continuation_mod.clone_ref(
            task.get("metadata"), self.cfg.git_branch_prefix
        )
        # `carrier: branches` (D13): a step whose parent kept its work on a
        # branch starts from that branch, instead of from the default branch
        # with the parent's patch to apply. Asked only when nothing above
        # chose a branch, so `continues` and `builds_on` keep their meaning.
        # `publish_to_pr` (archived issue 2183, publishpr.py): the pull request is read and
        # checked BEFORE anything is cloned, and a step that would otherwise
        # clone the workflow's ref starts from its head commit instead. A
        # step that builds on an upstream step keeps that step's branch,
        # which stands on the same head; the push checks it does.
        onto_pr = self._publish_to_pr()
        pr_head = self._read_pull_at_start(url, onto_pr) if onto_pr is not None else None
        pr_start = (
            onto_pr.branch(self.cfg.git_branch_prefix)
            if onto_pr is not None and not (pr_branch or continued or builds_on)
            else ""
        )
        # A STEP'S OWN REF (archived issue 2895). swarm-api writes a step's own
        # `repository_ref` onto its task and marks it `step_ref: true` in the
        # signed dispatch block. Such a step clones that ref as a root step
        # clones the workflow's: never a parent's carrier branch, and never
        # pinned to a parent's base below -- its parents cloned another ref.
        # The API refuses it beside `builds_on`, `pr_role` and `continues`.
        step_ref = self._dispatch_block().get("step_ref") is True
        carried, carried_head = (
            ("", None) if pr_branch or continued or builds_on or onto_pr or step_ref
            else self._carrier_parent(task, url)
        )
        # A resumed index run whose checkpoint left the checkout out clones
        # again at the commit its first attempt cloned, by the same call and
        # with the token read the same way, wherever `repository_ref` has
        # moved since: the restored graph describes that commit.
        resumed_at = indexrun_mod.resume_clone_ref(
            self.cfg.runner_profile,
            self._restored_from.clone_base if self._restored_from else None,
        )
        ref = (
            resumed_at
            or pr_branch
            or continued
            or (f"{self.cfg.git_branch_prefix}{builds_on}" if builds_on else None)
            or pr_start
            or (f"{self.cfg.git_branch_prefix}{carried}" if carried else None)
            or (self.cfg.repository_ref or task.get("repository_ref"))
        )
        # THE BASE PIN (owner decision 2026-10-02, after lane B15b's lost
        # implementation; docs/workflows.md "The base pin"). A downstream step
        # that would clone `repository_ref` clones the commit its upstream
        # steps cloned instead of wherever the branch has moved since, so a
        # patch staged from them still applies. A step that starts from an
        # upstream BRANCH already starts from the upstream's work and is not
        # pinned. `_upstream_base_pin` reads nothing for a root step or a
        # non-workflow task, which is therefore unchanged, nor for a step that
        # clones its own ref (`step_ref`).
        pinned_sha: str | None = None
        if not (resumed_at or pr_branch or continued or builds_on or carried or onto_pr
                or step_ref):
            pinned_sha, self._base_pin = self._upstream_base_pin(task)
        if pr_start and not resumed_at:
            # THE HEAD THAT WAS CHECKED, NOT THE BRANCH'S TIP: whatever moved
            # the branch since the read above was not checked, and the push
            # is leased on this sha anyway.
            pinned_sha = pr_head
        if carried:
            # THE PARENT'S RECORDED HEAD, NOT THE BRANCH'S TIP (archived issue 453). The tip
            # is whatever was pushed last, and the parent's record is the one
            # thing this worker trusts about what the parent finished with: a
            # stale attempt of the parent, or anything else holding a token,
            # can move the tip after the record was written. Pinned with no
            # fall back to the tip (see the GitError below).
            pinned_sha = carried_head
        # A worker whose memory the agent may read clones WITHOUT the token, so
        # the token is never in this process at all. A public repository still
        # clones; a private one fails, and the error below says why.
        refusal = self._git_token_refusal()
        clone = None
        # A clone or fetch the forge did not answer -- a connect or read
        # timeout, DNS, a reset, a 5xx (`gitops.GitTransient`) -- is tried
        # again in this process after 10 s and 30 s, within the platform's
        # in-worker wait and the step's deadline (`gitops.retry_clone`). Past
        # that the attempt ends RETRYABLY (`_fail_clone_unreachable`), so the
        # task's attempt budget applies. A missing repository, refused
        # authentication or a bad ref is a plain `GitError` and stays
        # terminal, at once (archived issue 623).
        # Every try recorded, the pinned fetch's and the failed ones included
        # (archived issue 742): the clone's mark (`_mark_clone_timed`) lists them in order.
        tries: list[dict[str, Any]] = []
        # Which address git and the egress probe each reached, per try, and
        # the pin to the probe's after a stalled connect elsewhere (archived issue 721, P27;
        # `gitops.PeerPin`). One for the whole clone, so a pin decided on the
        # base pin's fetch carries to the branch-tip clone of the same host.
        peers = PeerPin(self._egress, url)
        # Whether each try handed git a token, latest last: an "Authentication
        # failed" from a clone that held none is git asking for a credential
        # nobody gave it, which reading the slot again cannot change.
        held: list[bool] = []

        def clone_token() -> str | None:
            token = None if refusal else self._git_token()
            held.append(bool(token))
            return token

        def retry(call: Callable[[], CloneResult]) -> CloneResult:
            def tried(again: bool) -> CloneResult:
                if again:
                    # `call` reads the token at each try, so this one reads
                    # the slot's latest version. git refuses a folder the
                    # refused try left files in.
                    gitops_mod._empty_directory(Path(destination))
                return retry_clone(
                    call,
                    destination=destination,
                    max_wait_seconds=self.cfg.max_in_worker_retry_delay_seconds,
                    remaining_seconds=self._remaining_seconds,
                    logger=self.log,
                    sleep=self.forge_sleep,
                    # Redundant under `_clone_keeping_lease`, whose thread beats
                    # meanwhile; kept so `_maybe_clone` called alone still beats
                    # between tries. At worst one beat is doubled, an unfenced
                    # refresh of a lease the thread has just checked is ours.
                    on_retry=self._heartbeat,
                    record=tries,
                )

            # A refused credential (git's "Authentication failed", a 401) is
            # read again and the clone tried once more (owner decision
            # 2026-10-08): a refresh may have ended the version first read.
            # Never when the refused try held no token -- this worker clones
            # without it, or the task has none -- and never for a missing
            # repository, which `forge_credential_refused` does not match.
            return call_reading_again(
                tried,
                logger=self.log,
                what="the clone",
                twice=lambda exc: GitError(f"{exc}; {FORGE_REFUSED_TWICE}"),
                refused=lambda exc: bool(held) and held[-1]
                and not isinstance(exc, GitTransient)
                and forge_credential_refused(exc),
            )

        # An index run reads 90 days of history (hot spots, co-change); every
        # other step stays one commit deep. Every clone is the forge's: the
        # clone bundles of archived issue 940 were retired on 2026-10-11
        # (docs/clone-bundles.md), once forge clones measured faster.
        history_days = indexrun_mod.clone_history_days(self.cfg.runner_profile)
        if pinned_sha is not None:
            try:
                clone = retry(lambda: clone_at_commit(
                    url=url,
                    branch=ref,
                    commit=pinned_sha,
                    destination=destination,
                    private_dir=ws.private,
                    logs_dir=ws.logs,
                    timeout_seconds=self.cfg.git_clone_timeout_seconds,
                    logger=self.log,
                    token=clone_token(),
                    egress=self._egress,
                    peers=peers,
                    # archived issue 2901: the pull request's head is cloned through this pin.
                    pull_base=self._pr_base_ref if pr_start else None,
                ))
            except GitTransient as exc:
                # Not a fall back to the branch tip: the tip is on the same
                # forge, and an unpinned clone would be a silent change of base.
                self._mark_clone_timed(None, tries=tries, pinned=True)
                self._mark_egress_ready()
                raise _CloneUnreachable(self._scrub(str(exc)[-600:]), exc.tries) from exc
            except GitError as exc:
                if pr_start and pinned_sha == pr_head:
                    self._mark_clone_timed(None, tries=tries, pinned=True)
                    self._mark_egress_ready()
                    raise WorkerError(
                        f"repository clone failed: this step starts from pull request "
                        f"#{onto_pr.number if onto_pr else ''}'s head {pinned_sha} on "
                        f"{ref}, and that commit could not be fetched: "
                        f"{self._scrub(str(exc)[:500])}"
                    ) from exc
                if carried:
                    # Never the tip instead: the tip is exactly what the pin
                    # refuses to trust. The parent's head is not on the
                    # branch any more, which only a force push or a deleted
                    # branch does -- neither of them this platform's.
                    self._mark_clone_timed(None, tries=tries, pinned=True)
                    self._mark_egress_ready()
                    raise WorkerError(
                        f"repository clone failed: this step starts from task "
                        f"{carried}'s branch {ref} at the head that step recorded, "
                        f"{pinned_sha}, and that commit could not be fetched: "
                        f"{self._scrub(str(exc)[:500])}"
                    ) from exc
                # Not the end of the step: the branch tip is what every step
                # started from before the pin existed. Said, so a patch that
                # then fails to apply has its cause on record.
                self.log.warning(
                    "base pin: the upstream steps' base could not be fetched; "
                    "cloning the branch tip instead",
                    ref=ref, base=pinned_sha, parents=(self._base_pin or {}).get("from"),
                    error=self._scrub(str(exc)[:500]),
                )
                self._base_pin = {"pinned": False, "reason": "fetch_failed"}
        pinned_clone = clone is not None
        try:
            clone = clone or retry(lambda: shallow_clone(
                url=url,
                ref=ref,
                destination=destination,
                # The worker's own scratch directory, NOT `ws.tmp`: `ws.tmp` is
                # what the agent is handed as TMPDIR, and a credential file left
                # there is one `cat $TMPDIR/.git-credentials` away from any
                # prompt injection in the repository being cloned.
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                logger=self.log,
                token=clone_token(),
                egress=self._egress,
                peers=peers,
                history_days=history_days,
                pull_base=self._pr_base_ref if onto_pr is not None else None,
            ))
        except GitTransient as exc:
            self._mark_clone_timed(None, tries=tries, pinned=False)
            self._mark_egress_ready()
            raise _CloneUnreachable(self._scrub(str(exc)[-600:]), exc.tries) from exc
        except GitError as exc:
            self._mark_clone_timed(None, tries=tries, pinned=False)
            self._mark_egress_ready()
            based = (
                f"; this step builds on task {builds_on}, whose branch {ref} is "
                "pushed when that step publishes -- it was not found or could "
                "not be fetched"
                if builds_on
                else f"; this step reads the pull request's branch {ref}, which its "
                "author pushes when it publishes -- it was not found or could not "
                "be fetched"
                if pr_branch
                else ""
            )
            if refusal:
                raise WorkerError(
                    f"repository clone failed: {exc}{based}; cloned without the "
                    f"tenant git token because {refusal}"
                ) from exc
            raise WorkerError(f"repository clone failed: {exc}{based}") from exc
        self._mark_clone_timed(clone, tries=tries, pinned=pinned_clone)
        self._mark_egress_ready()
        self._repo_url = clone.url
        self._clone_base = clone.commit
        # `result_summary.git.clone_commit` (merge-step.md §3): the only record
        # of which head a reader actually saw. Known in this process.
        self._clone_commit = clone.commit
        # `fetch_pull_base`'s record, for the git tools' `too_large` (archived issue 2901 §5.8).
        self._pull_base = clone.base if isinstance(clone.base, dict) else None
        # Known in this process, so trusted; and recorded in every checkpoint
        # manifest from here on, so a resumed attempt can trust it too.
        self._publish_base = clone.commit or (EMPTY_CLONE_BASE if clone.empty else None)
        self.checkpoints.clone_base = self._publish_base
        try:
            self._start_branch = read_head_branch_as_data(Path(destination))
        except GitError as exc:
            # Unknown, not refused: the start sha still decides (`_head_moved`).
            self.log.warning("could not read the branch the clone landed on", error=str(exc))
        # Into every manifest from here on, so a resumed attempt knows it too.
        self.checkpoints.start_branch = self._start_branch
        self._pr_start_head = pr_head
        self.checkpoints.pr_start_head = pr_head
        # A `publish_to_pr` step is never rebased onto the default branch:
        # its push must fast-forward the pull request's head (archived issue 2183).
        self._prepublish_rebase = self._prepublish_rebase_ref(
            ref=ref,
            checkout=Path(destination),
            fixed_base=bool(pr_branch or continued or builds_on or carried or onto_pr)
            or pinned_sha is not None,
        )
        self._write_clone_base(clone.commit)
        info: dict[str, Any] = {
            "path": REPO_DIR_NAME,
            "url": clone.url,
            "ref": clone.ref,
            "commit": clone.commit,
        }
        if declared_builds_on:
            info["builds_on"] = declared_builds_on
        if self._builds_on_resolved is not None and self._builds_on_resolved[1]:
            info["builds_on_resolved"] = {
                "task_id": builds_on or None,
                "left_nothing": list(self._builds_on_resolved[1]),
            }
        if pr_branch:
            info["pr_role"] = self._pr_role()
        if carried:
            info["carried_from"] = carried
            info["carried_head"] = carried_head
        if onto_pr is not None:
            info["publish_to_pr"] = {
                **onto_pr.to_record(self.cfg.git_branch_prefix), "head": pr_head,
            }
            if clone.base is not None:
                info["publish_to_pr"]["base"] = clone.base
        if self._base_pin is not None:
            info["base_pin"] = dict(self._base_pin)
        if refusal:
            info["git_token_refused"] = refusal
        return info

    def _prepublish_rebase_ref(
        self, *, ref: str | None, checkout: Path, fixed_base: bool
    ) -> str | None:
        """The branch the runner may rebase this lane onto before publishing, or None.

        SPEED-D1 (owner decision 2026-10-14): 128 of 150 merged pull requests
        needed an update from main, because a lane's branch is cut from the
        commit it started on. The runner rebases onto the base's CURRENT tip
        through the git tools (`agent_worker.prepublish`), and the publish
        then diffs from where it landed (`_adopt_rebase_base`). Only where a
        moved base breaks nothing:

        * an AUTHOR of its own pull request -- `direct-pr`, or `single-pr`'s
          author. `collect` stages its patch onto a downstream step's pinned
          base, an amender only fast-forwards the author's branch, an
          integrator merges contributors onto their base;
        * cloned from the repository's branch, not from a continued, built-on,
          carried or author's branch (`fixed_base`), whose pushed tip a
          rewritten history would no longer fast-forward, and not pinned to an
          upstream step's base, which a downstream patch must apply to;
        * no `carrier: branches`, whose checkpoints already pushed the branch;
        * a BRANCH: a ref that is a sha has no newer tip. A full refname
          (archived issue 3347) is a branch only as `refs/heads/<name>`, offered as
          `<name>`, since the runner rebases onto `origin/<name>`; a pull
          request's `refs/pull/<n>/head` or `/merge`, a tag, or any other
          ref names no branch to rebase onto, and is offered nothing.

        The default branch, when the task names no ref, is read from the
        clone's own HEAD file, which this process wrote before any agent ran.
        """
        if fixed_base or self._dispatch_carrier() == "branches":
            return None
        role = self._pr_role()
        if role:
            if role != "author":
                return None
        elif self._dispatch_strategy() != "direct-pr":
            return None
        name = (ref or "").strip()
        if not name:
            try:
                head = (checkout / ".git" / "HEAD").read_text(encoding="utf-8").strip()
            except OSError:
                return None
            prefix = "ref: refs/heads/"
            name = head[len(prefix):] if head.startswith(prefix) else ""
        if is_full_refname(name):
            if not name.startswith("refs/heads/"):
                return None
            name = name[len("refs/heads/"):]
        if not name or re.fullmatch(r"[0-9a-f]{7,40}", name):
            return None
        try:
            validate_ref(name)
        except GitError:
            return None
        return name

    def _adopt_rebase_base(self, publish_repo: Path, floor: str | None) -> str | None:
        """The commit the publish diffs from after a pre-publish rebase, or None.

        The runner rebased the lane onto the base's newer tip
        (`agent_worker.prepublish`), so `floor..HEAD` holds every commit the
        base gained since the clone; replayed as the lane's, they would be
        republished under the worker's name. The commit the rebase landed on
        is a sha the git tools' relay fetched itself (`GitTools.rebased_onto`),
        so origin has it, and it becomes the publish base when, in the
        worker's own clean repository, the floor is its ancestor and HEAD
        descends from it -- an aborted or undone rebase fails the second test
        and the floor stands.
        """
        onto = self.children.rebased_onto
        base = (floor or "").strip()
        if not onto or not _FULL_SHA_RE.fullmatch(onto) or not _FULL_SHA_RE.fullmatch(base) \
                or onto == base:
            return None
        ws = self.ws
        assert ws is not None
        g = ["git", *_NO_HOOKS]
        for older, newer, slug in ((base, onto, "publish-rebase-floor"),
                                   (onto, "HEAD", "publish-rebase-head")):
            code, _ = _git_text(
                [*g, "merge-base", "--is-ancestor", older, newer],
                repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs, slug=slug,
                timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
            )
            if code != 0:
                self.log.warning(
                    "the lane was rebased but its head no longer stands on that base; "
                    "the publish diffs from the clone base",
                    onto=onto, base=base, failed=slug,
                )
                return None
        self.log.info("the lane was rebased before publishing; the publish diffs from "
                      "where it landed", onto=onto, base=base)
        return onto

    def _head_moved(self, repo: Path, publish_repo: Path, floor: str | None) -> str | None:
        """Where HEAD went when it left this step's work, or None when it did not (archived issue 2094).

        MEASURED 2026-10-23 (peer-app archived pull requests 456 and 465): two review
        steps checked out the branch they reviewed and committed nothing; the
        harvest counted that branch's commits since the clone base as theirs
        and published them. HEAD has moved when, at the end:

        * it is on another branch than the one the clone left it on
          (`_start_branch`, read before any agent process existed, or read
          back from the checkpoint manifest on a resume) AND that branch is
          one the agent FETCHED rather than made (`_fetched_line`) -- the
          branch's name is returned; or
        * it is DETACHED exactly on a `refs/remotes/origin/*` tip, whatever
          the start was -- a branch, or detached itself (a by-sha
          `repository_ref`, a resumed index run) -- unless that commit is
          `floor` itself or the tip of this step's own branch -- the head's
          short sha is returned (the owner, 2026-10-23 ~18:39Z, recorded on
          task_bf208a4d46f04764869d q1: "Yes, close it." A review cloned by
          sha that ran `git checkout --detach origin/<reviewed>` changed no
          branch, and its line descends from the start); or
        * it does not descend from `floor`, the commit the step started on
          (the start sha, or where the runner's pre-publish rebase landed,
          `_adopt_rebase_base`), nor from the tip this attempt's own
          `git_push_branch` left on the task's branch (`_adopt_tool_pushed_tip`,
          archived issue 1188) -- the branch's name is returned when HEAD changed branch
          (detached counts as a branch of its own),
          the head's short sha otherwise.

        RELAXED FOR A NEW LOCAL BRANCH (the owner, 2026-10-23 16:47Z, recorded on
        task_306e3aa8dee141a5a390 q0). All five implement agents sampled that
        day ran `git checkout -b <name>` from their start and committed there,
        and PR 2107's rule -- any other branch is a move -- refused them all. A
        branch change is therefore NOT a move when the end branch has no
        `refs/remotes/origin/<name>` in the clone (the clone tracks every
        branch, `gitops.TRACK_EVERY_BRANCH`, so a branch fetched by name has
        one) and HEAD descends from the start, the last rule above. A name
        collision alone is not a move either (archived issue 3435): task_884a993427ce44feb7a6
        ran `git checkout -b rebuild/pr-1233` at its start, committed a merge
        and two fixes, then a `git fetch --deepen=50 origin` wrote an unrelated
        `refs/remotes/origin/rebuild/pr-1233`, and its 3 commits were lost. The
        branch is fetched only when its line HOLDS that tip -- the tip is an
        ancestor of HEAD and not of `floor` -- and otherwise falls to the
        descent check. A
        detached HEAD, from any start, therefore publishes in exactly two
        cases: it is the start sha (`floor`) itself, whether or not an origin
        tip sits on that commit; or it descends from the start and equals no
        `refs/remotes/origin/*` tip but this step's own branch's -- a by-sha
        step's own commits on top of its start.

        THE KNOWN LIMIT THE OWNER ACCEPTED. A line fetched without a tracking ref
        reads as the agent's own: a reviewed pull request fetched BY SHA (or
        into FETCH_HEAD) and checked out, detached or onto a fresh local
        branch, a branch made with `checkout -b mine origin/<reviewed>`, a
        detached HEAD with a commit on top of a fetched tip, or a tracking ref
        the agent deleted. Each descends from the start sha, so each publishes
        the reviewed commits as this step's. Nothing in the clone tells those
        apart from an agent's own work, and the clone is the agent's to write.

        Every fact about the clone is read from its files as data
        (`read_head_branch_as_data`, `read_origin_ref_as_data`,
        `origin_tips_as_data`): no git runs there, so none of the agent's
        config or hooks does. The ancestry is asked in the worker's own
        `publish_repo`. An empty repository's clone has no start sha, and a
        resume from a manifest without `start_branch` knows no start branch:
        each check runs only where its fact is known. Raises `GitError` when
        a fact cannot be read, and the harvest then publishes nothing.
        """
        ws = self.ws
        assert ws is not None

        def git(argv: list[str], slug: str) -> tuple[int, str]:
            return _git_text(
                ["git", *_NO_HOOKS, *argv], repo=Path(publish_repo), private_dir=ws.private,
                logs_dir=ws.logs, slug=slug,
                timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
            )

        def short_head() -> str:
            code, head = git(["rev-parse", "--verify", "--quiet", "HEAD"], "harvest-moved-head")
            return head.strip()[:12] if code == 0 and head.strip() else "an unborn HEAD"

        def where() -> str:
            return short_head() if end == DETACHED_HEAD else end.removeprefix("refs/heads/")

        def contains(tip: str | None) -> bool:
            # The agent's line holds the fetched tip (archived issue 3435): HEAD's history is
            # fetched into publish_repo, so a tip absent there is not in it.
            # A tip that is not a full sha stays fetched (fail closed).
            if not isinstance(tip, str) or not _FULL_SHA_RE.fullmatch(tip):
                return True
            asked = [
                (["rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}"], "harvest-moved-fetched", True),
                (["merge-base", "--is-ancestor", tip, "HEAD"], "harvest-moved-fetched-head", True),
            ]
            if floor and _FULL_SHA_RE.fullmatch(floor):
                asked.append((["merge-base", "--is-ancestor", tip, floor], "harvest-moved-fetched-start", False))
            for argv, slug, want in asked:
                code, _ = git(argv, slug)
                if code not in (0, 1):
                    raise GitError("could not tell whether HEAD's line holds the branch it fetched")
                if (code == 0) != want:
                    return False
            return True

        end = read_head_branch_as_data(repo)
        start = self._start_branch
        left_branch = start is not None and end != start
        if end == DETACHED_HEAD:
            # Whatever the start was: a clone by sha starts detached, so
            # detaching onto a fetched tip changes no branch (the owner, 2026-10-23).
            head = read_head_as_data(repo)
            own = self._publish_branch_name()
            fetched = (
                head is not None
                and head != floor
                and not (own and read_origin_ref_as_data(repo, f"refs/heads/{own}") == head)
                and self._fetched_line(repo, end)
            )
        else:
            fetched = left_branch and self._fetched_line(repo, end) and contains(
                read_origin_ref_as_data(repo, end)
            )
        if fetched:
            self.log.warning(
                "HEAD left the line this step was given for one it fetched; nothing "
                "of it is harvested", start=start, end=end,
            )
            return where()
        if not floor or not _FULL_SHA_RE.fullmatch(floor):
            return None
        code, _ = git(["merge-base", "--is-ancestor", floor, "HEAD"], "harvest-moved-start")
        if code == 0:
            return None
        if code != 1:
            raise GitError("could not tell whether HEAD descends from the commit the step started on")
        branch = self._publish_branch_name()
        read = getattr(self.children, "pushed_tip", None)
        tip = read(branch) if branch and callable(read) else None
        if isinstance(tip, str) and _FULL_SHA_RE.fullmatch(tip):
            code, _ = git(["merge-base", "--is-ancestor", tip, "HEAD"], "harvest-moved-tip")
            if code == 0:
                return None
        moved = where() if left_branch else short_head()
        self.log.warning(
            "HEAD does not descend from the commit this step started on; nothing of "
            "it is harvested", start=floor, head=moved,
        )
        return moved

    def _fetched_line(self, repo: Path, end: str) -> bool:
        """True when the branch HEAD ended on is one the agent fetched, not one
        it made (archived issue 2094): a branch with a `refs/remotes/origin/<name>` in the
        clone, or a detached HEAD standing exactly on a `refs/remotes/origin/*`
        tip. Read from the clone's files as data (`gitops`), never by git.
        For a branch the name is only the first filter: `_head_moved` then
        asks, in `publish_repo`, whether the line holds that tip (archived issue 3435).

        The branch this step itself publishes (`_publish_branch_name`) is never
        a fetched line: a `git_fetch` of it writes its tracking ref, and its
        commits are this task's own, which the descent check (from the start
        or from this attempt's tool-pushed tip) still bounds."""
        own = self._publish_branch_name()
        if own and end == f"refs/heads/{own}":
            return False
        if end == DETACHED_HEAD:
            head = read_head_as_data(repo)
            return head is not None and head in origin_tips_as_data(repo)
        return read_origin_ref_as_data(repo, end) is not None

    @staticmethod
    def _moved_reason(moved: str) -> str:
        """What a step whose HEAD moved is told, by the harvest and the carrier alike."""
        return (
            f"HEAD moved off this task's branch (to {moved}); "
            "nothing of this task's was published"
        )

    def _keep_moved_head(self, publish_repo: Path, floor: str | None) -> str | None:
        """Keep a moved HEAD's line as `MOVED_HEAD_BUNDLE` in the artifacts.

        REPORTED by a peer application's session, 5 November 2026:
        task_884a993427ce44feb7a6's agent merged a train branch into its own,
        checked out another branch, and the harvest rightly published nothing
        -- and kept nothing, so a person rebuilt the merge by hand. The bundle
        holds HEAD's commits that are not on the step's start (`floor..HEAD`),
        or HEAD's whole line when the two share no history or the start is
        unknown, read from the worker's clean repository; `git fetch
        moved-head.bundle` brings it back. Nothing of it is pushed. Returns the
        bundle's name, or None when none was written: a bundle that cannot be
        written is logged and never fails the step.
        """
        ws = self.ws
        assert ws is not None
        target = ws.artifacts / MOVED_HEAD_BUNDLE

        def git(argv: list[str], slug: str) -> int:
            code, _ = _git_text(
                ["git", *_NO_HOOKS, *argv], repo=Path(publish_repo), private_dir=ws.private,
                logs_dir=ws.logs, slug=slug,
                timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
            )
            return code

        try:
            revs = ["HEAD"]
            if floor and _FULL_SHA_RE.fullmatch(floor):
                if git(["merge-base", floor, "HEAD"], "harvest-moved-bundle-base") == 0:
                    revs = [f"{floor}..HEAD"]
            code = git(["bundle", "create", "--quiet", str(target), *revs],
                       "harvest-moved-bundle")
        except Exception as exc:  # noqa: BLE001 -- keeping the line never costs the step
            self.log.warning("the moved HEAD's line could not be kept as a bundle",
                             error=type(exc).__name__)
            return None
        if code != 0 or not target.is_file():
            self.log.warning("the moved HEAD's line could not be kept as a bundle",
                             exit_code=code, revisions=revs)
            return None
        self.log.info("the moved HEAD's line is kept in the step's artifacts",
                      bundle=MOVED_HEAD_BUNDLE, revisions=revs)
        return MOVED_HEAD_BUNDLE

    def _moved_head_result(self, out: dict[str, Any], moved: str, *,
                           bundle: str | None = None) -> dict[str, Any]:
        """The harvest's record of a HEAD that left this step's work (archived issue 2094).

        Nothing on it is this step's, so it counts no commit and no
        uncommitted path, writes no patch and publishes nothing; and it is not
        a publish failure. A step allowed an empty diff therefore ends exactly
        as if its agent had stayed put (`expected_mod.changed_nothing`), a
        review's verdict standing, and one owed a pull request is told why it
        has none (`_published_nothing`). The line itself is kept when it could
        be (`bundle`, `_keep_moved_head`), and the reason names it.
        """
        moved = str(self._scrub(moved))
        reason = self._moved_reason(moved)
        if bundle is not None:
            out["moved_head_bundle"] = bundle
            reason += (f"; its commits are kept in the step's artifacts as {bundle} "
                       f"(git fetch {bundle})")
        out.update(
            {
                "head_moved": moved,
                "commits": [],
                "commit_count": 0,
                "insertions": 0,
                "deletions": 0,
                "dirty": [],
                "dirty_count": 0,
                "published": False,
                "publish_reason": reason,
            }
        )
        return out

    def _publish_branch_name(self) -> str | None:
        """The branch `_publish_git` would push, or None when it would refuse to
        name one. Read to ask for this attempt's own tool-pushed tip, and by
        the head-moved guard as this step's own line (`_fetched_line`)."""
        try:
            return self._push_branch()[0]
        except WorkerError:
            return None

    def _push_branch(self) -> "tuple[str, publishpr_mod.Target | None]":
        """The branch this step pushes, and the pull request it pushes onto
        (or None). The ONE place that is decided: `_publish_git` pushes it and
        the head-moved guard exempts it (`_publish_branch_name`).

        MEASURED 2026-10-26 (F04 of archived issue 2379): the guard named only the amender's
        or `swarm/<own task>`, while a `publish_to_pr` step pushes the pull
        request's branch; an agent that checked that branch out was refused
        `head_moved` and its work published nothing. The descent check and
        `_check_pull_before_push` still bound what lands there.

        Raises WorkerError where `_publish_git` refuses to name a branch.
        """
        pr_role = self._pr_role()
        # The amender's branch is the author's, derived from its task id;
        # `push_branch` never forces, so its push only fast-forwards.
        # Otherwise its own `<prefix><task id>`, or the branch a fix step
        # continues (archived issue 263).
        branch = self._pr_author_branch() if pr_role == "amender" else (
            continuation_mod.publish_branch(
                (self._task or {}).get("metadata"), self.cfg.git_branch_prefix, self.cfg.task_id
            )
        )
        # `publish_to_pr` (archived issue 2183): the step that would open the pull
        # request -- `direct-pr`, the `integrate` integrator -- pushes
        # onto the existing one's branch instead, derived from the signed
        # task id. A contributor still pushes its own branch: the
        # integrator merges it, as without the field.
        onto_pr = (
            self._publish_to_pr()
            if not pr_role and self._opens_pull_request() else None
        )
        if onto_pr is not None:
            branch = onto_pr.branch(self.cfg.git_branch_prefix)
        return branch, onto_pr

    def _adopt_tool_pushed_tip(self, publish_repo: Path, branch: str) -> str | None:
        """The tip this attempt's own `git_push_branch` left on `branch`, when
        HEAD stands on it; the publish diffs from it and leases on it. Else None.

        MEASURED 2026-10-23 (archived issue 1188, acceptance run 39926360548): a continuation
        granted the lane's branch rebased it onto main and pushed it through
        swarm-api. The publish then diffed from the CLONE base -- the lane's
        old tip, which the rebased head no longer stands on -- folded the tree
        into one worker commit on it, pushed that never-forced, was refused as
        behind its remote, and failed publish_failed with its work already on
        the branch.

        The tip is the `new_sha` swarm-api answered this attempt's push with
        (`ChildPath.pushed_tip`, from `GitTools.pushed_tip`), the tip it recorded for the task and branch:
        never a ref in the agent's clone, never a fetched tip, and only for a
        branch this task may push. HEAD must descend from it in the worker's
        own repository, so a head the agent moved off its push publishes as
        before. Adopted, the commits after it -- none, when the head is the
        tip -- are the agent's new work, and the push is `--force-with-lease`
        to exactly the tip: a branch somebody else moved since is refused.
        """
        read = getattr(self.children, "pushed_tip", None)
        tip = read(branch) if callable(read) else None
        if not isinstance(tip, str) or not _FULL_SHA_RE.fullmatch(tip):
            return None
        ws = self.ws
        assert ws is not None
        code, _ = _git_text(
            ["git", *_NO_HOOKS, "merge-base", "--is-ancestor", tip, "HEAD"],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-tool-tip", timeout_seconds=self.cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        if code != 0:
            self.log.warning(
                "the agent pushed this branch through git_push_branch but its head no "
                "longer stands on that push; the publish diffs from the clone base",
                branch=branch, tool_tip=tip,
            )
            return None
        code, head = _git_text(
            ["git", *_NO_HOOKS, "rev-parse", "HEAD"],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-tool-head", timeout_seconds=self.cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        if code == 0 and head.strip() == tip:
            self.log.info(
                "the head is the tip the agent already pushed through git_push_branch; "
                "the publish adds nothing and checks the branch under a lease on it",
                branch=branch, tool_tip=tip,
            )
        else:
            self.log.info(
                "the agent pushed this branch through git_push_branch and committed "
                "since; the publish adds those commits under a lease on that push",
                branch=branch, tool_tip=tip,
            )
        return tip

    def _prepublish_advisory(self) -> str:
        """The runner's "Advisory signals (not a gate)" section, or "" (CTXQ-G).

        Read, scrubbed and capped exactly like `_prepublish_section`, and only
        when it carries the runner's heading, so no other file in the
        artifacts folder reaches the pull request's body this way.
        """
        ws = self.ws
        if ws is None:
            return ""
        path = ws.artifacts / PREPUBLISH_ADVISORY_FILE
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        text = str(self._scrub(redact_text(text).text)).strip()
        if not text.startswith(PREPUBLISH_ADVISORY_HEADING):
            return ""
        if len(text) > PREPUBLISH_ADVISORY_MAX_CHARS:
            text = text[:PREPUBLISH_ADVISORY_MAX_CHARS].rstrip() + "\n\n_[cut by the worker]_"
        return text

    def _prepublish_section(self) -> str:
        """The runner's "Pre-publish checks failed" section, or "" (SPEED-D1).

        Written into the artifacts folder by the runner (`prepublish.Gate`)
        only when a check still failed after the agent's fix turn, or a
        rebase conflict was not resolved: the branch is published anyway and
        this tells the operator what is red. Read after the pre-publish reap,
        like the agent's own body; scrubbed again here, and capped.
        """
        ws = self.ws
        if ws is None:
            return ""
        path = ws.artifacts / PREPUBLISH_SECTION_FILE
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        text = str(self._scrub(redact_text(text).text)).strip()
        if not text.startswith(PREPUBLISH_SECTION_HEADING):
            return ""
        if len(text) > PREPUBLISH_SECTION_MAX_CHARS:
            text = text[:PREPUBLISH_SECTION_MAX_CHARS].rstrip() + "\n\n_[cut by the worker]_"
        return text

    def _fail_clone_unreachable(self, reason: str, tries: int) -> Outcome:
        """A retryable failure before the agent ran: the clone's forge did not answer.

        Measured 2026-10-05 (archived issue 623): task_943349914a88 and task_cb020e97d585
        failed for good on attempt 1 of 3 on "Failed to connect to github.com
        port 443 after 134 s". An outage says nothing about the repository,
        so this ends only the ATTEMPT (`fail_retryably`), as
        `_fail_issue_unreachable` does for the issue fetch: the task goes back
        to READY while it has attempts left, its lease and capacity are
        released, and a task whose attempts are spent ends CANNOT_START.
        """
        error = self._scrub(
            f"{FORGE_UNREACHABLE}: the repository could not be cloned, the forge "
            f"did not answer after {tries} tries: {reason}. The agent was not "
            "started; the attempt is retried while the task has attempts left."
        )
        self.log.warning(
            "the clone's forge did not answer; failing the attempt retryably "
            "before the agent runs",
            cause=FORGE_UNREACHABLE,
            tries=tries,
        )
        summary = self._upload_outputs()
        summary["clone_check"] = {"cause": FORGE_UNREACHABLE, "tries": tries}
        with self._metrics_after_the_record():
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=FORGE_UNREACHABLE,
                result_summary=summary,
                retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                detail={"clone": "repository", "tries": tries},
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _upstream_base_pin(
        self, task: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        """The commit this workflow step clones in place of the branch tip, and its record.

        Returns `(sha, base_pin)`. `(None, None)` -- no read at all, and the
        clone exactly as before -- for a task outside a workflow, a root step,
        and a `single-pr` step (its `pr_role` decides what it clones). For
        every other step the parents' own records are read through the
        worker's one tenant-checked upstream read (`inputs.fetch_upstream_task`,
        which fails the attempt for another tenant's task, as staging does):
        each parent's `result_summary.git.base` is the commit ITS clone started
        from.

          * every parent that recorded a base names the same one -> that sha,
            `{"pinned": true, "sha", "from": [parents]}`;
          * they name different ones -> `parents_disagree`; none recorded one
            -> `no_upstream_base`. Either way `(None, {"pinned": false,
            "reason"})`, and the branch tip is cloned as before.

        A base that is not a full sha is no record (a parent on an empty
        repository records none). A parent whose own `repository_ref` is not
        this task's (a step that cloned its own ref, archived issue 2895) is no record
        either: it cloned another ref, so its base is not this step's base,
        and with no other parent left the clone is `no_upstream_base`.
        """
        if not task.get("workflow_id") or self._dispatch_block().get("pr_role") is not None:
            return None, None
        parents = [
            parent.strip()
            for parent in (task.get("depends_on") or [])
            if isinstance(parent, str) and _TASK_ID_RE.match(parent.strip())
        ]
        if not parents:
            return None, None
        bases: dict[str, str | None] = {}
        for parent in parents:
            upstream = inputs_mod.fetch_upstream_task(
                self.db,
                upstream_task_id=parent,
                tenant_id=self.cfg.tenant_id,
                call_options=self.control.call_options(),
            )
            if (upstream.get("repository_ref") or None) != (task.get("repository_ref") or None):
                self.log.info(
                    "base pin: a parent cloned another ref; its base is not this step's",
                    parent=parent,
                )
                continue
            summary = upstream.get("result_summary")
            git = summary.get("git") if isinstance(summary, dict) else None
            base = git.get("base") if isinstance(git, dict) else None
            bases[parent] = (
                base if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base) else None
            )
        recorded = {base for base in bases.values() if base is not None}
        if len(recorded) == 1:
            sha = next(iter(recorded))
            source = [parent for parent, base in bases.items() if base == sha]
            self.log.info("base pin: cloning the upstream steps' base", base=sha, parents=source)
            return sha, {"pinned": True, "sha": sha, "from": source}
        reason = "parents_disagree" if recorded else "no_upstream_base"
        self.log.warning(
            "base pin: not pinned, cloning the branch tip; a patch staged from "
            "these parents may not apply to it",
            reason=reason,
            parents=dict(bases),
        )
        return None, {"pinned": False, "reason": reason}

    def _carrier_parent(self, task: dict[str, Any], url: str) -> tuple[str, str | None]:
        """`(parent, head)`: the parent whose pushed branch this step starts from (D13).

        `("", None)` when there is none.

        `carrier: branches` only, and never for an integrator: an integrator
        has several parents and merges their branches in step order at its
        publish (`merge_branches`, over `integrates`). Any other step with ONE
        direct parent (`task.depends_on`) starts from that parent's branch,
        when the parent's own result records it (`result_summary.branch`,
        written by the parent's finish) under the name this worker derives
        from the parent's task id with its own prefix -- never a name read as
        data, exactly as `builds_on` and the integrator's branches are derived.

        A step with several parents and no integrator role starts from the
        default branch, and says so: which parent's branch it should start
        from is not something this worker can decide. A parent that recorded
        no branch (it changed nothing, or its push failed) is said too.

        PINNED AND SAME-REPOSITORY (archived issue 453, the archived pull request 463 security review). `head` is
        the full sha the parent recorded in `result_summary.branch.head`, and
        `_maybe_clone` checks out exactly that commit, never the branch's
        current tip. And the branch is cloned from THIS step's repository
        (`url`), so the parent's own `repository_url` must name the same
        repository: a `swarm/<parent>` in another repository is not the
        parent's work. A record with no full sha, or a parent in another
        repository, starts this step from the default branch, and says so.
        """
        if self._dispatch_carrier() != "branches" or self._dispatch_role() == "integrator":
            return "", None
        parents = [
            parent.strip()
            for parent in (task.get("depends_on") or [])
            if isinstance(parent, str) and _TASK_ID_RE.match(parent.strip())
        ]
        if len(parents) != 1:
            if len(parents) > 1:
                self.log.info(
                    "carrier: this step has several parents and is not an integrator; "
                    "it starts from the default branch",
                    parents=parents,
                )
            return "", None
        parent = parents[0]
        upstream = inputs_mod.fetch_upstream_task(
            self.db,
            upstream_task_id=parent,
            tenant_id=self.cfg.tenant_id,
            call_options=self.control.call_options(),
        )
        summary = upstream.get("result_summary")
        branch = summary.get("branch") if isinstance(summary, dict) else None
        expected = f"{self.cfg.git_branch_prefix}{parent}"
        if not isinstance(branch, dict) or branch.get("name") != expected:
            self.log.info(
                "carrier: the parent recorded no branch of its own; this step starts "
                "from the default branch",
                parent=parent,
            )
            return "", None
        head = branch.get("head")
        if not (isinstance(head, str) and _FULL_SHA_RE.fullmatch(head)):
            self.log.warning(
                "carrier: the parent's branch record names no full commit, so there "
                "is nothing to pin to; this step starts from the default branch",
                parent=parent,
            )
            return "", None
        parent_url = upstream.get("repository_url") or self.cfg.repository_url
        if not (isinstance(parent_url, str) and _same_repository(parent_url, url)):
            self.log.warning(
                "carrier: the parent's repository is not this step's; its branch "
                "is not cloned and this step starts from the default branch",
                parent=parent,
                repository=self._scrub(url),
                parent_repository=(
                    self._scrub(parent_url) if isinstance(parent_url, str) else None
                ),
            )
            return "", None
        declared = (task.get("metadata") or {}).get("input_from")
        if isinstance(declared, dict) and declared.get(parent) == "swarm-work.patch":
            # Harmless but redundant: the clone already holds the parent's
            # work, so the patch is the same change a second time, and
            # applying it is the agent's choice, not the worker's.
            self.log.info(
                "carrier: this step starts from its parent's branch, so the "
                "parent's swarm-work.patch it also declared is redundant",
                parent=parent,
            )
        return parent, head

    def _stage_declared_inputs(self, task: dict[str, Any]) -> list[inputs_mod.StagedInput]:
        """Honour `metadata.input_from`: {upstream_task_id: artifact_filename}.

        The API validates the declaration against the DAG and the service
        rewrites it onto the task keyed by upstream TASK id; this is where the
        file actually arrives. See `agent_worker.inputs` for why a declared
        input that cannot be staged fails the attempt instead of warning.

        A task with no declaration returns here without a single read, which is
        what makes this invisible to every deployment that does not use it.
        """
        ws = self.ws
        assert ws is not None
        declared = inputs_mod.declared_inputs(task.get("metadata"))
        if not declared:
            return []
        # AN INPUT WHOSE UPSTREAM LEFT NOTHING IS NOT STAGED, AND IS NAMED
        # (archived issue 978). This step was not skipped, so another of its inputs changed
        # something (`expected_mod.nothing_to_work_on`): the review of two
        # implementers, one of which changed nothing, reviews the one patch
        # that exists instead of failing on the one that was never written.
        self._inputs_left_nothing = []
        kept: list[inputs_mod.DeclaredInput] = []
        for item in declared:
            left = self._upstream_left(item.upstream_task_id)
            if expected_mod.unstaged_left_nothing(item.filename, left):
                self._inputs_left_nothing.append({
                    "task_id": item.upstream_task_id, "filename": item.filename,
                    "left": str(left),
                })
            else:
                kept.append(item)
        if self._inputs_left_nothing:
            self.log.info(
                "declared inputs not staged: their upstream steps left nothing",
                left_nothing=[dict(i) for i in self._inputs_left_nothing],
            )
        declared = kept
        if not declared:
            return []
        staged = inputs_mod.stage_inputs(
            declared,
            work=ws.work,
            store=self.store,
            db=self.db,
            # The startup budget: staging runs before the runner, inside
            # `startup_budget()`, and reads each upstream step's task.
            call_options=self.control.call_options(),
            tenant_id=self.cfg.tenant_id,
            logger=self.log,
            resumed=self._restored_from is not None,
            # The workspace is memory-backed; see `stage_inputs` for why the
            # artifact cap is the right bound on what one attempt may stage.
            max_total_bytes=self.cfg.max_artifact_bytes,
            # Derived from the workspace rather than spelled out, so a new
            # control file added to `Workspace` cannot be silently stageable
            # over -- `control_file_names` finds every property it puts in
            # `work/`. `repo` and `.swarm` are the worker's OWN directories
            # inside `work/`, not the workspace's, so they are named here.
            reserved=frozenset({REPO_DIR_NAME, WORKER_STATE_DIR})
            | ws.control_file_names()
            # `issue.md`, when the task asks for an issue (archived issue 265).
            | issue_mod.reserved_names(task.get("input"), self.cfg.profile)
            # `pull_request.md`, when the step works on a pull request.
            | (
                frozenset({pull_request_mod.FILE_NAME})
                if self._requested_pull_request() is not None
                else frozenset()
            )
            # A review's owner questions, written after staging (archived issue 2070), and a
            # builds_on step's owner answers (archived issue 2836).
            | {owner_questions_mod.NAME, inputs_mod.OWNER_ANSWERS_NAME},
        )
        self._staged_inputs = staged
        self.log.info(
            "declared inputs staged",
            count=len(staged),
            files=[item.path for item in staged],
        )
        return staged

    # -- an empty diff that is a result (owner decision, 2026-10-05) --------

    def _allows_empty_diff(self) -> bool:
        """Whether this step's signed dispatch block says an empty diff is a
        result (`allow_empty_diff`), not the `empty_diff` failure."""
        return expected_mod.allows_empty_diff(self._dispatch_block())

    def _no_change(self, summary: dict[str, Any]) -> bool:
        """True when this attempt may end SUCCEEDED with `no_change`: it is
        allowed an empty diff and the harvest found one. Never for an
        integrator still owed a merge of its contributors, whose deliverable
        is their work, not its own diff."""
        return (
            self._allows_empty_diff()
            and not self._integration_is_pending()
            and expected_mod.changed_nothing(summary.get("git"))
        )

    def _verified_no_change(self, summary: dict[str, Any]) -> str | None:
        """The agent's `verification.md`, scrubbed and bounded, when this attempt
        changed nothing and wrote one; else None (Proposal J, 2026-10-08).

        The owner's rule: no change plus a written verification is the
        `no_change` result, whatever `allow_empty_diff` says, so a step that
        found its issue already fixed on main SUCCEEDS with the evidence
        instead of failing `empty_diff` or `published_nothing`. Not for an
        integrator still owed a merge (`_no_change`'s exception): its
        deliverable is its contributors' work. A blank file, a link or an
        unreadable one is no verification.
        """
        if self.ws is None or self._integration_is_pending():
            return None
        if not expected_mod.changed_nothing(summary.get("git")):
            return None
        text = self._read_agent_text(VERIFICATION_FILE, [])
        if text is None or not text.strip():
            text = self._check_verdict_text()
        if text is None or not text.strip():
            return None
        reason = str(self._scrub(text.strip()))
        encoded = reason.encode("utf-8")
        if len(encoded) > NO_CHANGE_REASON_MAX_BYTES:
            reason = encoded[:NO_CHANGE_REASON_MAX_BYTES].decode("utf-8", "ignore").rstrip() + "..."
        return reason

    def _onto_pr_target(self) -> "publishpr_mod.Target | None":
        """The pull request this step's no-change evidence lands on, or None.

        Only the step that would publish onto it -- the one that opens a pull
        request, with no `single-pr` role -- as in `_publish_git`. A value
        swarm-api could not have signed is None here: the start already
        refused it, so this attempt never reached its finish with one.
        """
        if self._pr_role() or not self._opens_pull_request():
            return None
        try:
            return self._publish_to_pr()
        except WorkerError:
            return None

    def _check_verdict_text(self) -> str | None:
        """A `publish_to_pr` step's `verdict.json`, as the reason its empty
        diff is a result (CHECKONLY-VERDICT, part of archived issue 2241); else None.

        On 2026-10-24 PR 2235's render lane rendered every frame, found no
        defect, changed nothing, and failed `empty_diff`, so its review never
        became a verdict. A verdict the reader cannot read -- not JSON, no
        MERGE or NOT_YET -- is no evidence, and the step fails as before.
        """
        if self._onto_pr_target() is None:
            return None
        raw = self._read_agent_text(CHECK_VERDICT_FILE, [])
        if raw is None:
            return None
        try:
            document = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        read = verdict_mod.verdict_of_document(document)
        if read is None:
            return None
        lines = [f"Review verdict ({CHECK_VERDICT_FILE}): {read.verdict}"]
        if read.findings:
            lines.append("")
            lines += [f"- {finding}" for finding in read.findings]
        if read.findings_dropped:
            lines.append(f"- ... and {read.findings_dropped} more")
        return "\n".join(lines)

    def _post_no_change_evidence(self, summary: dict[str, Any]) -> None:
        """Post a `publish_to_pr` step's no-change evidence as ONE comment on
        its pull request (CHECKONLY-VERDICT, part of archived issue 2241).

        A step that checked and changed nothing has no commit to push onto
        the pull request, so its evidence -- `no_change_reason`, from
        `verification.md` or its `verdict.json` -- goes there as a comment
        instead, where the pull request's reader and its merge step see it.
        The comment ends with a hidden marker naming this task and attempt,
        so a finish of the same attempt again finds it and posts nothing.

        The same gates as a push: publishing enabled, the forge grant, the
        git token, a token that can push, and the pull request read again and
        held to `publishpr.refusal` (open, ours, not a fork). Never a refusal
        of the step: the evidence is the step's result either way, so a
        comment not posted is recorded in `result_summary` and logged, as the
        out-of-territory comment is (archived issue 1468). Reached only from
        `_record_finalised`, beside the publish it stands in for.
        """
        target = self._onto_pr_target()
        reason = summary.get(NO_CHANGE_REASON_SUMMARY_KEY)
        if target is None or not isinstance(reason, str) or not reason.strip():
            return
        record: dict[str, Any] = {"number": target.number}
        summary[NO_CHANGE_COMMENT_SUMMARY_KEY] = record

        def skipped(why: str) -> None:
            record["posted"] = False
            record["reason"] = str(self._scrub(why[:400]))
            self.log.warning("the no-change evidence was not posted on the pull request",
                             number=target.number, reason=record["reason"])

        if not self.cfg.git_publish_enabled:
            return skipped("publishing is disabled for this worker")
        refused = self._forge_write_refusal()
        if refused is not None:
            return skipped(f"{refused[0]}: {refused[1]}")
        token_refusal = self._git_token_refusal()
        if token_refusal:
            return skipped(f"the tenant git token is refused: {token_refusal}")
        url = self._repo_url or self.cfg.repository_url
        if not url:
            return skipped("the repository URL is unknown")
        token = self._git_token(write=True)
        try:
            token, access = self._probe_reading_again(url, token, "the no-change comment probe")
            if access is None:
                return skipped("this repository is not on a forge this worker can publish to")
            if not access.can_push or not token:
                return skipped(access.reason or "no credential that can write")
            pull = self._read_pull(access, token, target.number)
            why = publishpr_mod.refusal(
                pull, target=target, repository=access.ref.full_name,
                branch_prefix=self.cfg.git_branch_prefix,
            )
            if why:
                return skipped(why)
            marker = (
                f"<!-- swarm:no-change task={self.cfg.task_id} "
                f"attempt={self.cfg.attempt_id} -->"
            )
            try:
                # A read is retried on an outage; the POST is not, since one
                # whose answer was lost may have made the comment (the marker
                # finds it on the next finish).
                existing = forge_mod.retry_transient(
                    lambda: forge_mod.list_issue_comments(
                        ref=access.ref, token=token, number=target.number
                    ),
                    policy=self._forge_retry(),
                    what="the pull request's comments",
                )
            except ForgeError as exc:
                existing = []
                self.log.warning("the pull request's comments could not be read",
                                 error=str(self._scrub(str(exc)[:300])))
            if any(marker in body for body in existing):
                record["posted"] = True
                record["comment"] = "already posted"
                return None
            checked = self._pr_start_head or pull.head_sha
            body = "\n".join([
                f"SwarmCloud checked this pull request at `{checked}` and changed nothing.",
                "",
                f"- task: `{self.cfg.task_id}`",
                f"- attempt: `{self.cfg.attempt_id}`",
                "",
                _neutralise_mentions(reason.strip()),
                "",
                marker,
            ])
            record["comment"] = forge_mod.create_issue_comment(
                ref=access.ref, token=token, number=target.number, body=body
            )
            record["posted"] = True
            record["head"] = checked
            self.log.info("the no-change evidence is posted on the pull request",
                          number=target.number)
        except ForgeError as exc:
            return skipped(f"the forge refused it: {exc}")
        return None

    def _no_change_after_publish(self, summary: dict[str, Any]) -> bool:
        """Mark `summary` as the `no_change` result when the publish found the
        continued branch already holding this round's change; True when it did.

        Decided at the publish, not by the harvest's diff (archived issue 2179): the round
        did change its clone, but the branch it continues moved by a merge of
        the default branch (`_push_onto_base_merge`) to a tip that already
        holds every change, the archived pull request 2093 case -- or origin's tip of the branch
        already holds the round's HEAD, the archived pull request 2446 case (archived issue 2506). The work is
        on the branch, so it is the `no_change` result rather than
        `publish_failed`, with the tip named in the reason. The same holds
        when the base merge rewrote every file the round changed (archived issue 2367):
        the reason then starts `superseded_by_base:` and names the paths
        whose base version stands.
        """
        git = summary.get("git")
        reason = git.get(BASE_MERGE_NO_CHANGE_FIELD) if isinstance(git, dict) else None
        if not reason or git.get(PUBLISH_FAILED_FIELD) is True:
            return False
        summary[expected_mod.NO_CHANGE_SUMMARY_KEY] = True
        summary[NO_CHANGE_REASON_SUMMARY_KEY] = str(reason)
        return True

    def _upstream_left(self, task_id: str) -> str | None:
        """`expected_mod.left_nothing` of one upstream, read through the
        tenant-checked upstream read; None for one that did not SUCCEED.

        None too for one that cannot be read -- no document, another
        tenant's: that is not evidence it changed nothing, and the step goes
        on to meet the refusal where it always did (staging, the clone, the
        merge's "missing"), with the words those give.
        """
        return expected_mod.left_nothing(self._succeeded_upstream_summary(task_id))

    def _succeeded_upstream_summary(self, task_id: str) -> Any:
        """One upstream's `result_summary`, read through the tenant-checked
        upstream read; None for one that did not SUCCEED or cannot be read."""
        try:
            upstream = inputs_mod.fetch_upstream_task(
                self.db,
                upstream_task_id=task_id,
                tenant_id=self.cfg.tenant_id,
                call_options=self.control.call_options(),
            )
        except InputUnavailable:
            return None
        if upstream.get("state") != TaskState.SUCCEEDED.value:
            return None
        return upstream.get("result_summary")

    def _nothing_to_work_on(self, task: dict[str, Any]) -> list[str]:
        """The upstream task ids whose missing change leaves this step nothing
        to do (`expected_mod.nothing_to_work_on`), or [] when it runs.

        Every id comes from the verified spec: `metadata.input_from`'s keys
        and the signed dispatch block's `builds_on`, `pr_author` (a
        `single-pr` reader or amender clones the author's branch), merge
        target and `integrates`. A step that names none reads nothing.

        `builds_on` is passed apart from the other branches (archived issue 978): a step it
        names that left nothing does not skip a step with another changed
        input, which then starts from `_builds_on_base`. The read-only
        contributors (`published_nothing`, archived issue 760) are passed so a review that
        ran is not counted as a change to integrate.
        """
        block = self._dispatch_block()
        declared = (task.get("metadata") or {}).get("input_from")
        input_from: dict[str, str] = {
            key.strip(): value
            for key, value in (declared.items() if isinstance(declared, dict) else ())
            if isinstance(key, str) and isinstance(value, str) and _TASK_ID_RE.match(key.strip())
        }
        branch_from: list[str] = []
        builds_on = self._dispatch_builds_on()
        author = block.get("pr_author")
        if self._pr_role() in ("reader", "amender") and isinstance(author, str) and (
            _TASK_ID_RE.match(author.strip())
        ):
            branch_from.append(author.strip())
        target = block.get(merge_mod.MERGE_TARGET_FIELD)
        pull = target.get("pull_request") if isinstance(target, dict) else None
        if isinstance(pull, str) and _TASK_ID_RE.match(pull.strip()):
            branch_from.append(pull.strip())
        integrates = (
            [t for t in self._dispatch_integrates() if _TASK_ID_RE.match(t)]
            if self._integration_is_pending() else []
        )
        upstream: list[str] = list(dict.fromkeys(
            [*input_from, *branch_from, *([builds_on] if builds_on else []), *integrates]
        ))
        if not upstream:
            return []
        summaries = {task_id: self._succeeded_upstream_summary(task_id) for task_id in upstream}
        left = {task_id: expected_mod.left_nothing(summaries[task_id]) for task_id in upstream}
        read_only = [t for t in integrates if published_nothing(summaries[t])]
        return expected_mod.nothing_to_work_on(
            input_from=input_from, branch_from=branch_from, integrates=integrates, left=left,
            builds_on=builds_on, read_only=read_only,
        )

    def _builds_on_owner_answers(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """The owner's answers to the questions of the steps this one builds on (archived issue 2836).

        One `{task_id, step_id, question_index, question, answer}` per
        ANSWERED question of the signed `builds_on` step, then of the step
        that one builds on, and so on: a fix of a fix is bound by the first
        step's decisions too. Read through the tenant-checked upstream read
        and `owner_questions.questions_of`, as a review's owner questions
        are; a step of another workflow ends the walk, which is bounded by
        the workflow's step count and never revisits a task. An upstream
        that cannot be read gives no rows from there on, with a line saying
        why: the answers inform the agent, and the clone has already met
        that upstream's refusal if it matters.
        """
        current = self._dispatch_builds_on()
        workflow_id = task.get("workflow_id")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current and current not in seen and len(seen) < owner_questions_mod.MAX_ANCESTORS:
            seen.add(current)
            try:
                document = inputs_mod.fetch_upstream_task(
                    self.db, upstream_task_id=current, tenant_id=self.cfg.tenant_id,
                    call_options=self.control.call_options(),
                )
                if document.get("workflow_id") != workflow_id:
                    break
                asked = owner_questions_mod.questions_of(
                    current, document, store=self.store, tenant_id=self.cfg.tenant_id)
            except InputUnavailable as exc:
                self.log.warning(
                    "the owner answers of a step this one builds on could not be read",
                    upstream=current, reason=str(exc),
                )
                break
            rows += [
                {key: row[key] for key in
                 ("task_id", "step_id", "question_index", "question", "answer")}
                for row in asked if row["answer"] is not None
            ]
            metadata = document.get("metadata")
            block = metadata.get("dispatch") if isinstance(metadata, dict) else None
            raw = block.get("builds_on") if isinstance(block, dict) else None
            current = raw.strip() if isinstance(raw, str) and _TASK_ID_RE.match(raw.strip()) else ""
        return rows

    def _builds_on_base(self) -> str:
        """The task whose pushed branch this step starts from, or "" for the
        default branch (archived issue 978).

        The signed `builds_on`, when that step changed something -- or when
        it cannot be read, so the clone meets the refusal it always did, with
        its words. When it left nothing (`no_change`, or skipped) it pushed no
        branch, and this step runs only because another of its inputs changed
        (`_nothing_to_work_on`): the walk follows that step's OWN `builds_on`
        back to the nearest ancestor that pushed, and with none, the default
        branch -- the base that step itself started from.

        Each ancestor whose dispatch block is followed is read through the
        tenant-checked upstream read and its signed spec verified as a step of
        THIS workflow (`specverify.upstream_verifier`, as a worker action
        verifies its upstreams), because its `builds_on` decides what this step
        clones. One that does not verify, cannot be read, or names something
        that is not a task id is a refusal naming it -- never a silent fall
        back to the default branch. The walk is bounded by the workflow's step
        count and refuses a cycle.
        """
        if self._builds_on_resolved is not None:
            return self._builds_on_resolved[0]
        declared = self._dispatch_builds_on()
        walked: list[str] = []
        base = declared
        if declared:
            raw_workflow = (self._task or {}).get("workflow_id")
            verify = specverify.upstream_verifier(
                self.cfg, raw_workflow if isinstance(raw_workflow, str) else None
            )
            current = declared
            base = ""
            for _ in range(_MAX_WORKFLOW_STEPS):
                try:
                    document = inputs_mod.fetch_upstream_task(
                        self.db, upstream_task_id=current, tenant_id=self.cfg.tenant_id,
                        call_options=self.control.call_options(),
                    )
                except InputUnavailable as exc:
                    if current == declared:
                        base = declared
                        break
                    raise WorkerError(
                        f"this step builds on task {declared}, which left nothing to "
                        f"build on; its ancestor {current} could not be read ({exc}), "
                        "so the branch to start from is unknown and nothing was cloned"
                    ) from exc
                summary = (
                    document.get("result_summary")
                    if document.get("state") == TaskState.SUCCEEDED.value else None
                )
                if expected_mod.left_nothing(summary) is None:
                    base = current
                    break
                walked.append(current)
                try:
                    verify(current, document)
                except specverify.UpstreamSpecUnverified as exc:
                    raise WorkerError(
                        f"this step builds on task {declared}, which left nothing to "
                        f"build on, and the signed spec of task {current} did not "
                        f"verify ({exc.why}); its builds_on is not trusted to choose "
                        "the branch to start from, so nothing was cloned"
                    ) from exc
                metadata = document.get("metadata")
                block = metadata.get("dispatch") if isinstance(metadata, dict) else None
                raw = block.get("builds_on") if isinstance(block, dict) else None
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    break
                if not isinstance(raw, str) or not _TASK_ID_RE.match(raw.strip()):
                    raise WorkerError(
                        f"this step builds on task {declared}, which left nothing to "
                        f"build on, and task {current}'s dispatch block names builds_on "
                        f"{str(raw)[:80]!r}, which is not a task id; nothing was cloned"
                    )
                current = raw.strip()
                if current in walked or current == self.cfg.task_id:
                    raise WorkerError(
                        f"this step builds on task {declared}, and the builds_on chain "
                        f"from it returns to task {current}; nothing was cloned"
                    )
            else:
                raise WorkerError(
                    f"this step builds on task {declared}, and the builds_on chain from "
                    f"it is longer than a workflow's {_MAX_WORKFLOW_STEPS} steps; "
                    "nothing was cloned"
                )
        self._builds_on_resolved = (base, walked)
        if walked:
            self.log.info(
                "builds_on: the step this one builds on left nothing; starting from "
                + (f"task {base}'s branch" if base else "the default branch"),
                builds_on=declared, left_nothing=walked, base=base or None,
            )
        return base

    def _finish_nothing_to_change(self, upstream: list[str]) -> Outcome:
        """End this step SUCCEEDED, SKIPPED for "nothing to change" (2026-10-05).

        No agent, no clone, no staging, no publish: the change this step was
        to work on does not exist. The generation is checked first, as before
        any ending, and the finish is the fenced write every other ending
        makes, which releases the lease. SUCCEEDED, not CANCELLED: the
        workflow reads a skipped step as a success (`swarm_api.rollup`), and
        its own dependants are promoted, read this marker and skip in turn.
        The frozen `TaskState` has no SKIPPED; the marker is
        `result_summary.skipped`.
        """
        self.phases.enter("nothing_to_change")
        self.control.validate_generation()
        summary: dict[str, Any] = {
            expected_mod.SKIPPED_SUMMARY_KEY: {
                "reason": expected_mod.NOTHING_TO_CHANGE,
                "upstream": list(upstream),
            },
        }
        with self._metrics_after_the_record():
            self.control.finish(state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary)
        self.log.info(
            "nothing to change: a step this one needs changed nothing; no agent ran",
            upstream=list(upstream),
        )
        return Outcome(exit_code=ExitCode.OK, state=TaskState.SUCCEEDED)

    def _ci_fix_superseded(self, repo_info: dict[str, Any] | None) -> str | None:
        """Why this CI fix has nothing left to fix, or None (archived issue 2276, `cifixstart`).

        Only for a task whose SIGNED dispatch block carries `ci_fix_head`, on a
        fresh clone: an attempt restored from a checkpoint has already run its
        agent on the red head, and the push judges that work. The pull request
        is read with the reader `_read_pull_at_start` uses; a forge that cannot
        be read, or a token this worker may not hold, leaves the clone alone
        to decide, so an outage never ends a fix that still has work.
        """
        rec = cifixstart_mod.record((self._task or {}).get("metadata"))
        if rec is None or repo_info is None or repo_info.get("from_checkpoint") \
                or self._restored_from is not None:
            return None
        pull: PullHead | None = None
        url = self._repo_url
        token = None if self._git_token_refusal() else self._git_token(write=True)
        if url and token:
            try:
                token, access = self._probe_reading_again(url, token, "the CI fix start check")
                if access is not None:
                    pull = self._read_pull(access, token or "", rec.pull_request)
            except (forge_mod.ForgeUnavailable, ForgeError) as exc:
                self.log.warning(
                    "CI fix start check: the pull request could not be read; the clone decides",
                    number=rec.pull_request, error=self._scrub(str(exc)[:300]),
                )
        return cifixstart_mod.superseded(rec, self._clone_commit, pull)

    def _finish_superseded_ci_fix(self, reason: str) -> Outcome:
        """End a superseded CI fix SUCCEEDED as `no_change`, before its agent ran (archived issue 2276).

        No agent, no publish, no push: the red commit it was to fix is no
        longer the pull request's head, or the pull request is merged or
        closed. The generation is checked first, as before any ending, and the
        finish is the fenced write every other ending makes, which releases
        the lease (invariant 5).
        """
        self.phases.enter("superseded_ci_fix")
        self.control.validate_generation()
        summary: dict[str, Any] = {
            expected_mod.NO_CHANGE_SUMMARY_KEY: True,
            NO_CHANGE_REASON_SUMMARY_KEY: reason,
        }
        with self._metrics_after_the_record():
            self.control.finish(state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary)
        self.log.info("CI fix superseded at start; no agent ran", reason=reason)
        return Outcome(exit_code=ExitCode.OK, state=TaskState.SUCCEEDED)

    def _integrates_with_changes(self) -> list[str]:
        """The contributors this integrator merges: `integrates`, less every
        one that ended with nothing to change (`no_change`, or skipped). Those
        pushed no branch, so merging them would name them "missing" on the
        pull request, as an incomplete integration they are not. They are
        recorded in `_no_change_contributors`, and the publish says so.

        Left out too, into `_read_only_contributors` (archived issue 760): one that
        SUCCEEDED having published nothing (`published_nothing`) -- under
        `integrate` a review step is a contributor, and one that writes only
        `verdict.json` never had a branch to push. "missing" means a step
        that should have pushed and did not; a FAILED contributor, or one
        that says it published, is still merged and still named missing when
        its branch is absent."""
        kept: list[str] = []
        self._no_change_contributors = []
        self._read_only_contributors = []
        for task_id in self._dispatch_integrates():
            # One that cannot be read is merged as before, and the merge
            # names a branch it cannot find.
            summary = (
                self._succeeded_upstream_summary(task_id) if _TASK_ID_RE.match(task_id) else None
            )
            if expected_mod.left_nothing(summary) is not None:
                self._no_change_contributors.append(task_id)
            elif published_nothing(summary):
                self._read_only_contributors.append(task_id)
            else:
                kept.append(task_id)
        return kept

    def _ui_paths_reviewed(
        self, task: dict[str, Any], staged: list[inputs_mod.StagedInput]
    ) -> list[str] | None:
        """The UI paths the patch this review step reviews touches (archived issue 2184), or None.

        None for a step that is not a review (no dependant stages its
        `verdict.json`) and for a patch that touches no UI path. The patch is
        every `*.patch` staged through `input_from`, and the `builds_on`
        step's own file list (`verdict.ui_paths_in_files`). A list cut at its
        bound with no UI path in it, or a `builds_on` step that cannot be
        read, is [] -- gated, naming no path -- because a review that could
        not be told what it reviews is not one that saw it.

        ONLY WHAT THE LANE ITSELF CHANGED IS ITS UI CHANGE (archived issue 2337). A lane
        started from another PR's branch that merged origin/main carries
        their UI files in its patch, and its code-only review was forced to
        NOT_YET. So an upstream's `swarm-work.patch`, and a `builds_on`
        step's list, are judged by the paths its own commits changed
        (`result_summary.git.own_files`) when it recorded them; a step
        recorded without them, or one whose document cannot be read, is
        judged by the whole patch, as before.

        A FIX LOOP'S RE-REVIEW (docs/design/fix-loop.md §4.3) is a lone
        `collect` step that no dependant stages from: its signed dispatch
        list `rendered_review_paths` is read as the patch headers are
        (`verdict.ui_paths_in_dispatch`), on any step that carries one.
        """
        signed = verdict_mod.ui_paths_in_dispatch(self._dispatch_block())
        found: dict[str, None] = dict.fromkeys(signed[0] if signed else ())
        unknown = bool(signed and signed[1])
        if not owner_questions_mod.is_review(task.get("metadata")):
            if not found and not unknown:
                return None
            self.log.info(
                "this re-review's signed paths touch a UI path: a MERGE counts only "
                "from a rendered review",
                ui_paths=list(found)[: verdict_mod.MAX_UI_PATHS_SHOWN],
                runner_profile=self.cfg.runner_profile,
            )
            return list(found)
        ws = self.ws
        assert ws is not None
        for item in staged:
            if not item.filename.endswith(verdict_mod.PATCH_SUFFIX):
                continue
            own = self._own_ui_paths(item)
            if own is not None:
                paths, cut = own
                found.update(dict.fromkeys(paths))
                unknown = unknown or cut
                continue
            try:
                text = (ws.work / item.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                unknown = True
                continue
            found.update(dict.fromkeys(verdict_mod.ui_paths_in_patch(text)))
        builds_on = self._dispatch_builds_on()
        if builds_on:
            try:
                upstream = inputs_mod.fetch_upstream_task(
                    self.db, upstream_task_id=builds_on, tenant_id=self.cfg.tenant_id,
                    call_options=self.control.call_options(),
                )
            except InputUnavailable:
                unknown = True
            else:
                paths, cut = verdict_mod.ui_paths_in_files(upstream.get("result_summary"))
                found.update(dict.fromkeys(paths))
                unknown = unknown or cut
        if not found and not unknown:
            return None
        self.log.info(
            "this review's patch touches a UI path: a MERGE counts only from a rendered review",
            ui_paths=list(found)[: verdict_mod.MAX_UI_PATHS_SHOWN],
            runner_profile=self.cfg.runner_profile,
        )
        return list(found)

    def _own_ui_paths(self, item: inputs_mod.StagedInput) -> tuple[list[str], bool] | None:
        """The UI paths the step that made this staged patch changed itself (archived issue 2337).

        Only for its harvested `swarm-work.patch`, the one `own_files`
        describes; any other `.patch` it wrote is read as a patch. None
        when the step recorded no own list or its document cannot be read.
        """
        if item.filename != PATCH_NAME:
            return None
        try:
            upstream = inputs_mod.fetch_upstream_task(
                self.db, upstream_task_id=item.upstream_task_id, tenant_id=self.cfg.tenant_id,
                call_options=self.control.call_options(),
            )
        except InputUnavailable:
            return None
        return verdict_mod.ui_paths_in_own_files(upstream.get("result_summary"))

    def _normalise_flow_output(self) -> None:
        """Rename a QA flow step's lone stray tar to its `<step>.tar` (`flow_output`; archived pull request 3521)."""
        ws = self.ws
        if ws is None or not self._expected_outputs:
            return
        record = flow_output_mod.normalise(
            ws.artifacts, (self._task or {}).get("step_id"), self._expected_outputs)
        self._flow_output = record
        if record is None:
            return
        for line in record["lines"]:
            (self.log.info if record["ok"] else self.log.warning)(line)
        # On the step's events too, RUNNING with `cause` as `_emit_startup_mark`
        # writes; not on a fenced exit, and never raised: the upload goes on.
        if self._fenced_exit or self._writes_forbidden or self._control_plane_down:
            return
        try:
            self.control.emit(EventType.RUNNING, self._scrub({
                "cause": flow_output_mod.EVENT_CAUSE, "expected": record["expected"], "ok": record["ok"],
                "renamed": record["renamed"], "lines": list(record["lines"]),
            }))
        except Exception as exc:  # noqa: BLE001 -- a mark is never worth the upload
            self.log.warning("could not record the flow output's normalisation on the task's events",
                             error=type(exc).__name__)

    def _hold_ui_review_to_rendered(self) -> None:
        """Rewrite this UI-gated review's MERGE to NOT_YET unless it rendered (archived issue 2184)."""
        ws = self.ws
        if ws is None or self._ui_review_paths is None:
            return
        try:
            record = verdict_mod.gate_rendered_review(
                ws.artifacts / owner_questions_mod.REVIEW_OUTPUT,
                ui_paths=self._ui_review_paths,
                runner_profile=self.cfg.runner_profile,
                artifacts=ws.artifacts,
            )
        except OSError as exc:
            # A MERGE that should read NOT_YET and could not be rewritten is
            # removed rather than left to count: the missing-output check then
            # fails the attempt by name, and no dependant reads a MERGE.
            (ws.artifacts / owner_questions_mod.REVIEW_OUTPUT).unlink(missing_ok=True)
            record = {"ui_paths": self._ui_review_paths[: verdict_mod.MAX_UI_PATHS_SHOWN],
                      "error": f"the verdict could not be rewritten ({type(exc).__name__}), "
                               "so it was removed"}
            self.log.error("a UI-gated review's verdict could not be rewritten; removed",
                           error=type(exc).__name__)
        self._rendered_review = record
        if record is not None and record.get("reason"):
            self.log.warning(
                "a UI-gated review's MERGE is recorded as NOT_YET", reason=record["reason"]
            )

    def _evaluate_verdict_gate(self, staged: list[inputs_mod.StagedInput]) -> bool | None:
        """Read this step's verdict gate (archived issue 264). None: no gate. True/False: run the agent or not.

        Raises `InputUnavailable` for a gate or a verdict file that cannot be
        read, so the agent never starts and nothing is published; see
        `agent_worker.verdict` for why neither is guessed at.
        """
        gate = verdict_mod.gate_from_dispatch(self._dispatch_block())
        if gate is None:
            return None
        ws = self.ws
        assert ws is not None
        source = next((item for item in staged if item.upstream_task_id == gate.task_id), None)
        if source is None:
            raise InputUnavailable(
                f"this step's verdict gate reads the verdict of task {gate.task_id}, "
                "and this step stages no file from that task; the agent was not "
                "started and nothing was published"
            )
        read = verdict_mod.read_verdict(
            ws.work / source.path, task_id=gate.task_id, filename=source.filename
        )
        # archived issue 4101: a review the size gate skipped reads as MERGE to the gate,
        # by the gate's rule (`verdict_mod.gate_reading`); its file says SKIPPED.
        reading = verdict_mod.gate_reading(read)
        runs = reading in gate.verdict_in
        # The gate's further reviews (`also_task_ids`): each one's staged
        # verdict is read as strictly as the first's, and ANY in `verdict_in`
        # runs the agent -- the security review beside the first review, whose
        # NOT_YET runs the fix even when the first said MERGE.
        also_read: list[tuple[inputs_mod.StagedInput, verdict_mod.Verdict, str]] = []
        for also_id in gate.also_task_ids:
            also_source = next(
                (item for item in staged if item.upstream_task_id == also_id), None)
            if also_source is None:
                raise InputUnavailable(
                    f"this step's verdict gate also reads the verdict of task {also_id}, "
                    "and this step stages no file from that task; the agent was not "
                    "started and nothing was published"
                )
            also_verdict = verdict_mod.read_verdict(
                ws.work / also_source.path, task_id=also_id, filename=also_source.filename)
            also_reading = verdict_mod.gate_reading(also_verdict)
            also_read.append((also_source, also_verdict, also_reading))
            runs = runs or also_reading in gate.verdict_in
        passed_on = ws.work / source.path
        # The no-op precheck, check (b) (archived issue 2524, the owner 2026-10-28): a NOT_YET
        # that would run the agent does not when this head already holds a
        # commit a review gave MERGE. The MERGE is then the verdict passed on.
        noop = self._noop_precheck_b(staged, source, read) if (
            runs and read.verdict == "NOT_YET" and not also_read) else None
        if noop is not None:
            runs, passed_on = False, noop[1]
        self._verdict_staged = (passed_on, source.filename)
        self._verdict = {
            "task_id": gate.task_id,
            "file": source.filename,
            "verdict": read.verdict,
            "verdict_in": list(gate.verdict_in),
            "agent_ran": runs,
            "findings": list(read.findings) + [
                finding for _src, other, _r in also_read for finding in other.findings],
            "findings_dropped": read.findings_dropped + sum(
                other.findings_dropped for _src, other, _r in also_read),
        }
        if also_read:
            self._verdict["also"] = [
                {"task_id": src.upstream_task_id, "file": src.filename,
                 "verdict": other.verdict, "gate_reading": other_reading}
                for src, other, other_reading in also_read
            ]
        if read.skipped is not None:
            self._verdict["gate_reading"] = reading
            self._verdict["skipped_review"] = read.skipped
        # Variant 3 of the diff viewer: where each finding is, and the digest
        # of the patch the review read, so a pin is shown only against that
        # patch. Added only when a finding said where it is: a verdict with no
        # location keeps exactly the record it always had.
        if read.locations:
            self._verdict["finding_locations"] = [loc.as_dict() for loc in read.locations]
            self._verdict["reviewed_patches"] = self._reviewed_patches(gate.task_id)
        if read.locations_dropped:
            self._verdict["locations_dropped"] = read.locations_dropped
        if noop is not None:
            self._verdict[verdict_mod.NOOP_PRECHECK_KEY] = noop[0].as_record()
        self._verdict_minors = read.minors
        self._verdict_minors_dropped = read.minors_dropped
        self.log.info(
            "verdict gate read: the agent runs" if runs
            else "verdict gate read: the agent does not run; the step still publishes",
            verdict=read.verdict,
            verdict_in=list(gate.verdict_in),
            verdict_task_id=gate.task_id,
            findings=len(read.findings) + read.findings_dropped,
        )
        return runs

    def _noop_precheck_b(
        self,
        staged: list[inputs_mod.StagedInput],
        source: inputs_mod.StagedInput,
        read: verdict_mod.Verdict,
    ) -> tuple[verdict_mod.ContainedMerge, Path] | None:
        """A MERGE verdict this step stages whose `commit` its head holds, and its file.

        Every staged `verdict.json` is read (the gate's own and any other a
        step stages from a review), and one that cannot be read is passed
        over: the gate's own verdict already decided the step. Only on a fresh
        clone, before any agent wrote to it; the ancestry is asked with hooks
        off. A git that fails or times out reads as "not contained", so the
        agent runs (`verdict.merged_commit_contained`).
        """
        ws = self.ws
        assert ws is not None
        repo = ws.checkout()
        if self._restored_from is not None or not (repo / ".git").exists():
            return None
        found: list[tuple[str, verdict_mod.Verdict, Path]] = [
            (source.upstream_task_id, read, ws.work / source.path)]
        for item in staged:
            if item is source or Path(item.filename).name != owner_questions_mod.REVIEW_OUTPUT:
                continue
            try:
                other = verdict_mod.read_verdict(
                    ws.work / item.path, task_id=item.upstream_task_id, filename=item.filename)
            except InputUnavailable:
                continue
            found.append((item.upstream_task_id, other, ws.work / item.path))

        def is_ancestor(commit: str) -> bool:
            code, _ = _git_text(
                ["git", *_NO_HOOKS, "merge-base", "--is-ancestor", commit, "HEAD"],
                repo=repo, private_dir=ws.private, logs_dir=ws.logs,
                slug="noop-precheck-ancestor",
                timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
            )
            return code == 0

        hit = verdict_mod.merged_commit_contained(
            ((task_id, v) for task_id, v, _path in found), is_ancestor)
        if hit is None:
            return None
        path = next(p for task_id, v, p in found
                    if task_id == hit.task_id and v.commit == hit.commit)
        self.log.info("no-op precheck (b): the agent does not run", reason=hit.reason)
        return hit, path

    def _reviewed_patches(self, review_task_id: str) -> list[dict[str, str]]:
        """The patches the review read, with digests (`verdict.reviewed_patches`).

        One more tenant-checked read of the review's task document. A failure
        records no digest rather than failing the step: the findings and the
        gate are already read, and a finding with no digest to compare is
        shown as not placed, never pinned on a guess.
        """
        try:
            review = inputs_mod.fetch_upstream_task(
                self.db,
                upstream_task_id=review_task_id,
                tenant_id=self.cfg.tenant_id,
                call_options=self.control.call_options(),
            )
        except Exception as exc:  # a digest is never worth the step  # noqa: BLE001
            self.log.warning(
                "verdict gate: the review's task could not be read, so its findings' "
                "locations carry no patch digest",
                verdict_task_id=review_task_id,
                error=type(exc).__name__,
            )
            return []
        return verdict_mod.reviewed_patches(review)

    def _finish_security_review(self) -> None:
        """Validate a security review's audit and write its verdict (`security_review.finish`).

        A failing validator writes no verdict: the missing-output check then
        fails the step, not retryably (`CAUSE_INVALID`), and the validator's
        message is `result_summary.security_review.error`. Never a silent pass.
        """
        ws = self.ws
        if ws is None or self._security_review is None:
            return
        task = self._task or {}
        try:
            record = security_review_mod.finish(
                ws.artifacts, task_id=self.cfg.task_id, step_id=str(task.get("step_id") or ""),
                skill=security_review_mod.skill_dir(security_review_mod.install_dir()),
                started=self._plugins,
            )
        except security_review_mod.SecurityReviewInvalid as exc:
            message = str(self._scrub(str(exc)))
            self._security_review = {"error": message}
            self.log.error("the security review's audit did not validate; the step fails",
                           error=message)
            return
        self._security_review = record
        self.log.info("security review verdict derived", verdict=record["verdict"],
                      blocking=record["blocking"], owner_questions=record["owner_questions"])

    def _post_security_report(self) -> dict[str, Any]:
        """Post the audit's REPORT.md on the pull request this step reviewed.

        The pull request is the one its `builds_on` step recorded opening; the
        token is the tenant's own `-git` one, read here after the agent ended,
        as `_file_review_minors` reads it. The report goes through the
        redaction filter and is cut to GitHub's limit, naming the artifact.
        Never raises: what was posted, or why not, is the result.
        """
        ws = self.ws
        assert ws is not None
        report = security_review_mod.output_dir(ws.artifacts) / security_review_mod.REPORT
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"posted": False, "reason": f"the audit wrote no {security_review_mod.REPORT}"}
        try:
            builds_on = self._dispatch_builds_on()
        except WorkerError as exc:
            return {"posted": False, "reason": str(exc)}
        number = None
        if builds_on:
            try:
                number = post_verdict_mod.pull_request_number(inputs_mod.fetch_upstream_task(
                    self.db, upstream_task_id=builds_on, tenant_id=self.cfg.tenant_id,
                    call_options=self.control.call_options(),
                ))
            except InputUnavailable:
                number = None
        if number is None:
            return {"posted": False,
                    "reason": "the step it builds on recorded no pull request to comment on"}
        ref = forge_mod.parse_repo(self._repo_url or self.cfg.repository_url or "")
        if ref is None or not forge_mod.may_receive_forge_token(ref.host):
            return {"posted": False, "pull_request": number,
                    "reason": "the repository is not on github.com, where the tenant's git "
                              "credential may be sent"}
        refused = self._forge_write_refusal()
        if refused:
            return {"posted": False, "pull_request": number,
                    "reason": f"{refused[0]}: {refused[1]}"}
        refusal = self._git_token_refusal()
        if refusal:
            return {"posted": False, "pull_request": number,
                    "reason": f"refusing to read the tenant git token: {refusal}"}
        token = self._git_token()
        if not token:
            return {"posted": False, "pull_request": number,
                    "reason": "no git credential is registered for this tenant"}
        self.log.register_secret(token)
        pointer = (f"`{security_review_mod.OUTPUT_DIR_NAME}/{security_review_mod.REPORT}` "
                   f"in the artifacts of task `{self.cfg.task_id}`")
        body = security_review_mod.comment_body(
            text, artifact_pointer=pointer,
            scrub=lambda value: _neutralise_mentions(str(self._scrub(value))))
        try:
            forge_mod.create_issue_comment(ref=ref, token=token, number=number, body=body)
        except Exception as exc:  # noqa: BLE001 - the comment is a report, never the verdict
            del token
            return {"posted": False, "pull_request": number,
                    "reason": f"GitHub refused the comment ({type(exc).__name__})"}
        del token
        return {"posted": True, "pull_request": number, "chars": len(body)}

    def _file_review_minors(self) -> dict[str, Any]:
        """File the verdict's minor findings on the tenant's wave epic (archived issue 638).

        See `agent_worker.findings_epic`. The epic is the signed
        `dispatch.findings_epic`; the token is the tenant's own `-git` one,
        read here by the worker after the agent ended and handed only to the
        two forge calls. Never raises: what was filed, or why nothing was, is
        the result, under `result_summary.findings_epic`.
        """
        minors = self._verdict_minors
        extra: dict[str, Any] = {}
        if self._verdict_minors_dropped:
            extra["minors_dropped"] = self._verdict_minors_dropped
        try:
            epic = findings_epic_mod.epic_from_dispatch(self._dispatch_block())
        except ValueError as exc:
            return findings_epic_mod.not_filed(None, len(minors), str(exc), **extra)
        if epic is None:
            return findings_epic_mod.not_filed(
                None, len(minors),
                f"no findings_epic is configured for this tenant, so the {len(minors)} "
                "minor finding(s) in the verdict were not posted",
                **extra,
            )
        if not minors:
            return {"epic": epic, "minors": 0, "filed": [], "already_filed": 0, **extra}
        ref = forge_mod.parse_repo(self._repo_url or self.cfg.repository_url or "")
        if ref is None or not forge_mod.may_receive_forge_token(ref.host):
            return findings_epic_mod.not_filed(
                epic, len(minors),
                "this step's repository is not on github.com, where the tenant's git "
                "credential may be sent, so the minor findings were not posted",
                **extra,
            )
        forge_refused = self._forge_write_refusal()
        if forge_refused is not None:
            # A comment on the epic is a forge write: a read grant, or a grant
            # gone since submission, posts nothing (OB5).
            return findings_epic_mod.not_filed(
                epic, len(minors), f"{forge_refused[0]}: {forge_refused[1]}", **extra
            )
        refusal = self._git_token_refusal()
        if refusal:
            return findings_epic_mod.not_filed(
                epic, len(minors), f"refusing to read the tenant git token: {refusal}", **extra
            )
        token = self._git_token()
        if not token:
            return findings_epic_mod.not_filed(
                epic, len(minors),
                "no git credential is registered for this tenant, so the minor findings "
                "were not posted",
                **extra,
            )
        self.log.register_secret(token)
        verdict = self._verdict or {}
        workflow_id = str((self._task or {}).get("workflow_id") or "")
        found_by = f"found by review `{verdict.get('task_id')}`" + (
            f" of workflow `{workflow_id}`" if workflow_id else ""
        )
        # The lambdas run inside this call, before `del token` below; ruff reads
        # the `del` as leaving `token` unbound in them (F821).
        result = findings_epic_mod.file_minors(
            minors=minors,
            epic=epic,
            ref=ref,
            # Reads are retried on an outage; the POST is not, since one whose
            # answer was lost may have made the comment (the dedup finds it).
            list_comments=lambda number: forge_mod.retry_transient(
                lambda: forge_mod.list_issue_comments(ref=ref, token=token, number=number),  # noqa: F821
                policy=self._forge_retry(),
                what="the findings epic's comments",
            ),
            post_comment=lambda number, body: forge_mod.create_issue_comment(
                ref=ref, token=token, number=number, body=body  # noqa: F821
            ),
            found_by=found_by,
            clean=lambda text: _neutralise_mentions(str(self._scrub(text))),
        )
        del token
        result.update(extra)
        self.log.info(
            "review minors filed on the findings epic" if not result.get("not_filed")
            else "review minors not all filed on the findings epic",
            epic=epic,
            filed=len(result["filed"]),
            already_filed=result["already_filed"],
            minors=len(minors),
            reason=result.get("not_filed"),
        )
        return result

    def _finish_without_agent(self) -> Outcome:
        """End a step whose verdict gate stayed shut: no agent, same ending (archived issue 264).

        Through `_finalise`, the one ending a runner that exited on its own
        gets, so the final checkpoint, the uploads, the publish, the missing-
        output check and the lease release are the ones every other clean
        attempt has. The result it reads is written here, in the runner's
        shape, saying the agent was skipped and why, and the verdict the gate
        read is re-published as this step's own artifact
        (`_republish_staged_verdict`), so a step that stages it reads it.
        """
        ws = self.ws
        assert ws is not None and self._verdict is not None
        self._take_workdir_baseline()
        if self._opens_pull_request():
            # Before the result is written and the finish reads the artifacts
            # folder: the pull request this step owes is titled by the
            # implementer's own text, or the workflow's label (2026-10-05).
            self._adopt_pull_request_text()
        self._republish_staged_verdict()
        ws.result_path.write_text(
            json.dumps(
                {
                    "status": "skipped",
                    "summary": (
                        f"no-op precheck (b): the "
                        f"{self._verdict[verdict_mod.NOOP_PRECHECK_KEY]['reason']}; the "
                        "agent was not started and the step published the reviewed work"
                        if verdict_mod.NOOP_PRECHECK_KEY in self._verdict else
                        f"the review verdict was {self._verdict['verdict']}, and this "
                        f"step's agent runs only on "
                        f"{', '.join(self._verdict['verdict_in'])}; the agent was not "
                        "started and the step published the reviewed work"
                    ),
                    "output": {"verdict_gate": self._verdict},
                }
            )
        )
        return self._finalise(
            ChildResult(
                exit_code=0,
                term_signal=None,
                timed_out=False,
                killed=False,
                duration_seconds=0.0,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )

    def _push_to_republish(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """The previous attempt's push outage, when this retry may publish its
        work without running the agent again, or None (archived issue 2833).

        MEASURED 2026-10-30 (task_982546a3b645480887e3): GitHub answered a
        step's push 500, and the step ended FAILED with its dependants
        cancelled, its finished work only in `swarm-work.patch`. A push the
        forge never answered now fails the attempt retryably
        (`_fail_for_publish_unreachable`), and this is the retry's half:
        running the agent again would spend its time and quota to redo work
        that is already done.

        Every one of these must hold, else the agent runs as on any retry:

        * a checkpoint was restored (`_restored_from`): the work is in `work/`.
          The attempt that failed the push took its final checkpoint before
          the publish, and the retryable failure moves no pointer;
        * the task's `result_summary.git` carries the push's marker
          (`PUBLISH_UNREACHABLE_FIELD` with `publish: push`) and full shas;
        * THE BASE IS STILL VALID: the clone base the restored manifest
          records -- the worker's, never the tree's -- is the marker's;
        * THE WORK IS STILL VALID: the restored clone's HEAD, read from its
          files (`read_head_as_data`, no git process in the agent's clone),
          is the HEAD that attempt harvested;
        * the patch was kept: the summary lists `swarm-work.patch`.

        The summary is the task document's, which a tenant's agent can
        write (see `_recorded_checkpoint`). Taking its word costs nothing it
        could not do already: the most a forged marker does is publish the
        restored checkpoint's own work, through the same harvest, scans and
        push as any attempt, without an agent run.
        """
        ws = self.ws
        restored = self._restored_from
        if ws is None or restored is None:
            return None
        summary = task.get("result_summary")
        git = summary.get("git") if isinstance(summary, dict) else None
        marker = git.get(PUBLISH_UNREACHABLE_FIELD) if isinstance(git, dict) else None
        if not isinstance(marker, dict) or marker.get("publish") not in (
            REPUBLISH_PUSH, REPUBLISH_FETCH,
        ):
            return None
        if marker.get("publish") == REPUBLISH_FETCH and marker.get("on_origin") is False:
            # Origin does not have the object the partial clone lacked (issue
            # 367): the same publish would fail again, so the agent runs.
            self.log.info("origin does not have the object the previous attempt's "
                          "publish fetch lacked; the agent runs again")
            return None
        head, base = marker.get("head"), marker.get("base")
        if not (
            isinstance(head, str) and _FULL_SHA_RE.fullmatch(head)
            and isinstance(base, str) and _FULL_SHA_RE.fullmatch(base)
        ):
            self.log.info("the previous attempt's push outage names no head and base; "
                          "the agent runs again")
            return None
        if restored.clone_base != base:
            self.log.info("the restored checkpoint's base is not the one the unanswered "
                          "push was made from; the agent runs again")
            return None
        try:
            restored_head = read_head_as_data(ws.work / REPO_DIR_NAME)
        except GitError as exc:
            self.log.info("the restored clone's HEAD could not be read; the agent runs again",
                          error=str(exc))
            return None
        if restored_head != head:
            self.log.info("the restored clone is not at the head the unanswered push "
                          "carried; the agent runs again")
            return None
        artifacts = summary.get("artifacts") if isinstance(summary, dict) else None
        if not any(
            isinstance(entry, dict) and entry.get("name") == PATCH_NAME
            for entry in artifacts or []
        ):
            self.log.info(f"the previous attempt kept no {PATCH_NAME}; the agent runs again")
            return None
        return {"head": head, "base": base, "tries": marker.get("tries")}

    def _carry_previous_artifacts(self, task: dict[str, Any]) -> int:
        """Download what the previous attempt uploaded into this one's artifacts (archived issue 2833).

        A retry starts with an empty artifacts directory (only `work/` is
        checkpointed), and the agent that wrote the pull request's title and
        body, and the step's expected outputs, does not run again. So each
        file the previous attempt listed in its `result_summary.artifacts` is
        downloaded back, and this attempt uploads it as its own. Not
        `swarm-work.patch`: the harvest writes it again from the same tree.

        AN ENTRY IS DATA, CHECKED BEFORE IT IS READ, as `_carry_parked_uploads`
        checks one: its `uri` must name the object `artifacts/<name>` of an
        attempt of THIS task under this tenant's prefix, its name a relative
        path that stays inside the artifacts directory, and the total within
        `max_artifact_bytes`. Anything else is skipped and counted. Never
        raises: a file that cannot be carried is missing, and the
        missing-output check says so. Returns how many were carried.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        summary = task.get("result_summary")
        entries = summary.get("artifacts") if isinstance(summary, dict) else None
        prefix = f"tenants/{cfg.tenant_id}/tasks/{cfg.task_id}/attempts/"
        root = ws.artifacts.resolve()
        carried = skipped = 0
        total = 0
        for entry in entries if isinstance(entries, list) else []:
            name = entry.get("name") if isinstance(entry, dict) else None
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if name == PATCH_NAME:
                continue
            key = None
            if isinstance(name, str) and name and isinstance(uri, str):
                at = uri.find(prefix)
                rest = uri[at + len(prefix):] if at >= 0 else ""
                attempt_id, _, tail = rest.partition("/")
                if attempt_id and tail == f"artifacts/{name}":
                    key = f"{prefix}{attempt_id}/artifacts/{name}"
            destination = (root / name).resolve() if key else None
            listed = entry.get("bytes") if isinstance(entry, dict) else None
            if (
                key is None or destination is None
                or name.startswith("/") or root not in destination.parents
                or isinstance(listed, bool) or not isinstance(listed, int)
                or total + listed > cfg.max_artifact_bytes
            ):
                skipped += 1
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                size = self.store.download_file(key, destination)
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                self.log.warning(
                    "could not carry the previous attempt's upload",
                    artifact=standalone_mod.shown(self._scrub(name)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            total += int(size or 0)
            if total > cfg.max_artifact_bytes:
                destination.unlink(missing_ok=True)
                skipped += 1
                continue
            carried += 1
        self.log.info("the previous attempt's uploads were carried into this one",
                      carried=carried, skipped=skipped)
        return carried

    def _republish_without_agent(self, republish: dict[str, Any], task: dict[str, Any]) -> Outcome:
        """Publish the previous attempt's kept work, with no agent run (archived issue 2833).

        Through `_finalise`, as `_finish_without_agent` ends a shut gate's
        step: the final checkpoint, the harvest of the restored clone, the
        uploads, the missing-output check, the publish and the lease release
        are the ones every clean attempt has. The previous attempt's uploads
        are carried in first (`_carry_previous_artifacts`), so the pull
        request is titled by the agent's own text and the expected outputs
        are there.
        """
        ws = self.ws
        assert ws is not None
        self._take_workdir_baseline()
        carried = self._carry_previous_artifacts(task)
        ws.result_path.write_text(
            json.dumps(
                {
                    "status": "republished",
                    "summary": (
                        "the previous attempt's agent finished and its branch push met "
                        f"a forge that did not answer after {republish.get('tries')} "
                        "tries; the agent was not started again and the step published "
                        "the work that attempt kept"
                    ),
                    "output": {"republished": {**republish, "artifacts_carried": carried}},
                }
            )
        )
        self.log.info("publishing the previous attempt's kept work without running the agent",
                      head=republish.get("head"), artifacts_carried=carried)
        return self._finalise(
            ChildResult(
                exit_code=0,
                term_signal=None,
                timed_out=False,
                killed=False,
                duration_seconds=0.0,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )

    def _republish_staged_verdict(self) -> None:
        """Publish the verdict file this step's shut gate read as its own artifact.

        Owner decision, 2026-10-10 (merge chain follow-up, part of archived issue 295): a
        gated step whose agent does not run used to write no artifact, so a
        step that stages from it -- the merge step `on_merge_verdict` derives
        reads the re-review's verdict, and the re-review is gated on the first
        review's NOT_YET -- found nothing on the MERGE path and refused
        `verdict_unreadable`. The verdict that decided this step is the one it
        passes on: copied byte for byte under its `input_from` name, never
        re-serialised, so the reader sees exactly what the review wrote and
        `read_verdict` judges it again. When the agent runs it writes its own
        verdict instead, and this is not called.
        """
        ws = self.ws
        assert ws is not None and self._verdict_staged is not None
        staged, filename = self._verdict_staged
        target = ws.artifacts / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unlinked first, so a name a restored checkpoint left as a link is
        # replaced rather than written through.
        target.unlink(missing_ok=True)
        shutil.copyfile(staged, target)
        self.log.info(
            "verdict gate shut: the staged verdict is re-published as this step's artifact",
            verdict_file=filename,
            verdict_task_id=self._verdict["task_id"] if self._verdict else None,
        )

    def _pr_label(self) -> str | None:
        """The workflow's label from the signed dispatch block (`pr_label`),
        as one line, or None. swarm-api writes it on a gated step only."""
        raw = self._dispatch_block().get(expected_mod.PR_LABEL_KEY)
        if not isinstance(raw, str):
            return None
        text = " ".join(raw.split())
        return text or None

    def _adopt_pull_request_text(self) -> None:
        """Title and describe the pull request of a step whose gate stayed shut.

        Owner decision, 2026-10-05: after a MERGE verdict no fix agent runs,
        so nothing writes the `pr-title.txt` an integrator owes. In order:

          1. the implementer's own `pr-title.txt` and `pr-body.md` -- the
             uploaded artifacts of the `builds_on` step (or, when that step
             changed nothing, of the step this one started from, else the
             first contributor that changed something; archived issue 978), located through the
             tenant-checked upstream read and its successful attempt's
             manifest (`inputs.artifact_reference`, whose key must lie under
             this tenant's prefix for that task), at most
             `PR_READ_LIMIT_BYTES` each;
          2. for whichever is absent or, for the title, unusable: text made
             from the workflow's label (`_pr_label`).

        Each is written into this attempt's artifacts folder, where the
        publish reads an agent's (`_agent_pull_request_text`), so it is
        scrubbed, refused on attribution (a body has its attribution lines
        removed instead) or a task id, and its mentions neutralised exactly
        as an agent's own text is. With neither, nothing
        is written, and the missing title fails the attempt as before. Where
        each came from is `result_summary.pull_request_text_from`.
        """
        ws = self.ws
        assert ws is not None and self._verdict is not None
        source: dict[str, str | None] = {"title": None, "body": None}
        upstream = self._dispatch_builds_on()
        if upstream and self._upstream_left(upstream) is not None:
            # The step this one builds on changed nothing (archived issue 978): its text,
            # if any, describes no change. The ancestor this step started
            # from, else the first contributor that changed something.
            changed = self._integrates_with_changes()
            upstream = self._builds_on_base() or (changed[0] if changed else "")
        document: dict[str, Any] | None = None
        if upstream:
            try:
                document = inputs_mod.fetch_upstream_task(
                    self.db,
                    upstream_task_id=upstream,
                    tenant_id=self.cfg.tenant_id,
                    call_options=self.control.call_options(),
                )
            except InputUnavailable as exc:
                self.log.warning(
                    "the implementer's pull request text could not be read",
                    upstream=upstream, error=str(self._scrub(str(exc))),
                )
        if document is not None:
            for name, part in ((PR_TITLE_FILE, "title"), (PR_BODY_FILE, "body")):
                if self._copy_upstream_text(document, upstream, name):
                    source[part] = "implementer"
        if source["title"] is not None:
            refused: list[str] = []
            if self._agent_title(refused) is None:
                # A title the publish would refuse is no title: the label's
                # stands in rather than the attempt failing on it.
                (ws.artifacts / PR_TITLE_FILE).unlink(missing_ok=True)
                source["title"] = None
                self.log.warning(
                    "the implementer's pr-title.txt was not usable; the label's is used",
                    refused=refused,
                )
        label = self._pr_label()
        if label is not None:
            if source["title"] is None and not (ws.artifacts / PR_TITLE_FILE).exists():
                (ws.artifacts / PR_TITLE_FILE).write_text(label + "\n", encoding="utf-8")
                source["title"] = "label"
            if source["body"] is None and not (ws.artifacts / PR_BODY_FILE).exists():
                verdict = self._verdict["verdict"]
                (ws.artifacts / PR_BODY_FILE).write_text(
                    f"{label}\n\nThe review's verdict was {verdict}, so no fix agent "
                    "ran: this pull request carries the implementer's work as it was "
                    "reviewed.\n",
                    encoding="utf-8",
                )
                source["body"] = "label"
        self._pr_text_from = source
        self.log.info("the shut gate's pull request text", title=source["title"],
                      body=source["body"])

    def _copy_upstream_text(self, document: dict[str, Any], upstream: str, name: str) -> bool:
        """Copy `name` from the upstream's successful attempt into this
        attempt's artifacts folder. False, and nothing written, when it has
        none, it is over `PR_READ_LIMIT_BYTES`, the read fails, or the folder
        already holds the name."""
        ws = self.ws
        assert ws is not None
        destination = ws.artifacts / name
        if destination.exists() or destination.is_symlink():
            return False
        try:
            reference = inputs_mod.artifact_reference(
                document, tenant_id=self.cfg.tenant_id, upstream_task_id=upstream, filename=name,
            )
        except InputUnavailable:
            return False
        if reference.size_bytes > PR_READ_LIMIT_BYTES:
            self.log.warning("the implementer's file is over the read limit; not used",
                             file=name, bytes=reference.size_bytes)
            return False
        try:
            data = self.store.download_bytes(reference.key)
        except Exception as exc:  # the store's own errors vary by backend  # noqa: BLE001
            self.log.warning("the implementer's file could not be read; not used",
                             file=name, error=type(exc).__name__)
            return False
        destination.write_bytes(data[:PR_READ_LIMIT_BYTES])
        return True

    def _link_artifacts(self) -> None:
        """Step 5c: make `work/artifacts` the directory that is uploaded (archived issue 149).
        `work/` is the agent's working directory when the task has no
        repository; with one, `_link_checkout_artifacts` links the checkout's
        `./artifacts` too (archived issue 226), and this one is `../artifacts` from there.

        On every attempt, not only one a later step stages from: the guess it
        catches is as natural for a caller's own prompt that says "write it to
        $SWARM_ARTIFACTS_DIR" as for the platform's instructions. Measured on
        2026-09-25, `wf_06a3a949d2c242c3b0e9`: scan-02 echoed the right path
        and then wrote `<work>/artifacts/scan-02.md`.

        Never fails the attempt. A name that is already taken is the agent's
        or the caller's (see `workspace.link_artifacts`), and a link that
        cannot be made leaves the agent where it was before this existed. Both
        are one WARNING, because either one is why a file written under
        `./artifacts` was not uploaded.
        """
        ws = self.ws
        assert ws is not None
        link = ws.artifacts_link()
        try:
            made = workspace_mod.link_artifacts(ws)
        except OSError as exc:
            self.log.warning(
                "could not link work/artifacts to $SWARM_ARTIFACTS_DIR; files the "
                "agent writes under ./artifacts will not be uploaded",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if made:
            self.log.info(
                "work/artifacts links to $SWARM_ARTIFACTS_DIR, so a file the agent "
                "writes under ./artifacts is uploaded",
                target=str(ws.artifacts),
            )
            return
        self.log.warning(
            "work/artifacts already exists, so it was left in place and not linked "
            "to $SWARM_ARTIFACTS_DIR; files the agent writes under it are not uploaded",
            kind="link" if link.is_symlink() else ("directory" if link.is_dir() else "file"),
            restored_from_checkpoint=self._restored_from is not None,
            staged_inputs_under_it=[
                item.path for item in self._staged_inputs
                if item.path.split("/", 1)[0] == link.name
            ],
        )

    def _link_checkout_artifacts(self) -> None:
        """Step 5c, for a repository task: `./artifacts` in the CHECKOUT is the
        directory that is uploaded, and git never sees it (archived issue 226).

        With a repository attached the agent starts in `work/repo` (owner
        decision of 2026-09-26), so the guess `work/artifacts` catches (archived issue 149:
        an agent that echoed `$SWARM_ARTIFACTS_DIR` and then wrote
        `./artifacts/scan-02.md`) is made in the checkout instead. The link is
        made there as well, and hidden with the clone's `.git/info/exclude`,
        so it is in no patch, no auto-commit and no pushed branch. A link git
        can still see -- the repository's own `.gitignore` un-ignores the name
        -- is taken away again, because a symlink to this attempt's directory
        in the tenant's repository is worse than a missed guess.

        Never fails the attempt, like `_link_artifacts`: every outcome other
        than a hidden link is one WARNING, since each is the reason a file
        written under the checkout's `./artifacts` was not uploaded.
        """
        ws = self.ws
        assert ws is not None
        checkout = ws.checkout()
        if not self._repo_url or not checkout.is_dir() or checkout.is_symlink():
            return
        link = ws.artifacts_link(checkout)
        try:
            made = workspace_mod.link_artifacts(ws, within=checkout)
        except OSError as exc:
            self.log.warning(
                "could not link the checkout's ./artifacts to $SWARM_ARTIFACTS_DIR; "
                "files the agent writes under it stay in the repository",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if not made:
            self.log.warning(
                "the checkout already has an artifacts entry, so it was left in place "
                "and not linked to $SWARM_ARTIFACTS_DIR; files the agent writes under "
                "it are the repository's, not uploaded artifacts",
                kind="link" if link.is_symlink() else ("directory" if link.is_dir() else "file"),
            )
            return
        hidden = hide_from_git(
            repo=checkout,
            name=link.name,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=self.cfg.git_clone_timeout_seconds,
            logger=self.log,
        )
        if hidden:
            self.log.info(
                "the checkout's ./artifacts links to $SWARM_ARTIFACTS_DIR and is "
                "hidden from git, so a file the agent writes under it is uploaded "
                "and is not part of its diff",
                target=str(ws.artifacts),
            )
            return
        try:
            link.unlink()
        except OSError as exc:
            self.log.warning(
                "the checkout's ./artifacts link could not be hidden from git or "
                "removed; it may appear in the agent's diff",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self.log.warning(
            "the checkout's ./artifacts link was removed because git would still "
            "see it (the repository's own .gitignore un-ignores the name, or git "
            "could not be asked); files the agent writes under ./artifacts stay "
            "in the repository"
        )

    def _hidden_checkout_names(self, checkout: Path) -> tuple[str, ...]:
        """Top-level names the publish repository hides from git, as the clone did.

        `_link_checkout_artifacts` hides the checkout's `./artifacts` link in
        the CLONE's `.git/info/exclude`; the worker's commit is now made in the
        publish repository (`gitops.mirror_worktree`, archived pull request 259 M1), which does not
        read that file, so the name is handed over. Only when the entry is
        still the worker's link -- a symlink to this attempt's artifacts
        directory. A repository that tracks its own `artifacts/` never got the
        link, and hiding the name there would drop the agent's new files under
        it from the branch.
        """
        ws = self.ws
        assert ws is not None
        link = ws.artifacts_link(checkout)
        try:
            if link.is_symlink() and os.path.realpath(link) == os.path.realpath(ws.artifacts):
                return (link.name,)
        except OSError:
            pass
        return ()

    # -- the working folder of a task with no repository (#184) --------------
    def _uploads_working_folder(self, task: dict[str, Any]) -> bool:
        """True when this attempt uploads what its agent creates in `work/`.

        A task with NO repository, on a runner whose child is a provider's
        coding-agent CLI (`cli_agent_spec`: claude-code, codex). The URL is
        read from the same two places `_maybe_clone` reads it, so "no
        repository" here and "nothing to clone" there are the same answer. A
        repository task is unchanged: its diff or pull request is the
        deliverable (owner decision of 2026-09-26). See
        `agent_worker.standalone_outputs` for why mock, generic and browser
        are not included.
        """
        if cli_agent_spec(self.cfg.runner_profile) is None:
            return False
        return not (self.cfg.repository_url or task.get("repository_url"))

    def _workdir_reserved(self) -> frozenset[str]:
        """Top-level names in `work/` that are the worker's, never the agent's.

        The control files, found by inspection (`control_file_names`), and the
        `./artifacts` link when it is the worker's own link: its target is
        uploaded already, and a second copy under `workdir/` would be the
        artifacts twice. A real `work/artifacts` folder (the link was not made
        because the name was taken) is the agent's, and is scanned.
        """
        ws = self.ws
        assert ws is not None
        names = set(ws.control_file_names())
        link = ws.artifacts_link()
        if ws.is_artifacts_link(link):
            names.add(link.name)
        if self._extra_checkouts:
            # `work/repos`: the worker's links to the additional checkouts
            # (MULTI-REPO §4.5), never the agent's deliverable.
            names.add(workspace_mod.REPOS_DIR_NAME)
        return frozenset(names)

    def _take_workdir_baseline(self) -> None:
        """Record what `work/` holds before the attempt's first runner starts.

        Once per attempt. Files a checkpoint restored count as created unless
        they are a staged input: the platform puts nothing else in a standalone
        task's `work/` but its control files, which are skipped by name.
        """
        ws = self.ws
        if not self._standalone or self._workdir_baseline is not None or ws is None:
            return
        try:
            present = frozenset(
                standalone_mod.scan(ws.work, reserved=self._workdir_reserved()).files
            )
        except Exception as exc:  # pragma: no cover - defensive; never fails the attempt  # noqa: BLE001
            # No baseline means nothing is uploaded from the working folder:
            # the behaviour before #184, said once, rather than a failed run.
            self.log.warning(
                "could not list the working folder before the runner started; nothing "
                "will be uploaded from it for this attempt",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        staged = {item.path for item in self._staged_inputs}
        carried = (self._restored_workdir - staged) & present
        self._workdir_baseline = present - carried
        self.log.info(
            "no repository: what the agent creates in its working folder is uploaded "
            f"when the attempt ends, under {standalone_mod.PREFIX}/",
            present_before=len(self._workdir_baseline),
            created_by_an_earlier_attempt=len(carried),
            cap_files=self.cfg.max_workdir_output_files,
            cap_bytes=self.cfg.max_workdir_output_bytes,
        )

    def _upload_workdir_outputs(self, *, taken: set[str], budget: int) -> dict[str, Any] | None:
        """Upload what the agent created in `work/`, within the caps. Never raises.

        None when this attempt does not upload its working folder: a
        repository task, a runner that is not a CLI agent, or an attempt whose
        runner never started (no baseline, so nothing can have been created).

        `taken` is the manifest names already uploaded from the artifacts
        folder: a file there named `workdir/<path>` keeps its place. `budget`
        is what `max_artifact_bytes` leaves, so this upload never takes the
        attempt past the cap every artifact is under.

        Each file is copied, without following any symlink, into the worker's
        own scratch folder (`ws.private`, which is in no child's environment),
        redacted there exactly as an artifact is (`_redact_file`), uploaded as
        `workdir/<path>`, and the copy deleted. The agent's own file is left as
        it was, because a resumed attempt restores `work/` from the checkpoint
        taken before this runs.

        Listed and NOT uploaded, each with its reason (see
        `agent_worker.standalone_outputs`): a name that is not UTF-8 or is too
        long to be an object's, a core dump, a name the artifacts folder took,
        a file over a cap, and -- unlike an artifact -- a file holding a
        registered secret the worker could not redact, or could not scan.

        Returns `uploaded` (manifest entries), `bytes` and `summary`, which is
        `result_summary.workdir_outputs`. Nothing goes into
        `artifacts_skipped` (every reader of it calls its names dropped at the
        artifacts folder's size cap) or into `redaction_skipped` (no file this
        uploads left the pod unredacted).
        """
        ws = self.ws
        cfg = self.cfg
        if ws is None or self._workdir_baseline is None:
            return None
        found = standalone_mod.scan(ws.work, reserved=self._workdir_reserved())
        new = standalone_mod.created(found, self._workdir_baseline)
        byte_cap = max(0, min(cfg.max_workdir_output_bytes, budget))
        uploaded: list[dict[str, Any]] = []
        not_uploaded: list[dict[str, Any]] = []
        total = 0
        staging = ws.private / "workdir-output"
        for relative, size in standalone_mod.upload_order(new):
            name = standalone_mod.manifest_name(relative)
            # NAMES FIRST: nothing below may put a name into a key, a log line
            # or the summary before it is known to be one they can all carry.
            if not standalone_mod.storable(name):
                not_uploaded.append(
                    {
                        "name": standalone_mod.shown(self._scrub(name)),
                        "bytes": size,
                        "reason": standalone_mod.NOT_UTF8,
                    }
                )
                continue
            key = f"{cfg.artifact_prefix}/{name}"
            if len(key.encode("utf-8")) > standalone_mod.MAX_OBJECT_NAME_BYTES:
                # SCRUB FIRST, THEN CUT (archived pull request 232 review): `shown` cuts a long name
                # to 256 characters for the log and the summary. Cutting a raw
                # name let a registered secret crossing that character survive
                # in part -- cutting the string is fine, cutting a key in half
                # is not.
                not_uploaded.append(
                    {
                        "name": standalone_mod.shown(self._scrub(name)),
                        "bytes": size,
                        "reason": standalone_mod.NAME_TOO_LONG,
                    }
                )
                continue
            if standalone_mod.is_core_dump(relative):
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.CORE_DUMP})
                continue
            if name in taken:
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.NAME_TAKEN})
                continue
            if len(uploaded) >= cfg.max_workdir_output_files or total + size > byte_cap:
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.OVER_CAP})
                continue
            try:
                ws.private.mkdir(parents=True, exist_ok=True)
                standalone_mod.copy_without_following(
                    ws.work, relative, staging, limit=byte_cap - total
                )
            except standalone_mod.OverCap:
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.OVER_CAP})
                continue
            except standalone_mod.Refused as exc:
                self.log.warning(
                    "refused a working-folder file: a symlink, or not a regular file, "
                    "when it was read; nothing was followed",
                    file=self._scrub(name),
                    error=str(exc),
                )
                not_uploaded.append(
                    {"name": name, "bytes": size, "reason": standalone_mod.SYMLINK_REFUSED}
                )
                continue
            except OSError as exc:
                self.log.warning(
                    "could not read a working-folder file", file=self._scrub(name), error=str(exc)
                )
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.UNREADABLE})
                continue
            try:
                if self._browser_state_refused(staging, relative, label=name):
                    not_uploaded.append(
                        {"name": name, "bytes": size, "reason": manifest_mod.BROWSER_STATE_REFUSED}
                    )
                    continue
                if self.log.has_secrets:
                    entry = self._redact_file(staging, label=name, withheld=True)
                    if entry is not None:
                        # Measured to hold a registered value it could not
                        # rewrite, or not examinable at all: kept in the pod.
                        # The agent never chose to deliver this file, so the
                        # artifacts folder's "upload it as-is and say so"
                        # does not apply (archived pull request 225 review).
                        not_uploaded.append(
                            {
                                "name": name,
                                "bytes": size,
                                "reason": (
                                    standalone_mod.HOLDS_SECRET
                                    if entry["secret_found"]
                                    else standalone_mod.UNEXAMINED
                                ),
                            }
                        )
                        continue
                final = staging.stat().st_size
                if total + final > byte_cap:
                    # Redaction can lengthen a file; the cap is on what is uploaded.
                    not_uploaded.append({"name": name, "bytes": final, "reason": standalone_mod.OVER_CAP})
                    continue
                try:
                    self.store.upload_file(key, staging)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(
                        "working-folder upload failed", artifact=self._scrub(name), error=str(exc)
                    )
                    not_uploaded.append(
                        {"name": name, "bytes": final, "reason": standalone_mod.UPLOAD_FAILED}
                    )
                    continue
            except OSError as exc:
                self.log.warning(
                    "could not prepare a working-folder file for upload",
                    file=self._scrub(name),
                    error=str(exc),
                )
                not_uploaded.append({"name": name, "bytes": size, "reason": standalone_mod.UNREADABLE})
                continue
            finally:
                staging.unlink(missing_ok=True)
            total += final
            uploaded.append({"name": name, "bytes": final, "uri": self.store.uri(key)})

        if ws.work.is_symlink():
            self.log.warning(
                "the working folder is a symlink, so nothing was uploaded from it; "
                "a link is never followed out of the workspace"
            )
        # EVERY NAME, IN THE LOG. The summary below lists the first 50 and
        # counts the rest (a Firestore document has a 1 MiB limit).
        self._log_not_uploaded(
            "files the agent created in its working folder were not uploaded",
            not_uploaded,
            cap_files=cfg.max_workdir_output_files,
            cap_bytes=cfg.max_workdir_output_bytes,
        )
        self.log.info(
            "uploaded what the agent created in its working folder",
            uploaded=len(uploaded),
            bytes=total,
            symlinks_skipped=len(found.symlinks),
        )
        return {
            "uploaded": uploaded,
            "bytes": total,
            "summary": {
                "prefix": f"{standalone_mod.PREFIX}/",
                "uploaded": len(uploaded),
                "uploaded_bytes": total,
                # The first 50; `not_uploaded_count` is the whole number and
                # the WARNING lines above name every one. "over cap" is the
                # owner's wording.
                "not_uploaded": not_uploaded[:50],
                "not_uploaded_count": len(not_uploaded),
                "symlinks_skipped": len(found.symlinks),
                "working_folder_is_symlink": ws.work.is_symlink(),
                "cap_files": cfg.max_workdir_output_files,
                "cap_bytes": cfg.max_workdir_output_bytes,
            },
        }

    def _log_not_uploaded(
        self, message: str, entries: Sequence[dict[str, Any]], **fields: Any
    ) -> None:
        """Name every file in `entries` in the worker's log, `LOG_BATCH` to a WARNING line.

        `entries` are `{"name", "reason", ...}`, and each is written as
        "<name>: not uploaded: <reason>". The ONE place both uploads name the
        files they did not upload: the working folder's (archived pull request 225 review: past 50
        a name was nowhere) and the artifacts folder's, past its 500-file cap
        (archived issue 228). A result summary lists at most the first 50 and counts the
        rest, because it is a Firestore document with a 1 MiB limit; these
        lines name all of them, and `LOG_BATCH` names to a line keep each well
        under Cloud Logging's 256 KiB entry however many files the agent left.
        Every line carries `count`, the whole number, `by_reason`, that number
        split by reason, and `batch`, "i of n". Nothing is written for no
        entries.

        BY REASON (archived issue 227). The artifacts folder's line counted every file it
        did not upload -- a name too long as well as one past the cap -- while
        the pane said "N over the 500-file cap" from `artifacts_over_cap`,
        which counts only the cap. Two numbers for one folder that disagreed
        with nothing to say why; `by_reason` states each.
        """
        batch = standalone_mod.LOG_BATCH
        batches = (len(entries) + batch - 1) // batch
        by_reason: dict[str, int] = {}
        for entry in entries:
            by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
        for index in range(batches):
            self.log.warning(
                message,
                count=len(entries),
                by_reason=by_reason,
                batch=f"{index + 1} of {batches}",
                files=[
                    f"{e['name']}: not uploaded: {e['reason']}"
                    for e in entries[index * batch : (index + 1) * batch]
                ],
                **fields,
            )

    def _declared_outputs(self, task: dict[str, Any]) -> tuple[str, ...]:
        """Honour `metadata.expected_outputs`: what later steps will stage from this one.

        Written by the API at workflow submission, on the UPSTREAM task (archived issue 149).
        An entry that cannot name a file in the artifacts directory is dropped
        and named in a warning rather than failing the attempt: no dependant
        could stage it either. A usable name that is missing when the runner
        finishes cleanly fails the attempt, retryably
        (`_fail_for_missing_outputs`). See `agent_worker.expected_outputs` for
        the measurements behind both.
        """
        # A review or re-review step owes `verdict.json` by its signed
        # `step_id`, whether or not a later step stages it (archived issue 2837).
        declared = expected_mod.declared_outputs(
            task.get("metadata"), step_id=task.get("step_id")
        )
        if declared.rejected:
            self.log.warning(
                "dropped unusable expected_outputs entries: "
                + ", ".join(repr(entry) for entry in declared.rejected),
                kept=list(declared.names),
            )
        if declared.names:
            self.log.info(
                "later steps will stage these artifacts from this task",
                expected_outputs=list(declared.names),
            )
        names = declared.names
        # Owed implies requested; both are asked so either alone decides.
        if PR_TITLE_FILE not in names and (self._title_owed(task) or self._title_requested(task)):
            # REQUIRED, NOT INVENTED (owner decision, 2026-09-28): a pull
            # request this attempt opens is titled by the agent. Owed as an
            # expected output, a CLI runner tells the agent to write it, and a
            # clean finish without it fails the attempt retryably (archived issue 149's
            # path) instead of opening an untitled pull request.
            names = (*names, PR_TITLE_FILE)
            self.log.info(
                "this attempt opens a pull request; the agent must write its title",
                file=PR_TITLE_FILE,
            )
        self._declared_names = tuple(declared.names)
        self._expected_outputs = names
        return names

    def _report_missing_outputs(
        self, summary: dict[str, Any], *, fails_the_attempt: bool
    ) -> list[str]:
        """Name the expected outputs this attempt did not upload, and return them (archived issue 149).

        One WARNING line naming them, and `result_summary[MISSING_SUMMARY_KEY]`,
        which the API serves with the task (`codec.task_to_api`). Written on
        every way `_finalise` ends, so a runner that failed on its own still
        says what it left out. `fails_the_attempt` is whether the runner
        finished cleanly, which decides what the line says it costs: then the
        attempt fails for it (`_fail_for_missing_outputs`), and otherwise it
        fails, or ends, for a cause of its own.

        Measured against what was UPLOADED, the `artifacts` manifest, not the
        directory listing. A dependant stages from that manifest, so a file
        that was written but not uploaded is missing as far as the dependant
        is concerned, and the line says which case it is.
        """
        if not self._expected_outputs:
            return []
        produced = [
            str(entry.get("name"))
            for entry in summary.get("artifacts") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        missing = expected_mod.missing_outputs(self._expected_outputs, produced)
        # Uploaded, but not a verdict (archived issue 2837): missing, with the cause
        # `_missing_causes` names, so a retry writes a readable one.
        self._verdict_unreadable = self._unreadable_verdict(missing)
        if self._verdict_unreadable:
            missing = [*missing, expected_mod.REVIEW_VERDICT_NAME]
        if summary.get(expected_mod.NO_CHANGE_SUMMARY_KEY) is True:
            # An allowed empty diff (2026-10-05) writes no patch and opens no
            # pull request, so neither the patch nor a title is owed; every
            # other expected output still is.
            missing = [name for name in missing if name not in (PATCH_NAME, PR_TITLE_FILE)]
        if (
            PR_TITLE_FILE in missing
            and PR_TITLE_FILE not in self._declared_names
            and not self._title_owed(self._task or {})
        ):
            # Requested, not owed (Proposal B, 2026-10-08): the agent had its
            # follow-up turn, and the issue titles the pull request with the
            # visible `[title missing] ` prefix (`_generated_pull_request_title`).
            # A dependant that declared the file still needs it.
            missing = [name for name in missing if name != PR_TITLE_FILE]
            self.log.warning(
                f"the agent wrote no {PR_TITLE_FILE}; the pull request is titled "
                f"from its issue, prefixed {TITLE_MISSING_PREFIX.strip()!r}",
            )
        if not missing:
            return []
        causes = self._missing_causes(summary)
        summary[expected_mod.MISSING_SUMMARY_KEY] = self._scrub(list(missing))
        summary[expected_mod.MISSING_CAUSES_SUMMARY_KEY] = self._scrub(
            [
                {"name": name, "cause": cause}
                for name, cause in expected_mod.causes_of(missing, causes).items()
            ]
        )
        if fails_the_attempt:
            consequence = (
                "The attempt fails for it, and is retried while the task has "
                "attempts left."
                if expected_mod.retryable(missing, causes)
                else "The attempt fails for it, and is not retried: another "
                "attempt would meet the same cause."
            )
        else:
            consequence = (
                "The runner did not finish cleanly, so the attempt ends on its own cause."
            )
        self.log.warning(
            expected_mod.missing_line(missing, causes=causes, consequence=consequence),
            missing=list(missing),
            causes=self._scrub(expected_mod.causes_of(missing, causes)),
        )
        return list(missing)

    def _unreadable_verdict(self, missing: Sequence[str]) -> bool:
        """Whether an owed `verdict.json` was uploaded but does not read as a
        verdict (`expected_outputs.verdict_problem`, archived issue 2837), logged with why.

        Only when it is owed and was not already missing: a file never
        written, or one not uploaded, keeps the cause it has.
        """
        name = expected_mod.REVIEW_VERDICT_NAME
        if name not in self._expected_outputs or name in missing or self.ws is None:
            return False
        problem = expected_mod.verdict_problem(self.ws.artifacts)
        if problem is None:
            return False
        self.log.warning(f"the review's verdict does not read: {problem}")
        return True

    def _missing_causes(self, summary: dict[str, Any]) -> dict[str, str]:
        """Each name the last upload did not upload, with WHY (archived issue 165).

        The skipped files' causes (`_skip_causes`: the byte cap, an upload
        error, a name or a file refused), the files the 500-file cap or the
        name bound left out (archived issue 228), and `swarm-work.patch` when the harvest did
        not write it: an empty diff, a patch over its cap, no clone base
        (`expected_mod.patch_cause`). A name in none of them was not written,
        which a retry can change; so can an upload error, and nothing else
        here (`expected_mod.retryable`).
        """
        causes = {**self._not_uploaded_causes, **self._skip_causes}
        if self._verdict_unreadable and expected_mod.REVIEW_VERDICT_NAME not in causes:
            causes[expected_mod.REVIEW_VERDICT_NAME] = expected_mod.CAUSE_UNREADABLE_VERDICT
        if (self._security_review or {}).get("error"):
            # Not retryable: the step fails, its validator's message in the summary.
            causes[expected_mod.REVIEW_VERDICT_NAME] = security_review_mod.CAUSE_INVALID
        if PATCH_NAME in self._expected_outputs and PATCH_NAME not in causes:
            patch = expected_mod.patch_cause(summary.get("git"))
            if patch is not None:
                causes[PATCH_NAME] = patch
        return causes

    def _publish_withheld(
        self,
        ran_clean: bool,
        *,
        missing: Sequence[str] = (),
        title_refused: str | None = None,
    ) -> str | None:
        """Why this attempt must not publish, or None when it may (archived issue 149, archived issue 165).

        Decided AFTER the upload, from the manifest the missing-output check
        read (`missing`), and before anything is pushed (`_publish_checked`).

        A MISSING EXPECTED OUTPUT WITHHOLDS THE PUBLISH ON EVERY ATTEMPT, the
        last one included (archived issue 165, owner decision 2026-09-28): the attempt is
        failed, retryably or not, and an attempt that is failed or retried has
        published nothing. Before, the publish ran first, from a directory
        listing, so a file present and then not uploaded (the cap, an upload
        error) published a branch and a pull request for an attempt the check
        then failed, and the last attempt published whatever it had.

        A refused pull-request title withholds only from an attempt that will
        run again, as before: the last one pushes its branch and opens no pull
        request (`_publish_git`). The count comes from the task this worker
        fetched when it started; the transaction that fails the attempt reads
        it again and decides (`control.fail_retryably`).

        Only an attempt whose runner finished cleanly is gated here: one that
        failed, timed out or was stopped ends on its own cause, unchanged.
        """
        if not ran_clean:
            return None
        if missing:
            return (
                "this attempt failed because expected outputs are missing ("
                + ", ".join(missing)
                + "); nothing is published by an attempt that is failed or retried"
            )
        if title_refused is None:
            return None
        task = self._task or {}
        if retries_exhausted(
            int(task.get("attempt_count", 0)), int(task.get("max_attempts", 3))
        ):
            return None
        return (
            f"this attempt is retried because {title_refused}; publishing "
            f"waits for the attempt that writes a usable one"
        )

    def _fail_for_missing_outputs(
        self, missing: list[str], summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this attempt, retryably, with the missing names as its cause (archived issue 149).

        The owner's decision on archived issue 149 (2026-09-25T10:30Z), which replaces the
        report-only behaviour this change first shipped: the dependant never
        starts on a parent that did not write what it promised. The task goes
        back to READY while it has attempts left, and ends FAILED once
        `max_attempts` is spent, whereupon the scheduler cancels its dependants
        as for any failed parent. A requested cancel ends it CANCELLED.

        `exit_code` is the RUNNER's, 0, kept on the attempt as it is for a
        runner that exited 0 without writing result.json. The worker exits 1:
        the attempt failed, whatever state the task was left in.

        The retry starts with an empty artifacts directory, because only
        `work/` is checkpointed, so it must write every expected output again.

        NOT RETRIED WHEN A RETRY CANNOT HELP (archived issue 165, owner decision
        2026-09-28): a missing output whose cause is the artifact cap, a file
        refused at read, or a `swarm-work.patch` the harvest could not write
        (an empty diff, a patch over its cap, no clone base) ends the task
        FAILED now, whatever attempts are left. The next attempt would write
        the same file into the same cap, against the same repository, and
        spend its compute and provider quota to fail the same way. Only a file
        never written, or one whose upload raised, is retried.
        """
        causes = self._missing_causes(summary)
        error = self._scrub(expected_mod.missing_error(missing, causes=causes))
        if not expected_mod.retryable(missing, causes):
            self._finish_terminal(
                state=TaskState.FAILED,
                exit_code=exit_code,
                error=error,
                result_summary=summary,
                end_cause=EndCause.OUTPUTS_MISSING,
            )
            self.log.info(
                "the attempt failed for missing expected outputs a retry cannot produce",
                missing_count=len(missing),
                causes=self._scrub(expected_mod.causes_of(missing, causes)),
            )
            return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)
        state = self._fail_retryably_terminal(
            exit_code=exit_code,
            error=error,
            cause="expected_outputs_missing",
            result_summary=summary,
            retry_delay_seconds=EXPECTED_OUTPUT_RETRY_DELAY_SECONDS,
            detail={"missing": self._scrub(list(missing))},
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.info(
            "the attempt failed for missing expected outputs",
            task_state=state.value,
            missing_count=len(missing),
        )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _fail_for_refused_title(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this attempt, retryably, because its `pr-title.txt` was refused.

        The owner's decision of 2026-09-28: the same outcome as a missing
        expected output (`_fail_for_missing_outputs`), with the refusal as the
        cause, so the retry is told what to fix. `reason` quotes none of the
        title. The retry starts with an empty artifacts folder and must write
        the title again.
        """
        error = self._scrub(reason)
        state = self._fail_retryably_terminal(
            exit_code=exit_code,
            error=error,
            cause="pull_request_title_refused",
            result_summary=summary,
            retry_delay_seconds=EXPECTED_OUTPUT_RETRY_DELAY_SECONDS,
            detail={"refused": error},
            # Contract request 29, applied 2026-10-02: the worker refused to
            # publish, which is neither a missing output nor a runner error.
            end_cause=EndCause.PUBLISH_REFUSED,
        )
        self.log.info("the attempt failed for a refused pull request title", task_state=state.value)
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _fail_for_refused_index(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail an index run whose repo-index.json promotion would refuse (archived issue 1693).

        The agent was told the errors and given one turn to fix them
        (`cliagent.INDEX_REPAIR_MAX_TURNS`); `reason` is the same lines,
        `parse_index`'s wording. FAILED now, not retried: the next scheduled
        index run starts from a fresh extractor pass, and a retry of this
        attempt would spend the extractor and the language servers again
        for the same agent's reading. The end cause is the missing output's,
        which this is: the one document the run owes is not usable.
        """
        error = self._scrub(reason)
        self._finish_terminal(
            state=TaskState.FAILED,
            exit_code=exit_code,
            error=error,
            result_summary=summary,
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.info("the attempt failed: promotion would refuse its repo-index.json",
                      reason=error)
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _fail_for_final_tree_leak(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this attempt, retryably, because the branch it would push adds a
        credential (owner decision, 2026-09-28, archived pull request 259 review M4).

        `reason` comes from `final_tree_leak`: it names the file and never the
        value, so it is safe as the attempt's error, which is what tells the
        retry what to remove. Nothing was pushed (`_publish_git` returned
        before the publish repository was made). The retry resumes from the
        final checkpoint, taken before the worker's commit of uncommitted work,
        so the agent's files are there to edit.
        """
        error = self._scrub(reason)
        state = self._fail_retryably_terminal(
            exit_code=exit_code,
            error=error,
            cause="final_tree_adds_a_credential",
            result_summary=summary,
            retry_delay_seconds=EXPECTED_OUTPUT_RETRY_DELAY_SECONDS,
            detail={"refused": error},
            # Contract request 29 (owner decision 2026-09-29, applied
            # 2026-10-02): the worker refused to publish.
            end_cause=EndCause.PUBLISH_REFUSED,
        )
        self.log.info("the attempt failed: its final tree adds a credential", task_state=state.value)
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _fail_for_auto_commit_undid(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """End FAILED, and not retried, because the worker's commit of the
        agent's uncommitted work would have undone the agent's commits (archived issue 2536).

        Measured on task_6e4f6aa4: the branch's net diff was empty and the
        step read SUCCEEDED, so the re-review met the same blocker and the
        merge refused. Not retried: the retry resumes the same working tree
        and would meet the same auto-commit. PUBLISH_REFUSED, as for the
        final-tree leak: the worker refused to publish (contract request 29).
        """
        error = self._scrub(reason[:4000])
        self._finish_terminal(
            state=TaskState.FAILED,
            exit_code=exit_code,
            error=error,
            result_summary=summary,
            end_cause=EndCause.PUBLISH_REFUSED,
        )
        self.log.error("the auto-commit undid the agent's commits; nothing was published",
                       reason=error)
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _opens_pull_request(self) -> bool:
        """True when this step's job is to OPEN A PULL REQUEST (GUARD 2, 2026-10-02).

        A `direct-pr` task or step, the `integrate` integrator, and a
        `single-pr` author. NOT an `integrate` contributor (its branch is its
        deliverable), a `single-pr` reader or amender, a `collect` task or a
        worker action: for those, changing nothing is a correct outcome -- a
        review that edits no file is a review that did its job. A contributor,
        an amender and a `carrier: branches` step still owe their BRANCH when
        they changed something: `_deliverable_branch` (G1, archived issue 1225).

        The strategy is read raw, not through `_dispatch_strategy`, which
        reads `single-pr` as `collect` (`_publish_git` reads its role through
        `_pr_role` instead): an author that changed nothing is still a step
        that owed a pull request.
        """
        block = self._dispatch_block()
        raw = block.get("strategy")
        strategy = raw.strip().lower() if isinstance(raw, str) else ""
        if strategy == "direct-pr":
            return True
        if strategy == "integrate":
            return self._dispatch_role() == "integrator"
        if strategy == "single-pr":
            role = block.get("pr_role")
            return isinstance(role, str) and role.strip().lower() == "author"
        return False

    def _deliverable_branch(self) -> str:
        """Why this step's PUSHED BRANCH is what it delivers, or "" (G1, archived issue 1225).

        Wider than `_opens_pull_request`, which stays what it says: a step
        that owes a pull request. A branch is also the deliverable of

          * an `integrate` contributor: the integrator merges `swarm/<its id>`
            and a review that `builds_on` it clones that branch;
          * a `carrier: branches` step, under any strategy but a `single-pr`
            reader's: the branch is where the next step finds the work;
          * a `single-pr` amender: its fast-forward push IS the pull
            request's update.

        A step another step `builds_on` is one of these already: swarm-api
        accepts `builds_on` only under `direct-pr` and `integrate`
        (`validation._EVERY_STEP_PUSHES_STRATEGIES`), where every step that
        runs an agent opens a pull request or is a contributor. Measured
        (archived issue 1225, wf_e55610ded71c4d9d9fae, wf_0260c920acf8414395b2): a review
        failed cloning "Remote branch swarm/task_... not found" behind a build
        step that ended SUCCEEDED having pushed nothing.
        """
        if self._opens_pull_request():
            return "the step was to open a pull request"
        pr_role = self._pr_role()
        if pr_role == "amender":
            return (
                "the step is the 'single-pr' amender, whose push to the author's "
                "branch is its update of the pull request"
            )
        if pr_role in ("reader", "none"):
            # `_publish_git` pushes nothing for these, whatever the carrier.
            return ""
        if self._dispatch_carrier() == "branches":
            return "the step carries its work on its branch (carrier: branches)"
        if self._dispatch_strategy() == "integrate" and self._dispatch_role() == "contributor":
            return (
                "the step is an 'integrate' contributor, whose branch the integrator "
                "merges and a step that builds on it clones"
            )
        return ""

    def _published_nothing(self, summary: dict[str, Any]) -> str | None:
        """Why a pull-request step that ran clean delivered nothing, or None.

        The measured failure (workflow wf_b9b337e107494c10a416, 2026-10-02):
        the fix step's staged patch did not apply to a moved `main`, its agent
        finished, the forge answered "No commits between main and swarm/...",
        and the step and the workflow both read SUCCEEDED with no pull request
        -- the implementation was lost and no state said so. Two shapes:

          * the harvest found no commit and no uncommitted change beyond the
            clone's base (`_harvest_git`'s "changed nothing"), so there was
            nothing to push or open;
          * the forge refused the pull request (`pull_request_refused`, set by
            `_publish_git`), quoted verbatim, already masked.

        A step whose verdict gate kept its agent from running is exempt: the
        gate deciding there is nothing to do is the outcome it exists for.
        "No usable title" and an unreadable default branch are not here:
        the first is the refused-title failure, the second pushed a branch.
        """
        if not self._opens_pull_request():
            return None
        if self._verdict is not None and not self._verdict.get("agent_ran", True):
            return None
        if summary.get(expected_mod.NO_CHANGE_SUMMARY_KEY) is True:
            # `allow_empty_diff` (2026-10-05): changing nothing is this
            # step's result, and there was nothing to open a pull request for.
            return None
        git = summary.get("git")
        if not isinstance(git, dict):
            return None
        refused = git.get("pull_request_refused")
        if refused:
            return f"published_nothing: {refused}"
        if git.get("head_moved"):
            # Its own words (archived issue 2094): the step's agent left its branch, which
            # is not "no commits beyond the base".
            return f"published_nothing: {git.get('publish_reason')}"
        unopened = git.get(PULL_REQUEST_NOT_OPENED_FIELD)
        if unopened:
            # Its own words, not "published_nothing": the branch IS pushed,
            # and the error says so with its name (archived issue 1623).
            return str(unopened)
        if git.get(PUBLISH_FAILED_FIELD) is True:
            # Refused or never ran (issue 284): `_publish_failed` names why.
            return None
        if (
            git.get("published") is False
            and git.get("commit_count") == 0
            and git.get("dirty_count") == 0
        ):
            base = git.get("base") or "its base"
            return (
                "published_nothing: the step was to open a pull request and its "
                f"branch has no commits beyond {base}"
            ) + self._artifacts_remedy(summary)
        return None

    def _artifacts_remedy(self, summary: dict[str, Any]) -> str:
        """The clause `_published_nothing` adds when the step's agent left
        artifacts of its own: how many, up to five names, and the declaration
        that makes them the deliverable (part of archived issue 2688).

        Measured 2026-11-07: seven read-only analysis steps under `direct-pr`
        each wrote triage.tsv and a summary, committed nothing as told, and
        FAILED with an error that named neither their artifacts nor
        `allow_empty_diff`. Names only from `summary["artifacts"]`, which the
        upload built before the finish; the worker's own files (the pull
        request's title and body, the hand-off note, the work patch) and the
        runner's captured streams are not the agent's result and are not
        counted. A name outside a plain file-name shape is counted, never
        shown, so the error carries nothing the agent chose to put in it.
        """
        own = {PR_TITLE_FILE, PR_BODY_FILE, "handoff.md", PATCH_NAME}
        streams = agent_stream_files(self.cfg.runner_profile)
        if streams is not None:
            own.update(streams.names())
        names = [
            str(entry.get("name"))
            for entry in summary.get("artifacts") or []
            if isinstance(entry, dict) and entry.get("name")
            and str(entry.get("name")) not in own
        ]
        if not names:
            return ""
        shown = [n for n in names if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", n)][:5]
        listed = f" ({', '.join(shown)})" if shown else ""
        return (
            f"; it produced {len(names)} artifacts{listed}; a step whose deliverable "
            "is its artifacts is submitted with allow_empty_diff: true"
        )

    def _publish_failed(self, summary: dict[str, Any]) -> str | None:
        """Why a step whose branch is its deliverable ended without pushing it,
        naming where the work is, or None (archived issue 872; G1, archived issue 1225).

        `_publish_git` sets `publish_failed` when git failed the publish --
        the push itself, or the commit, fold or authorship check before it.
        Measured 2026-10-08 (task_6a0c9afdbaea449eb53d): the push failed with
        exit 1, the agent had exited 0, and the step and its workflow read
        SUCCEEDED with the work only in `swarm-work.patch`.

        archived issue 872 covered only a step that opens a pull request. G1 (archived issue 1225) widens
        it to every step whose branch is its deliverable
        (`_deliverable_branch`), and to every way such a step can end with
        work and no pushed branch, not only a git failure: a publish that
        declined for a token that cannot push (`can_push` false), a refused
        git token, a forge this worker cannot publish to, publishing
        disabled, a harvest that failed or met survivors of the reap, or the
        catch-all in `_publish_checked`. Each read SUCCEEDED, and the step
        that builds on it then failed cloning a branch that was never there.

        Not here: a step that changed nothing (`published_nothing`, and the
        allowed empty diff), which had no branch to push and whose dependants
        already step past it; a shut verdict gate's step, which
        `_skipped_publish_missing` answers; a forge outage -- at the probe,
        the pull request or, since archived issue 2833, the push itself
        (`gitops.PushUnreachable`) -- which `_publish_unreachable` fails
        retryably. The forge-write refusal and
        the final-tree leak are answered before this is asked. A `collect`
        step without a carrier, a `single-pr` reader and a step with no
        repository deliver no branch and never reach here.
        """
        why = self._deliverable_branch()
        if not why:
            return None
        git = summary.get("git")
        if not isinstance(git, dict):
            return None
        # `_publish_git` says `published: true` only once the push returned.
        if git.get("published") is True:
            return None
        if git.get(PUBLISH_FAILED_FIELD) is not True:
            if summary.get(expected_mod.NO_CHANGE_SUMMARY_KEY) is True:
                return None
            if published_nothing(summary):
                return None
            if self._verdict is not None and not self._verdict.get("agent_ran", True):
                return None
            if isinstance(git.get(PUBLISH_UNREACHABLE_FIELD), dict):
                return None
        cause = str(git.get("publish_reason") or git.get("error") or "the push failed")
        patch = next(
            (
                entry.get("uri")
                for entry in summary.get("artifacts") or []
                if isinstance(entry, dict) and entry.get("name") == PATCH_NAME and entry.get("uri")
            ),
            None,
        )
        if patch:
            where = (
                f"The work is kept in {patch}; recover it with swarm_apply "
                f"(task {self.cfg.task_id})."
            )
        else:
            where = (
                f"No {PATCH_NAME} was uploaded for this attempt"
                + (f" ({git['patch_note']})" if git.get("patch_note") else "")
                + "; the work is in the attempt's final checkpoint."
            )
        bundle = next(
            (
                entry.get("uri")
                for entry in summary.get("artifacts") or []
                if isinstance(entry, dict) and entry.get("name") == WORK_BUNDLE_NAME
                and entry.get("uri")
            ),
            None,
        )
        if bundle and git.get("bundle") == WORK_BUNDLE_NAME:
            # The branch itself, merges included (archived issue 3174): what a re-dispatch
            # or a hand push takes, where the patch is the final tree flat.
            where += (
                f" The agent's branch, merge commits included, is kept as a git bundle in "
                f"{bundle} (ref {git.get('bundle_ref') or 'swarm-work'}, on "
                f"{git.get('bundle_base')})."
            )
        located = credential_locator(cause)
        if located:
            # A REFUSAL A BRIEF CAN BE CORRECTED FROM (part of archived issue 2524): which
            # scan rule fired and at which `path:line`, said first and in the
            # shape `scripts/publish-scan.sh` prints, so it survives whatever
            # cut the cause meets downstream. Never the matched text.
            cause = f"the pre-publish credential scan refused it ({located}): {cause}"
        return f"publish_failed: {why} and its branch was not pushed: {cause}. {where}"

    def _skipped_publish_missing(self, summary: dict[str, Any]) -> str | None:
        """Why a step whose verdict gate skipped its agent pushed no branch, or None (PUB-SKIP).

        Measured 2026-10-12 (wf_18f0b5e64639417f83d6, task_58ecd704c73a433b9782):
        the review said MERGE, the fix step skipped its agent and ended
        SUCCEEDED with no push and no error, and the re-review that builds on
        it failed cloning its branch. On the skip path the step's only
        deliverable is the reviewed branch, published, and `_published_nothing`
        exempts a shut gate -- so every way the publish can decline (a token
        that may not push, a refused git token, a forge this worker cannot
        publish to, a harvest that returned early) read as success. Now a
        skip-path step that owes a pull request (`_shut_gate_owes_publish`)
        and did not push its branch has this reason, quoting the publish's
        own, and ends FAILED. A step whose agent ran is unchanged.
        """
        if not self._shut_gate_owes_publish():
            return None
        git = summary.get("git")
        if isinstance(git, dict) and git.get("published") is True and git.get("branch"):
            return None
        if not isinstance(git, dict):
            cause = "no repository was harvested, so there was nothing to push"
        else:
            cause = str(
                git.get("publish_reason") or git.get("error")
                or "the publish recorded no reason"
            )
        return (
            f"publish_failed: the review verdict was {self._verdict['verdict']}, so this "
            f"step's agent did not run and the step was to publish the reviewed work, "
            f"but its branch was not pushed: {cause}"
        )

    def _mark_publish(self, summary: dict[str, Any], *, failed: str | None = None) -> None:
        """`published` or `publish_failed` on the task's events (PUB-SKIP).

        `published` when this attempt's publish pushed its branch: the branch,
        the head pushed and the pull request, if one was opened or reused, on
        both paths, so a reader of the events alone can tell a step that
        delivered from one that only said SUCCEEDED. `publish_failed`, with the
        reason, when the finish fails the step for a publish that did not
        happen. A RUNNING mark with a `cause`, as every other note here is
        (`_emit_startup_mark`): the frozen `EventType` has no note-only type.
        """
        verdict = self._verdict
        agent_ran = True if verdict is None else bool(verdict.get("agent_ran", True))
        if failed is not None:
            self._emit_startup_mark(
                "publish_failed", {"reason": failed, "agent_ran": agent_ran}
            )
            return
        git = summary.get("git")
        if not (isinstance(git, dict) and git.get("published") is True and git.get("branch")):
            return
        pull = git.get("pull_request")
        self._emit_startup_mark(
            "published",
            {
                "branch": git.get("branch"),
                "head": git.get("pushed_head"),
                "pull_request": (
                    {"number": pull.get("number"), "url": pull.get("url"),
                     "created": pull.get("created")}
                    if isinstance(pull, dict) else None
                ),
                "agent_ran": agent_ran,
            },
        )

    def _publish_unreachable(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        """The forge outage that kept a pull-request step from opening its pull
        request, or None (F1, 2026-10-04).

        Set by `_publish_git` only for a `ForgeUnavailable` that outlasted
        `retry_transient` -- a timeout, reset, DNS failure, 429, 5xx or
        rate-limit 403 at the probe or at `open_pull_request`. A forge that
        ANSWERED (a 422 refusal, a 404, a plain 403) is not here: the refusal
        is `_published_nothing`'s, and the rest stay the publish's recorded
        reason. Only a step whose branch is its deliverable
        (`_deliverable_branch`, widened from `_opens_pull_request` by G1,
        archived issue 1225) is failed for it: one that owes a pull request, or one whose
        outage at the probe kept its branch from being pushed. Any other
        step's deliverable is its artifacts, which an outage at the forge's
        API does not touch.
        """
        if not self._deliverable_branch():
            return None
        git = summary.get("git")
        if not isinstance(git, dict):
            return None
        if not self._opens_pull_request() and git.get("published") is True:
            return None
        marker = git.get(PUBLISH_UNREACHABLE_FIELD)
        return marker if isinstance(marker, dict) else None

    def _unreachable_marker(self, exc: forge_mod.ForgeUnavailable) -> dict[str, Any]:
        """What `_publish_git` records of an outage: scrubbed, bounded, no query string."""
        return {
            "reason": str(self._scrub(forge_mod.loggable(str(exc)))),
            "tries": int(exc.tries),
            "retry_after_seconds": exc.retry_after_seconds,
        }

    def _fail_for_published_nothing(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """End a pull-request step that delivered nothing FAILED, and not retried.

        The non-retryable path `_fail_for_missing_outputs` takes for a
        missing output a retry cannot produce: `finish(FAILED)` ends the task
        for good whatever attempts are left. Deterministic -- the next attempt
        clones the same base, runs the same prompt and meets the same forge --
        so a retry would spend compute and provider quota to repeat it. The
        workflow rollup then reads FAILED with this step's `last_error`, and
        `on_step_failure` acts on it as on any failed step.

        OUTPUTS_MISSING, the closest existing end cause: the step's promised
        deliverable, its pull request, does not exist. PUBLISH_REFUSED (request
        29) is the WORKER refusing to publish, which this is not -- here the
        agent produced nothing, or the forge said no. Its own cause,
        `PUBLISHED_NOTHING`, is contract request 46, not applied.
        """
        error = self._scrub(reason[:4000])
        self._finish_terminal(
            state=TaskState.FAILED,
            exit_code=exit_code,
            error=error,
            result_summary=summary,
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.error("the step was to open a pull request and published nothing", reason=error)
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _leaks_in_added_text(self, path: str, text: str) -> CredentialHit | None:
        """The leak test both publish scans apply to the text a file ADDS.

        A registered secret, in ANY file -- the scrub replaces exactly the
        registered values, so "the scrub changed it" is "a registered value
        is in it", and it is never relaxed for a test path (archived issue 373) -- or a
        credential-shaped literal (`_credential_in`, tiered by `path`), which
        catches a key the agent minted or pasted and nobody registered.

        The per-commit scan and the final-tree scan both call this, with the
        file's path, so the two decide the same way for the same file.
        """
        scrubbed = str(self._scrub(text))
        if scrubbed != text:
            return CredentialHit(REGISTERED_SECRET_RULE, _first_difference(text, scrubbed))
        return _credential_in(path, text)

    def _scan_overlap(self) -> int:
        """How far the leak scan's windows overlap: the longest credential
        match (`SCAN_OVERLAP_CHARS`) plus the longest registered secret, so a
        value straddling a window's edge is whole in the next window."""
        secrets = getattr(self.log, "_secrets", None) or ()
        return SCAN_OVERLAP_CHARS + max((len(s) for s in secrets), default=0)

    def _clone_base_path(self) -> Path | None:
        ws = self.ws
        if ws is None:
            return None
        return ws.work / WORKER_STATE_DIR / CLONE_BASE_FILE

    def _write_clone_base(self, commit: str | None) -> None:
        path = self._clone_base_path()
        if path is None or not commit:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(commit.strip() + "\n")
        except OSError as exc:
            # Losing the marker costs the harvest its diff base on a RESUMED
            # attempt only; the first attempt still has `clone.commit` in
            # memory. Not worth failing a run that is otherwise fine.
            self.log.warning("could not record the clone base", error=str(exc))

    def _read_clone_base(self) -> str | None:
        path = self._clone_base_path()
        if path is None or not path.exists():
            return None
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            return None
        return text or None

    def _git_token(self, *, write: bool = False) -> str | None:
        """The TASK's forge token, or None.

        Read from `swarm-tenant-<tenant>-<suffix>`, the suffix being the task's
        signed `forge_credential` (`_forge_suffix`; `git`, the tenant's own
        token, when it names none), through the same per-tenant Secret Manager
        path as a provider key, never from a platform-wide `GIT_TOKEN` in the
        worker's environment: one token able to clone every tenant's
        repositories would make a single malicious repository in one tenant a
        credential compromise for all of them (invariant 9).

        READ AGAIN AT EVERY CALL, never cached: the clone, each carrier push
        and the publish each call this, so each reads the `latest` version at
        that moment. A user token lives 8 hours and an attempt can outlive it;
        swarm-api's refresher keeps the latest version at least two hours from
        expiry (docs/onboarding.md §3.3 step 3).

        None is not an error. A public repository clones without a credential
        and a private one fails with git's own message, which is the correct
        diagnosis to surface.

        Nor is it read at all when `_git_token_refusal` names a reason: once
        read, the token stays in this process's heap for the rest of the
        attempt, next to an agent that may be able to read it there.

        A PERSON'S SLOT comes from swarm-api's broker, never Secret Manager
        (archived issue 1041, `forgebroker`) -- except in a deployment with no broker, where
        the worker's own account reads it while IAM still allows
        (`ForgeBroker.deployed`) -- asked for `read`: every push and pull request
        first asks `_forge_write_refusal`, which re-reads the grant for a
        write, before it calls this. A refusal is None here -- no token, and
        never the tenant's in its place, so a private repository fails with
        git's own message and a push has no credential. A superseded attempt
        raises `FencedError` and stands down. The merge, a write with no such
        gate of its own, asks the broker for `write` (`_read_action_git_token`).

        `write` (archived issue 2907): this read comes before a push or a pull request -- the
        publish, publish_to_pr, the push-scope and carrier-target probes, the
        CI fix start check, the no-change comment, and their re-reads after a
        401 -- and the token it returns is the one that write uses. It changes
        only A SERVICE RUN's ask (`_service_run`) whose SIGNED `forge_access`
        is exactly `write`, the condition swarm-api's `decide_run_token` mints
        `RUN_WRITE_PERMISSIONS` on: without it the broker minted the read-only
        run token and the probe refused the push ("an App installation token
        whose receive-pack advertisement GitHub refused (HTTP 403)"). A run
        signed `read`, or with no `forge_access`, still asks for `read`, and a
        person's run still asks for `read`, its write gated by
        `_forge_write_refusal`, so no run can obtain more than it could. A write
        the broker refuses as a write (`ForgeWriteRefused`) is asked again for
        `read`, today's token, and the probe refuses the push as it did.
        """
        self._git_token_error = None
        refusal = self._git_token_refusal()
        if refusal:
            self.log.error("not reading the tenant git credential", reason=refusal)
            self._forge_credentials_read.append(("none", None))
            return None
        suffix = self._forge_suffix()
        app_installation = self._service_run()
        if self.secret_client is None and user_slot_hash(suffix) is None \
                and not app_installation:
            self._forge_credentials_read.append(("none", None))
            return None
        source: str | None = None
        try:
            tenant = load_tenant(
                self.db, self.cfg.tenant_id, call_options=self.control.call_options()
            )
            source = tenant.secret_name(suffix)
            as_write = write and app_installation \
                and (self._task or {}).get("forge_access") == FORGE_WRITE
            try:
                token = resolve_git_token(
                    tenant=tenant, client=self.secret_client, logger=self.log,
                    provider=suffix, broker=self.forge_broker, write=as_write,
                    app_installation=app_installation,
                )
            except ForgeWriteRefused as exc:
                if not as_write:
                    raise
                self.log.warning("the broker refused this run a write token; reading it "
                                 "for read", cause=exc.cause)
                token = resolve_git_token(
                    tenant=tenant, client=self.secret_client, logger=self.log,
                    provider=suffix, broker=self.forge_broker,
                    app_installation=app_installation,
                )
            # The KIND, classified here in memory; the value goes no further
            # than the caller. A tenant with no `git` slot read nothing.
            self._forge_credentials_read.append(
                (forge_credential_kind(token), source if token else None)
            )
            return token
        except SecretError as exc:
            self._forge_credentials_read.append(("none", source))
            # Why, for a caller that must say it (`_open_pull_request_reading_
            # again`): a `SecretError` names the secret and the broker's code,
            # never a value.
            self._git_token_error = str(exc)
            self.log.warning(
                "no usable tenant git credential; cloning unauthenticated",
                error=str(exc),
            )
            return None

    def _forge_credential_summary(self) -> dict[str, Any] | None:
        """`result_summary.forge_credential`: which KIND of GitHub credential
        this attempt cloned and pushed with, and the secret NAME it came from.

        Lesson 43 (docs/design/day-one/lessons.md): a verification run must
        use a GitHub App installation token, never a personal access token;
        stage 14 of `swarm install` reads this to check it. `kind` is one of
        `secrets.FORGE_CREDENTIAL_KINDS`, classified in memory from GitHub's
        documented prefix (`secrets.forge_credential_kind`); `source` is
        `swarm-tenant-<tenant>-<suffix>` or None when nothing was read. Every
        read counts: the clone and each push read the latest version, so reads
        of two kinds are `mixed` with `kinds` listing them, never the first.
        NEVER the token, a prefix of it, a piece of it or a hash of it.

        None when this attempt never asked for a forge credential (no
        repository), so a summary says nothing it did not measure.
        """
        reads = self._forge_credentials_read
        if not reads:
            return None
        kinds = sorted({kind for kind, _ in reads})
        sources = [source for _, source in reads if source]
        found: dict[str, Any] = {"kind": kinds[0] if len(kinds) == 1 else "mixed"}
        if len(kinds) > 1:
            found["kinds"] = kinds
        found["source"] = sources[-1] if sources else None
        return found

    def _record_forge_credential(self, summary: dict[str, Any]) -> None:
        """Write `_forge_credential_summary` into `summary` again, after a push.

        The upload takes the summary BEFORE the deferred publish pushes, and
        the push reads the slot again (`_git_token`): without this, a slot
        rotated to a PAT between clone and push would still say
        `app_installation`, and stage 14 would pass it.
        """
        forge_credential = self._forge_credential_summary()
        if forge_credential is not None:
            summary["forge_credential"] = forge_credential

    def _stage_issue(
        self, issue_number: int, work_dir: Path, prefetch: issue_mod.IssuePrefetch | None
    ) -> Path:
        """`issue_mod.stage_issue`, read again once on a 401 (owner decision 2026-10-08).

        MEASURED 2026-10-08: a task failed "could not fetch archived issue 780: the
        forge refused the credential (401: Bad credentials)" -- a refresh had
        ended the version it read. The retry reads the slot's latest version
        (`_git_token`) and fetches anew, never the failed prefetch. A second
        401 is an `IssueUnavailable` saying the token was re-read and refused
        again (`secrets.FORGE_REFUSED_TWICE`), ending the task as before.
        """

        def stage(again: bool) -> Path:
            usable = None if again else prefetch
            return issue_mod.stage_issue(
                number=issue_number,
                repository_url=self._repo_url,
                token=usable.token if usable is not None else self._git_token(),
                refusal=self._git_token_refusal(),
                work_dir=work_dir,
                scrub=self._scrub,
                logger=self.log,
                on_request=self._heartbeat,
                retry=self._forge_retry(),
                prefetched=usable,
            )

        return call_reading_again(
            stage,
            logger=self.log,
            what="the issue fetch",
            twice=lambda exc: issue_mod.IssueUnavailable(
                f"{exc}; {FORGE_REFUSED_TWICE}"
            ),
            refused=self._refused_credential,
        )

    def _requested_pull_request(self) -> int | None:
        """The pull request this step works on, or None (a peer application, 2026-11-06).

        In order: the caller's `input.pull_request`; the signed `publish_to_pr`
        it publishes onto; the pull request its signed `builds_on` step -- or,
        for a single-pr reader or amender, its `pr_author` step -- recorded
        opening: a review step's, in a workflow that has one. Only
        for a step with a brief (a `prompt`) to name the file in. Worked out once.
        """
        if self._pull_request_number is not False:
            return self._pull_request_number
        number: int | None = None
        task = self._task or {}
        if pull_request_mod.takes_brief(task.get("input")):
            number = pull_request_mod.from_input(task.get("input"), self.cfg.profile)
            if number is None:
                try:
                    onto = self._publish_to_pr()
                except WorkerError:
                    onto = None
                number = onto.number if onto is not None else None
            if number is None and task.get("workflow_id"):
                try:
                    upstream = self._dispatch_builds_on()
                    if not upstream and self._pr_role() in ("reader", "amender"):
                        # A single-pr reader or amender: the pull request is
                        # the one its signed `pr_author` step opened.
                        upstream = self._pr_author_branch()[len(self.cfg.git_branch_prefix):]
                    if upstream:
                        number = post_verdict_mod.pull_request_number(
                            inputs_mod.fetch_upstream_task(
                                self.db, upstream_task_id=upstream,
                                tenant_id=self.cfg.tenant_id,
                                call_options=self.control.call_options(),
                            )
                        )
                except (WorkerError, InputUnavailable):
                    number = None
        self._pull_request_number = number
        return number

    def _stage_pull_request(self, number: int, work_dir: Path) -> Path:
        """`pull_request_mod.stage_pull_request` with the tenant's own `-git`
        token, read again once on a 401 as the issue fetch is; scrubbed of
        every registered secret and every credential-shaped run."""
        refusal = self._git_token_refusal()

        def token(again: bool) -> str | None:
            value = self._git_token()
            if value:
                self.log.register_secret(value)
            return value

        path, _read = pull_request_mod.stage_pull_request(
            number=number,
            repository_url=self._repo_url,
            token=token,
            refusal=refusal,
            work_dir=work_dir,
            scrub=lambda text: str(self._scrub(redact_text(text).text)),
            logger=self.log,
            on_request=self._heartbeat,
            retry=self._forge_retry(),
            read_again=lambda read: call_reading_again(
                read,
                logger=self.log,
                what="the pull request fetch",
                twice=lambda exc: pull_request_mod.PullRequestUnavailable(
                    f"{exc}; {FORGE_REFUSED_TWICE}"
                ),
                refused=self._refused_credential,
            ),
        )
        return path

    def _refused_credential(self, exc: BaseException) -> bool:
        """A 401 worth a re-read: only when this worker reads a token at all."""
        return not self._git_token_refusal() and forge_credential_refused(exc)

    def _probe_reading_again(
        self, url: str, token: str | None, what: str
    ) -> tuple[str | None, RepoAccess | None]:
        """`probe_repository` with `token`; on a 401, read the slot again and probe once more.

        Returns the token the answer is for, which the push and the pull
        request then use (owner decision 2026-10-08). A second 401 keeps
        `can_push` False and says the token was re-read and refused again.
        Raises `ForgeError` as `probe_repository` does.
        """

        def probe(with_token: str | None) -> RepoAccess | None:
            return forge_mod.retry_transient(
                lambda: probe_repository(url=url, token=with_token),
                policy=self._forge_retry(),
                what=what,
            )

        access = probe(token)
        if not token or access is None or access.can_push \
                or not self._refused_credential(RuntimeError(access.reason)):
            return token, access
        self.log.warning(
            "the forge refused the credential; reading its secret again and retrying once",
            call=what,
        )
        # For write, as every caller's first read is: each is a probe before a
        # push or a pull request (archived issue 2907, `_git_token`).
        token = self._git_token(write=True)
        access = probe(token)
        if access is not None and not access.can_push \
                and forge_credential_refused(access.reason):
            self.log.error("the forge refused the re-read credential as well", call=what)
            access = dataclasses.replace(
                access, reason=f"{access.reason}; {FORGE_REFUSED_TWICE}"
            )
        return token, access

    def _open_pull_request_reading_again(
        self, open_with: Callable[[str], PullRequest], token: str, *, branch: str
    ) -> PullRequest:
        """`open_with(token)`; on a 401, read the slot again ONCE and open once more.

        MEASURED 2026-10-19 (archived issue 1623): task_194ad2144eb6441c8a7c pushed its
        branch and was then refused "could not open a pull request (401): Bad
        credentials". `_publish_git` reads the token once and the probe, the
        push and the pull request all use that read; only the probe re-read
        on a 401. A refresh in between -- each one makes GitHub end the access
        token it replaces (docs/onboarding.md §3.3 item 5) -- leaves the pull
        request holding a dead token after the push used it.

        THE RE-READ IS THE FIRST READ AGAIN, gates and all: `_forge_write_
        refusal` (the submitter's grant, read again now) and `_git_token_
        refusal` are asked first, and a refusal is not read past; then
        `_git_token`, the same suffix through the same path -- a person's slot
        from the broker, a team token from Secret Manager, never the tenant's
        token in place of a person's. The new value lives in this frame only,
        as the first did in the publish's.

        Exactly one re-read and one more call, with no sleep: a second 401 or
        a refused re-read raises `ForgeError` saying the branch is pushed and
        the pull request is not open, which the publish records as
        `pull_request_not_opened` and the finish fails the step for
        (`_published_nothing`). Anything but a 401 (a 403 or 404 is a
        permission, not a stale token) is raised as it came.
        """
        try:
            return open_with(token)
        except forge_mod.ForgeCredentialRejected:
            pass
        unopened = f"branch {branch} pushed; pull request not opened"
        fresh = self._write_token_read_again(branch, "pr_open_401", unopened, ForgeError)
        try:
            return open_with(fresh)
        except forge_mod.ForgeCredentialRejected as exc:
            self.log.error("the forge refused the re-read credential at the pull request "
                           "as well", branch=branch)
            raise ForgeError(f"{unopened} (401 twice): {exc}; {FORGE_REFUSED_TWICE}") from None

    def _push_reading_again(
        self, push: Callable[[str | None], str | None], token: str | None, *, branch: str
    ) -> tuple[str | None, str | None]:
        """`push(token)`; on a stale credential, read the slot again ONCE and push once more.

        Returns the pushed sha and the token that pushed it, which the pull
        request and any replay push then use (archived issue 3223). MEASURED: task_a4f1d69c
        probed `can_push` true and its push then failed exit 128 "remote:
        Invalid username or token": swarm-api's 15-minute refresh sweep ended
        the token between the probe's read and the push.

        THE SAME RE-READ AS THE PULL REQUEST'S (`_open_pull_request_reading_
        again`), gates and all: `_forge_write_refusal` and `_git_token_
        refusal` are asked first and a refusal is not read past, then
        `_git_token(write=True)`, the first read's suffix through the same
        path -- a person's slot from the broker, never the tenant's token in
        its place. The fence is read again before the second push, so a
        superseded attempt never pushes (invariant 5).

        Exactly one re-read and one more push: a second refusal raises
        `GitError` with `FORGE_REFUSED_TWICE`. A `PushRejected` (a moved
        branch or a lease that no longer holds), a 403 and anything else are
        raised as they came: the credential was accepted, or it is a
        permission, not a stale token.
        """
        try:
            return push(token), token
        except PushRejected:
            raise
        except GitError as exc:
            if not token or not self._refused_credential(exc):
                raise
        fresh = self._write_token_read_again(branch, "push_401", f"branch {branch} not pushed")
        self._own_before_forge_write("branch push")
        try:
            return push(fresh), fresh
        except PushRejected:
            raise
        except GitError as exc:
            if not forge_credential_refused(exc):
                raise
            self.log.error("the forge refused the re-read credential at the push as well",
                           branch=branch)
            # The marker first: `_publish_git` keeps 500 characters of the
            # reason, and git's own refusal is the longer, less useful part.
            raise GitError(f"branch {branch} not pushed (401 twice): {FORGE_REFUSED_TWICE}; "
                           f"the push said: {str(exc)[:200]}") from None

    def _write_token_read_again(self, branch: str, reason: str, failed: str, error: type[Exception] = GitError) -> str:
        """The ONE re-read after a 401 at a pull request's open, a push or the read before
        it (issue 284), gates and all (`_forge_write_refusal`, `_git_token_refusal`, then
        this task's own `_git_token(write=True)`): the fresh token, or `error` with no token.
        """
        self.log.warning("the forge refused the credential; reading its secret again and "
                         "trying once more", event="forge_token_reread", reason=reason,
                         branch=branch)
        refused = self._forge_write_refusal()
        if refused is not None:
            cause, why = refused
            self._emit_token_reread(branch, outcome=cause, reason=reason)
            raise error(f"{failed}: the re-read was refused: {cause}: {why}")
        refusal = self._git_token_refusal()
        if refusal:
            raise error(f"{failed} (401): the token was not read again: {refusal}")
        fresh = self._git_token(write=True)
        if not fresh:
            why = getattr(self, "_git_token_error", None) or "no forge credential was read"
            self._emit_token_reread(branch, outcome="unread", reason=reason)
            raise error(f"{failed} (401): the token could not be read again: {why}")
        self.log.register_secret(fresh)
        self._emit_token_reread(branch, outcome="read", reason=reason)
        return fresh

    def _emit_token_reread(
        self, branch: str, *, outcome: str, reason: str = "pr_open_401"
    ) -> None:
        """A `forge_token_reread` mark on the task's events (`reason`: `pr_open_401`,
        `push_401` or `pre_push_read_401`, the read before a publish_to_pr push).

        RUNNING with `cause`, as `_emit_startup_mark` writes: the frozen
        `EventType` has no note-only type. The branch and the outcome, never
        the token. Never raises: the publish's answer is the real signal.
        """
        if self._fenced_exit or self._writes_forbidden or self._control_plane_down:
            return
        try:
            self.control.emit(EventType.RUNNING, {
                "cause": "forge_token_reread", "reason": reason,
                "branch": branch, "outcome": outcome,
            })
        except Exception as exc:  # noqa: BLE001 -- a mark is never worth a publish
            self.log.warning("could not record the forge token re-read",
                             error=type(exc).__name__)

    def _service_run(self) -> bool:
        """Whether this task's VERIFIED submitter is a platform service account,
        whose run swarm-api's broker mints an App installation token for when
        the repository's owner has the App installed for this tenant
        (DAYONE-SERVICE-APP-TOKEN; `swarm_api.credbroker.decide_run_token`).

        The signed `submitted_by`, the field the broker decides from, read the
        way `swarm_api.validation.is_service_submitter` reads it -- never the
        unsigned `metadata.forge_credential_source` label. Unsigned, False: a
        field anyone with write on the document could set does not choose
        where the token comes from. Additional repositories never ask: the
        minted token covers the task's one repository."""
        if not self._forge_signed:
            return False
        submitted_by = (self._task or {}).get("submitted_by")
        return isinstance(submitted_by, str) and \
            submitted_by.strip().lower().endswith(".gserviceaccount.com")

    def _forge_suffix(self) -> str:
        """The provider suffix of this task's forge secret (contract request 54).

        The VERIFIED document's `forge_credential` (`secrets.forge_suffix`),
        and only once `_verify_spec` verified its signature. A task admitted
        unsigned by the legacy window has no signature over the field, so it
        reads the tenant token, as every task did before request 54: a field
        anyone with write on the document could set must not choose which
        member's slot this worker reads.
        """
        task = self._task or {}
        if not self._forge_signed:
            named = task.get("forge_credential")
            if named not in (None, GIT_PROVIDER):
                self.log.warning(
                    "an unsigned task names a forge credential; reading the tenant token",
                )
            return GIT_PROVIDER
        return forge_suffix(task)

    def _forge_grant_refusal(self, *, write: bool) -> tuple[str, str] | None:
        """`(cause, reason)` when the submitter's grant does not cover this, or None.

        docs/onboarding.md §3.3 step 4: read again, by id, at every call
        (`secrets.grant_refusal`). Nothing for a tenant or repository token.
        The repository is the VERIFIED document's, which is the one the clone
        reads. A grant that could not be read is not a revoked grant: it is
        `FORGE_GRANT_UNREADABLE`, which the callers end retryably.
        """
        suffix = self._forge_suffix()
        if user_slot_hash(suffix) is None:
            return None
        task = self._task or {}
        url = task.get("repository_url") or self.cfg.repository_url or self._repo_url
        # Signed, like the suffix: whose token for one owner (D5) this is.
        submitted_by = task.get("submitted_by")
        try:
            reason = grant_refusal(
                self.db,
                tenant_id=self.cfg.tenant_id,
                suffix=suffix,
                repository_url=url if isinstance(url, str) else None,
                write=write,
                call_options=self.control.call_options(),
                submitted_by=submitted_by if isinstance(submitted_by, str) else None,
            )
        except Exception as exc:  # noqa: BLE001 -- a read failure, said by type
            self.log.warning("the forge grant could not be read", error=type(exc).__name__)
            return FORGE_GRANT_UNREADABLE, (
                f"the submitter's grant could not be read ({type(exc).__name__})"
            )
        if reason is None:
            self.log.info("forge grant re-read: it still covers this", write=write)
            return None
        self.log.warning("forge grant re-read: refused", reason=reason, write=write)
        return FORGE_GRANT_REVOKED, reason

    def _forge_write_refusal(self) -> tuple[str, str] | None:
        """`(cause, reason)` when this attempt may not push or open a pull request.

        Asked before every forge write (the publish, each carrier push, the
        findings epic, the merge) and BEFORE the token for it is read, so a
        refused write never holds a push credential:

          * the task's signed `forge_access` is `read` (D9): never a push;
          * the submitter's grant, read again now, was deleted or turned
            `read` since submission (§3.3 step 4).
        """
        if forge_read_only(self._task or {}):
            return FORGE_READ_GRANT, "this task's grant on its repository is read-only"
        return self._forge_grant_refusal(write=True)

    def _end_qa_identity_refused_before_start(self, cause: str) -> Outcome:
        """End the attempt before the agent ran: its QA identity was refused (QI-WORKER).

        docs/design/qa-identity.md §4.5: the first ask was refused as not
        granted or unconfigured (`qa_identity.FAILS_THE_ATTEMPT`). FAILED, not
        retried: the next attempt would read the same grant and the same
        deployment. The gate was already told `failed` and holds nothing.
        """
        error = (
            f"{cause}: the task asked for the console's QA identity and it was refused "
            f"({qa_identity_mod.reason(cause)}), so the agent was not started."
        )
        self.log.error(
            "the task's qa identity was refused; ending before the agent runs", cause=cause
        )
        summary = self._upload_outputs()
        summary["qa_identity"] = {"cause": cause}
        with self._metrics_after_the_record():
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _end_forge_refused_before_start(self, cause: str, reason: str) -> Outcome:
        """End the attempt before the agent ran: the grant does not allow the work.

        Called after the startup window closes, like every ending `_prepare`
        decides on. The agent never started and nothing was cloned with the
        credential. `control.finish` (or `fail_retryably`) writes the task's
        state and releases the lease, and that release is all this does to the
        lease: no extension, no other write. An unreadable grant fails
        retryably; a revoked or read grant does not, because the next attempt
        would read the same.
        """
        reason = self._scrub(reason)
        error = self._scrub(
            f"{cause}: {reason}. The agent was not started. The submitter can "
            "choose the repository again under Access (Write for a step that "
            "pushes) and submit the task again."
        )
        self.log.error(
            "the task's forge grant does not allow this work; ending before the agent runs",
            cause=cause,
            reason=reason,
        )
        summary = self._upload_outputs()
        summary["forge_check"] = {"cause": cause, "reason": reason}
        with self._metrics_after_the_record():
            if cause == FORGE_GRANT_UNREADABLE:
                state = self.control.fail_retryably(
                    exit_code=None,
                    error=error,
                    cause=cause,
                    result_summary=summary,
                    retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                    detail={"forge_check": cause},
                    end_cause=EndCause.CANNOT_START,
                )
                return Outcome(exit_code=ExitCode.FAILED, state=state)
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _fail_for_forge_write_refused(
        self, refused: dict[str, Any], summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """End the attempt: its push or pull request was refused by the grant (OB5).

        The final checkpoint is already taken (`_record_finalised` takes it
        first, invariant 8), so the work is kept; the push was refused before
        its token was read (`_publish_git`). PUBLISH_REFUSED: the worker
        refused to publish. An unreadable grant fails the attempt retryably;
        a read or revoked grant ends the task, because the next attempt would
        read the same.
        """
        cause = str(refused.get("cause") or FORGE_GRANT_REVOKED)
        reason = self._scrub(str(refused.get("reason") or ""))
        error = self._scrub(
            f"{cause}: {reason}. Nothing was pushed and no pull request was opened; "
            "the work is in the attempt's final checkpoint and its patch artifact."
        )
        summary["forge_check"] = {"cause": cause, "reason": reason}
        if cause == FORGE_GRANT_UNREADABLE:
            state = self._fail_retryably_terminal(
                exit_code=exit_code,
                error=error,
                cause=cause,
                result_summary=summary,
                retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                detail={"forge_check": cause},
                end_cause=EndCause.PUBLISH_REFUSED,
            )
            return Outcome(exit_code=ExitCode.FAILED, state=state)
        self._finish_terminal(
            state=TaskState.FAILED,
            exit_code=exit_code,
            error=error,
            result_summary=summary,
            end_cause=EndCause.PUBLISH_REFUSED,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _git_token_refusal(self) -> str | None:
        """Why this worker must not hold the tenant git token, or None.

        The token sits in the worker's heap from the moment it is read, and the
        agent runs beside it as the same uid. The entrypoint made this process
        non-dumpable so the agent cannot read that heap (hardening.py). When
        that did not happen, the token is not read, the clone runs without it,
        and the publish is refused.
        """
        memory = self.memory or MemoryProtection(
            FAILED, "the entrypoint established no memory protection for this worker"
        )
        return memory.git_token_refusal

    def _timeout_asked_env(self) -> dict[str, str]:
        """`TIMEOUT_ASKED_ENV` when the task document says its spec asked for
        more time than the step was given (archived issue 2981), so the runner records the
        clamp in `limits.clamped`; empty otherwise. Only ever a record: the
        limit is `cfg.timeout_seconds`, never this (invariant 10)."""
        asked = (self._task or {}).get(TIMEOUT_ASKED_FIELD)
        if isinstance(asked, bool) or not isinstance(asked, int):
            return {}
        return {TIMEOUT_ASKED_ENV: str(asked)} if asked > self.cfg.timeout_seconds else {}

    def _max_turns_env(self) -> dict[str, str]:
        """`MAX_TURNS_ENV` from the VERIFIED task's `max_turns` (contract request
        133), signed at spec format 7, so a cap raised or removed after
        submission failed `verify_spec` before this runs. Empty when the task
        has none: no cap, today's behaviour. Never from `input`, which a
        caller writes (invariant 10)."""
        cap = (self._task or {}).get(MAX_TURNS_FIELD)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            return {}
        return {MAX_TURNS_ENV: str(cap)}

    def _build_child_env(self) -> dict[str, str]:
        ws = self.ws
        assert ws is not None
        profile = self.cfg.profile
        base: dict[str, str] = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"
            ),
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "SWARM_TASK_ID": self.cfg.task_id,
            "SWARM_ATTEMPT_ID": self.cfg.attempt_id,
            "SWARM_TENANT_ID": self.cfg.tenant_id,
            "SWARM_RUNNER_PROFILE": self.cfg.runner_profile,
            # The ceilings a runner may apply to ITS OWN child (the agent CLI,
            # the catalogue command). `runners/limits.py` clamps whatever the
            # caller's `input` asks for against these, so `input` can lower a
            # limit and never raise one -- which is invariant 10 for the one
            # execution parameter a caller is allowed to influence at all.
            #
            # THE TIME LEFT, NOT THE STEP'S WHOLE LIMIT (archived issue 2363). This runs at
            # every runner start, including the restart after a credential
            # reload or an account swap; given the whole limit, a runner
            # restarted 87 minutes in planned its wrap-up from all of it and
            # was killed at `_runner_deadline` mid gate checks. Rounded up, so
            # a first start is told its whole limit: the supervision loop, not
            # this figure, is what kills at the deadline. Never below 1 s.
            TIMEOUT_ENV: str(max(1, math.ceil(self._runner_deadline - time.monotonic()))),
            # THE SAME DEADLINE AS AN INSTANT (archived issue 2981). The figure above is read
            # when this env is built, and a runner counts it from its own start
            # -- the claude-code runner only over its CLI's own run time -- so
            # the setup between (the issue fetch, the context pack: ~18 min on
            # task_2518ef3904ed47d0ac52) was counted nowhere, and its
            # pre-publish gate was still starting guard shards when this loop
            # killed it at `_runner_deadline`. Wall-clock epoch seconds: the
            # worker and its runner share the container's clock.
            RUNNER_DEADLINE_ENV: f"{time.time() + self._runner_deadline - time.monotonic():.3f}",
            **self._timeout_asked_env(),
            **self._max_turns_env(),
            GRACE_ENV: str(self.cfg.termination_grace_seconds),
            STDOUT_ENV: str(self.cfg.max_stdout_bytes),
            STDERR_ENV: str(self.cfg.max_stderr_bytes),
        }
        # THE CHECKOUT, WHEN THERE IS ONE (archived issue 226). A CLI runner starts its agent
        # there (`cliagent.agent_working_directory`), so Claude Code loads the
        # repository's own CLAUDE.md as a local lane does. Set by the worker,
        # after the clone, and never read from `input`, which a caller writes:
        # `input.repository` is `setdefault`, so a caller's own would shadow it.
        if self._repo_url and ws.checkout().is_dir():
            base["SWARM_REPO_DIR"] = str(ws.checkout())
        # THE CLONE BASE, for the publish scan the agent can run first
        # (`publish_scan.default_base`): the base the publish diffs from
        # (`_publish_base`, `empty` for a repository cloned with no commit),
        # else the workspace marker's, which describes the work even when the
        # publish will not trust it. Set by the worker, never from `input`.
        clone_base = self._publish_base or self._clone_base
        if clone_base:
            base[CLONE_BASE_ENV] = clone_base
        # THE BRANCH THE RUNNER REBASES ONTO BEFORE PUBLISHING (SPEED-D1), set
        # only when that is safe for this step (`_prepublish_rebase_ref`).
        # The runner rebases through the git tools, so without them it is
        # recorded and skipped. Set by the worker, never from `input`.
        if self._prepublish_rebase:
            base[PREPUBLISH_BASE_ENV] = self._prepublish_rebase
        # THE BRANCH THE STEP STARTED ON, for the gate's moved-HEAD turn
        # (`prepublish.Gate._moved_head`): the harvest refuses a HEAD that
        # left it (`_head_moved`), so the agent is told while its session can
        # still be continued. Unset for a clone at a commit. Set by the
        # worker, never from `input`.
        if self._start_branch and self._start_branch.startswith("refs/heads/"):
            base[START_BRANCH_ENV] = self._start_branch.removeprefix("refs/heads/")
        # WHETHER THIS STEP PUBLISHES (ONE-PLACE, 2026-11-03): "0" for a
        # repository step whose branch is not its deliverable, so the CLI
        # runner leaves the publishing paragraph out of its prompt
        # (`cliagent.step_publishes`). Set by the worker, never from `input`.
        if self._repo_url:
            base[STEP_PUBLISHES_ENV] = "1" if self._deliverable_branch() else "0"
        # WHICH OF swarm-git AND swarm-state THIS STEP'S AGENT IS HANDED
        # (ONE-PLACE, 2026-11-03; the owner's `explicit-list` on archived pull request 3113),
        # from the signed step facts `stepservers.step_servers` reads. Set on
        # every start, never from `input`; the spools stay offered either way.
        base[stepservers_mod.ENV] = stepservers_mod.render(self._step_servers())
        # WHAT THE IMAGE SETS THAT THE RUNNER NEEDS, carried by EXACT NAME.
        # Everything else in the worker's environment stays behind; see
        # `workspace.child_env` for why this environment is built rather than
        # inherited. Each name is platform-set -- the image's ENV or the Job
        # definition, never a caller (invariant 10) -- and each is a path the
        # runner's interpreter or libraries resolve code or data from.
        #
        # PLAYWRIGHT_BROWSERS_PATH is where `agent-runtime-browser` installed
        # Chromium: /opt/playwright, read-only to uid 10001, outside every
        # writable path. Without it Playwright looks under
        # $HOME/.cache/ms-playwright, and HOME here is the attempt's work dir,
        # which holds no browser -- so a browser attempt started by the
        # lifecycle cannot launch Chromium. Derived from the code and predicted
        # in the review of archived pull request 31, not observed: the bare runner the GKE Job
        # used to start inherited the container's environment whole, and no
        # browser attempt had run under the lifecycle before that PR made it
        # the entrypoint there.
        # Its sibling PLAYWRIGHT_SKIP_BROWSER_GC is deliberately NOT carried:
        # only Playwright's browser-install path reads it (checked in the 1.63.0
        # driver), and the runner never installs.
        #
        # NEVER A PREFIX. `PLAYWRIGHT_*` would also carry
        # PLAYWRIGHT_SERVICE_ACCESS_TOKEN, a real Playwright credential.
        # A test takes every ENV in the browser image -- both Dockerfiles,
        # plus the upstream python base's, recorded with the test and pinned
        # by digest -- and holds each one to either this list or a recorded
        # reason the runner must not have it. The image's pip/npm settings
        # are among the refused: no runner process runs pip or npm. Whether
        # the AGENTS the runners start should get them is an open owner
        # decision, recorded beside that test.
        for passthrough in (
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "NODE_PATH",
            "NODE_EXTRA_CA_CERTS",
            "PLAYWRIGHT_BROWSERS_PATH",
        ):
            value = os.environ.get(passthrough)
            if value:
                base[passthrough] = value
        if self.cfg.model:
            base["MODEL"] = self.cfg.model
        # WHO THE AGENT'S COMMITS NAME (P37): the person who dispatched the
        # task, from its document as swarm-api recorded it at submission --
        # never from `input` (invariant 10). See `gitidentity`.
        base.update(git_identity_env(self._task))
        # THE BUDGET (archived issue 2459): where the runner reports usage and reads the
        # stop, on a budgeted task only. Nothing for any other.
        base.update(self._budget_env())
        # THE SCRATCH NOTE (issue #26): where a crossing of two thirds of a
        # scratch volume is written for the agent, on GKE and claude-code only.
        base.update(self._scratch_note_env())
        for name in ("CLAUDE_CODE_BIN", "CLAUDE_CODE_ARGS", "CODEX_BIN", "CODEX_ARGS"):
            # Platform-set, never caller-set: they live in the Job definition.
            if os.environ.get(name):
                base[name] = os.environ[name]

        if profile.provider and profile.secrets:
            if self.secret_client is None:
                raise WorkerError(
                    f"runner {profile.name} needs the {profile.provider} credential but no "
                    "Secret Manager client is configured"
                )
            # THE POOL FIRST, THE TENANT SECRET AS THE FALLBACK, and never the
            # other way round. An assigned account is a subscription chosen for
            # its headroom; the tenant secret is the one credential every agent
            # of this tenant shares. Falling back to it is correct when there is
            # no pool and wrong whenever there is one, so the order is fixed.
            account_env = self._pool_credential_env(profile)
            if account_env is not None:
                base.update(account_env)
                # Only an attempt holding an account talks to its runner
                # about it; every other profile and attempt is unchanged.
                base.update(self._account_channel_env())
            else:
                # Under the startup budget before the runner. The credential
                # reload calls this again mid-run, outside the window, and gets
                # the mid-run `tenant` budget there (archived issue 70).
                tenant = load_tenant(
                    self.db, self.cfg.tenant_id, call_options=self.control.call_options("tenant")
                )
                resolved = resolve_credentials(
                    tenant=tenant,
                    provider=profile.provider,
                    secret_env_names=profile.secrets,
                    any_of=profile.secrets_any_of,
                    client=self.secret_client,
                    logger=self.log,
                )
                base.update(resolved.env)
                # Off the pool, a CLI that can resume still names its session
                # for the credential reload (archived issue 626).
                base.update(self._session_channel_env())
        base.update(self._continuation_env())
        base.update(self._question_bridge_env())
        # The child-task spool, when this attempt has a child path: the one
        # variable an agent needs to submit and await helpers (child tasks,
        # §3.1). And the git tools' spool, when it is offered them
        # (docs/runner-git-tools.md §1.3, lane G6): read from the VERIFIED
        # task, the checkout and the clone base this worker recorded. Paths,
        # never a credential: the attempt key stays here, and no forge token
        # is in this process while the agent runs (§0).
        base.update(self.children.prepare(
            ws.work,
            task=self._task,
            checkout=ws.checkout() if self._repo_url else None,
            repository_url=self._repo_url,
            clone_base=clone_base,
            private_dir=ws.private,
            logs_dir=ws.logs,
            store=self.store,
            pull_base=self._pull_base,
        ))
        # The hand-off spool, on a live-browser attempt (live-browser §4.1):
        # a path and the gate's loopback CDP address, never a credential.
        base.update(self.handoff.prepare(ws.work))
        # BEFORE THE GRAPH CONTEXT, whose `judge.attempt.setup` reads the key
        # file this writes (`attempt.TYPESAFE_KEY_FILE`): after it, a live-
        # browser attempt's judge on TypeSafe found no key and read `no_key`
        # (lane JEV-JUDGMENTS-RUN, 2026-11-02).
        # The jev MCP server's TypeSafe key, on a live-browser attempt with a
        # key (JEV-RUNNER, `jevkey`): the PATH of a 0600 file in private/,
        # never the key, which reaches only that server's process. Never
        # fatal: no key, or no read, is an attempt without the jev tools.
        base.update(jevkey_mod.prepare(
            endpoint=base.get(jevkey_mod.CDP_ENDPOINT_ENV),
            db=self.db,
            client=self.secret_client,
            tenant_id=self.cfg.tenant_id,
            submitted_by=(self._task or {}).get("submitted_by"),
            private_dir=ws.private,
            logger=self.log,
            call_options=self.control.call_options("tenant"),
            backend=base.get(jevkey_mod.BACKEND_ENV) or os.environ.get(jevkey_mod.BACKEND_ENV),
            inject=self.typesafe.on,
            shim_file=self.typesafe.shim_file,
        ))
        base.update(self._graph_context_env(ws, clone_base))
        # `work/repos`, when the task reads additional repositories: a path.
        base.update(extrarepos_mod.child_env(ws, self._extra_checkouts))
        # The lane quality gates (JEV-LANES, `qualitygates`): on a claude-code
        # attempt whose workspace turned `quality_gates.jev` on, the PATH of a
        # 0600 config naming the workspace key's 0600 file in private/, never
        # the key. Off, or no key: nothing, and no hook is registered.
        base.update(self._quality_gates_env(ws, profile))
        # The step's agent plugins' variables (`plugins.start`): gstack's
        # session kind, its state root under work/ and its switches. Names
        # and paths, never a credential.
        if self._plugins is not None:
            base.update(self._plugins.env)
        # The step's signed work contract for JC2 (`_stage_work_contract`):
        # the body's path and the signed digest. A path and a hash, never a
        # credential; nothing for a step without a signed contract.
        base.update(self._work_contract_env)
        # The released workspace secrets (SEC-RELEASE): the manifest's PATH,
        # `SWARM_SECRETS_MANIFEST`, and nothing else -- never a value. The
        # release ran once, in STEP 6a; a rebuild on a credential reload
        # hands the same path.
        released = self.secret_release.result
        if released is not None:
            base.update(released.env)
        # The MCP relay (MCP-WORKER): the relay file's PATH, whose contents
        # are loopback URLs and the local token -- never a credential.
        base.update(self.mcp.env)
        return ws.child_env(base)

    def _plugin_provider_key(self, provider: str) -> str | None:
        """A managed plugin's binding to the workspace key (managed-plugins.md
        §6.2, "Use a workspace key › TypeSafe"), asked by `pluginload` only
        for an entry the signed set binds: the key the quality gates resolve
        (`jevkey.resolve_workspace`: the workspace's, never a person's,
        registered for redaction), or with the forwarder on, the shim file's
        text and never the key; None when there is neither. Never raises:
        no key is a plugin whose adapter does not run, and the built-in gate
        it would replace keeps running."""
        if provider != "typesafe":
            return None
        if self.typesafe.on:
            shim = self.typesafe.shim_file
            try:
                return shim.read_text(encoding="ascii") if shim is not None else None
            except OSError:
                return None
        return jevkey_mod.resolve_workspace(
            db=self.db, client=self.secret_client, tenant_id=self.cfg.tenant_id,
            logger=self.log, call_options=self.control.call_options("tenant"))

    def _plugin_hard_stop_paths(self, task: dict[str, Any]) -> tuple[str, ...]:
        """The repository's `hard_stop_paths`, which `qualitygates.prepare`
        puts in the baked gates' config, for a managed Jev plugin's adapter.
        Read only for a step with managed plugins; a read that fails is
        none, as the gates take it (the secret names are still denied)."""
        if not pluginload_mod.entries(task) or self.db is None:
            return ()
        try:
            return tuple(qualitygates_mod.hard_stop_paths(
                self.db, self.cfg.tenant_id,
                self._repo_url or os.environ.get(qualitygates_mod.REPOSITORY_URL_ENV),
                call_options=self.control.call_options("tenant")))
        except Exception as exc:  # noqa: BLE001 -- the names are still denied
            self.log.warning("plugin hard stops unread", error=type(exc).__name__)
            return ()

    def _quality_gates_env(self, ws: Any, profile: Any) -> dict[str, str]:
        """`qualitygates.prepare`, once per attempt: a rebuild on a credential
        reload hands the same path. Only the claude-code runner registers the
        hooks, so no other runner's attempt reads the switch or the key."""
        if self._gates_env is None:
            spec = cli_agent_spec(self.cfg.runner_profile)
            if spec is None or spec.name != "claude-code":
                self._gates_env = {}
            else:
                # The block's MAC was checked by the scheduler, which holds
                # the settings key; its verdict is in this job's environment.
                self._gates_route = qualitygates_mod.GatesVerdict(os.environ)
                self._gates_env = qualitygates_mod.prepare(
                    db=self.db,
                    client=self.secret_client,
                    tenant_id=self.cfg.tenant_id,
                    private_dir=ws.private,
                    logger=self.log,
                    call_options=self.control.call_options("tenant"),
                    secret_env_names=tuple(profile.secrets or ()),
                    inject=self.typesafe.on,
                    shim_file=self.typesafe.shim_file,
                    verdict=self._gates_route,
                    # The signed workflow switch (2026-11-01), which only
                    # narrows what the stored, signed setting allows.
                    switch=self._dispatch_block().get(qualitygates_mod.SWITCH),
                    # The clone's repository: its registration's hard_stop_paths.
                    repository_url=self._repo_url,
                )
        return dict(self._gates_env)

    def _record_quality_gates(self, summary: dict[str, Any]) -> None:
        """After the agent (JEV-LANES): the hooks' counts in the summary, and
        one RUNNING mark, cause `quality_gate`, when jev-belay blocked a stop
        or jev-enforce flagged a rule -- counts only, never text -- so a wave
        can compare first-CI-red and NOT_YET rates with the switch on and
        off. The key file is removed. A stored "on" the scheduler did not
        vouch for is `{"jev": false, "refused": <why>}`. Never raises.

        A step whose signed dispatch block set a workflow switch (2026-11-01)
        also records each switch, its value and the deciding source: the
        gates' under `quality_gates.switch`, the graph's and the judgments'
        under `graphstage.SWITCHES_FIELD`, which the experiment reads."""
        self._record_switches(summary)
        record = dict(getattr(getattr(self, "_gates_route", None), "record", None) or {})
        if not self._gates_env:
            route = getattr(self, "_gates_route", None)
            refused = getattr(route, "refused", None)
            if isinstance(refused, str):
                summary[qualitygates_mod.SUMMARY_KEY] = {"jev": False, "refused": refused}
            if record:
                summary.setdefault(qualitygates_mod.SUMMARY_KEY, {"jev": False})["switch"] = record
            return
        try:
            counts = qualitygates_mod.finish(self._gates_env)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read the quality gates' counts", error=type(exc).__name__)
            return
        summary[qualitygates_mod.SUMMARY_KEY] = {"jev": True, **(counts or {}),
                                                 **({"switch": record} if record else {})}
        if qualitygates_mod.worth_an_event(counts):
            self._emit_startup_mark(qualitygates_mod.EVENT_CAUSE, dict(counts or {}))

    def _settle_jev(self) -> None:
        """After the agent, before any terminal write: this attempt's Jev calls
        and tokens into `jev_usage` (`judge.attempt.settle`), one row per
        attempt and the workspace's month, in one transaction fenced by this
        attempt's generation as every guarded write is (invariant 5): a
        superseded attempt writes nothing. The next attempt of the task counts
        these calls, and the month's total is what `code_intelligence
        .jev_monthly_cap_tokens` is held to. Never raises; no output changes."""
        from .judge import attempt as judge_attempt

        if not self._graph_env:
            return
        try:
            judge_attempt.settle(
                self._graph_env, db=self.db,
                run_transaction=self.control._run_transaction,
                tenant_id=self.cfg.tenant_id, task_id=self.cfg.task_id,
                generation=self.cfg.generation,
                fence=lambda txn: self.control._fenced_task(txn, write="jev usage"),
                log=self.log)
        except Exception as exc:  # noqa: BLE001 -- accounting is never worth the attempt
            self.log.warning("jev_settle_failed", error=type(exc).__name__)

    def _settle_jev_at_exit(self) -> None:
        """`judge.attempt.settle_at_exit` with this attempt's committed end,
        and the workspace's `private/`, where the quality hooks and the Jev
        browser left one record per call: those are sent even when the
        judgments are off."""
        from .judge import attempt as judge_attempt

        ws = self.ws
        private_dir = getattr(ws, "private", None) if ws is not None else None
        if self._graph_env or private_dir is not None:
            judge_attempt.settle_at_exit(
                self._graph_env or {}, db=self.db,
                committed_end=getattr(self.control, "committed_end", None),
                tenant_id=self.cfg.tenant_id, task_id=self.cfg.task_id,
                generation=self.cfg.generation, log=self.log, private_dir=private_dir)

    def _record_switches(self, summary: dict[str, Any]) -> None:
        """The workflow switches' record (`graphstage.switches_summary`), only
        when the signed dispatch block set one, and the judgments' key file
        removed after the agent (`judge.attempt.finish`). Never raises."""
        from .judge import attempt as judge_attempt

        try:
            dispatch = self._dispatch_block()
            work = getattr(self, "_switch_work", None)
            if work is not None:
                switches = graphstage_mod.switches_summary(work, dispatch)
                if switches:
                    summary[graphstage_mod.SWITCHES_FIELD] = switches
            if graphstage_mod.switch_of(dispatch, judge_attempt.SWITCH) == judge_attempt.ON:
                judge_attempt.finish(self._graph_env or {})
        except Exception as exc:  # noqa: BLE001 -- a record is never worth the attempt
            self.log.warning("could not record the workflow switches", error=type(exc).__name__)

    def _graph_context_env(self, ws: Any, clone_base: str | None) -> dict[str, str]:
        """The staged graph snapshot's variables (docs/design/context-quality.md
        §4.7, lane CTXQ-S): decided by the stored setting, once per attempt.
        A staging failure is a reason in `stage.json`, never this attempt's.

        Or the served graph's (graph-service.md §4.1, lane GS-WORKER): the pin,
        the ticket file in `private/` and the service URL, when the workspace
        stored `graph_delivery: service`. A 409 at the ticket's mint is the
        fence (invariant 5): raised as FencedWriteRefused, which `run` turns
        into a stand-down that touches neither the lease nor the task, as the
        secrets release's 409 is."""
        if self._graph_env is None:
            self._switch_work = ws.work
            try:
                self._graph_env = graphstage_mod.prepare(
                    ws.work, profile_name=self.cfg.runner_profile,
                    runner_argv=self.cfg.profile.runner_argv, db=self.db, store=self.store,
                    tenant_id=self.cfg.tenant_id, repository_url=self._repo_url,
                    task_id=self.cfg.task_id,
                    checkout=ws.checkout() if self._repo_url else None,
                    base_sha=clone_base, call_options=self.control.call_options(),
                    log=self.log, mark=self._mark_graph_ready,
                    graph_ticket=self.graph_ticket, private_dir=ws.private,
                    backend=self.cfg.backend, service_url=self.cfg.graph_service_url,
                    secret_env_names=self.cfg.profile.secrets,
                    graph_context=self.graph_context,
                    # A private repository's depth-1 clone is deepened for
                    # the ancestry proof with the forge token, held as the
                    # clone holds it (lane GRAPH-TOKEN-2563). The callable
                    # is asked only when a fetch is needed, and never when
                    # this worker must not hold the token at all. This runs
                    # before the agent starts.
                    forge_token=None if self._git_token_refusal() else self._git_token,
                    # The signed workflow switches (2026-11-01) and, for an
                    # opted-in step on the TypeSafe backend with no key file
                    # or shim, the WORKSPACE's TypeSafe key, resolved as the
                    # quality gates resolve it (lane jev-on-coding-profiles).
                    dispatch=self._dispatch_block(),
                    jev_key=lambda: jevkey_mod.resolve_workspace(
                        db=self.db, client=self.secret_client,
                        tenant_id=self.cfg.tenant_id, logger=self.log,
                        call_options=self.control.call_options("tenant")))
            except graph_ticket_mod.GraphTicketFenced as exc:
                raise FencedWriteRefused(
                    self.cfg.generation, -1, str(exc), write="graph ticket"
                ) from None
            except Exception as exc:  # noqa: BLE001 -- never fails the attempt
                self.log.warning("graph context not staged", error=type(exc).__name__)
                self._graph_env = {}
        return dict(self._graph_env)

    def _mark_graph_ready(self, detail: dict[str, Any]) -> None:
        """`graph_ready`: how the graph reached this attempt and what it cost.

        docs/design/graph-service.md §9.1 and §9.3 (lane GS-MEASURE, part of
        archived issue 1384). Staging ran inside `_build_child_env`, under the `credentials`
        phase, with no timer: no mark isolated it, and its bytes were only in
        the pod-local `stage.json`. The detail is `graph`, built by
        `graphstage.ready_detail`: `delivery` (`staged`, `service` or `none`),
        `seconds` (the decision's wall time, Firestore reads, ancestry git
        calls and downloads), `bytes` and `blobs` (None when nothing was
        delivered) and `reason`. Numbers and a fixed sentence only. Once per
        attempt, on the same RUNNING startup-mark path as `clone_timed`;
        never raises.
        """
        if self._graph_marked:
            return
        # Set before the write, so a failed write is not tried again later.
        self._graph_marked = True
        self._emit_startup_mark(graphstage_mod.READY_MARK, detail)

    def _live_browser_unavailable(self, note: dict[str, Any]) -> None:
        """`live_browser_unavailable`: a live profile ran with no live session.

        `note` is `HandoffPath._refuse`'s: the code (`live_path`) and a reason
        that is a swarm-api error code, an exception's class name or a fixed
        sentence -- never a ticket, a key or a response body.
        """
        reason = note.get("reason") or note.get("live_path") or "unknown"
        self._emit_startup_mark(
            "live_browser_unavailable",
            {
                "phase": note.get("phase", "live_session"),
                "live_path": note.get("live_path"),
                "reason": reason,
                # A failed VIEW says "live view unavailable" (NOLIVE-GUARD).
                "message": note.get("message") or f"live browser unavailable: {reason}",
            },
        )

    def _fail_live_gate_missing(self, code: str | None) -> Outcome:
        """A retryable failure before the agent ran: the live gate was not ready.

        NOLIVE-GUARD (part of archived issue 1366). Ending the ATTEMPT (`fail_retryably`)
        rather than parking: a park holds no capacity, but nothing about this
        pod's sidecar gets better by waiting, and only a new attempt brings a
        new one; SCHEDULED_RETRY parks are promoted by nothing. The task goes
        back to READY while it has attempts left, its lease and capacity
        released (invariant 1), and ends CANNOT_START when they are spent.
        The spec is verified first, as step 4 would have: the retry is decided
        on the signed `max_attempts`, never the live field (archived issue 346).
        """
        task, create_time = self.control.fetch_task_snapshot()
        self._verify_spec(task, create_time)
        self.control.pin_verified_max_attempts(task.get("max_attempts"))
        cause = handoff_mod.LIVE_GATE_UNAVAILABLE
        error = (
            f"{cause}: the live browser was not ready ({code or 'unknown'}), so the "
            "agent was not started without its browser; the attempt is retried while the "
            "task has attempts left."
        )
        self.log.warning(
            "the live gate is not ready; failing the attempt retryably before the agent runs",
            cause=cause,
            live_path=code,
        )
        with self._metrics_after_the_record():
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=cause,
                result_summary={"live_gate": {"cause": cause, "live_path": code}},
                retry_delay_seconds=handoff_mod.LIVE_GATE_RETRY_DELAY_SECONDS,
                detail={"live_path": code},
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _emit_startup_progress(self, step: str, at: datetime) -> bool:
        """`startup_progress`: one of the browser's start-up steps, at the gate's time (STARTUP-API).

        Called by `handoff.HandoffPath.note_startup` once per step and
        attempt, for the console's progress bar (contract request 74). Never
        on a fenced exit or a tenant mismatch (invariant 5: the stream is not
        this attempt's to write then), which settles the step unwritten; a
        control-plane outage leaves it for a later tick. A write that fails
        raises, and the caller tries it again. Holds and moves nothing.
        """
        if self._fenced_exit or self._writes_forbidden:
            return True
        if self._control_plane_down:
            return False
        self.control.emit(
            EventType.STARTUP_PROGRESS,
            {"step": step, "attempt_id": self.cfg.attempt_id, "generation": self.cfg.generation},
            at=at,
        )
        return True

    def _note_browser_idle(self, child: Any) -> None:
        """`browser_unused`: a live-browser agent BROWSER_IDLE_SECONDS in with
        no browser call and no image (BROWSER-USE-FLAG, part of archived issue 2322).

        Looked for ONCE per attempt, at the first poll past the mark, and
        written at most once: one read of the CLI's stream so far
        (`claude_code.browser_use`), never repeated, so a long run pays it
        once. Nothing is written when the stream cannot be read, and an agent
        that starts using the browser later is still recorded as having used
        it in the summary (`browser_use_record`). Never raises.
        """
        if self._browser_idle_checked or not getattr(self.handoff, "live", False):
            return
        try:
            elapsed = child.elapsed_seconds
        except Exception:  # noqa: BLE001
            return
        if elapsed is None:
            return
        if elapsed < BROWSER_IDLE_SECONDS:
            return
        self._browser_idle_checked = True
        ws = self.ws
        files = agent_stream_files(self.cfg.runner_profile)
        if ws is None or files is None:
            return
        try:
            from .runners.claude_code import browser_use as browser_use_counts

            counts = browser_use_counts(ws.artifacts / files.stdout, ws.artifacts)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read whether the agent has used its browser",
                             error=f"{type(exc).__name__}: {exc}")
            return
        if counts.get("read") is not True or counts.get("tool_calls") or counts.get("images"):
            return
        minutes = int(elapsed // 60)
        self._emit_startup_mark(
            "browser_unused",
            {
                "seconds": int(elapsed),
                "tool_calls": 0,
                "images": 0,
                "message": (f"the agent has run {minutes} minutes on a browser runtime "
                            "without a browser call or an image"),
            },
        )
        self.log.info("browser unused so far", seconds=int(elapsed))

    def _emit_startup_mark(self, cause: str, detail: dict[str, Any]) -> None:
        """One startup mark on the task's events: RUNNING, with `cause`.

        RUNNING because the task is RUNNING by now and the frozen `EventType`
        has no note-only type; `cause` says which mark it is, as it does for
        `account_assigned`. The API serves an event's detail as stored
        (`GET /v1/tasks/{id}/events`), so the marks need no reader of their
        own. Not on a fenced exit, a tenant mismatch or a control-plane
        outage: the stream is not this attempt's to write then. Never raises:
        a measurement is never worth an attempt.
        """
        if self._fenced_exit or self._writes_forbidden or self._control_plane_down:
            return
        try:
            self.control.emit(EventType.RUNNING, {"cause": cause, **detail})
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not record a startup mark on the task's events",
                cause=cause,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _mark_clone_timed(
        self,
        clone: CloneResult | None,
        *,
        tries: list[dict[str, Any]],
        pinned: bool,
    ) -> None:
        """`clone_timed`: where the clone's time went (archived issue 667, lane OB1). Once per attempt.

        MEASURED 2026-10-06 (chunk-1 observer): clones took a median 37.8 s
        and a p90 70.9 s, clustered at ~38 s and ~71 s whatever the load, and
        nothing recorded which part stalled. The detail is `clone`:
        `seconds` (the git steps' wall time, as the worker measured it),
        `tries` (one more for each in-process retry, `gitops.retry_clone`),
        `pinned` (the workflow base pin's fetch by sha landed it), and
        `gitops.clone_phase_timings`' numbers -- DNS, connect, TLS, ref
        negotiation, the pack's wait and transfer, the checkout -- read from
        git's traces in-process. Nothing from the trace's text is in it: the
        wire trace carries request headers.

        EVERY TRY, THE FAILED ONES INCLUDED (archived issue 742): `try_log` lists them in
        order, each `{connect_seconds, ok, error_class, seconds}`
        (`gitops.try_record`). On 2026-10-06 a try that hung about 134 s
        before a retry landed was in no `clone_timed` event, so the clone
        figures missed exactly the worst case. `tries` stays the count, and
        `seconds` and the phase numbers the clone that landed, for every
        reader of the fields before this one. A clone that never landed --
        the forge never answered, or refused -- is marked too, `ok: False`,
        with `seconds` the tries' wall time summed and no phase numbers.

        WHICH ADDRESS EACH SIDE REACHED (archived issue 721, P27): every `try_log` entry
        also carries `probe_peer`, `git_peer` and `peer_pinned`
        (`gitops.PeerPin`), and the top level the last try's two peers and
        `peer_pinned`, true when any try ran pinned to the probe's address.
        Distinct from `pinned`, which is the workflow BASE pin.

        WHERE THE COMMIT CAME FROM: `source` is always `forge`. archived issue 940's clone
        bundles added `bundle` and `bundle+delta`; they were retired on
        2026-10-11 (docs/clone-bundles.md), and the field stays so a reader
        of events from both sides of that date needs no second rule.
        """
        if self._clone_marked:
            return
        log = [dict(entry) for entry in tries]
        timings: dict[str, Any] = {
            "seconds": round(
                float(clone.duration_seconds) if clone is not None
                else sum(float(entry.get("seconds") or 0.0) for entry in log),
                3,
            ),
            "tries": len(log),
            "pinned": bool(pinned),
            "ok": clone is not None,
            "try_log": log,
            **(clone.phases if clone is not None else {}),
        }
        last = log[-1] if log else {}
        timings["source"] = "forge"
        timings.setdefault("probe_peer", last.get("probe_peer"))
        timings.setdefault("git_peer", last.get("git_peer"))
        timings["peer_pinned"] = any(bool(entry.get("peer_pinned")) for entry in log)
        # Set before the write, so a failed write is not tried again later.
        self._clone_marked = True
        self._emit_startup_mark("clone_timed", {"clone": timings})

    def _mark_agent_started(self) -> None:
        """`agent_started`: the moment the agent process started (archived issue 667, lane OB1).

        MEASURED 2026-10-06 (chunk-1 observer): dispatch -> container start
        was 62-65 s while dispatch -> agent running was 107-141 s. The
        attempt's `started_at` is written at the walk to RUNNING, BEFORE the
        checkpoint restore, the clone, the credential fetch, the account hold
        and the issue fetch, so it cannot say when the agent began. This
        event's `at` is that moment: the first runner start of the attempt,
        once, never an in-place restart. `seconds_since_process_start` is the
        worker's own clock from its first line; `cloned` says whether this
        attempt made a clone (a checkpoint can bring one back instead).

        `issue_fetch` (archived issue 721, P29), when the step was pointed at an issue:
        `started_seconds` and `ended_seconds` of the fetch on the same clock,
        `ok`, and `beside_clone` -- true when it ran in its thread beside the
        clone (`_start_issue_prefetch`), false when it was made at STEP 6b.
        """
        if self._agent_start_marked:
            return
        self._agent_start_marked = True
        detail: dict[str, Any] = {
            "seconds_since_process_start": float(self.phases.seconds_since_start()),
            "cloned": self._clone_marked,
        }
        if self._issue_fetch_timing is not None:
            detail["issue_fetch"] = dict(self._issue_fetch_timing)
        self._emit_startup_mark("agent_started", detail)

    # -- periodic work -----------------------------------------------------
    def _usage_reading(self) -> dict[str, Any]:
        """One HEARTBEAT event's detail: the attempt's usage so far.

        The ATTEMPT's usage, every runner so far plus the live one. A runner
        restarted in place gets a fresh sampler that starts from zero, and a
        cumulative series built on it went backwards there.

        `cpu_seconds` is CUMULATIVE, not a rate: this event series is the only
        time series the platform keeps, and two consecutive totals give
        utilisation over exactly the span between them, where a point-in-time
        rate would describe two seconds out of every heartbeat period. None
        while not measured, never 0. `cpu_source` says whether it is the
        container (cgroup) or the runner's process tree (proc).

        EXACTLY WHAT IT CARRIED BEFORE archived pull request 188. That change put the peak, the
        mean, the limit and a `final` flag here too, because the frozen
        `Attempt` had nowhere else for them. Contract request #15 gave them
        typed fields on the attempt (`_record_cpu`), which replaced this
        interim home; the reconciler's stuck-browser judgement
        (`reconciler.progress`) is what still reads `cpu_seconds` here.

        AND USE OVER TIME (LIVE-UTIL-API, archived issue 1689, owner request 2026-10-20),
        which `GET /v1/tasks/{id}/utilisation` serves:

          series                the 5-second points taken since the previous
                                beat (`ResourceSampler.sample_point`), the
                                newest `HEARTBEAT_POINTS`, each served once;
          series_clock          now, on the points' clock, so a reader dates a
                                point as this event's `at` less
                                `series_clock - t`;
          memory_limit_bytes    what `mem` is a fraction of: cgroup
                                `memory.max`, else the sized class's;
          throttled_seconds,    the attempt's throttling, every runner so far.
          peak_throttled_share, They would sit beside the CPU figures on the
          mean_throttled_share  attempt, but `Attempt` is frozen: contract
                                change request 107 asks for typed fields.

        The detail is held under `HEARTBEAT_DETAIL_MAX_BYTES`.
        """
        usage = self._attempt_usage()
        cpu_seconds = usage.cpu_seconds if usage else None
        child_seconds = self._child.elapsed_seconds if self._child else None
        points = self._pending_points
        self._pending_points = []
        points.extend(self._drain_points())
        series_clock: float | None = None
        if self._series_origin is not None:
            series_clock = time.monotonic() - self._series_origin
        if self._sampler is not None:
            try:
                series_clock = self._sampler.series_elapsed()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not read the series clock", error=f"{type(exc).__name__}: {exc}")
        return {
            # The heartbeat's clock: 0 before the agent child starts, as it
            # always was (a clock reading, not a stored run duration).
            "elapsed_seconds": round(child_seconds, 1) if child_seconds is not None else 0,
            # The working set (`metrics.working_set_bytes`), page cache
            # excluded: the figure the reconciler's memory judgement reads.
            "peak_rss_bytes": usage.peak_rss_bytes if usage else None,
            "checkpoints": self.checkpoints.seq,
            "cpu_seconds": round(cpu_seconds, 3) if cpu_seconds is not None else None,
            "cpu_source": usage.cpu_source if usage else None,
            "series": points[-HEARTBEAT_POINTS:],
            "series_clock": round(series_clock, 1) if series_clock is not None else None,
            "memory_limit_bytes": self._memory_limit_bytes(),
            "throttled_seconds": _rounded3(usage.throttled_seconds if usage else None),
            "peak_throttled_share": _rounded3(usage.peak_throttled_share if usage else None),
            "mean_throttled_share": _rounded3(usage.mean_throttled_share if usage else None),
        }

    def _drain_points(self) -> list[dict[str, Any]]:
        """The live sampler's points since the last drain; [] without one. Never raises:
        a heartbeat goes out, and extends nothing less, whatever the series did."""
        if self._sampler is None:
            return []
        try:
            return list(self._sampler.drain_points())
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read utilisation points", error=f"{type(exc).__name__}: {exc}")
            return []

    def _memory_limit_bytes(self) -> int | None:
        """The container's memory limit: cgroup `memory.max`, else the scheduler's
        sized memory, else the sized class's (`WorkerConfig.memory_limit_bytes_of`)."""
        enforced = cgroup_memory_max()
        if enforced is not None:
            return enforced
        try:
            limit = int(self.cfg.memory_limit_bytes_of(self._memory_class()))
        except Exception:  # noqa: BLE001
            return None
        return limit if limit > 0 else None

    def _record_cpu(self, *, periodic: bool = False) -> None:
        """The attempt's CPU, as typed fields on its own document. Never raises.

        CONTRACT REQUEST #15, accepted on #184 (2026-09-25): `cpu_seconds`,
        `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores` on `Attempt`,
        REPLACING archived pull request 188's interim path -- the same figures as flat keys on
        HEARTBEAT events, plus a `final` HEARTBEAT when each runner was reaped,
        which the API read back with an events query per request.

        Called with each periodic reading (`_heartbeat`), when each runner is
        reaped (`_stop_sampler`), and on every exit beside the spend
        (`_upload_outputs`, `_cleanup`), which covers a runner still alive at a
        crash or killed at cleanup without being reaped. The figures are the
        ATTEMPT's, every runner so far plus the live one, so an in-place
        restart continues them rather than starting again. Written only when
        they changed -- except the periodic reading (`periodic`), which is
        always written, because each write stamps `cpu_measured_at` (contract
        request #26, accepted on #184, 2026-09-26) and a reader ages the
        reading by it. With the figures goes `cpu_limit_source`: `cgroup` or
        `resource_class`, from `_cpu_limit`.

        NOT FENCED, like the memory peaks beside them: the attempt document is
        this attempt's own, and the fence guards the task, its lease and its
        event stream, none of which this touches. NOT WRITTEN on the
        tenant-mismatch exit, which must write nothing at all.

        Not fatal. A telemetry write that failed is logged; the attempt goes on.
        """
        if self._writes_forbidden:
            return
        cores, source = self._cpu_limit()
        fields = attempt_cpu_fields(self._attempt_usage(), limit_cores=cores, limit_source=source)
        if not fields or (fields == self._cpu_recorded and not periodic):
            return
        try:
            self.control.record_cpu_usage(fields)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not record CPU usage", error=f"{type(exc).__name__}: {exc}")
            return
        self._cpu_recorded = dict(fields)

    def _cpu_limit(self) -> tuple[float | None, str | None]:
        """The CPU limit the attempt's figures are a fraction of, in cores, and its source.

        cgroup v2 `cpu.max` first: what the kernel enforces. Otherwise the
        catalogue cpu of the class the container was SIZED with, which is
        `scheduler.dispatch.resource_class_for(task, profile)` -- the task's
        own class when the catalogue still has it, else the profile's -- and
        NOT `WorkerConfig.profile_resource_class`, which is always the profile's. A
        task submitted with a larger class than its profile's would otherwise
        have its CPU drawn against a limit its container never had. Restated
        rather than imported (the worker image does not install the
        scheduler); a test of the CPU sampler compares the two.

        WHERE IT CAME FROM IS RECORDED AGAIN (contract request #26, accepted
        on #184, 2026-09-26): `cgroup` when the kernel's `cpu.max` said it,
        `resource_class` when the catalogue did. Request #15's four fields
        dropped the interim HEARTBEAT's `cpu_limit_source`, and Details could
        only say `reported limit`. With `requests == limits` the two agree;
        nobody has yet read what Cloud Run's `cpu.max` holds, which is why the
        source is worth saying.

        THE SCHEDULER'S RENDERED LIMIT COMES BEFORE THE CATALOGUE
        (SIZES-WORKER-1, docs/design/runner-sizes.md §4.4): once CPU bursts,
        a `standard` container's limit is 8 cores over a request of 4, and
        the catalogue's `cpu` is the request. `SWARM_SIZED_CPU_LIMIT` says
        what this container was given, with no task document needed. Still
        recorded as `resource_class`: the frozen `cpu_limit_source` has two
        words, and this is the class's size as the platform set it. `cpu.max`
        still wins, because on a cluster that does not burst Autopilot sets
        the limit to the request, and the record must say what was enforced.

        `(None, None)` when neither the kernel nor the sized environment says
        it and the task document has not been read: nothing yet says which
        class this attempt was sized with, and a guess would be drawn as a fact.
        """
        try:
            cores = cgroup_cpu_limit_cores()
        except Exception:  # pragma: no cover - a telemetry read never fails an attempt  # noqa: BLE001
            cores = None
        if cores is not None:
            return cores, "cgroup"
        if self.cfg.sized_cpu_limit is not None:
            return float(self.cfg.sized_cpu_limit), "resource_class"
        sized = self._sized_resource_class()
        if sized is None:
            return None, None
        return float(RESOURCE_CLASSES[sized].cpu), "resource_class"

    def _sized_resource_class(self) -> str | None:
        """The class the container was sized with; None before the task is read.

        What the CPU limit (`_cpu_limit`), the OOM near-miss limit
        (`_start_sampler`) and the exported metric's `resource_class`
        (`_export_metrics`) are all read against (archived issue 205).
        """
        task = self._task
        if not task:
            return None
        return self.cfg.sized_resource_class(task.get("resource_class"))

    def _memory_class(self) -> str:
        """`_sized_resource_class`, or the profile's before the task is read.

        The sampler starts with the runner, which starts after the task was
        read, so the fallback is for a path that has no task yet and still
        needs a label -- never a guess drawn as the container's size.
        """
        return self._sized_resource_class() or self.cfg.profile_resource_class

    # -- carrier: branches (D13) --------------------------------------------
    def _push_carrier_branch(self, label: str) -> None:
        """`carrier: branches`: push the step's COMMITTED work to its branch. Never raises.

        Owner decision, 2026-10-01 (D13, docs/design/dispatch-and-integration.md
        4.3). With `carrier: branches` a checkpoint also pushes what the
        agent has committed to `<git_branch_prefix><task id>`, the branch the
        publish pushes, so the work survives the platform and a dependant
        starts from it. `carrier: checkpoints`, the default, returns at once:
        nothing below runs and nothing changes.

        NEVER FORCED. The work is ONE worker commit of the agent's committed
        tree on top of the branch's current tip (`gitops.commit_tree_onto`),
        so each push fast-forwards the last, across checkpoints and attempts.
        It goes through the publish's own gates: the reap, a worker-owned
        repository holding the agent's commits fetched with object checking,
        the final-tree leak scan, the authorship check, and `push_branch`,
        which refuses a protected branch and anything outside the prefix.

        NOT WHILE THE AGENT RUNS. The tenant token is never in hand while agent
        code can run (`_publish_git`): the agent shares this worker's uid, so
        the credential file and the token-bearing git process would be within
        its reach. A checkpoint taken with the runner alive -- the periodic
        one, and the control-plane-outage one, which archived issue 70 takes BEFORE it stops
        the runner -- pushes nothing and says so. The checkpoints taken once
        the runner has stopped (final, park, cancellation) push. The owner
        accepted this narrowing of D13's "every checkpoint" on 2026-10-02:
        periodic checkpoints do not push. The SIGTERM checkpoints do not push
        either (archived issue 453): the forge is not called inside the grace window the
        park has to land in (`_handle_interruption`).

        A FENCE IS RE-CHECKED RIGHT BEFORE THE PUSH (archived issue 453), and raised like
        the checkpoint's own: the fold onto the tip, the scan and the
        authorship check take long enough for a reclaim to land after the
        checkpoint's pointer write, and a superseded worker must not move
        `swarm/<task>` under the attempt that replaced it (invariant 5). A
        check that could not be made is not an owner: nothing is pushed.

        A FAILED PUSH IS LOGGED AND NEVER ENDS THE ATTEMPT. The checkpoint is
        the durable record either way.
        """
        if self._dispatch_carrier() != "branches":
            return
        try:
            # The fetch of the branch's tip and the push are network calls to
            # the forge, as long as the archive upload can be, so the lease is
            # beaten meanwhile exactly as it is under the archive.
            with self._heartbeat_meanwhile(f"carrier push ({label})"):
                self._carrier_push(label)
        except (FencedError, TenantMismatchError):
            # Not a failed push: the attempt is over (invariant 5), and every
            # caller of `_checkpoint` already ends it on these.
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "carrier: the step's branch was not pushed at this checkpoint; the "
                "attempt goes on",
                label=label,
                error=self._scrub(f"{type(exc).__name__}: {str(exc)[:500]}"),
            )

    def _carrier_push(self, label: str) -> None:
        ws = self.ws
        assert ws is not None
        repo = ws.work / REPO_DIR_NAME
        if not (repo / ".git").exists():
            return
        child = self._child
        if child is not None and child.poll() is None:
            self.log.info(
                "carrier: not pushing while the runner is running; the tenant token "
                "is never in hand while agent code can run",
                label=label,
            )
            return
        target = self._carrier_target()
        if isinstance(target, str):
            self.log.info("carrier: the step's branch was not pushed", label=label, reason=target)
            return
        url, token, branch, protected = target
        survivors = self.reap_before_publish()
        if survivors:
            self.log.error(
                "carrier: not pushing: agent processes survived the reap",
                surviving_pids=list(survivors),
            )
            return
        publish_repo = self._build_clean_repo(repo, floor=self._publish_base)
        self._carrier_push_from(
            publish_repo, url=url, token=token, branch=branch, protected=protected,
            message=self._worker_commit_message(
                f"swarm: checkpoint ({label}) of the committed work",
                "Pushed by the worker at a checkpoint (carrier: branches), so the "
                "step's committed work survives the platform and a later step can "
                "start from it.",
            ),
        )

    def _carrier_target(self) -> tuple[str, str, str, tuple[str, ...]] | str:
        """`(url, token, branch, protected)` for a carrier push, or why there is none.

        The publish's own gates, in its order (`_publish_git`): publishing
        enabled, a repository URL, the token refusal asked BEFORE the token is
        read, a forge that grants write, and the branch derived from the task
        id. Never logs the token.
        """
        cfg = self.cfg
        if not cfg.git_publish_enabled:
            return "publishing is disabled for this worker"
        url = self._repo_url or cfg.repository_url
        if not url:
            return "the repository URL is unknown"
        if self._publish_base is None:
            return "the clone base is unknown, so the agent's commits cannot be told apart"
        # Before the token, as the publish asks it (OB5): a read grant, or a
        # grant gone since submission, pushes nothing and reads no token.
        forge_refused = self._forge_write_refusal()
        if forge_refused is not None:
            return f"{forge_refused[0]}: {forge_refused[1]}"
        refusal = self._git_token_refusal()
        if refusal:
            return f"the tenant git token is refused: {refusal}"
        token = self._git_token(write=True)
        if not token:
            return "no credential"
        try:
            token, access = self._probe_reading_again(url, token, "the carrier target probe")
        except ForgeError as exc:
            return f"could not reach the forge: {self._scrub(str(exc)[:300])}"
        if access is None:
            return "this repository is not on a forge this worker can publish to"
        if not access.can_push:
            return str(access.reason)
        branch = continuation_mod.publish_branch(
            (self._task or {}).get("metadata"), cfg.git_branch_prefix, cfg.task_id
        )
        protected = (access.default_branch,) if access.default_branch else ()
        return url, token, branch, protected

    def _carrier_scope_refusal(self) -> Callable[[], Outcome] | None:
        """For `carrier: branches`, the failure to make before the agent runs, or None.

        Owner decision, 2026-10-02: the write-scope check is the WORKER's.
        swarm-api reads no tenant's git secret, so it cannot know whether the
        token can push; this asks the forge with the tenant's own token, the
        way the carrier push and the publish ask it (`probe_repository`).

          * NO GIT CREDENTIAL, or a token whose `permissions.push` is not
            True: FAILED now, NOT retried, cause `forge_read_only`. The next
            attempt reads the same secret and asks the same forge.
          * The forge could not be asked -- a network failure (`ForgeError`),
            a 429 or a 5xx: the attempt fails RETRYABLY, cause
            `forge_unreachable`.
          * A TOKEN THE MEMORY GUARD REFUSES TO READ (archived issue 453): FAILED now, NOT
            retried, cause `git_token_refused`. It used to proceed, and the
            agent then worked for an attempt whose branch no checkpoint and no
            finish could ever push, with nothing on the task to say so.
          * Everything else proceeds unchanged. A worker with publishing
            disabled, or a host that is not a forge this worker publishes to,
            pushes nothing at its checkpoints, exactly as `_carrier_target`
            already says in its log line; neither is a statement about the
            token's scope.

        Returned as a callable, like `_prepare`'s parks, so the terminal writes
        happen after the startup window closes. Never logs or quotes the
        token: the reasons are the probe's own words, and both are scrubbed.
        """
        cfg = self.cfg
        if not cfg.git_publish_enabled:
            return None
        url = self._repo_url or cfg.repository_url
        if not url:
            # swarm-api refuses `branches` without a repository (422), so this
            # is a task written by another path; the carrier logs and skips.
            return None
        refusal = self._git_token_refusal()
        if refusal:
            return functools.partial(self._fail_git_token_refused, refusal)
        token = self._git_token(write=True)
        if not token:
            return functools.partial(
                self._fail_forge_read_only, url,
                "no git credential is registered for this tenant",
            )
        try:
            token, access = self._probe_reading_again(url, token, "the push-scope probe")
        except ForgeError as exc:
            return functools.partial(
                self._fail_forge_unreachable, url, self._scrub(str(exc)[:300])
            )
        if access is None:
            return None
        if access.can_push:
            self.log.info("carrier: the tenant token can push to the step's repository")
            return None
        reason = str(access.reason)
        if _FORGE_TRANSIENT_ANSWER.match(reason):
            return functools.partial(self._fail_forge_unreachable, url, self._scrub(reason))
        return functools.partial(self._fail_forge_read_only, url, self._scrub(reason))

    def _fail_git_token_refused(self, refusal: str) -> Outcome:
        """FAILED, not retried, before the agent ran: this worker may not hold the token."""
        reason = self._scrub(refusal)
        error = self._scrub(
            f"{GIT_TOKEN_REFUSED}: carrier 'branches' pushes the step's work to its "
            f"branch, and this worker cannot hold the tenant git token to push it: "
            f"{reason}. The agent was not started. Choose carrier 'checkpoints', or "
            "run the worker where its memory can be made unreadable to the agent."
        )
        self.log.error(
            "carrier: the memory guard refuses the git token, so the branch could "
            "never be pushed; failing before the agent runs",
            cause=GIT_TOKEN_REFUSED,
            reason=reason,
        )
        summary = self._upload_outputs()
        summary["carrier_check"] = {"cause": GIT_TOKEN_REFUSED, "reason": reason}
        with self._metrics_after_the_record():
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _fail_forge_read_only(self, url: str, reason: str) -> Outcome:
        """FAILED, not retried, before the agent ran: the token cannot push (D13)."""
        error = self._scrub(
            f"{FORGE_READ_ONLY}: carrier 'branches' pushes the step's work to its "
            f"branch, and this tenant's forge credential cannot push to {url}: "
            f"{reason}. The agent was not started. Choose carrier 'checkpoints', "
            "or store a token with write access (scripts/create-secrets.sh "
            "--provider git --stdin)."
        )
        self.log.error(
            "carrier: the tenant token cannot push; failing before the agent runs",
            cause=FORGE_READ_ONLY,
            reason=reason,
        )
        summary = self._upload_outputs()
        summary["carrier_check"] = {"cause": FORGE_READ_ONLY, "reason": reason}
        with self._metrics_after_the_record():
            self.control.finish(
                state=TaskState.FAILED,
                exit_code=None,
                error=error,
                result_summary=summary,
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _fail_forge_unreachable(self, url: str, reason: str) -> Outcome:
        """A retryable failure before the agent ran: the forge could not be asked."""
        error = self._scrub(
            f"{FORGE_UNREACHABLE}: carrier 'branches' needs to know whether this "
            f"tenant's forge credential can push to {url}, and the forge could not "
            f"be asked: {reason}. The agent was not started; the attempt is "
            "retried while the task has attempts left."
        )
        self.log.warning(
            "carrier: the forge could not be asked; failing the attempt retryably "
            "before the agent runs",
            cause=FORGE_UNREACHABLE,
            reason=reason,
        )
        summary = self._upload_outputs()
        summary["carrier_check"] = {"cause": FORGE_UNREACHABLE, "reason": reason}
        with self._metrics_after_the_record():
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=FORGE_UNREACHABLE,
                result_summary=summary,
                retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                detail={"carrier": "branches"},
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _fail_mcp_unattached(self, cause: str, detail: str, retryable: bool) -> Outcome:
        """The attempt ends before its agent ran: a registered MCP server the
        task names was withheld, or the attach could not be made (MCP-WORKER,
        design §4.7 step 2). The caller named the server because the agent
        needs it; work built without it would rest on knowledge the agent was
        told it had. Retryable only when a later attempt may get it
        (`unreadable`, an unreachable swarm-api); `credential_expired` and
        the other reasons are final. `detail` holds names and reason words,
        never a value.
        """
        error = self._scrub(
            f"{cause}: the task names registered MCP servers this attempt could not "
            f"attach ({detail}). The agent was not started"
            + ("; the attempt is retried while the task has attempts left." if retryable
               else ".")
        )
        self.log.warning(
            "mcp servers not attached; failing before the agent runs",
            cause=cause, detail=detail, retryable=retryable,
        )
        summary = self._upload_outputs()
        summary["mcp_check"] = {"cause": cause, "detail": detail, "retryable": retryable}
        with self._metrics_after_the_record():
            if not retryable:
                self.control.finish(
                    state=TaskState.FAILED,
                    exit_code=None,
                    error=error,
                    result_summary=summary,
                    end_cause=EndCause.CANNOT_START,
                )
                return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=cause,
                result_summary=summary,
                retry_delay_seconds=FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                detail={"mcp": detail},
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _forge_retry(self) -> forge_mod.RetryPolicy:
        """The bound on retrying a forge call that failed transiently (F1).

        `cfg.forge_read_attempts` tries, sharing one wall-clock budget: the
        smaller of `max_in_worker_retry_delay_seconds` and what is left of the
        step's deadline, read when the call starts. Never the long in-worker
        wait invariant 4 forbids: a `Retry-After` past the budget is not slept
        at all, and the caller fails the attempt retryably instead.
        """
        return forge_mod.RetryPolicy.bounded(
            attempts=self.cfg.forge_read_attempts,
            max_in_worker_retry_delay_seconds=self.cfg.max_in_worker_retry_delay_seconds,
            remaining_seconds=self._remaining_seconds(),
            sleep=self.forge_sleep,
            log=self.log,
        )

    def _fail_issue_unreachable(
        self, number: int, reason: str, retry_after: int | None, tries: int
    ) -> Outcome:
        """A retryable failure before the agent ran: the issue's forge did not answer.

        The measured failure (run_51e2e460eef54d208986, 2026-10-04): one
        "could not reach api.github.com: timed out" ended the review step
        INPUTS_UNAVAILABLE after ONE attempt, and `fail_workflow` took the
        run with it. A forge that did not answer says nothing about the issue,
        so this ends only the ATTEMPT (`fail_retryably`): the task goes back
        to READY while it has attempts left, its lease and capacity are
        released, and the scheduler admits it again after the forge's own
        `Retry-After` or `FORGE_UNREACHABLE_RETRY_DELAY_SECONDS`, whichever
        is longer. A task whose attempts are spent ends CANNOT_START -- the
        agent could not be started -- and never INPUTS_UNAVAILABLE, which
        stays the answer to a forge that said the issue is not there.
        """
        error = self._scrub(
            f"{FORGE_UNREACHABLE}: input.issue asks for issue #{number}, and the "
            f"forge did not answer after {tries} tries: {reason}. The agent was not "
            "started; the attempt is retried while the task has attempts left."
        )
        self.log.warning(
            "the issue's forge did not answer; failing the attempt retryably "
            "before the agent runs",
            cause=FORGE_UNREACHABLE,
            issue=number,
            tries=tries,
        )
        summary = self._upload_outputs()
        summary["issue_check"] = {"cause": FORGE_UNREACHABLE, "issue": number, "tries": tries}
        with self._metrics_after_the_record():
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=FORGE_UNREACHABLE,
                result_summary=summary,
                retry_delay_seconds=max(
                    FORGE_UNREACHABLE_RETRY_DELAY_SECONDS, int(retry_after or 0)
                ),
                detail={"input": "issue", "issue": number},
                end_cause=EndCause.CANNOT_START,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

    def _scan_floor(
        self, publish_repo: Path, *, url: str, token: str, default_branch: str | None,
        tip: str | None = None,
    ) -> str | None:
        """The commit the publish scans read from when it is not the clone base, or None.

        A LANE THAT MERGED THE DEFAULT BRANCH (archived issue 453). The scans read what the
        push adds as a diff from the clone base, so a lane that merged a newer
        `main` carried every line `main` gained since the clone into it, and a
        credential-shaped fixture on `main` refused the lane. Those lines are
        published already: they are on the repository's default branch.

        So when HEAD's first-parent line since the base holds a merge, the
        default branch's tip is fetched from the FORGE into the worker's
        publish repository (`fetch_branch_tip`, the token-bearing fetch the
        carrier uses), and the floor is the newest commit HEAD and that tip
        share -- where the lane merged, as the forge's own pull-request diff
        reads it. Every line the floor's tree holds is on the default branch,
        so reading from it hides nothing unpublished. Proven against the
        forge, never the agent's clone: a side branch the agent made and
        merged is no ancestor of the tip, the floor stays the base, and its
        lines are scanned as before.

        No merge, no default branch, a fetch that fails, or a floor that is
        the base itself: None, and the scans read from the clone base.

        `tip`, when given, is the default branch's tip the caller already
        fetched from the forge (`_default_branch_tip`, a conflict fix round),
        and it is not fetched again.

        THE FLOOR IS ALSO WHAT KEEPS A MERGE A MERGE (archived issue 1321). The replay and
        the fold keep a merge parent that is the floor or one of its
        ancestors -- a commit the forge's default branch holds -- so a lane
        that merged `main` pushes a two-parent merge and its pull request's
        merge base moves to where it merged.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        base = (self._publish_base or "").strip()
        if not default_branch or not _SHA_RE.match(base):
            return None
        g = ["git", *_NO_HOOKS]

        def run(argv: list[str], slug: str) -> tuple[int, str]:
            return _git_text(
                argv,
                repo=Path(publish_repo),
                private_dir=ws.private,
                logs_dir=ws.logs,
                slug=slug,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )

        code, merges = run(
            [*g, "rev-list", "--first-parent", "--merges", f"{base}..HEAD"], "publish-floor-merges"
        )
        if code != 0 or not merges.split():
            return None
        if not tip:
            tip = self._default_branch_tip(
                publish_repo, url=url, token=token, default_branch=default_branch
            )
        if not tip:
            return None
        code, found = run([*g, "merge-base", "HEAD", tip], "publish-floor-base")
        floor = found.strip()
        if code != 0 or not _SHA_RE.match(floor) or floor == base:
            return None
        self.log.info(
            "the lane merged the default branch; the publish scans read from where it merged",
            floor=floor,
            base=base,
        )
        return floor

    def _forge_holder(self, *, url: str, token: str) -> Callable[[str], bool]:
        """Whether the forge serves a commit (`forge_holds_commit`), each sha
        asked once: the replay and the fold ask of every merged head (archived issue 3359)."""
        ws = self.ws
        assert ws is not None
        answers: dict[str, bool] = {}

        def holds(sha: str) -> bool:
            if sha not in answers:
                answers[sha] = forge_holds_commit(
                    url=url, sha=sha, token=token, private_dir=ws.private,
                    logs_dir=ws.logs, timeout_seconds=self.cfg.git_clone_timeout_seconds,
                    logger=self.log,
                )
            return answers[sha]

        return holds

    def _default_branch_tip(
        self, publish_repo: Path, *, url: str, token: str, default_branch: str | None
    ) -> str | None:
        """The default branch's tip, fetched from the FORGE into the publish
        repository (`fetch_branch_tip`, the token-bearing fetch the carrier
        uses), or None when there is no default branch or the fetch failed.
        Never read from the agent's clone, whose `origin/main` the agent can
        point anywhere."""
        if not default_branch:
            return None
        ws = self.ws
        assert ws is not None
        try:
            return fetch_branch_tip(
                repo=publish_repo,
                url=url,
                branch=default_branch,
                token=token,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                logger=self.log,
                # The default branch is read, never written: `push_branch`
                # still refuses it (`protected`), and the prefix rule governs
                # what this worker may push.
                branch_prefix="",
            )
        except GitError as exc:
            self.log.warning("could not read the default branch from the forge",
                             error=str(self._scrub(str(exc)[:300])))
            return None

    def _conflict_round(self) -> bool:
        """True when this step is a merge step's conflict fix round (archived issue 1076):
        `dispatch.conflict_round`, inside the signed block (archived issue 1321)."""
        return self._dispatch_block().get(CONFLICT_ROUND_FIELD) is True

    def _publish_identity(self) -> tuple[str, str]:
        """(name, email) every commit the worker pushes is written as.

        THE PERSON WHO DISPATCHED THE WORK, as author AND committer (archived issue 765,
        owner decision 2026-10-07: publish fully as the person). Read through
        `gitidentity.commit_identity` -- the one reader of the
        `metadata.dispatch.git_identity` record archived pull request 764 writes at submission,
        from the verified task document and never from `input` (invariant
        10) -- so the agent's commits and the worker's name the same person.

        When it names nobody (a service account's task with no usable
        record, or no submitter) it answers `BOT_GIT_IDENTITY`, and the
        worker keeps today's identity, `WorkerConfig.git_author_*`: the
        bot every SwarmCloud branch was published as before.
        """
        person = commit_identity(self._task)
        if person == BOT_GIT_IDENTITY:
            return self.cfg.git_author_name, self.cfg.git_author_email
        return person

    def _carrier_fold_onto_tip(
        self, publish_repo: Path, *, url: str, token: str, branch: str, message: str
    ) -> str | None:
        """Put HEAD's tree on the branch's current tip as one worker commit.

        The tip is what an earlier push of this step left (`fetch_branch_tip`),
        or the clone base before any. Returns the new commit, None when the tip
        already holds this tree.

        The commit is a TREE on top of the tip, not a replay. When a retry
        cloned a newer default branch than the one the tip was built on, the
        tree carries the default branch's intervening changes too, and the
        branch's diff against its first parent shows them. The branch still
        fast-forwards, and its tree is exactly the work; only that one
        commit's diff is noisier. Rebasing it would need a force push, which
        this carrier never does.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        tip = fetch_branch_tip(
            repo=publish_repo,
            url=url,
            branch=branch,
            token=token,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_clone_timeout_seconds,
            logger=self.log,
            branch_prefix=cfg.git_branch_prefix,
        )
        parent = tip or self._publish_base
        if not parent or parent == EMPTY_CLONE_BASE:
            raise GitError(
                "the repository was empty when it was cloned and the branch does "
                "not exist yet; there is no commit to push the work onto"
            )
        author_name, author_email = self._publish_identity()
        return commit_tree_onto(
            repo=publish_repo,
            parent=parent,
            message=message,
            author_name=author_name,
            author_email=author_email,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )

    def _carrier_push_from(
        self,
        publish_repo: Path,
        *,
        url: str,
        token: str,
        branch: str,
        protected: tuple[str, ...],
        message: str,
    ) -> None:
        """Scan, commit onto the tip, check authorship, push. Raises GitError.

        A HEAD that moved off this step's work (`_head_moved`, archived issue 2094) is not
        carried: the fold would put the tree of a branch the agent fetched on
        `swarm/<task>` as this step's checkpoint, exactly what the harvest
        refuses to publish. That push is skipped with the harvest's reason.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        moved = self._head_moved(ws.work / REPO_DIR_NAME, publish_repo, self._publish_base)
        if moved is not None:
            self.log.info(
                "carrier: the step's branch was not pushed",
                reason=self._moved_reason(str(self._scrub(moved))),
            )
            return
        leak = final_tree_leak(
            repo=publish_repo,
            base=self._publish_base,
            leaks=self._leaks_in_added_text,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
            overlap=self._scan_overlap(),
            floor=self._scan_floor(
                publish_repo, url=url, token=token,
                default_branch=protected[0] if protected else None,
            ),
        )
        if leak is not None:
            raise GitError(f"refusing to push: {self._scrub(leak)}")
        made = self._carrier_fold_onto_tip(
            publish_repo, url=url, token=token, branch=branch, message=message
        )
        if made is None:
            self.log.info("carrier: the branch already holds this work", branch=branch)
            return
        author_name, author_email = self._publish_identity()
        verify_worker_authorship(
            repo=publish_repo,
            base=self._publish_base,
            author_name=author_name,
            author_email=author_email,
            also_own=((cfg.git_author_name, cfg.git_author_email),),
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        # THE LAST THING BEFORE THE PUSH (archived issue 453): this attempt still owns its
        # task. Raises `FencedWriteRefused` when it does not, and anything
        # else when the check could not be made -- either way nothing is
        # pushed (`_push_carrier_branch`).
        self.control.ensure_owner(write="carrier push")
        pushed = push_branch(
            repo=publish_repo,
            url=url,
            branch=branch,
            token=token,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_clone_timeout_seconds,
            logger=self.log,
            branch_prefix=cfg.git_branch_prefix,
            protected=protected,
        )
        self._carrier_pushed = {"name": branch, "head": pushed or made}
        self._record_pushed_branch(publish_repo, branch=branch, head=pushed or made)
        self.log.info("carrier: pushed the step's committed work", branch=branch)

    def _record_pushed_branch(self, publish_repo: Path, *, branch: str, head: str) -> None:
        """Keep what a push just landed, read from git, for `result_summary.branch`.

        MEASURED 2026-10-06 (archived issue 667, observer P11): C1A's agent named a branch,
        `c1a-631-failure-classes`, that was never pushed; the real one was
        `swarm/task_<id>`. This record is the worker's, not the agent's: the
        name it pushed, the head it pushed (`head`, as `carrier: branches`
        always recorded it, and `head_sha`), the base it built on
        (`base_sha`, None for an empty repository's first push) and the
        commits between, newest first, from `gitops.branch_commits` in the
        publish repository the push ran in. Never raises: the push has
        happened, and a record that could not be read leaves the commits out.
        """
        ws = self.ws
        assert ws is not None
        base = self._publish_base
        base_sha = base if isinstance(base, str) and _FULL_SHA_RE.fullmatch(base) else None
        try:
            commits, truncated = branch_commits(
                repo=publish_repo,
                base=base_sha,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "could not read the pushed branch's commits",
                branch=branch,
                error=f"{type(exc).__name__}: {exc}",
            )
            commits, truncated = [], False
        self._pushed_branch = {
            "name": branch,
            "head": head,
            "head_sha": head,
            "base_sha": base_sha,
            "commits": commits,
            "commits_truncated": truncated,
        }

    # -- uploads -----------------------------------------------------------
    def _scrub(self, value: Any) -> Any:
        """Redact every registered secret from a value bound for Firestore."""
        return self.log.scrub_value(value)

    def _redact_before_upload(self) -> list[dict[str, Any]]:
        """Scrub the worker's own files in place before anything leaves the pod.

        THE THREE FILES OUTSIDE THE ARTIFACTS FOLDER: the runner's
        `stdout.log` and `stderr.log`, and `result.json`. Each is rewritten
        where it lies, because it is read again -- the streams are uploaded to
        `logs/`, and `result.json` lives in `work/`, which a later checkpoint
        archives. Each is read without following a link and put back without
        following one either (`_redact_in_place`).

        NOTHING IN THE ARTIFACTS FOLDER IS REWRITTEN IN PLACE ANY MORE (archived issue 227).
        The pass used to rewrite each file bound for upload by path, after a
        leaf check for a link -- so a link put in the path between the check
        and the open, or a FOLDER link the leaf check never saw, had the
        worker rewrite whatever it pointed at, and upload it after. Every file
        taken from there is now copied once, with no link followed, into the
        worker's own scratch, and the copy is what is redacted and what is
        uploaded (`_upload_copy`). The agent CLI's captures, which go to
        `logs/` on every exit whatever the cap decided, are copied the same
        way there. The folder is never checkpointed, so nothing reads its
        files again after the upload.

        A log line is only one of four ways a provider key gets out. The other
        three are `stdout.log` and `stderr.log`, the runner's artifacts, and the
        result summary -- and the first two are uploaded to GCS, where they
        outlive the pod. The logger holds the registered values, so it does the
        rewriting; binary and oversized files are left alone by the rewrite,
        because corrupting a tenant's artifact to protect a key that is probably
        not in it is the wrong trade.

        THAT TRADE IS UNCHANGED; SAYING NOTHING ABOUT IT IS WHAT CHANGED
        (docs/audits/2026-09-18/02-agent-worker-credentials.md section 4). The
        rewrite's answer used to be discarded, so an artifact the rewrite
        skipped went to GCS with no signal at all. Now a skipped file's raw
        bytes are scanned for the registered values -- which answers whether
        "probably not in it" held -- and it is returned for the result summary
        unless the scan found it clean:
        `{"file", "reason", "bytes", "secret_found"}`, where `secret_found` is
        True (it IS in there, and the file is uploaded as-is) or None (the file
        could not be read, so nobody knows).

        Returns only those entries. A skipped file whose bytes hold no
        registered value is as clean as a rewritten one, and listing every PNG
        an agent writes would bury the entry that matters.
        """
        ws = self.ws
        if ws is None or not self.log.has_secrets:
            return []
        unredacted: list[dict[str, Any]] = []
        for path in (ws.stdout_path, ws.stderr_path, ws.result_path):
            entry = self._redact_in_place(path, label=_workspace_label(ws, path))
            if entry is not None:
                unredacted.append(entry)
        return unredacted

    def _redact_in_place(self, path: Path, *, label: str) -> dict[str, Any] | None:
        """Rewrite one of the worker's own files with no link followed. Never raises.

        Copied out with `copy_without_following` (its folder opened with
        `O_NOFOLLOW`, the file too), redacted as the copy, and -- only when
        the redaction rewrote it -- renamed back over the file through a
        descriptor for its folder, again opened with `O_NOFOLLOW`. A rename
        replaces a link put at the name meanwhile; it never writes through
        one. A file that is a link, or whose folder is, is left alone and
        said so. Returns the file's `redaction_skipped` entry, or None.
        """
        ws = self.ws
        assert ws is not None
        staging = ws.private / "redact-in-place"
        try:
            ws.private.mkdir(parents=True, exist_ok=True)
            standalone_mod.copy_without_following(
                path.parent, path.name, staging, limit=sys.maxsize
            )
        except FileNotFoundError:
            return None
        except standalone_mod.Refused as exc:
            self.log.warning(
                "not redacted in place: a symlink, or not a regular file, when it "
                "was read; nothing was followed",
                file=label,
                error=str(exc),
            )
            return None
        except (OSError, standalone_mod.OverCap) as exc:
            self.log.warning("could not redact a file before upload", file=label, error=str(exc))
            return None
        try:
            rewritten, entry = self._redact_outcome(staging, label=label)
            if rewritten:
                folder = os.open(
                    os.fspath(path.parent),
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.replace(staging, path.name, dst_dir_fd=folder)
                finally:
                    os.close(folder)
            return entry
        except OSError as exc:
            self.log.warning("could not redact a file before upload", file=label, error=str(exc))
            return None
        finally:
            staging.unlink(missing_ok=True)

    def _upload_copy(
        self,
        root: Path,
        relative: str,
        *,
        key: str,
        label: str,
        limit: int,
        content_type: str | None = None,
        absent_ok: bool = False,
        report: bool = True,
    ) -> _Upload:
        """Upload `root/<relative>` as it is AT THIS MOMENT, with no link followed.

        THE ONE WAY A FILE FROM THE ARTIFACTS FOLDER LEAVES THE POD (archived issue 227).
        Copied with `standalone_outputs.copy_without_following` into the
        worker's own scratch (`ws.private`, in no child's environment): every
        folder on the way, `root` included, is opened with `O_NOFOLLOW`
        relative to the one above it, and the file too, so a link anywhere in
        the path is refused instead of followed, whenever it was put there.
        The copy is redacted (`_redact_outcome`), uploaded and deleted. The
        store is never handed a path the agent can write.

        `limit` is the bytes the caller's cap leaves; a file past it, before or
        after redaction, is not uploaded. `absent_ok` makes a missing file
        silent (a stream that never wrote anything). With `report`, a file
        that left unredacted is logged as such -- AFTER its upload, so a file
        the cap or the store kept in the pod is never logged as "uploaded
        as-is" (archived issue 227); without it, the caller reports.
        """
        ws = self.ws
        assert ws is not None
        staging = ws.private / "upload-copy"
        shown = standalone_mod.shown(self._scrub(label))
        try:
            ws.private.mkdir(parents=True, exist_ok=True)
            standalone_mod.copy_without_following(root, relative, staging, limit=max(0, limit))
        except standalone_mod.OverCap:
            return _Upload(skipped=standalone_mod.OVER_CAP)
        except standalone_mod.Refused as exc:
            self.log.warning(
                "refused a file bound for upload: a symlink, or not a regular file, "
                "when it was read; nothing was followed",
                file=shown,
                error=str(exc),
            )
            return _Upload(skipped=standalone_mod.SYMLINK_REFUSED)
        except FileNotFoundError as exc:
            if not absent_ok:
                self.log.warning("artifact upload failed", artifact=shown, error=str(exc))
            return _Upload(skipped=standalone_mod.UNREADABLE)
        except OSError as exc:
            self.log.warning("artifact upload failed", artifact=shown, error=str(exc))
            return _Upload(skipped=standalone_mod.UNREADABLE)
        try:
            if self._browser_state_refused(staging, relative, label=label):
                return _Upload(skipped=manifest_mod.BROWSER_STATE_REFUSED)
            entry = None
            if self.log.has_secrets:
                _rewritten, entry = self._redact_outcome(staging, label=label, report=False)
            size = staging.stat().st_size
            if size > limit:
                # Redaction can lengthen a file; the cap is on what is uploaded.
                return _Upload(skipped=standalone_mod.OVER_CAP)
            if content_type is None:
                self.store.upload_file(key, staging)
            else:
                self.store.upload_file(key, staging, content_type=content_type)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("artifact upload failed", artifact=shown, error=str(exc))
            return _Upload(skipped=standalone_mod.UPLOAD_FAILED)
        finally:
            staging.unlink(missing_ok=True)
        if entry is not None and report:
            self._report_unredacted(entry)
        return _Upload(bytes=size, unredacted=entry)

    def _browser_state_refused(self, staging: Path, relative: str, *, label: str) -> bool:
        """True, and `browser_state_refused` recorded, when the copy about to
        be uploaded is shaped like a browser session (LB-L4b).

        docs/design/live-browser.md section 5.5: an agent can write a
        Playwright storage state or copy a Chromium `Cookies` database into
        its workspace or `$SWARM_ARTIFACTS_DIR`, and the output upload would
        publish the signed-in session. Judged on the worker's own copy, the
        bytes that would leave, under the file's own name
        (`artifact_manifest.browser_state_kind`). Before redaction, so a
        refused file is never rewritten or scanned either.
        """
        kind = manifest_mod.browser_state_of(staging, name=relative.rsplit("/", 1)[-1])
        if kind is None:
            return False
        self.log.warning(
            "a file shaped like a browser session was refused; it was not uploaded",
            event=manifest_mod.BROWSER_STATE_REFUSED,
            path=standalone_mod.shown(self._scrub(label)),
            kind=kind,
        )
        return True

    def _walk_artifacts(self, root: Path) -> tuple[dict[str, int], list[str]]:
        """Every regular file under the artifacts folder, and the folders not walked.

        `{relative POSIX name: size as lstat saw it}`, and the relative names
        of the folders passed over: too deep for any name under them to be an
        object's, or unreadable. Never raises, never follows a link, and never
        recurses: the walk keeps its own stack, so no depth of tree can raise
        RecursionError (archived issue 227), and a folder whose name alone leaves no room
        under `MAX_OBJECT_NAME_BYTES` is listed instead of walked, which also
        bounds the work a deep tree costs.

        A folder the agent replaced with a link is not walked at all.
        """
        found: dict[str, int] = {}
        not_walked: list[str] = []
        if root.is_symlink() or not root.is_dir():
            if root.is_symlink():
                self.log.warning(
                    "the artifacts folder is a symlink, so nothing was uploaded "
                    "from it; a link is never followed out of the workspace"
                )
            return found, not_walked
        stack = [""]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(root / folder if folder else root) as listing:
                    entries = list(listing)
            except OSError as exc:
                if folder:
                    self.log.warning(
                        "could not list a folder in $SWARM_ARTIFACTS_DIR; its files "
                        "were not uploaded",
                        folder=standalone_mod.shown(self._scrub(folder)),
                        error=str(exc),
                    )
                    not_walked.append(folder)
                continue
            for entry in entries:
                relative = f"{folder}/{entry.name}" if folder else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        # The shortest name under it is `<folder>/x`.
                        width = len(relative.encode("utf-8", "surrogateescape")) + 2
                        if width > standalone_mod.MAX_OBJECT_NAME_BYTES:
                            not_walked.append(relative)
                        else:
                            stack.append(relative)
                    elif entry.is_file(follow_symlinks=False):
                        found[relative] = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        if not_walked:
            self.log.warning(
                "folders in $SWARM_ARTIFACTS_DIR were not walked: too deep for any "
                "name under them to be stored, or unreadable",
                count=len(not_walked),
                folders=[standalone_mod.shown(self._scrub(name)) for name in not_walked[:50]],
                cap_name_bytes=standalone_mod.MAX_OBJECT_NAME_BYTES,
            )
        return found, not_walked

    def _redact_file(
        self, path: Path, *, label: str, withheld: bool = False
    ) -> dict[str, Any] | None:
        """Scrub one file bound for GCS in place; the `redaction_skipped` entry, or None.

        None when the file was rewritten, or was left alone and its raw bytes
        hold no registered value. `label` is what a reader can place: a path
        under the workspace, or the manifest name of a working-folder copy
        (`_upload_workdir_outputs`), whose scratch path means nothing to anyone.
        See `_redact_before_upload` for the trade this reports on. `path` is
        always the WORKER's copy in `ws.private`, never a path the agent can
        write (archived issue 227).

        `withheld` is the working-folder upload's answer to that trade: a file
        this returns an entry for is NOT uploaded, and the lines say so. An
        artifact the agent chose to deliver goes up as-is and is reported; a
        file caught by the net under the working folder does not (archived pull request 225
        review). A file the rewrite raised on is unexamined, so with
        `withheld` it gets an entry rather than a pass.
        """
        return self._redact_outcome(path, label=label, withheld=withheld)[1]

    def _redact_outcome(
        self, path: Path, *, label: str, withheld: bool = False, report: bool = True
    ) -> tuple[bool, dict[str, Any] | None]:
        """`_redact_file`, also saying whether the file was rewritten.

        Without `report`, the entry is returned and not logged: the caller
        logs it with `_report_unredacted` once the file has actually left.
        """
        label = standalone_mod.displayable(label)
        try:
            outcome = self.log.scrub_file_outcome(path, max_bytes=redact_mod.MAX_SCRUB_BYTES)
        except OSError as exc:  # a read-only or vanished file must not fail the attempt
            self.log.warning("could not redact a file before upload", file=label, error=str(exc))
            if withheld:
                return False, {
                    "file": label, "reason": "unreadable", "bytes": None, "secret_found": None,
                }
            return False, None
        if not outcome.skipped:
            return outcome is redact_mod.ScrubOutcome.REWRITTEN, None
        found = self.log.file_contains_secret(path)
        try:
            size: int | None = path.stat().st_size
        except OSError:
            size = None
        if found is False:
            self.log.info(
                "file not rewritten, and its raw bytes hold no registered secret",
                file=label,
                reason=outcome.value,
                bytes=size,
            )
            return False, None
        entry = {"file": label, "reason": outcome.value, "bytes": size, "secret_found": found}
        if report:
            self._report_unredacted(entry, withheld=withheld)
        return False, entry

    def _report_unredacted(self, entry: dict[str, Any], *, withheld: bool = False) -> None:
        """Log a file that could not be redacted, as what happens to it next."""
        found = entry.get("secret_found")
        if withheld:
            self.log.warning(
                "a working-folder file could not be redacted and "
                + (
                    "holds a registered secret"
                    if found
                    else "could not be scanned for one"
                )
                + "; it is NOT uploaded",
                **entry,
            )
        elif found:
            self.log.error(
                "A FILE THAT COULD NOT BE REDACTED CONTAINS A REGISTERED SECRET; "
                "it is uploaded as-is",
                **entry,
            )
        else:
            self.log.warning(
                "a file could be neither redacted nor scanned; it is uploaded unexamined",
                **entry,
            )

    # -- live logs ----------------------------------------------------------
    def _publish_live_logs(self) -> None:
        """Publish a bounded, scrubbed TAIL of each stream while the agent runs.

        WHY A TAIL AND NOT THE FILE. GCS has no append. Publishing the whole
        stream would rewrite up to `max_stdout_bytes` every interval, so the
        cost of watching a run would grow with the length of the run -- the
        opposite of what a tail is for. A fixed window keeps it flat.

        WHY IT IS SCRUBBED HERE TOO. `_redact_before_upload` runs once, on the
        way out. Anything published DURING the run has not been through it, so
        a provider key echoed into stdout would reach GCS in the clear and stay
        there: the object is overwritten by the next flush, but "it is gone
        five seconds later" is not a property anyone should rely on for a
        credential. The tail is scrubbed on every flush instead.

        It NEVER raises. A failed flush costs the watcher five seconds of
        staleness; failing the attempt over it would trade a running agent for
        a cosmetic feature.

        FOUR STREAMS, NOT TWO (#184). `stdout` and `stderr` are the RUNNER
        process's -- its own JSON log lines. `agent_stdout` and `agent_stderr`
        are the agent CLI's, which the runner captures into `artifacts/` under
        the names `runners.streams.agent_stream_files` gives; for claude-code
        `agent_stdout` is the stream-json transcript. A runner with no agent
        child (mock, browser) publishes the first two only.

        THE HEADER is `#swarm-tail offset=<raw byte of the first served byte>
        size=<raw stream size> at=<RFC 3339 UTC publish time>`. `at` is when
        THIS window was cut, which a reader shows as the read's age; the
        object's own GCS `updated` time says the same thing about the upload,
        and each tail is republished every interval even when unchanged, so
        "no new output" is an unchanged `size` across two polls, never a stale
        object. Readers must accept a header without `at` (every object
        published before this change).
        """
        ws = self.ws
        cfg = self.cfg
        if ws is None or not cfg.live_logs_enabled:
            return
        for label, path in self._stream_files(ws):
            self._publish_tail(label, path)

    def _stream_files(self, ws: Any) -> list[tuple[str, Path]]:
        """Every captured stream of this attempt, as `(label, local file)`.

        The ONE list both the live publisher and the final upload walk, so a
        stream's live object (`logs/live/<label>.tail.log`) and its final one
        (`logs/<label>.log`) cannot name it differently. The labels are the
        API's stream names (`swarm_api.inspect.LOG_STREAMS`), compared by a
        parity test that imports both, so a renamed stream fails there.
        """
        streams = [("stdout", ws.stdout_path), ("stderr", ws.stderr_path)]
        files = agent_stream_files(self.cfg.runner_profile)
        if files is not None:
            streams.append(("agent_stdout", ws.artifacts / files.stdout))
            streams.append(("agent_stderr", ws.artifacts / files.stderr))
        return streams

    def _publish_tail(self, label: str, path: Path) -> None:
        """Cut, scrub and upload one stream's tail. Never raises."""
        cfg = self.cfg
        try:
            # A symlink is refused, not followed. The agent can write inside
            # `artifacts/`, and a link planted at a capture file's name would
            # otherwise publish whatever it points at; the final upload refuses
            # links for the same reason.
            #
            # REFUSED AT THE OPEN, NOT BEFORE IT (archived issue 227). A leaf check and then
            # an open by path were two moments, and a link swapped in between
            # was followed -- as was a link at the stream's FOLDER, which the
            # leaf check never saw. The descriptor is opened with `O_NOFOLLOW`
            # for the folder and the file, and everything below reads it.
            try:
                fd = standalone_mod.open_without_following(path.parent, path.name)
            except FileNotFoundError:
                return
            except standalone_mod.Refused as exc:
                self.log.debug("a live log stream is not a regular file; not published",
                               stream=label, error=str(exc))
                return
            with os.fdopen(fd, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size == 0:
                    return
                start, chunk = _read_tail(handle, size=size, window=cfg.live_log_tail_bytes)
            text = chunk.decode("utf-8", errors="replace")
            if self.log.has_secrets:
                text = self.log.scrub_text(text)
            # The byte offset the window starts at, so a reader can tell a
            # gap (it polled too slowly and the window moved past what it
            # had) from a continuation. Without it a tailer silently
            # stitches two non-adjacent pieces of output together.
            header = f"#swarm-tail offset={start} size={size} at={_rfc3339_now()}\n"
            self.store.upload_bytes(
                f"{cfg.log_prefix}/live/{label}.tail.log",
                (header + text).encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
        except Exception as exc:  # pragma: no cover - never fail a run for this  # noqa: BLE001
            self.log.debug("could not publish a live log tail", stream=label, error=str(exc))

    def _dispatch_block(self) -> dict[str, Any]:
        """The `task.metadata["dispatch"]` block, or an empty one.

        swarm-api writes four fields here -- strategy, carrier, role and
        integrates (see DispatchOptions.to_metadata) -- and, on a step that
        declares them, `builds_on` and `verdict_gate` (archived issue 264). For a long time this
        worker read only the first, so `integrate` took the `direct-pr` path
        and every step of a workflow opened its own pull request against an
        API that had promised, in three places, to open exactly one.
        """
        task = self._task or {}
        metadata = task.get("metadata")
        if not isinstance(metadata, dict):
            return {}
        dispatch = metadata.get("dispatch")
        return dispatch if isinstance(dispatch, dict) else {}

    def _dispatch_strategy(self) -> str:
        """How the caller asked for this work to be merged. Defaults to collect.

        UNKNOWN VALUES FALL BACK TO `collect`, deliberately. swarm-api validates
        the field, so an unrecognised one here means a newer control plane and
        an older worker -- and in that disagreement the safe reading is the one
        that pushes nothing. A worker that guessed "probably direct-pr" would
        open pull requests a caller never asked for.
        """
        value = self._dispatch_block().get("strategy")
        if not isinstance(value, str):
            return "collect"
        value = value.strip().lower()
        return value if value in ("collect", "direct-pr", "integrate") else "collect"

    def _dispatch_role(self) -> str:
        """This task's part in an `integrate` workflow.

        Only `integrate` assigns roles; every other strategy leaves the field
        absent and every step behaves identically. An UNRECOGNISED role reads as
        `contributor` rather than `integrator`, matching the same
        newer-control-plane-older-worker reasoning as the strategy above: a
        contributor pushes a branch and opens nothing, which is the outcome
        that cannot surprise a caller.
        """
        value = self._dispatch_block().get("role")
        if not isinstance(value, str):
            return ""
        value = value.strip().lower()
        return value if value in ("contributor", "integrator") else "contributor"

    def _dispatch_integrates(self) -> list[str]:
        """The upstream task ids this integrator must bring together.

        Order is preserved: swarm-api builds it from the workflow's topological
        prefix, so it is the order the patches have to be applied in.
        """
        raw = self._dispatch_block().get("integrates")
        if not isinstance(raw, list):
            return []
        return [t.strip() for t in raw if isinstance(t, str) and t.strip()]

    def _dispatch_builds_on(self) -> str:
        """The upstream task id whose pushed branch this step clones (archived issue 264), or "".

        A value that is not a task id is REFUSED rather than ignored: ignored,
        the step would clone the default branch and its agent would fix code
        that does not contain the change it was asked to fix.
        """
        raw = self._dispatch_block().get("builds_on")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return ""
        if not isinstance(raw, str) or not _TASK_ID_RE.match(raw.strip()):
            raise WorkerError(
                f"this step's dispatch block names builds_on {str(raw)[:80]!r}, "
                "which is not a task id; the branch to start from cannot be derived "
                "from it, so nothing was cloned"
            )
        return raw.strip()

    def _pr_role(self) -> str:
        """This step's part in a `single-pr` pull request, or "" under any other strategy.

        `author`, `reader`, `amender` or `none` (a worker action), as
        swarm-api's `validation.PR_ROLES` writes them. AN UNRECOGNISED ROLE
        READS AS `reader`, for the reason an unknown strategy reads as
        `collect`: a reader pushes nothing, the one outcome that cannot
        surprise a caller.
        """
        block = self._dispatch_block()
        raw = block.get("strategy")
        if not (isinstance(raw, str) and raw.strip().lower() == "single-pr"):
            return ""
        role = block.get("pr_role")
        role = role.strip().lower() if isinstance(role, str) else ""
        return role if role in ("author", "reader", "amender", "none") else "reader"

    def _pr_author_branch(self) -> str:
        """`swarm/<pr_author>`: the branch a reader clones and an amender pushes to.

        Derived from the author's TASK id in the signed dispatch block with
        this worker's own prefix, never read as a name, exactly as the
        integrator's contributor branches are. A value that is not a task id
        is refused: guessed, the step would read or amend some other branch.
        """
        raw = self._dispatch_block().get("pr_author")
        if not isinstance(raw, str) or not _TASK_ID_RE.match(raw.strip()):
            raise WorkerError(
                f"this single-pr step's dispatch block names pr_author {str(raw)[:80]!r}, "
                "which is not a task id; the pull request's branch cannot be derived "
                "from it, so nothing was cloned or pushed"
            )
        return f"{self.cfg.git_branch_prefix}{raw.strip()}"

    def _step_servers(self) -> tuple[str, ...]:
        """The platform MCP servers this step's agent is handed (`stepservers`)."""
        task = self._task or {}
        try:
            onto_pull_request = self._publish_to_pr() is not None
        except WorkerError:
            onto_pull_request = False
        return stepservers_mod.step_servers(
            step_id=task.get("step_id"), metadata=task.get("metadata"),
            dispatch=self._dispatch_block(), pr_role=self._pr_role(),
            onto_pull_request=onto_pull_request)

    def _publish_to_pr(self) -> "publishpr_mod.Target | None":
        """The existing pull request this step publishes onto (archived issue 2183), or None.

        From the signed dispatch block only (`publishpr.target`), which raises
        WorkerError for a value swarm-api could not have written.
        """
        return publishpr_mod.target((self._task or {}).get("metadata"))

    def _read_pull(self, access: RepoAccess, token: str, number: int) -> PullHead:
        """Pull request `number` of the task's repository, read now, an outage
        tried again within the forge retry policy."""
        return forge_mod.retry_transient(
            lambda: read_pull_request(ref=access.ref, token=token, number=number),
            policy=self._forge_retry(),
            what="the pull request",
        )

    def _read_pull_at_start(self, url: str, target: "publishpr_mod.Target") -> str:
        """Read and check the pull request a `publish_to_pr` step starts from,
        before anything is cloned; return its head sha (archived issue 2183).

        Every refusal is a WorkerError, so the attempt fails with the reason
        and no agent ever starts. An outage is `_CloneUnreachable`: the
        ATTEMPT ends retryably, as a clone the forge did not answer does.
        """
        number = target.number
        refusal = self._git_token_refusal()
        if refusal:
            raise WorkerError(
                f"publish_to_pr: pull request #{number} is read and pushed with the "
                f"tenant git token, which this worker may not hold: {refusal}; nothing "
                "was cloned and no agent ran"
            )
        token = self._git_token(write=True)
        if not token:
            raise WorkerError(
                f"publish_to_pr: pull request #{number} is read and pushed with the "
                "tenant git token, and this task has none; nothing was cloned and no "
                "agent ran"
            )
        try:
            token, access = self._probe_reading_again(url, token, "the publish_to_pr probe")
            if access is None:
                raise WorkerError(
                    "publish_to_pr: this repository is not on a forge this worker can "
                    "publish to; nothing was cloned and no agent ran"
                )
            pull = self._read_pull(access, token or "", number)
        except forge_mod.ForgeUnavailable as exc:
            raise _CloneUnreachable(
                str(self._scrub(f"pull request #{number}: {forge_mod.loggable(str(exc))}")),
                int(exc.tries),
            ) from exc
        except ForgeError as exc:
            raise WorkerError(
                f"publish_to_pr: could not read pull request #{number}: "
                f"{self._scrub(str(exc)[:400])}; nothing was cloned and no agent ran"
            ) from exc
        why = publishpr_mod.refusal(
            pull, target=target, repository=access.ref.full_name,
            branch_prefix=self.cfg.git_branch_prefix,
        )
        if why:
            raise WorkerError(
                f"publish_to_pr refused before the agent started: {self._scrub(why)}; "
                "nothing was cloned and no agent ran"
            )
        self.log.info(
            "publish_to_pr: starting from the pull request's head",
            number=number, branch=pull.head_ref, head=pull.head_sha, base=pull.base_ref,
        )
        self._pr_base_ref = pull.base_ref
        return pull.head_sha

    def _check_pull_before_push(
        self, publish_repo: Path, target: "publishpr_mod.Target", *,
        access: RepoAccess, token: str, url: str, branch: str,
    ) -> tuple[PullHead, bool, str]:
        """Read the pull request again and refuse, as a GitError, everything
        that would make the push onto it anything but a fast-forward of the
        head this step started from (archived issue 2183). Nothing has been pushed yet.

        Returns the pull request and whether its head moved ONLY BY THIS
        TASK'S OWN earlier generation (archived issue 2693, `_own_moved_pull_head`): then
        the lane's work stands on the head it started from, not on the
        moved one, and `_publish_git` publishes nothing or replays onto it. And
        the token that read it, a 401 read again once (issue 284), for the push.
        """
        ws = self.ws
        assert ws is not None
        unread = f"publish_to_pr: could not read pull request #{target.number} before the push"
        try:
            try:
                pull = self._read_pull(access, token, target.number)
            except forge_mod.ForgeCredentialRejected:
                token = self._write_token_read_again(
                    branch, "pre_push_read_401", f"{unread}; nothing was pushed")
                try:
                    pull = self._read_pull(access, token, target.number)
                except forge_mod.ForgeCredentialRejected as exc:
                    self.log.error("the forge refused the re-read credential at the pull "
                                   "request read as well", branch=branch)
                    raise GitError(
                        f"{unread} (401 twice); nothing was pushed: {FORGE_REFUSED_TWICE}; "
                        f"the read said: {str(exc)[:120]}"
                    ) from None
        except forge_mod.ForgeUnavailable as exc:
            raise _PullUnreachable(exc) from exc
        except ForgeError as exc:
            raise GitError(f"{unread}: {exc}; nothing was pushed") from exc
        why = publishpr_mod.refusal(
            pull, target=target, repository=access.ref.full_name,
            branch_prefix=self.cfg.git_branch_prefix,
        )
        if why:
            raise GitError(f"publish_to_pr refused before the push: {why}; nothing was pushed")
        moved = publishpr_mod.head_moved(
            started=self._pr_start_head, now=pull.head_sha, number=target.number
        )
        own_moved = False
        if moved:
            if not self._own_moved_pull_head(publish_repo, pull, url=url, token=token):
                raise GitError(moved)
            own_moved = True
        # The lane's HEAD must DESCEND from that head, or the push is not a
        # fast-forward: an upstream step that started from an older head of
        # the pull request (it moved between that step and this one) left a
        # branch this one stands on. Moved by this task's own earlier
        # generation, it must descend from the head it started from.
        stands_on = self._pr_start_head if own_moved else pull.head_sha
        code, _ = _git_text(
            ["git", *_NO_HOOKS, "merge-base", "--is-ancestor", str(stands_on), "HEAD"],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-to-pr-ancestor",
            timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
        )
        if code != 0:
            raise GitError(
                f"{publishpr_mod.HEAD_MOVED}: the lane's work does not stand on pull "
                f"request #{target.number}'s head {str(stands_on)[:12]}, so pushing it "
                "would not be a fast-forward; the head moved after the lane's first "
                "step started. Nothing was pushed and nothing was forced"
            )
        return pull, own_moved, token

    def _own_moved_pull_head(
        self, publish_repo: Path, pull: PullHead, *, url: str, token: str
    ) -> bool:
        """Whether the pull request's head moved off `_pr_start_head` only by
        commits this task's own worker wrote (archived issue 2693).

        MEASURED: task_ffc37f64's generation 1 was fenced `dead_worker` and
        still pushed onto archived pull request 2613 (a273bb75 -> 7eb64482, before the publish
        read the fence: `_own_before_forge_write`); generation 5, holding the
        same work as 831e2bef on a273bb75, then refused its own publish
        `head_moved` and the task failed with its work on the pull request.
        A head moved only by this task's worker -- its identities, the
        task's own "Made by the worker for task <id>." line on every commit
        (`gitops.own_moved_head`, the predicate `own_branch_tip` uses) --
        was moved by nobody else, so it is not refused. Anyone else's commit
        anywhere in the span still is, exactly as before. The fetch carries
        the token, so it runs only in the worker-owned publish repository.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        author_name, author_email = self._publish_identity()
        own, why = own_moved_head(
            repo=publish_repo,
            url=url,
            branch=pull.head_ref,
            token=token,
            task_id=cfg.task_id,
            started=self._pr_start_head,
            now=pull.head_sha,
            author_name=author_name,
            author_email=author_email,
            also_own=((cfg.git_author_name, cfg.git_author_email),),
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_clone_timeout_seconds,
            logger=self.log,
            branch_prefix=cfg.git_branch_prefix,
        )
        if not own:
            self.log.info("the pull request's head moved by more than this task's own "
                          "commits", number=pull.number, reason=str(self._scrub(why)))
        return own

    def _dispatch_carrier(self) -> str:
        """Where a step's work is kept for the next step. Defaults to checkpoints.

        THE VOCABULARY IS swarm-api's, and it is written out here rather than
        imported because the worker must not pull the control plane into the
        image every agent runs in. That copy had already drifted: this accepted
        `("patches", "branches")` and fell back to `"patches"`, a word
        `DISPATCH_CARRIERS` has never contained and the validator REFUSES at
        submission. The real default block says `carrier: "checkpoints"`, which
        this read as unrecognised -- so the two ends agreed on one word out of
        three, and never on the one almost every dispatch carries.

        It had broken nothing only because no decision in this worker branches
        on the carrier yet. The first one that did would have read "patches"
        for a dispatch that said "checkpoints".

        A dispatch-contract parity test imports swarm-api's
        own constants and asserts this set matches them, so the next divergence
        fails there instead of in a deployed worker.
        """
        value = self._dispatch_block().get("carrier")
        if not isinstance(value, str):
            return "checkpoints"
        value = value.strip().lower()
        return value if value in ("checkpoints", "branches") else "checkpoints"

    def _credential_refusal(self) -> dict[str, Any] | None:
        """The runner's `credential.json`, if it wrote one.

        Deliberately separate from the quota signal. The two arrive from the
        same provider over the same connection and mean opposite things: one
        says wait, the other says this will never work again.
        """
        ws = self.ws
        if ws is None or not ws.credential_path.exists():
            return None
        data = _read_json(ws.credential_path)
        return data if isinstance(data, dict) else {}

    # -- the publish's workflow route (AU-3, agent-autonomy.md §3.3) -----------

    def _workflow_broker(self) -> Any:
        """The attempt's git tools relay (`gittools.GitTools`), or None.

        The broker the publish hands a workflow-touching branch to: the same
        signed `git_push_branch` call the agent's own mid-run push makes, with
        the attempt key and the fence (invariant 5). None when this attempt
        is not offered the git tools (`ChildPath.git_offered`)."""
        return getattr(self.children, "_git", None) if self.children.git_offered else None

    def _workflow_negatives(self, floor: str | None) -> list[str]:
        """The commits origin has that the publish's range starts from: the
        clone base, the default branch's commit the lane merged, and origin's
        tip of the publish branch when HEAD descends from it (archived issue 2506).

        The tip is `_publish_remote_tip`, read from the forge and kept only
        after `merge-base --is-ancestor` showed HEAD stands on it, so a tip
        that moved elsewhere, or names an unrelated line, hides none of the
        round's own commits. Unread or absent, the range is as before."""
        out: list[str] = []
        for sha in (self._publish_base, floor, self._publish_remote_tip):
            if sha and _FULL_SHA_RE.fullmatch(sha) and sha not in out:
                out.append(sha)
        return out

    def _publish_continues_a_branch(self) -> bool:
        """True when this publish may find its branch already on origin, so
        `_read_publish_remote_tip` asks the forge (archived issue 2506): a round that
        continues another task's branch (`dispatch.continues`, the ci-fix
        round #2506 measured), or a later attempt of this task, whose earlier
        one may have pushed. A fresh lane's first attempt publishes a branch
        origin cannot hold yet, so it makes no token-bearing fetch for it.

        `attempt_count` is counted with the refunded child-await, hand-off and
        quota-park resumes, as `_recorded_checkpoint` counts it.
        """
        task = self._task or {}
        try:
            if continuation_mod.continued_task(task.get("metadata")):
                return True
        except WorkerError:
            return False
        count = task.get("attempt_count")
        if isinstance(count, bool) or not isinstance(count, int):
            return False
        resumes = _refunded_resumes(task)
        return count + resumes > 1

    def _read_publish_remote_tip(
        self, publish_repo: Path, *, url: str, token: str, branch: str, tool_tip: str | None
    ) -> str | None:
        """Origin's current tip of `branch`, fetched from the FORGE into the
        publish repository (`fetch_branch_tip`, the token-bearing fetch, as
        `_default_branch_tip` reads the default branch), or None when the
        branch is absent or could not be read (archived issue 2506). Never read from the
        agent's clone, whose refs the agent can point anywhere.

        `tool_tip`, the tip this attempt's own `git_push_branch` left
        (`_adopt_tool_pushed_tip`), is returned as it is, not fetched again.
        """
        if tool_tip is not None:
            return tool_tip
        ws = self.ws
        assert ws is not None
        try:
            tip = fetch_branch_tip(
                repo=publish_repo,
                url=url,
                branch=branch,
                token=token,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds,
                logger=self.log,
                branch_prefix=self.cfg.git_branch_prefix,
            )
        except GitError as exc:
            self.log.warning("could not read the publish branch from the forge",
                             branch=branch, error=str(self._scrub(str(exc)[:300])))
            return None
        return tip if isinstance(tip, str) and _FULL_SHA_RE.fullmatch(tip) else None

    def _publish_rev(self, publish_repo: Path, rev: str) -> str | None:
        """`rev` resolved in the publish repository, or None when git cannot."""
        ws = self.ws
        assert ws is not None
        code, text = _git_text(
            ["git", *_NO_HOOKS, "rev-parse", "--verify", "--quiet", rev],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-rev", timeout_seconds=self.cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        return (text.strip() or None) if code == 0 else None

    def _auto_commit_undid(self, publish_repo: Path, *, agent_head: str | None) -> str | None:
        """Why the worker's auto-commit undid the agent's commits, or None (archived issue 2536).

        Undid means: HEAD's tree after the auto-commit is the publish base's
        tree, and `agent_head`'s -- HEAD before it, the agent's last commit --
        is not. The reason names the paths the agent's commits changed, which
        the branch would have published as no change at all. A base that is
        unknown, the empty clone's, or not readable is not judged.
        """
        base = self._publish_base
        if not agent_head or not base or base == EMPTY_CLONE_BASE:
            return None
        base_tree = self._publish_rev(publish_repo, f"{base}^{{tree}}")
        if base_tree is None:
            return None
        if self._publish_rev(publish_repo, "HEAD^{tree}") != base_tree:
            return None
        if self._publish_rev(publish_repo, f"{agent_head}^{{tree}}") == base_tree:
            return None
        ws = self.ws
        assert ws is not None
        code, text = _git_text(
            ["git", *_NO_HOOKS, "diff", "--no-renames", "--name-only", "-z", base, agent_head],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-undone-paths", timeout_seconds=self.cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        paths = sorted({p for p in text.split("\0") if p}) if code == 0 else []
        shown = ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
        return (
            "the worker's commit of the agent's uncommitted work would put the branch "
            f"back at its base {base[:12]}, undoing the agent's own commits"
            + (f" to {shown}" if shown else "")
            + "; nothing was pushed"
        )

    def _publish_is_ancestor(self, publish_repo: Path, ancestor: str, descendant: str) -> bool:
        """True when `ancestor` is `descendant` or one of its ancestors in the
        publish repository; False when it is not or git could not tell."""
        ws = self.ws
        assert ws is not None
        code, _ = _git_text(
            ["git", *_NO_HOOKS, "merge-base", "--is-ancestor", ancestor, descendant],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-remote-tip", timeout_seconds=self.cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        return code == 0

    def _workflow_paths(self, publish_repo: Path, *, floor: str | None) -> list[str]:
        """The workflow and action files the branch's new commits touch, sorted.

        The command swarm-api reads the pushed bundle with
        (`swarm_api.gittools._workflow_paths`): `--name-only --no-renames
        --diff-merges=combined` over exactly the new commits, so a rename away
        from a workflow path counts, and the two never disagree about which
        route a branch takes.

        COMBINED, NOT `-m` (archived issue 2452). `-m` diffs a merge against EACH parent, so
        a lane's merge of main named every workflow file main changed since
        the branch was cut: task_1297d8f4 and task_f2b72856 (2026-10-26),
        after PR 2446 changed application.yml and release.yml, were sent to
        the broker with no workflow change of their own and ended FAILED.
        A combined listing names a merge's path only where the result differs
        from EVERY parent -- a conflict resolution, an evil merge -- and lists
        a non-merge commit exactly as before. Its one blind spot: a merge that
        keeps the branch's version of a file main also changed, where that
        version is already on origin (below the negatives), is not listed and
        goes out on the worker's own push. Should GitHub refuse that push for
        the `workflow` scope, the publish fails as any refused push does
        (`PUBLISH_FAILED_FIELD`), the work kept (docs/runner-git-tools.md).

        A listing git could not make is no workflow paths, logged: the
        worker's own push then runs, as before AU-3, and GitHub refuses a
        workflow file from a token without the `workflow` scope, which fails
        the publish (`PUBLISH_FAILED_FIELD`) the way any refused push does.
        Either way the step does not end SUCCEEDED with the file unpushed.
        """
        ws = self.ws
        assert ws is not None
        negatives = self._workflow_negatives(floor)
        code, text = _git_text(
            ["git", *_NO_HOOKS, "log", "--format=", "--name-only", "--no-renames",
             "--diff-merges=combined", "HEAD", *(["--not", *negatives] if negatives else []),
             "--", *WORKFLOW_PATHSPECS],
            repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
            slug="publish-workflow-paths",
            timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
        )
        if code != 0:
            self.log.warning("could not list the workflow files the branch touches; "
                             "publishing with the worker's own push")
            return []
        prefixes = tuple(f"{spec}/" for spec in WORKFLOW_PATHSPECS)
        return sorted({line.strip() for line in text.splitlines()
                       if line.strip().startswith(prefixes)})

    def _push_through_broker(
        self, publish_repo: Path, *, branch: str, floor: str | None
    ) -> tuple[str | None, dict[str, str] | None]:
        """Push the publish repository's HEAD to `branch` through swarm-api's
        `git_push_branch`. Returns (the pushed sha, None) or (None, the refusal
        as `{"code", "message"}`).

        The bundle is built here, in the worker-owned publish repository, from
        exactly the commits the token push would have sent -- HEAD not
        reachable from the clone base, the merged default-branch commit or
        origin's tip of the branch (`_workflow_negatives`, archived issue 2506), all sent
        as `haves` -- so swarm-api reads the same range and decides the credential: a
        GitHub App token it mints for that one repository when the
        registration's `workflow_files` switch is on (archived issue 2029),
        `workflow_scope_missing` otherwise. A fence (409) is a
        refusal too: nothing is pushed, and this worker touches no lease.

        A refusal marked `retryable` -- `GitTools.call` turns every retryable
        ChildApiError into `api_unavailable` -- is asked again in-process:
        one swarm-api blip failed task_b7b69bce7da247e0afaf's finished
        42-minute build with publish_failed (archived issue 2138). The bundle is built and
        uploaded once and every try carries the same request_id and fields,
        so the broker sees one logical call; a replay after a push whose
        answer was lost is safe because swarm-api's `_push` leases on
        `recorded_tip`, which that push already recorded. The tries are
        `_forge_retry()`'s bound -- `cfg.forge_read_attempts`, one budget of
        the smaller of `max_in_worker_retry_delay_seconds` and the step's
        remaining deadline -- so this is never the long in-worker wait
        invariant 4 forbids; past it the last refusal is returned as before.
        A fence, a refusal not marked retryable, and any other failure are
        tried once (invariant 5: a fence is never asked again).
        """
        from . import gittools as gittools_mod  # lazy: gittools imports this module

        relay = self._workflow_broker()
        if relay is None:
            return None, {
                "code": "git_tools_unavailable",
                "message": (
                    "this attempt is not offered the git tools, and a workflow file is "
                    "pushed only by swarm-api's git_push_branch, never with the tenant token"
                ),
            }
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        repo = Path(publish_repo)
        negatives = self._workflow_negatives(floor)
        local = "refs/heads/swarm-workflow-publish"
        bundle = repo / ".git" / "swarm-workflow-push.bundle"
        timeout = cfg.git_harvest_timeout_seconds

        def git(argv: list[str], slug: str) -> str:
            code, text = _git_text(
                ["git", *_NO_HOOKS, *argv], repo=repo, private_dir=ws.private,
                logs_dir=ws.logs, slug=slug, timeout_seconds=timeout, logger=self.log,
            )
            if code != 0:
                raise GitError(f"could not prepare the broker's bundle ({slug}); nothing "
                               "was pushed")
            return text.strip()

        head = git(["rev-parse", "--verify", "HEAD^{commit}"], "publish-broker-head")
        commits = git(["rev-list", "--count", "HEAD", *(f"^{sha}" for sha in negatives)],
                      "publish-broker-count")
        git(["update-ref", local, head], "publish-broker-ref")
        try:
            git(["bundle", "create", str(bundle), local, *(f"^{sha}" for sha in negatives)],
                "publish-broker-bundle")
            raw = bundle.read_bytes()
        finally:
            bundle.unlink(missing_ok=True)
        if len(raw) > gittools_mod.MAX_BUNDLE_BYTES:
            return None, {"code": "too_large", "message": (
                f"the bundle is {len(raw)} bytes; at most {gittools_mod.MAX_BUNDLE_BYTES}")}
        request_id = f"publish-{os.urandom(6).hex()}"
        fields: dict[str, Any] = {
            "branch": branch,
            "old_sha": None,
            "new_sha": head,
            "haves": negatives,
            "commits": int(commits or 0),
            "bundle_bytes": len(raw),
            "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        }
        inline = int(getattr(relay.cfg, "inline_bundle_bytes", gittools_mod.INLINE_BUNDLE_BYTES))
        try:
            if len(raw) <= inline:
                fields["bundle"] = base64.b64encode(raw).decode("ascii")
            else:
                key = gittools_mod.BUNDLE_OBJECT.format(
                    tenant=cfg.tenant_id, attempt=cfg.attempt_id, call=request_id)
                self.store.upload_bytes(key, raw, "application/x-git-bundle")
                fields["bundle_object"] = key
            policy = self._forge_retry()
            started = policy.clock()
            attempt = 1
            while True:
                try:
                    payload = relay.call("git_push_branch", fields, request_id=request_id)
                    break
                except gittools_mod.GitToolRefused as refusal:
                    if not refusal.retryable or attempt >= policy.attempts:
                        raise
                    wait = policy.base_delay_seconds * (2 ** (attempt - 1))
                    if (policy.clock() - started) + wait > policy.budget_seconds:
                        raise
                    self.log.warning(
                        "the broker could not answer the workflow publish; asking again",
                        code=refusal.code, attempt=attempt, attempts=policy.attempts,
                        wait_seconds=wait)
                    policy.sleep(wait)
                    attempt += 1
        except gittools_mod.GitToolFenced:
            self.log.warning("the broker fenced the workflow publish; nothing was pushed")
            return None, {"code": "fenced", "message": (
                "this attempt no longer holds the task (409); nothing was pushed")}
        except gittools_mod.GitToolRefused as refusal:
            self.log.warning("the broker refused the workflow publish", code=refusal.code)
            return None, {"code": refusal.code, "message": str(self._scrub(refusal.message))}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("the broker's workflow publish failed", error=type(exc).__name__)
            return None, {"code": "api_unavailable", "message": (
                f"the workflow publish did not reach swarm-api ({type(exc).__name__})")}
        new_sha = payload.get("new_sha") if isinstance(payload, dict) else None
        if not isinstance(new_sha, str) or not _FULL_SHA_RE.fullmatch(new_sha):
            return None, {"code": "api_unavailable",
                          "message": "swarm-api answered the workflow publish without a sha"}
        self.log.info("the broker pushed the workflow-touching branch", branch=branch,
                      new_sha=new_sha, request_id=request_id)
        return new_sha, None

    def _workflow_push_refused(
        self,
        out: dict[str, Any],
        publish_repo: Path,
        refused: dict[str, str],
        *,
        floor: str | None,
        auto_committed: bool,
    ) -> dict[str, Any]:
        """The publish's result for a workflow push the broker did not make.

        Nothing was pushed. `PUBLISH_FAILED_FIELD` is set, so `_publish_failed`
        ends a step whose branch is its deliverable FAILED, never SUCCEEDED,
        and the workflow and action lines are left as `workflow-lines.patch`
        in the attempt's artifacts -- written by the worker, from the publish
        repository, against the commit the branch starts from, and uploaded
        here because the artifacts left before this publish ran. Its
        `workflow_patch` entry is what `_publish_checked` adds to the
        attempt's manifest (archived issue 2023). The broker's message is quoted whole up
        to `WORKFLOW_REFUSAL_MAX_CHARS`, and cut at a word past it.
        """
        paths = ", ".join(out.get("workflow_paths") or [])
        out["published"] = False
        out["auto_committed"] = auto_committed
        out[PUBLISH_FAILED_FIELD] = True
        out["workflow_refused"] = dict(refused)
        patch = self._write_workflow_patch(publish_repo, floor=floor)
        if patch is not None:
            out["workflow_patch"] = patch
            where = f"the workflow change is in {WORKFLOW_PATCH_FILE} ({patch['uri']})"
            if patch.get("truncated"):
                where += f", cut at {WORKFLOW_PATCH_MAX_BYTES} bytes"
        else:
            where = f"{WORKFLOW_PATCH_FILE} could not be written; the work is in the patch"
        out["publish_reason"] = str(self._scrub(
            f"refusing to publish: the branch touches {paths}, which only swarm-api's "
            f"git_push_branch may push (agent-autonomy.md §3.3), and it answered "
            f"{refused['code']}: {_cut_at_word(refused['message'], WORKFLOW_REFUSAL_MAX_CHARS)}"
            f". Nothing was pushed; {where}"
        ))
        self.log.error("not publishing: the broker did not push the workflow change",
                       code=refused["code"])
        return out

    def _write_workflow_patch(
        self, publish_repo: Path, *, floor: str | None
    ) -> dict[str, Any] | None:
        """Write and upload `workflow-lines.patch`: `git diff --binary` from the
        branch's start to HEAD over the workflow and action paths only.

        Never follows a link: whatever stands at the name in the artifacts
        folder (the agent may have written its own) is unlinked and the file
        is created `O_EXCL | O_NOFOLLOW`, then uploaded through `_upload_copy`.
        Returns `{"name", "uri", "bytes", "truncated"}`, or None."""
        ws = self.ws
        assert ws is not None
        repo = Path(publish_repo)
        timeout = self.cfg.git_harvest_timeout_seconds
        try:
            # Not `_workflow_negatives`: a diff has one start. Origin's tip of
            # the branch, when HEAD stood on it past the clone base, already
            # IS `_publish_base` (archived issue 2506), so the patch carries only the lines
            # origin lacks and applies onto the branch as origin holds it.
            start = floor if floor and _FULL_SHA_RE.fullmatch(floor) else self._publish_base
            if not start or not _FULL_SHA_RE.fullmatch(start):
                code, start = _git_text(
                    ["git", *_NO_HOOKS, "hash-object", "-t", "tree", "/dev/null"],
                    repo=repo, private_dir=ws.private, logs_dir=ws.logs,
                    slug="publish-workflow-empty-tree", timeout_seconds=timeout,
                    logger=self.log,
                )
                start = start.strip()
                if code != 0 or not start:
                    raise GitError("could not name the empty tree")
            code, text, truncated = _git_text_full(
                ["git", *_NO_HOOKS, "diff", "--binary", "--no-color", "--no-ext-diff",
                 start, "HEAD", "--", *WORKFLOW_PATHSPECS],
                repo=repo, private_dir=ws.private, logs_dir=ws.logs,
                slug="publish-workflow-patch", timeout_seconds=timeout, logger=self.log,
                max_bytes=WORKFLOW_PATCH_MAX_BYTES,
            )
            if code != 0:
                raise GitError("git diff failed")
            target = ws.artifacts / WORKFLOW_PATCH_FILE
            if target.is_symlink() or target.exists():
                target.unlink()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except (GitError, OSError) as exc:
            self.log.warning(f"could not write {WORKFLOW_PATCH_FILE}", error=str(exc)[:200])
            return None
        key = f"{self.cfg.artifact_prefix}/{WORKFLOW_PATCH_FILE}"
        sent = self._upload_copy(
            ws.artifacts, WORKFLOW_PATCH_FILE, key=key,
            label=f"artifacts/{WORKFLOW_PATCH_FILE}", limit=self.cfg.max_artifact_bytes,
            content_type="text/x-diff; charset=utf-8",
        )
        if sent.bytes is None:
            self.log.warning(f"{WORKFLOW_PATCH_FILE} was written but not uploaded",
                             skipped=sent.skipped)
            return None
        return {"name": WORKFLOW_PATCH_FILE, "uri": self.store.uri(key), "bytes": sent.bytes,
                "truncated": bool(truncated)}

    def _territory_warning(
        self, publish_repo: Path | None, *, head: str, out: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """`(paths, labels)` for a pull request whose diff leaves the step's
        declared territory -- the paths outside it, sorted -- or `([], [])`
        (TERRITORY-WARN, part of archived issue 1468).

        The territory is `inputs.territory` in the task document as stored:
        submitted by the caller, refused at submit when malformed, and covered
        by the spec signature -- never a file in the workspace. A step that
        declares none returns `([], [])` before git is asked anything, so its
        publish is exactly what it was.

        Records `out["out_of_territory"]` (the paths) when there are any, and
        `out["territory_unchecked"]` (why) when a declared territory could not
        be compared: a warning that is owed and cannot be made is said where
        the run result is read, never silently dropped.
        """
        stored = (self._task or {}).get("input")
        try:
            territory = territory_mod.declared(stored)
        except territory_mod.TerritoryError as exc:
            out["territory_unchecked"] = f"the declared territory is malformed: {exc}"[:400]
            self.log.warning("the declared territory could not be read", error=str(exc)[:300])
            return [], []
        if territory is None:
            return [], []
        ws = self.ws
        paths = None
        if publish_repo is not None and ws is not None:
            try:
                paths = published_paths(
                    repo=publish_repo,
                    base=self._publish_base,
                    head=head,
                    private_dir=ws.private,
                    logs_dir=ws.logs,
                    timeout_seconds=self.cfg.git_harvest_timeout_seconds,
                    logger=self.log,
                )
            except GitError as exc:
                self.log.warning("the published paths could not be read", error=str(exc)[:300])
        if paths is None:
            out["territory_unchecked"] = (
                "git could not list the paths this branch changes, so they were not "
                "compared with the declared territory"
            )
            self.log.warning("the declared territory was not compared")
            return [], []
        outside = territory_mod.outside(paths, territory)
        if not outside:
            return [], []
        out["out_of_territory"] = {"paths": outside[: territory_mod.MAX_LISTED],
                                   "count": len(outside)}
        self.log.warning("the diff leaves the declared territory", paths=len(outside))
        return outside, [territory_mod.OUT_OF_TERRITORY_LABEL]

    def _post_territory_comment(
        self, *, access: RepoAccess, token: str, number: int, outside: list[str],
        out: dict[str, Any],
    ) -> None:
        """ONE comment on the adopted pull request `number` saying which of this
        push's paths left the territory (CIFIX-TERRITORY-2, part of archived issue 1499).

        CLD, 2026-10-23: the label alone, plus a section appended at the bottom
        of a long body, is easy to miss. The comment starts with the header in
        its continuation form and ends with a hidden marker naming this task
        and attempt, so a publish of the same attempt again finds it and posts
        nothing. Still a warning, never a refusal (archived issue 1468): a comment the forge
        refuses is recorded in `out["out_of_territory"]` and logged, as a
        refused label is, and the publish stands.

        Reached only from `_publish_git`, after the push, and the fence is
        read again just before the POST (`_own_before_forge_write`, archived issue 2693):
        an attempt fenced after its push posts nothing (invariant 5).
        """
        record = out.setdefault("out_of_territory", {})
        marker = (
            f"<!-- swarm:out-of-territory task={self.cfg.task_id} "
            f"attempt={self.cfg.attempt_id} -->"
        )
        ref = access.ref
        try:
            # A read is retried on an outage; the POST is not, since one whose
            # answer was lost may have made the comment (the marker finds it).
            existing = forge_mod.retry_transient(
                lambda: forge_mod.list_issue_comments(ref=ref, token=token, number=number),
                policy=self._forge_retry(),
                what="the pull request's comments",
            )
        except ForgeError as exc:
            # Unread is not "none there": posting anyway risks a second copy of
            # a warning, which is better than the warning not being made.
            existing = []
            self.log.warning("the pull request's comments could not be read",
                             error=str(self._scrub(str(exc)[:300])))
        if any(marker in body for body in existing):
            record["comment"] = "already posted"
            return
        body = _neutralise_mentions(
            territory_mod.warning_header(outside, continuation=True)
        ) + f"\n\n{marker}"
        self._own_before_forge_write("pull request comment")
        try:
            record["comment"] = forge_mod.create_issue_comment(
                ref=ref, token=token, number=number, body=body
            )
        except ForgeError as exc:
            error = str(self._scrub(str(exc)[:300]))
            record["comment_error"] = error
            self.log.warning("the out-of-territory comment was not posted", error=error)

    def _pull_request_body(
        self,
        *,
        branch: str,
        auto_committed: bool,
        merge: "MergeOutcome | None" = None,
        folded: int = 0,
        kept: int = 0,
    ) -> str:
        """The body a reviewer reads. Provenance first, prompt second.

        The prompt is truncated hard and scrubbed. It is UNTRUSTED text that
        ends up rendered as markdown on a public-facing page, so the one thing
        it must not do is arrive at full length with whatever the submitter
        decided to put in it.
        """
        cfg = self.cfg
        lines = [
            "Opened by SwarmCloud. This branch is written only by this task.",
            "",
            f"- task: `{cfg.task_id}`",
            f"- attempt: `{cfg.attempt_id}`",
            f"- tenant: `{cfg.tenant_id}`",
            f"- runner profile: `{cfg.profile.name}`",
            f"- branch: `{branch}`",
        ]
        if self._clone_base:
            lines.append(f"- base commit: `{self._clone_base}`")
        if self._last_checkpoint is not None:
            lines.append(f"- checkpoint: `{self._last_checkpoint.checkpoint_id}`")
        if folded:
            # Said on the page, because a reviewer who knows the agent made
            # several commits would otherwise look for them. They are in the
            # run result; here they are one commit the worker wrote.
            lines += [
                "",
                f"The agent's {folded} commit(s) and any changes it left "
                "uncommitted are one commit on this branch, made by the worker. "
                "The worker writes every commit it pushes.",
            ]
        elif kept:
            # Said on the page, because the commits a reviewer sees are the
            # agent's in content and order and the worker's in authorship.
            lines += [
                "",
                f"The agent's {kept} commit(s) are on this branch in order, each "
                "rewritten by the worker: the same tree, the agent's message with "
                "its trailers and footers taken out. The worker writes every "
                "commit it pushes.",
            ]
            if auto_committed:
                lines.append(
                    "The last commit is the worker's own: the agent left changes "
                    "uncommitted and they would otherwise not have reached this "
                    "branch at all."
                )
        elif auto_committed:
            lines += [
                "",
                "One commit on this branch was made by the worker, not the agent: "
                "the agent left changes uncommitted and they would otherwise not "
                "have reached this branch at all.",
            ]

        # WHAT THIS PULL REQUEST DOES NOT CONTAIN, stated on the pull request
        # rather than only in the run result. An integrator takes what merges
        # and names what it could not take; a reviewer who cannot see that
        # would read a partial integration as a complete one, which is the
        # single most misleading thing this page could do.
        if merge is not None:
            lines += ["", f"Integrates {len(merge.merged)} contributor branch(es):"]
            lines += [f"- merged: `{b}`" for b in merge.merged] or ["- none"]
            if self._read_only_contributors:
                # A neutral line, never under a NOT included heading: these
                # steps finished having published nothing by design (archived issue 760),
                # and a reviewer reading "missing" would re-run them for a
                # branch they were never to push.
                read_only = ", ".join(
                    f"`{cfg.git_branch_prefix}{tid}`" for tid in self._read_only_contributors
                )
                lines += ["", f"read-only, nothing to merge: {read_only}"]
            if merge.conflicted:
                lines += [
                    "",
                    "**NOT included -- these branches conflict and were left out. "
                    "This pull request is an INCOMPLETE integration of the "
                    "workflow:**",
                ]
                lines += [f"- conflicted: `{b}`" for b in merge.conflicted]
            if merge.missing:
                lines += [
                    "",
                    "**NOT included -- these branches were not found on the "
                    "remote. The step either failed before pushing or never "
                    "ran:**",
                ]
                lines += [f"- missing: `{b}`" for b in merge.missing]

        # THE REVIEW THIS PULL REQUEST PASSED THROUGH (archived issue 264), on the page the
        # operator reads, so the verdict and what it found arrive with the
        # work instead of from a reviewer after it opens.
        if self._verdict is not None:
            lines += verdict_mod.pull_request_lines(self._verdict)

        return "\n".join(lines)

    def _pull_request_amendment(
        self, *, agent_body: str | None, branch: str, head: str
    ) -> str:
        """The section an ADOPTED pull request's body gains (archived issue 807).

        Headed `## Continuation (attempt N): <task>` when this task continues
        another's branch (the CI fixer), `## Republished (attempt N): <task>`
        when it is this task's own branch on a later attempt. The heading is
        unique per attempt, which is what `forge.appended_body` dedups on.
        Under it: the agent's `pr-body.md` when it wrote one (already scrubbed
        and cut by `_agent_pull_request_text`), then a short provenance block
        -- never the "Opened by SwarmCloud" block, which claims the branch for
        this task. The review's verdict, when there is one, follows it.
        """
        cfg = self.cfg
        task = self._task or {}
        try:
            continued = continuation_mod.continued_task(task.get("metadata"))
        except WorkerError:
            continued = None
        kind = "Continuation" if continued else "Republished"
        count = task.get("attempt_count")
        attempt = (
            f"attempt {int(count)}"
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
            else f"attempt `{cfg.attempt_id}`"
        )
        lines = [f"## {kind} ({attempt}): `{cfg.task_id}`", ""]
        if agent_body:
            lines += [agent_body, ""]
        lines += [
            f"- task: `{cfg.task_id}`",
            f"- attempt: `{cfg.attempt_id}`",
            f"- runner profile: `{cfg.profile.name}`",
            f"- branch: `{branch}`",
        ]
        if continued:
            lines.append(f"- continues: `{continued}`")
        if head:
            lines.append(f"- head commit: `{head}`")
        if self._verdict is not None:
            lines += verdict_mod.pull_request_lines(self._verdict)
        return "\n".join(lines)

    def _is_default_title(self, title: str) -> bool:
        """True when `title` is one the worker writes itself, never a person:
        this attempt's own default (`_generated_pull_request_title`), the
        issue-number fallback `Work on issue #N (part of #N)`, or the retired
        `[swarm] task_...` (archived issue 807: a fixer's `pr-title.txt` replaces only these).
        """
        text = (title or "").strip()
        if not text:
            return False
        if text == (self._generated_pull_request_title() or "").strip():
            return True
        # The unprefixed issue title, as a worker before Proposal B wrote it.
        if text == (self._title_from_issue_input(self._task or {}) or "").strip():
            return True
        return bool(_DEFAULT_ISSUE_TITLE_RE.match(text) or _RETIRED_TITLE_RE.search(text))

    def _worker_commit_message(self, fallback_subject: str, explanation: str) -> str:
        """The message of a commit the worker makes on the published branch.

        The subject is the agent's `pr-title.txt` when it wrote a usable one --
        the file, and the checks, `_agent_title` applies for the pull request,
        so the branch's own commit and its pull request say the same thing --
        and otherwise `fallback_subject`, which names no task (archived issue 361). The task
        id is in the body: a commit on the branch still says which task made
        it, on a line nobody reads as the change's title.
        """
        subject = self._agent_title([]) or fallback_subject
        # Prose, not a `Key: value` line, so nothing reads it as a trailer.
        return f"{subject}\n\n{explanation}\n\nMade by the worker for task {self.cfg.task_id}."

    def _agent_pull_request_text(self) -> tuple[str | None, str | None, list[str]]:
        """The agent's pull request title and body, each usable or None (archived issue 214).

        Returns `(title, body, refused)`. `refused` names each file that was
        there and could not be used, and why, in words that quote none of it.

        THE SAME RULES AS ANY OTHER AGENT TEXT THE WORKER PUBLISHES:

        * read with `open_without_following`, like every file taken out of the
          artifacts folder (archived issue 227): a link is refused, never followed;
        * UTF-8 or refused, and blank is refused;
        * the title is ONE line -- a second line is refused, not joined -- and
          neither may carry attribution (`gitops.ATTRIBUTION_MARKERS`, the
          list the push check reads): the owner's rule is none on GitHub, and
          the forge would carry it where the push check cannot see. A title
          carrying it is refused; the body has its attribution lines removed
          (`strip_attribution`, archived issue 735) and is refused only when nothing
          meaningful is left;
        * scrubbed of every registered secret, then every `@` that could
          mention given a zero-width joiner (`_neutralise_mentions`; a mention
          never refuses either file), THEN cut (the archived pull request 232 rule): the title to
          `PR_TITLE_MAX_CHARS`, the body to `PR_BODY_MAX_BYTES`.

        The body is Markdown rendered on a public page, like the prompt the
        generated body used to quote; it gets no more trust than that.
        """
        ws = self.ws
        assert ws is not None
        refused: list[str] = []
        body: str | None = None

        title = self._agent_title(refused)

        raw = self._read_agent_text(PR_BODY_FILE, refused)
        if raw is not None:
            text = raw.strip()
            if not text:
                refused.append(f"{PR_BODY_FILE}: blank")
            elif not (stripped := strip_attribution(text)):
                # Only attribution, and nothing meaningful besides (archived issue 735).
                refused.append(f"{PR_BODY_FILE}: only attribution")
            else:
                if stripped is not text:
                    self.log.info("removed the attribution lines from the agent's pull request body")
                text = stripped
                # Scrubbed FIRST, then every mention neutralised: a joiner
                # inserted inside a registered value would stop the scrub
                # matching it (owner decision, 2026-09-29: neutralised, never
                # refused).
                text = _neutralise_mentions(str(self._scrub(text)))
                encoded = text.encode("utf-8")
                if len(encoded) > PR_BODY_MAX_BYTES:
                    text = (
                        encoded[:PR_BODY_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
                        + f"\n\n_[cut at {PR_BODY_MAX_BYTES} bytes by the worker]_"
                    )
                body = text

        if refused:
            self.log.warning(
                "the agent's pull request text was not used; the generated text was",
                refused=refused,
            )
        return title, body, refused

    def _agent_title(self, refused: list[str]) -> str | None:
        """The agent's `pr-title.txt`, scrubbed and capped, or None.

        None when the file is absent, or when it is there and refused, in
        which case `refused` gains one `pr-title.txt: <why>` entry that quotes
        none of it. The one check both the publish (`_agent_pull_request_text`)
        and the finish check (`_refused_title_reason`) make, so a title the
        finish accepts is a title the publish uses.
        """
        raw = self._read_agent_text(PR_TITLE_FILE, refused)
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            refused.append(f"{PR_TITLE_FILE}: blank")
        elif "\n" in text or "\r" in text:
            refused.append(f"{PR_TITLE_FILE}: more than one line")
        elif any(ord(char) < 32 and char != "\t" for char in text):
            refused.append(f"{PR_TITLE_FILE}: holds control characters")
        elif _carries_attribution(text):
            refused.append(f"{PR_TITLE_FILE}: carries attribution")
        else:
            # A mention does not refuse a title: `_scrub_and_cap_title`
            # neutralises it (owner decision, 2026-09-29).
            candidate = self._scrub_and_cap_title(text)
            if self._title_carries_task_id(text) or self._title_carries_task_id(candidate):
                # OWNER RULE, 2026-09-28: a pull request title never carries
                # the task id. An agent's own `pr-title.txt` is not an
                # exception -- one that echoed the id (by habit, or by
                # copying the platform's old `[swarm] <task>` fallback) is
                # refused like any other unusable title.
                refused.append(f"{PR_TITLE_FILE}: names a task id")
            else:
                return candidate
        return None

    def _refused_title_reason(self) -> str | None:
        """Why the agent's `pr-title.txt` cannot title the pull request this
        attempt owes, or None when it can, is not owed, or is absent.

        OWNER DECISION, 2026-09-28: a title that EXISTS but is refused (it
        names a task id, carries attribution, ...; a mention is neutralised,
        never refused, since 2026-09-29) fails
        the attempt retryably before anything is pushed, exactly like a
        missing one (`_publish_withheld`, `_fail_for_refused_title`). The
        finish check tests that the title is USABLE, not only that the file
        exists: a refused title reaching the publish would push a branch with
        no pull request. An absent file is the missing-output path's (archived issue 149).
        """
        if self.ws is None or PR_TITLE_FILE not in self._expected_outputs:
            return None
        if not self._title_owed(self._task or {}):
            return None
        refused: list[str] = []
        if self._agent_title(refused) is not None or not refused:
            return None
        why = refused[0].partition(": ")[2] or "unusable"
        return f"{PR_TITLE_FILE} refused: {why}; write a fact-style title"

    def _scrub_and_cap_title(self, text: str) -> str:
        """Redact every registered secret, neutralise every mention, THEN cut
        to `PR_TITLE_MAX_CHARS` (archived pull request 232's rule): scrubbed first, or a cut prefix
        of a secret would survive the cut that was supposed to remove it, and
        before the joiners go in, which would stop a registered value that
        holds an `@` from matching. Every title the platform sends -- the
        agent's and one made from an issue title -- comes through here, so
        none can page anyone (owner decision, 2026-09-29)."""
        text = _neutralise_mentions(str(self._scrub(text)))
        if len(text) > PR_TITLE_MAX_CHARS:
            text = text[: PR_TITLE_MAX_CHARS - 3].rstrip() + "..."
        return text

    def _title_carries_task_id(self, title: str) -> bool:
        """True when `title` names this attempt's task id, or looks like the
        platform's own old `[swarm] task_...` fallback (owner rule,
        2026-09-28). Checked against the agent's own `pr-title.txt` too: an
        agent that echoed the id, or copied the retired fallback's shape, gets
        no exception to a rule stated for the platform's generated text.
        """
        task_id = self.cfg.task_id
        if task_id and task_id in title:
            return True
        return _RETIRED_TITLE_RE.search(title) is not None

    def _generated_pull_request_title(self) -> str | None:
        """The platform's own pull request title when the agent wrote none, or None.

        NEVER THE TASK ID, AND NEVER INVENTED (owner decisions, 2026-09-28).
        The only title the platform writes is the one the step's `issue`
        input names (`_title_from_issue_input`), and it is PREFIXED
        `[title missing] ` (Proposal B, owner decision 2026-10-08): the agent
        was asked for `pr-title.txt` (`_title_requested`) and given a
        follow-up turn for it, so reaching this is a miss, and a reviewer has
        to be able to see it. Without an issue the agent's `pr-title.txt` is
        REQUIRED: a title made up from the prompt's first sentence or the
        runner profile described the request, not the change, and
        `f"[swarm] {task_id}"` described nothing. None here means no pull
        request is opened (`_publish_git`), and `_title_owed` makes a missing
        `pr-title.txt` a retryable failure of the attempt before that.
        """
        title = self._title_from_issue_input(self._task or {})
        if title is None:
            return None
        return self._scrub_and_cap_title(f"{TITLE_MISSING_PREFIX}{title}")

    def _title_requested(self, task: dict[str, Any]) -> bool:
        """True when this attempt will open a pull request the agent is asked
        to title: it may publish, its strategy opens one (`direct-pr`, the
        `integrate` integrator, a `single-pr` author) and it has a repository.

        WITH OR WITHOUT AN ISSUE (Proposal B, owner decision 2026-10-08).
        Until then a step whose `issue` input named an issue was never asked:
        `_title_owed` was false for it, and the worker silently titled its
        pull request with the issue's title -- 17 of 32 pull requests the
        observer read on 2026-10-08. Requested, `pr-title.txt` is an expected
        output, so the runner tells the agent to write it and gives it a
        follow-up turn when it did not (`cliagent`'s repair turn).
        """
        if not self.cfg.git_publish_enabled:
            return False
        if not (self.cfg.repository_url or task.get("repository_url")):
            return False
        # A step publishing onto an existing pull request (archived issue 2183) owes no
        # title: it retitles that pull request only when its agent wrote one.
        try:
            if publishpr_mod.target(task.get("metadata")) is not None:
                return False
        except WorkerError:
            return False
        # `_opens_pull_request`: `direct-pr`, the `integrate` integrator, and
        # a `single-pr` author, whose `pr-title.txt` is what the review sees
        # and the merge refuses any other title against (merge-step.md §3).
        return self._opens_pull_request()

    def _title_owed(self, task: dict[str, Any]) -> bool:
        """True when the title is requested (`_title_requested`) and nothing
        else can title the pull request: its `issue` input names no issue. A
        missing or refused `pr-title.txt` then fails the attempt; with an
        issue the `[title missing] ` fallback titles it instead.
        """
        return self._title_requested(task) and self._title_from_issue_input(task) is None

    def _title_from_issue_input(self, task: dict[str, Any]) -> str | None:
        """"<the issue's title> (part of #N)", else "Work on issue #N (part of #N)".

        NEVER A CLOSING KEYWORD. This used to fall back to "Fixes #N", and a
        squash merge writes the title into a commit on the base branch, which
        GitHub reads for closing keywords: the issue closed on merge whatever
        the review found, and for an issue run the only correction was the
        API's keyword block (`issuesync.sync_pull_request`), which a single
        failed GitHub write left unapplied (review of archived pull request 545). The platform's
        title therefore says what the work is and that it is `part of #N`;
        whether the pull request closes the issue is said in its body, by
        whatever decides completeness -- for an issue run, the API's block
        from the review's per-requirement verdict (owner decision on archived issue 454).
        Without an issue title the work is named by the issue number alone,
        still as `part of`. (Not the step's `metadata.label`: the step-spec
        signature does not cover it, and the worker reads no unsigned key.)

        `input.issue` is `#265`'s field (contract request 28, accepted
        2026-09-28): "a positive integer naming an issue in the step's own
        `repo`", which the worker fetches read-only and writes to
        `issue.md`. That fetch is landing separately and had not, as of this
        change, shipped, so this reads the number defensively -- a bare
        integer or a numeric string, also tolerating a mapping with a
        `number`/`issue_number` key in case the shape changes before it
        lands -- and returns None, not a guess, when there is no number at
        all. `self._issue_title` is the fetched issue's title, set in
        `_prepare` when the fetch lands (`_issue_title_from`); without one --
        no fetch, or an untitled issue -- the number names the work.
        """
        payload = task.get("input")
        issue = payload.get("issue") if isinstance(payload, dict) else None
        title = self._issue_title if isinstance(self._issue_title, str) and self._issue_title.strip() else None
        if isinstance(issue, dict):
            number = _as_issue_number(issue.get("number"))
            if number is None:
                number = _as_issue_number(issue.get("issue_number"))
            if title is None:
                raw_title = issue.get("title")
                if isinstance(raw_title, str) and raw_title.strip():
                    title = raw_title.strip()
        else:
            number = _as_issue_number(issue)
        if number is None:
            return None
        # An issue's title is anyone's text: a mention in it would page that
        # person or team from a title the platform wrote, so
        # `_scrub_and_cap_title` puts a zero-width joiner after each `@`.
        text = f"{title} (part of #{number})" if title else f"Work on issue #{number} (part of #{number})"
        return self._scrub_and_cap_title(text)

    def _read_agent_text(self, name: str, refused: list[str]) -> str | None:
        """`artifacts/<name>` as text, read with no link followed; None if absent or refused."""
        ws = self.ws
        assert ws is not None
        try:
            fd = standalone_mod.open_without_following(ws.artifacts, name)
        except FileNotFoundError:
            return None
        except standalone_mod.Refused:
            refused.append(f"{name}: a symlink, or not a regular file; nothing was followed")
            return None
        except OSError as exc:
            refused.append(f"{name}: unreadable ({type(exc).__name__})")
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(PR_READ_LIMIT_BYTES + 1)
        except OSError as exc:
            refused.append(f"{name}: unreadable ({type(exc).__name__})")
            return None
        cut = len(data) > PR_READ_LIMIT_BYTES
        if cut:
            data = data[:PR_READ_LIMIT_BYTES]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A read cut through the middle of a character is not a file that
            # is not UTF-8: at most three bytes of it are the cut's.
            if cut and exc.start >= len(data) - 3:
                return data[: exc.start].decode("utf-8")
            refused.append(f"{name}: not UTF-8")
            return None

    def _artifacts_first(self) -> list[str]:
        """The names the artifacts folder's cap takes before any other (archived issue 228).

        The expected outputs a later step declares, then the files the platform
        writes into the folder itself: the agent CLI's stream captures and
        transcript, and the harvest's patch. `artifact_manifest.upload_order`
        says why each comes first.
        """
        first = list(self._expected_outputs)
        # The agent's questions for the owner (`agent_worker.questions`): a
        # file the cap dropped would be questions nobody is told of.
        first.append(questions_mod.NAME)
        files = agent_stream_files(self.cfg.runner_profile)
        if files is not None:
            first += files.names()
        first.append(PATCH_NAME)
        # Beside the patch, the branch a failed publish kept (archived issue 3174).
        first.append(WORK_BUNDLE_NAME)
        return first

    def _upload_outputs(
        self, *, publish: bool = False, withheld: str = "", defer_publish: bool = False
    ) -> dict[str, Any]:
        """Harvest, upload and describe what this attempt leaves behind.

        `defer_publish` is `_finalise`'s (archived issue 165): the harvest describes the work
        and writes the patch, and the push and the pull request wait for
        `_publish_checked`, which runs only once the upload manifest has passed
        the missing-output check. Every other caller parks, cancels or crashes
        and publishes nothing, as before.
        """
        ws = self.ws
        if ws is None:
            return {}
        # SPEND FIRST. Every exit that writes a terminal or parked state passes
        # through here before it does -- the clean finish, the three parks, the
        # cancellation, the SIGTERM and the crash handler -- so this is where
        # the attempt's spend lands ahead of its state, whichever way it is
        # leaving. `_cleanup` records again for the two cases that cannot be
        # here yet: a runner still alive when the worker crashed, and a
        # mid-run fence, which uploads nothing.
        self._record_spend()
        # THE CPU BESIDE IT (archived pull request 210 review), for the exit whose runner is
        # still alive -- the crash handler -- or was never reaped through
        # `_stop_sampler`: without this the attempt kept its last periodic
        # reading, up to one reading behind, drawn as the figures at exit.
        # Written only when the figures changed, so an orderly exit that just
        # reaped its runner writes nothing twice.
        self._record_cpu()
        # BEFORE the upload, not after: the harvest writes a patch into
        # `artifacts/`, and the upload's redacted copy (`_upload_copy`) is what
        # scrubs what goes up from there. A patch produced afterwards would be
        # the one file in the upload that never had a provider key taken out
        # of it.
        self._deferred_publish = None
        try:
            if defer_publish:
                git_summary = self._harvest_git(publish=False, defer=True)
            else:
                git_summary = self._harvest_git(publish=publish, withheld=withheld)
        except Exception as exc:  # pragma: no cover - defensive
            self.log.exception("the git harvest raised; continuing without it", exc)
            git_summary = {"error": "the git harvest failed unexpectedly"}
        if defer_publish and self._security_review is not None:
            # A security review's verdict is the worker's, derived from the
            # validated audit, never the agent's; a UI path does not gate it.
            self._finish_security_review()
        elif defer_publish:
            # After the harvest's reap, so the agent can no longer write it,
            # and before the upload, so what goes up is the verdict that counts.
            self._hold_ui_review_to_rendered()
        if defer_publish:
            # A QA flow step's stray tar (re-run 8's flow-1.tar for flows-1)
            # is renamed to its owed `<step>.tar` after the reap and before the
            # upload, so the missing-output check and the flow report read it.
            self._normalise_flow_output()
        # WHICH FILES OF THE ARTIFACTS FOLDER, IN WHICH ORDER (archived issue 228, owner
        # decision of 2026-09-26): at most `max_artifact_files`, a declared
        # output first, no name past the manifest's bound; see
        # `agent_worker.artifact_manifest`. Decided BEFORE anything is copied,
        # so only what is going to leave the pod is read and redacted.
        #
        # WALKED WITHOUT RECURSION (archived issue 227). `Path.rglob` recursed once per
        # folder on Python 3.11, so a tree about 1,000 folders deep raised
        # RecursionError out of this method and none of the attempt's
        # artifacts, logs or summary went up. `_walk_artifacts` keeps its own
        # stack, and lists a folder too deep to hold any storable name as
        # skipped instead of walking it.
        found, too_deep = self._walk_artifacts(ws.artifacts)
        plan = manifest_mod.plan(
            found,
            first=self._artifacts_first(),
            cap=self.cfg.max_artifact_files,
            declared=self._expected_outputs,
        )
        if plan.declared_past_bound:
            # MORE DECLARED NAMES THAN THE MANIFEST'S BUDGET ALLOWS (archived issue 227): the
            # ones past `MAX_DECLARED_NAMES` lost their exemption from the name
            # bound in `plan`, which logs nothing itself. Named here, through
            # this logger, because the names come from task metadata: SCRUBBED
            # FIRST, then cut, the order the archived pull request 232 review set for every name.
            # `LOG_BATCH` to a line, so metadata holding thousands cannot pass
            # Cloud Logging's entry limit.
            past = [
                standalone_mod.shown(self._scrub(name)) for name in plan.declared_past_bound
            ]
            batch = standalone_mod.LOG_BATCH
            batches = (len(past) + batch - 1) // batch
            for index in range(batches):
                self.log.warning(
                    "declared outputs past the manifest's bound are held to its "
                    "name length like any other file",
                    count=len(past),
                    batch=f"{index + 1} of {batches}",
                    declared_exempt=manifest_mod.MAX_DECLARED_NAMES,
                    cap_name_bytes=manifest_mod.MAX_NAME_BYTES,
                    names=past[index * batch : (index + 1) * batch],
                )
        skipped: list[str] = []
        # WHY each one (archived issue 165), index for index with `skipped`: the
        # missing-output check reads it to decide whether a retry can help.
        skip_causes: list[str] = []
        # The same causes keyed by each name AS WRITTEN, for this attempt's
        # own check: the list above holds names scrubbed and cut to 256
        # characters, and a declared output can be longer than that, so its
        # cause looked up by the declared name was not found and it was
        # called not written, and retried.
        raw_causes: dict[str, str] = {}
        for folder in too_deep:
            # Scrubbed, then cut, like every name below (archived pull request 232 review).
            skipped.append(standalone_mod.shown(self._scrub(folder)))
            skip_causes.append(expected_mod.CAUSE_REFUSED)
            raw_causes[folder] = expected_mod.CAUSE_REFUSED
        for name in plan.unstorable:
            # A name whose bytes are not UTF-8 (archived pull request 225 review): no object can be
            # named with it, and as a lone surrogate in the summary it made
            # `finish` raise, and the next attempt fail the same way. Named as
            # the bytes it was, in the log and the skipped list.
            # SCRUBBED FIRST (archived pull request 232 review): cutting a raw name let a
            # registered secret crossing character 256 survive in part.
            shown = standalone_mod.shown(self._scrub(name))
            self.log.warning(
                "an artifact's name is not valid UTF-8, so no object can be "
                "named with it; not uploaded",
                artifact=shown,
            )
            skipped.append(shown)
            skip_causes.append(expected_mod.CAUSE_REFUSED)
            raw_causes[name] = expected_mod.CAUSE_REFUSED
        for entry in plan.not_uploaded:
            if entry["reason"] == manifest_mod.NAME_TOO_LONG:
                # Listed as archived pull request 225 lists a name it could not upload: cut short,
                # so 50 of them cannot take the summary near 1 MiB either.
                # SCRUBBED FIRST (archived pull request 232 review): cutting a raw name let a
                # registered secret crossing character 256 survive in part.
                skipped.append(standalone_mod.shown(self._scrub(entry["name"])))
                skip_causes.append(expected_mod.CAUSE_REFUSED)
                raw_causes[entry["name"]] = expected_mod.CAUSE_REFUSED
        # Past the cap: COUNTED in the summary (`artifacts_over_cap`, below),
        # and every name in the log -- the owner's shape for archived issue 228, through the
        # helper archived pull request 225's working-folder cap uses. Not in `artifacts_skipped`,
        # whose readers call a name there dropped at the SIZE cap. A name over
        # the bound is logged cut short, as archived pull request 225 logs one: a path runs to
        # 4,096 bytes, and 100 of those would pass Cloud Logging's 256 KiB
        # entry. A name under it is logged whole (`shown` leaves it alone).
        self._artifacts_not_uploaded = tuple(entry["name"] for entry in plan.not_uploaded)
        self._not_uploaded_causes = {
            entry["name"]: (
                expected_mod.CAUSE_CAP
                if entry["reason"] == manifest_mod.OVER_CAP
                else expected_mod.CAUSE_REFUSED
            )
            for entry in plan.not_uploaded
        }
        self._log_not_uploaded(
            "files in $SWARM_ARTIFACTS_DIR were not uploaded",
            [
                {**entry, "name": standalone_mod.shown(self._scrub(entry["name"]))}
                for entry in plan.not_uploaded
            ],
            cap_files=self.cfg.max_artifact_files,
            cap_name_bytes=manifest_mod.MAX_NAME_BYTES,
        )
        # The worker's own files, rewritten where they lie. Everything taken
        # from the artifacts folder is redacted as the COPY that is uploaded
        # (`_upload_copy`), never in place: see `_redact_before_upload`.
        unredacted = self._redact_before_upload()
        artifacts: list[dict[str, Any]] = []
        total = 0
        for rel in plan.take:
            key = f"{self.cfg.artifact_prefix}/{rel}"
            sent = self._upload_copy(
                ws.artifacts,
                rel,
                key=key,
                label=f"artifacts/{rel}",
                limit=self.cfg.max_artifact_bytes - total,
            )
            if sent.bytes is None:
                # SCRUBBED, THEN CUT (archived issue 227), as every other name this list
                # holds: a raw name ran to 1,024 bytes for a declared output,
                # and was scrubbed only later, with the summary, after
                # nothing had cut it.
                skipped.append(standalone_mod.shown(self._scrub(rel)))
                skip_causes.append(_skip_cause(sent.skipped))
                raw_causes[rel] = skip_causes[-1]
                continue
            # Listed only for a file that LEFT (archived issue 227): one the byte cap or an
            # upload failure kept in the pod was never uploaded "as-is".
            if sent.unredacted is not None:
                unredacted.append(sent.unredacted)
            total += sent.bytes
            artifacts.append({"name": rel, "bytes": sent.bytes, "uri": self.store.uri(key)})
        # LISTED IN PATH ORDER, as the manifest always has been: the order
        # above decides which files are taken, not the order a reader sees
        # them in, nor which of them the listing route's first page holds.
        artifacts.sort(key=lambda entry: entry["name"].split("/"))
        # An index run's repo-index.json also goes to its home under repos/
        # (lane IX3), before `finish` makes the task promotable.
        self._index_keep_copy(artifacts)

        # WHAT A CLI AGENT WITH NO REPOSITORY CREATED IN ITS WORKING FOLDER
        # (#184, owner decision of 2026-09-26), after the artifacts folder so a
        # file there keeps its name, and into the same manifest under
        # `workdir/`, so the Artifacts tab lists and serves it like any other.
        # A repository task returns None here and is unchanged.
        try:
            workdir = self._upload_workdir_outputs(
                taken={entry["name"] for entry in artifacts},
                budget=self.cfg.max_artifact_bytes - total,
            )
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.exception("the working-folder upload raised; continuing without it", exc)
            workdir = None
        if workdir is not None:
            # Its files it did not upload stay in `workdir_outputs`, with their
            # reasons, and out of `artifacts_skipped`, whose every reader calls
            # a name there dropped at THIS folder's size cap (archived pull request 225 review).
            artifacts.extend(workdir["uploaded"])
            total += workdir["bytes"]

        # THE RUNNER'S TWO STREAMS, AND THE AGENT'S TWO (#184). The agent
        # CLI's captures stay in `artifacts/` too -- a dependant's `input_from`
        # stages them from there -- and a copy goes beside the runner's logs as
        # `logs/agent_stdout.log` / `logs/agent_stderr.log`, so every reader of
        # an agent's own output reads one layout whatever the runner, and the
        # copy is not subject to `max_artifact_bytes`. Scrubbed whether or not
        # the file cap took them into the manifest: each copy is redacted as
        # it is uploaded.
        #
        # Through the same no-follow copy as an artifact (archived issue 227): the check for
        # a link and the upload by path were two moments, and a capture
        # swapped for a link between them was followed.
        agent_files = agent_stream_files(self.cfg.runner_profile)
        logs: dict[str, str] = {}
        reported = {entry.get("file") for entry in unredacted}
        for label, path in self._stream_files(ws):
            key = f"{self.cfg.log_prefix}/{label}.log"
            sent = self._upload_copy(
                path.parent,
                path.name,
                key=key,
                label=_workspace_label(ws, path),
                limit=sys.maxsize,
                content_type="text/plain",
                absent_ok=True,
                report=False,
            )
            if sent.bytes is None:
                continue
            logs[label] = self.store.uri(key)
            # Each file once: the runner's two streams were examined in place
            # above, and a capture the cap took was examined as an artifact.
            entry = sent.unredacted
            if entry is not None and entry.get("file") not in reported:
                self._report_unredacted(entry)
                unredacted.append(entry)
                reported.add(entry.get("file"))

        # EVERY SKIPPED NAME, IN THE LOG (archived issue 227). The summary lists the first
        # 50; this line used to carry only the count, and `_upload_copy` logs
        # no name for a file the byte cap kept, so the 51st on was named
        # nowhere. Each name is already scrubbed and cut (`shown`), and
        # `_log_not_uploaded` writes them `LOG_BATCH` to a line.
        self._log_not_uploaded(
            "artifacts skipped",
            [
                {"name": name, "reason": cause}
                for name, cause in zip(skipped, skip_causes)
            ],
            cap_bytes=self.cfg.max_artifact_bytes,
        )
        summary: dict[str, Any] = {
            "artifacts": artifacts,
            "artifact_bytes": total,
            "logs": logs,
            "agent_streams": self._agent_streams(agent_files, artifacts),
        }
        if git_summary is not None:
            summary["git"] = git_summary
        self._record_forge_credential(summary)
        if workdir is not None:
            summary["workdir_outputs"] = workdir["summary"]
        # THE AGENT'S QUESTIONS FOR THE OWNER (owner decision, 2026-10-05):
        # `questions: N` for a valid `questions.json` the upload took, 0 with
        # `questions_rejected` for one that is not, 0 alone for none. Counted,
        # never acted on: no state below reads it. An invalid file is a line
        # here, not a failed attempt. See `agent_worker.questions`.
        cause = raw_causes.get(questions_mod.NAME) or self._not_uploaded_causes.get(
            questions_mod.NAME
        )
        asked = questions_mod.summarise(
            ws.artifacts,
            uploaded={entry["name"] for entry in artifacts},
            cause=expected_mod.cause_text(cause) if cause else None,
        )
        if questions_mod.REJECTED_KEY in asked:
            self.log.warning(
                "questions.json was not taken as questions for the owner",
                reason=asked[questions_mod.REJECTED_KEY],
            )
        summary.update(asked)
        if skipped:
            # Each entry names the file and WHY it was skipped (archived issue 165): the
            # cap, an upload error, a refused file. Read through
            # `expected_mod.skipped_entries`, which also accepts the bare
            # names older summaries hold.
            summary["artifacts_skipped"] = [
                {"name": name, "cause": cause}
                for name, cause in zip(skipped[:50], skip_causes[:50])
            ]
        # Every skipped name's cause, uncut and by the name as written, for
        # this attempt's own check: the summary's list stops at 50, and a
        # declared output can be the 51st or longer than the cut.
        self._skip_causes = raw_causes
        if plan.over_cap:
            # How many files the folder held past the cap, and the cap, so a
            # reader can say "N over the 500-file cap" without restating the
            # number (archived issue 228). Absent when every file fitted: a zero would read
            # as something dropped.
            summary["artifacts_over_cap"] = plan.over_cap
            summary["artifacts_cap_files"] = self.cfg.max_artifact_files
        if unredacted:
            # Files that left the pod without being rewritten AND without a
            # clean scan. Capped like the list above; the log carries them all.
            summary["redaction_skipped"] = unredacted[:50]
        if self._last_checkpoint is not None:
            summary["checkpoint"] = {
                "checkpoint_id": self._last_checkpoint.checkpoint_id,
                "uri": self._last_checkpoint.uri,
                "bytes": self._last_checkpoint.archive_bytes,
            }
        if self._restored_from is not None:
            summary["restored_from"] = {
                "checkpoint_id": self._restored_from.checkpoint_id,
                "attempt_id": self._restored_from.attempt_id,
            }
        if self._staged_inputs:
            summary["staged_inputs"] = [item.as_dict() for item in self._staged_inputs]
        if self._inputs_left_nothing:
            summary["staged_inputs_left_nothing"] = [dict(i) for i in self._inputs_left_nothing]
        if self._builds_on_resolved is not None and self._builds_on_resolved[1]:
            # Where a step that builds on a no-change step started (archived issue 978).
            summary["builds_on_resolved"] = {
                "builds_on": self._dispatch_builds_on(),
                "task_id": self._builds_on_resolved[0] or None,
                "left_nothing": list(self._builds_on_resolved[1]),
            }
        # This dict becomes `task.result_summary`, a Firestore document that
        # every reader of the task can see. It is scrubbed on the way out for
        # the same reason the files above are.
        return self._scrub(summary)

    def _agent_streams(
        self, files: Any, artifacts: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """`result_summary.agent_streams`: which uploaded artifact is which agent stream.

        None -- explicitly, not a missing key -- when the runner has no agent
        child (mock, browser), which is what lets a reader say "not
        applicable" instead of "absent". Otherwise each name is the manifest
        entry that holds that stream, or None when it was not uploaded (the
        runner never wrote it, or `max_artifact_bytes` dropped it: the copy in
        `logs/` is still there). `transcript_skipped` is the runner's own
        report: `"too_large"` when it omitted a transcript over the cap
        (`cliagent.TRANSCRIPT_MAX_CHARS`), `"capture_truncated"` when the
        stdout it would be built from had its middle dropped at the output cap,
        and None otherwise -- including when no result.json was written, which
        says nothing either way.

        `stdout_truncated` / `stderr_truncated` (archived pull request 188 review) say whether the
        output cap cut that agent stream: the capture then kept its start and
        its end, and wrote a notice where it dropped the middle. The runner
        reports them on every outcome (`RunnerContext.report`); None when it
        reported nothing, which is "not known", never "not cut".
        """
        if files is None:
            return None
        uploaded = {entry.get("name") for entry in artifacts}
        result = _read_json(self.ws.result_path) if self.ws is not None else None
        output = (result or {}).get("output")
        output = output if isinstance(output, dict) else {}
        skipped = output.get("transcript_skipped")
        if skipped not in ("too_large", "capture_truncated"):
            skipped = None

        def cut(stream: str) -> bool | None:
            value = output.get(f"{stream}_truncated")
            return value if isinstance(value, bool) else None

        return {
            "stdout": files.stdout if files.stdout in uploaded else None,
            "stderr": files.stderr if files.stderr in uploaded else None,
            "transcript": (
                files.transcript if files.transcript and files.transcript in uploaded else None
            ),
            "transcript_skipped": skipped,
            "stdout_truncated": cut("stdout"),
            "stderr_truncated": cut("stderr"),
        }

    # -- metrics -----------------------------------------------------------
    def _start_sampler(self, child: ChildProcess) -> None:
        ws = self.ws
        assert ws is not None
        # THE MEMORY GUARD IS ON FOR EVERY RUNNER (archived issue 2504): at 95% of the
        # cgroup's `memory.max` the sampler kills the agent's largest process
        # rather than let the kernel's OOM killer take this worker with it.
        # Its kills are logged by `_log_memory_guard_kills` and counted on the
        # runner's usage (`ResourceUsage.memory_guard_kills`).
        # THE CONTAINER'S MEMORY, NOT THE PROFILE'S (archived issue 205). The dispatcher
        # sizes the container from the task's class, and a workflow step may
        # narrow `browser` (16 GiB) to `standard` (8 GiB): judged against the
        # profile's 16, a run at 7.9 of its 8 GiB raised no near miss.
        if self._series_origin is None:
            self._series_origin = time.monotonic()
        self._sampler = ResourceSampler(
            pid_provider=lambda: child.pid,
            disk_provider=ws.disk_bytes,
            memory_limit_bytes=self.cfg.memory_limit_bytes_of(self._memory_class()),
            series_origin=self._series_origin,
        )
        self._sampler.start()

    def _stop_sampler(self) -> None:
        if self._sampler is None:
            return
        usage = self._sampler.stop()
        # Kept, not replaced by the next runner's sampler: that replacement is
        # what made every figure below describe the last runner only.
        self._runner_usage.append(usage)
        # And its points since the last heartbeat, for the next one to carry.
        self._pending_points.extend(self._drain_points())
        self._log_memory_guard_kills()
        self._sampler = None
        # THE ATTEMPT'S PEAKS, not this runner's. `record_resource_usage`
        # merge-sets absolute values, so writing each runner's own peak let a
        # quiet second runner overwrite the first runner's high-water mark --
        # and a near miss in the first -- on the one document a sizing report
        # reads.
        attempt = self._attempt_usage() or usage
        # NOT FATAL, AND IT MUST NOT SKIP THE CPU WRITE BELOW (archived pull request 210 review).
        # Unguarded, a Firestore error here raised out of every site that
        # reaps a runner, and the attempt's CPU figures for this runner were
        # never written: Details then drew the previous periodic reading as
        # the figures at exit. The memory peak is telemetry like the CPU.
        try:
            self.control.record_resource_usage(
                peak_rss_bytes=attempt.peak_rss_bytes,
                peak_disk_bytes=attempt.peak_disk_bytes,
                oom_near_miss=attempt.oom_near_miss,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not record resource usage", error=f"{type(exc).__name__}: {exc}")
        if usage.oom_near_miss:
            self.log.error(
                "OOM NEAR MISS: this attempt came within a hair of its memory limit",
                peak_rss_bytes=usage.peak_rss_bytes,
                peak_memory_current_bytes=usage.peak_memory_current_bytes,
                limit_bytes=self.cfg.memory_limit_bytes_of(self._memory_class()),
                resource_class=self._memory_class(),
            )
        # The attempt's CPU at this runner's end, on the attempt (request #15)
        # -- where archived pull request 188 emitted a `final` HEARTBEAT into the task's events.
        self._record_cpu()

    def _log_memory_guard_kills(self) -> None:
        """One warning per process the live sampler's memory guard killed. Never raises.

        The pid, command name and sizes only: never the argv, which can carry
        a secret. Drained on every heartbeat and when the runner is reaped.
        """
        if self._sampler is None:
            return
        try:
            kills = self._sampler.drain_guard_kills()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read memory guard kills", error=f"{type(exc).__name__}: {exc}")
            return
        for kill in kills:
            self.log.warning(
                "memory guard killed the agent's largest process",
                pid=kill.get("pid"),
                name=kill.get("name"),
                rss_bytes=kill.get("rss_bytes"),
                memory_current_bytes=kill.get("memory_current_bytes"),
                working_set_bytes=kill.get("working_set_bytes"),
                memory_max_bytes=kill.get("memory_max_bytes"),
            )

    def _attempt_usage(self) -> ResourceUsage | None:
        """Every runner this attempt has started, combined; None if none has.

        The live runner is included as far as it has got, so a heartbeat
        mid-run and the export on a crash that left a runner alive both see
        the whole attempt.
        """
        parts = list(self._runner_usage)
        if self._sampler is not None:
            parts.append(self._sampler.usage)
        return combine_usage(parts)

    @contextmanager
    def _metrics_after_the_record(self) -> Iterator[None]:
        """Export the attempt's metrics AFTER the write that records its end.

        Every park and every terminal write runs inside this, so telemetry can
        never delay one. Measured on mock execution swarm-job-smoke-mock-sk8h9
        (2026-10-06 06:05): `_park_for_quota` exported first, and 53 s passed
        between the "attempt resource usage" line and the broker POST with
        nothing logged (1.3-3.1 s across 24 other quota parks). For all of it
        the task was LEASED and held its slot -- invariant 3 counts it -- for
        a measurement. The exporter is bounded too
        (`metrics.EXPORT_TIMEOUT_SECONDS`); that bound now only limits how
        long a hung exporter holds the exit.

        In a `finally`: a write the fence refused still exports, as it did
        when the export came first, and the refusal then propagates as before.
        """
        try:
            yield
        finally:
            self._export_metrics()

    def _export_metrics(self) -> None:
        """Export once per attempt. Never raises.

        It runs after the park or terminal write (`_metrics_after_the_record`),
        so an exception here would reach the crash handler with the task
        already PARKED or ended, and `_safe_finish` would write FAILED over
        it. A failure is logged once and swallowed, and not retried: the
        measurement is also in the exporter's log line, and a retry would wait
        on the same failing endpoint.
        """
        if self._metrics_exported:
            return
        usage = self._attempt_usage()
        if usage is None:
            return
        self._metrics_exported = True
        try:
            self.metrics.export(
                usage,
                {
                    "tenant_id": self.cfg.tenant_id,
                    "runner_profile": self.cfg.runner_profile,
                    # The sized class (archived issue 205): the sizing decisions read this
                    # label, and the profile's would file a narrowed step under
                    # a class its container was not.
                    "resource_class": self._memory_class(),
                    "backend": self.cfg.backend,
                    "attempt_id": self.cfg.attempt_id,
                    "task_id": self.cfg.task_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "metrics export failed",
                exporter=type(self.metrics).__name__,
                error=f"{type(exc).__name__}: {exc}",
            )

    # -- misc --------------------------------------------------------------
    def _remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _install_signal_handlers(self) -> None:
        # Through `route_signals`, which also re-arms the SIGTERM stack dump
        # that installing a Python handler would otherwise have disarmed.
        route_signals(self._on_signal)

    def _on_signal(self, signum: int, frame: Any) -> None:
        """SIGTERM or SIGINT. What it does depends on the window; see the module docstring.

        It used to set `_interrupted` and nothing else, and only the runner's
        supervision loop read that. A worker stuck before its runner existed
        ignored the reconciler's SIGTERM and died to the SIGKILL 120 s later
        without a line (the 2026-09-24 GKE incident). In the last window it
        still only sets the flag, and says so at INFO.
        """
        self._interrupted = True
        mode = self._signal_mode
        if mode == _SIGNAL_EXIT_NOW:
            self.phases.exit_on_signal(signum, frame)
        elif mode == _SIGNAL_RAISE:
            interrupt = StartupInterrupted(signum, self.phases.current)
            # Recorded BEFORE it is raised, and only the first. The exception
            # can be replaced on its way up (see `_execute`); the record
            # cannot. A second signal still raises, to cut short whatever the
            # first one's unwinding is waiting on.
            if self._startup_interrupt is None:
                self._startup_interrupt = interrupt
            raise interrupt
        else:
            # The runner exists (or the worker is on its way out): a SIGTERM
            # is the ordinary way an attempt is stopped, not a fault. ONE
            # INFO line, and no stack dump: the dump was disarmed when the
            # runner started (`_run_child_supervised`), because GKE files
            # every stderr line as ERROR (owner, 2026-09-25). The supervision
            # loop does the stopping, on the flag set above.
            name, phase = signal_name(signum), self.phases.current
            say_from_signal_handler(
                lambda: self.log.info(
                    f"stopping: {name} in phase {phase}", signal=name, phase=phase
                )
            )

    @contextmanager
    def _signal_policy(self, mode: str) -> Iterator[None]:
        previous = self._signal_mode
        self._signal_mode = mode
        try:
            yield
        finally:
            self._signal_mode = previous

    def _finish_terminal(
        self,
        *,
        state: TaskState,
        exit_code: int | None,
        error: str | None = None,
        result_summary: dict[str, Any] | None = None,
        end_cause: EndCause | None = None,
    ) -> None:
        """`ControlPlane.finish`, written once more with a minimal summary when
        Firestore refuses the document (archived issue 1438).

        MEASURED 2026-10-16: a `result_summary` holding an array inside an
        array made Firestore answer `400 ... invalid nested entity`
        (InvalidArgument); it propagated out of `_record_finalised` as
        "worker crashed", the process exited 1, the lease was released only
        ~100 s later by the reconciler as a lost worker, and the task was
        retried -- after an attempt that had decided its own state.
        `agent_worker.storable` reshapes the known case; this is what holds
        for the next shape, or a size, nobody has seen yet.

        So on InvalidArgument, and nothing else, the SAME transition is
        written once more through the same fenced `finish` -- state, exit
        code, error and end cause unchanged -- with `_minimal_terminal_summary`
        in place of the summary. The refused write committed nothing (the
        transition is the first write and is one transaction), so the lease is
        released once, by that second `finish`. A fenced refusal is never
        retried here: it is a `FencedError`, re-raised as it was, and a
        refusal of the minimal write too raises the FIRST refusal, as before.
        Logged by type and message only, never the summary.

        Kept as `_decided_terminal` while it is written, for the crash path to
        write the same transition should Firestore's contention outlast every
        retry (archived issue 2650).
        """
        self._decided_terminal = _DecidedTerminal(
            state=state,
            exit_code=exit_code,
            error=error,
            end_cause=end_cause,
            result_summary=result_summary,
        )
        self._write_terminal_once_more_if_refused(
            lambda summary: self.control.finish(
                state=state,
                exit_code=exit_code,
                error=error,
                result_summary=summary,
                end_cause=end_cause,
            ),
            decided=state.value,
            error=error,
            result_summary=result_summary,
        )

    def _fail_retryably_terminal(
        self,
        *,
        exit_code: int | None,
        error: str,
        cause: str,
        result_summary: dict[str, Any] | None = None,
        retry_delay_seconds: int = 0,
        detail: dict[str, Any] | None = None,
        end_cause: EndCause | None = None,
    ) -> TaskState:
        """`ControlPlane.fail_retryably`, under the same refusal rule as
        `_finish_terminal` (archived issue 1438): the attempt's retryable ends
        (`_fail_for_missing_outputs`, a refused title, a final-tree leak, an
        unreachable forge, an unreadable grant, an unpublished verdict) wrote
        the same summary `finish` did and crashed the same way when Firestore
        refused it.

        On InvalidArgument, and nothing else, the same transition is written
        once more -- cause, delay, detail, exit code, error and end cause
        unchanged -- with the minimal summary. Which state the task lands in
        (READY, FAILED or CANCELLED) is still decided inside that one fenced
        transaction, so the minimal summary records `failed_retryably`, what
        the attempt decided, not a state it cannot know. A FencedError is
        re-raised, never retried. Returns the state the task was left in.
        """
        return self._write_terminal_once_more_if_refused(
            lambda summary: self.control.fail_retryably(
                exit_code=exit_code,
                error=error,
                cause=cause,
                result_summary=summary,
                retry_delay_seconds=retry_delay_seconds,
                detail=detail,
                end_cause=end_cause,
            ),
            decided=FAILED_RETRYABLY,
            error=error,
            result_summary=result_summary,
        )

    def _write_terminal_once_more_if_refused(
        self,
        write: Callable[[dict[str, Any] | None], Any],
        *,
        decided: str,
        error: str | None,
        result_summary: dict[str, Any] | None,
    ) -> Any:
        """Run one terminal write; when Firestore refuses its document, run it
        once more with `_minimal_terminal_summary`. The mechanics
        `_finish_terminal` and `_fail_retryably_terminal` share (archived issue 1438).

        And when Firestore aborts it on contention past the transaction
        runner's own runs, the SAME write is made again, up to
        `TERMINAL_CONTENTION_ROUNDS` in all (archived issue 2650): an aborted transaction
        committed nothing, so writing it again cannot write it twice."""
        try:
            return self._write_through_contention(write, result_summary, decided=decided)
        except FencedError:
            raise
        except Exception as exc:
            if result_summary is None or not _firestore_refused_the_document(exc):
                raise
            refused = exc
        refusal = str(self._scrub(str(refused)))
        self.log.error(
            "the terminal write was refused; writing the same state once more "
            "with a minimal summary",
            state=decided,
            error_type=type(refused).__name__,
            error=refusal[:SUMMARY_REFUSED_MAX_CHARS],
        )
        try:
            written = self._write_through_contention(
                write,
                _minimal_terminal_summary(decided, error, result_summary, refusal),
                decided=decided,
            )
        except FencedError:
            raise
        except Exception as again:  # noqa: BLE001
            self.log.error(
                "the minimal terminal write was refused too",
                state=decided,
                error_type=type(again).__name__,
            )
            raise refused from None
        self.log.warning(
            "the terminal state landed with a minimal summary", state=decided
        )
        return written

    def _write_through_contention(
        self,
        write: Callable[[dict[str, Any] | None], Any],
        summary: dict[str, Any] | None,
        *,
        decided: str,
    ) -> Any:
        """`write(summary)`, made again after a contention abort (archived issue 2650).

        MEASURED 2026-10-31, dev acceptance on b3d45cdc7: the mock tasks that
        failed together (exit 2, exit 76, `fail_message`) ended `FAILED
        runner_error` with no exit code and `last_error` "409 Aborted due to
        cross-transaction contention": their terminal transactions -- each
        reading the pools every admission and release writes -- were aborted
        on every one of `FirestoreTransactionRunner`'s runs, the abort went up
        as "worker crashed", and the crash path's FAILED carried its text.
        A fence and every other error are raised as they are.
        """
        rounds = 1
        while True:
            try:
                return write(summary)
            except FencedError:
                raise
            except Exception as exc:
                if rounds >= TERMINAL_CONTENTION_ROUNDS or not _terminal_write_contended(exc):
                    raise
                self.log.warning(
                    "the terminal write met Firestore contention; writing the same state again",
                    state=decided,
                    round=rounds,
                    error_type=type(exc).__name__,
                )
            # Jittered, so the writers that met each other do not meet again.
            _terminal_contention_sleep(
                TERMINAL_CONTENTION_PAUSE_SECONDS * rounds * random.uniform(0.5, 1.5)
            )
            rounds += 1

    def _safe_finish(
        self,
        state: TaskState,
        *,
        exit_code: int | None,
        error: str | None,
        end_cause: EndCause | None = None,
        extra_summary: dict[str, Any] | None = None,
    ) -> int | None:
        """The crash path's terminal write. Never raises.

        `extra_summary` is merged into `result_summary`: a spec refusal's
        `spec_check` (contract request 34) travels this way.

        Returns None, or `ExitCode.GENERATION_FENCED` when the write was
        refused because this attempt is fenced. The old version wrote FAILED
        over whatever the task was by then, including a newer attempt the
        scheduler had already admitted.

        Or the exit of this attempt's OWN end, when a park, a finish or a
        retry had committed before the crash (`_own_end_exit`, archived issue 2064): its
        lease went back in that commit, and nothing is written over it.
        """
        own = self._own_end_exit(where="crash")
        if own is not None:
            return own
        try:
            summary = self._upload_outputs()
            self._add_runner_block(summary)
            if extra_summary:
                summary = {**(summary or {}), **extra_summary}
            with self._metrics_after_the_record():
                self._finish_terminal(
                    state=state,
                    exit_code=exit_code,
                    error=self._scrub(error[:4000]) if error is not None else None,
                    result_summary=summary,
                    end_cause=end_cause,
                )
        except FencedError as exc:
            return self._stand_down(exc, where="crash")
        except Exception as exc:
            # The reconciler is the backstop: a lease with no heartbeat gets
            # reclaimed, so a worker that cannot write its own epitaph still
            # cannot strand a slot.
            self.log.exception("could not persist the terminal state", exc)
        return None

    def _cleanup(self) -> None:
        """Every step after the attempt's outcome is decided, each guarded.

        NEVER SETS THE EXIT CODE (archived issue 737). This is the `finally` of `run()`, so
        an exception out of it replaced whatever `run()` was returning: a
        `RecursionError` from `workspace_mod.destroy` on a deep tree, raised
        after the task had SUCCEEDED and given its lease back, exited the
        container 1, and Cloud Run counted 93 successful tasks in a week as
        failed executions. A step that raises is logged as one line -- the
        step and the exception's type, never its message or a stack, which
        could carry a path or a value from the agent's tree -- and the
        steps after it still run: a failed spend write must not keep the
        account or the tenant's working tree."""

        def terminate_runner() -> None:
            if self._child is not None and self._child.poll() is None:
                self._child.terminate(self.cfg.termination_grace_seconds, reason="cleanup")
                self._child.finish()

        def destroy_workspace() -> None:
            if self.ws is not None:
                # The additional checkouts were made read-only; their folders
                # get write back first, or the removal could not unlink in them.
                extrarepos_mod.release(self.ws)
                workspace_mod.destroy(self.ws)

        def remove_released_secrets() -> None:
            # The released workspace secrets (SEC-RELEASE, design §4.1): gone
            # once the runner is, on every exit -- success, failure, park,
            # cancel, fence -- before anything else in cleanup can fail.
            if self.ws is not None:
                secretrelease_mod.remove(self.ws.private)

        for step, action in (
            ("terminate_runner", terminate_runner),
            # The declared test services (TS-RUN, §5.5): stopped and their
            # data removed once the agent that used them is gone, on every
            # exit, before the cache they write to is destroyed.
            ("stop_test_services", self._stop_test_services),
            ("remove_released_secrets", remove_released_secrets),
            # The MCP relay closes, its file goes and every credential
            # reference is dropped once the runner is gone (MCP-WORKER,
            # design §4.7 step 6). The bytes go when this process exits.
            ("close_mcp_relay", self.mcp.close),
            # The TypeSafe shim stops, its file goes and the ticket is dropped
            # once the runner is gone (SEC-MIGRATE-TYPESAFE). A no-op when off.
            ("stop_typesafe_shim", self.typesafe.close),
            # THE BROWSER IS WIPED ON EVERY EXIT (live-browser §5.5): success,
            # failure, park, cancel, fence. The hand-off's own park and cancel
            # wipe earlier, before their checkpoint; this is every other exit,
            # once the agent is gone. Once per attempt either way.
            ("wipe_browser", self.handoff.close),
            # THE SPEND BACKSTOP, after the runner is gone and before the
            # workspace holding its result.json is destroyed. Every orderly
            # exit has already recorded in `_upload_outputs`; this catches the
            # two that cannot have: a crash while a runner was still alive (it
            # has only just been reaped, above) and a mid-run fence, which
            # uploads nothing. Writing a fenced attempt's spend touches only
            # its OWN attempt document -- never the lease, which is what
            # invariant 5 forbids -- exactly as the resource usage write on
            # that same path already does.
            ("collect_spend", self._collect_spend),
            ("record_spend", self._record_spend),
            # And the CPU, for the same two exits: a runner killed just above
            # was never reaped through `_stop_sampler`, so its last stretch is
            # on no attempt document yet. The live sampler is still attached,
            # so `_attempt_usage` includes it. Nothing on the fenced path
            # touches the lease by writing its own attempt document.
            ("record_cpu", self._record_cpu),
            # Jev's spend on the state routes, settled with how the attempt
            # ended -- park, cancel and crash included -- before the workspace
            # holding its ledger is destroyed (`judge.attempt.settle_at_exit`).
            # Its own attempt's rows only, never the lease.
            ("settle_jev", self._settle_jev_at_exit),
            # AFTER the child is gone and BEFORE the workspace is destroyed.
            # Giving the account back while an agent could still be making
            # calls on it would let the broker hand the same subscription to
            # another agent and count one where there are two. This is the
            # single release point for every exit path -- see
            # `_release_account`.
            ("release_account", self._release_account),
            ("destroy_workspace", destroy_workspace),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "cleanup step failed; the exit code is the task's",
                    step=step,
                    error=type(exc).__name__,
                )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# -- what the git publish calls (issue 68) -----------------------------------
#
# The git publish moved to `gitpublish.py`. These it reads from here at call
# time, and a test patches them here, so a `monkeypatch.setattr(lifecycle, ...)`
# of one still reaches `_publish_git`; nothing else in this file reads them.
open_pull_request = forge_mod.open_pull_request
commit_dirty = gitops_mod.commit_dirty
fetch_shallow_lines_from_origin = gitops_mod.fetch_shallow_lines_from_origin
fold_agent_commits = gitops_mod.fold_agent_commits
merge_branches = gitops_mod.merge_branches
mirror_worktree = gitops_mod.mirror_worktree
own_branch_tip = gitops_mod.own_branch_tip
prepare_publish_repo = gitops_mod.prepare_publish_repo

# -- what guards a publish (issue 68) ----------------------------------------
#
# Moved, unchanged, to `publish_guard.py`: the commit replay, attribution
# stripping, the credential scan of the agent's commits and final tree, the
# runner input re-check and `_unavailable_cause`. Bound here only the names
# this file, another module or a test still reads through it; the worker calls
# each through this binding, so a `monkeypatch.setattr(lifecycle, ...)` of one
# still takes effect. Read and patch every other name in `publish_guard`
# itself: a binding nothing reads lets a stale patch succeed and change
# nothing (issue 193).
UNCOMMITTED_COMMIT_SUBJECT = publish_guard_mod.UNCOMMITTED_COMMIT_SUBJECT
_carries_attribution = publish_guard_mod._carries_attribution
_ROBOT_FACE = publish_guard_mod._ROBOT_FACE
strip_attribution = publish_guard_mod.strip_attribution
SCAN_WINDOW_CHARS = publish_guard_mod.SCAN_WINDOW_CHARS
SCAN_OVERLAP_CHARS = publish_guard_mod.SCAN_OVERLAP_CHARS
ScanHit = publish_guard_mod.ScanHit
REGISTERED_SECRET_RULE = publish_guard_mod.REGISTERED_SECRET_RULE
_DiffLeakScanner = publish_guard_mod._DiffLeakScanner
_private_key_spans = publish_guard_mod._private_key_spans
_scan_diff_stream = publish_guard_mod._scan_diff_stream
is_test_path = publish_guard_mod.is_test_path
HEADERLESS_KEY_RULE = publish_guard_mod.HEADERLESS_KEY_RULE
GENERIC_CREDENTIAL_RULES = publish_guard_mod.GENERIC_CREDENTIAL_RULES
CREDENTIAL_MIN_CHARS = publish_guard_mod.CREDENTIAL_MIN_CHARS
CREDENTIAL_MIN_ENTROPY_BITS = publish_guard_mod.CREDENTIAL_MIN_ENTROPY_BITS
_looks_like_a_credential = publish_guard_mod._looks_like_a_credential
_first_leaking_commit = publish_guard_mod._first_leaking_commit
_commits_leak = publish_guard_mod._commits_leak
final_tree_leak = publish_guard_mod.final_tree_leak
CREDENTIAL_ADVICE = publish_guard_mod.CREDENTIAL_ADVICE
credential_reason = publish_guard_mod.credential_reason
credential_refusal = publish_guard_mod.credential_refusal
replay_agent_commits = publish_guard_mod.replay_agent_commits
_recheck_runner_input = publish_guard_mod._recheck_runner_input
_error_chain = publish_guard_mod._error_chain
_unavailable_cause = publish_guard_mod._unavailable_cause


#: Every `@` that could start a GitHub mention, written literally, as a
#: character reference (`&#64;`, `&#x40;`, `&commat;`, or a numeric one with
#: no semicolon, which an HTML parser still decodes), or as the fullwidth
#: `U+FF20`. OWNER DECISION, 2026-09-29 (archived pull request 259): the agent's `pr-title.txt` and
#: `pr-body.md`, and every title or body the platform opens a pull request
#: with, have a U+200D ZERO WIDTH JOINER inserted right after each such `@`,
#: in code or not, so nothing the worker publishes pages anyone and no text
#: is refused for a mention. It replaced a Markdown parser that tried to tell
#: code from prose and that three reviews each found a way past (an escaped
#: backtick, an unterminated fence, an HTML block).
#:
#: WHAT FOLLOWS THE `@`: a character a GitHub username, organisation or team
#: can start with, widened to `_` and `-`, or `&`, which could begin a
#: character reference for the name's first letter (`@&#111;ctocat`).
#: Erring towards neutralising costs an invisible character; missing one
#: pages a person.
#:
#: CHARACTER REFERENCES ARE MATCHED, NOT DECODED: GitHub decodes them before
#: it looks for mentions, so `&#64;octocat` renders as `@octocat` and pages.
#: Decoding first and re-encoding would change more of the agent's text than
#: the joiner; matching the reference and putting the joiner after it is the
#: same result with nothing else changed. A decimal or hex reference is
#: matched only where it ends (`;`, or no further digit), so `&#640;` and
#: `&#x40a;` -- other characters -- are left alone.
#:
#: AN EMAIL ADDRESS IS LEFT INTACT. GitHub's mention filter (html-pipeline's
#: `MentionFilter`, the open-source implementation GitHub.com's is derived
#: from) matches `@` only at the start of the text or after a character
#: outside ASCII `[A-Za-z0-9_]` -- `(?:^|\W)@` in a Ruby pattern, where `\W`
#: is ASCII-only -- so `ops@example.com` pages no one. The lookbehind here is
#: that same ASCII class, spelled out: Python's `\w` would also match `é`,
#: and `café@octocat` DOES page `@octocat` on GitHub (archived pull request 259 review, M3).
#:
#: IDEMPOTENT: an `@` already followed by the joiner is not followed by a
#: name character, so a second pass inserts nothing.
_MENTION_AT_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(@|\uff20|&commat;|&#0*64(?![0-9]);?|&#[xX]0*40(?![0-9A-Fa-f]);?)"
    r"(?=[A-Za-z0-9_&-])"
)

#: U+200D ZERO WIDTH JOINER. Invisible where GitHub renders it, and not a
#: character a mention can contain, so `@<U+200D>octocat` names and pages no one.
MENTION_BREAK = "\u200d"


def _neutralise_mentions(text: str) -> str:
    """`text` with a zero-width joiner after every `@` that could mention.

    Nothing else changes: removing every joiner the call added gives back
    the input exactly. See `_MENTION_AT_RE` for which `@` qualify and why.
    """
    return _MENTION_AT_RE.sub(lambda match: match.group(1) + MENTION_BREAK, text)


#: The console's agent list a pasted link opens in, and what JavaScript's
#: encodeURIComponent leaves unescaped besides `quote`'s own `_.-~`.
#:
#: THESE SHAPES ARE THE API'S, NOT THE WORKER'S. The source of every console
#: URL is `swarm_api.codec.agent_console_url` / `workflow_console_url` (which
#: follow apps/swarm-ui/src/paths.ts). The worker does not depend on swarm_api,
#: so it spells them again here, and
#: a test of the pull-request body's console links pins the two spellings equal.
_CONSOLE_AGENT_TAB = "live"
_CONSOLE_URI_COMPONENT_SAFE = "!*'()"


def pr_body_with_console_links(
    body: str,
    *,
    origin: str | None,
    task_id: str,
    workflow_id: str | None,
    enabled: bool,
) -> str:
    """`body`, with the console links appended when the platform switch is on.

    Owner decision, 2026-10-01, OFF BY DEFAULT: `enabled` is
    `WorkerConfig.pr_console_links`, a platform setting the scheduler passes
    through, never anything a caller sent. Off, or with no console origin,
    the body is returned unchanged, byte for byte. On, exactly this block is
    appended (the workflow line only when the task belongs to a workflow):

        <body>

        ---

        Console:
        - workflow: <origin>/workflows/<workflow_id, URI-encoded>
        - agent: <origin>/agents/live/<task_id>

    That is: a blank line, `---`, a blank line, `Console:`, then one
    `- <kind>: <url>` line each, every line newline-terminated.
    A Markdown list, so each bare URL autolinks on its own line.
    """
    # Imported here, not at the top: docs/ cite this module's lines by number
    # (a docs test checks the amended documents' citations), and a line added
    # above them moves every citation.
    from urllib.parse import quote as _url_quote

    base = (origin or "").strip().rstrip("/")
    if not enabled or not base:
        return body
    lines: list[str] = []
    if workflow_id:
        encoded = _url_quote(workflow_id, safe=_CONSOLE_URI_COMPONENT_SAFE)
        lines.append(f"- workflow: {base}/workflows/{encoded}\n")
    lines.append(f"- agent: {base}/agents/{_CONSOLE_AGENT_TAB}/{task_id}\n")
    return body + "\n\n---\n\nConsole:\n" + "".join(lines)


#: The agent's environment variable holding the base the publish diffs from
#: (`Worker._build_child_env`), so `python -m agent_worker.publish_scan` scans
#: the same added text the worker will. Without it the CLI guessed HEAD on a
#: `--depth 1` clone of a pinned SHA or a non-main ref, and every COMMITTED
#: secret was outside its diff (archived pull request 470's review). A description of the work,
#: never trusted for the push: the publish keeps reading `_publish_base`.
CLONE_BASE_ENV = "SWARM_CLONE_BASE"
#: The branch the runner rebases onto before publishing (SPEED-D1;
#: `prepublish.PREPUBLISH_BASE_ENV`, held equal by a pre-publish gate test).
PREPUBLISH_BASE_ENV = "SWARM_PREPUBLISH_BASE"
#: The branch the clone left HEAD on (`_start_branch`), named to the runner
#: so the pre-publish gate can hand a moved HEAD back to the agent
#: (`prepublish.START_BRANCH_ENV`, held equal by a test of the moved HEAD).
START_BRANCH_ENV = "SWARM_START_BRANCH"
#: Where the worker writes the step's signed work contract's body for the
#: runner's JC2 (`Worker._stage_work_contract`), under the attempt's work/.
WORK_CONTRACT_FILE = "work-contract.json"
#: The runner's report key holding JC2's verdict (`cliagent.run_cli_agent`),
#: and the summary key recording its delivery (`_deliver_contract_verdict`).
CONTRACT_JUDGMENT_KEY = "contract_judgment"
CONTRACT_VERDICT_SUMMARY_KEY = "contract_verdict"


#: What a leak predicate found in one window of a file's added text: the rule
#: that matched (a `swarm_redaction` rule name, or `REGISTERED_SECRET_RULE`)
#: and where the match starts. Never the value. The shared check's own class.
CredentialHit = _credentials.CredentialHit


def _first_difference(original: str, scrubbed: str) -> int:
    """Where `scrubbed` first differs from `original`: the start of the first
    registered value the scrub replaced, near enough to name its line."""
    limit = min(len(original), len(scrubbed))
    for index in range(limit):
        if original[index] != scrubbed[index]:
            return index
    return limit


def _config_seal(repo: Path) -> str | None:
    """A digest of `repo/.git/config`, read without following a link, or None
    when it is missing or is not a regular file (archived issue 346 box 40)."""
    try:
        fd = os.open(Path(repo) / ".git" / "config", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return hashlib.sha256(handle.read()).hexdigest()


# -- the tiered, path-aware publish guard (archived issue 373) ------------------------------
#
# The guard -- `_credential_in` and every helper it asks, the tiers, the
# reference shapes and the headerless-key detection -- lives in the shared
# package `swarm_redaction.credentials` (the owner, 4 November 2026), so the
# shared Jev client scans with it in swarm-api as well as here. Its comments
# there keep every owner decision and residual risk. Each name a caller or a
# test reads is bound by assignment, so a cite of this file still resolves.
_credential_in = _credentials.credential_in
CREDENTIAL_RULES = _credentials.CREDENTIAL_RULES
_IDENTIFIER_DIGITS_MAX = _credentials._IDENTIFIER_DIGITS_MAX
_PLACEHOLDER = _credentials._PLACEHOLDER
_REFERENCE_SHAPE = _credentials._REFERENCE_SHAPE
_assigns_a_literal = _credentials._assigns_a_literal
_decodes_as_a_jwt = _credentials._decodes_as_a_jwt
_ends_the_value = _credentials._ends_the_value
_is_a_reference = _credentials._is_a_reference
_is_an_identifier = _credentials._is_an_identifier
_match_counts = _credentials._match_counts
_on_a_comment_line = _credentials._on_a_comment_line
_runtime_template = _credentials._runtime_template
_shannon_bits = _credentials._shannon_bits


#: `credential_refusal`'s file, rule and line, read back out of a reason that
#: has since been wrapped (`refusing to push: ...`) and scrubbed. The path is
#: already `_reason_path`'s masked form, and nothing here reads past `line N)`,
#: where the value would be if a refusal ever carried one.
_REFUSAL_LOCATOR_RE = re.compile(
    r"the final tree adds a credential in (?P<path>.+?) "
    r"\(rule (?P<rule>[A-Za-z0-9_]+), line (?P<line>[0-9]+)\)"
)


def credential_locator(reason: str) -> str | None:
    """`rule <rule> at <path>:<line>` for a credential refusal in `reason`, or
    None when `reason` holds none. Pure.

    The `path:line rule` pair is what `scripts/publish-scan.sh` prints, so the
    author of a brief reads the refusal and the local scan the same way. The
    matched text is never part of it: `credential_refusal` never wrote it.
    """
    found = _REFUSAL_LOCATOR_RE.search(reason or "")
    if found is None:
        return None
    return f"rule {found['rule']} at {found['path']}:{found['line']}"


def _recorded_start_branch(value: Any) -> str | None:
    """A checkpoint manifest's `start_branch` when it is one the worker could
    have written there -- `DETACHED_HEAD` or a `refs/heads/` name -- else None,
    which skips the branch check exactly as a manifest without it does (archived issue 2094)."""
    if value == DETACHED_HEAD or is_branch_ref_name(value):
        return str(value)
    return None


def _read_input_as_checked(task: dict[str, Any], runner_profile: str) -> None:
    """Replace `task["input"]` with what `_recheck_runner_input` returns.

    Contract request 58. The replacement has the SAME canonical bytes as the
    stored input -- `check_inputs` only turns an integral float into an int,
    which JCS writes identically -- so the verified document still verifies;
    what changes is the type every later reader sees. A recheck that returns
    None (no stored input, or a test that bypasses it) leaves it as stored.
    """
    checked = _recheck_runner_input(runner_profile, task.get("input"))
    if checked is not None:
        task["input"] = checked


#: Rounds of one terminal write when Firestore aborts it on contention, each
#: round being the transaction runner's own `TRANSACTION_ABORTED_RUNS` (archived issue 2650).
#: Four: the abort measured on 2026-10-31 outlasted one round's five runs in
#: 3.75 s; four rounds with their pauses cover about 25 s of contention, well
#: inside a finish's `TERMINAL_CALL_SECONDS`.
TERMINAL_CONTENTION_ROUNDS = 4

#: The pause before a terminal write's next round, times the round number and
#: a jitter of 0.5-1.5.
TERMINAL_CONTENTION_PAUSE_SECONDS = 2.0

#: Module seam, so a unit test does not sleep.
_terminal_contention_sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class _DecidedTerminal:
    """The terminal transition `_finish_terminal` is writing (archived issue 2650)."""

    state: TaskState
    exit_code: int | None
    error: str | None
    end_cause: EndCause | None
    result_summary: dict[str, Any] | None


def _terminal_write_contended(exc: BaseException) -> bool:
    """Whether `exc` is Firestore aborting a transaction on contention: an
    `Aborted` (a read's or the begin's, after the runner's runs), or the
    library's "Failed to commit transaction in N attempts" `ValueError` (a
    commit's), anywhere in the chain. Either way nothing was committed."""
    try:
        from google.api_core import exceptions as core
    except ImportError:
        core = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if core is not None and isinstance(current, core.Aborted):
            return True
        if isinstance(current, ValueError) and str(current).startswith(
            "Failed to commit transaction in "
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


#: How much of Firestore's refusal the minimal summary keeps, as
#: `summary_refused` (archived issue 1438). The refusal measured on 2026-10-16 -- "400
#: Property result_summary contains an invalid nested entity." -- is under 70
#: characters; 300 keeps the property and the rule of any refusal of that
#: form and still bounds a field the next refusal's text fills.
SUMMARY_REFUSED_MAX_CHARS = 300

#: The minimal summary's `state` when the refused write was `fail_retryably`
#: (archived issue 1438). Which state the task lands in -- READY, FAILED or CANCELLED -- is
#: picked inside that write's own fenced transaction, after the summary is
#: built, so the summary records what the attempt decided instead of guessing.
FAILED_RETRYABLY = "failed_retryably"


def _firestore_refused_the_document(exc: BaseException) -> bool:
    """Whether `exc` is Firestore refusing what was written: InvalidArgument,
    a document's shape or size (archived issue 1438). Imported here, not at module level,
    as `_is_final_refusal` does: the worker's tests run without the library
    loaded for anything else."""
    try:
        from google.api_core import exceptions as core
    except ImportError:
        return False
    return isinstance(exc, core.InvalidArgument)


def _minimal_terminal_summary(
    state: str, error: str | None, summary: dict[str, Any], refusal: str
) -> dict[str, Any]:
    """What a terminal write carries once Firestore refused its summary (archived issue 1438).

    The state, the error, the branch and the pull request when the attempt
    had them -- by their scalar fields only, so this summary holds no value
    of the shape that was refused -- and `summary_refused`, Firestore's
    reason cut to `SUMMARY_REFUSED_MAX_CHARS`. The rest of what the attempt
    produced is still in its artifacts and its final checkpoint."""

    def scalars(value: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        kept = {
            key: value[key]
            for key in keys
            if isinstance(value.get(key), (str, int, bool))
        }
        return kept or None

    git = summary.get("git") if isinstance(summary.get("git"), dict) else {}
    minimal: dict[str, Any] = {
        "state": state,
        "error": error,
        "summary_refused": refusal[:SUMMARY_REFUSED_MAX_CHARS],
    }
    branch = scalars(summary.get("branch"), ("name", "head", "head_sha", "base_sha"))
    if branch is None and isinstance(git.get("branch"), str):
        branch = {"name": git["branch"], "head": git.get("pushed_head")}
    if branch is not None:
        minimal["branch"] = branch
    pull_request = scalars(git.get("pull_request"), ("number", "url", "state", "created"))
    if pull_request is not None:
        minimal["pull_request"] = pull_request
    return minimal


def _end_cause_of(exc: BaseException) -> EndCause:
    """Why a deliberate worker failure ended its task, as the typed end cause.

    INPUTS_UNAVAILABLE for a declared input the worker refused to stage: the
    agent never started, so it is not the runner's error (archived issue 185, decision 4).
    CANNOT_START for an error that exits 78, the worker's own CANNOT-START.
    RUNNER_ERROR for every other one, which is how the outcome ledger's text
    classifier has always read a worker-written end with an exit code, so a
    task written with a cause and one written before the field existed land
    in the same class.
    """
    if isinstance(exc, InputUnavailable):
        return EndCause.INPUTS_UNAVAILABLE
    if isinstance(exc, SpecSignatureInvalid):
        return EndCause.SPEC_SIGNATURE_INVALID
    if getattr(exc, "exit_code", None) == ExitCode.CONFIG:
        return EndCause.CANNOT_START
    return EndCause.RUNNER_ERROR


def _runner_argv(cfg: WorkerConfig) -> list[str]:
    """The runner's argv comes from the FROZEN catalogue, keyed by profile name.

    Nothing from the environment and nothing from the caller contributes to it,
    which is invariant 10 enforced at the last possible moment.

    `RunnerProfile.runner_argv` is what THIS process starts as its supervised
    child. It is not, and must never be, the container's command: the
    container's command is this lifecycle (contract request 18).
    """
    argv = list(cfg.profile.runner_argv)
    program = argv[0]
    if program in ("python", "python3") and shutil.which(program) is None:
        argv[0] = sys.executable
    return argv


#: The metadata counters of the parks that REFUND their attempt: each takes
#: one off `attempt_count`, so the attempt after it reads the same number as
#: the attempt before it. A child await, a hand-off park and a provider-quota
#: park (control.py). The quota one was missing (archived issue 2650).
REFUNDED_RESUME_METADATA_KEYS = (
    CHILD_AWAIT_RESUMES_METADATA_KEY,
    handoff_mod.HUMAN_WAIT_RESUMES_METADATA_KEY,
    QUOTA_PARK_RESUMES_METADATA_KEY,
    TIME_LIMIT_CONTINUATIONS_METADATA_KEY,
)


def _refunded_resumes(task: Mapping[str, Any]) -> int:
    """How many parks of this task refunded their attempt: what `attempt_count`
    lacks to be this attempt's number."""
    metadata = task.get("metadata")
    if not isinstance(metadata, Mapping):
        return 0
    resumes = 0
    for key in REFUNDED_RESUME_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            resumes += max(0, value)
    return resumes


def _control_plane_unreachable(exc: BaseException) -> bool:
    """True for the errors a Firestore call raises when it cannot be completed.

    `GoogleAPICallError` (UNAVAILABLE, DEADLINE_EXCEEDED, PERMISSION_DENIED,
    ...) and `RetryError`, which is what the startup retry budget raises when it
    runs out. Also google-auth's own errors, for a credential that fails
    outside grpc's auth plugin. Imported here, on the failure path only, so
    that no unit test imports grpc just to construct a worker.
    """
    try:
        from google.api_core import exceptions as core_exceptions
    except ImportError:
        return False
    if isinstance(exc, (core_exceptions.GoogleAPICallError, core_exceptions.RetryError)):
        return True
    try:
        from google.auth import exceptions as auth_exceptions
    except ImportError:
        return False
    return isinstance(exc, auth_exceptions.GoogleAuthError)


def _generation_check_retryable(exc: BaseException) -> bool:
    """Whether the generation check is asked again after `exc` (archived issue 198).

    Exactly the errors `Worker._exit_control_plane_unreachable` would turn
    into 69: a Firestore or google-auth error that is either an outage
    (`_unavailable_cause`) or not a named refusal (`_refusal_cause`). A
    refusal is 78 and would be refused again. A fence or a tenant mismatch
    is not a Firestore error at all, and ends the attempt as it did.
    """
    if not _control_plane_unreachable(exc):
        return False
    return _unavailable_cause(exc) is not None or _refusal_cause(exc) is None


def _same_repository(a: str, b: str) -> bool:
    """Whether two clone URLs name the same repository (the carrier's check, archived issue 453).

    Forge URLs compare by host, owner and name, case-insensitively, so
    `https://github.com/Acme/Widgets.git` and `git@github.com:acme/widgets`
    agree. Anything `forge.parse_repo` does not understand compares as text,
    less a trailing `/` and `.git`.
    """
    left, right = forge_mod.parse_repo(a), forge_mod.parse_repo(b)
    if left is not None and right is not None:
        return (left.host, left.owner.lower(), left.name.lower()) == (
            right.host, right.owner.lower(), right.name.lower()
        )
    if left is not None or right is not None:
        return False

    def bare(url: str) -> str:
        url = url.strip().rstrip("/")
        return url[: -len(".git")] if url.endswith(".git") else url

    return bare(a) == bare(b)


def _one_line(value: Any, limit: int = 600) -> str:
    """An error's text on one bounded line, for a log field."""
    return " ".join(str(value).split())[:limit]


def _unreachable_hint(exc: BaseException) -> str:
    """What an operator reading a 69 at the generation check should know (archived issue 198)."""
    text = " ".join(str(current) for current in _error_chain(exc))
    hint = (
        "On Cloud Run, Direct VPC egress can take a minute or more to pass traffic "
        "after an instance starts (Google documents it), which is what these "
        "attempts are there to outlast. If every attempt failed, the network "
        "stayed down for all of them."
    )
    if "ipv6:" in text and "all addresses" in text:
        hint += (
            " 'failed to connect to all addresses' means every address gRPC resolved "
            "failed: if the DNS preflight line lists an IPv4 address, that one failed "
            "too (on 2026-09-25 the VPC flow logs showed its SYNs unanswered, "
            "docs/incidents/2026-09-25-worker-startup-network.md). The ipv6 address "
            "in it is only the last error gRPC kept: the swarm subnet is IPv4-only, "
            "so that address fails at once without leaving the instance, and a "
            "worker does not need it."
        )
    return hint


def _action_frames(exc: BaseException, limit: int = 6) -> str:
    """The worker's own frames `exc` came up through, innermost last, as one line.

    `merge.run_merge:1168 > merge._with_forge:1404 > mergeslot.renew:628`:
    names which write a worker action's outage came from (archived issue 2000). Only this
    package's modules, past `lifecycle`, and only function names and line
    numbers -- never a local or an argument, so nothing secret.
    """
    frames: list[str] = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        module = tb.tb_frame.f_globals.get("__name__", "")
        if module.startswith("agent_worker.") and module != __name__:
            frames.append(f"{module.rsplit('.', 1)[-1]}.{code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    return " > ".join(frames[-limit:])


def _refusal_cause(exc: BaseException) -> BaseException | None:
    """The error in `exc`'s chain that says the service was reached and REFUSED, or None.

    Used at step 1 only, and only once `_unavailable_cause` has found nothing.
    A refusal there is a 78, which fails the TASK with no retry (owner,
    2026-09-25), so this is a list of the answers that the next attempt would
    get too, and nothing else:

      * PERMISSION_DENIED and 403, UNAUTHENTICATED and 401: this worker's
        identity may not read its own task, or was not accepted;
      * NOT_FOUND: the database, or the project, is not there;
      * INVALID_ARGUMENT, FAILED_PRECONDITION, OUT_OF_RANGE and 400: the
        request, or the project's setup (a disabled API), is wrong;
      * a RefreshError google-auth did not mark retryable, and
        DefaultCredentialsError: the credential was refused, or there is
        none. One raised FROM a TransportError never gets here, because
        `_unavailable_cause` finds the TransportError first.

    Everything else at step 1 is a 69 and is retried: a kind this list does
    not name, a new one a library adds, a 409. The review of archived pull request 59 found
    gRPC UNKNOWN ending tasks because the rule was the other way round: 69
    for a named list, 78 for the rest. A wrong 69 costs one bounded retry
    (`max_attempts`). A wrong 78 ends a task that would have run.
    """
    try:
        from google.api_core import exceptions as core
    except ImportError:
        return None
    refusals: tuple[type[BaseException], ...] = (
        core.Forbidden,  # PermissionDenied is a subclass
        core.Unauthorized,  # Unauthenticated is a subclass
        core.NotFound,
        core.BadRequest,  # InvalidArgument, FailedPrecondition, OutOfRange
    )
    try:
        from google.auth import exceptions as auth
    except ImportError:
        auth = None
    if auth is not None:
        refusals = refusals + tuple(
            kind
            for kind in (
                getattr(auth, "RefreshError", None),
                getattr(auth, "DefaultCredentialsError", None),
            )
            if isinstance(kind, type)
        )
    for current in _error_chain(exc):
        if isinstance(current, refusals) and not getattr(current, "retryable", False):
            return current
    return None


def _config_backend(config: Any) -> str | None:
    """The backend this worker runs on (`WorkerConfig.backend`), or None when
    it cannot be said: the TypeSafe forwarder is then not used (fail closed)."""
    try:
        backend = config.backend
    except Exception:  # noqa: BLE001 -- an unresolvable profile is no forwarder
        return None
    return backend if isinstance(backend, str) else None


#: The GKE attempt's `<namespace>/<job>`, rendered into the Job by
#: `scheduler.dispatch.GkeJobDispatcher._manifest` (GKE_EXECUTION_NAME_ENV
#: there; a test that records the GKE execution name holds the two
#: spellings together, as this image cannot import the scheduler).
EXECUTION_NAME_ENV = "SWARM_EXECUTION_NAME"


def _execution_name() -> str | None:
    """The name this attempt's execution is recorded under.

    EXECUTION_NAME_ENV FIRST (archived issue 1804). A GKE Job sets none of the Cloud Run
    names, so a GKE worker fell through to HOSTNAME -- its POD's name,
    `swarm-<task>-<gen>-<5 chars>` -- and `record_attempt_start` wrote it over
    the dispatcher's `<namespace>/<job>`: the gate then asked the cluster for a
    Job of the pod's name and called a live agent gone, and the reconciler's
    cancel probe could not read it at all. The dispatcher's own string is the
    one every reader looks up. Cloud Run sets no EXECUTION_NAME_ENV and is
    unchanged.
    """
    for name in (EXECUTION_NAME_ENV, "CLOUD_RUN_EXECUTION", "K_REVISION", "JOB_NAME", "HOSTNAME"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else {"value": data}


# -- what an attempt used: token counts and cost (issue 68) ---------------------------
#
# Moved, unchanged, to `usage.py`. Bound here only the names a test still
# reads through it: `_add_spend`, `_stream_usage` and `_usage_summary`. The
# worker's spend methods (`spend.py`) import theirs from `usage`; read every
# other name in `usage` itself; a binding nothing reads let a stale
# `monkeypatch.setattr(lifecycle, ...)` succeed and change nothing (issue 193).
_add_spend = usage_mod._add_spend
_stream_usage = usage_mod._stream_usage
_usage_summary = usage_mod._usage_summary


def _workspace_label(ws: workspace_mod.Workspace, path: Path) -> str:
    """`artifacts/x.png`, `logs/stdout.log` -- a path a reader can place.

    Relative to the attempt's workspace, so the attempt id and the pod's
    filesystem layout stay out of a document every reader of the task sees.
    """
    try:
        return path.relative_to(ws.root).as_posix()
    except ValueError:
        return path.name


#: BROWSER-USE-FLAG (part of archived issue 2322): the `result_summary` keys the worker
#: writes whether a browser runtime was used under. `browser_used` is true,
#: false, or null -- null when the attempt had no browser (not a live-browser
#: profile) or its counts could not be read -- and swarm-api serves it on the
#: task read and the list row (`codec.task_to_api`) and sums the unused
#: browser seconds over a range (`outcomes`). Not a frozen-contract field: the
#: summary is where the API already serves per-attempt facts.
BROWSER_USED_SUMMARY_KEY = "browser_used"
BROWSER_USE_SUMMARY_KEY = "browser_use"
#: How long a live-browser agent runs with no browser call before the one
#: `browser_unused` mark goes on its events (BROWSER-USE-FLAG, item 4).
BROWSER_IDLE_SECONDS = 600


def _count_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def browser_use_record(live: bool, counts: Any, seconds: float | None) -> dict[str, Any]:
    """The `result_summary` keys for whether this attempt used its browser. Pure.

    `live` is whether the attempt ran on a live-browser profile, `counts` the
    runner's `browser_use` report (`claude_code.browser_use`), or None when it
    wrote none, and `seconds` how long the agent ran on that runtime.

    Not live: `browser_used` null and nothing else, so a claude-code task
    reads "not a browser runtime" rather than "unused". Live: used when the
    agent made any browser call or produced any image; unused when the stream
    was read whole and shows neither; null when no counts were reported, the
    stream could not be read, or a cut stream shows nothing -- a lower bound
    of 0 is not a measurement. `browser_use` carries the counts beside it.
    """
    if not live:
        return {BROWSER_USED_SUMMARY_KEY: None}
    report = counts if isinstance(counts, dict) else {}
    calls = _count_or_none(report.get("tool_calls")) if report.get("read") is True else None
    images = _count_or_none(report.get("images"))
    complete = report.get("complete") is True
    if (calls or 0) > 0 or (images or 0) > 0:
        used: bool | None = True
    elif calls == 0 and images == 0 and complete:
        used = False
    else:
        used = None
    detail: dict[str, Any] = {
        "tool_calls": calls,
        "mcp_calls": _count_or_none(report.get("mcp_calls")) if calls is not None else None,
        "command_calls": _count_or_none(report.get("command_calls")) if calls is not None else None,
        "images": images,
        "complete": complete,
        "seconds": None if seconds is None else round(float(seconds), 1),
    }
    if used is None:
        detail["unknown_because"] = (
            "not_reported" if not report else
            "stream_unread" if report.get("read") is not True else
            "stream_cut"
        )
    return {BROWSER_USED_SUMMARY_KEY: used, BROWSER_USE_SUMMARY_KEY: detail}


def _truncate_json(value: Any, limit: int) -> Any:
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "preview": encoded[:limit]}


def _tail_text(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]


def _runs_summary(runs: list[dict[str, Any]], swaps: list[dict[str, Any]]) -> dict[str, Any]:
    """`runs`, `swaps` and `swap_cost` for a summary, or {} (SWAP-RUNS, archived issue 2457).

    AN ATTEMPT OF ONE RUN GETS NOTHING NEW: {} when it started one runner and
    swapped nothing, so its summary is exactly what it was before these keys
    existed. `swap_cost` sums the `reread` of every run that followed a swap:
    what the swaps cost the attempt in seconds and tokens before the agent
    acted again.
    """
    if len(runs) <= 1 and not swaps:
        return {}
    out: dict[str, Any] = {
        "runs": [dict(run) for run in runs],
        "swaps": [dict(swap) for swap in swaps],
    }
    measured = [
        run["reread"] for run in runs
        if isinstance(run.get("reread"), dict) and "seconds" in run["reread"]
    ]
    if measured:
        tokens: dict[str, int] = {}
        for reread in measured:
            for key, value in (reread.get("tokens") or {}).items():
                tokens[key] = tokens.get(key, 0) + int(value)
        # A re-read whose seconds are unknown is skipped, not summed as 0, and
        # the cost says `partial` (issue 56).
        seconds = [float(r["seconds"]) for r in measured if _is_seconds(r.get("seconds"))]
        out["swap_cost"] = {
            "reread_seconds": round(sum(seconds), 3) if seconds else None,
            "reread_tokens": tokens,
            "measured_runs": len(measured),
            "reached_action": sum(1 for r in measured if r.get("reached")),
        }
        if len(seconds) < len(measured):
            out["swap_cost"]["partial"] = True
    return out


def _is_seconds(value: Any) -> bool:
    """A measured duration: a number, not a bool, not None."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _runs_duration(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """An attempt's agent time from its runs (SWAP-RUNS, archived issue 2457).

    The sum of the runs whose `duration_s` was measured. A run whose duration
    is unknown (`None`: closed with no result and no child, or its close
    failed) is left out and the total carries `duration_partial: true`
    (issue 56); summing it as 0.0 silently shortened the total. None when no
    run was measured.
    """
    known = [float(run["duration_s"]) for run in runs if _is_seconds(run.get("duration_s"))]
    out: dict[str, Any] = {"duration_seconds": round(sum(known), 3) if known else None}
    if len(known) < len(runs):
        out["duration_partial"] = True
    return out


def _rfc3339_now() -> str:
    """Now, UTC, to the second, as RFC 3339 (`2026-09-25T10:00:00Z`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_tail(handle: Any, *, size: int, window: int) -> tuple[int, bytes]:
    """The last `window` bytes of a stream `size` bytes long, starting on a whole line.

    Returns `(start, chunk)`, where `start` is the RAW byte offset of the first
    byte of `chunk` in the stream -- what the tail header calls `offset`.

    READ TO `size`, NOT TO EOF. The stream is still growing while it is read,
    and the header states `size` from the `stat` taken first. Reading to EOF
    made the window longer than `size - start`, so an offset derived from its
    length pointed before the bytes it described.

    WHOLE LINES. When the stream is longer than the window, the window would
    start mid-line, and for an NDJSON stream (claude-code's stdout) that first
    fragment is a line that parses as nothing. So the start moves past the
    first newline -- unless the byte just before the window already ends a
    line, or nothing would be left after it: a window that lies entirely
    inside one long final line keeps that partial line, because the newest
    output is the point of a tail and an empty one would hide it.
    """
    start = max(0, size - window)
    previous = b""
    if start > 0:
        handle.seek(start - 1)
        previous = handle.read(1)
    else:
        handle.seek(0)
    chunk = handle.read(size - start)
    if start > 0 and previous != b"\n":
        cut = chunk.find(b"\n")
        if 0 <= cut < len(chunk) - 1:
            start += cut + 1
            chunk = chunk[cut + 1 :]
    return start, chunk
