# Agent quality experiment protocol

How the agent quality program measures a change: the rules every experiment
follows, the conditions experiment batch three set (adopted here as the owner's
session wrote them), how arms are assigned, what each metric means, how a
historical item is kept from seeing its own future, and the corpus to build
after batch three, and the protocol of the Jev work-contract experiment
(section 8). The baseline every experiment states its change against is
[baseline.md](baseline.md); what was already failing is
[known-failures.md](known-failures.md).

## 1. The program's rules

These hold for every package and every experiment (the owner, 3 November 2026):

1. A model judgment is not an authorization decision. A reviewer's verdict, a
   Jev score or a graph answer never grants a merge, a secret or a repository.
2. Missing verification is not a pass. A check that did not run is recorded as
   not run, and the item is not accepted on it.
3. Evidence names the exact source revision it verifies: the full commit, the
   applied `image_refs` and the `runner_models` in force.
4. A strict contract never silently degrades to advisory. A gate that cannot
   run fails the item or says so; it never turns into a warning.
5. Optional Jev help has a bounded, deterministic fallback: a Jev failure
   leaves the output byte-identical to the local-only one
   ([matrix](capability-matrix.md#2-jev-accounting-and-adapter)).
6. A stale execution generation never publishes or merges (invariant 5).
7. Cost accounting covers failed, cancelled, parked and retried attempts.
8. Never report a test as passing that did not run. A skipped test is not a
   pass ([known-failures.md](known-failures.md#1-skipped-expected-to-fail-and-quarantined-tests)).
9. Keep proposed, implemented, behaviourally tested, enabled in configuration
   and live-verified apart, and record an unknown as unknown, never as zero.
10. Benchmark runs never merge (section 6).

## 2. Batch three conditions, adopted

Batch three's conditions, as written by the owner's session, are the
conditions of every batch until the owner changes them. Each condition has the
record that proves it held; a batch whose record is missing for a condition
did not meet it, and its results are reported with that condition named as
unmet, never pooled with batches that met it.

| Condition (as written) | How the batch proves it | On the baseline commit |
|---|---|---|
| tools loaded up front | every session's init lists `swarm-graph` (and `jev`, `swarm-jev` where on) as `connected`, with `alwaysLoad` in the attempt's `--mcp-config` | built (`claude_code.py` `ALWAYS_LOAD`) |
| the graph index ready for the exact pinned commit (the pin is the promoted index commit; indexing is paused for the batch window) | the repository's promoted index commit equals the batch's pinned commit at start and end; `index.paused` is true for the window; every graph-on attempt's `stage.json` names that commit | built (`IndexPaused`, `promotion_decision`) |
| agents with code intelligence call the graph | `result_summary.runner.metrics.tool_calls.families.code_graph` above zero on every graph-on build step | not met in batches two and three; the system-prompt fix is not yet live-verified |
| Jev's checks fire and its decision tool is called where there is a decision | the attempt's `quality_gates` summary shows the hooks ran; `families.jev_decision` above zero where the brief has an open choice | not met in batches two and three |
| the Jev browser group uses the Jev browser | `families.jev_browser` above zero and `families.playwright` zero on that group's browser steps | not read from here |
| Jev's triage reads the review verdicts | each review step's verdict carries J3's `p_real` annotations | built |
| the Lightpanda builder reaches its own server | the `claude-code-lightpanda` step's supervisor log shows `serve` up and the agent's Lightpanda calls answered | not read from here |
| no step cut by a time limit it did not ask for | no attempt ends on a profile timeout, a context ceiling or a gate budget unless the step set it; the end cause is in each attempt's terminal summary | not read from here |
| no hidden time in the publish check or pod shutdown | the gate's per-check times and the pod's end-to-exit time are recorded and summed into latency | the guard run is time-boxed (`timeboxed_guards`) |
| every group runs the same prompts, commit and model | one brief per item, byte-identical across arms; one pinned commit; `model_class` set explicitly on every step; `runner_models` recorded at start and end | `runner_models` in [baseline.md](baseline.md#3-models-per-class) |
| no release lands during the batch | `image_refs` applied at start equals the one at end ([baseline.md](baseline.md#2-agent-images)) | digests not read from here |
| the pool can carry the batch | no attempt waited in `QUEUED` on a pool ceiling (`pool_limits`) or parked on quota; the batch's peak `LEASED` count is below the pool's ceiling | dev and prod ceilings in [baseline.md](baseline.md#42-terraform-variables) |
| every measurement recorded | every row of section 5 is present for every attempt, or recorded as unknown with why | n/a |
| the prompt is not bloated or repeated | each brief's token count is recorded; no instruction appears twice in what the agent receives | n/a |
| reviews start their agent promptly | a review's first agent output within a minute of its pod; the pack within `PACK_DEADLINE_SECONDS` (30 s) | built (REVIEW-STARTUP-WAIT) |
| cost recorded across every session | the attempt's cost sums every fresh session after a context ceiling; `cost_incomplete` is false or the cost is reported as a floor | built (`ceiling.py`) |
| each instruction once and the guards run once (briefs: related tests only, the gate runs the guards, the work is never published) | the brief says each once; the gate's record shows the guard run skipped when the agent already passed it on the same tree; no item opens a PR | built (`guards_pass_on`, the repeat hook) |
| subagents hear about the graph | a subagent's or child agent's first prompt carries the graph note | **not implemented** ([matrix](capability-matrix.md#4-context-packs-and-graph-tools)) |
| related tests are related | the tests an agent runs are the ones `scripts/related-tests.sh` names for its diff | built (`related_tests.py`) |

## 3. Arms are assigned per whole workflow

An arm is a property of the workflow, never of a step: an item's implement,
review, fix and score steps all run under one arm, so a result is never a
mixture of arms.

* **Assignment.** Before submission, each item gets its arm from
  `sha256(batch id, item id) mod (number of arms)`, in balanced blocks of one
  item per arm, so the split is reproducible and no person picks it.
* **Realisation.** The arm is the workflow's signed `dispatch` switches
  (`code_intelligence`, `quality_gates`, `jev_judgments`, and `contract_jev`
  for the experiment of section 8;
  [workflows.md](../../workflows.md#workflow-switches-dispatch)). A switch set
  `on` or `off` takes the workflow out of the workspace's `context_ab` split,
  whose `sha256(task_id)` assignment is per step and is therefore never what
  decides an experiment arm. The switches only narrow, so the experiment
  workspace's stored settings allow every arm (`context` on,
  `code_intelligence.jev` on, `quality_gates.jev` `"on_request"`).
* **Read back.** Every attempt's `.swarm-context/stage.json` and
  `result_summary.switches` say which switch decided. An attempt whose record
  disagrees with its workflow's arm is an `arm_mismatch`: the item is excluded
  from both arms and listed, never relabelled.
* **Concurrency.** Arms run interleaved in the same window, not one after the
  other, so a queue or a provider slowdown falls on every arm alike.

## 4. Metric semantics

| Metric | Definition | Never |
|---|---|---|
| Quality: "accepted-task success" | An item is accepted when its review verdict is MERGE, its CI-equivalent commands all ran and passed, and its hidden tests (the tests the reference change added or changed, held out of the brief and the checkout) all ran and passed. Reported as accepted over submitted, with a 95% Wilson interval (`swarm_api/outcomes.py::wilson`) | A hidden test that did not run counts as not passed. An operator's cancel is a failure unless it was recorded as an infrastructure fault before the result was read, and then the item is re-run once in its arm |
| Hidden tests | Run by the score step after the agent finishes, at the agent's head, each on its own; recorded per test as passed, failed or not run | The agent never sees them: they are not in the brief, the checkout or the graph index it reads |
| Cost per accepted item | The sum, over every attempt of every step of every item in the arm (failed, cancelled, parked, retried, repaired, review and fix rounds, every session after a context ceiling) of model cost plus Jev usage priced at `jev_token_rates`, divided by the arm's accepted items | An unpriced model or a missing rate makes the total a floor, reported as "at least" with its coverage, never as zero; an arm with no accepted item has no cost per accepted item (unknown), not infinity or zero |
| End-to-end latency p50 and p95 | From the workflow's submission to the moment the item is accepted, including queueing, pod start-up, clone and staging, the agent, the pre-publish gate, review and fix rounds, and the score step; nearest-rank percentiles over accepted items, as `scripts/benchstat.py` computes them, with the count of items that had no end | A mean in place of a percentile. Merge is not reached in a benchmark (section 6): the merge step's own time is measured on live lanes in the same window and reported beside it as a separate, labelled component |
| Tool use | `result_summary.runner.metrics.tool_calls` by family, per step | A family absent from the record is unknown, not zero |

## 5. Keeping a historical item from seeing its future

A historical item is a change that already merged. Everything the agent can
read must stop at the item's base commit:

* **The checkout** is the item's base commit (the PR body's 40-character
  `base commit:` line; an abbreviated one is not replayable).
* **The brief** is the agent's input as recorded on the lane's `swarm/task_*`
  head. The issue is dropped: fetched now, it is closed with the merged PR in
  its comments (`scripts/lib/ctxq_replay.py`, SPEC).
* **The graph** must be an index of the base commit or an ancestor of it. The
  worker already refuses to stage one that is not an ancestor of its checkout
  (`graphstage.prove_ancestry`), so a later index is never read; the history
  rows of the context pack and `similar_changes` come from that index and stop
  where it stops.
* **The forge.** An agent with git tools could fetch the live repository's
  main or read the issue. Historical items run against a mirror repository
  holding only refs at or before the item's base, with no issues and no pull
  requests. Until that mirror exists, an attempt whose tool calls read the live
  repository past its base (`git fetch` of another ref, `issue_view`,
  `pr_view`, `gh`) is excluded and listed.
* **Jev.** Whether a Jev backend keeps anything from earlier calls about a
  repository is not read from here (the backends are outside this tree).
  Until it is, Jev-on historical items are reported apart from the others.

## 6. Benchmark runs never merge

* Every experiment workflow runs with strategy `collect` and
  `carrier: branches`: each step pushes its own `swarm/` branch and nothing
  opens a pull request ([dispatch-and-integration.md](../dispatch-and-integration.md)),
  so auto-merge never sees one.
* Results are mirrored only to `replay/<lane>/<arm>` branches, never forced and
  never deleted (`ctxq_replay.check_refspec`).
* No brief asks for a PR, a `ready` label, a merge or an issue comment. An item
  that opened a PR anyway is invalid: the PR is closed unmerged, and the item
  is listed with the step that opened it.

## 7. The corpus to build after batch three

Batch three measured one feature built several ways. The program's next corpus
covers the kinds of work below, each sourced so the reference answer and its
hidden tests exist before any arm runs. Every item records its source, its base
commit, its brief, its hidden tests and its reference change.

| Kind | Sourced from | Reference and hidden tests |
|---|---|---|
| Small bug fixes | Merged PRs that close a `bug`-labelled issue and change one module, with the issue's text as it stood when filed | The fix's diff; the tests the fix added or changed |
| Cross-module changes | Merged PRs whose diff spans two or more `apps/` packages (for example swarm-api and the worker), from the issue or lane brief | The merged diff; every test it changed, across the packages |
| API and schema changes | Merged PRs that change `swarm_api/schemas.py`, a route, a Firestore document's shape, or apply an entry of [contract-change-requests.md](../../contract-change-requests.md) | The merged diff; the API's own tests and the console tests that read the changed shape |
| Concurrency and authorization changes | Merged PRs that touch admission, leases, fencing, tenant isolation or IAM (the S0 areas of the issue forms) | The merged diff; the race and isolation unit tests it added; never run against a shared project |
| Reviews of defective patches | The first head of a lane that later needed a `## Continuation` fix round, before that round | The review must find what the fix round fixed; graded against the fix commit's files and the tests that went red |
| Test-only changes | The test commit of this repository's red-first pairs ("Tests red first": the test is pushed alone, the fix after) | The test must fail at the base and pass at the fix commit; both are run |
| Functional and visual browser work | The browser bench prompts (`scripts/browser_bench.py` `PROMPTS`), the console QA workflow ([docs/qa/](../../qa/)) and the acceptance fixture page | The bench's own success criteria and the QA steps' recorded verdicts; screenshots compared by the visual QA rules |
| Multi-repository changes | Built for the purpose: a pair of repositories registered in the experiment workspace where the change in one needs reading the other, until a historical source exists (none does; [known-failures.md](known-failures.md#2-documented-open-defects-in-the-programs-areas)) | A reference change and its tests written before the arms run |
| Concurrent overlapping assignments | Two workflows submitted together from one historical item, with overlapping territory | Exactly one lands its change; the other waits on the plan lock or is held as a duplicate; no two PRs claim one issue |

Items enter the corpus by a deterministic order (`sha256` of the corpus version
and the source PR number, as `ctxq_replay.order_key` does), and a short kind is
reported short, never padded.

## 8. The Jev work-contract experiment (`contract_jev`)

The owner chose on 3 November 2026 (the owner) to use Jev to make the work contract
better and more enforceable: a review at submission (JC1) and a judgment of
the acceptance criteria at the end of a step (JC2)
([jev-contract.md](jev-contract.md)). This section is how the experiment
measures whether that choice pays, under sections 1 to 6; it is
[jev-contract.md section 11](jev-contract.md#11-how-the-experiment-measures-it)
made into a procedure.

### 8.1 The arm

* **The switch.** `contract_jev`, `on` or `off`, in the workflow's signed
  `dispatch` block (`swarm_api.validation.DISPATCH_SWITCHES`). Any other word
  is refused with `invalid_dispatch` and the accepted values named.
* **Assignment.** Per whole workflow, by `sha256(batch id, item id)`, as
  section 3 says. Every step of an item runs under its arm.
* **What each arm is.** Both arms submit the same contracts. Arm `on` lets
  JC1 and JC2 run where the workspace's settings already allow them: the
  signed `contract_review` setting and `SWARM_CONTRACT_REVIEW` for JC1,
  `code_intelligence.jev` and the worker's post-step for JC2. Arm `off`
  compiles the contracts mechanically with JC1 and JC2 skipped and recorded as
  `skipped:arm`, so arm off is today's behaviour. The switch only narrows: it
  never turns a review or a judgment on that the stored settings leave off, so
  the experiment workspace stores `contract_review` on, `code_intelligence.jev`
  on and runs swarm-api with `SWARM_CONTRACT_REVIEW=enforce` for the batch.
* **Read back.** `scripts/benchstat.py contract-jev` reads the arm from every
  attempt's `result_summary.switches.contract_jev` (the worker's
  `{value, ...}` record, or the bare word). An item is counted in its arm only
  when every attempt records that arm. It is excluded from both arms and
  listed, never relabelled, as:
  * `arm_mismatch`: an attempt recorded the other arm, or Jev reviewed or
    judged an item of arm off, or an item of arm on was skipped as
    `skipped:arm`;
  * `arm_unrecorded`: the item has no attempt, or an attempt records no
    `contract_jev` (a missing record is not a pass, rule 2);
  * `arm_unassigned`: the item's arm is neither `on` nor `off`.

**Status, 5 November 2026** (the arm's row built the same day). Kept apart, as rule 9 requires:

| | State |
|---|---|
| The switch is accepted, stored and signed | implemented and behaviourally tested (`tests/unit/control_plane/test_dispatch_switches.py`) |
| The measures below | implemented and behaviourally tested (`tests/unit/scripts/test_benchstat_contract_jev.py`, and `benchstat.py self-test`) |
| JC1 and JC2 obey the switch; the attempt records it in `result_summary.switches` | implemented and behaviourally tested. Under `off`, `swarm_api.contractreview` reads no setting, sends nothing and records the review `skipped:arm` (`tests/unit/control_plane/test_contract_review_submission.py`); the worker hands the signed arm to the runner as `SWARM_CONTRACT_JEV` and the post-step asks Jev nothing and posts `not_run` with reason `skipped:arm` (`tests/unit/worker/test_contract_post_step.py`, `test_contract_verdict_hooks.py`), and the control plane's re-ask of that verdict asks Jev nothing either (`swarm_api.contractreask`, `tests/unit/control_plane/test_contract_reask.py`), so the step reaches the owner as a workspace that sends no code does; `agent_worker.graphstage.switches_summary` records `contract_jev` for `on` and `off` (`tests/unit/worker/test_dispatch_switches_worker.py`), which `benchstat.py contract-jev` counts in its arm (`tests/unit/scripts/test_benchstat_contract_jev.py`). `on`, or no switch, runs both exactly as before |
| Enabled in configuration; live-verified | **no**; not read from here |

### 8.2 The record per item

The collector writes one JSON line per item; `benchstat.py contract-jev
--input ITEMS.jsonl` reads them. A field the collector could not read is
`null` or absent, and is counted as unknown, never as zero.

| Field | From |
|---|---|
| `item_id`, `arm`, `historical` | the batch's assignment (section 3); `historical` for an item replayed from a merged change (section 5) |
| `attempts[].result_summary` | `GET /v1/tasks/{id}/attempts`, every attempt of every step |
| `review` | the step's signed dispatch block `contract_review` (`jev_review`): `outcome` (`ok`, `refused`, `amended`, `not_run` with `reason`, or `skipped:arm`); and from the person's next submission, `resubmitted` (a narrower contract), `overridden` (the same requirement resubmitted as enforceable) and `override_judged_cleanly` (JC2 then decided that requirement as `met` or `unmet`, not `cannot_judge`) |
| `judgments[]` | the `judgment` evidence of each judged requirement: `verdict` (`met`, `unmet`, `cannot_judge`) and `p`, the probability Jev gave its verdict |
| `hidden_tests`, `review_verdict`, `accepted` | the score step and the review (section 4) |
| `model_cost_usd`, `jev_tokens` `{input, output}` | every attempt's cost, and the item's `jev_usage` rows of kinds `contract_review` and `contract_judgment` with its other Jev calls |
| `submitted_at`, `accepted_at`, `send_backs` | the workflow's submission, its acceptance and the count of JC2 send-backs |
| `approvals`, `owner_overrides` | the owner's approvals of the item's steps, and those that accepted a step Jev judged `unmet` or that passed the send-back cap |

### 8.3 The measures

Per arm, with Jev-on historical items as their own group, `on:historical`
(section 5):

| Measure | Definition | Never |
|---|---|---|
| JC1 refusal and amendment rates | refused, and amended, over the reviews that ran (`ok`, `refused`, `amended`), each with its Wilson interval; `not_run` by reason and unrecorded reviews counted apart | A review that did not run counted as one that passed (answer 2 accepts the contract open; it is not a review) |
| JC1 after a refusal | resubmitted, overridden, and **false refusals**: refusals overridden by the person whose requirement JC2 then decided cleanly | |
| JC2 false send-back | `unmet` judgments on items whose hidden tests passed and whose review said MERGE, over `unmet` judgments with a hidden-test result and a review verdict | |
| JC2 missed defect | `met` judgments on items whose hidden tests failed, over `met` judgments with a hidden-test result | A hidden test that did not run scored as passed or failed: it is counted as `no hidden result` |
| JC2 `cannot_judge` rate | `cannot_judge` over every judgment | |
| JC2 calibration | the Brier score of Jev's probability that the requirement is met (`p` for `met`, `1 - p` for `unmet`) against the item's hidden-test outcome, over `met` and `unmet` judgments with a result; it is what moves the 0.6 and 0.7 thresholds (`contractjudge`) | |
| Accepted-item success | section 4, accepted over the arm's counted items, with its Wilson interval | Anything but `accepted: true` counted as accepted |
| Cost per accepted item | section 4: every item's model cost, failed ones included, plus its Jev tokens at `jev_token_rates` (`--jev-token-rates`, the platform setting's JSON), over the accepted items | **An unpriced arm prints no cost, never $0**: an arm that spent Jev tokens while `jev_token_rates` has no rate for them is `unpriced`, with no figure, because Jev's tokens are what arm on adds and a total without them would make it look free. A missing model cost or unknown Jev usage makes the total a floor, printed "at least" with its coverage |
| End-to-end latency | section 4, p50 and p95 nearest-rank from submission to acceptance over accepted items, send-back rounds included; items with no end counted | A mean |
| Owner time | the owner's approvals per accepted item, and the owner's overrides of Jev | An unknown approval count read as zero: it is counted as `unknown` |

`benchstat.py contract-jev` prints these per arm and lists every excluded item
with its reason; `--json` writes the same table for the batch's record. It
reports and never gates: its exit code says only whether the input was read.
