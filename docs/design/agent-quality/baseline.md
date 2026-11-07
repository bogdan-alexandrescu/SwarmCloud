# Agent quality baseline

Package one of the agent quality program (the owner, 3 November 2026): complete,
harden, connect and measure what SwarmCloud already has, never a second agent
platform. Every later package and every experiment states its change against
this page. Read on main at the commit below; a value this page cannot read from
the tree is written **unknown** and says where it lives, never a guess and
never zero.

The rest of the inventory: [capability matrix](capability-matrix.md),
[known failures](known-failures.md), [experiment protocol](experiment-protocol.md),
[ownership](ownership.md). `scripts/agent_quality_inventory.py` re-reads the
parts of this page that the tree holds (the model pins, the cited tests, the
skip counts), and `tests/unit/scripts/test_agent_quality_inventory.py` fails
when one of them moves without this page.

## 1. Pinned source commit

| | |
|---|---|
| Commit | `3e3a9875458719f438df1c2d426ae1844b4122b3` |
| Branch | `main` |
| Committed | 2026-11-03 07:03:48Z |
| Headline | Contract request 134 asks for a builder class, 8 CPU requested and 12 limit with 16 GiB, for the steps that build (part of the Day One install program) |

The program's research read main at its 3 November morning commit. About a
dozen merges landed between that read and this commit; each is in the tree read
here and appears in the matrix where it belongs:

| Merge since the research read | Where it is on this commit |
|---|---|
| Incremental index carry | `apps/swarm-graph/swarm_graph/carry.py`, `apps/swarm-api/swarm_api/repoindex.py::graph_carry_refusal` |
| The graph stats ticket | `apps/swarm-api/swarm_api/graph_ticket.py::mint_stats_ticket` |
| The cache panel's latency | `apps/swarm-ui/src/RepoGraphCache.tsx` (`latencyTile`) |
| Cost across context-ceiling restarts, and the console's "at least" | `apps/agent-worker/agent_worker/runners/ceiling.py` (`COST_INCOMPLETE_KEY`, `EARLIER_TOOL_CALLS_KEY`), `apps/swarm-ui/src/PlatformOverTime.tsx` (`FLOOR`) |
| Related-tests narrowing, graph tests added to the import pick | `scripts/lib/related_tests.py::python_related` |
| The time-boxed parallel publish check | `apps/agent-worker/agent_worker/prepublish.py::timeboxed_guards` (GUARDS-TIMEBOX) |
| The review start-up bound | `apps/agent-worker/agent_worker/contextpack/build.py` `PACK_DEADLINE_SECONDS = 30.0` (REVIEW-STARTUP-WAIT) |

## 2. Agent images

**The deployed digests are unknown from here.** The brief asked for them from
the environment's tfvars, and neither tfvars file holds one, by design:

* `terraform/environments/dev/dev.tfvars` and `prod/prod.tfvars` say "No image
  is named here. Every image is deployed by digest through `image_refs`".
* `terraform/infra/variables.tf` `variable "image_refs"` accepts only
  `<path>@sha256:<64 hex>`, written by `scripts/lib/image-refs.sh` from the
  promotion manifest `build/deployed-images-<env>.json` (or `--applied`, from
  Terraform state). `build/` is in `.gitignore`, so no agent image digest is
  committed anywhere in the tree.
* `terraform/infra/verify.tf` refuses an apply with an image that has no digest.

What an experiment records instead, per run: the `image_refs` the environment
applied (`scripts/lib/image-refs.sh --applied`) at the batch's start and end. If
the two differ, a release landed during the batch, which
[the protocol](experiment-protocol.md#2-batch-three-conditions-adopted) forbids.

The agent images and the profiles that run them (`terraform/infra/locals.tf`
`runner_profiles`, mirroring `swarm_common.profiles.RUNNER_PROFILES`):

| Image | Profiles |
|---|---|
| `agent-runtime-base` | `claude-code`, `claude-code-large`, `codex`, `generic`, `mock`, `merge` (and the retired `post-verdict`, `claude-code-review`; the control-plane `await-input`, `fix-loop`) |
| `agent-runtime-browser` | `claude-code-browser`, `browser` |
| `agent-runtime-lightpanda` | `claude-code-lightpanda` |
| `agent-runtime-indexer` | `indexer` |

What *is* pinned by digest in the tree is the toolchain the images are built
from: `images/agent-runtime-base/Dockerfile` `ARG PYTHON_IMAGE`, `NODE_IMAGE`
and `UV_IMAGE` (python 3.11-slim-bookworm, node 24-bookworm-slim, uv 0.12.15,
each `@sha256:`), listed in [docs/versions.md](../../versions.md). The browser,
lightpanda and indexer images are built `FROM ${BASE_IMAGE}`, the base image by
digest.

## 3. Models per class

The one place a model id is stated is `terraform/infra/locals.tf`
`runner_models`; it reaches swarm-api and the scheduler as
`WORKER_CLASS_MODELS`, the scheduler resolves a step's class to an id
(`apps/scheduler/scheduler/dispatch.py::step_model`), and the worker passes it
to the CLI as `--model`. A caller's `model` field is attribution only
(invariant 10). No tfvars overrides it, so dev and prod run the same pins.

| Class | Model id |
|---|---|
| `opus` | `claude-opus-5-5` |
| `fable` | `claude-fable-5-1` |
| `sonnet` | `claude-sonnet-5-5` |
| `haiku` | `claude-haiku-5-5` |

`swarm_common.profiles.ModelClass.admits` refuses an id below generation 5 at
scheduler and swarm-api start.

| Profile | Resource class | Model classes | Default class |
|---|---|---|---|
| `claude-code` | standard | opus, fable, sonnet, haiku | opus |
| `claude-code-large` | large | opus, fable, sonnet, haiku | opus |
| `claude-code-browser` | browser | opus, fable, sonnet, haiku | opus |
| `claude-code-lightpanda` | standard | opus, fable, sonnet, haiku | opus |
| `indexer` | standard | opus, fable, sonnet, haiku | opus |
| `codex` | standard | none | none: `codex exec` runs its own default model, which this tree does not pin (**unknown** from here) |
| `browser`, `generic`, `mock`, `merge` | browser / standard | none | no model |

A step picks its class (`model_class`); without one,
`swarm_common.profiles.step_model_class` gives the profile's default. The
repository index's runs default to `sonnet` (`apps/swarm-api/swarm_api/repoindex.py`,
[docs/repo-index.md](../../repo-index.md)). The reasoning behind the classes
and their prices is [model-per-step.md](../model-per-step.md).

## 4. Switches and settings that change agent behaviour

Three layers set them. **Terraform** (`terraform/infra/variables.tf`, set per
environment in tfvars) gates platform features. **The workspace's settings
document** (`workspace_settings/<tenant>`, written by swarm-api, signed into
the dispatch) sets per-workspace behaviour; its live values are in Firestore
and are **not read from here**. **Code constants** are the same everywhere.

### 4.1 Workspace settings (per workspace; live values not read from here)

| Setting | Read by | Default when unset | What it changes |
|---|---|---|---|
| `code_intelligence.context` | `agent_worker/graphstage.py` `CONTEXT_FIELD` | `on` (`DEFAULT_CONTEXT`) | whether the graph snapshot and context pack are staged at all |
| `code_intelligence.context_ab` | `graphstage.py` `AB_FIELD`, `assign_arm` | `ab` (`DEFAULT_AB`): arm by `sha256(task_id) mod 2` | the online A/B; arm A stages nothing |
| `code_intelligence.graph_delivery` | `graphstage.py` | `staged` (`DEFAULT_GRAPH_DELIVERY`) | local snapshot or the `swarm-graph` service |
| `code_intelligence.jev` | `agent_worker/judge/settings.py::JudgeSettings` | off | every Jev call (J0-J5, `jev_decide`, `JEV_SYSTEM_NOTE`) |
| `code_intelligence.judgment_backend` | `judge/settings.py::resolve_backend` | `internal-jev` | which Jev backend answers |
| `code_intelligence.jev_monthly_cap_tokens` | `judge/budget.py::FirestoreMonthlyLedger` | no cap | Jev's monthly token ceiling |
| `quality_gates.jev` | `agent_worker/qualitygates.py::decide` | off | Jev's code-quality gates (hooks) |
| `findings_epic` | `agent_worker/findings_epic.py` | none: minors are not posted | where review minors go |
| `merge_mode`, `train_size` | `swarm_api/mergesettings.py` | `direct`, 4 | merge slot or merge train |
| workspace budget | `swarm_api/budget.py::resolve` | no budget | the cost ceiling an agent stops at (`WARN_AT` 0.8, `STOP_AT` 1.0, `KILL_AT` 1.2 in `agent_worker/budget.py`) |

### 4.2 Terraform variables

| Variable | Default (`variables.tf` / `child_tasks.tf`) | dev.tfvars | prod.tfvars |
|---|---|---|---|
| `enable_child_tasks` | false | true | unset (false) |
| `forge_broker` | false | false | unset (false) |
| `additional_repositories_enabled` | false | true | unset (false) |
| `mcp_servers_enabled` | false | true | unset (false) |
| `search_tool_enabled` | false | false | unset (false) |
| `enable_live_browser` (`live.tf`) | false | true | unset (false) |
| `enable_graph_service` (`graph_service.tf`) | false | true | unset (false) |
| `workspace_gate` | false | true | unset (false) |
| `spec_signature_mode` (`spec_signing.tf`) | `enforce` | `enforce` | `enforce` |
| `pool_limits` | global 100, default_tenant 20, provider_tenant 10 | global 180, `claude-code` 80, `claude-code-browser` 100, `claude-code-lightpanda` 100 | global 100, `claude-code` 60; no `claude-code-browser` or `-lightpanda` entry |
| `pr_console_links` | false | unset (false) | unset (false) |

Which environment the batches ran against, and its applied values, is a fact
of the deployment: **not read from here**.

### 4.3 Code constants (same in every environment)

| Constant | Value | Where |
|---|---|---|
| Context ceiling (a fresh session with a handoff past it) | 200,000 tokens or 150 tool calls; at most 4 fresh sessions | `agent_worker/runners/ceiling.py` `CONTEXT_CEILING_TOKENS`, `TOOL_CALL_CEILING`, `MAX_FRESH_SESSIONS`; a step's `input.context_ceiling` may only lower them |
| Max turns | uncapped unless the step lowers it | `swarm_api/validation.py::validate_max_turns` (contract request 133) |
| Graph tools loaded up front | `alwaysLoad: true` on `swarm-graph`, `jev` and `swarm-jev` when registered | `agent_worker/runners/claude_code.py` `ALWAYS_LOAD` |
| When to call the graph and Jev | `GRAPH_SYSTEM_NOTE`, `JEV_SYSTEM_NOTE` in `--append-system-prompt`, only with the switch on | `claude_code.py::system_note` |
| Graph snapshot format floor | 3, and an ancestor of the checkout | `graphstage.py` `MIN_FORMAT`, `prove_ancestry` |
| Context pack bound | 30 s for the whole pack (10 s per service answer) | `contextpack/build.py` `PACK_DEADLINE_SECONDS`, `SERVICE_TIMEOUT_SECONDS` |
| Pre-publish gate | skipped below 120 s of budget; 600 s per check; 1 fix turn | `agent_worker/prepublish.py` `MIN_GATE_SECONDS`, `CHECK_TIMEOUT_SECONDS`, `FIX_TURNS` |
| Guard run in the gate | time-boxed, parts in parallel up to the pod's CPUs; what it did not reach is left to CI, never passed | `prepublish.py::timeboxed_guards` |
| Profile timeouts | `claude-code` 7,200 s, `claude-code-large` 14,400 s, `claude-code-browser` 7,200 s | `terraform/infra/locals.tf` `runner_profiles` |
| Reasoning effort | **no switch in this tree** | searched `effort` across the worker, the API and the contract |
