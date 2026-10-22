# Visual QA of any web application: the rendered workflow

What a QA run of a registered target is, step by step, and why each step has
the shape it has. The design is
[visual-qa-any-app.md](../design/visual-qa-any-app.md) §4; this page is the
built renderer, `apps/swarm-api/swarm_api/qaspec.py` (lane VQ-1 SPEC of
[agent-autonomy.md](../design/agent-autonomy.md) §6, part of #1933), held by
`tests/unit/control_plane/test_qaspec.py`.

There is no template file for it, unlike the console's
[`console-qa.workflow.json`](console-qa.workflow.json): the spec is rendered
from the target every time, because the target is the only thing a caller
names (invariant 10). A run is started by `POST /v1/qa-targets/{id}:run`
(lane VQ-2), never by submitting this spec by hand.

---

## 1. What the renderer refuses, before anything spends

| refusal | code | why |
|---|---|---|
| a flow with `mutating: true` on a target whose `environment` is `production` (or unset) | `qa_mutating_flow_on_production` | §3: only a non-production target may be written to. Absent is production, the safe reading of someone else's application |
| a screen or a flow start on an excluded path | `qa_screen_excluded` | the exclusions are never visited; `/logout`, `/logout/*`, `/signout` and `/signout/*` are always among them |
| more than 60 screens in one run | `qa_target_too_many_screens` | the discovery cap; at most 8 groups of at most 8 |
| no `screens` and nothing `discovered` | `qa_target_no_screens` | run the target's discovery first |
| a `base_url` that is not `https://host[:port]/[path]`; userinfo, a query, a fragment or an IP literal | `qa_target_invalid` | the base is a command-line argument in the capture prompt. Whether the host is public is the registration's check (`swarm_egress.policy`, lane VQ-2), not restated here |
| a screen path that is not origin-relative (`https://…`, `//host`), or carries whitespace or a backtick | `qa_target_invalid` | a path is joined to the approved origin and nothing else |
| a width that is not one of the live gate's presets | `qa_target_invalid` | the gate refuses a viewport; a preset is chosen by name. `PRESETS` is held equal to `agent_worker.livegate.presets` by the test |
| `file.max_issues` above 30, `file.evidence` other than `console_link` or `branch` | `qa_target_invalid` | 30 is the broker's per-agent bound (agent-autonomy.md §3.2) |

No message quotes a value from the target.

## 2. The steps

| step | profile, model | what it does |
|---|---|---|
| `capture-g<n>` | `claude-code-browser`, haiku, Playwright | writes its group's route list from the prompt, then `python -m swarm_qa capture --routes … --group g<n> --base <base_url> --presets … --themes … --settle generic --axe [--keep-storage] [--clock]` |
| `review-g<n>` | `claude-code`, opus | stages `capture-g<n>.tar`; reads the app's own design docs first (`design_docs`, default `DESIGN.md`, `docs/design*/**`, `**/style-guide*`); the §4.3 checklist; writes `findings-g<n>.json` and a crop per finding |
| `flow-<id>` | `claude-code-browser`, sonnet, jev | one per declared flow, recorded once per width (`flow-<id>-<preset>.gif`) |
| `report` | `claude-code`, opus | stages every capture and review tar; checks each review's `reviewed` against its manifest; writes `findings.json` and `report.md` |
| `flow-report` | `claude-code`, opus | only when the target declares flows; writes `flows.md` |
| `file` | `claude-code`, sonnet | stages the report's `findings.json`; files through the brokered `issue_create` / `issue_comment` (agent-autonomy.md §3.2); absent when the run asked for `file: "none"` |

Workflow-level: `repository_url` is the target's registered repository,
`strategy: collect`, `merge: off`, `on_step_failure: continue`, and every step
`allow_empty_diff: true`. A QA run changes no code: nothing is pushed (the
`branch` evidence opt-in pushes images to a `swarm/<task>` branch through
`git_push_branch`, never to the default branch), and there is nothing to merge.

**The report waits on no flow.** A flow that fails never costs the screen
findings, the lesson of the console template's split into `report` and
`flow-report`. Flow findings are in `flows.md`; they are not filed in version 1.

**Groups are even.** 17 screens are three groups of 6, 6 and 5, not 8, 8 and 1:
the slowest capture sets the run's length.

## 3. What is gone from the console template, and why

| console template | here | why |
|---|---|---|
| `npm ci`, `npm run dev`, `http://localhost:5173` | `--base <base_url>` | the deployed app; a build command from a caller is what invariant 10 forbids (§8 of the design names what a fixture build would need) |
| `--routes apps/swarm-ui/qa/routes.json` | the target's `screens` (or `discovered`), written by the step from its prompt | the checkout is the target's repository, which has no route list |
| the console's pending-read wait (`PENDING_JS`) | `--settle generic` | another app has no console reads to wait on |
| every load empties storage (`RESET_STATE_JS`) | `--keep-storage` on every capture of an `own_login` target | a single-page app often keeps its session in `localStorage` |
| the clock frozen at the route list's instant | off unless the target sets `clock` | freezing `Date` can expire an app's own session token |

`--settle generic`, `--keep-storage`, `--clock` as a flag, a route list with
no `clock`, and `swarm_qa discover` are lane VQ-5 CAPTURE-GENERIC's
(`images/agent-runtime-browser/swarm_qa.py`). Until VQ-5 merges, a rendered
spec reads correctly but its capture command is refused by the tool; a QA
run cannot start before VQ-2 merges either.

## 4. The target never travels in a step's input

§1.1 of the design. Every step's input is its prompt and, for a browser
step, `browser_tool`: nothing else, and no key names the target. swarm-api
stamps `qa_target_metadata(target)` (`{id, revision}`) on the agents of
`browser_steps(spec)` at submission, never from the caller, and the worker
asks for its attempt's target from that (§2.2). No value of `sign_in` or
`credential` is rendered into any prompt: the live gate signs in, and a
browser step of an `own_login` target is told only to run
`swarm-handoff sign-in` when it sees the login page again.

## 5. The findings, and their validator

`findings.json`, written by the `report` step:

```json
{"format": 1, "findings": [ … ], "about_run": [ … ]}
```

Each finding is the §4.4 shape: `id`, `title`, `severity` (`blocker`,
`major`, `minor`, `nit`), `screen`, `path`, `width` (a preset), `theme`,
`evidence`, `actual`, `expected`, `steps_to_reproduce`, and optionally
`also_seen` (`{width, theme}` each).

`qaspec.validate_findings(document, available=…)` splits it:

* **accepted** may be filed;
* **refused** carry the finding's id and what is wrong, by field name. A
  finding **without evidence fails validation**, and so does one whose
  evidence is not a relative image path, or (with `available`, every file
  name the staged tars hold) names a file no tar holds: nobody can see it,
  so it is not filed;
* **about_run** are the report's findings about the run (a missing tar, a
  review that looked at fewer images than its manifest lists). They are
  reported and never filed in the application's repository.

A document that is not format 1 with a `findings` list is
`qa_findings_invalid`. The problems quote no value from the page, and pass
through `swarm_redaction.redact` all the same.

## 6. Owner decisions this builds on

Recorded in [visual-qa-any-app.md](../design/visual-qa-any-app.md) §9
(BGD, 2026-10-22 ~06:40Z): VQ-Q1 "Any platform admin, audited" approves a
target's host (lane VQ-2); VQ-Q2 "One secret per tenant,
swarm-tenant-<t>-qa" holds the test credential (lane VQ-3); VQ-Q3 "Console
link by default; evidence branch as a per-target opt-in for private
repositories" is `file.evidence`, default `console_link`.
