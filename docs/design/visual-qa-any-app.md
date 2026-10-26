# Visual QA of any web application, its findings filed in that application's repository

**Status: DESIGN, not built.** Lane AUTONOMY-DESIGN, 2026-10-22, part of
#1933. Built by lanes VQ-1 to VQ-7 of [agent-autonomy.md](agent-autonomy.md)
§6, on that document's capabilities (above all the brokered issue writes of
its §3.2). Nothing here changes behaviour until those lanes merge. The
owner answered §9's three questions on 2026-10-22 (~06:40Z). Since
2026-10-25 a target may carry several environments that one dispatch runs
together, discovering as it captures (#2136): §10 says what that changed
here, and [qa-environments.md](qa-environments.md) is its design.

**The request (BGD, 2026-10-22):** "with the features we have today, can we
dispatch a lane to use our browser agents and do a visual qa of our product
oracle app at https://oracle.swarm.saga.xyz and report back with issues filed
in github in the project repo for product-oracle in my personal github?",
then: "the goal is to be able to QA any web application not specifically
oracle".

**The owner's decisions this design builds on, not reopens** (BGD, 2026-10-22
~05:15Z):

* **Generic, never per app.** No Oracle-specific code, route list, prompt or
  setting anywhere in the repository. Oracle is a QA target a person
  registers, like any other.
* **Version 1 signs in to two kinds of target**: **public pages** (no sign-in),
  and **apps with their own login**. For the second, a test credential is kept
  per target in Secret Manager and **typed into the page by the live gate,
  never by the agent**; the agent, its page logs, screenshots, checkpoints and
  artifacts never hold it.
* **Not in version 1:** apps behind Google IAP, and repositories' mock or
  fixture builds. §8 names each as a later option, with what it would need.
* **No pilot on Oracle with today's tools.** The first QA of any app runs on
  the generic build this document designs.

**The short answer to the first question, read on main 2026-10-22.** Not end
to end. Today a person could hand-write a workflow that points
`swarm_qa capture` at a public URL, but nothing signs in to an app's own
login without the agent holding the password, no agent can file an issue
(agent-autonomy.md §2 row 6), the capture tool empties every page's storage
and waits on the console's own pending-read marker, and the only QA template
is the console's. Each of those is a lane below.

---

## 0. What exists, measured on main (`0235ddd`)

| piece | state | proof |
|---|---|---|
| the capture tool | **exists, console-shaped** | `images/agent-runtime-browser/swarm_qa.py` (QA-CAPTURE): routes are `{id, path, ready, segments}` under `--base` any origin; it refuses without the live browser (`SWARM_CDP_ENDPOINT`), sets the device by name through the gate, the theme twice (`swarm-handoff theme` and `emulate_media`), segments each page, records text, the accessibility tree and errors, and writes `manifest.json`. Console-specific: every load empties local and session storage (`RESET_STATE_JS`), the settle waits on the console's pending reads (`PENDING_JS`), and the clock is frozen at the route list's instant |
| device presets and themes | **exists** | QA-KIT: `desktop` (1440) and `phone-390` (`apps/agent-worker/agent_worker/livegate/presets.py`), `swarm-handoff device` and `theme` |
| flow recording | **exists** | QA-FLOWS: `swarm-handoff record start/stop` writes a GIF per flow |
| the QA template | **exists, console only** | `docs/qa/console-qa.workflow.json`, 17 steps: six `capture-*` (`claude-code-browser`, haiku), six `review-*` (`claude-code`, opus, each `input_from` its capture tar), three `flow-*` (`claude-code-browser`, jev or Playwright), `report` and `flow-report` (`claude-code`, opus). The findings shape is [visual-qa.md](visual-qa.md) §4.4; QA-REPORT-TARS stages the capture tars into `report` too |
| signing in | **console only** | QA-IDENTITY ([qa-identity.md](qa-identity.md)): an IAP JWT for the console's one host, handed by the worker to the live gate on `ctl.sock` (verb `qa_identity`, `livegate/qaidentity.py`), added on the gate's own CDP sessions; the agent never holds it |
| password fields | **cleared on return** (no longer masked for people: BGD, 2026-10-25, [live-browser.md](live-browser.md) §5.3) | the screencast shows the screen as Chromium draws it, password fields included; on the return from a person's control it clears every password input before releasing the agent (`livegate/cdpproxy.py`, "THE ORDER ON RETURN") |
| cookies | **not the agent's** | `cdppolicy.py` refuses the agent `Network.getCookies`, `getAllCookies`, `Storage.getCookies` and every cookie write |
| an egress host allowlist | **none** | worker pods may reach any public address on 443 (`kubernetes/network-policies/allow-egress.yaml`, `0.0.0.0/0` minus private ranges); Terraform's `restrict_egress` is a port list, not hosts (`terraform/modules/network/variables.tf`); `apps/swarm-egress` is the optional residential proxy and refuses only internal names and ports (`swarm_egress/policy.py`) |
| filing issues | **missing for an agent** | agent-autonomy.md §2 row 6; designed in its §3.2 |

---

## 1. The QA target

**What a person registers**, one document per target, `qa_targets/{target_id}`
in swarm-api's own collections (no frozen field, as `qa_identity_grants` is
not one):

| field | holds | why |
|---|---|---|
| `name` | a label, unique in the tenant | how a person and the schedule name it |
| `tenant_id` | the registering workspace | invariant 9: the target, its runs, its artifacts and its credential are that tenant's |
| `base_url` | `https://host[:port]/[path]` | `https` only, no userinfo, no query; the host must resolve to a public unicast address and must not be an IP literal or an internal name (the rule of `swarm_egress.policy`, reused, not restated) |
| `repository` | one of the tenant's **registered** repositories | where its findings go (§5). A URL alone is refused: filing reaches a repository only through a registration whose forge slot the tenant already holds |
| `sign_in` | `{kind: "none"}` or `{kind: "own_login", login_path, username_selector, password_selector, submit_selector, signed_in_selector, extra_origin?}` | §2. Selectors are optional where the login page has exactly one `input[type=password]` and one `autocomplete=username` or `type=email` field; `signed_in_selector` is required, because "the page changed" is not "signed in" |
| `credential` | `{set: bool, set_at, set_by, version}`, never a value | a **reference**: the value is in Secret Manager (§2.1) |
| `challenge_bypass` | `{kind: "header" \| "cookie", name}`, optional; its value served only as `challenge_bypass_value: {set, set_at, set_by, version}` | §2.5: the app owner's own way past their CAPTCHA, added by the gate on the approved origins only |
| `screens` | a list `{id, path, ready?, segments?}`, or `discover: {max_pages ≤ 60, max_depth ≤ 3}` | the capture's route list; discovery is bounded and same-origin (§4.1) |
| `flows` | `{id, goal, start_path, mutating: false}` each, at most 150 | goal-driven flows, run in **batches** (QA-FLOWS-CAP, BGD 2026-10-26): `flows_per_step` read-only flows a live-browser step, one agent that signs in once and runs its batch in order, each flow ending "STOP and capture" with its own `flow-<id>.tar`. `flows_per_step` is 1-30, chosen by `:run`'s `flows_per_step`, else the workspace's `qa_flows_per_step` (`GET`/`PUT /v1/workspaces/{id}/qa-settings`), else default 15; the run's `qa_target_runs` row records the value and where it came from. 150 because AI Studio's ai-studio-staging needs 132 read-only frame-state flows; it is stated once, in `qaspec.MAX_FLOWS` (the capture tool never reads a flow). A leg over the 50 steps a workflow holds is refused naming the smallest `flows_per_step` that fits (with 60 screens, 150 flows need 5), and one whose prompts pass the 256 KiB of workflow input is refused saying so (§4.1 step 4). `mutating: true` is refused unless `environment` is `non_production` (§3). `start_path` is relative to **`base_url`**, path included: under `https://h/app/`, `/` opens `https://h/app/` and `/settings` opens `https://h/app/settings`. Screen paths, exclusions and `login_path` are origin-relative instead, because discovery writes routes as the origin's paths (QA-ORIGIN); a flow says where it starts in the app, so it is read from the app's base, through the one join `qaspec.screen_url`, and its start is refused when either reading is excluded (QA-TARGET-FILE, #2062) |
| `flows_browser_tool` | `playwright` (default, when unset) or `jev` | the tool the flows drive the live browser with (§4.1, step 4); anything else is refused at registration |
| `widths` | presets by name, default `["desktop", "phone-390"]` (1440 and 390) | the gate's presets, never a viewport override (the gate refuses one) |
| `themes` | default `["light", "dark"]`, through emulated media | as the console's capture sets them |
| `exclusions` | path globs never visited, element texts or selectors never clicked | always merged with the defaults: `/logout`, `/signout`, and controls whose text names delete, remove, pay, buy, purchase, send, invite, unsubscribe or cancel subscription |
| `environment` | `production` (default) or `non_production` | §3: only a non-production target can run a mutating flow |
| `read_posts` | target-origin paths whose `POST` is a read (a GraphQL endpoint) | §3's method rule would otherwise refuse them |
| `design_docs` | path globs in the repository, default `DESIGN.md`, `docs/design*/**`, `**/style-guide*` | §4.3: the review reads the app's own design where it has one |
| `file` | `{min_severity: "minor", max_issues: 20, labels: ["visual-qa"], label_rules: [], evidence: "console_link"}` | §5 |
| `rate` | `{navigation_ms: 2000, requests_per_s: 10}` | §3; a registrant may make it slower, never faster than 1 navigation a second |
| `authorized_by`, `attested_at` | the registrant's statement that the tenant may test this application | someone else's app: the platform does not decide whose it is, but it records who said it may be tested |
| `host_state` | `pending`, `approved` (with `approved_by`, `approved_at`) or `revoked` | §1.2 |

**Environments (#2136, §10).** A target may instead carry `environments[]`:
`base_url`, `sign_in`, `credential`, `challenge_bypass`, `environment`,
`read_posts`, `rate` and `host_state` (with its approval) then live in each
entry, one per leg, and everything else above stays on the target once.

**Who may register one.** A workspace **admin** of the tenant, through
`POST /v1/qa-targets` (and `PATCH`, `DELETE`), because a target sends the
tenant's agents to an outside site and files into the tenant's repository in
its name. A member may run an approved target (§6) and may not change it.
Every change writes an `admin_audit` row (`qa_target_created`, `_changed`,
`_deleted`) in the same transaction, as `swarm_api.admins` does.

### 1.1 Why the target, not the caller, carries everything

Invariant 10: a caller picks a runner profile **by name** and sends nothing
that picks an image, a command or a credential. A QA run is started by naming
a target (`POST /v1/qa-targets/{id}:run`). swarm-api renders the workflow from
the target (§4) and stamps each browser step's agent with
`metadata.qa_target = {id, revision}`, written by swarm-api, never accepted
from the caller. The worker asks swarm-api for its attempt's target (§2.2);
nothing about the target travels in the step's input. That is also why this
design needs **no frozen-contract change**: no `RunnerInput` is added to
`claude-code-browser` (QA-IDENTITY's `qa_identity` needed request QI-1
because its name travels in the input; this one does not).

### 1.2 The host, and the egress allowlist that does not exist yet

There is no hostname allowlist for workers today (§0). So the host approval
is the allowlist, made where it can be enforced:

* **A new target's host is `pending`** and no run starts against it
  (`qa_target_host_pending`, 409) until a **platform admin approves it**
  (`POST /v1/qa-targets/{id}:approve-host`), audited `qa_target_host_approved`
  with the host and the registrant. The approval is per host, and covers the
  declared `extra_origin` of a login (§2.3) only when it is named in the same
  approval. Changing `base_url`'s host or `extra_origin` sets it back to
  `pending`. On a target with `environments[]` the approval is per entry:
  `:approve-host` names the entry, and a change resets that entry alone
  (§10). The approver is any platform admin, audited (VQ-Q1, answered,
  §9). In the console the control is **Approve host** on the target's card
  on Repositories › QA targets (`apps/swarm-ui/src/QaTargets.tsx`), drawn to
  a platform admin only, which sends the route above once the admin has
  typed the host, and the login's `extra_origin` when one is declared.
* **The IdP token path is approved in the same place, and nowhere else**
  (QA-IDP-POST, BGD decided 2026-10-23, #2067). With the `extra_origin`, the
  platform admin may name one exact path on it, `idp_token_path` (`/...`; no
  query, fragment, `%`-encoding, glob, empty, `.` or `..` segment), stored as
  `approved_idp_token_path` and audited in the approval's row. It is refused
  without the `extra_origin` named, it is not a registration field (a
  workspace admin's change cannot set it), and a change of host or
  `extra_origin`, or an approval that does not name it, clears it. The
  console's approve form carries it as an optional field, filled with the
  approved one, and draws **Approve again** on an approved target that
  declares an `extra_origin`, so an admin can set or clear it later. §3 says
  what the gate does with it.
* **The live gate enforces it** (§3): the attempt's top frame never commits a
  document outside the approved origins, and nothing but a `GET` leaves for
  any other origin.
* **If `restrict_egress` ever becomes host-based**, the approved hosts are its
  entries for the browser pods, read from `qa_targets` by the same lane that
  makes that change; today it is a port list and needs none.

The first target BGD's request names, `https://oracle.swarm.saga.xyz` filed
into product-oracle in his personal GitHub, is registered exactly this way in
his personal workspace, whose forge slot already holds that repository. It is
data, not code.

---

## 2. Own-login sign-in

### 2.1 Setting the credential without any session or chat seeing it

The console's one-time link, as `swarm_key_set_link` does for provider keys
(`routes/provider_keys.py`: `POST /v1/provider-keys/{provider}/links`, then
the console's form checks the link token):

1. A workspace admin asks for a link: `POST /v1/qa-targets/{id}/credential-links`
   (console button, or the plugin's `swarm_qa_target_credential_link`, which
   returns **only** the URL and its expiry, as `keys.key_set_link` does).
   The link is single-use, bound to the asking person's email, the target,
   its entry and its `field` (`credential`, the default, or
   `challenge_bypass`, §2.5), and expires in 15 minutes.
2. The person opens it **in the browser**, types the test account's username
   and password into the console's form, and submits
   (`PUT /v1/qa-targets/{id}/credential` with the link token). Nothing passes
   through Claude Code, a chat, a prompt or a plugin argument.
3. swarm-api adds a version to the tenant's QA secret
   `swarm-tenant-<tenant>-qa` (§7: one Terraform-declared secret per tenant,
   its value a JSON map keyed by target id; VQ-Q2, answered, §9), disables the previous version, and records on the target
   only `credential: {set: true, set_at, set_by, version}`.
   A tenant Terraform does not declare gets the same secret, empty, with the
   same QA labels and swarm-api alone bound, from `scripts/register-tenant.sh`
   (`--group` or `--user`, section 4b, `scripts/lib/forge-slot.sh`
   `qa_secret_ensure`); a personal workspace that the swarm-workspace-apply job
   builds does not get one yet, because that job's call guard and the
   deployer's `outside_personal_workers` alert do not admit a swarm-api binding
   (VQ3-REGISTER's question to the owner).
4. The value is never returned by any route, never logged, never written to
   Firestore. "Is it set?" is the only question the API answers.

### 2.2 How the credential reaches the page

The QA-IDENTITY path, generalised from one header to one sign-in:

1. **The worker asks.** In `handoff.HandoffPath.prepare`, after the gate
   registers and before the runner starts, for a `claude-code-browser`
   attempt the worker calls `POST /v1/attempts/{attempt_id}/qa-target` with
   the attempt key (`authenticate_worker`, `verify_proof`, a live lease for
   this generation). swarm-api reads the **agent's** `metadata.qa_target`,
   resolves the tenant **from the lease**, checks the target is that
   tenant's, its host `approved`, and answers `204` when the agent has no
   target (the common case), else the target's public settings and, for
   `own_login`, the username and password read from Secret Manager at that
   moment. Every answer appends a `live_audit` row (`qa_target_issued`, the
   target id, the attempt, the generation, never a value).
2. **The worker hands it to the gate and forgets it.** `ctl.sock` verb
   `qa_sign_in` (`livegate/ctl.py`): `{origins, login_url, selectors,
   username, password, signed_in_selector}`, accepted only from the
   registered worker, refused after `wipe`, as `egress_ticket` and
   `qa_identity` are. The worker drops its copy once the gate acknowledges,
   and logs only the target id and the outcome.
3. **The gate signs in, with the agent held.** The gate takes control exactly
   as a person's hand-off does (`livegate/cdpproxy.py`: the agent's commands
   are held and its results withheld while someone else drives). On its own
   CDP session, on Chromium's pipe, which the agent never sees, it opens
   `login_url`, focuses each field by its selector, types with
   `Input.insertText`, presses the submit control, and waits up to 30 s for
   `signed_in_selector`.
4. **It cleans up before it lets go**, in the existing return order: clear
   every password input its sessions can reach, navigate the agent's tab to
   `base_url`, deliver the held responses, then release the held commands.
   The agent never had a tab on which the password was typed.
5. **The value is scrubbed from everything bound for the agent.** While a
   sign-in credential is held, the proxy replaces every occurrence of the
   password's bytes (and its URL- and base64-encodings) in every agent-bound
   CDP frame with `[qa-credential]`, the QA-IDENTITY value scrub extended to
   one more value, decoding `base64Encoded` bodies before matching. A page
   that echoes the password back ("show password", an error page) reaches
   the agent's screenshots, text and accessibility tree scrubbed. The
   screencast paints nothing over the screen since BGD's decision of
   2026-10-25 (password fields draw as dots, as Chromium draws them); a field
   typed by the gate is cleared before any agent capture can exist.

**Start clicks: a chooser, or a "Sign in" button before an identity
provider** (BGD, 2026-10-23; lane VQ8-START-CLICKS). Not every login page is
the form. AI Studio (`https://studio.saga.xyz`, targets
`ai-studio-staging-routes` and `ai-studio-staging-crawl`) opens on a chooser
with a Staging | Production toggle and a "Sign in" button, which redirects to
Keycloak on its `extra_origin` (`https://sso.agents-staging.saga.xyz`), where
the username form lives. Opening `login_url` and looking for a password input
there finds none, so the sign-in ended `qa_sign_in_no_form` every time. An
`own_login` target may therefore list `sign_in.start_clicks`, which the gate
makes in step 3 between opening `login_url` and looking for the form:

* **The shape.** 1 to 3 entries `{text, css?}`, in order. `text` (1-60
  characters) is the control's exact visible text, trimmed and
  case-sensitive. `css` (optional, at most 200 characters) narrows which
  elements are candidates, for example `button`. Allowed only with
  `kind: own_login` (`qaspec.start_clicks`, called by the registration's
  `_sign_in`, so a target that is stored is one the gate can click).
* **Plain CSS only.** Playwright's selector syntax (`:has-text()`, `:text()`,
  `:visible`, `>>`, `text=`) is refused at registration with a sentence
  saying why: the gate clicks over CDP, inside the page, with
  `querySelectorAll`, not through Playwright. The control's text goes in
  `text`, which is how a person would name it anyway.
* **Never an exit or a write.** A click whose text contains any of the
  target's exclusion words (the defaults, `logout`, and the target's own
  `exclusions.controls`) is refused at registration: the gate clicks before
  it has signed in, and nothing that names delete, pay or send is a way in.
* **Exactly one control, or no click.** For each click the gate waits up to
  `START_CLICK_SECONDS` (10 s: a chooser is drawn by the page's own load,
  which the form's 30 s already bounds, and an absent control should not
  hold the agent for a minute per click) for exactly one visible element
  matching `css` (any element without it) whose trimmed text equals `text`;
  of a nested pair (a button and the span inside it) only the inner one
  counts. It clicks that one and waits for the page to settle. None, or
  more than one, ends the sign-in with `qa_sign_in_start_click_missing` and
  a sentence naming the text (the gate's `status().sentence`). A challenge
  seen first is a challenge (§2.4); the signed-in state seen first is a
  sign-in (the IdP's session already held).
* **The origin lock holds.** Where a click leads is judged like every
  navigation of the gate's tab (`QaTarget.judge`, §3): the target's host
  and its approved `extra_origin` only. Anywhere else gets the left page,
  the form never appears, and the sign-in fails without typing.
* **Then nothing else changes.** After the last click the form steps run
  unchanged on whichever approved origin the page reached. The agent is
  held throughout, MFA and CAPTCHA are refused, password inputs are
  cleared, the gate's tab is closed, and the agent never sees that tab.

**The agent's own tab may land on the chooser again.** AI Studio keeps its
access token in page memory, so the agent's tab, sent to `base_url` after the
gate's tab signed in (step 4), shows the chooser, and one "Sign in" click
there completes through the Keycloak session with no form. So when a target
has start clicks, every capture, flow and discovery prompt carries one more
sentence naming them in order (`qaspec._sign_in_note`): *"If your tab shows
the sign-in chooser, click 'Staging' then 'Sign in'; the sign-in completes
without a form. Never type into a login form; if one appears, run
`swarm-handoff sign-in` once."* The agent never holds the credential, so a
form it reaches is one it cannot fill, and the hand-off asks the gate again.

### 2.3 Keeping the session

* **Cookies stay in Chromium** for the attempt. The agent cannot read them
  through CDP (`cdppolicy.py` refuses every cookie read and write), and
  cookies and storage of the target's origins are excluded from every
  `session_export` (carry, login, park), so nothing of the target's session
  enters LB-7's saved logins.
* **Storage is kept.** `swarm_qa capture` gains `--keep-storage`, used for
  every `own_login` target, because a single-page app often keeps its session
  in `localStorage` and the console capture's `RESET_STATE_JS` would sign it
  out on every route.
* **Losing it.** When a step sees the login page again (its `login_path`, or
  `signed_in_selector` absent where it was present), the agent runs
  `swarm-handoff sign-in`; the worker asks swarm-api again and the gate
  repeats §2.2 step 3. At most three sign-ins an attempt, then
  `qa_sign_in_exhausted`.
* **A login on another origin** (an `auth.` host of the same app) is
  `extra_origin`, approved with the host (§1.2), and allowed only as a
  navigation the gate's own sign-in makes.
* **`wipe`** clears the credential, then Chromium is closed with the attempt.
* **Residual risk, stated plainly.** A session token the app keeps in a
  non-`HttpOnly` cookie or in `localStorage` is readable by the agent through
  `Runtime.evaluate`, which the agent needs to test a page at all. That is the
  app's session for one test account, for one attempt, not the password; the
  test account should be one that can do no harm (§3), which the
  registration's attestation says.

### 2.4 MFA and CAPTCHA: refused, with a reason

If `signed_in_selector` is not reached and the page shows an MFA or CAPTCHA
challenge (an `autocomplete=one-time-code` input, a WebAuthn request, or a
challenge frame from the hosts the mask already knows, `mask.SHOWN_IN_CONTROL`),
the gate stops and answers `qa_sign_in_challenge` with the sentence
"the target's login asks for MFA or a CAPTCHA; register a test account
without one". It never solves one, never retries past it, and never hands the
page to a person to complete: a person typing their own code would leave the
agent signed in **as that person**, the reason QA-IDENTITY refuses hand-off on
its host. Steps that need the sign-in fail with that cause; public screens of
the same target still run and are reported.

### 2.5 A challenge on an app you own

BGD, 2026-10-23 (#2058, lane CAPTCHA-BYPASS): **a QA target may carry a
challenge bypass its owner configures, and only the gate applies it, only on
the target's own origins.** §2.4 stands for everything else: the gate never
solves a CAPTCHA, never calls a solver service or a model to solve one, and
never disguises the browser (stealth fingerprints, rotating proxies). A
CAPTCHA is a site owner saying no to automated access; on an app whose owner
says yes, the owner already has the tools to say it, and this section only
carries their answer to the page.

**What the owner configures** (the runbook,
[qa-challenge-bypass.md](../runbooks/qa-challenge-bypass.md)): in staging,
the always-pass test keys reCAPTCHA, hCaptcha and Cloudflare Turnstile each
publish; in production, a WAF or edge rule that skips the challenge for a
request carrying a header (or cookie) only they and this platform know.

**The target's spec.** `challenge_bypass: {kind: "header" | "cookie", name}`,
a registration field (`qaspec.challenge_bypass`): `name` is an HTTP token of
at most 64 characters; a header may never be one the app's session, the
request's framing or the browser owns (`Authorization`, `Cookie`, `Host`,
`Set-Cookie`, `Origin`, `Referer`, `Content-Length` and the other framing
headers, and anything under `Proxy-` or `Sec-`: `RESERVED_BYPASS_HEADERS`); a
target with no host to scope it to is refused. A value sent with the spec is
refused, so none is ever stored on the document.

**The value is write-only, stored where the credential is.** A workspace
admin sets it with `PUT /v1/qa-targets/{id}/challenge-bypass` (the target
form's write-only field in the console), 16 to 1024 characters a header and a
cookie can both carry unquoted. It is stored in the tenant's one QA secret
(§2.1), in the target's own entry beside the username and password (each
write keeps the other's keys): the same secret, the same reader (swarm-api
alone), so **no IAM changes**. The target records only
`challenge_bypass_value: {set, set_at, set_by, version}`, audited
`qa_target_challenge_bypass_set` with the version, kind, name and
`via_link`. **The value arrives through a one-time link, as the password
does** (BGD's Q8 of [secrets-scopes.md](secrets-scopes.md) §11, lane
SSC-LINKS): `POST .../credential-links {"field": "challenge_bypass"}` mints a
link to the console's bypass form (`/qa-targets/{id}/challenge-bypass?t=`),
only for an entry whose `challenge_bypass` names its kind and name; the PUT
sends its token as `link_token`. A link is bound to its field too, so a
password link never sets the bypass nor a bypass link the password, and one
presented for the wrong field is not burned. A PUT without a token is 403
`qa_bypass_link_required` through its switch, which ships off: off, the write
goes through, as the target form sends it today, and the log names the
tenant, target, entry and caller, never the value. A token that is presented
and not the caller's live bypass link is 403 `qa_credential_link_invalid`
whatever the switch says. Removing the bypass, or changing the host or `extra_origin` (which
sends the host back to `pending`), removes the value from the secret first:
a value made for one host never reaches another. Deleting the target removes
it with the credential.

**How it reaches the page.** Exactly as the password (§2.2): the worker's
attempt route answers `challenge_bypass: {kind, name, value}` beside the
sign-in, read from the secret at that moment, for a public target too, and
only when the value is set; the `live_audit` row names the kind, never the
value. The worker hands it to the gate in `qa_sign_in`'s args, registers it
with the logger's redaction, and drops its copy once the gate answers.

**The origin rule.** The gate applies it on its own CDP sessions, through
the Request-stage `Fetch` pause its guard already makes on every page, frame
and worker (§3), never through `Network.setExtraHTTPHeaders`, which would
send it to every origin a page touches:

* `header`: a request whose URL's origin is exactly one of the target's
  approved origins (its base origin, and the login's `extra_origin` when the
  platform admin approved it, §1.2; scheme, host and port all equal) is
  continued with the header added, replacing a same-named one. Any other
  origin, a look-alike host included, is continued untouched. An added
  header follows no redirect (qa-identity-measurements.md M5): a redirect's
  own request is judged by its own URL.
* `cookie`: the gate plants it once per approved origin on Chromium's pipe
  (`Storage.setCookies`), host-only (no `Domain`, so no subdomain is sent
  it), `Secure`, `HttpOnly` and `SameSite=None`, so the browser sends it to
  that host and nowhere else. Cookies are not port-scoped; a target's
  origins are hosts in practice, and the header kind is the one to use when
  two approved origins share a host.

**Out of the agent's reach.** The value is a scrub needle like the password:
every agent-bound CDP message has it, its URL encodings and its base64 forms
replaced with `[qa-credential]` (a page that echoes its request headers, and
Chromium's own `Network.requestWillBeSentExtraInfo`, which reports the headers
sent). The agent's CDP can neither read nor write cookies (`cdppolicy.py`),
an `HttpOnly` cookie is invisible to page script and so to the agent's
`Runtime.evaluate`, the session carry already leaves the target's origins out
(§2.3), and nothing the gate records names the value. That is why the value
has a 16-character floor: a short needle would blank unrelated text. **Known
limit, stated plainly:** a page that draws an echoed header as pixels puts it
in a screenshot, which no text scrub reaches; the owner's skip rule matches
the header and never echoes it, and an app that echoes request headers into
its pages should not be given a bypass. An agent that continues one of its
own paused requests with its own `headers` may send the placeholder in the
header's place, which costs it the bypass on that request and reveals
nothing.

---

## 3. Safety on someone else's application

Enforced by the live gate on its own sessions, not left to the prompt; the
prompt says the same so the agent does not fight its own gate.

* **Read-only by default.** While a QA target is held, the gate pauses
  requests at the `Request` stage (`Fetch.enable` on its own sessions, the
  QA-IDENTITY mechanism) and fails, with a gate-made answer recorded as
  `qa_write_refused`:
  * any `POST`, `PUT`, `PATCH` or `DELETE` to the target's origins, except its
    own sign-in submission and the paths in `read_posts`;
  * **any** request but `GET` and `HEAD` to every other origin, so a
    third-party payment, mail or analytics frame cannot be made to send.
  **One exception: the IdP token refresh** (QA-IDP-POST; BGD decided
  2026-10-23, "allow the token path only", #2067). On 2026-10-23 AI Studio's
  run signed in, and then the app's own Keycloak token exchange (a browser
  `POST` from `studio.saga.xyz` to its `extra_origin`'s
  `/protocol/openid-connect/token`) was answered by the gate's 403 with no
  CORS header: every screen stayed on "Checking your session…" and the run
  filed the gate's refusal as the app's CORS blocker. So a target whose
  platform admin approved an `idp_token_path` (§1.2) has exactly
  `POST <extra_origin><idp_token_path>` continued, and nothing else:
  * the gate compares the **whole URL** as a string, so a query (even an
    empty `?`), a fragment, a trailing slash, a double slash, another port
    (or the default one spelled out), userinfo, upper case or a
    percent-encoded variant is another URL and refused;
  * only `POST`: a `PUT`, `PATCH` or `DELETE` to the same path is refused;
  * only once **the gate's own sign-in has completed** (`signed_in`) and
    holds: never before it, never after a sign-in that failed or met a
    challenge (a later failed sign-in takes it back), never after the wipe,
    and never on a target that does not sign in. During the sign-in the
    gate's own submission rule already applies;
  * a top frame posted there is still a navigation off the target and gets
    the left page; a redirect of the `POST` is a pause of its own, judged by
    its own URL;
  * every other write to the `extra_origin`, and every non-`GET` to any other
    origin, is refused exactly as before.
  The gate records each request it let through apart from its refusals
  (`allowed`, code `idp_token_refresh`, "allowed: IdP token refresh"); the
  capture's manifest names them as `gate_refusals.allowed` and the report
  names them on a line of their own, never counted as refused.
  A declared flow with `mutating: true`, on a target whose `environment` is
  `non_production`, lifts the first rule for that flow's step only. Nothing
  lifts the second. swarm-api's attempt route decides it: its answer carries
  `mutating: true` only when the stored target is `non_production`, the asking
  agent's own server-written step id is `flow-<id>` of the rendered run, and the
  stored flow `<id>` is declared `mutating: true`; every other step answers
  `false`, and nothing the worker sends is read (lane VQ4-MUTATING).
* **Never leaving the origin.** A top-frame navigation to anything but the
  approved origins is answered with a short gate page ("left the QA target")
  and recorded, the redirect-guard pattern of QA-IDENTITY.
* **A rate limit.** The gate spaces top-frame navigations by `navigation_ms`
  (default 2 s, floor 1 s) and holds target-origin requests above
  `requests_per_s` (default 10) rather than failing them; discovery honours
  the site's `robots.txt` `Disallow` lines for the user agent `*`.
* **Exclusions** (§1) are never navigated to and never clicked; the capture
  and the discovery skip them, and the flow prompt names them.
* **Personal data seen in captures stays in the tenant's artifacts.** The
  images, page text and accessibility trees are the tenant's private
  artifacts. An issue (§5) carries the finding in words, never captured page
  text beyond the few words that name the defect, run through
  `swarm_redaction.redact` and an e-mail and phone-number scrub first, and its
  screenshot by link, not by upload, unless §5's opt-in says otherwise.

---

## 4. The generic QA workflow

swarm-api renders it from the target (`swarm_api/qaspec.py`, pure, testable
offline), in the console template's step shapes, so every lesson its runs
taught (QA-REPORT-TARS, the reviewed-count check, a failed route as a
finding) carries over.

### 4.1 The steps

1. **discover** (only when the target says `discover`): one
   `claude-code-browser` step, haiku, `swarm_qa discover --base <base_url>
   --max-pages N --max-depth D` (new subcommand): same-origin links from the
   rendered pages, exclusions and `robots.txt` honoured, written as
   `screens.json` in the route-list format. swarm-api stores it on the target
   as `discovered` and starts the QA run from it; a person can promote it to
   `screens`. `--base` may carry a path, and discovery then starts there and
   keeps only the links under it, still refusing userinfo, a query, a
   fragment, an IP literal and an internal name, because an app served under
   a sub-path (`https://demo.playwright.dev/todomvc/`) was otherwise crawled
   from its origin's `/` (#1981). `--hash-routes` (off by default) also lists
   a page's links to itself whose fragment starts with `#/` as routes, never
   `#section`, because a hash-routed app's screens are otherwise one route.
   A page whose main document answered 4xx or 5xx is not listed, because the
   first external proof listed and captured a GitHub Pages 404 as a screen.

   **How a discovery is started and stored** (lane QA-DISCOVER; BGD,
   2026-10-23, after `ai-studio-staging-crawl` could not be started: "build
   discovery, so a crawl target is started entirely from the console").
   Any member starts one with **Run discovery** on the QA targets page
   (**Run discovery again** once the target has discovered screens), which
   is `POST /v1/qa-targets/{id}:discover`. It takes no body: swarm-api
   renders `render_discovery(target)` and submits it as the member's own
   workflow, its one browser step marked `metadata.qa_target` and recorded
   in `qa_target_runs` exactly as `:run`'s are, so the gate signs it in
   (start clicks included) and the same refusals apply first: an approved
   host, a registered repository, an own login's credential set. One at a
   time per target: the target's `discovering` is claimed in a transaction
   before the submission, and a second call while one runs answers 409
   (`detail.reason` `qa_discovery_running`) naming the workflow. The console says
   **Discovering…** with a link to it, and reads the list again until it
   ends.

   When the step ends, the `task_finished` push swarm-api already answers
   (`POST /v1/admin/tasks/finished`, `routes/admin.py`) settles it
   (`QaTargets.settle_discovery`). A SUCCEEDED step's `screens.json` is read
   through the API's own tenant-checked, redacted artifact read and held to
   `qaspec.discovered_screens`, as strictly as `screens` at registration: a
   route off `base_url`'s origin (another host, scheme or port) and a route
   on an excluded path are dropped, a same-origin absolute URL is kept as its
   path, past 60 (`MAX_SCREENS`) the rest is cut, and an ill-formed id or
   path or two routes sharing an id refuse the whole file. The result is
   stored as `discovered: [{id, path}]` with `discovered_at`,
   `discovered_by` and `discovered_workflow`, and the console says "N
   screens discovered, <time ago>" and enables Run. A failed step, a missing
   or refused file, or nothing left stores only `discovery_error`, the
   sentence the console shows, and keeps the previous `discovered` list:
   only a successful discovery replaces it. A wake can be lost, and an agent
   the scheduler or reconciler ends rings none, so every read of the target
   settles a discovery whose agent has ended too. Storing the list never
   bumps the target's `revision`, so a run already started keeps its mark.
   `:run` on a target with `discover` and nothing stored no longer waits for
   a discovery: it renders `discover.slots` crawling capture steps that
   start from the base path (§10). Only a target with neither `screens` nor
   `discover` is refused `qa_target_no_screens`. The standalone discovery
   stays, for a list without a run.
2. **capture-<group>**: `claude-code-browser`, haiku, as the console's, with
   the console-specific parts replaced (§4.2). Groups of at most 8 screens,
   at most 8 groups (60 screens a run, the discovery cap).
3. **review-<group>**: `claude-code`, opus, `input_from` its capture tar,
   against the checklist (§4.3).
4. **flows-<k>**: `claude-code-browser`, sonnet, with the target's
   `flows_browser_tool` (Playwright unless it says `jev`), or the one run's
   `browser_tool` when `:run` names one; recorded with `swarm-handoff record`,
   each flow at each width. The prompt names the tool's own tools:
   Playwright MCP's `browser_*`, or the jev tools. Since QA-FLOWS-CAP
   (BGD, 2026-10-26) the read-only flows render as ceil(n / `flows_per_step`)
   batch steps, `flows-1` ... `flows-<k>`, in the target's order, each one
   agent that signs in once and runs its batch's flows one after another;
   each flow ends "STOP and capture" with its own `flow-<id>.json`,
   `flows/flow-<id>-*.gif` recordings and `flow-<id>.tar`, and the step
   bundles its flows' tars into `flows-<k>.tar`, the one file it stages to
   the flow report (`input_from` stages one file a step). A batch step's
   timeout is 2,700 s (one flow's step before batching) times its flows, held
   to the profile's 7,200 s: one flow 2,700 s, two 5,400 s, three or more
   7,200 s, so 15 flows have 480 s each and 30 have 240 s, which is why 30 is
   the most. **A mutating flow is never batched**: it keeps its own
   **flow-<id>** step, because the gate grants writes per step, from the step
   id naming one flow the stored target declares mutating
   (`QaTargetIssuer._mutating`); a batch carrying one would either lose the
   grant or hand it to the read-only flows beside it. The batch prompt says
   the per-width recipe once, not under every flow, so 150 flows of
   500-character goals stay inside the 256 KiB of workflow input; a leg whose
   prompts do not is refused at render with that reason. `MAX_ISSUES` (30)
   is not met by flows at all: the **file** step files the screen findings,
   and the flows are reported in `flow-report`'s `flows.md`, never filed.
5. **report**: `claude-code`, opus, every capture and review tar staged; the
   console report's checks (reviewed against listed; a missing tar is a
   finding about the run), writing `report.md` and `findings.json` (§4.4).
6. **file**: `claude-code`, sonnet, `input_from` the report; §5.

**Why flows default to Playwright, not Jev** (lane VQ9-TOOL; BGD,
2026-10-23). Flows were first rendered with Jev on every target. The
browser benchmark's batch `bench3-2026-10-23` ([browser-bench.md](../browser-bench.md):
10 prompts x 3 tools x 3 repeats, 83 runs measured) passed Playwright MCP on
8 of 10 prompts at a median $0.14 and 44.5 s a run, dev-browser on 7, and Jev
on 6 at $0.17 and 51.3 s: Playwright was the more reliable, the cheaper and
the faster. So a target's `flows_browser_tool` defaults to `playwright`; a
target may set `jev`, and one run may choose either with `:run`'s
`browser_tool`, which applies to that run alone and is recorded on its
`qa_target_runs` row (`browser_tool`, the override or null, and
`flows_browser_tool`, the tool its flows ran with); it never changes the
target. Choosing Jev keeps Jev's requirements: where the workspace's
judgment backend is TypeSafe, a flow step is refused at submission
(`browser_tool_unavailable`, reason `no_key`) unless the submitter or the
tenant has a TypeSafe key, exactly as any agent submitted with `browser_tool: jev` is. The
capture and discovery steps are not affected: they run the fixed capture
tool (`swarm_qa capture` and `discover` on Playwright) whatever the flows use.

Workflow-level: `repository_url` the target's repository at its default
branch (read for `design_docs`; `allow_empty_diff` on every step, nothing is
pushed), `strategy: collect`, `merge: off`, `on_step_failure: continue`, so a
failed flow never costs the screen findings.

### 4.2 What in the console template is console-specific, and becomes a parameter

| in `console-qa.workflow.json` | for any app |
|---|---|
| `npm ci` and `npm run dev`, then `http://localhost:5173` (the fixture build) | gone: `--base <base_url>`, the deployed app |
| `apps/swarm-ui/qa/routes.json` and its six named groups | the target's `screens` or `discovered`, grouped by `qaspec` |
| every load empties storage (`RESET_STATE_JS`) | `--keep-storage` for `own_login` targets |
| the settle waits on the console's pending reads (`PENDING_JS`) | `--settle generic`: network idle, then no DOM mutation for 500 ms, bounded as today |
| the console fixture's `swarm_qa_persona` cookie (`PERSONA_COOKIE`) on every route | not set under `--settle generic` when no route names a `persona` (BGD, 2026-10-22); a generic route list that names one keeps it, and `PERSONAS` still refuses an unknown one |
| the clock frozen at the route list's instant | off by default (`--clock` only when the target sets one): freezing `Date` can expire an app's own session token |
| the three console flows | the target's `flows` |
| the console's review list | §4.3's checklist plus the app's `design_docs` |
| `repository_url` SwarmCloud | the target's registered repository |

Nothing else changes: presets by name, the theme set twice, segments, the
manifest, the tar names, the findings shape.

Two checks hold for every capture, the console's included (#1981). A route
whose main document answered 4xx or 5xx is a failed route at `navigate`,
with the status, not a captured one. A subresource that answered 4xx or 5xx
is in the route's `errors.json` as `bad_responses`, its URL (origin and
path) and status, because an HTTP error is not a failed request, and the
console's "Failed to load resource" line carries no URL. A shot is checked
against the page's layout viewport read at the shot (innerWidth and
innerHeight over the visual viewport's scale), not the preset's nominal
size, because a page with no viewport meta tag is correctly laid out at
980 px on a phone and Playwright's first shot of it is a faithful 980x2121
(QA-DEVICE, Chrome 153): what was wrong were the later shots, after each
clipped capture lifted the gate's device, which the gate now puts back
(`DeviceController.reassert`, QA-REASSERT).

The gate's refusals are the run's, never the app's (QA-GATE-REFUSALS, part
of #2067). A 403 the gate makes for a request to another origin carries no
`Access-Control-Allow-Origin`, so Chrome reports it as a CORS block and a
failed request, exactly as if the app's own server had misconfigured CORS. On
2026-10-23 the AI Studio run met that on every route (its OIDC token
exchange, a `POST` to its identity provider) and filed the gate's refusal into
the app's repository as a blocker. So the gate keeps a record of every
request it answered itself (`QaTarget.refusals`: time, method, origin, path
and code, never a query or a body), the capture writes the refusals made while
each screen loaded into that screen's `errors.json` as `gate_refused`, marks
the failed request they caused as the gate's, and counts them in the
manifest's `gate_refusals`; the review puts such a screen in `about_run`, the
report opens with "the gate refused N requests, so these screens show the app
without them", and the file step never files a finding whose evidence is a
refusal. The rules of §3 do not change for this: whether a target may let its
identity provider's token endpoint through is the owner's decision, held. The
capture reads the record at `GET /json/swarm/qa-refusals` on the gate's CDP
endpoint, which it already connects to (the gate's agent endpoint answers it
from `CdpProxy.qa_refusals`); a gate holding no QA target answers 404, and
whenever the record cannot be read the capture writes `gate_refused: null` and
the manifest says so, never an empty list.

### 4.3 The review checklist

Stated in the rendered prompt, one finding per defect: layout overflow and
clipping; alignment and spacing; truncated or overlapping text; contrast,
in dark mode especially (and axe-core's report, `--axe`, on every screen);
what differs between 1440 and 390 that should not; broken or missing images;
console errors, page errors and failed requests from the manifest; empty,
loading and error states that read wrong; text that contradicts itself or
the page's purpose; a route that failed to capture. When the repository holds
files matching `design_docs`, the review reads them first and cites a
deviation from them by file.

### 4.4 The finding

`findings.json`, one entry per defect (the visual-qa.md §4.4 shape, with the
fields a filed issue needs):

```json
{
  "id": "g2-004",
  "title": "The pricing table overflows its card at 390 in dark mode",
  "severity": "major",
  "screen": "pricing",
  "path": "/pricing",
  "width": "phone-390",
  "theme": "dark",
  "evidence": ["g2-004.png", "g2/pricing--phone-390--dark--s2.png"],
  "actual": "The third column is cut off at the card's right edge.",
  "expected": "The table wraps or scrolls inside its card, as at 1440.",
  "steps_to_reproduce": ["Open /pricing at 390 px wide", "Set the system theme to dark", "Scroll to the plans table"],
  "also_seen": [{"width": "desktop", "theme": "dark"}]
}
```

`severity` is `blocker`, `major`, `minor` or `nit`. A finding with no
evidence file, or evidence the report cannot find in a staged tar, is
refused by the report and becomes a finding about the run.

---

## 5. Filing

Through agent-autonomy.md §3.2's brokered tools, in the target's repository,
which is the run's own repository, so the existing "own repository only" rule
holds unchanged:

* **The registration must allow it.** Owner decision Q2 of agent-autonomy.md:
  `agents_may_file_issues` on the repository's registration, admin-set, off
  by default. Off, the run is refused at `:run` with `issue_writes_off`
  naming the setting, before anything spends, unless the run was asked for
  `file: "none"` (report only).
* **One issue per finding** at or above `min_severity`, at most `max_issues`
  (default 20, never above agent-autonomy.md §3.2's 30 per agent). Findings
  beyond it are listed, one line each, in one roll-up issue "Visual QA of
  <name>, <date>: <n> further findings", so nothing is silently dropped.
* **De-duplicated by the broker.** The `file` step calls `issue_create` with
  `dedupe_key: "visual-qa:<target_id>:<environment>:<screen>:<normalised title>"`,
  the leg's name in it since #2136 (§10).
  swarm-api writes `<!-- swarm-qa target=<id> key=<sha256[:12]> -->` into the
  body and, before filing, searches the repository's open issues for that
  marker; a match answers `{existing: N}` and files nothing, and the step
  comments on `N` instead (`issue_comment`: the run, the date, the new
  evidence link). An open issue a person filed for the same defect is found
  by `issue_list` on the screen's path and the title's words; the step
  comments there and says so in the report.
* **The body** uses the repository's bug form when it has one, otherwise
  `form: "plain"` with the sections Summary, Where (URL path, width, theme),
  Actual, Expected, Steps to reproduce, Evidence, and "Found by SwarmCloud
  visual QA run <id>".
* **Screenshots.** Artifacts are private to the tenant. Default
  (`evidence: "console_link"`): the issue links each evidence file's console
  URL (the run's Artifacts pane), readable by the tenant's members, and the
  body describes the defect fully in words so it stands without the image.
  The opt-in (`evidence: "branch"`, a private repository only): the step
  pushes the evidence PNGs to its own `swarm/<id>` branch through
  `git_push_branch` (its existing push rule) and embeds them by blob URL, so
  everyone who can read the repository sees them. The console link is the
  default and the branch the per-target opt-in (VQ-Q3, answered, §9).
* **Labels**: `bug` (or the form's), the target's `labels` (default
  `visual-qa`), and `severity:<s>` when the repository has such a label;
  only labels that already exist are applied (§3.2's rule), never created.
* **Labels by route** (`file.label_rules`, #2062): `[{paths: ["/agents/"],
  labels: ["agent-page-rework"]}]`. A finding whose `path`, without its
  query, starts with one of a rule's prefixes also gets that rule's labels,
  on top of `labels`; every matching rule adds its own, and a finding no rule
  matches is filed with `labels` alone. AI Studio asked for it on 2026-10-23:
  their `/agents/{id}` page was being replaced, and they re-check those
  findings against the new build instead of fixing the old one, which until
  then meant labelling them by hand after the file step. The prefixes are
  origin-relative like the finding's `path`, and each is refused at
  registration, with the field named (`file.label_rules[i].paths`), unless it
  starts with one `/` and carries no scheme, host or query, because a
  prefix that can never equal a route's start is a rule that silently never
  fires. At most 20 rules, 1..10 prefixes and 1..10 labels each, the labels
  held to `labels`' own name rule. The file step's prompt states each rule,
  numbered, and `filed.json` records for each issue the rules that labelled
  it, so the run's report shows which rule labelled which issue. The step is
  an agent, so `qaspec.finding_labels` is the rule written once in code, the
  one its tests hold.

---

## 6. Triggers

* **On demand**: the console (Repositories → a repository's QA targets →
  Run, with "Flows with" for one run's flow browser), the API
  `POST /v1/qa-targets/{id}:run` (body: optional `environments`, the
  entries to run by name, default every one; optional `screens` subset,
  `file: "issues" | "none"`, optional `browser_tool: "playwright" | "jev"`
  for this run's flows, §4.1, optional `flows_per_step`, 1-30, how many
  flows one live-browser step runs, over the workspace's `qa_flows_per_step`
  and its default 15), and the plugin's `swarm_qa_run` (the same
  arguments). A
  member may run; the run is that member's submission in the tenant, one
  workflow per leg linked by a run group (§10).
* **A schedule type `visual-qa`** (optional): tenant-scoped, created by a
  workspace admin, parameters `target` (a target of the same tenant) and
  optional `environments`, minimum interval 24 hours, budget fields set by
  its creator like every type. It fires the same `:run` path, one work item
  per leg ([schedules.md](../schedules.md) §3.17). No merge gate exists to set: it files issues
  and changes no code.

---

## 7. New routes, tools, collections, IAM and secrets

| kind | name | owner at dev-iam |
|---|---|---|
| collection | `qa_targets/{target_id}`; `qa_run_groups/{group_id}` (§10) | no |
| routes | `POST/GET/PATCH/DELETE /v1/qa-targets`, `:approve-host` (platform admin), `:run`, `:discover`, `/credential-links`, `PUT /credential`, `PUT /challenge-bypass` (§2.5); `POST /v1/qa-targets:migrate` (platform admin) and `GET /v1/qa-runs/{group_id}` (§10) | no |
| worker route | `POST /v1/attempts/{attempt_id}/qa-target` | no |
| gate | `ctl.sock` verb `qa_sign_in`; the method rule, origin lock and rate limit (`livegate/qatarget.py`); the scrub of one more value | no |
| capture tool | `swarm_qa capture --keep-storage --settle generic`, `capture --crawl` (§10), `swarm_qa discover` | no |
| renderer | `swarm_api/qaspec.py` | no |
| plugin tools | `swarm_qa_targets`, `swarm_qa_run`, `swarm_qa_target_credential_link` | no |
| schedule type | `visual-qa` | no |
| **secret** | `swarm-tenant-<tenant>-qa` per tenant, declared empty by Terraform (`managed-by=swarm-terraform`), its versions added by swarm-api from the console form only | **yes** (VQ-Q2) |
| **IAM V-1** | swarm-api's account: `roles/secretmanager.secretVersionAdder` on each `swarm-tenant-<t>-qa` (the per-secret binding `modules/secret_manager` already makes for provider keys) | **yes** (VQ-Q2) |
| **IAM V-2** | swarm-api's account: `roles/secretmanager.secretAccessor` on each `swarm-tenant-<t>-qa`; no worker account gets it | **yes** (VQ-Q2) |
| frozen contract | **none**: the target travels as swarm-api-written task metadata, not an input (§1.1) | no |

**What stays the owner's**: the IAM apply and the secret, and any change to
the safety rules of §3. Approving a host is any platform admin's, audited
(VQ-Q1).

---

## 8. Later options, not version 1

* **Apps behind Google IAP.** Would need QA-IDENTITY generalised from the
  console to a target: a service account per target admitted by **that
  app's** IAP (an IAM binding in the app owner's project, which this platform
  cannot make), a JWT whose audience is the target's host, and the frozen
  `qa_identity` choices widened beyond `"console"` (a contract change, as
  QI-1 was).
* **A repository's mock or fixture build.** Would need the build command run
  in the browser pod, as the console template runs `npm run dev`. A command
  from a caller is what invariant 10 forbids, so it would have to be a
  registered, admin-approved build declaration per repository, network-free,
  with its toolchain in the browser image.

---

## 9. Questions for the owner: answered

Owner decisions, 2026-10-22 ~06:40Z (BGD, recorded on
task_1eefab7ffc1941d58986). Each was asked with the options below,
recommended first; the answer is built, not reopened.

* **VQ-Q1, who approves a QA target's host** (§1.2). Owner decision,
  2026-10-22: **"Any platform admin, audited"**. The options were (a) any
  platform admin, audited; (b) the owner alone; (c) no approval, any public
  https host a workspace admin registers.
* **VQ-Q2, where the test credential lives** (§2.1, §7). Owner decision,
  2026-10-22: **"One secret per tenant, swarm-tenant-<t>-qa"**, a map by
  target, with V-1 and V-2 per secret (lane VQ-3, at dev-iam). The options
  were (a) that; (b) a secret per target, created by swarm-api, which needs a
  name-prefix-conditioned `secrets.create` on the project; (c) no own-login in
  version 1.
* **VQ-Q3, how an issue shows its screenshot** (§5). Owner decision,
  2026-10-22: **"Console link by default; evidence branch as a per-target
  opt-in for private repositories"** (`file.evidence`, default
  `console_link`). The options were (a) that; (b) a console link only; (c)
  the evidence branch by default.

---

## 10. Environments, the run group and discovery during QA (built, #2136)

BGD asked on 2026-10-23 for one dispatch to QA an app's staging and
production legs together, discovering routes while it captures, because a
run over a stored route list misses what shipped since. The design and its
reasons are [qa-environments.md](qa-environments.md); BGD's answers are its
§10, and what each lane built is its §11. Each paragraph below says what
changed in this document's model.

**Environments.** A target may carry `environments[]`, at most 8 entries,
each a leg with its own `name`, `base_url`, `environment` mark, sign-in
(start clicks and `extra_origin` included), credential, bypass, host
approval, `read_posts` and `rate`; screens, flows, exclusions and filing
stay on the target once ([qa-environments.md](qa-environments.md) §1.1).
`:approve-host`, `:run`, `:discover` and the credential link name the
entry, and a link minted for one leg can never set another's password. The
tenant's one QA secret (VQ-Q2) stays one secret with no new IAM: an entry's
values sit under `values["<target_id>/<name>"]`. A target stored in the
one-origin shape reads as one entry until a platform admin's
`POST /v1/qa-targets:migrate` rewrites it, audited `qa_target_migrated`.

**The run group.** `:run` takes `environments` by name and checks every
named leg before submitting any, so one leg's pending host or missing
credential refuses the whole dispatch and nothing spends. It writes
`qa_run_groups/{group_id}` with every leg `pending`, then submits one
workflow per leg, each with its own one mark, so the single-mark rule
(VQ2-RESERVE) is unchanged; a leg refused after another started answers
207 and is shown "not started" with its reason. `GET /v1/qa-runs/{group_id}`
serves the group in its own tenant, and the console's run page,
`/qa-runs/<group_id>`, draws one column per leg
([qa-environments.md](qa-environments.md) §2.1, §7).

**Capture steps crawl.** Each capture step captures its stored routes from
minute zero, then walks the same graph from the base path and every stored
route and captures the new routes it owns, the owner fixed by a hash of the
path so two steps never capture one route (`swarm_qa capture --crawl`).
`found_per_step` defaults to 8, BGD's answer of 2026-10-23, and is cut so
stored plus new routes stay within 60 a leg; a crawling step's timeout is
2,700 s. Every step writes `found.json` and the report gains a coverage
section per leg. A discovering target with nothing stored renders
`discover.slots` (default 4, at most 8) crawling steps instead of waiting
for §4.1's discovery ([qa-environments.md](qa-environments.md) §3.1, §3.5).

**Disclosure clicks.** The walk, capture's and §4.1 discovery's alike, reads
more link sources and `#/` hash routes, and clicks only controls that
reveal: `aria-expanded="false"`, `aria-haspopup`, `role=tab` and
`<summary>`. It never clicks a control inside a `<form>`, a submit or reset,
an input, an off-origin link, or anything carrying an exclusion word (§1's
defaults plus logout and sign out) or matching an exclusion selector. A
click the gate refused marks its control `write_attempting`, never clicked
again and listed for a person. Bounds: 20 clicks a page and 200 a crawl,
600 s a crawl, every navigation spaced by `navigation_ms`
([qa-environments.md](qa-environments.md) §4).

**Per-leg `discovered`, and Promote.** When a leg's capture steps end, the
existing `task_finished` doorbell settles the run: its `found.json` files
are read through the tenant-checked, redacted artifact read, held to the
same rules as §4.1's `discovered_screens`, and merged into **that leg's**
`discovered` with `first_seen`, `last_seen` and the workflow that last saw
each route; a route unseen by 3 complete crawls in a row is dropped. A run
never writes `screens` or bumps `revision`. Each leg serves
`new_since_last_run`, and the card offers **Promote** for each, a workspace
admin's PATCH of `screens`. A target with `screens` runs `screens` alone,
so on it a found route is captured again by the crawl or once promoted
([qa-environments.md](qa-environments.md) §5, §11).

**Filing per environment.** One issue per finding per leg (BGD's Q8 (a)):
the dedupe key is `visual-qa:<target_id>:<environment>:<screen>:<title>`,
titles start `[<leg>]` (`[staging]`, `[production]`), and a grouped run's
issue links `/qa-runs/<group_id>`, where the other leg's run is. The two
legs' file steps never race on one key
([qa-environments.md](qa-environments.md) §7).

**The shared-origin rule.** When a `non_production` entry shares any origin
with a `production` entry of the same target, it is treated as production
for writes (`apps/swarm-api/swarm_api/qaspec.py::shares_production_origin`):
the worker route answers `mutating: false` for every step of it, and no
mutating flow is rendered for it or for any production leg of a target of
several entries. The rule compares the entries of one target only: AI
Studio's legs share `https://studio.saga.xyz` behind a Staging | Production
toggle, and they are held read-only by it only while they are entries of one
target (qa-environments.md §11 says how they are registered today). The
gate holds one leg per attempt: a second
`qa_sign_in` hand-off whose `(target_id, environment_name)` differs is
refused `target_changed` before any field of it is taken
([qa-environments.md](qa-environments.md) §1.5, §6).

Nothing of §3 is softened. Every rule there holds on every leg, production
legs read only (BGD's Q6 (a)), the production account is an MFA-free,
least-privilege QA account rather than a solved challenge (Q7 (a), §2.4),
and the shared-origin rule only ever turns a write permission off.
