# Runbook: retire the judgment-key file fallback and the tenant TypeSafe key grants

**When to use this.** The owner has decided to retire the worker's TypeSafe
key-file fallback and every tenant worker account's read of a `-typesafe`
secret. This is SEC-RETIRE: [`docs/design/workspace-secrets.md`](../design/workspace-secrets.md)
§6.1 step 5, the "SEC-RETIRE (owner applies)" row of its build-plan table, and
the "What is left" list under SEC-MIGRATE-TYPESAFE. It comes after the
forwarder serves every workspace that judges on TypeSafe
([`typesafe-forwarder.md`](typesafe-forwarder.md)).

**Who runs it.** Three hands:

* **The owner** decides, removes the grants swarm-api made, and applies
  bootstrap.
* **A reviewed pull request** carries the code change.
* **The release** ships that change.

**Written 2026-11-24 against `main`.** Nothing has been changed by writing it.
The code change it depends on is **not written**; §4 step 2 says what that
change must contain.

**Placeholders.**

| placeholder | what it is |
|---|---|
| `<project>` | the deployment's own project |
| `<region>` | its region |
| `<env>` | `dev` or `prod` |
| `<state bucket>` | the bucket that holds `config/<env>/<env>.tfvars` |
| `<tenant>` | a workspace id |
| `u-<user>` | a personal workspace id |
| `<context>` | the GKE context, as `typesafe-forwarder.md` builds it from the project and region |

---

## 1. What changes and why

Without the forwarder, a worker reads its workspace's (or submitter's) TypeSafe key from Secret Manager with its own account and writes it to `private/jev-typesafe.key` in the pod, where the agent's own processes can read it (`agent_worker.jevkey.prepare`, `apps/agent-worker/agent_worker/jevkey.py:244`; [`typesafe-forwarder.md`](typesafe-forwarder.md)). Even with the forwarder on, every tenant worker can still read its key through two grants: the per-secret accessor binding swarm-api adds when a key is written (`swarm_api.credentials._grant_accessor`, `apps/swarm-api/swarm_api/credentials.py:263`) and the bootstrap rows of `terraform/bootstrap/typesafe_user_keys.tf` (`google_project_iam_member.typesafe_user_key_reader`). This retirement removes the file path, that grant and its existing bindings, and those rows, so no tenant worker can read a TypeSafe key and the forwarder is the only path to TypeSafe (design §6.1 step 5).

---

## 2. Impact inventory

### Work in flight it can disturb

* **Every attempt that judges on TypeSafe and is not served by the
  forwarder** loses its Jev tools: no key file, so `jev_key_absent` and no
  jev-browser tools (`prepare` is "never fatal"). The scheduler serves a pod
  only when **all** of these hold (`scheduler.dispatch.typesafe_forwarder_on`,
  `apps/scheduler/scheduler/dispatch.py:1125`; typesafe-forwarder.md, the
  paragraph under the table):
  * the forwarder is served and `enabled`;
  * the tenant is in `typesafe_forwarder.tenants` by exact name
    (`terraform/infra/typesafe_fwd.tf:70`);
  * it is a GKE pod with the live browser, or a `jev_judgments: on` step on a
    profile carrying the Jev client;
  * the workspace's stored judgment backend is `typesafe`.

  So these lose TypeSafe judgment:
  * **every Cloud Run Job attempt**, which "is never labelled: it cannot reach
    a cluster Service";
  * **a judge running on Cloud Run**, which "cannot use it: that needs a
    decision before the judge moves" (workspace-secrets.md, "What is left");
  * **every workspace not in `tenants`**. Dev lists one,
    `dev.tfvars.example:57-62`, served from 2026-11-02 per
    typesafe-forwarder.md.

  This is open decision K1.
* **Workspaces on an internal backend** (`internal-jev`, `internal-laya`;
  `jevkey.INTERNAL_BACKENDS`, :93) are untouched: they read no key. The
  default backend is `internal-jev` (`apps/scheduler/scheduler/judgment.py:73`).
* **A group workspace's agent that used a member's personal key** through a
  `typesafe_user_key_readers` row loses that key. On the forwarder a group
  agent's ticket names only the workspace key, never the submitter's
  (`typesafe_ticket.TypesafeTicketService._key`, the owner's Q3 answer of
  2026-10-15, quoted in typesafe-forwarder.md step 2).
* **Quality gates and judgment steps.** They resolve the key through the same
  module: `qualitygates.py:332` (`jevkey.resolve`) and `lifecycle.py:10914`
  (`jevkey.resolve_workspace`). The judge reads `jevkey.KEY_FILE_NAME`
  (`judge/attempt.py:30`, :107).
  * **Not checked:** whether each of these already takes the shim when the
    pod is served. The code change in §4 step 2 must answer it.
* **Attempts running at the release** keep the worker image they started
  with, so a running attempt is not disturbed by step 2.
  * After step 3, a running pre-release attempt that re-reads its key gets
    `PERMISSION_DENIED` and logs `jev_key_unreadable`.
  * **Not checked:** whether a running attempt ever re-reads the key after
    `prepare`.
* **Workflows, schedules and releases.**
  * Workflow judgment steps are covered under the quality-gates item above.
  * Schedules are affected only through the tasks they start.
  * Step 2 is an ordinary release. Steps 3 and 4 are owner actions outside
    the release.

### Stateful things it touches

| kind | name | changed |
|---|---|---|
| code | `apps/agent-worker/agent_worker/jevkey.py` (`prepare`, `resolve`, `resolve_workspace`, `_write_private`), its callers above, `apps/swarm-api/swarm_api/credentials.py` (`_grant_accessor` for `typesafe`) | step 2, by a pull request |
| secrets (names only) | `swarm-tenant-<tenant>-typesafe` and `swarm-tenant-u-<user>-typesafe` (`jevkey.secret_id`, :99): their IAM policies lose the tenant worker member. **The secrets and their versions stay**: the forwarder reads them | step 3 |
| access grants | per-secret `roles/secretmanager.secretAccessor` for `swarm-agent-worker-<tenant>` on each `-typesafe` secret, made by swarm-api | step 3, removed |
| access grants | `google_project_iam_member.typesafe_user_key_reader["<group>/u-<user>"]` (`terraform/bootstrap/typesafe_user_keys.tf`), conditioned to one secret | step 4, destroyed |
| access grants that **stay** | the forwarder's read, `google_project_iam_member.typesafe_forwarder_accessor` (`terraform/bootstrap/typesafe_forwarder.tf:136`, role `swarmTypesafeForwarderAccessor`); swarm-api's key-writing grants | no |
| setting | `typesafe_user_key_readers` in the bootstrap root's own `terraform.tfvars`. It is not in the repository (`.gitignore:40`); `docs/design/day-one/manifest.draft.yaml:3765` records the row it held | step 4 |
| setting | `typesafe_forwarder.tenants` in `config/<env>/<env>.tfvars`, **read only**; widening it is the forwarder runbook's step, not this one | no |
| service identities | `swarm-agent-worker-<tenant>`: they lose a grant and are not deleted | — |
| stored documents | `provider_keys` and the store record that says a key is set (`jevkey.KEYS_COLLECTION`, :75). Unchanged: the console still shows the key as set | no |

### Outside the deployment's own project

It touches nothing there:

* Every grant removed is on a secret in `<project>`, or a project IAM member
  of `<project>` conditioned to one `swarm-tenant-*-typesafe` secret by its
  full resource name (`typesafe_user_keys.tf`, "THE CONDITION").
* Every member is a `swarm-agent-worker-*` account.
* The other team's secrets cannot match those names
  (`terraform/bootstrap/typesafe_forwarder.tf`, its header).
* Step 3 lists only secrets whose names end `-typesafe` and start
  `swarm-tenant-`, and stops on any member that is not a
  `swarm-agent-worker-*` account.

TypeSafe's own API (`api.typesafe.ai`) is still reached only by the forwarder,
as before ([`typesafe-egress.md`](typesafe-egress.md)).

### Steps that cannot be undone

None, strictly:

* the code change reverts by a release;
* the bindings and the bootstrap rows can be re-added.

**What does not come back** is the judgment an unserved attempt did without
while the fallback was gone.

### Downtime and degraded window

No service goes down. From step 2's release onwards, every unserved TypeSafe
attempt runs with no TypeSafe Jev tools, until either the workspace is served
or the change is rolled back.

---

## 3. Preconditions

Each is read-only. Stop at the first that fails.

**P1. The forwarder is serving, in this environment.** typesafe-forwarder.md,
"Serve a workspace", step 1:

```bash
kubectl --context <context> -n swarm-system get configmap swarm-typesafe-fwd-config -o jsonpath='{.data.config\.json}'
```

It prints `"enabled":true` and the `tenants` list. Use `kubectl_bin` from
`scripts/lib/common.sh` if the `kubectl` on `$PATH` is old (`CLAUDE.md`,
"Writing scripts").

**P2. Every workspace that judges on TypeSafe is in `tenants`.**

1. For each workspace, read its backend
   (`GET /v1/workspaces/{tenant_id}/judgment-backend`,
   `apps/swarm-api/swarm_api/routes/judgment.py:116`):

   ```bash
   scripts/api.sh GET /workspaces/<tenant>/judgment-backend
   ```

2. Every workspace answering `typesafe` must be in P1's list.
3. **Stop** on one that is not, and widen the forwarder first (the forwarder
   runbook), or record the owner's K1 answer that it may lose TypeSafe
   judgment.

**Not checked:** a route that lists every workspace's backend at once. This is
one call per workspace from Admin › People's list.

**P3. No fallback reads over the observation period** (design §6.1 step 5: "a
release-only period with no `jev_key_unreadable` and no fallback reads
logged"; length: open decision K2).

* **The log fields.** The worker logs JSON with `message` and its fields at
  the top level (`agent_worker.logs`, `log`, `apps/agent-worker/agent_worker/logs.py:216-227`).
  * A fallback read is `jev_key_resolved` with `scope` and `secret` and
    **no** `delivery` (`jevkey.prepare`).
  * A served read carries `delivery: inject` (`_shim_env`).
* **The resource types.** Worker logs are `cloud_run_job` and `k8s_container`
  (the filter `docs/epic-triage-2026-10-09.md` row 51 quotes from
  `terraform/modules/monitoring/metrics.tf`).

```bash
gcloud logging read \
  '(resource.type="cloud_run_job" OR resource.type="k8s_container")
   AND (jsonPayload.message="jev_key_unreadable"
        OR (jsonPayload.message="jev_key_resolved" AND NOT jsonPayload.delivery="inject"))' \
  --project <project> --freshness=<period> --format='value(timestamp,jsonPayload.labels.tenant_id,jsonPayload.message,jsonPayload.scope)' | redact
```

* **It passes when** it prints zero lines. Count them.
* **First prove the query matches anything.** Run it once more with
  `jsonPayload.message="jev_key_resolved"` alone over the same period and see
  served lines. An empty answer from a filter that matches no field is not
  "no fallback reads".
* **Caveat.** An agent can write JSON lines to the worker's stdout
  (`docs/epic-triage-2026-10-09.md` row 51), so a line can be forged, never
  hidden. A forged fallback line errs towards waiting, not towards
  retiring early.

**P4. Who holds a `-typesafe` secret today.**

1. List the secrets by name (the API lists names, never payloads):

   ```bash
   gcloud secrets list --project <project> --filter='name ~ swarm-tenant-.*-typesafe$' --format='value(name)'
   ```

   **Not checked** against a repository doc: the `--filter` regex form. Read
   the output by eye. It must name only `swarm-tenant-…-typesafe` secrets.
2. For each, read its members. The form is from
   [`github-app.md`](github-app.md):

   ```bash
   gcloud secrets get-iam-policy swarm-tenant-<tenant>-typesafe --project <project> --format='value(bindings.members)'
   ```

Record each `swarm-agent-worker-*` member. Those are step 3's removals.
Anything that is neither a `swarm-agent-worker-*` account nor one of the
platform's own accounts is a stop: report it, do not touch it.

**P5. The bootstrap rows.** In an up-to-date `main` checkout, with the
bootstrap root's own `terraform.tfvars`:

```bash
grep -n -A3 typesafe_user_key_readers terraform/bootstrap/terraform.tfvars
terraform -chdir=terraform/bootstrap state list | grep typesafe_user_key_reader
```

The first is the check the day-one manifest names
(`manifest.draft.yaml:3764`). The second lists the instances step 4 will
destroy. `terraform -chdir=terraform/bootstrap init -reconfigure` first, per
`docs/ci.md`, "A new account exists before the release that adds it", step 3.

**P6. No Terraform tenant lists `typesafe` in its `providers`.** A listed
provider gets a Terraform-made secret with an **authoritative** accessor list
that names the worker (`terraform/modules/secret_manager/main.tf:31-35`,
`accessors`):

```bash
current="$(scripts/lib/resolve.sh tfvars <env>)"
grep -n 'providers' "${current}"
```

No line names `"typesafe"`. If one does, removing that worker's read is a
tfvars change through the release (`dev-iam`), and it must be added to this
runbook before going on.

**P7. The code change exists, reviewed and green in CI** (§4 step 2). It does
not exist on 2026-11-24.

---

## 4. Steps

1. **Decide.** Who: the owner.
   * Answer K1-K4 below.
   * **It worked when** each answer is recorded on the epic.
   * **Stop** if K1 is "every TypeSafe workspace must keep Jev on Cloud Run
     too": the forwarder cannot serve Cloud Run, so the retirement waits for
     that design.

2. **Ship the code change.** Who: a pull request reviewed by the owner (it
   touches credentials, so it gets the one review `CLAUDE.md` asks for), then
   the release. The change is **not written**. It must:
   * make `jevkey.prepare` return `{}` unless `inject` is set: no `resolve`,
     no `_write_private`, no `KEY_FILE_NAME` file;
   * make the quality gates (`qualitygates.py:332`), the workspace resolve
     (`lifecycle.py:10914`) and the judge (`judge/attempt.py`) read no key
     file and no secret, taking the shim where served and nothing otherwise;
   * make `swarm_api.credentials` stop calling `_grant_accessor` for the
     `typesafe` provider (design §6.1 step 5);
   * update the tests that hold today's behaviour:
     * `tests/unit/worker/test_jev_runner.py`, including the design-doc
       phrase check at about :488-491 (`"typesafe_user_keys.tf"`,
       `"same uid"`);
     * the equality between the two key orders, which §6.1 step 3 says
       "stays until step 5";
   * add the tftest the build-plan row names: "the tenant GSA holds no
     accessor on a `-typesafe` … secret (a tftest over the plan)".

   **It worked when** the release is green and a served probe (§6) still
   passes. **Stop** on a red acceptance run or a `typesafe_refused` rate above
   zero: roll back (§5, step 2).

3. **Remove swarm-api's per-secret grants.** Who: the owner.
   * For each `swarm-agent-worker-*` member P4 recorded, remove it from that
     secret's `roles/secretmanager.secretAccessor` binding, leaving every
     other member, the forwarder's grant included.
   * **No removal command is given here.** None is documented in this
     repository for a per-secret binding, and this runbook names no command
     it did not find. Open decision K3 asks whether the step-2 pull request
     should carry a script for it, with a typed confirmation that ignores
     `SWARM_ASSUME_YES` (`CLAUDE.md`, "Writing scripts").
   * **It worked when** P4's `get-iam-policy` command prints no
     `swarm-agent-worker-*` member for any `-typesafe` secret. Count the
     secrets read, and say the count.
   * **Stop** on any `jev_key_unreadable` in the hour after: some reader was
     missed by step 2.

4. **Remove the bootstrap rows.** Who: the owner, `make bootstrap`
   (`Makefile:87`; `scripts/bootstrap.sh` applies `main`'s code after a typed
   confirmation, :348-350).
   * **The change.** `typesafe_user_key_readers = {}` in the bootstrap root's
     `terraform.tfvars` (the variable's default, `typesafe_user_keys.tf:35`).
     Deleting the file and the variable is a later code change.
   * **It worked when** the plan shows only destroys of
     `google_project_iam_member.typesafe_user_key_reader["<group>/u-<user>"]`,
     one per P5 instance, "0 to add, 0 to change, N to destroy", and the
     `typesafe_user_key_grants` output empties.
   * **Stop:** any other row is a stop, above all one touching
     `typesafe_forwarder_accessor`.

---

## 5. Rollback

In reverse order. Each step stands alone.

1. **Undo step 4.**
   1. Restore `typesafe_user_key_readers` to the rows P5 recorded.
   2. `make bootstrap`. The plan re-creates the same instances.

   Nothing is lost.
2. **Undo step 2.**
   1. Revert the pull request.
   2. Release.

   Attempts dispatched after it write the key file again, **but only if the
   grants are back** (steps 1 and 3 of this rollback). Without them they log
   `jev_key_unreadable` and run without Jev, as during the retirement.
3. **Undo step 3.** Re-add each worker member P4 recorded to its secret's
   accessor binding.
   * The same tool question as step 3 applies (K3).
   * Alternatively, re-saving the key from the console makes swarm-api add the
     binding again (`credentials.put_credential` → `_grant_accessor`), but
     only while step 2 is reverted.

**What is lost:** the Jev judgments unserved attempts ran without during the
window. Nothing stored is lost. No secret version is touched at any step.

---

## 6. Verify afterwards

* **The forwarder probe.** typesafe-forwarder.md, "See one Engineering Jev call
  go through the forwarder", run for each served workspace.
  * It passes when the forwarder logs `"event": "typesafe_forwarded"` with
    `"code": "forwarded"` for the agent's task
    (`apps/swarm-typesafe-fwd/swarm_typesafe_fwd/forwarder.py:270`).
  * The worker shows `jev_key_resolved` with `delivery: inject`.
* **No key reads at all.** P3's query over the 24 hours after step 3 prints
  zero lines, and its control query still prints served lines.
* **No worker holds a key.** P4's `get-iam-policy` loop shows no
  `swarm-agent-worker-*` member on any `-typesafe` secret. P5's `state list`
  shows no `typesafe_user_key_reader`.
* **The tftest** added in step 2 is green in CI.
* **Acceptance.** There is no acceptance group for TypeSafe judgment
  (`docs/acceptance.md`, "Running it", searched 2026-11-24). `live-browser`
  runs on a schedule (`docs/ci.md`, "live-browser runs on a schedule").
  **Not checked:** which judgment backend `smoke` uses. If it is internal,
  that group does not exercise this change.

**Invariants touched.** Invariant 9 (per-tenant isolation, own secrets) is
strengthened: a tenant's worker no longer reads even its own TypeSafe key.
Invariant 5 holds through the forwarder's fencing check (`stale_generation`).

---

## 7. Open decisions for the owner

Also in `questions.json` beside the pull request that wrote this runbook.

**K1. Attempts the forwarder cannot serve** (Cloud Run Jobs, a Cloud Run
judge, unlisted workspaces).
* (a) Accept that they run without TypeSafe judgment, or switch those
  workspaces to an internal backend. **Recommended:** it is what makes "no
  worker reads a key" true.
* (b) Keep the file fallback for Cloud Run only. The grants then stay, and the
  retirement is not complete.
* (c) Wait until the judge and every TypeSafe workspace run on GKE.

**K2. Observation period for P3.**
* (a) 14 days with no fallback read. **Recommended:** it matches the window
  the epic uses for graph staging.
* (b) 7 days.
* (c) One release cycle.

**K3. How step 3's per-secret removals are made.**
* (a) A script in the step-2 pull request: deny-listed, `redact`-piped, typed
  confirmation, names only `swarm-tenant-*-typesafe`. **Recommended.**
* (b) By hand in the console's secret permissions, one secret at a time.

**K4. Order of step 2 and steps 3-4.**
* (a) Code first, then grants. **Recommended:** a code revert is the whole
  rollback until step 3.
* (b) Grants first, as a live test that nothing still reads a key. Any
  `jev_key_unreadable` names the reader. Cheaper to learn, but users see
  missing Jev before the code says why.
