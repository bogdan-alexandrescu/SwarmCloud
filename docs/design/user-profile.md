# User profile: one page that says who you are, where you work, what reaches your bell and which Claude accounts are yours

**Status: designed 2026-10-18 (lane PROFILE-DESIGN, part of #1505).** Nothing
here is built. The owner answers §7 before any build lane starts.

Every file and symbol cited below was read on 2026-10-18 at `6174f32`. Lines
move, so each citation names the symbol; search for it.

The owner's request (#1505, 2026-10-18): a **Settings › Profile** page with
four sections, all chosen by the owner:

1. identity and workspaces, with a default workspace for the console and for
   `/sc`;
2. a summary of the person's GitHub connection, SwarmCloud API keys and
   TypeSafe key, **linking** to Work › Access and Settings › API keys, which
   stay the source of truth;
3. notification preferences: what reaches the bell (approvals, steps waiting
   for them, failed runs) and how often reminders repeat;
4. their personal Claude subscription accounts and quota, linked from
   Capacity › Accounts.

What the design settles, in one line each:

* **The page reads; it edits only what has no other home.** The default
  workspace and the notification preferences are the only things saved from
  Profile. Everything else is a summary that links to the page that owns it,
  so there is never a second editor for the same thing (§4.1).
* **Preferences live in one per-person document that no worker can find.**
  `user_preferences/<id>`, where the id is an HMAC of the verified email under
  a key only swarm-api and the scheduler hold. It is written only by
  swarm-api, only for the caller, and never inside a tenant document (§4.3).
* **The default workspace is applied by the API, not by each client.** A
  request without `X-Swarm-Tenant` acts in the stored default when the person
  is still a member of it. Any explicit choice still sends the header and
  still wins, so the console, `/sc`, `swarm` and the MCP bridge all follow one
  rule without each restating it (§4.4).
* **The bell stays derived on read.** Preferences are a filter applied after
  the sources derive their items, and a reminder is a read mark that has aged
  out. Nothing is queued, stored or sent (§4.5).
* **No credential value is ever on the page**, and neither are the names a
  credential is stored under. Keys show set or not set, when, by whom and
  their version. The response is built from an allow-list of fields, and a
  test plants a value and looks for it (§4.2).
* **"SwarmCloud API keys" do not exist today.** `/sc` signs in with Google
  (`sc login`). The page says so instead of inventing a row. Whether to build
  them is owner question Q6.

---

## 1. The problem, as measured

### 1.1 Where "me" lives today

The issue's table, checked against the code on 2026-10-18:

| What | Where it lives | What the code shows |
|---|---|---|
| TypeSafe key (personal and workspace) | Settings › API keys, `/settings/keys` | `apps/swarm-ui/src/ProviderKeys.tsx::ProviderKeysScreen`, over `GET /v1/provider-keys/{provider}` (`apps/swarm-api/swarm_api/routes/provider_keys.py::get_status`). The only provider is TypeSafe. The same page also picks the Jev judgment backend. |
| SwarmCloud API keys | **nowhere** | No such object exists. `/sc` and the bridge sign in with the person's Google identity (`apps/swarm-mcp/swarm_mcp/auth.py`, `sc login`), and the refresh token stays on that machine (`apps/swarm-mcp/swarm_mcp/credentials.py`). |
| GitHub connection and repository grants | Work › Access, `/access` | `apps/swarm-ui/src/Access.tsx::AccessScreen`, over `GET /v1/access` (`routes/access.py::overview`). The connection is **per workspace and person**: `forgeapp.py::connection_id_for(tenant_id, email)`. |
| Personal Claude accounts | Capacity › Accounts, `/capacity/accounts` | `apps/swarm-ui/src/Accounts.tsx::AccountsScreen`, over `GET /v1/accounts` (`routes/accounts.py::list_accounts`), scoped to the **acting** workspace. "Personal" is not a field. It is implied by `owner_tenant` being a `u-` workspace. |
| Onboarding | Work › Setup, `/setup` | the workspace request and the loan request (`routes/workspaces.py`) |
| Who I am | **no page** | `GET /v1/tenants/me` returns `principal.email`, `domain`, `groups`, `is_admin`. `display_name` exists only when the token carries a `name` claim, and an IAP assertion never does (`auth.py::AuthContext`). |
| My workspaces and my role in each | **no page** | `GET /v1/tenants/mine` lists the choices (`routes/tenants.py::get_mine`). There is **no per-workspace role**: `routes/provider_keys.py` and `routes/gittokens.py` both say "there is no per-tenant admin role". The only roles are platform roles, `admin_roles/{email}` (`admins.py`). |
| A default workspace | **no page, and not stored** | The default is computed on every request: the personal `u-` workspace when it is `ready`, else the first confirmed group (`auth.py::Authenticator._with_own_workspace`, owner decision 2026-10-12). |
| Notification preferences | **no page, and no concept** | The bell (`apps/swarm-api/swarm_api/notifications.py`) derives 5 groups from 8 sources on each read. Nobody can turn any of them off. A grep of `apps/` for `remind` finds nothing, so nothing repeats either. |

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

### 1.3 What "done" lets someone do that they cannot today

* Open Settings › Profile and see, on one screen, their identity, every
  workspace they can act in with their role and its state, the GitHub
  connection **in each workspace**, their TypeSafe key's state and version,
  and their own Claude accounts with each one's 5-hour and 7-day use.
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
  that exposure today, and so does `people/u-<slug>`.
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
  memberships the request already resolved, never ask again.
* **The personal workspace id is derived from the email by the frozen
  `swarm_common/identity.py::tenant_id_for_user`.** The slug drops the domain,
  so `bob@a` and `bob@b` derive one id. `auth.PersonalTenants.ready` tells
  them apart by the stored principal. Anything that names "your personal
  workspace" must go through that check, not through the slug alone.
* **The Settings tabs are not in `SECTIONS`.** They are constants in
  `apps/swarm-ui/src/App.tsx` (`SETTINGS`, `KEYS_TAB`, `WORKSPACE_TAB`).
  `tests/unit/scripts/test_issue_forms.py::_nav_options` adds them to the
  forms' expected "Where" list by hand. A new Settings tab therefore needs
  three changes together: the tab, a "Web UI · Settings › Profile
  (/settings/profile)" option in both `.github/ISSUE_TEMPLATE/bug_report.yml`
  and `feature_request.yml`, and the test's hand-written list. One lane makes
  all three (§6, PROFILE-NAV).

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

**1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Untouched.
Profile reads documents and writes one document of its own. A reminder is a
read mark that has aged, not a task, a timer or a job. Nothing waits, so
nothing costs while it waits.

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
moves it.

**6. Spot disabled; 7. requests == limits.** Untouched. No infrastructure.

**8. Mandatory checkpointing.** Untouched. No worker or runner change.

**9. Per-tenant isolation.** This is the invariant the design has to argue,
in four places:

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
* *Profile shows the person's own things across workspaces, and nothing of
  anyone else's.* It reads, per workspace the caller is in, only documents
  keyed by **(that workspace, this email)**: the GitHub connection. It reads
  the TypeSafe status of the caller's own `u-` scope and of the acting
  workspace, which the API-keys page already shows them. It reads the Claude
  accounts **owned by** or **lent to** their own personal workspace. A group
  workspace's pool accounts, other members' connections and other members'
  keys are never read.
* *Preferences never enter a tenant document.* Not `tenants/<t>`, not
  `workspace_settings/<t>`, not `people/<u->`. The scheduler, the reconciler
  and the worker never read `user_preferences`. Only swarm-api does, for the
  caller.

**10. Callers pick a `runner_profile` by name.** Untouched. Preferences hold
a workspace id, topic names and a reminder name, each checked against a
closed set. None of them is an image, a command, a resource or a backend
parameter, and none reaches a task.

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing. §5 records that as a request for no change, so no build lane adds
one. The personal workspace id comes from the frozen `tenant_id_for_user`.
The bell's topics, the preference document and the profile response are
swarm-api's own and live there.

---

## 3. Designs that could work

The decisions that separate the designs are where preferences are stored and
who applies the default workspace. The bell filter and the page are much the
same in all three.

### 3.1 Design A: the API stores preferences and applies the default (recommended)

**What it is.** swarm-api gains `user_preferences/<hmac id>`, one document per
person, and two routes: `GET /v1/profile` (the whole page in one read) and
`PUT /v1/profile/preferences`. Authentication reads the stored default in the
same batch as the personal-workspace check it already makes, and moves the
default to the front of `tenant_choices`. Every headerless request from every
client then acts in it. The bell reads the preferences after deriving its
items and filters them.

**What it costs.**

* *Code:* three new swarm-api modules (preferences, profile aggregation,
  routes), changes to `auth.py`, `notifications.py` and `routes/tenants.py`,
  one new UI screen, the Settings wiring, and wording in the plugin. Seven
  lanes (§6), most of them small.
* *Migration:* none. A person without a document gets today's behaviour
  exactly: no default (the computed one), nothing muted, no reminders.
* *Latency:* authentication for a person already reads `tenants/u-x` and
  `workspaces/u-x`, behind a per-instance cache, when it checks the personal
  workspace (`auth.PersonalTenants.ready`, two `get` calls today). The build
  folds the three documents into one `get_all` on a cache miss, cached 15 s
  per instance like `admins.ROLE_CACHE_SECONDS`. One more document in a read
  that already happens adds about a millisecond. That is an estimate, measured in PROFILE-DEFAULT's
  acceptance run. The bell adds no read, because it uses the context
  authentication already loaded.
* *Money:* one document read per person per 15 s per instance at most. That
  is pennies a month at this platform's size.

**What it breaks.** A person who sets a group as default and then runs `swarm`
without `--tenant` in a script acts in the group, not the personal workspace.
That is the point, but it changes a headerless script's tenant. The CLI's
`whoami` and `sc config` say which default applied and why (PROFILE-SC), so
the change is visible, not silent.

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
several places.

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
on the server, would disagree with the list.

### 3.4 Recommendation: A

A is the only design in which one setting governs every client, because the
API is the one place every client already asks "which workspace am I in". It
reuses a read authentication already makes, and it keeps `tenants/mine`'s
first entry and the headerless tenant the same thing by construction. B
restates one rule in four clients. C does not deliver the issue.

---

## 4. The recommended design, in enough detail to build

### 4.1 What the page shows, and where each part's source of truth is

| Section | Shown | Editable here | Source of truth, linked |
|---|---|---|---|
| **1. You** | email; display name when the token carries one (Q5); platform role (owner, admin, or none) | no | identity provider; Admin › People for roles |
| **1. Workspaces** | each workspace from `tenant_choices`: name, kind, your role (Q7), its state (personal workspace: `requested`, `applying`, `ready`...), which one you are in now, which is your default | **the default workspace** | Work › Setup for requesting a personal workspace |
| **2. GitHub** | per workspace: `@login`, method, state (`active`, `refresh_failed`, `revoked`, or not connected), token expiry, last renewal | no | Work › Access, in that workspace |
| **2. TypeSafe key** | your personal key: set or not set, when, by whom, version; the acting workspace's key: set or not; which one your tasks in the acting workspace would use | no | Settings › API keys |
| **2. SwarmCloud sign-in** | "SwarmCloud signs you in with Google; there are no SwarmCloud API keys" and the `sc login` command (Q6) | no | none |
| **3. Notifications** | one switch per topic (§4.5) and the reminder cadence; a line saying how many items are muted right now | **all of it** | this page |
| **4. Claude accounts** | accounts your personal workspace owns, and accounts lent to it: label, state, 5-hour and 7-day use with reset times, who you lend to (count), stale flag | no | Capacity › Accounts |

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

### 4.2 API

Both routes are in a new router, `apps/swarm-api/swarm_api/routes/profile.py`,
prefix `/v1/profile`. They are for people. A caller for which
`Authenticator._special` is true (the schedule tick, the rollup sweeper, a
listed service account, any service identity) gets 403 `profile_people_only`.
Neither route takes an email, a user id or a tenant id that selects **whose**
profile. The caller is always the verified identity.

#### `GET /v1/profile`

One read for the whole page. Each section is computed by its own function. A
section that fails fails alone: it is left out and named in `unavailable`, as
`notifications.py` does with `partial`. A broker outage therefore costs
section 4, not the page.

```json
{
  "identity": {
    "email": "ada@example.com",
    "display_name": null,
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
      "role": "owner",
      "via": null,
      "state": "ready",
      "is_acting": false,
      "is_default": false
    },
    {
      "tenant_id": "team-x",
      "display_name": "team-x@example.com",
      "kind": "group",
      "role": "member",
      "via": "team-x@example.com",
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
    },
    "swarmcloud_keys": {"exists": false, "sign_in": "sc login"}
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

* **`identity`** comes from `AuthContext` only, the same values
  `GET /v1/tenants/me` serves. `platform_role` is `admin_roles/{email}.role`
  (`owner`, `admin`) through `admins.py`'s 15-second cache, or `"admin"` for
  an admin-group member or listed admin, or `null`.
* **`workspaces`** is exactly `auth.tenant_choices`, in its order, which is
  already default first. No Cloud Identity call is made for it (§2.1). `role`
  is derived (Q7): `owner` for the confirmed personal workspace, `member` for
  a group, with `via` naming the group. `state` is read from
  `workspaces/u-x` for the personal entry only. A person whose personal
  workspace is not ready yet does not have it in `tenant_choices`, so it is
  added as a non-selectable row with its state, so the page can say
  "requested 2 days ago" instead of hiding it.
* **`default_workspace.source`** is `"profile"` (stored and honoured),
  `"personal"` (today's rule: the ready personal workspace) or
  `"first_group"`. `ignored` is `null` or the reason a stored default was not
  honoured: `not_a_member` (the group no longer confirms the person),
  `not_ready` (the personal workspace is not ready) or `unknown`. The stored
  value is **kept** when ignored, so a person whose group membership blinks
  during a Cloud Identity outage does not lose their setting.
* **`connections.github`**: one entry per selectable workspace, read by
  `get_all` over `forge_connections/<connection_id_for(t, email)>`. That is N
  computed ids and no query. N is the number of workspaces, single digits
  here. **Allowed fields only:** `state`, `forge_login`, `method`,
  `access_expires_at`, `refreshed_at`, `failure`. Never `secret_name`,
  `token_id`, `connection_id` or `forge_user_id`.
* **`connections.typesafe`** reuses `provider_keys.ProviderKeys.status` and
  `provider_keys.resolve_provider_key`, and serves only `set`, `set_at`,
  `set_by`, `version` and `uses`. Never `secret_id`, although
  `provider_keys.py::_status` returns one.
* **`swarmcloud_keys`** is a constant today (Q6). It is in the payload,
  rather than hard-coded in the page, so the page needs no change if Q6
  later adds keys.
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
  "default_workspace": "team-x",
  "notifications": {"muted": ["finished"], "reminder": "every_4h"},
  "revision": 4
}
```

* The whole preference set is replaced. Unknown fields are a 422
  (`extra="forbid"`).
* `default_workspace` is `null` (use the computed default) or a `tenant_id`
  in this request's `tenant_choices`. Anything else is 422
  `not_your_workspace`, and the message never says whether the workspace
  exists.
* `muted` is a subset of `TOPICS`, without duplicates. `reminder` is one of
  `as_set`, `every_4h`, `daily` or `never` (§4.5).
* `revision` must equal the stored revision, or the request is a 409
  `preferences_changed`, and the page reloads. Two open tabs cannot silently
  overwrite each other.
* The response is the `GET`'s `default_workspace`, `notifications` and
  `revision` sections. The instance drops its 15-second cache entry for the
  caller, so the next request on that instance sees the change. Other
  instances see it within 15 s. The page says "applies to new sessions within
  a few seconds".
* It is logged as one structured line naming the caller's `user_hash` and
  which fields changed, never their values.

### 4.3 Firestore: `user_preferences/<id>`

```
user_preferences/up_<32 hex>
{
  "v": 1,
  "default_workspace": "team-x" | null,
  "notifications": {
    "muted": ["finished"],
    "reminder": "as_set" | "every_4h" | "daily" | "never"
  },
  "revision": 4,
  "updated_at": <timestamp>
}
```

**The id.** It is `up_` followed by the first 32 hex digits of
`HMAC-SHA256(swarm-child-key, "swarm-profile-id/v1\n" + lower(email))`. This
is the same construction and the same key as the child-key registration's
document id (`apps/swarm-api/swarm_api/childkey.py`, module docstring: "the
registration's DOCUMENT ID, which no tenant identity can compute"),
domain-separated by its own purpose string so it can never be read as a
registration. The key is held by swarm-scheduler and swarm-api only. Q1 asks
whether to use a dedicated key instead.

Why an HMAC and not `user_hash`, which `notification_reads` uses: §2.2
invariant 9. A planted default workspace misfiles work across tenants. A
planted read mark only hides an item from the bell, which is why the read
marks could afford a computable id and this document cannot.

**Key rotation.** `childkey.py` holds the current and, during a rotation,
the previous `swarm-child-key` version. The preferences module computes the
id under each configured version, reads them
in one `get_all`, uses the newest that exists, and writes under the newest
key. If every version has been rotated out, the person gets the computed
default and an unmuted bell until they save again. That is today's
behaviour, so a lost document fails safe.

**What it does not hold:** the email (the id is enough), a display name, any
credential or credential name, and anything the scheduler, reconciler or
worker reads. A worker that creates a document in `user_preferences` under an
id of its own invention creates one that no person's request ever reads.

**Who writes it:** swarm-api, only in `PUT /v1/profile/preferences`, only for
the verified caller. No admin route writes another person's preferences
(Q8).

### 4.4 The default workspace: one rule, applied in authentication

`Authenticator._with_own_workspace` (in `auth.py`) already builds
`tenant_choices` with the personal workspace first. A new step runs after
it, in the same method or a sibling called from the same place:

1. When the caller is a person (not `_special`), read
   `user_preferences/<id>` in one `get_all` with `PersonalTenants.ready`'s
   two documents (which that method reads with two `get` calls today), through a 15-second per-instance cache keyed by email.
2. When `default_workspace` is set **and** is in `tenant_choices`, move it to
   the front. Otherwise leave the order alone, and record why on the context
   (`default_source`, `default_ignored`) for `GET /v1/profile` and
   `GET /v1/tenants/mine`.
3. `_select_tenant` is unchanged. A request with `X-Swarm-Tenant` acts in the
   header's tenant, and one without acts in the first choice, as it does
   today.

**Failure.** If the preferences document cannot be read and the request
carries no header, the request is a 503 ("your preferences could not be
read; retry shortly"). Guessing would act in a tenant other than the one the
person chose, which is the misfiling `_with_own_workspace` already refuses
for a failed personal-workspace read (its docstring: "A failed read is a 503,
never a guess"). The two documents are read in one batch, so this adds no
new way to fail: a Firestore outage that fails one fails both. A request that
names its tenant is unaffected. Q9 asks the owner to confirm.

**Precedence, per client. Every explicit choice still wins.**

| client | explicit choice (wins) | then |
|---|---|---|
| console | the panel's tenant switch (`X-Swarm-Tenant` from `fetch.ts::chosenTenant`) | the stored default, then today's rule, all in the API |
| `/sc`, the bridge, `sc` | `SWARM_TENANT` in Claude Code's environment, then the plugin's `tenant` option (`plugin/.claude-plugin/plugin.json`; `plugin/hooks/session-start.sh`) | the same |
| `swarm` CLI | `--tenant`, then `SWARM_TENANT` (`swarm_mcp/client.py::chosen_tenant`) | the same |

No client changes its precedence. The plugin and CLI change only what they
**say** when they send no header: `sc.py::_DEFAULT_TENANT_SOURCE` says "the API
chose your default: the one in Settings › Profile, else your personal
workspace", and `sc whoami` / `sc config` report the `default_source` that
`GET /v1/tenants/mine` now returns (PROFILE-SC).

**The console's remembered switch.** Today the panel's switch is stored in
`localStorage` and lasts forever in that browser. Left alone, "the console
opens in my default" would be true only for someone who has never switched.
The recommendation (Q2) is to keep the switch for the **tab's session**
(`sessionStorage`): a reload keeps it, and a new window opens in the default.
The panel's tenant block marks the default workspace and offers "Back to
default". `dropRefusedTenant` keeps working unchanged.

**`GET /v1/tenants/mine`** gains two fields per entry: `is_default` (true on
the first entry) and, on that entry, `default_source` (`profile`, `personal`
or `first_group`). The entries and their order otherwise stay exactly
`tenant_choices`, so its documented promise holds.

### 4.5 Notification preferences on a bell derived on read

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

**Filtering happens after derivation, at read time.** `Notifications.feed`
derives every item as today, drops items whose topic the caller muted, and
counts what it dropped:

```json
"muted": {"needs_you": 1, "updates": 12}
```

The badge (`unread_needs_you`) counts only what is shown. The `muted` line is
shown under the list ("1 muted item needs you · change in Settings ›
Profile") so a muted approval is never invisible, only quiet (Q3).
`mark_all_read` marks the shown items only. A muted item that is unmuted
later shows as unread, which is true. `admin_pending` drives the panel's
Admin pill, not the bell, so it is computed before the filter: an admin who
mutes `admin` still sees the pill.

**If the preferences cannot be read, the bell is served unfiltered**, with
`partial: true`. Muting is a convenience, and over-telling is the safe
failure.

**Reminders are read marks that age out. Nothing is stored.**

* `as_set` (the default) is today's behaviour. A read needs-you item stays
  read. A waiting step re-rings on its author's `remind_after_seconds`.
* `every_4h` / `daily`: a read mark (or `all_read_at`) on a **needs-you**
  item counts only while it is younger than 4 h / 24 h. After that, the item
  shows as unread again, and it keeps doing so until whatever it is about is
  resolved and it no longer derives. Updates never re-ring.
* `never`: no needs-you item re-rings, waiting steps included. For a waiting
  step that means its reminder index `k` is held at 0.

For a waiting step, the period is the person's when they chose `every_4h` or
`daily`, and the author's when they chose `as_set` (Q4). Its item id still
rotates per period (`task:input:<task>:<k>`), so HW-NOTIFY's rule is kept,
not replaced.

Marks are pruned at `KEEP_READS` (30 days) as today, and `daily` needs a
mark at most 1 day old, so no new retention is needed. The two settings
compose without a timer, a queue or a write, so "derived, never stored"
(`docs/notifications.md`) still holds word for word.

**What the bell does not gain:** e-mail, push or chat delivery. The issue asks
what reaches the bell, and the bell is the only channel. A delivery channel
would be stored and sent, and it would be its own design.

### 4.6 Changes by component, and the files each lands in

**swarm-api**

* `apps/swarm-api/swarm_api/preferences.py` (new): the id derivation, the
  read across key versions, the cached read for authentication, the
  transactional write with the revision check, `TOPICS` and `REMINDERS`.
* `apps/swarm-api/swarm_api/profile.py` (new): one function per section of
  `GET /v1/profile`, each from an allow-list, each failing alone.
* `apps/swarm-api/swarm_api/routes/profile.py` (new): the two routes.
* `apps/swarm-api/swarm_api/main.py`: `include_router(profile.router)`.
* `apps/swarm-api/swarm_api/auth.py`: §4.4 steps 1-2, and `default_source` /
  `default_ignored` on `AuthContext`.
* `apps/swarm-api/swarm_api/routes/tenants.py`: `is_default` and
  `default_source` in `_choice`.
* `apps/swarm-api/swarm_api/notifications.py`: topics, the filter, the
  reminder rule, `muted`.
* `apps/swarm-api/swarm_api/routes/notifications.py`: passes the caller's
  preferences to `Notifications.feed`.

**Console** (`apps/swarm-ui/src/`)

* `Profile.tsx` (new): `ProfileScreen`, four cards, built with `useUrRead`
  and `UrRegion` like `ProviderKeys.tsx` and `CodeIntelligence.tsx`.
* `api.ts`: `loadProfile`, `putPreferences`. `types.ts`: `Profile`,
  `Preferences`, `NotificationTopic`, `NotificationFeed.muted`,
  `TenantChoice.is_default` and `default_source`.
* `App.tsx`: `PROFILE_TAB = 'profile'`, `PROFILE_LABEL = 'Profile'`, and the
  address, title and render branches beside `WORKSPACE_TAB`'s. `paths.ts`:
  `'settings/profile': '/settings/profile'` in `FIXED`.
* `Spine.tsx`: a "Settings › Profile" link in the panel foot beside the two
  existing `sk-pset` links, the default marker and "Back to default" in
  `TenantBlock`. `fetch.ts`: the switch kept in `sessionStorage` (Q2).
* `Notifications.tsx`: the muted line.

**Issue forms:** `.github/ISSUE_TEMPLATE/bug_report.yml` and
`feature_request.yml` gain "Web UI · Settings › Profile (/settings/profile)".
`tests/unit/scripts/test_issue_forms.py::_nav_options` adds `PROFILE_TAB` /
`PROFILE_LABEL` beside the API-keys and Workspace entries.

**Plugin and CLIs:** `apps/swarm-mcp/swarm_mcp/sc.py` (`_DEFAULT_TENANT_SOURCE`,
`read_whoami` reporting `default_source`), `apps/swarm-mcp/swarm_mcp/client.py`
(`tenant_listing` marks the default), and `plugin/.claude-plugin/plugin.json`
(the `tenant` option's description names Settings › Profile).

**Docs:** `docs/notifications.md` (topics, muting, reminders) and
`docs/multi-tenancy.md` (the default's new first rule).

**Not changed:** the scheduler, the reconciler, the worker, the quota broker,
Terraform (Q1(a) reuses an existing key), and `apps/common/swarm_common/`.

### 4.7 Failure cases

| case | what happens |
|---|---|
| Stored default is a group the person left | ignored (`not_a_member`), computed default used, Profile says so, value kept |
| Stored default is the personal workspace, which is now being rebuilt | ignored (`not_ready`), same |
| Preferences unreadable, headerless request | 503, never a guess (Q9) |
| Preferences unreadable, bell | unfiltered, `partial: true` |
| Broker down | Profile omits `claude_accounts`, names it in `unavailable` |
| Cloud Identity down | as today: tenant resolution is a 503. Profile is not reachable either, because the person's workspaces cannot be known |
| Two tabs save | the second gets 409 and reloads |
| A tenant agent creates `user_preferences/<anything>` | read by nobody |
| `swarm-child-key` rotated out entirely | preferences reset to today's behaviour until saved again |

---

## 5. Frozen contract: change requests

### Request P1. No change to `apps/common/swarm_common/` for the user profile

**Status:** PROPOSED by lane PROFILE-DESIGN, part of #1505. Requests no
change, so nothing is to be applied. It is recorded so no build lane adds
one.

* **Preferences are not a contract type.** Only swarm-api reads or writes
  `user_preferences`. The scheduler, reconciler and worker never see it. A
  type in `swarm_common` would invite exactly those readers.
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

Seven lanes. No two lanes edit the same file. Every UI lane names at most six
files. Each lane pushes its red tests first (CLAUDE.md, "Red first"), and runs
`scripts/changed-guards.sh` and `scripts/publish-scan.sh` before finishing.

**Order:** PROFILE-API first. Then PROFILE-DEFAULT, PROFILE-BELL and
PROFILE-PAGE, in parallel. Then PROFILE-NAV (after PAGE), PROFILE-SHELL
(after PAGE and BELL) and PROFILE-SC (after DEFAULT).

**PROFILE-BELL and HW-NOTIFY both edit `notifications.py`**, so they are
serial, never parallel. Whichever lands second rebases. If HW-NOTIFY lands
first, PROFILE-BELL tags `awaiting_input` with `waiting_step` and applies the
reminder rule to it. If PROFILE-BELL lands first, it defines `waiting_step`
with no source yet, and HW-NOTIFY's brief says to tag its source with it.

### PROFILE-API: the store and the read (swarm-api)

* Files: `apps/swarm-api/swarm_api/preferences.py` (new),
  `apps/swarm-api/swarm_api/profile.py` (new),
  `apps/swarm-api/swarm_api/routes/profile.py` (new),
  `apps/swarm-api/swarm_api/main.py`,
  `tests/unit/control_plane/test_user_preferences.py` (new),
  `tests/unit/control_plane/test_profile_api.py` (new),
  `tests/unit/control_plane/test_profile_no_credentials.py` (new).
* Red first:
  * the id is not `user_hash(email)`, differs per purpose string, and is
    stable across reads;
  * `PUT` with a workspace outside `tenant_choices` is 422 and stores
    nothing;
  * a stale `revision` is 409;
  * an unknown topic or field is 422;
  * a `_special` caller is 403;
  * the credential sentinel test of §4.2;
  * a broker failure leaves `claude_accounts` out and names it in
    `unavailable`, and the rest is served;
  * a group pool account and another member's connection are never in the
    response.

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
  * an unreadable preferences document is 503 for a headerless request and
    not for a headed one;
  * `tenants/mine`'s first entry equals the headerless tenant in every case
    above;
  * a service identity never reads preferences.

### PROFILE-BELL: topics, muting and reminders

* Files: `apps/swarm-api/swarm_api/notifications.py`,
  `apps/swarm-api/swarm_api/routes/notifications.py`,
  `docs/notifications.md`,
  `tests/unit/control_plane/test_notification_preferences.py` (new).
* Red first:
  * every item carries a topic from `TOPICS`;
  * a failed workflow is `failed` and a succeeded one `finished`;
  * a muted topic leaves the list and the badge and is counted in `muted`;
  * `admin_pending` ignores muting;
  * unreadable preferences serve everything with `partial`;
  * with `every_4h`, an approval read 5 h ago is unread and one read 3 h ago
    is read;
  * an update never re-rings;
  * `mark_all_read` marks only shown items.

### PROFILE-PAGE: the screen (UI, 5 files)

* Files: `apps/swarm-ui/src/Profile.tsx` (new), `apps/swarm-ui/src/api.ts`,
  `apps/swarm-ui/src/types.ts`,
  `apps/swarm-ui/src/__tests__/profile.test.tsx` (new),
  `apps/swarm-ui/src/__tests__/profile.routes.test.ts` (new).
* Red first:
  * the four cards render from a fixture;
  * no rendered text contains a planted sentinel field;
  * each summary links to its owning page;
  * saving sends `revision`, and a 409 reloads;
  * an `unavailable` section renders "could not be read" rather than empty;
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
  * a switch survives a reload but not a new session (Q2);
  * "Back to default" clears the choice;
  * the panel foot links to Settings › Profile;
  * the bell shows "1 muted item needs you" when `muted.needs_you` is 1, and
    nothing when it is 0.

### PROFILE-SC: `/sc` and the CLIs say which default applied

* Files: `apps/swarm-mcp/swarm_mcp/sc.py`, `apps/swarm-mcp/swarm_mcp/client.py`,
  `plugin/.claude-plugin/plugin.json`, `tests/unit/mcp/test_sc_tenant.py`,
  `tests/unit/mcp/test_tenant_choice.py`,
  `tests/unit/mcp/test_plugin_user_config.py`.
* Red first:
  * with no `SWARM_TENANT`, `sc whoami` reports "your default, from Settings ›
    Profile" when `tenants/mine` says `default_source: profile`, and the
    personal-workspace wording otherwise;
  * `SWARM_TENANT` still wins and is reported as such;
  * `swarm_tenants` marks the default;
  * the plugin's `tenant` description names Settings › Profile.

---

## 7. Owner questions

Each is also in this lane's `questions.json`. The recommendation is listed
first.

**Q1. What keys the preferences document's id?**

* (a) **An HMAC under the existing `swarm-child-key`, with its own purpose
  string.** Recommended: no worker can compute it, it needs no new secret,
  Terraform or deploy step, and it follows `childkey.py`'s existing
  derivations. A rotation that drops every old version resets preferences to
  today's behaviour.
* (b) A dedicated `swarm-profile-key`. It is cleaner separation, but it adds
  a Terraform lane, a `create-secrets.sh` flag and an ordered first deploy.
* (c) `user_hash(email)`, like `notification_reads`. It is the simplest, but
  any tenant's agent can rewrite a person's default until design B removes
  worker Firestore access. That is a cross-tenant misfiling path.

**Q2. How long does a console tenant switch last?**

* (a) **For the tab's session. A new window opens in the default.**
  Recommended: "the console opens in my default" is then true for everyone.
* (b) Forever in that browser, as today, with saving a default in Profile
  clearing it. Less change, but someone who switched once opens there until
  they save again.
* (c) Forever, and the default applies only to people who never switched.

**Q3. What may be muted?**

* (a) **Every topic, with a quiet "N muted items need you" line under the
  bell.** Recommended: the person controls their bell, and a muted approval
  is quiet but never invisible.
* (b) Every topic, silently.
* (c) Only updates (`failed`, `finished`, the account updates). Needs-you
  items always ring.

**Q4. Whose reminder period wins for a step waiting for this person?**

* (a) **The person's, when they chose a cadence. The author's, when they left
  it at "as set".** Recommended: it is their bell, and the step still waits
  in the approvals inbox and on the workflow for everyone.
* (b) Always the author's. The person's cadence applies only to other items.
* (c) Whichever is more frequent.

**Q5. Where does the display name come from?**

* (a) **The token's `name` claim when present, otherwise none: the email is
  the identity.** Recommended: nothing stored and nothing anyone can set to
  look like someone else. IAP never carries a name, so this is often blank.
* (b) An editable display name stored in preferences. It is friendlier, but
  it is shown to others on People, where it can impersonate.
* (c) A Cloud Identity / Directory lookup. It is accurate, but it needs a
  directory scope this project does not hold today.

**Q6. "SwarmCloud API keys" do not exist. What does the page do?**

* (a) **State that SwarmCloud signs you in with Google, show `sc login`, and
  file a separate issue if keys are wanted.** Recommended: the page tells
  the truth, and personal API keys are a new authentication path that needs
  its own design and security review.
* (b) Build personal SwarmCloud API keys as part of #1505.
* (c) Omit the row.

**Q7. What is "my role" in a workspace?**

* (a) **Derived: owner of your personal workspace, member of a group (via the
  group's address), plus your platform role shown once.** Recommended:
  these are the only roles the platform enforces today.
* (b) Introduce per-workspace roles. That is a separate design, because it
  changes authentication and approvals.

**Q8. May an admin see or change another person's preferences?**

* (a) **No. Admin › People already shows what an admin needs.**
  Recommended: preferences are personal and change no capacity or access.
* (b) Admins may read them, but not write them.
* (c) Admins may read and write them.

**Q9. If the preferences cannot be read, what does a headerless request do?**

* (a) **Fail with 503, as a failed personal-workspace read already does.**
  Recommended: it is read in the same batch, so it adds no new failure, and
  it never runs work in a workspace the person did not choose.
* (b) Fall back to the computed default silently.

**Q10. Which workspaces does the GitHub summary cover?**

* (a) **Every workspace the person can act in, one row each.** Recommended:
  the connection is per workspace, and a failed token in a workspace the
  person is not in now is exactly what they cannot see today. It costs one
  batched read of N computed ids.
* (b) Only the acting workspace, as Work › Access shows.
