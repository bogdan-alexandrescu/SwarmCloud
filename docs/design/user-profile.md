# User profile: one page that says who you are, where you work, what reaches your bell and which Claude accounts are yours

**Status: designed 2026-10-18 (lane PROFILE-DESIGN, part of #1505); revised
2026-10-18 (lane PROFILE-DESIGN-B) for the owner's answers to §7, given on
2026-10-17/18 in PR 1506's comments.** Nothing here is built. The owner
approves this revision before any build lane starts. Two questions the answers
left open, Q11 and Q12, were answered on 2026-10-18 (§7.1): the edited name also names the agent's commits, and only names of two
or more words are checked for containment. Nothing is open (§7.2).

Every file and symbol cited below was read on 2026-10-18 at `6174f32`, and the
ones this revision added were read at `8ab9f26`. Lines move, so each citation
names the symbol; search for it.

The owner's request (#1505, 2026-10-18): a **Settings › Profile** page with
four sections, all chosen by the owner:

1. identity and workspaces, with a default workspace for the console and for
   `/sc`;
2. a summary of the person's connections, **linking** to the pages that own
   them, which stay the source of truth;
3. notification preferences: what reaches the bell (approvals, steps waiting
   for them, failed runs) and how often reminders repeat;
4. their personal Claude subscription accounts and quota, linked from
   Capacity › Accounts.

What the design settles, in one line each:

* **The page reads; it edits only what has no other home.** The display name,
  the default workspace and the notification preferences are the only things
  saved from Profile. Everything else is a summary that links to the page that
  owns it, so there is never a second editor for the same thing (§4.1).
* **Preferences live in one per-person document that no worker can find.**
  `user_preferences/<id>`, where the id is an HMAC of `"user-prefs:" + email`
  under the existing `swarm-child-key`, which only swarm-api and the scheduler
  hold (Q1). It is written only by swarm-api, only for the caller, and never
  inside a tenant document. **Only its owner reads or changes it**: there is
  no admin route and no `?user=` parameter (Q8, §4.3).
* **The display name is the person's own, and it cannot pass for someone
  else.** It is edited on Profile and stored in the preferences document
  (Q5). Wherever another person sees it, the email is beside it. It is 1-64
  characters of letters, marks, spaces and `.'-`, in one script, with no `@`.
  It may not equal or contain another person's name. Every change writes an
  `admin_audit` entry, so admins see its history (§4.4).
* **The default workspace is applied by the API, not by each client.** A
  request without `X-Swarm-Tenant` acts in the stored default when the person
  is still a member of it. Any explicit choice still sends the header and
  still wins. A console switch lasts for the tab's session, so a new window
  opens in the default (Q2). If the preferences cannot be read, a request that
  names no workspace fails with 503 `preferences_unavailable` and nothing is
  submitted; it never falls back silently (Q9, §4.5).
* **Roles are today's derived roles, behind one seam.** Owner of the personal
  workspace, member of a group through its address, and platform admin shown
  once. `workspace_role.py::role_in` computes them and serves one API field,
  `role: {name, source: "derived", via}`. Per-workspace roles come from a
  separate design (#1518, lane ROLES-DESIGN). That build replaces the
  function's body and leaves the page alone (Q7, §4.2).
* **The bell stays derived on read.** Preferences are a filter applied after
  the sources derive their items, and a reminder is a read mark that has aged
  out. Every topic may be muted, and a quiet line under the bell says how
  many muted items need the person (Q3). Nothing is queued, stored or sent
  (§4.6).
* **Connections are GitHub and the TypeSafe key, nothing else.** SwarmCloud
  has no personal API keys, and the page has no row for them (Q6). GitHub
  shows one row for every workspace the person can act in, from one batched
  read (Q10).
* **No credential value is ever on the page**, and neither are the names a
  credential is stored under. Keys show set or not set, when, by whom and
  their version. The response is built from an allow-list of fields, and a
  test plants a value and looks for it (§4.2).

---

## 1. The problem, as measured

### 1.1 Where "me" lives today

The issue's table, checked against the code on 2026-10-18:

| What | Where it lives | What the code shows |
|---|---|---|
| TypeSafe key (personal and workspace) | Settings › API keys, `/settings/keys` | `apps/swarm-ui/src/ProviderKeys.tsx::ProviderKeysScreen`, over `GET /v1/provider-keys/{provider}` (`apps/swarm-api/swarm_api/routes/provider_keys.py::get_status`). The only provider is TypeSafe. The same page also picks the Jev judgment backend. |
| GitHub connection and repository grants | Work › Access, `/access` | `apps/swarm-ui/src/Access.tsx::AccessScreen`, over `GET /v1/access` (`routes/access.py::overview`). The connection is **per workspace and person**: `forgeapp.py::connection_id_for(tenant_id, email)`. |
| Personal Claude accounts | Capacity › Accounts, `/capacity/accounts` | `apps/swarm-ui/src/Accounts.tsx::AccountsScreen`, over `GET /v1/accounts` (`routes/accounts.py::list_accounts`), scoped to the **acting** workspace. "Personal" is not a field. It is implied by `owner_tenant` being a `u-` workspace. |
| Onboarding | Work › Setup, `/setup` | the workspace request and the loan request (`routes/workspaces.py`) |
| Who I am | **no page** | `GET /v1/tenants/me` returns `principal.email`, `domain`, `groups`, `is_admin`. `AuthContext.display_name` exists only when the token carries a `name` claim, and an IAP assertion never does (`auth.py::AuthContext`). Its one use today is the author name on the agent's commits (`gitidentity.py::for_caller`, contract request P37). No page shows a person's name to anyone else. |
| My workspaces and my role in each | **no page** | `GET /v1/tenants/mine` lists the choices (`routes/tenants.py::get_mine`). There is **no per-workspace role**: `routes/provider_keys.py` and `routes/gittokens.py` both say "there is no per-tenant admin role". The only roles are platform roles, `admin_roles/{email}` (`admins.py`). |
| A default workspace | **no page, and not stored** | The default is computed on every request: the personal `u-` workspace when it is `ready`, else the first confirmed group (`auth.py::Authenticator._with_own_workspace`, owner decision 2026-10-12). |
| Notification preferences | **no page, and no concept** | The bell (`apps/swarm-api/swarm_api/notifications.py`) derives 5 groups from 8 sources on each read. Nobody can turn any of them off. A grep of `apps/` for `remind` finds nothing, so nothing repeats either. |

The issue also lists personal SwarmCloud API keys. SwarmCloud has none: `/sc`
and the bridge sign in with the person's Google identity
(`apps/swarm-mcp/swarm_mcp/auth.py`). The owner decided on 2026-10-18 (Q6)
that the page leaves them out.

So the issue's "spread across five pages, and some of it has no page at all"
is exact: four pages hold four things, and four things have no page.

### 1.2 What it costs today

* **A person in two workspaces cannot see both GitHub connections at once.**
  The connection is keyed by workspace, but Access shows only the acting one.
  To see whether their token in group B has failed, a person switches to B.
* **A person whose work is in a group has to switch on every visit.** Since
  2026-10-12 the default is the personal workspace. A person who works in
  `team-x` and has a ready personal workspace starts every console session
  and every `/sc` session in the wrong place. In the console, the switch is
  remembered in `localStorage` (`apps/swarm-ui/src/fetch.ts`, `TENANT_PREF`),
  so it is per browser. In `/sc`, it has to be set as the plugin's `tenant`
  option or `SWARM_TENANT`, per machine.
* **The bell is all or nothing.** A person who submits dozens of tasks a day
  sees up to 50 `finished` updates (`notifications.py::MAX_UPDATES`). Those
  bury the one failed run among them, and nothing can mute the successes.
* **A needs-you item read once never rings again.** An approval marked read
  on Monday is still pending on Wednesday and shows as read. The person-waits
  design (`docs/design/human-steps.md` §4.5) already needs a reminder period,
  but only the step's author can set it.
* **People are emails everywhere.** Approvals say "by ada@example.com", run
  pages say "by ada", and People lists addresses. A person whose IAP sign-in
  carries no name has no way to give one.

### 1.3 What "done" lets someone do that they cannot today

* Open Settings › Profile and see, on one screen, their identity, every
  workspace they can act in with their role and its state, the GitHub
  connection **in each workspace**, their TypeSafe key's state and version,
  and their own Claude accounts with each one's 5-hour and 7-day use.
* Set a display name that colleagues see on People, approvals and run pages,
  always beside their email.
* Pick a default workspace once. A new console window, a new `/sc` session,
  `swarm` and the MCP bridge then all open in it, from any machine, unless
  that session explicitly chose another.
* Mute what they do not want in the bell (successful runs, say), keep what
  they do (failed runs, approvals, steps waiting for them), and have a
  pending item ring again every 4 hours or every day until it is resolved.
* Do all of it without the page ever showing a credential, or a name it is
  stored under.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **Any tenant worker can get, create and update any Firestore document whose
  id it can compute** until design B of
  [tenant-settings-integrity.md](tenant-settings-integrity.md) removes the
  `swarmTenantWorkerFirestore` binding. It has no `list`. The agent can mint
  its worker's token, so the agent has the same reach (that document's §1.1).
  B is being built: the state client is wired (ISO-WIRE, `6174f32`). The role
  is still bound (`terraform/modules/tenancy/main.tf`,
  `resource "google_project_iam_member" "worker_firestore"`). A per-person
  document keyed by something a worker can compute, such as `u-<slug>` or
  `forgeapp.user_hash(email)`, is therefore readable and writable by any
  tenant's agent that knows the person's address. `notification_reads` has
  that exposure today, and so does `people/u-<slug>`. This is why the
  preferences document **and** the display-name claims are keyed by HMACs
  (§4.3, §4.4).
* **There are no Firestore security rules.** Every component uses a server
  SDK, so IAM is the only boundary (`docs/security.md`, rules section;
  tenant-settings-integrity.md §2.1).
* **The tenant header picks; it never grants.** `X-Swarm-Tenant` can only
  select among `AuthContext.tenant_choices` (`auth.py::Authenticator._select_tenant`).
  `GET /v1/tenants/mine` promises that its first entry is what a headerless
  request acts in (`routes/tenants.py::get_mine`, docstring). The default
  workspace has to keep that promise, not add a second rule beside it.
* **Group membership costs a Cloud Identity call per registered group.**
  `searchTransitiveGroups` returns 403 in this project, so `groups.py::CloudIdentityGroups`
  checks each group and caches each answer for 120 s, and every call carries
  `x-goog-user-project`. Listing a person's workspaces must reuse the
  memberships the request already resolved, never ask again. The same fact
  rules out "unique among the members of your workspaces" as the display-name
  check: listing a group's members is the call this project cannot make. The
  check is platform-wide instead (§4.4).
* **The personal workspace id is derived from the email by the frozen
  `swarm_common/identity.py::tenant_id_for_user`.** The slug drops the domain,
  so `bob@a` and `bob@b` derive one id. `auth.PersonalTenants.ready` tells
  them apart by the stored principal. Anything that names "your personal
  workspace" must go through that check, not through the slug alone.
* **`AuthContext.display_name` already reaches commits.** It is cleaned by
  `gitidentity.py::clean_name` (no `<`, `>` or control characters, at most
  `NAME_MAX_CHARS` = 100) and recorded in the task's dispatch block, which the
  worker turns into `GIT_AUTHOR_*` and `GIT_COMMITTER_*`. The edited
  name also names commits (Q11, §7.1), so the rules in §4.4, which are
  stricter than `clean_name`, apply to what reaches git.
* **The Settings tabs are not in `SECTIONS`.** They are constants in
  `apps/swarm-ui/src/App.tsx` (`SETTINGS`, `KEYS_TAB`, `WORKSPACE_TAB`).
  `tests/unit/scripts/test_issue_forms.py::_nav_options` adds them to the
  forms' expected "Where" list by hand. A new Settings tab therefore needs
  three changes together: the tab, a "Web UI · Settings › Profile
  (/settings/profile)" option in both `.github/ISSUE_TEMPLATE/bug_report.yml`
  and `feature_request.yml`, and the test's hand-written list. One lane makes
  all three (§6, PROFILE-NAV).
* **Per-workspace roles are being designed elsewhere.** Issue #1518 (lane
  ROLES-DESIGN, running on 2026-10-18) introduces them. This design must not
  define roles, and must not leave the page knowing how today's roles are
  derived, or the roles build would have to edit it (§4.2, `role`).

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Untouched.
Profile reads documents and writes one document of its own, plus a name
claim and an audit entry when the name changes. A reminder is a read mark
that has aged, not a task, a timer or a job. Nothing waits, so nothing costs
while it waits. A headerless request refused with 503
`preferences_unavailable` is refused before admission, so it creates no task
and no demand.

**2. All-or-nothing reservation.** Untouched. No pool, lease or slot is read
for writing. The Claude-account summary reads the broker's account list
through `brokerclient.AccountPool.list_accounts`, the read Capacity ›
Accounts already makes. It never assigns or holds an account.

**3. Concurrency counts from LEASED.** Untouched, for the same reason.

**4. Workers never sleep through a long wait.** Untouched. No worker code
changes.

**5. Fencing.** Untouched. No attempt, lease or generation is read or
written. The default workspace is resolved **before** a submission is
admitted, exactly where the computed default is resolved today. A task
records the tenant it was admitted in, and a later change of default never
moves it. A changed display name never rewrites a task already admitted: the
name a task's commits carry (Q11, §7.1) is the one recorded in its
dispatch block when it was admitted, as P37 already records it.

**6. Spot disabled; 7. requests == limits.** Untouched. No infrastructure.

**8. Mandatory checkpointing.** Untouched. No worker or runner change.

**9. Per-tenant isolation.** This is the invariant the design has to argue,
in six places:

* *The default cannot widen access.* A stored default is honoured only when
  it is still in the `tenant_choices` this request resolved from the verified
  identity. A forged, stale or foreign value is ignored, and today's rule
  applies. Selecting a workspace still needs the same membership it needs
  today.
* *The default cannot be planted by another tenant's agent.* If an agent
  could rewrite a person's default to group B, where the agent's owner is
  also a member, that person's next headerless `/sc` task would land in B,
  and B's members would read its prompt and its diff. That is a cross-tenant
  leak even though every membership check passes. So the document's id is an
  HMAC under a key no worker holds (§4.3). With no `list`, no worker can find
  the document, until B removes worker Firestore access entirely.
* *An unreadable default never becomes a guess.* If the preferences cannot be
  read, a headerless request fails with 503 rather than acting in the
  computed default (Q9). A guess would run work in a workspace the person may
  have chosen not to use, which is the misfiling the 503 exists to prevent.
* *Profile shows the person's own things across workspaces, and nothing of
  anyone else's.* It reads, per workspace the caller is in, only documents
  keyed by **(that workspace, this email)**: the GitHub connection. It reads
  the TypeSafe status of the caller's own `u-` scope and of the acting
  workspace, which the API-keys page already shows them. It reads the Claude
  accounts **owned by** or **lent to** their own personal workspace. A group
  workspace's pool accounts, other members' connections and other members'
  keys are never read.
* *A display name tells nobody anything they did not already know.* A name is
  served only **beside an email the same response already serves** (§4.4,
  "Where others see it"). It crosses workspaces by design, because it is one
  person's name everywhere, but it adds no membership, workspace or activity
  fact to any response.
* *Preferences never enter a tenant document, and no one but their owner
  reads them.* Not `tenants/<t>`, not `workspace_settings/<t>`, not
  `people/<u->`. The scheduler, the reconciler and the worker never read
  `user_preferences`. Only swarm-api does, for the caller, plus the display
  name alone for the emails a response already names. No admin route reads a
  person's default, mutes or reminder cadence (Q8).

**10. Callers pick a `runner_profile` by name.** Untouched. Preferences hold
a display name, a workspace id, topic names and a reminder name, each checked
against a rule or a closed set. None of them is an image, a command, a
resource or a backend parameter. The display name is the one that can reach
a task, and only through the existing cleaned `git_identity` record (P37), as Q11
decided. It is an author name, not a parameter of the backend.

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing. §5 records that as a request for no change, so no build lane adds
one. The personal workspace id comes from the frozen `tenant_id_for_user`.
The bell's topics, the preference document, the display-name rules, the role
seam and the profile response are swarm-api's own and live there.

---

## 3. Designs that could work

The decisions that separate the designs are where preferences are stored and
who applies the default workspace. The bell filter, the display name and the
page are much the same in all three.

### 3.1 Design A: the API stores preferences and applies the default (recommended)

**What it is.** swarm-api gains `user_preferences/<hmac id>`, one document per
person, and two routes: `GET /v1/profile` (the whole page in one read) and
`PUT /v1/profile/preferences`. Authentication reads the stored default in the
same batch as the personal-workspace check it already makes, and moves the
default to the front of `tenant_choices`. Every headerless request from every
client then acts in it. The bell reads the preferences after deriving its
items and filters them. The routes that name people add the display name
beside each email they already serve.

**What it costs.**

* *Code:* five new swarm-api modules (preferences, display names, the role
  seam, profile aggregation, routes), changes to `auth.py`, `errors.py`,
  `notifications.py`, `routes/tenants.py` and the four routes that name
  people. One new UI screen, one new UI component for a person's name, the
  Settings wiring, and wording in the plugin. Ten lanes (§6), most of them
  small.
* *Migration:* none. A person without a document gets today's behaviour
  exactly: no stored name (emails are shown as today), no default (the
  computed one), nothing muted, no reminders.
* *Latency:* authentication for a person already reads `tenants/u-x` and
  `workspaces/u-x`, behind a per-instance cache, when it checks the personal
  workspace (`auth.PersonalTenants.ready`, two `get` calls today). The build
  folds the three documents into one `get_all` on a cache miss, cached 15 s
  per instance like `admins.ROLE_CACHE_SECONDS`. One more document in a read
  that already happens adds about a millisecond. That is an estimate,
  measured in PROFILE-DEFAULT's acceptance run. The bell adds no read,
  because it uses the preferences PROFILE-DEFAULT puts on `AuthContext`
  (`AuthContext.preferences`), which is why PROFILE-BELL follows DEFAULT. A page that
  names people adds one `get_all` over the distinct emails it shows, behind a
  60-second per-instance name cache (§4.4).
* *Money:* one document read per person per 15 s per instance at most, plus
  one per distinct named person per 60 s per instance. That is pennies a
  month at this platform's size.

**What it breaks.** A person who sets a group as default and then runs `swarm`
without `--tenant` in a script acts in the group, not the personal workspace.
That is the point, but it changes a headerless script's tenant. The CLI's
`whoami` and `sc config` say which default applied and why (PROFILE-SC), so
the change is visible, not silent. A headerless script also gains a new
failure: during a Firestore outage it gets 503 `preferences_unavailable`
instead of running in the computed default (Q9). The message says how to name
a workspace, which makes the script immune.

### 3.2 Design B: the API stores preferences, and each client applies the default

**What it is.** Same document and routes. Instead of changing authentication,
`GET /v1/profile` returns `default_workspace`, and each client reads it at
start and sends it as `X-Swarm-Tenant`: the console, the MCP bridge
(`apps/swarm-mcp/swarm_mcp/server.py`, which reads the tenant once at start),
`sc`, and `swarm` (`swarm_mcp/cli.py`).

**What it costs.** Four clients each gain a startup read and a precedence rule
that must agree with the other three. `client.py::chosen_tenant` would gain
a third source. The bridge reads its tenant once at start, so a default
changed mid-session applies only on the next session. Old plugin installs
never learn the default, so the default "works" on some machines and not
others. That is the drift CLAUDE.md warns about when one rule is restated in
several places. Q9's "never fall back silently" would also have to be
restated in four clients, and an old client would fall back anyway.

**What it breaks.** Nothing on the server. Every client's tests need a new
precedence case, and `GET /v1/tenants/mine`'s promise that its first entry is
the headerless tenant stays true but stops being the default anyone sees.

### 3.3 Design C: preferences stay on the client

**What it is.** No server document. The console keeps the default and the
bell filter in `localStorage`. `/sc` keeps using its `tenant` plugin option.
Profile becomes a read-only aggregation page plus browser-local toggles.

**What it costs.** The least code: no store, no auth change.

**What it breaks.** It does not meet the issue. "The console and `/sc` open in
the default workspace" would need setting twice, per browser and per machine.
The bell filter would apply in one browser only. A filter on the client also
means the server still sends every muted item, and the badge count, computed
on the server, would disagree with the list. A display name others see cannot
live on one person's client at all, so Q5 rules C out on its own.

### 3.4 Recommendation: A

A is the only design in which one setting governs every client, because the
API is the one place every client already asks "which workspace am I in". It
reuses a read authentication already makes, it keeps `tenants/mine`'s first
entry and the headerless tenant the same thing by construction, and it puts
Q9's 503 in one place. B restates one rule in four clients. C does not
deliver the issue.

---

## 4. The recommended design, in enough detail to build

### 4.1 What the page shows, and where each part's source of truth is

| Section | Shown | Editable here | Source of truth, linked |
|---|---|---|---|
| **1. You** | email; display name (§4.4); platform role, once (owner, admin, or none) | **the display name** | this page for the name; Admin › People for platform roles |
| **1. Workspaces** | each workspace from `tenant_choices`: name, kind, your role (`role.name`, and `via` when there is one), its state (personal workspace: `requested`, `applying`, `ready`...), which one you are in now, which is your default | **the default workspace** | Work › Setup for requesting a personal workspace |
| **2. GitHub** | one row per workspace you can act in (Q10): `@login`, method, state (`active`, `refresh_failed`, `revoked`, or not connected), token expiry, last renewal | no | Work › Access, in that workspace |
| **2. TypeSafe key** | your personal key: set or not set, when, by whom, version; the acting workspace's key: set or not; which one your tasks in the acting workspace would use | no | Settings › API keys |
| **3. Notifications** | one switch per topic (§4.6) and the reminder cadence; a line saying how many items are muted right now | **all of it** | this page |
| **4. Claude accounts** | accounts your personal workspace owns, and accounts lent to it: label, state, 5-hour and 7-day use with reset times, who you lend to (count), stale flag | no | Capacity › Accounts |

Section 2 has exactly these two rows (Q6).

Each "Source of truth" cell is a link. A link into a page that is scoped to
the acting workspace (Access, Accounts) names the workspace. When it is not
the acting one, the link switches workspaces first (`fetch.ts::chooseTenant`)
and then navigates, with the same toast the panel's switcher shows. The page
URL never selects a tenant (`fetch.ts`, `TENANT_QUERY` comment), and this does
not change that.

**In a group workspace versus the personal one.** Profile is about the
person, so sections 1, 2 (GitHub) and 4 look the same whichever workspace the
console is acting in. Two things depend on the acting workspace, and the page
labels both "in <workspace>": the "you are here" marker and the TypeSafe
resolution ("your tasks in team-x would use: your key"). Section 4 never shows
a group's pool. In a group workspace it ends with "This workspace's shared
accounts are on Capacity › Accounts". A person with no ready personal
workspace sees "You have no personal workspace yet" with a link to Work ›
Setup, instead of an empty list that could read as "you have no accounts".

**Profile is reachable when the preferences are not.** `GET /v1/profile` is
one of the routes exempt from Q9's 503 (§4.5), so a person can still see who
they are and pick a workspace. It then shows sections 1 and 2 from the
request's own resolution, names `preferences` in `unavailable`, and disables
the editors with "your preferences could not be read; retry shortly".

### 4.2 API

Both routes are in a new router, `apps/swarm-api/swarm_api/routes/profile.py`,
prefix `/v1/profile`. They are for people. A caller for which
`Authenticator._special` is true (the schedule tick, the rollup sweeper, a
listed service account, any service identity) gets 403 `profile_people_only`.
Neither route takes an email, a user id or a tenant id that selects **whose**
profile, and none ever will: an unknown query parameter such as `?user=` is a
422, not ignored. The caller is always the verified identity. There is no
admin variant of either route (Q8).

#### `GET /v1/profile`

One read for the whole page. Each section is computed by its own function. A
section that fails fails alone: it is left out and named in `unavailable`, as
`notifications.py` does with `partial`. A broker outage therefore costs
section 4, not the page.

```json
{
  "identity": {
    "email": "ada@example.com",
    "display_name": "Ada Lovelace",
    "display_name_suggested": null,
    "domain": "example.com",
    "platform_role": "admin",
    "is_admin": true,
    "is_admin_unresolved": false
  },
  "workspaces": [
    {
      "tenant_id": "u-ada",
      "display_name": "Your workspace (u-ada)",
      "kind": "user",
      "role": {"name": "owner", "source": "derived", "via": null},
      "state": "ready",
      "is_acting": false,
      "is_default": false
    },
    {
      "tenant_id": "team-x",
      "display_name": "team-x@example.com",
      "kind": "group",
      "role": {"name": "member", "source": "derived", "via": "team-x@example.com"},
      "state": null,
      "is_acting": true,
      "is_default": true
    }
  ],
  "default_workspace": {
    "tenant_id": "team-x",
    "source": "profile",
    "ignored": null
  },
  "connections": {
    "github": [
      {
        "tenant_id": "team-x",
        "state": "active",
        "forge_login": "ada",
        "method": "app_user",
        "access_expires_at": "2026-10-18T20:00:00Z",
        "refreshed_at": "2026-10-18T12:00:00Z",
        "failure": null
      },
      {"tenant_id": "u-ada", "state": "not_connected"}
    ],
    "typesafe": {
      "personal": {"set": true, "set_at": "2026-10-10T09:00:00Z", "set_by": "ada@example.com", "version": 3},
      "acting_workspace": {"tenant_id": "team-x", "set": false},
      "uses": "user"
    }
  },
  "notifications": {
    "preferences": {"muted": ["finished"], "reminder": "every_4h"},
    "muted_now": {"needs_you": 0, "updates": 12},
    "topics": ["approval", "waiting_step", "question", "failed", "finished", "account", "admin"]
  },
  "claude_accounts": {
    "personal_workspace": "u-ada",
    "accounts": [
      {
        "account_id": "u-ada:main",
        "label": "main",
        "relation": "own",
        "state": "AVAILABLE",
        "windows": {
          "five_hour": {"utilization": 0.42, "resets_at": "2026-10-18T15:00:00Z"},
          "seven_day": {"utilization": 0.18, "resets_at": "2026-10-22T00:00:00Z"}
        },
        "lent_to_count": 1,
        "stale": false
      }
    ],
    "unreadable_document_count": 0
  },
  "revision": 4,
  "unavailable": []
}
```

Field rules, each with its reason:

* **`identity`** comes from `AuthContext` and the preferences document.
  `email`, `domain` and the admin fields are the values `GET /v1/tenants/me`
  serves. `platform_role` is `admin_roles/{email}.role` (`owner`, `admin`)
  through `admins.py`'s 15-second cache, or `"admin"` for an admin-group
  member or listed admin, or `null`. It is shown **once**, here, and never
  repeated as a per-workspace role.
* **`identity.display_name`** is the stored name, or `null` when none is
  stored. **`display_name_suggested`** is the token's `name` claim after the
  §4.4 rules, served only while no name is stored, so the editor can offer it
  as its initial value. It is never shown to anyone else (§4.4, "The token's
  `name` claim").
* **`workspaces`** is exactly `auth.tenant_choices`, in its order, which is
  already default first. No Cloud Identity call is made for it (§2.1).
  `state` is read from `workspaces/u-x` for the personal entry only. A person
  whose personal workspace is not ready yet does not have it in
  `tenant_choices`, so it is added as a non-selectable row with its state, so
  the page can say "requested 2 days ago" instead of hiding it.
* **`workspaces[].role` is the seam the roles build replaces (Q7).** It is
  produced by one function, `apps/swarm-api/swarm_api/workspace_role.py::role_in(ctx, tenant_id)`,
  and nothing else computes a person's role in a workspace. Today it returns
  `{"name": "owner", "source": "derived", "via": null}` for the confirmed
  personal workspace and `{"name": "member", "source": "derived", "via": <group address>}`
  for a group. A platform admin is not a workspace role, so `role_in` never
  returns `admin`; `identity.platform_role` carries it. The roles build
  (#1518) changes `role_in`'s body to read the assigned role, serves
  `source: "assigned"`, and may serve any role name. The page renders
  `role.name` as given and `via` when present, and branches on neither. A
  page test renders an unknown name (`"maintainer"`) verbatim, so a new role
  needs no page change. If ROLES-DESIGN puts the function in another module,
  the build moves `role_in` and keeps the field.
* **`default_workspace.source`** is `"profile"` (stored and honoured),
  `"personal"` (today's rule: the ready personal workspace) or
  `"first_group"`. `ignored` is `null` or the reason a stored default was not
  honoured: `not_a_member` (the group no longer confirms the person),
  `not_ready` (the personal workspace is not ready) or `unknown`. The stored
  value is **kept** when ignored, so a person whose group membership blinks
  during a Cloud Identity outage does not lose their setting.
* **`connections.github`**: one entry for **every** selectable workspace
  (Q10), read by one `get_all` over
  `forge_connections/<connection_id_for(t, email)>`. That is N computed ids
  and no query. N is the number of workspaces, single digits here. **Allowed
  fields only:** `state`, `forge_login`, `method`, `access_expires_at`,
  `refreshed_at`, `failure`. Never `secret_name`, `token_id`,
  `connection_id` or `forge_user_id`. A failed batch costs this list only,
  named `github` in `unavailable`; it is never served as "not connected".
* **`connections.typesafe`** reuses `provider_keys.ProviderKeys.status` and
  `provider_keys.resolve_provider_key`, and serves only `set`, `set_at`,
  `set_by`, `version` and `uses`. Never `secret_id`, although
  `provider_keys.py::_status` returns one.
* **`connections` has exactly two keys**, `github` and `typesafe` (Q6). The
  API-shape test asserts the key set, so a third row is a deliberate change.
* **`claude_accounts`** calls `AccountPool.list_accounts(<own u- id>)` only
  when the personal workspace is confirmed ready. It keeps accounts whose
  `owner_tenant` is that id (`relation: "own"`) or whose `lend_to` contains
  it (`relation: "borrowed"`). It serves the two quota windows by name, and
  `lend_to` as a count, so a group name the person lends to is not repeated
  here; Capacity › Accounts shows the names to the owner. The broker's
  `unreadable_document_count` is passed through, for the reason
  `routes/accounts.py::list_accounts` gives: a short list must not read as
  the whole pool.
* **`revision`** is the preference document's revision, which a `PUT` must
  echo.
* **`unavailable`** lists the sections that could not be read: any of
  `preferences`, `github`, `typesafe`, `claude_accounts`.

**No credential, and no credential's name.** `swarm_api/profile.py` builds
every section from an explicit allow-list. It never spreads a source
document. `tests/unit/control_plane/test_profile_no_credentials.py` stores a
connection, a key status and an account whose fake secret fields hold a
sentinel built at runtime (`"tok-" + secrets.token_hex(16)`). It then asserts
that the serialised response contains neither the sentinel nor any key named
`secret`, `secret_id`, `secret_name`, `token`, `token_id`, `value` or
`refresh_token`.

#### `PUT /v1/profile/preferences`

```json
{
  "display_name": "Ada Lovelace",
  "default_workspace": "team-x",
  "notifications": {"muted": ["finished"], "reminder": "every_4h"},
  "revision": 4
}
```

* The whole preference set is replaced. Unknown fields are a 422
  (`extra="forbid"`).
* `display_name` is `null` (no name: others see the email alone) or a string
  that passes §4.4's rules. A refusal is 422 with one code per rule
  (`display_name_invalid`, `display_name_taken`, `display_name_reserved`), and
  its message names the rule, never the person whose name it collided with.
* `default_workspace` is `null` (use the computed default) or a `tenant_id`
  in this request's `tenant_choices`. Anything else is 422
  `not_your_workspace`, and the message never says whether the workspace
  exists.
* `muted` is a subset of `TOPICS`, without duplicates. Any topic may be
  muted, needs-you topics included (Q3). `reminder` is one of `as_set`,
  `every_4h`, `daily` or `never` (§4.6).
* `revision` must equal the stored revision, or the request is a 409
  `preferences_changed`, and the page reloads. Two open tabs cannot silently
  overwrite each other.
* The response is the `GET`'s `identity.display_name`, `default_workspace`,
  `notifications` and `revision` sections. The instance drops its 15-second
  preference cache entry and its 60-second name cache entry for the caller,
  so the next request on that instance sees the change. Other instances see
  it within 15 s (the default) or 60 s (the name). The page says "applies to
  new sessions within a minute".
* It is logged as one structured line naming the caller's `user_hash` and
  which fields changed, never their values. A display-name change also writes
  the audit entry of §4.4; nothing else this route saves is audited, because
  nothing else is shown to anyone but its owner (Q8).

### 4.3 Firestore: `user_preferences/<id>`

```
user_preferences/up_<32 hex>
{
  "v": 1,
  "display_name": "Ada Lovelace" | null,
  "display_name_folded": "ada lovelace" | null,
  "default_workspace": "team-x" | null,
  "notifications": {
    "muted": ["finished"],
    "reminder": "as_set" | "every_4h" | "daily" | "never"
  },
  "revision": 4,
  "updated_at": <timestamp>
}
```

**The id (Q1).** It is `up_` followed by the first 32 hex digits of
`HMAC-SHA256(swarm-child-key, "user-prefs:" + lower(email))`. This is the same
key as the child-key registration's document id
(`apps/swarm-api/swarm_api/childkey.py`, module docstring: "the
registration's DOCUMENT ID, which no tenant identity can compute"), under its
own purpose string. No existing derivation's message begins with
`user-prefs:` (`childkey.py`'s are `<purpose>\n<message>` with purposes such
as `swarm-child-nonce/v1`), so this id can never be read as a registration,
a nonce or an attestation. The key is held by swarm-scheduler and swarm-api
only. It needs no new secret, Terraform or deploy step.

Why an HMAC and not `user_hash`, which `notification_reads` uses: §2.2
invariant 9. A planted default workspace misfiles work across tenants, and a
planted display name would let one person's agent rename another person. A
planted read mark only hides an item from the bell, which is why the read
marks could afford a computable id and this document cannot.

**Key rotation.** `childkey.py` holds the current and, during a rotation,
the previous `swarm-child-key` version. The preferences module computes the
id under each configured version, reads them in one `get_all`, uses the
newest that exists, and writes under the newest key. If every version has
been rotated out, the person gets no name, the computed default and an
unmuted bell until they save again. That is today's behaviour, so a lost
document fails safe. The display-name claim (§4.4) of a lost document is
freed by the same check that frees any orphaned claim.

**What it does not hold:** the email (the id is enough), any credential or
credential name, and anything the scheduler, reconciler or worker reads. A
worker that creates a document in `user_preferences` under an id of its own
invention creates one that no person's request ever reads.

**Who reads it:** swarm-api only. Authentication reads the caller's own
document (§4.5). The name lookup of §4.4 reads `display_name` alone from the
documents of the emails a response already names, and serves nothing else
from them. Nothing reads another person's default, mutes or reminder.

**Who writes it:** swarm-api, only in `PUT /v1/profile/preferences`, only for
the verified caller. No admin route reads or writes another person's
preferences, there is no `?user=` parameter on any profile route, and
Admin › People has no preferences section (Q8). A person who needs help with
their settings shares their screen; an admin who suspects a misfiled task
reads the task's tenant, which is on the task.

### 4.4 The display name (Q5)

**What it is.** A name the person chooses on Profile, stored in their
preferences document, and shown to other people beside the person's email on
Admin › People, on approvals and on run pages. It is a label, never an
identity: every check that decides what a person may do keeps using the
verified email, and nothing looks a person up by name.

**Where others see it, and the email always beside it.** A name is served
only by a response that already serves the email it belongs to, as a sibling
field: `submitted_by` gains `submitted_by_name`, `decided_by` gains
`decided_by_name`, each `approvers[]` entry gains its name, and a People row's
`email` gains `display_name`. The console renders every one of them through
one component, `apps/swarm-ui/src/PersonName.tsx::PersonName`, which always
prints `Name · email` and prints the email alone when there is no name. It
never truncates the email and truncates the name first when space is short,
so the part that can be chosen is the part that gives way. A unit test
renders every caller with a long name in a narrow box and asserts the full
email is in the DOM. The routes, all four in PROFILE-NAMES-API:

| page | route | fields that gain a name |
|---|---|---|
| Admin › People | `people.py::People._row` | `email` → `display_name` |
| Approvals | `routes/approvals.py::list_approvals`, `get_approval` | `decided_by`, `approvers[]` |
| Task page (Agents) | `routes/tasks.py` task reads | `submitted_by` |
| Workflow run page | `routes/workflows.py` workflow reads | `submitted_by` |

The bell, Activity, the timeline and the CLIs keep showing emails. They can
adopt `PersonName` later with no API change beyond the sibling field.

**The lookup.** `display_names.py::names_for(emails)` computes each email's
preference id and reads them in one `get_all`, returning `display_name` only.
A 60-second per-instance cache keyed by email bounds the reads. A failed
lookup serves every name as `null`, so the page shows emails, as today; it
never fails the page.

**The rules a name must pass**, in `display_names.py::check`, each with its
reason:

1. *Normalised first.* NFC, surrounding spaces stripped, internal runs of
   spaces collapsed to one. The stored name is the normalised one.
2. *1 to 64 characters.* Long enough for any real name; short enough that
   `PersonName` can always fit the email beside it. A stricter subset of
   `gitidentity.NAME_MAX_CHARS` (100), so a stored name is always a valid
   commit name.
3. *Only letters (Unicode category L), combining marks (M), spaces and
   `.`, `'`, `-`.* No digits, no `@`, no `<` or `>`, no punctuation that
   could form an address, a URL or a role tag such as `[admin]`, and none of
   the format characters (bidi overrides, zero-width joiners) that make a
   string render differently from its bytes. `@` is the rule that stops a
   name from **embedding an email**: no address can be written without it.
4. *One script.* Every letter is from one Unicode script (Latin, Cyrillic,
   Greek, Han...), judged by `unicodedata.name`'s leading word. A Latin name
   with a Cyrillic `а` in it is how "Ada" and "Аda" look identical, and this
   is the stdlib check that catches it without a confusables table.
5. *Not reserved.* No whole word, after folding, from `RESERVED`: `admin`,
   `administrator`, `owner`, `swarmcloud`, `system`, `bot`, `support`. These
   are the words that would make a person's name read as a platform role or
   as `gitidentity.BOT_NAME`. Code `display_name_reserved`.
6. *Not another person's name, and not containing one.* Compared on the
   **folded** form: NFKD, combining marks dropped, case-folded, `.'-` turned
   into spaces, spaces collapsed. The folded name may not equal another
   person's folded name. It may not contain another person's folded name as a
   whole-word run **when that other name has two or more words**, so "Ada
   Lovelace Admin" is refused when "Ada Lovelace" exists, but "Bob Smith" is
   allowed when "Bob" exists: a one-word name blocks only an exact match.
   The containment half came from the orchestrator's brief, not from the
   owner's Q5 answer; the owner narrowed it to names of two or more words on
   2026-10-18 (Q12, §7.1), because the email shown beside every name covers
   the rest. The check is **platform-wide**, not per workspace: listing a
   group's members is the Cloud Identity call this project cannot make
   (§2.1), and a name unique across the platform is unique in every
   workspace. Code `display_name_taken`.

The person's own email's local part is not checked: it is theirs, and the
email is printed beside the name anyway.

**Uniqueness, without a worker being able to squat a name.** Each stored name
holds one claim document, `display_name_claims/dn_<32 hex>`, whose id is the
first 32 hex digits of `HMAC-SHA256(swarm-child-key, "user-name:" + folded)`,
holding `{"owner": "up_<...>", "folded": "<folded>", "at": <timestamp>}`. The
save is one Firestore transaction: read the caller's preferences document and
the new name's claim; refuse when the claim exists, its owner is not the
caller, and the owner's document still holds that folded name; otherwise
create or take over the claim, delete the caller's old claim, write the
preferences document and write the audit entry. Two people saving the same
name at once therefore cannot both win. The containment check of rule 6
streams `display_name_claims` (a few hundred documents) before the
transaction, and **skips any claim whose id is not the HMAC of its own
`folded` field**. A worker cannot compute that HMAC, so a document a worker
plants there is ignored by both checks, and a claim whose owner no longer
holds the name is free. The containment check is not transactional; two
saves racing to contain each other within the same second is a race this
design accepts, because the audit names both and the email is beside both.

**History and audit.** Every change, including setting the first name and
clearing it, writes one `admin_audit` entry in the save's transaction:
`{"action": "display_name_changed", "target_workspace_id": null, "by": <email>,
"at": ..., "detail": {"from": <old or null>, "to": <new or null>}}`. That is
the collection `people.py::People.audit` and `admins.py` already write, which
Admin › People already lists, created and never updated. So **admins see
the history** of every person's name, in the audit they already read, without
any new admin route. The name is public on the platform by design, so its
history is not a preference in Q8's sense; the default workspace, the mutes
and the reminder cadence are, and they are never audited or shown. There is
no rate limit on changes: a typo fixed twice is common, and an impersonation
attempt is already refused by the rules and is visible in the audit.

**The token's `name` claim.** It applies in two places, and is never shown to
another person:

* *As the initial value only.* While no name is stored, `GET /v1/profile`
  serves the claim, after the rules above, as `display_name_suggested`, and
  the editor offers it. Others see the email alone until the person saves.
  A claim that fails a rule is not suggested.
* *As the commit author's name (Q11 (a), decided 2026-10-18).*
  `AuthContext.display_name` becomes the stored name when there is one, else
  the token's name claim, else the email's local part (today's value). The
  email is in every commit beside it, and a task keeps the name recorded at
  admission, so a later rename never rewrites a commit.

### 4.5 The default workspace: one rule, applied in authentication

`Authenticator._with_own_workspace` (in `auth.py`) already builds
`tenant_choices` with the personal workspace first. A new step runs after
it, in the same method or a sibling called from the same place:

1. When the caller is a person (not `_special`), read
   `user_preferences/<id>` in one `get_all` with `PersonalTenants.ready`'s
   two documents (which that method reads with two `get` calls today),
   through a 15-second per-instance cache keyed by email. The document read
   (or `None` when it could not be read) is kept on the context as
   `AuthContext.preferences`, which the bell reads (§4.6), so no route
   reads it a second time.
2. When `default_workspace` is set **and** is in `tenant_choices`, move it to
   the front. Otherwise leave the order alone, and record why on the context
   (`default_source`, `default_ignored`) for `GET /v1/profile` and
   `GET /v1/tenants/mine`.
3. `_select_tenant` is unchanged. A request with `X-Swarm-Tenant` acts in the
   header's tenant, and one without acts in the first choice, as it does
   today.

**Failure (Q9).** If the preferences document cannot be read and the request
carries no `X-Swarm-Tenant`, the request fails with 503 and code
`preferences_unavailable`, from a new
`errors.py::PreferencesUnavailable(UpstreamUnavailable)`. Its message says
what happened and how to proceed: "Your SwarmCloud preferences could not be
read, so your default workspace is not known and nothing was submitted. Name
a workspace and retry: choose one in the console's workspace switcher, set
SWARM_TENANT or the /sc plugin's tenant option, or pass --tenant to swarm."
Nothing is submitted, read in a guessed workspace or queued, and the request
**never falls back** to the computed default, because that is the misfiling
`_with_own_workspace` already refuses for a failed personal-workspace read
(its docstring: "A failed read is a 503, never a guess"). The two documents
are read in one batch, so this adds no new way to fail: a Firestore outage
that fails one fails both. A request that names its workspace is unaffected:
the header is honoured and the preferences are not needed.

Three routes act in no workspace and are exempt, listed in one set,
`auth.py::PREFERENCE_FREE_ROUTES`: `GET /v1/tenants/me`,
`GET /v1/tenants/mine` and `GET /v1/profile`. They are what a person needs to
see their choices and pick one. On them, an unreadable document makes
`default_source` `"unavailable"` and leaves `tenant_choices` in today's
order, and `GET /v1/profile` names `preferences` in `unavailable` (§4.1). The
set is a closed list held by a test, so a route that acts in a workspace
cannot join it by accident.

**Precedence, per client. Every explicit choice still wins.**

| client | explicit choice (wins) | then |
|---|---|---|
| console | the panel's tenant switch, for this tab's session (`X-Swarm-Tenant` from `fetch.ts::chosenTenant`) | the stored default, then today's rule, all in the API |
| `/sc`, the bridge, `sc` | `SWARM_TENANT` in Claude Code's environment, then the plugin's `tenant` option (`plugin/.claude-plugin/plugin.json`; `plugin/hooks/session-start.sh`) | the same |
| `swarm` CLI | `--tenant`, then `SWARM_TENANT` (`swarm_mcp/client.py::chosen_tenant`) | the same |

No client changes its precedence. The plugin and CLI change only what they
**say** when they send no header: `sc.py::_DEFAULT_TENANT_SOURCE` says "the API
chose your default: the one in Settings › Profile, else your personal
workspace", `sc whoami` / `sc config` report the `default_source` that
`GET /v1/tenants/mine` now returns, and a 503 `preferences_unavailable` is
printed with its message, which already says how to name a workspace
(PROFILE-SC).

**The console's switch lasts for the tab's session (Q2).** Today the panel's
switch is stored in `localStorage` and lasts forever in that browser. It
moves to `sessionStorage` under the same `TENANT_PREF` key: a reload keeps
it, and a new window or tab opens in the default. On first load after the
change, `fetch.ts` removes the old `localStorage` entry once, so nobody stays
pinned by a switch made before the default existed. The panel's tenant block
marks the default workspace and offers "Back to default", which clears the
session's choice. `dropRefusedTenant` keeps working unchanged. When a
headerless request returns 503 `preferences_unavailable`, `fetch.ts` raises
it like any error, and the panel shows the message beside the switcher, so
the person's next action is to pick a workspace, which sends the header.

**`GET /v1/tenants/mine`** gains two fields per entry: `is_default` (true on
the first entry) and, on that entry, `default_source` (`profile`, `personal`,
`first_group` or `unavailable`). The entries and their order otherwise stay
exactly `tenant_choices`, so its documented promise holds.

### 4.6 Notification preferences on a bell derived on read

**Topics.** A topic is a finer cut than a group. The groups stay as they are,
because the bell's layout and `docs/notifications.md` use them. Each source
tags its items with a topic:

| topic | needs you | sources (`notifications.py::SOURCES`) | what the issue calls it |
|---|---|---|---|
| `approval` | yes | `approvals` | approvals |
| `waiting_step` | yes | `awaiting_input`, added by HW-NOTIFY (human-steps.md §4.5) | steps waiting for me |
| `question` | yes | `handoffs`, `questions` | (agent needs you) |
| `failed` | no | `finished`, items whose task, workflow or run ended failed, cancelled or dead-lettered | failed runs |
| `finished` | no | `finished`, everything else (succeeded, PR merged) | |
| `account` | GitHub yes, others no | `github`, `workspace`, `loan` | |
| `admin` | yes, admins only | `admin` | |

`_item` gains a `topic` argument, and the item a `topic` field, so the
console can show "muted" counts per topic. The `finished` source assigns
`failed` or `finished` from the end state it already reads (`_ENDED`).

**Every topic may be muted (Q3).** Filtering happens after derivation, at
read time. `Notifications.feed` derives every item as today, drops items
whose topic the caller muted, and counts what it dropped:

```json
"muted": {"needs_you": 1, "updates": 12}
```

The badge (`unread_needs_you`) counts only what is shown. When
`muted.needs_you` is above zero, a quiet line under the list says "1 muted
item needs you · change in Settings › Profile", so a muted approval is never
invisible, only quiet. Muted updates are not announced; the count is on
Profile. `mark_all_read` marks the shown items only. A muted item that is
unmuted later shows as unread, which is true. `admin_pending` drives the
panel's Admin pill, not the bell, so it is computed before the filter: an
admin who mutes `admin` still sees the pill.

**If the preferences cannot be read, the bell follows Q9 like any other
route.** `GET /v1/notifications` is tenant-scoped (`routes/notifications.py`
takes `deps.tenant_scope`, and `notifications.py` derives approvals, tasks
and workflows from `viewer.tenant_id`), so a headerless bell read would show
the work of a guessed workspace, which is the silent fallback Q9 forbids. It
is not in `PREFERENCE_FREE_ROUTES`, so authentication answers a headerless
bell read with 503 `preferences_unavailable` before the bell runs, and the
console shows the message beside the switcher (§4.5). When the request named
its workspace with `X-Swarm-Tenant`, the workspace is known and only the
mutes and cadence are not: the bell is then served **unfiltered**, at
`as_set`, with `partial: true`. Muting is a convenience, and over-telling is
the safe failure once the workspace is the one the person chose.

**Reminders are read marks that age out. Nothing is stored.**

* `as_set` (the default) is today's behaviour. A read needs-you item stays
  read. A waiting step re-rings on its author's `remind_after_seconds`.
* `every_4h` / `daily`: a read mark (or `all_read_at`) on a **needs-you**
  item counts only while it is younger than 4 h / 24 h. After that, the item
  shows as unread again, and it keeps doing so until whatever it is about is
  resolved and it no longer derives. Updates never re-ring.
* `never`: no needs-you item re-rings, waiting steps included. For a waiting
  step that means its reminder index `k` is held at 0.

**Whose cadence wins for a waiting step (Q4):** the person's, when they chose
`every_4h`, `daily` or `never`; the author's `remind_after_seconds`, when the
person left it at `as_set`. Its item id still rotates per period
(`task:input:<task>:<k>`), so HW-NOTIFY's rule is kept, not replaced. The
step still waits in the approvals inbox and on the workflow for everyone,
whatever one person's bell does.

Marks are pruned at `KEEP_READS` (30 days) as today, and `daily` needs a
mark at most 1 day old, so no new retention is needed. The two settings
compose without a timer, a queue or a write, so "derived, never stored"
(`docs/notifications.md`) still holds word for word.

**What the bell does not gain:** e-mail, push or chat delivery. The issue asks
what reaches the bell, and the bell is the only channel. A delivery channel
would be stored and sent, and it would be its own design.

### 4.7 Changes by component, and the files each lands in

**swarm-api**

* `apps/swarm-api/swarm_api/preferences.py` (new): the id derivation
  (`"user-prefs:" + email`), the read across key versions, the cached read
  for authentication, the transactional write with the revision check,
  `TOPICS` and `REMINDERS`.
* `apps/swarm-api/swarm_api/display_names.py` (new): `check` (§4.4 rules 1-6),
  `fold`, `RESERVED`, the claim id (`"user-name:" + folded`), the claim and
  audit writes inside the preferences transaction, and `names_for` with its
  60-second cache.
* `apps/swarm-api/swarm_api/errors.py`: `PreferencesUnavailable` (503,
  `preferences_unavailable`) and the three `display_name_*` 422s.
* `apps/swarm-api/swarm_api/workspace_role.py` (new): `role_in(ctx, tenant_id)`,
  the one seam for a person's role in a workspace (Q7).
* `apps/swarm-api/swarm_api/profile.py` (new): one function per section of
  `GET /v1/profile`, each from an allow-list, each failing alone.
* `apps/swarm-api/swarm_api/routes/profile.py` (new): the two routes.
* `apps/swarm-api/swarm_api/main.py`: `include_router(profile.router)`.
* `apps/swarm-api/swarm_api/auth.py`: §4.5 steps 1-2, `PREFERENCE_FREE_ROUTES`,
  the 503, `preferences`, `default_source` / `default_ignored` on
  `AuthContext`, and
  `display_name` from the stored name (Q11, §7.1).
* `apps/swarm-api/swarm_api/routes/tenants.py`: `is_default` and
  `default_source` in `_choice`.
* `apps/swarm-api/swarm_api/notifications.py`: topics, the filter, the
  reminder rule, `muted`.
* `apps/swarm-api/swarm_api/routes/notifications.py`: passes the caller's
  preferences to `Notifications.feed`.
* `apps/swarm-api/swarm_api/people.py`, `routes/approvals.py`,
  `routes/tasks.py`, `routes/workflows.py`: the sibling `*_name` fields of
  §4.4, through `display_names.names_for`.

**Console** (`apps/swarm-ui/src/`)

* `Profile.tsx` (new): `ProfileScreen`, four cards, built with `useUrRead`
  and `UrRegion` like `ProviderKeys.tsx` and `CodeIntelligence.tsx`. The
  display-name editor shows the rules as it is typed and the API's refusal
  verbatim.
* `api.ts`: `loadProfile`, `putPreferences`. `types.ts`: `Profile`,
  `Preferences`, `WorkspaceRole`, `NotificationTopic`,
  `NotificationFeed.muted`, `TenantChoice.is_default` and `default_source`,
  and the `*_name` siblings on the task, workflow, approval and People row
  types.
* `App.tsx`: `PROFILE_TAB = 'profile'`, `PROFILE_LABEL = 'Profile'`, and the
  address, title and render branches beside `WORKSPACE_TAB`'s. `paths.ts`:
  `'settings/profile': '/settings/profile'` in `FIXED`.
* `Spine.tsx`: a "Settings › Profile" link in the panel foot beside the two
  existing `sk-pset` links, the default marker, "Back to default" and the
  `preferences_unavailable` message in `TenantBlock`. `fetch.ts`: the switch
  in `sessionStorage` and the one-time removal of the `localStorage` entry
  (Q2).
* `Notifications.tsx`: the muted line.
* `PersonName.tsx` (new), used by `Approvals.tsx`, `People.tsx`,
  `Workflows.tsx` and `AgentDetail.tsx` wherever they print a person.

**Issue forms:** `.github/ISSUE_TEMPLATE/bug_report.yml` and
`feature_request.yml` gain "Web UI · Settings › Profile (/settings/profile)".
`tests/unit/scripts/test_issue_forms.py::_nav_options` adds `PROFILE_TAB` /
`PROFILE_LABEL` beside the API-keys and Workspace entries.

**Plugin and CLIs:** `apps/swarm-mcp/swarm_mcp/sc.py` (`_DEFAULT_TENANT_SOURCE`,
`read_whoami` reporting `default_source`, the 503's message printed as
given), `apps/swarm-mcp/swarm_mcp/client.py` (`tenant_listing` marks the
default), and `plugin/.claude-plugin/plugin.json` (the `tenant` option's
description names Settings › Profile).

**Docs:** `docs/notifications.md` (topics, muting, reminders),
`docs/multi-tenancy.md` (the default's new first rule and the 503), and
`docs/security.md` (the display-name rules and why they are platform-wide).

**Not changed:** the scheduler, the reconciler, the worker, the quota broker,
`gitidentity.py` (a stored name already passes `clean_name`), Terraform (Q1
reuses an existing key), and `apps/common/swarm_common/`.

**Amended 2026-10-19 (#1633, lane NAV-PROFILE).** The "Settings › Profile"
link in the panel foot, above, is gone, with the other three `sk-pset`
links. Owner decision that day ("All under Profile; Admin by role"):
**Profile is the spine's first item**, under the Hive and above Submit,
drawn as the person's avatar (the foot's avatar went, so the person is
drawn once), and its panel holds every settings page — Profile, API keys,
Workspace, MCP servers, People and roles, in that order (`Spine.tsx`
`PANEL_PAGES.profile`). The reason is the one this page exists for: the
person's things in one place. Four links stacked under a name was not one
place, and People and roles had no link at all. The addresses are
unchanged (`/settings/profile` is still this page), `spineOf` lights
Profile for all five, and the panel foot keeps the name, the admin tag,
Sign out, the email and the theme toggle. Profile is not a `SECTIONS`
entry, so the issue forms' "Where" list above is unchanged.

### 4.8 Failure cases

| case | what happens |
|---|---|
| Stored default is a group the person left | ignored (`not_a_member`), computed default used, Profile says so, value kept |
| Stored default is the personal workspace, which is now being rebuilt | ignored (`not_ready`), same |
| Preferences unreadable, headerless request | 503 `preferences_unavailable` with how to name a workspace; nothing submitted; never the computed default (Q9) |
| Preferences unreadable, request names a workspace | served as today |
| Preferences unreadable, `tenants/me`, `tenants/mine`, `profile` | served, `default_source: "unavailable"`, editors disabled |
| Preferences unreadable, headerless bell read | 503 `preferences_unavailable`, like any headerless request (Q9) |
| Preferences unreadable, bell read that names a workspace | unfiltered, at `as_set`, `partial: true` |
| Name lookup fails on a page that names people | names `null`, emails shown as today |
| Broker down | Profile omits `claude_accounts`, names it in `unavailable` |
| GitHub batch read fails | Profile omits `connections.github`, names `github` in `unavailable`; never "not connected" |
| Cloud Identity down | as today: tenant resolution is a 503. Profile is not reachable either, because the person's workspaces cannot be known |
| Two tabs save | the second gets 409 and reloads |
| Two people save the same name at once | one transaction wins; the other gets 422 `display_name_taken` |
| A tenant agent creates `user_preferences/<anything>` or `display_name_claims/<anything>` | read by nobody; a claim whose id is not its own HMAC is skipped |
| `swarm-child-key` rotated out entirely | preferences, including the name, reset to today's behaviour until saved again; the orphaned claim is free |

---

## 5. Frozen contract: change requests

### Request P1. No change to `apps/common/swarm_common/` for the user profile

**Status:** PROPOSED by lane PROFILE-DESIGN, part of #1505, and confirmed by
lane PROFILE-DESIGN-B after the owner's answers of 2026-10-17/18. Requests
no change, so nothing is to be applied. It is recorded so no build lane adds
one.

* **Preferences are not a contract type.** Only swarm-api reads or writes
  `user_preferences`. The scheduler, reconciler and worker never see it. A
  type in `swarm_common` would invite exactly those readers.
* **The display name is not a contract type.** It is served beside emails by
  swarm-api only. Where it reaches a task (Q11, §7.1), it travels in the
  existing `git_identity` record of the dispatch block (P37), whose shape is
  unchanged.
* **Roles are not a contract type here.** `workspace_role.py::role_in` is
  swarm-api's. If the roles design (#1518) needs a role in the contract, it
  files its own request.
* **Notification topics are not a contract type.** They are swarm-api's and
  the console's, like the existing groups, and `test_ui_api_field_contract.py`
  holds the two spellings together.
* **The personal workspace id** comes from `identity.py::tenant_id_for_user`,
  unchanged.
* **`Tenant`, `Task`, `ParkReason` and `EventType`** are unchanged. A default
  workspace selects among tenants a person may already use, and is resolved
  before a task exists.

**What it would break:** nothing.
**Invariants:** 1-10 untouched.

---

## 6. Build plan

Ten lanes. No two lanes edit the same file. Every UI lane names at most six
files. Each lane pushes its red tests first (CLAUDE.md, "Red first"), and runs
`scripts/changed-guards.sh` and `scripts/publish-scan.sh` before finishing.
Every fake secret or token in a test is built at runtime.

**Order:**

1. **PROFILE-STORE** alone. Everything else reads its modules.
2. Then, in parallel: **PROFILE-API**, **PROFILE-DEFAULT** and
   **PROFILE-NAMES-API**.
3. Then **PROFILE-BELL** (after DEFAULT, which adds `AuthContext.preferences`
   and answers a headerless read with 503 when they are unreadable; the bell
   reads the former and its tests rely on the latter).
4. Then **PROFILE-PAGE** (after API, because `test_runtimes_screen.py`
   requires every `/v1` path the UI calls to be served).
5. Then, in parallel: **PROFILE-NAV** (after PAGE), **PROFILE-SHELL** (after
   PAGE, BELL and DEFAULT), **PROFILE-NAMES-UI** (after PAGE and NAMES-API)
   and **PROFILE-SC** (after DEFAULT).

Q11 is answered (a), so PROFILE-DEFAULT carries its line and test as
written; no lane waits on an answer.

**PROFILE-BELL and HW-NOTIFY both edit `notifications.py`**, so they are
serial, never parallel. Whichever lands second rebases. If HW-NOTIFY lands
first, PROFILE-BELL tags `awaiting_input` with `waiting_step` and applies the
reminder rule to it. If PROFILE-BELL lands first, it defines `waiting_step`
with no source yet, and HW-NOTIFY's brief says to tag its source with it.

**PROFILE-API and the roles build (#1518) meet at
`workspace_role.py`.** PROFILE-API creates it with the derived roles. The
roles build edits only that file's body (and its own files), never
`profile.py` or `Profile.tsx`. Whichever is dispatched second is told the
other's state.

### PROFILE-STORE: the preferences document and the display-name rules (swarm-api)

* Files: `apps/swarm-api/swarm_api/preferences.py` (new),
  `apps/swarm-api/swarm_api/display_names.py` (new),
  `apps/swarm-api/swarm_api/errors.py`,
  `tests/unit/control_plane/test_user_preferences.py` (new),
  `tests/unit/control_plane/test_display_names.py` (new).
* Red first:
  * the id is `up_` + 32 hex of the HMAC of `"user-prefs:" + lower(email)`,
    is not `user_hash(email)`, is stable across reads, and differs from the
    claim id of the same string;
  * a document under the previous key version is read, and a write lands
    under the newest;
  * a stale `revision` is 409 and stores nothing;
  * each rule of §4.4 refuses its case with its code: 65 characters, a
    digit, `@`, a bidi override, a zero-width joiner, mixed Latin and
    Cyrillic, `Admin` as a word, an exact folded match ("ADA  lovelace"
    against "Ada Lovelace"), an accented match ("Adá Lovelace"), and a
    containing name ("Ada Lovelace Admin" refused for the containment as
    well as the reserved word, "Ada Lovelace Jr" refused for containment);
  * "Bob Smith" is ACCEPTED when "Bob" exists (Q12 (b): containment is
    checked only for existing names of two or more words);
  * a one-word existing name blocks only an exact match: with "Bob" stored,
    "BOB" and "Bób" are refused with `display_name_taken`, "Bob Smith" and
    "Bobby" are accepted;
  * a claim planted at an id that is not its folded name's HMAC is ignored
    by both checks;
  * a claim whose owner no longer holds the name is taken over;
  * every change, including clearing, writes exactly one
    `display_name_changed` audit entry with `from` and `to`, in the same
    transaction as the document;
  * `names_for` returns only `display_name`, and `null` for every email when
    the read fails.

### PROFILE-API: the read and the save (swarm-api)

* Files: `apps/swarm-api/swarm_api/profile.py` (new),
  `apps/swarm-api/swarm_api/workspace_role.py` (new),
  `apps/swarm-api/swarm_api/routes/profile.py` (new),
  `apps/swarm-api/swarm_api/main.py`,
  `tests/unit/control_plane/test_profile_api.py` (new),
  `tests/unit/control_plane/test_profile_no_credentials.py` (new).
* Red first:
  * `PUT` with a workspace outside `tenant_choices` is 422 and stores
    nothing;
  * an unknown topic, field or query parameter (`?user=ada@...`) is 422;
  * a `_special` caller is 403;
  * every topic, needs-you topics included, can be muted;
  * the credential sentinel test of §4.2;
  * `connections` has exactly the keys `github` and `typesafe`;
  * `connections.github` has one entry per selectable workspace, from one
    `get_all` (the fake counts calls), and a failed batch is named in
    `unavailable`, never served as `not_connected`;
  * a broker failure leaves `claude_accounts` out and names it in
    `unavailable`, and the rest is served;
  * a group pool account and another member's connection are never in the
    response;
  * `role` comes from `role_in` only (a test patches `role_in` to return
    `{"name": "maintainer", "source": "assigned"}` and finds it served
    unchanged), and `role_in` never returns `admin`;
  * `display_name_suggested` is the cleaned claim while nothing is stored,
    `null` after a save, and `null` for a claim that fails a rule.

### PROFILE-DEFAULT: the default applies in authentication

* Files: `apps/swarm-api/swarm_api/auth.py`,
  `apps/swarm-api/swarm_api/routes/tenants.py`, `docs/multi-tenancy.md`,
  `tests/unit/control_plane/test_default_workspace.py` (new),
  `tests/unit/control_plane/test_tenant_switcher.py`.
* Red first:
  * a headerless request acts in the stored group default;
  * a header naming the personal workspace still wins;
  * a default the person is no longer a member of is ignored, with
    `not_a_member`, and the stored value is untouched;
  * an unreadable preferences document is 503 `preferences_unavailable` for
    a headerless submission, the store holds no new task, and the message
    names the switcher, `SWARM_TENANT` and `--tenant`;
  * the same document unreadable with a header is served normally;
  * `PREFERENCE_FREE_ROUTES` is exactly the three routes of §4.5 (so not
    `GET /v1/notifications`), and each is served with
    `default_source: "unavailable"`;
  * `AuthContext.preferences` holds the document read, and `None` when it
    could not be read;
  * `tenants/mine`'s first entry equals the headerless tenant in every case
    above;
  * a service identity never reads preferences;
  * (Q11 (a)) `AuthContext.display_name` is the stored name when set, else
    the token's name claim, else the email's local part.

### PROFILE-BELL: topics, muting and reminders

* Files: `apps/swarm-api/swarm_api/notifications.py`,
  `apps/swarm-api/swarm_api/routes/notifications.py`,
  `docs/notifications.md`,
  `tests/unit/control_plane/test_notification_preferences.py` (new).
* Red first:
  * every item carries a topic from `TOPICS`;
  * a failed workflow is `failed` and a succeeded one `finished`;
  * a muted topic leaves the list and the badge and is counted in `muted`,
    with needs-you items counted under `needs_you`;
  * `admin_pending` ignores muting;
  * unreadable preferences with `X-Swarm-Tenant` serve everything
    unfiltered with `partial: true`; without the header the bell is 503
    `preferences_unavailable` from authentication, and no derivation runs
    (the route is not in `PREFERENCE_FREE_ROUTES`);
  * with `every_4h`, an approval read 5 h ago is unread and one read 3 h ago
    is read;
  * a waiting step uses the person's cadence when it is not `as_set` and the
    author's `remind_after_seconds` when it is (Q4), including `never`
    holding `k` at 0;
  * an update never re-rings;
  * `mark_all_read` marks only shown items.

### PROFILE-NAMES-API: names beside emails (swarm-api)

* Files: `apps/swarm-api/swarm_api/people.py`,
  `apps/swarm-api/swarm_api/routes/approvals.py`,
  `apps/swarm-api/swarm_api/routes/tasks.py`,
  `apps/swarm-api/swarm_api/routes/workflows.py`,
  `docs/security.md`,
  `tests/unit/control_plane/test_display_name_fields.py` (new).
* Red first:
  * each route of the §4.4 table serves the `*_name` sibling of every email
    field it serves, and no name for an email it does not serve;
  * a page naming ten tasks by three people makes one name `get_all` of
    three ids;
  * a failed lookup serves every name as `null` and the page is a 200;
  * People's row carries no default workspace, mute or reminder field (Q8).

### PROFILE-PAGE: the screen (UI, 5 files)

* Files: `apps/swarm-ui/src/Profile.tsx` (new), `apps/swarm-ui/src/api.ts`,
  `apps/swarm-ui/src/types.ts`,
  `apps/swarm-ui/src/__tests__/profile.test.tsx` (new),
  `apps/swarm-ui/src/__tests__/profile.routes.test.ts` (new).
* Red first:
  * the four cards render from a fixture;
  * section 2 renders exactly a GitHub card and a TypeSafe card, and no
    text mentions SwarmCloud API keys or links to them (Q6);
  * one GitHub row per workspace in the fixture (Q10);
  * a role named `maintainer` with `source: "assigned"` renders verbatim,
    and `via` renders when present (Q7);
  * the name editor offers `display_name_suggested` as its initial value,
    and shows a `display_name_taken` refusal's message;
  * no rendered text contains a planted sentinel field;
  * each summary links to its owning page;
  * saving sends `revision`, and a 409 reloads;
  * an `unavailable` section renders "could not be read" rather than empty,
    and `preferences` in `unavailable` disables the editors;
  * `loadProfile` and `putPreferences` call `/v1/profile` and
    `/v1/profile/preferences`, so `test_runtimes_screen.py::test_every_v1_path_the_ui_calls_is_served_by_this_api`
    stays green only because PROFILE-API landed first.

### PROFILE-NAV: the Settings tab and the issue forms (6 files)

* Files: `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/paths.ts`,
  `.github/ISSUE_TEMPLATE/bug_report.yml`,
  `.github/ISSUE_TEMPLATE/feature_request.yml`,
  `tests/unit/scripts/test_issue_forms.py`,
  `apps/swarm-ui/src/__tests__/profile.nav.test.tsx` (new).
* Red first:
  * `test_issue_forms.py` gains Settings › Profile in `_nav_options`, which
    fails until both forms carry the option;
  * `profile.nav.test.tsx`: `/settings/profile` resolves to the Profile
    screen and its heading.

### PROFILE-SHELL: the panel, the switch and the bell's muted line (6 files)

* Files: `apps/swarm-ui/src/Spine.tsx`, `apps/swarm-ui/src/fetch.ts`,
  `apps/swarm-ui/src/Notifications.tsx`,
  `apps/swarm-ui/src/__tests__/tenant.switcher.test.tsx`,
  `apps/swarm-ui/src/__tests__/nav.links.test.tsx`,
  `apps/swarm-ui/src/__tests__/notifications.test.tsx`.
* Red first:
  * a switch is written to `sessionStorage`, survives a reload, and is
    absent in a new session (Q2);
  * an old `localStorage` choice is removed on first load and not honoured;
  * "Back to default" clears the choice;
  * a 503 `preferences_unavailable` shows its message beside the switcher;
  * the panel foot links to Settings › Profile;
  * the bell shows "1 muted item needs you" when `muted.needs_you` is 1, and
    nothing when it is 0 (Q3).

### PROFILE-NAMES-UI: a person's name, always with their email (UI, 6 files)

* Files: `apps/swarm-ui/src/PersonName.tsx` (new),
  `apps/swarm-ui/src/Approvals.tsx`, `apps/swarm-ui/src/People.tsx`,
  `apps/swarm-ui/src/Workflows.tsx`, `apps/swarm-ui/src/AgentDetail.tsx`,
  `apps/swarm-ui/src/__tests__/person.name.test.tsx` (new).
* Red first:
  * `PersonName` renders `Name · email`, and the email alone when the name
    is `null`;
  * with a 64-character name in a narrow box, the full email is in the DOM
    and the name is the part truncated;
  * Approvals' `decided_by`, People's row, the workflow page's owner and the
    task page's submitter each render through `PersonName` (the test finds
    the component's marker on each).

### PROFILE-SC: `/sc` and the CLIs say which default applied

* Files: `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/client.py`,
  `plugin/.claude-plugin/plugin.json`, `tests/unit/mcp/test_sc_tenant.py`,
  `tests/unit/mcp/test_tenant_choice.py`,
  `tests/unit/mcp/test_plugin_user_config.py`.
* Red first:
  * with no `SWARM_TENANT`, `sc whoami` reports "your default, from Settings ›
    Profile" when `tenants/mine` says `default_source: profile`, and the
    personal-workspace wording otherwise;
  * `default_source: unavailable` is reported as "your preferences could not
    be read; name a workspace";
  * a 503 `preferences_unavailable` is printed with the API's message and a
    non-zero exit, and is never retried in another workspace;
  * `SWARM_TENANT` still wins and is reported as such;
  * `swarm_tenants` marks the default;
  * the plugin's `tenant` description names Settings › Profile.

---

## 7. Owner questions

### 7.1 Answered 2026-10-17/18 (PR 1506's comments; Q11 and Q12 on 2026-10-18)

| # | question | answer | where the design follows it |
|---|---|---|---|
| Q1 | What keys the preferences document's id? | An HMAC under the existing `swarm-child-key`, purpose `"user-prefs:" + email` (as recommended) | §4.3 |
| Q2 | How long does a console tenant switch last? | The tab's session (`sessionStorage`); a new window opens in the default (as recommended) | §4.5, PROFILE-SHELL |
| Q3 | What may be muted? | Every topic, with a quiet "N muted items need you" line under the bell (as recommended) | §4.6, PROFILE-BELL, PROFILE-SHELL |
| Q4 | Whose reminder period wins for a waiting step? | The person's when they chose one; the author's at "as set" (as recommended) | §4.6, PROFILE-BELL |
| Q5 | Where does the display name come from? | **An editable name stored in the preferences document** (departs from the recommendation) | §4.4, PROFILE-STORE, PROFILE-NAMES-API, PROFILE-NAMES-UI |
| Q6 | SwarmCloud API keys do not exist. What does the page do? | **Leave the row out**; connections are GitHub and the TypeSafe key only (departs) | §4.1, §4.2, PROFILE-PAGE |
| Q7 | What is "my role" in a workspace? | **Per-workspace roles will be introduced by a separate design (#1518)**; this page shows today's derived role behind one seam (departs) | §4.2 (`role_in`, `role`), PROFILE-API |
| Q8 | May an admin see or change another person's preferences? | No: no admin route, no `?user=`, no preferences section on Admin › People | §4.3, PROFILE-API, PROFILE-NAMES-API |
| Q9 | If the preferences cannot be read, what does a headerless request do? | 503 `preferences_unavailable`, saying how to name a workspace; nothing submitted; never a silent fallback | §4.5, PROFILE-DEFAULT, PROFILE-SC |
| Q10 | Which workspaces does the GitHub summary cover? | Every workspace the person can act in, one row each, from one batched read (as recommended) | §4.2, PROFILE-API |
| Q11 | Does the edited display name also name the agent's commits? | **Yes, answered 2026-10-18:** the stored name, else the token's name claim, else the email's local part. The email is in every commit beside it, and a task keeps the name recorded at admission, so a later rename never rewrites a commit | §2.2, §4.4, PROFILE-DEFAULT |
| Q12 | Does a one-word name block every longer name that contains it? | **No, answered 2026-10-18:** only names of two or more words are checked for containment. An exact match is still refused for every name, and "Ada Lovelace Admin" is still refused when "Ada Lovelace" exists, but "Bob Smith" is allowed when "Bob" exists; the email shown beside every name covers the rest. Departs from the lane's recommendation | §4.4 rule 6, PROFILE-STORE |

#### Amendment, 2026-10-19: first name in the console, full name where the person is the subject

The owner checked the built Profile page in Chrome and decided: *"Show the
first name for each user, only show full name in the profile page or in the
user administration sections in admin for each user."*

This narrows Q11's "one name everywhere" to **what is stored and what names a
commit**. It does not change either: the stored name is still the whole name
the person saved, and Q11 (a) still makes that whole name the agent's commit
author. Only how the console **displays** a person changes (lane
PROFILE-FIRSTNAME, part of #1505):

* `PersonName` (`apps/swarm-ui/src/PersonName.tsx`) prints the saved name's
  **first word** by default (`variant="first"`): split on whitespace only, so
  a hyphen or an apostrophe stays in the word ("Mary-Jane O'Neil" is
  "Mary-Jane"), and a one-word name is the whole name.
* The **full** name is printed only where the person is the subject:
  Admin › People (its table and its per-person views, `variant="full"`) and
  Settings › Profile, whose name editor holds the whole saved name.
* §4.4's rule is unchanged: a name is never shown without its email, the
  email is never the part that is cut, and no saved name still prints the
  email alone. The whole name is in the element's title, so the short form
  hides nothing a hover cannot show.
* The panel's signed-in line shows the caller's saved first name, else the
  email's local part as before. `GET /v1/tenants/me` does not carry the name,
  so the shell reads `GET /v1/profile` for it once per session (Spine.tsx
  `askOwnName`), and the Profile page's own read and save update it.

Why a first name is safe here: the name was never the identity (§4.4). Two
people who share a first name are still told apart by the email beside each,
which is exactly the case §4.4 put the email there for.

### 7.2 Open

Nothing is open. Q11 and Q12 were answered on 2026-10-18 and are in §7.1.
