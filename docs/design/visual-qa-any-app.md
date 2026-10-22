# Visual QA of any web application, its findings filed in that application's repository

**Status: DESIGN, not built.** Lane AUTONOMY-DESIGN, 2026-10-22, part of
#1933. Built by lanes VQ-1 to VQ-7 of [agent-autonomy.md](agent-autonomy.md)
§6, on that document's capabilities (above all the brokered issue writes of
its §3.2). Nothing here changes behaviour until those lanes merge. The
owner answered §9's three questions on 2026-10-22 (~06:40Z).

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
| password fields | **masked for people, cleared on return** | the gate's mask paints every input that is or has been `type=password` out of the screencast (`livegate/mask.py`); on the return from a person's control it clears every password input before releasing the agent (`livegate/cdpproxy.py`, "THE ORDER ON RETURN") |
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
| `screens` | a list `{id, path, ready?, segments?}`, or `discover: {max_pages ≤ 60, max_depth ≤ 3}` | the capture's route list; discovery is bounded and same-origin (§4.1) |
| `flows` | `{id, goal, start_path, mutating: false}` each | goal-driven flows for jev or Playwright; `mutating: true` is refused unless `environment` is `non_production` (§3) |
| `widths` | presets by name, default `["desktop", "phone-390"]` (1440 and 390) | the gate's presets, never a viewport override (the gate refuses one) |
| `themes` | default `["light", "dark"]`, through emulated media | as the console's capture sets them |
| `exclusions` | path globs never visited, element texts or selectors never clicked | always merged with the defaults: `/logout`, `/signout`, and controls whose text names delete, remove, pay, buy, purchase, send, invite, unsubscribe or cancel subscription |
| `environment` | `production` (default) or `non_production` | §3: only a non-production target can run a mutating flow |
| `read_posts` | target-origin paths whose `POST` is a read (a GraphQL endpoint) | §3's method rule would otherwise refuse them |
| `design_docs` | path globs in the repository, default `DESIGN.md`, `docs/design*/**`, `**/style-guide*` | §4.3: the review reads the app's own design where it has one |
| `file` | `{min_severity: "minor", max_issues: 20, labels: ["visual-qa"], evidence: "console_link"}` | §5 |
| `rate` | `{navigation_ms: 2000, requests_per_s: 10}` | §3; a registrant may make it slower, never faster than 1 navigation a second |
| `authorized_by`, `attested_at` | the registrant's statement that the tenant may test this application | someone else's app: the platform does not decide whose it is, but it records who said it may be tested |
| `host_state` | `pending`, `approved` (with `approved_by`, `approved_at`) or `revoked` | §1.2 |

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
  `pending`. The approver is any platform admin, audited (VQ-Q1, answered,
  §9). In the console the control is **Approve host** on the target's card
  on Repositories › QA targets (`apps/swarm-ui/src/QaTargets.tsx`), drawn to
  a platform admin only, which sends the route above once the admin has
  typed the host, and the login's `extra_origin` when one is declared.
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
   The link is single-use, bound to the asking person's email and the target,
   and expires in 15 minutes.
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
   screencast's mask already paints password fields; a field typed by the
   gate is cleared before any agent capture can exist.

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
2. **capture-<group>**: `claude-code-browser`, haiku, as the console's, with
   the console-specific parts replaced (§4.2). Groups of at most 8 screens,
   at most 8 groups (60 screens a run, the discovery cap).
3. **review-<group>**: `claude-code`, opus, `input_from` its capture tar,
   against the checklist (§4.3).
4. **flow-<id>**: `claude-code-browser`, sonnet, jev for goals or Playwright,
   recorded with `swarm-handoff record`, one per declared flow, at each width.
5. **report**: `claude-code`, opus, every capture and review tar staged; the
   console report's checks (reviewed against listed; a missing tar is a
   finding about the run), writing `report.md` and `findings.json` (§4.4).
6. **file**: `claude-code`, sonnet, `input_from` the report; §5.

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
  `dedupe_key: "visual-qa:<target_id>:<screen>:<normalised title>"`.
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

---

## 6. Triggers

* **On demand**: the console (Repositories → a repository's QA targets →
  Run), the API `POST /v1/qa-targets/{id}:run` (body: optional `screens`
  subset, `file: "issues" | "none"`), and the plugin's `swarm_qa_run`. A
  member may run; the run is that member's submission in the tenant.
* **A schedule type `visual-qa`** (optional): tenant-scoped, created by a
  workspace admin, parameter `target` (a target of the same tenant), minimum
  interval 24 hours, budget fields set by its creator like every type. It
  fires the same `:run` path. No merge gate exists to set: it files issues
  and changes no code.

---

## 7. New routes, tools, collections, IAM and secrets

| kind | name | owner at dev-iam |
|---|---|---|
| collection | `qa_targets/{target_id}` | no |
| routes | `POST/GET/PATCH/DELETE /v1/qa-targets`, `:approve-host` (platform admin), `:run`, `/credential-links`, `PUT /credential` | no |
| worker route | `POST /v1/attempts/{attempt_id}/qa-target` | no |
| gate | `ctl.sock` verb `qa_sign_in`; the method rule, origin lock and rate limit (`livegate/qatarget.py`); the scrub of one more value | no |
| capture tool | `swarm_qa capture --keep-storage --settle generic`, `swarm_qa discover` | no |
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
