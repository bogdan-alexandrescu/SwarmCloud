# Brief template: a one-step fixer onto an existing pull request

Opt-in. Copy the block below into the step's `prompt`, replace every
`{{...}}` with this lane's own facts, and delete a line that does not apply.
A brief that uses no template is sent exactly as written.

Left out on purpose, because the worker adds it to every coding agent's
prompt: which tests to run, scratch space, saved outputs, sub-agents, the code
graph, the time limit, where the pull request files and questions.json are
written, and the branch rule.

```text
Goal: fix exactly the findings below on pull request {{PULL_REQUEST: number
and branch}}, in this one step.

Findings: {{FINDINGS: each one as file, line and what is wrong, copied from
the verdict}}

Fix those and nothing else: no refactor, no clean-up nearby, no new feature.
Build on the branch as it stands.

Never weaken an assertion to make it pass: no deleted test, no loosened
expectation, no skip or xfail, no wider tolerance. A test you believe is wrong
changes only with the reason written in the pull request body.

A finding that cannot be fixed in this step stays as it is: leave its code
alone and say in the pull request body which finding, why, and what fixing it
would need. One that needs the owner's call is asked in questions.json with
your recommendation.

The body gives each finding one line: fixed, with the commit, or not fixed,
with the reason.

Lane facts: {{LANE_FACTS: anything only this fix has, or "none"}}
```
