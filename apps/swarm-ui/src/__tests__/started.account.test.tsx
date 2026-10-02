// WHEN AN AGENT OR A WORKFLOW STARTED (#376), AND ON WHICH ACCOUNT (#379).
//
// #376: a sortable STARTED column on Work > Agents for every state, finished
// ones included; the start beside the state in the inspector's head with the
// submit and end times; a workflow's start DERIVED from its earliest step on
// the board row and in the open card. One formatter (`clockTime`): local
// `HH:MM:SS` today, `MM-DD HH:MM` older, the UTC instant and its age in the
// hover. A task that never started reads `never started` with its submit
// time -- never blank, never a time borrowed from another field.
//
// #379: an ACCOUNT column, the inspector head, and each attempt's own account
// with its swaps (`acct-eng-01 → 02 (swapped: unreadable)`). The words for
// "none" are the API's: `no model call`, `not assigned yet`, `not read`.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { AgentRun } from '../api'
import type { Task, TaskAccount, TaskPage, TaskState, Workflow, WorkflowStep } from '../types'
import { accountText, clockTime, compareStarted, startedOf, workflowStart, workflowStartText } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { Run } from '../AgentDetail'
import { WorkflowCard } from '../Workflows'

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// The formatter
// ---------------------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, '0')

describe('clockTime: one formatter for every start and submit time', () => {
  it('prints HH:MM:SS for an instant earlier today, in local time', () => {
    const now = new Date(2026, 9, 1, 15, 0, 0).getTime()
    const t = new Date(2026, 9, 1, 9, 4, 7)
    expect(clockTime(t.toISOString(), now)?.text).toBe('09:04:07')
  })

  it('prints MM-DD HH:MM for an instant on an earlier day', () => {
    const now = new Date(2026, 9, 1, 15, 0, 0).getTime()
    const t = new Date(2026, 8, 28, 23, 41, 59)
    expect(clockTime(t.toISOString(), now)?.text).toBe('09-28 23:41')
  })

  it('puts the full UTC instant and its age in the hover', () => {
    const now = Date.parse('2026-10-01T12:00:00.000Z')
    const out = clockTime('2026-10-01T11:00:00.000Z', now)
    expect(out?.title).toContain('2026-10-01T11:00:00.000Z')
    expect(out?.title).toContain('1h ago')
  })

  it('returns null for a missing or unparseable time, so the caller says what that means', () => {
    expect(clockTime(null)).toBeNull()
    expect(clockTime(undefined)).toBeNull()
    expect(clockTime('')).toBeNull()
    expect(clockTime('not a time')).toBeNull()
  })

  it('reads the wall clock of the timezone it runs in, not UTC', () => {
    const before = process.env.TZ
    try {
      process.env.TZ = 'Asia/Tokyo'
      // 02:30 UTC is 11:30 in Tokyo, the same calendar day.
      const instant = '2026-10-01T02:30:00.000Z'
      expect(new Date(instant).getHours(), 'the TZ switch did not take effect').toBe(11)
      const now = Date.parse('2026-10-01T05:00:00.000Z')
      expect(clockTime(instant, now)?.text).toBe('11:30:00')

      process.env.TZ = 'America/New_York'
      // The same instant is 22:30 the PREVIOUS day in New York: not today.
      expect(new Date(instant).getHours()).toBe(22)
      expect(clockTime(instant, now)?.text).toBe('09-30 22:30')
      // The hover keeps the UTC instant whatever the zone.
      expect(clockTime(instant, now)?.title).toContain(instant)
    } finally {
      if (before === undefined) delete process.env.TZ
      else process.env.TZ = before
    }
  })
})

// ---------------------------------------------------------------------------
// Started, and the sort
// ---------------------------------------------------------------------------

const NOW = Date.parse('2026-10-01T12:00:00.000Z')
const iso = (minutesBefore: number) => new Date(NOW - minutesBefore * 60_000).toISOString()

describe('startedOf and the STARTED sort', () => {
  it('says never started, with the submit time, when there is no start', () => {
    const s = startedOf({ started_at: null, created_at: iso(30), attempt_count: 0 }, NOW)
    expect(s.text).toBe('never started')
    expect(s.never).toBe(true)
    const sub = new Date(NOW - 30 * 60_000)
    expect(s.submitted).toBe(`${pad(sub.getHours())}:${pad(sub.getMinutes())}:${pad(sub.getSeconds())}`)
  })

  it('names a retried task’s start as the latest attempt’s, never the first', () => {
    const s = startedOf({ started_at: iso(5), created_at: iso(60), attempt_count: 3 }, NOW)
    expect(s.title).toMatch(/latest attempt \(of 3\) started/)
  })

  it('orders by start, newest first, with never-started rows after every started one', () => {
    const rows = [
      mk('task_never_a', 'CANCELLED', { started_at: null, created_at: iso(10) }),
      mk('task_old', 'SUCCEEDED', { started_at: iso(50) }),
      mk('task_new', 'FAILED', { started_at: iso(5) }),
      mk('task_never_b', 'READY', { started_at: null, created_at: iso(1) }),
    ]
    expect([...rows].sort((a, b) => compareStarted(a, b)).map((t) => t.id)).toEqual([
      'task_new',
      'task_old',
      'task_never_b',
      'task_never_a',
    ])
    expect([...rows].sort((a, b) => compareStarted(a, b, true)).map((t) => t.id)).toEqual([
      'task_old',
      'task_new',
      'task_never_b',
      'task_never_a',
    ])
  })
})

// ---------------------------------------------------------------------------
// The account words
// ---------------------------------------------------------------------------

function acct(over: Partial<TaskAccount> = {}): TaskAccount {
  return {
    status: 'assigned',
    account_id: 'acct-eng-02',
    provider: 'anthropic',
    attempt_id: 'att_2',
    generation: 2,
    swapped_from: null,
    swaps: [],
    ...over,
  }
}

describe('accountText', () => {
  it('prints the account id', () => {
    expect(accountText(acct()).text).toBe('acct-eng-02')
  })

  it('prints a swap as the account given back, the tail of the new one, and why', () => {
    const a = acct({ swapped_from: 'acct-eng-01', swaps: [{ account_id: 'acct-eng-01', cause: 'unreadable' }] })
    expect(accountText(a).text).toBe('acct-eng-01 → 02 (swapped: unreadable)')
    expect(accountText(a).title).toContain('acct-eng-02')
  })

  it('says the API’s answer for every account it does not have, never a guess', () => {
    const none = { account_id: null, provider: null, attempt_id: null, generation: null }
    expect(accountText(acct({ status: 'no_model_call', ...none })).text).toBe('no model call')
    expect(accountText(acct({ status: 'not_assigned_yet', ...none })).text).toBe('not assigned yet')
    expect(accountText(acct({ status: 'not_assigned', ...none })).text).toBe('not assigned')
    expect(accountText(acct({ status: 'unread', ...none })).text).toBe('not read')
    expect(accountText(undefined).text).toBe('not read')
    expect(accountText(null).known).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The Agents list
// ---------------------------------------------------------------------------

function mk(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id,
    state,
    created_at: iso(120),
    updated_at: iso(1),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? iso(1) : null,
    ...over,
  })
}

async function land(tasks: Task[]): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={null} />)
  await waitFor(() => expect(container.querySelector('.rows .row.clickable')).not.toBeNull())
  return container as HTMLElement
}

function rowIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('.rows .row.clickable')].map((e) => e.dataset.taskId ?? '')
}

describe('Work > Agents: the start and the account (#376, #379) under agents.html V1', () => {
  // THE COLUMNS WENT WITH THE TABLE (#503): V1's list keeps the mark, name,
  // elapsed, profile, owner and try, and "the detail gets the rest". The
  // start, the submit time and the account are the inspector head's facts,
  // pinned in the describe below; the start SORT stays the list's, as a
  // control of the list header instead of a column head.
  it('draws neither column in the list, which is the detail head’s to say', async () => {
    const c = await land([mk('task_a', 'SUCCEEDED', { started_at: iso(30), account: acct({ account_id: 'acct-eng-01' }) })])
    expect(c.querySelector('.rows .row.is-head')).toBeNull()
    expect(c.querySelector('.rows .started, .rows .acct')).toBeNull()
    expect(c.querySelector('.rows')!.textContent).not.toContain('acct-eng-01')
  })

  it('sorts by start from the list header, newest then oldest, never-started last both ways', async () => {
    const c = await land([
      mk('task_mid', 'SUCCEEDED', { started_at: iso(20), updated_at: iso(1) }),
      mk('task_never', 'CANCELLED', { started_at: null, updated_at: iso(2) }),
      mk('task_new', 'FAILED', { started_at: iso(5), updated_at: iso(3) }),
      mk('task_old', 'SUCCEEDED', { started_at: iso(60), updated_at: iso(4) }),
    ])
    // The list's own order first: most recently changed.
    expect(rowIds(c)).toEqual(['task_mid', 'task_never', 'task_new', 'task_old'])
    const button = within(c.querySelector('.ag-list-head') as HTMLElement).getByRole('button', { name: /start/i })
    expect(button.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(button)
    expect(rowIds(c)).toEqual(['task_new', 'task_mid', 'task_old', 'task_never'])
    expect(button.getAttribute('aria-label')).toBe('Sorted by start, newest first')
    fireEvent.click(button)
    expect(rowIds(c)).toEqual(['task_old', 'task_mid', 'task_new', 'task_never'])
    expect(button.getAttribute('aria-label')).toBe('Sorted by start, oldest first')
    fireEvent.click(button)
    expect(rowIds(c)).toEqual(['task_mid', 'task_never', 'task_new', 'task_old'])
    expect(button.getAttribute('aria-pressed')).toBe('false')
  })
})

// ---------------------------------------------------------------------------
// The agent inspector
// ---------------------------------------------------------------------------

function agentRun(t: Task, attempts = [attempt(1)]): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function fact(c: HTMLElement, key: string): HTMLElement | undefined {
  return [...c.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent?.trim() === key) as
    | HTMLElement
    | undefined
}

describe('the agent inspector head', () => {
  beforeEach(() => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  })

  it('shows started, submitted, ended and the account beside the state', () => {
    const t = runTask({
      state: 'SUCCEEDED',
      created_at: iso(90),
      started_at: iso(80),
      completed_at: iso(10),
      account: acct({ swapped_from: 'acct-eng-01', swaps: [{ account_id: 'acct-eng-01', cause: 'unreadable' }] }),
    })
    const { container } = render(<Run run={agentRun(t)} />)
    const head = container.querySelector('.section.panel') as HTMLElement
    expect(fact(head, 'started')?.textContent).toContain(clockTime(iso(80))!.text)
    expect(fact(head, 'started')?.getAttribute('title')).toContain(new Date(NOW - 80 * 60_000).toISOString())
    expect(fact(head, 'submitted')?.textContent).toContain(clockTime(iso(90))!.text)
    expect(fact(head, 'ended')?.textContent).toContain(clockTime(iso(10))!.text)
    expect(fact(head, 'account')?.textContent).toContain('acct-eng-01 → 02 (swapped: unreadable)')
  })

  it('says never started on a cancelled task that never ran, with its submit time', () => {
    const t = runTask({ state: 'CANCELLED', created_at: iso(45), started_at: null, completed_at: iso(40) })
    const { container } = render(<Run run={agentRun(t, [])} />)
    const head = container.querySelector('.section.panel') as HTMLElement
    expect(fact(head, 'started')?.textContent).toContain('never started')
    expect(fact(head, 'submitted')?.textContent).toContain(clockTime(iso(45))!.text)
    expect(fact(head, 'account')?.textContent).toContain('not read')
  })

  it('shows each attempt’s own account, with its swap, on the attempt card', async () => {
    const t = runTask({ state: 'SUCCEEDED', attempt_count: 2, started_at: iso(30), completed_at: iso(5) })
    const attempts = [
      attempt(1, {
        account: acct({
          account_id: 'acct-eng-02',
          generation: 1,
          swapped_from: 'acct-eng-01',
          swaps: [{ account_id: 'acct-eng-01', cause: 'unreadable' }],
        }),
      }),
      attempt(2, { created_at: iso(20), account: acct({ account_id: 'acct-eng-04', generation: 2 }) }),
    ]
    const { container } = render(<Run run={agentRun(t, attempts)} />)
    await waitFor(() => expect(container.querySelectorAll('.att-card').length).toBe(2))
    const cards = [...container.querySelectorAll('.att-card')] as HTMLElement[]
    const texts = cards.map((card) => fact(card, 'account')?.textContent ?? '')
    expect(texts).toContain('accountacct-eng-01 → 02 (swapped: unreadable)')
    expect(texts).toContain('accountacct-eng-04')
  })
})

// ---------------------------------------------------------------------------
// Workflows: the start derived from the earliest step
// ---------------------------------------------------------------------------

function step(step_id: string, task_id: string | null): WorkflowStep {
  return {
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on: [],
    input_from: {},
    task_id,
  } as WorkflowStep
}

function wf(steps: WorkflowStep[], over: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id: 'wf_started',
    state: 'RUNNING',
    tenant_id: 'acme',
    stored_state: 'RUNNING',
    state_source: 'derived',
    created_at: iso(100),
    updated_at: iso(1),
    submitted_by: 'ada@acme.test',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
    ...over,
  }
}

describe('a workflow’s start, derived from its steps', () => {
  const steps = [step('a', 'task_wa'), step('b', 'task_wb'), step('c', 'task_wc')]

  it('is the earliest step start', () => {
    const byId = new Map([
      ['task_wa', mk('task_wa', 'SUCCEEDED', { started_at: iso(50) })],
      ['task_wb', mk('task_wb', 'RUNNING', { started_at: iso(70) })],
      ['task_wc', mk('task_wc', 'READY', { started_at: null })],
    ])
    expect(workflowStart(wf(steps), byId)).toEqual({ kind: 'started', at: iso(70), partial: false })
  })

  it('is never started only when every step task was read, none started and all have finished', () => {
    const ended = new Map(
      ['task_wa', 'task_wb', 'task_wc'].map((id) => [id, mk(id, 'CANCELLED', { started_at: null })] as const),
    )
    expect(workflowStart(wf(steps), ended)).toEqual({ kind: 'never', settled: true })
    expect(workflowStartText(wf(steps), ended, NOW).text).toBe('never started')
    // Still live, none started: not yet, which is not the same claim.
    const waiting = new Map(
      ['task_wa', 'task_wb', 'task_wc'].map((id) => [id, mk(id, 'READY', { started_at: null })] as const),
    )
    expect(workflowStart(wf(steps), waiting)).toEqual({ kind: 'never', settled: false })
    expect(workflowStartText(wf(steps), waiting, NOW).text).toBe('not started yet')
  })

  it('is not read -- never never-started -- when the step tasks were not read', () => {
    expect(workflowStart(wf(steps), null)).toEqual({ kind: 'unread' })
    const partial = new Map([['task_wa', mk('task_wa', 'READY', { started_at: null })]])
    expect(workflowStart(wf(steps), partial)).toEqual({ kind: 'unread' })
    expect(workflowStartText(wf(steps), null, NOW).text).toBe('start not read')
  })

  it('says so when an earlier start may be among the steps not read', () => {
    const partial = new Map([['task_wb', mk('task_wb', 'RUNNING', { started_at: iso(70) })]])
    expect(workflowStart(wf(steps), partial)).toEqual({ kind: 'started', at: iso(70), partial: true })
    expect(workflowStartText(wf(steps), partial, NOW).title).toMatch(/some were not/)
  })

  function Card({ workflow, byId }: { workflow: Workflow; byId: Map<string, Task> | null }) {
    const [stages, setStages] = useState<Record<string, boolean>>({})
    return (
      <WorkflowCard
        workflow={workflow}
        taskById={byId}
        usage={{ kind: 'ready', usage: null }}
        openStages={stages}
        onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
        reload={() => {}}
      />
    )
  }

  // The board's row is gone with the rebrand (2026-10-01); the open card's
  // `.wf-times` facts are where the derived start and the submit are stated.
  it('prints the derived start and the submit time in the open card', () => {
    const byId = new Map([
      ['task_wa', mk('task_wa', 'SUCCEEDED', { started_at: iso(50) })],
      ['task_wb', mk('task_wb', 'RUNNING', { started_at: iso(70) })],
      ['task_wc', mk('task_wc', 'READY', { started_at: null })],
    ])
    const { container } = render(<Card workflow={wf(steps)} byId={byId} />)
    const times = container.querySelector('.wf-times') as HTMLElement
    expect(fact(times, 'started')?.textContent).toContain(clockTime(iso(70))!.text)
    expect(fact(times, 'submitted')?.textContent).toContain(clockTime(iso(100))!.text)
  })

  it('prints never started in the open card when every step ended without starting', () => {
    const byId = new Map(
      ['task_wa', 'task_wb', 'task_wc'].map((id) => [id, mk(id, 'CANCELLED', { started_at: null })] as const),
    )
    const { container } = render(<Card workflow={wf(steps)} byId={byId} />)
    const times = container.querySelector('.wf-times') as HTMLElement
    expect(fact(times, 'started')?.textContent).toContain('never started')
    expect(fact(times, 'submitted')?.textContent).toContain(clockTime(iso(100))!.text)
  })
})
