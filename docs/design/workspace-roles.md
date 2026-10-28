# Workspace roles: a workspace decides whether each person only reads it, works in it, or administers it

**Status: designed 2026-10-18 (lane ROLES-DESIGN, part of #1518); revised
the same day for the owner's answers (lane ROLES-DESIGN-B).** §7 records the
thirteen answers (Q12 and Q13 on 2026-10-18, after the first eleven);
nothing is open.

**Status: built (read 2026-10-19 at `24b0533`); the viewer gate and the
reach switched on in dev only (ROLES-ON-DEV, 2026-10-19).** Every build lane
but ROLES-ACCEPT has merged (ROLES-DOCS is the lane that wrote this note),
and §8 records what each landed and where the code departs from this
design. Both refusal switches and
`PLATFORM_ADMIN_REACH` ship off. **Amended 2026-10-19 (ROLES-ON-DEV):** dev
now runs with `role_member_required` and the reach on, both set in
`dev.tfvars` (§4.9's amendment); `role_admin_required` stays off in dev, and
prod has all three off, so in prod a role still narrows nobody and a
platform admin reaches no workspace they are not a member of. **Amended
2026-10-28 (ROLES-ACCEPT):** the acceptance probe is built and runs in
accept.yml's smoke job; it SKIPs until the owner provisions its identities
and test workspace (§8).

The owner asked for this on 2026-10-18, answering the Settings › Profile
design (#1505, PR 1506, question Q7):

> Give each workspace real roles, so that what a person may do in a workspace
> (approve a step, answer a waiting step, manage its repositories and secrets,
> invite or remove people) is decided per workspace rather than by "member of
> the group" plus one platform-wide admin bit.

The owner answered the first draft's §7 on 2026-10-18, in PR 1526's
comments. Two answers departed from the draft's recommendation: Q1 (three
roles, not five) and Q5 (a platform admin is admin in every group
workspace). This revision is rewritten around both, everywhere they reach.

Every file and symbol cited below was read on 2026-10-18 at `2c0d5a7`, and
the tenant-resolution symbols again at `86c953a`. Lines move, so each
citation names the symbol; search for it.

What the design settles, in one line each:

* **Three roles on one ladder: `viewer` < `member` < `admin`** (Q1). A
  `viewer` reads. A `member` does exactly what every member can do today. An
  `admin` adds the workspace's secrets, MCP servers and tenant provider keys
  (Q6), the workspace's settings (Q13: merge settings, code intelligence, the
  judgment backend and a repository's `context`) and granting and revoking
  roles (§4.1).
* **Membership still comes from Google, and a role only limits what a member
  may do** (Q3). A role never admits anyone. A grant to someone not yet in
  the group is stored and applies when they join. A personal workspace's
  person is its `admin`, fixed.
* **A group workspace has one default role and per-person overrides** (Q4).
  The default starts at `member`, so **on the day this ships everyone keeps
  exactly what they have.** No Google group is bound to a role in this build.
* **A platform admin is `admin` in every registered group workspace, member
  or not, with no grant row** (Q5). Tenant resolution changes for exactly
  this: `X-Swarm-Tenant` may select any registered group workspace for a
  platform admin, and stays membership-checked for everyone else (§4.2).
  **Personal workspaces are excluded.** A platform admin can neither select
  nor act in another person's `u-<user>` workspace, because it holds that
  person's own secrets and GitHub token.
* **Everything a platform admin does in a group workspace they are not a
  member of is audited `via: "platform_admin"`**, before the route body runs,
  and the request fails closed if the audit row cannot be written. Reads
  write one row per (admin, workspace, UTC hour). A platform admin who is a
  member but holds a role below what an action needs, and passes only
  because they are a platform admin, gets the same rows (Q12, §4.2.4).
* **A platform admin names a group workspace's first admin**, in Admin ›
  People, audited (Q2).
* **Roles are stored as signed, append-only grants with ids nobody can
  guess**, in `workspace_role_grants`, so the design ships before
  tenant-settings-integrity's design B (Q9). A tenant worker can create and
  update Firestore documents today. It still cannot forge a grant, replay an
  old one, or find a real one to overwrite (§4.4).
* **Two refusal codes, `role_member_required` and `role_admin_required`, one
  switch each, both off at ship** (Q10). Each goes on in dev first, then in
  prod after a week of report-only lines (§4.9).
* **Every member, of any role, sees the roster and the role events** (Q7).
* **The sc plugin grants and revokes only through `sc roles`, with a typed
  confirmation, plus one read-only MCP tool** (Q11).
* **No frozen-contract change is needed** (§5).

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

A platform admin who is not in a group's Google group cannot work in that
group's workspace at all today. `_select_tenant` answers `tenant_not_member`
to them exactly as to anyone else, so managing a group workspace's secrets
(`require_manager`: "group: platform admin only") needs a platform admin who
also happens to be in the group.

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

* **Read-only access.** There is no way to let someone watch a workspace's
  runs without also letting them submit, approve, delete a repository or
  remove a subscription account. Everyone in the group is a full member.
* **Approvals.** `approvals.py::check_gate_approver` lets any member approve
  by default. A gate that names `approvers` must list each email, per gate.
* **Human steps.** `docs/design/human-steps.md` §4.2 settles who may answer
  (owner's Q2): any member of the step's tenant, re-checked fresh, never a
  service account, and no admin bypass.
* **Secrets.** The workspace-secrets owner decision (§10, Q4: "platform admins
  only ... until a per-tenant admin role exists") leaves a group workspace
  unable to manage its own secrets without a platform admin, and (§1.1) that
  platform admin must also be in the group.
* **Settings › Profile.** PROFILE-DESIGN-B (in flight) shows a role derived
  from the fields in §1.1, behind one seam. With no stored role, the page can
  show only "owner", "member" or "platform admin".

### 1.4 What "done" lets someone do that they cannot today

These are the issue's acceptance tests, made concrete for the three roles:

* A workspace admin gives `carol@` the `viewer` role in `eng`. Carol still
  sees `eng` in her workspace switcher and can read its runs, logs and
  approvals. When she submits, approves, answers or cancels there, she gets
  403 `role_member_required`, and nothing is written.
* `dave@`, a `member` of `eng`, approves a held run. Carol cannot.
* Each grant and revocation appears in Admin › People › Recent admin actions
  and on the workspace's own people page, with who did it, from what, to
  what, and when.
* Settings › Profile shows "eng: viewer (granted by alice@, 2026-10-20)",
  not "member".
* A platform admin names `alice@` the first admin of `eng`. From then on
  `eng` manages its own secrets, MCP servers and provider keys with no
  platform admin involved.
* A platform admin who is not in `eng@example.com` selects `eng` in the
  switcher, rotates its provider key, and that action appears in
  `admin_audit` as `via: "platform_admin"`. The same admin selecting
  `u-carol` is refused `tenant_not_member`, as today.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **`searchTransitiveGroups` returns 403 in this project** (CONTRACT.md,
  "Cloud Identity group resolution"). The platform never lists a caller's
  groups. It asks "is this caller in THAT group?" with
  `checkTransitiveMembership`, one registered group at a time, and every call
  sends `x-goog-user-project` (`groups.py`, module docstring;
  `groups.py::CloudIdentityGroups.is_member`). Any design that needs "which
  groups is this person in?" is impossible here. A design that adds groups
  adds one lookup per group per caller per 120 s cache window. The
  platform-admin reach of §4.2 adds **no** lookup: it reads `TENANT_GROUPS`,
  a deploy-time list, and asks Cloud Identity nothing.
* **A group workspace is exactly one Google group.** `TENANT_GROUPS`
  (`settings.py`, `ApiSettings.tenant_groups`) is a deploy-time list, and
  the tenant id is the slug of the group's local part (frozen
  `identity.tenant_id_for_group`). Nobody registers a group in the console.
  So "a group workspace's registered groups" is one group, and "a per-group
  default role" is one value per workspace.
* **A registered group can derive a personal workspace's id.** The group
  slug adds no prefix while the personal slug adds `u-`, so a group
  `u-eng@example.com` derives `u-eng`, the same id as `eng@example.com`'s personal
  workspace (`store.py::assert_tenant_scope`, docstring). Anything that lets
  a platform admin select group workspaces by id must therefore never offer
  an id in the `u-` namespace (§4.2.2).
* **A tenant worker can get, create and update any Firestore document whose
  id it can name.** It cannot list or delete (`swarmTenantWorkerFirestore`,
  `terraform/bootstrap/platform_roles.tf`;
  `docs/design/tenant-settings-integrity.md` §1.1). That design's B removes
  the binding, but it is not built. The owner chose to ship roles first (Q9),
  so the role store must be safe while the binding exists.
* **A new 4xx refusal ships report-only** (`refusals.py`, module docstring;
  `tests/unit/control_plane/test_new_refusals_ship_off.py`). Every new
  refusal on an existing route is a `Switch` that defaults off.
* **The platform `is_admin` bit is about the caller, not the workspace,
  today.** `_select_tenant` changes neither it nor `principal.groups`.
  Approvals and human-step answers deliberately give a platform admin no
  bypass (`routes/approvals.py`, module docstring; `human-steps.md` §4.2
  step 3). **Q5 changes this for group workspaces**, and §4.2 and §4.8 say
  exactly where.

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

* **1. Only LEASED/DISPATCHED/STARTING/RUNNING cost anything.** A role is
  checked in swarm-api before an agent is written. A refused submission creates
  no agent, so it never reaches QUEUED, let alone a lease. Nothing waits on a
  role. A role change does not move any agent's state: work already submitted
  keeps running under the same tenant. A platform admin's submission in a
  group workspace is an ordinary submission in that tenant and is admitted
  against that tenant's limits like any member's. **Kept.**
* **2. All-or-nothing reservation.** Admission (`swarm_common/admission.py`)
  is not touched. Roles are read before `SubmissionService` and never inside
  the reservation transaction. **Kept, untouched.**
* **3. Concurrency counts from LEASED.** Untouched. Work a platform admin
  submits in `eng` counts against `eng`'s ceiling, not theirs.
* **4. Workers never sleep through a provider wait.** Untouched. A human step
  stays `PARKED(AWAITING_INPUT)` while it waits for an answer. A role decides
  who may answer, not how the step waits.
* **5. Fencing.** Worker routes (attempt proof plus generation) are not
  role-checked. They authenticate a worker, not a person. A stale worker still
  exits without running the agent. **Kept, untouched.**
* **6, 7.** Untouched.
* **8. Checkpoints.** Untouched. A refused cancel leaves a running attempt
  checkpointing as before.
* **9. Per-tenant isolation.** This is the invariant the design rests on, and
  Q5 is the one answer that changes how a tenant is reached. It holds in five
  ways:
  * **A role never admits.** A grant to someone outside the group gives them
    nothing until Cloud Identity says they are in it. Membership choices are
    still built only from verified memberships.
  * **The platform-admin reach selects a tenant whole, and only a registered
    group tenant** (§4.2). `tenant_id` and `tenant_principal` are replaced
    together from one `TENANT_GROUPS` entry, so `Store.assert_tenant_scope`
    and `SubmissionService.scope_for` keep their collision check. An id in
    the `u-` namespace is never reachable, so no person's own workspace (its
    user secrets, its GitHub token, its GCS prefix) is reachable by anyone
    but its person. The default tenant (`resolve_tenant`) is computed from
    `principal.groups` exactly as today, so the reach can never change where
    a headerless request lands.
  * **An admin acting in `eng` acts with `eng`'s identity only.** Work they
    submit is dispatched under `eng`'s GSA, reads `eng`'s secrets, writes
    `eng`'s GCS prefix and runs in `eng`'s namespace, because each of those
    is derived from the agent's `tenant_id` and nothing else. A user-scoped
    slot (`git-u-<hash>`, a user provider key) is named through the
    *selected* tenant's `Tenant.secret_name` (`gittokens.py`, module
    docstring), so it is `eng`'s slot for that person, never their personal
    workspace's. Nothing of another tenant's mixes in.
  * **A grant is bound to its tenant by its signature** (§4.4), so a worker
    in tenant A cannot copy one of A's grants into B.
  * **Every role read is scoped by the tenant `_select_tenant` chose.**
    Nothing a caller sends picks the tenant whose roles are read.
  **Kept.** The reach widens *who* may enter a group tenant (a platform
  admin, audited), never *what* a tenant can reach. That is why it goes
  through the security review the build plan names (§6).
* **10. Callers pick a `runner_profile` by name.** No role route accepts an
  image, a command, resources or backend parameters. A role restricts which
  routes a person may call. It never widens what a body may carry. A platform
  admin in a group workspace submits through the same bodies as a member.
  **Kept.**

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing (§5). The role ladder, the grant shape, the signature and the
platform-admin reach are read and written by swarm-api only. `AuthContext`
lives in `swarm_api/auth.py`, not in the contract. `resolve_tenant`,
`tenant_id_for_group`, `tenant_id_for_user` and `Principal` are used as they
are. The scheduler, the reconciler and the worker never decide what a person
may do. The signature reuses the settings key that swarm-api already holds
(`ApiSettings.settings_key`), under its own domain string, so it needs
nothing from `specsign.py`.

---

## 3. Designs that could work

### 3.1 Design A: a three-role ladder, a default per workspace, per-person overrides, signed append-only grants (chosen)

Each group workspace has one stored default role, and its admins can set any
person to another role. Grants are Firestore documents, one per change, with
random ids. Each carries an HMAC that binds the tenant, the person, the role
and the time. The newest valid grant for a (tenant, person) pair is that
person's role.

* **Code:** one new module (`roles.py`) and one new route file. Each gated
  route gets one call to `roles.require(auth, "member")` or
  `roles.require(auth, "admin")`. The UI and plugin get a role field.
* **Migration:** none written. An empty grant set means "the default", and
  the default with no default grant is `member`, which is today's member.
* **Latency:**
  * One Firestore query per (tenant, person) per 15 s per instance, about
    5-10 ms on a miss. None for a platform admin in a group workspace: their
    role is `admin` without a read.
  * Decisions (approve, answer, cancel, grant) read fresh.
  * No extra Cloud Identity call: membership is the lookup that already
    happens.
* **Money:** reads at Firestore list prices, about $0.06 per 100k. At the
  platform's request rate this costs well under a dollar a month. The
  platform-admin audit rows (§4.2.4) are writes at $0.18 per 100k, a few
  hundred a day at most.
* **What it breaks:** nothing on the day it ships. Once a switch is on, a
  person narrowed to `viewer` is refused, which is the point of the design.

### 3.2 Design B: role groups, where each role is a Google group (declined, Q4)

The workspace's admins bind more Google groups to roles, for example
`eng-viewers@example.com → viewer`. A person's role is the highest role among
the bound groups Cloud Identity confirms they are in.

* **Latency:** one more `checkTransitiveMembership` per bound group per
  caller per 120 s, because the 403 on `searchTransitiveGroups` means the
  groups cannot be fetched in one call.
* **Operations:** granting a role means editing a Google group, which a
  workspace's admins usually cannot do, and the change is never seen by the
  platform's audit log.
* **Why declined:** the owner chose one default plus per-person overrides for
  this build (Q4). Nothing in A precludes adding bound groups later as a
  third source, between the default and a person's override.

### 3.3 Design C: a capability set per person (declined; its case is now stronger)

Instead of a ladder, each person holds a set of capabilities, such as
`{submit, approve, repositories}`.

* **Code:** the same storage as A. More UI: a matrix of checkboxes per
  person.
* **What it costs:** every refusal message and every screen has to explain a
  combination, not a name. Two admins can build sets that nobody can describe
  in a word, which defeats "Settings › Profile shows the stored role".
* **What it buys:** separation of duty.

**Q1 changes C's rationale, and this records how.** The first draft's
five-role ladder (viewer < submitter < approver < maintainer < admin) could
already express "approves but does not manage schedules, repositories or
accounts". C's only advantage over it was the rarer case of "approves but
cannot submit". With three roles, **separating approving from maintaining is
no longer expressible at all**: a `member` approves, rejects, answers,
cancels, edits schedules, registers repositories and manages subscription
accounts, all together, because that is what a member does today. A
workspace that wants a reviewer who cannot delete a repository, or a
submitter who cannot approve, has no role for it. C would give it one; the
owner chose the smaller ladder anyway, for legibility, and the narrower
roles can be added to the ladder later without changing storage (a grant's
`role` is a string, and the ladder lives only in `roles.py`).

### 3.4 Within A: how a platform admin reaches a group workspace (Q5)

The owner decided that a platform admin is `admin` in every group workspace,
member or not, with no grant row, and not in anyone's personal workspace.
Three shapes could deliver it:

* **(i) The header reaches further for a platform admin (chosen).**
  `_select_tenant` lets `X-Swarm-Tenant` select any registered group tenant
  when `is_admin` is true, after the membership choices and never in the
  `u-` namespace. Every route then behaves exactly as for a member of that
  tenant, with the role answer `admin`. *Cost:* one new `AuthContext` field,
  one audit hook in `current_auth`, about 60 lines in `auth.py`; no Cloud
  Identity call, no Firestore read on the selection itself.
* **(ii) Synthesised grants.** Write an `admin` grant for every platform
  admin in every workspace. Refused by Q5's "no grant row", and wrong when a
  platform admin is removed: every grant would need revoking, and a missed
  one is a person keeping `admin` after losing the platform role.
* **(iii) A proxy under `/v1/admin/workspaces/{t}/…`.** Re-expose every
  tenant route under an admin prefix. It duplicates 167 routes and their
  tests, and each duplicate is one more place for a tenant filter to be
  forgotten.

(i) is the only shape whose isolation argument is the one every member
request already relies on: one tenant chosen whole, before any route body
runs, from a list the request's verified identity produced.

### 3.5 Decision: A, with (i)

A is the only design in which a workspace's own admins grant and revoke from
the console with each change audited, and it adds no Cloud Identity calls.
(i) delivers Q5 without a second route surface or a grant per admin.

---

## 4. The recommended design, in enough detail to build

### 4.1 The role set and what each permits

There are three roles on one ladder: `rank(viewer)=0`, `rank(member)=1`,
`rank(admin)=2`. A role permits everything the roles below it permit.
`roles.py` holds the table, and nothing else restates it. The UI and the
plugin read it from `GET /v1/workspace/roles` (§4.3), so the explanation a
screen shows is the server's own.

| lowest role | what it permits | routes (all under `current_auth` + `tenant_scope` today) |
|---|---|---|
| `viewer` | read | every GET of agents, checkpoints, attempts, leases, outcomes, workflows, runs, issues preview, repositories (and their code-intelligence reads), schedules (and firings, audit), approvals, questions, live session/recordings/audit, notifications, git-token slots, secrets (names and grants), MCP servers, browser logins, stranded PRs; `POST /v1/live/ticket` (a viewer ticket); `POST /v1/notifications/read*`; `POST /v1/onboarding/dismiss`; `GET /v1/workspace/roles` |
| `member` | everything a member does today | **submit:** `POST /v1/tasks`, `/v1/tasks/batch`, `/v1/workflows`, `/v1/runs`. **Cancel, anyone's** (Q8): `POST /v1/tasks/{id}/cancel`, `/v1/workflows/{id}/cancel`. **Decide:** `POST /v1/approvals/{id}:approve`, `:reject`; `/v1/runs/{id}/plan:approve`, `plan:edit`, `plan:reject`; `POST /v1/schedules/{id}:merge-mode`; a `/swarmcloud approve` comment (`ghapprove.py`). **Answer:** the human-step answer `POST /v1/workflows/{wf}/steps/{step}/input` (`human-steps.md` §4.2, built since: §8, ROLES-GATES-DECIDE); `POST /v1/tasks/{id}/answers`, `/human-ready`, `/browser-login`; `/live/control*`, `/live/device`; `POST`/`DELETE /v1/live/open-to-members`. **Schedules:** every write in `routes/schedules.py`. **Repositories:** register, PATCH (fields other than `platform`, `merge_policy` and `context`; `context` is `admin`, Q13), DELETE, index, index:run; every write in `routes/access.py`. **Accounts:** every write in `routes/accounts.py`. `DELETE /v1/browser-logins/{name}` |
| `admin` | `member`, plus Q6's powers, the workspace's settings (Q13) and roles | **Secrets** (Q6): workspace secrets writes (`workspace_secrets.py::require_manager`); tenant and repository git-token slots (`gittokens.py::_require_admin`), which are forge tokens in Secret Manager and so secrets by CLAUDE.md's rule. **MCP servers** (Q6): writes in `mcp_servers.py` (`require_manager`). **Tenant provider keys** (Q6): `PUT`/`DELETE /v1/provider-keys/{p}/tenant`, tenant-scoped `POST /v1/provider-keys/{p}/links`, `POST /v1/tenants/me/credentials`. **Workspace settings** (Q13): writes of merge settings (`mergesettings.py`), code intelligence (`codeintel.py`), the judgment backend (`judgment.py`) and a repository's `context` field (`PATCH /v1/repositories/{id}`). **Roles:** `PUT`/`DELETE /v1/workspace/roles/*` |

Each route keeps every narrower rule it has today *on top of* the role:
answers on an agent's question stay "the submitter or a platform admin"
(`agent_questions.py::answer_refusal`), human-ready stays the submitter's,
live control stays the owner's until opened, a gate's `approvers` list, the
last-editor rule and `second_member` holds stay as they are. The role is the
floor; those rules are narrower and unchanged.

**Workspace settings belong to a workspace admin** (Q13, answered
2026-10-18, which widens Q1's definition). Admin is now: member, plus Q6's
powers (secrets, MCP servers, tenant provider keys), plus the workspace's
settings, plus granting and revoking roles. The settings are merge settings,
code intelligence, the judgment backend and a repository's `context`. Today
they are "personal: its person; group: a platform admin"
(`mergesettings.py::is_owner`, `codeintel.py::require_owner_or_admin`,
`judgment.py`). They become "personal: its person; group: the role `admin`",
which a workspace admin and a platform admin (Q5) both hold. A workspace
admin therefore no longer needs a platform admin for its merge policy.
ROLES-GATES-ADMIN adds `mergesettings.py`, `codeintel.py` and `judgment.py`
(§6); the `context` field is in `routes/repositories.py`, which
ROLES-GATES-AUTOMATE owns.

**What no workspace role reaches.** These stay platform-only, gated exactly
as today (`require_admin`, `is_owner`), and a workspace `admin` who is not a
platform admin never reaches them. Each item was checked against Q13 and kept
only because it is platform-wide, not one workspace's own configuration: the
owner's list is a repository's `platform` and `merge_policy` and the
merge-mode switch on a `platform: true` repository; the rest are platform
routes, platform item types or other workspaces' data, and none is a setting
of the workspace the admin administers.

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
* their personal workspace request (`/v1/workspace`);
* `GET /v1/tenants/me`, `/v1/tenants/mine` and the Profile page.

### 4.2 Platform admins: admin in every group workspace, and in no one else's personal workspace

#### 4.2.1 The rule

| a platform admin (`AuthContext.is_admin`) in… | may select it | role there | audited `via: "platform_admin"` |
|---|---|---|---|
| a registered group workspace they are a member of | yes, as today | `admin`, `source: "platform_admin"`, whatever any grant says | no, unless a grant narrowed them below what the action needs and their platform-admin status is what lets it through: then **yes**, the same rows (Q12) |
| a registered group workspace they are **not** a member of | **yes, new** | `admin`, `source: "platform_admin"` | **yes, every action** |
| their own personal workspace | yes, as today | `admin`, `source: "personal"` | no |
| anyone else's personal workspace | **no**, `tenant_not_member` as today | none | (refused before the route) |

This replaces the first draft's "`is_admin` never satisfies a role check".
**A platform admin satisfies every role check in a group workspace and none
in someone else's personal workspace**, which they cannot select in the first
place. A grant naming a platform admin is stored and shown, but is inert
while they are one; it applies if they stop being one.

`is_pool_admin`, the rollup sweeper, the schedule tick and a listed service
account are not `is_admin` and get no reach.

#### 4.2.2 Tenant resolution: the change, exactly

`auth.py` today builds `tenant_choices` from verified memberships
(`_tenant_choices`), puts the caller's own ready personal workspace first
(`_with_own_workspace`), and lets `X-Swarm-Tenant` pick only from that list
(`_select_tenant`). The change keeps all three and adds a second, separate
list:

1. **`AuthContext.platform_reach: tuple[tuple[str, str], ...] = ()`** (new
   field, swarm-api only). Built at the end of `_from_claims` by
   `Authenticator._platform_reach(member_groups, is_admin)`:
   * empty unless `is_admin` is true **and** `ApiSettings.platform_admin_reach`
     is on (§4.7) **and** `_special(ctx)` is false;
   * otherwise one `(tenant_id_for_group(g), g.lower())` for each `g` in
     `TENANT_GROUPS`, in admin order, first per id, the same walk as
     `_tenant_choices`;
   * **skipping every id already in the membership choices** (a member
     reaches it as a member, not via the reach);
   * **skipping every id that starts with `u-`** (§2.1: a group slug can
     land in the personal namespace, and a personal workspace is never
     reachable).
   It asks Cloud Identity nothing: `TENANT_GROUPS` is configuration.
   `principal.groups` is not touched, so `resolve_tenant`, and with it the
   default tenant, is exactly as today.
2. **`_with_own_workspace`** treats an id in `platform_reach` like a
   membership choice in its early-return test, so a request naming a reach
   tenant costs no personal-workspace read.
3. **`_select_tenant`** tries, in order:
   1. `tenant_choices` (memberships and the caller's own `u-`), as today;
   2. then `platform_reach`, when `is_admin`. A match replaces `tenant_id`
      and `tenant_principal` together from the one entry, exactly as a
      membership match does, and sets **`tenant_via="platform_admin"`**
      (new `AuthContext` field, `""` otherwise);
   3. otherwise, when `admin_unresolved` is true and the requested id is a
      registered group id, **503 `upstream_unavailable`**, never a 403: the
      unanswered admin lookup could have changed the answer, and a 403 here
      would tell an operator mid-incident they lack a privilege they hold;
   4. otherwise `TenantNotMember`, with today's message, which is the same
      whether the id exists or not.
4. **Everyone else is unchanged.** For a caller with `is_admin` false,
   `platform_reach` is empty and steps 3.2 and 3.3 never match: the header
   still selects only a verified membership, per registered group, through
   `checkTransitiveMembership` with `x-goog-user-project`.
5. **`GET /v1/tenants/mine`** lists the membership choices, then the reach
   entries, each `TenantChoice` carrying `via: "member" | "personal" |
   "platform_admin"`. A platform admin's switcher therefore lists every
   registered group workspace, and the ones they reach only as an admin are
   marked as such.

The reach entries go through the same `ensure_tenant` /
`assert_tenant_scope` collision check as a member's (`tenant_principal` is
the registered group email), so a registered group whose id collides with
another tenant document's principal is refused exactly as it is for a member.
A group in `TENANT_GROUPS` whose tenant document does not exist yet is read
as empty and created on the first submission, as on a member's first sight.

#### 4.2.3 Re-checks that run with nobody on the call

Three paths ask "is this stored person a member NOW?" through
`Authenticator.is_tenant_member`: `schedulefire.py::member_auth` (a
schedule's owner, at each firing), `routes/runs.py::run_owner_auth` (an issue
run's creator, at its auto approval and CI-fix rounds), and the human-step
answer's fresh membership check (`human-steps.md` §4.2 step 2). Each would
refuse a platform admin who is not in the group, which would contradict Q5
for a schedule a platform admin owns.

So `auth.py` gains **`Authenticator.may_act_in(email, tenant) -> str`**,
which returns `"member"`, `"platform_admin"` or `""`:

* `"member"` when `is_tenant_member(email, tenant)` is true (no change, no
  extra cost);
* else `"platform_admin"` when the tenant's `kind` is `"group"`, its id does
  not start with `u-`, `platform_admin_reach` is on, and
  **`holds_platform_admin(email, fresh=True)`** is true: `PLATFORM_OWNER`,
  `ADMIN_USERS`, `admin_roles.holds_admin` read past its cache, or an
  `ADMIN_GROUPS` membership asked of Cloud Identity with `fresh=True`. That
  is one extra lookup per admin group, only when the membership check has
  already said no;
* else `""`. A failed lookup is 503 `upstream_unavailable`, never a guess,
  exactly as `is_tenant_member` does today.

The three callers switch to it. `member_auth` builds the stored owner's
`AuthContext` with `tenant_via` set from the answer, so the firing's role is
`admin` and its audit row (§4.2.4) is written. A personal workspace is never
`"platform_admin"`, so a platform admin who once owned work in someone
else's `u-` tenant (they cannot) is not a case.

**Human steps and approvals: what Q5 supersedes.** `human-steps.md` §4.2
step 3 ("no admin bypass; a platform admin answers only as a member") and the
same rule in `routes/approvals.py`'s docstring are superseded **for group
workspaces**: a platform admin answers and approves there as that
workspace's `admin`. Step 1 (`person_required`) and the gate-specific rules
(a gate's `approvers` list, the last editor, `second_member`) are unchanged
and still apply to them; a platform admin acting through the reach counts as
a member for `second_member`, because Q5 makes them one in effect. In a
personal workspace nothing changes: only its person answers. ROLES-DOCS
amended `human-steps.md` §4.2 on 2026-10-19; `routes/approvals.py`'s
docstring was amended by ROLES-GATES-DECIDE (§8).

#### 4.2.4 The audit of a platform admin's actions

**Every write a platform admin makes in a group workspace they are not a
member of (`tenant_via == "platform_admin"`) is audited before it runs, and a
read is recorded once per hour. An action is never taken unaudited** (Q12,
answered 2026-10-18).

* **Where:** in `deps.py::current_auth`, after `authenticate` and
  `require_continuation_route`, before the limiter, so every route reaches it
  (directly or through `tenant_scope`/`admin_auth`) and a route added
  tomorrow is covered without anyone remembering. The writer is
  `roles.py::audit_platform_admin(ctx, auth, route)`.
* **Writes (any method but GET/HEAD):** one `admin_audit` row
  `platform_admin_acted`, written **before the route body runs**:
  `{by, at, target_tenant_id, detail: {method, route, path_params, via:
  "platform_admin"}}`. `route` is the template (`/v1/secrets/{name}`), never
  the concrete path with its query, and **the body is never recorded**, so a
  secret value or a prompt cannot land in the audit log. A write that fails
  answers 503 `platform_admin_audit_unavailable` and the route never runs:
  fail closed. The response status is logged on the same request
  (`platform_admin acted tenant=… route=… status=…`).
* **Reads:** one `platform_admin_viewed` row per (admin, tenant, UTC hour)
  per instance, written on the first read of that hour, fail closed the same
  way. That keeps a console that polls every few seconds from writing
  thousands of rows while still recording that an admin looked, and when.
  The owner chose this over a row per read (Q12).
* **A member whose own role is too low.** A platform admin who *is* a member
  of the group, holds a role below what the action needs (a grant narrowed
  them to `viewer`), and is let through only because they are a platform
  admin (`may_act_in` answers `"platform_admin"` where the grant would have
  refused) gets exactly the rows above: `platform_admin_acted` before a
  write, `platform_admin_viewed` once per hour for a read, both fail closed.
  They cannot escape the audit by being added to the group as a viewer. A
  platform admin whose own role is enough for the action, and who is a
  member, writes none. `roles.py::audit_platform_admin` therefore decides
  from `tenant_via` **or** from "the grant alone would have refused this
  route", which ROLES-API-CORE computes from the role the route needs.
* **Stored-owner actions** (§4.2.3): a schedule firing or an issue run's
  auto step whose owner is reached as `"platform_admin"` writes
  `platform_admin_acted` with `route: "schedule_firing"` or
  `"issue_run_advance"` and the schedule or run id.
* **Actions with their own audit rows** (role writes, secrets, provider keys)
  also carry `via: "platform_admin"` in their own row, so the event reads
  correctly where it is shown, not only in the generic row.
* **Where it is seen:** Admin › People › Recent admin actions, and the
  workspace's own people page (§4.8), whose audit list includes these rows
  for its tenant. Every member of `eng` can therefore see that a platform
  admin acted in `eng` and on what (Q7).

### 4.3 Routes

There are two new route groups, both in `apps/swarm-api/swarm_api/routes/roles.py`
(new).

**A workspace's own, under the tenant `X-Swarm-Tenant` selects:**

```
GET    /v1/workspace/roles
PUT    /v1/workspace/roles/person      {email, role, reason?}
DELETE /v1/workspace/roles/person      {email, reason?}          -> back to the default
PUT    /v1/workspace/roles/default     {role, reason?}           -> viewer | member
```

The email is in the body, not the path, so that addresses do not land in
request logs and load-balancer access logs. People routes already avoid
addressing a person by email (`routes/people.py`, module docstring). The
writes need `admin` there: a workspace admin, or a platform admin (who
reaches any group workspace, §4.2).

`GET` response, readable by every role (Q7):

```json
{
  "tenant_id": "eng",
  "kind": "group",
  "default_role": "member",
  "default_source": "migration",         // "migration" until an admin sets it, then "grant"
  "me": {"email": "carol@example.com", "role": "viewer", "source": "override",
         "granted_by": "alice@example.com", "granted_at": "2026-10-20T09:14:03.120Z",
         "via": "member"},
  "people": [
    {"email": "alice@example.com", "role": "admin", "source": "override",
     "granted_by": "ops@example.com", "granted_at": "…", "granted_via": "platform_admin"},
    {"email": "carol@example.com", "role": "viewer", "source": "override", "…": "…"}
  ],
  "roles": [
    {"name": "viewer", "summary": "Sees everything in this workspace; changes nothing."},
    {"name": "member", "summary": "Submits, approves, answers, cancels, and manages schedules, repositories and accounts."},
    {"name": "admin",  "summary": "A member who also manages secrets, MCP servers, provider keys and people's roles."}
  ],
  "can_manage": false,
  "audit": [ {"action": "workspace_role_granted", "by": "…", "at": "…", "target_email": "…",
              "detail": {"from": "member", "to": "viewer", "via": "console"}} ]
}
```

* `me.source` is one of `override`, `default`, `personal`, `platform_admin`.
  `me.via` is the `TenantChoice.via` the request was selected through.
* `people` lists only overrides. Everyone else holds the default. Listing all
  members would mean enumerating a Google group, and the platform cannot do
  that (`searchTransitiveGroups` returns 403). Platform admins are not listed
  unless someone granted them an override.
* `audit` holds the last 50 role events and `platform_admin_*` rows for this
  tenant (§4.10).
* A personal workspace answers with `kind: "user"`, `default_role: null`,
  `people: []`, and `me.role: "admin"` with `source: "personal"`.

`PUT /person` returns `{email, role, previous_role, source: "override",
granted_at, member}`, where `member` is the fresh per-group check (§4.6).
Every write answers 409 `role_unchanged` when nothing would change, and
writes nothing.

**A platform admin's, for any group workspace (`admin_auth`):**

```
GET    /v1/admin/workspaces/{tenant_id}/roles
PUT    /v1/admin/workspaces/{tenant_id}/roles/person    {email, role, reason?}
DELETE /v1/admin/workspaces/{tenant_id}/roles/person    {email, reason?}
PUT    /v1/admin/workspaces/{tenant_id}/roles/default   {role, reason?}
GET    /v1/admin/roles/person?email=…   -> every override this email holds, across workspaces
```

These are what Admin › People calls (Q2), so a platform admin can name a
first admin without switching workspace. `{tenant_id}` must be a registered
group tenant whose id is not in the `u-` namespace; anything else is 404. The
writes are the same functions as the workspace routes, called with
`granted_via: "platform_admin"`.

**Role fields on routes that exist** (`routes/tenants.py`):

* `GET /v1/tenants/me` adds `role: {name, source}` for the selected tenant.
* `GET /v1/tenants/mine` adds the same `role`, and `via` (§4.2.2), to each
  `TenantChoice`.

That costs one cached query per listed membership workspace, and none for a
reach entry (its role is `admin` by rule). This field is the seam Settings ›
Profile reads (§4.11).

### 4.4 Storage: signed, append-only grants with unguessable ids

**Collection `workspace_role_grants`**, document id = Firestore auto-id
(20 random characters), never returned by any route:

```
workspace_role_grants/{auto-id}
  tenant_id:   "eng"
  subject:     "carol@example.com"   // lower-cased; "*" for the workspace default
  role:        "viewer"           // viewer | member | admin; null = "back to the default" (subject != "*")
  granted_by:  "alice@example.com"
  granted_at:  "2026-10-20T09:14:03.120Z"   // swarm-api's clock, written as a string, covered by the MAC
  via:         "console" | "cli" | "platform_admin"
  reason:      "moved to read-only for the audit"   // <= 200 chars, redacted (redaction.py) before it is written
  mac:         {v: 1, key: "<key_id>", sig: "<base64 HMAC-SHA256>"}
```

There is no grant for a platform admin's `admin` (Q5: no grant row) and none
for a personal workspace's person; both are rules in `roles.py`.

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
  * With no valid "*" grant, the default is `member` (§4.7).
  * A "*" grant whose role is `admin` is invalid by shape and skipped: the
    default can never be `admin`.
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
    raises an alert on the counter. It cannot touch a platform admin's role,
    which is never read from this collection.
* **Why not one document per (tenant, person), or one roster per tenant?**
  Those ids can be guessed (`eng__carol@…`, `eng`). A worker can then read
  the signed document today and write it back after an admin narrows the
  person. That is a replay that restores a revoked `admin`, and a signature
  cannot detect it. Append-only grants with ordering under the MAC are the
  shape where the worker's get/create/update cannot undo a revocation.
* **Index.** A composite index on (`tenant_id` ASC, `subject` ASC,
  `granted_at` DESC) in `terraform/modules/firestore/indexes.tf`. For the
  workspace audit list, an index on `admin_audit` (`target_tenant_id` ASC,
  `at` DESC).
* **Cache.** Per instance, per (tenant, subject), for 15 s, the same as
  `admins.py`'s role cache. **The decision routes always read fresh:**
  approve, reject, plan approve, human-step answer, cancel, and every role
  write. A person narrowed to `viewer` therefore cannot approve during the
  cache window. For a submit, up to 15 s of stale role is the trade for one
  query per request.
* **What a failed read does.** It does not 503 every request. The failure
  is "unknown", and a gate holding an unknown role behaves like its switch:
  * off: log and let the request through, as today;
  * on: 503 `role_unavailable` (5xx, so it is not a refusal code and needs
    no switch).
  Turning a switch on therefore never adds a Firestore dependency to a route
  that did not have one before the owner chose to turn it on. A platform
  admin's role in a group workspace never needs the read.

### 4.5 Personal workspaces

The person whose verified email the tenant document names
(`PersonalTenants.ready`, `_with_own_workspace`) is the workspace's `admin`,
with `source: "personal"` (Q3). Nothing is stored. Every role write against a
`u-` tenant answers 409 `personal_workspace_roles_fixed`, and nobody else can
hold a role there. Nobody else is a member: `_with_own_workspace` only ever
adds the caller's own `u-` id, and a role never admits.

**A platform admin is not admin in anyone else's personal workspace**
(owner's follow-up to Q5). It holds that person's own user secrets and
GitHub token. §4.2.2 keeps every `u-` id out of `platform_reach`, §4.2.3
keeps it out of `may_act_in`, and `/v1/admin/workspaces/{t}/roles` 404s on
one. This design adds no route that reaches a personal workspace's contents;
the existing `/v1/admin/*` platform operations that name a tenant id (limits,
disabling) are unchanged and out of its scope.

### 4.6 Who may grant and revoke

| action | who | refused with |
|---|---|---|
| set or clear a person's role in a group workspace | that workspace's `admin`s; platform admins (from the workspace, or through `/v1/admin/workspaces/{t}/roles`) | `role_admin_required` |
| set a group workspace's default (`viewer` or `member`) | the same | the same; `admin` is 409 `default_role_not_allowed` |
| name the **first** admin of a group workspace | a platform admin, in Admin › People (Q2), audited `granted_via: "platform_admin"`, or when adding the workspace, through `first_admin` on `POST /v1/admin/workspaces` (§8, WORKSPACE-ADMIN-FIELDS). A workspace has no workspace admin until one is granted, because the default can never be `admin` | `role_person_required` for a service account; 422 for anything that is not one address, or for a personal or service workspace |
| demote or remove oneself | allowed. The last workspace admin may step down, because platform admins are always admin there and can name another. That is why there is no `last_workspace_admin` refusal (see `admins.py`'s `LAST_ADMIN` for the platform-wide case, where nobody is above) | |
| raise oneself | not a case: only an `admin` can write a role, and `admin` is the top | |
| any role change in a personal workspace | nobody | `personal_workspace_roles_fixed` |

A grant names an email. The person does not need to be a confirmed member at
grant time (Q3). A grant to someone not in the group is stored and applies
when they join. The response says `member: false` when the one per-group
check (`Authenticator.is_tenant_member`, fresh) says they are not in it yet.
That lets an admin pre-assign `viewer` to someone joining the group tomorrow,
so they never hold `member` for a day.

The email must be a person. A service account is refused with
`role_person_required` (new route only), using
`gitidentity.py::is_service_account`. A listed service-account member
(`AuthContext.member_scope`, contract request 30) already has its own narrower
scope, and a role must not widen it.

### 4.7 Migration: everyone keeps what they have until an admin narrows it

Nothing is written on the day this ships.

1. **The default with no default grant is `member`.** `member` is defined as
   exactly what a member can do today (§4.1). Every member of every group
   workspace therefore holds today's powers, with `default_source:
   "migration"`.
2. **Every personal workspace's person is `admin`.** That is today's
   "owner" of its secrets, MCP servers and keys.
3. **Platform admins keep every path they have**, and gain the reach of
   §4.2. The reach ships behind `ApiSettings.platform_admin_reach`
   (`PLATFORM_ADMIN_REACH`), **off by default**, with the reason beside the
   value: it widens who may enter a tenant, so it goes on in dev first, is
   exercised by the acceptance probe, and then goes on in prod, the same
   order as the switches. Off, `platform_reach` is empty and `may_act_in`
   never answers `"platform_admin"`, which is today's behaviour exactly.
4. **Both refusal codes ship as switches that are off** (§4.9). Even an
   admin's narrowing is report-only until the owner turns that code's switch
   on. The log line `refusal report-only code=role_member_required tenant=…
   email=…` shows exactly who would be refused, before anyone is.
5. **The one widening for workspace people is opt-in.** A workspace admin
   manages a group workspace's secrets (Q6), and only a platform admin can
   create the first workspace admin.

So nobody loses access unless two separate decisions are made: the owner turns
a switch on, and an admin narrows that person. Rollout (Q10), per code: turn
it on in dev (`terraform/environments/dev/dev.tfvars` `api_refusals`); read
a week of report-only lines in prod; turn it on in prod
(`terraform/environments/prod/prod.tfvars` `api_refusals`).

**What three roles cost the rollout.** With two codes, every member action
is one switch: the submit gates and the approval gates go on together. The
first draft's five codes could stage them. The owner's Q1 and Q10 together
choose this, and the week of report-only lines is what makes it safe: it
names every person a switch would refuse before it refuses them.

### 4.8 What each surface does with a role

* **Workspace switcher** (`Spine.tsx`; `routes/tenants.py`): a platform
  admin's list shows their memberships, then "Other workspaces (platform
  admin)" from the reach entries. Selecting one shows a banner on every
  screen of that workspace: "You are acting in eng as a platform admin. Every
  action is recorded in its audit log."
* **Approvals inbox** (`Approvals.tsx`; `routes/approvals.py`):
  * The list stays readable by everyone.
  * `:approve` and `:reject` first check `member`, read fresh, then today's
    rules unchanged: a gate's `approvers` list, `second_member` holds, the
    last-editor rule and the `spec` admin rule.
  * A gate's `approvers` list still narrows within members. A listed email
    that holds `viewer` is refused `role_member_required`; the role is the
    floor, and the list is the narrower rule on top.
  * The screen disables both buttons for a `viewer`, showing "Your role in
    eng is viewer; members decide here". The server still decides.
  * `ghapprove.py` maps the GitHub login to the member as today, then checks
    `member` for that person. A comment approval from a `viewer` gets a reply
    naming the code.
* **Human-wait answers** (`human-steps.md` §4.2; built since this was
  written, and gated as below by ROLES-GATES-DECIDE, §8): the route's
  checks become: a person (`person_required`); `may_act_in` fresh, which a
  member or (in a group workspace) a platform admin passes (`not_a_member`
  otherwise); **then `member`**, read fresh (`role_member_required`). If
  HW-API-ANSWER lands first, the ROLES-GATES-DECIDE lane adds the calls; if
  it lands after, HW-API-ANSWER adds them. Whichever is second carries the
  test.
* **Agent questions** (`POST /v1/tasks/{id}/answers`): today's rule stays
  (the submitter or a platform admin), with `member` required on top. A
  submitter narrowed to `viewer` can no longer steer their own running agent's
  agent.
* **Cancel** (Q8): `member` for any agent or workflow, the caller's own
  included. A `viewer` cancels nothing.
* **Schedules** (`routes/schedules.py`, `schedulefire.py`):
  * Writes need `member` (`role_member_required`).
  * **A firing submits as the stored owner and needs that owner to hold
    `member` now.** `schedulefire.py::member_auth` already asks whether the
    owner is a member at each firing; it now asks `may_act_in` (§4.2.3) and
    then the owner's role.
  * If the owner was narrowed to `viewer`: when the switch is on, the firing
    is skipped and the schedule auto-paused with `OWNER_ROLE_NARROWED:
    "Paused: its owner's role in this workspace is viewer; a member can take
    ownership to resume"`, written to `schedule_audit`, mirroring today's
    `OWNER_NOT_MEMBER`. When the switch is off, the firing happens and the
    would-be refusal is logged.
  * Issue runs' auto approvals and CI-fix rounds (`run_owner_auth`) get the
    same check, with the run failing as `RunOwnerNotMember` does.
* **Repositories, access, accounts**: `member` (`role_member_required`).
  Platform-only fields stay platform-only.
* **Secrets, MCP servers, tenant provider keys, git-token tenant and
  repository slots** (Q6): `admin` (`role_admin_required`).
  * `workspace_secrets.py::require_manager` and its twin in `mcp_servers.py`
    become: personal, its person; group, the role `admin`, which a workspace
    admin and a platform admin both hold. `can_manage` follows.
  * `POST /v1/tenants/me/credentials` gains the gate it lacks.
* **Workspace settings** (merge settings, code intelligence, judgment
  backend, repository `context`) (Q13): personal, its person; group, the
  role `admin` (`role_admin_required`, read fresh), which a workspace admin
  and a platform admin both hold. `mergesettings.py::is_owner`,
  `codeintel.py::require_owner_or_admin` and `judgment.py` become that
  check; merge settings keeps its own switch, `merge_settings_owner_required`
  (§4.9). A `member` can no longer change them once the switches are on.
* **Admin › People** (`People.tsx`; `routes/people.py`):
  * Gets a **Workspace roles** card (`WorkspaceRolesAdmin.tsx`, new). It
    lists each registered group workspace with its default, its admins and
    the number of overrides, plus "Name an admin" (Q2), "Set default" and the
    per-person list.
  * The Manage drawer for a person adds "Roles in workspaces", read from
    `GET /v1/admin/roles/person`.
  * "Recent admin actions" shows the role events and the `platform_admin_*`
    rows (they are `admin_audit` rows already).
* **A workspace's own people page**, `#settings/people` (new
  `WorkspacePeople.tsx`):
  * Every member, of any role, sees the default, the overrides, what each
    role permits (from `roles`) and the last 50 role and platform-admin
    events (Q7).
  * Admins see the controls: set a person's role (email and role), return a
    person to the default, and set the default (`viewer` or `member`).
  * A personal workspace shows "You are this workspace's admin; a personal
    workspace has only you".
  * This is a Settings route, not a `SECTIONS` tab. The issue forms' "Where"
    list (`tests/unit/scripts/test_issue_forms.py`) is unchanged.
* **Where a workspace admin finds their workspaces** (amended 2026-10-19,
  lane NAV-ADMIN-ROLE; part of #1633, owner decision "All under Profile;
  Admin by role"): **Admin › Workspaces you admin**, `/admin/workspaces`
  (`WorkspacesYouAdmin.tsx`).
  * It lists each GROUP workspace whose stored role for the caller is
    `admin`, read from `GET /v1/tenants/mine` (`role.name`, `kind`). A
    personal workspace is never listed: its person is its admin by rule
    (`source: personal`), and its settings are under Profile. Nor is a
    platform admin's reach entry (`via: "platform_admin"`), which is a
    workspace they can reach, not one they were made admin of.
  * Each entry links to that workspace's Workspace settings, MCP servers,
    People and roles and workspace key (`#settings/keys`, the tenant key
    row). Each link selects the workspace first (`chooseTenant`, the
    switcher's own switch), then opens the page, so the page reads and
    changes the workspace it was opened for.
  * Admin's spine item is drawn only for a platform admin or for the stored
    admin of at least one group workspace. A workspace admin who is not a
    platform admin sees only this page under Admin; a platform admin sees it
    last, after Pool limits, Tenants, Platform counts and People. Anyone else
    sees no Admin item. The routes stay, and the server is still the gate: a
    non-admin's direct link lands on the page's own refusal.
  * A platform admin is `admin` by rule in every group workspace they are a
    member of (`source: platform_admin`, §4.2.2), so for them the page lists
    every group workspace they are in, and not those they only reach.
* **The sc plugin** (`apps/swarm-mcp/swarm_mcp/`), per Q11:
  * `swarm_tenants` and `sc whoami` show each workspace's role and `via`.
  * **`swarm_roles`, a read-only MCP tool**, returns `GET
    /v1/workspace/roles` for the selected workspace: the default, the
    overrides, the caller's own role and the recent events.
  * **`sc roles [list|set|clear|default]`** calls §4.3. `set`, `clear` and
    `default` require the person to type the email and the role back at a
    prompt read from the terminal (`/dev/tty`), never from arguments or
    stdin, so a script or an agent cannot pre-supply it. A mismatch sends
    nothing.
  * **No MCP tool grants or revokes a role.** An agent driving a person's own
    plugin session must not be able to raise anyone, and the elicitation
    route (`server.py::_elicit`) is for answers, not administration.
  * A refusal's code and message are printed as the server sent them.

### 4.9 Refusal codes, and which are switches

Each is a `Forbidden` (or `Conflict`, `ValidationFailed`) subclass in
`errors.py` with its own code, lower-case as this file's newer codes are. The
two on existing routes are raised through `refusals.refuse` and registered in
`refusals.SWITCHES`, default off: **one switch per code** (Q10). The ones
raised only by new routes are listed in `NOT_SWITCHABLE_CODES` with the
reason routes in #868 and LB-L10 gave: no existing caller can be refused by
them, and continuing past one is the breach itself.

| code | status | raised by | switch? | the Switch's `why` |
|---|---|---|---|---|
| `role_member_required` | 403 | every `member` route of §4.1 for a `viewer`: submit, cancel, decide, answer, schedules, repositories, access, accounts, live control, browser logins; a schedule firing or run auto step whose owner is a `viewer` | **yes**, off | on once `GET /v1/tenants/me` carries `role`, the console disables Submit, Approve and Cancel for a viewer, and a week of report-only lines in prod names only people an admin narrowed |
| `role_admin_required` | 403 | on existing routes: secrets, git-token tenant/repository slots, MCP servers, tenant provider keys, `POST /v1/tenants/me/credentials`, and (Q13) the workspace-settings writes of code intelligence, the judgment backend and a repository's `context`. On the new role write routes: raised directly | **yes**, off **on existing routes**. On the role write routes it is raised with `raise`, because they are new and letting a non-admin write a role is the breach | on once the report-only log shows no member relying on `POST /v1/tenants/me/credentials` and every group workspace that manages secrets has a named admin |
| `role_person_required` | 403 | role writes naming a service account | no: new route only | |
| `personal_workspace_roles_fixed` | 409 | role writes on a `u-` tenant | no: new route only | |
| `default_role_not_allowed` | 409 | `PUT …/default` with `admin` | no: new route only | |
| `role_unchanged` | 409 | a write that changes nothing | no: new route only | |
| `unknown_role` | 422 | a role not among the three | no: new route only (body validation) | |
| `role_unavailable` | 503 | a role read that failed, at a gate whose switch is on | not a 4xx | |
| `role_signing_unavailable` | 503 | a role write with no settings key | not a 4xx | |
| `platform_admin_audit_unavailable` | 503 | a platform admin's request in a non-member group workspace whose audit row could not be written (§4.2.4) | not a 4xx; and failing open here would be an unaudited action | |

`tenant_not_member` (existing) is unchanged and is what a platform admin
gets for another person's `u-` id. Merge settings'
`merge_settings_owner_required` keeps its own switch, and its group-workspace
rule is now the role `admin` (Q13). No refusal code is added or removed by
Q12 or Q13.

`role_admin_required` is one code raised two ways. The guard
(`test_new_refusals_ship_off.py`) keys on the code, so it is registered as a
switch. The role routes raise it with `raise`, not `refuse`, and a test pins
that (§6). If the guard's AST collector cannot tell the two call sites
apart, ROLES-API-CORE uses a second, non-switchable code,
`role_grant_required`, on the role routes; the behaviour is the same.

Every 403 body is the shape `errors.py::to_payload` already produces:
`{"code", "message", "detail": {"tenant_id", "role", "required", "source",
"admins"}}`. For example: "Your role in eng is viewer; this needs member or
above. Ask an admin of eng (alice@example.com) to change it. Nothing was
submitted." `detail.admins` names up to three workspace admins, so the
person knows whom to ask. That is not a leak: every member can already read
the roster (Q7).

**Amended 2026-10-19: the dev rollout (owner decision "Viewer gate + reach
now", lane ROLES-ON-DEV, part of #1518).** In dev, `role_member_required` is
on: `terraform/environments/dev/dev.tfvars`'s `api_refusals` holds
`role_member_required = true` beside `index_refresh_failed`, which renders
`REFUSAL_ROLE_MEMBER_REQUIRED=on` into swarm-api. A `viewer` is refused 403
there on every member route; everyone else keeps `member` until an admin
narrows them (§4.7), so it refuses only people an admin has made viewers.
The reach is on in dev too: `var.platform_admin_reach`
(`terraform/infra/variables.tf`) renders `PLATFORM_ADMIN_REACH="true"`, a
spelling `_bool` reads as on, and dev.tfvars sets it; prod leaves it at its
default, `false`. **Still off:** `role_admin_required`, in dev and prod. Its
own condition above is that every group workspace that manages secrets has
a named admin, and eng and smoke do not have one yet; on before that, a
group workspace's secrets could be changed by nobody but a platform admin.
Prod has all three off until ROLES-ACCEPT has exercised them in dev and a
week of report-only lines names only people an admin narrowed (§6, step 6).

### 4.10 Audit events

All are written to `admin_audit`, in the collection's existing `snake_case`
past-tense convention (`workspace_secret_created`, `provider_key_set`). The
role events are written in the same transaction as their grant.

| action | when | `detail` |
|---|---|---|
| `workspace_role_granted` | a person's override is set | `{from, to, from_source, via, reason, member}`, and `first_admin: true` when it was named on Add a new workspace |
| `workspace_role_cleared` | a person returns to the default | `{from, to: <default>, via, reason}` |
| `workspace_default_role_set` | the default changes | `{from, to, via, reason}` |
| `workspace_role_grant_invalid` | the reader skipped an invalid grant (once per document per instance, so a flood writes at most 60 rows per read) | `{tenant_id, subject, reason: "mac" \| "shape"}`, **no document id** |
| `platform_admin_acted` | a platform admin's write in a group workspace they are not a member of, a write by a member platform admin whose own role would have refused it, or a stored-owner action reached that way (§4.2.4) | `{method, route, path_params, via: "platform_admin"}`, never a body |
| `platform_admin_viewed` | the first read of a UTC hour by that admin in that workspace, per instance; also for a member platform admin whose own role would have refused the read | `{route, hour, via: "platform_admin"}` |

Each row carries `target_tenant_id`, `target_email` (role events, absent
for the default), `by` (the verified caller) and `at`. `via` is `console`,
`cli` or `platform_admin`. Refusals are not audited. They are log lines, as
every switch's report-only line already is, and
`swarm_api_role_refusals_total{code,enforced}` counts them.

### 4.11 Settings › Profile

PROFILE-DESIGN-B shows the derived role behind one seam. This design's build
replaces what is behind the seam, not the seam itself.

* The seam reads `TenantChoice.role` from `GET /v1/tenants/mine` when the
  field is present, and falls back to the derived role when it is absent (an
  older API).
* It shows `role`, `source` and `granted_by`/`granted_at`; a reach entry
  shows "admin (platform admin)".
* The ROLES-UI-GATES lane makes that one change in whichever file the seam
  has landed in.

Until this builds, Profile shows "member". Afterwards it shows "member
(workspace default)" for everyone, until an admin changes something.

### 4.12 Changes by component, and the files each lands in

* **swarm-api**
  * `auth.py`: `AuthContext.platform_reach` and `tenant_via`;
    `Authenticator._platform_reach`, the `_with_own_workspace` early-return
    test, `_select_tenant`'s second list and 503 case, `may_act_in`,
    `holds_platform_admin`.
  * `admins.py`: a `fresh` argument on `holds_admin` that reads past the
    cache.
  * `roles.py` (new): the ladder and its table, `Resolver` (query, verify,
    cache, fresh, the platform-admin and personal rules),
    `sign_grant`/`grant_verifies`, `write_grant` (transaction with its audit
    row), `require(auth, role, *, fresh=False)`, `role_payload`,
    `audit_platform_admin`.
  * `routes/roles.py` (new): §4.3.
  * `deps.py`: the audit hook in `current_auth`; a `workspace_role`
    dependency.
  * `main.py`: mount.
  * `errors.py`: the codes.
  * `refusals.py`: two switches.
  * `settings.py`: `role_cache_seconds` (15) and `platform_admin_reach`
    (off), each beside its value with why.
  * `metrics.py`: two counters.
  * `routes/tenants.py`: `role` and `via` on `/me` and `/mine`, and the gate
    on `/me/credentials`.
  * `schedulefire.py`, `routes/runs.py`: `may_act_in` and the owner's role.
  * Then one gate call per route in the files of §6's gate lanes.
* **scheduler, reconciler, agent-worker:** none.
* **terraform:** `terraform/modules/firestore/indexes.tf` (two indexes).
  `terraform/environments/{dev,prod}/*.tfvars` `api_refusals` and
  `PLATFORM_ADMIN_REACH` when they are turned on, which is an operator change,
  not a build lane.
* **swarm-ui:**
  * `WorkspacePeople.tsx`, `WorkspaceRolesAdmin.tsx` and `roles.ts` (new:
    `atLeast(me, 'member')`);
  * `api.ts` and `types.ts`;
  * `App.tsx` (the `#settings/people` route);
  * `People.tsx` (one card mount and the drawer section);
  * `Spine.tsx` (the switcher's reach section and the banner);
  * then `Approvals.tsx`, `QuestionsCard.tsx`, `Submit.tsx`,
    `Schedules.tsx`, `Repositories.tsx` and the Profile seam.
* **swarm-mcp / plugin:** `server.py` (`swarm_tenants`, `swarm_roles`),
  `sc.py` (`whoami`, `roles`), `client.py`, `plugin/skills/sc/SKILL.md`,
  `plugin/commands/sc.md`.
* **docs:**
  * `docs/workspaces.md` (§6.5 gains "Workspace roles" and the platform-admin
    reach);
  * `docs/security.md` (the grant integrity argument and the reach's
    isolation argument);
  * `docs/design/workspace-secrets.md` §10 (Q4's "until a per-tenant admin
    role exists" is answered);
  * `docs/design/human-steps.md` §4.2 (the role check, and step 3 superseded
    for group workspaces);
  * this file.

### 4.13 Failure cases

| case | what happens |
|---|---|
| Firestore is down at a gate whose switch is off | logged, let through (today's behaviour) |
| Firestore is down at a gate whose switch is on | 503 `role_unavailable`; nothing written. A platform admin in a group workspace is unaffected (no read) |
| Firestore is down when a non-member platform admin acts (or a narrowed-member one, Q12) | 503 `platform_admin_audit_unavailable`; nothing runs |
| Cloud Identity is down | unchanged: tenant resolution already 503s (`auth.py::_from_claims`) before any role is read. An admin-group lookup failure leaves `admin_unresolved`, and a reach request answers 503, never 403 |
| the settings key is missing in an environment | every grant fails verification. The reader treats every person as holding the default, and the default as `member`. Role writes answer 503 `role_signing_unavailable` and write nothing. This fails toward today's behaviour, never toward wider |
| the key rotates | `settings_key_previous` verifies older grants. Grants are append-only, so the next write signs with the new key. Retire the previous key only after every tenant's newest grant postdates the rotation; `GET /v1/admin/roles/person` reports `key` for checking |
| a person leaves the group | not a member. Their override stays, inert, and applies again if they rejoin |
| a platform admin loses the platform role | the reach is gone on their next request (`is_admin` is resolved per request; `admin_roles` caches 15 s). A schedule they own and reach only as an admin fails `may_act_in` at its next firing and auto-pauses `OWNER_NOT_MEMBER`, as today. A grant someone gave them applies again |
| a group is removed from `TENANT_GROUPS` | the workspace disappears from everyone's choices and from every reach. Its grants stay, inert |
| a registered group's slug starts with `u-` | never in any reach; its members reach it as members, behind today's collision check |
| two admins write at once | two grants with different `granted_at`; the later wins, and both are audited |

### 4.14 What this does not do

* It does not let a role admit someone to a workspace, or make a personal
  workspace shareable (Q3).
* It does not let a platform admin into anyone else's personal workspace.
* It does not bind Google groups to roles (Q4, design B).
* It does not let a workspace admin act on the platform: limits, pools,
  providers, other workspaces, `platform: true` repositories.
* It does not give a workspace admin a repository's `platform` or
  `merge_policy`, or the merge-mode switch on a `platform: true` repository
  (Q13 keeps those platform-only).
* It does not change who may answer a human step beyond the role floor and
  Q5's platform admins. Per-step assignees stay declined (`human-steps.md`
  Q2).
* It does not close `tenant-settings-integrity.md`'s wider problem. It only
  makes the role store safe while that problem remains.

---

## 5. Frozen contract: change requests

**None.** Nothing in `apps/common/swarm_common/` has to change, for these
reasons:

* **The ladder and its table live in `swarm_api/roles.py`.** No other service
  decides what a person may do. The scheduler dispatches agents that swarm-api
  already admitted. The reconciler and the worker never see a person.
* **The platform-admin reach lives in `swarm_api/auth.py`.** It adds a list
  beside `tenant_choices` and never touches `Principal.groups`, so the
  frozen `resolve_tenant(principal, group_priority)` is called with exactly
  what it is called with today, and `tenant_id_for_group` /
  `tenant_id_for_user` are read as they are. CONTRACT.md's Cloud Identity
  rule (iterate registered groups, `checkTransitiveMembership`,
  `x-goog-user-project`) is untouched: the reach makes no directory call.
* **The grant MAC is swarm-api's alone, so its canonical form is too.**
  `specsign.py`'s settings-block form (contract request 86) is in the
  contract because the scheduler verifies those blocks. Nothing outside
  swarm-api verifies a grant.
* **A role is not a field of `Tenant`.** Putting it there would make it a
  field the tenant's own worker can rewrite (`models.py`'s RECORD ONLY
  fields, contract request C of `tenant-settings-integrity.md`).
* **The audit rows need no task field.** `platform_admin_acted` names the
  route and its path ids; a `submitted_via` on the frozen `Task` was
  considered and is not needed.

**Considered and not requested:**

* a `WorkspaceRole` enum in `models.py`. The plugin and UI read role names
  as strings from the API, and the API publishes the ladder (`roles` in
  §4.3), so nothing outside swarm-api imports it;
* a `role` field on `Principal`. `Principal` is about the caller, and a role
  is per (caller, tenant), so the field would be wrong after
  `_select_tenant`.

---

## 6. Build plan

Each lane owns the files named in its row, and **no file appears in two
lanes' rows**. Every lane:

* pushes its tests alone to the PR branch first, and its fix to
  `<branch>-fix`;
* runs its area's unit tests and `scripts/changed-guards.sh` before
  finishing.

**Security review.** The CLAUDE.md review (credentials, tenant isolation,
IAM) applies to ROLES-REACH (tenant isolation: it changes who may enter a
tenant), ROLES-API-CORE, every ROLES-GATES lane and ROLES-MCP, because each
decides who may act in a tenant. ROLES-REACH's review is **blocking**: no
lane that depends on it starts until the review has reported no blocker or
major. Lanes stay within ~6 files besides their tests (`live-browser.md` §6),
so the UI is three lanes.

| phase | lane | files | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **ROLES-INDEX** (Track C) | `terraform/modules/firestore/indexes.tf`, `tests/terraform/firestore.tftest.hcl` | the plan has a composite index on `workspace_role_grants` (`tenant_id`, `subject`, `granted_at` DESC) and one on `admin_audit` (`target_tenant_id`, `at` DESC) |
| 1 | **ROLES-REACH** (security review, blocking) | `apps/swarm-api/swarm_api/auth.py`, `admins.py`, `settings.py` (both new settings, `platform_admin_reach` and CORE's `role_cache_seconds`, so CORE need not touch it), `tests/unit/control_plane/test_platform_admin_reach.py` (new), `tests/unit/control_plane/test_tenant_switcher.py` (new cases only) | with `platform_admin_reach` on, against fake directory and settings: **(1) a platform admin selects a group workspace they are not in** (`X-Swarm-Tenant: eng`, Cloud Identity says no for `eng@`): the request succeeds with `tenant_id == "eng"`, `tenant_principal == "eng@saga.xyz"`, `tenant_via == "platform_admin"`, and the directory fake records **no** extra call; **(2) a platform admin is refused any personal workspace but their own**: `X-Swarm-Tenant: u-carol` is 403 `tenant_not_member` with today's message, a registered group `u-carol@example.com` is not in `platform_reach`, and the admin's own `u-` still selects with `tenant_via == ""`; **(3) a non-admin is still membership-checked**: the same `eng` header from a non-admin is 403 `tenant_not_member`, `platform_reach` is empty, and the directory fake records exactly one `checkTransitiveMembership` per registered group with `x-goog-user-project`. Also: a headerless request from a platform admin lands in the same default tenant as before the change; `admin_unresolved` with a reach header is 503, not 403; with the setting off every case above behaves as today; `may_act_in` answers `"platform_admin"` for a group tenant and `""` for a `u-` tenant; `holds_admin(fresh=True)` reads past the cache |
| 2 | **ROLES-API-CORE** (after REACH) | `apps/swarm-api/swarm_api/roles.py` (new), `routes/roles.py` (new), `deps.py`, `main.py`, `errors.py`, `refusals.py`, `metrics.py`, `routes/tenants.py`, `tests/unit/control_plane/test_roles*.py` (new), `tests/unit/control_plane/test_new_refusals_ship_off.py` (only `NOT_SWITCHABLE_CODES` entries with reasons) | an empty grant set resolves every group member to `member` and a personal workspace's person to `admin`; a platform admin resolves to `admin` in a group workspace with no grant read (the fake records no query) and a grant naming them `viewer` changes nothing; a grant with a bad MAC, another tenant's `tenant_id`, or a copied older `granted_at` does not win (a fake Firestore with create/update but no list); a `"*"` grant with role `admin` is skipped; 61 unsigned newer grants give `role_unavailable`, never a wider role; `PUT /person` by a `member` is 403 `role_admin_required` and writes nothing (documents before and after); `PUT /default {admin}` is 409; a `u-` tenant write is 409; a service account is 403 `role_person_required`; a write and its `admin_audit` row commit together (an injected failure leaves neither); no response contains a grant document id (scan every body in the suite); **a non-member platform admin's `POST` writes one `platform_admin_acted` row before the route body runs, with the route template and no body field, and an injected audit failure gives 503 `platform_admin_audit_unavailable` with the route body never called; the first `GET` of an hour writes one `platform_admin_viewed` row (`{route, hour}`, no body) and a second `GET` in the same hour writes no second; a `GET` in the next UTC hour writes another; an injected audit failure on a read gives 503 `platform_admin_audit_unavailable`; a member platform admin whose own role is enough writes none; a member platform admin narrowed to `viewer` who `POST`s a submit writes one `platform_admin_acted` row before the route body runs and reads write the hourly row (the narrowed-member case, Q12), and an injected audit failure there is also 503 with the body never called**; `GET /v1/tenants/mine` carries `role` and `via`; `POST /v1/tenants/me/credentials` by a `member` logs `refusal report-only code=role_admin_required` with the switch off and is 403 with it on; switch on and Firestore failing gives 503 `role_unavailable`, switch off lets it through |
| 3 | **ROLES-GATES-WORK** (after CORE) | `routes/tasks.py`, `routes/workflows.py`, `routes/runs.py`, `routes/live.py`, `routes/browser_logins.py`, `agent_questions.py`, their tests | with the switch on: a `viewer` is 403 `role_member_required` on `POST /v1/tasks`, `/batch`, `/workflows`, `/runs`, and nothing is written (count agents before and after); a `viewer` cannot cancel their own agent or another's; a `member` cancels another's; `run_owner_auth` fails a run whose creator was narrowed to `viewer`, and advances one whose creator is a non-member platform admin with a `platform_admin_acted` row; with the switch off, all of the above go through and log the code |
| 3 | **ROLES-GATES-DECIDE** (after CORE) | `routes/approvals.py`, `approvals.py`, `ghapprove.py`, the human-step answer route's file if HW-API-ANSWER has merged, their tests | with the switch on, a `viewer` is 403 `role_member_required` on `:approve`, `:reject`, `plan:approve`, and the item stays pending; a `member` approves; a non-member platform admin approves through the reach, and a gate `approvers` list that does not name them still refuses them; a gate `approvers` list naming a `viewer` still refuses them; the decision reads the role fresh (a cache primed with `member`, a grant to `viewer`, then approve: refused); a `/swarmcloud approve` comment from a `viewer` is refused with the code in the reply |
| 3 | **ROLES-GATES-AUTOMATE** (after CORE) | `routes/schedules.py`, `schedulefire.py`, `routes/repositories.py`, `routes/access.py`, `routes/accounts.py`, their tests | with the switch on, a `viewer` is 403 `role_member_required` on schedule create/delete/run, repository register/delete, an access grant and an account delete, and a `member` is not; a `member` who PATCHes a repository's `context` is 403 `role_admin_required` (Q13) and a workspace `admin` is not, while `platform` and `merge_policy` stay refused to an `admin` who is not a platform admin; a firing whose owner was narrowed to `viewer` is skipped and the schedule auto-paused `OWNER_ROLE_NARROWED`, with a `schedule_audit` row; a firing whose owner is a non-member platform admin fires, with a `platform_admin_acted` row, and pauses `OWNER_NOT_MEMBER` once they lose the platform role; with the switch off, it fires and logs |
| 3 | **ROLES-GATES-ADMIN** (after CORE) | `workspace_secrets.py`, `mcp_servers.py`, `routes/provider_keys.py`, `gittokens.py`, `mergesettings.py`, `codeintel.py`, `judgment.py`, their tests | a group workspace `admin` (not a platform admin) creates a secret, registers an MCP server and sets the tenant provider key, and `can_manage` is true; a non-member platform admin can too, with a `platform_admin_acted` row; a `member` is refused `role_admin_required` with the switch on and logged with it off; a personal workspace's person still manages theirs and nobody else can select it; **(Q13) a group workspace `admin` (not a platform admin) changes merge settings, code intelligence and the judgment backend and each write is stored; a `member` is refused each (`role_admin_required` for code intelligence and the judgment backend, `merge_settings_owner_required` for merge settings) and nothing is written; a non-member platform admin changes them with a `platform_admin_acted` row; a personal workspace's person still changes theirs** |
| 4 | **ROLES-MCP** (after CORE) | `apps/swarm-mcp/swarm_mcp/server.py`, `sc.py`, `client.py`, `plugin/skills/sc/SKILL.md`, `plugin/commands/sc.md`, `tests/unit/mcp/` files for each | `swarm_tenants` output carries each workspace's role and `via`; `swarm_roles` returns the roster and calls only `GET /v1/workspace/roles` (a fake transport records one GET); `sc roles set` without the typed confirmation, or with it supplied on stdin, sends nothing (a fake transport records no request); no MCP tool's schema or handler reaches a role-write route (scan the registered tools and the client calls they make) |
| 4 | **ROLES-UI-PEOPLE** (after CORE) | `apps/swarm-ui/src/WorkspacePeople.tsx` (new), `roles.ts` (new), `api.ts`, `types.ts`, `App.tsx`, their `__tests__` | `#settings/people` renders the roster and events for a `viewer` with no controls, and with controls for an `admin`; the default selector offers `viewer` and `member` only; a personal workspace shows the fixed-admin line |
| 5 | **ROLES-UI-ADMIN** (after UI-PEOPLE) | `WorkspaceRolesAdmin.tsx` (new), `People.tsx`, `Spine.tsx`, their `__tests__` | Admin › People shows the Workspace roles card with "Name an admin" and the drawer's roles; a platform admin's switcher lists reach workspaces under their own heading, and selecting one shows the platform-admin banner; a non-admin's switcher is unchanged |
| 5 | **ROLES-UI-GATES** (after UI-PEOPLE) | `Approvals.tsx`, `QuestionsCard.tsx`, `Submit.tsx`, `Schedules.tsx`, `Repositories.tsx`, the Profile seam's file, their `__tests__` | a `viewer` sees Approve/Reject disabled with the role sentence; Submit and Cancel are disabled for a `viewer`; Profile shows the stored role and its source, "admin (platform admin)" for a reach entry, and the derived role when `role` is absent |
| each | **ROLES-DOCS** | `docs/workspaces.md`, `docs/security.md`, `docs/design/workspace-secrets.md`, `docs/design/human-steps.md`, this file | `test_docs_describe_what_was_built`, where it reads these files |
| 6 | **ROLES-ACCEPT** (after GATES, and the dev switches and reach on) | one probe, its workflow wiring (built as `scripts/acceptance/groups/roles.sh` in accept.yml's smoke job, §8) | with a dev acceptance identity granted `viewer` in a test workspace: `POST /v1/tasks` is 403 `role_member_required`, `GET /v1/tasks` is 200, and the grant and its revocation each appear in `GET /v1/workspace/roles` `audit`; with a dev platform-admin identity outside the test workspace's group: selecting it succeeds, a write appears as `platform_admin_acted`, and selecting another identity's `u-` workspace is 403 `tenant_not_member` |

Order and dependencies:

1. ROLES-INDEX and ROLES-REACH, in parallel. REACH's security review must
   report no blocker or major before step 2.
2. ROLES-API-CORE (it reads `tenant_via` and `may_act_in`).
3. The four GATES lanes, in parallel. No two share a file: `routes/runs.py`
   is WORK's, `schedulefire.py` is AUTOMATE's, `auth.py` is REACH's.
4. ROLES-MCP and ROLES-UI-PEOPLE, in parallel; then ROLES-UI-ADMIN and
   ROLES-UI-GATES, in parallel.
5. The operator turns on `PLATFORM_ADMIN_REACH` and the two switches in dev
   (`dev.tfvars`), then ROLES-ACCEPT goes green.
6. A week of report-only lines in prod, per code. Then each switch, and the
   reach, go on in prod.

The change touches admission's front door (submissions) and tenant
resolution, so step 5 also runs `make smoke concurrency-test race-test`
against dev. That is the operator's step, run in CI.

---

## 7. Owner questions

### 7.1 Answered on 2026-10-18 (Q1 to Q11 in PR 1526's comments; Q12 and Q13 afterwards)

| # | question | answer | where it lands |
|---|---|---|---|
| Q1 | the role set | **three roles, viewer < member < admin** (departs from the five-role recommendation); member = today's member exactly; admin = member + Q6 + roles (widened by Q13 to include the workspace's settings) | §3.3, §4.1, §4.9 |
| Q2 | who names a group workspace's first admin | a platform admin, in Admin › People, audited | §4.3, §4.6 |
| Q3 | may a grant admit, or share a personal workspace | no; Google owns membership; a grant to a non-member is stored and applies when they join (`member: false`); a personal workspace's person is its fixed admin | §4.5, §4.6 |
| Q4 | Google groups bound to roles | not in this build; one default per workspace plus per-person overrides | §3.2 |
| Q5 | a platform admin in a workspace | **admin in every group workspace, member or not, no grant row** (departs from the recommendation); not in personal workspaces; tenant resolution lets them select any registered group workspace; every action in a non-member workspace audited `via=platform_admin` | §4.2 |
| Q6 | may a workspace admin manage secrets, MCP servers and tenant keys | yes | §4.1, §4.8 |
| Q7 | who sees the roster and role events | every member, any role | §4.3, §4.8 |
| Q8 | who may cancel someone else's work | member or above; viewers cancel nothing | §4.1, §4.8 |
| Q9 | ship before tenant-settings-integrity design B | yes, with signed, append-only grants under unguessable ids | §4.4 |
| Q10 | turning refusals on | one switch per code, off at ship; dev first; a week of report-only lines in prod; then prod | §4.7, §4.9 |
| Q11 | granting from the plugin | `sc roles` with a typed confirmation, and a read-only MCP tool; no MCP write tool | §4.8 |
| Q12 | what the platform-admin audit records for reads, and for a platform admin who is also a narrowed member | **(a)**: every write by a platform admin in a group workspace they are not a member of is an `admin_audit` row written before the route runs, and the request is refused if the row cannot be written (fail closed); reads write one `platform_admin_viewed` row per (admin, workspace, UTC hour); a member platform admin who holds a role below what an action needs, and passes only as a platform admin, gets the same rows (answered 2026-10-18) | §4.2.1, §4.2.4, §4.10, §6 |
| Q13 | whether workspace settings belong to a workspace admin | **(a)**, which changes Q1's definition: a group workspace's admin also changes its merge settings, code intelligence, judgment backend and a repository's `context`; admin = member + Q6's powers + the workspace's settings + granting and revoking roles. A repository's `platform` and `merge_policy` and the merge-mode switch on a `platform: true` repository stay platform-only (answered 2026-10-18) | §4.1, §4.8, §4.9, §6 |

### 7.2 Left open

Nothing. Q12 and Q13, the two questions the first eleven answers left open,
were answered on 2026-10-18 and are in §7.1.

---

## 8. What was built

Read 2026-10-19 at `24b0533` by ROLES-DOCS. Each entry says what the lane
landed and, after it, where the code departs from §4 and §6. Where they
differ, the code is what runs and this section is what to read.

**ROLES-INDEX (phase 0, Track C).** The two composite indexes of §4.4,
`terraform/modules/firestore/indexes.tf` (`"workspace-role-grants-tenant-subject-granted" = {`)
and `terraform/modules/firestore/indexes.tf` (`"admin-audit-target-tenant-at" = {`),
held by `tests/terraform/firestore.tftest.hcl`. No departure.

**ROLES-REACH (phase 1, security review, blocking).** `AuthContext.platform_reach`
and `AuthContext.tenant_via`, built by
`apps/swarm-api/swarm_api/auth.py::Authenticator._platform_reach` and chosen
by `apps/swarm-api/swarm_api/auth.py::Authenticator._select_tenant`, which
tries memberships first, then the reach, then 503 for an unresolved admin
lookup, then `tenant_not_member`. Every `u-` id is dropped in
`apps/swarm-api/swarm_api/auth.py::Authenticator._reachable_groups`, so
another person's personal workspace is a 403 on or off.
`apps/swarm-api/swarm_api/auth.py::Authenticator.may_act_in` answers the
three stored-owner paths of §4.2.3. The switch is
`apps/swarm-api/swarm_api/settings.py::ApiSettings.platform_admin_reach`,
off by default with its reason beside it, read from `PLATFORM_ADMIN_REACH`.
Departures: `may_act_in` takes `fresh` and passes it to the membership
check; it also answers `""` for the schedule tick, the sweepers and a
listed service account (`_reach_excluded`), which §4.2.1 says get no reach
but §4.2.3 did not list. **The reach is on in dev only:**
`var.platform_admin_reach` renders `PLATFORM_ADMIN_REACH` into swarm-api,
`true` in `dev.tfvars` and the default `false` in prod (ROLES-ON-DEV,
2026-10-19; §4.9's amendment).

**WK-REACH (owner, 2026-10-19).** The reach also covers group workspaces
the workspace job made (#847), not only `TENANT_GROUPS`:
`apps/swarm-api/swarm_api/auth.py::Authenticator._tenant_groups` appends the
registry's `ready` job-made groups after the configured ones, and `may_act_in`
checks the same list through `_registered_reach`. Personal and service
workspaces are still never reached.

**ROLES-API-CORE (phase 2).** `apps/swarm-api/swarm_api/roles.py` holds the table
(`ROLES`, `RANK`, `SUMMARIES`) and the store,
`apps/swarm-api/swarm_api/roles.py::RoleStore`. A grant is signed under
`apps/swarm-api/swarm_api/roles.py::_MAC_DOMAIN`,
`swarm-workspace-role-grant/v1`, with the settings key. A read takes the
newest grant that verifies among the newest 60 grants for that subject and
gives `role_unavailable` past them. A person is cached for
`role_cache_seconds`, 15 s. Decisions read fresh. The routes are
`apps/swarm-api/swarm_api/routes/roles.py`: the four workspace routes and
the five admin routes of §4.3. `GET /v1/tenants/me` and `/mine` carry `role`
and `via` (`apps/swarm-api/swarm_api/routes/tenants.py::get_mine`), and
`apps/swarm-api/swarm_api/routes/tenants.py::put_credential` gained its
`admin` gate. The platform-admin audit runs in
`apps/swarm-api/swarm_api/deps.py::current_auth` through
`apps/swarm-api/swarm_api/roles.py::RoleStore.audit_platform_admin`, before
the limiter, and fails closed with `platform_admin_audit_unavailable`. The
ten codes of §4.9 are `errors.py` classes. `role_member_required` and
`role_admin_required` are the two entries in `refusals.SWITCHES`, both off,
read from `REFUSAL_ROLE_MEMBER_REQUIRED` and `REFUSAL_ROLE_ADMIN_REQUIRED`.
The five new-route codes are in `NOT_SWITCHABLE_CODES`. The counters are
`swarm_api_role_grant_invalid_total` and `swarm_api_role_refusals_total`.
Departures:

* **Names.** §6's `Resolver`, `write_grant` and `role_payload` are
  `RoleStore`, `RoleStore.write` and `workspace_payload`/`admin_payload`.
  There is no `workspace_role` dependency in `deps.py`: each gate calls
  `RoleStore.require` itself.
* **A batch, not a transaction.** A grant and its `admin_audit` row commit
  in one Firestore batch
  (`apps/swarm-api/swarm_api/roles.py::RoleStore.write`). A batch is atomic,
  which is the property §6 asked for. A transaction would add a read the
  write does not need.
* **One query per subject, not pages over `subject in (E, "*")`.** The read
  queries the person and the default separately, `subject ==`, each limited
  to 60 grants (`SCAN_LIMIT`, `PAGE_SIZE × PAGES`), newest first. The bound
  and the `role_unavailable` answer past it are §4.4's.
* **`role_grant_required` was not needed.** The role write routes raise
  `role_admin_required` with `raise`
  (`apps/swarm-api/swarm_api/routes/roles.py::_workspace_writer`), and the
  guard's collector told the two call sites apart.
* **A gap, found by this lane, not fixed by it (out of its files).**
  `apps/swarm-api/swarm_api/roles.py::registered_group_tenants` walks
  `TENANT_GROUPS` only, not the registry. So for a group workspace the
  workspace job made, which WK-REACH lets a platform admin select, the admin
  routes `/v1/admin/workspaces/{tenant_id}/roles*` answer 404,
  `GET /v1/admin/roles/person` leaves it out, and
  `RoleStore._target` does not see it as a group workspace when a
  `/v1/workspaces/{tenant_id}/…` path names it, so a non-member platform
  admin's write there through that path writes no `platform_admin_acted`
  row. A platform admin can still name its first admin by selecting it and
  using the workspace's own routes, which are audited. Its docstring's "the
  walk `Authenticator._reachable_groups` makes" is true only of Terraform's
  groups.

**WORKSPACE-ADMIN-FIELDS (2026-10-25, part of #2157; config-e.html S6c).**
`POST /v1/admin/workspaces` takes an optional `first_admin`, a platform
admin's only (`admin_auth`, as the rest of the body). It is checked before
anything is created: a group workspace only, one person's address through
`roles.subject_for_write`, so a service account is `role_person_required`
and anything else malformed is 422 with no record written. After the record
is filed, `apps/swarm-api/swarm_api/routes/people.py::name_first_admin` writes
the ordinary signed override with `RoleStore.write`: `admin`, `via:
platform_admin`, its `workspace_role_granted` row carrying `first_admin:
true`. The tenant id is the stored record's, derived from the group's address,
so the grant lands in that workspace and in no other. Membership stays the
group's. A person not yet in the group still gets the grant, the response
says `member: false`, and the console says the role takes effect once they
are in the group: tenant resolution still checks membership per registered
group, and a grant never admits anyone (§4.6). Three departures from a plain
role write:

* **Only while the workspace is being created.** For a record that is already
  `ready`, `denied` or `failed`, nothing is written and the answer is
  `granted: false, reason: workspace_exists`. Its admins are then People and
  roles' to change, so `first_admin` is never a second door to an
  established workspace.
* **Idempotent.** A repeated request finds the grant standing and writes
  nothing.
* **A failed grant does not undo the record.** The answer is `granted:
  false` with the refusal's code. Repeating the request while the workspace
  is still being created writes the grant.

The same lane added `PUT /v1/workspaces/{tenant_id}/name`
(`apps/swarm-api/swarm_api/routes/workspace_name.py`). It is an admin-level
write in `ADMIN_WRITES` for the workspace's admins and platform admins,
asked of the verified membership and never of the tenant document, and it
writes a `workspace_renamed` audit row. It changes the display name only.

**ROLES-ADMIN-LIST (owner, 2026-10-19).** Admin › People's Workspace roles
card reads `GET /v1/admin/workspaces/roles`,
`apps/swarm-api/swarm_api/routes/admin.py::list_workspace_roles`, a page of
up to 30 workspaces with each one's default, admins and override count.
`apps/swarm-api/swarm_api/routes/admin.py::_role_workspaces` lists Terraform
groups and job-made ones, each with `registered_by`. It is the one role route
that reads the registry; see the gap above.

**ROLES-GATES-WORK (phase 3).** Submit, batch, workflows, runs and cancel (anyone's,
the caller's own included, Q8), answers to an agent's question, human-ready,
live control and browser logins call
`apps/swarm-api/swarm_api/routes/tasks.py::require_member`.
`apps/swarm-api/swarm_api/routes/runs.py::run_owner_auth` asks `may_act_in`,
then the creator's role fresh; a platform admin passing it writes a
`platform_admin_acted` row. Departures: returning live control is ungated,
because handing control back only narrows what a person holds
(`apps/swarm-api/swarm_api/routes/live.py::return_control`); the row's route
is `issue_run_auto_step`
(`apps/swarm-api/swarm_api/routes/runs.py::RUN_OWNER_ROUTE`), not §4.2.4's
`issue_run_advance`.

**ROLES-GATES-DECIDE (phase 3).** Approve, reject, plan approve, edit and reject, and
a schedule's merge-mode switch call
`apps/swarm-api/swarm_api/approvals.py::require_decider`, which reads the
role fresh. A `/swarmcloud approve` comment from a `viewer` gets a reply
naming the code (`apps/swarm-api/swarm_api/ghapprove.py::_require_member`).
The human-step answer route had merged by then, so this lane gated it too:
`apps/swarm-api/swarm_api/awaitinput.py::member_refusal` asks `may_act_in`
fresh, then `member` fresh, held by
`tests/unit/control_plane/test_roles_gates_answer.py`. `routes/approvals.py`'s
docstring now says a platform admin decides in a group workspace as its
admin, which §4.2.3 asked for.

**ROLES-GATES-AUTOMATE (phase 3).** Schedule, repository, access and account writes
need `member`. A firing whose owner holds `viewer` is skipped and paused
`OWNER_ROLE_NARROWED`
(`apps/swarm-api/swarm_api/schedulefire.py::PAUSE_COPY`, §4.8's copy word for
word), and a non-member platform admin's firing writes a row with route
`schedule_firing`. A repository's `context` needs `admin`
(`apps/swarm-api/swarm_api/routes/repositories.py::_context_by`). Departure:
`readable_by_tasks` stays platform-only beside `platform` and
`merge_policy`, which §4.1's list did not name; it is a platform field of
the same kind.

**ROLES-GATES-ADMIN (phase 3).** A group workspace's secrets and MCP servers are its
`admin`'s:
`apps/swarm-api/swarm_api/workspace_secrets.py::require_manager` lets
`workspace_admin` through (read fresh), then a platform admin. Tenant
provider keys
(`apps/swarm-api/swarm_api/routes/provider_keys.py::_require_tenant_admin`),
git-token slots (`apps/swarm-api/swarm_api/routes/gittokens.py::_require_admin`),
merge settings (`apps/swarm-api/swarm_api/mergesettings.py::authorize`), code
intelligence (`apps/swarm-api/swarm_api/codeintel.py::require_owner_or_admin`)
and the judgment backend
(`apps/swarm-api/swarm_api/routes/judgment.py::put_judgment_backend`) do the
same. Departures: the MCP server gate is in `routes/mcp_servers.py`, which
calls the secrets module's `require_manager`; the git-token and judgment
gates are in their `routes/` modules, not the modules §4.1 names; and with
`role_admin_required` off, a `member` still meets the code each route raised
before roles, such as `code_intelligence_owner_required`. That is right
while the switch is off: a refusal that existed before roles is not a new
refusal.

**ROLES-MCP (phase 4).** `swarm_roles` is read-only and makes one
`GET /v1/workspace/roles`
(`apps/swarm-mcp/swarm_mcp/server.py` (`"name": "swarm_roles",`),
`apps/swarm-mcp/swarm_mcp/sc.py::workspace_roles`). `sc roles set|clear|default`
reads its confirmation from `/dev/tty`
(`apps/swarm-mcp/swarm_mcp/sc.py::_ask_terminal`), ignores
`SWARM_ASSUME_YES` and sends `via: "cli"`
(`apps/swarm-mcp/swarm_mcp/sc.py::_send_role_change`). `sc whoami` and
`swarm_tenants` show each workspace's role and `via`. No tool writes a role.
Departure: the typed phrase is `<email> <role>` for `set`, `<email> default`
for `clear` and `default <role>` for `default`, not always an email and a
role.

**ROLES-UI-PEOPLE (phase 4).** `#settings/people`, Config › People and roles
since CONFIG-NAV (`apps/swarm-ui/src/roles.ts`
(`export const WORKSPACE_PEOPLE_ADDRESS = 'config/people'`), CONFIG-ADMIN-POLISH),
in `apps/swarm-ui/src/WorkspacePeople.tsx`: the roster, what each role
permits and the recent events for every role, controls for an `admin`, a
default selector offering `viewer` and `member` only, and the
personal-workspace line.

**ROLES-UI-ADMIN (phase 5).** Admin › People's Workspace roles card with "Name an
admin" (`apps/swarm-ui/src/WorkspaceRolesAdmin.tsx`) and the drawer's roles;
the switcher's reach heading
(`apps/swarm-ui/src/Spine.tsx` (`export const REACH_HEADING = 'Other workspaces (platform admin)'`))
and the platform-admin banner, whose sentence is split into a title and a
body.

**ROLES-UI-GATES (phase 5).** One hook draws the role sentence for every gated
control, `useRoleGate` in `apps/swarm-ui/src/Profile.tsx`, used by
Approvals, Questions, Submit, Schedules, Repositories and Cancel. Profile
shows the stored role and its source, "admin (platform admin)" for a reach
entry, and the derived role when `role` is absent. Departures: Cancel's gate
is in `apps/swarm-ui/src/StopRun.tsx`, outside the lane's row; the sentence
names the tenant id, not a display name; Profile shows no `granted_by` or
`granted_at` (§4.11).

**ROLES-DOCS (each phase).** `docs/workspaces.md` (§6.4's Workspace roles
card, §6.5's platform-admin reach), `docs/security.md` ("Workspace roles and
a platform admin's reach", and the reach in "Authentication"),
`docs/design/workspace-secrets.md` (Q4, superseded by Q6),
`docs/design/human-steps.md` (§4.2 step 3, superseded by Q5 for group
workspaces) and this section, held by
`tests/unit/scripts/test_docs_describe_what_was_built.py`.

**ROLES-ACCEPT (not built on 2026-10-19; built on 2026-10-28, lane
accept-roles-and-labels, #1518).** The probe is
`scripts/acceptance/groups/roles.sh`, run by
`.github/workflows/accept.yml`'s smoke job as `verify-remote.sh
acceptance/groups/roles` (BGD, 2026-10-28: the smoke job, not a new
acceptance group or the tenant-isolation job). It acts as two identities that
are not swarm-verify's own, each through an impersonated ID token
(`acc_api_as` in `scripts/acceptance/lib.sh`), in a group workspace of its
own, and checks five things: a viewer's `POST /v1/tasks` is 403
`role_member_required` and creates nothing; its `GET /v1/tasks` is 200; the
viewer grant and its revocation, matched by a reason naming the run, are both
in `GET /v1/workspace/roles` `audit`; the platform admin, outside the
workspace's group, selects it and its write is recorded
`platform_admin_acted`; and that admin selecting another identity's `u-`
workspace is 403 `tenant_not_member`. Its judge reads only the status and the
API's code or audit action, and
`tests/unit/scripts/test_acceptance_roles.py` holds it to `swarm_api`'s own
codes. Departures from §6 row 6:

* **The grant is the workspace's default role, not a person's.** The planned
  viewer is the QA service account (swarm-qa, #1649), and a role write naming
  a service account is 403 `role_person_required` (§4.6). The workspace
  default is the one grant that reaches it, which is why the probe narrows a
  workspace of its own and refuses to run in `smoke`, where every other
  acceptance group submits.
* **It is a shell probe under `scripts/acceptance/`, not a file under
  `tests/acceptance/`,** because the suites in the swarm-verify job are shell;
  `test_docs_describe_what_was_built.py` still looks under `tests/acceptance/`
  for the code.
* **It SKIPs until the owner provisions it**, naming what is missing: a group
  workspace for it (repository variable `ROLES_TENANT`), the viewer identity
  in that workspace's group (`ROLES_VIEWER_SA`), a platform-admin identity
  outside it (`ROLES_ADMIN_SA`), another identity's personal workspace
  (`ROLES_OTHER_WORKSPACE`), and roles/iam.serviceAccountTokenCreator for
  swarm-verify on both identities. A skip is never a pass; #1518's last
  criterion is met when the probe first passes live.

**What is left, in order.** The owner provisions ROLES-ACCEPT's identities
and workspace, and the probe passes live; a named workspace admin in every
group workspace that manages secrets (eng, smoke), then
`role_admin_required` on in dev; the registry gap above; a week of
report-only lines in prod; then prod.
