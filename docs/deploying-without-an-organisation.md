# Deploying without a Google Workspace organisation

This platform was built inside one organisation, and several of its defaults
quietly assume that. This document records which assumptions those are, what
each one costs an adopter who does not share it, and the constraint behind the
shape of the answer — because the shape is not a preference, it is forced by
how Cloud Run works.

## The measurement everything else follows from

Taken on 2026-09-20 against a throwaway Cloud Run service with `ingress=all`,
IAM enforced, and **no IAP anywhere in the path**:

```
unauthenticated          -> 403                      (IAM does gate)
authenticated via proxy  -> authorization_present: false
                            goog_headers: []
```

The container receives **nothing** about the caller. Not a re-minted token, not
an `x-goog-authenticated-user-email` — nothing. Cloud Run's IAM is an allow/deny
gate and does not forward identity.

So a deployment can have **an edge gate** or **per-user tenant resolution**,
never both. That is not a quirk of one project and it is not something a future
release will soften; it is what the product does. Every choice below is
downstream of it.

An earlier note, from 2026-09-16, recorded the same conclusion from a
measurement taken with IAP in front. IAP rewrites the `Authorization` header
itself, so that observation could have been attributing IAP's behaviour to
Cloud Run. It was not — but it is worth knowing the two were measured
separately, because the distinction is exactly the kind that gets lost.

## Two shapes, and why there is no third

| | **solo** | **team** |
|---|---|---|
| Gate | Cloud Run IAM, named invokers | external load balancer with IAP |
| App learns about the caller | nothing | the IAP assertion |
| Tenant | one, fixed at deploy | resolved per user |
| Requires | a GCP project | an organisation, a domain, a certificate |

**Solo is not a degraded team.** One person — or a small group sharing one
workspace, one set of provider keys and one GCS prefix — *is* one tenant. The
multi-tenancy in `CONTRACT.md` invariant 9 exists to keep one team's secrets
away from another's; with one team there is nothing to separate, and paying for
the separation buys nothing.

There is no third shape where Cloud Run IAM gates *and* the app resolves a
tenant, because of the measurement above. A design that appears to do both is
misreading its own configuration — most likely it has granted `allUsers` and is
relying on something else entirely for its boundary.

## Why IAP cannot be a requirement

The IAP brand in this project is `orgInternalOnly: true`. An internal brand
**requires a Google Cloud organisation**, which means Workspace or Cloud
Identity. A person with a personal GCP project cannot create one. Add an
external load balancer, a managed certificate and a DNS record and the front
door costs an adopter more setup than the thing they came for.

IAP is the right front door for an organisation. It cannot be the only way in.

A second, quieter consequence: with a **Google-managed** OAuth client — which
is what an internal brand gets, and what `terraform/modules/frontend/main.tf`
deliberately chooses so that no client secret lands in state — the client id is
not exposed, and `gcloud iap oauth-clients list` is `PERMISSION_DENIED` for a
project editor. Programmatic access through IAP therefore needs a value that
someone with brand-owner rights has to read out of a console once. That is
tolerable for a team. It is an adoption wall for everyone else.

## Groups are optional, and the code already knew

Tenant resolution checks membership **per registered group**, then falls back to
the caller's own `u-<local-part>` tenant. With `tenant_groups` empty there is
nothing to check and every caller gets their personal tenant. That is correct
and needs no configuration.

What was **not** optional was the failure mode. A group lookup that fails is
fatal by design, because an unknown higher-priority group could file a caller's
work under the wrong tenant — a real risk when groups are configured. The
deliberate refusal is right. What made it dangerous was that the lookup could
fail for a reason nobody could see:

* Cloud Identity's Groups API does not authorize through GCP IAM, so a
  `*.gserviceaccount.com` identity cannot read a group regardless of its roles.
  `roles/cloudidentity.groupsReader` is ALPHA with no included permissions and
  granting it at the organisation changes nothing;
* domain-wide delegation fixes that, and the obvious call for it —
  `credentials.with_subject(user)` — exists **only** on credentials from a
  service-account key file. On Cloud Run there is no key file, so that branch
  never fired, every lookup used the service account's own identity, and Cloud
  Identity answered `Error(2028)` — which reads exactly like the missing-role
  problem delegation was introduced to solve.

`swarm_api/delegation.py` does it keylessly: build the assertion, have Google
sign it with `iamcredentials …:signJwt`, exchange it for a token that acts as
the subject. The two ways that can fail have **opposite remedies in different
consoles**, so they are worded differently on purpose:

| Failure | Meaning | Fix |
|---|---|---|
| `signJwt` 403 | the service account may not sign as itself | `roles/iam.serviceAccountTokenCreator`, GCP IAM |
| `unauthorized_client` | the domain has not authorised this client | the OAuth client id, Google Admin console |

Collapsing those into "permission denied" is what sends an operator to the
wrong console. It cost a live outage on 2026-09-20.

**For a solo deployment none of this applies.** Leave `tenant_groups` empty and
no Cloud Identity call is ever made.

## Authorising a person who has no domain

Authorisation is by hosted domain, which is exactly right for an organisation.
It collapses for anyone else: a single developer has no domain of their own, so
the only `allowed_domains` value that admits them is `["gmail.com"]` — which
admits every Google account on earth to run agents on their billing account.

`ALLOWED_USERS` is an additive list of exact addresses. Users only is the solo
deployment; domains only is the organisation; both is a company plus a named
outsider. It is not a second authentication path — the address still comes from
the verified token, so it decides *which* verified identities are admitted,
never whether the identity was verified.

With neither set, nobody is admitted. "Not configured" means refuse, the same
property the IAP audience check has and for the same reason: a check that is
off must not read as a check that passed.

## Reaching the API from a laptop

`apps/swarm-mcp` detects which of these it has, rather than being told:

| Tier | Mechanism | Reaches |
|---|---|---|
| explicit token | `SWARM_ID_TOKEN` | both |
| metadata server | running inside GCP | both |
| service account | `SWARM_IMPERSONATE_SA` | both |
| IAP | `SWARM_IAP_CLIENT_ID` + a service account | team |
| user credentials | `gcloud run services proxy` | solo |

The last row is the one that makes a laptop different from CI, and the reason is
narrow: **`gcloud auth print-identity-token` refuses `--audiences` for user
credentials.** Only a service account may set one, and Cloud Run rejects a token
whose audience is not its own URL. `gcloud run services proxy` exists for
exactly that gap; reimplementing it would mean reimplementing the user OAuth
flow.

`swarm doctor` prints the tier, what it reaches, and everything it checked and
rejected. The commonest failure for a newcomer is not choosing the wrong tier —
it is not knowing tiers exist, and reading `Invalid JWT audience` as a bug in
the platform.

## What is still assumed

Honest list of what an adopter outside this organisation would still hit:

* there is no `solo` tfvars profile yet; the terraform root takes an
  organisation-shaped configuration and a `frontend_hostname`;
* `terraform/environments/*/`.tfvars carry `allowed_domains` and expect a
  verified domain;
* the deploy path assumes an Artifact Registry repository and a Cloud Build
  quota that a brand-new project has to have enabled.

None of these is hard. All of them are currently undocumented defaults rather
than decisions, which is the reason this file exists.
