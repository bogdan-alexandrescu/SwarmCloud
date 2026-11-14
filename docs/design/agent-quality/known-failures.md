# Known failures at the baseline

What is skipped, expected to fail or quarantined on main at
`3e3a9875458719f438df1c2d426ae1844b4122b3` ([baseline](baseline.md)), and the
open defects documented in the program's areas, each with where it is recorded.
An experiment reads this page first: a failure listed here is not the
experiment's finding, and a skipped test is not a passing one. The skip table
in [§3](#3-every-skip-by-location) is kept to the current tree, not the
baseline's: the check below fails when the two differ.

## 1. Skipped, expected-to-fail and quarantined tests

Every skip, expected failure and `importorskip` in a `.py` file under `tests/`
is listed in [the table below](#3-every-skip-by-location), one row per file, test
and kind, with its reason; every `tests/flaky.txt` entry is listed in
[the quarantine](#the-quarantine). `scripts/agent_quality_inventory.py` finds
them in code only (a marker named in a comment or a string is not one), and
`tests/unit/scripts/test_agent_quality_inventory.py` fails, naming it, for a
live skip or quarantine this page does not list (with the row to add) and for a
row the tree no longer has.

There is no count on this page, on purpose (the owner, 6 November 2026): two green
changes that each add a skip and bump a count conflict or fail when they land
together; two that each add their own row pass together as each passed alone.

* `skipif`: `pytest.mark.skipif`; every one is conditional on the machine (a
  tool on `PATH`: git, jq, bash, shellcheck, uv, node, ffmpeg; procfs or Linux;
  a checked-out `apps/swarm-ui`; a built binary).
* `skip`: `pytest.mark.skip` with no condition.
* `skip_call`: `pytest.skip(...)` inside a test or a helper, again on a missing
  tool or platform, or on a fixture the test looks for.
* `xfail`: an expected failure ([below](#the-expected-failure)).
* `importorskip`: an optional module (`cryptography`, `rfc8785`,
  `tree_sitter`, `uvicorn`, `websockets`, another app's package).

The Test column is the enclosing function (`Class.method` inside a class), or
`<module>` for a module-wide marker such as `pytestmark`; a test that skips
twice for one kind is one row. Console tests (`apps/swarm-ui`) have no
`it.skip`, `test.skip`, `describe.skip`, `it.todo` or `.fails`; this page does
not check them.

### The quarantine

Nothing is quarantined: `tests/flaky.txt` was emptied on 2026-10-14
([ci.md](../../ci.md)). A quarantined test is listed here as
``| `tests/...py::test_name` | quarantined | issue and date |``.

How the quarantine works, for when it is not empty: `tests/unit/conftest.py`
marks every test listed in `tests/flaky.txt` with `pytest.mark.flaky`
(`tests/unit/flaky_quarantine.py`); CI's unit job deselects them from the gating
run (`-m "not flaky"`) and runs them in a separate step that reports and never
fails.

### The expected failure

`tests/unit/control_plane/e2e_setup/test_console_onboarding.py::test_the_smoke_row_serves_the_detail_and_link_the_page_shows`
is `xfail(strict=True)`: a Day One setup smoke whose agent fails serves no
`detail` and no link, so the console's failed row says "its task's page says
why" with no why and no link (E2E-2). It is outside the program's areas; it is
described here because it was the only expected failure at the baseline, and
[§3](#3-every-skip-by-location) lists any other.

### Skips that touch the program's areas

A conditional skip is a test that did not run where the condition held. These
guard the program's areas; whether CI's unit job meets each condition is **not
read from here**, so a green CI run is not, by itself, evidence that they ran:

| Test | Condition | Area |
|---|---|---|
| `tests/unit/worker/test_lightpanda_loopback.py` | the Lightpanda binary of `agent-runtime-lightpanda` is present | browser and runtime routing |
| `tests/unit/worker/test_jev_outbound_filter.py` | node is installed | Jev adapter (the outbound filter) |
| `tests/unit/worker/test_jev_internal_compat.py` | node is installed | Jev adapter |
| `tests/unit/worker/test_quality_gates.py` | node is installed | Jev quality gates |
| `tests/unit/worker/test_qa_target_signin.py` | node is installed | browser QA gate (the sign-in start click) |
| `tests/unit/control_plane/test_log_redaction.py` | node is installed | Jev outbound filter against the redaction rules |
| `tests/unit/worker/test_repo_index_lsp_servers.py` | the language servers are installed | index |
| `tests/unit/worker/test_mcp_runner_config.py` | the `claude` or `codex` CLI is installed | agent plugins and MCP |
| about 75 test modules, most under `tests/unit/worker/`, including the publish, pre-publish gate and territory tests | git is installed | verification, territory, publish |

## 2. Documented open defects in the program's areas

"As written" is the record's own status. Where this commit's code contradicts
the record, the row says so: the record is then stale, which is itself a
finding for the package that owns it.

| Area | Defect | Recorded at | As written | On this commit |
|---|---|---|---|---|
| Context packs and graph tools | Subagents and child agents are not told about the graph (a batch three condition) | No record before this page | n/a | Not implemented: `GRAPH_SYSTEM_NOTE` reaches the CLI session only ([matrix](capability-matrix.md#4-context-packs-and-graph-tools)) |
| Context packs and graph tools | Agents with code intelligence did not call the graph: batch two 1, 0, 0, 0, 0, 0 calls; batch three two calls in four steps, both `tests_for` by bare names answered `unknown` | [context-quality.md](../context-quality.md), "Told when, at the system prompt's weight" | fix built (AGENTS-USE-TOOLS-WHEN, 2026-11-03) | Not live-verified after the fix |
| Jev | Jev-on steps never called `jev_decide` in batches two and three | same | same fix | Not live-verified after the fix |
| Jev | The browser filter's counts reach the summary only on an attempt whose gates run; an internal-backend browser started without `jev_launch.py` gets neither variable | [jev-data-flow.md](../../jev-data-flow.md), "Known gaps" | "Still open" (owner, 2026-11-02) | Open |
| Jev | `register_jev_tool` (the agent's `jev_decide` tool) "not built here"; JEV-RUNNER "not on main yet" | [context-quality.md](../context-quality.md) §5, "Not built here" and §1 | not built, open | **Stale record**: `agent_worker/judge/mcp.py` serves `jev_decide` and `agent_worker/runners/jev_launch.py` exists |
| Jev | J2's coverage flags are only in `plan-advisory.json`, not on the plan approval screen or in `swarm_runs` | context-quality.md §5, "Not built here" | not built | Open |
| Context packs and graph tools | Staging reads `tenant_settings`, so a workspace's `context` and `context_ab` do not reach it | context-quality.md, "Not yet read from there by staging" | follow-up | **Stale record**: `graphstage.py` `SETTINGS_COLLECTION = "workspace_settings"` since 2026-11-01 |
| Index and freshness | GS-RETIRE (removing the staged-graph fallback) waits on a measured `graph_ready` fallback rate | [graph-service.md](../graph-service.md) | not built | Open |
| Index and freshness | No push webhook for index freshness; the poll is the only trigger | graph-service.md; context-quality.md | not built, deliberately | Open by decision |
| Verification | `make smoke concurrency-test race-test` cannot run against the deployment (no `api_url` output; IAP audience not recorded) | [docs/audits/2026-09-20/verification-targets-cannot-run.md](../../audits/2026-09-20/verification-targets-cannot-run.md) | High, found 2026-09-20; no resolution recorded | Not re-read here |
| Verification | The gate's guard parts are bound by the pod's CPUs; a `builder` class is requested twice and undecided | [contract-change-requests.md](../../contract-change-requests.md) 134 and 135 | open, requested 2026-11-03 | Owner's decision |
| Verification | An acceptance git-tools check failed with its cause unexplained (no `pr_checks` call succeeded) | [docs/incidents/2026-10-26-failures.md](../../incidents/2026-10-26-failures.md), F16 | "not explained" | Not re-read here |
| Work contracts | `WorkflowStep` cannot record a verdict gate or `builds_on`, so a second review round needs a gated step that is not built | contract-change-requests.md 31 | open, 2026-09-29 | Open (frozen contract) |
| Work claims | A repository with more than 300 open PRs is not swept at all | [issue-runs.md](../../issue-runs.md) | by design | Open by design |
| Work claims | The lane queue is proposed only | [lane-queue.md](../../lane-queue.md) | proposed 2026-10-05 | Not implemented |
| Browser and runtime routing | `tests/unit/control_plane/test_claude_code_lightpanda_streams.py` is "not yet added" | [lightpanda-runtime.md](../lightpanda-runtime.md), test ledger | not yet added | Still absent; the same ledger's `submit.lightpanda.test.tsx` "no entry" is stale (the file exists) |
| Multiple repositories | No acceptance group submits a multi-repository step, so it is held offline only | [multi-repository-steps.md](../multi-repository-steps.md) §8.4 | not built | Open. The same section says "nothing under `terraform/` sets" the switch; dev.tfvars sets `additional_repositories_enabled = true` (stale sentence) |
| Multiple repositories | A staged graph for an additional repository is written but the staged server reads only the step's own snapshot | multi-repository-steps.md | partial | Open |
| Evidence surfaces | `ParkReason.BUDGET_EXHAUSTED` is written by nothing | contract-change-requests.md 39 | open, 2026-10-01 | Not re-read here; [cost-control.md](../../cost-control.md) still says there are no per-tenant budgets while `swarm_api/budget.py` resolves a workspace budget (one of the two is stale) |
| Evidence surfaces | The catalogue does not say which profiles report a cost; swarm-api restates the set | contract-change-requests.md 45 | open, 2026-10-02 | Open (frozen contract) |
| Evidence surfaces | No benchmark baseline is recorded | `benchmarks/baselines/README.md` | none yet | Open |
| Agent plugins | No documented open defect found | n/a | n/a | n/a |

## 3. Every skip, by location

The rows `scripts/agent_quality_inventory.py` checks. Add a row when you add a
skip (the failing test prints it), delete it when you delete the skip, and
rewrite its reason freely: only the file, test and kind are matched. Keep rows
in path order, so two changes' rows land in different places.

| File | Test | Kind | Reason |
|---|---|---|---|
| `tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py` | `prober` | `skip_call` | tenant-isolation probe: set {OPT_IN_ENV}=1, or run pytest in this file's directory naming this file. It is red until ISO-IAM removes the worker's Firestore ... |
| `tests/integration/test_api_endpoint_resolution.py` | `<module>` | `skipif` | scripts/lib/common.sh and bash are both required |
| `tests/integration/test_bench_collectors.py` | `<module>` | `skipif` | the collectors need jq and curl |
| `tests/integration/test_bootstrap_state_bucket_checks.py` | `<module>` | `skipif` | bootstrap.sh, jq and curl are all required |
| `tests/integration/test_bootstrap_target.py` | `<module>` | `skipif` | bootstrap.sh, jq and curl are all required |
| `tests/integration/test_destroy_guard.py` | `<module>` | `skipif` | scripts/lib/destroy-guard.jq not built yet |
| `tests/integration/test_destroy_guard.py` | `test_shipped_self_test_passes` | `skipif` | destroy.sh not built yet |
| `tests/integration/test_destroy_guard_real_plan.py` | `<module>` | `skipif` | jq and scripts/destroy.sh are both required |
| `tests/integration/test_destroy_guard_real_plan.py` | `test_the_ownership_predicate_is_still_warn_only_while_a_real_plan_flags_our_own_resources` | `skip_call` | foreign_touches is empty on the recorded real plan: the promotion condition in scripts/lib/plan-guard.sh is met and this case is moot |
| `tests/integration/test_e2e_suite_can_fail.py` | `<module>` | `skipif` | scripts/e2e-test.sh and jq are both required |
| `tests/integration/test_forge_credential_broker.py` | `<module>` | `skip_call` | needs the Firestore emulator (FIRESTORE_EMULATOR_HOST) |
| `tests/integration/test_outcomes_emulator.py` | `<module>` | `skip_call` | needs the Firestore emulator (FIRESTORE_EMULATOR_HOST) |
| `tests/integration/test_purge_backup_guard.py` | `<module>` | `skipif` | purge-data.sh and jq are both required |
| `tests/integration/test_race_test_refuses_an_unnarrowed_pool.py` | `<module>` | `skipif` | race-test.sh and jq are both required |
| `tests/integration/test_register_tenant_add_provider.py` | `<module>` | `skipif` | register-tenant.sh and jq are both required |
| `tests/integration/test_register_tenant_grants.py` | `<module>` | `skipif` | register-tenant.sh and jq are both required |
| `tests/integration/test_register_tenant_probe_failures.py` | `<module>` | `skipif` | register-tenant.sh and jq are both required |
| `tests/integration/test_register_tenant_squat.py` | `<module>` | `skipif` | register-tenant.sh and jq are both required |
| `tests/integration/test_shared_resource_verification.py` | `<module>` | `skipif` | destroy.sh and jq are both required |
| `tests/integration/test_smoke_backend_matrix.py` | `<module>` | `skipif` | smoke-test.sh needs bash and jq |
| `tests/integration/test_verify_destroy_guard_exit_codes.py` | `<module>` | `skipif` | jq, scripts/verify-destroy-guard.sh and the recorded plan are all required |
| `tests/integration/test_verify_remote_prints_the_job_log.py` | `<module>` | `skipif` | verify-remote.sh needs bash and jq |
| `tests/unit/common/test_additional_repositories_contract.py` | `test_a_format_4_signature_verifies_and_a_rewrite_does_not` | `importorskip` | cryptography is not importable |
| `tests/unit/common/test_specsign.py` | `test_agrees_with_the_rfc8785_package_on_generated_values` | `importorskip` | rfc8785 is not importable |
| `tests/unit/common/test_specsign.py` | `test_a_spec_signed_before_format_3_still_verifies` | `importorskip` | cryptography is not importable |
| `tests/unit/control_plane/e2e_setup/test_console_onboarding.py` | `test_the_smoke_row_serves_the_detail_and_link_the_page_shows` | `xfail` | E2E-2 (findings): api.ts reads a smoke's `detail` (or `reason`) and `url`, and a smoke whose agent fails serves neither (see the expected failure above) |
| `tests/unit/control_plane/test_account_api.py` | `test_every_path_the_ui_calls_exists_on_this_api` | `skip_call` | apps/swarm-ui/src/api.ts is not present |
| `tests/unit/control_plane/test_account_listing_is_honest.py` | `<module>` | `skipif` | apps/swarm-ui is not checked out |
| `tests/unit/control_plane/test_additional_repositories_submit.py` | `test_the_worker_reads_the_budget_from_the_key_swarm_api_serves` | `importorskip` | agent_worker.extrarepos is not importable |
| `tests/unit/control_plane/test_blocker_ui_surface.py` | `<module>` | `skipif` | apps/swarm-ui is not checked out |
| `tests/unit/control_plane/test_cancel_stops_the_execution.py` | `test_a_deployment_turns_it_on_by_default` | `skip_call` | ApiSettings.from_env needs more environment here: {exc} |
| `tests/unit/control_plane/test_capacity_serves_availability.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_capacity_serves_declared_inputs.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_dispatch_ui_surface.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_dispatch_ui_surface.py` | `test_the_quoted_worker_reasons_are_still_the_worker_s` | `skipif` | agent-worker is not present |
| `tests/unit/control_plane/test_issue_writeback.py` | `test_the_mention_rule_is_the_workers_own` | `importorskip` | agent_worker.lifecycle is not importable |
| `tests/unit/control_plane/test_log_redaction.py` | `test_the_jev_outbound_filter_flags_exactly_what_the_rules_mask` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/control_plane/test_log_redaction.py` | `test_the_house_filter_streams_a_line_outside_a_key_before_its_input_ends` | `skipif` | needs a pseudo-terminal |
| `tests/unit/control_plane/test_max_turns_submission.py` | `test_a_cap_on_a_profile_that_does_not_cap_turns_is_refused` | `skip_call` | {profile} is disabled; its refusal comes first |
| `tests/unit/control_plane/test_overview_workflow_checks.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_schedule_tick.py` | `test_an_unbuilt_executor_is_never_imported_as_available` | `skip_call` | every type's executor is built |
| `tests/unit/control_plane/test_state_colour_discriminability.py` | `<module>` | `skipif` | apps/swarm-ui/src/styles.css is not present |
| `tests/unit/control_plane/test_stop_and_output_ui_surface.py` | `<module>` | `skipif` | apps/swarm-ui is not checked out |
| `tests/unit/control_plane/test_subscription_headroom_surface.py` | `<module>` | `skipif` | apps/swarm-ui is not checked out |
| `tests/unit/control_plane/test_task_fencing_round_trip.py` | `test_the_ui_declares_the_fencing_fields_on_task` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_task_fencing_round_trip.py` | `test_the_ui_no_longer_records_the_fields_as_unavailable` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_ui_fixture_input_contract.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_url_refusal_whatwg.py` | `_node` | `skip_call` | node is not installed here; CI runs this table |
| `tests/unit/control_plane/test_workflow_graph_screen.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_workflow_graph_ui_surface.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_workflow_step_input_surface.py` | `<module>` | `skipif` | apps/swarm-ui/src is not present |
| `tests/unit/control_plane/test_workflow_step_measurements.py` | `<module>` | `skipif` | apps/swarm-ui is not checked out |
| `tests/unit/docs/test_counts_mockups.py` | `test_counts_mockup_names_only_profiles_and_accounts_that_run` | `skipif` | no docs/web-ui/mockups/counts-*.html on main: nothing to check |
| `tests/unit/hooks/test_hooks_create_secret.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/hooks/test_hooks_image.py` | `test_build_images_finds_the_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/hooks/test_hooks_image.py` | `test_target_selection_reaches_it_only_for_its_own_inputs` | `skipif` | build-images.sh needs jq |
| `tests/unit/hooks/test_hooks_image.py` | `test_a_promoted_digest_becomes_its_image_refs_entry` | `skipif` | image-refs.sh needs jq |
| `tests/unit/live/test_recorder_duration.py` | `test_the_real_ffmpeg_writes_a_finite_duration` | `skipif` | no ffmpeg on this machine |
| `tests/unit/mcp/test_apply_reads_the_patch_through_the_api.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/mcp/test_auto_attach.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_bridge.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/mcp/test_build_board_wire.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_plugin_agents_and_workflows.py` | `_node` | `skip_call` | node is not installed; CI runs these |
| `tests/unit/mcp/test_plugin_bridge_install.py` | `test_the_bridge_installs_from_git_the_way_the_plugin_fetches_it` | `skipif` | network: CI sets SWARM_BRIDGE_INSTALL_REF to a pushed commit to install |
| `tests/unit/mcp/test_plugin_portable.py` | `_listed` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/mcp/test_plugin_user_config.py` | `test_nothing_in_the_manifest_names_this_repositorys_deployment` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/mcp/test_plugin_version_guard.py` | `_hook` | `skip_call` | sh, bash or ps is not installed |
| `tests/unit/mcp/test_remote_steps_bridge.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/mcp/test_sc_auto_watch.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_sc_browser_pane.py` | `_claude` | `skip_call` | the claude CLI is not installed here; `claude plugin validate` and `claude plugin test` need it (CI's runner has none) |
| `tests/unit/mcp/test_sc_says_whose_figures.py` | `_disabled_profile` | `skip_call` | the frozen catalogue disables no profile today |
| `tests/unit/mcp/test_sc_session_scope.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_sc_tenant.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_sc_titles_by_workflow_id.py` | `test_an_empty_or_unexpanded_project_dir_falls_back_to_the_cwd_rule` | `skip_call` | git is not installed |
| `tests/unit/mcp/test_sc_titles_project_folder.py` | `test_the_project_folder_is_ignored_by_git` | `skip_call` | git is not installed |
| `tests/unit/mcp/test_sc_titles_project_folder.py` | `test_the_bridges_working_directory_is_the_project_when_it_is_a_git_work_tree` | `skip_call` | git is not installed |
| `tests/unit/mcp/test_sc_watch.py` | `_hook` | `skip_call` | bash is not installed |
| `tests/unit/mcp/test_setup_and_access.py` | `test_the_repo_id_recipe_is_swarm_apis` | `importorskip` | swarm_api.repositories is not importable |
| `tests/unit/reconciler/test_backfill_attempt_ends.py` | `<module>` | `skipif` | backfill-attempt-ends.sh needs bash and jq |
| `tests/unit/scripts/test_accept_cancels_only_its_own.py` | `<module>` | `skipif` | the steps and verify-remote.sh need bash and jq |
| `tests/unit/scripts/test_accept_live_browser_streak.py` | `test_the_script_is_shellcheck_clean` | `skipif` | shellcheck is not installed here |
| `tests/unit/scripts/test_acceptance_fixture_page.py` | `run_page_script` | `skip_call` | node is not on PATH; the static tests above still hold the page |
| `tests/unit/scripts/test_acceptance_roles.py` | `<module>` | `skipif` | the probe needs bash and jq |
| `tests/unit/scripts/test_access_token_self_impersonation.py` | `<module>` | `skipif` | common.sh and bash are required |
| `tests/unit/scripts/test_api_signin_credential.py` | `<module>` | `skipif` | needs bash |
| `tests/unit/scripts/test_auto_merge_base_merge.py` | `<module>` | `skipif` | bash, jq and git are required |
| `tests/unit/scripts/test_auto_merge_ready_on_green.py` | `fake` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `run_gate` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `run_disable` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `_train_judge` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `run_requeue` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `test_the_body_edit_re_evaluation_dispatches_exactly_that_pull_request` | `skip_call` | bash is required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `test_the_body_edit_re_evaluation_refuses_a_number_that_is_not_one` | `skip_call` | bash is required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `run_resolve` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_auto_merge_workflow.py` | `run_merge` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_bootstrap_remote_state.py` | `<module>` | `skipif` | bootstrap.sh needs jq and curl |
| `tests/unit/scripts/test_build_images_concurrency.py` | `<module>` | `skipif` | build-images.sh needs bash and jq |
| `tests/unit/scripts/test_build_images_incremental.py` | `<module>` | `skipif` | build-images.sh needs bash, jq and git |
| `tests/unit/scripts/test_build_images_local.py` | `<module>` | `skipif` | build-images.sh needs bash and jq; the plan step needs git |
| `tests/unit/scripts/test_build_images_pr_check.py` | `<module>` | `skipif` | build-images.sh needs bash and jq |
| `tests/unit/scripts/test_build_mirror.py` | `test_the_new_scripts_are_shellcheck_clean` | `skipif` | shellcheck not installed |
| `tests/unit/scripts/test_check_closing_references.py` | `<module>` | `skipif` | bash and jq are required |
| `tests/unit/scripts/test_check_pr_dependencies.py` | `<module>` | `skipif` | bash and jq are required |
| `tests/unit/scripts/test_ci_built_images.py` | `<module>` | `skipif` | these scripts need bash, jq and git |
| `tests/unit/scripts/test_ci_fix.py` | `<module>` | `skipif` | jq and bash are required |
| `tests/unit/scripts/test_ci_fold.py` | `<module>` | `skipif` | the verdict step needs bash and jq |
| `tests/unit/scripts/test_ci_gate.py` | `<module>` | `skipif` | the gate needs bash and jq |
| `tests/unit/scripts/test_close_merged_issues.py` | `<module>` | `skipif` | bash and jq are required |
| `tests/unit/scripts/test_cloud_run_start_doc.py` | `test_each_stated_backend_is_the_catalogues` | `skip_call` | {name} is not named by the doc (the test above fails for it) |
| `tests/unit/scripts/test_concurrency_judge.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_concurrency_sampler_by_file.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_create_secrets_egress.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/scripts/test_create_secrets_git_wf.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/scripts/test_create_secrets_search_key.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/scripts/test_create_secrets_tailscale_oauth.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/scripts/test_deploy_promotion_mark.py` | `<module>` | `skipif` | the scripts need bash and jq |
| `tests/unit/scripts/test_e2e_gate_checks.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_frozen_contract_check.py` | `<module>` | `skipif` | the frozen-contract check needs git and bash |
| `tests/unit/scripts/test_fs_database_exists_retry.py` | `<module>` | `skipif` | common.sh, bash and jq are required |
| `tests/unit/scripts/test_gh_api_read_retry.py` | `<module>` | `skipif` | bash and jq are required |
| `tests/unit/scripts/test_github_app_runbook.py` | `<module>` | `skipif` | create-secrets.sh needs bash |
| `tests/unit/scripts/test_gstack_packaging_measure.py` | `test_a_read_only_install_runs_and_writes_nothing_into_it` | `skipif` | root ignores file modes |
| `tests/unit/scripts/test_hotfix_release.py` | `test_the_scripts_are_executable_and_shellcheck_clean` | `skip_call` | shellcheck is not installed here |
| `tests/unit/scripts/test_iam_policy_binding_predicate.py` | `<module>` | `skipif` | common.sh, jq and bash are all required |
| `tests/unit/scripts/test_iam_refusal_probe.py` | `<module>` | `skipif` | jq and bash are required |
| `tests/unit/scripts/test_image_refs_gated.py` | `<module>` | `skipif` | image-refs.sh and plan.sh need jq and curl |
| `tests/unit/scripts/test_image_sizes.py` | `<module>` | `skipif` | image-sizes.sh needs bash and jq |
| `tests/unit/scripts/test_install_sh_uv.py` | `<module>` | `skipif` | no pinned uv for this platform |
| `tests/unit/scripts/test_install_sh_uv.py` | `_run` | `skip_call` | a real uv in /usr/bin or /bin would be found first |
| `tests/unit/scripts/test_join_acceptance_probe.py` | `<module>` | `skipif` | the probe needs bash, jq and base64 |
| `tests/unit/scripts/test_k8s_render_project.py` | `_assert_names_no_live_project` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/scripts/test_lightpanda_build_scripts.py` | `test_target_selection_reaches_the_lightpanda_image_for_its_inputs_and_its_base` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_live_browser_acceptance.py` | `test_the_qa_identity_case_skips_every_check_unless_both_settings_are_set` | `skip_call` | bash is required |
| `tests/unit/scripts/test_live_browser_acceptance.py` | `_stuck` | `skip_call` | bash and jq are required |
| `tests/unit/scripts/test_make_logs_pem_order.py` | `<module>` | `skipif` | make and bash are both required |
| `tests/unit/scripts/test_merge_train.py` | `_need` | `skip_call` | {tool} is required |
| `tests/unit/scripts/test_merge_train_gather.py` | `_need` | `skip_call` | {tool} is required |
| `tests/unit/scripts/test_merge_train_skips.py` | `_need` | `skip_call` | {tool} is required |
| `tests/unit/scripts/test_migrate_index_interval.py` | `<module>` | `skipif` | migrate-index-interval.sh needs bash and jq |
| `tests/unit/scripts/test_no_live_literals.py` | `_listed` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/scripts/test_offboard_tenant.py` | `<module>` | `skipif` | offboard-tenant.sh needs bash and jq |
| `tests/unit/scripts/test_onboarding_docs_and_mockup.py` | `test_each_entry_variant_draws_the_checklist` | `skip_call` | {vid} not drawn |
| `tests/unit/scripts/test_plan_guard_iam_classification.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_plan_guard_metric_replace.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_plugin_bundle_recipes.py` | `_require` | `skip_call` | {tool} is not installed here; CI runs this |
| `tests/unit/scripts/test_plugin_bundle_recipes.py` | `test_each_recipe_starts_with_strict_mode_and_is_shellcheck_clean` | `skip_call` | shellcheck is not installed here; CI runs this |
| `tests/unit/scripts/test_plugin_intake_image.py` | `test_a_change_to_the_job_rebuilds_the_image_and_nothing_else_does` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_plugin_intake_image.py` | `test_build_images_finds_the_intake_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_plugin_tag.py` | `<module>` | `skipif` | the steps read plugin.json with jq |
| `tests/unit/scripts/test_pool_limit_units.py` | `<module>` | `skipif` | jq and bash are required |
| `tests/unit/scripts/test_promotion_mark.py` | `<module>` | `skipif` | the scripts need bash and jq |
| `tests/unit/scripts/test_publish_scan_script.py` | `test_the_script_reports_a_planted_secret_in_the_current_checkout` | `skipif` | needs git and the project's python |
| `tests/unit/scripts/test_push_images_all_or_nothing.py` | `<module>` | `skipif` | push-images.sh needs bash and jq |
| `tests/unit/scripts/test_push_images_promotes_recorded_digests.py` | `<module>` | `skipif` | push-images.sh needs bash and jq |
| `tests/unit/scripts/test_race_test_own_leases.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_register_tenant_forge_reader.py` | `test_the_touched_scripts_are_shellcheck_clean` | `skipif` | shellcheck is not installed |
| `tests/unit/scripts/test_register_tenant_merge_step.py` | `<module>` | `skipif` | register-tenant.sh and jq are both required |
| `tests/unit/scripts/test_register_tenant_workspace.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_related_tests.py` | `test_the_script_selects_one_swarm_api_routes_tests_and_none_of_the_workers` | `skip_call` | the budget route moved; pick another route |
| `tests/unit/scripts/test_release_acceptance_job.py` | `test_the_acceptance_scripts_are_shellcheck_clean` | `skipif` | shellcheck is not installed here |
| `tests/unit/scripts/test_release_apply_targeted_services.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_release_assemble.py` | `test_shellcheck_clean` | `skipif` | shellcheck not installed |
| `tests/unit/scripts/test_release_assemble.py` | `test_install_sh_is_posix_and_shellcheck_clean` | `skipif` | shellcheck not installed |
| `tests/unit/scripts/test_release_home.py` | `test_shellcheck_clean` | `skipif` | shellcheck not installed |
| `tests/unit/scripts/test_release_publish.py` | `test_shellcheck_clean` | `skipif` | shellcheck not installed |
| `tests/unit/scripts/test_release_skips_switched_off_images.py` | `test_a_dev_release_whose_switch_is_not_literally_off_builds_it` | `skip_call` | root reads a file whatever its mode |
| `tests/unit/scripts/test_release_skips_switched_off_images.py` | `test_a_manifest_holding_speech_with_its_switch_not_off_scans_and_promotes_it` | `skip_call` | root reads a file whatever its mode |
| `tests/unit/scripts/test_repo_index_design_doc.py` | `_listed` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/scripts/test_resign_workspace_settings.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_roll_voice_workflows.py` | `<module>` | `skipif` | no workflow names swarm-voice yet: {PATCH_HINT} |
| `tests/unit/scripts/test_sandbox_sync_git_failures.py` | `<module>` | `skipif` | git and jq are required |
| `tests/unit/scripts/test_security_audit_image.py` | `_node` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/scripts/test_select_unit_tests.py` | `test_on_this_repository_one_route_selects_its_tests_and_none_of_the_workers` | `skip_call` | the budget route moved; pick another route |
| `tests/unit/scripts/test_select_unit_tests.py` | `test_on_this_repository_a_scheduler_module_selects_the_tests_that_import_it` | `skip_call` | the scheduler's metrics module moved; pick another module |
| `tests/unit/scripts/test_smoke_covers_every_backend.py` | `<module>` | `skipif` | jq not installed |
| `tests/unit/scripts/test_smoke_tenant_parse.py` | `<module>` | `skipif` | jq not installed |
| `tests/unit/scripts/test_startup_measure.py` | `test_the_script_answers_help_without_credentials` | `skipif` | needs bash |
| `tests/unit/scripts/test_swarm_egress_image.py` | `test_build_images_finds_the_forwarders_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_swarm_egress_image.py` | `test_target_selection_reaches_the_forwarder_only_for_its_own_inputs` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_swarm_egress_image.py` | `test_a_promoted_forwarder_digest_becomes_its_image_refs_entry` | `skipif` | image-refs.sh needs jq |
| `tests/unit/scripts/test_swarm_live_image.py` | `test_build_images_finds_the_relays_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_swarm_live_image.py` | `test_the_export_the_builder_runs_hashes_every_requirement` | `skipif` | needs uv |
| `tests/unit/scripts/test_swarm_speech_image.py` | `test_build_images_finds_the_speech_servers_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/scripts/test_terraform_install_inputs.py` | `_listed` | `skip_call` | no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): nothing says what is live |
| `tests/unit/scripts/test_typesafe_policies_wired.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_verify_remote_slash_targets.py` | `<module>` | `skipif` | verify-remote.sh and bash are required |
| `tests/unit/scripts/test_warm_jobs.py` | `<module>` | `skipif` | warm-jobs.sh needs bash and jq |
| `tests/unit/scripts/test_workspace_apply_approval.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_workspace_apply_entry.py` | `<module>` | `skipif` | the exec test needs Linux's /proc, /usr/bin/timeout and /bin/bash |
| `tests/unit/scripts/test_workspace_guard.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_workspace_guard_corpus.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_workspace_guard_corpus.py` | `test_the_guards_dirname_and_basename_answer_as_coreutils_does` | `skip_call` | coreutils dirname/basename are not on PATH |
| `tests/unit/scripts/test_workspace_migrate_record.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/scripts/test_workspace_quota_one.py` | `<module>` | `skipif` | jq is required |
| `tests/unit/typesafe_fwd/test_typesafe_fwd_image.py` | `test_build_images_finds_the_recipe_and_reads_what_it_copies` | `skipif` | build-images.sh needs jq |
| `tests/unit/typesafe_fwd/test_typesafe_fwd_image.py` | `test_target_selection_reaches_it_only_for_its_own_inputs` | `skipif` | build-images.sh needs jq |
| `tests/unit/typesafe_fwd/test_typesafe_fwd_image.py` | `test_a_promoted_digest_becomes_its_image_refs_entry` | `skipif` | image-refs.sh needs jq |
| `tests/unit/worker/test_additional_repositories_clone.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_additional_repositories_not_harvested.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_agent_commits_are_kept.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_allow_empty_diff.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_artifacts_manifest_cap.py` | `test_a_secret_crossing_the_unstorable_shown_cut_leaves_no_fragment` | `skip_call` | this filesystem refuses names that are not UTF-8 |
| `tests/unit/worker/test_base_pin.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_checkpoint_leaves_out_repos.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_checkpoint_refuses_a_linked_work.py` | `test_an_unreadable_folder_is_left_out_and_named_and_the_rest_is_archived` | `skipif` | root reads a mode-000 folder; CI is not root |
| `tests/unit/worker/test_ci_fix_superseded_at_start.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_claude_repair_turns.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_a_bare_stdin_read_hangs_without_the_rewrite` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_the_hook_rewrites_cat_to_dev_null_so_it_returns_at_once` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_read_after_a_heredoc_sees_end_of_file` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_a_heredoc_body_with_a_less_than_sign_is_still_rewritten` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_a_pipe_between_commands_still_gets_its_pipe` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_the_wrap_keeps_exit_status_cwd_and_a_trailing_comment` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_stdin_hook.py` | `test_a_command_reading_a_file_by_redirect_still_reads_it` | `skipif` | needs bash |
| `tests/unit/worker/test_claude_workspace_trust.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_clone_full_refname.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_clone_phase_trace.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_clone_token_host.py` | `test_a_redirect_to_another_host_is_not_handed_the_token` | `skipif` | git is not installed |
| `tests/unit/worker/test_clone_transient_retry.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_conflict_round_merge_parents.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_continue_swarm_branch.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_cpu_sampler.py` | `test_a_real_process_tree_is_measured_including_its_children` | `skipif` | needs procfs |
| `tests/unit/worker/test_cpu_sampler.py` | `test_a_run_writes_the_attempts_own_figures_while_it_runs_and_at_the_end` | `skipif` | a live CPU reading needs cgroup v2 or procfs; without either (macOS) the only figure is the reaped-children total at the end, by design (SystemCpuMeter). CI is ... |
| `tests/unit/worker/test_egress_gate.py` | `test_a_real_loopback_peer_is_this_process_uid` | `skip_call` | no /proc/net/tcp here |
| `tests/unit/worker/test_fold_keeps_any_branch_parents.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_fold_keeps_merge_parents.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_forge_token_isolation.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_forge_token_isolation.py` | `test_no_agent_process_is_alive_when_the_publish_token_is_used` | `skipif` | the reap verifies liveness via /proc; this platform has none (CI is Linux) |
| `tests/unit/worker/test_gate_own_paths.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_git_env_has_no_graft_hint.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_git_tool_unavailable_names_cause.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_gittools.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_gittools_push_range.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_gittools_rebase_author.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_gittools_shallow.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_handoff.py` | `test_every_gate_operation_the_worker_sends_is_one_the_gate_accepts` | `importorskip` | agent_worker.livegate.ctl is not importable |
| `tests/unit/worker/test_harvest.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_harvest_detached_start.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_harvest_files.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_harvest_start_sha.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_integrate_read_only_contributor.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_issue_input.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_jev_internal_compat.py` | `node` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/worker/test_jev_outbound_filter.py` | `node` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/worker/test_jev_outbound_filter.py` | `test_the_path_deny_and_never_sent_keys_are_the_workers` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/worker/test_jev_wiring.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_lightpanda_loopback.py` | `test_live_a_loopback_page_is_read_and_the_metadata_server_is_refused` | `skipif` | needs the Lightpanda binary of agent-runtime-lightpanda |
| `tests/unit/worker/test_livegate_main.py` | `test_the_gates_tunnel_speaks_to_the_real_relay` | `importorskip` | uvicorn is not importable |
| `tests/unit/worker/test_mcp_runner_config.py` | `<module>` | `skipif` | the pinned claude-code is not installed (image test) |
| `tests/unit/worker/test_memory_guard.py` | `test_the_proc_table_reads_pid_ppid_pgid_rss_and_name_of_this_process` | `skip_call` | no procfs |
| `tests/unit/worker/test_memory_guard.py` | `test_a_real_sleep_grandchild_is_killed_and_only_it` | `skip_call` | no procfs |
| `tests/unit/worker/test_merge_action.py` | `test_the_step_closes_what_close_merged_issues_sh_closes` | `skip_call` | bash and jq are required to run the script |
| `tests/unit/worker/test_merge_verdict_runs_no_fix_agent.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_pluginload_runner.py` | `node` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/worker/test_pr_body_console_links.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_pr_open_reread.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_base_after_main_merge.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_check_budget.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_check_timeout.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_gate.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_gate_time.py` | `<module>` | `skipif` | git and jq are needed to run the guard script |
| `tests/unit/worker/test_prepublish_graph_gate.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_guards_dedup.py` | `<module>` | `skipif` | git and jq are needed to run the guard script |
| `tests/unit/worker/test_prepublish_guards_timebox.py` | `<module>` | `skipif` | git and jq are needed to run the guard script |
| `tests/unit/worker/test_prepublish_render_stderr.py` | `<module>` | `skipif` | needs a POSIX shell |
| `tests/unit/worker/test_prepublish_test_services.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_tree_restore.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_prepublish_unreachable_service.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_procman_reap.py` | `test_reap_kills_a_real_escaped_process_in_a_private_pid_namespace` | `skip_call` | unprivileged PID+user namespaces are unavailable here: {why} |
| `tests/unit/worker/test_publish_after_prepublish_rebase.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_after_tool_push.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_as_the_person.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_commits_in_the_clean_repository.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_deliverable_branch.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_failure_uploads_patch.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_fenced_before_forge_write.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_guard_security_pass.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_keeps_index_removals.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_outcomes.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_push_reread.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_scan_after_merging_main.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_scan_cli.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_scan_stacked_branch_floor.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_scans_a_clean_repository.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_to_pr.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_to_pr_own_moved_head.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_workflow_files.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_publish_workflow_remote_tip.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_published_nothing.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_published_nothing_names_artifacts.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_pull_request_text.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_push_retry_republish.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_qa_target_signin.py` | `_node` | `skip_call` | node is not installed here; CI runs the start click on its fixture pages |
| `tests/unit/worker/test_quality_gates.py` | `node` | `skip_call` | node is not installed here; CI runs these |
| `tests/unit/worker/test_questions_line_names_await.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_repo_index_history.py` | `ui_graph` | `importorskip` | tree_sitter is not importable |
| `tests/unit/worker/test_repo_index_lsp_servers.py` | `test_the_real_server_resolves_a_call_across_two_files` | `skip_call` | {', '.join(missing)} not installed in {bin_dir}: the real-server pass runs in the agent-runtime-indexer image (or with SWARM_LSP_BIN_DIR set) |
| `tests/unit/worker/test_republish_keeps_the_pull_request_body.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_review_verdict_publish.py` | `test_on_gcs_the_write_carries_if_generation_match_zero` | `importorskip` | google.api_core is not importable |
| `tests/unit/worker/test_security_review_step.py` | `test_the_real_validators_run_under_node_when_it_is_there` | `skip_call` | no node on this machine |
| `tests/unit/worker/test_single_pr_roles.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_skipped_fix_step_publishes.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_standalone_outputs.py` | `test_a_name_that_is_not_utf8_is_listed_escaped_and_the_result_is_still_written` | `skip_call` | this filesystem refuses names that are not UTF-8 |
| `tests/unit/worker/test_standalone_outputs.py` | `test_a_path_longer_than_a_storage_object_name_is_listed_shortened` | `skipif` | a 1,200-byte path is past macOS's PATH_MAX |
| `tests/unit/worker/test_standalone_outputs.py` | `test_a_repository_task_is_unchanged` | `skipif` | git is not installed |
| `tests/unit/worker/test_step_parity.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_step_repository_ref_clone.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_strategy_end_to_end.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_subagent_spend.py` | `test_a_scan_refusal_names_its_rule_and_path_line_and_not_the_text` | `skipif` | git is not installed |
| `tests/unit/worker/test_swarm_qa.py` | `_node` | `skip_call` | node is not installed here; CI runs the page's scripts |
| `tests/unit/worker/test_territory_warn.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_two_step_merge_verdict_opens_pr.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_verdict_commit_precheck.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_verdict_gate.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_worker_commit_title.py` | `<module>` | `skipif` | git is not installed |
| `tests/unit/worker/test_worker_memory_protection.py` | `_require_linux_without_ptrace_capability` | `skip_call` | why |
| `tests/unit/worker/test_workflow_paths_merge_of_main.py` | `<module>` | `skipif` | git is not installed |
