# Multi-tenancy

Multi-tenant from V1, not retrofitted. Retrofitting tenancy means retrofitting
every IAM binding, every storage path and every secret name at once, while
production data already exists in the single-tenant shapes.

**A tenant is a Google group.** `eng@saga.xyz` is one tenant whose members share
a quota budget, provider keys and artifacts. A user in no registered group falls
back to a personal tenant `u-<local-part>`, so nobody is ever hard-blocked from
the platform. A person whose personal workspace is **ready** acts in it by
default, and their groups are what they switch to (#1200; see the switcher
below).

---

## 1. What a tenant owns

Registering a tenant creates every boundary at once — there is no
"half-registered" state worth having:

| Boundary | Resource | Enforced by |
|---|---|---|
| Identity | `swarm-agent-worker-<id>@<project>.iam.gserviceaccount.com` | IAM |
| Control-plane data | custom role `swarmTenantWorkerFirestore`, **unconditioned**, across the whole `swarm` database | IAM, on the shape of the access only. **Not a tenant boundary today**: see the caveat below and [section 4](#the-firestore-row-in-full) |
| Artifacts & checkpoints | GCS prefix `tenants/<id>/`, granted by IAM condition | IAM condition on the binding |
| Provider keys | `swarm-tenant-<id>-<provider>`, secret-level IAM | Secret Manager resource policy |
| GKE workloads | namespace `swarm-tenant-<id>` + workload identity binding | Kubernetes RBAC + default-deny NetworkPolicy |
| Capacity | `tenant:<id>` and `provider:<p>:tenant:<id>` slot pools | admission transaction |
| Record | `tenants/<id>` document | control plane |

Removing a tenant is the same list in reverse, in an order that matters, and
the namespace is removed by hand: the reconciler holds no ClusterRole and does
not collect namespaces (owner decision, 2026-09-24). See
[the tenant offboarding runbook](runbooks/tenant-offboarding.md), which also
lists what a tenant accumulates at runtime and this table does not show. All
of that is deleted at offboarding, not kept (owner decision, 2026-09-24): the
records and objects by `scripts/offboard-tenant.sh`
([step 6](runbooks/tenant-offboarding.md#6-delete-everything-the-tenant-left)),
and the secrets by terraform and by hand in the runbook's later steps.
A tenant id is derived from its principal and the GCS grant is a prefix
condition on that id, so anything kept would be readable by the next
registration that derives the same id.

The namespace is `swarm-tenant-<id>`. The GKE row said `swarm-<id>` until
2026-09-24, which is the spelling behind the 2026-09-23 dispatch outage
([the dispatch 403 note](gke-dispatch-403.md)).

The service account name is `swarm-agent-worker-<id>`, and it is the one name to
grant or audit against. It is what `terraform/modules/tenancy` creates, what
`terraform/modules/cloud_run_jobs` binds to each `(tenant, profile)` Job, and
what `kubernetes/render.py` puts in the Workload Identity annotation. Two other
spellings used to appear — the Identity row said `swarm-tenant-<id>` and
`register-tenant.sh` created `swarm-t-<id>` — and neither existed in a deployed
project, so an operator checking a boundary found nothing and concluded
registration had failed.

**The Firestore row is the weakest boundary in this table, and it is worth
reading before relying on it.** Firestore IAM has no collection- or
document-level granularity: the smallest resource a binding or a condition can
name is the *database*. And the binding carries **no condition at all**, because
Firestore does not evaluate IAM Conditions on the data plane — attaching one
denies document access outright rather than scoping it (verified live; see
[docs/security.md](security.md#firestore-database-scoping-does-not-exist-verified-2026-09-16)).
So every swarm identity shares one authorization scope over *every* Firestore
database in this project — and a worker running attacker-controlled code is
inside that scope. What is available
at this layer is the *shape* of the access, so the role is built rather than
borrowed: `swarmTenantWorkerFirestore` drops `datastore.entities.delete` (a
hostile worker cannot destroy another tenant's tasks, leases or the pool
documents the platform admits against) and `datastore.entities.list` (no
queries, so no enumerating every tenant's prompts and repository URLs — a
document can only be fetched by an id already known, and task, attempt and
lease ids are `<prefix>_<20 hex>`). **It keeps `update`, and update does the
damage delete would have:** a worker that cannot delete `pools/global` can set
its `active` to 0, and one that cannot delete `tenants/<id>` can rewrite the
service account that tenant's next attempt runs as. Section 4 lists what a
worker can write today, what phase 0 of #1457 closes and what design B closes;
the residual is stated there rather than implied.

```bash
./scripts/register-tenant.sh --group eng@saga.xyz \
    --providers anthropic,openai --max-active 40 --capacity-units 80
```

For a tenant that `terraform/environments/dev/dev.tfvars` will also name, the
order matters (#334): the script creates the worker account, the owner applies
terraform/bootstrap's per-account `serviceAccountAdmin` grant on it to the
release deployer from `main`, reading the pull request's `dev.tfvars` as data
(the script checks for the grant and prints the commands), and
only then does the release that adds the tenant run. The release sets the
account's IAM in the same apply, and without the grant it 403s.
[docs/ci.md](ci.md#a-new-account-exists-before-the-release-that-adds-it) says
why.

Re-running updates limits and wiring and fills in whatever is missing — by
writing the whole record, so `--providers` **replaces** the tenant's
credentials and any limit not typed again goes back to its default. To add one
provider, use `--add-provider` (section 5), which changes nothing else. It will
**refuse** if `tenants/<id>` already names a different principal or kind: that
is not idempotency, it is re-pointing a tenant, and one
`--group contractors@saga.xyz --tenant eng` would otherwise hand every member of
`contractors@` eng's provider keys, artifact prefix and budget.

The script creates the GKE side by calling `kubernetes/apply.sh`, so a
registered tenant gets its NetworkPolicy, ResourceQuota, LimitRange, Pod
Security Admission labels and RBAC in the same run as its namespace. It used to
create only the namespace and a service account, which left the namespace on
Kubernetes' default allow-all pod networking — one tenant's agent could open a
socket to another's, traffic no IAM scoping ever sees. If the cluster is not
reachable the script says so and skips that step; nothing on the Cloud Run path
needs it, and browser-class runners cannot be dispatched for that tenant until
it is done.

---

## 2. Group resolution — the constraint that shapes the API

Tested live against `saga-agents-staging` on 2026-09-15:

```
groups:lookup?groupKey.id=eng@saga.xyz                      WORKS
groups/{id}/memberships:checkTransitiveMembership           WORKS
groups/-/memberships:searchTransitiveGroups                 403 Error(4013)
                              "Insufficient permissions to retrieve memberships"
```

So the API **must not** resolve a caller's tenant by enumerating the groups they
belong to. That call is the one that 403s, and fixing it would need an
org-level IAM grant.

Instead the question is inverted. For each admin-registered tenant group, in
priority order, the API asks:

```
GET https://cloudidentity.googleapis.com/v1/{group_id}/memberships:checkTransitiveMembership
    ?query=member_key_id == '<caller email>'
-> {"hasMembership": true|false}
```

This needs no org-level grant and is least-privilege: the platform only ever
learns about groups an admin deliberately registered. It also required no change
to the frozen contract — `identity.resolve_tenant(principal, group_priority)`
already takes the admin-ordered group list, so the API just populates
`Principal.groups` from these per-group checks.

**Every Cloud Identity call requires the header `x-goog-user-project: <project>`.**
Without it the call fails 403 `SERVICE_DISABLED` naming gcloud's shared client
project as the consumer, which reads like a permissions problem and is not.

`checkTransitiveMembership` is a network round trip on a request path that must
stay fast, so answers are cached per `(caller, group)` with a short TTL
(`GROUP_CACHE_TTL_SECONDS`, default 120).

### Priority order matters

`TENANT_GROUPS` is ordered, and the **first match wins**. A user in both
`eng@saga.xyz` and `research@saga.xyz` must land in the same tenant on every
request — the tenant determines which secrets they get, which GCS prefix their
artifacts land in, and which budget they spend. Non-deterministic resolution
would scatter one person's work across two tenants.

**A group workspace the job made is registered by being `ready`.** Its group
joins the list after every `TENANT_GROUPS` entry, in the order the records
became ready (`apps/swarm-api/swarm_api/auth.py::Authenticator._tenant_groups`).
A Terraform group therefore always wins, and a job-made group is a member's
default only when no Terraform group is above it (owner answer Q4 (a)).
Membership is still asked one group at a time, with `x-goog-user-project`,
and a failed lookup is a 503, as for any registered group (Q5 (a)). A group
record not yet `ready` is in nobody's choices. If the registry cannot be read,
swarm-api serves its last good read for 10 minutes and then answers 503
`registry_unavailable`, never a guess.

### The tenant switcher: `X-Swarm-Tenant` selects, it never grants

A person in more than one registered tenant group can work in any of them
(owner decision 2026-10-01), and in their own personal workspace beside them
(#1200, 2026-10-12). A request may carry

    X-Swarm-Tenant: <tenant_id>

and the API then acts as that tenant — but **only** if it is one of the
caller's **choices**:

1. **The caller's own personal workspace, first**, when it is ready: the id is
   `tenant_id_for_user(<verified email>)` — derived from the token, never from
   the header — and `auth.PersonalTenants` offers it only when
   `tenants/u-<you>` is `kind: user` with your email as its principal **and**
   `workspaces/u-<you>` is `ready` with that same principal. Both principal
   checks matter: the frozen slug keeps only the local part, so
   `bob@saga.xyz` and `bob@partner.com` derive the same `u-bob`, and only the
   principal says whose it is. `approved` is not enough: the build has not made
   the service account, namespace and secrets yet, and the workspace gate
   refuses a submit there anyway. A service identity, the schedule tick, the
   rollup sweeper and a listed service account are never offered one.
2. **Then every registered tenant group** Cloud Identity confirmed for this
   caller on this request, in admin order.
3. **A service account's own service tenant, only when the header names it**
   (HW-ACCEPT-ID, owner decision 2026-10-19): a service account may name its
   own service tenant in `X-Swarm-Tenant` -- `tenant_id_for_user(<verified
   email>)`, when `tenants/<that id>` is `kind: user` with exactly that email
   as principal, and the caller is not a worker, a job or a listed account --
   and its default is unchanged. No `workspaces/` record is asked for: a
   service tenant has none. This is how release acceptance answers its
   await-input steps as swarm-verify in `u-sw-c90291`
   (docs/design/human-steps.md §4.2.2).

**The default — no header — is the first choice** (owner decision 2026-10-12
15:55 PDT: personal by default, groups are what you switch to). So a person
with a ready workspace acts in `u-<you>` unless they name a group; a person
without one acts in the first matching group, exactly as before; a person in no
group acts in their personal tenant, exactly as before. Every caller that needs
a group must send the header: the console and the plugin do once a tenant is
picked; CI, smoke and acceptance call as service accounts, which keep the
group default.

**A default you chose comes before that rule** (docs/design/user-profile.md
§4.5). A person picks a default workspace on Settings › Profile; it is stored
in their own `user_preferences` document, whose id is an HMAC of their email
under `swarm-child-key`, so no tenant agent can plant one. In authentication,
after the personal workspace is placed, `Authenticator._with_default` reads
that document (through a 15-second per-instance cache, `Preferences.cached`)
and, **when the stored default is one of your choices**, moves it to the
front. Choices are what it chooses from, exactly as the header does: the
preference selects and never grants. A group you have left is ignored
(`default_ignored: "not_a_member"`), and so is your own workspace while it is
not ready (`"not_ready"`); the stored value is kept, and applies again when it
is a choice again. `_select_tenant` is unchanged, so **every explicit choice
still wins**: the console's switch, `SWARM_TENANT` or the plugin's tenant
option, and `--tenant` all send the header. `GET /v1/tenants/mine` keeps
listing exactly `tenant_choices`, so its first entry is still the tenant a
headerless request acts in; that entry carries `is_default: true` and
`default_source` — `profile`, `personal`, `first_group` or `unavailable` —
and every other entry `is_default: false`. A service identity, the schedule
tick, the rollup sweeper and a listed service account never read preferences.
The document also names you: `AuthContext.display_name`, the name your
agent's commits carry, is the stored name when you set one, else your token's
`name` claim, else your email's local part (Q11).

**If your preferences cannot be read, a request that names no workspace is a
`503 preferences_unavailable`** (Q9), raised before the route body runs, so
nothing is submitted or read. Its message says how to name a workspace: the
console's switcher, `SWARM_TENANT` or the `/sc` plugin's tenant option, or
`--tenant`. It never falls back to the computed default, for the same reason
a failed personal-workspace read does not: you may have moved your default
away from it, and the fallback would file your work in a workspace you did
not choose — another secret, another GCS prefix. A request that names its
workspace does not need the default and is served as before. Three routes act
in no workspace and are served anyway, with `default_source: "unavailable"`
and today's order: `GET /v1/tenants/me`, `GET /v1/tenants/mine` and
`GET /v1/profile`, the closed list `auth.PREFERENCE_FREE_ROUTES`. They are
what a person needs to see their choices and pick one; the bell
(`GET /v1/notifications`) is not among them, because its items belong to a
workspace. The check is `auth.require_known_default`, made in the per-route
gate every authenticated request reaches (`require_continuation_route`).

The stored default applies where the API's authenticator is built with the
preferences store (`Authenticator(..., preferences=...)`); an authenticator
built without one reads nothing and keeps the rule above, exactly as before
the profile. **The deployed API does not pass it yet**:
`deps.build_context` builds its `Authenticator` without `preferences=`, so in
every deployed process a headerless request still acts in the group or
personal default, `AuthContext.preferences` is `None`, the stored display name
does not name commits and the 503 above never fires. Wiring it is the
one-line `preferences=build_preferences(db, settings)` in `deps.py`, a file
no lane of docs/design/user-profile.md §6 holds; it waits on the owner's
go-ahead, and on a decision about an environment with no `swarm-child-key`,
where `Preferences.read_many` raises `PreferencesUnavailable` and every
headerless request from a person would become that 503.

Anything else — another group's tenant, **another person's `u-` tenant**, your
own `u-` tenant before its workspace is ready, a tenant that does not exist, an
admin group, a near miss in case or spacing — is a `403` with code
`tenant_not_member`, raised in authentication before any route body runs, so
nothing is written. A header naming neither one of your groups nor your own
`u-` id is refused before even your own workspace is read. A failed read of
your workspace is a `503`, never a fall-back to the group: that would file a
headerless submit in a tenant other than the one you act in by default. A
request that names one of your groups needs no answer about your workspace and
goes on in that group. The refusal is identical for "not yours" and "no such tenant", so the
header is not an oracle for enumerating tenant ids.

Why it selects and never grants. The tenant decides the service account, the
secrets, the GCS prefix and the namespace a task runs under (invariant 9). A
header that could *name* a tenant would let any caller pick any of those; a
header that can only *choose among memberships already proven* changes nothing
an attacker can reach — every tenant it can reach is one the same caller could
already reach by being first in that group's priority — or, for the personal
entry, by being the one person its documents name. So:

* **One membership check, not two.** `GET /v1/tenants/mine` lists, and the
  header selects from, `AuthContext.tenant_choices` — the same confirmed
  `member_groups` the default tenant is resolved from, walked in the same admin
  order (`Authenticator._tenant_choices`), with the ready personal workspace
  put first by `Authenticator._with_own_workspace`. Each entry carries `kind`
  (`user` for your workspace, named "Your workspace (u-<you>)"; `group` for a
  group, named by its email). There is no second lookup that could
  disagree with the first. A group whose lookup failed below a confirmed match
  is dropped by `groups_for` and is therefore not selectable: an unverified
  membership is never a choice.
* **Tenant id and tenant principal move together.** The header replaces both
  from one entry, so they still describe the same tenant and the store's
  collision check (`assert_tenant_scope`, `ensure_tenant`) keeps working. Two
  groups whose ids collide (`eng@saga.xyz`, `eng@partner.com`) appear once, as
  the first in admin order — the one the id selects.
* **Admin is unaffected.** `is_admin`, `is_pool_admin` and the caller's groups
  are about the person, not the tenant, and the header does not touch them. An
  admin gains no personal tenant but their own.
* **The answer is cached for 30 seconds per email** (`PersonalTenants`), both
  ways: a workspace that becomes ready is offered within that window. The
  cache is keyed by the verified email, so it can delay an offer but never
  make one to anybody else.
* **A listed service account** (contract request 30) has no memberships to
  choose among; any header is refused.

The console (`apps/swarm-ui`) shows the spine's tenant block as a switcher
when `/v1/tenants/mine` lists more than one tenant, and as the static label
with copy-id otherwise. Your own workspace is labelled "Your workspace", with
its `u-` id as the secondary line. The pick is stored per browser (`swarm.tenant`), sent
from one place (`apiHeaders` in `fetch.ts`, used by every read, write and both
submit paths), and switching re-reads the screen. A stored pick the caller is no
longer a member of is dropped on the first `tenant_not_member` 403 and the
default used; a refused **read** is retried once on the default, a refused
**write** is not — filing a change under a tenant its author did not pick is
worse than the refusal.

**Links the browser fetches itself carry it as `?tenant=`.** An artifact's
`<img src>`, its download link and its "open full" tab
(`/v1/tasks/{id}/artifacts/raw`), and a checkpoint's archive download
(`/v1/tasks/{id}/checkpoints/{n}/content`, a plain link so a large archive
streams to disk), are requests the browser makes on its own, and those carry
no custom header — so without another channel every artifact and checkpoint of
a task in a switched-to tenant would 404 against the default. On exactly the
routes in `auth.TENANT_QUERY_ROUTES` (today those two), `current_auth`
reads `?tenant=` and hands it to the **same** `_select_tenant` check as the
header: it selects among verified memberships and never grants, and a value
the caller is not a member of is the same `tenant_not_member` 403. Everywhere
else the query parameter is ignored, so it cannot become a second way to name
a tenant on a JSON route. A header and a query parameter that disagree are a
422 rather than a silent precedence rule. The console adds it in one place,
`tenantQuery` in `fetch.ts`, used by `artifactRawUrl` and
`checkpointDownloadHref`.

**A switch is a fresh screen, except for an unsent form.** The shell keys the
page on the chosen tenant, so switching remounts it and every read runs again
under the new tenant. A half-filled submit form is KEPT instead (owner's pick,
intake-tenants.html 2A, 2026-10-02; this replaces #501's deliberate discard):
what it was drawn from — the runner profiles and pools — is read again as the
new tenant, a banner over it says it will now submit as that tenant with a way
back, and its submit button names the tenant ("Submit as platform"). The
mistake the discard guarded against — a draft written for one tenant sent
unchanged into another by someone who still thinks it goes to the first — is
answered by saying where it goes, on the button that sends it, rather than by
throwing the draft away. The API still validates the runner profile against
the new tenant's catalogue on submit.

---

## 3. Isolation, layer by layer

### Authentication

Google ID token, `hd` claim must be an allowed domain. **There is no shared
platform bearer token.** A shared secret carries no identity; without identity
there is no tenant to attribute a task to, no way to scope a list response, and
no boundary to enforce. Multi-tenancy that starts after authentication is
decorative.

### Data

Every query the API issues is filtered by the caller's resolved tenant. A task id
from another tenant returns 404 — not 403 — because confirming existence is
itself a leak.

Admin routes (`/v1/admin/*`) are gated on admin — `ADMIN_GROUPS` membership or
an address on `ADMIN_USERS` — which is kept separate from `TENANT_GROUPS` so an
admin still belongs to a normal tenant for their own tasks. One narrower list,
`ADMIN_POOL_USERS`, reaches only the routes in `swarm_api.auth.POOL_ADMIN_ROUTES`
(today the runner-ceiling `PUT`) and is not admin; see
[security.md](security.md#authentication).

### Secrets

Three independent mechanisms, all in `swarm_api/credentials.py` and
`agent_worker/secrets.py`:

1. **Name.** `swarm-tenant-<tenant>-<provider>`, from the frozen
   `Tenant.secret_name()` — one spelling of a tenant's secret anywhere in the
   platform, never assembled from caller input.
2. **IAM.** The secret's own resource policy names exactly one accessor: that
   tenant's service account. A project-level `secretmanager.secretAccessor`
   grant would make every tenant's key readable by every tenant's workload, so
   the binding is never made at project level.
3. **No read path.** The API can write a version and report metadata. There is no
   function that returns payload bytes, so no future route can accidentally
   expose one.

**One exception to "exactly one accessor": `swarm-tenant-<tenant>-git` has two.**
swarm-api's issue preview (`swarm_api/forge.py` `preview`, `routes/issues.py`,
#511) reads the tenant's forge token to show an issue before a task is filed,
and the owner accepted swarm-api as that secret's second reader on 2026-10-02.
Terraform grants it through `forge_readers` in `terraform/infra/main.tf`, but
only on a `-git` secret Terraform manages, and no tenant's is: they are
registered by `scripts/register-tenant.sh`. So the script grants it too —
wherever it lets the worker read `-git` (`--add-provider git`, or a full
registration listing git), it binds swarm-api's account, read off
`terraform/modules/service_account_ids`, on that one secret, never at project
level; a re-run that finds the binding adds nothing. It never extends to
`-git-merge` or `-git-review`: those GitHub App keys keep only their own
accessors (`docs/merge-step.md` §1.3), and swarm-api never needs to sign as an
App. Without this grant the preview answers `no_access` for every tenant.

**A person's forge slot, `swarm-tenant-<tenant>-git-u-<hex>`, is the other
exception, and isolation between the people inside one group tenant is NOT
yet in force.** Every member of a group tenant runs agents as the tenant's
one worker account, and an agent can mint that account's token. While that
account can read the slots (owner decision D7,
[git-tokens.md](git-tokens.md#built-for-780-per-person-slots-the-migration-policy-retiring-the-tenant-token)),
isolation between tenants holds but isolation between the people inside one
tenant does not -- and it still can, in every environment: the tenant
worker's binding, `forge_slot_reader` in
`terraform/bootstrap/forge_user_slots.tf`, stands, held by
`tests/terraform/forge_app.tftest.hcl`, run
`the_tenant_worker_reads_only_its_tenants_base_user_slots`, to its tenant's
base slots. The owner decided on 2026-10-11 that secrets are user-scoped
(#1041). Built, behind the `forge_broker` switch (FORGE_BROKER,
`terraform/infra/child_tasks.tf`), off by default and off in dev
([design/user-scoped-secrets.md](design/user-scoped-secrets.md)); with it on:

* the worker obtains the slot from swarm-api's broker,
  `POST /v1/attempts/forge-credential`
  (`apps/swarm-api/swarm_api/credbroker.py::ForgeCredentialBroker.release`),
  signed with its attempt key
  (`apps/agent-worker/agent_worker/forgebroker.py::ForgeBroker.fetch`), and
  only for the current attempt of a task whose signed `submitted_by` owns
  the slot and still holds the grant. Bob's attempt asking for Alice's slot
  is refused before Secret Manager is read
  (`tests/unit/control_plane/test_forge_credential_broker.py::test_bobs_attempt_is_refused_alices_slot_and_reads_no_secret`);
* the scheduler issues the attempt key's nonce to every attempt whose signed
  `forge_credential` is a user slot, children included
  (`apps/scheduler/scheduler/dispatch.py::worker_env`), and swarm-api builds
  a child, slot included, only from a parent whose signature verifies
  (`apps/swarm-api/swarm_api/children.py::ChildService._read_parent`);
* the tenant token, repository tokens, the App keys and every provider key
  stay team credentials, read as described above.

Only then does swarm-api become the slot's only reader: removing
`forge_slot_reader` is a later, separate owner change and bootstrap apply,
made only after `forge_broker` has been on in that environment and a live
run passed (`forge_broker` needs `enable_child_tasks`, after the nonce
measurement of [design/child-tasks.md §9](design/child-tasks.md#9-phase-2)
item 2 on which the guarantee rests -- made in dev on 2026-10-11, not in
prod). With the switch off the worker reads a person's slot with its own
account, as before; were the binding removed first, that read would be
refused and every user-slot task would fail closed
([design/user-scoped-secrets.md §5](design/user-scoped-secrets.md#5-iam-the-worker-loses-its-read-of-user-slots);
[runbooks/github-app.md](runbooks/github-app.md), step 2).

#### What the forge credential must be allowed

`swarm-tenant-<tenant>-git` is no longer only the token an agent pushes with.
An issue run (#454) also **writes back to the issue** with it, from swarm-api
(`swarm_api/forgewrite.py`, `swarm_api/issuesync.py`): the plan as a comment,
one status comment edited in place, and the `Closes #N` / `part of #N` block
in the pull request's body (and, when the title carries a closing keyword --
an agent's own `pr-title.txt`; the worker's title says "part of #N" -- the
title, so a squash merge cannot close the issue the block did not). The owner decided on 2026-10-01 that these writes
use the tenant's own credential, not a platform-wide bot, so that a tenant can
only ever write where its own token can; the CI loop then reads the pull
request's checks with it. On the repositories a tenant submits issues from,
the token therefore needs:

| permission (fine-grained name) | what uses it |
|---|---|
| `Contents: Read and write` | the agents' pushes (unchanged) |
| `Pull requests: Read and write` | opening the pull request (unchanged), reading its head sha, writing its body's keyword block |
| `Issues: Read and write` | the plan comment and the status comment |
| `Checks: Read` | the CI loop reading the required checks at the head sha |
| `Commit statuses: Read` | the CI loop reading a required check reported as a commit status (`forgewrite.STATUSES_READ`) |
| `Actions: Read` | the CI loop reading a failing run's log excerpt |

A classic token's `repo` scope carries all six. Why the run is built this way
is in [issue-runs.md](issue-runs.md#the-tenants-credential-and-what-it-must-be-allowed). **Without `Issues: write` the
run still runs**: the write-back never fails or blocks a run, because the run's
truth is its Firestore document, not the comment. The failure is recorded on
the run as `writeback_error` — code `writeback_forbidden`, with a sentence
naming the missing permission and never the token — logged by its code, and
not retried for five minutes unless what it would write changes. Fix the
token and store a new version with `scripts/create-secrets.sh --stdin`; the
next read of the run posts what was missed. Nothing about how the secret is
stored changes: the same name, the same `--stdin`, the same two readers.

The comments are posted as whoever the token belongs to, on a repository that
may be public, so they carry nothing the token's owner would not post: every
text an agent or the issue wrote is redacted with the token as a known
literal, its `@`-mentions are broken so nobody is paged, its closing keywords
are turned into `refs` so it cannot close an issue, and an approver is named by
the local part of their address only.

The plaintext key exists only as a local variable during `put_credential`, is
registered with the logger for redaction the moment a worker reads it, and
reaches the agent through the child's environment only — never argv, never the
workspace, never an artifact.

Secret **material** is deliberately not managed by Terraform: a Terraform-managed
secret version puts the plaintext in the state file, and state lives in a bucket
several people can read. `scripts/create-secrets.sh` owns both the container and
its versions, which is also why `make destroy` never sees them in a plan.

### Storage

Each tenant's GCS access is granted with an IAM **condition** on the object
prefix `tenants/<id>/`. Tenant A's service account cannot read tenant B's
artifacts even by guessing the path, because the denial happens in Google's
authorization layer rather than in our code.

### Compute

* Cloud Run: one Job resource per `(tenant, profile)`, bound to that tenant's
  service account — forced by Cloud Run setting the SA on the Job, not the
  execution. See [execution-backends.md](execution-backends.md).
* GKE: namespace `swarm-tenant-<id>` (the dispatcher's default template,
  `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher.namespace_for`; this row said `swarm-<id>` until
  2026-10-02, the spelling §1 above records as behind the 2026-09-23 outage),
  tenant KSA workload-identity-bound to the tenant GSA, default-deny
  NetworkPolicy so one tenant's pod cannot reach another's.

### Capacity

`tenant:<id>` caps how much of the platform one tenant can hold.
`provider:<p>:tenant:<id>` caps their provider concurrency separately, which is
what stops one tenant's 429 storm from throttling everyone else — their AIMD
target falls, nobody else's does.

### Schedules: what a runaway one can and cannot do to another tenant

From 2026-10-11 recurring tenant work runs as **schedules**
([schedules.md](schedules.md), §5.6 there), not as a Cloud Scheduler job per
tenant. Lane SWEEP's per-tenant `issue_sweep` job was paused on 2026-10-11,
when its `issue-sweep` schedule replaced it, and the job, its route and
`SWEEP_ENABLED` were deleted on 2026-10-12, the owner having waived the
planned 7-day watch
([operations.md §13](operations.md#13-schedules-the-s9-migration-sweep-removed-2026-10-12)).
A schedule is a document in `schedules/`, carries its `tenant_id`, and fires
**as its owner, in its own tenant**. The tick that fires every tenant's
schedules holds one route and submits nothing as itself. So a schedule's work
is ordinary tenant work, under every isolation mechanism above:

| it CAN | it CANNOT |
|---|---|
| Fill its own tenant's `tenant:<id>` pool, up to `max_active` and `capacity_units`. The tenant's other work then queues behind it | Exceed its tenant's ceilings. Admission checks every pool in one transaction (invariants 2-3) |
| Take `global` and `resource:<class>` slots up to its tenant ceiling, so other tenants' work queues longer when the platform is full. That is the ceiling arithmetic an admin already chose | Hold capacity while waiting. Approvals, queued firings and `PLANNED` runs are documents (invariant 1) |
| Use a shared-pool Claude account's concurrency where its tenant borrows one. Per-key `provider:<p>:tenant:<id>` AIMD keeps its 429s to its own target | Exhaust another tenant's provider key: keys are per tenant (invariant 9) |
| Spend its own forge token's rate limit | Delay another tenant's firings by more than one tick: at most 5 firings per tenant per tick, round-robin |
| Fire as often as its type's minimum interval allows, across at most 25 schedules | Run without bound. `max_concurrent`, `per_day_usd`, the sweep's cap of 8 live runs and the consecutive-failure pause bound it, and an admin can pause it |

Another tenant's schedule id answers 404 on every route, as a task's does. When
one tenant's schedules crowd the platform, an admin uses what they use today:
the tenant's ceiling (`PUT /admin/tenants/<t>/limits`), plus pausing the
schedule from Automate › Schedules, switched to All tenants
(`/automate/schedules?scope=all`, admins only; it was Admin › Schedules until
the owner folded the two pages into one on 2026-10-13).

---

## 4. Cross-tenant escape: what would have to go wrong

The failure this design treats as the worst outcome. Every path needs at least
two independent mechanisms to fail together:

| Attack | Blocked by |
|---|---|
| Ask the API for another tenant's task | tenant-scoped queries; 404 not 403 |
| Name another tenant's secret in a request | secret name comes from the frozen contract, never from input |
| Read another tenant's secret from a worker | worker runs as the tenant GSA; secret IAM names one accessor |
| Read another tenant's checkpoints | GCS binding is conditioned on the tenant prefix, listing included |
| Hand another task **of the same tenant** a planted checkpoint | **partially.** The bucket grant is per tenant, not per task, so an agent can write under a sibling task's prefix. A first attempt restores nothing, and a retry restores only the checkpoint its earlier attempt recorded, with the archive digest, in Firestore (#347; [checkpointing.md](checkpointing.md#3-restore)). An agent that also writes the task and attempt documents is not stopped: see the Firestore row below, and #342 |
| Reach another tenant's pod | per-tenant namespace + default-deny NetworkPolicy |
| Have work dispatched under another tenant's identity | **not today.** The Job and the pod take the identity and namespace from the tenant's own `tenants/<id>` document, which that tenant's worker can rewrite: see "Today" below. Phase 0 of #1457 closes it by deriving both from the tenant id |
| Escalate via a runner profile | callers name profiles; images and commands are catalogue-only |
| Spoof a tenant claim | tenant is derived from a verified ID token, never from a header or body field |
| **Read or write another tenant's control-plane documents** | **partially. See below — this row is the one that does not have two mechanisms.** |

### The Firestore row, in full

Every other row above needs two independent mechanisms to fail. This one does
not have two, and pretending otherwise would be worse than saying so. The
identity row above is open today only because this one is: the dispatcher reads
the identity from a document this row lets a worker rewrite.

Firestore exposes no collection-level IAM to a server-side service account. The
smallest resource a binding or a condition can name is the database — and on the
data plane it cannot even name that, because Firestore ignores IAM Conditions
there. A `resource.name == .../databases/swarm` condition does not scope the
grant, it denies it: every identity that carried one reported `firestore
unavailable: PermissionDenied`, so both Terraform (`scope_firestore_to_database`,
default false) and `register-tenant.sh` now grant the role unconditioned.
`docs/security.md` concedes that an agent can mint its own
workload's metadata token and argues that the boundary is what that identity can
do — so here is what it can do:

* **It cannot delete anything.** `datastore.entities.delete` is not in the role.
  Another tenant's tasks, attempts, leases and the `pools/*` documents the whole
  platform admits against cannot be destroyed.
* **It cannot enumerate anything.** `datastore.entities.list` is not in the role,
  so there are no queries: no dumping every tenant's prompts, repository URLs and
  artifact paths. A document can only be fetched by an id already known, and
  task, attempt and lease ids are `<prefix>_<20 hex>`.
* **It CAN read and write a document whose id it can guess.** The role grants
  `datastore.entities.get`, `create` and `update` on every document in the
  database (`terraform/bootstrap/platform_roles.tf` (`worker_firestore = {`),
  whose permission list ends at `"datastore.entities.update",`), bound once per tenant with no
  condition (`terraform/modules/tenancy/main.tf`
  (`resource "google_project_iam_member" "worker_firestore" {`)), and
  `register-tenant.sh` grants the same role (`scripts/register-tenant.sh`
  (`FIRESTORE_ROLE_ID="swarmTenantWorkerFirestore${ROLE_ID_SUFFIX}"`)). The
  module records the same residual (`terraform/modules/tenancy/main.tf`
  (`# still holds create/get/update across the database, so it can read a document`)).
  `attempts/<id>` is not guessable, so bumping another attempt's fencing
  `generation` needs an id it was never given. Every document in the table below
  is either guessable or one the worker creates itself.

#### Today: what a tenant worker can write, and what the control plane does with it

Read from the source on 2026-10-17 at `766b9af`; not exercised against the live
database. The full inventory, 30 rows, is
[the tenant settings integrity design §1.3](design/tenant-settings-integrity.md#13-the-inventory-every-document-the-control-plane-acts-on-that-a-tenant-worker-can-write).
The worst rows, each with the reader that acts on the forged value:

| document a worker in tenant A writes | what the control plane then does | who acts on it |
|---|---|---|
| `tenants/A.service_account` set to tenant B's worker account | **A's next Cloud Run attempt runs as B's account**, with B's secrets and B's GCS prefix. The dispatcher refuses only an empty value and hands the stored one to the Job; the scheduler holds `actAs` on every tenant's worker account. | `apps/scheduler/scheduler/dispatch.py::assert_tenant_identity`, `apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher._build_job` |
| `tenants/A.namespace` set to `swarm-tenant-B` | **A's next GKE pod runs in B's namespace, as B's KSA and so as B's account.** A stored namespace is kept whenever it starts with `swarm-tenant-`, which B's does. The reconciler scans and protects the namespaces the documents record. | `apps/scheduler/scheduler/dispatch.py::verified_namespace`, `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher.namespace_for`, `apps/reconciler/reconciler/store.py::ControlStore.tenant_namespaces` |
| `tenants/A.gcs_prefix` set to B's prefix | A's `GCS_PREFIX` names B's. The bucket grant is prefix-conditioned on A's id, so this costs failed writes rather than a leak. | `apps/scheduler/scheduler/dispatch.py::worker_env` |
| `pools/global.active` set to 0 (or huge) | At 0, admission counts nothing in flight and **the platform admits past its global ceiling**: invariants 2 and 3, and money. Huge, nothing is admitted. A worker writes `active` legitimately on every release, so nothing can tell its write from a forged one. | `apps/common/swarm_common/admission.py::evaluate_capacity`, `apps/common/swarm_common/admission.py::release_lease_in_transaction` |
| `pools/<any>` `.hard_limit`, `.enabled` | Raise A's own ceiling past the admin's; disable `pools/global` (every tenant parks) or B's pool. | `apps/common/swarm_common/admission.py::evaluate_capacity` |
| `leases/<new id>` created with chosen `pools[]` and `units`, then released | The release decrements every pool the lease names, so a worker can free capacity other tenants hold. It holds every permission that takes; the reconciler's stale-lease release does it too if the worker leaves it. | `apps/common/swarm_common/admission.py::release_lease_in_transaction` |
| `control/dispatch` `.dispatch_paused: true` | **All dispatch stops, for every tenant.** | `apps/scheduler/scheduler/store.py::SchedulerStore.dispatch_paused` |
| `workspace_settings/B.judgment_backend` (#1457) | B's judgments go to TypeSafe on B's key; with B listed in `TYPESAFE_FORWARDER_TENANTS`, B's live-browser pods get the inject label and swarm-api mints B's forwarder ticket. Two readers take the top-level field and one takes `code_intelligence.judgment_backend` first, so a forger can also make them disagree. | `apps/scheduler/scheduler/judgment.py::read_backend`, `apps/scheduler/scheduler/dispatch.py::judge_backend`, `apps/swarm-api/swarm_api/judgment.py::JudgmentSettings.read` (read by `apps/swarm-api/swarm_api/typesafe_ticket.py::TypesafeTicketService.mint`) |
| `workspace_settings/B` `.merge.*`, `.code_intelligence.*`; `tenant_settings/B` | B's merge mode and train, B's graph tickets, Jev and context delivery. | swarm-api merge and code-intelligence settings; the worker's graph stage |

The same role reads, too: `tenants/<B>` (its principal), `workspace_settings/<B>`,
`quota/*:<B>`, `accounts/*` and any task whose id leaks are readable today.

**The worker's own tenant checks are not a boundary.**
`apps/agent-worker/agent_worker/control.py::ControlPlane._assert_tenant` refuses
a document of another tenant, but it runs in a process the agent shares a uid
with, and the agent can mint the worker account's token from the metadata
server ([security.md](security.md#cloud-metadata-abuse)) and call Firestore with
`curl`. Every write above is a direct Firestore call, not a call through the
worker.

#### Phase 0 of #1457: what it closes once its lanes merge

**Not built on 2026-10-17.** The owner approved the design on 2026-10-17
(design B, phase 0 interim). Phase 0 is three lanes, and each changes nothing a
running worker can do; it changes what the control plane believes:

* **Derived identity, namespace and prefix (lanes ISO-SCHED and ISO-RECON).**
  The dispatcher takes the service account from
  `apps/common/swarm_common/identity.py::worker_service_account_id`, the
  namespace from the template and `GCS_PREFIX` from the tenant id, never from
  `tenants/<id>`, and logs any disagreement. `verified_namespace` stops keeping a
  stored namespace that merely starts with the prefix, and the reconciler's
  `tenant_namespaces` derives too. **This closes the first three rows of the
  table above for good**: a rewritten `tenants/A` no longer makes A run as B.
* **A MAC on `workspace_settings` (lane ISO-SETTINGS-MAC).** swarm-api signs the
  fields the control plane acts on (`judgment_backend`, `merge.*`,
  `code_intelligence.*`) with a key no worker holds, and the scheduler and
  swarm-api treat a missing or bad MAC as unset. Unset `judgment_backend` is
  `internal-jev`, which fails closed. **This closes #1457's instance** and the
  other `workspace_settings` rows.

**What phase 0 leaves open, stated so nobody reads it as closed:** every other
row. A worker can still zero `pools/global.active`, raise or disable any pool,
forge and release a lease, pause all dispatch through `control/dispatch`,
rewrite `tenants/<B>.enabled`, `.max_active` and `.credentials`, `quota/*:<B>`,
`accounts/*`, `tenant_settings/<B>`, and read everything it can name. A MAC
cannot close the pool, lease, task and attempt rows at all: the worker writes
those legitimately, and a signature cannot tell its write from a forged one.

#### Design B: what it closes, and the cutover order

The worker loses direct Firestore access. It reaches its state only through
worker-only swarm-api routes, `/v1/attempts/{attempt_id}/state/...`, each
checked against the caller's ID token (which proves the tenant) and an Ed25519
proof from the attempt key (which proves the attempt), in fenced transactions
that permit only the transitions a worker makes today. Reads move too, and the
`swarmTenantWorkerFirestore` binding is then removed. That closes **every row of
the inventory**, reads included: no tenant identity can change, or read,
another tenant's control-plane state.

The cutover is per tenant, and **every switch defaults to today's behaviour**:
nothing changes for a tenant until the owner switches it.

1. The routes ship in swarm-api, with minimum instances raised to 2, because
   every heartbeat then goes through it. The worker's remote client ships behind
   a per-tenant switch that is off.
2. **Dev's tenants are switched first**, then `make smoke concurrency-test
   race-test` runs against dev, because the change touches dispatch and
   reconciliation.
3. **Every tenant is switched.**
4. **Only then is the binding removed**: the tenancy module's
   `worker_firestore_access` defaults on until the last tenant is switched, and
   `register-tenant.sh` stops granting the role. A worker still on the old path
   after that fails its first read and its attempt fails retryably.
5. An acceptance probe, from a tenant-A pod, gets `PermissionDenied` on a direct
   update of `workspace_settings/<B>`, a get of `tenants/<B>` and a create under
   `leases/`. It is red until step 4 is applied.

**What design B does not close:**

* **The boundary is the attempt, not the process.** The agent can still mint
  its worker's ID token. Without the attempt key that token gets
  `403 attempt_unproven`. Where the worker's heap is not protected
  (`make_non_dumpable` does not report `PROTECTED`), the attempt still runs with
  its key flagged `unprotected` (owner decision, 2026-10-17), so its agent could
  read the key and do what its own worker does, within its own attempt and
  tenant, and nothing for another tenant. That is today's posture within a
  tenant, recorded rather than implied.
* **The four platform services keep database-wide access**, and every swarm
  identity can read and write any second Firestore database created in this
  project ([security.md](security.md#firestore-database-scoping-does-not-exist-verified-2026-09-16)).
* **Admission does not verify a task's spec signature.** The forger loses write
  access, which is what makes that unnecessary; the worker's spec check still
  holds invariant 10.

Until step 4 is applied for every tenant, the honest statement is that the
Firestore boundary is *narrowed*, not *closed*, and the grant is identical
whether a tenant was created by Terraform or by `register-tenant.sh` — the
script refuses to run at all if the custom role is missing rather than falling
back to `roles/datastore.user`.

The one place to be careful when extending the platform: **anything that accepts
a tenant id as input**. Admin routes do, and they are gated on admin
(`ADMIN_GROUPS` membership or `ADMIN_USERS`). A non-admin route that took a
`tenant_id` parameter would be the single change that undoes this table.

The one non-admin caller on the admin surface is an `ADMIN_POOL_USERS` entry
(the verification gate), and it reaches only the routes in
`swarm_api.auth.POOL_ADMIN_ROUTES`. Today that is
`PUT /v1/admin/limits/runner/{runner_profile}`, which takes a runner profile,
not a tenant id, so it opens no path from one tenant's id to another's data.
It is not harmless across tenants: a ceiling of 0 on a profile stops every
tenant's work on it being admitted until someone puts it back. Adding a route
that takes a tenant id to that allow-list would be exactly the change
described above. See [security.md](security.md#authentication) and
[the dated decision](audits/2026-09-22/race-test-needs-a-write.md).

---

## 5. Tenant lifecycle

```bash
# Register. A re-run rewrites the whole record, --providers included, so it is
# not how a provider is added: see --add-provider below.
./scripts/register-tenant.sh --group eng@saga.xyz --providers anthropic,openai

# Add a provider key. Never passed as an argument: argv is readable via ps.
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
# ...then let eng's worker read it and list it for the tenant, keeping every
# provider eng already has. NOT a re-run with --providers: that REPLACES the
# tenant's credentials list, and resets max_active, capacity_units and
# display_name to their defaults unless they are typed again. It grants only a
# secret whose labels say tenant=eng provider=anthropic and that holds an enabled
# version: swarm-tenant-<tenant>-<provider> does not split (eng-team's git and
# eng's team-git are one name), so the name alone cannot say whose key it is.
./scripts/register-tenant.sh --tenant eng --add-provider anthropic   # or: make add-provider TENANT=eng PROVIDER=anthropic

# Rotate, keeping the previous version enabled until workers pick up the new one
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
# then, once nothing is using it:
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin --disable-previous

# Limits. monthly_budget_usd is refused (422,
# apps/swarm-api/swarm_api/routes/admin.py::set_tenant_limits): there are no
# per-tenant budgets, built or planned (owner decision, 2026-10-01). Per-attempt
# cost IS recorded --
# apps/agent-worker/agent_worker/control.py::ControlPlane.record_spend --
# but only after the attempt has spent it, so it cannot bound admission;
# max_active and capacity_units do. PARKED(BUDGET_EXHAUSTED) stays in the
# frozen enum and nothing writes it (docs/cost-control.md section 2). The one
# carve-out is a schedule's own per-type dollar caps, which bound what its
# firings start and nothing else (owner decision SD4, 2026-10-08; same section).
./scripts/api.sh PUT /admin/tenants/eng/limits \
    '{"max_active": 25, "capacity_units": 50}'

# Pause one tenant without touching anyone else. --keep-scheduler is what makes
# that true: without it pause-swarm.sh also pauses swarm-scheduler-tick, the
# one-minute backstop that keeps admission moving for every tenant when a wake
# message is missed.
./scripts/pause-swarm.sh --tenant eng --keep-scheduler
./scripts/resume-swarm.sh --tenant eng

# Purge runtime data by collection or age (never infrastructure). Not the
# offboarding path: it exports the whole database first, and removes only the
# live version of each object.
./scripts/purge-data.sh --tenant eng --dry-run

# Offboarding a tenant's DATA: every object version under tenants/<id>/ and
# every Firestore record it holds, then -- only after a proof that each is
# gone -- the tenant document. For a tenant declared in tfvars, the tenant
# document and the pools terraform created are LEFT for terraform: deleting
# them here would let the next release recreate them enabled, so the id is
# released by the tfvars removal (runbook step 7), not by this script.
# Dry run by default. Refuses a tenant that is enabled, holds capacity or is
# still lent an account.
./scripts/offboard-tenant.sh --tenant eng
./scripts/offboard-tenant.sh --tenant eng --apply     # type "eng" to confirm

# Offboard a tenant entirely -- identity, keys, jobs, namespace, records:
#   docs/runbooks/tenant-offboarding.md
```

Offboarding is a runbook rather than a script because two of its steps are
decisions or waits, not commands: whether its people also lose access, and
draining its running work. Whether its data is kept is not a decision any
more. The owner decided on 2026-09-24 that it is deleted, and
`scripts/offboard-tenant.sh` is the runbook's step for that. Its terraform half
is one tfvars edit. Its GKE half, deleting `swarm-tenant-<id>`, is manual by
design, because the reconciler holds no ClusterRole and no longer tries to
collect namespaces (owner decision, 2026-09-24).
[The runbook](runbooks/tenant-offboarding.md) has the order and the checks.

New tenants start small — `default_tenant_max_active` 20, `capacity_units` 40 —
and an admin raises them. The failure mode of starting large is a new tenant
consuming the platform on their first bad loop.

**The tenant's pool ceiling is the smaller of the two.** `max_active` and
`capacity_units` bound one count: the units the tenant's running work holds,
where every task costs at least one. So every path that writes `tenant:<id>`
writes `min(max_active, capacity_units)` as its hard limit — the admin routes
(`store.set_tenant_limits`), a first sign-in (`store.ensure_tenant`),
`scripts/register-tenant.sh`, and Terraform's bootstrap (`pool_tenants` in
`terraform/infra/locals.tf`) — and the console's Tenants screen prints that
figure as `Enforced`. A new tenant at the defaults is capped at 20, not 40.
Until 2026-09-25 the script wrote `capacity_units` and Terraform wrote
`max_active`, so a pool one of them created before then can hold the larger
value; setting either limit through the admin route rewrites it.

**The tenant ceiling route sets both, so the ceiling is the number typed.**
`PUT /v1/admin/limits/tenant/{id}` — what the console's Pool limits editor
sends for a `tenant:` pool — writes `max_active` AND `capacity_units` to the
requested limit, and the pool becomes exactly that limit. On 2026-10-07 it
wrote `max_active` alone: the owner raised `smoke` 8 -> 20, `capacity_units`
stayed 8, the min() held the pool at 8, and the route answered 200 with a
pool of 8 that the console called saved. The owner decided the ceiling is one
number. The min() rule stays for `PUT /v1/admin/tenants/{id}/limits`, which
sets the two separately, and the ceiling route's response carries
`capped_by` (`capacity_units`, `max_active`, …, or null) so the console can
say "Saved, but the ceiling is still N" instead of a success when a pool did
not move.

---

## 6. The personal fallback tenant

A caller in no registered group gets `u-<local-part>` (so `alice@saga.xyz` ->
`u-alice`). Three properties:

* the `u-` prefix cannot collide with a group-derived tenant id, because group
  ids are the group's local part with no prefix;
* slugging is **not** allowed to be lossy. `eng.team@` and `eng-team@` both
  reduce to `eng-team`, and two distinct Google groups silently becoming one
  tenant — one namespace, one service account, one set of provider keys — is the
  exact cross-tenant merge this design exists to prevent. So whenever the slug is
  not a faithful rendering of the local part, `swarm_common.identity` appends a
  6-character digest of the full principal. `eng.team@saga.xyz` becomes
  `eng-9aef5b`, not `eng-team`;
* the tenant is real — own GSA, own prefix, own pools — so a personal task is
  isolated exactly like a group's.

This exists so that onboarding never requires a group change first. It is also
why `tenant:<id>` pools are created on demand rather than only by Terraform.
Until the workspace gate, a personal tenant's documents appeared the first
time its owner submitted, with no identity, namespace or bucket grant behind
them, and its work failed at dispatch. With `WORKSPACE_GATE` on, they are made
only by the workspace job, after an admin's approval, together with
everything behind them: [§7](#7-a-persons-personal-workspace).

### Service tenants: a service account's own `u-` tenant

A service account in no registered group resolves the same way, to
`tenant_id_for_user(<its email>)`. The workspace gate exempts service
identities. There are two kinds of service tenant. **The two that exist** are
declared in `terraform/environments/dev/dev.tfvars` as `kind = "user"` with the
account as its principal, and `terraform/infra/variables.tf` admits a user-kind
tenant only for a service account. **Since 2026-10-19 a new one is a service
workspace** made by the job, and `var.tenants` is frozen to the tenants it
already has (below). The two:

| Tenant | Principal | Why it is its own tenant |
|---|---|---|
| `u-sw-c90291` | `swarm-verify@saga-agents-staging.iam.gserviceaccount.com` | The verification gate's suites (smoke, e2e, race) run on the `mock` profile and need a tenant with jobs to dispatch to. |
| `u-or-9a0792` | `oracle-api@saga-agents-staging.iam.gserviceaccount.com` | The oracle-api client application submits workflows on its own behalf (owner decision 2026-10-18, #1568). |

**oracle-api is never a member of `eng@saga.xyz`, and never a
`service_accounts` listing under it.** Either would file its work in eng, under
eng's worker account, eng's provider keys, eng's GCS prefix and eng's
namespace (invariant 9), and against eng's ceiling. As its own tenant it
reaches nothing of anybody else's. It is in no registered group, so it has no
membership for `X-Swarm-Tenant` to choose, and a header naming `eng` is a
`403 tenant_not_member`.

Each service tenant has to be admitted in three places, and the three must
agree:

1. **The front door.** `frontend_iap_members` in
   `terraform/bootstrap/terraform.tfvars` grants it
   `roles/iap.httpsResourceAccessor` on both IAP backends. The owner applies
   that layer. The committed line is the record of the apply, because a `-var`
   not written there makes the next bootstrap plan remove the grant.
2. **swarm-api's domain check.** A service account's domain is never
   `saga.xyz`, so it is listed in `ALLOWED_USERS`
   (`terraform/infra/locals.tf`). That list widens which verified identities are
   admitted, never whether the token was verified.
3. **The tenant.** It is declared in `dev.tfvars` under the id the frozen
   module computes. Both ids carry a digest, because each address is lossy and
   too long to slug. They are recomputed by a test (`test_oracle_api_tenant.py`,
   `test_verify_identity_grants.py`) and never typed in from memory.

**A service workspace passes the same three places with no Terraform.** An
admin files it from Admin › Tenants › New workspace, and **only the platform
owner approves it** (owner answer Q2 (a)), because it is the one kind that
admits a new principal. The account must be this project's, with no `swarm-`
id and not on the shared deny-list (Q7 (a)). The job's step S1 checks that
and records the account's unique id. Then:

1. **The front door** is step S2: the account becomes
   `roles/iap.httpsResourceAccessor` on the API backend only, never the
   console's (`scripts/register-tenant.sh` (`ws_s2() {`)). It runs only once
   the owner has granted the deployer K1 (workspaces.md §0, O1). Until then,
   IAP refuses the call and the run stops for the owner as `FRONT_DOOR_OWNER`,
   and the owner adds the account to `frontend_iap_members` as above.
2. **swarm-api's check** is the registry, not `ALLOWED_USERS`: a `ready`
   service record admits its account
   (`apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry.admitted_services`),
   and only a token whose subject is the unique id S1 recorded. A record not yet
   `ready` answers `403 WORKSPACE_NOT_READY` instead of the domain message.
3. **The tenant** is step A8's `tenants/<id>`, under the same derived id.

The three agree because one record drives all three, and the inner gate,
swarm-api's, opens last: only once A9 has read every object back.

Both tenants declare **no providers** and run at 4/8. swarm-verify's suites
need no key. oracle-api is lent a pool Claude account by the owner instead of
holding a key of its own. Its forge token is `swarm-tenant-u-or-9a0792-git`,
stored with `scripts/create-secrets.sh --stdin` and never in the repository.

### Tenant ids are capped at 11 characters

A GCP service account id is capped at 30 characters. The identity is
`swarm-agent-worker-<id>`, and that prefix is 19 characters, so a tenant id may
be at most **11**. Both provisioning paths enforce it —
`terraform/modules/tenancy/variables.tf` and `register-tenant.sh` — and neither
truncates to fit. Truncating is what turns a length limit into a security bug:
with a 30-character cut, every tenant id agreeing in its first 11 characters
collapses onto one service account, and both tenants' `secretAccessor` bindings
and both tenants' GCS prefix conditions then accumulate on it, each able to read
the other's provider keys.

`swarm_common.identity` derives ids to the same cap, from the same prefix
(`_GSA_PREFIX`), so every id the API resolves to is one both paths can
provision. It shortens rather than truncates: a slug that would overrun keeps
its first few characters and gains the 6-character digest, so two principals
that share a long prefix still get different ids.

| Principal | Resolves to |
|---|---|
| `eng@saga.xyz` | `eng` |
| `research@saga.xyz` | `research` |
| `platform-eng@saga.xyz` | `plat-af1226` |
| `eng.team@saga.xyz` | `eng-9aef5b` |
| `alice@saga.xyz` | `u-alice` |
| `alice.smith@saga.xyz` | `u-al-36b536` |
| `swarm-verify@saga-agents-staging.iam.gserviceaccount.com` | `u-sw-c90291` |
| `oracle-api@saga-agents-staging.iam.gserviceaccount.com` | `u-or-9a0792` |

The cost is readability: past 11 characters an id is mostly digest. This
section used to record an unresolved conflict here — the frozen module capped
ids at 22, sized for register-tenant.sh's old `swarm-t-` prefix, so ids of 12 to
22 characters resolved and could not be provisioned. The frozen module has
since moved to the one prefix that exists, and `swarm-t-` is gone from every
provisioning path and from the quota broker's allow-list (#176).

---

## 7. A person's personal workspace

A person's `u-<you>` tenant is a **workspace**: the same boundaries a group
tenant has (worker account, artifact prefix, forge slot, namespace, tenant and
pool documents), made for one person after an admin approves it. The design,
with every owner decision, is [workspaces.md](workspaces.md) (#847); this
section is what exists and how it is operated.

**Why it is not Terraform and not first-sight creation.** Two things failed
before it. A personal tenant written on first sight had documents and nothing
behind them, so its work failed at dispatch with a missing-identity error
(§6). And a personal tenant in tfvars puts the list of people in a public
repository and makes every new person a release. So people live in Firestore
(`workspaces/`, `people/`), and a guarded job makes each person's resources
(WD3, WD4). Since 2026-10-19 the same job makes new group and service
workspaces too, filed from Admin › Tenants. Terraform keeps only the tenants it
already held, with `var.tenants` frozen to them
([design/workspace-kinds.md](design/workspace-kinds.md) §8).

### 7.1 Request, approve, apply

| step | where | what happens |
|---|---|---|
| request | the console's setup checklist ("Request your workspace"), `sc setup` / `/sc:setup`, or the bridge tool `swarm_setup_workspace` | `POST /v1/workspace` writes `workspaces/u-<you>` in `requested`, with an opaque id `w-` and six hex digits. An admin's own request is approved in the same call |
| request, by an admin, any kind | **Admin › Tenants › New workspace**: Personal (naming the person), Group or Service | `POST /v1/admin/workspaces` with `{kind, principal, display_name}`. A personal or group record is approved in the same call. A service record waits for the platform owner |
| approve | **Admin → People**, the **Waiting for you** card: every open request, oldest first, with Approve and Deny beside it (`apps/swarm-ui/src/People.tsx` `WaitingCard`) | `POST /v1/admin/workspaces/{workspace_id}/approve` records the approver and publishes the workspace id. A denial needs a reason, shown to the person |
| start | Pub/Sub topic `swarm-workspace-apply` → Eventarc trigger → the Workflows definition `swarm-workspace-apply`, running as `swarm-workspace-dispatch` | the workflow checks the id against `^w-[0-9a-f]{6}$` and the mode against `create\|limits`, then runs the Cloud Run job with those two arguments and nothing else |
| apply | the **Cloud Run job `swarm-workspace-apply`**, as `swarm-workspace-deployer` | `images/workspace-apply/entry.py` discards the environment it was given, `scripts/workspace-apply.sh` installs the call guard, and `scripts/register-tenant.sh --workspace <w-id>` runs A1 to A9 |

It is a Cloud Run job in the project, **not** Cloud Build and not a GitHub
workflow (WD2, re-decided 2026-10-10). Why: the job's logs stay in the
project, out of a public CI log, and swarm-api, the service every user's token
reaches, holds only `pubsub.publisher` on one topic. It can publish an opaque
id; it cannot choose the job's arguments, environment, task count or timeout.
Publishing is `WORKSPACE_APPLY_PUBLISH` (`workspace_apply_publish`, on in dev
since 2026-10-11). The Cloud Scheduler job `swarm-workspace-sweep` calls
`POST /v1/admin/workspaces/sweep` every 10 minutes: it re-publishes an approved
record whose execution never claimed it, applies pending loans, and logs
`workspace_stuck` for the `workspace-stuck` alert.

**The steps**, each recorded on the record as `steps.<id>` (which is what
People shows live):

| id | step |
|---|---|
| A1 | claim the record in one transaction: `approved` (or `failed` / `needs_owner` with an admin's retry recorded after the last claim) by an email that is an admin in `admin_roles/` **now** (for a service, the platform owner); write the guard's expectation file from it. A group whose directory check is over 24 hours old fails `DIRECTORY_CHECK_STALE` |
| S1 | a service only: read the account. It must be this project's, enabled, not `swarm-`, not on the shared deny-list. Record its unique id. Its IAM is never written |
| A2 | inspect an existing worker account, read only; foreign bindings or a user-managed key stop the run |
| A3 | create `swarm-agent-worker-u-<you>` if absent |
| A4 | Workload Identity for the namespace's two Kubernetes service accounts; act-as for the scheduler and the reconciler |
| A5 | the two `tenants/u-<you>/`-conditioned bucket grants, the `.tenant` marker (create-only, `ifGenerationMatch=0`), and, on the WD9 fallback, the worker's Firestore, telemetry and bucket-metadata roles |
| A6 | the empty forge slot and its `-refresh` twin; the worker reads the slot, never the twin. A group's slot is the team slot `swarm-tenant-<g>-git`; a service's has no twin |
| A7 | the namespace, `kubernetes/apply.sh`, with the record's quota; a group's or service's carries `swarm.saga.xyz/workspace: <w-id>` |
| A8 | the tenant and pool documents with the record's limits |
| S2 | a service only: admit the account at the front door, on the API backend only |
| A9 | re-read every object made; only then `ready` |

A ceiling change from People runs `--mode limits`: A1, A7 and A8 only, no IAM
call.

### 7.2 What bounds the job

The deployer identity holds account-IAM power that Google cannot narrow to a
name prefix in this shared project (workspaces.md §2.4). Three things bound it
instead, and each was needed on the first real run:

* **The call guard** (`scripts/lib/workspace-guard.sh`, rules in
  `scripts/lib/workspace-calls.json`). The job's `gcloud`, `kubectl` and `curl`
  are shims in `scripts/lib/guard-bin/`; a call runs only if one rule matches
  it whole, with every member and condition checked against the approved
  record. No removal, deletion or key creation is ever allowed (C9). A refused
  call never reaches Google or the cluster: the record goes to `needs_owner`
  with the step, and People shows "Waiting for the platform owner".
* **The deployer's admission policies** on the swarm cluster
  (`kubernetes/policies/workspace-provisioner-scope.yaml`):
  `swarm-workspace-deployer-scope` and its `-namespaces`, `-roles` and
  `-rolebindings` companions. Server-side, the deployer may only create or
  update a `swarm-tenant-u-*` namespace, or one it created carrying the
  `swarm.saga.xyz/workspace` label, and the kinds and names the tenant render
  produces in it, with Roles carrying exactly the rendered rules. It refuses
  by name every namespace Terraform declares, and an update that adds the
  label to an unlabelled namespace. This is what turns the deployer's cluster
  RBAC from "any namespace" into "a namespace the job made". The policy's allowed NetworkPolicy names
  include `swarm-allow-live-egress`, and an absent or null `rules` list on the
  empty `swarm-worker` Role means no rules (PR 1220).
* **The WD9 fallback is on.** IAM does not accept one grant to "every personal
  worker" (W0), so the job grants each worker its database, telemetry and
  bucket-metadata roles itself at A5, under guard rules C4 and C5 that admit
  exactly those roles for exactly that member (`wd9_fallback: true`, owner
  decision 2026-10-12, PR 1217).

### 7.3 Where the job's logs are

Cloud Run writes the execution's output to Cloud Logging as
`resource.type="cloud_run_job"`, `resource.labels.job_name="swarm-workspace-apply"`.
The bootstrap's sink routes those entries to the **restricted log bucket
`swarm-workspace-apply`**, and a project exclusion of the same name keeps them
out of `_Default`. Why: `saga-agents-staging` is shared, and anyone with log
viewing there reads `_Default`. The bucket is readable by the project's owners
and logging admins and by `workspace_log_readers`. Log lines name the workspace
id only; the record holds a failure **code**, and the console serves the copy
from it. Read an execution's lines from the bucket's log view, or open the run
link on the person's row in People.

### 7.4 After ready: a Claude account, retries, the gate

* **A ready workspace runs nothing until it has a Claude account** (WD6). The
  person adds their own, or asks for a loan (`POST /v1/workspace/loan-request`,
  the checklist's second step), and an admin lends a group's account from
  People (`PUT /v1/admin/people/{workspace_id}/loan`). A lend to a workspace
  not yet `ready` is **pending**, not refused: the worker account it must
  grant does not exist yet, so the sweep applies it once the workspace is
  ready. **Reclaim** lets running work finish; new claude-code work parks
  `CREDENTIAL_MISSING`.
* **Retry.** A `failed` or `needs_owner` row has **Retry**,
  `POST /v1/admin/workspaces/{workspace_id}/retry`, which records the admin's
  retry and publishes again. Every step reads before it writes, so a re-run
  makes only what is missing.
* **The gate.** `WORKSPACE_GATE` (`workspaces.gate_from_env`), off by default.
  On, a submission that resolves to a personal tenant whose workspace is not
  `ready`, or that has no Claude account, is refused (`403
  WORKSPACE_NOT_READY`, `NO_CLAUDE_ACCOUNT`), and `tenant_for` no longer writes
  `tenants/u-*` on first sight. Group submissions are never gated (WD7). It
  ships off and is turned on per environment, in its tfvars
  (`workspace_gate`, rendered by `terraform/infra/locals.tf`), only after one
  real approval has gone end to end (WD8): turning it on refuses every person
  without a `ready` record. **Dev: on from W10 (2026-10-13), once its pull
  request is merged; prod: off.**

### 7.5 The first end-to-end run, 2026-10-12 to 13

The evidence that the path above works, read live on 2026-10-13:

* `devops@saga.xyz` requested `w-7f9285` (tenant `u-devops`) at
  2026-10-12 22:31Z, and an admin approved it at 2026-10-13 00:02Z.
* The first executions stopped, each on something only a real run could show,
  and each was fixed: the deployer's Firestore `datastore.databases.get`
  (PR 1216); the static walk of every call the job makes, which found the
  `.tenant` marker had to be create-only, added `iam.serviceAccountKeys.list`,
  and turned the WD9 fallback on (PR 1217); `workspace_apply_image` re-pinned
  to that image (PR 1219); and the deployer's Role policy treating a null
  `rules` list as no rules, with the scope policy re-applied to admit
  `swarm-allow-live-egress` (PR 1220).
* The Cloud Run job's execution of 05:03 to 05:05Z ran A1 to A9, and the
  workspace reached `ready` at 05:05Z. `eng:devops-main` was then lent to it
  (loan state `lent`).

That run is the precondition WD8 names for turning `WORKSPACE_GATE` on.

### 7.6 Offboarding a person, today

There is no deprovisioning run yet (workspaces.md §7). The guard refuses every
removal (C9) on purpose, so the job cannot undo what it made, and the separate,
owner-approved deletion mode §7 describes is not built. A person's tenant is
offboarded by hand with
[the offboarding runbook](runbooks/tenant-offboarding.md#a-personal-workspace-made-by-the-workspace-job),
which says what it removes and what is still left by hand. A group or
service workspace has
[its own section there](runbooks/tenant-offboarding.md#a-group-or-service-workspace-made-by-the-workspace-job):
the record is moved off `ready` first, which drops it from tenant resolution
at once, and a service's front-door member is removed by the owner.
