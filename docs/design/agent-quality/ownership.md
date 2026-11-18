# Agent quality packages and the paths they own

Which paths each package of the agent quality program owns, read against the
tree at `3e3a9875458719f438df1c2d426ae1844b4122b3` ([baseline](baseline.md)).
Every path below exists on that commit. The package names are this page's
labels for the program's ten areas, plus this baseline package.

Ownership here is the repository's lane convention and nothing more
([CLAUDE.md](../../../CLAUDE.md), "Track ownership"): a package edits its own
paths, reads everything, and when the owner's request needs another package's
file it edits it and says in the commit which package's file it crossed into
and why. It is never a reason to refuse or narrow work. Two packages that must
edit the same file are one lane for that change, not two. Tests follow their
code: a package owns the unit tests of its paths. `apps/common/swarm_common/`
is frozen and owned by no package; a need there is a contract request.

## 1. Packages

| Package | Owns | Its tests |
|---|---|---|
| Baseline and capability inventory (this one) | `docs/design/agent-quality/`, `scripts/agent_quality_inventory.py` | `tests/unit/scripts/test_agent_quality_inventory.py` |
| Work contracts and territory | `apps/agent-worker/agent_worker/territory.py`, `expected_outputs.py`, `specverify.py`, `verdict.py`, `questions.py`, `owner_questions.py`; `apps/swarm-api/swarm_api/territory.py`, `laneterritory.py` | `tests/unit/worker/test_territory_warn.py`, `test_specverify_upstream.py`, `test_verdict_gate.py`, `test_fencing.py`; `tests/unit/control_plane/test_plan_territory.py`, `test_territory_input.py` |
| Jev accounting and adapter | `apps/agent-worker/agent_worker/judge/`, `jevkey.py`, `qualitygates.py`, `runners/jev_launch.py`; `apps/swarm-api/swarm_api/jevusage.py`, `routes/jev_usage.py`, `judgment.py`; `apps/swarm-ui/src/JevUsagePage.tsx`; `images/agent-runtime-base/jev-gates/` | `tests/unit/worker/test_jev_*.py`, `test_judge_*.py`, `test_quality_gates.py`; `tests/unit/control_plane/test_jev_usage.py`, `test_issue_run_jev_hold.py`; `apps/swarm-ui/src/__tests__/jev.*.test.tsx` |
| Index and freshness | `apps/agent-worker/agent_worker/indexrun.py`, `indexspec.py`; `apps/swarm-api/swarm_api/repoindex.py`, `indexfailure.py`, `schedtypes/repo_index_refresh.py`; `apps/swarm-graph/`; `images/agent-runtime-indexer/` | `tests/unit/control_plane/test_repository_index*.py`; `tests/unit/worker/test_index_run_*.py`, `test_repo_index_*.py`; `tests/unit/graphsvc/` |
| Context packs and graph tools | `apps/agent-worker/agent_worker/contextpack/`, `graphmcp/`, `graphstage.py`, `graphroute.py`, `graph_ticket.py`; `apps/swarm-api/swarm_api/codeintel.py`, `graph_ticket.py`; `apps/swarm-ui/src/RepoGraphCache.tsx` | `tests/unit/worker/test_contextpack.py`, `test_graphstage*.py`, `test_graphmcp_*.py`, `test_agents_use_tools*.py`, `test_dispatch_switches_worker.py` |
| Verification and the pre-publish gate | `apps/agent-worker/agent_worker/prepublish.py`, `publish_scan.py`, `runners/guards_repeat_hook.py`, `runners/gate_wait_hook.py`; `scripts/changed-guards.sh`, `scripts/related-tests.sh`, `scripts/lib/related_tests.py`, `scripts/publish-scan.sh` | `tests/unit/worker/test_prepublish_*.py`, `test_publish_scan_cli.py`, `test_claude_lane_caps.py`; `tests/unit/scripts/test_related_tests.py`, `test_changed_guards.py` |
| Work claims and deduplication | `apps/swarm-api/swarm_api/issuesweep.py`, `mergetrain.py`, `mergesettings.py`; `apps/agent-worker/agent_worker/findings_epic.py`, `mergeslot.py` | `tests/unit/control_plane/test_issue_sweep.py`, `test_merge_train_*.py`; `tests/unit/worker/test_findings_epic.py`, `test_merge_slot.py` |
| Browser and runtime routing | `apps/agent-worker/agent_worker/browser_tools.py`, `lightpanda.py`, `runners/browser.py`, `livegate/`; `images/agent-runtime-browser/`, `images/agent-runtime-lightpanda/`; `scripts/browser_bench.py` | `tests/unit/worker/test_browser_*.py`, `test_lightpanda_*.py`, `test_live_cdp_proxy.py`; `tests/unit/scripts/test_lightpanda_build_scripts.py` |
| Multiple repositories | `apps/agent-worker/agent_worker/extrarepos.py`; `apps/swarm-api/swarm_api/extrarepos.py` | `tests/unit/worker/test_additional_repositories_*.py`, `test_graphstage_additional.py`; `tests/unit/control_plane/test_additional_repositories_submit.py`, `test_graph_ticket_additional.py`; `tests/unit/mcp/test_additional_repositories_bridge.py` |
| Evidence surfaces | `apps/agent-worker/agent_worker/runners/ceiling.py`, `spendprice.py`, `metrics.py`; `apps/swarm-api/swarm_api/outcomes.py`, `spendestimate.py`, `budget.py`, `modelprices.py`, `schedtypes/cost_report.py`; `apps/swarm-ui/src/PlatformOverTime.tsx`; `scripts/lib/quality_metrics.py`, `scripts/quality-baseline.sh`, `scripts/lib/ctxq_replay.py`, `scripts/ctxq-replay.sh`, `scripts/benchstat.py`, `benchmarks/` | `tests/unit/worker/test_session_totals.py`, `test_cancelled_attempt_cost.py`; `tests/unit/control_plane/test_outcomes_core.py`; `tests/unit/scripts/test_quality_metrics.py`, `test_benchstat.py`; `apps/swarm-ui/src/__tests__/cost.totals.test.tsx` |
| Agent plugins | `plugin/`; `apps/swarm-mcp/swarm_mcp/`; `apps/swarm-api/swarm_api/mcp_servers.py`, `routes/mcp_servers.py`; `apps/agent-worker/agent_worker/mcp_attach.py`, `mcp_relay.py` | `tests/unit/mcp/`; `tests/unit/control_plane/test_mcp_servers.py`; `tests/unit/worker/test_mcp_*.py`; `plugin/hooks/*.test.ts` |

## 2. The three shared files

Each is edited by several packages and integrated by one integrator. Any edit
by a package is minimal, in its own commit, and listed hunk by hunk in the PR
body under "Shared-file hunks".

| File | Lines | What each package reaches there |
|---|---|---|
| `apps/agent-worker/agent_worker/lifecycle.py` | 21,782 | `Worker` (most of the file): graph staging (`graphstage_mod.prepare`, context packs), Jev's gates (`Worker._quality_gates_env`), the fencing revalidation before publish and merge (contracts), the leak scan at publish (`final_tree_leak`, verification), cost and usage (`_add_spend`, `_usage_summary`, `_subagent_cost_usd`, evidence), console links in the PR body (`pr_body_with_console_links`) |
| `apps/swarm-api/swarm_api/validation.py` | 4,880 | the workflow switches (`DISPATCH_SWITCHES`, the arm of an experiment), model class (`resolve_task_model`), max turns (`validate_max_turns`), the base commit of a replay (`check_repository_ref`), additional repositories' refusals (`AdditionalRepositoriesDisabled` and its siblings), registered MCP servers' refusals, merge planning |
| `apps/swarm-api/swarm_api/issueruns.py` | 3,951 | plan territory and its lock (`territory_overlap`, `territory_refusal`), the planner's duplicate verdict (`NotReadyVerdict`), the owner signature that accepts Jev's hold (`_narrowed`), plan compilation (`compile_plan`) |

## 3. Contended files that are not designated shared

These are each edited by more than one package on this commit's layout and are
not on the shared list. Until the owner says otherwise, the package named first
owns the file and the others edit it as a crossing, said in the commit:

| File | Lines | Packages that reach it |
|---|---|---|
| `apps/agent-worker/agent_worker/runners/claude_code.py` | 2,019 | context packs and graph tools (`GRAPH_SYSTEM_NOTE`, `ALWAYS_LOAD`), Jev (`JEV_SYSTEM_NOTE`), agent plugins (`write_mcp_config`), evidence surfaces (`tool_calls_by_family`), verification (`CliAgentSpec(prepublish=True)`) |
| `apps/agent-worker/agent_worker/runners/cliagent.py` | 3,272 | context packs and graph tools (the prompt lines), evidence surfaces (subagent turns in the stream), verification |
| `apps/agent-worker/agent_worker/children.py` | 1,609 | context packs and graph tools (a child agent hearing about the graph), work claims |
