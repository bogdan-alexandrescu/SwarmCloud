# task_ff941315b37e4fbfa42f  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit apps/swarm-api/ for any path where a caller could act as a tenant they are not. Trace how tenant_id is derived and every place it could be influenced by request content rather than by the verified identity. CONTRACT.md invariant 9 is the rule. Give concrete request shapes, not general concerns. At most 5.

## Report

I have enough to write a precise, evidence-backed report.

## Audit: tenant_id derivation in `apps/swarm-api/`

**How it's supposed to work:** every route depends on `current_auth` (`deps.py:139`), which calls `Authenticator.authenticate()` (`auth.py:173`). That verifies a Google ID token, resolves group membership via `MembershipResolver.groups_for()`, and calls the frozen `resolve_tenant()` (`identity.py:101`) to get `tenant_id`. Every route then reads `auth.tenant_id` — never a body field, query param, or header. `schemas.py` uses `extra="forbid"` and no model anywhere has a `tenant_id` field. That part of invariant 9 is solid — I could not find a route where `tenant_id` is taken from request content.

The real gap is that **two different verified identities can be handed the *same* `tenant_id` string**, and once that happens, most routes trust the string with no further check.

### 1. The personal-tenant prefix collides with a group's own tenant id (`identity.py:45-89`)

`tenant_id_for_group(email)` = `_slug(email)` (no prefix). `tenant_id_for_user(email)` = `_slug(email, prefix="u-")`. The docstring at `identity.py:88` claims the `u-` prefix means a personal tenant "cannot collide with a group" — but nothing stops a *group's* local part from itself starting with `u-`, since the group path adds no prefix at all.

Concrete collision: admin registers `TENANT_GROUPS=...,u-eng@saga.xyz,...` (a plausible name — "unified-eng", abbreviated). `tenant_id_for_group("u-eng@saga.xyz")` → local `"u-eng"`, unchanged by slugging → returns `"u-eng"`.

Now any ordinary employee `eng@saga.xyz` who is **not** a member of `u-eng@saga.xyz` sends any authenticated request, e.g.:
```
POST /v1/tasks
Authorization: Bearer <eng@saga.xyz's real Google ID token>
{"runner_profile": "default"}
```
`resolve_tenant()` finds no group match, falls back to `tenant_id_for_user("eng@saga.xyz")` → local `"eng"` → returns `"u-" + "eng"` = `"u-eng"` — **identical** to the group's tenant id. `eng@saga.xyz` is now the `u-eng` tenant: same GSA, same secret prefix, same GCS prefix, same namespace as the `u-eng@saga.xyz` group. `store.py:232` even hard-codes `kind = "user" if tenant_id.startswith("u-") else "group"`, so the store itself assumes this never happens.

### 2. The read/cancel routes never re-check the collision guard that write routes have (`store.py:254-275`)

`_assert_principal_matches()` is the *only* place that would catch a tenant_id collision, and it only runs inside `ensure_tenant()`, reached solely via `SubmissionService.tenant_for()` — called from `submit_tasks`, `submit_workflow`, `providers()`, `get_me`, and `put_credential`. It is **not** called from `list_tasks`, `get_task`, `cancel_task`, `list_events`, `list_artifacts`, `list_workflows`, `get_workflow`, `cancel_workflow`, `stats()`, or `capacity()` (`routes/tasks.py:64-142`, `routes/workflows.py:27-66`, `service.py:271-332`) — those all just filter Firestore by the raw `tenant_id` string.

So continuing scenario 1: `eng@saga.xyz` can immediately do
```
GET  /v1/tasks
GET  /v1/tasks/{task_id}
POST /v1/tasks/{task_id}/cancel
GET  /v1/tasks/{task_id}/events
GET  /v1/workflows
```
and read, and even **cancel**, every task/workflow belonging to the real `u-eng@saga.xyz` group — with a valid, verified token for a completely unrelated identity, and no error, because the only collision check the codebase has is skipped on every one of these paths. The comment at `routes/tasks.py:3-6` explicitly claims "there is no request a caller can construct that reads another tenant's task" — this is the counterexample.

### 3. Same root cause reachable without the `u-` trick, if the deployment allows multiple hosted domains

`_slug()` discards the domain entirely (`identity.py:65`: `principal.split("@", 1)[0]`). If `ALLOWED_DOMAINS` (in the frozen `Settings`) is ever configured with more than one domain (e.g. onboarding a partner org), then `TENANT_GROUPS=eng@saga.xyz,eng@partner.com` collide outright — `tenant_id_for_group` returns `"eng"` for both. Whichever group's member submits a task first "wins" the tenant document (`ensure_tenant`, `store.py:228-231`); the other group is either blocked with `409 Conflict` the moment any of its members hits a *write* route, or — via finding 2 — can read/cancel the first group's work indefinitely via the unguarded read routes, never triggering the conflict check at all.

---

**Fixes belong in the frozen `apps/common/swarm_common/identity.py`** (I did not edit it, per CONTRACT.md) — reserving the `u-` namespace so no group slug can produce it (e.g. group slugs get a distinct required prefix, or the digest-collision logic also fires when a group slug would start with `u-`), and in `apps/swarm-api/swarm_api/store.py` — every read/cancel/list method should assert the tenant document's registered principal matches the caller's, the same way `ensure_tenant` does, not just the write paths.
