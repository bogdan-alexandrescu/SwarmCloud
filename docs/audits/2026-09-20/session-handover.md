# Where SwarmCloud stands, 2026-09-20

Written at the end of a long session so the next one does not have to
reconstruct it. Everything below was verified unless it says otherwise.

---

## What is live

All 26 buildable screens of the web UI, behind the external ALB and IAP at
`https://swarm.saga.xyz`. Seven nav tabs: Home (Control Room), Trouble,
Capacity, Agents, Workflows, Activity, Tenants.

Five Cloud Run services, each verified serving the **digest** the manifest
records — not merely "healthy", which is a weaker claim and the one that
fooled us earlier today.

## What is verified, and how

| Claim | Evidence |
|---|---|
| IAP protects every path | `curl` with no credential → 302 to accounts.google.com on `/v1/capacity`, `/healthz`, `/openapi.json` and an unknown path |
| Authenticated API works | 19 endpoints swept through a real IAP browser session; `/v1/capacity` 200 with real pools, `tenant_id: u-bogdan` |
| Composite indexes are built | `/v1/tasks?state=FAILED|PARKED|READY` all 200. The spec called a missing index (500 FAILED_PRECONDITION) the single most likely first-run failure. It is not present |
| 404s carry a proper envelope | `/v1/tasks/task_does_not_exist` → `{"code":"not_found","message":"task '…' not found"}` |
| Admin gating works | `/v1/admin/{dispatch,tenants,pools}` → 403 `forbidden` for a non-admin |
| Routes that do NOT exist | `/v1/admin/leases` and `/v1/admin/quota` → 404. This confirms the spec, and is why the "silent workers" and "admitted but never dispatched" panels were NOT built |
| All 7 screens render with real data | walked at 1440×900 |
| No horizontal overflow at 390px | measured `scrollWidth` on all 7 — was 521, now 390 |

## What is NOT verified, and say so

* **The group-to-tenant mapping.** `directory_group = false` on `eng`, so
  callers resolve to their personal `u-<user>` tenant. The mapping itself is
  untested. This is one of the seven original asks.
* **Tokens and cost.** The worker fix landed but has not run on a real
  attempt, and tasks that already ran will never carry the numbers.
* **The contract invariants.** `make smoke`, `concurrency-test` and
  `race-test` cannot reach the API — see the finding below.
* **Stale, session-expiry, 429 and dark/light contrast.** Built and reasoned
  about; not exercised against the live deployment.

## Three things that need you

1. **IAP OAuth client id.** `gcloud iap oauth-clients list
   projects/209001918367.../brands/…` is PERMISSION_DENIED for a project
   editor. Needed as `API_AUDIENCE` so scripts can authenticate through IAP.
2. **A root terraform output for the front-door URL.** The frontend module
   already exposes `url`; `terraform/infra/outputs.tf` does not surface it,
   which is why `tf_output api_url` is empty. Track C's file.
3. **Workspace Group Reader for `swarm-api`.** Cloud Identity does not use
   GCP IAM; the service account is not a Workspace principal, so it cannot
   read `eng@saga.xyz`. Blocked today because Google refuses the Admin SDK
   OAuth scope to the gcloud client in this domain.

## Findings filed today

* `tag-vs-digest.md` — Terraform deploys by tag, so a rebuilt tag leaves a
  stale revision reporting healthy. Track C.
* `verification-targets-cannot-run.md` — the gate CLAUDE.md requires before
  claiming a change is done cannot reach the API.

## Corrections I made to my own work

Recorded because the wrong versions were convincing:

* I concluded the 503 was a missing IAM grant and had an org-level
  `roles/cloudidentity.groupsReader` binding created. **It does nothing** —
  the role is stage ALPHA with no `includedPermissions`, and zero
  cloudidentity permissions are testable at the org. That binding can be
  revoked.
* I then concluded it was the group's `discussion_forum` label. That label is
  real, but it gates a *Workspace* admin-role condition, not GCP IAM. A true
  observation turned into a false conclusion.
* I reported the verification-gate fix as needing new code. It does not —
  `id_token()` already has the IAP branch.

## Requests against the frozen contract

`CONTRACT.md:55-76` records `groups:lookup … WORKS` and "needs NO org-level
IAM grant" from a live test on 2026-09-15. That test used **human**
credentials. It does not cover the identity the API runs as, which is exactly
the failure that cost this session. Raised as a request, not a change.
