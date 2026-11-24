# Brief template: a build lane

Opt-in. Copy the block below into the step's `prompt`, replace every
`{{...}}` with this lane's own facts, and delete a line that does not apply.
A brief that uses no template is sent exactly as written.

Left out on purpose, because the worker adds it to every coding agent's
prompt: which tests to run, scratch space, saved outputs, sub-agents, the code
graph, the time limit, where the pull request files and questions.json are
written, and the branch rule.

```text
Goal: {{GOAL: the change, stated as a fact once it is done}}

Read first: {{READ_FIRST: the files, docs and issues to read before editing}}

Done when: {{DONE_WHEN: each requirement as a statement someone can check}}

Territory: edit only {{TERRITORY: the paths this lane may change}}. Ask before
editing any other file; one you must cross into is named in the commit with
the reason.

Red first: commit the test that shows the defect or the missing behaviour
before the change that makes it pass, so the history shows it failing.

The merge train ejects a pull request whose checks go red beside the others in
its train, that conflicts with the base, or whose checks run out of time. So
add no test that sleeps, races the clock or depends on run order, keep away
from files outside the territory, and leave nothing red that you saw fail.

Pull request: the title states the work as a fact, never a task id. The body
says what changed, what was verified and how, and what was not verified.

Owner decisions: a choice that is the owner's is asked in questions.json with
its options and your recommendation; build the recommended option, mark it as
the one built, and carry on.

Lane facts: {{LANE_FACTS: anything only this lane has, or "none"}}
```
