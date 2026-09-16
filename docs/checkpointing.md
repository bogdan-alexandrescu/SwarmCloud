# Checkpointing

## The tension, stated plainly

Cloud Run Jobs was chosen as the primary backend because it has no nodes, no
autoscaler and no node upgrades — fewer mechanisms that can end a running agent
for reasons unrelated to the agent.

Long agent runs also need more than the default container filesystem, so the
resource classes request Cloud Run **ephemeral (second-generation) disk**.

**That feature is Preview, and per Google's documentation enabling it disables
live migration.**

So the configuration this platform runs partially undermines one of the reasons
it chose this backend. Live migration is precisely the mechanism that would have
carried a two-hour agent run across a host maintenance event. Without it, an
infrastructure event during a run destroys the sandbox.

This is a **known risk, accepted deliberately**, not an oversight, and not
something to soften in a status update. The compensation is this document:
mandatory checkpointing every 120 seconds, which converts "the attempt is lost"
into "the attempt loses at most two minutes". It does not convert it into "the
attempt is unaffected". If Google promotes ephemeral disk to GA with live
migration intact, revisit `requires_preview_disk` in
`swarm_common/profiles.py` — and until then, treat the checkpoint interval as a
reliability control, not a tuning knob.

See also [cost-control.md](cost-control.md#the-preview-disk-tension) for what the
interval costs.

---

## 1. What is checkpointed

The workspace, which is one directory tree per attempt:

```
<workspace_root>/<attempt_id>/
    work/        <- the runner's cwd. THIS is what gets checkpointed
    artifacts/   <- files to keep; uploaded on exit
    logs/        <- stdout.log, stderr.log
    tmp/         <- TMPDIR for the child
    restore/     <- staging for a downloaded archive; never checkpointed
```

Only `work/` goes into a checkpoint. `restore/` is excluded so a resumed attempt
does not checkpoint a copy of the archive it just restored, doubling its size on
every cycle.

**A resumed worker starts from an empty tree.** `Workspace.create()` removes the
directory first, every time, and refuses to continue if it cannot. Restoring a
checkpoint on top of leftovers from a previous attempt would produce a workspace
that exists in no checkpoint — irreproducible, and silently wrong.

---

## 2. Layout in GCS

Derived entirely from identifiers the control plane already has, so no index is
needed to find a checkpoint:

```
tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/archive.tar.gz
tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/manifest.json
```

The `tenants/<tenant>/` prefix is not cosmetic: each tenant's service account is
granted GCS access **conditioned on that prefix**, so tenant A's credentials
cannot read tenant B's checkpoints even by guessing the path. See
[multi-tenancy.md](multi-tenancy.md).

### The manifest is the commit marker

The archive is uploaded first; the manifest **last, and only after** the archive
completes. A checkpoint interrupted halfway therefore leaves an orphan archive
that no restore will ever select, rather than a manifest pointing at a truncated
one. A restore that selected a truncated archive would be worse than no restore
at all — it would hand the agent a corrupted working tree and let it continue.

The manifest records `seq`, `created_at`, `generation`, `archive_bytes`,
`archive_sha256` and `file_count`. The digest is verified on restore.

---

## 3. Restore

**Restore searches across every attempt of the task, not just this one.** A
resume is by definition a new attempt with a new id; the checkpoint worth
restoring was written by the attempt that died. Selection is by
`(created_at, seq)` — the newest committed checkpoint wins.

Extraction is hostile-input handling, because a checkpoint contains a tenant's
working tree and an agent can put anything in it:

* absolute paths are rejected;
* any member whose resolved destination escapes the workspace is rejected;
* symlinks and hard links whose target escapes the workspace are rejected;
* sockets, FIFOs and devices are skipped rather than restored.

Path traversal is checked on the way **out**, not trusted on the way in.

---

## 4. When checkpoints happen

| Trigger | Where |
|---|---|
| Every `checkpoint_interval_seconds` (default 120; `mock` uses 30) | worker step 8 |
| Before parking on provider quota | `agent_worker/quota.py` |
| Before exiting on cancellation | worker shutdown path |
| Final, after the runner exits | worker step 10 |

The interval is per runner profile (`RunnerProfile.checkpoint_interval_seconds`)
and is *mandatory* — there is no profile-level switch to turn it off, because a
profile with it off would be a profile whose long runs are unrecoverable.

---

## 5. Choosing the interval

```
expected loss per interrupted attempt ≈ interval / 2
checkpoint cost per hour              ≈ (3600 / interval) x (archive size / compression + upload)
```

At 120 s a two-hour run performs 60 checkpoints and risks losing ~60 seconds of
work. At 600 s it performs 12 and risks ~5 minutes. The default is deliberately
toward the frequent end because the workloads are long, expensive and
provider-billed: re-running five minutes of a Claude Code lane costs more in
tokens than the storage saved.

Raise it only if you measure checkpointing consuming a meaningful share of an
attempt's wall clock. Lower it for very long or very expensive runs.

```bash
CHECKPOINT_INTERVAL_SECONDS=60   # platform default for services that read Settings
```

Archives are capped (2 GiB by default in `CheckpointManager`). A workspace that
exceeds it indicates an agent writing build output or node_modules into `work/`;
fix that in the runner rather than raising the cap, because a 2 GiB checkpoint
every two minutes is an egress bill, not a safety net.

---

## 6. What a resume looks like

```
attempt 1  generation 4   runs 47 minutes, checkpoints 23 times, host event
           -> execution gone, lease goes stale

reconciler -> invalidate generation (5) -> terminate -> release -> task READY

scheduler  -> admits, new lease, generation 5, attempt 2
worker     -> validates generation 5 == 5, OK
           -> workspace created empty
           -> restore: newest checkpoint across ALL attempts = attempt 1, seq 23
           -> emits checkpoint_restored, continues from minute ~46
```

The task id, the workflow position and the artifacts are unchanged. Only the
attempt id and generation differ, which is exactly what makes the fencing check
in step 1 able to tell the two apart.

---

## 7. Verifying it

```bash
make test                       # checkpoint archive/restore, traversal rejection
make failure-test               # interrupted attempts resume, capacity returns
make quota-test                 # parking checkpoints before releasing the lease
```

To inspect what exists for a task:

```bash
gsutil ls -r "gs://${ARTIFACT_BUCKET}/tenants/eng/tasks/${TASK_ID}/attempts/"
```

and in the task's event stream, `checkpoint_started` / `checkpoint_completed` /
`checkpoint_restored`:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     "$API/v1/tasks/${TASK_ID}/events"
```

---

## 8. Failure modes worth recognising

| Symptom | Cause | Response |
|---|---|---|
| `checkpoint_started` with no matching `completed` | upload interrupted; the manifest was never written | none needed — that checkpoint is invisible to restore, by design |
| Resume restores an old checkpoint | newer ones never committed | check worker logs for upload errors and GCS permissions on the tenant prefix |
| Resume starts from scratch | no committed checkpoint exists yet | the attempt died inside its first interval |
| Checkpoints growing every cycle | build output or `node_modules` inside `work/` | fix the runner; do not raise `max_bytes` |
| Restore fails with "escapes the workspace" | the archive contains a traversing path or link | expected refusal; inspect the archive before trusting its producer |
