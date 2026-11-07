# PR #3358: merge of origin/main

Merged origin/main (feaab78) into swarm/task_5b03097c5af2447bbdf7 with a merge commit (5c25905). Main does
not contain this PR's change: it has no `variable "platform_memberships"`, no PLATFORM_MEMBERSHIPS rendering
and no dev switch.

## Conflicts
- `docs/design/agent-quality/known-failures.md`: main replaced the count table (this PR moved skipif from
  221 to 222) with one row per skip location, and dropped the counts on purpose. Resolution: I kept main's text and
  added the row `tests/unit/scripts/test_join_acceptance_probe.py | <module> | skipif | the probe needs bash, jq and base64`
  for this PR's skipif, in path order. This is the row `test_agent_quality_inventory.py` asked for.
- Git merged dev.tfvars, locals.tf, variables.tf, docs/multi-tenancy.md and docs/notifications.md automatically, with no conflict.

## Tests
- `uv run pytest -q tests/unit/scripts/test_agent_quality_inventory.py`: before the row was added, 1 failed (the unlisted
  join-probe skipif); after, 23 passed.
- `uv run pytest -q tests/unit/scripts/test_agent_quality_inventory.py tests/unit/install/test_manifest_complete.py
  tests/unit/install/test_tfvars_switches.py tests/unit/scripts/test_join_acceptance_probe.py
  tests/unit/control_plane/{test_join_metrics,test_join_one_store,test_join_requests_routes,test_join_requests_store,test_join_wire,test_notifications,test_notifications_join,test_notifications_join_wire,test_platform_memberships_resolve,test_workspace_memberships,test_admin_membership_unresolved}.py`:
  272 passed.
- `uv run pytest -q tests/unit/install/e2e/test_day_one_walk.py`: 5 passed.
- `scripts/publish-scan.sh`: clean.
- Not run: `scripts/related-tests.sh --base FETCH_HEAD --run`. Because swarm-api modules changed, it selected
  several hundred files, so I ran the named subset above.
- Not run: `terraform test -filter=platform_memberships_env.tftest.hcl`. The container has terraform 1.17.1 and
  the repo pins ~> 1.16.2, so init refuses. CI runs it.
