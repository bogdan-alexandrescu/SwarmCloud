export const meta = {
  name: 'swarmcloud',
  description: 'SwarmCloud: run a SwarmCloud workflow spec with every step executing in SwarmCloud and shown here as a [SwarmCloud] row per step, by stage, with a short progress line when its state changes',
  whenToUse: 'You have a SwarmCloud workflow spec, the JSON that swarm workflow reads, and want each step visible in /workflows while it runs remotely: pass the spec object, the spec object with its file as {spec, spec_path}, its JSON text, or the path of the spec file as args. Or a workflow already running in SwarmCloud lost its rows when this session restarted: pass {attach: "<workflow_id>"} to submit nothing, report its finished steps once and start a row for every unfinished one.',
  phases: [
    { title: 'Submit', detail: 'given a path, one sc:workflow agent reads the file with swarm_workflow_spec; one sc:workflow agent submits the spec with swarm_workflow by reference when it can, checked against the digest, and probes the follow every row makes' },
    { title: 'Attach', detail: 'given {attach: workflow_id}, one sc:workflow agent reads the workflow with swarm_workflow_status and probes the follow every row makes; nothing is submitted' },
    { title: 'Result', detail: 'one sc:workflow agent reads the workflow state SwarmCloud derived' },
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
// labelled `[SwarmCloud] <spec label or workflow id> · stage <n> · <step>`.
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
// Steps are grouped under 'Level N' -- their depth in the DAG -- or under the
// `stage` the spec gives a step. Neither is known before the spec is read, so
// they are not in meta.phases and each gets a progress group of its own.
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
  required: ['workflow_id', 'console', 'state', 'state_note', 'bridge_version', 'follow_error', 'steps', 'error'],
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
      required: ['label', 'steps'],
    },
    error: { type: ['string', 'null'] },
  },
  required: ['path', 'spec_ref', 'spec_digest', 'outline', 'error'],
}

// The states a task does not leave: swarm_common.states.TERMINAL_STATES,
// restated because a script cannot import it; a unit test holds the two equal.
const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']

const STEP_RESULT = {
  type: 'object',
  properties: {
    state: { type: 'string' },
    answer_excerpt: { type: ['string', 'null'] },
    cost_usd: { type: ['number', 'null'] },
    duration_s: { type: ['number', 'null'] },
    pr_url: { type: ['string', 'null'] },
    artifacts: { type: 'array', items: { type: 'string' } },
    last_error: { type: ['string', 'null'] },
    console: CONSOLE,
  },
  required: ['state', 'answer_excerpt', 'cost_usd', 'duration_s', 'pr_url', 'artifacts', 'last_error', 'console'],
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

// What /sc:swarmcloud was given: { attach } for {attach: "<workflow_id>"};
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
  if (isObject && Object.prototype.hasOwnProperty.call(value, 'attach')) {
    const id = typeof value.attach === 'string' ? value.attach.trim() : ''
    if (!id) {
      throw new Error('/sc:swarmcloud {attach: "<workflow_id>"} needs the id of a workflow already submitted to SwarmCloud; it was given ' + JSON.stringify(value.attach === undefined ? null : value.attach))
    }
    return { attach: id }
  }
  if (isObject && typeof value.spec_path === 'string' && value.spec_path.trim()) {
    return { spec: checkSpec(value.spec), specPath: value.spec_path.trim() }
  }
  return { spec: checkSpec(value) }
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
// than its deepest parent. Only for grouping rows; SwarmCloud schedules.
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

// Every row's label: `[SwarmCloud] <name> · <suffix>`, at most LABEL_CHARS.
// The NAME is cut, never the suffix, so a long workflow label still shows
// which stage and which step the row is.
const LABEL_PREFIX = '[SwarmCloud] '
const LABEL_CHARS = 80

function rowLabel(name, suffix) {
  const tail = ' · ' + suffix
  const room = Math.max(8, LABEL_CHARS - LABEL_PREFIX.length - tail.length)
  return LABEL_PREFIX + clip(name, room) + tail
}

// ' · console: <link>' for a link a relay copied from the bridge, else ''.
// The link is printed as given: nothing here builds or repairs one.
function consoleSuffix(link) {
  return typeof link === 'string' && link.trim() ? ' · console: ' + link.trim() : ''
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
// call while they run.
function stepPrompt(workflowId, step, parentTasks) {
  const parents = (step.depends_on || []).join(', ') || 'none'
  return [
    'task_id: ' + step.task_id,
    'step_id: ' + step.step_id,
    'workflow_id: ' + workflowId,
    'depends_on: ' + parents,
    'parent_task_ids: ' + ((parentTasks || []).join(', ') || 'none'),
  ].join('\n')
}

// The probe failed: this session's bridge refused the follow every row makes.
// The workflow is in SwarmCloud and runs regardless; no row starts, and none
// tries another format.
function followRefused(workflowId, version, error) {
  const bridge = version
    ? 'swarm-mcp ' + version
    : 'which reported no version, so it predates the version check this plugin makes'
  const message = 'SwarmCloud has workflow ' + workflowId + ' and it runs regardless, but this session\'s SwarmCloud bridge (' + bridge + ') refused the progress follow every row makes: ' + error + '. The likely cause is a SWARM_MCP_FROM override pinning an older bridge than this plugin ships: unset it, or point it at this plugin\'s release, and restart Claude Code. Then re-join the workflow with /sc attach ' + workflowId + ' (or /sc:swarmcloud {attach: "' + workflowId + '"}). No row was started, and no row falls back to another format.'
  log(workflowId + ' · follow refused by the bridge · ' + clip(error, 160))
  return { workflow_id: workflowId, state: 'FOLLOW_REFUSED', error: message, steps: [] }
}

function notSubmitted(error) {
  log('not submitted · ' + clip(error, 200))
  return { workflow_id: null, state: 'NOT_SUBMITTED', error: error, steps: [] }
}


// One sc:step row per step, shallowest first, each handed the task ids of its
// unfinished parents. `steps` carry step_id, task_id and depends_on.
async function followSteps(workflowId, workflowName, steps, stages, parentTasksOf) {
  const depth = levelsOf(steps)
  const ordered = steps.slice().sort((a, b) => (depth[a.step_id] || 0) - (depth[b.step_id] || 0))
  return await pipeline(
    ordered,
    async (step) => {
      if (!step.task_id) return { row_error: 'SwarmCloud named no task for this step' }
      try {
        return await agent(stepPrompt(workflowId, step, parentTasksOf(step)), {
          label: rowLabel(workflowName, 'stage ' + (stages[step.step_id] || (depth[step.step_id] || 0) + 1) + ' · ' + step.step_id),
          phase: stages[step.step_id] || 'Level ' + (depth[step.step_id] || 0),
          agentType: 'sc:step',
          schema: STEP_RESULT,
        })
      } catch (error) {
        return { row_error: failureText(error) }
      }
    },
    (result, step) => {
      log(narrate(step.step_id, result, step.console))
      const row = { step_id: step.step_id, task_id: step.task_id, depends_on: step.depends_on || [] }
      if (!result || result.row_error) {
        row.state = null
        row.row_error = result && result.row_error ? result.row_error : 'the row stopped before its task finished'
        if (consoleSuffix(step.console)) row.console = step.console.trim()
        return row
      }
      return Object.assign(row, result)
    },
  )
}

// The final status read, the same after a submission and after an attach.
// The rows are returned whatever happens here: a Result row that fails must
// not take every step's result down with it.
async function finish(workflowId, workflowName, rows, knownConsole) {
  phase('Result')
  let final = null
  let finalFailure = null
  try {
    final = await agent('STATUS\nworkflow_id: ' + workflowId, {
      label: rowLabel(workflowName, 'result'),
      phase: 'Result',
      agentType: 'sc:workflow',
      schema: WORKFLOW_STATE,
    })
  } catch (error) {
    finalFailure = failureText(error)
  }
  // A row whose relay failed is not a step whose task failed (#285). The sc:step
  // relay retypes its answer into StructuredOutput JSON, and one retyping a raw
  // answer excerpt (backticks, quotes, ******** masks, paths, backslashes) ran out
  // of its five attempts on a step whose task had SUCCEEDED -- so the row said
  // state null for a finished task. The workflow status read just above names
  // every step's state as SwarmCloud derived it, so such a row takes its state
  // from there, keeps its own row_error beside it, and says where the state came
  // from (state_from, on these rows only: a row that answered keeps the
  // STEP_RESULT shape). It stays null only when that read failed too, or did not
  // name the step, or named it with no state -- nothing here guesses one.
  const statusOf = {}
  if (final && Array.isArray(final.steps)) {
    for (const entry of final.steps) {
      if (entry && typeof entry.step_id === 'string' && typeof entry.state === 'string' && entry.state) {
        statusOf[entry.step_id] = entry.state
      }
    }
  }
  for (const row of rows) {
    if (!row || !row.row_error || row.state !== null) continue
    if (!Object.prototype.hasOwnProperty.call(statusOf, row.step_id)) continue
    row.state = statusOf[row.step_id]
    row.state_from = 'workflow status'
    log(row.step_id + ' ' + row.state + ' (from the workflow status; its row failed: ' + clip(row.row_error, 100) + ')')
  }

  const state = final ? final.state : null
  const note = final
    ? final.state_note
    : finalFailure
      ? 'the row reading the workflow state failed: ' + finalFailure
      : 'the row reading the workflow state stopped before it answered'
  // The workflow's console link: the result row's copy, else the one the
  // submit or attach reply carried.
  const workflowConsole = (final && final.console) || knownConsole
  const workflowLink = consoleSuffix(workflowConsole)
  log(workflowId + ' ' + (state || 'state not read') + (note ? ' · ' + clip(note, 160) : '') + workflowLink)

  const finished = {
    workflow_id: workflowId,
    state: state,
    state_note: note,
    steps: rows,
  }
  if (workflowLink) finished.console = workflowConsole.trim()
  return finished
}

const given = readSpec(args)

if (given.attach) {
  // ATTACH: nothing is submitted. One read, one probe, a row per unfinished step.
  phase('Attach')
  const workflowId = given.attach
  let attached = null
  let attachFailure = null
  try {
    attached = await agent('ATTACH\nworkflow_id: ' + workflowId, {
      label: rowLabel(workflowId, 'attach'),
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
  if (attached.follow_error) return followRefused(workflowId, attached.bridge_version, attached.follow_error)

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
  log(workflowId + ' attached · ' + attached.steps.length + ' step(s) · ' + open.length + ' unfinished' + consoleSuffix(attached.console))

  const followed = await followSteps(workflowId, workflowId, open, {}, (step) =>
    (step.depends_on || [])
      .filter((parent) => known[parent] && !finished(known[parent]) && known[parent].task_id)
      .map((parent) => known[parent].task_id),
  )
  const byStep = {}
  for (const row of followed) if (row) byStep[row.step_id] = row
  const rows = attached.steps.map((step) => doneRows[step.step_id] || byStep[step.step_id]).filter(Boolean)
  return await finish(workflowId, workflowId, rows, attached.console)
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
      label: rowLabel(given.path, 'read spec'),
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
  source.specRef = read.spec_ref
  digest = read.spec_digest
  log('read the spec from ' + (read.path || given.path) + ' · ' + digest)
}

if (!digest) digest = specDigest(spec)
const specLabel = typeof spec.label === 'string' && spec.label.trim() ? spec.label.trim() : null
const stages = {}
for (const step of spec.steps) {
  if (step && typeof step.stage === 'string' && step.stage.trim()) stages[step.step_id] = step.stage.trim()
}

let submitted = null
let submitFailure = null
try {
  submitted = await agent(submitPrompt(digest, source), {
    label: rowLabel(specLabel || 'workflow', 'submit'),
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
  return {
    workflow_id: submitted.workflow_id,
    state: 'SUBMITTED_UNVERIFIED',
    error: 'SwarmCloud accepted workflow ' + submitted.workflow_id + ', but the reply relayed back does not match the spec given, so no row was started: ' + problems.join('; ') + '. The workflow runs regardless: read it with swarm_workflow_status, or cancel it with swarm_workflow_cancel if it is not what was meant.',
    steps: [],
  }
}

if (submitted.follow_error) return followRefused(submitted.workflow_id, submitted.bridge_version, submitted.follow_error)

const where = submitted.repository ? ' · clones ' + submitted.repository : ' · clones no repository'
log(submitted.workflow_id + ' submitted · ' + submitted.steps.length + ' step(s)' + where + consoleSuffix(submitted.console))
// Every inference note the bridge made, verbatim: it exists only in this
// reply, and this is the one place a session watching the workflow can see it.
for (const note of submitted.repository_notes || []) log(submitted.workflow_id + ' repository note: ' + note)

// Just submitted, so no parent has finished: every row is handed all of its
// parents' task ids.
const taskOf = {}
for (const step of submitted.steps) taskOf[step.step_id] = step.task_id
const workflowName = specLabel || submitted.workflow_id
const rows = await followSteps(submitted.workflow_id, workflowName, submitted.steps, stages, (step) =>
  (step.depends_on || []).map((parent) => taskOf[parent]).filter((id) => typeof id === 'string' && id),
)
return await finish(submitted.workflow_id, workflowName, rows, submitted.console)
