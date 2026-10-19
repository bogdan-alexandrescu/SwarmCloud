# Multi-repository steps: one step reads several registered repositories, writes one

**Status: design, 2026-10-17 (lane MULTI-REPO-DESIGN, part of #1434).
Nothing here is built.** The owner reads this and answers §7 before any build
lane starts. The frozen-contract half is in §5 as requests, not changes
(CLAUDE.md rule 1); the operator files them in
[`docs/contract-change-requests.md`](../contract-change-requests.md) once
they are approved.

Every file and symbol cited below was read on 2026-10-17 at `35f2a61`.
Symbols are cited as `path::qualname` because line numbers in `lifecycle.py`
and `service.py` move by hundreds in one merge wave (#453, #647).

What the design settles, in one line each:

* **A step may name up to four additional repositories**, each by
  `owner/repo` from the tenant's own `repositories` registrations, plus a ref.
  Never a URL, so a caller can never supply a credential-bearing URL, a
  remote or a host (§4.1).
* **swarm-api resolves each ref to a 40-hex commit at submission** and signs
  it into the task (new spec format 4). Every attempt and every step that
  shares the list sees the same commits (§4.2).
* **The worker clones each one before the agent starts**, at its pinned
  commit, depth 1, with a forge slot swarm-api chose per repository. It
  places them in `work/repos/<name>`, beside `work/repo` and never inside it
  (§4.4).
* **Only `work/repo` is ever harvested, pushed or checkpointed.**
  `collect`, `direct-pr` and `integrate` are unchanged because they already
  read only `work/repo`. This design keeps everything else out of it (§4.5).
* **The extra checkouts live on the GKE cache volume, not in the
  checkpoint.** A resumed attempt clones them again at the same signed
  commits (§4.6).

---

## 1. The problem, as measured

### 1.1 What a step can read today

**The repository is workflow-level.** `WorkflowCreate` carries one
`repository_url` / `repository_ref`. `WorkflowStepCreate` has no repository
field and forbids extra keys (`apps/swarm-api/swarm_api/schemas.py::WorkflowStepCreate`).
`SubmissionService.submit_workflow` passes the same pair to every step's
`_build_task` (`apps/swarm-api/swarm_api/service.py::SubmissionService.submit_workflow`).
The stored `Workflow` and `WorkflowStep` have no repository field at all;
it exists only on `Task` (`apps/common/swarm_common/models.py::Task`,
`repository_url`, `repository_ref`).

**The worker clones exactly one repository** into `work/repo`
(`apps/agent-worker/agent_worker/workspace.py::REPO_DIR_NAME`), in
`Worker._maybe_clone`
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._maybe_clone`), wrapped
by `Worker._clone_keeping_lease` so the lease is heartbeated meanwhile.

**The one cross-repository read is a single file.** The runner git tool
`read_private` returns one file (at most 1 MiB,
`apps/swarm-api/swarm_api/gittools.py::MAX_FILE_BYTES`) or one directory
listing from a repository other than the task's own. It works only when that
repository's registration says `readable_by_tasks: true`
(`apps/swarm-api/swarm_api/repositories.py::readable_by_tasks_of`;
[runner-git-tools.md §2.3](../runner-git-tools.md#23-the-private-read-allowlist)).
It is enough to read a pinned API spec. It is not enough to grep a codebase,
follow a call through it, or answer "which service owns the endpoint the web
app calls?".

### 1.2 What people do instead (#1434)

#1434's measured workaround: one indexer task per repository, each writing a
summary artifact, then a step that stages every summary through
`input_from`. The cross-repository question is answered from summaries, not
code. For a product in four repositories (API, web app, infrastructure,
SDK), that is **five tasks, four of them read-only, for every question**.
Each costs a lease, a pod start (about 23 s on GKE for claude-code, contract
request 53) and a clone. The answer can only be as good as what each summary
happened to keep.

### 1.3 The numbers the design is sized from

| quantity | value | where it was read |
|---|---|---|
| forge clone, GKE, 7 days to 2026-10-11 | n = 293, p50 3.19 s, p90 10.56 s, max 59.97 s | [`docs/clone-bundles.md`](../clone-bundles.md) §1 |
| clone timeout per repository | 300 s | `apps/agent-worker/agent_worker/config.py::WorkerConfig` (`git_clone_timeout_seconds`) |
| GKE workspace emptyDir, class `standard` | 5 GiB workspace + 3 GiB `/tmp` + 2 GiB HOME (Autopilot's 10 GiB cap) | `apps/scheduler/scheduler/dispatch.py::GKE_DISK`, `AUTOPILOT_MAX_EPHEMERAL_GIB` |
| GKE cache volume per pod | 20 GiB pd-balanced PVC at `/cache`, created and deleted with the pod, never checkpointed | `apps/scheduler/scheduler/dispatch.py::CACHE_VOLUME_GIB`, `CACHE_MOUNT`; `apps/agent-worker/agent_worker/cachedirs.py` |
| Cloud Run workspace, class `standard` | 4 GiB memory-backed tmpfs, charged against the 8 GiB memory limit | `apps/common/swarm_common/profiles.py::RESOURCE_CLASSES`; `dispatch.py` workspace sizing |
| checkpoint ceiling | 2 GiB per archive, chain of 24 | `apps/agent-worker/agent_worker/config.py::WorkerConfig` (`max_checkpoint_bytes`); `checkpoint.py` |

**#1434 says "4 GiB today for claude-code". That figure is the Cloud Run
tmpfs.** claude-code has run on GKE since contract request 53. Its
workspace is a 5 GiB disk emptyDir, and an overflow there is an eviction,
not an ENOSPC (`dispatch.py::GKE_DISK`'s comment; evictions on 2026-10-08
and 2026-10-15). The volume with room is the 20 GiB cache PVC. That is why
§4.6 puts the extra checkouts there and needs no new resource class.

### 1.4 What "done" lets someone do

Submit one step, or one workflow, that names its primary repository and up
to four registered repositories beside it. The agent can grep, read, run a
language server over, and (phase 2) query the code graph of all of them at
known commits. Then it opens one PR against the primary. #1434's three
acceptance checks become three red-first tests (§6):

1. a step with two additional repositories sees all three checkouts at their
   pinned commits;
2. a change made inside an additional checkout is not in the collected patch;
3. a repository the tenant has not registered is refused at submission.

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**1. Nothing costs while waiting.** Nothing new is created before a lease.
swarm-api's resolution of refs and sizes happens in the submission request,
before the task exists. The clones happen inside the leased attempt, in the
same `_prepare` step as the primary clone. A QUEUED, PARKED or READY task
holds a list of names and commits, nothing else. There are no pending pods
and no pre-warmed clones.

**2. All-or-nothing reservation.** Untouched. Additional repositories add no
pool and no resource-class change (§4.6), so admission reserves exactly what
it reserves today, in one transaction. A design that needed a bigger class
for multi-repository steps would still go through the same
`acquire_lease_in_transaction`. This one does not need it.

**3. Concurrency counts from LEASED.** Untouched: the clones run after the
lease, as the primary clone does, and are counted from LEASED.

**4. Never sleep through a long wait.** A clone is network work bounded by
`git_clone_timeout_seconds`, not a provider wait. It runs under
`Worker._heartbeat_meanwhile` exactly as the primary clone does (`_clone_keeping_lease`).
The extra clones run in parallel, at most three at once. A failure is a
typed error, never a sleep-and-retry beyond `gitops.retry_clone`'s existing
10 s and 30 s.

**5. Fencing.** The list is in the signed spec (format 4, request B). The
worker checks the signature and its generation before `_prepare` reaches the
clone step. A stale worker therefore exits before cloning anything, primary
or additional. A rewritten list (a tenant identity editing the task
document to add a repository, or to move a commit) fails the signature, and
the attempt is refused `SPEC_SIGNATURE_INVALID` before the agent runs.

**8. Checkpoints.** Mandatory checkpointing is unchanged, and checkpoints do
not grow. The extra checkouts are read-only and fully determined by signed
commits, so they are re-materialised, not restored. `work/repos` is excluded
from the archive (§4.6). That keeps a four-repository step under the same
2 GiB archive ceiling as a one-repository step. It also means a resume never
restores a checkout an agent tampered with.

**9. Tenant isolation.** Every additional repository must be **the
submitting tenant's own registration** (`repositories/{repo_id}` with
`repo_id = repo_id_for(tenant, owner, repo)`,
`apps/swarm-api/swarm_api/repositories.py::repo_id_for`). The id is a hash
over the tenant, so another tenant's registration cannot even be named. The
credential is one of the task's own tenant's forge slots
(`swarm-tenant-<tenant>-git` or `-git-u-<hex>`), chosen by swarm-api and
signed. The clone lands in the tenant's own pod. Any graph snapshot comes
from `tenants/<tenant>/repos/<repo_id>/graph/...`. Nothing crosses a tenant.

**10. Callers pick a profile by name.** An additional repository is data,
like `repository_url`: it names *what to read*, never what runs or how.
The caller sends `owner/repo` and a ref, never a URL, a host, a remote, a
refspec, a credential, a path, a clone depth, a disk size or a backend.
swarm-api turns the name into the registered `https://github.com/<o>/<r>`
URL itself. The mount name is checked against a fixed pattern, and the
worker owns the paths. Whether a profile may take additional repositories at
all is the platform's rule (§4.3), not the caller's.

Invariants 6 (no Spot) and 7 (requests == limits) are untouched: no pod
shape changes.

### 2.2 What else binds it

* **A forge token is never in a URL, a log, the repository or a Job
  environment** (CLAUDE.md, owner rule 2026-09-25). The extra clones reuse
  `gitops._write_credentials`: a 0600 file under `private/`, deleted in
  `finally`, never the agent's to see, sent only to a host
  `forge.may_receive_forge_token` accepts
  (`apps/agent-worker/agent_worker/forge.py::may_receive_forge_token`).
* **The worker cannot read a repository (`git-r-`) slot.** Those are read by
  swarm-api only, for the runner git tools it executes (#1225,
  `terraform/bootstrap/forge_user_slots.tf`'s `enable_forge_user_slots`
  description). So a per-repository credential for an extra clone is the
  tenant slot `git` or a user slot `git-u-<hex>`, never `git-r-`.
  `read_private` can use `git-r-` only because swarm-api makes that call.
* **A per-person grant is per repository.** A person-submitted task is
  admitted only when `forge_grants/{tenant}__{user}__{repo_id}` names a mode
  (`SubmissionService._grant_mode`, `REPOSITORY_NOT_GRANTED` when
  `repository_grants_enforced`). The worker re-reads that grant before the
  clone (`apps/agent-worker/agent_worker/secrets.py::grant_refusal`). Each
  additional repository needs its own grant, of either mode, checked at
  submission and again before its clone.
* **The harvest is already scoped to `work/repo`.** `Worker._harvest_git`
  works only on `work/repo/.git`. `gitops.mirror_worktree` copies
  `work/repo`'s working tree into the clean publish repository and skips only
  *nested `.git` directories*, not nested checkouts' files. **A checkout
  placed inside `work/repo` would therefore be committed into the
  primary's patch as plain files.** That is why the extras go in
  `work/repos/`, a sibling, and not in `./repos/<name>` under the agent's
  working directory as #1434 sketches. The agent reaches them as
  `../repos/<name>`.
* **A task with no repository uploads `work/`** as its standalone output
  (`Worker._uploads_working_folder`, `standalone_outputs.scan`). Extra
  checkouts must be excluded from that upload, or a read-only analysis task
  would publish four codebases as its "outputs".

### 2.3 What it needs under `apps/common/swarm_common/`

Two changes, both requested in §5 rather than made:

* **A** `models.py`: a new `Task.additional_repositories` field and its
  shape check;
* **B** `specsign.py`: spec format 4, covering that field.

**C** records two non-changes so no lane makes them: `WorkflowStep` and
`Workflow` gain no field, and `profiles.py` gains no resource class or
profile flag.

---

## 3. Designs that could work

All three share §4.1's naming and §4.2's gate. They differ in **how the
code reaches the pod**.

### 3.1 Design A: no checkout; widen `read_private` into a browse-and-search tool

Add `list_tree` and `search` beside `read_private`. swarm-api answers them
from the forge API (`git/trees?recursive=1`, code search) under the same
allowlist, with the repository's own `git-r-` slot or the task's.

* **Costs.** Code: swarm-api `gittools.py`, the worker's `gitmcp`, plugin
  docs. No contract change. No migration. Latency: one forge round trip per
  call, 100-500 ms, paid on every read rather than once. Money: nothing
  measurable on GCP. GitHub's REST budget (5,000 requests an hour per token)
  becomes the ceiling for an agent that reads a lot. Code search covers only
  default branches, so it cannot honour a pinned commit.
* **What it breaks.** Nothing, and it does not deliver #1434. An agent cannot
  run `rg`, a type checker or a language server over a forge API.
  "Sees all three checkouts at their pinned commits" is false by
  construction. The code graph cannot be staged for a repository that is
  not on disk (`graphstage.prove_ancestry` runs `git merge-base` in the
  checkout).

### 3.2 Design B: the worker clones each one, with a per-repository slot chosen by swarm-api (recommended)

swarm-api validates each name against the tenant's registrations, picks the
forge slot for each repository the same way it picks the primary's
(`SubmissionService._resolve_forge`), resolves each ref to a commit with
that slot, and signs the lot into the task. The worker clones each at its
commit with `gitops.clone_at_commit` into the cache volume and links it in
as `work/repos/<name>`.

* **Costs.** Code: one contract field and one spec format. One new module
  each in swarm-api and the worker, plus edits to known call sites (§4.8).
  Migration: none. An absent field is today's task, and swarm-api signs at
  format 4 only when the list is non-empty, so a worker built before format
  4 keeps verifying every task that does not use the feature. Latency: two
  forge GETs per repository at submission (~0.3-1 s total, in the
  submission request). In the attempt, the extra clones run in parallel
  beside the existing clone step. On the measured distribution that adds
  about the p90 of the slowest clone, **about 10 s per attempt**, nearer
  3 s for small repositories. Money: about 10 s more of a 4 vCPU / 8 GiB
  Autopilot pod per attempt, roughly $0.0006 at us-central1 list price
  (an estimate, to be checked against billing after rollout). The cache PVC
  is already provisioned at 20 GiB for every GKE pod, so no new disk.
* **What it breaks.** A token is in the pod during the extra clones, in
  `private/`, and is removed before the agent starts. That is the exposure
  the primary clone already has, with the same slot kinds. A repository
  whose only usable credential is a `git-r-` slot cannot be cloned this way.
  It is refused at submission `additional_repository_unreadable`, because
  swarm-api's resolution uses the same slot the worker will. That is a
  real limit for an organisation onboarded only through repository tokens
  (§7 Q2).

### 3.3 Design C: swarm-api fetches a depth-1 bundle per (repository, commit); the worker unbundles

The worker asks swarm-api, on an attempt-key-signed, generation-fenced
route, for a bundle of each additional repository at its signed commit.
swarm-api runs `gitexec` (the `git_fetch` machinery,
`apps/swarm-api/swarm_api/gitexec.py`) with whichever slot can read the
repository, `git-r-` included. It caches the bundle at
`tenants/<t>/repos/<repo_id>/snapshots/<commit>.bundle`, and the worker
downloads and unbundles it.

* **Costs.** Code: B's, plus a new swarm-api route and service, a GCS
  prefix and its lifecycle rule (Track C), and the worker's unbundle path.
  Latency: a cache miss is a forge fetch in swarm-api, then a GCS upload,
  then a GCS download: slower than B's direct clone, which is the
  measurement that retired clone bundles (`docs/clone-bundles.md`: bundle
  plus delta p50 4.69 s against a plain clone's 3.19 s). A hit is one GCS
  download and saves the forge round trip, which helps a ten-step workflow
  reading the same four commits. Capacity: `gitexec` runs at most
  `MAX_CONCURRENT_CALLS = 2` git calls per swarm-api process, with
  `MAX_BUNDLE_BYTES` 256 MiB and `MAX_GIT_MEMORY_BYTES` 512 MiB. A wave of
  multi-repository steps would queue behind each other and behind every
  agent's `git_fetch`, or swarm-api would need its own git worker pool.
  Money: GCS storage for cached bundles under a 7-day lifecycle (small),
  plus swarm-api CPU and memory.
* **What it breaks.** Nothing for correctness. It is the strongest posture:
  no forge token for an additional repository ever enters the tenant's
  pod, and `git-r-`-only repositories work. But it moves a bulk data path
  into the control plane, which the runner-git-tools design kept to small
  per-call bundles on purpose. And a repository over 256 MiB as a depth-1
  bundle cannot be served at all.

### 3.4 Recommendation: B, with C kept as a phase-3 option

B delivers all three of #1434's checks with the least new machinery. Every
piece it uses already exists and is tested on the primary path:
`clone_at_commit`, the credentials file, `grant_refusal`, the cache volume
and the spec signature. Its security exposure is the one the platform
already accepts for the primary clone, with the same slot kinds, for the
same short window before the agent starts. Its one real gap, `git-r-`-only
repositories, is refused loudly at submission, never at 3 a.m. in a pod.
If the owner wants that gap closed (§7 Q2), C can be added later behind
the same task field, because the field records *what* to check out, not
*how*. A is not a design for this issue. It remains a good addition for
"read one file at another commit", which `read_private` already covers.

---

## 4. The recommended design in detail

### 4.1 API shape

**Request.** One new optional field on three schemas in
`apps/swarm-api/swarm_api/schemas.py`:

```jsonc
// TaskCreate, WorkflowCreate (every step's default), WorkflowStepCreate (replaces it)
"additional_repositories": [
  {
    "repository": "acme/web",   // owner/repo, registered by this tenant. Never a URL.
    "ref": "main",              // optional: branch, tag or full 40-hex sha.
                                // Default: the registration's default_branch.
    "name": "web"               // optional: the directory under ../repos/.
                                // Default: the repo part, lower-cased.
  }
]
```

* A new `AdditionalRepositoryCreate(StrictModel)` with `extra="forbid"`.
  `repository` is checked by the existing `repositories.parse_repository`.
  `ref` is checked by `validation.check_repository_ref`, which already
  refuses an abbreviated sha and any URL-ish or option-ish string. `name`
  must match `ADDITIONAL_REPOSITORY_NAME` (request A): lower-case
  `[a-z0-9][a-z0-9._-]{0,62}`, not `.` or `..`.
* **Step vs workflow.** `WorkflowCreate.additional_repositories` applies to
  every step. A step's own list **replaces** it, and `[]` means none
  (§7 Q6). A replace rule, not a merge, keeps "what does this step read?"
  answerable from one place.
* **Refusals**, each a typed 4xx in swarm-api's error envelope, before any
  task is written:

| code | status | when |
|---|---|---|
| `repository_not_registered` | 422 | `repo_id_for(tenant, owner, repo)` has no registration (the code schedules already use, `apps/swarm-api/swarm_api/schedules.py`) |
| `repository_not_readable_by_tasks` | 403 | registered, but `readable_by_tasks` is not `true` (§7 Q1) |
| `REPOSITORY_NOT_GRANTED` | 403 | the existing per-person grant check, run per additional repository |
| `additional_repository_unreadable` | 422 | the slot swarm-api picked cannot read it, or the ref names no commit. One sentence for both, as `read_private`'s `repo_not_allowlisted` |
| `additional_repository_duplicate` | 422 | two entries name one repository or one `name`, or an entry names the primary |
| `too_many_additional_repositories` | 422 | more than `MAX_ADDITIONAL_REPOSITORIES` (4, §7 Q5) |
| `additional_repositories_too_large` | 422 | the sum of the forge's reported sizes is over the budget (6 GiB, §7 Q5) |
| `additional_repositories_unsupported_profile` | 422 | the profile is not one that takes them (§4.3) |

**Response.** `codec.task_to_api` (`apps/swarm-api/swarm_api/codec.py::task_to_api`)
adds:

```jsonc
"additional_repositories": [
  {"name": "web", "repository": "acme/web", "repo_id": "repo_<16 hex>",
   "ref": "main", "commit": "<40 hex>", "path": "../repos/web",
   "forge_credential_source": "person"}   // tenant | owner | person, never a value
]
```

The URL is not echoed: it is derivable from `repository`, and
`task_to_api` already masks the primary's URL for a reason. The workflow
response (`codec.workflow_to_api`) has no repository today and gains none.
Its `tasks[]` carry the field.

### 4.2 Submission: resolve, gate, pin, sign

A new module, `apps/swarm-api/swarm_api/extrarepos.py`, called by
`SubmissionService._build_task` once per task, after `_resolve_forge`
(which it mirrors) and before `_sign`:

1. **Gate.** Read `repositories/{repo_id}` through `Repositories.find`
   (`apps/swarm-api/swarm_api/repositories.py::Repositories.find`). It must
   exist, belong to the tenant, not be `archived`, and satisfy
   `readable_by_tasks_of` (§7 Q1).
2. **Grant and slot.** For a person submitter, `_grant_mode` on that
   `repo_id` must name a mode (either mode: the clone only reads), and the
   slot is `_person_slot(tenant, email, owner)` for *that repository's*
   owner. It may differ from the primary's slot when the owners differ.
   For a service submitter, `forge.service_slot`. Never `git-r-` (§2.2).
3. **Pin.** With that slot's token, one GET of
   `commits?sha=<ref>&per_page=1`, exactly as `GitTools._read_private`
   resolves a ref (`apps/swarm-api/swarm_api/gittools.py::GitTools._read_private`),
   gives the 40-hex `commit`. Because swarm-api uses the slot the worker will
   use, a ref that resolves here can be cloned there.
4. **Size.** One GET of `repos/<o>/<r>` gives the forge's `size` (KiB of
   repository data). The sum over the list must be under the budget. The
   number is an estimate, so the worker enforces the real figure (§4.4).
5. **Write.** The entries go on the task as §4.7's shape. The task is signed
   at format 4 (request B).

**Pinning, compared with the primary.** #1434 asks that pinning "work as
for the primary". Read on 2026-10-17, the primary is **not** resolved at
submission by swarm-api: `check_repository_ref` only refuses a short sha.
The pins that exist are the MCP bridge's `infer`
(`apps/swarm-mcp/swarm_mcp/checkout.py::resolve`, the pushed branch's
commit), `merge_pr.head_sha`, index runs, and the worker's workflow base pin
(`Worker._upstream_base_pin`, all parents' recorded base). So this design
is *stronger* than the primary on purpose. An additional repository has no
upstream step to pin it, and a reader that sees `web@main` at one commit in
step 1 and another in step 3 answers a different question each time.
Making the primary resolve at submission too is a separate change. It
would alter `carrier`, `builds_on` and continuation semantics, and is not
proposed here.

**When the list is shared by a workflow,** the resolution runs once per
`(repository, ref)` per submission, so every step of one workflow gets the
same commit even if a branch moves mid-submission.

### 4.3 Which profiles take additional repositories

Only profiles whose backend is GKE Autopilot, because §4.6 needs the cache
volume, and only agent profiles: not `worker_action` profiles (`merge`,
`post-verdict`) and not `indexer`. The check is in `extrarepos.py`,
from `swarm_common.profiles.resolve_backend` and the profile's
`worker_action` flag. It is not a caller input, and it is not a contract
change. Read from `RUNNER_PROFILES` on 2026-10-17, that admits `generic`,
`claude-code`, `codex`, `browser` and `claude-code-browser` (GKE), and
refuses `mock` and `claude-code-review` (Cloud Run), `merge` and
`post-verdict` (worker actions) and `indexer`. (CONTRACT.md's "Platform
decisions" still says `generic` and `codex` run on Cloud Run Jobs; the
catalogue says GKE, and the catalogue is what this check reads.) §7 Q4
asks whether Cloud Run should take them from its memory tmpfs instead.

### 4.4 The worker: clone, link, mark read-only

A new module, `apps/agent-worker/agent_worker/extrarepos.py`, called from
`Worker._prepare` right after the primary clone, inside the same
`_heartbeat_meanwhile` scope (`Worker._clone_keeping_lease`):

1. For each signed entry, re-check the person grant with
   `secrets.grant_refusal(..., write=False)` for that `repo_id`. A revoked
   grant fails the attempt `additional_repository_not_granted`, which is
   terminal: retrying will not restore a grant.
2. Read that entry's slot with `secrets.resolve_git_token(tenant, client,
   provider=<entry.forge_credential>)`, read fresh per clone as for the
   primary.
3. `gitops.clone_at_commit(url=<entry.repository_url>, sha=<entry.commit>, ...)`
   into `<SWARM_CACHE_ROOT>/<attempt_id>/repos/<name>`. Depth 1, the
   credentials file in `private/` deleted in `finally`, and HEAD verified
   equal to the commit (`clone_at_commit` already does all three). At most
   three clones run at once. Each is timed with the existing `clone_timed`
   mark, tagged with the entry's `name`.
4. Measure the checkout (`du`, the same walk `cachedirs` sizing uses). If the
   running total passes the budget, fail the attempt
   `additional_repositories_too_large`. That is terminal: the forge's size
   was an underestimate, and the next attempt would hit the same wall.
5. Drop the remote's push URL (`remote.origin.pushurl` set to an invalid
   value) and remove write permission on the tree (`chmod -R a-w`). **This
   is a signal, not a boundary.** The agent runs as the same uid and can
   chmod it back (workspace-secrets.md §3.5 says the same of files). The
   boundary is §4.5.
6. Create `work/repos/` as a real directory holding one **symlink per
   entry** to its cache checkout. Write
   `work/.swarm-context/repositories.json` with
   `[{name, repository, ref, commit, path}]`. Export `SWARM_REPOS_DIR`
   through `Worker._build_child_env`, so the agent learns the layout from
   the platform rather than by guessing.

**Failure modes.** A clone that fails after `retry_clone`'s retries fails
the attempt with `clone_failed`, naming the entry's `name` and never a
URL, so the ordinary retry policy applies. The agent never starts with a
partial set. A step that asked for four repositories and silently got
three would answer with false confidence.

### 4.5 What stays write-only to the primary

Nothing in the publish path changes. What changes is a set of tests that
hold the facts it depends on:

* `Worker._harvest_git` reads `work/repo` only, and `work/repos/` is not
  under it, so `collect`'s `swarm-work.patch`, `direct-pr`'s push and
  `integrate`'s merge carry nothing from an additional checkout. Red-first
  test: edit a file in `work/repos/web`, harvest, assert the patch is
  byte-identical to the one without the edit.
* The runner git tools refuse any repository but the task's own
  (`other_repository`, [runner-git-tools.md](../runner-git-tools.md)), and
  `read_private` is read-only. No push credential for an additional
  repository is ever issued.
* `publish_scan.py` and `prepublish.py` diff `work/repo` only and are
  unchanged.
* **Standalone output.** `Worker._uploads_working_folder` uploads `work/`
  for a task with no repository. `standalone_outputs.scan` gains `repos`
  among its reserved names, so it never walks the links.
* **A task with no primary repository** may still name additional
  repositories (§7 Q8). It harvests nothing from them, by the same rule.

### 4.6 Disk, cache volume and checkpoints

* **Where.** The cache volume (`/cache/<attempt_id>/`), not the 5 GiB
  workspace emptyDir. The PVC is not ephemeral-storage, so it does not count
  against Autopilot's 10 GiB cap, and filling it is an ENOSPC the worker
  reports, not a pod eviction. The tool caches (uv, npm, pip, TF plugins)
  share it, so the extra checkouts get a budget of 6 GiB of the 20
  (§7 Q5), leaving 14 GiB for caches and `TMPDIR`.
* **Checkpoints.** `checkpoint.py` gains `EXTRA_REPOS_DIR = "repos"` as a
  top-level exclusion beside `TOOL_CACHES`. The walk already refuses to
  descend into a link below the root
  (`tests/unit/worker/test_checkpoint_refuses_a_linked_work.py`), and the
  restore already removes a restored link that resolves outside the
  destination, so the exclusion is belt and braces. It is still required:
  without it, every checkpoint would carry the link entries and every
  restore would remove them, which is noise.
* **Resume.** A resumed attempt has a new attempt id, hence a new empty
  cache directory. `extrarepos.py` runs again and clones the same signed
  commits. A resume therefore costs the clones again (about 10 s), and in
  exchange a checkpoint never carries four codebases and never restores
  one an agent edited.
* **No cross-attempt git cache.** The worker has none today (cachedirs
  empties the cache per attempt, and clone bundles were retired). This
  design does not add one. Design C's snapshot cache is where one would go.

### 4.7 Firestore document shape

On `tasks/{task_id}`, a new field written once by swarm-api in the creating
write, covered by the spec signature, never changed after:

```jsonc
"additional_repositories": [
  {
    "name": "web",                                   // ADDITIONAL_REPOSITORY_NAME
    "repo_id": "repo_<16 hex>",                      // the registration
    "repository": "acme/web",
    "repository_url": "https://github.com/acme/web", // from the registration, never a caller
    "ref": "main",                                   // as asked, for display
    "commit": "<40 hex>",                            // what the worker checks out
    "forge_credential": <slot suffix>,               // git, or git-u- and 16 hex; never git-r-
    "forge_credential_source": "person",             // tenant | owner | person
    "size_kib": 48213                                // the forge's estimate at submission
  }
]
```

`None` (absent) is today's task. There is no new collection and no new
index. `repositories/{repo_id}` is read, never written. A `git_tool_audit`
row is not written for the clone, as it is not for the primary. The
attempt's `clone_timed` marks record each one.

### 4.8 Changes by component, and the files they land in

| component | file | change |
|---|---|---|
| contract | `apps/common/swarm_common/models.py` | request A |
| contract | `apps/common/swarm_common/specsign.py` | request B |
| swarm-api | `apps/swarm-api/swarm_api/schemas.py` | `AdditionalRepositoryCreate`; the field on `TaskCreate`, `WorkflowCreate`, `WorkflowStepCreate` |
| swarm-api | `apps/swarm-api/swarm_api/extrarepos.py` (new) | §4.2 and §4.3: gate, grant, slot, pin, size, profile check |
| swarm-api | `apps/swarm-api/swarm_api/service.py` | `_build_task` and `submit_workflow` call it; the step-replaces-workflow rule |
| swarm-api | `apps/swarm-api/swarm_api/validation.py` | the refusal codes and builders of §4.1 |
| swarm-api | `apps/swarm-api/swarm_api/codec.py` | `task_to_api` adds §4.1's response list |
| worker | `apps/agent-worker/agent_worker/extrarepos.py` (new) | §4.4 |
| worker | `apps/agent-worker/agent_worker/lifecycle.py` | `_prepare` calls it after the primary clone; `_build_child_env` exports `SWARM_REPOS_DIR` |
| worker | `apps/agent-worker/agent_worker/workspace.py` | `REPOS_DIR_NAME = "repos"` beside `REPO_DIR_NAME` |
| worker | `apps/agent-worker/agent_worker/checkpoint.py` | the `repos` exclusion |
| worker | `apps/agent-worker/agent_worker/standalone_outputs.py` | `repos` reserved in `scan` |
| MCP bridge | `apps/swarm-mcp/swarm_mcp/server.py`, `client.py`, `workflows.py` | `additional_repositories` on the dispatch and workflow tools; `infer` does not apply to them |
| plugin | `plugin/skills/delegate/SKILL.md` | when to name additional repositories, and that the agent reads them at `../repos/<name>` |
| UI | `apps/swarm-ui/src/Dispatch.tsx`, `Submit.tsx`, `SubmitWorkflow.tsx`, `types.ts` | a picker over `GET /v1/repositories/readable` (it exists) and a ref box per row |
| UI | `apps/swarm-ui/src/AgentDetail.tsx` | the list, each `name · owner/repo @ ref (commit[:12])` |
| graph (phase 2) | `apps/agent-worker/agent_worker/graphstage.py`, `graph_ticket.py`, `graphmcp/shim.py`, `graphmcp/tools.py`; `apps/swarm-api/swarm_api/graph_ticket.py` | §4.9 |
| docs | `docs/workflows.md`, `docs/runner-git-tools.md`, this document | what was built |

The scheduler, reconciler, admission, dispatch manifests and Terraform are
**unchanged**. That is how invariants 1-3, 6 and 7 are preserved, not
merely asserted.

### 4.9 Indexing: the code graph for additional repositories (phase 2)

Today the graph is staged for one repository. `graphstage.prepare` stages
`work/.swarm-context/snapshot/` for the task's `repository_url`, choosing an
index version proven an ancestor of the clone base
(`apps/agent-worker/agent_worker/graphstage.py::prove_ancestry`). The ticket
route resolves the repository from the task too
(`apps/swarm-api/swarm_api/graph_ticket.py::GraphTicketService.mint`). The
graph service's claims already carry `repo_id`, so **swarm-graph needs no
change**.

Phase 2:

* `GraphTicketRequest` gains an optional `repo_id`. `mint` accepts it only
  when it is the task's primary or one of its **signed**
  `additional_repositories`. Anything else gets the same refusal as an
  unregistered repository.
* `graphstage.prepare` loops over the entries. For each registration with an
  index, it stages `work/.swarm-context/repos/<name>/snapshot/` from a
  version proven an ancestor of the entry's `commit` (the proof runs in the
  extra checkout), and writes `private/graph-ticket-<name>`.
* The graph MCP shim takes an optional `repo` argument (a `name` from
  `repositories.json`, default the primary) and picks that ticket file. One
  MCP server, not one per repository.
* **No new index triggers.** An additional repository has a graph only if
  its registration indexes it (`index`, `context`). A step over an
  unindexed one still has its checkout and `rg`. `repositories.json` says
  `graph: false` for it, so the agent is not left guessing (§7 Q7).

---

## 5. Frozen contract: change requests

Written in the shape of `docs/contract-change-requests.md` (entries 80-84).
The operator numbers them and files them there once approved. Until then,
nothing under `apps/common/swarm_common/` changes.

### Request A. `models.py`: `Task.additional_repositories`, the repositories a step reads beside its own, and their shape (MULTI-REPO, part of #1434)

**Status:** PROPOSED 2026-10-17 by lane MULTI-REPO-DESIGN. Not applied.

#### Why

A step that reasons over a product spread across repositories needs to
check them out at commits fixed at submission (§1). The task is the only
document the worker reads its spec from, and the signed spec is what makes
a rewritten list fail (invariant 5). A free `input` or `metadata` key would
be signed (`input` is in every format) but untyped. Every reader would then
re-validate a caller-shaped dict. The worker would have to trust that
swarm-api, not a caller, wrote `forge_credential` and `commit` inside it.
`metadata` keys outside `SIGNED_METADATA_KEYS` are not signed at all. The
fields must be written by swarm-api only, as `forge_credential` is (request
54).

#### The change

```python
#: Contract request <n> (MULTI-REPO, docs/design/multi-repository-steps.md §4.7).
#: The directory name an additional repository is checked out under,
#: `work/repos/<name>`. Lower-case, no separator, never "." or "..".
ADDITIONAL_REPOSITORY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")

#: The most additional repositories one task may read (§7 Q5).
MAX_ADDITIONAL_REPOSITORIES = 4

#: The forge slot an additional repository is cloned with: the tenant slot or
#: a user slot. Never a repository (git-r-) slot, which the worker cannot read.
ADDITIONAL_FORGE_CREDENTIAL = re.compile(r"git(-u-[0-9a-f]{16})?")

#: The keys of one entry, all required.
ADDITIONAL_REPOSITORY_KEYS = (
    "name", "repo_id", "repository", "repository_url", "ref", "commit",
    "forge_credential", "forge_credential_source", "size_kib",
)


def check_additional_repositories(value: Any) -> None:
    """Raise ValueError unless `value` is None or a list of at most
    MAX_ADDITIONAL_REPOSITORIES entries of exactly ADDITIONAL_REPOSITORY_KEYS:
    a name matching ADDITIONAL_REPOSITORY_NAME, unique in the list and not
    "." or ".."; repo_id matching ^repo_[0-9a-f]{16}$, unique; commit a
    40-hex sha; repository_url "https://github.com/" + repository;
    forge_credential matching ADDITIONAL_FORGE_CREDENTIAL; size_kib a
    non-negative int (not a bool). Its message names the key, never a value."""


@dataclass
class Task:
    ...
    #: The registered repositories this task reads beside its own, each checked
    #: out read-only at `commit` in `work/repos/<name>` and never harvested.
    #: WRITTEN BY swarm-api ONLY, in the creating write, from its resolution of
    #: the caller's `owner/repo` names against the tenant's registrations;
    #: never accepted from a caller. Covered by the spec signature (format 4).
    #: None is today's behaviour, and every document written before it decodes
    #: exactly as it did.
    additional_repositories: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        ...  # the existing forge checks, then:
        check_additional_repositories(self.additional_repositories)
```

`to_firestore` needs no change: `asdict` already writes the list of dicts.

#### Rollback

Remove the field and the four names. A document that carries the field
then fails to decode wherever `Task(**doc)` is used, so a rollback must first
stop swarm-api writing it (the build lanes gate the write on
`settings.additional_repositories_enabled`, default off) and wait for those
tasks to end.

#### What it would break

Nothing that exists. `Task(**doc)` on an old document leaves it None.
`__post_init__` refuses a malformed list, which no writer produces. A
reader that iterates task fields generically (the codec, the reconciler's
raw transaction reads) sees one more key.

#### Invariants

**5**: the field is inside the signature (request B). **9**: `repo_id` is
a tenant-scoped hash, and the slot is the task's own tenant's. **10**: the
field holds names and commits, never an image, command, resource or
backend parameter, and a caller cannot write it. 1-4 and 6-8 untouched.

### Request B. `specsign.py`: spec format 4 covers `additional_repositories` (MULTI-REPO, part of #1434)

**Status:** PROPOSED 2026-10-17 by lane MULTI-REPO-DESIGN. Not applied.

#### Why

A list that is not in the signed bytes can be rewritten by any identity
that can write the task document, the tenant's worker account included.
Repointing a commit, or adding a repository the tenant registered but this
step was never given, would then go unnoticed. Request 54 had the same
reason for format 3.

#### The change

```python
#: 4 is contract request <n> (MULTI-REPO, docs/design/multi-repository-steps.md):
#: format 3's fields plus `additional_repositories`.
SPEC_FORMAT = 4
SPEC_FORMATS = (1, 2, 3, 4)
_FORMAT_4_FIELDS = ("additional_repositories",)
```

* `canonical_step_spec`: at `spec_format < 4`, a set (non-None, non-empty)
  `additional_repositories` raises `SpecNotCanonical`, exactly as a format-3
  field does below 3. At 4, the list is projected as stored. The entries are
  in submission order, and order is part of the bytes.
* `signing_format`: the oldest format covering every set field. A task
  without the list keeps signing at 3 (or lower), so a worker built before
  format 4 verifies every task that does not use the feature. **Rollout
  order: workers first, then swarm-api**, as for formats 2 and 3.

#### Rollback

Return `SPEC_FORMAT` to 3. A document already signed at 4 then fails
verification (`SPEC_SIGNATURE_INVALID`) on a worker without format 4, so
stop the writer first (request A's flag) and let those tasks end.

#### What it would break

Nothing signed today. `tests/unit/common/test_specsign_covers.py` must learn
the new field. It is the guard that every signed field is covered.

#### Invariants

**5**: a rewritten list fails the worker's check before the agent runs.
Others untouched.

### Request C. No change to `WorkflowStep`, `Workflow` or `profiles.py` (recorded so no lane makes one)

**Status:** PROPOSED 2026-10-17 with A and B. It requests **no change**.

* **`WorkflowStep` / `Workflow` gain no field.** The primary repository is
  not on them either. The task is where a step's repository lives, and the
  submission schemas carry the per-step list to it (§4.1). Adding it to
  both would give the step two sources of truth.
* **No new resource class, no profile flag.** §4.6 puts the checkouts on the
  existing cache PVC, and §4.3 derives "takes additional repositories" from
  the profile's backend and `worker_action`, which the catalogue already
  states. If the owner answers §7 Q4 with Cloud Run support, *that* answer
  needs a request: a `ResourceClass` sized for it, or a profile
  declaration. This document does not pre-file it.

---

## 6. Build plan

Lanes are split by territory: no two lanes edit one file. Each pushes its
tests alone first (red), then the fix to `<branch>-fix` (CLAUDE.md, "Red
first, in this shape"). Each lane's last command is
`scripts/changed-guards.sh`.

| # | lane | files (its whole territory) | red-first tests | after |
|---|---|---|---|---|
| 0 | **MR-CONTRACT** (operator applies A and B once approved) | `apps/common/swarm_common/models.py`, `apps/common/swarm_common/specsign.py`, `docs/contract-change-requests.md`, `tests/unit/common/test_additional_repositories_contract.py` (new), `tests/unit/common/test_specsign.py`, `tests/unit/common/test_specsign_covers.py` | a malformed entry (`name` "..", a `git-r-` slot, a short commit, a duplicate name, five entries) raises; an old document decodes with None; a format-3 projection refuses a set list; a rewritten commit fails a format-4 verification; a task without the list signs at 3 | owner approval |
| 1 | **MR-WORKER** | `apps/agent-worker/agent_worker/extrarepos.py` (new), `lifecycle.py`, `workspace.py`, `checkpoint.py`, `standalone_outputs.py`; tests `tests/unit/worker/test_additional_repositories_clone.py`, `test_additional_repositories_not_harvested.py`, `test_checkpoint_leaves_out_repos.py` (all new) | **#1434 check 1**: two entries give three checkouts, each `rev-parse HEAD` equal to its signed commit; **#1434 check 2**: an edit under `work/repos/web` leaves `swarm-work.patch` byte-identical; a revoked grant fails before the agent; a clone failure fails the attempt naming the entry and no URL; the size cap fails terminal; a checkpoint archive has no `repos/` member; a resume re-clones at the same commits; a no-repository task's upload skips `repos/`; the credentials file is gone before the agent starts | 0 |
| 2 | **MR-API** | `apps/swarm-api/swarm_api/extrarepos.py` (new), `schemas.py`, `service.py`, `validation.py`, `codec.py`; tests `tests/unit/control_plane/test_additional_repositories_submit.py` (new) | **#1434 check 3**: an unregistered `owner/repo` is refused `repository_not_registered` and no task is written; not `readable_by_tasks` is refused; no person grant is refused `REPOSITORY_NOT_GRANTED`; a URL in `repository` is refused by shape; a Cloud Run profile is refused; the ref resolves once per workflow and every step gets the same commit; a step's `[]` replaces the workflow's list; the signed spec is at format 4 only when the list is set; the response carries no URL and no token | 0 (parallel with 1) |
| 3 | **MR-BRIDGE** | `apps/swarm-mcp/swarm_mcp/server.py`, `client.py`, `workflows.py`, `plugin/skills/delegate/SKILL.md`; tests `tests/unit/mcp/test_additional_repositories_bridge.py` (new) | the tools send `additional_repositories` as names; `infer` never rewrites them; a URL argument is refused before any request | 2 |
| 4 | **MR-UI** | `apps/swarm-ui/src/Dispatch.tsx`, `Submit.tsx`, `SubmitWorkflow.tsx`, `AgentDetail.tsx`, `types.ts`; tests `apps/swarm-ui/src/__tests__/submit.additional-repositories.test.tsx` (new) | the picker lists only `GET /v1/repositories/readable`; the body sends names; the detail shows the pinned commit | 2 (parallel with 3) |
| 5 | **MR-GRAPH** (phase 2) | `apps/agent-worker/agent_worker/graphstage.py`, `graph_ticket.py`, `graphmcp/shim.py`, `graphmcp/tools.py`; `apps/swarm-api/swarm_api/graph_ticket.py`; tests `tests/unit/worker/test_graphstage_additional.py`, `tests/unit/control_plane/test_graph_ticket_additional.py` (new) | a ticket for a `repo_id` not in the signed list is refused; an index version not an ancestor of the entry's commit is not staged; the shim's `repo` argument picks its own ticket file | 1 and 2 |
| 6 | **MR-DOCS** | `docs/workflows.md`, `docs/runner-git-tools.md`, `docs/design/multi-repository-steps.md` ("What was built" sections) | none (prose); `scripts/changed-guards.sh` | 1-4 (and 5 when built) |

**Acceptance (after 1-4 merge).** One `accept.yml` case belongs with lane
1's PR as a follow-up, since `scripts/acceptance/workflow.sh` is not in any
lane above. It submits a claude-code step with two additional repositories
from the acceptance tenant's registrations and asserts the three #1434
checks against the deployed environment. It is named here so it is not
forgotten. It is not assigned until the owner approves this plan.

**Feature flag.** `settings.additional_repositories_enabled` (swarm-api,
default off) gates the write in lane 2. A deployed swarm-api with lanes 0-2
merged refuses the field `additional_repositories_disabled` until the
operator turns it on, after the workers with lane 1 are deployed.

---

## 7. Owner questions

Each is a decision about cost, product behaviour or security posture. The
recommendation is the first option. They are also in the run's
`questions.json`.

**Q1. What admits a repository as an additional one?**
1. *(recommended)* Registered **and** `readable_by_tasks: true`, the same
   admin-set, audited gate `read_private` uses. One rule for "a task may read
   a repository that is not its own".
2. Registered alone. Less friction: every registration becomes readable by
   every task of the tenant.
3. Registered, plus a per-workflow allowlist the submitter declares.

**Q2. A repository readable only through a repository (`git-r-`) token.**
1. *(recommended)* Refuse it at submission
   (`additional_repository_unreadable`) in phase 1. Revisit with Design C
   if a tenant needs it.
2. Build Design C (swarm-api serves bundles) now, for those repositories
   only.
3. Grant the tenant worker account read on `git-r-` slots. This reverses
   #1225's decision that only swarm-api reads them.

**Q3. When is a ref pinned?**
1. *(recommended)* At submission, by swarm-api, to a 40-hex commit, signed.
   Every step and every retry sees the same code.
2. At each attempt's clone. Simpler, but a retry or a later step may see a
   different commit.
3. Require callers to pass full 40-hex shas.

**Q4. Which backends take additional repositories?**
1. *(recommended)* GKE agent profiles only (generic, claude-code, codex,
   browser, claude-code-browser), on the cache volume. The Cloud Run
   profiles (mock, claude-code-review) are refused.
2. Cloud Run too, inside the 4 GiB memory tmpfs, which risks ENOSPC and
   memory pressure.
3. Cloud Run with a new larger resource class (a contract request, more
   memory billed per attempt).

**Q5. Limits per step.**
1. *(recommended)* At most 4 additional repositories and 6 GiB in total, of
   the 20 GiB cache volume.
2. At most 8 and 12 GiB, which leaves 8 GiB for tool caches and `TMPDIR`.
3. No count limit, only the size budget.

**Q6. Workflow-level and step-level lists.**
1. *(recommended)* The workflow's list applies to every step. A step's own
   list replaces it, and `[]` means none.
2. Step-level only.
3. Workflow-level only, like the primary repository.

**Q7. The code graph for additional repositories.**
1. *(recommended)* Phase 2 (lane 5): staged when the repository is
   indexed, and plain checkout otherwise.
2. Never. Checkout and `rg` only.
3. Required: refuse an additional repository that has no index.

**Q8. A task with no primary repository.**
1. *(recommended)* Allowed. It reads the additional repositories and
   publishes only its own outputs (the checkouts are excluded from the
   standalone upload).
2. Refused: an additional repository needs a primary.

---

## 8. What was built

Read on 2026-10-19 at `c464b1f`, after MR-CONTRACT, MR-WORKER, MR-API,
MR-GRAPH, MR-BRIDGE and MR-UI had merged. This section records what holds
and where it differs from §1-§7. The reasoning above is left as written,
because it is the record of why. Where the two disagree, **this section
describes the code**. The status line at the top ("Nothing here is built")
predates the build and is outside this lane's territory.

### 8.1 The behaviour, in one line each

* **The gate is registered and `readable_by_tasks`** (owner's Q1, option 1).
  A repository the tenant has not registered is refused
  `repository_not_registered` (422). One registered but not readable by
  tasks, or `archived`, is refused `repository_not_readable_by_tasks` (403).
  Registered alone is not enough. Nothing is written for a refused
  submission.
* **Names, never URLs.** A caller sends `{repository: "owner/repo", ref?,
  name?}` (`apps/swarm-api/swarm_api/schemas.py::AdditionalRepositoryCreate`).
  The URL, the slot, the commit and the size are swarm-api's
  (`apps/swarm-api/swarm_api/extrarepos.py::Resolver`), never the caller's
  (invariant 10). The MCP bridge refuses a URL, a remote or any key but those
  three before it sends anything
  (`apps/swarm-mcp/swarm_mcp/client.py::check_additional_repositories`).
* **No repository (`git-r-`) slot, ever** (Q2, option 1). The clone uses the
  submitter's user slot for that repository's owner, or the service slot.
  A repository that only a `git-r-` slot could read is refused
  `additional_repository_unreadable` (422). The same code, and the same
  sentence, cover a slot that cannot read it, a missing token, a forge
  401/403/404 and a ref that names no commit. Which of them it was is not
  the caller's to learn (`extrarepos.py::UNREADABLE`). The frozen contract
  holds the same line from the other side: `ADDITIONAL_FORGE_CREDENTIAL`
  matches `git` and `git-u-<16 hex>` only, so a stored entry cannot name
  a `git-r-` slot.
* **Pinned at submission to a signed 40-hex commit** (Q3, option 1).
  swarm-api reads `commits?sha=<ref>&per_page=1` with the slot the worker
  will clone with, so a ref that resolves at submission can be cloned at the
  attempt. An absent ref is the registration's `default_branch`, else the
  forge's. Each (repository, ref, slot) is resolved once per submission, so
  every step of one workflow reads the same commit even if the branch moves
  while it is being submitted. The list is inside the spec signature at
  format 4 (contract request 96). A rewritten commit, an added entry or a
  swapped slot fails the worker's check before the agent runs (invariant 5).
* **GKE agent profiles only, on the cache volume** (Q4, option 1).
  `extrarepos.py::takes_additional_repositories` admits an agent profile
  whose backend is GKE Autopilot: on 2026-10-18 that was `generic`,
  `claude-code`, `codex`, `browser` and `claude-code-browser`. It refuses
  `mock` and `claude-code-review` (Cloud Run), `merge` and `post-verdict`
  (worker actions), `await-input` and `indexer` with
  `additional_repositories_unsupported_profile` (422). The worker refuses
  the same thing again, terminally and under the same name, if an attempt
  without a cache volume of its own ever gets one
  (`apps/agent-worker/agent_worker/extrarepos.py::prepare`).
* **The limit is a platform setting, not a constant.** See §8.2.
* **Only `work/repo` comes back.** `collect`, `direct-pr` and `integrate`
  are unchanged. A change made under `work/repos/<name>` is not in the
  collected patch (`tests/unit/worker/test_additional_repositories_not_harvested.py`).

### 8.2 The limit: `max_additional_repositories`, and the ceiling that is not it

§4.1, §5 request A and §7 Q5 propose `MAX_ADDITIONAL_REPOSITORIES = 4`, a
constant in the frozen contract. **That was not built.** The owner's
decision of 2026-10-17 on PR 1461 made the count a setting an admin edits
while the platform runs:

* **`max_additional_repositories`** is a platform setting. Its default is
  **5** and its accepted range is **1 to 10**. It lives in
  `apps/swarm-api/swarm_api/store.py::PLATFORM_SETTINGS_DEFAULTS` and
  `PLATFORM_SETTINGS_RANGES`, and an admin sets it with
  `PUT /v1/admin/settings` or the console's Admin settings → "Additional
  repositories" card. swarm-api reads it at **every submission**
  (`extrarepos.py::Limits.from_settings`), so a change admits or refuses
  the next submission without a deploy. A task already submitted keeps
  what it was pinned with. A stored value outside the range, or of the wrong
  type, reads as the default, so a typo written straight to Firestore cannot
  lift the limit.
* **A step over it is refused `too_many_additional_repositories` (422),
  naming the limit.** The message says "a step may read at most <n>
  additional repositories (the platform setting
  max_additional_repositories)". The detail carries `limit`, `count` and
  `setting`. The bridge does not check the count, because a copy of today's
  value would refuse tomorrow's legal list. The plugin's delegate skill
  tells an agent the same thing.
* **`ADDITIONAL_REPOSITORIES_CEILING = 10`, in
  `apps/common/swarm_common/models.py`, is a guard on the document's
  shape, never the limit.** `check_additional_repositories` refuses a
  stored list longer than 10, and the setting's upper bound is that ceiling,
  so the setting can rise to 10 without a contract change. Nobody should
  quote 10 as "how many a step may read". The request schema's own bound,
  `schemas.py::MAX_ADDITIONAL_REPOSITORIES_IN_REQUEST` (40), only caps what
  one body costs to parse. A list between the setting and 40 gets
  `too_many_additional_repositories`, not pydantic's generic shape.
* **The size budget is a setting too:** `additional_repositories_budget_gib`,
  default 6, range 1-20 (the cache volume's size). swarm-api enforces it on
  the forge's estimate at submission (`additional_repositories_too_large`,
  422, naming the budget and the setting). The worker reads the same key
  from `control/settings` at every clone
  (`apps/agent-worker/agent_worker/extrarepos.py::budget_bytes`) and
  enforces it on the measured checkouts, terminally.

### 8.3 Where the build differs from §4

| design said | built |
|---|---|
| At most 4, a contract constant (§4.1, §5 A, §7 Q5) | The setting `max_additional_repositories`, default 5, range 1-10; the contract's `ADDITIONAL_REPOSITORIES_CEILING` (10) is a shape guard (§8.2) |
| A 6 GiB budget (§4.6) | The setting `additional_repositories_budget_gib`, default 6, range 1-20, read by swarm-api and by the worker |
| Contract request `<n>`, spec format 4 | Requests 95 and 96 in `docs/contract-change-requests.md`, format 4. `SPEC_FORMAT` is now 5 (request 104, `model_class`, #1435). A task that names a model class and additional repositories signs at 5, which covers both |
| `REPOSITORY_NOT_GRANTED` per additional repository | The same code, one refusal naming every ungranted entry, and only while `repository_grants_enforced` is on. With grants not enforced, a person's ungranted entry is cloned with the service slot, as for the primary |
| The workflow's list applies to every step | It applies to every step that **runs an agent** (`extrarepos.py::runs_an_agent`). A worker action or an `await-input` step inherits none. A step's own list still replaces it, and `[]` means none (Q6) |
| Refusal codes (§4.1 table) | All of them, plus `additional_repositories_disabled` (422) while `ADDITIONAL_REPOSITORIES_ENABLED` is off, and `validation_failed` when the deployment has no forge reader configured. An archived registration is `repository_not_readable_by_tasks` |
| Checkouts at `<SWARM_CACHE_ROOT>/<attempt_id>/repos/<name>` (§4.4) | `<attempt cache>/repos/<name>`, the attempt's own cache directory on the pod's cache volume. `work/repos/<name>` links to it |
| A revoked grant fails `additional_repository_not_granted` | It does, terminally, and every entry's grant is read before any clone starts. A grant that **cannot be read** is `additional_repository_grant_unreadable`, which is retryable |
| `chmod -R a-w` and an invalid push URL | Both, with the push URL `read-only-additional-repository://no-push`. Still a signal, not a boundary (§4.5) |
| The checkpoint always leaves `repos` out | Only for a task that has additional checkouts (`CheckpointManager.leaves_out_extra_repos`). A task without them whose agent made its own `repos/` folder keeps it, because that folder is the agent's work |
| `standalone_outputs.scan` reserves `repos` | The worker's reserved names (`Worker._workdir_reserved`) add `repos` when the attempt has checkouts. `standalone_outputs.py` is unchanged |
| The UI picker lists `GET /v1/repositories/readable` (§4.8) | It lists `GET /v1/repositories` (up to 20 pages), keeping `readable_by_tasks === true` and not archived. `/readable` lists what the token can see, including repositories nobody registered, and every one of those the API refuses. The read-back is on the task detail (`AgentDetail.tsx`) and on the submit form's created view, each row `name · owner/repo @ ref · commit[:12]` |
| The graph for additional repositories, phase 2 (§4.9) | Built (MR-GRAPH). The ticket body's `repo_id` (`apps/swarm-api/swarm_api/graph_ticket.py::AdditionalGraphTicketRequest`, a subclass, so the primary's body is unchanged) is accepted only from the task's signed list; anything else is 404 `no_graph`. A served graph's shim takes a `repo` argument. A **staged** one is written under `work/.swarm-context/repos/<name>/snapshot/`, but the staged server reads only the step's own snapshot today. An entry never fails the attempt; one with no graph says `graph: false` in its stage record |
| One `accept.yml` case after lanes 1-4 | **Not built.** `scripts/acceptance/` has no additional-repositories case, so the three #1434 checks are held offline only (§8.4) |

### 8.4 How each #1434 check is held, and what is not

* **Three checkouts at their pinned commits**:
  `tests/unit/worker/test_additional_repositories_clone.py`.
* **A change in an additional checkout is not in the collected patch**:
  `tests/unit/worker/test_additional_repositories_not_harvested.py`.
* **An unregistered repository is refused at submission**:
  `tests/unit/control_plane/test_additional_repositories_submit.py`.
* **Not held against a deployed environment.** No acceptance group submits a
  multi-repository step. `ADDITIONAL_REPOSITORIES_ENABLED` defaults off and
  nothing under `terraform/` sets it, so a deployed swarm-api refuses the
  field `additional_repositories_disabled` until an operator turns it on.
  The rollout order is the design's: workers with MR-WORKER first, then the
  flag.
