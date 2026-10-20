# QA identity: a browser agent signs in to the console behind IAP without a person typing a password

**Status: built on `main`, except the agent's instructions and the
acceptance run (2026-10-20).** §8 records what each lane of §6 landed and where the code
departs from this design. Written 2026-10-19 as a design, lane QA-IDENTITY of
[#1649](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/1649). The
owner decided on 2026-10-19 that this lane designs first, and that the owner
picks before any code is written. The owner's answers are in §7.

The request, from #1649: a SwarmCloud workflow sweeps every console page at
1440×900 and 390 wide, light and dark. It attaches a screenshot per finding
and a GIF per flow, "and signs in without a person each time, by whichever
identity option the owner picks". The owner listed three options: a QA Google
account with a saved login, a QA service account admitted through IAP with an
injected token, and QA against the fixture build. This document weighs those
three and two more. It recommends one, and records why each of the others was
turned down.

This is security work before it is browser work. Whatever identity the
agent's browser holds, **the agent can act as it**. So the question is never
"can the agent read the credential?" alone. It is also "what can that
identity do, and to whom?" Every option below answers both.

Every file and symbol cited was read on 2026-10-19 at `2af9bed`. Lines move, so
each citation names a symbol (`path::qualname`) or quotes text. Search for it.

What the design settles, one line each:

* **Recommended: option B.** A dedicated service account, `qa-console`,
  is admitted at the front door. It has its own service workspace and no
  other authority. swarm-api signs a short-lived **IAP service-account JWT**
  for it, whose audience is the console's one host. The worker hands the JWT
  to the live gate on `ctl.sock`. The gate adds it to the console's own
  requests on its private CDP session. The agent, the page, the checkpoint
  and the artifacts never hold it (§3.2, §4).
* **The task names the identity, never the credential.** A caller sends
  `input.qa_identity: "console"`. That is a name from the frozen catalogue
  (contract request QI-1, §5), as invariant 10 requires.
* **Option C stays the default for layout sweeps.** The fixture build is free,
  and it already worked for #1038 and #1427. B is for the pass that has to see
  the real console, the real API and the real IAP (§3.6).
* **Option A is turned down.** A Google user account in `saga.xyz` passes
  every IAP app that admits `domain:saga.xyz`. Its session also carries a full
  Google sign-in that any page can use. And LB-7 keeps saved logins to
  personal workspaces, so the login could not be used where the QA tasks run
  (§3.1).
* **The front door is the existing list.** The new account goes into
  `frontend_iap_members`, applied by the owner from `terraform/bootstrap`.
  That list only ever names the platform's own `swarm*` backends
  (`terraform/bootstrap/variables.tf`, `frontend_iap_backends`' validation),
  so no IAM row touches the other team's resources in `saga-agents-staging`.
* **Five things are unmeasured, and the build cannot start until each is
  measured** (M1 to M5, §4.9). The most important is M1: whether this front
  door accepts a service-account JWT at all. It refuses a service account's
  ID token, as measured on 2026-09-22 (`scripts/lib/common.sh`, `The bearer
  THIS address expects`).

---

## 1. The problem, as measured

### 1.1 What #1649 measured

A survey of `origin/main` `7c2e148` (2026-10-19) found seven gaps in visual QA
on SwarmCloud's browser runtimes. This lane owns gap 3, quoted from the issue:

> An agent has no identity for an IAP app. A person must type each sign-in,
> and the hand-off holds 1 minute unwatched. Saved logins are personal-workspace
> only and untested for Google. There is no live browser in prod.

What that means in practice, read from the code at `2af9bed`:

* **Every page of the console sits behind IAP.** The load balancer's UI and
  API backends both turn IAP on (`terraform/modules/frontend/main.tf`,
  `iap { enabled = true }`), and the console holds no token of its own:
  "IAP sits in front and the browser carries the session"
  (`apps/swarm-ui/README.md`). An agent's browser that opens
  `https://swarm.saga.xyz` (`frontend_hostname` in
  `terraform/environments/dev/dev.tfvars`) lands on Google's sign-in page.
* **The only way past that today is a person.** The agent asks for a
  hand-off ([live-browser.md](live-browser.md) §4.1). A tenant member takes
  control in the console and types their own password into the agent's
  browser (§4.2). If nobody is watching, the hold lapses after a minute and
  the agent carries on without being signed in (§4.3, "The bounded wait").
  A sweep of every console page at two sizes and two themes therefore needs a
  person standing by for the whole run, on every run.
* **The person's sign-in is then the agent's.** After Return control the
  agent drives a browser signed in as that person. It can reach every
  workspace the person belongs to, plus admin screens if the person is a
  platform admin. Nothing narrows that to "the QA tenant".
* **Saved logins (LB-7) do not reach this case.** A login is kept only in a
  personal workspace (`swarm_api.browser_logins.refuse_unless_personal`), for
  a reason that still holds: every worker of a group tenant runs as one
  service account ([live-browser.md](live-browser.md) §5.5.1). Separately,
  reading the code shows that the use path is not yet reachable on the one
  live-browser profile. `input.browser_login` is not declared in
  `_CLAUDE_CODE_BROWSER_INPUTS` (`apps/common/swarm_common/profiles.py`), and
  `swarm_common.profiles.check_inputs` refuses an undeclared key with
  `invalid_input`. `swarm_api.validation.PLATFORM_INPUT_KEYS` exempts only
  `prompt` and `territory`. That is read from code, not run. It matches the
  issue's "untested".
* **The console's own visual QA so far avoided sign-in completely.** #1038 and
  #1427 ran on the plain `claude-code` profile against the fixture build
  (`npm run dev`, "fixtures, no network", `apps/swarm-ui/README.md`).

### 1.2 What "done" lets someone do that they cannot today

* An operator submits one workflow, a `claude-code-browser` step with
  `input.qa_identity: "console"`, and walks away. The agent opens
  `https://swarm.saga.xyz`, lands on the Overview signed in as
  `qa-console`, and sweeps every page at 1440×900 and 390×844, light
  and dark, with no hand-off. It sees what a member of the QA workspace sees,
  backed by the real API.
* The same run cannot see any other workspace's tasks, secrets, people or
  settings. It cannot reach an admin screen, and it cannot take the sign-in
  out of the pod.
* A platform admin grants or revokes, per tenant, whether that tenant's tasks
  may name the QA identity. Each grant and each credential issued is audited.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

1. **The agent and the worker share a uid and a network namespace**
   ([live-browser.md](live-browser.md) §1). Anything the worker's identity can
   do, the agent can do: the GKE metadata server answers both. So **no
   credential may be mintable by a tenant's worker identity.** That rules out
   granting a worker GSA `TokenCreator` on anything.
2. **The live gate is the one component outside the agent's reach that
   touches the browser.** It runs as its own uid in the `display` sidecar. It
   drives Chromium on a pipe the agent cannot open, and the agent's CDP goes
   through its method policy (`apps/agent-worker/agent_worker/livegate/cdppolicy.py`).
   The egress ticket already travels this path: minted by swarm-api, carried
   by the worker on `ctl.sock`, used only inside the gate
   (`apps/agent-worker/agent_worker/egress_ticket.py`).
3. **The egress proxy cannot add a header.** It is CONNECT-only, a TLS tunnel
   it cannot see into (`livegate/egress.py`, "ONLY CONNECT"), and the console's
   hostname goes direct in any case (`SWARM_EGRESS_BYPASS`). So the brief's
   "attached by the egress proxy or the CDP gate" comes down to **the CDP
   gate**.
4. **The agent may enable `Network` and `Fetch` on its own sessions**
   (`cdppolicy.ALLOWED_METHODS`). It therefore sees request headers in
   `Network.requestWillBeSentExtraInfo` and in its own `Fetch.requestPaused`.
   A header the gate adds has to be scrubbed from what the agent receives.
5. **At this front door IAP uses a Google-managed OAuth client.** A service
   account's **ID token** is refused 401, "Invalid JWT audience", whatever its
   audience (measured 2026-09-22). An impersonated service account's **access
   token** passes, and is then subject to IAM (`scripts/lib/common.sh`,
   `The bearer THIS address expects`). Google's documentation names a third
   way that suits a Google-managed client: "a service account JWT"
   (`terraform/bootstrap/iap_programmatic_clients.tf`, the quoted passage).
   That third way has not been tried here (M1).
6. **IAP membership is the owner's apply.** The deployer holds no IAP role
   (`terraform/modules/frontend/main.tf`, "Who may pass IAP -- NOT MANAGED
   HERE"). `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`
   grants `roles/iap.httpsResourceAccessor` on **both** swarm backends
   (`terraform/bootstrap/wif.tf`,
   `google_iap_web_backend_service_iam_member.frontend_accessors`).
7. **A service workspace is admitted on the API backend only**
   ([workspace-kinds.md](workspace-kinds.md) §4.5). The job's C10 rule refuses
   the `-ui-backend`, so a service workspace made by the job cannot load the
   console's pages. The QA account needs the UI backend as well, and that is
   the `frontend_iap_members` line from fact 6.
8. **`var.tenants` is frozen to its four keys** (workspace-kinds Q8 (a),
   lane WK-TF-FREEZE). A new tenant comes from Admin › Tenants › New
   workspace, which is built but switched off until the owner's O1 steps
   ([workspace-kinds.md](workspace-kinds.md) status line).
9. **A service identity is refused person-only routes** (`swarm_api.auth.person_refusal`,
   `person_required`). Whatever B covers, it does not cover screens that only
   a person may use.
10. **Chromium in the sidecar blocks `chrome://`, `devtools://`, `view-source:`
    and `file://`** (`images/agent-runtime-browser/chromium-policy.json`,
    `URLBlocklist`). So the agent cannot open a net-export log or DevTools to
    read headers.

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

| # | Invariant | How B keeps it |
|---|---|---|
| 1 | Only LEASED…RUNNING cost | No new state and no new resource. A credential is signed only for an attempt holding a live lease (`_lease_is_live`, as the browser routes check). A QUEUED, PARKED or READY task gets nothing and costs nothing. Tasks the agent submits **as** the QA identity are ordinary tasks in the QA workspace, under its own pool ceiling. With no account lent to that workspace (Q3 (a)), they stay QUEUED and cost nothing. |
| 2 | All-or-nothing reservation | Admission is untouched. The QA workspace's tasks reserve through the same transaction as every other tenant's. |
| 3 | Concurrency from LEASED | Untouched. |
| 4 | No sleeping through a wait | Renewal is a short call with the same backoff as the egress ticket. A lapsed credential never makes the worker wait: the console falls back to Google's sign-in page and the agent is told why (§4.5). |
| 5 | Fencing | swarm-api signs only for the attempt's **current** generation, checked against the lease in the same read as the egress ticket. A fenced worker gets `stale_generation` and nothing to hand on. The gate drops its credential at `wipe`, which runs on every exit, fence included ([live-browser.md](live-browser.md) §5.5). |
| 6 | No Spot | Untouched. |
| 7 | requests == limits | Untouched: no container is added. The scrubber runs inside the gate's existing budget (§4.4). |
| 8 | Checkpoints | The credential lives only in the worker's protected heap and the gate's heap, never under `work/`, so no checkpoint can hold it. A resumed attempt asks for its own. Checkpointing stays mandatory and periodic, unchanged. |
| 9 | Tenant isolation | The QA account has its **own** service workspace (own GSA, namespace, GCS prefix and secrets), and no consuming tenant's GSA gains anything. A consuming tenant's agent reaches only the QA workspace, and only while a platform admin has granted that tenant (§4.2). Stated plainly: every granted tenant's agents see the **same** QA workspace, so anything one of them leaves there is visible to the others. Nothing on the shared-project deny-list is named: the account is `swarm-`-prefixed, and IAP grants reach only `swarm*` backends. |
| 10 | Callers pick by name | The caller sends `qa_identity: "console"`, a choice from the frozen catalogue (§5). They never send a token, an account, an audience, a host or a header. |

### 2.3 What it needs from `apps/common/swarm_common/`

One declaration: `claude-code-browser` gains the input `qa_identity`
(request QI-1, §5). Nothing else is needed. The Firestore collections are
swarm-api's own, as `browser_logins` and `live_sessions` are. `Tenant.kind`
stays `user` for a service workspace (workspace-kinds §5). `Settings` is
untouched, because swarm-api's configuration lives in its own `settings.py`.

---

## 3. Designs that could work

### 3.1 Option A: a QA Google account, signed in once, kept as a saved login

**Shape.** A user account in the domain, `swarm-qa@saga.xyz`. A person signs
it in once through take-control, then saves the session as a named LB-7 login
(`swarm-tenant-u-<qa>-browser-login-console`). QA tasks name that login.

**Threat model.**

* *A malicious page* runs in a browser signed in to **Google**, not only to
  the console. The saved jar holds `accounts.google.com`'s session, because
  that is what IAP's sign-in redirect needs. Any page can start "Sign in with
  Google" or an OAuth consent screen. An injected agent clicks through, and
  the attacker's app receives the account's identity and whatever scopes were
  granted. Workspace admin can block third-party apps for the account's OU.
  That is a domain setting outside this repository.
* *A compromised agent* acts as a full Workspace user, in every Google
  service that account can open and in **every IAP app that admits
  `domain:saga.xyz`**. In this shared project that is, as read on
  2026-10-19 at 20:30Z (`gcloud compute backend-services list --global`),
  four IAP-enabled backends: the console's two (`swarm-ui-backend`,
  `swarm-ui-ui-backend`) and Product Oracle's two (`oracle-api-be`,
  `oracle-web-be`). The other team's gateway backends had IAP off that day,
  but nothing stops one being switched on, and the account would then pass it
  too. The CDP policy refuses cookie reads, but a cookie without
  `HttpOnly` is readable by page script ([live-browser.md](live-browser.md)
  §5.5.1, "Not covered"). Which of Google's sign-in cookies lack it was not
  measured. Any that do can be carried out and replayed from anywhere until
  the session is revoked: days, not minutes.
* *Another tenant.* LB-7 stores the login in the QA account's **personal**
  workspace, where only `swarm-agent-worker-u-<qa>` can read it. That is
  sound. But it means only tasks **in that workspace** can use the login, and
  only the workspace's own person, the QA account, may submit there. So a
  person would have to sign in to the console *as* `swarm-qa@` to start each
  QA run. Making it usable from `eng` means storing the session in a group
  tenant, which LB-7 forbids. LB-7's reason (one member's personal session
  readable by every member's agents) does not apply to an account meant to be
  shared, but lifting the rule is the owner's call, and it widens §5.5.1.

**IAM rows.**

| member | role / grant | on | why |
|---|---|---|---|
| `user:swarm-qa@saga.xyz` | `roles/iap.httpsResourceAccessor` | both swarm backends | already granted through `domain:saga.xyz`. **No narrower row exists**: the account cannot be kept out of anything else the domain is admitted to |
| `user:swarm-qa@saga.xyz` | Workspace licence, an OU with third-party OAuth apps and Gmail/Drive off, 2-Step Verification by a security key a person holds | the Google Workspace domain | outside Terraform; the domain admin's |
| `user:swarm-qa@saga.xyz` | principal of personal workspace `u-<qa>` | Admin › People | where LB-7 can keep the login |
| `serviceAccount:swarm-agent-worker-u-<qa>` | `secretAccessor`, `secretVersionAdder` | the one `-browser-login-console` secret | set by swarm-api per secret (lane L8c, existing) |

**Cost.** A Workspace licence. A frozen-contract request to declare
`browser_login` on `claude-code-browser` (§1.1). Either a person signed in as
the QA account to start every run, or an owner decision lifting LB-7 for one
shared account. Then a session that has to be re-saved whenever Google ends
it. Google binds sessions to device and network signals, and this was never
measured on a pod: Chromium here is Chrome for Testing with no TPM, and each
attempt is a new pod. If the session does not survive a new pod, A costs one
hand-off per run, which is the problem we started with. **Covers** everything
a person sees, person-only screens included. **Cannot**: be scoped to the
console, run without a person in the loop until the session's survival is
measured, or stay clear of other IAP apps in the domain.

**Bootstrap.** Domain admin creates the account, OU and security key. An
admin approves its personal workspace. A person signs in through
take-control, saves the login, and repeats whenever it lapses.

### 3.2 Option B: a QA service account, its IAP JWT added by the gate (recommended)

**Shape.** A service account `qa-console@saga-agents-staging.iam.gserviceaccount.com`
with **no project roles and no keys**. It is admitted at the front door and
is the principal of its own service workspace. swarm-api is the only
principal that can sign as it, and only through `signJwt`. For an attempt
that named `qa_identity: "console"` in a granted tenant, swarm-api signs a JWT
`{iss, sub: the account, aud: "https://swarm.saga.xyz/*", iat, exp: iat+900,
jti}` and returns it to the worker, proven by the attempt key. The worker
hands it to the gate on `ctl.sock`. The gate intercepts, on its **own** CDP
sessions, the requests the console makes to its own host, and adds
`Proxy-Authorization: Bearer` with the JWT before they leave Chromium. IAP
verifies the JWT, checks the account's accessor role, and forwards
`x-goog-iap-jwt-assertion` naming the account. swarm-api authenticates it as
it does any IAP caller (`swarm_api.auth.IapAssertionVerifier`).

**How the token is minted.** IAM Credentials `signJwt` on the QA account,
called by swarm-api's runtime service account. That account holds a custom
role with the one permission `iam.serviceAccounts.signJwt`, bound **on the QA
account resource only**. This is narrower than `roles/iam.serviceAccountTokenCreator`,
which also carries `getAccessToken`, `getOpenIdToken` and `signBlob`. No key
file exists anywhere. The project should refuse key creation for this account
(`iam.disableServiceAccountKeyCreation`, the owner's org-policy check in
§4.9).

**How it is scoped to one host.** Two ways, and both must fail before
anything leaks:

1. *By its audience.* IAP accepts the JWT only for a resource matching
   `https://swarm.saga.xyz/*`. It is not an OAuth access token, so no Google
   API accepts it (M1 confirms both).
2. *By where the gate sends it.* The gate adds it only to a request whose URL
   is `https`, whose host equals the console host exactly (parsed, not
   pattern-matched), and whose initiating frame's top-level origin is that
   same host, or which is a top-level navigation to it (§4.4).

   An HTTP redirect is the case that rule alone does not cover, and it is
   **unmeasured** (M5). A header added by `Fetch.continueRequest` is written
   into the request Chromium's network service holds, and Chromium normally
   carries a request's headers onto the request a 3xx produces. Its
   cross-origin redirect rule strips `Authorization`, not other headers. The
   gate's `https://<host>/*` pattern does not match the redirected request,
   so without more the gate would never see it, and a 302 from the console
   host to another origin would deliver the JWT there. That origin could be
   an open redirect, IAP's own `/_gcp_iap/` redirect parameter, or an OAuth
   or setup hand-off such as the GitHub connect flow. So the gate does not
   rely on the redirect rule: it stops an off-host redirect from a request
   it added the header to, and it strips the header from every request whose
   host is not the console's (§4.4, two guards).

**Rotated.** Lifetime 15 minutes (Q5). The worker renews at 10 minutes, as it
renews the egress ticket. Nothing to revoke per token. Revoking the
**identity** means removing the tenant's grant (immediate at swarm-api, which
stops signing) or removing the account from `frontend_iap_members` (IAP
refuses it on its next IAM check). Disabling the account stops `signJwt`. A
JWT already issued remains valid until it expires, at most 15 minutes.

**Kept out of the page, the agent and artifacts.**

* *Page.* Script cannot read request headers the browser adds. The
  credential never enters a document, a cookie or storage. If IAP sets a
  cookie of its own, the gate's session export excludes the console host, so
  LB-7's carry and saved logins never store it (§4.4).
* *Agent.* The scrubber removes `Proxy-Authorization` from every header
  object in every event and result the gate forwards to the agent. It also
  replaces any occurrence of the JWT's exact bytes in any agent-bound frame
  with `[qa-identity]`, and records `qa_identity_scrubbed` with a count.
  While an identity is active, the agent's `Fetch.continueRequest` is refused
  if it carries `url`, or `headers` for a request to the console host. Either
  would let the agent send the added header somewhere else, or read it back.
  `Network.setExtraHTTPHeaders` is already refused.
* *Artifacts.* Screenshots and recordings are pixels; the credential is never
  drawn. A Playwright trace or HAR is built from CDP events the scrubber has
  already cleaned. Neither the worker nor the gate logs the credential: log
  lines carry codes and the `jti`, as the egress ticket's do.

**Threat model.**

* *A malicious page* cannot read the credential. It can try **cross-site
  request forgery**: a page on `evil.example` submits a form to
  `https://swarm.saga.xyz/v1/...`. If the gate added the header to that
  request, IAP would authenticate it, because an added header, unlike a
  cookie, has no SameSite. That is why the gate adds it only for frames whose
  top-level origin is the console (§4.4). As a second guard, swarm-api
  refuses a state-changing request from the QA identity whose `Origin` is not
  the console host (`qa_identity_origin`).
* *A compromised agent* can act as the QA account in the QA workspace, for
  as long as its attempt holds the lease. It can do whatever a member of that
  workspace can: read its tasks, and submit and cancel tasks there (Q3 bounds
  what that costs). It cannot reach another workspace, an admin screen or a
  person-only route. To take the credential out it would have to defeat the
  scrubber, the host match and the `Fetch` refusals together. Even then it
  would hold a JWT valid for 15 minutes or less, good only at this front
  door, only as the QA account.
* *Another tenant.* No tenant gains a grant on any GSA. A tenant whose admin
  grant is absent is refused at submission (`qa_identity_not_granted`) and
  again at signing. The shared QA workspace is the residual (§3.7).
* *swarm-api compromised.* It could sign as the QA account with any audience
  and lifetime: `signJwt` cannot be narrowed by IAM. It can already act as
  every tenant, so this adds little authority. It is recorded in §3.7 all the
  same.

**IAM rows.**

| member | role | on | why |
|---|---|---|---|
| `serviceAccount:qa-console@…` | `roles/iap.httpsResourceAccessor` | `swarm-backend` and `swarm-ui-backend` (one `frontend_iap_members` line, owner's bootstrap apply) | to pass IAP for the console's pages and its `/v1` calls |
| `serviceAccount:qa-console@…` | principal of service workspace `u-<digest>` | Admin › Tenants › New workspace › Service, approved by the owner (workspace-kinds Q2) | its tenant in swarm-api, with its own GSA, namespace and prefix |
| `serviceAccount:<swarm-api runtime SA>` | custom `swarmQaJwtSigner` = {`iam.serviceAccounts.signJwt`} | the `qa-console` service account resource only | swarm-api, and nothing else, signs the JWT |
| `serviceAccount:qa-console@…` | *nothing* | the project | no project role, no key, no `actAs` granted to anyone else |
| any `swarm-agent-worker-*` | *nothing new* | — | fact 1: a worker grant would be the agent's |

**Cost.** An estimated 1,000 lines of code and tests (not counted from a build) across six lanes (§6), one
frozen-contract line (QI-1), and five measurements (M1 to M5). **Migration:**
none, because nothing exists to migrate. **Latency:** one swarm-api call per
attempt plus one every 10 minutes, and one CDP round trip on each console
request, which the gate already handles for paused requests. **Money:** one
service account and one service workspace. That costs nothing unless an
account is lent to it (Q3).

**Covers** every console screen a workspace member sees, on real data and
the real API, through the real IAP, at any device preset and theme, with no
person. **Cannot cover:** person-only routes (fact 9), admin screens, Google's
sign-in page itself, and any IAP app other than this console. That last one
is deliberate: QI-1's `choices` is `("console",)`.

### 3.3 Option C: no sign-in, the fixture build served in the pod

**Shape.** The agent builds `apps/swarm-ui` and serves it in fixture mode
(`npm run dev`: "fixtures, no network"). It points its browser at
`http://127.0.0.1:<port>` in the same pod. The gate's Chromium shares the
pod's network namespace, and loopback is on the egress bypass (`<local>`).
#1038 and #1427 did this on plain `claude-code` with headless Playwright.
QA-FLOWS adds a page served over http, because `file://` is blocked.

**Threat model.** None new: no identity, no credential, no front door.

**IAM rows.** None.

**Cost.** Nothing to build for sign-in. The QA-KIT and QA-FLOWS lanes already
carry what it needs. **Covers** layout, responsiveness, theme and component
states for every screen the fixtures model, admin screens included. **Cannot
cover** real data, real API answers and errors, IAP, tenant resolution,
latency, or anything the fixtures do not model. It also cannot show a
deployed release's behaviour.

**Bootstrap.** None.

### 3.4 Option D (considered, turned down): admit the tenant's worker identity at the front door

Add each consuming tenant's `swarm-agent-worker-<t>` to `frontend_iap_members`
and let the agent's browser use the worker's own token. This is cheapest to
build and wrong on fact 1. The agent can mint that identity's tokens from the
metadata server itself, so the credential is in the agent's hands with no
gate in the way. And a worker identity's authority at swarm-api is the
worker's routes (`swarm_api.auth.is_worker_identity`), not a member's view.

### 3.5 Option E (considered, turned down): a second, IAP-free door for QA

Serve the console and API on an internal address that the pod network can
reach, with swarm-api accepting a platform-minted QA ticket instead of IAP.
This removes the outer gate for one class of caller, and adds a second front
door whose NetworkPolicy and ticket check must stay right forever. It is also
not the deployed path, which is the one thing B covers that C does not.

### 3.6 Recommendation: B, with C kept as the default for layout sweeps

B is the only option that is **scoped by construction**. Its credential
names one account, one audience and one host, and lasts 15 minutes. Its
account can reach one workspace. No person is in the loop after the owner's
one-time steps. A's account cannot be scoped at all: `domain:saga.xyz` admits
it to whatever the domain is admitted to. A's credential is a Google session
measured in days, and it fails LB-7's tenancy rule or forces a person to sign
in as the bot.

C costs nothing and should remain the first tool: most layout findings need
no real data. The QA-GUIDE lane should say so. B is for the pass that must
see the deployed console.

### 3.7 Residual risk of B, stated plainly

1. **The agent acts as the QA account for the attempt's life.** That is the
   feature, and it cannot be designed away. It is bounded by the QA
   workspace's own authority and pool ceiling (Q3).
2. **Every granted tenant shares one QA workspace.** A task one tenant's
   agent submits there is visible to another's. Grant only tenants that
   would accept that (Q2). Store nothing in that workspace: no secrets, no
   forge token, no provider key.
3. **The scrubber matches by header name and by value.** A CDP channel
   nobody anticipated that carries the header in another encoding (for
   example base64 inside a larger blob) would get past the value match.
   Exposure is bounded to 15 minutes, this host and the QA account.
4. **`signJwt` cannot be narrowed by IAM** to an audience or a lifetime.
   swarm-api's code is the only bound. The audit row per issue (`jti`,
   attempt, generation, expiry) makes any use outside that code visible.
5. **A redirect off the console host could carry the header** until M5 is
   measured and QI-GATE's two redirect guards (§4.4) are built and tested.
   If Chromium carries an added header onto a cross-origin redirect, any
   3xx from the console host to another origin (an open redirect, an IAP
   redirect parameter, the GitHub connect hand-off) would hand that origin
   the QA account's JWT, usable at this front door for up to 15 minutes.
   The guards close it by construction; M5 shows whether they had to, and
   that the strip guard actually removes a header Chromium carried.
6. **M1 may fail.** If IAP here refuses a service-account JWT, the fallback
   is an OAuth access token (Q6). It is not host-bound, and other Google APIs
   accept it within its scopes. Narrowing it to `userinfo.email` would also
   need measuring. The design does not silently fall back: Q6 asks.
7. **Platform admins** with project-level IAM can grant themselves `signJwt`
   on the account, as they can for every account in the project.
8. **A key can be made for the account.** The project does not enforce
   `iam.disableServiceAccountKeyCreation` (read 2026-10-20: `booleanPolicy:
   {}`), and enforcing it would bind the other team's accounts in this shared
   project too. So anyone holding `iam.serviceAccountKeys.create` here (Owner,
   Editor, Service Account Key Admin) could mint a long-lived key for
   `qa-console`, which would pass IAP as it with no lease and no gate. None
   exists, and none is declared. The owner accepted this as it is on
   2026-10-20, with no alert and no check added.

---

## 4. The recommended design, in enough detail to build

### 4.1 The catalogue entry and the configuration

* **Frozen catalogue (QI-1).** `claude-code-browser` declares
  `qa_identity: RunnerInput("string", choices=("console",), means=...)`. No
  other profile declares it. Only `claude-code-browser` has a live gate
  (`live_browser=True`), and B needs the gate.
* **swarm-api configuration** (`apps/swarm-api/swarm_api/settings.py`,
  rendered by `terraform/infra/locals.tf` beside `TENANT_GROUPS`):
  * `QA_IDENTITY_CONSOLE_ACCOUNT`: the account's email. Empty means the
    deployment has no QA identity, and every request naming one is refused
    `qa_identity_unconfigured` (503 at signing, 422 at submission).
  * The console host comes from the hostname swarm-api is already deployed
    behind (`frontend_hostname`). It is never a second, separately set
    value that could drift from the real one.
  * `QA_IDENTITY_LIFETIME_SECONDS`: default 900, clamped to 300–3600.

### 4.2 Firestore: swarm-api's own collections

`qa_identity_grants/{tenant_id}`, one document per consuming tenant:

```json
{
  "tenant_id": "eng",
  "identity": "console",
  "state": "granted",
  "granted_by": "<admin email>",
  "granted_at": "2026-10-20T09:00:00Z",
  "reason": "console visual QA (#1649)",
  "revoked_by": null,
  "revoked_at": null
}
```

`state` is `granted` or `revoked`. A revoke keeps the document. A re-grant
overwrites it, and `admin_audit` keeps the history. Each change writes an
`admin_audit` row (`qa_identity_granted` or `qa_identity_revoked`, with
`target` the tenant id and `detail` the identity name) **in the same
transaction**, as `swarm_api.admins` does.

Each credential issued appends a row to `live_audit`, in the shape of the
LB-7 rows ([live-browser.md](live-browser.md) §5.8):

```json
{
  "event": "qa_identity_issued",
  "tenant_id": "eng",
  "task_id": "task_…",
  "attempt_id": "att_…",
  "generation": 3,
  "identity": "console",
  "jti": "<random id, not a secret>",
  "expires_at": "2026-10-20T09:15:00Z",
  "at": "2026-10-20T09:00:00Z"
}
```

The credential itself is never written anywhere.

### 4.3 The API

**Submission** (`swarm_api.service`, beside `refuse_unless_personal`). A task,
batch task or workflow step whose input names `qa_identity` is refused with:

* `qa_identity_unconfigured` (422) when the deployment has no account;
* `qa_identity_not_granted` (403) when the submitting tenant has no `granted`
  document. The message names the rule and how to ask a platform admin.

The catalogue check (QI-1) refuses any value but `"console"` and any profile
but `claude-code-browser`.

**Worker route** (`swarm_api.qa_identity`, mounted beside the egress ticket's,
same attempt-key signature scheme under its own purpose `qa-identity`):

`POST /v1/attempts/{attempt_id}/qa-identity`

Request (signed over the raw body):

```json
{ "task_id": "task_…", "generation": 3, "identity": "console" }
```

Response 200, `Cache-Control: no-store`, never logged by swarm-api's access
log (the route is added to the same redaction list as the egress ticket's):

```json
{
  "identity": "console",
  "host": "swarm.saga.xyz",
  "header": "Proxy-Authorization",
  "scheme": "Bearer",
  "expires_in": 900,
  "jti": "<id>"
}
```

The answer also carries one more field, `credential`: the signed JWT
itself. It is left out of the example above so that no example in this
repository holds a value of a credential's shape.

Refusals, each a code with no free text: `bad_signature` (401), `not_found`
(404, also for another tenant's task), `stale_generation` (409),
`lease_not_live` (409), `qa_identity_not_requested` (422: the task's signed
input does not name it), `qa_identity_not_granted` (403: re-read at signing,
so a revoke takes effect on the next renewal), `qa_identity_unconfigured`
(503), `qa_identity_sign_failed` (503: IAM Credentials refused or did not
answer). Rate limit: 12 per attempt per hour. A renewal every 10 minutes
needs 6.

**Admin routes** (`require_admin`), in the same module:

* `GET /v1/admin/qa-identities` → `{"identities": [{"identity": "console",
  "host": "...", "account": "...", "configured": true, "grants":
  [{"tenant_id", "state", "granted_by", "granted_at", "reason"}]}]}`
* `PUT /v1/admin/qa-identities/console/grants/{tenant_id}` with
  `{"reason": "..."}` (1–200 characters) → the document. Refused for the QA
  workspace's own tenant (`qa_identity_self`), and for an unknown tenant.
* `DELETE /v1/admin/qa-identities/console/grants/{tenant_id}` → the document
  with `state: revoked`.

**Front-door guard** (`swarm_api.auth`). A request authenticated as the
configured QA account, with a method other than GET, HEAD or OPTIONS, whose
`Origin` is absent or is not `https://<console host>`, is refused 403
`qa_identity_origin` before any route runs. This is the second guard against
cross-site request forgery (§3.2), and it costs the console nothing: its own
fetches carry that origin.

**Member read of the tenant's own grant** (`swarm_api.routes.qa_identity`,
any workspace role). The submit form must know whether to offer the
option, and the admin `GET` is `require_admin`:

`GET /v1/qa-identities/mine` → `{"identities": [{"identity": "console",
"configured": true, "granted": true}]}` for the caller's current tenant.
No account, host, reason or other tenant's grant is in it.

**Standing rule: swarm-api never echoes a request header.** No route, error
page or debug answer reflects any request header's value, the
`Proxy-Authorization` or `Authorization` header least of all. If IAP
forwards the header to the backend (M2), a reflected value would reach
page script in a console frame, which the agent can drive with
`Runtime.evaluate` and re-encode past the CDP value scrub. M2's audit
checks today's routes once; this rule holds every later route to it, and
QI-API's tests assert it for the error handlers.

### 4.4 The live gate

New module `apps/agent-worker/agent_worker/livegate/qaidentity.py`:

* **`ctl.sock` verb `qa_identity`** (`livegate/ctl.py`): `{host, header,
  scheme, credential, expires_in}` sets or renews the credential, and
  `{"failed": cause}` clears it. Accepted only from the registered worker, as
  `egress_ticket` is. Refused once the attempt's `wipe` has run.
* **Injection on the gate's own sessions.** For every target the gate
  auto-attaches to (pages, out-of-process iframes, dedicated and service
  workers), the gate sends `Fetch.enable` with three patterns:
  `https://<host>/*` at the `Request` stage (injection),
  `https://<host>/*` at the `Response` stage (redirect guard 1), and `*` at
  the `Request` stage (redirect guard 2). On a `Request`-stage
  `Fetch.requestPaused` whose URL is on the console host:
  1. Parse the URL. Its scheme must be `https`, its host must equal `<host>`
     exactly, and it must have no userinfo.
  2. The request is a main-frame navigation (`resourceType == "Document"` in
     the top frame), **or** the gate's frame map (built from `Page.frameNavigated`
     on its own sessions) says the requesting frame's top-level origin is
     `https://<host>`.
  3. A credential is held and not expired.

  If all three hold, `Fetch.continueRequest` with the request's headers plus
  `<header>: <scheme> <credential>`. Otherwise `Fetch.continueRequest`
  unchanged. Never `failRequest`: a missing credential must look like being
  signed out, not like a broken site.

  A worker target has no frame, so rule 2 never holds for a request a
  dedicated or service worker makes. **Worker-originated console fetches are
  unauthenticated by design**, and fail closed: IAP answers them as it
  answers a signed-out caller. The console registers no service worker
  today; if it ever does, that is a change to this design, not a gap to
  widen rule 2 for.
* **Redirect guard 1, at the `Response` stage** (`https://<host>/*`). For a
  request the gate added the header to (it keeps the set of those
  `requestId`s), a 3xx whose `Location` resolves to any origin other than
  `https://<host>` is not followed: the gate answers it with
  `Fetch.fulfillRequest`, a 200 and a short gate-made page saying the
  redirect to `<origin>` was stopped because the browser holds the QA
  identity, and records `qa_identity_redirect_stopped` with the target
  origin and no value. A redirect that stays on `https://<host>` is
  continued (`Fetch.continueResponse`). This breaks flows that leave the
  console by an HTTP redirect, the GitHub connect hand-off among them,
  while an identity is held; that is the price of not sending the JWT
  there, and such flows are person-only or admin-only anyway (fact 9).
* **Redirect guard 2, at the `Request` stage** (`*`). On any paused request
  whose parsed host is not exactly `<host>`, if its `request.headers` holds
  `<header>` (case-insensitive), the gate continues it with that header
  removed and records `qa_identity_header_stripped`. Otherwise it continues
  it unchanged. This is the second guard, for a redirect guard 1 did not
  see (a response the `Response` stage was not offered, for example). It
  costs one gate round trip on every request the browser makes while an
  identity is held; with no identity held the gate does not enable the `*`
  pattern.

  Both guards are needed because both rest on unmeasured Chromium
  behaviour (M5): whether an added header survives a cross-origin
  redirect, and whether `continueRequest` with a header removed actually
  removes one Chromium carried. Redirects started by script or markup
  (`location.href`, `<meta http-equiv=refresh>`) are new navigations, so
  the `Request`-stage rules above already judge them.
* **Scrubbing** (`livegate/cdpproxy.py`, on the path every agent-bound
  message already takes, `_strip`'s neighbour). Remove any header named
  `proxy-authorization` (case-insensitive) from `request.headers`,
  `headers` and `associatedHeaders`-shaped objects in `Network.*` and
  `Fetch.*` events and results. Then replace every occurrence of the held
  credential's bytes in the serialised frame with `[qa-identity]`. Record
  `qa_identity_scrubbed` with a count, never a value. A frame larger than the
  existing message cap is scanned like any other.
* **Policy** (`livegate/cdppolicy.py`). While a credential is held, refuse the
  agent's `Fetch.continueRequest` if it carries `url`, or `headers` for a
  `requestId` the proxy saw paused on `<host>` (`qa_identity_redirect`,
  `qa_identity_headers`). The proxy keeps the requestId→host map it needs from
  the agent's own `Fetch.requestPaused` events.

  The agent also holds the rest of the `Fetch` domain
  (`cdppolicy.ALLOWED_METHODS`: `continueResponse`, `fulfillRequest`,
  `failRequest`, `continueWithAuth`, `getResponseBody`) and
  `Network.getResponseBody`. Why none of them exposes the added header:
  `continueResponse` and `fulfillRequest` change or replace the
  **response**; neither returns or re-sends the request's headers, and the
  agent cannot use them to follow a redirect somewhere else, because
  redirect guards 1 and 2 run on the gate's sessions whatever the agent
  answers on its own. `failRequest` sends nothing. `continueWithAuth`
  answers a 401 challenge with the agent's own credentials, not the gate's.
  The one way in is a **response body** that reflects the request: the
  body of an echo route or an error page that prints the header. That is
  why the value scrub runs over `getResponseBody` results too, decoding a
  `base64Encoded: true` body before matching (a body the agent receives
  base64-encoded would otherwise carry the JWT past a byte match), and why
  swarm-api keeps the standing rule in §4.3 never to echo a request header.
  While a credential is held, `Fetch.getResponseBody` and
  `Network.getResponseBody` for a console-host `requestId` whose body the
  gate cannot decode are refused (`qa_identity_body`).
* **Hand-off on the console host is refused** while the task named
  `qa_identity` (`handoff_refused`, cause `qa_identity_host`, Q7). A person
  would otherwise type their own sign-in into a page meant to run as the QA
  account. The agent would then carry on **as that person**, with all of
  their workspaces.
* **Session export** (`livegate/session.py`). Cookies and storage for
  `<host>` are excluded from every `session_export` (carry, login, park), so
  nothing IAP sets for the QA account can enter LB-7's storage.
* **`wipe`** clears the credential before closing Chromium.

### 4.5 The worker

New module `apps/agent-worker/agent_worker/qa_identity.py`, modelled on
`egress_ticket.py`:

* Runs in `handoff.HandoffPath.prepare`, after the gate registers and before
  the runner starts, only when the signed input names `qa_identity`.
* Asks swarm-api (`POST /v1/attempts/{attempt_id}/qa-identity`) with the
  attempt key, then hands the answer to the gate (`qa_identity`). It renews
  at two-thirds of `expires_in`. Transient failures retry with the egress
  ticket's backoff, until the credential in hand expires.
* **On a refusal** it tells the gate `{"failed": cause}` and writes the cause
  to the agent's notice (`handoff.EGRESS_NOTICE_NAME`'s sibling,
  `qa-identity.md`). The agent then sees a Google sign-in page and a plain
  sentence explaining why, rather than a mystery. The task is **not** failed:
  the agent can still report what it saw. A first-call refusal of
  `qa_identity_not_granted` or `_unconfigured` fails the attempt before the
  agent starts, because nothing it could do would be signed in.
* Never logs the credential. Log lines carry the code and the `jti`.

### 4.6 The console

* **Admin › Tenants**, a "QA identity" panel
  (`apps/swarm-ui/src/QaIdentityGrants.tsx`, new; mounted from
  `apps/swarm-ui/src/Activity.tsx`, where Admin › Tenants lives). It lists the
  identity, its host and account, `configured`, and each grant with Grant and
  Revoke, both requiring a reason. No nav section is added, so the issue
  forms' Where list is unchanged (`tests/unit/scripts/test_issue_forms.py`).
* **Task submit.** The input arrives through the catalogue's declaration, as
  `browser_tool` did. The form offers "Sign in to the console as the QA
  identity" only when the tenant is granted.
* **Task page.** A badge, "Signed in as QA identity (console)", on a task that
  named it, so a person watching knows whose session the browser holds.

### 4.7 Plugin and agent instructions

* **Plugin / MCP** (`apps/swarm-mcp/swarm_mcp/details.py`). The step details
  line gains `QA identity: console` beside `Browser tool:`. `swarm_run`
  passes `input` through, so it needs no new parameter.
* **Agent instructions.** One paragraph tells the agent that the console is
  already signed in, that it must not ask for a hand-off there, and what
  `qa-identity.md` means if it appears. That text lives where QA-KIT puts the
  device and theme commands (`images/agent-runtime-browser/swarm_browser.py`).
  So it is written **by QA-KIT's lane or after it merges**, never by two
  lanes at once (§6).

### 4.8 Infrastructure (Track C)

> **Amended 2026-10-20 (owner, #1649): the account is `qa-console`, with no
> `swarm-` prefix.** This design named it `swarm-qa-console`, and QI-IAM built
> `swarm-qa`. Both start with the platform's prefix, and the workspace flow
> refuses every such account (`WORKSPACE_SERVICE_PLATFORM`,
> `swarm_api.workspaces.PLATFORM_SA_PREFIX`), so O3 could not give it the
> service workspace §2.2 rests on. The owner chose to rename it rather than
> exempt it, so the platform-account rule stays absolute.
> `tests/unit/scripts/test_qa_identity_account_name.py` holds the name to the
> rule. Measured the same day, with the account still named `swarm-qa`: the
> project does not enforce `iam.disableServiceAccountKeyCreation`
> (`booleanPolicy: {}`), so O2's "confirm the constraint covers the account"
> does not hold here. The owner accepted that as a residual risk (§3.7).

`terraform/bootstrap/qa_identity.tf` (new), behind `enable_qa_identity`
(default false):

* `google_service_account.qa_console`, `qa-console`, with a description
  saying what it is for and that no key may be made. A service account takes
  no labels, so `managed-by=swarm-terraform` cannot be set on it. The lane
  confirms the type is on the destroy guard's unlabelable-type list in
  `scripts/lib/common.sh`, as the platform's other service accounts are.
* The custom role `swarmQaJwtSigner`, with exactly `iam.serviceAccounts.signJwt`,
  in `platform_roles.tf`'s custom-role set.
* `google_service_account_iam_member`, binding that role for swarm-api's
  runtime account **on the QA account only**.
* A precondition that `frontend_iap_members` contains
  `serviceAccount:<the QA account>` when enabled, in the pattern of
  `ci_fix.tf`'s check. Adding that line is the owner's apply.

`terraform/infra/locals.tf` renders `QA_IDENTITY_CONSOLE_ACCOUNT` for
swarm-api from a bootstrap output, or empty.

### 4.9 Measurements before the build (M1–M5) and the owner's steps

> **Amended 2026-10-20 (owner, #1649): M2 is not a gate before the build.**
> Nothing on swarm-api shows request headers, so whether IAP passes the
> gate's header through cannot be observed from outside before swarm-api
> logs it. QI-API was therefore built as if IAP does pass it through:
> beside an IAP assertion, swarm-api's front door
> (`apps/swarm-api/swarm_api/main.py::FrontDoor.__call__`) removes
> `Authorization`, `Proxy-Authorization` and `X-Serverless-Authorization`
> before any route runs (`apps/swarm-api/swarm_api/main.py::CREDENTIAL_HEADERS`),
> its error pages scrub those values, a request carrying an assertion is
> judged on the assertion alone
> (`apps/swarm-api/swarm_api/auth.py::Authenticator._authenticate`), and it
> logs `authorization_present` and `proxy_authorization_present`, each
> `true` or `false`, never a value. M2 is read from the
> `proxy_authorization_present` log line after QI-API deploys. So QI-API
> waited for M1, M3 and M4, and QI-GATE for M1, M3 and M5 (§6).
>
> **The M2 reading is still to come.** As of 2026-10-20,
> [qa-identity-measurements.md](qa-identity-measurements.md) ("M2: not
> measured before the build") records the decision and no reading. One
> thing to read with it: M3 measured that Chromium refuses to add
> `Proxy-Authorization`, so the gate sends `Authorization`
> (`apps/agent-worker/agent_worker/livegate/qaidentity.py::HEADER`). For
> the gate's own requests the line that answers M2 is therefore
> `authorization_present`, which the same log line carries;
> `proxy_authorization_present` answers it for the header this section
> first named. M3's follow-on, that swarm-api ignores an `Authorization`
> header when an assertion is present, is held by the code cited above, not
> yet by a reading.

No lane past QI-CONTRACT starts until M1 to M3 and M5 are recorded, dated, in
`docs/design/qa-identity-measurements.md` (new, §8). Each is run by the owner, or by a lane the owner authorises,
**with the QA account after step O2**:

* **M1.** A JWT signed by `signJwt` for the QA account, with
  `aud: https://swarm.saga.xyz/*` and a 15-minute `exp`, in
  `Proxy-Authorization: Bearer`, gets a 200 for `/` and for `/v1/tenants/me`.
  The same JWT with another audience gets a 401. The same JWT sent as a bearer to a
  Google Cloud API (Cloud Resource Manager's `projects.get`) gets a 401. **If the first fails,
  the build stops and Q6 is asked again.**
* **M2.** swarm-api, behind IAP, receives **no** `Proxy-Authorization`
  header. If IAP passes it through, the scrubber is extended to swarm-api's
  own error pages, and every route that echoes a request is audited.
* **M3.** Chromium (the image's Chrome for Testing build) sends
  `Proxy-Authorization` added by `Fetch.continueRequest` to an `https` origin
  that is not a proxy. If it drops the header, use `Authorization`. M2 then
  also has to show that swarm-api ignores an `Authorization` header when an
  IAP assertion is present.
* **M4.** The console loaded as the QA account resolves to its service
  workspace with no `X-Swarm-Tenant` header. `/v1/tenants/mine` lists it.
  Read from `swarm_api.auth.Authenticator._with_own_service_tenant`, a
  service tenant is **appended only when named**, so this may need the
  console to name it. That is QI-API's to decide from the measurement.
* **M5.** A console-host URL that answers 302 to another origin the lane
  controls (an `https` echo it runs), requested in the image's Chromium
  with the header added by `Fetch.continueRequest` and no guard enabled:
  does the second request carry it? The control is the same request with
  `Authorization` added instead, which Chromium's cross-origin rule should
  strip; if that one arrives too, the harness is wrong. Then, with redirect
  guard 2 alone enabled: does the echo still see the header? It must not.
  The result decides nothing about whether the guards are built (they are,
  either way); it records which one is load-bearing.

The owner's one-time steps, in order. No agent runs them:

* **O1.** Workspace kinds' own O1 and its bootstrap apply
  (`enable_workspace_kinds`), if not already done (fact 8).
* **O2.** `enable_qa_identity = true` and the `frontend_iap_members` line, in
  one bootstrap apply. Confirm `iam.disableServiceAccountKeyCreation` covers
  the account.
* **O3.** Admin › Tenants › New workspace › Service for the QA account,
  approved by the owner. Lend it no account (Q3 (a)).
* **O4.** Grant the first consuming tenant (Q2).

---

## 5. Frozen contract: change requests

One request. The operator files it in `docs/contract-change-requests.md` with
the next free number once the owner approves this design. It is written here
in that file's shape and not filed there.

> ### QI-1. `profiles.py`: `claude-code-browser` declares `qa_identity` (QA-IDENTITY, part of #1649)
>
> **Status:** REQUESTED by lane QA-IDENTITY's design, 2026-10-19. Not accepted.
>
> #### What is true today
>
> `_CLAUDE_CODE_BROWSER_INPUTS` is `_CLI_AGENT_INPUTS`, `_MCP_SERVER_INPUTS`
> and `browser_tool` (requests 77 and 88). A task that names any other key is
> refused `invalid_input` by `check_inputs`. No profile lets a caller say the
> browser should be signed in to anything.
>
> #### The change
>
> ```python
> _CLAUDE_CODE_BROWSER_INPUTS: dict[str, RunnerInput] = {
>     **_CLI_AGENT_INPUTS,
>     **_MCP_SERVER_INPUTS,   # contract request 88
>     "browser_tool": RunnerInput(...),   # unchanged
>     "qa_identity": RunnerInput(
>         "string",
>         choices=("console",),
>         means=(
>             "sign the live browser in to this deployment's console as its QA "
>             "identity, a service account with its own workspace; the platform "
>             "adds the credential, the agent never holds it, and a tenant may "
>             "name it only when a platform admin has granted it"
>         ),
>     ),
> }
> ```
>
> Nothing else in `apps/common/swarm_common/` changes. The signed spec already
> covers `input`, so the worker acts only on a value the submitter signed.
>
> **Tests:** `tests/unit/common/test_qa_identity_contract.py`: the key is
> declared on `claude-code-browser` with exactly the choice `console`. No
> other profile declares it. `check_inputs` refuses `"admin"`, `""` and a
> non-string.
>
> #### Rollback
>
> Delete the entry. A queued task that names it is then refused by the worker
> as an undeclared input, failing closed. Revoke every grant first so nothing
> new is submitted.
>
> #### What it would break
>
> Nothing that exists: the key was refused before. Pickers that render the
> catalogue's inputs (the console's Submit form, `swarm_profiles`) show one
> more option.
>
> #### Invariants
>
> **10**: a name from a closed list, never a credential, host or account.
> **9**: the name grants nothing by itself. swarm-api's per-tenant grant
> decides, at submission and again at signing. 1 to 8 are untouched.

Considered and **not** requested:

* **A `qa_identity` field on `Task` or `Attempt`.** The signed input already
  carries it, and the audit row carries the `jti`.
* **A `Tenant.kind = "qa"`.** The QA workspace is a service workspace, `kind = "user"`
  with a service-account principal, as workspace-kinds §5 keeps it.
* **Declaring `browser_login` on `claude-code-browser`** (§1.1) is option A's
  prerequisite, not B's. If the owner picks A, it is filed then. It is also
  reported as a finding on LB-7, in a PR comment, not here.

---

## 6. Build plan

> **Amended 2026-10-20 (owner, #1649): M2 is not a gate before the build**
> (§4.9). Row 0's measurements no longer all gate the build: QI-API waits for
> M1, M3 and M4, and QI-GATE waits for M1, M3 and M5. The order-2 rows'
> "after CONTRACT and M1–M4" and "after CONTRACT, M1–M3 and M5" are read
> that way.
> M2 is recorded in `docs/design/qa-identity-measurements.md` from swarm-api's
> `proxy_authorization_present` log line after QI-API deploys.

Lanes are split by territory: **no file appears in two rows**. Every lane
pushes its tests alone to the PR branch first, and its fix to `<branch>-fix`.
It runs its area's unit tests, then `scripts/changed-guards.sh` and
`scripts/publish-scan.sh`. Any fake JWT in a test is built at run time from
parts, never written as one literal.

**Security review is blocking** for QI-IAM, QI-API, QI-GATE and QI-WORKER
(credentials, IAM, tenant isolation, redaction).

| order | lane | files | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **M1–M5** (owner or an authorised lane, after O2) | `docs/design/qa-identity-measurements.md` (new) | none; each measurement dated, with the control that shows it could have come out the other way |
| 1 | **QI-CONTRACT** (after owner approval of QI-1) | `apps/common/swarm_common/profiles.py`, `docs/contract-change-requests.md`, `tests/unit/common/test_qa_identity_contract.py` (new), and any mirror `scripts/lib/check-contract-parity.sh` names | the three in §5's request; `check-contract-parity.sh` passes |
| 1 | **QI-IAM** (Track C) | `terraform/bootstrap/qa_identity.tf` (new), `terraform/bootstrap/platform_roles.tf`, `terraform/bootstrap/variables.tf`, `terraform/bootstrap/outputs.tf`, `tests/terraform/qa_identity.tftest.hcl` (new) | with `enable_qa_identity = false` nothing is planned; with it true: one service account and no key resource; `swarmQaJwtSigner` holds exactly `iam.serviceAccounts.signJwt`; its only binding is on the QA account, to swarm-api's account; no project-level binding names the QA account; the precondition fails when `frontend_iap_members` lacks it |
| 2 | **QI-API** (after CONTRACT and M1–M4) | `apps/swarm-api/swarm_api/qa_identity.py` (new), `apps/swarm-api/swarm_api/routes/qa_identity.py` (new), `apps/swarm-api/swarm_api/main.py`, `apps/swarm-api/swarm_api/settings.py`, `apps/swarm-api/swarm_api/service.py`, `apps/swarm-api/swarm_api/auth.py`, `apps/swarm-api/swarm_api/refusals.py`, `apps/swarm-api/swarm_api/roles.py` (the member `GET /v1/qa-identities/mine`'s role row), `terraform/infra/locals.tf`, `tests/unit/control_plane/test_qa_identity.py` (new), `tests/unit/control_plane/test_new_refusals_ship_off.py` (entries only) | an ungranted tenant's submission is 403 `qa_identity_not_granted`, and a revoked one likewise; signing for a stale generation is 409 and calls the signer fake zero times; a revoke between issue and renewal refuses the renewal; the signed payload's `aud` is exactly `https://<host>/*` and `exp − iat` is the configured lifetime, clamped; the response carries `no-store`, and the access-log fake never sees the credential; grant and its `admin_audit` row commit together (an injected failure leaves neither); a POST as the QA account with `Origin: https://evil.example` is 403 `qa_identity_origin` and with the console origin passes; `GET /v1/qa-identities/mine` as a non-admin member answers its own tenant's `granted` and no other tenant's; no error handler's body contains a request header's value |
| 2 | **QI-GATE** (after CONTRACT, M1–M3 and M5) | `apps/agent-worker/agent_worker/livegate/qaidentity.py` (new), `livegate/cdpproxy.py`, `livegate/cdppolicy.py`, `livegate/ctl.py`, `livegate/session.py`, `livegate/__main__.py`, `tests/unit/worker/test_livegate_qa_identity.py` (new) | with a fake Chromium: a console-host request from a console frame gets the header and one from an `evil.example` frame does not; `https://swarm.saga.xyz.evil.example/` and `http://swarm.saga.xyz/` do not; an agent-bound `Network.requestWillBeSentExtraInfo` carrying the header arrives without it, and a frame carrying the credential's bytes anywhere arrives with `[qa-identity]`; the agent's `Fetch.continueRequest` with `url` is refused; a console-host request the gate added the header to that answers 302 to `https://evil.example/` is fulfilled with the gate's stopped-redirect page and the fake never sees a request to `evil.example`, while a 302 to another console path is continued; a request to `evil.example` arriving with the header in `request.headers` is continued without it; a request from a dedicated-worker target gets no header; a base64-encoded `getResponseBody` result containing the credential's bytes arrives with `[qa-identity]`; a hand-off on the console host is refused with `qa_identity_host`; `session_export` omits the host's cookies; after `wipe` the verb is refused |
| 3 | **QI-WORKER** (after GATE and API) | `apps/agent-worker/agent_worker/qa_identity.py` (new), `apps/agent-worker/agent_worker/handoff.py`, `tests/unit/worker/test_qa_identity_worker.py` (new) | no `qa_identity` input means no call is made; a first refusal `qa_identity_not_granted` fails the attempt before the runner starts; renewal happens at two-thirds of the life; a refusal at renewal sends `failed` to the gate and writes `qa-identity.md`; no log record contains the credential (captured-log scan) |
| 3 | **QI-UI** (after API) | `apps/swarm-ui/src/QaIdentityGrants.tsx` (new), `apps/swarm-ui/src/Activity.tsx`, `apps/swarm-ui/src/Submit.tsx` (the option, beside `BrowserToolPicker`), `apps/swarm-ui/src/BrowserBadges.tsx` (the badge, drawn by `LiveBrowser.tsx`'s existing `<BrowserBadges>`), `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/__tests__/qa-identity.test.tsx` (new) | a non-admin sees no panel; Grant without a reason is disabled; a 403 renders the API's message word for word; the submit form offers the option only when `GET /v1/qa-identities/mine` says `granted`; the task-page badge shows only for a task that named the identity |
| 3 | **QI-MCP** (after CONTRACT) | `apps/swarm-mcp/swarm_mcp/details.py`, `tests/unit/mcp/test_qa_identity_details.py` (new) | a step naming `qa_identity` shows `QA identity: console`; one without does not |
| 4 | **QI-INSTRUCT** (after QA-KIT merges; or folded into QA-KIT) | `images/agent-runtime-browser/swarm_browser.py` | the instruction text names the console as signed in and forbids a hand-off there; QA-KIT's own tests still pass |
| 5 | **QI-DOCS** (after WORKER) | `docs/design/live-browser.md` (a §5.5.1 note on the excluded host), `docs/workspaces.md` (the QA workspace) | `test_docs_describe_what_was_built` where it reads these files |
| 6 | **QI-ACCEPT** (after O3, O4 and every lane) | `tests/acceptance/test_live_browser.py` (new cases only) | on dev: a granted tenant's `claude-code-browser` task with `qa_identity: "console"` screenshots the Overview showing the QA workspace; the same task from an ungranted tenant is refused at submission; the attempt's events, logs and artifacts contain no header named `Proxy-Authorization`, and its `qa_identity_scrubbed` records carry counts only; a navigation to a console-host URL that 302s to an external echo the test controls ends on the gate's stopped-redirect page, and the echo records no request carrying the header |

A release that ships QI-API runs `make smoke` against dev in CI. It changes
authentication, not admission, so `concurrency-test` and `race-test` are not
required by this design.

---

## 7. Owner questions

**Answered by the owner, 2026-10-19 ~22:30Z (Claude Code session f050e91d):** Q1 (a), option B: the QA service account with a gate-added IAP JWT, and C stays the default for layout sweeps. Q2 through Q8 are (a) each, the recommendations:

- Q2: eng only, granted by a platform admin with a reason.
- Q3: member rights, with no account lent.
- Q4: a service workspace through Admin › Tenants once workspace kinds is on.
- Q5: 15-minute tokens, renewed at 10.
- Q6: if M1 shows IAP refuses the JWT, stop and ask.
- Q7: no hand-off on the console host.
- Q8: dev first.

QI-1 is accepted with option B and is filed at the next free number by lane QI-CONTRACT.

Also in this lane's `questions.json`. The recommended option is listed first.

**Q1. Which identity option is built?**
* **(a) B, the QA service account with a gate-added IAP JWT; C stays the
  default for layout sweeps (recommended).** Scoped to one host, one account
  and one workspace. No person in the loop after setup.
* (b) C only. Nothing is built for sign-in, and the deployed console is never
  swept by an agent.
* (c) A, a QA Google account with a saved login. Needs LB-7 lifted for a
  shared account, a Workspace licence, and a measurement that Google keeps
  the session across pods.

**Q2. Which tenants may name the QA identity?**
* **(a) Per tenant, granted by a platform admin with a reason, starting with
  `eng` only (recommended).** Every granted tenant shares the QA workspace
  (§3.7).
* (b) Every tenant, with no grant.
* (c) A fixed list in Terraform. Terraform cannot name job-made tenants, the
  same lesson as lane L8c.

**Q3. What may the QA identity do inside its own workspace?**
* **(a) Act as a member, with no account lent to the workspace
  (recommended).** Submit and cancel flows can be exercised, and what it
  submits stays QUEUED at no cost (invariant 1).
* (b) Act as a member, with a small account lent and a pool ceiling of 1, so
  flows that need a task to run are covered. Costs money per run.
* (c) Viewer only. This needs workspace roles on service workspaces, which
  workspace-roles does not define today, and it leaves every write flow
  uncovered.

**Q4. Where does the QA workspace come from?**
* **(a) A service workspace via Admin › Tenants › New workspace, after
  workspace kinds is switched on (recommended).** It follows workspace-kinds
  Q8 (a): new tenants are job-made.
* (b) A fifth Terraform tenant. This reverses WK-TF-FREEZE.

**Q5. How long does each credential live?**
* **(a) 15 minutes, renewed at 10 (recommended).**
* (b) 60 minutes, renewed at 40. Fewer calls, four times the exposure.
* (c) 5 minutes, renewed at 3. Smallest exposure, 3× the signing calls.

**Q6. If M1 shows this front door refuses a service-account JWT, what then?**
* **(a) Stop and come back to the owner with the measurement
  (recommended).**
* (b) Fall back to an OAuth access token for the account, scoped to
  `userinfo.email` if IAP accepts that scope (also to be measured). It is not
  bound to the host.
* (c) Fall back to a `cloud-platform`-scoped access token, as swarm-verify
  uses. This is not recommended: with that scope, any Google API that grants
  the account anything accepts the token.

**Q7. May the agent ask for a hand-off on the console while it holds the QA
identity?**
* **(a) No: refused with `qa_identity_host` (recommended).** A person's own
  sign-in in that browser would hand the agent the person's workspaces.
* (b) Yes, as on any other site.

**Q8. Which environments?**
* **(a) dev first. Prod gets its own QA account and workspace only after
  QI-ACCEPT is green on dev and a live browser exists in prod
  (recommended).** The issue notes there is none yet.
* (b) dev and prod together.

---

## 8. Measurements and what was built

Recorded 2026-10-20 by lane QI-DOCS, read from `main` at `9f945b0`. M1, M3,
M4 and M5 are in
[qa-identity-measurements.md](qa-identity-measurements.md), each with a
date, the command, its result and the control: M1 passes, M3 found that
Chromium refuses `Proxy-Authorization`, M4 passed in part (the account
resolves to its workspace, which `GET /v1/tenants/mine` did not list) and M5
found an added header follows no redirect. M2 is still to come (§4.9). What
each lane of §6 landed, in its order:

* **M1–M5 (measured 2026-10-20, M2 still to come).** See above and §4.9.
* **QI-CONTRACT (built).** Contract request 106 in
  `docs/contract-change-requests.md`: `claude-code-browser` declares
  `qa_identity` with the one choice `console`, and no other profile
  declares it.
* **QI-IAM (built, and applied by the owner).**
  `terraform/bootstrap/qa_identity.tf` behind `enable_qa_identity`. The
  account is `qa-console`, not `swarm-qa` (the §4.8 amendment), and the
  owner's applies added `swarmQaJwtSigner` for swarm-api alone and, later,
  the front-door read `swarmFrontDoorReader` for the workspace job on the
  API backend, which let O3's workspace reach `ready`. The project does not
  enforce `iam.disableServiceAccountKeyCreation`; the owner accepted that
  as a residual risk (§3.7).
* **QI-API (built).** `apps/swarm-api/swarm_api/qa_identity.py`: the grants
  in `qa_identity_grants`, each change with its `admin_audit` row in one
  transaction, read at submission and again at every signing; the signing
  route, fenced like the egress ticket and held to 12 credentials an
  attempt an hour. Where it departs from or adds to the design:
  - swarm-api tells the gate to send `Authorization`, because M3 measured
    Chromium refusing `Proxy-Authorization`.
  - Beside an IAP assertion, the front door removes the credential headers
    before any route runs
    (`apps/swarm-api/swarm_api/main.py::CREDENTIAL_HEADERS`), rather than
    only scrubbing error pages: M2 is unmeasured, so it is built as if IAP
    passes them through (§4.9).
  - M4's empty list is fixed by listing a service account's own tenant
    when it is already the default
    (`apps/swarm-api/swarm_api/auth.py::Authenticator._list_own_service_default`).
    It selects nothing new.
  - The QA workspace's own tenant cannot be granted its own identity
    (`qa_identity_self`).
  - The account is rendered for dev only (`terraform/infra/locals.tf`,
    `local.qa_identity_console_account`), which is Q8 (a).
* **QI-GATE (built).** `apps/agent-worker/agent_worker/livegate/qaidentity.py`.
  Where it departs from §4.4:
  - The header is `Authorization`
    (`apps/agent-worker/agent_worker/livegate/qaidentity.py::HEADER`):
    Chromium refuses `Fetch.continueRequest` that adds
    `Proxy-Authorization` (M3).
  - Redirect guard 2 strips a header from another host's request by its
    value, when the value carries a credential the gate held, not by name
    (`apps/agent-worker/agent_worker/livegate/qaidentity.py::QaInjector`).
    Stripping every `Authorization` header from every other site would
    break any site's own API sign-in for the whole attempt.
    `proxy-authorization` is still removed by name from what the agent
    reads.
  - Both redirect guards are built although M5 found neither one
    load-bearing on this Chromium.
  - The hand-off on the console host is refused (`qa_identity_host`), and
    the session carry leaves the host out ([live-browser.md](live-browser.md)
    §5.5.1).
* **QI-WORKER (built).** `apps/agent-worker/agent_worker/qa_identity.py`:
  renewed with a third of the credential's life left
  (`apps/agent-worker/agent_worker/qa_identity.py::RENEW_FRACTION`), so a
  15-minute credential at 10 minutes (Q5 (a)). A first refusal of
  `qa_identity_not_granted` or `qa_identity_unconfigured` fails the attempt
  before the agent starts; a later refusal tells the gate and writes
  `qa-identity.md` for the agent.
* **QI-UI (built).** Admin › Tenants › QA identity
  (`apps/swarm-ui/src/QaIdentityGrants.tsx`), the submit form's option when
  `GET /v1/qa-identities/mine` says `granted`, and the task page's
  "Signed in as QA identity" badge (`apps/swarm-ui/src/BrowserBadges.tsx`).
* **QI-MCP (built).** A step that names the identity shows
  `QA identity: console` (`apps/swarm-mcp/swarm_mcp/details.py`).
* **QI-INSTRUCT (not built on 2026-10-20).**
  `images/agent-runtime-browser/swarm_browser.py` does not yet tell the
  agent that the console is signed in, that it must not ask for a hand-off
  there, or what `qa-identity.md` means. Until it does, an agent that asks
  is refused `qa_identity_host` by the gate.
* **QI-DOCS (built, this record).** [live-browser.md](live-browser.md)
  §5.5.1 notes the excluded host, and [workspaces.md](../workspaces.md)
  §6.6 records the QA workspace.
* **QI-ACCEPT (not built on 2026-10-20).** `tests/acceptance/test_live_browser.py`
  has no QA identity case yet. No run as the QA account has been recorded,
  so nothing above has been seen working end to end on dev.
