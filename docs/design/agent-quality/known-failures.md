# Known failures at the baseline

What is skipped, expected to fail or quarantined on main at
`3e3a9875458719f438df1c2d426ae1844b4122b3` ([baseline](baseline.md)), and the
open defects documented in the program's areas, each with where it is recorded.
An experiment reads this page first: a failure listed here is not the
experiment's finding, and a skipped test is not a passing one.

## 1. Skipped, expected-to-fail and quarantined tests

Counted by `scripts/agent_quality_inventory.py` over every `.py` file under
`tests/`, in code only (a marker named in a comment or a string is not one).
`tests/unit/scripts/test_agent_quality_inventory.py` fails when the tree's
counts differ from this table, so the table is the tree's.

| Kind | Count | What it means here |
|---|---|---|
| `skipif` | 220 | `pytest.mark.skipif`: every one is conditional on the machine (a tool on `PATH`: git, jq, bash, shellcheck, uv, node, ffmpeg; procfs or Linux; a checked-out `apps/swarm-ui`; a built binary) |
| `skip` | 0 | `pytest.mark.skip` with no condition: none |
| `skip_call` | 59 | `pytest.skip(...)` inside a test, again on a missing tool or platform, or on a fixture the test looks for ("the budget route moved; pick another route") |
| `xfail` | 1 | the one expected failure, below |
| `importorskip` | 11 | an optional module (`cryptography`, `rfc8785`, `tree_sitter`, `uvicorn`, `websockets`, another app's package) |
| `tests/flaky.txt` entries | 0 | the quarantine is empty; it was emptied on 2026-10-14 ([ci.md](../../ci.md)) |
| Console tests (`apps/swarm-ui`) | 0 | no `it.skip`, `test.skip`, `describe.skip`, `it.todo` or `.fails` |

How the quarantine works, for when it is not empty: `tests/unit/conftest.py`
marks every test listed in `tests/flaky.txt` with `pytest.mark.flaky`
(`tests/unit/flaky_quarantine.py`); CI's unit job deselects them from the gating
run (`-m "not flaky"`) and runs them in a separate step that reports and never
fails.

### The expected failure

`tests/unit/control_plane/e2e_setup/test_console_onboarding.py::test_the_smoke_row_serves_the_detail_and_link_the_page_shows`
is `xfail(strict=True)`: a Day One setup smoke whose agent fails serves no
`detail` and no link, so the console's failed row says "its task's page says
why" with no why and no link (E2E-2). It is outside the program's areas and is
listed because it is the only one.

### Skips that touch the program's areas

A conditional skip is a test that did not run where the condition held. These
guard the program's areas; whether CI's unit job meets each condition is **not
read from here**, so a green CI run is not, by itself, evidence that they ran:

| Test | Condition | Area |
|---|---|---|
| `tests/unit/worker/test_lightpanda_loopback.py` (line 177) | the Lightpanda binary of `agent-runtime-lightpanda` is present | browser and runtime routing |
| `tests/unit/worker/test_jev_outbound_filter.py` (lines 61, 231) | node is installed | Jev adapter (the outbound filter) |
| `tests/unit/worker/test_jev_internal_compat.py` (line 94) | node is installed | Jev adapter |
| `tests/unit/worker/test_quality_gates.py` (line 674) | node is installed | Jev quality gates |
| `tests/unit/control_plane/test_log_redaction.py` (line 585) | node is installed | Jev outbound filter against the redaction rules |
| `tests/unit/worker/test_repo_index_lsp_servers.py` (line 148) | the language servers are installed | index |
| `tests/unit/worker/test_mcp_runner_config.py` (lines 439, 441) | the `claude` or `codex` CLI is installed | agent plugins and MCP |
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
| Jev | `register_jev_tool` (the agent's `jev_decide` tool) "not built here"; JEV-RUNNER "not on main yet" (#1297) | [context-quality.md](../context-quality.md) §5, "Not built here" and §1 | not built, open | **Stale record**: `agent_worker/judge/mcp.py` serves `jev_decide` and `agent_worker/runners/jev_launch.py` exists |
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
