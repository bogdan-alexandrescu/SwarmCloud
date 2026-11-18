# task_74430a9c760849ccaa3d  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit scripts/ for places where a failure is reported as a DIFFERENT problem than it actually is - an error message that sends someone at the wrong cause. Several were found today (a dead gcloud session reported as a missing Artifact Registry repository; a missing Container Analysis permission reported as an unbuilt image). Find more. For each, quote the message, say what really causes it, and say what a person following that message would do wrong. Report at most 5, most misleading first. If you find none, say so plainly.

## Report

All confirmed. Here's the compiled audit, ranked most misleading first.

## 1. `scripts/purge-data.sh:133` — could lead to unrecovered data loss

> `"artifact bucket gs://${ARTIFACT_BUCKET} does not exist, so no backup can be taken; pass --no-backup to accept that"`

**Real cause:** the guard is `gcloud storage buckets describe ... >/dev/null 2>&1` (line 126) — stderr is thrown away. An expired gcloud session, wrong `PROJECT_ID`, or a transient permission/network error produces the exact same falsy result as a genuinely absent bucket.

**Wrong action:** this fires mid-way through an *irreversible Firestore purge*, and the message's own suggested remedy is to re-run with `--no-backup` — i.e., skip the safety backup and delete anyway. An operator who trusts the message could destroy data with zero backup because a token expired, not because the bucket is missing.

## 2. `scripts/lib/testlib.sh:96` — masks a live, healthy API as undeployed

> `"the API at ${url} did not answer /readyz. Run 'make deploy', or set API_URL."`

**Real cause:** `api_reachable` curls `/readyz` using a bearer token from `id_token()` and checks for a 2xx. Cloud Run's IAM layer returns 403 for an expired/invalid token before the request ever reaches the service — `common.sh` itself documents that this reads as "unreachable even when the API is perfectly healthy." Every `*-test.sh` script calls `require_platform`, so this is the single most-triggered false diagnosis in the suite.

**Wrong action:** operator re-runs `make deploy` against a fully healthy, already-deployed service instead of running `gcloud auth login`.

## 3. `fs_database_exists()` (`scripts/lib/common.sh:664`) — "database missing" on any failure, repeated in 6 call sites

> e.g. `pause-swarm.sh:47`: `"Firestore database '${FIRESTORE_DATABASE}' does not exist; nothing to pause"`
> also `resume-swarm.sh:37`, `testlib.sh:93`, `purge-data.sh:75`, `register-tenant.sh:561`, `status.sh:213`

**Real cause:** the shared helper runs `gcloud firestore databases describe ... >/dev/null 2>&1`, discarding stderr — same auth/permission/network ambiguity as above, just for Firestore instead of the API.

**Wrong action:** varies by caller, and some are worse than others — `pause-swarm.sh` tells the operator there's "nothing to pause" (they walk away leaving a running swarm consuming quota), while `register-tenant.sh`/`status.sh` send them to re-run `make infra` against infrastructure that's already provisioned.

## 4. `scripts/configure-kubectl.sh:66` — near-exact repeat of the Artifact Registry bug, for GKE clusters

> `"cluster ${GKE_CLUSTER} not found in ${GKE_LOCATION}"` → `die "run 'make infra' to create the swarm cluster, or pass --cluster/--location"`

**Real cause:** `gcloud container clusters describe ... 2>/dev/null` (stderr discarded) — a dead gcloud session reads identically to a cluster that was never created. This is the same failure shape as today's calibration example, just for a different `gcloud describe` call that was never wrapped with the auth-failure check used elsewhere in the same file set.

**Wrong action:** operator runs `make infra`, attempting to create a cluster that already exists (at best wasted time/cost, at worst a Terraform conflict), instead of re-authenticating.

## 5. `scripts/lib/deploy.sh:82` — silently skips a real deploy, blames "infra"

> `warn "Cloud Run service ${service} does not exist yet; run 'make infra' first"` (then `continue`)

**Real cause:** same `gcloud run services describe ... >/dev/null 2>&1` pattern with discarded stderr.

**Wrong action:** because this is a non-fatal `warn`+`continue` rather than a hard failure, a deploy run can silently skip updating a live service — under an auth blip, part of the fleet just doesn't get the new image, and the log tells the operator the service was never provisioned rather than that the deploy step failed to authenticate.

---
All five trace back to the same root pattern: a `gcloud ... describe` (or `curl`) call has its stderr redirected to `/dev/null` or discarded, so the script cannot distinguish "resource absent" from "auth expired / wrong project / permission denied / network blip," and the hardcoded message picks the absent-resource explanation every time. The repo already has the correct fix (`gcloud_auth_failure`/`die_if_auth_failure` in `lib/common.sh`) applied in a few places (`account.sh:235`, `create-secrets.sh:211`, `build-images.sh`, `push-images.sh`) — it just wasn't applied consistently to the call sites above. Lower-confidence relatives of the same bug also exist in `quota-test.sh:63`, `concurrency-test.sh:45`, `register-tenant.sh` (custom-role and bucket checks), and `account.sh:133`, all with the identical "swallowed stderr → wrong absence claim" shape, if you want them addressed in the same pass.
