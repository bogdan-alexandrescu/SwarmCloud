# CLAUDE.md — working in this repository

Instructions for an AI agent (or a new human) making changes here. Read
`CONTRACT.md` first; this file is the working practice around it.

---

## The three rules that are not negotiable

**1. `apps/common/swarm_common/` is FROZEN.**
Import from it. Never redefine its types, never edit the files. Every other
component was written against those exact types, so a quiet change breaks all of
them at once. If you are convinced it needs to change, **say so in your report or
PR description instead of changing it**.

**2. `saga-agents-staging` is a SHARED project.**
It holds another team's live GKE cluster (`agents-staging`), their VPC
(`agents-staging-vpc`), their buckets and 12 of their service accounts. Nothing
you write may delete, modify or even point `kubectl` at any of them. The
deny-list lives once, in `scripts/lib/common.sh`.

**3. Nothing ships as a TODO, a stub or pseudocode.**
Everything in this repository must run. A function that raises
`NotImplementedError` in a dispatch path is a production outage with a comment
attached.

---

## Track ownership

Several tracks work in this repository at once. Stay inside your own:

| Area | Owner |
|---|---|
| `apps/` | Track A (control plane) / Track B (worker + images) |
| `terraform/`, `kubernetes/` | Track C |
| `docs/`, `scripts/`, `.github/`, `Makefile`, `README.md`, `CLAUDE.md`, `.env.example`, `LICENSE`, `docker-compose.yml` | Track D (operations) |

Read other tracks' code freely — you must, to describe it accurately — but do not
edit it. If another track's layout contradicts yours (for example, where the
Terraform root lives), **change your side to match theirs and report the
conflict**; do not patch theirs.

---

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
7. `requests == limits`. No bursting.
8. Checkpointing is mandatory and periodic.
9. Per-tenant isolation: own GSA, own secrets, own GCS prefix, own namespace.
10. API callers pick a `runner_profile` **by name**. Never accept images,
    commands, resource specs or backend parameters from a caller.

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

## Before you say you are finished

```bash
make lint        # shellcheck + doc links + terraform fmt/validate + tflint + manifests
make test        # unit tests + terraform tests + the guard and parity self-tests
```

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
