export const meta = {
  name: 'swarmcloud',
  description: 'SwarmCloud: run a SwarmCloud workflow spec with every step executing in SwarmCloud and shown here as one [SwarmCloud] row per step, by the console\'s stage, plus at most two setup rows',
  whenToUse: 'You have a SwarmCloud workflow spec, the JSON that swarm workflow reads, and want each step visible in /workflows while it runs remotely: pass the spec object, the spec object with its file as {spec, spec_path}, its JSON text, or the path of the spec file as args. Or a workflow already running in SwarmCloud lost its rows when this session restarted: pass {attach: "<workflow_id>"} to submit nothing, report its finished steps once and start a row for every unfinished one. Or list every workflow of your tenant still running in SwarmCloud: pass {attach: "all"} for the {attach: "<workflow_id>"} call that attaches each, one run per workflow. Or show SwarmCloud SINGLE tasks -- dispatched with swarm_dispatch, in no workflow -- as live rows: pass {attach_tasks: [{task_id, title, console}]} for one [SwarmCloud] row per task, titled from its label. The sc plugin launches each run from a copy of this script titled after its workflow or its tasks (swarm_workflow_launch, swarm_dispatch\'s rows).',
  phases: [
    { title: 'Submit', detail: 'given a path, one sc:workflow agent reads the file with swarm_workflow_spec; one sc:workflow agent submits the spec with swarm_workflow by reference when it can, checked against the digest, and probes the follow every row makes' },
    { title: 'Attach', detail: 'given {attach: workflow_id}, one sc:workflow agent reads the workflow with swarm_workflow_status and probes the follow every row makes; given {attach: "all"}, one sc:workflow agent lists the running workflows and your running single tasks with swarm_workflows and the run returns the attach call for each workflow and one for the tasks; nothing is submitted' },
    { title: 'Tasks', detail: 'given {attach_tasks: [...]}, one sc:task row per single task -- a task in no workflow -- titled from its label, follows it until it finishes; nothing is submitted' },
    { title: 'Result', detail: 'one sc:workflow agent reads the workflow state SwarmCloud derived with one swarm_workflow_status call, and re-attaches once any step whose row ended UNKNOWN while the workflow still runs' },
  ],
}

// /sc:swarmcloud -- a SwarmCloud workflow, shown as a Claude Code workflow.
// (Named `run` until 2026-10-01; the owner asked that it read as SwarmCloud
// in /workflows, and that every row carry a `[SwarmCloud]` prefix.)
//
// Every step EXECUTES in SwarmCloud: one agent submits the whole spec, and
// SwarmCloud owns the DAG from then on -- dependencies, input_from staging,
// on_step_failure, retries. Each step is then SHOWN here by one sc:step agent
// that follows that step's task until it finishes, so /workflows lists a row
// per step with its phase, its state and its elapsed time. Each row is
// labelled `[SwarmCloud] <workflow name> · stage <n> · <step>`.
//
// THE ROWS ARE THE CONSOLE'S STEPS (owner request, 2026-10-04: "they should
// be identically structured either here or there"; owner chose steps-only
// counting and a 1:1 mapping). One sc:step row per SwarmCloud step, no extra
// and none missing, each with the stage number the console's Steps table and
// Graph give it -- the step's level in the DAG plus one, apps/swarm-ui's
// `levelsOf` -- in the console's order: level by level, the workflow's own
// step order within a level. A spec's `stage` key is NOT used: the bridge
// never sends it (swarm_mcp.workflows.DISPLAY_ONLY_STEP_KEYS), so the console
// cannot show it, and a row that did would differ from the console. Every
// other row is labelled `[SwarmCloud] <workflow> · setup · <what>` so it is
// never read as a step. A spec given as an object makes N + 2 rows: the submit
// (which also probes the follow) and the Result (one status read). Neither
// can go: a script cannot call a tool, so submitting and reading the derived
// state each take an agent. A bare path adds one `setup · read spec` row, for
// the outline the reply is checked against; /sc run never passes one.
//
// THE RUN IS NAMED AFTER THE WORKFLOW. `meta` is a literal and a running
// script cannot rename its run, so the plugin launches each run from a copy
// of this script whose meta.name is `SC · <name> · N steps` (`SC · <label> ·
// task` for single tasks) and whose meta.description starts with its DETAILS
// -- `Workspace: <workspace> (<tenant>) · Started by: ... · Profiles: ... ·
// LLM: ... · Browser tool: ... · Session: ...` (SC-WS-DESC, then
// SC-DESC-DETAILS, owner 2026-10-14) -- never with the workspace in the name
// (swarm_mcp.launch, the swarm_workflow_launch tool). Inside, the same name
// and step count open the first row's label, and the Result row adds the
// workflow id, which only exists once SwarmCloud has answered. No row label
// carries `[<workspace>]`: each row's own details line ends its prompt, which
// is what Claude Code shows when the row is expanded (rowDetails below).
//
// A ROW SHOWS PROGRESS, NOT THE LOG (owner decision, 2026-10-01). Rows
// once followed the remote agent's narrated log: 5-16 KB per reply, re-read
// on every turn -- 4.0M tokens for one row, ~22M for six three-step
// workflows. A row follows `format: "progress"` and nothing else, and writes
// one short line only when its task's state changes.
//
// A ROW NEVER FALLS BACK (owner decision, 2026-10-01). 19 rows over 4
// workflows quietly followed the log instead because a pinned bridge answered
// that it did not know the progress format. So the row that submits (or
// attaches) probes the bridge once, with the exact follow the rows make, and
// a refusal ends this workflow -- FOLLOW_REFUSED, naming the bridge's version
// and the likely cause -- before any row starts. The workflow itself runs on
// in SwarmCloud; attach re-joins it once the bridge is fixed.
//
// A step's row waits for its OWN task only. It may start before its parents
// finish; it is handed its unfinished parents' task ids and makes ONE call
// that holds until they finish (swarm_follow `parents`), because a waiting
// task holds no capacity and has nothing to report until then.
//
// ATTACH (owner decision, 2026-10-01). Given {attach: "<workflow_id>"} this
// script submits NOTHING: one sc:workflow row reads the workflow, a finished
// step is reported once without a row, every unfinished step gets the same
// sc:step row as after a submission, and the run ends with the same status
// read. It is how a session that restarted re-joins a workflow still running
// in SwarmCloud. An id the API does not know -- or another tenant's, which it
// answers as unknown -- ends the run NOT_ATTACHED with the API's own error.
//
// ATTACH ALL (owner decision, 2026-10-02; one run per workflow, 2026-10-04).
// Given {attach: "all"} this script submits nothing and attaches nothing: one
// sc:workflow row lists the caller's tenant's running workflows
// (swarm_workflows), and the run returns, for each of the first
// MAX_ATTACHED_WORKFLOWS, the exact {attach: "<workflow_id>"} call that
// attaches it -- each its OWN Claude Code run, so each SwarmCloud workflow has
// a counter of its own. This run cannot start them: a workflow script has no
// way to start a separate run (`workflow()` nests the child inside this one,
// sharing its agent counter, which is the combined count the owner asked to
// end). The sc skill's `/sc attach --all` launches one run per workflow
// through swarm_workflow_launch; the rest are returned as not_followed, each
// with the /sc attach that follows it.
//
// ROWS SURVIVE A MISSING OR UNRESPONSIVE BRIDGE (owner, 2026-10-04; measured
// on wf_425effa5481d498bb305, where two rows answered UNKNOWN with the bridge
// "not connected" / "not responding" while the workflow ran on). An sc:step
// agent's tools are fixed when it starts -- it has no ToolSearch -- so a row
// started while the bridge was reconnecting cannot gain swarm_follow. The row
// therefore answers at once, and THIS SCRIPT starts the step's row again, under
// the same label, up to BRIDGE_TRIES times with an sc:wait row of
// BRIDGE_RETRY_S between tries (a script has no timer); the last
// such UNKNOWN's last_error ends `resume: /sc attach <workflow_id>`. The
// Result, when SwarmCloud says the workflow still runs, re-attaches every
// unfinished step whose row ended UNKNOWN, once.
//
// SINGLE TASKS (#830, owner 2026-10-07: "we need to be able to auto attach on
// single tasks too"). A task sent with swarm_dispatch belongs to no workflow,
// so no attach above reaches it. Given {attach_tasks: [{task_id, title,
// console}]} this script submits nothing and starts ONE sc:task row per task,
// labelled `[SwarmCloud] <title> · task`, where the title is the task's label
// (else its id). sc:task follows a task WITHOUT step_id: sc:step always passes
// one, and the bridge stops a step row whose task is no workflow step. The
// args come from the bridge -- swarm_dispatch's `rows` for the tasks it just
// sent, swarm_workflow_launch {attach: "all"} for the caller's running single
// tasks -- and {attach: "all"} launched by name returns them as `task_call`.
//
// Steps are grouped under 'Stage N', the console's stage. It is not known
// before the spec is read, so it is not in meta.phases and each gets a
// progress group of its own.
//
// THE SPEC IS NOT RETYPED WHEN IT CAN BE AVOIDED (measured 2026-10-01: specs
// of 11-44 KB came back altered from the relay and were refused three times in
// one evening). A script cannot call a tool, so whatever reaches
// swarm_workflow goes through an agent. Given the spec with its file
// ({spec, spec_path}, which is what /sc run passes), the agent is handed the
// PATH and swarm_workflow reads the bytes itself; given a path alone, the
// bridge reads the file (swarm_workflow_spec) and hands back a `spec_ref` it
// holds, which the submit row passes on. Only a spec given as an object or
// JSON text with no file behind it is still carried inline.
//
// NOTHING CROSSES THE RELAY UNCHECKED. The step-to-task map comes back through
// the relay too, and a haiku relay can drop a step, swap two task ids or tidy
// a prompt, and nothing downstream would notice. So:
//   * the script computes the spec's digest (specDigest, the same function as
//     swarm_mcp.workflows.spec_digest) and the agent passes it beside the
//     spec: swarm_workflow refuses, before sending anything, a spec that
//     arrived different;
//   * the reply must carry that digest back, every one of the spec's step ids
//     (none dropped, none renamed), each spec step's depends_on exactly, and
//     one distinct task per step -- or no row starts. A step the reply names
//     beyond the spec is SwarmCloud's addition, and gets a row, when the
//     bridge marked it `added` or it hangs off the spec's steps (serverAdded);
//     any other extra step is refused;
//   * each sc:step row passes its step_id to swarm_follow, which will not
//     follow a task that is a different step.
//
// A SUBMISSION WHOSE OUTCOME IS UNKNOWN IS NOT 'NOT SUBMITTED'. agent()
// resolves to null when its row is stopped and throws when StructuredOutput
// validation runs out of attempts -- either can happen after swarm_workflow
// created the workflow. Claude Code re-runs a failed or stopped agent on
// relaunch and the API has no idempotency key, so calling that NOT_SUBMITTED
// would invite a second copy. Only an explicit refusal from swarm_workflow is
// NOT_SUBMITTED.
//
// A SPEC FILE IS READ BY THE BRIDGE. A script has no filesystem, so given a
// path (anything that is not a spec object or JSON text) one sc:workflow row
// calls swarm_workflow_spec, which reads and checks the file in the session's
// checkout and returns its digest, a `spec_ref` and the spec's OUTLINE -- its
// label, strategy and merge choice and each step's id, depends_on and stage,
// with the runner profile, verdict gate and input_from `appendable` reads. The row relays those, never
// the spec; the submit row passes the ref and the digest, and the reply is
// checked against the outline exactly as it is checked against a spec.
//
// A CONSOLE LINK IS COPIED, NEVER BUILT (owner decision, 2026-10-01). The API
// serves `links.console` on every task, workflow and step, the bridge hands it
// on as `console`, and the relay rows copy it into their answers. This script
// prints that string -- the workflow's on the submit and result lines, each
// step's on its final line -- and spells no host and no path of its own. A
// null or missing link prints nothing.
//
// EVERY ROW SAYS WHERE ITS TASK IS WATCHED, WHATEVER ITS STATE (owner request,
// 2026-10-04). A step's row LABEL -- the text /workflows shows for it -- ends
// with the link the submit or attach reply carried for that step, from the
// moment the row exists: a QUEUED or PARKED step that has not started, and may
// not for an hour, is exactly the one a person opens the console to look at.
// The Result row's label ends with the workflow's link the same way. The link
// is a bare https URL, not an OSC 8 hyperlink: the label is plain text in
// Claude Code's row list, and a terminal makes a bare URL clickable where an
// escape sequence the renderer does not pass through would print as noise.
// Each row is also handed its step's link (`console:` in its prompt) to end
// its progress lines with when a follow reply carries none.
//
// This script derives nothing SwarmCloud decides. The workflow's final state
// is read back from swarm_workflow_status, which serves the state the server
// derived from its steps; it is never computed here from the rows.

// What a relay answers for a console link: the bridge reply's `console`,
// copied, or null when the reply has none.
const CONSOLE = {
  type: ['string', 'null'],
  description: "the reply's `console`, copied character for character; null when the reply has none. Never build a link",
}

// `workspace` in the relays' answers below: the tenant the API's document
// names for the workflow or task (SC-ROWS), which every row is labelled with.
// Never required: a bridge before 0.5.34 sends none, and its rows are then
// labelled as before.
const WORKSPACE = { type: ['string', 'null'] }

const SUBMITTED = {
  type: 'object',
  properties: {
    workflow_id: { type: ['string', 'null'] },
    workspace: WORKSPACE,
    console: CONSOLE,
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          step_id: { type: 'string' },
          task_id: { type: ['string', 'null'] },
          depends_on: { type: 'array', items: { type: 'string' } },
          console: CONSOLE,
          // The bridge's mark: true for a step SwarmCloud added to the spec
          // it submitted (a re-review, a merge), false for a spec step. Not
          // required: a bridge before 0.5.32 sends none (SPEED-D5).
          added: { type: ['boolean', 'null'] },
        },
        required: ['step_id', 'task_id', 'depends_on', 'console'],
      },
    },
    repository: { type: ['string', 'null'] },
    // Owner decision, 2026-09-26: every inference note the bridge's reply
    // carries (uncommitted changes not visible, the branch's upstream, the
    // checkout path) must reach here -- it exists only in that reply, and a
    // session watching this workflow has no other way to see it.
    repository_notes: { type: 'array', items: { type: 'string' } },
    spec_digest: { type: ['string', 'null'] },
    // The probe: the bridge's own version, and its refusal of the follow every
    // row makes (null when it answered).
    bridge_version: { type: ['string', 'null'] },
    follow_error: { type: ['string', 'null'] },
    error: { type: ['string', 'null'] },
  },
  required: ['workflow_id', 'console', 'steps', 'repository', 'repository_notes', 'spec_digest', 'bridge_version', 'follow_error', 'error'],
}

const ATTACHED = {
  type: 'object',
  properties: {
    workflow_id: { type: ['string', 'null'] },
    workspace: WORKSPACE,
    // The names SwarmCloud stored: the spec's title, and its label.
    title: { type: ['string', 'null'] },
    label: { type: ['string', 'null'] },
    console: { type: ['string', 'null'] },
    state: { type: ['string', 'null'] },
    state_note: { type: ['string', 'null'] },
    bridge_version: { type: ['string', 'null'] },
    follow_error: { type: ['string', 'null'] },
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          step_id: { type: 'string' },
          task_id: { type: ['string', 'null'] },
          depends_on: { type: 'array', items: { type: 'string' } },
          state: { type: ['string', 'null'] },
          console: { type: ['string', 'null'] },
        },
        required: ['step_id', 'task_id', 'depends_on', 'state', 'console'],
      },
    },
    error: { type: ['string', 'null'] },
  },
  required: ['workflow_id', 'title', 'label', 'console', 'state', 'state_note', 'bridge_version', 'follow_error', 'steps', 'error'],
}

// What the LIST row answers: swarm_workflows' `count` and, for each workflow
// it listed, the five fields this script uses. Never the steps: each workflow
// is read again by its own attach run, which checks it against the API.
const LISTED = {
  type: 'object',
  properties: {
    count: { type: ['integer', 'null'] },
    workflows: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          workflow_id: { type: 'string' },
          workspace: WORKSPACE,
          title: { type: ['string', 'null'] },
          label: { type: ['string', 'null'] },
          state: { type: ['string', 'null'] },
          console: CONSOLE,
          // SC-SESSION-SCOPE: swarm_workflows marks what this Claude Code
          // session started; only those are attached.
          this_session: { type: 'boolean' },
          // BOARD-ORDER: where it stands, as swarm_workflows serves it --
          // what groupByWorkspace orders the attach calls by.
          progress: {
            type: ['object', 'null'],
            properties: {
              done: { type: ['integer', 'null'] },
              total: { type: ['integer', 'null'] },
              age_seconds: { type: ['integer', 'null'] },
            },
          },
        },
        required: ['workflow_id', 'title', 'label', 'state', 'console', 'this_session'],
      },
    },
    // The caller's running single tasks (#830): swarm_workflows'
    // `single_tasks`, each the three fields a task row needs.
    single_tasks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          task_id: { type: 'string' },
          workspace: WORKSPACE,
          label: { type: ['string', 'null'] },
          state: { type: ['string', 'null'] },
          console: CONSOLE,
          this_session: { type: 'boolean' },
        },
        required: ['task_id', 'label', 'state', 'console', 'this_session'],
      },
    },
    single_tasks_error: { type: ['string', 'null'] },
    error: { type: ['string', 'null'] },
  },
  required: ['count', 'workflows', 'single_tasks', 'single_tasks_error', 'error'],
}

// THE CAP on {attach: "all"}: the running workflows closest to finish are
// followed, each in a run of its own, 100 by default (BOARD-ORDER, owner
// 2026-10-16: rows for every launch; 30 since SC-ROWS, 10 before). swarm_mcp.launch.MAX_ATTACHED_WORKFLOWS
// is the same default for the runs the skill launches.
// Every unfinished step of a followed workflow is one live sc:step row -- an
// agent of its own, holding a turn in this session for as long as its task
// runs -- so it stays a cap: the plugin's `max_live_rows` option moves it
// within 1..MAX_LIVE_ROWS_LIMIT, and reaches this script as the `max_rows` the
// bridge puts in each copy's args (a script cannot read the plugin's options).
// Claude Code's /workflows panel is its own UI and this script cannot make it
// scroll, so each row is one compact line, closest to finish first, grouped
// by workspace. The rest -- the ones furthest from finishing -- are listed
// with the command that follows each.
const MAX_ATTACHED_WORKFLOWS = 100

// THE CAP on single-task rows in one run: each is an agent of its own in this
// session for as long as its task runs, exactly like a step row, so the same
// readability limit applies (swarm_mcp.launch.MAX_ATTACHED_TASKS is the same
// cap for the copies the bridge writes). The rest are listed, not followed.
const MAX_ATTACHED_TASKS = 100

// The most `max_live_rows` can be set to (swarm_mcp.config.MAX_LIVE_ROWS_LIMIT).
const MAX_LIVE_ROWS_LIMIT = 200

// `given` (the args' `max_rows`) as a row cap: a whole number held to
// 1..MAX_LIVE_ROWS_LIMIT, `fallback` when it is absent or not a number.
// swarm_mcp.config.clamp_live_rows is the same rule.
function liveRowCap(given, fallback) {
  const value = typeof given === 'string' && given.trim() !== '' ? Number(given) : given
  if (typeof value !== 'number' || !isFinite(value)) return fallback
  return Math.max(1, Math.min(MAX_LIVE_ROWS_LIMIT, Math.trunc(value)))
}

const READ = {
  type: 'object',
  properties: {
    path: { type: ['string', 'null'] },
    spec_ref: { type: ['string', 'null'] },
    spec_digest: { type: ['string', 'null'] },
    outline: {
      type: ['object', 'null'],
      properties: {
        title: { type: ['string', 'null'] },
        label: { type: ['string', 'null'] },
        steps: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              step_id: { type: 'string' },
              depends_on: { type: 'array', items: { type: 'string' } },
              stage: { type: ['string', 'null'] },
              // What `appendable` reads to know the merge steps SwarmCloud
              // appends: a stated merge step, the verdict gate, the staged files.
              runner_profile: { type: ['string', 'null'] },
              when_step: { type: ['string', 'null'] },
              input_from: { type: 'object', additionalProperties: { type: 'string' } },
            },
            required: ['step_id', 'depends_on', 'stage', 'runner_profile', 'when_step', 'input_from'],
          },
        },
        strategy: { type: ['string', 'null'] },
        merge: { type: ['string', 'null'] },
      },
      required: ['title', 'label', 'steps', 'strategy', 'merge'],
    },
    error: { type: ['string', 'null'] },
  },
  required: ['path', 'spec_ref', 'spec_digest', 'outline', 'error'],
}

// The states a task does not leave: swarm_common.states.TERMINAL_STATES,
// restated because a script cannot import it; a unit test holds the two equal.
const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']

// SC-SESSION-SCOPE (owner 2026-10-14): a submission whose outcome is unknown,
// or that was created but did not match the spec, may have work running in
// SwarmCloud, so it ARMS THE WATCH like a clean one does -- only a refusal
// (NOT_SUBMITTED) sent nothing. The session arms with the `arm_watch`
// swarm_workflow_launch handed it before this run (skill: "Arming the watch").
const ARM_ANYWAY = 'arm the watch: work may be running in SwarmCloud. Make the arm_watch call swarm_workflow_launch returned for this run (the skill\'s "Arming the watch"; CronList first, never a second job)'


// What an sc:step row returns: `{state, result}`, `result` copied as given
// from the bridge's last reply (owner decision, 2026-10-05, lane review B2).
// Every one of the 14 StructuredOutput calls refused that day was a bare null
// -- `"pr_url": ,` -- typed by a Haiku relay retyping the outcome field by
// field, and one review row lost a SUCCEEDED result that way. The bridge now
// builds `result` with NO null in it (swarm_mcp.compact.step_result): an empty
// text is "", a figure that was not recorded is left out, never 0.
// stepAnswer() reads it back into the flat step result the rest of this
// script takes. The other way -- this script reading every outcome itself
// with a swarm_workflow_status call -- was not taken: that read names each
// step's state but not its answer, cost, duration or pull request, and it is
// one more relay row returning StructuredOutput, the very thing that failed.
//
// Nothing is required, and every field also takes a null (SPEED-D5,
// 2026-10-14). ROW-SKIP (part of #1194; measured 2026-10-12) loosened every
// field but `state`: Re-review and merge rows
// whose agent was SKIPPED -- no answer, no artifacts -- ran out of their five
// StructuredOutput attempts on "must have required property 'answer_excerpt',
// 'pr_url', 'artifacts', 'last_error', 'console'" and on a missing root
// `state`, and the row ended in row_error over a step that had finished. The
// empty fields carry nothing stepAnswer() needs: it reads a missing or null
// text back as null, a missing or null list as [], and the root `state` only
// when `result.state` is absent. Rows on plugin 0.5.27 still ran out of their
// five attempts on a missing `state`, and others ended "subagent completed
// without calling StructuredOutput" -- an error row over a finished step. So
// `state` is optional too, `result` may be null, and a row that answers with
// no state, or throws, takes its state from one swarm_workflow_status read
// (rowFromStatus) instead of ending as an error row.
const STEP_FIELDS = {
  type: ['object', 'null'],
  properties: {
    state: { type: ['string', 'null'] },
    answer_excerpt: { type: ['string', 'null'] },
    cost_usd: { type: ['number', 'null'] },
    duration_s: { type: ['number', 'null'] },
    pr_url: { type: ['string', 'null'] },
    artifacts: { type: ['array', 'null'], items: { type: 'string' } },
    last_error: { type: ['string', 'null'] },
    console: { type: ['string', 'null'] },
    questions: { type: ['array', 'null'] },
  },
  required: [],
}

const STEP_RESULT = {
  type: 'object',
  properties: {
    state: { type: ['string', 'null'] },
    result: STEP_FIELDS,
  },
  required: [],
}

// A row's `{state, result}` as the flat step result: an empty text is null, a
// figure left out is null (not recorded, never 0), and a link is kept only
// when there is one. The bridge's own `result.state` wins over the row's copy
// of it. An answer already flat (a row from before 0.5.17) is taken as it is;
// one whose `result` is null is read from its root `state` alone.
function stepAnswer(answer) {
  if (!answer || typeof answer !== 'object') return answer
  if (!answer.result || typeof answer.result !== 'object') {
    if (!Object.prototype.hasOwnProperty.call(answer, 'result')) return answer
  }
  const given = answer.result && typeof answer.result === 'object' ? answer.result : {}
  const text = (value) => (typeof value === 'string' && value !== '' ? value : null)
  const figure = (value) => (typeof value === 'number' && isFinite(value) ? value : null)
  const flat = {
    state: text(given.state) || text(answer.state),
    answer_excerpt: text(given.answer_excerpt),
    cost_usd: figure(given.cost_usd),
    duration_s: figure(given.duration_s),
    pr_url: text(given.pr_url),
    artifacts: Array.isArray(given.artifacts) ? given.artifacts.filter((name) => typeof name === 'string' && name) : [],
    last_error: text(given.last_error),
  }
  if (text(given.console)) flat.console = given.console
  return flat
}

const WORKFLOW_STATE = {
  type: 'object',
  properties: {
    state: { type: ['string', 'null'] },
    state_note: { type: ['string', 'null'] },
    console: CONSOLE,
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          step_id: { type: 'string' },
          state: { type: ['string', 'null'] },
        },
        required: ['step_id', 'state'],
      },
    },
  },
  required: ['state', 'state_note', 'console', 'steps'],
}

// What /sc:swarmcloud was given: { attachAll } for {attach: "all"}; { attach }
// for {attach: "<workflow_id>"}; { attachTasks } for {attach_tasks: [...]};
// { spec, specPath } for a spec object with the file it came from; { spec }
// for a spec object or its JSON text; or { path } for anything else -- the
// path of a spec file, which the bridge reads (the READ SPEC row below). Text
// that begins like JSON is JSON with a mistake in it, and is reported as that
// rather than looked for as a file.
function readSpec(given) {
  let value = given
  if (typeof value === 'string') {
    const text = value.trim()
    const first = text.charAt(0)
    if (text && first !== '{' && first !== '[' && first !== '"') return { path: text }
    try {
      value = JSON.parse(text)
    } catch (error) {
      throw new Error('/sc:swarmcloud takes a SwarmCloud workflow spec object, its JSON text, the path of a spec file, or {attach: "<workflow_id>"}; its argument begins like JSON and is not JSON: ' + error.message)
    }
    if (typeof value === 'string' && value.trim()) return { path: value.trim() }
  }
  const isObject = value !== null && typeof value === 'object' && !Array.isArray(value)
  if (isObject && Object.prototype.hasOwnProperty.call(value, 'attach_tasks')) {
    return { attachTasks: readTasks(value.attach_tasks), maxRows: liveRowCap(value.max_rows, MAX_ATTACHED_TASKS) }
  }
  if (isObject && Object.prototype.hasOwnProperty.call(value, 'attach')) {
    const id = typeof value.attach === 'string' ? value.attach.trim() : ''
    // `all` is never a workflow id: the API's ids begin wf_.
    if (id === 'all' || id === '--all') return { attachAll: true, maxRows: liveRowCap(value.max_rows, MAX_ATTACHED_WORKFLOWS) }
    if (!id) {
      throw new Error('/sc:swarmcloud {attach: "<workflow_id>"} needs the id of a workflow already submitted to SwarmCloud; it was given ' + JSON.stringify(value.attach === undefined ? null : value.attach))
    }
    // `title`: the name swarm_workflow_launch titled this run with, and
    // `workspace` the one it read off the API's document, for the attach row
    // until the workflow itself is read again.
    // `details`: each step row's details line, as the bridge wrote it.
    return { attach: id, title: flatText(value.title), workspace: flatText(value.workspace), details: readDetails(value.details) }
  }
  if (isObject && typeof value.spec_path === 'string' && value.spec_path.trim()) {
    return { spec: checkSpec(value.spec), specPath: value.spec_path.trim(), details: readDetails(value.details) }
  }
  // A spec object handed with `details` beside it (swarm_workflow_launch's
  // `row_details`) is `{spec, details}`; the spec itself never has the key.
  if (isObject && value.spec && typeof value.spec === 'object' && value.details) {
    return { spec: checkSpec(value.spec), details: readDetails(value.details) }
  }
  return { spec: checkSpec(value), details: {} }
}

// `{step_id: step}` of a spec, for rowDetails' fallback.
function specStepsById(spec) {
  const out = {}
  for (const step of (spec && Array.isArray(spec.steps) ? spec.steps : [])) {
    if (step && typeof step.step_id === 'string') out[step.step_id] = step
  }
  return out
}

// {attach_tasks}: each entry `{task_id, title, console}` (or a bare task id),
// de-duplicated in order. A list naming no task is refused: there is nothing
// to show, and a run with no row would read as one that worked.
function readTasks(given) {
  const tasks = []
  const seen = {}
  for (const entry of Array.isArray(given) ? given : []) {
    const raw = typeof entry === 'string' ? entry : entry && typeof entry === 'object' ? entry.task_id : null
    const id = flatText(raw)
    if (!id || seen[id]) continue
    seen[id] = true
    const isEntry = entry !== null && typeof entry === 'object'
    tasks.push({
      task_id: id,
      title: isEntry ? flatText(entry.title) : null,
      workspace: isEntry ? flatText(entry.workspace) : null,
      console: isEntry ? consoleLink(entry.console) : null,
      details: isEntry ? detailsText(entry.details) : null,
    })
  }
  if (tasks.length === 0) {
    throw new Error('/sc:swarmcloud {attach_tasks: [{task_id, title, console}]} needs the id of at least one SwarmCloud task; it was given ' + JSON.stringify(given === undefined ? null : given))
  }
  return tasks
}

function checkSpec(given) {
  let spec = given
  if (spec && typeof spec === 'object' && !Array.isArray(spec) && spec.spec && typeof spec.spec === 'object') {
    spec = spec.spec
  }
  if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !Array.isArray(spec.steps) || spec.steps.length === 0) {
    throw new Error('/sc:swarmcloud takes a SwarmCloud workflow spec: an object with a non-empty `steps` list, the shape `swarm workflow` reads')
  }
  return spec
}

// The spec as canonical JSON: keys sorted, no whitespace, each scalar exactly
// as JSON.stringify writes it. swarm_mcp.workflows.spec_digest builds the same
// text in Python, and the plugin's tests run this function and hold the two
// digests equal.
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map((item) => canonical(item)).join(',') + ']'
  if (value !== null && typeof value === 'object') {
    const keys = Object.keys(value).sort()
    return '{' + keys.map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  }
  return JSON.stringify(value)
}

// FNV-1a over the canonical text's UTF-8 bytes, 32 bits: 'fnv1a32:' and eight
// hex digits. Written out because a workflow script has no crypto, no imports
// and no guaranteed TextEncoder. It catches a careless relay, which is its job.
function specDigest(spec) {
  const text = canonical(spec)
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    let code = text.charCodeAt(i)
    if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) {
      const low = text.charCodeAt(i + 1)
      if (low >= 0xdc00 && low <= 0xdfff) {
        code = 0x10000 + (code - 0xd800) * 1024 + (low - 0xdc00)
        i++
      }
    }
    let bytes
    if (code < 0x80) bytes = [code]
    else if (code < 0x800) bytes = [0xc0 | (code >> 6), 0x80 | (code & 63)]
    else if (code < 0x10000) bytes = [0xe0 | (code >> 12), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63)]
    else bytes = [0xf0 | (code >> 18), 0x80 | ((code >> 12) & 63), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63)]
    for (const byte of bytes) hash = Math.imul(hash ^ byte, 0x01000193) >>> 0
  }
  return 'fnv1a32:' + ('0000000' + hash.toString(16)).slice(-8)
}

// Each step's depth in the DAG: 0 for a step with no parents, else one more
// than its deepest parent -- the console's `levelsOf` (apps/swarm-ui/src/
// dag.ts), whose Stage N is this depth plus one. A parent the list does not
// hold counts as depth 0, and a cycle ends at 0, as there. Only for labelling
// rows; SwarmCloud schedules.
function levelsOf(steps) {
  const byId = {}
  for (const step of steps) byId[step.step_id] = step
  const depth = {}
  const visiting = {}
  function walk(id) {
    if (depth[id] !== undefined) return depth[id]
    if (visiting[id] || !byId[id]) return 0
    visiting[id] = true
    let deepest = 0
    for (const parent of byId[id].depends_on || []) deepest = Math.max(deepest, walk(parent) + 1)
    visiting[id] = false
    depth[id] = deepest
    return deepest
  }
  for (const step of steps) walk(step.step_id)
  return depth
}

// The console's stage number of every step, and the console's order: level by
// level, the workflow's own step order within a level (stepviews.ts
// `stepOrder`). `steps` is the WHOLE workflow, finished steps included: an
// attach that left them out would number its rows from the wrong level.
function consoleStages(steps) {
  const depth = levelsOf(steps)
  const stage = {}
  for (const step of steps) stage[step.step_id] = (depth[step.step_id] || 0) + 1
  const order = steps
    .map((step, index) => ({ id: step.step_id, index: index }))
    .sort((a, b) => stage[a.id] - stage[b.id] || a.index - b.index)
    .map((entry) => entry.id)
  return { stage: stage, order: order }
}

function stagePhase(stage) {
  return 'Stage ' + stage
}

// THE RUN'S TITLE: `SC · <name> · N steps`, at most TITLE_CHARS characters,
// the name cut at the last word that fits, with `…`. Owner 2026-10-04:
// SwarmCloud prefix, at most 100; owner 2026-10-05: prefix `SC · ` because
// Claude Code's task panel shows ~28 characters of a name, and at most 150
// because Enter shows the full name. swarm_mcp.workflows.workflow_title writes
// the same title into the per-run copy's meta.name, and this Result title is
// that title: no `[<workspace>] ` in either since SC-DESC-DETAILS (owner
// 2026-10-14: "workflows in here should not have the [Engineering] or
// [eng@saga.xyz] prefix in front of the names") -- the copy's
// meta.description leads with the workspace instead -- and the plugin's tests
// hold the two cuts equal. Counted in code points, as Python counts.
const TITLE_CHARS = 150
const TITLE_PREFIX = 'SC · '

// `text` with every run of whitespace one space and none at either end; null
// for anything that is not text or holds none.
function flatText(text) {
  if (typeof text !== 'string') return null
  const words = []
  let word = ''
  for (const ch of text) {
    if (ch.trim() === '') {
      if (word) words.push(word)
      word = ''
    } else {
      word += ch
    }
  }
  if (word) words.push(word)
  return words.length > 0 ? words.join(' ') : null
}

// The first of `candidates` that holds text: the spec's title, its label,
// the title or label SwarmCloud stored, the workflow id -- in that order,
// never a summary anyone wrote for it.
function titleName(candidates) {
  for (const candidate of candidates) {
    const name = flatText(candidate)
    if (name) return name
  }
  return 'workflow'
}

function stepsText(count) {
  return count === 1 ? '1 step' : count + ' steps'
}

// ` · workspace: <workspace>`, or '' when none is known: a narrator line that
// lists work of several workspaces still says whose each is (SC-ROWS), at
// its END -- never `[<workspace>]` in front of a name (SC-DESC-DETAILS). The
// workspace is never cut: a cut name could be another workspace's.
function workspaceNote(workspace) {
  const flat = flatText(workspace)
  return flat ? ' · workspace: ' + flat : ''
}

// `SC · <name> · N steps`: the NAME is cut, never the step count
// (swarm_mcp.workflows.workflow_title).
function workflowTitle(name, count) {
  const tail = ' · ' + stepsText(count)
  const room = Math.max(1, TITLE_CHARS - Array.from(TITLE_PREFIX).length - Array.from(tail).length)
  let chars = Array.from(flatText(name) || 'workflow')
  if (chars.length > room) {
    let head = chars.slice(0, room - 1)
    if (chars[room - 1] !== ' ' && head.indexOf(' ') >= 0) head = head.slice(0, head.lastIndexOf(' '))
    while (head.length > 0 && head[head.length - 1] === ' ') head.pop()
    chars = head.concat(['…'])
  }
  return TITLE_PREFIX + chars.join('') + tail
}

function duration(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds)) return 'duration not recorded'
  const whole = Math.round(seconds)
  const minutes = Math.floor(whole / 60)
  const rest = whole % 60
  return minutes > 0 ? minutes + 'm' + String(rest).padStart(2, '0') + 's' : rest + 's'
}

// Null is NOT MEASURED, never $0.00: a run with no recorded spend must not read
// as a free one.
function money(usd) {
  return typeof usd === 'number' && isFinite(usd) ? '$' + usd.toFixed(2) : 'cost not recorded'
}

function pullRequest(url) {
  if (typeof url !== 'string' || !url) return null
  const at = url.lastIndexOf('/pull/')
  return at >= 0 ? 'PR #' + url.slice(at + 6).split('/')[0] : 'PR ' + url
}

function clip(text, limit) {
  const flat = String(text).split('\n').join(' ')
  return flat.length <= limit ? flat : flat.slice(0, limit - 1) + '…'
}

// Every row's label: `[SwarmCloud] <name> · <suffix>`, at most LABEL_CHARS,
// then ` · <console link>` when the API served one. The NAME is cut, never the
// suffix, so a long workflow label still shows which stage and which step the
// row is -- and never the link, which sits outside the budget because a
// clipped URL opens nothing. 100, the run title's own cap: the Result row's
// suffix carries the step count and the workflow id (about 50 characters), and
// at 80 that left a name of a dozen characters.
const LABEL_PREFIX = '[SwarmCloud] '
const LABEL_CHARS = 100

// A row that is not a step: `[SwarmCloud] <name> · [<facts> · ]setup · <what>`.
function setupLabel(name, facts, what, link) {
  return rowLabel(name, facts.concat(['setup', what]).join(' · '), link)
}

// NO WORKSPACE IN ANY LABEL (SC-DESC-DETAILS, owner 2026-10-14 ~22:00 PDT:
// "workflows in here should not have the [Engineering] or [eng@saga.xyz]
// prefix in front of the names"). SC-ROWS put `[<workspace>] ` after the
// prefix; the workspace is now the first field of each row's details line
// (rowDetails), which Claude Code shows when the row is expanded.
function rowLabel(name, suffix, link) {
  const tail = ' · ' + suffix
  const room = Math.max(8, LABEL_CHARS - LABEL_PREFIX.length - tail.length)
  const served = consoleLink(link)
  return LABEL_PREFIX + clip(name, room) + tail + (served ? ' · ' + served : '')
}

// THE DETAILS OF A ROW (SC-DESC-DETAILS, owner 2026-10-14: "a section in the
// workflow or task description when you expand it in claude, that will
// provide more details like user that created the job, tenant or workspace
// that is using and maybe even what LLM subscription or config is using").
// One line, the six fields in swarm_mcp.details.line's order. The Workflow
// API gives an agent no description of its own, so the line is the LAST line
// of the row's prompt -- `details: <line>` -- which is what Claude Code shows
// when the row is expanded; sc:step and sc:task read it as display only.
// The bridge writes it from the API's documents into each copy's args
// (`details.steps` for a workflow, `details` on each attach_tasks entry);
// a run launched without it gets the fields this script knows -- the
// workspace, and a spec step's profile and browser tool -- and `not
// reported` for the rest, never a guess.
const DETAILS_CHARS = 600
const NOT_REPORTED = 'not reported'
const BROWSER_PROFILE = 'claude-code-browser'

function detailsText(text) {
  const flat = flatText(text)
  return flat ? clip(flat, DETAILS_CHARS) : null
}

// `{step_id: line}` from the args' `details` ({steps: {...}}), or {}.
function readDetails(value) {
  const out = {}
  const steps = value && typeof value === 'object' && value.steps && typeof value.steps === 'object' ? value.steps : {}
  for (const id of Object.keys(steps)) {
    const line = detailsText(steps[id])
    if (line) out[id] = line
  }
  return out
}

function fallbackDetails(workspace, specStep) {
  const profile = specStep ? flatText(specStep.runner_profile) : null
  const raw = specStep ? specStep.inputs || specStep.input : null
  let browser = NOT_REPORTED
  if (profile === BROWSER_PROFILE) browser = flatText(raw && typeof raw === 'object' ? raw.browser_tool : null) || 'auto'
  else if (profile) browser = 'not used'
  return [
    'Workspace: ' + (flatText(workspace) || NOT_REPORTED),
    'Started by: ' + NOT_REPORTED,
    'Profiles: ' + (profile || NOT_REPORTED),
    'LLM: ' + NOT_REPORTED,
    'Browser tool: ' + browser,
    'Session: ' + NOT_REPORTED,
  ].join(' · ')
}

// A step row's line: the bridge's for this step, else what this script knows.
function rowDetails(wf, step) {
  return (wf.details && wf.details[step.step_id]) || fallbackDetails(wf.workspace, wf.specSteps && wf.specSteps[step.step_id])
}

// Closest to finish first (BOARD-ORDER, owner 2026-10-16): steps done / total
// descending, then the current step's age descending (the step that has run
// longest first), then the id, so the order is total. swarm_mcp.board.order
// is the same rule; it reads each entry's `progress` ({done, total,
// age_seconds}) as swarm_workflows serves it. Entries handed WITHOUT a
// progress on every one -- an attach_tasks list, or a bridge older than the
// rule -- keep the order they were handed, which is the bridge's own.
function boardOrder(entries) {
  const hasProgress = (entry) => !!(entry && entry.progress && typeof entry.progress === 'object')
  if (entries.length === 0 || !entries.every(hasProgress)) return entries.slice()
  const key = (entry) => {
    const total = Number(entry.progress.total) || 0
    const fraction = total ? (Number(entry.progress.done) || 0) / total : 0
    const age = typeof entry.progress.age_seconds === 'number' && isFinite(entry.progress.age_seconds) ? entry.progress.age_seconds : -1
    return { fraction: fraction, age: age, id: String(entry.workflow_id || entry.task_id || '') }
  }
  return entries.map((entry) => ({ entry: entry, key: key(entry) })).sort((a, b) => {
    if (a.key.fraction !== b.key.fraction) return b.key.fraction - a.key.fraction
    if (a.key.age !== b.key.age) return b.key.age - a.key.age
    return a.key.id < b.key.id ? -1 : a.key.id > b.key.id ? 1 : 0
  }).map((item) => item.entry)
}

// Entries closest to finish first (boardOrder), grouped by their `workspace`:
// one block per workspace, the blocks in the order of each block's
// closest-to-finish entry, and that order inside each block.
// swarm_mcp.sc.group_by_workspace is the same.
function groupByWorkspace(entries) {
  const order = []
  const groups = {}
  for (const entry of boardOrder(entries)) {
    const key = '#' + (flatText(entry && entry.workspace) || '')
    if (!groups[key]) {
      groups[key] = []
      order.push(key)
    }
    groups[key].push(entry)
  }
  const out = []
  for (const key of order) for (const entry of groups[key]) out.push(entry)
  return out
}

// The link a relay copied from the bridge, trimmed, or null. Nothing here
// builds or repairs one.
function consoleLink(link) {
  return typeof link === 'string' && link.trim() ? link.trim() : null
}

// ' · console: <link>' for a link a relay copied from the bridge, else ''.
function consoleSuffix(link) {
  const served = consoleLink(link)
  return served ? ' · console: ' + served : ''
}

// `result` with `console` set to the served link, or without the key.
function withConsole(result, link) {
  const served = consoleLink(link)
  if (served) result.console = served
  return result
}

function failureText(error) {
  return String((error && error.message) || error)
}

// One narrator line per finished step: 'scan-03 SUCCEEDED · 4m12s · $0.21 · PR #231',
// ending with the step's console link: the row's own answer's, else the one
// the submit reply carried for the step -- a row that failed still says where
// its task can be watched.
function narrate(stepId, result, submittedLink) {
  const link = consoleSuffix((result && result.console) || submittedLink)
  if (!result || result.row_error) {
    const why = result && result.row_error ? ': ' + clip(result.row_error, 100) : ''
    return stepId + ' · its row stopped before the task finished' + why + ' -- the SwarmCloud task is unaffected' + link
  }
  if (result.state_from) {
    return stepId + ' ' + result.state + ' (from the ' + result.state_from + '; its row could not answer: ' + clip(result.row_note, 100) + ')' + link
  }
  const parts = [stepId + ' ' + (result.state || 'UNKNOWN'), duration(result.duration_s), money(result.cost_usd)]
  const pr = pullRequest(result.pr_url)
  if (pr) parts.push(pr)
  // What it produced, by name: a row that ends without saying so sends the
  // reader to the console to find out. Said for a success that made nothing,
  // too, rather than left to be read off a missing clause.
  const made = Array.isArray(result.artifacts) ? result.artifacts.filter((name) => typeof name === 'string' && name) : []
  if (made.length > 0) parts.push('produced ' + clip(made.join(', '), 120))
  else if (result.state === 'SUCCEEDED') parts.push('produced no artifacts')
  if (result.state !== 'SUCCEEDED' && result.last_error) parts.push(clip(result.last_error, 100))
  return parts.join(' · ') + link
}

function sameSet(left, right) {
  const a = (left || []).slice().sort()
  const b = (right || []).slice().sort()
  return a.length === b.length && a.every((item, index) => item === b[index])
}

// THE STEPS SWARMCLOUD APPENDS FOR A MERGE (2026-10-11). swarm-api appends a
// `merge` step -- and, for "on_merge_verdict", a `re-review` before it -- to
// the spec it was sent (swarm_api.service._with_merge_step), so the reply can
// name steps the spec does not. Every rule below restates that function and
// swarm_api.validation's merge_sources, merge_step_for and rereview_step_for,
// which a script cannot import; a unit test holds each shape to the server's.
// The choice is the spec's `merge`, else the repository's registered
// merge_policy ("off" | "on_merge_verdict"), else the platform's
// merge_by_default ("on"). The last two are not visible from here, so a spec
// that says nothing could get any one of the shapes they produce. Since
// SPEED-D5 these shapes gate nothing -- serverAdded() decides which extra steps
// are the server's -- they only order a recognised shape's rows and name its
// reason in the log.
const MERGE_STEP_ID = 'merge'
const REREVIEW_STEP_ID = 're-review'
const MERGE_ON_VERDICT = 'on_merge_verdict'

// `base`, or `base-2`, `base-3`... -- the first id no step has.
function freeStepId(base, taken) {
  let id = base
  for (let n = 2; taken.indexOf(id) >= 0; n++) id = base + '-' + n
  return id
}

// A step's verdict gate: a spec's `when.step`, or a file outline's `when_step`.
function whenStep(step) {
  if (typeof step.when_step === 'string' && step.when_step) return step.when_step
  return step.when && typeof step.when.step === 'string' && step.when.step ? step.when.step : null
}

function inputFrom(step) {
  const staged = step.input_from
  return staged && typeof staged === 'object' && !Array.isArray(staged) ? staged : {}
}

// merge_sources: the step that opens the one pull request, the review its
// gate reads and the file it stages from it; null when there is not ONE.
function mergeSources(steps, strategy) {
  if (strategy === 'direct-pr') return steps.length === 1 ? { pr: steps[0].step_id, review: null, file: null } : null
  if (strategy !== 'integrate' || steps.length < 2) return null
  const dependedOn = {}
  for (const step of steps) for (const dep of step.depends_on || []) dependedOn[dep] = true
  const sinks = steps.filter((step) => !dependedOn[step.step_id])
  if (sinks.length !== 1) return null
  const review = whenStep(sinks[0])
  return { pr: sinks[0].step_id, review: review, file: review ? inputFrom(sinks[0])[review] || null : null }
}

// What SwarmCloud may append to `spec`: a list of the step lists it could
// append, each step {step_id, depends_on, why}. `[[]]` when it appends nothing.
function appendable(spec) {
  const strategy = typeof spec.strategy === 'string' && spec.strategy ? spec.strategy : 'collect'
  const choice = typeof spec.merge === 'string' ? spec.merge : null
  const steps = spec.steps
  // single-pr ends in its own merge; a spec that states a merge step keeps it
  // (or is refused, which names no workflow).
  if (strategy === 'single-pr' || choice === 'off' || steps.some((step) => step.runner_profile === MERGE_STEP_ID)) return [[]]
  const ids = steps.map((step) => step.step_id)
  const sources = mergeSources(steps, strategy)
  let on = null
  if (sources) {
    on = [{ step_id: freeStepId(MERGE_STEP_ID, ids), depends_on: [sources.pr].concat(sources.review ? [sources.review] : []) }]
  }
  let verdict = null
  const review = sources && sources.review ? steps.filter((step) => step.step_id === sources.review)[0] : null
  if (strategy === 'integrate' && review && sources.file) {
    const staged = Object.keys(inputFrom(review)).concat([sources.review])
    const rereview = freeStepId(REREVIEW_STEP_ID, ids)
    verdict = [
      { step_id: rereview, depends_on: [sources.pr].concat(staged).filter((id, at, all) => all.indexOf(id) === at) },
      { step_id: freeStepId(MERGE_STEP_ID, ids.concat([rereview])), depends_on: [sources.pr, rereview] },
    ]
  }
  const why = (shape, said) => shape.map((step) => Object.assign({ why: said }, step))
  if (choice === MERGE_ON_VERDICT) return [why(verdict || [], 'the spec\'s merge ' + MERGE_ON_VERDICT)]
  if (choice === 'on') return [why(on || [], 'the spec\'s merge on')]
  // Said nothing: the repository's merge_policy, else the platform default.
  const shapes = [[]]
  if (verdict) shapes.push(why(verdict, 'the repository\'s merge_policy ' + MERGE_ON_VERDICT))
  if (on) shapes.push(why(on, 'the platform\'s merge_by_default'))
  return shapes
}

// The steps the reply names beyond the spec, when they are exactly one shape
// SwarmCloud appends for this spec's merge choice; else null.
function appendedShape(spec, submitted) {
  const given = {}
  for (const step of spec.steps) given[step.step_id] = true
  const extra = (submitted.steps || []).map((step) => step && step.step_id).filter((id) => !given[id])
  return appendable(spec).filter((shape) => sameSet(shape.map((step) => step.step_id), extra))[0] || null
}

// THE STEPS SWARMCLOUD ADDED (SPEED-D5, owner decision 2026-10-14). Measured
// on plugin 0.5.27: a 3-step spec came back with 5 -- swarm-api had appended a
// re-review and a merge -- and the run ended SUBMITTED_UNVERIFIED because the
// reply named steps `appendable` did not predict. Predicting the server's
// merge rules from here is a copy that drifts, so the prediction no longer
// gates anything: it only names the reason in the log. A step the reply names
// beyond the spec is the server's when
//   * the bridge marked it `added: true` -- it compared the reply with the
//     spec it submitted itself (swarm_mcp.server._workflow_created), or
//   * it carries no mark (a bridge before 0.5.32) and every step it depends
//     on is a spec step or another of the server's steps, at least one.
// `added: false` on a step the spec lacks, or an unmarked step tied to no
// spec step, is refused: that is a relay inventing or renaming a step. A spec
// step missing from the reply is refused in mismatches() whatever is added.
// Returns {added: [reply steps, in reply order], refused: [step ids]}.
function serverAdded(spec, submitted) {
  const given = {}
  for (const step of spec.steps) given[step.step_id] = true
  const extras = (submitted.steps || []).filter((step) => step && !given[step.step_id])
  const ours = {}
  for (const step of extras) if (step.added === true) ours[step.step_id] = true
  // Unmarked steps join one at a time, so a chain of them is accepted only
  // when it starts on the spec's own steps.
  for (let grew = true; grew;) {
    grew = false
    for (const step of extras) {
      if (ours[step.step_id] || step.added === true || step.added === false) continue
      const deps = step.depends_on || []
      if (deps.length > 0 && deps.every((dep) => given[dep] || ours[dep])) {
        ours[step.step_id] = true
        grew = true
      }
    }
  }
  return {
    added: extras.filter((step) => ours[step.step_id]),
    refused: extras.filter((step) => !ours[step.step_id]).map((step) => step.step_id),
  }
}

// Every way the Submit reply can differ from the spec this script was given.
// Empty means the relay carried both directions faithfully.
function mismatches(spec, submitted, digest) {
  const problems = []
  if (submitted.spec_digest !== digest) {
    problems.push(
      submitted.spec_digest
        ? 'SwarmCloud received a spec with digest ' + submitted.spec_digest + ', and the spec given has ' + digest + ': the relay changed it on the way'
        : 'the reply carries no spec digest, so what SwarmCloud received cannot be shown to be the spec given (' + digest + ')',
    )
  }
  if (submitted.error) problems.push('the reply names a workflow and also an error: ' + clip(submitted.error, 160))
  const given = {}
  for (const step of spec.steps) given[step.step_id] = step
  // The steps SwarmCloud added (serverAdded); any other step the spec does
  // not have is refused.
  const refused = {}
  for (const id of serverAdded(spec, submitted).refused) refused[id] = true
  const seen = {}
  const owner = {}
  for (const step of submitted.steps || []) {
    const id = step && step.step_id
    if (seen[id]) {
      problems.push('step ' + id + ' appears twice in the reply')
      continue
    }
    seen[id] = true
    if (refused[id]) {
      problems.push('the reply names a step the spec does not have: ' + id)
      continue
    }
    // A spec step's dependencies are the spec's; an added step's are the
    // server's, and the rows follow them as served.
    if (given[id] && !sameSet(step.depends_on, given[id].depends_on)) {
      problems.push('step ' + id + ' depends on [' + (step.depends_on || []).join(', ') + '] in the reply and on [' + (given[id].depends_on || []).join(', ') + '] in the spec')
    }
    if (typeof step.task_id !== 'string' || !step.task_id) {
      problems.push('the reply names no task for step ' + id)
    } else if (owner[step.task_id]) {
      problems.push('task ' + step.task_id + ' is named for both ' + owner[step.task_id] + ' and ' + id)
    } else {
      owner[step.task_id] = id
    }
  }
  for (const step of spec.steps) {
    if (!seen[step.step_id]) problems.push('step ' + step.step_id + ' is in the spec and missing from the reply')
  }
  // The spec's own merge choice ("on", "on_merge_verdict") makes SwarmCloud
  // append steps or refuse the spec, so a reply naming NO step beyond the spec
  // lost the server's steps on the way. Their exact shape is the server's to
  // decide: any added step satisfies this, predicted or not.
  const shapes = appendable(spec)
  const added = serverAdded(spec, submitted).added
  if (added.length === 0 && shapes.length === 1 && shapes[0].length > 0) {
    problems.push('the reply names no step beyond the spec, and SwarmCloud appends ' + shapes[0].map((step) => step.step_id).join(', ') + ' for ' + shapes[0][0].why)
  }
  return problems
}

// By reference when there is one -- the bridge reads the file, or submits the
// spec it already holds -- so the relay carries a path, not the spec. Inline
// only for a spec with no file behind it.
//
// THE SPEC IS THE REQUEST. A launch with a spec -- inline, embedded in the
// launched script, or by spec_path -- was asked for when it was launched, so
// the submit row submits it, whatever the person typed last. Measured on epic
// #1334 (wf_c7292607-6ef, 2026-10-16): the row was relayed the owner's last
// message, "status", answered "The user's request takes precedence, so no
// workflow was submitted", and the same launch submitted once the owner had
// typed "resume". So the prompt's last line says it, and sc:workflow's SUBMIT
// section says the same.
const SUBMIT_REQUEST = 'request: this spec is the whole request -- submit it. Whatever the person last typed in this session ("status", "resume", a question) is not this job\'s request: never weigh it, and never refuse, hold or question the spec because of it.'

function submitPrompt(digest, source) {
  const head = ['SUBMIT', 'spec_digest: ' + digest]
  if (source.specPath) return head.concat(['spec_path: ' + source.specPath, SUBMIT_REQUEST]).join('\n')
  if (source.specRef) return head.concat(['spec_ref: ' + source.specRef, SUBMIT_REQUEST]).join('\n')
  return head.concat(['BEGIN SPEC', JSON.stringify(source.spec, null, 2), 'END SPEC', SUBMIT_REQUEST]).join('\n')
}

// THE ONE PLACE A RELAYED STEP ID ENTERS A ROW. A relay retypes every step
// id it hands back, and one kept the quotes around it (`"implement"`, epic
// #1334): that row's follow was refused as another step's and ended UNKNOWN.
// A step id never holds a quote, so the quotes around one are stripped --
// from step_id and from every depends_on -- before anything keys on it, in
// the submit reply and the attach reply alike.
const QUOTES = '"\'`\\'

function unquotedId(id) {
  if (typeof id !== 'string') return id
  let text = id.trim()
  while (text.length > 0 && QUOTES.indexOf(text[0]) >= 0) text = text.slice(1)
  while (text.length > 0 && QUOTES.indexOf(text[text.length - 1]) >= 0) text = text.slice(0, -1)
  return text.trim()
}

function relayedSteps(steps) {
  if (!Array.isArray(steps)) return steps
  return steps.map((step) => {
    if (!step || typeof step !== 'object') return step
    const out = Object.assign({}, step, { step_id: unquotedId(step.step_id) })
    if (Array.isArray(step.depends_on)) out.depends_on = step.depends_on.map(unquotedId)
    return out
  })
}

// The step a row's task IS. A platform fix round is served as a step of its
// own, `ci-fix:<n>` or `conflict-fix:<n>` (swarm_api.mergewake
// `_append_round_step`: a caller's step id never holds ':'), but its task is
// the round workflow's step `ci-fix` / `conflict-fix`, and swarm_follow
// refuses a task that is not the step it is told (epic #1334: those rows
// ended UNKNOWN, "this row was handed another step's task id"). So the row
// follows the step its task carries; its label keeps the round number.
function followedStepId(id) {
  if (typeof id !== 'string') return id
  const at = id.lastIndexOf(':')
  const round = id.slice(at + 1)
  const digits = round.length > 0 && round.split('').every((c) => c >= '0' && c <= '9')
  return at > 0 && digits ? id.slice(0, at) : id
}

// `parent_task_ids` are the task ids of the parents that have not finished:
// the row's first call holds on them, so a step that has not started makes one
// call while they run. `console` is the step's link as the submit or attach
// reply carried it, or `none`: the row ends a progress line with it when the
// follow reply that line came from carries no link of its own.
function stepPrompt(workflowId, step, parentTasks, details) {
  const parents = (step.depends_on || []).join(', ') || 'none'
  return [
    'task_id: ' + step.task_id,
    'step_id: ' + followedStepId(step.step_id),
    'workflow_id: ' + workflowId,
    'depends_on: ' + parents,
    'parent_task_ids: ' + ((parentTasks || []).join(', ') || 'none'),
    'console: ' + (consoleLink(step.console) || 'none'),
    'details: ' + details,
  ].join('\n')
}

// The probe failed: this session's bridge refused the follow every row makes.
// The workflow is in SwarmCloud and runs regardless; no row starts, and none
// tries another format.
function followRefused(workflowId, version, error, workflowConsole) {
  const bridge = version
    ? 'swarm-mcp ' + version
    : 'which reported no version, so it predates the version check this plugin makes'
  const message = 'SwarmCloud has workflow ' + workflowId + ' and it runs regardless, but this session\'s SwarmCloud bridge (' + bridge + ') refused the progress follow every row makes: ' + error + '. The likely cause is a SWARM_MCP_FROM override pinning an older bridge than this plugin ships: unset it, or point it at this plugin\'s release, and restart Claude Code. Then re-join the workflow with /sc attach ' + workflowId + ' (or /sc:swarmcloud {attach: "' + workflowId + '"}). No row was started, and no row falls back to another format.'
  log(workflowId + ' · follow refused by the bridge · ' + clip(error, 160) + consoleSuffix(workflowConsole))
  return withConsole({ workflow_id: workflowId, state: 'FOLLOW_REFUSED', error: message, steps: [] }, workflowConsole)
}

function notSubmitted(error) {
  log('not submitted · ' + clip(error, 200))
  return { workflow_id: null, state: 'NOT_SUBMITTED', error: error, steps: [] }
}


// A row's answer that says the bridge itself was missing or did not answer --
// the tool absent when the row started (step.md answers that at once), or its
// calls failing as not connected or not responding -- rather than anything
// about the task. Anchored on the MCP server's own wording, so a task whose
// abandoned_because merely contains "not connected" is not retried.
const BRIDGE_DOWN = ['not connected', 'not responding']
const BRIDGE_TRIES = 5
const BRIDGE_RETRY_S = 30

function bridgeDown(result) {
  if (!result || result.state !== 'UNKNOWN' || typeof result.last_error !== 'string') return false
  // The clause that names the MCP server, up to its first ';' or '.'.
  const text = result.last_error.toLowerCase()
  const at = text.indexOf('mcp server')
  if (at < 0) return false
  let clause = text.slice(at)
  for (const stop of [';', '.']) {
    const end = clause.indexOf(stop)
    if (end >= 0) clause = clause.slice(0, end)
  }
  return BRIDGE_DOWN.some((words) => clause.indexOf(words) >= 0)
}

// The wait between two tries. A workflow script has no timer: the runtime
// gives it plain JavaScript built-ins and no host API (the workflow-authoring
// reference: "No filesystem or Node.js API access"; even the clock throws),
// so a host timer here would be absent and the five tries would come back to
// back in seconds. The wait is therefore an agent: one sc:wait row, labelled
// as setup, that runs `sleep 30` and says whether it did. A wait that could
// not be made is logged, never passed off as a wait.
const WAITED = {
  type: 'object',
  properties: {
    waited: { type: 'boolean' },
    error: { type: ['string', 'null'] },
  },
  required: ['waited', 'error'],
}

async function waitForBridge(wf, step, next, phaseName) {
  let answer = null
  try {
    answer = await agent('WAIT\nseconds: ' + BRIDGE_RETRY_S, {
      label: setupLabel(wf.name, [], 'waiting for the bridge · ' + step.step_id + ' · try ' + next + ' of ' + BRIDGE_TRIES, null),
      phase: phaseName,
      agentType: 'sc:wait',
      schema: WAITED,
    })
  } catch (error) {
    answer = { waited: false, error: failureText(error) }
  }
  if (!answer || answer.waited !== true) {
    const why = answer && answer.error ? answer.error : 'the wait row stopped before it answered'
    log(step.step_id + ' · the ' + BRIDGE_RETRY_S + 's wait before try ' + next + ' of ' + BRIDGE_TRIES + ' could not be made (' + clip(why, 120) + ') · trying again at once')
  }
}

// One step's row, started again under the same label while the bridge is
// down, BRIDGE_TRIES times at most, a BRIDGE_RETRY_S wait row between two
// tries (about two minutes of waits plus the rows' own start, ~5 min in all).
// The last answer's last_error then ends with the command that re-joins the
// workflow.
async function followRow(wf, step, prompt, opts) {
  let result = null
  for (let tried = 1; tried <= BRIDGE_TRIES; tried++) {
    try {
      result = stepAnswer(await agent(prompt, opts))
    } catch (error) {
      return await rowFromStatus(wf, step, failureText(error), opts.phase)
    }
    if (result && !(typeof result.state === 'string' && result.state)) {
      return await rowFromStatus(wf, step, 'the row answered with no state', opts.phase)
    }
    if (!bridgeDown(result)) return result
    if (tried < BRIDGE_TRIES) {
      log(step.step_id + ' · its row could not reach the SwarmCloud bridge (' + clip(result.last_error, 100) + ') · trying again in ' + BRIDGE_RETRY_S + 's, ' + (tried + 1) + ' of ' + BRIDGE_TRIES)
      await waitForBridge(wf, step, tried + 1, opts.phase)
    }
  }
  return Object.assign({}, result, { last_error: result.last_error + ' · resume: /sc attach ' + wf.id })
}

// A step row that threw -- "StructuredOutput retry cap (5) exceeded", "subagent
// completed without calling StructuredOutput" -- or answered with no state
// (SPEED-D5, measured on plugin 0.5.27). The task is SwarmCloud's and ran on
// regardless, so one swarm_workflow_status read, made at once for this row,
// says how it ended: a step SwarmCloud names as finished becomes a row with
// that state, `state_from` saying where it came from and `row_note` what the
// row failed with. A step still running, or not named, or a read that failed
// too, stays the row_error it was, which finish() re-attaches while the
// workflow runs on: nothing here guesses a state.
async function rowFromStatus(wf, step, why, phaseName) {
  const read = await readStatus(wf.id, setupLabel(wf.name, [step.step_id], 'status', null), phaseName)
  const state = statusSteps(read.final)[step.step_id]
  if (!state || TERMINAL.indexOf(state) < 0) return { row_error: why }
  return {
    state: state,
    state_from: 'workflow status',
    row_note: why,
    answer_excerpt: null,
    cost_usd: null,
    duration_s: null,
    pr_url: null,
    artifacts: [],
    last_error: null,
  }
}

// The prompt of one sc:task row: its task, the link the dispatch or the
// list served for it, or `none`, and its details line (rowDetails).
function taskPrompt(task) {
  return [
    'task_id: ' + task.task_id,
    'console: ' + (consoleLink(task.console) || 'none'),
    'details: ' + (task.details || fallbackDetails(task.workspace, null)),
  ].join('\n')
}

// One single task's row, started again under the same label while the bridge
// is down, exactly as followRow does for a step; the last answer's last_error
// then ends with the command that gives it a row again.
async function followTaskRow(task, name) {
  const opts = {
    label: rowLabel(name, 'task', task.console),
    phase: 'Tasks',
    agentType: 'sc:task',
    schema: STEP_RESULT,
  }
  let result = null
  for (let tried = 1; tried <= BRIDGE_TRIES; tried++) {
    try {
      result = stepAnswer(await agent(taskPrompt(task), opts))
    } catch (error) {
      return { row_error: failureText(error) }
    }
    if (!bridgeDown(result)) return result
    if (tried < BRIDGE_TRIES) {
      log(task.task_id + ' · its row could not reach the SwarmCloud bridge (' + clip(result.last_error, 100) + ') · trying again in ' + BRIDGE_RETRY_S + 's, ' + (tried + 1) + ' of ' + BRIDGE_TRIES)
      await waitForBridge({ name: name, workspace: task.workspace }, { step_id: task.task_id }, tried + 1, 'Tasks')
    }
  }
  return Object.assign({}, result, { last_error: result.last_error + ' · resume: /sc attach --all' })
}

// ATTACH TASKS: one sc:task row per single task, up to `cap` (the args'
// `max_rows`, else MAX_ATTACHED_TASKS), all at once, grouped by workspace;
// each ends when its task does. Nothing is submitted.
async function attachTasks(given, cap) {
  const limit = liveRowCap(cap, MAX_ATTACHED_TASKS)
  const tasks = groupByWorkspace(given)
  const follow = tasks.slice(0, limit)
  const rest = tasks.slice(limit)
  log(follow.length + ' single task(s) · one row each' + (rest.length ? ' · ' + rest.length + ' listed, not followed (at most ' + limit + ')' : ''))
  for (const task of rest) log(task.task_id + ' · not followed · swarm_follow it, or /sc status ' + task.task_id + workspaceNote(task.workspace) + consoleSuffix(task.console))
  const rows = await pipeline(
    follow,
    async (task) => await followTaskRow(task, titleName([task.title, task.task_id])),
    (result, task) => {
      log(narrate(titleName([task.title, task.task_id]), result, task.console) + workspaceNote(task.workspace))
      const row = { task_id: task.task_id, workspace: task.workspace || null, title: task.title || null }
      if (!result || result.row_error) {
        row.state = null
        row.row_error = result && result.row_error ? result.row_error : 'the row stopped before its task finished'
        return withConsole(row, task.console)
      }
      return withConsole(Object.assign(row, result), result.console || task.console)
    },
  )
  return {
    attach: 'tasks',
    state: 'FOLLOWED',
    error: null,
    tasks: rows,
    not_followed: rest.map((task) => withConsole({ task_id: task.task_id, workspace: task.workspace || null, title: task.title || null }, task.console)),
  }
}

// A workflow as its rows show it: `id`; `name`, the title's name; `count`,
// its steps; `all`, every step (step_id, task_id, depends_on, console) in the
// workflow's own order; `stages`, consoleStages(all); `workspace`, the one the
// API's document names, or null; `title`; `details`, the bridge's details
// line per step id ({} when it sent none), and `specSteps`, the spec's own
// step per id when this run submitted it -- both for rowDetails.
function workflowView(id, name, all, workspace, details, specSteps) {
  const ws = flatText(workspace)
  return {
    id: id, name: name, count: all.length, all: all, stages: consoleStages(all), workspace: ws,
    title: workflowTitle(name, all.length), details: details || {}, specSteps: specSteps || {},
  }
}

// One sc:step row per step in `steps`, in the console's order, each handed
// the task ids of its unfinished parents. `steps` carry step_id, task_id,
// depends_on and console.
async function followSteps(wf, steps, parentTasksOf) {
  const wanted = {}
  for (const step of steps) wanted[step.step_id] = step
  const ordered = wf.stages.order.filter((id) => wanted[id]).map((id) => wanted[id])
  return await pipeline(
    ordered,
    async (step) => {
      if (!step.task_id) return { row_error: 'SwarmCloud named no task for this step' }
      const stage = wf.stages.stage[step.step_id] || 1
      return await followRow(wf, step, stepPrompt(wf.id, step, parentTasksOf(step), rowDetails(wf, step)), {
        label: rowLabel(wf.name, 'stage ' + stage + ' · ' + step.step_id, step.console),
        phase: stagePhase(stage),
        agentType: 'sc:step',
        schema: STEP_RESULT,
      })
    },
    (result, step) => {
      log(narrate(step.step_id, result, step.console))
      const row = { step_id: step.step_id, task_id: step.task_id, depends_on: step.depends_on || [] }
      if (!result || result.row_error) {
        row.state = null
        row.row_error = result && result.row_error ? result.row_error : 'the row stopped before its task finished'
        return withConsole(row, step.console)
      }
      // A row that answered with no link keeps the one its submission carried.
      return withConsole(Object.assign(row, result), result.console || step.console)
    },
  )
}

// The status read: ONE swarm_workflow_status call by one agent, the cheapest
// read there is of the state SwarmCloud derived.
async function readStatus(workflowId, label, phaseName) {
  try {
    const final = await agent('STATUS\nworkflow_id: ' + workflowId, {
      label: label,
      phase: phaseName || 'Result',
      agentType: 'sc:workflow',
      schema: WORKFLOW_STATE,
    })
    return { final: final, failure: null }
  } catch (error) {
    return { final: null, failure: failureText(error) }
  }
}

function statusSteps(final) {
  const statusOf = {}
  if (final && Array.isArray(final.steps)) {
    for (const entry of final.steps) {
      if (entry && typeof entry.step_id === 'string' && typeof entry.state === 'string' && entry.state) {
        statusOf[entry.step_id] = entry.state
      }
    }
  }
  return statusOf
}

// A row whose relay failed is not a step whose task failed (#285). The sc:step
// relay retypes its answer into StructuredOutput JSON, and one retyping a raw
// answer excerpt (backticks, quotes, ******** masks, paths, backslashes) ran out
// of its five attempts on a step whose task had SUCCEEDED -- so the row said
// state null for a finished task. The workflow status read names every step's
// state as SwarmCloud derived it, so such a row takes its state from there,
// keeps its own row_error beside it, and says where the state came from
// (state_from, on these rows only: a row that answered keeps the STEP_RESULT
// shape). It stays null only when that read failed too, or did not name the
// step, or named it with no state -- nothing here guesses one.
function fillFromStatus(rows, final) {
  const statusOf = statusSteps(final)
  for (const row of rows) {
    if (!row || !row.row_error || row.state !== null) continue
    if (!Object.prototype.hasOwnProperty.call(statusOf, row.step_id)) continue
    row.state = statusOf[row.step_id]
    row.state_from = 'workflow status'
    log(row.step_id + ' ' + row.state + ' (from the workflow status; its row failed: ' + clip(row.row_error, 100) + ')')
  }
}

// The steps to re-attach: while SwarmCloud says the workflow still runs, every
// step whose row ended UNKNOWN (or stopped) and whose task SwarmCloud names as
// unfinished. A workflow state of UNKNOWN is a partial read, not a running
// workflow, and starts nothing.
function reattachable(wf, rows, final) {
  const state = final ? final.state : null
  if (typeof state !== 'string' || !state || state === 'UNKNOWN' || TERMINAL.indexOf(state) >= 0) return []
  const statusOf = statusSteps(final)
  const lost = {}
  for (const row of rows) {
    if (!row || !(row.state === 'UNKNOWN' || row.row_error)) continue
    const now = statusOf[row.step_id]
    if (now && TERMINAL.indexOf(now) < 0) lost[row.step_id] = true
  }
  return wf.all.filter((step) => lost[step.step_id] && step.task_id)
}

// The final status read, the same after a submission and after an attach,
// re-attaching once the steps whose rows were lost while the workflow runs on.
// The rows are returned whatever happens here: a Result row that fails must
// not take every step's result down with it.
async function finish(wf, rows, knownConsole) {
  phase('Result')
  const label = setupLabel(wf.name, [stepsText(wf.count), wf.id], 'result', knownConsole)
  let read = await readStatus(wf.id, label)
  fillFromStatus(rows, read.final)

  let reattached = null
  const again = reattachable(wf, rows, read.final)
  if (again.length > 0) {
    reattached = again.map((step) => step.step_id)
    log(wf.id + ' · ' + reattached.join(', ') + ' ended without a state while SwarmCloud says the workflow is ' + read.final.state + ' · re-attaching ' + (again.length === 1 ? 'it' : 'them') + ' once, with a new row each')
    const statusOf = statusSteps(read.final)
    const taskOf = {}
    for (const step of wf.all) taskOf[step.step_id] = step.task_id
    const followed = await followSteps(wf, again, (step) =>
      (step.depends_on || [])
        .filter((parent) => taskOf[parent] && TERMINAL.indexOf(statusOf[parent]) < 0)
        .map((parent) => taskOf[parent]),
    )
    const byStep = {}
    for (const row of followed) if (row) byStep[row.step_id] = row
    rows = rows.map((row) => (row && byStep[row.step_id]) || row)
    read = await readStatus(wf.id, label)
    fillFromStatus(rows, read.final)
  }

  const final = read.final
  const state = final ? final.state : null
  const note = final
    ? final.state_note
    : read.failure
      ? 'the row reading the workflow state failed: ' + read.failure
      : 'the row reading the workflow state stopped before it answered'
  // The workflow's console link: the result row's copy, else the one the
  // submit or attach reply carried.
  const workflowConsole = (final && final.console) || knownConsole
  const workflowLink = consoleSuffix(workflowConsole)
  log(wf.id + ' ' + (state || 'state not read') + (note ? ' · ' + clip(note, 160) : '') + workspaceNote(wf.workspace) + workflowLink)

  const finished = {
    workflow_id: wf.id,
    workspace: wf.workspace,
    title: wf.title,
    state: state,
    state_note: note,
    steps: rows,
  }
  if (reattached) finished.reattached = reattached
  if (workflowLink) finished.console = workflowConsole.trim()
  return finished
}

// ATTACH one workflow: nothing is submitted. One read, one probe, a row per
// unfinished step. `givenTitle` is the name the launch titled this run with,
// if any; the rows take the names SwarmCloud stored once the read returns.
async function attachOne(workflowId, givenTitle, givenWorkspace, givenDetails) {
  let attached = null
  let attachFailure = null
  try {
    attached = await agent('ATTACH\nworkflow_id: ' + workflowId, {
      label: setupLabel(titleName([givenTitle, workflowId]), [], 'attach', null),
      phase: 'Attach',
      agentType: 'sc:workflow',
      schema: ATTACHED,
    })
  } catch (error) {
    attachFailure = failureText(error)
  }
  if (!attached || attached.error || !Array.isArray(attached.steps)) {
    // The API's error, verbatim: an unknown id and another tenant's read the same.
    const error = attached && attached.error
      ? attached.error
      : attachFailure
        ? 'the row reading workflow ' + workflowId + ' failed: ' + attachFailure
        : 'the row reading workflow ' + workflowId + ' stopped before it answered'
    log('not attached · ' + clip(error, 200) + workspaceNote(givenWorkspace))
    return { workflow_id: workflowId, workspace: givenWorkspace || null, state: 'NOT_ATTACHED', error: error, steps: [] }
  }
  if (attached.follow_error) return followRefused(workflowId, attached.bridge_version, attached.follow_error, attached.console)
  attached.steps = relayedSteps(attached.steps)

  // The workspace the read names wins over the one the launch was given.
  const wf = workflowView(workflowId, titleName([attached.title, attached.label, givenTitle, workflowId]), attached.steps, flatText(attached.workspace) || givenWorkspace, givenDetails)
  const known = {}
  for (const step of attached.steps) known[step.step_id] = step
  const finished = (step) => !!step && TERMINAL.indexOf(step.state) >= 0
  const open = attached.steps.filter((step) => !finished(step))
  const doneRows = {}
  for (const step of attached.steps) {
    if (!finished(step)) continue
    // Said once, here, and given no row: there is nothing left to watch.
    log(step.step_id + ' ' + step.state + ' · finished before attach' + consoleSuffix(step.console))
    doneRows[step.step_id] = {
      step_id: step.step_id, task_id: step.task_id, depends_on: step.depends_on || [],
      state: step.state, finished_before_attach: true,
    }
    if (consoleSuffix(step.console)) doneRows[step.step_id].console = step.console.trim()
  }
  log(workflowId + ' attached · ' + wf.title + ' · ' + open.length + ' unfinished' + workspaceNote(wf.workspace) + consoleSuffix(attached.console))

  const followed = await followSteps(wf, open, (step) =>
    (step.depends_on || [])
      .filter((parent) => known[parent] && !finished(known[parent]) && known[parent].task_id)
      .map((parent) => known[parent].task_id),
  )
  const byStep = {}
  for (const row of followed) if (row) byStep[row.step_id] = row
  const rows = attached.steps.map((step) => doneRows[step.step_id] || byStep[step.step_id]).filter(Boolean)
  return await finish(wf, rows, attached.console)
}

// ATTACH ALL: one list, and the {attach} call that attaches each listed
// workflow up to the cap (`cap`, the args' `max_rows`, else
// MAX_ATTACHED_WORKFLOWS) -- each a run of its own, never rows in this one --
// closest to finish first, grouped by workspace (groupByWorkspace).
async function attachAll(cap) {
  const limit = liveRowCap(cap, MAX_ATTACHED_WORKFLOWS)
  let listed = null
  let listFailure = null
  try {
    listed = await agent('LIST', {
      label: setupLabel('running workflows', [], 'list'),
      phase: 'Attach',
      agentType: 'sc:workflow',
      schema: LISTED,
    })
  } catch (error) {
    listFailure = failureText(error)
  }
  if (!listed || listed.error || !Array.isArray(listed.workflows)) {
    const error = listed && listed.error
      ? listed.error
      : listFailure
        ? 'the row listing the running workflows failed: ' + listFailure
        : 'the row listing the running workflows stopped before it answered'
    log('not attached · ' + clip(error, 200))
    return { attach: 'all', state: 'NOT_ATTACHED', error: error, workflows: [], attach_calls: [], not_followed: [], task_call: null }
  }
  // ONLY THIS SESSION'S (SC-SESSION-SCOPE, owner 2026-10-14): swarm_workflows
  // lists the whole tenant and marks `this_session`; another session's work,
  // and untagged work, is never attached here -- `/sc attach <wf_id>` still
  // attaches one by hand. Anything but `true` is not this session's.
  const ownTasks = (Array.isArray(listed.single_tasks) ? listed.single_tasks : []).filter((entry) => entry && entry.this_session === true)
  const ownWorkflows = listed.workflows.filter((entry) => entry && entry.this_session === true)
  const leftOut = (listed.workflows.length - ownWorkflows.length) + ((Array.isArray(listed.single_tasks) ? listed.single_tasks.length : 0) - ownTasks.length)
  if (leftOut > 0) log(leftOut + ' running workflow(s) or task(s) of the tenant are not this session\'s · not attached · /sc attach <wf_id> attaches one by hand')
  // The single tasks (#830): one {attach_tasks} call for all of them, a run of
  // its own, so their rows never wait on this one.
  const tasks = []
  const seenTask = {}
  for (const entry of ownTasks) {
    const id = entry && flatText(entry.task_id)
    if (!id || seenTask[id]) continue
    seenTask[id] = true
    tasks.push({ task_id: id, workspace: flatText(entry.workspace), title: flatText(entry.label), console: consoleLink(entry.console), state: entry.state || null })
  }
  const groupedTasks = groupByWorkspace(tasks)
  if (listed.single_tasks_error) log('your single tasks could not be listed · ' + clip(listed.single_tasks_error, 200))
  for (const task of groupedTasks) log(task.task_id + (task.title ? ' ' + task.title : '') + ' ' + (task.state || 'state not read') + ' · single task · row: /sc attach --all' + workspaceNote(task.workspace) + consoleSuffix(task.console))
  const taskCall = groupedTasks.length === 0
    ? null
    : {
        attach_tasks: groupedTasks.map((task) => {
          const out = { task_id: task.task_id, title: task.title || task.task_id }
          if (task.workspace) out.workspace = task.workspace
          if (task.console) out.console = task.console
          return out
        }),
        max_rows: limit,
      }
  const seen = {}
  const running = groupByWorkspace(ownWorkflows.filter((entry) => {
    if (!entry || typeof entry.workflow_id !== 'string' || !entry.workflow_id || seen[entry.workflow_id]) return false
    seen[entry.workflow_id] = true
    return true
  }))
  if (typeof listed.count === 'number' && listed.count !== listed.workflows.length) {
    // The relay dropped or repeated an entry: said, not repaired.
    log('the bridge listed ' + listed.count + ' running workflow(s) and the relay passed on ' + listed.workflows.length)
  }
  if (running.length === 0 && !taskCall) {
    log('no SwarmCloud workflow or single task this session started is running · nothing to attach')
    return { attach: 'all', state: 'NOTHING_RUNNING', error: null, workflows: [], attach_calls: [], not_followed: [], task_call: null }
  }
  const follow = running.slice(0, limit)
  const rest = running.slice(limit)
  log(running.length + ' running workflow(s) · ' + follow.length + ' to attach, one run each' + (rest.length ? ' · ' + rest.length + ' listed, not followed (at most ' + limit + ')' : ''))
  const nameOf = (entry) => flatText(entry.title) || flatText(entry.label)
  // One line per workflow, with its link: any of them can be opened before
  // its run has started.
  for (const entry of follow) {
    const name = nameOf(entry) ? ' ' + nameOf(entry) : ''
    log(entry.workflow_id + name + ' ' + (entry.state || 'state not read') + ' · attach: /sc attach ' + entry.workflow_id + workspaceNote(entry.workspace) + consoleSuffix(entry.console))
  }
  for (const entry of rest) {
    const name = nameOf(entry) ? ' ' + nameOf(entry) : ''
    log(entry.workflow_id + name + ' ' + (entry.state || 'state not read') + ' · not followed · /sc attach ' + entry.workflow_id + workspaceNote(entry.workspace) + consoleSuffix(entry.console))
  }
  const described = (entry) => {
    const out = { workflow_id: entry.workflow_id, workspace: flatText(entry.workspace), title: entry.title || null, label: entry.label || null, state: entry.state || null }
    if (consoleSuffix(entry.console)) out.console = entry.console.trim()
    return out
  }
  const calls = follow.map((entry) => {
    const call = { attach: entry.workflow_id }
    if (nameOf(entry)) call.title = nameOf(entry)
    if (flatText(entry.workspace)) call.workspace = flatText(entry.workspace)
    return call
  })
  return {
    attach: 'all',
    state: 'LISTED',
    error: null,
    workflows: follow.map(described),
    attach_calls: calls,
    not_followed: rest.map(described),
    task_call: taskCall,
  }
}

const given = readSpec(args)

if (given.attachTasks) {
  phase('Tasks')
  return await attachTasks(given.attachTasks, given.maxRows)
}

if (given.attachAll) {
  phase('Attach')
  return await attachAll(given.maxRows)
}

if (given.attach) {
  phase('Attach')
  return await attachOne(given.attach, given.title, given.workspace, given.details)
}

phase('Submit')

let spec = given.spec
let digest = null
const source = { spec: given.spec, specPath: given.specPath || null, specRef: null }
if (given.path) {
  // The bridge reads the file and holds it; the row relays its ref, digest and
  // outline -- never the spec -- and nothing is submitted without all three.
  let read = null
  let readFailure = null
  try {
    read = await agent('READ SPEC\npath: ' + given.path, {
      label: setupLabel(given.path, [], 'read spec'),
      phase: 'Submit',
      agentType: 'sc:workflow',
      schema: READ,
    })
  } catch (error) {
    readFailure = failureText(error)
  }
  if (!read || read.error) {
    if (read && read.error) return notSubmitted(read.error)
    const why = readFailure ? 'the row reading ' + given.path + ' failed: ' + readFailure : 'the row reading ' + given.path + ' stopped before it answered'
    return notSubmitted(why + '. Nothing was submitted.')
  }
  const outline = read.outline
  if (typeof read.spec_ref !== 'string' || !read.spec_ref || typeof read.spec_digest !== 'string' || !read.spec_digest || !outline || !Array.isArray(outline.steps) || outline.steps.length === 0) {
    return notSubmitted('the row reading ' + given.path + ' did not hand back the spec_ref, spec_digest and outline the bridge returns, so nothing was submitted. Run /sc:swarmcloud again, or pass the spec object with its spec_path.')
  }
  spec = { steps: outline.steps }
  // What decides the merge steps SwarmCloud appends (`appendable`).
  if (typeof outline.strategy === 'string' && outline.strategy) spec.strategy = outline.strategy
  if (typeof outline.merge === 'string' && outline.merge) spec.merge = outline.merge
  if (typeof outline.label === 'string' && outline.label.trim()) spec.label = outline.label
  if (typeof outline.title === 'string' && outline.title.trim()) spec.title = outline.title
  source.specRef = read.spec_ref
  digest = read.spec_digest
  log('read the spec from ' + (read.path || given.path) + ' · ' + digest)
}

if (!digest) digest = specDigest(spec)
const specLabel = flatText(spec.label)

let submitted = null
let submitFailure = null
try {
  submitted = await agent(submitPrompt(digest, source), {
    label: setupLabel(titleName([spec.title, spec.label]), [stepsText(spec.steps.length)], 'submit'),
    phase: 'Submit',
    agentType: 'sc:workflow',
    schema: SUBMITTED,
  })
} catch (error) {
  submitFailure = failureText(error)
}

if (!submitted || (!submitted.workflow_id && !submitted.error)) {
  const why = submitFailure
    ? 'the submitting row failed: ' + submitFailure
    : submitted
      ? 'the submitting row answered with neither a workflow id nor an error'
      : 'the submitting row stopped before it answered'
  const error = why + '. Whether SwarmCloud created the workflow is UNKNOWN: swarm_workflow may already have submitted it. Look for it in the console\'s workflow list' + (specLabel ? ' (label ' + specLabel + ')' : '') + ' before running /sc:swarmcloud again, which would submit a second copy.'
  log('submission outcome unknown · ' + clip(why, 200))
  return { workflow_id: null, state: 'SUBMISSION_UNKNOWN', error: error, steps: [], arm_watch: ARM_ANYWAY }
}

if (!submitted.workflow_id) {
  // An explicit refusal: swarm_workflow answered with an error, so nothing was sent.
  return notSubmitted(submitted.error)
}

if (Array.isArray(submitted.steps)) submitted.steps = relayedSteps(submitted.steps)
const problems = mismatches(spec, submitted, digest)
if (problems.length > 0) {
  for (const problem of problems) log(submitted.workflow_id + ' · ' + clip(problem, 200))
  return withConsole({
    workflow_id: submitted.workflow_id,
    state: 'SUBMITTED_UNVERIFIED',
    error: 'SwarmCloud accepted workflow ' + submitted.workflow_id + ', but the reply relayed back does not match the spec given, so no row was started: ' + problems.join('; ') + '. The workflow runs regardless: read it with swarm_workflow_status, or cancel it with swarm_workflow_cancel if it is not what was meant.',
    steps: [],
    arm_watch: ARM_ANYWAY,
  }, submitted.console)
}

if (submitted.follow_error) return followRefused(submitted.workflow_id, submitted.bridge_version, submitted.follow_error, submitted.console)

// The steps in the SPEC's order, which is the order SwarmCloud serves them in
// and so the console's; the reply's own order is the relay's.
// The steps SwarmCloud added come last, as the server appends them, and get a
// row each: the console counts them as steps. In the order of the merge shape
// the script recognises when they are exactly that shape, else the reply's.
const replied = {}
for (const step of submitted.steps) replied[step.step_id] = step
const addedSteps = serverAdded(spec, submitted).added
const knownShape = appendedShape(spec, submitted)
const appendedSteps = knownShape || addedSteps
const wf = workflowView(
  submitted.workflow_id,
  titleName([spec.title, spec.label, submitted.workflow_id]),
  spec.steps.concat(appendedSteps).map((step) => replied[step.step_id]),
  submitted.workspace,
  given.details,
  specStepsById(spec),
)

const where = submitted.repository ? ' · clones ' + submitted.repository : ' · clones no repository'
log(submitted.workflow_id + ' submitted · ' + submitted.steps.length + ' step(s)' + where + workspaceNote(wf.workspace) + consoleSuffix(submitted.console))
log(wf.title + ' · ' + submitted.workflow_id)
// Every inference note the bridge made, verbatim: it exists only in this
// reply, and this is the one place a session watching the workflow can see it.
for (const note of submitted.repository_notes || []) log(submitted.workflow_id + ' repository note: ' + note)
if (knownShape && knownShape.length > 0) {
  log(submitted.workflow_id + ' · SwarmCloud appended ' + knownShape.map((step) => step.step_id).join(', ') + ' for ' + knownShape[0].why)
} else if (addedSteps.length > 0) {
  log(submitted.workflow_id + ' · SwarmCloud added ' + addedSteps.map((step) => step.step_id).join(', '))
}

// Just submitted, so no parent has finished: every row is handed all of its
// parents' task ids.
const taskOf = {}
for (const step of submitted.steps) taskOf[step.step_id] = step.task_id
const rows = await followSteps(wf, wf.all, (step) =>
  (step.depends_on || []).map((parent) => taskOf[parent]).filter((id) => typeof id === 'string' && id),
)
return await finish(wf, rows, submitted.console)
