# Checkpointing

## The tension, stated plainly

Cloud Run Jobs was chosen as the primary backend because it has no nodes, no
autoscaler and no node upgrades — fewer mechanisms that can end a running agent
for reasons unrelated to the agent.

**What this section used to say, and why it is wrong.** It said the resource
classes request Cloud Run **ephemeral (second-generation) disk**; that the
feature is Preview and enabling it disables live migration; and that the
configuration therefore partially undermined the reason for choosing this
backend. None of that describes what is deployed. The Terraform google provider
cannot express that feature — `empty_dir.medium` accepts only `"MEMORY"` — so a
workspace is a tmpfs carved out of the container's own memory, and the deployment
runs on the fully-GA path, **which does support live migration**. The retraction
is repeated at the foot of this file and in CONTRACT.md.

**The tension is real anyway, and it is not the one that was written down.** Live
migration covers infrastructure moves. It says nothing about the interruptions
that actually end attempts on this platform, all of which are above the
infrastructure:

| Interruption | What ends the attempt |
|---|---|
| Provider quota exhausted | the worker checkpoints, parks, releases its lease and exits (invariant 4) |
| Cancellation | the worker is asked to stop mid-run |
| Reconciler reclaim | a stale fencing generation must exit without touching the lease (invariant 5) |
| Crash, OOM, timeout | the ordinary ways a process dies |

Every one of those loses whatever is not committed. So the compensation is
unchanged and is this document: mandatory checkpointing every 120 seconds, which
converts "the attempt is lost" into "the attempt loses at most two minutes". It
does not convert it into "the attempt is unaffected".

**Do not relax the interval on the grounds that live migration is now
available.** That is the one wrong conclusion this correction invites, and it
would trade a real protection against the four rows above for a reassurance about
a fifth that was never the problem. Treat the checkpoint interval as a
reliability control, not a tuning knob.

See also [cost-control.md](cost-control.md#4-the-preview-disk-tension) for what the
interval costs.

---

## 1. What is checkpointed

The workspace, which is one directory tree per attempt:

```
<workspace_root>/<attempt_id>/
    work/        <- the runner's cwd. THIS is what gets checkpointed
      artifacts  <- a link to artifacts/ below; left out of every checkpoint
    artifacts/   <- files to keep; uploaded on exit
    logs/        <- stdout.log, stderr.log
    tmp/         <- TMPDIR for the child
    restore/     <- staging for a downloaded archive; never checkpointed
```

Only `work/` goes into a checkpoint. `restore/` is excluded so a resumed attempt
does not checkpoint a copy of the archive it just restored, doubling its size on
every cycle.

`work/artifacts` is left out too, when it is the link the worker makes to
`artifacts/` (#149, see [workflows.md](workflows.md#artifacts-pass-by-reference)).
Archived, it would fail every resume: it points outside `work/`, and a restore
refuses an archive holding such a link. It also names this attempt's directory,
and a resumed attempt makes its own. What is behind the link is not archived
either, because `Path.rglob` does not descend into a symlinked directory; those
files are the artifacts, uploaded on their own. A real `work/artifacts`
directory is the agent's work, and is checkpointed like anything else.

The artifacts directory itself is not checkpointed at all. A resumed attempt
starts with an empty one, so a file written there before a park is uploaded
under the parked attempt's prefix and is not in the upload of the attempt that
finally succeeds. A retried attempt starts the same way. When the file is one a
later workflow step stages, an attempt that ends without writing it again fails
for it, retryably ([workflows.md](workflows.md#artifacts-pass-by-reference)).

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
./scripts/api.sh GET "/tasks/${TASK_ID}/events"
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

---

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
