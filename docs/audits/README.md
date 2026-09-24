# Audits

Findings from agents run against this repository, kept because the reasoning
behind a fix is worth more than the fix. Each report is the agent's own words,
unedited. They are EVIDENCE, not instructions: several findings were refuted on
verification, and a few are wrong.

## 2026-09-18

Twelve agents, one per area, dispatched to SwarmCloud and run on SwarmCloud —
the platform auditing itself. Reports 01–12. Report 13 is a follow-on sweep of a
single class, five lenses with adversarial verification.

Roughly 100 findings. What follows is what has been ACTED ON, so that the next
reader does not re-litigate what is already settled.

### Fixed

| Finding | Where | Commit |
|---|---|---|
| Four terraform/app env-var name mismatches | 07 | `b2a63ab` |
| Three more found by the check itself (`ARTIFACT_REGISTRY`, `IMAGE_BASE`, reconciler GKE) | — | `b2a63ab` |
| Build manifest written empty while the build reported success | 13 | `aebb849` |

`scripts/lib/check-env-parity.sh` was added with the first of those. It is the
part meant to outlive the fix: two rules, no suppression list, mutation-tested
against all three bug shapes, and it found the `ARTIFACT_REGISTRY` pair that no
human had noticed.

### Recorded as a request, deliberately not fixed

`apps/common/swarm_common/` is frozen (CLAUDE.md rule 1). The `u-` prefix
collision in `identity.py` found by report 12 is written up in
`docs/contract-change-requests.md` instead of being changed. The audit agent
that found it declined to edit the module for the same reason.

### Closed since, on a second pass over the backlog (2026-09-22)

| Finding | Where | What it was |
|---|---|---|
| A repair reset a task its NEWER lease was running | 10 §1 | `repair_task_state` matched on `task_id` alone and cleared `current_lease_id` unconditionally, so a finding about an old attempt unhooked a live one and the next drain admitted a second worker on the same repository. It now takes `expected_lease_id` and refuses on a mismatch, the way `invalidate_generation` already refused on generation. `test_repair_respects_the_current_lease.py` |
| Everything after a committed admission could leak the slot | 05 §1–2 | `create_attempt`/`append_event` sat outside every guard and the guard caught only `DispatchError`, so a `DefaultCredentialsError` from building the Cloud Run client left the pools incremented and the task `LEASED` — a state no drain ever revisits. The capacity now comes back before the exception propagates. `test_admission_is_not_a_one_way_door.py` |
| The purge backup guard blamed an absent bucket for a dead session | 04 §1 | And offered `--no-backup` — skip the backup of an irreversible purge — as the remedy. Present, provably absent and could-not-look are three answers now; only the middle one may offer the flag. `tests/integration/test_purge_backup_guard.py` |
| Two CI gates and two `make test` gates passed without looking | 01 §1 | `if [ -d tests/integration ] && ls …` printed "no integration tests present" and exited 0; the Makefile had the same shape for both suites, and `tf-test` gave one message for a missing binary and a missing suite. pytest already fails on all three cases; the guards are gone |
| The Spot check reported a clean tree it never read | — | `.github/workflows/security.yml`'s `[ -d kubernetes/worker-templates ] && grep` passed when the directory moved. Verified before and after by running the extracted step against a renamed tree |
| Two private copies of the slug character class | 08 §1 | `scheduler/dispatch.py` and `reconciler/detect.py` each re-declared `[^a-z0-9-]+`; the reconciler's `sanitised()` reverse-maps what the dispatcher's `sanitize_name` produced, so drift breaks the match. Both import the frozen symbol now, as `kubernetes/render.py` already did |

Also verified as already fixed in the intervening commits, so nobody re-opens
them: 04 §2–5, 06 §1–2 and §4, 08 §2–4, 12 §2, 13's `plan-guard.sh:84`,
`fs_request`, `status.sh` and the fourteen-file fan-out's landed diffs.

### Open, and worth reading before the rest

* **Report 12 §1** — two verified identities can be handed the same `tenant_id`.
  The read/cancel half is fixed (`tenant_scope` applies the principal check on
  every route now); the `u-` collision itself is in the frozen module and is
  recorded in `docs/contract-change-requests.md`.
* **Report 05 §3–4** — a SIGKILL between the admission transaction and the
  follow-up writes, and an admission commit whose ack is lost. Neither is
  reachable from the caller: the first has no code to run and the second never
  learns the lease id. Both are the reconciler's to reclaim, which is the
  argument for keeping its deadline sweep rather than for a scheduler change.
* **Report 06 §3** — nothing serialises the quota sweep, so two ticks can race
  a refresh token into `invalid_grant`. `main.py:800-809` already names the fix:
  a lease on the sweep itself.
* **Report 02 §1** — periodic checkpoints tar `ws.work` to GCS with no
  redaction. `_redact_before_upload` never runs against the work tree, so a
  cached CLI auth file or a shell history written there rides into the archive.
  The trade (scrubbing a whole tree every interval) is a decision, not a typo.
* **Report 02 §4** — `scrub_file`'s return value is discarded, so an oversized
  artifact is uploaded with no signal that redaction was skipped. Leaving it
  unscrubbed is deliberate; saying nothing about it is not.
* **Report 09**, **`tag-vs-digest.md`** — Track C. Not ours to edit.

### Two reports found little, and said so

03 (tests that pass while broken) and 11 (docs vs reality) came back nearly
empty. 03 explicitly refused to pad its list. That is worth as much as the long
ones: it means the negative-assertion bug found earlier that day was not a
pattern, and nobody needs to look again.
