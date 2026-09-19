# task_a23c4d41cf3f4771ab52  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit apps/quota-broker/ - especially accounts.py, accountstore.py and credentials.py, which were written today. Look for ways an account could be assigned to a tenant that does not own it and was not lent it, or a refresh token lost, or an account silently stop being refreshed. Give concrete sequences. At most 5.

## Report

Audit complete. This module (`accounts.py`/`accountstore.py`/`credentials.py`) is not yet wired to any assignment API or scheduler — the only live path today is the `/v1/quota/sweep` refresh loop in `main.py`. Within that scope, four concrete issues:

**1. `REAUTH_REQUIRED` is never actually written back to the account — the whole point of the state is defeated.**
`main.py:500-548`'s sweep endpoint calls `refresher.sweep_accounts(...)` and reads `reason == "reauth_required"` only to populate the HTTP response (`main.py:272`). Nowhere does it call `account_store.set_state(account_id, AccountState.REAUTH_REQUIRED, ...)`. Sequence:
- Account `acme:ci`'s refresh token is revoked.
- Sweep tick: `_refresh` in `credentials.py:172-180` catches `ReauthRequired`, logs it, returns `reason="reauth_required"`. Firestore doc for `acme:ci` still has `state: AVAILABLE`.
- Every subsequent tick, `due_for_refresh()` (`accounts.py:338-356`) still includes it (state never changed), so the broker retries the dead token forever — exactly the "burn the endpoint's rate limit rediscovering the same answer" scenario the module's own docstring warns against.
- `choose()` still treats it as `ASSIGNABLE` (`accounts.py:86,321`), so new agents keep getting dispatched onto an account whose access token secret is never refreshed again — they fail auth, and `scripts/account.sh list` still reports it as `AVAILABLE` with no `reason`, so an operator has nothing pointing them at the actual dead account.

**2. Secret name collision across tenant/label boundaries → real cross-tenant credential exposure.**
`secret_name()` (`accounts.py:97-102`) and `scripts/account.sh`'s `secret_base()` both build `swarm-account-<owner_tenant>-<label>` by plain concatenation, with no delimiter protection. Tenant ids and labels both permit internal dashes (`account.sh:76`, `accounts.py:56`), so two different, both-valid registrations collide:
- `scripts/account.sh add --tenant acme-prod --label x --stdin` → account `acme-prod:x`, secret `swarm-account-acme-prod-x` (verified: `secret_name("acme-prod","x") == secret_name("acme","prod-x")`).
- `scripts/account.sh add --tenant acme --label prod-x --stdin` → account `acme:prod-x`, **same secret name**. `grant_access` (`account.sh:102-141`) now binds `swarm-agent-worker-acme`'s IAM identity as `secretAccessor` on the secret that also holds `acme-prod`'s live refresh/access tokens, and if this operator later uploads their own credential, `gcloud secrets versions add` silently adds a version on top of `acme-prod`'s, effectively hijacking it.
- Result: when `choose()` assigns tenant `acme` its own account `acme:prod-x` (no lending involved at all — `may_serve` is happy), the worker mounts a secret carrying `acme-prod`'s actual Claude credential. Direct CONTRACT.md invariant-9 violation, and it requires no attacker action, just two ordinary onboardings with tenant/label names that happen to concatenate the same way (`u-bogdan`/`prod-x` vs `u-bogdan-prod`/`x` is an equally realistic collision given `u-<user>` personal-tenant convention already called out as dash-ambiguous in `secretstore.py`'s own docstring).

**3. No mutual exclusion on the sweep → duplicate concurrent refreshes can race-invalidate a refresh token.**
`terraform/modules/scheduler/jobs.tf:70-99`: `quota_refresh` has `attempt_deadline = "300s"` and `retry_count = 2`; Cloud Scheduler does not cancel the original request on timeout, it just fires another. `cloud_run/main.tf:28` sets default concurrency 80, so a single instance can genuinely run two sweeps at once. `credentials.py`'s own module docstring explains why this is dangerous ("an exchange can return a new refresh token and invalidate the one presented... N workers running concurrently would each invalidate the others") but the broker's own sweep endpoint has no lock, lease, or idempotency guard against exactly that happening to itself:
- Sweep A and Sweep B both read the same stored refresh token `r1` for `acme:ci`.
- A exchanges `r1` → provider returns `r2`, A persists it.
- B (already in flight) exchanges `r1` again → provider now returns `invalid_grant` for the just-rotated-away token → B's `_refresh` raises `ReauthRequired`, logs `"subscription credential needs a human; tasks will park"` for a perfectly healthy account, and (combined with finding #1) at minimum produces a false alarm; every other account refreshed by the losing sweep in that same tick has its exchange call wasted against the token endpoint's shared rate limit too.

**4. `Account.with_reading()` replaces the whole `windows` dict rather than merging per key — a later partial reading can erase a still-binding one.**
`accounts.py:225-237`: `with_reading` does `replace(self, windows=dict(windows), ...)`, a full overwrite, even though the module's own docstring says windows are "keyed rather than fixed" specifically so a new one can appear without erasing others. If two readings for the same account carry different subsets of windows (plausible once something wires up `record_reading` from multiple pods, since the `stream-json` events are per-window), a later report containing only `seven_day` would silently discard a `five_hour` reading that was the binding (worse) one, making `_binding_remaining()` compute headroom from a stale, rosier picture. This one is currently unreachable — nothing in the repo calls `record_reading` yet — but it's a live landmine for whoever wires that path up next, worth fixing now while the intended "keyed, not fixed" semantics are still fresh.

Findings 1 and 2 are the ones I'd fix first — both are reachable today through the code that's actually wired up (the sweep endpoint and `scripts/account.sh`), and finding 2 in particular is a direct violation of CONTRACT.md invariant 9 with no attacker required, just two ordinary account onboardings.
