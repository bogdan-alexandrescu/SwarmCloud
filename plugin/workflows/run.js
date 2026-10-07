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
// of this script whose meta.name is `SC · <name> · N steps`
// (swarm_mcp.launch, the swarm_workflow_launch tool). Inside, the same name
// and step count open the first row's label, and the Result row adds the
// workflow id, which only exists once SwarmCloud has answered.
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
//   * the reply must carry that digest back, the spec's step ids exactly, each
//     step's depends_on exactly, and one distinct task per step -- or no row
//     starts;
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
// label and each step's id, depends_on and stage. The row relays those, never
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

const SUBMITTED = {
  type: 'object',
  properties: {
    workflow_id: { type: ['string', 'null'] },
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
          title: { type: ['string', 'null'] },
          label: { type: ['string', 'null'] },
          state: { type: ['string', 'null'] },
          console: CONSOLE,
        },
        required: ['workflow_id', 'title', 'label', 'state', 'console'],
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
          label: { type: ['string', 'null'] },
          state: { type: ['string', 'null'] },
          console: CONSOLE,
        },
        required: ['task_id', 'label', 'state', 'console'],
      },
    },
    single_tasks_error: { type: ['string', 'null'] },
    error: { type: ['string', 'null'] },
  },
  required: ['count', 'workflows', 'single_tasks', 'single_tasks_error', 'error'],
}

// THE CAP on {attach: "all"}: the newest 10 running workflows are followed,
// each in a run of its own (swarm_mcp.launch.MAX_ATTACHED_WORKFLOWS is the
// same cap for the runs the skill launches).
// Every unfinished step of a followed workflow is one live sc:step row -- an
// agent of its own, holding a turn in this session for as long as its task
// runs -- so 10 workflows of three to five steps is already 30-50 rows at once.
// Past that, /workflows stops being readable and watching costs more than the
// work it watches. The rest are listed with the command that follows each.
const MAX_ATTACHED_WORKFLOWS = 10

// THE CAP on single-task rows in one run: each is an agent of its own in this
// session for as long as its task runs, exactly like a step row, so the same
// readability limit applies (swarm_mcp.launch.MAX_ATTACHED_TASKS is the same
// cap for the copies the bridge writes). The rest are listed, not followed.
const MAX_ATTACHED_TASKS = 10

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
            },
            required: ['step_id', 'depends_on', 'stage'],
          },
        },
      },
      required: ['title', 'label', 'steps'],
    },
    error: { type: ['string', 'null'] },
  },
  required: ['path', 'spec_ref', 'spec_digest', 'outline', 'error'],
}

// The states a task does not leave: swarm_common.states.TERMINAL_STATES,
// restated because a script cannot import it; a unit test holds the two equal.
const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']

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
const STEP_FIELDS = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    answer_excerpt: { type: 'string' },
    cost_usd: { type: 'number' },
    duration_s: { type: 'number' },
    pr_url: { type: 'string' },
    artifacts: { type: 'array', items: { type: 'string' } },
    last_error: { type: 'string' },
    console: { type: 'string' },
  },
  required: ['state', 'answer_excerpt', 'pr_url', 'artifacts', 'last_error', 'console'],
}

const STEP_RESULT = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    result: STEP_FIELDS,
  },
  required: ['state', 'result'],
}

// A row's `{state, result}` as the flat step result: an empty text is null, a
// figure left out is null (not recorded, never 0), and a link is kept only
// when there is one. The bridge's own `result.state` wins over the row's copy
// of it. An answer already flat (a row from before 0.5.17) is taken as it is.
function stepAnswer(answer) {
  if (!answer || typeof answer !== 'object' || !answer.result || typeof answer.result !== 'object') return answer
  const given = answer.result
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
    return { attachTasks: readTasks(value.attach_tasks) }
  }
  if (isObject && Object.prototype.hasOwnProperty.call(value, 'attach')) {
    const id = typeof value.attach === 'string' ? value.attach.trim() : ''
    // `all` is never a workflow id: the API's ids begin wf_.
    if (id === 'all' || id === '--all') return { attachAll: true }
    if (!id) {
      throw new Error('/sc:swarmcloud {attach: "<workflow_id>"} needs the id of a workflow already submitted to SwarmCloud; it was given ' + JSON.stringify(value.attach === undefined ? null : value.attach))
    }
    // `title`: the name swarm_workflow_launch titled this run with, for the
    // attach row until the workflow's own stored names are read.
    return { attach: id, title: flatText(value.title) }
  }
  if (isObject && typeof value.spec_path === 'string' && value.spec_path.trim()) {
    return { spec: checkSpec(value.spec), specPath: value.spec_path.trim() }
  }
  return { spec: checkSpec(value) }
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
    tasks.push({ task_id: id, title: isEntry ? flatText(entry.title) : null, console: isEntry ? consoleLink(entry.console) : null })
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
// the same title into the per-run copy's meta.name; the plugin's tests hold
// the two equal. Counted in code points, as Python counts.
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

function workflowTitle(name, count) {
  const tail = ' · ' + stepsText(count)
  const room = TITLE_CHARS - Array.from(TITLE_PREFIX).length - Array.from(tail).length
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

function rowLabel(name, suffix, link) {
  const tail = ' · ' + suffix
  const room = Math.max(8, LABEL_CHARS - LABEL_PREFIX.length - tail.length)
  const served = consoleLink(link)
  return LABEL_PREFIX + clip(name, room) + tail + (served ? ' · ' + served : '')
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
  const seen = {}
  const owner = {}
  for (const step of submitted.steps || []) {
    const id = step && step.step_id
    if (seen[id]) {
      problems.push('step ' + id + ' appears twice in the reply')
      continue
    }
    seen[id] = true
    if (!given[id]) {
      problems.push('the reply names a step the spec does not have: ' + id)
      continue
    }
    if (!sameSet(step.depends_on, given[id].depends_on)) {
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
  return problems
}

// By reference when there is one -- the bridge reads the file, or submits the
// spec it already holds -- so the relay carries a path, not the spec. Inline
// only for a spec with no file behind it.
function submitPrompt(digest, source) {
  const head = ['SUBMIT', 'spec_digest: ' + digest]
  if (source.specPath) return head.concat(['spec_path: ' + source.specPath]).join('\n')
  if (source.specRef) return head.concat(['spec_ref: ' + source.specRef]).join('\n')
  return head.concat(['BEGIN SPEC', JSON.stringify(source.spec, null, 2), 'END SPEC']).join('\n')
}

// `parent_task_ids` are the task ids of the parents that have not finished:
// the row's first call holds on them, so a step that has not started makes one
// call while they run. `console` is the step's link as the submit or attach
// reply carried it, or `none`: the row ends a progress line with it when the
// follow reply that line came from carries no link of its own.
function stepPrompt(workflowId, step, parentTasks) {
  const parents = (step.depends_on || []).join(', ') || 'none'
  return [
    'task_id: ' + step.task_id,
    'step_id: ' + step.step_id,
    'workflow_id: ' + workflowId,
    'depends_on: ' + parents,
    'parent_task_ids: ' + ((parentTasks || []).join(', ') || 'none'),
    'console: ' + (consoleLink(step.console) || 'none'),
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
      label: setupLabel(wf.name, [], 'waiting for the bridge · ' + step.step_id + ' · try ' + next + ' of ' + BRIDGE_TRIES),
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
      return { row_error: failureText(error) }
    }
    if (!bridgeDown(result)) return result
    if (tried < BRIDGE_TRIES) {
      log(step.step_id + ' · its row could not reach the SwarmCloud bridge (' + clip(result.last_error, 100) + ') · trying again in ' + BRIDGE_RETRY_S + 's, ' + (tried + 1) + ' of ' + BRIDGE_TRIES)
      await waitForBridge(wf, step, tried + 1, opts.phase)
    }
  }
  return Object.assign({}, result, { last_error: result.last_error + ' · resume: /sc attach ' + wf.id })
}

// The prompt of one sc:task row: its task and the link the dispatch or the
// list served for it, or `none`.
function taskPrompt(task) {
  return ['task_id: ' + task.task_id, 'console: ' + (consoleLink(task.console) || 'none')].join('\n')
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
      await waitForBridge({ name: name }, { step_id: task.task_id }, tried + 1, 'Tasks')
    }
  }
  return Object.assign({}, result, { last_error: result.last_error + ' · resume: /sc attach --all' })
}

// ATTACH TASKS: one sc:task row per single task, up to MAX_ATTACHED_TASKS, all
// at once; each ends when its task does. Nothing is submitted.
async function attachTasks(tasks) {
  const follow = tasks.slice(0, MAX_ATTACHED_TASKS)
  const rest = tasks.slice(MAX_ATTACHED_TASKS)
  log(follow.length + ' single task(s) · one row each' + (rest.length ? ' · ' + rest.length + ' listed, not followed (at most ' + MAX_ATTACHED_TASKS + ')' : ''))
  for (const task of rest) log(task.task_id + ' · not followed · swarm_follow it, or /sc status ' + task.task_id + consoleSuffix(task.console))
  const rows = await pipeline(
    follow,
    async (task) => await followTaskRow(task, titleName([task.title, task.task_id])),
    (result, task) => {
      log(narrate(titleName([task.title, task.task_id]), result, task.console))
      const row = { task_id: task.task_id, title: task.title || null }
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
    not_followed: rest.map((task) => withConsole({ task_id: task.task_id, title: task.title || null }, task.console)),
  }
}

// A workflow as its rows show it: `id`; `name`, the title's name; `count`,
// its steps; `all`, every step (step_id, task_id, depends_on, console) in the
// workflow's own order; `stages`, consoleStages(all); `title`.
function workflowView(id, name, all) {
  return { id: id, name: name, count: all.length, all: all, stages: consoleStages(all), title: workflowTitle(name, all.length) }
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
      return await followRow(wf, step, stepPrompt(wf.id, step, parentTasksOf(step)), {
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
async function readStatus(workflowId, label) {
  try {
    const final = await agent('STATUS\nworkflow_id: ' + workflowId, {
      label: label,
      phase: 'Result',
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
  log(wf.id + ' ' + (state || 'state not read') + (note ? ' · ' + clip(note, 160) : '') + workflowLink)

  const finished = {
    workflow_id: wf.id,
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
async function attachOne(workflowId, givenTitle) {
  let attached = null
  let attachFailure = null
  try {
    attached = await agent('ATTACH\nworkflow_id: ' + workflowId, {
      label: setupLabel(titleName([givenTitle, workflowId]), [], 'attach'),
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
    log('not attached · ' + clip(error, 200))
    return { workflow_id: workflowId, state: 'NOT_ATTACHED', error: error, steps: [] }
  }
  if (attached.follow_error) return followRefused(workflowId, attached.bridge_version, attached.follow_error, attached.console)

  const wf = workflowView(workflowId, titleName([attached.title, attached.label, givenTitle, workflowId]), attached.steps)
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
  log(workflowId + ' attached · ' + wf.title + ' · ' + open.length + ' unfinished' + consoleSuffix(attached.console))

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
// workflow up to the cap -- each a run of its own, never rows in this one.
async function attachAll() {
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
  // The single tasks (#830): one {attach_tasks} call for all of them, a run of
  // its own, so their rows never wait on this one.
  const tasks = []
  const seenTask = {}
  for (const entry of Array.isArray(listed.single_tasks) ? listed.single_tasks : []) {
    const id = entry && flatText(entry.task_id)
    if (!id || seenTask[id]) continue
    seenTask[id] = true
    tasks.push({ task_id: id, title: flatText(entry.label), console: consoleLink(entry.console), state: entry.state || null })
  }
  if (listed.single_tasks_error) log('your single tasks could not be listed · ' + clip(listed.single_tasks_error, 200))
  for (const task of tasks) log(task.task_id + (task.title ? ' ' + task.title : '') + ' ' + (task.state || 'state not read') + ' · single task · row: /sc attach --all' + consoleSuffix(task.console))
  const taskCall = tasks.length === 0
    ? null
    : { attach_tasks: tasks.map((task) => { const out = { task_id: task.task_id, title: task.title || task.task_id }; if (task.console) out.console = task.console; return out }) }
  const seen = {}
  const running = listed.workflows.filter((entry) => {
    if (!entry || typeof entry.workflow_id !== 'string' || !entry.workflow_id || seen[entry.workflow_id]) return false
    seen[entry.workflow_id] = true
    return true
  })
  if (typeof listed.count === 'number' && listed.count !== running.length) {
    // The relay dropped or repeated an entry: said, not repaired.
    log('the bridge listed ' + listed.count + ' running workflow(s) and the relay passed on ' + running.length)
  }
  if (running.length === 0 && !taskCall) {
    log('no SwarmCloud workflow of this tenant and no single task of yours is running · nothing to attach')
    return { attach: 'all', state: 'NOTHING_RUNNING', error: null, workflows: [], attach_calls: [], not_followed: [], task_call: null }
  }
  const follow = running.slice(0, MAX_ATTACHED_WORKFLOWS)
  const rest = running.slice(MAX_ATTACHED_WORKFLOWS)
  log(running.length + ' running workflow(s) · ' + follow.length + ' to attach, one run each' + (rest.length ? ' · ' + rest.length + ' listed, not followed (at most ' + MAX_ATTACHED_WORKFLOWS + ')' : ''))
  const nameOf = (entry) => flatText(entry.title) || flatText(entry.label)
  // One line per workflow, with its link: any of them can be opened before
  // its run has started.
  for (const entry of follow) {
    const name = nameOf(entry) ? ' ' + nameOf(entry) : ''
    log(entry.workflow_id + name + ' ' + (entry.state || 'state not read') + ' · attach: /sc attach ' + entry.workflow_id + consoleSuffix(entry.console))
  }
  for (const entry of rest) {
    const name = nameOf(entry) ? ' ' + nameOf(entry) : ''
    log(entry.workflow_id + name + ' ' + (entry.state || 'state not read') + ' · not followed · /sc attach ' + entry.workflow_id + consoleSuffix(entry.console))
  }
  const described = (entry) => {
    const out = { workflow_id: entry.workflow_id, title: entry.title || null, label: entry.label || null, state: entry.state || null }
    if (consoleSuffix(entry.console)) out.console = entry.console.trim()
    return out
  }
  const calls = follow.map((entry) => {
    const call = { attach: entry.workflow_id }
    if (nameOf(entry)) call.title = nameOf(entry)
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
  return await attachTasks(given.attachTasks)
}

if (given.attachAll) {
  phase('Attach')
  return await attachAll()
}

if (given.attach) {
  phase('Attach')
  return await attachOne(given.attach, given.title)
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
  return { workflow_id: null, state: 'SUBMISSION_UNKNOWN', error: error, steps: [] }
}

if (!submitted.workflow_id) {
  // An explicit refusal: swarm_workflow answered with an error, so nothing was sent.
  return notSubmitted(submitted.error)
}

const problems = mismatches(spec, submitted, digest)
if (problems.length > 0) {
  for (const problem of problems) log(submitted.workflow_id + ' · ' + clip(problem, 200))
  return withConsole({
    workflow_id: submitted.workflow_id,
    state: 'SUBMITTED_UNVERIFIED',
    error: 'SwarmCloud accepted workflow ' + submitted.workflow_id + ', but the reply relayed back does not match the spec given, so no row was started: ' + problems.join('; ') + '. The workflow runs regardless: read it with swarm_workflow_status, or cancel it with swarm_workflow_cancel if it is not what was meant.',
    steps: [],
  }, submitted.console)
}

if (submitted.follow_error) return followRefused(submitted.workflow_id, submitted.bridge_version, submitted.follow_error, submitted.console)

// The steps in the SPEC's order, which is the order SwarmCloud serves them in
// and so the console's; the reply's own order is the relay's.
const replied = {}
for (const step of submitted.steps) replied[step.step_id] = step
const wf = workflowView(
  submitted.workflow_id,
  titleName([spec.title, spec.label, submitted.workflow_id]),
  spec.steps.map((step) => replied[step.step_id]),
)

const where = submitted.repository ? ' · clones ' + submitted.repository : ' · clones no repository'
log(submitted.workflow_id + ' submitted · ' + submitted.steps.length + ' step(s)' + where + consoleSuffix(submitted.console))
log(wf.title + ' · ' + submitted.workflow_id)
// Every inference note the bridge made, verbatim: it exists only in this
// reply, and this is the one place a session watching the workflow can see it.
for (const note of submitted.repository_notes || []) log(submitted.workflow_id + ' repository note: ' + note)

// Just submitted, so no parent has finished: every row is handed all of its
// parents' task ids.
const taskOf = {}
for (const step of submitted.steps) taskOf[step.step_id] = step.task_id
const rows = await followSteps(wf, wf.all, (step) =>
  (step.depends_on || []).map((parent) => taskOf[parent]).filter((id) => typeof id === 'string' && id),
)
return await finish(wf, rows, submitted.console)
