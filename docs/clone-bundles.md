# Clone bundles: retired

**Status: RETIRED 2026-10-11 (owner decision), after being built 2026-10-09
for [#940](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/940).**
Every clone is a plain clone from the forge again. `clone_timed`'s
`clone.source` is always `forge`. The worker no longer reads or writes a
bundle, and it writes no head pointer.

This document says why the bundles were built, why they were retired with
the numbers behind the decision, what was removed, and what is left behind.
It is kept so that nobody rebuilds the path without first re-reading the
measurement that ended it.

---

## 1. Why it was retired

**The measurement, 2026-10-11 10:20Z.** It covers seven days of `clone_timed`
startup marks across 2,436 tasks, 1,436 of them with a startup mark, taken
with the `bundle-report` reader this change removes (posted on #940 that day):

| source | n | failed | p50 s | p90 s | max s |
|---|---|---|---|---|---|
| `bundle` (pinned sha, no forge contact) | 1 | 0 | 1.99 | 1.99 | 1.99 |
| `bundle+delta` (branch's last bundle, then the delta) | 124 | 0 | 4.69 | 9.63 | 17.48 |
| `forge` (plain clone) | 293 | 1 | 3.19 | 10.56 | 59.97 |

The miss-then-forge fallback ran 0 times.

* **Plain forge clones were faster than bundle-served ones**: a p50 of 3.19 s
  against 4.69 s for `bundle+delta`.
* **A pure bundle hit almost never happened**, n=1 in seven days. A pure hit
  needs the exact commit to be bundled already, and that turned out to be rare.
  #940's acceptance bar of p50 under 5 s over at least 30 `bundle` steps could
  not be met.
* **What the bundles avoided is gone.** They existed to skip the 35-70 s
  egress stall that a fresh Cloud Run instance hit on its first connect to
  github.com: a median 36.6 s connect against 1.7 s of transfer
  ([incident](incidents/2026-10-06-clone-connect-stall.md), §1). Since #939
  moved the runner profiles to GKE Autopilot, a fresh pod's egress no longer
  stalls. A bundle then costs an upload after every first clone and storage
  for every sha, and it buys a slower clone in return.

**Revisit only with a new measurement.** If a backend's egress starts
stalling again, `clone_timed`'s `connect_seconds` and `egress_ready`'s
`egress_ready_seconds` will show it. That is the time to look at this again,
and git history has the full implementation (PR #983).

---

## 2. What it was

A step that cloned a commit wrote a one-commit git bundle of it under its own
tenant's prefix. A later step that needed the same commit downloaded the
bundle over Private Google Access instead of contacting GitHub:

    tenants/<tenant>/bundles/<repo_id>/<sha>.swarm-clone.bundle
    tenants/<tenant>/bundles/<repo_id>/heads/<sha256(ref)[:32]>.swarm-clone.head

There were three read paths:

* **pinned** (a workflow base pin or a carried parent head): clone from the
  sha's bundle with no forge contact. `source` was `bundle`.
* **branch tip**: seed from the branch's last bundle (found through the head
  pointer), then fetch only the delta. `source` was `bundle+delta`.
* **miss**: the plain clone, with `source` `forge`.

Isolation held through the tenant's own prefix and service account
(invariant 9). `tenants/<t>/` was already that service account's grant, so no
IAM was ever added for bundles. A bucket-wide lifecycle rule keyed on the two
suffixes expired the objects after 7 days.

---

## 3. What was removed (2026-10-11)

* **Worker.** The bundle read before a clone and the bundle write after it,
  the head-pointer objects, and the upload thread and its join in `_cleanup`
  were removed. So were `agent_worker/clonebundle.py`, the git half in
  `gitops.py` (`write_clone_bundle`, `clone_from_bundle`,
  `fetch_tip_onto_bundle`), and the `SWARM_CLONE_BUNDLES` /
  `clone_bundle_max_bytes` settings.
* **`clone_timed`.** The mark is kept. `clone.source` is still written and is
  always `forge`, so a reader of events from both sides of 2026-10-11 needs
  no second rule. The `clone.bundle` sub-record is no longer written.
* **Terraform.** The suffix-keyed `age` Delete rule and
  `clone_bundle_retention_days` were removed from
  `terraform/modules/storage`. That rule was the only `age` Delete on live
  objects, and the storage tests now refuse any `age` Delete.
* **Reports.** `scripts/egress_ready_report.py bundle-report` and
  `scripts/egress-ready-report.sh --clone-bundles` were removed. Nothing else
  used them. The `report`, `window` and `next-token` subcommands, and #939's
  egress report, are unchanged.
* **Tests.** The tests of the removed code were deleted. The clone tests
  stay, and `tests/unit/worker/test_clone_phase_trace.py` now also pins
  `source == "forge"` and the absence of `bundle`.

---

## 4. What is left behind

**Objects already written.** Bundles and head pointers written before the
retired worker was deployed stay in the bucket under `tenants/*/bundles/`.
Every one was uploaded with a customTime stamp, so the bucket's customTime
Delete removes them after `artifact_retention_days`. Nothing reads them in
the meantime. To remove them sooner, an operator can delete the
`tenants/*/bundles/` prefixes. That is a manual, destructive step and is not
part of any script.

**IAM.** None to remove: bundles used the tenant's existing `tenants/<t>/`
grant, and no binding was ever added for them.
