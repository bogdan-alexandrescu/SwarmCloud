// THE ARITHMETIC UNDER THE TIMELINE, THE TABLE, THE DATA EDGES AND THE
// SCRUBBERS -- pure, so every decision about what is KNOWN is asserted here
// without mounting anything. `workflow.views.test.tsx` asserts the same rules
// through the rendered screen; this file pins each arm on its own, including
// the ones no screen fixture reaches (clock skew, an unparseable timestamp, a
// malformed staged-input entry).

import { describe, expect, it } from 'vitest'

import {
  depItems,
  edgeKinds,
  heightOf,
  edgeProvenance,
  inputFileOf,
  inputsByStep,
  layoutOf,
  nodeHeightAt,
  shapeOf,
  stepDuration,
  taskInputsOf,
} from '../dag'
import {
  attemptFacts,
  attemptPhase,
  attemptsInOrder,
  axisOf,
  drawnSpan,
  failureCause,
  failureGroups,
  firstLine,
  nextSort,
  parentsDoneOf,
  pctOf,
  sameStepAcross,
  shapeSignature,
  sortRows,
  spanLabel,
  stateRankOf,
  stepTimes,
  stepWhy,
  type SortFacts,
  type TimelineAxis,
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
    // And says so, in the word the node and the table use.
    expect(t.mark?.text).toBe('never started')
  })

  it('says "never started" for a terminal step with neither a start nor an end, and draws nothing', () => {
    const t = stepTimes(joined(task('a', 'CANCELLED')), T0)
    expect(t.spans).toEqual([])
    expect(t.mark?.text).toBe('never started')
  })

  it('puts no word on a finished step whose recorded start is merely skewed', () => {
    // A start AFTER the end: clock skew. Something did record a start, so it is
    // not "never started".
    const t = stepTimes(joined(task('a', 'SUCCEEDED', { started_at: iso(-100), completed_at: iso(-300) })), T0)
    expect(t.mark).toBeNull()
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

  it('WF-12: hangs a last label left of its line only in the last quarter of the track, and never the first', () => {
    const ends = (a: TimelineAxis) => a.ticks.map((t) => t.end === true)
    const spanning = (seconds: number) =>
      axisOf(
        [stepTimes(joined(task('a', 'SUCCEEDED', { created_at: iso(-seconds), started_at: iso(-seconds), completed_at: iso(0) })), T0)],
        T0,
        null,
      )!
    // Ten days and an hour: the step is 7d, so the last of two ticks sits at
    // 168 / 241 of the track -- about 70%. There is room to its right, so its
    // label faces right like every other.
    const far = spanning(241 * 3600)
    expect(far.ticks.map((t) => t.label)).toEqual(['0', '+7d'])
    const farLast = pctOf(far, far.ticks[far.ticks.length - 1]!.at)
    expect(farLast).toBeGreaterThan(65)
    expect(farLast).toBeLessThan(75)
    expect(ends(far)).toEqual([false, false])
    // Twenty-one minutes: 5m steps, the last at 20 / 21 -- about 95%. It hangs.
    const near = spanning(21 * 60)
    expect(near.ticks.map((t) => t.label)).toEqual(['0', '+5m', '+10m', '+15m', '+20m'])
    expect(ends(near)).toEqual([false, false, false, false, true])
    // The first tick never hangs, including on the one-second floor, where the
    // last tick is at 100% and there are only two.
    const floor = spanning(1)
    expect(ends(floor)).toEqual([false, true])
    for (const a of [far, near, floor]) expect(ends(a)[0]).toBe(false)
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
      shapeSignature([step('plan', [])]),
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

  const OVER = attemptPhase('SUCCEEDED', true)
  const RUNNING = attemptPhase('RUNNING', true)

  it('prints an unreported cost as a word, and a counted zero as a digit', () => {
    const facts = attemptFacts(attempt(1), OVER, T0)
    expect(fact(facts, 'cost')).toMatchObject({ kind: 'absent', text: 'not reported' })
    expect(fact(facts, 'ckpts')).toMatchObject({ kind: 'measured', text: '0' })
    expect(fact(facts, 'cost').text).not.toMatch(/\$0/)
  })

  it('tells "still running" from "never written" for a missing exit code', () => {
    const open = attempt(1, { completed_at: null, exit_code: null })
    expect(fact(attemptFacts(open, RUNNING, T0), 'exit').text).toBe('still running')
    expect(fact(attemptFacts(open, OVER, T0), 'exit').text).toBe('not recorded')
    expect(fact(attemptFacts(open, OVER, T0), 'took').text).toBe('end not written')
    expect(fact(attemptFacts(open, RUNNING, T0), 'took').text).toMatch(/so far$/)
  })

  it('says an attempt that never started never started', () => {
    expect(fact(attemptFacts(attempt(1, { started_at: null }), OVER, T0), 'took').text).toBe('never started')
  })

  it('times only the newest attempt of a STARTING or RUNNING task as "so far"', () => {
    // Every state a task can be in, with its newest attempt started and not
    // ended -- the shape `control.park()` leaves behind, since it never calls
    // `record_attempt_end`.
    const open = attempt(1, { completed_at: null, exit_code: null })
    const states: TaskState[] = [
      'SUBMITTED', 'QUEUED', 'READY', 'LEASED', 'DISPATCHED', 'STARTING', 'RUNNING',
      'PARKED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED',
    ]
    for (const s of states) {
      const facts = attemptFacts(open, attemptPhase(s, true), T0)
      const live = s === 'STARTING' || s === 'RUNNING'
      expect(/so far/.test(fact(facts, 'took').text), s).toBe(live)
      expect(fact(facts, 'exit').text === 'still running', s).toBe(live)
      expect(fact(facts, 'took').kind, s).toBe(live ? 'measured' : 'absent')
    }
    // An EARLIER attempt of a running task is over.
    expect(fact(attemptFacts(open, attemptPhase('RUNNING', false), T0), 'took').text).toBe('end not written')
    // A parked task's newest attempt says what the task is doing now.
    expect(fact(attemptFacts(open, attemptPhase('PARKED', true), T0), 'took').text).toBe('parked, end not written')
  })

  it('says the current attempt of a task being dispatched has not started YET', () => {
    const fresh = attempt(1, { started_at: null, completed_at: null, exit_code: null })
    for (const s of ['LEASED', 'DISPATCHED', 'STARTING'] as const) {
      const facts = attemptFacts(fresh, attemptPhase(s, true), T0)
      expect(fact(facts, 'took').text, s).toBe('not started yet')
      expect(fact(facts, 'exit').text, s).toBe('no exit yet')
    }
    // Over, with no start: it never did.
    expect(fact(attemptFacts(fresh, attemptPhase('PARKED', true), T0), 'took').text).toBe('never started')
    expect(fact(attemptFacts(fresh, attemptPhase('LEASED', false), T0), 'took').text).toBe('never started')
    // Unread: nobody knows which.
    expect(fact(attemptFacts(fresh, attemptPhase(null, true), T0), 'took').text).toBe('task unread')
  })

  it('keeps the first line of an error on the surface and the whole of it underneath', () => {
    const e = fact(attemptFacts(attempt(1, { error: 'killed\nline two\nline three' }), OVER, T0), 'error')
    expect(e.text).toBe('killed (+2 lines)')
    expect(e.note).toBe('killed\nline two\nline three')
  })

  it('WF-21: says which timestamps "took" is read from, and points at the table’s "ran"', () => {
    // Two different facts, not one figure disagreeing with itself: `took` is ONE
    // attempt's own start and end (control.py:816, :930); the Table's `ran` is
    // the task's latest start and its completion (control.py:794, :1073). They
    // are separate utcnow() reads, so a single-attempt step can differ by about
    // a second. Each note says which it is and points at the other.
    const took = fact(attemptFacts(attempt(1), OVER, T0), 'took')
    expect(took.kind).toBe('measured')
    expect(took.note, 'the note does not say it is the attempt’s own timestamps').toMatch(/attempt’s own started_at/)
    expect(took.note, 'the note does not point at the table’s figure').toMatch(/table’s “ran”/)
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

  it('does not say "lists no such file" when the result holds an entry it could not read', () => {
    const bad = inputsByStep(steps, new Map([...tasks, [
      'to',
      task('to', 'SUCCEEDED', { completed_at: iso(-5), result_summary: { staged_inputs: [{ path: 'patch.diff' }] } }),
    ]]))
    expect(bad.get('old')!.declared.get('build')).toMatchObject({ kind: 'declared', why: 'unreadable' })
    expect(bad.get('old')!.malformed).toBe(1)
  })
})

describe('edgeKinds: a collapsed stage is painted as one edge', () => {
  // 1 -> 13 -> 1. The thirteen-wide stage fits no zoom tier, so it is a band,
  // and every edge into it and out of it shares one path.
  const fan = (childKind: (n: number) => 'order' | 'staged' | 'running') => {
    const steps: WorkflowStep[] = [step('plan', [], { task_id: 'tp' })]
    const tasks = new Map<string, Task>()
    for (let n = 1; n <= 13; n++) {
      const k = childKind(n)
      steps.push(step(`c${n}`, ['plan'], { task_id: `t${n}`, input_from: k === 'order' ? {} : { plan: 'plan.md' } }))
      tasks.set(
        `t${n}`,
        k === 'running'
          ? task(`t${n}`, 'RUNNING')
          : task(`t${n}`, 'SUCCEEDED', {
              completed_at: iso(-5),
              result_summary:
                k === 'staged' ? { staged_inputs: [{ task_id: 'tp', filename: 'plan.md', path: 'plan.md', bytes: 1 }] } : {},
            }),
      )
    }
    steps.push(step('join', Array.from({ length: 13 }, (_, i) => `c${i + 1}`)))
    const layout = layoutOf(steps)
    expect(layout.bands.some((b) => !b.expanded), 'the fixture stage is not collapsed').toBe(true)
    return { kinds: edgeKinds(layout, inputsByStep(steps, tasks)), steps, tasks }
  }
  const into = (kinds: Map<string, string>) =>
    new Set(Array.from({ length: 13 }, (_, i) => kinds.get(`plan->c${i + 1}`)))

  it('gives every member the weakest kind, whichever child is last', () => {
    expect(into(fan((n) => (n === 13 ? 'staged' : 'running')).kinds)).toEqual(new Set(['declared']))
    expect(into(fan((n) => (n === 13 ? 'running' : 'staged')).kinds)).toEqual(new Set(['declared']))
    expect(into(fan((n) => (n === 13 ? 'staged' : 'order')).kinds)).toEqual(new Set(['declared']))
    expect(into(fan(() => 'staged').kinds)).toEqual(new Set(['staged']))
    expect(into(fan(() => 'order').kinds)).toEqual(new Set(['order']))
  })

  it('keeps an edge between two drawn nodes as its own kind', () => {
    const { steps, tasks } = fan(() => 'order')
    // Opened: every child is a node, every edge its own path again.
    const open = layoutOf(steps, new Set([1]))
    const inputs = inputsByStep(
      steps.map((s) => (s.step_id === 'c1' ? { ...s, input_from: { plan: 'plan.md' } } : s)),
      tasks,
    )
    const kinds = edgeKinds(open, inputs)
    expect(kinds.get('plan->c1')).toBe('declared')
    expect(kinds.get('plan->c2')).toBe('order')
  })
})

describe('the node says "never started" for a terminal step with no start', () => {
  it('and never measures it from submission as time run', () => {
    const d = stepDuration(joined(task('a', 'CANCELLED', { created_at: iso(-480), completed_at: iso(-300) })), T0)
    expect(d.kind).toBe('none')
    expect(d.text).toBe('never started')
    // A finished step that did start still ran.
    const ran = stepDuration(joined(task('b', 'SUCCEEDED', { started_at: iso(-400), completed_at: iso(-300) })), T0)
    expect(ran.text).toBe('ran 1m 40s')
  })
})

describe('taskInputsOf: one run’s inputs, joined from its own side', () => {
  it('joins the declaration (keyed by upstream TASK) to what the result reports', () => {
    const r = taskInputsOf(
      task('tb', 'SUCCEEDED', {
        completed_at: iso(-5),
        metadata: { input_from: { tp: 'plan.md', ts: 'scan.log', bad: 7 } },
        result_summary: {
          staged_inputs: [
            { task_id: 'tp', filename: 'plan.md', path: 'plan.md', bytes: 12 },
            { filename: 'brief.txt', path: 'brief.txt', bytes: 3 },
            { task_id: 'tx', filename: 'x', path: 'x', bytes: 1 },
          ],
        },
      }),
    )
    expect(r.rows.map((x) => [x.file, x.from.kind, x.declared, x.arrival.kind])).toEqual([
      ['plan.md', 'task', true, 'staged'],
      ['scan.log', 'task', true, 'declared'],
      ['brief.txt', 'submission', false, 'staged'],
      ['x', 'task', false, 'staged'],
    ])
    expect(r.rows[1]!.arrival).toMatchObject({ why: 'not-reported' })
    expect(r.malformed).toBe(0)
  })

  it('reads a live run as "reported at finish" and a malformed declaration as none', () => {
    const live = taskInputsOf(task('tb', 'RUNNING', { metadata: { input_from: { tp: 'plan.md' } } }))
    expect(live.rows[0]!.arrival).toMatchObject({ kind: 'declared', why: 'in-flight' })
    expect(taskInputsOf(task('tb', 'RUNNING', { metadata: { input_from: 'plan.md' } })).rows).toEqual([])
    expect(taskInputsOf(task('tb', 'RUNNING')).rows).toEqual([])
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

// ---------------------------------------------------------------------------
// #112: the same step, in a workflow of the same shape
// ---------------------------------------------------------------------------

/** `n` parallel steps joining into `join`: an `n -> 1` workflow. */
function fanIn(n: number, join = 'synthesis'): WorkflowStep[] {
  const branches = Array.from({ length: n }, (_, i) => step(`part-${i}`, []))
  return [...branches, step(join, branches.map((b) => b.step_id))]
}

/** Thirty steps: one root, a 28-wide fan, and the join. */
function thirty(join = 'synthesis'): WorkflowStep[] {
  const fan = Array.from({ length: 28 }, (_, i) => step(`scan-${i}`, ['plan']))
  return [step('plan', []), ...fan, step(join, fan.map((f) => f.step_id))]
}

describe('#112: sameStepAcross compares a step only with workflows of the same shape', () => {
  it('visits only the 5 -> 1 workflows when scrubbing from one, past the 30-step and the 19 -> 1 ones', () => {
    expect(thirty()).toHaveLength(30)
    const board = [
      wf('w_30', iso(-10), thirty()),
      wf('w_19', iso(-20), fanIn(19)),
      wf('w_5a', iso(-30), fanIn(5)),
      wf('w_30b', iso(-40), thirty()),
      wf('w_5b', iso(-50), fanIn(5)),
    ]
    // Every one of them has a `synthesis` step: step_id alone would visit all five.
    expect(board.every((w) => w.steps.some((s) => s.step_id === 'synthesis'))).toBe(true)
    const from5 = sameStepAcross(board, 'synthesis', shapeSignature(fanIn(5)))
    expect(from5.map((f) => f.workflowId)).toEqual(['w_5a', 'w_5b'])
    const from30 = sameStepAcross(board, 'synthesis', shapeSignature(thirty()))
    expect(from30.map((f) => f.workflowId)).toEqual(['w_30', 'w_30b'])
    const from19 = sameStepAcross(board, 'synthesis', shapeSignature(fanIn(19)))
    expect(from19.map((f) => f.workflowId)).toEqual(['w_19'])
  })

  it('keys the shape on the level widths AND the step count', () => {
    // Same widths, same count: the same shape, whatever the steps are called.
    expect(shapeSignature(fanIn(5))).toBe(shapeSignature(fanIn(5, 'merge')))
    expect(shapeSignature(fanIn(5))).not.toBe(shapeSignature(fanIn(6)))
    expect(shapeSignature(fanIn(5))).toBe(`${shapeOf(fanIn(5)).text} · 6`)
    // A chain and a join of the same count are different shapes.
    const chain = Array.from({ length: 6 }, (_, i) => step(`c${i}`, i === 0 ? [] : [`c${i - 1}`]))
    expect(shapeSignature(chain)).not.toBe(shapeSignature(fanIn(5)))
  })
})

// ---------------------------------------------------------------------------
// #107: the wait split at the parents' finish, and outliers clamped
// ---------------------------------------------------------------------------

describe('#107: parentsDoneOf', () => {
  const steps = [step('a', [], { task_id: 'ta' }), step('b', [], { task_id: 'tb' }), step('c', ['a', 'b'], { task_id: 'tc' })]

  it('is the LATEST parent completion when every parent has finished', () => {
    const tasks = new Map([
      ['ta', task('ta', 'SUCCEEDED', { completed_at: iso(-500) })],
      ['tb', task('tb', 'SUCCEEDED', { completed_at: iso(-300) })],
    ])
    expect(parentsDoneOf(steps[2]!, steps, tasks)).toEqual({ kind: 'at', at: T0 - 300_000 })
  })

  it('is pending while a parent is still going, none for a root, unknown for an unread parent', () => {
    const tasks = new Map([
      ['ta', task('ta', 'SUCCEEDED', { completed_at: iso(-500) })],
      ['tb', task('tb', 'RUNNING', { started_at: iso(-400) })],
    ])
    expect(parentsDoneOf(steps[2]!, steps, tasks)).toEqual({ kind: 'pending', stepIds: ['b'] })
    expect(parentsDoneOf(steps[0]!, steps, tasks)).toEqual({ kind: 'none' })
    expect(parentsDoneOf(steps[2]!, steps, new Map([['ta', tasks.get('ta')!]]))).toEqual({ kind: 'unknown' })
  })
})

describe('#107: stepTimes splits the wait at max(parent.completed_at)', () => {
  it('draws waiting-on-parents, then queued, then ran', () => {
    const t = stepTimes(
      joined(task('c', 'SUCCEEDED', { created_at: iso(-600), started_at: iso(-200), completed_at: iso(-100) })),
      T0,
      { kind: 'at', at: T0 - 300_000 },
    )
    expect(t.spans.map((s) => [s.kind, (s.to - s.from) / 1000])).toEqual([
      ['parents', 300],
      ['waited', 100],
      ['ran', 100],
    ])
    expect(t.parentsMs).toBe(300_000)
    // THE SORT VALUE IS THE QUEUE: the part of the wait that was this step's.
    expect(t.waitedMs).toBe(100_000)
    expect(t.sentence).toMatch(/^waited 5m 0s on its parents, then queued 1m 40s until its start, then ran 1m 40s/)
  })

  it('draws a step still waiting on a parent as waiting on parents, open, and not as queued', () => {
    const t = stepTimes(joined(task('c', 'QUEUED', { created_at: iso(-600) })), T0, { kind: 'pending', stepIds: ['b'] })
    expect(t.spans.map((s) => [s.kind, s.open])).toEqual([['parents', true]])
    expect(t.parentsMs).toBe(600_000)
    expect(t.waitedMs).toBeNull()
    expect(t.sentence).toContain('on its parents (b)')
  })

  it('splits a step still waiting after its parents finished, with the queued part open', () => {
    const t = stepTimes(joined(task('c', 'READY', { created_at: iso(-600) })), T0, { kind: 'at', at: T0 - 120_000 })
    expect(t.spans.map((s) => [s.kind, s.open])).toEqual([
      ['parents', false],
      ['waiting', true],
    ])
    expect(t.waitedMs).toBe(120_000)
    expect(t.waitedOpen).toBe(true)
  })

  it('does not split what it cannot place: a root, or a parent nobody read', () => {
    const done = task('c', 'SUCCEEDED', { created_at: iso(-600), started_at: iso(-200), completed_at: iso(-100) })
    for (const parents of [{ kind: 'none' as const }, { kind: 'unknown' as const }]) {
      const t = stepTimes(joined(done), T0, parents)
      expect(t.spans.map((s) => s.kind)).toEqual(['waited', 'ran'])
      expect(t.waitedMs).toBe(400_000)
      expect(t.parentsMs).toBeNull()
    }
  })
})

/** The drawn (broken) width of an axis, in ms: its real width less every break. */
function drawnWidth(axis: TimelineAxis): number {
  return axis.t1 - axis.t0 - axis.breaks.reduce((n, b) => n + Math.max(0, Math.min(axis.t1, b.to) - Math.max(axis.t0, b.from)), 0)
}

describe('#107: axisOf clamps an outlier wait beyond the 95th percentile', () => {
  /** Twenty finished steps with a ten-second wait and a one-minute run each,
   *  and one whose wait was three hours. */
  function rows() {
    const out = Array.from({ length: 20 }, (_, i) =>
      stepTimes(
        joined(task(`s${i}`, 'SUCCEEDED', { created_at: iso(-600), started_at: iso(-590), completed_at: iso(-530) })),
        T0,
      ),
    )
    out.push(
      stepTimes(
        joined(task('slow', 'SUCCEEDED', { created_at: iso(-600 - 3 * 3600), started_at: iso(-590), completed_at: iso(-530) })),
        T0,
      ),
    )
    return out
  }

  it('cuts the outlier to the threshold, keeps its end, and keeps the real length to print', () => {
    const r = rows()
    const axis = axisOf(r, T0, null)!
    // 42 spans, sorted: twenty 10s waits, twenty-one 60s runs, one 3h wait.
    // The nearest-rank 95th percentile is the 40th: 60s.
    expect(axis.clampMs).toBe(60_000)
    const slow = r[20]!.spans[0]!
    const drawn = drawnSpan(axis, slow)
    expect(drawn.clampedMs).toBe(slow.to - slow.from)
    expect(drawn.to).toBe(slow.to)
    expect(drawn.breaks).toEqual([slow.from])
    // Drawn at the threshold: the time before its last minute is taken out.
    const drawnMs = ((pctOf(axis, slow.to) - pctOf(axis, slow.from)) / 100) * drawnWidth(axis)
    expect(drawnMs).toBeCloseTo(60_000, 3)
    // An ordinary span is drawn as it is.
    const ordinary = drawnSpan(axis, r[0]!.spans[0]!)
    expect(ordinary.clampedMs).toBeNull()
    // And a RUN is never clamped, however long: only a wait is cut.
    expect(drawnSpan(axis, { kind: 'ran', from: T0 - 9e6, to: T0, open: false }).clampedMs).toBeNull()
  })

  it('BREAKS THE AXIS when every step is submitted together and one starts hours late', () => {
    // The ordinary case: twenty roots submitted at -600s run in minutes; one
    // more, submitted with them, queues three hours and then runs a minute.
    // Trimming the front of its wait alone leaves the axis three hours long,
    // because its run still ends three hours out.
    const late = 3 * 3600
    const r = Array.from({ length: 20 }, (_, i) =>
      stepTimes(
        joined(task(`s${i}`, 'SUCCEEDED', { created_at: iso(-late - 600), started_at: iso(-late - 590), completed_at: iso(-late - 530) })),
        T0,
      ),
    )
    r.push(
      stepTimes(
        joined(task('slow', 'SUCCEEDED', { created_at: iso(-late - 600), started_at: iso(-60), completed_at: iso(0) })),
        T0,
      ),
    )
    const axis = axisOf(r, T0, Date.parse(iso(-late - 600)))!
    expect(axis.clampMs).toBe(60_000)
    expect(axis.breaks.length).toBe(1)
    // THE DRAWN AXIS IS MINUTES, NOT HOURS.
    expect(drawnWidth(axis)).toBeLessThan(5 * 60_000)
    // Every other row's run is a readable bar, not a sliver.
    const ran = r[0]!.spans.find((s) => s.kind === 'ran')!
    expect(pctOf(axis, ran.to) - pctOf(axis, ran.from)).toBeGreaterThan(20)
    // The slow step's run keeps its real length on the drawn axis: runs are never cut.
    const slowRan = r[20]!.spans.find((s) => s.kind === 'ran')!
    expect(((pctOf(axis, slowRan.to) - pctOf(axis, slowRan.from)) / 100) * drawnWidth(axis)).toBeCloseTo(60_000, 3)
    // And the break is only where nothing else was drawn: after the others ended.
    expect(axis.breaks[0]!.from).toBeGreaterThanOrEqual(ran.to)
    // A tick after the break reads the REAL time since the start, hours on.
    expect(axis.ticks.length).toBeLessThanOrEqual(6)
    expect(axis.ticks.at(-1)!.label).toMatch(/^\+3h/)
  })

  it('never takes out time another step ran in', () => {
    // An outlier queue with another step running inside it: the run is drawn
    // whole, so only the gaps either side of it are taken out.
    const others = Array.from({ length: 20 }, (_, i) =>
      stepTimes(joined(task(`s${i}`, 'SUCCEEDED', { created_at: iso(-7200), started_at: iso(-7190), completed_at: iso(-7130) })), T0),
    )
    const mid = stepTimes(joined(task('mid', 'SUCCEEDED', { created_at: iso(-3600), started_at: iso(-3600), completed_at: iso(-3540) })), T0)
    const slow = stepTimes(joined(task('slow', 'SUCCEEDED', { created_at: iso(-7200), started_at: iso(-60), completed_at: iso(0) })), T0)
    const axis = axisOf([...others, mid, slow], T0, null)!
    const run = mid.spans.find((s) => s.kind === 'ran')!
    for (const b of axis.breaks) expect(b.to <= run.from || b.from >= run.to, 'a break cuts a run').toBe(true)
    expect(axis.breaks.length).toBe(2)
    expect(drawnSpan(axis, slow.spans[0]!).breaks.length).toBe(2)
  })

  it('clamps nothing on a small workflow, where the 95th percentile is the longest span', () => {
    const axis = axisOf(rows().slice(18), T0, null)!
    for (const s of rows().slice(18).flatMap((x) => x.spans)) expect(drawnSpan(axis, s).clampedMs).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// #105: a failure's cause
// ---------------------------------------------------------------------------

describe('#105: the cause of a failure', () => {
  it('takes the first line, and normalises a cause so the same failure groups', () => {
    expect(firstLine('input collision: plan.md\nTraceback ...')).toBe('input collision: plan.md')
    expect(failureCause('input collision: `plan.md` is staged by both a and b\nmore')).toBe('input collision')
    expect(failureCause('Input collision: notes.md from tsk_1234abcd')).toBe('input collision')
    expect(failureCause('exit 1: the agent crashed')).toBe('exit 1')
    expect(failureCause('lease lost')).toBe('lease lost')
    expect(failureCause(null)).toBeNull()
    expect(failureCause('   \n')).toBeNull()
  })

  it('groups a workflow’s failed steps by cause, largest first', () => {
    const tasks = new Map<string, Task>()
    const steps: WorkflowStep[] = []
    const add = (id: string, state: TaskState, last_error: string | null) => {
      tasks.set(`t_${id}`, task(`t_${id}`, state, { last_error }))
      steps.push(step(id, [], { task_id: `t_${id}` }))
    }
    add('a', 'FAILED', 'input collision: a.md')
    add('b', 'FAILED', 'exit 1: boom')
    add('c', 'FAILED', 'input collision: c.md')
    add('d', 'FAILED', 'Input collision: d.md')
    add('e', 'FAILED', 'input collision: e.md')
    add('f', 'SUCCEEDED', null)
    add('g', 'FAILED', null)
    expect(failureGroups(steps, tasks)).toEqual([
      { cause: 'input collision', n: 4 },
      { cause: 'exit 1', n: 1 },
      { cause: null, n: 1 },
    ])
  })
})

// ---------------------------------------------------------------------------
// #106: why a step is not running
// ---------------------------------------------------------------------------

describe('#106: stepWhy', () => {
  const steps = [
    step('plan', [], { task_id: 't_plan' }),
    step('build', ['plan'], { task_id: 't_build' }),
    step('ship', ['build', 'plan'], { task_id: 't_ship' }),
    step('later', ['ship']),
  ]

  it('says a READY step held by a pool why, in the words the Agents list uses, with its ink', () => {
    const t = task('t_build', 'READY', {
      blocked_by: [{ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 0, active: 0 }],
    })
    const tasks = new Map([['t_plan', task('t_plan', 'SUCCEEDED', { completed_at: iso(-60) })], ['t_build', t]])
    const why = stepWhy(steps[1]!, joined(t), steps, tasks, null)!
    expect(why.text).toMatch(/paused by operator \(limit 0\)/i)
    expect(why.warn).toBe(true)
  })

  it('says a routine park in plain ink, and a step waiting on its parents names them', () => {
    const parked = task('t_build', 'PARKED', { park_reason: 'QUOTA_EXHAUSTED' })
    const tasks = new Map([['t_plan', task('t_plan', 'SUCCEEDED', { completed_at: iso(-60) })], ['t_build', parked]])
    const p = stepWhy(steps[1]!, joined(parked), steps, tasks, null)!
    expect(p.text.length).toBeGreaterThan(0)
    expect(p.warn).toBe(false)
    const queued = task('t_ship', 'QUEUED')
    tasks.set('t_ship', queued)
    const q = stepWhy(steps[2]!, joined(queued), steps, tasks, null)!
    expect(q.text).toBe('waiting on build')
    expect(q.warn).toBe(false)
  })

  it('upgrades the cascade copy to name the parent that failed, on ANY parent failing', () => {
    const tasks = new Map([
      ['t_plan', task('t_plan', 'SUCCEEDED', { completed_at: iso(-300) })],
      ['t_build', task('t_build', 'DEAD_LETTERED', { completed_at: iso(-200) })],
    ])
    const cascade = task('t_ship', 'CANCELLED', { completed_at: iso(-190), depends_on: ['build', 'plan'] })
    tasks.set('t_ship', cascade)
    expect(stepWhy(steps[2]!, joined(cascade), steps, tasks, null)!.text).toBe('blocked: build failed')
    // A person's cancel is not a cascade, whatever its parents did.
    const asked = { ...cascade, cancel_requested: true }
    expect(stepWhy(steps[2]!, joined(asked), steps, tasks, null)!.text).toBe('Cancelled by request.')
    // And a step with no task yet under a failed parent says so too.
    tasks.set('t_ship', task('t_ship', 'FAILED', { last_error: 'boom' }))
    expect(stepWhy(steps[3]!, { kind: 'unstarted' }, steps, tasks, null)!.text).toBe('blocked: ship failed')
  })

  it('gives a failure its first line, the whole error underneath, and nothing for a running or finished step', () => {
    const failed = task('t_build', 'FAILED', { last_error: 'input collision: plan.md\nstaged twice' })
    const f = stepWhy(steps[1]!, joined(failed), steps, new Map([['t_build', failed]]), null)!
    expect(f).toMatchObject({ kind: 'cause', text: 'input collision: plan.md', full: 'input collision: plan.md\nstaged twice', warn: true })
    const running = task('t_build', 'RUNNING', { started_at: iso(-10) })
    expect(stepWhy(steps[1]!, joined(running), steps, new Map([['t_build', running]]), null)).toBeNull()
    const ok = task('t_build', 'SUCCEEDED', { started_at: iso(-10), completed_at: iso(-5) })
    expect(stepWhy(steps[1]!, joined(ok), steps, new Map([['t_build', ok]]), null)).toBeNull()
  })
})

describe('#105/#106: a node that carries a cause or a why line is measured for it', () => {
  it('adds one micro line and its gap to the node, and the layout gives the level that height', () => {
    const s = [step('a', []), step('b', ['a'])]
    // One `.node-note` at --t-micro/--lh-micro (17.4) and the `--ctl-s1` gap
    // above it, rounded up the way every node height is: 22.
    const line = Math.ceil(12 * 1.45 + 4)
    expect(heightOf(s[0]!, 'figures', undefined, true)).toBe(nodeHeightAt('figures') + line)
    expect(heightOf(s[1]!, 'names', 200, true) - heightOf(s[1]!, 'names', 200)).toBe(line)
    const plain = layoutOf(s)
    const noted = layoutOf(s, undefined, 'figures', new Set(['b']))
    const h = (l: typeof plain, id: string) => l.nodes.find((n) => n.step.step_id === id)!.h
    expect(h(noted, 'b')).toBe(h(plain, 'b') + line)
    expect(h(noted, 'a')).toBe(h(plain, 'a'))
    expect(noted.height).toBe(plain.height + line)
  })
})
