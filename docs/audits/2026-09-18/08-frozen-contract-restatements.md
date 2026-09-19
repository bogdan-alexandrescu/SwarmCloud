# task_e20c3e8ca3a94a37950e  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Read apps/common/swarm_common/ - the frozen contract - and find any place where ANOTHER module restates one of its rules instead of importing it, so the two could drift apart. Today a shell script and a Python module were found restating the same account-label regex. For each, name both copies and say what breaks if they diverge. At most 6.

## Report

All findings verified against the current tree. Here's what restates a `swarm_common` rule instead of importing it:

**1. The tenant/name slugification regex — restated 3 times**
Canonical: `_TENANT_SAFE = re.compile(r"[^a-z0-9-]+")` in `apps/common/swarm_common/identity.py:32`, used inside `_slug()` to derive tenant ids.
Copies: `apps/scheduler/scheduler/dispatch.py:64` (`_NAME_SAFE`), `apps/reconciler/reconciler/detect.py:78` (`_NAME_SAFE`), `kubernetes/render.py:68` (`_NAME_SAFE`) — all three re-declare the identical pattern instead of `from swarm_common.identity import _TENANT_SAFE`, even though dispatch.py and render.py already import other swarm_common symbols.
**Breaks if diverged:** the reconciler's `sanitised()` reverse-maps a Job/annotation name back to a real Firestore task/attempt id by re-slugging and looking it up in an index (`detect.py:88,111,113`). If identity.py's escaping rule ever changes (e.g. to also fold underscores or dots differently) and the reconciler's copy doesn't follow, `sanitised()` stops round-tripping — the reconciler can no longer match a running Cloud Run execution back to its task, so it either leaks capacity (treats a live execution as orphaned and kills it) or fails to detect a truly orphaned one.

**2. The GSA tenant-id length budget — already stale in one copy**
Canonical: `_GSA_PREFIX`/`_MAX_TENANT_ID` in `identity.py:40-42`, whose own comment records a past incident ("an earlier value of 22 was computed from a `swarm-t-` prefix that no longer exists").
Copy: `scripts/register-tenant.sh:150-173` computes `MAX_TENANT_ID` correctly (11, from its own `GSA_PREFIX="swarm-agent-worker-"`), but its `die` message text still says *"swarm_common.identity caps a tenant id at 22, sized for the prefix 'swarm-t-' (8 chars)"* — a restatement of identity.py's old rationale that was never updated when identity.py was fixed.
**Breaks if diverged:** an operator hitting this failure during tenant onboarding is told a wrong cap (22 vs the real 11) and a nonexistent prefix, sending them chasing a mismatch that no longer exists instead of the real one.

**3. `CONCURRENCY_STATES` (which states hold capacity) — restated as a literal list**
Canonical: `frozenset({LEASED, DISPATCHED, STARTING, RUNNING})` in `apps/common/swarm_common/states.py:35-42`.
Copy: `scripts/status.sh:235` hardcodes `[.tasks.LEASED, .tasks.DISPATCHED, .tasks.STARTING, .tasks.RUNNING]` for the "holding cap" line, not derived from the enum.
**Breaks if diverged:** if a future state is added to `CONCURRENCY_STATES` (or one is removed), `make status`'s "holding cap" figure silently stops matching what the admission transaction actually counts — an operator reads the dashboard as healthy while real pool usage is different, during exactly the incident where that number matters.

**4. Account-label validation — the pair already found, plus a third, already-diverged copy**
Canonical(-ish): `_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")` in `apps/quota-broker/quota_broker/accounts.py:56`.
Known copy: `scripts/account.sh:68`, same pattern, comment admits "Same expression as `quota_broker.accounts.validate_label`."
**Undetected copy:** `kubernetes/render.py:123` — `"ACCOUNT_LABEL"` uses the generic 63-char RFC1123 pattern (`{0,61}`) instead of the 40-char account rule, and this one has already drifted from the other two. Since `--account` (`render.py:279`) is an operator-supplied CLI flag, render.py's looser check will accept a 41-63 char label that could never have been legitimately created by `account.sh`/`accounts.py`, defeating render.py's own stated purpose of validating every interpolated value before it reaches the manifest.
