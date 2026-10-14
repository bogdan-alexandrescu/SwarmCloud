# Browser egress: routing the live browser through a residential or ISP proxy

**Status: DESIGN, for the owner to approve. Nothing here is built.** No code,
no Terraform, no vendor account. Lane EGRESS-PROXY, owner decision 2026-10-13
(~20:40 PDT), part of #1030.

## 0. Why this exists

The live-browser analysis of 26 browser tasks (the operator's
`scratchpad/lb-analysis/ANALYSIS.md`, on the laptop, not in this repository)
found that **Google answered 429 and redirected to `/sorry/` on 3 of 3 real
searches**. The likely reason is that every SwarmCloud agent leaves through
one address. `swarm-nat` is `MANUAL_ONLY` with a single address,
`swarm-nat-ip-0` (`terraform/modules/network/main.tf`,
`google_compute_router_nat.this`; measured in
[incidents/2026-10-09-egress-open-delay.md](incidents/2026-10-09-egress-open-delay.md),
reading 2). That one Google Cloud datacenter address carries every tenant's
clones, API calls and browser pages. To Google's abuse heuristics it looks like
one very busy bot on a cloud ASN, and a person's sign-in during a hand-off
comes from the same address.

The owner chose **residential/proxy egress** for the browser. This document
proposes how to do it, what it costs, what it exposes, and how to show it
worked. It leaves the choices that are the owner's to the owner (section 7).

**A sample of 3 is too small to measure a fix with.** It shows the problem
exists, but it cannot show how often it happens. Section 6 sets the
measurement that has to come before and after.

---

## 1. Vendors and proxy types

### 1.1 The three proxy types, and which ones can solve this problem

| Type | What the site sees | Billing | Sticky for a sign-in | Fit here |
|---|---|---|---|---|
| **Datacenter** | a hosting-provider ASN, the same class as `swarm-nat-ip-0` | per IP or per GB, cheapest | yes (the IP is fixed) | **No.** Google rate-limits datacenter ranges the same way it rate-limits ours. This would move the block to a new address and leave it in place. |
| **Rotating residential** | a consumer ISP address (a real household's connection, through the vendor's peer SDK) | **per GB** | a session id in the username holds one peer for a bounded time, and the peer can go offline at any moment | **Yes.** It has the best reputation, but billing is metered and a sign-in can break if the peer drops. |
| **ISP / static residential** | a consumer-ISP-registered address hosted in a datacenter | **per IP per month**, bandwidth often unmetered | **yes, for as long as you rent it** | **Yes.** Cost is fixed and the IP stays stable through a sign-in. Its reputation is lower than true residential, and a small pool used heavily can be rate-limited the way our NAT IP is now. |
| Mobile (for completeness) | a carrier CGNAT address | per GB, the most expensive | short | Not proposed. Its reputation is highest, but at roughly twice the residential price nothing here needs it. |

### 1.2 Vendors

Prices were **read 2026-10-14**, mostly from third-party comparison pages that
cite vendor list prices (sources at the end). Vendors change prices and run
promotions monthly, so **confirm on the vendor's own pricing page before
signing up**. The arithmetic in section 5 is written so that a different
price can be substituted directly.

| Vendor | Rotating residential | ISP / static | Sticky session | Country targeting | Compliance / KYC |
|---|---|---|---|---|---|
| **Bright Data** | list ~$8/GB, PAYG promo ~$4/GB, committed tiers ~$3.50 → $2.50/GB | ~$1.30-$3.00 per IP/month | session id in the username; a peer is held while in use | country, state, city, ASN | **Human-reviewed KYC, company only, no personal email**, for every Residential zone created after 2026-07-07. Requests outside the approved use case get `Access denied` with the category. ISP proxies are the documented no-KYC alternative. |
| **Oxylabs** | ~$6/GB entry ($30/month minimum), ~$4/GB at volume | sold separately (per IP) | `sessid` + `sesstime`, **up to 30 minutes** | country, state, city | KYC for some targets. Residential IPs are described as opt-in sourced. |
| **Decodo** (was Smartproxy) | PAYG ~$4/GB, plans $3.75 → $2.00/GB (3 GB → 1 TB) | sold separately (per IP) | **up to 24 hours** | country, state, city | Lighter onboarding. It has a **sub-user API with per-sub-user traffic limits and auto-disable** (section 3.2 uses that). It also supports IP-allowlist auth, which we must not use (section 2.6). |
| **IPRoyal** | $7/GB at 1 GB down to $1.75/GB in bulk. **Traffic never expires** | **from ~$2.40-$2.70 per IP/month** (90/30-day terms) | session id + lifetime in the password | country, state, city | Lighter onboarding. Non-expiring traffic suits a pilot whose volume is not known yet. |

**What every vendor must offer, whichever is chosen.** Each item is checked
on the vendor's own documentation before signing:

1. **User/password auth with a session id in the username or password.** This
   is how a task keeps one IP (section 2.4).
2. **Sub-users, or zones, with a traffic cap and auto-disable.** This is the
   budget cap and the outer kill switch (sections 3.5, 5).
3. **A TLS-wrapped proxy endpoint (`https://` proxy), or a documented reason
   it is not needed.** Most vendors' default endpoint is plain HTTP on a high
   port. On that endpoint the `Proxy-Authorization` header, which carries our
   credential, crosses the internet in clear text. Page content stays
   encrypted either way (section 3.2).
4. **Acceptable use that covers "an AI agent browsing on a user's behalf,
   including search and signing in to the user's own accounts."** Section 3.4
   explains why this needs a written answer and why a dashboard checkbox is
   not enough.

### 1.3 Expected GB per browser task

No per-task byte count exists yet, because nothing meters the browser's
traffic today. Section 6 adds that meter. Until then, here is an estimate from
published page weights.

**Inputs.** The HTTP Archive Web Almanac 2025 median home page is **2.86 MB on
desktop** and 2.56 MB on mobile. The live browser is a desktop preset by
default (`livegate/presets.py`), so use **W ≈ 2.9 MB** per full page load. The
browser profile is a fresh memory volume on each attempt (live-browser.md
§5.5), so the first load of every site is uncached. A repeat load on the same
site re-fetches only part of the page, so take a cache factor **c ≈ 0.6**
(40% served from cache) across a task. Add **10%** for TLS handshakes, HTTP
headers and CONNECT overhead.

```
GB per task = P (page loads) × W × c × 1.10 / 1000

light   (a search, 3-5 results opened)       P =  6, c = 1.0 :  6 × 2.9 × 1.0 × 1.1 =  19 MB ≈ 0.02 GB
typical (search, research, a form, a check) P = 25, c = 0.6 : 25 × 2.9 × 0.6 × 1.1 =  48 MB ≈ 0.05 GB
heavy   (QA of an SPA or a news site, 4 MB)  P = 80, c = 0.5 : 80 × 4.0 × 0.5 × 1.1 = 176 MB ≈ 0.18 GB
```

**Planning figure: 0.10 GB per task**, which is twice the typical case and
allows for the background traffic below. **Ceiling: 0.25 GB** for a
heavy-QA mix. One video page can exceed every number here, which is why
section 3.5 adds a per-attempt byte cap.

**Traffic that is not the agent's.** Chromium makes its own requests:
component updates, Safe Browsing lists, the optimization guide. On a fresh
profile these can reach tens of MB per attempt. Section 2.3 sends them direct
instead of paying per GB for them. The live view itself (the RFB stream to
`swarm-live`) is the gate's own connection, not Chromium's, so it never
crosses the proxy and is never billed by the vendor.

---

## 2. Architecture

### 2.1 What is routed, and what is not

**Only the live browser's Chromium is routed.** That is the headed Chromium
the gate launches in the `display` sidecar
(`apps/agent-worker/agent_worker/livegate/__main__.py::chromium_args`). These
are **not** routed:

* the agent's own traffic: git, `api.anthropic.com`, package installs, `curl`.
  It leaves the `worker` container, and the worker's environment is unchanged;
* the worker's traffic (Firestore, GCS, Secret Manager, swarm-api);
* the gate's own connections: the tunnel to `swarm-live`, and the session
  carry;
* every other profile, including the headless `browser` runner
  (`runners/browser.py`), which launches its own Playwright Chromium and is not
  the live browser;
* the sites on the bypass list (section 2.3).

### 2.2 Recommended shape: a platform forwarder holds the credential, and the pod never sees it

```
 tenant pod (swarm-tenant-<t>)                                    swarm-system                      internet
 +-------------------------------------------------------+
 | worker container           | display sidecar          |
 |  agent (no proxy, no cred) |  Chromium                |
 |                            |   --proxy-server=        |
 |                            |     http://127.0.0.1:P   |
 |                            |   --proxy-bypass-list=.. |
 |                            |      |                   |
 |                            |  gate: local forwarder   |   CONNECT host:443          +-------------+   CONNECT, vendor     +--------+
 |                            |   accepts only uid       |   + attempt egress ticket   | swarm-egress|   credential +        | vendor |--> site
 |                            |   swarm-display ---------+---- (TCP, in VPC) --------->| (holds the  |---- session id ------>| proxy  |
 |                            |   adds the ticket        |                             |  credential)|   (TLS to vendor)     +--------+
 +-------------------------------------------------------+                             +-------------+
```

There are three hops, and each one exists for a reason:

1. **Chromium → the gate's local forwarder on `127.0.0.1:P`.** Chromium's
   `--proxy-server` flag cannot carry credentials. Without this hop, the
   browser would have to answer a 407 challenge through CDP
   (`Fetch.authRequired`), which section 2.6 rejects. The forwarder is a
   small CONNECT-only server inside the gate process, which already holds
   Chromium, so it adds no new container.
2. **The forwarder accepts only connections from the sidecar's uid.** The
   agent shares the pod's network namespace
   (`kubernetes/network-policies/allow-egress.yaml`, "WHAT RULE 2 COSTS"), so
   it can connect to `127.0.0.1:P` as well. On loopback, the forwarder reads
   the peer socket's owner from `/proc/net/tcp`, which lists every socket in
   the namespace with its uid. It serves only `swarm-display`'s uid and
   refuses everything else, recording `egress_refused` with cause `peer_uid`.
   Without that check the agent could send all of its own traffic out through
   our paid residential pool. That costs money, and it would let a prompt
   injection put abusive traffic on our vendor account.
3. **The forwarder → `swarm-egress` in `swarm-system`.** The forwarder adds
   an **attempt egress ticket** to each CONNECT. swarm-api mints it, it names
   `tenant_id`, `task_id`, `attempt_id`, `generation` and an expiry, and it is
   MAC'd under a new platform key, the same way the tunnel ticket is MAC'd
   under `swarm-live-key` (live-browser.md §3.3). `swarm-egress` checks the
   ticket. It then opens the CONNECT to the vendor, putting **the vendor
   credential and the sticky session id** (section 2.4) into
   `Proxy-Authorization`, and from there it only relays bytes.

**Why the credential stays out of the pod.** Two facts in this repository
decide it:

* **The pod's identity is the agent's identity.** Every process in the pod can
  mint the tenant GSA's token from the metadata server (allow-egress.yaml,
  "WHAT RULE 2 COSTS"). So a Secret Manager grant to the tenant GSA would hand
  the credential to the agent. The gate must not read the credential with the
  pod's identity.
* **Chromium runs `--no-sandbox` as the same uid as the gate**
  (`chromium_args`; the sidecar drops the capabilities the sandbox needs). A
  renderer exploit from a hostile page runs as `swarm-display` and can read
  the gate's memory. If the vendor credential were there, one malicious page
  could take a credential that works for every tenant until someone rotates
  it. With the forwarder design, the same exploit gets an attempt ticket that
  works for one attempt, until its expiry, and only from inside the VPC.

**The alternative, for the owner to weigh (section 7, Q4).** swarm-api could
hand the vendor credential itself to the gate over the worker's MAC'd
`ctl.sock` channel, the way the tunnel ticket travels today
(`handoff.py`, `_open_tunnel`). The gate would then dial the vendor directly.
That design has one less service, but the vendor credential lives in every
browser pod's sidecar memory, within reach of the renderer exploit above. It
also spreads metering and the kill switch across every pod instead of one
place. It is cheaper to build and weaker, and this document recommends
against it.

### 2.3 The bypass list

Chromium is launched with `--proxy-bypass-list` (semicolon-separated). These
hosts go **direct**, through `swarm-nat`, as they do today:

| Bypass | Why |
|---|---|
| `<local>`, and loopback (implicit) | cluster-internal names. The managed `URLBlocklist` already refuses them (`images/agent-runtime-browser/chromium-policy.json`), and the vendor must never see an internal hostname. |
| SwarmCloud's own hosts: the console and swarm-api | IAP and our own logs should see our NAT address, and we should not pay per GB to load our own UI. **Rendered from configuration, never hard-coded.** The dispatcher passes the hosts it already knows. |
| `github.com`, `*.github.com`, `*.githubusercontent.com` | GitHub does not block `swarm-nat-ip-0` (the agent's git traffic uses it every day). A person signing in to GitHub in a hand-off should come from the same address the agent's git calls use, and repository pages are heavy. |
| Chromium's own background services (component updater, Safe Browsing list updates, optimization guide) | Section 1.3: tens of MB per fresh profile, of no value to the site. The exact host list is **measured in phase 0** (section 6), not guessed. Alternatively, switch those services off by enterprise policy where a policy exists. |

**The agent cannot change any of this.** Chromium's proxy settings come from
flags the gate sets. The CDP method policy already refuses
`Target.createBrowserContext` with `proxyServer` or `proxyBypassList`
(live-browser.md §5.4.4). A context created without those parameters (the
agent's, or the gate's own "fresh page" context of §5.4.3) inherits the
browser's proxy settings, so a sign-in in a fresh context still goes out
through the same sticky IP. **To verify in the build:** that inheritance is
how Chromium behaves today, but the build must prove it in a test rather than
assume it.

**Leaks closed by Chromium policy.** These are added to the managed policy in
proxied pods only:

* `WebRtcIPHandling: "disable_non_proxied_udp"`. WebRTC's STUN would
  otherwise reveal `swarm-nat-ip-0` around the proxy, and Google reads it.
* `HttpsOnlyMode: "force_enabled"`. A plain-`http:` page through a
  residential peer is readable by the peer and by the vendor (section 3.2).
* DNS needs no setting. With an HTTP proxy, Chromium sends the hostname in the
  CONNECT and does not resolve it itself, so the name is resolved where the
  IP is and does not leak through cluster DNS. QUIC is not used through an
  HTTP proxy, so nothing goes around it over UDP.

The policy names above are from the Chrome Enterprise policy list. The build
lane checks them against the Chromium version in the image, which
`selftest.py` already checks policies against.

### 2.4 Sticky sessions: one IP for an attempt, and for a sign-in

Vendors select a sticky peer by a session id embedded in the username or
password. The exact syntax is vendor-specific, and `swarm-egress` renders it
from one template per vendor. It is never built in the pod.

* **Session id = a hash of `(tenant_id, task_id, attempt_id)`, truncated.**
  It is opaque to the vendor, so no tenant or task name leaves the platform.
  It is stable for the whole attempt, which covers the sign-in, the agent's
  use of the session afterwards, and the "fresh page" context.
* **The sticky lifetime is set to the vendor's maximum.** For Oxylabs that is
  30 minutes, which is shorter than many attempts. If Oxylabs is chosen, an
  attempt longer than 30 minutes changes IP mid-attempt, and a site may sign
  the person out. Decodo allows 24 hours. ISP proxies have no limit.
* **Across a park.** A hand-off that times out parks and resumes as a new
  attempt (live-browser.md §4.3), often hours later. A rotating-residential
  peer will be gone by then whatever id is used. **With ISP proxies, map
  `(tenant_id, task_id)` to one IP of the pool** (a deterministic hash), so a
  resumed attempt, and a personal tenant's carried session (§5.5.1), returns
  from the address the cookies were issued to. This is the strongest argument
  for ISP proxies for sign-in work (section 7, Q2).
* **A peer that drops mid-attempt** (rotating residential only) moves the
  vendor to a new IP. The forwarder cannot prevent it. It can only count it:
  `swarm-egress` records the vendor's peer change where the vendor reports it.
  Sites usually take this as a new location, not as a reason to sign the
  person out. It is measured in phase 1.
* **Two tenants never share a session id**, because the id includes
  `tenant_id`.

### 2.5 What changes in the pod, the dispatcher and the network

* **Dispatcher** (`apps/scheduler/scheduler/dispatch.py`). For a live-browser
  pod whose tenant and profile are switched on (section 6.1), it adds
  `SWARM_BROWSER_EGRESS=proxy` and the bypass hosts to the `display`
  container's environment, and a pod label `swarm-egress: "proxy"`, next to
  `LIVE_POD_LABEL`. The decision is made **once, at dispatch, per attempt**.
  An attempt's route never changes while it runs.
* **Gate.** When `SWARM_BROWSER_EGRESS=proxy` is set, `chromium_args` adds
  `--proxy-server=http://127.0.0.1:<P>` and the bypass list. The local
  forwarder starts before Chromium. Until the worker hands over the egress
  ticket on a new `ctl.sock` operation (`egress_ticket`, next to
  `tunnel_ticket` in `ctl.py::OPS`), it **refuses every CONNECT**. The worker
  is up before the agent exists, so the agent never sees the browser before
  the ticket arrives (the same ordering argument as §5.4.2).
* **Worker.** It asks swarm-api for the egress ticket with the attempt-key
  signature it already uses for `/live-session`, renews it before expiry, and
  hands it to the gate. If no ticket can be obtained (no attempt key, or the
  tenant is switched off), the gate falls back to **direct for the whole
  attempt** and records `egress_direct` with the cause. The attempt always
  runs, and it is never proxied for part of its life.
* **NetworkPolicy.** Today the tenant egress policy allows the internet minus
  every private range, so it **excludes `swarm-system`**. "Pod traffic is
  never covered by an ipBlock rule" (allow-egress.yaml). So the forwarder
  hop needs a new policy, `swarm-allow-egress-proxy`, built exactly like
  `swarm-allow-live-egress` (live-browser.md §8.3). It selects pods labelled
  `swarm-egress: "proxy"` and allows TCP `<port>` to pods
  `app.kubernetes.io/name=swarm-egress` in `swarm-system`. In `swarm-system`,
  ingress to `swarm-egress` is allowed from tenant namespaces by their
  template label, and from nothing else. Policies are unions: a pod without
  the label simply has no proxy path.
* **`swarm-egress`'s own egress** is the vendor's endpoint. A NetworkPolicy
  `ipBlock` of the vendor's addresses is brittle, because vendors move them.
  The build lane evaluates GKE's FQDN network policy on this Autopilot
  cluster. Until that lands, `swarm-egress` gets the internet minus private
  ranges, as workers do, and its code dials only the configured vendor host.
* **No change to `swarm-nat`.** The vendor sees our CONNECTs from
  `swarm-nat-ip-0`, and the site sees the vendor's peer. The traffic is
  already NAT-processed today when it goes direct, so the GCP cost does not
  change.

### 2.6 Rejected alternatives

| Alternative | Why not |
|---|---|
| **The gate answers proxy auth through CDP** (`Fetch.enable` with `handleAuthRequests` on its own session, then `Fetch.continueWithAuth`) | Chromium raises `authRequired` only for requests it intercepts, so every request of every target would have to be paused and resumed by the gate. A target the gate misses (a worker, a popup) shows a 407 page. And `Fetch.*` is on the agent's allowlist (`cdppolicy.py`), so the agent's interception and the gate's would race. |
| **Vendor IP-allowlist auth** (allowlist `swarm-nat-ip-0`, with no credential) | Every pod behind `swarm-nat`, which is every tenant's agent, could use the vendor anonymously. It removes the credential by giving its power to all of them. |
| **A Chromium extension that answers `onAuthRequired`** | It puts the credential in files on the sidecar volume, within reach of the renderer exploit, and adds an extension to a browser whose policy surface we keep minimal. |
| **Routing the whole pod, or `swarm-nat`, through the vendor** | It would bill per GB for every clone and API call, show GitHub and Anthropic a residential address, and change the egress of every profile, which is the opposite of the brief. |
| **Datacenter proxies** | Section 1.1: they move the block to another address without removing it. |

### 2.7 What the live view and the hand-off see

* **The live view is unchanged.** Frames come from Xvfb in the sidecar and
  leave on the gate's tunnel, which does not go through the proxy. The mask
  and the input path are unchanged.
* **The person in control sees the site as a residential visitor in the
  targeted country.** Google is served in that country's locale. A sign-in
  may trigger the site's "new sign-in from <city>" notice, or a "verify it's
  you" challenge, on **the person's own account**, because the IP belongs to a
  stranger's household or an ISP block, not to SwarmCloud. The take-control
  dialog should say so in one sentence: "Sites see this browser connecting
  from <country>, not from SwarmCloud." The country comes from the tenant's
  setting.
* **Page loads are slower** by the vendor hop. Typing is not affected, because
  input goes to the local browser. The p50/p95 page-load time is measured in
  phase 1 (section 6.2), and no number is promised here.
* **Timezone and language.** Chromium in the sidecar runs in UTC with
  `en-US`. An IP in the US with a UTC clock is a weak mismatch signal. Setting
  the sidecar's `TZ` to match the targeted country is a one-line option that
  phase 1 can A/B, and it is not the default.
* **The console** shows the route on the attempt: "browser egress: proxy
  (US)" or "direct (<cause>)", from the gate's records.

---

## 3. Security review points

This lane touches credentials, tenant isolation and redaction, so per
CLAUDE.md the build PRs get a review. These are the points that review checks.

### 3.1 The credential

* **Where it lives.** It is one new platform secret, named
  `swarm-browser-egress` in this proposal. It holds the vendor endpoint, the
  username template and the password, and nothing else. **Terraform owns the
  secret container and its single accessor binding** (`swarm-egress`'s
  Workload Identity principal; not swarm-api, not the scheduler, not a tenant
  GSA). That is the same pattern as `swarm-live-key` in `terraform/infra/live.tf`.
  **The value is added only by `scripts/create-secrets.sh`**, through a new
  `--browser-egress` slot that reads **stdin only**, as `--github-app` does. It
  is never in a tfvars file, an environment variable, a Job spec, a log or
  this repository, and the repository is public (CLAUDE.md, owner rule
  2026-09-25).
* **How it is read.** `swarm-egress` reads it by version reference with its
  own Workload Identity, as the relay reads `SWARM_LIVE_KEY_SECRET`
  (live-browser.md §8.1.1 (c)). It holds the value in memory and re-reads it
  on rotation.
* **What is logged.** `swarm-egress` and the gate log event names, causes,
  counts and byte totals only, never a username (the sticky session id is in
  it), a password, a ticket, a URL or a CONNECT host. Anything they print goes
  through `swarm_redaction`. A test pins that no record carries a field named
  after any of those.
* **Rotation.** Change the vendor password, add a version with
  `create-secrets.sh --browser-egress`, and `swarm-egress` picks it up. Open
  CONNECTs finish, and new ones use the new password. No pod is involved.
* **The attempt ticket** is short-lived (minutes, renewed by the worker), is
  bound to the attempt and generation, and is refused by `swarm-egress` once
  the lease is released or the generation is stale. A stale worker gets no
  egress (invariant 5).

### 3.2 The proxy sees all page traffic

* **Contents stay end-to-end.** For `https:` the browser sends
  `CONNECT host:443` and runs TLS with the site through the tunnel. The
  vendor, the residential peer and `swarm-egress` relay ciphertext. A vendor
  or peer that tried to intercept would have to present its own certificate.
  `SSLErrorOverrideAllowed: false`, the refusal of every `Security.*` method
  to the agent, and the gate's refusal of control on a non-secure page
  (§5.4.4) make that fail instead of succeed.
* **What the vendor does see:** every hostname the browser connects to (the
  CONNECT target and the TLS SNI), timing and volume, and the session id. It
  also sees **the full contents of any plain-`http:` page**, which is why
  `HttpsOnlyMode` is forced (section 2.3).
* **The credential's hop to the vendor** is in clear text unless the vendor
  offers a TLS proxy endpoint (section 1.2, requirement 3). `swarm-egress`
  uses the TLS endpoint where the vendor has one. If the chosen vendor has
  none, that exposure is stated in the approval and is not hidden here.

### 3.3 Data residency

The hostnames our tenants' agents and people visit become the vendor's
records, kept for the vendor's own compliance purposes, in the vendor's
jurisdiction, under its retention policy. Before signing, the owner needs
three answers: the vendor's log retention period, its jurisdiction, and
whether a DPA is available. Section 7, Q1 lists them per vendor as "ask".
**Country targeting is not data residency.** A US exit IP says nothing about
where the vendor stores its logs.

### 3.4 Abuse and terms of service

* **Google's terms.** Google's terms of service restrict automated access, and
  `/sorry` is Google's rate limit working as Google intends. Routing through
  residential IPs to avoid that limit is circumventing an anti-bot measure.
  This is a policy and legal choice for the owner, and this document does not
  dilute it. A narrower alternative for the commonest case, search, is a
  **search API**: Google's own programmable search where it is still offered
  to new customers, or a vendor's SERP API billed per request. It gets search
  results without the browser and without the ToS question. It does not help
  with sign-ins (section 7, Q5). **Built, switched off:** the owner chose
  both (Q5), and live-browser agents now get a `search` tool that queries
  the Brave Search API through swarm-api and opens the results in the live
  browser. See [search-tool.md](search-tool.md).
* **The vendor's acceptable use.** Our tenants' agents run arbitrary tasks.
  One agent that credential-stuffs, mass-creates accounts or scrapes a
  forbidden category puts **our** vendor account at risk, and a suspension
  stops every tenant's proxied browsing. The controls are: tenant opt-in
  (section 6.1), a per-attempt byte cap and a per-tenant monthly cap in
  `swarm-egress` (section 3.5), a per-attempt rate limit on new CONNECTs, and
  the vendor's own category blocks, which return `Access denied` and are
  recorded as `egress_vendor_refused`.
* **Residential sourcing.** A rotating-residential IP is someone's home
  connection, enrolled through an SDK in an app. Prefer a vendor that
  documents opt-in consent, and treat this as a reputational question for the
  owner, not a technical one. ISP proxies do not raise it.
* **People's accounts.** A person signing in through the proxy may see
  security notices on their own account (section 2.7). The take-control
  dialog says so before they type.

### 3.5 Kill switch and caps

There are three layers, from fastest to most independent:

1. **`swarm-egress` refuses**, within seconds. A platform flag
   (`browser_egress_enabled`, re-read periodically) set to off makes it
   refuse every new CONNECT with `egress_disabled` and close open tunnels.
   Running proxied attempts then **fail closed** (pages do not load) rather
   than switching to direct mid-attempt. A switch would change the IP under a
   signed-in session, and an emergency stop has to actually stop. The worker
   sees the refusal and records it on the attempt. New attempts are
   dispatched direct.
2. **The vendor dashboard**: disable the sub-user, or rotate its password.
   This works even if our code is wrong, and it is in the runbook the build
   lane writes.
3. **The per-tenant switch** (section 6.1). Removing a tenant sends its next
   attempts direct. It stops one tenant without stopping the others.

**Caps.**
* A **per-attempt byte cap** (proposed 1 GB, ten times the planning figure)
  closes that attempt's CONNECTs and records `egress_cap`.
* A **per-tenant monthly cap** in `swarm-egress` refuses that tenant past it.
* The **vendor sub-user traffic limit** is set to the owner's monthly budget
  (section 7, Q3) and is the hard stop.

### 3.6 The ten invariants

| # | Effect |
|---|---|
| 1 | Unchanged. Only a running browser attempt produces proxy traffic, and nothing is reserved for `QUEUED`/`PARKED`/`READY`. |
| 2, 3 | Unchanged. Egress is not a capacity pool, and admission is untouched. |
| 4 | Unchanged. The hand-off hold and park are as in live-browser.md §4.3, and a park ends the attempt's ticket. |
| 5 | The egress ticket carries the generation. `swarm-egress` refuses a stale one, so a stale worker's browser gets no egress. |
| 6 | `swarm-egress` runs on standard (non-Spot) capacity, like every other service. |
| 7 | `swarm-egress` has `requests == limits`. The gate's forwarder runs inside the sidecar's existing share of the `browser` class (live-browser.md §8.2). |
| 8 | Unchanged. |
| 9 | `swarm-egress` and the vendor account are **shared platform infrastructure**, like the `swarm-live` relay. Isolation is kept by a per-tenant ticket, a per-tenant session id, per-tenant byte accounting and caps, and per-tenant opt-in. Phase 3 can give each tenant its own vendor sub-user if the owner wants billing separated. |
| 10 | **A caller never names a proxy, a country or a vendor.** Routing is per tenant and profile, set by the platform. A task input carrying such a field is refused, as any backend parameter is. |

**Frozen contract.** None of this needs a change to `apps/common/swarm_common/`
if the switch lives in platform configuration (section 6.1). If the owner
prefers the switch as an attribute of the runner profile, that is a change to
`RunnerProfile` in `swarm_common/profiles.py`. It goes in
[contract-change-requests.md](contract-change-requests.md) as a request and
is not made in a lane.

---

## 4. Cost

### 4.1 Rotating residential: per GB

`GB/month = N tasks/day × 30 × g GB/task`. Two price points: **$4.00/GB**, a
typical pay-as-you-go price, and **$2.50/GB**, a committed tier. Both are from
section 1.2, read 2026-10-14.

| N browser tasks/day | GB/month at g = 0.10 | $ at $4.00/GB | $ at $2.50/GB | GB/month at g = 0.25 | $ at $4.00/GB | $ at $2.50/GB |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 30 | $120 | $75 | 75 | $300 | $188 |
| 50 | 150 | $600 | $375 | 375 | $1,500 | $938 |
| 200 | 600 | $2,400 | $1,500 | 1,500 | $6,000 | $3,750 |
| 1,000 | 3,000 | $12,000 | $7,500 | 7,500 | $30,000 | $18,750 |

Committed tiers carry a **monthly minimum**, owed whether it is used or not.
At 10 tasks/day a pay-as-you-go plan, or IPRoyal's non-expiring traffic, is
cheaper than any commitment.

### 4.2 ISP / static: per IP

The pool size is set by **concurrency and per-IP search rate, not by task
count.** One IP per concurrent proxied attempt keeps sign-ins apart. If many
tasks share a few IPs, those IPs build the same reputation `swarm-nat-ip-0`
has, so size the pool at **2 × peak concurrent browser attempts, and at least
10**. At **$2.50-$3.00 per IP/month**:

| Peak concurrent browser attempts | IPs | $/month |
|---:|---:|---:|
| ≤ 5 | 10 | $25-$30 |
| 20 | 40 | $100-$120 |
| 100 | 200 | $500-$600 |

Whether bandwidth is unmetered on ISP proxies depends on the vendor (some
include it, some add a per-GB charge), so confirm that per vendor. **Per IP,
ISP is an order of magnitude cheaper at this platform's volume.** Its open
question is whether Google rates a hosted ISP block well enough. Phase 1
measures that rather than assuming it.

### 4.3 What does not change

GCP cost does not change. Proxied traffic still leaves through `swarm-nat` to
the vendor, which is the same NAT processing the direct traffic pays today.
Ingress is free. `swarm-egress` is one small Deployment in `swarm-system`.
Its size is set in the build and measured, not guessed here.

---

## 5. Build, once approved

Each item is a lane, in this order:

1. **EGRESS-SECRET**: the `swarm-browser-egress` secret container and its one
   accessor in Terraform (`managed-by=swarm-terraform`), the
   `create-secrets.sh --browser-egress` stdin slot, and the egress-ticket
   platform key, generated as `--live-key` is.
2. **EGRESS-SERVICE**: `swarm-egress` (image, manifest under `kubernetes/`,
   NetworkPolicies, ticket check, vendor template, byte metering, caps, flag),
   and its runbook including the vendor-side kill switch.
3. **EGRESS-ROUTE**: the swarm-api route that mints and renews the egress
   ticket under the attempt-key signature, with the fence checks of
   `/live-session`.
4. **EGRESS-GATE**: the gate's forwarder with the peer-uid check, the
   `egress_ticket` ctl op, `chromium_args` under `SWARM_BROWSER_EGRESS`, the
   Chromium policy additions, and the gate's `egress_stats` record.
5. **EGRESS-DISPATCH**: the dispatcher's label, environment and bypass hosts
   for switched-on tenants and profiles. The console shows the route.

Unit tests for each lane follow the repository's rule: offline, with no
vendor and no cloud. The forwarder and `swarm-egress` are tested against a
fake upstream that records what it received, and **a test fails if the
credential, the ticket or a CONNECT host appears in any record or log line**.

---

## 6. Rollout and measurement

### 6.1 The switch: per tenant and per profile, off by default

There is one platform setting, rendered by Terraform into the scheduler,
swarm-api and `swarm-egress`:

```
browser_egress = {
  enabled  = false                      # the platform flag (kill switch, 3.5 layer 1)
  country  = "us"                       # the exit country, per platform; per tenant if Q6 says so
  tenants  = []                         # tenant ids switched on; empty = nobody
  profiles = ["claude-code-browser"]    # live-browser profiles only
}
```

**Nothing is proxied until a tenant is named.** The dispatcher proxies an
attempt only when all three are true: `enabled`, a listed tenant, a listed
profile. The setting is configuration and not part of the frozen contract
(section 3.6).

### 6.2 The metric: Google's block rate, with a baseline first

**What is counted.** The gate already follows the page on screen on its own
CDP session (the mask needs it). For each **main-frame navigation to a Google
search host**, it counts one of three outcomes:

* `ok`
* `sorry`: the final URL's path begins with `/sorry/`, or the response is 429
* `captcha`: a reCAPTCHA interstitial on the result page

At attempt end the gate writes one `egress_stats` record:
`{route: proxy|direct, cause, bytes_up, bytes_down, connects,
google_navigations, google_sorry, google_captcha, page_load_p50_ms,
page_load_p95_ms}`. It holds **counts only**, never a URL or a query,
consistent with "nothing carried here is logged" (`livegate/__main__.py`).
The bytes come from the forwarder for proxied attempts, and from
Chromium's own `Network` totals on the gate's session for direct ones. That
gives the GB-per-task number that section 1.3 had to estimate.

**Phases.**

* **Phase 0, baseline (direct, nothing bought).** Ship `egress_stats` with
  `route: direct` first. Run a fixed set of **30 search-led browser tasks**
  (the 26 analysed tasks plus enough search-heavy ones to reach 30 Google
  searches or more) and count `/sorry`. Thirty is the smallest set that can
  distinguish "always blocked" (3/3 so far) from "blocked one time in five" at
  a useful confidence. Three cannot. Measure the background-traffic hosts for
  the bypass list (section 2.3) in the same run.
* **Phase 1, A/B on the operator's own tenant.** Switch on one tenant. Run
  the same 30 tasks proxied, through **both** a rotating-residential trial and
  an ISP trial if the vendor offers both. Compare the `sorry` rate,
  `captcha` rate, GB/task, and page-load p50/p95 against phase 0.
  **Success:** a `sorry` rate under 10% with the proxy, against the phase-0
  rate, at a GB/task within the section 4 planning figure. **Failure:** if
  the residential `sorry` rate does not fall, the block is not about the IP
  (it could be the browser's fingerprint, or the search pattern itself).
  Stop, and do not buy more.
* **Phase 2, opt-in tenants.** Named tenants are switched on, with the
  monthly cap set. `egress_stats` gives weekly `sorry` rate, GB and spend per
  tenant.
* **Phase 3, decide the default.** The owner decides whether the proxy
  becomes the default for `claude-code-browser`, from phase 2's numbers. The
  owner also decides whether each tenant gets its own vendor sub-user.

---

## 7. Owner questions

These are also in the lane's `questions.json`.

**Q1. Which vendor?**
* **Decodo**: PAYG ~$4/GB, plans down to $2/GB, 24 h sticky sessions, and a
  sub-user API with traffic caps. *Recommended for the pilot*: it has the
  longest sticky session among the rotating vendors and the caps the kill
  switch needs.
* **Bright Data**: the largest pool and full targeting, but company KYC
  (human-reviewed, about a week) before Residential works. Its ISP proxies
  need no KYC.
* **Oxylabs**: opt-in sourcing, but a 30-minute sticky cap that a long
  attempt will exceed.
* **IPRoyal**: the cheapest ISP IPs and non-expiring traffic, so nothing is
  wasted on a pilot of unknown volume.
* For each one, ask before signing: log retention, jurisdiction, DPA, a TLS
  proxy endpoint, and acceptable use for agent browsing and sign-ins.

**Q2. Rotating residential or ISP/static?**
* **ISP**: fixed cost (~$25-$120/month at our scale), one stable IP per task
  across parks and carried sessions, and no residential-sourcing question.
  Its risk is a weaker reputation with Google.
* **Rotating residential**: the best reputation, but metered ($120-$2,400/month
  at 10-200 tasks/day), and the IP can change mid-attempt.
* **Both in phase 1, decide on the measured rate.** *Recommended.*

**Q3. Monthly budget cap?**
* $150 (pilot: phase 0-1 and a few tenants)
* $500
* $2,500
* Recommended: $150 until phase 1 reports, then revisit with the measured
  GB/task.

**Q4. Where does the credential live?**
* **A platform forwarder, `swarm-egress`; pods hold only attempt tickets.**
  *Recommended*, section 2.2.
* **In each browser pod's gate**: one less service, but a renderer exploit
  can take a credential that works for every tenant.

**Q5. Proxy the browser, or use a search API for search?**
* **Proxy the browser** (this design): it covers sign-ins and every site, but
  carries the ToS question of section 3.4.
* **A search API for search, and keep the browser direct**: no ToS
  circumvention for search and cheaper per query, but no help with sign-ins
  or other sites' blocks.
* **Both**: a search tool for the agent, and the proxy only for tenants who
  need sign-ins.

**Q6. Exit country?**
* US for the platform (matches `en-US` and the console's users).
* Per tenant, set by an operator: more configuration, and a person's
  sign-in comes from their own country.

**Q7. On the kill switch, should running attempts fail closed (pages stop
loading) or fall back to direct mid-attempt?**
* **Fail closed**: *Recommended*, section 3.5. An emergency stop stops, and
  no session silently changes IP.
* **Fall back to direct**: attempts keep working, but signed-in sessions may
  break and the agent sees a changed IP.

---

### 7.1 Decisions (owner, 2026-10-13 ~21:10 PDT)

| # | question | answer |
|---|---|---|
| Q1 | Vendor for the pilot | **Bright Data.** Its residential pool needs a human-reviewed company KYC, which the owner completes; its ISP proxies need none. |
| Q2 | Rotating residential or ISP/static | **Trial both** in phase 1, against the direct baseline, and keep the one with the lower measured /sorry rate per dollar. |
| Q3 | Monthly budget cap | **$150 pilot**, set as the vendor sub-user traffic limit. |
| Q4 | Where the vendor credential lives | **Only in the platform forwarder `swarm-egress`.** Pods carry a ticket scoped to one attempt. |
| Q5 | Proxy, search API, or both | **Both: search tool + proxy** (answered separately, 2026-10-13 ~21:30 PDT). The search tool is [search-tool.md](search-tool.md); the build here does not depend on it. |
| Q6 | Exit country | **US**, for the whole platform. |
| Q7 | Kill switch | **Fail closed.** |

The owner holds the Bright Data account, KYC and credential. The credential is stored with `scripts/create-secrets.sh --stdin` and is never written anywhere else. The build ships with the flag off.

## 8. Not decided here, and not verified

* **That a residential or ISP IP actually lowers Google's `/sorry` rate for
  this browser.** The block could be partly fingerprint- or pattern-driven.
  Phase 1 is the measurement, and its failure criterion is stated in
  section 6.2.
* **Every vendor price, limit and policy above** was read 2026-10-14, mostly
  from third-party comparison pages, and must be confirmed on the vendor's own
  pages.
* **That a CDP-created browser context without proxy parameters inherits
  `--proxy-server`**, and **the exact Chromium policy names** in section 2.3.
  Both are checked by the build lane's tests and the image's `selftest.py`.
* **GKE FQDN network policy on this Autopilot cluster**, for pinning
  `swarm-egress` to the vendor's hostname.
* **The 26-task analysis itself** is on the operator's laptop. This document
  relies on its quoted finding (3 of 3 searches answered 429 `/sorry`) and on
  the NAT configuration measured in the 2026-10-09 incident, not on a re-run.

## Sources (read 2026-10-14)

* Bright Data pricing: [use-apify.com, Bright Data pricing 2026](https://use-apify.com/blog/bright-data-pricing-guide-2026); [aimultiple, proxy pricing](https://aimultiple.com/proxy-pricing).
* Bright Data Residential KYC and network access: [docs.brightdata.com, network access](https://docs.brightdata.com/products/residential/network-access).
* Oxylabs: [oxylabs.io pricing](https://oxylabs.io/pricing/residential-proxy-pools); [hostadvice, Oxylabs review](https://hostadvice.com/proxy-servers/oxylabs-review/).
* Decodo: [afftank, Decodo review](https://afftank.com/blog/decodo-review); [Smartproxy sub-users API](https://apis.io/apis/smartproxy/smartproxy-sub-users-api/).
* IPRoyal: [use-apify.com, IPRoyal pricing 2026](https://use-apify.com/blog/iproyal-pricing-plans-2026); [hostadvice, IPRoyal review](https://hostadvice.com/proxy-servers/iproyal-review/).
* Page weight: [HTTP Archive Web Almanac 2025, page weight](https://almanac.httparchive.org/en/2025/page-weight).
