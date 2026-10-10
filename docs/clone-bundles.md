# Clone bundles: a known commit without GitHub on the start path

**Status: built 2026-10-09 for
[#940](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/940); not yet
measured in a deployed environment.** The acceptance number (§8) can only be
taken after release.

This document records why a step clones from a git bundle in its tenant's own
prefix, where the bundle lives, what keeps it inside the tenant, when it
expires, and how to tell whether it worked. The code is
`apps/agent-worker/agent_worker/clonebundle.py` (the key layout and the store
operations), `apps/agent-worker/agent_worker/gitops.py::write_clone_bundle`,
`apps/agent-worker/agent_worker/gitops.py::clone_from_bundle` and
`apps/agent-worker/agent_worker/gitops.py::fetch_tip_onto_bundle` (the git
half), and `apps/agent-worker/agent_worker/lifecycle.py::Worker._maybe_clone`
(which path a step takes). The expiry rule is in
`terraform/modules/storage/main.tf`.

---

## 1. Why

**The measurement.** On 2026-10-06 the chunk-2 observer read 60 `clone_timed`
events in dev ([incident](incidents/2026-10-06-clone-connect-stall.md), §1).
96.2 % of the clone time was the TCP connect to github.com: median **36.6 s**,
p90 **69.5 s**. The transfer itself took **1.7 s**. A clone is slow because a
new Cloud Run instance waits for its internet egress path to open, not because
the repository is big.

**GCS does not use that path.** The worker subnet has
`private_ip_google_access = true` (`terraform/modules/network/main.tf`), so
Google APIs, GCS included, are reached over Private Google Access. That route
does not go through the internet egress path that stalls, and it answers at
once on the same instances. A commit the step can read from GCS does not need
GitHub at all.

**Where this came from.** #721 offered three options: (a) an egress probe,
which is on main; (b) finding out why the path stays closed, an open question
to Track C; (c) cloning from a per-SHA bundle in GCS. The owner split (c) into
#940 on 2026-10-09. It does not fix #939 (the closed egress itself). It avoids
that path for a step whose commit is already known, and only for that step.

---

## 2. Layout

Every key is built from the worker's own `cfg.tenant_id`, which dispatch sets
and the task's input cannot. The other key segment is the registration's
`repo_id` (`indexrun.target`), so no URL text ever appears in a key:

    tenants/<tenant>/bundles/<repo_id>/<sha>.swarm-clone.bundle
    tenants/<tenant>/bundles/<repo_id>/heads/<sha256(ref)[:32]>.swarm-clone.head

* **A bundle** (`.swarm-clone.bundle`) holds one commit: its objects and the
  single ref `refs/swarm/bundle`.
* **A head pointer** (`.swarm-clone.head`) is a small object holding the last
  bundled sha of one branch. The name is a hash of the ref, so branch names
  with any characters fit in a key and two branches never share a pointer.
  It is the only object here that is ever overwritten, because a branch moves.

Bundles are deliberately **not** under `repos/`. That prefix is off the
expiry clock (`objectstore.is_repos_key`), and a bundle is a cache that is
meant to expire.

### Why a bundle is only ever one depth-1 commit

A step's clone is depth 1, so one commit is all a reader needs and all a
writer has. `write_clone_bundle` refuses anything else: `.git/shallow` must
name exactly that commit, and the history count must be 1. That refusal is
what keeps an index run's 90-day clone, or a clone later deepened by
`deepen_history`, from being uploaded as a cache entry that is far larger than
any reader needs. A `bundle+delta` clone has the seed commit as well as the
tip in `.git/shallow`, so the worker bundles a depth-1 local copy of the tip
instead. Making that copy does not contact the forge.

### Why the reader writes `.git/shallow` first

A bundle made from a shallow clone names a commit whose parent it does not
hold. Measured 2026-10-09 on git 2.39: `git clone x.bundle` of such a bundle
fails with `Could not read <parent>`. So `clone_from_bundle` runs
`git init`, adds the forge URL as `origin`, writes the commit into
`.git/shallow` to declare it a shallow boundary, and only then fetches the
bundle's ref and checks the commit out. The result has the same `origin`, the
same shallow boundary and the same detached HEAD as a `clone_at_commit`
result, so `deepen_history`, `fetch_branch_tip`, `push_branch` and the summary
cannot tell the two apart. After checkout HEAD is verified against the
requested sha. A mismatch empties the destination and becomes a fallback.

### Written once

"Written once per sha, by whoever clones first" is enforced by the upload's
`ifGenerationMatch=0` precondition (`upload_file_if_absent`). A second writer
gets `False`, and the first object stays. Two steps that clone the same sha at
the same moment both bundle it and one upload wins. Both copies hold the same
commit, so it does not matter which.

The bundle is written after the clone and before the agent starts, so nothing
the agent does can end up in it. The upload runs in a thread that `_cleanup`
joins with a bound, so the agent never waits for it. A failed write is logged
and never fails the step. The next step simply clones from the forge.

**The repository-index job does not write bundles.** Its clone is deepened
to its history window, so it is never one commit. Requirement 3 of #940 lists
the index job as an optional writer. The first step that clones a sha is the
writer instead.

---

## 3. Three read paths

`Worker._maybe_clone` chooses one:

| step | what it does | contacts GitHub? | `clone_timed.source` |
|---|---|---|---|
| **pinned**: a workflow step's base pin, a carried parent head | downloads `<sha>.swarm-clone.bundle`, clones from it | **no** (no egress wait, no token) | `bundle` |
| **branch tip**: an unpinned branch clone | reads the branch's head pointer, seeds from that sha's bundle, then `git fetch --depth 1` of the tip | yes, for the delta | `bundle+delta` |
| **miss**: no key, no bundle, over the size cap, corrupt, wrong sha, kill switch | empties the destination, runs today's clone unchanged | yes | `forge` |

**The branch-tip path saves the transfer, not the connect.** The delta fetch
still opens a connection to github.com, so it still waits for the egress path
that §1 measured. It moves fewer bytes and nothing more. Only a pinned step
skips GitHub entirely. **The acceptance number in §8 is therefore for pinned
steps.** Branch-tip steps are worth measuring on their own, but they will not
reach it.

The fallback is today's code path, not a copy of it. A carried pin that
misses still never takes the branch tip, and a base pin keeps its tip fallback.
`tests/unit/worker/test_clone_bundle_lifecycle.py` holds this. In every case
the checked-out commit is the one today's clone would check out.

Single-PR reader/amender steps and index runs never take the branch-tip path.
Index runs neither read nor write bundles.

---

## 4. Isolation (invariant 9)

* **Own prefix.** The tenant segment comes from the worker's own
  configuration and never from the task. A step can only key, and so only
  read, what its own tenant wrote.
* **Own service account.** Reads and writes go through the worker's object
  store, which runs as the tenant's own GSA. `tenants/<t>/` is already that
  GSA's read/write grant and nobody else's. **No IAM change was needed**, and
  none was made.
* **No sharing across tenants.** Two tenants that register the same GitHub
  repository get two `repo_id`s and two bundles. Deduplicating across tenants
  would mean one tenant reading another tenant's prefix, which invariant 9
  forbids. The cost is one extra forge clone per tenant per sha.
* **The bundle is downloaded to `ws.private`**, which the agent is never
  given.

## 5. No token in a bundle or its metadata

A bundle holds one commit's objects and one ref. It never contains the clone's
`.git/config`, its remote URL or its credential file:
`tests/unit/worker/test_clone_bundle_git.py` scans the bundle bytes for both.
The object carries a content type (`application/x-git-bundle`) and no custom
metadata, so a token has no metadata field to end up in. Reading a pinned
bundle uses no token at all. The branch-tip delta fetch writes and removes the
credential file the same way `shallow_clone` does.

---

## 6. Expiry

A lifecycle rule on the artifacts bucket deletes any object whose name ends in
`.swarm-clone.bundle` or `.swarm-clone.head` once its `age` reaches
`clone_bundle_retention_days` (default **7**, `terraform/modules/storage/variables.tf`).

* **Why a separate rule.** Every upload gets a customTime stamp, so the
  bucket's customTime Delete would also catch bundles, but only after
  `artifact_retention_days`: 180 days in prod. A bundle is useful for hours:
  a workflow's downstream steps pin to the upstream's base within one run. A
  week also covers re-runs and resumes. Expiring too soon costs one forge
  clone and nothing else.
* **Why bucket-wide by suffix.** A `matches_prefix` list cannot reach the
  personal `u-<slug>` tenants the API creates at runtime, which is the same
  reason the customTime rule is bucket-wide. The suffixes are distinctive
  so the rule can never catch an agent artifact named `*.bundle`, a
  checkpoint or anything under `repos/`. Only the bundle writer produces
  them. `tests/terraform/clone_bundle_lifecycle.tftest.hcl` checks the rule,
  and `tests/unit/worker/test_clone_bundle_suffix_parity.py` checks that
  Terraform's two suffixes equal `clonebundle.BUNDLE_SUFFIX` and
  `clonebundle.HEAD_SUFFIX`. **Change both or neither**: if they drift, no
  test turns red in production, and bundles quietly live 180 days.
* `with_state = "ANY"` is stated explicitly: a head pointer's noncurrent
  versions are as stale as the live one.

---

## 7. Kill switch and size cap

`SWARM_CLONE_BUNDLES=0` in the worker's environment turns bundles off
(`WorkerConfig.clone_bundles_enabled`, default on). Every step then clones as
before, with `clone_timed.bundle.miss_reason` set to `disabled`. Nothing needs
to change in dispatch or Terraform. Objects already written simply expire.

A bundle over `clone_bundle_max_bytes` (512 MiB) is neither written nor read.
The workspace is memory-backed tmpfs, so a bundle counts against the task's
own memory.

---

## 8. Measuring it (#940's acceptance, after release)

Each attempt's `clone_timed` startup mark (a RUNNING event with
`cause: clone_timed`, served by `GET /v1/tasks/{id}/events`) carries:

* `clone.source`: `bundle`, `bundle+delta` or `forge`;
* `clone.bundle`: `{hit, download_seconds, bytes, written, miss_reason}`,
  plus `write_seconds` when this step bundled its clone;
* `clone.total_seconds`: what the step waited for its repository. **For a
  bundle hit this includes the download**, so it is the issue's number and
  needs no adjustment. A bundle hit made no forge try, so `tries` is 0.

The acceptance, as #940 states it:

1. Take `clone_timed` events with `clone.source == "bundle"` from steps that
   ran on the released worker. Pinned workflow steps are the source; a
   workflow with several downstream steps on one base produces them quickly.
2. With **at least 30** such steps, the **p50 of `clone.total_seconds` is under
   5 s**.
3. Check that the same steps checked out the same commit as before (the pinned
   sha), and that the fallback works: events with `clone.source == "forge"`
   and `clone.bundle.hit == false` (`miss_reason` `miss`, `no_key` or
   `bundle_error`) finished their clone with `ok: true`.
4. For contrast, record the p50 of `forge` steps over the same window. The
   2026-10-06 baseline is the incident's §1.

One command takes it, read-only, through the API with the operator's
credential:

    scripts/egress-ready-report.sh --clone-bundles --since 7d

It prints n, p50 and p90 of `clone.total_seconds` per `clone.source`, how
many forge clones after a bundle miss landed, and a verdict. The verdict is
PASS only when steps 1, 2 and the fallback half of 3 all hold; the `forge`
row is step 4's contrast. Too few bundled steps is a FAIL (`insufficient n`),
not a pass. A bundled clone that did not land counts as slower than any that
did. A fallback that never ran (no forge clone after a `miss`, `no_key` or
`bundle_error`) is a FAIL, because it was not shown to work. The other half
of step 3, the same commit as the pin, is not in the mark;
`tests/unit/worker/test_clone_bundle_git.py` holds it. The arithmetic is
unit-tested offline in `tests/unit/scripts/test_egress_ready_report.py`.

Report the window, the count and both p50s on #940. The issue stays open
("part of #940") until that measurement is posted.
