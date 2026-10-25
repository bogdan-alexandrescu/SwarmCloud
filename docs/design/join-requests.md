# Join requests: a person asks to join a group workspace, and its admin approves or denies it

**Status: designed 2026-10-25 (lane JOIN-REQUESTS-DESIGN, part of #2325).
Nothing is built.** BGD asked for this on 2026-10-25, after deciding
ADMIN-PEOPLE-ROLES' question "Later lane" (recorded on
task_31ecaec218d1408a9c8d). The design is what mock-up
`docs/web-ui/mockups/config-e.html` frame **S5f** draws: a workspace admin's
Admin › People and roles leads with "1 person is waiting to join Example
Labs", and the **Waiting for you** card has the row "lee@example.com · to join
Example Labs · 2 h ago · Approve / Deny".

§7 lists the owner's questions, approval model first. They are also in the
lane's `questions.json`. **Q1 (what approval does) decides most of this
document.** §4 is written for the recommended answer, (A). §3 says what
changes under (B) or (C).

Every file and symbol cited below was read on 2026-10-25 at `6b724c7`. Lines
move, so each citation names the symbol; search for it.

What the design settles, in one line each:

* **A request is a signed, append-only Firestore document,
  `join_requests/{auto-id}`.** A signed-in person makes it for one group
  workspace. It is open for 14 days at most, and it admits nobody by itself
  (§4.1).
* **A person finds a workspace through a join link its admin shares**, never
  through a list of workspaces. The link carries a per-workspace code that is
  off until an admin turns it on, can be rotated, and is stored only as a
  hash. An unknown, rotated or switched-off code gets the same answer, so a
  code that does not work reveals nothing (§4.2, Q2).
* **Approval writes a platform-side membership beside the Google group
  (recommended, Q1 (A)).** It is a signed, append-only
  `workspace_memberships` grant. Tenant resolution offers that workspace to
  the person after the groups that Cloud Identity confirmed, and the
  stored-person re-checks accept it. Nobody edits a Google group, and the
  platform gets no Cloud Identity write (§4.4, §4.5).
* **(A) partly reverses an owner answer.** In `workspace-roles.md` Q3,
  "membership still comes from Google" becomes "from Google **or** an approved
  request". "A role never admits" still holds, because the membership admits
  and the role only limits. That is why Q1 is the owner's to decide.
* **Who decides:** that workspace's `admin`s and platform admins. They decide
  from Admin › People and roles (the lead, the Waiting for you card and the
  grid's `requested` cell) and from the bell. Every request, decision and
  removal writes an `admin_audit` row in the same batch as the change it
  records (§4.6, §4.7).
* **No frozen-contract change is needed** under (A), (B) or (C) (§5).
* **Eight build lanes** whose files do not overlap (§6).

---

## 1. The problem, as measured

### 1.1 Membership of a group workspace is its Google group, and nothing else

`auth.py` builds a caller's `tenant_choices` in three steps:

* `Authenticator._tenant_choices` adds every registered group that Cloud
  Identity confirmed, through `checkTransitiveMembership`, one group at a
  time, with `x-goog-user-project`;
* `_with_own_workspace` adds the caller's own ready personal workspace;
* `_with_own_service_tenant` adds a service account's own service tenant.

`X-Swarm-Tenant` selects only from that list (`_select_tenant`: "THE HEADER
SELECTS; IT NEVER GRANTS"). The stored-person re-checks go through
`Authenticator.is_tenant_member` and `may_act_in`, which ask the same one
group. Those re-checks are a schedule's firing (`schedulefire.py::member_auth`),
an issue run's auto step (`routes/runs.py::run_owner_auth`) and a human-step
answer.

A platform admin's reach (`platform_reach`, `workspace-roles.md` §4.2.2) is
the one exception, and it admits nobody who is not a platform admin. So
**today the only way into a group workspace is to be added to its Google
group**, by someone with rights over that group in the Admin console or
`gcloud identity groups memberships add`.

### 1.2 The platform cannot write a group, and cannot list one

* **No write.** swarm-api reads groups through domain-wide delegation. The
  scope is `cloud-identity.groups.readonly`, authorised in the Admin console
  against swarm-api's OAuth client id (`groups.py`, the comment above the
  delegation branch; `settings.py::groups_impersonate_user`;
  `terraform/modules/iam/bindings.tf` `api_signs_as_itself`).
* **No list.** `groups/-/memberships:searchTransitiveGroups` 403s in this
  project (`docs/multi-tenancy.md` §2), so the platform can never enumerate a
  group's members. The People grid's "in it" is therefore the last
  sighting's `teams` (`people.py::role_cell`).

### 1.3 The `requested` cell and the Waiting for you card serve another request

The `requested` state that Admin › People shows today belongs to a person's
own **personal** workspace record: `workspaces/u-<you>` in `requested`
(`docs/multi-tenancy.md` §7.1). A Claude-account loan request is also shown
there. Both are a platform admin's to decide:

* `People.in_workspaces` says "Nobody is waiting for a workspace admin here"
  and returns `pending: 0`;
* `notifications.py::admin_requests` reads nothing for a non-admin.

A workspace admin therefore has nothing to answer today. The mock-up's S5f
row "to join Example Labs" is a request that does not exist.

### 1.4 What "done" lets someone do that they cannot today

From #2325, "How would you know it worked":

1. A person outside a group workspace asks to join it.
2. That workspace's admin sees the request in the page's lead and decides it.
3. The decision takes effect **without anyone editing a Google group by
   hand**, or the design says plainly why it must.

Under (A), point 3 holds. Under (C), the design says why it cannot (§3.3).

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **`searchTransitiveGroups` 403s here, and every Cloud Identity call needs
  `x-goog-user-project`** (CLAUDE.md, `docs/multi-tenancy.md` §2). Nothing
  here may list a caller's groups or a group's members.
* **Some registered groups carry GCP IAM on this shared project.**
  `eng@saga.xyz` holds project-wide admin roles on `saga-agents-staging`
  (`docs/ci.md`, "Admitting the fixer through `swarm-eng-bots@saga.xyz`"). So
  adding a person to such a group gives them that IAM as well, in the project
  that holds another team's live cluster (CLAUDE.md rule 2). The admin groups
  (`ADMIN_GROUPS`) are Google groups too.
* **A tenant worker can get, create and update Firestore documents, without
  list** (`workspace-roles.md` §2.1, `tenant-settings-integrity.md`). Any
  record that admits a person must therefore be one the worker cannot forge,
  replay or overwrite. That is the reason `workspace_role_grants` is signed
  and append-only, with unguessable ids (`workspace-roles.md` §4.4).
* **The frozen slug.** `tenant_id_for_group` keeps the local part only, so
  two groups can share an id. The first group per id in admin order is the
  one the id selects (`_tenant_choices`). A membership must name the tenant
  **and** the group, and must apply only while that group is still the one
  the id selects.
* **A personal workspace holds its person's own secrets and GitHub token.**
  Nobody else may join it, not even a platform admin
  (`workspace-roles.md` §4.5). A service workspace is its service account's
  own.

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

| invariant | how it is kept |
|---|---|
| 1, 2, 3 (demand, capacity, concurrency) | Untouched. A request, a decision and a membership are control-plane records. Admission, leases and pools read none of them. A new member's submissions count against the workspace's `max_active` like any member's |
| 4, 5, 8 (parking, fencing, checkpointing) | Untouched: nothing here runs in a worker |
| 6, 7 (Spot, resources) | Untouched |
| **9 (per-tenant isolation)** | **The one this design touches.** The tenant boundary does not move. A new member acts **in** the workspace, through its GSA, secrets, prefix and namespace, exactly as a group member does, and gains nothing outside it. What changes, under (A), is **who** counts as a member. §4.4 keeps that list unforgeable: the membership is signed by swarm-api, ordered under its MAC, stored under ids nobody serves, never in the `u-` namespace, and valid only while its group is the registered group the id selects |
| 10 (profiles by name) | No route here takes an image, command, resource spec or backend parameter. The request body carries a join code and an optional message. A decision carries a role name from the existing ladder and a reason |

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing new (§5). `identity.resolve_tenant(principal, group_priority)` and
`Principal.groups` are not touched. A membership is a selectable tenant
choice built in swarm-api, the same way `platform_reach` and the caller's
own personal workspace are.

---

## 3. What approval does: three designs

### 3.1 Design A: a platform-side membership beside the Google group (recommended)

Approval writes a signed `workspace_memberships` grant: `(tenant, group,
person) → member`. swarm-api then counts the person as a member of that
workspace in three places, and has no Cloud Identity write anywhere:

* tenant resolution, as a choice after the confirmed groups (§4.5);
* `is_tenant_member` and `may_act_in`, for the stored-person re-checks;
* the People grid.

* **What it costs.**
  * One new signed collection, built on the pattern and key that
    `workspace_role_grants` already uses.
  * One Firestore query per request for a person's memberships, cached for
    15 s like the role read, and read fresh on decision routes.
  * A change to `auth.py` that needs a security review.
  * A remove action, because a Google-group removal no longer removes
    everyone. A person approved through the platform is removed through the
    platform.
* **What it gives.**
  * The decision takes effect within one cache window, and nobody touches a
    group.
  * **It grants no GCP IAM.** A person approved into `eng` reaches eng's
    workspace on the platform. They do not get the project roles
    `eng@saga.xyz` carries on the shared project. Of the three designs, this
    is the only one where a workspace admin's approval cannot widen IAM on
    `saga-agents-staging`.
* **Isolation (invariant 9).**
  * The new attack surface is a forged or replayed membership. §4.4 closes
    it the way roles closed theirs: a worker cannot sign a grant, cannot
    replay an old one past a newer removal, and cannot find a real one to
    overwrite.
  * The worst a worker can do is flood the collection, which denies the
    person their platform membership. That is denial of service and never
    escalation, and the invalid-document counter raises an alert.
  * Group members are unaffected. A platform admin's reach is unaffected.
* **Owner decisions it revisits.** It partly reverses `workspace-roles.md`
  Q3 ("membership still comes from Google"). If it is chosen, that sentence
  and `docs/multi-tenancy.md` §2 are amended (lane JOIN-DOCS, §6).
* **Frozen contract.** No change (§5).

### 3.2 Design B: approval adds the person to the Google group through Cloud Identity

On approval, the platform calls `groups.memberships.create` on the
workspace's group.

* **What it costs.**
  * **An org-level change the platform does not hold.** The domain-wide
    delegation would widen from `cloud-identity.groups.readonly` to
    `cloud-identity.groups`. Only a Workspace super admin can authorise that,
    in the Admin console. The impersonated user (`GROUPS_IMPERSONATE_USER`)
    would have to be an owner or manager of every registered group, or hold
    the Groups Admin role.
  * `searchTransitiveGroups` is a different call and does not block the
    write. But the write needs exactly the org-level grant that the
    per-group design in `docs/multi-tenancy.md` §2 exists to avoid.
* **The blast radius.**
  * swarm-api is the service every user's token reaches. With group write it
    could add anyone to any group its impersonated user manages. That
    includes `eng@saga.xyz`, with its project-wide roles on the shared
    project, and an `ADMIN_GROUPS` group, which would make the person a
    platform admin.
  * One defect in a role check would become IAM on another team's project.
  * Moving the write into the guarded `swarm-workspace-apply` job
    (swarm-api publishes an opaque id; the job holds the delegation) narrows
    who holds the write. It does not narrow what the write does.
* **What it cannot do at all.**
  * It cannot write a group in another organisation, or a group whose
    settings forbid external members.
  * A person the platform added is indistinguishable from one an admin
    added. The audit row says who approved it; the group does not.
* **Isolation (invariant 9).** Membership stays Google's, so tenant
  resolution is unchanged. The risk sits entirely outside the platform, in
  the IAM those groups carry.
* **Frozen contract.** No change.

### 3.3 Design C: record the decision, and tell the admin to change the group

Approval records the decision and tells both sides:

* the requester: "approved; waiting for the group change";
* the admin: the group to change, and the exact command.

The request then reads `approved` until the per-group check
(`is_tenant_member`, fresh) says the person is in the group. It then shows
`joined`, the next time the admin's page or the person's own request list is
read.

* **What it costs.** The least of the three. The request store, the screens
  and the bell are the same as (A). There is no membership collection and no
  change to `auth.py`.
* **What it does not do.** It fails #2325's point 3 by design. Someone still
  edits the group by hand, and the reason is the one §3.2 gives: the
  platform holds no group write, and getting one is an org-level grant with
  that blast radius.
* **Isolation (invariant 9).** Untouched. Nothing new admits anyone.
* **Frozen contract.** No change.

### 3.4 Recommendation: A, built so that C is what it does with the switch off

Choose (A). It is the only design that meets #2325's point 3 without
granting the platform a write over groups that carry IAM on the shared
project.

Build it behind `ApiSettings.platform_memberships` (`PLATFORM_MEMBERSHIPS`),
**off at ship**, with the reason beside the value: it widens who may enter a
tenant. It goes on in dev first, then prod after the acceptance probe, in the
same order as `PLATFORM_ADMIN_REACH`.

While the switch is off, approval behaves as (C):

* it records the decision and shows the admin the group to change;
* nothing admits the person;
* every membership read is skipped.

So the request flow can ship and be used before the owner turns on a single
new way into a tenant. Turning the switch off again is the rollback: anyone
admitted only by a membership loses the workspace on the next request, and
nothing has to be deleted.

---

## 4. The recommended design, in enough detail to build

### 4.1 What a request is

**Who may ask:**

* a **person**: a verified human email, admitted at the front door
  (`ALLOWED_DOMAINS` / `ALLOWED_USERS`);
* who is **not** a service account (`gitidentity.py::is_service_account`);
* who is not the schedule tick, the rollup sweeper, a listed service account
  (`member_scope`, `tenant_member`) or a pool admin acting as one;
* who is **not already a member** of that workspace, by either the fresh
  per-group check or a valid membership.

A platform admin may ask too, but needs nothing: their reach already makes
them admin there. Their request is refused with 409 `join_already_reaches`,
so they do not file requests nobody needs to decide.

**For which workspaces:** registered **group** workspaces only. A registered
group is one of these, each with the first group per id winning, as
`Authenticator.registered_group_workspaces` reads them:

* a `TENANT_GROUPS` entry;
* a `ready` job-made group record.

Never a `u-` id, a personal workspace or a service workspace. A group
workspace accepts requests only while its **join link is on** (§4.2), which
is off by default. So no workspace takes requests until one of its admins
chooses to.

**The document.** Collection `join_requests`. The document id is a Firestore
auto-id (20 random characters), never returned by any route:

```
join_requests/{auto-id}
  ref:          "j-" + 12 lower-case hex     // the handle every route uses
  tenant_id:    "example-labs"
  group:        "labs@example.com"            // the registered group the id selected at request time
  requester:    "lee@example.com"             // lower-cased, from the verified token, never a body
  requested_at: "2026-10-25T06:41:09.512Z"    // swarm-api's clock, as a string, under the MAC
  expires_at:   "2026-11-08T06:41:09.512Z"    // requested_at + 14 days, under the MAC
  message:      "I'm on the labs rota from Monday"   // optional, <= 200 chars, redacted, not under the MAC
  mac:          {v: 1, key: "<key_id>", sig: "<base64 HMAC-SHA256>"}
```

**Decisions are separate, append-only documents in the same collection's
subcollection**:

```
join_requests/{auto-id}/decisions/{auto-id}
  ref, tenant_id, requester         // copied, under the MAC
  state:   "approved" | "denied" | "withdrawn"
  by:      "alex@example.com"         // the deciding admin, or the requester for "withdrawn"
  at:      "<iso>"                    // under the MAC
  via:     "workspace_admin" | "platform_admin" | "requester"
  role:    "viewer" | "member" | null // approved only: the role the admin chose
  reason:  "…"                        // denied: required, <= 200, redacted, shown to the person
  mac:     {...}
```

* **The signature.** The MAC is HMAC-SHA256 under swarm-api's
  `settings_key` (`swarm-settings-key`, with `key_id` and
  `settings_key_previous` for rotation, exactly as `roles.py::sign_grant`
  uses it). Each record has its own domain string, so a role grant or a
  settings block can never verify as either:
  * `"swarm-join-request/v1\n"` for a request;
  * `"swarm-join-decision/v1\n"` for a decision.
* **Why it is signed at all, when a request admits nobody.** A worker that
  can update a request could change `requester` between the ask and the
  approval. The admin would then approve someone they never saw. Approval
  verifies the request's MAC and refuses one that fails it (409
  `join_request_invalid`, logged, counted). The admin's page does not show a
  request that fails its MAC.
* **The state** is derived, never stored on the request:
  * the newest valid decision, by `at`, wins;
  * no valid decision and `expires_at` in the past is `expired`;
  * otherwise it is `requested`.

  A worker that writes a forged "approved" decision has written a document
  that does not verify, and it is skipped. Even a verified approval admits
  nobody: only the membership grant of §4.4 admits, and approval writes it
  in the same batch.

**How long it lives.**

* **Open for 14 days.** After that it reads `expired`. A person may ask
  again, and that creates a new document with a new `ref`.
* **Kept for 90 days after it was decided or expired**, then deleted by the
  existing ten-minute sweep (`POST /v1/admin/workspaces/sweep`). The
  `admin_audit` rows (§4.7) are the permanent record, so pruning a request
  loses nothing an audit needs.

**Rate limits.** Each is checked in swarm-api, in the transaction that
writes the request:

| limit | value | refused with |
|---|---|---|
| open requests per (person, workspace) | 1. A second identical ask is 200 with the open one, and writes nothing | |
| open requests per person, across workspaces | 3 | 429 `join_requests_open_limit` |
| requests per person per day | 10 | 429 `join_requests_daily_limit` |
| after a denial, a new request to the same workspace | not for 7 days | 429 `join_denied_recently`, with the day it opens |
| open requests per workspace | 50 | 429 `join_workspace_queue_full`, shown only to someone holding a working code, so it reveals nothing to anyone else |

Each value is a setting, with the reason beside its default, as every limit
in `settings.py` has.

### 4.2 How a person finds a workspace without learning the others exist (Q2)

**A join link, per workspace, off by default (recommended).** A workspace
admin turns it on from Settings › People and roles. The page then shows a
link of the form `https://<console>/join#<code>`, to share however the team
already shares things.

* **The code.** 128 random bits, url-safe base64, made with
  `secrets.token_urlsafe(16)` when the admin turns the link on or rotates it.
* **Where it is kept.** Only its SHA-256 is stored, in a signed
  `join_links/{tenant_id}` document `{tenant_id, group, code_sha256, on,
  set_by, set_at, mac}`. The code is shown to the admin once, and again only
  by rotating.
* **What happens to a worker that overwrites the document.** The MAC fails,
  the link reads as off, and requests stop (denial of service, never
  admission). It cannot learn a code, because none is stored. It cannot
  forge an `on` link, because it cannot sign one.
* **Lookups.** By hash: `POST /v1/join/lookup {code}` hashes the code and
  queries `code_sha256 ==` (a single-field index).
* **A code that does not work leaks nothing.** An unknown, rotated or
  switched-off code, and a code for a workspace whose group is no longer the
  registered one, all answer the same 404 `join_link_unknown`, with the same
  body and the same work done. So probing codes, at 128 bits, finds nothing
  and learns nothing.
* **What a working code shows.** The workspace's display name, and nothing
  else: no id, no group, no member count, no admin's name. The code is
  rate-limited per caller at the existing limiter's per-caller budget, so a
  guessing loop is throttled as any route's is.
* **The code never travels in a log.** The shared link puts the code in the
  URL fragment (`https://<console>/join#<code>`), which a browser never
  sends. The console reads it there and posts it in a body, so it never
  reaches a request log or a load-balancer access log. That is the rule
  `routes/people.py` keeps for emails, and it is why no route takes the code
  in its path.

Two alternatives, for Q2:

* **(b) The person types the group's address** (`labs@example.com`). The
  answer is the same 202 whether or not that is a registered workspace with
  its link on. A request is stored only when it is, and the person's list
  says "sent, if this workspace takes requests". It needs nothing shared,
  but anyone who knows the address can queue a request for the admin, and
  the person cannot tell a typo from a quiet admin.
* **(c) A directory of "listed" workspaces.** An admin marks a workspace
  listed, and every signed-in person sees it with a Join button. It is the
  easiest to find, and it is exactly the leak #2325 asks to avoid, made
  opt-in.

### 4.3 What approval does

In one Firestore batch, all or nothing:

1. **Re-read.** Read the request fresh and verify its MAC. It must be
   `requested`: not decided, not expired.
2. **Re-check the workspace.** It must still be a registered group
   workspace, with this request's `group` still the group its id selects
   (`_registered_reach`'s check). Otherwise 409 `join_workspace_changed`, and
   nothing is written.
3. **Re-check the decider** (§4.6, read fresh).
4. **Write the decision** `approved`, with the role the admin chose:
   `viewer`, `member`, or the workspace default when none is chosen. `admin`
   is not offered (`join_role_not_allowed`): an admin is named person by
   person, after joining, as `workspace-roles.md` §4.6 already requires.
5. **Under (A), with `platform_memberships` on:** write the
   `workspace_memberships` grant `member` (§4.4). When the chosen role
   differs from the default, also write a `workspace_role_grants` override,
   through `RoleStore.write`, in the same batch.
6. **Write the audit rows** (§4.7).

**With the switch off (C's behaviour)**, step 5 is skipped. The response
carries `admitted: false` and `group_change: {group, command}`, the
`gcloud identity groups memberships add --group-email=<group>
--member-email=<person>` line. The admin's row then reads "approved, waiting
for the group change". It reads `joined` once a fresh per-group check says
the person is in the group.

**A denial** writes the decision `denied`, with a required reason, which is
shown to the person, and its audit row.

**A withdrawal** is the requester's own, on an open request.

### 4.4 Storage: `workspace_memberships`, signed and append-only

```
workspace_memberships/{auto-id}
  tenant_id:   "example-labs"
  group:       "labs@example.com"       // the group the id selected when it was written
  subject:     "lee@example.com"        // lower-cased; never "*", never a service account
  state:       "member" | "removed"
  by:          "alex@example.com"
  at:          "<iso>"                    // swarm-api's clock, under the MAC, strictly after the subject's previous grant here
  via:         "join_request" | "workspace_admin" | "platform_admin"
  reason:      "…"                        // removal: optional, <= 200, redacted; not under the MAC
  mac:         {v: 1, key: "<key_id>", sig: HMAC(settings_key,
                "swarm-workspace-membership/v1\n" + canonical_json({tenant_id, group, subject, state, by, at}))}
```

**The read** is the one `RoleStore.newest_valid` makes, over a different
query:

* Query `subject == E ORDER BY at DESC`, in pages of 20. This is one query
  per person across tenants, because the resolver needs every workspace the
  person joined, not one.
* Per `tenant_id`, take the first grant **that verifies**. It must also pass
  the shape checks:
  * `tenant_id` does not start with `u-`;
  * `subject` is a person;
  * `state` is one of the two values.
* It counts only when `state == "member"` **and** `(tenant_id, group)` is an
  entry of the registered group workspaces now (`_registered_reach`). A
  group that was replaced, or a record that is no longer `ready`, admits
  nobody through an old membership.
* An invalid document is skipped, logged
  (`membership invalid tenant=… reason=mac`) and counted in
  `swarm_api_membership_invalid_total`. That counter is a new alert, the
  same shape as `swarm_api_role_grant_invalid_total`.
* After three pages, the read stops. The tenants it has read are answered;
  for the rest the membership is "unknown" (below).

**What a worker can and cannot do to it** (get, create, update, no list):

* *Forge a membership.* It can create a document, but cannot sign it. The
  reader skips it.
* *Replay an old `member`* after a removal. The copy keeps its old `at`
  under the MAC, so the newer `removed` still wins.
* *Overwrite the current grant.* It needs the random id. No route serves
  that id, no audit row names it, and it cannot be listed. An overwrite would
  also fail the MAC, which drops the person to their previous grant, at
  worst `removed`.
* *Copy one into another tenant.* `tenant_id` and `group` are under the MAC.
* *Flood.* Unsigned documents with a future `at` push the valid one past
  three pages. That person loses their platform membership (the read gives
  up, below) and the counter alerts. It is denial of service, never
  escalation. It cannot touch a Google-group member, and it cannot touch a
  platform admin.

**Why not one document per (tenant, person)?** For the reason
`workspace-roles.md` §4.4 gives: a guessable id lets a worker read the
signed `member` document today and write it back after a removal.

**What a failed read does.** It never stops a group member. They do not
need the read: the read runs after the group choices, and its failure only
withholds the platform memberships.

* A request that names a tenant the person could reach only by membership
  (`X-Swarm-Tenant: example-labs`, with no group match) answers 503
  `membership_unavailable`, never `tenant_not_member`. The unread membership
  could have changed the answer, which is the reasoning `_select_tenant`
  applies to `admin_unresolved`.
* With no header, the person lands on whatever their group choices and
  personal workspace give, and the console's switcher marks the memberships
  "could not be read".

**Cache.** Per instance, per subject, for 15 s, like the role cache. The
decision paths always read fresh:

* approve, reject, plan approve and the human-step answer;
* `may_act_in` for a schedule firing and an issue run's advance;
* every membership write.

**Indexes:**

* `workspace_memberships` (`subject` ASC, `at` DESC);
* `join_requests` (`tenant_id` ASC, `requested_at` DESC);
* `join_requests` (`requester` ASC, `requested_at` DESC);
* `join_links` `code_sha256` single-field (the default single-field index
  serves it).

All of them go in `terraform/modules/firestore/indexes.tf`.

**Removal.** A workspace's admin or a platform admin writes a `removed`
grant (§4.8, `DELETE /v1/workspace/members/person`). It takes effect on the
next request, after the 15 s cache. Any role override the person held stays
stored and inert, as a grant to a non-member is today
(`workspace-roles.md` §4.6).

A removal applies only to the platform membership. When the person is also
in the Google group, the response says so (`still_in_group: true`, from a
fresh per-group check), and so does the grid. The admin then knows the group
is the other half, and that the platform cannot change it.

### 4.5 Tenant resolution: the change, exactly

All of it is in `auth.py`, and all of it is gated by `platform_memberships`:

1. **`AuthContext.platform_memberships: tuple[tuple[str, str], ...] = ()`**
   is a new swarm-api-only field. `Authenticator._platform_memberships(ctx)`
   builds it at the end of `_from_claims`. It is:
   * **empty** unless the switch is on and `_special(ctx)` is false;
   * otherwise `(tenant_id, group)` for each counted membership (§4.4), in
     `at` order;
   * **skipping every id already in `tenant_choices`.** A group member
     reaches it as a group member, and the Google answer wins.
2. **`tenant_choices`** has these entries appended, after the group choices.
   * The caller's own personal workspace stays first. Groups keep their admin
     order. A membership can never displace either.
   * When `tenant_choices` was empty, which means no group and no ready
     personal workspace, the first membership becomes the default:
     `tenant_id` and `tenant_principal` are replaced together from that
     entry, exactly as `_with_own_workspace` replaces the frozen default for
     a personal workspace. That keeps the rule on `tenant_choices`' comment:
     "when it is non-empty its first entry IS the default tenant".
   * A person's chosen default (`preferences.default_workspace`) may name a
     membership entry, as it may any choice.
3. **`TenantChoice.via`** gains `"platform_member"`, beside `"member"`,
   `"personal"` and `"platform_admin"`. `GET /v1/tenants/mine` lists it, and
   `GET /v1/workspace/roles` `me.via` says it.
4. **`_select_tenant`** needs no new step. A membership entry is in
   `tenant_choices`, so the header selects it as it selects any choice. It
   also passes the `ensure_tenant` / `assert_tenant_scope` collision check,
   because `tenant_principal` is the registered group email, as for a group
   member.
5. **`is_tenant_member(email, tenant)`** answers true for a group tenant when
   the per-group check says no **and** a counted membership exists, read
   fresh when `fresh=True`. A failed read raises `UpstreamUnavailable`,
   exactly as a failed group lookup does. So `may_act_in` answers `"member"`
   for a platform member, and a schedule a platform member owns keeps firing
   until they are removed. The first removal is what stops it, as removal
   from the group does today.
6. **`principal.groups` and `resolve_tenant` are untouched.**

### 4.6 Who decides, and where

| action | who | refused with |
|---|---|---|
| turn a workspace's join link on, off, or rotate it | that workspace's `admin`s; platform admins | `role_admin_required` (already a switch, `workspace-roles.md` §4.9); a `u-` or service workspace is 409 `join_not_offered` |
| approve or deny a request | that workspace's `admin`s, **read fresh** (`RoleStore.person_role(…, fresh=True)`); platform admins (`admin_auth`, or the reach, audited `via: "platform_admin"`) | 403 `join_decider_required`. It is **not** a switch: like `WORKSPACE_APPROVAL_OWNER_ONLY` it gates a new route from its first build, and it is listed in `NOT_SWITCHABLE_CODES` with that reason |
| remove a platform membership | the same | the same |
| withdraw a request | its requester only | 404 `join_request_unknown`, the same answer as for a request that does not exist |
| decide one's own request | nobody: an admin of the workspace is already a member, so they never had one | |

A workspace admin's own role check needs no switch for these routes. The
switch on `role_admin_required` exists so as not to narrow what members can
do today, and nobody can approve a join today.

**Where they decide:**

* **Admin › People and roles** (`People.tsx`). The workspace-scoped read
  (`People.in_workspaces`) gains the open join requests for the workspaces
  the caller administers. The platform read (`People.everyone`) gains every
  open join request.
  * **The lead.** A workspace admin's lead says "1 person is waiting to join
    Example Labs" (S5f). A platform admin's lead counts join requests with
    personal-workspace and loan requests ("3 people are waiting for an
    admin").
  * **Waiting for you.** One row per open request: "lee@example.com · to
    join Example Labs · 2 h ago · Approve / Deny". The message, if any, is
    shown beneath. Approve offers the role (default preselected). Deny
    requires a reason, as a personal-workspace denial does.
  * **The grid.** The requester's row under that workspace's column reads
    `requested`. `role_cell` returns it when an open request exists and no
    role or sighting does, so a person with nothing else in the workspace
    still appears in the workspace admin's grid. After approval the cell
    shows the role, with a "joined by request" mark that distinguishes a
    platform membership from a Google-group member.
  * **The count.** `pending` stops being 0 for a workspace admin.
* **The bell** (`notifications.py`). Two new sources, each derived and never
  stored, in the module's own rule:
  * `join_requests_to_decide`, for a person who administers group workspaces
    (the `administered_workspaces` read People already makes) and for
    platform admins across all workspaces. Item id
    `admin:join:<ref>`, topic `T_ADMIN`, link `/admin/people`.
  * `own_join_requests`, for the requester. "Your request to join Example
    Labs was approved", or "…was denied: <reason>", within 7 days of the
    decision. Item id `join:<ref>:<state>`.
* **Settings › People and roles** (`WorkspacePeople.tsx`). It gains the join
  link card for admins, and a "Joined by request" list with Remove.

### 4.7 The audit record

Every row goes in `admin_audit`, `{action, target_email, target_tenant_id,
by, at, detail}`, in the **same batch** as the change it records. So a
change and its row both land or neither does:

| action | written when | detail |
|---|---|---|
| `join_requested` | a request is created | `{ref, via: "console"}`. The message is not copied |
| `join_approved` | approval | `{ref, role, admitted: true\|false, via: "workspace_admin"\|"platform_admin"}` |
| `join_denied` | denial | `{ref, via}`. The reason is in the decision, redacted |
| `join_withdrawn` | withdrawal | `{ref}` |
| `workspace_member_added` | the membership grant | `{via: "join_request", ref}` |
| `workspace_member_removed` | a removal | `{via, still_in_group}` |
| `join_link_changed` | link on, off or rotated | `{on, rotated: true\|false}`. **Never the code or its hash** |

Expiry writes no row, because it is derived. These rows appear in Admin ›
People and roles › Recent admin actions and in the workspace's own role
audit (`RoleStore.audit`, which reads `admin_audit` by `target_tenant_id`).
So every member of the workspace can see who was let in and by whom, as
`workspace-roles.md` Q7 decided for roles. A platform admin acting through
the reach also gets the generic `platform_admin_acted` row, as for any of
their writes.

### 4.8 Routes

**The person's own** (`current_auth`; not tenant-scoped, because the person
is not in the workspace yet):

```
POST   /v1/join/lookup                  {code}            -> {display_name} | 404 join_link_unknown
POST   /v1/join-requests                {code, message?}  -> 202 {ref, state, display_name, expires_at}
GET    /v1/join-requests/mine                             -> [{ref, display_name, state, requested_at, decided_at?, reason?}]
POST   /v1/join-requests/{ref}/withdraw
```

**A workspace admin's, under the tenant `X-Swarm-Tenant` selects** (`admin`
there, or a platform admin):

```
GET    /v1/workspace/join-link                            -> {on, set_by, set_at}
PUT    /v1/workspace/join-link          {on, rotate?}     -> {on, link?}   // link only when just made
GET    /v1/workspace/members                              -> platform memberships, with still_in_group
DELETE /v1/workspace/members/person     {email, reason?}
```

**The decisions, for Admin › People and roles**, which is not
tenant-switched. They use a new `join_scope` dependency, beside
`people_scope`, that admits a platform admin or an admin of that request's
workspace, read fresh:

```
POST   /v1/admin/join-requests/{ref}/approve   {role?}
POST   /v1/admin/join-requests/{ref}/deny      {reason}
```

The open requests themselves are served inside `GET /v1/admin/people`'s
existing read, as `join_requests`, scoped as the rest of that read is.

* Emails travel in bodies, never paths.
* `ref` is not a secret. It is what an admin acts on, and it is not the
  document id, so knowing it does not let a worker overwrite anything.
* Every body is a closed pydantic model (an extra field is 422) with no
  image, command or resource spec (invariant 10).

### 4.9 Refusal codes

New codes, each listed in `refusals.py` and `docs/api-refusals.md`:

* `join_link_unknown` (404);
* `join_already_member` (409);
* `join_already_reaches` (409);
* `join_not_offered` (409);
* `join_request_unknown` (404);
* `join_request_invalid` (409);
* `join_request_decided` (409);
* `join_workspace_changed` (409);
* `join_role_not_allowed` (409);
* `join_decider_required` (403);
* the four 429 limits of §4.1;
* `membership_unavailable` (503).

None is a switch. Every one gates a route or a path that does not exist
today, so none narrows anything a person can do now. They are added to
`NOT_SWITCHABLE_CODES` with that reason, as WK-RECORD did for its codes.

### 4.10 Failure cases

| case | what happens |
|---|---|
| the group was replaced in `TENANT_GROUPS` after approval | the old membership names the old group, `_registered_reach` says no, and it admits nobody. The grid shows "joined by request, group changed" |
| the workspace record leaves `ready` | the same: it is no longer registered, so nobody, member or not, selects it |
| the person is later added to the Google group | the group choice wins (§4.5 step 1). The membership is redundant and shown as both |
| the person is removed from the Google group but holds a membership | they stay in by membership. This is the point of (A), and the grid shows the source, so the admin removes the membership too |
| two admins decide the same request at once | the batch re-reads the request in a transaction: one wins, and the other gets 409 `join_request_decided` |
| Firestore write fails | nothing is written, including the audit row: 503, retry |
| `settings_key` is rotated | `settings_key_previous` verifies old requests, memberships and links, as for roles |

### 4.11 What this does not do

* **Invitations** (S6's "People invited · Invite"). Under (A) an admin
  writing a membership for an email directly is the same write without a
  request, but it is a product decision of its own and is not built here.
* **The sc plugin and the MCP bridge.** No `sc join`. The console is the
  only surface in this build; a later lane can add a read-only tool.
* **Writing a Google group**, under any option.
* **Joining a personal or service workspace**, ever.

---

## 5. Frozen contract: no change requested

Nothing in `apps/common/swarm_common/` changes under (A), (B) or (C):

* `resolve_tenant` and `Principal.groups` are not touched (§4.5 step 6);
* the membership is a swarm-api `AuthContext` field, as `platform_reach` is;
* no `Task`, lease or `Tenant` field is added;
* the new collections are swarm-api's own, read by nothing outside it.

`docs/contract-change-requests.md` is therefore not edited.

One thing would need a request: making a platform member's workspace their
default **through the frozen resolver** rather than beside it. §4.5 step 2
avoids that, by the precedent `_with_own_workspace` set.

---

## 6. Build plan

The lanes are split by territory. No two lanes share a file. Each lane's
tests are red first, run with no credentials and no emulator, and are named
in its brief.

| # | lane | files | depends on | what its tests show |
|---|---|---|---|---|
| 1 | **JOIN-STORE** | `apps/swarm-api/swarm_api/joinrequests.py` (new), `memberships.py` (new), `errors.py`, `refusals.py`, `tests/unit/control_plane/test_join_requests_store.py` (new), `test_workspace_memberships.py` (new), `test_new_refusals_ship_off.py` (only `NOT_SWITCHABLE_CODES` entries) | | a request, decision, membership or link signed under one domain does not verify under another; a forged unsigned membership is skipped and counted; a replayed old `member` loses to a newer `removed`; a request whose `requester` was edited after signing is `join_request_invalid` on approval; each rate limit of §4.1 refuses at its edge and not one below; expiry is derived at 14 days; a `u-` tenant or a service-account subject is refused at write and skipped at read |
| 2 | **JOIN-RESOLVE** (after STORE; security review, blocking) | `apps/swarm-api/swarm_api/auth.py`, `settings.py` (`platform_memberships`, off, with its reason), `routes/tenants.py` (`via: "platform_member"`), `tests/unit/control_plane/test_platform_memberships_resolve.py` (new) | 1 | switch off: every existing auth test is unchanged and no membership query is made; switch on: a valid membership is in `tenant_choices` after the groups, the header selects it, and `tenant_principal` is the group; one for a replaced group admits nobody; a group member's choice is not duplicated; a failed read 503s a header naming that tenant and leaves a group member untouched; `is_tenant_member(fresh=True)` reads fresh; no Cloud Identity call is added (the directory fake counts) |
| 3 | **JOIN-API** (after STORE) | `apps/swarm-api/swarm_api/routes/join_requests.py` (new), `main.py` (one `include_router`), `people.py` (`in_workspaces`/`everyone` gain `join_requests`, `pending`, the `requested` cell), `routes/people.py` (`join_scope`), `tests/unit/control_plane/test_join_requests_routes.py` (new) | 1 | an unknown, rotated and off code answer byte-identical 404s; a workspace admin approves only their workspace's request and gets 403 `join_decider_required` on another's; a platform admin approves any; approve writes decision, membership, optional role override and audit rows in one batch (an injected failure leaves none); with the switch off approve answers `admitted: false` with the group command; no response carries a document id or a code hash |
| 4 | **JOIN-BELL** (after API) | `apps/swarm-api/swarm_api/notifications.py`, `tests/unit/control_plane/test_notifications_join.py` (new) | 3 | a workspace admin is rung for their workspace's request and not another's; a non-admin is rung for nothing; the requester is rung once per decision; a decided request derives no "needs you" item |
| 5 | **JOIN-INDEX** (Track C) | `terraform/modules/firestore/indexes.tf`, `tests/terraform/` (one assertion per index), `terraform/environments/dev/dev.tfvars` (switch on in dev, a separate commit after lane 8 is green) | | the three composite indexes exist; prod's switch is absent, so off |
| 6 | **JOIN-UI-ADMIN** (after API) | `apps/swarm-ui/src/People.tsx`, `apps/swarm-ui/src/__tests__/admin.peopleJoin.test.tsx` (new) | 3 | S5f: the lead's sentence, the Waiting for you row with Approve/Deny, the grid's `requested` cell; a denial cannot be sent without a reason; `admitted: false` shows the group command |
| 7 | **JOIN-UI-PERSON** (after API) | `apps/swarm-ui/src/JoinWorkspace.tsx` (new), `WorkspacePeople.tsx` (link card, joined-by-request list), `App.tsx` (the `/join` route only), `api.ts`, `types.ts`, `apps/swarm-ui/src/__tests__/joinWorkspace.test.tsx` (new) | 3 | the code is read from the fragment and posted in a body, never put in a request path; the person's request list shows each state; the link is shown once on creation and never again |
| 8 | **JOIN-DOCS-ACCEPT** (last) | `docs/multi-tenancy.md` §2 and §3, `docs/design/workspace-roles.md` (Q3's amendment), `docs/notifications.md`, `docs/api-refusals.md`, this file's §8, and the acceptance probe under `tests/acceptance/` | 2–7 | in dev: a second test identity asks with a link, an admin identity approves, the asker's next request selects the workspace, removal ends it, and no Google group was written |

`types.ts` and `api.ts` are in lane 7 only. Lane 6 adds its types to
`People.tsx`'s own module, so the two UI lanes never touch one file. If
lane 6 needs a shared type, it waits for 7.

---

## 7. Owner questions

Also in this lane's `questions.json`. The recommendation comes first in each.

**Q1. What does approval do?**

* **(A) A platform-side membership beside the Google group (recommended).**
  It meets #2325's point 3. It grants no GCP IAM. It needs no new Google
  permission. It ships behind a switch, and with the switch off it behaves
  as (C). It partly reverses `workspace-roles.md` Q3.
* **(B) Add the person to the Google group through Cloud Identity.** This
  needs a Workspace super admin to widen swarm-api's delegation to group
  write. Approval then confers whatever IAM the group carries, which for
  `eng@saga.xyz` is project-wide roles on the shared project.
* **(C) Record the decision, and tell the admin to change the group.** The
  cheapest, with no isolation change. A person still edits the group by hand.

**Q2. How does a person find the workspace to ask?**

* **(a) A join link with a per-workspace code, off until an admin turns it
  on (recommended).**
* (b) They type the group's address, and get the same answer whether or not
  it is registered.
* (c) A directory of workspaces their admins chose to list.

**Q3. Who may decide?**

* **(a) That workspace's admins and platform admins (recommended).** This is
  what S5f draws.
* (b) Platform admins only, as for personal workspaces.

**Q4. What role does an approved person get?**

* **(a) The workspace default, which the approving admin may lower to
  `viewer` (recommended).**
* (b) Always the default.
* (c) Always `viewer`, raised later by an admin.

**Q5. How long is a request open, and how long is a decided one kept?**

* **(a) Open 14 days; kept 90 days after its decision. The `admin_audit` row
  is permanent (recommended).**
* (b) Open until decided. A queue nobody owns grows forever.
* (c) Open 7 days.

**Q6. Does a membership expire?**

* **(a) No. It lasts until an admin removes it, as a group membership does
  (recommended).**
* (b) The approving admin may set an end date.
* (c) Always 90 days, renewed by asking again.

## 8. What was built

Nothing yet. The lane that builds the last part (lane 8 of §6) records here
what each lane landed and where the code departs from this design.
