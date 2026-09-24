# task_6d14a516860740d58942  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit tests/ for tests that would PASS while the thing they describe is broken. Two were found today: both asserted that nothing was written, which quietly encoded a bug where writing nothing was the failure. Look especially for assertions about what did NOT happen, and for fakes that accept calls the real object would reject. Name the test, what it claims, and how it could pass while broken. At most 6.

## Report

Good — the two known bugs are already fixed and documented in place (`test_credential_refresh.py:189-198`). Both dedicated audits, plus my own read of the three integration tests, came back clean.

## Result

No additional instances of the pattern were found. Both background audits (18 files in `tests/unit/control_plane/`, 15 files in `tests/unit/worker/`) and my own read of `tests/integration/*.py` and both `fakes.py` files turned up nothing new:

- Every "nothing happened" assertion (`store.writes == []`, `dispatcher.dispatched == []`, `mock.assert_not_called()`, etc.) is paired with a positive assertion that pins down a concrete value — a state transition, an event type, an exit code, a specific field — so the test can't pass merely because the code path was deleted or no-opped.
- Tests whose *entire point* is a negative-safety property (fencing, tenant isolation — e.g. `test_fenced_worker_does_not_touch_the_lease`) are legitimately testing "nothing happens," and each is paired with proof the code path was actually reached (`ExplodingChildProcess` that raises if ever invoked, an explicit `ExitCode.GENERATION_FENCED`/`TENANT_MISMATCH`) — so a deleted fencing check would still fail these, not silently pass.
- Both `fakes.py` files enforce real Firestore rejection semantics (`update()` on a missing doc raises, `where()` without `filter=` raises, batch commit caps at 500 ops) and other inline fakes (`FakeBackend.delete_job_resource`, `FakeSecretClient`) reject or validate the way the real backends would, rather than silently accepting anything.
- The two bugs found today are already fixed and self-documented at `tests/unit/control_plane/test_credential_refresh.py:189` and `test_account_refresh.py:174` — their docstrings explicitly describe the old `writes == []`-as-success trap and why the reseeded version now asserts something meaningful.

I'm not padding this list — genuinely nothing further fit the pattern.
