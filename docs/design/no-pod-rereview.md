# A re-review after MERGE, republished by the control plane without a pod

Status: **design, not built.** Written for BGD to read before a follow-up lane
builds it (#2322, lane NO-POD-STEPS). Nothing here is implemented yet; the
worker still runs this step.

## Why

The #2322 audit (tenant eng, 2026-10-24 07:45Z to 2026-10-25 07:45Z) counted
43 browser pods that ran no agent, all of them fix and re-review steps after a
MERGE verdict. NO-POD-STEPS made the fix step publishable without a pod when it
reaches its implementer through the review
(`swarm_api.verdictpublish._reached_through_review`). That leaves the
re-review.

`metadata.merge.on_merge_verdict` appends a re-review step
(`swarm_api.validation.rereview_step_for`). It runs the **review's** runner
profile, by name, and is gated on the same review and verdicts as the fix. On
MERGE no fix agent ran, so the head the merge pins is the one the first review
already judged. The re-review's gate is shut, and its worker does exactly one
thing (`agent_worker.lifecycle.Worker._finish_without_agent` →
`_republish_staged_verdict`): it copies the staged verdict, the first
review's MERGE, byte for byte as its own artifact under the same file name,
so the merge step reads one file name from one step on both paths. For that
copy it takes a lease, a pool slot and a container. When the review is on
`claude-code-browser`, that container includes a display. None of this does
any work.

## What the control plane would do

The shape is the one `verdictpublish` already uses for the fix: the scheduler
holds the step PARKED on `DEPENDENCY_INCOMPLETE`, swarm-api claims it on the
`task_finished` wake, and swarm-api either ends it SUCCEEDED (PARKED →
SUCCEEDED, contract request 52, applied 2026-10-09) or declines it to the
worker.

### What it reads

All of these are read on the wake for the fix step's `task_finished`, since
the re-review depends on the fix (its `builds_on`) and on the review:

1. **The re-review's own document**, read by id. Its spec signature is
   verified (`verdictpublish.spec_refusal`) before any of these is trusted:
   its dispatch block's `verdict_gate`, `builds_on`, its `depends_on` and its
   `metadata.input_from`.
2. **Its gate's review** (`verdict_gate.task_id`, a direct dependency). It must
   be SUCCEEDED, in this tenant and in this workflow. Its verdict file is
   read under the name the re-review stages from it, through the same
   size-capped, tenant-checked read the fix publisher uses. It must read as a
   verdict outside `verdict_in`, as `parse_verdict` reads it.
3. **The fix step it builds on.** It must be SUCCEEDED, in this tenant and
   workflow, with `verdict_gate.agent_ran == false`, and its
   `result_summary.git.pushed_head` must equal the implementer's recorded
   `pushed_head`. That last check holds the step to "the head the merge pins
   is the head the review judged". It is the one thing the worker gets for
   free (its gate is shut only because no fix agent ran) and the control plane
   has to check explicitly.
4. **The bytes of the staged verdict object.** These come from the review's
   successful attempt's `result_summary.artifacts` entry. They are not read
   through the redacted `read_artifact` view, because the copy has to be the
   same bytes. If redaction would mask anything in the file, the step is
   declined to the worker, as the fix publisher declines a masked title.

### What it writes

1. **One object**:
   `tenants/<t>/tasks/<re-review>/attempts/<synthetic attempt>/artifacts/<verdict file>`.
   It holds the review's bytes, unchanged. The merge step finds it only this
   way: input staging (`agent_worker.inputs.artifact_reference` and
   `artifact_key`) accepts a URI only under
   `tenants/<t>/tasks/<that task>/attempts/`.
2. **The step's ending**, in one transaction guarded on the claim: SUCCEEDED,
   `result_summary` with `artifacts: [{name, bytes, uri}]`,
   `published_by: "control_plane"`, `verdict_gate` (the record the worker
   writes, `agent_ran: false`), and `skipped_agent`. This design does not
   establish whether the worker's ending on this path records a `git` block.
   The follow-up lane reads that from a real re-review's summary and
   reproduces exactly what the worker records.
3. **A SUCCEEDED event** naming the publisher, and the step's own
   `task_finished` wake, which releases the merge step.
4. **On a decline:** only the `metadata.control_publish` marker and the
   parent's wake. That is the fix publisher's behaviour exactly.

### The decision that is BGD's

swarm-api **cannot write the object today**. Its bucket role is
`roles/storage.objectViewer` (`terraform/modules/iam/bindings.tf`,
`api_reader`: "the worker produces artifacts, the API only hands them over").
That leaves three options:

| | change | cost | risk |
|---|---|---|---|
| A | a conditional `storage.objects.create` grant for swarm-api, limited to `tenants/*/tasks/*/attempts/*/artifacts/*`, in the shape of `api_history_purge_deleter` | IAM review (Track C); the API's "never writes artifacts" stance ends | a swarm-api bug could write an artifact into any task's prefix in any tenant. The condition bounds the path, not the tenant |
| B | the re-review's artifact entry points at the **review's** object, and `artifact_key` accepts another task's prefix when that task is the reader's own signed `verdict_gate.task_id` | worker change in `inputs.py` (Track B) | widens the one rule that keeps a forged `result_summary` inside its own task's prefix. `result_summary` is unsigned, and a tenant's agents can write it |
| C | the merge step reads the first review's verdict itself when the re-review ended `published_by: control_plane` | merge-step change | the merge path would branch on an unsigned summary field. It would have to recompute "the first review's verdict is outside the gate" itself, or a forged marker could swap a re-review's NOT_YET for the first review's MERGE |

The recommendation is **A**. The bytes are produced exactly as the worker
produces them, the merge step and input staging do not change, and the grant
can be written as narrowly as the purge deleter's. B and C both move a trust
decision into the merge path.

## Invariants it touches

- **1 (only leased states create demand)** and **2/3 (capacity, concurrency
  from LEASED)**: the step goes PARKED → SUCCEEDED and never holds a lease or
  a pool count. That is the whole point. Nothing about admission changes.
- **5 (fencing)**: no attempt exists, so there is no generation. The claim
  marker does that job, as for the fix. Here is the race. The scheduler's hold
  ends (claim timeout) and a worker starts while swarm-api is still copying.
  Both write a verdict, each under its own attempt prefix. The ending
  transaction is guarded on the claim, and the worker's own ending is guarded
  on its lease, so only one of the two summaries is recorded. Its URI is the
  only one the merge step reads.
- **9 (tenant isolation)**: the copy stays within one tenant's prefix. Every
  document is checked for `tenant_id` against the wake's tenant before
  anything is read, as `on_task_finished` already does.
- **10 (callers pick a profile by name)**: no profile is chosen at all. This is
  also what removes the browser pod: the review's `claude-code-browser` is
  never admitted for a step that runs no agent.
- **8 (checkpointing)**: not applicable. Nothing runs long enough to
  checkpoint.

## What could go wrong in the merge path

1. **The merge pins a head the review never judged.** This would happen if the
   re-review were republished while the fix actually ran an agent (NOT_YET),
   or if the fix's branch had moved. Check 3 above closes it (`agent_ran ==
   false`, and the fix's pushed head equals the implementer's), together with
   the gate check on the review's verdict. Every other case declines to the
   worker, which runs the re-review's agent.
2. **A stale or foreign verdict.** This means a verdict from another workflow,
   another tenant, or an earlier attempt of the review. The review is read by
   the id the signed gate names. It must be SUCCEEDED in this workflow, and the
   bytes come from its recorded successful attempt only.
3. **The scheduler promotes the step before swarm-api decides.** Today
   `held_for_control_publish` holds only `integrate`'s gated integrator, and
   the re-review is not one. So the scheduler would need a second predicate:
   a gated step whose `builds_on` is that integrator and whose gate names the
   same review. It must be restated in `verdictpublish.held_by_scheduler`, and
   `test_the_scheduler_restates_the_marker_the_claim_and_who_is_held` must hold
   the two equal. If a step is held by one side and never decided by the
   other, it waits out the whole hold.
4. **Merge-step ordering.** The merge step depends on the re-review. The
   re-review's wake has to come only after its ending transaction commits, as
   the fix publisher's does. Otherwise the merge stages from a task that is
   not yet SUCCEEDED and refuses `verdict_unreadable`.
5. **Re-serialisation.** If the copy were written from the parsed verdict
   instead of the bytes, the merge step's `read_verdict` would judge a
   different file from the one the review wrote. The copy is bytes only.

## Tests the follow-up lane needs (red first)

- MERGE, fix control-published: the re-review ends SUCCEEDED with no lease, no
  attempt and no dispatch, and its verdict artifact is the review's bytes.
  The merge step's input staging then stages it, through the real
  `artifact_key`.
- NOT_YET (the fix ran): the re-review is declined and its worker runs.
- The fix's pushed head differs from the implementer's: declined.
- A re-review whose spec was rewritten after signing: declined `credential`.
- The scheduler and swarm-api agree on which re-reviews are held.
