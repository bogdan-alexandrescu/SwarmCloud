# Runbook: turn the forge-credential broker on in an environment

**When to use this.** The owner has decided to turn `forge_broker` on in
`<env>` (dev first). This is step 3 of the rollout order in
[`docs/design/user-scoped-secrets.md`](../design/user-scoped-secrets.md) §5
("The rollout order"). Steps 1 and 2 are already done in dev; step 4 (a live
run) is this runbook's verification. Step 5, removing `forge_slot_reader`, is
**not** part of this runbook: it is a separate, later owner change
([`docs/runbooks/github-app.md`](github-app.md), "Step 2: the user slots'
grants").

**Who runs it.** The owner: the tfvars edit and the decision. The release
applies it.

**Written 2026-11-24 against `main`.** Nothing has been changed by writing it.

**Placeholders.**

| placeholder | what it is |
|---|---|
| `<project>` | the deployment's own project |
| `<region>` | its region |
| `<env>` | `dev` or `prod` |
| `<state bucket>` | the state bucket that holds `config/<env>/<env>.tfvars` |
| `<tenant>` | a group workspace whose members have connected GitHub |
| `<member A>`, `<member B>` | two of its members |
| `<repo>` | a repository both may push to |

---

## 1. What changes and why

Today a tenant's worker reads a person's GitHub user slot (`swarm-tenant-<tenant>-git-u-<hex>`) with its own service account through the bootstrap grant `forge_slot_reader` (`terraform/bootstrap/forge_user_slots.tf`), so any agent of a group workspace could read any member's slot. Setting `forge_broker = true` (`terraform/infra/child_tasks.tf:71`) makes swarm-api's broker (`POST /v1/attempts/forge-credential`, `swarm_api.credbroker.ForgeCredentialBroker.release`, `apps/swarm-api/swarm_api/credbroker.py:355`) the only source of that slot, released to the current attempt of a task that person submitted and proven with the attempt key, the worker failing closed otherwise (`agent_worker.forgebroker.ForgeBroker`). It is the owner's 2026-10-11 decision that secrets are user-scoped, and the precondition for later removing the worker's own read; the [appendix](#appendix-the-switch-and-what-each-component-does-with-it) says what each component does with the switch.

---

## 2. Impact inventory

### Work in flight it can disturb

* **Attempts that use a person's slot, dispatched before the release.**
  * They have no `FORGE_BROKER` in their environment, so they keep reading
    directly. `forge_slot_reader` still stands, so they are not disturbed.
  * This is inferred from `worker_env` setting the variable at dispatch time
    only. **Not checked:** a test that asserts it.
* **Attempts dispatched in the seconds between the two new revisions.**
  * The release applies swarm-api's and the scheduler's environments in one
    apply, but each is its own Cloud Run revision.
  * A worker dispatched by the new scheduler while swarm-api's old revision
    still serves gets `503`. The worker retries for `child_submit_retry_seconds`
    (30 s, `config.py:413`; `agent_worker.children` `retrying`), then fails
    closed with `ForgeCredentialRefused`.
  * **Not checked:** whether that attempt is retried by the platform or
    fails terminally. Check the attempt's `last_error` if one appears.
* **A token re-read mid-attempt.** With the broker on, a long attempt
  re-reads its slot through the broker too
  (`tests/unit/worker/test_forge_token_reread.py`). This matters for
  rollback, below.
* **Workflows, child tasks and issue runs.** A user-slot child is keyed by the
  same `dispatch.py:1883` condition, and is covered by the live run in §6. A
  service submitter's run is unaffected.
* **Schedules.** **Not checked:** whether a schedule's task carries a `git-u-`
  slot. If it does, it follows the switch like any task dispatched after the
  release.
* **Releases.**
  * This is an env-only change. No IAM row is in the plan, so the dev
    release should not hold at `dev-iam` (`docs/ci.md`, "A dev release that
    changes IAM waits for the owner": only plans classified as IAM wait).
  * **Not checked** by running a plan. Step 2 below reads that plan.

### Stateful things it touches

| kind | name | changed? |
|---|---|---|
| setting | `forge_broker` in `config/<env>/<env>.tfvars` in `<state bucket>` | yes, `false` → `true` |
| running services | swarm-api and swarm-scheduler: one new revision each, `FORGE_BROKER` `"false"` → `"true"` | yes |
| secrets (names only) | `swarm-child-key`, the attempt-key and nonce key; read, not written | no |
| secrets (names only) | `swarm-tenant-<tenant>-git-u-<hex>` and their `-refresh` twins, read by swarm-api's broker instead of the worker | read path only |
| secrets (names only) | `swarm-tenant-<tenant>-git`, the team token | no |
| access grants | `forge_slot_reader["<tenant>"]` (`terraform/bootstrap/forge_user_slots.tf`) **stays**; that is what makes this switch's undo safe | no |
| access grants | swarm-api's slot reads and the other bootstrap forge grants (`forge_user_slots.tf`, gated by `enable_forge_user_slots`) | no |
| service identities | `swarm-agent-worker-<tenant>`, swarm-api, swarm-scheduler | no |
| stored documents | none written by the switch itself | no |

### Outside the deployment's own project

It touches nothing there:

* The change is two environment variables on two of this deployment's own
  Cloud Run services.
* It makes no IAM change at project or organisation level, and creates and
  destroys nothing.
* The release's plan guard and deny-list (`scripts/lib/common.sh`) refuse a
  plan naming a never-touch resource.
* Step 2 requires the plan to be exactly the two in-place updates, so anything
  else stops it.

GitHub is reached as before, by the same callers' tokens. The switch changes
who reads the slot, not where the token is sent.

### Steps that cannot be undone

None. `forge_slot_reader` is not removed here, so `forge_broker = false` is
the undo ([`user-scoped-secrets.md` §5](../design/user-scoped-secrets.md),
rollout step 3).

### Downtime and degraded window

No planned downtime. There are two short windows, each limited to tasks on a
person's slot:

* the seconds between the two revisions going live (above);
* on rollback, the attempts then running in broker mode (§5).

---

## 3. Preconditions

Each is read-only. Stop at the first that fails. Items 1-4 are
[`github-app.md`](github-app.md) step 2's checklist, applied to turning the
switch on.

**P1. The nonce measurement has been made for `<env>`.** That is design §5,
"The precondition this rests on", and child-tasks.md §9 item 2. In dev it was
made on 2026-10-11, recorded in the dev tfvars
([`github-app.md`](github-app.md)). In prod it has not been made. **Not made
means stop:** the broker is no guarantee there.

**P2. Child tasks are on and `swarm_api_url` is set.**

```bash
current="$(scripts/lib/resolve.sh tfvars <env>)"
grep -nE '^(enable_child_tasks|swarm_api_url|forge_broker)\b' "${current}"
```

* `enable_child_tasks = true`, `swarm_api_url` set, and `forge_broker = false`
  (or absent, the default).
* `scripts/lib/resolve.sh tfvars` is the documented fetch (`docs/ci.md`, "A
  new account exists before the release that adds it", step 2).

**P3. `swarm-child-key` has an enabled version.** This lists names, never
values, in the form `docs/runbooks/github-app.md` uses:

```bash
gcloud secrets versions list swarm-child-key --project <project> --filter=state=ENABLED --format='value(name)'
```

At least one line. The value is generated by
`scripts/create-secrets.sh --child-key` (usage at the top of the script), and
nobody types it.

**P4. The running services and worker carry the broker code, switched off.**

```bash
gcloud run services describe swarm-api --project <project> --region <region> \
  --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' | grep FORGE_BROKER | redact
gcloud run services describe swarm-scheduler --project <project> --region <region> \
  --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' | grep FORGE_BROKER | redact
```

* Both print `'value': 'false'`. The command form is from
  `docs/runbooks/onboarding-acceptance.md`.
* The worker image's commit, read off the release run, contains
  `apps/agent-worker/agent_worker/forgebroker.py` ([`github-app.md`](github-app.md)
  checklist item 4).

**P5. `forge_slot_reader` still stands.** The undo depends on it. Its
condition title is `swarm forge user slots <tenant>` (`forge_user_slots.tf`,
resource `forge_slot_reader`):

```bash
gcloud projects get-iam-policy <project> --format=json \
  | jq -r '.bindings[] | select(.role=="roles/secretmanager.secretAccessor") | select(.condition.title // "" | startswith("swarm forge user slots ")) | .condition.title + "  " + (.members|join(","))' \
  | redact
```

It prints titles and members, never a secret value. Expect one line per
Terraform tenant with members (`local.forge_user_slot_tenants`), each naming
that tenant's `swarm-agent-worker-<tenant>`. Count the lines. If it prints
none, stop: either `enable_forge_user_slots` is off or the grant is gone, and
either way the undo is not safe.

**P6. A live-run fixture exists.**

* `<tenant>` has two members, `<member A>` and `<member B>`, who have each
  connected GitHub, so each has a user slot (onboarding writes it;
  `forge_user_slots.tf` header).
* Both may push to `<repo>`.
* Read this from the console's Config › Secrets status rows, which list forge
  tokens by status, never value (`docs/design/workspace-secrets.md` §6.2).

**P7. No release is holding.** The latest `release.yml` run for `<env>` is
finished and none waits at `dev-iam` (the Actions page).

---

## 4. Steps

1. **Edit the tfvars.** Who: the owner.
   * **The change.** `forge_broker = true` in a copy of the bucket file:

     ```bash
     current="$(scripts/lib/resolve.sh tfvars <env>)"
     cp "${current}" "${TMPDIR}/new-<env>.tfvars"   # set forge_broker = true here
     diff -u "${current}" "${TMPDIR}/new-<env>.tfvars"
     ```

     The diff is one line. Have it reviewed, then write the copy to
     `gs://<state bucket>/config/<env>/<env>.tfvars` (`docs/ci.md`, "A new
     account exists before the release that adds it", steps 2 and 4; the
     bucket is versioned).
   * **It worked when** a fresh `scripts/lib/resolve.sh tfvars <env>` shows
     the line.
   * **Stop** if the diff has any other line.
   * The repository's own example, `dev.tfvars.example:170`, should then
     record the new decision in a separate docs-and-tests change.
     `tests/terraform` reads it (open decision F3).

2. **Release.** Who: the release.
   * **The change.** Merge to `main`, or open Actions › `release.yml` › Run
     workflow, `environment` = `<env>`, `skip_build` = true
     (`.github/workflows/release.yml:136-146`).
   * **It worked when** the plan shows exactly two in-place updates:
     * `module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]`
     * `module.cloud_run.google_cloud_run_v2_service.this["swarm-scheduler"]`

     Each changes only `FORGE_BROKER` from `"false"` to `"true"`. That is 0
     to add, 2 to change, 0 to destroy, and the release does not stop at
     `dev-iam`.
   * **Stop** on any other row, or a hold at `dev-iam`. Do not approve it;
     cancel the run and read why.

3. **Confirm the services took it.** Who: the owner.
   * P4's two commands now print `'value': 'true'` on both.
   * **Stop** if only one does: roll back (§5) rather than leave the two out
     of step.

4. **Watch the first hour.** Who: the owner.
   * swarm-api's log lines are `forge credential released
     decision=released …` and `forge credential refused decision=<code> …`
     (`credbroker.py:350`, :440, :475; they name tenant, task and secret,
     never the token).
   * The worker's are `forge credential released by the broker` and `the
     forge broker refused this attempt` (`forgebroker.py`, `fetch` and
     `_refused`).

   ```bash
   gcloud logging read \
     'resource.type="cloud_run_revision" AND resource.labels.service_name="swarm-api" AND textPayload:"forge credential"' \
     --project <project> --freshness=1h --format='value(timestamp,textPayload)' | redact
   ```

   **Not checked:** whether swarm-api's lines land in `textPayload` or
   `jsonPayload`. If the query prints nothing, try `jsonPayload.message:"forge
   credential"` before concluding there were none. An empty result from a
   query that matched no field is not "no refusals".

   **Stop and roll back** on any of these:

   | in the logs | why it stops |
   |---|---|
   | `forge_credential_unavailable` after step 3 | swarm-api is not serving |
   | `forge_broker_unconfigured` | a worker has no `SWARM_API_URL` |
   | `attempt_key_unregistered` | the nonce is not reaching the worker |

   A `decision=` refusal for a task whose submitter is not the slot's owner
   is the broker working.

5. **The live run** (§6). Who: the owner. It is the gate. Until it passes, the
   switch is on but not accepted.

---

## 5. Rollback

1. **Undo the switch.** Who: the owner, then the release.
   * Set `forge_broker = false` in the bucket tfvars, or restore the
     previous generation of the bucket object, which is versioned.
   * Release as in step 2. The plan is the same two in-place updates, back
     to `"false"`.
2. **Confirm.** P4 prints `'value': 'false'` on both.

**What is lost.**

* An attempt running in broker mode at the moment swarm-api turns off gets
  `503` on its next slot read and fails closed (`ForgeCredentialRefused`).
  Its work since its last checkpoint is lost (invariant 8). Attempts
  dispatched after the rollback read directly again through
  `forge_slot_reader`.
* Nothing else is lost. No secret, grant or document was changed.

**Never** roll back by removing `forge_slot_reader`, or after it has been
removed: with the grant gone and the broker off, every task on a person's slot
fails closed ([`github-app.md`](github-app.md), step 2).

---

## 6. Verify afterwards

**No acceptance group covers the broker.** The groups in `docs/acceptance.md`
("Running it") and `scripts/acceptance/groups/` were searched on 2026-11-24
for a user-slot or broker check, and none was found. The verification is the
live run that design §5 rollout step 4 and [`github-app.md`](github-app.md)
checklist item 5 require. It is run by the owner as the two members, in
`<tenant>`:

1. **`<member A>`'s agent clones and pushes `<repo>` on their own slot.**
   Submit one `claude-code` agent whose task uses `<member A>`'s GitHub
   connection.
   * Its worker log shows `forge credential released by the broker`.
   * swarm-api shows `forge credential released decision=released` for that
     task.
   * The branch is pushed as `<member A>`.
2. **A user-slot child of that agent does the same.** **Not checked:** which
   console screen starts a child on the parent's slot. Use an agent whose
   task submits a child (`docs/child-tasks-for-agents.md`).
3. **`<member B>`'s agent is refused `<member A>`'s slot.** **Not checked:**
   how to make an agent ask for another member's slot. swarm-api names a
   person's task by that person's own slot (`forge_answer`,
   `apps/scheduler/scheduler/credentials.py`), so the console offers no way to
   pick it.
   * The offline proof is
     `tests/unit/control_plane/test_forge_credential_broker.py` and
     `tests/integration/test_forge_credential_broker.py`, which hold the
     refusal.
   * Whether a live refusal is required, or the offline proof suffices, is
     open decision F2.

**Regression.** The next `accept.yml` run's groups `claude-code` and
`git-tools` are green. **Not checked:** whether `smoke`'s acceptance tasks
use a person's slot; if they use the team token, they do not exercise the
broker.

**The invariants this touches.**

* Invariant 5 (fencing) is strengthened: a superseded attempt is refused
  `attempt_superseded` and exits as fenced (`forgebroker.py`, `_refused` →
  `FencedError`).
* Invariant 9 (per-tenant isolation) is strengthened within a tenant.
* Invariant 10 is untouched: callers still name a profile, and the broker
  takes no image, command or credential from a caller.

---

## 7. Open decisions for the owner

Also in `questions.json` beside the pull request that wrote this runbook.

**F1. Turn the broker on in dev now?**
* (a) Yes, following this runbook. **Recommended:** the undo is one line
  while `forge_slot_reader` stands, and dev's preconditions P1-P3 were met
  on 2026-10-11.
* (b) Wait for the secrets store's second phase (per-person signed
  submissions).

**F2. What counts as the live run's refusal check?**
* (a) The offline tests plus a live run of items 1-2. **Recommended:** the
  console cannot make an agent ask for another member's slot.
* (b) A live refusal is also required, which needs a purpose-built probe
  first.

**F3. When to record the new value in `dev.tfvars.example`** (and so in the
tests that read it)?
* (a) In a docs-and-tests pull request right after step 3 succeeds.
  **Recommended.**
* (b) Only after the live run passes.

**F4. Removing `forge_slot_reader`** (design §5 rollout step 5).
* (a) A separate runbook and owner bootstrap change, after N days with the
  broker on and no `attempt_key_unregistered`. **Recommended.**
* (b) Never; keep the grant as a fallback. That leaves the within-tenant
  read the broker exists to remove.

---

## Appendix: the switch and what each component does with it

### What the switch is

* **Variable.** `forge_broker` (`terraform/infra/child_tasks.tf:71`, bool,
  default `false`).
* **Validation.** `!var.forge_broker || var.enable_child_tasks`
  (`child_tasks.tf:87-91`): the plan refuses it without child tasks.
* **Dev's recorded value.** `terraform/environments/dev/dev.tfvars.example:169-170`
  records dev's decision as `forge_broker = false`, "until a separate owner
  step turns it on (#1041)". The example is what the tests that hold dev
  read. The real file is the bucket copy.
* **Where it renders.** `FORGE_BROKER = var.forge_broker ? "true" : "false"`
  is set into swarm-api's environment (`terraform/infra/locals.tf:1072`) and
  swarm-scheduler's (`locals.tf:1462`).

`tests/terraform/child_tasks_infra.tftest.hcl` holds both renders and the
refusal.

### What each component does with it on

| component | on | off (today) |
|---|---|---|
| swarm-api (`ApiSettings.forge_broker`, `apps/swarm-api/swarm_api/settings.py:966`) | answers the route after its ordered checks: the worker account, the attempt proof, the tenant, the current attempt (`is_current`), the spec signature, then slot ownership and grant (`decide_slot`) | answers `503 forge_credential_unavailable` before reading anything (`credbroker.py:113-114`, the docstring at :14) |
| scheduler (`SchedulerSettings.forge_broker`, `apps/scheduler/scheduler/settings.py:482`) | `worker_env` (`apps/scheduler/scheduler/dispatch.py:1797-1799`) hands each worker `FORGE_BROKER=true`. A task that needs its submitter's slot (`needs_user_slot`, `apps/scheduler/scheduler/credentials.py:709`) is keyed with a nonce even when it is not a child (`dispatch.py:1883`) | no `FORGE_BROKER` and no nonce for that reason |
| worker (`WorkerConfig.forge_broker`, `apps/agent-worker/agent_worker/config.py:398`; `ForgeBroker.deployed`, `forgebroker.py:97`) | `resolve_git_token` (`apps/agent-worker/agent_worker/secrets.py:252`) takes a `git-u-` slot only from the broker. A refusal is `ForgeCredentialRefused`, a `SecretError`, never the tenant token | reads the `git-u-` slot directly with its own account |

**Unaffected either way:**

* a service submitter's task, which is released an App installation token
  whatever the switch says (`credbroker.py`, `_release_run_token`; docstring
  :47-49);
* the team `git` slot and the `git-r-` repository slots, which are read as
  before.
