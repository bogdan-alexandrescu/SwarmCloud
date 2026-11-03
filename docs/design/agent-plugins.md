# Agent plugins: gstack's skills in SwarmCloud agents, chosen per step, unattended or as a planner and designer

**Status: plan, 3 November 2026. Nothing here is built.** The owner (BGD) asked on 3 November 2026 for agents to
use Claude Code plugins chosen per runtime and per task, starting with gstack, whose planner, designer and reviewer
skills "could be super useful in making sc agents a lot more powerful and capable", and for a plan to build it as
soon as possible. Every fact below was read on 3 November 2026 on `origin/main`; files are named, lines are not,
because they move.

## 1. Decisions (BGD, 3 November 2026)

1. **Chosen per step from the start.** A workflow step (and a single task) names the plugins it uses and, for each,
   its mode and skills. There is no workflow-wide switch first. Per-step names need a frozen-contract change
   request (§5), so that request is the first lane.
2. **Baked into the agent images at a pinned version.** No install at task start.
3. **Two modes, toggled per step.**
   * **Unattended:** gstack answers every question that has a recommended option by itself and logs it. A question
     it cannot answer that way (a consent, no recommendation, a final gate) **parks the step for an answer**, and
     the same step resumes with it.
   * **Planner or designer:** gstack's planning and design skills run an interview, a back-and-forth of questions
     with a person. The step parks between questions. The plan or design it produces goes to approval, or is
     approved automatically when the run says so, as an issue run's plan is today.
4. **The interview happens on the planning page and in Voice it, both in the first release** of the planner mode.
5. **No outside AI services at first.** gstack's second-opinion reviewer and its image generation are off. Plans,
   designs and reviews run on Claude alone.

## 2. What exists today

**SwarmCloud agents load no plugin and no skill.** The Claude Code runner (`agent_worker/runners/claude_code.py`)
writes its own settings file (`write_headless_settings`: environment, hooks) and its own tool-server list
(`write_mcp_config`) under `work/.swarm-claude-code/`, and passes both on the command line. Nothing writes
`~/.claude/skills`, `enabledPlugins` or a plugin directory. `HOME` is the attempt's `work/`, which is checkpointed;
the pod's root filesystem is read-only.

**Jev is the precedent to copy.** Its two packages are copied into `images/agent-runtime-base` at fixed versions
(one vendored, one checked by hash), under `/opt/swarm/jev-gates`, root-owned and read-only. At start,
`lifecycle._quality_gates_env` and `qualitygates.prepare` decide from a signed workspace setting, the scheduler's
verdict and the workflow's switch whether to run it, write a private config, and the runner registers Jev's hooks.

**Switches are per workflow, not per step.** `DISPATCH_SWITCHES` (`swarm_api/validation.py`) is on/off only, set on
a task or a workflow, written into every step's signed `metadata.dispatch` block. `WorkflowStepCreate` has no
dispatch field. Runner profiles (`swarm_common/profiles.py`, frozen) have no field for plugins, skills or hooks.

**Images** are pinned by digest; the base image is about 730 MB and the browser image about 1.25 GB
(`docs/worker-images.md`). The base Dockerfile may not fetch from GitHub; outside sources come through the build
mirror or are vendored. Every byte added is paid on every agent start.

**Waiting for a person exists.** `PARKED(AWAITING_INPUT)` (`swarm_common/states.py`) and the `await-input` step
([human-steps.md](human-steps.md)) wait at no cost and resume on an answer. Owner questions (`questions.json`,
recorded with `POST /v1/tasks/{id}/answers`) already reach the console and this operator's session. The plan
decision page (`ApprovalDecision.tsx`, [plans-pipeline.md](plans-pipeline.md)) shows a plan and its conversation.

**gstack** (github.com/garrytan/gstack, MIT) is a skill suite installed as a git checkout at
`~/.claude/skills/gstack` with each skill linked beside it. Facts that shape the plan:

* It needs **Bun at run time** almost everywhere (81 helper scripts), plus `jq` and `git`. Its browser, design and
  PDF tools are compiled binaries, so they must be built on Linux. Its browser skills need Chromium.
* Its **unattended mode** (`GSTACK_SESSION_KIND=unattended`) answers questions that carry a recommendation, logs
  them, and leaves consents, unrecommended questions and final gates open. A bare `claude -p` counts as
  interactive, so the kind must be set.
* Its state lives in a state root (`GSTACK_HOME`), plus a `.gstack/` folder in the project, which must never land
  in a step's diff. Telemetry is off by default; the update check is on by default and must be turned off.
* It writes hooks into `~/.claude/settings.json` on setup (a Stop hook by default). Some skills carry their own
  hooks (`careful`, `guard`, `freeze`); `careful` and `guard` answer "ask", which nobody can answer in an agent.
* Its skill paths are literal (`~/.claude/skills/gstack/...`), and it ships no Claude Code plugin manifest.
* About 27 MB is skills, helpers and libraries; most of the rest of the 1.5 GB local install is development
  dependencies, the browser profile, a 112 MB prompt-injection model and compiled binaries of about 60 MB each.

## 3. The design

### 3.1 A closed plugin catalogue, owned by the platform

A catalogue in the worker and swarm-api lists each plugin SwarmCloud may load: its name (`gstack` first), pinned
version, read-only install path in the image, the runner families it supports (the five claude-code profiles
first; codex later), and per mode the **skills it may offer**. A step names plugins and skills from it by name only:
never a path, command, URL or version (invariant 10). A workspace setting, signed like the quality-gates setting,
says which catalogue plugins that workspace may use; it is admin-only and audited.

gstack's first offer, from the research:

| Mode | Skills offered |
|---|---|
| Unattended | `review`, `investigate`, `health`, `cso`, `qa-only`, `qa`, `design-review`, `document-generate`, `document-release`, `ship` (handoff only: never pushes), `plan-eng-review`, `plan-design-review`, `autoplan` |
| Planner | `office-hours`, `plan-ceo-review`, `plan-eng-review`, `plan-devex-review`, `autoplan`, `spec` (file-only) |
| Designer | `design-consultation`, `plan-design-review`, `design-review`, `design-html` (image-drawing skills such as `design-shotgun` wait for outside AI, decision 5) |

Never offered: `careful` and `guard` (they answer "ask"), `gstack-upgrade`, `land-and-deploy`, `canary`, `captain`,
`pair-agent`, `connect-chrome`, `setup-browser-cookies`, the gbrain and iOS skills, `plan-tune`, and any skill that
calls a paid model.

### 3.2 Per-step selection (frozen-contract change request)

A step carries `plugins: {"gstack": {"mode": "unattended" | "planner" | "designer", "skills": [...]}}`, validated by
swarm-api against the catalogue, the workspace setting and the step's runner profile, and signed with the step (a
spec format bump in `specsign`), so the worker trusts only what was signed. The request (§5) also asks for the
workspace settings block and, if needed, a profile marker for runners that can load plugins.

### 3.3 Packaging: baked in, pinned

* A build stage takes gstack at a pinned stable release tag from the build mirror, installs production dependencies
  only, renders the skills for Claude Code and compiles its binaries, all on Linux. It skips the Chromium download,
  the fonts step, the security scanner images and the 112 MB model (`GSTACK_SECURITY_OFF=1`).
* **The base image** gets `/opt/swarm/plugins/gstack` (skills, helpers, libraries, needed modules) and Bun.
  **The browser image** also gets gstack's browser binary, pointed at the Chromium it already carries.
* The added size is measured by the packaging lane and stated in its pull request; the base image's start time is
  measured before and after.
* A lane bumps the pinned version; CI renders every offered skill and runs each one's smoke check.

### 3.4 Start of a step

When a signed step names gstack, the worker (a module modelled on `qualitygates.prepare`):

1. links `work/.claude/skills/gstack` to the read-only install, and links **only the step's allowed skills** beside
   it, so the agent's skill list, and its prompt, carry nothing else;
2. sets the environment: `GSTACK_SESSION_KIND` for the mode, `GSTACK_HOME` under the attempt (checkpointed, so a
   resumed step keeps gstack's own state), `GSTACK_EPHEMERAL=1`, the update check, second opinions and image
   generation off, `GSTACK_CHROMIUM_NO_SANDBOX` on browser profiles;
3. adds `.gstack/` to the checkout's `.git/info/exclude`, so nothing gstack writes in the project reaches the diff;
4. registers **no** gstack settings hooks: the runner keeps writing its own settings, and skill-frontmatter hooks
   apply only to the offered skills;
5. records in the result which plugin, version, mode and skills the step had, and how many times each skill ran.

### 3.5 The question bridge: one mechanism for both modes

This is the new core piece. In a headless agent, when gstack asks a question it cannot answer by itself:

1. A hook on the question tool (gstack already hooks it for `plan-tune`) captures the question: text, options,
   recommendation, previews and the skill that asked. The runner writes it to the step's pending question and ends
   the session cleanly.
2. The worker checkpoints the workspace, the Claude session and `GSTACK_HOME` (invariant 8), and the step parks as
   `PARKED(AWAITING_INPUT)`: no lease, no slot, no container (invariants 1, 4).
3. The question appears on the planning page, in Voice it, and in this operator's owner questions. The first answer
   is recorded on the step, with who answered and where.
4. The scheduler requeues the step. A new attempt, at a new fencing generation (invariant 5), restores the
   checkpoint and resumes the Claude session with the answer as the next message.
5. A question unanswered for 24 hours (human steps' default) ends the step with the question left open, never with
   an answer nobody gave.

Unattended mode uses this only for what gstack cannot answer itself. Planner and designer modes use it for every
interview question.

### 3.6 Planner and designer mode on the planning surfaces

* **The planning page.** The plan decision page gets an interview panel: the pending question with its options,
  recommendation and previews, the questions answered so far, and "use the recommended answer". When the interview
  ends, the proposal (plan, design doc, diagrams) is shown for Approve, Edit or Reject, or approved automatically
  when the run asks for it.
* **Voice it.** Voice it reads the same pending questions aloud and records spoken answers through the same answers
  route, so a person can run a planning interview by voice. This depends on the Voice it pool, its access approval
  and its planner lane landing first.
* **The issue-run planner** gains planner mode as an option: its planner step runs gstack's planning skills, and its
  output must still fit the run's plan shape and digest, so approval, editing and auto-approval work unchanged.

## 4. Build order

Each step is a SwarmCloud lane (build, review, fix, merge) unless it says otherwise.

| # | Lane | Needs | Gives |
|---|---|---|---|
| 1 | **Contract change request:** per-step `plugins`, the signed workspace block, the spec format bump | nothing | the request, for BGD to accept |
| 2 | **Packaging spike:** gstack built into a test image stage, sizes and start time measured, every offered skill's smoke run in a container in unattended mode | nothing | the measured numbers and what breaks |
| 3 | **Question bridge spike:** prove a headless agent can capture a question through the hook, park, and resume the same Claude session with the answer | nothing | a yes or no on the core mechanism, before the build |
| 4 | **Catalogue and step start:** the catalogue, the worker module of §3.4, swarm-api validation and signing, the workspace setting | 1 accepted, 2 | unattended mode without the bridge (open questions end as owner questions until 5 lands) |
| 5 | **The bridge** of §3.5 | 3, 4 | unattended mode as decided: park and resume |
| 6 | **Console:** Config › Plugins (catalogue, versions, workspace setting), the step's plugin picker in the workflow builder, the interview panel on the planning page | 4, 5 | planner and designer modes on the page |
| 7 | **Voice it interview** | 5, the Voice it pool and planner lanes | the interview by voice |
| 8 | **Issue-run planner in planner mode** | 5, 6 | gstack planning for issue runs, with approval or auto-approval |
| 9 | Later: outside AI (second opinions, image mockups) once keys or accounts are decided; codex parity; more catalogue plugins | | |

Lanes 1, 2 and 3 start together. Image changes release through the normal image pipeline, and no image release
lands while experiment batch three runs (its conditions forbid it).

## 5. Contract change request (lane 1 writes it)

Requested, not applied: `apps/common/swarm_common/` is frozen. The request adds the step's `plugins` field (and the
same on a single task) with its shape and limits, the signed settings block for the workspace's allowed plugins
(`specsign.SETTINGS_MAC_FIELDS`), a spec format bump so a step's plugins are signed, and, if lane 2 shows it is
needed, a profile marker for the runners that can load plugins.

## 6. Invariants

1-3: a parked step holds nothing, and capacity rules are untouched. 4: a step waiting for an answer parks and exits;
it never sleeps. 5: every resumed attempt has a new generation, and a stale one exits without running the agent.
6-7: no change. 8: the step is checkpointed before it parks. 9: gstack's state lives under the attempt, inside the
tenant's own workspace and prefix; nothing is shared between tenants. 10: plugins, modes and skills are names from
the platform catalogue, never paths, commands, images or versions from a caller.

## 7. Risks the spikes answer first

* Whether headless Claude Code lets a hook capture gstack's questions and end the session cleanly (lane 3).
* gstack's literal skill paths and Bun under a read-only image with `HOME` in the workspace (lane 2).
* The base image's size and start time with Bun and gstack added (lane 2).
* The prompt cost of each offered skill's description; only the step's own skills are linked, to keep it small.
