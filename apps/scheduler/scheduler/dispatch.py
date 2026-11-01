"""Turning a lease into a running container.

Dispatch is strictly DOWNSTREAM of the lease (invariant 1 and 3): nothing here
runs until capacity has already been reserved in Firestore, and every failure
path hands that capacity straight back rather than leaving a slot held by a
container that will never exist.

Cloud Run Jobs was chosen as the primary backend; since 2026-10-10 GKE
Autopilot runs every agent profile but `mock` (docs/architecture.md §5). GKE
first took browser work, because
Chromium needs a large /dev/shm and GKE gives direct control over it.
That is `browser`, and `claude-code-browser` since contract request 67
(2026-10-11).
It also takes claude-code since contract request 53 (2026-10-08), for its start
latency: about 23 s there against Cloud Run's 128 s median. And indexer since
contract request 63 (2026-10-10), the canary for #939: a new GKE pod's internet
path opens in about 1 s, a new Cloud Run instance's in a median 20 s. And
generic, codex and merge since contract requests 64-66 (2026-10-10, #939
option A), for the same reason; Cloud Run keeps mock, and post-verdict and
claude-code-review, which were designed to run as accounts of their own.

Per-tenant-per-profile Job resources exist because of a Cloud Run constraint,
not a preference: Cloud Run sets the service account on the JOB resource and it
cannot be overridden per execution. One shared Job would therefore run every
tenant's work as the same identity, and invariant 9 -- a tenant's provider key
must never be reachable from another tenant's pod -- would be unenforceable. So
each (tenant, profile) gets its own Job bound to that tenant's service account,
and the reconciler garbage-collects the ones that stop being used. The same
constraint applies to sizing, which Cloud Run also pins on the Job resource, so a
workflow step that named a smaller resource class gets a Job of its own -- see
the sizing note below.

Two rules from the contract are applied here and are not negotiable:
  * Memory and ephemeral-storage: requests == limits, on every container. A
    memory burst is what gets a container OOM-killed under node pressure.
    CPU (invariant 7 as amended by contract request RS-1, owner 2026-10-20;
    SIZES-SCHED-1, part of #1689): on GKE the request is the class's `cpu` and
    the limit its `cpu_limit_cores`, at most CPU_BURST_MAX_RATIO x the request.
    Autopilot bills, and admission charges, the request; past it a container
    borrows its node's idle requested CPU and is throttled, never killed, under
    contention. Cloud Run bills its limit and has no request to separate, so a
    Cloud Run Job renders the request as its only number.
  * No Spot, anywhere. Spot Pods cannot use Autopilot extended run time, so
    Spot and "no preemption" are mutually exclusive.

SIZING COMES FROM `task.resource_class`, NOT from `profile.resource_class`.
Those are usually the same, and differ only when a workflow step named a
smaller named class (validation.py permits nothing larger). Admission has always
accounted against the TASK's class -- `pool_names_for` and the weighted `units`
both read `task.resource_class` inside the frozen admission transaction -- so a
container sized from the profile's class instead would mean the reserved
capacity and the running container disagree: the pool that governs the workload
would never be in the lease's `required` list (so a drain or an admin cap on it
would not stop the task), and the weighted global budget would be charged for
something other than what is running. The Cloud Run Job id carries the class for
the same reason: Cloud Run fixes sizing on the Job resource, so one Job per
(tenant, profile) could only ever express one size.

AND A GKE POD IS SIZED FROM THE SIZE ADMISSION PINNED (SIZES-SCHED-2,
docs/design/runner-sizes.md §4.4). An admin may resize a class; the scheduler
reads the current version once per admission pass (`sizes.py`), charges the
lease's units from it, records it on the attempt, and hands the same object
here (`BackendRouter.dispatch(sized=...)`), so the pod is the one the lease
paid for even when a newer version was saved in between. Cloud Run does not
follow a resize: its resources live on the Job, so a Cloud Run profile is
pinned at the code default it already runs at (§4.7).

NEITHER DISPATCHER SETS A CONTAINER `command` OR `args`, and that is the rule
the rest of the platform's safety hangs on. Both worker images declare
`ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]`: the
worker LIFECYCLE. It checks the fencing generation (invariant 5), honours a
requested cancel, moves the task to STARTING and RUNNING, heartbeats, starts
the runner as a supervised child, checkpoints (invariant 8), writes the
terminal state and releases the lease. A container `command` REPLACES the
ENTRYPOINT, so setting it to `RunnerProfile.command` -- which is the runner's
argv, `python -m agent_worker.runners.<x>` -- ran the bare runner with none of
that. Until 2026-09-24 the GKE path did exactly this for every browser task
(incident wf_ebb3ab2d65664707a559): the pods crashed on `/artifacts`, never
wrote to the control plane, and left their leases holding every browser slot.
That field is now `RunnerProfile.runner_argv` (contract request 18, accepted
2026-09-24): the lifecycle's CHILD argv (`lifecycle._runner_argv`), renamed so
the line that did this cannot be written again. The lifecycle finds it by the
profile NAME in `RUNNER_PROFILE`, which `worker_env` below sets. tests/unit/control_plane/test_dispatch_manifests.py
and tests/unit/worker/test_kubernetes_manifests.py hold both dispatchers to it.

SECRETS ARE NOT INJECTED ON THE GKE PATH. The worker reads its tenant's key from
Secret Manager at runtime using Workload Identity (agent_worker/secrets.py), and
on Cloud Run the Job additionally projects it natively from Secret Manager. On
GKE there is no such native source: a `secretKeyRef` needs a Kubernetes Secret,
nothing in this repository ever creates one, and a Job referencing a missing
Secret never starts (CreateContainerConfigError) while the scheduler records it
as DISPATCHED and holds the lease to the deadline. So the GKE manifest passes no
key material at all and the pod's KSA-to-GSA binding is what grants access.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import math
import random
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE
from swarm_common.identity import worker_service_account_id
from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import (
    CPU_BURST_MAX_RATIO,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    RUNTIME_TOOLS,
    Backend,
    LiveShare,
    ResourceSize,
    RunnerProfile,
    step_model_class,
    tools_launched,
)

from .credentials import AccountPool, CredentialSource, credential_for, needs_user_slot
from .mcp_admission import names_mcp_servers
from .judgment import BACKENDS as JUDGMENT_BACKENDS
from .judgment import (
    DEFAULT_BACKEND,
    TYPESAFE_BACKEND,
    jev_dispatch,
    quality_gates_env,
    read_quality_gates_env,
    read_settings,
    settings_mac_key,
)

if TYPE_CHECKING:
    from .sizes import Sized

log = logging.getLogger(__name__)

#: IMPORTED, not restated. This was a fourth private copy of `[^a-z0-9-]+`
#: (here, reconciler/detect.py, kubernetes/render.py and identity.py itself),
#: and the reconciler's `sanitised()` reverse-maps a Job label back to the
#: Firestore id this function produced -- so the two escaping rules agreeing is
#: what lets a running execution be matched to its task at all. If they drift,
#: the reconciler either kills a live execution it read as orphaned or never
#: finds a genuinely orphaned one. kubernetes/render.py already imports this
#: symbol for the same reason. docs/audits/2026-09-18/08, finding 1.

#: The image creates uid/gid 10001 with HOME=/home/swarm and runs as it
#: (images/agent-runtime-base/Dockerfile). The pod spec repeats the number so
#: the cluster enforces it rather than trusting the image, and so
#: `runAsNonRoot` has something to check against.
WORKER_UID = 10001
WORKER_HOME = "/home/swarm"

#: THE WORKSPACE MOUNT, AND THE ARTIFACTS DIRECTORY INSIDE IT.
#:
#: `/workspace` was written out in three places here -- the Cloud Run volume
#: mount, the GKE volume mount, and nothing that tied them together -- so it is
#: a constant now for the same reason WORKER_HOME is.
#:
#: THE `/artifacts` CRASH, AND WHAT IT WAS ACTUALLY A SYMPTOM OF. Measured
#: 2026-09-24 on the first browser attempt that ever started a container:
#:
#:     OSError: [Errno 30] Read-only file system: '/artifacts'
#:
#: `runners/base.py` defaults `artifacts_dir` to `work.parent / "artifacts"`
#: when SWARM_ARTIFACTS_DIR is unset, which from a cwd of `/workspace` is
#: `/artifacts`, on a root this container mounts read-only.
#:
#: CORRECTED 2026-09-24 (incident wf_ebb3ab2d65664707a559). This note used to say
#: Cloud Run never showed the crash because it ran the "identical image,
#: identical `python -m agent_worker.runners.<x>` command" on a writable root.
#: The commands were NOT identical, and that false sentence is what hid the real
#: defect for a day:
#:
#:   * Terraform's Cloud Run Jobs carry `command = null`
#:     (terraform/modules/cloud_run_jobs/main.tf), so the image ENTRYPOINT --
#:     the worker lifecycle -- ran. The lifecycle builds its runner child's
#:     environment from an allowlist and sets SWARM_ARTIFACTS_DIR to the
#:     attempt's own `<WORKSPACE_ROOT>/<attempt>/artifacts`
#:     (`workspace.Workspace.child_env`). The runner never reaches the default.
#:   * The GKE Job this module built set `command` to the RUNNER, which replaced
#:     the lifecycle. With no lifecycle, nothing set the variable. The crash was
#:     the first visible consequence of the missing lifecycle -- and fixing the
#:     crash alone (ffa82e3, which set the variable on the container) would have
#:     let the bare runner go on to run unfenced, uncheckpointed, with no
#:     heartbeat and no lease release. See the module docstring.
#:
#: WHAT SWARM_ARTIFACTS_DIR ON THE CONTAINER DOES NOW: nothing the lifecycle
#: reads. It is kept, on both backends from this one builder and in all three
#: worker templates, because it is inert and because a runner started BY HAND
#: inside a worker container (a `kubectl exec` repro during an incident) then
#: gets a writable path instead of Errno 30. It is not what makes a runner work,
#: and it is not the reason Cloud Run and GKE behaved differently.
#:
#: INSIDE THE WORKSPACE VOLUME rather than in a volume of its own, for the same
#: reason the lifecycle puts its own there: a dedicated emptyDir would need a
#: `sizeLimit`, a second disk number to invent and keep in step, when
#: `workspace` already has a limit of its own (the class's on Cloud Run,
#: `GkeDisk.workspace_gib` on GKE).
WORKSPACE_MOUNT = "/workspace"
WORKER_ARTIFACTS_DIR = f"{WORKSPACE_MOUNT}/artifacts"

#: THE GKE POD'S CACHE VOLUME (CACHE-SCRATCH, owner decision 2026-10-15), and
#: the variable that tells the worker where it is. The worker puts the agent's
#: tool caches (uv, npm, pip, terraform, XDG) and its TMPDIR under
#: `<CACHE_MOUNT>/<attempt>/` and never checkpoints or uploads them
#: (agent_worker/cachedirs.py, whose CACHE_ROOT_ENV is this name;
#: tests/unit/control_plane/test_gke_cache_volume.py holds the two equal).
#: OUTSIDE the workspace mount, because the workspace's own sizeLimit is what
#: evicted three claude-code attempts twice each on 2026-10-15 while it held
#: every cache as well as the clone. GKE only: Cloud Run has no second disk,
#: and a worker that is not told of one keeps its caches small in its tmpfs.
CACHE_MOUNT = "/cache"
CACHE_ROOT_ENV = "SWARM_CACHE_ROOT"

#: THE GKE POD'S LOCAL SCRATCH VOLUMES, and the variable that tells the worker
#: which they are (#2436): `scratch_volumes_value`. kubelet evicts the pod with
#: no grace period when one passes its sizeLimit, so the worker reads their
#: sizes on each heartbeat to report what filled one and to warn before it
#: does (agent_worker's scratch module, whose SCRATCH_VOLUMES_ENV is this
#: name). GKE only, as CACHE_ROOT_ENV: Cloud Run has no such volumes.
SCRATCH_VOLUMES_ENV = "SWARM_SCRATCH_VOLUMES"

#: THE CACHE VOLUME IS A NETWORK DISK (CACHE-PVC, owner decision 2026-10-15
#: ~04:00 PDT, "Network-disk cache"): a generic ephemeral volume, i.e. a
#: PersistentVolumeClaim the ephemeral-volume controller creates from the pod
#: spec when the pod is created and deletes when the pod is deleted. It is NOT
#: ephemeral-storage: kubelet counts neither it nor its usage against the pod's
#: limit, so filling it is ENOSPC in the writing process, never an eviction.
#: The class is kubernetes/storage/cache-storage-class.yaml's (pd-balanced,
#: reclaimPolicy Delete, WaitForFirstConsumer; none existed in terraform/ or
#: kubernetes/); the size is the owner's, ten times the 2 GiB emptyDir that
#: evicted (GKE_DISK says why). ReadWriteOnce: one pod, one disk.
CACHE_STORAGE_CLASS = "swarm-cache-pd-balanced"
CACHE_VOLUME_GIB = 20

#: THE CLOUD RUN WORKSPACE IS A TMPFS, sized as terraform sizes it.
#:
#: terraform/modules/cloud_run_jobs/main.tf gives its Jobs `empty_dir { medium =
#: "MEMORY" }` on launch stage GA, because the provider cannot express Cloud
#: Run's disk-backed ephemeral volume (Preview, and it disables live
#: migration) -- CONTRACT.md, "Correction (workspace storage)". The Jobs
#: `_build_job` creates on a 404 -- every tenant terraform does not list, every
#: pool-served tenant, every non-default resource class -- asked for that
#: Preview volume on BETA until 2026-10-01 (gap audit D7), so the same profile
#: ran on two different storage models depending on who had created its Job.
#:
#: A memory-medium volume is charged against the container's memory limit, so
#: it is capped below it: a full workspace then fails a write with ENOSPC,
#: which the worker can checkpoint through, instead of OOM-killing the agent
#: (a SIGKILL: no checkpoint, no park). 0.5 is `var.workspace_memory_fraction`'s
#: default, which terraform/infra does not override.
#: tests/unit/scheduler/test_scheduler_job_matches_terraform_job.py reads the
#: module and fails if the fraction, the formula, the medium or the launch
#: stage moves on either side alone.
WORKSPACE_MEMORY_FRACTION = 0.5


def workspace_size_gib(rc: Any) -> int:
    """terraform's `local.workspace_gib` for one resource class, in GiB.

    `max(1, min(rc.disk_gib, floor(rc.memory_gib * var.workspace_memory_fraction)))`
    """
    return max(1, min(rc.disk_gib, math.floor(rc.memory_gib * WORKSPACE_MEMORY_FRACTION)))


#: AUTOPILOT'S CEILING on a pod's total ephemeral-storage request in the
#: general-purpose class (every class but Performance and accelerator pods):
#: "between 10 MiB and 10 GiB" (GKE docs, "Resource requests in Autopilot",
#: read 2026-10-07). A pod that asks for more is refused at admission.
AUTOPILOT_MAX_EPHEMERAL_GIB = 10


@dataclass(frozen=True)
class GkeDisk:
    """A GKE worker pod's LOCAL disk-backed scratch volumes, in GiB, for one resource class.

    `readOnlyRootFilesystem` makes these, and the cache's network disk
    (CACHE_VOLUME_GIB, not here: a PVC is not ephemeral-storage), the only
    places the worker and its toolchain can write: the workspace (the attempt
    tree, which is also the agent's HOME -- see GKE_DISK), /tmp (where `checkpoint.py`
    builds its archive, up to `max_checkpoint_bytes`, 2 GiB, and wherever an
    agent names /tmp by its literal path) and the WORKER's HOME, /home/swarm.
    kubelet sums every disk-backed emptyDir against the pod's ONE
    ephemeral-storage limit and evicts the pod -- no checkpoint, no park -- when
    the sum passes it, so the pod's limit IS the sum (`ephemeral_gib`): each
    volume's own sizeLimit binds first, and never the pod's. requests == limits
    on that number (invariant 7). The memory-medium /dev/shm is not in here: it
    is charged to the memory limit, not to ephemeral-storage.
    """

    workspace_gib: int
    tmp_gib: int
    home_gib: int

    @property
    def ephemeral_gib(self) -> int:
        return self.workspace_gib + self.tmp_gib + self.home_gib


#: Per resource class, because one formula cannot fit them all under
#: Autopilot's 10 GiB. tests/unit/control_plane/test_gke_workspace_fits.py
#: renders the GKE manifest for every class and holds sum(sizeLimits) <=
#: ephemeral-storage <= AUTOPILOT_MAX_EPHEMERAL_GIB.
#:
#: WHAT EACH VOLUME HOLDS, read from the worker (#893). Every value below is
#: sized from this, not from a formula:
#:
#: * workspace -- `<WORKSPACE_ROOT>/<attempt>/` (agent_worker/workspace.py):
#:   the checkout, its `node_modules` and `.venv`, and the agent's HOME
#:   (`work/`: the CLI's own state, which is checkpointed). `restore/` stages
#:   a downloaded checkpoint here too, before the agent starts.
#: * cache -- NOT a GkeDisk volume since CACHE-PVC: a CACHE_VOLUME_GIB network
#:   disk (below). `<CACHE_MOUNT>/<attempt>/` (agent_worker/cachedirs.py,
#:   CACHE-SCRATCH): UV_CACHE_DIR, npm_config_cache, PIP_CACHE_DIR,
#:   TF_PLUGIN_CACHE_DIR, XDG_CACHE_HOME and the agent's TMPDIR, so every
#:   `mkdtemp` and pytest/xdist temp tree too. Emptied at the start of every
#:   attempt, never checkpointed, never uploaded. uv cannot hard-link a venv
#:   across volumes, so it copies (`UV_LINK_MODE=copy`): the venv in the
#:   checkout is as large as before, and what left the workspace is what the
#:   caches hold beyond it -- npm's tarballs, downloads, builds, terraform's
#:   providers and all of TMPDIR.
#: * tmp -- the WORKER's own temp dir: its TMPDIR is unset, so `checkpoint.py`'s
#:   `tempfile.TemporaryDirectory` builds the archive here, while the agent
#:   runs, up to `max_checkpoint_bytes` (2 GiB). The agent reaches this volume
#:   only by naming `/tmp` literally -- `cd /tmp && git clone ...` -- which no
#:   environment variable can redirect. That is what evicted
#:   task_22e763906edd4edcb630 three times on 2026-10-08 (`Usage of EmptyDir
#:   volume "tmp" exceeds the limit "2Gi"`): at 2 GiB, a literal /tmp write
#:   and a checkpoint archive shared the same 2 GiB.
#: * home -- `/home/swarm`, the image's HOME, which only the worker process
#:   itself inherits: no child it starts keeps it (the runner gets `work/`,
#:   git gets the attempt's `private/`). It holds the worker's own dotfiles and
#:   nothing of the agent's, which is why it can give up three of its four GiB.
#:
#: WHY THE CACHES MOVED AFTER ALL (CACHE-SCRATCH, reversing #893's option a).
#: #893 left them on the workspace volume because pointing the variables at
#: another directory on the SAME volume would move nothing. On 2026-10-15
#: task_41ae250f, task_c6a07695 (CI-fix rounds on PR 1338) and task_44c61c77
#: (BROWSER-CLASS implement) were evicted twice each with `Usage of EmptyDir
#: volume "workspace" exceeds the limit "5Gi"`, and the owner moved them to a
#: volume of their OWN, which does move them. None of these variables reaches a
#: literal `/tmp`; that is still the `tmp` volume's.
#:
#: WHY THE CACHE LEFT THE LOCAL DISK (CACHE-PVC, owner decision 2026-10-15
#: ~04:00 PDT). The cache volume CACHE-SCRATCH (PR 1360) added was a 2 GiB
#: emptyDir, and task_247c87a8714c4977921a (SEC-INJECT-TYPESAFE implement) was
#: evicted on it twice -- `EmptyDir volume "cache" exceeds 2Gi` -- and not
#: retried: one uv sync and one npm install of this repository fill 2 GiB.
#: There was no local GiB left to give it: Autopilot caps a general-purpose
#: pod's ephemeral-storage at 10 GiB (AUTOPILOT_MAX_EPHEMERAL_GIB), and the
#: split was already 10. So the cache is a CACHE_VOLUME_GIB (20 GiB)
#: pd-balanced generic ephemeral volume: outside the 10 GiB cap entirely, sized
#: by the owner, deleted with the pod, and a full one is ENOSPC in the tool
#: that filled it rather than an eviction of the pod. Its 2 local GiB went
#: back to /tmp and HOME, one each.
#:
#: THE TRADE, stated: a disk attach is on every GKE start's critical path. The
#: pod waits, after it is scheduled, for the CSI driver to create a disk in the
#: node's zone (WaitForFirstConsumer) and attach it before the container can
#: start -- tens of seconds that a local emptyDir never cost. UNMEASURED when
#: this was written: docs/browser-startup-speed.md §11 has the slot for it
#: beside the warm 6 s / cold 125 s it adds to. And a disk outlives the run by
#: as long as the pod does: a finished Job keeps its pod for
#: ttlSecondsAfterFinished (ten minutes), so the tenant quota admits one claim per
#: Job, not per running pod (kubernetes/namespaces/tenant-namespace.yaml).
#:
#: HOW GKE FAILS WHEN A VOLUME FILLS, compared with Cloud Run. Cloud Run's
#: workspace is a memory-medium tmpfs with a size (`workspace_size_gib`): a
#: write past it fails in the writing process with ENOSPC, and the worker
#: survives to checkpoint and report it. A disk emptyDir's sizeLimit is NOT a
#: filesystem size: the write succeeds, kubelet's eviction manager notices on
#: its next scan, and the whole pod is evicted -- no ENOSPC, no checkpoint, no
#: park, the Job Failed. That cannot be made in-process on Autopilot (a
#: memory-medium /tmp would be charged to the 8 GiB memory limit and OOM-kill
#: the agent instead). What this layout does is make the limit hard to reach;
#: what the reconciler does when it is reached anyway is name the eviction and
#: retry it at most once (`reconciler.detect.detect_evicted`).
#:
#: standard: workspace 5 + /tmp 3 + HOME 2 = 10, Autopilot's maximum (CACHE-PVC,
#:   the owner's preview, 2026-10-15), plus the 20 GiB network-disk cache. It
#:   was 5 + cache 2 + /tmp 2 + 1 (CACHE-SCRATCH, the same day), 5 + 4 + 1
#:   (#893), and 4 + 2 + 4 before that (contract request 53). The cache's
#:   2 GiB went back one to /tmp -- 1 GiB above the 2 GiB checkpoint cap, which
#:   the test still holds every class to, for an agent's literal /tmp writes
#:   while an archive is built (task_22e763906edd4edcb630's eviction, #893) --
#:   and one to HOME. An agent that writes more than that 1 GiB under a
#:   LITERAL /tmp while a checkpoint is being built can still be evicted on
#:   /tmp; the reconciler names and retries such an eviction once
#:   (`detect_evicted`). The history of the other numbers, for the record:
#:   - /tmp 4: the 2 GiB checkpoint archive (`max_checkpoint_bytes`) plus 2 GiB
#:     an agent can write under a literal /tmp while one is being built, and 4
#:     when none is. The checkpoint cap still fits on its own:
#:     tests/unit/control_plane/test_gke_workspace_fits.py holds tmp >= the cap.
#:   - workspace 5: one more than Cloud Run's 4 GiB tmpfs (the test holds it no
#:     smaller); until CACHE-SCRATCH it also carried the agent's TMPDIR and
#:     caches, and it keeps the GiB now that they are on the cache volume.
#:     task_5ccac63ae3724e72b5cd was evicted on the 4 GiB workspace once on
#:     2026-10-08.
#:   - HOME 2: what is left (it was 1 until CACHE-PVC). It holds the worker's
#:     own files only (above).
#:   On Cloud Run claude-code's /tmp and HOME sit in the container's in-memory
#:   writable layer, bounded only by the 8 GiB memory limit; on GKE all three
#:   are disk, and their sum is the pod's ephemeral-storage limit.
#:
#: browser: workspace 4 + /tmp 3 + HOME 3 = 10 (CACHE-PVC, the owner's preview,
#: 2026-10-15), plus the 20 GiB network-disk cache. It was 4 + cache 2 + 2 + 2
#: (CACHE-SCRATCH), which had taken the owner's 4 + 2 + 2 below from 8 GiB to
#: Autopilot's 10; the cache's 2 GiB went one each to /tmp and HOME.
#: claude-code-browser runs the same uv and npm work as claude-code on this
#: class. The 8 was also the class's disk_gib until contract request 79
#: (2026-10-14) made that 4; disk_gib sizes Cloud Run's memory tmpfs, and on GKE
#: these volumes are disk, outside the 8 GiB memory limit, so the request left
#: this layout as the owner set it.
#: Owner decisions 2026-10-07 (contract request 53): it was workspace 8 + /tmp 2 +
#: HOME 4 = 14 GiB of sizeLimits against an 8 GiB pod limit, so the pod could be
#: evicted before any one volume was full; then 5 + 1 + 2, which left /tmp below
#: the checkpoint cap. /tmp must hold the worker's largest checkpoint archive
#: (agent_worker/config.py max_checkpoint_bytes = 2 GiB), so /tmp is 2 and the
#: workspace gives up the GiB: 4 + 2 + 2. Left as the owner set it: #893 is
#: claude-code's eviction, and browser's numbers are a decision of their own.
#:
#: large: 5 + 3 + 2 = 10, standard's layout. No GKE profile can reach it --
#: none is `large`, and a step's resource_class override may only shrink
#: (swarm_api.validation.validate_resource_class_override) -- but the manifest
#: renders any class it is given, and its disk_gib of 16 alone is past what
#: Autopilot admits, so a pod sized from it would never start.
GKE_DISK: dict[str, GkeDisk] = {
    "standard": GkeDisk(workspace_gib=5, tmp_gib=3, home_gib=2),
    "browser": GkeDisk(workspace_gib=4, tmp_gib=3, home_gib=3),
    "large": GkeDisk(workspace_gib=5, tmp_gib=3, home_gib=2),
}


def scratch_volumes_value(disk: GkeDisk, *, home_volume: str = "home",
                          home_mount: str = WORKER_HOME) -> str:
    """SCRATCH_VOLUMES_ENV's value: `<volume>=<mountPath>:<sizeLimit GiB>`, comma separated.

    `workspace=/workspace:5,tmp=/tmp:3,home=/home/swarm:2` for `standard`.
    The volume is the emptyDir's `name`, which is what kubelet's eviction
    message says (`Usage of EmptyDir volume "tmp" exceeds the limit "3Gi"`),
    so the worker can match a reading to an eviction. Built from the constants
    `GkeJobDispatcher._manifest` builds the volumes from, so it cannot drift.
    The LOCAL disk volumes only: not the cache, a network disk that is never
    an eviction, and not dshm, a memory tmpfs charged to the memory limit.
    `home_volume`/`home_mount` are for kubernetes/render.py's gvisor template,
    whose HOME is a volume named `work` at /work.
    """
    return ",".join((
        f"workspace={WORKSPACE_MOUNT}:{disk.workspace_gib}",
        f"tmp=/tmp:{disk.tmp_gib}",
        f"{home_volume}={home_mount}:{disk.home_gib}",
    ))


def gke_disk(rc: Any) -> GkeDisk:
    """The scratch volumes of a GKE worker pod of this resource class.

    A class added to the frozen catalogue without a row here is refused at
    dispatch, loudly, rather than sized by a guess that may not fit Autopilot.
    """
    try:
        return GKE_DISK[rc.name]
    except KeyError:
        raise ValueError(
            f"resource class {rc.name!r} has no GKE disk layout in GKE_DISK "
            "(apps/scheduler/scheduler/dispatch.py); add one whose sum fits "
            f"AUTOPILOT_MAX_EPHEMERAL_GIB ({AUTOPILOT_MAX_EPHEMERAL_GIB})"
        ) from None


def cpu_quantity(cores: float) -> str:
    """`cores` as a Kubernetes CPU quantity in millicores: 4 -> "4000m".

    Millicores so a quarter-core size (docs/design/runner-sizes.md §4.2's
    step) renders exactly, where `str(int(cores))` truncated 2.25 to "2".
    """
    return f"{round(cores * 1000)}m"


def cpu_burst_ratio(rc: Any) -> float:
    """The class's CPU limit over its request: 2 today, 1 for a class rolled
    back to `cpu_limit=None` (contract request RS-1's rollback)."""
    return rc.cpu_limit_cores / rc.cpu


def gke_ephemeral_storage_gib(rc: Any) -> int:
    """The ephemeral-storage a GKE worker pod of this resource class requests and is limited to."""
    return gke_disk(rc).ephemeral_gib


def profile_model(settings: Any, profile: RunnerProfile) -> str | None:
    """The model this profile's agent runs when a step names no class, or None.

    The `MODEL` of every Cloud Run Job this scheduler creates (#226), so a Job
    run any way other than `dispatch` runs the profile's default class, as a
    terraform Job does. With WORKER_CLASS_MODELS set it is that map's pin for
    the profile's `default_model_class` (#1435, §4.2): the map terraform's own
    Jobs take their `MODEL` from, through the same default. Without it, it is
    WORKER_MODELS, the profile-keyed map that map replaces, read for as long as
    terraform derives it (one release, so a scheduler rolled back alone still
    finds it). None means nothing pins one, and the Job carries no MODEL.
    """
    class_models = getattr(settings, "worker_class_models", None) or {}
    if class_models:
        if profile.default_model_class is None:
            return None
        return class_models.get(profile.default_model_class)
    models = getattr(settings, "worker_models", None) or {}
    value = models.get(profile.name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def step_model(settings: Any, profile: RunnerProfile, task: Task) -> str | None:
    """The model ID this step's worker runs, as `MODEL` in `worker_env`, or None.

    #1435, docs/design/model-per-step.md §4.5: ONE resolution, for both
    backends. The class is the frozen `step_model_class` over the step's own
    `model_class` -- the rule swarm-api applies at submission and the worker
    at start, so the three never resolve a step differently -- and the ID is
    the scheduler's WORKER_CLASS_MODELS entry for it. Never `task.model`,
    never anything in `input` or `metadata`: a caller names a class, the
    platform picks the ID (invariant 10).

    None for a profile with no classes (codex, mock, generic, ...): no MODEL,
    as before. An unknown class, one the profile does not accept, or one with
    no pin raises `model_not_pinned`, which returns the lease and the task. It
    never falls back to the profile's default, and never to the CLI's own,
    which is how the QA task of 2026-09-26 ran claude-sonnet-5 on an Opus
    profile. The startup check makes a missing pin unreachable once the map is
    set; this is the second line, for a document written past the API.

    WITHOUT A CLASS MAP (WORKER_CLASS_MODELS unset: the local emulator loop,
    or a scheduler configured before #1435), a step naming a class refuses,
    and a step naming none gets None here: the profile's model then reaches it
    as it did before, on the Job (`profile_model`) or, on GKE, from the pod
    manifest. The per-execution environment carries a MODEL only from the
    class map.
    """
    try:
        model_class = step_model_class(profile, task.model_class)
    except ValueError as exc:
        raise DispatchError(
            f"task {task.id} on {profile.name}: {exc}", code="model_not_pinned"
        ) from None
    if model_class is None:
        return None
    class_models = getattr(settings, "worker_class_models", None) or {}
    if not class_models and task.model_class is None:
        return None
    model_id = class_models.get(model_class)
    if not isinstance(model_id, str) or not model_id:
        raise DispatchError(
            f"task {task.id} on {profile.name}: model class {model_class!r} has no "
            "pin in WORKER_CLASS_MODELS",
            code="model_not_pinned",
        )
    return model_id


#: Pod hardening. kubernetes/namespaces/tenant-namespace.yaml holds Pod Security
#: Admission at `enforce: baseline` rather than `restricted` precisely because
#: the manifest this module builds carried no securityContext at all, so
#: enforcing the stronger policy would have rejected every job the dispatcher
#: created. These two blocks are what `restricted` asks for, and they mirror
#: kubernetes/worker-templates/worker-job-browser.yaml field for field -- which
#: was a claim until 2026-09-24 and is now an assertion:
#: test_the_rendered_job_and_the_dispatched_job_are_the_same_job renders both
#: from the same inputs and fails on any field that changes what the pod does.
POD_SECURITY_CONTEXT: dict[str, Any] = {
    "runAsNonRoot": True,
    "runAsUser": WORKER_UID,
    "runAsGroup": WORKER_UID,
    "fsGroup": WORKER_UID,
    "seccompProfile": {"type": "RuntimeDefault"},
}

#: The container runs untrusted agent code from a caller-supplied repository on
#: a cluster shared with other tenants' pods, so it drops every capability and
#: cannot gain privilege. Chromium's own sandbox needs user namespaces and
#: CAP_SYS_ADMIN and stays off; the isolation is the pod and the namespace, and
#: handing a capability back to regain the inner sandbox would be a bad trade.
CONTAINER_SECURITY_CONTEXT: dict[str, Any] = {
    "allowPrivilegeEscalation": False,
    "privileged": False,
    "readOnlyRootFilesystem": True,
    "runAsNonRoot": True,
    "runAsUser": WORKER_UID,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}

#: THE POD'S RESOLVER, WITHOUT THE CLUSTER'S SEARCH PATH (#341; owner decision
#: 2026-09-29).
#:
#: Under GKE's default `dnsPolicy: ClusterFirst` a pod gets `ndots:5` and the
#: search list `<ns>.svc.cluster.local svc.cluster.local cluster.local ...`, so
#: a short name a browser task opens -- `kubernetes.default`,
#: `swarm-api.swarm-system` -- is tried under every cluster suffix and reaches a
#: cluster Service. `url_refusal` cannot close that: it sees the string a caller
#: typed, never what the pod's resolver does with it.
#:
#: `ndots:1` and `searches: []` alone would not do it either. Under
#: ClusterFirst (and Default) a pod's `searches` are APPENDED to the policy's
#: own, so `[]` removes nothing, and ndots only orders the tries: the absolute
#: `kubernetes.default.` fails, and the resolver falls back to the search list.
#: `None` is the one policy under which this dnsConfig is the WHOLE resolver,
#: and the API server then requires a nameserver.
#:
#: The nameserver is NodeLocal DNSCache's address, 169.254.20.10: what a
#: ClusterFirst pod on swarm-autopilot is ALREADY given (measured 2026-09-24,
#: kubernetes/network-policies/allow-egress.yaml), which GKE Autopilot always
#: runs ("enabled by default, and you can't disable it"), and which the tenant
#: egress policy's rule 1b opens on 53/UDP+TCP. So a fully qualified name --
#: an internet host, `firestore.googleapis.com`, or a cluster name spelled out
#: as `kube-dns.kube-system.svc.cluster.local` -- goes to exactly the resolver
#: it went to before, and only the search-path trial is gone.
#:
#: Every GKE profile, deliberately: browser was the first, and every GKE
#: profile runs tenant code in the same cluster, so it gets the same
#: resolver (test_gke_browser_pod_dns.py parametrises over all of them).
#: GKE pods only: Cloud Run has no cluster search path. Mirrored field for
#: field in kubernetes/worker-templates/worker-job-browser.yaml, and held there
#: by tests/unit/scheduler/test_gke_browser_pod_dns.py.
GKE_NODE_LOCAL_DNS_IP = "169.254.20.10"
GKE_POD_DNS_POLICY = "None"
GKE_POD_DNS_CONFIG: dict[str, Any] = {
    "nameservers": [GKE_NODE_LOCAL_DNS_IP],
    "searches": [],
    "options": [{"name": "ndots", "value": "1"}],
}

#: THE LIVE BROWSER'S DISPLAY SIDECAR (docs/design/live-browser.md §3.1, §8.2,
#: §8.3; lane L5, part of #1030).
#:
#: A pod of a `live_browser` profile (`claude-code-browser`, contract request
#: 71) carries a second container, `display`: Xvfb, the platform's headed
#: Chromium and the live gate, under a uid of its own. Rendered only while
#: `settings.live_browser_sidecar` is on, the rollout switch, because the gate
#: it runs is built by lanes L1-L4 (SchedulerSettings.live_browser_sidecar).
#:
#: WHY A SECOND UID. It is the only boundary available inside one pod: a
#: same-uid process can read the other's /proc/<pid>/environ, signal it and
#: open its sockets. The agent runs in the worker container as WORKER_UID; the
#: display, its X socket (in the sidecar's own /tmp) and the browser profile
#: (cookies) are in a container the worker container shares no such volume
#: with. The images create the uid (images/agent-runtime-browser/Dockerfile,
#: `swarm-display`); the pod repeats it so the cluster enforces it. Note what
#: the pod-level `fsGroup` (WORKER_UID) does: it is a supplementary group of
#: BOTH containers, which is what lets the worker move a download the sidecar
#: wrote and the sidecar read an upload the worker wrote. It opens nothing
#: else, because the sidecar's private volumes are not mounted in the worker
#: container at all -- the mount, not the mode bits, is the boundary there.
#:
#: WHY NATIVE (an init container with `restartPolicy: Always`). A Job's pod
#: completes when its containers exit, so an ordinary second container that
#: keeps running would hold the attempt open forever. A native sidecar starts
#: BEFORE the worker -- and, with the startup probe below, is listening on
#: ctl.sock before the worker's lifecycle begins, so the worker's
#: first-registration-wins slot (§5.4.2) is taken before the agent exists --
#: and is stopped after the worker exits.
DISPLAY_UID = 10002

#: The gate's entrypoint. The worker's container has NO command (the image
#: ENTRYPOINT is the worker lifecycle); the sidecar names its own, under tini
#: like the worker, so Xvfb and Chromium are reaped.
DISPLAY_COMMAND = ("/usr/bin/tini", "--", "python", "-m", "agent_worker.livegate")

#: The label the live egress policy selects (`swarm-allow-live-egress`, §8.3,
#: lane L6). Only a pod carrying it can reach the relay on TCP 8080; the
#: dispatcher sets it on live-browser pods and nowhere else.
LIVE_POD_LABEL = ("swarm-live", "enabled")

#: RESIDENTIAL EGRESS FOR THE LIVE BROWSER (docs/browser-egress.md §10.3; lane
#: EGRESS-DISPATCH, part of #1030). What the gate reads on the `display`
#: sidecar (agent_worker.livegate.egress EGRESS_ENV, BYPASS_ENV) and the label
#: `swarm-allow-egress-proxy` (kubernetes/network-policies/allow-egress-proxy.yaml)
#: selects to open TCP 3128 to swarm-egress. Set together by
#: `with_browser_egress`, only when `browser_egress_on`, and nowhere else.
BROWSER_EGRESS_ENV = ("SWARM_BROWSER_EGRESS", "proxy")
BROWSER_EGRESS_BYPASS_ENV = "SWARM_EGRESS_BYPASS"
EGRESS_POD_LABEL = ("swarm-egress", "proxy")

#: THE TYPESAFE FORWARDER'S LABEL (docs/design/workspace-secrets.md §3.5,
#: SEC-MIGRATE-TYPESAFE; lane TS-MIGRATE-POD, part of #1347). What
#: `swarm-allow-typesafe-fwd` (kubernetes/network-policies/allow-typesafe-fwd.yaml,
#: the tenant side) and `swarm-typesafe-fwd-ingress` (the forwarder's own
#: manifest) both select: only a pod carrying it reaches swarm-typesafe-fwd on
#: TCP 8080. Set by `_with_judgment`, only when `typesafe_forwarder_on`.
TYPESAFE_POD_LABEL = ("swarm-typesafe", "inject")
#: THE WORKER'S FORWARDER SWITCH (lane TS-INJECT, part of #1347), set on the
#: worker container by `_with_judgment` with TYPESAFE_POD_LABEL and never
#: otherwise: a labelled pod whose worker still reads the key file, or a
#: switched worker the policies do not open the forwarder to, is half a
#: migration. RESTATED from `agent_worker.typesafe_shim.SWITCH_ENV` and
#: `SWITCH_ON`, because the scheduler's image does not carry the worker;
#: tests/unit/control_plane/test_typesafe_switch_parity.py holds them equal.
TYPESAFE_INJECT_ENV = ("SWARM_TYPESAFE_INJECT", "on")

#: The three volumes both containers mount, and only these (§3.1). All memory,
#: so inside the pod's memory limit, and none under WORKSPACE_MOUNT, so none
#: is checkpointed.
LIVE_GATE_MOUNT = "/run/swarm-gate"
LIVE_GATE_CTL = f"{LIVE_GATE_MOUNT}/ctl.sock"
LIVE_DOWNLOADS_MOUNT = "/run/swarm-browser/downloads"
LIVE_UPLOADS_MOUNT = "/run/swarm-browser/uploads"
#: The sidecar's own: the browser profile (wiped at attempt end, §5.5).
LIVE_PROFILE_MOUNT = "/run/swarm-display/profile"
#: The gate's CDP proxy, for the agent's Playwright (`connectOverCDP`, §3.2).
#: Loopback: the pod's containers share a network namespace, nothing else
#: does, and the tenant ingress policy admits nothing to the pod.
LIVE_CDP_LISTEN = "127.0.0.1:9222"


@dataclass(frozen=True)
class LiveDisplayShare:
    """What the `display` sidecar takes out of a resource class.

    THE CLASS DOES NOT CHANGE; IT IS SPLIT (§8.2). The worker keeps the rest,
    each container has memory and disk requests == limits (invariant 7), and
    the two REQUESTS sum to the class, so admission's unit arithmetic and the
    pod Autopilot bills are what they were. CPU may burst (RS-1): the worker
    to its request x the class's ratio, the display to `cpu_limit`, or that
    ratio when it is None, and never past the class's own ratio, so a class
    that does not burst renders a live pod that does not either. `ephemeral_gib` is the sidecar's container-level disk
    (its logs; its root is read-only and every volume it mounts is memory);
    the worker's ephemeral-storage gives up the same amount, so the pod's sum
    is still `gke_ephemeral_storage_gib(rc)` and every disk volume's
    sizeLimit still binds before the pod's limit does (GkeDisk).
    """

    cpu: int
    memory_gib: int
    ephemeral_gib: int = 1
    cpu_limit: float | None = None


#: Per class, like GKE_DISK, and refused for a class without a row.
#:
#: browser: display 2 vCPU / 5 GiB, worker 2 / 3, of the class's 4 / 8.
#:   Chromium, Xvfb and the encoder on one side; claude-code, Node and the
#:   worker on the other. The owner's decision of 2026-10-20 (BROWSER-SPLIT,
#:   part of #1689), within the same 4 cores so the pod's size and cost do
#:   not move: it was display 3 / worker 1 (contract request 79), and
#:   browser-profile attempts that day ran at cpu_limit_cores 1.0, so a lane
#:   running `npm ci` and a dev server (the visual-QA capture) did it on one
#:   core. The worker goes from 1 core to 2. The display's measured peak
#:   (docs/browser-startup-speed.md §11, 2026-10-15, four pods, eight
#:   samples) was 1.90 cores, so 2 leaves it about 1.05x headroom at that
#:   peak; past it Chromium is throttled, not killed. The memory split did
#:   not move: the display peaked at 1.48 GiB, the worker at 0.37, so the
#:   worker has about 8x its memory peak. The sidecar's resident tmpfs
#:   (/dev/shm 2, profile 1, /tmp 0.25) plus its measured peak is 4.73 of its
#:   5 GiB; downloads (1) do not stay resident because the worker moves each
#:   one into work/. A heavier page or a download in flight at a peak is an
#:   OOM kill, whose cost the worker's periodic checkpoint bounds (invariant 8).
#:   live_display_share() still refuses a share that leaves the worker nothing.
#:   CPU BURSTS (SIZES-SCHED-1, docs/design/runner-sizes.md §4.1, owner
#:   2026-10-20): the worker to 4, the class's 2x; the display to 3, its size
#:   before BROWSER-SPLIT, which keeps the room it had for Chromium's spikes
#:   past that 1.90 peak while the agent keeps its 2. Pod limit 7, request 4.
#: standard: display 2 / 4, worker 2 / 4. A workflow step may shrink a task's
#:   class (swarm_api.validation.validate_resource_class_override), and admission
#:   charged the smaller class, so the pod must fit it; half each, still with
#:   the 2 GiB /dev/shm on the sidecar's side. The display bursts to 3, as
#:   browser's: it runs the same Chromium.
#: large: no live profile can reach it (a step only shrinks), but the manifest
#:   renders any class it is given; the design's starting sidecar (§8.2, what
#:   browser's was until contract request 79), the rest to the worker.
LIVE_DISPLAY_SHARE: dict[str, LiveDisplayShare] = {
    "browser": LiveDisplayShare(cpu=2, memory_gib=5, cpu_limit=3),
    "standard": LiveDisplayShare(cpu=2, memory_gib=4, cpu_limit=3),
    "large": LiveDisplayShare(cpu=5, memory_gib=10),
}

#: The display container's hardening: CONTAINER_SECURITY_CONTEXT's every
#: field, under DISPLAY_UID and a primary group of its own.
DISPLAY_SECURITY_CONTEXT: dict[str, Any] = {
    **CONTAINER_SECURITY_CONTEXT,
    "runAsUser": DISPLAY_UID,
    "runAsGroup": DISPLAY_UID,
}


def live_display_share(rc: Any) -> LiveDisplayShare:
    """The sidecar's part of resource class `rc`, refused loudly when it has no row."""
    try:
        share = LIVE_DISPLAY_SHARE[rc.name]
    except KeyError:
        raise ValueError(
            f"resource class {rc.name!r} has no live-browser split in LIVE_DISPLAY_SHARE "
            "(apps/scheduler/scheduler/dispatch.py); add one that leaves the worker room"
        ) from None
    if share.cpu >= rc.cpu or share.memory_gib >= rc.memory_gib:
        raise ValueError(f"LIVE_DISPLAY_SHARE[{rc.name!r}] leaves the worker nothing")
    if share.cpu_limit is not None and not (
            share.cpu <= share.cpu_limit <= CPU_BURST_MAX_RATIO * share.cpu):
        raise ValueError(
            f"LIVE_DISPLAY_SHARE[{rc.name!r}]: cpu_limit {share.cpu_limit} must be between "
            f"its request {share.cpu} and {CPU_BURST_MAX_RATIO} x the request (invariant 7)"
        )
    return share


def size_of(rc: Any) -> ResourceSize:
    """Resource class `rc` as a size with no stored live split: what a pod
    is rendered at when no size was pinned for it (a caller of `_manifest`
    other than admission)."""
    return ResourceSize(cpu_request=rc.cpu, cpu_limit=rc.cpu_limit_cores,
                        memory_gib=rc.memory_gib, disk_gib=rc.disk_gib)


def live_display_on(profile: Any, settings: Any) -> bool:
    """Whether a GKE pod of `profile` carries the display sidecar: a
    live-browser profile while the rollout switch is on. One predicate, so
    the size admission records (`sizes.for_task`) and the pod rendered here
    cannot disagree about whether there is a split."""
    return bool(getattr(profile, "live_browser", False)
                and getattr(settings, "live_browser_sidecar", False))


def live_split_for(rc: Any, size: ResourceSize | None = None) -> tuple[LiveShare, LiveShare]:
    """The (worker, display) containers of a live pod of class `rc` at `size`.

    `size` defaults to the class itself (`size_of`). A size carrying a stored
    split (the browser class's, docs/design/runner-sizes.md §4.3) is rendered
    as stored; `check_size` has already held its requests and memories to
    the class's sum. Otherwise LIVE_DISPLAY_SHARE's row for the class, scaled
    to the size as before sizes were a setting: the display takes its row,
    bursting to `cpu_limit` capped at the size's own ratio, and the worker
    the rest at that ratio, so a size that does not burst renders a live pod
    that does not either. Refused, loudly, when that leaves the worker nothing.
    """
    size = size if size is not None else size_of(rc)
    if size.live_split is not None:
        return size.live_split
    share = live_display_share(rc)
    if share.cpu >= size.cpu_request or share.memory_gib >= size.memory_gib:
        raise ValueError(f"LIVE_DISPLAY_SHARE[{rc.name!r}] leaves a {size.cpu_request:g}-core, "
                         f"{size.memory_gib} GiB worker nothing")
    ratio = size.cpu_limit / size.cpu_request
    worker_cpu = size.cpu_request - share.cpu
    display_cpu_limit = share.cpu * ratio
    if share.cpu_limit is not None:
        display_cpu_limit = min(share.cpu_limit, display_cpu_limit)
    return (
        LiveShare(cpu_request=worker_cpu, cpu_limit=worker_cpu * ratio,
                  memory_gib=size.memory_gib - share.memory_gib),
        LiveShare(cpu_request=share.cpu, cpu_limit=display_cpu_limit,
                  memory_gib=share.memory_gib),
    )


def with_live_display(job: dict[str, Any], *, rc: Any,
                      split: tuple[LiveShare, LiveShare] | None = None) -> dict[str, Any]:
    """`job`, a rendered GKE worker Job, with the display sidecar added.

    Applied to the finished manifest, so a pod that is not live is not merely
    "the same by construction" -- it is the same object this function never
    touched, which test_dispatch_live_sidecar.py holds byte for byte.

    `split` is the (worker, display) pair admission pinned for this attempt
    (`sizes.Sized.live_split`, SIZES-SCHED-2); None renders the class's own
    split (`live_split_for(rc)`).
    """
    job = copy.deepcopy(job)
    share = live_display_share(rc)
    worker_share, display_share = split if split is not None else live_split_for(rc)
    disk = gke_disk(rc)
    template = job["spec"]["template"]
    spec = template["spec"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    image = worker["image"]

    template["metadata"]["labels"][LIVE_POD_LABEL[0]] = LIVE_POD_LABEL[1]

    # The split. Memory and disk equal in both directions on both containers
    # (invariant 7); the CPU requests sum to the size and each limit stays
    # within its own burst ratio (RS-1, LiveDisplayShare, `live_split_for`).
    # Disk is the class's layout (GKE_DISK): an admin does not edit it (§7 Q5).
    worker_fixed = {
        "memory": f"{worker_share.memory_gib}Gi",
        "ephemeral-storage": f"{disk.ephemeral_gib - share.ephemeral_gib}Gi",
    }
    display_fixed = {
        "memory": f"{display_share.memory_gib}Gi",
        "ephemeral-storage": f"{share.ephemeral_gib}Gi",
    }
    worker["resources"] = {
        "requests": {"cpu": cpu_quantity(worker_share.cpu_request), **worker_fixed},
        "limits": {"cpu": cpu_quantity(worker_share.cpu_limit), **worker_fixed},
    }

    # /dev/shm moves to where Chromium runs. The agent's own headless
    # Chromium, if it starts one, is not shown (§3.2) and gets the runtime's
    # default /dev/shm, which `--disable-dev-shm-usage` copes with.
    worker["volumeMounts"] = [m for m in worker["volumeMounts"] if m["name"] != "dshm"]
    worker["volumeMounts"] += [
        {"name": "live-gate", "mountPath": LIVE_GATE_MOUNT},
        # Read-write: the worker MOVES a download into work/.
        {"name": "live-downloads", "mountPath": LIVE_DOWNLOADS_MOUNT},
        {"name": "live-uploads", "mountPath": LIVE_UPLOADS_MOUNT},
    ]
    # Where the worker (lane L4) finds the gate. No DISPLAY: the worker sets
    # none in the agent's environment either (Workspace.child_env), so a
    # headed launch in the worker container fails loudly.
    worker["env"] = worker["env"] + [
        {"name": "SWARM_GATE_CTL", "value": LIVE_GATE_CTL},
        {"name": "SWARM_CDP_ENDPOINT", "value": f"http://{LIVE_CDP_LISTEN}"},
        {"name": "SWARM_BROWSER_DOWNLOADS", "value": LIVE_DOWNLOADS_MOUNT},
        {"name": "SWARM_BROWSER_UPLOADS", "value": LIVE_UPLOADS_MOUNT},
    ]

    spec["initContainers"] = [{
        "name": "display",
        "image": image,
        "command": list(DISPLAY_COMMAND),
        # Native sidecar: started before the worker, stopped after it.
        "restartPolicy": "Always",
        # Paths only. No task, tenant, lease or attempt identifier and no
        # secret: the tunnel ticket arrives over ctl.sock from the worker
        # (§3.3, §5.4.2), and the gate needs to know nothing else.
        "env": [
            # Chromium writes under HOME (crash reports, caches); the root is
            # read-only, so HOME is the sidecar's own memory /tmp.
            {"name": "HOME", "value": "/tmp"},
            {"name": "DISPLAY", "value": ":0"},
            {"name": "SWARM_GATE_CTL", "value": LIVE_GATE_CTL},
            {"name": "SWARM_BROWSER_PROFILE_DIR", "value": LIVE_PROFILE_MOUNT},
            {"name": "SWARM_BROWSER_DOWNLOADS", "value": LIVE_DOWNLOADS_MOUNT},
            {"name": "SWARM_BROWSER_UPLOADS", "value": LIVE_UPLOADS_MOUNT},
            {"name": "SWARM_CDP_LISTEN", "value": LIVE_CDP_LISTEN},
        ],
        "resources": {
            "requests": {"cpu": cpu_quantity(display_share.cpu_request), **display_fixed},
            "limits": {"cpu": cpu_quantity(display_share.cpu_limit), **display_fixed},
        },
        "securityContext": copy.deepcopy(DISPLAY_SECURITY_CONTEXT),
        # The worker starts only once this passes: the gate is listening, so
        # the worker's registration is the first one (§5.4.2). A gate that
        # restarts mid-attempt is probed again, and refuses every registration
        # whatever the probe says (the restart marker in its /tmp).
        "startupProbe": {
            "exec": {"command": ["/usr/bin/test", "-S", LIVE_GATE_CTL]},
            "periodSeconds": 1,
            "failureThreshold": 120,
        },
        "volumeMounts": [
            # Its own /tmp: the X socket (/tmp/.X11-unix/X0) and the gate's
            # restart marker. Not mounted in the worker container.
            {"name": "display-tmp", "mountPath": "/tmp"},
            {"name": "browser-profile", "mountPath": LIVE_PROFILE_MOUNT},
            {"name": "dshm", "mountPath": "/dev/shm"},
            {"name": "live-gate", "mountPath": LIVE_GATE_MOUNT},
            {"name": "live-downloads", "mountPath": LIVE_DOWNLOADS_MOUNT},
            # The worker writes, the sidecar only reads (setInputFiles paths).
            {"name": "live-uploads", "mountPath": LIVE_UPLOADS_MOUNT, "readOnly": True},
        ],
    }]

    spec["volumes"] = spec["volumes"] + [
        {"name": "display-tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
        {"name": "browser-profile", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
        {"name": "live-gate", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
        {"name": "live-downloads", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
        {"name": "live-uploads", "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"}},
    ]
    return job

def browser_egress_on(settings: Any, *, tenant_id: str, profile: Any) -> bool:
    """Whether a pod of `profile` for `tenant_id` is dispatched for residential egress.

    FROM THE SCHEDULER'S CONFIGURATION ONLY (invariant 10): the platform
    switch, the tenant by exact membership and the profile by name, the three
    tests swarm-api mints the attempt's egress ticket by (swarm_api.egress
    `_switch`). Nothing on the task is read, so no caller field can turn it
    on. A profile with no live browser has no Chromium to route and is never on.
    """
    if not getattr(settings, "browser_egress_enabled", False):
        return False
    if not getattr(profile, "live_browser", False):
        return False
    if tenant_id not in tuple(getattr(settings, "browser_egress_tenants", ())):
        return False
    return str(getattr(profile, "name", "")) in tuple(
        getattr(settings, "browser_egress_profiles", ()))


#: The workflow switch in the task's signed `metadata.dispatch` block that
#: runs the worker's Jev judgments on the step's own profile, live browser or
#: not (`swarm_api.validation.DISPATCH_SWITCHES`, the owner's experiment,
#: 2026-11-01). An on/off word; it names no backend (invariant 10).
JEV_JUDGMENTS_SWITCH = "jev_judgments"


def jev_judgments_switched_on(task: Any) -> bool:
    """Whether `task`'s dispatch block says `jev_judgments: on`. The block is
    covered by the spec signature, which the worker verifies before it acts:
    an altered block costs at most a pod label, never a judgment."""
    metadata = getattr(task, "metadata", None)
    block = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    return isinstance(block, Mapping) and block.get(JEV_JUDGMENTS_SWITCH) == "on"


def typesafe_forwarder_on(settings: Any, *, tenant_id: str, profile: Any, backend: str,
                          judgments_switched_on: bool = False) -> bool:
    """Whether this attempt's TypeSafe key is delivered by the forwarder, so its
    pod is labelled `swarm-typesafe: inject` (design §3.5).

    Also, since 2026-11-01, an attempt whose signed workflow switch says
    `jev_judgments: on` (`judgments_switched_on`, `jev_judgments_switched_on`)
    on a profile that carries the Jev client (`runs_jev_client`), live
    browser or not: the worker then resolves the workspace's TypeSafe key for
    its judgments, so the forwarder serves it as it serves the browser's.

    All four, FROM CONFIGURATION ONLY (invariant 10): the forwarder switched on,
    the tenant listed by exact name, a live-browser pod (the profile and the
    display rollout switch), and the workspace's stored judgment backend is
    `typesafe`. A TypeSafe-backed jev or judge is exactly that attempt: the
    worker resolves a TypeSafe key only for a live-browser attempt on the
    TypeSafe backend (`jevkey.prepare`), and the judge reads the key jevkey
    wrote, so neither runs on TypeSafe anywhere else. GKE only, because this is
    called only from GkeJobDispatcher: a Cloud Run Job cannot reach the
    forwarder and keeps today's path.
    """
    if not getattr(settings, "typesafe_forwarder_enabled", False):
        return False
    if tenant_id not in tuple(getattr(settings, "typesafe_forwarder_tenants", ())):
        return False
    live = (getattr(profile, "live_browser", False)
            and getattr(settings, "live_browser_sidecar", False))
    if not live and not (judgments_switched_on and runs_jev_client(profile)):
        return False
    return backend == TYPESAFE_BACKEND


#: THE RUNNERS THAT CARRY THE JEV CLIENT (`agent_worker.judge`, registered by
#: runners/cliagent.py): the worker sets the judge up in `graphstage.prepare`,
#: which does so only for these runners and never for an index run. RESTATED
#: from `graphstage.GRAPH_RUNNERS` and `indexrun.INDEXER_PROFILE`, because the
#: scheduler does not depend on the worker package;
#: tests/unit/control_plane/test_typesafe_judge_on_gke.py holds them equal.
JEV_CLIENT_RUNNERS = ("agent_worker.runners.claude_code", "agent_worker.runners.codex")
JEV_CLIENT_EXCLUDED_PROFILES = ("indexer",)
#: The worker's reading of the one settings document (`judge.attempt
#: .judge_settings`): `code_intelligence.judgment_backend` when the block
#: carries one, else the document's top-level `judgment_backend`.
CODE_INTELLIGENCE_FIELD = "code_intelligence"
JUDGMENT_BACKEND_FIELD = "judgment_backend"
#: The DispatchError code of a TypeSafe judge held off Cloud Run.
TYPESAFE_JUDGE_NEEDS_GKE = "typesafe_judge_needs_gke"


def runs_jev_client(profile: Any) -> bool:
    """Whether an attempt of `profile` can ask Jev at all."""
    if str(getattr(profile, "name", "")) in JEV_CLIENT_EXCLUDED_PROFILES:
        return False
    argv = tuple(getattr(profile, "runner_argv", ()) or ())
    return any(module in argv for module in JEV_CLIENT_RUNNERS)


#: The runner that registers the quality gates' hooks
#: (`agent_worker.qualitygates`, whose lifecycle asks only for a claude-code
#: child): the one profile family whose dispatch carries the verdict.
QUALITY_GATES_RUNNER = "agent_worker.runners.claude_code"


def runs_quality_gates(profile: Any) -> bool:
    """Whether an attempt of `profile` can run the quality gates at all."""
    return QUALITY_GATES_RUNNER in tuple(getattr(profile, "runner_argv", ()) or ())


def quality_gates_dispatch_env(gates: Callable[[str], dict[str, str]] | None,
                               tenant_id: str, profile: Any) -> dict[str, str]:
    """The quality gates' verdict for this dispatch (contract request 128;
    `judgment.read_quality_gates_env`): {} for a profile that cannot run
    them, an explicit off when this deployment reads no settings, else the
    verdict on the tenant's stored block. FROM THE STORED SETTING AND THIS
    SERVICE'S KEY ONLY, never a caller's (invariant 10)."""
    if not runs_quality_gates(profile):
        return {}
    if gates is None:
        return quality_gates_env(False, None)
    return dict(gates(tenant_id))


def judge_backend(doc: Any) -> str:
    """The judgment backend the worker's judge will resolve from `doc`, the
    tenant's `workspace_settings` document, in the worker's precedence. An
    unknown or missing value is the worker's default, internal-jev."""
    doc = doc if isinstance(doc, Mapping) else {}
    block = doc.get(CODE_INTELLIGENCE_FIELD)
    block = block if isinstance(block, Mapping) else {}
    value = block.get(JUDGMENT_BACKEND_FIELD) if JUDGMENT_BACKEND_FIELD in block \
        else doc.get(JUDGMENT_BACKEND_FIELD)
    return value if isinstance(value, str) and value in JUDGMENT_BACKENDS else DEFAULT_BACKEND


def read_judge_backend(db: Any, tenant_id: str, *, mac_key: str | None = None) -> str | None:
    """`judge_backend` of the tenant's stored document; None when unreadable,
    which `typesafe_judge_needs_gke` treats as TypeSafe (it fails closed).

    With `mac_key` (`judgment.settings_mac_key`, the SETTINGS_MAC_REQUIRED
    switch), a block whose MAC does not verify counts as unset
    (`judgment.trusted_settings`): an unsigned `code_intelligence
    .judgment_backend` is ignored and the top level decides, and an unsigned
    top level reads as the default. `pod_judge_backend` is this reading for
    the GKE pod, so the pod and the router never disagree on a value, forged
    or not."""
    try:
        doc = read_settings(db, tenant_id, mac_key=mac_key)
    except Exception as exc:  # noqa: BLE001 - said by type, decided by the caller
        log.warning("judge backend unreadable for tenant=%s: %s", tenant_id,
                    type(exc).__name__)
        return None
    return judge_backend(doc)


def pod_judge_backend(db: Any, tenant_id: str, *, mac_key: str | None = None) -> str:
    """`read_judge_backend` for the GKE pod's SWARM_JEV_BACKEND, swarm-judge
    and swarm-typesafe label (`GkeJobDispatcher._with_judgment`), with an
    unreadable document read as DEFAULT_BACKEND.

    THE ROUTER'S PRECEDENCE, NOT THE TOP LEVEL ALONE (#1334 c31): the pod read
    `judgment.read_backend`, the top-level `judgment_backend` only, while the
    router and the worker's judge prefer `code_intelligence.judgment_backend`.
    A workspace with `code_intelligence.judgment_backend: typesafe` was held on
    GKE for a TypeSafe judge and then given a pod that judged internally, with
    no forwarder label; the reverse got the forwarder label and the worker's
    forwarder switch for a judge that never asks TypeSafe.

    Unreadable is the default here, as it always was for the pod: the router
    fails closed on None by sending the attempt to GKE, and on GKE the pod's
    choice that sends nothing outside the network is internal-jev."""
    backend = read_judge_backend(db, tenant_id, mac_key=mac_key)
    return DEFAULT_BACKEND if backend is None else backend


def typesafe_judge_needs_gke(settings: Any, *, tenant_id: str, profile: Any,
                             backend: Backend, judgment: str | None) -> bool:
    """Whether this attempt must NOT run as a Cloud Run Job, because its judge
    would answer through TypeSafe (lane JUDGE-GKE, owner decision 2026-10-16
    on #1347).

    The forwarder that keeps the key out of the pod is an in-cluster Service,
    so a TypeSafe judgment belongs on GKE. All four, FROM CONFIGURATION AND
    THE STORED SETTING ONLY (invariant 10):

      * the forwarder's switch family serves the tenant: `typesafe_forwarder_
        enabled` and the tenant in `typesafe_forwarder_tenants` by exact name.
        Off, routing is exactly what it was;
      * the attempt would be a Cloud Run Job (`backend`, the catalogue's);
      * the profile carries the Jev client (`runs_jev_client`);
      * the workspace's judgment backend, read as the worker reads it
        (`judge_backend`), is `typesafe`. `None` -- the document could not be
        read -- counts as TypeSafe: the worker reads it again at setup, and an
        unread setting is not evidence that it says otherwise.

    It does not read the code-intelligence opt-in or the repository's
    override: either can only turn the judge off, so ignoring them can hold
    back an attempt that would not have judged, never let one through that
    would.
    """
    if not getattr(settings, "typesafe_forwarder_enabled", False):
        return False
    if tenant_id not in tuple(getattr(settings, "typesafe_forwarder_tenants", ())):
        return False
    if backend is not Backend.CLOUD_RUN_JOB:
        return False
    if not runs_jev_client(profile):
        return False
    return judgment is None or judgment == TYPESAFE_BACKEND


def with_browser_egress(job: dict[str, Any], *, bypass: Any) -> dict[str, Any]:
    """`job`, a live-browser Job from `with_live_display`, routed through swarm-egress.

    Adds exactly three things and changes nothing else, which
    test_dispatch_browser_egress.py holds byte for byte: the pod label the
    proxy NetworkPolicy selects, and on the `display` container -- where
    Chromium runs; the worker container gets neither -- the switch and the
    bypass list. The bypass list is configuration (egress.tf's
    `egress_bypass_hosts`); the gate adds GitHub itself, so an empty one still
    names the variable. No ticket and no forwarder address: the ticket reaches
    the gate over ctl.sock, and the gate's default forwarder is swarm-egress's
    Service.
    """
    job = copy.deepcopy(job)
    template = job["spec"]["template"]
    template["metadata"]["labels"][EGRESS_POD_LABEL[0]] = EGRESS_POD_LABEL[1]
    (display,) = [c for c in template["spec"]["initContainers"] if c["name"] == "display"]
    display["env"] = display["env"] + [
        {"name": BROWSER_EGRESS_ENV[0], "value": BROWSER_EGRESS_ENV[1]},
        {"name": BROWSER_EGRESS_BYPASS_ENV, "value": ",".join(bypass or ())},
    ]
    return job

#: THE BACKEND'S DEADLINE IS A BACKSTOP, AND IT MUST FIRE AFTER THE LIFECYCLE'S.
#:
#: The worker lifecycle enforces the task's timeout itself: at
#: `TASK_TIMEOUT_SECONDS` it stops the runner child, checkpoints, uploads,
#: writes FAILED ("runner exceeded its timeout") and releases the lease. The
#: backend's own deadline -- a GKE Job's `activeDeadlineSeconds`, a Cloud Run
#: execution's `timeout` -- exists only for a lifecycle that has wedged.
#:
#: Both used to be `task.timeout_seconds`, and the backend always won. A Job's
#: deadline counts from Job CREATION, which includes Autopilot provisioning a
#: node and pulling the image; the lifecycle's counts from `Worker.__init__`,
#: minutes later. So the Job controller SIGTERMed the pod first, and on SIGTERM
#: the lifecycle takes its INTERRUPTION path instead (`_handle_interruption`):
#: it parks the task PARKED/SCHEDULED_RETRY with `next_eligible_at=now`. At the
#: time nothing promoted a SCHEDULED_RETRY park (the scheduler's
#: `_promote_scheduled_retries` does now, and dead-letters a spent last attempt),
#: so a task that simply ran out of time never became FAILED and its workflow
#: never finished -- and even now it would burn a retry on a timeout -- the
#: "workflow stuck" symptom of incident wf_ebb3ab2d65664707a559, reached through
#: its own fix (review of PR #31). Cloud Run had the same race with a margin of
#: seconds.
#:
#: So the backend's deadline is the task's timeout PLUS:
#:
#:   * the latest a lifecycle can START and still be running --
#:     `dispatch_timeout_seconds` after the lease. The reconciler reclaims any
#:     attempt with no heartbeat by then (reconciler/detect.py), and the
#:     lifecycle heartbeats before any slow work, so a lifecycle that started
#:     later has already been fenced. Read from the scheduler's settings, the
#:     same value admission writes into `lease.dispatch_deadline`, never restated.
#:   * WORKER_FINALISE_BUDGET_SECONDS, below.
#:
#: The headroom is the BACKEND's, not the agent's: TASK_TIMEOUT_SECONDS in the
#: worker's environment is still exactly `task.timeout_seconds`.
#:
#: The cost of the headroom is that a wedged lifecycle is killed later. It is
#: small: a wedged lifecycle stops heartbeating, and the reconciler reclaims a
#: silent lease after `heartbeat_grace_seconds` (90) and terminates the
#: execution itself, long before either backend deadline.
#:
#: tests/unit/control_plane/test_dispatch_manifests.py holds both dispatchers to
#: it, feeding the worker's own `WorkerConfig.from_env` the environment the
#: dispatcher built; tests/unit/worker/test_kubernetes_manifests.py holds the
#: three YAML templates to it, and its parity test holds `render.py`'s value
#: equal to this one's (render.py imports this function).
#:
#: WHAT THE LIFECYCLE DOES AFTER ITS OWN DEADLINE, which the backend must wait
#: for: stop the runner child (TERMINATION_GRACE_SECONDS, 20 by default), take
#: the final checkpoint, harvest the git changes (GIT_HARVEST_TIMEOUT_SECONDS,
#: 120), upload the outputs and write the terminal state. The first two numbers
#: are bounded by the worker's own config and add up to 140s; the uploads have no
#: bound of their own, so they get the rest. 300s is a chosen number, not a
#: measured one. The tests compare it with the worker's actual defaults, so
#: raising either of those past it fails CI rather than bringing the race back.
WORKER_FINALISE_BUDGET_SECONDS = 300


def backend_deadline_seconds(task_timeout_seconds: int, *, dispatch_timeout_seconds: int) -> int:
    """The backend's hard deadline for one attempt. See WORKER_FINALISE_BUDGET_SECONDS.

    The ONE place it is computed: both dispatchers call it, and
    kubernetes/render.py imports it for the YAML templates.
    """
    return (
        int(task_timeout_seconds)
        + int(dispatch_timeout_seconds)
        + WORKER_FINALISE_BUDGET_SECONDS
    )


class DispatchError(Exception):
    """Dispatch failed. The caller releases the lease and returns to READY.

    `code` is the only part of this that is safe to hand back to a tenant.
    `str(exc)` carries the upstream API's message, and a Cloud Run or Kubernetes
    error routinely echoes the resource it was given: the tenant service account
    email, the job name, the secret names in the manifest. None of that is key
    material, but it is internal infrastructure detail, and `task.last_error` is
    returned to the caller by `codec.task_to_api`. So the full text goes to the
    log and the code goes to the tenant.
    """

    def __init__(self, message: str, *, code: str = "dispatch_failed") -> None:
        super().__init__(message)
        self.code = code


class Dispatcher(Protocol):
    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant
    ) -> str: ...


def resource_class_for(task: Task, profile: RunnerProfile) -> str:
    """The class the container is sized from -- the same one admission charged.

    A task whose stored class is not in the catalogue any more falls back to the
    profile's, because a missing class would otherwise raise a KeyError deep in
    manifest construction. Admission would have failed first in that case
    (`RESOURCE_CLASSES[task.resource_class]` in the drain loop), so this is a
    guard, not a path.
    """
    if task.resource_class in RESOURCE_CLASSES:
        return task.resource_class
    log.warning(
        "task %s names resource class %r which is no longer in the catalogue; "
        "sizing from the profile's own class %r",
        task.id,
        task.resource_class,
        profile.resource_class,
    )
    return profile.resource_class


def sanitize_name(*parts: str, max_length: int = 63) -> str:
    """A Cloud Run / k8s safe name: lowercase alnum and dashes, <= 63 chars."""
    joined = "-".join(p for p in parts if p)
    slug = _NAME_SAFE.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise DispatchError(
            f"cannot build a resource name from {parts!r}", code="invalid_resource_name"
        )
    if len(slug) > max_length:
        # Truncating alone would collide for two long tenant names sharing a
        # prefix, so the tail carries a hash of the full name.
        import hashlib

        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_length - 9].rstrip("-") + "-" + digest
    if not slug[0].isalpha():
        slug = "s" + slug[: max_length - 1]
    return slug


def job_id_for(tenant_id: str, profile_name: str, resource_class: str | None = None) -> str:
    """The Cloud Run Job resource id.

    Still one Job per (tenant, profile) for every task that takes the profile's
    own resource class, which is the overwhelming majority and what CONTRACT.md
    describes. A workflow step that named a SMALLER class gets its own Job,
    because Cloud Run pins CPU, memory and the ephemeral-disk size limit on the
    Job resource and cannot override them per execution -- so without this the
    smaller class would be charged for at admission and then run in the profile's
    full-size container anyway.

    The "job" segment is NOT decoration: terraform/infra/locals.tf names these
    `${name_prefix}-job-${tenant}-${profile}`, and this must match exactly. It
    did not -- the dispatcher asked for `swarm-u-bogdan-mock` while terraform had
    created `swarm-job-u-bogdan-mock` -- so every dispatch 404'd and the
    dispatcher then created its OWN job under the wrong name. Those jobs carry no
    `managed-by=swarm-terraform` label, which means scripts/destroy.sh would
    refuse to run at all rather than delete an unlabelled resource.
    """
    profile = RUNNER_PROFILES.get(profile_name)
    default_class = profile.resource_class if profile else None
    if resource_class and resource_class != default_class:
        return sanitize_name("swarm", "job", tenant_id, profile_name, resource_class)
    return sanitize_name("swarm", "job", tenant_id, profile_name)


#: The worker's step-spec verification settings (contract request 34). Only
#: the PLATFORM sets them -- terraform on its Cloud Run Jobs and in the GKE
#: ConfigMap, this scheduler on every Job it creates on EITHER backend, from
#: its own settings (`spec_job_env`). `worker_env`, shaped by a task, must
#: never carry one: a tenant-writable document would then choose the keys it is
#: checked against. A test holds both dispatchers. They are public keys, a KMS
#: key name, a mode and a timestamp -- no private material exists to leak.
SPEC_SETTING_NAMES = (
    "SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY", "SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER",
)
#: The ConfigMap terraform renders into each tenant's namespace, and where a
#: GKE worker pod mounts it read-only (`agent_worker.specverify.VERIFY_KEYS_MOUNT`).
#: Since 2026-10-08 it is the FALLBACK on GKE: the pod's environment carries
#: `spec_job_env`, and the worker reads the mount only when that environment
#: has no SPEC_VERIFY_KEYS (a scheduler deployed without them).
SPEC_VERIFY_KEYS_CONFIG_MAP = "swarm-spec-verify-keys"
SPEC_VERIFY_KEYS_MOUNT = "/etc/swarm/spec-verify-keys"


def spec_job_env(settings: Any) -> dict[str, str]:
    """The verification settings for a Job this scheduler creates; absent when unset."""
    values = {
        "SPEC_VERIFY_KEYS": getattr(settings, "spec_verify_keys", ""),
        "SPEC_SIGNING_KEY": getattr(settings, "spec_signing_key", ""),
        "SPEC_SIGNATURE_MODE": getattr(settings, "spec_signature_mode", ""),
        "SPEC_LEGACY_CUTOVER": getattr(settings, "spec_legacy_cutover", ""),
    }
    return {name: str(value) for name, value in values.items() if value}


#: The runners the `swarm-git` MCP shim is named in (docs/runner-git-tools.md
#: §7, lane G6: claude-code's `--mcp-config`, codex's MCP configuration).
GIT_TOOL_RUNNERS = ("agent_worker.runners.claude_code", "agent_worker.runners.codex")


def offers_git_tools(profile_name: str) -> bool:
    """Whether a task on this profile is offered the git tools, so needs an attempt key.

    Read from the frozen catalogue rather than kept as a list of profile names,
    so a new profile on one of these runners is covered without a second edit
    here. A worker action (merge, post-verdict) runs no agent at all; the mock
    and browser runners have no MCP configuration to name the shim in.
    """
    profile = RUNNER_PROFILES.get(profile_name)
    if profile is None or profile.worker_action is not None:
        return False
    return any(part in GIT_TOOL_RUNNERS for part in profile.runner_argv)


def consumes_workspace_secrets(profile_name: str, inputs: Any) -> bool:
    """Whether an attempt on this profile could be released a workspace secret,
    so needs an attempt key to sign the release with (docs/design/workspace-
    secrets.md §3.3, lane SEC-RELEASE).

    The frozen catalogue's declarations (contract requests 80-81): the profile
    declares a secret slot, or a RUNTIME_TOOLS entry its `browser_tool` input
    launches does. The platform components a release can name (`merge`,
    `indexer`) declare no slot, so they add nobody; the judge's TypeSafe key
    is not released into a pod at all (owner decision Q8). Read from the
    catalogue rather than from the grants, so the scheduler reads no
    workspace's secrets to dispatch it.
    """
    profile = RUNNER_PROFILES.get(profile_name)
    if profile is None or profile.worker_action is not None:
        return False
    if profile.secret_slots:
        return True
    launched = tools_launched(profile, inputs if isinstance(inputs, dict) else {})
    return any(RUNTIME_TOOLS[name].secret_slots for name in launched)


def worker_env(*, task: Task, lease: Lease, tenant: Tenant, settings: Any) -> dict[str, str]:
    """The ONLY thing the environment carries is identifiers and endpoints.

    No image, no command, no resource spec. A worker launched with a doctored
    environment still reads what to run from the frozen catalogue keyed by
    RUNNER_PROFILE, which is invariant 10 enforced at the last possible moment.

    QUOTA_BROKER_URL IS HERE BECAUSE THERE IS NOWHERE ELSE IT COULD GO. This
    function is the single source of a worker's execution environment for both
    dispatchers below, so a name it does not carry is a name no worker ever
    sees. The account pool was complete on both sides and entirely inert
    because of that: nothing set it, `WorkerConfig.quota_broker_url` was always
    None, and every worker took the "no broker configured" branch. It is not an
    execution parameter -- it names a platform service, the same way
    ARTIFACT_BUCKET does -- and it still cannot be influenced by a caller,
    because it comes from the scheduler's own settings.

    OMITTED RATHER THAN SET EMPTY when the deployment has no pool. A Cloud Run
    execution override MERGES with the Job's own environment, so an empty value
    here would override a URL that terraform had baked into the Job and turn a
    wired deployment back into an unwired one.

    The GKE pod gets this through `gke_worker_env` below, which adds the
    metadata server's address and nothing else. Anything BOTH backends need
    still belongs here, not there.
    """
    gcs_prefix = tenant_gcs_prefix(task.tenant_id, settings.core.artifact_bucket)
    _record_disagrees(task.tenant_id, "gcs_prefix", tenant.gcs_prefix, gcs_prefix)
    env = {
        "TASK_ID": task.id,
        "ATTEMPT_ID": lease.attempt_id,
        "LEASE_ID": lease.lease_id,
        "GENERATION": str(lease.generation),
        "TENANT_ID": task.tenant_id,
        "RUNNER_PROFILE": task.runner_profile,
        "PROJECT_ID": settings.project_id,
        "REGION": settings.region,
        "FIRESTORE_DATABASE": settings.core.firestore_database,
        "ARTIFACT_BUCKET": settings.core.artifact_bucket,
        # DERIVED, never the stored `tenant.gcs_prefix` (#1457, row 3).
        "GCS_PREFIX": gcs_prefix,
        "CHECKPOINT_INTERVAL_SECONDS": str(
            RUNNER_PROFILES[task.runner_profile].checkpoint_interval_seconds
        ),
        "MAX_IN_WORKER_RETRY_DELAY_SECONDS": str(
            settings.core.max_in_worker_retry_delay_seconds
        ),
        "TASK_TIMEOUT_SECONDS": str(task.timeout_seconds),
        # Inert under the worker lifecycle, which gives its runner child a
        # per-attempt value of its own. See WORKER_ARTIFACTS_DIR for why it is
        # kept and why it was never the fix it was taken for.
        "SWARM_ARTIFACTS_DIR": WORKER_ARTIFACTS_DIR,
    }
    # THE STEP'S MODEL (#1435; the owner's answer to Q2, 2026-10-17). The ID
    # of the step's resolved class, from the scheduler's own class map: on a
    # Cloud Run execution it overrides the Job's default-class MODEL for this
    # execution only, and on GKE it is the pod's. Omitted, not set empty, for
    # a profile with no classes, for the merge reason below.
    model = step_model(settings, RUNNER_PROFILES[task.runner_profile], task)
    if model is not None:
        env["MODEL"] = model
    broker_url = str(getattr(settings, "quota_broker_url", "") or "").strip()
    if broker_url:
        env["QUOTA_BROKER_URL"] = broker_url
        # The audience defaults to the URL inside the worker's client, which is
        # what Cloud Run checks when a service declares no custom audience.
        # This deployment's broker declares one, so the token minted for the
        # URL alone would be rejected by the broker's own `aud` check.
        audience = str(getattr(settings, "quota_broker_audience", "") or "").strip()
        if audience:
            env["QUOTA_BROKER_AUDIENCE"] = audience
    # THE PULL REQUEST CONSOLE LINKS (owner decision, 2026-10-01; off by
    # default). Both are PLATFORM settings, read from the scheduler's own
    # configuration (SWARM_CONSOLE_URL and SWARM_PR_CONSOLE_LINKS on this
    # service) and never from anything a caller sent: invariant 10 holds, the
    # same way it does for the broker URL above. Like that URL, each is
    # OMITTED rather than written empty or "false": a Cloud Run execution
    # override MERGES with the Job's own environment, so a value written here
    # would override one terraform had baked into the Job. The worker's own
    # default for an absent switch is OFF and for an absent origin is "no
    # link", so omission means off unless the Job itself says otherwise.
    # THE FINISH WAKE (#636). The topic the worker publishes `task_finished`
    # on once it has ended its task, so the scheduler releases that task's
    # dependants at once rather than on its next safety tick. The scheduler's
    # own wake topic, from its own settings -- a platform endpoint like the
    # broker URL, never a caller's (invariant 10) -- and omitted when unset,
    # for the same merge reason. No topic means no wake; the tick still
    # releases the dependants.
    wake_topic = str(getattr(settings, "dispatch_topic", "") or "").strip()
    if wake_topic:
        env["DISPATCH_TOPIC"] = wake_topic
    console_url = str(getattr(settings, "console_url", "") or "").strip()
    if console_url:
        env["SWARM_CONSOLE_URL"] = console_url
    if getattr(settings, "pr_console_links", False) is True:
        env["SWARM_PR_CONSOLE_LINKS"] = "true"
    # THE FORGE BROKER'S SWITCH (#1041, `settings.forge_broker`, terraform
    # `forge_broker`). Passed through like SWARM_PR_CONSOLE_LINKS, and omitted
    # when off, so an unswitched worker reads a person's slot with its own
    # account exactly as before the broker (`agent_worker.forgebroker`).
    forge_broker = getattr(settings, "forge_broker", False) is True
    if forge_broker:
        env["FORGE_BROKER"] = "true"
    # THE ATTEMPT KEY (docs/design/child-tasks.md §3.2). A one-use registration
    # nonce bound to this exact attempt, which its worker spends registering
    # the attempt key it generates BEFORE its agent exists; the agent can read
    # it later from /proc/1/environ, by which time it is worthless. Only when
    # this deployment has the key -- omitted otherwise, so the worker registers
    # nothing. The nonce and the API's address travel together: a nonce the
    # worker had nowhere to spend would sit unspent in an environment its
    # agent reads.
    #
    # A CHILD GETS ONE ONLY WHEN ITS PROFILE OFFERS GIT TOOLS
    # (docs/runner-git-tools.md §1.4, lane G2). The key was the child path's
    # alone, and a child may not have children, so a child got none. The git
    # tools are signed with the same key, and a fan-out lane runs as a child,
    # so it needs one too. Having a key does not lift the depth limit:
    # swarm-api refuses a child's `submit` as `child_depth_exceeded` from the
    # task document, whatever its worker registered.
    #
    # AND WHEN IT COULD BE RELEASED A WORKSPACE SECRET (docs/design/workspace-
    # secrets.md §3.3, lane SEC-RELEASE). The release is signed with the same
    # key, so a child whose profile or tools declare a secret slot needs one
    # even with no git tools (a `browser` child): without it the worker cannot
    # ask, a required slot parks it, and an optional one is never delivered.
    #
    # AND WHEN ITS SIGNED INPUT NAMES AN MCP SERVER (docs/design/registered-
    # mcp-servers.md §4.6, lane MCP-SCHED). The attach that hands the worker
    # each server's URL and credential is signed with the same key, so a child
    # naming a server needs one whatever else its profile offers: without it
    # the worker cannot attach, and the attempt fails before its agent runs.
    # A task naming none is keyed exactly as before.
    # THE FORGE BROKER (#1041, docs/design/user-scoped-secrets.md) reuses the
    # same registered attempt key: swarm-api releases a person's `git-u-` slot
    # only to an attempt that signs with it. So WHILE THE BROKER IS SWITCHED ON
    # (`forge_broker` above), an attempt whose signed `forge_credential` is a
    # user slot gets the nonce too, child or not -- without it a child running
    # on its submitter's slot could never prove itself and the broker would
    # refuse it its own credential.
    #
    # THE SWITCH, NOT THE KEY, DECIDES. Dev has child tasks ON
    # (terraform/environments/dev/dev.tfvars `enable_child_tasks`), so its
    # scheduler holds the key and hands SWARM_API_URL to every root task. The
    # broker is nonetheless off there until the owner sets `forge_broker`: off,
    # no child is keyed for its slot (dispatch is what it was before #1041) and
    # every worker reads the slot with its own account while
    # `forge_slot_reader` allows it (`agent_worker.forgebroker.ForgeBroker.deployed`).
    # That binding is removed only after the switch has been on in dev and a
    # live run passed (docs/design/user-scoped-secrets.md §5, the rollout order).
    #
    # A CHILD KEYED ONLY FOR THE BROKER GETS NO CHILD PATH. SWARM_CHILD_PATH=false
    # tells its worker to register the key and offer its agent nothing to
    # submit children with. It is written only when the broker is the SOLE
    # reason the child has a key: a child keyed for git tools, a workspace
    # secret or an MCP server needs the key's child path (`ChildPath.available`)
    # for those, and the flag would switch them off.
    # That flag is a courtesy, not the guard, and neither is swarm-api's
    # stored-`parent_task_id` check: an agent can rewrite its own task
    # document, `parent_task_id` included. Depth is enforced by the CHILD'S
    # WORKER from its verified spec -- `parent_task_id` is inside the signature
    # (format 2), and `agent_worker.children` offers no child path to an
    # attempt whose verified spec `is_child`, nor submits from one -- and
    # since #1041 swarm-api builds a child only from a parent whose signature
    # verifies (`children.ChildParentUnsigned`), so a child that erased its
    # own `parent_task_id` fails its signature instead of passing as a root.
    # A root task is written nothing: the child path is the worker's default.
    child_key = str(getattr(settings, "child_key", "") or "")
    api_url = str(getattr(settings, "swarm_api_url", "") or "").strip()
    is_child = bool(task.parent_task_id)
    other_key_use = (
        offers_git_tools(task.runner_profile)
        or consumes_workspace_secrets(task.runner_profile, task.input)
        or names_mcp_servers(task.runner_profile, task.input)
    )
    # THE STATE SWITCH (docs/design/tenant-settings-integrity.md §4.6; ISO-SCHED
    # phase 2, part of #1457). A switched worker opens its attempt at
    # swarm-api's `state/open` by spending this nonce, before any other state
    # call, so EVERY attempt of a switched tenant is keyed -- a child included,
    # which on its own would get none. The nonce is still this attempt's own
    # tuple and generation, so it opens nothing else; and a child keyed only
    # for this still gets SWARM_CHILD_PATH=false below, because the state
    # routes are not `other_key_use`. Membership is the tenant id exactly.
    state_via_api = state_via_api_on(settings, tenant_id=task.tenant_id)
    keyed = (
        state_via_api
        or not is_child
        or (forge_broker and needs_user_slot(task))
        or other_key_use
    )
    if child_key and api_url and keyed:
        from .children import registration_nonce

        env["SWARM_CHILD_NONCE"] = registration_nonce(
            child_key,
            tenant_id=task.tenant_id,
            task_id=task.id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            generation=lease.generation,
        )
        env["SWARM_API_URL"] = api_url
        api_audience = str(getattr(settings, "swarm_api_audience", "") or "").strip()
        if api_audience:
            env["SWARM_API_AUDIENCE"] = api_audience
        if is_child and not other_key_use:
            env["SWARM_CHILD_PATH"] = "false"
    # The switch itself, OUTSIDE the key's condition: a deployment missing the
    # key or the address still tells a switched worker it is switched, and the
    # worker refuses to start without a nonce (agent_worker.config, exit 78)
    # rather than quietly taking the direct Firestore path. An unswitched
    # tenant is written nothing -- not even "false" -- because a Cloud Run
    # execution override merges with the Job's own environment, and so that
    # its attempts render exactly what they rendered before the switch.
    if state_via_api:
        env["SWARM_STATE_VIA_API"] = "true"
        if api_url:
            env["SWARM_API_URL"] = api_url
            api_audience = str(getattr(settings, "swarm_api_audience", "") or "").strip()
            if api_audience:
                env["SWARM_API_AUDIENCE"] = api_audience
    return env


def state_via_api_on(settings: Any, *, tenant_id: str) -> bool:
    """Whether `tenant_id`'s attempts reach their state through swarm-api's routes.

    The scheduler's `state_via_api_tenants` (SWARM_STATE_VIA_API_TENANTS), by
    exact tenant id: never a prefix, a pattern or anything a task carries
    (invariant 10). The tenant id is the task's, the one its lease, its nonce
    and its worker's identity are bound to.
    """
    switched = getattr(settings, "state_via_api_tenants", None) or ()
    return isinstance(tenant_id, str) and tenant_id in frozenset(switched)


#: The GKE metadata server's link-local address. On a Workload Identity node
#: pool gke-metadata-server answers here, on plain HTTP port 80, for every pod
#: on the node -- the address, not a name, so reaching it needs no DNS.
GKE_METADATA_SERVER_IP = "169.254.169.254"


def gke_worker_env(*, task: Task, lease: Lease, tenant: Tenant, settings: Any) -> dict[str, str]:
    """`worker_env`, plus what only a GKE worker pod needs: the metadata server BY ADDRESS.

    WHY. The silent GKE worker of 2026-09-24 (task_1f699ef4cdbb4cc79c16,
    attempt att_78e34e7537de487ba71d). The tenant egress NetworkPolicy allowed
    DNS only to the `k8s-app=kube-dns` pods, but swarm-autopilot answers pod
    DNS from the host-network NodeLocal DNSCache, which no rule allowed, so
    every lookup by NAME was dropped -- silently, because anetd runs with
    `--policy-deny-response=none`. google-auth's first request, the ping,
    goes to the metadata server by IP, and the gke-metadata-server log shows
    it arriving (`"/" HTTP/200`). The project-id request that follows goes to
    `metadata.google.internal` by name; nothing after the ping ever arrived,
    and no token was ever minted for the tenant's GSA.

    WHAT THE TWO VARIABLES DO. google-auth reads both once, when
    `google.auth.compute_engine._metadata` is imported -- in this container
    that is before any worker code runs, with this environment already set:

      * GCE_METADATA_HOST is the host of every metadata `get()`: the project
        id, the service-account email, and every access and identity token
        google-auth mints. Unset, it is `metadata.google.internal`.
      * GCE_METADATA_IP is the host of the `ping()` that decides whether the
        process is on Google Cloud at all. Its default is already this address;
        it is set anyway so the one value is stated for both, not half by
        google-auth's default.

    169.254.169.254 is in google-auth's own list of default metadata hosts, so
    its strict-mTLS check does not treat it as an override it must refuse, and
    the tenant egress policy (kubernetes/network-policies/allow-egress.yaml,
    rule 2) opens it on TCP 80, the port these requests use.

    THIS TAKES DNS OUT OF GOOGLE-AUTH'S METADATA AND TOKEN PATH, AND NOTHING
    ELSE. It is not the fix for that incident. Everything else the worker
    reaches, it still reaches BY NAME, and every one of those still needs the
    NetworkPolicy DNS rule that reaches NodeLocal DNSCache (lane gke-netpol-dns,
    F1 of the incident report):
      * firestore.googleapis.com, Secret Manager, Cloud Storage, Monitoring;
      * the quota broker, and the identity token the worker fetches for it --
        `agent_worker/accountlease.py` builds that URL from
        `metadata.google.internal` itself and does not read either variable;
      * the provider APIs (api.anthropic.com and the rest) and every site a
        browser task opens.
    With DNS still dropped, a worker with this override gets its project id
    and its token and then hangs on Firestore instead: later in the same
    silence, not out of it.

    WHO SEES THEM. The worker lifecycle process -- the one that authenticates.
    Its runner child does not: `lifecycle._build_child_env` builds the child's
    environment from an allowlist, and no runner authenticates to Google.
    Nothing in `apps/agent-worker` reads these names; google-auth does, so they
    are not dead configuration if scripts/lib/check-env-parity.sh is ever
    extended to the worker's environment (today it excludes it on purpose).

    NOT CLOUD RUN, which is why this is a separate function and not two more
    entries in `worker_env`. Cloud Run has no NetworkPolicy, its path by name
    is measured working (execution swarm-job-u-sw-c90291-mock-6smzz ran the
    full startup sequence), and its metadata server is not gke-metadata-server
    -- nothing here measured that it answers this address the same way. A
    Cloud Run execution override also MERGES into the Job's own environment,
    so a value in `worker_env` would change every Cloud Run worker to fix a
    GKE-only fault. tests/unit/control_plane/test_gke_worker_metadata_env.py
    fails if either variable reaches the Cloud Run execution override, the
    Cloud Run Job the scheduler creates or `worker_env`, or is named in a
    file terraform's Cloud Run Jobs take their environment from (terraform/
    infra, modules/cloud_run_jobs, every environment's tfvars -- its
    `settings_env` is merged into each of those Jobs). That last check reads
    text, not a plan: a `-var settings_env=...` override is not seen by it.

    The YAML copy of this Job (kubernetes/worker-templates/*.yaml) carries the
    same two entries, held to these values by
    tests/unit/worker/test_worker_templates_name_the_metadata_server.py.
    """
    env = worker_env(task=task, lease=lease, tenant=tenant, settings=settings)
    env["GCE_METADATA_HOST"] = GKE_METADATA_SERVER_IP
    env["GCE_METADATA_IP"] = GKE_METADATA_SERVER_IP
    # The pod's cache volume (CACHE_MOUNT, CACHE-SCRATCH). GKE only, for the
    # reason the two above are: a Cloud Run worker has no second disk, and a
    # value here in `worker_env` would reach every Cloud Run execution.
    env[CACHE_ROOT_ENV] = CACHE_MOUNT
    return env


def image_uri(settings: Any, profile: RunnerProfile) -> str:
    """The image a worker for `profile` runs: a DIGEST whenever the deployment pins one.

    `worker_image_refs` (WORKER_IMAGE_REFS, written by terraform from the
    promotion manifest) is authoritative when it is non-empty. A profile whose
    image is missing from it is refused, not given a tag: a tag is resolved
    when the image is pulled, and the GKE template pulls IfNotPresent, so a
    fallback here would run whatever a node last cached under that tag -- the
    exact path the digest map exists to close, taken silently.

    An empty map is the local emulator loop, which has no promotion manifest;
    it keeps the tag it always had. A deployed scheduler is never in that
    state: terraform always sets the map, and check-env-parity requires it.
    """
    refs = getattr(settings, "worker_image_refs", None) or {}
    if refs:
        ref = refs.get(profile.image)
        if not ref:
            raise DispatchError(
                f"no digest-pinned image for {profile.image!r} (runner profile "
                f"{profile.name!r}) in WORKER_IMAGE_REFS, which pins "
                f"{sorted(refs)}; refusing to run it at a tag. The promotion "
                "manifest the deployment was applied from did not include this "
                "image -- rebuild and redeploy every image",
                code="worker_image_not_pinned",
            )
        return ref
    return f"{settings.artifact_registry_host}/{profile.image}:{settings.worker_image_tag}"


#: GCP's cap on a service account id. A tenant id whose
#: `worker_service_account_id` is longer has no worker account it could run as
#: (swarm-api's `store.derived_service_account` records None for it).
SERVICE_ACCOUNT_ID_MAX = 30

#: The event logged when a `tenants/<id>` record disagrees with what dispatch
#: derives. The namespace keeps its own, `TENANT_NAMESPACE_MISMATCH`, which the
#: monitoring module counts.
TENANT_RECORD_MISMATCH = "tenant_record_mismatch"


def tenant_service_account(tenant_id: str, project_id: str) -> str | None:
    """`swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com`, the one
    account a tenant's attempt runs as, or None when no such account can exist.

    DERIVED, NEVER READ from `tenants/<id>.service_account` (design
    tenant-settings-integrity.md §1.3 row 1, #1457): a tenant's worker can
    rewrite its own tenant document, and this service holds `actAs` on every
    tenant's worker account, so a Job built from the stored value ran tenant A's
    next attempt as tenant B. Terraform's tenancy module, register-tenant.sh
    and swarm-api all name the account by `identity.worker_service_account_id`.
    """
    account_id = worker_service_account_id(tenant_id)
    if not tenant_id or not project_id or len(account_id) > SERVICE_ACCOUNT_ID_MAX:
        return None
    return f"{account_id}@{project_id}.iam.gserviceaccount.com"


def tenant_gcs_prefix(tenant_id: str, artifact_bucket: str) -> str:
    """`gs://<bucket>/tenants/<tenant>`, as swarm-api records it when it creates
    the tenant; derived for the same reason as `tenant_service_account` (row 3).
    "" when the deployment names no bucket, as an unset record read before."""
    return f"gs://{artifact_bucket}/tenants/{tenant_id}" if artifact_bucket else ""


def _record_disagrees(tenant_id: str, name: str, stored: Any, derived: str) -> None:
    """Log, once per tenant and field per process per hour, a stored record
    that is not the derived value. The record is IGNORED either way: a
    disagreement is a stale document or a forged one, and both are repaired in
    Firestore, never followed."""
    stored = str(stored).strip() if stored else ""
    if not stored or stored.lower() == derived.lower():
        return
    if _record_mismatch_due(f"{name}\n{tenant_id}"):
        log.error(
            "%s tenant=%s field=%s stored=%r derived=%r: IGNORED, dispatch uses the "
            "derived value. A tenant's worker can write its own tenant document; "
            "repair it and find out who wrote it",
            TENANT_RECORD_MISMATCH, tenant_id, name, stored, derived,
            extra={"event": TENANT_RECORD_MISMATCH, "tenant_id": tenant_id,
                   "field": name, "stored": stored, "derived": derived},
        )


def assert_tenant_identity(tenant: Tenant, settings: Any) -> str:
    """The service account this tenant's attempt runs as: DERIVED from its id.

    `tenant.service_account` is a record, not authority (contract request 87):
    a stored value that is not the derived one is logged and ignored
    (`tenant_service_account`, which says why). What is refused is a tenant
    that can have no account at all -- an id too long for one, or a scheduler
    with no project -- because Cloud Run falls back to the project's DEFAULT
    compute service account when a Job names none, and that identity is shared
    and usually far more privileged than a worker. Invariant 9 requires the
    tenant's own identity, and a task that cannot have one belongs back in
    READY with a clear error, not in a container.
    """
    project_id = str(getattr(settings, "project_id", "") or "")
    derived = tenant_service_account(tenant.tenant_id, project_id)
    if derived is None:
        raise DispatchError(
            f"tenant {tenant.tenant_id!r} has no service account it could run as: "
            f"{worker_service_account_id(tenant.tenant_id)!r} is not a valid account "
            f"id in project {project_id!r} (over {SERVICE_ACCOUNT_ID_MAX} characters, "
            "or no project), and the platform will not run its work under a shared one",
            code="tenant_identity_missing",
        )
    _record_disagrees(tenant.tenant_id, "service_account", tenant.service_account, derived)
    return derived


# --------------------------------------------------------------------------
# Cloud Run Jobs
# --------------------------------------------------------------------------

def _plain_env_value(container: Any, name: str) -> str | None:
    """The literal value of env var `name` on a Cloud Run container, or None.

    A variable read from a secret has no literal value and counts as absent;
    so does an empty string, which is what an unset proto field reads as.
    """
    for env in getattr(container, "env", None) or ():
        if getattr(env, "name", None) == name:
            value = getattr(env, "value", "") or ""
            return value or None
    return None


def _workspace_drift(existing: Any, rc: Any) -> list[str]:
    """Which of launch stage, workspace medium and size `existing` has wrong.

    Compared with what `_build_job` writes for resource class `rc`, without
    building one: building a Job also asks the account pool who serves the
    tenant (`credential_for`), which deciding whether to rebuild must not.
    Read defensively: `existing` is whatever `get_job` returned, and a Job with
    no template or no workspace volume is drift, not a crash.
    """
    from google.api import launch_stage_pb2
    from google.cloud import run_v2

    drift: list[str] = []
    # UNSPECIFIED counts as GA: it is what a Job created without the field
    # reads back as, and treating it as drift would rewrite every such Job
    # once per scheduler process. Only a pre-GA stage (BETA, ALPHA, ...) is
    # the Preview volume's mark.
    if getattr(existing, "launch_stage", None) not in (
        launch_stage_pb2.LaunchStage.GA,
        launch_stage_pb2.LaunchStage.LAUNCH_STAGE_UNSPECIFIED,
    ):
        drift.append("launch_stage")
    try:
        have = next(v for v in existing.template.template.volumes if v.name == "workspace")
    except (AttributeError, StopIteration):
        return [*drift, "workspace volume"]
    if have.empty_dir.medium != run_v2.EmptyDirVolumeSource.Medium.MEMORY:
        drift.append("workspace medium")
    if have.empty_dir.size_limit != f"{workspace_size_gib(rc)}Gi":
        drift.append("workspace size_limit")
    return drift


def _tenant_job_vpc_access(settings: Any) -> Any:
    """Terraform's worker-Job `vpc_access` (modules/cloud_run_jobs/main.tf),
    from the scheduler's settings, or None while `tenant_job_vpc_egress` is off.

    ALL_TRAFFIC for terraform's reason: a provider call must leave through the
    swarm Cloud NAT to carry the addresses a provider allow-lists, and a call to
    swarm-api must arrive from inside the VPC or its internal ingress answers
    404 (#1893). The tag is what the worker-ingress deny rule names.
    """
    from google.cloud import run_v2

    # getattr: a test's stand-in settings object predates the switch.
    if not getattr(settings, "tenant_job_vpc_egress", False):
        return None
    return run_v2.VpcAccess(
        egress=run_v2.VpcAccess.VpcEgress.ALL_TRAFFIC,
        network_interfaces=[
            run_v2.VpcAccess.NetworkInterface(
                network=settings.tenant_job_network,
                subnetwork=settings.tenant_job_subnetwork,
                tags=list(settings.tenant_job_network_tags),
            )
        ],
    )


def _vpc_drift(existing: Any, settings: Any) -> list[str]:
    """Whether `existing` lacks the `vpc_access` `_build_job` writes (#1893).

    Nothing while the switch is off: off, a Job is left as it is. Network and
    subnetwork compare by their last path segment, because Cloud Run may read
    either back by name rather than by full resource name, and treating that
    as drift would rewrite every Job once per scheduler process; the rewrite
    itself always writes the full names from settings.
    """
    from google.cloud import run_v2

    wanted = _tenant_job_vpc_access(settings)
    if wanted is None:
        return []
    try:
        have = existing.template.template.vpc_access
        nics = list(have.network_interfaces)
    except AttributeError:
        return ["vpc_access"]
    if have.egress != run_v2.VpcAccess.VpcEgress.ALL_TRAFFIC or len(nics) != 1:
        return ["vpc_access"]
    want = wanted.network_interfaces[0]

    def tail(value: str) -> str:
        return str(value or "").rstrip("/").rsplit("/", 1)[-1]

    if (tail(nics[0].network) != tail(want.network)
            or tail(nics[0].subnetwork) != tail(want.subnetwork)
            or sorted(nics[0].tags) != sorted(want.tags)):
        return ["vpc_access"]
    return []


class CloudRunJobDispatcher:
    def __init__(
        self, settings: Any, *, client: Any | None = None, pool: AccountPool | None = None,
        quality_gates: Callable[[str], dict[str, str]] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        #: tenant id -> the quality gates' verdict env
        #: (`judgment.read_quality_gates_env`); None reads as off.
        self._quality_gates = quality_gates
        self._ensured: set[str] = set()
        # The Scheduler's own AccountPool, handed over by main.build_scheduler,
        # so the Job's secret mount is decided on the list admission read.
        # Built on its own, the dispatcher gets one that raises if it is ever
        # asked who the pool serves -- see AccountPool.unwired.
        self._pool = pool if pool is not None else AccountPool.unwired(settings)

    def _jobs(self) -> Any:
        if self._client is None:
            from google.cloud import run_v2

            self._client = run_v2.JobsClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._settings.project_id}/locations/{self._settings.region}"

    def job_name(self, job_id: str) -> str:
        return f"{self.parent}/jobs/{job_id}"

    def _model_for(self, profile: RunnerProfile) -> str | None:
        """The Job's own MODEL: the profile's default-class pin, or None.

        A step's own class reaches each execution through `worker_env`, whose
        MODEL overrides this one for that execution only (#1435, §4.5)."""
        return profile_model(self._settings, profile)

    def _build_job(
        self, profile: RunnerProfile, tenant: Tenant, resource_class: str | None = None
    ) -> Any:
        from google.api import launch_stage_pb2
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        service_account = assert_tenant_identity(tenant, self._settings)
        rc = RESOURCE_CLASSES[resource_class or profile.resource_class]
        env = [
            run_v2.EnvVar(name="RUNNER_PROFILE", value=profile.name),
            run_v2.EnvVar(name="TENANT_ID", value=tenant.tenant_id),
        ]
        # THE PROFILE'S MODEL, ON THE JOB (#226). A Job Terraform creates carries
        # `MODEL` from `local.runner_models`; this Job -- for a tenant Terraform
        # does not list, or a resource class other than the profile's own --
        # gets the same value through WORKER_MODELS, or its agents would run the
        # CLI's default model while Terraform's ran the pinned one. On the Job
        # and not in `worker_env`, which a task shapes: a caller never chooses it.
        model = self._model_for(profile)
        if model:
            env.append(run_v2.EnvVar(name="MODEL", value=model))
        # THE STEP-SPEC VERIFICATION SETTINGS, ON THE JOB (contract request 34),
        # for the same reason as MODEL: a Job terraform creates carries them
        # from `local.spec_worker_env`; this one gets the scheduler's copy of
        # the same values. On the Job, never in `worker_env`.
        for name, value in spec_job_env(self._settings).items():
            env.append(run_v2.EnvVar(name=name, value=value))
        # THE SAME QUESTION ADMISSION ASKED, of the same pool list
        # (credentials.py, #169). This used to be a private rule of its own,
        # `_pool_can_serve`: "the deployment has a broker and the tenant has no
        # key". Admission never let such a tenant through, so it was never
        # exercised -- and it was also wider than the pool is. The pool serves
        # a tenant only through an account owned by or lent to it, and only a
        # profile that takes a subscription token.
        credential = credential_for(profile, tenant, self._pool)
        for secret_env in profile.secrets:
            if not profile.provider:
                continue
            if credential.source is CredentialSource.ACCOUNT_POOL:
                # A POOL ACCOUNT IS THIS TENANT'S CREDENTIAL, so there is no
                # per-tenant secret to project and naming one would be naming a
                # secret that does not exist. Cloud Run resolves a
                # `secretKeyRef` when the JOB is created, so that fails the
                # create outright and the tenant cannot be dispatched at all --
                # not "runs without a key", but "never starts".
                #
                # Only for ACCOUNT_POOL. A tenant with a key keeps its own
                # secret mounted whether or not an account also serves it: the
                # pool is an addition to its options, not a replacement. And a
                # tenant with neither (MISSING) never gets here, because
                # admission parks it first; if it did, the mount is kept and
                # the Job create fails loudly, as it always did.
                #
                # The worker resolves its own credential either way, from the
                # pool or from Secret Manager, so this mount has always been a
                # convenience rather than the thing the agent runs on.
                continue
            # The tenant's OWN secret, by the one spelling the frozen contract
            # defines. A shared secret here would break invariant 9.
            env.append(
                run_v2.EnvVar(
                    name=secret_env,
                    value_source=run_v2.EnvVarSource(
                        secret_key_ref=run_v2.SecretKeySelector(
                            secret=tenant.secret_name(profile.provider),
                            version="latest",
                        )
                    ),
                )
            )

        container = run_v2.Container(
            name="worker",
            image=image_uri(self._settings, profile),
            # NO `command` AND NO `args`: the image ENTRYPOINT is the worker
            # lifecycle, and `profile.runner_argv` is its child's argv, not the
            # container's (module docstring). Terraform's Jobs already leave
            # both unset; this Job -- created whenever `get_job` 404s, for a
            # new tenant, profile or resource class -- used to set
            # `command=list(profile.command)` (the field's old name) and would
            # have run the bare runner exactly as the GKE pods of 2026-09-24 did.
            env=env,
            # Cloud Run expresses sizing as limits and bills them, so the
            # limit is the class's CPU REQUEST, never `cpu_limit_cores`: a
            # Job has no request to burst past, and rendering the burst limit
            # here would double the bill and allow no burst (invariant 7 as
            # amended by RS-1; docs/design/runner-sizes.md §2.5).
            resources=run_v2.ResourceRequirements(
                limits={"cpu": str(int(rc.cpu)), "memory": f"{rc.memory_gib}Gi"}
            ),
            volume_mounts=[run_v2.VolumeMount(name="workspace", mount_path=WORKSPACE_MOUNT)],
        )

        volume = run_v2.Volume(
            name="workspace",
            # MEMORY, capped below the memory limit: exactly terraform's
            # workspace volume. See WORKSPACE_MEMORY_FRACTION for why not the
            # Preview disk-backed volume (MEDIUM_UNSPECIFIED) this used to ask for.
            empty_dir=run_v2.EmptyDirVolumeSource(
                medium=run_v2.EmptyDirVolumeSource.Medium.MEMORY,
                size_limit=f"{workspace_size_gib(rc)}Gi",
            ),
        )

        vpc_access = _tenant_job_vpc_access(self._settings)
        vpc = {} if vpc_access is None else {"vpc_access": vpc_access}

        return run_v2.Job(
            # GA, as terraform's Jobs are: nothing on this Job is a pre-GA
            # feature now that the workspace is a tmpfs.
            launch_stage=launch_stage_pb2.LaunchStage.GA,
            labels={
                "managed-by": "swarm-scheduler",
                "swarm-tenant": sanitize_name(tenant.tenant_id),
                "swarm-profile": sanitize_name(profile.name),
                "swarm-resource-class": sanitize_name(rc.name),
            },
            template=run_v2.ExecutionTemplate(
                parallelism=1,
                task_count=1,
                template=run_v2.TaskTemplate(
                    containers=[container],
                    volumes=[volume],
                    # The platform owns retries: a retry is a new attempt with a
                    # new fencing generation. Cloud Run retrying underneath us
                    # would run the agent twice on one generation.
                    max_retries=0,
                    # What an execution started BY HAND (no override) gets. The
                    # scheduler's own executions override it per task below.
                    # Later than the lifecycle's deadline; see
                    # WORKER_FINALISE_BUDGET_SECONDS.
                    timeout=duration_pb2.Duration(
                        seconds=backend_deadline_seconds(
                            profile.timeout_seconds,
                            dispatch_timeout_seconds=self._settings.core.dispatch_timeout_seconds,
                        )
                    ),
                    service_account=service_account,
                    execution_environment=run_v2.ExecutionEnvironment.EXECUTION_ENVIRONMENT_GEN2,
                    # TERRAFORM'S DIRECT VPC EGRESS (#1893), only while
                    # `tenant_job_vpc_egress` is on; off, the field is not set
                    # and the Job is the one this built before the switch.
                    **vpc,
                ),
            ),
        )

    def _refresh_image(
        self,
        client: Any,
        name: str,
        existing: Any,
        profile: RunnerProfile,
        tenant: Tenant,
        resource_class: str | None,
    ) -> bool:
        """Bring a job THIS dispatcher created up to the image, and the MODEL,
        it would create today -- and, while `tenant_job_vpc_egress` is on, its
        Direct VPC egress (#1893).

        The MODEL half is #226: a job created before WORKER_MODELS existed has
        none, and its agents would run the CLI's default model until the next
        image change happened to rebuild it.

        A Cloud Run Job pins its image on the job resource, and this method's
        caller used to stop at "it exists". So a job the dispatcher created for a
        tenant terraform does not know -- every self-service `u-<email>` tenant --
        kept the image of the day it was created, at a tag from before the
        digest map existed, for every execution afterwards. Pinning NEW jobs by
        digest does nothing for those; this does.

        ONLY jobs labelled managed-by=swarm-scheduler. Terraform's jobs are
        terraform's: every apply pins them by digest, and the dispatcher
        rewriting one is precisely the repoint modules/cloud_run_jobs keeps the
        image un-ignored in order to catch as drift.

        Checked once per job per process (the caller caches the result in
        `_ensured`), and a new scheduler revision starts with an empty cache --
        which is exactly when the digest map changes.

        Returns True when it rewrote the job. The rewrite is a whole Job from
        `_build_job`, so it carries no container `command` or `args` either,
        and the caller need not look for an ENTRYPOINT override on it
        (`_clear_entrypoint_override`). A failed rewrite refuses the dispatch,
        so a job that also overrides the ENTRYPOINT is never run on this path.
        """
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2

        labels = dict(getattr(existing, "labels", None) or {})
        if labels.get("managed-by") != "swarm-scheduler":
            return False
        wanted = image_uri(self._settings, profile)
        try:
            container = existing.template.template.containers[0]
            current = container.image
        except (AttributeError, IndexError):
            container = None
            current = ""
        # AND ITS MODEL (#226). A Job created before WORKER_MODELS carries no
        # MODEL, and the image check alone would leave it that way until the
        # next image change. Same rebuild, same single write.
        wanted_model = self._model_for(profile)
        current_model = _plain_env_value(container, "MODEL")
        # AND ITS STEP-SPEC SETTINGS (contract request 34; #353 security
        # review, M1). `_build_job` bakes SPEC_* from this scheduler's own
        # settings, so a tfvars-only change -- the cutover, the move to
        # enforce, a rotated or revoked key -- must rebuild the Jobs this
        # dispatcher created (every self-service `u-*` tenant, every
        # non-default class), or they keep verifying against the old values.
        wanted_spec = spec_job_env(self._settings)
        spec_drift = sorted(
            name for name in SPEC_SETTING_NAMES
            if _plain_env_value(container, name) != wanted_spec.get(name)
        )
        # AND ITS WORKSPACE (gap audit D7). A Job this dispatcher created
        # before 2026-10-01 carries the Preview disk-backed volume on BETA;
        # `_build_job` no longer writes either, but only a rebuild moves a Job
        # that already exists, and without this one waited for the next image
        # change to happen to rebuild it.
        storage_drift = _workspace_drift(
            existing, RESOURCE_CLASSES[resource_class or profile.resource_class]
        )
        # AND ITS IDENTITY (#1457). Until the account was derived, `_build_job`
        # named whatever `tenants/<id>.service_account` said, and a tenant's
        # worker can write that. A Job built while the record named another
        # tenant's account would go on running this tenant's attempts as that
        # account for as long as it exists, so it is rebuilt as the derived one.
        wanted_account = assert_tenant_identity(tenant, self._settings)
        try:
            current_account = str(existing.template.template.service_account or "")
        except AttributeError:
            current_account = ""
        if current_account.lower() != wanted_account.lower():
            storage_drift = [*storage_drift, "service_account"]
        # AND ITS NETWORK (#1893). A Job made before `tenant_job_vpc_egress`
        # was on has no `vpc_access`, and its switched worker's state calls are
        # refused at swarm-api's ingress; rebuilt, not deleted, so
        # `swarm-job-u-bogdan-*` keep their names. Nothing while the switch is off.
        storage_drift = [*storage_drift, *_vpc_drift(existing, self._settings)]
        if (current == wanted and current_model == wanted_model and not spec_drift
                and not storage_drift):
            return False
        job = self._build_job(profile, tenant, resource_class)
        job.name = name
        try:
            operation = client.update_job(request=run_v2.UpdateJobRequest(job=job))
            operation.result(timeout=120)
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not move Cloud Run job {name} from {current or 'an unreadable image'} "
                f"to {wanted}: {exc}",
                code="cloud_run_update_job_failed",
            ) from exc
        # Names only: the values are public keys and a mode, but the log line
        # needs no more than which settings moved.
        log.info(
            "cloud run job %s moved from %s (MODEL %s) to %s (MODEL %s); SPEC_* changed: %s; "
            "workspace, identity or network changed: %s",
            name, current or "?", current_model or "unset", wanted, wanted_model or "unset",
            ", ".join(spec_drift) or "none", ", ".join(storage_drift) or "none",
        )
        return True

    def ensure_job(
        self, profile: RunnerProfile, tenant: Tenant, resource_class: str | None = None
    ) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2

        job_id = job_id_for(tenant.tenant_id, profile.name, resource_class)
        name = self.job_name(job_id)
        if job_id in self._ensured:
            return name

        client = self._jobs()
        try:
            existing = client.get_job(request=run_v2.GetJobRequest(name=name))
        except gexc.NotFound:
            existing = None
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not read Cloud Run job {job_id}: {exc}",
                code="cloud_run_get_job_failed",
            ) from exc
        if existing is not None:
            # Two corrections, and at most ONE write. A job this dispatcher
            # created and left at an old image is rebuilt whole from
            # `_build_job`, which sets no container `command` or `args`, so the
            # rebuild also removes an ENTRYPOINT override -- and a job created
            # before 2026-09-24 typically carries both. Only a job that was NOT
            # rebuilt (terraform's own, or one already at the wanted image) is
            # then READ for an override and cleared in place. Running both on
            # one job would write it twice, and the second write would send
            # `existing` -- the pre-rebuild Job, with its now-stale etag and its
            # old image -- so it would either be refused as a conflict or put
            # the old image back.
            if not self._refresh_image(client, name, existing, profile, tenant, resource_class):
                # READ, not merely found. See _clear_entrypoint_override.
                self._clear_entrypoint_override(client, existing, job_id)
            self._ensured.add(job_id)
            return name

        try:
            operation = client.create_job(
                request=run_v2.CreateJobRequest(
                    parent=self.parent,
                    job_id=job_id,
                    job=self._build_job(profile, tenant, resource_class),
                )
            )
            operation.result(timeout=120)
        except gexc.AlreadyExists:
            # Another scheduler instance created it between our get and create.
            pass
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not create Cloud Run job {job_id}: {exc}",
                code="cloud_run_create_job_failed",
            ) from exc
        self._ensured.add(job_id)
        return name

    def _clear_entrypoint_override(self, client: Any, job: Any, job_id: str) -> None:
        """Remove a container `command`/`args` from a Job that already exists.

        Taking `command=` out of `_build_job` fixed only the Jobs created from
        then on. Every Job this dispatcher created BEFORE 2026-09-24 -- any
        non-default resource class (`swarm-job-<t>-<p>-<class>`), any provider
        outside a tenant's terraform `providers` -- still carries
        `command=python -m agent_worker.runners.<x>`, which replaces the image
        ENTRYPOINT and runs the bare runner: no fencing (invariant 5), no
        checkpoint (invariant 8), no heartbeat, no lease release (module
        docstring). `ensure_job` used to return the moment `get_job` succeeded,
        and a per-execution `ContainerOverride` has `args`, `env` and
        `clear_args` but NO `command` field, so `run_job` could never clear it.
        So the Job is corrected in place, once per process, before its first
        execution here (review of PR #31).

        CLEARED IN PLACE rather than rebuilt from `_build_job`: only the two
        fields that replace the lifecycle change, so a Job terraform owns keeps
        terraform's image, environment, secrets, labels and identity -- terraform
        already leaves both fields null, so this moves it towards its own
        configuration, never away. The dispatcher holds `run.jobs.update`
        (terraform/modules/iam/custom_roles.tf). `update_job` sends the Job's
        etag, so a concurrent writer is refused rather than overwritten.

        REFUSED, never run, if the correction fails. Running the bare runner is
        not a fallback: the DispatchError returns the lease and the task, and the
        code names what to fix.
        """
        from google.cloud import run_v2

        template = getattr(getattr(job, "template", None), "template", None)
        containers = list(getattr(template, "containers", None) or [])
        overriding = [c for c in containers if list(c.command) or list(c.args)]
        if not overriding:
            return
        found = ", ".join(
            f"{c.name or '<unnamed>'}: command={list(c.command)!r} args={len(c.args)}"
            for c in overriding
        )
        for container in overriding:
            container.command = []
            container.args = []
        log.warning(
            "Cloud Run job %s overrides the image ENTRYPOINT (%s), so its executions "
            "would run the bare runner instead of the worker lifecycle; clearing both "
            "before running it",
            job_id,
            found,
        )
        try:
            operation = client.update_job(request=run_v2.UpdateJobRequest(job=job))
            operation.result(timeout=120)
        except Exception as exc:  # any failure, a poll timeout included, refuses
            raise DispatchError(
                f"Cloud Run job {job_id} overrides the image ENTRYPOINT ({found}) and "
                f"could not be corrected: {exc}. Its executions would run the bare "
                "runner with no fencing, checkpoint, heartbeat or lease release, so "
                "nothing is started on it. Clear the container command and args on the "
                "Job, or delete it and let the scheduler recreate it.",
                code="cloud_run_job_overrides_entrypoint",
            ) from exc

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        # Before any API call: a tenant with no identity is refused here rather
        # than after a round trip that would only be wasted.
        assert_tenant_identity(tenant, self._settings)
        # The step's model before the Job is touched, for the same reason: a
        # class with no pin refuses with no API call made (#1435).
        step_model(self._settings, profile, task)
        name = self.ensure_job(profile, tenant, resource_class_for(task, profile))
        env = worker_env(task=task, lease=lease, tenant=tenant, settings=self._settings)
        env.update(quality_gates_dispatch_env(self._quality_gates, tenant.tenant_id, profile))
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    name="worker",
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()],
                )
            ],
            task_count=1,
            # Later than the lifecycle's own deadline, which is
            # TASK_TIMEOUT_SECONDS in `env`. See WORKER_FINALISE_BUDGET_SECONDS.
            timeout=duration_pb2.Duration(
                seconds=backend_deadline_seconds(
                    task.timeout_seconds,
                    dispatch_timeout_seconds=self._settings.core.dispatch_timeout_seconds,
                )
            ),
        )
        try:
            operation = self._jobs().run_job(
                request=run_v2.RunJobRequest(name=name, overrides=overrides)
            )
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"run_job failed for {name}: {exc}", code="cloud_run_run_job_failed"
            ) from exc

        # The execution name is available from the operation's metadata without
        # waiting for the execution to FINISH -- waiting here would hold the
        # drain loop open for the whole length of an agent run.
        metadata = getattr(operation, "metadata", None)
        execution_name = getattr(metadata, "name", None)
        if not execution_name:
            execution_name = f"{name}/executions/pending-{lease.attempt_id}"
        return execution_name


# --------------------------------------------------------------------------
# GKE Autopilot
# --------------------------------------------------------------------------

def gke_api_host(endpoint: str) -> str:
    """The `host` a kubernetes client Configuration needs for this cluster.

    GKE_ENDPOINT is `google_container_cluster.endpoint` passed straight through
    by terraform/infra/locals.tf, and that attribute is a BARE address --
    `34.118.229.12`, no scheme. The kubernetes client concatenates
    `configuration.host` with the resource path and hands the result to urllib3,
    which needs a scheme to choose a connection pool at all: without it every
    call fails on a URL it cannot parse, and it fails AFTER admission has taken
    the lease, so the task is already committed to a backend that will never
    start it.

    This function is duplicated, deliberately: the identical body lives in
    `reconciler.backends`. The two services are separate images that share only
    the FROZEN `swarm_common` package -- images/swarm-scheduler/Dockerfile
    copies `apps/common/` and `apps/scheduler/` and nothing else -- so neither
    can import the other and there is nowhere in-image to put a single copy.
    `tests/unit/control_plane/test_gke_client_host.py` pins the two together by
    asserting they agree on the same inputs, which is exactly the check that was
    missing while they did not: the reconciler added the scheme and this side
    did not.
    """
    raw = (endpoint or "").strip()
    if not raw:
        # The caller treats "unset" as "GKE is not configured" and refuses to
        # dispatch; it must not be turned into the URL "https://".
        return ""
    scheme, separator, rest = raw.partition("://")
    if not separator:
        rest = raw
    elif scheme.lower() != "https":
        # The client puts a cloud-platform OAuth token in an Authorization
        # header on every call; any other scheme puts it on the wire in clear.
        raise ValueError(
            "GKE_ENDPOINT must be a bare host or an https:// URL; "
            f"{scheme}:// would send the bearer token in clear text"
        )
    rest = rest.rstrip("/")
    if not rest:
        raise ValueError(f"GKE_ENDPOINT is not a usable host: {endpoint!r}")
    return f"https://{rest}"


def google_bearer_token(credentials: Any) -> str:
    """The access token for the NEXT Kubernetes API call, refreshed if stale.

    A Google OAuth access token lives about an hour, and on Cloud Run often much
    less: the metadata server hands out whatever remains of the token it has
    cached. This dispatcher is built once per process -- `main.create_app` puts
    the Scheduler in app state and the one-minute Cloud Scheduler tick keeps the
    instance warm -- so a token minted at first use is the token every later
    dispatch carries.

    The reconciler proved what that costs. Instance 00a41e8c of swarm-reconciler
    started at 2026-09-22T00:18:03Z, minted on its first pass at 00:20:24Z, and
    from 00:55:08Z answered 401 Unauthorized for 3h50m and 40 consecutive
    passes; the identical construction lived here. On this side the damage is
    worse in one way and better in another: every GKE dispatch on a warm
    instance fails, so a browser-profile task never starts -- but the 401 is
    wrapped into a DispatchError by `GkeJobDispatcher.dispatch`, so the lease
    does come back and no capacity leaks.

    `Credentials.valid` is google.auth's own answer to "would this token still be
    accepted", already carrying google.auth's refresh threshold, so a token this
    returns had time left when the request was built. Credentials that cannot say
    when they expire are refreshed on every call: one extra metadata round trip
    is cheaper than one dispatch lost to a dead token.
    """
    import google.auth.transport.requests

    expiry = getattr(credentials, "expiry", None)
    if expiry is None or not getattr(credentials, "valid", False):
        credentials.refresh(google.auth.transport.requests.Request())
    token = getattr(credentials, "token", None)
    if not token:
        # Never the token itself. `DispatchError.code` and the message below
        # reach `task.last_error`, which the API returns to the tenant verbatim.
        raise RuntimeError(
            "google.auth returned no access token for the GKE API; "
            "no job can be created without one"
        )
    return str(token)


def install_google_bearer_token(configuration: Any, credentials: Any) -> None:
    """Give a kubernetes Configuration a token it refreshes for every request.

    `refresh_api_key_hook` is the kubernetes client's own extension point for
    exactly this, and it is called on every single request:
    `ApiClient.update_params_for_auth` -> `Configuration.auth_settings` ->
    `get_api_key_with_prefix("authorization")` -> the hook. Hooking it is what
    lets the client, its connection pool and the CA file on disk stay cached for
    the life of the process while the credential does not.

    This function is duplicated in `reconciler.backends`, deliberately and for
    the same reason `gke_api_host` is: the two services ship as separate images
    that share only the FROZEN `swarm_common` package, so neither can import the
    other and there is nowhere in-image to put a single copy.
    `tests/unit/control_plane/test_gke_client_auth.py` pins the two copies
    together, by running the same expiring credential through both and asserting
    the same sequence of headers comes out.
    """

    def refresh(config: Any) -> None:
        config.api_key["authorization"] = f"Bearer {google_bearer_token(credentials)}"

    # Refused, not assumed. A Configuration that does not KNOW about the hook
    # would accept the attribute and never call it -- silently restoring the
    # one-shot token this replaces, with no test and no log line to show for it.
    # Both images resolve `kubernetes>=30` at build time rather than from a
    # lock, and this repository has already lost a working check to a client
    # version that quietly ignored what it was handed.
    if not hasattr(configuration, "refresh_api_key_hook"):
        raise RuntimeError(
            "this kubernetes client Configuration has no refresh_api_key_hook, "
            "so a GKE access token cannot be refreshed per request"
        )
    # Seeded as well as hooked. `auth_settings()` only asks for a bearer token
    # when `api_key` ALREADY holds an "authorization" entry, so a Configuration
    # carrying the hook alone would send no Authorization header at all -- and
    # the API server answers a missing bearer with the very same 401 this is
    # here to stop.
    configuration.api_key = {}
    refresh(configuration)
    configuration.refresh_api_key_hook = refresh


#: What the dispatch-failure message says instead of an address when this
#: process cannot name the identity it is authenticating with. It is a sentence
#: rather than an empty string or a plausible-looking default: a fabricated
#: address in this message would send the reader to check a RoleBinding against
#: an account that was never presented, which is the same class of mistake as
#: the numeric-default this repository refused in `render.py` -- it reads as a
#: fact and authorises nobody.
UNNAMED_IDENTITY = "an identity this process could not name"


def authenticated_identity(credentials: Any) -> str:
    """The service account this process presents to the Kubernetes API server.

    WHY THIS IS IN THE ERROR MESSAGE AT ALL. GKE resolves a Google service
    account that authenticated with an OAuth ACCESS token -- which is how
    `install_google_bearer_token` reaches the API -- to the account's numeric
    `uniqueId`, not to its email. So the 403 the API server sends back names a
    subject like

        User "117405034245659033603" cannot create resource "jobs" ...

    while every RoleBinding in this repository was written naming the EMAIL. On
    2026-09-24 that made a binding which applied cleanly authorise nobody, and
    the two spellings were far enough apart that nothing connected the digits in
    the error to the address in the manifest. Naming the account we authenticated
    AS, beside the namespace we tried, is what closes that gap: the reader then
    has the email to look up in the RoleBinding and the digits to compare it to.

    ASKED ONCE, AND NOT REFRESHED HERE. `swarm_api.delegation.service_account_email`
    does the same lookup in three escalating steps, the second of which is a
    `credentials.refresh()` -- on Cloud Run the attribute is the literal string
    "default" until something refreshes it. That step is unnecessary on this path
    because `install_google_bearer_token` has already minted a token from these
    credentials before this is called, so the refresh has happened; and the third
    step, a urllib call to the metadata server, is deliberately NOT copied here
    because this value is only ever used to decorate a failure message. A network
    call inside an error path can turn one failed dispatch into a hung one.

    Returns "" when the credentials cannot name themselves (a user credential
    from `gcloud auth application-default login`, or an unrefreshed compute
    credential). The caller substitutes UNNAMED_IDENTITY rather than guessing.
    """
    email = getattr(credentials, "service_account_email", None)
    # "default" is what compute and Cloud Run credentials report before a
    # refresh; it names no account and must not be printed as if it did.
    if isinstance(email, str) and email and email != "default" and "@" in email:
        return email
    return ""


# ---------------------------------------------------------------------------
# A stored tenant namespace is a RECORD, not authority (#1457)
# ---------------------------------------------------------------------------
#
# `tenants/u-bogdan` carried `namespace: swarm-u-bogdan`, the spelling used
# before 2026-09-23, while its real namespace was `swarm-tenant-u-bogdan`.
# `namespace_for` preferred the stored value unchecked, so every GKE task for
# that tenant was created in a namespace that does not exist -- and Kubernetes
# reports that as `jobs.batch is forbidden`, never as a missing namespace
# (docs/gke-dispatch-403.md). The owner's 2026-10-11 rule then kept a stored
# value inside the authority prefix (`swarm-tenant-`) and ignored one outside.
#
# THAT RULE ACCEPTED ANOTHER TENANT'S NAMESPACE. `swarm-tenant-B` is inside
# the prefix, and a tenant's worker can write its own `tenants/<id>` document,
# so tenant A's next GKE pod ran in B's namespace, as B's KSA and so as B's
# Google account (docs/design/tenant-settings-integrity.md §1.3 row 2). The
# namespace is therefore DERIVED, always, from the template and the tenant id:
#
#   * a stored value that is not the derived name is IGNORED and logged as
#     `tenant_namespace_mismatch` at ERROR, inside the prefix or not. A rename
#     within the prefix (`kubernetes/render.py --namespace`'s `-canary`) is
#     no longer followed by dispatch: that is the price of the fix, and the
#     derived namespace is the one terraform's tenancy module binds.
#
# The reconciler must read where this writes. `reconciler.backends
# .GkeBackend.namespace_for` (that image cannot import this one) derives the
# same name when it is handed no recorded value, and still keeps a recorded
# one inside the prefix; `reconciler.store.tenant_namespaces` is what hands it
# that value, and lane ISO-RECON (#1457) makes it the derived one. tests/unit/
# control_plane/test_reconciler_gke_namespaced.py pins the derived names of
# the two together.
#
# Logged at most once per tenant per process per hour: the dispatch loop asks
# on every dispatch, and the alert (terraform/modules/monitoring,
# `tenant_namespace_mismatch`) needs one entry, not one per task.
# `scripts/tenant-namespace-audit.sh` finds the same values before anything
# dispatches into them.

#: The event name the monitoring module's log-based metric counts. Spelled
#: once per image; the reconciler's copy is `reconciler.backends`'.
TENANT_NAMESPACE_MISMATCH = "tenant_namespace_mismatch"

#: One entry per tenant per process per this many seconds.
TENANT_NAMESPACE_MISMATCH_LOG_INTERVAL_S = 3600.0


class _OncePerInterval:
    """Says yes for a key at most once per `interval_s`, process-wide.

    Module-level rather than per dispatcher, so "once per process" holds however
    many dispatchers a process builds.
    """

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._clock = time.monotonic

    def __call__(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval_s:
                return False
            self._last[key] = now
            return True

    def clear(self) -> None:
        with self._lock:
            self._last.clear()


_namespace_mismatch_due = _OncePerInterval(TENANT_NAMESPACE_MISMATCH_LOG_INTERVAL_S)
#: The same, for `tenant_record_mismatch` (service account, GCS prefix).
_record_mismatch_due = _OncePerInterval(TENANT_NAMESPACE_MISMATCH_LOG_INTERVAL_S)


def verified_namespace(tenant_id: str, stored: Any, derived: str, prefix: str) -> str:
    """`derived`, always; logs a `stored` value that disagrees.

    See the block comment above. `prefix` is the authority prefix, the literal
    part of the namespace template before `{tenant}`; it only says, in the
    log, whether the ignored value was inside it -- another tenant's namespace
    or a rename -- or outside it, a stale spelling.
    """
    stored = str(stored) if stored else ""
    if not stored or stored == derived:
        return derived
    if _namespace_mismatch_due(tenant_id):
        inside = bool(prefix) and stored.startswith(prefix) and len(stored) > len(prefix)
        log.error(
            "%s tenant=%s stored=%r derived=%r: IGNORED (%s %r); dispatching to the "
            "derived namespace. Repair the tenant document "
            "(scripts/tenant-namespace-audit.sh)",
            TENANT_NAMESPACE_MISMATCH, tenant_id, stored, derived,
            "inside" if inside else "outside", prefix,
            extra={
                "event": TENANT_NAMESPACE_MISMATCH,
                "tenant_id": tenant_id,
                "stored": stored,
                "derived": derived,
                "used": derived,
            },
        )
    return derived


#: The GKE attempt's full execution name, `<namespace>/<job>`, on the worker
#: container (#1804; owner decision 2026-10-21). `agent_worker.lifecycle.
#: _execution_name` prefers it over every other source, so `record_attempt_start`
#: writes the name `GkeJobDispatcher.dispatch` returned rather than the pod's
#: HOSTNAME. GKE injects neither name into a pod: before this, a GKE worker
#: recorded `swarm-<task>-<gen>-<5 chars>`, the gate's [10] asked for a Job of
#: that name and called seven live agents gone, and cancel's probe answered
#: "not a <namespace>/<job> name" and left the Job running until the sweep.
#: GKE only: a Cloud Run worker still records CLOUD_RUN_EXECUTION.
GKE_EXECUTION_NAME_ENV = "SWARM_EXECUTION_NAME"


#: A GKE create that loses the namespace's ResourceQuota update race (#2063).
#: Quota admission charges a create by updating the ResourceQuota's `used`
#: under optimistic concurrency, so two creates in one namespace at once can
#: leave one answering 409 `Conflict` on `resourcequotas` ("the object has been
#: modified"). Nothing was created, so the create is sent again.
#:
#: THE BOUND: 3 more tries, so 4 creates in all, and under 2 s of backoff in
#: total. The race clears as soon as the competing create's quota write lands
#: -- milliseconds -- so a few short jittered tries clear any realistic burst
#: (an acceptance group starts its tasks together). Longer is wrong: this
#: create runs INLINE in the drain, inside `max_run_seconds`, holding a lease
#: no container occupies yet, and every other tenant's admission waits behind
#: it. Past the bound the DispatchError hands the task to the existing path:
#: lease released, READY after its 30 s backoff, the attempt counted against
#: `max_attempts` -- so a quota that keeps conflicting can never re-dispatch
#: for hours (store.py `return_to_ready_after_failed_dispatch`).
GKE_QUOTA_CONFLICT_RETRIES = 3
#: Each retry's backoff before jitter; the jitter multiplies by 0.5-1.5, which
#: keeps the whole worst case at 1.5 * (0.1 + 0.3 + 0.6) = 1.5 s. Jittered so
#: two creates that collided once do not collide again in lockstep.
GKE_QUOTA_CONFLICT_BACKOFF_SECONDS = (0.1, 0.3, 0.6)


def _kubernetes_status(exc: BaseException) -> dict[str, Any]:
    """The API server's `Status` object from a `kubernetes.client.ApiException`
    (`.body`, JSON text), or {} for anything that does not carry one: a
    transport error, or a body that is not JSON."""
    body = getattr(exc, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if not isinstance(body, str) or not body.strip():
        return {}
    try:
        status = json.loads(body)
    except ValueError:
        return {}
    return status if isinstance(status, dict) else {}


def _is_409(exc: BaseException, status: Mapping[str, Any]) -> bool:
    for value in (getattr(exc, "status", None), status.get("code")):
        try:
            if value is not None and int(value) == 409:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _is_quota_conflict(exc: BaseException) -> bool:
    """A 409 `Conflict` whose subject is a `resourcequotas` object.

    Read from the Status body, never from the exception's HTTP reason phrase:
    that is "Conflict" for EVERY 409, AlreadyExists included. A 409 Conflict
    on any other object is not this race and is not retried.
    """
    status = _kubernetes_status(exc)
    if not _is_409(exc, status) or status.get("reason") != "Conflict":
        return False
    details = status.get("details")
    return isinstance(details, dict) and details.get("kind") == "resourcequotas"


def _already_exists_name(exc: BaseException) -> str | None:
    """The Job name a 409 `AlreadyExists` names, else None."""
    status = _kubernetes_status(exc)
    if not _is_409(exc, status) or status.get("reason") != "AlreadyExists":
        return None
    details = status.get("details")
    if not isinstance(details, dict) or details.get("kind") != "jobs":
        return None
    name = details.get("name")
    return str(name) if name else None


# ---------------------------------------------------------------------------
# A 403 has three causes, and only one of them is ours (#1820)
# ---------------------------------------------------------------------------
#
# On 2026-10-20 19:24-20:06Z the admission policy `swarm-worker-job-constraints`
# refused every CPU-burst Job, and all 54-56 refusals read
# `gke_create_job_forbidden`, the code for an RBAC or namespace fault. They
# were neither: the platform said no to THIS manifest. A Job-quota refusal
# (`count/jobs.batch`, 200 per tenant namespace) reads the same today. So the
# API server's own Status message decides the code:
#
#   message                                          code
#   -----------------------------------------------  ------------------------
#   `ValidatingAdmissionPolicy '...' ... denied      gke_admission_refused
#     request` / `admission webhook "..." denied
#     the request`
#   `exceeded quota: <quota>, requested: ...`        gke_job_quota_exceeded
#   anything else (`User "..." cannot create`, a     gke_create_job_forbidden
#     namespace that does not exist)
#
# The admission and quota texts are the API server's (apiserver
# `validating/dispatcher.go`, `resourcequota/controller.go`), not ours, so they
# are matched as the server spells them. Unrecognised text keeps
# `gke_create_job_forbidden` and its namespace diagnosis: being wrong toward
# that code costs an operator one sentence, while being wrong away from it
# would hide an RBAC fault behind a "the platform refused" code.
#
# Once contract request 123 (PLATFORM_REFUSED) is applied, the two precise
# codes are the ones the drain parks on instead of spending an attempt
# (docs/design/dispatch-refusals.md). Until then they are only the code the
# task and the log carry; the store retries them as it retries any other.

GKE_ADMISSION_REFUSED = "gke_admission_refused"
GKE_JOB_QUOTA_EXCEEDED = "gke_job_quota_exceeded"
GKE_CREATE_JOB_FORBIDDEN = "gke_create_job_forbidden"

#: The codes that are the platform refusing this dispatch rather than this
#: dispatcher lacking a permission: what PLATFORM_REFUSED will park on.
PLATFORM_REFUSAL_CODES = frozenset({GKE_ADMISSION_REFUSED, GKE_JOB_QUOTA_EXCEEDED})

#: How long a finished worker Job (and so its pod and its cache claim) stays
#: in the tenant namespace: `ttlSecondsAfterFinished`. It holds a
#: `count/jobs.batch` slot for all of it, so the namespace admits
#: quota / (runtime + this) Jobs an hour. 600 s, from 3600, by BGD's decision
#: of 2026-10-28 after finished Jobs filled eng's quota of 240 and refused
#: every dispatch past it (#1820).
FINISHED_JOB_TTL_SECONDS = 600

_ADMISSION_DENIAL = re.compile(
    r"ValidatingAdmissionPolicy\b.*\bdenied request"
    r"|admission webhook \"[^\"]*\" denied the request",
    re.DOTALL,
)
_QUOTA_EXCEEDED = re.compile(r"\bexceeded quota:")


def forbidden_code(exc: BaseException) -> str:
    """The code for a 403 on Job create, from the API server's own reason.

    Read from the Status body's `message` when the exception carries one
    (`kubernetes.client.ApiException`), else from the exception's text, as
    `_is_forbidden` does. Admission is checked before quota: a webhook whose
    message happened to quote a quota is still the webhook refusing.
    """
    message = _kubernetes_status(exc).get("message")
    text = message if isinstance(message, str) and message else str(exc)
    if _ADMISSION_DENIAL.search(text):
        return GKE_ADMISSION_REFUSED
    if _QUOTA_EXCEEDED.search(text):
        return GKE_JOB_QUOTA_EXCEEDED
    return GKE_CREATE_JOB_FORBIDDEN


@dataclass(frozen=True)
class GkeTarget:
    endpoint: str
    ca_cert_path: str
    namespace_template: str = "swarm-tenant-{tenant}"
    #: Kubernetes service account the pod runs as. It must be the KSA the
    #: Workload Identity binding was issued for, or the pod gets no Google
    #: identity at all and cannot read its tenant's secret, its GCS prefix or
    #: Firestore. Terraform's `tenancy` module binds
    #: `<pool>[<namespace>/<ksa_name>]` with `ksa_name` defaulting to
    #: `swarm-agent-worker`, so that is the default here; `WORKER_KSA_NAME`
    #: overrides it for a cluster provisioned with a different spelling.
    ksa_name: str = "swarm-agent-worker"
    #: The cluster CA as terraform actually supplies it. `locals.tf` sets
    #: GKE_CA_CERT_B64 for this service (and for the reconciler) from
    #: `module.gke_autopilot[0].ca_certificate`, which is base64 exactly as the
    #: container API returns it. Nothing anywhere writes a CA file into this
    #: image, so `ca_cert_path` alone could never be satisfied in a deployed
    #: environment; it stays for a path supplied out of band.
    ca_cert_b64: str = ""

    @property
    def api_host(self) -> str:
        """`endpoint` as a URL the kubernetes client can use. See gke_api_host."""
        return gke_api_host(self.endpoint)


class GkeJobDispatcher:
    """Creates a batch/v1 Job in the tenant's own namespace.

    The cluster endpoint and CA bundle come from the environment (written by the
    terraform track) rather than from a live container.googleapis.com lookup, so
    dispatch needs no extra API dependency and no extra IAM role on the hot path.
    """

    #: Each pod is rendered from the size admission pinned (`BackendRouter.dispatch`).
    renders_pinned_size = True

    def __init__(self, settings: Any, *, target: GkeTarget | None = None,
                 batch_api: Any | None = None, identity: str = "",
                 judgment: Callable[[str], str] | None = None,
                 quality_gates: Callable[[str], dict[str, str]] | None = None) -> None:
        self._settings = settings
        self._target = target
        self._batch_api = batch_api
        #: tenant id -> the quality gates' verdict env, as the Cloud Run
        #: dispatcher's (`judgment.read_quality_gates_env`); None reads as off.
        self._quality_gates = quality_gates
        #: tenant id -> the workspace's Jev judgment backend (JUDGE-SELECT;
        #: `pod_judge_backend` over Firestore in a deployment, the router's
        #: reading). None reads as the default, internal-jev.
        self._judgment = judgment
        self._ca_file: str | None = None
        #: WHO THIS DISPATCHER AUTHENTICATES AS, for the failure message. Filled
        #: in by `_api()` from the credentials it resolves, so in a deployment it
        #: needs no wiring and cannot disagree with the token actually sent.
        #:
        #: `identity` travels with `batch_api` and exists for the same reason:
        #: a caller that supplies a ready-made Kubernetes client has taken over
        #: authentication, so this class can no longer learn the identity from
        #: google.auth and cannot invent it. Supplying the client without the
        #: identity is honest and costs only the vaguer message.
        self._identity: str = str(identity or "")

    def _ca_cert_file(self, target: GkeTarget) -> str:
        """A path on local disk holding the cluster CA.

        Terraform hands this service the CA base64-encoded in GKE_CA_CERT_B64,
        never as a file, so a base64 value is materialised once per process --
        the same thing the reconciler does with the same variable.
        """
        if target.ca_cert_path:
            return target.ca_cert_path
        if self._ca_file is None:
            handle = tempfile.NamedTemporaryFile(
                prefix="gke-ca-", suffix=".crt", delete=False
            )
            handle.write(base64.b64decode(target.ca_cert_b64))
            handle.close()
            self._ca_file = handle.name
        return self._ca_file

    def _api(self) -> Any:
        if self._batch_api is not None:
            return self._batch_api
        if self._target is None:
            raise DispatchError(
                "GKE dispatch is not configured: set GKE_ENDPOINT and GKE_CA_CERT_B64",
                code="gke_not_configured",
            )
        try:
            host = self._target.api_host
            ca_cert = self._ca_cert_file(self._target)
        except ValueError as exc:
            # A malformed GKE_ENDPOINT or a CA that is not base64 is a
            # configuration fault, and the dispatch loop catches DispatchError
            # and NOTHING else: any other exception escapes it with the lease
            # still held, so the slot stays occupied until the dispatch deadline
            # for a container that was never created. (binascii.Error, which
            # b64decode raises, is a ValueError.)
            raise DispatchError(
                f"GKE dispatch is misconfigured: {exc}", code="gke_misconfigured"
            ) from exc
        import google.auth
        from kubernetes import client as k8s

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        configuration = k8s.Configuration()
        configuration.host = host
        configuration.ssl_ca_cert = ca_cert
        # Per request, not once. `self._batch_api` is cached for the life of the
        # process and this method returns it unchanged on every later dispatch,
        # so a token minted here would be the token every subsequent
        # create_namespaced_job carried until the instance died.
        install_google_bearer_token(configuration, credentials)
        # AFTER the token is installed, not before: minting the token is what
        # refreshes these credentials, and an unrefreshed compute credential
        # reports its email as the literal string "default".
        self._identity = authenticated_identity(credentials)
        self._batch_api = k8s.BatchV1Api(k8s.ApiClient(configuration))
        return self._batch_api

    def namespace_for(self, tenant: Tenant) -> str:
        """The tenant's namespace: DERIVED from the template, never read.

        A stored `tenant.namespace` that differs is logged and ignored
        (`verified_namespace`): a tenant's worker can rewrite it, and any
        value inside the prefix would run this tenant's pod as another's.
        """
        template = (
            self._target.namespace_template if self._target else "swarm-tenant-{tenant}"
        )
        derived = sanitize_name(template.format(tenant=tenant.tenant_id))
        prefix = template.split("{tenant}", 1)[0]
        return verified_namespace(tenant.tenant_id, tenant.namespace, derived, prefix)

    def ksa_for(self, tenant: Tenant) -> str:
        """The Kubernetes service account the pod runs as.

        NOT derived from the tenant id. The KSA only means anything because a
        Workload Identity binding maps `<pool>[<namespace>/<ksa>]` to the
        tenant's Google service account, and that binding is issued by
        provisioning for a fixed KSA name inside the tenant's own namespace --
        the namespace is what makes it per-tenant, which is exactly why terraform
        notes that a pod in another tenant's namespace cannot impersonate this
        GSA even if it guesses the KSA name. Inventing a third spelling here
        produced a pod with no Google identity at all.
        """
        configured = str(getattr(self._settings, "worker_ksa_name", "") or "").strip()
        if not configured and self._target is not None:
            configured = self._target.ksa_name
        return sanitize_name(configured or "swarm-agent-worker")

    def _manifest(self, *, task: Task, lease: Lease, profile: RunnerProfile,
                  tenant: Tenant, sized: Sized | None = None) -> dict[str, Any]:
        assert_tenant_identity(tenant, self._settings)
        rc = RESOURCE_CLASSES[resource_class_for(task, profile)]
        disk = gke_disk(rc)
        # THE SIZE ADMISSION PINNED (SIZES-SCHED-2, docs/design/runner-sizes.md
        # §4.4): the one the lease's units were charged from and the attempt
        # records, never a fresh read of the setting, so a version saved after
        # the lease cannot render a pod other than the one admission paid for.
        # Without one (a caller other than admission) the class as it stands.
        size = sized.size if sized is not None else size_of(rc)
        live = live_display_on(profile, self._settings)
        split = None
        if live:
            split = (sized.live_split if sized is not None and sized.live_split is not None
                     else live_split_for(rc, size))
        # What THIS container was given, for the worker's own readings
        # (SIZES-WORKER-1: `_cpu_limit`'s fallback, `memory_limit_bytes_of`):
        # the worker's share on a live pod, the whole size otherwise.
        own = split[0] if split is not None else LiveShare(
            cpu_request=size.cpu_request, cpu_limit=size.cpu_limit, memory_gib=size.memory_gib)
        # Identifiers only. No provider key is injected: there is no Kubernetes
        # Secret to project from, and the worker fetches its tenant's key from
        # Secret Manager itself under the identity this pod's KSA assumes.
        # `gke_worker_env`, not `worker_env`: the GKE pod also names the
        # metadata server by address, and only the GKE pod (see its docstring).
        job_name = sanitize_name("swarm", task.id.replace("task_", ""), str(lease.generation))
        env = [{"name": k, "value": v} for k, v in
               gke_worker_env(task=task, lease=lease, tenant=tenant,
                              settings=self._settings).items()]
        env.extend({"name": k, "value": v} for k, v in quality_gates_dispatch_env(
            self._quality_gates, tenant.tenant_id, profile).items())
        # The Job's own name, for the worker's check 5 (contract request 34).
        # FROM THIS SAME RENDER, so the comparison proves only that this
        # dispatcher agrees with itself; GKE injects no Job name of its own.
        env.append({"name": "RUNNER_JOB_NAME", "value": job_name})
        # The pod's local scratch volumes and their limits (SCRATCH_VOLUMES_ENV,
        # #2436). Here and not in `gke_worker_env`, which sets CACHE_ROOT_ENV
        # for the same GKE-only reason, because the sizes are the resource
        # class's (`disk`, above) and that function is not given the class.
        env.append({"name": SCRATCH_VOLUMES_ENV, "value": scratch_volumes_value(disk)})
        # The attempt's execution name, from THIS render: the namespace below
        # is `metadata.namespace` and `dispatch` returns this very string, so
        # what the worker records and what the dispatcher records are one value
        # (GKE_EXECUTION_NAME_ENV says why it exists).
        namespace = self.namespace_for(tenant)
        env.append({"name": GKE_EXECUTION_NAME_ENV, "value": f"{namespace}/{job_name}"})
        # THE STEP'S MODEL comes from `gke_worker_env` above (`step_model`,
        # #1435), the one source both backends share. Only a scheduler with no
        # WORKER_CLASS_MODELS leaves it unset for a classed profile, and then
        # the pod carries the profile's model as a Cloud Run Job does (#226;
        # contract request 53): `profile_model` over WORKER_MODELS, and the
        # same absence -- no pinned model, no MODEL, the CLI's default on both
        # backends. Never `task.model`: a caller never chooses it (invariant 10).
        if not any(e["name"] == "MODEL" for e in env):
            model = profile_model(self._settings, profile)
            if model:
                env.append({"name": "MODEL", "value": model})
        # THE STEP-SPEC VERIFICATION SETTINGS, ON THE JOB, as a Cloud Run Job
        # carries them (contract request 34; owner decision 2026-10-08). This
        # reverses the original GKE design, which delivered them ONLY through
        # the `swarm-spec-verify-keys` ConfigMap mount below. That was safe
        # while GKE ran browser alone in namespaces Terraform applied; it stopped
        # being enough when claude-code moved to GKE (PR 866) and onboarding
        # (#847) began creating a namespace per person -- a namespace no deploy
        # had rendered the ConfigMap into, where every task then ended
        # CANNOT_START. From the scheduler's OWN settings, never `worker_env`
        # (which a task shapes), so a caller still cannot choose the keys it is
        # verified against. The worker still fails closed on a missing or bad
        # signature; only a missing ConfigMap stops being fatal.
        for name, value in spec_job_env(self._settings).items():
            env.append({"name": name, "value": value})
        # The size the scheduler rendered this container at (design §4.4).
        # Platform-derived, from admission's own reading, never from a task
        # document (invariant 10), as `spec_job_env` above.
        env += [
            {"name": "SWARM_SIZED_CPU_REQUEST", "value": f"{own.cpu_request:g}"},
            {"name": "SWARM_SIZED_CPU_LIMIT", "value": f"{own.cpu_limit:g}"},
            {"name": "SWARM_SIZED_MEMORY_GIB", "value": f"{own.memory_gib:g}"},
        ]
        # Memory and ephemeral-storage are the same number in both directions
        # (invariant 7); CPU requests the class's `cpu` -- what admission
        # charged and Autopilot bills -- and is limited to `cpu_limit_cores`,
        # which it reaches by borrowing its node's idle requested CPU (RS-1,
        # SIZES-SCHED-1). On a cluster without bursting Autopilot lowers the
        # limit to the request; the worker records the limit the kernel
        # enforces (`cpu.max`), which is how that would show.
        fixed = {
            "memory": f"{size.memory_gib}Gi",
            # The whole pod's local disk -- the sum of the scratch volumes'
            # sizeLimits below, not the workspace's alone, and not the
            # cache's network disk, which is no ephemeral-storage: see GkeDisk.
            "ephemeral-storage": f"{gke_ephemeral_storage_gib(rc)}Gi",
        }
        requests = {"cpu": cpu_quantity(size.cpu_request), **fixed}
        limits = {"cpu": cpu_quantity(size.cpu_limit), **fixed}
        labels = {
            "managed-by": "swarm-scheduler",
            "swarm-tenant": sanitize_name(tenant.tenant_id),
            "swarm-profile": sanitize_name(profile.name),
            "swarm-resource-class": sanitize_name(rc.name),
            "swarm-task": sanitize_name(task.id),
        }
        # Autopilot extended run time: the pod is not evicted for scale-down.
        # This is only available on on-demand capacity, which is why Spot is
        # disabled platform-wide.
        #
        # ON THE POD TEMPLATE AS WELL AS THE JOB, and the pod is the one that
        # matters. The cluster autoscaler reads this annotation from PODS, and a
        # Job's own annotations are not copied onto the pods it creates -- so
        # until 2026-09-24, when it sat on the Job alone, every pod this
        # dispatcher created was evictable in a scale-down while this comment
        # said it was not. kubernetes/worker-templates/ had it on both; the
        # parity test in tests/unit/worker/test_kubernetes_manifests.py
        # (test_the_rendered_job_and_the_dispatched_job_are_the_same_job) is
        # what found the difference.
        not_evictable = {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"}
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": labels,
                "annotations": dict(not_evictable),
            },
            "spec": {
                # The platform owns retries; a k8s-level retry would re-run the
                # agent under a stale fencing generation.
                "backoffLimit": 0,
                "completions": 1,
                "parallelism": 1,
                # Later than the lifecycle's own deadline (TASK_TIMEOUT_SECONDS
                # in `env`), which it must never pre-empt: the Job controller
                # SIGTERMs the pod, and a SIGTERMed lifecycle PARKS the task
                # instead of failing it. See WORKER_FINALISE_BUDGET_SECONDS.
                "activeDeadlineSeconds": backend_deadline_seconds(
                    task.timeout_seconds,
                    dispatch_timeout_seconds=self._settings.core.dispatch_timeout_seconds,
                ),
                # TEN MINUTES, not an hour (BGD 2026-10-28). A finished Job
                # keeps its `count/jobs.batch` slot until this expires, and on
                # 2026-10-28 ~09:30-11:15Z eng's 240 slots filled with FINISHED
                # Jobs from a burst of ~45 lanes: every dispatch past them was
                # refused `gke_job_quota_exceeded` and 28 steps FAILED. Ten
                # minutes is still long enough to read a failed pod's status
                # and logs live; Cloud Logging keeps them after.
                "ttlSecondsAfterFinished": FINISHED_JOB_TTL_SECONDS,
                "template": {
                    "metadata": {
                        "labels": dict(labels),
                        "annotations": dict(not_evictable),
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": self.ksa_for(tenant),
                        # The workload is arbitrary code from a caller-supplied
                        # repository on a cluster shared with other tenants. A
                        # mounted Kubernetes API token is the first thing a
                        # prompt injection or a malicious repository reaches for,
                        # and the worker never calls the Kubernetes API.
                        "automountServiceAccountToken": False,
                        # The worker checkpoints on SIGTERM; killing it at the
                        # 30s default would lose the workspace it was uploading.
                        "terminationGracePeriodSeconds": 120,
                        # No cluster search path; see GKE_POD_DNS_CONFIG (#341).
                        # Copies, so no manifest shares the module's dict.
                        "dnsPolicy": GKE_POD_DNS_POLICY,
                        "dnsConfig": copy.deepcopy(GKE_POD_DNS_CONFIG),
                        "securityContext": POD_SECURITY_CONTEXT,
                        "containers": [
                            {
                                "name": "worker",
                                "image": image_uri(self._settings, profile),
                                # NO "command" AND NO "args". The image
                                # ENTRYPOINT, `tini -- python -m agent_worker`,
                                # is the worker lifecycle; it reads
                                # RUNNER_PROFILE from `env` below and starts
                                # `profile.runner_argv` as its supervised CHILD.
                                # Setting `command` here replaced the lifecycle
                                # with the bare runner on every GKE pod until
                                # 2026-09-24: no fencing, no cancel, no
                                # heartbeat, no checkpoint, no lease release
                                # (incident wf_ebb3ab2d65664707a559; module
                                # docstring).
                                "env": env,
                                # Memory and disk equal in both directions;
                                # CPU bursts past its request (above).
                                "resources": {"requests": requests, "limits": limits},
                                "securityContext": CONTAINER_SECURITY_CONTEXT,
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": WORKSPACE_MOUNT},
                                    {"name": "dshm", "mountPath": "/dev/shm"},
                                    # The step-spec public keys (contract
                                    # request 34), terraform's ConfigMap in
                                    # this namespace, READ-ONLY. The FALLBACK
                                    # since 2026-10-08: `env` above carries
                                    # the scheduler's copy, which wins, and
                                    # the worker reads this mount only when
                                    # `env` has no SPEC_VERIFY_KEYS. Kept so a
                                    # scheduler deployed without the keys
                                    # still verifies wherever Terraform
                                    # applied the ConfigMap.
                                    {"name": "spec-verify-keys",
                                     "mountPath": SPEC_VERIFY_KEYS_MOUNT,
                                     "readOnly": True},
                                    # readOnlyRootFilesystem means every path the
                                    # runtime writes to needs a volume: /tmp, and
                                    # a HOME for tool caches and crash dumps.
                                    {"name": "tmp", "mountPath": "/tmp"},
                                    {"name": "home", "mountPath": WORKER_HOME},
                                    # The agent's tool caches and TMPDIR,
                                    # off the workspace (CACHE-SCRATCH); the
                                    # worker is told where by CACHE_ROOT_ENV.
                                    {"name": "cache", "mountPath": CACHE_MOUNT},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "workspace", "emptyDir": {
                                "sizeLimit": f"{disk.workspace_gib}Gi"}},
                            # Chromium's shared memory. The 64 MiB default is
                            # what makes headless Chrome crash under load, and
                            # is the reason browser work is on GKE at all.
                            # Every pod gets it, so `claude-code-browser`
                            # (contract request 67) has it too; its `browser`
                            # class (8 GiB since contract request 79) is what
                            # pays for it, since this tmpfs is charged to the
                            # memory limit. Chromium's
                            # own sandbox stays off on both profiles
                            # (CONTAINER_SECURITY_CONTEXT): the claude-code
                            # runner tells its agent to launch with
                            # chromium_sandbox=False.
                            {"name": "dshm", "emptyDir": {
                                "medium": "Memory", "sizeLimit": "2Gi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": f"{disk.tmp_gib}Gi"}},
                            {"name": "home", "emptyDir": {"sizeLimit": f"{disk.home_gib}Gi"}},
                            # A NETWORK DISK, not a local emptyDir (CACHE-PVC):
                            # the 2 GiB emptyDir evicted, and Autopilot's
                            # 10 GiB had no more to give it. The ephemeral-
                            # volume controller creates the claim with the pod
                            # and deletes it with the pod, under its own
                            # identity, so the dispatcher's Role needs nothing
                            # new. Not ephemeral-storage. See CACHE_STORAGE_CLASS.
                            {"name": "cache", "ephemeral": {"volumeClaimTemplate": {
                                "spec": {
                                    "accessModes": ["ReadWriteOnce"],
                                    "storageClassName": CACHE_STORAGE_CLASS,
                                    "resources": {"requests": {
                                        "storage": f"{CACHE_VOLUME_GIB}Gi"}},
                                }}}},
                            # `optional`: a namespace whose ConfigMap has not
                            # been applied (one onboarding created) still
                            # starts its pod, and verifies with the keys in
                            # `env`. Only when NEITHER carries keys does the
                            # worker exit CANNOT_START for every task, signed
                            # or not -- loud, and never an unverified run.
                            {"name": "spec-verify-keys", "configMap": {
                                "name": SPEC_VERIFY_KEYS_CONFIG_MAP,
                                "optional": True}},
                        ],
                    },
                },
            },
        }
        # The live browser's display sidecar (docs/design/live-browser.md):
        # only for a live-browser profile, and only while the rollout switch
        # is on. Every other pod is returned exactly as built above.
        if live:
            self._with_judgment(job, tenant, profile)
            job = with_live_display(job, rc=rc, split=split)
            # Residential egress (docs/browser-egress.md §10.3): only on a pod
            # that has the display sidecar, so only a browser that exists is
            # routed, and only when configuration switches its tenant and
            # profile on. Otherwise the live pod is returned as it was.
            if browser_egress_on(self._settings, tenant_id=tenant.tenant_id, profile=profile):
                job = with_browser_egress(
                    job, bypass=getattr(self._settings, "browser_egress_bypass", ()))
        elif jev_judgments_switched_on(task):
            # The signed workflow switch `jev_judgments: on` (2026-11-01):
            # the TypeSafe forwarder's label and variable only, nothing else.
            self._with_judgments_forwarder(job, tenant, profile)
        return job

    def _with_judgment(self, job: dict[str, Any], tenant: Tenant, profile: Any) -> None:
        """Where this live-browser pod's Jev judgments go (`judgment`,
        JUDGE-SELECT): SWARM_JEV_BACKEND always, and for an internal backend
        with the tailnet egress on, JEV_API_URL, the `swarm-judge: internal`
        pod label JUDGE-NET's NetworkPolicy opens the decision API to, and the
        hostAliases entry that resolves the API's tailnet name to the egress
        Service. For the TypeSafe backend with the forwarder serving the
        tenant (`typesafe_forwarder_on`), the `swarm-typesafe: inject` label
        that opens swarm-typesafe-fwd instead, and SWARM_TYPESAFE_INJECT=on on
        the worker, which turns its forwarder path on. Only with the live browser:
        jev-browser attaches to nothing else."""
        backend = self._judgment(tenant.tenant_id) if self._judgment else DEFAULT_BACKEND
        env, labels, host_aliases = jev_dispatch(
            backend, str(getattr(self._settings, "internal_decision_api_url", "") or ""))
        if typesafe_forwarder_on(self._settings, tenant_id=tenant.tenant_id,
                                 profile=profile, backend=backend):
            labels[TYPESAFE_POD_LABEL[0]] = TYPESAFE_POD_LABEL[1]
            env[TYPESAFE_INJECT_ENV[0]] = TYPESAFE_INJECT_ENV[1]
        template = job["spec"]["template"]
        (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
        worker["env"].extend({"name": k, "value": v} for k, v in env.items())
        template["metadata"]["labels"].update(labels)
        if host_aliases:
            template["spec"].setdefault("hostAliases", []).extend(host_aliases)

    def _with_judgments_forwarder(self, job: dict[str, Any], tenant: Tenant,
                                  profile: Any) -> None:
        """A pod with no live browser whose signed workflow switch says
        `jev_judgments: on` (2026-11-01): the `swarm-typesafe: inject` label
        and SWARM_TYPESAFE_INJECT=on when the forwarder serves its tenant on
        the TypeSafe backend (`typesafe_forwarder_on`), so the worker's
        judgments use the shim as the quality checks do. Nothing else of
        `_with_judgment`: every other pod is returned exactly as built."""
        backend = self._judgment(tenant.tenant_id) if self._judgment else DEFAULT_BACKEND
        if not typesafe_forwarder_on(self._settings, tenant_id=tenant.tenant_id,
                                     profile=profile, backend=backend,
                                     judgments_switched_on=True):
            return
        template = job["spec"]["template"]
        (worker,) = [c for c in template["spec"]["containers"] if c["name"] == "worker"]
        worker["env"].append({"name": TYPESAFE_INJECT_ENV[0], "value": TYPESAFE_INJECT_ENV[1]})
        template["metadata"]["labels"][TYPESAFE_POD_LABEL[0]] = TYPESAFE_POD_LABEL[1]

    @staticmethod
    def _is_forbidden(exc: BaseException) -> bool:
        """True for a 403 from the API server, however the client reports it.

        `kubernetes.client.ApiException` carries `.status`, but this path also
        sees transport wrappers and, in tests, plain exceptions -- so the
        status is preferred and the message is the fallback rather than the
        other way round. Being wrong in the FALSE direction costs the extra
        sentence below; being wrong in the TRUE direction would add it to an
        unrelated failure, so neither is expensive and neither is guessed at
        beyond these two signals.
        """
        status = getattr(exc, "status", None)
        if status is not None:
            try:
                return int(status) == 403
            except (TypeError, ValueError):
                pass
        text = str(exc)
        return "is forbidden" in text or '"code": 403' in text or "'code': 403" in text

    def presented_identity(self) -> str:
        """The identity for the failure message, never blank and never invented."""
        return self._identity or UNNAMED_IDENTITY

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant,
                 sized: Sized | None = None) -> str:
        manifest = self._manifest(task=task, lease=lease, profile=profile, tenant=tenant,
                                  sized=sized)
        namespace = manifest["metadata"]["namespace"]
        # What the attempt records, read back from the render BEFORE anything
        # is created: the worker records the same string from its env.
        recorded = _manifest_execution_name(manifest)
        try:
            self._create(namespace, manifest)
        except DispatchError:
            raise
        except Exception as exc:  # kubernetes.client.ApiException and transport errors
            # THIS ATTEMPT'S OWN JOB ALREADY EXISTS: adopted, never created
            # twice and never failed. The name carries the lease's generation
            # (`_manifest`), so only this attempt ever creates it, and a Job of
            # an older generation has another name and is not adopted
            # (invariant 5). Failing here would release the lease of a Job
            # that runs.
            if _already_exists_name(exc) == manifest["metadata"]["name"]:
                log.info("gke create found its own job already created namespace=%s job=%s",
                         namespace, manifest["metadata"]["name"])
                return recorded
            # KUBERNETES AUTHORISES BEFORE IT RESOLVES, so a 403 here does NOT
            # establish that this is a permissions problem.
            #
            # A Job created into a namespace that does not exist comes back as
            #
            #     jobs.batch is forbidden: User "1174050342..." cannot create
            #     resource "jobs" in API group "batch" in the namespace
            #     "swarm-tenant-eng"
            #
            # -- the authorizer runs against the namespaced request before
            # anything looks for the namespace, so the API server reports a
            # missing permission and never a missing namespace. There is no 404
            # to wait for. On 2026-09-23 that message sent three separate
            # investigations at IAM while the actual cause was that the
            # provisioner spelled the namespace `swarm-eng` and this dispatcher
            # spelled it `swarm-tenant-eng`; all seven `browser` tasks this
            # platform had ever accepted had failed that way, over two days.
            #
            # So the namespace is NAMED and the ambiguity is stated, in the
            # message rather than in a doc nobody reads at 03:00. `loop.py`
            # logs `str(exc)` on its `dispatch failed` line, which is where an
            # operator meets this. docs/gke-dispatch-403.md has the mechanism and
            # docs/incidents/2026-09-24-gke-dispatch.md the whole sequence.
            #
            # AND THE IDENTITY IS NAMED TOO, which it was not until 2026-09-24.
            # Four of the seven causes behind that outage -- a wrong namespace, a
            # missing Role, a missing RoleBinding, and a RoleBinding whose subject
            # named the right account by the wrong one of its two names -- all
            # produce this one message, and the two facts that separate them are
            # WHERE we tried and WHO we were. The upstream text supplies the
            # numeric uniqueId GKE resolved us to; only this process knows which
            # email that is, and the gap between those two spellings is what hid
            # the real cause for two days. Both are now in one line.
            if self._is_forbidden(exc):
                identity = self.presented_identity()
                code = forbidden_code(exc)
                if code != GKE_CREATE_JOB_FORBIDDEN:
                    # The platform refused this manifest: the namespace and
                    # the identity are fine, or nothing would have reached
                    # admission or quota, so the 403-means-404 diagnosis
                    # below would send the reader at the wrong thing.
                    raise DispatchError(
                        f"GKE refused the job in {namespace} ({code}): {exc} "
                        f"-- the platform's admission policy or the namespace's "
                        f"ResourceQuota said no to this manifest; RBAC and the "
                        f"namespace are not the cause. See "
                        f"docs/design/dispatch-refusals.md.",
                        code=code,
                    ) from exc
                raise DispatchError(
                    f"could not create GKE job in {namespace} as {identity}: {exc} "
                    f"-- NOTE: a 403 here does not prove this is a permissions "
                    f"problem. Kubernetes authorises before it resolves, so a Job "
                    f"created into a namespace that DOES NOT EXIST is also reported "
                    f"as `jobs.batch is forbidden`, never as 404. Before changing any "
                    f"IAM, confirm the namespace {namespace} exists and that its "
                    f"swarm-dispatcher RoleBinding names the account this dispatcher "
                    f"authenticated as -- {identity} -- BOTH by email and by numeric "
                    f"uniqueId: kubectl get ns {namespace} && kubectl get rolebinding "
                    f"swarm-dispatcher -n {namespace} -o yaml. This dispatcher "
                    f"presents an OAuth access token, and on that path GKE names the "
                    f"caller by uniqueId -- the `User \"...\"` above -- so a subject "
                    f"list carrying the email alone applies cleanly and authorises "
                    f"nobody. See docs/gke-dispatch-403.md and "
                    f"docs/incidents/2026-09-24-gke-dispatch.md.",
                    code=GKE_CREATE_JOB_FORBIDDEN,
                ) from exc
            # A quota race that outlasted GKE_QUOTA_CONFLICT_RETRIES keeps
            # today's code (the loop, the runbook and the UI read it) and says
            # so in the operator's message.
            raced = (
                f" (lost the ResourceQuota update race on all "
                f"{GKE_QUOTA_CONFLICT_RETRIES + 1} tries)"
                if _is_quota_conflict(exc) else ""
            )
            raise DispatchError(
                f"could not create GKE job in {namespace} as "
                f"{self.presented_identity()}{raced}: {exc}",
                code="gke_create_job_failed",
            ) from exc
        # The name the worker was handed, never a second derivation of it
        # (#1804). The Job is created by exact name, never `generateName`, so
        # the server's answer cannot name another Job; and nothing is checked
        # AFTER the create, because a dispatch that raised there would release
        # the lease of a Job that runs.
        return recorded

    def _create(self, namespace: str, manifest: dict[str, Any]) -> None:
        """`create_namespaced_job`, sent again while the namespace's
        ResourceQuota update race refuses it (GKE_QUOTA_CONFLICT_RETRIES).
        Anything else, and the last conflict, is raised to `dispatch`."""
        api = self._api()
        tries = GKE_QUOTA_CONFLICT_RETRIES + 1
        for attempt in range(1, tries + 1):
            try:
                api.create_namespaced_job(namespace=namespace, body=manifest)
                return
            except Exception as exc:
                if attempt == tries or not _is_quota_conflict(exc):
                    raise
                delay = GKE_QUOTA_CONFLICT_BACKOFF_SECONDS[attempt - 1] * random.uniform(0.5, 1.5)
                # The namespace, the try and the reason: never the manifest or
                # the upstream body, which carry the pod spec and identities.
                log.warning(
                    "gke create lost the resourcequota update race namespace=%s try=%d/%d "
                    "reason=Conflict on resourcequotas; retrying in %.2fs",
                    namespace, attempt, tries, delay,
                )
                time.sleep(delay)


def _manifest_execution_name(manifest: dict[str, Any]) -> str:
    """GKE_EXECUTION_NAME_ENV as `GkeJobDispatcher._manifest` rendered it."""
    for container in manifest["spec"]["template"]["spec"]["containers"]:
        for entry in container.get("env") or []:
            if entry.get("name") == GKE_EXECUTION_NAME_ENV:
                return str(entry["value"])
    raise DispatchError(
        f"the rendered GKE job carries no {GKE_EXECUTION_NAME_ENV}; nothing was created",
        code="gke_create_job_failed",
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

class BackendRouter:
    """Picks the dispatcher for a profile's resolved backend."""

    def __init__(self, *, cloud_run: Dispatcher | None, gke: Dispatcher | None,
                 settings: Any, judgment: Callable[[str], str | None] | None = None) -> None:
        self._cloud_run = cloud_run
        self._gke = gke
        self._settings = settings
        #: tenant id -> the judgment backend its worker's judge will resolve
        #: (`read_judge_backend` in a deployment), None when unreadable. Unset
        #: reads as unreadable: `cloud_run_refused` then fails closed.
        self._judgment = judgment

    def cloud_run_refused(self, *, tenant_id: str, profile: RunnerProfile,
                          backend: Backend) -> bool:
        """`typesafe_judge_needs_gke` for this attempt. The stored setting is
        read only when everything else already says yes, so with the switch
        off a dispatch reads nothing it did not read before."""
        if not typesafe_judge_needs_gke(self._settings, tenant_id=tenant_id, profile=profile,
                                        backend=backend, judgment=TYPESAFE_BACKEND):
            return False
        judgment = self._judgment(tenant_id) if self._judgment is not None else None
        return typesafe_judge_needs_gke(self._settings, tenant_id=tenant_id, profile=profile,
                                        backend=backend, judgment=judgment)

    def for_backend(self, backend: Backend) -> Dispatcher:
        if backend is Backend.CLOUD_RUN_JOB:
            if self._cloud_run is None or not self._settings.core.enable_cloud_run_jobs:
                raise DispatchError(
                    "Cloud Run Jobs dispatch is disabled", code="backend_disabled"
                )
            return self._cloud_run
        if backend is Backend.GKE_AUTOPILOT:
            if self._gke is None or not self._settings.core.enable_gke_autopilot:
                raise DispatchError(
                    "GKE Autopilot dispatch is disabled", code="backend_disabled"
                )
            return self._gke
        raise DispatchError(
            f"backend {backend} was never resolved to a concrete target",
            code="backend_unresolved",
        )

    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant, backend: Backend,
        sized: Sized | None = None,
    ) -> str:
        # A TYPESAFE JUDGE NEVER RUNS AS A CLOUD RUN JOB (lane JUDGE-GKE). The
        # scheduler routes by the catalogue's backend and has no runtime
        # fallback, so a GKE profile reaches Cloud Run only through a rollback
        # of its catalogue line (terraform/infra/locals.tf
        # `cloud_run_fallback_profiles` keeps those Jobs), and a CLOUD_RUN_JOB
        # profile by contract (claude-code-review, contract request 84). Either
        # way the forwarder, an in-cluster Service, is out of reach. Refused
        # BEFORE anything is created: the DispatchError returns the lease and
        # the task to READY, where it holds no capacity (invariant 1) and no
        # pool stays reserved (2). Each refusal is an attempt, so at the retry
        # cap the task ends FAILED with this code: loud, never a Cloud Run
        # judge. Rerouting it to GKE here is not an option: the lease
        # reserved the Cloud Run backend's pool.
        if self.cloud_run_refused(tenant_id=tenant.tenant_id, profile=profile, backend=backend):
            raise DispatchError(
                f"profile {profile.name} judges with TypeSafe in tenant {tenant.tenant_id} "
                "and the forwarder serves that tenant, so it does not run as a Cloud Run "
                "Job: the forwarder is reachable only from GKE (lane JUDGE-GKE)",
                code=TYPESAFE_JUDGE_NEEDS_GKE,
            )
        dispatcher = self.for_backend(backend)
        # THE PINNED SIZE GOES ONLY TO A DISPATCHER THAT RENDERS IT (SIZES-SCHED-2):
        # the GKE renderer sizes each pod from it. A Cloud Run Job's resources
        # are fixed on the Job, which is why a Cloud Run profile is pinned at
        # the code default it already runs at (design §4.7), so there is
        # nothing to hand it.
        if sized is not None and getattr(dispatcher, "renders_pinned_size", False):
            return dispatcher.dispatch(
                task=task, lease=lease, profile=profile, tenant=tenant, sized=sized
            )
        return dispatcher.dispatch(task=task, lease=lease, profile=profile, tenant=tenant)


def _gke_ca_file() -> str:
    """Materialise the cluster CA bundle, returning a path, or "" if unset.

    The environment carries the bundle base64-encoded (`GKE_CA_CERT_B64`, written
    by terraform from the cluster's own output) because a Cloud Run environment
    variable is a string and a CA bundle is a file. This end of it used to read
    `GKE_CA_CERT_PATH` instead -- a name nothing has ever set, and a different
    shape besides -- so `target` was always None and every GKE dispatch died on
    "GKE dispatch is not configured".

    It never looked like a configuration bug: the scheduler treats a dispatch
    failure as transient, returns the task to READY and retries on the next
    drain, so the symptom was a browser task that stayed queued forever rather
    than anything that crashed or alerted. The reconciler read the right name
    all along (reconciler/backends.py), which is what made this survivable and
    also what made it invisible -- the two halves disagreed and only one was
    ever exercised.
    """
    import base64
    import os
    import tempfile

    ca_b64 = os.environ.get("GKE_CA_CERT_B64", "").strip()
    if not ca_b64:
        return ""
    handle = tempfile.NamedTemporaryFile(prefix="gke-ca-", suffix=".crt", delete=False)
    handle.write(base64.b64decode(ca_b64))
    handle.close()
    return handle.name


def build_router(settings: Any, *, pool: AccountPool | None = None,
                 db: Any | None = None) -> BackendRouter:
    """The deployment's router. `pool` is the Scheduler's AccountPool.

    main.build_scheduler passes it, so the Cloud Run Job's secret mount and
    admission read one account list per drain (credentials.py). Without it the
    Cloud Run dispatcher gets `AccountPool.unwired`, which is loud rather than
    a second answer.
    """
    import os

    endpoint = os.environ.get("GKE_ENDPOINT", "").strip()
    # _gke_ca_file already decodes GKE_CA_CERT_B64 to a file, so there is one
    # representation of the bundle here rather than two.
    ca_path = _gke_ca_file()
    target = None
    if endpoint and ca_path:
        target = GkeTarget(
            endpoint=endpoint,
            ca_cert_path=ca_path,
            ksa_name=getattr(settings, "worker_ksa_name", "") or "swarm-agent-worker",
        )
    # None while SETTINGS_MAC_REQUIRED is off; otherwise both readers below
    # count only a value swarm-api signed (#1457).
    mac_key = settings_mac_key(settings)
    # The quality gates' verdict (contract request 128): one document read
    # per claude-code dispatch, the block's MAC checked here because no worker
    # holds the key. Without `db`, every claude-code attempt is told off.
    gates = ((lambda tenant_id: read_quality_gates_env(db, tenant_id, mac_key=mac_key))
             if db is not None else None)
    return BackendRouter(
        cloud_run=CloudRunJobDispatcher(settings, pool=pool, quality_gates=gates),
        # `db` for the workspace's Jev judgment backend (JUDGE-SELECT), one
        # document read per live-browser dispatch; without it, the default.
        # Read in the router's precedence (#1334 c31), so the pod judges where
        # the router decided it would.
        gke=GkeJobDispatcher(
            settings, target=target,
            judgment=(lambda tenant_id: pod_judge_backend(db, tenant_id, mac_key=mac_key))
            if db is not None else None,
            quality_gates=gates),
        settings=settings,
        # The same document, read as the worker's judge reads it (JUDGE-GKE).
        judgment=(lambda tenant_id: read_judge_backend(db, tenant_id, mac_key=mac_key))
        if db is not None else None,
    )
