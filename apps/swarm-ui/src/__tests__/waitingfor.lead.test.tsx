// #362: A READY TASK HELD BEHIND A FULL POOL SAYS WHICH POOL REFUSES IT.
//
// `waiting_for` is served by `GET /v1/tasks[/{id}]` for a READY task, read
// live from the task's own pools (swarm_api/waiting.py). These tests hold the
// three lines the inspector and the Agents list draw from it:
//
//   full     "waiting for: tenant:eng  20/20 units"  + "(holds no capacity)"
//   paused   "waiting for: tenant:eng paused"
//   unknown  "waiting for: tenant:eng: limit unknown" -- never a 0, never full
//
// and that the `blocked_by` bar stays, labelled "last scheduler pass".
//
// MUTATION: drop the `waitingLine` branch from `whyAgent` (types.ts) and the
// Agents-list test reads the pre-#362 blocked_by sentence; drop the bar from
// `Alerts` (AgentDetail.tsx) and the inspector tests find no waiting line.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun, ResourceClasses } from '../api'
import type { Task, WaitingFor, WaitingPool } from '../types'
import { waitingLead, whyAgent, whyNeedsAction, whyNotRunning } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { Run } = await import('../AgentDetail')

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

function lead(over: Partial<WaitingPool>): WaitingPool {
  return {
    pool: 'tenant:eng',
    state: 'full',
    active: 20,
    limit: 20,
    units: 1,
    reason: 'TENANT_LIMIT',
    ...over,
  }
}

function waiting(l: WaitingPool | null, over: Partial<WaitingFor> = {}): WaitingFor {
  return {
    as_of: '2026-09-30T12:00:00Z',
    admissible_now: l === null ? true : false,
    holds_capacity: false,
    lead: l,
    pools: l ? [l] : [],
    complete: true,
    ...over,
  }
}

function readyTask(w: WaitingFor | null, over: Partial<Task> = {}): Task {
  return task({
    id: 'tsk_waiting',
    state: 'READY',
    resource_class: 'standard',
    started_at: null,
    completed_at: null,
    // What the scheduler wrote on its last pass: a different, older reading.
    blocked_by: [{ pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 50, active: 50 }],
    waiting_for: w,
    ...over,
  })
}

const FULL = waiting(lead({}))
const PAUSED = waiting(lead({ state: 'paused', reason: 'MANUAL_PAUSE', limit: 20, active: 3 }))
const UNKNOWN = waiting(lead({ state: 'unknown', limit: null, active: null, reason: null }), {
  admissible_now: null,
  complete: false,
})

function ok<T>(data: T) {
  return { status: 'ok' as const, data, fetchedAt: Date.now() }
}

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

// ---------------------------------------------------------------------------
// The lead line, as data
// ---------------------------------------------------------------------------

describe('waitingLead', () => {
  it('prints the three lines', () => {
    expect(waitingLead(FULL)).toBe('tenant:eng  20/20 units')
    expect(waitingLead(PAUSED)).toBe('tenant:eng paused')
    expect(waitingLead(UNKNOWN)).toBe('tenant:eng: limit unknown')
  })

  it('never renders an unknown pool as 0 or as full', () => {
    const line = waitingLead(UNKNOWN) ?? ''
    expect(line).not.toMatch(/\d/)
    expect(line).not.toMatch(/full/i)
  })

  it('is null when nothing refuses the task, or nothing was served', () => {
    expect(waitingLead(waiting(null))).toBeNull()
    expect(waitingLead(null)).toBeNull()
    expect(waitingLead(undefined)).toBeNull()
  })

  it('is preferred by whyAgent and whyNotRunning for READY only', () => {
    expect(whyAgent(readyTask(FULL))).toBe('waiting for: tenant:eng  20/20 units')
    expect(whyNotRunning(readyTask(FULL))).toBe('waiting for: tenant:eng  20/20 units')
    // No waiting_for served (an older API): the last pass's record, as before.
    expect(whyAgent(readyTask(null))).not.toMatch(/waiting for/)
    expect(whyAgent(readyTask(null))).toMatch(/50\/50/)
    // Not READY: waiting_for is ignored even if something sent it.
    expect(whyAgent(readyTask(FULL, { state: 'SUCCEEDED' }))).not.toMatch(/waiting for/)
  })

  it('asks a person to act for paused, not for full or unknown', () => {
    expect(whyNeedsAction(readyTask(PAUSED))).toBe(true)
    expect(whyNeedsAction(readyTask(FULL))).toBe(false)
    expect(whyNeedsAction(readyTask(UNKNOWN))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The agent inspector
// ---------------------------------------------------------------------------

describe('the agent inspector', () => {
  async function inspect(t: Task) {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container } = render(<Run run={run(t)} />)
    const bar = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('[data-waiting-for]')
      expect(el, 'the inspector drew no waiting-for bar').not.toBeNull()
      return el!
    })
    return { container, bar }
  }

  it('full: names the pool and its units, and says it holds no capacity', async () => {
    const { container, bar } = await inspect(readyTask(FULL))
    const leadEl = bar.querySelector('[data-waiting-lead]')!
    expect(leadEl.textContent).toBe('waiting for: tenant:eng  20/20 units')
    expect(bar.textContent).toContain('(holds no capacity)')
    // The last pass's record is still drawn, and labelled as such.
    const last = container.querySelector<HTMLElement>('[data-blocked-by]')
    expect(last, 'the blocked_by bar went').not.toBeNull()
    expect(last!.textContent).toContain('last scheduler pass')
    expect(last!.textContent).toContain('global')
    // Printed once, not again as the why sentence.
    expect(container.querySelectorAll('.why-full').length).toBe(0)
  })

  it('paused: says the pool is paused', async () => {
    const { bar } = await inspect(readyTask(PAUSED))
    expect(bar.querySelector('[data-waiting-lead]')!.textContent).toBe('waiting for: tenant:eng paused')
    expect(bar.classList.contains('amber'), 'a paused pool needs a person').toBe(true)
  })

  it('unknown: says the limit is unknown and never prints 0 or full', async () => {
    const { bar } = await inspect(readyTask(UNKNOWN))
    const text = bar.querySelector('[data-waiting-lead]')!.textContent ?? ''
    expect(text).toBe('waiting for: tenant:eng: limit unknown')
    expect(text).not.toMatch(/\d/)
    expect(text).not.toMatch(/full/i)
  })

  it('draws no waiting bar for a task that is not READY', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container } = render(
      <Run run={run(readyTask(FULL, { state: 'RUNNING', started_at: '2026-09-30T11:00:00Z' }))} />,
    )
    await waitFor(() => expect(container.querySelector('h1, h2, section')).not.toBeNull())
    expect(container.querySelector('[data-waiting-for]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Work > Agents
// ---------------------------------------------------------------------------

describe('the Agents list Why column', () => {
  it('uses the lead line', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [readyTask(FULL)], tenant_id: 'eng' }))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    render(<AgentsScreen onOpen={() => {}} />)
    const why = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.row .why')
      expect(el, 'no row drew a why line').not.toBeNull()
      return el!
    })
    expect(why.textContent).toBe('waiting for: tenant:eng  20/20 units')
    expect(why.classList.contains('is-warn'), 'a full pool clears by waiting').toBe(false)
  })

  it('an unknown lead is never drawn as 0 or full', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [readyTask(UNKNOWN)], tenant_id: 'eng' }))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    render(<AgentsScreen onOpen={() => {}} />)
    const why = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.row .why')
      expect(el).not.toBeNull()
      return el!
    })
    expect(why.textContent).toBe('waiting for: tenant:eng: limit unknown')
    expect(why.textContent ?? '').not.toMatch(/\d|full/i)
  })
})
