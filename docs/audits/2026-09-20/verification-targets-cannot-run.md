# The three targets CLAUDE.md requires before "done" cannot run

**Found** 2026-09-20, by running them.
**Owner** Track D (`scripts/`) — mine. Reported first because the fix is a
design decision about how a script authenticates, not a typo.
**Severity** High. The gate that is supposed to catch regressions is itself
broken, and it fails in a way that reads as an API problem.

---

## What happens

CLAUDE.md says, of any change to admission, dispatch or reconciliation:

> and, if you touched anything in the admission, dispatch or reconciliation
> paths, against a deployed environment:
> `make smoke concurrency-test race-test`

`make smoke` exits 2 with:

```
fail the API at https://swarm-api-tonstldhta-uc.a.run.app/readyz answered
HTTP 404, not 2xx. Rule out auth/IAM before running 'make deploy' -- this was
not a transport failure and not a 401/403.
```

The message is good — it refuses to blame auth, and it is explicit that this
is not a 401/403. But it names the wrong suspect, because the real cause is
not on the list it offers.

## Why

`swarm-api` now runs with ingress `internal-and-cloud-load-balancing`, which
is correct and deliberate: it is what puts the service behind the external
ALB and IAP. A direct request to the `*.run.app` URL is refused by design.

`api_url()` (`scripts/lib/common.sh:475`) resolves in three steps:

1. `$API_URL` if set — not set
2. `tf_output api_url` — **empty**; `terraform output` in `terraform/infra`
   returns nothing at all today
3. `gcloud run services describe … --format='value(status.url)'` — returns
   the `*.run.app` URL, which ingress blocks

So every script that talks to the API goes to an address that cannot answer.

## What it is not

Not auth, not IAM, not a missing deploy, not an expired session. Verified:

```
gcloud run services describe swarm-api --format='value(...ingress)'
  -> internal-and-cloud-load-balancing

curl https://swarm.saga.xyz/v1/capacity            (no credential)
  -> 302 to accounts.google.com          (IAP is working)

browser with an IAP session -> /v1/capacity
  -> 200, real pool data                 (the API is healthy)
```

The API is fine. The front door is fine. The scripts are pointed at a door
that was deliberately locked.

## Why the front door is not a one-line substitute

Setting `API_URL=https://swarm.saga.xyz` is honoured by `api_url()`, but IAP
then refuses the request:

```
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     https://swarm.saga.xyz/readyz
  -> 401
```

IAP requires an OIDC ID token whose **audience is the IAP OAuth client id**,
presented by a principal holding `roles/iap.httpsResourceAccessor`. A plain
user identity token has the wrong audience.

Note also that `frontend_iap_audiences` in the environment tfvars is a
**backend service path** (`/projects/…/global/backendServices/…`), which is
what `swarm-api` verifies an assertion against. It is NOT the OAuth client id
a caller needs to mint a token with. The two are different values and only
one of them is currently written down.

## Correction: this is smaller than it first looked

Two things I got wrong on the first pass, both of which shrink the fix.

**The auth plumbing already exists.** `id_token()`
(`scripts/lib/common.sh:441`) already has the IAP-shaped branch:

```bash
elif [[ -n "${SWARM_IMPERSONATE_SA:-}" ]]; then
  _ID_TOKEN="$(gcloud auth print-identity-token \
    --impersonate-service-account="${SWARM_IMPERSONATE_SA}" \
    --audiences="${API_AUDIENCE:-$(api_url)}" --include-email ...)"
```

So a service account plus `API_AUDIENCE` set to the IAP OAuth client id is
already supported. No change to the token path is needed. Only the plain-user
branch lacks an audience, and that branch cannot work with IAP by
construction.

**`api_url()` step 2 can never succeed.** It calls `tf_output api_url`, and
**no output named `api_url` is declared** — `terraform/infra/outputs.tf`
declares twenty outputs and that is not one of them. The frontend module does
expose `url` (`terraform/modules/frontend/outputs.tf:6`) and `iap_audiences`
(`:29`), but the root never surfaces either. So the lookup always returns
empty and always falls through to the blocked `*.run.app` address.

That makes the minimum fix: surface the frontend `url` as a root output so
`tf_output api_url` resolves, and record the IAP OAuth client id so
`API_AUDIENCE` has a value. The first is Track C's file. The second needs
someone who can read the brand — `gcloud iap oauth-clients list
projects/209012342332/brands/209012342332` is PERMISSION_DENIED for an
ordinary project editor.

## Options, none of which is free

1. **Give the scripts an IAP-capable identity.** A service account with
   `roles/iap.httpsResourceAccessor`, and `id_token()` extended to mint with
   `--audiences=<iap oauth client id>`. Needs the client id recorded
   somewhere — it is not in the repo today. Correct, and makes the targets
   work from CI as well as a laptop.
2. **Run them from inside the VPC**, where the internal path is reachable.
   Moves the problem to "who has a jump host".
3. **Keep a direct-ingress path for verification only.** Rejected on sight:
   the whole point of the ingress setting is that there is no unauthenticated
   path to the API.

## What must not happen

The targets must not be allowed to keep failing quietly. CLAUDE.md tells the
next person to run them before claiming a change is done; if they always fail
for an unrelated reason, they will be skipped, and then they are not a gate
at all — they are a ritual. Either they work, or CLAUDE.md should say plainly
that they do not and why.
