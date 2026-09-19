## Front door, auth, and the API surface the UI needs

This section owns everything between a browser on someone's phone and a Firestore
document: the load balancer that does not exist, the identity that arrives with the
request, the shape of every response the UI parses, and the rule that stops a failed
query rendering as a calm zero. It specifies five things — a sign-in gate (F1), an
identity bar (F2), a fetch contract (F3), a system-status panel (F4) and a
session-expiry overlay (F5) — plus the complete route map every other section builds
on (§7) and the prerequisites everything here is blocked behind (§6).

**The blunt version.** Today there is no network path from a browser to swarm-api. The
repository states in three places (`scripts/swarm.py:5-12`, `scripts/pool-limit.sh:30-34`,
`docs/BUILD_PROMPT_V2.md:380-387`) that the ingress setting is what blocks this. It is
not. `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` is Cloud Run's *"Internal and Cloud Load
Balancing"* — precisely the setting an external Application Load Balancer in front of
Cloud Run requires, and the validation at `terraform/modules/cloud_run/variables.tf:39-42`
forbids only `INGRESS_TRAFFIC_ALL`. Nothing needs relaxing. A load balancer needs
building.

Two corrections to that same repo sentence, because it is wrong twice. It says
**internal** load balancer, and an internal ALB gives a private VIP reachable only from
inside `swarm-vpc` — a laptop still cannot reach it without VPN, Interconnect or IAP TCP
forwarding, and `terraform/modules/network/main.tf:37-66` has no
`purpose = REGIONAL_MANAGED_PROXY` subnet for one anyway. The thing that makes this
product reachable is an **external** global ALB. And the LB is not the last blocker:
after it is built, every admin screen in this product still returns 403 to every human
alive, because `ADMIN_GROUPS` is deployed with an empty value and `auth.py:226` computes
`is_admin = any(g.lower() in set() for g in ...)` for everyone.

One honesty marker carried from the findings and not softened: *no ALB was built, so
"an external ALB is admitted by this service in this project" is Cloud Run's documented
behaviour for that ingress value, not something measured here.*

---

### 1. The front door, concretely

Decided already (not re-litigated here): external Application Load Balancer + IAP in
front of the existing swarm-api. No BFF holding a cross-tenant identity. Tenant
isolation stays in `swarm_api.auth` → `swarm_common.identity.resolve_tenant`, where
CONTRACT.md invariant 9 already lives.

**Topology, resource by resource.** One hostname. Two backends. This is the whole
design and the single-hostname part is load-bearing:

```
   browser (iPhone / desktop)
        |  https://swarm.<domain>
        v
   global external ALB        google_compute_global_forwarding_rule
        |                     google_compute_target_https_proxy
        |                     google_compute_managed_ssl_certificate
        |                     google_compute_global_address
        v
   URL map                    google_compute_url_map
        |
        |-- /v1/*, /healthz, /readyz, /metrics, /docs, /openapi.json
        |        -> backend service "swarm-api"   (iap {} enabled)
        |              -> serverless NEG -> Cloud Run service swarm-api
        |
        `-- everything else (/, /assets/*, index.html)
                 -> backend service "swarm-ui"    (iap {} enabled)
                       -> serverless NEG -> Cloud Run service swarm-ui
```

Four things about that picture are decisions, not decoration:

1. **Same origin for the SPA and the API.** The UI is served from the same hostname as
   `/v1/*`, so there is no cross-origin request, so **CORS is not needed in production at
   all**. There is no `CORSMiddleware` anywhere in this repository (`main.py:75-97` adds
   exactly one custom middleware, a latency/counter observer, and nothing else), and
   adding one forces an allowed-origins decision nothing currently records. Same-origin
   deletes the whole problem. It also means the IAP session cookie covers both, and the
   SPA never holds a bearer token in JavaScript — which matters, because an ID token here
   is not a session cookie, it is full impersonation of that caller's tenant
   (`scripts/lib/common.sh:452-459` says so in as many words).
   **The one place this does not hold is the developer's own machine**, where a Vite dev
   server on `localhost:5173` calling a deployed API *is* cross-origin. That is solved by
   a dev-server proxy for `/v1`, `/healthz` and `/readyz` — not by adding
   `CORSMiddleware`. Write the proxy into the SPA's dev config on day one, because the
   moment someone hits a preflight failure with no proxy in front of them, the fix they
   reach for is the middleware, and the allowed-origins decision this design deleted comes
   straight back.
2. **The SPA is served by a Cloud Run service, not a GCS backend bucket.** IAP attaches
   to backend *services*. A backend bucket behind the same URL map would be
   **unauthenticated** — the login page and every JS bundle served to the internet while
   the API sits behind IAP. Adding `swarm-ui` is a fifth entry in the `services` map at
   `terraform/infra/main.tf:245-266`, which inherits internal ingress, the invoker
   validation and Direct VPC egress for free (`terraform/modules/cloud_run/main.tf:9-53`),
   and the name passes the module's own `startswith(k, "swarm-")` validation
   (`terraform/modules/cloud_run/variables.tf:80`). **It is not only that entry, and the
   spec should not pretend otherwise** — each of these is a real file in another track's
   area, and every one of them is a required edit:
   - a service account in `local.platform_accounts`
     (`terraform/modules/iam/service_accounts.tf:17-34`), because the services map wants
     `module.iam.service_account_emails["swarm-ui"]` and that map has exactly four keys today;
   - a `service_max_instances` entry (`terraform/infra/variables.tf:359-373`, whose
     default names only `swarm-api`, `swarm-scheduler`, `swarm-quota-broker`,
     `swarm-reconciler`) — a missing key is a plan-time error, not a default;
   - a `local.service_env` entry (`terraform/infra/locals.tf:295`);
   - a build target in `scripts/build-images.sh:22`'s `ALL_TARGETS`, or the image the
     services map names is never pushed.
   Every new resource carries `managed-by=swarm-terraform` or `make destroy` aborts on
   it, and every new name must clear `SHARED_DENY_LIST` (`scripts/lib/common.sh:202-223`)
   — note `"default"` is on that list, which is a live trap when naming a URL map's
   default backend or any resource the module derives a name for.
3. **Cloud Run edge IAM needs no change.** `api_invokers = ["allUsers"]`
   (`terraform/environments/dev/dev.tfvars:218-225`) is already the supported
   configuration, documented on measured evidence at
   `terraform/infra/variables.tf:278-309`: when Cloud Run enforces edge IAM it *consumes*
   the caller's `Authorization` header and the container sees a different, non-JWT
   credential (measured 2026-09-16: client sent `tok:7f518e564d27`, app saw
   `tok:c130e289a085`, MalformedError). `swarm-ui` should get the same treatment for the
   same reason (it has no auth of its own; IAP is its gate, ingress is its wall), and the
   paired validation at `terraform/modules/cloud_run/variables.tf:115-130` permits
   `allUsers` only while ingress is not `INGRESS_TRAFFIC_ALL`, which is exactly the
   configuration being kept.
4. **A terraform ordering trap worth naming once.** The IAP JWT's `aud` claim is
   `/projects/<project-number>/global/backendServices/<numeric id>` — known only after
   the backend service is created, and swarm-api needs it in its environment to verify
   the assertion. That reads like a cycle. It is not, provided the serverless NEG names
   the Cloud Run service by **literal string** (`service = "swarm-api"`) rather than by
   resource attribute: then NEG → backend service → cloud_run env is a straight line.
   I did not apply this; treat it as a design note for Track C, not a verified fact.

**Enabling IAP is not a one-line terraform change, and this section previously implied it
was.** `iap.googleapis.com` is absent from the API list terraform enables
(`terraform/infra/main.tf:23-42`, eighteen services, no IAP) and was verified absent from
`gcloud services list --enabled`. IAP also needs an OAuth brand/consent screen
(`google_iap_brand`, internal type), which is **org-scoped and typically needs
Workspace-admin rights** — the same rights `terraform/environments/dev/dev.tfvars:232`
already records as missing for creating `swarm-admins@saga.xyz`. So the brand may be a
human step outside this repository, and it should be started before anyone schedules the
UI work, not discovered during it.

**DNS and the certificate are an external dependency, possibly a blocking one.**
`google_compute_managed_ssl_certificate` cannot provision until the hostname's A record
points at the global address, and `gcloud dns managed-zones list` returned **Forbidden**
for bogdan@saga.xyz — so saga.xyz DNS may not be manageable from this project or by this
operator at all. Until someone confirms who owns that zone, "one hostname" is an
assumption, not a plan. A self-managed cert or a `*.run.app`-style placeholder does not
rescue this: IAP needs a stable hostname the browser trusts.

**`/openapi.json` and `/docs` behind IAP is a decision, and it is this one.** Those
routes are unauthenticated in the app (`main.py:61-66`; `create_app()` attaches no global
dependency), which is harmless while nothing can reach the service and becomes a public
schema dump the moment an LB exists. Routing them through the IAP-gated `swarm-api`
backend is what keeps them internal, and it is why they appear on the API path in the URL
map above rather than being excluded from it. The same applies to `/healthz` and
`/readyz`: they go behind IAP too. Nothing breaks today — `terraform/modules/monitoring/`
contains no uptime check at all — but if one is ever added it must target the `.run.app`
URL directly, because an IAP 302 would read as a healthy 200 to a naive check.

**New terraform outputs.** `terraform/infra/outputs.tf` exports `service_urls`
(a map, lines 71-73) and no `api_url`, which is why `scripts/lib/common.sh:477`'s
`tf_output api_url` silently returns empty and every script falls through to
`gcloud run services describe` (`common.sh:471-505`, with the honest `die` at :505). Add
`api_url` and `ui_url` (the LB hostname) when the module lands, or the deploy pipeline
and the UI's own build-time config have nothing to read.

**What this costs in dollars.** A global forwarding rule plus a small amount of data
processing — order of magnitude, a couple of tens of dollars a month, dominated by the
forwarding rule rather than traffic. IAP itself adds no per-request charge. Verify
against current list prices before anyone quotes this number; I did not.

---

### 2. The auth chain, header by header

What the app does **today** with an authenticated request, verified line by line:

| Step | Where | What it does |
|---|---|---|
| Pick a credential | `deps.py:139-165` | Reads both `Authorization` and `X-Serverless-Authorization`, **preferring the latter** |
| Split it | `auth.py:125-152` | Splits on `,` and returns every `Bearer` credential, because Starlette joins repeated headers |
| Verify | `auth.py:93-109` | `google.oauth2.id_token.verify_oauth2_token` against Google's OAuth2 certs; requires `email`; rejects `email_verified` only when explicitly `False` |
| Domain | `identity.py:92-98` | `email.rsplit("@",1)[1]` must be in `ALLOWED_DOMAINS` (`saga.xyz`). **Nothing reads an `hd` claim anywhere in this repository** |
| Groups | `groups.py:152-168` | One `checkTransitiveMembership` per registered tenant/admin group, cached per `(caller, group)` for `GROUP_CACHE_TTL_SECONDS` (default 120, `settings.py:74`) |
| Tenant | `identity.py:101-113` | First match in admin-ordered `TENANT_GROUPS` wins; otherwise `u-<local part>` |
| Admin | `auth.py:225-226` | `is_admin` = membership in any `ADMIN_GROUPS` entry |
| Rate limit | `deps.py:181-186` | In-process token bucket keyed by principal email |

**What changes for IAP.** IAP presents `X-Goog-IAP-JWT-Assertion`: ES256, `iss =
https://cloud.google.com/iap`, keys from the gstatic IAP JWK set, `aud` as above. The
existing verifier rejects it outright — it pins Google's OAuth2 certs URL and the
`accounts.google.com` issuer. Two details happen to fall in our favour: `auth.py:107-108`
rejects `email_verified` only when it is explicitly `False` (IAP omits the claim), and
the domain is derived from the email, so no `hd` claim is needed. The new verifier is
genuinely small — `google.auth` in this repo already supports ES256
(`google/auth/crypt/__init__.py:47-48`, `google/auth/jwt.py:69-72`), so it is
`id_token.verify_token(assertion, request, audience=IAP_AUDIENCE,
certs_url=<IAP JWK url>)` plus an explicit `iss` check, selected by settings, sitting
alongside `GoogleTokenVerifier` in `apps/swarm-api/swarm_api/auth.py`. *The exact claim
set an IAP JWT carries in this org was not verified — no IAP instance exists to inspect.*

Three rules the implementation must not get wrong:

- **Verify the signed assertion. Never trust `X-Goog-Authenticated-User-Email`.** That
  header is a plain string. Ingress admits any load balancer in this shared project, and
  `allUsers` holds `run.invoker`, so a header-derived identity is forgeable by anyone who
  can create a serverless NEG in `saga-agents-staging`.
- **Require the audience when hardened.** `ApiSettings.hardened` (`settings.py:102-105`)
  is False for `ENVIRONMENT=dev`, which is why `GoogleTokenVerifier` currently runs with
  the `aud` check *off* — `API_AUDIENCE` is not set on the live revision at all, so any
  `saga.xyz` Google ID token authenticates. Reproduce the same `require_audience` guard
  for the IAP verifier (`auth.py:74-82`) or you rebuild the identical silent hole.
- **Precedence.** Prefer a valid IAP assertion; fall back to the `Authorization` /
  `X-Serverless-Authorization` path for machine callers. Do not delete the bearer path:
  a service account arriving through IAP carries a `*.gserviceaccount.com` email, which
  `assert_allowed_domain` refuses, and adding `gserviceaccount.com` to `ALLOWED_DOMAINS`
  would make `resolve_tenant` file every machine request under one tenant
  (`identity.py:101-113`) — multi-tenancy collapses. Machine access through IAP is a
  separate, unsolved problem; nothing regresses by leaving it unsolved, because scripts
  cannot reach the API today either (`scripts/swarm.py` goes in-process to Firestore
  precisely for this reason).

One consequence for later, recorded here so it is not discovered in prod: keeping both
paths means two audiences, and `Settings.api_audience` is a single string
(`apps/common/swarm_common/config.py:47,112`), consumed as `audience: str` at
`auth.py:74`. That file is FROZEN, so making it a CSV tuple is a **contract-change
request** per CLAUDE.md rule 1, not an edit. It is latent while `hardened` is False and
becomes a hard break the day `ENVIRONMENT` is not `dev`.

---

### 3. Screen F1 — The gate: sign-in, and "you have no access"

**WHAT THE USER SEES.** Three distinct pages, and it matters that they are three:

1. **Google's own consent screen**, owned by IAP. We render nothing. The user lands on
   `https://swarm.<domain>/`, IAP 302s to Google, they pick an account, they come back.
   There is no "Sign in with Google" button in our code and no OAuth client in the SPA.
2. **IAP's 403 page**, for an authenticated Google account that lacks
   `roles/iap.httpsResourceAccessor`. Also not ours. It is Google's generic
   "You don't have access" page. This is the page a new engineer hits before anyone adds
   them to `eng@saga.xyz`, so the grant must be to the group, not to individuals.
3. **Our own "authenticated, but not admitted" page** — the one we do own, reached when
   IAP let them in but swarm-api refused. Full-bleed, one column, max 420pt wide,
   centred. It shows: the email IAP asserted, and exactly one of four reasons, each with
   its own copy and its own next step:
   - `403 forbidden` / `domain <x> is not permitted` (`identity.py:96`) — "SwarmCloud
     accepts saga.xyz accounts. You are signed in as `<email>`." + a "switch account"
     link. *The sign-out path `/_gcp_iap/clear_login_cookie` is Google's, not ours, and
     is not verifiable in this repository — confirm it against IAP's docs before shipping
     the link, and fall back to plain copy ("sign out of Google and sign in again") if it
     does not hold.*
   - `503 upstream_unavailable` / *"group membership could not be resolved; retry
     shortly"* (`auth.py:213-214`) — "We could not confirm which team you belong to. This
     is Google's group service, not your account." + a Retry button. **This is never
     rendered as "no access"**; it is the `upstream_degraded` row of §5's table, and it
     fires only when a Cloud Identity lookup failed at a *higher* priority than any
     confirmed match (`groups.py:211-218`), i.e. exactly when the caller's tenant is
     genuinely unknown rather than merely unconfirmed.
   - `409 conflict` from `_assert_principal_matches` (`store.py:255-275`) — "Your tenant
     id collides with a registered group. This is a platform misconfiguration." + the two
     principals from `detail.registered_principal` / `detail.requested_principal`.
   - `403 forbidden` / *"tenant `<x>` is disabled"* (`service.py:97-98`).

**WHERE EVERY VALUE COMES FROM.** The asserted email: the `email` claim of the verified
IAP JWT, echoed back by `GET /v1/tenants/me` → `principal.email` (`tenants.py:30-37`).
Every reason string: the JSON error envelope from `ApiError.to_payload()`
(`errors.py:24-28`) — `{"code": "...", "message": "...", "detail": {...}}` — with the
`code` values enumerated at `errors.py:31-67`. The `WWW-Authenticate` header on a 401 is
set at `main.py:104-105` (and again on the `AuthError` handler at `main.py:116`).

**WHAT IT CANNOT SHOW YET.** Nothing here renders at all until the ALB and IAP exist and
the IAP verifier ships — prerequisites **P1, P2, P3** in §6. Until then the entire product
is unreachable; this is not a screen that degrades, it is a screen that does not exist.
Also: we cannot show "you are not in eng@saga.xyz, ask X to add you", because the API
never reports *which* groups were checked — `groups_for` returns only the matches
(`groups.py:170-219`) and `GET /v1/tenants/me` returns `principal.groups` (the matches)
but never the candidate list. Adding `tenant_groups_checked` to that response is three
lines in `routes/tenants.py`; without it the copy has to stay generic.

**REFRESH AND COST.** Zero polling. One `GET /v1/tenants/me` on mount: one Firestore
document read, plus up to two Cloud Identity round trips on a cold auth (two registered
groups today, `TENANT_GROUPS=eng@saga.xyz,swarm-smoke@saga.xyz` on the live revision),
cached 120s. **But note the side effect in §4's second warning: this endpoint writes.**

**PHONE LAYOUT.** This is the one screen that is already phone-shaped. Single column,
24pt side gutter (not 16 — this page is text, it should breathe), the reason in 17pt
regular, the action as a full-width 48pt button. The asserted email in a monospace chip
that wraps rather than truncates; a truncated email on a mis-signed-in page is the one
piece of information the user needs to see in full.

**EMPTY, LOADING AND BROKEN.** There is no empty state. Loading is a 200ms-delayed
spinner (anything faster flashes). Broken is the point of the screen — and the one trap
is that a *network* failure here must not be painted as *forbidden*. If the fetch throws,
or returns HTML, the copy is "SwarmCloud is unreachable", not "you do not have access".
Blaming the user for an outage is how an incident becomes a support ticket.

---

### 4. Screen F2 — Identity and tenant bar (the app shell header)

**WHAT THE USER SEES.** A single 44pt-high row pinned above everything, present on every
screen. Left to right: the SwarmCloud wordmark; a **tenant chip** showing
`display_name` with the raw `tenant_id` in monospace beneath it at 11pt; an **admin
badge** (a filled dot + "ADMIN") *only* when `principal.is_admin` is true; a
**dispatch-paused banner** that takes over the whole row in amber when dispatch is
paused; and the avatar initial. Tapping the chip opens a detail sheet containing, as a
labelled list:

| Field | Value shown |
|---|---|
| Signed in as | `principal.email` |
| Domain | `principal.domain` |
| Groups matched | `principal.groups[]`, or the literal text "none — personal tenant" |
| Tenant | `tenant.tenant_id` · `tenant.kind` (group/user) |
| Tenant principal | `tenant.principal` |
| Concurrency ceiling | `min(tenant.max_active, tenant.capacity_units)` with both numbers shown |
| Enabled | `tenant.enabled` |
| Provider keys registered | `tenant.credentials[]` — **provider names only, never key material** |
| Worker identity | `tenant.service_account`, labelled *"expected — existence not checked"* |
| Artifact prefix | `tenant.gcs_prefix` |
| Namespace | `tenant.namespace` |
| Admin rights | `principal.is_admin`, and when false, the one-line reason (see below) |

**WHERE EVERY VALUE COMES FROM.** All of it from one call: `GET /v1/tenants/me`
(`apps/swarm-api/swarm_api/routes/tenants.py:24-38`). The `tenant` object is
`codec.tenant_to_api` (`codec.py:283-303`), reading the Firestore document
`tenants/<tenant_id>`. The `principal` object is assembled inline at `tenants.py:32-37`
from the `AuthContext` — note `is_admin` lives **inside** `principal`, not at the top
level of the body; there is no top-level `is_admin` key. `dispatch_paused` comes from
`GET /v1/stats` → `service.py:277`, which reads Firestore `control/dispatch` via
`Store.get_control` (`store.py:707-715`). The concurrency ceiling arithmetic is not
invented by the UI — it is exactly what `Store.set_tenant_limits` writes into the
`tenant:<id>` pool's hard limit (`store.py:310-320`, and `store.py:248-251` on tenant
creation), so showing anything else would contradict what the scheduler enforces.

**WHAT IT CANNOT SHOW YET, and I am unhappy about all four:**

- **`is_admin` is `False` for every human on this deployment, unconditionally.** The
  live swarm-api revision carries an `ADMIN_GROUPS` entry with no value; `_csv()`
  (`settings.py:34`, called at `settings.py:122`) turns that into `()`; `auth.py:225-226`
  then computes membership against an empty set. `terraform/infra/variables.tf:228-250`
  documents empty as the deliberate dev default, `dev.tfvars` sets `admin_groups`
  nowhere at all, and `dev.tfvars:232` records that `swarm-admins@saga.xyz` **does not
  exist in this Workspace** and creating it needs Workspace-admin rights. So the admin
  badge is correct-but-permanently-dark, and every `/v1/admin/*` screen and `/metrics`
  403s. The bar must therefore render a specific line — *"admin surface unavailable: no
  admin group is configured on this deployment"* — rather than letting the UI imply the
  user merely lacks rights. This is a platform state, not a permission.
- **`GET /v1/tenants/me` is a WRITE.** `get_me` → `submissions.tenant_for`
  (`service.py:82-99`) → `Store.ensure_tenant` (`store.py:190-252`), which on first sight
  **creates** the tenant document *and* a `tenant:<id>` slot pool (`store.py:247-251`). A
  dashboard that calls this on every page load will, for an engineer nobody has
  provisioned, quietly manufacture a control-plane tenant whose GSA, secrets, GCS prefix
  and namespace do not exist — and their first task then fails to dispatch with a
  missing-identity error. Call it **once per app mount**, cache the result in memory for
  the session, and when the returned `created_at` is within the last minute, raise a loud
  banner: *"This tenant was created just now. Its infrastructure is not provisioned — run
  `scripts/register-tenant.sh`."* The same side effect is on `GET /v1/providers`
  (`service.py:335`), so that endpoint is not a free read either. `/v1/stats` and
  `/v1/capacity` do **not** call `tenant_for` (`service.py:271-289`, `:290-331`) and are
  genuinely read-only — which is why F4, not this bar, owns the polling.
- **`tenant.service_account` is a derived string, not a checked identity.**
  `store.derived_service_account` (`store.py:107-131`) builds
  `swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com` and returns `None` only
  when it would exceed Google's 30-character id limit. It never asks IAM whether that
  account exists. The nearest honest label is the one in the table above. A real check
  needs an IAM read swarm-api does not perform and has no role for. When it is `null`,
  render *"no identity — this tenant cannot dispatch"* in amber; `null` here is a loud
  failure by design and must never render as blank.
- **No session expiry time.** The IAP session's remaining life is not visible to the
  application; the assertion's `exp` is not surfaced by any route. F5 handles expiry
  reactively instead.

**REFRESH AND COST.** One document read per call after the first. Refresh **on window
focus after 5 minutes hidden, and not on a timer** — the fields here (`enabled`,
`max_active`, `capacity_units`, `credentials`) change only through an admin route or
`register-tenant.sh`, never minute to minute, and every call re-enters the
`ensure_tenant` path. `dispatch_paused` does **not** come from here on a timer: it
piggybacks on F4's `/v1/stats` poll, and F4 sets that interval — which matters, because
`/v1/stats` calls `count_tasks_by_state` (`store.py:524-533`), one Firestore aggregation
COUNT query **per TaskState, twelve per call**. This is the panel that must not poll at
1Hz.

**PHONE LAYOUT.** At 390pt the bar shows wordmark + tenant chip + admin dot + avatar, and
nothing else; the tenant chip truncates the `display_name` at one line with the
`tenant_id` hidden. The detail sheet is a bottom sheet at 85% height, one label-value pair
per row, values selectable (an operator will want to copy the GCS prefix). The
dispatch-paused banner, when active, replaces the entire bar contents with the amber
message and stays — it is the one thing that must never be behind a tap.

**EMPTY, LOADING AND BROKEN.** Loading: the chip renders as a skeleton, never as a
placeholder tenant name. Broken: the chip goes to a struck-through dash on an amber
field with the error code beside it, and **the rest of the app is not rendered as
empty** — if we do not know who the user is, we do not claim they have no agents. A
failed `/v1/tenants/me` puts the whole shell in degraded mode (F5). There is no empty
state: this endpoint either answers or fails.

---

### 5. Component F3 — the fetch contract, and the rule that "no data" and "broken" are different pixels

This platform's defining bug, found repeatedly: an error rendered as an empty success.
Commit `9c639af` is literally titled *"An HTTP error is not an empty result"*; an expired
session rendered as an empty, healthy platform on `status.sh` during an incident, and
turned two concurrency assertions green by giving them nothing to count. A screen that
cannot tell "no agents running" from "the query failed" is that bug with a nicer font.

So this is a **specified component, not a convention**. Every value on every screen in
this product renders through it, and it is the first thing to build because it depends on
nothing.

**The result type.** Every fetch resolves to exactly one of five states, never a bare
array:

```ts
type Result<T> =
  | { status: 'loading';  since: number }
  | { status: 'ok';       data: T; fetchedAt: number; serverAt?: string }
  | { status: 'empty';    fetchedAt: number; serverAt?: string }   // 2xx, zero rows
  | { status: 'stale';    data: T; fetchedAt: number; error: ApiError } // had data, refresh failed
  | { status: 'error';    error: ApiError }                        // never had data
```

`ApiError` is the server's own envelope, not a string:
`{ code, message, detail?, httpStatus, kind }` where `code` and `message` come verbatim
from `ApiError.to_payload()` (`errors.py:24-28`) and `kind` is the client's
classification below.

**The `stale` rule, because the type declares it and nothing was saying what produces
it.** Any refresh that fails while a previous `ok` or `empty` is in hand produces
`stale`, never `error` and never a silent no-op: the last-known data stays on screen,
dimmed to 60% opacity, with the error code and the age of `fetchedAt` in the panel header.
`stale` is what a dashboard looks like during a Cloud Identity blip or a 429, and it is
the state that keeps an operator from reading a frozen number as a live one.

**Three cross-cutting rules, in the words the incidents earned:**

- **Never render a `null` as `0`.** A missing number and a zero are different pixels.
  `tenant.service_account: null` is "no identity", not an empty string; an absent count is
  an em dash on a grey field, not a zero.
- **Unreadable must be visually louder than zero.** The whole failure class is that zero
  looks calm. An error state gets colour and weight; an empty state gets muted text.
- **`enabled` is compared explicitly.** CLAUDE.md records `false // true` returning `true`
  in jq as a bug that has already cost this repository a working check — the paused pool
  that reported as open. The TypeScript equivalent is `pool.enabled ?? true` and
  `!pool.enabled` on a possibly-absent field. Compare `pool.enabled === false` for
  "paused" and require the key to be present.

**The classification table — this is the whole contract.**

| What arrives | `kind` | What the UI does |
|---|---|---|
| 2xx, list has rows | `ok` | Render. Show age from `serverAt` when the body carries `generated_at`, else from receipt time |
| 2xx, list is `[]` | `empty` | Render "No agents in this view" in muted text **with the checked-at time beside it** |
| **2xx but the body is not JSON** (HTML, or a content-type that is not `application/json`) | `session_expired` | **Session overlay (F5), never `empty` and never `ok`.** See the note below — this, not the 401, is how an expired IAP session actually arrives |
| 401 + `WWW-Authenticate` (`main.py:104-105`, `:116`) | `unauthenticated` | Session overlay (F5). Never an empty list |
| 403, message contains *"admin group membership is required"* (`auth.py:248-251`) | `admin_required` | Lock icon + the platform reason from F2. Not empty, not forbidden-to-you |
| 403, `domain ... is not permitted` (`identity.py:96`) | `wrong_domain` | Gate page F1 |
| 403, `tenant ... is disabled` (`service.py:97-98`) | `tenant_disabled` | Gate page F1 |
| 404 `not_found` | `not_found` | "This task does not exist, or belongs to another tenant" — the API deliberately returns the same 404 for both (`store.py:369-379`), so the copy must not promise which |
| 409 `conflict` | `conflict` | Surface `detail` verbatim, including `registered_principal` / `requested_principal` (`store.py:270-274`) |
| 422 `validation_failed` (`errors.py:31-33`) | `invalid` | Field-level message on the form that sent it; never a page-level error |
| 429 `rate_limited` + `Retry-After` (`main.py:102-103`, `errors.py:56-63`) | `rate_limited` | Hold the existing data as `stale`, stop polling for `Retry-After` seconds, show "refreshing paused". Never blank the screen — the data was correct a second ago |
| 503 `upstream_unavailable` (`errors.py:65-67`; `auth.py:213-214` for the group case) | `upstream_degraded` | "We could not confirm which team you belong to" + Retry. **Never "no access", never empty** |
| 5xx other, or a body that is not an `ApiError` envelope | `server_error` | `error` state with the raw status; do not invent a `code` |
| `fetch` rejects (DNS, TLS, offline, CORS preflight) | `unreachable` | "SwarmCloud is unreachable" + Retry. Distinct from every 4xx: this one is never the user's fault |

**The row that matters most, and the reason this table was not finishable without it.**
An expired IAP session does not arrive as a 401 JSON envelope. The ALB intercepts the
request before swarm-api sees it and answers with a 302 to Google's sign-in page;
`fetch()` follows redirects by default, so the SPA receives a **200 carrying HTML**. Every
JSON parse then fails, and the naive handler — the one this whole component exists to
prevent — turns that into an empty array and paints a calm, healthy, empty platform. That
is precisely the `status.sh` incident, reproduced in a browser. So: parse the content-type
before the body, treat a non-JSON 2xx on a `/v1/*` route as `session_expired`, and hand it
to F5. *This is inferred from how IAP fronts an XHR, not measured here — no IAP instance
exists to test against. It is the first thing to verify the day P2 lands, and the
`credentials`/`redirect` handling on the fetch wrapper is where the verification belongs.*

**Rate-limit arithmetic the UI has to live inside.** The token bucket is **20 rps per
principal per instance**, hard-coded: `config.py:74` declares `requests_per_second = 20`
and `from_env()` never reads it, so no environment variable changes it. Burst is
`max(40, 20*2) = 40` (`settings.py:127`). With `max_instances` for swarm-api the effective
ceiling is fuzzy — requests land on whichever instance the LB picks — so treat 20 rps as
the budget and count every open tab against it. Page sizes default to 50 and cap at 200
(`deps.py:194-199`).

**PHONE LAYOUT.** Error surfaces are inline, in the panel that failed, never a toast: a
toast at the bottom of a 390pt screen sits under the thumb and gets dismissed by accident,
and the one thing this component exists to guarantee is that a failure is still on screen
when the operator looks. `empty`'s "checked at" timestamp moves below the message rather
than beside it under 420pt. `stale`'s dimming plus header badge survives at phone width
unchanged — it is the cheapest of the five to render small, which is deliberate.

---

### 6. Prerequisites, in order, with what each one unblocks

Referenced throughout as P1–P5. They are not equal and they are not parallel.

| | Prerequisite | Owner | Unblocks | Blocked on |
|---|---|---|---|---|
| **P1** | External global ALB: serverless NEG, backend services, URL map, target HTTPS proxy, managed cert, global address, forwarding rule | Track C (`terraform/`) | Everything. Nothing in this product renders without it | A hostname and a DNS zone nobody has confirmed we can write to (§1) |
| **P2** | IAP: enable `iap.googleapis.com`, OAuth brand + client, `iap {}` on both backend services, `roles/iap.httpsResourceAccessor` to `eng@saga.xyz` | Track C, plus a human | F1, and any identity at all | The brand is org-scoped and typically needs Workspace-admin — the same rights already missing for `swarm-admins@saga.xyz` |
| **P3** | IAP assertion verifier alongside `GoogleTokenVerifier`, selected by settings | Track A (`apps/`) | Every authenticated route. Without it IAP admits the user and swarm-api rejects them | P2 (needs a real `aud`) |
| **P4** | `swarm-ui` Cloud Run service: SA, max-instances entry, env entry, build target, image | Tracks C and D | Serving the SPA same-origin, which is what deletes CORS | Nothing — can start now |
| **P5** | `ADMIN_GROUPS` set to a group that exists | Track C, plus a Workspace admin | Every `/v1/admin/*` screen and `/metrics`. **Independent of P1–P4**: fixing the network changes nothing here | A Google group that does not exist in this Workspace |

P5 is the one that gets forgotten, because P1 is so much more visible. A team that ships
P1–P4 and demos the product will still have zero working admin screens, and the reason
will not be in any error message the UI can show.

---

### 7. The route map every other section builds on

Verified by reading the routers. `create_app()` (`main.py:54-73`) includes exactly six:
health, tasks, workflows, tenants, platform, admin.

**Unauthenticated in the app** (behind IAP once P1/P2 land, which is the point):

| Route | Where | Notes |
|---|---|---|
| `GET /healthz` | `health.py:36-39` | `{status, runner_profiles[]}`. Touches no dependency |
| `GET /readyz` | `health.py:42-52` | Real Firestore round trip; 503 when not ready |
| `GET /docs`, `GET /openapi.json` | `main.py:61-66` | Full schema dump. Useful for generating a typed client; behind IAP by design (§1) |

**Tenant-scoped**, all `Depends(current_auth)`, prefix `/v1`:

| Route | Where | Notes |
|---|---|---|
| `POST /v1/tasks` | `tasks.py:37` | |
| `POST /v1/tasks/batch` | `tasks.py:50` | |
| `GET /v1/tasks` | `tasks.py:64` | Filtered, paged: default 50, cap 200 |
| `GET /v1/tasks/{task_id}` | `tasks.py:98` | Same 404 for missing and cross-tenant (`store.py:369-379`) |
| `POST /v1/tasks/{task_id}/cancel` | `tasks.py:107` | |
| `GET /v1/tasks/{task_id}/events` | `tasks.py:123` | Paged Firestore subcollection read. **The only live-progress source there is** |
| `GET /v1/tasks/{task_id}/artifacts` | `tasks.py:134` | **Metadata only.** `store.py:501-508` never mints a download URL |
| `POST /v1/workflows` | `workflows.py:15` | |
| `GET /v1/workflows` | `workflows.py:27` | |
| `GET /v1/workflows/{workflow_id}` | `workflows.py:44` | |
| `POST /v1/workflows/{workflow_id}/cancel` | `workflows.py:60` | |
| `GET /v1/tenants/me` | `tenants.py:24` | **WRITES on first sight** (§4) |
| `POST /v1/tenants/me/credentials` | `tenants.py:41` | Write-only; no read path returns key material |
| `GET /v1/stats` | `platform.py:13` | Read-only. **Twelve COUNT queries per call** |
| `GET /v1/capacity` | `platform.py:21` | Read-only. Own tenant's pool + shared pools |
| `GET /v1/providers` | `platform.py:29` | **WRITES on first sight** (`service.py:335`) |

**Admin**, all `Depends(admin_auth)` → `require_admin` → `ctx.is_admin`, **403 for every
caller today** (P5):

| Route | Where |
|---|---|
| `GET /metrics` | `health.py:55` |
| `GET /v1/admin/dispatch` | `admin.py:70` |
| `POST /v1/admin/dispatch/pause` · `/resume` | `admin.py:78` · `:90` |
| `PUT /v1/admin/limits/global` · `/provider/{p}` · `/resource/{rc}` · `/runner/{rp}` · `/tenant/{t}` | `admin.py:107` · `:118` · `:131` · `:144` · `:157` |
| `PUT /v1/admin/tenants/{tenant_id}/limits` | `admin.py:171` |
| `POST /v1/admin/providers/{p}/drain` · `/resources/{rc}/drain` · `/providers/{p}/enabled` | `admin.py:219` · `:245` · `:267` |
| `GET /v1/admin/pools` · `GET /v1/admin/tenants` | `admin.py:296` · `:305` |

**What has no route at all**, so no section may promise it: any streaming transport (no
`StreamingResponse`, no SSE, no WebSocket, no long-poll anywhere in `apps/swarm-api`); any
artifact or log **body** (metadata only, and no `generate_signed_url` / `signBlob` /
`roles/iam.serviceAccountTokenCreator` exists anywhere in `apps/` or `terraform/`); leases;
attempts (`codec.attempt_from_dict` at `codec.py:159` is dead code, defined and never
called — though `execution_name` is readable through `GET /v1/tasks/{id}/events`, whose
`detail` is returned unfiltered). "Live" anything means polling, and polling means the
rate-limit arithmetic in §5.

---

### 8. Screen F4 — System status panel

**WHAT THE USER SEES.** A collapsible strip under the identity bar, present on every
screen, four tiles wide on desktop and a horizontally-scrolling row of the same four on a
phone: **Dispatch** (running / PAUSED, amber), **My tasks** (a count per non-zero state),
**My capacity** (`active` / `effective_limit` for the `tenant:<id>` pool, with a bar), and
**Platform** (ready / not-ready from `/readyz`). Expanding it shows every pool the caller
is entitled to see, one row each: name, `active`, `effective_limit`, `hard_limit`,
`adaptive_target`, `quota_derived_limit`, and a paused chip when `enabled === false`.

**WHERE EVERY VALUE COMES FROM.** `GET /v1/stats` (`platform.py:13-18` →
`service.py:271-288`) gives `tasks_by_state`, `dispatch_paused`, `limits{max_batch_size,
max_input_bytes, max_workflow_steps, requests_per_second_per_instance}` and `generated_at`.
`GET /v1/capacity` (`platform.py:21-26` → `service.py:290-331`) gives `pools[]` through
`codec.pool_to_api` and `runner_profiles{}` with each profile's resource class, backend,
provider, units and pool names. `GET /readyz` (`health.py:42-52`) gives the Firestore
round trip. Neither `/v1/stats` nor `/v1/capacity` calls `tenant_for`, so unlike
`/v1/tenants/me` and `/v1/providers` these are genuine reads with no write side effect —
which is the whole reason the polling lives here and not in F2.

**WHAT IT CANNOT SHOW YET:**

- **`platform_tasks_by_state`** — the cross-tenant counts — is added to `/v1/stats` only
  when `ctx.is_admin` (`service.py:286-287`). Absent for everyone today (P5). The tile
  must render *"platform totals need an admin group"*, not a zero and not a blank.
- **Another tenant's pool is filtered out by design** (`service.py:295-311`): its `active`
  count is a usage signal about that tenant. A non-admin sees shared pools plus their own.
  So "the platform is full" is not answerable from this panel by a normal user, and the
  copy must not imply it is.
- **Configured-vs-live drift is not visible from any API.** Pool ceilings are seeded by
  terraform with `ignore_changes` and mutated live by the admin routes; only
  `scripts/pool-limit.sh --check` compares them. This panel shows the **live** value only,
  and must label it *live* rather than implying it matches what terraform declares.
- **`/v1/admin/dispatch`** would give who paused dispatch and why; it 403s (P5). So the
  amber banner can say *that* dispatch is paused and, from `control/dispatch` via
  `/v1/stats`, nothing more. Who and why stay invisible until P5.

**REFRESH AND COST.** `/v1/capacity` is cheap — one collection read, capped at 500 pools —
and may refresh at **15s**. `/v1/stats` is **not** cheap: twelve Firestore aggregation
COUNT queries per call (`store.py:524-533`), doubled to twenty-four for an admin once P5
lands. It refreshes at **30s**, pauses entirely when the tab is hidden, and backs off to
120s after any `rate_limited` or `upstream_degraded` result. `/readyz` at 60s. All three
render through F3, so a failed poll shows the last value as `stale`, dimmed, with its age
— never a zero.

**PHONE LAYOUT.** The four tiles become a single horizontally-scrolling row at 100pt each,
snap-aligned, with Dispatch pinned first and non-scrolling because it is the one that
changes an operator's behaviour. The expanded pool table becomes one card per pool, name
and `active/effective_limit` on the front row, the remaining four numbers in a 2×2 grid
beneath. No horizontal table scroll at 390pt.

**EMPTY, LOADING AND BROKEN.** Loading: tiles are skeletons with their labels already
drawn, so the layout does not jump. Empty is real and distinct here — `tasks_by_state`
with every state at zero means "you have submitted nothing", rendered as muted copy plus
the checked-at time, **not** as four zeros that look like a reading. Broken: each tile
fails independently, because `/v1/stats` and `/v1/capacity` are separate calls and one
failing must not blank the other; a failed tile shows an em dash on an amber field with
its error code, and the panel header carries the oldest `fetchedAt` across the three
sources so a wholly-frozen strip is obvious at a glance.

---

### 9. Screen F5 — Session expiry and degraded mode

**WHAT THE USER SEES.** Two distinct treatments, and conflating them is the failure this
screen exists to prevent.

1. **Session expired** — a full-screen modal, not a toast, not dismissible: "Your
   SwarmCloud session has ended. Sign in again to continue." One full-width button that
   reloads the current URL, which sends the browser back through IAP and returns it to
   the same page. **Whatever is on screen underneath stays rendered and dimmed**, so the
   user can see it was real data a moment ago, and no in-flight form loses its contents.
   Triggered by `kind: 'session_expired'` (non-JSON 2xx) or `kind: 'unauthenticated'`
   (401 + `WWW-Authenticate`, `main.py:104-105`).
2. **Degraded mode** — a persistent amber bar under the identity bar, not a modal:
   "SwarmCloud is answering, but we could not confirm your tenant" (503
   `upstream_unavailable`, `auth.py:213-214`) or "SwarmCloud is unreachable" (`fetch`
   rejected). The app stays usable for whatever is already loaded, every panel goes
   `stale`, submit actions are disabled with the reason on the disabled control, and
   polling backs off. Entered whenever `/v1/tenants/me` fails, because without an
   identity nothing below can be trusted to belong to this user.

**WHY THE FIRST ONE IS NOT JUST A 401 HANDLER.** Restating §5's key row because it is the
single most likely way this product reproduces its own defining bug: behind IAP, an
expired session does not reach swarm-api. The ALB answers the XHR with a 302 to Google,
`fetch` follows it, and the SPA gets **200 + HTML**. A handler that only looks at
`response.status` sees success, `JSON.parse` throws or yields nothing, and the dashboard
paints itself empty and healthy — with the identity bar still showing the user's name from
cached state. Content-type is checked before the body on every `/v1/*` response, and a
non-JSON 2xx goes straight here. *Inferred from IAP's redirect behaviour, not measured;
verify on the day P2 lands.*

**WHAT IT CANNOT SHOW.** How long is left. The IAP session's remaining life is not visible
to the application and the assertion's `exp` is not surfaced by any route, so there is no
countdown and no pre-emptive warning — expiry is discovered by a request failing, which is
why the treatment has to preserve what is on screen rather than assume the user can simply
redo it. Do not fake a countdown from a local timer.

**RATE LIMITING IS NOT EXPIRY.** A 429 (`main.py:102-103`, `Retry-After`) never opens this
overlay. It pauses polling for the advertised interval and marks panels `stale` — the
session is fine, the user is simply asking too often, most likely because they have four
tabs open against a 20 rps per-principal budget (`config.py:74`, not tunable by
environment). Treating it as expiry would sign a working user out mid-incident.

**PHONE LAYOUT.** The expiry modal is a full-screen sheet with the button at the bottom in
the thumb zone, 48pt tall, and no close affordance — there is nothing behind it the user
can act on. The degraded bar is 36pt, single line, truncating the reason with the error
code always visible at the right; tapping it opens the same bottom sheet F2 uses, with the
full error envelope including `detail`. Neither ever becomes a toast: both are conditions,
not events, and a condition that can be swiped away is a condition that gets missed.

**EMPTY, LOADING AND BROKEN.** This screen has no empty state and no loading state by
construction — it is itself the broken state of everything else. Its only failure mode is
being wrong about which of the two treatments applies, which is why the classification
lives in F3's table and not in this component.
