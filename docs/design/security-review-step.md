# The optional security review step

Owner's decisions, 4 November 2026 (part of #2688). This page says what the
step does, why each choice was made, and how a workflow adds it.

## What it is

The step is an optional extra review. Any workflow that implements, reviews
or merges code can add it. It runs the
[security-audit skill](https://github.com/cloudflare/security-audit-skill)
(MIT licence, pinned to commit `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8`,
14 September 2026) on the pull request's diff. The worker then checks the
skill's output and turns it into a verdict the merge reads:

```
implement ──> review ──> fix ──┬──> re-review ─────┐
                               └──> security-review ┴──> merge
```

| Piece | Where |
|---|---|
| Recognising the step, the agent's instructions, the validators, the verdict, the owner questions, the comment text | `apps/agent-worker/agent_worker/security_review.py` |
| The hooks: instructions when the plugin starts, `finish` after the agent, the failure cause, the pull request comment | `apps/agent-worker/agent_worker/lifecycle.py` (`_finish_security_review`, `_post_security_report`) |
| The merge requires both verdicts | `apps/agent-worker/agent_worker/merge.py` (`_security_hold`) |
| The issue-run option | `apps/swarm-api/swarm_api/issueruns.py` (`IssueRun.security_review`, `_with_security_review`) |

## Each choice and why

**Optional, and off by default.** A security audit costs more than a code
review: one quick pass can start as many as sixteen sub-agents. Most changes
do not need one, so the workflow or the run asks for it.

**After the fix, beside the re-review.** The audit has to judge the head
that will merge. Before the fix, that head does not exist yet. The step does
not wait for the re-review because neither needs the other's result.

**The step is recognised by its signed work contract.** The step runs on a
claude-code profile. Its work contract's `capabilities.plugins` names
`security-audit` in unattended mode, with the one skill `security-audit`. The
worker loads a plugin only from a contract whose digest matches the signed
dispatch block (`plugins.selected`). The same check tells the worker that the
step is a security review (`security_review.applies`). A tenant worker can
write the task document but cannot forge the signature, so it cannot turn a
step into a security review or turn one into an ordinary step. The plugin's
catalogue entry and image install come from a sibling lane. This code uses
only the name, and its tests give the catalogue the entry themselves.

**Full audit mode at the `quick` profile, scoped to the diff.** The owner
chose a bounded pass over one change, not a whole-repository audit. The
agent is told to audit the diff from `git merge-base HEAD origin/HEAD` to
HEAD. A quick, scoped run records everything outside the diff as
`out_of_scope`, never as `covered`, so the report never claims that the rest
of the repository is clean.

**The sub-agent budget is 16** (`SUBAGENT_BUDGET`). That covers:

- the four reconnaissance agents and the one coverage critic that the skill
  reserves for `quick`;
- room for six hunters and five verifiers.

One pull request's diff seldom produces more work than that. The skill's own
budget gate marks what does not fit as `deferred`, says so in the report,
and never goes over the budget.

**The output directory is `$SWARM_ARTIFACTS_DIR/security-audit/`.** The
skill requires a directory outside the target checkout. The artifacts folder
is outside it and is uploaded, so `findings.json`, `coverage-ledger.json`,
`run-metadata.json`, `FINDINGS-DETAIL.md` and `NEEDS-VALIDATION.md` become
the step's artifacts with no extra copy.

**No sandbox, so the target's code never runs.** The skill runs target code
only inside an OS-enforced sandbox: no network, an allowlisted environment,
a read-only target and resource limits. A SwarmCloud step does not provide
all of those to the agent, so the prompt says that no sandbox is available.
The skill then stays source-only and keeps every lead it cannot prove from
source as `needs_validation`.

**The worker derives the verdict, not the agent.** After the agent ends and
before the upload, `security_review.finish` does the following:

1. It deletes any `verdict.json` the agent wrote.
2. It runs the skill's own `validate-findings.cjs` and
   `validate-coverage-ledger.cjs` with node. Each validator gets only `PATH`
   in its environment and has a time limit.
3. It reads `findings.json`.

If a validator fails, a file is missing, or node is missing, the step fails
with the validator's message (`result_summary.security_review.error`). The
cause is `security_review_invalid`, which is not retryable, and no verdict
exists for anything downstream to read. The step never passes silently.

**The merge rule (owner decision 2).**

| What the audit found | Verdict | Effect on the merge |
|---|---|---|
| A confirmed `critical` or `high` finding (`severity.overall_severity`) | `NOT_YET`, naming each one | Held for the owner in Needs you |
| Confirmed `medium`, `low` or `informational` findings only | `MERGE`, with the counts | Reported; the merge goes on |
| `needs_validation` only, whatever severity it claims | `MERGE` | Never blocks |

`needs_validation` never blocks because the skill uses it for a lead it
could not prove without running code. Blocking on it would let one unproven
guess stop every merge.

**The owner questions use the existing owner-question park.** Each blocking
finding gets one question in `questions.json`. It is `blocking: true` and
offers three options:

- **Fix it now** (the recommended option);
- **Accept the risk** (say why in the answer);
- **Mark it a false positive** (say why in the answer).

The verdict's `needs_owner` names each question, so the merge parks on
`owner_wait` exactly as it does for a review's questions (#2070). The
existing answer route wakes the merge.

`built` is `null` because the branch built none of the three options. The
answer decides the merge; it is not a pick for a pick-fix round to build.

Once every question is answered, the merge goes on only if each answer
accepts the risk or marks a false positive. Any other answer, including free
text that names no option, makes the merge refuse with
`security_fix_requested`. The finding is then fixed and reviewed before
anything merges. If there are more than twenty blocking findings, which is
the `questions.json` limit, the twentieth question covers the rest together.

**The verdict is written under two names, with the same bytes.** Every step
whose id ends in `review` must produce `verdict.json`
(`expected_outputs.is_review_step`), and the console and `swarm_result` read
that file. The merge stages the security verdict as `security-verdict.json`
so that it does not collide with the re-review's `verdict.json`: staged
inputs are keyed by file name.

**The merge requires both verdicts (`merge._security_hold`).** A merge step
whose signed `input_from` stages `security-verdict.json` reads that file
after the review's verdict. A merge that stages none behaves exactly as it
did before. The security verdict is held to the same rules as the review's:

- a file that cannot be read refuses `security_verdict_unreadable`;
- `NOT_YET` with no question refuses `security_verdict_not_merge`;
- an answer on a task of another workflow does not count.

**The full report goes on the pull request (owner decision 3).** The step
posts `REPORT.md` as a pull request comment with the tenant's own `-git`
token, read after the agent ended, exactly as the minor findings are filed.
Before posting, the worker:

- runs the report through the worker's redaction filter;
- breaks every `@` mention;
- cuts the report to GitHub's 65,536-character comment limit and ends it
  with a pointer to the artifact when it is longer.

All other files stay as artifacts.

**A public repository shows unfixed findings.** On a public repository, the
comment publishes the audit, including confirmed critical or high findings,
before they are fixed. The owner chose the full report anyway: the people
who decide on the merge need the whole picture where they work. A team that
cannot accept this should not add the step to a public repository's
workflow.

## How a workflow adds the step

Add a step with these fields:

- `runner_profile`: `claude-code`;
- `depends_on` and `builds_on`: the step that opened the pull request (the
  integrator, the fix in implement → review → fix);
- a step id ending in `review`, conventionally `security-review`;
- `metadata.work_contract` loading the plugin:

```json
{
  "step_id": "security-review",
  "runner_profile": "claude-code",
  "depends_on": ["fix"],
  "builds_on": "fix",
  "allow_empty_diff": true,
  "input": {"prompt": "Security review of this pull request. Do not edit files."},
  "metadata": {"work_contract": {
    "...": "the contract's other sections",
    "capabilities": {"plugins": {
      "security-audit": {"mode": "unattended", "skills": ["security-audit"]}}},
    "limits": {"runner_profile": "claude-code"}
  }}
}
```

The prompt does not need to state the profile, the budget, the output
directory or the sandbox rule: the worker appends all of them
(`security_review.guidance`). The workspace must allow the plugin in its
signed plugin settings, and SWARM_WORK_CONTRACTS must be `on`.

For an issue run, set `security_review: true` on the run.
`issueruns._with_security_review` then compiles the step after the fix in
both the chain and the staged shapes. The option is refused when work
contracts are off, rather than compiled into a step that would load no
plugin.

## What still needs a change outside this lane

These are listed as questions in the pull request that added the step:

- `swarm_api.schemas.RunCreate` and `routes.runs.start_run` must accept and
  pass `security_review` (one field and one argument).
- `validation.resolve_integrator_step` and `merge_sources` must treat the
  security review as they treat the re-review: a step after the integrator
  that is not the integrator. Until then, `integrate` refuses a second final
  step.
- The merge step that swarm-api appends must stage the security review's
  `security-verdict.json` in its `input_from`. For an issue run, the merge
  that `issueci` submits must also read that verdict as it reads the
  re-review's.

## The invariants

The step is an ordinary claude-code agent step. Admission, leases,
concurrency, fencing, checkpointing and Spot are unchanged (invariants 1–8).

The pull request comment uses the tenant's own token on the tenant's own
repository (invariant 9).

A caller still picks a profile and a catalogue plugin by name only. The
image and the skill's install come from the catalogue (invariant 10).
