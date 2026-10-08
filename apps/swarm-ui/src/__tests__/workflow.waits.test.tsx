/**
 * THE WORKFLOWS SCREENS' WAITS AND ORDER (#503, console QA epic).
 *
 *  - The list's default order: running work first, then parked / ready /
 *    queued, then succeeded, then failed, newest first in each group.
 *  - A graph node for a step that holds a slot and has not started (LEASED,
 *    DISPATCHED) says its own state, never `queued 3m`.
 *  - A PARKED node is timed from its PARKED event, not the record's last write,
 *    which a re-check moves on and so read minutes short. Without the event the
 *    last write is only a lower bound, and the node says `≥`.
 */
import STYLES from '../styles.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { Task, TaskEvent, TaskEventsPage, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadTaskEventsPage: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowCard } = await import('../Workflows')
const { stepDuration } = await import('../dag')
const { stateEnteredAt } = await import('../stepviews')
const wl = await import('../workflowlist')

const T0 = Date.parse('2026-10-07T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'sam@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_waits',
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

function workflow(id: string, state: TaskState | null, steps: WorkflowStep[], ageSeconds = 600): Workflow {
  return {
    workflow_id: id,
    state: state ?? 'RUNNING',
    tenant_id: 'eng',
    stored_state: state ?? 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: state ?? 'RUNNING',
      complete: state !== null,
      reason: 'steps_hold_capacity',
      counts: {},
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: 'sam@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
}

function event(task_id: string, type: string, atSeconds: number): TaskEvent {
  return { event_id: `${task_id}-${type}-${atSeconds}`, task_id, type, at: iso(atSeconds), attempt_id: null, lease_id: null, generation: null, detail: null }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
})

// ---------------------------------------------------------------------------

describe('the list sorts running work first and failed last, newest first in each (#503)', () => {
  // Ages are seconds since submission: a smaller age is newer.
  const rows = [
    workflow('failed_new', 'FAILED', [], 100),
    workflow('failed_old', 'FAILED', [], 900),
    workflow('succeeded', 'SUCCEEDED', [], 50),
    workflow('queued', 'QUEUED', [], 10),
    workflow('ready', 'READY', [], 20),
    workflow('parked', 'PARKED', [], 30),
    workflow('running_old', 'RUNNING', [], 800),
    workflow('leased_new', 'LEASED', [], 40),
  ]

  it('puts every live workflow above every waiting one, every waiting one above succeeded, and failed last', () => {
    const order = wl.sortWorkflows(rows, 'state', null).map((w) => w.workflow_id)
    expect(order).toEqual([
      'leased_new',
      'running_old',
      'parked',
      'ready',
      'queued',
      'succeeded',
      'failed_new',
      'failed_old',
    ])
  })

  it('does not depend on the order the rows were read in', () => {
    const order = wl.sortWorkflows(rows.slice().reverse(), 'state', null).map((w) => w.workflow_id)
    expect(order[0]).toBe('leased_new')
    expect(order.at(-1)).toBe('failed_old')
  })
})

// ---------------------------------------------------------------------------

describe('when a step entered its state, from its events (#503)', () => {
  it('reads the PARKED event past the non-transitions after it', () => {
    const events = [event('t', 'queued', -900), event('t', 'parked', -431), event('t', 'quota_exhausted', -400)]
    expect(stateEnteredAt('PARKED', events)).toBe(T0 - 431_000)
  })

  it('keeps the first of a run of parks, which is when the step stopped', () => {
    const events = [event('t', 'parked', -100), event('t', 'parked', -500), event('t', 'ready', -700)]
    expect(stateEnteredAt('PARKED', events)).toBe(T0 - 500_000)
  })

  it('gives no time when the newest transition is not the state’s own entry', () => {
    // A page older than the record: the park it shows is an earlier one.
    const events = [event('t', 'parked', -900), event('t', 'ready', -300)]
    expect(stateEnteredAt('PARKED', events)).toBeNull()
  })

  it('reads lease_acquired for LEASED and dispatched for DISPATCHED, and nothing for RUNNING', () => {
    const events = [event('t', 'queued', -600), event('t', 'lease_acquired', -180)]
    expect(stateEnteredAt('LEASED', events)).toBe(T0 - 180_000)
    expect(stateEnteredAt('DISPATCHED', [...events, event('t', 'dispatched', -120)])).toBe(T0 - 120_000)
    expect(stateEnteredAt('RUNNING', events)).toBeNull()
  })
})

// ---------------------------------------------------------------------------

describe('a step that holds a slot and has not started says its state (#503)', () => {
  it.each(['LEASED', 'DISPATCHED'] as const)('%s reads its own state word and 3m 0s from its event, never queued', (state) => {
    const t = task('t', state, { created_at: iso(-600) })
    const d = stepDuration({ kind: 'state', state, task: t }, T0, T0 - 180_000)
    expect(d.text).toBe(`${state.toLowerCase()} 3m 0s`)
    expect(d.kind).toBe('held')
    expect(d.note).toContain('holds a slot')
  })

  it.each(['LEASED', 'DISPATCHED'] as const)('%s without its event is a lower bound from the last write, never queued', (state) => {
    const t = task('t', state, { created_at: iso(-600), updated_at: iso(-90) })
    const d = stepDuration({ kind: 'state', state, task: t }, T0)
    expect(d.text).toBe(`${state.toLowerCase()} ≥1m 30s`)
    expect(d.text).not.toMatch(/queued/)
  })

  it('times a second attempt’s lease from its event, which excludes the earlier run', () => {
    const t = task('t', 'LEASED', { created_at: iso(-600), started_at: iso(-450), attempt_count: 2 })
    expect(stepDuration({ kind: 'state', state: 'LEASED', task: t }, T0, T0 - 20_000).text).toBe('leased 20s')
  })
})

describe('a parked step is timed from its PARKED event (#503)', () => {
  it('uses the event, not the record’s last write', () => {
    const t = task('t', 'PARKED', { park_reason: 'QUOTA_EXHAUSTED', updated_at: iso(-60) })
    const d = stepDuration({ kind: 'state', state: 'PARKED', task: t }, T0, T0 - 431_000)
    expect(d.text).toBe('parked 7m 11s')
    expect(d.note).toContain('PARKED event')
  })

  it('says ≥ when only the last write is known', () => {
    const t = task('t', 'PARKED', { park_reason: 'QUOTA_EXHAUSTED', updated_at: iso(-60) })
    const d = stepDuration({ kind: 'state', state: 'PARKED', task: t }, T0)
    expect(d.text).toBe('parked ≥1m 0s')
    expect(d.note).toContain('At least')
  })
})

// ---------------------------------------------------------------------------

function Harness({ w, tasks }: { w: Workflow; tasks: Map<string, Task> }) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  return (
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      zoom="details"
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={() => {}}
    />
  )
}

function nodeDur(root: ParentNode, name: string): string {
  const node = [...root.querySelectorAll<HTMLElement>('.node')].find((n) => n.querySelector('.node-id')?.textContent?.startsWith(name))
  expect(node, `no node named ${name}`).toBeTruthy()
  return node!.querySelector('.node-dur')?.textContent ?? ''
}

describe('the graph reads the waiting steps’ events and draws their real state and time (#503)', () => {
  const steps = [
    step('plan', [], { task_id: 't_plan' }),
    step('held', ['plan'], { task_id: 't_held' }),
    step('leased', ['plan'], { task_id: 't_leased' }),
    step('sent', ['plan'], { task_id: 't_sent' }),
  ]
  const tasks = new Map<string, Task>([
    ['t_plan', task('t_plan', 'SUCCEEDED', { started_at: iso(-900), completed_at: iso(-700) })],
    ['t_held', task('t_held', 'PARKED', { park_reason: 'QUOTA_EXHAUSTED', updated_at: iso(-60) })],
    ['t_leased', task('t_leased', 'LEASED', { created_at: iso(-600), updated_at: iso(-170) })],
    ['t_sent', task('t_sent', 'DISPATCHED', { created_at: iso(-600), updated_at: iso(-170) })],
  ])
  const pages: Record<string, TaskEvent[]> = {
    t_held: [event('t_held', 'parked', -431), event('t_held', 'ready', -700)],
    t_leased: [event('t_leased', 'lease_acquired', -180), event('t_leased', 'queued', -600)],
    t_sent: [event('t_sent', 'dispatched', -180), event('t_sent', 'lease_acquired', -200)],
  }

  it('says ≥ from the last write until the events land, then the event’s time, and never queued', async () => {
    let land: () => void = () => {}
    const landed = new Promise<void>((r) => (land = r))
    api.loadTaskEventsPage.mockImplementation(async (id: string): Promise<Result<TaskEventsPage>> => {
      await landed
      return { status: 'ok', fetchedAt: T0, data: { events: pages[id] ?? [], next_page_token: null } }
    })
    const { container } = render(<Harness w={workflow('wf_waits', 'RUNNING', steps)} tasks={tasks} />)

    expect(nodeDur(container, 'held')).toBe('parked ≥1m 0s')
    expect(nodeDur(container, 'leased')).toBe('leased ≥2m 50s')
    expect(nodeDur(container, 'sent')).toBe('dispatched ≥2m 50s')

    land()
    await waitFor(() => expect(nodeDur(container, 'held')).toBe('parked 7m 11s'))
    await waitFor(() => expect(nodeDur(container, 'leased')).toBe('leased 3m 0s'))
    await waitFor(() => expect(nodeDur(container, 'sent')).toBe('dispatched 3m 0s'))
    for (const n of ['held', 'leased', 'sent']) expect(nodeDur(container, n)).not.toMatch(/queued/)

    // Only the three waits are read, once each: a finished step has no wait to time.
    const asked = api.loadTaskEventsPage.mock.calls.map((c) => c[0] as string).sort()
    expect(asked).toEqual(['t_held', 't_leased', 't_sent'])
  })

  it('keeps the lower bound when the events read fails', async () => {
    api.loadTaskEventsPage.mockResolvedValue({ status: 'error', error: { kind: 'network', message: 'down' } })
    const { container } = render(<Harness w={workflow('wf_waits', 'RUNNING', steps)} tasks={tasks} />)
    await waitFor(() => expect(api.loadTaskEventsPage).toHaveBeenCalledTimes(3))
    expect(nodeDur(container, 'held')).toBe('parked ≥1m 0s')
    expect(nodeDur(container, 'leased')).toBe('leased ≥2m 50s')
  })

  it('draws a held wait in the wait colour the queued and parked lines use, light and dark', () => {
    const rule = STYLES.split('\n').find((l) => l.includes('.node-dur.is-held'))
    expect(rule, 'no rule colours .node-dur.is-held').toBeTruthy()
    expect(rule).toContain('.node-dur.is-parked')
    expect(rule).toContain('var(--warn-ink)')
  })
})
