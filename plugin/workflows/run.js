export const meta = {
  name: 'run',
  description: 'Run a SwarmCloud workflow spec with every step executing in SwarmCloud and shown here as a running agent with its live progress',
  whenToUse: 'You have a SwarmCloud workflow spec, the JSON that swarm workflow reads, and want each step visible in /workflows while it runs remotely. Pass the spec object as args.',
  phases: [
    { title: 'Submit', detail: 'one sc:workflow agent submits the spec with swarm_workflow' },
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
    error: { type: ['string', 'null'] },
  },
  required: ['workflow_id', 'steps', 'repository', 'error'],
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

function submitPrompt(spec) {
  return ['SUBMIT', 'BEGIN SPEC', JSON.stringify(spec, null, 2), 'END SPEC'].join('\n')
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
const stages = {}
for (const step of spec.steps) {
  if (step && typeof step.stage === 'string' && step.stage.trim()) stages[step.step_id] = step.stage.trim()
}

phase('Submit')
const submitted = await agent(submitPrompt(spec), {
  label: 'submit',
  phase: 'Submit',
  agentType: 'sc:workflow',
  schema: SUBMITTED,
})
if (!submitted || submitted.error || !submitted.workflow_id) {
  const why = submitted
    ? submitted.error || 'the submission answered with no workflow id'
    : 'the submitting agent stopped before it answered'
  log('not submitted · ' + clip(why, 200))
  return { workflow_id: null, state: 'NOT_SUBMITTED', error: why, steps: [] }
}
const where = submitted.repository ? ' · clones ' + submitted.repository : ' · clones no repository'
log(submitted.workflow_id + ' submitted · ' + submitted.steps.length + ' step(s)' + where)

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
      return { row_error: String((error && error.message) || error) }
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

phase('Result')
const final = await agent('STATUS\nworkflow_id: ' + submitted.workflow_id, {
  label: 'workflow state',
  phase: 'Result',
  agentType: 'sc:workflow',
  schema: WORKFLOW_STATE,
})
const state = final ? final.state : null
const note = final ? final.state_note : 'the agent reading the workflow state stopped before it answered'
log(submitted.workflow_id + ' ' + (state || 'state not read') + (note ? ' · ' + clip(note, 160) : ''))

return {
  workflow_id: submitted.workflow_id,
  state: state,
  state_note: note,
  steps: rows,
}
