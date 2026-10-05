# Registered repositories and the repository index

**Status: PROPOSED 2026-10-04, design and mock-ups only (functionality wave 4,
lane RI0).** Nothing described here is built. The owner asked on 2026-10-04
for "a section where we register new repos to work in and we have an agent
index the repo on a set interval or when a new change is detected and
provides that context to any planning job that is given an issue to work on
... Including what tests should be run based on the committed change about to
be turned into a PR". This document is the design that request needs before
any code, so the owner can pick; the screens are drawn, in three variants
each, in [web-ui/mockups/repositories.html](web-ui/mockups/repositories.html).
The frozen-contract half is one request, written out in §6.3 for filing; it
is not filed by this lane.

What the design settles, in one line each, with the section that carries the
detail:

* **A registered repository is a per-tenant Firestore document** naming one
  GitHub `owner/repo`, its default branch, the runner profiles allowed to
  work in it and its index schedule. The credential is the tenant's existing
  `swarm-tenant-<tenant>-git` secret, never a new one (§1).
* **The index is a JSON document plus a rendered summary**, keyed by the
  commit sha it describes, stored under the tenant's own GCS prefix, with a
  hard size budget so it always fits a planner's prompt (§2).
* **An index run is an ordinary task**: queued, admitted, leased and counted
  like any other, so a tenant's indexing competes with its own work and with
  nobody else's (§3).
* **The issue-run planner gets the index of the issue's repository**; any
  other step asks for it by name; a `tests:select` query maps a diff's
  changed paths to the tests that cover them (§4).
* **An index older than the head it describes says so**, every time it is
  served, and is never silently treated as current (§5).
* **Phase 1 needs no frozen-contract change.** Staging the index into an
  arbitrary step's workspace (phase 2) needs one runner input on
  `claude-code` and `codex`, which is a request, not an edit (§6.3).

---

## 0. Why a registry at all

Today a repository is not a thing the platform knows about. It is a string on
a task: `TaskCreate.repository_url`, checked by one rule,
`check_repository_url` (`apps/swarm-api/swarm_api/validation.py:792`), which
accepts any https, ssh or `git@` URL that carries no credential. An issue run
derives its repository from the issue reference (`IssueRef`,
`apps/swarm-api/swarm_api/validation.py:854`; GitHub only, `ISSUE_FORGE_HOSTS`)
and reads the forge for that one run: the issue for the preview, and the
repository's open issues and pull requests for the planner
(`read_open_work`, `apps/swarm-api/swarm_api/forge.py:531`). Nothing outlives
the run. Every planner therefore starts from nothing: it clones the
repository and spends the first part of its budget discovering the layout,
the test conventions and the territory rules that the previous planner on the
same repository discovered an hour earlier.

That is the cost the owner's request removes. Anything that runs on a
schedule or reacts to a push needs something to hang the schedule on, and
"the set of repository URLs that happen to appear on recent tasks" is not a
set anyone chose. So the index needs a registry, and the registry is the
smallest thing that can carry a schedule, a credential reference and a
default branch.

---

## 1. A registered repository

A registered repository is a **per-tenant** record: tenant `eng` registering
`example-org/example-api` says nothing about tenant `ops`, which may register
the same repository and gets its own record, its own index and its own index
runs (§2.4 says why indexes are not shared).

| field | meaning | why it is there |
|---|---|---|
| `repo_id` | `repo_` + the first 16 hex of sha256(`tenant_id` + `github.com/` + lower-cased `owner/repo`) | deterministic, so registering twice is idempotent, and a Firestore id cannot contain `/` |
| `tenant_id` | the owner of the record | every read compares it with the caller's tenant and answers a mismatch with the same 404 as a missing record, as `issue_runs` does |
| `forge` | `github` — the only value accepted in phase 1 | the issue fetch, the preview and `read_open_work` all read GitHub's API and nothing else; a second forge is a second client, not a field |
| `owner/repo` | stored as `owner` and `repo`, validated with `IssueRef`'s owner and repository patterns | one spelling with the issue runs, so an issue run finds its registration by equality, not by URL parsing |
| `repository_url` | derived, `https://github.com/<owner>/<repo>`, and passed through `check_repository_url` | a registration must name a URL a task would have accepted, so nothing the registry hands a task is refused later |
| `default_branch` | read from the forge at registration, overridable by an admin of the tenant | the change trigger watches it and the index describes its head; a fork that works on `develop` says so here |
| `allowed_profiles` | runner profile **names** that may work in the repository, default `["claude-code"]` | a registration narrows what a tenant runs against a repository; it never widens what a profile is (invariant 10) |
| `index` | the schedule and trigger settings of §3, plus a pointer to the current index (§2.3) | the registry is where a schedule lives |
| `created_by`, `created_at`, `updated_at` | the caller's email and timestamps | the record of who asked |

**The credential is the tenant's existing forge token,
`swarm-tenant-<tenant>-git`, by name.** It is the secret the worker clones and
publishes with, that swarm-api already reads through
`SecretManagerForgeTokens` (`apps/swarm-api/swarm_api/forge.py:156`) for the
issue preview and the open-work read, and that `scripts/create-secrets.sh
--stdin` stores. Registration does not accept a token, does not store one,
and does not echo one: the record holds no credential field at all.
Registering a repository the token cannot read is refused at registration,
by a read of `GET /repos/{owner}/{repo}` with the tenant's token, answering
`no_access` with the secret's name and no part of its value. Registration is
also where `default_branch` comes from, so the read is not extra work.

**How it relates to today.** Nothing that works today changes. A task with a
`repository_url` that no registration names still runs exactly as it does:
the registry is opt-in context, not a gate. Where a registration exists, three
things use it: an issue run looks up the registration whose `owner/repo`
equals its `IssueRef.repository` and, finding one, gives its planner the index
(§4.1); a task whose `repository_url` matches a registration may ask for the
index by name (§4.2); and the console's Repositories section lists them. Phase
1 does **not** make `allowed_profiles` a submission check for tasks that do
not use the index; turning the registry into a gate is a product decision the
owner may take later, and the field exists so it can be taken without a
migration.

---

## 2. The index

### 2.1 What it contains

The index is what a competent engineer would write on their first day in the
repository, as data. Every field answers a question a planner or an
implementer otherwise spends tokens answering:

| key | content | question it answers |
|---|---|---|
| `commit_sha`, `branch`, `built_at`, `kind` (`full` / `incremental`), `base_sha` | what the index describes | "is this about the code I am looking at?" (§5) |
| `modules` | one entry per package or top-level directory that holds code: `path`, `language`, a one-line `purpose`, `files`, `lines` | "where does X live?" |
| `entry_points` | executables, service mains, CLI commands, workers, `create_app()` factories, with `path` and how they are started | "what runs?" |
| `routes` | public APIs: HTTP routes (`method`, `path`, handler `file`), exported library symbols, MCP tools | "what do callers depend on?" — the impact question |
| `test_layout` | test roots, frameworks, how each suite runs, what needs an emulator or credentials | "how are tests organised here?" |
| `test_map` | source path globs → the tests that cover them, each edge with its `evidence` (`import` / `naming` / `co-change` / `declared`) | "which tests does this change need?" (§4.3) |
| `territory` | ownership hints read from the repository: CLAUDE.md track tables, CODEOWNERS, frozen directories, "do not edit" notes, quoted with their source file | "may I edit this, and who do I tell?" |
| `commands` | build, lint, test and CI commands, read from Makefile, `package.json`, `pyproject.toml` and the CI workflows, each with its source | "how do I prove it works?" |
| `hot_spots` | the files changed most in the last 90 days of the default branch, with change counts and the paths most often changed together | "what is fragile, and what else moves when this moves?" |
| `notes` | at most 20 one-line facts the indexer judged a newcomer must know (an invariant, a frozen contract, a known trap) | "what will bite me?" |

`modules`, `test_layout`, `test_map`, `commands` and `hot_spots` are computed
mechanically (§3.4); the one-line purposes, `territory` and `notes` are the
agent's reading. Every entry that came from a file names the file, so a
consumer can check a claim rather than trust it.

### 2.2 Format and size budget

Two objects per indexed commit:

* **`repo-index.json`**, the structured index, schema `swarm.repo-index/v1`,
  at most **512 KiB**. Validated by the API against a pydantic model
  (`RepoIndexSpec`, phase 1 lane RI2) the way `plan.json` is validated against
  `PlanSpec`: an extra key is refused, naming it, and every list has its own
  bound (`modules` 400, `routes` 1,000, `test_map` 4,000 edges, `hot_spots`
  50, `notes` 20), so one huge field cannot spend the whole file.
* **`repo-index.md`**, the rendered summary, at most **24 KiB**, rendered by
  the API from the JSON (never written by the agent, so it cannot say
  something the JSON does not). 24 KiB is chosen against the planner's prompt:
  `MAX_PLANNER_PROMPT_BYTES` is 64 KiB
  (`apps/swarm-api/swarm_api/issueruns.py:162`) because the claude-code runner
  passes the prompt as one argv string and Linux refuses one argument over
  128 KiB. The open-work section, the instructions and the summary together
  must fit; 24 KiB leaves the open work more than half the remaining budget.
  When the rendering would exceed it, the renderer drops whole sections from
  the end of a fixed order (notes, hot-spots, routes beyond the first 100) and
  says what it dropped, the way the open-work section says "N more open items
  not shown".

A repository too large for the budget is still indexed: the JSON keeps the
module map and the test map at directory granularity and says so in
`truncated: ["modules", ...]`. A truncated index is honest about it; it is
never padded to look complete.

### 2.3 Where it lives, and how it is versioned

The index objects are the indexer task's **artifacts**:

    tenants/<tenant>/tasks/<task>/attempts/<attempt>/artifacts/repo-index.json

That path is already under the tenant's own GCS prefix, written by the
tenant's own worker service account and readable by nobody else's
(invariant 9: own GSA, own secrets, own GCS prefix, own namespace). Using the
artifact path rather than a new `tenants/<tenant>/repos/` prefix means phase 1
needs no new bucket, no new IAM binding and no new writer: the worker uploads
it like any artifact, and swarm-api reads it back like it reads `plan.json`.
The catch is retention: an index is an artifact and lives as long as
artifacts do. Lane RI2 reads the bucket's lifecycle rule and, if it is shorter
than the longest schedule a registration may set, copies the current index to
`tenants/<tenant>/repos/<repo_id>/index/<commit_sha>/` under the same tenant
prefix instead.

**Versioned by commit sha.** When an index run succeeds, swarm-api validates
the JSON, renders the markdown, and in one Firestore transaction writes an
`index_versions/{commit_sha}` entry under the registration (task, attempt,
object paths, the JSON's sha256 digest, `kind`, `built_at`) and moves
`repositories/{repo_id}.index.current_sha` to it — **only if** the new sha is
the branch head or a descendant of the current one, so a slow full run
finishing after a newer incremental one cannot move the pointer backwards.
The last 20 versions are kept; older entries are deleted with their objects
left to the artifact lifecycle. A consumer reads a version by sha and checks
the digest, so an artifact rewritten after promotion is detected rather than
served (§5.2).

### 2.4 Why two tenants on one repository do not share an index

It would halve the indexing cost, and it is refused. A shared index is an
object one tenant's agent wrote and another tenant's agent reads: a channel
between tenants, which invariant 9 exists to close. Two tenants can also see
different things in one repository (one token may read a private submodule
the other cannot), so a shared index would leak what one tenant's credential
could reach. The cost of indexing twice is paid instead.

---

## 3. How the index is built

### 3.1 The indexer step

An index run is **one ordinary task**, submitted by swarm-api on the tenant's
behalf through the same signed `submit_tasks` path every task takes:

| setting | phase 1 value | why |
|---|---|---|
| runner profile | `claude-code` | the only enabled agent profile; a purpose-built profile would be a frozen-contract change (§6.3, optional request B) and is not needed to start |
| resource class | the profile's own, `standard` | indexing is reading and one tool run, not a build |
| `repository_url` / `repository_ref` | the registration's URL and the exact sha being indexed | the index describes a commit, not a moving branch |
| `timeout_seconds` | 1,800 for a full run, 900 for an incremental one | may only shorten the profile's timeout; a run that cannot finish in 30 minutes is a repository that needs the directory-granularity fallback, not more time |
| `priority` | −50 | below the tenant's default-0 work: an index makes work better, it is not the work |
| `metadata` | `{"repo_index": "<repo_id>", "commit_sha": "<sha>", "index_kind": "full"}` | how the promotion path (§2.3) finds a finished index run |
| prompt | fixed, composed by swarm-api; never a caller's text | invariant 10: the caller chooses a repository and a schedule, nothing that runs |

It writes exactly one file, `$SWARM_ARTIFACTS_DIR/repo-index.json`, and
changes nothing in the repository; it does not publish, so it opens no pull
request and pushes no branch.

**At most one index run per registration is in flight.** A trigger that
fires while one is QUEUED or running records the newer head on the
registration (`index.pending_sha`) and does nothing else; when the running one
ends, the newest pending sha is indexed, once. Ten pushes in a minute cost one
run, not ten.

### 3.2 Indexing is ordinary capacity-accounted work (invariants 1-3)

Nothing about indexing bypasses admission, and that is deliberate: the
alternative — a side channel that runs index jobs outside the scheduler — is
exactly how a platform ends up with work nobody counted.

**Invariant 1.** A scheduled or triggered index run is a QUEUED task, which
creates no infrastructure demand: no Job execution, no pod, nothing pending.
A registration with a schedule and no due run is a Firestore document and
nothing else. The schedule never creates a pending pod as a backlog; it
creates a QUEUED task, or records a pending sha on an existing one.

**Invariant 2.** The index run's lease is acquired by the same
`acquire_lease_in_transaction` as any task, reserving every pool its profile
needs (global, tenant, profile, provider) all-or-nothing in one Firestore
transaction. Indexing adds no pool and reserves none of its own.

**Invariant 3.** It counts against the tenant's `max_active` from the moment
it is LEASED, like any task, so a schedule that fires for twenty repositories
at once cannot oversubscribe a slow-starting tenant. Indexing spends the
tenant's own capacity and only the tenant's own: one tenant's twenty
registrations queue behind that tenant's limits, never another's. A tenant
that wants its capacity for work can pause the schedule (§3.3), and the
console shows index runs among the tenant's agents like any other task, so the
cost is visible where capacity is read.

The worker rules that bind any task bind this one: it checkpoints
periodically (invariant 8), carries a fencing generation and exits without
running if it is stale (invariant 5), and parks on a provider wait rather
than sleeping (invariant 4).

### 3.3 Triggers: an interval, and a change on the default branch

Each registration carries:

| setting | default | range | why |
|---|---|---|---|
| `index.interval_hours` | 24 | 1-168, or `off` | the backstop: a repository whose pushes are never detected is still re-indexed |
| `index.on_change` | `poll` | `poll`, `webhook` (phase 3), `off` | the default branch moving is the event the owner named |
| `index.min_change_interval_minutes` | 30 | 10-1,440 | a busy repository merging every five minutes is indexed at most twice an hour by change; the interval still applies |
| `index.full_every_days` | 7 | 1-30 | incremental runs drift; a weekly full run resets them |
| `index.paused` | false | — | an operator stops all indexing of the repository without deleting the record |

**Polling the forge (phase 1).** A per-tenant Cloud Scheduler job,
`repo_index_poll`, calls `POST /v1/admin/repositories/poll?tenant_id=<tenant>`
every 5 minutes, the pattern `issue_run_advance` already uses
(`terraform/modules/scheduler/jobs.tf:214`), and like it says
`managed-by=swarm-terraform` in its description because a Cloud Scheduler job
has no labels. The route reads, for each of the tenant's registrations whose
`on_change` is `poll`, `GET /repos/{owner}/{repo}/commits/{default_branch}`
with the tenant's token and the last response's `ETag`; GitHub answers an
unchanged branch `304 Not Modified`, which does not count against the
token's rate limit, so polling forty repositories every five minutes costs
almost nothing. The route stores `head_sha` and `head_read_at` on every
registration it read — the head is what staleness is measured against
(§5) — and queues an index run where the head moved, the minimum change
interval has passed and no run is in flight. The interval trigger is checked
in the same pass: `last_indexed_at + interval_hours` passed means queue one.

**A webhook (phase 3, optional).** A GitHub `push` webhook would cut the
detection delay from five minutes to seconds. It is not first because it is
the only part of this design that needs an **unauthenticated** route on
swarm-api (GitHub cannot present a Google ID token), verified instead by an
HMAC over the body with a per-registration webhook secret held in Secret
Manager. That is a new kind of ingress, and polling every five minutes is
already fast enough for "the planner has today's index". The webhook, when
built, only records a pending head; the same poll route queues the run, so
the two triggers cannot double-queue.

### 3.4 Incremental and full

A **full** run reads the whole tree. An **incremental** run is given the
previous index's JSON (staged as an `input_from` file from the earlier index
task) and `git diff --name-status <base_sha>..<head>`, and rewrites only the
entries for changed paths, carrying the rest forward with their original
`commit_sha` on each module so a consumer can tell which entries are fresh.

Incremental is chosen when all of these hold, and full otherwise: a previous
index exists for an ancestor of the head; the diff touches fewer than 300
files; no build or test configuration changed (`Makefile`, `pyproject.toml`,
`package.json`, lockfiles, anything under `.github/workflows/`, a `conftest.py`
or a test runner config), because those change what `commands` and
`test_map` mean everywhere; and the last full run is younger than
`full_every_days`.

**The mechanical half is a tool, not tokens.** The file tree, line counts,
the language of each module, the import graph that grounds `test_map`
`import` edges, the naming-convention edges (`src/x/y.py` ↔
`tests/**/test_y.py`), the co-change pairs and the hot-spot counts from `git
log --numstat --since=90.days` are deterministic and cheap. Lane RI3 ships
them as one script in the `agent-runtime-base` image, which the indexer
prompt tells the agent to run first; the agent then spends its tokens on what
needs reading: purposes, territory, notes, and checking the edges the tool
was unsure of. That is what keeps a full run on a 2,000-file repository under
the 30-minute timeout.

---

## 4. How the index is consumed

### 4.1 The issue-run planner (#454)

`POST /v1/runs` already reads the forge for the planner and puts the open work
in its prompt between two delimiter lines carrying the run id, as data
(`planner_prompt`, `apps/swarm-api/swarm_api/issueruns.py:591`). The index
joins it the same way, **in phase 1, with no frozen-contract change**: when a
registration matches the issue's `owner/repo` and has a current index,
`planner_prompt` adds a section

    === REPO INDEX <run_id> <commit_sha> ===
    (repo-index.md, at most 24 KiB, with the staleness line of §5 first)
    === END REPO INDEX <run_id> ===

before the open work, inside the same 64 KiB budget, and the run stores
`index_sha` and `index_digest` beside `open_work`, so the plan records which
index it was made from and the console can show it as a "context used" chip.
The planner's instructions gain one sentence: use the index's `test_map` to
fill each step's `tests`, and its `territory` to keep steps out of files the
repository says are frozen. The planner still clones the repository: the
index tells it where to look, not what the code says.

A planner never **waits** for an index. If the registration has no current
index, the planner runs without one and the run says `index: none` with the
reason; if an index run is in flight, the planner uses the current (older)
index with its staleness line. Holding a planner for an index would hold an
issue run on work it can do without.

The compiled implement steps get the slice that concerns them: for each plan
step, the `tests:select` answer (§4.3) for the step's planned `files`, as a
short list in the step's prompt, which `compile_plan` composes today anyway.

### 4.2 Any step in the same repository, by name (invariant 10)

A task or workflow step whose `repository_url` matches a registration asks for
the index **by name**, never by path: phase 2 adds one runner input to
`claude-code` and `codex`,

    "input": {"prompt": "...", "repo_index": true}

and the worker stages it exactly as it stages `input.issue` today
(`apps/agent-worker/agent_worker/issue.py`): it resolves the registration from
the task's **own** tenant and `repository_url`, reads the current version's
objects with the tenant's own service account, checks the digest, writes
`work/repo-index.md` and `work/repo-index.json` beside the checkout — so they
are in no diff and no pull request — and adds one prompt line naming them.
A caller cannot name an object, a sha, a bucket or another repository's index:
the only value is `true`. That is invariant 10 applied to context: a caller
picks a runner profile by name and, now, a context by name, and the platform
resolves everything behind the name. A step asking for an index its
repository does not have starts without it and its prompt line says so; a
missing index is context missing, not an input failure, so it never ends a
task `INPUTS_UNAVAILABLE`.

Phase 2 needs the frozen-contract request in §6.3, because `_CLI_AGENT_INPUTS`
(`apps/common/swarm_common/profiles.py:1100`) declares `issue` as the only
input those profiles take, and swarm-api refuses an undeclared input.

### 4.3 "What tests should run for this diff"

`POST /v1/repositories/{repo_id}/tests:select` takes changed paths and
answers which tests cover them, from the current index's `test_map`:

    {"paths": ["src/api/routes/users.py", "src/api/models.py"]}
    ->
    {"index_sha": "...", "head_sha": "...", "behind_by": 3, "stale": false,
     "tests": [{"target": "tests/api/test_users.py", "command": "uv run pytest tests/api/test_users.py -q",
                "because": ["src/api/routes/users.py"], "evidence": "import"}],
     "always": [{"target": "tests/unit/test_contract.py", "because": "declared in CLAUDE.md"}],
     "unmapped": ["src/api/models.py"],
     "fallback": "uv run pytest tests/api -q"}

`unmapped` is the honest half of the answer: a changed path the map has no
edge for is listed, and the `fallback` is the narrowest suite whose directory
contains it, never an empty list that reads as "no tests needed". Paths are
data: the route matches them against the index and never touches a
filesystem, so a `../` in one is a path that matches nothing.

Who calls it:

* **implement and fix steps** — in phase 1 through their prompt (§4.1), in
  phase 2 through the staged JSON, which carries the whole map so the agent
  can select for its own diff after it is written. CLAUDE.md asks an agent to
  run the offline unit tests for what it touched; this is how it knows which.
* **the merge step** (docs/merge-step.md, gated on #342) — before merging, it
  asks for the pull request's changed paths and records in its checklist
  which selected tests ran in CI and which did not. **Advisory in v1**: the
  merge step's gate stays the required GitHub checks (merge-step.md §5.2); a
  selection derived from an index an agent wrote is context, not a gate.
* **the operator**, through the console's repository page and an MCP tool
  (`swarm_repo_tests`, lane RI5), before turning a local commit into a pull
  request.

---

## 5. Staleness

### 5.1 An index older than the head it describes says so

Every place an index is served carries three values: `index_sha` (what it
describes), `head_sha` with `head_read_at` (the default branch's head when last
polled) and `behind_by` (commits between them, from GitHub's compare API,
read when the head is polled). The rules:

* **current** — `index_sha == head_sha`.
* **behind** — the index describes an ancestor of the head. Served, with the
  line "This index describes `<index_sha>`, N commits behind `<head_sha>`
  (read <time>); files changed since: …" first in every rendering, and up to
  50 of the changed paths, so a consumer knows which parts to distrust.
* **stale** — behind by more than 200 commits, older than 7 days, or the
  index's sha is not an ancestor of the head (a force-push): served with
  `stale: true`, the console draws it in the warning colour, and an index run
  is queued if none is in flight.
* **unknown** — the head has never been read (polling off, or the token lost
  access): the freshness is a dash with that reason, never "current".

A consumer is never handed an index without these, so "the index said there
were no tests for that file" can always be answered with "the index was 40
commits old and the file was added since".

### 5.2 Security notes (minors; functionality first)

Tenant isolation is the one property that is not a minor, and §2.3 and §2.4
are how it holds: the index is written and read only under the tenant's own
prefix, with the tenant's own credentials, and never shared. The rest are
minors, listed so they are decided rather than discovered:

* **The index is agent-written text.** A repository's own content can steer
  the indexer, and an agent in the same tenant can overwrite an artifact
  after it is written. The digest recorded at promotion catches the second;
  the first is bounded by the schema (data fields only, no instructions
  field), by the planner's delimiters, and by the prompt line calling it
  data. A minor because it is the same tenant's own repository and agents.
* **Paths and purposes are masked like open work.** Every string goes through
  the same `neutral_line` masking `read_open_work` applies before it is
  stored or rendered, with the tenant's token as a literal to redact.
* **The webhook secret** (phase 3) is a Secret Manager secret per
  registration, never a field on the record, never in a log line.
* **`tests:select` reveals file paths** to anyone in the tenant, which the
  repository's clone already does.

---

## 6. API sketch

### 6.1 Routes

All tenant-scoped exactly as issue runs are: the caller's tenant is resolved
from their identity, and another tenant's `repo_id` is a 404
indistinguishable from a missing one.

| route | does |
|---|---|
| `POST /v1/repositories` | register: `{"repository": "owner/repo", "default_branch"?: "...", "allowed_profiles"?: [...], "index"?: {...}}`; reads the forge once (§1); idempotent on `repo_id` |
| `GET /v1/repositories` | the tenant's registrations with freshness (§5), last index run, schedule and `test_map` coverage |
| `GET /v1/repositories/{repo_id}` | one registration, with its last 20 index runs and the runs and workflows that used its index |
| `PATCH /v1/repositories/{repo_id}` | schedule, trigger, `allowed_profiles`, `default_branch`, `paused` |
| `DELETE /v1/repositories/{repo_id}` | unregister; typed confirmation in the console; index objects are left to the artifact lifecycle |
| `POST /v1/repositories/{repo_id}/index:run` | queue an index run now (`{"kind": "full" \| "incremental"}`); the in-flight rule of §3.1 applies |
| `GET /v1/repositories/{repo_id}/index` | the current index's summary and freshness; `?sha=` for a kept version; `?format=json` for the structured document |
| `POST /v1/repositories/{repo_id}/tests:select` | §4.3 |
| `POST /v1/admin/repositories/poll?tenant_id=` | the Cloud Scheduler job's route (§3.3); admits only the scheduler's OIDC identity |

### 6.2 Firestore documents

Kept by their own module, read and written there only, not through
`store.py` or `codec.py`, for the reason `issue_runs` gives
(`apps/swarm-api/swarm_api/issueruns.py:135`): the shape is not the frozen
contract's and must not leak into it.

    repositories/{repo_id}
      tenant_id, forge, owner, repo, repository_url, default_branch,
      allowed_profiles, created_by, created_at, updated_at,
      index: {interval_hours, on_change, min_change_interval_minutes,
              full_every_days, paused,
              current_sha, current_digest, last_indexed_at, last_kind,
              head_sha, head_read_at, behind_by, etag,
              pending_sha, in_flight_task_id, coverage}

    repositories/{repo_id}/index_versions/{commit_sha}
      task_id, attempt_id, json_object, md_object, digest, kind, base_sha,
      built_at, bytes, truncated

    repo_index_runs/{task_id}
      tenant_id, repo_id, commit_sha, kind, trigger (interval|change|manual),
      state (mirrors the task), queued_at, ended_at, end_cause

    issue_runs/{run_id}                     (existing; two new optional fields)
      index_sha, index_digest

`repo_index_runs` duplicates a little of the task for one reason: the
repository page lists a repository's index runs, and a query over all tasks by
a metadata key is a composite index on a collection every other lane writes.
Indexes: `repositories (tenant_id, owner, repo)` and
`repo_index_runs (tenant_id, repo_id, queued_at desc)`.

### 6.3 The frozen contract

**Phase 1 needs no change.** Registrations, index runs and promotion are
swarm-api documents; the indexer is `claude-code`; the planner gets the index
through its prompt, which is composed in swarm-api; no state, event type,
model or admission rule changes. An index run's task is a `Task` like any
other, its events are the task's events.

**Phase 2 needs one request (A)**, to be filed in
[contract-change-requests.md](contract-change-requests.md) when the owner
picks this design (next free number at the time of writing: 47). It is
written here, not filed, because this lane's territory is this document and
the mock-up:

* *What is true today:* `_CLI_AGENT_INPUTS` in `profiles.py` declares one
  input, `issue`, for `claude-code`, `codex` and `claude-code-review`, and
  swarm-api refuses any other input key on those profiles.
* *The requested change:* add `"repo_index": RunnerInput("boolean", means=
  "stage the tenant's current index of the task's own repository as
  work/repo-index.md and work/repo-index.json, named in the prompt")`.
* *What it would break if accepted:* nothing existing; the input is optional
  and false by default. The worker gains a staging step beside
  `stage_issue`, and `tests/unit/mcp/test_runner_inputs.py` gains the key.
* *If it is declined:* only steps whose prompt swarm-api composes (the
  planner and the compiled issue-run steps) get the index; a hand-written
  workflow step does not, and the owner's "accessed by other workflows or
  tasks working in the same repo" is met only for issue runs.

**Optional request (B), only if the owner wants it:** a `repo-indexer`
runner profile in `RUNNER_PROFILES` — the same image, a smaller resource
class, a 30-minute timeout, no `publish` — so index runs are visible as their
own profile in capacity and cost views and can be given their own profile
pool ceiling. Not needed to build anything above.

---

## 7. Build plan

Each lane is one territory, so no two lanes edit the same file (CLAUDE.md:
territory, not subject, splits work). Lanes in one phase run side by side;
a phase starts when the one before it has merged.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| RI1 | 1 | registrations: the `repositories` module, `POST/GET/PATCH/DELETE /v1/repositories`, the registration forge read, tenant scoping, unit tests | new `repositories.py` and `routes/repositories.py` in `apps/swarm-api/swarm_api/`, the router line in `main.py`, tests | — |
| RI2 | 1 | index runs and promotion: `RepoIndexSpec`, the indexer prompt, `index:run`, promotion with the sha-order rule and digest, `repo_index_runs`, the markdown renderer, `GET .../index`, `tests:select` | new `repoindex.py` in `apps/swarm-api/swarm_api/`, the index routes in `routes/repositories.py`, tests | RI1 |
| RI3 | 1 | the mechanical extractor script in `agent-runtime-base` (tree, import-graph and naming test edges, co-change, hot-spots) and its tests | `images/agent-runtime-base/`, `tests/unit/worker/` | — |
| RI4 | 2 | triggers: `POST /v1/admin/repositories/poll`, ETag polling, interval check, in-flight coalescing; the per-tenant `repo_index_poll` Cloud Scheduler job (description says `managed-by=swarm-terraform`) and its OIDC grant | the poll in `repoindex.py`, `terraform/modules/scheduler/` | RI2 |
| RI5 | 2 | consumption: the planner's REPO INDEX section and `index_sha` on the run, `tests:select` slices in compiled step prompts, the `swarm_repo_tests` MCP tool | `apps/swarm-api/swarm_api/issueruns.py`, `plugin/` | RI2 |
| RI6 | 2 | the console section from the owner's pick in the mock-ups: Repositories list, Register form, repository detail, the "context used" chip on a run and in an agent's Details | `apps/swarm-ui/` | RI1, RI2 |
| RI7 | 3 | `input.repo_index` staging in the worker, after request A is accepted and applied | `apps/agent-worker/agent_worker/`, the frozen-contract edit by the owner | request A |
| RI8 | 3 | optional: the GitHub push webhook with per-registration HMAC secret | `apps/swarm-api/`, `terraform/modules/secret_manager/` | RI4, owner's go-ahead |

Every lane ships with the mutation rule CLAUDE.md states: tests pushed first
and red in CI, then the change. RI2 and RI8 are where a review belongs
(credentials, tenant isolation): RI2 because promotion decides what every
later consumer trusts, RI8 because it is the only unauthenticated route.

**Not decided here, for the owner:** which mock-up variants to build (the
page's recommendation is a recommendation); whether `allowed_profiles`
should ever gate submission (§1); whether request B is wanted; and whether
the webhook is wanted at all once polling is live.
