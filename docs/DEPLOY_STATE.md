# Deploy state — read this before the next `make deploy`

**As of 2026-09-19, the registry and the running platform disagree, on purpose.**

    images at tag 2d7e0dff6345   built, scanned, promoted to :dev  ✅
    terraform                    NOT applied                       ❌

So `build/deployed-images-dev.json` promises six images that the four Cloud Run
services are not yet running, and none of the environment fixes below are live.

## What is waiting to be applied

| | |
|---|---|
| `DISPATCH_TOPIC` on api + scheduler | the Pub/Sub fast-wake path has never fired |
| `TENANT_GROUPS` / `ADMIN_GROUPS` on api | group tenant resolution has never engaged |
| `PUSH_SERVICE_ACCOUNT` / `PUSH_AUDIENCE` | the second auth layer in front of the admission controller is inert |
| `BROKER_AUDIENCE` | same, for the quota broker |
| `ARTIFACT_REGISTRY_HOST` | works today only because two defaults coincide |
| pool ceilings | 20/10/5 live; 40/20/15 committed |
| GKE authorized networks | `a stale operator /32`, an address nobody holds |

## Why it stopped

`make deploy` died at `lookup oauth2.googleapis.com: no such host` — a DNS
failure, before terraform read state. Nothing was applied and no lock was left.
The retry is clean.

It was briefly believed to have succeeded, because it ran as
`make deploy > log 2>&1; echo "exit=$?"` and the *wrapper* exited 0. The real
status, `deploy exit=2`, was in the log the whole time. This is the same class
of bug as `docs/audits/2026-09-18/13-swallowed-stderr-sweep.md`, committed by
the person auditing it, on the same day, which is the most useful thing about
the incident.

## To finish

    gcloud auth login && gcloud auth application-default login
    make deploy          # read ITS exit status, not a wrapper's

Then confirm against the live services rather than against a script — several of
the scripts that would report on this are themselves in report 13, and will say
"not deployed" when they mean "could not look".

Delete this file once the two halves agree again.
