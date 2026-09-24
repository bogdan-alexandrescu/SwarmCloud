# Swallowed-stderr sweep, 2026-09-18/19

Five independent lenses over `scripts/`, every candidate then handed to a
separate agent told to REFUTE it and to default to refuted when uncertain.
76 candidates examined, 71 confirmed, 5 refuted. Collapsed by call site below,
because several lenses found the same line independently.

## The class

A probe -- `gcloud describe/list`, `curl`, `kubectl`, `jq` -- has its stderr sent
to `/dev/null`, or its status thrown away by `|| true`, or (a variant that turns
up repeatedly here) its status read from the END of a pipeline rather than from
the command that matters. The script then cannot tell "resource absent" from
"auth expired / permission denied / API disabled / wrong region / network blip",
and its hardcoded message asserts absence.

Everything in this class fails OPEN and SILENTLY. None of it produces a red
test, an alert or a stack trace. That is what makes it worth a sweep rather than
a code review: only a reader comparing a probe against its message can see it,
and nothing makes anyone compare them.

Confirmed live four times on 2026-09-18 alone: a build manifest written empty
while the build reported "ok built 6 image(s)"; a registry listing read as zero
images when the session had expired; a `make deploy` that exited 2 and was
reported as success because a wrapper's `echo` succeeded; and a service
environment read as empty for the same reason.

## What is NOT here

`gcloud_auth_failure` / `die_if_auth_failure` in `scripts/lib/common.sh` are the
correct fix and are already applied at several sites (`account.sh:235`,
`create-secrets.sh:211`, `push-images.sh:97`, `build-images.sh`). Call sites
already routing through them were excluded rather than reported.

## Fixed so far

* `scripts/build-images.sh` digest read-back -- stderr captured, unreadable
  digest now fatal, and the probe changed from `images describe` (which needs
  `containeranalysis.occurrences.list`) to `tags list`, which does not.

## Confirmed, not yet fixed


### high (31)

**`scripts/account.sh:119`** — warn "could not grant: broker may read swarm-account-<tenant>-<label>-refresh" (and the same for the two other bindings). Nothing aborts: cmd_add continues to register_account and ends at line 276-277

* also caused by: stderr is discarded inside _bind, so "expired session", "you lack secretmanager.secrets.setIamPolicy", "the broker service account does not exist in this project" and "policy version conflict" all produce the identical warn line.

* leads to: The operator reads the final `ok` and walks away. The account is registered in Firestore but the quota broker cannot read the refresh secret, so it is never refreshed; the unexchanged refresh token then expires on its own — the exact outage this file's header (lines 88-95) describes and blames creat

**`scripts/account.sh:133`** — no worker service account for tenant ${TENANT} yet  /  run: scripts/register-tenant.sh --tenant ${TENANT}

* also caused by: iam.serviceAccounts.get denied (gcloud's own wording for it is "(or it may not exist)" — already captured as a non-auth fixture at scripts/lib/auth-guard.sh:95), session expired between the guarded describe at account.sh:231-235 and this call, wrong PROJECT_ID, or the SA existing under a different n

* leads to: The secretAccessor grant on the short-lived secret is silently skipped, so the tenant's worker cannot read the access token the broker publishes — every task for that tenant fails at credential load. The operator is sent to re-run register-tenant.sh, which creates/updates the service account and say

**`scripts/concurrency-test.sh:45`** — the global pool has no limit configured; run 'make infra' first

* also caused by: pool_doc -> fs_get -> fs_request, which never checks an HTTP status. Any error body decodes to null, and effective_limit over null is `[0] | min` = 0 — indistinguishable from a real pool whose hard_limit is 0. Causes: access-token expiry, datastore.entities.get denied, the wrong FIRESTORE_DATABASE, 

* leads to: Runs `make infra` — a terraform apply against the shared saga-agents-staging project — to create a pool configuration that already exists and is correct.

**`scripts/concurrency-test.sh:82`** — No failure message. VIOLATIONS stays 0, so assert_eq "0" "${VIOLATIONS}" "pool-over-limit samples" (106) PASSes, as do peak global usage (111), global active at end (120) and peak holding capacity (12

* also caused by: fs_list_docs on an error body yields no documents, so SNAPSHOT is []; holding_capacity and fs_count return 0 for the same reason. Causes: denied datastore.entities.list, a 429/503, or the cached _ACCESS_TOKEN expiring mid-run -- the exact build-images shape, since submissions use a separately cached

* leads to: Line 99 also sees zeros and breaks early with 'backlog drained', so the suite finishes fast and green having observed nothing, and writes build/concurrency-samples-<env>.jsonl full of pools:[] samples that later analysis reads as evidence. A human treats a green concurrency-test as sign-off that inv

**`scripts/create-secrets.sh:222`** — Nothing at all. With PREVIOUS empty, the guard at line 229 (`if [[ "${DISABLE_PREVIOUS}" -eq 1 && -n "${PREVIOUS}" ]]`) is false, the whole "Disabling superseded versions" step is skipped without a wo

* also caused by: Expired session, missing secretmanager.versions.list (an operator can hold secretVersionAdder without list), a wrong PROJECT_ID, or a 5xx — all collapse to the same empty string as "this secret genuinely has no enabled versions".

* leads to: The operator asked for `--disable-previous`, sees a clean success, and believes the superseded credential is disabled. It is still ENABLED and still usable. When the reason for the rotation was a leaked key, the leaked key keeps working and nobody re-checks, because the tool said the rotation comple

**`scripts/destroy.sh:340`** — SHARED RESOURCES ARE MISSING AFTER DESTROY: ${MISSING[*]-}  /  This should be impossible -- the plan assertions passed. Escalate immediately.  (destroy.sh:386-388, exit 3)

* also caused by: The same discarded-stderr probe is repeated for the shared VPCs (destroy.sh:349), the three shared buckets (destroy.sh:359) and the eleven shared service accounts (destroy.sh:377). A terraform destroy is long; a session that expires during it, or a caller lacking container.clusters.get / compute.net

* leads to: The operator is told, in the strongest words the repository has, that `make destroy` just deleted another team's live GKE cluster, VPCs, buckets and service accounts — the one outcome CLAUDE.md rule 2 exists to prevent. They escalate to the other team, page people, and start planning a restore of re

**`scripts/failure-test.sh:111`** — PASS — malformed bodies rejected: 5

* also caused by: `else` here means only 'not 2xx', and `2>&1` throws away curl's error text. An expired ID token (401), a missing run.invoker binding (403), a cold-start 503, a DNS failure and a 30s timeout each increment REJECTED exactly as a genuine 400 does. Five bad bodies fired at a dead endpoint produce a flaw

* leads to: Reports input validation as proven when not one of the five requests was evaluated — including `{"runner_profile":"../../etc/passwd"}`. The suite goes green and nobody looks again.

**`scripts/lib/common.sh:481`** — cannot resolve the API URL; set API_URL in .env or deploy first

* also caused by: Session expired (the documented failure: print-access-token still returns a valid-looking ya29 token, so nothing earlier objects); run.services.get denied for an operator who has Firestore/API access but no run.viewer; Cloud Run Admin API disabled (the FAILED_PRECONDITION string is already a fixture

* leads to: This is the entry point for every ops script that talks to the control plane (scripts/api.sh, status, smoke/quota/race/load tests). The operator is told the API is not deployed and reaches for 'make deploy' — a redeploy of a healthy control plane, in the middle of the incident they were trying to di

**`scripts/lib/common.sh:544`** — No message, and no non-zero status. curl has no -f and no -w, so an HTTP 401/403/429/500 exits 0 and the error body is returned as if it were data. Verified locally: the body {"error":{"code":403,...}

* also caused by: Expired gcloud session (UNAUTHENTICATED), missing datastore/firestore permission on the named database, wrong FIRESTORE_DATABASE, database not yet created, quota 429, or a Firestore 5xx. All are indistinguishable from "the document does not exist" / "the collection is empty" / "the write succeeded".

* leads to: Everything downstream acts on "absent": pause-swarm.sh reports admission paused when nothing was written and the operator then does maintenance believing no work is being admitted; register-tenant.sh:233-249's re-point guard reads EXISTING_DOC as null, concludes the tenant is new, and writes princip

**`scripts/lib/common.sh:569`** — Emits `[]` and stops paginating. Downstream: status.sh prints "no pools configured yet" / "no provider state recorded yet"; race-test.sh:202 prints t_pass "every pool is within [0, effective_limit]"; 

* also caused by: Verified: an error body `{"error":{...}}` has no `.documents`, so `// []` yields `[]` exactly as an empty collection does. Causes: expired session, PERMISSION_DENIED, wrong database name, a curl timeout, or a genuinely empty collection.

* leads to: The accounting invariants (CONTRACT.md 2 and 3) are asserted by `join(", ")` over this list, so an unreadable Firestore prints a green PASS for the two checks that exist to catch a double-release or a missing release. A run that proved nothing is recorded as a run that proved the invariant.

**`scripts/lib/common.sh:603`** — the count 0 — rendered as PASS "tasks holding capacity back to baseline" (smoke-test.sh:140, failure-test.sh:131) and as "0 task(s) hold capacity" in status.sh

* also caused by: fs_count_where pipes an unchecked fs_request response through jq; a 401/403/429/5xx error body has no aggregateFields, so `first // "0"` yields 0 by exactly the same route an empty collection does. holding_capacity (testlib.sh:131-138) sums four of these, and active_leases is one more, so a broken F

* leads to: 'No capacity leaked across any failure path' and 'Capacity was returned' pass green while the platform may be holding every lease it ever took; on the status dashboard the same zeros read as an idle, healthy swarm. The check passes hardest precisely when the probe behind it is broken.

**`scripts/lib/common.sh:656`** — No output at all. status.sh then prints "  none active" under Leases; concurrency/race tests read zero unreleased leases; purge-data.sh's select_docs finds nothing to delete.

* also caused by: Verified: `.[]?` over an error object yields the error value, whose `.document` is null, so `select` drops it — byte-identical to a query that legitimately matched nothing. Causes: UNAUTHENTICATED, PERMISSION_DENIED, a FAILED_PRECONDITION for a missing composite index (real for the created_at filter

* leads to: "Leases: none active" is read as "no capacity is held", so expired leases holding slots become invisible at the moment the operator is asking why nothing new is being admitted. The index case is nastier still: it produces the same emptiness forever, so the reader concludes the collection is empty ra

**`scripts/lib/deploy.sh:80`** — warn "Cloud Run service ${service} does not exist yet; run 'make infra' first", continue -- then ok "deployed ${TAG}" at the end, exit 0.

* also caused by: stderr discarded: an expired session, missing run.services.get, or the wrong REGION. If it fires for all four services nothing is updated and the run still succeeds.

* leads to: The operator is told the services do not exist and sent to make infra, while the pipeline records a successful deploy of TAG. The old revisions keep serving as release notes, the CI summary and the ok line all say the new tag is live.

**`scripts/lib/testlib.sh:96`** — the API at <url> did not answer /readyz. Run 'make deploy', or set API_URL.

* also caused by: api_reachable (scripts/lib/common.sh:529-531) is `curl -sS -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(id_token)" "$(api_url)/readyz" 2>/dev/null | grep -q '^2'` -- stderr discarded, every outcome collapsed to one boolean. Non-2xx also comes from: the caller lacking roles/run.in

* leads to: Runs `make deploy` -- a real terraform apply or `gcloud run services update` roll of the control plane in the shared saga-agents-staging project -- against a deployment that is healthy; or edits API_URL to point away from the correct service. The actual fix in most of these cases is `gcloud auth log

**`scripts/lib/testlib.sh:107`** — POST /v1/tasks returned HTTP <API_STATUS> — a code this shell never set

* also caused by: Exactly the command-substitution trap CLAUDE.md documents: `response="$(api_post ...)"` runs api_post in a subshell, so the API_STATUS it assigns dies with the subshell and the parent prints whatever was left in the variable (verified in bash). In smoke-test.sh the last in-shell api_post is line 75'

* leads to: The operator reads the printed code as the API's verdict on this submission and chases a 422 validation bug in the admission path (or 'HTTP 0' as a network outage), when the truth was a 401 from an expired token, a 403 from missing run.invoker, or a 503 from a failing revision. The body is printed o

**`scripts/lib/testlib.sh:122`** — MISSING — surfaced as smoke-test.sh:121 "task did not reach a terminal state within 600s (stuck at MISSING)" and failure-test.sh:57 "no terminal state within Ns (at MISSING)"

* also caused by: fs_request (scripts/lib/common.sh:544-555) runs curl with no -f, no status capture and no error check at all, so a Firestore REST error body ({"error":{"code":401|403|429,...}}) flows straight into `if .fields then doc else null end` -> null -> `.state // "MISSING"`. Causes: the cached _ACCESS_TOKEN

* leads to: 'MISSING' asserts the task vanished from the authoritative store, so the operator hunts for a scheduler or reconciler that deleted a task — after wait_for_state has already burned the entire timeout polling an endpoint that was returning 403 every two seconds.

**`scripts/pause-swarm.sh:74`** — warn "pool ${pool} does not exist; skipping (refusing to create a pool with no limit)", then a state file carrying pools:[] (107-112), ok "recorded ${STATE_FILE}", and ok "admission paused", exit 0.

* also caused by: existing comes from fs_get on line 73, an unchecked curl. A 403/401/429/503 returns an error object with no .fields, so the jq emits exactly the literal null this guard tests for. Denied, throttled, expired-token and genuinely-absent collapse into one value.

* leads to: This is the incident command. The operator reads 'admission paused', stops paging and moves on to the incident itself -- while every pool is still enabled and the scheduler keeps admitting. The state file then tells resume-swarm there is nothing to undo.

**`scripts/pause-swarm.sh:87`** — ok "paused ${pool} (${active} lease(s) still holding capacity)" (90), and the pool is added to the paused array written to the state file.

* also caused by: fs_patch (common.sh:580-589) ends in fs_request ... >/dev/null, so a 403 PERMISSION_DENIED, a 401 from the expired cached token, or a 429 returns an error body that is discarded with exit 0. There is no read-back, so a rejected write is reported as a completed pause.

* leads to: Worse than the read case because the pool WAS readable: the operator gets a per-pool ok line and a state file naming every pool, and still nothing is paused. Re-running status.sh to verify looks consistent only if that read fails too.

**`scripts/purge-data.sh:108`** — Each collection prints as 0 (110), then lines 115-117: ok "nothing to purge"; exit 0.

* also caused by: fs_count -> fs_count_where (common.sh:594-604) ends in jq '... | first // "0"'. Fed the error object from a denied or throttled aggregation query it yields the string 0. The gate at line 75 is fs_database_exists, a gcloud call needing a different permission, so it passes while the REST reads fail.

* leads to: The operator reads 'nothing to purge', exit 0, and reports the environment clean. With WITH_ARTIFACTS and WITH_SECRETS defaulting to 0 (36-37) this is the default path, so the run exits before the confirmation could print a number that looked wrong.

**`scripts/purge-data.sh:168`** — Nothing on failure. The caller then prints ok "${collection} purged" (246) and ok "purged ${DELETED} document(s)" (295). DELETED counts document NAMES read out of a file (211, 245), never confirmed de

* also caused by: fs_request (lib/common.sh:544-555) is curl -sS with no -f and no %{http_code}: every non-2xx is a JSON error body on stdout with exit 0, and >/dev/null discards it. A 403 on datastore.entities.delete, a 401 because the cached _ACCESS_TOKEN (common.sh:419-426, minted once, never refreshed) expired mi

* leads to: The operator records 'purged 41912 document(s)' for a retention run or a tenant deletion request and closes the ticket. Every document is still there, and the GCS backup taken at line 129 makes it look diligent. Nobody re-checks a purge that reported a count.

**`scripts/purge-data.sh:210`** — dim "  ${collection}: nothing matched" (236), then ok "purged 0 document(s)" (295), exit 0.

* also caused by: select_docs ends in fs_query/fs_list, unchecked curl piped into jq. On 403/401/429/503 the Firestore error object makes fs_list's '.documents // []' return [] and fs_query's 'select(.document != null)' return nothing, exit 0, empty file; the || true swallows hard curl failures too. A denied read is 

* leads to: The operator concludes the tenant or older-than window is already clean and stops. Tenant data survives a purge that reported completion.

**`scripts/purge-data.sh:261`** — warn "nothing to delete under ${PREFIX}/" and then, unconditionally on the next line, ok "artifacts removed".

* also caused by: Any nonzero from gcloud lands in this branch: expired session, missing storage.objects.delete, a bucket in another project, a retryable 503. The exit code is never inspected and stderr went through the pipe, so absence and denial share one branch.

* leads to: The line immediately after the warning asserts the opposite of what happened. The operator sees 'artifacts removed' and believes tenant artifacts are gone; they are untouched in GCS, still billed and still readable.

**`scripts/purge-data.sh:274`** — no matching secrets  — and then, at purge-data.sh:295, ok "purged ${DELETED} document(s)"

* also caused by: secretmanager.secrets.list denied (a very common split: an operator can read and delete Firestore data without Secret Manager list rights), expired session, wrong PROJECT_ID, or a malformed --filter expression. Note the label filter is built at purge-data.sh:272-273 from the tenant id, so a filter s

* leads to: This runs with --with-secrets, i.e. the operator is offboarding a tenant or responding to a leaked provider key. They read "no matching secrets", see the run end in ok, and record the tenant's credentials as destroyed. The secrets are still live in Secret Manager, still readable by whatever can reac

**`scripts/resume-swarm.sh:68`** — warn "pool ${pool} does not exist; skipping" per pool, then ok "resumed 0 pool(s)" (116), exit 0 -- after the state file has been mv'd to a timestamped name at 111-113.

* also caused by: Identical to pause: the error object from fs_get has no .fields, so the jq prints null. Any 403/401/429/503 makes every pool named in the state file look deleted.

* leads to: The operator believes the pools were removed while the swarm was paused and starts recreating them by hand, or re-runs with --all (46) which enables EVERY pool including ones paused for unrelated reasons. The state file recording what to resume has already been rotated away, so the safe retry no lon

**`scripts/smoke-test.sh:78`** — PASS — rejected with HTTP 401 / rejected with HTTP 403

* also caused by: 401 and 403 sit inside the 4xx window this test accepts. An expired ID token, an audience mismatch, or a caller without roles/run.invoker all land there, as does a 404 if API_PREFIX or the route moved. Only a 400/422 actually demonstrates that the unknown runner_profile was refused by the frozen pro

* leads to: The operator sees a green line for invariant 10 and stops. The printed code (401) is the only tell, and it is wrapped in the word 'rejected', which reads as the profile check having worked.

**`scripts/smoke-test.sh:100`** — PASS — request carrying an image/command was rejected outright (HTTP <stale code>)

* also caused by: The else branch is reached whenever the pipeline yielded no id. That includes 401/403 (expired token, missing run.invoker), any 5xx, a curl timeout or DNS failure, and a response whose shape changed so jq found no id field. None of those is evidence that the API adjudicated the injected image/comman

* leads to: Records invariant 10 (callers never supply images, commands or resource specs) as verified and green when the request was never evaluated by the API at all, and cites a status code belonging to an earlier request as the proof.

**`scripts/status.sh:59`** — Renders as "  no swarm services deployed" (status.sh:299), "  job executions: 0 active, 0 completed (last 0 listed)" (status.sh:309) and "  per-tenant job resources: 0" (status.sh:312); with nothing e

* also caused by: Any failure of `gcloud run services list` / `run jobs list` / `run jobs executions list`: expired session, run.services.list denied, Cloud Run Admin API disabled, wrong region, network blip. Worse, the `if` cannot even see a hard failure: the exit status captured is that of `redact` at the end of th

* leads to: status.sh is the incident dashboard. An operator debugging "work sits in READY while pools are idle" reads a control plane with zero services, zero jobs and zero executions and concludes the deployment is gone — escalating a redeploy, or chasing a phantom outage — when the services are up and only t

**`scripts/status.sh:68`** — warn "Firestore database '...' does not exist yet -- run 'make infra'" (214); every task count then renders 0, leases 'none active', pools 'no pools configured yet', and finally ok "nothing needs atte

* also caused by: fs_database_exists (common.sh:664) is gcloud ... >/dev/null 2>&1: an expired session, missing datastore.databases.get, or a wrong PROJECT_ID / FIRESTORE_DATABASE. db_ok=false then skips every collect() branch, so states, leases, pools and quota keep their empty initialisers instead of being marked u

* leads to: The dashboard an operator opens during a 'nothing is running' incident shows a clean idle platform and tells them to run make infra -- a terraform apply against a live environment -- because their credentials expired.

**`scripts/status.sh:74`** — 'demand-free : QUEUED 0   PARKED 0   READY 0   SUBMITTED 0', 'holding cap : LEASED 0 ...', '0 task(s) hold capacity' (230-236), and ok "nothing needs attention" -- with no warning at all, because db_o

* also caused by: fs_count returns the string 0 for an error body (common.sh:603, first // "0"). The gcloud access that gates db_ok and Firestore REST document/aggregation access are different permissions, and on a long --watch a different token lifetime: _ACCESS_TOKEN is cached once and never refreshed, so hour two 

* leads to: Strictly worse than the db_ok=false path because nothing is flagged. An operator watching a stalled swarm sees a fully drained system and concludes the backlog cleared; pools, quota and the attention list are empty for the same reason.

**`scripts/status.sh:104`** — Silence: with dispatch_paused false the "DISPATCH IS PAUSED platform-wide" banner (line 221) and the matching Attention line (line 333) are both suppressed, and the screen ends with "nothing needs att

* also caused by: A failed read of control/dispatch returns an error body with no `.fields`, which this collapses to the meaningful value `false`. Causes: expired session, 403, timeout — or the document genuinely not existing. The `2>/dev/null` discards curl's diagnosis as well.

* leads to: The header comment on line 96 says this flag is checked by the scheduler before anything else and was "the one cause this screen could not show". A read failure restores exactly that blind spot while looking healthy: the operator works down the runbook (paused pools, Cloud Scheduler tick, blocked_by

**`scripts/status.sh:129`** — `{"reachable":true,"pods":[]}`, rendered under `GKE (swarm-autopilot)` as "  no swarm workloads" — and, because `.gke.pods` is empty, the Attention section prints "nothing needs attention".

* also caused by: Simulated and confirmed: with kubectl exiting non-zero the pipeline yields an empty `pods`, `${pods:-[]}` makes it `[]`, and reachable is hardcoded `true`. The `/readyz` probe on line 128 passing does NOT imply `get pods -A` works: an operator with namespace-scoped RBAC gets Forbidden on a cluster-w

* leads to: An operator debugging "work is dispatched but nothing is running" sees a positive assertion that GKE is reachable and has no swarm pods, and concludes the scheduler never dispatched — when the pods may be running and simply unlistable. The same emptiness suppresses the pod-restart warning that the p


### medium (24)

**`scripts/configure-kubectl.sh:64`** — err "cluster swarm-autopilot not found in us-central1" then die "run 'make infra' to create the swarm cluster, or pass --cluster/--location". The follow-up `gcloud container clusters list` at line 68 

* also caused by: Expired session, missing container.clusters.get, the Kubernetes Engine API disabled, or a zonal/regional GKE_LOCATION mismatch. stderr is discarded; die_if_auth_failure is never called even though this script sources the library that defines it.

* leads to: The operator runs `make infra` to create a cluster that already exists. Compounding it, Makefile:109 invokes this script as `@$(SCRIPTS)/configure-kubectl.sh || echo "note: the GKE cluster is not reachable yet; the Cloud Run path does not need it"`, so during `make infra` the die never reaches make'

**`scripts/configure-kubectl.sh:134`** — "API server did not answer /readyz; a private control plane needs an authorised network or a bastion" — a single named cause, asserted. The script then continues and ends with "ok kubectl configured",

* also caused by: stderr is discarded, so the allowlist explanation is a guess. The same branch fires for an expired gcloud session (gke-gcloud-auth-plugin cannot mint a token), a missing/blocked RBAC binding for /readyz, a cluster still PROVISIONING or REPAIRING, and a corporate proxy. The 2>/dev/null is what remove

* leads to: The operator goes to the networking team about master authorized networks when the fix was `gcloud auth login` — the exact inversion of the auth-guard self-test's stated purpose. Because the script still exits 0 saying "kubectl configured", the next script in the chain is run as if the cluster had b

**`scripts/configure-kubectl.sh:137`** — API server did not answer /readyz; a private control plane needs an authorised network or a bastion

* also caused by: `2>&1` discards kubectl's error, which is the only thing that separates the cases: a missing or failing gke-gcloud-auth-plugin, an expired gcloud session, RBAC denying the non-resource URL /readyz (403), a stale kubeconfig entry pointing at a deleted cluster, a proxy — or a genuinely unreachable pri

* leads to: Sends the operator to master-authorized-networks or to standing up a bastion host, when the fix is `gcloud auth login`, installing the auth plugin, or one role binding. This is the final step of the script whose entire purpose is producing a working kubeconfig, so the wrong diagnosis here is what th

**`scripts/create-secrets.sh:246`** — no service account can read ${SECRET_NAME} yet  /  run: scripts/register-tenant.sh --tenant ${TENANT} --providers ${PROVIDER}

* also caused by: secretmanager.secrets.getIamPolicy denied — very plausible for a rotation operator who holds secretVersionAdder but not admin on the secret — plus expired session or wrong project. The trailing `|| true` is there to absorb grep's exit 1 on zero matches, which means a gcloud failure and a genuinely u

* leads to: The operator has just rotated a live provider key. They are told the tenant's worker cannot read it and sent to re-run register-tenant.sh, which re-walks IAM, the bucket policy and the Kubernetes namespace for a tenant that was already wired — extra writes on a shared project to answer a question th

**`scripts/failure-test.sh:125`** — PASS — oversized input rejected with HTTP 0

* also caused by: api_request sets API_STATUS=0 and returns 1 whenever curl itself fails, and this case pushes ~400 KB against HTTP_TIMEOUT (default 30s): a timeout, a reset connection, or a proxy dropping a large body lands in the same else branch as a genuine 413. A 401/403 lands there too, and here at least the pr

* leads to: Records that max_input_bytes (256 KiB) is enforced when the body may never have reached the API, and prints 'HTTP 0' as if it were a response code the API produced.

**`scripts/lib/deploy.sh:76`** — "no image for swarm-scheduler in the manifest; skipping swarm-scheduler", then the script continues and ends with "deployed ${TAG}".

* also caused by: Empty output here means the manifest has no entry — but push-images.sh:143-147 writes deployed-images-<env>.json BEFORE its `if [[ "${#FAILED[@]}" -gt 0 ]]; then die` on line 150, so any partially failed promotion (a trivy finding, a permission gap on one image, an expired session) leaves a manifest

* leads to: The operator reads "skipping" as a deliberate no-op and trusts the closing "deployed <tag>", while that Cloud Run service is still serving the previous revision — a half-deployed control plane where the API is new and the scheduler is old, with an exit code of 0 and no failure anywhere.

**`scripts/lib/deploy.sh:113`** — Nothing at all: the service is skipped without a word, and the run ends on ok "deployed ${TAG}" (deploy.sh:127) followed by dim "verify end to end with: make smoke".

* also caused by: Expired session (a deploy is long enough for one), run.services.get denied, Cloud Run API disabled, wrong region. The readiness wait just above (deploy.sh:99-101) uses the same discarded-stderr describe, so the same cause degrades that to warn "${service} was not ready within ${WAIT_SECONDS}s (condi

* leads to: The health section can silently check nothing and still print a clean deploy. The operator believes all four services answered /readyz and moves on — or, having seen the four "not ready within Ns" warnings that precede it, concludes the revisions are bad and rolls back a deployment that actually rol

**`scripts/lib/deploy.sh:115`** — Nothing at all -- a silent skip. The run still ends ok "deployed ${TAG}", with no health line printed for that service.

* also caused by: url comes from lines 113-114, gcloud ... 2>/dev/null || true. An expired session, missing run.services.get or the wrong region make it empty. If all four are empty, the Health step prints only its header.

* leads to: Absence of output reads as absence of problems. The post-deploy verification this step exists to provide never ran, and the operator moves on believing /readyz was checked on all four services.

**`scripts/lib/deploy.sh:117`** — <service> /readyz returned 000000

* also caused by: Verified locally: on a DNS or connection failure curl still writes its -w output (000) to stdout and exits non-zero, so `|| echo 000` appends a second one and code becomes the six-digit '000000' — not an HTTP status at all. The same warn covers a 403 from missing run.invoker, a 401 from a wrong audi

* leads to: At the end of a deploy this reads as 'the new revision is not serving', so the operator rolls back or redeploys, when the fault was on their own workstation (VPN, DNS, expired token). The script then prints 'ok deployed ${TAG}' and exits 0 regardless, so this warning is the only signal the pipeline 

**`scripts/lib/plan-guard.sh:84`** — With an empty verdict file, n is the empty string, `[[ "" -gt 0 ]]` is false for every check, and the script prints "no deny-listed resource is touched" / "every deletion is ours, and every creation c

* also caused by: Verified by running the real filter: `jq -f scripts/lib/destroy-guard.jq ... /tmp/empty-plan.json` on a zero-byte plan exits 0 and writes 0 bytes, so judge() succeeds and every subsequent `length` read comes back empty rather than 0. A truncated or zero-byte plan.json (a disk-full redirect, an inter

* leads to: This is the guard protecting another team's live cluster, VPC and service accounts, and its own header says "Exit 1 = it could not be judged, which is also a refusal: this fails closed." On this path it fails OPEN and states positively that the plan is clean. The reviewer merges or applies on the st

**`scripts/lib/resolve.sh:40`** — Nothing: kubectl_bin's three-line diagnosis ("no kubectl >= 1.30 found. Checked: ...", the 1.22/1.25 explanation, and the SWARM_KUBECTL hint) goes to /dev/null and `|| true` converts its die into exit

* also caused by: kubectl absent (a CI runner, a fresh checkout), or present but older than 1.30 — which is the default state of the reference workstation the header describes, where 1.22 and 1.25 win $PATH. The empty value is also what "the tool is fine" would look like if it were missing for any other reason.

* leads to: Nobody acts, which is the problem: validate-manifests.sh needs only python3 (it deliberately dropped kubectl — see its lines 71-79 about `--dry-run=client` not being offline), so the gate is keyed on a binary the check no longer uses. All of it — the unrendered-placeholder check, the YAML structural

**`scripts/pause-swarm.sh:96`** — info "scheduler job ${SCHEDULER_JOB} not found; nothing to pause" (102), PAUSE_SCHEDULER=0, and scheduler_paused:false written into the state file (109).

* also caused by: stderr discarded: an expired session, no cloudscheduler.jobs.get, or a job in a location other than REGION. The safety tick that was supposed to stop keeps ticking.

* leads to: The operator believes the scheduler job was already absent and stops looking. The false scheduler_paused:false is then durable -- resume-swarm reads it at line 54 and will never resume a job it thinks was never paused, so the error outlives the incident.

**`scripts/purge-data.sh:235`** — "  tasks: nothing matched" for each collection, then the closing "purged 0 document(s)".

* also caused by: select_docs feeds through `fs_list | jq -r '.[]?.name'` or `fs_query | jq -r '... .name'`, both of which emit nothing for an error body (verified), and the `|| true` on line 210 discards the exit status on top of that. So UNAUTHENTICATED, PERMISSION_DENIED, a timeout, and a FAILED_PRECONDITION for a

* leads to: This is the tenant-offboarding / data-retention tool. "purged 0 document(s)" with a zero exit is recorded as a completed deletion, so the tenant's tasks, leases and events stay in Firestore while someone signs off that they are gone — and nobody re-runs it, because it reported that there was nothing

**`scripts/race-test.sh:117`** — FAIL — tasks accepted concurrently: 0 < 19 (assert_ge at line 127)

* also caused by: `2>/dev/null || true` in each background subshell discards submit_task's own 'POST /v1/tasks returned HTTP ...' line and the response body — the only place the cause is recorded. Every submission failing for one shared reason (a token that expired between suites, run.invoker revoked, the API scaling

* leads to: Reads as the platform failing to accept tasks submitted in parallel, sending the reader into the all-or-nothing capacity reservation transaction (invariant 2) with zero evidence — the per-request errors were thrown away and cannot be recovered without another full run.

**`scripts/race-test.sh:159`** — t_info "checked 0 task(s) with attempts" (168) followed by a PASS from assert_eq "0" "${BAD_GENERATIONS}" "tasks with duplicate attempt generations" (169).

* also caused by: gens comes from fs_query attempts ... 2>/dev/null (157-158): a denied or throttled query returns an error object that yields no rows, identical to a task that genuinely has no attempts yet. Every task skips, the counter stays 0, and the assertion compares 0 to 0.

* leads to: Invariant 5 (every attempt carries a distinct fencing generation) is reported as verified by a run that read zero attempt documents. The PASS line is what a reviewer reads; the 'checked 0' t_info sits just above it in the same green block.

**`scripts/register-tenant.sh:419`** — No message on failure: the probe simply falls through to the add-iam-policy-binding branch, whose own failure surfaces as gcloud's "Specified policy version (1) must be at least 3" — the exact abort t

* also caused by: storage.buckets.getIamPolicy denied, expired session, or a policy gcloud cannot render at the requested version — any of which produce empty stdout, which greps as "binding absent". The identical pattern repeats for the metadata role at register-tenant.sh:439-441.

* leads to: The guard was written precisely because the unconditional add aborts the script before it reaches the tenant document ("the thing it is actually needed for"). With stderr discarded, a probe failure re-enters that branch, the script dies mid-registration on a policy-version error, and the operator de

**`scripts/register-tenant.sh:434`** — custom role ${BUCKET_METADATA_ROLE_ID} does not exist yet; run 'make infra'  /  without it the worker can reach its objects but cannot read the bucket's own metadata,  /  which Cloud Storage FUSE need

* also caused by: iam.roles.get denied, expired session, wrong PROJECT_ID, or the role existing in a DELETED/disabled state. Nothing in this run has proved the session is alive — register-tenant.sh's earlier probes (the group check at :254, the SA check at :263) discard stderr too.

* leads to: The operator runs `make infra` — a full terraform apply against a shared project — to fix what is actually a missing iam.roles.get permission or a dead token. The apply either no-ops (the role is already there) and leaves them re-running register-tenant.sh to the same warning, or fails with a fourth

**`scripts/register-tenant.sh:528`** — Falls to the else on line 553: "not connected to the swarm cluster (context: swarm-dev); skipping the GKE namespace", "run scripts/configure-kubectl.sh and re-run", and "until then this tenant has NO 

* also caused by: Same discarded-stderr probe as status.sh. The control plane here is behind an IP allowlist, so an operator whose IP moved gets a hang or a connection error; an expired gcloud session breaks gke-gcloud-auth-plugin; RBAC can forbid /readyz. All print "not connected", often while naming the correct con

* leads to: Registration has already created the GSA, the secrets and the GCS prefix by this point, so the operator is left with a half-registered tenant and told to fix their kubectl — which will not help if the cause is the allowlist. Re-running after "fixing" it repeats the GCP-side work. And with no --reque

**`scripts/resume-swarm.sh:84`** — scheduler job ${SCHEDULER_JOB} not found — printed as info, and the run still ends ok "resumed ${resumed} pool(s)" (resume-swarm.sh:116)

* also caused by: cloudscheduler.jobs.get denied, expired session, or — the quiet one — a Cloud Scheduler job that lives in an App Engine location that is not ${REGION}, since both describe and resume hardcode --location "${REGION}". The twin probe at resume-swarm.sh:99 behaves identically for Pub/Sub, so one dead se

* leads to: This path only runs when RESUME_SCHEDULER came back 1 from the pause-state file (resume-swarm.sh:54), i.e. the job really was paused by pause-swarm.sh. It stays paused. The operator reads "resumed N pool(s)", believes the incident is over, and goes to watch status.sh while the safety tick that was s

**`scripts/smoke-test.sh:39`** — ${service}: not deployed

* also caused by: run.services.get denied for the caller's own identity (require_platform at testlib.sh:90-98 proves Firestore and the API answer, not that this caller may read Cloud Run), Cloud Run Admin API disabled (that string is a non-auth fixture at scripts/lib/auth-guard.sh:85), REGION mismatch, or a session t

* leads to: `make smoke` is the gate after touching admission, dispatch or reconciliation. Three simultaneous "not deployed" failures read as a botched deploy, so the operator redeploys or rolls back a change that was fine — while the API those same services expose is answering /readyz in the very next test cas

**`scripts/smoke-test.sh:50`** — <service> /readyz 000000

* also caused by: Same `|| echo 000` concatenation as deploy.sh:117 (verified: the six-digit value is what actually prints). Beyond transport failure: a 403 from IAM or from an empty bearer token, and — specific to this loop — the token is minted once for the API's audience, so under SWARM_IMPERSONATE_SA the schedule

* leads to: Fails the 'Control-plane services are healthy' case with an impossible status code; the reader treats the scheduler and quota-broker as unhealthy and starts reading their logs, when nothing is wrong with them.

**`scripts/smoke-test.sh:69`** — GET /v1/tenants/me -> HTTP 0 (always 0 at this point in the script)

* also caused by: Same subshell trap: api_get runs inside `$( )`, and nothing earlier in this script calls api_request in the parent shell — require_platform's api_reachable uses raw curl — so API_STATUS is still the common.sh:492 initializer. 401, 403, 404 and 500 all print 'HTTP 0'.

* leads to: This is the first genuine diagnostic an operator sees after a deploy. 'HTTP 0' reads as 'no response, network down', so they check connectivity and the revision, rather than the hosted-domain check, the token audience, or the tenant registration — which a real 401 or 403 would have pointed at direct

**`scripts/smoke-test.sh:159`** — no objects under ${PREFIX} (a mock run may legitimately produce none) — followed by t_pass "artifact prefix is tenant-scoped"

* also caused by: storage.objects.list denied on the artifact bucket, expired session, wrong bucket name in ARTIFACT_BUCKET, or a network blip — all indistinguishable here from an empty prefix, because `gcloud storage ls` on a truly empty prefix and a permission denial both exit non-zero with nothing on stdout.

* leads to: The test case is titled "Artifacts landed in the tenant's own prefix" — the per-tenant isolation invariant (CONTRACT invariant 9). On any probe failure it prints a PASS for a property it did not observe at all, so a run where artifacts are landing in the wrong tenant's prefix, or not landing, is cer

**`scripts/status.sh:325`** — "not connected to the swarm cluster (context: swarm-dev)" plus "run scripts/configure-kubectl.sh" — i.e. a context/credentials diagnosis, printed even when the context is provably correct.

* also caused by: The else branch covers three different failures on lines 126-128: no kubectl >= 1.30 resolved, a non-swarm context, and `get --raw=/readyz` failing with its stderr discarded. The last one is the IP-allowlist case: an operator whose IP changed gets a dropped connection or a hang, not a denial. An exp

* leads to: The operator re-runs configure-kubectl.sh, which happily re-fetches credentials and writes a kubeconfig, then sees the same message — the actual fix is adding their IP to the master authorized networks, which nothing here mentions. Note also that none of the three /readyz probes in this repo pass --

---

## Fan-out result, 2026-09-19

Fourteen agents, one per file, dispatched to SwarmCloud against this branch and
verified locally before landing. **13 of 14 succeeded.** Every landed diff passed
`shellcheck -x`, `bash -n`, `--help`, the "typed confirmation still present"
check and `make test`. None was rejected.

The agents were better than expected in one specific way: the purge-data.sh run
DECLINED two of its six findings, arguing that the common.sh fix already made
those paths fail loudly and that the call sites were bare statements rather than
`if` conditions -- and verified it with a minimal repro. Both refusals were
correct.

### The one that failed, and why it is worth keeping

`register-tenant.sh` failed after three attempts, each reclaimed:

    reconciled: lease silent for 240s (grace 90s, expired=True)

The workers logged:

    FENCED: this attempt has been superseded; exiting without running the agent
    Container called exit(70)

**Invariant 5 behaved exactly as designed** -- the fenced worker exited without
running the agent and without touching the lease. The fault is upstream: the
reconciler reclaimed the lease before the worker had finished cold-starting,
pulling its image and cloning the repository. The replacement then hit the same
wall, three times, and the task never ran at all.

`heartbeat_grace_seconds` defaults to 90. A Cloud Run Job cold start plus an
image pull plus a shallow clone can exceed that under load -- this fan-out ran
five concurrent. Worth noting that the grace is measured from lease acquisition,
not from container start, so everything before the worker's first heartbeat
counts against it.

This is adjacent to report 10's finding 2 (staleness judged once at snapshot
time and never re-checked before fencing) but not the same bug: here the
judgement was correct at the time it was made. The question is whether 90
seconds is the right budget for a worker that has to pull an image before it can
say anything.

Not fixed. Recorded because a task that silently never runs, three times, is the
most expensive failure shape on this list.

