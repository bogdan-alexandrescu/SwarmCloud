# Working in this repository: the long explanations

[`CLAUDE.md`](../CLAUDE.md) is read on every turn of every agent working here,
so it carries the rules and a one-line form of each laptop rule, and this file
carries the explanations behind them. A token analysis of 2026-10-20..27 (#2524)
found 97% of the pool's tokens were cache reads of long contexts, and the
25.6 KB `CLAUDE.md` was in every one of them; BGD decided on 2026-10-27 to keep
an agent-facing core of about 10 KB there and move the rest here. No rule was
dropped in the move: every section below is the text `CLAUDE.md` used to carry,
verbatim except for link paths and three words this directory's vocabulary rule
asks for (`tests/unit/scripts/test_docs_agent_vocabulary.py`: the running unit
is an agent), and `CLAUDE.md` still states each rule and links to its section.

---

## Rule zero

The owner's words behind rule zero, kept here because `CLAUDE.md` states the
rule without them:

Owner's instruction, 2026-09-23: "Do not ever forget or exclude tasks without
asking my permission to do so and do not commit to anything you are not going
to deliver or fail to deliver on anything you already committed on doing."

---

## Track ownership

The table and the layout rule behind `CLAUDE.md`'s "Track ownership". Its last
paragraph -- track ownership is never a reason to refuse or defer work -- stays
in `CLAUDE.md` in full.

Several tracks work in this repository at once. Stay inside your own:

| Area | Owner |
|---|---|
| `apps/` | Track A (control plane) / Track B (worker + images) |
| `terraform/`, `kubernetes/` | Track C |
| `docs/`, `scripts/`, `.github/`, `Makefile`, `README.md`, `CLAUDE.md`, `.env.example`, `LICENSE`, `docker-compose.yml` | Track D (operations) |

Read other tracks' code freely — you must, to describe it accurately. Prefer to
edit inside your own area, and if another track's layout contradicts yours (for
example, where the Terraform root lives), change your side to match theirs and
report the conflict.

---

## Unblocked work

A blocked step does not block the whole request. Decide whether the block is **real** — a
permission denial on that exact action, a credential only the owner can supply,
a decision that is genuinely theirs — or **artificial**: your own caution, a
convention like track ownership, an ordering you invented. Artificial blocks
are not blocks.

Then decompose and keep going. "Fix GKE dispatch" is not one piece of work; it is find
the cause, fix the renderer, fix the guard, write the RBAC, apply it, cover it
in smoke, add the logging. On 2026-09-23 exactly one of those was blocked by a
permission classifier and the other six were not. Do every unblocked piece
before reporting anything, and report the block in one line.

Prepare the blocked step's dependants anyway — write the manifest, render it,
validate it, commit it — so that when the block lifts, what remains is one
command.

---

## Writing scripts

Every script in `scripts/` must be:

* `#!/usr/bin/env bash` with `set -euo pipefail` as the **first effective line** —
  the first line that is not the shebang, a comment or blank. CI checks exactly
  that, not a fixed window, so a long explanatory header (which is the house
  style here) is free;
* **shellcheck-clean** (`shellcheck -x scripts/*.sh scripts/lib/*.sh`);
* executable;
* sourcing `scripts/lib/common.sh` rather than re-deriving project, region,
  paths, the deny-list, the redaction filter, the unlabelable-type list, or the
  plan guard. The Makefile and the workflows go through the scripts too
  (`scripts/lib/resolve.sh`, `scripts/lib/plan-guard.sh`): every rule that got
  restated in a second place here has since drifted.

Two shell/jq traps that have already cost this repository a working check:

* **`false // true` is `true` in jq.** The alternative operator treats `false` as
  absent, so `.enabled // true` reports a *paused* pool as open — the one thing
  that column exists to show. Compare explicitly: `.enabled == false`.
* **A command substitution runs in a subshell.** `X="$(api_request ...)"` throws
  away anything the function assigned, so `API_STATUS` stays at its old value and
  every response reads as a success. Redirect to a file instead.

Environment specifics that will bite you:

* **bash 3.2.57** is what macOS ships and what these scripts target. No
  associative arrays, no `mapfile`, no `${var,,}`. Expand possibly-empty arrays
  as `${arr[@]+"${arr[@]}"}` — plain `"${arr[@]}"` on an empty array is an
  unbound-variable error under `set -u` in 3.2.
* The interactive shell is **zsh**, which does **not** word-split unquoted
  variables. Use arrays or explicit quoting.
* Resolve tools explicitly. `kubectl` 1.22 and 1.25 win `$PATH` on this machine
  and an old client **silently drops manifest fields**; Homebrew's checkov 3.3.10
  raises on import and shadows the working 3.3.17. Use `kubectl_bin` and
  `prefer_local_bin` from `common.sh`.
* Anything destructive requires a **typed** confirmation and must ignore
  `SWARM_ASSUME_YES`.
* Pipe any command output that could contain a credential through `redact`.

---

## Writing Terraform

* One root: `terraform/infra`, parameterised by
  `terraform/environments/<env>/<env>.tfvars`. A root per environment is how prod
  quietly drifts from dev.
* Every resource carries `managed-by=swarm-terraform`. `make destroy` aborts on
  anything without it, so an unlabelled resource is one nobody can safely delete
  later.
* `terraform fmt` before committing; `make lint` runs `fmt -check`.
* Never put secret material in Terraform. A managed secret **version** puts the
  plaintext in a state file several people can read. `scripts/create-secrets.sh`
  owns secret values.
* **A forge token (a GitHub PAT or App key) is never written to this
  repository**, a tfvars file, a Job environment or a log. It lives only in
  Secret Manager as `swarm-tenant-<tenant>-git`, stored with
  `scripts/create-secrets.sh --stdin`, and the worker reads it at runtime.
  The repository is public: a token committed once is a token published, and
  rewriting history does not unpublish it. Owner rule, 2026-09-25.

---

## Writing Python

* Python 3.11, `uv` for everything (`uv run pytest tests/unit -q`).
* Import the frozen contract; do not restate its types.
* Unit tests must run with **no cloud credentials and no emulator**. The
  admission logic is pure for exactly this reason — the concurrency invariant is
  the thing most worth testing exhaustively, so testing it must be fast.
* Never build a Firestore client at import time; expose `create_app()` and let
  uvicorn use `--factory`. A module-level client makes every test that imports the
  module need credentials.

---

## Writing docs

`docs/` explains **why**, not just what. A doc that restates the code is dead
weight; a doc that records the constraint behind a decision is what stops the
decision being silently reverted in six months.

Specifically, these must stay in the docs and must not be softened:

* Cloud Run ephemeral disk is **Preview** and **disables live migration** — which
  partially undermines the reason Cloud Run was chosen. Mandatory checkpointing
  compensates; it does not cure.
* Spot cannot coexist with Autopilot extended run time, which is why "Spot
  preferred" is impossible here rather than merely undesirable.
* The Cloud Identity constraint: `searchTransitiveGroups` 403s in this project,
  so tenant resolution checks membership **per registered group** and every call
  needs `x-goog-user-project`.

---

## Laptop

What `CLAUDE.md`'s "Before you say you are finished" says in one line each for
the operator's laptop.

**THIS MACHINE AUTHORS CODE AND OPENS PULL REQUESTS. NOTHING ELSE.**

Tests run in CI. Builds run in CI. Deployments run in CI. Do not run `make
test`, `make lint`, `pytest`, `vitest` or a build here — not the full gate and
not one file of it. The owner has said so four times; the fourth was *"why are
you running tests in here!??? Make it so that tests run in CI, builds run in
CI, deployments run in CI. Here we only author code, create PRs."*

There is no narrow-suite exception. "Just this one file, it takes six seconds"
is what produced the fourth telling.

So the finishing sequence is:

```bash
git commit && git push          # then:
gh pr create                    # or push to an existing PR branch
gh run list --branch <branch>   # read the run
gh run view <id> --log-failed   # read the failure
```

Report the CI run's conclusion. Never report a local exit code, because there
should not be one.

**Proving a test actually catches its defect is still required** — see the
mutation rule below. Prove it by committing an assertion that would fail, and
letting CI demonstrate that, rather than by running the mutation here. A
mutation proven in CI is proven for everyone forever; one proven on this laptop
is proven once.

### CI jobs

What the CI jobs cover, so you know what you are waiting for:

| workflow | jobs |
|---|---|
| `application.yml` | shell · ruff · python (unit; a pull request runs the tests its change reaches) · ui (typecheck + component) · integration (emulator) · manifests · build images (main; skips the tests a run of the identical tree passed) · record the tested tree |
| `terraform.yml` | fmt/validate/tflint · `terraform test` (86 assertions) · checkov · plan (main only) |
| `security.yml` | filesystem · secrets · iac · policy · images (scheduled) |
| `release.yml` | verify · build · infrastructure · deploy (push to main, env-gated) |
| `accept.yml` | what dev runs · warm, smoke and GKE proof · acceptance (6 groups, two waves) · report (a red run opens or updates one `bug` issue); after `release` completes on main. The 7th group, live-browser, runs every three hours off the red path with its own issue until `scripts/accept-live-browser-streak.sh` says five in a row ([docs/ci.md](ci.md#live-browser-runs-on-a-schedule)) |

### make test, smoke and the checklist

`make test` is fully offline — no credentials, no emulator, nothing created. It
runs the unit tests, `terraform test` over `tests/terraform` (86 assertions
against a mock provider), the destroy-guard and plan-guard self-tests, and
`scripts/lib/check-contract-parity.sh`, which asserts that every shell or jq
restatement of the frozen contract still matches the Python.

and, if you touched anything in the admission, dispatch or reconciliation paths,
against a deployed environment:

```bash
make smoke concurrency-test race-test
```

Then check your own work against this list:

* Did I edit anything under `apps/common/swarm_common/`? (Do not.)
* Did I edit another track's files? (Do not — report the conflict instead.)
* Does every new script pass shellcheck and carry `set -euo pipefail`?
* Did I add a TODO, a stub, or a code path that raises `NotImplementedError`?
* Could anything I wrote print a secret, a token or an environment variable?
* Could anything I wrote name, modify or delete a resource on the shared
  deny-list?
* If I added a Terraform resource, does it carry `managed-by=swarm-terraform`?
* If I changed a default, did I say **why** in the file, next to the value?

---

## SwarmCloud agent tests

What `CLAUDE.md` says in one paragraph for an agent running in a SwarmCloud
container, in full.

**A SwarmCloud agent is not this machine.** The rule above is about the
operator's laptop. An agent running in a SwarmCloud container (the claude-code
or codex runner, image `agent-runtime-base`, which carries uv, node/npm and
pytest) **runs the offline unit tests for what it touched before it
finishes** (`uv run pytest tests/unit -q` narrowed to its area, and vitest for
`apps/swarm-ui`), and fixes what they catch. CI is still the gate, and a
green run in the container proves nothing CI has not. Owner decision,
2026-09-28, after 2 of the first 6 SwarmCloud fix PRs arrived red on failures
a unit run would have caught (#248, #249). A brief written for a SwarmCloud
step must therefore not repeat "do not run tests"; a brief for a local lane
still must. Its one pre-finish command is **`scripts/changed-guards.sh`**,
after the area run: it runs the repo-wide guard set (`GUARD_TESTS` in
`scripts/changed-guards.sh`, the one list, which pull-request CI reads too)
and every unit test file that names a changed path, in one `pytest -n auto` call, then
`scripts/lib/check-contract-parity.sh` — about a minute for the guard set
alone, each pytest call held to 540 s. A selection that weighs more than 540
CI-seconds (`unit_test_durations.json`) is not one call: run without
`--shard`, it runs as parts one after another, each its own call under the
540 s limit, so the whole can pass ten minutes; `--shard k/n` runs the k-th
part alone, and an agent whose command limit is ten minutes runs the n parts
as n commands, which the script's warning spells out. A narrowed
area run never reaches those guards, and they were most of the 25% of lane PRs
red on their first CI run (#642). Until 2026-11-01 every SwarmCloud agent ran
it, and any brief, wherever it was written, named it.

**Since 2026-11-01 an agent under the pre-publish gate does not run it.**
Owner decision, 2026-11-01 (#2981), after the code intelligence experiment
measured it. The claude-code runner (`claude-code-lightpanda` included; its
`CliAgentSpec` sets `prepublish=True`, `agent_worker/prepublish.py`) runs
`scripts/changed-guards.sh` itself in its pre-publish gate after the agent's
turn, and hands the agent what fails in a fix turn. On eight identical
workflows whose patch touched 33 files in hot modules, the script selected
645-681 test files: its first run timed out at the 600 s check limit on all 15
steps, the gate's rerun as 10 shards took 55-65 minutes on the reviews, and
the agents, told by their briefs to run it too, hit their own 10-minute
command limit first, about ten more minutes a step for a result the gate then
computed again. So an agent on that runner runs the related tests and
`scripts/publish-scan.sh` and leaves `scripts/changed-guards.sh` to the gate;
the runner's prompt note (`RELATED_TESTS_NOTE`) and its repeat warning
(`guards_repeat_hook.py`) say so. A runner without the gate -- codex, the
only other coding-agent runner (`CliAgentSpec`) -- keeps the rule above: its agent
runs `scripts/changed-guards.sh` once as its one pre-finish command, and its
brief names it.

**While it works, that agent runs only the tests related to its change**,
through **`scripts/related-tests.sh`** (`--run` to run them): it names the
pytest files that import or name a changed path and the vitest files that
import a changed UI module, as a few runs of named files. Never vitest without
file arguments; never pytest over a whole directory or with `-n auto` across
the suite; never parallel shards or background test batches polled with
`sleep`. Owner decision 2026-10-20 (#1694): 36 unfiltered vitest runs cost
2.05 h, 8 whole-directory pytest runs 0.73 h, and both out-of-memory kills came
from four vitest shards started with `&`
([docs/ci.md](ci.md#while-it-works-a-swarmcloud-agent-runs-only-the-related-tests)).
The rule then ended "`scripts/changed-guards.sh` once at the end"; since
2026-11-01 (#2981, the paragraph above) that holds only on a runner without
the pre-publish gate (codex), and under the gate (claude-code) the agent
leaves the script to the gate.

---

## Lanes

Measured 2026-09-26 across 773 agents: 8.9 billion cached tokens re-read and ~166
agent-hours, almost none of it output. Cost is turns × context. Whole-file reads
and waiting inside agents made lanes carry 300-500k tokens on every turn.

* **Read by window.** Never read a whole file over 1,000 lines (a hook refuses it).
  `grep -n` or `rg` first, then read 100-300 lines around the hit.
* **Never wait or poll.** No `sleep` loops, `gh run watch`, `--watch`, or waiting
  for a PR to merge. The orchestrator waits for CI at zero token cost, and it
  dispatches a dependent lane only after its dependency has merged.
* **Red first, in this shape.** Push the tests alone to the PR branch, push the fix
  to `<branch>-fix`, and stop. CI cancels an in-progress run when a newer commit is
  pushed, so the red run must finish before the fix lands.
* **At most ~150 tool calls per agent.** Then commit, push (WIP to `<branch>-wip`),
  and hand off to a fresh agent.
* **Territory.** Stop and ask before editing a file your brief did not name. Keep
  scratch files under `scratchpad/<branch>/`.
* **Nothing you add may look like a credential.** The worker refuses to publish a
  diff whose added lines match its credential scan, after all the work is done
  (3 of 6 lanes lost, 2026-10-02). Build every fake token, key or password at
  runtime (`"sk-" + "x" * 40`, `secrets.token_hex`), never as one literal. Never
  touch a secret-shaped `NAME = "..."` line you do not need to — a trailing comma
  re-adds it. A SwarmCloud agent runs `scripts/publish-scan.sh` before finishing
  and fixes every `path:line rule` it prints; a laptop lane leaves it to CI
  (its push never meets the worker; CI's secret scan still reads it).
* **One review, and only where it matters.** Credentials, tenant isolation,
  redaction and IAM get a review. It reports blockers and majors; minors go to the
  wave epic.
* **A PR labelled `ready` is merged by GitHub's native auto-merge** (`.github/workflows/auto-merge.yml`,
  [docs/ci.md](ci.md#a-ready-pull-request-is-merged-by-github-not-by-a-session)) once its required
  checks are green at head, **but only once the merge App is configured**
  ([runbook](runbooks/merge-app.md)). Until then the workflow refuses with a comment and a `ready`
  PR merges only while an operator runs the merge watcher. A `[swarm] task_` title is refused: retitle it first.

---

## Reporting

When you finish, report:

* what you wrote, with absolute paths;
* what you verified, with the command and its result — not "should work";
* **every conflict you found with another track**, especially layout or naming
  mismatches you worked around;
* anything you believe belongs in the frozen contract, as a request rather than
  a change.

Say plainly what you did not verify. An unverified claim in this repository ends
up as a runbook step someone follows at 3am.

---

## Issues

Bugs, fixes and improvements we mean to do later are filed as GitHub issues,
through the forms in [`.github/ISSUE_TEMPLATE/`](../.github/ISSUE_TEMPLATE/). The
forms and the rules below were adopted from saga-prompt-lab on 2026-09-24, at
the owner's request: the structure is theirs, the content is this platform's.
A finding that lives only in a session transcript or a PR description is lost
the day that session ends; an issue is where it survives.

**Use the form, and fill in "what you expected".** It is the box that tells a
bug from documented behaviour, and a lot that looks wrong here is the contract:
a `READY` or `PARKED` agent that holds no capacity and shows no progress is
invariant 1 working. **Severity S0 is reserved for the guarantees this platform
exists to keep** — tenant isolation, the other team's resources in
`saga-agents-staging`, capacity accounting, fencing, and secrets — and an S0 is
looked at before anything else. A secret is reported by *where* it appeared,
never pasted.

**Filing from the CLI with `gh issue create --body-file` bypasses the form, so
write the body in the form's sections, in its order, and pass its label**
(`--label bug`, `--label enhancement`, `--label epic`). The form is the
checklist of what an issue has to answer; skipping it from a terminal is how an
issue ends up with a headline and no expected behaviour.
Blank issues stay on for chores and notes-to-self — `config.yml` says why.

**The title states the defect as a fact**, the way this repository's commit
headlines do — "The release's digest check asked for an ID token #36 had
stopped minting", not "digest check broken". The body says what was measured,
where and when, with the control that shows the measurement could have come out
the other way.

**SEVERAL FINDINGS FROM ONE WAVE GO IN ONE EPIC, NOT ONE ISSUE EACH.** A wave of
parallel lanes turns up mechanical defects outside each lane's territory — a
stale comment, a missed call site, a guard that checks less than its name says.
File them as `[epic] Wave <date> out-of-territory findings` (the epic form,
label `epic`), **one comment per finding**, each a checkbox naming **the file
and the call site**:

    - [ ] **<the defect, as a fact>** · `apps/scheduler/scheduler/dispatch.py` `worker_env()` · <how it was measured, by which lane or PR>

Tick a box only with **evidence**: the PR that fixed it, or a measurement that
its premise was wrong. **Batching is for findings whose fix is MECHANICAL.** A
change to the frozen contract, a Firestore document shape or product behaviour
gets **its own issue**, linked from its box — and a frozen-contract change is
also an entry in
[`docs/contract-change-requests.md`](contract-change-requests.md), because
rule 1 says so.

**AN EPIC CLOSES ON ITS COMMENTS, NEVER ON ITS INDEX.** In saga-prompt-lab,
#871 closed over two live defects its body's index never listed. Enumerate the
boxes from the comments, reconcile them against anything the body lists, and if
the two disagree the epic is not closeable. So an epic's body carries **no checklist
item at all** — not from the CLI, and not from the form, which has no
checkboxes for exactly this reason: GitHub writes a form's checkboxes into the
filed body as `- [X]` items, and a required acknowledgement would arrive as a
ticked box no comment matches, on every epic.

**`Closes #N` ONLY WHEN IT IS UNCONDITIONALLY TRUE.** GitHub reads the keyword
and drops every qualifier around it: "Closes #12 except the GKE half" closes
#12. For a partial fix write `part of #N` and say what is left.

**FINDINGS ABOUT OTHER ISSUES GO IN A PR COMMENT, NOT THE PR BODY.** Keep
reporting them — but an issue cited in the body reads as the PR's own subject,
to GitHub's linked-issues view and to the next reader. Context references for
*this* change stay in the body.

**AN ISSUE'S FACTS ABOUT A MOVING TARGET ARE STALE THE DAY AFTER IT IS
WRITTEN.** A pool ceiling, a lease count, what is deployed, the image a job
runs, which release run is red: read the live value yourself before acting on
an issue that quotes one, and say *when* you read it.
[`docs/DEPLOY_STATE.md`](DEPLOY_STATE.md) is dated for this reason — its
2026-09-24 reading found the running services were a laptop deploy of
`7c52762`, not main. An issue that said "main is deployed" would have become
false without anyone editing it.

**TWO ISSUES THAT EDIT THE SAME FILE ARE ONE LANE, NOT TWO.** Territory, not
subject, decides how work is split, and it is found by reading the files the
issues name rather than their titles. Two lanes handed `App.tsx` for two
different reasons overwrite each other exactly as if they had been handed the
same one.

**The forms' "Where" list follows the nav, and a test holds it there.**
`tests/unit/scripts/test_issue_forms.py` reads `SECTIONS` in
`apps/swarm-ui/src/App.tsx` and fails when a tab is added, renamed or removed
without the forms following — a form still offering "Runner profiles" after the
tab became "Profile headroom" sends the reporter looking for a screen that is
not there. The same test holds every label a form applies to
[`.github/labels.yml`](../.github/labels.yml). It cannot read GitHub, so a label
deleted there is not caught; create a missing one from its entry in that file
with `gh label create`.

---

## Workspace storage

**Correction (workspace storage).** This platform does NOT use Cloud Run ephemeral
disk. The Terraform google provider cannot express it (`empty_dir.medium` accepts
only `"MEMORY"`), so workspaces are memory-backed tmpfs and the deployment runs on
the fully-GA path, which DOES support live migration.

Mandatory periodic checkpointing therefore remains required, but for different
reasons than originally written: a worker can still lose its attempt to a quota
park-and-exit, a cancellation, a reconciler reclaim of a stale generation, or an
ordinary crash. Checkpointing is what makes any of those cost minutes instead of
the whole attempt. Do not relax it on the grounds that live migration is now
available — migration covers infrastructure moves, not the application-level
interruptions above.
