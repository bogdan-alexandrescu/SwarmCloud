# One service calling another, in this deployment

Read this before writing a brief, a module, or a client that makes one
component of this platform talk to another. It exists because that turned out
to need **four** things simultaneously, three of which fail with error messages
that point somewhere else, and I discovered them one deploy at a time while
wiring swarm-api to the quota broker on 2026-09-21.

Nothing here is exotic. All four were knowable before any code was written.

## The four preconditions

For caller **C** to call service **S**, all of these must hold. Any one missing
and the call fails — differently each time.

| # | Requirement | Where it lives |
|---|---|---|
| 1 | C knows S's URL | an env var; see "the cycle" below |
| 2 | C's egress routes through the VPC | `vpc_egress = "ALL_TRAFFIC"` |
| 3 | C holds `roles/run.invoker` on S | `terraform/infra/main.tf` |
| 4 | C's token targets S's **custom audience** | `local.push_audiences[...]` |

Plus a fifth that is specific to the broker: it derives a tenant from the
caller's **service-account name**, so a caller that is not a worker must be on
`PLATFORM_SERVICE_ACCOUNTS` or every request is 403.

## The failure signatures, and what they are NOT

This is the part worth memorising, because each one reads like a different bug.

**Missing #2 (egress) → `HTTP 404` with an HTML body.**
Cloud Run's `internal-and-cloud-load-balancing` ingress refuses external
callers with a 404, not a 403. Under `PRIVATE_RANGES_ONLY` a request to S's
public `*.run.app` hostname is not a private range, so it never enters the VPC
and arrives as external traffic.

It is **not** a missing route, and it is **not** a version skew between the
two services. I read it as both, twice, having already written a function in
this repository whose entire job is to stop someone doing that. If you see an
HTML 404 from a Google endpoint, check ingress and egress before you check
anything about your own code.

**Missing #3 (invoker) → `HTTP 403`, and Cloud Run consumes the
`Authorization` header.** Measured 2026-09-20: when Cloud Run enforces IAM the
container receives no caller identity at all — see
`docs/deploying-without-an-organisation.md`.

**Missing #4 (audience) → `401 Invalid JWT audience`.** Sends people to the IAM
console, where the problem is not. S declares a custom audience precisely so
callers can agree on a constant rather than a URL known only after apply; a
token minted for the URL alone is rejected by S's own `aud` check.

**Missing #5 (platform list) → `403 caller is not a swarm worker service
account`.** Self-explanatory only once you know the broker identifies callers
by SA-name pattern.

## The cycle, and why a URL is sometimes declared rather than derived

`module.cloud_run` takes the services' environment as an **input** and produces
their URLs as an **output**. So a service inside that module cannot reference
another's URL: terraform refuses to plan the cycle.

* **Worker jobs** live outside the module, so they derive the broker's URL from
  `module.cloud_run.service_urls`.
* **swarm-api and the scheduler** live inside it, so they take
  `var.quota_broker_url` — applied once, read from `terraform output`, written
  into tfvars.

An unset value must be **loud**. swarm-api refuses to start its broker client
and answers 503 naming the variable; the scheduler has a `check` block. The
failure being prevented — a pool configured everywhere except the one place
that matters — is otherwise completely silent.

## Egress is per service, not per platform

`ALL_TRAFFIC` routes a service's whole egress through the VPC and Cloud NAT.
Only swarm-api needs it among the control-plane services, because it is the
only one that calls another. It is therefore an override on that one service
rather than a module-wide setting: widening all four would be a change nobody
asked for to three of them.

## The checklist to put in a brief

When specifying work that introduces a new service-to-service call, the brief
must state, for each direction:

- [ ] which env var carries the URL, and whether it is derived or declared
- [ ] the caller's `vpc_egress`, and that `PRIVATE_RANGES_ONLY` cannot reach an
      internal-ingress service by its public hostname
- [ ] the `run.invoker` binding
- [ ] the audience the token must carry
- [ ] how the callee identifies the caller, and what list the caller belongs on
- [ ] what an unset value does — and it must refuse, not degrade silently

An API contract alone is not a specification for a distributed system. It
describes what the two ends say to each other and nothing about whether they
can reach each other at all.
