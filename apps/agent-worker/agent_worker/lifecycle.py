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
    5c. link `work/artifacts` to `artifacts/`, unless the name is taken (#149)
    6.  resolve the tenant's provider credential
    7.  start the runner child
    8.  while it runs: heartbeat, MANDATORY periodic checkpoint, watch for
        cancellation / quota exhaustion / a generation change
    9.  capture stdout and stderr
    10. upload artifacts and a final checkpoint; for a CLI agent with no
        repository, also what it created in its working folder, under
        `workdir/` (#184)
    11. persist the terminal state, or READY for a retry when a clean run left
        out an expected output and the task has attempts left (#149)
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

**Step 1's read is asked again before it gives up (#198).** A Cloud Run
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
import fnmatch
import functools
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Sequence

from swarm_common.models import EndCause, ProviderState, retries_exhausted, utcnow
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    InputRefused,
    WorkerAction,
    check_inputs,
)
from swarm_common.states import EventType, ParkReason, TaskState
from swarm_redaction import KEY_VALUE as CREDENTIAL_KEY_VALUE
from swarm_redaction import RULES as CREDENTIAL_RULES

from . import artifact_manifest as manifest_mod
from . import children as children_mod
from . import continuation as continuation_mod
from . import expected_outputs as expected_mod
from . import inputs as inputs_mod
from . import issue as issue_mod
from . import redact as redact_mod
from . import standalone_outputs as standalone_mod
from . import verdict as verdict_mod
from . import workspace as workspace_mod
from .accountlease import (
    ACCOUNT_TOKEN_ENV,
    ACCOUNT_UNREADABLE,
    BROKER_REFUSED,
    NO_RECENT_READING,
    POOL_PAUSED,
    AccountBroker,
    AccountUnreadable,
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
    NoAccountAvailable,
    ReadingForwarder,
    credential_env_from_account,
)
from .checkpoint import (
    ARCHIVE_NAME,
    MANIFEST_NAME,
    CheckpointManager,
    CheckpointRecord,
    checkpoint_prefix,
)
from .config import WorkerConfig
from .control import (
    CHECKPOINT_DIGESTS_FIELD,
    CHILD_AWAIT_RESUMES_METADATA_KEY,
    ControlPlane,
    ControlSignals,
)
from .errors import (
    CheckpointError,
    ConfigError,
    ControlPlaneError,
    ExitCode,
    FencedError,
    FencedWriteRefused,
    InputUnavailable,
    SpecSignatureInvalid,
    TenantMismatchError,
    WorkerError,
)
from . import forge as forge_mod
from . import merge as merge_mod
from . import post_verdict as post_verdict_mod
from . import specverify
from .forge import ForgeError, probe_repository, open_pull_request
from .gitops import (
    ATTRIBUTION_MARKERS,
    EMPTY_CLONE_BASE,
    GitError,
    GitTransient,
    MergeOutcome,
    clone_at_commit,
    commit_dirty,
    commit_tree_onto,
    fetch_branch_tip,
    fold_agent_commits,
    hide_from_git,
    merge_branches,
    mirror_worktree,
    prepare_publish_repo,
    push_branch,
    read_agent_excludes,
    retry_clone,
    shallow_clone,
    summarize_work,
    verify_worker_authorship,
)
# The helpers every function in `gitops` builds its git commands from. Borrowed
# rather than restated for `replay_agent_commits` below, so its commands carry
# exactly the hook, fsmonitor, signing and identity overrides the fold's do.
from .gitops import (
    _NO_HOOKS,
    _SHA_RE,
    _git_stream,
    _git_text,
    _git_text_full,
    _worker_identity,
)
from .hardening import FAILED, MemoryProtection
from .metrics import (
    ResourceSampler,
    ResourceUsage,
    attempt_cpu_fields,
    cgroup_cpu_limit_cores,
    combine_usage,
)
from .objectstore import ObjectStore
from .procman import ChildProcess, ChildResult, reap_foreign_processes
from .quota import QuotaDecision, decide, read_runner_signal, signal_from_control
from .runners.base import EXIT_QUOTA_EXHAUSTED, EXIT_TERMINATED, SPEND_KEYS
from .runners.cliagent import (
    ACCOUNT_MOVE_ENV,
    ACCOUNT_STREAM_ENV,
    RESUME_SESSION_ENV,
    STOP_DRAIN,
    STOP_EXHAUSTED,
)
from .runners.limits import GRACE_ENV, STDERR_ENV, STDOUT_ENV, TIMEOUT_ENV
from .runners.streams import agent_stream_files, cli_agent_spec
from .secrets import (
    GIT_PROVIDER,
    CredentialMissing,
    SecretError,
    SecretManagerClient,
    load_tenant,
    resolve_credentials,
    resolve_git_token,
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
#: eligible again (#149). Zero: the agent left a file out, and nothing about
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
#: The retryable sibling: the forge could not be asked (a network failure, a
#: 429, a 5xx), which the next attempt may not meet.
FORGE_UNREACHABLE = "forge_unreachable"
#: How long a task failed for an unreachable forge waits before it is eligible
#: again. A forge outage clears with time, unlike a missing output, so a short
#: delay keeps the retry from meeting the same outage at once. Bounded by
#: `max_attempts`, like every retry.
FORGE_UNREACHABLE_RETRY_DELAY_SECONDS = 60
#: The answers `forge.probe_repository` turns into a `can_push=False` access
#: whose reason starts "the forge answered <status>", and which say the forge
#: could not answer now rather than that the token cannot push. Matched on
#: that reason because the probe returns no status; the wording is pinned by
#: tests/unit/worker/test_carrier_branches.py through the real probe.
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

#: One heartbeat event per this many lease heartbeats. The lease is refreshed
#: every interval; the event stream would be unreadable at that rate.
HEARTBEAT_EVENT_EVERY = 5

#: `work/repo`. Spelled once, in `workspace`, because the checkpoint needs it
#: too: the `./artifacts` link inside the checkout is never archived (#226).
REPO_DIR_NAME = workspace_mod.REPO_DIR_NAME

#: Worker-owned scratch INSIDE the checkpointed work directory. It has to be
#: inside `work/` so a resumed attempt still knows which commit its clone
#: landed on, and outside `work/repo/` so nothing it holds can turn up in the
#: agent's own diff.
WORKER_STATE_DIR = ".swarm"
CLONE_BASE_FILE = "clone-base"
PATCH_NAME = "swarm-work.patch"

#: The checkpoint label, and the cause on the attempt's document, of an attempt
#: that left because Firestore stayed unreachable while its agent ran (#70).
CONTROL_PLANE_OUTAGE = "control_plane_outage"

#: What `builds_on` may be (#264): a task id as `swarm_common.models.new_id`
#: mints them, and nothing that could make the derived branch name a path
#: (`..`, `/`) or an option (a leading `-`).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: How `forge.open_pull_request` words the forge's own refusal of a pull
#: request (its 422 with no open one to reuse), as opposed to a forge that
#: could not be reached. The publish reads it to tell a refusal, which fails a
#: pull-request step (`_published_nothing`), from an outage, which does not.
#: Held to the forge's wording by tests/unit/worker/test_published_nothing.py.
PULL_REQUEST_REFUSED = "the forge refused the pull request"

#: The `result_summary.git` key `_publish_git` sets when the forge stayed
#: unreachable past the in-process retry (`forge.retry_transient`) while
#: probing the repository or opening the pull request (F1, 2026-10-04). Read
#: by `_publish_unreachable`: a pull-request step that ends with it fails its
#: ATTEMPT retryably rather than SUCCEEDING with no pull request -- the shape
#: that turned release 37017777271's acceptance red on 2026-10-02.
PUBLISH_UNREACHABLE_FIELD = "forge_unreachable"

#: The files an agent leaves in `$SWARM_ARTIFACTS_DIR` to write its own pull
#: request's title and body (#214), so a platform pull request can say what it
#: closes. Read without following a link, scrubbed, bounded, and refused -- in
#: favour of the generated text -- on attribution; see
#: `Worker._agent_pull_request_text`.
PR_TITLE_FILE = "pr-title.txt"
PR_BODY_FILE = "pr-body.md"
#: One line, at most this many characters: GitHub's own title field is 256.
PR_TITLE_MAX_CHARS = 256
#: At most this many bytes of the agent's body, before the platform's block. A
#: GitHub body holds 65,536 characters; the platform's block has to fit too, so
#: the agent's part is cut here rather than letting the forge refuse the lot.
PR_BODY_MAX_BYTES = 60 * 1024
#: How much of either file is read at all. Past this the text is cut anyway.
PR_READ_LIMIT_BYTES = 256 * 1024

#: The subjects of the worker's own commits -- the one holding what the agent
#: left uncommitted, and the one a fold makes -- when the agent wrote no usable
#: `pr-title.txt`. With one, that title is the subject instead
#: (`Worker._worker_commit_message`). NEVER THE TASK ID (#361): the subject is
#: the line a reviewer and `git log --oneline` read, and the owner's rule for
#: pull request titles (2026-09-28) holds for it too. The task id goes in the
#: body, where the provenance belongs.
UNCOMMITTED_COMMIT_SUBJECT = "Commit the changes the agent left uncommitted"
FOLDED_COMMIT_SUBJECT = "Everything the agent changed, as one commit"

#: The retired generated title's shape, `[swarm] task_...`, in any case. A
#: title that matches is treated as carrying the task id even when the id in
#: it is not this task's (an agent that copied another task's old title).
_RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)

#: One path segment of a checkpoint's key, as a manifest's `attempt_id` and
#: `checkpoint_id` must be before a restore builds a key from them (#347). A
#: manifest is bucket data: an id carrying `/` or `..` would name a key that
#: starts with this task's prefix and leaves it.
_KEY_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


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

#: The most agent commits the publish keeps one by one (#242). Each costs two
#: git processes; past this many the agent's history is folded into one worker
#: commit as it was before, and the run result says which happened.
MAX_KEPT_COMMITS = 200
#: The most bytes of one agent commit message kept, after it is cleaned.
COMMIT_MESSAGE_MAX_BYTES = 16 * 1024

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
    """`_Upload.skipped`, as the cause the missing-output check reads (#165).

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


@dataclass
class Outcome:
    exit_code: int
    state: TaskState | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class _CloneUnreachable(WorkerError):
    """The clone's forge did not answer through every in-process try (#623).

    Raised by `_maybe_clone` and turned by `_prepare` into a retryable end of
    the attempt (`_fail_clone_unreachable`). A `WorkerError`, so a caller
    that does not tell it apart still fails deliberately rather than crashing.
    `reason` is already scrubbed.
    """

    def __init__(self, reason: str, tries: int) -> None:
        super().__init__(f"repository clone failed: {reason}")
        self.reason = reason
        self.tries = tries


class Worker:
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
        # Which window a SIGTERM lands in. `_on_signal` reads it; `run` and
        # `_execute` set it, always through `_signal_policy` so that it is
        # restored however the window is left.
        self._signal_mode = _SIGNAL_FLAG
        # The first SIGTERM or SIGINT that arrived in the startup window, kept
        # here as well as raised. See `_execute` for why the record, not the
        # exception that reaches it, decides the exit.
        self._startup_interrupt: StartupInterrupted | None = None
        self._child: ChildProcess | None = None
        # How the publish path makes sure no process the agent started is alive
        # before the tenant credential is used (`_publish_git`). A callable, not
        # a direct call, so a unit test can substitute a scoped reaper: the real
        # one runs `os.kill(-1, SIGKILL)`, which in a shared test process would
        # kill the test runner. Returns the PIDs still alive after the reap --
        # empty means clean, non-empty means refuse to publish.
        self.reap_before_publish: Callable[[], tuple[int, ...]] = self._default_reap
        # A worker action's seams (#295): the forge transport (None is the
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
        self._metrics_exported = False
        self._last_checkpoint: CheckpointRecord | None = None
        self._restored_from: CheckpointRecord | None = None
        self._repo_url: str | None = None
        self._task: dict[str, Any] | None = None
        #: An `input.issue` number's title, when something upstream of the pull
        #: request title (#265's own fetch, once it lands) already read it.
        #: Nothing sets this yet -- #265 is landing separately and had not, as
        #: of this change, shipped the fetch -- so it stays None and
        #: `_title_from_issue_input` names the work by the issue number,
        #: always as "part of #N", never "Fixes #N".
        self._issue_title: str | None = None
        self._clone_base: str | None = None
        #: The commit this attempt's clone checked out, recorded as
        #: `result_summary.git.clone_commit` (#295, merge-step.md §3).
        self._clone_commit: str | None = None
        # The clone base the PUBLISH trusts, which is not always the one above.
        # `_clone_base` may come from `work/.swarm/clone-base`, a file in the
        # agent's HOME (its working directory's parent, when it starts in the
        # checkout -- #226), which it can write; that is good enough to describe the
        # work and not good enough to decide what reaches a forge. This one is
        # only ever the worker's own knowledge: the commit its clone landed on
        # in this process, or the base a previous attempt's worker recorded in
        # the checkpoint manifest. `EMPTY_CLONE_BASE` for an empty repository.
        self._publish_base: str | None = None
        # What `metadata.input_from` put in the workspace. Kept so the result
        # summary can report which upstream artifact this attempt actually ran
        # on -- the question every debugging of a wrong workflow output starts
        # with, and one the workspace cannot answer because it is destroyed.
        self._staged_inputs: list[inputs_mod.StagedInput] = []
        # The verdict this step's gate read (#264), as `result_summary` and the
        # pull request report it; None for a step with no gate.
        self._verdict: dict[str, Any] | None = None
        # What this task's dependants will stage from it, per
        # `metadata.expected_outputs` (#149). Kept so `_finalise` can name any
        # the attempt did not upload; see `agent_worker.expected_outputs`.
        self._expected_outputs: tuple[str, ...] = ()
        # Every file in the artifacts folder the last `_upload_outputs` did not
        # upload for the file cap or the name bound (#228), by its WHOLE name.
        # The summary counts them and does not list them, so a declared output
        # among them is named here as written and not uploaded rather than as
        # never written (`_missing_causes`).
        self._artifacts_not_uploaded: tuple[str, ...] = ()
        # The same names, each with its cause (#165): the file cap is `cap`,
        # the name bound `refused`.
        self._not_uploaded_causes: dict[str, str] = {}
        # A CLI agent's task with no repository has what its agent CREATED in
        # the working folder uploaded, under `workdir/` (#184, owner decision
        # of 2026-09-26; `agent_worker.standalone_outputs`). `_standalone` is
        # decided in `_prepare`, once the task is known. `_restored_workdir` is
        # what a checkpoint brought back into `work/`. `_workdir_baseline` is
        # what was there just before the attempt's FIRST runner started,
        # less what an earlier attempt's agent created; None until then, and
        # None means nothing is uploaded from the working folder.
        self._standalone = False
        self._restored_workdir: frozenset[str] = frozenset()
        self._workdir_baseline: frozenset[str] | None = None
        self._heartbeats = 0
        # When the last beat that returned started (monotonic), from whichever
        # thread made it: the beat thread under a long operation counts its
        # next beat from here, not from its own start (#426).
        self._last_beat_started: float | None = None
        self._deadline = time.monotonic() + config.timeout_seconds
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
        # THE ACCOUNT MID-RUN (S13-S15), for an attempt that holds one. The
        # session id the runner's stream named, kept to `--resume` it under
        # another account and NEVER logged (it is registered as a secret);
        # the session the next runner start continues, once a swap set it;
        # the forwarder for the held account's readings; the last turn count
        # the hold's move mark was asked at, so it is asked once per turn;
        # the moves so far; and whether the last start followed a swap.
        self._session_id: str | None = None
        self._resume_session: str | None = None
        self._readings: ReadingForwarder | None = None
        self._turns_checked = 0
        self._channel_seen: int | None = None
        self._swaps = 0
        self._just_swapped = False
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
        # Set on the tenant-mismatch exit, the one path that must write NOTHING
        # -- not even spend onto what may be another tenant's attempt.
        self._writes_forbidden = False
        # The publish `_finalise` defers until the missing-output check has
        # passed (#165): what `_harvest_git(defer=True)` left for
        # `_publish_checked`. None when there is nothing to publish.
        self._deferred_publish: dict[str, Any] | None = None
        # Every name the last upload skipped, with its cause (#165), uncut.
        self._skip_causes: dict[str, str] = {}
        # Until when (monotonic) the lease this worker last extended is live
        # (#70): the last heartbeat that landed, plus the extension it gave.
        # A mid-run Firestore call that fails before then is logged and the
        # loop goes on; one that fails after it is an outage, and the worker
        # leaves through `_exit_control_plane_outage`. None until a beat lands.
        self._lease_live_until: float | None = None
        # Set on every fenced exit (`_exit_fenced`, `_exit_fenced_mid_run`,
        # `_stand_down`). The task's event stream is then a newer generation's,
        # so `_give_back` returns the account and writes no `account_released`
        # into it (#380).
        self._fenced_exit = False
        # Set by `_exit_control_plane_outage` (#70): Firestore is what could not
        # be reached, so no event is attempted on the way out.
        self._control_plane_down = False
        # `carrier: branches` (D13): the branch and head the last push of this
        # step's work landed, `{"name", "head"}`, or None before any.
        self._carrier_pushed: dict[str, str] | None = None
        # The workflow base pin (`_upstream_base_pin`): what this step's clone
        # started from and why, `result_summary.git.base_pin`. None for a root
        # step, a non-workflow task, a step that starts from an upstream
        # branch, and an attempt resumed from a checkpoint (its clone is not
        # made again, so nothing is decided again).
        self._base_pin: dict[str, Any] | None = None
        # What `record_cpu_usage` last wrote onto the attempt (contract request
        # #15), so the periodic readings and each runner's end write once per
        # change rather than once per heartbeat.
        self._cpu_recorded: dict[str, float | str] | None = None
        # The child-task path (docs/design/child-tasks.md): the attempt key,
        # the spool and the await. Inert unless the scheduler passed a nonce.
        self.children = children_mod.ChildPath(
            config, logger=deps.logger, memory=deps.memory, api=deps.child_api
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
        if callable(prepared):
            return prepared()
        child_env = prepared
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        # What is in the working folder before the FIRST runner starts: once
        # per attempt, so an in-place restart below does not make the first
        # run's files "existing" ones (#184, standalone tasks only).
        self._take_workdir_baseline()

        # ---- STEPS 7-9: run the child, supervised -----------------------
        attempt_number = 0
        credential_reloads = 0
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
                if refusal is not None and credential_reloads < MAX_CREDENTIAL_RELOADS:
                    credential_reloads += 1
                    self.log.warning(
                        "credential refused; reloading it and restarting in place",
                        provider=refusal.get("provider"),
                        marker=refusal.get("marker"),
                        reload=credential_reloads,
                    )
                    self.control.emit(
                        EventType.RETRYING,
                        {
                            "cause": "credential_reloaded",
                            "provider": refusal.get("provider"),
                            "reload": credential_reloads,
                        },
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

        # ---- STEP 9b: the children's await (child tasks, §3.3) ----------
        awaited = self._await_children(result)
        if awaited is not None:
            return awaited

        # ---- STEPS 10-12: artifacts, checkpoint, terminal state, lease --
        return self._finalise(result)

    # ------------------------------------------------------------------
    # worker actions (#295): merge and post-verdict, no runner
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
            verify_upstream=lambda upstream, doc: specverify.verify_upstream_spec(
                doc, upstream_task_id=upstream, workflow_id=workflow_id, cfg=self.cfg
            ),
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
            # reap and every check that needs none (#219).
            read_git_token=self._read_action_git_token,
            # The VERIFIED spec's own repository: the one the merge acts on.
            repository_url=(
                task.get("repository_url")
                if isinstance(task.get("repository_url"), str) else None
            ),
        )
        self.phases.enter("worker_action")
        self.log.info("worker action: no runner is started", action=action.value)
        run = merge_mod.run_merge if action is WorkerAction.MERGE else (
            post_verdict_mod.run_post_verdict
        )
        with self._heartbeat_meanwhile(f"worker_action:{action.value}"):
            outcome = run(ctx)
        return self._end_worker_action(action, outcome)

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
        """The tenant's `-git` token, for the merge (contract request 47).

        The secret `swarm-tenant-<tenant>-git`, read by the Job's account --
        the tenant's worker account, the secret's ordinary reader -- through
        `resolve_git_token`, which registers the value with the logger's
        redaction before returning it and logs the secret's NAME only. Held in
        this process's heap and nowhere else: never written to the workspace,
        a file, an environment or an event. A tenant that has not registered
        one raises `CredentialMissing`, which parks the step at no cost.
        """
        tenant = load_tenant(self.db, self.cfg.tenant_id, call_options=self.control.call_options())
        if GIT_PROVIDER not in tenant.credentials or self.secret_client is None:
            raise CredentialMissing(self.cfg.tenant_id, GIT_PROVIDER)
        token = resolve_git_token(tenant=tenant, client=self.secret_client, logger=self.log)
        if not token:
            raise CredentialMissing(self.cfg.tenant_id, GIT_PROVIDER)
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
        summary = self._upload_outputs()
        summary["merge" if action is WorkerAction.MERGE else "verdict"] = self._scrub(
            dict(outcome.summary)
        )
        if outcome.spec_check is not None:
            summary["spec_check"] = dict(outcome.spec_check)
        self._export_metrics()
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
        self._checkpoint("child-await")
        self._upload_outputs()
        self._export_metrics()
        detail: dict[str, Any] = {
            "park_phase": "child_await",
            "asked": requested,
            "live_children": sorted(live) if live is not None else None,
        }
        if child_path is not None:
            detail["child_path"] = child_path
        self.control.park_awaiting_children(
            max_resumes=self.cfg.max_child_await_resumes, detail=detail
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _verify_spec(self, task: dict[str, Any], create_time: Any) -> None:
        """Contract request 34's check, with its log line and its legacy note.

        The spec itself is never logged: a prompt can hold anything.
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

    def _prepare(self) -> dict[str, str] | Callable[[], Outcome]:
        """Steps 2 to 6, the fence re-check and the quota preflight.

        Returns the runner's environment, or a park to make once the startup
        window has closed (see `_execute`).
        """
        cfg = self.cfg

        # ---- STEP 2: STARTING -> RUNNING --------------------------------
        self.phases.enter("record_attempt_start")
        self.control.record_attempt_start(
            backend=cfg.backend, execution_name=_execution_name()
        )
        self.phases.enter("advance_to_running")
        # STEP 2a, inside STEP 2: the child-task attempt key is registered
        # while the task is STARTING, before the agent exists (child tasks,
        # §3.2 step 3). swarm-api refuses a registration once it is RUNNING.
        # ONE STARTING event: the walk to STARTING writes none, and the
        # registration's note (no child path, and why) is its detail.
        if self.cfg.child_nonce:
            moved = self.control.advance_to_starting()
            notes: list[dict[str, Any]] = []
            self.children.register(notes.append)
            if moved or notes:
                self.control.emit(EventType.STARTING, notes[-1] if notes else None)
        self.control.advance_to_running()

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
        self.ws = workspace_mod.create(cfg.workspace_root, cfg.attempt_id)
        ws = self.ws
        self.log.info("workspace created", path=str(ws.root))

        # ---- STEP 4: fetch the task and VERIFY ITS SPEC -----------------
        # Contract request 34 (#342). Immediately after the fetch and before
        # anything reads the document for what to do: the checkpoint restore,
        # the clone (and its git token), the staged inputs, the child's
        # credentials and the issue. A phase of its own, so the phase log
        # proves the order. A refusal raises `SpecSignatureInvalid`, which
        # `_execute` ends FAILED with SPEC_SIGNATURE_INVALID, fenced.
        self.phases.enter("verify_spec")
        task, create_time = self.control.fetch_task_snapshot()
        self._verify_spec(task, create_time)

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
            _recheck_runner_input(cfg.runner_profile, task.get("input"))
            self._task = task
            self.phases.enter("stage_inputs")
            staged_inputs = self._stage_declared_inputs(task)
            return functools.partial(self._run_worker_action, action, task, staged_inputs)

        # ---- STEP 4b: restore the latest checkpoint ---------------------
        self.phases.enter("restore_checkpoint")
        # THE STORED INPUT IS CHECKED AGAIN, AT THE FIRST READ OF THE TASK
        # (contract request 32, *Preconditions*; the owner's decision on #345:
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
        # decision on #353/#345 ordering: verify the signature first, then
        # re-check the input, both on the one verified document.
        _recheck_runner_input(cfg.runner_profile, task.get("input"))
        # Kept because the publish gate, several steps later, needs the
        # caller's dispatch strategy and re-fetching it there would be a
        # second read of a document that cannot have changed. It is the
        # VERIFIED document: nothing later reads a covered field from Firestore.
        self._task = task
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
        self.phases.enter("clone")
        try:
            repo_info = self._maybe_clone(task)
        except _CloneUnreachable as exc:
            # The forge did not answer through every in-process try (#623):
            # the ATTEMPT ends retryably, after the startup window closes.
            return functools.partial(self._fail_clone_unreachable, exc.reason, exc.tries)
        # A clone is the single slowest step before the agent starts, and the
        # one most likely to vary with repository size.
        self._heartbeat()

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
        # repository task starts in the checkout (#226): the prompt names each
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
        # And in the checkout, where a repository task's agent starts (#226).
        self._link_checkout_artifacts()

        # ---- STEP 5d: the verdict gate (#264) -------------------------------
        # After staging, which put the verdict file on disk. A verdict that
        # does not name this step skips the AGENT, not the step: the step
        # still finishes and publishes, because it is the one that opens the
        # pull request. The generation is checked first, as it is before an
        # agent starts, because that publish pushes.
        if self._evaluate_verdict_gate(staged_inputs) is False:
            self.phases.enter("revalidate_generation")
            self.control.validate_generation()
            return self._finish_without_agent

        # ---- runner input -----------------------------------------------
        payload = dict(task.get("input") or {})
        if repo_info:
            payload.setdefault("repository", repo_info)
        if staged_inputs:
            # Assigned, not `setdefault`: this key describes what is actually on
            # disk right now, so a caller's own `staged_inputs` in the step input
            # must not shadow it and leave the agent reading a stale claim.
            payload["staged_inputs"] = [item.as_dict() for item in staged_inputs]
        elif "staged_inputs" in payload:
            # AND REMOVED WHEN NOTHING WAS STAGED (#226). A CLI runner now turns
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
            # A CALLER NEVER CHOOSES THE MODEL (#226, invariant 10). The API
            # refuses `input.model` on every profile that declares its inputs
            # (#213); this is the same rule for a task written before that
            # refusal shipped (every profile declares since contract request
            # 32, #218, so the API now refuses it for all of them), or a path
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
        # `cfg.model` is NOT written here any more (#226). It was, with
        # `setdefault`, so a caller's `input.model` won over the Job's MODEL.
        payload.setdefault("task_id", cfg.task_id)
        payload.setdefault("attempt_id", cfg.attempt_id)
        # This attempt's number: the task's `attempt_count`, which admission
        # increments in the lease's own transaction, so 1 on the first lease
        # and one more on every lease after, a park's next attempt included.
        # A platform record, not the workspace's: it reaches the runner
        # whether or not a checkpoint did, which is what bounds the mock's
        # simulated park (runners/mock.py, the review of #213). ASSIGNED, not
        # `setdefault`: a count a caller could set would be a bound a caller
        # could lift.
        payload["attempt_count"] = int(task.get("attempt_count") or 0)
        payload.setdefault("resumed_from_checkpoint", bool(self._restored_from))
        ws.input_path.write_text(json.dumps(payload, indent=2, default=str))

        # ---- STEP 6: tenant credentials ---------------------------------
        self.phases.enter("credentials")
        try:
            child_env = self._build_child_env()
        except CredentialMissing as exc:
            return functools.partial(self._park_credential_missing, exc)
        except NoAccountAvailable as exc:
            # The pool is this tenant's way of running and it is momentarily
            # empty. A wait, not a failure -- see `_park_no_account`.
            return functools.partial(self._park_no_account, exc)

        # ---- STEP 6b: the issue this step was pointed at (#265) -----------
        # After the credentials, so every secret this attempt holds is
        # registered before the issue's text is scrubbed. A fetch that fails
        # fails the attempt here, before the agent starts (`agent_worker.issue`).
        # A forge that did not answer is retried in-process (`_forge_retry`),
        # and if it stays down the ATTEMPT fails retryably -- never
        # INPUTS_UNAVAILABLE, which is for a forge that answered "no" (F1).
        issue_number = issue_mod.requested(task.get("input"), cfg.profile)
        if issue_number is not None:
            self.phases.enter("fetch_issue")
            try:
                issue_mod.stage_issue(
                    number=issue_number,
                    repository_url=self._repo_url,
                    token=self._git_token(),
                    refusal=self._git_token_refusal(),
                    work_dir=ws.work,
                    scrub=self._scrub,
                    logger=self.log,
                    on_request=self._heartbeat,
                    retry=self._forge_retry(),
                )
            except forge_mod.ForgeUnavailable as exc:
                return functools.partial(
                    self._fail_issue_unreachable,
                    issue_number,
                    str(self._scrub(str(exc)[:600])),
                    exc.retry_after_seconds,
                    exc.tries,
                )

        # Re-check fencing immediately before the agent starts. Cloning a large
        # repository can take minutes, and the whole point of step 1 is that
        # nothing runs under a stale generation -- including after a slow setup.
        self.phases.enter("revalidate_generation")
        self.control.validate_generation()

        # A provider that is already exhausted must not be hit again.
        self.phases.enter("quota_preflight")
        preflight = self.control.poll(cfg.provider)
        quota_signal = signal_from_control(preflight, cfg.provider)
        if quota_signal is not None:
            decision = decide(
                quota_signal,
                max_in_worker_retry_delay_seconds=cfg.max_in_worker_retry_delay_seconds,
                remaining_task_seconds=self._remaining_seconds(),
            )
            if decision.park:
                return functools.partial(self._park_for_quota, decision, source="preflight")
        return child_env

    # ------------------------------------------------------------------
    # child supervision
    # ------------------------------------------------------------------
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
        self._spend_pending = True

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
            # THE END OF EACH STREAM IS KEPT (#208). A runner's stderr ends
            # with the line it failed on, and `last_error` is the last 2,000
            # characters of it when the runner wrote no `error` of its own
            # (`_finalise`). Head-only, a stderr past its cap ended with the
            # truncation notice, and `last_error` quoted output from before
            # it. Head-only is git's rule -- a patch with its middle removed
            # must never read as one that fitted -- and a log is not a patch.
            keep_tail=True,
            # BELOW THE WORKER (#426): the agent and every process it forks
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

        now = time.monotonic()
        if self._lease_live_until is None:
            # The startup's beats landed (a failed one would have ended the
            # startup), so the lease is good for one extension from about now.
            self._lease_live_until = now + self.control.heartbeat_extension_seconds
        next_heartbeat = now + cfg.heartbeat_interval_seconds
        next_checkpoint = now + cfg.checkpoint_interval_seconds
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
                    self._deadline - time.monotonic(),
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
                except Exception as exc:
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
                next_checkpoint = now + cfg.checkpoint_interval_seconds

            # BEATEN THROUGH, LIKE THE CHECKPOINT (#426). Both leave the control
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

            if now >= next_poll:
                next_poll = now + cfg.control_poll_seconds
                try:
                    signals = self.control.poll(cfg.provider)
                except Exception as exc:
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

            if now >= self._deadline:
                self.log.error("task timeout reached", timeout_seconds=cfg.timeout_seconds)
                child.mark_timed_out()
                child.terminate(cfg.termination_grace_seconds, reason="task timeout")
                break

        result = child.finish()
        self._child_ended()
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
        """A mid-run Firestore call failed after its budget: log it; the outage, or None (#70).

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

    def _exit_control_plane_outage(self, child: ChildProcess, exc: Exception) -> Outcome:
        """Firestore stayed unreachable past the lease: checkpoint, stop, exit 69 (#70).

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
            "the control plane stayed unreachable past the lease; checkpointing, "
            "stopping the runner and exiting 69 for a requeue",
            error_type=type(exc).__name__,
            error=_one_line(exc),
            exit_code=ExitCode.UNAVAILABLE,
            then=(
                "the reconciler reads this execution's exit code and requeues the "
                "task, or fails it when its attempts are spent"
            ),
        )
        self._checkpoint(CONTROL_PLANE_OUTAGE)
        child.terminate(cfg.termination_grace_seconds, reason="control plane outage")
        child.finish()
        self._child_ended()
        self._control_plane_down = True
        try:
            self.control.record_attempt_end(
                exit_code=ExitCode.UNAVAILABLE,
                error=self._scrub(
                    f"{CONTROL_PLANE_OUTAGE}: the control plane was unreachable past "
                    f"the lease while the agent ran ({type(exc).__name__}: "
                    f"{_one_line(exc, 300)}); the attempt checkpointed and exited "
                    "for a requeue"
                ),
            )
        except Exception as write_exc:
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
            child.terminate(cfg.termination_grace_seconds, reason="cancelled")
            child.finish()
            self._child_ended()
            self._checkpoint("cancellation")
            summary = self._upload_outputs()
            self._add_runner_block(summary)
            self._export_metrics()
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=None,
                error="cancelled by request",
                result_summary=summary,
                end_cause=self.control.cancel_cause(),
            )
            return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

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
            superseded attempt, and the browser-eviction repair (PR #41)
            fences in one pass and deletes in a later one. Such a worker owns
            nothing. Every write it would make here lands on a task, a lease
            or pools that now belong to a newer generation. The old code made
            them all. It parked a task the reconciler had fenced, and it
            parked or failed a newer attempt the scheduler had already
            admitted (found by PR #41).

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
        try:
            self._checkpoint("interrupted")
            summary = self._upload_outputs()
            self._export_metrics()
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
        self._fenced_exit = True
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
        except Exception as exc:
            self.log.warning("could not stop the runner cleanly", error=f"{type(exc).__name__}: {exc}")
        try:
            self._child_ended()
        except Exception as exc:
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
        except Exception as exc:
            self.log.warning(
                "could not record the fenced exit on this attempt's document",
                error=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # finalisation
    # ------------------------------------------------------------------
    def _finalise(self, result: ChildResult) -> Outcome:
        ws = self.ws
        assert ws is not None

        self._checkpoint("final")
        # A runner that finished cleanly, by the same three tests the SUCCEEDED
        # branch below applies: the one outcome a missing expected output turns
        # into a retryable failure (#149). Anything else fails, or ends, on its
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
        # The ONLY call that may lead to a publish. The agent exited on its own
        # here; the other five call sites are parks and crashes. The harvest
        # runs and the publish WAITS (#165, owner decision 2026-09-28): it is
        # made below, only once the upload manifest has passed the check.
        summary = self._upload_outputs(defer_publish=True)
        # Before the check, which counts what is carried as present (#166).
        self._carry_parked_uploads(summary)
        # Here, where the runner ended on its own, and not on the park, cancel
        # and crash paths: a parked attempt resumes later and may still write
        # the file.
        missing = self._report_missing_outputs(summary, fails_the_attempt=ran_clean)
        self._publish_checked(
            summary,
            self._publish_withheld(ran_clean, missing=missing, title_refused=title_refused),
        )
        if self._carrier_pushed is not None:
            # `carrier: branches` (D13): where this step's work is kept for
            # the next step, by name and head, as the last push left it.
            summary["branch"] = self._scrub(dict(self._carrier_pushed))
        summary["exit_code"] = result.exit_code
        summary["duration_seconds"] = round(result.duration_seconds, 3)
        if self._verdict is not None:
            summary["verdict_gate"] = dict(self._verdict)
        self._export_metrics()

        runner_result = self._add_runner_block(summary)

        if result.timed_out:
            error = f"runner exceeded its {self.cfg.timeout_seconds}s timeout and was killed"
            self.control.finish(
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
                self.control.finish(
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
            git_summary = summary.get("git")
            leak = git_summary.get("final_tree_leak") if isinstance(git_summary, dict) else None
            if leak:
                return self._fail_for_final_tree_leak(str(leak), summary, exit_code=0)
            nothing = self._published_nothing(summary)
            if nothing is not None:
                return self._fail_for_published_nothing(nothing, summary, exit_code=0)
            unreachable = self._publish_unreachable(summary)
            if unreachable is not None:
                return self._fail_for_publish_unreachable(unreachable, summary, exit_code=0)
            # LAST before the success, so an attempt that fails above never
            # leaves a write-once verdict behind it for its retry to meet.
            unpublished = self._publish_review_verdict(summary)
            if unpublished is not None:
                return unpublished
            self.control.finish(
                state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary
            )
            return Outcome(exit_code=ExitCode.OK, state=TaskState.SUCCEEDED)

        if result.exit_code == EXIT_TERMINATED:
            self.control.finish(
                state=TaskState.CANCELLED,
                exit_code=result.exit_code,
                error="runner stopped on SIGTERM",
                result_summary=summary,
            )
            return Outcome(exit_code=ExitCode.CANCELLED, state=TaskState.CANCELLED)

        error = (runner_result or {}).get("error") or _tail_text(ws.stderr_path)
        self.control.finish(
            state=TaskState.FAILED,
            end_cause=EndCause.RUNNER_ERROR,
            exit_code=result.exit_code,
            # `last_error` is a Firestore field and a failing CLI is exactly the
            # thing that echoes its own configuration, so the tail is scrubbed.
            error=self._scrub(str(error)[:4000]) if error else f"runner exited {result.exit_code}",
            result_summary=summary,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

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
            state = self.control.fail_retryably(
                exit_code=0, error=error, cause="verdict_unpublished",
                result_summary=summary, end_cause=end_cause,
            )
            return Outcome(exit_code=ExitCode.FAILED, state=state)
        self.control.finish(state=TaskState.FAILED, exit_code=0, error=error,
                            result_summary=summary, end_cause=end_cause)
        return Outcome(exit_code=ExitCode.FAILED, state=TaskState.FAILED)

    def _carry_parked_uploads(self, summary: dict[str, Any]) -> None:
        """List what earlier PARKED attempts uploaded and this one did not (#166).

        Owner decision, 2026-09-28. A park uploads what the agent wrote so far
        and the next attempt resumes the work; but a retry starts with an empty
        artifacts folder (only `work/` is checkpointed), so a file written
        before the park and not again was absent from the finishing attempt's
        manifest, and its expected-output check and its dependants' staging
        both failed with the object sitting in the bucket.

        BY REFERENCE: `{name, bytes, uri, carried_from}`, the parked attempt's
        own object. Nothing is downloaded into the workspace or uploaded again.
        A name this attempt uploaded itself wins, and among parked attempts the
        later park wins. The parks are read off their PARKED events
        (`ControlPlane.parked_uploads`).

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
        except Exception as exc:
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
            except Exception as exc:
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

        ON EVERY TERMINAL PATH WHERE THE RUNNER WROTE ITS RESULT FILE (#361):
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
        runner_result = _read_json(ws.result_path)
        if runner_result:
            # The SAME figure `_record_spend` wrote onto the attempt (it ran
            # inside `_upload_outputs`): the attempt's total across every
            # runner it started, so the summary a human reads and the typed
            # fields a query reads cannot disagree after an in-place retry.
            usage_summary = dict(self._spend)
            summary["runner"] = self._scrub(
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
        return runner_result

    # ------------------------------------------------------------------
    # pieces
    # ------------------------------------------------------------------
    def _exit_fenced(self, exc: FencedError) -> int:
        """Emit the event and leave. No workspace, no agent, no lease change."""
        self._fenced_exit = True
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
        except Exception as emit_exc:  # the exit code is the real signal
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
        """One WARNING for a generation check that failed and will be asked again (#198).

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
        it, failed the task for good (review of PR #59). A wrong 69 costs one
        bounded retry, and a wrong 78 ends a task that would have run.

        Before 2026-09-25 both were 78, and nothing read the 78, so the
        difference cost nothing. Once 78 fails the task, a Firestore outage
        at startup would have ended the task for good.

        The error is logged whole, with its type and its cause. Before PR #57,
        every one of these looked like 300 s of nothing.

        A 69 comes after every attempt of `CONTROL_PLANE_READ_SCHEDULE_SECONDS`
        failed (#198); `attempts` and `seconds` say how many, and over how
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
            owns nothing, and PR #49's rule applies: the stale worker writes
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
        except Exception as write_exc:
            self.log.warning(
                "could not record the end of this attempt on its own document",
                error=f"{type(write_exc).__name__}: {write_exc}",
            )

    def _restore_checkpoint(self, task: dict[str, Any]) -> None:
        """Restore the checkpoint this task's own earlier attempt recorded, or nothing.

        ONLY A RECORDED CHECKPOINT, AND NEVER ON A FIRST ATTEMPT (#347, owner
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
        size caps -- still apply to whatever is accepted.
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
        files = self.checkpoints.restore(record, ws)
        self._restored_from = record
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
        is the signed step specs' work (#342).
        """
        refuse = functools.partial(
            self.log.info, "no checkpoint is restored; starting from an empty workspace"
        )
        count = task.get("attempt_count")
        # A refunded child await (child tasks, §3.3 step 5) takes one off
        # `attempt_count`, so the attempt after it can read 1 and still be a
        # resume. Counted back here; a tenant that can write the counter can
        # write `attempt_count` itself, so this adds no reach (see below).
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        resumes = metadata.get(CHILD_AWAIT_RESUMES_METADATA_KEY)
        resumes = resumes if isinstance(resumes, int) and not isinstance(resumes, bool) else 0
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count + max(0, resumes) <= 1
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
        digests = attempt.get(CHECKPOINT_DIGESTS_FIELD) if attempt else None
        if (
            attempt is None
            or attempt.get("task_id") != self.cfg.task_id
            or not isinstance(listed, list)
            or record.checkpoint_id not in listed
            or not isinstance(digests, dict)
            or digests.get(record.checkpoint_id) != record.archive_sha256
        ):
            self.log.error(
                "refusing a checkpoint no earlier attempt of this task recorded",
                attempt_id=record.attempt_id,
                checkpoint_id=record.checkpoint_id,
                attempt_document=attempt is not None,
                digest_recorded=isinstance(digests, dict)
                and record.checkpoint_id in digests,
            )
            refuse(reason="no attempt document of this task lists the checkpoint")
            return None
        return record

    def _maybe_clone(self, task: dict[str, Any]) -> dict[str, Any] | None:
        ws = self.ws
        assert ws is not None
        url = self.cfg.repository_url or task.get("repository_url")
        if not url:
            return None
        destination = ws.work / REPO_DIR_NAME
        self._repo_url = url
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
            return {
                "path": REPO_DIR_NAME,
                "from_checkpoint": True,
                "commit": self._clone_base,
            }
        # A fix step clones the branch it continues (#263, continuation.py)
        # first; failing that, a step that BUILDS ON an upstream step (#264)
        # starts from the branch that step pushed, so a fix sees the code it
        # is fixing. Either way the branch is derived from a task id with
        # this worker's own prefix, never read as a name, exactly as the
        # integrator's contributor branches are.
        #
        # A `single-pr` READER or AMENDER (#295, docs/merge-step.md §3) clones
        # the author's branch, `swarm/<pr_author>`, derived from the author's
        # task id in the signed dispatch block -- never a branch name read
        # from metadata -- and nothing below chooses for it. An author, and
        # every other strategy, is unchanged.
        pr_branch = (
            self._pr_author_branch() if self._pr_role() in ("reader", "amender") else ""
        )
        builds_on = "" if pr_branch else self._dispatch_builds_on()
        continued = "" if pr_branch else continuation_mod.clone_ref(
            task.get("metadata"), self.cfg.git_branch_prefix
        )
        # `carrier: branches` (D13): a step whose parent kept its work on a
        # branch starts from that branch, instead of from the default branch
        # with the parent's patch to apply. Asked only when nothing above
        # chose a branch, so `continues` and `builds_on` keep their meaning.
        carried = "" if pr_branch or continued or builds_on else self._carrier_parent(task)
        ref = (
            pr_branch
            or continued
            or (f"{self.cfg.git_branch_prefix}{builds_on}" if builds_on else None)
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
        # non-workflow task, which is therefore unchanged.
        pinned_sha: str | None = None
        if not (pr_branch or continued or builds_on or carried):
            pinned_sha, self._base_pin = self._upstream_base_pin(task)
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
        # terminal, at once (#623).
        retry = functools.partial(
            retry_clone,
            destination=destination,
            max_wait_seconds=self.cfg.max_in_worker_retry_delay_seconds,
            remaining_seconds=self._remaining_seconds,
            logger=self.log,
            sleep=self.forge_sleep,
            on_retry=self._heartbeat,
        )
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
                    token=None if refusal else self._git_token(),
                ))
            except GitTransient as exc:
                # Not a fall back to the branch tip: the tip is on the same
                # forge, and an unpinned clone would be a silent change of base.
                raise _CloneUnreachable(self._scrub(str(exc)[-600:]), exc.tries) from exc
            except GitError as exc:
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
                token=None if refusal else self._git_token(),
            ))
        except GitTransient as exc:
            raise _CloneUnreachable(self._scrub(str(exc)[-600:]), exc.tries) from exc
        except GitError as exc:
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
        self._repo_url = clone.url
        self._clone_base = clone.commit
        # `result_summary.git.clone_commit` (merge-step.md §3): the only record
        # of which head a reader actually saw. Known in this process.
        self._clone_commit = clone.commit
        # Known in this process, so trusted; and recorded in every checkpoint
        # manifest from here on, so a resumed attempt can trust it too.
        self._publish_base = clone.commit or (EMPTY_CLONE_BASE if clone.empty else None)
        self.checkpoints.clone_base = self._publish_base
        self._write_clone_base(clone.commit)
        info: dict[str, Any] = {
            "path": REPO_DIR_NAME,
            "url": clone.url,
            "ref": clone.ref,
            "commit": clone.commit,
        }
        if builds_on:
            info["builds_on"] = builds_on
        if pr_branch:
            info["pr_role"] = self._pr_role()
        if carried:
            info["carried_from"] = carried
        if self._base_pin is not None:
            info["base_pin"] = dict(self._base_pin)
        if refusal:
            info["git_token_refused"] = refusal
        return info

    def _fail_clone_unreachable(self, reason: str, tries: int) -> Outcome:
        """A retryable failure before the agent ran: the clone's forge did not answer.

        Measured 2026-10-05 (#623): task_943349914a88 and task_cb020e97d585
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
        self._export_metrics()
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
        repository records none).
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

    def _carrier_parent(self, task: dict[str, Any]) -> str:
        """The parent task whose pushed branch this step starts from (D13), or "".

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
        """
        if self._dispatch_carrier() != "branches" or self._dispatch_role() == "integrator":
            return ""
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
            return ""
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
            return ""
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
        return parent

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
            # `issue.md`, when the task asks for an issue (#265).
            | issue_mod.reserved_names(task.get("input"), self.cfg.profile),
        )
        self._staged_inputs = staged
        self.log.info(
            "declared inputs staged",
            count=len(staged),
            files=[item.path for item in staged],
        )
        return staged

    def _evaluate_verdict_gate(self, staged: list[inputs_mod.StagedInput]) -> bool | None:
        """Read this step's verdict gate (#264). None: no gate. True/False: run the agent or not.

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
        runs = read.verdict in gate.verdict_in
        self._verdict = {
            "task_id": gate.task_id,
            "file": source.filename,
            "verdict": read.verdict,
            "verdict_in": list(gate.verdict_in),
            "agent_ran": runs,
            "findings": list(read.findings),
            "findings_dropped": read.findings_dropped,
        }
        self.log.info(
            "verdict gate read: the agent runs" if runs
            else "verdict gate read: the agent does not run; the step still publishes",
            verdict=read.verdict,
            verdict_in=list(gate.verdict_in),
            verdict_task_id=gate.task_id,
            findings=len(read.findings) + read.findings_dropped,
        )
        return runs

    def _finish_without_agent(self) -> Outcome:
        """End a step whose verdict gate stayed shut: no agent, same ending (#264).

        Through `_finalise`, the one ending a runner that exited on its own
        gets, so the final checkpoint, the uploads, the publish, the missing-
        output check and the lease release are the ones every other clean
        attempt has. The result it reads is written here, in the runner's
        shape, saying the agent was skipped and why.
        """
        ws = self.ws
        assert ws is not None and self._verdict is not None
        self._take_workdir_baseline()
        ws.result_path.write_text(
            json.dumps(
                {
                    "status": "skipped",
                    "summary": (
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

    def _link_artifacts(self) -> None:
        """Step 5c: make `work/artifacts` the directory that is uploaded (#149).
        `work/` is the agent's working directory when the task has no
        repository; with one, `_link_checkout_artifacts` links the checkout's
        `./artifacts` too (#226), and this one is `../artifacts` from there.

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
        directory that is uploaded, and git never sees it (#226).

        With a repository attached the agent starts in `work/repo` (owner
        decision of 2026-09-26), so the guess `work/artifacts` catches (#149:
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
        publish repository (`gitops.mirror_worktree`, #259 M1), which does not
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
        except Exception as exc:  # pragma: no cover - defensive; never fails the attempt
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
                # SCRUB FIRST, THEN CUT (#232 review): `shown` cuts a long name
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
                if self.log.has_secrets:
                    entry = self._redact_file(staging, label=name, withheld=True)
                    if entry is not None:
                        # Measured to hold a registered value it could not
                        # rewrite, or not examinable at all: kept in the pod.
                        # The agent never chose to deliver this file, so the
                        # artifacts folder's "upload it as-is and say so"
                        # does not apply (#225 review).
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
                except Exception as exc:
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
        files they did not upload: the working folder's (#225 review: past 50
        a name was nowhere) and the artifacts folder's, past its 500-file cap
        (#228). A result summary lists at most the first 50 and counts the
        rest, because it is a Firestore document with a 1 MiB limit; these
        lines name all of them, and `LOG_BATCH` names to a line keep each well
        under Cloud Logging's 256 KiB entry however many files the agent left.
        Every line carries `count`, the whole number, and `batch`, "i of n".
        Nothing is written for no entries.
        """
        batch = standalone_mod.LOG_BATCH
        batches = (len(entries) + batch - 1) // batch
        for index in range(batches):
            self.log.warning(
                message,
                count=len(entries),
                batch=f"{index + 1} of {batches}",
                files=[
                    f"{e['name']}: not uploaded: {e['reason']}"
                    for e in entries[index * batch : (index + 1) * batch]
                ],
                **fields,
            )

    def _declared_outputs(self, task: dict[str, Any]) -> tuple[str, ...]:
        """Honour `metadata.expected_outputs`: what later steps will stage from this one.

        Written by the API at workflow submission, on the UPSTREAM task (#149).
        An entry that cannot name a file in the artifacts directory is dropped
        and named in a warning rather than failing the attempt: no dependant
        could stage it either. A usable name that is missing when the runner
        finishes cleanly fails the attempt, retryably
        (`_fail_for_missing_outputs`). See `agent_worker.expected_outputs` for
        the measurements behind both.
        """
        declared = expected_mod.declared_outputs(task.get("metadata"))
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
        if PR_TITLE_FILE not in names and self._title_owed(task):
            # REQUIRED, NOT INVENTED (owner decision, 2026-09-28): a pull
            # request this attempt opens is titled by the agent. Owed as an
            # expected output, a CLI runner tells the agent to write it, and a
            # clean finish without it fails the attempt retryably (#149's
            # path) instead of opening an untitled pull request.
            names = (*names, PR_TITLE_FILE)
            self.log.info(
                "this attempt opens a pull request; the agent must write its title",
                file=PR_TITLE_FILE,
            )
        self._expected_outputs = names
        return names

    def _report_missing_outputs(
        self, summary: dict[str, Any], *, fails_the_attempt: bool
    ) -> list[str]:
        """Name the expected outputs this attempt did not upload, and return them (#149).

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
            entry.get("name")
            for entry in summary.get("artifacts") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        missing = expected_mod.missing_outputs(self._expected_outputs, produced)
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

    def _missing_causes(self, summary: dict[str, Any]) -> dict[str, str]:
        """Each name the last upload did not upload, with WHY (#165).

        The skipped files' causes (`_skip_causes`: the byte cap, an upload
        error, a name or a file refused), the files the 500-file cap or the
        name bound left out (#228), and `swarm-work.patch` when the harvest did
        not write it: an empty diff, a patch over its cap, no clone base
        (`expected_mod.patch_cause`). A name in none of them was not written,
        which a retry can change; so can an upload error, and nothing else
        here (`expected_mod.retryable`).
        """
        causes = {**self._not_uploaded_causes, **self._skip_causes}
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
        """Why this attempt must not publish, or None when it may (#149, #165).

        Decided AFTER the upload, from the manifest the missing-output check
        read (`missing`), and before anything is pushed (`_publish_checked`).

        A MISSING EXPECTED OUTPUT WITHHOLDS THE PUBLISH ON EVERY ATTEMPT, the
        last one included (#165, owner decision 2026-09-28): the attempt is
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
        """Fail this attempt, retryably, with the missing names as its cause (#149).

        The owner's decision on #149 (2026-09-25T10:30Z), which replaces the
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

        NOT RETRIED WHEN A RETRY CANNOT HELP (#165, owner decision
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
            self.control.finish(
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
        state = self.control.fail_retryably(
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
        state = self.control.fail_retryably(
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

    def _fail_for_final_tree_leak(
        self, reason: str, summary: dict[str, Any], *, exit_code: int
    ) -> Outcome:
        """Fail this attempt, retryably, because the branch it would push adds a
        credential (owner decision, 2026-09-28, #259 review M4).

        `reason` comes from `final_tree_leak`: it names the file and never the
        value, so it is safe as the attempt's error, which is what tells the
        retry what to remove. Nothing was pushed (`_publish_git` returned
        before the publish repository was made). The retry resumes from the
        final checkpoint, taken before the worker's commit of uncommitted work,
        so the agent's files are there to edit.
        """
        error = self._scrub(reason)
        state = self.control.fail_retryably(
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

    def _opens_pull_request(self) -> bool:
        """True when this step's job is to OPEN A PULL REQUEST (GUARD 2, 2026-10-02).

        A `direct-pr` task or step, the `integrate` integrator, and a
        `single-pr` author. NOT an `integrate` contributor (its branch is its
        deliverable), a `single-pr` reader or amender, a `collect` task or a
        worker action: for those, changing nothing is a correct outcome -- a
        review that edits no file is a review that did its job.

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
        git = summary.get("git")
        if not isinstance(git, dict):
            return None
        refused = git.get("pull_request_refused")
        if refused:
            return f"published_nothing: {refused}"
        if (
            git.get("published") is False
            and git.get("commit_count") == 0
            and git.get("dirty_count") == 0
        ):
            base = git.get("base") or "its base"
            return (
                "published_nothing: the step was to open a pull request and its "
                f"branch has no commits beyond {base}"
            )
        return None

    def _publish_unreachable(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        """The forge outage that kept a pull-request step from opening its pull
        request, or None (F1, 2026-10-04).

        Set by `_publish_git` only for a `ForgeUnavailable` that outlasted
        `retry_transient` -- a timeout, reset, DNS failure, 429, 5xx or
        rate-limit 403 at the probe or at `open_pull_request`. A forge that
        ANSWERED (a 422 refusal, a 404, a plain 403) is not here: the refusal
        is `_published_nothing`'s, and the rest stay the publish's recorded
        reason. Only a step that owes a pull request (`_opens_pull_request`)
        is failed for it; any other step's deliverable is its branch or its
        artifacts, which an outage at the forge's API does not touch.
        """
        if not self._opens_pull_request():
            return None
        git = summary.get("git")
        if not isinstance(git, dict):
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
        """
        reason = str(marker.get("reason") or "the forge did not answer")
        tries = marker.get("tries")
        retry_after = marker.get("retry_after_seconds")
        error = self._scrub(
            f"{FORGE_UNREACHABLE}: the step was to open a pull request, and the "
            f"forge did not answer after {tries} tries: {reason}. The attempt is "
            "retried while the task has attempts left."
        )
        state = self.control.fail_retryably(
            exit_code=exit_code,
            error=error,
            cause=FORGE_UNREACHABLE,
            result_summary=summary,
            retry_delay_seconds=max(
                FORGE_UNREACHABLE_RETRY_DELAY_SECONDS,
                int(retry_after) if isinstance(retry_after, int) else 0,
            ),
            detail={"publish": "pull_request", "tries": tries},
            end_cause=EndCause.OUTPUTS_MISSING,
        )
        self.log.warning(
            "the forge did not answer at publish; failing the attempt retryably",
            cause=FORGE_UNREACHABLE,
            tries=tries,
            task_state=state.value,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

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
        self.control.finish(
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
        is in it", and it is never relaxed for a test path (#373) -- or a
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

    def _git_token(self) -> str | None:
        """The TENANT's own clone token, or None.

        Read from `swarm-tenant-<tenant>-git` through the same per-tenant Secret
        Manager path as a provider key, never from a platform-wide `GIT_TOKEN`
        in the worker's environment: one token able to clone every tenant's
        repositories would make a single malicious repository in one tenant a
        credential compromise for all of them (invariant 9).

        None is not an error. A public repository clones without a credential
        and a private one fails with git's own message, which is the correct
        diagnosis to surface.

        Nor is it read at all when `_git_token_refusal` names a reason: once
        read, the token stays in this process's heap for the rest of the
        attempt, next to an agent that may be able to read it there.
        """
        refusal = self._git_token_refusal()
        if refusal:
            self.log.error("not reading the tenant git credential", reason=refusal)
            return None
        if self.secret_client is None:
            return None
        try:
            tenant = load_tenant(
                self.db, self.cfg.tenant_id, call_options=self.control.call_options()
            )
            return resolve_git_token(
                tenant=tenant, client=self.secret_client, logger=self.log
            )
        except SecretError as exc:
            self.log.warning(
                "no usable tenant git credential; cloning unauthenticated",
                error=str(exc),
            )
            return None

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
            TIMEOUT_ENV: str(self.cfg.timeout_seconds),
            GRACE_ENV: str(self.cfg.termination_grace_seconds),
            STDOUT_ENV: str(self.cfg.max_stdout_bytes),
            STDERR_ENV: str(self.cfg.max_stderr_bytes),
        }
        # THE CHECKOUT, WHEN THERE IS ONE (#226). A CLI runner starts its agent
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
        # in the review of PR #31, not observed: the bare runner the GKE Job
        # used to start inherited the container's environment whole, and no
        # browser attempt had run under the lifecycle before that PR made it
        # the entrypoint there.
        # Its sibling PLAYWRIGHT_SKIP_BROWSER_GC is deliberately NOT carried:
        # only Playwright's browser-install path reads it (checked in the 1.63.0
        # driver), and the runner never installs.
        #
        # NEVER A PREFIX. `PLAYWRIGHT_*` would also carry
        # PLAYWRIGHT_SERVICE_ACCESS_TOKEN, a real Playwright credential.
        # tests/unit/worker/test_image_env_reaches_the_runner.py takes every ENV
        # in the browser image -- both Dockerfiles, plus the upstream python
        # base's, recorded there and pinned by digest -- and holds each one to
        # either this list or a recorded reason the runner must not have it.
        # The image's pip/npm settings are among the refused: no runner process
        # runs pip or npm. Whether the AGENTS the runners start should get them
        # is an open owner decision, recorded in that file.
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
                # the library's defaults there.
                tenant = load_tenant(
                    self.db, self.cfg.tenant_id, call_options=self.control.call_options()
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
        # The child-task spool, when this attempt has a child path: the one
        # variable an agent needs to submit and await helpers (child tasks,
        # §3.1). A path, never a credential: the attempt key stays here.
        base.update(self.children.prepare(ws.work))
        return ws.child_env(base)

    # -- the account pool --------------------------------------------------
    def _pool_credential_env(self, profile: Any) -> dict[str, str] | None:
        """The child env an assigned account supplies, or None for the tenant secret.

        THE LOOP IS THE POINT. `choose()` is deterministic, so an account this
        worker cannot read is one it would be handed again on every retry --
        and the three real ways that happens (a freshly onboarded account whose
        secret has no version yet, an account lent by a tenant that never
        granted this worker's service account access to it, an empty version)
        are all invisible to the broker, which knows about Firestore documents
        and not about IAM. So: hand the unusable one back, say WHY, ask again
        excluding it, and park if nothing works.

        Parking rather than failing, because none of this is the task's fault
        and an unusable pool must cost nothing. Failing would burn all three
        attempts on the same account in a row.
        """
        for _ in range(MAX_ACCOUNT_TRIES):
            account = self._lease_account(profile)
            if account is None:
                return None
            try:
                return self._account_credential_env(profile, account)
            except AccountUnreadable as exc:
                self._reject_account(account, exc)
        raise NoAccountAvailable(
            NoAccount(reason=ACCOUNT_UNREADABLE),
            profile.provider or "unknown",
        )

    def _lease_account(self, profile: Any) -> Assignment | None:
        """The account this attempt runs on, or None to use the tenant secret.

        None is returned -- rather than raised -- for every case that means
        "the pool is not how this runs":

          * no broker is configured, which is every deployment that has not
            adopted the pool;
          * the profile does not declare `CLAUDE_CODE_OAUTH_TOKEN`, so it wants
            a metered API key and an account has none to give;
          * the broker is unreachable or answers unusably, which must degrade
            to the behaviour that worked yesterday rather than fail a task;
          * the tenant has no account registered at all.

        `NoAccountAvailable` is raised for the cases that are genuinely a wait
        or genuinely broken: accounts exist and every one is spent, paused,
        draining or unobserved; or the broker REFUSED this worker. The caller
        parks in both cases -- an attempt is never failed over the pool.
        """
        if self._account is not None:
            # Already held. Re-reading it is what the credential-reload path
            # wants; re-ASSIGNING would double-count this agent on the pool and
            # could move it onto a different subscription halfway through a run.
            return self._account
        if self._account_declined is not None:
            # Decided once, at step 6, and not revisited. See the field.
            return None
        if self._account_broker is None:
            return self._decline_pool("no_broker_configured")
        if ACCOUNT_TOKEN_ENV not in (profile.secrets or ()):
            self.log.info(
                "runner profile does not accept a subscription token; using the "
                "tenant credential rather than the account pool",
                runner_profile=profile.name,
                declared=sorted(profile.secrets or ()),
            )
            return self._decline_pool("profile_takes_no_subscription")

        provider = profile.provider
        try:
            outcome = self._account_broker.assign(
                provider, exclude=tuple(self._account_rejected)
            )
        except BrokerRefused as exc:
            # REFUSE, DO NOT CARRY ON. A 401/403/404 means the pool is
            # configured and this worker may not use it -- almost always a
            # missing `run.invoker` grant for this tenant's worker service
            # account, or a URL that is not the broker. Falling back here would
            # put every agent back on the one shared tenant subscription while
            # the pool's own dashboards showed it healthy and idle, which is
            # the failure nobody would ever find. Parking costs nothing and
            # puts the cause in the task's own blocked_by.
            self.log.error(
                "the quota broker refused this worker; the account pool is "
                "configured and this tenant cannot use it",
                provider=provider,
                error=str(exc),
            )
            raise NoAccountAvailable(
                NoAccount(reason=BROKER_REFUSED), provider or "unknown"
            ) from None
        except BrokerUnavailable as exc:
            # DEGRADE, DO NOT FAIL. The broker being down is not the task's
            # fault and the tenant secret still works; turning an outage in a
            # control-plane service into failed attempts across every tenant is
            # a much larger incident than the one that started it.
            self.log.warning(
                "could not reach the quota broker for an account; falling back "
                "to the tenant credential",
                provider=provider,
                error=str(exc),
            )
            return self._decline_pool("broker_unreachable")
        except Exception as exc:  # pragma: no cover - defence in depth
            self.log.warning(
                "the account pool client raised; falling back to the tenant credential",
                provider=provider,
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._decline_pool("broker_client_error")

        if isinstance(outcome, NoAccount):
            if outcome.is_pool_absent:
                self.log.info(
                    "no account is registered for this tenant; using the tenant credential",
                    provider=provider,
                )
                return self._decline_pool("no_accounts_registered")
            raise NoAccountAvailable(outcome, provider or "unknown")

        self._account = outcome
        self._account_released = False
        self.log.info(
            "account assigned from the pool",
            account_id=outcome.account_id,
            provider=provider,
            # Whether this tenant borrowed it. Worth a field of its own: a
            # deployment leaning on borrowed capacity looks healthy right up to
            # the day the lender revokes the loan.
            borrowed=bool(outcome.owner_tenant and outcome.owner_tenant != self.cfg.tenant_id),
        )
        self.control.emit(
            EventType.RUNNING,
            {
                "cause": "account_assigned",
                "account_id": outcome.account_id,
                "provider": provider,
            },
        )
        return outcome

    # -- the account mid-run (S13-S15) ---------------------------------------
    def _account_channel_paths(self) -> tuple[Path, Path]:
        """(stream, move): the runner's channel, in the worker's own `private/`.

        Not in `work/`, which a checkpoint archives and the agent can read.
        """
        assert self.ws is not None
        return (
            self.ws.private / "account-stream.json",
            self.ws.private / "account-move.json",
        )

    def _account_channel_env(self) -> dict[str, str]:
        """What a runner on a held account is told: where its channel is, and
        the session to continue when the last move set one."""
        if self._account is None:
            return {}
        stream, move = self._account_channel_paths()
        env = {ACCOUNT_STREAM_ENV: str(stream), ACCOUNT_MOVE_ENV: str(move)}
        if self._resume_session:
            env[RESUME_SESSION_ENV] = self._resume_session
        return env

    def _read_account_channel(self) -> dict[str, Any]:
        stream, _move = self._account_channel_paths()
        try:
            data = json.loads(stream.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _note_session(self, session_id: Any) -> None:
        """Remember the run's session id, as a secret: never in a log or event."""
        if isinstance(session_id, str) and session_id and session_id != self._session_id:
            self.log.register_secret(session_id)
            self._session_id = session_id

    def _forward_readings(self, readings: Any, *, final: bool = False) -> None:
        account = self._account
        if account is None or self._account_broker is None or not readings:
            return
        if self._readings is None:
            broker = self._account_broker
            self._readings = ReadingForwarder(
                lambda windows: broker.report_reading(account, windows), logger=self.log
            )
        self._readings.offer(readings, final=final)

    def _watch_account(self) -> None:
        """One look at the runner's channel while it runs. Never raises.

        Forwards each window's latest reading (S15) and, once per new turn,
        asks the broker whether this hold is marked to move (S14) -- a drain.
        If it is, the move file asks the runner to stop the CLI at its next
        turn boundary; the swap itself happens when the runner has exited.
        """
        try:
            stream, move = self._account_channel_paths()
            try:
                seen = stream.stat().st_mtime_ns
            except OSError:
                return
            if seen == self._channel_seen:
                return
            self._channel_seen = seen
            data = self._read_account_channel()
            self._note_session(data.get("session_id"))
            self._forward_readings(data.get("readings"))
            turns = data.get("turns")
            if not isinstance(turns, int) or turns <= self._turns_checked:
                return
            self._turns_checked = turns
            if self._account_broker is None or self._account is None or move.exists():
                return
            status = self._account_broker.hold_status(self._account)
            if status.get("move"):
                move.write_text(json.dumps({"reason": str(status["move"])}))
                self.log.warning(
                    "this attempt's account is draining; the agent moves to another "
                    "account at its next turn boundary",
                    account_id=self._account.account_id,
                    move=str(status["move"]),
                )
        except Exception as exc:
            self.log.warning(
                "could not read the account channel or the hold's status; the run "
                "carries on as it is",
                error_type=type(exc).__name__,
            )

    def _move_account_after(self, result: ChildResult) -> dict[str, str] | Outcome | None:
        """After a runner on a held account exits: move the attempt, or None.

        None is "nothing to move": not on an account, or the run ended for a
        reason a move does not answer -- and the caller carries on exactly as
        before. A dict is the child environment to restart with `--resume`
        under the new account. An Outcome is a park.
        """
        if self._account is None or self._account_broker is None:
            return None
        data = self._read_account_channel()
        # CONSUMED: what this run said is never read again as the next run's.
        # The session id is kept in memory; the next runner writes its own.
        stream, _move = self._account_channel_paths()
        stream.unlink(missing_ok=True)
        self._channel_seen = None
        self._note_session(data.get("session_id"))
        # AT THE LATEST AT THE END OF THE RUN, every window not yet sent.
        self._forward_readings(data.get("readings"), final=True)
        stopped_for = data.get("stopped_for")
        just_swapped, self._just_swapped = self._just_swapped, False
        reason: str | None = None
        if stopped_for == STOP_DRAIN:
            reason = SWAP_DRAIN
        elif stopped_for == STOP_EXHAUSTED or result.exit_code == EXIT_QUOTA_EXHAUSTED:
            reason = SWAP_EXHAUSTED
        elif just_swapped and self._credential_refusal() is not None:
            # The account this attempt just moved to could not start the CLI.
            reason = SWAP_UNUSABLE
            assert self.ws is not None
            self.ws.credential_path.unlink(missing_ok=True)
        if reason is None:
            return None
        if self._session_id is None:
            # Nothing to resume. A rate limit goes the way it always went; a
            # drain stop with no session cannot be continued, so it parks.
            self.log.warning(
                "the run named no session to continue; not moving it",
                reason=reason,
            )
            if reason == SWAP_DRAIN:
                return self._park_no_account(
                    NoAccountAvailable(NoAccount(reason=POOL_PAUSED), self.cfg.provider or "unknown")
                )
            return None
        return self._swap_account(reason)

    def _swap_account(self, reason: str) -> dict[str, str] | Outcome | None:
        """Move the hold through the broker and rebuild the child's environment.

        ONE BROKER CALL PER MOVE (`AccountBroker.swap`), which releases this
        hold and takes another in one transaction. If the new account cannot
        be read, it moves again with reason `unusable` -- the broker marks the
        unreadable one for this tenant -- up to MAX_ACCOUNT_TRIES; then the
        account is given back unusable (`_give_back`) and the attempt parks.

        Re-checks the fence first: nothing continues under a stale generation
        (invariant 5), and the broker refuses a fenced attempt's swap anyway.
        """
        profile = self.cfg.profile
        provider = profile.provider or "unknown"
        current = self._account
        assert current is not None and self._account_broker is not None
        if self._swaps >= MAX_ACCOUNT_SWAPS:
            self.log.warning(
                "this attempt has moved accounts as often as it may; not moving again",
                swaps=self._swaps,
            )
            return self._cannot_swap(reason, current, NoAccount(reason="no_account_available"))
        self.control.validate_generation()
        for _ in range(MAX_ACCOUNT_TRIES):
            try:
                outcome = self._account_broker.swap(
                    current,
                    provider=provider,
                    reason=reason,
                    exclude=tuple(self._account_rejected),
                )
            except (BrokerRefused, BrokerUnavailable) as exc:
                self.log.warning(
                    "the quota broker did not move this attempt to another account",
                    reason=reason,
                    refused=isinstance(exc, BrokerRefused),
                    error_type=type(exc).__name__,
                )
                if isinstance(exc, BrokerRefused):
                    # A refused swap is what a fenced attempt meets; the fence
                    # check says so and ends the attempt the fenced way.
                    self.control.validate_generation()
                return self._cannot_swap(reason, current, NoAccount(reason="no_account_available"))
            if isinstance(outcome, NoAccount):
                return self._cannot_swap(reason, current, outcome)
            self._swaps += 1
            self._account = outcome
            self._account_released = False
            self._emit_swapped(current, outcome, reason)
            try:
                self._account_credential_env(profile, outcome)
            except AccountUnreadable as exc:
                self.log.error(
                    "the account this attempt moved to cannot be read; moving again",
                    account_id=outcome.account_id,
                    error=exc.detail,
                )
                self._account_rejected.append(outcome.account_id)
                current, reason = outcome, SWAP_UNUSABLE
                continue
            self._resume_session = self._session_id
            self._just_swapped = True
            self._readings = None
            self._turns_checked = 0
            self._channel_seen = None
            _stream, move = self._account_channel_paths()
            move.unlink(missing_ok=True)
            return self._build_child_env()
        # Every account it moved to was unreadable: give the last one back as
        # such and park, as a fresh attempt does (`_pool_credential_env`).
        return self._cannot_swap(SWAP_UNUSABLE, current, NoAccount(reason=ACCOUNT_UNREADABLE))

    def _cannot_swap(
        self, reason: str, current: Assignment, outcome: NoAccount
    ) -> Outcome | None:
        """No other account took this attempt. Fall back to what happens today.

        An exhausted account: None, and the caller's quota path checkpoints
        and parks exactly as `_park_for_quota` always has. A drain: park on
        the pool, holding the hold until the exit path gives it back. An
        account that could not be read: give it back unusable, then park.
        """
        self.log.warning(
            "no other account can take this attempt; falling back to a checkpoint and park",
            reason=reason,
            pool_reason=outcome.reason,
        )
        if reason == SWAP_EXHAUSTED:
            return None
        if reason == SWAP_UNUSABLE:
            self._account = None
            self._account_released = False
            self._give_back(current, unusable="the agent could not start on this account")
            outcome = NoAccount(reason=ACCOUNT_UNREADABLE)
        return self._park_no_account(NoAccountAvailable(outcome, self.cfg.provider or "unknown"))

    def _emit_swapped(self, left: Assignment, taken: Assignment, reason: str) -> None:
        """The move, on the task's events and in the log. Never a token or a session id.

        `account_assigned` for the account taken -- the cause swarm-api's task
        account read already follows, so the task shows the account it is on
        now -- carrying where it came from and why.
        """
        self.log.info(
            "moved this attempt to another account",
            from_account_id=left.account_id,
            account_id=taken.account_id,
            reason=reason,
        )
        try:
            self.control.emit(
                EventType.RUNNING,
                {
                    "cause": "account_assigned",
                    "account_id": taken.account_id,
                    "provider": self.cfg.provider,
                    "swapped_from": left.account_id,
                    "swap_reason": reason,
                },
            )
        except Exception as exc:
            self.log.warning(
                "could not record the account move on the task's events",
                error_type=type(exc).__name__,
            )

    def _decline_pool(self, cause: str) -> None:
        """Record that this attempt is NOT on the pool, once and for good.

        Returns None so call sites can `return self._decline_pool(...)`. The
        stickiness is the behaviour: `_build_child_env` runs again whenever a
        credential is reloaded mid-attempt, and a second ask there could move a
        running agent onto a pool account it did not start on, or raise from a
        call site whose job is to restart the child rather than to park.
        """
        self._account_declined = cause
        return None

    def _account_credential_env(self, profile: Any, account: Assignment) -> dict[str, str]:
        """Read the ASSIGNED account's secret and shape it for the child.

        `{base}` holds only the access token -- the `{base}-refresh` half that
        can mint successors is readable by the broker alone -- so what lands in
        the child's environment expires on its own. Read on EVERY call, which
        is what makes the credential-reload path work: `access()` always takes
        `latest`, so a token the refresher rotated under a running agent is
        picked up by re-reading the same name.

        EVERY WAY THIS CAN FAIL BECOMES `AccountUnreadable`, including the ones
        that arrive as a raw google-cloud exception. `access()` raises NotFound
        for a secret with no version -- which is exactly the state
        `scripts/account.sh` leaves a freshly added account in until the
        broker's next sweep publishes the access token -- and PermissionDenied
        for an account lent by a tenant that never granted this worker's
        service account `secretmanager.secretAccessor` on it. Both used to
        travel straight out of here into `run()`'s generic handler and fail the
        attempt; both are "this account, not this task", so the caller hands it
        back and asks for another.
        """
        assert self.secret_client is not None
        try:
            payload = self.secret_client.access(account.secret)
            env = credential_env_from_account(
                payload,
                secret_env_names=profile.secrets,
                secret_name=account.secret,
            )
        except Exception as exc:
            raise AccountUnreadable(
                account.account_id, f"{type(exc).__name__}: {exc}"
            ) from None
        for value in env.values():
            self.log.register_secret(value)
        self.log.info(
            "account credentials resolved",
            account_id=account.account_id,
            secret=account.secret,
            variables=sorted(env),
        )
        return env

    def _reject_account(self, account: Assignment, exc: AccountUnreadable) -> None:
        """Hand back an account this worker cannot read, and remember not to ask for it.

        Three things, and all three matter. The hold goes back, or the pool
        counts an agent that never started. The id joins `exclude`, or the next
        ask returns the same account, because `choose()` is deterministic. And
        the reason travels with the release, so the broker can stop offering
        this account to THIS tenant for a while and an operator can see the
        difference between an account nobody wants and an account nobody can
        read.
        """
        self.log.error(
            "the assigned account cannot be read; handing it back and asking "
            "for another",
            account_id=account.account_id,
            secret=account.secret,
            error=exc.detail,
        )
        self.control.emit(
            EventType.RETRYING,
            {
                "cause": "account_unreadable",
                "account_id": account.account_id,
                "provider": self.cfg.provider,
            },
        )
        self._account_rejected.append(account.account_id)
        self._account = None
        self._account_released = False
        self._give_back(account, unusable=exc.detail)

    def _give_back(self, account: Assignment, *, unusable: str = "") -> None:
        """One release call, and it never raises.

        A failed release costs one over-counted hold until it EXPIRES. It is an
        expiry and not a reconciler: `apps/reconciler/` has no account code at
        all, and a comment here used to claim it did -- which is worse than no
        comment, because the next person reads it as a reason not to build the
        backstop. The real one is in the broker: every hold carries a deadline
        and the quota sweep prunes the expired ones, so a worker that is
        SIGKILLed, OOM-killed or preempted costs a few stale minutes on one
        account's counter rather than a slot that never comes back.
        """
        if self._account_broker is None:
            return
        try:
            self._account_broker.release(
                account.account_id, account.assignment_id, unusable=unusable
            )
            self.log.info(
                "account released",
                account_id=account.account_id,
                unusable=bool(unusable),
            )
        except Exception as exc:
            self.log.warning(
                "could not release the account; its hold expires on its own and "
                "the broker's quota sweep prunes it",
                account_id=account.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._emit_account_released(account, unusable=bool(unusable))

    def _emit_account_released(self, account: Assignment, *, unusable: bool) -> None:
        """`account_released` on the task's events, beside `account_assigned` (#380).

        ONCE PER RELEASE THE BROKER ACCEPTED: `_give_back` calls it only after
        `release` returned, so a failed release, whose hold is left to expire,
        claims nothing. The same shape as `account_assigned` -- a `cause` on an
        event the frozen `EventType` already has -- because the API reads every
        `account_*` cause through one range query (`swarm_api.task_accounts`).
        LEASE_RELEASED rather than RUNNING: it is a hold given back, and it is
        written on the way out, after the attempt's own end.

        WHAT IT CARRIES: the account id, the provider, and whether it went back
        unusable, as a bool. `control.emit` stamps the task, the attempt, the
        lease and the generation on the event itself. Never the secret's name
        or payload, and never the unusable reason's text, which quotes Secret
        Manager's error and names the secret.

        Not on a fenced exit or a tenant mismatch: the stream is not this
        attempt's to write then (`_stand_down`, `_exit_tenant_mismatch`).
        Never raises.
        """
        if self._fenced_exit or self._writes_forbidden or self._control_plane_down:
            return
        try:
            self.control.emit(
                EventType.LEASE_RELEASED,
                {
                    "cause": "account_released",
                    "account_id": account.account_id,
                    "provider": self.cfg.provider,
                    "unusable": unusable,
                },
            )
        except Exception as exc:
            self.log.warning(
                "could not record the account's release on the task's events",
                account_id=account.account_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _release_account(self) -> None:
        """Give the account back, on whatever path this attempt is leaving by.

        CALLED FROM `_cleanup`, which is the `finally` of `run()`, because that
        is the ONE place every exit runs through: success, failure, a park on
        quota or on a missing credential, a cancellation, a crash, and a
        mid-run fencing exit. Releasing at each of those sites instead would
        mean a new exit path is a leaked assignment, discovered weeks later as
        an account that `choose()` has quietly stopped picking.

        Never raises. An exception here would replace the attempt's real
        outcome with this one.
        """
        account, self._account = self._account, None
        if account is None or self._account_released or self._account_broker is None:
            return
        self._account_released = True
        self._give_back(account)

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
            every sweep. Minutes, not a quarter of an hour;
          * `pool_paused` -- waiting on a PERSON. There is no instant to wake
            at, so the long fallback;
          * `broker_refused` / `account_unreadable` -- a configuration error.
            It parks as CREDENTIAL_MISSING rather than as quota, because the
            honest summary is "an admin has to fix something", not "come back
            when there is room".
        """
        reason = exc.decision.reason
        configuration_error = reason in (BROKER_REFUSED, ACCOUNT_UNREADABLE)
        fallback_seconds = (
            STALE_READING_RETRY_SECONDS
            if reason == NO_RECENT_READING
            else NO_ACCOUNT_RETRY_SECONDS
        )
        next_eligible = _parse_iso(exc.decision.next_reset_at) or (
            utcnow() + timedelta(seconds=fallback_seconds)
        )
        self.log.warning(
            "the account pool cannot serve this attempt; parking",
            provider=exc.provider,
            reason=reason,
            waiting_on=(
                "an administrator" if configuration_error
                else "a person" if reason == POOL_PAUSED
                else "the broker's next usage poll" if reason == NO_RECENT_READING
                else "a quota window"
            ),
            next_eligible_at=next_eligible.isoformat(),
        )
        self._checkpoint("no-account")
        summary = self._upload_outputs()
        self._export_metrics()
        detail = {
            "provider": exc.provider,
            "account_pool_reason": exc.decision.reason,
            **summary,
        }
        self.control.park(
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
                    {**detail, "next_eligible_at": next_eligible, "park_phase": "account_assign"},
                )
            ],
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_credential_missing(self, exc: CredentialMissing) -> Outcome:
        """No key for this provider. Park; it costs nothing while an admin fixes it."""
        self.log.error("tenant credential missing; parking", provider=exc.provider)
        self._checkpoint("credential-missing")
        summary = self._upload_outputs()
        self._export_metrics()
        self.control.park(
            reason=ParkReason.CREDENTIAL_MISSING,
            next_eligible_at=utcnow() + timedelta(hours=1),
            detail={"provider": exc.provider, **summary},
        )
        return Outcome(exit_code=ExitCode.PARKED, state=TaskState.PARKED)

    def _park_for_quota(self, decision: QuotaDecision, *, source: str) -> Outcome:
        """Checkpoint -> upload -> publish quota -> PARK -> release -> exit."""
        self.log.warning(
            "parking on provider quota",
            wait_seconds=decision.wait_seconds,
            reason=decision.reason.value,
            source=source,
        )
        self._checkpoint("quota-park")
        summary = self._upload_outputs()
        self._export_metrics()
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
            self.control.update_quota_state(
                provider=provider,
                state=state,
                retry_after_seconds=decision.wait_seconds,
                reset_at=decision.next_eligible_at,
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
        self.control.park(
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

    # -- periodic work -----------------------------------------------------
    def _heartbeat(self) -> None:
        # Read BEFORE the beat: the lease's new expiry is computed from the
        # time the beat started, and a beat that retried for most of its
        # budget must not be credited with that time again.
        started = time.monotonic()
        if self.control.heartbeat() is not False:
            # Landed: the lease is live for one more extension from when the
            # beat started (#70). A refused beat (False) extends nothing, and
            # the control poll meets the fence behind it.
            self._lease_live_until = started + self.control.heartbeat_extension_seconds
        self._last_beat_started = started
        self._heartbeats += 1
        if self._heartbeats % HEARTBEAT_EVENT_EVERY == 1:
            self.control.emit(EventType.HEARTBEAT, self._usage_reading())
            # THE LIVE READING DETAILS DRAWS, on the attempt itself (contract
            # request #15): the same cadence as the event, so a running
            # attempt's figures are never more than one reading behind it.
            # WRITTEN EVEN WHEN THE FIGURES DID NOT MOVE (request #26): the
            # reading's time is what says the worker is still reading, and an
            # idle agent's unchanged figures skipped would age as a stall.
            self._record_cpu(periodic=True)

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

        EXACTLY WHAT IT CARRIED BEFORE #188. That change put the peak, the
        mean, the limit and a `final` flag here too, because the frozen
        `Attempt` had nowhere else for them. Contract request #15 gave them
        typed fields on the attempt (`_record_cpu`), which replaced this
        interim home; the reconciler's stuck-browser judgement
        (`reconciler.progress`) is what still reads `cpu_seconds` here.
        """
        usage = self._attempt_usage()
        cpu_seconds = usage.cpu_seconds if usage else None
        return {
            "elapsed_seconds": round(self._child.elapsed_seconds, 1) if self._child else 0,
            "peak_rss_bytes": usage.peak_rss_bytes if usage else None,
            "checkpoints": self.checkpoints.seq,
            "cpu_seconds": round(cpu_seconds, 3) if cpu_seconds is not None else None,
            "cpu_source": usage.cpu_source if usage else None,
        }

    def _record_cpu(self, *, periodic: bool = False) -> None:
        """The attempt's CPU, as typed fields on its own document. Never raises.

        CONTRACT REQUEST #15, accepted on #184 (2026-09-25): `cpu_seconds`,
        `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores` on `Attempt`,
        REPLACING #188's interim path -- the same figures as flat keys on
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
        except Exception as exc:
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
        scheduler); `tests/unit/worker/test_cpu_sampler.py` compares the two.

        WHERE IT CAME FROM IS RECORDED AGAIN (contract request #26, accepted
        on #184, 2026-09-26): `cgroup` when the kernel's `cpu.max` said it,
        `resource_class` when the catalogue did. Request #15's four fields
        dropped the interim HEARTBEAT's `cpu_limit_source`, and Details could
        only say `reported limit`. With `requests == limits` the two agree;
        nobody has yet read what Cloud Run's `cpu.max` holds, which is why the
        source is worth saying.

        `(None, None)` before the task document has been read: nothing yet
        says which class this attempt was sized with, and a guess would be
        drawn as a fact.
        """
        try:
            cores = cgroup_cpu_limit_cores()
        except Exception:  # pragma: no cover - a telemetry read never fails an attempt
            cores = None
        if cores is not None:
            return cores, "cgroup"
        sized = self._sized_resource_class()
        if sized is None:
            return None, None
        return float(RESOURCE_CLASSES[sized].cpu), "resource_class"

    def _sized_resource_class(self) -> str | None:
        """The class the container was sized with; None before the task is read.

        What the CPU limit (`_cpu_limit`), the OOM near-miss limit
        (`_start_sampler`) and the exported metric's `resource_class`
        (`_export_metrics`) are all read against (#205).
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

    def _checkpoint(self, label: str) -> CheckpointRecord | None:
        """Mandatory checkpoint. A failure here is logged, never swallowed.

        The one thing a failed checkpoint must not do is end the attempt: the
        agent is still working, and the correct response to "this checkpoint did
        not upload" is to try again at the next interval, not to throw away the
        run that is currently succeeding.

        A FENCE IS NOT A FAILED CHECKPOINT. The attempt is over, and
        `FencedWriteRefused` is raised to say so. It is checked twice. The first
        check comes before anything is uploaded, because a stale archive in the
        task's prefix is bytes a superseded attempt should not have written
        (a restore no longer lists the prefix, #347, but the archive still
        costs storage and reads as this task's work). The second is in
        the pointer's own transaction (`ControlPlane.record_checkpoint`),
        because a fence can land during the upload.

        THE LEASE IS BEATEN THROUGH ALL OF IT (#426), not only under the
        archive (#286): the owner check, the start event and the pointer's
        transaction are Firestore calls with budgets of 30-60 s each, and they
        ran with nothing beating. The carrier push after it beats on its own.
        """
        if self.ws is None:
            return None
        with self._heartbeat_meanwhile(f"checkpoint ({label})"):
            record = self._write_checkpoint(label)
        if record is None:
            return None
        self._last_checkpoint = record
        self._push_carrier_branch(label)
        return record

    def _write_checkpoint(self, label: str) -> CheckpointRecord | None:
        """`_checkpoint`'s owner check, archive and record, under its heartbeat."""
        assert self.ws is not None  # checked by `_checkpoint`
        try:
            self.control.ensure_owner(write=f"checkpoint ({label})")
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:
            # A read that could not be made is not a fence. Carry on, as a
            # checkpoint always has. The pointer's own transaction is still the
            # authority, and it re-checks.
            self.log.warning(
                "could not confirm this attempt still owns its task before "
                "checkpointing; the pointer write will check again",
                label=label,
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            # The announcement is an audit record, written to Firestore; the
            # archive goes to the bucket. An event that cannot be written must
            # not cost the checkpoint, and during an outage (#70) it is the
            # checkpoint that is needed.
            self.control.emit(EventType.CHECKPOINT_STARTED, {"label": label})
        except (FencedError, TenantMismatchError):
            raise
        except Exception as exc:
            self.log.warning(
                "could not record the checkpoint's start; taking it anyway",
                label=label,
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            record = self.checkpoints.create(self.ws, label=label)
        except CheckpointError as exc:
            self.log.error("CHECKPOINT FAILED", label=label, error=str(exc))
            return None
        except Exception as exc:
            self.log.exception("checkpoint failed unexpectedly", exc, label=label)
            return None
        # THE RECORD'S ERRORS STAY HERE (#70). The archive is in the bucket; the
        # record -- the attempt's list and digest, the task's pointer, the
        # event -- is Firestore's, under its budget. A record that could not
        # be written leaves a checkpoint no restore trusts (#347), which is
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
        except Exception as exc:
            self.log.error(
                "CHECKPOINT NOT RECORDED: the archive was uploaded and its record "
                "could not be written, so no restore will use it",
                label=label,
                checkpoint_id=record.checkpoint_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        return record

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
        one, and the control-plane-outage one, which #70 takes BEFORE it stops
        the runner -- pushes nothing and says so. The checkpoints taken once
        the runner has stopped (final, park, cancellation, SIGTERM) push.
        The owner accepted this narrowing of D13's "every checkpoint" on
        2026-10-02: periodic checkpoints do not push.

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
        except Exception as exc:
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
        refusal = self._git_token_refusal()
        if refusal:
            return f"the tenant git token is refused: {refusal}"
        token = self._git_token()
        if not token:
            return "no credential"
        try:
            access = forge_mod.retry_transient(
                lambda: probe_repository(url=url, token=token),
                policy=self._forge_retry(),
                what="the carrier target probe",
            )
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
          * Everything else proceeds unchanged. A worker with publishing
            disabled, a token the memory guard refuses to read, or a host that
            is not a forge this worker publishes to pushes nothing at its
            checkpoints, exactly as `_carrier_target` already says in its log
            line; none of those is a statement about the token's scope.

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
        if self._git_token_refusal():
            return None
        token = self._git_token()
        if not token:
            return functools.partial(
                self._fail_forge_read_only, url,
                "no git credential is registered for this tenant",
            )
        try:
            access = forge_mod.retry_transient(
                lambda: probe_repository(url=url, token=token),
                policy=self._forge_retry(),
                what="the push-scope probe",
            )
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
        self._export_metrics()
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
        self._export_metrics()
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
        self._export_metrics()
        state = self.control.fail_retryably(
            exit_code=None,
            error=error,
            cause=FORGE_UNREACHABLE,
            result_summary=summary,
            retry_delay_seconds=max(FORGE_UNREACHABLE_RETRY_DELAY_SECONDS, int(retry_after or 0)),
            detail={"input": "issue", "issue": number},
            end_cause=EndCause.CANNOT_START,
        )
        return Outcome(exit_code=ExitCode.FAILED, state=state)

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
        return commit_tree_onto(
            repo=publish_repo,
            parent=parent,
            message=message,
            author_name=cfg.git_author_name,
            author_email=cfg.git_author_email,
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
        """Scan, commit onto the tip, check authorship, push. Raises GitError."""
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        leak = final_tree_leak(
            repo=publish_repo,
            base=self._publish_base,
            leaks=self._leaks_in_added_text,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
            overlap=self._scan_overlap(),
        )
        if leak is not None:
            raise GitError(f"refusing to push: {self._scrub(leak)}")
        made = self._carrier_fold_onto_tip(
            publish_repo, url=url, token=token, branch=branch, message=message
        )
        if made is None:
            self.log.info("carrier: the branch already holds this work", branch=branch)
            return
        verify_worker_authorship(
            repo=publish_repo,
            base=self._publish_base,
            author_name=cfg.git_author_name,
            author_email=cfg.git_author_email,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
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
        self.log.info("carrier: pushed the step's committed work", branch=branch)

    @contextmanager
    def _heartbeat_meanwhile(self, what: str) -> Iterator[None]:
        """Heartbeat the lease from a thread for as long as the block runs.

        THE CHECKPOINT RAN ON THE LOOP THAT HEARTBEATS THE LEASE (#286), so the
        lease aged for as long as the archive and the upload took: a large
        work tree -- tool caches, before `checkpoint.TOOL_CACHES` -- held it
        for however long gzip and GCS needed, and on 2026-09-29 the attempt was
        fenced 64 s after `checkpoint_started`. The block's own work touches
        only the workspace and the object store, so the heartbeat has the
        control plane to itself meanwhile.

        Beats every `heartbeat_interval_seconds`, the loop's own cadence, and
        through `_heartbeat`, the loop's own call. A heartbeat that raises is
        logged and ends this thread only: the loop's next heartbeat meets the
        same condition and handles it as it always has.

        COUNTED FROM THE LAST BEAT, NOT FROM THE BLOCK'S START (#426). The
        first beat is due one interval after the last beat started
        (`_last_beat_started`), at once if that is already past. Counted from
        the block's start, a checkpoint begun just before the loop's next beat
        added a whole interval of silence, and on 2026-10-01 a worker
        compiling on every core was reclaimed alive after 98 s of it.

        BOUNDED, AND IT ASKS BEFORE EVERY BEAT (the PR #288 review). A thread
        that beats for as long as the block runs keeps the lease -- and the
        capacity reserved behind it -- alive for a checkpoint that never ends,
        a wedged upload, which the supervision loop is not running to notice.
        So it beats for at most `heartbeat_meanwhile_max_seconds` (three lease
        timeouts; `config.WorkerConfig` says why), and before each beat it
        reads the control plane: once the attempt is FENCED -- another
        generation owns the task, or its lease is released or it is terminal
        (`ControlSignals.is_fenced`) -- it stops at once, rather than
        prolonging a lease that is no longer this attempt's. The loop sees the
        same signal on its next poll and acts on it as it always has.

        A CANCEL DOES NOT STOP IT (owner decision 2026-09-29, PR #288). A
        cancelled attempt still writes a checkpoint on its way out
        (`self._checkpoint("cancellation")`), and that checkpoint needs the lease alive as
        much as any other; stopping the beat on `cancel_requested` would let
        the lease lapse under the very checkpoint the cancel asked for. The
        bound still applies to it.

        Joined before the block's caller goes on, with a timeout: a beat stuck
        in a Firestore call does not hold the checkpoint's caller up with it.
        What such a beat can still write is `heartbeat_at` and `expires_at` on
        the lease (`ControlPlane.heartbeat` is an unfenced update of those two
        fields). It cannot clear `released_at`, and a lease with `released_at`
        set reads as released (`ControlSignals.is_fenced`) whatever its expiry.
        """
        stop = threading.Event()
        interval = self.cfg.heartbeat_interval_seconds
        bound = self.cfg.heartbeat_meanwhile_max_seconds
        opened = time.monotonic()
        deadline = opened + bound

        # When this thread last asked for a beat: it never asks twice within
        # an interval, whatever `_last_beat_started` says.
        asked: list[float] = []

        def due() -> float:
            # A fixed time, never "now": with no beat on record the first is
            # due at the block's start, which is at once.
            starts = [t for t in (self._last_beat_started, *asked) if t is not None]
            return max(starts) + interval if starts else opened

        def beat() -> None:
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    self.log.warning(
                        "stopped heartbeating the lease during a long operation: it "
                        "outlasted its bound, and the lease is left to expire",
                        during=what,
                        bound_seconds=bound,
                    )
                    return
                if stop.wait(max(0.0, min(due() - now, remaining))):
                    return
                if time.monotonic() >= deadline:
                    continue  # the top of the loop logs the bound and ends
                if time.monotonic() < due():
                    continue  # another beat landed meanwhile; wait for the next
                asked[:] = [time.monotonic()]
                try:
                    signals = self.control.poll()
                    if signals.is_fenced(self.cfg.generation):
                        self.log.warning(
                            "stopped heartbeating the lease during a long operation: "
                            "the attempt is fenced",
                            during=what,
                            task_generation=signals.generation,
                            lease_released=signals.lease_released,
                            state=signals.state,
                        )
                        return
                    self._heartbeat()
                except Exception as exc:
                    # A beat that could not reach the control plane is one of
                    # the missed beats the interval is sized for (#426): its
                    # budget is one interval now, not the whole grace, so the
                    # next is asked for on time rather than the thread ending
                    # with the block still running. A fence, a tenant mismatch
                    # or a defect ends the thread, as before.
                    unreachable = not isinstance(
                        exc, (FencedError, TenantMismatchError)
                    ) and _control_plane_unreachable(exc)
                    self.log.warning(
                        "a heartbeat during a long operation failed; "
                        + ("the next beat is asked for on time" if unreachable
                           else "the loop will retry it"),
                        during=what,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    if not unreachable:
                        return

        thread = threading.Thread(target=beat, name="heartbeat-meanwhile", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=interval)
            if thread.is_alive():
                self.log.warning(
                    "a heartbeat during a long operation is still in flight; going on without it",
                    during=what,
                )

    def _sleep_with_heartbeat(self, seconds: float) -> None:
        """Short waits only -- long ones park. Keeps the lease alive meanwhile."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._interrupted:
            time.sleep(min(self.cfg.heartbeat_interval_seconds, max(0.1, end - time.monotonic())))
            try:
                self._heartbeat()
            except Exception as exc:
                # Logged, never fatal here (#70): the supervision loop's next
                # beat decides, against the lease, whether it is an outage.
                self._control_plane_call_failed("heartbeat", exc)

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

        NOTHING IN THE ARTIFACTS FOLDER IS REWRITTEN IN PLACE ANY MORE (#227).
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

        THE ONE WAY A FILE FROM THE ARTIFACTS FOLDER LEAVES THE POD (#227).
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
        as-is" (#227); without it, the caller reports.
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
        except Exception as exc:
            self.log.warning("artifact upload failed", artifact=shown, error=str(exc))
            return _Upload(skipped=standalone_mod.UPLOAD_FAILED)
        finally:
            staging.unlink(missing_ok=True)
        if entry is not None and report:
            self._report_unredacted(entry)
        return _Upload(bytes=size, unredacted=entry)

    def _walk_artifacts(self, root: Path) -> tuple[dict[str, int], list[str]]:
        """Every regular file under the artifacts folder, and the folders not walked.

        `{relative POSIX name: size as lstat saw it}`, and the relative names
        of the folders passed over: too deep for any name under them to be an
        object's, or unreadable. Never raises, never follows a link, and never
        recurses: the walk keeps its own stack, so no depth of tree can raise
        RecursionError (#227), and a folder whose name alone leaves no room
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
        write (#227).

        `withheld` is the working-folder upload's answer to that trade: a file
        this returns an entry for is NOT uploaded, and the lines say so. An
        artifact the agent chose to deliver goes up as-is and is reported; a
        file caught by the net under the working folder does not (#225
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
        API's stream names (`swarm_api.inspect.LOG_STREAMS`), compared by
        `tests/unit/control_plane/test_agent_stream_parity.py`.
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
            # REFUSED AT THE OPEN, NOT BEFORE IT (#227). A leaf check and then
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
        except Exception as exc:  # pragma: no cover - never fail a run for this
            self.log.debug("could not publish a live log tail", stream=label, error=str(exc))

    def _dispatch_block(self) -> dict[str, Any]:
        """The `task.metadata["dispatch"]` block, or an empty one.

        swarm-api writes four fields here -- strategy, carrier, role and
        integrates (see DispatchOptions.to_metadata) -- and, on a step that
        declares them, `builds_on` and `verdict_gate` (#264). For a long time this
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
        """The upstream task id whose pushed branch this step clones (#264), or "".

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

        `tests/unit/worker/test_dispatch_contract_parity.py` imports swarm-api's
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

    # -- git: harvest, and publish when the forge allows it -----------------
    def _harvest_git(
        self, *, publish: bool, withheld: str = "", defer: bool = False
    ) -> dict[str, Any] | None:
        """Describe the agent's changes, and publish them when permitted.

        Returns the dict that becomes `result_summary["git"]`, or None when
        there is no repository to describe. It NEVER raises: an attempt that
        ran is not a failed attempt because its epilogue could not talk to
        GitHub, and this runs on the teardown path where the lease is about to
        be released either way. Every failure becomes a readable string in the
        returned dict instead.

        `publish` is False on all three park paths. A parked attempt resumes
        from its checkpoint and will reach the terminal path later, so pushing
        a branch and opening a pull request for work that is still in progress
        would put a half-finished change in front of a reviewer -- and do it
        again on every quota bounce. A park passes nothing and is described as
        one.

        `defer` is `_finalise`'s (#165): everything up to the publish runs --
        the reap, the clean repository, the patch -- and the publish is left
        in `_deferred_publish` for `_publish_checked`, which makes it only
        after the upload manifest has passed the missing-output check. So an
        attempt that check fails, retryably or not, has pushed nothing.
        """
        ws = self.ws
        cfg = self.cfg
        if ws is None or not cfg.git_harvest_enabled:
            return None
        repo = ws.work / REPO_DIR_NAME
        if not (repo / ".git").exists():
            return None

        base = self._clone_base or self._read_clone_base()
        out: dict[str, Any] = {"base": base}
        if self._clone_commit:
            out["clone_commit"] = self._clone_commit
        if self._base_pin is not None:
            out["base_pin"] = dict(self._base_pin)
        if base is None and self._publish_base == EMPTY_CLONE_BASE:
            out["note"] = (
                "the repository was empty when it was cloned, so there is no "
                "base to diff against; uncommitted files are still listed"
            )
        elif base is None:
            out["note"] = (
                "the clone base is unknown, so no diff could be computed; "
                "uncommitted files are still listed"
            )

        # THE HARVEST RUNS NO GIT IN THE CLONE EITHER (#259, owner item 3).
        # `summarize_work` used to take its patch with `git diff <base>` in the
        # clone, and a diff of the working tree reads each file through the
        # clean filter the clone's `.gitattributes` and `.git/config` name --
        # the agent's program, run as the worker. So the order is now:
        #
        #   1. REAP. No process the agent started is alive from here on
        #      (`reap_before_publish`); if one survives, nothing below runs.
        #      This used to happen only inside `_publish_git`; it moves here
        #      because the clean repository is built here, and a live agent
        #      process could poison it (see `_publish_git`).
        #   2. BUILD the worker's clean repository: the agent's commits are
        #      fetched in (`prepare_publish_repo`) and its working tree copied
        #      in (`mirror_worktree`), with the patterns it excluded read as
        #      data.
        #   3. HARVEST there: the patch, the commit list and the dirty list
        #      are read from the worker's repository, where a `.gitattributes`
        #      naming a filter is data.
        #   4. PUBLISH from the same repository (`_publish_git`), which
        #      commits the uncommitted work on top and pushes.
        #
        # The clone itself is read only by the object-checked fetch, whose
        # `upload-pack` runs with every program-naming key pinned
        # (`gitops._UPLOAD_PACK_ENV`).
        survivors = self.reap_before_publish()
        if survivors:
            reason = (
                f"refusing to publish: {len(survivors)} process(es) the agent started "
                f"are still alive after the pre-publish reap ({', '.join(str(p) for p in survivors[:10])}); "
                "the tenant credential is not put in hand while agent code can run"
            )
            self.log.error(
                "not harvesting or publishing: agent processes survived the reap",
                surviving_pids=list(survivors),
            )
            out["published"] = False
            out["publish_reason"] = reason
            out["error"] = (
                "the agent's work was not harvested: processes it started survived "
                "the reap, and the worker reads no repository while agent code can run"
            )
            return out

        # The floor the clean repository is built on: the worker's own record
        # when it has one, else the harvest's base (the workspace marker, on a
        # resume from a checkpoint that recorded none -- the publish refuses
        # that case before it pushes, but the work is still described). A floor
        # that is not a full sha is unknown, and then only HEAD is fetched.
        floor = self._publish_base if self._publish_base is not None else base
        fetched_base = (
            floor if floor and re.fullmatch(r"[0-9a-f]{40}", floor) else None
        )
        try:
            publish_repo = self._build_clean_repo(repo, floor=floor, allow_unknown_base=True)
            work = summarize_work(
                repo=publish_repo,
                base=fetched_base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                patch_path=ws.artifacts / PATCH_NAME,
                max_patch_bytes=cfg.max_patch_bytes,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                # `_publish_base`, the worker's own record, not the workspace
                # marker the agent can write: only a clone the worker itself
                # saw land on nothing counts every commit as the agent's.
                empty_base=base is None and self._publish_base == EMPTY_CLONE_BASE,
            )
        except GitError as exc:
            self.log.warning("could not harvest the agent's git changes", error=str(exc))
            out["error"] = self._scrub(str(exc)[:500])
            if publish or defer:
                # Fails closed, and says so where a reader of the publish looks.
                out["published"] = False
                out["publish_reason"] = f"nothing was published: {out['error']}"
            return out

        out.update(
            {
                "head": work.head,
                "commits": [
                    {
                        "sha": c.sha,
                        "subject": self._scrub(c.subject[:200]),
                        "author": c.author,
                        "committed_at": c.committed_at,
                        "files_changed": c.files_changed,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                        "binary_files": c.binary_files,
                    }
                    for c in work.commits[:100]
                ],
                "commit_count": len(work.commits),
                "insertions": work.insertions,
                "deletions": work.deletions,
                "dirty": [self._scrub(path) for path in work.dirty],
                "dirty_count": len(work.dirty),
                "dirty_truncated": work.dirty_truncated,
                "patch": work.patch_name,
                "patch_bytes": work.patch_bytes,
                "patch_omitted": work.patch_omitted,
            }
        )
        if work.patch_omitted:
            out["patch_note"] = (
                f"the diff was {work.patch_bytes} bytes, over the "
                f"{cfg.max_patch_bytes} cap, and was discarded rather than "
                "truncated -- a truncated patch applies cleanly and silently "
                "drops the rest of the change"
            )

        # AN INTEGRATOR IS THE ONE STEP WHOSE DELIVERABLE IS NOT ITS OWN WORK.
        #
        # For every other step "changed nothing" means there is nothing to
        # push and nothing to open, and returning here is right. For an
        # integrator it meant the opposite of what the caller was promised: a
        # step whose agent edited no files -- an entirely ordinary outcome for
        # one whose prompt is "bring these together" -- returned before
        # `_publish_git` ran, so no contributor branch was ever merged, no
        # branch was pushed, and the ONE pull request `integrate` promises was
        # never opened. Zero, for a workflow whose contributors had all already
        # run, been billed and pushed their branches.
        #
        # It was invisible because it depended on whether the integrator's
        # agent happened to touch a file. Found by running the strategy against
        # a real repository, not by reading it.
        if work.is_empty and not self._integration_is_pending():
            out["published"] = False
            out["publish_reason"] = "the agent changed nothing in the repository"
            return out

        if defer:
            self._deferred_publish = {
                "repo": repo, "work_head": work.head, "publish_repo": publish_repo,
            }
            return out
        out.update(
            self._publish_git(
                repo=repo, work_head=work.head, publish=publish, withheld=withheld,
                publish_repo=publish_repo,
            )
        )
        return out

    def _publish_checked(self, summary: dict[str, Any], withheld: str | None) -> None:
        """The publish `_harvest_git` deferred, made now that the check has run (#165).

        `withheld` is `_publish_withheld`'s answer, None when this attempt may
        publish. Without a deferred publish -- no repository, a harvest that
        returned early (a survivor of the reap, a git error, nothing changed)
        -- the harvest already said why nothing was published, and this does
        nothing. The result is scrubbed into `summary["git"]` like the rest
        of the summary. Never raises: the epilogue cannot fail the attempt.
        """
        deferred, self._deferred_publish = self._deferred_publish, None
        git = summary.get("git")
        if deferred is None or not isinstance(git, dict):
            return
        try:
            result = self._publish_git(
                repo=deferred["repo"],
                work_head=deferred["work_head"],
                publish=withheld is None,
                withheld=withheld or "",
                publish_repo=deferred["publish_repo"],
            )
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.exception("the publish raised; continuing without it", exc)
            result = {"published": False, "publish_reason": "the publish failed unexpectedly"}
        git.update(self._scrub(result))

    def _integration_is_pending(self) -> bool:
        """True when this step still owes a merge even having changed nothing.

        Narrow on purpose: `integrate` AND the integrator role AND at least one
        upstream task to merge. A single-step `integrate` workflow that changed
        nothing has genuinely nothing to publish and keeps the short circuit
        above, and no other strategy is affected at all.
        """
        return (
            self._dispatch_strategy() == "integrate"
            and self._dispatch_role() == "integrator"
            and bool(self._dispatch_integrates())
        )

    def _build_clean_repo(
        self, repo: Path, *, floor: str | None, allow_unknown_base: bool = False
    ) -> Path:
        """The worker's clean repository, holding the agent's commits and a copy
        of its working tree (#259). Call only after the reap.

        `prepare_publish_repo` fetches the clone's HEAD (and `floor`) in with
        object checking; `mirror_worktree` copies the working tree in, with the
        worker's `./artifacts` link and the patterns the agent excluded
        (`read_agent_excludes`, read as data) hidden from git there. Nothing is
        committed yet: the harvest reads the uncommitted work from here, and
        `_publish_git` commits it here.
        """
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        publish_repo = prepare_publish_repo(
            source_repo=repo,
            # The clone's HEAD: the agent's last commit, or none at all in an
            # empty repository it never committed in.
            work_head=None,
            base=floor,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_clone_timeout_seconds,
            logger=self.log,
            allow_unknown_base=allow_unknown_base,
        )
        mirror_worktree(
            source=repo,
            dest=publish_repo,
            logger=self.log,
            hidden_names=self._hidden_checkout_names(repo),
            agent_excludes=read_agent_excludes(
                clone=repo, workspace_root=ws.root, home=ws.work, logger=self.log
            ),
        )
        return publish_repo

    def _publish_git(
        self,
        *,
        repo: Path,
        work_head: str | None,
        publish: bool,
        withheld: str = "",
        publish_repo: Path | None = None,
    ) -> dict[str, Any]:
        """The push-and-pull-request half. Gated on the forge, not on hope.

        `publish_repo` is the clean repository the harvest already built,
        after its reap (`_harvest_git`). Without one -- a direct call -- this
        reaps and builds it itself, at the same point it always did.
        """
        cfg = self.cfg
        ws = self.ws
        assert ws is not None

        if not publish:
            return {
                "published": False,
                "publish_reason": withheld
                or "this attempt parked; publishing waits for the run to finish",
            }
        if not cfg.git_publish_enabled:
            return {"published": False, "publish_reason": "publishing is disabled for this worker"}

        # THE CALLER'S STRATEGY IS A PROMISE AND THIS IS WHERE IT IS KEPT.
        #
        # swarm-api accepts `strategy` on submit and returns it in the 201, and
        # `collect` -- the default every caller gets -- states that patches are
        # harvested and NOTHING IS PUSHED. Until this check existed that
        # guarantee held only because the tenant's token happened to lack write
        # scope: granting write scope would have made every `collect` dispatch
        # start opening pull requests while its own API response promised it
        # would not. A guarantee enforced by an unrelated accident is not a
        # guarantee.
        #
        # Read from metadata rather than a typed field because adding one to
        # Task would be a frozen-contract change; the request to type it is
        # recorded in docs/contract-change-requests.md.
        strategy = self._dispatch_strategy()
        # `single-pr` (#295, docs/merge-step.md §3) publishes by ROLE: the
        # author pushes its own branch and opens the one pull request, as
        # `direct-pr` does; the amender fast-forward pushes to the AUTHOR's
        # branch and opens nothing; a reader pushes nothing at all, whatever
        # it changed -- review and proof only write their JSON.
        pr_role = self._pr_role()
        if pr_role in ("reader", "none"):
            return {
                "strategy": "single-pr",
                "pr_role": pr_role,
                "published": False,
                "publish_reason": (
                    f"strategy is 'single-pr' and this step's pr_role is {pr_role!r}: "
                    "it pushes nothing and opens nothing"
                ),
            }
        if pr_role:
            strategy = "single-pr"
        # `carrier: branches` PUSHES under every strategy (D13): the branch is
        # where the step's work is kept for the next step, so a `collect` step
        # pushes it too -- and opens nothing, below.
        carrier = self._dispatch_carrier()
        if strategy == "collect" and carrier != "branches":
            return {
                "strategy": strategy,
                "published": False,
                "publish_reason": (
                    "strategy is 'collect': the patch is harvested and nothing "
                    "is pushed. Submit with strategy 'direct-pr' to open a pull "
                    "request from this agent"
                ),
            }

        url = self._repo_url or cfg.repository_url
        if not url:
            return {"published": False, "publish_reason": "the repository URL is unknown"}

        # Before the token is read, not after: see `_git_token_refusal`. Asked
        # here as well as inside `_git_token`, so the publish says WHY it did
        # not happen instead of reporting a forge that refused an anonymous probe.
        refusal = self._git_token_refusal()
        if refusal:
            self.log.error("not publishing: the tenant git token is refused", reason=refusal)
            return {"published": False, "publish_reason": f"refusing to publish: {refusal}"}

        token = self._git_token()
        try:
            access = forge_mod.retry_transient(
                lambda: probe_repository(url=url, token=token),
                policy=self._forge_retry(),
                what="the publish probe",
            )
        except ForgeError as exc:
            out = {
                "published": False,
                "publish_reason": f"could not reach the forge: {self._scrub(str(exc)[:300])}",
            }
            if isinstance(exc, forge_mod.ForgeUnavailable):
                out[PUBLISH_UNREACHABLE_FIELD] = self._unreachable_marker(exc)
            return out
        if access is None:
            return {
                "published": False,
                "publish_reason": "this repository is not on a forge this worker can publish to",
            }

        out: dict[str, Any] = {
            "repository": access.ref.full_name,
            "default_branch": access.default_branch or None,
            "can_push": access.can_push,
        }
        if not access.can_push:
            # THE EXPECTED PATH TODAY, and the reason it reads as a fact rather
            # than a failure. The tenant's secret holds a clone token; the forge
            # was asked and said no. The patch above is the deliverable.
            out["published"] = False
            out["publish_reason"] = access.reason
            self.log.info("not publishing: no write permission", reason=access.reason)
            return out

        if not token:  # pragma: no cover - can_push implies a token
            out["published"] = False
            out["publish_reason"] = "no credential"
            return out

        # Its own `<prefix><task id>`, or the branch a fix step continues (#263).
        try:
            # The amender's branch is the author's, derived from its task id;
            # `push_branch` never forces, so its push only fast-forwards.
            branch = self._pr_author_branch() if pr_role == "amender" else (
                continuation_mod.publish_branch(
                    (self._task or {}).get("metadata"), cfg.git_branch_prefix, cfg.task_id
                )
            )
        except WorkerError as exc:
            out["published"] = False
            out["publish_reason"] = f"refusing to publish: {exc}"
            return out
        protected = (access.default_branch,) if access.default_branch else ()

        role = self._dispatch_role() if strategy == "integrate" else ""
        out["role"] = role or None
        if pr_role:
            out["pr_role"] = pr_role

        # NO AGENT PROCESS IS ALIVE ONCE THE TOKEN IS IN HAND. This is the one
        # place the credential-bearing publish begins -- everything below carries
        # or leads directly to the tenant token (`prepare_publish_repo` mkdtemps
        # the clean publish repo and fetches the agent's commits into it,
        # `mirror_worktree` copies its uncommitted files there, and the commit,
        # the scans, the replay or fold and the merge/push all run there). A
        # process the agent double-forked into
        # its own session survives the runner's group kill (procman.py), shares
        # the worker's uid, and could watch `ws.private` to poison the publish
        # repo before the push or read the short-lived credential file while it
        # runs. So every such process is killed and its death verified BEFORE the
        # publish repo is created; if any survives the bounded retry, the whole
        # publish is refused rather than run with agent code still able to act.
        # Reaped AGAIN here even when the harvest already reaped and built the
        # repository (`_harvest_git`), before any step that writes the
        # credential file or pushes: the #259 final review (B1) found a git in
        # the clone that could start a process after the first reap. That path
        # is closed (HEAD is read as data), and this second reap keeps the
        # guarantee above from depending on it.
        survivors = self.reap_before_publish()
        if survivors:
            out["published"] = False
            out["publish_reason"] = (
                f"refusing to publish: {len(survivors)} process(es) the agent started "
                f"are still alive after the pre-publish reap ({', '.join(str(p) for p in survivors[:10])}); "
                "the tenant credential is not put in hand while agent code can run"
            )
            self.log.error(
                "refusing to publish: agent processes survived the pre-publish reap",
                surviving_pids=list(survivors),
            )
            return out

        auto_committed = False
        folded = 0
        kept = 0
        merge: MergeOutcome | None = None
        try:
            # EVERY COMMIT THIS PUSHES IS THE WORKER'S. Whatever the agent
            # committed itself -- possibly as Claude, possibly with a
            # Co-Authored-By trailer and a "Generated with" line, which is what
            # Claude Code adds by default -- is rewritten as a commit the
            # worker writes. See `gitops.fold_agent_commits` for why this is
            # done here and not with a setting in the image.
            #
            # EACH OF THE AGENT'S COMMITS IS KEPT (#242), in order, with its
            # own tree and its own message cleaned of trailers and footers
            # (`replay_agent_commits`); the worker's commit of the uncommitted
            # work below, if there is one, stays last. Folding them into one
            # made a red-first pull request -- tests, then the fix --
            # impossible: CI never saw the
            # tests alone. The fold is kept for the history that cannot be
            # replayed: one that does not descend from the clone base, or one
            # past `MAX_KEPT_COMMITS`.
            # `_publish_base`, never the workspace marker: see `_maybe_clone`.
            if self._publish_base is None and self._clone_base:
                # Said precisely, because "unknown" would be false: the harvest
                # HAD a base, and a reader comparing the two would think one
                # of them was a bug.
                raise GitError(
                    "the clone base is known only from the marker in the "
                    "workspace, which the agent can write: this attempt resumed "
                    "from a checkpoint that does not record the base, so the "
                    "worker cannot tell which commits are the agent's. Nothing "
                    "was pushed; the harvest still describes the work against "
                    "that marker"
                )
            # A repository the harvest built may stand on no base at all
            # (`allow_unknown_base`, so the dirty list survives); it is never
            # published from. One built below refuses the unknown base itself,
            # in `prepare_publish_repo`.
            if publish_repo is not None and self._publish_base is None:
                raise GitError(
                    "the clone base is unknown, so the worker cannot tell the agent's "
                    "commits from the repository's; nothing was pushed"
                )

            # EVERYTHING THAT DECIDES WHAT IS PUSHED, AND THE PUSH, RUNS IN A
            # REPOSITORY THE WORKER OWNS (owner decision, 2026-09-29, #259).
            # The agent's commits are fetched out of the clone into a fresh,
            # worker-owned repository -- object-checked, by sha, with no
            # alternates into the clone and none of its config, hooks, grafts,
            # shallow file, replace refs or commit-graph honoured -- and the
            # kept-commit list, the per-commit scan, the fold, the final-tree
            # scan, the replay, the authorship check, the integrator's merges
            # and the token-bearing push all run there. Three reviews found
            # the scans bypassable through `.git` state the agent writes, one
            # file at a time; this removes the class. The token is still only
            # ever used in this repository (#219). See
            # `gitops.prepare_publish_repo` and docs/merge-strategy-live-proof.md §6.
            #
            # WHAT THE AGENT LEFT UNCOMMITTED IS COMMITTED THERE, NOT IN ITS
            # CLONE (#259 M1, owner decision 2026-09-28). `git add` in the
            # clone read the clone's `.git/config` and the tree's
            # `.gitattributes`, so a filter driver the agent defined ran as the
            # worker after the reap. The working tree is copied into the
            # publish repository instead (`_build_clean_repo`) -- no `.git`,
            # links as links -- and staged and committed with the worker's
            # configuration alone, where a `.gitattributes` naming a filter is
            # data: no filter of that name is defined. Nothing under the
            # clone's `.git` runs at publish; the clone is read only by the
            # object-checked fetch.
            if publish_repo is None:
                publish_repo = self._build_clean_repo(repo, floor=self._publish_base)
            new_sha = commit_dirty(
                repo=publish_repo,
                message=self._worker_commit_message(
                    UNCOMMITTED_COMMIT_SUBJECT,
                    "Staged by the worker at the end of the attempt so that work "
                    "the agent edited but did not commit is not lost between the "
                    "workspace and this branch.",
                ),
                author_name=cfg.git_author_name,
                author_email=cfg.git_author_email,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )
            if new_sha:
                auto_committed = True
                work_head = new_sha

            # `carrier: branches` (D13): the branch may already hold this step's
            # earlier pushes (`_push_carrier_branch`), and a replay writes new
            # shas every time, so its line would not descend from them and the
            # push, never forced, would be refused. The final tree goes on the
            # branch's tip as one worker commit instead.
            if carrier == "branches":
                self._carrier_fold_onto_tip(
                    publish_repo, url=url, token=token, branch=branch,
                    message=self._worker_commit_message(
                        FOLDED_COMMIT_SUBJECT,
                        "The step's work as it finished, committed by the worker on "
                        "top of what its checkpoints already pushed (carrier: "
                        "branches), so the branch only ever fast-forwards.",
                    ),
                )
            else:
                replayed = replay_agent_commits(
                    repo=publish_repo,
                    base=self._publish_base,
                    keep=new_sha,
                    task_id=cfg.task_id,
                    scrub=lambda text: str(self._scrub(text)),
                    author_name=cfg.git_author_name,
                    author_email=cfg.git_author_email,
                    private_dir=ws.private,
                    logs_dir=ws.logs,
                    timeout_seconds=cfg.git_harvest_timeout_seconds,
                    logger=self.log,
                    # A registered secret OR a credential-shaped run in any kept
                    # commit's diff folds the history instead (#259 review; owner
                    # decision 4, 2026-09-28, for the patterns): the scrub
                    # replaces exactly the registered values, so "the scrub
                    # changed it" is "a registered value is in it", and a key the
                    # agent minted or an `.env` it committed is registered nowhere
                    # and caught only by the patterns. Asked about the text each
                    # file ADDS, `+` removed, in overlapping windows.
                    leaks=self._leaks_in_added_text,
                    overlap=self._scan_overlap(),
                )
                if replayed is not None:
                    kept = replayed
                    out["agent_commits_kept"] = kept
                else:
                    replaced = fold_agent_commits(
                        repo=publish_repo,
                        base=self._publish_base,
                        keep=new_sha,
                        message=self._worker_commit_message(
                            FOLDED_COMMIT_SUBJECT,
                            "Everything the agent changed in this attempt, committed or "
                            "not, as one commit made by the worker. The worker writes "
                            "every commit it pushes, so no author, trailer or footer "
                            "added inside the agent's container reaches this branch.",
                        ),
                        author_name=cfg.git_author_name,
                        author_email=cfg.git_author_email,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_harvest_timeout_seconds,
                        logger=self.log,
                    )
                    # The worker's own auto-commit is among what was replaced when
                    # there was one; the rest were the agent's.
                    folded = max(replaced - (1 if new_sha else 0), 0)
                    out["agent_commits_folded"] = folded

            # THE BRANCH AS IT WILL BE PUSHED IS SCANNED BEFORE ANY PUSH
            # (owner decision, 2026-09-28, #259 review M4). The scan above
            # decides only between keeping and folding; the fold's one commit
            # and the worker's commit of uncommitted work carry the FINAL
            # tree, which it never read. A registered value or a
            # credential-shaped literal that tree ADDS publishes nothing, and
            # the attempt fails retryably (`_fail_for_final_tree_leak`),
            # naming the file and never the value.
            leak = final_tree_leak(
                repo=publish_repo,
                base=self._publish_base,
                leaks=self._leaks_in_added_text,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                overlap=self._scan_overlap(),
            )
            if leak is not None:
                reason = str(self._scrub(leak))
                out["published"] = False
                out["auto_committed"] = auto_committed
                out["final_tree_leak"] = reason
                out["publish_reason"] = f"refusing to publish: {reason}"
                self.log.error("not publishing: the final tree failed the leak check", reason=reason)
                return out

            # EVERYTHING THAT CARRIES THE TOKEN RUNS IN THE SAME WORKER-OWNED
            # REPOSITORY, NOT IN THE CLONE. The push and the integrator's
            # contributor fetch authenticate with the tenant token; run in the
            # clone, a `credential.helper` or `url.*.insteadOf` the agent wrote
            # into `.git/config` would receive or redirect it. The verify runs
            # here too, so it sees the integrator's merge commits.

            # THE INTEGRATOR MERGES BEFORE IT PUSHES.
            #
            # Its own commit has to be in the tree first (transferred above), and
            # every contributor's branch has to be in it before the push, or the
            # single pull request this strategy promises would contain only
            # the integrator's own step.
            #
            # The branch names are DERIVED from the upstream task ids with the
            # same prefix the contributors pushed under -- never taken from
            # metadata as names -- so nothing in a task document can point this
            # at an arbitrary ref.
            if role == "integrator":
                upstream = [
                    f"{cfg.git_branch_prefix}{tid}" for tid in self._dispatch_integrates()
                ]
                if upstream:
                    merge = merge_branches(
                        repo=publish_repo,
                        url=url,
                        branches=upstream,
                        token=token,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_clone_timeout_seconds,
                        logger=self.log,
                        author_name=cfg.git_author_name,
                        author_email=cfg.git_author_email,
                        branch_prefix=cfg.git_branch_prefix,
                    )
                    out["integrated"] = {
                        "merged": list(merge.merged),
                        "conflicted": list(merge.conflicted),
                        "missing": list(merge.missing),
                        "complete": merge.complete,
                    }

            # THE PROPERTY, CHECKED WHERE THE WORK LEAVES. The fold and the
            # identity arguments are how every pushed commit is made the
            # worker's; this reads the commits the push is about to add and
            # refuses the push if any is not, whatever route got it there. It
            # runs in the publish repo, so an integrator's merge commits are in
            # its first-parent span.
            verify_worker_authorship(
                repo=publish_repo,
                base=self._publish_base,
                author_name=cfg.git_author_name,
                author_email=cfg.git_author_email,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )

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
        except GitError as exc:
            out["published"] = False
            out["auto_committed"] = auto_committed
            out["publish_reason"] = self._scrub(str(exc)[:500])
            self.log.warning("could not publish the branch", error=str(exc))
            return out

        out.update(
            {
                "branch": branch,
                "pushed_head": pushed or work_head,
                "auto_committed": auto_committed,
            }
        )

        # A CONTRIBUTOR PUSHES AND STOPS. This is the whole difference between
        # `integrate` and `direct-pr`, and its absence is what made them the
        # same run: every step used to reach the code below and open its own
        # pull request, so a six-step `integrate` workflow produced six of them
        # against an API whose schema, validator and docs all say it produces
        # ONE. The branch is the deliverable here; the integrator merges it.
        if carrier == "branches":
            self._carrier_pushed = {"name": branch, "head": str(pushed or work_head or "")}
        if pr_role == "amender":
            out["published"] = True
            out["publish_reason"] = (
                "strategy is 'single-pr' and this step is the amender: it fast-forward "
                f"pushed the author's branch {branch} and opened nothing; the author's "
                "pull request carries the push"
            )
            return out
        if strategy == "collect":
            # Reached only with `carrier: branches` (D13): the branch is the
            # carrier, and `collect` still opens nothing.
            out["published"] = True
            out["publish_reason"] = (
                "carrier is 'branches': the step's branch was pushed; strategy "
                "'collect' opens no pull request"
            )
            return out
        if role == "contributor":
            out["published"] = True
            out["publish_reason"] = (
                "strategy is 'integrate' and this step is a contributor: its "
                "branch was pushed and no pull request was opened. The "
                "integrator step merges this branch and opens the single pull "
                "request for the whole workflow."
            )
            return out

        if not access.default_branch:
            out["published"] = True
            out["publish_reason"] = (
                "the branch was pushed; no pull request was opened because the "
                "repository's default branch could not be read"
            )
            return out

        # THE AGENT'S OWN TITLE AND BODY WHEN IT WROTE THEM (#214), so a
        # platform pull request can say `Closes #N`. The platform's block --
        # provenance, the commit note, an integration's missing branches --
        # follows the agent's text, whole: a reviewer still learns what ran
        # and what this pull request does NOT contain. Read here, after the
        # pre-publish reap, so no agent process is left to change the files.
        generated = self._pull_request_body(
            branch=branch, auto_committed=auto_committed, merge=merge, folded=folded, kept=kept
        )
        agent_title, agent_body, refused = self._agent_pull_request_text()
        title = agent_title or self._generated_pull_request_title()
        body = f"{agent_body}\n\n---\n\n{generated}" if agent_body else generated
        # The generated block quotes the step's prompt, which is anyone's
        # text too; the whole body is neutralised, so no part of what the
        # platform posts can page anyone (owner decision, 2026-09-29). The
        # agent's part already was, and a second pass changes nothing.
        body = _neutralise_mentions(body)
        # The console links go on AFTER the neutralising pass, so no joiner
        # can land inside a URL; they are built from the platform's own task
        # and workflow ids and the scheduler's console origin, and a workflow
        # id is percent-encoded, so they carry no `@` of anyone's.
        body = pr_body_with_console_links(
            body,
            origin=cfg.console_url,
            task_id=cfg.task_id,
            workflow_id=str((self._task or {}).get("workflow_id") or "") or None,
            enabled=cfg.pr_console_links,
        )
        out["pull_request_text"] = {
            "title": "agent" if agent_title else "platform",
            "body": "agent" if agent_body else "platform",
        }
        if refused:
            out["pull_request_text_refused"] = refused
        if title is None:
            # NO TITLE IS INVENTED (owner decision, 2026-09-28). The branch is
            # pushed; the pull request waits for a title the agent writes.
            # An attempt with retries left never gets here without the file:
            # `_title_owed` made `pr-title.txt` an expected output, so its
            # absence failed the attempt retryably before the publish.
            out["published"] = True
            out["publish_reason"] = (
                f"the branch was pushed; no pull request was opened because the "
                f"agent wrote no usable {PR_TITLE_FILE}. Write a one-line title "
                f"to $SWARM_ARTIFACTS_DIR/{PR_TITLE_FILE} (no task id, no "
                f"attribution) and retry"
            )
            self.log.warning("no pull request opened: no usable agent title", refused=refused)
            return out
        try:
            # Retried on an outage: a POST that timed out after GitHub made
            # the pull request is answered 422 on the next try, which
            # `open_pull_request` resolves by adopting the open one.
            pr = forge_mod.retry_transient(
                lambda: open_pull_request(
                    access=access,
                    token=token,
                    head=branch,
                    base=access.default_branch,
                    title=title,
                    body=body,
                    # A reused pull request takes the agent's text too; generated
                    # text never overwrites one a human may have edited.
                    update_existing=bool(agent_title or agent_body),
                    # An adopted pull request whose title carries the task id --
                    # the OLD generated `[swarm] <task id>`, from before this
                    # rule -- is retitled (title only) even when nothing here
                    # asked for an update: the owner's rule is that a title never
                    # carries the task id, and that has to reach a pull request
                    # this attempt only adopts, not just one it opens.
                    retitle_if=self._title_carries_task_id,
                ),
                policy=self._forge_retry(),
                what="the pull request",
            )
        except ForgeError as exc:
            message = str(self._scrub(str(exc)[:300]))
            out["published"] = True
            out["publish_reason"] = (
                f"the branch was pushed but no pull request was opened: {message}"
            )
            if message.startswith(PULL_REQUEST_REFUSED):
                # The forge answered and said no (`forge.open_pull_request`'s
                # 422, e.g. "No commits between main and swarm/..."): a fact
                # about this branch that a retry would meet again, so the
                # finish fails the step for it (`_published_nothing`). An
                # unreachable forge is not a refusal.
                out["pull_request_refused"] = message
            elif isinstance(exc, forge_mod.ForgeUnavailable):
                # An outage that outlasted the in-process retry: the finish
                # fails the ATTEMPT retryably for it (`_publish_unreachable`),
                # and the next attempt's 422 adopts a pull request this one
                # may have opened before its answer was lost.
                out[PUBLISH_UNREACHABLE_FIELD] = self._unreachable_marker(exc)
            return out

        updated = bool(getattr(pr, "updated", False))
        out.update(
            {
                "published": True,
                "pull_request": {
                    "number": pr.number,
                    "url": pr.url,
                    "state": pr.state,
                    "created": pr.created,
                    "updated": updated,
                },
                "publish_reason": (
                    "opened"
                    if pr.created
                    else "an open pull request already existed and was reused"
                    + (
                        (
                            ", with the agent's title and body"
                            if agent_title or agent_body
                            else ", retitled because its title carried the task id"
                        )
                        if updated
                        else ""
                    )
                ),
            }
        )
        self.log.info("pull request ready", number=pr.number, url=pr.url, created=pr.created)
        return out

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
            lines.append(f"- checkpoint: `{self._last_checkpoint.uri}`")
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

        # THE REVIEW THIS PULL REQUEST PASSED THROUGH (#264), on the page the
        # operator reads, so the verdict and what it found arrive with the
        # work instead of from a reviewer after it opens.
        if self._verdict is not None:
            lines += verdict_mod.pull_request_lines(self._verdict)

        return "\n".join(lines)

    def _worker_commit_message(self, fallback_subject: str, explanation: str) -> str:
        """The message of a commit the worker makes on the published branch.

        The subject is the agent's `pr-title.txt` when it wrote a usable one --
        the file, and the checks, `_agent_title` applies for the pull request,
        so the branch's own commit and its pull request say the same thing --
        and otherwise `fallback_subject`, which names no task (#361). The task
        id is in the body: a commit on the branch still says which task made
        it, on a line nobody reads as the change's title.
        """
        subject = self._agent_title([]) or fallback_subject
        # Prose, not a `Key: value` line, so nothing reads it as a trailer.
        return f"{subject}\n\n{explanation}\n\nMade by the worker for task {self.cfg.task_id}."

    def _agent_pull_request_text(self) -> tuple[str | None, str | None, list[str]]:
        """The agent's pull request title and body, each usable or None (#214).

        Returns `(title, body, refused)`. `refused` names each file that was
        there and could not be used, and why, in words that quote none of it.

        THE SAME RULES AS ANY OTHER AGENT TEXT THE WORKER PUBLISHES:

        * read with `open_without_following`, like every file taken out of the
          artifacts folder (#227): a link is refused, never followed;
        * UTF-8 or refused, and blank is refused;
        * the title is ONE line -- a second line is refused, not joined -- and
          neither may carry attribution (`gitops.ATTRIBUTION_MARKERS`, the
          list the push check reads): the owner's rule is none on GitHub, and
          the forge would carry it where the push check cannot see;
        * scrubbed of every registered secret, then every `@` that could
          mention given a zero-width joiner (`_neutralise_mentions`; a mention
          never refuses either file), THEN cut (the #232 rule): the title to
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
            elif _carries_attribution(text):
                refused.append(f"{PR_BODY_FILE}: carries attribution")
            else:
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
        no pull request. An absent file is the missing-output path's (#149).
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
        to `PR_TITLE_MAX_CHARS` (#232's rule): scrubbed first, or a cut prefix
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
        input names (`_title_from_issue_input`). Without it the agent's
        `pr-title.txt` is REQUIRED: a title made up from the prompt's first
        sentence or the runner profile described the request, not the change,
        and `f"[swarm] {task_id}"` described nothing. None here means no pull
        request is opened (`_publish_git`), and `_title_owed` makes a missing
        `pr-title.txt` a retryable failure of the attempt before that.
        """
        return self._title_from_issue_input(self._task or {})

    def _title_owed(self, task: dict[str, Any]) -> bool:
        """True when this attempt will open a pull request that only the agent
        can title: it may publish, its strategy opens one (`direct-pr`, or
        `integrate` as the integrator), it has a repository, and its `issue`
        input names no issue to title it from.
        """
        if not self.cfg.git_publish_enabled:
            return False
        if not (self.cfg.repository_url or task.get("repository_url")):
            return False
        # `_opens_pull_request`: `direct-pr`, the `integrate` integrator, and
        # a `single-pr` author, whose `pr-title.txt` is what the review sees
        # and the merge refuses any other title against (merge-step.md §3).
        return self._opens_pull_request() and self._title_from_issue_input(task) is None

    def _title_from_issue_input(self, task: dict[str, Any]) -> str | None:
        """"<the issue's title> (part of #N)", else "Work on issue #N (part of #N)".

        NEVER A CLOSING KEYWORD. This used to fall back to "Fixes #N", and a
        squash merge writes the title into a commit on the base branch, which
        GitHub reads for closing keywords: the issue closed on merge whatever
        the review found, and for an issue run the only correction was the
        API's keyword block (`issuesync.sync_pull_request`), which a single
        failed GitHub write left unapplied (review of #545). The platform's
        title therefore says what the work is and that it is `part of #N`;
        whether the pull request closes the issue is said in its body, by
        whatever decides completeness -- for an issue run, the API's block
        from the review's per-requirement verdict (owner decision on #454).
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
        all. `self._issue_title` is the hook the fetch fills in once it
        exists; until then it is always None and the number names the work.
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
        """The names the artifacts folder's cap takes before any other (#228).

        The expected outputs a later step declares, then the files the platform
        writes into the folder itself: the agent CLI's stream captures and
        transcript, and the harvest's patch. `artifact_manifest.upload_order`
        says why each comes first.
        """
        first = list(self._expected_outputs)
        files = agent_stream_files(self.cfg.runner_profile)
        if files is not None:
            first += files.names()
        first.append(PATCH_NAME)
        return first

    def _upload_outputs(
        self, *, publish: bool = False, withheld: str = "", defer_publish: bool = False
    ) -> dict[str, Any]:
        """Harvest, upload and describe what this attempt leaves behind.

        `defer_publish` is `_finalise`'s (#165): the harvest describes the work
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
        # THE CPU BESIDE IT (PR #210 review), for the exit whose runner is
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
        # WHICH FILES OF THE ARTIFACTS FOLDER, IN WHICH ORDER (#228, owner
        # decision of 2026-09-26): at most `max_artifact_files`, a declared
        # output first, no name past the manifest's bound; see
        # `agent_worker.artifact_manifest`. Decided BEFORE anything is copied,
        # so only what is going to leave the pod is read and redacted.
        #
        # WALKED WITHOUT RECURSION (#227). `Path.rglob` recursed once per
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
            # MORE DECLARED NAMES THAN THE MANIFEST'S BUDGET ALLOWS (#227): the
            # ones past `MAX_DECLARED_NAMES` lost their exemption from the name
            # bound in `plan`, which logs nothing itself. Named here, through
            # this logger, because the names come from task metadata: SCRUBBED
            # FIRST, then cut, the order the #232 review set for every name.
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
        # WHY each one (#165), index for index with `skipped`: the
        # missing-output check reads it to decide whether a retry can help.
        skip_causes: list[str] = []
        for folder in too_deep:
            # Scrubbed, then cut, like every name below (#232 review).
            skipped.append(standalone_mod.shown(self._scrub(folder)))
            skip_causes.append(expected_mod.CAUSE_REFUSED)
        for name in plan.unstorable:
            # A name whose bytes are not UTF-8 (#225 review): no object can be
            # named with it, and as a lone surrogate in the summary it made
            # `finish` raise, and the next attempt fail the same way. Named as
            # the bytes it was, in the log and the skipped list.
            # SCRUBBED FIRST (#232 review): cutting a raw name let a
            # registered secret crossing character 256 survive in part.
            shown = standalone_mod.shown(self._scrub(name))
            self.log.warning(
                "an artifact's name is not valid UTF-8, so no object can be "
                "named with it; not uploaded",
                artifact=shown,
            )
            skipped.append(shown)
            skip_causes.append(expected_mod.CAUSE_REFUSED)
        for entry in plan.not_uploaded:
            if entry["reason"] == manifest_mod.NAME_TOO_LONG:
                # Listed as #225 lists a name it could not upload: cut short,
                # so 50 of them cannot take the summary near 1 MiB either.
                # SCRUBBED FIRST (#232 review): cutting a raw name let a
                # registered secret crossing character 256 survive in part.
                skipped.append(standalone_mod.shown(self._scrub(entry["name"])))
                skip_causes.append(expected_mod.CAUSE_REFUSED)
        # Past the cap: COUNTED in the summary (`artifacts_over_cap`, below),
        # and every name in the log -- the owner's shape for #228, through the
        # helper #225's working-folder cap uses. Not in `artifacts_skipped`,
        # whose readers call a name there dropped at the SIZE cap. A name over
        # the bound is logged cut short, as #225 logs one: a path runs to
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
                # SCRUBBED, THEN CUT (#227), as every other name this list
                # holds: a raw name ran to 1,024 bytes for a declared output,
                # and was scrubbed only later, with the summary, after
                # nothing had cut it.
                skipped.append(standalone_mod.shown(self._scrub(rel)))
                skip_causes.append(_skip_cause(sent.skipped))
                continue
            # Listed only for a file that LEFT (#227): one the byte cap or an
            # upload failure kept in the pod was never uploaded "as-is".
            if sent.unredacted is not None:
                unredacted.append(sent.unredacted)
            total += sent.bytes
            artifacts.append({"name": rel, "bytes": sent.bytes, "uri": self.store.uri(key)})
        # LISTED IN PATH ORDER, as the manifest always has been: the order
        # above decides which files are taken, not the order a reader sees
        # them in, nor which of them the listing route's first page holds.
        artifacts.sort(key=lambda entry: entry["name"].split("/"))

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
            # a name there dropped at THIS folder's size cap (#225 review).
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
        # Through the same no-follow copy as an artifact (#227): the check for
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

        if skipped:
            self.log.warning(
                "artifacts skipped", count=len(skipped), cap_bytes=self.cfg.max_artifact_bytes
            )
        summary: dict[str, Any] = {
            "artifacts": artifacts,
            "artifact_bytes": total,
            "logs": logs,
            "agent_streams": self._agent_streams(agent_files, artifacts),
        }
        if git_summary is not None:
            summary["git"] = git_summary
        if workdir is not None:
            summary["workdir_outputs"] = workdir["summary"]
        if skipped:
            # Each entry names the file and WHY it was skipped (#165): the
            # cap, an upload error, a refused file. Read through
            # `expected_mod.skipped_entries`, which also accepts the bare
            # names older summaries hold.
            summary["artifacts_skipped"] = [
                {"name": name, "cause": cause}
                for name, cause in zip(skipped[:50], skip_causes[:50])
            ]
        # Every skipped name's cause, uncut, for this attempt's own check: the
        # summary's list stops at 50, and a declared output can be the 51st.
        self._skip_causes = dict(zip(skipped, skip_causes))
        if plan.over_cap:
            # How many files the folder held past the cap, and the cap, so a
            # reader can say "N over the 500-file cap" without restating the
            # number (#228). Absent when every file fitted: a zero would read
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

        `stdout_truncated` / `stderr_truncated` (#188 review) say whether the
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

    # -- spend -------------------------------------------------------------
    def _child_ended(self) -> None:
        """Everything that has to happen the moment a runner is gone.

        Called at every site that reaps a runner, so a new site gets both
        halves by calling one thing: the resource sample is stopped and
        written, and the run's spend is taken before a restart can overwrite
        the result.json it is in.
        """
        self._stop_sampler()
        self._collect_spend()

    def _collect_spend(self) -> None:
        """Add the runner that just ended to this attempt's spend. Never raises.

        ONCE PER RUNNER: `_spend_pending` is set when a runner starts and
        cleared here, so the `_cleanup` backstop cannot count a run twice.

        Reads result.json knowing it is THIS runner's or nothing: the reply
        files are cleared before every start (`_run_child_supervised`), so a
        runner killed before writing leaves no file here rather than a
        restored one from a previous attempt.
        """
        if not self._spend_pending or self.ws is None:
            return
        self._spend_pending = False
        try:
            result = _read_json(self.ws.result_path) or {}
            usage = _usage_summary(result.get("output"))
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.warning("could not read the runner's spend", error=str(exc))
            return
        if usage:
            self._spend = _add_spend(self._spend, usage)

    def _record_spend(self) -> None:
        """Write what this attempt has spent onto its attempt document. Never raises.

        Called from every exit (see `_upload_outputs` and `_cleanup`), and
        writes only when there is something new: `record_spend` merge-sets
        absolute totals, so writing the same figure twice is harmless and
        writing a larger one later -- a crash that reaped its runner after the
        first write -- corrects it.

        Not fatal. An attempt that ran is not a failed attempt because its
        accounting write failed, and this runs on the teardown path where the
        lease is about to be released either way.
        """
        if self._writes_forbidden or not self._spend or self._spend == self._spend_recorded:
            return
        snapshot = dict(self._spend)
        try:
            self.control.record_spend(snapshot)
        except Exception as exc:
            self.log.warning("could not record spend", error=f"{type(exc).__name__}: {exc}")
            return
        self._spend_recorded = snapshot

    # -- metrics -----------------------------------------------------------
    def _start_sampler(self, child: ChildProcess) -> None:
        ws = self.ws
        assert ws is not None
        # THE CONTAINER'S MEMORY, NOT THE PROFILE'S (#205). The dispatcher
        # sizes the container from the task's class, and a workflow step may
        # narrow `browser` (16 GiB) to `standard` (8 GiB): judged against the
        # profile's 16, a run at 7.9 of its 8 GiB raised no near miss.
        self._sampler = ResourceSampler(
            pid_provider=lambda: child.pid,
            disk_provider=ws.disk_bytes,
            memory_limit_bytes=self.cfg.memory_limit_bytes_of(self._memory_class()),
        )
        self._sampler.start()

    def _stop_sampler(self) -> None:
        if self._sampler is None:
            return
        usage = self._sampler.stop()
        # Kept, not replaced by the next runner's sampler: that replacement is
        # what made every figure below describe the last runner only.
        self._runner_usage.append(usage)
        self._sampler = None
        # THE ATTEMPT'S PEAKS, not this runner's. `record_resource_usage`
        # merge-sets absolute values, so writing each runner's own peak let a
        # quiet second runner overwrite the first runner's high-water mark --
        # and a near miss in the first -- on the one document a sizing report
        # reads.
        attempt = self._attempt_usage() or usage
        # NOT FATAL, AND IT MUST NOT SKIP THE CPU WRITE BELOW (PR #210 review).
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
        except Exception as exc:
            self.log.warning("could not record resource usage", error=f"{type(exc).__name__}: {exc}")
        if usage.oom_near_miss:
            self.log.error(
                "OOM NEAR MISS: this attempt came within a hair of its memory limit",
                peak_rss_bytes=usage.peak_rss_bytes,
                limit_bytes=self.cfg.memory_limit_bytes_of(self._memory_class()),
                resource_class=self._memory_class(),
            )
        # The attempt's CPU at this runner's end, on the attempt (request #15)
        # -- where #188 emitted a `final` HEARTBEAT into the task's events.
        self._record_cpu()

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

    def _export_metrics(self) -> None:
        # Once per attempt. Several exits reach this, and one that failed
        # partway -- an export that raised -- falls through to the crash
        # handler, which calls it again; a successful export is not repeated.
        if self._metrics_exported:
            return
        usage = self._attempt_usage()
        if usage is None:
            return
        self.metrics.export(
            usage,
            {
                "tenant_id": self.cfg.tenant_id,
                "runner_profile": self.cfg.runner_profile,
                # The sized class (#205): the sizing decisions read this label,
                # and the profile's would file a narrowed step under a class
                # its container was not.
                "resource_class": self._memory_class(),
                "backend": self.cfg.backend,
                "attempt_id": self.cfg.attempt_id,
                "task_id": self.cfg.task_id,
            },
        )
        self._metrics_exported = True

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

    def _safe_finish(
        self,
        state: TaskState,
        *,
        exit_code: int,
        error: str,
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
        """
        try:
            summary = self._upload_outputs()
            self._add_runner_block(summary)
            if extra_summary:
                summary = {**(summary or {}), **extra_summary}
            self._export_metrics()
            self.control.finish(
                state=state,
                exit_code=exit_code,
                error=self._scrub(error[:4000]),
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
        if self._child is not None:
            try:
                if self._child.poll() is None:
                    self._child.terminate(self.cfg.termination_grace_seconds, reason="cleanup")
                    self._child.finish()
            except Exception:
                pass
        # THE SPEND BACKSTOP, after the runner is gone and before the workspace
        # holding its result.json is destroyed. Every orderly exit has already
        # recorded in `_upload_outputs`; this catches the two that cannot have:
        # a crash while a runner was still alive (it has only just been reaped,
        # above) and a mid-run fence, which uploads nothing. Writing a fenced
        # attempt's spend touches only its OWN attempt document -- never the
        # lease, which is what invariant 5 forbids -- exactly as the resource
        # usage write on that same path already does.
        self._collect_spend()
        self._record_spend()
        # And the CPU, for the same two exits: a runner killed just above was
        # never reaped through `_stop_sampler`, so its last stretch is on no
        # attempt document yet. The live sampler is still attached, so
        # `_attempt_usage` includes it. Never raises; nothing on the fenced
        # path touches the lease by writing its own attempt document.
        self._record_cpu()
        # AFTER the child is gone and BEFORE the workspace is destroyed. Giving
        # the account back while an agent could still be making calls on it
        # would let the broker hand the same subscription to another agent and
        # count one where there are two. This is the single release point for
        # every exit path -- see `_release_account`.
        self._release_account()
        if self.ws is not None:
            workspace_mod.destroy(self.ws)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------



def _carries_attribution(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ATTRIBUTION_MARKERS)


#: Every `@` that could start a GitHub mention, written literally, as a
#: character reference (`&#64;`, `&#x40;`, `&commat;`, or a numeric one with
#: no semicolon, which an HTML parser still decodes), or as the fullwidth
#: `U+FF20`. OWNER DECISION, 2026-09-29 (#259): the agent's `pr-title.txt` and
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
#: and `café@octocat` DOES page `@octocat` on GitHub (#259 review, M3).
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
#: tests/unit/worker/test_pr_body_console_links.py pins the two spellings equal.
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
    # (tests/unit/scripts/test_docs_spec_amendments.py), and a line added above
    # them moves every citation.
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


#: A trailer line as git reads one: `Token: value`, the token letters, digits
#: and hyphens. `Co-Authored-By:`, `Signed-off-by:`, `Change-Id:`.
_TRAILER_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*[ \t]*:[ \t]")


def clean_commit_message(message: str, *, scrub: Callable[[str], str], fallback: str) -> str:
    """An agent's commit message as the worker pushes it (#242).

    * every line carrying attribution (`gitops.ATTRIBUTION_MARKERS`) is
      removed, wherever it is -- the "Generated with" footer is a paragraph of
      its own, not a trailer;
    * then the trailer block -- a closing paragraph of `Token: value` lines,
      as git reads one -- is removed, more than once if there are several;
      the subject is never taken for one;
    * registered secrets are scrubbed, then the message is cut to
      `COMMIT_MESSAGE_MAX_BYTES`;
    * a message with nothing left is `fallback`, the worker's own.

    NULs are dropped: an argv cannot carry one, and a commit message can.
    """
    text = message.replace("\x00", "").replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    lines = [line for line in lines if not _carries_attribution(line)]
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    paragraphs = [p for p in paragraphs if p]
    while len(paragraphs) > 1 and _is_trailer_block(paragraphs[-1]):
        paragraphs.pop()
    cleaned = scrub("\n\n".join("\n".join(p) for p in paragraphs)).strip()
    if not cleaned:
        return fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) > COMMIT_MESSAGE_MAX_BYTES:
        cleaned = (
            encoded[:COMMIT_MESSAGE_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
            + f"\n\n[cut at {COMMIT_MESSAGE_MAX_BYTES} bytes by the worker]"
        )
    return cleaned


def _is_trailer_block(paragraph: list[str]) -> bool:
    """True when every line is a trailer, or continues the one above it."""
    if not _TRAILER_LINE.match(paragraph[0]):
        return False
    return all(_TRAILER_LINE.match(line) or line[:1] in (" ", "\t") for line in paragraph)


#: How much added text one window of the leak scan holds (`_DiffLeakScanner`).
#: OWNER DECISION, 2026-09-29 (#259): a diff is scanned WHOLE however big it
#: is, in windows of this size, rather than refused or cut at a cap. Sized so
#: a window is cheap to hold and to run every rule over, not as a limit.
SCAN_WINDOW_CHARS = 4 * 1024 * 1024

#: How much of one window is scanned again at the start of the next, so a
#: credential straddling the cut is still seen whole in one of them. It must
#: be at least the longest credential match and the longest registered
#: secret; the callers add the longest registered secret to this. Every
#: credential rule matches within one line, and a key/value assignment or a
#: provider token is far under 64 KiB. A PEM block's marker is what its rule
#: finds, and the marker is 40 characters.
SCAN_OVERLAP_CHARS = 64 * 1024

#: The agent's environment variable holding the base the publish diffs from
#: (`Worker._build_child_env`), so `python -m agent_worker.publish_scan` scans
#: the same added text the worker will. Without it the CLI guessed HEAD on a
#: `--depth 1` clone of a pinned SHA or a non-main ref, and every COMMITTED
#: secret was outside its diff (#470's review). A description of the work,
#: never trusted for the push: the publish keeps reading `_publish_base`.
CLONE_BASE_ENV = "SWARM_CLONE_BASE"


class CredentialHit(NamedTuple):
    """What a leak predicate found in one window of a file's added text: the
    rule that matched (a `swarm_redaction` rule name, or
    `REGISTERED_SECRET_RULE`) and where the match starts. Never the value."""

    rule: str
    offset: int


class ScanHit(NamedTuple):
    """The first leak a diff scan found: the file, the rule, and the line of
    the new file it is on. This is what a refusal names (#373), and it holds
    no part of the value."""

    path: str
    rule: str
    line: int


#: The predicate both publish scans ask about each file's added text:
#: `(path, text) -> CredentialHit | None` (`Worker._leaks_in_added_text`).
#: The path is the file's, so the guard can be tiered by it (#373).
LeakPredicate = Callable[[str, str], "CredentialHit | None"]

#: The rule name a refusal gives for a task's registered secret.
REGISTERED_SECRET_RULE = "registered_secret"

#: The new-file start line of a `-U0` hunk header: `@@ -a[,b] +c[,d] @@`.
_HUNK_NEW_START = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)")


def _first_difference(original: str, scrubbed: str) -> int:
    """Where `scrubbed` first differs from `original`: the start of the first
    registered value the scrub replaced, near enough to name its line."""
    limit = min(len(original), len(scrubbed))
    for index in range(limit):
        if original[index] != scrubbed[index]:
            return index
    return limit


class _DiffLeakScanner:
    """Scans a `git diff` stream's ADDED text, file by file, in bounded windows.

    `feed` takes the diff's bytes as they arrive (`gitops._git_stream`), in
    chunks of any size; `close` ends the stream and returns the first hit --
    the file, the rule `leaks` named, and the new file's line (`ScanHit`) --
    or None. `leaks` is asked with the file's path, so the guard is tiered by
    it (#373). Nothing is held but the
    current window, the overlap carried from the one before, and one line's
    first few characters -- a 40 MB single-line file costs a window, not 40 MB.

    The diff is parsed as `_added_by_file` parses a whole one: a `+++ `
    line is a file's header only before its first `@@`, and every `+` line
    after is content. Bytes are decoded incrementally as UTF-8 with
    replacement, and no newline translation happens, so a lone `\\r` stays
    inside its line (#259 review, M2).

    WINDOWS OVERLAP. When a file's added text passes `window` characters, it
    is scanned and all but its last `overlap` characters are dropped; those
    are scanned again with what follows. The carry starts at a line boundary
    when one is within another `overlap` of the cut, so a match at the start
    of a window reads the true character before it. On a single line longer
    than that the carry starts mid-line, where a pattern starting the window
    counts as starting a token: a possible false hit, which folds or refuses
    -- the safe direction.
    """

    _HEADS = ("diff --git ", "+++ ", "@@")

    def __init__(self, leaks: LeakPredicate, *, window: int, overlap: int) -> None:
        import codecs

        self._leaks = leaks
        self._window = max(window, 2 * overlap + 1)
        self._overlap = overlap
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.hit: ScanHit | None = None
        self._path = ""
        # WHERE A HIT IS, IN THE FILE'S OWN LINES (#373): the refusal names the
        # line. `_file_lines` counts the added lines fed so far, `_window_line`
        # is the added-line index the current window starts at, and `_hunks`
        # maps the added-line index each `@@` hunk starts at to the new-file
        # line it starts at. With `-U0` a hunk's added lines are consecutive.
        self._file_lines = 0
        self._window_line = 0
        self._hunks: list[tuple[int, int]] = []
        self._in_header = True
        self._mode: str | None = None  # None (line start), "add", "header", "skip"
        self._head = ""
        self._header_line = ""
        self._buf: list[str] = []
        self._buf_len = 0
        self._carry = ""

    def feed(self, data: bytes) -> None:
        if self.hit is None:
            self._text(self._decoder.decode(data))

    def close(self) -> ScanHit | None:
        if self.hit is None:
            self._text(self._decoder.decode(b"", final=True))
        if self.hit is None:
            self._end_line()
            self._flush_file()
        return self.hit

    # -- parsing -------------------------------------------------------------

    #: Runs of whole content lines, taken in one step rather than a line at a
    #: time: a 40 MB diff is millions of lines, and a Python step per line
    #: made the scan the slowest thing in the publish. Inside a hunk a line
    #: starting `+` is added content, and `-`, ` ` or `\\` is not; neither
    #: run can hold a `diff --git` or `@@` line, which start otherwise.
    _ADDED_RUN = re.compile(r"(?:\+[^\n]*\n)+")
    _OTHER_RUN = re.compile(r"(?:[-\\ ][^\n]*\n)+")

    def _text(self, text: str) -> None:
        i, n = 0, len(text)
        while i < n and self.hit is None:
            if self._mode is None and not self._head and not self._in_header:
                run = self._ADDED_RUN.match(text, i)
                if run is not None:
                    # Each line's one `+` prefix goes: the first by the
                    # slice, every other as the character after a newline.
                    self._add(run.group()[1:].replace("\n+", "\n"))
                    i = run.end()
                    continue
                run = self._OTHER_RUN.match(text, i)
                if run is not None:
                    i = run.end()
                    continue
            newline = text.find("\n", i)
            end = n if newline < 0 else newline
            if self._mode is None:
                need = 11 - len(self._head)
                take = text[i : min(end, i + need)]
                self._head += take
                i += len(take)
                complete = i == end and newline >= 0
                if not complete and len(self._head) < 11 and i >= n:
                    if any(p.startswith(self._head) for p in self._HEADS):
                        return  # undecided until more arrives
                self._classify()
                continue
            segment = text[i:end]
            if self._mode == "add":
                self._add(segment)
            elif self._mode in ("header", "hunk") and len(self._header_line) < 64 * 1024:
                self._header_line += segment
            i = end
            if newline >= 0:
                i += 1
                self._end_line()

    def _classify(self) -> None:
        head = self._head
        if head.startswith("diff --git "):
            self._flush_file()
            self._path, self._in_header, self._mode = "", True, "skip"
        elif head.startswith("@@"):
            self._in_header, self._mode, self._header_line = False, "hunk", head
        elif self._in_header:
            if head.startswith("+++ "):
                self._mode, self._header_line = "header", head
            else:
                self._mode = "skip"
        elif head.startswith("+"):
            self._mode = "add"
            self._add(head[1:])
        else:
            self._mode = "skip"

    def _end_line(self) -> None:
        if self._mode is None and self._head:
            self._classify()
        if self._mode == "add":
            self._add("\n")
        elif self._mode == "header":
            name = self._header_line[4:].rstrip("\r")
            if len(name) > 1 and name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            self._path = name[2:] if name.startswith("b/") else name
        elif self._mode == "hunk":
            start = _HUNK_NEW_START.match(self._header_line)
            if start is not None:
                self._hunks.append((self._file_lines, int(start.group(1))))
        self._mode, self._head, self._header_line = None, "", ""

    # -- scanning ------------------------------------------------------------

    def _add(self, text: str) -> None:
        if not text:
            return
        self._file_lines += text.count("\n")
        self._buf.append(text)
        self._buf_len += len(text)
        if self._buf_len >= self._window:
            self._scan(last=False)

    def _scan(self, *, last: bool) -> None:
        text = self._carry + "".join(self._buf)
        self._buf, self._buf_len = [], 0
        found = self._leaks(self._path, text) if text else None
        if found:
            self.hit = ScanHit(
                self._path or "a file",
                found.rule,
                self._file_line(self._window_line + text.count("\n", 0, found.offset)),
            )
            return
        if last:
            self._carry = ""
            return
        cut = max(len(text) - self._overlap, 0)
        line_start = text.rfind("\n", max(cut - self._overlap, 0), cut)
        carry_from = line_start + 1 if line_start >= 0 else cut
        self._window_line += text.count("\n", 0, carry_from)
        self._carry = text[carry_from:]

    def _file_line(self, added_index: int) -> int:
        """The new file's 1-based line number of the added line at `added_index`."""
        line = added_index + 1
        for first, start in self._hunks:
            if first > added_index:
                break
            line = start + (added_index - first)
        return line

    def _flush_file(self) -> None:
        if self._buf:
            self._scan(last=True)
        self._carry = ""
        self._file_lines, self._window_line, self._hunks = 0, 0, []


def _scan_diff_stream(
    argv: list[str],
    *,
    leaks: LeakPredicate,
    overlap: int,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
) -> tuple[int, ScanHit | None]:
    """Run a `git diff` and scan everything it adds; (exit code, the first hit or None)."""
    scanner = _DiffLeakScanner(leaks, window=SCAN_WINDOW_CHARS, overlap=overlap)
    code = _git_stream(
        argv,
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug=slug,
        timeout_seconds=timeout_seconds,
        logger=logger,
        consume=scanner.feed,
    )
    return code, scanner.close()


def _adds_a_credential(diff: str) -> bool:
    """True when a line this diff ADDS matches a credential pattern, each
    file judged by its own path (`_credential_in`, #373).

    The patterns are `swarm_redaction.RULES`, the same families swarm-api
    masks at read time (owner decision 4, 2026-09-28), so "credential-shaped"
    means one thing on the platform. Only added lines are read: a removed line
    was in the parent's tree already -- the repository's own history, or an
    earlier agent commit, whose own diff added it and is scanned in its turn --
    and `+++ ` is a file header, not content.

    A MATCH MUST START A TOKEN. swarm-api masks a run wherever it starts,
    because there a false positive costs one masked word. Here it costs the
    agent's whole history (a fold), and code is full of identifiers that hold
    a family's prefix in the middle: `keyword_only` holds `eyword_only`, which
    the JWT rule takes for a token. So a pattern match counts only when the
    character before it is not a letter, a digit or `_`. The private-key
    block is exempt: its marker is never part of an identifier.
    """
    return any(_credential_in(path, added) is not None for path, added in _added_by_file(diff))


def _added_by_file(diff: str) -> list[tuple[str, str]]:
    """Each file in a `git diff` that adds text, with that text, `+` removed.

    A file's `+++ b/<path>` line is read as its header only BEFORE its first
    `@@` hunk line. After that every line starting with `+` is content, even
    one that reads `+++ ...`: an added line whose own text is `++ AKIA...`
    prints as `+++ AKIA...`, so dropping every `+++ ` line as a header let a
    credential through behind two plus signs. The path is what the `+++ `
    header names after the `b/` destination prefix; a deleted file
    (`+++ /dev/null`) adds nothing. Text before any `diff --git` line is read
    as one file, named by its own `+++ ` header if it has one.
    """
    files: list[tuple[str, list[str]]] = []
    path = ""
    added: list[str] = []
    # A file's header runs from its `diff --git` line (or the start of the
    # text) to its first `@@`: git prints every hunk behind one.
    in_header = True
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            if added:
                files.append((path, added))
            path, added, in_header = "", [], True
            continue
        if line.startswith("@@"):
            in_header = False
            continue
        if in_header:
            if line.startswith("+++ "):
                name = line[4:]
                if len(name) > 1 and name.startswith('"') and name.endswith('"'):
                    name = name[1:-1]
                path = name[2:] if name.startswith("b/") else name
            continue
        if line.startswith("+"):
            added.append(line[1:])
    if added:
        files.append((path, added))
    return [(name, "\n".join(lines)) for name, lines in files]


# -- the tiered, path-aware publish guard (#373) ------------------------------
#
# OWNER DECISION, 2026-09-30 (#373). The publish scans refused any added text
# the generic credential rules match, and a test of the redaction rules has to
# hold exactly such text (`"password": "********"`, `secret="<name>"`), so no
# SwarmCloud agent could publish one (#327's two runs, #376, #379). The guard
# is tiered by the file's path:
#
# * TIER 1, every file, always refused: a registered secret (checked by the
#   caller, `Worker._leaks_in_added_text`, never relaxed); a vendor-shaped key,
#   whose tail must carry an explicit placeholder IN A TEST PATH, so an
#   `sk-test-aaaa` fixture passes there and is refused anywhere else; a JWT
#   whose header decodes to JSON naming `alg`; any private key.
#   PRIVATE KEYS FOLLOW MAIN'S RULE IN EVERY PATH AND TIER: refused exactly
#   when `swarm_redaction.rules.mask_private_keys` masks something, so any
#   BEGIN ... PRIVATE KEY marker and any orphan-END tail is refused, a stub
#   marker under `tests/` included. Owner decision, 2026-10-01: four review
#   rounds of relaxing it (body length, entropy, DER shape, windows) each
#   found inputs main refuses and the branch published. #373's real blocker
#   was the generic key=value rule in redaction tests, not PEM markers; a
#   test builds its marker at runtime.
# * TIER 2, outside test paths: the generic rules refuse as before, except
#   for a REFERENCE (owner decisions, 2026-10-02; `_is_a_reference`): a value
#   that is wholly one `${name}` slot, or -- not under a password name and
#   not after an `Authorization` scheme -- one short lowercase word or a
#   KNOWN reference shape. Vendor rules are not relaxed.
# * TIER 3, in test paths: a generic match refuses only when its value looks
#   like a credential (`_looks_like_a_credential`). The loose tier is test
#   CODE only (`is_test_path`).
#
# `swarm_redaction.RULES` is unchanged: read-time masking shares it, and a
# false positive there costs one masked word, not a refused publish.
#
# RESIDUAL RISK, accepted by the owner: a weak real password committed under a
# test path is published. Since 2026-10-02, outside test paths, tier 2 also
# publishes, as a reference, a WHOLE value (`_ends_the_value`: nothing joined
# to it by `+`, `,` or another literal, and an escaped quote does not close
# it) that is one of:
#   - a single lowercase word of ANY length (no cap since the owner's decision
#     of 2026-10-02; an all-lowercase, digitless random token outside test
#     paths therefore publishes, a residual risk the owner accepted), no `-`,
#     `.`, `_` or digit (`anthropic`, `retained`) -- so a weak
#     one-word secret under a `token`/`secret`/`api_key` name, or after a
#     prose `Bearer`/`Basic` (`http_authorization`), publishes. There is no
#     dictionary: `letmein` under `token = ` publishes. Under a name ending
#     `password`/`passwd` it does NOT: that name is assigned the password
#     itself, so only a slot is a reference there;
#   - a `swarm-tenant-` secret name, or a hyphen-joined lowercase name ending
#     `-api-key`, `-token`, `-secret` or `-git` (`hunter-horse-token` too);
#   - a dotted path under `var.`, `local.`, `user_config.`, `data.` or
#     `module.`.
# Any other hyphen- or dot-joined value (`correct-horse-battery-staple`) is
# refused, and so is every bare word after an `Authorization:` header's
# scheme, whatever its case. A `${name}` slot holds no value, so it adds no
# risk. CI's trivy secret scan still runs after the push.

#: The `swarm_redaction` rules that match a NAME followed by a value rather
#: than a provider's own token format. Tiers 2 and 3 apply to these. Any rule
#: not named here, `jwt` or `private_key_block` is treated as a vendor rule --
#: the stricter tier -- so a rule added to `RULES` is never silently relaxed.
GENERIC_CREDENTIAL_RULES = frozenset(
    {"key_value_assignment", "http_authorization", "http_authorization_scheme"}
)

#: A vendor key's own prefix, stripped before its tail is judged.
_VENDOR_PREFIX = re.compile(r"(?:sk-|ya29\.|AIza|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|ASIA)")

#: What a value needs to look like a credential (owner decision, 2026-09-30):
#: at least this many characters...
CREDENTIAL_MIN_CHARS = 16
#: ...and at least this much Shannon entropy per character. Random base62 of
#: 16 distinct characters is 4.0 bits; English words and repeated fixtures
#: (`p4ssw0rd-p4ssw0rd` is 3.0) sit below.
CREDENTIAL_MIN_ENTROPY_BITS = 3.5

#: A value holding any of these is a placeholder, not a credential: a mask,
#: a run of x, a `<name>` or `${VAR}` slot, or a word that says so.
_PLACEHOLDER = re.compile(r"\*|x{4,}|<[^>]*>|\$\{[^}]*\}|fake|test|dummy|example", re.IGNORECASE)

#: Where a refusal names a private key: its first BEGIN or END marker.
_PEM_MARKER = re.compile(r"-----(?:BEGIN|END) [A-Z ]*PRIVATE KEY-----")

#: Directory names that make a CODE file under them a test file (compared
#: lower-cased, so `Tests/x.py` is the same as `tests/x.py`). `fixtures/` and
#: `testdata/` are NOT here: they hold data, and data is judged strictly.
_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
#: Only these extensions can be test CODE (owner decision, 2026-10-01). The
#: loose tier exists so a test may assert on `password="hunter2"`; a config or
#: data file (.env, .yaml, .json, .toml, .pem, no extension...) is where real
#: credentials live, so it is judged strictly even under `tests/`.
_TEST_CODE_EXTENSIONS = frozenset(
    {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
     ".kt", ".rb", ".sh", ".bats"}
)
#: File names that are test files wherever they sit (matched lower-cased).
_TEST_FILE = re.compile(r"(?:test_.+\..+|.+_test\..+|.+\.(?:test|spec)\..+)")
#: NEVER a test path, even under `tests/`: the files real credentials live in.
#: Matched against the file name; `*credential*` also against every directory.
_NEVER_TEST_FILES = (".env*", "*.pem", "*.key", "*credential*", "*secret*.json")


def is_test_path(path: str) -> bool:
    """True when `path` (repository-relative, `/`-separated) is test CODE for
    the publish guard's loose tier (owner decisions, 2026-09-30 and
    2026-10-01, #373).

    Both must hold: the file has a code extension (`_TEST_CODE_EXTENSIONS`),
    and it sits under a `tests/`, `test/` or `__tests__/` directory at any
    depth (case-insensitive) or is named `test_*`, `*_test.*`, `*.test.*` or
    `*.spec.*`. A file named `*credential*` -- or under a `*credential*`
    directory -- never is. An empty or unknown path is not one: the guard
    falls to its stricter tier when it cannot tell.
    """
    if not path:
        return False
    parts = path.split("/")
    name = parts[-1].lower()
    if os.path.splitext(name)[1] not in _TEST_CODE_EXTENSIONS:
        return False
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _NEVER_TEST_FILES):
        return False
    if any("credential" in part.lower() for part in parts[:-1]):
        return False
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return True
    return _TEST_FILE.fullmatch(name) is not None


def _shannon_bits(value: str) -> float:
    """Shannon entropy of `value`'s characters, in bits per character."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    size = len(value)
    return -sum((n / size) * math.log2(n / size) for n in counts.values())


def _looks_like_a_credential(value: str) -> bool:
    """True when `value` is shaped like a real credential rather than a
    fixture: at least `CREDENTIAL_MIN_CHARS` characters, letters AND digits,
    at least `CREDENTIAL_MIN_ENTROPY_BITS` of entropy per character, and no
    placeholder (`_PLACEHOLDER`)."""
    if len(value) < CREDENTIAL_MIN_CHARS:
        return False
    if _PLACEHOLDER.search(value):
        return False
    if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        return False
    return _shannon_bits(value) >= CREDENTIAL_MIN_ENTROPY_BITS


def _decodes_as_a_jwt(token: str) -> bool:
    """True when `token`'s first segment base64url-decodes to a JSON object
    naming `alg`, which every JWT header does. `eyword_only_args`, or an
    `ey...` run in a fixture, is not one."""
    header = token.split(".", 1)[0]
    try:
        decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
    except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError all are
        return False
    return isinstance(decoded, dict) and "alg" in decoded


#: Words that mark a vendor-shaped fixture as one.
_VENDOR_PLACEHOLDER = re.compile(r"example|test|fake|x{4,}", re.IGNORECASE)


def _is_explicit_placeholder(tail: str) -> bool:
    """True when a vendor token's variable part says it is a fixture: it holds
    EXAMPLE, test, fake or a run of four x, or is one repeated character."""
    return (
        _VENDOR_PLACEHOLDER.search(tail) is not None
        or (len(tail) >= 4 and len(set(tail)) == 1)
    )


#: A value that is wholly one interpolation slot holding only a NAME:
#: `${user_config.client_secret}`, `${var.anthropic}`. Anything before or after
#: it, or a second slot, is not. Nor is a slot carrying an operator --
#: `${DB_PW:-<literal>}` (or `-`, `=`, `:=`, `:+`, `:?`) expands to the literal
#: it holds -- which the owner's `^\$\{[^}]+\}$` would have let through.
_INTERPOLATION_SLOT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_.]*\}")
#: One lowercase word of ANY length (owner decision, 2026-10-02: the single-word
#: length cap is removed): no `-`, `.`, `_` or digit to join it to another.
#: RESIDUAL RISK, accepted by the owner: an all-lowercase, digitless random
#: token (32 letters carry ~150 bits, but a weak one-word secret carries few)
#: under a non-password name, outside test paths, publishes. Only
#: `_REFERENCE_MAX_CHARS` bounds it. Under a password name it is still refused.
_REFERENCE_WORD = re.compile(r"[a-z]+")
#: The KNOWN shapes of a multi-segment reference (owner decision, 2026-10-02,
#: #470's review), and no other: a tenant secret's name (`swarm-tenant-eng-git`),
#: a name that says it names a credential (`anthropic-api-key`, `github-token`,
#: `db-secret`, `tenant-git`), and an interpolation path under a root that
#: Terraform, a plugin manifest or a module reads (`var.`, `local.`,
#: `user_config.`, `data.`, `module.`). `correct-horse-battery-staple` and
#: `correct.horse.battery.staple` are none of them, and are refused.
#: RESIDUAL RISK, accepted by the owner (2026-10-02): these shapes are UNCAPPED
#: in segments. A passphrase with a known prefix or suffix publishes outside
#: test paths -- `correct-horse-battery-staple-token`, `swarm-tenant-<words>`,
#: a `var.<words>` path -- up to the 64-character bound. No segment cap.
_REFERENCE_SHAPE = re.compile(
    r"swarm-tenant(?:-[a-z]+)+"
    r"|[a-z]+(?:-[a-z]+)*-(?:api-key|token|secret|git)"
    r"|(?:var|local|user_config|data|module)(?:\.[a-z_]+)+"
)
#: The longest value a shape may be: a name, not a passphrase.
_REFERENCE_MAX_CHARS = 64
#: The words an `Authorization` value opens with. Never a reference: it is
#: the scheme, and the credential follows it (#470's review).
_AUTH_SCHEME_WORDS = frozenset(
    {"bearer", "basic", "token", "digest", "negotiate", "oauth", "ssws"}
)


def _is_a_reference(value: str, rule: Any = None, match: re.Match[str] | None = None) -> bool:
    """True when a generic rule's matched `value` NAMES a credential rather
    than holding one, so tier 2 does not refuse it (owner decisions,
    2026-10-02).

    WHY. Three of six SwarmCloud lanes were refused at publish for text that
    held nothing: plugin.json's unchanged `"..._CLIENT_SECRET":
    "${user_config.oauth_client_secret}"`, re-added by a trailing comma, a
    Terraform local naming the Anthropic key with the provider id
    `anthropic`, and a tenant secret's name `swarm-tenant-eng-git`. The
    relaxation is narrow, for the generic rules outside test paths only:

    * (i) the whole value is ONE `${...}` slot holding only a name
      (`_INTERPOLATION_SLOT`). A slot with anything glued to it
      (`${a}hunter2`), or with a shell default inside it (`${A:-hunter2}`),
      is still refused: the glued part or the default may be the credential.
    * (ii) otherwise, the value is a single lowercase word of any length
      (`_REFERENCE_WORD`) or one of the
      KNOWN reference shapes (`_REFERENCE_SHAPE`), holds no digit, and
      `_looks_like_a_credential` rejects it (with no digit it always does;
      it is asked so a change to it cannot make this rule pass a value it
      accepts). Rule (ii) never applies:
      - after an `Authorization` header's scheme (`http_authorization_scheme`):
        what follows the scheme is the credential, whatever its case;
      - to a scheme word itself (`bearer`, `Basic`, `token`): it names nothing;
      - under a name ending `password`/`passwd` (the key/value rule's `pw`
        group): that name is assigned the password, not a provider id or a
        secret's name, so `password = "letmein"` is refused. No dictionary.

    Only quotes, their escaping backslashes, whitespace and a trailing `;` or
    `,` are stripped, never a bracket, so a slot's closing brace is kept.
    """
    candidate = value.strip("\"'`\\ \t").rstrip(";,").strip("\"'`\\")
    if _INTERPOLATION_SLOT.fullmatch(candidate):
        return True
    if rule is not None and rule.name == "http_authorization_scheme":
        return False
    if candidate.lower() in _AUTH_SCHEME_WORDS:
        return False
    if (
        rule is CREDENTIAL_KEY_VALUE
        and match is not None
        and match.re is rule.pattern
        and match.group("pw")
    ):
        return False
    if len(candidate) > _REFERENCE_MAX_CHARS or any(c.isdigit() for c in candidate):
        return False
    shaped = (
        _REFERENCE_WORD.fullmatch(candidate) is not None
        or _REFERENCE_SHAPE.fullmatch(candidate) is not None
    )
    return shaped and not _looks_like_a_credential(candidate)


#: What may follow an unquoted reference on its line: nothing, or a closing
#: bracket, separator or (escaped) quote that ends an enclosing string.
_VALUE_END = re.compile(r"[ \t]*(?:$|[;,)\]}]|\\*[\"'`])")


def _odd_backslashes(text: str) -> bool:
    """True when `text` ends in an odd run of backslashes, i.e. a quote right
    after it is escaped."""
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


def _ends_the_value(match: re.Match[str]) -> bool:
    """True when a generic match's value is the WHOLE value, not its first word.

    The generic rules' value class stops at whitespace, a comma or a quote, so
    a password name assigned a quoted four-word passphrase matches only its
    first word, a digitless lowercase word `_is_a_reference` would pass. The owner's rule
    (ii) asks the whole value to be one identifier, so a reference counts only
    when it ends the value: a quoted value must close right after it (the
    rule's opening `"`, escaped or not, or a `'`/`` ` `` the value opens with),
    and an unquoted one must end the line or meet a separator, a closing
    bracket or the quote of an enclosing string.
    """
    prefix = match.group(1)
    value = match.group(0)[len(prefix):]
    rest = match.string[match.end():].split("\n", 1)[0]
    opened = prefix.rstrip()
    if opened.endswith('"'):
        # The close must be escaped EXACTLY as the open was: in `"letmein\"
        # <secret>"` the `\"` is part of the value, not its end (#470's review).
        escapes = len(opened[:-1]) - len(opened[:-1].rstrip("\\"))
        close = re.match(r'(\\*)"', rest)
        # The value class swallows a backslash right before the close, so the
        # backslashes the value ends in count with the close's own: `NAME =
        # "x\" <rest>"` reads as the value `x\` and a bare `"`.
        trailing = len(value) - len(value.rstrip("\\"))
        if close is None or trailing + len(close.group(1)) != escapes:
            return False
        return not _value_continues(rest[close.end():], '"', escapes)
    if value[:1] in ("'", "`"):
        quote = value[0]
        body = value[1:].rstrip(";,)]}")
        if body.endswith(quote):
            if not (len(body) > 1 and quote not in body[:-1]):
                return False
            if _odd_backslashes(body[:-1]):
                return False  # the quote is escaped, so it does not close
            return not _value_continues(rest, quote, 0)
        if rest.startswith(quote):
            if _odd_backslashes(body):
                return False
            return not _value_continues(rest[1:], quote, 0)
        return False
    return _VALUE_END.match(rest) is not None and not _value_continues(rest, None, 0)


#: An opening quote, with the backslashes that escape it.
_OPEN_QUOTE = re.compile(r"(\\*)([\"'`])")


#: A comment start, which may follow a value only after whitespace.
_COMMENT_START = re.compile(r"(?:#|//|--)")

#: The next key or argument after a `,`: an identifier then `=` (not `==`) or `:`.
_NEXT_KEY = re.compile(r"[A-Za-z_][\w.\-]*[ \t]*(?::|=(?!=))")


def _comment_or_nothing(text: str) -> bool:
    """True when `text` is empty, whitespace, or whitespace then a comment."""
    if not text.strip():
        return True
    return text[:1] in " \t" and _COMMENT_START.match(text.lstrip(" \t")) is not None


def _value_continues(after: str, closed: str | None, escapes: int) -> bool:
    """True unless the text `after` a value's end on its line PROVES the value
    ended there. An allow-list (#470's review: every deny-list of joiners, `+`,
    `,`-literal, adjacency, missed `.`, `&`, `||`, `~`, `|`, a bare token, a
    conditional and `;`). After the close the value is whole only when what
    follows is the end of the line, whitespace, a `;` ending the statement, a
    closing bracket (then `,`, `;` or the line's end), a comment after
    whitespace, or a `,` and then the next key or argument. Anything else is
    read as more of the value, so the generic rule refuses.

    `closed` is the quote the value just closed with (None when it was
    unquoted) and `escapes` how many backslashes escaped it. A quote of
    another kind, or one escaped less, straight after it closes an ENCLOSING
    string (`'"secret": "anthropic"'`, `"{\\"secret\\": \\"anthropic\\"}"`),
    and nothing after that belongs to this value.
    """
    straight = _OPEN_QUOTE.match(after)
    if straight is not None and (
        closed is None or straight.group(2) != closed or len(straight.group(1)) < escapes
    ):
        return False
    rest = after.lstrip(" \t")
    if not rest:
        return False
    if len(rest) != len(after) and _COMMENT_START.match(rest):
        return False
    while rest[:1] in (")", "]", "}") and rest:
        rest = rest[1:]
        if not rest:
            return False
        if rest[:1] in " \t":
            return not _comment_or_nothing(rest)
        if _OPEN_QUOTE.match(rest):
            return False  # the bracket ends a literal inside an enclosing string
    if rest[:1] == ";":
        return not _comment_or_nothing(rest[1:])
    if rest[:1] != ",":
        return True
    following = rest[1:]
    if _comment_or_nothing(following):
        return False
    following = following.lstrip(" \t")
    if _NEXT_KEY.match(following):
        return False
    literal = _OPEN_QUOTE.match(following)
    if literal is None:
        return True
    end = following.find(literal.group(2), literal.end())
    if end < 0:
        return True
    return not following[end + 1:].lstrip(" \t").startswith(":")


def _match_counts(rule: Any, match: re.Match[str], in_tests: bool) -> bool:
    """Whether one token-starting match of `rule` is a credential, by tier."""
    if rule.name == "jwt":
        return _decodes_as_a_jwt(match.group(0))
    if rule.name in GENERIC_CREDENTIAL_RULES:
        if rule is CREDENTIAL_KEY_VALUE and not _assigns_a_literal(match):
            return False
        value = match.group(0)[len(match.group(1)):]
        if not in_tests:  # tier 2
            return not (_is_a_reference(value, rule, match) and _ends_the_value(match))
        return _looks_like_a_credential(value.strip("\"'`;,)]}\\ \t"))
    # A vendor key: refused outside tests as before; in a test path it passes
    # ONLY with an explicit placeholder (owner decision, 2026-10-01). Entropy
    # and digits cannot tell a fixture from a real key: 22% of real-shaped
    # AKIA tokens passed that judgement.
    if not in_tests:
        return True
    prefix = _VENDOR_PREFIX.match(match.group(0))
    tail = match.group(0)[prefix.end():] if prefix is not None else match.group(0)
    return not _is_explicit_placeholder(tail)


def _credential_in(path: str, added: str) -> CredentialHit | None:
    """The first credential in `added` -- text the file at `path` adds, its
    `+` removed -- tiered by whether `path` is a test path (#373), or None.

    A pattern match counts only where it starts a token (`_adds_a_credential`);
    the private-key block is exempt, its marker is never part of an
    identifier. Rules are asked in `swarm_redaction.RULES` order, and the
    answer names the rule and where its match starts, never the value.
    """
    if not added:
        return None
    in_tests = is_test_path(path)
    for rule in CREDENTIAL_RULES:
        if rule.apply is not None:  # the private-key block
            # Main's rule, in every path and tier: refused exactly when
            # `mask_private_keys` would mask something (owner, 2026-10-01).
            if rule.apply(added, False, False)[1]:
                marker = _PEM_MARKER.search(added)
                return CredentialHit(rule.name, marker.start() if marker else 0)
            continue
        for match in rule.pattern.finditer(added):
            start = match.start()
            if start and (added[start - 1].isalnum() or added[start - 1] == "_"):
                continue
            if _match_counts(rule, match, in_tests):
                return CredentialHit(rule.name, start)
    return None


#: A bare value shaped like a credential: twelve or more token characters,
#: with at least one letter and one digit. `get_token()`, `str`, `self.token`
#: and `DEFAULT_TOKEN` are not; `a1b2c3d4e5f6g7h8` is.
_BARE_CREDENTIAL_RE = re.compile(r"(?=[^\s]*[A-Za-z])(?=[^\s]*[0-9])[A-Za-z0-9_\-+/=~]{12,}")


def _assigns_a_literal(match: re.Match[str]) -> bool:
    """True when a `KEY_VALUE` match assigns a LITERAL to the credential's name.

    OWNER DECISION, 2026-09-28 (#259): in a commit's code, `token =
    get_token()` and `password: str` name a credential without holding one,
    and folding a history for them would fold nearly every history that
    touches authentication. So the key/value rule counts only for a quoted
    string -- the rule's group 1 ends with the opening quote, or the value
    opens with a single quote the rule's value class takes as a character --
    or for a bare value shaped like a credential (`_BARE_CREDENTIAL_RE`). The
    specific families (AWS keys, private keys, JWTs, provider tokens) and the
    registered literals count wherever they appear.
    """
    prefix = match.group(1)
    value = match.group(0)[len(prefix):]
    # A backtick opens a literal too (`_ends_the_value` already reads one as a
    # quoted value); without it `` secret = `x\` <token>` `` was never counted.
    if prefix.rstrip().endswith('"') or value.startswith(("'", "`")):
        return True
    return _BARE_CREDENTIAL_RE.fullmatch(value.rstrip(";)}]'")) is not None


def _first_leaking_commit(
    *,
    shas: list[str],
    keep: str | None,
    leaks: LeakPredicate,
    git: list[str],
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    overlap: int = SCAN_OVERLAP_CHARS,
    floor: str = "",
) -> int | None:
    """The 1-based index of the first agent commit whose ADDED text `leaks`, or None.

    `floor` is the clone base the first commit must sit on ("" for an empty
    repository, whose first commit has no parent). A commit whose first
    parent is not the entry before it in `shas` (or `floor`) is returned as
    a hit too: the list is not the chain the replay would write.

    Each commit is diffed against the tree it will sit on once replayed --
    the entry before it, which the chain check makes its first parent (the
    empty tree for a parentless first commit) -- with `--text` so a file git would call
    binary is still shown byte for byte, and `--no-renames` so a moved file's
    content is shown rather than only its new name. `leaks` is asked about
    what each file ADDS, `+` removed: a removed line was in the parent's tree
    already. The worker's own `keep` commit is skipped here; its tree is the
    final tree, which `final_tree_leak` scans before any push.
    """

    def run(argv: list[str], slug: str, cap: int = 4 * 1024 * 1024) -> tuple[int, str, bool]:
        return _git_text_full(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=cap,
        )

    empty_tree: str | None = None
    for index, sha in enumerate(shas, 1):
        code, raw, _ = run([*git, "cat-file", "commit", sha], "publish-scan-read")
        if code != 0:
            raise GitError(f"could not read commit {sha[:12]}")
        header = raw.partition("\n\n")[0]
        parents = [line[len("parent "):].strip() for line in header.split("\n") if line.startswith("parent ")]
        # EACH COMMIT IS SCANNED AGAINST THE TREE IT WILL ACTUALLY SIT ON
        # (#259 re-review, grafts). The replay writes commit i on the
        # rewritten commit i-1 (or the base for the first), so the diff that
        # matters is against THAT, and it is the same as the commit's own
        # first parent only when the list is an unbroken chain. A list that
        # skips a commit -- `.git/info/grafts`, which `rev-list` and
        # `merge-base` follow and `GIT_GRAFT_FILE=/dev/null` now switches off
        # (`gitops._git_env`) -- would have scanned each commit against a
        # parent the push never sends, and shipped a skipped commit's secret
        # in the next kept tree. A broken chain folds: the index is returned.
        expected = shas[index - 2] if index > 1 else (floor or None)
        actual = parents[0] if parents else None
        if actual != expected:
            return index
        if sha == keep:
            continue
        if expected is not None:
            before = expected
        else:
            if empty_tree is None:
                code, made, _ = run([*git, "hash-object", "-t", "tree", "/dev/null"], "publish-scan-empty")
                empty_tree = made.strip()
                if code != 0 or not _SHA_RE.match(empty_tree):
                    raise GitError("could not name the empty tree")
            before = empty_tree
        # EVERY BYTE OF THE DIFF IS SCANNED, AND NONE IS STORED (owner
        # decision, 2026-09-29): streamed from git through a pipe into
        # overlapping windows (`_DiffLeakScanner`), so a big diff is neither
        # refused for its size nor scanned only in part, and no file holds
        # the value it was scanned for.
        code, hit = _scan_diff_stream(
            [
                *git, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "--text", "--no-renames", "--submodule=short", "--src-prefix=a/",
                "--dst-prefix=b/", "-U0", before, sha, "--",
            ],
            leaks=leaks,
            overlap=overlap,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="publish-scan-diff",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        if code != 0:
            raise GitError(f"could not diff commit {sha[:12]} against its parent")
        if hit is not None:
            return index
    return None


def final_tree_leak(
    *,
    repo: Path,
    base: str | None,
    leaks: LeakPredicate,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    overlap: int = SCAN_OVERLAP_CHARS,
) -> str | None:
    """Why the branch about to be pushed must not be, or None when it may.

    OWNER DECISION, 2026-09-28 (#259 review, M4). The per-commit scan
    (`_first_leaking_commit`) decides only between keeping the agent's
    commits and folding them; it skips the worker's own commit of
    uncommitted work, and a fold pushes the final tree unscanned. So a key
    the agent committed and never deleted -- or wrote and never committed --
    reached the forge in the one commit the worker wrote. This reads what
    the push would add, `base..HEAD` diffed as one span AFTER the replay or
    the fold, so it covers every commit's tree the push carries: each kept
    commit is scanned by the per-commit pass and the final tree here.

    `leaks` is asked about the text each FILE adds, with the `+` removed, so
    a line the branch removes -- the repository's own, already public --
    never refuses it. The answer names the FILE and never the value: the
    reason becomes the attempt's error, which the retry and a human both
    read. The whole diff is scanned however big it is, streamed in
    overlapping windows (`_DiffLeakScanner`; owner decision, 2026-09-29):
    size alone never refuses a publish, and no file holds the diff.

    `repo` is the worker's clean repository (`gitops.prepare_publish_repo`,
    owner decision 2026-09-29): the agent's objects were fetched into it with
    object checking, and it holds no commit-graph, grafts, shallow file,
    replace refs or config of the agent's -- a forged commit-graph entry made
    `git diff` in the clone compare a tree the push never sends. The
    `GIT_NO_REPLACE_OBJECTS=1`, `GIT_GRAFT_FILE` and `core.commitGraph=false`
    of `gitops._git_env`, and the explicit `--src-prefix`/`--dst-prefix` and
    `core.quotePath=false`, stay as a second belt.
    """
    g = [git_binary, *_NO_HOOKS, "-c", "core.quotePath=false"]
    run_kwargs: dict[str, Any] = {
        "repo": Path(repo),
        "private_dir": private_dir,
        "logs_dir": logs_dir,
        "timeout_seconds": timeout_seconds,
        "logger": logger,
    }
    code, _head, _ = _git_text_full(
        [*g, "rev-parse", "--verify", "--quiet", "HEAD"], slug="publish-final-head", **run_kwargs
    )
    if code != 0:
        # Nothing is committed, so the push has nothing to send.
        return None
    if base == EMPTY_CLONE_BASE:
        code, made, _ = _git_text_full(
            [*g, "hash-object", "-t", "tree", "/dev/null"], slug="publish-final-empty", **run_kwargs
        )
        before = made.strip()
        if code != 0 or not _SHA_RE.match(before):
            raise GitError("could not name the empty tree")
    elif base and _SHA_RE.match(base.strip()):
        before = base.strip()
    else:
        raise GitError(
            "the clone base is unknown, so the worker cannot read what the push "
            "would add; nothing was pushed"
        )
    code, hit = _scan_diff_stream(
        [
            *g, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
            "--no-renames", "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", "-U0",
            before, "HEAD", "--",
        ],
        leaks=leaks,
        overlap=overlap,
        slug="publish-final-diff",
        **run_kwargs,
    )
    if code != 0:
        raise GitError("could not diff the branch against the clone base")
    if hit is not None:
        # The rule and the line tell the retry what to remove (#373); the
        # value is never in it, and the caller scrubs the reason anyway.
        return f"the final tree adds a credential in {hit.path} (rule {hit.rule}, line {hit.line}); remove it"
    return None


def replay_agent_commits(
    *,
    repo: Path,
    base: str | None,
    keep: str | None,
    task_id: str,
    scrub: Callable[[str], str],
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    leaks: LeakPredicate | None = None,
    overlap: int = SCAN_OVERLAP_CHARS,
) -> int | None:
    """Rewrite each commit on HEAD's first-parent line since `base` as the worker's (#242).

    Returns how many of the AGENT's commits were kept -- `keep`, the worker's
    own commit from `commit_dirty`, is rewritten too and not counted -- or
    None when this history cannot be kept commit by commit, and the caller
    folds it as before (`gitops.fold_agent_commits`):

    * HEAD does not descend from `base` (the agent reset or checked out
      another line): nothing tells its commits from the repository's;
    * more than `MAX_KEPT_COMMITS` of them;
    * `leaks` finds a credential in the text any agent commit ADDS to a file
      (#259 review), read whole in windows overlapping by `overlap`. A kept commit keeps its TREE, so a key the agent added
      in one commit and deleted in the next would be pushed in the first
      one's tree even though the final tree is clean -- and a pushed object
      on a public forge is published for good. The fold pushes only the
      final tree. Only the offending commit's index is logged, never any of
      its content. A diff of any size is scanned whole (owner decision,
      2026-09-29), so size alone never folds a history.

    WHAT IS KEPT, AND WHAT IS NOT. Each commit keeps its TREE, exactly -- so
    the tests-only commit of a red-first change is still tests only, and CI
    can run it -- and its message, cleaned (`clean_commit_message`). Its
    author, committer, dates and signature are the worker's: the commit is
    made with `git commit-tree` under `_worker_identity`, and #219's rule that
    the worker writes every commit it pushes holds, checked again at the push
    by `verify_worker_authorship`. A merge the agent made keeps its merged
    tree and loses its second parent: every commit on that side would
    otherwise be pushed as it was written.

    The index and working tree are not touched; `reset --soft` moves the
    branch onto the rewritten line, which ends in the same tree HEAD had.
    An unknown `base` refuses, exactly as the fold does.

    `repo` is the worker's clean repository, never the agent's clone
    (`gitops.prepare_publish_repo`, owner decision 2026-09-29): the list, the
    scan and the rewrite read only objects fetched with object checking, and
    none of the agent's grafts, shallow file, replace refs, commit-graph or
    config.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot tell the agent's "
            "commits from the repository's; nothing was pushed rather than "
            "commits whose author and message the worker did not write"
        )
    floor = "" if empty else (base or "").strip()
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-keep-born")
        if code != 0:
            return 0
        span = "HEAD"
    else:
        code, _ = run([*g, "merge-base", "--is-ancestor", floor, "HEAD"], "publish-keep-descends")
        if code == 1:
            logger.warning(
                "the agent's branch does not descend from the clone base; its "
                "commits are folded into one worker commit"
            )
            return None
        if code != 0:
            raise GitError("could not tell whether the agent's work descends from the clone base")
        span = f"{floor}..HEAD"

    code, text = run([*g, "rev-list", "--first-parent", "--reverse", span], "publish-keep-list")
    if code != 0:
        raise GitError("could not list the commits made since the clone base")
    shas = text.split()
    if not shas:
        return 0
    if len(shas) > MAX_KEPT_COMMITS:
        logger.warning(
            "the agent made more commits than are kept one by one; they are "
            "folded into one worker commit",
            commits=len(shas),
            cap=MAX_KEPT_COMMITS,
        )
        return None

    if leaks is not None:
        index = _first_leaking_commit(
            shas=shas,
            keep=keep,
            leaks=leaks,
            overlap=overlap,
            floor=floor,
            git=g,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        if index is not None:
            logger.warning(
                "an agent commit's diff carries a registered secret; the agent's "
                "commits are folded into one worker commit of the final tree",
                commit_index=index,
                commits=len(shas),
            )
            return None

    parent = floor or None
    kept = 0
    for index, sha in enumerate(shas, 1):
        code, raw = run([*g, "cat-file", "commit", sha], "publish-keep-read")
        if code != 0:
            raise GitError(f"could not read commit {sha[:12]}")
        header, _, message = raw.partition("\n\n")
        trees = [line[len("tree "):] for line in header.split("\n") if line.startswith("tree ")]
        tree = trees[0].strip() if trees else ""
        if not _SHA_RE.match(tree):
            raise GitError(f"could not read the tree of commit {sha[:12]}")
        if sha == keep:
            # The worker's own message, from `commit_dirty`.
            text = message.replace("\x00", "").strip()
            text = text or UNCOMMITTED_COMMIT_SUBJECT
        else:
            kept += 1
            text = clean_commit_message(
                message,
                scrub=scrub,
                fallback=f"swarm: commit {index} of {len(shas)} from {task_id}",
            )
        argv = [*g, "commit-tree", tree]
        if parent:
            argv += ["-p", parent]
        argv += ["-m", text]
        code, made = run(argv, "publish-keep-write")
        made = made.strip()
        if code != 0 or not _SHA_RE.match(made):
            raise GitError(f"could not rewrite commit {sha[:12]} as the worker's")
        parent = made
    code, _ = run([*g, "reset", "--soft", parent or ""], "publish-keep-reset")
    if code != 0:
        raise GitError("could not move the branch onto the worker's rewritten commits")
    logger.info(
        "kept the agent's commits, each rewritten as the worker's",
        kept=kept,
        rewritten=len(shas),
    )
    return kept


def _recheck_runner_input(runner_profile: str, stored: Any) -> None:
    """Refuse a stored `input` its profile's declaration refuses. Every profile.

    The same rule swarm-api and the plugin's bridge apply at submission,
    `swarm_common.profiles.check_inputs`, asked of the input as stored, without
    its `prompt` (which every profile takes and no declaration names). A
    refusal is `ConfigError`, which exits 78, "cannot start": the task fails
    with `EndCause.CANNOT_START`, and the reconciler does not retry it, because
    every retry would read the same document and be refused the same way.

    NO PROFILE IS EXEMPT (the owner, on #345, 2026-09-29). The mock reads keys
    no caller may send -- `spend`, `provider`, `credential_revoked_times` and
    the others with which it acts out a provider -- and the worker's unit suite
    used to write them into the stored input. It hands them to the runner
    through a test-only seam now (tests/unit/worker/conftest.py, `simulated`),
    so a mock task is held to its declaration like any other.

    The message names the key and the bound, never the value: a refused URL
    may carry `user:password@`, and this text is stored as the task's error.
    """
    profile = RUNNER_PROFILES[runner_profile]
    raw = stored if stored is not None else {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"the task's input is a {type(raw).__name__}, not an object; runner profile "
            f"{runner_profile!r} cannot read it (checked again by the worker, contract "
            "request 32)"
        )
    try:
        check_inputs(profile, {key: value for key, value in raw.items() if key != "prompt"})
    except InputRefused as refused:
        bound = f"; expected {refused.expected}" if refused.expected else ""
        raise ConfigError(
            f"the task's input is refused by runner profile {runner_profile!r}: "
            f"{', '.join(refused.keys)}{bound} (checked again by the worker, contract "
            "request 32)"
        ) from None


def _end_cause_of(exc: BaseException) -> EndCause:
    """Why a deliberate worker failure ended its task, as the typed end cause.

    INPUTS_UNAVAILABLE for a declared input the worker refused to stage: the
    agent never started, so it is not the runner's error (#185, decision 4).
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
    """Whether the generation check is asked again after `exc` (#198).

    Exactly the errors `Worker._exit_control_plane_unreachable` would turn
    into 69: a Firestore or google-auth error that is either an outage
    (`_unavailable_cause`) or not a named refusal (`_refusal_cause`). A
    refusal is 78 and would be refused again. A fence or a tenant mismatch
    is not a Firestore error at all, and ends the attempt as it did.
    """
    if not _control_plane_unreachable(exc):
        return False
    return _unavailable_cause(exc) is not None or _refusal_cause(exc) is None


def _one_line(value: Any, limit: int = 600) -> str:
    """An error's text on one bounded line, for a log field."""
    return " ".join(str(value).split())[:limit]


def _unreachable_hint(exc: BaseException) -> str:
    """What an operator reading a 69 at the generation check should know (#198)."""
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


def _error_chain(exc: BaseException) -> list[BaseException]:
    """`exc`, then what it was raised from or during (`__cause__`, `__context__`).

    Breadth first, bounded at 16, and cycle-safe. A library can raise
    something else on the way up: Firestore's rollback of a transaction whose
    begin failed raises a ValueError whose `__context__` is the RetryError,
    and google-auth raises RefreshError FROM the TransportError.
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return chain


def _unavailable_cause(exc: BaseException) -> BaseException | None:
    """The error in `exc`'s chain that says a dependency could not be reached, or None.

    UNAVAILABLE ONLY, not every Google API error. Counted: RetryError (a
    budget ran out), EVERY ServerError, RESOURCE_EXHAUSTED and 429, ABORTED
    (contention that outlasted the transaction's own retries), CANCELLED, and
    google-auth's transport and timeout errors or any it marks retryable.
    These say the call did not complete, and the next attempt may find the
    service back.

    EVERY ServerError, not a list of them: UNAVAILABLE, DEADLINE_EXCEEDED,
    INTERNAL, 502 and 504, and also gRPC UNKNOWN, DATA_LOSS and UNIMPLEMENTED.
    The first version named five and left those three out. UNKNOWN is what
    Firestore's client raises when a stream is reset under it ("Stream
    removed"), and the startup Retry does not retry it. So a stream reset
    FAILED the task for good after the generation check, and ended it through
    a 78 at the check (review of PR #59). Each of the three could be permanent,
    UNIMPLEMENTED above all. But a wrong guess here costs one bounded retry,
    and the other wrong guess ends the task. CANCELLED is the RPC being
    dropped, not an answer.

    Not counted: PERMISSION_DENIED, NOT_FOUND, INVALID_ARGUMENT,
    FAILED_PRECONDITION, UNAUTHENTICATED and a non-retryable RefreshError.
    Those are answers: the service was reached and said no, and it will say no
    to the next attempt too. The attempt owns its task at this point (step 1
    passed), so it fails it with the reason, as it did before the startup
    budget. Step 1 is different, and `_exit_control_plane_unreachable` turns
    anything that is not a named refusal (`_refusal_cause`) into a retry:
    without the generation check, the attempt does not know that the task is
    its to fail.

    The whole chain is searched (`_error_chain`).
    """
    try:
        from google.api_core import exceptions as core
    except ImportError:
        return None
    transient: tuple[type[BaseException], ...] = (
        core.RetryError,
        # UNAVAILABLE, DEADLINE_EXCEEDED, INTERNAL, UNKNOWN, DATA_LOSS,
        # UNIMPLEMENTED, 502, 504: every 5xx and every server-side gRPC code.
        core.ServerError,
        core.TooManyRequests,  # ResourceExhausted is a subclass
        core.Aborted,
        core.Cancelled,
    )
    try:
        from google.auth import exceptions as auth
    except ImportError:
        auth = None  # type: ignore[assignment]
    auth_transient: tuple[type[BaseException], ...] = ()
    if auth is not None:
        auth_transient = tuple(
            kind
            for kind in (getattr(auth, "TransportError", None), getattr(auth, "TimeoutError", None))
            if isinstance(kind, type)
        )

    for current in _error_chain(exc):
        if isinstance(current, transient):
            return current
        if auth is not None and isinstance(current, auth.GoogleAuthError):
            if isinstance(current, auth_transient) or getattr(current, "retryable", False):
                return current
    return None


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
    not name, a new one a library adds, a 409. The review of PR #59 found
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
        auth = None  # type: ignore[assignment]
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


def _execution_name() -> str | None:
    for name in ("CLOUD_RUN_EXECUTION", "K_REVISION", "JOB_NAME", "HOSTNAME"):
        value = os.environ.get(name)
        if value:
            return value
    return None


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


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else {"value": data}


#: The keys _usage_summary reads. Used to decide WHICH level of a possibly
#: nested result actually carries the numbers, rather than assuming one.
#:
#: Imported, not restated: the runner lifts exactly these out of a FAILED run
#: into result.json (runners/base.py), and a second copy here would be a key the
#: runner carries and this never reads.
_USAGE_KEYS = SPEND_KEYS

#: The fields of a usage summary that ADD UP across the runs of one attempt.
#: Everything `_usage_summary` emits except `models`, which is a set.
_SUMMED_SPEND = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "thinking_tokens",
    "total_cost_usd",
    "num_turns",
    "duration_ms",
    "duration_api_ms",
)


def _add_spend(total: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    """Two usage summaries as one: numbers summed, model lists unioned.

    A key absent from BOTH stays absent. "Not reported" is not zero (see
    `control.record_spend`), and a sum must not turn the one into the other.
    """
    out = dict(total)
    for key in _SUMMED_SPEND:
        if key in more:
            out[key] = out.get(key, 0) + more[key]
    if "total_cost_usd" in out:
        # Summing binary floats accumulates noise (0.1 + 0.2); no cost figure
        # means anything past the tenth decimal place of a dollar.
        out["total_cost_usd"] = round(out["total_cost_usd"], 10)
    models = set(out.get("models") or ()) | set(more.get("models") or ())
    if models:
        out["models"] = sorted(models)
    return out


def _workspace_label(ws: workspace_mod.Workspace, path: Path) -> str:
    """`artifacts/x.png`, `logs/stdout.log` -- a path a reader can place.

    Relative to the attempt's workspace, so the attempt id and the pod's
    filesystem layout stay out of a document every reader of the task sees.
    """
    try:
        return path.relative_to(ws.root).as_posix()
    except ValueError:
        return path.name


def _has_usage_keys(candidate: dict[str, Any]) -> bool:
    return any(key in candidate for key in _USAGE_KEYS)


def _usage_summary(output: Any) -> dict[str, Any]:
    """Token and cost numbers, pulled out of a CLI agent's result BEFORE truncation.

    `_truncate_json` replaces the whole result with a preview STRING once it
    exceeds its limit, so the runs that consumed the most tokens were exactly the
    runs whose token counts were discarded. The raw file still reaches GCS, but
    nothing queryable kept the numbers, and a per-tenant spend figure assembled by
    reading one GCS object per attempt does not survive real volume.

    This is deliberately a small, flat dict of scalars: it stays far below any
    truncation limit, so it survives whatever the rest of the result does.

    It does NOT add a field to `Attempt` -- `apps/common/swarm_common/` is frozen.
    These land inside the free-form runner summary. A typed field is a contract
    change request; see docs/contract-change-requests.md.
    """
    if not isinstance(output, dict):
        return {}

    # WHERE THE NUMBERS ACTUALLY ARE.
    #
    # A CLI runner does not return the agent's JSON. It returns its own
    # envelope -- {summary, provider, model, exit_code, structured_output,
    # limits, metrics} (runners/cliagent.py:342-357) -- and the CLI's own JSON,
    # which is where `usage`, `total_cost_usd`, `num_turns` and `modelUsage`
    # live, is nested one level down under `structured_output`.
    #
    # This function read only the top level, so on the production path it found
    # none of those keys and returned {} for every attempt -- while its unit
    # test, which feeds the raw CLI shape, passed. A test exercising a shape
    # production never produces is worse than no test: it reports a working
    # extractor when there is none. tests/unit/worker/test_usage_summary.py now
    # also feeds the real envelope.
    #
    # Preferring whichever level actually carries the keys, rather than always
    # descending, keeps this correct for a runner that returns the CLI JSON
    # directly and for one that wraps it.
    source = output
    if not _has_usage_keys(source):
        nested = output.get("structured_output")
        if isinstance(nested, dict) and _has_usage_keys(nested):
            source = nested

    summary: dict[str, Any] = {}

    usage = source.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                summary[key] = value
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
            summary["thinking_tokens"] = details["thinking_tokens"]

    # bool is a subclass of int, so it is excluded explicitly -- `is_error: true`
    # arriving as a cost of 1 would be a quietly wrong number, which is the whole
    # class of bug this file is being edited to avoid.
    for key in ("total_cost_usd", "num_turns", "duration_ms", "duration_api_ms"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value

    models = source.get("modelUsage")
    if isinstance(models, dict) and models:
        summary["models"] = sorted(str(m) for m in models)

    return summary


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
