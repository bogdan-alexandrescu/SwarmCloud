# Runbook: prove the destroy guard against a real terraform plan

**When to use this.** Before trusting `make destroy` in an environment for the
first time; after changing `scripts/lib/destroy-guard.jq`,
`scripts/lib/plan-guard.sh`, `SHARED_DENY_LIST` or
`scripts/lib/unlabelable-types.json`; and whenever the recorded plan in
`tests/integration/fixtures/destroy-plan-dev-real.json` no longer matches the
real infrastructure (new resource types, a new neighbour in the shared project).

**How long.** About four minutes, most of it the destroy plan's refresh.

**What it changes.** Nothing. Every step is read-only: a `terraform plan
-destroy` refreshes state and writes a plan FILE, and the proof reads that file.
There is no `terraform apply` and no `terraform destroy` anywhere in this
runbook, and `scripts/verify-destroy-guard.sh` contains neither.

**What it is protecting.** `saga-agents-staging` is shared. It holds another
team's live GKE cluster `agents-staging`, their VPC `agents-staging-vpc`, two
subnets, three buckets, twelve service accounts and Firestore's `(default)`
database. `make destroy` aborting on all of those is the only control between
this repository and deleting their production.

---

## 0. Why a real plan, when there are already tests

Because every existing test of that guard fed it fixtures somebody wrote by
hand, and a fixture agrees with whoever wrote it. When this was first run, on
2026-09-24, the fixtures had already drifted three ways:

* `tests/integration/test_destroy_guard.py` carried its own deny-list of
  **fourteen** entries where `SHARED_DENY_LIST` has **twenty** — and held
  service accounts by short name, where production passes full emails;
* `destroy.sh --self-test` carried a **third** spelling of the deny-list
  transformation, which had lost the `(default)` entry, so the self-test of the
  guard protecting the shared Firestore database could never reach that branch;
* nothing had ever confirmed that the guard reads fields a real
  `terraform show -json` actually has. It reads five different label spellings;
  two of them (`resource_labels` on `google_container_cluster`, `user_labels` on
  `google_monitoring_alert_policy`) exist only for types no fixture contained.

A guard that reads a field the real format does not have passes every
hand-written fixture and aborts nothing.

---

## 1. Produce the plan (read-only)

```bash
scripts/destroy.sh --environment dev --dry-run
```

It writes `build/destroy-dev.tfplan`, `build/destroy-dev.plan.json` and
`build/destroy-dev.verdict.json`, runs the guard, and stops. Expect it to exit
**1** with `ABORTED to protect data` — the dev plan deletes the Firestore
database and both buckets, and `--include-data` is what says you meant it. Every
safety assertion has already run by then.

> **`build/destroy-dev.tfplan` is an applyable destroy plan for a shared
> project.** `build/` is gitignored; never commit it, never copy it somewhere
> that is not, and delete it when you are done:
> `rm -f build/destroy-dev.tfplan build/destroy-dev.plan.json build/destroy-dev.plan.jsonl`.

## 2. Run the proof

```bash
scripts/verify-destroy-guard.sh                      # uses build/destroy-<env>.plan.json
scripts/verify-destroy-guard.sh --plan <show-json>   # or any plan you already have
```

Exit 0 means every assertion held. Exit 2 means the guard did not behave as
required; exit 1 means something could not be checked, which is also a failure.
On the dev plan of 2026-09-24 it ran 58 assertions:

* the real plan (264 deletions, 42 resource types) is **allowed**;
* each of the 20 `SHARED_DENY_LIST` entries, plus Firestore's `(default)`,
  substituted into the **real** resource of its own type and **refused** — by
  the CI entry point *and* by `.denylist_hits`, the verdict key `make destroy`
  itself reads;
* the deny-list still covers the whole shared inventory (floors, not equalities);
* four label mutations on a real label map: `managed-by` removed → refused,
  `managed-by=other-terraform` → refused, `labels: {}` with the truth in
  `effective_labels` → **allowed**, an unreviewed resource type → refused;
* the unlabelable carve-out matches the real provider output in both directions;
* a deletion flipped to an update → refused.

## 3. Re-record the fixture when the plan shape changes

```bash
scripts/verify-destroy-guard.sh --record tests/integration/fixtures/destroy-plan-dev-real.json
```

The recording keeps VALUES only for the fields the guard reads; every other key
survives by name in `_recording.before_keys_by_type`. That is deliberate: a
Cloud Run service's `template` holds its whole environment, a Firestore
document's `fields` holds live data, and a monitoring dashboard is 7 KB of JSON.
The recorder checks that the redacted copy still reaches the **same verdict** as
the plan it came from, so the offline suite cannot end up testing a different
thing from the one that runs at teardown.

`tests/integration/test_destroy_guard_real_plan.py` then replays that recording
through `scripts/destroy.sh` in CI with a stub terraform — no credentials, no
state, nothing to destroy — and fails if the guard grows a read of a field the
recording carries no values for.

---

## What this does NOT prove

* **That a real `terraform apply` of a destroy plan stops.** Nothing here
  applies anything. The abort is proven at the point where `destroy.sh` exits 2,
  which is before it reaches `terraform apply`; the replay suite asserts that
  apply was never invoked, by leaving a marker file if it ever is.
* **That the neighbours survive a real destroy.** That is a different check with
  its own exit codes — `scripts/destroy.sh --verify-shared`, covered by
  `tests/integration/test_shared_resource_verification.py`.
* **That the deny-list is complete.** Every case here iterates
  `SHARED_DENY_LIST`, so a shorter list is a smaller proof. MEASURED: deleting
  one service account from it left the proof reporting 28 green assertions and
  exit 0. The inventory floors in `scripts/lib/destroy-guard-proof-cases.json`
  are the independent statement of what the shared project contains; keep them
  in step with reality, because nothing else can.
* **Any environment but the one you planned.** `prod.tfvars` describes different
  resources. Run this against prod's plan before trusting `make destroy` there.

## If the proof fails

| It says | What it means | What to do |
|---|---|---|
| `expected exit 2 (refused), got 0` on a deny case | The guard would let a deny-listed resource through | Do not run `make destroy` anywhere. Compare `guard_deny_json` in `scripts/lib/common.sh` with the case's `patch` in `scripts/lib/destroy-guard-proof-cases.json` |
| `is not in .denylist_hits` | The CI gate would pass a plan `make destroy` reads differently — `deny_hits` (before only) vs `deny_hits_both` (before and after) | Fix `destroy-guard.jq`; both keys must name the resource |
| `below the floor of N` | An entry was REMOVED from `SHARED_DENY_LIST` | Restore it, or, if the resource genuinely no longer exists, lower the floor in the same commit and say why |
| `matches no rule in ...proof-cases.json` | A new neighbour was added and nothing knows how to place it in a plan | Add a `classify` rule and a `kinds` entry; never skip it |
| `the recording judges identically` fails | The redaction changed the verdict | Re-record; do not hand-edit the fixture |
| `destroy-guard.jq reads [...] and the recorded plan carries no VALUES` | The guard grew a new field read | Re-record after a fresh `--dry-run` |
