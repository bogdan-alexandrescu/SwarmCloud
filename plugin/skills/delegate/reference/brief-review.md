# Brief template: a read-only review

Opt-in. Copy the block below into the step's `prompt`, replace every
`{{...}}` with this lane's own facts, and delete a line that does not apply.
Give the step an id that ends in `review`, so the worker holds it to its
verdict file. A brief that uses no template is sent exactly as written.

Left out on purpose, because the worker adds it to every coding agent's
prompt: which tests to run, scratch space, saved outputs, sub-agents, the code
graph, the time limit, where the artifacts folder is, and how to launch the
image's own browser.

```text
Goal: review {{SUBJECT: the pull request, branch or staged patch}} against
{{AGAINST: the plan, issue or requirements it must meet}}.

Edit nothing. Make no commit and change no file in the repository. The output
is the verdict file{{EXTRA_OUTPUTS: ", and review.md", or nothing}}.

Check: {{CHECKS: this lane's own list, most important first}}; then that each
requirement is met, that a test would fail without the change, and anything
touching credentials, tenant isolation, redaction or IAM.

Verdict file, verdict.json, exactly this shape:
{"verdict": "MERGE" | "NOT_YET",
 "findings": [{"severity": "blocker" | "major" | "minor",
               "file": "<repository path>", "line": <number>,
               "problem": "<what is wrong>", "fix": "<what would fix it>"}]}

Blockers and majors decide: one of either makes it NOT_YET. Minors alone leave
it MERGE; they are filed on the wave epic, not fixed in this lane.

A console change is reviewed rendered: when the change touches
{{CONSOLE_PATHS: the console's paths, or "nothing"}}, open each changed
screen in a local headless browser and leave one screenshot per screen in the
artifacts. A console review without its screenshots is not finished.

Lane facts: {{LANE_FACTS: anything only this review has, or "none"}}
```
