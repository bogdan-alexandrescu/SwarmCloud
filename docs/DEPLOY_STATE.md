# Deploy state

**2026-09-19: applied and verified, with two exceptions recorded below.**

Tag `2d7e0dff6345` is live on all four control-plane services. 18 of 19 planned
changes applied.

## Verified against the live platform

Checked with `gcloud ... --format=json`, deliberately NOT through `status.sh` or
`smoke-test.sh` — both are in `docs/audits/2026-09-18/13-swallowed-stderr-sweep.md`
and report "not deployed" when they mean "could not look".

| | |
|---|---|
| `DISPATCH_TOPIC=swarm-scheduler-wake` | api + scheduler; `WAKE_TOPIC` gone |
| `TENANT_GROUPS=eng@saga.xyz,swarm-smoke@saga.xyz` | group resolution live for the first time |
| `PUSH_SERVICE_ACCOUNT`, `PUSH_AUDIENCE` | scheduler |
| `BROKER_AUDIENCE` | quota broker |
| `ARTIFACT_REGISTRY_HOST` | scheduler; `ARTIFACT_REGISTRY`/`IMAGE_BASE` gone |
| custom audiences | set on scheduler and broker |
| GKE `0.0.0.0/0` | `kubectl get nodes` returns a Ready node |

**The quota sweep now returns 200.** It had returned 403 on every tick for the
life of the deployment. Three consecutive 200s after the apply.

**Dispatch works end to end.** A mock task reached `DISPATCHED`.

## Exception 1: the alert policy did not create

    Error 404: Cannot find metric(s) that match type =
    "cloudscheduler.googleapis.com/job/attempt_count"

`module.monitoring.google_monitoring_alert_policy.safety_tick_absent` is the one
resource in the plan that failed, and it is why `make deploy` exited 2. The
metric does not exist until Cloud Scheduler has emitted it, and Google's own
message says it can take ten minutes to become queryable. The ticks are running
now, so a later `make deploy` should create it with no other change.

Nothing else depends on it. It is an alert, not a control.

## Exception 2: pool ceilings are NOT what dev.tfvars says

The live pools are still `global=20`, `provider:anthropic=10`,
`provider_tenant=5`, while `dev.tfvars` says 40/30/15.

This is not drift and not a failed apply. `terraform/modules/firestore/bootstrap.tf`
puts `ignore_changes = [fields]` on the pool documents on purpose: `active` is
mutated by the admission transaction on every lease, so an apply that rewrote
them would zero live concurrency counters and oversubscribe every pool.
Terraform creates pool documents once and then stops.

So the tfvars numbers are what a NEW environment is born with. Changing a
running one goes through `PUT /v1/admin/limits/...`, the only path that writes
`hard_limit` without touching `active`. Nothing checks that the two agree.

This is now written next to the values in `dev.tfvars` as well.

---

# Front door — applied 2026-09-19, waiting on one DNS record

An external ALB with IAP now stands in front of swarm-api.

    swarm-ui-http    8.232.87.184:80    -> redirect to https
    swarm-ui-https   8.232.87.184:443   -> IAP -> swarm-api
    IAP access       domain:saga.xyz     roles/iap.httpsResourceAccessor
    certificate      swarm.saga.xyz      PROVISIONING

## The one manual step

    A   swarm.saga.xyz   ->   8.232.87.184

saga.xyz is at an external registrar, so terraform reserves the address and
outputs it but cannot create the record. **Until that record resolves, the
managed certificate stays in PROVISIONING and the host serves a TLS error.**
That is expected and takes up to an hour. It is indistinguishable from a broken
deployment unless you know, which is why it is written here.

Check with:

    gcloud compute ssl-certificates describe swarm-ui-cert --global \
      --project saga-agents-staging --format='value(managed.status)'

## Two IAM roles were granted to get here

`roles/compute.loadBalancerAdmin` and `roles/iap.admin`, both to
bogdan@saga.xyz. Neither was held before. The first apply failed on
`compute.regionNetworkEndpointGroups.create` and the second on
`iap.webServices.getIamPolicy`; IAP IAM took about 45 seconds to propagate after
the grant, during which the retry failed identically, which reads as the grant
not having worked.

## The IAP brand is shared, and is not ours

`projects/209012342332/brands/209012342332`, titled **"AI Agents"**, support
contact emanuel@saga.xyz, `orgInternalOnly: true`. Only one brand is permitted
per project and it already existed, so SwarmCloud's consent screen says "AI
Agents". Terraform neither creates nor modifies it -- `google_iap_brand` cannot
be deleted, so owning it would make this module impossible to destroy cleanly.

## Still outstanding

The monitoring alert policy `safety_tick_absent` still fails to create: the
Cloud Scheduler metric it watches does not exist until Cloud Scheduler has
emitted it. It is the sole reason `make deploy` exits 2. Nothing depends on it.

