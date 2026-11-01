# CLAUDE.md — working in this repository

Read `CONTRACT.md` first; this file is the working practice around it. Every
rule is here; the long explanation behind each lives in
[docs/working-in-this-repository.md](docs/working-in-this-repository.md).

## Rule zero: deliver everything that was asked, in full

Every task in the request is delivered. Not most of it, not the easy part, not
a different task that resembles it. If something genuinely cannot be done, or
should be dropped or deferred, **ask** — do not decide it silently and do not
bury it in a status list. If you said you would do it, it is a debt; close it
([owner, 2026-09-23](docs/working-in-this-repository.md#rule-zero)).

**Empty output is not success.** A loop that printed nothing, a probe that
returned `[]`, a sweep that found no defects: verify it RAN before reporting
from it. zsh does not word-split unquoted variables, so `for x in $LIST`
iterates once; feed loops from an array or a `while read` and print the count
you visited. Report **done and verified** (command, result), **attempted** and
**not started** (with why) apart; never hide the third inside the first.

## Never stop while unblocked work remains

A blocked step is not a blocked task. A block is **real** only when it is a
permission denial on that exact action, a credential only the owner can supply
or a decision that is genuinely theirs; your own caution, track ownership or an
ordering you invented are **artificial** and not blocks. Decompose, do every
unblocked piece before reporting, report the block in one line, and prepare the
blocked step's dependants so that what remains is one command
([example](docs/working-in-this-repository.md#unblocked-work)).

## The three rules that are not negotiable

**1. `apps/common/swarm_common/` is FROZEN.** Import from it. Never redefine its
types, never edit the files. If you are convinced it needs to change, **say so
in your report or PR description instead of changing it**.

**2. `saga-agents-staging` is a SHARED project.** It holds another team's live
GKE cluster (`agents-staging`), their VPC (`agents-staging-vpc`), their buckets
and service accounts, every one in `SHARED_DENY_LIST` (once, in
`scripts/lib/common.sh`). Nothing you write may delete, modify or even point
`kubectl` at any of them.

**3. Nothing ships as a TODO, a stub or pseudocode.** Everything must run. A
`NotImplementedError` in a dispatch path is a production outage with a comment
attached.

## Track ownership

Several tracks work here at once
([table](docs/working-in-this-repository.md#track-ownership)); read other
tracks' code freely and, on a layout conflict, change your side and report it.

**TRACK OWNERSHIP IS NEVER A REASON TO REFUSE OR DEFER WORK.** It is a
convention for keeping parallel lanes from overwriting each other — nothing
more. When the owner asks for something, do it, wherever it lives, and say in
the commit which track's files you crossed into and why. Reclassify the area if
that is what the work needs. Never answer a request with "that belongs to
another track"; never quietly narrow a task to the part that sits inside your
own area. Owner's instruction, 2026-09-23: *"I dont care what track it is... I
want to make sure this is done and we never refuse or circumvent work that we
said we will do."*

## Invariants to check before you claim a change is done

From `CONTRACT.md`. If a change touches any of these, say explicitly how it
preserves them:

1. Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create infrastructure demand.
   `QUEUED`, `PARKED`, `READY` cost nothing. Never use pending pods as a backlog.
2. Capacity is reserved **all-or-nothing** across every pool, in one Firestore
   transaction.
3. Concurrency counts from `LEASED`, not `RUNNING`.
4. Workers never sleep through a long provider wait — checkpoint, park, release,
   exit.
5. Every attempt carries a fencing generation; a stale worker exits **without**
   running the agent and without touching the lease.
6. Spot is disabled platform-wide.
7. Memory and ephemeral storage: request equals limit. CPU: request ≤ limit ≤ `CPU_BURST_MAX_RATIO` × request, on GKE only (Cloud Run renders the request as its one limit), so the pod's QoS class is Burstable (amended 2026-10-20, contract request 108, #1689).
8. Checkpointing is mandatory and periodic.
9. Per-tenant isolation: own GSA, own secrets, own GCS prefix, own namespace.
10. API callers pick a `runner_profile` **by name**. Never accept images,
    commands, resource specs or backend parameters from a caller.

## Writing scripts

([why each rule](docs/working-in-this-repository.md#writing-scripts))

* `#!/usr/bin/env bash` with `set -euo pipefail` as the **first effective
  line** (the first that is not the shebang, a comment or blank);
  **shellcheck-clean** (`shellcheck -x scripts/*.sh scripts/lib/*.sh`);
  executable.
* Source `scripts/lib/common.sh` for project, region, paths, the deny-list, the
  redaction filter, the unlabelable-type list and the plan guard; the Makefile
  and workflows go through the scripts too. Never restate a rule elsewhere.
* jq: **`false // true` is `true`**; compare explicitly (`.enabled == false`).
* **A command substitution runs in a subshell**: `X="$(api_request ...)"` loses
  what the function assigned (`API_STATUS`). Redirect to a file instead.
* **bash 3.2.57**: no associative arrays, no `mapfile`, no `${var,,}`; expand
  possibly-empty arrays as `${arr[@]+"${arr[@]}"}`. The interactive shell is
  **zsh**: use arrays or explicit quoting.
* Resolve tools with `kubectl_bin` and `prefer_local_bin` from `common.sh`: old
  `kubectl` clients on `$PATH` **silently drop manifest fields**.
* Anything destructive requires a **typed** confirmation and ignores
  `SWARM_ASSUME_YES`. Pipe any output that could hold a credential through
  `redact`.

## Writing Terraform

([why each rule](docs/working-in-this-repository.md#writing-terraform))

* One root, `terraform/infra`, parameterised by
  `terraform/environments/<env>/<env>.tfvars`.
* Every resource carries `managed-by=swarm-terraform` (`make destroy` aborts
  without it). `terraform fmt` before committing; `make lint` runs
  `fmt -check`.
* Never put secret material in Terraform; `scripts/create-secrets.sh` owns
  secret values.
* **A forge token (a GitHub PAT or App key) is never written to this
  repository**, a tfvars file, a Job environment or a log. It lives only in
  Secret Manager as `swarm-tenant-<tenant>-git`, stored with
  `scripts/create-secrets.sh --stdin`. Owner rule, 2026-09-25.

## Writing Python

* Python 3.11, `uv` for everything (`uv run pytest tests/unit -q`). Import the frozen contract; do not restate
  its types.
* Unit tests run with **no cloud credentials and no emulator**.
* Never build a Firestore client at import time; expose `create_app()` and let
  uvicorn use `--factory`
  ([why](docs/working-in-this-repository.md#writing-python)).

## Writing docs

`docs/` explains **why**, not just what. These stay in the docs, unsoftened
([in full](docs/working-in-this-repository.md#writing-docs)): Cloud Run
ephemeral disk is **Preview** and **disables live migration**; Spot cannot
coexist with Autopilot extended run time; `searchTransitiveGroups` 403s here,
so tenant resolution checks membership **per registered group** and every call
needs `x-goog-user-project`.

**Correction (workspace storage).** This platform does NOT use Cloud Run
ephemeral disk: workspaces are memory-backed tmpfs on the fully-GA path, which
DOES support live migration. Mandatory periodic checkpointing stays — for a
quota park-and-exit, a cancellation, a reconciler reclaim of a stale
generation, an ordinary crash — and is never relaxed because migration is
available ([why](docs/working-in-this-repository.md#workspace-storage)).

## Before you say you are finished

* The operator's laptop authors code and opens PRs; tests, builds and deploys
  run in CI, so it runs no `make test`, `make lint`, `pytest`, `vitest` or
  build, not one file; it reports the CI run's conclusion, never a local exit
  code ([laptop](docs/working-in-this-repository.md#laptop)).
* Prove a test catches its defect by committing the failing assertion and
  letting CI show it red ([laptop](docs/working-in-this-repository.md#laptop)).
* What each CI workflow runs and what offline `make test` covers:
  [CI jobs](docs/working-in-this-repository.md#ci-jobs). A change to admission,
  dispatch or reconciliation also runs `make smoke concurrency-test race-test`
  against a deployed environment, then the
  [checklist](docs/working-in-this-repository.md#make-test-smoke-and-the-checklist).

**A SwarmCloud agent is not this machine.** An agent in a SwarmCloud container
(image `agent-runtime-base`) runs the offline unit tests for what it touched
before it finishes and fixes what they catch, then `scripts/publish-scan.sh`;
CI is still the gate. Under the worker's pre-publish gate (claude-code) it does
**not** run `scripts/changed-guards.sh`: the gate runs it after the turn and
hands back what fails; without the gate (codex) it runs it once at the end. A
brief for a SwarmCloud step must not repeat "do not run tests"; a brief for a
local lane still must (#2981; [in
full](docs/working-in-this-repository.md#swarmcloud-agent-tests)).

**While it works, that agent runs only the tests related to its change**,
through `scripts/related-tests.sh` (`--run` to run them). Never vitest without
file arguments; never pytest over a whole directory or with `-n auto` across
the suite; never parallel shards or background test batches polled with
`sleep` ([docs/ci.md](docs/ci.md#while-it-works-a-swarmcloud-agent-runs-only-the-related-tests)).

Then check your own work: nothing edited under `apps/common/swarm_common/`;
another track's files crossed only with the reason in the commit; every new
script shellcheck-clean with `set -euo pipefail`; no TODO, stub or
`NotImplementedError`; nothing that can print a secret, token or environment
variable; nothing that names, modifies or deletes a deny-listed resource; every
new Terraform resource labelled `managed-by=swarm-terraform`; every changed
default explained next to its value.

## Lanes: how an agent works here

Cost is turns × context ([measured](docs/working-in-this-repository.md#lanes)).

* **Read by window.** Never read a whole file over 1,000 lines; `grep -n` or
  `rg` first, then read 100-300 lines around the hit.
* **Never wait or poll.** No `sleep` loops, `gh run watch`, `--watch`, or
  waiting for a PR to merge; the orchestrator waits for CI.
* **Red first.** Push the tests alone to the PR branch, the fix to
  `<branch>-fix`, and stop.
* **At most ~150 tool calls per agent**, then commit, push WIP to
  `<branch>-wip`, and hand off.
* **Territory.** Ask before editing a file your brief did not name. Scratch
  files go under `scratchpad/<branch>/`.
* **Nothing you add may look like a credential.** Build every fake token, key
  or password at runtime (`secrets.token_hex`), never as one literal; never
  touch a secret-shaped `NAME = "..."` line you do not need to. A SwarmCloud
  agent runs `scripts/publish-scan.sh` before finishing and fixes every
  `path:line rule` it prints; a laptop lane leaves it to CI.
* **One review, where it matters**: credentials, tenant isolation, redaction
  and IAM. Blockers and majors; minors go to the wave epic.
* **A PR labelled `ready` is merged by GitHub's native auto-merge** once its
  required checks are green, only once the merge App is configured
  ([runbook](docs/runbooks/merge-app.md)); until then a `ready` PR merges only
  while an operator runs the merge watcher. A `[swarm] task_` title is refused:
  retitle it first.

## Reporting

Report what you wrote (absolute paths), what you verified (command and result),
every conflict with another track, and any frozen-contract change as a request;
say plainly what you did not verify
([in full](docs/working-in-this-repository.md#reporting)).

## Issues

Findings and later work are issues filed through the forms in
[`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/)
([every rule in full](docs/working-in-this-repository.md#issues)):

* Fill in "what you expected". S0 is reserved for tenant isolation, the shared
  project's resources, capacity accounting, fencing and secrets; a secret is
  reported by where it appeared, never pasted.
* From the CLI, write the form's sections in order and pass its label.
* The title states the defect as a fact.
* **Several findings from one wave go in one epic**, one comment per finding
  naming the file and the call site; a contract, Firestore-shape or product
  change gets its own issue (and a contract one an entry in
  [`docs/contract-change-requests.md`](docs/contract-change-requests.md)).
* **An epic closes on its comments, never on its index**; its body carries no
  task item.
* **`Closes #N` only when it is unconditionally true**; else `part of #N`.
* Findings about other issues go in a PR comment, not the PR body.
* An issue's facts about a moving target are stale the day after: read the live
  value yourself and say when.
* Two issues that edit the same file are one lane, not two.
* The forms' "Where" list follows the nav and their labels follow
  [`.github/labels.yml`](.github/labels.yml);
  `tests/unit/scripts/test_issue_forms.py` holds both.
