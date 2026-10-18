# Workspace roles: what a person may do in a workspace is decided by that workspace, not by "in the group" plus one platform bit

**Status: designed 2026-10-18 (lane ROLES-DESIGN, part of #1518).** Nothing
here is built. The owner answers §7 before any build lane starts.

The owner asked for this on 2026-10-18, answering the Settings › Profile
design (#1505, PR 1506, question Q7):

> Give each workspace real roles, so that what a person may do in a workspace
> (approve a step, answer a waiting step, manage its repositories and secrets,
> invite or remove people) is decided per workspace rather than by "member of
> the group" plus one platform-wide admin bit.

Every file and symbol cited below was read on 2026-10-18 at `2c0d5a7`. Lines
move, so each citation names the symbol; search for it.

What the design settles, in one line each:

* **Five roles on one ladder:** `viewer` < `submitter` < `approver` <
  `maintainer` < `admin`. Each role can do everything the role below it can
  (§4.1). Today's "member" is `maintainer`, exactly. `admin` adds what only a
  personal workspace's person or a platform admin can do today: secrets,
  workspace settings and people.
* **Membership still comes from Google, and a role only says what a member
  may do.** A role never admits anyone. Who is in a group workspace is still
  asked of Cloud Identity, one registered group at a time, with
  `x-goog-user-project`. `X-Swarm-Tenant`, `tenant_choices` and invariant 9
  are unchanged (§2.2).
* **A group workspace has one default role and per-person overrides.** A
  member with no override holds the workspace's default. The default starts
  at `maintainer`, so **on the day this ships everyone keeps exactly what
  they have.** An admin narrows people afterwards (§4.6).
* **A personal workspace's person is always its `admin`.** That is not
  stored, cannot be revoked, and nobody else can be given a role there
  (§4.4).
* **Roles are stored as signed, append-only grants with ids nobody can
  guess**, in `workspace_role_grants`. A tenant worker can create and update
  Firestore documents today (`tenant-settings-integrity.md` §1.1). It still
  cannot forge a grant (it has no key), cannot replay an old one (the newest
  valid grant wins, and a copy keeps its old time), and cannot find a real
  grant to overwrite (it cannot list, and ids are never served) (§4.3).
* **Who may grant:** the workspace's admins, and platform admins through
  Admin › People. A platform admin names the first admin of a group
  workspace (§4.5).
* **Every new gate on an existing route is a refusal switch that ships off**
  (`refusals.py`), with one named 403 code per action (§4.8). A failed role
  read refuses only once its switch is on; until then the request goes
  through. Switches off is the second half of "nobody loses access on day
  one".
* **Each grant, revocation and default change is an `admin_audit` event**
  (§4.9).
* **No frozen-contract change is needed** (§5). Only swarm-api reads a role.

---

## 1. The problem, as measured

### 1.1 Roles today are derived, never stored

`apps/swarm-api/swarm_api/auth.py::AuthContext` carries what a request knows
about its caller. Four fields decide what the caller may do:

| field | what makes it true | scope |
|---|---|---|
| `tenant_choices` / `tenant_id` | the caller's own ready personal workspace (`u-<slug>`), plus every registered group in `TENANT_GROUPS` that Cloud Identity confirms the caller is in (`Authenticator._tenant_choices`) | per workspace, but only in or out |
| `is_admin` | an `ADMIN_GROUPS` membership, an `ADMIN_USERS` entry, the platform owner, or an `admin_roles/{email}` document (`admins.py`) | the whole platform |
| `is_owner` | the caller is `PLATFORM_OWNER` | the whole platform |
| `is_pool_admin` | `ADMIN_POOL_USERS` | one route |

There is no field that says what this person may do in this workspace. A
route either needs membership (`deps.py::tenant_scope`, `current_auth`) or
`require_admin`, or it compares the caller with a personal tenant's
`principal` (`mergesettings.py::is_owner`, `codeintel.py::require_owner_or_admin`,
`workspace_secrets.py::require_manager`).

### 1.2 What a member can do, route by route

This lane listed every route mounted by `apps/swarm-api/swarm_api/main.py`
at `2c0d5a7`. There are 263 routes:

* 167 are tenant-member routes;
* 58 go through `admin_auth`;
* 36 are worker-only (an ID token plus an attempt proof);
* 2 are unauthenticated health checks.

Of the 167 member routes, the ones that change something, grouped by what they
do:

| class | routes | today's gate |
|---|---|---|
| submit | `POST /v1/tasks`, `/v1/tasks/batch`, `/v1/workflows`, `/v1/runs` | any member |
| cancel | `POST /v1/tasks/{id}/cancel`, `/v1/workflows/{id}/cancel` | any member, not only the submitter |
| decide | `POST /v1/approvals/{id}:approve`, `:reject`, `/v1/runs/{id}/plan:approve`, `plan:edit`, `plan:reject`, `/v1/schedules/{id}:merge-mode`, a `/swarmcloud approve` comment (`ghapprove.py`) | any member. Exceptions: the last editor of a gate, `second_member` holds, a `spec` item (platform admin or owner), and a `platform: true` repository |
| answer | `POST /v1/tasks/{id}/answers`, `/human-ready`, `/live/control*`, `/live/device`, `/live/open-to-members` | the submitter or a platform admin for answers. The submitter for human-ready. Live control is the owner, or any member once opened |
| schedules | 20 routes in `routes/schedules.py` | any member for create (per type), delete, pause, pause-all, take-ownership and run now. Platform admin for a `disabled` resume and for `admin`-type schedules (`routes/schedules.py::_may`) |
| repositories | register, delete, index, `PATCH` (`routes/repositories.py`), 9 routes in `routes/access.py` | any member ("Who may register: any member of the tenant", module docstring). Platform admin for `platform` and `merge_policy`. Owner or platform admin for `context` |
| subscription accounts | `POST /v1/accounts`, lending, state, delete (`routes/accounts.py`) | **any member**, with no admin gate |
| secrets | workspace secrets, MCP servers, tenant provider key, tenant and repository git-token slots | personal: its person. Group: **platform admin only** (`workspace_secrets.py::require_manager`). Exception: **`POST /v1/tenants/me/credentials` writes a tenant provider key for any member** (`routes/tenants.py::put_credential`) |
| workspace settings | merge settings, code intelligence, judgment backend | the owner or a platform admin. **Merge settings' refusal (`merge_settings_owner_required`) is a switch that is off**, so any member can change them today |
| people | Admin › People (`routes/people.py`), 10 routes | platform admin only. There is no per-workspace people surface |

So in a group workspace there are two levels and nothing between them:
everything a member can do, or a platform admin. The table also shows three
inconsistencies that a role model removes, rather than adds to:

* a tenant provider key is admin-only on one route and open to every member
  on another;
* subscription accounts, a shared spend, can be deleted by any member;
* merge settings are "owner or admin" in code and "anyone" in production.

### 1.3 Where the absence is felt

* **Approvals.** `approvals.py::check_gate_approver` lets any member approve
  by default. A gate that names `approvers` must list each email, per gate.
  Nobody can say "these three people approve in this workspace".
* **Human steps.** `docs/design/human-steps.md` §4.2 settles who may answer
  (owner's Q2): any member of the step's tenant, re-checked fresh, never a
  service account, and no admin bypass. §4.2 also records what Q2 gave up: "A
  step can no longer say 'only Alice may approve this'." A role brings back
  the workspace-wide half of that without bringing back per-step assignees.
* **Secrets.** The workspace-secrets owner decision (§10, Q4: "platform admins
  only ... until a per-tenant admin role exists") leaves a group workspace
  unable to manage its own secrets without a platform admin.
* **Settings › Profile.** PROFILE-DESIGN-B (in flight) shows a role derived
  from the fields in §1.1, behind one seam. With no stored role, the page can
  show only "owner", "member" or "platform admin".

### 1.4 What "done" lets someone do that they cannot today

These are the issue's acceptance tests, made concrete:

* A workspace admin gives `carol@` the `viewer` role in `eng`. Carol still
  sees `eng` in her workspace switcher and can read its runs, logs and
  approvals. When she submits, approves or answers there, she gets a 403
  whose code names the action (`ROLE_SUBMIT_REQUIRED`,
  `ROLE_APPROVE_REQUIRED`, `ROLE_ANSWER_REQUIRED`).
* `dave@` holds `approver` and approves a held run. `erin@`, a `submitter`,
  cannot.
* Each grant and revocation appears in Admin › People › Recent admin actions
  and on the workspace's own Roles card, with who did it, from what, to what,
  and when.
* Settings › Profile shows "eng: approver (granted by alice@, 2026-10-20)",
  not "member".
* A group workspace manages its own secrets once a platform admin names its
  first admin, with no platform admin needed after that.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **`searchTransitiveGroups` returns 403 in this project.** The platform never
  lists a caller's groups. It asks "is this caller in THAT group?" with
  `checkTransitiveMembership`, one registered group at a time, and every call
  sends `x-goog-user-project` (`groups.py`, module docstring;
  `groups.py::CloudIdentityGroups.is_member`). Any design that needs "which
  groups is this person in?" is impossible here. A design that adds groups
  adds one lookup per group per caller per 120 s cache window.
* **A group workspace is exactly one Google group.** `TENANT_GROUPS`
  (`settings.py`, `ApiSettings.tenant_groups`) is a deploy-time list, and
  the tenant id is the slug of the group's local part (frozen
  `identity.tenant_id_for_group`). Nobody registers a group in the console.
  So "a group workspace's registered groups" is one group, and "a per-group
  default role" is one value per workspace.
* **A tenant worker can get, create and update any Firestore document whose
  id it can name.** It cannot list or delete (`swarmTenantWorkerFirestore`,
  `terraform/bootstrap/platform_roles.tf`;
  `docs/design/tenant-settings-integrity.md` §1.1). That design's B removes
  the binding, but it is not built. A role store that is safe only once B
  lands would be unsafe for as long as B is unbuilt.
* **A new 4xx refusal ships report-only** (`refusals.py`, module docstring;
  `tests/unit/control_plane/test_new_refusals_ship_off.py`). Every new
  refusal on an existing route is a `Switch` that defaults off.
* **The platform is_admin bit is about the caller, not the workspace.**
  `_select_tenant` changes neither it nor `principal.groups`. Approvals and
  human-step answers deliberately give a platform admin no bypass
  (`routes/approvals.py`, module docstring; `human-steps.md` §4.2 step 3).

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

* **1. Only LEASED/DISPATCHED/STARTING/RUNNING cost anything.** A role is
  checked in swarm-api before a task is written. A refused submission creates
  no task, so it never reaches QUEUED, let alone a lease. Nothing waits on a
  role. A role change does not move any task's state: work already submitted
  keeps running under the same tenant. **Kept.**
* **2. All-or-nothing reservation.** Admission (`swarm_common/admission.py`)
  is not touched. Roles are read before `SubmissionService` and never inside
  the reservation transaction. **Kept, untouched.**
* **3. Concurrency counts from LEASED.** Untouched.
* **4. Workers never sleep through a provider wait.** Untouched. A human step
  stays `PARKED(AWAITING_INPUT)` while it waits for an approver. A role decides
  who may answer, not how the step waits.
* **5. Fencing.** Worker routes (attempt proof plus generation) are not
  role-checked. They authenticate a worker, not a person. A stale worker still
  exits without running the agent. **Kept, untouched.**
* **6, 7.** Untouched.
* **8. Checkpoints.** Untouched. A refused cancel leaves a running attempt
  checkpointing as before.
* **9. Per-tenant isolation.** This is the invariant the design rests on, in
  three ways:
  * **A role never admits.** A grant to someone outside the group gives them
    nothing until Cloud Identity says they are in it. `tenant_choices` is
    still built only from verified memberships, so a role can't add a
    workspace to a person's list.
  * **A grant is bound to its tenant by its signature** (§4.3), so a worker
    in tenant A cannot copy one of A's grants into B.
  * **Every role read is scoped by the tenant `_select_tenant` chose.**
    Nothing a caller sends picks the tenant whose roles are read.
  **Kept, and strengthened inside a tenant.**
* **10. Callers pick a `runner_profile` by name.** No role route accepts an
  image, a command, resources or backend parameters. A role restricts which
  routes a person may call. It never widens what a body may carry. **Kept.**

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing (§5). The role ladder, the grant shape and the signature are read and
written by swarm-api only. The scheduler, the reconciler and the worker never
decide what a person may do. The signature reuses the settings key that
swarm-api already holds (`ApiSettings.settings_key`), under its own domain
string, so it needs nothing from `specsign.py`.

---

## 3. Designs that could work

### 3.1 Design A: a default role per workspace, per-person overrides, signed append-only grants (recommended)

Each group workspace has one stored default role, and its admins can set any
member to another role. Grants are Firestore documents, one per change, with
random ids. Each carries an HMAC that binds the tenant, the person, the role
and the time. The newest valid grant for a (tenant, person) pair is that
person's role.

* **Code:** one new module (`roles.py`) and one new route file. Each gated
  route gets one call to `roles.require(auth, capability)`. The UI and plugin
  get a role field.
* **Migration:** none written. An empty grant set means "the default", and
  the default with no default grant is `maintainer`, which is today's member.
* **Latency:**
  * One Firestore query per (tenant, person) per 15 s per instance, about
    5-10 ms on a miss.
  * Decisions (approve, answer, grant) read fresh.
  * No extra Cloud Identity call: membership is the lookup that already
    happens.
* **Money:** reads at Firestore list prices, about $0.06 per 100k. At the
  platform's request rate this costs well under a dollar a month.
* **What it breaks:** nothing on the day it ships. Once a switch is on, a
  narrowed person is refused, which is the point of the design.

### 3.2 Design B: role groups, where each role is a Google group

The workspace's admins bind more Google groups to roles, for example
`eng-approvers@saga.xyz → approver`. A person's role is the highest role among
the bound groups Cloud Identity confirms they are in. Nothing per-person is
stored.

* **Code:** about the same as A, plus binding routes and a lookup per bound
  group.
* **Latency:**
  * One more `checkTransitiveMembership` per bound group per caller per
    120 s. The 403 on `searchTransitiveGroups` means the groups cannot be
    fetched in one call.
  * A workspace with four bound groups makes five directory calls on a
    cold cache.
* **Operations:** granting a role means editing a Google group. Admins of a
  workspace usually cannot do that, so "invite or remove people" leaves the
  console for Google Admin. A revocation takes up to the 120 s cache to
  apply, or longer if the directory is slow to propagate.
* **What it breaks:** the issue's "granting and revoking a role appears in
  the audit log". The change happens in Google Workspace, so the platform
  never sees it happen.
* **Where it fits:** organisations that already run access in groups.
  Design A does not preclude it. A later lane can add bound groups as a third
  source, between the default and a person's override (Q4).

### 3.3 Design C: a capability set per person

Instead of a ladder, each person holds a set of capabilities, such as
`{submit, approve, repositories}`.

* **Code:** the same storage as A. More UI: a matrix of checkboxes for each
  person.
* **What it costs:** every refusal message and every screen has to explain a
  combination, not a name. An audit row like "added `secrets` to bob" is
  harder to review than "bob: maintainer → admin". Two admins can build sets
  that nobody can describe in a word, which defeats the issue's "Settings ›
  Profile shows the stored role".
* **What it buys:** separation of duty, for example someone who approves but
  cannot submit. The ladder cannot express that. Q1 asks whether it is needed.

### 3.4 Recommendation: A

A is the only design in which a workspace's own admins grant and revoke from
the console with each change audited, and it adds no Cloud Identity calls.
B is a good later source for groups-first organisations, and A leaves room for
it (Q4). C's flexibility is not what the issue asks for, and it costs
legibility on every screen.

---

## 4. The recommended design, in enough detail to build

### 4.1 The role set and what each permits

There are five roles on one ladder. `rank(viewer)=0 … rank(admin)=4`, and a
role permits everything the roles below it permit. `roles.py` holds the
capability table, and nothing else restates it. The UI and the plugin read it
from `GET /v1/workspace/roles` (§4.2), so the explanation a screen shows is the
server's own.

| capability | lowest role | routes (all under `current_auth` + `tenant_scope` today) |
|---|---|---|
| `read` | `viewer` | every GET of tasks, checkpoints, attempts, leases, outcomes, workflows, runs, issues preview, repositories (and their code-intelligence reads), schedules (and firings, audit), approvals, questions, live session/recordings/audit, notifications, git-token slots, secrets (names and grants), MCP servers, browser logins, stranded PRs; `POST /v1/live/ticket` (a viewer ticket); `POST /v1/notifications/read*`; `POST /v1/onboarding/dismiss`; `GET /v1/workspace/roles` |
| `submit` | `submitter` | `POST /v1/tasks`, `/v1/tasks/batch`, `/v1/workflows`, `/v1/runs`; on **own** work: `POST /v1/tasks/{id}/cancel`, `/v1/workflows/{id}/cancel`, `/v1/tasks/{id}/human-ready`, `/v1/tasks/{id}/answers` (still submitter-or-platform-admin, as today), `/v1/tasks/{id}/browser-login`, `/live/control*` and `/live/device` on own task |
| `approve` | `approver` | `POST /v1/approvals/{id}:approve`, `:reject`; `/v1/runs/{id}/plan:approve`, `plan:edit`, `plan:reject`; a `/swarmcloud approve` comment (`ghapprove.py`); the human-step answer `POST /v1/workflows/{wf}/steps/{step}/input` (`human-steps.md` §4.2, not built yet); cancel **anyone's** task or workflow; `/live/control*` on a task opened to members |
| `maintain` | `maintainer` | every schedule write in `routes/schedules.py` (create, PATCH, DELETE, pause, pause-all, resume, take-ownership, run) and `POST /v1/schedules/{id}:merge-mode`; repositories register, PATCH (fields other than `platform`, `merge_policy`, `context`), DELETE, index, index:run; every write in `routes/access.py`; every write in `routes/accounts.py`; `POST`/`DELETE /v1/live/open-to-members`; `DELETE /v1/browser-logins/{name}` |
| `administer` | `admin` | workspace secrets writes (`workspace_secrets.py::require_manager`); MCP server writes (`mcp_servers.py`, `require_manager`); `PUT`/`DELETE /v1/provider-keys/{p}/tenant`, tenant-scoped `POST /v1/provider-keys/{p}/links`, `POST /v1/tenants/me/credentials`; tenant and repository git-token slots (`gittokens.py::_require_admin`); `PUT` merge settings, code intelligence, judgment backend; repository `context`; `PUT`/`DELETE /v1/workspace/roles/*` |

The rows above `approve` are in that order on purpose. Approving is judging
someone else's work. A person trusted to judge work is trusted to stop it, but
not necessarily to change what the workspace automates (schedules) or what it
can reach (repositories, accounts).

**What no role reaches.** These stay platform-only, gated exactly as today
(`require_admin`, `is_owner`):

* the `/v1/admin/*` routes;
* a repository's `platform` and `merge_policy`;
* approving a `spec` (custom-prompt) item;
* the merge-mode switch on a `platform: true` repository;
* `admin`-type and `owner`-type schedules (`routes/schedules.py::_may`);
* resuming a `disabled` schedule;
* `scope=platform` reads;
* naming another tenant on `GET /v1/stranded-prs`.

A workspace admin administers one workspace and never the platform.

**What every role keeps, because it is the person's own and not the
workspace's:**

* the caller's GitHub connection (`/v1/onboarding/github/*`);
* their user git-token slot and user provider key;
* their personal workspace request (`/v1/workspace`).

**Platform admins** (Q5, recommendation (a)):

* **`is_admin` never satisfies a role check.** A platform admin who is a
  `viewer` in `eng` is refused a submit there, exactly as anyone else.
* **A role check never removes a platform-admin path that exists today.** On
  the five `administer` gates that accept a platform admin today
  (`require_manager` in both modules, provider keys, git tokens, the owner or
  admin settings checks), the gate becomes "workspace `admin` **or** platform
  admin". A platform admin keeps every power they hold on the day this ships.
  `agent_questions.py::answer_refusal` keeps its `is_admin` branch too.
* **A platform admin who wants a role in a workspace grants it to themselves**
  through Admin › People. The grant is audited `via: "platform_admin"`, so a
  self-grant is visible rather than silent.

### 4.2 Routes

There are two new route groups, both in `apps/swarm-api/swarm_api/routes/roles.py`
(new).

**A workspace's own, under the tenant `X-Swarm-Tenant` selects:**

```
GET    /v1/workspace/roles
PUT    /v1/workspace/roles/person      {email, role, reason?}
DELETE /v1/workspace/roles/person      {email, reason?}          -> back to the default
PUT    /v1/workspace/roles/default     {role, reason?}
```

The email is in the body, not the path, so that addresses do not land in
request logs and load-balancer access logs. People routes already avoid
addressing a person by email (`routes/people.py`, module docstring).

`GET` response, readable by every role (Q7):

```json
{
  "tenant_id": "eng",
  "kind": "group",
  "default_role": "maintainer",
  "default_source": "migration",         // "migration" until an admin sets it, then "grant"
  "me": {"email": "carol@saga.xyz", "role": "viewer", "source": "override",
         "granted_by": "alice@saga.xyz", "granted_at": "2026-10-20T09:14:03.120Z",
         "capabilities": ["read"]},
  "people": [
    {"email": "alice@saga.xyz", "role": "admin", "source": "override",
     "granted_by": "ops@saga.xyz", "granted_at": "…", "via": "platform_admin"},
    {"email": "carol@saga.xyz", "role": "viewer", "source": "override", "…": "…"}
  ],
  "roles": [
    {"name": "viewer", "capabilities": ["read"], "summary": "Sees everything; changes nothing."},
    {"name": "submitter", "capabilities": ["read", "submit"], "summary": "…"},
    {"name": "approver", "capabilities": ["read", "submit", "approve"], "summary": "…"},
    {"name": "maintainer", "capabilities": ["read", "submit", "approve", "maintain"], "summary": "…"},
    {"name": "admin", "capabilities": ["read", "submit", "approve", "maintain", "administer"], "summary": "…"}
  ],
  "can_manage": false,
  "audit": [ {"action": "workspace_role_granted", "by": "…", "at": "…", "target_email": "…",
              "detail": {"from": "maintainer", "to": "viewer", "via": "console"}} ]
}
```

* `people` lists only overrides. Everyone else holds the default. Listing all
  members would mean enumerating a Google group, and the platform cannot do
  that (`searchTransitiveGroups` returns 403).
* `audit` holds the last 50 role events for this tenant.
* A personal workspace answers with `kind: "user"`, `default_role: null`,
  `people: []`, and `me.role: "admin"` with `source: "personal"`.

`PUT /person` returns `{email, role, previous_role, source: "override",
granted_at}`. Every write route answers 409 `role_unchanged` when nothing
would change, and writes nothing.

**A platform admin's, for any workspace (`admin_auth`):**

```
GET    /v1/admin/workspaces/{tenant_id}/roles
PUT    /v1/admin/workspaces/{tenant_id}/roles/person    {email, role, reason?}
DELETE /v1/admin/workspaces/{tenant_id}/roles/person    {email, reason?}
PUT    /v1/admin/workspaces/{tenant_id}/roles/default   {role, reason?}
GET    /v1/admin/roles/person?email=…   -> every override this email holds, across workspaces
```

The `{tenant_id}` must be a registered group tenant. The writes are the same
functions as the workspace routes, called with `via: "platform_admin"`.

**Role fields on routes that exist** (`routes/tenants.py`):

* `GET /v1/tenants/me` adds `role: {name, source, capabilities}` for the
  selected tenant.
* `GET /v1/tenants/mine` adds the same `role` to each `TenantChoice`.

That costs one cached query per listed workspace, and a person is in a
handful. This field is the seam Settings › Profile reads (§4.10).

### 4.3 Storage: signed, append-only grants with unguessable ids

**Collection `workspace_role_grants`**, document id = Firestore auto-id
(20 random characters), never returned by any route:

```
workspace_role_grants/{auto-id}
  tenant_id:   "eng"
  subject:     "carol@saga.xyz"   // lower-cased; "*" for the workspace default
  role:        "viewer"           // one of the five; null = "back to the default" (subject != "*")
  granted_by:  "alice@saga.xyz"
  granted_at:  "2026-10-20T09:14:03.120Z"   // swarm-api's clock, written as a string, covered by the MAC
  via:         "console" | "cli" | "platform_admin"
  reason:      "moved to the review rota"   // <= 200 chars, redacted (redaction.py) before it is written
  mac:         {v: 1, key: "<key_id>", sig: "<base64 HMAC-SHA256>"}
```

* **The signature.** It is `HMAC-SHA256(settings_key, "swarm-workspace-role-grant/v1\n" +
  canonical_json({tenant_id, subject, role, granted_by, granted_at}))`.
  * The key is the one swarm-api already holds for `workspace_settings`
    (`ApiSettings.settings_key`, the `swarm-settings-key` secret). A
    separate domain string means a settings block's MAC can never verify as
    a grant, and a grant's MAC never as a settings block.
  * `key_id` and `settings_key_previous` allow rotation exactly as
    `judgment.py::settings_keys` does.
  * `via` and `reason` are not covered. They are shown, never acted on.
* **The read.** Query `tenant_id == T AND subject in (E, "*") ORDER BY
  granted_at DESC`, in pages of 20. Take the first grant **that verifies** for
  E, then the first for "*".
  * An invalid document is skipped, logged
    (`role grant invalid tenant=… reason=mac`) and counted in
    `swarm_api_role_grant_invalid_total`.
  * After three pages with no valid grant for E, the read gives up. That is
    `role_unavailable`, not a guess (see "What a failed read does" below).
  * A newest valid E grant with `role: null` means "the default".
  * With no valid "*" grant, the default is `maintainer` (§4.6).
* **What a worker can and cannot do to it.** The worker's role is get,
  create and update, without list (§2.1).
  * *Forge a grant:* it can create a document, but cannot sign one. The
    reader skips it.
  * *Replay an old grant* (one it read earlier, by an id it should never
    have had): a copy keeps the old `granted_at` under the MAC, so the newer
    grant still wins. Changing `granted_at` breaks the MAC.
  * *Overwrite the current grant:* it needs the id, which is random, is
    never served by a route, is never written to `admin_audit`, and cannot
    be listed. An overwrite it somehow managed would fail the MAC and be
    skipped. The next-older valid grant would then apply, so the worst case
    is the person's previous role. That residual needs an id that nothing
    serves, and it closes entirely when ISO-IAM removes the binding
    (`tenant-settings-integrity.md` §6).
  * *Copy into another tenant:* `tenant_id` is under the MAC.
  * *Flood:* thousands of unsigned grants with a future `granted_at` push
    the valid one past three pages. The person's requests then meet
    `role_unavailable`. That is denial of service, never escalation, and it
    raises an alert on the counter.
* **Why not one document per (tenant, person), or one roster per tenant?**
  Those ids can be guessed (`eng__carol@…`, `eng`). A worker can then read
  the signed document today and write it back after an admin narrows the
  person. That is a replay that restores a revoked `admin`, and a signature
  cannot detect it. Append-only grants with ordering under the MAC are the
  shape where the worker's get/create/update cannot undo a revocation.
* **Index.** A composite index on (`tenant_id` ASC, `subject` ASC,
  `granted_at` DESC) in `terraform/modules/firestore/indexes.tf`. For the
  workspace audit card, an index on `admin_audit` (`target_tenant_id` ASC,
  `at` DESC).
* **Cache.** Per instance, per (tenant, subject), for 15 s, the same as
  `admins.py`'s role cache. **The decision routes always read fresh:**
  approve, reject, plan approve, human-step answer, cancel of someone
  else's work, and every role write. A narrowed approver therefore cannot
  approve during the cache window. For a submit, up to 15 s of stale role is
  the trade for one query per request.
* **What a failed read does.** It does not 503 every request. The failure
  is "unknown", and a gate holding an unknown role behaves like its switch:
  * off: log and let the request through, as today;
  * on: 503 `role_unavailable` (5xx, so it is not a refusal code and needs
    no switch).
  Turning a switch on therefore never adds a Firestore dependency to a route
  that did not have one before the owner chose to turn it on.

### 4.4 Personal workspaces

The person whose verified email the tenant document names
(`PersonalTenants.ready`, `_with_own_workspace`) is the workspace's `admin`,
with `source: "personal"`. Nothing is stored. Every role write against a `u-`
tenant answers 409 `personal_workspace_roles_fixed`, and nobody else can hold
a role there. Nobody else is a member: `_with_own_workspace` only ever adds
the caller's own `u-` id, and a role never admits (Q3). So a role there would
have nobody to apply to.

### 4.5 Who may grant and revoke

| action | who | refused with |
|---|---|---|
| set or clear a person's role in a group workspace | that workspace's `admin`s; platform admins through `/v1/admin/workspaces/{t}/roles` | `ROLE_ADMINISTER_REQUIRED` (switched) |
| set a group workspace's default | the same | the same |
| name the **first** admin of a group workspace | platform admins only. A workspace has no admin until one is granted, and the default can never be `admin` (409 `default_role_not_allowed`) | |
| raise oneself | nobody. A workspace admin is already at the top. A platform admin may, audited as `via: "platform_admin"` | `ROLE_SELF_GRANT` (not switchable, new route only) |
| demote or remove oneself | allowed. The last workspace admin may step down, because platform admins can always name another. That is why there is no `LAST_WORKSPACE_ADMIN` refusal (see `admins.py`'s `LAST_ADMIN` for the platform-wide case, where nobody is above) | |
| any role change in a personal workspace | nobody | `personal_workspace_roles_fixed` |

A grant names an email. The person does not need to be a confirmed member at
grant time. A grant to someone not in the group is stored and applies when
they join. The response says `member: false` when the one per-group check
(`Authenticator.is_tenant_member`, fresh) says they are not in it yet. This
lets an admin pre-assign `viewer` to someone joining the group tomorrow, so
they never hold `maintainer` for a day.

The email must be a person. A service account is refused with
`ROLE_PERSON_REQUIRED` (not switchable, new route only), using
`gitidentity.py::is_service_account`. A listed service-account member
(`AuthContext.member_scope`, contract request 30) already has its own narrower
scope, and a role must not widen it.

### 4.6 Migration: everyone keeps what they have until an admin narrows it

Nothing is written on the day this ships.

1. **The default with no default grant is `maintainer`.** `maintainer` is
   defined as exactly what a member can do today (§4.1 rows 1-4). Every
   member of every group workspace therefore holds today's powers, with
   `default_source: "migration"`.
2. **Every personal workspace's person is `admin`.** That is today's "owner"
   (secrets, merge settings, code intelligence, judgment backend).
3. **Platform admins keep every platform path** (§4.1, "Platform admins").
4. **Every new gate on an existing route ships as a switch that is off**
   (§4.8). Even an admin's narrowing is report-only until the owner turns
   that action's switch on. The log line `refusal report-only
   code=ROLE_SUBMIT_REQUIRED tenant=… email=…` shows exactly who would be
   refused, before anyone is.
5. **The one widening is opt-in.** A workspace admin may manage a group
   workspace's secrets (Q6), and only a platform admin can create a
   workspace admin.

So nobody loses access unless two separate decisions are made: the owner turns
a switch on, and an admin narrows that person. Rollout (Q10): turn switches
on in dev; read a week of report-only lines; turn them on in prod through
`terraform/environments/prod/prod.tfvars` `api_refusals`.

### 4.7 What each surface does with a role

* **Approvals inbox** (`Approvals.tsx`; `routes/approvals.py`):
  * The list stays readable by everyone (`read`).
  * `:approve` and `:reject` first check `approve`, then today's rules
    unchanged: a gate's `approvers` list, `second_member` holds, the
    last-editor rule and the `spec` admin rule.
  * A gate's `approvers` list now narrows within approvers. A listed email
    that holds `submitter` is refused `ROLE_APPROVE_REQUIRED`, and the
    response's `detail.role` says so. The workspace role is the floor, and
    the gate's list is the narrower rule on top.
  * The screen disables both buttons when `me.capabilities` lacks `approve`,
    showing "Your role in eng is viewer; approvers and above decide". The
    server still decides.
  * `ghapprove.py` maps the GitHub login to the member as today, then checks
    `approve` for that member. A comment approval from a narrowed member
    gets a reply naming the code.
* **Human-wait answers** (`human-steps.md` §4.2, not built): the route's
  checks become: a person (`person_required`); a fresh member
  (`not_a_member`); **then `approve`**, read fresh (`ROLE_ANSWER_REQUIRED`).
  * The code is distinct from `ROLE_APPROVE_REQUIRED` so that the switches
    can be turned on separately.
  * Any member may still answer, as the owner decided in Q2. A workspace
    can now narrow who that is, by role, without per-step assignees coming
    back.
  * If HW-API-ANSWER lands first, the ROLES-GATES-DECIDE lane adds the call.
    If it lands after, HW-API-ANSWER adds it. Whichever is second carries
    the test.
* **Agent questions** (`POST /v1/tasks/{id}/answers`): today's rule stays
  (the submitter or a platform admin), with `submit` required on top. A
  submitter narrowed to `viewer` can no longer steer their own running task's
  agent. `ROLE_ANSWER_REQUIRED`; `may_answer_because` names the role.
* **Schedules** (`routes/schedules.py`, `schedulefire.py`):
  * Writes need `maintain` (`ROLE_MAINTAIN_REQUIRED`).
  * **A firing submits as the stored owner and needs that owner to hold
    `submit` now.** `schedulefire.py::member_auth`, the shape
    `schedule_owner_auth` and `routes/runs.py::run_owner_auth` share, already
    asks whether the owner is a member at each firing. It now also asks for
    their role.
  * If the owner was narrowed below `submitter`: when the switch is on, the
    firing is skipped and the schedule auto-paused with
    `OWNER_ROLE_NARROWED: "Paused: its owner's role in this workspace no
    longer allows submitting; take ownership to resume"`. That mirrors
    today's `OWNER_NOT_MEMBER` and is written to `schedule_audit`. When the
    switch is off, the firing happens and the would-be refusal is logged.
  * Issue runs' auto approvals and CI-fix rounds (`run_owner_auth`) get the
    same check, with the run failing as `RunOwnerNotMember` does.
* **Repositories, access, accounts**: `maintain` (`ROLE_MAINTAIN_REQUIRED`).
  Platform-only fields stay platform-only.
* **Secrets, MCP servers, provider and git-token tenant slots, workspace
  settings**: `administer` or a platform admin (`ROLE_ADMINISTER_REQUIRED`).
  * `workspace_secrets.py::require_manager` becomes: personal, its person;
    group, a workspace `admin` or a platform admin. `can_manage` follows.
  * `POST /v1/tenants/me/credentials` gains the gate it lacks. It shares
    `ROLE_ADMINISTER_REQUIRED`'s switch with the other `administer` gates,
    so they are turned on together.
  * Merge settings' `merge_settings_owner_required` keeps its own switch.
    The owner check becomes "workspace admin (owner, for a personal
    workspace) or platform admin".
* **Admin › People** (`People.tsx`; `routes/people.py`):
  * Gets a **Workspace roles** card (`WorkspaceRolesAdmin.tsx`, new). It
    lists each registered group workspace with its default, its admins and
    the number of overrides, plus "Name an admin", "Set default" and the
    per-person list.
  * The Manage drawer for a person adds "Roles in workspaces", read from
    `GET /v1/admin/roles/person`.
  * "Recent admin actions" shows the role events (they are `admin_audit`
    rows already).
* **A workspace's own people page**, `#settings/people` (new
  `WorkspacePeople.tsx`):
  * Every member sees the default, the overrides, what each role permits
    (from `roles`) and the last 50 role events.
  * Admins see the controls: set a person's role (email and role), return
    a person to the default, and set the default (never to `admin`).
  * A personal workspace shows "You are this workspace's admin; a personal
    workspace has only you".
  * This is a Settings route, not a `SECTIONS` tab. The issue forms' "Where"
    list (`tests/unit/scripts/test_issue_forms.py`) is unchanged.
* **The sc plugin** (`apps/swarm-mcp/swarm_mcp/`):
  * `swarm_tenants` and `sc whoami` show each workspace's role.
  * `sc roles [list|set|clear|default]` calls §4.2 and requires a typed
    confirmation of the email and role. **No MCP tool grants or revokes
    a role** (Q11): an agent driving a person's own plugin session must not
    be able to raise anyone, and the elicitation route
    (`server.py::_elicit`) is for answers, not administration.
  * A refusal's code and message are printed as the server sent them.

### 4.8 Refusal codes, and which are switches

Each is a `Forbidden` subclass in `errors.py` with its own code. The ones on
existing routes are raised through `refusals.refuse` and registered in
`refusals.SWITCHES`, default off. The ones raised only by the new routes are
listed in `NOT_SWITCHABLE_CODES` with the reason that routes in #868 and
LB-L10 gave: no existing caller can be refused by them, and continuing past one
is the breach itself.

| code | status | raised by | switch? | the Switch's `why` |
|---|---|---|---|---|
| `ROLE_SUBMIT_REQUIRED` | 403 | submit routes; cancel, human-ready, answers, browser-login, live control on own work; a schedule firing or run auto approval whose owner lacks `submit` | **yes**, off | on once `GET /v1/tenants/me` carries `role` and the console disables Submit for a viewer, and the report-only log names only people an admin narrowed |
| `ROLE_APPROVE_REQUIRED` | 403 | approvals approve/reject, plan approve/edit/reject, `ghapprove` comments, cancel of others' work | **yes**, off | on once the inbox disables decisions for a role below approver, and the log names only narrowed people |
| `ROLE_ANSWER_REQUIRED` | 403 | the human-step answer route; `POST /v1/tasks/{id}/answers` | **yes**, off | on once AwaitInputCard and QuestionsCard show the role reason |
| `ROLE_MAINTAIN_REQUIRED` | 403 | schedules, repositories, access, accounts, live open-to-members, browser-login delete | **yes**, off | on once Schedules, Repositories and Accounts disable their writes for a role below maintainer |
| `ROLE_ADMINISTER_REQUIRED` | 403 | secrets, MCP servers, tenant provider key, `POST /v1/tenants/me/credentials`, git-token tenant/repository slots, workspace settings, repository `context`, every role write | **yes**, off **on existing routes**. On the role write routes it is raised directly, because they are new and letting a non-admin write a role is the breach | on once the log shows no member relying on `POST /v1/tenants/me/credentials` |
| `ROLE_SELF_GRANT` | 403 | role writes | no: new route only | |
| `ROLE_PERSON_REQUIRED` | 403 | role writes naming a service account | no: new route only | |
| `personal_workspace_roles_fixed` | 409 | role writes on a `u-` tenant | no: new route only | |
| `default_role_not_allowed` | 409 | `PUT …/default` with `admin` | no: new route only | |
| `role_unchanged` | 409 | a write that changes nothing | no: new route only | |
| `unknown_role` | 422 | a role not in the five | no: new route only (body validation) | |
| `role_unavailable` | 503 | a role read that failed, at a gate whose switch is on | not a 4xx | |

`ROLE_ADMINISTER_REQUIRED` is one code raised two ways. The guard
(`test_new_refusals_ship_off.py`) keys on the code, so it is registered as a
switch. The role routes raise it with `raise`, not `refuse`, and a test pins
that (§6). The alternative is a second code, `ROLE_GRANT_REQUIRED`, that is
not switchable. ROLES-API-CORE may choose it if the guard's AST collector
cannot tell the two call sites apart. The behaviour is the same either way.

Every 403 body is the shape `errors.py::to_payload` already produces:
`{"code", "message", "detail": {"tenant_id", "role", "required", "source"}}`.
For example, the message "Your role in eng is viewer; submitting needs
submitter or above. Ask an admin of eng (alice@saga.xyz) to change it.
Nothing was submitted." `detail` names the workspace's admins (up to three),
so the person knows whom to ask. That is not a leak: every member can already
read the roster (Q7).

### 4.9 Audit events

The events are written to `admin_audit` in the same transaction as the grant,
in the collection's existing `snake_case` past-tense convention
(`workspace_secret_created`, `provider_key_set`):

| action | when | `detail` |
|---|---|---|
| `workspace_role_granted` | a person's override is set | `{from, to, from_source, via, reason, member}` |
| `workspace_role_cleared` | a person returns to the default | `{from, to: <default>, via, reason}` |
| `workspace_default_role_set` | the default changes | `{from, to, via, reason}` |
| `workspace_role_grant_invalid` | the reader skipped an invalid grant (once per document id per instance, so a flood writes one row per document it reads, at most 60 per read) | `{tenant_id, subject, reason: "mac" \| "shape"}`, **no document id** |

Each row carries `target_tenant_id` and `target_email` (absent for the
default), `by` (the verified caller) and `at`. Refusals are not audited.
They are log lines, as every switch's report-only line already is, and
`swarm_api_role_refusals_total{code,enforced}` counts them.

### 4.10 Settings › Profile

PROFILE-DESIGN-B shows the derived role behind one seam. This design's build
replaces what is behind the seam, not the seam itself.

* The seam reads `TenantChoice.role` from `GET /v1/tenants/mine` when the
  field is present.
* It falls back to the derived role when the field is absent (an older
  API).
* The ROLES-UI-GATES lane makes that one change in whichever file the seam
  has landed in, and shows `role`, `source` and `granted_by`/`granted_at`.

Until this builds, Profile shows "member". Afterwards it shows "maintainer
(workspace default)" for everyone, until an admin changes something.

### 4.11 Changes by component, and the files each lands in

* **swarm-api**
  * `roles.py` (new): the ladder, the capability table, `Resolver` (query,
    verify, cache, fresh), `sign_grant`/`grant_verifies`, `write_grant`
    (transaction with its audit row), `require(auth, capability, *,
    fresh=False, own=False)`, `role_payload`.
  * `routes/roles.py` (new): §4.2.
  * `deps.py`: `workspace_role` dependency.
  * `main.py`: mount.
  * `errors.py`: the codes.
  * `refusals.py`: five switches.
  * `settings.py`: `role_cache_seconds` (15, beside the value with why).
  * `metrics.py`: two counters.
  * `routes/tenants.py`: `role` on `/me` and `/mine`, and the gate on
    `/me/credentials`.
  * Then one gate call per route in the files of §6's gate lanes.
* **scheduler, reconciler, agent-worker:** none.
* **terraform:** `terraform/modules/firestore/indexes.tf` (two indexes).
  `terraform/environments/{dev,prod}/*.tfvars` `api_refusals` when switches
  are turned on, which is an operator change, not a build lane.
* **swarm-ui:**
  * `WorkspacePeople.tsx` and `WorkspaceRolesAdmin.tsx` (new);
  * `roles.ts` (new: capability helpers, `can(me, 'approve')`);
  * `api.ts` and `types.ts`;
  * `App.tsx` (the `#settings/people` route);
  * `People.tsx` (one card mount and the drawer section);
  * then `Approvals.tsx`, `QuestionsCard.tsx`, `Submit.tsx`,
    `Schedules.tsx`, `ScheduleDetail.tsx`, `Repositories.tsx` and the
    Profile seam.
* **swarm-mcp / plugin:** `server.py` (`swarm_tenants`), `sc.py` (`whoami`,
  `roles`), `client.py`, `plugin/skills/sc/SKILL.md`, `plugin/commands/sc.md`.
* **docs:**
  * `docs/workspaces.md` (§6.5 gains "Workspace roles");
  * `docs/security.md` (the grant integrity argument);
  * `docs/design/workspace-secrets.md` §10 (Q4's "until a per-tenant admin
    role exists" is answered);
  * `docs/design/human-steps.md` §4.2 (the role check);
  * this file.

### 4.12 Failure cases

| case | what happens |
|---|---|
| Firestore is down at a gate whose switch is off | logged, let through (today's behaviour) |
| Firestore is down at a gate whose switch is on | 503 `role_unavailable`; nothing written |
| Cloud Identity is down | unchanged: tenant resolution already 503s (`auth.py::_from_claims`) before any role is read |
| the settings key is missing in an environment | every grant fails verification. The reader treats every person as holding the default, and the default as `maintainer`. Role writes answer 503 `role_signing_unavailable` and write nothing. The resolver logs once at start. This fails toward today's behaviour, never toward wider |
| the key rotates | `settings_key_previous` verifies older grants. A re-sign pass is not needed, because grants are append-only and the next write signs with the new key. Retire the previous key only after every tenant's newest grant postdates the rotation. `scripts/` gets no new job; `GET /v1/admin/roles/person` reports `key` for checking |
| a person leaves the group | not a member. Their override stays, inert, and applies again if they rejoin. Q3(b) would change that |
| a group is removed from `TENANT_GROUPS` | the workspace disappears from everyone's choices. Its grants stay, inert |
| two admins write at once | two grants with different `granted_at`; the later wins, and both are audited |

### 4.13 What this does not do

* It does not let a role admit someone to a workspace, or make a personal
  workspace shareable (Q3).
* It does not bind Google groups to roles (Q4, design B).
* It does not let a workspace admin act on the platform: limits, pools,
  providers, other workspaces, `platform: true` repositories.
* It does not change who may answer a human step beyond adding the role
  floor. Per-step assignees stay declined (`human-steps.md` Q2).
* It does not close `tenant-settings-integrity.md`'s wider problem. It only
  makes the role store safe while that problem remains.

---

## 5. Frozen contract: change requests

**None.** Nothing in `apps/common/swarm_common/` has to change, for these
reasons:

* **The ladder and capability table live in `swarm_api/roles.py`.** No other
  service decides what a person may do. The scheduler dispatches tasks that
  swarm-api already admitted. The reconciler and the worker never see a
  person.
* **The grant MAC is swarm-api's alone, so its canonical form is too.**
  `specsign.py`'s settings-block form (contract request 86) is in the
  contract because the scheduler verifies those blocks. Nothing outside
  swarm-api verifies a grant.
* **`Principal`, `Tenant`, `tenant_id_for_group` and `tenant_id_for_user` are
  read as they are.** A role is not a field of `Tenant`. Putting it there
  would make it a field the tenant's own worker can rewrite (`models.py`'s
  RECORD ONLY fields, contract request C of `tenant-settings-integrity.md`).

**Considered and not requested:**

* a `WorkspaceRole` enum in `models.py`. The plugin and UI read role names
  as strings from the API, and the API publishes the ladder (`roles` in
  §4.2), so nothing outside swarm-api imports it;
* a `role` field on `Principal`. `Principal` is about the caller, and a role
  is per (caller, tenant), so the field would be wrong after
  `_select_tenant`.

If the owner chooses Q4(b) later (groups bound to roles), the binding is
still swarm-api's, and that also needs no request.

---

## 6. Build plan

Each lane owns the files named in its row, and no file appears in two lanes'
rows. Every lane:

* pushes its tests alone to the PR branch first, and its fix to
  `<branch>-fix`;
* runs `scripts/changed-guards.sh` before finishing.

The CLAUDE.md review (credentials, tenant isolation, IAM) applies to
ROLES-API-CORE, every ROLES-GATES lane, and ROLES-MCP, because each decides
who may act in a tenant. The UI is split across two lanes to stay within
~6 files each (`live-browser.md` §6).

| phase | lane | files | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **ROLES-INDEX** (Track C) | `terraform/modules/firestore/indexes.tf`, `tests/terraform/firestore.tftest.hcl` | the plan has a composite index on `workspace_role_grants` (`tenant_id`, `subject`, `granted_at` DESC) and one on `admin_audit` (`target_tenant_id`, `at` DESC) |
| 1 | **ROLES-API-CORE** | `apps/swarm-api/swarm_api/roles.py` (new), `routes/roles.py` (new), `deps.py`, `main.py`, `errors.py`, `refusals.py`, `settings.py`, `metrics.py`, `routes/tenants.py`, `tests/unit/control_plane/test_roles*.py` (new), `tests/unit/control_plane/test_new_refusals_ship_off.py` (only `NOT_SWITCHABLE_CODES` entries with reasons) | an empty grant set resolves every group member to `maintainer` and a personal workspace's person to `admin`; a grant with a bad MAC, another tenant's `tenant_id`, or a copied older `granted_at` does not win (compare the resolved role before and after writing it with a fake Firestore that has create/update but no list); 61 unsigned newer grants give `role_unavailable`, never a wider role; `PUT /person` by a `maintainer` is 403 `ROLE_ADMINISTER_REQUIRED` and writes nothing (documents before and after); `PUT /default {admin}` is 409; a `u-` tenant write is 409; a service account email is 403 `ROLE_PERSON_REQUIRED`; a write and its `admin_audit` row commit together (an injected failure leaves neither); no route response contains a grant document id (scan every response body in the suite); `GET /v1/tenants/mine` carries `role` per choice; `POST /v1/tenants/me/credentials` by a `maintainer` logs `refusal report-only code=ROLE_ADMINISTER_REQUIRED` with the switch off and is 403 with it on; with the switch on and Firestore failing, 503 `role_unavailable`; with it off and Firestore failing, the request goes through |
| 2 | **ROLES-GATES-WORK** (after CORE) | `routes/tasks.py`, `routes/workflows.py`, `routes/runs.py`, `routes/live.py`, `routes/browser_logins.py`, `agent_questions.py`, their tests | with each switch on: a `viewer` is 403 `ROLE_SUBMIT_REQUIRED` on `POST /v1/tasks`, `/batch`, `/workflows`, `/runs` and nothing is written (count tasks before and after); a `submitter` cancels their own task and is 403 `ROLE_APPROVE_REQUIRED` cancelling another's; `run_owner_auth` fails a run whose creator was narrowed to `viewer`; with each switch off, all of the above go through and log the code |
| 2 | **ROLES-GATES-DECIDE** (after CORE) | `routes/approvals.py`, `approvals.py`, `ghapprove.py`, the human-step answer route's file if HW-API-ANSWER has merged, their tests | with the switch on, a `submitter` is 403 `ROLE_APPROVE_REQUIRED` on `:approve`, `:reject`, `plan:approve`, and the item stays pending; an `approver` approves; a gate `approvers` list naming a `submitter` still refuses them; the decision reads the role fresh (a cache primed with `approver`, a grant to `viewer`, then approve: refused); a `/swarmcloud approve` comment from a narrowed member is refused with the code in the reply |
| 2 | **ROLES-GATES-AUTOMATE** (after CORE) | `routes/schedules.py`, `schedulefire.py`, `routes/repositories.py`, `routes/access.py`, `routes/accounts.py`, their tests | with the switch on, an `approver` is 403 `ROLE_MAINTAIN_REQUIRED` on schedule create/delete/run, repository register/delete, an access grant and an account delete; a firing whose owner was narrowed to `viewer` is skipped and the schedule auto-paused `OWNER_ROLE_NARROWED`, with a `schedule_audit` row; with the switch off, it fires and logs |
| 2 | **ROLES-GATES-SETTINGS** (after CORE) | `workspace_secrets.py`, `mcp_servers.py`, `routes/provider_keys.py`, `gittokens.py`, `mergesettings.py`, `codeintel.py`, `judgment.py`, their tests | a group workspace `admin` (not a platform admin) can create a secret and `can_manage` is true; a platform admin who is a `viewer` in the workspace still can (no path lost); a `maintainer` is refused `ROLE_ADMINISTER_REQUIRED` with the switch on; merge settings keep their own switch and accept a workspace admin |
| 3 | **ROLES-MCP** (after CORE) | `apps/swarm-mcp/swarm_mcp/server.py`, `sc.py`, `client.py`, `plugin/skills/sc/SKILL.md`, `plugin/commands/sc.md`, `tests/unit/mcp/` files for each | `swarm_tenants` output carries each workspace's role; `sc roles set` without the typed confirmation sends nothing (a fake transport records no request); no MCP tool's schema names a role-write route (scan the registered tools) |
| 3 | **ROLES-UI-PEOPLE** (after CORE) | `apps/swarm-ui/src/WorkspacePeople.tsx` (new), `WorkspaceRolesAdmin.tsx` (new), `roles.ts` (new), `api.ts`, `types.ts`, `App.tsx`, `People.tsx`, their `__tests__` | `#settings/people` renders the roster for a `viewer` with no controls and with controls for an `admin`; the default selector offers no `admin`; Admin › People shows the Workspace roles card and the drawer's roles; a personal workspace shows the fixed-admin line |
| 4 | **ROLES-UI-GATES** (after UI-PEOPLE) | `Approvals.tsx`, `QuestionsCard.tsx`, `Submit.tsx`, `Schedules.tsx`, `Repositories.tsx`, the Profile seam's file, their `__tests__` | a `viewer` sees Approve/Reject disabled with the role sentence; Submit is disabled for a `viewer`; Profile shows the stored role and its source, and the derived one when `role` is absent |
| each | **ROLES-DOCS** | `docs/workspaces.md`, `docs/security.md`, `docs/design/workspace-secrets.md`, `docs/design/human-steps.md`, this file | `test_docs_describe_what_was_built`, where it reads these files |
| 5 | **ROLES-ACCEPT** (after GATES and the dev switches) | `tests/acceptance/` one probe, its workflow wiring | with a dev acceptance identity granted `viewer` in a test workspace: `POST /v1/tasks` is 403 `ROLE_SUBMIT_REQUIRED`, `GET /v1/tasks` is 200, and the grant and its revocation each appear in `GET /v1/workspace/roles` `audit` |

Order:

1. ROLES-INDEX and ROLES-API-CORE, in parallel.
2. The four GATES lanes, in parallel. No two share a file: `routes/runs.py`
   is WORK's, and `schedulefire.py` is AUTOMATE's.
3. ROLES-MCP and ROLES-UI-PEOPLE, in parallel; then ROLES-UI-GATES.
4. The operator turns the five switches on in dev (`dev.tfvars`
   `api_refusals`), then ROLES-ACCEPT goes green.
5. A week of report-only lines in prod. Then the switches go on in prod.

The change touches admission's front door (submissions), so step 4 also
runs `make smoke concurrency-test race-test` against dev. That is the
operator's step, run in CI.

---

## 7. Owner questions

Each is also in this lane's `questions.json`. The recommendation is first.

**Q1. The role set:**

* (a) **Five roles on one ladder: viewer < submitter < approver < maintainer
  < admin.** Recommended: each role is one word on every screen; maintainer
  is exactly today's member; approver is the issue's own example.
* (b) Three roles: viewer, member, admin. Simpler, but there is no role that
  can approve without also managing schedules and repositories.
* (c) Capability sets per person (design C). Allows separation of duty
  (approve but not submit), at the cost of a role nobody can name.

**Q2. Who names the first admin of a group workspace?**

* (a) **A platform admin, in Admin › People.** Recommended: it is one click,
  it is audited, and no workspace starts with a self-appointed admin.
* (b) A deploy-time list beside `TENANT_GROUPS` (`TENANT_GROUP_ADMINS`),
  seeded as grants `via: "config"`. It needs no click, but a role change then
  needs a deploy.
* (c) No workspace admins: only platform admins manage roles. The smallest
  change, but every narrowing goes through the platform team.

**Q3. May a role grant admit someone who is not in the workspace's Google
group, or make a personal workspace shareable?**

* (a) **No: membership comes from Google, and a role only narrows or
  widens what a member does.** Recommended: `X-Swarm-Tenant`, tenant
  resolution and invariant 9 stay as they are.
* (b) Yes: a grant admits. This changes `tenant_choices`, makes personal
  workspaces multi-person, and needs its own design for whose secrets and
  GCS prefix a guest's work uses.

**Q4. Google groups bound to roles (design B), for example
`eng-approvers@ → approver`?**

* (a) **Not now. Per-person overrides and one default, with groups as a
  later lane.** Recommended: no extra Cloud Identity calls, and every change
  is audited in the platform.
* (b) Yes, in this build. Each bound group costs one `checkTransitiveMembership`
  per caller per 120 s, and its changes happen outside the audit log.

**Q5. A platform admin in a workspace:**

* (a) **Holds that workspace's role like anyone, keeps every platform-admin
  path that exists today, and may grant themselves a role, audited.**
  Recommended: consistent with approvals' and human steps' "no admin bypass".
* (b) Is implicitly `admin` in every workspace they are a member of.
* (c) Is implicitly `admin` in every workspace, member or not.

**Q6. May a group workspace's `admin` manage its secrets, MCP servers and
tenant keys (today platform admins only, workspace-secrets Q4)?**

* (a) **Yes.** Recommended: Q4 was decided "until a per-tenant admin role
  exists", and this is that role. Only a platform admin can create the first
  one.
* (b) No: secrets stay platform-admin-only, and a workspace admin manages
  roles and settings only.

**Q7. Who may see a workspace's roster and role events?**

* (a) **Every member, any role.** Recommended: a refused person can see whom
  to ask, and the roster lists only overrides, never the whole group.
* (b) Workspace admins and platform admins only.

**Q8. Who may cancel someone else's task or workflow (today any member)?**

* (a) **Approver and above.** Recommended: whoever is trusted to judge work
  is trusted to stop it.
* (b) Maintainer and above.
* (c) Submitter and above, which is nearly today's behaviour.

**Q9. Ship before tenant-settings-integrity's design B removes the worker's
Firestore access?**

* (a) **Yes, with signed append-only grants (§4.3).** Recommended: a worker
  can neither forge, replay nor find a grant. The residual is a denial of
  service, which is alerted.
* (b) No: wait for ISO-IAM, then store plain grants.

**Q10. Turning the refusals on:**

* (a) **Per-code switches, off at ship; dev on first; a week of
  report-only lines in prod; then prod on.** Recommended: it is reversible
  per action without a deploy.
* (b) One switch, `WORKSPACE_ROLES_ENFORCED`, for all five. It is simpler to
  operate, but it cannot turn on submit gates before approval gates.

**Q11. Granting and revoking from the plugin:**

* (a) **The `sc roles` CLI with a typed confirmation, and no MCP tool.**
  Recommended: an agent in a person's session cannot raise anyone.
* (b) An MCP tool that requires a host elicitation prompt, as
  `swarm_account_remove` does.
* (c) Console only.
