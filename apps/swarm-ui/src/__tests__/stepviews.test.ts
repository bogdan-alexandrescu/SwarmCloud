// THE ARITHMETIC UNDER THE TIMELINE, THE TABLE, THE DATA EDGES AND THE
// SCRUBBERS -- pure, so every decision about what is KNOWN is asserted here
// without mounting anything. `workflow.views.test.tsx` asserts the same rules
// through the rendered screen; this file pins each arm on its own, including
// the ones no screen fixture reaches (clock skew, an unparseable timestamp, a
// malformed staged-input entry).

import { describe, expect, it } from 'vitest'

import {
  depItems,
  edgeProvenance,
  inputFileOf,
  inputsByStep,
} from '../dag'
import {
  attemptFacts,
  attemptsInOrder,
  axisOf,
  nextSort,
  pctOf,
  sameStepAcross,
  sortRows,
  spanLabel,
  stateRankOf,
  stepTimes,
  type SortFacts,
} from '../stepviews'
import {
  stagedInputsOf,
  type AttemptRow,
  type Task,
  type TaskState,
  type Workflow,
  type WorkflowStep,
} from '../types'

const T0 = Date.parse('2026-09-24T12:00:00.000Z')
const iso = (s: number) => new Date(T0 + s * 1000).toISOString()

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 't',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: null,
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: null,
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

const joined = (t: Task) => ({ kind: 'state' as const, state: t.state, task: t })

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

// ---------------------------------------------------------------------------
// stepTimes
// ---------------------------------------------------------------------------

describe('stepTimes', () => {
  it('draws nothing for a step with no task, and says so in its own words', () => {
    const t = stepTimes({ kind: 'unstarted' }, T0)
    expect(t.spans).toEqual([])
    expect(t.mark?.text).toBe('not started')
    expect(t.waitedMs).toBeNull()
    expect(t.ranMs).toBeNull()
  })

  it('draws nothing for a task nobody read, and does not call it idle', () => {
    const t = stepTimes({ kind: 'unknown', taskId: 'tx' }, T0)
    expect(t.spans).toEqual([])
    expect(t.mark?.text).toBe('task unread')
    expect(t.mark?.note).toMatch(/not idle/)
  })

  it('splits a finished step into time waited and time run', () => {
    const t = stepTimes(joined(task('a', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })), T0)
    expect(t.spans.map((s) => s.kind)).toEqual(['waited', 'ran'])
    expect(t.waitedMs).toBe(10_000)
    expect(t.ranMs).toBe(90_000)
    expect(t.spans.every((s) => !s.open)).toBe(true)
  })

  it('keeps a MEASURED zero run as a zero, not as an absence', () => {
    const t = stepTimes(joined(task('a', 'SUCCEEDED', { started_at: iso(-500), completed_at: iso(-500) })), T0)
    expect(t.ranMs).toBe(0)
    const ran = t.spans.find((s) => s.kind === 'ran')
    expect(ran).toBeTruthy()
    expect(ran!.to - ran!.from).toBe(0)
  })

  it('never draws a step that ended without ever starting as having run', () => {
    const t = stepTimes(joined(task('a', 'CANCELLED', { completed_at: iso(-300) })), T0)
    expect(t.spans.map((s) => s.kind)).toEqual(['waited'])
    expect(t.ranMs).toBeNull()
  })

  it('invents no end for a terminal step that recorded none', () => {
    const t = stepTimes(joined(task('a', 'FAILED', { started_at: iso(-500) })), T0)
    expect(t.spans.map((s) => s.kind)).toEqual(['waited'])
    expect(t.spans[0]!.to).toBe(T0 - 500_000)
    expect(t.mark?.text).toBe('end not recorded')
    expect(t.ranMs).toBeNull()
  })

  it('draws a running step as open to now', () => {
    const t = stepTimes(joined(task('a', 'RUNNING', { started_at: iso(-400) })), T0)
    const running = t.spans.find((s) => s.kind === 'running')!
    expect(running.open).toBe(true)
    expect(running.to).toBe(T0)
    expect(t.ranMs).toBe(400_000)
  })

  it('never runs backwards when the browser clock is behind the platform', () => {
    const t = stepTimes(joined(task('a', 'RUNNING', { started_at: iso(30) })), T0)
    const running = t.spans.find((s) => s.kind === 'running')!
    expect(running.to).toBeGreaterThanOrEqual(running.from)
    expect(t.ranMs).toBe(0)
  })

  it('draws a step waiting for another attempt as waiting, whatever start time it carries', () => {
    for (const state of ['PARKED', 'QUEUED', 'READY', 'LEASED', 'DISPATCHED'] as const) {
      const t = stepTimes(joined(task('a', state, { started_at: iso(-450), attempt_count: 2 })), T0)
      expect(t.spans.map((s) => s.kind), state).toEqual(['waiting'])
      expect(t.spans[0]!.open).toBe(true)
      expect(t.ranMs, state).toBeNull()
      expect(t.attempts).toBe(2)
    }
  })
})

// ---------------------------------------------------------------------------
// The axis
// ---------------------------------------------------------------------------

describe('axisOf', () => {
  it('is null when nothing has a time, rather than an axis of zero width', () => {
    expect(axisOf([stepTimes({ kind: 'unstarted' }, T0)], T0, null)).toBeNull()
  })

  it('starts at the workflow when the workflow is older than every step', () => {
    const rows = [stepTimes(joined(task('a', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })), T0)]
    const axis = axisOf(rows, T0, T0 - 900_000)!
    expect(axis.t0).toBe(T0 - 900_000)
    expect(axis.t1).toBe(T0 - 500_000)
    expect(axis.now).toBeNull()
  })

  it('carries now only when a span is still open', () => {
    const rows = [stepTimes(joined(task('a', 'RUNNING', { started_at: iso(-60) })), T0)]
    expect(axisOf(rows, T0, null)!.now).toBe(T0)
  })

  it('labels at most six round ticks, the first at zero', () => {
    const rows = [stepTimes(joined(task('a', 'RUNNING', { started_at: iso(-60) })), T0)]
    const axis = axisOf(rows, T0, null)!
    expect(axis.ticks.length).toBeLessThanOrEqual(6)
    expect(axis.ticks[0]!.label).toBe('0')
    expect(axis.ticks.map((t) => t.label)).toEqual(['0', '+2m', '+4m', '+6m', '+8m', '+10m'])
    expect(pctOf(axis, axis.ticks[axis.ticks.length - 1]!.at)).toBe(100)
  })

  it('gives a workflow whose every step took no time a one-second axis, not a zero one', () => {
    const rows = [stepTimes(joined(task('a', 'SUCCEEDED', { created_at: iso(-5), started_at: iso(-5), completed_at: iso(-5) })), T0)]
    const axis = axisOf(rows, T0, null)!
    expect(axis.t1 - axis.t0).toBe(1000)
  })

  it('writes whole units and no zero remainders', () => {
    expect(spanLabel(300_000)).toBe('5m')
    expect(spanLabel(90_000)).toBe('1m 30s')
    expect(spanLabel(7_200_000)).toBe('2h')
    expect(spanLabel(86_400_000 * 2)).toBe('2d')
  })
})

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

describe('sortRows', () => {
  const row = (id: string, order: number, costUsd: number | null) => ({
    id,
    sort: { order, stateRank: 0, waitedMs: null, ranMs: null, attempts: null, costUsd } satisfies SortFacts,
  })
  const rows = [row('a', 0, null), row('b', 1, 0.5), row('c', 2, 0), row('d', 3, null), row('e', 4, 0.1)]

  it('puts an unmeasured value last going up', () => {
    expect(sortRows(rows, { key: 'cost', dir: 'ascending' }).map((r) => r.id)).toEqual(['c', 'e', 'b', 'a', 'd'])
  })

  it('and still last going down: an absence is neither small nor large', () => {
    expect(sortRows(rows, { key: 'cost', dir: 'descending' }).map((r) => r.id)).toEqual(['b', 'e', 'c', 'a', 'd'])
  })

  it('keeps a measured zero among the numbers', () => {
    const up = sortRows(rows, { key: 'cost', dir: 'ascending' }).map((r) => r.id)
    expect(up.indexOf('c')).toBeLessThan(up.indexOf('a'))
  })

  it('starts a new column ascending and flips the same one', () => {
    expect(nextSort({ key: 'step', dir: 'ascending' }, 'cost')).toEqual({ key: 'cost', dir: 'ascending' })
    expect(nextSort({ key: 'cost', dir: 'ascending' }, 'cost')).toEqual({ key: 'cost', dir: 'descending' })
  })

  it('ranks failures first and gives an unread state no rank at all', () => {
    expect(stateRankOf({ kind: 'unknown', taskId: 'x' })).toBeNull()
    const failed = stateRankOf(joined(task('a', 'FAILED')))!
    const running = stateRankOf(joined(task('a', 'RUNNING')))!
    const done = stateRankOf(joined(task('a', 'SUCCEEDED')))!
    const none = stateRankOf({ kind: 'unstarted' })!
    expect(failed).toBeLessThan(running)
    expect(running).toBeLessThan(done)
    expect(done).toBeLessThan(none)
  })
})

// ---------------------------------------------------------------------------
// The scrubbers
// ---------------------------------------------------------------------------

function wf(id: string, created: string, steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: id,
    state: 'RUNNING',
    tenant_id: 't',
    stored_state: 'RUNNING',
    state_source: 'derived',
    created_at: created,
    updated_at: created,
    submitted_by: null,
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
}

describe('sameStepAcross', () => {
  it('lists every workflow holding the step, newest first, and puts an unreadable date last', () => {
    const found = sameStepAcross(
      [
        wf('w_old', iso(-7200), [step('plan', [])]),
        wf('w_none', iso(-10), [step('other', [])]),
        wf('w_bad', 'not a date', [step('plan', [])]),
        wf('w_new', iso(-60), [step('plan', [])]),
      ],
      'plan',
    )
    expect(found.map((f) => f.workflowId)).toEqual(['w_new', 'w_old', 'w_bad'])
  })
})

function attempt(n: number, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `a${n}`,
    task_id: 't',
    tenant_id: 't',
    generation: n,
    lease_id: `l${n}`,
    backend: 'cloud-run',
    execution_name: null,
    created_at: iso(-600 + n),
    started_at: iso(-599 + n),
    completed_at: iso(-590 + n),
    exit_code: 0,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...over,
  }
}

describe('attemptsInOrder and attemptFacts', () => {
  it('orders attempts oldest first, whatever order the route served them in', () => {
    expect(attemptsInOrder([attempt(3), attempt(1), attempt(2)]).map((a) => a.generation)).toEqual([1, 2, 3])
  })

  const fact = (facts: ReturnType<typeof attemptFacts>, key: string) => facts.find((f) => f.key === key)!.cell

  it('prints an unreported cost as a word, and a counted zero as a digit', () => {
    const facts = attemptFacts(attempt(1), false, T0)
    expect(fact(facts, 'cost')).toMatchObject({ kind: 'absent', text: 'not reported' })
    expect(fact(facts, 'ckpts')).toMatchObject({ kind: 'measured', text: '0' })
    expect(fact(facts, 'cost').text).not.toMatch(/\$0/)
  })

  it('tells "still running" from "never written" for a missing exit code', () => {
    const open = attempt(1, { completed_at: null, exit_code: null })
    expect(fact(attemptFacts(open, true, T0), 'exit').text).toBe('still running')
    expect(fact(attemptFacts(open, false, T0), 'exit').text).toBe('not recorded')
    expect(fact(attemptFacts(open, false, T0), 'took').text).toBe('not recorded')
    expect(fact(attemptFacts(open, true, T0), 'took').text).toMatch(/so far$/)
  })

  it('says an attempt that never started never started', () => {
    expect(fact(attemptFacts(attempt(1, { started_at: null }), false, T0), 'took').text).toBe('never started')
  })

  it('keeps the first line of an error on the surface and the whole of it underneath', () => {
    const e = fact(attemptFacts(attempt(1, { error: 'killed\nline two\nline three' }), false, T0), 'error')
    expect(e.text).toBe('killed (+2 lines)')
    expect(e.note).toBe('killed\nline two\nline three')
  })
})

// ---------------------------------------------------------------------------
// Staged inputs and the data edges they are drawn on
// ---------------------------------------------------------------------------

describe('stagedInputsOf', () => {
  it('keeps "nothing reported" apart from "reported nothing"', () => {
    expect(stagedInputsOf(task('a', 'RUNNING'))).toEqual({ kind: 'unreported' })
    expect(stagedInputsOf(task('a', 'SUCCEEDED', { result_summary: {} }))).toEqual({ kind: 'unreported' })
    expect(stagedInputsOf(task('a', 'SUCCEEDED', { result_summary: { staged_inputs: [] } }))).toEqual({
      kind: 'reported',
      inputs: [],
      malformed: 0,
    })
  })

  it('counts an entry it cannot read rather than dropping it, and never makes a size zero', () => {
    const r = stagedInputsOf(
      task('a', 'SUCCEEDED', {
        result_summary: { staged_inputs: [{ filename: 'x.md', path: 'x.md' }, 42, { path: 'no-name' }] },
      }),
    )
    expect(r.kind).toBe('reported')
    if (r.kind !== 'reported') return
    expect(r.malformed).toBe(2)
    expect(r.inputs).toHaveLength(1)
    expect(r.inputs[0]!.bytes).toBeNull()
    expect(r.inputs[0]!.upstreamTaskId).toBeNull()
  })
})

describe('inputsByStep', () => {
  const steps = [
    step('plan', [], { task_id: 'tp' }),
    step('scan', [], { task_id: 'ts' }),
    step('build', ['plan', 'scan'], { task_id: 'tb', input_from: { plan: 'plan.md' } }),
    step('later', ['build'], { input_from: { build: 'patch.diff' } }),
    step('lost', ['build'], { task_id: 'tl', input_from: { build: 'patch.diff' } }),
    step('old', ['build'], { task_id: 'to', input_from: { build: 'patch.diff' } }),
  ]
  const tasks = new Map<string, Task>([
    [
      'tb',
      task('tb', 'SUCCEEDED', {
        completed_at: iso(-10),
        result_summary: {
          staged_inputs: [
            { task_id: 'tp', filename: 'plan.md', path: 'plan.md', bytes: 12, from_checkpoint: true },
            { task_id: 'ts', filename: 'scan.log', path: 'scan.log', bytes: 5 },
            { task_id: 'elsewhere', filename: 'x', path: 'x', bytes: 1 },
            { filename: 'brief.txt', path: 'brief.txt', bytes: 3 },
          ],
        },
      }),
    ],
    ['to', task('to', 'SUCCEEDED', { completed_at: iso(-5), result_summary: {} })],
  ])
  const inputs = inputsByStep(steps, tasks)

  it('joins a staged file back to the step it came from, through the task id', () => {
    expect(inputs.get('build')!.declared.get('plan')).toEqual({
      kind: 'staged',
      file: 'plan.md',
      bytes: 12,
      fromCheckpoint: true,
    })
  })

  it('names the four kinds of not-yet apart', () => {
    expect(inputs.get('later')!.declared.get('build')).toMatchObject({ kind: 'declared', why: 'not-started' })
    expect(inputs.get('lost')!.declared.get('build')).toMatchObject({ kind: 'declared', why: 'unread' })
    expect(inputs.get('old')!.declared.get('build')).toMatchObject({ kind: 'declared', why: 'not-reported' })
    const running = inputsByStep(steps, new Map([...tasks, ['tl', task('tl', 'RUNNING')]]))
    expect(running.get('lost')!.declared.get('build')).toMatchObject({ kind: 'declared', why: 'in-flight' })
  })

  it('keeps every staged file no edge can carry, and says where each came from', () => {
    const stray = inputs.get('build')!.stray
    expect(stray.map((s) => s.source.kind).sort()).toEqual(['outside', 'submission', 'undeclared'])
  })

  it('draws an edge with no declared file as an ordering edge', () => {
    expect(edgeProvenance({ from: 'scan', to: 'build' }, inputs)).toEqual({ kind: 'order' })
    expect(edgeProvenance({ from: 'plan', to: 'build' }, inputs).kind).toBe('staged')
  })
})

describe('the dependency line', () => {
  it('names the file a dependency stages, and nothing for one that only orders', () => {
    expect(depItems(step('b', ['a', 'c'], { input_from: { a: 'a.md' } })).map((d) => d.text)).toEqual([
      'a (a.md)',
      'c',
    ])
  })

  it('reads a malformed declaration as "declared nothing", never as a value', () => {
    expect(inputFileOf(step('b', ['constructor']), 'constructor')).toBeNull()
    expect(inputFileOf(step('b', ['a'], { input_from: { a: 7 } as unknown as Record<string, string> }), 'a')).toBeNull()
    expect(inputFileOf(step('b', ['a'], { input_from: 'a.md' as unknown as Record<string, string> }), 'a')).toBeNull()
  })
})
