export const meta = {
  name: 'run',
  description: 'Run a SwarmCloud workflow spec with every step executing in SwarmCloud and shown here as a running agent with its live progress',
  whenToUse: 'You have a SwarmCloud workflow spec, the JSON that swarm workflow reads, and want each step visible in /workflows while it runs remotely. Pass the spec object as args.',
  phases: [
    { title: 'Submit', detail: 'one sc:workflow agent submits the spec with swarm_workflow, checked against the digest this script computed' },
    { title: 'Result', detail: 'one sc:workflow agent reads the workflow state SwarmCloud derived' },
  ],
}

// /sc:run -- a SwarmCloud workflow, shown as a Claude Code workflow.
//
// Every step EXECUTES in SwarmCloud: one agent submits the whole spec, and
// SwarmCloud owns the DAG from then on -- dependencies, input_from staging,
// on_step_failure, retries. Each step is then SHOWN here by one sc:step agent
// that follows that step's task until it finishes, so /workflows lists a row
// per step with its phase, its state, its elapsed time and a transcript of
// what the remote agent is doing.
//
// A step's row waits for its OWN task only. It may start before its parents
// finish and will say that it is waiting, and why, in its first lines; that
// costs nothing, because a waiting task holds no capacity.
//
// Steps are grouped under 'Level N' -- their depth in the DAG -- or under the
// `stage` the spec gives a step. Neither is known before the spec is read, so
// they are not in meta.phases and each gets a progress group of its own.
//
// NOTHING CROSSES THE RELAY UNCHECKED. A script cannot call a tool, so the
// spec reaches swarm_workflow through an agent that RETYPES it, and the
// step-to-task map comes back the same way. A haiku relay can drop a step,
// swap two task ids or tidy a prompt, and nothing downstream would notice. So:
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
// This script derives nothing SwarmCloud decides. The workflow's final state
// is read back from swarm_workflow_status, which serves the state the server
// derived from its steps; it is never computed here from the rows.

const SUBMITTED = {
  type: 'object',
  properties: {
    workflow_id: { type: ['string', 'null'] },
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          step_id: { type: 'string' },
          task_id: { type: ['string', 'null'] },
          depends_on: { type: 'array', items: { type: 'string' } },
        },
        required: ['step_id', 'task_id', 'depends_on'],
      },
    },
    repository: { type: ['string', 'null'] },
    // Owner decision, 2026-09-26: every inference note the bridge's reply
    // carries (uncommitted changes not visible, the branch's upstream, the
    // checkout path) must reach here -- it exists only in that reply, and a
    // session watching this workflow has no other way to see it.
    repository_notes: { type: 'array', items: { type: 'string' } },
    spec_digest: { type: ['string', 'null'] },
    error: { type: ['string', 'null'] },
  },
  required: ['workflow_id', 'steps', 'repository', 'repository_notes', 'spec_digest', 'error'],
}

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
  },
  required: ['state', 'answer_excerpt', 'cost_usd', 'duration_s', 'pr_url', 'artifacts', 'last_error'],
}

const WORKFLOW_STATE = {
  type: 'object',
  properties: {
    state: { type: ['string', 'null'] },
    state_note: { type: ['string', 'null'] },
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
  required: ['state', 'state_note', 'steps'],
}

function readSpec(given) {
  let spec = given
  if (typeof spec === 'string') {
    try {
      spec = JSON.parse(spec)
    } catch (error) {
      throw new Error('/sc:run takes a SwarmCloud workflow spec object; its argument is a string that is not JSON: ' + error.message)
    }
  }
  if (spec && typeof spec === 'object' && !Array.isArray(spec) && spec.spec && typeof spec.spec === 'object') {
    spec = spec.spec
  }
  if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !Array.isArray(spec.steps) || spec.steps.length === 0) {
    throw new Error('/sc:run takes a SwarmCloud workflow spec: an object with a non-empty `steps` list, the shape `swarm workflow` reads')
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

function failureText(error) {
  return String((error && error.message) || error)
}

// One narrator line per finished step: 'scan-03 SUCCEEDED · 4m12s · $0.21 · PR #231'.
function narrate(stepId, result) {
  if (!result || result.row_error) {
    const why = result && result.row_error ? ': ' + clip(result.row_error, 100) : ''
    return stepId + ' · its row stopped before the task finished' + why + ' -- the SwarmCloud task is unaffected'
  }
  const parts = [stepId + ' ' + (result.state || 'UNKNOWN'), duration(result.duration_s), money(result.cost_usd)]
  const pr = pullRequest(result.pr_url)
  if (pr) parts.push(pr)
  if (result.state !== 'SUCCEEDED' && result.last_error) parts.push(clip(result.last_error, 100))
  return parts.join(' · ')
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

function submitPrompt(spec, digest) {
  return ['SUBMIT', 'spec_digest: ' + digest, 'BEGIN SPEC', JSON.stringify(spec, null, 2), 'END SPEC'].join('\n')
}

function stepPrompt(workflowId, step) {
  const parents = (step.depends_on || []).join(', ') || 'none'
  return [
    'task_id: ' + step.task_id,
    'step_id: ' + step.step_id,
    'workflow_id: ' + workflowId,
    'depends_on: ' + parents,
  ].join('\n')
}

const spec = readSpec(args)
const digest = specDigest(spec)
const specLabel = typeof spec.label === 'string' && spec.label.trim() ? spec.label.trim() : null
const stages = {}
for (const step of spec.steps) {
  if (step && typeof step.stage === 'string' && step.stage.trim()) stages[step.step_id] = step.stage.trim()
}

phase('Submit')
let submitted = null
let submitFailure = null
try {
  submitted = await agent(submitPrompt(spec, digest), {
    label: 'submit',
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
  const error = why + '. Whether SwarmCloud created the workflow is UNKNOWN: swarm_workflow may already have submitted it. Look for it in the console\'s workflow list' + (specLabel ? ' (label ' + specLabel + ')' : '') + ' before running /sc:run again, which would submit a second copy.'
  log('submission outcome unknown · ' + clip(why, 200))
  return { workflow_id: null, state: 'SUBMISSION_UNKNOWN', error: error, steps: [] }
}

if (!submitted.workflow_id) {
  // An explicit refusal: swarm_workflow answered with an error, so nothing was sent.
  log('not submitted · ' + clip(submitted.error, 200))
  return { workflow_id: null, state: 'NOT_SUBMITTED', error: submitted.error, steps: [] }
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

const where = submitted.repository ? ' · clones ' + submitted.repository : ' · clones no repository'
log(submitted.workflow_id + ' submitted · ' + submitted.steps.length + ' step(s)' + where)
// Every inference note the bridge made, verbatim: it exists only in this
// reply, and this is the one place a session watching the workflow can see it.
for (const note of submitted.repository_notes || []) log(submitted.workflow_id + ' repository note: ' + note)

const depth = levelsOf(submitted.steps)
const ordered = submitted.steps.slice().sort((a, b) => (depth[a.step_id] || 0) - (depth[b.step_id] || 0))

const rows = await pipeline(
  ordered,
  async (step) => {
    if (!step.task_id) return { row_error: 'SwarmCloud named no task for this step' }
    try {
      return await agent(stepPrompt(submitted.workflow_id, step), {
        label: step.step_id,
        phase: stages[step.step_id] || 'Level ' + (depth[step.step_id] || 0),
        agentType: 'sc:step',
        schema: STEP_RESULT,
      })
    } catch (error) {
      return { row_error: failureText(error) }
    }
  },
  (result, step) => {
    log(narrate(step.step_id, result))
    const row = { step_id: step.step_id, task_id: step.task_id, depends_on: step.depends_on || [] }
    if (!result || result.row_error) {
      row.state = null
      row.row_error = result && result.row_error ? result.row_error : 'the row stopped before its task finished'
      return row
    }
    return Object.assign(row, result)
  },
)

// The rows are returned whatever happens here: a Result row that fails must
// not take every step's result down with it.
phase('Result')
let final = null
let finalFailure = null
try {
  final = await agent('STATUS\nworkflow_id: ' + submitted.workflow_id, {
    label: 'workflow state',
    phase: 'Result',
    agentType: 'sc:workflow',
    schema: WORKFLOW_STATE,
  })
} catch (error) {
  finalFailure = failureText(error)
}
const state = final ? final.state : null
const note = final
  ? final.state_note
  : finalFailure
    ? 'the row reading the workflow state failed: ' + finalFailure
    : 'the row reading the workflow state stopped before it answered'
log(submitted.workflow_id + ' ' + (state || 'state not read') + (note ? ' · ' + clip(note, 160) : ''))

return {
  workflow_id: submitted.workflow_id,
  state: state,
  state_note: note,
  steps: rows,
}
