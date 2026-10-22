# Agent autonomy: from a prompt to a merged and deployed change, inside SwarmCloud

**Status: DESIGN, not built.** Lane AUTONOMY-DESIGN, 2026-10-22, part of
#1933. The implementation lanes are §6; the questions the owner answers before
they start are §7. Nothing here changes behaviour until a lane in §6 builds it.

**The request (BGD, 2026-10-22 ~04:45Z):** "also make sure we include in this
plan the changes required to our agent runtimes so they can access github, the
api and all the other mandatory or optional but needed dependencies and
capabilities to be able to run independently from this session or from claude
code, all inside swarmcloud, able to work and deliver a fix or feature end to
end from prompt to merge and deploy".

The schedule type that is the first user of all of it, `failure-review`, is
[failure-review.md](failure-review.md). It is built on this document and needs
§6's capability lanes before its own.

**What "independent" means here.** An agent started by a schedule, an issue
run, another agent, or a person in the console reaches a merged and deployed
change without a Claude Code session anywhere: no operator laptop reading CI
for it, answering it, relaunching it or pushing what it could not. Where a step
is the owner's **on purpose** (an IAM apply, a contract change, a live-infra
one-off, spend), the agent stops there, says so, and spends nothing while it
waits.

---

## 1. The rules this design keeps

Each capability below is designed against these, and §3 says for each how.

* **The agent never holds a credential.** Two patterns already broker every
  access and this design extends them rather than adding a third:
  * **runner git tools** — the agent calls an MCP tool, the shim writes a
    request into the spool `work/.swarm-git/requests/`
    (`apps/agent-worker/agent_worker/gitmcp/server.py`), the worker signs and
    posts it to `POST /v1/attempts/{attempt_id}/git/{tool}`
    (`apps/swarm-api/swarm_api/routes/gittools.py`), and swarm-api runs the
    call with the tenant's forge slot (`apps/swarm-api/swarm_api/gittools.py`,
    [runner-git-tools.md](../runner-git-tools.md));
  * **child agents** — the agent writes `requests/<id>.json` into
    `work/.swarm-children/`, the worker posts it to
    `POST /v1/tasks/{task_id}/children` with its Google ID token and an Ed25519
    attempt proof over `swarm-child-req/v1` (`apps/agent-worker/agent_worker/children.py`,
    [child-tasks.md](child-tasks.md)).
  Both are fenced (a stale generation gets `git_fenced` / `attempt_fenced`),
  rate-limited per attempt, and authenticated as the **worker**, whose key is
  made in the worker's own memory, never as the agent.
* **A forge token lives only in Secret Manager** (`swarm-tenant-<tenant>-git`
  and its `-git-r-<hex>` / `-git-u-<hex>` slots, [git-tokens.md](../git-tokens.md)),
  read by swarm-api and the worker process, never written to a Job
  environment, a file in the workspace or a log (owner rule 2026-09-25).
* **Invariant 9.** An agent reads only its own tenant's state. The only
  exception is a platform-scoped schedule designed to read more, and
  [failure-review.md](failure-review.md) §1 is that design.
* **Invariant 10.** A caller picks a runner profile by name. Nothing a caller
  or an agent sends picks an image, a command, a credential or a backend
  parameter. Every new tool below takes ids and names, never a token, a URL to
  fetch with one, or an argv.
* **Egress.** There is no hostname allowlist for workers today:
  `kubernetes/network-policies/allow-egress.yaml` allows DNS, the metadata
  server, the Private Google Access VIPs and `0.0.0.0/0` minus private ranges
  on 443, 80 and 22, so `api.anthropic.com` and `github.com` are reachable.
  `apps/swarm-egress` is the browser's CONNECT proxy, a different thing. So no
  capability here is blocked by egress, and none needs it widened. Every new
  read below is made **by swarm-api**, never by the worker reaching a new
  host, so a future hostname allowlist (Terraform's `restrict_egress`,
  `terraform/modules/network/firewall.tf`) needs no entry for any of them.

---

## 2. Inventory, measured on main (`0235ddd`, 2026-10-22)

Read from the code, nothing run. **exists** = an agent can do it today with no
session; **partial** = some of it, or only the platform can; **missing** = no
path.

| # | capability | state | proof |
|---|---|---|---|
| 1 | read the issue | **exists** | the worker fetches `/issues/N` and its comments with the tenant slot and writes `work/issue.md` beside the checkout (`apps/agent-worker/agent_worker/issue.py`, fetch at :653) |
| 2 | read the repository | **exists** | shallow clone with a 0600 credential file deleted before the agent starts (`apps/agent-worker/agent_worker/gitops.py`); later reads through `git_fetch` (`swarm_api/gittools.py` `_fetch`, :786) |
| 3 | read another private repository | **partial** | `read_private` (`swarm_api/gittools.py`, :939): one file or one directory listing, only in repositories the tenant marked `readable_by_tasks` |
| 4 | read a PR's checks and failing-job log tails | **partial** | `pr_checks` (`swarm_api/gittools.py`, :1024) returns the head's check runs, statuses and a scrubbed `job_log_tail` (`forgewrite.py`, :912); only for one PR's head |
| 5 | read any CI run's jobs and logs, a PR's body, diff and comments, issues and their comments, issue search, release runs | **missing** | `TOOLS` is exactly five names (`agent_worker/gitmcp/server.py`, :56-80; `swarm_api/gittools.py`, :148-150); `issue.md` is the only issue read |
| 6 | file an issue, comment on one | **missing** for an agent; **exists** for the platform | no swarm-git tool writes issues; swarm-api does through `forgewrite.GitHubWriter.create_issue` (:579) and `create_comment` (:504), from `observer`, `ci_flake_hunter`, `release_health`, `cost_report`, `epic_triage`, `cifix`, `issuesync`; the worker posts review minors on the findings epic (`agent_worker/forge.py`, :1065) |
| 7 | push a branch mid-run | **exists** | `git_push_branch` (`swarm_api/gittools.py` `_push`, :837), only to the derived `swarm/<id>` branch (`push_policy`, :341), never forced |
| 8 | open a PR | **exists** (at the end) | the worker's `_publish_git` (`agent_worker/lifecycle.py`, :11507) opens it from `pr-title.txt` and `pr-body.md` with `forge.open_pull_request` (`agent_worker/forge.py`, :780); no tool opens one mid-run |
| 9 | push `.github/workflows` files | **missing** | the tenant slot has no `workflow` scope (no such row in [git-tokens.md](../git-tokens.md) §5.1), GitHub refuses the push, surfacing as `remote_rejected` (`swarm_api/gitexec.py`, :663); an issue run's plan naming such a file is refused `WORKFLOWS_PATH` ([schedules.md](../schedules.md) §4.4); lanes write `workflow-lines.patch` and the operator pushes it by hand. The brief records SwarmCloud lanes ending SUCCEEDED with the file unpushed; `PUBLISH_FAILED_FIELD` (#872, `lifecycle.py` :12046) is meant to fail a refused push, and whether it caught those lanes was not measured here |
| 10 | read its own tenant's SwarmCloud state (agents, workflows, events, results, artifacts, the stored log tail) | **missing** | `write_mcp_config` gives the agent `playwright`, `jev`, `swarm-git`, `swarm-search`, `swarm-graph` and relayed registered servers (`agent_worker/runners/claude_code.py`, :835-879); the read routes (`routes/tasks.py` `/events` :623, `/artifacts` :759, `/logs` :938, `/questions` :1061) need a person's scope; the only attempt-scoped reads are `/v1/attempts/{id}/context/inflight` and `/context/graph` (`routes/attemptcontext.py`) |
| 11 | read Cloud Logging for its tenant's attempts | **missing** | nothing in `apps/` reads Cloud Logging (no `google.cloud.logging`, `entries:list` or `logadmin`); swarm-api's account holds only `roles/logging.logWriter` among logging roles (`terraform/modules/iam/bindings.tf`, `telemetry_roles`) |
| 12 | dispatch child agents | **exists** | the spool and `routes/children.py` (:90); at most 16 per parent (`settings.max_children_per_task`), depth 1 (`ChildDepthExceeded`), the parent's repository |
| 13 | dispatch a child workflow or issue run | **missing** | `ChildSpec` takes `runner_profile`, `input`, `resource_class`, `provider`, `model`, `model_class`, `timeout_seconds`, `repository_ref` only (`swarm_api/children.py`, :185-191) |
| 14 | park on CI_PENDING and resume on a wake, spending nothing | **exists** (the merge step) | `merge.py` parks `PARKED/CI_PENDING` with `metadata.merge_wait` (:1067), woken by the per-tenant tick `POST /v1/admin/merges/wake` (`routes/admin.py` :1112, `mergewake.wake_tenant` :1108), no lease held while parked |
| 15 | fix rounds on red CI | **exists** | merge step `fix_rounds` 0-5 (`MERGE_FIX_ROUNDS_MAX`, `merge.py` :212); an issue run's CHECKING ⇄ FIXING loop, 1-5 rounds, default 3 ([issue-runs.md](../issue-runs.md)); the excerpt from `issueci.excerpt_at` (:850), redacted, bounded to 8 KiB, between nonce markers (`issueci.ci_fix_continuation`, :953-962) |
| 16 | merge | **exists** | the merge step, only on a review `verdict == "MERGE"` (`merge.py`, :1145); `merge: auto` only through the audited switch `POST /v1/schedules/{id}:merge-mode` (`routes/approvals.py`, :317-375, audited `gate_merge_auto` in `schedaudit.py`) |
| 17 | see its change deploy (release status, deployed digest) | **missing** | only `GET /v1/version` (`routes/platform.py`, :34-66), whose `git_sha` names the commit that built the image, not the release that deployed it; `release-health` opens issues on red streaks and reports nothing to an agent |
| 18 | ask the owner a question and get the answer with no session open | **partial, one-way** | `questions.json` is validated and uploaded (`agent_worker/questions.py`), shown in the console (`apps/swarm-ui/src/QuestionsCard.tsx`) and the bell's "needs you" (`swarm_api/notifications.py`, :488-520), answered through `POST /v1/tasks/{id}/answers` into `metadata.owner_answers` (`swarm_api/agent_questions.py`); nothing in the worker or scheduler reads an answer back (`questions.py`: "DATA, NEVER A CHANNEL"), and nothing reaches the owner outside the console |
| 19 | toolchain: uv, gh, gcloud, git, node/npm, ripgrep | **exists** | `images/agent-runtime-base/Dockerfile`: uv 0.12.15 (:53), Node 24 with npm 11 (:47, :261-273), gcloud 587.0.0 (:582), gh 2.102.0 (:638), git and ripgrep (:243-257), terraform, checkov, shellcheck (:899) |
| 20 | toolchain: `apps/swarm-ui` node_modules | **missing** | no image installs them; every UI agent runs `npm ci` itself, from the network, inside its own time limit |
| 21 | toolchain: Playwright and browsers | **partial** | only `agent-runtime-browser` (Playwright 1.63.0, Chromium only, `images/agent-runtime-browser/Dockerfile` :43, :71-76), reached by `browser` and `claude-code-browser`; `claude-code` has none |
| 22 | memory | **partial** | every code profile is `standard`, 8 GiB, request = limit (`swarm_common/profiles.py`); `large` (32 GiB) "is reached by no profile today" ([runner-sizes.md](runner-sizes.md) §4.1); #1926 made an OOM fail the agent at once and say so (`reconciler/memory.py`), it did not add memory |
| 23 | time | **partial** | `claude-code` is 7,200 s (`profiles.py`, `timeout_seconds=7200`), killed by the worker as "runner exceeded its 7200s timeout and was killed" (`lifecycle.py`, :3899, `EndCause.TIMEOUT`); three agents died there on 2026-10-22 |

**What the operator's laptop does that no agent can (measured 2026-10-22).**
The table is §4; in one line: rows 5, 6, 9, 10, 11, 13, 17 and 18 are the
reasons a session had to exist that day.

---

## 3. The design, per missing or partial capability

### 3.1 GitHub reads (rows 4, 5, 17): more swarm-git tools

New tools on the existing server, same route, same proofs, same fencing:
`POST /v1/attempts/{attempt_id}/git/{tool}`. Every one runs in swarm-api with
the agent's own signed `forge_credential` slot (`gittools.task_slot`), on the
agent's **own repository** only (`other_repository` otherwise), returns
redacted text (`swarm_redaction.redact`) bounded per field, and counts against
the per-attempt budget of 60 calls an hour.

| tool | arguments | returns | GitHub API |
|---|---|---|---|
| `pr_view` | `number` | title, body, state, head and base, labels, changed files with additions and deletions, reviews and comments (last 50) | pulls, pulls/files, issues/comments, pulls/reviews |
| `run_list` | `workflow` (a file name under `.github/workflows/`), optional `branch`, `head_sha`, `status`, `limit` ≤ 20 | runs with id, head sha, event, status, conclusion, created and updated | actions/workflows/{file}/runs |
| `run_view` | `run_id`, optional `failed_only` (default true) | jobs, steps and conclusions, and each failed job's log tail (the `job_log_tail` reader `pr_checks` already uses, `forgewrite.py` :912), 8 KiB per job, 5 jobs | actions/runs/{id}, jobs, jobs/{id}/logs |
| `issue_view` | `number` | title, body, state, labels, comments (last 50) | issues/{n}, comments |
| `issue_list` | `labels`, `state`, `query` (searched in title and body), `limit` ≤ 30 | number, title, labels, state, updated | search/issues scoped `repo:<own>` |
| `release_status` | `sha` | §3.7 | actions runs, compare, pending deployments |

Why tools rather than `gh` in the image: `gh` is installed (row 19), but
it would need the forge credential in the container, which is the one thing the agent
must never hold. The tool returns the same facts the operator reads with
`gh run view --log-failed` and `gh pr view`, scrubbed.

**Text from GitHub is data.** Every tool result that carries text a third
party wrote (a body, a comment, a log) is returned between nonce marker lines,
the `issueci.ci_fix_continuation` pattern, with the sentence that it is data.

### 3.2 Filing issues and comments (row 6)

Two write tools, also swarm-git, also the own repository only:

* **`issue_create`** — arguments: `form` (`bug_report`, `feature_request` or
  `epic`, the file names in `.github/ISSUE_TEMPLATE/`), `title`, and `sections`, a map from
  the form's field ids to text. swarm-api reads the form from the default
  branch, **refuses** a missing required field (`issue_form_incomplete`,
  naming it), renders the body in the form's sections and order, and applies
  the form's label. So an agent files in the form exactly as CLAUDE.md
  "Issues" requires of a CLI filing, and cannot skip "what you expected".
  The body opens with a marker naming the attempt, so a retried call finds the
  issue it already filed instead of filing it twice (the `observer_epics`
  rule, [schedules.md](../schedules.md) §3.4).
* **`issue_comment`** — `number`, `body`. Refused on a closed issue unless
  `reopen: false` is stated, and refused on a pull request (that is the merge
  step's and the review's channel).

Bounds: at most 10 issue writes per attempt and 30 per agent across its
attempts, so a looping agent files a page, not a flood. A body is scanned with
`swarm_redaction.redact` and **refused** (`leak_found`) rather than silently
masked when it matches, because an issue is public in a public repository.

**Who may write.** Reads are every agent's that has git tools. Writes are a
rise in GitHub reach for every agent, so they are the owner's (§7 Q2). The
recommended answer: an agent may write issues when the tenant's registration
of the repository says `agents_may_file_issues: true`, set by an admin, off by
default. The platform's own registration would set it.

### 3.3 Pushing `.github/workflows` files (row 9)

Today the tenant slot cannot, and [schedules.md](../schedules.md) §4.4 refuses
such a plan outright (`WORKFLOWS_PATH`). The design keeps the agent
credential-free and makes the push the broker's:

* **A separate slot**, `swarm-tenant-<tenant>-git-wf`, holding a token with
  the `workflow` scope (a classic PAT) or `workflows: write` (a fine-grained PAT
  or App), stored only by `scripts/create-secrets.sh --stdin` and readable by
  **swarm-api alone**, not by the tenant worker. Declared empty by Terraform;
  the value is the owner's to create.
* **Used only by `git_push_branch`**, and only when the pushed range touches
  `.github/workflows/**` or `.github/actions/**`, the repository is
  `platform: true`, and the slot holds a version. Otherwise the push is
  refused with a code of its own, `workflow_scope_missing`, naming the files,
  instead of GitHub's generic `remote_rejected`.
* **The end-of-agent publish** routes a workflow-touching branch through the
  same broker instead of the worker's own push (`lifecycle.py` `_publish_git`),
  and a refusal fails the agent with `PUBLISH_FAILED_FIELD`, writing the diff
  as `workflow-lines.patch` in its artifacts. A lane can no longer end
  SUCCEEDED with its workflow change unpushed.
* **The plan refusal becomes a hold.** With the slot present, a plan naming
  a workflow file is held `NEEDS_OWNER` instead of refused: a workflow can
  reach CI's secrets and the deployer's identity, so merging one stays the
  owner's (§7 Q3). With the slot absent, `WORKFLOWS_PATH` stays exactly as it
  is.

### 3.4 Reading its own tenant's SwarmCloud state (row 10)

A new MCP server in the worker, **`swarm-state`**, with the same shim-and-spool
shape as `swarm-git` (spool `work/.swarm-state/`), relayed by the worker with
the `swarm-state-req/v1` proof the attempt already registers
(`agent_worker/statecall.py`). swarm-api serves it on new attempt-scoped
routes, `GET /v1/attempts/{attempt_id}/reads/{tool}`, each of which:

1. authenticates the worker and verifies the proof (`authenticate_worker`,
   `verify_proof`), and checks in one read that the lease is live for this
   attempt and generation (stale: `attempt_fenced`);
2. resolves the **tenant from the lease**, never from the request, and reads
   only that tenant's documents. A document of another tenant answers exactly
   as a missing one does (404 `not_found`), as every tenant-scoped route does.

| tool | returns |
|---|---|
| `agents_list` | own tenant's agents: id, state, profile, end cause, `last_error` (redacted), created, ended; filter by state, window (≤ 7 days), workflow, run |
| `agent_view` | one agent: the task document's public fields, its attempts (exit code, `error`, `peak_rss_bytes`, `oom_near_miss`, durations), `result_summary` |
| `agent_events` | its events (`type`, `at`, `generation`, `detail`), last 200 |
| `agent_log_tail` | a stream's stored tail (`{log_prefix}/live/<stream>.tail.log` while running, `logs/<stream>.log` after), the last 64 KiB, redacted |
| `agent_artifacts` | the artifact list, and a named text artifact up to 256 KiB |
| `workflow_view` | a workflow's steps and states, and `workflow_result` |
| `run_view_sc` | an issue run: state, moves, plan, its workflow, its PR |
| `heartbeats` | a lease's heartbeat series (`routes/leases.py`'s rows) |

All reads, no writes: cancelling, retrying or answering stays with people and
with the platform's own paths. Each call is counted against a per-attempt
budget (120 an hour). The agent's own task document is readable like any other
of its tenant's, which is what lets a fix run read the failure it is fixing.

### 3.5 Reading Cloud Logging (row 11)

A `swarm-state` tool, `logs_query`, served by swarm-api, with a **fixed filter
the agent cannot widen**: swarm-api builds it from the attempt's tenant (the
GKE namespace `swarm-tenant-<tenant>` and the tenant's Cloud Run Job names) and
the agent supplies only an agent id or attempt id, a severity floor and a time
window ≤ 24 h inside that agent's life. Results are capped at 200 entries and
64 KiB, redacted.

The IAM is the hard part, because the project is **shared**:
`roles/logging.viewer` on `saga-agents-staging` would let swarm-api read the
other team's `agents-staging` cluster logs. So:

* **A log view** on the project's `_Default` bucket, `swarm-workers`, whose
  filter admits only SwarmCloud's resources (`k8s_container` in namespaces
  matching `swarm-tenant-` and the swarm system namespaces; `cloud_run_job` and
  `cloud_run_revision` whose names start `swarm-`). Terraform-managed, carrying
  `managed-by=swarm-terraform`.
* **IAM row L-1:** `roles/logging.viewAccessor` for swarm-api's account
  **on that view only** (the resource-level binding with the view name in its
  condition), not project-wide. Owner at dev-iam (§5).

Until the view exists, `logs_query` answers `logs_unavailable`, a real refusal
that says what is missing. Platform components' logs (scheduler, reconciler,
swarm-api) are **not** in a tenant agent's reach under invariant 9: they mix
tenants. Only `failure-review`, platform-scoped, reads them, through swarm-api
(failure-review.md §3).

### 3.6 Child workflows and child issue runs (row 13)

The child spool gains two kinds beside today's agent:

* **`kind: "issue_run"`** — `issue` (a number in the parent's own
  repository), optional `after_merged` (another run id the parent started).
  swarm-api starts it with `routes/runs.py`'s `start_run` path as the
  parent's submitter: plan, implement, review, fix rounds, merge, under the
  run's own gates. With `after_merged`, the run is created **held**
  (`PLANNED`-before-approval, no workflow, no agent, so no capacity, invariant
  1) and released by the schedule tick when the named run reaches `DONE` with
  its PR merged. That is "chaining dependent lanes after a merge" without
  anyone watching.
* **`kind: "workflow"`** — `spec_name`: a **saved, admin-approved** workflow
  spec of the tenant (the `custom-prompt` rule, [schedules.md](../schedules.md)
  §3.12), plus its declared inputs. Never a spec body: a body is images and
  commands by another name, which invariant 10 forbids.

Both count against the parent's 16 children and the depth rule (a child
cannot have children), and the parent's link is recorded in swarm-api's own
children ledger (the parent agent id on the run or workflow document's
`metadata`), which needs no frozen field. Waiting is today's: the parent writes
`await` and exits 0, parks `CHILDREN_INCOMPLETE`, and is restarted when every
child has ended; for an issue run "ended" is `DONE`, `FAILED`, `REJECTED` or
`CANCELLED`.

### 3.7 Seeing its change deploy (row 17)

`release_status(sha)`, a swarm-git tool (§3.1), answers from GitHub alone:

1. the `release.yml` runs on the default branch whose head contains `sha`
   (`compare` from `sha` to the run's head: `ahead` or `identical`);
2. for the newest such run: each job's conclusion, and whether it is **waiting
   at an environment** (`pending_deployments`), naming it, so "held at
   `dev-iam` for the owner" is a state the agent can read and report rather
   than a timeout;
3. the `deploy` job's conclusion: that job verifies the deployed digests
   ([ci.md](../ci.md), the release timeline), so its success is the
   deployed-digest check;
4. `GET /v1/version` of swarm-api, whose `git_sha`, read with the docstring's
   caution, is reported beside it.

No Cloud Run read and no IAM row: the release workflow already proves the
digest, and this reads its verdict. An agent that wants to *wait* for a
release does what the merge step does: it parks (§3.8).

### 3.8 Waiting without spending (rows 14, 17, 18)

The merge step's park is the pattern: checkpoint, park, release the lease,
exit (invariant 4), and a tick decides when to wake. This design reuses it
for two more waits, with no new park reason:

* **Waiting for a release**: an agent writes `$SWARM_ARTIFACTS_DIR/await-release.json`
  (`{"sha": ...}`) and exits 0; the worker parks it `DEPENDENCY_INCOMPLETE`
  with `metadata.release_wait`; the merge-wake tick (`mergewake.wake_tenant`)
  reads `release_status` for parked waits at most every 5 minutes and sets
  READY on a terminal release (deployed, failed, or held at an environment for
  more than 6 hours, which it reports rather than waits through). Bounded at 24
  hours.
* **Waiting for an answer**: §3.9.

### 3.9 Asking the owner, and getting the answer back (row 18)

Today's `questions.json` stays as it is for non-blocking questions. A
question becomes **blocking** with `"blocking": true` on an item:

1. The agent writes `questions.json`, writes an empty `await-answers` file and
   exits 0. The worker uploads the questions and parks the agent
   **`HUMAN_REQUIRED`**, the frozen reason that is "promoted only by a person"
   (`swarm_common/states.py`), with `metadata.question_wait`. No lease, no
   tokens.
2. **Where the question goes:**
   * the console's question list (`QuestionsCard.tsx`) and the bell's "needs
     you", as today;
   * **a comment on the agent's issue** when it has one (an issue run's issue,
     or the issue a schedule filed), naming the owner, each question with its
     options, recommended first, and the reply syntax;
   * the agent's own artifacts, so the record survives.
3. **The answer:** `POST /v1/tasks/{id}/answers` as today, from the console, the
   plugin's `swarm_task_answer`, or a reply comment `/answer <n> <option>` on
   that issue by an authorised answerer (the submitter, or an admin with at
   least the member role, `agent_questions.py`), read by the GitHub comment
   approvals path that already maps a GitHub login to a member
   (`swarm_api/ghapprove.py`). When every blocking question is answered,
   swarm-api promotes the agent to READY, the same transition `human-ready`
   makes.
4. **On resume** the worker writes the answers as `work/answers.json`,
   between nonce markers in the prompt's continuation, as data.
5. Unanswered after 72 hours, it is dead-lettered with
   `last_error: "owner question unanswered"`, which `failure-review` skips by
   rule (failure-review.md §2).

Whether answers by GitHub comment are accepted is the owner's (§7 Q5).

### 3.10 Toolchain (rows 20, 21)

* **UI dependencies.** `agent-runtime-base` gains a warmed npm cache built
  from `apps/swarm-ui/package-lock.json` at image build (`npm ci --cache
  /opt/npm-cache` then the cache kept and `node_modules` discarded) and
  `NPM_CONFIG_CACHE=/opt/npm-cache` read-only with `--prefer-offline` in the
  agent's npm config. `npm ci` then installs from the image in seconds and
  fails over to the network for a lockfile the image did not see. A copied
  `node_modules` is not baked in: it would be wrong for every other
  repository and for this one the day the lockfile changes.
* **Playwright.** It stays in `agent-runtime-browser`. A code agent that needs
  a browser runs as `claude-code-browser` by name, which is invariant 10
  working: the planner picks the profile, nobody ships Chromium in every
  image.
* **Nothing else is missing** for a fix lane: uv, gh, gcloud, git, ripgrep,
  terraform, checkov and shellcheck are in the base image (row 19). `gh` and
  `gcloud` hold no credential there and the agent does not get one: §3.1 to
  §3.5 are how they would have been used.

### 3.11 Memory and time (rows 22, 23)

* **Memory.** #1926's measurement was 8 GiB OOMs on `claude-code`. Two routes,
  both the owner's because both are spend (§7 Q6): a runner-size version
  raising `standard` memory (Admin → Runner sizes, [runner-sizes.md](runner-sizes.md)
  §4.5, no contract change, every standard agent pays), or a new profile
  `claude-code-large` on the `large` class, picked by name by a planner that
  knows the lane is heavy (a contract change, request 113 in
  [contract-change-requests.md](../contract-change-requests.md)). Recommended:
  the profile, because it puts the cost only on the agents that need it.
* **Time.** The same request gives `claude-code-large` 14,400 s. A timeout
  is otherwise left as it is: a two-hour lane is usually a lane that should
  have been two, and `failure-review` reports each timeout's cause rather
  than raising the ceiling for all.

### 3.12 New routes, tools, profiles, IAM and secrets, in one place

| kind | name | owner at dev-iam |
|---|---|---|
| swarm-git tools | `pr_view`, `run_list`, `run_view`, `issue_view`, `issue_list`, `release_status`, `issue_create`, `issue_comment` | no |
| MCP server | `swarm-state` (worker shim) | no |
| routes | `GET /v1/attempts/{attempt_id}/reads/{tool}` | no |
| child kinds | `issue_run` (with `after_merged`), `workflow` (saved spec by name) | no |
| park uses | `DEPENDENCY_INCOMPLETE` + `metadata.release_wait`; `HUMAN_REQUIRED` + `metadata.question_wait` | no |
| registration field | `agents_may_file_issues` (admin-set, off by default) | no (§7 Q2 decides it) |
| runner profile | `claude-code-large` (contract request 113) | no IAM; a contract change and spend, §7 Q6 |
| Terraform | log view `swarm-workers` on `_Default` | **yes**, with L-1 |
| **IAM L-1** | swarm-api's account: `roles/logging.viewAccessor` on the view `swarm-workers` only | **yes** (§7 Q4) |
| **IAM L-2** | swarm-api's account: `secretmanager.secretAccessor` on `swarm-tenant-<platform tenant>-git-wf` only; no worker account gets it | **yes** (§7 Q3) |
| **secret** | `swarm-tenant-<platform tenant>-git-wf`, declared empty, value by `create-secrets.sh --stdin` | **yes**, the owner creates the token |

The forge scopes of the existing `-git` slot do not change. No agent gets a
broader GitHub right except through §7's answers.

### 3.13 What stays the owner's, and why

* **An IAM apply.** `dev-iam` is a GitHub environment only the owner
  approves ([ci.md](../ci.md), "A dev release that changes IAM waits for the
  owner"). An agent can write, render and validate the Terraform; it cannot
  and should not apply it. `release_status` reads the hold, so the agent
  reports "waiting at dev-iam" and stops.
* **A frozen-contract change.** CLAUDE.md rule 1: a request in
  `docs/contract-change-requests.md`, never an edit. The §4.4 contract hold
  already makes such a merge `NEEDS_OWNER`.
* **A one-off fix to live infrastructure** (the metric label added with
  `gcloud` on 2026-10-22). No agent holds a GCP write role, and none should:
  the shared project makes a mistaken write the other team's outage. The agent
  writes the Terraform or the runbook step and the owner runs it.
* **Spend**: a bigger size, a longer timeout, a budget field. The owner's
  by the owner's own rule.
* **Merging a workflow file** (§3.3), and anything §4.4 holds.
* **Bumping and installing the plugin.** The bump is a file in a PR and an
  agent can make it. Installing it runs on the operator's machine, which is
  not SwarmCloud's to reach.

---

## 4. What the laptop session did on 2026-10-22, and where each goes

| the session did | goes to | how |
|---|---|---|
| read CI job logs and PR checks with `gh` | **agent** | `pr_checks`, `run_list`, `run_view` (§3.1) |
| read Cloud Logging and `gcloud` describes to diagnose a failure | **agent** for its own tenant's logs (`logs_query`, §3.5); **failure-review** for platform components; describes of live resources stay the **owner's** | a describe needs project-level read in a shared project |
| read `swarm_debug`, `swarm_result`, owner questions | **agent** | `swarm-state` (§3.4), which serves the same records the plugin's tools read |
| answer lanes' questions | **owner, on purpose**, from anywhere | §3.9: console, or a reply on the issue; the agent waits parked |
| relaunch a lane, write a brief from a finding | **failure-review** and **agents** | an issue filed in the form (§3.2) and an issue run on it (§3.6); the brief is the issue |
| file issues in the repository's form | **agent** | `issue_create` (§3.2) |
| chain dependent lanes after a merge | **agent** | `issue_run` children with `after_merged` (§3.6) |
| approve a dev-iam release hold | **owner, on purpose** | §3.13 |
| a one-off fix to live infrastructure | **owner, on purpose** | §3.13 |
| push workflow files | **broker**, held for the owner's merge | §3.3 |
| bump and install the plugin | bump: **agent**; install: **owner** | §3.13 |

---

## 5. End to end, written out

One path, from a failure to a deployed fix. Each step names the component,
the capability (§2 row, §3 section) and where it stops for the owner.

1. **The failure.** An eng agent ends FAILED with "runner exceeded its 7200s
   timeout and was killed" (`lifecycle.py`, `EndCause.TIMEOUT`).
2. **The firing.** The `failure-review` schedule fires on its cron. swarm-api
   builds the digest (failure-review.md §3): the failure, its events, attempts,
   heartbeat series and log tail, redacted and bounded, and the known causes.
3. **The investigation.** A `claude-code` agent in the platform tenant, with
   the SwarmCloud repository, gets the digest as data between nonce markers.
   It reads the code, the agent's own records (`swarm-state`, §3.4, for the
   platform tenant; the digest for the rest), the PR's CI (`run_view`, §3.1)
   and, through the digest, the platform components' logs. It may fan out one
   child agent per group (row 12). It writes `rca.json`: here one cause, a
   fingerprint, the evidence, the fix, the files, hold category `none`.
4. **The issue.** swarm-api files it with the bug form's sections and label
   (`forgewrite.create_issue`, the platform's path; an agent would use
   `issue_create`, §3.2), its body opening with the fingerprint marker, and
   records `failure_causes/{fingerprint}`.
5. **The fix run.** swarm-api starts an issue run on it (issue-sweep's
   executor, failure-review.md §6). The planner plans; the plan's files are
   checked against §4.4's stops: **a hold stops here for the owner**
   (`NEEDS_OWNER`, label `needs-owner`). Otherwise the implementer steps run:
   red-first tests pushed first, then the fix (`git_push_branch`, row 7), the
   unit tests in the container, `scripts/changed-guards.sh`,
   `scripts/publish-scan.sh`. If a step needs a decision it asks a blocking
   question (§3.9) and parks: **the owner answers** from the console or the
   issue.
6. **Review.** The `review` step writes `verdict.json`. `NOT_YET` sends the
   fix step round; `MERGE` lets the run proceed.
7. **CI and fix rounds.** The PR opens (row 8). The run parks `CHECKING` while
   CI runs, woken by the tick (row 14), spending nothing. Red CI: the first
   reading re-runs failed jobs; then a `ci-fix:<n>` round gets the redacted
   excerpt between nonce markers (row 15), up to the run's `fix_rounds`.
8. **The merge.** Green CI, verdict MERGE, and no hold on the PR's **actual**
   changed files (§4.4's second check point): the merge continuation merges
   (row 16) under the schedule's `merge: auto`, set through the audited
   switch (failure-review.md §6). A hold found here **stops for the owner**.
9. **The release.** `release.yml` runs on main. If the plan changes IAM it
   **waits at `dev-iam` for the owner**. The run's last agent (or the issue
   run's closing step) calls `release_status(<merge sha>)` (§3.7), parking on
   `await-release.json` (§3.8) until the release is terminal.
10. **The deployed digest.** `release_status` reports the `deploy` job, which
    verified the digests, as succeeded, and `GET /v1/version`'s `git_sha`. The
    agent comments on the issue (`issue_comment`, §3.2) with the merge sha, the
    release run and the deploy verdict. The next `failure-review` firing sees
    no new failure with that fingerprint and records the cause as quiet.

---

## 6. The lane plan (both documents)

Dependency order: capabilities first, then the schedule type. Two lanes never
share a file; a lane that finds it needs another lane's file stops and asks.
Every lane is red first (its tests pushed alone, then the fix on
`<branch>-fix`), its unit tests run with no cloud credentials and no emulator,
and it ends with `scripts/changed-guards.sh` and `scripts/publish-scan.sh`.

| lane | builds | territory | depends on | red-first tests | IAM / Terraform | owner at dev-iam |
|---|---|---|---|---|---|---|
| **AU-1 GIT-READS** | §3.1, §3.2, §3.3's broker half, §3.7 | `apps/swarm-api/swarm_api/gittools.py`, `apps/swarm-api/swarm_api/forgewrite.py`, `apps/agent-worker/agent_worker/gitmcp/server.py`, `apps/agent-worker/agent_worker/gittools.py`, `docs/runner-git-tools.md`, new `tests/unit/control_plane/test_git_tools_reads.py`, `test_git_tools_issue_writes.py`, `test_git_tools_release_status.py`, `test_git_tools_workflow_slot.py` | §7 Q2, Q3 answered | a `run_view` on another repository is `other_repository`; a body from GitHub comes back between nonce markers; `issue_create` without "what you expected" is `issue_form_incomplete`; a retried `issue_create` files once; an 11th write is refused; a workflow-touching push with no `-git-wf` version is `workflow_scope_missing`, not `remote_rejected`; `release_status` reports a run waiting at `dev-iam` | none | no |
| **AU-2 WF-SLOT** | §3.3's slot | `terraform/modules/secret_manager/` (the slot), `terraform/infra/git_tools.tf`, `scripts/create-secrets.sh` (`--git-wf`), `tests/terraform/` (one assertion), `tests/unit/scripts/test_create_secrets_git_wf.py` | §7 Q3 | `terraform test`: the slot exists empty, labelled `managed-by=swarm-terraform`, readable by swarm-api only and by no worker account; the script refuses a value on argv | **L-2**, the secret | **yes** |
| **AU-3 PUBLISH-WF** | §3.3's publish half | `apps/agent-worker/agent_worker/lifecycle.py` (`_publish_git` only), new `tests/unit/worker/test_publish_workflow_files.py` | AU-1, AU-2 | a publish whose branch touches `.github/workflows/` goes through the broker, and a refused one ends with `PUBLISH_FAILED_FIELD` and a `workflow-lines.patch` artifact, never SUCCEEDED | none | no |
| **AU-4 STATE-READS** | §3.4, §3.5's route | new `apps/swarm-api/swarm_api/attemptreads.py`, new `apps/swarm-api/swarm_api/routes/attemptreads.py`, `apps/swarm-api/swarm_api/main.py` (one `include_router`), new `apps/agent-worker/agent_worker/statemcp/`, `apps/agent-worker/agent_worker/runners/claude_code.py` (`write_mcp_config`), new `tests/unit/control_plane/test_attempt_reads.py`, `tests/unit/worker/test_state_mcp.py` | none | another tenant's agent id answers exactly as a missing one; a stale generation is `attempt_fenced`; the tenant comes from the lease, not the body; a log tail is redacted; `logs_query` with no view configured is `logs_unavailable`; the filter a caller sends cannot name another namespace | none | no |
| **AU-5 LOG-VIEW** | §3.5's view and IAM | new `terraform/infra/log_view.tf`, `terraform/modules/iam/` (the one binding), `tests/terraform/` (assertions) | §7 Q4 | `terraform test`: the view's filter admits only `swarm-tenant-` namespaces and `swarm-` Cloud Run names; the binding is on the view, never on the project; the role is `viewAccessor` | **L-1** | **yes** |
| **AU-6 CHILD-RUNS** | §3.6 | `apps/swarm-api/swarm_api/children.py`, `apps/swarm-api/swarm_api/routes/children.py`, `apps/agent-worker/agent_worker/children.py`, `docs/design/child-tasks.md`, `docs/child-tasks-for-agents.md` (the `AGENT_GUIDE` copy), new `tests/unit/control_plane/test_child_runs.py` | none | a child `issue_run` on another repository is refused; a `workflow` child naming an unapproved spec is refused; a spec body is refused; an `after_merged` run holds no agent until its predecessor is DONE and merged; it counts toward the 16 | none | no |
| **AU-7 WAITS** | §3.8, §3.9 | `apps/agent-worker/agent_worker/questions.py`, `apps/agent-worker/agent_worker/handoff.py`, `apps/swarm-api/swarm_api/agent_questions.py`, `apps/swarm-api/swarm_api/mergewake.py` (the release-wait read), `apps/swarm-api/swarm_api/ghapprove.py` (`/answer`), `apps/swarm-api/swarm_api/notifications.py`, `apps/swarm-ui/src/QuestionsCard.tsx`, new tests `tests/unit/worker/test_blocking_questions.py`, `tests/unit/control_plane/test_answer_promotes.py`, `test_release_wait.py`, `apps/swarm-ui/test/QuestionsCard.blocking.test.tsx` | AU-1 (`release_status`, `issue_comment`) | a blocking question parks HUMAN_REQUIRED with no lease; the last answer promotes to READY and the resumed attempt finds `answers.json`; an answer by a non-member's comment is ignored; 72 h unanswered dead-letters with the named error; a release wait wakes on a terminal release and not before | none | no |
| **AU-8 IMAGE-NPM** | §3.10 | `images/agent-runtime-base/Dockerfile`, new `tests/unit/scripts/test_base_image_npm_cache.py` | none | the Dockerfile builds the cache from the UI lockfile, discards `node_modules`, and sets the cache read-only; the image check list names npm's offline install | none | no |
| **AU-9 PROFILE-LARGE** | §3.11, if Q6 says so | `apps/common/swarm_common/profiles.py` (**only after the owner accepts request 113**), `docs/design/runner-sizes.md` §1a.3, tests in `tests/unit/common/` | §7 Q6 | `claude-code-large` names `large`, 14,400 s, GKE, on-demand, `requests == limits` for memory | none | no IAM; **owner's acceptance** of the contract change |
| **FR-1 HOLD-CATEGORIES** | failure-review.md §6's added stops, and §3.3's plan hold | `apps/swarm-api/swarm_api/issueruns.py` (`hard_stop_hold` and the `WORKFLOWS_PATH` refusal), new `tests/unit/control_plane/test_hold_categories.py`, `tests/unit/control_plane/test_workflows_path_hold.py` | AU-2, §7 Q3, Q7 | a plan naming a workflow file is `NEEDS_OWNER` with the `-git-wf` slot and `WORKFLOWS_PATH` without; a PR touching `scripts/create-secrets.sh` is held `secrets`; one touching `swarm_api/auth.py` `tenancy`; one naming a deny-list resource `shared`; one changing a budget cap or a resource class `cost`; a clean one is not held | none | no |
| **FR-2 DIGEST** | failure-review.md §2, §3, §5 | new `apps/swarm-api/swarm_api/failuredigest.py`, new `apps/swarm-api/swarm_api/failurecauses.py`, new `tests/unit/control_plane/test_failure_digest.py`, `test_failure_causes.py` | AU-4 (its readers), AU-5 for log lines (degrades to none without it) | each skip rule skips exactly its case and names itself; a person's cancel and a cascade from it are skipped, a system cancel is not; the digest is redacted, bounded and carries no prompt; the same failure in two overlapping windows is one entry; a fingerprint is stable across reruns | none | no |
| **FR-3 TYPE** | failure-review.md, the type | new `apps/swarm-api/swarm_api/schedtypes/failure_review.py`, `apps/swarm-api/swarm_api/scheduletypes.py` (`TYPES`), `tests/unit/control_plane/test_schedules_model.py` (`CATALOGUE`), new `tests/unit/control_plane/test_schedtypes_failure_review.py`, `docs/schedules.md` (§3 entry to "built", the §3 table row) | FR-1, FR-2, AU-1, AU-6, §7 Q1, Q8, Q9 | the issue's "how would you know": a seeded mock failure files one issue and starts one run; a second firing over the same window files nothing; an IAM fix is filed `needs-owner` with no run; the reviewer's own failure is not in the next digest | none | no |

**Order.** AU-1, AU-4, AU-6, AU-8 have no dependency and start together.
AU-2 and AU-5 wait for the owner's answers and then for dev-iam; their code
lanes do not wait for the apply. AU-3 after AU-1 and AU-2; AU-7 after AU-1;
FR-1 after AU-2; FR-2 after AU-4; FR-3 last. AU-9 only on Q6's answer.

**The two lanes that need the owner at dev-iam:** AU-2 (L-2 and the secret)
and AU-5 (L-1). AU-9 needs the owner's contract acceptance, not dev-iam.

---

## 7. Questions for the owner

Asked in the lane's `questions.json`, recommended option first. Nothing in §6
that depends on one starts before it is answered.

* **Q1 — what crosses tenants into the reviewer.** failure-review.md §1.
* **Q2 — may agents file issues and comment?** (a) per registration,
  `agents_may_file_issues`, admin-set, off by default; (b) every agent with
  git tools; (c) only the platform files issues, agents never.
* **Q3 — workflow files.** (a) a `-git-wf` slot read by swarm-api alone,
  used only for `platform: true` repositories, and every workflow change held
  `NEEDS_OWNER` at merge; (b) the same slot, merged under the run's gate like
  any file; (c) keep `WORKFLOWS_PATH` and the hand-pushed patch.
* **Q4 — Cloud Logging.** (a) a log view filtered to SwarmCloud's resources,
  `viewAccessor` on the view only (L-1); (b) no Cloud Logging, Firestore and
  stored log tails only; (c) project-wide `roles/logging.viewer` (reads the
  other team's logs; not recommended).
* **Q5 — answers by GitHub comment.** (a) yes, from a member mapped by the
  existing comment-approval path; (b) console and plugin only.
* **Q6 — memory and time.** (a) contract request 113, `claude-code-large`,
  picked by name; (b) raise `standard` through a runner-size version; (c)
  neither.
* **Q7 to Q9** are failure-review's: hold scope, merge gate, budget
  (failure-review.md §6, §7).
