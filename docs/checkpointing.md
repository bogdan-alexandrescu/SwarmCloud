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

### What is left out, and what rebuilds it

The 2026-10-05 history analysis (#637) measured 247 GB of checkpoints uploaded
over the platform's history, 88.5 GB of it on 2026-10-02 alone from 1,732
checkpoints: a median of 66 MB every 120 s. Only 1.25% of those bytes were ever
restored. Most of them were directories the agent's next command recreates, so
they are not archived (owner decision 2026-10-05):

| Left out | Where | What rebuilds it after a restore |
|---|---|---|
| `.cache`, `.npm`, `.local/share/uv`, `.local/share/pnpm`, `.yarn/cache` | HOME (`work/`) only | the next `uv run` / `uv sync`, `npm ci`, `pnpm install`, `yarn install` fetches them again |
| `node_modules/` | anywhere in `work/` | `npm ci` (or `pnpm install`, `yarn install`) |
| `.venv/` | anywhere, unless git tracks a file in it | `uv sync` (`uv run` syncs first) |
| `__pycache__/` | anywhere, unless tracked | the interpreter, on the next import |
| `dist/` | anywhere, unless tracked | the project's build: `npm run build`, `uv build` |
| `.test-build/` | anywhere, unless tracked | `npm run typecheck` |
| `coverage/` | anywhere, unless tracked | the next test run with coverage on |

The lists are `TOOL_CACHES` and `BUILD_DIRS` in
`apps/agent-worker/agent_worker/checkpoint.py`.

**A restore still produces a working tree.** Every row above is output of a
command, never the agent's work, and a resumed agent runs the command again the
way a fresh clone needs it run. The one way that could fail is a build
directory a repository *commits* — some libraries commit `dist/`. Left out, it
would come back as a checkout whose committed files read as deleted, and a
publish could carry the deletion. So a `BUILD_DIRS` directory is left out only
when the nearest git checkout around it tracks nothing under it. The worker
reads that from the checkout's index file itself (formats 2-4, SHA-1 or
SHA-256), never by running git, which would run the agent's `core.fsmonitor`;
an index it cannot read keeps the directory. Only directories are matched: a
*file* named `dist` is kept.

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

It also records `clone_base`: the commit the attempt's clone landed on, as the
worker knew it (`"empty"` for a repository that had no commits). A resumed
attempt publishes from that value and not from `work/.swarm/clone-base`, the
copy in the archived tree, because the tree is the agent's to write: an agent
that moved the marker onto a later commit of its own kept its earlier commits
out of the fold that makes every pushed commit the worker's. A manifest written
before the field existed holds `null`; that attempt's work is harvested against
the marker and is not pushed.

### Incremental archives after the first

**An attempt's first checkpoint is a full archive; each one after it holds only
what changed since the one before** (#637). The worker remembers, per entry,
what it archived last time — for a file its mode, size, mtime, ctime and inode;
for a link its target; for a directory its mode — and archives the entries that
differ. Deletions, and entries that changed kind (a file that became a
directory), are listed so the restore removes them first. A file written within
a second of the walk that read it is *racy* — a same-size rewrite in the same
clock tick changes none of those fields — and is archived again next time, which
is git's rule for the same problem.

**The chain lives inside the archives, bound by digest.** An incremental
archive's gzip header comment holds `swarm-checkpoint {"base": {checkpoint_id,
archive_sha256}, "deleted": [...]}`. A restore verifies the head against the
digest the attempt document recorded (section 3), reads its base from the
verified bytes, fetches that base from beside it, verifies it against the digest
the head named, and so on down to a full archive; then it replays them oldest
first. Rewriting a base in the bucket, or a manifest's `base_checkpoint_id` /
`base_archive_sha256` (which are there for a reader only), gets the restore
refused, not a planted tree restored. The comment is not a tar member, so it
cannot collide with an agent's file, and every gzip reader skips it — the
console's checkpoint browser lists an incremental archive's own members, which
are the changes; its manifest says `"kind": "incremental"`.

**A chain never crosses an attempt.** A resume is a new attempt, and its first
checkpoint is full. That is what keeps retention simple: the reconciler keeps
every checkpoint of the attempt `task.latest_checkpoint` names
(`reconciler/checkpoints.py`, `classify`), because which of them is the full
archive is in the bytes, not the key.

**Chains are short.** A new full archive is written after
`CHECKPOINT_CHAIN_MAX` (24) incremental ones, or as soon as the incremental
archives since the last full one weigh as much as it does, whichever is first.
A restore refuses a chain longer than 200 or one that loops.

---

## 3. Restore

**A restore takes only the checkpoint an earlier attempt of this task
recorded, and a task's first attempt restores nothing** (#347, owner decision
2026-09-29). A resume is by definition a new attempt with a new id, so the
checkpoint worth restoring was written by an earlier attempt. The worker does
**not** find it by listing the task's prefix for the newest manifest, which is
what it did until #347: the tenant's worker account can write anywhere under
`tenants/<tenant>/`, so any agent of the tenant — an implement step, say —
could put a checkpoint under another task's prefix before that task ran.
HOME is `work/` and `.claude/` is kept in every checkpoint, so the planter's
`~/.claude/settings.json`, and the hooks in it, arrived in the next step, and a
hook runs code without persuading any model.

What is restored, and only when every check holds
(`Worker._recorded_checkpoint`):

1. the task has had an earlier attempt: `attempt_count`, which admission
   increments in the lease's own transaction, is above 1;
2. `task.latest_checkpoint` names a checkpoint, and it resolves inside this
   task's own prefix to a manifest naming this tenant and task
   (`CheckpointManager.find_by_uri`); a pointer anywhere else is refused;
3. the pointer, the manifest and the archive sit exactly where the
   manifest's own attempt and checkpoint ids put them, and those ids are
   single key segments;
4. the attempt document of the attempt that wrote it is this tenant's and
   this task's, lists the checkpoint id in `checkpoints`, and records the
   archive's SHA-256 in `checkpoint_sha256` — both written by
   `ControlPlane.record_checkpoint` before it moves the pointer. The restore
   then verifies the archive's bytes against that digest.

Anything else starts the attempt from an empty workspace, as a first attempt
does, with one log line saying which check refused it. An attempt document
written before `checkpoint_sha256` existed records no digest, so a retry of
such an attempt starts clean once. The field is the frozen
`Attempt.checkpoint_sha256` (contract request 51, accepted 2026-10-09), whose
empty default means exactly that: no digest recorded, never "anything goes".
No backfill computes digests from the bucket, because the bucket's bytes are
what the digest exists to distrust.

**What this does and does not stop.** It stops every first attempt, and, on a
retry, an agent that can write only the bucket: a checkpoint added beside the
recorded one is never chosen, and the recorded one rewritten in place no
longer matches the digest in Firestore. It does **not** stop an agent that
also writes Firestore: Firestore has no document-level IAM
([multi-tenancy.md](multi-tenancy.md)), so an agent that knows this task's id
can raise `attempt_count`, repoint `latest_checkpoint` and write an attempt
document of an id it chose, and pass every check above. Closing that needs a
record the tenant cannot write, which is the signed step specs' work (#342).

Extraction is hostile-input handling, because a checkpoint contains a tenant's
working tree and an agent can put anything in it:

* **a symlink whose target leaves the workspace is skipped**: never created,
  and named in one log line per restore (at most 20 names, scrubbed, then a
  count), while the rest of the archive restores. uv leaves
  `bin/python -> /usr/local/bin/python3.11` in every build environment, and
  refusing the archive over it failed every later resume of the task (#286);
* **an archive inconsistent with itself is refused whole**, before a byte is
  extracted: a member path that is absolute, holds `..` or appears twice; a
  member whose path passes through a symlink member (`d -> .`, `c -> d/..`,
  then `c/x`); a hard link whose target is not an earlier regular-file member
  of the archive -- one to a missing member, to a skipped symlink, or out of
  the archive root (`../private/secret.txt`). This platform's archiver follows
  no link and writes each name once, so none of these comes from an agent's
  ordinary tree;
* extraction then runs through `tarfile.data_filter`, which checks each member
  again against the links already made (`agent_worker.checkpoint` refuses to
  import on a Python without it, and the image build asserts 3.11.4 or later);
* sockets, FIFOs and devices are skipped rather than restored.

Path traversal is checked on the way **out**, not trusted on the way in.
[`security.md`](security.md) says why each rule is there.

---

## 4. When checkpoints happen

| Trigger | Where | Skipped when nothing changed? |
|---|---|---|
| Every `checkpoint_interval_seconds` (default 120; `mock` uses 30), backing off while unchanged | worker step 8 | **yes** — nothing is written, and the interval doubles |
| Before parking on provider quota | `agent_worker/quota.py` | never |
| Before exiting on cancellation | worker shutdown path | never |
| On SIGTERM, a control-plane outage, a child await | worker shutdown paths | never |
| Final, after the runner exits | worker step 10 | never |

The interval is per runner profile (`RunnerProfile.checkpoint_interval_seconds`)
and is *mandatory* — there is no profile-level switch to turn it off, because a
profile with it off would be a profile whose long runs are unrecoverable.

**The periodic checkpoint backs off while the tree is unchanged** (#637, owner
decision 2026-10-05). At each tick the worker walks `work/` with `stat` only; if
nothing differs from the last checkpoint it writes nothing — no archive, no
`checkpoint_started` event, no id — and waits twice as long for the next look:
2 → 4 → 8 → 10 minutes at the default interval, capped by
`CHECKPOINT_MAX_INTERVAL_SECONDS` (default 600). The first change found resets
it to the base interval. A failed checkpoint is not an unchanged one and is
retried at the base interval. The cost, stated plainly: a change made just after
an unchanged look waits up to the current backoff, at most 10 minutes, to be
taken. Every other checkpoint in the table is written whether or not anything
changed — an empty incremental archive is a few hundred bytes — because each is
the one the next attempt restores (invariant 8).

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
CHECKPOINT_INTERVAL_SECONDS=120   # RunnerProfile's default; `mock` declares 30 s
```

The worker does not choose this. The dispatcher sets `CHECKPOINT_INTERVAL_SECONDS`
on every attempt from the task's runner profile
(`apps/scheduler/scheduler/dispatch.py::worker_env`), so the value lives in the frozen
catalogue: 120 s for every profile except `mock`, which declares 30 s
(`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`). The worker's own 120
(`apps/agent-worker/agent_worker/config.py::WorkerConfig.checkpoint_interval_seconds`) is only the fallback for a
process started without the variable. This block said 60 until 2026-10-02, which
nothing in the repository sets; change the interval in the catalogue (a contract
change request), not in an environment.

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
           -> restore: task.latest_checkpoint = attempt 1, seq 23, which
              attempt 1's document lists with its archive digest
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
| Resume starts from scratch with "no checkpoint is restored; starting from an empty workspace" | a check in section 3 refused the recorded checkpoint; the log line's `reason` names which | expected on a first attempt; on a retry, an error line just before it says what did not match. A checkpoint under the prefix that no attempt recorded is never restored, by design (#347) |
| Checkpoints growing every cycle | build output inside `work/` that is not in `TOOL_CACHES` or `BUILD_DIRS`, or a build directory the checkout tracks (section 1) | fix the runner; do not raise `max_bytes` |
| Restore fails with "failed integrity check" on a `ckpt-` other than the recorded one | a base of an incremental chain was rewritten or deleted in the bucket | expected refusal; the chain is bound by digest (section 2) |
| A periodic checkpoint missing for several minutes, "checkpoint unchanged; nothing written" in the log | the tree did not change; the interval backed off | none needed; the next change resets it |
| Restore fails with "holds a path outside the workspace", "through its own link", "holds a hard link" or "more than once" | the archive is inconsistent with itself: a traversing path, a member written through a symlink member, or a hard link to a missing, skipped or outside member | expected refusal; this platform's archiver never writes one, so inspect the archive before trusting its producer |
| "checkpoint restore skipped links escaping the workspace" | the agent's tree held symlinks pointing outside `work/` (uv's `bin/python` is the usual one) | none needed; those links are not recreated and the rest restored |

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
