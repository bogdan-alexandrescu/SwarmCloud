# Requests against the frozen contract

`apps/common/swarm_common/` is frozen. CLAUDE.md rule 1 is explicit about what
to do when you believe it needs to change: *say so instead of changing it.* This
file is where that is said, so a request survives the session that found it.

Nothing here is applied unless its **Status** line says so. Each entry states the
defect, the proof, what the change would be, and — the part that decides whether
it is worth it — what breaks downstream if it is made, and what is left for
somebody to live with if it is declined.

These are requests for a person to decide. Nothing in this file is a plan.

| 1 | `identity.py`: the `u-` prefix does not namespace personal tenants | open |
| 2 | `models.py`: `Attempt` records memory and disk, but not tokens or cost | APPLIED 2026-09-19 |
| 3 | `models.py`: a typed `input_from` on `Task` | open |
| 4 | A typed artifact-manifest entry | open |
| 5 | `gke_api_host` in a shared module (`swarm_common/kube.py`) | open |
| 6 | A typed dispatch block | open |
| 7 | `models.py`/`states.py`: the workflow rollup has no shared home | open |
| 8 | `states.py`: `Workflow.state` reuses `TaskState`; a workflow vocabulary | open |
| 9 | `models.py`: `Lease.dispatch_overdue` excluded the leases it names | APPLIED 2026-09-22 |
| 10 | `profiles.py`: `requires_preview_disk` names a feature this platform does not use | open |
| 11 | `profiles.py`: a runner profile had no way to be turned off | applied |
| 12 | `models.py`: the retry cap had no shared home, so one path forgot it | applied |
| 13 | `models.py`: `Attempt` does not record which pool account it ran on | open |
| 14 | `models.py`: a sub-agent has nowhere to name its parent | APPLIED 2026-10-02 (accepted by the owner 2026-10-02, decision OD-B15-1, with the cancel and capacity rules of its amendment; built as docs/design/child-tasks.md phase 2) |
| 15 | `models.py`: `Attempt` records memory, disk and spend, but not CPU | ACCEPTED 2026-09-25 (owner, on #184), applied in PR #210 |
| 16 | `identity.py`: the tenant namespace name, `sanitize_name` included, has two copies | open |
| 17 | `states.py`: a cancel that is only requested is recorded as `cancelled` (incident CR-1) | ACCEPTED 2026-09-24 (the owner's "#13"), applied in PR #44 |
| 18 | `profiles.py`: `RunnerProfile.command` is the lifecycle's child argv, never a container command (incident CR-2) | ACCEPTED 2026-09-24 (the owner's "#14"), rename applied in PR #44 |
| 19 | `states.py`: no `EventType` says "the reconciler evicted this execution" | open |
| 20 | `models.py`: `Workflow.on_step_failure` is a bare `str`, and its vocabulary is stated four times | open |
| 21 | `states.py`: the worker's exit codes have no shared home, and the reconciler now acts on one | open |
| 22 | `profiles.py`: whether a runner profile can run on a pool account is stated by the worker and restated by the scheduler | open |
| 23 | `models.py`: a task's end has no typed cause, so the outcome ledger classifies `last_error` text | ACCEPTED 2026-09-25 (#185, decision 9), applied in PR #217 |
| 24 | `profiles.py`: whether a profile's cost is declared rather than measured is named outside the catalogue | ACCEPTED 2026-09-25 (#185, decision 9), applied in PR #217 |
| 25 | `profiles.py`: a runner profile cannot declare the inputs a caller may send it, so the bridge names the mock's by profile | ACCEPTED 2026-09-25 (owner, on #142), applied in PR #213; both amendments confirmed by the owner 2026-09-26: `inputs=None` for `browser` and `generic` (#218), and the bounded park counted by the task's `attempt_count` rather than the state file |
| 26 | `models.py`: the attempt's CPU figures carry no time and their limit no source | ACCEPTED 2026-09-26 (owner, on #184), applied in PR #229 |
| 27 | `identity.py`: `_slug`'s docstring still sizes tenant ids for the `swarm-t-` prefix that no longer exists | ACCEPTED 2026-09-28 by the owner on #245, applied in PR #245 |
| 29 | `models.py`: `EndCause` gains `PUBLISH_REFUSED` (#259) | APPLIED 2026-10-02 (accepted by the owner 2026-09-29 on #259), functionality wave 3, lane B46 |
| 30 | `identity.py`: a tenant may list service accounts that resolve to it by exact email (#273) | ACCEPTED 2026-09-29 by the owner after three security reviews |
| 31 | `models.py`: `WorkflowStep` cannot record a step's verdict gate or its `builds_on` | open |
| 32 | `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none (#218) | ACCEPTED 2026-09-29 by the owner after three security reviews, applied by #345 |
| 33 | `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it (#295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01; #364 amendment items 1-3 applied: item 1 with it, items 2-3 in `terraform/modules/secret_manager`, recorded 2026-10-08) |
| 34 | `models.py` / `specsign.py`: a step's spec is signed by swarm-api and verified by every worker (#342) | ACCEPTED 2026-09-29 by the owner after three security reviews, applied in PR #353 (code) and #354 (Terraform) |
| 35 | `profiles.py` / `models.py`: the `post-verdict` worker-action profile, and its own end causes (part of #295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01) |
| 36 | `profiles.py`: the `claude-code-review` profile, and a typed `never_restore_checkpoint` (part of #295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01) |
| 37 | `config.py` / `admission.py`: the lease's dispatch deadline is 300 s, shorter than a slow cold start plus the worker's startup read (#401) | ACCEPTED 2026-09-30 by the owner, applied by this PR (#404) |
| 38 | `states.py` / `admission.py`: a pool with no `hard_limit` is refused as "set to 0" (#374) | accepted by the owner 2026-10-05 (recorded on #374), IMPLEMENTED 2026-10-05 (functionality wave 7, lane CR38) |
| 39 | `states.py` / `models.py`: `ParkReason.BUDGET_EXHAUSTED` names a park nothing writes, because there are no budgets (owner, 2026-10-01) | open |
| 40 | `states.py`: an agent awaiting its children has no park reason, and `DEPENDENCY_INCOMPLETE` would be promoted at once (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 41 | `models.py`: a child cancelled because of its parent has no end cause (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 42 | `specsign.py`: the signed step spec does not cover a child's parent (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 43 | `identity.py`: the tenant worker service account's name has no public home (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 44 | `states.py`: `account_assigned` and `account_released` ride on `RUNNING` and `LEASE_RELEASED` (functionality wave 1, lane B4) | open |
| 45 | `profiles.py`: the catalogue does not say which runner profiles report a cost (filed with #72) | open |
| 46 | `models.py`: a pull-request step that published nothing has no end cause (functionality wave 3, lane B46) | open |
| 48 | `profiles.py`: no runner profile runs `agent-runtime-indexer`, so index runs cannot reach the repo-index toolchain (filed with #625, functionality wave 8, lane IMG) | open |
| 49 | `states.py`: a merge step waiting for its pull request's checks has no park reason (docs/merge-step.md 2026-10-06 request (A), lane MS1) | accepted by the owner 2026-10-06 (#352), to be applied by lane MS2 |
| 50 | `profiles.py` / `models.py`: retire the disabled `single-pr` catalogue entries (docs/merge-step.md 2026-10-06 request (B), lane MS1) | open; removal decided by the owner 2026-10-06 for a cleanup lane |
| 51 | `models.py`: `Attempt` does not type `checkpoint_sha256`, the digest a retry binds its restore to (#350, part of S0 #347) | ACCEPTED by the owner 2026-10-09 and APPLIED by the pull request that adds this line (proposed 2026-10-07, then accepted 2026-10-09) |
| 52 | `states.py`: a step the control plane finishes without a worker cannot end SUCCEEDED from PARKED (#748) | ACCEPTED by the owner 2026-10-09 and APPLIED by the pull request that adds this line (proposed (owner 2026-10-07: not accepted for now), then accepted 2026-10-09) |
| 53 | `profiles.py`: run the `claude-code` profile on GKE Autopilot, whose fresh-node start p90 is 120 s against Cloud Run's 212 s (#363, #625, #667; the owner's pre-set rule of 2026-10-07 met) | APPLIED 2026-10-08 (accepted by the owner 2026-10-07, with conditions; switched after request 55's canary passed 5/5) |
| 54 | `models.py` / `specsign.py`: a task does not say which forge credential it uses, or whether it may write (request E of docs/onboarding.md §3.3, part of #780) | APPLIED 2026-10-07 (accepted by the owner 2026-10-07) |
| 55 | `profiles.py`: a temporary `claude-code-gke` profile, the canary for request 53 | REMOVED 2026-10-08 (the switch replaced it); accepted and applied 2026-10-07 |
| 56 | `profiles.py`: the `mock` profile cannot write its artifact before a simulated park, so #166's carry cannot be proven live | APPLIED 2026-10-08 (accepted by the owner 2026-10-08) |
| 57 | `profiles.py`: `url_refusal` accepts site-local, Teredo and other reserved IPv6 ranges, two internal names and a trailing double dot, and a refused URL's 422 can echo its userinfo (#349) | APPLIED 2026-10-08 (accepted by the owner 2026-10-08) |
| 58 | `specsign.py`: `3600` and `3600.0` have the same canonical bytes, so a rewrite of an integer field to a float keeps the signature valid (#346 box 53) | ACCEPTED by the owner 2026-10-11 and APPLIED by the pull request that adds this line (proposed 2026-10-09) |
| 59 | `specsign.py`: a spec nested past Python's recursion limit raises `RecursionError` instead of `SpecNotCanonical` (#346 box 55) | ACCEPTED by the owner 2026-10-11 and APPLIED by the pull request that adds this line (proposed 2026-10-09) |
| 60 | `profiles.py`: a worker-action profile with `secrets`, a GKE backend or a plain-string `worker_action` passes `__post_init__` (#453 box 87) | ACCEPTED by the owner 2026-10-11, applied in this PR (item 2 scoped to `post-verdict`, per its amendment, so `merge` on GKE still imports) |
| 61 | `profiles.py`: `url_refusal` still accepts the benchmarking and deprecated ORCHID ranges of `2001::/23`, the remainder of #346 box 42 after request 57 | ACCEPTED by the owner 2026-10-11, applied in this PR |
| 62 | `states.py`: a merge step waiting for its repository's merge slot parks as `CI_PENDING`, a reason that says it waits for CI | proposed |
| 63 | `profiles.py`: indexer runs on GKE_AUTOPILOT (canary for #939) | ACCEPTED by the owner 2026-10-10 and APPLIED by the pull request that adds this line |
| 64 | `profiles.py`: generic runs on GKE_AUTOPILOT (#939 option A) | ACCEPTED by the owner 2026-10-10 and APPLIED by the pull request that adds this line, held until the indexer canary is measured |
| 65 | `profiles.py`: codex runs on GKE_AUTOPILOT (#939 option A) | ACCEPTED by the owner 2026-10-10 and APPLIED by the pull request that adds this line, held until the indexer canary is measured |
| 66 | `profiles.py`: merge runs on GKE_AUTOPILOT (#939 option A); post-verdict and claude-code-review stay on CLOUD_RUN_JOB | ACCEPTED by the owner 2026-10-10 and APPLIED by the pull request that adds this line, held until the indexer canary is measured; conflicts with proposed request 60's item 2 (see 60's amendment) |
| 67 | `profiles.py`: a `claude-code-browser` runner profile, claude-code on agent-runtime-browser | ACCEPTED by the owner 2026-10-11 and APPLIED by the pull request that adds this line |
| 68 | `states.py`: `ParkReason.HUMAN_REQUIRED`, a live-browser hand-off parked when nobody came within the hold (LB-A) | ACCEPTED by the owner 2026-10-11 (#1030) and APPLIED by the pull request that adds this line |
| 69 | `states.py`: the four `human_*` hand-off `EventType` members (LB-B) | ACCEPTED by the owner 2026-10-11 (#1030) and APPLIED by the pull request that adds this line |
| 70 | `models.py`: `Task.human_wait`, the hand-off a holding task waits on, as a field and not a new state (LB-C) | ACCEPTED by the owner 2026-10-11 (#1030) and APPLIED by the pull request that adds this line |
| 71 | `profiles.py`: `RunnerProfile.live_browser`, True only on `claude-code-browser`, valid only on a GKE_AUTOPILOT profile (LB-D) | ACCEPTED by the owner 2026-10-11 (#1030) and APPLIED by the pull request that adds this line |
| 72 | `profiles.py`: the `post-verdict` and `claude-code-review` comments name per-tenant accounts that no longer exist | proposed |
| 74 | `states.py`: `EventType.STARTUP_PROGRESS`, the browser agent's start-up steps (STARTUP-API) | ACCEPTED by the owner 2026-10-13 and APPLIED by the pull request that adds this line |
| 75 | `models.py`: `Workflow.fix_rounds` and `WorkflowStep.spawned_by`, a merge step's fix rounds as workflow fields (SPEED-D2) | proposed; worked around |
| 76 | `models.py`/`states.py`: `Workflow.after` and `ParkReason.PULL_REQUEST_PENDING`, a workflow that waits for pull requests to merge (SPEED-D3) | proposed; worked around |
| 77 | `profiles.py`: `claude-code-browser` declares the input `browser_tool` (auto, playwright, dev-browser, jev), the live browser's tool picked by name (BSTATUS-API) | ACCEPTED by the owner 2026-10-14 ("Picker on Submit, Auto by default"), APPLIED by this PR |
| 78 | `models.py` / `states.py`: a workspace's merge mode on `Tenant`, and a park reason for a merge step waiting on its merge train (TRAIN-WS) | proposed; worked around |
| 79 | `profiles.py`: the `browser` resource class shrinks from 8 vCPU / 16 GiB to 4 vCPU / 8 GiB, and from 2 units to 1 (MEASURE, BROWSER-CLASS) | ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line |
| 80 | `profiles.py`: `RunnerProfile.secret_slots`, `SecretSlot` and `SECRET_KINDS`, the workspace-secret slots a profile may consume (workspace secrets request A, part of #1347) | ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line (proposed 2026-10-14; accepted on PR #1354) |
| 81 | `profiles.py`: `RUNTIME_TOOLS`, the runtime tools (`jev-browser`, `dev-browser`, `playwright`), their secret slots and the `browser_tool` values that launch them (workspace secrets request B, part of #1347) | ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line (proposed 2026-10-14; accepted on PR #1354) |
| 82 | `models.py`: `SECRET_CONSUMER_KINDS`, `PLATFORM_COMPONENTS` and `consumer_name`, one spelling of a secret consumer (workspace secrets request C, part of #1347) | ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line (proposed 2026-10-14; accepted on PR #1354) |
| 83 | `states.py`: no new park reason; a required secret slot with no grant parks `CREDENTIAL_MISSING` (workspace secrets request D, part of #1347) | ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line (proposed 2026-10-14; accepted on PR #1354) |
| 84 | `profiles.py`: `claude-code-review` is the one CLOUD_RUN_JOB profile that carries the Jev client; move it to GKE_AUTOPILOT, or retire it under request 50 (JUDGE-GKE, part of #1347) | ACCEPTED as (b) by the owner 2026-10-16 (on PR 1422): retired under request 50, not moved; nothing to apply |
| 85 | `states.py`: `WORKER_TRANSITIONS` and `worker_may_transition`, the transitions a tenant worker may request (ISO-CONTRACT, tenant settings integrity request A, part of #1457) | ACCEPTED by the owner 2026-10-17 (PR 1464) and APPLIED by the pull request that adds this line |
| 86 | `specsign.py`: `canonical_settings_block`, `SETTINGS_MAC_FIELDS` and `SETTINGS_PURPOSE`, the bytes a `workspace_settings` MAC covers (ISO-CONTRACT, tenant settings integrity request B, part of #1457) | ACCEPTED by the owner 2026-10-17 (PR 1464, Q2: the interim MAC) and APPLIED by the pull request that adds this line |
| 87 | `models.py`: `Tenant.service_account`, `.namespace` and `.gcs_prefix` are records, not authority (ISO-CONTRACT, tenant settings integrity request C, part of #1457) | ACCEPTED by the owner 2026-10-17 (PR 1464) and APPLIED by the pull request that adds this line (comments only) |
| 88 | `profiles.py`: `claude-code`, `codex` and `claude-code-browser` declare the input `mcp_servers`, from a dict of its own (MCP-CONTRACT, registered MCP servers request M1, part of #1433) | ACCEPTED by the owner 2026-10-17 (PR 1466) and APPLIED by the pull request that adds this line |
| 89 | `models.py` / `profiles.py`: `mcp` is a secret-consumer kind, with one slot, `auth` (`MCP_SECRET_SLOTS`) (MCP-CONTRACT, registered MCP servers request M2, part of #1433) | ACCEPTED by the owner 2026-10-17 (PR 1466) and APPLIED by the pull request that adds this line |
| 90 | `models.py`: `MCP_SERVER_NAME`, `RESERVED_MCP_SERVER_NAMES` and `RESERVED_MCP_SERVER_PREFIX`, one spelling for an MCP server's name (MCP-CONTRACT, registered MCP servers request M3, part of #1433) | ACCEPTED by the owner 2026-10-17 (PR 1466) and APPLIED by the pull request that adds this line |
| 91 | No new spec format, park reason or event type for registered MCP servers (MCP-CONTRACT, registered MCP servers request M4, part of #1433) | ACCEPTED by the owner 2026-10-17 (PR 1466); requests no change, so nothing to apply |
| 92 | `states.py`: `ParkReason.AWAITING_INPUT`, a workflow step waiting for a person's answer (HW-CONTRACT, human steps request A, part of #1436) | ACCEPTED by the owner 2026-10-17 (Q1, on PR 1462/1493) and APPLIED by the pull request that adds this line |
| 93 | `profiles.py`: `RunnerProfile.control_plane`, a catalogue profile the platform never leases (HW-CONTRACT, human steps request B, part of #1436) | ACCEPTED by the owner 2026-10-17 (Q10, on PR 1493) and APPLIED by the pull request that adds this line |
| 94 | `profiles.py`: the `await-input` catalogue entry, shipped unavailable (HW-CONTRACT, human steps request C, part of #1436) | ACCEPTED by the owner 2026-10-17 (on PR 1493) and APPLIED by the pull request that adds this line; `available=True` is request D, which the owner applies after HW-ACCEPT, and is not filed or applied here |
| 95 | `models.py`: `Task.additional_repositories`, the registered repositories a step reads beside its own, with `check_additional_repositories` and a shape ceiling of 10 (MR-CONTRACT, multi-repository steps request A, part of #1434) | ACCEPTED by the owner 2026-10-17 (PR 1461; ceiling 10, the operative limit a platform setting) and APPLIED by the pull request that adds this line |
| 96 | `specsign.py`: spec format 4 covers `additional_repositories`, signed at 4 only when the list is set (MR-CONTRACT, multi-repository steps request B, part of #1434) | ACCEPTED by the owner 2026-10-17 (PR 1461) and APPLIED by the pull request that adds this line |
| 97 | No change to `WorkflowStep`, `Workflow` or `profiles.py` for multi-repository steps (MR-CONTRACT, multi-repository steps request C, part of #1434) | ACCEPTED by the owner 2026-10-17 (PR 1461); requests no change, so nothing to apply |
| 98 | `states.py`: QUEUED -> FAILED, a task that can never run ends at admission with no lease (MCP-FAILED-EDGE, part of #1433) | ACCEPTED by the owner 2026-10-18 (answering PR 1504's question) and APPLIED by the pull request that adds this line; KEPT by the owner 2026-10-18 when accepting request 99. No writer today: no task is stored QUEUED, so admission writes request 99's edges instead |
| 99 | `states.py`: READY -> FAILED and PARKED -> FAILED, the two states admission and the credential sweep actually hold (MCP-FAILED-EDGE, part of #1433) | ACCEPTED by the owner 2026-10-18 (on PR 1530) and APPLIED by the pull request that adds this line (MCP-FAILED-EDGE-B); admission writes READY -> FAILED, the credential sweep PARKED -> FAILED |
| 100 | `profiles.py`: `ModelClass` and `MODEL_CLASSES`, the four model classes a step may name (MODEL-CONTRACT, model per step request A, part of #1435) | ACCEPTED by the owner 2026-10-17 (PR 1509) and APPLIED by the pull request that adds this line |
| 101 | `profiles.py`: `RunnerProfile.model_classes`, `default_model_class` and `step_model_class`, each profile's classes and the one resolution rule (MODEL-CONTRACT, model per step request B, part of #1435) | ACCEPTED by the owner 2026-10-17 (PR 1509) and APPLIED by the pull request that adds this line |
| 102 | `models.py`: `Task.model_class` and `WorkflowStep.model_class`, the class a step asked for (MODEL-CONTRACT, model per step request C, part of #1435) | ACCEPTED by the owner 2026-10-17 (PR 1509) and APPLIED by the pull request that adds this line |
| 103 | `models.py`: `Attempt.model`, the model ID an attempt's agent was started with (MODEL-CONTRACT, model per step request D, part of #1435) | ACCEPTED by the owner 2026-10-17 (PR 1509) and APPLIED by the pull request that adds this line |
| 104 | `specsign.py`: spec format 5 covers `model_class`, signed at 5 only when a class is set (MODEL-CONTRACT, model per step request E, part of #1435) | ACCEPTED by the owner 2026-10-17 (PR 1509) and APPLIED by the pull request that adds this line |
| 105 | `profiles.py`: `await-input` becomes available, `available=True` and its `disabled_reason` removed (HW-ENABLE, human steps request D, part of #1436) | ACCEPTED by the owner 2026-10-18 (on PR 1570, turned on in dev before HW-ACCEPT so the acceptance group can run against it) and APPLIED by the pull request that adds this line |
| 106 | `profiles.py`: `claude-code-browser` declares `qa_identity`, one choice `console`, the live browser signed in to the console as the QA identity (QI-CONTRACT, QA identity request QI-1, part of #1649) | ACCEPTED by the owner 2026-10-19 (option B, design `docs/design/qa-identity.md` §7) and APPLIED by the pull request that adds this line |
| 107 | `models.py`: `Attempt.throttled_seconds`, `peak_throttled_share`, `mean_throttled_share` and `memory_limit_bytes`, the attempt's measured CPU throttling and memory limit beside its CPU figures (LIVE-UTIL-API, part of #1689) | open, REQUESTED 2026-10-20 by lane LIVE-UTIL-API. Not applied: the four ride on each HEARTBEAT event until this is decided |
| 108 | `profiles.py`, `CONTRACT.md`: invariant 7 amended for CPU, `CPU_BURST_MAX_RATIO` and `ResourceClass.cpu_limit`, every class's CPU limit twice its request while memory and disk stay request == limit (SIZES-CONTRACT-1, runner sizes request RS-1, part of #1689) | ACCEPTED by the owner 2026-10-20 (design `docs/design/runner-sizes.md` §5, §7 Q1 A) and APPLIED by the pull request that adds this line. No pod bursts yet: the scheduler renders `cpu_limit_cores` in lane SIZES-SCHED-1 |
| 109 | `profiles.py`: the frozen runner-size bounds, `units_for` and `check_size`, the one validator every admin-set size passes (SIZES-CONTRACT-2, runner sizes request RS-2, part of #1689) | ACCEPTED by the owner 2026-10-20 (design `docs/design/runner-sizes.md` §5, §7) and APPLIED by the pull request that adds this line. Nothing calls `check_size` yet: the admin route is lane SIZES-API, the scheduler's read SIZES-SCHED-2 |
| 110 | `profiles.py`: `ResourceSize`, `LiveShare`, `default_sizes` and `effective_size`, the one shape a runner-size version is read in, and the rule that only a GKE profile follows it (SIZES-CONTRACT-2, runner sizes request RS-3, part of #1689) | ACCEPTED by the owner 2026-10-20 (design `docs/design/runner-sizes.md` §5, §7 Q3 A) and APPLIED by the pull request that adds this line. No pod changes size until SIZES-SCHED-2 renders from it |
| 111 | `models.py`: `Attempt.size_version` and the `sized_*` fields, the size the scheduler rendered an attempt at (SIZES-CONTRACT-2, runner sizes request RS-4, part of #1689) | ACCEPTED by the owner 2026-10-20 (design `docs/design/runner-sizes.md` §5, §7) and APPLIED by the pull request that adds this line. No writer yet: the scheduler's `create_attempt` writes them in SIZES-SCHED-2 |
| 112 | `CONTRACT.md`: "Primary backend Cloud Run Jobs" no longer says where agents run; every agent profile but `mock` is on GKE Autopilot (DOCS-TRUTH, part of #1822) | open, REQUESTED 2026-10-21 by lane DOCS-TRUTH. Not applied: `CONTRACT.md` is not edited by a lane |
| 113 | `profiles.py`: a `claude-code-large` runner profile on the `large` class with a 14,400 s timeout, picked by name for heavy lanes (AUTONOMY-DESIGN, part of #1933) | ACCEPTED by the owner 2026-10-22 and APPLIED by the pull request that adds this line (lane AU9-PROFILE-LARGE, part of #1933; Q6 of `docs/design/agent-autonomy.md` §7: "Accept contract request 113: a claude-code-large profile") |
| 114 | `CONTRACT.md` invariant 7: swarm-api alone starts with Cloud Run's startup CPU boost, the one recorded exception; every other Cloud Run service and every worker stays under invariant 7 as written (API-CPU-BOOST, part of #1816) | ACCEPTED by BGD 2026-10-23 (#1816 option (a)) and APPLIED by PR 2132; recorded here by lane FOLLOWUPS-1023 (part of #2150). The invariant's text is unchanged |
| 115 | `models.py`: `Task.publish_to_pr`, the existing pull request a lane's commits are pushed onto instead of opening one, typed rather than carried in `metadata.dispatch` (PUBLISH-TO-PR, part of #2183) | open, REQUESTED 2026-10-24 by lane PUBLISH-TO-PR. Not applied, and not needed for the feature to work: the field ships in the signed `metadata.dispatch` block, as `strategy` and `continues` do |
| 116 | `specsign.py`: `SETTINGS_MAC_FIELDS` gains a `"qa"` block, `("flows_per_step",)`, so the workspace's QA batch size is signed like `merge` and `code_intelligence` (QA-TRIPWIRE-MAC, part of #2136) | ACCEPTED by BGD 2026-10-26 (answering QA-FLOWS-CAP's question, task_23a1c31d528e450187b8). Not applied: `apps/common/swarm_common/` is frozen and a lane does not edit it |
| 117 | `profiles.py`: a `cf-access` secret slot on `claude-code-browser`, kind `oauth_client`, whose value the live gate sends as Cloudflare Access service-token headers to the hosts its grant lists and the agent never holds (CF-ACCESS-TOKEN, part of #2442) | ACCEPTED 2026-10-26 by BGD (requested by lane CF-ACCESS-TOKEN as 116, renumbered: QA-TRIPWIRE-MAC took 116 first) and APPLIED by the pull request that adds this line (lane CF-ACCESS-CONTRACT, part of #2442): `_CF_ACCESS_SLOT` beside `_FILES_SLOT`, declared on `claude-code-browser` only. A grant into `cf-access` is accepted with a host list and refused without one |
| 118 | `models.py`: `EndCause.BUDGET_EXCEEDED`, a step the worker stopped because it spent its budget (BUDGET-CAP, part of #2459) | ACCEPTED by BGD 2026-10-26 (~22:15Z, answering BUDGET-CAP's questions on PR 2466; requested by lane BUDGET-CAP as 117-120 and renumbered: CF-ACCESS-TOKEN took 117 on main first). APPLIED 2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part of #2459): the worker writes it on a budget stop; the outcome ledger classes it `runner_error`, as its text fallback did |
| 119 | `specsign.py`: a `"budget"` block in `SETTINGS_MAC_FIELDS`, `("unit", "amount")`, so the workspace's per-agent budget is signed (BUDGET-CAP, part of #2459) | ACCEPTED by BGD 2026-10-26 (~22:15Z, answering BUDGET-CAP's questions on PR 2466; requested by lane BUDGET-CAP as 117-120 and renumbered: CF-ACCESS-TOKEN took 117 on main first). APPLIED 2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part of #2459): `BudgetStore` signs every write and verifies every read for a tenant on the MAC cutover, and `judgment.WRITER_FIELDS` lists the block so the re-sign pass signs stored ones |
| 120 | `models.py` / `specsign.py`: `Task.budget` and `WorkflowStep.budget`, a step's budget as a typed, signed field (BUDGET-CAP, part of #2459) | ACCEPTED by BGD 2026-10-26 (~22:15Z, answering BUDGET-CAP's questions on PR 2466; requested by lane BUDGET-CAP as 117-120 and renumbered: CF-ACCESS-TOKEN took 117 on main first). APPLIED 2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part of #2459): spec format 6; submission writes `Task.budget` and `WorkflowStep.budget`, and the worker and the API read `metadata.dispatch.budget` as the fallback for one release |
| 121 | `states.py`: `EventType.BUDGET_WARNING`, the 80% warning as its own event type (BUDGET-CAP, part of #2459) | ACCEPTED by BGD 2026-10-26 (~22:15Z, answering BUDGET-CAP's questions on PR 2466; requested by lane BUDGET-CAP as 117-120 and renumbered: CF-ACCESS-TOKEN took 117 on main first). APPLIED 2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part of #2459): the worker writes it; `codec.stored_event_type` reads the old `running` form as it for one release |
| 122 | `models.py`: `Tenant.principal`'s comment gives a fictional example on the company's real domain (EXAMPLE-IDENTITIES, part of #1647) | open |
| 123 | `states.py` / `models.py`: `ParkReason.PLATFORM_REFUSED` and `Task.platform_refusals`, so a dispatch the platform refuses parks instead of spending attempts (PLATFORM-REFUSED, part of #1820) | ACCEPTED by the owner (BGD) 2026-10-28 (~11:10Z, after the eng Job-quota incident) and APPLIED 2026-10-28 by the pull request that adds this line (lane PLATFORM-REFUSED-APPLY, part of #1820): the enum member, the field, `PLATFORM_REFUSED_MAX_PARKS` and the edge LEASED -> PARKED. Per BGD's brief the backoff is 30 s doubling to at most 10 min (in the scheduler) and a task past the bound stays parked as trouble, never FAILED |
| 124 | `profiles.py`: a `fix-loop` catalogue profile, `control_plane: true`, shipped unavailable until FL-ACCEPT passes, as `await-input` was (FL-CONTRACT, fix loop request A, part of #2509) | ACCEPTED by the owner (BGD) 2026-10-27 (Q1, Q2, Q10 of docs/design/fix-loop.md §9) and APPLIED by the pull request that adds this line (lane FL-CONTRACT): mirrored in `terraform/infra/locals.tf` and listed in `profiles_without_a_job` |
| 125 | `states.py`: `ParkReason.FIX_ROUNDS_RUNNING`, a fix loop's step while its rounds run: never promoted by the scheduler, in no capacity set, ended by swarm-api (FL-CONTRACT, fix loop request B, part of #2509) | ACCEPTED by the owner (BGD) 2026-10-27 (Q1, Q2, Q10 of docs/design/fix-loop.md §9) and APPLIED by the pull request that adds this line (lane FL-CONTRACT), with the console's and swarm-mcp's park-reason lists |
| 126 | `models.py`: `EndCause.FIX_LOOP_EXHAUSTED`, a fix loop that ended without a MERGE (FL-CONTRACT, fix loop request C, part of #2509) | ACCEPTED by the owner (BGD) 2026-10-27 ('accept-126', answering FL-CONTRACT's question on PR 2570; requested by lane FL-CONTRACT the same day, Q3 (c) needs an end cause). APPLIED 2026-10-28 by the pull request that adds this line (lane FL-CONTRACT-126): appended to `EndCause`; the outcome ledger classes it `fix_loop_exhausted`, its own failure class (DERIVE_VERSION 12), and the console's `EndCause`, `END_CAUSES` and `END_CAUSE_SAY` carry it |
| 127 | `profiles.py`: a `claude-code-lightpanda` catalogue profile, claude-code's runner on image `agent-runtime-lightpanda` and the `standard` class, no live browser, for DOM-only browser work (LIGHTPANDA-ANALYSIS, part of #2688; docs/design/lightpanda-runtime.md) | ACCEPTED by the owner 2026-10-31 and APPLIED by the pull request that adds this line (lane LIGHTPANDA-CONTRACT, part of #2688; BGD: "go with the recommended options", accept contract request 127). Its `terraform/infra/locals.tf` mirror lands in the same pull request once `agent-runtime-lightpanda` is built and pinned, so no plan fails on a missing image digest (CLD for BGD, 2026-10-31) |
| 128 | `specsign.py`: a `"quality_gates"` block in `SETTINGS_MAC_FIELDS`, `("jev",)`, so the workspace's switch for the claude-code lanes' TypeSafe quality checks is signed (JEV-LANES, PR 2905; QUALITY-GATES-SETTINGS-MAC, part of #2688) | ACCEPTED by the owner 2026-10-31 ('Add it (contract request)', answering PR 2905's first question) and APPLIED by the pull request that adds this line (lane QUALITY-GATES-SETTINGS-MAC): swarm-api's `qualitygates.QualityGatesStore` signs the block it writes, and `judgment.WRITER_FIELDS` gains `"quality_gates": ("jev",)` so the re-sign pass signs stored blocks |
| 129 | `admission.py` / `models.py`: a lease takes 1 unit in each `provider:*` pool and its resource units in every other pool, records what it took per pool (`Lease.pool_units`), and release returns exactly that (LARGE-ONE-PROVIDER-SLOT, part of #2950) | ACCEPTED by the owner (BGD) 2026-11-01 (~03:55Z, choosing the expected behaviour #2950 states) and APPLIED by the pull request that adds this line (lane LARGE-ONE-PROVIDER-SLOT, part of #2950): `pool_units_for` is the one rule; a lease from before it has no `pool_units` and releases its `units` from every pool |
| 130 | `profiles.py`: `codex` becomes available, and runs on either the tenant's `OPENAI_API_KEY` or a pool ChatGPT plan (`CODEX_PLAN_ACCESS_TOKEN`, `CODEX_PLAN_ACCOUNT_ID`), `secrets_any_of=True` (CODEX-PLAN-CONTRACT, part of #2688) | ACCEPTED IN PART: accepted by the owner (BGD) 2026-11-01 with one change, "Accept, but keep codex unavailable until the pool lanes land", and APPLIED in part by the pull request that adds this line (lane CODEX-CONTRACT-APPLY): `codex` declares `OPENAI_API_KEY`, `CODEX_PLAN_ACCESS_TOKEN` and `CODEX_PLAN_ACCOUNT_ID` with `secrets_any_of=True`, with its `terraform/infra/locals.tf` `secret_env` mirror; it stays `available=False`, its `disabled_reason` saying it waits for the ChatGPT-plan pool, and a follow-up turns it on once the broker and runner lanes have merged |
| 131 | `models.py`: `Task.timeout_seconds_asked`, the `timeout_seconds` a spec asked for, written by swarm-api only when it stored a lower one; a record only, never a limit (TIMEOUT-ASKED-CONTRACT, part of #2688) | ACCEPTED by the owner (BGD) 2026-11-01 ("API records the ask", answering the pre-publish gate lane's question on #2981, PR 2986; requested the same day by lane TIMEOUT-ASKED-CONTRACT) and APPLIED by the pull request that adds this line (lane TIMEOUT-ASKED-APPLY, part of #2688): the field last in `Task`, unsigned; swarm-api writes it in `_build_task` and a child's `replace`, reads and serves it in its codec and refuses it in a caller's metadata; the console's `Task` type declares it |
| 133 | `profiles.py` / `models.py` / `specsign.py`: `RunnerProfile.supports_max_turns` and `max_turns`, `WorkflowStep.max_turns` and `Task.max_turns`, a turn cap the worker passes to the agent CLI (CONTRACT-MAXTURNS, part of #2524) | REQUESTED for BGD 2026-10-28 (BGD decided ~22:20Z that the cap is a contract field); filed on PR 2682 as 127 and renumbered 2026-11-02 (lane CARRY-TURN-CAP: 127-131 taken on main, 132 by the open `Task.attempt_totals` request). PR 2678's 900 s timeout and 30-turn prompt instruction are the interim on main |

---

## 1. `identity.py`: the `u-` prefix does not namespace personal tenants

**Status:** open, found 2026-09-18 by an audit agent that correctly declined to
edit the frozen module itself.

### The claim that is false

`apps/common/swarm_common/identity.py::tenant_id_for_user`:

```python
def tenant_id_for_user(email: str) -> str:
    """Personal fallback tenant, namespaced so it cannot collide with a group."""
    return _slug(email, prefix="u-")
```

It can collide with a group, because the group path adds no prefix at all:

```python
def tenant_id_for_group(group_email: str) -> str:
    return _slug(group_email)
```

`_slug` appends a disambiguating digest only when the slug is *lossy* or *too
long*. A group whose local part is already a clean, short string starting with
`u-` is neither, so it passes through untouched:

| principal | function | tenant id |
|---|---|---|
| `u-eng@example.com` (a group) | `tenant_id_for_group` | `u-eng` |
| `eng@example.com` (a user) | `tenant_id_for_user` | `u-eng` |

Same GSA, same secret prefix, same GCS prefix, same namespace — the cross-tenant
merge invariant 9 exists to prevent. `store.py:232` compounds it by deriving
`kind = "user" if tenant_id.startswith("u-") else "group"`, which assumes
exactly this cannot happen.

### Why it is not yet exploitable, and why that is temporary

`TENANT_GROUPS` was never set by terraform, so `resolve_tenant()` had no groups
to check and there were no registered groups at all. The collision needs one.

That changed on 2026-09-18: `TENANT_GROUPS` is now wired (see
`terraform/infra/locals.tf`). The collision is reachable from the moment a group
whose local part begins with `u-` is registered. No such group exists today, and
nothing stops one being added — `u-` reads naturally as an abbreviation
(`u-eng` for "unified eng"), which is what makes this a trap rather than a
curiosity.

### The requested change

In `_slug`, force the digest branch when a **group** slug would begin with `u-`,
reserving that namespace for personal tenants. Equivalently: give group slugs
their own required prefix. Either makes the docstring's claim true.

### What it would cost — the reason this is a request and not a patch

**Tenant ids are not just identifiers; they are the names of live
infrastructure.** A tenant id appears in a GSA name, a Kubernetes namespace, a
Secret Manager secret name, and a GCS prefix. Re-deriving them means the
platform stops finding the resources it already provisioned.

For the change as scoped, only groups starting with `u-` change id, and there
are none — so the migration is empty *today*. That is precisely why it is worth
doing now and expensive later: the same change made after such a group exists is
a data migration across four services.

Whoever applies it should also add the collision to `tests/unit` as a case, not
merely as a regression guard: `tenant_id_for_group("u-eng@example.com") !=
tenant_id_for_user("eng@example.com")` is the property, and it is currently false.

### The unfrozen half, which does not need this request

Separately from the collision, `_assert_principal_matches` — the only check that
would catch two identities sharing a tenant id — runs *only* inside
`ensure_tenant`, reached only via `SubmissionService.tenant_for()`. The write
paths call it. `list_tasks`, `get_task`, `cancel_task`, `list_events`,
`list_artifacts`, `list_workflows`, `get_workflow` and `cancel_workflow` do not;
they filter Firestore by the raw `tenant_id` string.

That is in `apps/swarm-api/`, which is not frozen, and it is the half that turns
a collision into a cross-tenant **read and cancel**. It should be fixed whether
or not this request is accepted, and the comment at `routes/tasks.py:3-6` —
"there is no request a caller can construct that reads another tenant's task" —
should not be restored until it is true.

---

## 2. `models.py`: `Attempt` records memory and disk, but not tokens or cost

**Status:** APPROVED by the platform owner and APPLIED, 2026-09-19.

The five fields below are now on `Attempt`, and
`agent_worker.control.record_spend` populates them from the summary
`_usage_summary` extracts before truncation. A field the runner did not report
is omitted rather than written as zero -- `None` means "not reported" and `0`
means "cost nothing", and a mock agent is genuinely the second while an
unparseable result is the first.

The original request follows, unchanged, as the record of what was asked for.

### What is missing

`Attempt` (`apps/common/swarm_common/models.py::Attempt`) carries
`peak_rss_bytes` and `peak_disk_bytes`. It carries no token count and no cost.

So the platform records precisely how much MEMORY an agent used and nothing at
all about what it spent -- on a platform whose entire cost is tokens.

### Why this surfaced now

A per-user activity view ("how many agents, tokens and jobs has each engineer
used") cannot be assembled. Two separate obstacles, one now fixed:

1. **The numbers were being thrown away.** `lifecycle.py` wrapped the runner
   result in `_truncate_json(..., 8000)`, which replaces an oversized result
   with a preview *string*. Long runs exceed 8000 characters, so the attempts
   that consumed the most tokens were exactly the ones whose token counts were
   discarded. Fixed on the unfrozen side: `_usage_summary()` now extracts
   `input_tokens`, `output_tokens`, cache counts, `thinking_tokens`,
   `total_cost_usd`, `num_turns` and the model list *before* truncation, into
   the free-form runner summary.

2. **There is still nowhere typed to put them.** The values now reach Firestore
   inside an untyped dict. Nothing can query them, no index can cover them, and
   every reader must know the shape by convention.

### The requested change

Add to `Attempt`:

```python
input_tokens: int | None = None
output_tokens: int | None = None
cache_read_input_tokens: int | None = None
cache_creation_input_tokens: int | None = None
cost_usd: float | None = None
```

Optional, defaulting to `None`, so every existing document remains valid and no
migration is required. `None` means "not reported", which is genuinely different
from zero -- a mock task reports nothing and did not cost nothing by accident.

### What it costs, and what it buys

**Costs:** five fields on the busiest document in the system, and a small write
on every attempt teardown.

**Buys:** cost and token usage become queryable per tenant, per profile and over
time. Today that question can only be answered by reading one GCS object per
attempt, which is correct and does not survive volume.

### Related work that is NOT part of this request

`submitted_by` is not filterable (`store.py:381-411` accepts `state`,
`workflow_id` and `runner_profile` only) and no Firestore index covers it
(`terraform/modules/firestore/indexes.tf`). Per-ENGINEER breakdown needs that
index regardless of this request. Both are unfrozen and can proceed
independently.

---

## 3. `models.py`: a typed `input_from` on `Task`

**Status:** open, recorded 2026-09-21. Raised in 5c99c79 ("input_from works;
work can be routed between agents for the first time") as one of "two
frozen-contract REQUESTS, raised rather than made" -- and then recorded in a
commit message, which is not a place anybody looks for an open decision.

**Update 2026-09-25 (#151):** failure (a) below, a plain `POST /v1/tasks`
carrying `metadata.input_from` past every DAG check, is closed on the unfrozen
side. The owner decided that the key is reserved, like `dispatch`.
`reject_reserved_metadata` now refuses it from callers with 422
`invalid_dispatch`, on an agent, a batch and a workflow's own `metadata`, whatever
its value, and creates nothing. Workflow expansion is the only writer, and a
step's own `input_from` is the only declaration a caller can make, so the
`validate_dag` filename checks #64 added (PR #65) see every declaration that
reaches the worker. `metadata.expected_outputs`, the other key workflow
expansion writes (#149), is reserved by the same function. The request for a
typed field is still open. It never depended on (a) alone: the five spellings
below remain, and the worker still re-validates a free-form dict.

### What is asked for

A field on `Task`, mirroring the one `WorkflowStep` already has:

```python
#: upstream TASK id -> artifact filename staged into this task's workspace.
input_from: dict[str, str] = field(default_factory=dict)
```

`WorkflowStep.input_from` is already typed (`models.py:333`) and is keyed by
upstream STEP id. `Task` has no equivalent, so the same declaration -- rewritten
to task ids, which is the form the worker can actually resolve -- travels in
`task.metadata`.

### What is restated today, and where

One idea, five spellings:

| where | type | keyed by |
|---|---|---|
| `swarm_common/models.py:333` `WorkflowStep.input_from` | `dict[str, str]` | step id |
| `swarm-api/schemas.py:80` `WorkflowStepCreate.input_from` | `dict[str, str]` | step id |
| `swarm-api/validation.py` `StepSpec.input_from` | `Mapping[str, str]` | step id (was a tuple of ids with no filenames until #64) |
| `task.metadata["input_from"]` | untyped | **task id** |
| `swarm-ui/src/types.ts:1227` `WorkflowStep.input_from` | `string \| null` | -- |

**Written by exactly one place.** `swarm-api/service.py:389-392`, inside
`submit_workflow`, translating step ids to the task ids it has just minted:

```python
if source.input_from:
    task.metadata[INPUT_FROM_METADATA_KEY] = {
        step_task_id[src]: filename for src, filename in source.input_from.items()
    }
```

**Read by exactly one place.** `agent-worker/agent_worker/inputs.py:56`
(`METADATA_KEY = "input_from"`) and `declared_inputs()` at `:109-154`, called
from `apps/agent-worker/agent_worker/lifecycle.py::Worker._stage_declared_inputs`. `declared_inputs` then
re-validates every key and every value at runtime -- the key is a non-empty
string, the value is a non-empty string, no two entries share a destination --
because the contract types `metadata` as `dict[str, Any]` and says nothing about
what is inside it. That defensiveness is correct and would still be wanted; the
request is about the fifth row of that table. It was also about the submission
path that was never validated at all, a caller's own `metadata.input_from`, and
that half is closed (#151, below).

`StepSpec.input_from` was a *tuple of ids* because the dependency rule needs to
know only which step a file comes from. That shape is also why the API could not
see two parents staging one filename, a mistake that surfaced only after both
parents had run. Since #64 it carries the filenames too, and `validate_dag`
refuses that collision and any absolute or traversing filename at submission.

### Why: the failure it prevents

**(a) The DAG rule is enforced on one submission path and not the other.**
`validation.py:373` states the rule -- "every `input_from` source is also an
upstream dependency" -- and `:412-425` enforces it, so a step cannot stage an
artifact from a step that may never have run. That check lives in the workflow
path. Meanwhile `reject_reserved_metadata` (`validation.py:242-255`) reserves
`dispatch` and nothing else, and
`tests/unit/control_plane/test_dispatch_strategy.py::test_reject_reserved_metadata_allows_everything_else` pins that
`input_from` is deliberately allowed through:

```python
reject_reserved_metadata({"unit": "payments", "input_from": {"x": "y"}})
```

`_build_task` then copies caller metadata verbatim (`service.py:205`,
`metadata = dict(spec.metadata)`). So a plain `POST /v1/tasks` carrying
`metadata.input_from` is stored as written and honoured by the worker, having
passed none of the DAG checks. A workflow's own `metadata.input_from` took the
same route onto every step that declared no `input_from` of its own, which
always included the root steps. #64 (PR #65) briefly checked that value's
filenames at submission (`validate_workflow_input_from_metadata`), but the task
ids it named still carried no dependency edge. That check was removed when #151
reserved the key, below: there is no valid workflow-level value left to check.

Be precise about what that is and is not. It is **not** a tenant escape:
`inputs.fetch_upstream_task` (`inputs.py:226-254`) refuses an agent belonging to
another tenant, and `inputs.artifact_key` (`:327-352`) builds the object key
from *this* attempt's tenant, so a cross-tenant artifact is unreachable by
construction. It is an **ordering** bypass inside one tenant: the caller names
any of their own agents, with no dependency edge and no guarantee the upstream
ran. A typed field gives the API one field to validate on every path, instead of
one path validating a key the other path waves through.

**Closed on the unfrozen side, 2026-09-25 (#151).** The paragraphs above describe
the code before that date. The owner chose to refuse the key from callers rather
than validate it for a standalone agent. `reject_reserved_metadata` now reserves
`input_from` alongside `dispatch`, so a `POST /v1/tasks` (or a batch) carrying it
gets 422 `invalid_dispatch` and nothing is created. A workflow whose own
`metadata` carries it is refused the same way, whatever the value, which also
closes the path by which that value reached every root step verbatim. That
refusal runs before `validate_dag`, so such a workflow answers
`invalid_dispatch`, never `invalid_dag`, even when its declaration would also
have broken the #64 filename rules. #65's workflow-level filename check and the
tests that pinned a well-formed, `{}` or `null` workflow-level value as
accepted went with it; its step-level checks are unchanged. The worker's
checks stay as defence in depth, not as the only guard on any submission path.
The test at
`test_dispatch_strategy.py` quoted above no longer pins `input_from` as allowed.
`tests/unit/control_plane/test_input_from_is_reserved.py` holds the refusal, the
empty store after it, and workflow expansion still writing the key.

**(b) The UI's copy is already wrong.** `codec.workflow_to_api:486` serves
`"input_from": s.input_from`, a `dict[str, str]`. `swarm-ui/src/types.ts:1227`
declares `input_from: string | null`, and the fixture at `api.ts:1185-1191`
builds it that way too, so the mock agrees with the wrong shape. Nothing renders
the field today, which is the only reason this has cost nothing -- a seam
declared at both ends with nothing in the middle.

### What it would break if accepted

* **No document migration.** `Task.to_firestore()` is `asdict(self)`, so new
  documents gain one key; `codec.task_from_dict` builds from named fields, so an
  older reader ignores it and an older document takes the default.
* **The rollout is the risky part, and it has a specific failure.** A worker
  image older than the API reads `metadata` only. If `service.py` stops writing
  `metadata["input_from"]` the moment the typed field exists, that worker sees
  no declaration, stages nothing, and runs the agent on a prompt written as
  though the file is there -- the exact silent-wrong-answer outcome
  `inputs.py`'s module docstring says must never happen. So: add the field, have
  the worker prefer it and fall back to metadata, ship both for a deploy window,
  and only then stop writing metadata.
* **`check-contract-parity.sh` gains nothing automatically.** It compares shell
  and jq to Python, and there is no shell restatement of this field.

### If it is declined

The worker keeps re-validating a free-form dict, which is defensible and is done
well today. Two things then need doing on the **unfrozen** side regardless, and
neither needs this request:

* `reject_reserved_metadata` should reserve `input_from`, or `_build_task`
  should validate it for a standalone agent. **Done** the first way, by owner
  decision on #151 (2026-09-25): the key is refused from callers on every path
  that creates an agent, and only workflow expansion writes it.
* `swarm-ui/src/types.ts` should declare `input_from` as
  `Record<string, string>`. **Done**, along with the fixture in `api.ts` that
  built it as a bare string, and `check-contract-parity.sh` section 5 now
  asserts the declaration against the frozen annotation -- so "nothing will tell
  anybody", which is what this bullet used to end on, is no longer true. Neither
  change touches the frozen module, which is why neither needed this request.

---

## 4. A typed artifact-manifest entry

**Status:** open, recorded 2026-09-21. Raised alongside #3 in 5c99c79: "a typed
artifact-manifest entry so check-contract-parity.sh can hold the producer and
the consumer together. Today they agree by convention, which is why the consumer
re-validates every key at runtime."

### The shape, and its single writer

`apps/agent-worker/agent_worker/lifecycle.py::Worker._upload_outputs`:

```python
artifacts.append({"name": rel, "bytes": size, "uri": self.store.uri(key)})
```

placed into `result_summary` at `:2126-2134` beside `artifact_bytes` (int) and,
when the attempt hit its cap, `artifacts_skipped` (`list[str]`, truncated to 50).
`name` is a path relative to the artifacts directory, so it may contain
separators; `bytes` is `path.stat().st_size`; `uri` is whatever the store
rendered (`gs://...` in production, `file://...` locally).

### Every place that parses it

1. **`agent-worker/agent_worker/inputs.py:280-324`** -- `artifact_reference`.
   Reads `name`, `uri`, `bytes`. This is the one that resolves a declared input,
   and the one with the defect below.
2. **`swarm-mcp/swarm_mcp/patches.py:92-95`** -- `patch_uri`. Reads `name` and
   `uri` only, matching `result_summary.git.patch` against the manifest so a
   patch that failed to upload yields `None` rather than a link to nothing.
3. **`swarm-ui/src/AgentDetail.tsx:1439-1441`** -- shape-checks
   `summary.artifacts` is an array before casting, then `:1550-1556` renders
   `name`, `bytes` and `uri`, and `:1760` matches `git.patch` against `name`
   again.
4. **`swarm-ui/src/types.ts:345-349`** -- `ArtifactRef { name: string; bytes:
   number; uri: string }`. The cast at `AgentDetail.tsx:1440` checks "is it an
   array" and nothing further, so `bytes` is declared non-optional and is not.

A fifth thing shares the name and not the shape:
`agent-worker/agent_worker/runners/base.py:172` writes
`"artifacts": [<filename>, ...]` -- a list of **strings** -- into the runner's
`result.json`. It is not copied into `result_summary` (`apps/agent-worker/agent_worker/lifecycle.py::Worker._add_runner_block`
takes `status`, `summary`, `output` and `metrics` only), so the two do not
collide today. They are two identically-named fields with incompatible shapes in
one codebase, which is the ambiguity a named type removes.

### The defect nearby, and whether typing would have prevented it

`inputs.py:298-307`:

```python
size = entry.get("bytes")
return ArtifactReference(
    key=artifact_key(...),
    uri=uri,
    size_bytes=int(size) if isinstance(size, int) and not isinstance(size, bool) else 0,
)
```

Measured directly against that function (`uv run python`, five hand-built
manifest entries, nothing in the repository changed):

```
  bytes: int     -> size_bytes = 900000000
  bytes absent   -> size_bytes = 0
  bytes: str     -> size_bytes = 0
  bytes: float   -> size_bytes = 0
  bytes: True    -> size_bytes = 0
```

`stage_inputs:432-437` then sums `reference.size_bytes` and refuses the attempt
if the total exceeds `max_total_bytes`; the actual download at `:445` returns the
real size and is never compared to anything. So an entry whose `bytes` is
absent, a string, a float or a bool contributes **zero** to the cap and is then
fetched in full into a memory-backed tmpfs workspace. The comment at `:427-431`
says exactly what that cap is for: "an OOM kill several minutes into an agent
run is a far worse diagnosis than a refusal at setup."

**Would typing have prevented it? Half of it.**

* The half a type fixes: with `bytes` a required int at the boundary, the `0`
  has nowhere to come from. "Not reported" becomes a parse failure naming the
  upstream agent and the file, instead of a silent zero -- and this module has
  already decided, in its own docstring, that a declared input which cannot be
  resolved fails the attempt rather than degrading quietly.
* The half it does not: the cap is computed from **declared** sizes and never
  re-checked against what arrived. A manifest that under-reports -- a truncated
  upload, a hand-edited document, a future writer that rounds -- walks through
  the same door with a perfect type in place. The fix that holds is a running
  total during pass 4 that aborts when the bytes actually downloaded exceed
  `max_total_bytes`.

That second half is **unfrozen**, lives in `inputs.py`, and should be fixed
whether or not this request is accepted. This entry must not become the reason
it waits.

### What it would break if accepted

* **Scope matters.** `Task.result_summary` and `Attempt.result_summary` are
  `dict[str, Any] | None`. Typing the whole summary would mean enumerating and
  freezing every key `lifecycle.py` writes (`git`, `logs`, `runner`,
  `checkpoint`, `restored_from`, `inputs`, `artifact_bytes`,
  `artifacts_skipped`) -- a far larger change than this request. The cheap,
  useful version is a dataclass for the **entry** alone, which nothing is forced
  to store and which every parser can construct from.
* **Strict parsing of history is plausible but unproven.** There has only ever
  been one writer and its shape has not changed, so every existing entry should
  carry all three keys. That has **not** been checked against production data,
  and it must be before anything parses strictly -- otherwise this request turns
  an under-counted cap into a failed attempt on an old artifact.
* `check-contract-parity.sh` could then hold the TypeScript `ArtifactRef` to it,
  except that it does not read TypeScript at all. See the closing section.

### If it is declined

`artifact_reference` keeps re-validating every key, which is the right thing to
do with an untyped dict, and `patches.py` and `AgentDetail.tsx` keep their own
defensive checks. The size-cap bypass above is fixed anyway, on the unfrozen
side. The cost is that four readers stay in agreement by convention, and the
fifth -- `ArtifactRef`, which claims `bytes: number` -- already is not.

---

## 5. `gke_api_host` in a shared module (proposed: `swarm_common/kube.py`)

**Status:** open, recorded 2026-09-21. c43844a ends its GKE-host section with
"the contract request to give it one owner is recorded rather than made." It was
not recorded anywhere a person would find it. This entry is that record.

### What is duplicated

`scheduler/dispatch.py:507-549` and `reconciler/backends.py:459-500`. Strip the
docstrings and comments and the two are byte-identical -- thirteen lines:

```python
raw = (endpoint or "").strip()
if not raw:
    return ""
scheme, separator, rest = raw.partition("://")
if not separator:
    rest = raw
elif scheme.lower() != "https":
    raise ValueError(
        "GKE_ENDPOINT must be a bare host or an https:// URL; "
        f"{scheme}:// would send the bearer token in clear text"
    )
rest = rest.rstrip("/")
if not rest:
    raise ValueError(f"GKE_ENDPOINT is not a usable host: {endpoint!r}")
return f"https://{rest}"
```

Verified by diffing the two comment-stripped bodies: no output. The docstrings
differ deliberately -- each names the other copy and states its *own* caller's
failure mode, which is the more useful half of what is written there.

### The claim behind the request, verified

The claim is that `apps/common` is the only directory both images carry. It
holds. Those are the only two `COPY apps/...` lines in either file:

| image | copies |
|---|---|
| `images/swarm-scheduler/Dockerfile:59-60` | `apps/common/`, `apps/scheduler/` |
| `images/swarm-reconciler/Dockerfile:61-62` | `apps/common/`, `apps/reconciler/` |

Each then builds exactly two wheels from those two directories
(`:65-66` and `:67-68` respectively) and installs its own service. So neither
image contains the other's package, neither can import the other, and
`swarm_common` is the only place a single copy could live inside both.

`apps/common/pyproject.toml` declares `dependencies = []` and
`packages = ["swarm_common"]`, so a stdlib-only module added there ships in both
images and adds no dependency to either. This function needs nothing but
`str.partition`.

### Why: the failure it prevents

The two copies **did** disagree, and the direction of the disagreement is the
point. The reconciler added the scheme; the scheduler did not. So every GKE
dispatch failed on a URL urllib3 could not route -- and it failed *after*
admission had already taken the lease. Invariant 3 counts concurrency from
`LEASED`, so the slot is held by an agent committed to a backend that will never
start it. The reconciler, which is what would notice, was reading the same
cluster through the copy that worked.

What holds them together today is
`tests/unit/control_plane/test_gke_client_host.py::test_the_two_copies_agree_on_every_input`:

```python
assert scheduler_gke_api_host(endpoint) == reconciler_gke_api_host(endpoint)
```

That is a real guard and it does work. But it works only because the *test*
process can import both services, which neither *image* can -- the file says so
itself at lines 10-14. One test, importing two packages that never run in the
same process, is the entire structural connection between two copies of a
security-relevant string function. Delete the test and the copies are free.

### What it would break if accepted

* **It is an addition, not a change.** No existing type moves, no stored
  document changes, no wire format changes. That makes it the cheapest request
  in this file and the one with the least to argue about.
* Both call sites become imports. The two docstrings must move to the call
  sites rather than be lost with the duplicate: "the dispatch fails after the
  lease is taken" and "the reconciler goes blind on the GKE backend" are
  different facts about different callers, and both are worth keeping.
* The equality test should be **replaced, not deleted**: one test of the shared
  function, plus whatever already asserts that each service configures its
  client from it.
* CLAUDE.md rule 1 says never edit the files in that directory. Adding a file is
  not editing one, but it is still a change to the frozen package, which is
  exactly why this is a request and not a commit.

### If it is declined

Nothing breaks today, and the copies agree. The cost is that the only thing
standing between the next editor of either copy and a repeat of that outage is
one test file in a directory neither service builds -- and that the reasoning
for the duplication now lives in two docstrings that have to be kept accurate by
hand.

---

## 6. A typed dispatch block

**Status:** open, recorded 2026-09-21, from the defect fixed in 1cfdf57
("`integrate` promised ONE pull request and opened one per step").

> **Half of this was done on 2026-09-22, as this entry recommended.** "If it is
> declined" below says the carrier vocabulary has to be reconciled regardless
> because both sides are unfrozen. It has been: `_dispatch_carrier` now accepts
> `("checkpoints", "branches")` and defaults to `checkpoints`, and
> `tests/unit/worker/test_integrate_strategy.py` no longer pins `"patches"`.
> `tests/unit/worker/test_dispatch_contract_parity.py` imports swarm-api's own
> `DISPATCH_CARRIERS`, `DISPATCH_STRATEGIES` and `DispatchOptions.to_metadata()`
> and asserts the worker's parsers agree with them — which is the "something for
> a parity check to assert" this entry asked for, obtained without unfreezing
> anything. **The request itself stays open**: the vocabularies are still typed
> out twice, still in components that cannot import each other, and only a test
> now stops them drifting. That test lives in the unit suite, so it protects the
> repository and not a worker image built from an older commit.

### What is restated, and where

**swarm-api writes it.** `validation.py:186-229` resolves `DispatchOptions`
(`strategy`, `carrier`, `role`, `integrates`) and `to_metadata()` at `:222-229`
renders it into `task.metadata["dispatch"]` (`DISPATCH_METADATA_KEY`, `:175`).
The accepted vocabularies are `DISPATCH_STRATEGIES` (`:155`) and
`DISPATCH_CARRIERS` (`:161`).

**Two things read it back.**

* `codec.dispatch_of` (`:101-120`) -- for API callers. Fills absent fields with
  the API's defaults so an agent predating the feature reads as
  `collect`/`checkpoints` rather than null, and `codec.workflow_dispatch`
  (`:122-149`) rolls that up to the workflow.
* `apps/agent-worker/agent_worker/lifecycle.py::Worker._dispatch_block` and the four beside it -- for the worker. Five accessors:
  `_dispatch_block`, `_dispatch_strategy`, `_dispatch_role`,
  `_dispatch_integrates`, `_dispatch_carrier`.

So three parsers, not two. `dispatch_of` and the worker deliberately disagree
about defaults, and that is correct -- one reports what a caller should *see*,
the other picks what is safe to *do*. What is not correct is the vocabulary.

### The drift that is already there

Measured by importing both sides:

```
swarm-api writes carrier in ('checkpoints', 'branches'), default 'checkpoints'

worker _dispatch_carrier body:
    value = self._dispatch_block().get("carrier")
    if not isinstance(value, str):
        return "patches"
    value = value.strip().lower()
    return value if value in ("patches", "branches") else "patches"
```

`checkpoints` -- the value on every agent swarm-api has ever written that did not
ask for branches -- is not in the worker's accepted set, so `_dispatch_carrier()`
returns `"patches"` for it. And `"patches"` is a value swarm-api cannot produce:
`_accepted_value` (`validation.py:233-240`) refuses it at submission with the
accepted list in the error. Two sides of one field, and neither can say a word
the other accepts as the default.

**Today this costs nothing, and the reason matters.** `_dispatch_carrier` has no
caller in production code. Its only references are its own definition and
`tests/unit/worker/test_integrate_strategy.py::test_carrier_defaults_to_checkpoints_and_reads_branches`, which asserts the default
is `"patches"` -- pinning the disagreement into the suite rather than catching
it. 1cfdf57 says as much about the other end: "`carrier: "branches"` forced
`needs_repository` at submission and then changed no behaviour at all."

That is the shape of the two defects this repository fixed today: a seam built
at both ends with nothing in the middle, and a test that passes because it
agrees with the wrong side. The first change that wires the carrier up gets a
worker that reads every default dispatch as a carrier the API cannot name.

### Why: what a shared type prevents

Not the four-field read -- the worker reads all four now. It prevents the
vocabularies drifting apart again, which they already have. Concretely, a
`DispatchBlock` in the frozen package, owning the accepted values, would mean:

* one list of strategies and one list of carriers, imported by both sides
  instead of typed out twice;
* something for `check-contract-parity.sh` to assert. It covers nothing about
  dispatch today: its four checks are `SlotPool.effective_limit`,
  `Tenant.secret_name()`, the `TaskState` arrays, and the tenant-id budget;
* an explicit home for the worker's tolerance rules, which are **correct and
  must survive**. An unknown strategy reading as `collect` and an unknown role
  as `contributor` is the newer-control-plane/older-worker rule: in that
  disagreement the safe reading is the one that pushes nothing and opens
  nothing. That reasoning is currently in two docstrings and nowhere else.

### What it would break if accepted

* **Scope decides the risk.** A `DispatchBlock` dataclass plus a
  `from_metadata()` classmethod changes no stored document -- the block stays
  where it is, inside `metadata`. A typed *field* on `Task` would, and it
  carries the same rollout trap as #3 in the opposite direction: a worker older
  than the API would see no dispatch block, every `integrate` step would read as
  `collect`, and the workflow would run to completion publishing nothing. That
  is the failure 1cfdf57 fixed, arriving from the other side.
* **The tolerance rules must be part of the type's written contract.** If the
  next implementer makes `from_metadata()` strict, a version skew stops being a
  conservative downgrade and becomes a failed attempt.
* `tests/unit/worker/test_integrate_strategy.py::test_carrier_defaults_to_checkpoints_and_reads_branches` has to change. It
  currently asserts the wrong vocabulary.

### If it is declined

The carrier vocabulary still has to be reconciled. Both sides are **unfrozen**,
so that can be done today and should be: a dead accessor carrying constants the
writer cannot produce, with a test holding it in place, is a trap set for
whoever wires it up. Everything else keeps working by convention, re-parsed in
three places, with the frozen contract silent about a four-field block that
decides whether a workflow opens one pull request or five.

### A fifth field, 2026-09-28 (#263): `continues`

The CI fixer needed a step to push to an EXISTING swarm branch rather than its
own, and it was added the way this entry's "if it is declined" path allows:
inside the block, with no change to `apps/common/swarm_common/`. So the block
now has five fields, and this request covers all five.

* **What it is.** A task id. swarm-api writes it only on the one step of a
  `direct-pr` workflow submitted with `continues_task`, after checking the agent
  is the caller's own, was itself `direct-pr`, and is in the same repository,
  and after resolving a chain of continuations to its root
  (`swarm_api/continuation.py`). The worker derives `<prefix><id>` from it,
  clones that branch and pushes onto it (`agent_worker/continuation.py`).
* **Why not a typed field.** The same reason as the other four, and the same
  rollout trap in its mildest form: a worker older than the API ignores the
  key, pushes `swarm/<its own id>` and opens a second pull request. That is
  wrong but not unsafe -- no branch is overwritten, and `push_branch` never
  forces either way.
* **The parity it relies on** is a test, as for the others:
  `tests/unit/worker/test_continue_swarm_branch.py` builds the block with
  swarm-api's own `DispatchOptions(continues=new_id("task")).to_metadata()`
  and asserts the worker derives the branch from it, and that the worker's
  task-id pattern accepts every id `new_id("task")` mints. The pattern is
  restated in both components because neither can import the other;
  `swarm_common.models` exporting it beside `new_id` is the frozen-contract
  change that would remove the copy. **Requested, not made.**

`codec.dispatch_of` does not serve `continues`: the workflow create response
echoes it as `dispatch.continues_task`, and adding it to every agent's
`dispatch` would change a response shape several clients hold exactly.

---

## 7. The workflow rollup has no shared home, so only one service can own it

**Status:** open, found 2026-09-22 while making `Workflow.state` advance at all. The periodic caller this request was one way to get is built without it (D17, 2026-10-02): a Cloud Scheduler job per registered tenant calls `POST /v1/admin/workflows/rollup` as the dedicated `swarm-rollup-sweeper` account, which holds `roles/run.invoker` on swarm-api and nothing else and which swarm-api admits to that route alone (`terraform/modules/scheduler/jobs.tf` `workflow_rollup`, `terraform/infra/main.tf` `rollup_sweeper_invokes_api`, `swarm_api.auth.ROLLUP_SWEEPER_ROUTES`). What remains open is the shared home for the derivation.

### The problem

Nothing ever wrote `Workflow.state` after submission. It is now derived from the
step agents and written back, and the derivation lives in
`apps/swarm-api/swarm_api/rollup.py`. It lives there because that is the only
place it CAN live and still be used by more than one service — and it is used by
exactly one.

The scheduler would have been the better home for the write. It already runs on a
guaranteed one-minute clock (`terraform/modules/scheduler/jobs.tf` (`resource "google_cloud_scheduler_job" "safety_tick"`), the safety
tick into the wake topic) and it already owns "state advances over time". It
cannot have the derivation, because:

```
images/swarm-scheduler/Dockerfile:59-60   COPY apps/common/  apps/scheduler/
images/swarm-api/Dockerfile:59-60         COPY apps/common/  apps/swarm-api/
```

Each image installs the frozen contract plus its own package. `swarm-scheduler`
cannot `import swarm_api` and `swarm-api` cannot `import scheduler`; a
cross-package import would be an `ImportError` in production and nowhere else,
which is the failure `apps/swarm-api/pyproject.toml` already carries a comment
about for `google-cloud-storage`.

So the choice was: one implementation in the API, or two implementations with a
cron. Two was refused, because the second copy would be compared against the
first by the drift check this feature also ships — and the check would then be
reporting the two copies drifting rather than the data drifting, which is worse
than no check.

### What the change would be

A new module in the frozen package — `swarm_common/rollup.py` — holding the pure
derivation only:

* the terminal-severity ranking (`DEAD_LETTERED > FAILED > CANCELLED > SUCCEEDED`)
* the pending precedence (`READY > PARKED > QUEUED > SUBMITTED`)
* `derive(readings) -> WorkflowRollup`
* the `UNKNOWN` sentinel and the rule that an incomplete read produces it

It defines no new dataclass field and changes no existing type. It is a pure
function over `TaskState` and a small result object, so it adds no dependency to
any image that does not already have `swarm_common`.

### What it buys

The scheduler's `_promote_dependencies` sweep (`scheduler/loop.py:472`) could then
do the write on the existing one-minute tick, and the API would derive for
display from the same function. One rule, two callers, nothing to drift.

### What breaks if it is made

Nothing imports it yet, so nothing breaks. The API's `swarm_api/rollup.py` would
keep the impure half — the store reads, the write-back, the metric — and import
the pure half instead of defining it.

### What is left to live with if it is declined

What ships today, which is not broken but is narrower than it should be:

* the write-back fires on every workflow READ, so the stored copy converges for
  any workflow anybody looks at — which is most of them, because the web UI polls
  `GET /v1/workflows?limit=100`;
* `POST /v1/admin/workflows/rollup` converges the rest on demand;
* **there is no periodic caller for that route.** A workflow nobody lists and
  nobody sweeps keeps a stale STORED state indefinitely. Every READER still sees
  the truth, because every read derives; it is the *queryable* copy that lags,
  which is the half the write exists for. Closing that needs either this request
  or a Cloud Scheduler job against the admin route, which is Track C.

---

## 8. `Workflow.state` reuses `TaskState`; a workflow needs its own vocabulary

**Status:** open, found 2026-09-22.

### The claim that is imprecise

`apps/common/swarm_common/models.py::Workflow.state`:

```python
@dataclass
class Workflow:
    ...
    state: TaskState
```

A workflow is not an agent, and the task vocabulary is the one available to
describe it. Most of it transfers cleanly — SUCCEEDED, FAILED, CANCELLED,
DEAD_LETTERED, RUNNING, READY, PARKED and QUEUED all mean at the workflow level
what they mean at the agent level. Two things do not:

* **There is no word for a partial failure.** A workflow whose steps are
  SUCCEEDED, FAILED and CANCELLED derives FAILED, and "FAILED" is not wrong — but
  it cannot distinguish "every step failed" from "four of five succeeded".
  `rollup.counts` carries that today, and a caller filtering on state alone
  cannot see it.
* **`LEASED`, `DISPATCHED` and `STARTING` are meaningless for a workflow.** They
  describe one agent's relationship to one lease. The derivation collapses all
  four capacity-holding states into RUNNING for exactly that reason, so three of
  the twelve values are unreachable for a workflow and nothing says so.

### What the change would be

A `WorkflowState` enum in `swarm_common/states.py` and `Workflow.state` retyped
to it:

```python
class WorkflowState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PARKED = "PARKED"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"   # some steps succeeded, some did not
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
```

### Why it was NOT done as part of the work that found it

`Workflow` is in the frozen contract, and the existing vocabulary can express
everything the platform currently needs to say. Deriving into `TaskState` needed
no contract change and is what shipped. This is the request, not a plan.

### What breaks if it is made

More than request #7, and it should be costed before anyone agrees:

* `codec.workflow_from_dict` decodes the stored field with `TaskState(...)`.
  Every workflow document written before the change holds `"QUEUED"`, which is
  in both enums, so the read survives — but any document holding `LEASED`,
  `DISPATCHED`, `STARTING` or `DEAD_LETTERED` would stop decoding. Nothing writes
  those today; nothing guarantees nothing ever did.
* `apps/swarm-ui/src/types.ts` widens `Workflow.state` to `string` already, so
  the UI tolerates a new value without a typecheck — which is the same property
  that let it tolerate a wrong one. `check-contract-parity.sh` does not cover
  TypeScript (see the surfaces table at the end of this file).
* `PARTIALLY_FAILED` is a genuinely new concept and needs a decision about
  whether it is terminal for the purposes of "list my failed workflows".

### What is left to live with if it is declined

The derivation stays inside `TaskState`, `rollup.counts` carries the partial
picture, and a caller wanting "mostly succeeded" reads the counts rather than the
state. That is what ships today and it is honest; it is just less queryable than
a named state would be.
---

## Why these requests keep arising

Four of the requests above -- #3, #4, #6 and #7 -- are one situation: a value
with a single definition and several readers, in components that cannot import
each other. #5 is the same situation with a function instead of a value, and #7
is the same situation with a RULE instead of a value -- which is the sharpest
form of it, because the thing that would notice the two copies drifting is the
drift check that one of the copies exists to feed.

---

## 10. `profiles.py`: `requires_preview_disk` names a feature this platform does not use

**Status: open.** Raised while sweeping the documentation for the retracted
workspace-storage story, because the docs were not the only place it survived.

### What is there

```python
#: Cloud Run ephemeral disk is a Preview feature and, per Google's docs,
#: disables live migration -- which is why mandatory checkpointing exists.
requires_preview_disk: bool = False
```

`ResourceClass.requires_preview_disk`, `swarm_common/profiles.py`.

### Why it is a defect and not merely tidy-up

Three separate things, in increasing order of cost:

1. **Nothing sets it and nothing reads it.**
   `grep -rn requires_preview_disk` over the whole repository returns exactly
   one line: the declaration. No resource class passes it, no dispatcher
   branches on it, no test asserts it. It is a field whose every value is the
   default.
2. **Its docstring states a retracted premise, inside the frozen module.**
   The correction at the end of `CONTRACT.md` and of `CLAUDE.md` says this
   platform does NOT use Cloud Run ephemeral disk -- the Terraform google
   provider cannot express it, `empty_dir.medium` accepts only `"MEMORY"` -- so
   workspaces are memory-backed tmpfs on the fully-GA path, **with** live
   migration. The comment above therefore contradicts the contract it lives in,
   and it is the copy a reader is most likely to trust, because it sits beside
   the numbers.
3. **The clause "which is why mandatory checkpointing exists" is the dangerous
   half.** If the stated reason for invariant 8 is a feature the platform does
   not use, the invariant looks obsolete -- and the one conclusion nobody may
   draw is that the checkpoint interval can be relaxed. The real reasons are a
   quota park-and-exit, a cancellation, a reconciler reclaim of a stale
   generation and an ordinary crash; live migration covers none of them.
   `docs/checkpointing.md` now says so at the top of the file, but this comment
   still says the other thing.

### The requested change

Either:

* **remove the field** and its comment, since nothing sets or reads it; or
* **keep the field** (as a placeholder for the day the Preview feature becomes
  expressible) and rewrite the comment to say that it is unused today, that
  nothing may branch on it until a provider can express the feature, and that
  invariant 8 does not depend on it.

Removing it is cleaner. Keeping it is defensible only if somebody intends to
revisit the feature, which is a product decision rather than an engineering one.

### What it would break if accepted

Nothing detectable. There is no reader, so no decoder, no manifest, no test and
no Terraform mirror changes. `make test` covers the catalogue through
`tests/terraform/catalogue_mirror/`, which mirrors name, cpu, memory, disk,
units and the runner fields -- not this flag.

### If it is declined

The comment should still be corrected even if the field stays, and that is the
part that actually matters: a retracted premise stated inside the frozen module,
next to the reason for an invariant, is how the invariant gets argued away in six
months. Every doc that told the old story has now been corrected in place --
`README.md`, `docs/architecture.md`, `docs/checkpointing.md`,
`docs/cost-control.md`, `docs/concurrency.md`, `docs/execution-backends.md` --
so this comment and one other line are what is left.

**The other line is `CONTRACT.md` invariant 8 itself**, which still reads
"Mandatory periodic checkpointing. Cloud Run ephemeral disk is Preview and
disables live migration, so checkpoints are what make interruption survivable."
The correction is 50 lines further down the same file, which is better than
nothing and worse than the invariant being true where it is stated. Restating
invariant 8 on its real grounds -- park-and-exit, cancellation, reclaim, crash --
is a second request, made here rather than taken, because `CONTRACT.md` is the
document these requests are addressed to.

---

## Why these requests keep arising

Three of the requests above -- #3, #4 and #6 -- are one situation: a value with a
single definition and several readers, in components that cannot import each
other. #5 is the same situation with a function instead of a value. (#10 is not:
it is a stale comment on a field nothing uses, and it is here only because the
field is inside the frozen module.)

`CONTRACT.md` permits a restatement where it is unavoidable, and
`docs/architecture.md` pays for the one it permits with a test. There are three
restatement surfaces in this repository. Two are paid for.

| surface | what holds it to the Python | run by |
|---|---|---|
| shell / jq | `scripts/lib/check-contract-parity.sh` -- four checks | `make test` (Makefile:164), and the `shell` job in `.github/workflows/application.yml` |
| Terraform's runner catalogue | `tests/terraform/catalogue.tftest.hcl` with `tests/terraform/catalogue_mirror/` | `make tf-test` |
| TypeScript | `scripts/lib/check-contract-parity.sh` section 5 -- nine checks | the same |

Verified rather than assumed, because an earlier report asserted it. **The
TypeScript row was `nothing` when that was written, and the two drifts the table
below recorded are why.** Both have since been fixed and the surface is now
covered; the paragraphs are kept because the reasoning is what justifies the
check rather than a second route.

* `grep -c 'tsc\|typescript\|swarm-ui' scripts/lib/check-contract-parity.sh
  Makefile` -> `0` and `0` **at the time**. The parity script read no `.ts` file;
  its four checks were `SlotPool.effective_limit`, `Tenant.secret_name()`, the
  four `TaskState` arrays, and the tenant-id length budget.
* `grep -rc 'tsc\|npm \|typecheck\|swarm-ui' .github/workflows/` -> `0` in all
  four workflow files. **Still true**, and it does not need to change: section 5
  is pure Python and `re`, reads `types.ts` as text, and needs no node toolchain
  on the machine or in CI.
* `apps/swarm-ui/package.json` does define `"typecheck": "tsc -b --noEmit"`.
  Nothing in this repository runs it.

And running it would not help. `tsc` checks `types.ts` against itself; it has no
way to know what `swarm_common` says. Only a comparison, or a route, can. Section
5 is the comparison: it reads the literals out of `types.ts` with regular
expressions and asserts them against the imported frozen modules, and it
**refuses** rather than skips when a literal is not where it expects -- because a
parity check that passes because it could not find what it compares reports an
agreement it never established.

What that cost, measured before the check existed:

| in `apps/swarm-ui/src/types.ts` | frozen source | state |
|---|---|---|
| `RESOURCE_UNITS`: standard 1, browser 2, large 4 | `profiles.RESOURCE_CLASSES` units 1 / 2 / 4 | agreed. Hand-copied; **now asserted** |
| `TaskState` union, 12 values | `states.TaskState`, 12 values | agreed; **now asserted** |
| `ParkReason` union, 8 values | `states.ParkReason`, 8 values | agreed; **now asserted** |
| `CONCURRENCY_STATES`, `TERMINAL_STATES` | the frozen frozensets | agreed; **now asserted** |
| `REAL_STATES` + `NEVER_WRITTEN` | must partition `TaskState` | agreed; **now asserted** |
| `PoolKind`, `POOL_FAMILY_ORDER`, `poolKind`'s switch | the families `models.pool_names_for` emits | agreed; **now asserted** |
| `QuotaState.state`: `'HEALTHY' \| 'COOLDOWN' \| 'EXHAUSTED' \| 'DISABLED' \| string` | `models.ProviderState`: `AVAILABLE`, `THROTTLED`, `EXHAUSTED`, `COOLDOWN`, `UNKNOWN`, `DISABLED` | **was drifted; FIXED** -- the field now reuses `ProviderStateName`, which is asserted |
| `WorkflowStep.input_from`: `string \| null` | `codec.workflow_to_api` serves `dict[str, str]` | **was drifted; FIXED** to `Record<string, string>`, and asserted |
| `ResourceClassSpec` | served by `GET /v1/resource-classes` | **not copied, deliberately** |

The `QuotaState` row is worth spelling out, because it is the one that shows why
a typechecker would not have helped. There is no provider state called `HEALTHY`
anywhere in this platform -- the only other occurrence of the word in the
repository is an unrelated fixture name at `tests/unit/mcp/test_sc.py::HEALTHY`. Three
states that *do* exist (`AVAILABLE`, `THROTTLED`, `UNKNOWN`) were missing from
the union. And the trailing `| string` widens the whole thing back to `string`,
so even a typechecker that ran would have reported nothing: the union documented
a vocabulary rather than enforcing one, and the vocabulary it documented was
wrong. The `| string` is kept -- a value the server adds must still decode and
render as itself -- and the vocabulary half is now a named type with an assertion
behind it.

The last row is the remedy the repository found first and used best. The comment
on `ResourceClassSpec` gives this exact reasoning for **not** copying the
resource-class numbers:

> `check-contract-parity.sh` asserts that shell and jq restatements of the
> frozen catalogue still match the Python; it does not cover TypeScript, so a
> copy here would drift the first time a class is resized and nothing would
> notice.

So somebody had already worked this out once, wrote a route instead of a copy,
and the two drifted rows above are what happened in the places where that was
not done. **The second clause of that comment is no longer true** -- the check
does cover TypeScript now -- but the decision it justified is still the better
one, and it stands: a route beats an asserted copy, because an asserted copy
still has to be edited in two places.

Which gives the shape of the answer to all of these:

* where every reader is Python, a shared type ends it -- requests #3, #4, #5, #6;
* where one reader is jq, Terraform or TypeScript, a parity check ends it, and
  all three exist;
* a route that serves the value is better than any of them where the value can
  be served, because nothing has to be kept in step at all. That is what
  `GET /v1/resource-classes` and `GET /v1/runtimes` are, and it is the remedy to
  reach for first.

**A parity check is not a substitute for a shared type.** It catches drift after
it is written, at `make test` rather than in review, and it cannot catch drift in
a value that is restated somewhere it does not read. The requests above are still
requests.

---

## 9. `models.py`: `Lease.dispatch_overdue` excluded the leases it names

**Status: ACCEPTED and applied 2026-09-22, by the owner's decision.**

### The claim that was false

```python
def dispatch_overdue(self, now=None) -> bool:
    """Admitted but never started. The reconciler reclaims these."""
    return self.state is TaskState.LEASED and (now or utcnow()) > self.dispatch_deadline
```

`mark_dispatched` (`apps/scheduler/scheduler/store.py`) writes `DISPATCHED` to
the lease the moment the backend *accepts* the create call — long before a
container runs. So `state is TaskState.LEASED` was False for essentially every
lease whose dispatch was in flight, which is exactly the population the method
names. Measured on `task_b5dc2568713a40158851` on 2026-09-22: still False 301
seconds after admission.

### What it cost

* `apps/swarm-api/swarm_api/routes/admin.py::list_leases` — the `overdue_only=1` filter
  is the query an operator runs during a capacity incident to find stuck
  dispatches. It returned an empty list at exactly the moment it was asked.
* `apps/swarm-api/swarm_api/codec.py::lease_to_api` — `lease_to_api` reported
  `dispatch_overdue: false` on every lease the API served.
* `apps/swarm-ui/src/Overview.tsx` — narrowed again with
  `dispatch_state === 'LEASED' &&`, a second copy of a guard that had already
  emptied the set.

### Why no test caught it

The only test that touched the predicate asserted the KEY was present and never
its VALUE:

```python
for key in ("released", "expired", "dispatch_overdue"):
    assert key in body
```

A predicate is not covered by a test that checks it was spelled correctly. The
suite stayed green for weeks with the flag permanently False.

### What was applied

The `state is LEASED` guard was removed from the helper, the stale comment in
`codec.py` defending it was corrected, and the duplicate guard in `Overview.tsx`
was deleted. A `heartbeat_at is None` conjunct was deliberately NOT added: the
reconciler needs that distinction because it decides whether to fence a
generation, while this method answers the narrower question of whether the
deadline has passed.

Three tests were added in
`tests/unit/control_plane/test_leases_and_attempts_read_path.py`, and the fix
was proved by mutation — restoring the guard turns
`test_a_dispatched_lease_past_its_deadline_is_overdue` red.

---

## 11. `profiles.py`: a runner profile had no way to be turned off

**Status: ACCEPTED and applied 2026-09-23, by the owner's decision.** This edits
`apps/common/swarm_common/`, which is frozen; it is recorded here as such.

### What was asked

"disable codex support for now. just focus on support and deep integration with
claude."

### Why a flag and not a deletion

`RunnerProfile` had no availability concept at all, so the only way to stop a
profile being dispatched was to remove it from `RUNNER_PROFILES`. That would
have:

* stranded anything already queued against it — nothing was, which was checked,
  but the mechanism should not depend on that being true;
* broken the catalogue entry that four existing `codex` task documents in
  `saga-agents-staging` still name, making those runs unreadable rather than
  merely unrepeatable;
* made re-enabling a revert rather than a word, for something explicitly asked
  for "for now".

So `available: bool = True` and `disabled_reason: str = ""` were added, with
`__post_init__` refusing a profile that is disabled without a reason. A caller
told only that a known profile was refused has nothing to act on.

### The distinction that matters

`validate_runner_profile` now separates **unknown** from **known but refused**.
Collapsing them sends a caller hunting for a typo that is not there: `codex` is
spelled correctly, exists, and will not run. The 422 carries `disabled: true`
and the reason.

The suggestion list in that error is now the AVAILABLE set, not the whole
catalogue — offering a profile that would also be refused is not a suggestion.

### What is deliberately NOT narrowed

`known_providers()` still reads the whole catalogue, so `openai` stays
registrable. A tenant already holds `swarm-tenant-eng-openai`; narrowing this
would make that secret unregisterable, unrotatable and undeletable through the
route that owns it. Disabling a runner must not strand a credential someone has
to be able to clean up.

### Why codex specifically

A twenty-step load test on 2026-09-23 dispatched four codex steps across all
five profiles. All four failed with `openai refused the credential`. The
tenant's `swarm-tenant-eng-openai` holds a single version written 2026-09-16
that the provider rejects. Nine `claude-code` and four `mock` steps in the same
run succeeded.

### Proved by

Three mutations, each caught: re-enabling codex reddens two named guard tests;
skipping the availability check reddens
`test_a_disabled_profile_is_refused_as_disabled_not_as_unknown`; and disabling
without a reason raises at import.

The route-to-type parity tests caught the rest — `/v1/runtimes` serves both new
fields, `types.ts` had to match field-for-field, and
`test_the_screen_reads_every_field_the_route_publishes` then obliged the
Runtimes screen to actually render the disabled state and its reason rather
than accept a field it ignored.

---

## 12. `models.py`: the retry cap had no shared home, so one path forgot it

**Status: ACCEPTED and applied 2026-09-23.** This edits
`apps/common/swarm_common/`, which is frozen, and is recorded here as such.

### The outage

`task_d18d8d8b044d469cb43c` -- the browser step of a twenty-step workflow --
reached **83 attempts against a `max_attempts` of 3**, re-dispatching roughly
every 30 seconds for hours on `gke_create_job_failed`. It was still climbing
when an operator stopped it by hand. Its `current_generation` had reached 83,
so every one of those attempts burned a fencing generation as well as a lease.

### Why

TWO paths return an agent to READY and only one enforced the cap.

`reconciler/store.py` checked it, on the path that REPAIRS an agent to READY.
`scheduler/store.py`'s `return_to_ready_after_failed_dispatch` -- reached
whenever a dispatch fails after the lease is taken -- wrote
`state: READY` unconditionally.

That function is not careless. Its docstring reasons at length about retry
PRESSURE: "a backend that is refusing one dispatch is usually about to refuse
the next, and a tight retry would burn the whole run's lease budget on one
broken task", and it applies a 30-second backoff for exactly that reason. It
spaced the attempts out and never counted them. A backoff is not a cap.

### The change

`retries_exhausted(attempt_count, max_attempts)` in `swarm_common.models`, plus
`Task.retries_exhausted()` delegating to it. Both call sites now use it.

A FREE FUNCTION as well as a method because the two enforcement points do not
both hold a `Task`: the reconciler decides inside a Firestore transaction from
the raw document, where constructing one would mean decoding an agent to read two
integers.

### Why it belongs in the frozen contract

The scheduler and the reconciler are separate images and cannot import each
other. A rule they both need therefore has exactly one home that is not a
restatement, and `scripts/lib/check-contract-parity.sh` exists because
restatements drift. This one did worse than drift: the second copy was never
written.

### Proved by

Two mutations, each caught by named tests. Reinstating the original bug
(`exhausted = False` on the dispatch path) reddens
`test_a_task_at_its_cap_fails_instead_of_retrying` and
`test_a_terminal_task_is_not_given_a_retry_time`. An off-by-one (`>` for `>=`)
reddens four.

The tests pin BOTH paths deliberately, because pinning one is what produced the
bug.

### Also fixed on the way past

An agent sent to FAILED by this path was getting a `next_eligible_at`, which
promises a retry that is not coming and renders in the console as a scheduled
attempt. It now gets a `completed_at` and no eligibility time.

### Correction, 2026-09-24: the shared predicate was right and the scheduler still fed it the wrong number

The predicate is correct and unchanged. The scheduler's caller was wrong. It
passed `task.attempt_count` from the READY snapshot the drain loop had read.
`acquire_lease_in_transaction` increments the count in the **document** and
never in that object, so every check was one attempt behind, and
`max_attempts` 3 bought four attempts. `task_b568a623be8645eb87c6` ended FAILED
at 4/3 on 2026-09-24.

"Proved by" above did not catch this because the tests handed the store an Agent
already carrying the post-increment count, which is a number the production
caller never has. `return_to_ready_after_failed_dispatch` now reads the count
from the document inside a transaction, as the reconciler always did. Its tests
now feed it what the loop feeds it, and one drives the real drain loop from
zero. Nothing in `swarm_common` changed; this note exists so the "Proved by"
section above is not read as covering the caller.

---

## 13. `models.py`: `Attempt` does not record which pool account it ran on

**Status:** open, filed 2026-09-24. A request, not a change: nothing under
`apps/common/swarm_common/` is edited. Raised in
[`docs/web-ui/redesign-v2.md`](web-ui/redesign-v2.md) §6 S6 ("request, not
proposal"), which asked for it to be filed here if pursued.

### The claim, made precise

S6 says "nothing assigns an account to an attempt. `Lease` and `Attempt` carry
no account field". The second sentence is true: `Lease` (`models.py:115-135`)
and `Attempt` (`models.py:242-281`) have no account field. The first is not
quite, and the difference is the reason for this request rather than an
argument against it:

* **The worker does record it -- as an untyped event.** `_lease_account` in
  `apps/agent-worker/agent_worker/lifecycle.py` emits `RUNNING` with
  `detail = {"cause": "account_assigned", "account_id": ..., "provider": ...}`,
  and `control.emit` stamps the event with the attempt id, lease id and
  generation. A rejected account emits `RETRYING` with
  `{"cause": "account_unreadable", "account_id": ...}`.
* **The broker records a HOLD, not a history.** `acquire_hold`
  (`apps/quota-broker/quota_broker/main.py`) counts the assignment against the
  account under an `assignment_id`, and the hold expires and is pruned by the
  quota sweep. Nothing there survives the attempt.

### Why an event is not enough

* **"Which attempts ran on account X"** -- S6's question, i.e. which agent spent
  this subscription's quota -- is a read of every agent's `events` subcollection,
  filtered on `detail.cause` and `detail.account_id` in client code. The two
  collection-group indexes on `events` (`terraform/modules/firestore/indexes.tf`,
  `events-tenant-at`, `events-task-at`) do not cover a field inside `detail`,
  and `detail` is a map whose shape is a convention.
* **One attempt can be offered more than one account.** `_reject_account` hands
  back an account whose secret it cannot read and asks again with it excluded.
  The events record every offer; which account the agent RAN on is "the last
  `account_assigned` not followed by an `account_unreadable` for the same id" --
  an inference every reader would re-derive, and the first one to get it wrong
  attributes spend to an account that never ran the agent.
* **Events are declared an audit trail, not a record.** The `events_ttl` field
  policy expires them on `expires_at`. The worker's `emit` sets no `expires_at`
  today, so these particular events do not expire -- but the policy says what
  events are for, and attribution is not that.
* **Spend is already on `Attempt`** (request #2, applied). Attribution on the
  same document makes "spend per account" one query rather than a join through
  untyped event detail.

### The requested change

Add to `Attempt`:

```python
#: The pool account this attempt's agent RAN on: the last assignment it kept,
#: not every one it was offered. None means "not recorded" -- an older attempt,
#: or a worker that never reached account selection.
account_id: str | None = None
#: "account_pool" or "tenant_secret". None means "not recorded". Separate from
#: account_id because "no account" has two meanings that want different
#: readings: the pool is not how this tenant runs, or nobody wrote it down.
credential_source: str | None = None
```

**Single writer: the worker**, at the two places that already decide it --
`_lease_account` when an account is kept and `_decline_pool` when the tenant
secret is used -- through `control.py`'s attempt document, the way
`record_spend` writes spend (merge, tenant-stamped).

**Not on `Lease`.** The lease is capacity, taken by the scheduler before any
account is chosen; the account is chosen afterwards, by the worker. A lease
field would be one the scheduler writes empty and never knows.

### What it would break if accepted

* **No existing document.** Both fields are optional and default to `None`, so
  nothing needs migrating -- the same shape as request #2.
* **Every reader must read `None` as "not recorded", never as "tenant secret".**
  That is the rule #2 set for spend, and `credential_source` exists so that
  nobody has to infer it from an absent `account_id`.
* **Indexes (Track C).** `attempts.account_id` equality is covered by Firestore's
  automatic single-field index; "an account's attempts, newest first" needs a
  composite `(account_id, created_at)` in `terraform/modules/firestore/indexes.tf`.
* **Fencing (invariant 5).** An attempt document is keyed by its own
  `attempt_id`, so a stale worker can only write its own attempt, never a newer
  one. The write should still go through the same path as `record_spend`, not a
  new one.
* **The TypeScript restatement.** `types.ts` gains the two fields, and
  `scripts/lib/check-contract-parity.sh` section 5 should hold them.

### If it is declined

S6 stays unanswerable except by scanning event subcollections, and the console
cannot show "this account's attempts" without an N+1 over agents. What is left to
live with is the event: its `detail` shape (`cause`, `account_id`, `provider`)
becomes the contract of record by default, and should then be written down as
one, in `docs/`, so that every reader applies the same "last kept assignment"
rule.

---

## 14. `models.py`: a sub-agent has nowhere to name its parent

**Status:** APPLIED 2026-10-02. ACCEPTED 2026-10-02 (owner decision OD-B15-1), WITH the rules in its amendment: accepted by the owner 2026-10-02, when they chose to build child agents (D15). `Task.parent_task_id` and `Task.parent_attempt_id` are in `apps/common/swarm_common/models.py`, set only by swarm-api's children route (`apps/swarm-api/swarm_api/children.py`).

Filed 2026-09-24 as a request. The cancellation and capacity rules it asked to
be decided with the field are in the amendment at the end of this entry. Raised as S1 / B31
in [`docs/web-ui/ui-audit-and-build-prompt.md`](web-ui/ui-audit-and-build-prompt.md):
"Write the request; do not build a fake hierarchy from `depends_on`."

### What was asked for, and what exists

The brief asked for "workflows of agents **and sub-agents**". A workflow step
is a `Task` with `workflow_id`, `step_id` and `depends_on` (`models.py:172-205`).
There is no parent/child relationship between agents anywhere -- not in the
frozen model, not in the API, not in the UI.

`depends_on` is not one. It is ORDER: a step runs after the steps it names have
succeeded. A dependency is not a parent -- the step that produced your input did
not create you, cannot cancel you, and is not waiting on you -- which is why S1
forbids inferring a hierarchy from it.

### How a sub-agent would be created today, and what would be lost

The worker gives the agent its own identity: `SWARM_TASK_ID`, `SWARM_ATTEMPT_ID`
and `SWARM_TENANT_ID` are in the child environment it builds
(`agent_worker/lifecycle.py`, the `base` env). An agent that submitted work of
its own could therefore name itself. There is nowhere typed to put that name:

* `TaskCreate.metadata` (`apps/swarm-api/swarm_api/schemas.py::TaskCreate.metadata`) is a free
  `dict`. A child could carry `metadata.parent_task_id` by convention -- the
  same shape request #3 records for `input_from`.
* A convention in `metadata` is **unvalidated**: any caller can claim any
  parent, including an agent in another tenant (invariant 9), and a UI drawing a
  tree from it would draw whatever a caller typed.
* It is **unindexed**: "this task's children" is a scan.

**Not verified here, and part of the cost:** that an agent inside a worker can
reach the API at all. The child environment carries no API address and no
credential for it (the same `base` env), so sub-agents also need a submission
path. That is not a contract change and is not requested here; it is named so
the field is not mistaken for the whole feature.

### The requested change

Add to `Task`:

```python
#: The task whose agent submitted this one from inside a running attempt.
#: None for work a person, a client or a workflow submitted.
parent_task_id: str | None = None
#: The parent's ATTEMPT. A parent can be retried, and the children of attempt 1
#: and attempt 2 are different work -- the second may re-create the first's.
parent_attempt_id: str | None = None
```

**Part of the request, not a detail: the API SETS these; a caller never
supplies them.** A caller-supplied parent is invariant 10's shape (a caller
naming something the platform should decide) and invariant 9's hazard (a
parent in another tenant). The API resolves the caller's identity already;
the parent is whatever attempt that identity is currently running, or nothing.

Listing children needs `GET /v1/tasks?parent_task_id=` -- a filter in
`swarm-api/store.py` and a composite `(tenant_id, parent_task_id, created_at)`
index. Both are unfrozen (Tracks A and C); they are listed so the full cost is
visible.

### What it would break if accepted

* **No existing document.** Optional, default `None`.
* **Cancellation has to be decided WITH the field, not after it.** Does
  cancelling a parent cancel its children? The field decides nothing, but the
  first screen that draws the tree will be asked, and an answer arrived at by
  whoever writes that screen is the wrong place for it.
* **Capacity (invariants 1-4).** Each child holds its own lease. A parent that
  WAITS on its children holds a slot they may need -- on a narrow pool, a
  deadlock -- and it sleeps through a long wait, which invariant 4 forbids.
  Agents can already do this today by submitting and polling; naming the
  relationship will invite it. Whatever accepts this request should also say
  what a parent is allowed to do while its children run.
* **The workflow rollup** (request #7) must not count a step's children as
  steps.
* **The TypeScript restatement.** `types.ts` gains the two fields, and
  `check-contract-parity.sh` section 5 should hold them.

### If it is declined

"Sub-agents" stays at **does not exist** in the brief's table, and the console
says so. There is no honest partial: a hierarchy inferred from `depends_on` is
refused by S1, and one read from a `metadata` convention would be a tree any
caller can draw.

### Amendment, 2026-10-02: accepted with the cancellation and capacity rules (B15)

The owner's decisions of 2026-10-02 settle what this entry left open. The full
design, with every flow, failure case, route and limit, is
[`docs/design/child-tasks.md`](design/child-tasks.md); this amendment records
what the request now consists of, so the entry stays the contract of record
for the field. **Nothing under `apps/common/swarm_common/` is changed by this
amendment.** The phase-2 pull request that applies it must add its own
acceptance line here, because `scripts/lib/check-frozen-contract.sh` credits
only a line the same pull request adds.

**Citations corrected.** The entry above cites `models.py:172-205` for the
workflow fields; they are `apps/common/swarm_common/models.py::Task.workflow_id`,
`Task.step_id` and `Task.depends_on` today (`Task` is
`apps/common/swarm_common/models.py::Task`). The "base env" it
names is `_build_child_env` (`apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`),
with the three ids at `apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env.base`.
`TaskCreate.metadata` is still `apps/swarm-api/swarm_api/schemas.py::TaskCreate.metadata`.

**OD-B15-1 -- the field is accepted as requested**, both fields optional,
default `None`, and set only by swarm-api from the submitting attempt. Nothing
about the two fields changes. What the acceptance adds is the rules below,
which CR 14 itself said must be decided with the field.

**OD-B15-2 -- the submission path.** The agent never holds a credential. It
writes a request into a spool in its workspace; its worker validates it and
calls `POST /v1/tasks/{parent_task_id}/children`, which accepts only the
tenant's worker service account (derived from the tenant id, never read from
`tenants/<id>`) AND an attempt proof: a signature made with an Ed25519 key the
worker generated in its non-dumpable heap and registered with swarm-api BEFORE
the agent was spawned. An agent can mint its tenant's token from the metadata
server ([security.md](security.md#cloud-metadata-abuse)), and it can read the
whole container environment at `/proc/1/environ`, because `tini` is PID 1 and
is not non-dumpable (`apps/agent-worker/agent_worker/hardening.py` (`WHAT IT DOES NOT COVER. The container's environment is also PID 1's (tini's),`)). So
nothing that authorises a submission travels through the environment, a
mounted file or `work/` and stays valid while the agent runs. The scheduler
passes only a one-use registration nonce, which the worker spends between the
fence and `STARTING -> RUNNING`; registration is first-wins per generation, is
refused once the agent is `RUNNING`, and is attested by swarm-api with its own
key (`swarm-child-key`, held only by `swarm-scheduler` and `swarm-api`) at a
document id no tenant identity can compute. A worker without memory protection
spends the nonce on a tombstone and offers no child path. The route re-reads
the parent and its lease in one transaction and creates nothing unless the
lease is this attempt's live one. `parent_task_id` and `parent_attempt_id`
come from the attested registration, never from the body; `POST /v1/tasks`
keeps refusing them. The design states the one residual (a registration that
could not be made at startup) in
[§3.2](design/child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key).

**OD-B15-3 -- Capacity: what a parent may do while its children run.** It may
run, and submit. It may not wait while holding its lease: the agent gets no
channel that reports a running child's progress, and the only way to learn a
child's result is `await`, which checkpoints, parks the parent with
`ParkReason.CHILDREN_INCOMPLETE` (request 40), releases its lease and exits.
The scheduler promotes it when every child is terminal and every succeeded
child's outputs are written; the resumed worker stages them before the agent
starts. So:

* invariant 1: an awaiting parent is `PARKED` and costs nothing; children cost
  nothing until leased;
* invariant 2: each child acquires its own lease all-or-nothing through the
  unchanged admission; no gang reservation for a fan-out;
* invariant 3: children count from `LEASED` against the same pools; the slot
  the parent gives back by awaiting is the one its children can use, so the
  narrow-pool deadlock this entry warned of cannot form;
* invariant 4: no in-worker wait for a child of any length.

The await refunds the attempt admission counted, up to
`max_child_await_resumes` (4), so waiting is not failing. Depth is 1; fan-out
is capped at 16 children per agent across all its attempts. An agent that exits
0 with live children is awaited anyway: a parent never succeeds over running
children.

**OD-B15-4 -- Cancellation.** Cancelling a parent cancels its non-terminal
children: the API's cancel cascades at once, and a scheduler sweep guarantees
it. A parent that ends `FAILED` or `DEAD_LETTERED` with live children has them
cancelled too. A retried or fenced parent keeps them: they belong to the agent,
and the next attempt finds them. An awaiting parent past
`child_await_max_seconds` (one day) has its outstanding children cancelled and
is then promoted. A child's end never cascades upward. Every cascade is
tenant-scoped, and the API never releases capacity: a running child is flagged
and stops through the ordinary cancel path (invariant 1). A cascaded child
ends with `EndCause.CHILD_CASCADE` (request 41).

**The workflow rollup** (request 7) does not count a child: a child has no
`workflow_id`, even when its parent is a workflow step.

**The TypeScript restatement** stands as requested: `types.ts` gains the two
fields, and `check-contract-parity.sh` section 5 holds them, together with the
values requests 40 and 41 add.

The acceptance needed four more changes to the frozen contract. Each exists
only because this one was accepted, and each is filed as its own request:
[40](#40-statespy-an-agent-awaiting-its-children-has-no-park-reason-and-dependency_incomplete-would-be-promoted-at-once),
[41](#41-modelspy-a-child-cancelled-because-of-its-parent-has-no-end-cause),
[42](#42-specsignpy-the-signed-step-spec-does-not-cover-a-childs-parent) and
[43](#43-identitypy-the-tenant-worker-service-accounts-name-has-no-public-home).

---

## 15. `models.py`: `Attempt` records memory, disk and spend, but not CPU

**Status: ACCEPTED — accepted by the owner on #184, 2026-09-25, as amended
(four fields), and applied in PR #210.** The decision, in the owner's words
([#184, 2026-09-25](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/184#issuecomment-5840698104)): "Contract request #15 is approved: typed
`Attempt` fields `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores` and
`cpu_limit_cores`. They replace the interim heartbeat-event path." Raised
2026-09-24 by the worker-broker lane, which measured CPU without it and
stopped at the frozen line. What was applied, and what it replaced, is under
[Applied](#applied-2026-09-25-pr-210) at the end of this entry.

### What is there now, without the contract

`agent_worker/metrics.py` had no CPU reading at all, so "requested vs used"
(docs/web-ui/redesign-v2.md section 6, S5) had a memory row and could not have
a CPU row. It now measures CPU on every attempt -- cgroup v2 `cpu.stat` when the
container has one, the runner's process tree in /proc otherwise, the kernel's
reaped-children total as the last resort -- and reports:

| field | meaning |
|---|---|
| `cpu_seconds` | CPU time consumed while the attempt's runners ran, summed over every runner it started |
| `peak_cpu_cores` | the busiest sampling interval of any of those runners, in cores |
| `mean_cpu_cores` | total `cpu_seconds` over the total runner time it was measured across |
| `cpu_source` | `cgroup` (the whole container), `proc` (the runner's tree) or `rusage` |

Every figure is the ATTEMPT's. An attempt restarts its runner in place after
a short rate limit or a reloaded credential. Each runner has its own sampler,
and `metrics.combine_usage` puts the runners back together. The first cut of
this reported only the last runner. The same fix also corrected
`peak_rss_bytes` / `peak_disk_bytes` / `oom_near_miss` on the attempt
document, which each runner used to overwrite with its own figure.

It reaches three places, none of them typed or indexable:

* the `attempt resource usage` log line (`LoggingMetricsExporter`);
* Cloud Monitoring, as `cpu_time_ms`, `peak_cpu_millicores` and
  `mean_cpu_millicores` -- which swarm-api cannot read (it holds no
  `roles/monitoring.viewer`);
* every HEARTBEAT event, as a CUMULATIVE `cpu_seconds`, so two consecutive
  events give utilisation over the span between them. This is the only
  per-attempt CPU figure a reader of the API can reach today. It covers every
  runner the attempt has started, so an in-place restart continues the series
  instead of resetting it. The span between two events can include a retry
  wait in which no runner ran, and utilisation computed over that span counts
  the wait as idle time.

### Why it stopped there

The one place a per-attempt figure belongs is the attempt document, next to
`peak_rss_bytes` -- and `record_resource_usage` writes only the keys the frozen
`Attempt` declares, for the reason `record_spend` gives: an untyped field on the
busiest document is one no index can reach and every reader must know by
convention.

### The requested change

Add to `Attempt`, beside `peak_rss_bytes`:

```python
cpu_seconds: float | None = None
peak_cpu_cores: float | None = None
```

Optional, defaulting to `None`, so every existing document remains valid and
nothing migrates. `None` means NOT MEASURED -- a macOS local run has a total and
no peak, and an attempt fenced before its runner started has neither -- which
is different from an agent that used no CPU. `mean_cpu_cores` is deliberately
not requested. `cpu_seconds` over the attempt's own `started_at`/`completed_at`
gives an approximation of it. That approximation is lower than the worker's
figure, because the attempt's span also covers setup, the clone and any retry
wait, where the worker divides by runner time only. For a sizing question
("how much of the reserved CPU did the agent use") the lower figure is the one
that matters, since the reservation is held for the whole span.

### What breaks if it is made

Nothing reads these yet. `agent_worker.control.record_resource_usage` would
gain two keys; `swarm_api.codec.attempt_from_dict`/`attempt_to_api` would each
gain two lines, and `test_api_contract_shapes.py` pins the latter, so the
serialiser cannot drop them silently. `apps/swarm-ui/src/types.ts` restates
`Attempt` by hand and would need the two fields added there too --
`check-contract-parity.sh` does not cover TypeScript.

### What is left to live with if it is declined

A CPU row in "requested vs used" can still be drawn, from the last HEARTBEAT
event of each attempt. It is up to one heartbeat period stale at the end of a
run (the last event before exit is not the exit), it is not queryable across
attempts, and a "CPU per resource class over the last week" report means
reading every attempt's event stream.

### Amendment, 2026-09-25 (#184): add the mean and the limit

**Status:** accepted with the request, 2026-09-25 (see the status line above).
Raised by the #184 backend lane.

**What changed.** The owner decided on #184 that Details shows CPU as the
PEAK and the MEAN cores of the runtime's CPU limit, beside memory and
workspace. The request above deliberately left `mean_cpu_cores` out; it is now
asked for, together with the limit it is a fraction of. The requested change
becomes four fields on `Attempt`, beside `peak_rss_bytes`:

```python
cpu_seconds: float | None = None
peak_cpu_cores: float | None = None
mean_cpu_cores: float | None = None
cpu_limit_cores: float | None = None
```

`mean_cpu_cores` is the worker's figure -- CPU-seconds over RUNNER wall time,
so setup, the clone and retry waits are excluded -- not an approximation from
`started_at`/`completed_at`. `cpu_limit_cores` is what the kernel enforces
(cgroup v2 `cpu.max`), or, where that reads `max` or cannot be read, the
catalogue cpu of the class the container was SIZED with
(`scheduler.dispatch.resource_class_for(task, profile)`, not the profile's own
class). None means not measured throughout, never zero.

**The interim, shipped without the contract (#184).** The worker now puts
every figure on its HEARTBEAT events as flat keys in `detail`
(`agent_worker.metrics.heartbeat_cpu_fields`): the existing `cpu_seconds` and
`cpu_source`, plus `peak_cpu_cores`, `mean_cpu_cores` (kept current while the
runner runs, not only at exit), `cpu_wall_seconds`, `cpu_limit_cores`,
`cpu_limit_source` (`cgroup` or `resource_class`) and `final`. The periodic
reading stays on every fifth heartbeat; one more, with `final: true`, is
emitted when each runner is reaped, and never by an attempt that has been
fenced. `GET /v1/tasks/{id}/attempts?include=usage` serves the newest reading
per attempt from ONE descending read of the agent's events
(`swarm_api.attempt_usage`), with a status that says what the reading is
(`final`, `live`, `last_reading`, `never_ran`, `absent`, `beyond_window`,
`unread`). It works; it is not queryable across attempts, and it costs an
events read per request, which is why it is opt-in.

**What changes if this is accepted.** `control.record_resource_usage` writes
the four fields at each runner's end (the same combined, attempt-wide figures
the final HEARTBEAT carries); `codec.attempt_from_dict` / `attempt_to_api`
read and serve them (`test_api_contract_shapes.py` pins the serialiser);
`include=usage` prefers the typed fields for a `final` reading and reads
events only for a live one. `apps/swarm-ui/src/types.ts` restates `Attempt`
by hand and needs the same four fields.

### Applied, 2026-09-25 (PR #210)

The owner's decision went further than the amendment's last paragraph: the
typed fields REPLACE the interim path, so nothing reads the events for a live
reading either.

* **The contract.** `Attempt` gains the four fields, `float | None = None`,
  at the end of the class rather than beside `peak_rss_bytes`, so no
  positional construction changes meaning. Nothing else in
  `apps/common/swarm_common` changed.
* **The worker** writes them on its own attempt document
  (`control.record_cpu_usage`, from `metrics.attempt_cpu_fields`) with each
  periodic reading, which is every fifth heartbeat, and when each runner is
  reaped. They are the attempt's combined figures. A key that was not measured
  is left out of the merge, never written as null.
* **Removed:** the interim keys on the HEARTBEAT event (`peak_cpu_cores`,
  `mean_cpu_cores`, `cpu_wall_seconds`, `cpu_limit_cores`,
  `cpu_limit_source`, `final`), the `final` HEARTBEAT emitted when a runner
  was reaped, `swarm_api.attempt_usage`, and `include=usage`. The HEARTBEAT
  carries exactly what it did before #188. `cpu_seconds` and `cpu_source`
  stay, because `reconciler.progress` reads them.
* **The API** serves the four fields on every attempt row
  (`codec.attempt_from_dict` / `attempt_to_api`, pinned by
  `test_api_contract_shapes.py` and `test_attempt_cpu_fields.py`).
* **The UI** restates them on `AttemptRow`, as optional fields, where a
  missing key means an API older than the fields.
  `test_ui_api_field_contract.py` holds them both ways.
  `scripts/lib/check-contract-parity.sh` restates no `Attempt` field and did
  not change.
* **Attempts from before the typed fields.** Attempts that ran between #188's
  deploy and this one carry their CPU only on HEARTBEAT events. A read-only
  look at dev at 23:16 UTC on 2026-09-25 found one. Attempts from before #188
  carry their cpu-seconds there too, with no cores. The UI keeps a legacy
  reader for both over the drawer's own event page, and the server keeps
  none. `docs/agent-output.md` says why.
* **What the four fields cannot say.** The interim reading carried its time
  (`measured_at`, `age_seconds`) and the limit's source (`cpu_limit_source`).
  The accepted fields carry neither. Request #26 asks for both; the owner
  accepted it on 2026-09-26 and PR #229 applied it.

---

## 16. `identity.py`: the tenant namespace name, `sanitize_name` included, has two copies

**Status:** open, recorded 2026-09-24 by the reconciler-gke lane (PR #32). The
owner asked for the reconciler to name tenant namespaces "through the SAME
namespace-naming helper the dispatcher uses". It cannot import that helper, so
this is the request that would make that literally true.

### What is duplicated

The rule the dispatcher uses to name a tenant's namespace when the tenant
document records none:

* `apps/scheduler/scheduler/dispatch.py`: `GkeJobDispatcher.namespace_for`
  returns `sanitize_name(template.format(tenant=tenant_id))`, with
  `namespace_template = "swarm-tenant-{tenant}"`.
* `apps/reconciler/reconciler/backends.py`: `GkeBackend.namespace_for`
  returns `sanitize_name(prefix + tenant_id)`, with
  `ReconcilerConfig.namespace_prefix = "swarm-tenant-"`. `sanitize_name` here
  is a copy of the scheduler's. The comment-stripped bodies are identical, and
  both import the character class from `swarm_common.identity._TENANT_SAFE`.

Both prefer the tenant document's recorded `namespace`. That precedence is also
stated twice.

### Why it cannot be one copy today

The same reason as request 5. `images/swarm-reconciler/Dockerfile` copies
`apps/common/` and `apps/reconciler/`, and `images/swarm-scheduler/Dockerfile`
copies `apps/common/` and `apps/scheduler/`. Neither image contains the other's
package, so `swarm_common` is the only place a single copy could live in both.

### How the copies drifted before the copy existed

Until PR #32, the reconciler named the namespace with `detect.sanitised`. That
function reproduces only the character-class half of `sanitize_name`. It has no
63-character truncation, no hash of the full name, and no `s` prefix for a name
that does not start with a letter. The only test pinning the two used short
tenant ids, so it could not see the difference.

The difference was latent, not observed:

* `identity._slug` caps a tenant id at 11 characters.
* `scripts/register-tenant.sh` refuses anything longer.
* A live attempt is read at the namespace its own `execution_name` records.

### What holds the copies together now

`tests/unit/control_plane/test_reconciler_gke_namespaced.py`:

* `test_the_reconciler_and_the_dispatcher_sanitise_names_identically` runs both
  `sanitize_name` copies over a corpus that reaches every branch.
* `test_the_reconciler_names_a_long_tenant_namespace_exactly_as_the_dispatcher_does`
  pins the two `namespace_for` methods on ids up to 200 characters.
* `test_the_reconciler_names_a_tenant_namespace_exactly_as_the_dispatcher_does`
  pins the recorded-namespace precedence.

`scripts/lib/check-contract-parity.sh` section 6 holds the prefix to the
template. As with request 5, these checks work only because the test process
can import both services, which neither image can.

### The requested change

Add a stdlib-only function to `swarm_common/identity.py`, next to
`_TENANT_SAFE`, which it already depends on:

```python
def k8s_name(*parts: str, max_length: int = 63) -> str: ...        # today's sanitize_name
def tenant_namespace(tenant_id: str, recorded: str | None = None,
                     template: str = "swarm-tenant-{tenant}") -> str: ...
```

Both services would call it. Two decisions go with it:

* **The exception type.** Today the scheduler raises `DispatchError(code=
  "invalid_resource_name")` and the reconciler raises `ValueError`. A shared
  function would raise `ValueError`, and the dispatcher would wrap it.
* **Whether `detect.sanitised` should use `k8s_name` too.** `sanitised` matches
  label-derived ids back to document ids, and labels also pass through
  `sanitize_name` with the 63-character cap. Agent and attempt ids are 25
  characters, so the cap is unreachable there today.

### What it would break if accepted

It adds functions and changes none, so no stored document and no wire format
changes. There is also a third copy, in shell: `tenant_namespace` in
`scripts/lib/common.sh`, which `register-tenant.sh` calls. It is the bare
prefix plus the id, with no sanitising. That is correct only because
`register-tenant.sh` first refuses any id that is not a clean slug of 11
characters or fewer. It would stay a restatement, and `check-contract-parity.sh`
section 6 would still hold its prefix.

### What is left to live with if it is declined

Two copies, and three tests that pin them over inputs longer than any id in
use. If the rule changes, both copies must be edited. If only one is edited,
the tests fail rather than production.

---

## 17. `states.py`: a cancel that is only requested is recorded as `cancelled`

**Status: ACCEPTED — accepted by the owner in session, 2026-09-24 — and applied
in PR #44.** This edits `apps/common/swarm_common/states.py`, which is frozen,
and is recorded here as such. The owner accepted it as "#13" together with
request 18 as "#14": those were the session's numbers for the two incident
requests, not this file's. **Requests 13 and 14 in this file (the pool account
on `Attempt`, a sub-agent's parent) are NOT accepted by this and are still
open.**

Recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559` (where it was filed
as CR-1). Found while the incident's stuck agents were read event by event. It
did not cause that incident.

### What was applied

* `EventType.CANCEL_REQUESTED = "cancel_requested"`, documented in `states.py`
  as NOT terminal, next to `CANCELLED`, which is documented as "the task
  reached CANCELLED".
* `swarm_api.store.request_cancel` writes `CANCEL_REQUESTED` when it only sets
  the flag and `CANCELLED` only for the transition it makes itself (SUBMITTED,
  QUEUED, READY, PARKED to CANCELLED). `detail.phase` is still written on both,
  so a reader of the old discriminator keeps working.
* The terminal `cancelled` for an agent that held capacity was already written by
  whoever finishes it, and is unchanged: the worker (`control.finish`), the
  reconciler (F-3, `repair.py`), the scheduler (`SchedulerStore.cancel`).
* **History is read, not rewritten.** Events stored before this change keep
  `type: cancelled, phase: cancel_requested`. `swarm_api.codec.stored_event_type`
  (called by `event_from_dict`, the decoder behind every event route) serves
  exactly that shape as `cancel_requested`, with the detail as stored, so a
  served legacy request is the same shape as a new one. A `cancelled` with
  `phase: cancelled` or with no phase is a real cancel and is untouched. No
  migration was written: this lane writes no live data, and the read is one
  line where a migration is a write to every agent's history.
* Four readers can see the raw legacy row, and each applies the same reading to
  it. Only one reads Firestore directly: `scripts/load-test.sh`, through
  `testlib.sh`'s `task_events`. The other three read through the API. They
  carry their own copy because they can meet a `swarm-api` image from before
  this change, which serves the legacy row as stored:
  * `swarm_mcp.follow.event_type`, called by `swarm_follow` (`_event_row`) and
    `swarm tail` (`cli.cmd_tail`);
  * `scripts/benchstat.py`, whose one collector, `bench-dispatch.sh`, reads
    `GET /v1/tasks/{id}/events`. (Corrected 2026-09-24: this record first said
    benchstat reads Firestore directly. It does not.)
  * the console's `apps/swarm-ui/src/events.ts`, used by the Timeline's
    end-of-history check and its type labels.
* Tests: `tests/unit/control_plane/test_cancel_request_is_not_a_cancel.py`,
  plus additions to `test_request_cancel_is_transactional.py`,
  `tests/unit/mcp/test_follow_cursor.py`, `tests/unit/scripts/test_benchstat.py`
  and `apps/swarm-ui/src/__tests__/cancel.request.timeline.test.tsx`. Every
  copy of the legacy reading except `load-test.sh`'s jq is pinned by a test
  that feeds it the raw row. For the plugin, two tests run the real application
  with its decoder put back to the pre-change `EventType(data["type"])`: through
  the current API, which already reads the legacy shape, removing either call
  site stayed green.

### What is still true after it (the breakage the request predicted)

The breakage predicted below is real and was not engineered away. A
`swarm-api` image from before this change decodes a stored event with
`EventType(data["type"])`, and `cancel_requested` is not in its enum. So when
such an image serves a page of `GET /v1/tasks/{id}/events` that holds a
`cancel_requested` event, it raises `ValueError` inside `_keyset_page`. There is
no per-row handling, so the whole page is a 500. `swarm-api` is the only
decoder of stored events; the scheduler, reconciler and worker never decode
one.

That happens in two situations, and only the first one ends by itself:

* **During the rollout.** An instance still on the old image serves the page
  after a new instance has written the event. This lasts until the rollout
  completes.
* **After a rollback past this change.** The documented recovery for a broken
  control-plane service is to deploy the previous manifest
  ([operations.md §7](operations.md#7-deploying-a-change),
  [disaster-recovery.md §3](disaster-recovery.md#3-a-control-plane-service-is-broken)).
  The `cancel_requested` events written since the deploy are permanent. A
  pre-change image fails on them for as long as it serves, on every agent that
  was cancelled while it held capacity. The console Timeline, `swarm_follow`
  and `swarm tail` then report that agent's history as unreadable, during an
  incident, which is when it is needed most. Reverting this change has the
  same effect. (Corrected 2026-09-24: this record first said the crash "lasts
  one rollout of `swarm-api`", which holds only if nobody rolls back.)

Both runbooks now say this: do not roll `swarm-api` back to a build from before
this change, and do not revert it. Roll forward instead, and check a rollback
target with `git grep CANCEL_REQUESTED <tag> -- apps/common/swarm_common/states.py`.

Not done, because it is the owner's choice: shipping the reader (the enum
member and `stored_event_type`) as a release of its own before the writer
(`request_cancel`). That would make a one-step rollback after the writer safe.
It would not make a rollback past the reader safe, so the runbook constraint
is needed either way.

### The claim that is false

`EventType.CANCELLED` (`states.py`, value `"cancelled"`) is read as "this task
is cancelled". The UI treats it as one of the four terminal events
(`AgentDetail.tsx`, `TERMINAL_EVENTS`: "a terminal task has one by
construction"). It is also written when nothing has been cancelled.

`swarm_api.store.request_cancel` handles an agent that holds capacity (LEASED,
DISPATCHED, STARTING, RUNNING) by setting `cancel_requested = true` and nothing
else, because the worker or the reconciler has to release the lease. It still
appends an event of type `CANCELLED`, and only a detail field,
`phase: "cancel_requested"`, distinguishes it from a real cancel.

### Measured

check-2, check-3, check-4 and check-5 of `wf_ebb3ab2d65664707a559` each carry a
`cancelled` event from the cancel POSTs at 07:40:20–07:40:31Z, with
`from_state: DISPATCHED` and `phase: cancel_requested`. At 08:55:11Z all four
were still `DISPATCHED`, with leases unreleased. For more than an hour, every
consumer that reads the event type saw four cancelled agents, and the task
documents said otherwise.

### The requested change

Add `EventType.CANCEL_REQUESTED = "cancel_requested"`. `request_cancel` emits it
when it only sets the flag, and keeps `CANCELLED` for the immediate transition
it makes itself (SUBMITTED, QUEUED, READY, PARKED to CANCELLED). The terminal
`cancelled` for an agent that held capacity then comes from whoever actually
finishes it:

* the worker (`control.py`, state-to-event map);
* the scheduler (`SchedulerStore.cancel`);
* the reconciler, once incident item F-3 lands.

### What it would break if accepted

* **It is additive to the enum.** Every writer of existing values is unchanged.
* **An old reader crashes on the new value.** `swarm_api.codec.event_from_dict`
  decodes a stored event with `EventType(data["type"])`. An API instance on the
  previous image that lists the events of an agent holding a `cancel_requested`
  event raises `ValueError` for that page. `swarm-api` is both the only writer
  and a reader, so the exposure is one rolling deploy. It is still a window in
  which an agent's event page can fail. (Corrected after acceptance: a rollback
  to an image from before the change reopens it, for as long as the rollback
  lasts. See "What is still true after it" above.)
* **History keeps the old shape.** Events already written stay
  `type: cancelled, phase: cancel_requested`. A reader that wants the truth
  about old agents must keep reading `detail.phase`, unless a one-off migration
  rewrites them. Nothing here proposes that migration.
* The UI's `TERMINAL_EVENTS` and any timeline grouping need to show the new type
  as a non-terminal marker. That is a UI change in `apps/swarm-ui`.

### If it is declined

The phase discriminator stays the only signal, so every consumer that treats
`cancelled` as terminal must also check `detail.phase`. The UI's
terminal-event check is one such consumer today. A consumer that forgets
reports a cancelled agent that still holds capacity, which is the situation this
incident's operators were in.

---

## 18. `profiles.py`: `RunnerProfile.command` is the lifecycle's child argv, never a container command

**Status: ACCEPTED — accepted by the owner in session, 2026-09-24 — in its
stronger form, and applied in PR #44.** This edits
`apps/common/swarm_common/profiles.py`, which is frozen, and is recorded here as
such. The owner's "#14" (see request 17's status for the numbering): the field
is RENAMED to `runner_argv` and documented as the argv the worker lifecycle
starts as its child, never a container command.

Recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559` (where it was filed
as CR-2). The code defect it describes is fixed outside the frozen package, in
`apps/scheduler/scheduler/dispatch.py`. This request is about the field that
made the defect easy to write.

### What was applied

* `RunnerProfile.command` is now `RunnerProfile.runner_argv`, with the comment
  proposed below on the field itself (plus a note of the old name and why it
  changed). Values are unchanged.
* Every reader moved: `agent_worker.lifecycle._runner_argv`,
  `swarm_api.runnerinputs.runner_module`, the comments in
  `scheduler/dispatch.py`, and the tests listed below plus
  `test_runtime_catalogue.py`, `test_runtimes_screen.py`,
  `test_dispatch_manifests.py` and `tests/unit/mcp/test_profiles.py` (whose
  leak list named `command` and now names `runner_argv`, with an assertion
  that every name on it is a real field).
* `tests/unit/control_plane/test_runner_argv_is_the_lifecycles_child.py` pins
  that no field is called `command`, that every profile's `runner_argv` names a
  real runner module, and that the warning stays on the field.
* Not a wire change and not a data change, as predicted: no route serialises
  the field and no stored document holds it. `scripts/lib/check-contract-parity.sh`
  restates nothing about it (checked).

### What happened

`RunnerProfile.command` is `("python", "-m", "agent_worker.runners.<x>")`, the
argv of the RUNNER. The worker lifecycle starts it as a supervised child
(`agent_worker.lifecycle._runner_argv`), and `swarm_api.runnerinputs` reads its
last element to name the runner module. Nothing in `profiles.py` says so. A
field named `command` on the object that describes what a container runs reads
as a container command. Both dispatchers used it that way:

* `GkeJobDispatcher._manifest` set `"command": list(profile.command)`;
* `CloudRunJobDispatcher._build_job` set `command=list(profile.command)`.

A container `command` replaces the image ENTRYPOINT, which is the lifecycle.
Every GKE pod therefore ran the bare runner, with no fencing (invariant 5), no
checkpoints (invariant 8), no heartbeat and no lease release. Five leases held
every browser slot until an operator intervened. The Cloud Run copy was latent
only because Terraform's Jobs leave `command` unset. The prohibition existed in
exactly one place, a comment in `kubernetes/worker-templates/worker-job.yaml`
that the code never read.

### The requested change

The minimum is a comment on the field:

```python
#: The argv of the RUNNER, which the worker lifecycle starts as a supervised
#: CHILD (agent_worker.lifecycle._runner_argv). NEVER a container command:
#: both worker images' ENTRYPOINT is the lifecycle (`tini -- python -m
#: agent_worker`), and a container `command` replaces it, switching off
#: fencing, cancel, heartbeat, checkpointing and lease release at once. A
#: dispatcher names the profile in RUNNER_PROFILE and sets no command.
command: tuple[str, ...]
```

The optional, stronger change is to rename the field to `runner_argv`, so that
`list(profile.command)` cannot be written by someone reaching for a container
command.

### What it would break if accepted

* **The comment breaks nothing.**
* **The rename touches every reader:**
  * `agent_worker/lifecycle.py` (`_runner_argv`);
  * `swarm_api/runnerinputs.py` (`runner_module`);
  * `tests/unit/worker/test_runners.py`;
  * `tests/unit/control_plane/test_workflow_step_input_surface.py`.

  It is not a wire change. The runtimes catalogue
  (`swarm_api/routes/platform.py`) serialises the profile field by field and
  does not include `command`. No stored document holds the field either, since
  profiles are code, so there is no data migration.

### If it is declined

The guard is the tests added with the fix. For every profile, on both backends,
`tests/unit/control_plane/test_dispatch_manifests.py` asserts that neither
dispatcher sets `command` or `args`.
`tests/unit/worker/test_kubernetes_manifests.py` asserts that the dispatcher's
Job and the rendered YAML agree field by field. Those catch a reintroduction.
Nothing warns the next reader of `profiles.py` before they write it.

---

## 19. `states.py`: no `EventType` says "the reconciler evicted this execution"

**Status:** open, recorded 2026-09-24 by the browser-eviction lane. The owner
asked for browser pods that are stuck without progress, or left running after
their agent finished, to be evicted "with an event naming the reason". The event
is written. Its type is borrowed.

### What is there now, without the contract

The reconciler's two GKE eviction rules (`apps/reconciler/reconciler/detect.py`,
runbook `docs/runbooks/browser-eviction.md`) write these events:

* **`stuck_no_progress`.** The reconciler only fences in that pass, so
  `generation_fenced`, with `detail.finding` and `detail.reason`, is accurate.
  The rules that finish the job on a later pass write their usual
  `lease_released`, then `ready`, `failed` or `cancelled`.
* **`left_running`.** The agent is already terminal, and nothing is fenced. The
  event is `generation_fenced` with `detail.phase: "left_running"`. That type is
  the nearest the frozen `EventType` offers: the reconciler stopped an
  execution of this generation that had no right to run. It is still a
  borrowed word. A reader of the agent timeline sees "generation fenced" after
  "succeeded" and must open `detail` to learn it was an eviction.

`EventType` cannot be extended from outside: `swarm_api.codec` parses every
stored event with `EventType(data["type"])`, so writing an unknown type string
would break the read path for that agent.

### The requested change

Add one member to `swarm_common/states.py`:

```python
class EventType(str, Enum):
    ...
    EXECUTION_EVICTED = "execution_evicted"
```

The `left_running` rule would write it instead of `generation_fenced`. The
stuck rule would keep `generation_fenced`, because fencing is what it does.

### What breaks if it is made

Nothing stored. Old events keep their types. The costs:

* **An old reader crashes on the new value.** This is the same window request
  17 describes. `swarm_api.codec` decodes with `EventType(data["type"])`, so an
  API instance on the previous image fails the event page of any agent holding
  an `execution_evicted` event until the rolling deploy finishes. Request 17
  and this one both add a member to `EventType`, so they are cheapest applied
  together, in one release.
* **The UI.** `AgentDetail.tsx` and `AttemptTimeline.tsx` render `e.type` as
  text, so the new type shows as its own name. `TERMINAL_EVENTS` does not list
  it, which is correct, because an eviction is not a terminal transition.
* **Closed-list consumers.** Anything that assumes the list of types is closed
  needs a case for the new one. None was found in `apps/`.

### What is left to live with if it is declined

The `generation_fenced` events with `phase: left_running` stay as they are.
They are accurate about what was stopped, but not about why. The pass report
and the `evicted a GKE job` log line name the kind exactly either way.

---

## 20. `models.py`: `Workflow.on_step_failure` is a bare `str`, and its vocabulary is stated four times

**Status:** open, recorded 2026-09-24 by the lane that made the scheduler honour
the field (branch `lane/on-step-failure`). It was filed as 19 on that branch,
and renumbered 20 when main took 19 for the browser-eviction request.

### What is true today

`Workflow.on_step_failure: str = "fail_workflow"   # or "continue"`. The comment
is the only place in the frozen contract that names the second value. Nothing
constrains the first. The vocabulary is therefore stated separately by each
component that needs it:

* `swarm_api.schemas.WorkflowCreate.on_step_failure`:
  `Literal["fail_workflow", "continue"]`, the only validation;
* `swarm_mcp.server.TOOLS` (`swarm_workflow`): a JSON-schema `enum` and
  `default`;
* `scheduler.loop.FAIL_WORKFLOW`: the one value the scheduler acts on;
* the frozen dataclass default itself.

Until 2026-09-24 the scheduler did not read the field, so a disagreement
between these copies cost nothing. It does now. A rename of `fail_workflow` in
the API's Literal that missed `scheduler/loop.py` would make the scheduler
ignore the setting again, silently, which is the defect that change fixed.
That is the mirrored-value failure this repository has already had three
outages from.

### The requested change

Add to `states.py` (or `models.py`):

```python
class OnStepFailure(str, Enum):
    """What a FAILED or DEAD_LETTERED step does to the rest of its workflow.

    CANCELLED is not a failure under either value: a stopped step takes only its
    own dependents.
    """
    #: Cancel every step of the workflow that has not started.
    FAIL_WORKFLOW = "fail_workflow"
    #: Cancel only the transitive dependents of the failed step.
    CONTINUE = "continue"
```

and type the field `on_step_failure: OnStepFailure = OnStepFailure.FAIL_WORKFLOW`.
The API's Literal, the MCP enum and the scheduler constant are then derived from
`OnStepFailure` rather than restated.

### What it would break if accepted

* **The stored value does not change.** A `str` Enum's value is the same string,
  so no workflow document needs migrating.
* **Every constructor of `Workflow` still type-checks at runtime,** because
  dataclasses do not enforce annotations. A caller passing a plain string keeps
  working until it is changed to pass the enum.
* **`asdict(workflow)` would carry the enum member, not the string.**
  `swarm_api.codec.workflow_to_firestore` must write `.value` explicitly, as it
  already does for `state`. Otherwise the Firestore client receives an Enum,
  and whether it serialises a `str` subclass unchanged has not been verified.
* **Decoding must map an unknown stored value to something.** Today the API
  echoes whatever string is stored. `OnStepFailure(value)` would raise. The
  decoder needs an explicit rule, and the scheduler's current rule (anything
  other than `fail_workflow` leaves only the dependency rule in force) is the
  conservative one, because a cancel cannot be undone.

### If it is declined

`tests/unit/control_plane/test_on_step_failure.py::test_the_policy_vocabulary_agrees_everywhere_it_is_stated`
holds the four copies together: the API's Literal, the MCP enum and default,
the frozen default and the scheduler's constant. It catches a rename in any one
of them in CI. It does not stop a fifth copy from being written somewhere it
does not look.

---

## 21. `states.py`: the worker's exit codes have no shared home, and the reconciler now acts on one

**Status:** open, recorded 2026-09-25 by the lane that made exit 78
non-retryable (branch `lane/cannot-start-handling`). If another branch has
taken 21 by the time this merges, renumber this one.

### What is true today

The worker's exit codes are `agent_worker.errors.ExitCode`. Until 2026-09-25 no
other component read them. The owner decided on 2026-09-25 that a worker that
cannot start exits 78 and that 78 is non-retryable. So the reconciler now reads
a finished execution's exit code (a Cloud Run task's
`last_attempt_result.exit_code`, a GKE pod's `state.terminated.exitCode`). On
78 it fails the agent at once, with no retry. The same change split a second
code off 78: 69, "a dependency was unavailable before the runner", which the
reconciler must go on retrying.

The reconciler's image carries `apps/common/` and `apps/reconciler/` only, so
it cannot import `agent_worker`. The number is therefore stated twice:

* `agent_worker.errors.ExitCode.CONFIG = 78`, which the worker exits with;
* `reconciler.detect.WORKER_EXIT_CANNOT_START = 78`, which the reconciler
  fails an agent on.

`tests/unit/worker/test_worker_cannot_start.py::test_the_reconcilers_78_is_the_workers_78`
holds the two together in CI.

Since #198 the reconciler acts on every OTHER code too, but only to requeue,
and without naming any of them: an execution that ended with anything but 78
while its agent was still DISPATCHED or STARTING is fenced, released and
requeued in the pass that sees it (`reconciler.detect.detect_ended_at_startup`),
with the code quoted in `last_error`. That rule needs no second number, so it
adds no copy. It does depend on 78 being the only code that must NOT be
requeued, which is the same fact this request would put in one place. That is the mirrored-value arrangement this
repository has had three outages from. Here the failure would be quiet in both
directions. If the worker's number moved and the reconciler's did not, a worker
that cannot start would go back to being retried until its attempts ran out.
If the reconciler's moved onto a code the worker uses for something else, such
as 69 or 75, agents that should be retried or parked would be failed.

### The requested change

Add the worker's exit contract to `swarm_common/states.py`, beside the other
vocabularies both sides of the platform act on:

```python
class WorkerExit(IntEnum):
    """What a worker process's exit status means to the platform."""
    OK = 0
    FAILED = 1
    #: A dependency was unavailable before the runner. Retried.
    UNAVAILABLE = 69
    GENERATION_FENCED = 70
    CANCELLED = 71
    PARKED = 75
    TIMEOUT = 76
    #: The worker cannot start, and another attempt would fail the same way.
    #: The reconciler fails the task without a retry.
    CANNOT_START = 78
    TENANT_MISMATCH = 79
```

`agent_worker.errors.ExitCode` and `reconciler.detect.WORKER_EXIT_CANNOT_START`
would then both be derived from it, and the parity test deleted.

### What it would break if accepted

* **Nothing stored.** Exit codes are not persisted as names. The attempt
  document's `exit_code` is the integer, and it stays the integer.
* **`ExitCode.CONFIG` is referenced by that name** in the worker and its tests.
  Keeping `CONFIG = WorkerExit.CANNOT_START` as an alias avoids a rename in
  the same change.
* **143** (128 + SIGTERM, `startup.EXIT_INTERRUPTED`) is a signal convention,
  not a platform decision, and could stay where it is.

### If it is declined

The parity test stays and does its job for these two copies. A third reader of
exit codes (a UI badge, a smoke check, an alert on 78s) would have to restate
the number again, and would need its own parity test to be safe.

---

## 22. `profiles.py`: whether a runner profile can run on a pool account is stated by the worker and restated by the scheduler

**Status:** open, recorded 2026-09-25 by the lane that made admission and
dispatch ask one question (branch `lane/pool-credential-admission`, #169). If
another branch has taken 22 by the time this merges, renumber this one.

### What is true today

A pool account is a Claude subscription. Its token fills one variable,
`CLAUDE_CODE_OAUTH_TOKEN`, and a runner profile can run on an account only if
its `secrets` declare that name. `claude-code` does. `browser` does not.

The frozen catalogue says nothing about this. It lists the names, and the
meaning of one of them is decided outside it:

* `agent_worker.accountlease.ACCOUNT_TOKEN_ENV`, which the worker checks before
  it asks the broker for an account (`lifecycle._lease_account`);
* `scheduler.credentials.SUBSCRIPTION_TOKEN_ENV`, which admission checks before
  it lets a tenant with no key of its own through on the pool.

The scheduler's image does not carry the worker, so it cannot import the
worker's constant. `tests/unit/worker/test_pool_credential_parity.py` holds the
two together. It compares the constants, and it also runs the real worker's
credential resolution for every profile in the catalogue against the
scheduler's `credential_for`.

If they drifted, the failure would be quiet in both directions:

* the scheduler expects the pool and the worker does not ask it: a keyless
  tenant's agent is admitted, a container starts, the worker parks it on
  CREDENTIAL_MISSING, and the credential sweep promotes it again on the next
  drain;
* the worker would ask and the scheduler does not expect it: a tenant the pool
  serves is parked at admission and never runs.

### The requested change

Add to `profiles.py`:

```python
#: The variable a Claude subscription token fills. A profile whose `secrets`
#: name it can run on an account from the pool; one that does not, cannot.
SUBSCRIPTION_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


@dataclass(frozen=True)
class RunnerProfile:
    ...

    @property
    def runs_on_a_pool_account(self) -> bool:
        return self.provider is not None and SUBSCRIPTION_TOKEN_ENV in self.secrets
```

`agent_worker.accountlease.ACCOUNT_TOKEN_ENV` and
`scheduler.credentials.SUBSCRIPTION_TOKEN_ENV` would then both be derived from
it. The worker's `_lease_account` and the scheduler's `credential_for` would
both ask `profile.runs_on_a_pool_account`, and the parity test could shrink to
the behavioural half.

### What it would break if accepted

* **Nothing stored.** No document carries the name.
* **`ACCOUNT_TOKEN_ENV` is imported by that name** in the worker and its tests.
  Keeping it as an alias of the new constant avoids a rename in the same
  change.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) mirrors `RunnerProfile` fields, not properties, so
  a property adds nothing it has to follow.

### If it is declined

The parity test stays and does its job for these two copies. A third reader,
for example a UI hint that says which profiles a lent account can run, would
have to restate the name again and would need its own parity test.

---

## 23. `models.py`: a task's end has no typed cause, so the outcome ledger classifies `last_error` text

**Status: ACCEPTED — accepted by the owner on 2026-09-25, in the decisions
comment on #185 (item 9: "Contract requests 23 ... and 24 ... are accepted") —
and applied in PR #217.** This edits `apps/common/swarm_common/models.py`,
which is frozen, and is recorded here as such. Recorded 2026-09-25 by the lane
that built `GET /v1/outcomes` (branch `lane/outcomes-api`, #185).

### What was applied

* `EndCause(str, Enum)` in `models.py`, beside `Task`, with the request's ten
  values and ONE MORE, `INPUTS_UNAVAILABLE = "inputs_unavailable"`. It is the
  same comment's item 4: the worker refusing to stage a declared input
  (`agent_worker.errors.InputUnavailable`) is its own class, and a typed cause
  that could not say so would have sent those agents back to the text. 10 of
  dev's 13 "runner errors" on 2026-09-25 were exactly that.
  `CANCELLED_PARENT` is kept as requested: it is what item 2 (split "after a
  cancel" from "after a failure") needs a writer to say.
* `Task.end_cause: EndCause | None = None`, written by `to_firestore` as its
  string value. None on a success, on every agent that has not ended, and on
  every document written before this change.
* Every terminal writer records it beside `completed_at`, deciding it where the
  terminal state is decided:
  * the worker (`control.finish`, `control.fail_retryably`; the lifecycle
    passes TIMEOUT, OUTPUTS_MISSING, INPUTS_UNAVAILABLE, CANNOT_START for a
    78, RUNNER_ERROR, or CANCEL_REQUESTED). `finish` writes the field on EVERY
    terminal state, None included; "runner stopped on SIGTERM" without a
    requested cancel is the one end no value names, and carries None;
  * the reconciler (`ControlStore.repair_task_state`, inside its transaction):
    CANCEL_REQUESTED when the flag picks CANCELLED, else `failed_cause` --
    CANNOT_START for an exit-78 finding, LOST_WORKER for a requeue downgraded
    on spent attempts;
  * the scheduler: `return_to_ready_after_failed_dispatch` (DISPATCH_FAILED, or
    CANCEL_REQUESTED), `cancel` (a REQUIRED keyword: CANCEL_REQUESTED, or
    FAILED_PARENT / CANCELLED_PARENT by `loop._parent_cause` from each
    parent's OWN END -- a CANCELLED parent that is a failure's cascade or was
    swept passes a failure down, and a failure wins -- or, for a parent
    cancelled before the field existed with no flag, None, which the ledger
    splits by the chain) and `cancel_if_not_started` (WORKFLOW_SWEEP);
  * the API's cancel, only when that write ends the agent (a pending agent): a
    flag on an agent holding capacity ends nothing and records nothing.
* `swarm_api.outcomes` reads `end_cause` first and falls back to its text
  classifier only for an agent without one. `DERIVE_VERSION` and
  `CLASSIFIER_VERSION` went 1 -> 2, and a stored day is re-derived when EITHER
  differs (the classifier's version had been written and never read).
* **Corrected in review, before release (the review of #217):** the cascade
  split first read only the direct parents' STATES, in both the scheduler and
  the ledger's fallback. The dependency rule is transitive, so every step two
  or more hops below a FAILED one was "after a cancel" nobody made. Both now
  read each parent's own end, and the ledger follows untyped cascades up the
  chain (`outcomes._read_cascade_ancestors`).

### Proved by

`tests/unit/control_plane/test_outcomes_end_cause.py` (the enum, the field, the
classifier, the split, the versions), `tests/unit/control_plane/
test_end_cause_writers.py` (scheduler and API), `tests/unit/worker/
test_end_cause_worker.py` and `tests/unit/worker/test_end_cause_reconciler.py`,
each pushed red before the change (PR #217 names the runs).

The request as it was filed follows, unchanged.

### What is true today

The Timeline's "Why tasks failed" and "Why tasks were cancelled" cards need one
fixed class per ended task. `Task` carries `state` and a free-text
`last_error`, and nothing else about why the task ended. So
`swarm_api.outcomes.classify_failure` and `cancel_cause` infer the class from
text written by four writers the API image does not carry:

* `agent_worker/lifecycle.py` (timeouts, runner errors);
* `agent_worker/expected_outputs.py` (missing outputs);
* `reconciler/detect.py` and `reconciler/repair.py` (could not start, lost
  worker);
* `scheduler/store.py` and `scheduler/loop.py` (dispatch failures, cascade
  cancels, the fail_workflow sweep).

`tests/unit/control_plane/test_outcomes_classifier_parity.py` pins every pattern
to its writer's source, so a reworded message turns CI red. That is still a
restatement of every writer's words, in a fifth place.

One case is not recoverable from the text at all. "an upstream workflow step
did not succeed" is written when a parent is FAILED, DEAD_LETTERED **or
CANCELLED**. So a cancel caused by a person's cancel reads the same as one
caused by a failure.

### The requested change

Add to `models.py`:

```python
class EndCause(str, Enum):
    """Why a task reached its terminal state. Written by the terminal writer."""
    TIMEOUT = "timeout"
    CANNOT_START = "cannot_start"
    LOST_WORKER = "lost_worker"
    OUTPUTS_MISSING = "outputs_missing"
    DISPATCH_FAILED = "dispatch_failed"
    RUNNER_ERROR = "runner_error"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED_PARENT = "failed_parent"
    CANCELLED_PARENT = "cancelled_parent"
    WORKFLOW_SWEEP = "workflow_sweep"
```

Add `end_cause: EndCause | None = None` to `Task`, set by the four terminal
writers beside `completed_at`. `classify_failure` would then read the field
first and fall back to the text only for tasks that ended before it existed.

### What it would break if accepted

* **Nothing stored.** The field is optional, and an old document decodes with
  None.
* **Every terminal writer changes**: the worker's `control.finish`, the
  reconciler's `repair_task_state`, and the scheduler's cancel and
  dispatch-failure paths. They are separate images, so the field would be
  written by some before others during a rollout. The text fallback covers
  that window.
* **`DERIVE_VERSION` in `swarm_api.outcomes` must be bumped**, so that stored
  days are re-derived with the new classes.

### If it is declined

The parity test stays and does its job. The cascade-cancel overclaim stays
unless the ledger reads each cancelled step's parents at derive time, which is
the recommended option on #185's open question.

---

## 24. `profiles.py`: whether a profile's cost is declared rather than measured is named outside the catalogue

**Status: ACCEPTED — accepted by the owner on 2026-09-25, in the decisions
comment on #185 (item 9) — and applied in PR #217.** This edits
`apps/common/swarm_common/profiles.py`, which is frozen, and is recorded here as
such. Recorded 2026-09-25 by the lane that built `GET /v1/outcomes` (branch
`lane/outcomes-api`, #185).

### What was applied

* `RunnerProfile.cost_declared: bool = False`, set True on `mock`, exactly as
  requested.
* `swarm_api.outcomes.DECLARED_COST_PROFILES` is derived from the catalogue;
  the module names no profile. `tests/unit/control_plane/
  test_outcomes_end_cause.py` holds both, and that the source names none.
* NOT served on `/v1/runtimes`, and NOT added to the UI's `RunnerProfile`: the
  UI reads the declared set from `GET /v1/outcomes` (`groups.rows[].declared_cost`
  and `totals.cost.declared.profiles`), so it has no copy to follow. The
  catalogue mirror in `types.ts` is not field-for-field (it carries neither
  `secrets_any_of` nor `supports_checkpoint`), so the "would gain a field to
  follow" below did not arise.

The request as it was filed follows, unchanged.

### What is true today

The Timeline marks cost that a runner **declares**, rather than cost measured
from a provider bill: `mock` reports $0.00 on purpose. That lets a reader tell a
deliberate zero from a real one. The catalogue has no field for it, so
`swarm_api.outcomes.DECLARED_COST_PROFILES = frozenset({"mock"})` names it, and
`test_outcomes_classifier_parity.py` holds every entry to a real profile.

### The requested change

Add `cost_declared: bool = False` to `RunnerProfile`, and set it True on
`mock`. `DECLARED_COST_PROFILES` would then be derived from the catalogue.

### What it would break if accepted

* **Nothing stored.** No document carries the name.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) mirrors `RunnerProfile` field for field. The
  parity check would require the new field there as well.

### If it is declined

The single named set stays, held to the catalogue by its test.

---

## 25. `profiles.py`: a runner profile cannot declare the inputs a caller may send it, so the bridge names the mock's by profile

**Status: ACCEPTED by the owner on 2026-09-25 and applied the same day** (branch
`lane/mock-inputs-contract`). This edits `apps/common/swarm_common/`, which is
frozen, and is recorded here as such. The owner's comment on #142
([issuecomment-5840939054](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/142#issuecomment-5840939054)):
"contract request 25 is accepted: `RunnerProfile.inputs` goes in the frozen
catalogue and the API enforces it for every caller, not only the bridge." What
was applied, and the one place it departs from the text below, is under
*Applied* at the end of this entry. **That departure is an amendment the owner
has NOT approved**: the field is typed `Mapping | None`, not the `Mapping` the
request asked for, and for the two profiles left `None` the API does not
enforce a declaration for every caller. It is recorded under *Amendment
awaiting the owner* below, and the decision is
[#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218). A
second change awaits the owner's confirmation too: the bounded park is counted
by the agent's `attempt_count`, not by the state-file counter the acceptance
named, because a failed checkpoint lost that one. It is under *Fixed after the
second review of #213*, at the end.

Recorded 2026-09-25 by the plugin lane that delivered #142 (branch
`lane/plugin-cli-0.5.2`). Numbered 25 because #196 (the outcomes API) takes 23
and 24.

### What is true today

A runner reads its agent's `input`. The mock reads `sleep_seconds`, `steps`,
`fail`, `artifact_text` and more (`agent_worker/runners/mock.py`); the CLI
runners read `input.prompt` and `input.model` (`runners/cliagent.py`). The
frozen catalogue says nothing about which keys a profile reads, or which of
them a caller may set.

#142 asked for the plugin to send the mock's test knobs, so a mock step can be
caught RUNNING and cancelled, or fail or park on purpose (the park is withheld;
see below). Invariant 10 is not
at stake -- these are data, never an image, a command, a resource spec or a
backend -- but an input means something only to the runner that reads it:
`input.model` would select the model a `claude-code` agent runs, which is the
contract change `tests/unit/mcp/test_model_flag_is_attribution_only.py` exists
to stop. So the gate has to be per profile.

With nothing in the catalogue to read, the bridge gates on the profile NAME,
in one place: `DECLARED_INPUTS` in `apps/swarm-mcp/swarm_mcp/profiles.py`,
which declares eight keys for `mock` and nothing for any other profile.
`tests/unit/mcp/test_runner_inputs.py` holds every name there to
`RUNNER_PROFILES` and every key to a `payload` read in the runner's source,
which it finds from the profile's own `runner_argv`.

Two of #142's asks are **not** declared, and why is what a catalogue field
would have to carry too. `quota_exhausted` (and `retry_after_seconds`, which
only shapes it) would let a caller start something they cannot stop: the mock
raises its rate limit on every attempt, with no count, and a park does not
spend an attempt, so the agent parks and resumes until it is cancelled. A
bounded park needs a counter in the mock (a `quota_exhausted_times`, kept in
its state file the way `credential_revoked_times` keeps its own), which is a
worker change. And `exit_code` is declared with values refused inside its
range -- 0, 77, 78 and 143 -- because the worker reads those as a success, a
rate limit, a refused credential and a cancellation; a failure on purpose that
exits with one is not a failure. The API does not check
input keys at all -- `validate_input_size` bounds the size -- so a caller that
does not go through the bridge can still send anything.

### The requested change

Add to `profiles.py`:

```python
@dataclass(frozen=True)
class RunnerInput:
    kind: str                     # number | integer | boolean | string | filename
    minimum: float | None = None
    maximum: float | None = None
    means: str = ""
    #: Values inside the bounds that are refused anyway, each with what the
    #: platform would read it as (the mock's exit codes 77, 78 and 143).
    refused: tuple[tuple[Any, str], ...] = ()


@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: The keys of `input`, besides `prompt`, a caller may set for this
    #: profile. Empty for every profile that takes only a prompt.
    inputs: Mapping[str, RunnerInput] = field(default_factory=dict)
```

and give `mock` the eight entries the bridge declares today. The bridge would
then read `profile.inputs` and delete its table, and the API could refuse an
undeclared key at submission, for every caller, with the same rule.

### What it would break if accepted

* **Nothing stored.** No document carries the field; agents keep their `input`.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) would gain a field to follow, or state that it
  does not.
* **An API that refuses undeclared keys** would refuse a caller that sends one
  today and has it ignored. That is the point, and it is a behaviour change to
  announce, not to slip in.

### If it is declined

The bridge's table stays, gated on the profile name, with its two parity tests.
Anything that is not the bridge -- the web UI's New Workflow screen, a script
posting to `/v1/tasks` -- keeps sending whatever `input` it likes, and a new
profile that should take inputs takes none from the plugin until someone edits
the table.

### Applied, 2026-09-25

Accepted by the owner on #142 (the comment quoted under *Status*), together
with "a bounded park for the mock (a `quota_exhausted_times` counter in its
state file, like `credential_revoked_times`), after which `quota_exhausted` and
a bounded `retry_after_seconds` join the declared inputs."

**In `swarm_common/profiles.py`**, as requested: `RunnerInput` (kind, bounds,
`means`, `refused`) and `RunnerProfile.inputs`. Beside them, so that the rule
has one home as well as the data: `INPUT_KINDS`, `InputRefused` (carrying the
key, every refused key and the bound) and `check_inputs(profile, raw)`, which
swarm-api and the bridge both call. `RunnerInput.check` refuses NaN and
Infinity, which `json.loads` reads and which every bound comparison lets
through. `RunnerProfile.inputs` is frozen into a read-only mapping and excluded
from the hash; `prompt` cannot be declared, because every profile takes it.

**The mock declares ten keys**: the eight the bridge declared, plus
`quota_exhausted` (boolean) and `retry_after_seconds` (integer 1..3600).
`exit_code` stays 1..255 except 77, 78 and 143. Still not declared: `spend`,
`provider`, `credential_revoked_times`, `credential_detail`, `quota_detail`,
`reset_at`. `agent_worker/runners/mock.py` parks one attempt: as first
applied, it recorded `quota_exhausted_times` and the parking attempt's id in
`mock_state.json` before it raised, the park's checkpoint carried the file
forward, and the next attempt ran. A retry in place is the same attempt and is
refused again. The count has since moved to the agent's `attempt_count`,
because a failed checkpoint lost it; see *Fixed after the second review*
below.

**`claude-code` and `codex` declare nothing**, so `input.model` is refused from
every caller, which is the attribution-only rule
`test_model_flag_is_attribution_only.py` holds for the bridge. (Since #226,
2026-09-26, the CLI runners do not read `input.model` either: the model is the
profile's Job's `MODEL`, set once in Terraform, and the worker drops a stored
`input.model` with a WARNING, which covers `browser` and `generic` too.)

**The one departure: `browser` and `generic` are `inputs=None`, NOT DECLARED
YET, and the API bounds them by size alone, as it bounded every profile
before.** The text above says "Empty for every profile that takes only a
prompt", and neither does: the browser runner refuses an input with neither
`url` nor `actions`, and the generic runner cannot start without `command`,
the name of an entry in its own catalogue. Declaring nothing for them would
have refused every agent they run, the smoke suite's GKE row included. The
"What it would break" section above assumed an undeclared key is "ignored"
today; for these two runners it is the work. So which keys they declare, with
which bounds, is open, and it is the owner's to decide
([#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218)):

* `browser` reads `url`, `actions` (a list of objects, which `INPUT_KINDS` has
  no kind for), `timeout_ms`, `launch_timeout_ms`, `viewport_width`,
  `viewport_height`, `user_agent`, `extract_text` and `screenshot`;
* `generic` reads `command`, `paths` (a list), `target`, `working_directory`
  and the four limits in `runners/limits.py`. A declared input named `command`
  would read as invariant 10 relaxed although it names a catalogue entry, not
  an argv.

**swarm-api** refuses an undeclared key, or a declared key out of its bounds,
with 422 `invalid_input` at `POST /v1/tasks`, the batch and every workflow
step, before anything is created (`validation.validate_runner_input`). The
bridge's `DECLARED_INPUTS` and `InputSpec` are deleted; it reads the catalogue.

**Downstream, changed in the same PR because each became a restatement the
API now enforces:** the operational scripts sent `message`, `index` and
`run_id`, which no runner read, and now send the prompt; the Submit form
offered `model` to the CLI profiles and `timeout_seconds` to every profile,
and now offers neither to a declared profile. Section 13 of
`scripts/lib/check-contract-parity.sh` holds both to the declaration. The UI's
catalogue mirror (`apps/swarm-ui/src/types.ts`) does not follow the field:
nothing serves it to the browser yet, which the Submit form's comment records
as a request.

### Amendment awaiting the owner: `inputs` may be `None`

**Not approved.** Recorded 2026-09-25 after the review of #213, which found
it applied without being named as a change to what the owner accepted.

| | as accepted | as applied |
|---|---|---|
| the type | `inputs: Mapping[str, RunnerInput] = field(default_factory=dict)` | `inputs: Mapping[str, RunnerInput] \| None = field(default_factory=dict, hash=False)` |
| what it can mean | a declaration: some keys, or none ("Empty for every profile that takes only a prompt") | three things: some keys; none, so the prompt only (`claude-code`, `codex`); or **not declared yet** (`None`: `browser`, `generic`) |
| what the API enforces | the declaration, for every caller | the declaration, for every caller, where there is one; for `None`, the input's size alone |

**What `None` does, and where that is decided.** `swarm_common.profiles.
check_inputs(profile, raw)` is the one place: for `None` it hands `raw` back
unchecked, and `validate_input_size`, which every submission runs first,
bounds it. `validate_runner_input` asks that function for every profile and
keeps no branch of its own. Before the review it did: it returned early for
`None` while `check_inputs` refused every key, so the one rule and the API
answered the same question oppositely.
`tests/unit/control_plane/test_runner_inputs_by_declaration.py` now holds the
API's answer equal to the rule's for every available profile.

**The bridge sends a `None` profile nothing.** That is the bridge's own send
policy, not a copy of the rule: it sends a key only when a declaration names
it, and it types `--input` by the declared kind. Letting it send a browser
agent's `actions` is #218's third question.

**What would retire the amendment**, whichever way #218 goes: declare
`browser` and `generic` (which needs a list kind, and a decision on a key
named `command`), and put the field back to `Mapping` with no `None`; or
approve `None` as it stands, and record that approval here with the owner's
comment.

### Fixed after the second review of #213, 2026-09-25

Four engineering defects the review found, fixed in the same PR. None of them
decides what `browser` or `generic` declare; that is still #218.

**A declared number has both bounds, inside the range Firestore stores.**
`steps`, `sleep_seconds` and `cpu_burn_seconds` were declared with a floor
only, and Python reads a JSON integer at any length, so `steps: 10**30` passed
every check and failed when the agent was written: a 500 at the store, not a
422 at the door. `RunnerInput.__post_init__` now refuses a `number` or
`integer` declared without both `minimum` and `maximum`, and an `integer`
whose bounds leave the signed 64-bit range. That range is two new names in
`profiles.py`, `INT64_MIN` and `INT64_MAX`, which swarm-api imports rather
than restates. The field types are unchanged from what was accepted; what
changed is which values of them the catalogue will hold. The mock's ceilings:
`sleep_seconds` and `cpu_burn_seconds` one hour (past the mock's 600 s timeout
on purpose, so the timeout path can be exercised), `steps` a thousand (each
step is a file every later checkpoint carries). A refusal repeats at most
forty characters of the value it refuses. swarm-api's `validate_storable`
also refuses, with 422 and the path, an integer anywhere in an input or a
task's metadata that Firestore could not encode, which is the only number
check a profile not declared yet gets.

**Where the bounded park's count lives. A CHANGE TO WHAT THE OWNER WROTE, NOT
YET CONFIRMED BY THE OWNER.** The acceptance above reads "a bounded park for
the mock (a `quota_exhausted_times` counter in its state file, like
`credential_revoked_times`)". Applied that way, the count reached the next
attempt only through the park's checkpoint, and `Worker._checkpoint` logs a
failed upload and parks anyway, as it must for a real provider. So a park
whose checkpoint failed was followed by another, for as long as uploads kept
failing: the bound held only while checkpoints did. The count is now the
agent's `attempt_count`, which admission increments in the lease's own
transaction. The lifecycle writes it into every runner's input beside
`attempt_id`, assigned rather than defaulted so no caller can set it, and the
mock parks while it is 1: the agent's first attempt, and no other. A mock
started without it refuses to simulate the park and fails, which spends an
attempt that `max_attempts` bounds. The bound itself, one park, is the
owner's and is unchanged; so is the declaration's own wording, "park the first
attempt". The other option the review named, failing retryably when the
park's checkpoint fails, was not taken: it would change the park path for
every runner, and a real provider that is refusing is still refusing on the
retry. `tests/unit/worker/test_mock_bounded_park.py::test_the_bound_holds_when_the_parks_checkpoint_fails`
makes every checkpoint of the parking attempt fail and requires the next
attempt to finish.

**The prose copies of the bounds are generated.** `docs/workflows.md` and
`plugin/README.md` each carry a table of the mock's inputs between
`runner-inputs:mock` markers, rendered from `RunnerInput.describe()` and
`means`. `tests/unit/mcp/test_runner_input_prose.py` holds both equal to the
catalogue, fails when a sentence in either, or in the delegate skill, states a
number about a declared numeric key outside the table, and requires every
example input to pass `check_inputs`.

**The Submit form no longer offers `browser` a key its runner never reads.**
`timeout_seconds` is read through `runners/limits.py` by generic and the CLI
runners; the browser runner starts no child through it.
`tests/unit/control_plane/test_submit_offers_only_what_runners_read.py` holds
every offer to a profile not declared yet to a `payload` read in its runner.

---

## 26. `models.py`: the attempt's CPU figures carry no time and their limit no source

**Status: ACCEPTED — accepted by the owner on #184, 2026-09-26, and applied in
PR #229.** The decision, in the owner's words
([#184, 2026-09-26](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/184#issuecomment-5841716442)):
"Contract request #26 is accepted: typed `cpu_measured_at` and
`cpu_limit_source` on Attempt." Recorded 2026-09-25 by the #184 follow-up
lane (PR #210), which applied request #15. What was applied is under
[Applied](#applied-2026-09-26-pr-229) at the end of this entry.

### What is true today

Request #15's four fields are on `Attempt`, and the worker rewrites them with
each periodic reading while a runner runs. So on a running attempt they are
a live reading. The interim path they replaced carried two more facts, and
the accepted fields carry neither:

* **When the figures were measured.** The HEARTBEAT reading had its event's
  `at`, and `include=usage` served `measured_at` and `age_seconds`. Details
  drew `latest heartbeat 20s ago`, aged on the server's clock (the #187
  review's fix). Now it can only say `live reading · age not recorded`. A
  worker that has stopped writing looks exactly like one that wrote a second
  ago. The drawer's liveness badge is the only thing that says otherwise, and
  it reads a different document.
* **Where the limit came from.** `cpu_limit_source` said `cgroup` (the
  container's `cpu.max`) or `resource_class` (the catalogue cpu of the class
  it was sized with). Details now names the class when the limit equals that
  class's cpu, and otherwise says `reported limit`. Nobody has read what Cloud
  Run's `cpu.max` holds (#188, "not verified"), so this is not academic.

### The requested change

Add to `Attempt`, beside the four:

```python
cpu_measured_at: datetime | None = None   # when the four were last written
cpu_limit_source: str | None = None       # "cgroup" | "resource_class"
```

Both optional and None by default, so nothing migrates. `cpu_measured_at` is
the worker's clock at the reading, and the API would serve an age computed
against its own `read_at`, as #188 did.

### What it would break if accepted

Nothing stored. `control.record_cpu_usage` would write two more keys,
`codec` would read and serve them, `AttemptRow` would declare them, and
Details would draw the age again. A string for the source is a vocabulary
that would want a home (compare request #20).

### If it is declined

Details keeps `age not recorded` and `reported limit`. A stalled worker's CPU
figures are not dated, and a limit is never attributed to the kernel.

### Applied, 2026-09-26 (PR #229)

* **The contract.** `Attempt` gains `cpu_measured_at: datetime | None = None`
  and `cpu_limit_source: str | None = None`, at the end of the class, after
  request #15's four. Nothing else in `apps/common/swarm_common` changed.
* **The worker** writes them with the four. `metrics.attempt_cpu_fields`
  takes the limit's source (`CPU_LIMIT_SOURCES`: `cgroup` or
  `resource_class`) and puts it beside a limit only. `control.record_cpu_usage`
  stamps `cpu_measured_at` with the worker's clock on every write that carries
  a figure, and never writes a time, a limit or a source with no figure beside
  it. The periodic reading (every fifth heartbeat) is now written even when the
  figures did not move, because its time is what says the worker is still
  reading; the reap and exit writes still skip an unchanged reading.
* **The API** decodes and serves both on every attempt row, and serves
  `cpu_reading_age_seconds`, the reading's age against the route's own
  `read_at` (both attempt routes now carry `read_at`), clamped at zero. It is
  declared computed in `test_api_contract_shapes.py`.
* **The UI** restates the two fields and the age on `AttemptRow`, optional.
  Details' CPU strip says `live reading · 20s ago` (and `last written · 3m
  ago` for an attempt that ended with no recorded finish), and the ceiling's
  source is `cgroup limit`, the class's name, or `class limit`, from the typed
  source. `test_ui_api_field_contract.py` and
  `test_artifacts_tab_field_contract.py` hold the fields both ways.
* **Attempts from before the change** carry neither field. They keep the
  legacy words, `age not recorded` and `reported limit`, and the legacy
  heartbeat reader stays, as the owner decided on #184.
* The source is a bare string, as this entry said it would be. Its vocabulary
  is stated once in the worker (`metrics.CPU_LIMIT_SOURCES`) and read by the
  UI's `limitSource`; compare request #20.


## 27. `identity.py`: `_slug`'s docstring still sizes tenant ids for the `swarm-t-` prefix that no longer exists

**Status: ACCEPTED, accepted by the owner 2026-09-28 on #245 and applied in
PR #245.** Recorded 2026-09-28 by the #176 lane.

### What is true today

`apps/common/swarm_common/identity.py`, `_slug`'s docstring, LENGTH bullet:
"A long group name yields an id no GCP service account can be named for,
because `swarm-t-<id>` must fit in 30 characters." The code under it is right:
`_MAX_TENANT_ID` is computed from `_GSA_PREFIX = "swarm-agent-worker-"`, and the
comment on `_GSA_PREFIX` itself says the `swarm-t-` prefix "no longer exists".
So the frozen module contradicts itself about which identity the cap is for.

That contradiction is how #176 survived: the quota broker's comment said
register-tenant.sh writes `swarm-t-<tenant>`, and accepted it as a worker
identity, long after nothing created one. The broker no longer accepts it; this
docstring is the one live statement left in the repository that `swarm-t-<id>`
is the name a tenant's worker must fit.

### The requested change

Docstring only: `swarm-t-<id>` becomes `swarm-agent-worker-<id>` in that
sentence. No code, no value, no type.

### What it would break if accepted

Nothing. No code reads a docstring, and `check-contract-parity.sh` compares
`_GSA_PREFIX`, not prose.

### If it is declined

The frozen module keeps telling its reader that tenant ids are sized for an
identity nothing creates, and the next restatement copied from that sentence
reintroduces #176.

### Applied, 2026-09-28 (PR #245)

`_slug`'s LENGTH docstring bullet now reads `swarm-agent-worker-<id>` in place
of `swarm-t-<id>`, matching `_GSA_PREFIX` and the comment already on it.


## 28. `profiles.py`: claude-code and codex declare an `issue` runner input

**Status: ACCEPTED, accepted by the owner 2026-09-28** (recorded on #265), and
applied by the #265 change the same day (see "Applied" below). Recorded
2026-09-28 from #265. If another branch has taken 28 by the time this merges,
renumber this one.

### What is true today

A runner profile declares the inputs a caller may send (#213, contract
request 25). `claude-code` and `codex` declare none, so a caller can only put
an issue's text into the prompt itself. Every brief in the 2026-09-27/28 wave
restated its issue by hand, and #247's brief lost the two screens its issue
named as the reproduction.

### The requested change

`claude-code` and `codex` declare one input, `issue`: a positive integer
naming an issue in the step's own `repo`. No other key is added and no type
changes. The worker fetches that issue's title, body and comments read-only
with the tenant's forge credential, writes them to the workspace as
`issue.md`, and names the file in the prompt. The fetch is the worker's; the
declared input is only the number.

### What it would break if accepted

Nothing that exists: `mock` is the only profile that declares inputs today
(#213), and neither `claude-code` nor `codex` does, so every current
submission stays valid. The API's declared-inputs check (#213) starts
accepting `issue` for these two profiles and still refuses everything else.

### Invariants

- **Invariant 10.** The number is data, and the issue text the worker fetches is
  data for the agent, never an image, a command, a resource spec or a backend
  parameter.
- **#219.** The forge token never reaches the workspace or the agent.
- **Invariant 9.** The fetch uses the step's own tenant's credential against the
  step's own repository.

### Applied, 2026-09-28 (#265)

Exactly the requested change, and nothing else in `apps/common/swarm_common/`:
`profiles.py` gives `claude-code` and `codex` one declared input, `issue`, a
`RunnerInput("integer", minimum=1, maximum=999_999)`. No other key, type or
profile changed.

- **The ceiling.** The request says "a positive integer" and a declared integer
  needs both bounds (#213). 999999 is the largest bound `RunnerInput.describe()`
  prints exactly: it formats six significant digits, so `2**31 - 1` would be
  served as "2.14748e+09", a bound the check does not apply.
- **The API** (`validation.validate_runner_input`) accepts `issue` for these
  two profiles through the declaration, as it does every declared key, and
  refuses it with 422 `invalid_input` on a submission with no
  `repository_url`: the issue is the agent's repository's, and there would be
  nothing to fetch it from.
- **The worker** (`agent_worker/issue.py`) fetches the issue and its comments
  read-only after the credentials step, writes `work/issue.md`, and fails the
  attempt `INPUTS_UNAVAILABLE` before the agent starts when it cannot. The
  runner names the file in the prompt (`runners/cliagent.py`).
- **Invariant 10.** The number is data, and so is the text: nothing in the
  worker reads the issue for anything it does.
- **#219.** The token is read by `Worker._git_token`, the clone's path, and
  goes only into the request's `Authorization` header; a redirect to another
  host is refused rather than followed with it.
- **Invariant 9.** The fetch is against the agent's own repository with its own
  tenant's credential.

## 29. `models.py`: `EndCause` gains `PUBLISH_REFUSED`

**Status: APPLIED 2026-10-02** (accepted by the owner 2026-09-29, recorded
on #259; applied by functionality wave 3, lane B46, exactly as requested
below). Recorded 2026-09-29 from #259.

**As applied.** `EndCause.PUBLISH_REFUSED = "publish_refused"`, appended
after `VERDICT_FAILED` so no existing value moved. The worker writes it from
both call sites, `_fail_for_final_tree_leak` and `_fail_for_refused_title`;
both stay retryable, so the cause is the agent's once its attempts are spent.
`swarm_api/outcomes.py` gains the failure class `publish_refused` ("publish
refused"), and `DERIVE_VERSION` moves to 5 so stored days are derived again.
The UI's `FailureClassKey`, its ledger fixture and a new `END_CAUSES` mirror
in `types.ts` gain it too. Section 5 of `scripts/lib/check-contract-parity.sh`
now holds that mirror to the enum.

**Not covered by this cause: the forge refusing a pull request.** The
definition below is "the worker refused to publish", and a pull request the
forge refuses (GitHub's "No commits between") is not the worker refusing.
That case, and a pull-request step that changed nothing, are written as
`OUTPUTS_MISSING` meanwhile. Their own cause is request 46.

### What is true today

The worker refuses to publish in two ways that fail the attempt retryably
(#259): the branch it would push adds a credential ("the final tree adds a
credential in <file>; remove it"), or the agent's `pr-title.txt` is present
and unusable (a task id, or attribution; since 2026-09-29 a mention is
neutralised with a zero-width joiner, never refused). Neither has an end cause
of its own. `EndCause` is in the frozen `swarm_common`, so the worker writes
`RUNNER_ERROR` for the first and `OUTPUTS_MISSING` for the second
(`agent_worker.lifecycle._fail_for_final_tree_leak`,
`_fail_for_refused_title`). The outcome ledger then counts a platform refusal
as the runner's error or as a missing output, and neither is what happened.

### The requested change

One new member, `EndCause.PUBLISH_REFUSED = "publish_refused"`: the worker
refused to publish -- a credential in the final tree, or an unusable
`pr-title.txt`. The worker writes it from both call sites above. swarm-api's
outcome classes (`swarm_api/outcomes.py`) and the UI's fixture gain the
class with it.

### What it would break if accepted

Nothing that exists: a new enum value. A reader that does not know it falls
back to its text classifier, as for any agent written before `end_cause`
existed. The outcome ledger's cause-to-class map and the UI's class list
must add it in the same change, or those agents read as unclassified.

### Invariants

- **Secrets.** The cause names the refusal, never the value; the attempt's
  error names the file, never its content.
- **Invariant 1.** A refused attempt that goes back to READY holds no
  capacity, exactly as any retryable failure.
---

## 30. `identity.py`: a tenant may list service accounts that resolve to it by exact email

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
Decided in principle by the owner on 2026-09-28 for #273 (the red-CI fixer);
this entry went through three rounds of security re-review on 2026-09-29 --
the WIF/deployer-IAM correction, the default-deny scope and IAP-subject
correction, and the required-keyword `submitted_by` correction, each recorded
in its own addendum below -- before the owner accepted it as it now reads.
Nothing here is applied YET: `identity.py`, `auth.py`, `settings.py`,
`variables.tf` and `locals.tf` are unchanged by the pull request that adds
this entry; implementation is tracked separately (`part of #273`). Recorded
2026-09-29. Numbered 30 because 27 and 29 are taken on open branches (29
twice); if another branch has taken 30 by the time this merges, renumber this
one.

**2026-09-29, after a security review: accept with changes.** The owner made
four decisions, folded into this entry below and marked where they land:
the account stays in `saga-agents-staging` as an accepted risk (**Who can
mint the account's token**, below); a listed account's rights are narrowed
to continuation-only (**5. Scope: continuation-only rights**); the deployer
loses `roles/iam.workloadIdentityPoolAdmin` outright (#334) and, since an IAM
condition cannot scope `roles/iam.serviceAccountAdmin` at all (IAM resources
expose no `resource.name` to a condition), instead loses that role
project-wide in favour of project-wide `roles/iam.serviceAccountCreator` plus
a per-account `serviceAccountAdmin` grant on each account `terraform/infra`
manages, excluding `swarm-ci-fix` and `swarm-tf-deployer` (end of **Who can
mint the account's token**, corrected 2026-09-29 after PR #334); and the
reviewer's minors are folded into the diffs below. Status stays
**PROPOSED** — this is
what re-review checks against, not an acceptance.

**2026-09-29, second re-review pass: "NOT YET," three more corrections.**
(1) The per-route scope list in item 5 was incomplete -- it missed
`routes/accounts.py` (8 routes, some writing), `POST
/v1/tenants/me/credentials`, `GET /v1/attempts`, `GET /v1/outcomes`, and the
checkpoint-content routes, which took no `AuthContext` at all and so had no
seam for the old design to attach to. Item 5 is now a DEFAULT-DENY check
inside `current_auth` against an explicit `CONTINUATION_ROUTES` allow-list
(the `POOL_ADMIN_ROUTES` pattern), and the `submitted_by` filter moved into
`Store.get_task`/`Store.list_tasks` themselves rather than being restated at
each read route; `/v1/stats`, `/v1/capacity`, `/v1/providers` and
`/v1/tenants/me` are now explicitly classified out, `/v1/resource-classes`
and `/v1/runtimes` explicitly in. (2) The `uid` pin (item 3/4) compared
`principal.subject` against Terraform's bare unique id, but IAP -- which
`ci-fix.yml` actually calls through -- prefixes `sub` with
`"accounts.google.com:"`; item 4 now strips that prefix once, for both
paths, before `Principal` is built, and notes that `email_verified`
defaults to `True` on the IAP path regardless, so decision 4's "explicit
True" check is a bearer-path protection only. (3) The stolen-token reach
(**Isolation analysis**, "What an attacker who steals its token gets") is
restated: `continues_task` carries an ARBITRARY, caller-chosen prompt, not a
constrained fix, and the account can read back what that prompt produced --
an exfiltration path, not merely an unwanted commit -- repeatable without
limit, because the owner declined a continuation cap (decision 3,
2026-09-29). Recorded as an accepted reach, not a mitigated one.

**2026-09-29, round 3: "NOT YET" on one mechanical point, folded in without
another owner round.** The `submitted_by` filter (item 5) was still OPT-IN:
`get_task`, `list_tasks`, `get_workflow` and `list_workflows` defaulted
`submitted_by=None`, so a route that reaches an agent through a layer round 2
did not personally thread the parameter through -- the artifact service,
`ctx.inspection`, the transcript or answer service, or one not yet written --
would keep compiling and keep serving, silently unfiltered. `submitted_by` is
now a REQUIRED keyword with no default on all four store methods: a missed
call site is a `TypeError` at the call and a type-check error before that,
not a silent pass-through. `test_continuation_scope_is_narrow.py` is extended
to drive every task/workflow-scoped route in `CONTINUATION_ROUTES` at an agent
another `eng` member submitted and assert 404, so a route added to the
allow-list later without its filter wired through fails on the day it is
added.

**2026-09-29, implementation correction: the "is not a task in your tenant"
wording throughout item 5 and its tests was wrong.** Checked directly against
`apps/swarm-api/swarm_api/store.py` on `main` while implementing this entry
(`part of #273`): `Store.get_task` today raises `NotFound(f"task {task_id!r}
not found")` for BOTH a genuinely missing agent and another tenant's agent --
never "is not a task in your tenant". That phrase belongs to a DIFFERENT code
path: #273's own `resolve_continuation` wraps its `continues_task` lookup in
a `DispatchOptionError` carrying that exact sentence, but it never touches
`Store.get_task`'s own message. Every place below that quoted "is not a task
in your tenant" as `Store.get_task`'s (and therefore the new `submitted_by`
filter's) refusal text has been corrected to the words the store actually
uses, "task 'X' not found" -- the invariant this entry cares about (SAME
words for "not yours" as for "not found" and "cross-tenant", so the filter is
not a new enumeration oracle) is unaffected; only the literal quoted string
was wrong. Every place describing #273's own `continues_task` message is
unchanged and was already correct.

**2026-10-06, owner decision (#754): the fixer's reach widened from "any
`direct-pr` task" to an `integrate` workflow's integrator as well.** Every
SwarmCloud lane pull request is opened by an integrator -- the workflow's
final step, which pushes `<prefix><its own task id>` and opens the one pull
request `integrate` produces -- so a fixer confined to `direct-pr` agents fixed
none of them: PR #740's first real run was refused by `resolve_continuation`.
`_has_own_pull_request` in `apps/swarm-api/swarm_api/continuation.py` now
admits both, for a member and for a continuation-scoped account alike. What did
NOT widen: the integrator is told apart by the `role` the API itself wrote into
the agent's dispatch block (a reserved key no caller can write), never by
anything the request carries; it must be in the caller's own tenant; an
`integrate` contributor is still refused (its branch has no pull request of its
own); a chain of continuations is checked again at its root; and a merge-only
continuation is still a member's power, never this account's. So a stolen
token's reach in **Isolation analysis (invariant 9)** below reads "any `eng`
`direct-pr` task **or integrator** that still exists": every open lane pull
request of the tenant as well as every `direct-pr` one, and still no merge, no
new work, no read of what it did not submit, and no other tenant. Proved by
`tests/unit/control_plane/test_continuation_scope_is_narrow.py`
(`test_the_listed_account_can_continue_an_integrate_workflows_integrator`, and
the contributor, chain-root and merge-only refusals beside it).
[ci.md](ci.md), "What the CI fixer may continue", states the same rule from
the operator's side.

### What is true today

`.github/workflows/ci-fix.yml` (#273) federates as
`swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com` and submits a
one-step `claude-code` workflow with `continues_task` set to the red swarm
agent. `resolve_continuation` (`apps/swarm-api/swarm_api/continuation.py` on
#273) looks the task up with `store.get_task(tenant_id, requested)` and answers
"is not a task in your tenant" for another tenant's agent exactly as for a
missing one. The `swarm/` pull requests belong to `eng`, which resolves from
the Google group `eng@saga.xyz` (`terraform/environments/dev/dev.tfvars`,
`tenants.eng`, `directory_group = true`).

So the fixer's account has to resolve to `eng`, and today there are exactly
two ways a caller does:

- **Group membership.** `Authenticator._from_claims` asks Cloud Identity
  `groups_for(email, tenant_groups)`, puts the answers in `Principal.groups`,
  and the frozen `resolve_tenant` returns the first registered group the
  caller is in. Adding the account to `eng@saga.xyz` would work, and the owner
  rejected it: that group is a company group, and every grant it holds
  anywhere in saga.xyz -- Drive, other GCP projects, other applications that
  authorise by it -- would go to a bot whose only job is to comment on pull
  requests and submit fix steps.
- **Being the tenant's principal.** A `kind = "user"` tenant whose principal is
  the account's own email (as `u-sw-c90291` is for `swarm-verify`). That is a
  tenant of its own, not `eng`, so it owns none of the agents it must continue.

Without either, the account is also refused before tenant resolution: its
domain is `saga-agents-staging.iam.gserviceaccount.com`, not `saga.xyz`, so
`assert_allowed_domain` answers 403 unless it is in `ALLOWED_USERS`. And if it
were admitted through `ALLOWED_USERS`, `groups_for` would ask Cloud Identity
about a service-account address on every request, a lookup that can only
answer "no" and whose failure is fatal by design (503).

### The requested change

**1. Terraform: one new optional field per tenant.** In
`terraform/infra/variables.tf`, `var.tenants` gains

```hcl
    # Service accounts that resolve to THIS tenant besides its principal, by an
    # exact match on the email the caller's verified token carries. For a bot
    # that must act as the tenant without joining the tenant's group, which
    # would hand it every grant the group holds across the company (contract
    # request 30). Bare emails of user-managed accounts in this project; never
    # a human, never an admin, never under two tenants -- see the validations.
    service_accounts = optional(list(string), [])
```

and `dev.tfvars` would set, once #273's account exists:

```hcl
  eng = {
    ...
    service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }
```

**2. How it reaches swarm-api.** `terraform/infra/locals.tf` renders one new
environment variable on swarm-api beside `TENANT_GROUPS`. **Revised from the
first draft (reviewer minor): a JSON list of objects, not a JSON object keyed
by email**, because Python's `json.loads` on an object keyed by email would
silently keep the LAST of two duplicate keys rather than refuse them -- fine
for what Terraform renders today (its own for-expression already errors on a
duplicate key at plan time), not fine for `TENANT_SERVICE_ACCOUNTS` set by
hand, which is exactly the case `settings.py`'s own startup check exists for.
A list carries duplicates through unchanged, so the Python-side check
(below) is the one thing that can refuse them, not an accident of which
representation the object literal happens to pick:

```hcl
locals {
  # Every listed service account across every tenant, lower-cased and deduped
  # for the data source below -- a `for_each` key must be unique even though
  # the SAME email under two tenants is refused by validation, not by this.
  listed_service_accounts = toset(flatten([
    for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]
  ]))
}

# The account's unique id (`sub` on the token it presents), pinned beside its
# email so a service account deleted and recreated under the same address --
# GCP lets the account id be reused -- does not inherit the old one's tenant.
# Reviewer minor.
data "google_service_account" "listed" {
  for_each   = local.listed_service_accounts
  project    = var.project_id
  account_id = split("@", each.value)[0]
}

      # Every listed service account -> its tenant's kind and principal, and
      # its own unique id. A list, not an object: see above.
      TENANT_SERVICE_ACCOUNTS = jsonencode(flatten([
        for t, v in var.tenants : [
          for sa in v.service_accounts : {
            email     = lower(sa)
            kind      = v.kind
            principal = lower(v.principal)
            uid       = data.google_service_account.listed[lower(sa)].unique_id
          }
        ]
      ]))
```

JSON rather than `_csv`, because each entry carries four values now and a
delimiter scheme invented for it would be a second format to get wrong.
`directory_group` is deliberately not consulted: a listed account never needs
a directory lookup, so it resolves to a `kind = "group"` tenant whose group
cannot be read (as `smoke`'s cannot) exactly as to one whose group can.

`apps/swarm-api/swarm_api/settings.py` gains

```python
    #: Service accounts a tenant lists besides its principal (contract request
    #: 30), from TENANT_SERVICE_ACCOUNTS. Refused at startup, not at request
    #: time, when an entry is not a user-managed service account in THIS
    #: project, is listed more than once, sits under two tenants, is missing
    #: its unique id, or is also in admin_users, admin_pool_users or
    #: secret_admin_principals: a revision that would resolve one wrongly must
    #: not start serving.
    tenant_service_accounts: tuple[TenantMember, ...] = ()
```

parsed by a `_tenant_members("TENANT_SERVICE_ACCOUNTS")` helper that
`json.loads` the value as a **list** (empty or absent is `()`), and for each
entry: lower-cases `email`, `kind` and `principal`; requires `kind` in
`{"group", "user"}`, a non-empty `principal` and a non-empty `uid`; requires
`email` to match `identity.SERVICE_ACCOUNT_EMAIL` **and** to end with
`@{self.project_id}.iam.gserviceaccount.com` -- the second check is what a
bare regex on the shape cannot do, and is why a Google-managed service agent
(`service-<number>@gcp-sa-<api>.iam.gserviceaccount.com`) can never match: its
domain never ends with this project's id, however the regex is written
(reviewer minor). It then **refuses a duplicate email itself**, by building
the tuple through a plain loop that checks membership before appending rather
than relying on `dict`/`json` key semantics to catch it (the other half of the
reviewer minor above): two entries for the same email raise `ValueError`
whether or not they agree on tenant. It raises `ValueError` on any of the
refusals above, including the ones already caught at `terraform plan` --
belt and suspenders for a hand-set environment variable Terraform never saw.
A startup `ValueError` fails the new Cloud Run revision's readiness, so the
previous revision keeps serving.

**3. How `resolve_tenant` uses it.** The frozen change is the diff below:
a `TenantMember` type, a `SERVICE_ACCOUNT_EMAIL` expression, a
`tenant_member_for()` matcher, and a third, defaulted parameter on
`resolve_tenant`. The match is:

- **exact and case-insensitive on email, exact on the account's unique id**:
  `email.strip().lower() == member.email` (already lower-cased once, on
  `TenantMember`, not at every comparison -- reviewer minor) **and**
  `principal.subject == member.uid`. The second half is new: pinning the
  token's `sub` claim beside the listed email is what stops a service account
  that was deleted and recreated under the same address -- GCP allows the
  account id to be reused -- from silently inheriting the old account's
  tenant the moment its email is presented again. An email match with a
  mismatched `uid` is treated as no match at all, exactly like an unlisted
  account, and falls through to today's refusal (wrong domain, not in
  `ALLOWED_USERS`);
- **matched with `re.fullmatch`, not `re.match`** (reviewer minor: `match`
  with a trailing `$` in the pattern still accepts a string with a trailing
  newline, which `fullmatch` does not; a compiled anchor is not a substitute
  for asking the regex engine to consume the whole string);
- **only on a user-managed service-account address**
  (`<id>@<project>.iam.gserviceaccount.com`); a listed entry of any other
  shape never matches even if it got past Terraform and settings. The
  project-id pin that excludes a Google-managed service agent is enforced at
  startup in `settings.py`, not in this regex -- `identity.py` is frozen and
  has no notion of which project it is running in, so the regex alone would
  still accept `service-1234@gcp-sa-x.iam.gserviceaccount.com` under a
  project literally named `gcp-sa-x`; only `settings.py`'s `endswith` check
  closes that (reviewer minor, see item 2);
- **on the email and subject the token verified**: `_from_claims` passes the
  `email` and `sub` claims of a Google ID token that `verify_oauth2_token`
  accepted with `email_verified` not false, or of an IAP assertion
  `IapAssertionVerifier` accepted. Nothing the caller sends in a body, a
  header or a query is read;
- **before any group lookup**: a match returns the listing tenant's id,
  derived by `tenant_id_for_group(principal)` for `kind="group"` or
  `tenant_id_for_user(principal)` for `kind="user"`, from the tenant's own
  kind and principal, so the account and the group it stands beside cannot
  name two different tenants. An entry whose `kind` is neither now **raises**
  `AuthError` instead of silently falling through to the `user` branch
  (reviewer minor) -- `settings.py` already refuses that shape at startup, so
  reaching it here means the belt-and-suspenders check caught something the
  suspenders should already have caught, and that is worth surfacing as a 401
  rather than quietly guessing.

With the default `service_accounts=()` every existing caller of
`resolve_tenant` behaves exactly as today.

**4. `auth.py`, the non-frozen half.** **Corrected 2026-09-29: the `sub` claim
needs normalising before it reaches `tenant_member_for`, and the first draft
of this entry did not do that.** `_from_claims` reads it today at line 335:

```python
        subject = str(claims.get("sub", ""))
```

For a bearer ID token that is already the bare unique id Terraform's
`data.google_service_account.unique_id` renders. For an **IAP assertion it is
not**: `IapAssertionVerifier.verify` documents it in so many words --
*"IAP puts the verified identity in `email`, and `sub` carries a stable
`accounts.google.com:<id>` rather than a bare subject"* (`auth.py`, in the
class docstring). **`ci-fix.yml` calls swarm-api through IAP**, like every
other caller of this Cloud Run service that is not inside the VPC, so the
listed path's `uid` pin -- compared as `principal.subject == member.uid` in
`tenant_member_for` -- would never match the real account's own token as
first drafted: the claim it would compare carries a twelve-character prefix
Terraform's rendered `uid` never has. Fixed at the same line, before
`Principal` is built, since this is a fact about the claims format and not
about listing:

```python
        subject = str(claims.get("sub", ""))
        # IAP prefixes a stable "accounts.google.com:" (IapAssertionVerifier,
        # above); a bearer ID token's `sub` carries no such prefix. Stripped
        # here, once, for BOTH paths, so `principal.subject` is the bare id
        # either way and `tenant_member_for`'s `uid` comparison (item 3) does
        # not need to know which path produced it -- normalising per-path
        # inside the comparison would be the second copy of this rule,
        # findable only by reading both call sites at once.
        subject = subject.removeprefix("accounts.google.com:")
```

`Principal.subject` had no reader anywhere in the codebase before this
change (`grep -rn "\.subject\b" apps/swarm-api apps/common` -- none), so this
is safe to normalise unconditionally rather than only inside the new listed
path: nothing existing depended on the prefixed form.

Then, as the first thing after reading `email` and `subject`:

```python
        member = tenant_member_for(email, subject, self._settings.tenant_service_accounts)
        if member is not None:
            # Reviewer minor: the listed path requires an EXPLICIT True, not
            # merely "not False" -- the weaker rule the bearer path applies
            # everywhere else (GoogleTokenVerifier.verify). A listed account
            # skips both Cloud Identity passes below, so this claim is the
            # only outside confirmation of the identity left; treating an
            # absent claim as acceptable here would remove it.
            if claims.get("email_verified") is not True:
                raise AuthError("listed service account requires a verified email claim")
            principal = Principal(
                email=email,
                subject=subject,
                domain=email.rsplit("@", 1)[1],
                groups=(),
            )
            log.info("tenant member %s resolved by listing", email)
            return AuthContext(
                principal=principal,
                tenant_id=resolve_tenant(
                    principal,
                    self._settings.tenant_groups,
                    service_accounts=self._settings.tenant_service_accounts,
                ),
                is_admin=False,
                tenant_principal=member.principal,
                admin_unresolved=False,
                is_pool_admin=False,
                tenant_member=email,
                # Decision 2: a listed account's rights, below.
                member_scope="continuation",
            )
```

That is: the listing **admits** the account (it does not also go in
`ALLOWED_USERS`, so there is one place that says it may call), it **skips both
Cloud Identity passes** (no tenant-group lookup and no admin-group lookup, so a
directory outage cannot 503 it and it can never pick up admin through a group
it happens to be in), and it is **never an admin or a pool admin**. An
unlisted service account takes today's path unchanged.

**Said plainly, because it changes what the `email_verified is not True` check
(above) is actually worth for `ci-fix.yml`: on the IAP path that check is
inert.** `IapAssertionVerifier.verify` already does `claims.setdefault
("email_verified", True)` (`auth.py:211`) before `_from_claims` ever sees the
claims, because IAP "only ever forwards identities it has already
authenticated" -- so by the time the new listed-path check runs, the claim is
always `True` for an IAP caller, whatever IAP itself received. The check is
real and load-bearing on the **bearer** path, where `GoogleTokenVerifier`
applies the weaker "not False" rule and an absent claim would otherwise pass
through unchanged; on the **IAP** path it can never fire, because IAP has
already made the decision this check exists to double-check. Since
`ci-fix.yml` calls through IAP, decision 4's "listed path requires an
explicit True" is, for this account specifically, a bearer-path-only
protection that happens to also be written for a caller that never takes the
bearer path. It stays in the diff because a future listed account might.

**A second, non-frozen fix folded in here (reviewer minor: "a duplicate-listing
`AuthError` maps to 401/403, not 500").** `Authenticator._authenticate_bearer`
today calls `self._from_claims(claims)` unwrapped at its tail, unlike the IAP
branch of `authenticate()`, which already catches `AuthError` from
`_from_claims` and converts it to `Unauthenticated`. `main.py` registers a
global `@app.exception_handler(AuthError)` that maps an uncaught one to 401
today, so this is not observed as a live 500 -- but it is the one place in
`auth.py` that relies on that global handler instead of converting its own
error, which is exactly the asymmetry `tenant_member_for`'s new
"listed under more than one tenant" `AuthError` (a case this change
introduces) would go through on the bearer path. Made symmetric with the IAP
branch:

```python
        try:
            return self._from_claims(claims)
        except Forbidden:
            raise
        except AuthError as exc:
            raise Unauthenticated(str(exc)) from None
```

**5. Scope: continuation-only rights, DEFAULT-DENY (decision 2, 2026-09-29;
redesigned 2026-09-29 after re-review found the per-route list incomplete).**
A listed account's tenant membership is real -- it is `eng`, with `eng`'s GSA,
secrets, prefix and namespace -- but its RIGHTS within that tenant are not a
human member's. `AuthContext` gains

```python
    #: "" for an ordinary member (a human, or -- unlisted -- today's only
    #: kind of caller): every route behaves exactly as today. "continuation"
    #: for a listed service account (decision 2, 2026-09-29): it may submit a
    #: `continues_task` workflow and read what IT submitted, and nothing else.
    #: Not a kind of admin and not read by `require_admin` -- `is_admin` and
    #: `is_pool_admin` already answer that, independently, and stay False for
    #: every listed account regardless of this field.
    member_scope: str = ""
```

**What the first draft of this entry got wrong.** It named a list of routes
to gate: `create_task`, `create_task_batch`, `cancel_task`, `cancel_workflow`,
and a `submitted_by` filter on the task/workflow read routes. Re-review found
that list incomplete by construction -- it was built by reading `tasks.py` and
`workflows.py` and stopping, not by enumerating every router the app
includes. Missed, all of them reachable by a continuation-scoped caller under
that design because they depend on bare `current_auth` and were simply never
looked at:

- **`routes/accounts.py`, all eight routes** (`routes/accounts.py:157-369`):
  `GET /v1/accounts` (list, read-only). `POST /v1/accounts/authorize` only
  starts a sign-in (no write). The other six WRITE: `POST /v1/accounts`
  (register a credential), `POST /v1/accounts/exchange` (redeem a sign-in
  and register the account), `POST /v1/accounts/{id}/refresh` (rotate the
  credential now), `PUT /v1/accounts/{id}/lending`, `PUT
  /v1/accounts/{id}/state` (pause/drain), `DELETE /v1/accounts/{id}`. A
  stolen token could register a credential into `eng`'s pool, pause or
  delete one of `eng`'s accounts, or change who it lends to.
- **`POST /v1/tenants/me/credentials`** (`routes/tenants.py:54`): the one
  route in the service that accepts key material. A stolen token could
  overwrite `eng`'s provider API key with one the attacker controls.
- **`GET /v1/attempts` and `GET /v1/outcomes`**: both tenant-wide by design
  (`list_attempts`'s docstring: "Attempts across every TASK of the caller's
  tenant"), not agent-scoped, so neither goes through `Store.get_task` and
  neither can be narrowed by a `submitted_by` filter the way the task routes
  can -- there is no agent in the call to filter.
- **The checkpoint-content routes** (`routes/checkpoints.py:53-118`): `GET
  .../checkpoints/{checkpoint_id}/files`, `.../files/{path}`, `.../content`.
  These three don't even declare `auth: AuthContext` -- only `tenant_id: str =
  Depends(tenant_scope)` -- so there was **no seam** for a per-route
  `member_scope` check to attach to in the first draft's design at all; the
  gap was not a missed line, it was a missing parameter.

**The fix: default-deny in `current_auth` itself, not an opt-in gate each
route remembers to add.** `tenant_scope` already depends on `current_auth`
(every route above does, transitively, including the checkpoint ones -- that
part was never the gap), so putting the check there closes every route in one
place, present and future, the same way `admin_auth`/`POOL_ADMIN_ROUTES`
already gates the admin surface:

```python
# auth.py, beside POOL_ADMIN_ROUTES

#: Every route a CONTINUATION-SCOPED account (member_scope="continuation") may
#: call, as (HTTP method, route template) -- an ALLOW-LIST, same reasoning as
#: POOL_ADMIN_ROUTES: a route defaults to CLOSED for this scope until named
#: here, where opening it is a decision visible in review, rather than
#: defaulting OPEN until someone notices and closes it. This is what the first
#: draft's per-route list should have been from the start.
#: tests/unit/control_plane/test_continuation_scope_is_narrow.py holds this
#: set equal to the decided one and sweeps every route in every router
#: against it, the same shape as test_pool_admin_is_narrow.py.
CONTINUATION_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/workflows"),  # only WITH continues_task -- see item 4
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("GET", "/v1/tasks/{task_id}/attempts"),
    ("GET", "/v1/tasks/{task_id}/artifacts"),
    ("GET", "/v1/tasks/{task_id}/artifacts/content"),
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
    ("GET", "/v1/tasks/{task_id}/checkpoints"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    ("GET", "/v1/tasks/{task_id}/logs"),
    ("GET", "/v1/tasks/{task_id}/transcript"),
    ("GET", "/v1/tasks/{task_id}/answer"),
    ("GET", "/v1/tasks/{task_id}/input"),
    ("GET", "/v1/workflows"),
    ("GET", "/v1/workflows/{workflow_id}"),
    ("GET", "/v1/resource-classes"),  # static catalogue, no tenant data
    ("GET", "/v1/runtimes"),          # static catalogue, no tenant data
})


def require_continuation_route(
    ctx: AuthContext, route: tuple[str, str] | None
) -> AuthContext:
    """A continuation-scoped caller may reach only CONTINUATION_ROUTES.

    An ordinary member (`member_scope == ""`) returns immediately -- this
    changes nothing for anyone but a listed account. `route=None` (no route
    matched) fails closed, same as `require_admin`.
    """
    if not ctx.member_scope:
        return ctx
    if route is not None and route in CONTINUATION_ROUTES:
        return ctx
    method, path = route if route else ("?", "unmatched")
    raise Forbidden(f"a continuation-scoped account may not call {method} {path}")
```

```python
# deps.py, current_auth -- after ctx.authenticator.authenticate(...) succeeds,
# before the rate limiter (an account refused the route gets no limiter
# consequence for the request it was never allowed to make)
    path = getattr(request.scope.get("route"), "path", None)
    route = (request.method.upper(), path) if path else None
    require_continuation_route(auth, route)
```

This is the SAME dependency every route already calls to authenticate at
all -- directly, or through `tenant_scope`/`admin_auth`, both of which
`Depends(current_auth)` themselves -- so a route added tomorrow with no idea
this scope exists is closed to it by default, not open until someone
remembers to gate it. `routes/accounts.py`, `routes/tenants.py`'s credential
route, `/v1/attempts`, `/v1/outcomes`, `/v1/stats`, `/v1/capacity`,
`/v1/providers`, `POST /v1/tasks`, `POST /v1/tasks/batch`, every `/{id}/cancel`
route and every `/v1/admin/*` route are refused **because they are absent from
CONTINUATION_ROUTES**, not because each was individually taught to check
`member_scope` -- the fix for the missed routes and the fix for the routes
this entry already named are the same fix.

**`/v1/stats`, `/v1/capacity`, `/v1/providers`, `/v1/tenants/me`, classified
explicitly, per re-review:** none is in `CONTINUATION_ROUTES`. All four are
tenant-wide informational reads the fixer's job -- submit a continuation, read
what it submitted -- does not need; `/v1/tenants/me` is otherwise harmless
(no secrets, only the caller's own principal and the tenant's declared
providers) but is left out on the same "nothing not needed for the job"
principle, not because it is dangerous. `/v1/resource-classes` and
`/v1/runtimes` ARE included: both are the platform's static, non-tenant
catalogue (`platform.py`'s own docstrings: "no store read, no tenant filter"),
so listing them costs nothing and a client would otherwise have no way to
interpret `resource_class`/`runtime` names it already receives back on the
agents it can read.

**`POST /v1/workflows`** stays in `CONTINUATION_ROUTES`, but
`SubmissionService.submit_workflow`, right beside its existing
`resolve_continuation` call (#273), still refuses when
`auth.member_scope == "continuation" and spec.continues_task is None`: being
on the route allow-list only means the ROUTE is reachable, not that every
request to it succeeds. Everything `resolve_continuation` already checks --
caller's own tenant, `direct-pr`, one step, no `repository_ref` -- still
applies unchanged on top of this.

**The `submitted_by` filter lives in the store's task lookup, not
route-by-route, per re-review.** One shared dependency derives it:

```python
# deps.py
def submission_scope(auth: AuthContext = Depends(current_auth)) -> str | None:
    """`None` for an ordinary member (unfiltered within the tenant, today's
    behaviour); the caller's own email for a continuation-scoped one. ONE
    function, so every read route derives this the same way instead of each
    restating `auth.email if auth.member_scope == "continuation" else None` --
    which is what the first draft did, and how the checkpoint-content routes,
    which never even took an `AuthContext`, were missed: there was nowhere
    that repeated expression could have been written for them.
    """
    return auth.email if auth.member_scope else None
```

Every route in `CONTINUATION_ROUTES` that reads an agent or workflow now also
declares `submitted_by: str | None = Depends(submission_scope)` and passes it
down to `Store.get_task`/`Store.list_tasks`/`Store.get_workflow`/
`Store.list_workflows`, which gain the parameter and do the filtering
**once, in the store**.

**`submitted_by` is a REQUIRED keyword, with no default, on all four --
corrected 2026-09-29, round 3 of re-review.** The first draft gave it
`submitted_by: str | None = None`, which is opt-in: a route or service
that reaches an agent through a layer that never learned about this change --
the artifact service, `ctx.inspection`, the transcript and answer services,
`Store`'s OWN internal helpers that call `self.get_task(...)` before doing
something else -- keeps compiling, keeps passing its existing tests, and
keeps returning the agent, silently, to a caller the filter was supposed to
refuse. `Store.get_task` alone has two such internal callers today
(`store.py:961` and `:1051`, a tenant check and an artifact-manifest read
that each call `self.get_task(tenant_id, task_id)` with no third argument),
and `Store.get_workflow` has a third (`store.py:1194`) -- none of them
security-relevant today, all three exactly the shape a missed thread-through
would take. A default makes that shape free to write; removing the default
makes it a `TypeError` at the call, and a `reportCallIssue`/missing-argument
error at type-check, before it ever reaches a test:

```python
# store.py
def get_task(self, tenant_id: str, task_id: str, *, submitted_by: str | None) -> Task:
    task = ...  # unchanged lookup and tenant check
    if submitted_by is not None and task.submitted_by != submitted_by:
        # SAME words as the existing cross-tenant refusal, corrected 2026-09-29:
        # `Store.get_task` today (main, checked directly) raises
        # `NotFound(f"task {task_id!r} not found")` for BOTH a genuinely
        # missing task and another tenant's task -- NOT "is not a task in
        # your tenant", which is #273's own `resolve_continuation` wrapper
        # message for `continues_task` specifically, a different code path.
        # An earlier draft of this entry quoted the wrong one. Not a new
        # enumeration oracle: a continuation-scoped caller cannot distinguish
        # "not yours" from "not in your tenant" from "does not exist" any
        # more than a cross-tenant caller can today.
        raise NotFound(f"task {task_id!r} not found")
    return task
```

`list_tasks`, `get_workflow` and `list_workflows` gain the identical
`*, submitted_by: str | None` (no default) and the identical check, so all
four fail the same way when a call site forgets the argument. **Every
existing call site in the codebase passes `submitted_by=None` explicitly**
(today's unfiltered behaviour, made an explicit decision rather than an
implicit default) -- verified by `grep -rln
"\.get_task(\|\.list_tasks(\|\.get_workflow(\|\.list_workflows(" --include="*.py" .`
against this branch: `apps/swarm-api/swarm_api/{store.py, rollup.py,
outcomes.py, inspect.py, routes/admin.py, routes/tasks.py,
routes/workflows.py}`, `apps/reconciler/reconciler/backends.py`, and
`tests/unit/control_plane/{test_scheduler_writes_are_guarded.py,
test_workflow_state_rollup.py}` -- fifteen call sites in `apps/swarm-api`
alone outside `store.py`'s own internals, plus the reconciler's. Every one of
them is a full-member or internal (non-tenant-scope) read today, so
`submitted_by=None` is a no-op change to their behaviour and a REQUIRED
statement of that fact, not a new one to justify per site.

**This is what closes the checkpoint-content gap, and closes it the way
round 2 intended but did not enforce.** Those three routes gain
`submitted_by: str | None = Depends(submission_scope)` and thread it through
`service.list_files(tenant_id, task_id, submitted_by=submitted_by, ...)` ->
`CheckpointContent` -> `InspectionService`'s own `Store.get_task` call. Round
2 already proposed this thread-through; round 3's fix is that `Store.get_task`
itself now REFUSES to compile or run without the keyword arriving from
somewhere, so a fourth service layer discovered later -- the artifact
service, the transcript service, the answer service, or one not yet
written -- cannot reach an agent by forgetting the argument; it can only reach
one by supplying it, correctly or not, and supplying it wrong is a much
narrower way to fail than supplying nothing at all.

### What `tenant_principal` becomes for such a caller

**The tenant's principal, not the caller's email**: `eng@saga.xyz` for the
fixer, exactly what a human member of `eng` gets from `_tenant_principal`.
It has to be. `Service.tenant_for` passes it to `Store.ensure_tenant`, and
`Service.scope_for` to `Store.assert_tenant_scope`, and both compare it with
the principal stored on the `eng` tenant document to refuse a caller whose
tenant id belongs to a different principal. The account's own email there
would make every request it sends a collision refusal -- and if it were ever
the first caller to create the document, it would record the bot as `eng`'s
principal. The `secret_admin_principals` check in `tenant_for` is unaffected,
because it reads that same tenant principal, and the listing refuses an
account in `secret_admin_members` separately (below).

The caller's own identity is still `principal.email`, which is what
`submitted_by` is built from. `member_scope` (item 5, above) is a separate,
narrower gate layered on top of `tenant_id`/`tenant_principal`: the tenant
says WHICH secrets, GSA and prefix a caller gets; the scope says WHICH of the
tenant's routes it may use. A future listed account with fuller rights would
set `member_scope=""` and get everything a human member gets -- this entry
proposes `"continuation"` for `swarm-ci-fix` specifically, not a ceiling on
the mechanism.

### Audit: which member submitted a task

Three records, none a frozen-type change; extended so submit, cancel and
continue are recorded the same way rather than only the first (decision 4,
"audit records the acting member ... consistently"):

- **`Task.submitted_by` and `Workflow.submitted_by`** are already the verified
  email (`service.py` sets both from `ctx.email`), so a fixer agent already
  carries `swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com`, and the
  outcome ledger's `submitted_by` grouping separates the bot's agents from the
  humans' in `eng` with no further change. This covers submit AND continue --
  a continuation is a normal `Store.create_tasks` call for its one step agent,
  so it already gets both fields with no extra code.
- **`AuthContext.tenant_member`** (new, in `auth.py`, default `""`) is the
  listed email when the tenant came from a listing. The `SUBMITTED` event that
  `Store.create_tasks` writes gains `detail["submitted_by"]` and
  `detail["tenant_member"]` -- `"service_account"` for a listed caller, absent
  otherwise -- so the event trail says, per agent, not only who submitted it
  but that the tenant was assigned by a listing rather than by a group. The
  sign-in log line above records the same per request, with no token material
  (the email is already what every other `auth.py` log line names).
- **`Store.request_cancel`**, called from `cancel_task` with `by=auth.email`
  already, gains the same `tenant_member` parameter `create_tasks` gets, so
  the event it writes carries `detail["tenant_member"] = "service_account"`
  under the identical rule. Today only a full member can reach `cancel_task`
  at all -- `("POST", "/v1/tasks/{task_id}/cancel")` is absent from
  `CONTINUATION_ROUTES` (item 5), so `current_auth` itself refuses a
  continuation-scoped caller before the route body, let alone the store, runs
  -- so this is written for consistency with any future listed account whose
  scope is not `"continuation"`, not for `swarm-ci-fix` itself -- a
  `member_scope` that changes must not also have to remember to re-wire the
  audit trail for the one action it newly permits.

### Validation

Every rule is enforced at plan time in `terraform/infra/variables.tf`, and again
at swarm-api startup in `settings.py` for anything Terraform cannot see (an
environment variable set by hand). The variable already requires Terraform
`>= 1.9.0` (`versions.tf`), which is what lets a validation on `var.tenants`
read `var.admin_users`; `secret_admin_members` already validates against
`var.tenants` the same way.

1. **Only a user-managed service account in this project, never a human.**

   ```hcl
   validation {
     condition = alltrue(flatten([
       for t, v in var.tenants : [
         for sa in v.service_accounts :
         can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", sa))
         && endswith(sa, "@${var.project_id}.iam.gserviceaccount.com")
       ]
     ]))
     error_message = "tenants.*.service_accounts takes bare, lower-case emails of user-managed service accounts in this project (<id>@<project>.iam.gserviceaccount.com): never a person, a group, a domain, a pattern, a serviceAccount: member or a Google-managed account."
   }
   ```

   The expression is the one #273's `ci_fix_service_account` validation
   already uses. It refuses a human (no Workspace domain ends in
   `.iam.gserviceaccount.com`), a `serviceAccount:` member, a wildcard, and
   the Google-managed accounts every workload in a project can run as
   (`<number>-compute@developer.gserviceaccount.com`,
   `<project>@appspot.gserviceaccount.com`). The project pin is this entry's
   addition, and one the owner must accept: who can mint a token for an
   account is decided by IAM in that account's project, and only this
   project's IAM is reviewed here.

2. **Under one tenant only.**

   ```hcl
   validation {
     condition = length(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])) == length(distinct(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])))
     error_message = "a service account may be listed under ONE tenant: it decides the account's secrets, GCS prefix and namespace, and two listings would make that depend on rendering order."
   }
   ```

   The same rule is enforced two more times, not three as the first draft of
   this entry said: `TENANT_SERVICE_ACCOUNTS` renders as a JSON **list**, not
   an object keyed by email (item 2, above, reviewer minor), so a duplicate
   there is no longer a `locals.tf` plan-time error by construction -- it is
   `settings.py` that refuses one explicitly at startup (by membership check,
   not by relying on `dict`/`json` key collapsing), and `tenant_member_for`
   that raises `AuthError` rather than pick one if a caller somehow reaches
   this layer with two live entries for the same email. Terraform's own
   `var.tenants` validation above is still the first and cheapest of the
   three: it never lets the plan apply with a duplicate in the first place.

3. **Never an admin, in any admin list.**

   ```hcl
   validation {
     condition = length(setintersection(
       toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
       toset(concat(
         [for u in var.admin_users : lower(u)],
         [for u in var.admin_pool_users : lower(u)],
         [for m in var.secret_admin_members : lower(replace(m, "serviceAccount:", ""))]
       ))
     )) == 0
     error_message = "a tenant's service account may not also be in admin_users, admin_pool_users or secret_admin_members: a listing gives exactly one tenant's rights, and an admin entry would add every tenant's."
   }
   ```

   `admin_groups` cannot be checked here, because whether a group contains the
   account is a fact in the directory, not in Terraform. That is why
   `auth.py` skips the admin-group pass for a listed caller and sets
   `is_admin=False` unconditionally: membership of an admin group does not
   make a listed account an admin, by construction rather than by validation.

4. **Never a tenant's own principal.**

   ```hcl
   validation {
     condition = length(setintersection(
       toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
       toset([for t, v in var.tenants : lower(v.principal)])
     )) == 0
     error_message = "a service account that is a tenant's principal already has a tenant; listing it under another would give it two."
   }
   ```

   This is what stops `swarm-verify` (principal of `u-sw-c90291`) being listed
   under `eng`.

**The account's unique id is not a `validation` block** -- it is not user
input, it is read live from `data.google_service_account.listed` (item 2,
above) at plan time, from whatever account currently holds that email. If the
email is later deleted and recreated, the next `terraform apply` reads the
NEW account's unique id and renders that; nothing here freezes the old one.
The pin's job is narrower than a validation: it stops a stale, already-issued
token's `sub` claim (from the old account) from matching a listing that now
names a different one, in the window between the account being recreated and
the next successful apply -- not to catch a recreation that Terraform itself
already reflects.

Every refusal happens at `terraform plan`, before anything is applied, and
`tests/terraform` asserts each one (below).

### Isolation analysis (invariant 9)

**A listed account gets exactly its tenant's rights, narrowed further to
continuation only, and nothing else.** It resolves to one `tenant_id` and one
`tenant_principal`, the same pair a human member of that tenant gets, so every
tenant-scoped check downstream -- `get_task(tenant_id, ...)`,
`assert_tenant_scope`, the tenant's GSA, secrets, GCS prefix and namespace --
applies to it unchanged. On top of that, `member_scope` (below) cuts it down
to a fraction of what a human member of `eng` can do. It gets no admin (the
flag is forced false and the lists refuse it), no pool admin, no second
tenant (one listing, never a principal), and no personal tenant (a listed
account never falls through to `u-...`). The change grants it **no GCP IAM at
all**: it does not touch the tenant's GSA, its secrets or its bucket, and the
account's own roles stay exactly what #273's `ci_fix.tf` and
`frontend_iap_members` give it. Compared with the rejected alternative
(joining `eng@saga.xyz`), it gets none of that group's grants outside this
platform, and, after decision 2 below, far less than a member's grants inside
it too.

**What an attacker who steals its token gets (M2), restated honestly after
re-review -- the first draft of this section understated the reach, and the
owner has now reviewed and accepted the reach as stated here, not the earlier,
narrower one.** For as long as the token lives -- an ID token or IAP assertion
expires in at most an hour, and an access token minted by federation the
same, though "who can mint it" is wider than the token's own lifetime, see
below -- and constrained by `member_scope = "continuation"` (item 5, above):

- **Submit `continues_task` for ANY `eng` `direct-pr` agent that still exists,
  with an ARBITRARY PROMPT.** `resolve_continuation` (#273) constrains the
  SHAPE of a continuation -- caller's own tenant, `direct-pr` strategy, one
  step, no `repository_ref`, the continued agent's own repository -- and
  constrains NOTHING about the step's own instructions: that field is
  ordinary caller-supplied agent input, read exactly as any other agent's is.
  A continuation is not "reapply the same fix"; it is "run a new agent
  session, with a prompt of the attacker's choosing, against that repository,
  using `eng`'s forge token to push whatever it produces to that PR's
  branch." Cannot spend `eng`'s capacity on a genuinely NEW agent (no existing
  `direct-pr` PR to attach to) and cannot touch another tenant's agent --
  `resolve_continuation` and the scope check both still refuse those -- but
  neither of those is much of a ceiling: any `eng` repository with an open
  `swarm/` pull request is reachable, for whatever prompt the attacker sends.
- **Exfiltrate private repository content through the agent's own
  artifacts.** The account may read `GET /v1/tasks/{id}/artifacts`,
  `/logs`, `/transcript` and `/answer` for agents it itself submitted (item 5).
  A continuation it submits is such an agent. So the arbitrary prompt above can
  instruct the agent to copy the contents of any file the checked-out
  repository holds -- not only files related to the original red build --
  into a commit message, an artifact, or its own transcript, and the
  attacker reads it back through the same allowed routes. This is not a
  hypothetical composition of two separately-acceptable capabilities: it is
  the direct consequence of "arbitrary prompt with `eng`'s push access" plus
  "read what I submitted" existing on the same account.
- **No cap on how many times, and no cap on spend.** The owner considered and
  DECLINED a limit on continuation count or cost for this account (decision
  3, 2026-09-29). So the two capabilities above are not "one bounded
  incident" -- they can be repeated against every `eng` repository with an
  open `direct-pr` PR, for as long as the token can be used or re-minted (see
  **Who can mint the account's token**, below), each repetition spending real
  agent-provider cost against `eng`'s subscription and real capacity against
  `eng`'s `max_active` ceiling (invariant 2 still gates CONCURRENT capacity;
  nothing gates the NUMBER of sequential continuations over time). **Read the
  agents it itself submitted** is otherwise the account's only read reach: it
  cannot list or read a human `eng` member's other work, and it cannot
  cancel anything, its own agents included.

**`.github/workflows/auto-merge.yml` remains a real mitigation, but of a
narrower thing than the reach above.** A push to a pull request's branch
fires `synchronize`, which the workflow's `remove-stale-ready` job treats as
"this head changed" -- it turns auto-merge off and removes the `ready` label
unconditionally, label-adding events excepted
(`.github/workflows/auto-merge.yml` lines ~237-270). So a malicious commit
pushed this way cannot merge ITSELF: the pull request it lands on drops out
of the auto-merge queue the moment the push lands, and needs a human to
re-review and re-label it `ready`. **That bounds only the worst
CODE-SHIPPING outcome.** It does nothing for the exfiltration path above --
reading a secret back through the agent's own artifacts needs no merge at
all -- and nothing for the spend -- an agent session costs money and capacity
whether or not its commit ever merges. Restated so this is not read as more
protective than it is: it is one mitigation against one of three
consequences, not a bound on the reach as a whole.

**This is accepted, not mitigated, by owner decision on 2026-09-29 (decision
3).** The owner reviewed this restated reach -- arbitrary-prompt push access
plus artifact-based exfiltration plus unbounded repetition and spend -- and
chose not to add a continuation cap. What actually bounds this account is the
scope in item 5 (it is `continues_task`-or-nothing, and read-your-own-only)
and the token-lifetime and who-can-mint analysis below, not a limit on how
many times or how expensively the account can act within that scope.

Every agent it submits still says so in `submitted_by` and in its `SUBMITTED`
event (and, after decision 4's audit fix, every cancel and continuation it
performs says so too), so what it did is enumerable afterwards, after the
fact. It is not other tenants, not the admin surface, not the provider key or
forge token AS MATERIAL (#219 keeps both out of the workspace -- the
exfiltration path above is through repository content the agent reads and
reports, not through reading the credential itself), and not saga.xyz outside
this platform.

### Who can mint the account's token

**#273's `terraform/bootstrap/ci_fix.tf` binds `roles/iam.workloadIdentityUser`
on the account to exactly one principalSet,**
`attribute.job_workflow_ref/bogdan-alexandrescu/SwarmCloud/.github/workflows/ci-fix.yml@refs/heads/main`.
GitHub sets `job_workflow_ref` from the workflow file the job actually runs,
so another workflow -- in this repository on main, on another branch, or in a
fork -- presents a different value and is refused; the pool provider's
`attribute_condition` separately refuses any other repository and any ref
outside `github_allowed_refs`, and never `refs/pull/*`. `workflow_run` runs
the default branch's copy, so a pull request cannot change the file it runs
under. **That is a real limit on an external caller who holds nothing else in
this project:** minting a token that way means first getting a change to
`ci-fix.yml` merged to `main`.

**It is not a limit on the project's own IAM, and the first draft of this
entry overstated it as one.** Measured on 2026-09-29 as bogdan@saga.xyz,
`saga-agents-staging` IAM already lets several principals mint the account's
token without touching `ci-fix.yml` at all:

- **`roles/iam.serviceAccountAdmin`** includes `iam.serviceAccounts.setIamPolicy`
  on every service account in the project, so a holder can bind
  `roles/iam.serviceAccountTokenCreator` on `swarm-ci-fix` to any principal,
  themselves included, and mint a token directly -- no workflow run, no WIF
  pool involved. Held by: the deployer (`swarm-tf-deployer`), `bogdan@`,
  `facu@`, `konstantin@`.
- **`roles/iam.workloadIdentityPoolAdmin`** can edit the pool provider's
  `attribute_condition` and attribute mapping, so a holder can widen the
  principalSet the binding above already trusts to admit a different
  workflow, ref or repository -- defeating the "only `ci-fix.yml` on `main`"
  guarantee from the inside rather than around it. Held by: the deployer,
  `bogdan@`.
- **`roles/owner`** can do both of the above and everything else in the
  project. Held by: `bogdan@`, `emanuel@`.

**The owner accepted this as a known risk on 2026-09-29, not as a gap for
this PR to close.** Every principal listed already holds project-level trust
for other reasons -- the deployer runs Terraform against this project;
`bogdan@`, `facu@`, `konstantin@` and `emanuel@` administer it. What this PR
narrows is the ACCOUNT's own reach once a token is minted (decision 2,
continuation-only rights, above) and the DEPLOYER's standing roles (next
paragraph) -- not who else in the project could, in principle, mint the
token; that is accepted, not mitigated, here.

**The deployer's two roles, corrected (2026-09-29, after this entry's first
draft): `roles/iam.serviceAccountAdmin` cannot be scoped by an IAM
condition.** IAM resources do not expose `resource.name` to a condition at
all: *"the condition `resource.name.endsWith == devResource` never grants
access to any IAM resource because IAM resources don't provide the resource
name"* ([conditions attribute reference](https://docs.cloud.google.com/iam/docs/conditions-attribute-reference)),
and `iam.googleapis.com` is absent from the resource-service table in
[conditions-resource-attributes](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes).
A `resource.name` condition on this role would revoke it outright, not narrow
it, and the deployer's first service-account change after applying one would
403. `terraform/bootstrap/deployer_conditions.tf` (#334) records this in the
same words, as the reason the role stays **unscoped** in that PR.

**`roles/iam.workloadIdentityPoolAdmin` IS removed, in #334, and needs no
condition:** `terraform/infra` names no pool or provider of its own -- it
only names GKE's pool as a string inside a service-account binding's member
-- so the role was pure standing reach, dropped outright rather than scoped.

**For `roles/iam.serviceAccountAdmin`, the owner decided the shape that
actually works, recorded in #334's `docs/ci.md` on 2026-09-29 but not yet
applied by that PR's own diff -- a further bootstrap change:** remove the
deployer's project-wide `roles/iam.serviceAccountAdmin`; grant it
project-wide `roles/iam.serviceAccountCreator` instead (which needs no
per-resource name and stays project-wide by necessity, same as
`serviceusage.serviceUsageAdmin` above); and grant `serviceAccountAdmin`
**per account**, one resource-level binding on each `swarm-*` service account
`terraform/infra` manages, **excluding `swarm-ci-fix` and `swarm-tf-deployer`
themselves** (both bootstrap-managed, outside `terraform/infra`'s remit) --
the shape #23 already gave IAP admin, applied here to service accounts.
Until that lands, the deployer keeps its project-wide `serviceAccountAdmin`
and remains part of the accepted risk above; once it lands, the deployer can
still create and administer the `swarm-*` accounts `terraform/infra` manages,
but can no longer touch `swarm-ci-fix`'s IAM policy at all -- narrower than
"scoped", closed for that one account specifically.

**The residual, stated plainly: this narrows the deployer, not the humans
who separately hold these roles.** `bogdan@`, `facu@` and `konstantin@` hold
`roles/iam.serviceAccountAdmin` as individually granted principals, not
through the deployer, and neither #334 nor the per-account change above
touches those grants. So after both land, **`facu@`, `konstantin@` and
`emanuel@` (`roles/owner`) still can mint `swarm-ci-fix`'s token** --
an accepted risk, for the reason given above: they already hold project-level
trust for other reasons, and this entry narrows the account's own reach and
the deployer's standing access, not every human administrator's.

The remaining preconditions this entry's WIF analysis still depends on:

- the account has **no user-managed keys** (`gcloud iam service-accounts keys
  list --iam-account=swarm-ci-fix@...` lists only system-managed ones), since a
  key mints tokens with no workflow involved;
- `ci-fix.yml` never runs pull-request code in a step that can read the
  federated credential. On #273's head it checks out `main` only ("NOT the
  branch being fixed: its code runs in the fix step's container, never here")
  and runs only when the head repository is this one. That has to stay true,
  because a `workflow_run` job that executes the head's code with the token
  present hands the token to whoever wrote the pull request.

### The diff proposed for `identity.py`

Not applied. Generated against `main` at `d4a2352`. **Revised 2026-09-29 to
fold in the reviewer's minors** (`fullmatch`; `TenantMember` normalises once;
a `uid` pinned beside the email; an unknown `kind` raises) -- this supersedes
the diff this entry's first draft posted.

```diff
--- a/apps/common/swarm_common/identity.py
+++ b/apps/common/swarm_common/identity.py
@@ -8,12 +8,20 @@
 A TENANT IS A GOOGLE GROUP. `eng@saga.xyz` is one tenant whose members share a
 quota budget, provider keys and artifacts. A user in no mapped group falls back
 to a personal tenant so nobody is ever hard-blocked from the platform.
+
+A TENANT MAY ALSO LIST SERVICE ACCOUNTS (contract request 30). A service
+account is not a Workspace principal, and making it a member of the tenant's
+group would hand it every grant that group holds across the company. So a
+listed account resolves to its tenant by an EXACT match on the email AND the
+unique id its verified token carries, before any group is consulted -- never
+by a pattern, a prefix or a domain.
 """
 
 from __future__ import annotations
 
 import hashlib
 import re
+from collections.abc import Iterable
 from dataclasses import dataclass
 
 
@@ -29,6 +37,70 @@
     groups: tuple[str, ...] = ()
 
 
+#: A user-managed service account: `<account id>@<project id>.iam.gserviceaccount.com`,
+#: both halves 6-30 characters as GCP names them. Google-managed accounts
+#: (`<number>-compute@developer.gserviceaccount.com`, `<project>@appspot...`)
+#: do NOT match on purpose: every workload in a project can run as those, so
+#: listing one would put the whole project in the tenant. A human address
+#: cannot match either -- no Workspace domain ends in `.iam.gserviceaccount.com`.
+#: This regex alone does not pin the PROJECT: `settings.py` does that at
+#: startup with an `endswith` check this frozen module has no project id to
+#: perform itself -- see contract request 30, item 2. terraform/infra/variables.tf
+#: validates `tenants.*.service_accounts` with the same expression, and
+#: scripts/lib/check-contract-parity.sh holds the two equal.
+SERVICE_ACCOUNT_EMAIL = re.compile(
+    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
+)
+
+
+@dataclass(frozen=True)
+class TenantMember:
+    """A service account a tenant lists besides its group or user principal.
+
+    `kind` and `principal` are the TENANT's, not the account's: the tenant id
+    is derived from them by the same function that derives it for a human
+    member, so a listed account and the group it stands beside can never name
+    two different tenants.
+
+    Every field is normalised ONCE, here, rather than at each comparison --
+    the earlier draft of this type left `email`/`kind`/`principal` as given
+    and called `.strip().lower()` at every call site instead, which is
+    exactly the kind of repetition that lets one site be missed.
+    """
+
+    email: str       # swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com
+    kind: str        # "group" | "user" -- the tenant's kind
+    principal: str   # eng@saga.xyz -- the tenant's principal
+    uid: str         # the account's OAuth2 unique id (the token's `sub`
+                     # claim), from Terraform's
+                     # data.google_service_account.unique_id -- pinned beside
+                     # the email so an account deleted and recreated under
+                     # the same address, which GCP permits, does not inherit
+                     # the old one's tenant.
+
+    def __post_init__(self) -> None:
+        object.__setattr__(self, "email", self.email.strip().lower())
+        object.__setattr__(self, "kind", self.kind.strip().lower())
+        object.__setattr__(self, "principal", self.principal.strip().lower())
+        object.__setattr__(self, "uid", self.uid.strip())
+
+
+def tenant_member_for(
+    email: str, subject: str, members: Iterable[TenantMember]
+) -> TenantMember | None:
+    """The listed member whose email AND unique id (`sub`) both match, or None.
+
+    Exact equality on both, nothing else. An entry that is not a user-managed
+    service-account address is never matched even when listed, so a
+    misconfiguration that names a human cannot route that human around their
+    group. An email match with no matching `uid` is treated as no match at
+    all -- the caller falls through to today's refusal exactly as an unlisted
+    account would, rather than being told why. An address listed under two
+    tenants is refused rather than resolved by order: the tenant decides
+    which secrets and which GCS prefix a caller gets, and "whichever was
+    rendered first" is not a decision.
+    """
+    wanted = email.strip().lower()
+    if not SERVICE_ACCOUNT_EMAIL.fullmatch(wanted):
+        return None
+    found = [m for m in members if m.email == wanted and m.uid == subject]
+    tenants = {(m.kind, m.principal) for m in found}
+    if len(tenants) > 1:
+        raise AuthError("a service account is listed under more than one tenant")
+    return found[0] if found else None
+
+
 _TENANT_SAFE = re.compile(r"[^a-z0-9-]+")
 
 
@@ -98,14 +166,33 @@
     return domain
 
 
-def resolve_tenant(principal: Principal, group_priority: tuple[str, ...]) -> str:
+def resolve_tenant(
+    principal: Principal,
+    group_priority: tuple[str, ...],
+    service_accounts: tuple[TenantMember, ...] = (),
+) -> str:
     """Pick the caller's tenant.
 
-    `group_priority` is the admin-ordered list of group emails that map to
-    tenants. First match wins, so a user in several mapped groups lands
+    A caller whose verified email AND subject match a listed service account
+    resolves to the tenant that lists it, FIRST and regardless of
+    `principal.groups`: the listing is an admin's explicit statement about
+    this one identity, and a group membership (which a service account can
+    hold) must not be able to move it somewhere else.
+
+    Otherwise `group_priority` is the admin-ordered list of group emails that
+    map to tenants. First match wins, so a user in several mapped groups lands
     deterministically in the same tenant on every request -- which matters
     because the tenant determines which secrets and which GCS prefix they get.
     """
+    member = tenant_member_for(principal.email, principal.subject, service_accounts)
+    if member is not None:
+        if member.kind == "group":
+            return tenant_id_for_group(member.principal)
+        if member.kind == "user":
+            return tenant_id_for_user(member.principal)
+        # settings.py already refuses this shape at startup; reaching it here
+        # means that check was bypassed (a hand-set env var, a test double) --
+        # surfaced rather than guessed at.
+        raise AuthError(f"a listed tenant member names an unknown kind {member.kind!r}")
+
     member_of = {g.lower() for g in principal.groups}
     for group in group_priority:
         if group.lower() in member_of:
```

### What it would break if accepted

Nothing that exists. `resolve_tenant`'s new parameter is keyword-defaulted to
`()`, so `auth.py`'s current call, `test_group_resolution.py` and any other
caller behave exactly as today until `TENANT_SERVICE_ACCOUNTS` is non-empty.
`AuthContext.member_scope` is likewise defaulted to `""`, and
`require_continuation_route` (item 5) returns its argument immediately
whenever `ctx.member_scope` is falsy -- which is every existing caller, since
nothing sets it today -- so `current_auth` gaining this check changes nothing
for a human member or an unlisted service account, on any route, existing or
future. It is EVERY route that newly runs this check, not routes that opt
in (the design item 5 replaced was the opt-in shape, and re-review is why it
was replaced); the change in observable behaviour is confined entirely to a
caller whose `member_scope` is non-empty, and there are none until
`TENANT_SERVICE_ACCOUNTS` lists one. No stored document changes shape: `Task`,
`Workflow`, `Tenant` and `TaskEvent` are untouched (the audit fields are
`detail` keys and `AuthContext` fields outside the contract). `docs/ci.md` on
#273 says the fixer's account must be "admitted (`allowed_users`) and a
member of that tenant's Google group"; that sentence changes to naming
`tenants.eng.service_accounts` and to saying its rights are continuation-only.

### If it is declined

The fixer's account can resolve to `eng` only by joining `eng@saga.xyz`, which
the owner has rejected, or not at all, in which case #273's continuation is
refused with "is not a task in your tenant" on every red pull request and the
fixer can only comment.

### The tests that would prove it

Unit, no credentials and no emulator:

- `tests/unit/control_plane/test_group_resolution.py` (`resolve_tenant`,
  `tenant_member_for`):
  - a listed account resolves to `eng` when `principal.email` AND
    `principal.subject` both match the listing's `email`/`uid`, with
    `groups=()`, and to `tenant_id_for_user(principal)` when the listing
    tenant is `kind="user"`;
  - `SWARM-CI-FIX@SAGA-AGENTS-STAGING.IAM.GSERVICEACCOUNT.COM` resolves the
    same (case-insensitive on email; `uid` still matches exactly);
  - **an email match with a mismatched `uid` does NOT resolve to `eng`** --
    falls through exactly as an unlisted account would (the recreated-account
    case the `uid` pin exists for);
  - **`fullmatch`, not `match`:** an email with a trailing newline
    (`"swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com\n"`) does not
    match, where the earlier `.match()` with `$` in the pattern would have;
  - the listing wins over groups: the same account with
    `groups=("other@example.com",)` and `other@example.com` first in priority still
    resolves to `eng`;
  - no pattern and no domain: `swarm-ci-fix2@...`, `x-swarm-ci-fix@...`,
    `swarm-ci-fix@other-project.iam.gserviceaccount.com` and
    `swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com.evil.example`
    each fall through to today's rule;
  - a human address listed as a member (`bogdan@saga.xyz`) never matches;
  - an account listed under two tenants raises `AuthError`; listed twice
    under the same tenant (same `kind`/`principal`) resolves;
  - **a `TenantMember` with `kind="workspace"` (anything but `"group"` or
    `"user"`) raises `AuthError` from `resolve_tenant`** rather than
    resolving as `kind="user"`;
  - `TenantMember(" Eng@Saga.xyz ".strip(), " GROUP ", ...)`-style
    constructor arguments come out normalised once (`__post_init__`), so two
    `TenantMember`s built from differently-cased/whitespaced input compare
    equal on `email`/`kind`/`principal`;
  - **`tenant_member_for` does no IAP-prefix stripping of its own:** a
    `principal.subject` of `"accounts.google.com:12345"` against a listed
    `uid` of `"12345"` does NOT match here -- proving the frozen function
    does exact comparison only, and that the `"accounts.google.com:"` strip
    (item 4) has to happen in `auth.py`, before this function is ever called,
    not be expected of it;
  - with `service_accounts` omitted, every existing case in the file gives
    today's answer (the file's current cases, unchanged, are this test).
- a new `tests/unit/control_plane/test_tenant_service_accounts.py`
  (`Authenticator` with `StaticTokenVerifier`):
  - a listed account gets `tenant_id="eng"`, `tenant_principal="eng@saga.xyz"`,
    `is_admin=False`, `is_pool_admin=False`, `admin_unresolved=False`,
    `tenant_member=<its email>`, **`member_scope="continuation"`**;
  - a caller with no listing gets `member_scope=""`, including every case
    `test_group_resolution.py` already covers for a human;
  - it is admitted with `allowed_domains=("saga.xyz",)` and empty
    `ALLOWED_USERS`, and an unlisted service account is still 403;
  - the `MembershipResolver` is a fake that raises `GroupLookupError` on any
    call, and `admin_groups` is non-empty: the listed account still
    authenticates (no lookup made, no 503), and is still not an admin;
  - `email_verified: False` is still 401 (the general rule);
  - **`email_verified` ABSENT (not `False`, simply missing) is 401 for the
    listed path on the BEARER path specifically** -- the stricter, listed-only
    rule (decision 4) -- while an unlisted bearer caller with the claim absent
    still succeeds, proving the two paths are not accidentally the same code;
  - **on the IAP path, the same listed caller with `email_verified` absent
    from the raw assertion still authenticates.** This is not a gap in the
    check -- `IapAssertionVerifier.verify` already forces
    `claims.setdefault("email_verified", True)` (`auth.py:211`) before
    `_from_claims` runs, so the listed-path check never observes an absent
    claim on this path at all. The test exists to SAY so, not to catch a
    bug: it is the proof that decision 4's "explicit True" rule is a
    bearer-path protection that happens to be inert for `ci-fix.yml`, which
    calls through IAP;
  - **the IAP path and the bearer path resolve to the SAME `AuthContext`
    (including `member_scope`) from DIFFERENT-SHAPED `sub` claims** -- a
    bearer token's bare `"12345"` and an IAP assertion's
    `"accounts.google.com:12345"` both resolve to the listed account when its
    `uid` is `"12345"`, proving the `"accounts.google.com:"` strip (item 4)
    actually closes the gap it exists for, not merely that both paths agree
    when fed identical input;
  - **a duplicate listing (`tenant_member_for` raising `AuthError`) comes back
    401, not 500,** on both the bearer path and the IAP path -- the symmetric
    `try`/`except` decision 4 adds to `_authenticate_bearer`;
  - `continues_task` naming an `eng` agent is accepted, naming a `u-bogdan`
    agent is refused "is not a task in your tenant";
  - **`CONTINUATION_ROUTES` is swept against every route in every router the
    app includes, and extended in round 3 to a second assertion per
    task/workflow-scoped route** (`tests/unit/control_plane/test_continuation_scope_is_narrow.py`,
    the same shape as `test_pool_admin_is_narrow.py`):
      - **reachability:** every route NOT in the set 403s a continuation-scoped
        caller with an otherwise-valid request, and every route IN the set
        does not 403 for that reason. This is the test that would have caught
        the first draft's gap, because it does not require anyone to have
        thought of `routes/accounts.py` -- it walks the app's own route table;
      - **ownership, round 3's addition:** for every route in the swept set
        whose path names a `{task_id}` or `{workflow_id}`, the fixture creates
        ONE task/workflow submitted by a different `eng` member and drives
        that route at it with the listed caller's credentials -- every one
        404s ("task 'X' not found", the existing cross-tenant wording --
        corrected 2026-09-29, see item 5's `Store.get_task` snippet). This is
        the assertion that
        catches a missed `submitted_by` thread-through directly, rather than
        relying on someone naming the right route by hand: it walks
        `CONTINUATION_ROUTES` itself, so a route added to the allow-list
        later without its filter wired through fails this test on the day it
        is added, not on the day someone thinks to ask;
  - named explicitly, because re-review found them missing: **all eight
    `routes/accounts.py` routes** (list, register, authorize, exchange,
    refresh, lending, state, delete), **`POST /v1/tenants/me/credentials`**,
    **`GET /v1/attempts`**, **`GET /v1/outcomes`**, **`GET /v1/stats`**,
    **`GET /v1/capacity`**, **`GET /v1/providers`**, **`GET /v1/tenants/me`**
    each 403 for the listed caller with a request that would succeed for an
    ordinary `eng` member;
  - **`GET /v1/resource-classes` and `GET /v1/runtimes`** succeed for the
    listed caller (static catalogue, no tenant data, explicitly allow-listed);
  - **`POST /v1/tasks`, `POST /v1/tasks/batch`, `POST /v1/tasks/{id}/cancel`,
    `POST /v1/workflows/{id}/cancel`** each 403 for the listed caller and
    succeed for an ordinary `eng` member with an identical request otherwise
    -- covered by the sweep above too, and named individually because they
    are the routes decision 2 was written about first;
  - **`POST /v1/workflows` with no `continues_task`** 403s for the listed
    caller and succeeds for an ordinary `eng` member -- `submit_workflow`'s
    own check, since being in `CONTINUATION_ROUTES` only opens the route, not
    every request to it;
  - **`GET /v1/tasks/{id}` for an agent `eng`'s human member submitted** 404s
    ("task 'X' not found", the existing cross-tenant wording) for the listed
    caller, and **the same
    call for an agent the listed caller itself submitted** succeeds -- proving
    the `submitted_by` filter is per-caller, not per-tenant. Also covered by
    the sweep's ownership assertion above; kept as its own named case because
    it is the one this entry's examples build on elsewhere;
  - **`GET /v1/tasks`** for the listed caller returns only agents it submitted,
    even when `eng` has others (a LIST assertion, which the sweep's
    single-agent ownership check does not itself make -- `Store.list_tasks`
    filtering is exercised here specifically);
  - **`GET /v1/tasks/{id}/checkpoints/{checkpoint_id}/files` (and `/files/{path}`,
    `/content`) for a checkpoint of an agent `eng`'s human member submitted**
    404s for the listed caller, and the same call for the listed caller's OWN
    agent succeeds -- the case that had no seam at all before `submission_scope`
    was threaded into `checkpoints.py`, and now also caught by the sweep's
    ownership assertion since these three are members of `CONTINUATION_ROUTES`;
  - a new `tests/unit/control_plane/test_store_task_scope_is_required.py`:
    calling `store.get_task(tenant_id, task_id)` (two positional arguments,
    no `submitted_by`) raises `TypeError: get_task() missing 1 required
    keyword-only argument: 'submitted_by'`, and the same for `list_tasks`,
    `get_workflow`, `list_workflows` -- the test that proves the signature
    change itself, independent of any route, so it stays red even if every
    route-level test above were somehow satisfied by an accident of fixture
    setup;
  - the created agent's `submitted_by` is the account's email and its
    `SUBMITTED` event carries `tenant_member="service_account"`; the same is
    true of a `CANCELLED` event when a FULL member (not the listed caller,
    which cannot reach cancel -- absent from `CONTINUATION_ROUTES`) cancels
    while resolving through a listing, and of a continuation's own
    `SUBMITTED` event.
- settings: `TENANT_SERVICE_ACCOUNTS` parses as a **list**; `ApiSettings.from_env`
  raises `ValueError` for a human address, a Google-managed service agent
  under a project-shaped id (`service-123@gcp-sa-x.iam.gserviceaccount.com`
  where `x` happens to look like a project id, refused by the `endswith`
  pin, not by the regex), an account from a DIFFERENT project, a duplicate
  email (two list entries, same or different tenant -- refused by the
  explicit membership check, not by relying on `dict`/`json` key collapse), a
  missing or empty `uid`, a `kind` outside `{group, user}`, and an entry also
  in `ADMIN_USERS`, `ADMIN_POOL_USERS` or `SECRET_ADMIN_PRINCIPALS`; absent or
  `[]` is `()`.

Terraform, `tests/terraform/tenancy.tftest.hcl`, each refusal a `run` with
`expect_failures = [var.tenants]`: an account under two tenants; a human
address; `serviceAccount:`-prefixed; a Google-managed compute account; an
account in another project; one in `admin_users`, in `admin_pool_users`, in
`secret_admin_members`; one that is another tenant's principal. `data.
google_service_account.listed` is overridden with a mocked `unique_id` via
`override_data` in the `.tftest.hcl` file, so these runs need no real GCP
call. And one passing `run` asserting the rendered `TENANT_SERVICE_ACCOUNTS`
equals
`[{"email":"swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com","kind":"group","principal":"eng@saga.xyz","uid":"<mocked>"}]`
-- a **list**, revised from the object-keyed-by-email shape the first draft
of this entry proposed (reviewer minor) -- and `[]` when no tenant lists one.

Parity: `scripts/lib/check-contract-parity.sh` gains a check that the
expression in the `variables.tf` validation equals
`identity.SERVICE_ACCOUNT_EMAIL.pattern`, so the plan-time rule and the
runtime rule cannot drift apart. It does not and cannot check the project-id
pin, which lives only in `variables.tf` and `settings.py` (`identity.py` has
no project id to compare against) -- those two are checked against each
other by the settings test above instead.

Each test is proved red first in CI, per CLAUDE.md: the tests land alone on
the implementing branch and must fail there before the change lands on
`<branch>-fix`.

### Invariants

- **Invariant 9.** Analysed above: a listed account is one more member of one
  tenant, with that tenant's GSA, secrets, prefix and namespace and nothing
  else -- and, after decision 2 (2026-09-29), fewer of that tenant's ROUTES
  than a human member, not merely the same rights routed through a bot.
- **Invariant 10.** Unaffected: the listing is operator configuration in
  Terraform; nothing a caller sends chooses a tenant.
- **Invariant 2 (all-or-nothing capacity).** Unaffected in mechanism -- a
  continuation still reserves capacity through the same transaction every
  other agent does -- but worth stating given decision 3: nothing here caps
  HOW MANY times a continuation-scoped account can go through that
  transaction over time, only how many it can hold `LEASED` at once (`eng`'s
  `max_active`, invariant 3). The owner accepted that as part of the same
  no-cap decision, not as a separate gap.
- **CONTRACT.md "Tenant = Google group".** Refined, not reversed: a tenant is
  still a group (or a user), and its listed accounts are members by
  declaration rather than by directory.
Applied by #343 (accepted by the owner 2026-09-29).
---

## 31. `models.py`: `WorkflowStep` cannot record a step's verdict gate or its `builds_on`

**Status:** open, recorded 2026-09-29 from #264. If another branch has taken
31 by the time this merges, renumber this one.

### What is true today

#264 added two workflow step fields, `when` (`{"step", "verdict_in"}`: run
this step's agent only on an upstream review's verdict) and `builds_on` (an
upstream step whose pushed branch this step clones). They are accepted by
`WorkflowStepCreate`, checked by `validation.validate_step_routing`, and
stored where the worker reads them: `task.metadata["dispatch"]`, as
`verdict_gate: {"task_id", "verdict_in"}` and `builds_on: <task id>`, keyed by
task id the way `integrates` is. That block is already reserved, so no new
reserved key was needed and nothing frozen was edited.

The frozen `WorkflowStep` has no field for either, so the workflow document
does not record them. `GET /v1/workflows/{id}` shows a gated step exactly as
it shows an ungated one; a reader has to open the step's agent to see the gate
(`GET /v1/tasks/{id}`, `dispatch.verdict_gate`). The declaration as the caller
wrote it, keyed by step id, is not stored anywhere.

### The requested change

Two optional fields on `WorkflowStep`, both defaulting to "absent", so every
stored workflow reads back unchanged:

```python
#: {"step": upstream step_id, "verdict_in": [...]} -- run this step's agent
#: only on those verdicts (#264).
when: dict[str, Any] | None = None
#: An upstream step_id whose pushed branch this step's checkout starts from.
builds_on: str | None = None
```

And, with them, `REVIEW_VERDICTS = ("MERGE", "NOT_YET")` in `models.py`, which
is today written out twice (`swarm_api.validation.REVIEW_VERDICTS` and
`agent_worker.verdict.REVIEW_VERDICTS`) and held equal only by
tests/unit/worker/test_verdict_gate.py.

### What it would break if accepted

Nothing stored: both fields default to absent. `SubmissionService.submit_workflow`
would copy them onto the `WorkflowStep` it already builds, and the workflow
codec and the UI's step rows would start showing them.

### If it is declined

The gate stays visible only on the agent, and the verdict vocabulary stays in
two copies behind a parity test. That test lives in the unit suite, so it
protects the repository, not a worker image built from an older commit.
---

## 32. `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
**Applied by #345** (accepted by the owner 2026-09-29), `part of #218` (see `docs/DEPLOY_STATE.md` or the linked
PR for its state — this entry itself does not track a moving target). It
answers #218,
which contract request 25 left open: which inputs `browser` and `generic`
declare, and with which bounds. Originally numbered 29, because 28 was the
last entry on `main` when this branch was cut and 27 appears nowhere on
`main`. **Renumbered to 32 on 2026-09-29**, after a security review found
four other open PRs had each numbered their own new entry 29 for the same
reason: #259, #314, #304 and #316. Resolved as #259 keeps 29, #314 becomes
30, #304 becomes 31, this entry (#218) becomes 32, and #316 becomes 33.

### What is true today

Measured from `main` at `d4a2352` on 2026-09-29.

**Both profiles are `inputs=None`**, and `check_inputs` hands their input
back unchecked, so swarm-api bounds it by size (`validate_input_size`) and by
Firestore's integer range (`validate_storable`) and nothing else. The owner
confirmed that on 2026-09-26 as the interim (request 25's table row). It was
meant to last until #218 was decided.

**It has already stopped work.** #220 found on 2026-09-28 that every browser
agent dispatched through the `sc` plugin failed after it started, with
"browser runner needs input.url or at least one action". The bridge sends a
key only when a declaration names it (`swarm_mcp/profiles.py`, `check_inputs`),
and `browser` declares nothing. So no caller of the plugin can give a browser
agent the one thing it cannot run without, and the refusal arrives after
admission, after a lease and a GKE pod start.

**What the size bound lets through, and where it fails instead.** Every item
below is a value the API accepts today that the runner then fails on, or
quietly reads differently:

* `timeout_ms: 0` and `launch_timeout_ms: 0`. Playwright reads 0 as "no
  timeout", so one `wait_for` on a selector that never appears holds the
  attempt, and its `browser` capacity (2 units), for the profile's full
  5400 s.
* `timeout_ms: "abc"`. The runner calls `int()` on it, raises `ValueError` and
  fails the attempt after admission.
* An action without the field its type needs: `{"type": "click"}` raises
  `KeyError` at `action["selector"]`, also after admission.
* `{"type": "wait", "seconds": 600}`. The runner clamps it to 60 with
  `min(..., 60.0)` and does not report the clamp.
* A `goto` to `http://10.0.0.1/`. The runner accepts any http(s) URL with a
  host. The worker's NetworkPolicy drops the packets, and Dataplane V2 drops
  without replying, so the action waits out `timeout_ms` and fails as a
  timeout. Nothing in the failure says the address was refused.
* `generic` with no `command`. It is admitted, leased and started, and then
  fails with "input.command is required".
* `generic` limits. `timeout_seconds: 0` is read as "not asked" and ignored.
  `timeout_seconds: 99999` is clamped to the platform's ceiling, with a
  WARNING in the worker's log that the caller never sees.
* Keys the worker fills with `setdefault` (`task_id`, `attempt_id`,
  `repository`, `resumed_from_checkpoint`, `lifecycle.py` around line 860).
  For these two profiles a value the caller sent takes precedence over the
  worker's. Neither runner reads those keys, so nothing is affected today.
  A declaration closes the gap anyway, because an undeclared key is refused
  at the door.

### What each runner reads

Read again from the runners' source on 2026-09-29. These tables list every
key `body()` reads from `ctx.payload`, and nothing else.
`generic.py` refuses `argv`, `script`, `env`, `command_line` and `shell` by
name. Once the profile declares, the API refuses those at 422 like any other
undeclared key.

**`browser`** (`agent_worker/runners/browser.py`):

| key | kind | bounds | required | runner's default, and why this bound |
|---|---|---|---|---|
| `url` | `url` (new) | http or https, public host (see question 2) | no; `url` or `actions` | none. The runner turns it into a leading `goto`. |
| `actions` | `list` (new) of `object` (new) | 0..200 entries | no; `url` or `actions` | `[]`. 200 is the runner's `MAX_ACTIONS`. |
| `timeout_ms` | integer | 1..300000 | no | 30000. Must be at least 1 because 0 disables Playwright's timeout. Five minutes is the most one action may wait. |
| `launch_timeout_ms` | integer | 1..180000 | no | 60000. The same reason for 1. A launch still waiting at three minutes means the pod is short of `/dev/shm`. |
| `viewport_width` | integer | 320..3840 | no | 1280. The viewport, and a full-page screenshot of it, are held in the pod's memory. |
| `viewport_height` | integer | 240..2160 | no | 900 |
| `user_agent` | string | none | no | Chromium's own; `""` also means Chromium's |
| `extract_text` | boolean | none | no | true: the final page's text is kept as `page.txt` |
| `screenshot` | boolean | none | no | true: a final full-page `final.png` is kept |

Each element of `actions` is an object whose `type` selects one of eight
fixed shapes. A key that the selected shape does not name is refused:

| `type` | fields (required in bold) | runner's defaults |
|---|---|---|
| `goto` | **`url`** (`url`), `wait_until` (string, one of `load`, `domcontentloaded`, `networkidle`, `commit`) | `wait_until` `load` |
| `click` | **`selector`** (string) | |
| `fill` | **`selector`** (string), `text` (string) | `text` `""` |
| `press` | **`selector`** (string), `key` (string) | `key` `Enter` |
| `wait_for` | **`selector`** (string), `timeout_ms` (integer 1..300000) | the task's `timeout_ms` |
| `wait` | `seconds` (number 0..60) | 1. Refusing a value above 60 replaces the runner's silent clamp. |
| `screenshot` | `name` (`filename`), `full_page` (boolean) | `screenshot-NNN.png`, `true` |
| `extract` | `selector` (string), `name` (`filename`) | `body`, `extract-NNN.txt` |

The runner writes both `name` fields through `RunnerContext.artifact_path`,
which keeps only the last path segment. That is the rule the existing
`filename` kind already states. The runner lower-cases `type`, so `"Goto"`
runs today. The declaration matches `type` exactly.

**`generic`** (`agent_worker/runners/generic.py`, `runners/limits.py`):

| key | kind | bounds | required | runner's default, and why this bound |
|---|---|---|---|---|
| `command` | string | one of `make`, `npm-build`, `npm-ci`, `npm-test`, `pytest`, `uv-sync` | **yes** | none. The runner fails without it. The list is `GENERIC_COMMANDS`. |
| `paths` | `list` (new) of `argument` (new) | 0..32 entries | no | none, so pytest runs the whole suite. 32 is `GenericCommand.max_arguments`. Only `pytest` reads it. |
| `target` | `argument` (new) | the runner's argument rule | no | `all`. Only `make` reads it. |
| `working_directory` | `argument` (new) | the runner's argument rule | no | the workspace root |
| `timeout_seconds` | number | 1..3600 | no | the profile's `timeout_seconds` (3600), which the worker exports as the ceiling |
| `grace_seconds` | number | 1..20 | no | `WorkerConfig.termination_grace_seconds` (20) |
| `max_stdout_bytes` | integer | 1..33554432 | no | `WorkerConfig.max_stdout_bytes` (32 MiB) |
| `max_stderr_bytes` | integer | 1..8388608 | no | `WorkerConfig.max_stderr_bytes` (8 MiB) |

**The four limits may only lower the platform's own.** Each declared ceiling
is the value the worker exports by default, so a request above it gets a 422
at submission. Today the runner accepts it and clamps it. The runner still
clamps to whatever the attempt's worker exports, because an operator may set
that lower through the environment. The minimum is 1 because the runner reads
0 or less as "not asked". `argument` is exactly what the runner's
`_check_argument` accepts: `^[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}$` and no
`..`. Whether the path exists and lies inside the workspace stays the
runner's check, because only the runner has the workspace.

### Question 1 (#218): `RunnerInput.kind` has no kind for a list

**Recommendation: add one `list` kind, whose bounds are its length and whose
elements are a declared `RunnerInput` (`items`).** It reuses the fields that
already exist. `minimum` and `maximum` become the length bounds and are
required, as they are for a number, for the same reason: without them a list
is unbounded until the agent write fails. Each element is checked by the same
`check`, under the key `actions[3]`, so a refusal names the element that
failed.

For `paths` that is the whole addition, plus an `argument` kind for the
element. `argument` is the generic runner's argument rule, added as a kind for
the same reason `filename` is one: a regex field would be a second, more
general feature that one rule does not need. `target` and
`working_directory` use it as well.

`actions` needs one more addition, because its elements are objects in eight
shapes. The smallest form that still checks each element is **an `object`
kind with `variants`**: a mapping from the value of `type` to the fields that
shape takes. Each field is a `RunnerInput`, which gains `required`. A shape
refuses keys it does not name. It is not general: the discriminator is always
`type`, and there is no nesting beyond what `items` and `variants` compose.
That is enough for every input any runner reads today. Two further fields
serve keys that are not lists. `choices` (string only) covers `command` and
`wait_until`. `required` is also read for a profile's top-level keys, so a
`generic` agent with no `command` is refused at the door.

**Alternatives considered, and why they were not chosen:**

* An opaque `json` kind, bounded by size. That is the current behaviour under
  another name. It checks no element, so a missing `selector` is still found
  after admission.
* A `browser_action` kind. It would put one runner's vocabulary into the kind
  list as code instead of as data, and the next runner with a list of shapes
  would need another kind.
* A full JSON Schema. It is far larger than any runner needs, and a caller
  could no longer read it from `describe()`.

**What `required` cannot say:** the browser runner needs `url` *or*
`actions`. This request leaves that check with the runner. That is a
decision for the owner, listed below.

### The `url` rule: does it need an allow-list or a scheme restriction?

**Not one of #218's three questions.** #218 asks three things, quoted
verbatim below under Question 2 and Question 3; whether `url` needs a scheme
or host restriction is not among them. It is this entry's own addition,
because declaring `url` as a kind at all raises it. A revision of this entry
mislabelled it "Question 2" and cited it as "#218's second question" in the
diff's own docstring; that was wrong on both counts and is fixed below and in
the diff.

**Recommendation: a scheme restriction and a public-host rule, checked at
submission. No host allow-list, and not https-only.** The check sits in one
place, `url_refusal` in the frozen module. It serves `url` and every `goto`
action's `url`. swarm-api, the bridge and the runner's `_check_url` all call
it.

**What the browser can reach today.** The pod's egress is
`kubernetes/network-policies/allow-egress.yaml`. It allows cluster DNS, the
metadata server at 169.254.169.254, the Private Google Access VIPs
(199.36.153.4/30 and 199.36.153.8/30) and the public internet. It excepts
every private range: RFC 1918, link-local, CGNAT and the cluster's pod and
service ranges. So the browser can reach:

1. **The GKE metadata server.** The pod needs it for Workload Identity, and
   the policy's own header says a NetworkPolicy cannot close it for one
   process in a pod. It mints the tenant GSA's token. What protects that
   token from a browser is that the server answers `/computeMetadata/v1/`
   only with a `Metadata-Flavor: Google` header. A navigation does not send
   that header. A cross-origin `fetch()` from page script that sets it
   triggers a CORS preflight, and the metadata server does not answer the
   preflight in a way that permits the request. The preflight behaviour is
   **not measured here**. The applying change should measure it with a
   browser agent against the live cluster.
2. **The Google API VIPs.** These are authenticated APIs, so they are useless
   without a token.
3. **The public internet.** That is the browser profile's purpose. The
   policy's header says it "has to fetch whatever page the task names, which
   cannot be an allow-list".

**Why the submission check cannot be the SSRF control.** It sees only the
URL the caller typed. It cannot see redirects (a public page can answer
`302` to `http://169.254.169.254/...`), the page's subresources, requests
its script makes, or what a name resolves to when the pod asks. That last gap
includes DNS rebinding, and names such as `svc.namespace` that the pod's
search path expands into the cluster. Resolving the name at submission does
not close it, because the pod resolves it again later. **The control is the
network, plus the header requirement.** A check at the door is defence in
depth.

**What the door check still buys:**

* A private address becomes a 422 with the reason, instead of a timeout that
  never says the address was dropped.
* A URL with `user:password@` is never stored with the agent, served by
  the API or shown in the UI. The runner would also echo it
  back as `final_url`.
* An address the rule can actually see, and that is not global, becomes an
  error at submission instead of a silent pass. **This is narrower than the
  first draft of this entry claimed** ("addresses that parse as public but
  are not become errors"): a security review on 2026-09-29 found that claim
  false, because the first draft's rule let the address it was supposed to
  catch avoid being parsed as that address at all -- see the four bypasses
  below, each of which reached `""` (accepted) under the original diff.

**A security review on 2026-09-29 found the rule below, as it stood, refused
by a different notion of "URL" than the browser that opens it.** Chromium
parses to the WHATWG URL Standard; `urlsplit` parses to RFC 3986. The two
disagree on backslash, on percent-encoding inside a host, and on non-ASCII
characters, and every disagreement is a bypass here: `url_refusal` sees a
value it does not recognise as the metadata host and returns `""`, while
Chromium normalises the same value to that host and opens it.

* `169.254.169.254\@example.com` -- `urlsplit` does not treat `\` as a netloc
  terminator (RFC 3986 does not, either), so the whole string, backslash
  included, becomes `parsed.hostname`. It fails no check the first draft
  had, and passes. Chromium treats `\` exactly like `/` in an authority, so
  it opens `169.254.169.254` with an ignored path.
* `metadata.google.interna%6cl` (`%6c` is a lower-case `l`) -- `urlsplit`
  does **not** percent-decode a host; `.hostname` returns the literal string
  with the `%6c` still in it, so the `.internal` suffix check never matches
  it and the loop over `_URL_REFUSED_SUFFIXES` passes. A browser host-parses
  it as the WHATWG standard requires (percent-decode, then IDNA), which
  yields `metadata.google.internal`, and opens it.
* `metadata.google.ｉｎｔｅｒｎａｌ` (full-width Unicode letters, U+FF49 etc.)
  and `169254169254｡` (U+3002 IDEOGRAPHIC FULL STOP, which IDNA/UTS46
  maps to `.`) -- neither is refused by anything in the first draft, which
  checked only ASCII punctuation and a handful of named suffixes. A
  browser's IDNA/UTS46 host processing folds full-width Unicode to its ASCII
  equivalent before it resolves, so both open the address they spell out
  once normalised.
* `kubernetes.default.svc` and `swarm-api.swarm-system.svc` -- refused today
  only by the "single-label host" rule below, and both have three labels, so
  they passed it. GKE's pod `resolv.conf` sets `ndots:5`: any name with
  fewer than five dots is tried against every search domain
  (`<namespace>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`,
  ...) before the absolute name, and Kubernetes' own DNS resolves the
  `<service>.<namespace>.svc` short form through exactly that path. A
  single-label check does not see either name as a risk; the cluster's
  resolver does. **Revision history on this one bypass, because it changed
  twice:** the first draft's single-label rule missed it (three labels). A
  second draft (2026-09-29) replaced the single-label rule with "fewer than
  two dots," which caught `kubernetes.default.svc` (only by widening the net
  far past what the bypass needed) but ALSO refused every bare apex domain a
  browser agent might target (`github.com`, one dot). **The owner declined
  that trade on 2026-09-29** and asked for the narrow fix instead: keep the
  single-label rule, and add `.svc` to the refused-suffix list next to
  `.internal`, `.local` and `.localhost`. That is what ships below. It also
  does not pretend to close the general `ndots:5` gap for an arbitrary
  short name -- see the paragraph after the rule for what does.

**The fix is one check before anything else runs, not four patches.** Every
bypass above turns on `url_refusal` accepting a host character, or a host
shape, that Chromium's own parser would not read as plain ASCII. So the
rule now normalises nothing and instead refuses anything that is not
already plain ASCII host syntax, before the rest of the checks run: a
backslash anywhere in the URL, and a host that is not `[a-z0-9.-]+` once
lower-cased -- which is refused whether it got there by a literal
backslash, a literal percent sign, or a literal non-ASCII character, so
those three bypasses close with one rule instead of three. The alternative
this entry considered -- IDNA/UTS46-normalise the host, then require
`[a-z0-9.-]+` of the *normalised* form, so a legitimately internationalised
domain still passes -- was not chosen: it accepts more real hosts, but it
also means the two representations (raw and normalised) must be kept in
step with whatever `agent_worker/runners/browser.py`'s own Chromium build
does, forever. Refusing raw non-ASCII outright means a caller whose target
is a real IDN sends the ASCII (punycode) form, which every such site
already answers to.

The rule, as written in the diff, now refuses, **in this order**:

* a URL over 2048 characters;
* a space, a control character, or any character outside 0x20-0x7E anywhere
  in the URL -- this alone is what closes the full-width and U+3002 bypass,
  because neither is ASCII;
* a backslash anywhere in the URL;
* any scheme but `http` and `https`;
* userinfo (`user:password@`);
* a host that, once lower-cased, is not `[a-z0-9.-]+` -- this is what closes
  the percent-encoded-host bypass, because `%` is not in that class, and it
  is what would also close a raw non-ASCII host if the character check above
  had not already caught it;
* a host whose last label does not start with a letter (`http://2852039166/`
  and `http://0xa9.254.169.254/`, which a browser reads as 169.254.169.254
  and `ipaddress` parses as neither; no public suffix starts with a digit);
* a host with an empty label (`a..b`, or a label produced by a leading or
  doubled `.`) -- added 2026-09-29 (minor, folded into this revision): the
  character-class check does not catch it, because `.` is itself allowed,
  and nothing downstream would otherwise notice a zero-length label;
* a host with a label made only of hyphens (`http://---.com/`) -- added
  2026-09-29 (minor): the previous message for this case ("ends in a
  number") was wrong and confusing, since a hyphen is not a digit; it now
  says plainly that an all-hyphen label is not a valid domain label;
* a host whose last label does not start with a letter, with two distinct
  messages now instead of one: a digit-led last label (`http://2852039166/`,
  `http://0xa9.254.169.254/`, which a browser reads as 169.254.169.254 and
  `ipaddress` parses as neither; no public suffix starts with a digit) says
  so; anything else that is not a letter (a hyphen, after the all-hyphen
  case above is already out of the way) says which character it found;
* **a single label** (`kubernetes`, `metadata`) -- restored 2026-09-29. A
  second draft of this rule (2026-09-29) had widened this to "fewer than two
  dots," to also catch the `.svc` bypass below; the owner declined that
  trade the same day, because it refused every bare apex domain
  (`github.com`, `example.com`) a browser agent's own purpose is to reach.
  The single-label form is restored, unchanged from the first draft, and
  costs nothing a real page address would ever need;
* any name under `.internal`, `.local`, `.localhost` or **`.svc`** (new
  2026-09-29, for `kubernetes.default.svc` / `swarm-api.swarm-system.svc` --
  see the bypass write-up above), checked after the ASCII and single-label
  rules, so a suffix check can no longer be the only thing standing between
  a bypass and a pass;
* a host that is an IP address and, after unwrapping an IPv4-mapped IPv6
  address or one of five other IPv6 forms that embed an IPv4 address --
  NAT64 well-known (`64:ff9b::/96`, RFC 6052), **NAT64 local-use
  (`64:ff9b:1::/48`, RFC 8215, new 2026-09-29)**, 6to4 (`2002::/16`, RFC
  3056), **SIIT / "IPv4-translated" (`::ffff:0:0:0/96`, RFC 6052 section
  2.1, new 2026-09-29)** and **IPv4-compatible, deprecated but still a
  parseable literal (`::/96`, new 2026-09-29)** -- down to the IPv4 address
  each embeds, is not global by this module's own explicit list of refused
  networks (below) -- **not `ipaddress.*.is_global`**, whose own membership
  changed between Python 3.11 and 3.13 (the CGNAT block and `192.0.0.0/24`
  both moved category more than once across that span). An explicit, named
  list is what a reviewer can diff against RFC 1918, 5735, 4291 and 6890
  directly, and it does not move under this module on a Python upgrade the
  platform did not choose for this reason;
* the two Google VIP ranges, which are global addresses by any definition
  but are refused anyway (see above).

**This is not the general `ndots:5` fix, and does not claim to be.** The
`.svc` suffix closes the one specific, named bypass above -- a fixed string,
not an open-ended shape -- but GKE's search path can in principle try any
name under five dots against every search domain, and nothing in this
function sees what the pod's resolver eventually does with a name it
accepted. What actually closes that gap: the worker's NetworkPolicy (which
does not depend on what a name resolves to at all), the pod's own DNS
config -- **tracked as #341: set `ndots:1` and drop search domains**, which
removes the search-path trial rather than trying to enumerate every name
shape it could produce -- and the planned `context.route` guard (see
*Preconditions for the applying PR*). This entry's door check is one layer
among those three, not a substitute for the other two.

**Underscore.** A host carrying one (`a_b.example.com`) is refused by the
same `[a-z0-9.-]+` character class as any other character outside it, with
no separate rule needed: RFC 952/1035 never allowed an underscore in a
hostname label, so refusing it costs nothing a valid page address needs.

**Required for the applying PR: a test table that runs every refusal above,
and every bypass this review found, through a real WHATWG URL parser, not
only `urlsplit`.** A test that constructs its expected value with `urlsplit`
and checks `url_refusal` against that same `urlsplit` output proves nothing
about Chromium; it is the rule re-checking itself in a mirror. The applying
PR's test table parses each case with a WHATWG-conformant parser -- for
example Node's own `URL` (the runtime the plugin already ships with), driven
from the test as a subprocess, or a maintained Python WHATWG binding if one
is vendored -- and asserts `url_refusal`'s verdict agrees with what that
parser resolves the host to, for every case in this section, including the
four original bypasses, the `.svc` bypass, and all six embedded-IPv4 forms
(IPv4-mapped, NAT64 well-known, NAT64 local-use, 6to4, SIIT, IPv4-compatible).

**Why not https-only, decided.** The scheme is not the SSRF vector. The
metadata server and every private address are plain http either way, and
the rule above refuses them by host. The runner already sets
`ignore_https_errors=False`, so an https page with a bad certificate still
fails. Refusing http would refuse legitimate plain-http targets and close no
path. **The owner confirmed on 2026-09-29: no https-only rule.** It is not
listed as an open decision below any more.

**IDN, decided.** A caller whose real target is an internationalised domain
sends its ASCII (punycode) form; this entry does not add a separate IDN
allowance or normalisation path. **The owner confirmed on 2026-09-29:
accepted as written**, alongside declining the https-only rule above.

**Why no allow-list.** A platform-wide list contradicts the profile's purpose
and the NetworkPolicy's stated design. A per-tenant allow-list is tenant
configuration, not catalogue. If it is ever wanted, it belongs with the
tenant's registration, not in `RunnerInput`.

**Not in this request, and recommended as its own worker issue:** a
request-level guard in the runner, `context.route("**/*", ...)`, that aborts
any request, including redirects and subresources, whose host fails
`url_refusal`. It still cannot see DNS answers. That makes it the second
layer behind the network, not a replacement for it. **File it as its own
worker issue rather than folding it into this request**, because
`context.route` is the only layer in this entire path that sees a redirect
or a page's subresources; the door check above never does, whichever way its
own bugs are fixed. Until that issue lands, a page reachable at a refused URL
that itself redirects, or loads a subresource from one, is not caught by
anything this request adds.

### Question 2 (#218): a key named `command`

**#218's own text**, read directly on 2026-09-29 (an earlier draft of this
entry could not read the issue and reconstructed this question from where
this repository quotes it; that reconstruction is replaced here): "A
declared input NAMED `command` reads as invariant 10 relaxed, although it
names a catalogue entry, not an argv. `FORBIDDEN_CALLER_FIELDS` in
`swarm_api/validation.py` and `_NEVER` in
`tests/unit/mcp/test_runner_inputs.py` both list `command`." Request 25
recorded the same concern in its own words before #218 was filed: "a
declared input named `command` would read as invariant 10 relaxed although
it names a catalogue entry, not an argv."

**Recommendation: keep the name, and narrow the guard rather than remove
it.** `command` is what every existing caller sends, including the example
in `docs/workflows.md`, the Submit form and the runner's own error text. The
declaration makes the difference #218 asks about visible instead of hiding
it: `choices` is the closed list of catalogue names, and `describe()`
renders it as `string, one of make | npm-build | ...`. A reader sees a menu,
not an argv. Renaming it to `catalogue_command` would break every caller to
address something a reviewer can already see.

**What this means for the two guards #218 names, checked directly against
their source on 2026-09-29:**

* **`_NEVER` in `tests/unit/mcp/test_runner_inputs.py` (line 52) lists
  `command`, and `test_only_the_mock_declares_inputs_and_no_declaration_
  names_execution_detail` (line ~314) asserts, for **every** profile in
  `RUNNER_PROFILES`, that none of its declared keys intersect `_NEVER`. That
  is invariant 10's own test, and it is written to fail the moment any
  profile declares `command` -- this request must say so explicitly, because
  the diff below makes it fail as written. **This entry does not ask for
  `command` to be removed from `_NEVER`.** It asks for one narrow, named
  exemption: a declared key named `command` is allowed **only** for the
  `generic` profile, and **only** when its `RunnerInput` is `kind="string"`
  with `choices` exactly equal to `GENERIC_COMMANDS`. Any other profile
  declaring `command`, any other kind, or a `command` whose `choices` is not
  exactly that closed list stays refused by the same guard it is refused by
  today. The applying PR narrows the test's assertion to express that
  exemption in code, rather than deleting `command` from `_NEVER` or
  weakening the assertion for every profile.
* **`FORBIDDEN_CALLER_FIELDS` in `swarm_api/validation.py` (line 41) also
  lists `command`, but is unaffected by this request and needs no change.**
  It refuses fields on the **top-level task body** (`image`, `command`,
  `resources`, and the rest of invariant 10's list, checked in
  `swarm_api/main.py` before a runner profile is even resolved). A
  `generic` agent's declared `command` lives under `input`, a separate
  namespace `FORBIDDEN_CALLER_FIELDS` does not read and has never read --
  `mock`'s existing declarations already put keys under `input` today
  without needing a change there. Declaring `input.command` for `generic`
  touches neither this list nor its call site.

### Question 3 (#218): should the plugin's bridge send inputs to a profile that has not declared?

**#218's own text**, read directly on 2026-09-29: "Once declared, the
plugin's bridge reads the same field, so `swarm_dispatch` could send a
browser task's `actions` for the first time. That is a new plugin
capability, not only a narrowing." An earlier draft of this entry could not
read #218 and reconstructed this question instead from the places in this
repository that quote it -- request 25's amendment ("Letting it send a
browser task's `actions` is #218's third question"), the header of
`apps/swarm-mcp/swarm_mcp/profiles.py`, and `check_inputs`'s docstring. Read
directly, the issue's wording matches all three; no further check against
the issue's text is needed.

**Recommendation: no. Keep the send policy, and declare instead.** This
request fixes #220's failure by giving `browser` a declaration. No bridge
exception is needed. The bridge sends a key only when a declaration names it,
because the declaration gives `--input` a kind to type the value by, and it
lets the bridge refuse a bad value before anything travels. Letting it send
unchecked keys to a `None` profile would forward, from the plugin, exactly the
unchecked input this request exists to close.

Once `browser` declares, `swarm_dispatch`'s `inputs` object carries `url`
and `actions` as JSON with no change. `swarm dispatch --input` needs one line:
`parse_input_flags` keeps `string` and `filename` values as typed text, and
must keep `url` and `argument` values the same way. Otherwise
`--input target=123` would be parsed as the number 123 and refused. A list is
already read as JSON (`--input 'actions=[{"type":"screenshot"}]'`).

With the diff below applied there is no `None` profile left, so the policy
stops being a live question.

### The requested change

The exact diff against `apps/common/swarm_common/profiles.py` at `d4a2352`.
**Not applied.** Revised 2026-09-29 after a security review; this version
re-verified with `git apply --check` against the current file (still
`d4a2352`; nothing has touched `profiles.py` since) and with a standalone
run of `url_refusal` and `check_inputs` against every case this entry names,
including the four bypasses -- both done outside this repository's own test
suite, since nothing here runs `pytest`. The applying PR still owes the real
test files listed under *Downstream restatements* and the WHATWG-parser
table above; this only proves the diff parses, applies and behaves as this
entry claims.

* **Kinds:** `url`, `argument`, `list`, `object`, and **`header`** (added in
  this revision, for `user_agent`): printable ASCII only, bounded like a
  list, by length.
* **New `RunnerInput` fields:** `choices`, `required`, `items`, `variants`.
  `required` on a list's `items` is now refused in `__post_init__`: an
  element of a list is always present, so the flag had nothing to say on one.
* **`string` may now also give a `maximum`** (a length bound in characters,
  `minimum` defaulting to 0), for `selector`, `text` and `key`: 4 KiB each,
  the runner's own content otherwise unconstrained.
* **A shared rule:** `url_refusal`, rewritten in this revision to refuse
  before parsing rather than after: a backslash or non-ASCII character
  anywhere, and a host that is not `[a-z0-9.-]+` once lower-cased, checked
  before the scheme, the dot count or the suffix list. See the entry's prose
  above for the four bypasses this closes and the explicit refused-network
  lists that replace `is_global`.
* **`browser` and `generic` declare** the tables above. `generic`'s
  `command` is `RunnerProfile.__post_init__`'s one exemption from `_NEVER`,
  enforced in code, not only in prose: a string whose `choices` are exactly
  `_GENERIC_COMMANDS`, for `generic` only.
* **`generic`'s `timeout_seconds` ceiling is read off the profile itself**
  (`_GENERIC_PROFILE.timeout_seconds`, built before `_GENERIC_INPUTS` and
  attached to `RUNNER_PROFILES["generic"]` with `dataclasses.replace`),
  not restated as a second literal.
* **`required` is enforced at the top level** by `check_inputs`.
* **The field returns to the type request 25 was accepted with.**
  `RunnerProfile.inputs` becomes `Mapping[str, RunnerInput]` with no `None`,
  and `check_inputs` loses its `None` branch. That retires the amendment.
* **`describe()` prints whole-number bounds in full** (`1..33554432`, not
  `:g`'s `1..3.35544e+07`). The mock's bounds render unchanged.
* **The article** reads "a url".

CR 28 (`issue` for `claude-code` and `codex`), accepted and not yet applied
on `main`, touches neither these lines nor these kinds.

```diff
--- a/apps/common/swarm_common/profiles.py
+++ b/apps/common/swarm_common/profiles.py
@@ -19,12 +19,15 @@
 
 from __future__ import annotations
 
+import ipaddress
 import math
+import re
 from collections.abc import Mapping
-from dataclasses import dataclass, field
+from dataclasses import dataclass, field, replace
 from enum import Enum
 from types import MappingProxyType
 from typing import Any
+from urllib.parse import urlsplit
 
 
 class Backend(str, Enum):
@@ -98,13 +101,35 @@
 # choose the model a `claude-code` agent runs. So what may be sent is declared
 # per profile, here, once. swarm-api refuses anything else at submission and
 # the plugin's bridge refuses it sooner; both read this, and neither keeps a
-# table of its own. Two profiles, `browser` and `generic`, have not declared
-# yet (#218), and the API bounds what is sent to them by size alone.
+# table of its own. Every profile declares: `browser` and `generic` last, by
+# contract request 32 (#218), which added the kinds their inputs needed.
 
 #: The kinds an input can be. `filename` is a bare file name with no
 #: directory: the worker keeps only the last path segment of an artifact name
 #: (`RunnerContext.artifact_path`), so `../x` would quietly become `x`.
-INPUT_KINDS = ("number", "integer", "boolean", "string", "filename")
+#:
+#: Added by contract request 32 (#218), for the two runners whose work IS
+#: their input:
+#:
+#: * `url`: an http or https URL a browser may open. See `url_refusal`.
+#: * `argument`: a value the generic runner appends to a catalogue argv, or
+#:   runs in -- `_ARGUMENT_SAFE` and the `..` refusal in
+#:   `agent_worker/runners/generic.py`, restated because the catalogue cannot
+#:   import the worker. Existence and "inside the workspace" stay the
+#:   runner's: only it has the workspace.
+#: * `list`: a JSON array. Its BOUNDS ARE ITS LENGTH, both required, as a
+#:   number's are; every element is `items`, and `items` may not itself be
+#:   `required` -- an element of a list is always present, so that flag on an
+#:   element has nothing to say.
+#: * `object`: a JSON object in one of the fixed shapes `variants` names,
+#:   chosen by its `type` key. A key its shape does not name is refused, as a
+#:   key a profile does not declare is.
+#: * `header`: a string that is, or could become, an HTTP header value:
+#:   printable ASCII only (0x20-0x7E), which by construction rules out CR and
+#:   LF and every other control character, so a caller cannot use it to
+#:   inject a second header. `maximum` is required, as it is for a list: the
+#:   bound is the string's length in characters.
+INPUT_KINDS = ("number", "integer", "boolean", "string", "filename", "url", "argument", "list", "object", "header")
 
 #: The signed 64-bit range, which is what Firestore stores an integer in.
 #: Python reads a JSON integer of any length, so an integer input whose bounds
@@ -113,16 +138,282 @@
 INT64_MIN = -(2**63)
 INT64_MAX = 2**63 - 1
 
+#: `argument`: what `generic._ARGUMENT_SAFE` accepts, restated because the
+#: catalogue cannot import the worker. No leading dash, so no value becomes a
+#: flag; no leading slash; 256 characters at most.
+_ARGUMENT = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}")
+
+#: The longest URL a browser task may name. Chromium's own limit is far
+#: larger; a task's URL is stored with the task and shown with it, and past
+#: this it is not a page address but a payload.
+_URL_MAX_CHARS = 2048
+
+#: A host, once `urlsplit` has extracted it and this module has lower-cased
+#: it, must be exactly this: letters, digits, `.` and `-`. Nothing else is
+#: plain ASCII host syntax, so this single check is what closes a
+#: percent-encoded host (`interna%6cl`), a host carrying a literal backslash,
+#: and a raw non-ASCII host at once -- see `url_refusal` and the entry's own
+#: prose for the three bypasses a security review found here on 2026-09-29.
+_HOST_CHARS = re.compile(r"[a-z0-9.-]+")
+
+#: Every network `url_refusal` refuses a *global* IPv4 address in, besides
+#: RFC 1918 and the rest of the ranges `ipaddress` itself would already
+#: refuse as non-global. Chosen as an explicit, named list instead of relying
+#: on `ipaddress.IPv4Address.is_global`, because that property's own
+#: membership is not pinned across the versions this platform runs: the
+#: CGNAT block (100.64.0.0/10) and 192.0.0.0/24 have both changed category
+#: in `ipaddress` between Python 3.11 and 3.13. An explicit list is what a
+#: reviewer can diff against RFC 1918, 5735 and 6890 directly, and it does
+#: not move under this module on a Python upgrade the platform did not make
+#: for this reason.
+_URL_REFUSED_V4_NETWORKS = (
+    ipaddress.ip_network("0.0.0.0/8"),  # "this network" (RFC 791)
+    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
+    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT, RFC 6598
+    ipaddress.ip_network("127.0.0.0/8"),  # loopback
+    ipaddress.ip_network("169.254.0.0/16"),  # link-local, and the metadata server
+    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
+    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
+    ipaddress.ip_network("192.0.2.0/24"),  # documentation (TEST-NET-1)
+    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
+    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
+    ipaddress.ip_network("198.51.100.0/24"),  # documentation (TEST-NET-2)
+    ipaddress.ip_network("203.0.113.0/24"),  # documentation (TEST-NET-3)
+    ipaddress.ip_network("224.0.0.0/4"),  # multicast
+    ipaddress.ip_network("240.0.0.0/4"),  # reserved
+    ipaddress.ip_network("255.255.255.255/32"),  # limited broadcast
+)
+
+#: The IPv6 equivalent of `_URL_REFUSED_V4_NETWORKS`, for a literal IPv6 host
+#: that is neither IPv4-mapped nor a NAT64/6to4 embedding (both unwrapped to
+#: an IPv4 address and checked against the list above instead; see
+#: `_embedded_v4`).
+_URL_REFUSED_V6_NETWORKS = (
+    ipaddress.ip_network("::1/128"),  # loopback
+    ipaddress.ip_network("::/128"),  # unspecified
+    ipaddress.ip_network("100::/64"),  # discard-only, RFC 6666
+    ipaddress.ip_network("2001:db8::/32"),  # documentation
+    ipaddress.ip_network("fc00::/7"),  # unique local
+    ipaddress.ip_network("fe80::/10"),  # link-local
+    ipaddress.ip_network("ff00::/8"),  # multicast
+)
+
+#: Private Google Access, which the worker's NetworkPolicy opens
+#: (kubernetes/network-policies/allow-egress.yaml, rule 3). These are global
+#: addresses by any definition, so they are refused explicitly rather than by
+#: `is_global`: the pod can reach them, but they are authenticated Google
+#: APIs, useless to a browser task without a token, and not what "the public
+#: internet" is meant to include.
+_URL_REFUSED_NETWORKS = (
+    ipaddress.ip_network("199.36.153.4/30"),
+    ipaddress.ip_network("199.36.153.8/30"),
+)
+
+#: Every IPv6 range that EMBEDS an IPv4 address, so a refused v4 address
+#: reachable through any of them would otherwise pass
+#: `_URL_REFUSED_V6_NETWORKS` unseen. IPv4-mapped (`::ffff:a.b.c.d`) is
+#: handled separately by `ipaddress.IPv6Address.ipv4_mapped`; the rest are
+#: unwrapped by `_embedded_v4`:
+#: * `64:ff9b::/96` -- NAT64, RFC 6052 (well-known prefix).
+#: * `64:ff9b:1::/48` -- NAT64, RFC 8215 (local-use prefix; the embedding
+#:   follows RFC 6052 section 2.2's PL48 layout: 48-bit prefix, 16 bits of
+#:   v4, an 8-bit zero field, 16 more bits of v4, 40-bit suffix).
+#: * `2002::/16` -- 6to4, RFC 3056.
+#: * `::ffff:0:0:0/96` -- "IPv4-translated", RFC 6052's SIIT form
+#:   (`::ffff:0:a.b.c.d`; note the extra `:0:` before the address, which is
+#:   what distinguishes it from IPv4-mapped).
+#: * `::/96` -- IPv4-compatible, deprecated (RFC 4291 says so; RFC 6540 says
+#:   not to originate or accept it) but still a parseable literal
+#:   (`::a9fe:a9fe`), so still refused here explicitly rather than assumed
+#:   gone.
+_URL_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
+_URL_NAT64_LOCAL_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")
+_URL_6TO4_PREFIX = ipaddress.ip_network("2002::/16")
+_URL_SIIT_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")
+_URL_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
+
+#: Names that are never a public page: GKE's metadata server is
+#: `metadata.google.internal`; `.local`/`.localhost` never leave the host or
+#: the cluster (`cluster.local`); and `.svc` is the short, no-FQDN-suffix
+#: form Kubernetes' own DNS resolves for a Service
+#: (`<service>.<namespace>.svc`, e.g. `kubernetes.default.svc`), which the
+#: pod's search path completes to `.svc.cluster.local` for any name under
+#: five dots (`ndots:5`) -- added 2026-09-29, after the owner found the
+#: dot-count rule this replaced still admitted it. See `url_refusal`'s
+#: docstring for what actually closes the general search-path gap; this
+#: suffix closes only the specific, well-known `.svc` shorthand.
+_URL_REFUSED_SUFFIXES = (".internal", ".local", ".localhost", ".svc")
+
 #: How much of a refused value a refusal repeats. A caller who sent three
 #: hundred digits needs the bound, not the digits back.
 _SHOWN_VALUE_CHARS = 40
 
 
+def _bound(value: float) -> str:
+    """`33554432`, not `3.35544e+07`: a caller copies a bound, and `:g` rounds it."""
+    return str(int(value)) if float(value).is_integer() else f"{value:g}"
+
+
+def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
+    """The IPv4 address a NAT64, 6to4, SIIT or IPv4-compatible address
+    embeds, or None. IPv4-mapped is handled by the caller
+    (`address.ipv4_mapped`) and never reaches here."""
+    packed = address.packed
+    if address in _URL_NAT64_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    if address in _URL_NAT64_LOCAL_PREFIX:
+        # RFC 6052 section 2.2, PL48: prefix(6 bytes) + v4-hi(2) + u(1, zero)
+        # + v4-lo(2) + suffix(5). The `u` byte at packed[8] is skipped.
+        return ipaddress.IPv4Address(bytes([packed[6], packed[7], packed[9], packed[10]]))
+    if address in _URL_6TO4_PREFIX:
+        return ipaddress.IPv4Address(packed[2:6])
+    if address in _URL_SIIT_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    if address in _URL_IPV4_COMPATIBLE_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    return None
+
+
+def url_refusal(value: str) -> str:
+    """Why `value` is not a URL a browser task may open, or "" when it is.
+
+    One home for the rule, so swarm-api, the plugin's bridge and the browser
+    runner (`_check_url`, which today checks the scheme and that there is a
+    host) give one answer. Not one of #218's three questions -- declaring
+    `url` as a kind at all raises it; see the entry's prose in
+    docs/contract-change-requests.md for why, and for four bypasses a
+    security review found and closed here on 2026-09-29.
+
+    THIS IS NOT THE SSRF CONTROL, AND CANNOT BE. It sees the URL a caller
+    typed, never the page's redirects, its subresources, its script's
+    requests, or what a name resolves to when the pod asks. The control is the
+    network: the worker's NetworkPolicy drops every private range but the
+    metadata server, and the metadata server answers only a request carrying
+    `Metadata-Flavor: Google`, which a navigation does not send. What this
+    buys is a 422 naming the reason, instead of a task that waits out
+    `timeout_ms` against an address the network silently drops (Dataplane V2
+    drops; the sender sees a timeout), and a URL with a password in it that
+    is never stored with the task, served by the API or shown in the UI.
+
+    Refuses rather than normalises. Every check below runs on the raw string
+    or on `urlsplit`'s own view of it: a host that is not already plain ASCII
+    (`[a-z0-9.-]+` once lower-cased) is refused outright rather than
+    IDNA/UTS46-normalised and re-checked, because a normalising rule has to
+    track whatever WHATWG host-parsing Chromium does, forever, while a
+    refusing rule only has to be a subset of what Chromium accepts. A caller
+    whose real target is an internationalised domain sends its ASCII
+    (punycode) form, which the site already answers to -- the owner accepted
+    this as the rule on 2026-09-29 (no separate IDN allowance).
+
+    An underscore is refused by the same `[a-z0-9.-]+` check as any other
+    character outside that set, with no separate rule: RFC 952/1035 do not
+    allow one in a hostname label at all (some internal DNS -- SRV records,
+    `_service._proto.name` -- uses one anyway, which is one more reason a
+    browser task should not be handed a host carrying one).
+
+    WHAT THIS DOES NOT DO, as of the owner's decision on 2026-09-29: it does
+    not refuse a host by dot count. An earlier revision refused any non-IP
+    host with fewer than two dots, which caught `kubernetes.default` and
+    `swarm-api.swarm-system` but also every bare apex domain a browser task
+    might legitimately target (`github.com`, `example.com` both have exactly
+    one dot) -- for a browser profile, refusing those is refusing the
+    profile's main use. It was also incomplete on its own terms: a three-label
+    name ending in `.svc` (`kubernetes.default.svc`) has two dots and was
+    never caught by it either. The general gap -- GKE's `ndots:5` pod
+    resolver tries every search domain before the absolute name for anything
+    under five dots -- is NOT closed by any check in this function, and
+    cannot be from here: it sees the string the caller sent, never what the
+    pod's resolver does with it. The real controls are the worker's
+    NetworkPolicy (which does not depend on what a name resolves to), the
+    pod's own DNS config (tracked as #341: set `ndots:1` and drop search
+    domains, which removes the search-path trial entirely rather than
+    guessing at every name shape it could produce), and the planned
+    `context.route` guard (see the entry's *Preconditions*). What this
+    function still refuses is the single-label case (`kubernetes`,
+    `metadata`), which resolves only through the cluster's search path and
+    is never a real page address, and the specific `.svc` shorthand below,
+    which is the one case named in the review that is also a fixed, known
+    string rather than an open-ended shape.
+    """
+    if len(value) > _URL_MAX_CHARS:
+        return f"it is longer than {_URL_MAX_CHARS} characters"
+    if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
+        return "it contains a space, a control character or a non-ASCII character"
+    if "\\" in value:
+        return "it contains a backslash, which a browser treats as a host or path separator"
+    try:
+        parsed = urlsplit(value)
+        parsed.port  # noqa: B018 -- raises ValueError on a port out of range
+    except ValueError:
+        return "it is not a URL"
+    if parsed.scheme not in ("http", "https"):
+        return "only http and https are opened"
+    if parsed.username is not None or parsed.password is not None:
+        return "it carries credentials, which would be stored with the task and shown with it"
+    host = (parsed.hostname or "").rstrip(".")
+    if not host:
+        return "it has no host"
+    # An IP LITERAL IS CHECKED BEFORE THE HOST-CHARACTER RULE, not after: an
+    # IPv6 literal's `hostname` is unbracketed and colon-bearing
+    # (`64:ff9b::808:808`), which `_HOST_CHARS` never matches, and `ipaddress`
+    # itself already rejects a backslash, a percent sign or a non-ASCII
+    # character in an address -- there is nothing left for a second charset
+    # check to catch there.
+    try:
+        address = ipaddress.ip_address(host)
+    except ValueError:
+        address = None
+    if address is None:
+        if not _HOST_CHARS.fullmatch(host):
+            return (
+                "its host is not letters, digits, '.' and '-' once lower-cased -- a "
+                "browser's own host parsing accepts more than this, including a "
+                "percent-encoded or backslash-bearing host, and this module refuses "
+                "rather than reproduces it"
+            )
+        labels = host.split(".")
+        if any(label == "" for label in labels):
+            return "its host has an empty label ('..' in it, or it starts or ends with '.')"
+        if any(label.strip("-") == "" for label in labels):
+            return "its host has a label made only of hyphens, which is not a valid domain label"
+        # A browser reads a host whose last label is a number (`2852039166`,
+        # `0xa9.254.169.254`) as an IPv4 address in another notation, which
+        # `ip_address` does not parse. No public suffix starts with a digit.
+        if labels[-1][:1].isdigit():
+            return "its host ends in a number, which a browser reads as an address"
+        if not labels[-1][:1].isalpha():
+            return f"its host's last label starts with {labels[-1][:1]!r}, not a letter"
+        # SINGLE LABEL ONLY, not a general dot-count rule: see the docstring
+        # above for why the broader rule this replaced was both too costly
+        # (it refused bare apex domains) and still incomplete.
+        if len(labels) < 2:
+            return "its host is a single label, which only the cluster's search path resolves"
+        if host.endswith(_URL_REFUSED_SUFFIXES):
+            return "its host is a cluster or node-local name"
+        return ""
+    if isinstance(address, ipaddress.IPv6Address):
+        if address.ipv4_mapped is not None:
+            address = address.ipv4_mapped
+        else:
+            embedded = _embedded_v4(address)
+            if embedded is not None:
+                address = embedded
+    if isinstance(address, ipaddress.IPv4Address):
+        if any(address in net for net in _URL_REFUSED_V4_NETWORKS) or any(
+            address in net for net in _URL_REFUSED_NETWORKS
+        ):
+            return "its host is not a public address"
+    elif any(address in net for net in _URL_REFUSED_V6_NETWORKS):
+        return "its host is not a public address"
+    return ""
+
+
 class InputRefused(ValueError):
     """An input a profile does not accept, and why.
 
     `key` is the first key refused and `keys` every one; `expected` is the
-    declared bound a value broke, or None when the key is not declared at all.
+    declared bound a value broke, or None when the key is not declared at all
+    or is required and was not sent.
     The message names the key and the bound, and never repeats a value longer
     than it has to.
     """
@@ -165,18 +456,73 @@
     #: the platform would read it as instead -- the mock's exit codes 77, 78
     #: and 143. Pairs, not a dict, so the declaration stays hashable.
     refused: tuple[tuple[Any, str], ...] = ()
+    #: A `string` must be one of these, when any are named: a name from a
+    #: catalogue the runner owns, such as the generic runner's commands.
+    choices: tuple[str, ...] = ()
+    #: The caller must send this key. Read for a profile's keys and for an
+    #: object's fields. Refused on a list's `items`: an element of a list is
+    #: always present, so `required` on it has nothing to say.
+    required: bool = False
+    #: What each element of a `list` must be.
+    items: RunnerInput | None = None
+    #: An `object`'s shapes: the value of its `type` key, to the fields that
+    #: shape takes besides `type`. Excluded from the hash, as
+    #: `RunnerProfile.inputs` is; frozen read-only below.
+    variants: Mapping[str, Mapping[str, RunnerInput]] | None = field(default=None, hash=False)
 
     def __post_init__(self) -> None:
         if self.kind not in INPUT_KINDS:
             raise ValueError(f"input kind {self.kind!r} is not one of {INPUT_KINDS}")
         numeric = self.kind in ("number", "integer")
-        if not numeric and (self.minimum is not None or self.maximum is not None or self.refused):
-            raise ValueError(f"a {self.kind} input has no bounds; only a number or an integer does")
-        if numeric and (self.minimum is None or self.maximum is None):
+        #: `list` and `header` MUST give both bounds, like a number: without
+        #: them a list or a header string is unbounded until the task write
+        #: fails. `string` MAY give a `maximum` -- a length bound in
+        #: characters, for keys such as `selector`, `text` and `key`, whose
+        #: content the runner does not otherwise constrain -- and when it
+        #: does, an omitted `minimum` defaults to 0 rather than being
+        #: required, because most bounded strings have no meaningful floor.
+        strictly_bounded = numeric or self.kind in ("list", "header")
+        length_boundable = strictly_bounded or self.kind == "string"
+        if not length_boundable and (self.minimum is not None or self.maximum is not None):
             raise ValueError(
+                f"a {self.kind} input has no bounds; only a number, a list, a header or a "
+                "string does"
+            )
+        if not numeric and self.refused:
+            raise ValueError(f"a {self.kind} input refuses no values; only a number does")
+        if strictly_bounded and (self.minimum is None or self.maximum is None):
+            raise ValueError(
                 f"a {self.kind} input declares both a minimum and a maximum; without "
-                "one, a JSON number of any size passes and fails at the store instead"
+                "one, a JSON number -- or a string, for a header -- of any size passes "
+                "and fails at the store instead"
             )
+        if self.kind == "string" and self.maximum is not None and self.minimum is None:
+            object.__setattr__(self, "minimum", 0.0)
+        if self.kind in ("list", "header") and not (
+            float(self.minimum).is_integer() and float(self.maximum).is_integer() and self.minimum >= 0
+        ):
+            raise ValueError(f"a {self.kind}'s bounds are its length: whole numbers, from 0")
+        if self.choices and self.kind != "string":
+            raise ValueError(f"a {self.kind} input has no choices; only a string does")
+        if (self.kind == "list") != isinstance(self.items, RunnerInput):
+            raise ValueError("a list input names what its elements are, and nothing else does")
+        if self.kind == "list" and self.items is not None and self.items.required:
+            raise ValueError(
+                "a list's items are always present; `required` on them is refused"
+            )
+        if (self.kind == "object") != bool(self.variants):
+            raise ValueError("an object input names its shapes, and nothing else does")
+        if self.variants:
+            frozen: dict[str, Mapping[str, RunnerInput]] = {}
+            for shape, fields in self.variants.items():
+                for name, declared in fields.items():
+                    if name == "type" or not isinstance(declared, RunnerInput):
+                        raise ValueError(
+                            f"shape {shape!r}: field {name!r} must be a RunnerInput, "
+                            "and `type` is the shape's name, not a field"
+                        )
+                frozen[shape] = MappingProxyType(dict(fields))
+            object.__setattr__(self, "variants", MappingProxyType(frozen))
         for bound in (self.minimum, self.maximum):
             if bound is not None and not math.isfinite(bound):
                 raise ValueError("an input's bounds must be finite numbers")
@@ -197,10 +543,16 @@
     def describe(self) -> str:
         """`integer 1..255 except 77, 78, 143`: the kind and the bound, as a caller reads it."""
         # `__post_init__` gives a number both bounds and anything else neither.
+        if self.kind == "list" and self.items is not None:
+            return f"list of {_bound(self.minimum)}..{_bound(self.maximum)}, each {self.items.describe()}"
+        if self.variants:
+            return f"object, `type` one of {' | '.join(self.variants)}"
         if self.minimum is not None and self.maximum is not None:
-            text = f"{self.kind} {self.minimum:g}..{self.maximum:g}"
+            text = f"{self.kind} {_bound(self.minimum)}..{_bound(self.maximum)}"
         else:
             text = self.kind
+        if self.choices:
+            text += f", one of {' | '.join(self.choices)}"
         if self.refused:
             text += " except " + ", ".join(str(value) for value, _ in self.refused)
         return text
@@ -208,7 +560,7 @@
     def check(self, key: str, value: Any) -> Any:
         """`value`, normalised (an integral float becomes an int), or InputRefused."""
         expected = self.describe()
-        article = "an" if expected[0] in "aeiou" else "a"
+        article = "an" if expected[0] in "aeio" else "a"  # "a url"
         wanted = f"input {key!r} must be {article} {expected}"
 
         def refuse(detail: str = "") -> InputRefused:
@@ -245,18 +597,64 @@
             if not isinstance(value, bool):
                 raise refuse(" (true or false)")
             return value
+        if self.kind == "list":
+            if not isinstance(value, list):
+                raise refuse()
+            if not self.minimum <= len(value) <= self.maximum:
+                raise refuse(f" -- it has {len(value)} entries")
+            # Named by position, so a refusal says WHICH element: `actions[3].url`.
+            return [self.items.check(f"{key}[{index}]", item) for index, item in enumerate(value)]
+        if self.kind == "object":
+            if not isinstance(value, Mapping):
+                raise refuse()
+            shape = value.get("type")
+            if not isinstance(shape, str) or shape not in self.variants:
+                raise refuse()
+            fields = self.variants[shape]
+            unknown = sorted(set(value) - set(fields) - {"type"})
+            if unknown:
+                raise refuse(f" -- a {shape!r} takes {sorted(fields) or 'no field'}, not {unknown}")
+            missing = sorted(name for name, spec in fields.items() if spec.required and name not in value)
+            if missing:
+                raise refuse(f" -- a {shape!r} needs {missing}")
+            checked = {
+                name: fields[name].check(f"{key}.{name}", value[name])
+                for name in sorted(value)
+                if name != "type"
+            }
+            return {"type": shape, **checked}
         if not isinstance(value, str):
             raise refuse()
         if self.kind == "filename" and (
             not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value
         ):
             raise refuse(" -- a bare file name, with no directory")
+        if self.kind == "argument" and (not _ARGUMENT.fullmatch(value) or ".." in value):
+            raise refuse(
+                " -- letters, digits, '.', '_', '-' and '/', starting with none of '-' or "
+                "'/', at most 256 characters, and no '..'"
+            )
+        if self.kind == "url":
+            reason = url_refusal(value)
+            if reason:
+                raise refuse(f" -- {reason}")
+        if self.kind == "header":
+            if not (self.minimum <= len(value) <= self.maximum):
+                raise refuse(f" -- it has {len(value)} characters")
+            if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
+                raise refuse(" -- printable ASCII only (0x20-0x7E), which rules out CR and LF")
+        if self.kind == "string" and self.maximum is not None and not (
+            self.minimum <= len(value) <= self.maximum
+        ):
+            raise refuse(f" -- it has {len(value)} characters")
+        if self.choices and value not in self.choices:
+            raise refuse()
         return value
 
 
-def _frozen_inputs(declared: Mapping[str, RunnerInput] | None) -> Mapping[str, RunnerInput] | None:
+def _frozen_inputs(declared: Mapping[str, RunnerInput]) -> Mapping[str, RunnerInput]:
     """A read-only copy, so no caller can widen a profile's declaration in place."""
-    return None if declared is None else MappingProxyType(dict(declared))
+    return MappingProxyType(dict(declared))
 
 
 @dataclass(frozen=True)
@@ -330,29 +728,39 @@
     #: That is what closes `input.model` on `claude-code`, which its runner
     #: would pass as `--model`.
     #:
-    #: NONE MEANS NOT DECLARED YET, and then only the input's size is bounded,
-    #: as it was for every profile before this field existed. It exists for
-    #: the runners whose work IS their input -- `browser` cannot start without
-    #: `url` or `actions`, `generic` without `command` -- where an empty
-    #: declaration would refuse every task they run. What they declare is the
-    #: owner's open question #218, recorded under contract request 25 in
-    #: docs/contract-change-requests.md as an amendment awaiting approval:
-    #: the request as accepted typed this field `Mapping`, with no None.
+    #: THERE IS NO "NOT DECLARED YET". Until contract request 32 (#218) this
+    #: field could be None, for `browser` and `generic`, and then only the
+    #: input's size was bounded -- an amendment to request 25 the owner
+    #: confirmed on 2026-09-26 as the state until #218 was decided. Both
+    #: declare now, so the type is the one request 25 was accepted with.
     #:
     #: Excluded from the hash: a mapping is not hashable, and a profile's
     #: identity is its name.
-    inputs: Mapping[str, RunnerInput] | None = field(default_factory=dict, hash=False)
+    inputs: Mapping[str, RunnerInput] = field(default_factory=dict, hash=False)
 
     def __post_init__(self) -> None:
-        if self.inputs is not None:
-            for key, declared in self.inputs.items():
-                if not isinstance(declared, RunnerInput):
-                    raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
-                if key == "prompt":
-                    raise ValueError(
-                        f"runner {self.name}: `prompt` is every profile's input and is not declared"
-                    )
-            object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
+        for key, declared in self.inputs.items():
+            if not isinstance(declared, RunnerInput):
+                raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
+            if key == "prompt":
+                raise ValueError(
+                    f"runner {self.name}: `prompt` is every profile's input and is not declared"
+                )
+            if key == "command" and self.name != "generic":
+                raise ValueError(
+                    f"runner {self.name}: `command` is invariant 10's own guard "
+                    "(_NEVER, FORBIDDEN_CALLER_FIELDS); only `generic` is exempted, "
+                    "and only for its closed catalogue -- see contract request 32"
+                )
+            if key == "command" and self.name == "generic" and (
+                declared.kind != "string" or set(declared.choices) != set(_GENERIC_COMMANDS)
+            ):
+                raise ValueError(
+                    f"runner {self.name}: `command` may only be a string whose choices "
+                    "are exactly GENERIC_COMMANDS -- the one exemption contract request "
+                    "32 asks _NEVER to carry, not a general licence to declare it"
+                )
+        object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
         if not self.available and not self.disabled_reason:
             raise ValueError(
                 f"runner {self.name}: a disabled profile must say why. A caller "
@@ -450,6 +858,194 @@
 }
 
 
+#: A wait the browser runner hands Playwright, in milliseconds. FROM 1, NOT 0:
+#: Playwright reads a timeout of 0 as "no timeout", so a 0 here would let one
+#: action wait out the whole 5400 s attempt. FIVE MINUTES at most: a selector
+#: that has not appeared in five minutes is not coming, and the attempt's own
+#: timeout is the ceiling above that.
+_BROWSER_WAIT_MS = {"minimum": 1, "maximum": 300_000}
+
+#: `selector`, `text` and `key` may carry arbitrary Unicode (a CSS selector, a
+#: page's own text, a key combination), so they are `string` with a length
+#: bound rather than `header`, which is ASCII-only. 4 KiB: far past any real
+#: selector or typed text, and small enough that a caller who sent this much
+#: sent a payload, not a selector.
+_BROWSER_TEXT_MAX = 4096
+
+#: The browser runner's actions (`agent_worker/runners/browser.py`, `body`),
+#: one shape per `type`, each field as the runner reads it. `type` is matched
+#: exactly: the runner lower-cases it, so `"Goto"` runs today and is refused
+#: here. The restatement is held to the runner's source by a test, as the
+#: mock's keys are (tests/unit/mcp/test_runner_inputs.py).
+_BROWSER_ACTION = RunnerInput(
+    "object",
+    means="one step of the run, in the shape its `type` names",
+    variants={
+        "goto": {
+            "url": RunnerInput("url", required=True, means="the page to open"),
+            "wait_until": RunnerInput(
+                "string",
+                choices=("load", "domcontentloaded", "networkidle", "commit"),
+                means="when the load counts as done; default load",
+            ),
+        },
+        "click": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to click",
+            )
+        },
+        "fill": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the field to fill",
+            ),
+            "text": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="what to type into it; default empty",
+            ),
+        },
+        "press": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to press a key in",
+            ),
+            "key": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="the key; default Enter",
+            ),
+        },
+        "wait_for": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to wait for",
+            ),
+            "timeout_ms": RunnerInput(
+                "integer", **_BROWSER_WAIT_MS, means="how long to wait; default the task's timeout_ms"
+            ),
+        },
+        # 0..60: the runner clamps above 60 with `min(..., 60.0)`, so a larger
+        # value is refused rather than quietly shortened.
+        "wait": {"seconds": RunnerInput("number", minimum=0, maximum=60, means="how long to pause; default 1")},
+        "screenshot": {
+            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
+            "full_page": RunnerInput("boolean", means="the whole page, not the viewport; default true"),
+        },
+        "extract": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="the element whose text is kept; default body",
+            ),
+            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
+        },
+    },
+)
+
+#: The browser runner's inputs. NEITHER `url` NOR `actions` IS REQUIRED ALONE:
+#: the runner needs one or the other, which `required` cannot say, so that
+#: refusal stays the runner's (contract request 32, *Owner's decisions*).
+_BROWSER_INPUTS: dict[str, RunnerInput] = {
+    "url": RunnerInput("url", means="opened first, before any action"),
+    # 200: the runner's MAX_ACTIONS.
+    "actions": RunnerInput(
+        "list", minimum=0, maximum=200, items=_BROWSER_ACTION, means="run in order, after `url`"
+    ),
+    "timeout_ms": RunnerInput(
+        "integer", **_BROWSER_WAIT_MS, means="how long any one action may take; default 30000"
+    ),
+    # Three minutes: Chromium starts in seconds, and a launch still waiting at
+    # three minutes is a pod short of /dev/shm, not a slow start.
+    "launch_timeout_ms": RunnerInput(
+        "integer", minimum=1, maximum=180_000, means="how long Chromium may take to start; default 60000"
+    ),
+    # Up to 4K. The viewport is rendered in the pod's memory, and a full-page
+    # screenshot of it is written to the workspace, which is memory too.
+    "viewport_width": RunnerInput("integer", minimum=320, maximum=3840, means="pixels; default 1280"),
+    "viewport_height": RunnerInput("integer", minimum=240, maximum=2160, means="pixels; default 900"),
+    # `header`, not `string`: this value is sent as the User-Agent HTTP
+    # header verbatim, so it must be printable ASCII -- a caller could
+    # otherwise inject a second header through it. 512: real User-Agent
+    # strings run under 300 characters; past 512 it is not a browser
+    # signature.
+    "user_agent": RunnerInput(
+        "header", minimum=0, maximum=512, means="the User-Agent sent; default Chromium's"
+    ),
+    "extract_text": RunnerInput("boolean", means="keep the final page's text as page.txt; default true"),
+    "screenshot": RunnerInput("boolean", means="keep a final full-page screenshot; default true"),
+}
+
+#: The generic runner's catalogue (`agent_worker/runners/generic.py`,
+#: `GENERIC_COMMANDS`), restated because the catalogue cannot import the
+#: worker, and held to it by a test, as `_ARGUMENT` is.
+_GENERIC_COMMANDS = ("make", "npm-build", "npm-ci", "npm-test", "pytest", "uv-sync")
+
+#: Built without `inputs` first, so `_GENERIC_INPUTS` below can read this
+#: profile's OWN `timeout_seconds` for its `timeout_seconds` input's ceiling
+#: instead of restating the number as a second literal. `RunnerProfile` sets
+#: no `timeout_seconds` for `generic`, so this is the class default (3600) --
+#: reading it here, rather than writing `3600` again, is what keeps the two
+#: from drifting if a future change gives `generic` its own value.
+_GENERIC_PROFILE = RunnerProfile(
+    name="generic",
+    image="agent-runtime-base",
+    resource_class="standard",
+    backend=Backend.CLOUD_RUN_JOB,
+    runner_argv=("python", "-m", "agent_worker.runners.generic"),
+    provider=None,
+)
+
+#: The generic runner's inputs (`agent_worker/runners/generic.py`).
+#:
+#: `command` IS A NAME, NOT AN ARGV: `choices` is `_GENERIC_COMMANDS`, the
+#: runner's own `GENERIC_COMMANDS` restated, whose argv are constants in the
+#: runner. Declaring it reads as invariant 10 relaxed and is not -- see
+#: contract request 32, Question 2 (#218) -- and `RunnerProfile.__post_init__`
+#: enforces the one exemption `_NEVER` (tests/unit/mcp/test_runner_inputs.py)
+#: is asked to carry: `command` declared as a string whose `choices` are
+#: exactly `_GENERIC_COMMANDS`, for `generic` only.
+#:
+#: THE FOUR LIMITS MAY ONLY LOWER THE PLATFORM'S (`runners/limits.py`). Each
+#: ceiling here is the value the worker exports by default -- the profile's
+#: `timeout_seconds`, and `WorkerConfig`'s grace and output caps -- so a
+#: request above it is refused at the door instead of accepted and clamped.
+#: The runner still clamps to what the attempt's worker exports, which an
+#: operator may have set lower. From 1: the runner reads 0 or less as "not
+#: asked", so a 0 was accepted and meant nothing.
+_GENERIC_INPUTS: dict[str, RunnerInput] = {
+    "command": RunnerInput(
+        "string",
+        required=True,
+        choices=_GENERIC_COMMANDS,
+        means="the platform catalogue entry to run; the platform owns its argv",
+    ),
+    # 32: `GenericCommand.max_arguments`. Read for `pytest` only.
+    "paths": RunnerInput(
+        "list",
+        minimum=0,
+        maximum=32,
+        items=RunnerInput("argument", means="an existing path inside the workspace"),
+        means="pytest only: what to run; default everything",
+    ),
+    "target": RunnerInput("argument", means="make only: the target; default all"),
+    "working_directory": RunnerInput(
+        "argument", means="a directory inside the workspace to run in; default the workspace"
+    ),
+    "timeout_seconds": RunnerInput(
+        "number", minimum=1, maximum=_GENERIC_PROFILE.timeout_seconds,
+        means="lowers the command's wall clock",
+    ),
+    "grace_seconds": RunnerInput(
+        "number", minimum=1, maximum=20, means="lowers the wait between SIGTERM and SIGKILL"
+    ),
+    "max_stdout_bytes": RunnerInput(
+        "integer", minimum=1, maximum=32 * 1024 * 1024, means="lowers the stdout kept"
+    ),
+    "max_stderr_bytes": RunnerInput(
+        "integer", minimum=1, maximum=8 * 1024 * 1024, means="lowers the stderr kept"
+    ),
+}
+
+
 RUNNER_PROFILES: dict[str, RunnerProfile] = {
     "mock": RunnerProfile(
         name="mock",
@@ -463,18 +1059,10 @@
         checkpoint_interval_seconds=30,
         inputs=_MOCK_INPUTS,
     ),
-    "generic": RunnerProfile(
-        name="generic",
-        image="agent-runtime-base",
-        resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
-        runner_argv=("python", "-m", "agent_worker.runners.generic"),
-        provider=None,
-        # NOT DECLARED YET. The runner cannot start without `input.command`,
-        # the NAME of an entry in its own catalogue, so an empty declaration
-        # would refuse every task it runs. See RunnerProfile.inputs.
-        inputs=None,
-    ),
+    # Built from `_GENERIC_PROFILE` (declared above, alongside `_GENERIC_INPUTS`,
+    # so the input's own `timeout_seconds` ceiling can read this profile's
+    # `timeout_seconds` instead of restating it).
+    "generic": replace(_GENERIC_PROFILE, inputs=_GENERIC_INPUTS),
     "claude-code": RunnerProfile(
         name="claude-code",
         image="agent-runtime-base",
@@ -528,10 +1116,7 @@
         provider="anthropic",
         secrets=("ANTHROPIC_API_KEY",),
         timeout_seconds=5400,
-        # NOT DECLARED YET. The runner cannot start without `input.url` or
-        # `input.actions`, so an empty declaration would refuse every task it
-        # runs, the smoke suite's GKE row included. See RunnerProfile.inputs.
-        inputs=None,
+        inputs=_BROWSER_INPUTS,
     ),
 }
 
@@ -556,23 +1141,10 @@
 
     `raw` holds the keys BESIDES the prompt. Returns them normalised (an
     integral float for an integer input becomes an int), or raises
-    InputRefused: for every key the profile does not declare, naming them all,
-    or for the first declared key whose value is out of its bounds, naming the
-    bound.
-
-    NOT DECLARED YET IS DECIDED HERE, AND ONLY HERE. A profile whose inputs are
-    not declared yet (`inputs is None`: `browser` and `generic`, open with the
-    owner on #218) has no declaration to check a key against, so `raw` comes
-    back as it was sent and only its size bounds it, which the caller that
-    measures the size enforces (`validate_input_size` in swarm-api). The review
-    of #213 found this decided twice and differently: this function refused
-    every key while the API returned before asking and accepted every key, so
-    the one rule and the API gave opposite answers for the same profile. The
-    plugin's bridge sending such a profile nothing is its own send policy, not
-    this rule (#218's third question).
+    InputRefused: for every key the profile does not declare, naming them all;
+    for every required key `raw` does not send, naming them all; or for the
+    first declared key whose value is out of its bounds, naming the bound.
     """
-    if profile.inputs is None:
-        return dict(raw)
     declared = profile.inputs
     unknown = sorted(set(raw) - set(declared))
     if unknown:
@@ -590,4 +1162,11 @@
                 f"so {unknown} cannot be sent"
             )
         raise InputRefused(message, key=unknown[0], keys=tuple(unknown))
+    missing = sorted(key for key, spec in declared.items() if spec.required and key not in raw)
+    if missing:
+        raise InputRefused(
+            f"runner profile {profile.name!r} needs {missing} in its input",
+            key=missing[0],
+            keys=tuple(missing),
+        )
     return {key: declared[key].check(key, raw[key]) for key in sorted(raw)}
```

### What it would break if accepted

* **Nothing stored.** Agents keep their `input`. The check runs at submission
  only.
* **Callers that send what the declaration refuses** would get 422
  `invalid_input` for input the API accepts today. That is the point, and it
  is a behaviour change to announce. Every case below fails or misbehaves at
  the runner today. The only exceptions are an upper-case `type`, a number
  sent as a string, and a limit above the ceiling: today those are coerced or
  clamped, and they would now be refused.
  * `generic` with no `command`, or a `command` outside the catalogue.
  * An action with an upper-case `type`, a missing `selector` or `url`, or a
    key its shape does not take.
  * `timeout_ms`, `launch_timeout_ms` or a viewport sent as a string or out
    of bounds, and `seconds` above 60.
  * A `goto` to a private, metadata, cluster or credentialled URL.
  * A `generic` limit of 0 or less, or above its ceiling.
* **In-repository callers were checked on 2026-09-29.**
  * `scripts/lib/testlib.sh` `profile_input` sends `browser` a `screenshot`
    action with `name` and `full_page` and `extract_text: false`. This
    passes the proposed catalogue.
  * `scripts/prove-gke-dispatch.sh --url https://example.com` passes.
  * `docs/workflows.md`'s example step `{"command": "pytest"}` passes.
  * `profile_input`'s default branch sends `generic` only a prompt. It would
    now be refused at 422 instead of failing at the runner. No script
    submits `generic` through that branch today.
* **Downstream restatements that must follow in the applying PR:**
  * The bridge: `parse_input_flags`'s string kinds (question 3). Its
    `not_yet` wording for a `None` profile is deleted.
  * swarm-api: `validation.validate_runner_input`'s and `runnerinputs.py`'s
    docstrings, which describe `None`. There is no code branch to remove;
    the rule decides.
  * The worker: `browser._check_url` calls `url_refusal`. The runner may keep
    its own checks, because an agent written before this change still reaches
    it.
  * The Submit form's `SUGGESTED` for `browser` and `generic`
    (`apps/swarm-ui/src/Submit.tsx`). Today it is a runner census. Section 13
    of `scripts/lib/check-contract-parity.sh` would then hold it to the
    declaration, as it does for the declared profiles.
  * The generated tables. `docs/workflows.md` and `plugin/README.md` gain
    `runner-inputs:browser` and `runner-inputs:generic` blocks, and
    `test_runner_input_prose.py` needs a rendering for `list` and `object`.
  * The tests that key on `inputs is None`.
    `test_runner_inputs.py::_UNDECLARED` skips itself ("#218 is settled").
    `test_runner_inputs_by_declaration.py` and
    `test_submit_offers_only_what_runners_read.py` assert the `None`
    behaviour and are rewritten for the declarations.
  * `apps/swarm-ui/src/types.ts`, if it ever mirrors `inputs`. Today it does
    not.
* **Restatements this creates, each needing a parity test** of the kind
  `test_runner_inputs.py` already runs for the mock:
  * `choices` for `command`, against `GENERIC_COMMANDS`;
  * the `argument` pattern, against `_ARGUMENT_SAFE`;
  * 200, against `MAX_ACTIONS`, and 32, against `max_arguments`;
  * the eight action shapes and their fields, against the `if kind ==`
    chain in `browser.py`;
  * 60, against the `wait` clamp;
  * the four limit ceilings, against `WorkerConfig`'s defaults and the
    profile's `timeout_seconds`.

  The catalogue cannot import the worker, so each of these is a copy, the
  same kind of copy as the mock's exit codes.

### If it is declined

`browser` and `generic` stay `None`. The API goes on bounding them by size,
and every failure listed under *What is true today* goes on arriving after
admission, having spent a lease and a pod start. The plugin still cannot
dispatch a working browser agent (#220). The only way around that without a
declaration is a bridge exception for `None` profiles, and question 3
recommends against it. A private-address `goto` still times out instead of
being refused, and a URL with a password is still stored with its agent.

### Preconditions for the applying PR

Three things this entry does not itself change, because they are code, not
the frozen module, and this request is docs-only. The applying PR does not
merge without them:

* **Measure against the live cluster that page script cannot use the
  metadata token endpoint, before it merges.** The CORS-preflight claim
  above ("a cross-origin `fetch()` ... triggers a CORS preflight, and the
  metadata server does not answer the preflight in a way that permits the
  request") is reasoned from the metadata server's documented behaviour, not
  measured from this cluster. Run a `browser` agent against the live GKE pod
  that attempts exactly that `fetch()` and confirm the token never reaches
  the page before this entry's SSRF posture is relied on for anything.
* **The worker re-runs `check_inputs` on the payload it reads.** Confirmed
  on 2026-09-29: nothing under `apps/agent-worker` calls `check_inputs`
  today; only swarm-api (`validation.py`) and the plugin's bridge
  (`swarm_mcp/server.py`, `workflows.py`) do, both at submission. A task
  document written before this declaration existed, or reached by a future
  bypass of the submission check, is never re-checked at the point that
  actually runs it. The applying PR adds that second call in the worker's
  own dispatch path, so a bad `input` is refused twice, not once.
* **The runner's `_ARGUMENT_SAFE` uses `fullmatch`, not `match`.** Confirmed
  on 2026-09-29: `agent_worker/runners/generic.py` line 71 defines
  `_ARGUMENT_SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}$")`
  and line 181 calls `_ARGUMENT_SAFE.match(text)`. Without `re.MULTILINE`,
  `$` matches at the end of the string **or immediately before a trailing
  newline**, so `"safe\n"` passes `.match()` today though it should not: a
  value the runner appends to an argv, or writes as a working directory,
  carrying a trailing newline. `.fullmatch()` has no such exception. This is
  the runner's own code, not `apps/common/swarm_common/profiles.py`
  (`_ARGUMENT.fullmatch(value)` there already uses `fullmatch`, and was
  correct on this point already); it is listed here because it is the same
  class of bug this entry's URL work found, in the sibling check.

**Neither of the two remaining layers is a precondition of this request,
because both are already filed and neither is code this entry touches:**

* **The pod's DNS config is #341** ("Browser worker pods resolve short
  names through the cluster search path, so `kubernetes.default` reaches
  cluster services by DNS"). Set `ndots:1` and drop search domains, which
  removes the search-path trial for every short name at once, rather than
  this entry's door check trying to enumerate which short names are unsafe.
  This is the fix the owner pointed to on 2026-09-29 when declining the
  broader host-dot-count rule: `url_refusal`'s job is a 422 with a clear
  reason for the names it CAN recognise as unsafe from the string alone
  (single-label, `.svc`); closing the general case is #341's job, in the
  pod spec, not this module's.
* **A runner-side `context.route` guard is not filed as part of this
  request either.** It is the only layer anywhere in this path that sees a
  redirect or a subresource; the door check above never does, however its
  own bugs are fixed. File it as its own worker issue, so this entry is not
  blocked on worker-side Playwright work.

Together with the worker's NetworkPolicy, these three -- #341, the
`context.route` guard, and the network policy already deployed -- are what
actually keep the pod from reaching a cluster-internal service by name. This
entry's `url_refusal` is a fourth, outermost layer: a fast, clear refusal at
submission for the shapes it can recognise, not the mechanism any of this
relies on for correctness.

### Invariants

* **Invariant 10.** Every declared key is data. `command` is a name from a
  closed list whose argv the platform owns, and the ONE EXEMPTION `_NEVER`
  (`tests/unit/mcp/test_runner_inputs.py`) is asked to carry for it is
  narrow and enforced in code (`RunnerProfile.__post_init__`): `generic`
  only, and only a string whose `choices` are exactly `_GENERIC_COMMANDS`.
  `FORBIDDEN_CALLER_FIELDS` (`swarm_api/validation.py`) is unaffected -- it
  guards the top-level task body, a different namespace declared `input`
  keys never touch. `paths`, `target` and `working_directory` are appended
  after a fixed argv and can never become a flag. The four limits can only
  lower the platform's own. Nothing declared is an image, a command line, a
  resource spec or a backend parameter, and `argv`, `script`, `env`,
  `command_line` and `shell` become refusals at the door.
* **Invariant 9.** `url_refusal` refuses the cluster's own names it can
  parse as such, and a global address it can identify in the ranges this
  module lists. **It does not refuse every private address by construction**
  -- a security review on 2026-09-29 found the first draft's version of this
  claim false, because a host it failed to parse the way a browser does
  could avoid every check that followed. The revised rule refuses non-ASCII,
  backslash and any host that is not plain `[a-z0-9.-]+` before any of the
  address logic runs, which is what makes the address logic trustworthy
  again; it is still not the SSRF control (see above), and per-tenant
  isolation stays the network's job regardless.
* **Invariant 1.** A refusal at submission creates no agent. So an agent that
  is bound to fail no longer takes a lease, which is less infrastructure
  demand, never more.
* **Invariant 7.** Unchanged. Nothing here sizes a pod.

### Owner's decisions

1. **Accept the three new kinds** (`list` with `items`, `object` with
   `variants`, and `argument`), the `url` kind, and the `choices` and
   `required` fields. Or name a smaller shape.
2. **The either/or on `browser`.** An agent with neither `url` nor `actions`
   passes the declaration and fails at the runner. Options:
   * leave it with the runner (this request);
   * add a profile-level `requires_one_of` to the catalogue;
   * make `actions` required with a minimum of 1 and drop `url`, which
     breaks every current caller of `url`.
3. **The `url` rule, revised 2026-09-29, DECIDED the same day.** Accept the
   scheme restriction and the public-host rule as rewritten above (refuse
   before parsing, explicit refused-network lists in place of `is_global`,
   the single-label rule kept, `.svc` added to the refused suffixes, six
   IPv4-in-IPv6 embedding forms unwrapped), with no allow-list. **The owner
   answered all three sub-decisions on 2026-09-29:**
   * **the host-dot-count question, resolved: DROP the "fewer than two
     dots" rule.** It refused apex domains (`github.com`, `example.com`),
     the browser profile's main targets, while still admitting
     `kubernetes.default.svc` and `swarm-api.swarm-system.svc` (GKE's
     `ndots:5` search path applies to any name under five dots, so a
     two-label rule was never going to close that on its own terms either).
     The single-label rule is restored, and `.svc` is added to the refused
     suffixes; the general search-path gap is #341's job, not this
     module's (see *Preconditions for the applying PR*);
   * **https-only: no.** Recorded above ("Why not https-only, decided");
   * **IDN: accepted as written**, i.e. punycode only, no separate
     normalisation path. Recorded above ("IDN, decided").
   A runner-side `context.route` guard is filed as its own worker issue, not
   part of this request (recommended, see *Preconditions*).
4. **The bounds.**
   * `timeout_ms` 1..300000;
   * `launch_timeout_ms` 1..180000;
   * viewport up to 3840×2160;
   * `wait.seconds` 0..60;
   * `user_agent`: printable ASCII (0x20-0x7E), 0..512 characters (new in
     this revision, closing a CRLF-header-injection gap the first draft left
     open, since `user_agent` had no bound at all);
   * `selector`, `text` and `key`: 0..4096 characters each (new in this
     revision, for the same reason);
   * the `generic` limits at the worker's default ceilings: refused above
     them at the door, still clamped to the attempt's actual ceiling by the
     runner; `timeout_seconds`'s ceiling is read off `RunnerProfile.
     timeout_seconds` itself rather than restated as a literal (new in this
     revision).
5. **Exact matching of an action's `type`**, which refuses the `"Goto"` the
   runner accepts today.
6. **Keep the key name `command`, with the narrow `_NEVER` exemption this
   revision adds** (`generic` only, only a string whose `choices` are
   exactly `_GENERIC_COMMANDS`, enforced in `RunnerProfile.__post_init__`,
   not only asserted in prose) -- or decline the exemption and rename the
   key, which breaks every existing caller to avoid a guard this revision
   already narrows to the one case invariant 10 does not mean to forbid.
7. **Retire the `None` amendment.** This returns `RunnerProfile.inputs` to
   `Mapping` with no `None`, the type request 25 was accepted with. The
   alternative is to keep `| None` for a future profile, which leaves the
   branch in `check_inputs`.
8. **Question 3 (#218).** Keep the bridge's send-only-what-is-declared
   policy (recommended). #218's own text, read directly on 2026-09-29,
   confirms this is its third question; no further check is needed.

---

## 33. `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it

**Superseded in part 2026-10-06 (lane MS0, part of #352):** the App-shaped half -- `provider="git-merge"`, the merge App, its own Job account -- was already replaced by request 47; the [2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs) keeps everything else this request applied (`WorkerAction`, `RunnerProfile.worker_action`, the `merge` entry, `MERGE_REFUSED`/`MERGE_FAILED`) as the merge step's own vocabulary, and adds new refusal codes inside `result_summary.merge.refusal`, not new end causes.

**Status: ACCEPTED 2026-09-29 by the owner, as the design; build gated on
#342. APPLIED 2026-10-01, the build accepted by the owner on 2026-10-01**
(functionality wave 3, lane M1). The owner decided that #295 is built now
and stays disabled for every tenant until #342 is enforced and the owner
creates the review and merge Apps. Applied as written below: `WorkerAction`,
`RunnerProfile.worker_action` with both `__post_init__` refusals, the `merge`
entry and `MERGE_REFUSED`/`MERGE_FAILED`. The entry also carries
`available=False` and a `disabled_reason` naming #295 and #342, so nothing
can dispatch it. Terraform creates no Job for it (`profiles_without_a_job`,
`terraform/infra/locals.tf`) until its own service account lands. Recorded
from #295's design step. Revised 2026-09-29, several times,
against a security review's rounds (B1, B2, M1–M5 and their minors, then B1
corrected repeatedly against a joint review with CR 34, ending with R8
recorded open rather than closed); see [merge-step.md](merge-step.md)'s own
revision note for the changes, which are almost entirely outside this
request's own frozen-contract surface — `worker_action`, the `merge` profile
and the two end causes are unchanged by the review. The design is
[merge-step.md](merge-step.md). Nothing under `apps/common/swarm_common/`
has been edited. **The owner accepted this request's `profiles.py`/
`models.py` shape as the design to build against — acceptance of the design
is not yet the build:** `merge` (and the `post-verdict`/`claude-code-review`
requests below) must not be enabled for any tenant until #342 (signed step
specs) ships, per merge-step.md §0 consequence 4 and §10. Renumbered from 29
to 33 on 2026-09-29: #259 is 29, #314 is 30, #304 is 31, #315 is 32. Since
then, `main`'s own numbering moved independently — its own request 29
(`EndCause` gains `PUBLISH_REFUSED`) is unrelated content, and request 31 is
not present on `main` at all — but 33 was never taken by anything else, so
no further renumbering was needed on merge.

**Pointer, not a request of its own (round-3 re-review, 2026-09-29): the
design's `post-verdict` and `claude-code-review` catalogue entries are
separate, not-yet-filed frozen-contract requests, tracked in
[merge-step.md](merge-step.md) §10 (build items 2–3) and §6a (the pinned
`VERDICT_REFUSED`/`VERDICT_FAILED` end causes), not folded into this one.**
This request stays scoped to what its heading says — the `merge` profile —
because `post-verdict` and `claude-code-review` each need their own
`### What it would break if accepted` analysis once filed, the same way this
one has its own. This design also now depends on S0 issue #342, signed step
specs, which every step's worker (not only `merge`'s or `post-verdict`'s)
must verify before running. **Corrected, joint review with CR 34 (#344),
2026-09-29: an earlier draft of this paragraph said #342 "is not a change to
a frozen type" and so did not belong in this file. That was wrong.** Signing
and verifying a step's canonical spec needs somewhere on the frozen `Task`
or `WorkflowStep` shape to carry the signature (or an equivalent frozen
type), which is exactly the kind of change this file exists to record; #342
is tracked as its own S0 issue for the security decision and the build plan,
but its frozen-contract surface — whatever field or type CR 34 ends up
needing — belongs here too, as its own numbered request once CR 34 states
precisely what it is. This entry does not attempt to state it first; see
[merge-step.md](merge-step.md) §0 consequence 4 and §7 T14/R7 for why this
design cannot be enabled without it regardless.

### What is true today

A workflow cannot merge its own pull request. Every `RunnerProfile` names a
runner that the lifecycle starts as a supervised child
(`runner_argv`, contract request 18), and every runner runs an agent or a
command. The only credential-bearing thing a worker does after its runner exits
is `_publish_git`, which runs under the tenant's service account in the same
container the agent just ran in.

An agent can mint that service account's token from the metadata server
([security.md](security.md#cloud-metadata-abuse)). So a merge credential
readable by the worker that publishes is readable by every agent of the tenant
([merge-step.md](merge-step.md) §0). The contract's own platform decision says
where a step's identity can differ: Cloud Run sets the service account on the
Job, and there is one Job per tenant per **profile**. A merge that no agent can
reach therefore needs a profile of its own.

`EndCause` (contract request 23) has no value that says a merge was refused or
failed. Recording one as `RUNNER_ERROR` would be false, since no runner ran.
Recording it as None would send the outcome ledger back to classifying text.

### The requested change

In `profiles.py`:

```python
class WorkerAction(str, Enum):
    """A platform action the WORKER performs instead of starting a runner."""
    MERGE = "merge"


@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: When set, the lifecycle performs this action itself and starts no
    #: runner child, so no agent ever runs under this profile's Job identity.
    #: `runner_argv` must then be empty, and it must be non-empty otherwise.
    worker_action: WorkerAction | None = None
```

`__post_init__` refuses `worker_action` with a non-empty `runner_argv`, and
refuses `runner_argv=()` without a `worker_action`.

A catalogue entry:

```python
"merge": RunnerProfile(
    name="merge",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=(),
    worker_action=WorkerAction.MERGE,
    # The credential is never mounted: its secret is read by the worker at
    # merge time, as `swarm-<tenant>-merge`, the Job's own service account.
    # `provider` is what parks the step CREDENTIAL_MISSING, at no cost, for a
    # tenant that has not registered one, and keeps Terraform from creating a
    # merge Job for that tenant.
    provider="git-merge",
    secrets=(),
    timeout_seconds=600,
    inputs={},
),
```

In `models.py`, two `EndCause` values, written only by the worker:

```python
MERGE_REFUSED = "merge_refused"   # a condition for merging was not met; nothing changed on the forge
MERGE_FAILED = "merge_failed"     # the merge was allowed, and the forge did not do it
```

The specific reason (`verdict_not_merge`, `checks_pending`, `head_moved`, ...)
goes in `result_summary.merge.refusal`, a worker vocabulary and not a frozen
one, just as `publish_reason` is today. [merge-step.md](merge-step.md) §6 lists
every code and its cause.

### What it would break if accepted

* **Nothing stored.** `worker_action` defaults to None, so every existing
  profile is unchanged. Old task documents never carry the new causes.
* **Every consumer of `runner_argv` must learn that it can be empty.** Today
  that is `lifecycle._runner_argv` and the dispatchers, which set no command
  (request 18). The lifecycle must branch on `worker_action` before it builds
  an argv.
* **Terraform's `job_matrix`** creates a `merge` Job for each tenant whose
  providers include `git-merge`. That Job has to run as the tenant's merge
  service account, not `worker_service_accounts[tenant]`, and that is a
  Track C change. Until it lands, the Job must not exist: a merge Job running
  as the tenant's worker account is the hole this request exists to close.
* **Every restatement of the catalogue** has to follow: the plugin's bridge,
  `swarm_profiles`, the UI's profile list, and the parity checks that hold the
  shell and jq copies to the Python (`check-contract-parity.sh`). A profile
  with no agent should appear as such, not as a runner.
* **The outcome ledger** (`swarm_api.outcomes`) gains two classes, and its
  `DERIVE_VERSION` is bumped so stored days are re-derived.

### If it is declined

A merge can be done only by something that is not a workflow step:
`auto-merge.yml` with a human's `ready`, or an operator. The owner's chain
then stops at proof. The two alternatives [merge-step.md](merge-step.md) §1
compares both put the merge credential where an agent, or another tenant, can
reach it. They are listed there as rejected, not as fallbacks.

### Invariants

- **Invariants 1–3.** The merge step is an ordinary agent: admitted in the same
  transaction, parked and costing nothing until its parents succeed, and
  counted from `LEASED`.
- **Invariant 4.** A pending check is a refusal, not a wait. A long forge
  rate-limit fails the attempt retryably with `next_eligible_at`, and the
  worker does not sleep through it.
- **Invariant 5.** The worker checks its generation at start and again
  immediately before the merge call. A stale merge worker never touches the
  forge.
- **Invariant 6, 7.** On-demand `standard`; `requests == limits`.
- **Invariant 8.** Checkpointing stays on. The workspace is empty, and the
  merge is idempotent on its pinned sha, so a lost attempt costs one re-read.
- **Invariant 9.** The merge credential's only accessor is the tenant's own
  merge service account. No agent runs as that account, and no other tenant's
  Job can.
- **Invariant 10.** A caller names `merge` and sends `input: {}`. The
  repository, the pull request and the sha all come from the workflow's own
  steps, never from the caller.
- **#219.** The credential is read only by the merge worker, only at merge
  time. It is never in the workspace, a file, an environment variable, argv or
  a log, and it is revoked in a `finally`.

### Amendment (proposed 2026-09-30, #364)

**Status: ACCEPTED — accepted by the owner 2026-10-01. Item 1 APPLIED 2026-10-01**
(functionality wave 3, lane M1, in the same change as request 33):
`validation.known_providers()` leaves out every `worker_action` profile's
provider (`APP_CREDENTIAL_PROVIDERS`: `git-merge`, `git-review`), and both
credential routes call it, so neither route file was edited and neither
accepts the two. **Items 2 and 3 APPLIED** (recorded 2026-10-08, #364) in
`terraform/modules/secret_manager`: `accessor_overrides` is the complete
reader list for a provider in `action_providers`, validated to exist for each
such provider and to never name the worker account; and `refreshable`
excludes `action_providers` from the `-refresh` twins and from the
refresher's grants. Both are tested by
`tests/terraform/merge_step_iam.tftest.hcl`. `terraform/infra/variables.tf`
additionally refuses either provider for any tenant, since the keys were
retired (MS0-Q4, 2026-10-06), and the credential store
(`swarm_api.credentials.put_credential`) refuses both, and the forge token,
itself, before any write (#364). Found by the
security review of #351 (CR 35/36), filed as issue #364. This amendment does
not touch this request's frozen-contract surface (`profiles.py`/`models.py`
are unchanged by it); it closes a gap in how the design it names is *built*,
so it is recorded here rather than as a separate numbered request. It leaves
the Status line above as is — that line is about the `profiles.py`/`models.py`
shape, which is still accepted as designed.

`git-merge` must never end up bound to the tenant's ordinary worker service
account, and the same is true of `git-review` (contract request 35, revised
on #351). Nothing enforces that yet outside `register-tenant.sh`
(merge-step.md §10 item 5):

* The API credential routes accept whatever the catalogue lists as a
  provider — `POST /me/credentials` (`routes/tenants.py`) and the admin
  route (`routes/admin.py`) — so once `git-merge`/`git-review` are catalogue
  providers, a tenant (or an operator, on the admin route) can register one
  against the worker account through the ordinary credential path, with
  nothing in that path aware that these two providers are different.
* `terraform/modules/secret_manager` has no per-provider accessor override.
  Its `iam_binding` is authoritative and today reads `members=[cfg.accessor]`
  (`main.tf:17-28,109-116`), always the tenant's worker account
  (`terraform/infra/main.tf` (`accessor      = cfg.accessor`)). Left as is, the next `terraform apply`
  after `git-merge`/`git-review` join the catalogue both binds the worker
  account to the secret and **removes** a separately-granted merge or review
  service account, since `iam_binding` (not `iam_member`) replaces the whole
  membership list.

The build of contract request 33 (and of `post-verdict`/`claude-code-review`,
merge-step.md §10 items 2–3) is gated on all of the following, in addition to
what merge-step.md §10 already lists:

1. The API credential routes (`routes/tenants.py`'s `POST /me/credentials`,
   and `routes/admin.py`'s admin route) refuse to register or bind
   `git-merge` or `git-review` to a tenant's worker account, the same way
   `register-tenant.sh` refuses (merge-step.md §10 item 5).
2. `terraform/modules/secret_manager` gains a per-provider accessor
   override, so that for `git-merge` and `git-review` the merge or review
   service account is the **sole** `secretAccessor` member — not an addition
   to `cfg.accessor`, a replacement of it for those two providers — closing
   the removal hazard in `main.tf:17-28,109-116` above.
3. Both providers are excluded from `refresh_secrets` and from the
   refresher's own grants (its `versionAdder` role and its `-refresh` twin
   secret), so the subscription-refresh path never touches either
   credential.

See merge-step.md §10 item 5's own note on this (added 2026-09-30, #364) and
issue #364 for the full finding.

---

## 34. `models.py` / a new `specsign.py`: a step's spec is signed by swarm-api and verified by every worker

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
Applied by #353 (accepted by the owner 2026-09-29): `swarm_common/specsign.py`
and the three signature fields and `EndCause.SPEC_SIGNATURE_INVALID` in
`swarm_common/models.py`; the Terraform half by #354.
Recorded from #342 (S0), which the round-3 security re-review of #316
(contract request 33, the merge step) found the same day. This entry went
through three rounds of joint review with contract request 33 on 2026-09-29 --
the initial mechanism decision, the checkpoint-restore/shared-signer/GKE
review, and the checkpoint-residual/end-cause/post-verdict-mechanism
correction, each recorded in this entry as it was made -- before the owner
accepted it as it now reads. The owner decided the mechanism on 2026-09-29, in
a joint review with contract request 33: swarm-api signs each step's canonical
spec at
submission with a Cloud KMS asymmetric key that only `swarm-api`'s service
account holds `signer` on; every worker verifies the signature against the
spec it fetched before it starts an agent or a worker action, and refuses a
mismatch with a typed end cause; worker-action steps (`post-verdict` and
`merge`, contract request 33) also verify the upstream specs they rely on.
The key stays in the shared `saga-agents-staging` project (the owner's
decision, 2026-09-29, **not** a dedicated project), and workers trust every
`ENABLED` version of it. Both are accepted residuals, stated honestly in **Key
management** and **The threat model** rather than designed away: a project
owner or editor of the shared project can already reach `swarm-api`'s signing
identity, and a KMS admin's new version is trusted the moment it ships. This
entry is what that decision needs from the frozen contract, and the
design around it that the frozen fields only make sense inside. Nothing under
`apps/common/swarm_common/` has been edited. Numbered 34 because 29 to 33 are
taken on open branches (#259, #314, #304, #315, #316); if another branch has
taken 34 by the time this merges, renumber this one.

### What is true today

A step's instructions are whatever its task document says when its worker
reads it, and the document is writable by every agent of the tenant.

* **The worker trusts the document unsigned.** `Lifecycle._prepare`
  (`apps/agent-worker/agent_worker/lifecycle.py`, step 4) calls
  `self.control.fetch_task()` and builds the runner's `input.json` from
  `task.get("input")`. The same dict is kept as `self._task` and is where the
  worker later reads `repository_url` and `repository_ref` (`_maybe_clone`),
  `metadata.input_from` (`inputs.py`), `metadata.expected_outputs`
  (`expected_outputs.py`), `metadata.dispatch` (the publish gate, and #273's
  `continuation.py`), `resource_class` and `max_attempts`.
  `ControlPlane.fetch_task` (`control.py`) checks one thing about the
  document: that its `tenant_id` is this worker's.
* **The scheduler trusts it too.** `worker_env()`
  (`apps/scheduler/scheduler/dispatch.py`) sets `RUNNER_PROFILE` and
  `TASK_TIMEOUT_SECONDS` on the execution from the document, and picks the Job
  (one per tenant per profile) by the document's `runner_profile`. The
  per-execution override wins over the Job's own `RUNNER_PROFILE`
  (`terraform/infra/locals.tf`, "The dispatcher's per-execution overrides
  MERGE with this").
* **Any agent of the tenant can write the document.**
  [multi-tenancy.md](multi-tenancy.md#the-firestore-row-in-full) records it:
  the tenant worker's service account holds its Firestore role on the whole
  database, because Firestore has no document-level IAM, and an agent can mint
  that account's token from the metadata server
  ([security.md](security.md#cloud-metadata-abuse)). So "it CAN read and write
  a document whose id it can guess". Within a workflow nothing has to be
  guessed: every step's `task_id` is on the workflow document
  (`WorkflowStep.task_id`), and every step knows its workflow.
* **So one step can rewrite another's instructions.** In an implement →
  review → fix chain the implement agent can set the parked review agent's
  `input.prompt` to "write verdict.json with MERGE", point its
  `metadata.input_from` at a different file, or rewrite the fix step's prompt.
  The real review step then runs the rewritten instructions, with no prompt
  injection involved. #342 has the reproduction.

`swarm-api`'s `Store` is the only thing in `apps/` that creates a task document
(`create_tasks`, `create_workflow`; read on main at `bc90da1`, 2026-09-29).

### The requested change

Three frozen additions: a module that defines the canonical form, three
optional fields on `Task`, and one `EndCause`. Everything else in this entry
(the KMS key, the signing call, the verification, the rollout) is outside
`swarm_common` and is written here so the frozen part can be judged against
the design it serves.

#### 1. The canonical form: `swarm_common/specsign.py` (new, frozen)

The spec is a projection of the task document: the fields that decide what a
step runs, as swarm-api wrote them, and nothing a later writer changes. Both
sides compute it from the **Firestore dict** (`Task.to_firestore()` on
swarm-api, the fetched snapshot in the worker), so they project the same
shape.

**Covered, format 1.** Every value is taken from the document as stored, with
a missing key read as null:

| canonical key | source | why it is covered |
|---|---|---|
| `purpose` | the constant `"swarm.step-spec"` | domain separation: the bytes cannot be read as any other message this key might one day sign |
| `format` | the constant `1` | a later format is a different message, never a reinterpretation of this one |
| `task_id` | **the document id the worker was pointed at**, never the document's own `id` field | binds the signature to one task, so a signature copied onto another task fails |
| `tenant_id`, `workflow_id`, `step_id` | the fields | binds it to one tenant and one workflow; a spec cannot be replayed across either |
| `submitted_by` | the field | who submitted is part of what was submitted (and of contract request 30's audit) |
| `runner_profile` | the field | the profile picks the Job, and so the service account; a rewrite to `merge` or `post-verdict` would move a step onto an identity no agent may use |
| `resource_class`, `timeout_seconds`, `max_attempts` | the fields | read by the scheduler and the worker (`TASK_TIMEOUT_SECONDS`, `_sized_resource_class`, `fail_retryably`) |
| `provider`, `model` | the fields | set at submission, from the profile and the request, and changed by no later writer |
| `input` | the **whole object** | the prompt and every declared input (`issue`, the mock's), whatever keys a profile declares later |
| `depends_on` | the list, **in stored order** | a removed parent is how a step would be made to run early |
| `repository_url`, `repository_ref` | the fields | what is cloned, and where a pull request is opened |
| `metadata` | exactly the three keys `dispatch`, `input_from`, `expected_outputs`, each the stored value or null | the platform's own blocks. `dispatch` carries `strategy`, `carrier`, `role`, `integrates`, #273's `continues`, and contract request 33's `pr_role`, `merges` and `verdict_source` (`post-verdict`'s own dispatch-block pointer at the review task it reads, `{review: <review task id>}`, added in that entry's round-5 re-review so `post-verdict` never has to trust `input_from`'s tenant-writable staging for the one read this whole design depends on); `input_from` is the task-id-keyed map `submit_workflow` writes; `expected_outputs` is what `record_expected_outputs` writes on an upstream step |

**Not covered, on purpose.** `state`, `park_reason`, `blocked_by`,
`current_lease_id`, `current_generation`, `attempt_count`,
`next_eligible_at`, `cancel_requested`, `started_at`, `completed_at`,
`last_error`, `result_summary`, `latest_checkpoint`, `end_cause` and
`updated_at`: every one is written after submission by the scheduler, the
reconciler, the worker or a cancel, so a signature over any of them would
break on the first legitimate write. `created_at` binds nothing `task_id`
does not, and a timestamp is the one value whose round trip through Firestore
(microseconds, a zone) would have to be specified for no gain. `priority` is admission's
ordering within the tenant and no worker reads it. `metadata.startup_refunds`
is the reconciler's. Every other `metadata` key is the caller's own free-form
label, which no worker acts on. **The rule that keeps this true:** a metadata
key the worker starts reading is added to `SIGNED_METADATA_KEYS` in the same
change, with a format bump; a test holds the two together (**Tests**, below).

**Serialisation: RFC 8785 (JSON Canonicalization Scheme), implemented inside
`specsign.py` with no dependency.** `swarm_common` declares
`dependencies = []`, and the `sc` plugin installs it from a git tag
(`apps/common/pyproject.toml`), so it cannot take the `rfc8785` package. The
value domain is small enough to implement exactly: null, booleans, strings,
integers, finite doubles, arrays and string-keyed maps, which is everything
Firestore returns for a document swarm-api wrote from JSON. The implementation
is held to RFC 8785's own test vectors and to the `rfc8785` package as a
test-only oracle (**Tests**). Three values have no canonical form, and swarm-api
refuses them at submission with 422 `invalid_input` **before** anything is
signed or stored: an integer outside ±(2^53 − 1) (JCS numbers are IEEE doubles;
today `validate_storable` admits up to 2^63 − 1), a non-finite float, and a
string holding a lone surrogate. This is a narrowing of what the API accepts,
and says so.

**Hash: SHA-256** over the UTF-8 bytes. The key signs that digest
(`EC_SIGN_P256_SHA256`, below).

The proposed module, whole. It was run on 2026-09-29, from a scratch copy
outside the repository, against RFC 8785's number vectors (Appendix B) and its
§3.2.2 and §3.2.3 examples, and against the `rfc8785` package (0.1.4) on
20,000 generated values, with no difference. The first draft sorted members by
their *escaped* names and put `"\r"` after `"1"`; §3.2.3's example is the
vector that caught it, which is why it is named in **Tests**.

```python
"""The canonical form of a step's spec, signed by swarm-api and verified by the worker.

Contract request 34 (#342). A tenant's agents can write any task document of
their tenant (docs/multi-tenancy.md, "The Firestore row, in full"), so a
worker must not run a spec swarm-api did not sign. This module is the ONE
definition of which fields that signature covers and how they become bytes:
swarm-api signs `spec_digest(canonical_step_spec(task.to_firestore(),
task_id=task.id))`, and the worker verifies the same digest over the document
it fetched. Two copies of this projection would be two opinions about what
is signed. No cryptography lives here: signing is swarm-api's (Cloud KMS) and
verification is the worker's.

The encoding is RFC 8785 (JCS), implemented for exactly the values a
Firestore document written from JSON can hold, with no dependency: this
package is installed from a git tag by the plugin and declares none.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any, Mapping

#: The canonical form's version. A change to the covered fields or the encoding
#: is a NEW format, never an edit to this one: a signature is over a format.
SPEC_FORMAT = 1

#: Domain separation. The bytes of a step spec cannot be read as another message.
SPEC_PURPOSE = "swarm.step-spec"

#: The keys of `Task.metadata` that swarm-api writes and the worker acts on.
#: Every other metadata key is the caller's label or another writer's record.
SIGNED_METADATA_KEYS = ("dispatch", "input_from", "expected_outputs")

#: JCS numbers are IEEE doubles, so an integer is exact only within this bound.
MAX_SAFE_INTEGER = 2**53 - 1


class SpecNotCanonical(ValueError):
    """A value in the spec has no canonical form (see `jcs`)."""


def canonical_step_spec(doc: Mapping[str, Any], *, task_id: str) -> bytes:
    """The bytes the signature covers, from a task document as stored.

    `task_id` is the id the document was READ BY (the worker's TASK_ID, the
    id swarm-api minted), never the document's own `id` field.
    """
    metadata = doc.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SpecNotCanonical("metadata is not a map")
    spec = {
        "purpose": SPEC_PURPOSE,
        "format": SPEC_FORMAT,
        "task_id": task_id,
        "tenant_id": doc.get("tenant_id"),
        "workflow_id": doc.get("workflow_id"),
        "step_id": doc.get("step_id"),
        "submitted_by": doc.get("submitted_by"),
        "runner_profile": doc.get("runner_profile"),
        "resource_class": doc.get("resource_class"),
        "timeout_seconds": doc.get("timeout_seconds"),
        "max_attempts": doc.get("max_attempts"),
        "provider": doc.get("provider"),
        "model": doc.get("model"),
        "input": doc.get("input"),
        "depends_on": doc.get("depends_on"),
        "repository_url": doc.get("repository_url"),
        "repository_ref": doc.get("repository_ref"),
        "metadata": {key: metadata.get(key) for key in SIGNED_METADATA_KEYS},
    }
    return jcs(spec)


def spec_digest(canonical: bytes) -> bytes:
    """SHA-256 of the canonical bytes: what the KMS key signs."""
    return hashlib.sha256(canonical).digest()


def jcs(value: Any) -> bytes:
    """RFC 8785 serialisation of `value`, as UTF-8 bytes."""
    return _encode(value, "$").encode("utf-8")


def _encode(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise SpecNotCanonical(f"{path} is an integer outside +/-(2**53 - 1)")
        return str(value)
    if isinstance(value, float):
        return _number(value, path)
    if isinstance(value, str):
        return _string(value, path)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    if isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise SpecNotCanonical(f"{path} has a key that is not a string")
            _string(key, path)  # refuses a lone surrogate before it is sorted
            items.append(key)
        # RFC 8785 3.2.3: members sorted by the UTF-16 code units of their RAW
        # names -- not their escaped form, which would put "\r" after "1".
        items.sort(key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(
            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}") for k in items
        ) + "}"
    raise SpecNotCanonical(f"{path} is a {type(value).__name__}, which JSON has no form for")


def _string(text: str, path: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecNotCanonical(f"{path} holds a lone surrogate") from exc
    # json.dumps with ensure_ascii=False escapes exactly what RFC 8785 3.2.2.2
    # does: '"', '\\', the five short forms, and \u00xx (lowercase) for the
    # rest of U+0000..U+001F. Everything else is written as itself.
    return json.dumps(text, ensure_ascii=False)


def _number(x: float, path: str) -> str:
    # RFC 8785 3.2.2.3: ECMAScript's Number.prototype.toString.
    if not math.isfinite(x):
        raise SpecNotCanonical(f"{path} is not a finite number")
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _number(-x, path)
    # repr() is the shortest round-tripping decimal, as ECMAScript requires.
    _, digits_t, exponent = Decimal(repr(x)).as_tuple()
    digits = "".join(map(str, digits_t))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
```

#### 2. `models.py`: three fields on `Task` and one `EndCause` (exact diff against `bc90da1`, not applied)

```diff
--- a/apps/common/swarm_common/models.py
+++ b/apps/common/swarm_common/models.py
@@ -182,7 +182,8 @@
     Each value is written by exactly one kind of writer, beside `completed_at`:
 
       * the worker (`agent_worker.control`): TIMEOUT, OUTPUTS_MISSING,
-        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED;
+        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED,
+        SPEC_SIGNATURE_INVALID;
       * the reconciler (`repair_task_state`): LOST_WORKER, CANNOT_START,
         CANCEL_REQUESTED;
       * the scheduler: DISPATCH_FAILED, FAILED_PARENT, CANCELLED_PARENT,
@@ -209,6 +210,17 @@
     its own class, not a runner error -- 10 of dev's 13 "runner errors" on
     2026-09-25 were exactly that. CANCELLED_PARENT is the request's own, and
     what decision item 2 (split "after a cancel" from "after a failure") needs.
+
+    SPEC_SIGNATURE_INVALID is contract request 34 (#342): the worker refused
+    to run a spec swarm-api did not sign -- a signature that does not verify
+    over the fetched document, a missing one outside the rollout window, or a
+    key version that is not the platform's. A worker-action step's OWN spec
+    failing this check is this cause too; an UPSTREAM step's spec failing it
+    is contract request 33's own end cause instead (that entry names it; this
+    one does not), with reason "upstream:<task id>:<why>" -- the same reason
+    shape this cause uses for its own failures. Never retried: another
+    attempt reads the same document. Every occurrence is either a tenant's
+    agent rewriting a step or a platform bug, and both causes are alerted on.
     """
 
     TIMEOUT = "timeout"
@@ -222,6 +234,7 @@
     FAILED_PARENT = "failed_parent"
     CANCELLED_PARENT = "cancelled_parent"
     WORKFLOW_SWEEP = "workflow_sweep"
+    SPEC_SIGNATURE_INVALID = "spec_signature_invalid"
 
 
 @dataclass
@@ -263,6 +276,21 @@
     #: every task that has not ended, on a success, and on anything written
     #: before 2026-09-25 -- an old document decodes exactly as it did.
     end_cause: EndCause | None = None
+    #: swarm-api's signature over this task's canonical step spec (contract
+    #: request 34, #342): standard base64 of the DER ECDSA signature that Cloud
+    #: KMS returns for `specsign.spec_digest(specsign.canonical_step_spec(...))`.
+    #: Written once, in the write that creates the document, and never after.
+    #: None on every task written before swarm-api signed; the worker refuses
+    #: such a task outside the rollout window.
+    spec_signature: str | None = None
+    #: The FULL resource name of the key version that made `spec_signature`:
+    #: projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<n>.
+    #: Written by the same write. A tenant can rewrite it, so the worker trusts
+    #: it only as a lookup key among the versions the platform published.
+    spec_key_version: str | None = None
+    #: `specsign.SPEC_FORMAT` at signing. Read to choose the projection; it is
+    #: also inside the signed bytes, so a rewrite to another format fails.
+    spec_format: int | None = None
 
     def retries_exhausted(self) -> bool:
         """This task has used its last attempt. See `retries_exhausted`."""
```

**Re-verified against current `main`, 2026-09-29.** The `SPEC_SIGNATURE_INVALID`
docstring above was reworded after the joint review, to separate own-spec from
upstream-spec failures (**Verification in the worker**, below), which changed
this hunk's line counts twice over. The original PR's "Checked" note that this
diff patched cleanly onto `bc90da1` predated both rewordings and this
hunk-count fix. Re-run here: `git apply --check` against `apps/common/swarm_common/models.py`
at `origin/main` (`e7c18ac`, 2026-09-29) exits 0, and the patched file parses
(`ast.parse`). The first re-run, before this fix, failed with `error: corrupt
patch` — the second hunk's header claimed 16 new lines where the body had 17
(a one-line miscount left over from the rewording), which is corrected above.

`to_firestore` needs no change: `asdict` writes the three fields as they are.
`WorkflowStep` and `Workflow` are unchanged (see **What signing does not
close**, the workflow document).

#### 3. Key management (Terraform; Track C's files, named here, not written)

* **`cloudkms.googleapis.com`** joins the service list in
  `terraform/infra/main.tf`. `project_services` already refuses
  `disable_on_destroy = true`, so enabling it can never disable it for the
  other team.
* **One key ring and one key per environment, and this MUST hold.** `google_kms_key_ring`
  `swarm-<env>-specs` in `var.region` (`us-central1`), and in it
  `google_kms_crypto_key` `step-spec` with `purpose = "ASYMMETRIC_SIGN"`,
  `version_template { algorithm = "EC_SIGN_P256_SHA256", protection_level =
  "SOFTWARE" }` and `labels = { managed-by = "swarm-terraform", ... }`.
  P-256 because verification is fast and the signature is 70-odd bytes of DER
  in every task document. **The canonical form carries no `environment`
  field** (section 1's table): nothing in the signed bytes says "dev" or
  "prod". A key shared across environments would let a spec signed by dev's
  swarm-api verify against prod's worker, so `SPEC_SIGNING_KEY` and every
  version in `SPEC_VERIFY_KEYS` must name that environment's key ring and no
  other's; this is an operational rule Terraform's naming enforces (one ring
  per `<env>`), not something the canonicaliser can check.
* **`managed-by=swarm-terraform`, and the one resource that cannot carry it.**
  The crypto key carries the label. **A key ring has no labels at all** in
  Cloud KMS or the google provider, so `google_kms_key_ring` has to be added
  to `scripts/lib/unlabelable-types.json`, checked against
  `terraform providers schema -json` for the pinned provider the way every
  entry there was. The IAM members below are `google_kms_crypto_key_iam_member`,
  which is unlabelable for the same reason as every other `*_iam_member` and
  needs its own entry. **Neither a key ring nor a key can be deleted** in
  Cloud KMS: `terraform destroy` removes them from state and schedules the
  key's versions for destruction, and the names stay taken. So `make destroy`
  followed by a fresh apply collides with the old names; the recreate path
  needs an `import` block, and this is written next to the resources.
* **Who holds what.** Nobody holds `roles/cloudkms.signerVerifier`.

  | identity | role, on the key only | why |
  |---|---|---|
  | `swarm-api` | `roles/cloudkms.signer` (`useToSign` only) | it signs; it never verifies or reads the public key, so `signerVerifier` would be reach it does not use |
  | the CI deployer (`swarm-tf-deployer`) | `roles/cloudkms.publicKeyViewer` and `roles/cloudkms.viewer` | Terraform reads the enabled versions and their public keys at plan time (below); neither role can sign or change IAM |
  | every worker (`swarm-agent-worker-<tenant>`, and contract request 33's `swarm-<tenant>-merge` and `swarm-<tenant>-post-verdict`) | **none** (recommended; decision 4) | the public keys reach it in its Job's environment |

  The key ring, the key and the one `signer` binding live in
  `terraform/bootstrap`, applied by the owner directly, not in
  `terraform/infra` (decision 4). That keeps the CI deployer
  (`swarm-tf-deployer`) off `setIamPolicy` on the key, so **the CI pipeline**
  cannot grant itself `signer`. The binding names `swarm-api` by its email,
  `swarm-api@<project>.iam.gserviceaccount.com`, since the account itself is
  created by `terraform/infra` (in `modules/iam`).

  **This is not a boundary against the project itself, and the owner accepted
  that rather than designing it away (2026-09-29): the key stays in the shared
  `saga-agents-staging` project** (CLAUDE.md rule 2 -- another team's project,
  not a dedicated one). `roles/editor` on that project carries
  `iam.serviceAccounts.actAs` and `run.services.update`, so any project Owner
  or Editor -- not only this platform's operators -- can redeploy
  `swarm-api`'s Cloud Run service to run code of their choosing as its service
  account, which holds `signer`, and sign anything through it without ever
  calling `setIamPolicy` on the key. An Owner can also `setIamPolicy` the key,
  the ring or the project directly, bootstrap's separation notwithstanding.
  **This residual holds against the platform team and the other team sharing
  the project. It does not hold against tenant agents:** the table above gives
  no tenant or worker account any role on this key, ever, so a tenant's own
  agent -- the actor #342 is about -- has no path to a signature through KMS
  IAM at all; its only avenue is still the one this entry closes, rewriting an
  already-signed document. Stated again in **The threat model**.
* **Trusted versions.** A worker trusts **every `ENABLED` version** in
  `SPEC_VERIFY_KEYS` (or reachable through `GetPublicKey`, under the runtime
  alternative), with no separate allow-list of "which versions swarm-api is
  actually using." **Accepted residual, stated plainly:** anyone who can
  create or import a version on this key -- a KMS admin, or `swarm-tf-deployer`
  running `terraform apply` against `terraform/bootstrap` -- gets that version
  trusted by every worker from the next release on, whether or not swarm-api
  ever signs with it. Narrowing trust to "the version `SPEC_SIGNING_KEY_VERSION`
  currently names" was considered and rejected: it would break rotation step 2
  (the entry two bullets below), where N and N+1 must both verify before
  swarm-api cuts over.
* **How workers get the public keys, and how they are cached (recommended).**
  `terraform/infra` reads the key's versions with the
  `google_kms_crypto_key_versions` data source, keeps those in state
  `ENABLED` (the data source's attributes, the state and the PEM, are to be
  checked against `terraform providers schema -json` for the pinned
  provider, as the unlabelable types are).

  **On Cloud Run**, it renders one worker environment variable on every Job,
  `SPEC_VERIFY_KEYS`: a JSON map `{<full version name>: <PEM public key>}`.
  The Job's environment **is** the cache: fixed for the life of the execution,
  refreshed by every release, and needing no network call, no quota and no IAM
  at attempt start. Beside it, `SPEC_SIGNING_KEY` names the crypto key.
  `worker_env()` must never override either (a test holds the scheduler's
  override keys to an allow-list that excludes them).

  **On GKE the same map is not inlined into the pod's `env:`** (the owner's
  decision, 2026-09-29 review): a browser pod's manifest is template-rendered
  by `GkeJobDispatcher._manifest` from
  `kubernetes/worker-templates/worker-job-browser.yaml`, not written once by
  Terraform, and the tenant's own worker KSA runs in the same per-tenant
  namespace as that Job (`kubernetes/namespaces/tenant-namespace.yaml`) --
  putting the JSON map inline is one placeholder away from a tenant-writable
  copy the way `worker_env()`'s per-execution overrides already are, and is
  exactly the large-value-in-a-template problem that file's own
  `SWARM_ARTIFACTS_DIR` comment warns about for a far smaller value. Instead,
  `terraform/infra` renders a `kubernetes_config_map`,
  `swarm-spec-verify-keys` (the same `{version: PEM}` shape, plus
  `SPEC_SIGNING_KEY`), **in each tenant's namespace**, applied by Terraform's
  own identity. `GkeJobDispatcher._manifest` mounts it **read-only** at
  `/etc/swarm/spec-verify-keys` on the `browser` profile's Job in place of an
  `env:` entry.

  **The RBAC fact that matters is a write grant's absence, not a read grant.**
  A volume mount is fetched by the kubelet with the node's own credentials,
  never the pod's `ServiceAccount` token -- and that token is not even mounted
  on these pods (`automountServiceAccountToken: false`,
  `kubernetes/rbac/worker-rbac.yaml`'s deliberately empty `swarm-worker`
  Role, "a worker needs nothing from the Kubernetes API"). So this ConfigMap
  needs no `get`/`list`/`watch` rule added to that Role, and none is added --
  the pod has no token to make such a call with regardless. What has to be
  true, and is checked (**Tests**, below), is that no `RoleBinding` in the
  tenant namespace grants the tenant's worker KSA (or any subject bound to the
  `swarm-worker` Role) `update`, `patch` or `delete` on `configmaps`; only
  Terraform's deployer identity, via its own `kubectl`/provider credentials,
  ever writes it. `worker_env()` must never set a `SPEC_VERIFY_KEYS` /
  `SPEC_SIGNING_KEY` environment entry on a GKE Job either, alongside the
  Cloud Run names (the same allow-list test, extended to the GKE dispatcher).
* **The runtime alternative (decision 4).** The worker calls
  `GetPublicKey` on the version the document names, after the prefix check
  below, holds the PEM in memory for the attempt (one attempt is one process,
  so there is nothing longer-lived to cache in), and runs inside
  `startup_budget()`. That needs `roles/cloudkms.publicKeyViewer` on the key
  for every worker account, granted from `terraform/infra` and from
  `register-tenant.sh` for script-registered tenants, which puts
  `setIamPolicy` on the key back in the deployer's hands. It gives instant
  revocation (below) at that price. **Not verified:** whether `GetPublicKey`
  refuses a DISABLED version. If it does not, this alternative also needs
  `roles/cloudkms.viewer` on the key so the worker can read the version's
  state, and the live API has to answer that before it is chosen.
* **Rotation.** Cloud KMS does not rotate asymmetric keys on a schedule, so
  rotation is by hand and in this order: (1) create version N+1 (bootstrap, or
  `gcloud kms keys versions create`); (2) release, so every Job's
  `SPEC_VERIFY_KEYS` holds N and N+1; (3) point swarm-api's
  `SPEC_SIGNING_KEY_VERSION` (a full version name, because an asymmetric key
  has no primary version) at N+1 and release; (4) once no non-terminal agent
  names version N, disable it and release. Several enabled versions verify at
  once; that is what makes steps 2 to 4 safe.
* **Revocation.** Disable the version. With the recommended rendering, every
  agent signed by it is refused from the next release on; with the runtime
  alternative, from the next attempt start. A signing identity that was
  compromised could sign anything by submitting it through the API anyway, so
  revocation is about closing the leak, and a release is fast enough for that.
* **`AsymmetricSign` quota, not verified.** Every agent and every workflow step
  is one `AsymmetricSign` call at submission (one per step, decision 2), so the
  platform's submission rate is Cloud KMS's request rate for this key. The
  default Cloud KMS asymmetric-sign quota is per project and per region and is
  shared with anything else in `saga-agents-staging` calling `AsymmetricSign`
  in `us-central1` -- including, potentially, the other team's own use of KMS
  in that project. **Not verified here:** the project's current quota value,
  its current headroom against the other team's usage, and the platform's
  peak submission rate against it. Before `enforce` is the default (section
  6), this needs a check against
  `gcloud quotas info list --service=cloudkms.googleapis.com`
  (`gcloud services quota list` does not exist: gcloud 586 answers
  `Invalid choice: 'quota'`; the runbook,
  [spec-signing-rollout.md](runbooks/spec-signing-rollout.md#not-verified-here),
  has the full command and the values read 2026-10-01) or the Cloud
  KMS quota page for this project, and a quota increase request filed ahead
  of the rollout if headroom is thin -- a KMS `RESOURCE_EXHAUSTED` here is a
  503 on every submission (this section's "Fails closed"), not a soft
  degradation.

#### 4. Signing in swarm-api

* **Where.** In `SubmissionService.submit_tasks` and `submit_workflow`
  (`apps/swarm-api/swarm_api/service.py`), after every write to the agent has
  been made and immediately before `self._store.create_tasks` /
  `create_workflow`. For a workflow that is after the loop that writes
  `metadata.input_from` and after `record_expected_outputs`, both of which run
  after `_build_task`; signing inside `_build_task` would sign a spec missing
  both.
* **What.** For each agent: `canonical_step_spec(task.to_firestore(),
  task_id=task.id)`, SHA-256, then KMS `AsymmetricSign` with `digest.sha256`
  and `digest_crc32c`, checking `verified_digest_crc32c` and the response's
  `name`. The three fields are set on the `Task`, so they are part of the write
  that creates the document, never a second update.
* **Refused before anything is signed.** `SpecNotCanonical` from the
  canonicaliser is 422 `invalid_input`, counted as a rejected submission like
  every other refusal.
* **Fails closed.** If KMS cannot sign, the submission answers 503 and nothing
  is stored. After the cutover (below) swarm-api never writes an unsigned
  agent.
* **One sign call per step**, concurrently for a workflow, as the owner
  decided ("each step's canonical spec"). A per-workflow manifest signed once
  was considered (decision 2).
* **New dependency:** `google-cloud-kms` for `swarm-api` only.

#### 5. Verification in the worker

**Where.** In `Lifecycle._prepare`, **immediately after
`self.control.fetch_task()` (step 4, today around line 749)** and before
anything reads the document for what to do: before `_restore_checkpoint`,
before `_maybe_clone` (which reads the tenant's git token through
`_git_token`), before `_stage_declared_inputs`, before `_build_child_env`
(step 6, the provider credential), before `issue.stage_issue` (#265) and so
long before the runner child exists. Nothing between the generation check and
that point reads a spec or a secret: step 2 records the attempt and advances
it to RUNNING, and step 3 makes an empty workspace. It gets a phase of its
own, `verify_spec`, so the phase log proves the order. For a worker-action
profile (contract request 33) the same call runs at the same point, before the
action reads its secret.

**What it checks, in this order, refusing at the first failure.** Corrected
from the first draft of this entry, which put the format check first: an
unsigned legacy agent has no `spec_format` either, so checking it before the
legacy rule refused every legacy agent with `unknown_format` instead of
admitting it. The presence/legacy check has to run first.

1. `spec_signature` and `spec_key_version` are present, or the legacy rule
   below applies (in which case the agent runs and checks 2 to 5 are skipped).
2. `spec_format` is a format this worker knows (1).
3. `spec_key_version` is `<SPEC_SIGNING_KEY>/cryptoKeyVersions/<digits>` and
   is a key of `SPEC_VERIFY_KEYS`. **Checked as a string, before any key is
   used:** the document can name any version of any key in any project, and
   the worker never goes looking for one.
4. `canonical_step_spec(doc, task_id=cfg.task_id)` succeeds, and its digest
   verifies against `spec_signature` with that version's public key
   (`cryptography`, added to the worker's dependencies explicitly).
   **`SpecNotCanonical` is a refusal, not a crash:** the document is writable
   by any agent of the tenant (**What is true today**, above), so a covered
   field can hold a value the canonicaliser rejects (an out-of-range integer,
   a non-finite float, a lone surrogate) without ever having gone through
   swarm-api's submission-time check. `_prepare` catches
   `SpecNotCanonical` from this call specifically and maps it to
   `SpecSignatureInvalid` with reason `not_canonical`, the same way an
   unverified signature is refused; letting it propagate would crash the
   worker process instead of failing the agent cleanly.
5. The execution's own environment agrees with the signed spec:
   `RUNNER_PROFILE`, `TENANT_ID`, `TASK_TIMEOUT_SECONDS`, `REPOSITORY_URL` /
   `REPOSITORY_REF` when set, and the Job's own identity against the Job name
   the scheduler derived from `tenant_id` and `runner_profile` (one Job per
   tenant per profile). The scheduler derives `RUNNER_PROFILE`, `TENANT_ID`
   and `TASK_TIMEOUT_SECONDS` from the document, so a rewritten
   `runner_profile` that got a step dispatched onto another profile's Job is
   refused here even though step 4 alone would catch it too.

   **The two backends' Job-identity checks are not equally strong, and this
   entry says so rather than implying otherwise.** On **Cloud Run**,
   `CLOUD_RUN_JOB` is set by the platform itself -- the execution environment,
   not anything the scheduler wrote -- so comparing it catches a scheduler bug
   that dispatched onto the wrong Job while still copying the right
   environment variables across; that check is independent of the scheduler's
   own arithmetic. On **GKE**, there is no such platform-injected value: the
   dispatcher's manifest template sets `RUNNER_JOB_NAME` itself, from the same
   `tenant_id`/`runner_profile` computation that also produces the Job's
   actual `metadata.name` and the other env vars in the same render. **This
   check on GKE only confirms the scheduler agrees with itself within one
   render** -- it catches a copy-paste mismatch inside `GkeJobDispatcher`, not
   an independent confirmation of which Job the pod is actually running in the
   way Cloud Run's does. Closing that gap on GKE would need the worker to read
   its own Job's name from the Kubernetes Downward API (`metadata.name` into
   an env var, which needs no RBAC) rather than from a value the same
   dispatcher wrote twice; not proposed here, since GKE runs only the
   `browser` profile today and a wrong-Job dispatch there still fails step 4
   (`RUNNER_PROFILE` disagreement) or the tenant-namespace boundary itself.

**After it passes, the worker reads only the verified dict.** It already does:
the rest of the attempt reads `self._task`, and the later `fetch_task` calls
(`validate_generation`, `poll`, `_current_state`) read only the state,
generation and cancel flag, none of them covered.

**On a refusal.** A new `SpecSignatureInvalid(WorkerError)`. The lifecycle
closes the startup window and calls `control.finish(state=FAILED,
end_cause=SPEC_SIGNATURE_INVALID, exit_code=ExitCode.FAILED, error=<reason
and task id, no spec content>, result_summary={"spec_check": {"reason": ...,
"task_id": ..., "key_version": ..., "digest": <hex>}})`. **FAILED directly,
whatever attempts are left:** a retry reads the same document. The write is
fenced like every terminal write, so a superseded worker stands down instead.
The reason is a worker vocabulary, not a frozen one (`signature_mismatch`,
`unsigned`, `unknown_format`, `not_canonical`, `foreign_key_version`,
`environment_mismatch`, `upstream:<task id>:<reason>`), the way
`publish_reason` is. The log line is
ERROR and carries the same fields; the spec itself is never logged, since a
prompt can hold anything. The process exits 1.

**Outages are not refusals.** With the recommended rendering, verification
makes no network call. Under the runtime alternative, a KMS `UNAVAILABLE` or a
spent budget is exit 69 (`UNAVAILABLE`: nothing written, the reconciler
retries), a `PERMISSION_DENIED` is exit 78 (`CANNOT_START`: the worker's own
configuration), and only `NOT_FOUND` or a disabled or destroyed version is a
refusal.

**Worker-action steps verify their upstream steps too** (the owner's
decision). Before it reads its credential, contract request 33's
`post-verdict` step fetches `review.json` from the path it computes itself,
using the review task id **named in its own signed `metadata.dispatch.verdict_source`**
(`{review: <review task id>}`, the same way `merge`'s `dispatch.merges` block
names task ids, not through `input_from`). **Corrected from the first draft of
this entry, which said
`post-verdict` uses `input_from`:** contract request 33's joint review with
this entry, 2026-09-29, removed `post-verdict`'s `input_from` on purpose --
the ordinary staging path resolves an artifact's location from the upstream
agent's own `result_summary`, a Firestore field the tenant identity writes,
which is exactly the tenant-writable pointer this whole design exists to stop
trusting. `merge` verifies the **union** of every agent its own signed spec
names: the `dispatch.merges` block (author, review, **post-verdict**, fix,
proof) and `input_from`. `post-verdict` is in that union because `merge`
trusts what `post-verdict` posted to GitHub every bit as much as it trusts
`review`'s verdict file. Each upstream agent must pass checks 2 to 4 above
**with no legacy exception**, and in addition carry the acting step's own
`tenant_id` and `workflow_id` and the role contract request 33's `single-pr`
table gives it: `reader` for `review` and `proof`, `amender` for `fix`,
`author` for the implement step, and **`none`, the same as `merge` itself,
for `post-verdict`** -- it clones nothing, holds no `pr_role`, and only reads
an upstream verdict and posts it. (This entry's earlier draft gave
`post-verdict` role `reader`; contract request 33's own table has always said
`none`, and this entry now agrees with it rather than the other way round.)

**The end causes are deliberately not unified with #342's own (the owner's
decision, 2026-09-29): a worker-action step's own spec failing its own check
is still `SPEC_SIGNATURE_INVALID`,** exactly as for an agent step. **An
upstream agent failing this check is contract request 33's own concern, not
this entry's, and keeps that entry's own end causes:** `merge` ends
`EndCause.MERGE_REFUSED`, and `post-verdict` ends `EndCause.VERDICT_REFUSED`
(contract request 33 names and owns both; this entry only requires that an
upstream-spec failure use them, never `SPEC_SIGNATURE_INVALID`). **The owner's
decision, 2026-09-29: both report through the same field this entry uses for
its own refusals,** `result_summary.spec_check.reason`, with the value
`"upstream:<task id>:<why>"` -- the same shape `SPEC_SIGNATURE_INVALID` uses
for its own failures, so a reader (or an alert) does not need two different
field names to find out why a spec check failed. **Decision 9's alert is keyed
on both:** a log-based metric over `end_cause == SPEC_SIGNATURE_INVALID` alone
would miss every worker-action refusal caused by a tampered upstream spec,
since those end `MERGE_REFUSED` or `VERDICT_REFUSED`; the alert matches
`end_cause == SPEC_SIGNATURE_INVALID` **or** (`end_cause` in `{MERGE_REFUSED,
VERDICT_REFUSED}` **and** `result_summary.spec_check.reason` starts with
`"upstream:"`), because both are the same threat -- a spec that does not
verify -- surfacing through two different steps' vocabularies, and `MERGE_REFUSED`
/ `VERDICT_REFUSED` have other, unrelated causes (contract request 33's own
table) that this alert must not also fire on. Verifying an upstream spec proves that the step
the action trusts was submitted as that step of this workflow and still
carries that spec. It does not prove what that step produced (next section).

#### 6. Rollout: legacy unsigned tasks

Every agent written before swarm-api signs carries no signature, and some of
those will still be parked or queued when the verifying worker ships. Signing
cannot be retrofitted onto them: re-signing a document as it now stands would
sign whatever an agent already wrote into it.

* **`SPEC_SIGNATURE_MODE`**, on the worker Jobs: `enforce` (**the default**)
  or `legacy`. In `legacy`, an unsigned agent runs, with a WARNING log and a
  `spec_check: {"reason": "legacy_unsigned"}` note on its event, **only if the
  document's Firestore `create_time` is earlier than `SPEC_LEGACY_CUTOVER`**
  (an RFC 3339 time, the moment the signing swarm-api revision took all
  traffic). `create_time` is set by Firestore and no client can write it, so
  stripping the signature off an agent created after the cutover does not
  qualify it; the tenant's role has no delete, so the document cannot be
  re-created with an earlier one either. `fetch_task` returns the snapshot's
  `create_time` beside the dict for this.
* **The end date is in the code, not only the configuration:**
  `SPEC_LEGACY_UNTIL = 2026-10-20T00:00:00Z` (decision 5). After it the worker
  ignores `legacy`, logs that it did, and enforces. A flag nobody remembers to
  turn off stays off anyway.
* **Before the end date**, an operator counts non-terminal agents without
  `spec_signature` (a read-only query) and lets them finish or cancels and
  resubmits them; after it, any left fail `SPEC_SIGNATURE_INVALID` with reason
  `unsigned`.
* **Worker-action steps never take the legacy path, for themselves or their
  upstream steps.** Contract request 33 is not built yet, so no legacy
  worker-action agent can exist.
* **Order:** (1) the key and its IAM (bootstrap), then the contract fields and
  `specsign.py`; (2) swarm-api signs, and its release time becomes
  `SPEC_LEGACY_CUTOVER`; (3) the worker verifies, with `legacy` and the
  cutover set; (4) `enforce` by the end date at the latest. A worker that
  enforces before swarm-api signs refuses every agent, which is why (2) and (3)
  are separate releases.

#### 7. Continuations and retries

* **A retry is another attempt at the same agent.** Same document, same spec,
  same signature, nothing re-signed. That is exactly why no field a retry
  changes (`attempt_count`, `current_generation`, `state`, `next_eligible_at`,
  `startup_refunds`) is covered. The scheduler's
  `return_to_ready_after_failed_dispatch` and the reconciler's
  `repair_task_state` write none of the covered fields, and a test keeps it so.
* **A continuation (#273's `continues_task`) is a new submission.** It is a
  `direct-pr` workflow submitted through `submit_workflow`, so its agent is
  signed at its own submission, and the continued task id is inside its signed
  `metadata.dispatch.continues`. An agent therefore cannot point a
  continuation at another agent's branch. The continued agent's own signature is
  never consulted: nothing about the continued task's spec is read, only its
  id, from which the worker derives the branch (`continuation.py`).
* **Contract request 30's continuation-scoped accounts** submit through the
  same code, so their agents are signed identically and `submitted_by` records
  the account.
* **Any future re-run that creates a new agent** goes through swarm-api and is
  signed at its own submission. No path may copy a signature from one agent to
  another; it would not verify anyway, because `task_id` is inside the bytes.

#### 8. The threat model

**What signing closes.** A rewrite, by any agent of the tenant (or by anything
holding any worker's Firestore grant), of a step's `input` (prompt and
inputs), `input_from`, `expected_outputs`, dispatch roles, `depends_on`,
profile, class, timeout, attempt cap or repository, **before or between that
step's attempts**. The worker refuses it before any credential is read and
before any agent or action starts. Copying one agent's signature onto another,
moving a spec to another workflow or tenant, forging a new task document, and
naming a key the platform did not publish are all refused the same way.

**What signing does not close,** stated plainly because the platform's
exposure is otherwise unchanged
([multi-tenancy.md](multi-tenancy.md#the-firestore-row-in-full)):

* **The restored workspace, a channel the signature does not cover at
  all.** `latest_checkpoint` is in the "not covered" list (section 1) because
  the reconciler and the worker itself write it legitimately mid-attempt. But
  that also means a signed, unmodified, correctly-verifying spec can still
  start an attempt against a workspace restored from a **planted**
  checkpoint: the signature is over the spec, never over what
  `_restore_checkpoint` pulls from GCS before the runner starts. Joint review
  finding, 2026-09-29 (MAJOR): a planted checkpoint gets past this entry's
  signature entirely, because there was never anything here for it to get
  past.

  **Fixed separately, ahead of this entry: S0 #347, closed by PR #348.**
  #348 makes `_restore_checkpoint` accept only the checkpoint this agent's own
  earlier attempt recorded on its own attempt document (never
  `find_latest()`'s scan of the agent's GCS prefix), and never restores
  anything on a first attempt. **State it as #348's own body states it, not
  more strongly:** this closes the reproduction in #347 (an implement agent
  planting for a parked review step's first attempt) and, on a retry, every
  path limited to the GCS bucket -- a checkpoint added beside the recorded one,
  or the recorded one's archive rewritten in place. **It does not stop an
  agent that can also write Firestore.** Firestore has no document-level IAM
  (`multi-tenancy.md`), so on a retry such an agent can still raise the
  victim agent's `attempt_count`, repoint `latest_checkpoint`, and write a
  forged `attempts/<id>` document naming its own planted checkpoint and
  digest -- every check #348 added then passes, because all of it reads
  Firestore fields and documents the tenant identity can write. #348's own
  words: "the complete fix needs a record the tenant cannot write." Signed
  step specs do not supply that record: `latest_checkpoint` and `attempt_count`
  are both explicitly in contract request 34's own "Not covered" list
  (section 1, above), by design -- the scheduler and the worker legitimately
  rewrite both after submission, so signing either would break on the first
  honest write -- and the `attempts/<id>` document is a different Firestore
  collection entirely, never a candidate for this entry's per-agent signature
  in the first place. Extending coverage to close this was not the owner's
  decision (decision 7 covers only re-checking a signed *parent's* `state`, a
  different field, for a different attack).

  **The owner's decision, 2026-09-29: this residual is ACCEPTED, not a
  precondition of anything closing.** Firestore-capable planting on a retry
  remains open until workers leave direct Firestore access (the alternative
  **If it is declined** already names, and the same one #347's issue and
  #348's PR both point at), a materially larger change than either #347 or
  this entry. Contract request 34 closes the rewritten-prompt path #342
  reproduced; it was never going to also close a checkpoint-planting path
  that reads and writes fields it deliberately does not sign.
* **Every field that is not covered.** A tenant agent can still write an
  agent's `state`, `result_summary`, `cancel_requested`, `latest_checkpoint`
  and the rest. In particular:
  * **It can mark a parked step READY early**, without touching
    `depends_on`, and the scheduler will admit it. The signature still
    verifies, because the spec is unchanged. In contract request 33's chain
    that would let `fix` start before `post-verdict` has posted the review,
    which is the ordering merge-step.md §4.3 relies on to keep `fix`'s agent
    away from `review.json`. Decision 7 adds a start-time re-check of each
    signed parent's state, which forces the attacker to forge those states as
    well; it raises the cost, and does not close it.
  * **It can re-run a finished signed step by rewriting its state.** `state`
    and `end_cause` are both uncovered, so an agent can write a SUCCEEDED or
    FAILED agent's `state` back to `READY` (or `QUEUED`, ahead of admission)
    and have the scheduler dispatch a fresh attempt against the same,
    still-verifying spec -- the signature says nothing about how many times a
    step may run. A re-run costs the tenant's own capacity and attempt cap,
    and can re-execute a step whose real effect (a merge, a posted review) has
    already happened and is not naturally idempotent. Contract request 33's
    sha-equality chain and GitHub-side checks are what stop a re-run from
    double-merging or double-posting; this entry stops none of it, because
    `state` is not a field this entry signs.
  * **It can forge an upstream step's `result_summary` or its GCS
    artifacts.** Contract request 33 answers that for the merge with the sha
    equality chain and the GitHub review `post-verdict` posts, not with this
    entry.
* **The workflow document.** `WorkflowStep.input` is a copy for display. No
  worker reads it, so a rewrite of it misleads a person reading the API or the
  UI and changes nothing that runs.
* **Everything outside `tasks/*`:** `tenants/<id>` (whose `service_account`
  and `gcs_prefix` the control plane trusts as configuration), `pools/*`,
  `quota/*` and `leases/*`. These need the fix multi-tenancy.md names, the
  worker off direct Firestore access or one database per tenant.
* **Capacity spent before the refusal.** The scheduler admits on the
  document's `resource_class` before any worker verifies, so a rewritten
  class costs the tenant's own capacity for one attempt (decision 8).
* **Anyone who can sign -- an accepted residual, not a closed one (owner,
  2026-09-29).** `swarm-api`'s own account, which holds `signer`; the release
  deployer, which deploys the Cloud Run revision that runs as it; and, because
  the key stays in the shared `saga-agents-staging` project rather than a
  dedicated one, **any Owner or Editor of that project** -- `roles/editor`
  carries `iam.serviceAccounts.actAs` and `run.services.update`, enough to
  redeploy `swarm-api` running arbitrary code as its own service account, and
  an Owner can `setIamPolicy` the key, the ring or the project directly
  regardless of where the key's Terraform lives. **A KMS admin who creates or
  imports a version** gets it trusted by every worker at the next release,
  without ever signing anything themselves (**Key management**'s "Trusted
  versions", above). Putting the key's own IAM binding in `terraform/bootstrap`
  keeps the *CI deployer* off `setIamPolicy` on the key; it was never a
  boundary against the shared project's own Owners and Editors, and this entry
  no longer implies that it is. **This residual holds against the platform
  team and the other team sharing the project. It does not hold against
  tenant agents:** no tenant or worker account holds any role on this key, so
  a tenant's own agent -- the actor #342 and this entry are about -- has no
  KMS-side path to a valid signature at all.
* **Anything submitted through the API.** A signature says swarm-api accepted
  this spec from an authenticated submitter. It does not say a person meant
  it: a continuation-scoped account (contract request 30) or a stolen ID
  token gets a valid signature on what it submits.
* **Prompt injection** of the step itself (contract request 33's T11).

#### 9. Tests that would prove it

All offline, as CLAUDE.md requires: no KMS, no credentials, no emulator. The
signer in tests is a local P-256 key from `cryptography` behind the same
interface as the KMS signer, and the worker's `SPEC_VERIFY_KEYS` holds its
public key. Each goes to CI red first, in the shape CLAUDE.md gives.

* **`tests/unit/common/test_specsign.py`** (the canonical form).
  RFC 8785's own number and ordering vectors (Appendix B, and the sorting
  example in 3.2.3); a pinned golden vector, one fixed task dict to fixed bytes
  and a fixed SHA-256 hex, so any change to the projection is a visible diff
  that demands a format bump; agreement with the `rfc8785` package as a
  test-only oracle over generated JSON values; and refusal of 2^53, NaN,
  infinity and a lone surrogate.
* **`tests/unit/common/test_specsign_covers.py`** (the rule that keeps the
  field list honest). Every metadata key the worker reads (`inputs.METADATA_KEY`,
  `expected_outputs.METADATA_KEY`, `"dispatch"`, found by an AST scan of
  `agent_worker`) is in `SIGNED_METADATA_KEYS`, and `SIGNED_METADATA_KEYS`
  equals swarm-api's `RESERVED_METADATA_KEYS` minus `startup_refunds`.
* **`tests/unit/control_plane/test_spec_signing_submission.py`** (swarm-api).
  Every agent from `submit_tasks` and `submit_workflow` carries the three
  fields, and its signature verifies over its stored form; the signed bytes
  include the task-id-keyed `input_from` and the `expected_outputs`, so a
  signature made before either was written fails this test; a KMS failure is
  503 and the store is never called; each non-canonical value is 422 and KMS
  is never called.
* **`tests/unit/worker/test_spec_signature_worker.py`** (the worker).
  Parameterised over **every covered field**, nested ones included
  (`input.prompt`, `input.issue`, `metadata.dispatch.role`,
  `metadata.dispatch.integrates` reordered, a value of `metadata.input_from`,
  `depends_on` reordered or shortened, `repository_ref`): a rewrite after
  signing ends the agent FAILED with `spec_signature_invalid` on attempt 1 of 3,
  and the fakes record **no runner child, no secret read, no git token read,
  no GCS download and no checkpoint restore**. Parameterised over every
  uncovered field (`state`, `attempt_count`, `result_summary`,
  `startup_refunds`, `priority`, a caller metadata key): the agent runs. A
  signature copied from another agent, a version outside `SPEC_SIGNING_KEY`, a
  version not in `SPEC_VERIFY_KEYS`, a `RUNNER_PROFILE` override that
  disagrees, and (Cloud Run only, where the platform sets `CLOUD_RUN_JOB`
  independently of the scheduler) an execution whose `CLOUD_RUN_JOB` names a
  Job other than the one the scheduler derived from `tenant_id` and
  `runner_profile`, are each refused. A separate GKE case proves the weaker
  claim honestly: a `RUNNER_JOB_NAME` that disagrees with the dispatcher's own
  `tenant_id`/`runner_profile` computation in the same render is refused, but
  a test also documents that this check cannot catch the dispatcher deriving
  the wrong Job **consistently** (setting `RUNNER_JOB_NAME` and the Job's real
  `metadata.name` to the same wrong value), since GKE has no platform-injected
  value to check against. A covered field holding
  a value `canonical_step_spec` rejects (an integer past 2^53 − 1, a
  non-finite float, a lone surrogate -- written directly to Firestore, not
  through swarm-api's submission check) is refused `not_canonical`, and the
  worker process does not crash. **The checks run in the order section 5
  gives**, proven by a legacy-eligible agent with no `spec_format` at all
  (never `unknown_format`) alongside an out-of-window unsigned agent that
  correctly still gets `unsigned`. The phase log shows `verify_spec` before
  `restore_checkpoint`, `clone`, `stage_inputs`, `credentials` and
  `fetch_issue`.
* **`tests/unit/worker/test_spec_legacy_window.py`.** Unsigned in `enforce`:
  refused. In `legacy` with `create_time` before the cutover: runs, with the
  note. After the cutover: refused. After `SPEC_LEGACY_UNTIL`, with an injected
  clock: refused, and the log says the flag was ignored.
* **Worker-action upstream checks**, in contract request 33's own test files
  when it lands: `post-verdict` and `merge` refuse an upstream agent that fails
  verification, is unsigned (in `legacy` mode too), belongs to another
  workflow or carries the wrong role -- **`post-verdict` included** in
  `merge`'s upstream set, with role `none` (matching contract request 33's own
  `single-pr` table); the merge and review credentials are never read in any
  of those cases; and the end cause on such a refusal is `EndCause.MERGE_REFUSED`
  (`merge`) or `EndCause.VERDICT_REFUSED` (`post-verdict`) -- contract request
  33's own causes, never `SPEC_SIGNATURE_INVALID` -- with
  `result_summary.spec_check.reason == "upstream:<task id>:<why>"`. That cause
  is reserved for the acting step's own spec, and a test in this entry's own
  suite asserts the two causes are never confused for the acting step's own
  failures.
* **Writers.** A test that the scheduler's and the reconciler's agent updates
  (`return_to_ready_after_failed_dispatch`, `cancel`, `repair_task_state` and
  the rest) write no covered field; that **swarm-api's own writes to an
  existing agent** (`Store.request_cancel`/`cancel_workflow`, `store.py`, which
  patch `cancel_requested`, `state` and `completed_at` -- the only writes
  swarm-api itself makes to an agent it did not just create -- and any future
  one) also write no covered field --
  the three signed fields are written once, at creation, and never again
  (section 2's diff comment), so this test fails loudly the day a second
  swarm-api write path touches, say, `input` or `metadata.dispatch`; and that
  `worker_env()` never sets `SPEC_VERIFY_KEYS`, `SPEC_SIGNING_KEY`,
  `SPEC_SIGNATURE_MODE` or `SPEC_LEGACY_CUTOVER` **on either dispatcher** --
  Cloud Run's per-execution override and the GKE manifest's template
  substitution both go through the same allow-list check.
* **The ledger.** `test_outcomes_end_cause.py` gains the class, and
  `DERIVE_VERSION` is bumped.
* **Terraform (`tests/terraform`).** The key's purpose and algorithm; its
  `managed-by` label; `swarm-api` is the only member holding `signer`, and no
  tenant or worker account holds `signer` or `signerVerifier`; the Cloud Run
  Jobs' environment carries `SPEC_VERIFY_KEYS` built from enabled versions
  only; `cloudkms.googleapis.com` is enabled; the key ring and the key's IAM
  member are in `unlabelable-types.json`.
* **GKE (`tests/unit/worker/test_kubernetes_manifests.py` and
  `tests/terraform`).** The rendered `swarm-spec-verify-keys` `ConfigMap`
  carries the same `{version: PEM}` map as `SPEC_VERIFY_KEYS`, lives in the
  tenant's own namespace, and is applied by Terraform's identity, not the
  scheduler's per-agent render; `worker-job-browser.yaml`'s manifest and the
  dispatcher's rendered Job agree that neither sets `SPEC_VERIFY_KEYS` or
  `SPEC_SIGNING_KEY` in `env:` and both mount the ConfigMap read-only.
  **The write-grant check is wider than one `RoleBinding` lookup, because a
  write path could come in by any of several doors:**
  * no `RoleBinding` in the tenant namespace (`kubernetes/rbac/*.yaml`) grants
    `update`, `patch` or `delete` on `configmaps` to the `swarm-worker` Role,
    to any subject bound to it, or **to the tenant's GSA named either way a
    Kubernetes subject can name it** -- by email
    (`swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com`) and by
    its numeric `uniqueId` -- since an email-only check here would repeat the
    defect a prior lane already measured elsewhere in this platform: an
    email-only RBAC subject can apply cleanly and bind nothing, which reads as
    a passing test while authorising nobody, but the reverse -- a binding
    keyed on `uniqueId` alone -- would be invisible to a check that only
    greps for the email;
  * no `ClusterRoleBinding` grants it either, cluster-wide, to any subject --
    matching `kubernetes/rbac/worker-rbac.yaml`'s existing invariant that no
    `ClusterRole` or `ClusterRoleBinding` appears anywhere in `kubernetes/` at
    all, which this ConfigMap must not become the first exception to;
  * and, in `tests/terraform`, no project-level IAM binding grants the
    tenant's GSA a `container.*` role or permission (`roles/container.admin`,
    `roles/container.developer`, or a custom role naming
    `container.configMaps.update` or `container.secrets.update`) that would
    let it write the object through the GKE API directly, bypassing in-cluster
    RBAC entirely -- the tenant's GSA holds no `container.*` role today
    (Terraform's `modules/iam`), and this test pins that absence rather than
    assuming it.

  This is the same assertion style `test_kubernetes_manifests.py` already uses
  to hold the empty worker Role empty.
* **Live, in dev, once, after (3) of the rollout.** Submit a two-step workflow,
  rewrite the parked step's `input.prompt` with the tenant worker account's
  own credentials, and see the step end `spec_signature_invalid` with no
  agent started. Worth keeping as a `make smoke` case, since this is the one
  guarantee the entry exists for.

### What it would break if accepted

* **Nothing stored.** The three fields are optional and an old document
  decodes with None. What an unsigned agent does is the rollout's question
  (section 6), not the decoder's.
* **Every writer that creates a task document must sign.** Today that is only
  swarm-api's `Store`. The integration tests that seed task documents
  directly in the emulator need the test signer.
* **The API refuses three inputs it accepts today**: integers past 2^53 − 1,
  non-finite numbers and lone surrogates. No declared input's bounds reach
  2^53 (the largest is `issue`'s 999999), so only undeclared profiles' inputs
  (`browser`, `generic`) can be affected.
* **A submission now depends on Cloud KMS.** A KMS outage stops submissions
  (503) instead of accepting unsigned ones; that is the point.
* **Every restatement of `EndCause` follows:** the outcome ledger
  (`swarm_api.outcomes`, with its `DERIVE_VERSION` bumped), the UI's
  `outcomes.ts`, `plugin/README.md` and `docs/workflows.md`.
* **Terraform:** a new API, a key ring and key in bootstrap, two entries in
  `unlabelable-types.json`, Cloud Run worker environment in `terraform/infra`,
  and a per-tenant `swarm-spec-verify-keys` `ConfigMap` (**Key management**,
  above) that `register-tenant.sh` and every existing tenant's namespace need.
* **`kubernetes/`:** `worker-job-browser.yaml` gains a read-only volume mount
  for the ConfigMap; `test_kubernetes_manifests.py` gains the assertion that
  no RoleBinding grants the worker KSA write on it.
* **If contract request 3 is applied** (`input_from` as a `Task` field), the
  projection changes and that is format 2.

### If it is declined

#342 stays open at S0. Every multi-step workflow runs whatever its parked
steps' documents say when they start, and contract request 33's merge step has
no answer to its round-3 blocker: a review step whose prompt an earlier agent
rewrote produces a real, honest-looking verdict that `post-verdict` would post.
The remaining alternative is the one multi-tenancy.md names, taking workers
off direct Firestore access, which closes this and much more at a far larger
cost.

### Invariants

- **Invariants 1 to 3.** Unchanged. Signing happens before the agent exists;
  a refusal is an ordinary terminal write that releases the lease in the same
  way as every other.
- **Invariant 4.** No wait is added. With the recommended rendering,
  verification makes no network call; under the alternative the one call is
  inside the startup budget, and an outage is exit 69, not a sleep.
- **Invariant 5.** Verification runs after the generation check, and the
  refusal's terminal write is fenced: a stale worker stands down without
  writing it.
- **Invariants 6 and 7.** Untouched.
- **Invariant 8.** Untouched. A refused attempt has done no work to keep.
- **Invariant 9.** Strengthened within a tenant, which is where #342 lives,
  and across tenants for task documents: the signature binds `tenant_id`, and
  `_assert_tenant` keeps its check. No tenant account gains any KMS role.
- **Invariant 10.** The signature binds exactly what the caller chose by name
  and what swarm-api derived from it. The caller still sends no image,
  command, resource spec or backend parameter, and cannot choose a key.
- **#219.** Verification runs before any secret is read, so a refused step
  never holds the tenant's git token or provider key.

### Owner decisions, each with a recommendation

1. **The covered fields.** Recommended: the table in section 1, with
   `priority` and the caller's own metadata keys left out.
2. **One signature per step, or one per workflow.** Recommended: one per step,
   as decided on 2026-09-29. A per-workflow manifest is one sign call instead
   of N, but every worker would read and verify the whole workflow's specs, and
   a standalone agent would need a second shape.
3. **The canonicaliser.** Recommended: RFC 8785 written into `specsign.py`,
   with the `rfc8785` package as a test-only oracle, because `swarm_common`
   takes no dependency.
4. **Where the key lives, and how workers get the public key.** Recommended:
   ring, key and the `signer` binding in `terraform/bootstrap`; public keys
   rendered by `terraform/infra` into the Jobs' `SPEC_VERIFY_KEYS`; no KMS role
   for any worker account; the deployer holds only `publicKeyViewer` and
   `viewer`. The alternative (runtime `GetPublicKey` with `publicKeyViewer` on
   every worker account) revokes faster but returns `setIamPolicy` on the key
   to the deployer.
5. **The legacy window.** Recommended: `enforce` by default, `legacy` only
   for agents whose Firestore `create_time` is before the cutover, and a hard
   `SPEC_LEGACY_UNTIL` of 2026-10-20.
6. **What a refusal does.** Recommended: FAILED at once, not retried and not
   parked, with `SPEC_SIGNATURE_INVALID` and an alert.
7. **A start-time re-check of parent states for every step with `depends_on`**
   (not only worker actions): each signed parent must be SUCCEEDED, or the
   worker parks the agent back as `DEPENDENCY_INCOMPLETE` without running.
   Recommended: yes, as defence in depth, stated as raising the cost of the
   early-release rewrite rather than closing it.
8. **Verification at admission too.** Recommended: not now. The worker is the
   gate; admission's hot path stays one query, and the cost of a rewritten
   class is one attempt of the tenant's own capacity.
9. **An alert on `spec_signature_invalid`.** Recommended: yes, a log-based
   metric and an alert in `terraform/modules/monitoring`, because every
   occurrence is an attack or a bug. **Keyed on both end causes** (**Verification
   in the worker**, above): `end_cause == SPEC_SIGNATURE_INVALID` for an
   acting step's own spec, or (`end_cause` in `{MERGE_REFUSED, VERDICT_REFUSED}`
   and `result_summary.spec_check.reason` starts with `"upstream:"`) for an
   upstream one -- the owner did not unify the end causes themselves, so the
   metric is what ties the two vocabularies to one alert, and the reason
   prefix is what keeps it from also firing on `MERGE_REFUSED` /
   `VERDICT_REFUSED`'s many other, unrelated causes.
10. **`SOFTWARE` or `HSM` protection.** Recommended: `SOFTWARE`. The private
    key never leaves Cloud KMS either way, and the threat here is who may call
    `AsymmetricSign`, which IAM decides, not extraction of the key.

### Addendum 2026-09-29: corrections after acceptance

The accepted text above is left as the owner accepted it. Three statements in
it were found wrong while building it (#353, #354), and one decision was added:

1. **The GKE Job name is derived from the agent, not the tenant and profile.**
   Requested change item 5 says the worker compares its identity against "the
   Job name the scheduler derived from `tenant_id` and `runner_profile` (one
   Job per tenant per profile)". That is Cloud Run's rule
   (`swarm-job-<tenant>-<profile>`). `GkeJobDispatcher` creates one Job per
   attempt, named `sanitize_name("swarm", <task id without "task_">,
   <generation>)`, that is `swarm-<hex>-<gen>`
   (`apps/scheduler/scheduler/dispatch.py`), and the worker checks
   `RUNNER_JOB_NAME` against `specverify.gke_job_name(task_id, generation)`.
   The weaker-claim paragraph about GKE still holds: the name and the
   environment come from one render.
2. **`cloudkms.googleapis.com` is enabled in bootstrap, not in
   `terraform/infra/main.tf`.** The key ring and key live in
   `terraform/bootstrap/spec_signing.tf` (#354), which enables the API and adds
   it to `prerequisite_services`; `terraform/infra` only reads the key's
   versions. Section 3's first bullet names the wrong root.
3. **A GKE worker reads the signature mode and the legacy cutover from the
   ConfigMap mount too** (owner decision, 2026-09-29). The text above has the
   GKE mount carrying only `SPEC_VERIFY_KEYS` and `SPEC_SIGNING_KEY`, which
   left a GKE worker in `enforce` from its first day while Cloud Run ran
   `legacy`. The `swarm-spec-verify-keys` ConfigMap rendered by
   `kubernetes/render.py` (#354) carries `SPEC_SIGNATURE_MODE` and
   `SPEC_LEGACY_CUTOVER` as files beside the keys, and the worker reads each
   of the four from its environment first and from
   `/etc/swarm/spec-verify-keys` where the environment has none. GKE therefore
   follows the legacy window exactly as Cloud Run does, including
   `SPEC_LEGACY_UNTIL`.
4. **A worker with no keys at all is CANNOT_START for every agent, signed or
   not.** The key check now runs before the legacy rule, so a GKE pod whose
   namespace has no ConfigMap (the volume is `optional`) can neither admit an
   unsigned agent through a mode it could not read nor refuse a tenant's agent
   as a signature failure: it exits `ExitCode.CONFIG`, the operator's fault,
   loudly.

### Addendum 2026-10-08: the GKE Job carries the keys in its environment

Owner decision, 2026-10-08. The "not inlined into the pod's `env:`" design
above is reversed. `GkeJobDispatcher._manifest` now puts the scheduler's own
`SPEC_VERIFY_KEYS`, `SPEC_SIGNING_KEY`, `SPEC_SIGNATURE_MODE` and
`SPEC_LEGACY_CUTOVER` (`scheduler.dispatch.spec_job_env`) on the worker
container's `env:`, as on a Cloud Run Job the scheduler creates. The
`swarm-spec-verify-keys` ConfigMap stays mounted read-only, as the fallback
the worker reads only when its environment carries no keys (amendment 3
above). Why: claude-code moved to GKE (PR 866), and onboarding (#847) creates
a namespace per person with no ConfigMap in it, so every agent there ended
`CANNOT_START` under amendment 4.

The tenant still cannot choose its keys. The values come from the
scheduler's settings and never from `worker_env()`, which an agent shapes; a
test holds both dispatchers to that. A pod's manifest is built by the
scheduler and is not writable from the tenant namespace. The worker still
fails closed on a missing or bad signature. Only a missing ConfigMap stopped
being fatal. Nothing under `apps/common/swarm_common/` changed.

---

## 35. `profiles.py` / `models.py`: the `post-verdict` worker-action profile, and its own end causes

**Superseded 2026-10-06 (lane MS0, part of #352):** the merge step anchors the verdict on the review's verdict file and green required checks, not on an App's GitHub review, and there is no review App ([2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs), §3). The applied entry stays disabled; retiring it is that revision's request (B), the owner's decision.

**Status: ACCEPTED — accepted by the owner 2026-10-01, as written; APPLIED 2026-10-01**
(functionality wave 3, lane M1): `WorkerAction.POST_VERDICT`, the
`post-verdict` entry (`available=False` until #342 is enforced and the review
App exists) and `VERDICT_REFUSED`/`VERDICT_FAILED`. Its MAJOR 1 companions:
the API routes' refusal is applied (request 33's amendment, item 1); the
`secret_manager` accessor override and the refresh exclusion are APPLIED
(request 33's amendment, items 2 and 3, recorded 2026-10-08: `accessor_overrides`
and `refreshable` in `terraform/modules/secret_manager`, tested by
`tests/terraform/merge_step_iam.tftest.hcl`). Part of #295. Recorded from
[merge-step.md](merge-step.md)'s design (contract request 33, ACCEPTED by
the owner 2026-09-29 as the design for #295), which pointed at this as a
separate, not-yet-filed request ([merge-step.md](merge-step.md)'s own §10
build item 2, §6a — not contract request 33's; §10 and §6a are merge-step.md's
own sections). The design is merge-step.md §4.3 and §6a. Nothing under
`apps/common/swarm_common/` has been edited.

**This request depends on contract request 33's `WorkerAction` enum and
`RunnerProfile.worker_action` field, which it extends rather than
restates.** If 33 is not yet applied when this is decided, apply 33 first;
this diff assumes it exists.

### What is true today

Contract request 33 gives `merge` a worker-action profile so an App key
never sits in a container that runs an agent. The verdict-anchoring design
in merge-step.md needs the identical isolation one step earlier in the
chain, for a different key: a GitHub PR review posted by a `swarm-review`
App, using the `swarm-tenant-<tenant>-git-review` secret. If that key were
held by the review step's own agent-running container (a `claude-code-review`
profile, contract request 36), the review agent — which reads
attacker-controlled diffs — would share a container with it, exactly the
exposure request 33's own §1.1 already rejects for `merge`. `post-verdict`
is the same shape as `merge`: a worker-action profile that runs no agent,
reads `review.json` (from the review-only-writable GCS prefix, at a path it
derives from its own signed dispatch, not through ordinary `input_from`;
merge-step.md §4.3), and posts the GitHub review.

`EndCause` has no value that says a verdict-posting attempt was refused or
failed, for the same reason request 33 gave `merge` its own pair: recording
one as `RUNNER_ERROR` would be false (no runner ran), and `None` sends the
outcome ledger back to classifying text.

### The requested change

In `profiles.py`, extend the `WorkerAction` enum contract request 33 adds:

```python
class WorkerAction(str, Enum):
    """A platform action the WORKER performs instead of starting a runner."""
    MERGE = "merge"
    POST_VERDICT = "post_verdict"
```

A catalogue entry:

```python
"post-verdict": RunnerProfile(
    name="post-verdict",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=(),
    worker_action=WorkerAction.POST_VERDICT,
    # The credential is never mounted: its secret is read by the worker at
    # post-time, as `swarm-<tenant>-post-verdict`, the Job's own service
    # account. `provider` is what parks the step CREDENTIAL_MISSING, at no
    # cost, for a tenant that has not registered one, and keeps Terraform
    # from creating a post-verdict Job for that tenant.
    #
    # Listing `git-review` here is not by itself sufficient to keep the
    # ordinary worker account off this secret (security review 2026-09-29,
    # MAJOR 1, NOT_YET) -- see "What it would break if accepted" below for
    # the three companion changes this entry depends on.
    provider="git-review",
    secrets=(),
    timeout_seconds=300,
    inputs={},
),
```

`__post_init__`'s existing refusal (request 33: `worker_action` requires
empty `runner_argv`, and vice versa) already covers this entry; nothing new
is needed there.

In `models.py`, two more `EndCause` values, written only by the worker,
alongside request 33's `MERGE_REFUSED`/`MERGE_FAILED`:

```python
VERDICT_REFUSED = "verdict_refused"   # a condition for posting the review was not met; nothing changed on the forge
VERDICT_FAILED = "verdict_failed"     # the post was allowed, and the forge did not do it
```

The specific reason goes in `result_summary.spec_check.reason` for a signed-
spec failure (`upstream:<task id>:<why>`, CR 34's own field and format — not
a `post-verdict`-only convention) and in `result_summary.verdict.refusal`
for every other refusal code (`verdict_unreadable`, `verdict_prefix_unavailable`,
`forge_unavailable`, `forge_refused`, ...). merge-step.md §6a lists every
code and its cause.

### What it would break if accepted

* **MAJOR 1 (security review 2026-09-29, NOT_YET): the catalogue entry alone
  makes the tenant's ORDINARY worker account a reader of the review App
  key.** `known_providers()` (`apps/swarm-api/swarm_api/validation.py::known_providers`)
  derives its accepted set from the catalogue, so `POST /me/credentials`
  (`apps/swarm-api/swarm_api/routes/tenants.py::put_credential`) and the admin route
  (`apps/swarm-api/swarm_api/routes/admin.py::_check_provider`) both accept `git-review`
  as an ordinary, registrable provider name; `credentials.py`'s
  `_grant_accessor` (202-235) then grants `tenant.service_account` — the
  tenant's ordinary worker account, not `post-verdict`'s — `secretAccessor`
  on it; and `terraform/modules/secret_manager/main.tf` (`resource "google_secret_manager_secret_iam_binding" "accessor"`) writes
  an authoritative `iam_binding` (not an additive `iam_member`) whose
  `members` list is `[cfg.accessor]` for every listed provider
  (`terraform/infra/main.tf` (`accessor      = cfg.accessor`) sets `cfg.accessor` to the worker
  account for every tenant), which on its next apply would also REMOVE any
  post-verdict accessor granted separately. `enable_subscription_refresh`
  compounds it: the refresher's `-refresh` twin grants the same worker
  account `secretVersionAdder` the same way. Left as written, any agent
  running under the tenant's worker account can mint that account's token,
  read the review App's private key out of Secret Manager, and post its own
  `APPROVE` review — the exact App-key theft B1 exists to close
  (merge-step.md §7, T3). **This request is accepted only together with:**
  * the API credential routes (`routes/tenants.py`, `routes/admin.py`) and
    `register-tenant.sh` refusing to bind `git-review` — and `git-merge`,
    see below — to the tenant's ordinary worker account;
  * `secret_manager` gaining a per-provider accessor override, so that for
    these two providers the `iam_binding`'s sole member is the post-verdict
    (respectively, merge) service account, never the worker account; and
  * `git-review` and `git-merge` being excluded from `refresh_secrets` and
    from the refresher's own admin grants, so no `-refresh` twin ever names
    the worker account for either provider.

  Without those three changes, this catalogue entry by itself reopens B1.
  **Contract request 33's `git-merge` has the identical gap** — a separate
  issue is being filed for it.
* **Nothing stored**, the same as request 33: `worker_action` already
  defaults to `None`; this only adds one more enum member and one more
  catalogue entry.
* **Terraform's `job_matrix`** creates a `post-verdict` Job for each tenant
  whose providers include `git-review`, running as `swarm-<tenant>-post-verdict`,
  not `worker_service_accounts[tenant]` and not the `merge` account either —
  a third, distinct identity (merge-step.md §1.3, invariant 9). Until it
  lands, the Job must not exist. **`post-verdict`'s own service account
  carries only the ordinary tenant-wide read every identity gets under
  `tenants/<t>/`** (merge-step.md §4.3, binding 1) — nothing narrower,
  nothing wider, and no `objectUser` grant for its own artifacts or
  checkpoints, since it runs no agent and writes neither.
* **Every restatement of the catalogue** — the plugin's bridge,
  `swarm_profiles`, the UI's profile list, `check-contract-parity.sh` —
  needs the new entry, the same as request 33.
* **The outcome ledger** gains two more classes, and `DERIVE_VERSION` bumps
  again, so stored days are re-derived a second time if this lands after
  request 33 already bumped it once.
* **The review POST's `{owner}/{repo}/{n}`** (`POST
  /repos/{owner}/{repo}/pulls/{n}/reviews`, merge-step.md §4.3) split two
  ways: `{owner}/{repo}` come from the tenant's control-plane record, never
  from a task document, but **`{n}`, the pull request number, comes from the
  author's own tenant-writable `result_summary.git`** (merge-step.md §4.2) —
  a value `post-verdict` does not independently verify. Misdirecting which
  pull request the anchored review lands on therefore needs only a
  Firestore write, not a GCS race; this is part of why R8 stays OPEN
  (merge-step.md §7), not something this request closes.

### If it is declined

The verdict cannot be anchored without an agent-adjacent App key, which
merge-step.md's threat model (§0, §1.3, §4.3) already shows is not safe:
either the key sits with the review agent (rejected once already, the
reason `claude-code-review`, request 36, holds no App key at all) or B1
reverts to the control-plane attestation the owner already declined. The
merge step cannot be enabled for any tenant without this.

### Invariants

- **Invariants 1–3.** `post-verdict` is an ordinary agent: admitted in the
  same transaction, parked and costing nothing until `review` succeeds, and
  counted from `LEASED`.
- **Invariant 4.** A forge failure fails the attempt retryably; the worker
  does not sleep through it.
- **Invariant 5.** The worker checks its generation at start and again
  before the post. A stale worker never touches the forge.
- **Invariant 8.** Checkpointing stays on, restore is skipped (the same
  `worker_action`-gated skip request 33 already establishes, merge-step.md
  §1.3); the workspace is empty either way.
- **Invariant 9.** The review credential's only accessor is the tenant's own
  post-verdict service account, and no agent runs as that account —
  **true only once the three companion changes in MAJOR 1 above (the API
  credential routes and `register-tenant.sh` refusing to bind `git-review`
  to the worker account, `secret_manager`'s per-provider accessor override,
  and `git-review`'s exclusion from refresh) are in place.** The catalogue
  entry by itself does not establish this invariant; today, unamended, it
  would also grant the tenant's ordinary worker account the same accessor
  role.
- **Invariant 10.** A caller names `post-verdict` and sends `input: {}`.

---

## 36. `profiles.py`: the `claude-code-review` profile, and a typed `never_restore_checkpoint`

**Superseded 2026-10-06 (lane MS0, part of #352) for the profile, not the field:** the review-only-writable prefix the `claude-code-review` profile served went with the review App ([2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs), §3), so the entry stays disabled and retiring it is that revision's request (B). `never_restore_checkpoint` stays: the lifecycle reads it.

**Status: ACCEPTED — accepted by the owner 2026-10-01, as written; APPLIED 2026-10-01**
(functionality wave 3, lane M1): `RunnerProfile.never_restore_checkpoint`
(default `False`) and the `claude-code-review` entry, `available=False` until
#342 is enforced and the review App exists. The lifecycle does not read the
field yet (merge-step.md §10 item 4a, Track B), which is safe only because
nothing can dispatch the profile. Part of #295. Recorded from
[merge-step.md](merge-step.md)'s design (contract request 33, ACCEPTED by
the owner 2026-09-29 as the design for #295), which pointed at this as a
separate, not-yet-filed request ([merge-step.md](merge-step.md)'s own §10
build item 3, §1.3 and §4.3 — not contract request 33's; §10, §1.3 and §4.3
are merge-step.md's own sections). The design is merge-step.md §1.3 and
§4.3. Nothing under `apps/common/swarm_common/` has been edited.

### What is true today

The `review` step of merge-step.md's chain needs an identity distinct from
the tenant's ordinary worker account, `swarm-agent-worker-<tenant>`, for one
narrow reason: it is the only identity allowed to write `review.json` to the
review-only-writable GCS prefix (`tenants/<t>/verdicts/`, merge-step.md
§4.3), which the ordinary tenant account's own IAM binding excludes. Unlike
`post-verdict` and `merge`, `review` still runs an agent — the same
`claude-code` runner, on the same image, needing the same Anthropic
credential — so it cannot be a `worker_action` profile; it needs its own
ordinary, agent-running profile with its own Job, so Terraform can bind
`swarm-<tenant>-review` to that Job specifically and not to every
`claude-code` step of the tenant.

Separately: merge-step.md §1.3 and §10 (build item 4a) require that
`claude-code-review`, `post-verdict` and `merge` never restore a checkpoint,
on any attempt, not only the first. `post-verdict` and `merge` get this for
free from the existing `worker_action`-gated skip (request 33). A profile
that runs an agent, like `claude-code-review`, is not covered by that
mechanism, and the repository's own house style is against keying behaviour
off `profile.name` in the lifecycle (request 18's history: `command` keyed
off a name once and produced incident wf_ebb3ab2d65664707a559). A typed
field is needed for this profile alone.

### The requested change

In `profiles.py`, add one field to `RunnerProfile`:

```python
@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: When True, the lifecycle never restores a checkpoint for this
    #: profile, on any attempt, not only the first. False (the default)
    #: preserves today's behaviour for every existing profile. `review`
    #: must never resume an agent inside a workspace a previous attempt
    #: left behind -- its whole judgement depends on seeing the checked-out
    #: head honestly (merge-step.md's own threat model, S0).
    never_restore_checkpoint: bool = False
```

**The name was chosen by the owner on 2026-09-30** over the originally
proposed `restore_on_retry=False`, which read as "restore normally on attempt
1, skip only on retries". The field means never restore, on any attempt, for
this profile. Attempt 1 has nothing to restore regardless, so its effect is
only ever visible from attempt 2, but the name does not limit it to retries.
The unusual value, `True`, is the one a reader sees set.

**A retry that finds `review.json` already written at its own path must
refuse the attempt, never adopt the existing object as this attempt's own
output.** Because `never_restore_checkpoint=True` skips workspace restore but says
nothing about the review step's GCS write, and because the platform's own
retry of a failed attempt reuses the *same* path — `f(workflow_id, review
task id)`, stable across attempts of one agent (merge-step.md §7's R8
discussion) — a naive worker that treats "object already exists" as "already
done" would adopt whatever is sitting at that path, honest or planted, rather
than treating a pre-existing object as the refusal it is. The `review`
worker's write must be create-only (`objectCreator`, no update, no delete;
merge-step.md §4.3) and a create failure on an existing object must end the
attempt in refusal, not success — this is a requirement on the worker
alongside `never_restore_checkpoint`, not a consequence the field produces by itself,
and does not by itself close R8 (merge-step.md §7), which stays OPEN.

A catalogue entry, identical to `claude-code`'s except its name (and so its
Job and service account) and `never_restore_checkpoint`:

```python
"claude-code-review": RunnerProfile(
    name="claude-code-review",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
    provider="anthropic",
    secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    secrets_any_of=True,
    timeout_seconds=7200,
    inputs=_CLI_AGENT_INPUTS,
    never_restore_checkpoint=True,
),
```

### What it would break if accepted

* **Nothing stored.** `never_restore_checkpoint` defaults to `False`, so every
  existing profile keeps today's restore behaviour unchanged.
* **The lifecycle's restore path** (`agent_worker.lifecycle`, wherever it
  currently restores unconditionally on a retried attempt) must read
  `profile.never_restore_checkpoint` before restoring, the same way it already
  branches on `worker_action` before building an argv (request 33).
* **Terraform** needs a per-tenant `review` service account and Job, holding
  `roles/storage.objectCreator` on `tenants/<t>/verdicts/` alone (no
  delete), plus the ordinary `objectViewer`/excluding-`objectUser` pair for
  its own artifacts and checkpoints (merge-step.md §4.3, §10). Until it
  lands, `claude-code-review` must not be dispatchable: an agent running
  under the tenant's ordinary account, with no distinct identity, gives up
  exactly the isolation this profile exists for. **How this is enforced,
  stated explicitly (minor, review 2026-09-29 — this was previously left
  unstated):** the same kind of provider gate contract request 35 uses for
  `post-verdict` — `claude-code-review`'s admission additionally requires
  the tenant to have registered `git-review` (the same registration
  `post-verdict` requires), parking a tenant that has not with
  `CREDENTIAL_MISSING` at no cost, even though the credential it actually
  mounts is `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, not the App key.
  Since `RunnerProfile.provider` is already `"anthropic"` for this entry,
  this needs either a second, unmounted-provider field (e.g.
  `requires_providers`) or an equivalent registration check keyed off the
  tenant's Terraform-rendered review Job existing — a small model extension
  this request does not yet specify further than naming the shape.
* **Submission must refuse dispatching `claude-code-review` or
  `post-verdict` outside a workflow step, or with a null `workflow_id`; for
  `post-verdict` specifically, also without a `verdict_source`** (merge-step.md
  §3's submission refusals) — neither profile is meant to run as a
  caller-dispatched standalone agent, and both derive part of their identity
  (which workflow's verdict they anchor, or which review they post) from the
  workflow context alone.
* **Every restatement of the catalogue** — the plugin's bridge,
  `swarm_profiles`, the UI's profile list, `check-contract-parity.sh` —
  needs the new entry and the new field.

### If it is declined

`review` runs on the plain `claude-code` profile, sharing its Job and
service account with `implement`, `fix` and `proof`. Since that shared
account would then need write access to the verdicts prefix for `review` to
do its job, every other step on that profile — and every other tenant
workflow's `claude-code` steps — inherits the same write grant, reopening
the in-chain forgery T3a exists to close (merge-step.md §7). The merge step
cannot be enabled for any tenant without this either.

### Invariants

- **Invariants 1–3, 5, 10.** Unchanged: `claude-code-review` is an ordinary
  agent-running agent like `claude-code`, admitted, fenced and named the same
  way.
- **Invariant 8.** Checkpointing stays on; `never_restore_checkpoint=True` is the
  point of this request, not a violation of it — checkpointing and
  restoring are separate steps, and mandatory periodic checkpointing is
  unaffected. What is skipped is resuming a workspace a previous attempt
  left behind.
- **Invariant 9.** `swarm-<tenant>-review`'s only elevated grant relative to
  the ordinary tenant account is the verdicts-prefix write. **MAJOR 2
  (security review 2026-09-29, NOT_YET): this is not "no portable secret an
  agent could exfiltrate and reuse elsewhere," and that claim is withdrawn.**
  A prompt-injected review agent (T11) can mint `swarm-<tenant>-review`'s own
  token from the metadata server and use it for as long as that token is
  valid — long enough, with `objectCreator` on `tenants/<t>/verdicts/`, to
  write a fabricated `review.json` at another workflow's path in the same
  tenant's verdicts subtree, ahead of that workflow's own review, before its
  own `post-verdict` reads it. This is exactly the residual merge-step.md
  records as **R8** (merge-step.md §7, T3b), and the owner has accepted it
  as **OPEN**, not closed by this request. This request narrows what the
  grant can do (create-only, no update, no delete, scoped to one prefix); it
  does not make the grant non-portable, and does not claim to.


---

## 37. `config.py` / `admission.py`: the lease's dispatch deadline is 300 s, shorter than a slow cold start plus the worker's startup read

**Status: ACCEPTED, accepted by the owner 2026-09-30, applied by this PR
(#404).** The owner decided the value (480 s) and that it goes through this
file, on #401, after #402 lengthened the worker's startup read. Numbered 37
because 35 and 36 are taken on an open branch
(`docs/cr35-cr36-merge-step-followons`); if another branch has taken 37 by the
time this merges, renumber this one.

### What is true today

The lease's dispatch deadline is 300 s, stated three times inside the frozen
contract:

* `swarm_common/config.py`, `Settings.dispatch_timeout_seconds: int = 300`;
* `swarm_common/config.py`, `Settings.from_env`, the fallback of
  `DISPATCH_TIMEOUT_SECONDS`, `300`;
* `swarm_common/admission.py`, `AdmissionConfig.dispatch_timeout_seconds: int = 300`.

Admission writes `lease.dispatch_deadline = now + dispatch_timeout_seconds`
(`admission.py`, `acquire_lease_in_transaction`). Where it is read:

* **the scheduler** passes its setting into `AdmissionConfig`
  (`apps/scheduler/scheduler/loop.py`), and into
  `backend_deadline_seconds` for every Cloud Run and GKE dispatch
  (`apps/scheduler/scheduler/dispatch.py`): the backend's hard deadline is the
  agent timeout + the dispatch timeout + `WORKER_FINALISE_BUDGET_SECONDS` (300);
* **the reconciler** judges a lease that has never heartbeated by
  `dispatch_deadline` alone (`detect_stale_leases`, `reconciler/detect.py`),
  and `MISSING_EXECUTION_GRACE_SECONDS` defaults to it
  (`reconciler/config.py`);
* **`kubernetes/render.py`** reads the dataclass default for the GKE Job
  templates' `activeDeadlineSeconds`, and takes `--dispatch-timeout-seconds`
  where a deployment overrides it.

No Terraform module sets `DISPATCH_TIMEOUT_SECONDS`; every deployed service
runs on the default. (`grep -rn "dispatch_timeout_seconds\|DISPATCH_TIMEOUT_SECONDS"`,
2026-09-30, over the whole tree.)

### The requested change

`dispatch_timeout_seconds` is 480 s in all three places. Nothing else in the
contract changes: not the field, not the env var, not the lease document.

Why 480. #401 (owner decision 2026-09-30) asked the worker's generation check
to keep asking for about 180 s, because fresh Cloud Run instances were measured
getting no reply from Google APIs for 30 to 90 s after starting, and two
attempts were lost to it under a ~90 s schedule (2026-09-25
`swarm-job-eng-mock-9ngvq`, 2026-09-26 `r7ff9`). #402 built it: the read now
spans ~180 s and its worst case, every try hanging its whole call timeout, is
200 s (`agent_worker.startup.CONTROL_PLANE_READ_WINDOW_SECONDS`). The deadline
counts from dispatch, and the cold starts measured on 2026-09-25 were 103 s and
195 s from dispatch to the worker's first line
(`docs/incidents/2026-09-25-worker-startup-network.md`). At 300 s, a worker
that cold-started in 195 s and met an API that did not answer was fenced by the
reconciler before its own last attempt ended: the retry the owner asked for was
cut off by the platform's own deadline, and the attempt was lost exactly as it
was before #401. 195 + 200 = 395 s; with 60 s of margin for the first heartbeat
after the verdict, and because 195 s is the slower of two samples rather than a
bound, 455 s; 480 is the round figure above it.

`tests/unit/worker/test_control_plane_read_retries.py`
(`test_the_dispatch_deadline_outlasts_the_slowest_cold_start_plus_the_worst_read`)
holds the three numbers to each other, imports the worker's window rather than
restating it, and holds all three frozen statements of the default equal.

### What it would break if accepted

* **A genuinely lost dispatch is detected 3 minutes later.** A Job that was
  never created, an image that never pulled, a worker that died before its first
  heartbeat and whose execution the ended-at-startup rule cannot see: the
  reconciler reclaims each at the deadline, now 480 s instead of 300 s. For
  those 180 s the lease is held, and with it the concurrency slot and the
  capacity units across every pool. **Invariants 1 and 3 still hold** (a
  `LEASED`/`DISPATCHED` agent is demand and counts toward concurrency, as it
  must); what changes is how long a dead one is counted. At the 100-agent
  ceiling, a burst of lost dispatches holds its slots up to 8 minutes rather
  than 5. The ended-at-startup rule (#198), on a `*/1` tick, still requeues
  any attempt whose execution has visibly ended within 30 to 90 s of the end,
  so the longer wait is paid only by dispatches that left no ended execution
  behind.
* **The backend's hard deadline grows by 180 s** for every attempt
  (`backend_deadline_seconds`). A wedged lifecycle stops heartbeating and is
  reclaimed by the heartbeat rule long before either deadline, so this is the
  ceiling for a hung pod, not its expected life. The largest agent timeout the
  API accepts (86 400 s) plus 480 + 300 s stays far inside Cloud Run's agent
  timeout limit.
* **The overview's workflow-stall threshold (600 s, `apps/swarm-ui/src/checks.ts`)**
  stays above the deadline and above the p90 cold start, the two bounds its
  tests hold, but no longer above their sum (about 700 s). It was left at 600
  by this change.
* **The reconciler's `missing_execution_grace_seconds`** defaults to the same
  setting, so an execution missing from the backend listing is also given
  480 s. Its dataclass default in `reconciler/config.py`, which tests that do
  not pass the field use, moves to 480 with it.

### If it is declined

The worker's startup read stays at ~180 s (200 s worst) and a cold start
slower than about 94 s from dispatch (300 - 200 - 6) that meets an API with no
reply is fenced before its verdict. The alternative is shortening #402's
schedule back toward ~90 s, which is what lost the two attempts #401 recorded.

### Invariants

1. Demand is still only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING`; the change
   lengthens how long a dead dispatch is counted, stated above.
2. All-or-nothing reservation: unchanged.
3. Concurrency counts from `LEASED`: unchanged.
5. Fencing: a worker that starts after the deadline is still fenced; the
   deadline is later.

---

## 38. `states.py` / `admission.py`: a pool with no `hard_limit` is refused as "set to 0" (#374)

**Status: ACCEPTED 2026-10-05 by the owner (recorded on #374) and IMPLEMENTED
2026-10-05 (functionality wave 7, lane CR38).** Proposed 2026-10-01
(functionality wave 1, lane B1). What was applied is listed under "As
implemented" at the end of this entry.

### What is true today

`acquire_lease_in_transaction` reads each pool document with
`hard_limit=d.get("hard_limit", 0)` (`swarm_common/admission.py`), so a pool
document with no `hard_limit` key -- written by hand, or by a writer that set
only `enabled` -- is a ceiling of 0. `evaluate_capacity` then refuses through it
with the pool's ordinary reason (`TENANT_LIMIT`, `RESOURCE_CLASS_LIMIT`, ...)
and `limit: 0`, which says an operator set the pool to zero. Nobody did.
`BlockedReason` has no value for "this ceiling was never set".

What #374's fix does without touching the contract: both `pool_from_dict`s
return an `UnsetLimitPool` (a `SlotPool` subclass that adds no field) for such
a document; `pool_to_api` serves its `hard_limit`, `effective_limit` and
`available` as null; `/v1/capacity` analyses its profiles as `unknown`; and
the scheduler, after admission has refused, rewrites that pool's blocker to
`POOL_LIMIT_UNSET` with `limit: null` (`scheduler/loop.py`,
`_name_unset_limits`). That last step is a relabel AFTER the frozen function
has spoken: the string is not a `BlockedReason`, and it depends on the
scheduler re-reading which pools are unset.

### The requested change

* `BlockedReason.POOL_LIMIT_UNSET = "POOL_LIMIT_UNSET"`, in the needs-action
  group (`headroom.NEEDS_ACTION`).
* `evaluate_capacity` (and the transaction's pool read) treat a missing or
  null `hard_limit` as unknown: still a refusal, reported as
  `{"pool": name, "reason": POOL_LIMIT_UNSET, "limit": None, ...}`, checked
  after `MANUAL_PAUSE` (a pause refuses at any limit, so it stays the reason).
* `SlotPool.hard_limit: int | None`, `effective_limit` None when it is None.

### What it would break if accepted

* Every reader of `SlotPool.effective_limit` as an `int` must handle None
  (the API codec, headroom, the scheduler's metrics, `reconciler` pool checks).
* `scheduler/loop.py` `_name_unset_limits` and the `UnsetLimitPool` class in
  both codecs become dead and are removed in the same change.
* `tests/unit/control_plane/test_blocker_groups.py` grows the new member.

### If it is declined

The relabel stays. Its weakness: a blocker recorded by any path other than the
scheduler's drain (none today) would still say `TENANT_LIMIT` at 0.

### Invariants

2. All-or-nothing reservation: unchanged; an unset pool still refuses.
3. Concurrency counts from `LEASED`: unchanged.
7. `requests == limits`: unaffected; this is the pool ceiling, not a pod spec.

### As implemented, 2026-10-05

* `states.py`: `BlockedReason.POOL_LIMIT_UNSET`. `swarm_api/headroom.py` files
  it under `NEEDS_ACTION` (waiting never clears it; somebody sets a limit).
* `models.py`: `SlotPool.hard_limit: int | None`. None makes `effective_limit`
  and `available` None and `has_capacity` False. `SlotPool` lives in
  `models.py`, not `admission.py`/`states.py`; it is the type this request's
  third bullet names, so that is the one edit outside the two modules in the
  title.
* `admission.py`: the transaction reads `d.get("hard_limit")`, so a missing
  key and a stored null are both None; `evaluate_capacity` refuses through
  such a pool with `{"reason": "POOL_LIMIT_UNSET", "limit": None}`, after the
  `MANUAL_PAUSE` check. An explicit 0 is still a 0 with the pool's own reason.
* Removed as dead: `Scheduler._name_unset_limits` (`scheduler/loop.py`), the
  `UnsetLimitPool` class in both codecs and the scheduler codec's string
  `POOL_LIMIT_UNSET`, and `_UnsetLimitReads`/`_PoolSnapshot`
  (`scheduler/store.py`, #601), which hid a stored null from the transaction
  because the old read crashed on it. swarm-api's `hard_limit_known` stays,
  now `pool.hard_limit is not None`, for `service.py`'s `/v1/capacity` filter.
* `swarm_api/headroom.py` `_ceiling`: a None limit bounds headroom at 0, never
  "unbounded" (a paused pool with no limit reaches it).
* The jq restatement `effective_limit` in `scripts/lib/common.sh` returns null
  for an absent or null `hard_limit`, as the model returns None, and
  `scripts/lib/check-contract-parity.sh` holds it there with three
  null-`hard_limit` rows. `status.sh` prints such a pool's LIMIT and HARD as
  `unset`; `resume-swarm.sh` says `POOL_LIMIT_UNSET`, not `hard_limit 0`; the
  over-limit checks in `concurrency-test.sh` and `race-test.sh` treat a null
  limit as admitting nothing, because in jq every number is greater than null.
* Proved by `tests/unit/common/test_pool_limit_unset_contract.py` and the
  updated `tests/unit/control_plane/test_pool_limit_unset.py`.

---

## 39. `states.py` / `models.py`: `ParkReason.BUDGET_EXHAUSTED` names a park nothing writes, because there are no budgets

**Status: open (functionality wave 1, lane B0, 2026-10-01).** Recorded as a
request, per CLAUDE.md rule 1; nothing under `apps/common/swarm_common/` was
edited. Numbered 39 on the assumption that 38 (lane B1) lands first; renumber if
another branch has taken it.

### What is true today

The owner decided on 2026-10-01 that there are NO per-tenant budgets: no
`monthly_budget_usd` enforcement, built or planned, and nothing writes
`PARKED(BUDGET_EXHAUSTED)`. The frozen contract still carries the vocabulary of
the feature that was dropped:

* `ParkReason.BUDGET_EXHAUSTED` (`apps/common/swarm_common/states.py::ParkReason.BUDGET_EXHAUSTED`).
  Nothing writes it, and no scheduler sweep reads it
  (`apps/scheduler/scheduler/loop.py::Scheduler._stop_for_failed_workflow`;
  `tests/unit/control_plane/test_every_park_has_an_unparker.py`
  `test_no_sweep_reads_budget_exhausted` holds the second).
* `BlockedReason.BUDGET_LIMIT` (`apps/common/swarm_common/states.py::BlockedReason.BUDGET_LIMIT`).
  `evaluate_capacity` never produces it; `apps/swarm-api/swarm_api/headroom.py::NEEDS_ACTION`
  lists it in the needs-action group with the advice "raise the budget", which
  there is no way to do.
* `Tenant.monthly_budget_usd` (`apps/common/swarm_common/models.py::Tenant.monthly_budget_usd`). The
  admin route refuses it with a 422
  (`apps/swarm-api/swarm_api/routes/admin.py::set_tenant_limits`), so no write path sets it;
  the codecs still read it from a document if a hand-edit put it there.

Per-attempt cost IS recorded, which is what made the old refusal message
("no cost attribution source") stale: `record_spend`
(`apps/agent-worker/agent_worker/control.py::ControlPlane.record_spend`) writes `cost_usd` onto the
attempt (`apps/common/swarm_common/models.py::Attempt.cost_usd`). The decision is not "budgets
once attribution exists"; it is "no budgets".

Readers outside the contract that name the unused value and would go with it:
`apps/swarm-mcp/swarm_mcp/compact.py::_WAITS_FOR`, `apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`,
and the UI's park-reason lists in `apps/swarm-ui/src/types.ts` (`:1822`,
`:2088`, `:2109`, `:2134`, `:2196`).

### The requested change

Either of these, at the owner's choice:

* **Remove** `ParkReason.BUDGET_EXHAUSTED`, `BlockedReason.BUDGET_LIMIT` and
  `Tenant.monthly_budget_usd`, with the readers above in the same change; or
* **Keep and document** them: a comment on each in the frozen modules saying the
  value is reserved and never written (owner, 2026-10-01), so a reader of the
  enum does not go looking for the code that sets it.

### What it would break if accepted

* Removal: a Firestore task document that somehow carries
  `park_reason: "BUDGET_EXHAUSTED"` would no longer decode into `ParkReason`.
  None should exist, since nothing has ever written it; a read-only count over
  `tasks` where `park_reason == "BUDGET_EXHAUSTED"` should confirm 0 before
  merging. A tenant document with a hand-written `monthly_budget_usd` would
  decode only if the codecs drop the key.
* Removal: the API's `TenantLimitsRequest.monthly_budget_usd`
  (`apps/swarm-api/swarm_api/schemas.py::TenantLimitsRequest.monthly_budget_usd`) exists only so the refusal can
  explain itself. It would stay, as a refusal of a field the contract no longer
  has, or go, and the refusal would become a plain `extra_forbidden`.
* Removal: the UI and MCP lists lose a member; their tests that enumerate
  `ParkReason` follow.
* Keep-and-document: nothing breaks; it is comments only.

### If it is declined

The value stays, unused, and the docs say so where a reader meets it:
`docs/quota-management.md` §4 marks it never written, `docs/cost-control.md` §2
says budgets are not built and not planned, and `docs/multi-tenancy.md` says why
`monthly_budget_usd` is refused. The UI keeps a label for a state no agent can
reach, which costs nothing but a misleading line in a list.

### Invariants

1. Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create demand: unchanged;
   this park was never reached, so no agent leaves or enters one.
2. All-or-nothing reservation: unaffected. `BUDGET_LIMIT` is not a pool.
10. Callers pick a profile by name: unaffected.

---

## 40. `states.py`: an agent awaiting its children has no park reason, and `DEPENDENCY_INCOMPLETE` would be promoted at once

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `ParkReason.CHILDREN_INCOMPLETE` is in `apps/common/swarm_common/states.py`; the worker's await park writes it and the scheduler's await sweep promotes it (`apps/scheduler/scheduler/loop.py`, `_promote_child_awaits`).

### What is true today

`ParkReason` (`apps/common/swarm_common/states.py::ParkReason`) has no value for "this
task is waiting for tasks it created". The nearest, `DEPENDENCY_INCOMPLETE`
(`apps/common/swarm_common/states.py::ParkReason.DEPENDENCY_INCOMPLETE`), is the scheduler's: its sweep
promotes an agent parked on it whose `depends_on` is empty immediately, with
reason `no_dependencies` (`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`). An awaiting
parent has an empty `depends_on`.

### The requested change

```python
#: The task's agent asked to await the child tasks it submitted. Promoted by
#: the scheduler when every child is terminal; see docs/design/child-tasks.md.
CHILDREN_INCOMPLETE = "CHILDREN_INCOMPLETE"
```

### What it would break if accepted

Nothing that exists: no document carries the value. Every exhaustive reading
of `ParkReason` must learn it -- `types.ts` and parity section 5, the MCP
progress sentences (`apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`) and compact
labels, and the console's blocker copy.

### If it is declined

The await parks on `DEPENDENCY_INCOMPLETE` with a reserved metadata marker, and
the scheduler's empty-`depends_on` promotion learns to skip a marked agent.
That works, and it is the hierarchy-from-dependencies S1 forbids in reverse:
every reader of the park reason would show an awaiting parent as "a step it
depends on has not finished yet", which is false, and the marker is in a
document any tenant identity can rewrite.

---

## 41. `models.py`: a child cancelled because of its parent has no end cause

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `EndCause.CHILD_CASCADE` is in `apps/common/swarm_common/models.py`, written by the API's cascade, the scheduler's sweeps and the worker ending a flagged child; the outcome ledger counts it as its own cancel class, `child_cascade` (DERIVE_VERSION 5). So does every writer that ends a flagged child CANCELLED on its `cancel_requested` -- the worker (`agent_worker.control.cancel_end_cause`), the reconciler's repair (`reconciler.model.cancel_end_cause`), and the scheduler's admission, exhausted-retry and dispatch-failure cancels (`scheduler.children.cancel_end_cause`) -- each from `metadata.child_cascade` with the one rule, held equal by `tests/unit/worker/test_end_cause_reconciler.py` and `tests/unit/control_plane/test_child_tasks_scheduler.py`.

### What is true today

`EndCause` (`apps/common/swarm_common/models.py::EndCause`) has `CANCELLED_PARENT`,
which means an UPSTREAM workflow step was cancelled, and `CANCEL_REQUESTED`,
which the outcome ledger reads as a cancel somebody pressed. A child cancelled
because its parent was cancelled, failed, dead-lettered or out-waited its
deadline is neither.

### The requested change

```python
CHILD_CASCADE = "child_cascade"   # the parent task was cancelled or ended, or its await expired
```

Written by the API's cancel cascade and the scheduler's sweep, and by the
worker or reconciler ending a child flagged by either. The specific reason
(`parent_cancelled`, `parent_ended`, `await_expired`) is event detail, not a
frozen vocabulary -- the same split `MERGE_REFUSED` uses for its refusals.

### What it would break if accepted

Nothing that exists. `EndCause`'s docstring gains the writers above; the
ledger and `types.ts` gain one class.

### If it is declined

Cascaded children end `CANCEL_REQUESTED` with the reason in event detail, and
the ledger counts a parent's failure as a person's cancel -- the confusion
request 23 was accepted to end.

---

## 42. `specsign.py`: the signed step spec does not cover a child's parent

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `SPEC_FORMAT = 2` and `SPEC_FORMATS = (1, 2)` in `apps/common/swarm_common/specsign.py`; `canonical_step_spec` takes `spec_format`, and the worker verifies each document under the projection its own `spec_format` names. swarm-api signs an agent that names no parent at FORMAT 1 (`specsign.signing_format`), so a worker built before format 2 keeps running every non-child agent through a rollout that updates swarm-api first; only a child, which needs a worker with the child path anyway, is signed at format 2.

### What is true today

`canonical_step_spec` (`apps/common/swarm_common/specsign.py::canonical_step_spec`) covers the
fields a worker must trust, at `SPEC_FORMAT = 1`
(`apps/common/swarm_common/specsign.py::SPEC_FORMAT`). Any tenant identity can rewrite a
task document whose id it knows ([multi-tenancy.md](multi-tenancy.md)), so a
child's `parent_task_id` could be rewritten to attach it to, or detach it
from, a parent's cancel cascade and await, and nothing would notice.

### The requested change

`parent_task_id` and `parent_attempt_id` inside the canonical spec, under
`SPEC_FORMAT = 2`. Format 1 documents keep verifying under format 1's
projection, which the worker already chooses by `spec_format`.

### What it would break if accepted

The worker must accept both formats during the rollout, as it accepted
unsigned agents during request 34's. No agent written before has the fields, so
none changes its digest.

### If it is declined

The cascade and the sweeps stay tenant-scoped, so a rewrite cannot reach
across tenants; within one tenant a rewritten parent is undetectable, and the
tree the console draws is a claim rather than a fact.

---

## 43. `identity.py`: the tenant worker service account's name has no public home

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `worker_service_account_id` is in `apps/common/swarm_common/identity.py`; `scripts/lib/check-contract-parity.sh` section 8 holds it to the prefix every restatement is checked against.

### What is true today

The worker service account is `swarm-agent-worker-<tenant>`, stated privately
as `_GSA_PREFIX` (`apps/common/swarm_common/identity.py::_GSA_PREFIX`) and restated by
`scripts/register-tenant.sh` (`GSA_PREFIX="swarm-agent-worker-"`) and
`terraform/modules/tenancy/main.tf` (`sa_account_id = { for t, _ in var.tenants`).
The children route must derive the identity it accepts from the tenant id,
because `tenants/<id>.service_account` is a document any tenant identity can
rewrite.

### The requested change

```python
def worker_service_account_id(tenant_id: str) -> str:
    """`swarm-agent-worker-<tenant_id>`: the account id the tenant's workers run as."""
```

### What it would break if accepted

Nothing; the private constant stays. `check-contract-parity.sh` gains a check
that the shell and Terraform restatements still equal it.

### If it is declined

swarm-api restates the prefix, held equal to `_GSA_PREFIX` by a parity test --
one more copy of a name request 16 already counts two of.

---

## 44. `states.py`: `account_assigned` and `account_released` ride on `RUNNING` and `LEASE_RELEASED`

**Status: open (functionality wave 1, lane B4, 2026-10-01).** Recorded as a
request, per CLAUDE.md rule 1; nothing under `apps/common/swarm_common/` was
edited. Numbered 40 on the assumption that 38 and 39 land first; renumber if
another branch has taken it.

### What is true today

The worker records a subscription account's lifecycle on the agent's events
with a `cause` on an event type the frozen `EventType`
(`apps/common/swarm_common/states.py::EventType`) already has:

* `account_assigned` is `EventType.RUNNING` with `cause: "account_assigned"`
  (`Worker` in `apps/agent-worker/agent_worker/lifecycle.py`).
* `account_released` (#380) is `EventType.LEASE_RELEASED` with
  `cause: "account_released"` (`Worker._emit_account_released`).

The API reads both through one range query on the `cause`
(`swarm_api.task_accounts`), so the account view is right. A reader that
renders events by type alone is not: a timeline draws the account's release as
a lease release, and the assignment as a second `running`.

### The requested change

Add `EventType.ACCOUNT_ASSIGNED = "account_assigned"` and
`EventType.ACCOUNT_RELEASED = "account_released"`, and have the worker emit
them, keeping the `cause` for one release so readers of either shape work.

### What it would break if accepted

* Every reader that enumerates `EventType` (the UI's event lists in
  `apps/swarm-ui/src/types.ts`, the MCP renderers) gains two members; their
  tests that enumerate the enum follow.
* `swarm_api.task_accounts` would read both shapes for as long as events
  written before the change are retained.

### If it is declined

The `cause` stays the discriminator, and the docstring on
`_emit_account_released` says why the type is `LEASE_RELEASED`. A generic
timeline keeps drawing the release as a lease release.

### Invariants

5. Fencing: unaffected. `control.emit` stamps the attempt, lease and
   generation either way, and a fenced exit emits neither event.
9. Tenant isolation: unaffected; the event carries the account id, the
   provider and a bool, never a secret's name or payload.

---

## 45. `profiles.py`: the catalogue does not say which runner profiles report a cost

**Status:** open, filed 2026-10-02 with #72. A request, not a change.

### What is true today

`GET /v1/attempts` `coverage` (#72) splits rows without a `cost_usd` into
"a profile that never reports a cost" and "spent and recorded nothing". The
frozen `RunnerProfile` (`apps/common/swarm_common/profiles.py`) does not say
which profiles report one, so swarm-api restates it as
`COST_REPORTING_RUNNERS` (`apps/swarm-api/swarm_api/routes/attempts.py`),
matched against each profile's `runner_argv`, and reads `cost_declared` as
"reports only when the input asks" because the mock is the only declared
profile. A unit test holds the set to modules under
`apps/agent-worker/agent_worker/runners` and pins the mock as the only
declared profile.

### The requested change

```python
    #: Whether an attempt on this profile records `cost_usd`: "always" for the
    #: CLI runners that parse a usage block, "on_input" for a runner that
    #: reports one only when its input carries it (the mock), "never" otherwise.
    reports_cost: Literal["always", "on_input", "never"] = "never"
```

### What it would break if accepted

Nothing that exists. swarm-api drops `COST_REPORTING_RUNNERS` and the
`cost_declared` stand-in and reads the field; the UI's `types.ts` gains it if
the profiles route serves it.

### If it is declined

The restatement stays, held by its test; a new cost-reporting runner module
counts as "never reports" until someone adds it to the set.

---

## 46. `models.py`: a pull-request step that published nothing has no end cause

**Status:** open, filed 2026-10-02 by functionality wave 3, lane B46. A
request, not a change.

### What is true today

Since lane B46 (GUARD 2, owner decision 2026-10-02), the worker ends a step
FAILED, and does not retry it, when the step's job was to open a pull
request and it did not. That covers a `direct-pr` step, the `integrate`
integrator and a `single-pr` author. Either the step ended with no commit
beyond its base, or the forge refused its pull request ("No commits between
main and swarm/..."). `last_error` begins `published_nothing:`
(`agent_worker.lifecycle._published_nothing`). No `EndCause` names this
case, so `_fail_for_published_nothing` writes `OUTPUTS_MISSING`, the closest
existing value: the step's promised deliverable, its pull request, does not
exist. `PUBLISH_REFUSED` (request 29) does not fit, because the worker did
not refuse anything. The outcome ledger therefore counts a lost
implementation (workflow `wf_b9b337e107494c10a416`) together with an
expected file nobody wrote.

### The requested change

```python
    PUBLISHED_NOTHING = "published_nothing"   # a pull-request step ended with nothing beyond its base, or the forge refused its pull request
```

The worker writes it from `_fail_for_published_nothing`. `swarm_api/outcomes.py`
gains the failure class `published_nothing` ("published nothing"), the UI's
`FailureClassKey`, fixture and `END_CAUSES` follow it, and
`DERIVE_VERSION` moves.

### What it would break if accepted

Nothing that exists: a new enum value. A reader that does not know it falls
back to its text classifier, as for any agent written before `end_cause`
existed. The parity script's section 5 and
`test_every_end_cause_has_exactly_one_class` turn red until every mirror
follows, which is what they are for.

### If it is declined

These steps stay `outputs_missing`, and `last_error`'s `published_nothing:`
prefix is the only way to count them apart.

### Invariants

- **Secrets.** The refusal is quoted after the worker's scrub; the cause
  names the case, never a value.
- **Invariant 1.** A FAILED step holds no capacity; the lease is released on
  the same finish as any other failure.

---

## 47. `profiles.py`: the `merge` profile is enabled, and reads the tenant's `-git` token

**Status:** ACCEPTED by the owner on 2026-10-04 and applied by functionality
wave 4, lane M1a. The decisions are recorded on #295; the risk acceptance on
#476.

### What was true before

Contract request 33 put `merge` in the catalogue with `provider="git-merge"`:
a GitHub App key, `swarm-tenant-<tenant>-git-merge`, read only by the Job's
own service account `swarm-<tenant>-merge`, so that no container an agent
had run in could ever hold a credential able to land code. The profile was
`available=False` until #342 (signed step specs) was enforced and the owner
had created the merge App. Meanwhile pull requests were merged on the GitHub
side, by `.github/workflows/auto-merge.yml` and the `swarmcloud-merge` App.
That App merged #564 and #562 and left #72 and #560 open, although GitHub
listed both as their closing references (#569).

### The change

```python
    "merge": RunnerProfile(
        ...
        worker_action=WorkerAction.MERGE,
        provider="git",
        secrets=(),
        timeout_seconds=600,
        inputs={},
    ),
```

`available` returns to its default, True; `disabled_reason` is dropped; the
provider is `git`, the tenant's existing forge token. Nothing else in
`swarm_common` changed: `post-verdict` and `claude-code-review` stay disabled
with their reason, and `WorkerAction`, the end causes and every other field
are as request 33 left them.

### Why (the owner's decisions, 2026-10-04)

1. Merging moves off the GitHub-side App into a workflow `merge` step.
2. The step uses the tenant's EXISTING `-git` token. The owner accepted, on
   #476, that an agent holding `-git` can also merge: the token that pushes a
   branch can already do everything the merge App was meant to keep from it,
   so a second identity bought no isolation worth its cost.
3. It works in any repository a workflow runs on, not one fixed by Terraform.
4. It explicitly closes the issues the pull request closes, because the App
   path did not (#569).
5. It is an opt-in final step: a platform default (`merge_by_default`,
   default off) or per job (`metadata.merge`: `on` | `off`).

#342 is closed, so the gate request 33 named is lifted. The rest of
docs/merge-step.md stands except where its "Revised 2026-10-04 (owner)"
section says otherwise.

### What it breaks

* `validation.APP_CREDENTIAL_PROVIDERS` no longer derives `git-merge`.
  `git` stays out of `known_providers()` for a different reason: the forge
  token is registered only by `scripts/register-tenant.sh --add-provider git`
  and stored only with `scripts/create-secrets.sh --stdin` (owner rule,
  2026-09-25), never through the credential routes.
* Terraform's catalogue mirror (`terraform/infra/locals.tf`) follows the
  provider, and the merge Job runs as the tenant's worker account: it is
  removed from the profiles that run as their own account. The
  `swarm-<tenant>-merge` account definition in
  `terraform/modules/service_account_ids` is no longer read for the merge
  Job's identity; removing it is a follow-up outside this lane.
* The App-shaped merge action (`merge.run_merge` over a `dispatch.merges`
  block) is replaced by the `-git` merge over a signed `dispatch.merge_target`
  block. A `single-pr` chain still cannot be submitted: it needs
  `post-verdict`, which stays disabled.

### Invariants

- **Secrets.** The token is read at merge time only, after the reap and every
  check that needs no credential (#219), registered with the log redaction,
  and never written to the workspace, a file, an agent environment, an event,
  a log line or `result_summary`.
- **Invariant 1.** The merge step holds capacity only while LEASED through
  RUNNING; a merge waiting on pending checks fails its attempt retryably and
  waits READY with `next_eligible_at`, at no cost (invariant 4).
- **Invariant 9.** The token is the tenant's own; the merge acts only on the
  workflow's own `repository_url`, which the signed spec covers.
- **Invariant 10.** A caller chooses the step by naming the `merge` profile or
  by `metadata.merge`; nothing a caller sends picks an image, a command or a
  credential.

---

## 48. `profiles.py`: no runner profile runs `agent-runtime-indexer`, so index runs cannot reach the repo-index toolchain

**Status:** accepted by the owner 2026-10-05 (#625), and applied by
functionality wave 8, lane IDX. The owner's decision changed the requested
entry in two ways: the profile is named `indexer`, not `repo-indexer`, and it
is `claude-code` in EVERY field but its name and its image -- so it takes
claude-code's inputs (`issue`), not `inputs={}`. Its pool ceiling is the
`runner_profiles` lookup's default, the global ceiling, as for merge,
post-verdict and claude-code-review; a tfvars entry narrows it. Filed
2026-10-05 with #625 (functionality wave 8, lane IMG); it is the image half
of docs/repo-index.md §6.3 request (B), in the smallest form that restores
what index runs had.

### What was true before

#625 moved the repository index's toolchain out of `agent-runtime-base` into
its own image, `agent-runtime-indexer` (docs/worker-images.md). Every
claude-code start pulled about 133 MB of it, compressed, and only index runs
used it. The image is built, promoted and scanned with the others.

No profile names it. `RunnerProfile.image` in `RUNNER_PROFILES` is the only
map from a profile to its image. `terraform/infra/locals.tf` mirrors it, and
`tests/terraform/catalogue.tftest.hcl` holds the mirror equal. Index runs are
submitted as `claude-code` (`swarm_api.repoindex.INDEXER_PROFILE`), which runs
`agent-runtime-base`. An index run therefore records "not installed in this
image", writes no graph, and an impact query on that index plans every changed
file as `unindexed`.

### The requested change

A profile identical to `claude-code` in everything but its name, its image and
its inputs, so the existing indexer prompt runs on the image that carries the
tools it names:

```python
    #: Index runs only (#625, docs/worker-images.md): claude-code's runner on
    #: the image that carries the repository index's toolchain. Submitted by
    #: swarm-api's index runs, never chosen with a caller's image (invariant 10).
    "repo-indexer": RunnerProfile(
        name="repo-indexer",
        image="agent-runtime-indexer",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs={},
    ),
```

`inputs={}`: an index run's input is its prompt alone, which swarm-api
composes. 7200 s is the largest row of §3.5's budget table (120 minutes). The
agent-free shape that §6.3 (B) describes for P1 and P3, with the tool pass as a
worker-run step, remains a later change and does not depend on this one.

### What it would break if accepted

Nothing that exists. The work that follows acceptance:

* the Terraform mirror gains the entry. `runner_images` then includes
  `agent-runtime-indexer`, `WORKER_IMAGE_REFS` carries its digest, and each
  tenant that registers `anthropic` gets a `repo-indexer` Job running as its
  worker account;
* the entry needs a pool ceiling;
* `INDEXER_PROFILE` becomes `"repo-indexer"`;
* the tests that enumerate `RUNNER_PROFILES` gain the entry.

As applied (lane IDX): `RUNNER_PROFILES["indexer"]`, the `indexer` entry and
its model in `terraform/infra/locals.tf`, `INDEXER_PROFILE = "indexer"`, its
agent-stream rows in the worker and swarm-api, and the UI's fixture and
Submit rows. The catalogue has no internal-only mechanism, so `indexer` is
offered to every caller like `claude-code`; keeping it platform-only would
need a catalogue field, which is a request of its own.

### If it is declined

There are two choices, both measured in docs/worker-images.md:

* Index runs stay on `claude-code` without the extractor or a graph.
* The toolchain goes back into `agent-runtime-base`, and every agent start
  pays for it again.

### Invariants

- **Invariant 10.** swarm-api names the profile for its own index runs. No
  caller sends an image, and the image is named by the catalogue.
- **Invariant 7.** `standard` is `requests == limits`, as for `claude-code`.
- **Invariant 9.** The Job runs as the tenant's own worker account and writes
  the graph under the tenant's own prefix, as index runs do today.
- **Invariants 1-3.** It is an ordinary profile: QUEUED until admitted,
  counted from LEASED.

---

## 49. `states.py`: a merge step waiting for its pull request's checks has no park reason

**Status:** accepted by the owner 2026-10-06 (#352), APPLIED 2026-10-06 by lane MS2. Filed
2026-10-06 by functionality wave 11, lane MS1, as request (A) of
[docs/merge-step.md's 2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs)
("Owner decisions on this plan", decision 1). Lane MS2 applies it, with its
mirrors, and adds the line the frozen-contract guard reads to its own pull
request.

### What is true today

The built merge step waits for CI by failing its attempt retryably: a
required check still queued or in progress ends the attempt, and the step
waits READY for `CHECKS_PENDING_RETRY_SECONDS` before it is admitted again,
at most `MERGE_STEP_MAX_ATTEMPTS` times
(`apps/agent-worker/agent_worker/merge.py`,
`apps/swarm-api/swarm_api/validation.py`). Every wait spends an attempt and a
Job execution, so a slow CI exhausts the step, and a fast one is read only
every five minutes.

None of the existing `ParkReason` members fits a park that waits for a
forge's checks:

* `SCHEDULED_RETRY` is promoted by time alone and counts every wake as an
  attempt, which is today's behaviour;
* `DEPENDENCY_INCOMPLETE` is promoted when the workflow's upstream steps
  end, and a merge step's have already ended;
* `CHILDREN_INCOMPLETE` names child agents, and the observer tools say so
  (`apps/swarm-mcp/swarm_mcp/progress.py`, `_PARKED_BECAUSE`).

### The requested change

In `apps/common/swarm_common/states.py`, after `CHILDREN_INCOMPLETE`:

```python
    #: A merge step waits for its pull request's checks; promoted by the
    #: scheduler's CI-wait sweep on the wake marker or the fallback instant.
    #: Contract request 49 (docs/merge-step.md, 2026-10-06, request (A)).
    CI_PENDING = "CI_PENDING"
```

### What it would break if accepted

Nothing that exists: no writer emits it until MS2's worker park does. MS2
adds its mirrors with it -- the UI's park list, the MCP plugin's
`_PARKED_BECAUSE` row (MS5), and `scripts/lib/check-contract-parity.sh` --
and the scheduler's `_promote_ci_waits` sweep, which returns the park to
READY on `metadata.merge_wait.wake_requested_at` or on `next_eligible_at`.
A reader that enumerates `ParkReason` and has no row for the new member
shows the raw value until its mirror lands.

### If it is declined

MS2 parks on `SCHEDULED_RETRY` with a `blocked_by` reason of `CI_PENDING`
and no refund, and nothing frozen changes. A slow CI then still spends the
step's attempts, one per wake.

### Invariants

- **Invariants 1 and 3.** A `CI_PENDING` agent is PARKED: no lease, no pool
  count, no Job execution. Only admission takes capacity when it is woken.
- **Invariant 4.** This is the point of the request: the step parks,
  releases and exits instead of waiting.
- **Invariant 5.** The park is written in the fenced transaction every park
  uses.

---

## 50. `profiles.py` / `models.py`: retire the disabled `single-pr` catalogue entries

**Status:** open. Filed 2026-10-06 by functionality wave 11, lane MS1, as
request (B) of
[docs/merge-step.md's 2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs).
The owner decided on 2026-10-06 ("Owner decisions on this plan", decision 4)
that the unused #295 pieces are removed by a separate cleanup lane; this
request is that lane's frozen half, and records the change it makes so that
lane carries its own acceptance line.

### What is true today

Requests 35 and 36 added, for the `single-pr` chain:

* the `post-verdict` and `claude-code-review` runner profiles, both
  `available=False` since 2026-10-04;
* `WorkerAction.POST_VERDICT`;
* `EndCause.VERDICT_REFUSED` and `EndCause.VERDICT_FAILED`.

The 2026-10-04 revision moved the merge onto the tenant's `-git` token and
appended the `merge` step to `integrate` and one-step `direct-pr` workflows
(request 47), and the 2026-10-06 revision superseded the review App and the
GitHub-review verdict anchor these served (merge-step.md, 2026-10-06 §3).
Nothing submits either profile, and nothing writes either end cause.

### The requested change

Remove the two profiles from `RUNNER_PROFILES`, `WorkerAction.POST_VERDICT`
from `profiles.py`, and `VERDICT_REFUSED`/`VERDICT_FAILED` from `EndCause`,
with every mirror: the Terraform catalogue in `terraform/infra/locals.tf` and
its test, the worker's `post-verdict` action, swarm-api's `single-pr`
planning, the outcome ledger's classes and the UI's and plugin's rows.

### What it would break if accepted

A stored agent or outcome that names either end cause would no longer parse
as an `EndCause`. None should exist, since neither profile has been
submittable; the cleanup lane reads the store to confirm before it removes
them.

### If it is declined

The entries stay disabled, as they are today, and every reader keeps the
rows for values nothing writes.

### Invariants

- **Invariant 10.** Two fewer profiles a caller can name; neither was
  available.
- **Invariant 9.** The per-tenant review, post-verdict and merge service
  accounts are the Terraform half of the same cleanup (decision 4), decided
  at dev-iam, not here.

---

## 51. `models.py`: `Attempt` does not type `checkpoint_sha256`, the digest a retry binds its restore to

**Status:** ACCEPTED by the owner 2026-10-09 and APPLIED by the pull request
that adds this line. History: proposed, filed 2026-10-07 for #350, the
Firestore document-shape record of #348 (the fix for S0 #347), as a request
that edited nothing; then accepted 2026-10-09, in the operator session, whose
brief is the record. This edits `apps/common/swarm_common/models.py`, which is
frozen, and is recorded here as such.

### What was applied

* `Attempt.checkpoint_sha256: dict[str, str] = field(default_factory=dict)`,
  after `checkpoints`, with the comment the request below gives, word for word.
* `agent_worker.control.CHECKPOINT_DIGESTS_FIELD` is looked up on the
  dataclass (`Attempt.__dataclass_fields__["checkpoint_sha256"].name`), so a
  rename in the contract fails the worker's import rather than a retry's
  restore. `agent_worker.control.recorded_checkpoint_digests` reads the map
  in its typed shape, keeping only `str -> str` entries, and both the writer
  (`ControlPlane.record_checkpoint`) and the reader
  (`Worker._recorded_checkpoint`) go through it. A missing or malformed map is
  the empty map: "no digest recorded", and the retry starts clean.
* `swarm_api.codec.attempt_from_dict` reads it, keeping only `str -> str`
  entries. `attempt_to_api` does not serve it.
* `tests/unit/worker/test_checkpoint_digest_contract.py` holds the three
  together: the worker's key is the frozen field of the requested type and
  default; what `record_checkpoint` writes decodes identically through both
  readers; a missing digest is the empty map to both; the API response does
  not carry it. `tests/unit/worker/test_no_planted_restore.py` adds a retry of
  an attempt document with no map, which starts clean.
  `scripts/lib/check-contract-parity.sh` restates no attempt field, so it is
  unchanged.
* No migration, as the request says: a pre-#348 document decodes with the
  empty default.

### What is true today

Since #348, attempt documents (`attempts/<id>`) carry a field the frozen
`Attempt` does not name: `checkpoint_sha256`, a map of checkpoint id to the
SHA-256 of that checkpoint's archive. Its only name is the worker's
`agent_worker.control.CHECKPOINT_DIGESTS_FIELD`, restated as a key by the test
seeds in `tests/unit/worker/worker_seeds.py`.

* **Who writes it, and when.** Only the worker:
  `ControlPlane.record_checkpoint` adds `{checkpoint_id: archive_sha256}` in
  the same merge-set of the attempt's own document that appends the id to
  `checkpoints`, and only then runs the fenced transaction that moves
  `task.latest_checkpoint` to the new checkpoint. A pointer therefore never
  names a checkpoint whose digest is not yet recorded. A checkpoint recorded
  without a digest adds no entry.
* **Who reads it.** Only the worker of a LATER attempt of the same agent:
  `Worker._recorded_checkpoint` reads the document of the attempt that wrote
  the checkpoint `latest_checkpoint` names (`ControlPlane.fetch_attempt`,
  which refuses another tenant's document), and restores only when that
  document is this agent's, lists the checkpoint id in `checkpoints`, and
  records the digest the manifest carries. The restore then checks the
  archive's bytes against it (docs/checkpointing.md, "What is restored").
* **Who drops it.** swarm-api's `codec.attempt_from_dict` builds an `Attempt`
  field by field, so it reads the field nowhere and drops it silently -- the
  same shape as the five spend fields this decoder once dropped while the
  worker wrote them correctly (request 2).

### Why

A retry restores only a checkpoint whose archive digest its earlier attempt
recorded. The manifest that also carries the digest sits in the bucket, and
every agent of the tenant can write anywhere under `tenants/<tenant>/`
(docs/security.md, "Which checkpoint is restored is not chosen from the
bucket"). Before #348 a restore took whatever the bucket held under the agent's
prefix, and `.claude/` -- settings and hooks, which run code -- travels in
every checkpoint because HOME is `work/`. Binding the restore to a digest in
Firestore is what refuses an archive rewritten in place, even with its
manifest rewritten to match. The field is a security record of S0 weight,
and the frozen contract is where every other component learns what an
attempt document holds.

### The requested change

In `apps/common/swarm_common/models.py`, on `Attempt`, after `checkpoints`:

```python
    #: Checkpoint id -> lowercase hex SHA-256 of that checkpoint's archive.
    #: Written ONLY by the worker's `ControlPlane.record_checkpoint`, in the
    #: same merge as `checkpoints` and before `task.latest_checkpoint` moves;
    #: read ONLY by a later attempt of the same task, which restores a
    #: checkpoint only when its archive matches the digest recorded here (#347).
    #: Empty means "no digest recorded": a document written before #348, or a
    #: checkpoint recorded without one. Its retry starts from an empty
    #: workspace; an empty map is never read as "anything goes".
    checkpoint_sha256: dict[str, str] = field(default_factory=dict)
```

* **Type:** `dict[str, str]`, keyed by checkpoint id (`ckpt-` and five or
  more digits), valued by a 64-character lowercase hex digest.
* **Default:** an empty dict, through `field(default_factory=dict)` as for
  `checkpoints`, so every existing document still decodes.
* **The mirrors that follow it, in the applying change:**
  `CHECKPOINT_DIGESTS_FIELD` becomes `"checkpoint_sha256"` checked against
  the dataclass field rather than a free string (a unit test, or a section of
  `scripts/lib/check-contract-parity.sh`); `codec.attempt_from_dict` reads
  it, keeping only `str -> str` entries. `codec.attempt_to_api` does not
  serve it: no API caller needs it, and leaving it out keeps the response
  shape unchanged. Whether a later change shows it to an operator is a
  separate decision.

No writer or reader changes behaviour. The worker writes and checks exactly
what it does today.

### What it would break if accepted

Nothing that exists. A new optional field with an empty default: every
stored attempt still parses, and no reader that ignores it changes.

**Migration for existing attempt documents.** None is run, on purpose. An
attempt document written before #348 has no `checkpoint_sha256`, decodes with
the empty default, and a retry of that attempt starts from an empty workspace
once, as docs/checkpointing.md already says. A backfill must **not** compute
digests from the bucket: the bucket's current bytes are the value the binding
exists to distrust, and a backfill would bless whatever a planter had already
put there. How agents that were mid-retry at deploy are treated is the owner's
decision recorded on #348; this request takes no position on it and changes
nothing about it.

### If it is declined

Behaviour is identical: the field stays a worker-private key in
`agent_worker.control`, and the restore keeps working. What remains is that
the frozen `Attempt` under-describes a security-relevant document, that
swarm-api's decoder drops the field without anyone deciding to, and that a
future writer of attempt documents (a reconciler repair, a copy tool) learns
of the field only by reading the worker.

### Invariants

- **Invariant 1.** No state, lease or pool count changes. The field is
  written by a RUNNING attempt that already holds its lease; a QUEUED, PARKED
  or READY agent creates no demand by having it.
- **Invariant 2.** Untouched: nothing in admission's all-or-nothing
  transaction reads or writes the attempt document.
- **Invariant 3.** Untouched: concurrency counts from LEASED as before.
- **Invariant 4.** No wait is added. The digest is computed while the
  archive is written, and the restore's check is one point read at startup.
- **Invariant 5.** The digest map is written to the attempt's OWN document;
  the pointer that makes a later attempt read it moves only in the fenced
  transaction. A stale worker can write digests into its own document, but
  cannot repoint `latest_checkpoint`, so no later attempt reads them.
- **Invariants 6 and 7.** Untouched: no Spot, no resource spec.
- **Invariant 8.** Checkpointing stays mandatory and periodic at the same
  cadence; this makes a restored checkpoint trustworthy against a
  bucket-only writer. Its cost is that a retry of a pre-#348 attempt starts
  clean once.
- **Invariant 9.** `fetch_attempt` refuses another tenant's attempt
  document, and the checkpoint must lie in the agent's own prefix. Within one
  tenant this stops a bucket-only writer, not one that also writes Firestore,
  which has no document-level IAM (docs/multi-tenancy.md); that half rests on
  signed step specs (#342).
- **Invariant 10.** No caller sends it: no API route accepts the field, and
  the serialiser does not return it.

---

## 52. `states.py`: a step the control plane finishes without a worker cannot end SUCCEEDED from PARKED

**Status:** ACCEPTED by the owner 2026-10-09 and APPLIED by the pull request
that adds this line. History: proposed (owner 2026-10-07: not accepted for
now), then accepted 2026-10-09, in the operator session; the brief that
applied it is the record. This edits `apps/common/swarm_common/states.py`,
which is frozen, and is recorded here as such. Filed 2026-10-07 with #748 (a
MERGE verdict starts a container only to open the pull request).

### What was applied

Exactly the PARKED row of "The requested change" below, comment included, and
nothing else in the frozen package. The code that needed it shipped switched
off by this request and is unchanged:
`swarm_api.verdictpublish.contract_allows` and the scheduler's hold
(`scheduler.loop.Scheduler._held_for_control_publish`) both read
`swarm_common.states.can_transition(PARKED, SUCCEEDED)`, which is now true,
so a MERGE verdict's one-contributor integrator is published by swarm-api
without a worker. Proved by `tests/unit/common/test_parked_to_succeeded.py`
(the edge, every other PARKED edge unchanged, PARKED -> RUNNING still
refused) and section 8 of `tests/unit/control_plane/test_verdict_publish.py`
(the publish and the scheduler hold on the real state machine, no stand-in),
committed red before the change.

The request as it was filed follows, unchanged.

### What is true today

`_ALLOWED` lets a task reach SUCCEEDED only from RUNNING. Every success
therefore passes through a lease, a dispatch and a worker, including the one
success that needs none of them: the gated integrator of an implement ->
review -> fix workflow whose review said MERGE. That step runs no agent
(docs/workflows.md, "What a MERGE verdict publishes"), yet it holds a lease,
starts a Cloud Run execution and clones the repository only to create a
branch and open a pull request. Measured on 2026-10-06 (#748, three MERGE
workflows): 116 s from the review's end to the pull request, about 57 s of it
container start and 35 s egress wait and clone.

swarm-api can open that pull request itself in a few seconds: the
implementer's branch is already pushed and was already scanned by the
implementer's worker before its push, and the integrator, cloned from it with
`builds_on`, would push exactly its tip. What it cannot do is record the step
as ended: the step is PARKED on `DEPENDENCY_INCOMPLETE` and was never leased,
and PARKED -> SUCCEEDED is not a transition.

### The requested change

In `apps/common/swarm_common/states.py`, the PARKED row of `_ALLOWED`:

```python
    # PARKED -> SUCCEEDED: a step the control plane finishes without a worker
    # (#748, contract request 52). It held no lease, so there is nothing to
    # release; only swarm-api writes it, guarded on its own claim.
    TaskState.PARKED: frozenset(
        {TaskState.READY, TaskState.CANCELLED, TaskState.DEAD_LETTERED, TaskState.SUCCEEDED}
    ),
```

### What it would break if accepted

Nothing that exists writes the transition until this is applied. Once it is:

* a SUCCEEDED task with no attempt and no lease exists. The readers that
  count attempts (the UI's attempt list, the outcome ledger, the MCP
  observer) show none, which is true; `result_summary.published_by:
  "control_plane"` says why;
* `scripts/lib/check-contract-parity.sh` has no restatement of `_ALLOWED`
  to update (it holds the state names, not the edges);
* the reconciler repairs leases and live states; a terminal task with no
  lease is outside every repair it makes.

### If it is declined

The swarm-api half and the scheduler hold stay switched off by the check
above and cost nothing, and a MERGE workflow keeps starting a worker to open
its pull request. The other way to reach SUCCEEDED, a lease taken by swarm-api
so that the step passes through LEASED/DISPATCHED/STARTING/RUNNING, would
book capacity for work that needs none and would put a second admission
writer beside the scheduler, which invariant 2 exists to prevent.

### Invariants

- **Invariant 1.** The step goes from PARKED to SUCCEEDED and is never
  LEASED, DISPATCHED, STARTING or RUNNING, so it never creates demand. That
  is the point of the request.
- **Invariants 2 and 3.** No pool is reserved or released: nothing was
  reserved.
- **Invariant 5.** No attempt exists, so there is no generation to fence.
  swarm-api's write is guarded on its own claim on the task, taken in a
  transaction on the same PARKED state, and the scheduler does not promote a
  claimed step until the claim is older than its timeout.
- **Invariant 9.** The pull request is opened with the step's own tenant's
  `-git` token, on the step's own repository, from the branch of the
  step's own upstream task in the same tenant and workflow.

---

## 53. `profiles.py`: run the `claude-code` profile on GKE Autopilot, whose fresh-node start p90 is 120 s against Cloud Run's 212 s

**Status:** APPLIED 2026-10-08. The owner switched `claude-code` to
`Backend.GKE_AUTOPILOT` after request 55's canary passed: 5/5 `claude-code-gke`
steps ran on GKE Autopilot with DISPATCHED -> RUNNING p50 ~23 s, max 44 s
(against Cloud Run's p50 128 s / p90 212 s), on `claude-opus-5-5`, and opened
their pull requests. The same change removed the canary (request 55). How the
conditions below were met: (1) MODEL reaches the GKE pod
(`GkeJobDispatcher._manifest`, via `profile_model`) and the workspace is sized
for the disk `emptyDir`; (2) `pool_limits.backends.GKE_AUTOPILOT` is 100 in
`terraform/environments/dev/dev.tfvars`; (3) the tenants' claude-code Cloud Run
Jobs are kept, idle, until 2026-10-15 by `cloud_run_fallback_profiles` in
`terraform/infra/locals.tf` -- without it the Job loop, which makes a Job only
for a `CLOUD_RUN_JOB` profile, would have destroyed them with the switch.
Rolling back while they exist is one line: the backend back to
`CLOUD_RUN_JOB`.

Accepted by the owner 2026-10-07, with conditions:
1. Land the follow-ups first: MODEL is passed to GKE pods, and the workspace
   is sized for the 4Gi disk emptyDir.
2. Raise the `GKE_AUTOPILOT` backend ceiling to **100**. us-central1 has 2,990
   of 3,000 regional vCPUs free, so 100 x 4 vCPU fits. T2D allows only 128
   vCPUs, so no compute class may pin to T2D.
3. Keep the Cloud Run claude-code Jobs as a fallback, until a week of clean
   GKE runs.
The profiles.py switch is the last change, after those land. Filed
2026-10-07. The owner set the rule before the
probe ran (decision on #667, 2026-10-07): file this request if a fresh-node
GKE Autopilot start of the claude-code image has p90 <= 128 s AND a warm start
has p50 <= 20 s; do not if the fresh-node p50 is >= 128 s. The probe met the
first condition: fresh-node p50 89 s / p90 120 s (n=12), warm p50 1 s (n=10).
A request, not a change: nothing under `apps/common/swarm_common/` and no
tfvars is edited by it, and the owner accepts or refuses it.

### What is true today

* **The catalogue.** `apps/common/swarm_common/profiles.py`, the
  `"claude-code"` entry (line 1137): `resource_class="standard"` (4 CPU /
  8 GiB, `disk_gib=4`, `units=1`), `backend=Backend.CLOUD_RUN_JOB`,
  `timeout_seconds=7200`. The `"browser"` entry (line 1181) already names
  `Backend.GKE_AUTOPILOT`, so the GKE dispatch path is live, not new:
  `scheduler/dispatch.py` `BackendRouter.for_backend` routes it to
  `GkeJobDispatcher`, which creates a `batch/v1` Job in the tenant's
  namespace, and the reconciler's GKE backend (`reconciler/backends.py`,
  `name = "GKE_AUTOPILOT"`) watches it. 313 browser attempts went through it
  between 09-29 and 10-07.
* **Where claude-code's start goes on Cloud Run** (874 attempts, 09-30 ..
  10-07, read-only, scratchpad `cloudrun-start-measure.md`, summarised on
  #667): DISPATCHED -> STARTING p50 128 s / p90 212 s. 90-95 % of it is Cloud
  Run's own `ResourcesAvailable -> Started` (p50 138 s / p90 227 s, n=497);
  everything of ours is small and flat (dispatch call -> execution created
  -0.2 s, Started -> worker's first log 3 s, first log -> STARTING 3 s). It
  swings by day with no change on our side (10-03 p50 92 s, 10-05 163 s,
  10-07 176 s), and the 27.6 MB, 1 CPU `swarm-verify` job swings the same way,
  so neither image size nor shape is the cause. The lease is held, and its
  units counted, for all of it.
* **The same image on GKE Autopilot** (probes run 2026-10-07 on
  `swarm-autopilot`, image `agent-runtime-base@sha256:de51d9cf...`; scratchpad
  `gke-probe/summary.md`, `gke-probe/fresh/summary.md`,
  `gke-probe/cc/summary.md`). Metric: pod `creationTimestamp` -> container
  `startedAt`, 1 s resolution. Pod: 4 CPU / 8Gi with requests == limits, 4Gi
  ephemeral storage, `safe-to-evict: "false"`, no Spot, no Google identity,
  no-op command.

  | set | n | p50 s | p90 s | min | max |
  |---|---|---|---|---|---|
  | warm (a node with room already exists) | 10 | 1 | 1 | 1 | 1 |
  | existing node, not warm | 9 | 8 | 8 | 1 | 9 |
  | **fresh node, pod create -> container started** | 12 | **89** | **120** | 75 | 126 |
  | fresh node, default `ek` family only | 2 | 82 | 90 | 82 | 90 |
  | fresh node, pod create -> scheduled | 12 | 72 | 99 | 60 | 112 |
  | fresh node, TriggeredScaleUp -> node Ready | 11 | 71 | 97 | 57 | 111 |
  | fresh node, image pull | 12 | 4.8 | 9.2 | 1.6 | 9.2 |
  | *baseline:* Cloud Run claude-code DISPATCHED -> STARTING | 874 | 128 | 212 | | |
  | *baseline:* browser on GKE DISPATCHED -> STARTING | 313 | 15 | 83 | | |

  Ten of the twelve fresh samples come from probe 3, which forced a new node
  by pinning a machine family or compute class no current node carried
  (Balanced -> n2d, Scale-Out -> t2d, e2, n2, n2d, t2d, c3, c3d); two come
  from the default general-purpose class (`ek`), which is what claude-code
  would actually get.
* **What the probe saw go wrong.** Probe 3 made 13 runs. **3 never
  scheduled** (t2d on its first try, n4, n4d: "Pod didn't trigger scale-up
  ... 4 in backoff after failed scale-up"), and **3 of the 10 that ran logged
  "GCE out of resources"** scale-up failures in us-central1-f (n2d, t2d) or
  us-central1-a (n2d) before a node came up in another zone. Every one of the
  six was pinned to a single machine family. The default-class runs (n=2)
  saw neither, which is too few to call a rate.
* **The ceilings.** `terraform/environments/dev/dev.tfvars`, `pool_limits`:
  `backends = { CLOUD_RUN_JOB = 100, GKE_AUTOPILOT = 40 }`, and
  `runner_profiles.claude-code = 80`, `resource_classes.standard = 80`.
  Admission takes every pool all-or-nothing, so on GKE claude-code would be
  capped at 40 concurrent, shared with browser (2 units each), until the GKE
  ceiling moves.

### Why

The DISPATCHED -> STARTING wait is lease-held time: the agent occupies its
global, tenant, provider, profile, class and backend slots while doing
nothing, and on Cloud Run that wait is two minutes at the median and three
and a half at p90, set by a provider step we cannot influence (#363, #625,
#667). On Autopilot the same image starts in about a second when a node has
room, about 8 s when the autoscaler only has to place it, and about 90 s
(p90 120 s) when a node must be created, which is the worst case rather than
the typical one. The browser profile, on the same cluster, already reaches
STARTING in p50 15 s / p90 83 s measured from DISPATCHED. Expected saving:
roughly 100 s per attempt at the median and more at p90, on the profile that
runs nearly every SwarmCloud step.

### The requested change

In `apps/common/swarm_common/profiles.py`, the `"claude-code"` entry:

```diff
     "claude-code": RunnerProfile(
         name="claude-code",
         image="agent-runtime-base",
         resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
+        # GKE Autopilot since contract request 53: the same image starts in
+        # p50 89 s / p90 120 s on a NEW node and ~1 s on a warm one, against
+        # Cloud Run's DISPATCHED -> STARTING p50 128 s / p90 212 s, 90-95 %
+        # of it in Cloud Run's own provisioning (#363, #625, #667). Extended
+        # run time is what keeps a two-hour agent from being consolidated
+        # away, and it is on-demand only: no Spot (invariant 6).
+        backend=Backend.GKE_AUTOPILOT,
         runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
```

Nothing else in the frozen package changes. `resolve_backend` returns the
named backend unchanged; `standard` stays 4 CPU / 8 GiB.

**The non-frozen follow-ups, in the applying change** (each must land with or
before the catalogue edit, or claude-code regresses on GKE):

1. **MODEL on the GKE pod** -- `apps/scheduler/scheduler/dispatch.py`,
   `GkeJobDispatcher` manifest. A Cloud Run Job carries `MODEL` from
   `terraform/infra/locals.tf` `runner_models` (or `WORKER_MODELS` for Jobs the
   scheduler creates); `gke_worker_env` and the GKE manifest set no `MODEL`,
   so a claude-code pod on GKE today would run the CLI's default model instead
   of `claude-opus-5-5` (#226). The GKE render must add `MODEL` from the same
   `WORKER_MODELS` source, with a unit test.
2. **The Terraform mirror** -- `terraform/infra/locals.tf`
   `runner_profiles["claude-code"].backend = "GKE_AUTOPILOT"`, which
   `tests/terraform/catalogue.tftest.hcl` holds byte-identical to the Python.
   What that does to the `swarm-job-<tenant>-claude-code` Cloud Run Jobs
   (kept as an idle fallback, or no longer created for a GKE profile) is read
   from the same file's Job loop when the change is written; this request has
   not traced it.
3. **The backend ceiling** -- `terraform/environments/dev/dev.tfvars`
   `pool_limits.backends.GKE_AUTOPILOT`, today 40. Leaving it caps claude-code
   at 40 (from 80); raising it to 80 or 100 is a cost and quota decision for
   the owner, as is whether the Autopilot cluster's own limits (regional CPU
   quota, the cluster's node auto-provisioning maximum) cover 80 concurrent
   4-CPU pods. `CLOUD_RUN_JOB = 100` then only bounds mock, generic and the
   Cloud Run profiles.
4. **Node class: none pinned.** Every unscheduled and every out-of-resources
   probe run was pinned to ONE machine family; the dispatcher today sets no
   `nodeSelector`, so Autopilot's default general-purpose class picks from
   every family and zone it can, and this request asks to keep it so. If live
   telemetry later shows stockouts on the default class, the remedy is a GKE
   custom ComputeClass listing families in priority order (fallback, not a
   pin); whether the cluster's GKE version supports one on Autopilot is not
   verified here.
5. **The docs** -- `docs/execution-backends.md` says Cloud Run is preferred
   "for everything it can hold" because it has no nodes; it would name
   claude-code as the exception and why (start latency), with the reliability
   cost below kept, not softened. `docs/architecture.md` and
   `docs/cost-control.md` get the same line where they name the backend per
   profile.

### What the probe did NOT measure

* **DISPATCHED, not pod creation.** The GKE numbers start at the pod's
  `creationTimestamp`; the Cloud Run numbers start at the scheduler's
  DISPATCHED event and include the dispatch call. GKE adds the Job-create
  call, the Job controller creating the pod, the worker's first log (Cloud Run
  S_W 3 s) and its STARTING write (3 s). Browser-on-GKE's 15 s / 83 s is the
  only measured DISPATCHED figure on this cluster, on a different image and
  shape.
* **The worker.** The pod ran a no-op command: no Workload Identity token, no
  Secret Manager read, no Firestore write, no clone. Those run on both
  backends, but the GKE path's DNS and metadata route differ
  (`gke_worker_env`, #341).
* **Shape and volumes.** The probe's 4 CPU / 8Gi with requests == limits
  matches `standard`, but the real Job also mounts `/dev/shm` (2Gi, memory),
  `/tmp`, HOME and the spec-verify ConfigMap, which the probe did not.
* **Image pull at scale.** Pulls were 1.6-9.2 s for the 727 MB image on a
  fresh node; a burst of new nodes pulling at once, and the
  first pull after each release's new digest, were not measured.
* **Extended run time over a full run.** The pods carried `safe-to-evict:
  "false"` but exited at once; nothing measured a two-hour pod surviving node
  upgrades, consolidation or auto-repair.
* **Stockout rate on the default class.** 2 default-class samples, no
  failure; the six pinned failures are the evidence that a pin is harmful,
  not a rate for the unpinned path. A pod that never schedules holds its lease
  until the 480 s dispatch deadline reclaims it, as on Cloud Run; whether the
  reconciler deletes that pod's Job on reclaim was not checked by this
  request.
* **Cost.** Autopilot bills the pod's requests for its life; Cloud Run bills
  the execution. Not compared.

### What it would break if accepted

* **The reason Cloud Run was chosen.** Extended run time suppresses
  consolidation and auto-upgrade for up to seven days; it does not stop node
  auto-repair or memory/disk-pressure eviction (docs/execution-backends.md).
  claude-code gains a node-side way to lose an attempt that it does not have
  today. Mandatory periodic checkpointing makes that cost minutes rather than
  the run; it does not make it free.
* **Workspace becomes disk.** On Cloud Run the 4 GiB workspace is a tmpfs
  carved out of the 8 GiB memory limit. On GKE it is a disk `emptyDir`; the
  pod's ephemeral-storage limit is 4Gi and kubelet evicts the pod when its
  total local usage (workspace, `/tmp` and HOME together) exceeds it, while
  the agent gets the full 8 GiB of memory. A run that fits today can fail on
  disk instead, or one that ran out of memory can now fit.
* **Concurrency** halves to 40 unless the backend ceiling moves (follow-up 3).
* **The model** silently changes to the CLI default unless follow-up 1 lands
  first.
* **Running attempts.** None expected: an attempt already dispatched keeps
  running where it was created; only dispatches after the release go to GKE. A retry of an
  attempt checkpointed on Cloud Run restores on GKE from the same GCS prefix.

### If it is declined

Nothing changes. claude-code keeps a 2-3.5 minute lease-held start that moves
with Cloud Run's provisioning, and the remaining levers are the ones already
decided on #667: a post-deploy warm run per Cloud Run Job (absorbs the 30-59 s
per-digest import on ~3 % of starts) and the Cloud Run support case. Neither
touches the 138 s median in `ResourcesAvailable -> Started`.

### Invariants

- **Invariant 1.** Unchanged: only LEASED/DISPATCHED/STARTING/RUNNING create
  demand. A GKE pod is created only for a leased, admitted agent, exactly as a
  Cloud Run execution is. Pending pods are never a backlog: an unschedulable
  pod is a DISPATCHED lease bounded by the dispatch deadline, never a queue.
- **Invariant 2.** Unchanged: admission still reserves every pool, the
  `GKE_AUTOPILOT` backend pool instead of `CLOUD_RUN_JOB`, in the same single
  Firestore transaction.
- **Invariant 3.** Unchanged: counted from LEASED.
- **Invariant 4.** Unchanged: the worker lifecycle is the same image
  ENTRYPOINT on both backends, and parks and exits the same way.
- **Invariant 5.** Unchanged: the generation travels in `GENERATION` in the
  pod's env and is in the Job name; `backoffLimit: 0` means Kubernetes never
  re-runs a stale attempt.
- **Invariant 6 (Spot).** Preserved and load-bearing. The GKE manifest
  requests no Spot and sets `safe-to-evict: "false"` on the Job and the pod
  template; extended run time is only available on on-demand capacity, which is
  why "Spot preferred" is impossible here rather than merely undesirable. The
  frozen catalogue still raises at import on any Spot strategy. The probe ran
  without Spot.
- **Invariant 7 (requests == limits).** Preserved: `GkeJobDispatcher` sets the
  same `resources` dict as both `requests` and `limits` for CPU, memory and
  ephemeral storage, from `RESOURCE_CLASSES["standard"]`. The probe used the
  same equality.
- **Invariant 8.** Unchanged and more important: the node-side eviction paths
  above are exactly what periodic checkpointing exists to absorb.
- **Invariant 9.** Unchanged and already exercised by browser: the pod runs
  in the tenant's own namespace as the tenant's KSA, workload-identity-bound
  to the tenant's GSA, behind default-deny NetworkPolicy, and reads its
  provider credential from Secret Manager itself; no Kubernetes Secret holds
  it. Its GCS prefix is the same.
- **Invariant 10.** Unchanged: the backend is a catalogue field; a caller
  still names `claude-code` and cannot choose where it runs.

---

## 54. `models.py` / `specsign.py`: a task does not say which forge credential it uses, or whether it may write

**Status:** ACCEPTED by the owner 2026-10-07, APPLIED 2026-10-07 by the pull
request that adds this entry (part of #780). This is request E of
[docs/onboarding.md §3.3](onboarding.md#33-how-the-worker-gets-the-per-agent-credential)
("With request E (recommended)" and the draft after it), accepted by the
owner 2026-10-07 for the guided-onboarding work of #780; it supersedes request
(E) as [git-tokens.md §8](git-tokens.md) sketched it, by adding the access mode
and the signature. Only the frozen half is applied here: swarm-api writing the
fields at submission is lane OB7, and the worker reading them is lane OB5.

### What is true today

`Task` (`apps/common/swarm_common/models.py::Task`) says nothing about which
forge secret an agent uses. The worker reads `Tenant.secret_name("git")`
(`Worker._git_token`, through `resolve_git_token`), so registration, clone and
push all use the one tenant token `swarm-tenant-<tenant>-git`. The
repository-scoped (`git-r-<hex>`) and per-user (`git-u-<hex>`) slots of
docs/git-tokens.md exist, and swarm-api can resolve them, but no agent can name
one. `canonical_step_spec` (`apps/common/swarm_common/specsign.py`) signs
`submitted_by` and the signed metadata keys, and no credential.

### Why

#780 asks for SwarmCloud to act **as the user**, across more than one GitHub
org, on repositories the user chose to grant read or write. That needs an agent
to carry two decisions swarm-api makes at submission (onboarding.md §3.3 step
1): whose token the worker reads, and whether it may push with it. Both must
come from somewhere the worker can trust. A tenant's agents can write any task
document of their tenant (docs/multi-tenancy.md), so an unsigned field would
let one user's agent point its agent at another user's slot in the same tenant,
or lift a `read` grant to `write`. Inside the spec signature, a rewrite fails
the worker's check (`SPEC_SIGNATURE_INVALID`) before anything runs.

The alternative -- the worker derives the user's slot from the signed
`submitted_by` -- needs no frozen change, but restates the user-hash rule in
the worker and cannot say "this task uses the tenant token", so the tenant
fallback of decision D4 would be impossible.

### The requested change

In `apps/common/swarm_common/models.py`:

* `FORGE_CREDENTIAL = re.compile(r"git(-[ru]-[0-9a-f]{16})?")`, matched with
  `fullmatch`, and `FORGE_ACCESS = ("write", "read")`;
* on `Task`, after `parent_attempt_id`, two optional fields:
  `forge_credential: str | None = None` (the provider suffix the worker reads:
  `git` for the tenant token, `git-r-<16 hex>` for a repository token,
  `git-u-<16 hex>` for a user's slot; None is `git`, today's behaviour) and
  `forge_access: str | None = None` (`write` or `read`; None is `write`, for
  backward compatibility). Both are **written by swarm-api only**, at
  submission, and never accepted from a caller;
* `Task.__post_init__` refuses a value of any other shape, so a writer cannot
  construct a malformed agent. It checks only these two fields, so no document
  that decodes today is refused.

In `apps/common/swarm_common/specsign.py`, **format 3**: format 2's projection
plus `forge_credential` and `forge_access`. `SPEC_FORMAT` becomes 3 and
`SPEC_FORMATS` `(1, 2, 3)`. `signing_format` signs at the oldest format that
covers every field an agent sets: 1 with no parent and no forge field, 2 with a
parent and no forge field, 3 when either forge field is set. Formats 1 and 2
**refuse** (`SpecNotCanonical`) a document that sets either forge field. A
parent added to a format-1 document stays outside the signed bytes (#476); a
forge field must not, because the worker acts on it itself.

swarm-api's `codec.task_from_dict` reads both back, so an agent it decodes and
writes again keeps its signed fields; a stored value of the wrong shape
decodes as None rather than making the agent unreadable. Neither field is
served by the API: `test_api_contract_shapes` lists them as deliberate
omissions until OB7 decides otherwise.

### What it would break if accepted

Nothing existing. A document without the fields decodes as it does today, and
an agent that sets neither is signed at format 1 or 2 with **the same canonical
bytes as before**: `tests/unit/common/test_specsign.py` pins the SHA-256 of
both formats as computed before format 3 existed, and verifies a P-256
signature over that pinned digest against the digest the new code computes.
A worker built before format 3 knows only formats 1 and 2, and refuses a
format-3 agent as `unknown_format`. Only an agent swarm-api resolved a forge
field for is signed at format 3, and swarm-api writes none until OB7, so the
rollout order is the worker (OB5) first, then the submission (OB7).

One edge is said plainly: the worker's legacy window (`SPEC_LEGACY_UNTIL`)
admits an UNSIGNED agent created before signing shipped. Such an agent has no
signature to protect a forge field, so the worker that reads the fields (OB5)
must ignore both on an unsigned agent and read `git`/`write`, as today.

### If it is declined

The "without it" path of onboarding.md §3.3: the worker derives the user slot
from the signed `submitted_by`, reads the grant for the mode, restates the
user-hash rule beside swarm-api's under a parity test, and no agent can use the
tenant token as a fallback for a user without a grant (decision D4).

### Invariants

- **Invariants 1, 2 and 3.** Untouched: no state, lease, pool count or
  admission transaction reads or writes either field here. (The scheduler's
  `CREDENTIAL_MISSING` check reading the named slot is OB7's, and parks at no
  cost as today.)
- **Invariant 4.** No wait is added.
- **Invariant 5.** The worker verifies the signature, fenced as today, before
  it reads anything the fields name; a stale worker still exits without
  running the agent. A rewrite of either field, or adding one to an agent
  signed without it, fails verification.
- **Invariants 6, 7 and 8.** Untouched.
- **Invariant 9.** The field is a suffix, not a secret name: the worker
  places it with `Tenant.secret_name` under the agent's OWN tenant, and the
  anchored shape admits no other tenant's prefix and no `-refresh` twin, so it
  can never name another tenant's secret.
- **Invariant 10.** No caller sets either field. swarm-api writes them from
  its own resolution of the grants, as it writes `parent_task_id`.

---

## 55. `profiles.py`: a temporary `claude-code-gke` profile, the canary for request 53

**Status:** REMOVED 2026-10-08 (the switch replaced it): request 53 moved
`claude-code` itself to `Backend.GKE_AUTOPILOT` after this canary passed 5/5,
and the same change deleted the `claude-code-gke` entry and every mirror of it
(Terraform, the console, the worker's and the API's stream tables, the docs).
`tests/unit/control_plane/test_claude_code_on_gke.py` holds that it is gone.

Accepted by the owner 2026-10-07, and applied in the same change
that files it. TEMPORARY: the entry is removed in the same change that
switches `claude-code` itself to `Backend.GKE_AUTOPILOT` (request 53's last
step), or, if request 53 is withdrawn after the canary, in the change that
records that. This is the sanctioned edit to `apps/common/swarm_common/` for
it; nothing else in the frozen package changes.

### What is true today

* **Request 53 is accepted, with conditions** (above): its follow-ups land
  first, the `GKE_AUTOPILOT` ceiling rises to 100, and the Cloud Run
  claude-code Jobs stay as a fallback until a week of clean GKE runs. The
  `profiles.py` switch is the last change.
* **Nothing can run claude-code on GKE before that switch.** A caller names a
  profile, never a backend (invariant 10), and `claude-code` resolves to
  `CLOUD_RUN_JOB`. Request 53's probe measured the image on Autopilot with a
  no-op command: no Workload Identity token, no Secret Manager read, no
  Firestore write, no clone, no `/dev/shm`/`/tmp`/HOME/spec-verify mounts, no
  two-hour run (its "What the probe did NOT measure"). The only way to measure
  those on real steps, without moving every claude-code step at once, is a
  second catalogue name for the same runner on the other backend.
* **The GKE pod carried no `MODEL`** (request 53, follow-up 1):
  `GkeJobDispatcher._manifest` built its environment from `gke_worker_env`
  and `RUNNER_JOB_NAME` only, so a claude-code pod would have run the CLI's
  default model.

### Why

A canary on real steps answers what the probe could not -- the DISPATCHED ->
STARTING time including the Job controller, the worker's first log and its
STARTING write; the workspace on a 4Gi disk `emptyDir` instead of tmpfs;
extended run time over a full run -- while `claude-code` keeps running on
Cloud Run for everyone else. The week of clean GKE runs request 53's third
condition asks for needs runs to count.

### The requested change

In `apps/common/swarm_common/profiles.py`, after the `RUNNER_PROFILES` dict
literal:

```python
RUNNER_PROFILES["claude-code-gke"] = replace(
    RUNNER_PROFILES["claude-code"], name="claude-code-gke", backend=Backend.GKE_AUTOPILOT
)
```

IDENTICAL to `claude-code` in every other field -- image, `runner_argv`,
provider, secrets and `secrets_any_of`, resource class, timeout, checkpoint
interval, inputs, availability -- because it is BUILT from that entry with
`dataclasses.replace`, not restated, so the two cannot drift.
`tests/unit/control_plane/test_claude_code_gke_canary.py` asserts that only
`name` and `backend` differ.

**The non-frozen mirrors, in the same change:**

1. `terraform/infra/locals.tf`: `runner_profiles["claude-code-gke"]`
   (backend `GKE_AUTOPILOT`), so the catalogue comparison holds and its pool
   `runner:claude-code-gke` exists, with no `pool_limits.runner_profiles`
   entry: it takes the `global` ceiling, and the `GKE_AUTOPILOT` backend pool
   (shared with browser) is what binds. The Job matrix keeps only
   `backend == "CLOUD_RUN_JOB"`, so **no Cloud Run Job is created for it**;
   `catalogue.tftest.hcl` asserts that.
   `runner_models["claude-code-gke"]` names claude-code's model, which reaches
   the scheduler as `WORKER_MODELS`.
2. `tests/terraform/catalogue_mirror/main.tf`: the text parser reads this
   `replace(...)` shape as its base with the backend overridden, and cuts
   each literal entry at the end of the dict so the last one does not absorb
   the assignment.
3. `apps/scheduler/scheduler/dispatch.py`: `profile_model(settings, profile)` (already on main from request 53),
   one lookup for both backends, and `GkeJobDispatcher._manifest` sets `MODEL`
   from it -- request 53's follow-up 1, for every GKE profile with a pinned
   model (browser has none and gets none).
4. Worker and API: `agent_worker.runners.streams.cli_agent_spec` and
   `swarm_api.agent_streams.AGENT_STREAM_FILES` treat it as the claude_code
   CLI, as they do `claude-code-review` and `indexer`.
5. Console: `PLATFORM_RUNNERS` in `RunnerPicker.tsx` marks it "platform", so
   the Submit picker lists it after the runners people submit rather than
   beside `claude-code`. The console has no mechanism that hides a profile
   (that list's comment says so: a name is drawn, never hidden), and the
   frozen `RunnerProfile` has no audience field; a caller who names it can
   still submit it, which is how the canary is run.
6. `kubernetes/worker-templates/worker-job.yaml`: the reference GKE Job now
   mounts `/dev/shm` and the spec-verify ConfigMap, which the dispatcher
   already mounts on EVERY GKE pod; the template was unread while browser
   was the only GKE profile.

### What it would break if accepted

* **A second name for claude-code.** Reports, pools and the console count
  the canary apart from `claude-code`. That is the point of a canary, and it
  is temporary.
* **Shared memory on GKE.** Every GKE pod gets a 2Gi memory-backed
  `/dev/shm`. It counts against the 8 GiB limit only as far as something
  writes to it, which the claude CLI is not known to do; it is not measured.
* **The `GKE_AUTOPILOT` pool** is shared with browser until request 53's
  ceiling rise lands: a canary step can make a browser step wait.

### If it is declined

Request 53 is applied without a canary: `claude-code` moves to GKE for every
step at once, and the probe's unmeasured items are first measured in
production.

### Invariants

- **Invariant 1.** Unchanged: a GKE pod is created only for a leased,
  admitted agent; an unschedulable pod is a DISPATCHED lease bounded by the
  dispatch deadline, never a backlog.
- **Invariant 2.** Unchanged: admission reserves `runner:claude-code-gke`,
  `backend:GKE_AUTOPILOT` and the rest in the same single transaction.
- **Invariant 3.** Unchanged: counted from LEASED.
- **Invariant 4.** Unchanged: the same image ENTRYPOINT and lifecycle.
- **Invariant 5.** Unchanged: `GENERATION` in the pod's env and the Job
  name; `backoffLimit: 0`.
- **Invariant 6 (Spot).** Preserved: no Spot request, `safe-to-evict:
  "false"` on the Job and the pod, `spot` copied as `ON_DEMAND_ONLY`.
- **Invariant 7.** Preserved: `requests == limits` from
  `RESOURCE_CLASSES["standard"]`.
- **Invariant 8.** Unchanged: checkpointing is mandatory and periodic at
  claude-code's interval; the node-side eviction paths are what it absorbs.
- **Invariant 9.** Unchanged: the tenant's namespace, KSA, GSA, secrets and
  GCS prefix, as browser's pods use today.
- **Invariant 10.** Unchanged: a caller names `claude-code-gke`, and the
  catalogue says where it runs. `MODEL` comes from the scheduler's
  `WORKER_MODELS`, never from the agent (`test_a_tasks_own_model_never_reaches_the_pod`).

---

## 56. `profiles.py`: the `mock` profile cannot write its artifact before a simulated park, so #166's carry cannot be proven live

**Status:** ACCEPTED, accepted by the owner 2026-10-08, and APPLIED
2026-10-08 by the pull request that adds this entry (lane MOCK-CARRY, part of
#166). This is the sanctioned edit to `apps/common/swarm_common/` for it;
nothing else in the frozen package changes.

### What is true today

#166's carry is on main (PR 689 and #712): a parking attempt's uploads are
recorded (`ControlPlane._record_parked_uploads`), and the attempt that
finishes the agent lists them by reference, `{name, bytes, uri,
carried_from}`, read back through `ControlPlane.parked_uploads`. The only
runner that parks on demand is `mock` (`quota_exhausted`), and it raises its
rate limit BEFORE it writes its artifact
(`apps/agent-worker/agent_worker/runners/mock.py`: the `_parks_this_attempt`
check comes before `ctx.write_artifact`). So a mock park uploads no artifact,
and the attempt after the park writes its artifact itself.
`tests/unit/worker/test_parked_uploads_carry.py` proves the carry offline only
by patching the worker's upload to put a file in the artifacts folder.

### Why

#166 asks for the carry to be shown on a live run, not only offline. No live
runner can show it: a CLI agent cannot be made to meet a provider rate limit on
demand, and the mock, which can, parks with nothing written. The mock needs to
write its artifact first, park, and then NOT write it again, so the file the
agent ends with can only have arrived by the carry.

### The requested change

In `apps/common/swarm_common/profiles.py`, `_MOCK_INPUTS` gains one key, after
`retry_after_seconds`:

```python
"artifact_before_park": RunnerInput(
    "boolean",
    means="write the output artifact before the simulated rate limit parks the first attempt",
),
```

Default false (an omitted key). Nothing else in the catalogue changes.

**The non-frozen half, in the same change:**

1. `agent_worker/runners/mock.py`: with `quota_exhausted` and
   `artifact_before_park` both set, the attempt that parks writes
   `artifact_name`/`artifact_text` to the artifacts folder and then raises
   `QuotaExhaustedSignal`; the attempt after the park writes nothing under
   that name. Without `quota_exhausted` the flag changes nothing.
2. The generated `runner-inputs:mock` tables in docs/workflows.md and
   plugin/README.md, which `tests/unit/mcp/test_runner_input_prose.py` holds
   to the catalogue, and the
   live-proof recipe in docs/workflows.md: a two-step workflow,
   A(mock, `quota_exhausted`, `artifact_before_park`,
   `artifact_name=notes.md`) -> B(mock, `input_from` A: `notes.md`). A's second
   attempt lists `notes.md` with `carried_from` its first, and B stages it.
3. Tests: `tests/unit/worker/test_mock_artifact_before_park.py` on the runner,
   and `test_artifact_before_park_is_carried_to_the_finishing_attempt` in
   `test_parked_uploads_carry.py`, the full carry through the lifecycle with
   no upload patched.

The bridge and swarm-api read `RunnerProfile.inputs` directly
(`check_inputs`), so neither restates the key; the console's `SUGGESTED` mock
list is a subset by design and does not offer the park keys.

### What it would break if accepted

Nothing existing. The key is new and optional; an agent that does not send it
runs exactly as before, and `test_mock_bounded_park.py` still holds the
one-park bound. An agent that sends it to a worker built before this change is
refused by that worker's re-check of the stored input, as any undeclared key
is, so the rollout order is the worker image with the API.

### If it is declined

#166's carry stays proven offline only, by a test that patches the worker's
upload, and the live proof needs a CLI agent to meet a real rate limit.

### Invariants

- **Invariants 1, 2 and 3.** Untouched: no state, lease or pool count changes.
- **Invariant 4.** Unchanged: the mock still checkpoints, parks and exits.
- **Invariant 5.** Unchanged: a fenced park records nothing to carry
  (`test_a_fenced_park_records_nothing_to_carry`).
- **Invariants 6, 7 and 8.** Untouched.
- **Invariant 9.** Unchanged: the carry is read by id within the agent's own
  tenant and prefix.
- **Invariant 10.** Preserved: a caller names `mock` and sends a declared
  boolean, data rather than an image, a command or a resource spec.

---

## 57. `profiles.py`: `url_refusal` accepts site-local, Teredo and other reserved IPv6 ranges, two internal names and a trailing double dot, and a refused URL's 422 can echo its userinfo (#349)

**Status:** ACCEPTED, accepted by the owner 2026-10-08 (the approval of
#349's SwarmCloud plan), and APPLIED 2026-10-08 by the pull request that adds
this entry. Part of nothing else. This is the sanctioned edit to
`apps/common/swarm_common/` for it; nothing else in the frozen package
changes.

### What is true today

Request 32 put one URL rule in the catalogue, `profiles.py::url_refusal`,
and #345 applied it. The #345 security review probed that function on
2026-09-29 and found four gaps, filed as #349:

1. **Four reserved IPv6 ranges are accepted.** `_URL_REFUSED_V6_NETWORKS`
   lists loopback, unspecified, discard-only, `2001:db8::/32`, unique local,
   link-local and multicast, and nothing else. So site-local `fec0::/10`,
   Teredo `2001::/32` (whose address embeds an obfuscated IPv4 one), the
   RFC 9637 documentation range `3fff::/20` and the SRv6 SID range
   `5f00::/16` all pass: `url_refusal("http://[fec0::1]/")` and
   `url_refusal("http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/")` return
   `""`.
2. **Two internal names are accepted.** `_URL_REFUSED_SUFFIXES` refuses
   `.internal`, `.local`, `.localhost` and `.svc`, and neither
   `metadata.goog` (the metadata server's other name) nor
   `localhost.localdomain` ends in one: `url_refusal("http://metadata.goog/")`
   returns `""`.
3. **A trailing double dot is accepted.** `url_refusal` strips the host with
   `.rstrip(".")`, which removes every trailing dot, so `example.com..`
   becomes `example.com` and is accepted, while `a..b.com` reaches the
   empty-label check and is refused.
4. **A refusal can echo a URL's userinfo.** `RunnerInput.check`'s `refuse()`
   repeats up to 40 characters (`_SHOWN_VALUE_CHARS`) of the refused value.
   For `http://user:<password>@169.254.169.254/` that is the whole userinfo,
   in the 422 message the API serves -- while `url_refusal`'s docstring says a
   URL with a password in it is "never stored with the task, served by the
   API or shown in the UI". An `object` or `list` refusal repeats its value's
   `repr` the same way, so a `goto` action refused for an unknown field
   echoed `'url': 'http://user:...` from inside the object.

### Why

None of the four is a blocker -- the worker's NetworkPolicy drops the
private ranges, #341 removes the search path, and the metadata server
answers only a request carrying `Metadata-Flavor: Google` -- which is why #349
is S2. But each is cheap to close and each is the rule saying something
untrue: that the range is a public address, that the name is a public page,
that `example.com..` is a different shape from `a..b`, and that a password
URL is never served. The fourth is the one a caller can see: a 422 body is
logged by whatever client sent the request.

### The requested change

In `apps/common/swarm_common/profiles.py`:

1. `_URL_REFUSED_V6_NETWORKS` gains, each with a comment naming its RFC:

   ```python
   ipaddress.ip_network("fec0::/10"),  # site-local, deprecated by RFC 3879
   ipaddress.ip_network("2001::/32"),  # Teredo, RFC 4380 (refused, not unwrapped)
   ipaddress.ip_network("3fff::/20"),  # documentation, RFC 9637
   ipaddress.ip_network("5f00::/16"),  # SRv6 SIDs, RFC 9602
   ```

   Teredo is refused outright rather than unwrapped by `_embedded_v4` the
   way 6to4 and NAT64 are: its client IPv4 is XOR-obfuscated and its server
   IPv4 is a second embedded address, so unwrapping it would be the rule
   reproducing a tunnelling scheme rather than refusing it. Refusing the /32
   is the subset-of-what-Chromium-accepts rule `url_refusal` already states.
2. A new exact-name tuple, checked next to `_URL_REFUSED_SUFFIXES`:

   ```python
   _URL_REFUSED_HOSTS = ("metadata.goog", "localhost.localdomain")
   ```

   Exact, not a suffix: `.goog` is a real gTLD that Google serves public
   pages on, so `.goog` as a suffix would refuse public sites.
3. The host loses AT MOST ONE trailing dot (`host.removesuffix(".")`)
   instead of `.rstrip(".")`. So
   `example.com.`, a fully-qualified name, is still accepted, and
   `example.com..` reaches the empty-label check and is refused exactly as
   `a..b.com` is. `169.254.169.254.` is still read as the address and refused.
4. `RunnerInput.check`'s `refuse()` never repeats a value whose `repr`
   contains `@`, of any kind: it says `<a value of N characters, not repeated
   because it contains '@'>` instead, and keeps the reason. Every value
   without `@` is repeated as before, up to `_SHOWN_VALUE_CHARS`. Blunt on
   purpose: masking only the userinfo needs a parse of exactly the URL being
   refused -- the one a parser already disagreed about -- and
   `http:user:pw@host` has no `//` for a pattern to anchor on. Not repeating
   the value at all cannot leak. Applied to every kind, not only `url`,
   because an `object` or `list` refusal repeats a `repr` that can hold a
   nested `url` (finding 4's `goto`).
5. `url_refusal`'s docstring lists the new refusals and cites this request.

**The non-frozen half, in the same change:** tests first --
`tests/unit/control_plane/test_url_refusal_349.py` (the issue's repro URLs,
each range's first and last address, its neighbours just outside, and the
userinfo never in a refusal, including `http:user:pw@host` and a nested
`goto`), the WHATWG table in `test_url_refusal_whatwg.py` (each new refusal
as Node's `URL` resolves it, and accepted neighbours
`2001:4860:4860::8888`, `2001:1::1`, `3fff:1000::1`, `5f01::1`), the worker
door in `tests/unit/worker/test_runner_input_door_checks.py` and the HTTP
doors in `test_runner_inputs_by_declaration.py`; and docs/workflows.md's
description of the rule. No shell, jq or TypeScript restatement of these
lists exists: swarm-api and the plugin bridge call `check_inputs`, and
`browser._check_url` calls `url_refusal`.

### What it would break if accepted

A caller that today sends a browser `url` or `goto` naming any of these hosts
gets a 422 instead of an agent -- an agent that would have timed out against a
dropped address, or opened the metadata server's name to a refused request.
swarm-api, the plugin bridge and `browser._check_url` all call the one rule,
so no restatement drifts. `http://example.com./` stays accepted. A 422 for a
value containing `@` no longer quotes that value; the key, the declared bound
and the reason are still named.

### If it is declined

The four gaps stay, each behind its other control, and the docstring's
"never served by the API" stays false for a refused URL.

### Invariants

- **Invariant 10.** Strengthened: a caller still names a runner profile and
  sends data; this only narrows what the `browser` profile's `url` inputs
  accept and what a refusal repeats.
- **Invariants 1-9.** Untouched: no state, lease, pool, fencing, checkpoint,
  resource or tenancy rule changes.

## 58. `specsign.py`: `3600` and `3600.0` have the same canonical bytes, so a rewrite of an integer field to a float keeps the signature valid (#346 box 53)

**Status:** ACCEPTED by the owner 2026-10-11, applied in this PR -- the
diff below, and the non-frozen half (the worker reads its input as
`check_inputs` returns it; swarm-api's 422 names the reason). Already-signed
specs keep verifying: no value with a canonical form changes its bytes, and
no signer writes a float in either field (swarm-api's schemas declare both
`int`). Was: proposed, filed 2026-10-09 for
[#346 box 53](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/346#issuecomment-5901303466)
(the #353 security review), by the owner's decision of 2026-10-09 that the
2026-10-09 epic triage's frozen-contract findings get requests, not edits.

### What is true today

`apps/common/swarm_common/specsign.py::_encode` writes an `int` with
`str(value)` and a `float` through `_number`, RFC 8785's ECMAScript form,
which writes an integral double without a fraction. So `jcs({"a": 3600})` and
`jcs({"a": 3600.0})` are the same bytes (probed 2026-10-09 on main at
`e385027`: equal). That is RFC 8785 working as written -- a JSON number has no
int/float distinction -- but a Firestore document does: it stores an
`integerValue` and a `doubleValue` as different things and reads them back as
`int` and `float`.

So a tenant's agent, which can write any task document of its tenant
(request 34's premise), can rewrite `timeout_seconds: 3600` to `3600.0`,
`max_attempts: 3` to `3.0`, or `input.issue: 7` to `7.0`, and
`apps/common/swarm_common/specsign.py::canonical_step_spec` gives the
rewritten document the bytes the signature covers. The worker's check
(`apps/agent-worker/agent_worker/specverify.py::_verify_signed`) passes it.

What the rewrite cannot do is change a quantity: the value is numerically
the same. What it can do is change a type a reader did not expect. The
readers this lane read cast (`int(task.get("max_attempts", 3))` in the
scheduler's codec and the worker's control plane) or compare with `==`
(`apps/agent-worker/agent_worker/specverify.py::_environment_agrees`, where
`3600 == 3600.0`), so none of them was found to act differently. That is a
property of today's readers, not of the signature, and the signature is
what request 34 says a worker may rely on.

### Why

Request 34 made the signature the worker's reason to trust the spec. A
signed field whose stored type can change under the signature is a field
every future reader has to defend itself against, one `isinstance` at a
time, without knowing it has to. The two top-level fields are cheap to pin:
every writer types them `int` (`Task.timeout_seconds: int`,
`Task.max_attempts: int`, and swarm-api's request schemas declare both
`int`), so the signer never meets a float there.

`input` is different, and this request deliberately leaves its bytes alone.
swarm-api stores an input **as sent** (`apps/swarm-api/swarm_api/validation.py::validate_runner_input`:
"Values are checked, never rewritten"), and a `number` input -- `wait`'s
`seconds`, say -- may legitimately be sent as `2.0`. Refusing an integral
float anywhere in the spec would refuse that submission at the signer.
Instead the input half is closed outside the frozen package, below: an
`integer` input is read through
`apps/common/swarm_common/profiles.py::check_inputs`, which already turns an
integral float into an `int`, so `7.0` reads as issue 7 -- the same issue the
signer signed.

### The requested change

In `apps/common/swarm_common/specsign.py`, refuse a non-integer in the two
integer fields, at **every** format:

```diff
 #: The fields format 3 adds to format 2's projection. Formats 1 and 2 do not
 #: cover them, so a document at either format that carries one is not
 #: canonical (see `canonical_step_spec`): adding a forge credential to a task
 #: signed without one must fail verification, not slip outside the bytes.
 _FORMAT_3_FIELDS = ("forge_credential", "forge_access")
+
+#: The top-level fields every writer stores as integers (`Task`'s types).
+#: JCS gives 3600 and 3600.0 the same bytes, so without this a rewrite of one
+#: to the other keeps the signature valid. Checked at every format: it removes
+#: a canonical form from a value no signer writes, and changes the bytes of
+#: none that has one, so it is not a new format (contract request 58).
+_INTEGER_FIELDS = ("timeout_seconds", "max_attempts")
@@ def canonical_step_spec(
     if spec_format < 3:
         for key in _FORMAT_3_FIELDS:
             if doc.get(key) is not None:
                 raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
+    for key in _INTEGER_FIELDS:
+        value = doc.get(key)
+        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
+            raise SpecNotCanonical(f"{key} is a {type(value).__name__}, not an integer")
     spec = {
```

**At every format, not as format 4.** The module's rule is that a change to
the encoding is a new format. This changes no bytes: every value that had a
canonical form before keeps the same one, and a float in either field had
bytes only because JCS could not tell it from the integer. A format-4-only
check would leave every format-1 document -- every agent that names no parent
and no forge credential, which is most of them -- exactly as rewritable as
today.

**The non-frozen half, in the same change:** tests first, in
`tests/unit/common/test_specsign_covers.py` (a document with `3600.0` or
`3.0` is `SpecNotCanonical` at formats 1, 2 and 3; `3600` still verifies; a
`True` is refused), `tests/unit/worker/test_spec_signature_worker.py` (the
rewritten document ends `SPEC_SIGNATURE_INVALID` with reason
`not_canonical`, and no runner starts) and
`tests/unit/control_plane/test_spec_signing_submission.py`. Then:

1. `apps/swarm-api/swarm_api/specsigning.py::sign_task_specs`'s 422 message,
   which lists the reasons a value has no canonical form, names this one.
2. The worker reads its inputs through `check_inputs`'s **return**, not the
   stored map: `apps/agent-worker/agent_worker/lifecycle.py::_recheck_runner_input`
   calls `check_inputs` today and discards what it returns, so the issue
   number the worker uses (`apps/agent-worker/agent_worker/lifecycle.py::Worker._title_from_issue_input`
   and the fetch) is the stored value. Returning the normalised map and
   reading that closes `input.issue` without touching the bytes.

### What it would break if accepted

* **A stored agent whose `timeout_seconds` or `max_attempts` is a double**
  fails its worker's check (`not_canonical`) and ends
  `SPEC_SIGNATURE_INVALID` instead of running. No writer this lane read
  stores one, but this was not measured against Firestore: before applying,
  query the non-terminal agents for a `doubleValue` in either field.
* **A submission that reached the signer with a float there** becomes a 422
  `NonCanonicalInput` instead of a signed agent. swarm-api's schemas declare
  both fields `int`, so pydantic turns a JSON `3600.0` into `3600` first.
* Nothing restates the projection: swarm-api, the worker and
  `apps/swarm-api/swarm_api/verdictpublish.py` all call
  `canonical_step_spec`.

### If it is declined

The signature keeps vouching for a numeric value, not its type, in
`timeout_seconds`, `max_attempts` and every number under `input`. Today's
readers cast or compare with `==`, so nothing is known to break; each new
reader of those fields has to keep doing so, and nothing tells it to.

### Invariants

- **Invariant 10.** Strengthened: a caller still sends a profile name and
  data; a rewrite of the signed spec's types after submission is refused.
- **Invariant 5.** Untouched: a refused spec ends before any runner starts,
  as every other `SPEC_SIGNATURE_INVALID` does; fencing is not consulted.
- **Invariants 1-4, 6-9.** Untouched: no state, lease, pool, checkpoint,
  resource or tenancy rule changes.

## 59. `specsign.py`: a spec nested past Python's recursion limit raises `RecursionError` instead of `SpecNotCanonical` (#346 box 55)

**Status:** ACCEPTED by the owner 2026-10-11, applied in this PR -- the
diff below, and swarm-api's 422 message names the depth. Was: proposed, filed
2026-10-09 for
[#346 box 55](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/346#issuecomment-5901303857)
(the #353 security review), by the owner's decision of 2026-10-09.

### What is true today

`apps/common/swarm_common/specsign.py::_encode` recurses once per list and
once per map, with no depth bound, and nothing catches `RecursionError`. A
list nested 2,000 deep raises `RecursionError`, not `SpecNotCanonical`
(probed 2026-10-09 on main at `e385027`). Both callers catch only
`SpecNotCanonical`: `apps/agent-worker/agent_worker/specverify.py::_verify_signed`
turns it into `SPEC_SIGNATURE_INVALID` with reason `not_canonical`, and
`apps/swarm-api/swarm_api/specsigning.py::sign_task_specs` into a 422
`NonCanonicalInput`. A `RecursionError` escapes both.

It **fails closed**: the worker crashes before any runner starts, and the
signer writes nothing. And it is **unreachable today**: Firestore refuses a
document nested deeper than 20 levels, so no stored agent reaches the worker
that deep; at swarm-api the `input` is bounded by its profile's declaration
and the three signed metadata keys are the API's own blocks, and
`apps/swarm-api/swarm_api/validation.py::validate_storable` already walks a
payload with a stack for exactly this reason.

### Why

The function's contract is "canonical bytes or `SpecNotCanonical`", and each
caller is written to that contract. A crash where a refusal belongs is a
worker that ends with no end cause and an error nobody branches on, if the
unreachable ever becomes reachable -- a store with a different limit, a
caller that passes something other than a stored document, or a cycle
(a map that contains itself also recurses until `RecursionError`). The bound
costs one integer.

### The requested change

In `apps/common/swarm_common/specsign.py`:

```diff
 #: JCS numbers are IEEE doubles, so an integer is exact only within this bound.
 MAX_SAFE_INTEGER = 2**53 - 1
+
+#: How deep `_encode` follows lists and maps. Firestore refuses a document
+#: nested deeper than 20 levels and the spec puts a stored field one level
+#: down, so 32 refuses nothing Firestore can hold, and stays far inside
+#: Python's recursion limit -- which `_encode` reached as `RecursionError`,
+#: not `SpecNotCanonical`, before this bound (contract request 59).
+MAX_DEPTH = 32
@@ def jcs(value: Any) -> bytes:
     """RFC 8785 serialisation of `value`, as UTF-8 bytes."""
-    return _encode(value, "$").encode("utf-8")
+    return _encode(value, "$", 0).encode("utf-8")
 
 
-def _encode(value: Any, path: str) -> str:
+def _encode(value: Any, path: str, depth: int) -> str:
@@
     if isinstance(value, (list, tuple)):
-        return "[" + ",".join(_encode(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
+        if depth >= MAX_DEPTH:
+            raise SpecNotCanonical(f"{path} is nested deeper than {MAX_DEPTH} levels")
+        return "[" + ",".join(
+            _encode(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)
+        ) + "]"
     if isinstance(value, Mapping):
+        if depth >= MAX_DEPTH:
+            raise SpecNotCanonical(f"{path} is nested deeper than {MAX_DEPTH} levels")
         items = []
@@
         return "{" + ",".join(
-            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}") for k in items
+            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}", depth + 1)
+            for k in items
         ) + "}"
```

No new format: every value within the bound encodes to the same bytes, and
a value beyond it had no bytes before (it raised).

**The non-frozen half, in the same change:** tests first, in
`tests/unit/common/test_specsign_covers.py` (a list and a map 2,000 deep,
and a map that contains itself, are each `SpecNotCanonical`; 32 levels of
containers still encode and 33 do not; a stored document 20 deep still
verifies), and `tests/unit/worker/test_spec_signature_worker.py` (a deep
document ends `SPEC_SIGNATURE_INVALID`, reason `not_canonical`, with no
runner started). And
`apps/swarm-api/swarm_api/specsigning.py::sign_task_specs`'s 422 message
names the depth among its reasons.

### What it would break if accepted

Nothing a stored document can reach: 32 is above Firestore's 20. A caller of
`jcs` outside a stored document that nests deeper than 32 gets
`SpecNotCanonical` instead of bytes; none exists (swarm-api, the worker and
`apps/swarm-api/swarm_api/verdictpublish.py` all pass a stored document).

### If it is declined

The failure stays closed and unreachable: a deep or cyclic spec crashes the
worker before any runner starts, with no end cause recorded, and a signer
that met one would answer 500 rather than 422.

### Invariants

- **Invariant 5.** Untouched: a refused spec ends before any runner starts,
  as today; it now ends with an end cause instead of a crash.
- **Invariants 1-4, 6-10.** Untouched.

## 60. `profiles.py`: a worker-action profile with `secrets`, a GKE backend or a plain-string `worker_action` passes `__post_init__` (#453 box 87)

**Status:** ACCEPTED by the owner 2026-10-11, applied in this PR. Item 2
is applied as its amendment below allows: scoped to `WorkerAction.POST_VERDICT`,
the one action that runs as its own Cloud Run Job's service account, so
`merge` on `GKE_AUTOPILOT` (request 66) still constructs. Items 1 and 3 are
applied as written, for every worker action.

Filed 2026-10-09 as a proposal for
[#453 box 87](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/453#issuecomment-5944064436)
(the #462 review probe: "tighten before any worker-action profile is
enabled"), by the owner's decision of 2026-10-09.

### What is true today

`apps/common/swarm_common/profiles.py::RunnerProfile.__post_init__` checks
two things about a worker action: a profile that names one has an empty
`runner_argv`, and a profile that names none has a non-empty one. Nothing
else. So each of these constructs without error (read on main at
`e385027`):

1. **`secrets=("x",)` on a worker-action profile.** The catalogue's own
   comment on `merge` and `post-verdict` is that "the credential is never
   mounted": the worker reads it at action time. For a profile with a
   `provider` (both have one) and a tenant not served from the account pool,
   the Cloud Run dispatcher projects each entry of `profile.secrets` into the
   Job's environment
   (`apps/scheduler/scheduler/dispatch.py::CloudRunJobDispatcher._build_job`),
   so a profile that listed one would mount it.
2. **`backend=Backend.GKE_AUTOPILOT` (or `AUTO`).** A worker action's whole
   isolation is its own Cloud Run Job's service account (contract requests 33
   and 35; `swarm-<tenant>-post-verdict`). A GKE pod runs as the tenant's
   worker KSA (`apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher.ksa_for`),
   the identity the catalogue's comment says these profiles exist to keep
   off the action's credential.
3. **`worker_action="merge"`, a plain string.** `WorkerAction` is a `str`
   enum, so the string compares equal to `WorkerAction.MERGE` and every
   `is not None` check treats the profile as a worker action. But
   `apps/agent-worker/agent_worker/lifecycle.py::Worker._run_worker_action`
   logs `action.value` (an `AttributeError` on a `str`) and then picks the
   action with `action is WorkerAction.MERGE`, which a string never is -- so
   the string `"merge"`, past the first line, would run `post-verdict`.

The two worker-action profiles in the catalogue today, `merge` and
`post-verdict`, are each `Backend.CLOUD_RUN_JOB`, `secrets=()` and a
`WorkerAction` member, so none of the three is live. Each is a catalogue
edit away.

### Why

The catalogue is the one place a profile is declared, and every reader
trusts `__post_init__` to have refused a profile that cannot be what it
says. The worker-action profiles are the ones that hold a forge credential
no agent may share; a profile that mounted it, or ran it as the tenant's
worker identity, would undo request 33's reason for existing, and the check
that should refuse it is three `if`s.

### The requested change

In `apps/common/swarm_common/profiles.py`, `RunnerProfile.__post_init__`:

```diff
         if self.worker_action is None and not self.runner_argv:
             raise ValueError(
                 f"runner {self.name}: an empty runner_argv starts nothing; only a "
                 "worker_action profile may have one (contract request 33)"
             )
+        if self.worker_action is not None:
+            # Contract request 60 (#453): a worker action reads its credential
+            # itself, at action time, as its own Cloud Run Job's service
+            # account, and the lifecycle dispatches on the enum by identity.
+            if not isinstance(self.worker_action, WorkerAction):
+                raise ValueError(
+                    f"runner {self.name}: worker_action must be a WorkerAction, not a "
+                    f"{type(self.worker_action).__name__} (contract request 60)"
+                )
+            if self.secrets:
+                raise ValueError(
+                    f"runner {self.name}: a worker_action profile mounts no secret; the "
+                    "worker reads its credential at action time (contract request 60)"
+                )
+            if self.backend is not Backend.CLOUD_RUN_JOB:
+                raise ValueError(
+                    f"runner {self.name}: a worker_action profile runs as its own Cloud "
+                    "Run Job's service account, so its backend is CLOUD_RUN_JOB, not "
+                    f"{self.backend.value} (contract request 60)"
+                )
         for key, declared in self.inputs.items():
```

`AUTO` is refused with `GKE_AUTOPILOT`: it may resolve to GKE.

**The non-frozen half, in the same change:** tests first, in
`tests/unit/common/test_worker_action_profiles.py`: each of the three
constructs raises `ValueError` naming its rule (a string `"merge"`,
`secrets=("x",)`, `Backend.GKE_AUTOPILOT` and `Backend.AUTO`), and `merge`
and `post-verdict` still construct. No other file restates the rule.

### What it would break if accepted

Nothing in today's catalogue: both worker-action profiles already satisfy
all three. A future worker-action profile that wants a mounted secret or a
GKE backend has to come back here, which is the point.

**Amendment 2026-10-10 (contract request 66):** that is no longer true of
item 2. Request 66 (accepted by the owner, option A of #939) moves `merge` to
`GKE_AUTOPILOT`, so the `backend is not Backend.CLOUD_RUN_JOB` check written
above would refuse the catalogue at import. Item 2's premise -- "a worker
action's whole isolation is its own Cloud Run Job's service account" -- is
contract requests 33 and 35's, and request 47 (owner, 2026-10-04) retired it
for `merge`: merge runs as the tenant's worker account on either backend, the
identity the GKE pod's KSA is bound to. If request 60 is accepted, item 2
has to name the profiles that DO run as an account of their own
(`post-verdict` today) instead of every worker action, or be dropped. Items 1
and 3 are unaffected. The decision is the owner's.

### If it is declined

The three stay one catalogue edit from live, held only by review: a
worker-action profile that mounted its credential or ran as the tenant's
worker identity would construct, dispatch and run.

### Invariants

- **Invariant 9.** Strengthened: a worker action's credential stays with its
  own service account, never the tenant worker's identity or a mounted
  environment.
- **Invariant 10.** Untouched: callers still pick a profile by name; this
  constrains what the catalogue may declare.
- **Invariants 1-8.** Untouched.

## 61. `profiles.py`: `url_refusal` still accepts the benchmarking and deprecated ORCHID ranges of `2001::/23`, the remainder of #346 box 42 after request 57

**Status:** ACCEPTED by the owner 2026-10-11, applied in this PR.

Filed 2026-10-09 as a proposal for
[#346 box 42](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/346#issuecomment-5895194106)
(the #345 security review, tracked in #349), by the owner's decision of
2026-10-09.

### What is true today

Request 57 (#349, applied 2026-10-08 by PR 915) closed all four gaps #349
names, each re-read on main at `e385027` on 2026-10-09:

* site-local `fec0::/10`, Teredo `2001::/32`, `3fff::/20` and `5f00::/16`
  are in `apps/common/swarm_common/profiles.py::_URL_REFUSED_V6_NETWORKS`;
* `metadata.goog` and `localhost.localdomain` are
  `apps/common/swarm_common/profiles.py::_URL_REFUSED_HOSTS`, refused by
  `apps/common/swarm_common/profiles.py::url_refusal`;
* the host loses at most one trailing dot (`host.removesuffix(".")`), so
  `http://example.com../` is refused for its empty label;
* `apps/common/swarm_common/profiles.py::RunnerInput.check`'s `refuse()`
  never repeats a value whose `repr` contains `@`.

Box 42 also names "reserved IPv6 ranges" generally, and the 2026-10-09
triage read it as `2001::/23` (IANA's IETF Protocol Assignments block).
Teredo is the part of it request 57 refused. Two more of its assignments are
**not globally reachable** in the IANA IPv6 Special-Purpose Address Registry
and are still accepted (probed 2026-10-09: `url_refusal` returns `""` for
each, and Python's `ipaddress` calls neither global):

* `2001:2::/48`, benchmarking (RFC 5180): `http://[2001:2::1]/`;
* `2001:10::/28`, the deprecated first ORCHID range (RFC 4843, obsoleted by
  RFC 7343): `http://[2001:10::1]/`.

The block's globally reachable assignments -- `2001:1::1`/`::2`/`::3`
anycast, AMT `2001:3::/32`, AS112 `2001:4:112::/48`, ORCHIDv2
`2001:20::/28`, Drone Remote ID `2001:30::/28` -- are public addresses and
stay accepted, as request 57 already kept `2001:1::1`.

### Why

The same reason as request 57: none is a blocker (the worker's
NetworkPolicy and the absence of routes behind both ranges stand in the
way), but each is the rule saying a non-public range is a public address,
and closing it is two lines.

### The requested change

In `apps/common/swarm_common/profiles.py`:

```diff
     ipaddress.ip_network("2001::/32"),  # Teredo, RFC 4380 (refused, not unwrapped)
+    ipaddress.ip_network("2001:2::/48"),  # benchmarking, RFC 5180 (request 61)
+    ipaddress.ip_network("2001:10::/28"),  # ORCHID, RFC 4843, deprecated by RFC 7343 (request 61)
     ipaddress.ip_network("2001:db8::/32"),  # documentation
```

and `url_refusal`'s docstring names the two ranges among those refused.

Named ranges, not the whole `/23` with the global assignments carved out:
that would need an accept list inside a refuse list, a second mechanism for
two ranges, and the unassigned rest of the block routes nowhere.

**The non-frozen half, in the same change:** tests first, in
`tests/unit/control_plane/test_url_refusal_349.py` (each range's first and
last address refused; `2001:1::1`, `2001:3::1`, `2001:4:112::1` and
`2001:20::1` still accepted) and the WHATWG table in
`tests/unit/control_plane/test_url_refusal_whatwg.py`.

### What it would break if accepted

A `browser` agent whose `url` or `goto` names an address in either range gets
a 422 instead of an agent that would have timed out. swarm-api, the plugin
bridge and the browser runner all call the one rule.

### If it is declined

Both ranges stay accepted behind the NetworkPolicy, and box 42 stays open
for them alone.

### Invariants

- **Invariant 10.** Strengthened: it narrows what the `browser` profile's
  `url` inputs accept.
- **Invariants 1-9.** Untouched.

## 62. `states.py`: a merge step waiting for its repository's merge slot parks as `CI_PENDING`, a reason that says it waits for CI

**Status:** proposed, filed 2026-10-10 by the merge-race lane (part of
[#295](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/295)). A
request, not a change: nothing under `apps/common/swarm_common/` is edited
by it, and the slot is built on `ParkReason.CI_PENDING` meanwhile.

### What is true today

Measured 2026-10-10 across the eng tenant: 16 merge steps ran against one
`main` that requires an up-to-date branch, each landing merge put the rest
behind, and 7 refused `behind_too_often`. The fix
(`apps/agent-worker/agent_worker/mergeslot.py`) gives every (tenant,
repository, base) one merge slot. A step whose checks are green and that
cannot have the slot parks, holding nothing, until the holder's release
hands the slot to it.

That park is `ParkReason.CI_PENDING` with the code `merge_slot_wait` in
`metadata.merge_wait.code`. Everything that must tell the two apart reads
the code, not the reason:

* `apps/agent-worker/agent_worker/control.py::ControlPlane.park_ci_pending`
  counts it on `merge_wait.slot_waits`, not `wakes`, and clears
  `first_parked_at` so `checks_timeout` does not time a queue;
* `apps/swarm-api/swarm_api/mergewake.py::wake_tenant` skips it, because
  its checks are green and reading them would wake it every tick;
* the API's `blocked_by`, the UI and the MCP tools show `CI_PENDING`, so a
  step queued behind seven merges reads as "waiting for CI".

### Why

A park reason is what a reader -- a person, the UI, a metric -- uses to say
why an agent is not running. "Waiting for CI" for a step whose CI is green is
the wrong answer, and the code that corrects it lives in a tenant-writable
metadata field that nothing outside the merge path reads.

### The requested change

In `apps/common/swarm_common/states.py`, beside `CI_PENDING`:

```diff
     CI_PENDING = "CI_PENDING"
+    #: A merge step whose checks are green waits for its repository's merge
+    #: slot (`agent_worker.mergeslot`); promoted by the scheduler's CI-wait
+    #: sweep on the release's wake marker or the fallback instant.
+    #: Contract request 63.
+    MERGE_SLOT_WAIT = "MERGE_SLOT_WAIT"
```

**The non-frozen half, in the same change:** the scheduler's
`_promote_ci_waits` sweeps both reasons; `park_ci_pending` takes the reason
from the code; `mergewake.wake_tenant` queries `CI_PENDING` alone and drops
its skip; `mergeslot.FirestoreMergeSlots.wake` guards on the new reason;
and the UI and MCP park labels name it.

### What it would break if accepted

Every reader that lists park reasons exhaustively (the UI's labels, the MCP
rows, `docs/api-refusals.md`) needs the new member, and a slot wait parked
by a worker of the old image during the rollout stays `CI_PENDING` with the
code -- the sweep must keep reading both until those have drained.

### If it is declined

The slot keeps working as it is: `CI_PENDING` plus the code. Only the label
a person reads is wrong.

### Invariants

- **Invariant 1.** Unchanged: the wait is a park, holding no lease and no
  pool count, whichever reason it carries.
- **Invariant 5.** Unchanged: the slot's writes are fenced on the
  attempt's agent and lease, under either reason.
- **Invariants 2-4, 6-10.** Untouched.

---

## 63. `profiles.py`: indexer runs on GKE_AUTOPILOT (canary for #939)

**Status:** ACCEPTED by the owner 2026-10-10, option A of
`docs/incidents/2026-10-09-egress-open-delay.md` (PR 986), and APPLIED by the
pull request that adds this entry. Canary first: this moves ONLY `indexer`;
the other profiles on Cloud Run Jobs follow after the canary is measured.

### The measurement

A new Cloud Run instance's internet path opens a median **20.2 s** after start
(n=19), against GKE Autopilot's **1.17 s** (n=148) through the same Cloud NAT
(`egress_ready_seconds`, the per-step mark #721 (a) added; the #939
investigation, PR 986). The wait sits inside every clone a Cloud Run step
makes, and nothing the step does shortens it.

### The change

In `apps/common/swarm_common/profiles.py`, the `"indexer"` entry:

```diff
     "indexer": RunnerProfile(
         name="indexer",
         image="agent-runtime-indexer",
         resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
+        backend=Backend.GKE_AUTOPILOT,
```

Nothing else in the frozen package changes. `indexer` is again claude-code in
every field but its name and its image (contract request 48), now that both run
on Autopilot. The GKE path needs nothing new for it: the dispatcher renders the
default worker Job for any profile, with the image from `WORKER_IMAGE_REFS`
(where `agent-runtime-indexer`'s digest is already pinned), MODEL from
`WORKER_MODELS` (which already names `indexer`), and the `TENANT_ID` and
`ARTIFACT_BUCKET` the index run's graph writer takes as its target
(`agent_worker.indexrun.phase_env`). The `standard` class's GKE disk layout
(`GKE_DISK`, workspace 5 + /tmp 4 + HOME 1 GiB) is claude-code's, and the
index run keeps its scratch inside the workspace (`ws.tmp`).

The non-frozen follow-ups, in the same change: `terraform/infra/locals.tf`
mirrors the backend and adds `indexer` to `cloud_run_fallback_profiles`, so
the tenants' `swarm-job-<tenant>-indexer` Cloud Run Jobs stay, idle, as the
rollback; `tests/terraform/catalogue.tftest.hcl` holds both; and the docs that
named indexer's backend name the new one.

### Rollback

Set `indexer`'s backend back to `Backend.CLOUD_RUN_JOB` and release. While
`indexer` is in `cloud_run_fallback_profiles` its Cloud Run Jobs still exist,
so no Terraform apply is needed.

### What it would break

What request 53 recorded for claude-code, now for index runs too: Autopilot
nodes can end a pod by auto-repair or pressure eviction, which extended run
time does not suppress, and periodic checkpointing makes that cost minutes,
not the run. The workspace is a disk `emptyDir`, bounded by the pod's
ephemeral-storage limit rather than by memory. Index runs share the
`GKE_AUTOPILOT` backend ceiling (dev 100) with claude-code and browser.

### The follow-up list: profiles still on CLOUD_RUN_JOB after this change

| profile | reaches the internet? |
|---|---|
| `merge` | yes: GitHub, to merge with the tenant's `-git` token |
| `claude-code-review` | yes: GitHub and the Anthropic API (retired, `available=False`, no Job) |
| `generic` | yes when its task names a repository: the worker clones it from GitHub, and `npm-ci` / `uv-sync` fetch from package registries |
| `codex` | yes: GitHub and the OpenAI API (disabled, `available=False`) |
| `post-verdict` | yes: GitHub (disabled, `available=False`, no Job) |
| `mock` | no |

### Invariants

- **Invariant 1.** Unchanged: a GKE pod is created only for a leased agent; an
  unschedulable pod is a DISPATCHED lease bounded by the dispatch deadline,
  never a backlog.
- **Invariant 2.** Unchanged: admission reserves the `GKE_AUTOPILOT` backend
  pool instead of `CLOUD_RUN_JOB`, in the same single transaction.
- **Invariants 3, 4, 5, 8.** Unchanged: the same worker image ENTRYPOINT, the
  generation in the pod's env and Job name, `backoffLimit: 0`.
- **Invariant 6.** Preserved: no Spot; `safe-to-evict: "false"` on Job and pod.
- **Invariant 7.** Preserved: the GKE manifest sets requests == limits.
- **Invariant 9.** Unchanged: the tenant's namespace and KSA bound to its own
  GSA, its own secrets and GCS prefix.
- **Invariant 10.** Unchanged: a caller still names `indexer` and cannot choose
  where it runs.

---

## 64. `profiles.py`: generic runs on GKE_AUTOPILOT (#939 option A)

**Status:** ACCEPTED by the owner 2026-10-10, option A of #939
(`docs/incidents/2026-10-09-egress-open-delay.md`, PR 986), and APPLIED by the
pull request that adds this entry. That pull request is HELD: the owner merges
it after measuring request 63's indexer canary (PR 991, deployed 2026-10-10).
Requests 64, 65 and 66 are the follow-up list request 63 recorded.

### The measurement

Request 63's: a new Cloud Run instance's internet path opens a median
**20.2 s** after start (n=19), against GKE Autopilot's **1.17 s** (n=148),
through the same Cloud NAT (`egress_ready_seconds`, #721 (a); PR 986). A
generic agent that names a repository clones it first, and `npm-ci` and
`uv-sync` fetch from package registries, so the wait sits inside it.

### The change

In `apps/common/swarm_common/profiles.py`, `_GENERIC_PROFILE` (the `"generic"`
entry is built from it):

```diff
 _GENERIC_PROFILE = RunnerProfile(
     name="generic",
     image="agent-runtime-base",
     resource_class="standard",
-    backend=Backend.CLOUD_RUN_JOB,
+    backend=Backend.GKE_AUTOPILOT,
```

Nothing else in the frozen package changes for it. The GKE path needs nothing
new: the dispatcher renders the default worker Job for any profile, with
`agent-runtime-base` from `WORKER_IMAGE_REFS`; generic starts no model, so no
MODEL. The runner child's HOME and TMPDIR are inside the workspace
(`Workspace.child_env`), so npm's and uv's caches land on the `standard`
class's 5 GiB disk workspace (`GKE_DISK`), as claude-code's do.

The non-frozen follow-ups, in the same change: `terraform/infra/locals.tf`
mirrors the backend and adds `generic` to `cloud_run_fallback_profiles`, so
every tenant's `swarm-job-<tenant>-generic` Cloud Run Job stays, idle, as the
rollback; `tests/terraform/catalogue.tftest.hcl` holds both;
`tests/unit/control_plane/test_internet_profiles_on_gke.py` pins the backend;
and the docs that named generic's backend name the new one.

**The smoke suite's GKE row changes with it.** `scripts/smoke-test.sh` covers
each backend with one profile, preferring one that needs no credential, so
GKE_AUTOPILOT's row becomes `generic` (its fixture clones this public
repository and runs `pytest`) instead of `browser`. That row now also proves
the GKE internet path; it no longer submits a browser agent or its page-load
fixture check. `tests/unit/scripts/test_smoke_covers_every_backend.py`
follows.

### Rollback

Set `_GENERIC_PROFILE`'s backend back to `Backend.CLOUD_RUN_JOB` and release.
While `generic` is in `cloud_run_fallback_profiles` its Cloud Run Jobs still
exist, so no Terraform apply is needed.

### What it would break

What request 63 recorded for indexer: Autopilot nodes can end a pod by
auto-repair or pressure eviction, which extended run time does not suppress,
and periodic checkpointing makes that cost minutes. A full workspace evicts
the pod instead of failing a write with ENOSPC. Generic agents share the
`GKE_AUTOPILOT` backend ceiling (dev 100) with claude-code, browser and
indexer.

### Invariants

- **Invariant 1.** Unchanged: a GKE pod is created only for a leased agent.
- **Invariant 2.** Unchanged: admission reserves the `GKE_AUTOPILOT` backend
  pool instead of `CLOUD_RUN_JOB`, in the same single transaction.
- **Invariants 3, 4, 5, 8.** Unchanged: the same worker ENTRYPOINT, the
  generation in the pod's env and Job name, `backoffLimit: 0`.
- **Invariant 6.** Preserved: no Spot; `safe-to-evict: "false"` on Job and pod.
- **Invariant 7.** Preserved: the GKE manifest sets requests == limits.
- **Invariant 9.** Unchanged: the tenant's namespace and KSA bound to its own
  GSA, its own secrets and GCS prefix.
- **Invariant 10.** Unchanged: a caller still names `generic` and cannot
  choose where it runs.

---

## 65. `profiles.py`: codex runs on GKE_AUTOPILOT (#939 option A)

**Status:** ACCEPTED by the owner 2026-10-10, option A of #939, and APPLIED
by the pull request that adds this entry, held with request 64 until the
indexer canary is measured.

### The change

In `apps/common/swarm_common/profiles.py`, the `"codex"` entry:

```diff
     "codex": RunnerProfile(
         name="codex",
         image="agent-runtime-base",
         resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
+        backend=Backend.GKE_AUTOPILOT,
```

codex clones from GitHub and calls the OpenAI API, so request 64's
measurement is its reason too. It is disabled (`available=False`, 2026-09-23),
so nothing dispatches to it either way today; moving it now means re-enabling
it is still one word, not one word and a backend switch. On GKE no secret is
projected: the worker reads `swarm-tenant-<tenant>-openai` from Secret Manager
itself (`agent_worker/secrets.py`) under the pod's KSA, as it reads
claude-code's key. No MODEL: codex's CLI takes no Anthropic model name.

The non-frozen follow-ups are request 64's: `locals.tf` mirrors the backend
and adds `codex` to `cloud_run_fallback_profiles` (each tenant that registers
`openai` keeps its `swarm-job-<tenant>-codex`), the catalogue test holds both,
and the docs follow.

### Rollback

`codex`'s backend back to `Backend.CLOUD_RUN_JOB` and a release; its Jobs
exist while it is in `cloud_run_fallback_profiles`.

### What it would break

Request 64's, should it be re-enabled.

### Invariants

As request 64: 1-8 unchanged or preserved by the same GKE manifest; 9 the
tenant's own namespace, KSA and secret; 10 a caller still names `codex`.

---

## 66. `profiles.py`: merge runs on GKE_AUTOPILOT (#939 option A); post-verdict and claude-code-review stay on CLOUD_RUN_JOB

**Status:** ACCEPTED by the owner 2026-10-10, option A of #939, and APPLIED
by the pull request that adds this entry, held with request 64 until the
indexer canary is measured. **It conflicts with proposed request 60's item 2**
(see the amendment recorded there): the owner decides which gives way.

### The change

In `apps/common/swarm_common/profiles.py`, the `"merge"` entry:

```diff
     "merge": RunnerProfile(
         name="merge",
         image="agent-runtime-base",
         resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
+        backend=Backend.GKE_AUTOPILOT,
```

A merge reads GitHub's checks and then merges, so every MERGE verdict paid a
Cloud Run start and request 64's internet-path wait (#748). The incident's
option A left `merge` out because request 60 would refuse a GKE worker action;
that rule's reason is a worker action's own service account, and merge has
had none since contract request 47 (owner, 2026-10-04): it runs as the
tenant's worker account and reads the tenant's `-git` secret at merge time
through `resolve_git_token`. On GKE the pod's KSA is bound to that same
account, so it reads the same secret. The merge action reads no Job-rendered
environment (`agent_worker/merge.py` takes its target from the agent's
`repository_url`), mounts no secret (`secrets=()`), and starts no runner.

The non-frozen follow-ups are request 64's: `locals.tf` mirrors the backend
and adds `merge` to `cloud_run_fallback_profiles` (each tenant that registers
`git` keeps its `swarm-job-<tenant>-merge`, as
`tests/terraform/merge_step_iam.tftest.hcl` already holds), the catalogue
test holds the backend, and the docs follow.

### What does not move, and why

| profile | stays on `CLOUD_RUN_JOB` because |
|---|---|
| `post-verdict` | It was designed to run as its own Job's service account, `swarm-<tenant>-post-verdict` (request 35), and its action reads `FORGE_HOST`/`FORGE_OWNER`/`FORGE_REPO` and the review App's ids from the environment Terraform renders onto that Job (`agent_worker.forge.forge_target_from_env`). A GKE pod runs as the tenant's one worker KSA and carries neither. Disabled (`available=False`), no Job. |
| `claude-code-review` | It was designed to run as its own account, `swarm-<tenant>-review` (request 36), which a GKE pod cannot be. Retired (`available=False`), no Job; request 50 decides whether it leaves the catalogue. |
| `mock` | It reaches no internet. |

Neither of the first two runs anywhere today, so nothing waits on them.

### Rollback

`merge`'s backend back to `Backend.CLOUD_RUN_JOB` and a release; its Jobs
exist while it is in `cloud_run_fallback_profiles`.

### What it would break

Request 64's node-side costs, for a step bounded at 600 s. And request 60 as
written: accepting it unchanged after this would make `__post_init__` refuse
`merge`, so the catalogue would not import.

### Invariants

As request 64: 1-8 unchanged or preserved by the same GKE manifest. **9**
unchanged: merge already ran as the tenant's worker account, now through its
namespace's KSA; the `-git` token is still never mounted, written or logged.
**10**: a caller still names `merge` and cannot choose where it runs.

---

## 67. `profiles.py`: a `claude-code-browser` runner profile, claude-code on agent-runtime-browser

**Status:** ACCEPTED by the owner 2026-10-11, and APPLIED by the pull request
that adds this entry.

### Why

An agent could not render or screenshot a page. `browser` is a scripted
runner: it opens `input.url` or plays `input.actions` and needs one of them,
so it cannot look at a page it has just built or follow a bug report's steps
on its own judgement. `claude-code` can, but its image, agent-runtime-base,
carries no Chromium. agent-runtime-browser (agent-runtime-base plus Playwright
and Chromium, `images/agent-runtime-browser/Dockerfile`) is already built,
scanned and promoted by every release. Visual QA and reproducing a UI bug
need the agent and the browser in one pod.

### The change

In `apps/common/swarm_common/profiles.py`, a new entry in `RUNNER_PROFILES`:

```python
"claude-code-browser": RunnerProfile(
    name="claude-code-browser",
    image="agent-runtime-browser",
    resource_class="browser",
    backend=Backend.GKE_AUTOPILOT,
    runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
    provider="anthropic",
    secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    secrets_any_of=True,
    timeout_seconds=7200,
    inputs=_CLI_AGENT_INPUTS,
),
```

It is `claude-code` in every field but three: its name; its image; and its
resource class, `browser` (8 vCPU, 16 GiB, 2 units), because the image needs
it: the GKE pod mounts a 2 GiB memory-medium tmpfs at `/dev/shm` for Chromium
and that tmpfs counts against the memory limit before a tab is open. Its
backend is GKE Autopilot, as claude-code's is and as the browser image's must
be: Cloud Run cannot size `/dev/shm`. Its runner is claude-code's, so it
takes the same prompt handling, the same `issue` input (request 28), the same
credential choice and the same model (`local.runner_models`).

The non-frozen follow-ups, in the same pull request: the Terraform mirror
(`runner_profiles`, `runner_models`); the worker's `cli_agent_spec` and the
API's `AGENT_STREAM_FILES`, so its streams are claude-code's; the claude-code
runner passes `PLAYWRIGHT_BROWSERS_PATH` to the CLI when the image sets it,
and tells the agent Chromium launches only with its sandbox off (no user
namespaces, every capability dropped -- the pod is the isolation); the UI's
and the plugin's profile lists; the parity script; the runner-profiles doc.

### Rollback

Delete the entry and its mirrors (the follow-ups above) and release. No Cloud
Run Job exists for it (a GKE profile gets none), so nothing in Terraform is
destroyed but the pool document Terraform made for it.

### What it would break

Nothing that exists: a new name. It costs a `browser` resource class, twice a
`standard` step's units, so a tenant's browser pool ceiling is shared with the
scripted `browser` profile.

### Invariants

1-8 unchanged: the same GKE manifest, requests == limits from the `browser`
class, on-demand only, checkpointing as claude-code's. **9**: the tenant's own
namespace, KSA and secret, as claude-code's. **10**: a caller names
`claude-code-browser`; the image, class and backend are the catalogue's, and an
agent that sends an image is refused as every agent is.

---

## 68. `states.py`: `ParkReason.HUMAN_REQUIRED`, a live-browser hand-off parked when nobody came within the hold (LB-A)

**Status:** ACCEPTED by the owner 2026-10-11 (#1030, the answers to Q1-Q13,
every recommendation taken), and APPLIED by the pull request that adds this
entry (lane LB-L0).

### Why

docs/design/live-browser.md section 4.3. A browser agent that needs a person
to sign in holds its pod for a bounded time (`human_hold_seconds`, 10 minutes,
Q3). If nobody takes control within it, invariant 4 forbids sleeping on: the
worker checkpoints, wipes the browser, parks, releases its lease and exits,
exactly as the quota path does. The park needs a reason, and no existing one
fits: the scheduler promotes every existing reason by its own rule (a window
resets, a dependency ends), and nothing but a person should promote this one.
`MANUAL_PAUSE`, the obvious candidate, is promoted by
`Scheduler._promote_manual_pauses` once the pool it names is resumed, so a
hand-off parked under it would be re-admitted with nobody there to sign in.

### The change

```python
class ParkReason(str, Enum):
    ...
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
```

Promoted ONLY by swarm-api's `human-ready` route (lane L10); no sweep and no
timer promotes it. Past `human_park_max_seconds` (24 hours, Q4) a sweep
dead-letters it with `end_cause: human_wait_expired`. `RUNNING -> PARKED`,
`PARKED -> READY` and `PARKED -> DEAD_LETTERED` are already legal transitions,
so `_ALLOWED` does not change.

The non-frozen follow-ups, in the same pull request, because the contract-parity
script and the unit tests hold every restatement of `ParkReason` to the enum:
`apps/swarm-ui/src/types.ts` (the union, `PARK_REASONS`, the
`PARK_NEEDS_A_PERSON` group, `REASON_COPY`), the pill words `PARK_WORD` in
`apps/swarm-ui/src/components/StatePill.tsx`, and the MCP
progress sentences (`swarm_mcp.progress._PARKED_BECAUSE`,
`swarm_mcp.compact._WAITS_FOR`).

### Rollback

Delete the member and its restatements. A task document parked under it
would then decode to an unknown reason, so roll back only when none is
parked.

### What it would break

Nothing that exists: a new member. A reader with an exhaustive table of park
reasons is held to the enum by the parity script or a test, and gets its row
in this change.

### Invariants

**1**: a `PARKED(HUMAN_REQUIRED)` agent holds no lease and creates no demand;
`PARKED` is in `PENDING_STATES` and the capacity sets are unchanged. **4**:
this is the reason the park exists. 2, 3 and 5-10 unchanged.

---

## 69. `states.py`: the four `human_*` hand-off `EventType` members (LB-B)

**Status:** ACCEPTED by the owner 2026-10-11 (#1030), and APPLIED by the pull
request that adds this entry (lane LB-L0).

### Why

docs/design/live-browser.md sections 4.1 and 4.2: the hand-off has four
moments a reader of the event timeline must be able to tell apart -- the agent
asked, a person took control, control ended, the wait is over -- and the agent
stays `RUNNING` throughout (Q2), so the state history cannot show them.

### The change

```python
class EventType(str, Enum):
    ...
    HUMAN_HANDOFF_REQUESTED = "human_handoff_requested"
    HUMAN_CONTROL_TAKEN = "human_control_taken"
    HUMAN_CONTROL_RETURNED = "human_control_returned"
    HUMAN_HANDOFF_RESOLVED = "human_handoff_resolved"
```

Written by swarm-api (lanes L9 and L10). Control ending without a Return press
(renewal lapsed, idle, preempted, membership lapsed, cancelled, hold expired)
is `human_control_returned` with its cause in `detail`, not a fifth type.

### Rollback

Delete the four members. Events already stored with these values would then
be read as unknown types by any reader that decodes strictly.

### What it would break

Nothing: new members, written by code that does not exist yet.

### Invariants

None touched: an event is a record, and creates no demand and holds nothing.

---

## 70. `models.py`: `Task.human_wait`, the hand-off a holding task waits on, as a field and not a new state (LB-C)

**Status:** ACCEPTED by the owner 2026-10-11 (#1030; Q2 answered "the
field"), and APPLIED by the pull request that adds this entry (lane LB-L0).
The alternative the design offered, a `TaskState.WAITING_FOR_HUMAN` in
`CONCURRENCY_STATES`, was NOT taken and is not applied.

### Why

docs/design/live-browser.md section 4.3, "Why a field and not a new task
state". While the pod holds, the console and `sc status` need the reason, the
expected site, the deadline and who is in control. A new state would have had
to join `CONCURRENCY_STATES` and gain transitions, and every reader that asks
"is this attempt running?" -- the reconciler's heartbeat and stale-generation
checks, the worker's fence checks, release, the UI's state lists, this
repository's parity script -- would have had to learn it; one that was missed
would reclaim a holding agent or oversubscribe its pool. The field carries the
same information and changes no reader that does not want it.

### The change

```python
@dataclass
class Task:
    ...
    human_wait: dict[str, Any] | None = None
```

Keys: `reason` (at most 200 characters, redacted before it is stored),
`expect` (a host `url_refusal` accepts, or None), `requested_at`, `deadline`,
`controller` (None, or `{email, since, renew_by}`). None when nothing is
waiting, and on every document written before it, which therefore decodes
exactly as it did. WRITTEN BY swarm-api ONLY, from a worker route signed with
the attempt key; never accepted from a caller. Not in the spec signature: it
is attempt state, not the task's spec, and changes during the attempt. The
shape is not validated in `__post_init__`, so a document a later writer
extends is never refused on decode; swarm-api, its only writer, validates it.

Both codecs (`swarm_api.codec`, `scheduler.codec`) decode it here -- a dict,
or None for anything else -- so a decode-and-rewrite never clears a live
hand-off. `task_to_api` does not serve it: lane L10, which writes the field,
decides its public shape.

### Rollback

Delete the field. Nothing reads it until lane L10 lands.

### What it would break

Nothing: an optional field with a None default.

### Invariants

**1, 3**: a holding agent is `RUNNING`, holds its lease and is counted from
`LEASED` like any other attempt; no state was added, so no capacity set
changed. 2 and 4-10 unchanged (4 is kept by the bounded hold and request 68).

---

## 71. `profiles.py`: `RunnerProfile.live_browser`, True only on `claude-code-browser`, valid only on a GKE_AUTOPILOT profile (LB-D)

**Status:** ACCEPTED by the owner 2026-10-11 (#1030), and APPLIED by the pull
request that adds this entry (lane LB-L0).

### Why

docs/design/live-browser.md sections 3 and 8.1. Whether an agent's pod runs the
display sidecar, and whether swarm-api will open a live session for it, has to
be a property of the catalogue: a caller names a profile and nothing else
(invariant 10), and lane L5's dispatcher and lane L9's API both need one place
to read it.

### The change

```python
@dataclass(frozen=True)
class RunnerProfile:
    ...
    live_browser: bool = False

    def __post_init__(self) -> None:
        ...
        if self.live_browser and self.backend is not Backend.GKE_AUTOPILOT:
            raise ValueError(...)
```

and `live_browser=True` on `claude-code-browser`, the only profile that sets
it. The check is on the profile's STATED backend, so `AUTO` is refused too:
`resolve_backend` sends the `browser` class (8 vCPU, 16 GiB) to Cloud Run
Jobs, where the sidecar and its second uid do not exist, and a profile whose
hand-off cannot work must not resolve there silently. For the same reason
`claude-code-browser` must stay out of the Cloud Run fallback list
(`cloud_run_fallback_profiles`, `terraform/infra/locals.tf`).

### Rollback

Delete the field, the check and the one `live_browser=True`. Nothing reads it
until lanes L5 and L9 land.

### What it would break

Nothing: a defaulted field. `claude-code-browser` now differs from
`claude-code` in four fields, not three (request 67).

### Invariants

**10**: a caller still names a profile; whether it is live is the catalogue's,
and there is no input, field or parameter by which a caller asks for a live
browser on another profile. 1-9 unchanged.

---

## 72. `profiles.py`: the `post-verdict` and `claude-code-review` comments name per-tenant accounts that no longer exist

**Status:** ACCEPTED by the owner 2026-10-11, applied in this PR. Filed
2026-10-11 by the stale-name audit lane. A comment fix only; no field changes.

### What is true today

`apps/common/swarm_common/profiles.py` says, in the `"post-verdict"` entry,
that its secret is read "as `swarm-<tenant>-post-verdict`, the Job's own
service account", and, above `"claude-code-review"`, that the profile has its
own "Job and service account, `swarm-<tenant>-review`". Neither account
exists: `terraform/infra/variables.tf` (the retired #295 App keys validation)
records that the per-tenant merge and post-verdict accounts are gone, and
docs/merge-step.md §3 "What this supersedes" lists the `-merge`,
`-post-verdict` and `-review` accounts as superseded. Both profiles are
`available=False` with no Job for any tenant (`profiles_without_a_job`).

### Why

A reader of the frozen catalogue is told an identity exists that no
Terraform creates. The docs that said the same were marked as history in the
same change that files this request; the frozen file cannot be edited by a
lane.

### The requested change

Comments only, in `apps/common/swarm_common/profiles.py`:

* `"post-verdict"`: replace "as `swarm-<tenant>-post-verdict`, the Job's own
  service account" with a note that the per-tenant post-verdict account was
  retired with the #295 design (docs/merge-step.md §3) and the profile has no
  Job;
* above `"claude-code-review"`: drop "and so its Job and service account,
  `swarm-<tenant>-review`" (the profile has no Job and that account is gone).

### What it would break if accepted

Nothing: no value, type or field changes, so no component written against the
contract sees a difference.

### If it is declined

The two comments keep naming accounts that do not exist; the docs say they
are history.

### Invariants

None touched: a comment change in the catalogue alters no admission, lease,
fencing, checkpoint or isolation behaviour.

---

## 73. `models.py`: `Task.start_problem`, why the current attempt's pod is not starting (F1-POD)

**Status:** REQUESTED 2026-10-12 by lane F1-POD. Not applied: the record
lives in `metadata.start_problem` until this is decided, which works today
and changes no frozen type.

### What is true today

On 2026-10-11 between 20:58 and 21:05 PDT every claude-code-browser pod's
`livegate` sidecar crash-looped for about three minutes while the agent read
only "dispatched". The reconciler now reads the pod of a DISPATCHED or
STARTING agent and records the worst container -- `{container, reason,
restarts, message (<= 300 chars), pod_started, attempt_id, seen_at}` -- as
`metadata.start_problem` (`reconciler.startproblem`,
`ControlStore.record_start_problem`). swarm-api reserves the key
(`validation.START_PROBLEM_METADATA_KEY`), masks every string in it in both
copies it serves, and lifts it to a top-level `start_problem` on the agent
read (`codec.start_problem_to_api`), as `dispatch` is lifted from
`metadata.dispatch`.

### Why a field would be better

`metadata` is a map several writers rewrite whole (the worker's refund
counters, swarm-api's hand-off marks, the reconciler's eviction counter), so
each of them has to carry this key through untouched, inside a transaction;
a writer that rebuilt `metadata` from a decoded copy taken before the
reconciler's write would drop it. A field is not in that map.

### The requested change

```python
@dataclass
class Task:
    ...
    start_problem: dict[str, Any] | None = None
```

Same keys as above. Written ONLY by the reconciler; never accepted from a
caller; not in the spec signature (attempt state, written during the
attempt). Not validated in `__post_init__`, as `human_wait` is not.

### What it would break if accepted

Nothing: an optional field with a None default. The reconciler and swarm-api
would read the field first and `metadata.start_problem` second for one
release, then the metadata key would be retired from
`RESERVED_METADATA_KEYS`.

### If it is declined

The record stays in `metadata.start_problem`, which is what ships.

### Invariants

**1-3** unchanged: it is a record, not a state; nothing that writes it moves
a state, a lease or a pool. **9**: it is on the tenant's own agent and served
only through the tenant-scoped task routes, masked. 4-8 and 10 unchanged.

---

## 74. `states.py`: `EventType.STARTUP_PROGRESS`, the browser agent's start-up steps (STARTUP-API)

**Status:** ACCEPTED by the owner 2026-10-13 ("we should put a progress bar
when the browser agent is starting up the browser so you get a sense of
progress as it comes up"; detail "Stages + real pod steps", carried as "One
new event type"), and APPLIED by the pull request that adds this entry (lane
STARTUP-API).

### Why

A `claude-code-browser` attempt spends a minute or more between `dispatched`
and a first picture in the Browser tab: the pod is scheduled, the image is
pulled, the `display` sidecar launches Chromium and waits for CDP, the worker
container starts, and only then does the live stream paint. The agent's state
history shows none of it -- the agent reads DISPATCHED, then STARTING, then
RUNNING -- so the console had nothing to draw a progress bar from. The steps
are moments in one attempt, not states (the agent does not change state for
any of them), so they are events, as the hand-off moments of entry 69 are.

### The change

```python
class EventType(str, Enum):
    ...
    STARTUP_PROGRESS = "startup_progress"
```

`detail` is `{"step": <step>, "attempt_id": ..., "generation": ...}`, the
steps in order `pod_scheduled`, `image_pulling`, `image_pulled`,
`container_started` (written by the reconciler from the GKE pod's status,
`reconciler.startsteps`), then `browser_launching`, `browser_ready`,
`first_frame` (written by the worker from the live gate's `status`,
`agent_worker.handoff.HandoffPath.note_startup`). Each step at most once per
attempt; a step from a generation that is no longer the agent's is not
written. One type with the step in `detail`, not seven types: the existing
`lease_acquired`, `dispatched`, `starting` and `running` events stay as they
are and are the bar's first stages. swarm-api serves the steps of the current
attempt with a per-profile estimate at `GET /v1/tasks/{id}/startup`.

### Rollback

Delete the member. Events already stored with this value would then fail to
decode in `swarm_api.codec.event_from_dict` (`EventType(data["type"])`), so a
rollback must first delete the stored `startup_progress` events, or ship a
decoder that skips unknown types.

### What it would break

Nothing: a new member, written by code that ships with it. No reader matches
every type exhaustively (`reconciler.progress` reads named types only, and
the stuck rule's clock starts at `starting`/`running`/`checkpoint_restored`,
never at this). No shell, jq or TypeScript copy of `EventType` exists to keep
in step (`scripts/lib/check-contract-parity.sh` restates none; the console's
`TaskEvent.type` is a plain string).

### Invariants

**1-3** untouched: an event is a record; writing it creates no demand, holds
nothing and moves no state, lease or pool. **5**: the reconciler writes a pod
step only inside a transaction that finds the agent still on the lease and
generation the pod was read for, and the worker writes only while it is not
fenced; the route serves only the current attempt's generation. 4, 6-10
unchanged.

## 75. `models.py`: `Workflow.fix_rounds` and `WorkflowStep.spawned_by` (SPEED-D2)

**Status:** proposed, and worked around without it by the pull request that
adds this entry (lane SPEED-D2, owner decision 2026-10-14 "Auto fix rounds").
Nothing under `apps/common/swarm_common/` was edited.

### Why

A lane workflow's merge step now spends up to `fix_rounds` agent rounds (2 by
default) on a red or conflicting pull request, and each round is shown as a
step of the lane's workflow (docs/merge-step.md "Revised 2026-10-14"). Two
facts about that have no home in the frozen `Workflow`:

1. **The budget.** `Workflow` carries no metadata, so the spec's `fix_rounds`
   is stored only on the merge AGENT (`metadata.merge_fix_rounds`, which the
   wake tick and the worker read). A reader of the workflow alone cannot say
   how many rounds it was given.
2. **Which steps the platform appended.** A round's step is an ordinary
   `WorkflowStep`. The rollup must not let a round's FAILED fail a workflow
   whose merge then succeeded, so it has to tell a round from a caller's step.
   Today it does so by the step id's shape, `ci-fix:<n>` / `conflict-fix:<n>`
   (`swarm_rollup.FIX_ROUND_STEP_RE`), which works only because a caller's
   step id may not hold `:` (`WorkflowStepCreate.step_id`'s pattern). The
   input could not carry the mark: the reconciler decodes steps without it.

### The change

```python
@dataclass
class WorkflowStep:
    ...
    #: The step id of the platform step that appended this one (a merge
    #: step's fix round names the merge step), or None for a submitted step.
    spawned_by: str | None = None

@dataclass
class Workflow:
    ...
    #: Agent fix rounds the merge step may spend on a red pull request;
    #: None when the workflow has no merge step.
    fix_rounds: int | None = None
```

`swarm_rollup.is_fix_round_step` would then read `spawned_by`, and the
step-id shape would stop carrying meaning.

### Rollback

Delete the two fields; both default to None, and the workaround above is
what would remain.

### What it would break

Nothing: two defaulted fields. `codec.workflow_from_dict` and
`reconciler.model.WorkflowView.from_doc` would have to decode `spawned_by`
for the rollup to read it; until they do, the step-id rule stays.

### Invariants

**1-3** untouched: a field records a decision; a round is an ordinary agent
admitted like any other. 4-10 unchanged.

## 76. `models.py`/`states.py`: `Workflow.after` and `ParkReason.PULL_REQUEST_PENDING` (SPEED-D3)

**Status:** proposed, and worked around without it by the pull request that
adds this entry (lane SPEED-D3, owner decision 2026-10-14, delivery speed
plan D3). Nothing under `apps/common/swarm_common/` was edited.

### Why

A workflow spec may now say `"after": {"pr": 41}` (or a list): no step starts
until each named pull request of the workflow's own repository has merged,
and one closed without merging fails the workflow (docs/workflows.md
"Waiting for a pull request: `after`"). Two facts about that have no home in
the frozen contract:

1. **What the workflow waits for.** `Workflow` carries no metadata, so the
   numbers live on each ROOT step's agent as `metadata.after_prs`
   (`swarm_api.prgate`), with the gate's state (`waiting`, `merged`,
   `closed`). A reader of the workflow alone cannot say what it waits for;
   the API reads it back off the step agents (`codec.workflow_to_api`'s
   `after_pull_requests`).
2. **Why a root step is parked.** No `ParkReason` says "waiting for a pull
   request", so a gated root is parked on `DEPENDENCY_INCOMPLETE` with no
   `depends_on`. Every reader that promotes that reason had to learn the
   gate: the scheduler's dependency sweep (`scheduler.loop.after_prs_hold`)
   and the reconciler's DEPENDENCIES_MET rule
   (`reconciler.model.held_for_pull_requests`), which would otherwise have
   promoted a root that "depends on no step" after its grace. Each restates
   the key and the `merged` state, held equal by
   tests/unit/control_plane/test_workflow_after_pr.py. A third promoter added
   later without that check would start a held workflow.

### The change

```python
# states.py
class ParkReason(str, Enum):
    ...
    #: A workflow submitted with `after` waits for pull requests of its own
    #: repository to merge. Marked by swarm-api's wake tick, promoted by the
    #: scheduler on the mark; never promoted by the dependency rule.
    PULL_REQUEST_PENDING = "PULL_REQUEST_PENDING"

# models.py
@dataclass
class Workflow:
    ...
    #: Pull request numbers of the workflow's repository that must merge
    #: before any step starts; empty for a workflow without `after`.
    after: list[int] = field(default_factory=list)
```

The gate's state would stay on the root agents (it is written per agent, in a
guarded transaction, by the tick); only the park reason and the declaration
move.

### Rollback

Delete the reason and the field. Every park written with the reason would
need rewriting to `DEPENDENCY_INCOMPLETE` first, which is the workaround
above.

### What it would break

A new `ParkReason` value is read by every surface that names park reasons:
the console's state pills and blockers (`apps/swarm-ui/src/types.ts`), the
bridge's progress text (`swarm_mcp.progress`, `swarm_mcp.compact`) and
`test_every_park_has_an_unparker.py`, which requires a sweep for each. The
field is defaulted and decodes old documents unchanged.

### Invariants

**1** kept either way: a gated root is PARKED, holding no lease, no pool
count and no execution. **3** untouched: nothing is counted before LEASED.
**9**: the tick reads with the named tenant's own `-git` token, only that
tenant's parks. 2, 4-8 and 10 unchanged.

---

## 77. `profiles.py`: `claude-code-browser` declares the input `browser_tool` (BSTATUS-API)

**Status:** ACCEPTED by the owner 2026-10-14 ("Picker on Submit, Auto by
default"), APPLIED by this PR (lane BSTATUS-API, part of #1030).

### Why

The live-browser agent can drive its browser with three tools: the
Playwright MCP server (`browser_*` tools), the dev-browser CLI (DEVB-INSTALL)
and the jev-browser MCP server (JEV-IMAGE, JEV-RUNNER). Each is better at
something different, and jev sends page text to TypeSafe, which a person may
not want for a given agent. The owner's decision: the Submit form offers
"Browser tool: Auto / Playwright / dev-browser / Jev", Auto by default. What
a caller may send a runner is the catalogue's declaration (contract request
25), so the choice has to be declared here or the API refuses it as an
undeclared key.

### The change

In `apps/common/swarm_common/profiles.py`, `claude-code-browser` takes
`_CLAUDE_CODE_BROWSER_INPUTS` instead of `_CLI_AGENT_INPUTS`: claude-code's
inputs plus one key.

```python
_CLAUDE_CODE_BROWSER_INPUTS: dict[str, RunnerInput] = {
    **_CLI_AGENT_INPUTS,
    "browser_tool": RunnerInput(
        "string",
        choices=("auto", "playwright", "dev-browser", "jev"),
        means="the live browser's tool: auto (every tool available; the default), ...",
    ),
}
```

Absent means `auto`. Nothing else in `swarm_common` changes.

The non-frozen follow-ups, in the same pull request: the runner's mapping
from name to registration (`agent_worker/browser_tools.py`,
`runners/claude_code.py`); the API's refusal of `jev` when no TypeSafe key
resolves or jev is not installed (`browser_tool_unavailable`, behind its
switch); the client tag the three wrappers put on the CDP endpoint and the
gate's attribution; the `browser` object on the live-session read; and a
section in `scripts/lib/check-contract-parity.sh` that holds every
restatement of the four names (the runner's, the API's, the gate's tags and
the wrappers') to these choices.

### Rollback

Put `inputs=_CLI_AGENT_INPUTS` back. An agent stored with `browser_tool`
keeps it in its input; the runner reads only the names it knows and falls
back to `auto` for anything else, so an old agent still runs.

### What it would break

Nothing that exists: a new optional key on one profile. The Submit form's
`SUGGESTED` table may now offer it (section 13 of the parity script allows
any declared key). A caller who already sent `browser_tool` was refused with
422 `invalid_input`; now it is accepted.

### Invariants

**10** is the point: a caller names one of four options, never a command, a
binary, an MCP config or a URL; the image's wrappers and the runner own what
each name runs. 1-9 unchanged: the same profile, image, class, backend and
secrets.


## 78. `models.py` / `states.py`: a workspace's merge mode, and a merge-train park reason (TRAIN-WS)

**Status:** proposed, and worked around without it by the pull request that
adds this entry (lane TRAIN-WS, owner decision 2026-10-14: "the merge train
should be a setting users manage on their own workspace and admins on all
workspaces"). Nothing under `apps/common/swarm_common/` was edited.

### Why

A workspace now chooses how its lane pull requests land -- directly, or in
merge trains (docs/merge-trains.md). Two facts about that have no home in
the frozen contract:

1. **The setting.** A workspace is a `Tenant`, which is frozen and has no
   field for its merge mode, train size or max train age. They are stored in
   swarm-api's own `workspace_settings/{tenant_id}` (field `merge`, beside JUDGE-SELECT's `judgment_backend`) (`swarm_api.mergesettings`),
   and a repository's override in its registration (`repositories/{id}`
   `merge_mode`, swarm-api's own registry, not the contract). Every reader
   goes through `mergesettings.settings_of`, which reads a malformed value as
   the default, so nothing breaks; but a tenant's merge policy is a property
   of the tenant that the contract cannot describe.
2. **The wait.** A merge step whose pull request is registered for a train
   parks `CI_PENDING` with the code `merge_train_waiting`, as a step waiting
   for the merge slot parks `CI_PENDING` with `merge_slot_wait` (request 62).
   It is not waiting for CI: its checks were green when it registered.
   Both the CI wake tick and anything that reads park reasons must know the
   code to tell the two apart.

### The change

```python
@dataclass
class Tenant:
    ...
    #: How this workspace's merge steps land their pull requests:
    #: "direct" (default) or "train"; and the train's shape.
    merge_mode: str = "direct"
    merge_train_size: int = 4
    merge_train_max_age_minutes: int = 90

class ParkReason(str, Enum):
    ...
    MERGE_TRAIN = "MERGE_TRAIN"   # waiting for its repository's merge train
```

### Rollback

Delete the fields and the member. The workaround above is what would remain.

### What it would break

Nothing: defaulted fields and a new enum member. `codec` would decode the
three fields; the scheduler's `_promote_ci_waits` would promote
`MERGE_TRAIN` parks on the same wake marker, and `mergewake` would stop
needing to skip a code.

### Invariants

**1** preserved: a waiting train and its parked steps hold no lease and no
pool count; only the coordinator's per-tenant tick reads them. **2-3**
untouched: a merge step is admitted like any other. **5** preserved: the
step's registration is a fenced transaction. 4, 6-10 unchanged.

## 79. `profiles.py`: the `browser` resource class shrinks from 8 vCPU / 16 GiB to 4 vCPU / 8 GiB, and from 2 units to 1 (MEASURE, BROWSER-CLASS)

**Status:** ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request
that adds this line. Accepted at about 18:10 PDT ("File the request now"), on
the measurement in [browser-startup-speed.md §11](browser-startup-speed.md),
with `units` kept at 2 as written below; amended by the owner at about 22:55
PDT ("Drop to 1") to `units=1`. Lane BROWSER-CLASS applied the one line, as
amended:

```python
    "browser": ResourceClass("browser", cpu=4, memory_gib=8, disk_gib=4, units=1),
```

### Amendment: `units` drops to 1 (owner, 2026-10-14 ~22:55 PDT)

The request as filed (below) kept `units` at 2 so that pool accounting would
not move with the resize, and called lowering it "a separate decision". The
owner took that decision the same evening: **"Drop to 1"**. A browser agent
now weighs what a `standard` one does, which is consistent with what it now
is: the same 4 vCPU / 8 GiB.

**Why it doubles browser concurrency per pool.** Admission reserves an agent's
`units` in every pool it requires, in one transaction (invariants 2 and 3),
and a pool admits while `active + units <= limit`. Every pool a browser agent
requires (`global`, `tenant:<t>`, `resource:browser`, `runner:<profile>`,
`backend:GKE_AUTOPILOT`, `provider:anthropic`, `provider:anthropic:tenant:<t>`)
therefore held `floor(limit / 2)` browser agents and now holds `limit`. A
`resource:browser` pool of 20, as `dev.tfvars` and `prod.tfvars` set it,
admitted 10 browser agents and now admits 20. No pool limit changed with this
request: the doubling is entirely the weight. `resource:browser` stays its
own pool, and is now the one ceiling that tells browser concurrency apart
from `standard`'s, since the two cost the same.

**What else it moved.**

* **The live display sidecar's split.** `LIVE_DISPLAY_SHARE["browser"]`
  (`apps/scheduler/scheduler/dispatch.py`) was display 5 vCPU / 10 GiB and
  worker 3 / 6, the design's starting split, which cannot fit 4 / 8. It is now
  display 3 vCPU / 5 GiB and worker 1 vCPU / 3 GiB, sized from §11's
  per-container peaks (display 1.90 cores / 1.48 GiB, worker 0.55 cores /
  0.37 GiB). Both containers keep `requests == limits` and sum to the class.
  The sidecar's resident tmpfs (`/dev/shm` 2, profile 1, `/tmp` 0.25 GiB)
  plus its measured peak is 4.73 of its 5 GiB, so a page heavier than any
  sampled, or a 1 GiB download in flight at that peak, is an OOM kill that
  checkpointing (invariant 8) bounds.
* **The GKE disk layout did not move.** `GKE_DISK["browser"]` stays
  4 + 2 + 2 = 8 GiB of disk-backed `emptyDir`s, outside the memory limit.
  `disk_gib` sizes Cloud Run's memory tmpfs; on GKE it was only ever equal to
  that layout's total by coincidence of the old numbers.
* **`scripts/pool-limit.sh`'s below-units warning** can no longer fire for
  `browser` (#66's case): a limit of 1 now admits one browser agent. No
  profile in the catalogue reaches a class above 1 unit, so the warning stays
  for `large` only.

**Invariants, as amended.** **7** preserved: request and limit stay equal on
every container. **1-3**: admission and reservation count units exactly as
before, all-or-nothing across every pool, from `LEASED`; what changes is the
number, 1 per browser agent instead of 2. **8** is what bounds an OOM's cost.
4, 5, 6, 9, 10 unchanged.

### As filed (2026-10-14 ~18:10 PDT, before the amendment)

### Why

Both profiles on the `browser` class (`browser` and `claude-code-browser`)
are given 8 vCPU and 16 GiB as request and limit, because requests == limits
(invariant 7). On 2026-10-15 at 01:00 UTC, while the live-browser QA matrix
was running, eight `kubectl top` samples of four `browser` pods peaked at
**2.13 cores and 1.85 GiB** for the whole pod. The display sidecar peaked at
1.90 cores and 1.48 GiB, the worker at 0.55 cores and 0.37 GiB. That is under
80 % of 4 vCPU / 8 GiB. Every browser agent is therefore paying for about
four times the CPU and eight times the memory it was seen to use.

### The change

```python
RESOURCE_CLASSES = {
    ...
    "browser": ResourceClass("browser", cpu=4, memory_gib=8, disk_gib=4, units=2),
}
```

The class keeps its name, so neither profile changes. The workspace is a
tmpfs carved out of memory (see the comment above `RESOURCE_CLASSES`), so
`disk_gib` drops with it: 4 GiB of tmpfs leaves 4 GiB for processes, against
a measured peak of 1.85 GiB. **`units` stays 2** in this request, so pool
accounting (invariants 2 and 3) does not move with the resize. Lowering it
to 1 would admit twice as many browser agents into the same pools, and that
is a separate decision.

### Rollback

Restore `cpu=8, memory_gib=16, disk_gib=8`. A running pod keeps the size it
was created with; the next dispatch takes the restored size.

### What it would break

* **A page heavier than any sampled.** Eight samples 15 s apart miss peaks
  between them, and four pods are not a load test. A browser agent that
  needs more than 8 GiB, or a download filling the 4 GiB tmpfs, is now
  OOM-killed or out of space where before it was not. The worker's
  checkpointing (invariant 8) bounds the loss to the last checkpoint.
* **The Cloud Run path.** `ResourceClass` validates against the Cloud Run
  Jobs ceiling; 4/8 is inside it, so nothing there is refused.
* **Tests that pin the catalogue's numbers** (`tests/unit` asserting the
  `browser` class's cpu or memory) must change with it; the lane names them.

### Invariants

**7** preserved: request and limit stay equal, only smaller. **1-3**
unchanged: `units` is kept, so admission and reservation count the same.
**6** unchanged (no Spot). **8** is what limits an OOM's cost. 4, 5, 9, 10
unchanged.


## 80. `profiles.py`: a runner profile declares the secret slots it may consume, and the vocabulary of secret kinds (SEC-CONTRACT, workspace secrets request A)

**Status:** ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line. Filed by lane SEC-CONTRACT, phase 0 of
[docs/design/workspace-secrets.md](design/workspace-secrets.md) (§7 request A),
part of #1347. Proposed 2026-10-14 (workspace secrets, owner decisions
2026-10-14), accepted on PR #1354 at about 23:20 PDT.

### Why

A workspace secret is granted to a *consumer* by checkbox (design §2.2), and
a consumer takes it through a *slot*: a named place with the kinds of secret
it accepts and whether the consumer can run without it (§2.4). Three readers
must agree on one declaration of a profile's slots:

1. **The console** renders the grant matrix and disables a cell whose slot
   cannot take the secret's kind ("jev-browser reads an API key").
2. **swarm-api** refuses a grant to a slot the consumer does not have, or of
   a kind the slot refuses (422, SEC-STORE-API), and decides at release time
   which slot each released secret lands in (§3.3 step 4).
3. **The worker** refuses any released item whose slot its own catalogue does
   not have (§3.4, the defence in depth kept under option B), and parks an
   agent whose `required` slot has no enabled grant as `CREDENTIAL_MISSING`
   before it leases (§4.2).

Today a profile declares only `secrets`, the env-var names of its runner's
provider key (`RunnerProfile.secrets`, which stays as it is: §7 "Not
requested", §6.3). There is nowhere to say "this profile takes arbitrary
workspace files" or "this slot must be filled". Restated in each reader, the
three copies drift, which is exactly what request 25 did for `inputs`: one
declaration, read by swarm-api and the plugin.

The kind vocabulary belongs with it. `SecretSlot.kinds` names kinds, and a
kind spelt `api-key` in the console and `api_key` in the worker would make
every grant refuse silently. The design's kinds (§2.1) are `api_key`,
`bearer_token`, `basic_auth`, `oauth_client` and `opaque`, plus
`forge_token` for the read-only legacy forge rows (§6.2), which no slot but
`component:git`'s accepts.

### The change

```python
#: Contract request 80 (workspace secrets, docs/design/workspace-secrets.md
#: §2.1): the kinds a workspace secret can be. A kind decides the value's
#: SHAPE (basic_auth is a JSON object of exactly username and password,
#: oauth_client of exactly client_id and client_secret) and which slots may
#: take it; never behaviour. `forge_token` is listed so a forge row has a
#: kind, and is accepted by no profile or tool slot (owner rule 2026-09-25:
#: a forge token is never readable by the agent).
SECRET_KINDS: tuple[str, ...] = (
    "api_key", "bearer_token", "basic_auth", "oauth_client", "opaque", "forge_token",
)
_SLOT_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class SecretSlot:
    """A place a consumer takes a workspace secret (design §2.4).

    The slot's NAME is the key it is declared under and becomes a directory
    (`private/secrets/<slot>/<name>`, design §4.1), so it is a short
    lower-case word. A grant names one slot; a release lands each secret in
    its grant's slot and nowhere else."""

    kinds: tuple[str, ...]
    means: str
    #: True when the consumer cannot run without it: the task parks
    #: CREDENTIAL_MISSING before it leases, at no cost (invariant 1), and
    #: starts by itself once a grant is ticked. False (the default) starts
    #: without it.
    required: bool = False

    def __post_init__(self) -> None:
        if not self.kinds:
            raise ValueError("a secret slot accepts at least one kind")
        unknown = [k for k in self.kinds if k not in SECRET_KINDS]
        if unknown:
            raise ValueError(f"secret slot: unknown kind(s) {unknown}")
        if "forge_token" in self.kinds:
            raise ValueError(
                "secret slot: a forge token is never granted to a profile or a tool "
                "(contract request 80)"
            )
        if not self.means.strip():
            raise ValueError("secret slot: `means` says what the consumer does with it")


#: The slot every agent profile declares: any non-forge kind, one 0600 file
#: per secret, listed by name in the manifest the runner is pointed at.
_FILES_SLOT = SecretSlot(
    kinds=("api_key", "bearer_token", "basic_auth", "oauth_client", "opaque"),
    means="one file per granted secret under private/secrets/files/, named in "
          "the manifest at $SWARM_SECRETS_MANIFEST; never in the environment",
)


@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: Contract request 80: the workspace-secret slots this profile's
    #: attempts may be released into, by slot name. Empty (the default)
    #: means a grant to `profile:<name>` is refused at write time. NOT the
    #: provider key (`secrets` above, unchanged). Excluded from the hash, as
    #: `inputs` is: a mapping is not hashable.
    secret_slots: Mapping[str, SecretSlot] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        ...
        if self.worker_action is not None:
            ...
            if self.secret_slots:
                raise ValueError(
                    f"runner {self.name}: a worker_action profile runs no agent and "
                    "takes no workspace secret (contract request 80)"
                )
        for slot, declared in self.secret_slots.items():
            if not _SLOT_NAME.fullmatch(slot):
                raise ValueError(f"runner {self.name}: secret slot {slot!r} is not a slot name")
            if not isinstance(declared, SecretSlot):
                raise ValueError(f"runner {self.name}: secret slot {slot!r} is not a SecretSlot")


RUNNER_PROFILES = {
    # Every profile that runs an agent declares `files`; none is required.
    "claude-code":         RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    "codex":               RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    "browser":             RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    "claude-code-review":  RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    "indexer":             RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    "claude-code-browser": RunnerProfile(..., secret_slots={"files": _FILES_SLOT}),
    # mock, generic, merge, post-verdict: no slots.
}
```

**Two corrections to the design's sketch, found by reading the catalogue at
`78fab43`:**

* **`browser` does not reach `typesafe` "through its tools".** §7 says
  `claude-code-browser` and `browser` declare `typesafe` through request B's
  tools. Only `claude-code-browser` declares the `browser_tool` input
  (`_CLAUDE_CODE_BROWSER_INPUTS`, request 77). `browser` runs
  `agent_worker.runners.browser`, a scripted runner driven by `url` and
  `actions` (`_BROWSER_INPUTS`) that launches none of the three tools. So
  `browser` declares `files` only, and no tool consumer is ever derived for
  it (request 81). Request 81 is the only place `typesafe` is declared.
* **`mock` and `generic` declare nothing.** The design's consumer list
  (§2.3) leaves them out without saying why. `mock` exists so the smoke
  path works before a tenant registers anything (`provider=None`).
  `generic` runs one of six fixed commands (`_GENERIC_COMMANDS`) with no
  agent to read a manifest, so giving it secrets would hand files to a
  process that cannot use them by name.

### Rollback

Delete `SecretSlot`, `SECRET_KINDS`, `_FILES_SLOT`, the `secret_slots`
field and its checks. Nothing in the contract reads them. A reader built
on them (SEC-STORE-API, SEC-RELEASE) then refuses every grant to a profile
as "no such slot", which fails closed: no secret is released.

### What it would break

* **Nothing that exists.** It is a defaulted field and a new class. `codec`
  does not serialise `RunnerProfile`. `hash=False` keeps profiles hashable,
  as `inputs` does.
* **The profile catalogue's mirrors.** The plugin's and UI's copies of the
  catalogue (the parity checks behind `scripts/lib/check-contract-parity.sh`)
  do not carry slots, and need not until SEC-UI renders the matrix, which
  reads `GET /v1/workspace/secrets/consumers` rather than a mirror (§2.3).
* **Tests that construct a `worker_action` profile with arbitrary fields**
  keep passing: a default-empty mapping trips no check.
* **The red tests §8 names for this lane:** a `worker_action` profile with
  a slot raises; a slot with an unknown kind or with `forge_token` raises.

### Invariants

**10** is the reason for the shape. A slot is a catalogue property. A
caller names a profile and cannot add a slot, an image, a path or a
command. **9** preserved: a slot says *where* a secret lands, never *whose*;
which workspace's secrets fill it is decided by the release's
`tenant_id == task.tenant_id` bound (§3.3), not here. **1** preserved: a
`required` slot with no grant parks before the lease, holding nothing.
**5** unchanged: the release that fills slots is fenced on the generation
(request 82 and SEC-RELEASE), and a slot declaration fences nothing.
2-4 and 6-8 untouched.

## 81. `profiles.py`: `RUNTIME_TOOLS`, the catalogue of runtime tools, their secret slots and the `browser_tool` values that launch them (SEC-CONTRACT, workspace secrets request B)

**Status:** ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line. Filed by lane SEC-CONTRACT (design §7 request B), part of #1347.
Proposed 2026-10-14, accepted on PR #1354 at about 23:20 PDT. The
import-time checks are `_check_runtime_tools`, called on the catalogue.

### Why

The owner's own example is a grant to a tool: "only allow access to it for a
specific runtime job type and image like jev-browser". A grant to
`tool:jev-browser` has to mean the same tool to swarm-api, which derives an
attempt's consumers from its signed spec (§2.3), and to the worker, which
re-checks every released item against its catalogue (§3.4). Today the tool
names exist in three spellings and none is a catalogue:

* the frozen `browser_tool` input's choices on `claude-code-browser`:
  `auto`, `playwright`, `dev-browser`, `jev` (request 77);
* the worker's `agent_worker.browser_tools.TOOLS = ("playwright",
  "dev-browser", "jev")`, outside the contract;
* the design's consumer names, `jev-browser`, `dev-browser`, `playwright`.
  `jev` is spelt `jev-browser` there because that is the program the secret
  reaches.

This makes it the tool-level checkbox the design recommends option B for
(§3.6): an attempt with `browser_tool: playwright` is **not** released the
jev-browser key. That rule is only as good as the mapping from a signed
`browser_tool` value to the tools it launches, so the mapping has to be
frozen too, not left for each reader to restate. `auto` launches all three
(request 77's `means`).

### The change

```python
#: Contract request 81 (workspace secrets, design §2.3): a program the
#: platform launches INSIDE an attempt that can be granted a workspace
#: secret on its own. A tool is a NAME, never an image or a command
#: (invariant 10): the agent can start any process in its pod, so a grant
#: is matched against the signed spec's `browser_tool`, never against a
#: process name.
@dataclass(frozen=True)
class RuntimeTool:
    name: str
    #: The profiles whose attempts can launch this tool.
    profiles: tuple[str, ...]
    #: The `browser_tool` input values that launch it, on those profiles.
    #: An attempt whose signed value is not listed never launches it, and is
    #: never released a secret granted only to it.
    launched_by: tuple[str, ...]
    secret_slots: Mapping[str, SecretSlot] = field(default_factory=dict, hash=False)


RUNTIME_TOOLS: Mapping[str, RuntimeTool] = MappingProxyType({
    "jev-browser": RuntimeTool(
        name="jev-browser",
        profiles=("claude-code-browser",),
        launched_by=("auto", "jev"),
        secret_slots={"typesafe": SecretSlot(
            kinds=("api_key",),
            means="the TypeSafe key jev_launch.py puts in the jev process's own "
                  "environment; jev sends page text to TypeSafe",
        )},
    ),
    "dev-browser": RuntimeTool(
        name="dev-browser", profiles=("claude-code-browser",),
        launched_by=("auto", "dev-browser"),
    ),
    "playwright": RuntimeTool(
        name="playwright", profiles=("claude-code-browser",),
        # The fallback under every choice (request 77's `means`).
        launched_by=("auto", "playwright", "dev-browser", "jev"),
    ),
})


def tools_launched(profile: RunnerProfile, inputs: Mapping[str, Any]) -> tuple[str, ...]:
    """The RUNTIME_TOOLS an attempt of `profile` with signed `inputs` launches.

    `browser_tool` absent is `auto` (request 77). Empty for a profile that no
    tool lists. Read by swarm-api's release and by the worker's re-check, so
    the two derive the same set."""
    selected = inputs.get("browser_tool", "auto")
    return tuple(
        t.name for t in RUNTIME_TOOLS.values()
        if profile.name in t.profiles and selected in t.launched_by
    )
```

`jev-browser` with no `typesafe` grant still launches as today. Its slot is
optional, which keeps `browser_tools.choose`'s `no_key` path, where jev is
not registered and Playwright is used. A tool slot is never `required`,
because the profile runs without the tool.

**Import-time checks** (in the module, where the catalogue's other
cross-checks are). Every name in a tool's `profiles` is a key of
`RUNNER_PROFILES` that declares `browser_tool`. Every `launched_by` value
is one of that input's choices. Every `browser_tool` choice except `auto`
is in some tool's `launched_by`. That last check is the red test §8 names
("every `browser_tool` choice but `auto` maps to a `RUNTIME_TOOLS`
entry"): it fails the import the day a fifth choice is added without a
tool.

### Rollback

Delete `RuntimeTool`, `RUNTIME_TOOLS`, `tools_launched` and the import-time
checks. A release built on them then derives no `tool:` consumer, so a
secret granted only to a tool is released to nobody (fail closed), and jev
falls back to today's read through `jevkey` while the §6.1 switch is off.

### What it would break

* **Nothing that exists.** These are new names. `browser_tools.TOOLS` in
  the worker keeps the short spelling `jev`, the input value. The consumer
  name `jev-browser` is the catalogue key, and `launched_by` is the bridge
  between the two. A worker lane may later read `RUNTIME_TOOLS` instead of
  its own tuple; this request does not require it.
* **Adding a `browser_tool` choice** now needs a `RUNTIME_TOOLS` edit in the
  same change, or the import fails. That coupling is deliberate.
* **A spelling decision for the owner.** The consumer is `tool:jev-browser`
  while the input value is `jev`. The design chose the program's name. If
  the owner prefers one spelling, the cheaper change is to key the
  catalogue `jev`, since the input is already frozen and filed in task
  documents.

### Invariants

**10** is preserved by construction: the tool is derived from the signed
`browser_tool` value, a catalogue choice the caller picked by name, and is
never read from the request or the pod. **9** preserved: the catalogue is
platform-wide and holds no tenant data. **5** unchanged: what an attempt is
derived from is its signed spec, verified by `specsign` before the worker
acts on it. 1-4 and 6-8 untouched.

**Stated limit, not hidden** (design §3.6). The boundary is the attempt. In
a `claude-code-browser` attempt with `browser_tool: auto`, the jev-browser
key is a file in a pod whose agent runs as the same uid, so the agent can
read it. The tool checkbox decides **which attempts** receive a secret, not
which process inside an attempt can read it. Only the later `inject` mode
(§3.5) narrows that.

## 82. `models.py`: the secret consumer's spelling, `consumer_name`, and the platform component list (SEC-CONTRACT, workspace secrets request C)

**Status:** ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line. Filed by lane SEC-CONTRACT (design §7 request C), part of #1347.
Proposed 2026-10-14, accepted on PR #1354 at about 23:20 PDT.
`is_secret_consumer` is applied beside the catalogue in `profiles.py`, as
"Where it lives" says, so `profiles.py` now imports `models.py`.
`parse_consumer`'s message echoes nothing, as this entry says, so
`consumer_name`'s own refusal is replaced rather than propagated (the
sketch below would have propagated it).

### Why

A grant's consumer is a string that is stored in many places: a Firestore
document id (`workspace_secret_grants/{secret_id}~{consumer}`), a field on
it, a URL path segment (`PUT /v1/workspace/secrets/{name}/grants/{consumer}`),
a release row's `consumers` and an audit detail. swarm-api writes it, the
worker re-derives it, and the console renders it. That is the reason
`Tenant.secret_name` is frozen: one spelling, or a grant written as
`tool/jev-browser` silently matches no release.

The spelling also carries the separation the design depends on.
`profile:indexer` (the agent profile) and `component:indexer` (the
platform's index step) are different consumers with the same bare name, so
the kind prefix is part of the identity. A parser that drops it widens a
grant. And `component:git` is fixed to forge tokens (§6.2), so the
component list has to be closed: an open list is how a forge token reaches
an unnamed step.

### The change

```python
#: Contract request 82 (workspace secrets, design §2.3). A workspace secret
#: is granted to a CONSUMER, spelt "<kind>:<name>". `profile:` names a key of
#: RUNNER_PROFILES, `tool:` a key of RUNTIME_TOOLS (request 81), `component:`
#: one of PLATFORM_COMPONENTS. Never an image, a command or a path
#: (invariant 10).
SECRET_CONSUMER_KINDS: tuple[str, ...] = ("profile", "tool", "component")

#: Platform steps that take a workspace secret themselves, outside any
#: agent. `judge` reads private/jev-key for TypeSafe judging; `merge` and
#: `indexer` are the worker's own steps; `git` takes forge tokens only and is
#: never offered a non-forge secret (design §6.2).
PLATFORM_COMPONENTS: tuple[str, ...] = ("judge", "merge", "indexer", "git")

_CONSUMER_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")


def consumer_name(kind: str, name: str) -> str:
    """The one spelling of a secret consumer: "<kind>:<name>".

    Checks the kind, the name's characters and, for `component`, the closed
    list. Membership of `profile` and `tool` names is checked against the
    catalogue by its reader (`profiles.py` imports this module, not the
    reverse, so this function cannot see RUNNER_PROFILES). A consumer
    name holds only `[a-z0-9-]` and one `:`, so it is safe in a Firestore
    id and a URL segment without escaping."""
    if kind not in SECRET_CONSUMER_KINDS:
        raise ValueError(f"unknown secret consumer kind {kind!r}")
    if not _CONSUMER_NAME.fullmatch(name):
        raise ValueError(f"secret consumer name {name!r} is not [a-z0-9-]")
    if kind == "component" and name not in PLATFORM_COMPONENTS:
        raise ValueError(f"unknown platform component {name!r}")
    return f"{kind}:{name}"


def parse_consumer(consumer: str) -> tuple[str, str]:
    """The inverse of consumer_name; raises on anything consumer_name would
    not have produced, so `parse_consumer(consumer_name(k, n)) == (k, n)`
    and no other string parses."""
    kind, sep, name = consumer.partition(":")
    if not sep or consumer_name(kind, name) != consumer:
        raise ValueError("not a secret consumer name")
    return kind, name
```

The error messages echo the kind and name a caller sent. Those are
catalogue words a member typed into a grant URL, never secret material.
`parse_consumer`'s message deliberately echoes nothing, because it is fed
stored ids.

**Where it lives.** §7 says `models.py`. That works because `models.py`
imports only `states.py` and `profiles.py` imports neither, so the
catalogue-membership check that needs `RUNNER_PROFILES` and
`RUNTIME_TOOLS` is a short helper beside them in `profiles.py`
(`is_secret_consumer(consumer) -> bool`), applied by SEC-STORE-API on
write and SEC-RELEASE on read. The owner may prefer all of it in
`profiles.py`, which is equally valid. The spelling must be frozen either
way.

### Rollback

Delete the two tuples and the two functions. The readers built on them
(SEC-STORE-API, SEC-RELEASE) would have to carry the spelling themselves,
which is the drift this request exists to prevent.

### What it would break

* **Nothing that exists.** These are new names, and no Firestore document
  carries a consumer yet.
* **Renaming a profile or a tool** now orphans its grants: they stay in
  Firestore, disabled in effect because no attempt derives that consumer
  any more. That fails closed. A rename lane must migrate grant ids in the
  same change, and the request records that so nobody learns it from a
  user.
* **The red test §8 names:** `consumer_name` round-trips through
  `parse_consumer` for every catalogue consumer, and refuses
  `component:anything-else`, an uppercase name, a second `:`, and an empty
  kind.

### Invariants

**10** is the point. A consumer is a name from a platform list, so a
workspace member who writes a grant chooses *which named consumer*, never
what runs. **9** preserved: a consumer carries no tenant. The tenant of a
grant is its own field, and the release bounds its query by
`tenant_id == task.tenant_id`, taken from the agent and not from the
request (§3.3 step 3). Nothing here can name another workspace. **5**: the
release that reads grants by consumer is fenced on the attempt's
generation (SEC-RELEASE), so a stale worker is released nothing whatever
its consumers. 1-4 and 6-8 untouched.

## 83. `states.py`: no new park reason for a missing workspace secret; a required slot with no grant parks `CREDENTIAL_MISSING` (SEC-CONTRACT, workspace secrets request D)

**Status:** ACCEPTED by the owner 2026-10-14 and APPLIED by the pull request that adds this line. Filed by lane SEC-CONTRACT (design §7 request D), part of #1347.
Proposed 2026-10-14, accepted on PR #1354 at about 23:20 PDT. It requests
**no change**, and none was made: `ParkReason.CREDENTIAL_MISSING` exists
(`states.py`) and `scheduler/credentials.py` parks it before any lease. It
is recorded so that no lane adds a park reason, and so the owner's
acceptance covers the reuse explicitly.

### Why

A `required` slot (request 80) with no enabled grant must stop the agent
before it leases, at no cost, and start it by itself once someone ticks the
grant (design §4.2). That is exactly what `ParkReason.CREDENTIAL_MISSING`
already means for a missing runner provider key. A tenant whose
`Tenant.credentials` lacks the profile's `provider` parks the step
`CREDENTIAL_MISSING` (`models.py`, the comment on `Tenant.credentials`),
and the scheduler promotes it when the credential appears. A second reason
(`SECRET_MISSING`, `GRANT_MISSING`) would make every reader of park reasons
(the scheduler's promotion, the console's park text, the outcome ledger)
learn a new member that means the same thing. A wake path keyed on the old
reason would also leave the new one parked for ever.

What distinguishes the two cases for a person is the park's **code** and
message, as `merge_slot_wait` distinguishes a `CI_PENDING` (request 62).
The proposed code `secret_grant_missing` names the slot and the consumer,
never a value. Writing it belongs to the scheduler's admission, not the
contract: `scheduler/credentials.py` already parks `CREDENTIAL_MISSING`
before any lease, and its credential sweep returns such a park to `READY`
once the credential exists.

### The change

```python
# None. ParkReason is unchanged:
class ParkReason(str, Enum):
    ...
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"   # also: a required secret slot has no enabled grant
```

The comment is the only edit, and it is optional. Applying the request
means accepting the reuse.

### Rollback

Nothing to roll back. If the owner declines, the alternative is a new
member with its own wake path in the scheduler, filed as a new request.

### What it would break

Nothing. **It places one obligation on the build lanes.** The scheduler's
credential check (`scheduler/credentials.py`) must count an enabled grant
for a required slot, and its sweep must promote such a park once the grant
is ticked, the way it promotes a park once a provider key is registered.
Otherwise "starts by itself" (§4.2) is false. That belongs to the
scheduler and SEC-STORE-API, not the contract. §8 names no lane for it, so
the PR body raises it.

### Invariants

**1** is why it exists. The park happens before the lease, so an agent
missing a required secret holds no capacity and no pod. **2-3** unchanged:
it is never admitted, so nothing is reserved or counted. **4** unchanged:
it is not a long provider wait inside a worker. 5-10 untouched.

## 84. `profiles.py`: `claude-code-review` is the one CLOUD_RUN_JOB profile that carries the Jev client; move it to GKE_AUTOPILOT, or retire it under request 50 (JUDGE-GKE)

**Status:** ACCEPTED as (b) by the owner 2026-10-16 (~11:35 PDT, on PR 1422 and
#1347): `claude-code-review` is retired under request 50 and is not moved, so
`profiles.py` is unchanged; it must move to GKE before it is ever re-enabled.
Filed 2026-10-16 by lane JUDGE-GKE, part of #1347, after the
owner's decision of 2026-10-16 (~11:20 PDT, on #1347): every attempt that
judges with TypeSafe runs on GKE, so the key never enters a pod and
SEC-RETIRE can remove every tenant worker's `-typesafe` grant.

### What is true today

The worker's Jev client (`agent_worker/judge/`) is set up by
`graphstage.prepare`, only for a runner in `graphstage.GRAPH_RUNNERS`
(`runners.claude_code`, `runners.codex`) and never for an index run. In the
catalogue that is `claude-code`, `codex`, `claude-code-browser` and
`claude-code-review`. The first three are GKE_AUTOPILOT (requests 53, 65,
67). `claude-code-review` is CLOUD_RUN_JOB, and no other CLOUD_RUN_JOB
profile carries the client: `mock` runs the mock runner, and `post-verdict`
is a worker action that runs no agent.

`claude-code-review` is also `available=False` (the #295 chain is off for
every tenant) and has no Job (`profiles_without_a_job` in
`terraform/infra/locals.tf`). It can dispatch nowhere today. The scheduler's
guard (`scheduler.dispatch.typesafe_judge_needs_gke`, lane JUDGE-GKE) would
refuse it on Cloud Run anyway whenever the forwarder serves its tenant and
the workspace judges with TypeSafe. So this request changes what the
catalogue says, not what can run.

### The change

Either of these. The owner picks one.

**(a) Move it to GKE, as `claude-code` moved in request 53.**

```python
_CLAUDE_CODE_REVIEW = RunnerProfile(
    name="claude-code-review",
    ...
    backend=Backend.GKE_AUTOPILOT,   # was CLOUD_RUN_JOB: its judge reaches the
                                     # TypeSafe forwarder only from GKE
    ...
)
```

**(b) Leave it on Cloud Run, and let request 50 retire it.** Then it simply
never judges with TypeSafe: it never runs. If it is ever re-enabled, it must
move to GKE first. Until it does, the scheduler's guard refuses a
TypeSafe-judging attempt of it at dispatch: each refusal returns the agent to
READY, and at the retry cap it ends FAILED with `typesafe_judge_needs_gke`.

The lane recommends (b). The profile is retired in all but name, request 50
already removes it, and moving a profile that cannot run buys nothing.

### Rollback

(a) is one line back to CLOUD_RUN_JOB. (b) changes nothing.

### What it would break

(a) Nothing that runs: the profile is unavailable and has no Job. The
Terraform catalogue mirror (`runner_profiles` in `terraform/infra/locals.tf`,
held field for field by `tests/terraform/catalogue.tftest.hcl`) and
`tests/unit/control_plane/test_internet_profiles_on_gke.py`, which lists the profiles that stay on Cloud Run, must
follow in the same pull request. (b) Nothing.

### Invariants

**1** unchanged: a refused attempt is returned to READY and holds no
capacity. **2** unchanged: the guard runs before anything is created, and
the lease it returns gives back every pool it reserved. **6** and **7**
unchanged: GKE runs on demand with `requests == limits`, as every
GKE_AUTOPILOT profile does. **10** unchanged: the backend stays the
catalogue's, never a caller's.

## 85. `states.py`: `WORKER_TRANSITIONS`, the transitions a tenant worker may request (ISO-CONTRACT, tenant settings integrity request A)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1464, design B) and APPLIED by the pull request that adds this line. Filed by lane ISO-SETTINGS-DESIGN
([tenant-settings-integrity.md](design/tenant-settings-integrity.md) §5 request A), applied by lane ISO-CONTRACT, part of #1457.
The applied set differs from the design's sketch in the way the request
itself directs ("the building lane reads `control.py` and `handoff.py` again
and lists exactly the edges they use"); see "The change".

### Why

Design B moves every worker state write behind worker-only swarm-api routes,
`/v1/attempts/{attempt_id}/state/...`, and each route permits only the
transitions a worker makes today (design §4.4). The agent can mint its
worker's ID token, so without that subset the routes would let an agent move
its own agent anywhere `_ALLOWED` permits: back to READY for another attempt,
or into DEAD_LETTERED. The route, the worker's client and the scheduler and
reconciler tests all need the subset, and until now it was implied only by
`control.py`'s call sites. One spelling, in the contract, or each restatement
drifts.

### The change

```python
WORKER_TRANSITIONS: frozenset[tuple[TaskState, TaskState]] = frozenset({
    (TaskState.LEASED, TaskState.DISPATCHED),
    (TaskState.DISPATCHED, TaskState.STARTING),
    (TaskState.STARTING, TaskState.RUNNING),
    *((s, t) for s in (TaskState.LEASED, TaskState.DISPATCHED, TaskState.STARTING)
             for t in (TaskState.FAILED, TaskState.CANCELLED)),
    *((TaskState.RUNNING, t) for t in (TaskState.PARKED, TaskState.READY,
          TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)),
})

def worker_may_transition(frm: TaskState, to: TaskState) -> bool:
    return (frm, to) in WORKER_TRANSITIONS and can_transition(frm, to)
```

Each edge, and the worker code that writes it (read 2026-10-17 at `766b9af`):

* LEASED→DISPATCHED→STARTING→RUNNING: `ControlPlane.advance_to_starting`,
  `advance_to_running`.
* LEASED, DISPATCHED or STARTING to FAILED: the crash handler's
  `_safe_finish` in `lifecycle.py`, for an exception before the walk ends.
* LEASED, DISPATCHED or STARTING to CANCELLED: `lifecycle._run`'s
  "cancelled before execution started", at whatever state the worker read. A
  restarted container of the same generation reads STARTING.
* RUNNING→PARKED: `park`, `await_park`, the CI park and `handoff`'s human park.
* RUNNING→READY: `fail_retryably`, and `handoff`'s human park when a person
  returned control. That park reads RUNNING and writes READY in one fenced
  transaction. It never writes PARKED→READY.
* RUNNING to SUCCEEDED, FAILED or CANCELLED: `finish`.

**Where it differs from the design's sketch, and why:**

* **PARKED→READY is not in the set.** The sketch named it for the
  hand-off's "person returned control". The worker's code makes that edge as
  RUNNING→READY (`handoff.py`, `assert_transition(current, TaskState.READY)`
  with `current` the RUNNING agent). PARKED→READY is swarm-api's `human-ready`,
  a person's route, so a worker that asked for it would be re-queueing an agent
  that holds no capacity. That is the scheduler's decision or a person's
  (invariant 1).
* **STARTING→READY is not in the set.** No worker path writes it. A worker
  that cannot start leaves the requeue to the reconciler
  (`_exit_unavailable_before_runner`, `_exit_interrupted_before_runner`).
* **STARTING→PARKED, STARTING→SUCCEEDED and both edges into DEAD_LETTERED
  are not in the set.** `_ALLOWED` refuses them, and the request says such an
  edge is dropped, not added to `_ALLOWED`.
* **The pre-running FAILED and CANCELLED edges are in the set.** The sketch
  did not list them, but the worker writes them.

**Tests:** `tests/unit/common/test_worker_transitions.py`.
`worker_may_transition(RUNNING, LEASED)` is false. Every edge is in `_ALLOWED`.
The set equals the edge table above, and the function equals membership and
legality over all 144 pairs. No edge enters LEASED or DEAD_LETTERED, or leaves
a state that holds no capacity.

### Rollback

Delete the constant and the function. Nothing reads them until ISO-STATE-API
builds the routes.

### What it would break

Nothing. Both are new names. **One obligation falls on the build lanes:** a
worker edge added later (a new park path, say) must join this set in the same
change, or the state route refuses it once the tenant's cutover switch is on.
Before the switch nothing reads the set, so what a running worker can do is
unchanged.

### Invariants

**5**: the routes run the fence on every edge in the set, in the transaction
that writes. **1**: no edge moves an agent that holds no capacity, and every edge
into PARKED or READY is paired with a release in the same transaction (design
§4.2). **3**: no edge enters LEASED, which only admission writes. 2, 4 and 6-10
are untouched.

## 86. `specsign.py`: a canonical form for a `workspace_settings` block (ISO-CONTRACT, tenant settings integrity request B)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1464; owner question Q2 chose phase 0 plus the interim MAC on `workspace_settings`) and APPLIED by the pull request that adds this line. Filed by lane ISO-SETTINGS-DESIGN
([tenant-settings-integrity.md](design/tenant-settings-integrity.md) §5 request B), applied by lane ISO-CONTRACT, part of #1457.

### Why

#1457: a tenant worker can write `workspace_settings/<B>.judgment_backend`,
and the scheduler and swarm-api act on it. Until design B removes the
worker's Firestore access, swarm-api MACs the fields the control plane acts
on, and every reader verifies the MAC. The scheduler (`judgment.py`,
`dispatch.py`) and swarm-api (`judgment.py`, `typesafe_ticket.py`,
`mergesettings.py`, `codeintel.py`) must compute identical bytes, and the only
code they share is `swarm_common`. `specsign` already owns JCS and purpose
separation.

### The change

```python
SETTINGS_PURPOSE = "swarm.settings/v1"

SETTINGS_MAC_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "top": ("judgment_backend",),
    "merge": ("mode", "train_size", "max_train_age_minutes"),
    "code_intelligence": ("jev", "context", "context_ab", "graph_delivery",
                          "judgment_backend"),
})

def canonical_settings_block(*, tenant_id: str, block: str,
                             fields: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": "swarm.settings/v1", "tenant_id", "block", "fields"}."""
```

The function projects `fields` onto the block's list. A listed key that is
absent is `null`, and any other key is not in the bytes. An unknown block, a
missing tenant or a `fields` that is not a map is `SpecNotCanonical`. These
three refusals were added in applying the request: each removes an input no
writer sends, so no MAC can be computed over an ambiguous tenant or block. The
purpose was named `SETTINGS_PURPOSE`, as `SPEC_PURPOSE` is named. The mapping
is read-only (`MappingProxyType`), so no reader can widen another's list at
runtime.

The MAC is `HMAC-SHA256(swarm-settings-key, canonical_settings_block(...))`.
It is not in the contract, because its key is not. The field names were
checked against the writers on 2026-10-17: `mergesettings.py` `as_dict`
writes `mode`, `train_size` and `max_train_age_minutes`, and `codeintel.py`
names `jev`, `context`, `context_ab` and `graph_delivery`.

**Tests:** `tests/unit/common/test_settings_block.py`. Two tenants with the
same fields get different bytes. A key outside the list does not change the
bytes. A changed covered value does. The block name is inside the bytes. The
form is JCS with its own purpose, and the lists are the request's and
read-only.

### Rollback

Delete the function, the constant and the purpose. Readers then fall back to
"unset".

### What it would break

Nothing until a reader verifies, and no reader does yet: ISO-SCHED and
ISO-SETTINGS-MAC build them, each behind its tenant's cutover. **Once a reader
verifies, every unsigned document reads as unset**, so ISO-SETTINGS-MAC runs
its re-sign pass over `workspace_settings` before switching readers on. A
change to a block's list changes that block's bytes, and every stored MAC of
it must be re-signed in the same rollout.

### Invariants

**9** is the point: a block copied from tenant A's document onto B's does not
verify, because the tenant is inside the bytes. 1-8 and 10 are untouched.

## 87. `models.py`: `Tenant.service_account`, `.namespace` and `.gcs_prefix` are records, not authority (ISO-CONTRACT, tenant settings integrity request C)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1464, phase 0 "derive, don't read") and APPLIED by the pull request that adds this line. Comments only; no field removed or retyped. Filed by lane ISO-SETTINGS-DESIGN
([tenant-settings-integrity.md](design/tenant-settings-integrity.md) §5 request C), applied by lane ISO-CONTRACT, part of #1457.

### Why

A tenant identity can rewrite its own `tenants/<id>` document. A dispatcher
that ran the next attempt as the stored `service_account`, or in the stored
`namespace`, would run tenant A's attempt as tenant B (design §1.3, rows 1-3).
Phase 0 makes every reader derive these values from `tenant_id`. The frozen
`Tenant` type is where a future reader learns whether it may trust them;
contract request 43 made `identity.worker_service_account_id` public for the
same reason.

### The change

```python
service_account: str | None = None  # RECORD ONLY: dispatch derives it with
                                    # identity.worker_service_account_id. A
                                    # tenant identity can rewrite this field.
gcs_prefix: str | None = None       # RECORD ONLY: derived from tenant_id.
namespace: str | None = None        # RECORD ONLY: derived from the template.
```

A `#:` paragraph above the three says why, in one place.

**Tests:** `tests/unit/common/test_tenant_records.py`. Each field's line
carries `RECORD ONLY`, and each still defaults to `None`.

### Rollback, what it would break

None. These are comments.

### Invariants

**9**. Nothing else is touched.

**Not requested, and not applied:** a new `identity` function,
or any change to `admission.py` or `config.py` (design §5, "Not requested").


## 88. `profiles.py`: `claude-code`, `codex` and `claude-code-browser` declare the input `mcp_servers` (MCP-CONTRACT, registered MCP servers request M1)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1466) and APPLIED by the
pull request that adds this line. Filed by lane CALLER-MCP-DESIGN
([registered-mcp-servers.md](design/registered-mcp-servers.md) §5 request
M1), applied by lane MCP-CONTRACT, part of #1433.

### Why

A step names the registered MCP servers its agent may call, by name. What a
caller may send a runner is the catalogue's declaration (request 25), read by
swarm-api's `check_inputs`, the plugin and the console, and a key inside
`input` is covered by the spec signature (every format since request 34's
format 1; request 91 records that it stays at format 3), so the
worker can trust the list it reads from Firestore.

### The change

A dict of its own, `_MCP_SERVER_INPUTS`, holding one key:

```python
"mcp_servers": RunnerInput(
    "list", minimum=1, maximum=4,
    items=RunnerInput("string", maximum=31),
    means="registered MCP servers, by name, the agent may call during its run; ...",
)
```

`_CLI_AGENT_MCP_INPUTS` is `_CLI_AGENT_INPUTS` plus it, and is the `inputs=` of
`claude-code` and `codex`; `_CLAUDE_CODE_BROWSER_INPUTS` spreads it too.
`_CLI_AGENT_INPUTS` itself is not edited, so `indexer` and
`claude-code-review`, which share it, do not gain the key: the indexer runs
no prompt a caller wrote, and a review step reads the diff it was given. Had
the key gone into `_CLI_AGENT_INPUTS`, both would have taken it, and an empty
`allowed_profiles` (design §4.2) would have let them reach a server.

31 is `MCP_SERVER_NAME`'s length (request 90). Membership is checked by
swarm-api against the tenant's registry, which changes at runtime, so the
names are not `choices`.

**Tests:** `tests/unit/common/test_mcp_contract.py`: accepted on the three
profiles; refused on `browser`, `merge`, `indexer` and `claude-code-review`
as an undeclared key; `issue` still accepted on `indexer` and
`claude-code-review`, whose inputs stay exactly `{"issue"}`; five names, none,
a non-string, a bare string and a 32-character name refused for a bound.

### Rollback

Put `inputs=_CLI_AGENT_INPUTS` back on `claude-code` and `codex`, and drop the
spread from `_CLAUDE_CODE_BROWSER_INPUTS`. An agent stored with the key fails
`check_inputs` on any re-validation, so roll back only with the registry
disabled.

### What it would break

Nothing that exists: an agent without the key is unchanged. Eleven unit tests
in six files outside this lane's territory restated the old declarations.
The lane left them for the operator's `mcp-contract-operator.patch`; the
pull request's first CI run was red on exactly these eleven, and its CI fix
updated them in the same pull request:

* `tests/unit/common/test_workspace_secret_contract.py`
  `test_consumer_kinds_and_platform_components` (the three kinds, request 89);
* `tests/unit/common/test_worker_action_profiles.py`
  `test_claude_code_review_is_claude_code_but_its_name_and_never_restore_checkpoint`;
* `tests/unit/control_plane/test_claude_code_browser_profile.py`
  `test_it_accepts_the_issue_input_like_claude_code`;
* `tests/unit/control_plane/test_indexer_profile.py`
  `test_the_indexer_profile_is_claude_code_on_the_indexer_image`;
* `tests/unit/control_plane/test_issue_input.py`
  `test_the_cli_agent_profiles_declare_issue_and_nothing_else` (claude-code, codex);
* `tests/unit/mcp/test_runner_inputs.py`
  `test_the_profiles_view_says_what_each_profile_takes`,
  `test_the_declaring_profiles_and_no_declaration_names_execution_detail` and
  `test_every_declared_input_is_one_its_runner_reads` (no runner reads the key
  until the worker lane; the patch names it as one exemption);
* `tests/unit/mcp/test_runner_input_prose.py`, twice: the generated
  `runner-inputs` tables for claude-code, codex and claude-code-browser in
  `docs/workflows.md` and `plugin/README.md`, which the patch regenerates.

Until lane MCP-SUBMIT merges,
swarm-api's `check_inputs` accepts the key on the three profiles and the
worker does not read it; MCP-SUBMIT refuses every `mcp_servers` with
`mcp_unavailable` while `SWARM_MCP_SERVERS` is off.

### Invariants

**10** is why it exists: names, never a URL or a command. **5**: the list is
signed, so a worker never attaches a server a tampered task document added.
1-4 and 6-9 untouched.


## 89. `models.py` / `profiles.py`: `mcp` is a secret-consumer kind with one slot, `auth` (MCP-CONTRACT, registered MCP servers request M2)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1466) and APPLIED by the
pull request that adds this line. Filed by lane CALLER-MCP-DESIGN (design §5
request M2), applied by lane MCP-CONTRACT, part of #1433.

### Why

A long-lived MCP credential is a workspace secret granted to the server that
uses it (design §4.5). Requests 80-82 made a consumer's spelling and slots
contract, so swarm-api, the worker and the console read one declaration; a
registered MCP server is a fourth kind of consumer.

### The change

```python
# models.py
SECRET_CONSUMER_KINDS: tuple[str, ...] = ("profile", "tool", "component", "mcp")

# profiles.py
MCP_SECRET_SLOTS: Mapping[str, SecretSlot] = MappingProxyType({
    "auth": SecretSlot(
        kinds=("api_key", "bearer_token"),
        means="the relay adds it as the registered server's auth header; never a file",
    ),
})
```

`consumer_name("mcp", name)` checks `name` against `MCP_SERVER_NAME` and the
reserved names (request 90), so `parse_consumer` accepts exactly the `mcp:`
spellings a registration may take. `is_secret_consumer` returns False for
every `mcp:` consumer: the name is a tenant's registration, not a catalogue
entry, so the catalogue cannot vouch for it, and swarm-api checks the
registry itself. Without that branch `mcp:judge` would have fallen through to
the platform-component check and passed.

**Tests:** `tests/unit/common/test_mcp_contract.py`: `consumer_name("mcp", "kb")`
round-trips through `parse_consumer`; `is_secret_consumer` is False for
`mcp:kb`, `mcp:judge` and `mcp:indexer`; the slot is `auth` alone, optional,
of two kinds, and read-only.

### Rollback

Remove the member and the slots. Grants already written to `mcp:` consumers
stop parsing and are dropped by the release and the attach as unknown, which
disables them safely.

### What it would break

`parse_consumer` accepts a fourth prefix; a reader that switched on the three
kinds exhaustively must handle `mcp` (the release's `attempt_consumers` must
exclude it, design §4.5, lane MCP-API). Until MCP-API, a grant to an `mcp:`
consumer is refused on write, because swarm-api's consumer catalogue
(`workspace_secrets.CONSUMERS_BY_NAME`) lists none.

### Invariants

**9**: a grant is still bounded by its own `tenant_id`. **10**: a consumer is
still a name. 1-8 untouched.


## 90. `models.py`: one spelling for an MCP server's name (MCP-CONTRACT, registered MCP servers request M3)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1466) and APPLIED by the
pull request that adds this line. Filed by lane CALLER-MCP-DESIGN (design §5
request M3), applied by lane MCP-CONTRACT, part of #1433.

### Why

The name is written by swarm-api (registration) and read by the worker, where
it becomes a server key in the CLI's MCP config and a path segment of the
relay, and by the plugin. It must never collide with a server the worker
registers itself.

### The change

```python
MCP_SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
#: Names the worker registers itself; a registration may not take them.
RESERVED_MCP_SERVER_NAMES: tuple[str, ...] = ("playwright", "jev")
RESERVED_MCP_SERVER_PREFIX = "swarm-"
```

`consumer_name` applies all three to an `mcp:` name (request 89). The
registration's check (lane MCP-API) applies the same three.

**Tests:** `tests/unit/common/test_mcp_contract.py`: `swarm-git` and
`swarm-graph` match the pattern and are refused by the reserved prefix;
`playwright` and `jev` by the list; upper case, a leading digit or hyphen, a
dot, a slash, an underscore and 32 characters by the pattern.

### Rollback

Remove the constants and the check in `consumer_name`; each reader would then
restate them, which is the drift the contract exists to stop.

### What it would break

Nothing; new names.

### Invariants

**10**: a server is named, never addressed. 1-9 untouched.


## 91. No new spec format, park reason or event type for registered MCP servers (MCP-CONTRACT, registered MCP servers request M4)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1466). Requests no
change, so nothing is applied; recorded so no lane adds one. Filed by lane
CALLER-MCP-DESIGN (design §5 request M4), part of #1433.

* **Spec signing:** `input` is already in `canonical_step_spec`, so
  `mcp_servers` is signed by format 3. The credential is not in the agent and
  must not be signed into it. `SPEC_FORMAT` stays 3. Held by
  `tests/unit/common/test_mcp_contract.py`: the canonical spec of an agent with
  `mcp_servers` differs from the same agent's without, and carries the list.
* **Park reason:** a disabled registration parks `CREDENTIAL_MISSING`, with
  code `mcp_server_unavailable`, exactly as request 83 reused it for a missing
  secret grant.
* **Event types:** the relay logs `mcp_call` as a structured log line, not an
  agent event; the attach's audit is its own collection. `EventType` is
  unchanged.

**Not requested:** `Tenant.secret_name` already spells `mcpc-<hex>`; `Task`
gains no field; `RunnerProfile.secrets` is unchanged.


## 92. `states.py`: `ParkReason.AWAITING_INPUT`, a workflow step waiting for a person's answer (HW-CONTRACT, human steps request A)

**Status:** ACCEPTED by the owner 2026-10-17 (Q1, on PR 1462; the design
merged as PR 1493) and APPLIED by the pull request that adds this line. Filed
by the design (docs/design/human-steps.md §5 request A), applied by lane
HW-CONTRACT, part of #1436. The design numbered it 85 ("the next free
number"); 85-91 were filed by ISO-CONTRACT and MCP-CONTRACT before this lane
ran, so it is 92 here and the design's §5 letter is the stable name.

### What is true today

An `await-input` step must be durably parked, at no cost, and ended only by a
member's answer, its declared expiry, or a cancel. No existing reason fits.
`HUMAN_REQUIRED` already has a promoter (`human-ready`, to READY), a 24-hour
dead-letter sweep (`Scheduler._expire_human_waits`), a bell item
(`notifications.py::handoffs`) and plugin wording, and all four are wrong for
this step (design §3.4). Request 68 refused to reuse `MANUAL_PAUSE` for the
same reason.

### The change

```python
class ParkReason(str, Enum):
    ...
    AWAITING_INPUT = "AWAITING_INPUT"
```

Never promoted to READY, and never read by the HUMAN_REQUIRED sweep,
`human-ready` or the browser bell: swarm-api ends it PARKED -> SUCCEEDED on
the answer or on its declared default, PARKED -> DEAD_LETTERED on expiry (at
most 24 hours), PARKED -> CANCELLED on a cancel. `_ALLOWED` is unchanged:
every one of those edges is legal today.

**Restatements in the same pull request**, held by
`scripts/lib/check-contract-parity.sh` and the UI's exhaustive tables:
`apps/swarm-ui/src/types.ts` (the union, `PARK_REASONS`,
`PARK_NEEDS_A_PERSON`, `REASON_COPY`),
`apps/swarm-ui/src/components/StatePill.tsx` (`PARK_WORD`, "waiting for a
member"), `apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE` and
`apps/swarm-mcp/swarm_mcp/compact.py::_WAITS_FOR`.

**The readers parity test**, `tests/unit/common/test_awaiting_input_readers.py`:
every non-test file under `apps/` and `scripts/` that names `HUMAN_REQUIRED`
must name `AWAITING_INPUT` or be on the test's list with its reason; and the
human-wait sweep, `human-ready` and the `handoffs` bell source each leave an
`AWAITING_INPUT` agent untouched (a 25-hour-old park is not dead-lettered,
`human-ready` is 409 `not_waiting_for_a_person`, the bell derives nothing).

**Tests:** `tests/unit/common/test_await_input_contract.py` (the member, and
that it holds no capacity) and the readers test above.

### Rollback

Delete the member, its restatements and the readers test, but only when no
agent is parked under it, because such a document would decode to an unknown
reason.

### What it would break

Nothing that exists: it is a new member, and readers with exhaustive tables
gain their row in the same change. A future file that branches on
`HUMAN_REQUIRED` without deciding about the new member fails the readers test
until it does, which is the intent.

### Invariants

**1**: a `PARKED(AWAITING_INPUT)` agent holds no lease and creates no demand,
because `PARKED` is in `PENDING_STATES` and not in `CONCURRENCY_STATES`.
**4**: no worker waits. 2, 3 and 5-10 are unchanged.


## 93. `profiles.py`: `RunnerProfile.control_plane`, a catalogue profile the platform never leases (HW-CONTRACT, human steps request B)

**Status:** ACCEPTED by the owner 2026-10-17 (the Q10 answer on PR 1493
accepted the design it serves) and APPLIED by the pull request that adds this
line. Design §5 request B (numbered 86 there; see entry 92 on the number),
applied by lane HW-CONTRACT, part of #1436.

### What is true today

A step type is chosen by profile name (invariant 10), and every reader (the
console, the bridge, the outcome ledger) already labels a step by its
profile. But `RunnerProfile.__post_init__` allowed an empty `runner_argv` only
on a `worker_action` profile, and a worker action is performed by the worker,
in a container, under a lease.

### The change

```python
    control_plane: bool = False
```

True says the opposite of `worker_action`: **nothing ever runs this profile,
anywhere**; the control plane ends its agents itself. In `__post_init__` the
empty-`runner_argv` refusal admits `control_plane`, and a `control_plane`
profile is refused if it sets `runner_argv`, `worker_action`, `provider`,
`secrets`, `secret_slots`, `inputs` or `live_browser`. The scheduler's
admission backstop (lane HW-SCHED) reads it so a step of such a profile is
never leased.

**Restatements in the same pull request:** `terraform/infra/locals.tf`
mirrors the catalogue field by field (`runner_profiles`, held by
`tests/terraform/catalogue.tftest.hcl`), and every `control_plane` profile is
in `profiles_without_a_job`, held by
`tests/unit/common/test_await_input_contract.py`.

**Tests:** `tests/unit/common/test_await_input_contract.py`: each of the seven
fields raises on a `control_plane` profile; an empty `runner_argv` is admitted
on one and still refused on a profile that is neither; every existing profile
is `control_plane=False`.

### Rollback

Delete the field and its checks, once no profile sets it (request 94 rolled
back first).

### What it would break

Nothing: the default is False, and every existing profile is unchanged.

### Invariants

**1, 2, 3**: the field exists to prevent a lease. **10**: the caller still
names a profile, and the field is the catalogue's own, never a caller's.


## 94. `profiles.py`: the `await-input` catalogue entry, shipped unavailable (HW-CONTRACT, human steps request C)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1493) and APPLIED by the
pull request that adds this line. Design §5 request C (numbered 87 there; see
entry 92 on the number), applied by lane HW-CONTRACT, part of #1436.
**Request D** (`available=True`, `disabled_reason` removed) is NOT filed or
applied here: the owner applies it after lane HW-ACCEPT is green on dev.

### What is true today

No catalogue entry names a step that waits for a person.

### The change

```python
    "await-input": RunnerProfile(
        name="await-input",
        image="agent-runtime-base",   # never pulled: control_plane
        resource_class="standard",    # never reserved: control_plane
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=(),
        control_plane=True,
        provider=None,
        secrets=(),
        timeout_seconds=300,          # unused; the wait is await_input's (at most 24 h)
        inputs={},
        available=False,
        disabled_reason="await-input steps are being built (docs/design/human-steps.md)",
    ),
```

`terraform/infra/locals.tf` mirrors it in `runner_profiles` and adds it to
`profiles_without_a_job`, so no Job is ever created for it.

**Why unavailable.** Lanes HW-API-SUBMIT and HW-SCHED read the field and the
reason, so the contract lands first. Until every build lane has merged, a
submission naming `await-input` is refused, and `available=False` is the
refusal that already exists (`validation.validate_runner_profile`), with its
reason served to the caller. Nothing merged here changes behaviour while it
is unavailable.

**Tests:** `tests/unit/common/test_await_input_contract.py`: the entry is
`control_plane`, unavailable with a reason, runs nothing and declares
nothing, and is in `profiles_without_a_job`;
`tests/terraform/catalogue.tftest.hcl`: eleven profiles, its pool exists, and
no tenant gets a Job for it.

### Rollback

Delete the entry and its Terraform mirror. No agent can name it while it is
unavailable.

### What it would break

Nothing. Profile pickers list it as disabled, with the reason shown, as they
list `post-verdict`.

### Invariants

As request 93.


## 95. `models.py`: `Task.additional_repositories`, the registered repositories a step reads beside its own (MR-CONTRACT, multi-repository steps request A)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1461, with the decisions
in its comments) and APPLIED by the pull request that adds this line. Filed
by the design (docs/design/multi-repository-steps.md §5 request A), applied
by lane MR-CONTRACT, part of #1434. The design wrote `<n>`; 95 is the next
free number on main at 2c0d5a7, and the design's §5 letter is the stable name.

**Changed from the design.** The design's `MAX_ADDITIONAL_REPOSITORIES = 4`
is not in the contract. The owner made the count limit (default 5) and the
total size budget (default 6 GiB) PLATFORM SETTINGS, editable at run time
and read by swarm-api at submission, never a code constant. The contract
keeps only `ADDITIONAL_REPOSITORIES_CEILING = 10` (owner, 2026-10-17 ~01:00
UTC): a guard on the document's shape, the top of the setting's accepted
range (1-10), never the operative limit. swarm-api refuses a step over the
setting `too_many_additional_repositories`, naming the current limit. The
entry's `forge_credential_source` values are named here too
(`ADDITIONAL_REPOSITORY_SOURCES`), from swarm-api's `ForgeSlot.source`.

### Why

A step that reasons over a product spread across repositories needs to
check them out at commits fixed at submission. The agent is the only document
the worker reads its spec from, and the signed spec is what makes a
rewritten list fail (invariant 5). An `input` key would be signed but
untyped, and every reader would re-validate a caller-shaped dict. A
`metadata` key outside `SIGNED_METADATA_KEYS` is not signed at all. The
entries carry `forge_credential` and `commit`, which only swarm-api may
write, as request 54's `forge_credential` is.

### The change

```python
ADDITIONAL_REPOSITORY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
ADDITIONAL_REPOSITORIES_CEILING = 10          # shape guard; the limit is a setting
ADDITIONAL_FORGE_CREDENTIAL = re.compile(r"git(-u-[0-9a-f]{16})?")   # never git-r-
ADDITIONAL_REPOSITORY_SOURCES = ("tenant", "owner", "person")
ADDITIONAL_REPOSITORY_KEYS = (
    "name", "repo_id", "repository", "repository_url", "ref", "commit",
    "forge_credential", "forge_credential_source", "size_kib",
)

def check_additional_repositories(value: Any) -> None: ...

@dataclass
class Task:
    ...
    additional_repositories: list[dict[str, Any]] | None = None
    # __post_init__ calls check_additional_repositories
```

`check_additional_repositories` accepts None or a list of at most the
ceiling, each entry exactly the keys above. `name` and `repo_id` must be
unique. `repository_url` must be `https://github.com/` + `repository`, and
`ref` must not be an option. `commit` is a 40-hex lower-case sha, and
`size_kib` a non-negative int, not a bool. Its message names the key and the
entry's index, never a value. `to_firestore` is unchanged: `asdict` writes
the list.

**Tests:** `tests/unit/common/test_additional_repositories_contract.py`.
Each of these raises: a name of `..`, a `git-r-` slot, a short commit, a
duplicate name or repo_id, and one entry over the ceiling. The message never
carries the value. An old document decodes with None.
`tests/unit/common/test_specsign_covers.py::test_every_task_field_is_signed_or_named_unsigned`
fails when a `Task` field is neither in the signed projection nor named
unsigned with its reason.

### Rollback

Remove the field and the names. A document that carries the field then fails
to decode wherever `Task(**doc)` is used, so first stop swarm-api writing it
(`settings.additional_repositories_enabled`, default off) and let those
agents end.

### What it would break

Nothing that exists. `Task(**doc)` on an old document leaves it None, and
`__post_init__` refuses a malformed list, which no writer produces. A reader
that walks task fields generically (the codec, the reconciler's raw reads)
sees one more key.

### Invariants

**5**: the field is inside the signature (request 96). **9**: `repo_id` is a
tenant-scoped hash, and the slot is the agent's own tenant's. **10**: the
field holds names and commits, never an image, command, resource or backend
parameter, and swarm-api writes it, never a caller. 1-4 and 6-8 untouched.


## 96. `specsign.py`: spec format 4 covers `additional_repositories` (MR-CONTRACT, multi-repository steps request B)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1461) and APPLIED by the
pull request that adds this line. Filed by the design (§5 request B), applied
by lane MR-CONTRACT, part of #1434. Format 4 is the next free format: main's
newest was 3 (request 54). Request 86 (ISO-CONTRACT) added the settings-block
MAC with its own purpose string, not a spec format, and request 91 recorded
that MCP servers add none.

### Why

If the list is not in the signed bytes, any identity that can write the task
document can rewrite it, the tenant's worker account included. A repointed
commit, or a repository the tenant registered but this step was never given,
would go unnoticed. Request 54 had the same reason for format 3.

### The change

```python
SPEC_FORMAT = 4
SPEC_FORMATS = (1, 2, 3, 4)
_FORMAT_4_FIELDS = ("additional_repositories",)
```

* `canonical_step_spec`: at formats 1-3, a set list (non-None, non-empty)
  raises `SpecNotCanonical`, as a format-3 field does below 3. At 4 the list
  is projected as stored, in submission order, and the order is in the bytes.
* `signing_format`: 4 only when the list is set. An agent without it keeps
  signing at 1, 2 or 3, with byte-identical canonical forms (pinned in
  `test_specsign.py::PRE_FORMAT_4_DIGESTS`), so a worker built before format
  4 verifies every agent that does not use the feature. **Rollout order:
  workers first, then swarm-api** (and the flag stays off until then), as for
  formats 2 and 3.

**Tests:** `tests/unit/common/test_specsign.py` holds format 4's golden
bytes and the pre-format-4 digests of formats 1-3. It also checks that a set
list at 1-3 has no canonical form.
`tests/unit/common/test_additional_repositories_contract.py` checks that a
rewritten commit, entry, order or slot changes the format-4 digest, and that
a real P-256 signature stops verifying. `test_specsign_covers.py` runs its
integer-field checks at every format, including 4.
`tests/unit/common/test_mcp_contract.py` asserted `SPEC_FORMAT == 3` as
request 91's "MCP adds no format". It now asserts that `mcp_servers` does not
change `signing_format`, which is what request 91 meant.

### Rollback

Return `SPEC_FORMAT` to 3 and `SPEC_FORMATS` to `(1, 2, 3)`. A document
already signed at 4 then fails verification (`SPEC_SIGNATURE_INVALID`) on
every worker, so first stop the writer (request 95's flag) and let those
agents end.

### What it would break

Nothing signed today: formats 1-3 project the bytes they did. A worker built
before format 4 refuses a format-4 agent as `unknown_format`, which is why
workers roll out first.

### Invariants

**5**: a rewritten list fails the worker's check before the agent runs.
Others untouched.


## 97. No change to `WorkflowStep`, `Workflow` or `profiles.py` for multi-repository steps (MR-CONTRACT, multi-repository steps request C)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1461). Requests no
change, so nothing is applied. Recorded so no lane makes one. Part of #1434.

* **`WorkflowStep` / `Workflow` gain no field.** The primary repository is
  not on them either. swarm-api's submission schemas carry the workflow's
  default list and a step's replacement list (`[]` = none) to each agent, so
  the step has one source of truth.
* **No new resource class and no profile flag.** Only GKE agent profiles take
  additional repositories (the owner's Q4). swarm-api derives that from
  `profiles.resolve_backend` and `worker_action`, which the catalogue
  already states. The Cloud Run profiles refuse them.
* **No limit in the contract.** The count limit and the size budget are
  platform settings (request 95). Only the shape ceiling is frozen.


## 98. `states.py`: QUEUED -> FAILED, a task that can never run ends at admission with no lease (MCP-FAILED-EDGE, part of #1433)

**Status:** ACCEPTED by the owner 2026-10-18 (~01:40 UTC, answering lane
MCP-SCHED's question on PR 1504) and APPLIED by the pull request that adds
this line. **No reader can write it yet**: the scheduler never holds an agent in
QUEUED (see "Who reads it"), so the behaviour the owner asked for needed
request 99 as well. The owner accepted 99 on 2026-10-18 and kept this edge
beside it; 99 is what ends such an agent FAILED now, and nothing writes
QUEUED -> FAILED.

### Why

[registered-mcp-servers.md](design/registered-mcp-servers.md) §4.10
("Expiry in the queue"): an agent whose per-run MCP credential expired, was
destroyed or is missing before it is admitted fails `mcp_credential_expired`
at admission, with no lease. It cannot park, because nobody can hand a queued
agent a new value; it cannot be dead-lettered either, because it did not fail
its way there through attempts (the owner: "not a dead-letter, not a permanent
park"). `_ALLOWED` had no edge from a waiting state to FAILED, so MCP-SCHED
shipped the stopgap: such an agent parks CREDENTIAL_MISSING and the credential
sweep never promotes it. That holds no capacity (invariant 1), but it never
ends: a workflow step blocks its workflow and a parent waits on its child.

### The change

```python
TaskState.QUEUED: frozenset(
    {TaskState.READY, TaskState.PARKED, TaskState.CANCELLED, TaskState.FAILED}
),
```

One edge, nothing else.

### Who reads it

The one reader meant to use it is scheduler admission,
`apps/scheduler/scheduler/loop.py` `Scheduler._admit_one`, on a
`CredentialAnswer.terminal` answer (`scheduler/credentials.py`, from
`McpAnswer.terminal` in `scheduler/mcp_admission.py`). **But the agent
admission reads is READY, not QUEUED**: `SchedulerStore.ready_tasks` selects
`state == "READY"`, and swarm-api stores a new agent as READY or PARKED, only
walking SUBMITTED -> QUEUED in memory (`swarm_api/service.py`, "walk the real
state machine even though only the end state is stored"). The credential
sweep, `Scheduler._promote_credentials`, reads PARKED. Neither READY -> QUEUED
nor PARKED -> QUEUED is an edge, so "promote to QUEUED, then fail" is not a
path either. So the reader that would write QUEUED -> FAILED has no QUEUED
agent to write it on; request 99 asks for the edges it does have.

Every other reader of `_ALLOWED` / `can_transition`, read 2026-10-18 at
`5ad3656`, is unchanged by the edge:

* `swarm_api/store.py` `_final_workflow_states`: asks only about edges OUT of
  a terminal state; FAILED's row is unchanged.
* `swarm_api/verdictpublish.py`, `scheduler/loop.py` (`PARKED -> SUCCEEDED`),
  `reconciler/store.py` (`PARKED -> READY`), `reconciler/checkpoints.py`
  (`state -> READY`): other edges.
* `reconciler/store.py` `repair_task_state`: repairs agents with attempts,
  never a QUEUED one.
* `states.worker_may_transition`: QUEUED is not in `WORKER_TRANSITIONS`, so a
  worker still cannot ask for it (request 85).
* `scripts/lib/check-contract-parity.sh` restates the state sets, not the
  transition table, so it has nothing to follow.

### Retry

FAILED -> READY stays an edge, and `mcp_credential_expired` must never take
it. Today nothing writes FAILED -> READY at all: the scheduler has no retry
policy module, the worker's retry is RUNNING -> READY (`fail_retryably`) and
the reconciler's is a requeue of a live attempt (`repair_task_state`, capped by
`retries_exhausted`). An agent failed at admission has no attempt, so neither
path can reach it. If a FAILED -> READY writer is ever added, it must treat
`mcp_credential_expired` and `mcp_credential_missing` as permanent.

**Tests:** `tests/unit/common/test_queued_to_failed.py`: QUEUED -> FAILED is
allowed; all 144 pairs equal the table above, so no other edge appeared; READY,
PARKED and SUBMITTED still cannot reach FAILED; QUEUED is the only source of
FAILED outside the concurrency states.

### Rollback

Remove `TaskState.FAILED` from the QUEUED row. Nothing writes it.

### What it would break

Nothing: no code writes the edge, and every reader is listed above.

### Invariants

**1, 2, 3**: the edge leaves a state that holds nothing for a terminal one,
so there is no lease, no reservation and nothing for concurrency to count.
**5**: no attempt, so no generation to fence. Others untouched.


## 99. `states.py`: READY -> FAILED and PARKED -> FAILED, the two states admission and the credential sweep actually hold (MCP-FAILED-EDGE, part of #1433)

**Status:** proposed 2026-10-18 by lane MCP-FAILED-EDGE. ACCEPTED by the
owner 2026-10-18, on that lane's PR 1530, keeping request 98 as it is, and
APPLIED by the pull request that adds this line (lane MCP-FAILED-EDGE-B, part
of #1433). The two rows below are the only lines of `apps/common/swarm_common/`
it changes, with a comment naming this request.

### What writes each edge (applied)

| edge | request | writer |
|---|---|---|
| QUEUED -> FAILED | 98 | **none.** Kept by the owner; a task is never stored QUEUED while it waits, so the scheduler has nothing to write it on |
| READY -> FAILED | 99 | `Scheduler._admit_one` (`apps/scheduler/scheduler/loop.py`), on a `CredentialAnswer.terminal` answer, after the credential question and before `acquire_lease` |
| PARKED -> FAILED | 99 | `Scheduler._promote_credentials`, on a CREDENTIAL_MISSING park whose answer is terminal: a park MCP-SCHED's stopgap left, or one parked for a disabled server whose credential then expired |

Both go through `Scheduler._fail_unrunnable` to one guarded write,
`SchedulerStore.fail_before_lease` (`apps/scheduler/scheduler/store.py`),
which refuses any source but READY or PARKED. It writes `state` FAILED,
`last_error` the terminal code (`mcp_credential_expired` for a row expired or
destroyed, i.e. revoked; `mcp_credential_missing` for no row), `completed_at`,
`end_cause` None, and clears `park_reason`, `next_eligible_at` and
`blocked_by`, guarded on the state and park reason the drain read, then a
`failed` event naming the servers and codes only. It takes no lease, reads no
pool and leaves `attempt_count` alone.

**Never retried.** FAILED -> READY stays in the table and nothing writes it
on such an agent: `SchedulerStore.promote_to_ready`, the scheduler's only READY
writer, now refuses any source but PARKED (its guard would otherwise match a
FAILED snapshot, whose park reason is None like READY's), and
`tests/unit/scheduler/test_mcp_admission.py` scans `apps/` for any writer
naming FAILED -> READY.

**Not done here:** `end_cause` stays None. A dedicated `EndCause` value is a
further `models.py` request, not filed.

### Why

Request 98 was accepted so that admission ends an agent with an expired per-run
MCP credential FAILED, with no lease. Admission holds that agent in READY, and
the credential sweep in PARKED (request 98, "Who reads it"), so the edges that
do what the owner asked are:

* **READY -> FAILED**, written by `Scheduler._admit_one` on a
  `CredentialAnswer.terminal` answer, before `acquire_lease`;
* **PARKED -> FAILED**, written by `Scheduler._promote_credentials` on a
  CREDENTIAL_MISSING park whose answer has become terminal (every park
  MCP-SCHED's stopgap left behind is one).

The alternative, READY -> QUEUED and PARKED -> QUEUED followed by request 98's
edge, adds two edges that move an agent backwards through admission for no
purpose but to reach a third, and lets any writer re-queue a waiting agent.

### The change

```python
TaskState.PARKED: frozenset(
    {TaskState.READY, TaskState.CANCELLED, TaskState.DEAD_LETTERED,
     TaskState.SUCCEEDED, TaskState.FAILED}
),
TaskState.READY: frozenset(
    {TaskState.LEASED, TaskState.PARKED, TaskState.CANCELLED, TaskState.FAILED}
),
```

Request 98's QUEUED -> FAILED then has no writer, and can be kept (swarm-api
walks QUEUED in memory, so a submission-time refusal could use it) or rolled
back.

### What it needs outside the contract

* A guarded store write, applied as `SchedulerStore.fail_before_lease`
  beside `SchedulerStore.cancel` (`apps/scheduler/scheduler/store.py`): FAILED,
  `completed_at`, `last_error` = the code, `park_reason` and `blocked_by`
  cleared, a FAILED event carrying the refusal detail (server names and codes
  only), guarded on the state the drain read.
* An `end_cause`. `EndCause` has no value for "a credential nobody can
  replace"; the write can carry None (the ledger falls back to `last_error`),
  or a new value, which is a further `models.py` request.
* `loop.py` calls `_note_ended` and drops the workflow's cached verdict, as
  the dispatch-failure branch of `_admit_one` does for its own FAILED write.

### What it would break

`test_parked_to_succeeded.py` pins the PARKED row and
`test_queued_to_failed.py` the whole table; both change with it.
`swarm_api/store.py` `_final_workflow_states` is unchanged (edges out of
terminal states). A PARKED or READY agent reaching FAILED is a new shape for
the console's waiting lists, which already read state, not history.

**Tests:** `tests/unit/common/test_queued_to_failed.py` pins all 144 pairs
of the table with 98 and 99 both present, and that QUEUED, READY and PARKED
are the only sources of FAILED outside the concurrency states;
`test_parked_to_succeeded.py` pins the PARKED row;
`tests/unit/scheduler/test_mcp_admission.py` ends an agent FAILED from READY
and from PARKED for an expired, destroyed and missing row, with no lease, no
pool moved, no attempt and no retry over two further drains.

### Rollback

Remove `TaskState.FAILED` from the READY and PARKED rows and the two calls to
`_fail_unrunnable`; such an agent then parks CREDENTIAL_MISSING for good again,
which the owner rejected on 2026-10-18.

### Invariants

**1, 2, 3**: both edges leave states that hold nothing and are written before
any lease, so nothing is reserved or released. **5**: no attempt, no
generation.


## 100. `profiles.py`: `ModelClass` and `MODEL_CLASSES`, the four model classes a step may name (MODEL-CONTRACT, model per step request A)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1509, with the decisions
recorded there and on PR 1460) and APPLIED by the pull request that adds this
line. Filed by the design (docs/design/model-per-step.md §5 request A),
applied by lane MODEL-CONTRACT, part of #1435. The design wrote "85 to 89 at
this reading"; 100 to 104 are the next free numbers on main at a989111, and
the design's §5 letter is the stable name.

### Why

Since #226 every claude-code step runs the pinned `claude-opus-5-5`, so a step
that classifies feedback or reformats a document costs what a deep-reasoning
step costs (#1435). Invariant 10 forbids a caller from naming a model ID, so a
step names a CLASS by a name that must have one spelling across swarm-api's
validation, the signed spec, the scheduler's dispatch, the worker's check, the
API, the console and the plugin. That is why the `resource_class` vocabulary
is frozen, and it applies equally here. The vocabulary is what a caller is
promised; the IDs behind it are Terraform's (`runner_models` in
terraform/infra/locals.tf, keyed by class), so a model release is an edit
there and a release, never a contract request (owner, Q1 (a)).

### The change

```python
@dataclass(frozen=True)
class ModelClass:
    name: str
    family: str      # the family word in claude-<family>-<major>[-<minor>]
    min_major: int   # the oldest generation a pin of this class may name

    def admits(self, model_id: str) -> bool:
        # claude-<family>-<major>[-<minor>][-<8-digit date>], major >= min_major;
        # anything that is not a str is refused.

MODEL_CLASSES: dict[str, ModelClass] = {
    "opus": ModelClass("opus", "opus", 5),       # the default: coding, implementation
    "fable": ModelClass("fable", "fable", 5),    # planning ("Fable 5+")
    "sonnet": ModelClass("sonnet", "sonnet", 5),
    "haiku": ModelClass("haiku", "haiku", 5),
}
```

The pins the owner chose for them (Terraform, not here): opus
`claude-opus-5-5`, fable `claude-fable-5-1`, sonnet `claude-sonnet-5-5`,
haiku `claude-haiku-5-5`. `admits` has one addition to the design's text: a
value that is not a string is refused rather than raising `TypeError`, so a
malformed environment map reaches its reader's own refusal.

**Tests:** `tests/unit/common/test_model_classes.py`: the vocabulary is
exactly the four; `fable` admits `claude-fable-5-1` and `claude-fable-6` and
not `claude-opus-5-5`; `sonnet` does not admit `claude-sonnet-4-6` (below the
floor); `haiku` admits `claude-haiku-5-5-20261001` and not
`claude-haiku-5-5[1m]`. `scripts/lib/check-contract-parity.sh` section 17
applies `admits` to Terraform's `runner_models`, and fails on a class with no
pin, a pin under a name the vocabulary lacks, or a pin its class refuses.

### Rollback

Delete the dict and the class. Requests 101, 102 and 104 depend on it and go
with it.

### What it would break

Nothing that runs: it is a new name.

### Invariants

**10** is the reason: a closed vocabulary of names, never a model ID. 1 to 9
are untouched.


## 101. `profiles.py`: `RunnerProfile.model_classes`, `default_model_class` and `step_model_class` (MODEL-CONTRACT, model per step request B)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1509, with the decisions
recorded there and on PR 1460) and APPLIED by the pull request that adds this
line. Filed by the design (docs/design/model-per-step.md §5 request B),
applied by lane MODEL-CONTRACT, part of #1435. The design wrote "85 to 89 at
this reading"; 100 to 104 are the next free numbers on main at a989111, and
the design's §5 letter is the stable name.

**Changed from the design.** The two fields come LAST in `RunnerProfile`, after
`control_plane` (request 93), not after `secret_slots`: request 93 had added a
field there since the design was written, and the end of the class is where
no positional construction can change meaning (the reason `Attempt`'s
additions give). `model_classes` naming a class twice is also refused.

### Why

Each profile must say which classes it accepts and which one a step gets when
it names none, so that swarm-api, the scheduler and the worker resolve a
step's class by one rule rather than three. Owner's Q3: every
claude-code-family profile accepts all four classes; R3: a step naming no
class runs its profile's default, opus.

### The change

```python
# RunnerProfile, last:
    model_classes: tuple[str, ...] = ()
    default_model_class: str | None = None

def step_model_class(profile: RunnerProfile, requested: str | None) -> str | None:
    """`requested` if the profile accepts it, the profile's default if None,
    None for a profile with no classes; ValueError naming the accepted classes
    otherwise."""
```

`__post_init__` refuses: a name outside `MODEL_CLASSES`; a name listed twice;
a default outside the profile's own `model_classes`; classes without a
default, or a default without classes; and classes on a `worker_action`
profile or on a profile whose `provider` is not `anthropic`. `claude-code`,
`claude-code-browser` and `indexer` set
`model_classes=("opus", "fable", "sonnet", "haiku"), default_model_class="opus"`.
`claude-code-review` (retired under request 50, request 84 (b)) sets
`("opus",)` and `"opus"`: it keeps the pin it has today and gains nothing.
`codex`, `mock`, `generic`, `browser`, `merge`, `post-verdict` and
`await-input` set neither. `step_model_class` refuses a name outside the
vocabulary, any name on a profile with no classes, and a name the profile
does not accept, each with its own message, so swarm-api can answer
`unknown_model_class` and `model_class_not_accepted` from them.

The Terraform mirror (`runner_profiles` in terraform/infra/locals.tf) gains
both fields in the same change. The Jobs' `MODEL` and the scheduler's
`WORKER_MODELS` read each profile's default class from it.

**Tests:** `tests/unit/common/test_model_classes.py`: a `merge` profile with
classes raises; codex and mock with classes raise; a default outside the
list, classes without a default and a default without classes raise; every
available profile whose `runner_argv` is the claude_code runner has all four
classes and `opus`; `step_model_class` returns `opus` for None on
`claude-code` and `haiku` for `haiku`, and raises for `haiku` on
`claude-code-review` and for anything on `codex`.
`tests/terraform/catalogue.tftest.hcl` (`the_mirror_carries_each_profiles_model_classes`)
holds the mirror's values, and `check-contract-parity.sh` section 17 compares
the mirror to the imported catalogue, profile by profile.

### Rollback

Delete the two fields and the function, with the mirror's two fields.
Request 102's fields would have no rule to resolve against, so 102 goes with
it.

### What it would break

Nothing that runs: two fields with defaults. The mirror and its tests change
with it, in this change.

### Invariants

**10**: the classes are catalogue data, and a request names one of them.
**6, 7**: the profiles' backend, Spot strategy and resources are untouched.
1 to 5, 8 and 9 are untouched.


## 102. `models.py`: `Task.model_class` and `WorkflowStep.model_class` (MODEL-CONTRACT, model per step request C)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1509, with the decisions
recorded there and on PR 1460) and APPLIED by the pull request that adds this
line. Filed by the design (docs/design/model-per-step.md §5 request C),
applied by lane MODEL-CONTRACT, part of #1435. The design wrote "85 to 89 at
this reading"; 100 to 104 are the next free numbers on main at a989111, and
the design's §5 letter is the stable name.

**Changed from the design.** Both fields come LAST in their classes, not after
`Task.model` and `WorkflowStep.resource_class`, for the reason request 101
gives: a field inserted mid-class moves every positional argument after it.

### Why

The class a step asked for is a fact about the agent. It must survive retries,
be signed, and be read by the scheduler at dispatch and by the worker at
start. `Task.model` cannot carry it: that is an ID, a record of a pin at
submission.

### The change

```python
# Task, last:
    model_class: str | None = None   # None: the profile's default_model_class

# WorkflowStep, last:
    model_class: str | None = None
```

swarm-api writes `Task.model_class` at submission only when it differs from
the profile's default (MODEL-API); it is signed at spec format 5 (request
104) and resolved by `profiles.step_model_class`.

**Tests:** `tests/unit/common/test_model_classes.py`: an agent names no class by
default and its stored document carries `model_class: None`; a task's class
reaches `to_firestore()`; a workflow step keeps the class it names. The codec
round trip the design names (`swarm_api.codec.task_from_dict` reading
`model_class` back, request 54's precedent) is MODEL-API's: the codec is in
its territory, not this lane's.

### Rollback

Delete both fields. A stored value is then ignored and every step runs its
profile's default. Request 104's format 5 must go first, or a format-5 agent
would fail verification on a worker whose `Task` lacked the field.

### What it would break

Nothing that decodes today: two optional fields with defaults.
`tests/unit/common/test_specsign_covers.py::test_every_task_field_is_signed_or_named_unsigned`
sees the new field inside the format-5 projection, as it must.

### Invariants

**1 to 3**: a field, not a pool, so admission is unchanged. **5**: signed,
and fixed across attempts. **8**: a checkpoint never crosses a class.
**10**: a name from the frozen vocabulary.


## 103. `models.py`: `Attempt.model`, the model ID an attempt's agent was started with (MODEL-CONTRACT, model per step request D)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1509, with the decisions
recorded there and on PR 1460) and APPLIED by the pull request that adds this
line. Filed by the design (docs/design/model-per-step.md §5 request D),
applied by lane MODEL-CONTRACT, part of #1435. The design wrote "85 to 89 at
this reading"; 100 to 104 are the next free numbers on main at a989111, and
the design's §5 letter is the stable name.

### Why

"What did this attempt run" must be a fact about the attempt. Neither
`runner_profile` nor `model_class` answers it once a pin changes.
`control.record_spend` writes only fields the frozen `Attempt` declares, so
the worker cannot add it alone.

### The change

```python
# Attempt, last:
    model: str | None = None
```

Written by the worker at attempt start after its generation check
(MODEL-WORKER). None: no model (mock, generic, browser, worker actions), or
an attempt written before the field existed.

**Tests:** `tests/unit/common/test_model_classes.py`: an attempt records no
model by default and carries one it is given. The `attempt_from_dict` to
`attempt_to_api` round trip the design names is MODEL-API's (`swarm_api.codec`).

### Rollback

Delete the field. Readers fall back to the runner summary's `usage.models`.

### What it would break

Nothing: an optional field with a default.

### Invariants

**5**: written after the fencing check, so a stale worker never writes it. 1
to 4 and 6 to 10 are untouched.


## 104. `specsign.py`: spec format 5 covers `model_class` (MODEL-CONTRACT, model per step request E)

**Status:** ACCEPTED by the owner 2026-10-17 (on PR 1509, with the decisions
recorded there and on PR 1460) and APPLIED by the pull request that adds this
line. Filed by the design (docs/design/model-per-step.md §5 request E),
applied by lane MODEL-CONTRACT, part of #1435. The design wrote "85 to 89 at
this reading"; 100 to 104 are the next free numbers on main at a989111, and
the design's §5 letter is the stable name.

**Changed from the design.** The design named this format 4. Request 96
(MR-CONTRACT) took format 4 for `additional_repositories` before this lane
ran, so `model_class` is format 5: format 4's projection plus `model_class`.
The design's rollback named an API gate (`model_classes_enabled`); the owner
dropped every enablement flag (R4, 2026-10-17), so no such gate exists and
lane order alone keeps format 5 unwritten until the worker that verifies it
has shipped (MODEL-WORKER before MODEL-API).

### Why

The worker acts on the class, and the scheduler dispatches by it. A field the
worker acts on must be inside the signature (request 34's rule,
`tests/unit/common/test_specsign_covers.py`). Without it, anyone who can write
the tenant's task document could move a step to another class, and the step
would run on a model nobody submitted under a signature that still verifies.

### The change

```python
SPEC_FORMAT = 5
SPEC_FORMATS = (1, 2, 3, 4, 5)
_FORMAT_5_FIELDS = ("model_class",)
```

`signing_format` returns 5 when `model_class` is set (non-None), and otherwise
falls through to its existing rule. `canonical_step_spec` refuses
(`SpecNotCanonical`) a document at formats 1 to 4 that sets `model_class`,
and includes the field in the projection at format 5.

**Tests:** `tests/unit/common/test_specsign.py`: a format-5 document has the
pinned bytes and verifies; rewriting its `model_class` (or removing it)
changes the digest; a document at formats 1 to 4 with `model_class` added is
`SpecNotCanonical`; an agent with no class (absent or None) signs at the format
it signed at before, with the SHA-256 of formats 1 to 4 pinned as computed
before this change (at a989111).

### Rollback

Stop writing format 5 (swarm-api stores no non-default class), then remove
the format once no stored agent carries it. A worker that loses format 5 while
such an agent is queued refuses it as `unknown_format`, failing closed.

### What it would break

Nothing for an agent without a class: its format and bytes are unchanged, and
nothing sets `model_class` until MODEL-API. A worker built before format 5
refuses a format-5 agent; the design's §4.3 order (worker first, then the API)
is what retires those workers before anything writes one. Tests that pinned
`SPEC_FORMAT == 4` or listed one document per format change with it.

### Invariants

**5**: the class is fixed with the attempt's other signed fields. **10**: the
signature is what makes the caller's named class the only one that can run.
1 to 4 and 6 to 9 are untouched.


## 105. `profiles.py`: `await-input` becomes available (HW-ENABLE, human steps request D)

**Status:** ACCEPTED by the owner 2026-10-18 (on PR 1570) and APPLIED by the
pull request that adds this line. The owner turned the profile on in dev BEFORE
lane HW-ACCEPT, because the acceptance group cannot submit an await-input step
while the profile is unavailable; HW-ACCEPT then proves it on dev. Design §5
request D, applied by lane HW-ENABLE, part of #1436.

### What is true today

`RUNNER_PROFILES["await-input"]` is `available=False` with
`disabled_reason="await-input steps are being built (docs/design/human-steps.md)"`
(entry 94). While it is, a workflow that names it is refused like any disabled
profile (`validation.validate_runner_profile`), and the expiry sweep reads
nothing (`swarm_api.awaitinput.dark`). Of the design's §6 lanes, HW-CONTRACT
(PR 1510), HW-WORKER (PR 1520), HW-SCHED (PR 1521), HW-IAM (PR 1523),
HW-API-SUBMIT (PR 1527), HW-NOTIFY (PR 1529), HW-API-ANSWER (PR 1537), HW-DOCS
(PR 1556) and HW-MCP (PR 1557) are on main at bc64f38; HW-UI, the console
card, and HW-ACCEPT are not (read 2026-10-18).

### The change

```python
    "await-input": RunnerProfile(
        ...
        inputs={},
        available=True,
    ),
```

`available=False` becomes `available=True`, and the `disabled_reason` line is
removed (its default is `""`). Nothing else in `apps/common/swarm_common/`
changes; the entry's comment, which says the profile shipped unavailable until
this request, stays as it is.

**What follows outside the contract, in the same pull request.**
`apps/swarm-ui/src/api.ts` `FIXTURE_AVAILABILITY`, which
`tests/unit/control_plane/test_capacity_serves_availability.py` holds to the
catalogue. `tests/unit/common/test_await_input_contract.py`, which held the
shipped value. The dark-path tests in `test_await_input_api.py` and
`test_await_input_submission.py`, which now switch the profile off with
`monkeypatch` rather than assert the shipped value, so the dark path stays
tested. `TASK_PROFILES` in `test_runner_inputs_by_declaration.py`, which now
leaves out `control_plane` profiles as it leaves out worker actions, because a
`control_plane` profile is a workflow step or nothing
(`validation.refuse_control_plane_outside_workflow`). `docs/workflows.md` and
its guard in `test_docs_describe_what_was_built.py`; `docs/worker-images.md`,
`docs/BUILD_PROMPT_V2.md` and `plugin/README.md`. `scripts/lib/check-contract-parity.sh`
restates no profile's availability, and `terraform/infra/locals.tf`'s mirror of
the entry carries no `available` field, so neither changes.

**Tests:** `tests/unit/common/test_await_input_contract.py`:
`test_await_input_is_available_since_request_d` (the entry is available, with
no reason, and still `control_plane`) and `test_request_d_is_filed_as_entry_105`.

### Rollback

The word back: `available=False` with a `disabled_reason`, and the fixture line
with it. A new submission naming the step is then refused again and the expiry
sweep stops reading. A step already parked `AWAITING_INPUT` stays parked,
holding no capacity (invariant 1): the answer route still takes a member's
answer, but nothing expires it while the profile is dark, so cancel the open
waits (`GET /v1/inputs?status=waiting` lists them) if none should be answered.

### What it would break

Nothing that exists. From then on a member of a tenant can submit a workflow
with an `await-input` step. Pickers that list available profiles list it as
usable: `swarm_profiles` (`swarm_mcp.profiles.dispatchable`), as it lists
`merge`, a workflow step too; and the console's agent form, which draws it as an
ordinary runner until HW-UI lands. Choosing it for a single agent is refused
422 at the door, naming the workflow route.

### Invariants

As request 93. **1**: a parked await step holds no lease and no capacity
(HW-SCHED). **10**: the step is named by profile, and its wait is the signed
`await_input` block, never a caller-supplied command.


## 106. `profiles.py`: `claude-code-browser` declares `qa_identity` (QI-CONTRACT, QA identity request QI-1)

**Status:** ACCEPTED by the owner 2026-10-19 with option B (a QA service
account whose IAP JWT the live gate adds; `docs/design/qa-identity.md` §7)
and APPLIED by the pull request that adds this line. Design §5 request QI-1,
filed here at the next free number by lane QI-CONTRACT, part of #1649.

### What is true today

`_CLAUDE_CODE_BROWSER_INPUTS` is `_CLI_AGENT_INPUTS`, `_MCP_SERVER_INPUTS`
and `browser_tool` (requests 77 and 88). An agent that names any other key is
refused `invalid_input` by `check_inputs`. No profile lets a caller say the
browser should be signed in to anything: an agent that has to reach the
console behind IAP waits for a person to type a sign-in (#1649, gap 3).

### The change

```python
_CLAUDE_CODE_BROWSER_INPUTS: dict[str, RunnerInput] = {
    **_CLI_AGENT_INPUTS,
    **_MCP_SERVER_INPUTS,   # contract request 88
    "browser_tool": RunnerInput(...),   # unchanged
    "qa_identity": RunnerInput(
        "string",
        choices=("console",),
        means=(
            "sign the live browser in to this deployment's console as its QA "
            "identity, a service account with its own workspace; the platform "
            "adds the credential, the agent never holds it, and a tenant may "
            "name it only when a platform admin has granted it"
        ),
    ),
}
```

Nothing else in `apps/common/swarm_common/` changes. No profile but
`claude-code-browser` declares it: only that profile has a live gate
(`live_browser=True`), and option B needs the gate. The signed spec already
covers `input`, so the worker acts only on a value the submitter signed; no
new spec format.

**What follows outside the contract, in the same pull request.** The two
generated `runner-inputs:claude-code-browser` tables, in `docs/workflows.md`
and `plugin/README.md`, which `tests/unit/mcp/test_runner_input_prose.py`
holds to the catalogue. The two tests that held the profile's input set to
`{issue, browser_tool, mcp_servers}`:
`tests/unit/control_plane/test_claude_code_browser_profile.py` and
`tests/unit/mcp/test_runner_inputs.py`. `scripts/lib/check-contract-parity.sh`
restates no `qa_identity` and compares the Submit form's offers as a subset
of what a profile declares, so it does not change. `terraform/infra/locals.tf`
mirrors no profile's inputs.

**Until lane QI-API lands, the name is accepted and does nothing.** An agent
that sends `qa_identity: "console"` passes `check_inputs`, and no worker
reads the key, so its browser is signed in to nothing, exactly as without it.
The per-tenant grant (403 `qa_identity_not_granted`) and the unconfigured
refusal (422 `qa_identity_unconfigured`) are QI-API's, in swarm-api.

**Tests:** `tests/unit/common/test_qa_identity_contract.py`: the key is
declared on `claude-code-browser`, a string with exactly the choice
`console`; no other profile declares it, and each refuses it as undeclared;
`check_inputs` accepts `"console"` and refuses `"admin"`, `""` and a
non-string as a bound broken on the declared key; and
`test_request_qi_1_is_filed_as_entry_106`.

### Rollback

Delete the entry. A queued agent that names it is then refused by the worker
as an undeclared input, failing closed. Revoke every grant first so nothing
new is submitted.

### What it would break

Nothing that exists: the key was refused before. Pickers that render the
catalogue's inputs (the console's Submit form, `swarm_profiles`) show one
more option.

### Invariants

**10**: a name from a closed list, never a credential, host or account.
**9**: the name grants nothing by itself. swarm-api's per-tenant grant
decides, at submission and again at signing. 1 to 8 are untouched.

## 107. `models.py`: `Attempt.throttled_seconds`, `peak_throttled_share`, `mean_throttled_share` and `memory_limit_bytes` (LIVE-UTIL-API, part of #1689)

**Status:** open, REQUESTED 2026-10-20 by lane LIVE-UTIL-API. Not applied:
the four figures ride on every periodic HEARTBEAT event's `detail` until this
is decided, which works today and changes no frozen type.

### What is true today

The owner asked on 2026-10-20 (#1689) for close to real-time CPU and memory
use of a running agent, with CPU throttling MEASURED, so "starved" stops being
an inference from a peak at the limit. The worker now reads the container's
cgroup v2 `cpu.stat` CFS counters (`nr_periods`, `nr_throttled`,
`throttled_usec`) every 5 seconds (`agent_worker.metrics.ResourceSampler`
`sample_point`) and keeps, per attempt, every runner combined
(`agent_worker.metrics` `combine_usage`):

* `throttled_seconds`: `throttled_usec` over the attempt, in seconds -- the
  time runnable work waited for its quota, summed across CPUs;
* `peak_throttled_share`: the most-throttled 5-second window, 0..1;
* `mean_throttled_share`: throttled periods over all periods.

The CPU figures they belong beside are typed `Attempt` fields (requests 15 and
26), written by `ControlPlane.record_cpu_usage`. These four are not: `Attempt`
has no field for them, so the worker puts them, with the container's memory
limit (`memory_limit_bytes`, cgroup `memory.max` else the sized class's), on
each HEARTBEAT event (`agent_worker.lifecycle` `Worker._usage_reading`), and
`GET /v1/tasks/{id}/utilisation` (`swarm_api.utilisation`) reads the newest
beat's. That is the interim path request 15 replaced for the CPU figures:
not queryable across attempts, and a read of the agent's HEARTBEAT events per
request.

### The requested change

```python
@dataclass
class Attempt:
    ...
    model: str | None = None

    # CPU throttling and the memory limit (contract request 107, #1689). The
    # ATTEMPT's, every runner combined, as for the CPU figures above. None
    # means NOT MEASURED -- no cgroup throttling counters -- never zero; a
    # measured 0.0 is an attempt that was never throttled.
    throttled_seconds: float | None = None
    peak_throttled_share: float | None = None
    mean_throttled_share: float | None = None
    memory_limit_bytes: int | None = None
```

At the end of the class, for the reason request 15 gives: no positional
construction of an `Attempt` changes meaning. Optional, so every existing
document stays valid and nothing migrates.

**What follows outside the contract, once accepted.** The worker adds the
four to `attempt_cpu_fields` (written with each periodic reading and at each
reap, as the CPU figures are); swarm-api's `attemptstate` allow-list and
`codec.attempt_to_api` serve them; `swarm_api.utilisation` reads them off the
attempt instead of the newest beat. The 5-second points stay on the HEARTBEAT:
they are a series, not an attempt figure.

### If it is declined

Nothing breaks. The figures stay on the HEARTBEAT, and the utilisation route
keeps reading the newest beat's. What is lost is a query across attempts
("which attempts of this profile were throttled last week") without reading
each agent's events.

### Invariants

**7** (requests == limits): unchanged; these MEASURE what the limit did.
**9**: the attempt document is its own tenant's, as today. 1 to 6, 8 and 10
are untouched.

## 108. `profiles.py`, `CONTRACT.md`: invariant 7 amended for CPU; `ResourceClass.cpu_limit` (SIZES-CONTRACT-1, runner sizes request RS-1, part of #1689)

**Status:** ACCEPTED by the owner 2026-10-20 ("CPU limits may be higher than
requests ... memory stays fixed"; `docs/design/runner-sizes.md` §7, Q1 A: the
limit is 2 x the request, the requests unchanged) and APPLIED by the pull
request that adds this line. Design §5 request RS-1, filed here at the next
free number by lane SIZES-CONTRACT-1.

### What is true today

Invariant 7: "requests == limits. No bursting; bursting is what gets
OOM-killed." `ResourceClass` has one `cpu`, documented as "BOTH request and
limit", and `profiles.py`'s module docstring forbids bursting. On 2026-10-20
the day's implement attempts averaged 1.74 of their 4 cores and sat at the
limit 22% of the time, 30% on long runs (#1689).

### The change

```python
#: How far a class's CPU limit may sit above its request.
CPU_BURST_MAX_RATIO = 2


@dataclass(frozen=True)
class ResourceClass:
    """`cpu` is the CPU REQUEST ... `cpu_limit` is what it may burst to on GKE
    Autopilot ... `memory_gib` and `disk_gib` are request AND limit."""

    name: str
    cpu: float
    memory_gib: int
    disk_gib: int
    units: int
    requires_preview_disk: bool = False
    cpu_limit: float | None = None   # None: equal to `cpu`

    @property
    def cpu_limit_cores(self) -> float:
        return self.cpu if self.cpu_limit is None else self.cpu_limit
```

`__post_init__` adds `cpu <= cpu_limit_cores <= CPU_BURST_MAX_RATIO * cpu`.
The Cloud Run ceiling (8 cores, 32 GiB) keeps binding `cpu` and `memory_gib`,
not the limit, so `large` at 8/16 constructs. `RESOURCE_CLASSES` gains
`cpu_limit=8`, `8`, `16` on `standard`, `browser`, `large`; `cpu`,
`memory_gib`, `disk_gib` and `units` are unchanged (§7 Q2 A). The module
docstring's first hard rule becomes: memory and disk requests == limits; a CPU
limit within the ratio, on GKE only.

CONTRACT.md invariant 7 becomes:

> 7. Memory and ephemeral storage: requests == limits, on every container.
>    CPU: request <= limit <= `CPU_BURST_MAX_RATIO` x request, on GKE
>    Autopilot only; Cloud Run renders the request as its single limit.
>    Admission charges, and Autopilot bills, the request. A CPU burst is
>    throttled under contention, never killed; memory never bursts, because
>    a memory burst is what gets OOM-killed. The pod's QoS class is
>    Burstable, so its kernel OOM score is above a Guaranteed pod's; the
>    periodic checkpoint (invariant 8) bounds that kill as it bounds any.

**What follows outside the contract, in later lanes.** Nothing reads
`cpu_limit` yet: `dispatch.py` renders `rc.cpu` as both request and limit, so
every pod is unchanged until SIZES-SCHED-1 renders `cpu_limit_cores` on the
GKE manifest (and keeps the Cloud Run Job at the request). The worker's
fallback limit (`lifecycle.py`, read only when the cgroup cannot be) follows
in SIZES-WORKER-1. `scripts/lib/check-contract-parity.sh` compares each
class's `units` only, and `terraform/infra/locals.tf` mirrors no CPU limit, so
neither changes.

**Tests:** `tests/unit/common/test_cpu_burst_contract.py`: the ratio is 2;
every class's request is unchanged and its limit is twice it; memory and disk
have no limit field; a limit of 2.5 x the request, or below it, is refused at
construction; a limit equal to the request constructs; `large` at 8/16
constructs and a 9-core request is still refused (the ceiling binds the
request); `test_invariant_7_text_names_cpu_only` reads CONTRACT.md; the module
docstring no longer forbids every burst; `test_request_rs_1_is_filed_as_entry_108`.

### Rollback

`cpu_limit` back to `None` on every class: every pod renders requests ==
limits again. A running pod keeps its limit until it ends.

### What it would break

Nothing today, since no renderer reads the limit. When SIZES-SCHED-1 renders
it, the tests that assert requests == limits for CPU on the GKE manifest
(`tests/unit/control_plane/test_dispatch_*.py`,
`test_browser_class_request_79.py`, `test_gke_workspace_fits.py`,
`test_gke_cache_volume.py`, `test_claude_code_browser_profile.py`) change
there, red first, to hold memory and ephemeral storage equal and CPU within
the ratio.

### Invariants

**7** amended as above. **8**: unchanged, and named in 7's text. **1-6, 9,
10** are untouched: admission still charges the request's units.

## 109. `profiles.py`: the frozen runner-size bounds, `units_for` and `check_size` (SIZES-CONTRACT-2, runner sizes request RS-2, part of #1689)

**Status:** ACCEPTED by the owner 2026-10-20 (`docs/design/runner-sizes.md`
§7: every question took its recommendation, and RS-1 to RS-4 were accepted)
and APPLIED by the pull request that adds this line. Design §5 request RS-2,
filed here at the next free number by lane SIZES-CONTRACT-2.

### What is true today

A class's size is a code constant (`RESOURCE_CLASSES`), its `units` a
hand-written weight beside it, and the only bounds are `ResourceClass`'s
Cloud Run ceiling and, since request 108, its CPU burst ratio. Phase 2 of the
design makes sizes a versioned platform setting an admin edits in the
console (§4); nothing frozen says what an admin may set.

### The change

```python
CPU_REQUEST_MIN, CPU_REQUEST_MAX, CPU_REQUEST_STEP = 0.5, 8, 0.25
MEMORY_GIB_MIN, MEMORY_GIB_MAX = 2, 32
MEMORY_PER_CPU_MIN, MEMORY_PER_CPU_MAX = 1.0, 6.5
LIVE_WORKER_MIN = (1, 2)      # cpu request, memory GiB
LIVE_DISPLAY_MIN = (1, 5)
CLASS_ORDER = ("standard", "browser", "large")

def units_for(cpu_request: float, memory_gib: int) -> int:
    return max(math.ceil(cpu_request / 4), math.ceil(memory_gib / 8))

@dataclass(frozen=True)
class SizeRefusal:
    resource_class: str
    field: str
    value: float | None
    bound: str

def check_size(sizes: Mapping[str, ResourceSize]) -> list[SizeRefusal]: ...
```

`CPU_BURST_MAX_RATIO` is request 108's and is reused, not restated.
`check_size` is pure, takes the whole table so it can check `CLASS_ORDER`
(every request dimension -- `cpu_request`, `memory_gib`, `disk_gib` -- is
`standard <= browser <= large`, refused on both classes of the pair), and
returns every refusal rather than the first. It checks §4.2's table: the CPU
request's range and step; the limit between the request and the ratio;
memory an integer in range; memory per core inside Autopilot's 1 ... 6.5;
`disk_gib` an integer, 1 ... `memory_gib` - 2; a live split's worker and
display minimums, each container's own CPU ratio, and the two requests and
memories summing to the class's. Three choices the design left open are
settled here, each the conservative one: a name that is not a class is
refused (an admin resizes, never creates); a class the table omits is judged
as its default, which is what request 110's `effective_size` falls back to;
and a value that is not a finite number (a string, a bool, NaN) is a refusal,
never an exception, because a stored version is judged after it is parsed.
`RESOURCE_CLASSES`' units are checked against `units_for` at import, so a
hand-written weight cannot drift from the formula.

**What follows outside the contract, in later lanes.** Nothing calls
`check_size` yet. SIZES-API calls it on every PUT, preview and restore;
SIZES-SCHED-2 calls it again on read, so a malformed stored version sizes
no pod (it reads as the code defaults).

**Tests:** `tests/unit/common/test_runner_size_bounds.py`: every bound at,
just inside and just outside its edge, looking only at the refusals for the
field under test; the ordering both ways; the live split's minimums at their
edges and each of its refusals; every refusal returned; an unknown class
refused and an omitted one judged as its default; a non-number refused, not
raised; `units_for` at 1 / 1 / 4 for today's classes; a seeded sweep over
5,000 random tables that whatever `check_size` accepts sits inside
Autopilot's 1:1 ... 1:6.5, with the counts visited and accepted asserted;
`test_request_rs_2_is_filed_as_entry_109`.

### What it would break

Nothing that exists: new names only, and the import-time units check holds
for today's three classes.

### Invariants

**7**: the validator is its enforcement for admin-set sizes (memory and disk
carry one number each, so they cannot differ from their request). **10**: the
bounds are frozen code, so neither a caller nor an admin can size a pod
outside them. **1-6, 8, 9** untouched.

## 110. `profiles.py`: `ResourceSize`, `LiveShare`, `default_sizes` and `effective_size` (SIZES-CONTRACT-2, runner sizes request RS-3, part of #1689)

**Status:** ACCEPTED by the owner 2026-10-20 (`docs/design/runner-sizes.md`
§7, Q3 A: Cloud Run profiles stay at the code defaults) and APPLIED by the
pull request that adds this line. Design §5 request RS-3, filed here at the
next free number by lane SIZES-CONTRACT-2.

### What is true today

There is no shape for a size other than `ResourceClass`, which carries a
name, a hand-written weight and the preview-disk flag, and is validated at
construction -- so a stored version that broke a bound could not even be
parsed to say which.

### The change

```python
@dataclass(frozen=True)
class LiveShare:
    cpu_request: float
    cpu_limit: float
    memory_gib: int

@dataclass(frozen=True)
class ResourceSize:
    cpu_request: float
    cpu_limit: float
    memory_gib: int
    disk_gib: int
    live_split: tuple[LiveShare, LiveShare] | None = None   # (worker, display)

    @property
    def units(self) -> int:
        return units_for(self.cpu_request, self.memory_gib)

def default_sizes() -> Mapping[str, ResourceSize]: ...
def effective_size(name: str, version: Mapping[str, ResourceSize] | None,
                   backend: Backend) -> ResourceSize: ...
```

`default_sizes()` is `RESOURCE_CLASSES` number for number, read-only, with
the browser class's live split (worker 2 / 4 / 3 GiB, display 2 / 3 / 5 GiB,
design §4.1) -- the pod the scheduler's `LIVE_DISPLAY_SHARE` renders today;
`standard` and `large` carry none (§4.4). `effective_size` returns the
version's size for `GKE_AUTOPILOT`, the default for `CLOUD_RUN_JOB` whatever
the version says, the default for a class the version omits, and refuses an
unresolved `AUTO` (`resolve_backend` first). `ResourceSize` validates
nothing at construction; request 109's `check_size` judges it.

`scripts/lib/check-contract-parity.sh` section 5's unit-weights check now
compares the UI's bundled `RESOURCE_UNITS` to `default_sizes()` and reports
itself as `RESOURCE_UNITS (code defaults)`: once a class can be resized the
bundled copy can only be the defaults, and the live weights are served by
`GET /v1/resource-classes` (SIZES-API).

**Tests:** `tests/unit/common/test_runner_size_shape.py`: the shape's fields;
both classes frozen; `units` derived, not stored; `default_sizes()` equals
`RESOURCE_CLASSES`, names every class and nothing else, and cannot be edited
by a reader; only `browser` has a default split, and it is the pod
`scheduler.dispatch` renders from `LIVE_DISPLAY_SHARE`; `effective_size`'s
four cases and its refusal of `AUTO` and of an unknown class; the parity
script reads the defaults; `test_request_rs_3_is_filed_as_entry_110`.

### What it would break

Nothing that exists: new names only. The parity check's report line is
renamed; no script parses that name.

### Invariants

**10**: the backend, not a caller, decides whether a size follows. **2**:
the shape SIZES-SCHED-2 charges units from is the one it renders the pod
from, so the lease and the pod agree by construction. **1, 3-9** untouched.

## 111. `models.py`: `Attempt.size_version` and the `sized_*` fields (SIZES-CONTRACT-2, runner sizes request RS-4, part of #1689)

**Status:** ACCEPTED by the owner 2026-10-20 (`docs/design/runner-sizes.md`
§7) and APPLIED by the pull request that adds this line. Design §5 request
RS-4, filed here at the next free number by lane SIZES-CONTRACT-2.

### What is true today

`Attempt.cpu_limit_cores` is what the worker MEASURED (`cpu.max`, else the
catalogue). Nothing records what the scheduler RENDERED, and after a resize
the catalogue no longer says what an older attempt ran at.

### The change

```python
    size_version: int | None = None
    sized_cpu_request: float | None = None
    sized_cpu_limit: float | None = None
    sized_memory_gib: int | None = None
    sized_disk_gib: int | None = None
    sized_live_split: dict[str, dict[str, float]] | None = None
    # {"worker":  {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 3},
    #  "display": {"cpu_request": 2, "cpu_limit": 3, "memory_gib": 5}}
```

At the end of the class, so no positional construction changes meaning.
`Attempt.__post_init__` (new) calls `check_sized_live_split`, which refuses
anything but None or a map of exactly `worker` and `display`, each of exactly
`cpu_request`, `cpu_limit` and `memory_gib`, each a number (a bool is not).
A mapping, not request 110's `LiveShare` pair, because the attempt is
Firestore JSON and a named key survives a reader that does not know the
pair's order.

**What follows outside the contract, in later lanes.** No writer yet:
SIZES-SCHED-2's `create_attempt` writes them, from the size the lease
charged. **No reader yet either, and no lane in the design's §6 lists the
one that has to change:** `swarm_api/codec.py::attempt_from_dict` reads
every field explicitly (its own comments record dropping the spend fields
that way once), so it will drop these until it names them, and because
`Attempt` now refuses a malformed `sized_live_split` at construction, that
decoder must pass the stored value through `check_sized_live_split` (or
drop a malformed one to None) rather than hand it to `Attempt` raw. Raised
as a question on the pull request that adds this line.

**Tests:** `tests/unit/common/test_attempt_sized_fields.py`: the fields
default to None and are the last of the class; a plain and a live attempt
round-trip through `asdict` and back, the live one with both keys; nine
malformed live splits are refused; no sized field is a `Task` field, and a
task document carrying them signs the same step-spec bytes
(`canonical_step_spec`), so they are outside the signature by construction
-- the worker acts on its cgroup, not on them;
`test_request_rs_4_is_filed_as_entry_111`.

### What it would break

Nothing: optional fields, and the new check refuses only a value no writer
produces.

### Invariants

**5**: the record is per attempt, so a retried agent's new size never
rewrites an old attempt's. **7**: the record is what makes "a running agent
keeps its admitted size" checkable after the fact. **1-4, 6, 8-10**
untouched.

## 112. `CONTRACT.md`: "Primary backend Cloud Run Jobs" no longer says where agents run (DOCS-TRUTH, part of #1822)

**Status:** open, REQUESTED 2026-10-21 by lane DOCS-TRUTH, from the
2026-10-21 system analysis (epic #1822). Not applied: `CONTRACT.md` is the
document these requests are addressed to, so a lane files the drift here and
does not edit it.

### What is true today

`CONTRACT.md`, "Platform decisions", reads "Primary backend Cloud Run Jobs.
GKE Autopilot only for browser/GPU/>32GiB, and for claude-code since contract
request 53", and names `mock`, `generic` and `codex` as Cloud Run profiles.
Its "Pod" bullet speaks of "the four Cloud Run profiles" and of a Kubernetes
pod "for `browser`".

The catalogue (`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`) and
the live system, read with read-only `gcloud` and `kubectl` on 2026-10-21,
say otherwise:

* GKE Autopilot runs `browser`, `claude-code`, `claude-code-browser`,
  `codex`, `generic`, `indexer` and `merge`: contract request 53 moved
  `claude-code` (2026-10-08), 63 `indexer` and 64-66 `generic`, `codex` and
  `merge` (2026-10-10).
* Cloud Run Jobs runs `mock`, the disabled `post-verdict` and
  `claude-code-review`, and the fallback Jobs Terraform keeps for
  `claude-code`, `indexer`, `generic`, `codex` and `merge`
  (`cloud_run_fallback_profiles` in `terraform/infra/locals.tf`), which
  nothing dispatches to while those profiles name GKE.
* On GKE the workspace, `/tmp` and HOME are disk `emptyDir`s plus a 20 GiB
  cache PVC (`apps/scheduler/scheduler/dispatch.py::CACHE_VOLUME_GIB`). The
  "Correction (workspace storage)" at the foot of `CONTRACT.md`, that
  workspaces are memory-backed tmpfs, is true of Cloud Run only.

The code is right and the contract text is behind it, so nothing misbehaves;
a reader who takes the contract at its word plans for the wrong backend.

### The requested change

Replace the "Primary backend Cloud Run Jobs" bullet with what the catalogue
declares: GKE Autopilot runs every agent profile but `mock` (contract
requests 53 and 63-66, with the dates), Cloud Run Jobs runs `mock`, the two
disabled worker-action profiles and the fallback Jobs. Keep the "Why"
clause as history of the first decision, as `docs/architecture.md` §5 now
does. Amend the "Pod" bullet to say a Kubernetes pod for every GKE profile.
Add one sentence to "Correction (workspace storage)" that the tmpfs
workspace is Cloud Run's, and that GKE's is disk; leave the rest of the
correction, the Preview disk and live migration, exactly as it stands.

### What it would break if accepted

Nothing in code: no reader parses `CONTRACT.md`'s backend bullet. Docs that
quote it would follow it; `docs/architecture.md`, `docs/execution-backends.md`,
`docs/checkpointing.md` and `docs/BUILD_PROMPT_V2.md` were brought up to date
by the pull request that files this.

### If it is declined

The contract keeps a backend split that has not been true since 2026-10-10,
next to the invariants, which is how the decisions it records get argued
from a false premise. Every doc that restates the split would then be the
better source than the contract it defers to.

---

## 113. `profiles.py`: a `claude-code-large` runner profile (AUTONOMY-DESIGN, part of #1933)

**Status:** ACCEPTED by the owner 2026-10-22, requested the same day by lane
AUTONOMY-DESIGN (`docs/design/agent-autonomy.md` §3.11, question Q6). Owner
decision, 2026-10-22: "Accept contract request 113: a claude-code-large
profile". APPLIED by lane AU9-PROFILE-LARGE (part of #1933), in the pull
request that rewrote row 113 of the index above: one entry in
`RUNNER_PROFILES`, a literal `RunnerProfile(...)` beside `claude-code`
(the Terraform catalogue mirror reads the file as text), held equal to
`claude-code` in every other field by
`tests/unit/common/test_claude_code_large.py`.

### What is true today

Every code profile in `apps/common/swarm_common/profiles.py::RUNNER_PROFILES`
names the `standard` class, 8 GiB with requests equal to limits, and
`claude-code` has `timeout_seconds=7200`. The `large` class (8 CPU requested,
32 GiB) exists and "is reached by no profile today"
(`docs/design/runner-sizes.md` §4.1), and a step can only shrink its class,
never grow it. On 2026-10-22, #1926 measured 8 GiB out-of-memory kills on
`claude-code`, and three agents were killed at the 7,200 s timeout ("runner
exceeded its 7200s timeout and was killed", `agent_worker/lifecycle.py`). An
agent that needs more memory or time has no profile to be given by name.

### The requested change

Add one entry, `claude-code-large`: `claude-code`'s fields unchanged except
`resource_class="large"` and `timeout_seconds=14400`. GKE Autopilot,
on-demand only (invariant 6), memory request equal to limit (invariant 7),
the checkpoint interval of the catalogue's default (invariant 8). Callers
pick it by name (invariant 10); a planner or `failure-review`'s fix run names
it for a lane whose cause was an out-of-memory kill or a timeout.

### What it would break if accepted

Nothing that exists: a new key in the catalogue. The tenants' admission
ceilings count `large` at its 4 units (`runner-sizes.md` §4.1), so a tenant
with a small `large` ceiling queues these agents rather than running them.
Docs that list the profiles (`docs/design/runner-sizes.md` §1a.3, the runner
input tables) follow it.

### If it is declined

Heavy lanes keep dying at 8 GiB and 7,200 s, and the alternative is raising
`standard` itself through a runner-size version (`runner-sizes.md` §4.5),
which every standard agent pays for.

---

## 114. `CONTRACT.md` invariant 7: swarm-api's startup CPU boost, the one recorded exception (API-CPU-BOOST, part of #1816)

**Status:** ACCEPTED by BGD on 2026-10-23 (~17:37Z; #1816 option (a)) and
APPLIED by PR 2132 (lane API-CPU-BOOST). Recorded here the same day by lane
FOLLOWUPS-1023 (part of #2150), because PR 2132 asked for this entry and its
file was outside that lane. This is an exception to invariant 7 for one
service, not an amendment of it: the invariant's text in `CONTRACT.md` and
`CLAUDE.md` stays exactly as request 108 left it.

### What is true today

swarm-api, a Cloud Run service with a 1 vCPU limit, starts with Cloud Run's
startup CPU boost on: `terraform/infra/main.tf` sets
`startup_cpu_boost = true` in swarm-api's entry and nowhere else, and
`terraform/modules/cloud_run/main.tf` passes each service's
`startup_cpu_boost` (default `false`, `terraform/modules/cloud_run/variables.tf`)
to its revision. While an instance starts, the boost gives it more CPU than
its declared limit. That is a burst on Cloud Run, which invariant 7 does not
allow there: "Cloud Run renders the request as its single limit".

### Why it was allowed

Startup latency of the API, and of nothing else. swarm-api's Python start on
1 vCPU sometimes ran past its startup probe: four instances were killed at
96 s on 2026-10-21, and callers got Cloud Run's 429 "no available instance"
(#1816). The boost covers only the start: once the instance is serving it
runs at its 1 vCPU limit like every other service. It costs about $1 a month
(35-80 starts a day, about 30 boosted vCPU-seconds each). The burst
invariant 7 guards against is a worker's. An agent's memory burst is what
gets OOM-killed, and an agent's CPU is what admission charges. swarm-api
holds no lease, runs no agent and is charged to no pool, and its memory
stays at its limit while it is boosted.

### What it does not change

* **every other Cloud Run service** keeps the module's `false`: swarm-scheduler,
  swarm-quota-broker, swarm-reconciler and swarm-ui in `terraform/infra/main.tf`,
  and `github-hooks` and `mcp-test-server`, which hard-code
  `startup_cpu_boost = false` (`terraform/infra/github_hooks.tf`,
  `terraform/infra/mcp_test_server.tf`).
* **every worker** stays under invariant 7 as written: memory and ephemeral
  storage request == limit on every container, and CPU at the request on a
  Cloud Run Job or within `CPU_BURST_MAX_RATIO` of it on GKE Autopilot
  (request 108). A Cloud Run Job has no startup boost, and no worker
  manifest asks for one.
* `cpu_idle` stays `false` on every service, swarm-api included, so CPU
  stays allocated for the whole of a request.
* The tests hold all three: `tests/terraform/cloud_run.tftest.hcl`
  (`a_service_that_sets_startup_cpu_boost_gets_it_and_no_other_does`,
  `the_infra_root_boosts_swarm_api_and_nothing_else`, and the file-level
  invariant-7 assertion), `github_hooks.tftest.hcl` and
  `mcp_test_server.tftest.hcl` fail if another service turns the boost on.
  `docs/architecture.md` names the exception in its invariant-7 section.

### Rollback

Delete `startup_cpu_boost = true` from swarm-api's entry in
`terraform/infra/main.tf` and apply: its next revision starts unboosted, and
the slow starts of #1816 come back with it. Nothing else reads the flag.

### Invariants

**7**: one recorded exception, for swarm-api's start only. The invariant's
text is unchanged and every other container is held to it. **1-6 and 8-10**
are untouched: swarm-api holds no capacity, and no runner profile, resource
class or admission charge changes.

---

## 115. `models.py`: `Task.publish_to_pr` (PUBLISH-TO-PR, part of #2183)

**Status:** open, REQUESTED 2026-10-24 by lane PUBLISH-TO-PR. Not applied:
`apps/common/swarm_common/` is frozen. Nothing waits on it; the feature
ships without it.

### What is true today

A workflow spec or a single agent may name an existing pull request,
`publish_to_pr: <N>` (BGD's approval of #2183, 2026-10-24). swarm-api checks
it at submission (`swarm_api/publishtopr.py`) and stores what it checked in
every step's `metadata.dispatch` as `publish_to_pr: {"number": N, "task_id":
"<the task whose swarm/ branch is the pull request's head>"}`, inside the
block the step-spec signature covers. The worker reads it only from the
verified document (`agent_worker/publishpr.py`). `Task` has no field for it,
exactly as it has none for `strategy`, `carrier` or `continues`.

### The requested change

Add `publish_to_pr: PublishTarget | None = None` to `Task`, with
`PublishTarget(number: int, task_id: str)`, signed like every other field,
and read by the worker from there instead of the dispatch block.

### What it would break if accepted

Nothing at once: the dispatch block's key would be read as a fallback for
agents stored before the field, then retired. Every reader of the block
(`lifecycle._publish_to_pr`, `verdictpublish._dispatch_facts`) moves to the
field in the same change.

### If it is declined

The field stays in `metadata.dispatch`, where the other integration choices
already live, and nothing changes. The cost is the one #263's `continues`
already pays: a reader has to know the block to find it.

### Invariants

**10** holds either way: the caller names a pull request number, never a
branch, image, command or backend parameter. **9**: the pull request is read,
and its branch pushed, with the tenant's own forge credential, and its head
branch must be a lane branch of the submitting tenant. **1-8** are untouched.

## 116. `specsign.py`: a `"qa"` block in `SETTINGS_MAC_FIELDS`, for the workspace's QA batch size (QA-TRIPWIRE-MAC, part of #2136)

**Status:** ACCEPTED by BGD 2026-10-26, answering lane QA-FLOWS-CAP's
question ("Should a 'qa' MAC block be added to the frozen contract?",
task_23a1c31d528e450187b8, answered 08:26Z: "Request a 'qa' block").
Filed by lane QA-TRIPWIRE-MAC. Not applied: `apps/common/swarm_common/` is
frozen, and this lane does not edit it.

### What is true today

QA-FLOWS-CAP (PR 2409) adds a per-workspace default for how many read-only
flows one live-browser step runs, `qa_flows_per_step` (1-30, default 15),
stored in `workspace_settings/{tenant_id}` beside `judgment_backend`,
`merge` and `code_intelligence`, and written by
`apps/swarm-api/swarm_api/qasettings.py`. A QA run that names no
`flows_per_step` of its own, and every scheduled `visual-qa` firing, reads
it. No block of `SETTINGS_MAC_FIELDS` covers it, so it is not signed.

### Why

[tenant-settings-integrity.md](design/tenant-settings-integrity.md) §1.3:
any tenant worker can write `workspace_settings/<B>` (rows 5-7 are the
fields already there), and the QA batch size is now read from that
document. So tenant A's worker can choose tenant B's batch size. The harm
is bounded, because PR 2409's reader treats any stored value other than an
int in 1-30 as unset, and a run's own value overrides the workspace's. A
forger can choose only a batch size B's admin could have chosen. But that
choice still changes B's runs: a batch of 30 gives each flow 240 s rather
than 480 s (2409's timeout arithmetic), and it changes how many steps and
how much spend a leg takes. It is the same kind of write as row 6's
`merge.*`, which is signed for that reason.

### The change

```python
SETTINGS_MAC_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "top": ("judgment_backend",),
    "merge": ("mode", "train_size", "max_train_age_minutes"),
    "code_intelligence": ("jev", "context", "context_ab", "graph_delivery",
                          "judgment_backend"),
    "qa": ("flows_per_step",),
})
```

Nothing else in `specsign.py` changes: `canonical_settings_block` already
takes any listed block, and the block name is inside the bytes, so a `qa`
MAC cannot be replayed onto another block or tenant.
`tests/unit/common/test_settings_block.py`, which pins the mapping, gains
the entry in the same change.

### The follow-up once it is applied

* `qasettings` stores the value as a `qa` map, `qa: {flows_per_step, mac}`,
  and signs it on every write with `judgment.sign_block(..., block="qa")`,
  as `mergesettings.py` signs `merge`.
* Its reader takes the block through `judgment.trusted_block(tenant_id,
  "qa", ...)`, so a tenant on the MAC cutover whose `qa` block does not
  verify gets the default, 15. The 1-30 bound stays as a second check.
* `judgment.WRITER_FIELDS` gains `"qa": ("flows_per_step",)`
  (`tests/unit/control_plane/test_settings_mac.py` holds it equal to the
  contract's blocks), so `scripts/resign-workspace-settings.sh` signs a
  stored `qa` block like the others.
* The value PR 2409 stores at the top level, `qa_flows_per_step`, is read
  as a fallback only for a tenant not yet on the cutover, then retired.
* §1.3 of the design gains a row for `workspace_settings/<B>.qa.*`.

### What it would break if accepted

Nothing at once. A new block changes no other block's bytes, so every MAC
stored today still verifies. Until the follow-up lands, no writer signs a
`qa` block, and `trusted_block` is not called for it.

### If it is declined

The batch size stays unsigned, and the 1-30 bound is its only defence: a
forger picks B's batch size within what B's admin could pick.

### Invariants

**9** is what this protects: one tenant's worker must not set another
tenant's settings. **10** holds either way: a caller names a bounded
integer, never a spec. **1-8** are untouched.

## 117. `profiles.py`: a `cf-access` secret slot on `claude-code-browser` (CF-ACCESS-TOKEN, part of #2442)

**Status:** ACCEPTED by the owner (BGD) 2026-10-26 (filed by lane CF-ACCESS-TOKEN
as 116 and renumbered, since QA-TRIPWIRE-MAC took 116 on main first). APPLIED by lane
CF-ACCESS-CONTRACT (part of #2442), in the pull request that rewrote row 117
of the index above: `_CF_ACCESS_SLOT` in `profiles.py`, worded as below, and
`claude-code-browser`'s `secret_slots` is `{"files": _FILES_SLOT,
"cf-access": _CF_ACCESS_SLOT}`. Nothing beyond the request. The tests that
added the slot for their own run now read it from the catalogue
(`tests/unit/control_plane/test_cf_access_slot.py`,
`tests/unit/worker/test_cf_access_worker.py`,
`tests/unit/common/test_workspace_secret_contract.py`), and hold that no
other profile takes it. "What is true today" below is the state the request
was filed against.

### What is true today

BGD decided on 2026-10-26 that browser visual-QA agents reach a site behind
Cloudflare Access with a Cloudflare Access service token
(docs/runbooks/cf-access-service-token.md). The lane built every reader of
it: swarm-api stores a host list on a grant into a host-scoped slot and signs
it in grant form "v3" (`workspace_secrets.HOST_SCOPED_SLOTS`,
`grant_attest.FORM_HOSTS`), the release answers the item with its `hosts`,
the worker holds it in its heap and hands it to the live gate
(`secretrelease.GATE_SLOTS`, `agent_worker/cf_access.py`), and the gate sends
`CF-Access-Client-Id` / `CF-Access-Client-Secret` to listed hosts only
(`livegate/cfaccess.py`). `claude-code-browser` declares one slot, `files`,
so `check_grant` refuses a grant into `cf-access` and the worker refuses an
item in it.

### The requested change

In `profiles.py`, beside `_FILES_SLOT`:

    _CF_ACCESS_SLOT = SecretSlot(
        kinds=("oauth_client",),
        means="a Cloudflare Access service token ({client_id, client_secret}); the live "
              "gate sends it as CF-Access-Client-Id and CF-Access-Client-Secret to the "
              "hosts its grant lists and to no other; never a file, never in the "
              "agent's environment",
    )

and on `claude-code-browser` only:
`secret_slots={"files": _FILES_SLOT, "cf-access": _CF_ACCESS_SLOT}`. Optional
(`required=False`): a step whose workspace granted none starts and reaches
the site signed out.

### What it would break if accepted

Nothing that exists. The console's grant matrix gains a column for the slot;
until the console has a host-list field, a grant into it is set through the
API (`hosts` in `PUT .../grants/{consumer}`), and a tick without hosts is
refused with a message saying why. `check-contract-parity.sh` is unaffected:
no shell restates the slot list. `consumer_slots` in the worker and
`attempt_consumers` in swarm-api pick the slot up from the catalogue.

### If it is declined

The headers are never sent and the platform admin stays unreachable to
agents, as today. The alternatives are worse: the `files` slot would hand
the token to the agent, and a slot declared outside the contract would be a
second statement of the catalogue in swarm-api and the worker, the drift
rule 1 exists to prevent.

### Invariants

**9** is the point: the token is the workspace's own secret, released only
to that workspace's attempts, and held by its gate, never by the agent.
**10** holds: a caller names nothing; the admin's host list is a property of
the grant, signed by swarm-api. **1-8** are untouched.
## 118. `models.py`: `EndCause.BUDGET_EXCEEDED` (BUDGET-CAP, part of #2459)

**Status:** ACCEPTED by the owner (BGD) 2026-10-26 (~22:15Z, on PR 2466) and APPLIED
2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part
of #2459). Requested 2026-10-26 by lane BUDGET-CAP. "What is true today" below
is the text as filed, the state before it was applied.

**As applied (ledger), 2026-10-27, lane LEDGER-BUDGET-CLASS (part of #2459).**
The outcome ledger classes the member as its own failure class,
`budget_exceeded` ("budget exceeded"), after `publish_refused` and before
`other` in `swarm_api.outcomes.FAILURE_CLASSES`, not as `runner_error`: BGD's
decision, 2026-10-27 (~01:30Z), on the owner question of PR 2519
(BUDGET-CONTRACT). `DERIVE_VERSION` is 11, so every stored day counted under
10 is re-derived. No text rule was added (`CLASSIFIER_VERSION` stays 3): an
attempt with a null cause and a `budget_exceeded:` `last_error` still reads as
`runner_error`.

### What is true today

BGD's decision on #2459 (2026-10-26): at 100% of its budget the worker stops
the agent at its next turn boundary, checkpoints, publishes what it has, and
ends the step FAILED "with the end cause `budget_exceeded`". `EndCause` has
no such member. Until it does, the worker
(`agent_worker/lifecycle.py::Worker._record_finalised`, `agent_worker/budget.py`)
ends the step FAILED with `end_cause` null -- `EndCause`'s own docstring rule
for "a writer ended it for a reason with no value here" -- with `last_error`
beginning `budget_exceeded:` and `result_summary.budget` carrying
`end_cause: "budget_exceeded"`, the limits and what was used.

### The change

```python
class EndCause(str, Enum):
    ...
    BUDGET_EXCEEDED = "budget_exceeded"   # the worker stopped the agent: it spent its budget
```

written only by the worker, beside `TIMEOUT`, and listed in the docstring's
worker line. Never retried by the worker: another attempt of the same step
starts from the same budget, so a resume is a person's choice.

### What it would break if accepted

Nothing at once. The outcome ledger (`swarm_api.outcomes`) falls back to its
text classifier for a null cause today, so a budget stop reads as a runner
failure there; with the member it is its own class. The worker switches from
null to the member in the same change, and the ledger and the console read
`budget_exceeded` from the field instead of the summary.

### If it is declined

The cause stays in `last_error` and `result_summary.budget.end_cause`, and
the ledger classifies the text. A reader that branches on `end_cause` sees
null for a budget stop.

### Invariants

**1** holds either way: the stopped step is FAILED, its lease released in the
same write, and it holds nothing. **5**: the end is written under the
attempt's generation like every worker end. **2-4, 6-10** are untouched.

## 119. `specsign.py`: a `"budget"` block in `SETTINGS_MAC_FIELDS` (BUDGET-CAP, part of #2459)

**Status:** ACCEPTED by the owner (BGD) 2026-10-26 (~22:15Z, on PR 2466) and APPLIED
2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part
of #2459). Requested 2026-10-26 by lane BUDGET-CAP. "What is true today" below
is the text as filed, the state before it was applied.

### What is true today

A workspace's per-agent budget default is `workspace_settings/{tenant_id}.
budget: {unit, amount, updated_at, updated_by}`, written by
`apps/swarm-api/swarm_api/budget.py::BudgetStore` beside `merge`, `qa` and
`code_intelligence`. No block of `SETTINGS_MAC_FIELDS` covers it, so it is
not signed, and the reader acts on it only when it is a shape the writer
could have written (a unit of `usd` or `tokens`, a positive amount in
bounds); anything else is no default.

### Why

[tenant-settings-integrity.md](design/tenant-settings-integrity.md) §1.3:
any tenant worker can write `workspace_settings/<B>`. A forger can remove
B's default -- so B's agents run with no limit -- or move it anywhere under
the platform ceiling. Bounded: a step's own budget is inside the step-spec
signature, and the ceiling lives in `control/settings`, not B's document. It
is the same kind of write `merge.*` is signed against.

### The change

```python
SETTINGS_MAC_FIELDS = MappingProxyType({
    ...,
    "budget": ("unit", "amount"),
})
```

Nothing else in `specsign.py` changes. `tests/unit/common/test_settings_block.py`
gains the entry.

### The follow-up once it is applied

None in `budget.py`: `SIGNED = MAC_BLOCK in SETTINGS_MAC_FIELDS` turns the
writer's `block_mac` and the reader's `trusted_block` on with the listing.
`judgment.WRITER_FIELDS` gains `"budget": ("unit", "amount")` in the same
change, so `scripts/resign-workspace-settings.sh` signs stored blocks, and
§1.3 of the design gains a row.

### What it would break if accepted

Nothing at once: a new block changes no other block's bytes. A tenant on the
MAC cutover whose stored `budget` block was written before the re-sign reads
as no default until the re-sign pass signs it.

### If it is declined

The default stays unsigned and its shape check is its only defence.

### Invariants

**9** is what this protects. **10** holds either way: a unit and a number,
never a resource. **1-8** are untouched.

## 120. `models.py` / `specsign.py`: `Task.budget` and `WorkflowStep.budget` (BUDGET-CAP, part of #2459)

**Status:** ACCEPTED by the owner (BGD) 2026-10-26 (~22:15Z, on PR 2466) and APPLIED
2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part
of #2459). Requested 2026-10-26 by lane BUDGET-CAP. "What is true today" below
is the text as filed, the state before it was applied.

### What is true today

An agent or a workflow step may send `budget: {usd: N}` or `{tokens: N}`
(`swarm_api.schemas.StepBudget`). swarm-api resolves the attempt's budget at
submission, min(step, workspace default, platform ceiling) per unit, and
pins it in `metadata.dispatch.budget: {usd?, tokens?, from: {step,
workspace, ceiling}}`, inside the block the step-spec signature covers
(`SIGNED_METADATA_KEYS`). The worker reads it only from the verified
document (`agent_worker.budget.Limits.from_dispatch`). `WorkflowStep`
records no budget, so the workflow's own document does not say what each
step asked for; the step's agent does.

### The change

`Task.budget: dict[str, float | int] | None = None` (the RESOLVED limits,
`usd` and/or `tokens`) and `WorkflowStep.budget` (what the step asked for),
with a spec format that covers `Task.budget`, signed at that format only
when it is set -- as request 104 did for `model_class`.

### What it would break if accepted

Nothing at once: the dispatch block's key is read as a fallback for agents
stored before the field, then retired.

### If it is declined

The budget stays in `metadata.dispatch`, beside `allow_empty_diff` and
`publish_to_pr`, and nothing changes.

### Invariants

**10** holds either way: a budget only lowers a limit and chooses no image,
command, resource or backend, as `timeout_seconds` does. **1** holds: it
holds no capacity. **2-9** are untouched.

## 121. `states.py`: `EventType.BUDGET_WARNING` (BUDGET-CAP, part of #2459)

**Status:** ACCEPTED by the owner (BGD) 2026-10-26 (~22:15Z, on PR 2466) and APPLIED
2026-10-27 by the pull request that adds this line (lane BUDGET-CONTRACT, part
of #2459). Requested 2026-10-26 by lane BUDGET-CAP. "What is true today" below
is the text as filed, the state before it was applied.

### What is true today

At 80% of its budget the worker records a `budget_warning` event the
console can show (BGD, #2459). `EventType` has no such member, so it is
written as `running` with `detail: {cause: "budget_warning", percent, told,
limit, used}`, as `account_assigned` rides on `running` (request 44).

### The change

`EventType.BUDGET_WARNING = "budget_warning"`, written only by the worker.

### What it would break if accepted

Nothing at once: readers that look for `running` with the cause read the
new type as well for one release, then only it.

### If it is declined

The warning stays a `running` event with a cause, which every reader of the
timeline must know to look for.

### Invariants

None is touched: an event records, it decides nothing.

## 122. `models.py`: `Tenant.principal`'s example is a fictional address on the company's real domain (EXAMPLE-IDENTITIES, part of #1647)

**Status:** open, REQUESTED 2026-10-27 by lane EXAMPLE-IDENTITIES (bundle
B03). Not applied: `apps/common/swarm_common/` is frozen. #1647's triage
asked for this entry in place of the edit; the edit waits for a decision.

### What is true today

`apps/common/swarm_common/models.py`, `class Tenant`, field `principal`
(line 832 on 2026-10-27) carries a trailing comment naming two examples:
the deployment's real group `eng@saga.xyz`, and a fictional `alice@` on the
same domain. The group stays. The person is fictional, and #1647's owner
decision (2026-10-19) is that example data uses the RFC 2606 family: every
other fictional `alice@` on that domain in `tests`, `docs`, `scripts` and
`apps` has moved to `alice@example.com`, so this comment is the one place `grep -rnoiE
'(alice|other|u-carol|u-eng|group)@saga\.xyz' tests docs scripts apps` still
finds in the code (with `tests/terraform/tenancy_worker_firestore.tftest.hcl`
outside this lane's territory, noted on the PR).

### The change

The comment only, nothing a program reads:

    principal: str                          # eng@saga.xyz | alice@example.com

### What it would break if accepted

Nothing. It is a comment; no type, default, field or document shape moves,
and `scripts/lib/check-contract-parity.sh` reads no comment.

### If it is declined

The frozen contract keeps one fictional person on the company's domain, and
the #1647 guard grep has a permanent known exception to explain.

### Invariants

None is touched: a comment decides nothing.

## 123. `states.py` / `models.py`: `ParkReason.PLATFORM_REFUSED` and `Task.platform_refusals` (PLATFORM-REFUSED, part of #1820)

**Status:** ACCEPTED by BGD 2026-10-28 (~11:10Z) and APPLIED 2026-10-28 by
the pull request that adds this status (lane PLATFORM-REFUSED-APPLY, part of
#1820), after swarm-tenant-eng's Job quota (`count/jobs.batch` 240) filled
with finished Jobs on 2026-10-28 ~09:30-11:15Z and 28 steps FAILED
`gke_job_quota_exceeded`. Applied in `apps/common/swarm_common/`:
`ParkReason.PLATFORM_REFUSED`, `Task.platform_refusals: int = 0`,
`models.PLATFORM_REFUSED_MAX_PARKS = 5` and the transition LEASED -> PARKED
(the failed-dispatch path parks an agent it found LEASED; no other edge was
needed: PARKED -> READY, CANCELLED and DEAD_LETTERED already existed).

**Two points of "The scheduler, once applied" below were REPLACED by BGD's
brief of 2026-10-28, which applies the standing rule of 2026-09-14 ("never
fail workflows when quotas are reached. just pause them and resume them when
new quota is refreshed"):**

* the backoff is 30 s doubling, held at 10 minutes (30 s, 1, 2, 4, 8, 10,
  10, ...), not 1-16 minutes; it is `scheduler/store.py`
  `platform_refusal_backoff`, so no `platform_refusal_backoff` was added to
  `models.py`;
* past `PLATFORM_REFUSED_MAX_PARKS` consecutive refusals the agent is NOT
  ended FAILED: it stays PARKED/`PLATFORM_REFUSED`, retried every 10 minutes,
  logged at ERROR as trouble (and listed by `swarm_trouble`, which lists every
  park but DEPENDENCY_INCOMPLETE). The refusal never fails an agent.

The rest is as written: the lease is released in the parking transaction,
`attempt_count` is put back, `platform_refusals` is incremented, a successful
dispatch resets it to 0, a sweep beside `_promote_scheduled_retries`
(`_promote_platform_refusals`) promotes a due park, and each park counts in
`swarm_scheduler_platform_refusals_total{code}`. The finished-Job TTL was
shortened from 3600 s to 600 s by the same pull request (BGD, 2026-10-28).

The text below is the request as it was proposed. The precise codes it parks on are built without it
(`apps/scheduler/scheduler/dispatch.py` `forbidden_code`); the design is
[docs/design/dispatch-refusals.md](design/dispatch-refusals.md).

### What is true today

A 403 on GKE Job create is a `DispatchError`, and the drain answers every
`DispatchError` with `SchedulerStore.return_to_ready_after_failed_dispatch`:
READY at `now + 30 s`, FAILED at `max_attempts`. On 2026-10-20 19:24-20:06Z
the admission policy `swarm-worker-job-constraints` refused every CPU-burst
Job; 54-56 dispatches failed, 11-13 agents ended FAILED `dispatch_failed`
about two minutes after submission, and three merge agents used 10 of 10
attempts without running once. A full Job quota (`count/jobs.batch`, 200 per
tenant namespace) would do the same to every agent past the 200th.

From this lane on, those 403s carry `gke_admission_refused` and
`gke_job_quota_exceeded` instead of `gke_create_job_forbidden`
(`PLATFORM_REFUSAL_CODES`), but they are still retried and failed as above:
`ParkReason` has no member that says "the platform refused this dispatch, try
later", and `Task` has nowhere to count how often it has.

### The change

```python
# states.py, class ParkReason
#: The platform refused this task's dispatch -- a cluster admission
#: policy or webhook, or the tenant namespace's ResourceQuota
#: (`gke_admission_refused`, `gke_job_quota_exceeded`). Written by the
#: scheduler's failed-dispatch path with the lease released; promoted by
#: the scheduler once `next_eligible_at` passes. Holds no capacity
#: (invariant 1). Contract request 123.
PLATFORM_REFUSED = "PLATFORM_REFUSED"

# models.py, class Task (with to_dict/from_dict, default 0)
#: Consecutive dispatches the platform refused (PLATFORM_REFUSED parks).
#: Reset to 0 by a dispatch that succeeds. Contract request 123.
platform_refusals: int = 0

# models.py, beside retries_exhausted
PLATFORM_REFUSED_MAX_PARKS = 5
def platform_refusal_backoff(parks: int) -> timedelta:
    """Park n (1-based) waits 2**(n-1) minutes: 1, 2, 4, 8, 16."""
```

The scheduler, once applied:

* a `DispatchError` whose code is in `PLATFORM_REFUSAL_CODES`, on an agent
  with `platform_refusals < PLATFORM_REFUSED_MAX_PARKS`, is written
  PARKED/`PLATFORM_REFUSED` in the same transaction and under the same guards
  as today's READY return, with the lease released, `attempt_count` restored
  to its pre-lease value (the refusal spends no attempt), `platform_refusals`
  incremented and `next_eligible_at = now + platform_refusal_backoff(n)`;
* the sixth consecutive refusal ends the agent FAILED with the precise code in
  `last_error` and `end_cause` `dispatch_failed`: 31 minutes of parks in all;
* a sweep beside `_promote_scheduled_retries` promotes a due park through
  `promote_to_ready`;
* each park counts in `swarm_scheduler_platform_refusals_total{code}` and logs
  at ERROR: that is the alert #1820 asks for.

### Why parking keeps invariant 1

A PARKED agent creates no infrastructure demand: it holds no lease, no pool
count and no pod, and admission does not consider it. The lease is released in
the transaction that parks, so the all-or-nothing reservation (invariant 2) is
untouched, and concurrency, counted from LEASED (invariant 3), drops at once.
Today's 30 s READY loop is the costlier one: every retry leases capacity for
the length of a create that will be refused.

### Who reads it

* **the scheduler**: writes the park (`loop.py`'s failed-dispatch branch,
  `store.py`) and promotes it (a new sweep);
* **the reconciler**: nothing to do, a PARKED agent has no lease;
* **swarm-api and the console**: `park_reason` and `next_eligible_at`, as for
  every park; `platform_refusals` on the agent's detail;
* **`scripts/lib/check-contract-parity.sh`**: any shell restatement of
  `ParkReason` gains the member.

### What it would break if accepted

Nothing that reads today's values: a new enum member and a new defaulted
field, so an old document decodes with `platform_refusals = 0`. A reader that
switches over every `ParkReason` (the console's park label, swarm-mcp's
compaction) needs a label for the new member; an unlabelled one shows the raw
value.

### If it is declined

The two codes stay what the agent and the log carry, and a refusal keeps
spending attempts on the fixed 30 s: a policy or quota fault fails every
affected agent in about `30 s × max_attempts`.

### Invariants

1, 2 and 3 as above. 5 is untouched: the park is guarded on the lease and
generation exactly as today's return is, and a later attempt carries a new
generation.

## 124. `profiles.py`: a `fix-loop` catalogue profile, `control_plane`, shipped unavailable (FL-CONTRACT, fix loop request A)

**Status:** ACCEPTED by the owner (BGD) 2026-10-27 (Q1, Q2, Q10 of
[docs/design/fix-loop.md](design/fix-loop.md) §9: the loop is its own
control-plane step, named `fix-loop`) and APPLIED by the pull request that
adds this line (lane FL-CONTRACT, part of #2509). "What is true today" below
is the state before it was applied.

### What is true today

A workflow's review-then-fix loop has no step of its own. A review that says
NOT_YET ends the workflow (`verdict_not_merge`), and every further fix round
is a new workflow (#2509). The catalogue's one `control_plane` profile is
`await-input` (contract requests 93 and 94).

### The change

```python
"fix-loop": RunnerProfile(
    name="fix-loop",
    image="agent-runtime-base",   # never pulled: control_plane
    resource_class="standard",    # never reserved: control_plane
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=(),
    control_plane=True,
    provider=None,
    secrets=(),
    timeout_seconds=300,
    inputs={},
    available=False,
    disabled_reason="fix-loop is not available yet: ...",
),
```

mirrored in `terraform/infra/locals.tf` `runner_profiles`, and listed in
`profiles_without_a_job` so no tenant gets a Job for it.
`tests/terraform/catalogue.tftest.hcl` counts thirteen profiles and asserts
no `-fix-loop` Job. It ships unavailable, as `await-input` did, until every
build lane of the design's §8 has merged and FL-ACCEPT is green on dev;
enabling it is then `available=True` and nothing else, a request of its own.

### What it would break if accepted

Nothing at once. swarm-api refuses an unavailable profile by name, so no
caller can submit one before it is enabled. A reader that lists every
profile shows one more, disabled, with its reason. A `runner:fix-loop` pool
document is materialised, as for every profile.

### If it is declined

The loop has no step to live in, and Q1's (b) or (c) would have to be
designed instead.

### Invariants

**10** holds: a caller names `fix-loop` by name and sends no image, command,
resource or backend. **1** holds: nothing runs it, so it creates no demand,
and its class is never reserved. **6** and **7** are untouched: no pod.
**9** is untouched: no Job, no account, no secret.

## 125. `states.py`: `ParkReason.FIX_ROUNDS_RUNNING` (FL-CONTRACT, fix loop request B)

**Status:** ACCEPTED by the owner (BGD) 2026-10-27 (Q1, Q2, Q10 of
[docs/design/fix-loop.md](design/fix-loop.md) §9; Q2 (a) is this request)
and APPLIED by the pull request that adds this line (lane FL-CONTRACT, part
of #2509). "What is true today" below is the state before it was applied.

### What is true today

A PARKED agent says why through `ParkReason`. None of its members says "this
control-plane step is waiting while steps it appended run". `HUMAN_REQUIRED`
or `AWAITING_INPUT` with a phase would tell every reader -- `/sc check`, the
console's "needs a person" group, the notification bell -- that a running
loop waits for a person.

### The change

```python
# states.py, class ParkReason
#: A workflow's `fix-loop` step while its review-fix rounds run. Never
#: promoted to READY by the scheduler; swarm-api ends it PARKED ->
#: SUCCEEDED / FAILED / CANCELLED, and moves it to HUMAN_REQUIRED with a
#: `park_phase` when a person is needed. Holds no capacity.
FIX_ROUNDS_RUNNING = "FIX_ROUNDS_RUNNING"
```

* **Never promoted by the scheduler.** No sweep reads it; the scheduler
  promotes by reason (`_parked_window`), so a reason no sweep names is never
  made READY. FL-SCHED parks a READY `fix-loop` agent and never leases it.
* **In no capacity set.** It rides on PARKED, which is in `PENDING_STATES`
  and not in `CONCURRENCY_STATES`, and it is in no `PREWARM_REASONS`.
* **Ended by swarm-api**: PARKED -> SUCCEEDED, FAILED or CANCELLED, every
  edge already in `_ALLOWED`.
* **Moved to `HUMAN_REQUIRED` with a `park_phase`** (`fix_loop_exhausted`,
  ...) when a person is needed. Changing a park reason is a field write on a
  PARKED agent, not a transition. By Q3 (c) that park expires after 7 days
  and ends the loop FAILED `fix_loop_exhausted` (see request 126 for the
  end cause).

The readers that list every `ParkReason` gain the member in the same change,
because `scripts/lib/check-contract-parity.sh` and the unit tests hold them to
it: the console's `ParkReason` union, `PARK_REASONS`, `PARK_CLEARS_ITSELF`
(the rounds end on their own) and `REASON_COPY` in
`apps/swarm-ui/src/types.ts`, `PARK_WORD` in
`apps/swarm-ui/src/components/StatePill.tsx`, and swarm-mcp's
`progress._PARKED_BECAUSE` and `compact._WAITS_FOR`.

### What it would break if accepted

Nothing that reads today's values: a new enum member. A reader that switches
over every `ParkReason` without the member would show the raw value; the
readers above are given a label in the same change.

### If it is declined

Q2's (b) or (c): a running loop reads as waiting for a person everywhere a
park is classified.

### Invariants

**1** is what it keeps: a `FIX_ROUNDS_RUNNING` park holds no lease, no pool
count and no execution, and admission never sees it. **2** and **3** are
untouched: nothing is reserved or counted. **4** holds: the rounds are steps
of their own, each with its own worker, and the loop's step waits parked.

## 126. `models.py`: `EndCause.FIX_LOOP_EXHAUSTED`, a fix loop that ended without a MERGE (FL-CONTRACT, fix loop request C)

**Status:** ACCEPTED by the owner (BGD) 2026-10-27 ('accept-126',
answering the question lane FL-CONTRACT put on PR 2570) and APPLIED
2026-10-28 by the pull request that adds this line (lane FL-CONTRACT-126,
part of #2509). Requested 2026-10-27 by lane FL-CONTRACT, because BGD's Q3
pick (c) did not decide whether the end is a new end cause or text under an
existing one. Applied as written below: the member is appended after
`BUDGET_EXCEEDED`, the outcome ledger classes it `fix_loop_exhausted`, its
own failure class after `budget_exceeded` (DERIVE_VERSION 12, so every
stored day is re-derived), and the console's `EndCause` union, `END_CAUSES`
and `END_CAUSE_SAY` ("its fix loop ended without a merge") carry it, held to
the enum by `scripts/lib/check-contract-parity.sh`. "What is true today"
below is the state before it was applied.

### What is true today

BGD's Q3 (c) and §4.5 of the design end a loop FAILED `fix_loop_exhausted`:
when a person answers "stop", and when the park for a person passes 7 days.
`EndCause` (`models.py`) has no such member. Its closest are
`runner_error` (a runner failed, which is not what happened: every round
ran) and `verdict_refused` (the review's post was refused on the forge,
which is not what happened either).

### The change

```python
# models.py, class EndCause
FIX_LOOP_EXHAUSTED = "fix_loop_exhausted"   # the fix loop's rounds ended without a MERGE
```

written only by swarm-api (FL-DRIVE's "stop" route) and the scheduler's
`_expire_human_waits` (FL-SCHED), and classed by the outcome ledger as a
failure of the work, not of the platform.

### What it would break if accepted

Nothing at once: a new member. The console's end-cause list and the outcome
ledger's classes gain it in the same change, as request 118's
`budget_exceeded` did.

### If it is declined

The loop ends FAILED with `last_error` beginning `fix_loop_exhausted:` under
an existing end cause BGD picks, and every reader that wants to tell an
exhausted loop from a crash reads text, which is the defect request 23 was
accepted to end.

### Invariants

None is touched: an end cause records why, it decides nothing.

## 127. `profiles.py`: a `claude-code-lightpanda` catalogue profile, DOM-only browser work on `standard` with no live browser (LIGHTPANDA-ANALYSIS)

**Status:** ACCEPTED by the owner 2026-10-31 and APPLIED by lane
LIGHTPANDA-CONTRACT (part of #2688), in the pull request that rewrote row 127
of the index above: one literal entry in `RUNNER_PROFILES`, right after
`claude-code-browser`, exactly as below, held equal to `claude-code` in every
field but its name and image by
`tests/unit/common/test_claude_code_lightpanda.py`. Its `terraform/infra/locals.tf`
mirror waits for the image: it lands in the same pull request once
`agent-runtime-lightpanda` is built and pinned, so no plan fails on a missing
image digest (CLD for BGD, 2026-10-31). Requested 2026-10-31 by
lane LIGHTPANDA-ANALYSIS (part of #2688), answering BGD's ask of the same day
to analyse Lightpanda as a runtime for browser agents and, if it adds value,
design and build it. BGD said to go with the recommended options. This is
decision B (2) of
[`docs/design/lightpanda-runtime.md`](design/lightpanda-runtime.md) §5.

### What is true today

`claude-code-browser` (`RUNNER_PROFILES` in `profiles.py`) is the only profile with a
browser. It is `live_browser=True` on the `browser` class, so every browser
agent is one pod with a worker and a `display` sidecar (Xvfb and a headed
Chromium, 2 CPU / 5 GiB of the class). Each agent leases a unit from
`resource:browser`, which is 20 in prod against 100 for `standard`
(`pool_limits.resource_classes` in the prod tfvars). That holds even when the
work needs no pixels: reading pages, scraping, link checks, form flows.

Measured on 2026-10-31 (§3 of the design;
`docs/design/lightpanda-runtime/results.json`), Lightpanda 1.0.2 on 17 real
pages returned the same links as headless Chromium on 16 of 17. Its medians
against Chromium's were:

* proportional memory: 48 against 454 MiB;
* CPU: 0.30 against 4.61 s;
* time to DOM ready: 253 against 470 ms.

Ten pages at once took 183 MiB and 3.0 CPU-s, against 1,831 MiB and
37.3 CPU-s. Lightpanda draws no pixels and loaded no iframe content, so it
cannot replace the live browser. It can carry DOM-only work outside the
`browser` pool.

### The change

```python
# profiles.py, RUNNER_PROFILES, immediately after "claude-code-browser"
    # CONTRACT REQUEST 127 (LIGHTPANDA-ANALYSIS, part of #2688;
    # docs/design/lightpanda-runtime.md): claude-code's runner on
    # agent-runtime-lightpanda, a headless browser that runs JavaScript and
    # draws no pixels, for DOM-only browser work: reading, scraping, link
    # checks, form flows. The `standard` class and one container: no display
    # sidecar, no live view, no take-control, so `live_browser` stays False
    # and the task leases `resource:standard`, not the scarce
    # `resource:browser`. A page it cannot read goes to a claude-code-browser
    # child the agent submits. A literal, not `replace()`, so
    # tests/terraform/catalogue_mirror sees it (request 113's reason).
    # GKE Autopilot, on-demand only (invariant 6).
    "claude-code-lightpanda": RunnerProfile(
        name="claude-code-lightpanda",
        image="agent-runtime-lightpanda",
        resource_class="standard",
        backend=Backend.GKE_AUTOPILOT,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs=_CLI_AGENT_MCP_INPUTS,   # claude-code's; no browser_tool
        secret_slots={"files": _FILES_SLOT},
        model_classes=_CLAUDE_CODE_MODEL_CLASSES,
        default_model_class="opus",
    ),
```

Nothing else in `apps/common/swarm_common/` changes:

* no new resource class (a smaller class would still be 1 unit, because of
  the `units_for` floor);
* no new `browser_tool` choice;
* no `RUNTIME_TOOLS` entry (that table is keyed by `browser_tool` choices,
  and this profile declares none);
* no state, end cause or model field.

### What it would break if accepted

Nothing at once. A new profile with no Job and no pool is never dispatched.
These mirrors must follow it, all listed with their lanes in §6 and §7 of the
design:

* `terraform/infra/locals.tf` `runner_profiles` (and so the
  `runner:claude-code-lightpanda` pool);
* the environments' `runner_profiles` pool limits;
* the stream-file tuples in `runners/streams.py` and `agent_streams.py`;
* the console's `Submit.tsx` and `api.ts` mirrors;
* the parity section in `scripts/lib/check-contract-parity.sh`.

`tests/terraform/catalogue.tftest.hcl` fails until `locals.tf` mirrors it,
and so does the parity script's catalogue comparison. They are to land in
the same wave.

### If it is declined

DOM-only browser work stays on `claude-code-browser`. It queues on the 20
`browser` units and pays for a display it never shows. An agent that needs
tens of pages at once pays Chromium's measured ~180 MiB and 3.7 CPU-s a page
inside the display's 3-CPU limit.

The alternatives are both worse:

* a fifth `browser_tool` would change the frozen `browser_tool` choices
  instead and save no capacity;
* a `claude-code` input would put an AGPL binary in every claude-code image.

### Invariants

* **2 and 3:** the lease takes `runner:claude-code-lightpanda` with the
  other pools, all or nothing, counted from LEASED.
* **6:** GKE on-demand, no Spot.
* **7:** the `standard` class already holds it (memory request = limit, CPU
  4 ≤ 8 ≤ 2 × 4). There is one container, so no split.
* **9:** the tenant's own GSA, secrets, prefix and namespace, as `claude-code`.
* **10:** a caller names the profile. The image, the command and Lightpanda's
  flags come from the catalogue and the runner.
* **1, 4, 5, 8:** unchanged; the claude-code runner's checkpointing and
  fencing are reused as they are.

## 128. `specsign.py`: a `"quality_gates"` block in `SETTINGS_MAC_FIELDS` (JEV-LANES, QUALITY-GATES-SETTINGS-MAC, part of #2688)

**Status:** ACCEPTED by the owner 2026-10-31 and APPLIED by lane
QUALITY-GATES-SETTINGS-MAC (part of #2688), in the pull request that adds
row 128 of the index above. Requested 2026-10-31 by lane JEV-LANES in the
body of PR 2905 (merged 2026-10-31T22:37Z), whose first question the owner
answered 'Add it (contract request)'. "What is true today" below is the
state before it was applied.

### What is true today

`workspace_settings/{tenant_id}.quality_gates: {jev, set_at, set_by}` is
written by `apps/swarm-api/swarm_api/qualitygates.py::QualityGatesStore`
(owner, workspace admin or platform admin only, audited). With `jev` true
the worker's `qualitygates.prepare` registers jev-belay and jev-enforce on
a claude-code lane, keyed with the workspace's own TypeSafe key. No block of
`SETTINGS_MAC_FIELDS` covers it, so it is not signed, and the worker acts on
a stored `jev: true` whoever wrote it.

### Why

[tenant-settings-integrity.md](design/tenant-settings-integrity.md) §1.3:
any tenant worker can write `workspace_settings/<B>`. A forged "on" sends
B's lane text (the final reply, the edits, the CLAUDE.md rule files) to
TypeSafe under B's own key, a key B's admin set for TypeSafe, without B's
admin having chosen it for the lanes. Bounded -- nothing goes anywhere B's
admin had not already pointed a key at -- but it is the same kind of write
`code_intelligence.jev` is signed against.

### The change

```python
SETTINGS_MAC_FIELDS = MappingProxyType({
    ...,
    "quality_gates": ("jev",),
})
```

Only `jev` is covered: `set_at` and `set_by` are a record of who changed it,
not something any reader acts on. Nothing else in `specsign.py` changes.
`tests/unit/common/test_settings_block.py` gains the entry.

### The follow-up once it is applied

In the same pull request: `QualityGatesStore.put` stores
`quality_gates.mac` (`judgment.block_mac`) whenever it writes the block, and
reads the stored block through `judgment.trusted_block`, as
`codeintel.CodeIntelligenceStore` does (PR 1330's `code_intelligence.jev`);
`judgment.WRITER_FIELDS` gains `"quality_gates": ("jev",)`, so
`scripts/resign-workspace-settings.sh` signs the blocks stored before it.
The worker cannot check an HMAC whose key would let it sign any tenant's
block, so the scheduler, which holds the key, vouches for it (the owner's
pick on PR 2955, "Scheduler hands a verdict"): at each claude-code dispatch
`scheduler.judgment.read_quality_gates_env` reads the attempt's own tenant's
block, and both dispatchers write `SWARM_QUALITY_GATES_JEV` ("true" only for
a block that says on and whose MAC verifies, "false" otherwise) and, for a
refused "on", `SWARM_QUALITY_GATES_REFUSED` into the job's environment. The
MAC is required whatever SETTINGS_MAC_TENANTS says, and the verdict is
given only while SETTINGS_MAC_REQUIRED is on. The worker's
`qualitygates.GatesVerdict` acts on "true" alone, and only while the stored
block still says on. Unsigned, mismatched, no key or the switch off is off,
and the attempt's summary says why.

### What it would break if accepted

Nothing at once: a new block changes no other block's bytes. A reader that
requires the MAC reads a stored `quality_gates` block written before the
re-sign pass as off until the pass signs it.

### If it is declined

The switch stays unsigned, and a forged "on" is acted on.

### Invariants

**9** is what this protects: the tenant id and the block name are inside
the signed bytes, so a block copied from another tenant's document does not
verify. **10** holds either way: one bool, never an image, a command or a
resource. **1-8** are untouched.
## 129. `admission.py` / `models.py`: a lease takes 1 unit in each provider pool, whatever its size (LARGE-ONE-PROVIDER-SLOT, part of #2950)

**Status:** ACCEPTED by the owner (BGD) 2026-11-01 (~03:55Z, choosing the
expected behaviour #2950 states) and APPLIED by lane LARGE-ONE-PROVIDER-SLOT
(part of #2950), in the pull request that adds row 129 of the index above.

### What is true today

`acquire_lease_in_transaction` reserves the same `units` -- the resource
class's: standard 1, browser 1, large 4 -- in every pool `pool_names_for`
lists, and `release_lease_in_transaction` subtracts the lease's `units` from
each. Two of those pools, `provider:<p>` and `provider:<p>:tenant:<t>`, stand
for concurrent LLM sessions against the provider's accounts, and a
claude-code-large agent runs one Claude session, like a standard one.

Measured 2026-11-01 03:46Z with `swarm capacity`: ten claude-code-large agents
in Engineering held 40 of `provider:anthropic:tenant:eng`'s 50 slots for ten
sessions; every other Engineering agent shared the last 3.

### The change

* `models.pool_units_for(pool, units)`: THE rule. A `provider:*` pool takes
  `PROVIDER_POOL_UNITS` (1); every other pool takes the resource units.
  `reserved_pool_units` applies it to a lease's pools.
* `Lease.pool_units: dict[str, int] | None`, written at reserve: what the
  lease took in each pool. `held_pool_units` (and `Lease.held_units()`) reads
  a stored lease: its `pool_units`, or -- for a lease reserved before this
  change, which has none -- `units` in every pool, the rule it was reserved
  under.
* `evaluate_capacity(pools, required, units, tasks=1)` checks each pool for
  `tasks * pool_units_for(name, units)`; `acquire_lease_in_transaction`
  increments each pool by its share and stores `pool_units` on the lease;
  `release_lease_in_transaction` subtracts `held_pool_units` from each pool.

Each pool, and what it bounds:

* `provider:<p>`, `provider:<p>:tenant:<t>`: LLM sessions against the
  provider (the subscription accounts' concurrency, a tenant's own key's).
  One agent is one session: **1**.
* `resource:<c>`: the class's compute. **Units**, unchanged.
* `global`: the platform's compute: every agent's CPU and memory land on the
  same backends. **Units**, unchanged.
* `tenant:<t>`: a tenant's share of compute (its namespace's quota, its
  Cloud Run instances). **Units**, unchanged.
* `backend:<b>`: a backend's compute capacity (Autopilot nodes, Cloud Run Job
  quota). **Units**, unchanged.
* `runner:<p>`: a profile's concurrency. Every agent of a profile has one
  class, so units and agents differ by a constant factor; the configured
  limits are written in units (`runner:claude-code-large` 100 is 25 agents),
  so changing it would silently quadruple them. **Units**, unchanged.

### Who else applies the rule

Through the frozen functions: the scheduler (`acquire_lease`), the worker,
the reconciler and swarm-api (every release goes through
`release_lease_in_transaction`; nothing else writes `active`). Restated and
changed in the same pull request: swarm-api's `headroom` (n agents need
`n * pool_units_for` per pool), `waiting` (a row's `units` and the
below-units verdict), the `/v1/capacity` profile's `pool_units`, the admin
leases listing's per-row `pool_units` and `units_held_by_pool`;
`scripts/lib/concurrency-judge.jq` (invariant 3's exact check) and
`scripts/lib/pool-units.jq` (read by make pool-check). The console and
swarm-mcp read the served per-pool figures in the same pull request (the
owner's pick, 2026-11-01): `types.ts` weighs an agent per pool (`unitsIn`,
`taskWeight`, `leaseHeld`) for `blockerCeiling`, `ProfileMatrix`'s `fitsIn`,
`AdminSettings`' ceiling arithmetic, `Submit`'s cost and verdicts and
`Holders.tsx`'s drift card; swarm-mcp's `binding_pool` divides each pool's
room by the profile's served share. None of them restates the rule: each
reads what the API serves, and falls back to `units` in every pool only for an
API older than the fields, whose admission charged exactly that.

### What it would break if accepted

Nothing at once: an old lease releases by the old rule, and the console and
swarm-mcp read each lease's and each profile's served share.

### If it is declined

Large work holds four provider slots per session and starves the workspace's
other agents; the only relief is raising the provider caps, which over-admits
standard work against the provider's real session limit.

### Invariants

* **2:** every pool's increment and the lease, with its `pool_units`, are
  written in the one admission transaction, all or nothing, as before; release
  is one transaction that reads every pool before any write.
* **3:** counted from LEASED, as before: the shares are taken at reserve.
  `concurrency-judge.jq` sums each lease's held share per pool, so its exact
  `active == held` check stays exact across old and new leases.
* **1, 4-10:** unchanged.

---

## 130. `profiles.py`: `codex` becomes available, on the tenant's key or a pool ChatGPT plan (CODEX-PLAN-CONTRACT, part of #2688)

**Status:** ACCEPTED IN PART by the owner (BGD) on 2026-11-01, with one
change: "Accept, but keep codex unavailable until the pool lanes land". The
secrets change below is applied (lane CODEX-CONTRACT-APPLY), with its
`secret_env` mirror; `available=False` stays, and `disabled_reason` now says
codex waits for the ChatGPT-plan pool (the quota broker leasing OpenAI plan
accounts and the codex runner using them) and is turned on by a follow-up once
both have merged. That follow-up applies the rest: `available`, the console's
fixture availability, the delegate skill's sentence and the three tests that
pin codex refused (item 5). Requested 2026-11-01 by lane CODEX-PLAN-CONTRACT. The owner (BGD) asked on 2026-11-01 for the
codex runner to run on ChatGPT plans from a shared pool, the way claude-code
runs on the pool of Claude subscriptions, and accepted that this needs a
contract request.

### What is true today

In `apps/common/swarm_common/profiles.py`, `RUNNER_PROFILES["codex"]`:

* `available=False`, with the `disabled_reason` "codex is disabled on this
  platform. The provider refused the registered credential and the platform
  is focused on Claude. Use claude-code." It was turned off by request 11
  (2026-09-23): four codex steps of a twenty-step load test failed with
  "openai refused the credential", because the tenant's
  `swarm-tenant-eng-openai` held one rejected version.
* `provider="openai"`, `secrets=("OPENAI_API_KEY",)`, and `secrets_any_of`
  left at its default, `False`.

So codex authenticates only with a metered OpenAI API key, and nothing in the
catalogue lets it name a plan's credential. claude-code already has the shape
this needs: `secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")`,
`secrets_any_of=True`, either one sufficing.

### The change

```diff
--- a/apps/common/swarm_common/profiles.py
+++ b/apps/common/swarm_common/profiles.py
@@ "codex": RunnerProfile(
         runner_argv=("python", "-m", "agent_worker.runners.codex"),
         provider="openai",
-        secrets=("OPENAI_API_KEY",),
+        # INTERCHANGEABLE, as claude-code's are (contract request 130, owner
+        # 2026-11-01): the tenant's metered key, or a ChatGPT plan leased from
+        # the pool. The plan arrives as an access token (about an hour) and the
+        # plan's account id; the quota broker alone holds and refreshes the
+        # refresh token, which never reaches a worker.
+        secrets=("OPENAI_API_KEY", "CODEX_PLAN_ACCESS_TOKEN", "CODEX_PLAN_ACCOUNT_ID"),
+        secrets_any_of=True,
         timeout_seconds=7200,
         inputs=_CLI_AGENT_MCP_INPUTS,   # contract request 88
-        # DISABLED 2026-09-23 by the owner's decision: ...
-        # ...
-        available=False,
-        disabled_reason=(
-            "codex is disabled on this platform. The provider refused the "
-            "registered credential and the platform is focused on Claude. Use "
-            "claude-code."
-        ),
+        # Re-enabled by contract request 130 (owner, 2026-11-01): disabled
+        # 2026-09-23 by request 11 when the one registered key was refused;
+        # it now runs on a pool ChatGPT plan as well as a tenant's key.
         secret_slots={"files": _FILES_SLOT},   # contract request 80
     ),
```

The exact patch against main is in the pull request's body. Nothing else in
the catalogue changes: same image, class, backend (GKE Autopilot, request 65),
provider, timeout, inputs and slots.

`secrets_any_of=True` is what makes the three names one credential among
alternatives: a step runs if it has `OPENAI_API_KEY`, or if it has the plan's
pair. The pair is not two independent alternatives -- an access token without
its account id does not reach the Codex backend -- and the flag cannot say so;
the codex runner is what refuses a plan token without its account id
(`agent_worker.runners.codex`, in the runner lane), exactly as claude-code's
runner picks one of its two names.

**The interface the three lanes share** (owner's brief, 2026-11-01; recorded
here because the names are the contract's): a pool account with
`provider: "openai"` holds a ChatGPT plan's sign-in (refresh token, account
id). The quota broker is its ONLY writer and refreshes it serialised per
account, because each refresh replaces the refresh token. `POST
/v1/accounts/assign {provider: "openai", ...}` returns as `secret` a current
access token plus the plan's `account_id`, never the refresh token, and the
worker hands them to codex as `CODEX_PLAN_ACCESS_TOKEN` and
`CODEX_PLAN_ACCOUNT_ID`. The sign-in is "Sign in with ChatGPT": OAuth with PKCE
at `https://auth.openai.com`, the Codex CLI's public client; the plan's traffic
goes to the Codex backend on the Responses wire and refuses some model names
(measured by sagaxyz/ai-studio on a live ChatGPT Plus login, 2026-08-10).
Measured here 2026-11-01: the codex CLI in the runtime image (`codex-cli
0.154.0`) offers `codex login --with-api-key` and `codex login
--with-access-token` (its help names `CODEX_ACCESS_TOKEN`), each reading stdin.
`CODEX_PLAN_*` are the platform's names, between the broker, the worker and
the runner; the runner turns them into whatever the CLI reads.

### Who else applies the rule (every mirror, read 2026-11-01 on `bf5ca60`)

Each must change with it, in the pull request that applies it unless noted:

1. **`terraform/infra/locals.tf` `runner_profiles["codex"]`**:
   `secret_env = { OPENAI_API_KEY = "openai" }` becomes the three names, each
   mapped to `"openai"` (`{ for name in [...] : name => "openai" }`, as
   claude-code-large writes its two). `tests/terraform/catalogue.tftest.hcl`,
   run `the_terraform_mirror_matches_the_python_catalogue`, compares
   `secret_env_names` to the catalogue (parsed by
   `tests/terraform/catalogue_mirror/main.tf`) and reddens until it does.
   Nothing in Terraform states availability. The `runner:codex` and
   `provider:openai` pool limits (dev 20 and 100, prod 40 and 40) already exist
   and need no change for the request; their sizing for a plan pool is the
   pool lane's to propose.
2. **`scripts/lib/check-contract-parity.sh`**: compares no profile's
   `secrets`, `secrets_any_of` or `available` (its catalogue checks cover
   inputs, model classes, image/class/backend and provider/class/backend for
   pool units). It needs no change; it is listed because the brief named it,
   and checked.
3. **The console's profile list** (`apps/swarm-ui`): the live console reads
   `available` and `disabled_reason` from `/v1/capacity` (`RunnerPicker`,
   `ProfileMatrix`, `Submit`), so it follows the catalogue. The DEV FIXTURE
   restates it: `src/api.ts`, `FIXTURE_AVAILABILITY`'s `"codex"` entry
   (`available: false` and the reason), which
   `tests/unit/control_plane/test_capacity_serves_availability.py::test_the_ui_fixture_table_is_the_catalogues_availability`
   holds to the catalogue. Vitest files that render that fixture and expect
   codex refused change with it: `__tests__/submit.picks.test.tsx` ("Not
   enabled yet. Use claude-code") and `__tests__/submit.picker.test.tsx`
   (`option('codex').disabled`).
4. **The MCP bridge's profile list** (`apps/swarm-mcp/swarm_mcp/profiles.py`):
   reads the BUNDLED `swarm_common.RUNNER_PROFILES`, not the API, and refuses
   an unavailable profile in `check()`, so an installed plugin keeps refusing
   codex until it is upgraded to a release carrying this change. No list of its
   own to edit; comments at `profiles.py`, `workflows.py` and `cli.py` that
   recount the 2026-09-23 disabling stay true as history.
   `plugin/skills/delegate/SKILL.md` says "**`codex` is DISABLED.**" and
   `tests/unit/mcp/test_profiles.py::test_the_delegation_skill_never_recommends_a_profile_the_platform_refuses`
   holds it to `available`: the sentence changes with it.
5. **Tests that pin codex refused**, which redden when it is applied (request
   11's guards, doing their job):
   `tests/unit/control_plane/test_guards.py::test_unknown_runner_profile_is_rejected`
   (`"codex" not in known_runner_profiles`) and
   `::test_a_disabled_profile_is_refused_as_disabled_not_as_unknown` move to a
   profile that stays disabled (`post-verdict`, `claude-code-review`,
   `fix-loop`); `tests/unit/docs/test_counts_mockups.py::test_the_fixture_names_a_profile_the_catalogue_marks_unavailable`
   (`_CODEX in UNAVAILABLE`) is re-pointed the same way.
6. **The pool's eligibility, stated in three places, all keyed to
   `CLAUDE_CODE_OAUTH_TOKEN`** (the duplication request 22 names): the
   scheduler's `credentials.SUBSCRIPTION_TOKEN_ENV`
   (`PROFILE_TAKES_NO_SUBSCRIPTION`), the worker's
   `accountlease.ACCOUNT_TOKEN_ENV` (and `lifecycle`'s "does this profile take
   a pool account"), and swarm-api's `task_accounts.ACCOUNT_TOKEN_ENV` and
   `service`'s pooled-profile listing. swarm-api's `SUBSCRIPTION_PROVIDER =
   "anthropic"` refuses an openai pool account at `POST /v1/accounts`. These
   are the broker and worker lanes' changes (a profile is pool-eligible when it
   declares its provider's plan name: `CLAUDE_CODE_OAUTH_TOKEN` for
   anthropic, `CODEX_PLAN_ACCESS_TOKEN` for openai); until they land, codex
   accepted by this request runs on a tenant key only, and
   `tests/unit/worker/test_pool_credential_parity.py` keeps the scheduler and
   the worker agreeing either way.
7. **The worker's credential shaping** (`agent_worker/secrets.py`,
   `resolve_credentials`): a BARE-string tenant payload is copied into every
   declared name. For claude-code that is harmless (the runner tells a key from
   a token by prefix); for codex it would put an API key into
   `CODEX_PLAN_ACCESS_TOKEN` and `CODEX_PLAN_ACCOUNT_ID`. The runner lane must
   treat a bare payload as `OPENAI_API_KEY` only, and take the plan names from
   a pool lease (or a JSON payload naming them), never from a copied string.
   Likewise the scheduler's GKE/Cloud Run env (`dispatch.py`) wires every
   declared name to `swarm-tenant-<t>-openai`, so the same rule applies to
   what arrives through it. `agent_worker/cliagent.py` reads only `key_env`
   and `alt_key_envs` (and registers only those for redaction), and refuses a
   codex step that has no `OPENAI_API_KEY`: `runners/codex.py` must declare the plan
   names so they are both handed to the child and redacted.
8. **`/v1/runtimes` and `/v1/capacity`** (`routes/platform.py`) serve
   `secrets`, `secrets_any_of`, `available` and `disabled_reason` straight from
   the catalogue: no change, and `types.ts` already carries every field.
9. **Docs** that say codex is disabled (`docs/provider-keys.md`,
   `execution-backends.md`, `architecture.md`, `workflows.md`,
   `worker-images.md`, `plugin/README.md`, the web-ui mockups) are corrected in
   the applying pull request; request 11 above stays as history.

### What it would break if accepted

* **A tenant holding a dead key is dispatched again.** `swarm-tenant-eng-openai`
  was the rejected key of 2026-09-23; a codex step for that tenant, with no
  plan pool leased, fails at the provider as it did then, now as a refusal
  against a profile that admits it. The applying lane checks each tenant's
  registered `openai` secret (that it exists and is current, never its value)
  or the owner rotates it first.
* **Old plugins refuse, new API admits**, until the bridge is upgraded (item 4).
* **Tests listed in item 5 redden** until re-pointed; that is the intended
  signal.
* Nothing in Firestore changes shape: the four task documents that name codex
  still read; no lease, pool or attempt field is added.

### If it is declined

codex stays refused, and the owner's ChatGPT-plan pool has no runner to serve:
the broker and worker lanes can hold and lease plan credentials, but no
profile may name them, and the only way to run an OpenAI agent stays a metered
key the platform cannot dispatch either.

### Invariants

* **3 (concurrency counts from LEASED):** unchanged. A codex step reserves
  `provider:openai`, `provider:openai:tenant:<t>`, `runner:codex` and the
  compute pools at LEASED in the one admission transaction (**2**), whichever
  credential it ends up using; the pool-account lease the worker takes from
  the broker is a separate, per-account claim that bounds a plan's own
  concurrency and is released with the attempt, as claude-code's is.
* **9 (per-tenant isolation):** a tenant key stays the tenant's own
  `swarm-tenant-<t>-openai`, read by the tenant's own GSA. A pool plan is
  served to a tenant only through the broker's assign, under the pool's
  existing rule (`quota_broker/accounts.py`: an account belongs to exactly one
  tenant and serves another only where its owner named it, per account), now
  applied to `provider: "openai"` accounts too; the refresh token never leaves the
  broker, and the access token handed to a worker lives about an hour and is
  registered for redaction.
* **5 (fencing):** unchanged; the worker's fencing check is its step 1, before it
  asks the broker for an account.
* **10:** the caller still names `codex` by name and supplies no credential,
  image or command; which credential runs is the platform's choice.
* **6, 7, 8:** unchanged (on-demand GKE Autopilot, the standard class,
  periodic checkpoints). **1, 4:** unchanged.

---

## 131. `models.py`: `Task.timeout_seconds_asked`, the timeout a spec asked for when swarm-api stored a lower one (TIMEOUT-ASKED-CONTRACT, part of #2688)

**Status:** ACCEPTED by the owner (BGD) 2026-11-01 -- "API records the ask"
-- and APPLIED by the pull request that adds this line (lane
TIMEOUT-ASKED-APPLY, part of #2688), exactly as below: the field in
`models.py`, `validation.timeout_asked` and the refused metadata key,
`service._build_task`, the child's `replace` in `children.py`, the codec's
`task_from_dict` and `task_to_api`, the console's `Task` type
(`apps/swarm-ui/src/types.ts`), docs/workflows.md's step table and the three
tests of item 9; pinned by `tests/unit/control_plane/test_timeout_seconds_asked.py`.
Requested 2026-11-01 by lane TIMEOUT-ASKED-CONTRACT; that request's pull
request edited nothing under `apps/common/swarm_common/`. The pre-publish gate lane (#2981, PR 2986, merged
2026-11-01) asked who records a lowered timeout; the owner (BGD) answered "API
records the ask". This entry is that answer as a contract change.

### What is true today

Read 2026-11-01 on `9cc20f0`.

* `apps/swarm-api/swarm_api/validation.py`, `validate_timeout(profile,
  requested)`, returns `min(requested, profile.timeout_seconds)`, and
  `service.py` `_build_task` stores only that as `Task.timeout_seconds`. Every
  submission goes through `_build_task`: an agent, a batch entry
  (`submit_tasks`), a workflow step (`submit_workflow`, which passes the
  step's own `timeout_seconds`) and a child (`children.py`). A spec asking
  10,800 s of `claude-code` (cap 7,200 s) runs under 7,200 s, and the task
  document says nothing of the 10,800.
* A child is lowered twice more, in `children.py`: to its parent's
  `timeout_seconds` before `_build_task`, and again by the `replace` after it.
  What the child's spec asked is not kept either.
* A `browser_if_ui` step (#2322) stores its `code` alternate's timeout in
  `metadata.runtime.alternate.timeout_seconds` (`_runtime_alternate`), also
  through `validate_timeout`, and the scheduler swaps it onto the task document at
  dispatch (`scheduler/runtime.py`, `RUNTIME_SWAP_FIELDS`).
* The workflow document's step (`WorkflowStep.timeout_seconds`) keeps what
  the caller wrote, unlowered. The worker never reads the workflow document
  for its limit, and a lone agent, a batch entry and a child have no such
  record anywhere.
* The worker already reads the record this entry asks for:
  `apps/agent-worker/agent_worker/lifecycle.py` `TIMEOUT_ASKED_FIELD =
  "timeout_seconds_asked"`. `Worker._timeout_asked_env` reads it from the task
  document and, only when it is an int above the attempt's
  `cfg.timeout_seconds`, hands it to the runner as
  `SWARM_TIMEOUT_ASKED_SECONDS`; `runners/cliagent.py` records it in the
  runner's `limits.clamped` and `limits.timeout_seconds_asked`
  (`tests/unit/worker/test_prepublish_deadline.py`). No writer sets the field,
  so today that path never fires.

### The change

```diff
--- a/apps/common/swarm_common/models.py
+++ b/apps/common/swarm_common/models.py
@@ class Task:
     #: construction of a task changes meaning; absent on every older
     #: document, which decodes as 0.
     platform_refusals: int = 0
+    #: Contract request 131 (TIMEOUT-ASKED-CONTRACT, part of #2688), the
+    #: owner's "API records the ask" (2026-11-01): the `timeout_seconds` the
+    #: spec asked for, written by swarm-api at submission ONLY when it stored
+    #: a lower `timeout_seconds` (the profile's cap, or a child's parent's).
+    #: A RECORD ONLY: the limit is `timeout_seconds` alone, and nothing reads
+    #: this as one (invariant 10); the worker reports it in the runner's
+    #: `limits.clamped`. Unsigned: written at submission, it chooses nothing.
+    #: None on a task that was not lowered and on every older document. Last
+    #: in the class, after `platform_refusals`, so no positional construction
+    #: of a task changes meaning.
+    timeout_seconds_asked: int | None = None
```

That is the whole frozen change: one field, appended. No new enum member, no
state edge, no spec format. The exact patches, against `9cc20f0`, are in the
pull request's body: the one above, and swarm-api's.

The rule swarm-api applies (its patch, outside the frozen package):

* `validation.timeout_asked(requested, *granted)`: the asked value when it is
  above ANY timeout this service stores for the step, else None.
  `validate_timeout` is unchanged and stays the limit.
* `service._build_task` collects every timeout it grants the step -- its own,
  and its `code` alternate's on a `browser_if_ui` step -- and writes
  `timeout_seconds_asked=timeout_asked(spec.timeout_seconds, *granted)`. One
  place, so an agent, a batch entry and a workflow step are covered alike. The
  worker compares the record with the timeout it actually runs under, so a
  step whose primary profile was lowered and whose alternate was not reports
  nothing when it runs as the alternate.
* `children.py` sets it again in its `replace`, from the CHILD's spec, against
  the final `min(task.timeout_seconds, parent.timeout_seconds)`: the
  `TaskCreate` it hands `_build_task` is already lowered to the parent's.
* The name is refused in a caller's metadata like every platform-written key:
  `TIMEOUT_ASKED_FIELD` joins `REFUSED_UNWRITTEN_METADATA_KEYS` with its
  sentence in `_RESERVED_BECAUSE` (it is a top-level field, so no one writes
  `metadata.timeout_seconds_asked`, and it is not added to
  `RESERVED_METADATA_KEYS`, whose members are signed or served as platform
  metadata). As a top-level key it is refused already: `TaskCreate`, the
  batch entry, the workflow step and the child spec are `extra="forbid"`
  (`schemas.py`, `children.py`). In `input` it is refused already: an input
  key the profile does not declare is refused (`validate_runner_input`,
  contract request 25).

### Who else applies the rule (every mirror, read 2026-11-01 on `9cc20f0`)

1. **The store's task document codec, swarm-api** (`apps/swarm-api/swarm_api/codec.py`
   `task_from_dict`): MUST change. `task_to_api` serves a DECODED `Task`, so
   without the read the API can never serve the record; and swarm-api writes
   whole task documents (`store.create_tasks`, the child's `txn.set`, both
   through `task_to_firestore`), so any task document it builds from a decoded one
   would drop it. It reads an int above 0, anything else as None.
2. **The API's response codec** (`codec.task_to_api`): MUST change, to serve
   `timeout_seconds_asked` beside `timeout_seconds`, so a caller can tell a
   lowered step from one that asked for nothing more. **The API's schemas**
   (`schemas.py`): no change. Responses are the codec's dicts, not a response
   model; every request model is already `extra="forbid"`.
3. **The scheduler's task document codec** (`apps/scheduler/scheduler/codec.py`): no
   change. It decodes for admission and dispatch and patches a task document with
   `txn.update` (`store.py`), never writing a whole decoded task document, so a field
   it does not read survives. The runtime swap does not touch the field.
4. **The worker** (`lifecycle.TIMEOUT_ASKED_FIELD`, `_timeout_asked_env`;
   `cliagent.TIMEOUT_ASKED_ENV`): no change. It already reads the raw task
   document and treats the value as a record. The comment above
   `TIMEOUT_ASKED_FIELD` ("requested of swarm-api in #2981") may name this
   request when it is applied.
5. **The console's `Task` type** (`apps/swarm-ui/src/types.ts`, `export
   interface Task`): SHOULD change, `timeout_seconds_asked?: number | null`,
   optional because an older API does not serve it. Showing it (a "lowered
   from 10,800 s" beside the timeout) is a console change of its own, not
   required by this request. `scripts/lib/check-contract-parity.sh` section 9
   compares only `parent_task_id` and `parent_attempt_id` of `Task`: no change.
6. **The MCP bridge's task record rendering** (`apps/swarm-mcp/swarm_mcp/render.py`
   `render_task`): no change required. It renders no timeout today; it passes
   the served task record through, so the field is there for a later line. Its
   workflow builder (`workflows.py`) forwards a step's `timeout_seconds` as
   written and must not forward `timeout_seconds_asked` (it does not).
7. **The Firestore-shape notes:** `docs/architecture.md` §4 lists collections,
   not task fields: no change. `docs/workflows.md`'s step table
   ("`timeout_seconds`: optional; may only shorten the profile's own
   default") SHOULD add that a longer ask is lowered and recorded as
   `timeout_seconds_asked`. `docs/web-ui/03-agents-and-workflows.md` quotes
   an older `task_to_api` key list as a dated snapshot: no change.
8. **The spec signature** (`specsign.canonical_step_spec`): no change, and no
   format bump. The field is UNSIGNED: it limits nothing and selects nothing,
   so a forged value can only add a line to the runner's report.
9. **Tests that pin the class's tail**, which redden when it is applied and
   change with it (each measured red with both patches applied, 2026-11-01):
   `tests/unit/common/test_budget_contract.py`
   (`test_120_the_fields_default_to_none_and_come_last`: the last two fields
   are `budget` and `platform_refusals`),
   `tests/unit/common/test_model_classes.py`
   (`test_the_new_fields_come_last_so_no_positional_construction_moves`: the
   last three end at `platform_refusals`), and
   `tests/unit/common/test_specsign_covers.py`
   (`test_every_task_field_is_signed_or_named_unsigned`: the field joins
   `UNSIGNED_TASK_FIELDS` with its reason).

### What it would break if accepted

* **Nothing that reads a limit.** No reader takes the field as a timeout; the
  worker already compares it with the timeout it runs under.
* **The three tail and signature tests in item 9 redden** until updated in
  the applying pull request; that is their job.
* **Firestore shape:** one optional field on `tasks/{id}`, absent on every
  older document and on every agent not lowered. No index, no rule, no
  migration.
* **An old API with a new worker, or the reverse:** no record, no report;
  the same as today.

### If it is declined

A lowered timeout stays silent: a spec asking 10,800 s runs under 7,200 s,
and neither the task document, the API nor the runner's `limits.clamped` says so. The
worker's reader stays dead code, and an agent killed at 7,200 s looks like one
that ran past a limit it chose. The only record left is the workflow
document's unlowered step, and only for workflow steps.

### Invariants

* **10 (callers pick a profile by name; never a resource spec):** held. The
  caller may still only SHORTEN a timeout; the ask is recorded, never
  honoured. The field is written by swarm-api alone and refused from a caller
  as a top-level key, a metadata key and an input key.
* **5 (fencing):** unchanged; the worker's fencing check runs before it reads
  the task document's timeout record.
* **8 (periodic checkpoints):** unchanged; the attempt still ends at
  `timeout_seconds`, checkpointed as before.
* **1, 2, 3 (capacity):** unchanged. The field adds no state and no demand,
  and admission never reads it.
* **4, 6, 7, 9:** unchanged; no backend, class, resource or tenant boundary is
  touched.
## 133. `profiles.py` / `models.py` / `specsign.py`: a turn cap, `RunnerProfile.max_turns` and `WorkflowStep.max_turns`, that the worker passes to the agent CLI (CONTRACT-MAXTURNS, part of #2524)

**Status:** open, REQUESTED 2026-10-28 by lane CONTRACT-MAXTURNS (PR 2682,
filed there as 127). On 2026-11-02 the owner chose to finish that pull
request, and lane CARRY-TURN-CAP carried it onto main and renumbered it:
127 to 131 had been taken on main, and the open `Task.attempt_totals`
request (stored agent totals) takes 132. BGD decided on 2026-10-28 (~22:20Z,
answering question 2 of lane REVIEW-GATE-DEFAULT on PR 2678) that the cheap
requirements check's turn cap is a field in the frozen runner-profile
contract, not only a timeout and a prompt instruction. Not applied:
`apps/common/swarm_common/` is frozen, and the shape below is BGD's to
accept. PR 2678 has merged, so main runs the interim: the issue run's
`requirements-check` step (`swarm_api.issueruns._requirements_check`) runs
on `claude-code` with `REQUIREMENTS_CHECK_TIMEOUT_SECONDS = 900` and a "use
at most 30 turns" instruction in its prompt, which the agent may ignore.

### What is true today

Read on main on 2026-11-02. Nothing in the contract caps an agent's turns. A
step's spend is limited by `timeout_seconds` (the profile's, lowered by the
step, `swarm_api.validation.validate_timeout`, with the ask recorded by
request 131's `Task.timeout_seconds_asked` when it was lowered) and by its
budget (requests 118-121), and both stop an agent only after it has spent
the time or the money. The claude-code runner can already pass a cap:
`agent_worker.runners.claude_code.body` adds `--max-turns N` when the
agent's `input` carries `max_turns` (`ctx.payload.get("max_turns")`). That
path is unreachable: every claude-code profile declares its inputs
(request 25), `max_turns` is not one of them, and swarm-api refuses the key
at every door (`tests/unit/control_plane/test_runner_inputs_by_declaration.py`,
`test_claude_code_refuses_every_key_but_its_declared_issue`). So a step that
should end in about 30 turns -- the requirements check, which reads an issue
and a diff and writes one `requirements.json` -- can only be asked to, and
pays for the turns until its timeout if it does not listen.

### The change

Two fields on the profile and one on the step and its agent (`Task`), the
same shape as `timeout_seconds`: the profile sets the ceiling, the step only
lowers it.

```python
# profiles.py, class RunnerProfile
#: Whether this profile's runner passes a turn cap to its agent CLI
#: (claude_code: `--max-turns`). False: swarm-api refuses a step's
#: `max_turns` for it, because a cap the runner ignores is a promise
#: nothing keeps.
supports_max_turns: bool = False
#: The most turns an attempt on this profile may run, and the cap for a
#: step that names none. None: no cap (today's behaviour). Only read when
#: `supports_max_turns`.
max_turns: int | None = None

# models.py, class WorkflowStep -- the cap the step ASKED for, as submitted
max_turns: int | None = None
# models.py, class Task -- the RESOLVED cap: min(step, profile), or the
# profile's when the step names none; None: no cap
max_turns: int | None = None
```

* **Profile and step, and why both.** The requirements check runs on the
  shared `claude-code` profile, whose implement steps legitimately run
  hundreds of turns, so a cap on the profile alone would cap every
  implementer or need a new catalogue profile (and a Job per tenant) for one
  step. The cap belongs to the STEP, as `timeout_seconds` and `budget` do.
  The profile carries what only it can say: whether its runner honours a
  cap (`supports_max_turns`) and, optionally, a ceiling no step may raise.
  `supports_max_turns=True` on the six profiles whose `runner_argv` is
  `agent_worker.runners.claude_code` on main today (`claude-code`,
  `claude-code-large`, `claude-code-review`, `indexer`,
  `claude-code-browser` and request 127's `claude-code-lightpanda`);
  `max_turns` stays None on every profile in this request, so no running
  agent changes.
* **swarm-api** resolves the cap at submission, beside `validate_timeout`:
  `max_turns` joins `timeout_seconds` on the request schemas
  (`swarm_api.schemas`: `TaskCreate`, the workflow step and the child spec,
  `Field(default=None, ge=1)`); it is refused for a profile with
  `supports_max_turns=False`; it is lowered to the profile's `max_turns`
  when that is set. A `browser_if_ui` step resolves it against both its
  browser profile and its `code` alternate, as its timeout is (both run
  the claude-code runner). A child's cap is never above its parent's
  resolved cap, as `children.py` already keeps a child's timeout at or
  below its parent's. `WorkflowStep.max_turns` records what was asked and
  `Task.max_turns` what was resolved. `input.max_turns` stays refused: the
  cap is a field, never a caller input to the runner.
* **Signed.** Spec format 7 (main's `SPEC_FORMATS` is still 1 to 6) is
  format 6's fields plus `max_turns`, and a `Task` is signed at it only when
  the field is set, as request 120 did for `budget`: an agent whose cap was
  raised or removed after submission fails its worker's check.
* **The worker** reads `Task.max_turns` only from the verified document and
  passes it to the agent CLI: the claude-code runner's `--max-turns N`,
  taken from the verified `Task` instead of `input`. None passes no flag,
  which is today's behaviour exactly. An attempt that ends on the cap ends
  as the CLI's `error_max_turns` result already ends one.

### What reads it

swarm-api's submission (`validation`, beside `validate_timeout`, and
`children.py` for a child) and the issue-run compiler that writes the
`requirements-check` step (`issueruns._requirements_check`, which sets
`max_turns=30` and drops the prompt's "use at most 30 turns"); `specsign`
(format 7); the worker's claude-code runner; and, read-only, the console's
step detail and `/sc check`, which can show the cap beside the timeout.
`scripts/lib/check-contract-parity.sh` gains nothing: no enum moves.

### What it would break if accepted

Nothing at once. Every new field defaults to None or False, so every
existing profile, step and stored agent means what it means today, and an
agent without `max_turns` is still signed at format 6. A reader that lists a
step's fields shows one more. Rollout order is the one formats 2 to 6 used:
workers first, then swarm-api.

### If it is declined

The interim on main stays: a 900 s timeout and a 30-turn instruction the
agent may ignore. The smaller alternative is to declare `max_turns` as a
`RunnerInput` (integer, 1 to a ceiling) on the claude-code profiles' inputs,
which the runner already reads; it is signed with `input` (format 3) but
lives in the caller's input rather than a typed field, and no profile can
carry a ceiling or say that its runner ignores a cap.

### Invariants

**10** holds: a step asks for a cap by number, and a cap only lowers a
limit, as `timeout_seconds` does; it chooses no image, command, resource
spec or backend, and `input.max_turns` stays refused. **5** holds: the
field is in the signed spec from format 7, so a tampered cap fails the
worker's check before the agent runs. **8** holds: an attempt that ends on
its cap ends like any other, after its checkpoints. **1-4**, **6**, **7**
and **9** are untouched: no capacity, lease, pod shape or tenant boundary
moves.

### How it is tested

When it is applied, by the applying lane, red first:
`tests/unit/control_plane/test_runner_inputs_by_declaration.py` keeps
refusing `input.max_turns`; new swarm-api tests that a step's `max_turns` is
stored on the step and resolved on the agent's `Task`, lowered to a profile
ceiling and, for a child, to its parent's cap, and refused for a profile
with `supports_max_turns=False` or a value below 1; a `specsign` test that
an agent with `max_turns` is signed at format 7 and fails verification when
the cap is raised or removed, and that an agent without it still signs at
format 6; and a worker test in `tests/unit/worker/test_runners.py` that the
claude-code runner passes `--max-turns` from the verified `Task.max_turns`
and passes none for None.

### Who decided

BGD, 2026-10-28 (~22:20Z): the turn cap is a contract field (question 2 of
PR 2678, REVIEW-GATE-DEFAULT). The shape -- step field resolved against the
profile, rather than a profile field alone -- is this request's proposal
and BGD's to accept. The owner chose on 2026-11-02 to carry PR 2682 onto
main rather than drop it.
