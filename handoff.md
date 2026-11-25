# Handoff: review fixes

## Finding 1 (wrong-project.jq, major): fixed
`scripts/lib/wrong-project.jq` now fails closed. A change is flagged when:
- any `project`, `project_id` or `*_project` attribute, at any depth, in either half, names another id;
- any string holds a `projects/<id>/` path with a foreign id;
- `after_unknown` marks such an attribute unknown (reported as "(unknown at plan time)");
- the type is organization, folder, billing or `google_project` level.

Own project number, `-`/`_` and the Google-published image project `deeplearning-platform-release` are allowed. The own number is read from `google_project` data in the plan, or from the `name`/`id` of a resource whose project is the install's. This was needed because the real destroy fixture's paths use the number.

Providers: `provider_projects` flags a literal `project` in an aliased provider config. A reference to a variable is not a literal and is not read.

The engine (`planguard.py`) and `destroy-guard.jq` both call `plan_wrong_project`. The shell guard still only looks at deletions, as before.

Tests (`test_plan_guard_wrong_project.py`): the unknown-value pass test is replaced by a refusal test. New parametrized cases cover a `projects/<other>/` path, `crypto_key_id`, `host_project`, a nested `subnetwork_project`, organization/folder/billing types and an unknown `host_project`. Each case is checked through the engine and the shell guard. Further tests cover own-number passes and the provider case.

## Finding 2 (inventory.py, major): fixed
`off_means_empty` now defaults to False. Only `cluster` sets it True. Every other kind reports `not read`, so the exit code is 3 and the listing is not complete. This includes network, subnet, firewall and address, which the finding did not list. I could not verify that an API being off means nothing exists for those kinds, so I failed closed.

New parametrized test `test_an_api_that_is_off_is_not_read_never_empty` covers 15 kinds. `docs/install.md` and the module docstring are updated.

## Verified
- `tests/unit/install` without e2e: 1583 passed.
- `tests/unit/scripts` filtered on guard/destroy/plan: all pass (including `test_verify_destroy_guard_record.py` after the own-number fix).
- `tests/unit/install/e2e`: 9 fail identically on the base commit ("jq printed no verdict"), so they are not caused by this change. I did not investigate them.
