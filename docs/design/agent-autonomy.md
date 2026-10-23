# Agent autonomy: from a prompt to a merged and deployed change, inside SwarmCloud

**Status: DESIGN, not built.** Lane AUTONOMY-DESIGN, 2026-10-22, part of
#1933. The implementation lanes are §6. The owner answered this design's
questions on 2026-10-22 (~05:42Z, recorded on task_08e785ae3e9949d6b86b); each
answer is written where its question was, as "Owner decision, 2026-10-22", and
§7 lists them with the few questions still open. Nothing here changes
behaviour until a lane in §6 builds it.

**The request (BGD, 2026-10-22 ~04:45Z):** "also make sure we include in this
plan the changes required to our agent runtimes so they can access github, the
api and all the other mandatory or optional but needed dependencies and
capabilities to be able to run independently from this session or from claude
code, all inside swarmcloud, able to work and deliver a fix or feature end to
end from prompt to merge and deploy".

Two designs are built on this one, and §6 plans the lanes of all three:

* the schedule type that is the first user of all of it, `failure-review`,
  [failure-review.md](failure-review.md) (BGD's request (1));
* visual QA of **any** web application, its findings filed as issues in that
  application's repository, [visual-qa-any-app.md](visual-qa-any-app.md)
  (BGD's request (3)).

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
| 10 | read its own tenant's SwarmCloud state (agents, workflows, events, results, artifacts, the stored log tail) | **missing** | `write_mcp_config` gives the agent `playwright`, `jev`, `swarm-git`, `swarm-search`, `swarm-graph` and relayed registered servers (`agent_worker/runners/claude_code.py` `write_mcp_config`, :799); the read routes (`routes/tasks.py` `/events` :623, `/artifacts` :759, `/logs` :938, `/questions` :1061) need a person's scope; the only attempt-scoped reads are `/v1/attempts/{id}/context/inflight` and `/context/graph` (`routes/attemptcontext.py`) |
| 11 | read Cloud Logging for its tenant's attempts | **missing** | nothing in `apps/` reads Cloud Logging (no `google.cloud.logging`, `entries:list` or `logadmin`); swarm-api's account holds only `roles/logging.logWriter` among logging roles (`terraform/modules/iam/bindings.tf`, `telemetry_roles`) |
| 12 | dispatch child agents | **exists** | the spool and `routes/children.py` (:90); at most 16 per parent (`settings.max_children_per_task`), depth 1 (`ChildDepthExceeded`), the parent's repository |
| 13 | dispatch a child workflow or issue run | **missing** | `ChildSpec` takes `runner_profile`, `input`, `resource_class`, `provider`, `model`, `model_class`, `timeout_seconds`, `repository_ref` only (`swarm_api/children.py`, :185-191) |
| 14 | park on CI_PENDING and resume on a wake, spending nothing | **exists** (the merge step) | `merge.py` parks `PARKED/CI_PENDING` with `metadata.merge_wait` (:1067), woken by the per-tenant tick `POST /v1/admin/merges/wake` (`routes/admin.py` :1112, `mergewake.wake_tenant` :1166), no lease held while parked |
| 15 | fix rounds on red CI | **exists** | merge step `fix_rounds` 0-5 (`MERGE_FIX_ROUNDS_MAX`, `merge.py` :212); an issue run's CHECKING ⇄ FIXING loop, 1-5 rounds, default 3 ([issue-runs.md](../issue-runs.md)); the excerpt from `issueci.excerpt_at` (:850), redacted, bounded to 8 KiB, between nonce markers (`issueci.ci_fix_continuation`, :953-962) |
| 16 | merge | **exists** | the merge step, only on a review `verdict == "MERGE"` (`merge.py`, :1145); `merge: auto` only through the audited switch `POST /v1/schedules/{id}:merge-mode` (`routes/approvals.py`, :317-375, audited `gate_merge_auto` in `schedaudit.py`) |
| 17 | see its change deploy (release status, deployed digest) | **missing** | only `GET /v1/version` (`routes/platform.py`, :34-66), whose `git_sha` names the commit that built the image, not the release that deployed it; `release-health` opens issues on red streaks and reports nothing to an agent |
| 18 | ask the owner a question and get the answer with no session open | **partial, one-way** | `questions.json` is validated and uploaded (`agent_worker/questions.py`), shown in the console (`apps/swarm-ui/src/QuestionsCard.tsx`) and the bell's "needs you" (`swarm_api/notifications.py`, :488-520), answered through `POST /v1/tasks/{id}/answers` into `metadata.owner_answers` (`swarm_api/agent_questions.py`); nothing in the worker or scheduler reads an answer back (`questions.py`: "DATA, NEVER A CHANNEL"), and nothing reaches the owner outside the console |
| 19 | toolchain: uv, gh, gcloud, git, node/npm, ripgrep | **exists** | `images/agent-runtime-base/Dockerfile`: uv 0.12.15 (:53), Node 24 with npm 11 (:47, :261-273), gcloud 587.0.0 (:582), gh 2.102.0 (:638), git and ripgrep (:243-257), terraform, checkov, shellcheck (:899) |
| 20 | toolchain: `apps/swarm-ui` node_modules | **missing** | no image installs them; every UI agent runs `npm ci` itself, from the network, inside its own time limit |
| 21 | toolchain: Playwright and browsers | **partial** | only `agent-runtime-browser` (Playwright 1.63.0, Chromium only, `images/agent-runtime-browser/Dockerfile` :43, :71-76), reached by `browser` and `claude-code-browser`; `claude-code` has none |
| 22 | memory | **partial** | every code profile is `standard`, 8 GiB, request = limit (`swarm_common/profiles.py`); `large` (32 GiB) was reached by no profile until `claude-code-large` (the `large` class, 14,400 s, contract request 113, 2026-10-22) ([runner-sizes.md](runner-sizes.md) §4.1); #1926 made an OOM fail the agent at once and say so (`reconciler/memory.py`), it did not add memory |
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
  rule, [schedules.md](../schedules.md) §3.4). An optional `dedupe_key`
  widens that across attempts and runs: swarm-api writes its hash as a
  marker and, when an open issue already carries it, answers
  `{existing: N}` and files nothing, so the caller comments on `N` instead
  ([visual-qa-any-app.md](visual-qa-any-app.md) §5 is the first user).
* **`issue_comment`** — `number`, `body`. Refused on a closed issue unless
  `reopen: false` is stated, and refused on a pull request (that is the merge
  step's and the review's channel).

Bounds: at most 10 issue writes per attempt and 30 per agent across its
attempts, so a looping agent files a page, not a flood. A body is scanned with
`swarm_redaction.redact` and **refused** (`leak_found`) rather than silently
masked when it matches, because an issue is public in a public repository.

**Who may write.** Reads are every agent's that has git tools. Writes are a
rise in GitHub reach for every agent, so they were the owner's (Q2). **Owner
decision, 2026-10-22: "Per repository registration, admin-set, off by
default".** An agent may write issues and comments only when the tenant's
registration of the repository says `agents_may_file_issues: true`. Only a
workspace admin sets it, the change is audited (`admin_audit`,
`agents_may_file_issues_set`), and it is off for every registration that does
not say otherwise. The platform's own registration (`platform: true`) is set
on. With it off, both tools answer `issue_writes_off`, naming the setting.

**A repository with no issue forms.** [visual-qa-any-app.md](visual-qa-any-app.md)
files into applications' repositories, and most have no
`.github/ISSUE_TEMPLATE/`. `issue_create` then takes `form: "plain"`: swarm-api
renders the given `sections` as `### <name>` headings in the order given, and
refuses `plain` in a repository that **has** forms (`issue_form_required`), so
the platform's own repository is still filed in its form. Labels: an
`issue_create` may name `labels`, and swarm-api applies only those that
already exist in the repository (it reads them once per call); it never
creates a label, and the ones it dropped are named in the answer.

### 3.3 Pushing `.github/workflows` files (row 9)

Today the tenant slot cannot, and [schedules.md](../schedules.md) §4.4 refuses
such a plan outright (`WORKFLOWS_PATH`). The design keeps the agent
credential-free and makes the push the broker's.

**As decided by BGD on 2026-10-22 (#2029, lane WF-APP-TOKEN), superseding the
`-git-wf` slot of Q3's first answer:**

1. **The SwarmCloud GitHub App mints the token at push time.** swarm-api
   (`forge_installation.InstallationTokens.workflow_push_token`) mints an
   installation token for ONE repository (`repositories=[<repo>]`) with
   exactly `contents: write` and `workflows: write`, from the workspace's
   OWN installation (its `forge_orgs` documents, never another tenant's and
   never an owner reached through a person's `method: pat`; invariant 9). It
   is minted fresh for each push and never cached, stored, logged or handed
   to a worker, and lives at most an hour. BGD: "can we use the github app
   creds for this? This way we can point a similar job in the future at
   another project or repo."
2. **Self-serve, per-repo opt-in.** A `workflow_files` switch on each
   repository registration, off by default (only a stored `true` is on;
   `platform: true` alone is not), set by the workspace's admin when the
   repository is added or later, each change an `admin_audit` entry
   (`repository_workflow_files_set`) in the same transaction, as for
   `agents_may_file_issues`. BGD: "when adding a new repo to SwarmCloud, the
   per-repo token is created for the new repo as well, self serve".
3. **The `-git-wf` slot and IAM row L-2 are removed in the same lane**
   (BGD chose this over removing them after a live proof). The release holds
   at `dev-iam` for the L-2 removal.
4. **The App asks for "Workflows: Read and write".** CLD changes the App's
   settings at GitHub and each installation's owner accepts. An installation
   that has not accepted reads as "Workflows: not granted" in the console
   (forgeperms, on a switched-on repository only), and its pushes fall back
   to the patch.

**The risk, and why the switch is per repository and off by default:** a
workflow on a pushed branch runs with that repository's secrets BEFORE any
merge hold. A hold on the merge does not stop a `push`- or
`pull_request`-triggered workflow on the branch itself, so opting a
repository in is a decision about that repository's secrets, made by its
workspace's admin, one repository at a time.

How it works:

* **Used only by `git_push_branch`**, and only when the pushed range touches
  `.github/workflows/**` or `.github/actions/**` and the agent's own
  repository's registration (its tenant's, never another's) has
  `workflow_files` on. Otherwise -- or when the mint fails -- the push is
  refused with a code of its own, `workflow_scope_missing`,
  naming the files (`detail.paths`) and ending "Push without those files and
  write them as workflow-lines.patch", instead of GitHub's generic
  `remote_rejected`. Each mint failure is its own reason inside it: the
  installation has not granted Workflows, does not cover the repository, or
  there is no installation. The audit row names `credential:
  app_installation`, never the token.
* **The end-of-agent publish** routes a workflow-touching branch through the
  same broker instead of the worker's own push (`lifecycle.py` `_publish_git`),
  and a refusal fails the agent with `PUBLISH_FAILED_FIELD`, writing the diff
  as `workflow-lines.patch` in its artifacts. A lane can no longer end
  SUCCEEDED with its workflow change unpushed.
* **The plan refusal becomes a hold where the switch is on.** A plan naming a
  workflow file is held `STOP_WORKFLOWS`, `NEEDS_OWNER`, instead of refused: a
  workflow can reach CI's secrets and the deployer's identity, so merging one
  stays the owner's. With the switch off, `WORKFLOWS_PATH` stays exactly as
  it is.

**Owner decision, 2026-10-22 (Q3), the part that stands: "merge held
NEEDS_OWNER".** Every merge of a change under `.github/workflows/` or
`.github/actions/` in a repository whose switch is on -- and, as before, in a
`platform: true` repository, where a person may still push a patch by hand --
is `NEEDS_OWNER` (`STOP_WORKFLOWS` stays in `ENFORCED_STOPS`), whatever the
run's merge gate says. Q3's "a -git-wf slot read only by swarm-api, platform
repo only" was replaced by the four decisions above.

### 3.4 Reading its own tenant's SwarmCloud state (row 10)

A new MCP server in the worker, **`swarm-state`**, with the same shim-and-spool
shape as `swarm-git` (spool `work/.swarm-state/`), relayed by the worker
(`ChildPath`, beside the git and search relays, so its fenced exit is
theirs) and signed with the attempt key under **either of two proofs**:

* **`swarm-state-req/v1`** (`agent_worker/statecall.py`), when `state/open`
  registered the key for the `state` purpose -- a tenant switched to the state
  routes;
* **`swarm-child-req/v1`** (`children.request_message`), when the key carries
  only the `child` purpose -- registered at `/child-key`, as every tenant not
  switched to the state routes does.

**Owner decision, 2026-10-22 08:54Z (BGD): "Also accept the child-purpose
proof (swarm-child-req/v1) on the read route".** Accepting only the state
proof would have offered the reads to `u-bogdan` alone, and a failure-review
agent in `eng` could not read the failures it reviews. The wider proof widens
nothing else: either proof is verified against **this attempt's**
registration for **its** purpose, over the path **with** its query, and every
check below is the same for both. A failure of both is one 403
(`attempt_unproven`), so the answer says nothing about which purposes a key
carries. A key from an unprotected heap (registered for `state` only) still
cannot sign under the child purpose, and a `/child-key` key still cannot
sign the state WRITE routes: the acceptance is the read route's alone. The
worker offers the reads to every attempt whose worker holds a key, and
answers `no_attempt_key` only when it holds none (lane AU4-WIRE).

swarm-api serves it on new attempt-scoped routes,
`GET /v1/attempts/{attempt_id}/reads/{tool}`, each of which:

1. authenticates the worker and verifies the proof (`authenticate_worker`,
   then the state proof, else the child proof), and checks in one read that
   the lease is live for this attempt and generation (stale:
   `attempt_fenced`, which ends the attempt through the worker's fenced exit
   without touching the lease, invariant 5);
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
  condition), not project-wide. Owner at dev-iam (§3.12).

**Owner decision, 2026-10-22 (Q4): "A log view filtered to SwarmCloud
resources, viewAccessor on the view only".** Row L-1 is decided as written;
its apply is still the owner's, at `dev-iam`, when lane AU-5's release
reaches it.

**Built, then found impossible on `_Default`.** AU5-LOG-VIEW built the view
(`terraform/infra/log_view.tf`), and release run 39742374084 (2026-10-22)
failed creating it: `Error creating LogView: googleapi: Error 400: Filter
exceeds the view filter nesting depth limit of 1.` Google's LogView reference
allows a view filter only a logical AND of `=`/`!=` checks on the originating
project, folder, organization or billing account, the resource type and the
log id, so no view on `_Default` can select by cluster or namespace, and the
other team's `agents-staging` cluster writes to that bucket. The view and the
L-1 binding are therefore off (`swarm_workers_log_view_enabled`, default
`false`, lane AU5-VIEWOFF), and `logs_query` answers `logs_unavailable`, its
documented refusal, until the owner decides the redesign (#1933).

**Redesigned: a bucket of its own, fed by a sink (BGD, 2026-10-22, #1960).**
A sink filter takes the full query language where a view filter cannot, so
the selecting moved to a sink. `terraform/infra/log_bucket.tf` creates the log
bucket `swarm-worker-logs` in the project's region with 30-day retention and a
project sink, `swarm-worker-logs`, that routes
into it only SwarmCloud's workers: `k8s_container` in the swarm cluster in
namespaces `swarm-tenant-*`, and `cloud_run_job` named `swarm-job-*`. It never
names the other team's cluster, not even to exclude it. The sink gets no IAM
grant, so L-1 is the only IAM row: BGD decided on 2026-10-22 (#1960) to drop
the writer's `roles/logging.bucketWriter`, because Google authorizes a sink
that writes a log bucket in its own project ("Route logs to supported
destinations", Set destination permissions), as bootstrap's `workspace_apply`
sink already relies on. The view keeps its id, `swarm-workers`, now on that bucket, with
the simplest filter a view takes (`SOURCE("projects/<project>")`); L-1 is
unchanged. Two consequences. Worker logs are stored twice: `_Default` keeps
its copy, and the new bucket holds a second for 30 days. And the platform's
own components (`swarm-system`, the `swarm-*` services) are no longer in the
view, so failure-review reads workers' logs only through it. The switch is on
in dev and off in prod; `tests/terraform/log_view.tftest.hcl` holds all of it.

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

**Owner decision, 2026-10-22 (Q5): "Yes, '/answer <n> <option>' from a member
mapped by the comment-approval path".** A reply comment is accepted only from
a GitHub login that `ghapprove` maps to a member of the agent's workspace with
the authority `agent_questions.py` already requires, on the agent's own issue,
for a question that is still open; any other `/answer` is ignored and
recorded (`answer_comment_ignored`, with the login and the reason), never
acted on. `<option>` must be one of the question's option labels.

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

* **Memory.** #1926's measurement was 8 GiB OOMs on `claude-code`. There were
  two routes, both the owner's because both are spend: a runner-size version
  raising `standard` memory (Admin → Runner sizes, [runner-sizes.md](runner-sizes.md)
  §4.5, no contract change, every standard agent pays), or a new profile
  `claude-code-large` on the `large` class (32 GiB), picked by name by a
  planner that knows the lane is heavy (a contract change, request 113 in
  [contract-change-requests.md](../contract-change-requests.md)).
  **Owner decision, 2026-10-22 (Q6): "Accept contract request 113: a
  claude-code-large profile".** Lane AU-9 applies it; nothing else changes
  size.
* **Time.** The same request gives `claude-code-large` 14,400 s. A timeout
  is otherwise left as it is: a two-hour lane is usually a lane that should
  have been two, and `failure-review` reports each timeout's cause rather
  than raising the ceiling for all.
* **Who picks it.** A caller or planner names `claude-code-large` like any
  profile (invariant 10). An issue run's planner names it for a step whose
  issue was filed from an out-of-memory kill or a timeout (`failure-review`'s
  cause class, failure-review.md §4); the tenant's `large` admission ceiling
  then decides whether it runs now or queues (invariants 2 and 3).

### 3.12 New routes, tools, profiles, IAM and secrets, in one place

| kind | name | owner at dev-iam |
|---|---|---|
| swarm-git tools | `pr_view`, `run_list`, `run_view`, `issue_view`, `issue_list`, `release_status`, `issue_create`, `issue_comment` | no |
| MCP server | `swarm-state` (worker shim) | no |
| routes | `GET /v1/attempts/{attempt_id}/reads/{tool}` | no |
| child kinds | `issue_run` (with `after_merged`), `workflow` (saved spec by name) | no |
| park uses | `DEPENDENCY_INCOMPLETE` + `metadata.release_wait`; `HUMAN_REQUIRED` + `metadata.question_wait` | no |
| registration field | `agents_may_file_issues` (admin-set, off by default; owner decision Q2) | no |
| registration field | `workflow_files` (workspace-admin-set, off by default, audited; BGD 2026-10-22, #2029) | no |
| runner profile | `claude-code-large` (contract request 113, accepted by the owner, Q6) | no IAM; the contract change is accepted, lane AU-9 applies it |
| Terraform | log view `swarm-workers` on `_Default` | **yes**, with L-1 |
| **IAM L-1** | swarm-api's account: `roles/logging.viewAccessor` on the view `swarm-workers` only | **yes** (decided, Q4; applied by the owner) |
| ~~**IAM L-2**~~ | **Removed** (BGD, 2026-10-22, #2029): swarm-api's `secretmanager.secretAccessor` on `swarm-tenant-<platform tenant>-git-wf`. A workflow-file push uses an App installation token minted per push; no IAM row replaces it | **yes**, the removal holds at `dev-iam` |
| ~~**secret**~~ | **Removed** (#2029): `swarm-tenant-<platform tenant>-git-wf` and `create-secrets.sh --git-wf` | no |

Visual QA of any app adds its own rows (a QA target's credential secret and
its IAM, V-1 and V-2): [visual-qa-any-app.md](visual-qa-any-app.md) §7, and
they are still open questions (§7 below).

The forge scopes of the existing `-git` slot do not change. No agent gets a
broader GitHub right except through the owner's answers in §7.

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
   (`NEEDS_OWNER`, label `needs-owner`). With no hold, the implementer steps
   run, and because the cause was a timeout the planner names
   `claude-code-large` (§3.11) for the heavy step, by name:
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

## 6. The lane plan (all three documents)

Dependency order: capabilities first, then the two schedule types and the
visual-QA build on them. **No two lanes that can run at the same time share
a file**: where two lanes need one file, the later lane depends on the
earlier, and the table names the file in both rows. A lane that finds it
needs a file not in its row stops and asks. Every lane is red first (its
tests pushed alone, then the fix on `<branch>-fix`), its unit tests run with
no cloud credentials and no emulator, it builds every fake token at runtime,
and it ends with `scripts/changed-guards.sh` and `scripts/publish-scan.sh`.

### 6.1 Capabilities (this document)

| lane | builds | territory | depends on | red-first tests | IAM / Terraform | owner at dev-iam |
|---|---|---|---|---|---|---|
| **AU-0 REG-FLAG** | §3.2's `agents_may_file_issues` (owner decision Q2) | `apps/swarm-api/swarm_api/repositories.py`, `apps/swarm-api/swarm_api/routes/repositories.py`, `apps/swarm-ui/src/Repositories.tsx`, new `tests/unit/control_plane/test_agents_may_file_issues.py`, new `apps/swarm-ui/src/__tests__/repositories.file-issues.test.tsx` | none | a registration without the field reads off; a `platform: true` registration reads on; a member's change is 403; an admin's change writes `agents_may_file_issues_set` to `admin_audit` in the same transaction; the console shows and sets it for an admin only | none | no |
| **AU-1 GIT-TOOLS** | §3.1, §3.2 (with `form: "plain"`, `dedupe_key`, existing-only labels), §3.3's broker half, §3.7 | `apps/swarm-api/swarm_api/gittools.py`, `apps/swarm-api/swarm_api/forgewrite.py`, `apps/agent-worker/agent_worker/gitmcp/server.py`, `apps/agent-worker/agent_worker/gittools.py`, `docs/runner-git-tools.md`, new `tests/unit/control_plane/test_git_tools_reads.py`, `test_git_tools_issue_writes.py`, `test_git_tools_release_status.py`, `test_git_tools_workflow_slot.py` | AU-0 (the flag it reads) | a `run_view` on another repository is `other_repository`; a body from GitHub comes back between nonce markers; with the flag off `issue_create` is `issue_writes_off`; without "what you expected" it is `issue_form_incomplete`; `plain` where forms exist is `issue_form_required`; a second `issue_create` with the same `dedupe_key` answers `existing` and files nothing; a label the repository lacks is dropped and named, never created; an 11th write is refused; a body matching a redaction rule is `leak_found`; a workflow-touching push with no `-git-wf` version is `workflow_scope_missing`, not `remote_rejected`; `release_status` reports a run waiting at `dev-iam` | none | no |
| **AU-2 WF-SLOT** | §3.3's slot (owner decision Q3) | `terraform/infra/git_tools.tf`, `scripts/create-secrets.sh` (`--git-wf`), new `tests/terraform/git_wf_slot.tftest.hcl`, new `tests/unit/scripts/test_create_secrets_git_wf.py` | none | `terraform test`: the slot exists for the platform tenant only, empty, labelled `managed-by=swarm-terraform`, readable by swarm-api's account and by no worker account; the script refuses a value on argv and reads it only from `--stdin` | **L-2**, the secret | **yes** |
| **AU-3 PUBLISH-WF** | §3.3's publish half | `apps/agent-worker/agent_worker/lifecycle.py` (`_publish_git` only), new `tests/unit/worker/test_publish_workflow_files.py` | AU-1, AU-2 | a publish whose branch touches `.github/workflows/` goes through the broker; a refused one ends with `PUBLISH_FAILED_FIELD` and a `workflow-lines.patch` artifact, never SUCCEEDED | none | no |
| **AU-4 STATE-READS** | §3.4, §3.5's route | new `apps/swarm-api/swarm_api/attemptreads.py`, new `apps/swarm-api/swarm_api/routes/attemptreads.py`, `apps/swarm-api/swarm_api/main.py` (one `include_router`), new `apps/agent-worker/agent_worker/statemcp/`, `apps/agent-worker/agent_worker/runners/claude_code.py` (`write_mcp_config`), new `tests/unit/control_plane/test_attempt_reads.py`, new `tests/unit/worker/test_state_mcp.py` | none | another tenant's agent id answers exactly as a missing one; a stale generation is `attempt_fenced`; the tenant comes from the lease, not the body; a log tail is redacted; `logs_query` with no view configured is `logs_unavailable`; a filter a caller sends cannot name another namespace; every tool is read-only | none | no |
| **AU-5 LOG-VIEW** | §3.5's view and IAM (owner decision Q4) | new `terraform/infra/log_view.tf`, new `tests/terraform/log_view.tftest.hcl` | none | `terraform test`: the view's filter admits only `swarm-tenant-` and swarm system namespaces and `swarm-` Cloud Run names; the binding is on the view, never on the project; the role is `viewAccessor`; the view carries `managed-by=swarm-terraform` | **L-1** | **yes** |
| **AU-6 CHILD-RUNS** | §3.6 | `apps/swarm-api/swarm_api/children.py`, `apps/swarm-api/swarm_api/routes/children.py`, `apps/agent-worker/agent_worker/children.py`, `docs/design/child-tasks.md`, `docs/child-tasks-for-agents.md` (the `AGENT_GUIDE` copy), new `tests/unit/control_plane/test_child_runs.py` | none | a child `issue_run` on another repository is refused; a `workflow` child naming an unapproved spec is refused; a spec body is refused; an `after_merged` run holds no agent until its predecessor is DONE and merged; both count toward the 16 and the depth rule | none | no |
| **AU-7 WAITS** | §3.8, §3.9 (owner decision Q5) | `apps/agent-worker/agent_worker/questions.py`, `apps/agent-worker/agent_worker/lifecycle.py` (the `await-answers` and `await-release` exits only), `apps/swarm-api/swarm_api/agent_questions.py`, `apps/swarm-api/swarm_api/mergewake.py` (the release-wait read), `apps/swarm-api/swarm_api/ghapprove.py` (`/answer`), `apps/swarm-api/swarm_api/notifications.py`, `apps/swarm-ui/src/QuestionsCard.tsx`, new `tests/unit/worker/test_blocking_questions.py`, new `tests/unit/control_plane/test_answer_promotes.py`, `test_release_wait.py`, `test_answer_comment.py`, new `apps/swarm-ui/src/__tests__/questions.blocking.test.tsx` | AU-1 (`release_status`, `issue_comment`), AU-3 (`lifecycle.py`) | a blocking question parks HUMAN_REQUIRED with no lease; it is posted on the agent's issue with its options and the reply syntax; the last answer promotes to READY and the resumed attempt finds `answers.json` between nonce markers; `/answer` from a login `ghapprove` does not map to a member is ignored and recorded; an option that is not one of the labels is ignored; 72 h unanswered dead-letters with `owner question unanswered`; a release wait wakes on a terminal release and not before | none | no |
| **AU-8 IMAGE-NPM** | §3.10 | `images/agent-runtime-base/Dockerfile`, new `tests/unit/scripts/test_base_image_npm_cache.py` | none | the Dockerfile builds the cache from the UI lockfile, discards `node_modules`, and sets the cache read-only with offline-first installs | none | no |
| **AU-9 PROFILE-LARGE** | §3.11 (owner decision Q6, contract request 113 accepted) | `apps/common/swarm_common/profiles.py` (the one entry, **accepted by the owner** 2026-10-22), `docs/design/runner-sizes.md` §1a.3, `docs/contract-change-requests.md` (row 113 rewritten to "ACCEPTED by the owner 2026-10-22 and APPLIED by the pull request that adds this line": `scripts/lib/check-frozen-contract.sh` passes a `swarm_common` change only on an added line carrying "accepted by the owner" in the same pull request), new `tests/unit/common/test_claude_code_large.py` | none | `claude-code-large` names `large`, 14,400 s, GKE, on-demand, memory `requests == limits`, the catalogue's checkpoint interval; every other profile is unchanged | none | no IAM; the contract change is already the owner's accepted request |

### 6.2 `failure-review` ([failure-review.md](failure-review.md))

| lane | builds | territory | depends on | red-first tests | IAM / Terraform | owner at dev-iam |
|---|---|---|---|---|---|---|
| **FR-1 HOLD-CATEGORIES** | failure-review.md §6's added stops for every issue run in a `platform: true` repository (owner decision Q7), and §3.3's plan hold | `apps/swarm-api/swarm_api/issueruns.py` (`hard_stop_hold` and the `WORKFLOWS_PATH` refusal), new `tests/unit/control_plane/test_hold_categories.py`, new `tests/unit/control_plane/test_workflows_path_hold.py` | AU-2 | a plan naming a workflow file is `NEEDS_OWNER` with the `-git-wf` slot and `WORKFLOWS_PATH` without; a PR touching `scripts/create-secrets.sh` is held `secrets`; `swarm_api/auth.py` `tenancy`; a deny-list name `shared`; a budget cap or resource class `cost`; the same changes in a repository that is not `platform: true` are held only by today's IAM and contract stops; a clean change is not held | none | no |
| **FR-2 DIGEST** | failure-review.md §2, §3, §5 | new `apps/swarm-api/swarm_api/failuredigest.py`, new `apps/swarm-api/swarm_api/failurecauses.py`, new `tests/unit/control_plane/test_failure_digest.py`, `test_failure_causes.py` | AU-4 (its projections), AU-5 for log lines (degrades to none without it) | each skip rule skips exactly its case and names itself; a person's cancel and its cascade are skipped, a system cancel is not; content fields appear only for the platform's own agents (owner decision Q1); the digest is redacted, bounded and carries no prompt; the same failure in two overlapping windows is one entry; a fingerprint is stable across reruns | none | no |
| **FR-3 TYPE** | failure-review.md, the type and its filing | new `apps/swarm-api/swarm_api/schedtypes/failure_review.py`, `apps/swarm-api/swarm_api/scheduletypes.py` (`TYPES`), `tests/unit/control_plane/test_schedules_model.py` (`CATALOGUE`), new `tests/unit/control_plane/test_schedtypes_failure_review.py`, `docs/schedules.md` (§3.16 to "built", the §3 table row) | FR-1, FR-2, AU-1, AU-6 | the issue's "how would you know": a seeded mock failure files one issue and starts one run; a second firing over the same window files nothing; an IAM fix is filed `needs-owner` with no run; the reviewer's own failure is not in the next digest; creation without `merge: auto` from the audited switch keeps `approve`; budget defaults are the owner's tripwires (Q9) | none | no |
| **FR-4 FINDINGS** | failure-review.md §1's per-tenant findings, read side | new `apps/swarm-api/swarm_api/routes/failurefindings.py`, `apps/swarm-api/swarm_api/main.py` (one `include_router`), `apps/swarm-api/swarm_api/notifications.py` (topic `failure_review`), new `apps/swarm-ui/src/FailureFindingCard.tsx`, new `tests/unit/control_plane/test_failure_findings.py`, new `apps/swarm-ui/src/__tests__/failure-findings.test.tsx` | FR-3, AU-7 (`notifications.py`), VQ-2 (`main.py`) | a tenant reads only its own findings; another tenant's id answers as missing; the bell item names the agent and the cause sentence; no platform-tenant text reaches a tenant | none | no |

### 6.3 Visual QA of any app ([visual-qa-any-app.md](visual-qa-any-app.md))

| lane | builds | territory | depends on | red-first tests | IAM / Terraform | owner at dev-iam |
|---|---|---|---|---|---|---|
| **VQ-1 SPEC** | its §4: the renderer and the findings validator | new `apps/swarm-api/swarm_api/qaspec.py`, new `tests/unit/control_plane/test_qaspec.py`, new `docs/qa/any-app-qa.md` | none | a rendered spec passes `swarm_mcp.workflows.read_spec`; it names no console path, `localhost` or `npm`; groups are at most 8 by 8; an `own_login` target's captures carry `--keep-storage`; a `mutating` flow on a `production` target is refused; every step has `allow_empty_diff` and the workflow `merge: off`; no step's input carries a target field (§1.1); a finding without evidence fails validation | none | no |
| **VQ-2 TARGETS** | its §1, §2.1, §2.2's route, §6's `:run` | new `apps/swarm-api/swarm_api/qatargets.py`, new `apps/swarm-api/swarm_api/routes/qatargets.py`, `apps/swarm-api/swarm_api/main.py` (one `include_router`), new `tests/unit/control_plane/test_qa_targets.py`, `test_qa_target_credential.py`, `test_qa_target_attempt_route.py` | VQ-1, AU-0 (the flag `:run` reads), AU-4 (`main.py`) | a member cannot register; `http`, an IP literal, userinfo or an internal host is refused; a run on a `pending` host is `qa_target_host_pending`; changing the host resets approval; the credential value (built with `secrets.token_hex`) appears in no response, log line or Firestore write; a link is single-use, bound to its email, and expires; the attempt route takes the tenant from the lease, answers 204 with no target and as missing for another tenant's; `:run` with the registration's flag off is `issue_writes_off` unless `file: "none"` | none (reads V-2's secret; `qa_credential_unavailable` until VQ-3 is applied) | no |
| **VQ-3 QA-SECRET** | its §7 secret and IAM (VQ-Q2, answered 2026-10-22) | new `terraform/infra/qa_targets.tf`, new `tests/terraform/qa_secret.tftest.hcl` | VQ-Q2 answered | `terraform test`: one `swarm-tenant-<t>-qa` per tenant, empty, `managed-by=swarm-terraform`; swarm-api's account is adder and accessor on each one only; no worker account is bound | **V-1, V-2**, the secrets | **yes** |
| **VQ-4 GATE-SIGNIN** | its §2.2 to §2.4 and §3 | new `apps/agent-worker/agent_worker/qa_target.py`, `apps/agent-worker/agent_worker/handoff.py` (`HandoffPath.prepare`, one call), new `apps/agent-worker/agent_worker/livegate/qatarget.py`, `livegate/ctl.py` (verb `qa_sign_in`), `livegate/cdpproxy.py` (the held sign-in and the value scrub), `livegate/session.py` (export exclusion), `images/agent-runtime-browser/swarm-handoff` (`sign-in`), new `tests/unit/worker/test_qa_target_signin.py`, new `tests/unit/worker/test_qa_target_gate_policy.py` | VQ-2 | the password's bytes, URL-encoded and base64 forms reach no agent-bound frame, including a base64 response body; the agent's commands are held during the sign-in and every password input is cleared before release; an MFA or CAPTCHA page answers `qa_sign_in_challenge` and offers no hand-off; a `POST` to the target is refused, one to a `read_posts` path is not, a non-GET off-origin is refused even in a mutating flow; a top-frame navigation off the approved origins gets the gate page; navigations are spaced by `navigation_ms`; `session_export` excludes the target's origins; `wipe` clears the credential; the worker logs only the target id | none | no |
| **VQ-5 CAPTURE-GENERIC** | its §4.1 discover and §4.2's capture flags | `images/agent-runtime-browser/swarm_qa.py`, `tests/unit/worker/test_swarm_qa.py` | none | `--keep-storage` never runs `RESET_STATE_JS`; `--settle generic` never evaluates `PENDING_JS`; the clock is not frozen unless asked; `discover` stays same-origin, skips exclusions and `robots.txt` `Disallow`, stops at its page and depth caps and writes a valid route list; the console template's captures are unchanged | none | no |
| **VQ-6 CONSOLE-PLUGIN** | its §1 registration screen, §2.1's link form, §6's on-demand run | new `apps/swarm-ui/src/QaTargets.tsx`, new `apps/swarm-ui/src/qaTargetsApi.ts`, `apps/swarm-ui/src/Repositories.tsx` (one mount), new `apps/swarm-mcp/swarm_mcp/qatargets.py`, `apps/swarm-mcp/swarm_mcp/server.py` (three tools), new `apps/swarm-ui/src/__tests__/qatargets.test.tsx`, new `tests/unit/mcp/test_qa_targets_tools.py` | VQ-2, AU-0 (`Repositories.tsx`) | the credential form reads its link token from the URL and removes it from the address bar, as `ProviderKeys.tsx` does; no plugin tool takes or returns a credential; `swarm_qa_target_credential_link` returns only a URL and an expiry; a member sees Run and not Edit; a pending host shows why it cannot run | none | no |
| **VQ-7 TYPE** | its §6 schedule type `visual-qa` | new `apps/swarm-api/swarm_api/schedtypes/visual_qa.py`, `apps/swarm-api/swarm_api/scheduletypes.py`, `tests/unit/control_plane/test_schedules_model.py`, new `tests/unit/control_plane/test_schedtypes_visual_qa.py`, `docs/schedules.md` (a §3.17 entry) | VQ-2, FR-3 (`scheduletypes.py`, `test_schedules_model.py`, `docs/schedules.md`) | a firing runs `:run` for its target; a target of another tenant is refused at creation; the minimum interval is 24 h; a pending host makes the firing a refusal, not a failure | none | no |

### 6.4 Order, and where the owner is needed

* **Wave 1, no dependency:** AU-0, AU-2, AU-4, AU-5, AU-6, AU-8, AU-9, VQ-1,
  VQ-5. AU-2 and AU-5 are code lanes the owner already decided (Q3, Q4); only
  their applies wait for the owner at `dev-iam`.
* **Wave 2:** AU-1 (after AU-0), FR-2 (after AU-4), VQ-2 (after VQ-1, AU-0,
  AU-4).
* **Wave 3:** AU-3 (after AU-1, AU-2), FR-1 (after AU-2), VQ-4 and VQ-6
  (after VQ-2).
* **Wave 4:** AU-7 (after AU-1, AU-3).
* **Wave 5:** FR-3 (after FR-1, FR-2, AU-1, AU-6).
* **Wave 6:** FR-4 (after FR-3, AU-7, VQ-2), VQ-7 (after FR-3, VQ-2).
* **VQ-3** can start: VQ-Q2 was answered on 2026-10-22. Nothing else waits on its lane,
  only own-login sign-in waits on its apply.

**Lanes that need the owner at `dev-iam`:** AU-2 (L-2 and the `-git-wf`
secret; both removed again by WF-APP-TOKEN, #2029, whose release holds at
`dev-iam` for the removal), AU-5 (L-1), VQ-3 (V-1, V-2 and the per-tenant QA secrets). AU-9
edits the frozen contract under the owner's acceptance of request 113 (Q6).
After the merges the owner also: (no `-git-wf` token any more, #2029;
instead each App installation's owner accepts "Workflows: Read and write" and
the workspace's admin switches `workflow_files` on where wanted), creates the `failure-review` schedule and
switches its merge gate to `auto` through the audited switch (Q8). Any
platform admin approves each QA target's host, audited (VQ-Q1).

---

## 7. Questions for the owner

### 7.1 Answered, 2026-10-22 (~05:42Z, recorded on task_08e785ae3e9949d6b86b)

Each is also written where its question was asked.

* **Q1 — what crosses tenants into the reviewer.** Owner decision,
  2026-10-22: "Platform-authored fields for every tenant; content only for
  the platform's own" (failure-review.md §3).
* **Q2 — may agents file issues and comment?** Owner decision, 2026-10-22:
  "Per repository registration, admin-set, off by default" (§3.2;
  `agents_may_file_issues`, the platform's own repository on).
* **Q3 — workflow files.** Owner decision, 2026-10-22: "A -git-wf slot read
  only by swarm-api, platform repo only, merge held NEEDS_OWNER" (§3.3).
  **Superseded later on 2026-10-22 by BGD (#2029):** the GitHub App mints a
  per-push token for one repository with `contents` and `workflows` write,
  from the workspace's own installation; a per-repository `workflow_files`
  switch, off by default, set by the workspace's admin; the `-git-wf` slot
  and IAM row L-2 removed in the same lane; the App requests "Workflows: Read
  and write". "Merge held NEEDS_OWNER" stands. A workflow on a pushed branch
  runs with that repository's secrets before any merge hold, which is why the
  switch is per repository and off by default.
* **Q4 — Cloud Logging.** Owner decision, 2026-10-22: "A log view filtered to
  SwarmCloud resources, viewAccessor on the view only" (§3.5; row L-1, at
  dev-iam).
* **Q5 — answers by GitHub comment.** Owner decision, 2026-10-22: "Yes,
  '/answer <n> <option>' from a member mapped by the comment-approval path"
  (§3.9).
* **Q6 — memory and time.** Owner decision, 2026-10-22: "Accept contract
  request 113: a claude-code-large profile" (§3.11; 32 GiB, 14,400 s).
* **Q7 — hold categories.** Owner decision, 2026-10-22: "Every issue run in a
  platform: true repository" (failure-review.md §6).
* **Q8 — failure-review's merge gate.** Owner decision, 2026-10-22: "merge:
  auto, set once by you through the audited merge switch at creation"
  (failure-review.md §6).
* **Q9 — failure-review's budget.** Owner decision, 2026-10-22: "Tripwires:
  per_run_usd 200, per_day_usd 1000, max_concurrent 1" (failure-review.md
  §7).

### 7.2 Answered, 2026-10-22 (~06:40Z, recorded on task_1eefab7ffc1941d58986): visual QA of any app

[visual-qa-any-app.md](visual-qa-any-app.md) §9 gives each with its options.

* **VQ-Q1 — who approves a QA target's host.** Owner decision, 2026-10-22:
  "Any platform admin, audited".
* **VQ-Q2 — where the test credential lives** (a secret, rows V-1 and V-2).
  Owner decision, 2026-10-22: "One secret per tenant, swarm-tenant-<t>-qa".
* **VQ-Q3 — how a filed issue shows its screenshot.** Owner decision,
  2026-10-22: "Console link by default; evidence branch as a per-target
  opt-in for private repositories".
